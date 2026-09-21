"""tests/test_explore_fills_sync.py
Task 2.2／7.9b：`src/spark/publicapi/explore_fills_sync.py` 遍歷軌／增量軌雙
游標同步演算法。純函式，零網路；最後一個測試額外整合 `ExploreStore` 驗證
insert 冪等。
"""
from __future__ import annotations

import dataclasses

from spark.publicapi.explore_fills_sync import (
    HL_FILLS_RETENTION_LIMIT,
    OVERLAP_MS,
    PAGE_LIMIT,
    PARAMS_FP,
    RETENTION_SAFETY_MARGIN,
    RETENTION_SAFETY_THRESHOLD,
    WINDOW_DAYS,
    IncrementalPlan,
    ScanPlan,
    apply_incremental_page,
    apply_scan_page,
    build_fills_coverage,
    external_coverage_state,
    fresh_scan_window,
    plan_incremental,
    plan_scan,
)
from spark.publicapi.explore_store import (
    REASON_COUNT_BELOW_RETENTION_THRESHOLD,
    ExploreStore,
    FillsScan,
    FillsSyncState,
)

ADDR = "0xabc"
NOW = 1_700_000_000_000  # 任意固定 ms 時間戳


def _fill(time_ms: int, tid: int, coin: str = "BTC") -> dict:
    return {"coin": coin, "tid": tid, "time": time_ms, "px": "1", "sz": "1"}


def test_module_constants_match_spec():
    assert PAGE_LIMIT == 2000
    assert WINDOW_DAYS == 30
    assert OVERLAP_MS == 1


def test_retention_constants_named_and_derived():
    """Task 7.5 點 1（命名修正）：官方留存上限、保守緩衝（一頁）、與兩者相減得到
    的實際判準門檻，三個常數各自命名、彼此可追溯（工程原則 1）——舊名
    `RETENTION_LIMIT` 已移除（見模組 import：不再存在該名稱可 import）。"""
    assert HL_FILLS_RETENTION_LIMIT == 10_000
    assert RETENTION_SAFETY_MARGIN == PAGE_LIMIT
    assert RETENTION_SAFETY_THRESHOLD == HL_FILLS_RETENTION_LIMIT - RETENTION_SAFETY_MARGIN
    assert RETENTION_SAFETY_THRESHOLD == 8_000


def test_params_fp_constant_matches_actual_hl_request_body():
    """Task 7.5 點 4／7.6 點 8：`hl.get_fills_page` 的實際請求體只送
    `type/user/startTime/endTime`，不送 `aggregateByTime`——字串只描述「沒送
    這個參數」的事實本身，不宣稱上游會用什麼值當預設。"""
    assert PARAMS_FP == "aggregateByTime=<omitted>"


def test_page_limit_same_object_as_hl_module_constant():
    """Task 3.5 D：`PAGE_LIMIT` 與 `hl.py` 用的是同一個常數來源
    （`spark.exchange.base.USER_FILLS_PAGE_LIMIT`），不是兩處各自硬編 2000。"""
    from spark.exchange.base import USER_FILLS_PAGE_LIMIT
    assert PAGE_LIMIT is USER_FILLS_PAGE_LIMIT


def test_fresh_scan_window_is_now_minus_window_days():
    start, end = fresh_scan_window(NOW)
    assert end == NOW
    assert start == NOW - WINDOW_DAYS * 86_400_000


def test_fresh_scan_window_accepts_custom_window_days():
    start, end = fresh_scan_window(NOW, window_days=7)
    assert end == NOW
    assert start == NOW - 7 * 86_400_000


def test_default_fills_coverage_has_evidence_key_defaulting_none():
    """Task 7.9b B6：`hl_explore.DEFAULT_FILLS_COVERAGE` 必須含 `evidence`
    （全部 None）——`_row_from_dict` 逐鍵合併時才不會讓舊快照缺這個鍵。"""
    from spark.publicapi.hl_explore import DEFAULT_FILLS_COVERAGE
    assert DEFAULT_FILLS_COVERAGE["evidence"] is None


