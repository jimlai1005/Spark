"""src/spark/publicapi/explore_fills_sync.py
單頁 `userFillsByTime` 同步演算法（spec `docs/superpowers/specs/2026-09-20-hl-leaderboard-refactor-spec.md`
§8；plan `docs/superpowers/plans/2026-09-20-explore-rate-limit-refactor.md` Task 2.2）。

純函式，**零網路、零 sleep**、不碰 `ExploreStore` 以外的任何 IO——worker 每輪只抓一頁，
呼叫端負責：`plan_page()` 決定這輪抓哪一段 → 打 HL `userFillsByTime` → `apply_page()`
算出新狀態與是否結束 → 呼叫端自行 `ExploreStore.insert_fills_page()` 落地並視 `done`
決定要不要排下一頁。

留存判準（HL `userFillsByTime` 官方只保留最近 `HL_FILLS_RETENTION_LIMIT`（10,000）
筆可查，spec §8；Task 7.5，2026-09-21 使用者裁決：這是「正確性缺陷」等級的標示
修正——舊版把內部保守門檻直接寫成 `RETENTION_LIMIT` 且未命名為何保守，容易被誤讀成
官方數字本身）：
`RETENTION_SAFETY_MARGIN`＝一頁（`USER_FILLS_PAGE_LIMIT`），
`RETENTION_SAFETY_THRESHOLD = HL_FILLS_RETENTION_LIMIT - RETENTION_SAFETY_MARGIN`
（＝8,000）。區間內的成交比區間外更新，所以若本輪回補全程觀測到的筆數
`fills_in_window < RETENTION_SAFETY_THRESHOLD`，代表區間內成交必然全部落在
「最近 `HL_FILLS_RETENTION_LIMIT` 筆」的可查範圍內 → `complete`／
`reason="count_below_retention_threshold"`（**這仍然只是門檻推論，不是留存邊界
本身的證據**——「相同 API 重抓零差異」只證明同步一致，不證明留存邊界早於窗口起點；
更強的證據見 `explore_scheduler._run_fills` 的留存邊界探測，探測成功會把 reason
升級為 `"retention_boundary_verified"`，見 `explore_store.REASON_RETENTION_BOUNDARY_VERIFIED`）；
一旦達到或超過這個門檻，無法再證明區間更早的部分沒有被擠出可查窗口 →
`partial`（`reason="retention_limit"`）。
第一輪回補在遍歷完成前恆為 `backfilling`（2026-09-20 使用者裁決：不用那個代表
「狀況不明」的英文字——`backfilling` 明確表示第一輪回補尚未完成）。空頁與正常 200
回應不改變 completeness（只有短頁／空頁終止時才會依上面的門檻判定一次）。

`cursor_ms` 推進採 inclusive 重疊（下一頁 `startTime` = 上一頁最後一筆的 `time`，
**游標不額外遞增**）——同一毫秒可能跨頁被拆散，去重交給 `fills` 表的 `(address, coin, tid)`
PRIMARY KEY，本模組不做去重判斷。同毫秒佔滿整頁（`same_ms_overflow`）與理論上不可能
發生的「滿頁但游標未推進」（`no_progress`，防禦用）都视为無法繼續往前分頁，
標記 `partial` 並終止本輪，避免無界重試同一頁。

單輪續頁另設硬上限 `max_pages_per_round`（預設 20，Task 3.7 D，S1 修法）：
20 頁＝40,000 筆已超過留存上限 `HL_FILLS_RETENTION_LIMIT`（10,000），正常資料到不了
這個頁數，達到即代表卡在異常續頁——終止本輪並標記 `partial`／
`reason="page_cap"`，避免無界重試。

`reason` 的語意（Task 7.6 A3，正確性修正）：`reason` 描述的是**建立該
`completeness` 的那次全區間遍歷**（首輪 `backfilling`，或某次增量輪的本輪
缺口超標而合法降級）所依據的證據，不是「這一秒鐘還成立嗎」的即時判定。
之後的增量輪只延伸 `synced_through_ms`——本地已經持有的區段不會因為時間
流逝而受 HL 留存邊界影響（過去的資料不會突然消失），所以增量輪收尾若沒有
新的降級證據（本輪缺口本身超標），不重新量測、不重寫 `reason`：`partial`
仍是 `partial`、`retention_boundary_verified` 跨輪存活，見 `apply_page`
短頁分支。`window_start_ms`／`window_end_ms` 是**目前這一輪**的查詢區間，
不是「reason 所描述的那次遍歷」的區間（兩者在增量輪只延伸的情況下會不同——
`reason` 仍指向較早那次遍歷，`window_*` 已經推進到最新一輪）。
"""
from __future__ import annotations

