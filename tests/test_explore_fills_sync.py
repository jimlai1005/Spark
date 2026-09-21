"""tests/test_explore_fills_sync.py
Task 2.2 驗收：`src/spark/publicapi/explore_fills_sync.py` 單頁同步演算法。
純函式，零網路；最後一個測試額外整合 `ExploreStore` 驗證 insert 冪等。
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
    PagePlan,
    apply_page,
    plan_page,
)
from spark.publicapi.explore_store import (
    REASON_COUNT_BELOW_RETENTION_THRESHOLD,
    ExploreStore,
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


def test_retention_constants_named_and_derived(tmp_path):
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
    這個參數」的事實本身，不宣稱上游會用什麼值當預設（7.6 複審 S1 修法：
    舊字面值 `default(false)` 是對上游行為未經驗證的推測）。"""
    assert PARAMS_FP == "aggregateByTime=<omitted>"


def test_page_limit_same_object_as_hl_module_constant():
    """Task 3.5 D：`PAGE_LIMIT` 與 `hl.py` 用的是同一個常數來源
    （`spark.exchange.base.USER_FILLS_PAGE_LIMIT`），不是兩處各自硬編 2000。"""
    from spark.exchange.base import USER_FILLS_PAGE_LIMIT
    assert PAGE_LIMIT is USER_FILLS_PAGE_LIMIT


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
    assert plan.state.params_fp == PARAMS_FP
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
    result = apply_page(plan, page, page_limit=3, retention_threshold=100, now_ms=NOW)
    assert result.done is False
    assert result.state.cursor_ms == 9  # 最後一筆 time，無 +1
    assert result.state.pages_done == 1
    assert result.accepted == page
    assert result.note is None


def test_apply_page_overlap_first_record_equals_cursor_accepted():
    state = _backfilling_state(window_end_ms=1000, cursor_ms=9)
    plan = PagePlan(start_ms=9, end_ms=1000, state=state)
    page = [_fill(9, 3), _fill(9, 4), _fill(15, 5)]  # 重疊那一毫秒重複出現
    result = apply_page(plan, page, page_limit=3, retention_threshold=100, now_ms=NOW)
    assert result.done is False
    assert result.state.cursor_ms == 15
    assert len(result.accepted) == 3  # 去重交給 store PK，本層照單全收


def test_apply_page_same_ms_overflow_full_page():
    state = _backfilling_state(window_end_ms=1000, cursor_ms=5)
    plan = PagePlan(start_ms=5, end_ms=1000, state=state)
    page = [_fill(5, 1), _fill(5, 2), _fill(5, 3)]
    result = apply_page(plan, page, page_limit=3, retention_threshold=100, now_ms=NOW)
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
    result = apply_page(plan, page, page_limit=3, retention_threshold=100, now_ms=NOW)
    assert result.done is True
    assert result.state.completeness == "complete"
    # Task 7.5 點 2：complete 也要有 reason——這是門檻推論，不是留存邊界的直接證據。
    assert result.state.reason == REASON_COUNT_BELOW_RETENTION_THRESHOLD
    assert result.note == REASON_COUNT_BELOW_RETENTION_THRESHOLD
    assert result.state.synced_through_ms == 1000
    assert result.state.observed_from_ms == 60
    assert result.state.observed_to_ms == 60
    assert result.state.fills_in_window == 2


def test_apply_page_short_page_over_retention_threshold_is_partial():
    # retention_threshold=7（Task 7.5：threshold 本身已是保守調整後的值，不再由
    # apply_page 內部另外減 page_limit）
    state = _backfilling_state(window_end_ms=1000, cursor_ms=50, fills_in_window=6)
    plan = PagePlan(start_ms=50, end_ms=1000, state=state)
    page = [_fill(60, 9)]  # 短頁（1 < 3），累計 fills_in_window=7 >= 7
    result = apply_page(plan, page, page_limit=3, retention_threshold=7, now_ms=NOW)
    assert result.done is True
    assert result.state.completeness == "partial"
    assert result.state.reason == "retention_limit"
    assert result.note == "retention_limit"


def test_apply_page_empty_page_completes_observed_unchanged():
    state = _backfilling_state(window_end_ms=1000, cursor_ms=50,
                                observed_from_ms=10, observed_to_ms=20)
    plan = PagePlan(start_ms=50, end_ms=1000, state=state)
    result = apply_page(plan, [], page_limit=3, retention_threshold=100, now_ms=NOW)
    assert result.done is True
    assert result.state.completeness == "complete"
    assert result.state.reason == REASON_COUNT_BELOW_RETENTION_THRESHOLD
    assert result.state.observed_from_ms == 10
    assert result.state.observed_to_ms == 20
    assert result.state.synced_through_ms == 1000