# --- 遍歷軌：plan_scan / apply_scan_page ---

def _scan(**overrides) -> FillsScan:
    base = dict(
        scan_id="s1", address=ADDR, kind="initial", window_start_ms=0, window_end_ms=1000,
        cursor_ms=0, pages_done=0, fills_in_window=0, observed_from_ms=None,
        observed_to_ms=None, status="running", result=None, reason=None, started_at=0.0,
        finished_at=None, last_error=None, params_fp=PARAMS_FP,
    )
    base.update(overrides)
    return FillsScan(**base)


def test_plan_scan_uses_cursor_to_window_end():
    scan = _scan(cursor_ms=42, window_end_ms=100)
    plan = plan_scan(scan)
    assert plan.start_ms == 42
    assert plan.end_ms == 100
    assert plan.scan is scan


def test_apply_scan_page_full_page_advances_cursor_no_plus_one():
    scan = _scan(window_end_ms=1000, cursor_ms=0)
    plan = ScanPlan(start_ms=0, end_ms=1000, scan=scan)
    page = [_fill(0, 1), _fill(5, 2), _fill(9, 3)]
    result = apply_scan_page(plan, page, page_limit=3, retention_threshold=100, now_ms=NOW)
    assert result.done is False
    assert result.scan.cursor_ms == 9  # 最後一筆 time，無 +1
    assert result.scan.pages_done == 1
    assert result.accepted == page
    assert result.note is None


def test_apply_scan_page_overlap_first_record_equals_cursor_accepted():
    scan = _scan(window_end_ms=1000, cursor_ms=9)
    plan = ScanPlan(start_ms=9, end_ms=1000, scan=scan)
    page = [_fill(9, 3), _fill(9, 4), _fill(15, 5)]  # 重疊那一毫秒重複出現
    result = apply_scan_page(plan, page, page_limit=3, retention_threshold=100, now_ms=NOW)
    assert result.done is False
    assert result.scan.cursor_ms == 15
    assert len(result.accepted) == 3  # 去重交給 store PK，本層照單全收


def test_apply_scan_page_same_ms_overflow_full_page():
    scan = _scan(window_end_ms=1000, cursor_ms=5)
    plan = ScanPlan(start_ms=5, end_ms=1000, scan=scan)
    page = [_fill(5, 1), _fill(5, 2), _fill(5, 3)]
    result = apply_scan_page(plan, page, page_limit=3, retention_threshold=100, now_ms=NOW)
    assert result.done is True
    assert result.scan.result == "partial"
    assert result.scan.reason == "same_ms_overflow"
    assert result.scan.cursor_ms == 5  # 不跳過，維持原地
    assert result.note == "same_ms_overflow"


def test_apply_scan_page_short_page_completes():
    scan = _scan(window_end_ms=1000, cursor_ms=50, fills_in_window=1)
    plan = ScanPlan(start_ms=50, end_ms=1000, scan=scan)
    page = [_fill(60, 9)]
    result = apply_scan_page(plan, page, page_limit=3, retention_threshold=100, now_ms=NOW)
    assert result.done is True
    assert result.scan.result == "complete"
    assert result.scan.reason == REASON_COUNT_BELOW_RETENTION_THRESHOLD
    assert result.note == REASON_COUNT_BELOW_RETENTION_THRESHOLD
    assert result.scan.cursor_ms == 1000
    assert result.scan.observed_from_ms == 60
    assert result.scan.observed_to_ms == 60
    assert result.scan.fills_in_window == 2


def test_apply_scan_page_short_page_over_retention_threshold_is_partial():
    scan = _scan(window_end_ms=1000, cursor_ms=50, fills_in_window=6)
    plan = ScanPlan(start_ms=50, end_ms=1000, scan=scan)
    page = [_fill(60, 9)]  # 短頁（1 < 3），累計 fills_in_window=7 >= 7
    result = apply_scan_page(plan, page, page_limit=3, retention_threshold=7, now_ms=NOW)
    assert result.done is True
    assert result.scan.result == "partial"
    assert result.scan.reason == "retention_limit"


