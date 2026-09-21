"""src/spark/publicapi/explore_fills_sync.py
遍歷軌／增量軌雙游標同步演算法（spec
`docs/superpowers/specs/2026-09-20-hl-leaderboard-refactor-spec.md` §8；plan
`docs/superpowers/plans/2026-09-20-explore-rate-limit-refactor.md` Task 2.2／7.9b B2）。

純函式，**零網路、零 sleep**、不碰 `ExploreStore` 以外的任何 IO——worker 每輪只抓一頁，
呼叫端（`explore_scheduler`）負責：規劃這輪抓哪一段 → 打 HL `userFillsByTime` → 套用
一頁算出新狀態與是否結束 → 呼叫端自行落地並視 `done` 決定要不要排下一頁。

Task 7.9b（2026-09-21 使用者第二輪裁決）起，舊版單一 `plan_page`／`apply_page`
（同時承載「全區間遍歷」與「增量」兩種輪）拆成兩軌互不覆蓋：

- **遍歷軌**（`plan_scan`／`apply_scan_page`，對映 `ExploreStore.FillsScan`）：
  一次全區間遍歷（`initial`／`partial_rescan`／`verify`），從頭量測留存門檻，
  完成後 `result`／`reason` 透過 `ExploreStore.complete_scan` 的 CAS 寫回
  `fills_sync`。三種 `kind` 的頁面套用邏輯完全相同（都是「量測一次窗口內的
  成交數，短頁收尾時依門檻判 complete／partial」），差別只在窗口與 CAS
  之後的排程動作，見 `explore_scheduler._run_scan`。
- **增量軌**（`plan_incremental`／`apply_incremental_page`，對映
  `ExploreStore.FillsSyncState`）：只延伸 `synced_through_ms`，**永不**判定
  `completeness`／`reason`——這兩欄完全由遍歷軌的 CAS 決定，增量軌讀寫時
  原樣帶著目前值（`dataclasses.replace` 不動它們），確保重掃期間增量仍能
  持續保存新成交、不被遍歷覆蓋、也不會覆蓋遍歷（B7 (i)）。

留存判準（HL `userFillsByTime` 官方只保留最近 `HL_FILLS_RETENTION_LIMIT`（10,000）
筆可查，spec §8）：
`RETENTION_SAFETY_MARGIN`＝一頁（`USER_FILLS_PAGE_LIMIT`），
`RETENTION_SAFETY_THRESHOLD = HL_FILLS_RETENTION_LIMIT - RETENTION_SAFETY_MARGIN`
（＝8,000）。若本輪遍歷全程觀測到的筆數 `fills_in_window < RETENTION_SAFETY_THRESHOLD`，
代表區間內成交必然全部落在「最近 `HL_FILLS_RETENTION_LIMIT` 筆」的可查範圍內 →
`complete`／`reason="count_below_retention_threshold"`（**這仍然只是門檻推論，
不是留存邊界本身的證據**；更強的證據見 `explore_scheduler._run_probe` 的留存邊界
探測，探測成功會把 reason 升級為 `"retention_boundary_verified"`，見
`explore_store.REASON_RETENTION_BOUNDARY_VERIFIED`）；一旦達到或超過這個門檻 →
`partial`（`reason="retention_limit"`）。

`cursor_ms` 推進採 inclusive 重疊（下一頁 `startTime` = 上一頁最後一筆的 `time`，
**游標不額外遞增**）——同一毫秒可能跨頁被拆散，去重交給 `fills` 表的 `(address, coin, tid)`
PRIMARY KEY，本模組不做去重判斷。同毫秒佔滿整頁（`same_ms_overflow`）與理論上不可能
發生的「滿頁但游標未推進」（`no_progress`，防禦用）都視為無法繼續往前分頁，
標記 `partial` 並終止本輪，避免無界重試同一頁。

單輪續頁另設硬上限 `max_pages_per_round`（預設 20）：20 頁＝40,000 筆已超過留存上限
`HL_FILLS_RETENTION_LIMIT`（10,000），正常資料到不了這個頁數，達到即代表卡在異常
續頁——終止本輪並標記 `partial`／`reason="page_cap"`，避免無界重試。

`partial` 的復原路徑：增量軌不做完整性判定，`partial` 若像 `complete` 一樣被增量軌
悄悄「延伸」也不會改變它的證據狀態——真正的復原只能靠遍歷軌重新做一次全區間遍歷
（`partial_rescan`，`explore_scheduler` 在遍歷完成 `result=="partial"` 時自動排程
下一次，見 `PARTIAL_RESCAN_AFTER_MS`）。
"""
from __future__ import annotations