def test_apply_page_out_of_order_is_invalid_cursor_unchanged():
    state = _backfilling_state(window_end_ms=1000, cursor_ms=50)
    plan = PagePlan(start_ms=50, end_ms=1000, state=state)
    page = [_fill(60, 1), _fill(55, 2)]  # 降冪
    result = apply_page(plan, page, page_limit=3, retention_threshold=100, now_ms=NOW)
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
    result = apply_page(plan, page, page_limit=3, retention_threshold=100, now_ms=NOW)
    assert result.done is True
    assert result.state.cursor_ms == 50
    assert "invalid_page:" in result.state.last_error


def test_apply_page_time_out_of_window_is_invalid():
    state = _backfilling_state(window_end_ms=1000, cursor_ms=50)
    plan = PagePlan(start_ms=50, end_ms=1000, state=state)
    page = [_fill(2000, 1)]  # 超出 [50,1000]
    result = apply_page(plan, page, page_limit=3, retention_threshold=100, now_ms=NOW)
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
    assert plan.state.fills_in_window == 0  # 歸零（2026-09-20 主線程裁決：門檻只看本輪區間）


def test_plan_page_increment_resets_fills_in_window_so_stale_count_does_not_flip_complete():
    # 第一輪已 complete，fills_in_window=7900（本身未達門檻）；增量輪若不歸零，
    # 光是舊計數就會讓門檻在沒有新資料時逐輪逼近，甚至疊加新頁後立刻跨過 8000。
    # `reason` 顯式設為既有判準（Task 7.6 A2 起 reason 跨輪存活，不再由短頁收尾
    # 無條件覆寫——這裡的初始狀態需要一個真實的既有 reason 才符合「complete
    # 必有 reason」的不變量，見 explore_store.py 檔頭）。
    state = _backfilling_state(
        window_start_ms=0, window_end_ms=1000, cursor_ms=1000,
        synced_through_ms=1000, completeness="complete", fills_in_window=7900,
        reason=REASON_COUNT_BELOW_RETENTION_THRESHOLD,
    )
    now = 1000 + 5 * 3600 * 1000
    plan = plan_page(state, address=ADDR, now_ms=now, incremental_after_ms=4 * 3600 * 1000)
    assert plan.state.fills_in_window == 0

    page = [_fill(now - 300 + i, i) for i in range(200)]  # 短頁（200 < PAGE_LIMIT），時間落在區間內
    result = apply_page(plan, page, page_limit=PAGE_LIMIT,
                         retention_threshold=RETENTION_SAFETY_THRESHOLD, now_ms=now)
    assert result.done is True
    assert result.state.completeness == "complete"  # 7900 的舊帳不該讓 0+200 觸頂
    assert result.state.reason == REASON_COUNT_BELOW_RETENTION_THRESHOLD


def test_end_to_end_three_full_pages_then_short_page_completes():
    state = None
    plan = plan_page(state, address=ADDR, now_ms=1000)
    # 覆寫成小區間方便手動走三頁
    small_state = dataclasses.replace(plan.state, window_end_ms=30, cursor_ms=0)
    plan = PagePlan(start_ms=0, end_ms=30, state=small_state)

    page1 = [_fill(0, 1), _fill(1, 2), _fill(2, 3)]
    r1 = apply_page(plan, page1, page_limit=3, retention_threshold=100, now_ms=1000)
    assert r1.done is False
    assert r1.state.pages_done == 1

    plan2 = PagePlan(start_ms=r1.state.cursor_ms, end_ms=30, state=r1.state)
    page2 = [_fill(2, 4), _fill(3, 5), _fill(4, 6)]
    r2 = apply_page(plan2, page2, page_limit=3, retention_threshold=100, now_ms=1000)
    assert r2.done is False
    assert r2.state.pages_done == 2

    plan3 = PagePlan(start_ms=r2.state.cursor_ms, end_ms=30, state=r2.state)
    page3 = [_fill(4, 7), _fill(5, 8), _fill(6, 9)]
    r3 = apply_page(plan3, page3, page_limit=3, retention_threshold=100, now_ms=1000)
    assert r3.done is False
    assert r3.state.pages_done == 3

    plan4 = PagePlan(start_ms=r3.state.cursor_ms, end_ms=30, state=r3.state)
    page4 = [_fill(6, 10)]  # 短頁，結束
    r4 = apply_page(plan4, page4, page_limit=3, retention_threshold=100, now_ms=1000)
    assert r4.done is True
    assert r4.state.completeness == "complete"
    assert r4.state.pages_done == 3  # 終止頁不計入 pages_done
    assert r4.state.synced_through_ms == 30
    assert r4.state.observed_from_ms == 0
    assert r4.state.observed_to_ms == 6