def test_apply_scan_page_empty_page_completes_observed_unchanged():
    scan = _scan(window_end_ms=1000, cursor_ms=50, observed_from_ms=10, observed_to_ms=20)
    plan = ScanPlan(start_ms=50, end_ms=1000, scan=scan)
    result = apply_scan_page(plan, [], page_limit=3, retention_threshold=100, now_ms=NOW)
    assert result.done is True
    assert result.scan.result == "complete"
    assert result.scan.observed_from_ms == 10
    assert result.scan.observed_to_ms == 20


def test_apply_scan_page_out_of_order_is_invalid_cursor_unchanged():
    scan = _scan(window_end_ms=1000, cursor_ms=50)
    plan = ScanPlan(start_ms=50, end_ms=1000, scan=scan)
    page = [_fill(60, 1), _fill(55, 2)]  # 降冪
    result = apply_scan_page(plan, page, page_limit=3, retention_threshold=100, now_ms=NOW)
    assert result.done is True
    assert result.accepted == []
    assert result.scan.cursor_ms == 50
    assert result.scan.last_error is not None
    assert result.scan.last_error.startswith("invalid_page:")


def test_apply_scan_page_hits_page_cap_on_20th_consecutive_full_page():
    """Task 3.7 D（S1 修法）：單輪續頁加硬上限（預設 20 頁＝40,000 筆 >
    留存上限 10,000，正常資料到不了）。"""
    scan = _scan(window_end_ms=10_000, cursor_ms=0)
    plan = ScanPlan(start_ms=0, end_ms=10_000, scan=scan)
    cursor = 0
    result = None
    for i in range(20):
        page = [_fill(cursor + 1, 2 * i + 1), _fill(cursor + 2, 2 * i + 2)]
        result = apply_scan_page(plan, page, page_limit=2, retention_threshold=10_000, now_ms=1000)
        if result.done:
            break
        cursor = result.scan.cursor_ms
        plan = ScanPlan(start_ms=cursor, end_ms=10_000, scan=result.scan)

    assert result.done is True
    assert result.scan.result == "partial"
    assert result.scan.reason == "page_cap"
    assert result.scan.pages_done == 20
    assert result.scan.cursor_ms == result.scan.cursor_ms


def test_end_to_end_scan_three_full_pages_then_short_page_completes():
    scan = _scan(window_end_ms=30, cursor_ms=0)
    plan = ScanPlan(start_ms=0, end_ms=30, scan=scan)

    page1 = [_fill(0, 1), _fill(1, 2), _fill(2, 3)]
    r1 = apply_scan_page(plan, page1, page_limit=3, retention_threshold=100, now_ms=1000)
    assert r1.done is False
    assert r1.scan.pages_done == 1

    plan2 = ScanPlan(start_ms=r1.scan.cursor_ms, end_ms=30, scan=r1.scan)
    page2 = [_fill(2, 4), _fill(3, 5), _fill(4, 6)]
    r2 = apply_scan_page(plan2, page2, page_limit=3, retention_threshold=100, now_ms=1000)
    assert r2.done is False

    plan3 = ScanPlan(start_ms=r2.scan.cursor_ms, end_ms=30, scan=r2.scan)
    page3 = [_fill(4, 7), _fill(5, 8), _fill(6, 9)]
    r3 = apply_scan_page(plan3, page3, page_limit=3, retention_threshold=100, now_ms=1000)
    assert r3.done is False

    plan4 = ScanPlan(start_ms=r3.scan.cursor_ms, end_ms=30, scan=r3.scan)
    page4 = [_fill(6, 10)]  # 短頁，結束
    r4 = apply_scan_page(plan4, page4, page_limit=3, retention_threshold=100, now_ms=1000)
    assert r4.done is True
    assert r4.scan.result == "complete"
    assert r4.scan.pages_done == 3  # 終止頁不計入 pages_done
    assert r4.scan.cursor_ms == 30
    assert r4.scan.observed_from_ms == 0
    assert r4.scan.observed_to_ms == 6