import dataclasses
from typing import NamedTuple

from spark.exchange.base import USER_FILLS_PAGE_LIMIT
from spark.publicapi.explore_store import (REASON_COUNT_BELOW_RETENTION_THRESHOLD, ExploreStore,
                                           FillsScan, FillsSyncState)

PAGE_LIMIT = USER_FILLS_PAGE_LIMIT   # HL userFillsByTime 單頁上限（同 hl.py 常數來源，Task 3.5 D）

# Task 7.5（命名修正，工程原則 1：門檻是假設，不是事實，得先讓讀者看得出來）：
# `HL_FILLS_RETENTION_LIMIT` 是 HL 官方文件的留存上限；本模組的實際判準
# `RETENTION_SAFETY_THRESHOLD` 比官方數字保守一頁（`RETENTION_SAFETY_MARGIN`）。
HL_FILLS_RETENTION_LIMIT = 10_000   # HL 官方文件：只保留最近這麼多筆可查
RETENTION_SAFETY_MARGIN = USER_FILLS_PAGE_LIMIT   # 保守緩衝＝一頁
RETENTION_SAFETY_THRESHOLD = HL_FILLS_RETENTION_LIMIT - RETENTION_SAFETY_MARGIN   # 8_000
WINDOW_DAYS = 30
OVERLAP_MS = 1
_DAY_MS = 86_400_000

# Task 7.9a 補（2026-09-21 主線程裁決）：fills 增量週期的**唯一**字面值來源
# ——`ApiConfig.explore_fills_period_s` 的預設值與 `ExploreScheduler.__init__`
# 的 `fills_every_s` 預設值都 import 這個常數，不得各自重新寫一份 21600／
# 6*3600 的字面值。
DEFAULT_FILLS_PERIOD_S = 6 * 3600

# 這個值只在呼叫端沒有明講 `period_s` 時才會用到——生產路徑
# （`explore_scheduler._run_increment`）一律顯式傳入
# `self._fills_every_s`（由 `run_api.py` 從 `ApiConfig.explore_fills_period_s`
# 單一來源注入），這裡只是「沒有 config 時」的保底值。
_DEFAULT_INCREMENTAL_PERIOD_S = DEFAULT_FILLS_PERIOD_S

# Task 7.7 W2（partial 復原路徑）／Task 7.9b B3：`partial` 遍歷完成後，
# `explore_scheduler` 自動排程下一次 `partial_rescan`，間隔即此常數。
PARTIAL_RESCAN_AFTER_MS = 24 * 3600 * 1000

# Task 7.5 點 4／7.6 點 8（查詢參數留證）：`hl.get_fills_page` 實際請求體只送
# `type/user/startTime/endTime`——刻意不送 `aggregateByTime`。字串只描述「送了
# 什麼參數」這個事實本身，**不宣稱**上游會用什麼值當預設。
PARAMS_FP = "aggregateByTime=<omitted>"


def fresh_scan_window(now_ms: int, window_days: int = WINDOW_DAYS) -> tuple[int, int]:
    """新的全區間遍歷窗口 `[now - window_days, now]`——`initial`／
    `partial_rescan`／`verify` 三種 scan 建立時共用同一份算法（Task 7.9b B1／B3）。"""
    return now_ms - window_days * _DAY_MS, now_ms


class IncrementalPlan(NamedTuple):
    """`plan_incremental()` 的輸出：增量軌這一輪要向 HL 要哪一段 `[start_ms,
    end_ms]`。`next_due_ms` 只有 noop 計畫才會填，值＝下次應該重新呼叫
    `plan_incremental()` 的時刻（`window_end_ms + period_ms`）——排程端只能
    用它重排，不得另算一份週期常數（工程原則 1：比較的兩個量要同源，
    Task 7.8 的教訓）。"""
    start_ms: int
    end_ms: int
    state: FillsSyncState
    next_due_ms: int | None = None

    @property
    def is_noop(self) -> bool:
        return self.start_ms == self.end_ms