def test_apply_page_hits_page_cap_on_20th_consecutive_full_page():
    """Task 3.7 D（S1 修法）：單輪續頁加硬上限（預設 20 頁＝40,000 筆 >
    留存上限 10,000，正常資料到不了）——連續 20 個滿頁後，第 20 頁改判
    `partial`／`reason="page_cap"`／`synced_through=cursor`／`done=True`，
    不再無界續頁。"""
    small_state = dataclasses.replace(
        _backfilling_state(window_end_ms=10_000), cursor_ms=0)
    plan = PagePlan(start_ms=0, end_ms=10_000, state=small_state)
    cursor = 0
    result = None
    for i in range(20):
        page = [_fill(cursor + 1, 2 * i + 1), _fill(cursor + 2, 2 * i + 2)]
        result = apply_page(plan, page, page_limit=2, retention_threshold=10_000, now_ms=1000)
        if result.done:
            break
        cursor = result.state.cursor_ms
        plan = PagePlan(start_ms=cursor, end_ms=10_000, state=result.state)

    assert result.done is True
    assert result.state.completeness == "partial"
    assert result.state.reason == "page_cap"
    assert result.state.pages_done == 20
    assert result.state.synced_through_ms == result.state.cursor_ms


# --- Task 7.6 A：增量輪語義修正（round-in-progress、reason 跨輪存活） ---

def test_plan_page_round_in_progress_continues_regardless_of_grace_period():
    """A1：`cursor_ms > synced_through_ms` 代表增量輪已開始但尚未跑到底
    （例如上一次滿頁後 cursor 推進、`synced_through` 尚未更新）——下一次
    `plan_page` 即使遠低於增量寬限期也要續抓本輪剩餘部分，不能誤判 noop。"""
    state = _backfilling_state(
        window_start_ms=0, window_end_ms=1000, cursor_ms=500,
        synced_through_ms=100, completeness="complete",
        reason=REASON_COUNT_BELOW_RETENTION_THRESHOLD,
    )
    now = 110  # 遠低於任何合理的增量寬限期
    plan = plan_page(state, address=ADDR, now_ms=now, incremental_after_ms=4 * 3600 * 1000)
    assert not plan.is_noop
    assert plan.start_ms == 500
    assert plan.end_ms == 1000
    assert plan.state is state  # 不改 state


def test_plan_page_synced_through_none_does_not_trigger_round_in_progress():
    """`synced_through_ms is None` 不得誤觸發 round-in-progress 分支——落回既有
    的增量寬限期判斷（此例還沒到寬限期 → noop）。"""
    state = _backfilling_state(
        window_start_ms=0, window_end_ms=1000, cursor_ms=500,
        synced_through_ms=None, completeness="partial", reason="retention_limit",
    )
    now = 100
    plan = plan_page(state, address=ADDR, now_ms=now, incremental_after_ms=4 * 3600 * 1000)
    assert plan.is_noop


def test_end_to_end_incremental_round_full_page_then_short_page_stays_complete():
    """A1+A2 端到端（正式機重現場景）：complete 地址過寬限期開新增量輪，第一頁
    剛好滿頁（`done=False`，`synced_through` 未推進）；1 分鐘後下一次 `plan_page`
    不能被誤判 noop（A1）；短頁收尾後 `completeness`／`reason` 維持原值，不因
    這段小缺口而重新宣稱整個窗口的完整性（A2）。"""
    state = _backfilling_state(
        window_start_ms=0, window_end_ms=1000, cursor_ms=1000,
        synced_through_ms=1000, completeness="complete",
        reason=REASON_COUNT_BELOW_RETENTION_THRESHOLD, fills_in_window=5,
    )
    now1 = 1000 + 5 * 3600 * 1000  # 過寬限期，開新增量輪
    plan1 = plan_page(state, address=ADDR, now_ms=now1, incremental_after_ms=4 * 3600 * 1000)
    assert not plan1.is_noop
    assert plan1.state.completeness == "complete"  # 開新輪時原樣保留
    assert plan1.state.synced_through_ms == 1000   # 尚未推進

    page1 = [_fill(plan1.start_ms + i, i) for i in range(PAGE_LIMIT)]  # 滿頁
    r1 = apply_page(plan1, page1, now_ms=now1)
    assert r1.done is False
    assert r1.state.completeness == "complete"   # 續頁中，未改判
    assert r1.state.synced_through_ms == 1000    # 未推進——A1 判斷靠這個

    now2 = now1 + 60_000  # 1 分鐘後，遠低於下一次增量寬限期
    plan2 = plan_page(r1.state, address=ADDR, now_ms=now2, incremental_after_ms=4 * 3600 * 1000)
    assert not plan2.is_noop  # A1：本輪未完成，不能被誤判 noop
    assert plan2.start_ms == r1.state.cursor_ms
    assert plan2.end_ms == r1.state.window_end_ms

    page2 = [_fill(plan2.start_ms + 1, 99_999)]  # 短頁，結束本輪
    r2 = apply_page(plan2, page2, now_ms=now2)
    assert r2.done is True
    assert r2.state.completeness == "complete"
    assert r2.state.reason == REASON_COUNT_BELOW_RETENTION_THRESHOLD  # A2：不重寫
    assert r2.state.synced_through_ms == r1.state.window_end_ms