# --- 增量軌：plan_incremental / apply_incremental_page ---

def _sync(**overrides) -> FillsSyncState:
    base = dict(
        address=ADDR, window_start_ms=0, window_end_ms=1000, cursor_ms=1000,
        synced_through_ms=1000, observed_from_ms=None, observed_to_ms=None,
        completeness="complete", reason=REASON_COUNT_BELOW_RETENTION_THRESHOLD, pages_done=0,
        fills_in_window=0, updated_at=0.0, last_error=None, params_fp=PARAMS_FP,
        inc_from_ms=0, scan_id="s1", evidence_unknown=False, coverage_gap=False,
    )
    base.update(overrides)
    return FillsSyncState(**base)


def test_plan_incremental_within_grace_period_is_noop():
    state = _sync(window_end_ms=1000, cursor_ms=1000, synced_through_ms=1000)
    now = 1000 + 3600 * 1000  # 1 小時後，小於 4 小時寬限期
    plan = plan_incremental(state, now_ms=now, period_s=4 * 3600)
    assert plan.is_noop
    assert plan.start_ms == 1000
    assert plan.end_ms == 1000
    assert plan.next_due_ms == 1000 + 4 * 3600 * 1000


def test_plan_incremental_after_grace_period_opens_new_round():
    state = _sync(window_end_ms=1000, cursor_ms=1000, synced_through_ms=1000)
    now = 1000 + 5 * 3600 * 1000
    plan = plan_incremental(state, now_ms=now, period_s=4 * 3600, overlap_ms=OVERLAP_MS)
    assert not plan.is_noop
    assert plan.end_ms == now
    assert plan.start_ms == max(state.inc_from_ms, 1000 - OVERLAP_MS)
    assert plan.state.window_end_ms == now
    assert plan.state.cursor_ms == plan.start_ms
    # 增量軌完全不動 completeness／reason（遍歷軌專屬）。
    assert plan.state.completeness == "complete"
    assert plan.state.reason == REASON_COUNT_BELOW_RETENTION_THRESHOLD
    assert plan.state.pages_done == 0


def test_plan_incremental_round_in_progress_continues_regardless_of_grace_period():
    state = _sync(cursor_ms=500, synced_through_ms=100, window_end_ms=1000)
    now = 110  # 遠低於任何合理的增量寬限期
    plan = plan_incremental(state, now_ms=now, period_s=4 * 3600)
    assert not plan.is_noop
    assert plan.start_ms == 500
    assert plan.end_ms == 1000
    assert plan.state is state  # 不改 state


def test_apply_incremental_page_short_page_advances_synced_through_only():
    state = _sync(window_end_ms=1000, cursor_ms=1000, synced_through_ms=None,
                  completeness="backfilling", reason=None)
    plan = IncrementalPlan(start_ms=1000, end_ms=2000, state=dataclasses.replace(
        state, window_end_ms=2000))
    page = [_fill(1500, 1)]
    result = apply_incremental_page(plan, page, now_ms=2000)
    assert result.done is True
    assert result.state.synced_through_ms == 2000
    assert result.state.cursor_ms == 2000
    # completeness／reason 完全不受影響——增量軌不判定它們。
    assert result.state.completeness == "backfilling"
    assert result.state.reason is None


def test_apply_incremental_page_full_page_continues():
    state = _sync(window_end_ms=5000, cursor_ms=1000)
    plan = IncrementalPlan(start_ms=1000, end_ms=5000, state=state)
    page = [_fill(1000 + i, i) for i in range(3)]
    result = apply_incremental_page(plan, page, page_limit=3, now_ms=2000)
    assert result.done is False
    assert result.state.cursor_ms == 1002
    assert result.state.pages_done == 1
    assert result.state.synced_through_ms == state.synced_through_ms  # 未推進