class IncrementalResult(NamedTuple):
    """`apply_incremental_page()` 的輸出——`state` 只更新增量軌自己的欄位
    （`cursor_ms`／`synced_through_ms`／`pages_done`／`last_error`／
    `updated_at`），**不動** `completeness`／`reason`／`scan_id`／
    `evidence_unknown`／`coverage_gap`（遍歷軌專屬，見模組檔頭）。"""
    state: FillsSyncState
    accepted: list[dict]
    done: bool
    note: str | None


def plan_incremental(state: FillsSyncState, *, now_ms: int, period_s: float = None,
                     overlap_ms: int = OVERLAP_MS) -> IncrementalPlan:
    """增量軌：只延伸 `synced_through_ms`，永不覆蓋遍歷軌的 `completeness`／
    `reason`。`state` 必為非 `None`（Task 7.9b B1：新地址建立時
    `ExploreStore.bootstrap_address_fills` 已同時建好 `fills_sync` 列，增量軌
    从此恆有列可讀）。

    「輪進行中」：`cursor_ms` 大於目前基準（`synced_through_ms`，首次尚未
    完成任何一輪時用 `inc_from_ms`）代表本輪尚未跑到底，續抓，不看週期。
    """
    period_s = DEFAULT_FILLS_PERIOD_S if period_s is None else period_s
    period_ms = int(period_s * 1000)
    baseline = state.synced_through_ms if state.synced_through_ms is not None \
        else state.inc_from_ms
    if baseline is not None and state.cursor_ms > baseline:
        return IncrementalPlan(start_ms=state.cursor_ms, end_ms=state.window_end_ms, state=state)
    if now_ms - state.window_end_ms >= period_ms:
        new_end = now_ms
        new_cursor = max(state.inc_from_ms, (baseline or state.inc_from_ms) - overlap_ms)
        new_state = dataclasses.replace(
            state, window_end_ms=new_end, cursor_ms=new_cursor, pages_done=0)
        return IncrementalPlan(start_ms=new_cursor, end_ms=new_end, state=new_state)
    through = baseline if baseline is not None else state.window_end_ms
    return IncrementalPlan(start_ms=through, end_ms=through, state=state,
                           next_due_ms=state.window_end_ms + period_ms)


def validate_page(page: list[dict], start_ms: int, end_ms: int) -> str | None:
    """回傳 `None` 代表合法；否則回傳簡短原因字串。公開名（Task 7.6 B5：
    `explore_scheduler._run_probe` 也要用它驗證探測回應是否真的落在探測窗內，
    不再是本模組私有的實作細節）。"""
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


def apply_incremental_page(plan: IncrementalPlan, page: list[dict], *, page_limit: int = PAGE_LIMIT,
                           max_pages_per_round: int = 20, now_ms: int) -> IncrementalResult:
    """套用一頁到增量軌。短頁／空頁 → `synced_through_ms` 推到 `end_ms`、本輪
    結束；滿頁 → 續抓（同遍歷軌的同毫秒溢位／無進展／頁數上限三個終止條件，
    純防禦，正常增量缺口極小很少觸發）。全程不判定 `completeness`／`reason`。"""
    state = plan.state
    start_ms, end_ms = plan.start_ms, plan.end_ms

    invalid_reason = validate_page(page, start_ms, end_ms)
    if invalid_reason is not None:
        new_state = dataclasses.replace(
            state, last_error=f"invalid_page:{invalid_reason}", updated_at=now_ms / 1000)
        return IncrementalResult(state=new_state, accepted=[], done=True, note=invalid_reason)

    times = [int(f["time"]) for f in page]
    if len(page) < page_limit:
        new_state = dataclasses.replace(
            state, cursor_ms=end_ms, synced_through_ms=end_ms, last_error=None,
            updated_at=now_ms / 1000)
        return IncrementalResult(state=new_state, accepted=list(page), done=True, note=None)

    if all(t == start_ms for t in times):
        new_state = dataclasses.replace(
            state, cursor_ms=start_ms, synced_through_ms=start_ms,
            last_error="same_ms_overflow", updated_at=now_ms / 1000)
        return IncrementalResult(state=new_state, accepted=list(page), done=True,
                                 note="same_ms_overflow")

    new_cursor = times[-1]
    if new_cursor == start_ms:
        new_state = dataclasses.replace(
            state, cursor_ms=start_ms, synced_through_ms=start_ms, last_error="no_progress",
            updated_at=now_ms / 1000)
        return IncrementalResult(state=new_state, accepted=list(page), done=True,
                                 note="no_progress")

    new_pages_done = state.pages_done + 1
    if new_pages_done >= max_pages_per_round:
        new_state = dataclasses.replace(
            state, cursor_ms=new_cursor, synced_through_ms=new_cursor,
            pages_done=new_pages_done, last_error="page_cap", updated_at=now_ms / 1000)
        return IncrementalResult(state=new_state, accepted=list(page), done=True,
                                 note="page_cap")

    new_state = dataclasses.replace(
        state, cursor_ms=new_cursor, pages_done=new_pages_done, last_error=None,
        updated_at=now_ms / 1000)
    return IncrementalResult(state=new_state, accepted=list(page), done=False, note=None)


