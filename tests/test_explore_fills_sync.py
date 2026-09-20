"""tests/test_explore_fills_sync.py
Task 2.2 驗收：`src/spark/publicapi/explore_fills_sync.py` 單頁同步演算法。
純函式，零網路；最後一個測試額外整合 `ExploreStore` 驗證 insert 冪等。
"""
from __future__ import annotations

import dataclasses

from spark.publicapi.explore_fills_sync import (
    OVERLAP_MS,
    PAGE_LIMIT,
    RETENTION_LIMIT,
    WINDOW_DAYS,
    PagePlan,
    apply_page,
    plan_page,
)
from spark.publicapi.explore_store import ExploreStore, FillsSyncState

ADDR = "0xabc"
NOW = 1_700_000_000_000  # 任意固定 ms 時間戳


def _fill(time_ms: int, tid: int, coin: str = "BTC") -> dict:
    return {"coin": coin, "tid": tid, "time": time_ms, "px": "1", "sz": "1"}


def test_module_constants_match_spec():
    assert PAGE_LIMIT == 2000
    assert RETENTION_LIMIT == 10_000
    assert WINDOW_DAYS == 30
    assert OVERLAP_MS == 1


def test_plan_page_new_state_none():
    plan = plan_page(None, address=ADDR, now_ms=NOW)
    assert plan.end_ms == NOW
    assert plan.start_ms == NOW - WINDOW_DAYS * 86_400_000
    assert plan.state.cursor_ms == plan.start_ms
    assert plan.state.window_start_ms == plan.start_ms
    assert plan.state.window_end_ms == NOW
    assert plan.state.completeness == "backfilling"
    assert plan.state.pages_done == 0
    assert plan.state.fills_in_window == 0
    assert plan.state.synced_through_ms is None
    assert not plan.is_noop


def _backfilling_state(**overrides) -> FillsSyncState:
    base = dict(
        address=ADDR, window_start_ms=0, window_end_ms=100, cursor_ms=0,
        synced_through_ms=None, observed_from_ms=None, observed_to_ms=None,
        completeness="backfilling", reason=None, pages_done=0, fills_in_window=0,
        updated_at=0.0, last_error=None,
    )
    base.update(overrides)
    return FillsSyncState(**base)


def test_plan_page_continues_backfilling_from_cursor():
    state = _backfilling_state(cursor_ms=42, pages_done=1, fills_in_window=3)
    plan = plan_page(state, address=ADDR, now_ms=NOW)
    assert plan.start_ms == 42
    assert plan.end_ms == 100
    assert plan.state is state


def test_apply_page_full_page_advances_cursor_no_plus_one():
    state = _backfilling_state(window_end_ms=1000, cursor_ms=0)
    plan = PagePlan(start_ms=0, end_ms=1000, state=state)
    page = [_fill(0, 1), _fill(5, 2), _fill(9, 3)]
    result = apply_page(plan, page, page_limit=3, retention_limit=100, now_ms=NOW)
    assert result.done is False
    assert result.state.cursor_ms == 9  # 最後一筆 time，無 +1
    assert result.state.pages_done == 1
    assert result.accepted == page
    assert result.note is None


def test_apply_page_overlap_first_record_equals_cursor_accepted():
    state = _backfilling_state(window_end_ms=1000, cursor_ms=9)
    plan = PagePlan(start_ms=9, end_ms=1000, state=state)
    page = [_fill(9, 3), _fill(9, 4), _fill(15, 5)]  # 重疊那一毫秒重複出現
    result = apply_page(plan, page, page_limit=3, retention_limit=100, now_ms=NOW)
    assert result.done is False
    assert result.state.cursor_ms == 15
    assert len(result.accepted) == 3  # 去重交給 store PK，本層照單全收


def test_apply_page_same_ms_overflow_full_page():
    state = _backfilling_state(window_end_ms=1000, cursor_ms=5)
    plan = PagePlan(start_ms=5, end_ms=1000, state=state)
    page = [_fill(5, 1), _fill(5, 2), _fill(5, 3)]
    result = apply_page(plan, page, page_limit=3, retention_limit=100, now_ms=NOW)
    assert result.done is True
    assert result.state.completeness == "partial"
    assert result.state.reason == "same_ms_overflow"
    assert result.state.synced_through_ms == 5
    assert result.state.cursor_ms == 5  # 不跳過，維持原地
    assert result.note == "same_ms_overflow"