import dataclasses
from typing import NamedTuple

from spark.exchange.base import USER_FILLS_PAGE_LIMIT
from spark.publicapi.explore_store import REASON_COUNT_BELOW_RETENTION_THRESHOLD, FillsSyncState

PAGE_LIMIT = USER_FILLS_PAGE_LIMIT   # HL userFillsByTime 單頁上限（同 hl.py 常數來源，Task 3.5 D）

# Task 7.5（命名修正，工程原則 1：門檻是假設，不是事實，得先讓讀者看得出來）：
# `HL_FILLS_RETENTION_LIMIT` 是 HL 官方文件的留存上限；本模組的實際判準
# `RETENTION_SAFETY_THRESHOLD` 比官方數字保守一頁（`RETENTION_SAFETY_MARGIN`），
# 舊名 `RETENTION_LIMIT` 把兩者混為一談、且未命名保守幅度，已移除。
HL_FILLS_RETENTION_LIMIT = 10_000   # HL 官方文件：只保留最近這麼多筆可查
RETENTION_SAFETY_MARGIN = USER_FILLS_PAGE_LIMIT   # 保守緩衝＝一頁
RETENTION_SAFETY_THRESHOLD = HL_FILLS_RETENTION_LIMIT - RETENTION_SAFETY_MARGIN   # 8_000
WINDOW_DAYS = 30
OVERLAP_MS = 1
_DAY_MS = 86_400_000
_DEFAULT_INCREMENTAL_AFTER_MS = 4 * 3600 * 1000

# Task 7.5 點 4／7.6 點 8（查詢參數留證）：`hl.get_fills_page` 實際請求體只送
# `type/user/startTime/endTime`——刻意不送 `aggregateByTime`。這裡把「送了
# 什麼參數」明確寫成指紋字串，供 `fills_sync.params_fp` 落地；若未來
# `get_fills_page` 改送這個參數，這裡要跟著改（不多不少，字串必須與實際
# 請求體一致，見該函式 docstring）。字串只描述「請求體沒送這個參數」這個
# 事實本身，**不宣稱**上游會用什麼值當預設（舊字面值 `default(false)` 是
# 對上游行為的推測，未經驗證——7.5 複審 S1 修法：工程原則 1，欄位/參數的
# 語意是假設，不是事實，得先讓讀者看得出來是推測還是已驗證的事實）。
PARAMS_FP = "aggregateByTime=<omitted>"


class PagePlan(NamedTuple):
    """`plan_page()` 的輸出：這一輪要向 HL 要哪一段 `[start_ms, end_ms]`，
    `state` 是本輪起始狀態（可能是尚未落地過的新建狀態）。"""
    start_ms: int
    end_ms: int
    state: FillsSyncState

    @property
    def is_noop(self) -> bool:
        """`start_ms == end_ms`：已完成且還沒到增量時間，呼叫端不該打上游。"""
        return self.start_ms == self.end_ms


class PageResult(NamedTuple):
    """`apply_page()` 的輸出：`state` 是套用這一頁之後的新狀態（不可變、`dataclasses.replace`
    產生）；`accepted` 是通過校驗、可交給 `ExploreStore.insert_fills_page` 的 fill 清單
    （校驗失敗時為空list）；`done` 代表本輪（backfill 或 increment）是否結束；`note`
    是終止原因或校驗失敗原因（正常繼續分頁時為 `None`）。"""
    state: FillsSyncState
    accepted: list[dict]
    done: bool
    note: str | None


