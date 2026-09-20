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
    # 第一次暫停（到 1060）已在 t=1100 過期，這是「再犯」→ 升級：n=2，
    # pause = min(max(retry_after=90, PAUSE_MIN_S*2**(2-1)=120), 900) = 120
    # （2026-09-20 reviewer 修正：Retry-After 不再乘上指數，見 hl_budget.note_429）
    lim.note_429("explore", retry_after_s=90)
    assert lim.snapshot()["paused_until"]["explore"] == pytest.approx(1220.0)
    c.t = 1300.0
    lim.note_ok("explore")
    assert lim.try_reserve(2, "explore")


def test_note_429_during_active_pause_does_not_escalate():
    """暫停仍在生效中收到 429（同一輪違規的延續）不升級 `n`，只延長
    paused_until；只有「已解除又再犯」才真正升級（reviewer 修正）。"""
    lim, c = _lim()
    lim.note_429("explore")                              # n=1，until=1060
    assert lim.snapshot()["paused_until"]["explore"] == pytest.approx(1060.0)
    c.t = 1010.0                                          # 仍在暫停中（<1060）
    lim.note_429("explore")                               # 不升級：until=max(1060,1010+60)=1070
    s = lim.snapshot()
    assert s["paused_until"]["explore"] == pytest.approx(1070.0)
    assert s["consecutive_429"]["explore"] == 1           # 沒有變成 2
    c.t = 1071.0                                          # 暫停解除後才第二次真違規
    lim.note_429("explore")
    s = lim.snapshot()
    assert s["consecutive_429"]["explore"] == 2
    assert s["paused_until"]["explore"] == pytest.approx(1071.0 + 120.0)


def test_settle_refunds_down_only():
    lim, c = _lim()
    token = lim.reserve(120, "interactive")
    lim.settle(token, 23)
    assert lim.snapshot()["used"]["interactive"] == 23
    assert lim.snapshot()["counters"]["refunded_weight"] == 97
    lim.settle(token, 200)                                # 只降不升，200 > 23 不生效
    assert lim.snapshot()["used"]["interactive"] == 23


def test_retry_after_is_floor_not_clamped():
    """2026-09-20 opus 複審 W1：Retry-After 是「至少等這麼久」的下限，不得被
    `PAUSE_MAX_S`（指數退避的上限）夾住——舊版 `min(max(retry_after, 指數),
    PAUSE_MAX_S)` 會讓一個很長的 Retry-After 被我們自己的退避上限截斷變短。"""
    lim, c = _lim()
    c.t = 2000.0
    lim.note_429("explore", retry_after_s=1800)
    assert lim.snapshot()["paused_until"]["explore"] == pytest.approx(3800.0)


def test_escalation_survives_interactive_success():
    """2026-09-20 opus 複審 W2（撤回 1.5-A4）：`interactive` 的成功不歸零
    `explore` 的連續 429 計數——升級階梯要靠 `explore` 自己的 429 累積。"""
    lim, c = _lim()
    lim.note_429("explore")                               # n=1，until=1060
    c.t = 1061.0                                           # 暫停已解除
    lim.note_ok("interactive")                             # 不歸零（撤回 1.5-A4）
    lim.note_429("explore")                                # 再犯 → 升級
    s = lim.snapshot()
    assert s["consecutive_429"]["explore"] == 2
    assert s["paused_until"]["explore"] == pytest.approx(1061.0 + 120.0)


def test_explore_success_resets_escalation():
    """`explore` 自己成功才歸零自己的連續 429 計數。"""
    lim, c = _lim()
    lim.note_429("explore")
    c.t = 1061.0
    lim.note_ok("explore")
    assert lim.snapshot()["consecutive_429"]["explore"] == 0
    c.t = 1062.0
    lim.note_429("explore")
    assert lim.snapshot()["consecutive_429"]["explore"] == 1   # 沒有殘留升級


def test_settle_after_prune_does_not_count_refund():
    """S1：token 已滑出 60 秒視窗（被 `_prune` 移出帳本）後 `settle` 是 no-op，
    連 `refunded_weight` 計數器都不該累計——那筆 weight 早已不計入 `_used()`，
    算進退款會虛報一筆從未真正佔額度的退款。"""
    lim, c = _lim()
    token = lim.try_reserve(120, "explore")
    c.t = 1061.0                                          # 超過 WINDOW_S(60)
    lim.snapshot()                                        # 觸發 _prune，token 被移出 deque
    lim.settle(token, 20)
    assert lim.snapshot()["counters"]["refunded_weight"] == 0