def test_apply_page_short_page_completes():
    state = _backfilling_state(window_end_ms=1000, cursor_ms=50, fills_in_window=1)
    plan = PagePlan(start_ms=50, end_ms=1000, state=state)
    page = [_fill(60, 9)]
    result = apply_page(plan, page, page_limit=3, retention_limit=100, now_ms=NOW)
    assert result.done is True
    assert result.state.completeness == "complete"
    assert result.state.reason is None
    assert result.state.synced_through_ms == 1000
    assert result.state.observed_from_ms == 60
    assert result.state.observed_to_ms == 60
    assert result.state.fills_in_window == 2


def test_apply_page_short_page_over_retention_threshold_is_partial():
    # retention_limit=10, page_limit=3 → threshold = 10-3 = 7
    state = _backfilling_state(window_end_ms=1000, cursor_ms=50, fills_in_window=6)
    plan = PagePlan(start_ms=50, end_ms=1000, state=state)
    page = [_fill(60, 9)]  # 短頁（1 < 3），累計 fills_in_window=7 >= 7
    result = apply_page(plan, page, page_limit=3, retention_limit=10, now_ms=NOW)
    assert result.done is True
    assert result.state.completeness == "partial"
    assert result.state.reason == "retention_limit"
    assert result.note == "retention_limit"


def test_apply_page_empty_page_completes_observed_unchanged():
    state = _backfilling_state(window_end_ms=1000, cursor_ms=50,
                                observed_from_ms=10, observed_to_ms=20)
    plan = PagePlan(start_ms=50, end_ms=1000, state=state)
    result = apply_page(plan, [], page_limit=3, retention_limit=100, now_ms=NOW)
    assert result.done is True
    assert result.state.completeness == "complete"
    assert result.state.observed_from_ms == 10
    assert result.state.observed_to_ms == 20
    assert result.state.synced_through_ms == 1000


def test_apply_page_out_of_order_is_invalid_cursor_unchanged():
    state = _backfilling_state(window_end_ms=1000, cursor_ms=50)
    plan = PagePlan(start_ms=50, end_ms=1000, state=state)
    page = [_fill(60, 1), _fill(55, 2)]  # 降冪
    result = apply_page(plan, page, page_limit=3, retention_limit=100, now_ms=NOW)
    assert result.done is True
    assert result.accepted == []
    assert result.state.cursor_ms == 50
    assert result.state.last_error is not None
    assert result.state.last_error.startswith("invalid_page:")
    assert result.note is not None


def test_apply_page_missing_tid_is_invalid():
    state = _backfilling_state(window_end_ms=1000, cursor_ms=50)
    plan = PagePlan(start_ms=50, end_ms=1000, state=state)
    page = [{"coin": "BTC", "time": 55}]
    result = apply_page(plan, page, page_limit=3, retention_limit=100, now_ms=NOW)
    assert result.done is True
    assert result.state.cursor_ms == 50
    assert "invalid_page:" in result.state.last_error


def test_apply_page_time_out_of_window_is_invalid():
    state = _backfilling_state(window_end_ms=1000, cursor_ms=50)
    plan = PagePlan(start_ms=50, end_ms=1000, state=state)
    page = [_fill(2000, 1)]  # 超出 [50,1000]
    result = apply_page(plan, page, page_limit=3, retention_limit=100, now_ms=NOW)
    assert result.done is True
    assert result.state.cursor_ms == 50
    assert "invalid_page:" in result.state.last_error


def test_plan_page_increment_within_grace_period_is_noop():
    state = _backfilling_state(
        window_start_ms=0, window_end_ms=1000, cursor_ms=1000,
        synced_through_ms=1000, completeness="complete", fills_in_window=5,
    )
    now = 1000 + 3600 * 1000  # 1 小時後，小於 4 小時寬限期
    plan = plan_page(state, address=ADDR, now_ms=now, incremental_after_ms=4 * 3600 * 1000)
    assert plan.is_noop
    assert plan.start_ms == 1000
    assert plan.end_ms == 1000