def plan_page(state: FillsSyncState | None, *, address: str, now_ms: int,
              window_days: int = WINDOW_DAYS, overlap_ms: int = OVERLAP_MS,
              incremental_after_ms: int = _DEFAULT_INCREMENTAL_AFTER_MS) -> PagePlan:
    """決定這一輪要抓 `[start_ms, end_ms]` 的哪一頁。

    - `state is None`：從未同步過 → 開新區間 `[now-window_days, now]`，
      游標從區間起點開始，`completeness="backfilling"`。
    - `state.completeness == "backfilling"`：上一輪回補尚未跑完 → 沿用同一個
      `window_end_ms`，從 `cursor_ms` 續抓。
    - `state.completeness in ("complete", "partial")`：上一輪已經跑到底，
      **或**目前正在跑一個尚未結束的增量輪（見下方「輪進行中」）。
      若 `cursor_ms > synced_through_ms`（Task 7.6 A1，正確性修正）：代表增量輪
      已經開始但尚未跑到 `window_end_ms`（`synced_through_ms` 只在整輪結束時
      才推進，見 `apply_page`）——續抓本輪剩餘部分：`PagePlan(start_ms=cursor_ms,
      end_ms=window_end_ms, state=state)`，不看增量寬限期、不改 `state`。
      **這個判斷必須排在增量寬限期判斷之前**：否則增量輪第一頁若剛好滿頁
      （`done=False`，`completeness` 未變、仍是舊值），下一次 tick 進來時
      `now - window_end_ms` 已經 < 寬限期（`window_end_ms` 剛推到 `now`
      附近），會被誤判成「還沒到增量時間」而回傳 `is_noop`——游標從此卡住，
      永遠不再前進（正式機尚未觀測到，需重度帳戶 4 小時內 >2,000 筆才觸發，
      但屬正確性缺陷）。
      否則（`synced_through_ms is None` 或 `cursor_ms <= synced_through_ms`，
      代表上一輪已完整跑到底）：若已過增量寬限期
      （`now - window_end_ms >= incremental_after_ms`）→ 開新的增量輪：
      `window_end_ms` 推到 `now`、`window_start_ms` 同步前推到
      `now-window_days`（更早的 fills 由 `ExploreStore.purge` 處理，這裡不刪）、
      游標從 `max(新 window_start, synced_through_ms - overlap_ms)` 開始（inclusive
      重疊，去重交給 PK）、`pages_done` 歸零；`completeness` 原樣保留（`partial`
      仍是 `partial`——增量輪只從尾端續抓，沒有重新掃過整個區間，不能因為新抓到幾筆
      就宣稱已補完先前無法證明完整的部分）。`fills_in_window` 同樣歸零（2026-09-20
      主線程裁決）：留存門檻判的是「這一輪查詢區間內觀測到的筆數」，增量輪的查詢區間
      只有 `synced_through_ms` 到 `now` 這一小段缺口，門檻要對這段缺口套用；若跨輪
      累加舊區間的計數，會在無新增風險的情況下隨時間單調上升，幾輪之後無條件觸頂，
      把本來 `complete` 的地址誤標 `partial`。
      若還沒到增量寬限期 → 回傳 `start_ms == end_ms == synced_through_ms` 的空計畫
      （`PagePlan.is_noop` 為 True），呼叫端據此不打上游。
    """
    if state is None:
        window_start_ms = now_ms - window_days * _DAY_MS
        new_state = FillsSyncState(
            address=address, window_start_ms=window_start_ms, window_end_ms=now_ms,
            cursor_ms=window_start_ms, synced_through_ms=None, observed_from_ms=None,
            observed_to_ms=None, completeness="backfilling", reason=None, pages_done=0,
            fills_in_window=0, updated_at=now_ms / 1000, last_error=None,
            params_fp=PARAMS_FP,
        )
        return PagePlan(start_ms=window_start_ms, end_ms=now_ms, state=new_state)

    if state.completeness == "backfilling":
        return PagePlan(start_ms=state.cursor_ms, end_ms=state.window_end_ms, state=state)

    if state.completeness in ("complete", "partial"):
        # Task 7.6 A1（正確性修正，必須排在增量寬限期判斷之前——見上方 docstring）：
        # `cursor_ms > synced_through_ms` 代表本輪（增量輪）已經開始抓頁但尚未
        # 跑到底，續抓剩餘部分，不看寬限期、不改 state。
        if (state.synced_through_ms is not None
                and state.cursor_ms > state.synced_through_ms):
            return PagePlan(start_ms=state.cursor_ms, end_ms=state.window_end_ms, state=state)
        if now_ms - state.window_end_ms >= incremental_after_ms:
            new_window_start = now_ms - window_days * _DAY_MS
            new_cursor = max(new_window_start, (state.synced_through_ms or new_window_start)
                              - overlap_ms)
            new_state = dataclasses.replace(
                state, window_start_ms=new_window_start, window_end_ms=now_ms,
                cursor_ms=new_cursor, pages_done=0, fills_in_window=0,
                # Task 7.5：每個新輪重寫查詢參數指紋——既是「新輪寫入本輪實際
                # 用的參數」，也順帶自我修復 migration 帶來的舊值 `''`（見
                # `explore_store._migrate_v1_to_v2`）。
                params_fp=PARAMS_FP,
            )
            return PagePlan(start_ms=new_cursor, end_ms=now_ms, state=new_state)
        through = state.synced_through_ms
        return PagePlan(start_ms=through, end_ms=through, state=state)

    raise ValueError(f"explore_fills_sync: unexpected completeness {state.completeness!r}")


