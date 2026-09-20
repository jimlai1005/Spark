"""src/spark/publicapi/explore_fills_sync.py
單頁 `userFillsByTime` 同步演算法（spec `docs/superpowers/specs/2026-09-20-hl-leaderboard-refactor-spec.md`
§8；plan `docs/superpowers/plans/2026-09-20-explore-rate-limit-refactor.md` Task 2.2）。

純函式，**零網路、零 sleep**、不碰 `ExploreStore` 以外的任何 IO——worker 每輪只抓一頁，
呼叫端負責：`plan_page()` 決定這輪抓哪一段 → 打 HL `userFillsByTime` → `apply_page()`
算出新狀態與是否結束 → 呼叫端自行 `ExploreStore.insert_fills_page()` 落地並視 `done`
決定要不要排下一頁。

留存判準（HL `userFillsByTime` 只保留最近 `RETENTION_LIMIT` 筆可查，spec §8）：
區間內的成交比區間外更新，所以若本輪回補全程觀測到的筆數
`fills_in_window < RETENTION_LIMIT - PAGE_LIMIT`，代表區間內成交必然全部落在
「最近 RETENTION_LIMIT 筆」的可查範圍內 → `complete`；一旦達到或超過這個門檻，
無法再證明區間更早的部分沒有被擠出可查窗口 → `partial`（`reason="retention_limit"`）。
第一輪回補在遍歷完成前恆為 `backfilling`（2026-09-20 使用者裁決：不用那個代表
「狀況不明」的英文字——`backfilling` 明確表示第一輪回補尚未完成）。空頁與正常 200
回應不改變 completeness（只有短頁／空頁終止時才會依上面的門檻判定一次）。

`cursor_ms` 推進採 inclusive 重疊（下一頁 `startTime` = 上一頁最後一筆的 `time`，
**游標不額外遞增**）——同一毫秒可能跨頁被拆散，去重交給 `fills` 表的 `(address, coin, tid)`
PRIMARY KEY，本模組不做去重判斷。同毫秒佔滿整頁（`same_ms_overflow`）與理論上不可能
發生的「滿頁但游標未推進」（`no_progress`，防禦用）都视为無法繼續往前分頁，
標記 `partial` 並終止本輪，避免無界重試同一頁。
"""
from __future__ import annotations

import dataclasses
from typing import NamedTuple

from spark.publicapi.explore_store import FillsSyncState

PAGE_LIMIT = 2000          # HL userFillsByTime 單頁上限
RETENTION_LIMIT = 10_000   # HL 只保留最近這麼多筆可查
WINDOW_DAYS = 30
OVERLAP_MS = 1
_DAY_MS = 86_400_000
_DEFAULT_INCREMENTAL_AFTER_MS = 4 * 3600 * 1000


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
    - `state.completeness in ("complete", "partial")`：上一輪已經跑到底。
      若已過增量寬限期（`now - window_end_ms >= incremental_after_ms`）→ 開新的
      增量輪：`window_end_ms` 推到 `now`、`window_start_ms` 同步前推到
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
        )
        return PagePlan(start_ms=window_start_ms, end_ms=now_ms, state=new_state)

    if state.completeness == "backfilling":
        return PagePlan(start_ms=state.cursor_ms, end_ms=state.window_end_ms, state=state)

    if state.completeness in ("complete", "partial"):
        if now_ms - state.window_end_ms >= incremental_after_ms:
            new_window_start = now_ms - window_days * _DAY_MS
            new_cursor = max(new_window_start, (state.synced_through_ms or new_window_start)
                              - overlap_ms)
            new_state = dataclasses.replace(
                state, window_start_ms=new_window_start, window_end_ms=now_ms,
                cursor_ms=new_cursor, pages_done=0, fills_in_window=0,
            )
            return PagePlan(start_ms=new_cursor, end_ms=now_ms, state=new_state)
        through = state.synced_through_ms
        return PagePlan(start_ms=through, end_ms=through, state=state)

    raise ValueError(f"explore_fills_sync: unexpected completeness {state.completeness!r}")


def _validate_page(page: list[dict], start_ms: int, end_ms: int) -> str | None:
    """回傳 `None` 代表合法；否則回傳簡短原因字串。"""
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
               retention_limit: int = RETENTION_LIMIT, now_ms: int) -> PageResult:
    """套用一頁 HL 回應，算出新狀態與是否結束本輪。呼叫端負責把 `plan.start_ms`／
    `plan.end_ms` 當成這次 `userFillsByTime` 的 `startTime`／`endTime`。"""
    state = plan.state
    start_ms, end_ms = plan.start_ms, plan.end_ms

    invalid_reason = _validate_page(page, start_ms, end_ms)
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
        if fills_in_window >= retention_limit - page_limit:
            completeness, reason = "partial", "retention_limit"
        else:
            completeness, reason = "complete", None
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

    new_state = dataclasses.replace(
        state, cursor_ms=new_cursor, observed_from_ms=observed_from,
        observed_to_ms=observed_to, fills_in_window=fills_in_window,
        pages_done=state.pages_done + 1, last_error=None, updated_at=now_ms / 1000,
    )
    return PageResult(state=new_state, accepted=list(page), done=False, note=None)