def test_plan_page_increment_after_grace_period_preserves_completeness_and_shifts_window():
    state = _backfilling_state(
        window_start_ms=0, window_end_ms=1000, cursor_ms=1000,
        synced_through_ms=1000, completeness="partial", reason="retention_limit",
        fills_in_window=42,
    )
    now = 1000 + 5 * 3600 * 1000  # 5 小時後，超過 4 小時寬限期
    plan = plan_page(state, address=ADDR, now_ms=now, incremental_after_ms=4 * 3600 * 1000,
                      window_days=WINDOW_DAYS, overlap_ms=OVERLAP_MS)
    assert not plan.is_noop
    assert plan.end_ms == now
    assert plan.start_ms == max(now - WINDOW_DAYS * 86_400_000, 1000 - OVERLAP_MS)
    assert plan.state.window_end_ms == now
    assert plan.state.window_start_ms == now - WINDOW_DAYS * 86_400_000
    assert plan.state.cursor_ms == plan.start_ms
    assert plan.state.completeness == "partial"  # 保留，不重置
    assert plan.state.pages_done == 0
    assert plan.state.fills_in_window == 42  # 未被歸零（規格只明列 pages_done 歸零）


def test_end_to_end_three_full_pages_then_short_page_completes():
    state = None
    plan = plan_page(state, address=ADDR, now_ms=1000)
    # 覆寫成小區間方便手動走三頁
    small_state = dataclasses.replace(plan.state, window_end_ms=30, cursor_ms=0)
    plan = PagePlan(start_ms=0, end_ms=30, state=small_state)

    page1 = [_fill(0, 1), _fill(1, 2), _fill(2, 3)]
    r1 = apply_page(plan, page1, page_limit=3, retention_limit=100, now_ms=1000)
    assert r1.done is False
    assert r1.state.pages_done == 1

    plan2 = PagePlan(start_ms=r1.state.cursor_ms, end_ms=30, state=r1.state)
    page2 = [_fill(2, 4), _fill(3, 5), _fill(4, 6)]
    r2 = apply_page(plan2, page2, page_limit=3, retention_limit=100, now_ms=1000)
    assert r2.done is False
    assert r2.state.pages_done == 2

    plan3 = PagePlan(start_ms=r2.state.cursor_ms, end_ms=30, state=r2.state)
    page3 = [_fill(4, 7), _fill(5, 8), _fill(6, 9)]
    r3 = apply_page(plan3, page3, page_limit=3, retention_limit=100, now_ms=1000)
    assert r3.done is False
    assert r3.state.pages_done == 3

    plan4 = PagePlan(start_ms=r3.state.cursor_ms, end_ms=30, state=r3.state)
    page4 = [_fill(6, 10)]  # 短頁，結束
    r4 = apply_page(plan4, page4, page_limit=3, retention_limit=100, now_ms=1000)
    assert r4.done is True
    assert r4.state.completeness == "complete"
    assert r4.state.pages_done == 3  # 終止頁不計入 pages_done
    assert r4.state.synced_through_ms == 30
    assert r4.state.observed_from_ms == 0
    assert r4.state.observed_to_ms == 6


def test_integration_with_store_replaying_page_is_idempotent(tmp_path):
    store = ExploreStore(tmp_path / "x.db")
    plan = plan_page(None, address=ADDR, now_ms=1000)
    small_state = dataclasses.replace(plan.state, window_end_ms=30, cursor_ms=0)
    p1 = PagePlan(start_ms=0, end_ms=30, state=small_state)
    page1 = [_fill(0, 1), _fill(1, 2)]
    r1 = apply_page(p1, page1, page_limit=2, retention_limit=100, now_ms=1000)
    added1 = store.insert_fills_page(ADDR, r1.accepted, r1.state)
    assert added1 == 2

    p2 = PagePlan(start_ms=r1.state.cursor_ms, end_ms=30, state=r1.state)
    page2 = [_fill(1, 3)]  # 短頁，結束
    r2 = apply_page(p2, page2, page_limit=2, retention_limit=100, now_ms=1000)
    added2 = store.insert_fills_page(ADDR, r2.accepted, r2.state)
    assert added2 == 1

    before = store.get_sync(ADDR)
    assert before.completeness == "complete"

    # 重播第二頁 → 新增 0，checkpoint 不變
    added2_replay = store.insert_fills_page(ADDR, r2.accepted, r2.state)
    assert added2_replay == 0
    after = store.get_sync(ADDR)
    assert after == before