def validate_page(page: list[dict], start_ms: int, end_ms: int) -> str | None:
    """回傳 `None` 代表合法；否則回傳簡短原因字串。公開名（Task 7.6 B5：
    `explore_scheduler._probe_retention_boundary` 也要用它驗證探測回應是否
    真的落在探測窗內，不再是本模組私有的實作細節）。"""
    if not isinstance(page, list):
        return "not_a_list"
    prev_time: int | None = None
    for i, f in enumerate(page):
        if not isinstance(f, dict):
            return f"item_not_dict:{i}"
        for key in ("coin", "tid", "time"):
            if key not in f:
                return f"missing_field:{key}"
        try:
            t = int(f["time"])
        except (TypeError, ValueError):
            return "time_not_int"
        try:
            int(f["tid"])
        except (TypeError, ValueError):
            return "tid_not_int"
        if prev_time is not None and t < prev_time:
            return "time_not_ascending"
        if t < start_ms or t > end_ms:
            return "time_out_of_range"
        prev_time = t
    return None


def apply_page(plan: PagePlan, page: list[dict], *, page_limit: int = PAGE_LIMIT,
               retention_threshold: int = RETENTION_SAFETY_THRESHOLD,
               max_pages_per_round: int = 20, now_ms: int) -> PageResult:
    """套用一頁 HL 回應，算出新狀態與是否結束本輪。呼叫端負責把 `plan.start_ms`／
    `plan.end_ms` 當成這次 `userFillsByTime` 的 `startTime`／`endTime`。"""
    state = plan.state
    start_ms, end_ms = plan.start_ms, plan.end_ms

    invalid_reason = validate_page(page, start_ms, end_ms)
    if invalid_reason is not None:
        new_state = dataclasses.replace(
            state, last_error=f"invalid_page:{invalid_reason}", updated_at=now_ms / 1000,
        )
        return PageResult(state=new_state, accepted=[], done=True, note=invalid_reason)

    times = [int(f["time"]) for f in page]
    observed_from = state.observed_from_ms
    observed_to = state.observed_to_ms
    if times:
        observed_from = min(times) if observed_from is None else min(observed_from, min(times))
        observed_to = max(times) if observed_to is None else max(observed_to, max(times))
    fills_in_window = state.fills_in_window + len(page)

    if len(page) < page_limit:
        # 短頁或空頁：確認遍歷到本輪 end_ms，依留存門檻判定完整性。
        # Task 7.5：`retention_threshold` 本身已是保守調整後的門檻（見模組檔頭
        # `RETENTION_SAFETY_THRESHOLD`），這裡不再另外減 `page_limit`。complete
        # 也要有 reason——這只是門檻推論，不是留存邊界本身的證據（見模組檔頭）。
        # Task 7.6 A2（正確性修正）：`state.completeness == "backfilling"` 代表
        # 這是第一輪全區間遍歷收尾，門檻推論適用於整個窗口，照舊判定。否則
        # （前態是 `complete` 或 `partial`）是增量輪收尾——增量輪只延伸
        # `synced_through`，查詢區間只有一小段缺口，不能拿這一小段的觀測筆數
        # 重新宣稱整個窗口的完整性：本輪缺口本身超標才合法降級為 `partial`
        # （新的、更強的證據）；沒超標時 `completeness`／`reason` 原樣保留
        # （`partial` 仍是 `partial`；`retention_boundary_verified` 跨輪存活——
        # 本地已持有的區段不受 HL 留存影響，不需要重新量測）。
        is_incremental_round = state.completeness != "backfilling"
        if fills_in_window >= retention_threshold:
            completeness, reason = "partial", "retention_limit"
        elif is_incremental_round:
            completeness, reason = state.completeness, state.reason
        else:
            completeness, reason = "complete", REASON_COUNT_BELOW_RETENTION_THRESHOLD
        new_state = dataclasses.replace(
            state, synced_through_ms=end_ms, observed_from_ms=observed_from,
            observed_to_ms=observed_to, completeness=completeness, reason=reason,
            fills_in_window=fills_in_window, last_error=None, updated_at=now_ms / 1000,
        )
        return PageResult(state=new_state, accepted=list(page), done=True, note=reason)

    # 滿頁。
    if all(t == start_ms for t in times):
        new_state = dataclasses.replace(
            state, cursor_ms=start_ms, synced_through_ms=start_ms,
            observed_from_ms=observed_from, observed_to_ms=observed_to,
            completeness="partial", reason="same_ms_overflow", fills_in_window=fills_in_window,
            last_error=None, updated_at=now_ms / 1000,
        )
        return PageResult(state=new_state, accepted=list(page), done=True,
                           note="same_ms_overflow")

    new_cursor = times[-1]
    if new_cursor == start_ms:
        # 理論上不可能（非降冪＋最後一筆等於起點代表全部相同，上面分支已處理），純防禦。
        new_state = dataclasses.replace(
            state, cursor_ms=start_ms, synced_through_ms=start_ms,
            observed_from_ms=observed_from, observed_to_ms=observed_to,
            completeness="partial", reason="no_progress", fills_in_window=fills_in_window,
            last_error=None, updated_at=now_ms / 1000,
        )
        return PageResult(state=new_state, accepted=list(page), done=True, note="no_progress")

    new_pages_done = state.pages_done + 1
    if new_pages_done >= max_pages_per_round:
        # Task 3.7 D（S1 修法）：單輪續頁硬上限——20 頁＝40,000 筆遠超留存上限
        # 10,000，正常資料到不了這個頁數；到頂代表卡在異常續頁（例如上游回應
        # 有問題但每頁都恰好滿頁推進），終止本輪並標記 partial，避免無界重試。
        new_state = dataclasses.replace(
            state, cursor_ms=new_cursor, synced_through_ms=new_cursor,
            observed_from_ms=observed_from, observed_to_ms=observed_to,
            completeness="partial", reason="page_cap", fills_in_window=fills_in_window,
            pages_done=new_pages_done, last_error=None, updated_at=now_ms / 1000,
        )
        return PageResult(state=new_state, accepted=list(page), done=True, note="page_cap")

    new_state = dataclasses.replace(
        state, cursor_ms=new_cursor, observed_from_ms=observed_from,
        observed_to_ms=observed_to, fills_in_window=fills_in_window,
        pages_done=new_pages_done, last_error=None, updated_at=now_ms / 1000,
    )
    return PageResult(state=new_state, accepted=list(page), done=False, note=None)