def test_apply_incremental_page_invalid_page_marks_error():
    state = _sync(window_end_ms=1000, cursor_ms=50)
    plan = IncrementalPlan(start_ms=50, end_ms=1000, state=state)
    page = [_fill(2000, 1)]  # 超出 [50,1000]
    result = apply_incremental_page(plan, page, now_ms=NOW)
    assert result.done is True
    assert result.accepted == []
    assert "invalid_page:" in result.state.last_error


# --- 對外契約：external_coverage_state / build_fills_coverage ---

def test_external_coverage_state_complete_clean():
    state = _sync(completeness="complete", coverage_gap=False, evidence_unknown=False)
    assert external_coverage_state(state) == ("complete", REASON_COUNT_BELOW_RETENTION_THRESHOLD)


def test_external_coverage_state_downgrades_on_gap():
    state = _sync(completeness="complete", coverage_gap=True, evidence_unknown=False)
    assert external_coverage_state(state) == ("partial", "coverage_gap")


def test_external_coverage_state_downgrades_on_evidence_unknown():
    state = _sync(completeness="complete", coverage_gap=False, evidence_unknown=True)
    assert external_coverage_state(state) == ("partial", "evidence_unknown")


def test_external_coverage_state_gap_takes_precedence_over_unknown():
    state = _sync(completeness="complete", coverage_gap=True, evidence_unknown=True)
    assert external_coverage_state(state) == ("partial", "coverage_gap")


def test_external_coverage_state_partial_passthrough():
    state = _sync(completeness="partial", reason="retention_limit", coverage_gap=True)
    # gap／unknown 只在內部值是 complete 時才會造成降級。
    assert external_coverage_state(state) == ("partial", "retention_limit")


def test_build_fills_coverage_none_sync():
    cov = build_fills_coverage(None, None)
    assert cov["state"] == "backfilling"
    assert cov["evidence"] is None
    assert cov["window_start"] is None and cov["window_end"] is None


def test_build_fills_coverage_window_is_incremental_track_range(tmp_path):
    store = ExploreStore(tmp_path / "x.db")
    store.bootstrap_address_fills(ADDR, 0.0, window_start_ms=0, window_end_ms=1000,
                                  params_fp=PARAMS_FP)
    active = store.get_active_scan(ADDR)
    finished = dataclasses.replace(active, cursor_ms=1000, result="complete",
                                   reason=REASON_COUNT_BELOW_RETENTION_THRESHOLD,
                                   finished_at=5.0)
    ok = store.complete_scan(ADDR, [], finished)
    assert ok
    sync = store.get_sync(ADDR)
    cov = build_fills_coverage(sync, store)
    assert cov["state"] == "complete"
    assert cov["window_start"] == sync.inc_from_ms
    assert cov["window_end"] == sync.synced_through_ms
    assert cov["evidence"]["scan_id"] == finished.scan_id
    assert cov["evidence"]["kind"] == "initial"
    assert cov["evidence"]["window_start"] == 0
    assert cov["evidence"]["window_end"] == 1000
    assert cov["evidence"]["gap"] is False
    assert cov["evidence"]["unknown"] is False


def test_integration_with_store_replaying_incremental_page_is_idempotent(tmp_path):
    store = ExploreStore(tmp_path / "x.db")
    store.bootstrap_address_fills(ADDR, 0.0, window_start_ms=0, window_end_ms=0,
                                  params_fp=PARAMS_FP)
    st = store.get_sync(ADDR)
    plan = IncrementalPlan(start_ms=st.cursor_ms, end_ms=1000,
                           state=dataclasses.replace(st, window_end_ms=1000))
    page1 = [_fill(0, 1), _fill(1, 2)]
    r1 = apply_incremental_page(plan, page1, page_limit=2, now_ms=1000)
    added1 = store.insert_fills_page(ADDR, r1.accepted, r1.state)
    assert added1 == 2

    st2 = store.get_sync(ADDR)
    before = st2
    # 重播同一頁 → 新增 0，checkpoint 不變。
    added_replay = store.insert_fills_page(ADDR, r1.accepted, r1.state)
    assert added_replay == 0
    after = store.get_sync(ADDR)
    assert after == before