class ScanPlan(NamedTuple):
    """`plan_scan()` 的輸出：遍歷軌這一輪要抓 `[start_ms, end_ms]`。"""
    start_ms: int
    end_ms: int
    scan: FillsScan


class ScanPageResult(NamedTuple):
    """`apply_scan_page()` 的輸出。`done=True` 時 `scan.status` 應由呼叫端
    （`explore_scheduler`）透過 `ExploreStore.complete_scan` 落地為
    `'done'`——本函式只回傳新的 `FillsScan`（`dataclasses.replace`，`status`
    欄位維持傳入值不變，呼叫端另外設）。"""
    scan: FillsScan
    accepted: list[dict]
    done: bool
    note: str | None


def plan_scan(scan: FillsScan) -> ScanPlan:
    """遍歷軌續抓：一律從 `scan.cursor_ms` 抓到 `scan.window_end_ms`——
    `initial`／`partial_rescan`／`verify` 三種 `kind` 的續抓邏輯完全相同。"""
    return ScanPlan(start_ms=scan.cursor_ms, end_ms=scan.window_end_ms, scan=scan)


def apply_scan_page(plan: ScanPlan, page: list[dict], *, page_limit: int = PAGE_LIMIT,
                    retention_threshold: int = RETENTION_SAFETY_THRESHOLD,
                    max_pages_per_round: int = 20, now_ms: int) -> ScanPageResult:
    """套用一頁到遍歷軌，算出新 `FillsScan` 與是否結束本輪。呼叫端負責把
    `plan.start_ms`／`plan.end_ms` 當成這次 `userFillsByTime` 的
    `startTime`／`endTime`。"""
    scan = plan.scan
    start_ms, end_ms = plan.start_ms, plan.end_ms

    invalid_reason = validate_page(page, start_ms, end_ms)
    if invalid_reason is not None:
        new_scan = dataclasses.replace(scan, last_error=f"invalid_page:{invalid_reason}")
        return ScanPageResult(scan=new_scan, accepted=[], done=True, note=invalid_reason)

    times = [int(f["time"]) for f in page]
    observed_from = scan.observed_from_ms
    observed_to = scan.observed_to_ms
    if times:
        observed_from = min(times) if observed_from is None else min(observed_from, min(times))
        observed_to = max(times) if observed_to is None else max(observed_to, max(times))
    fills_in_window = scan.fills_in_window + len(page)

    if len(page) < page_limit:
        if fills_in_window >= retention_threshold:
            result, reason = "partial", "retention_limit"
        else:
            result, reason = "complete", REASON_COUNT_BELOW_RETENTION_THRESHOLD
        new_scan = dataclasses.replace(
            scan, cursor_ms=end_ms, observed_from_ms=observed_from, observed_to_ms=observed_to,
            fills_in_window=fills_in_window, result=result, reason=reason, last_error=None)
        return ScanPageResult(scan=new_scan, accepted=list(page), done=True, note=reason)

    # 滿頁。
    if all(t == start_ms for t in times):
        new_scan = dataclasses.replace(
            scan, cursor_ms=start_ms, observed_from_ms=observed_from, observed_to_ms=observed_to,
            fills_in_window=fills_in_window, result="partial", reason="same_ms_overflow",
            last_error=None)
        return ScanPageResult(scan=new_scan, accepted=list(page), done=True,
                              note="same_ms_overflow")

    new_cursor = times[-1]
    if new_cursor == start_ms:
        new_scan = dataclasses.replace(
            scan, cursor_ms=start_ms, observed_from_ms=observed_from, observed_to_ms=observed_to,
            fills_in_window=fills_in_window, result="partial", reason="no_progress",
            last_error=None)
        return ScanPageResult(scan=new_scan, accepted=list(page), done=True, note="no_progress")

    new_pages_done = scan.pages_done + 1
    if new_pages_done >= max_pages_per_round:
        new_scan = dataclasses.replace(
            scan, cursor_ms=new_cursor, observed_from_ms=observed_from,
            observed_to_ms=observed_to, fills_in_window=fills_in_window,
            pages_done=new_pages_done, result="partial", reason="page_cap", last_error=None)
        return ScanPageResult(scan=new_scan, accepted=list(page), done=True, note="page_cap")

    new_scan = dataclasses.replace(
        scan, cursor_ms=new_cursor, observed_from_ms=observed_from, observed_to_ms=observed_to,
        fills_in_window=fills_in_window, pages_done=new_pages_done, last_error=None)
    return ScanPageResult(scan=new_scan, accepted=list(page), done=False, note=None)