def test_snapshot_has_paused_remaining_s():
    """W3：`paused_until` 是 `now_fn` 時基（正式路徑 monotonic），ops/health 的
    wall-clock `checked_at` 不可比——`paused_remaining_s` 用同一次快照的
    `now_fn` 算出剩餘秒數，供 ops 直接讀。"""
    lim, c = _lim()
    lim.note_429("explore")                               # until=1060
    assert lim.snapshot()["paused_remaining_s"]["explore"] == pytest.approx(60.0)
    c.t = 1030.0
    assert lim.snapshot()["paused_remaining_s"]["explore"] == pytest.approx(30.0)
    c.t = 1200.0                                          # 已過期，不得為負
    assert lim.snapshot()["paused_remaining_s"]["explore"] == pytest.approx(0.0)


def test_scope_paused_message_has_no_digits():
    """reviewer W1：訊息不含任何數字，避免十進位的 `until` 偶然含 "502"/"503"/
    "504" 子字串被 `spark.resilience._TRANSIENT_MARKERS` 誤判成 transient。"""
    from spark.resilience import _is_transient_error
    exc = ScopePaused("explore", 250238.0)
    assert not any(ch.isdigit() for ch in str(exc))
    assert _is_transient_error(exc) is False


def test_is_rate_limited_ignores_json_column_429():
    import json
    from spark.publicapi.hl_budget import is_rate_limited
    try:
        json.loads(" " * 428)
    except json.JSONDecodeError as e:
        exc = e
    else:
        raise AssertionError("expected JSONDecodeError")
    assert is_rate_limited(exc) is False
    assert is_rate_limited(RuntimeError("429 Too Many Requests")) is True


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


# ---------- P6 Task 6.3（D15，2026-09-20：觀測補齊）----------

def test_wait_ms_stats_have_n_p50_p95_max():
    """`reserve` 等待 0.5s 與 2s 兩筆樣本 → wait_ms 用 nearest-rank 百分位數：
    n=2 時 p50=較小值（rank=ceil(0.5*2)=1）、p95=較大值（rank=ceil(0.95*2)=2，
    此例等於 max）。立即成功（wait_s=0）記 0，不進同一 scope 就不干擾。"""
    lim, c = _lim()
    for _ in range(45):
        lim.try_reserve(20, "interactive")           # 900 用完，之後的 reserve 得等

    class _AdvancingSleep:
        """`sleep_fn` 每次呼叫推進時鐘，並在指定次數後釋放一筆額度，讓
        `reserve` 的輪詢在預期的等待時間點成功——用來精準控制「等了多久」。"""
        def __init__(self, clock, release_after_s):
            self.clock, self.release_after_s, self.start = clock, release_after_s, clock.t

        def __call__(self, s):
            self.clock.t += s
            if self.clock.t - self.start >= self.release_after_s:
                lim._log.popleft()                    # 讓出一筆額度（模擬視窗滑出）

    lim._sleep = _AdvancingSleep(c, 0.5)
    lim.reserve(20, "interactive", wait_s=5.0)
    lim._sleep = _AdvancingSleep(c, 2.0)
    lim.reserve(20, "interactive", wait_s=5.0)

    stats = lim.snapshot()["wait_ms"]["interactive"]
    assert stats["n"] == 2
    assert stats["p50"] == pytest.approx(500, abs=50)
    assert stats["p95"] == pytest.approx(2000, abs=50)
    assert stats["max"] == pytest.approx(2000, abs=50)


def test_wait_ms_records_zero_for_immediate_success():
    lim, c = _lim()
    lim.reserve(20, "explore")
    stats = lim.snapshot()["wait_ms"]["explore"]
    assert stats == {"n": 1, "p50": 0, "p95": 0, "max": 0}


def test_wait_ms_absent_for_untouched_scope():
    lim, c = _lim()
    lim.reserve(20, "explore")
    assert "interactive" not in lim.snapshot()["wait_ms"]


def test_note_http_counts_by_class_and_scope():
    lim, c = _lim()
    lim.note_http("interactive", "2xx")
    lim.note_http("interactive", "2xx")
    lim.note_http("interactive", "timeout")
    lim.note_http("explore", "429")
    s = lim.snapshot()["http"]
    assert s["interactive"] == {"2xx": 2, "timeout": 1}
    assert s["explore"] == {"429": 1}


def test_percentile_nearest_rank():
    from spark.publicapi.hl_budget import percentile
    assert percentile([], 50) is None
    assert percentile([10.0], 50) == 10.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 95) == 4.0


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