def test_incremental_round_short_page_preserves_partial_reason():
    """A2：`partial`／`retention_limit` 地址增量輪短頁收尾，不因這段小缺口本身
    未超標就升級回 `complete`——與 `plan_page` docstring「partial 仍是
    partial」一致。"""
    state = _backfilling_state(
        window_start_ms=0, window_end_ms=1000, cursor_ms=1000,
        synced_through_ms=1000, completeness="partial", reason="retention_limit",
        fills_in_window=8500,
    )
    now = 1000 + 5 * 3600 * 1000
    plan = plan_page(state, address=ADDR, now_ms=now, incremental_after_ms=4 * 3600 * 1000)
    page = [_fill(plan.start_ms + 1, 1)]  # 短頁
    result = apply_page(plan, page, now_ms=now)
    assert result.done is True
    assert result.state.completeness == "partial"
    assert result.state.reason == "retention_limit"


def test_incremental_round_short_page_preserves_retention_boundary_verified_reason():
    """A2：`retention_boundary_verified`（探測實測證據）跨增量輪存活——本地已
    持有的區段不受 HL 留存邊界影響，增量輪收尾不需要重新量測。"""
    state = _backfilling_state(
        window_start_ms=0, window_end_ms=1000, cursor_ms=1000,
        synced_through_ms=1000, completeness="complete",
        reason="retention_boundary_verified",
    )
    now = 1000 + 5 * 3600 * 1000
    plan = plan_page(state, address=ADDR, now_ms=now, incremental_after_ms=4 * 3600 * 1000)
    page = [_fill(plan.start_ms + 1, 1)]
    result = apply_page(plan, page, now_ms=now)
    assert result.done is True
    assert result.state.completeness == "complete"
    assert result.state.reason == "retention_boundary_verified"


def test_incremental_round_gap_itself_exceeds_threshold_downgrades_to_partial():
    """A2：增量輪的合法降級路徑不受影響——本輪缺口本身觀測筆數就超過門檻，
    仍然要降級為 `partial`／`retention_limit`（新的、更強的證據，不是「原樣
    保留」的情境）。"""
    state = _backfilling_state(
        window_start_ms=0, window_end_ms=1000, cursor_ms=1000,
        synced_through_ms=1000, completeness="complete",
        reason=REASON_COUNT_BELOW_RETENTION_THRESHOLD,
    )
    now = 1000 + 5 * 3600 * 1000
    plan = plan_page(state, address=ADDR, now_ms=now, incremental_after_ms=4 * 3600 * 1000)
    page = [_fill(plan.start_ms + i, i) for i in range(6000)]  # 短頁但缺口本身就超標
    result = apply_page(plan, page, page_limit=8000, retention_threshold=6000, now_ms=now)
    assert result.done is True
    assert result.state.completeness == "partial"
    assert result.state.reason == "retention_limit"


def test_integration_with_store_replaying_page_is_idempotent(tmp_path):
    store = ExploreStore(tmp_path / "x.db")
    plan = plan_page(None, address=ADDR, now_ms=1000)
    small_state = dataclasses.replace(plan.state, window_end_ms=30, cursor_ms=0)
    p1 = PagePlan(start_ms=0, end_ms=30, state=small_state)
    page1 = [_fill(0, 1), _fill(1, 2)]
    r1 = apply_page(p1, page1, page_limit=2, retention_threshold=100, now_ms=1000)
    added1 = store.insert_fills_page(ADDR, r1.accepted, r1.state)
    assert added1 == 2

    p2 = PagePlan(start_ms=r1.state.cursor_ms, end_ms=30, state=r1.state)
    page2 = [_fill(1, 3)]  # 短頁，結束
    r2 = apply_page(p2, page2, page_limit=2, retention_threshold=100, now_ms=1000)
    added2 = store.insert_fills_page(ADDR, r2.accepted, r2.state)
    assert added2 == 1

    before = store.get_sync(ADDR)
    assert before.completeness == "complete"

    # 重播第二頁 → 新增 0，checkpoint 不變
    added2_replay = store.insert_fills_page(ADDR, r2.accepted, r2.state)
    assert added2_replay == 0
    after = store.get_sync(ADDR)
    assert after == before