def external_coverage_state(sync: FillsSyncState) -> tuple[str, str | None]:
    """Task 7.9b B1／B6：對外 `completeness`／`reason`——`complete` 需同時
    `result == complete AND coverage_gap == 0 AND evidence_unknown == 0`；
    否則依序降級為 `partial`／`reason` 為 `coverage_gap` 或
    `evidence_unknown`。`backfilling`／`partial` 內部值本來就等於對外值，
    原樣透傳（gap／unknown 只有在內部值是 `complete` 時才可能造成降級）。"""
    if sync.completeness != "complete":
        return sync.completeness, sync.reason
    if sync.coverage_gap:
        return "partial", "coverage_gap"
    if sync.evidence_unknown:
        return "partial", "evidence_unknown"
    return "complete", sync.reason


def build_fills_coverage(sync: FillsSyncState | None, store: "ExploreStore | None") -> dict:
    """對外 `fills_coverage` 字典的唯一組裝點（Task 7.9b B6）——探索清單
    （`explore_publisher.compose_rows`）與交易員詳情頁（`app.py
    ._local_trader_data`）共用（工程原則 1）。`sync is None`（尚未 enrich 過
    的候選／非本地路徑）→ 全部鍵為 `None`（`state="backfilling"`，與既有
    `DEFAULT_FILLS_COVERAGE` 語意一致）。

    `window_start`／`window_end`：**Task 7.9b 起語義變更**——改為增量軌覆蓋
    區間 `[inc_from, synced_through]`（不再是「目前這一輪」的查詢區間，那個
    語意現在放進 `evidence.window_start`／`evidence.window_end`，指向「建立
    目前 completeness 的那次遍歷」）。`evidence` 全部鍵可為 `None`：
    `sync.scan_id` 尚未指向任何完成的遍歷（例如全新地址仍在首次 backfilling）
    時，`store` 查無列或 `sync.scan_id is None` → `evidence=None`。"""
    if sync is None:
        return {
            "state": "backfilling", "observed_from": None, "observed_to": None,
            "reason": None, "synced_through": None, "last_success_at": None,
            "window_start": None, "window_end": None, "params_fp": None, "evidence": None,
        }
    state, reason = external_coverage_state(sync)
    evidence = None
    if sync.scan_id is not None and store is not None:
        scan = store.get_scan(sync.scan_id)
        if scan is not None:
            evidence = {
                "scan_id": scan.scan_id, "kind": scan.kind,
                "window_start": scan.window_start_ms, "window_end": scan.window_end_ms,
                "finished_at": scan.finished_at, "reason": scan.reason,
                "gap": bool(sync.coverage_gap), "unknown": bool(sync.evidence_unknown),
            }
    return {
        "state": state, "observed_from": sync.observed_from_ms,
        "observed_to": sync.observed_to_ms, "reason": reason,
        "synced_through": sync.synced_through_ms, "last_success_at": sync.updated_at,
        "window_start": sync.inc_from_ms, "window_end": sync.synced_through_ms,
        "params_fp": sync.params_fp, "evidence": evidence,
    }
