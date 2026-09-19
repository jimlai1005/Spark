"""tests/test_hl_budget.py — `WeightLimiter` 滑動視窗權重帳本（P1 Task 1.1）。

全離線；除了新增的並發測試（要求真實 `time.monotonic` 才能有意義地驗證跨執行緒的
互斥），其餘測試一律用注入的 fake clock（`Clock`），不真睡。
"""
import threading

import pytest

from spark.publicapi.hl_budget import (WeightLimiter, BudgetExhausted, ScopePaused,
                                       weight_for, ENDPOINT_WEIGHTS)


class Clock:
    def __init__(self): self.t = 1000.0
    def now(self): return self.t
    def sleep(self, s): self.t += s


def _lim(**kw):
    c = Clock()
    return WeightLimiter(global_cap=900, scope_caps={"explore": 300},
                         now_fn=c.now, sleep_fn=c.sleep, rng=lambda: 0.0, **kw), c


def test_weight_table_matches_hl_docs():
    assert weight_for("clearinghouseState") == 2
    assert weight_for("portfolio") == 20
    assert weight_for("userFillsByTime") == 120
    assert weight_for("nonFundingLedgerUpdates") == 20
    assert weight_for("somethingUndocumented") == 20
    # ENDPOINT_WEIGHTS 是 weight_for 的資料來源，直接對照一筆，避免匯入後全無引用
    # （ruff F401；plan Step 1 匯入了 ENDPOINT_WEIGHTS 但未在測試內使用，此為唯一偏離）。
    assert ENDPOINT_WEIGHTS["clearinghouseState"] == 2


def test_sliding_window_never_exceeds_global_cap():
    lim, c = _lim()
    for _ in range(45):
        assert lim.try_reserve(20, "interactive")
    assert not lim.try_reserve(20, "interactive")       # 900 用完
    c.t += 59.9
    assert not lim.try_reserve(20, "interactive")       # 還在視窗內
    c.t += 0.2
    assert lim.try_reserve(20, "interactive")           # 最早那筆滑出


def test_any_60s_window_bounded_by_cap():
    """任意 60 秒切片總和 ≤ 900（spec §5.1：不是 token bucket 的突發語意）。"""
    lim, c = _lim()
    ledger = []
    for step in range(600):
        c.t += 0.5
        if lim.try_reserve(20, "interactive"):
            ledger.append((c.t, 20))
    for t0, _ in ledger:
        assert sum(w for t, w in ledger if t0 < t <= t0 + 60) <= 900


def test_scope_cap_applies_and_global_still_applies():
    lim, c = _lim()
    for _ in range(15):
        assert lim.try_reserve(20, "explore")
    assert not lim.try_reserve(20, "explore")            # explore 300 用完
    assert lim.try_reserve(20, "interactive")            # 其他 scope 仍可用
    for _ in range(29):
        lim.try_reserve(20, "interactive")
    assert not lim.try_reserve(20, "interactive")        # 全域 900


def test_reserve_waits_then_raises():
    lim, c = _lim()
    for _ in range(45):
        lim.try_reserve(20, "interactive")
    with pytest.raises(BudgetExhausted):
        lim.reserve(20, "interactive", wait_s=2.0)
    assert c.t - 1000.0 == pytest.approx(2.0, abs=0.3)


def test_429_pauses_explore_scope_with_escalation_and_retry_after():
    lim, c = _lim()
    lim.note_429("explore")
    with pytest.raises(ScopePaused):
        lim.try_reserve(2, "explore")
    assert lim.snapshot()["paused_until"]["explore"] == pytest.approx(1060.0)
    c.t = 1100.0
    lim.note_429("explore", retry_after_s=90)           # 第二次：max(60,90)*2 = 180
    assert lim.snapshot()["paused_until"]["explore"] == pytest.approx(1280.0)
    c.t = 1300.0
    lim.note_ok("explore")
    assert lim.try_reserve(2, "explore")


def test_interactive_429_also_pauses_explore():
    lim, c = _lim()
    lim.note_429("interactive")
    with pytest.raises(ScopePaused):
        lim.try_reserve(2, "explore")
    assert lim.try_reserve(2, "interactive")            # interactive 自己不暫停（spec §5.2：不停必要查詢）


def test_snapshot_shape():
    lim, c = _lim()
    lim.try_reserve(20, "explore")
    lim.try_reserve(2, "interactive")
    s = lim.snapshot()
    assert s["window_s"] == 60 and s["global_cap"] == 900
    assert s["used"] == {"explore": 20, "interactive": 2}
    assert s["counters"]["reservations"] == 2 and s["counters"]["rate_limited"] == 0


def test_concurrent_reservations_never_exceed_cap():
    """8 條 thread 用真實 time.monotonic 搶額度；任何 60 秒切片總和不得超過全域上限
    （派工 prompt 額外要求：plan §3 自審提到要補的並發驗證）。"""
    lim = WeightLimiter(global_cap=900, scope_caps={"explore": 300})
    ledger: list[tuple[float, int]] = []
    ledger_lock = threading.Lock()

    def worker():
        for _ in range(200):
            if lim.try_reserve(20, "interactive"):
                with ledger_lock:
                    ledger.append((lim._now(), 20))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert ledger, "至少要有一些成功預留才能驗證上限"
    for t0, _ in ledger:
        assert sum(w for t, w in ledger if t0 < t <= t0 + 60) <= 900
    assert lim.snapshot()["counters"]["reserved_weight"] <= 900
