"""_reconcile_orders / sync_open_orders 狀態機測試（Task 8）。

全離線：FakeExecutor 實作 ExecutorPort（可注入 modify 回傳序列與 get_open_orders
回應**序列**）、RecordingNotifier、假 clock、sleep_fn 只記錄不睡。
重點覆蓋 hl-copytrader orders.py:253-271 的 settle 驗證分支（hl 自己沒測的缺口）。
"""
from decimal import Decimal

import pytest

from spark.copytrade.config import CopySettings
from spark.copytrade.executor import ExecutorPort
from spark.copytrade.notifier import RecordingNotifier
from spark.copytrade.orders import (
    CycleReport,
    OrderSpec,
    ReconcileState,
    SkippedOrder,
    _reconcile_orders,
    _set_entry_leverage,
    sync_open_orders,
)
from spark.exchange.base import OpenOrder, OrderResult, Position

SETTINGS = CopySettings()  # px_rel_tol=1e-4, size_tolerance=0.08, settle_seconds=2, ttl=120


# ── 測試替身 ─────────────────────────────────────────────────────────
class FakeExecutor:
    """實作 ExecutorPort 的離線假 executor。records 記錄所有動作供順序斷言。

    - modify_results：modify() 依序回傳的 bool 序列，耗盡後回 True。
    - open_orders_seq：get_open_orders() 依序回傳的清單序列，耗盡後回 []。
    - place_exc：place() 被呼叫時直接 raise 該例外（模擬 transient 失敗）。
    - update_leverage_ok：update_leverage() 的回傳值（模擬槓桿設定失敗）。
    """

    def __init__(self, modify_results=None, open_orders_seq=None,
                 place_ok=True, cancel_ok=True, place_exc=None, update_leverage_ok=True):
        self.records: list = []
        self._modify_results = list(modify_results or [])
        self._open_orders_seq = [list(x) for x in (open_orders_seq or [])]
        self._place_ok = place_ok
        self._cancel_ok = cancel_ok
        self._place_exc = place_exc
        self._update_leverage_ok = update_leverage_ok

    def place(self, spec) -> bool:
        if self._place_exc is not None:
            raise self._place_exc
        self.records.append(("place", spec))
        return self._place_ok

    def place_with_reason(self, spec) -> tuple[bool, str]:
        return self.place(spec), ""

    def modify(self, oid, spec) -> bool:
        self.records.append(("modify", oid, spec))
        return self._modify_results.pop(0) if self._modify_results else True

    def cancel(self, coin, oid) -> bool:
        self.records.append(("cancel", coin, oid))
        return self._cancel_ok

    def market_open(self, coin, is_buy, size) -> OrderResult:
        self.records.append(("market_open", coin, is_buy, size))
        return OrderResult(ok=True, filled_size=size, avg_px=Decimal("0"), raw={})

    def close_reduce_only(self, coin, is_buy, size) -> OrderResult:
        self.records.append(("close_reduce_only", coin, is_buy, size))
        return OrderResult(ok=True, filled_size=size, avg_px=Decimal("0"), raw={})

    def update_leverage(self, coin, leverage, is_cross) -> bool:
        self.records.append(("update_leverage", coin, leverage, is_cross))
        return self._update_leverage_ok

    def get_open_orders(self) -> list[OpenOrder]:
        self.records.append(("get_open_orders",))
        return self._open_orders_seq.pop(0) if self._open_orders_seq else []

    def get_size_decimals(self, coin) -> int:
        return 4


class FakeClock:
    def __init__(self, t: float = 1_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _spec(coin="ETH", is_buy=True, sz="1.0", limit_px="2000", reduce_only=False,
          is_trigger=False, tpsl=None, trigger_px=None, is_market=False, tif="Gtc") -> OrderSpec:
    return OrderSpec(
        coin=coin, is_buy=is_buy, sz=Decimal(sz), limit_px=Decimal(limit_px),
        reduce_only=reduce_only, is_trigger=is_trigger, tpsl=tpsl,
        trigger_px=Decimal(trigger_px) if trigger_px is not None else None,
        is_market=is_market, tif=tif,
    )


def _open_order(coin="ETH", is_buy=True, sz="1.0", limit_px="2000", reduce_only=False,
                is_trigger=False, tpsl=None, trigger_px=None, oid=1,
                is_market=False, tif="Gtc") -> OpenOrder:
    return OpenOrder(
        oid=oid, coin=coin, is_buy=is_buy, limit_px=Decimal(limit_px), sz=Decimal(sz),
        reduce_only=reduce_only, is_trigger=is_trigger,
        trigger_px=Decimal(trigger_px) if trigger_px is not None else None,
        tpsl=tpsl, is_market=is_market, tif=tif,
    )


def _position(coin="ETH") -> Position:
    return Position(
        coin=coin, szi=Decimal("1"), entry_px=Decimal("1900"), leverage=5,
        is_cross=True, unrealized_pnl=Decimal("0"), margin_used=Decimal("100"),
    )


def _run_reconcile(ex, desired, my_orders, *, settings=SETTINGS, notifier=None,
                   state=None, live=False, clock=None, sleeps=None):
    return _reconcile_orders(
        ex, desired, my_orders,
        settings=settings, notifier=notifier or RecordingNotifier(),
        state=state or ReconcileState(), live=live,
        clock=clock or FakeClock(),
        sleep_fn=(sleeps.append if sleeps is not None else lambda s: None),
    )


def test_fake_executor_satisfies_port():
    assert isinstance(FakeExecutor(), ExecutorPort)


# ── 1. happy path：動作序列，cancel 全先於 place ─────────────────────
def test_happy_path_action_sequence_all_cancels_before_places():
    matched_spec = _spec(coin="ETH", limit_px="2000")
    modify_target = _spec(coin="BTC", limit_px="50100", sz="0.5")
    to_place = _spec(coin="SOL", limit_px="150", sz="10")
    desired = [matched_spec, modify_target, to_place]
    my_orders = [
        _open_order(coin="ETH", limit_px="2000", oid=1),                    # matched
        _open_order(coin="BTC", limit_px="50000", sz="0.5", oid=2),         # modify（價移）
        _open_order(coin="DOGE", limit_px="0.1", sz="100", oid=3),          # to_cancel
    ]
    ex = FakeExecutor()
    res = _run_reconcile(ex, desired, my_orders)

    assert res.matched == 1
    assert res.modified == 1
    assert res.placed == 1
    assert res.cancelled == 1
    assert res.sync_failed is False
    assert res.skipped_small == ()

    ops = [r[0] for r in ex.records]
    assert ops == ["modify", "cancel", "place"]  # cancel 全部先於 place（紅線）
    assert ex.records[0] == ("modify", 2, modify_target)
    assert ex.records[1] == ("cancel", "DOGE", 3)
    assert ex.records[2] == ("place", to_place)


# ── 2. modify False → TTL 登記 + 同輪 cancel+place 補位 ──────────────
def test_modify_failure_registers_ttl_and_falls_back_same_cycle():
    desired = [_spec(coin="BTC", limit_px="50100", sz="0.5")]
    my_orders = [_open_order(coin="BTC", limit_px="50000", sz="0.5", oid=2)]
    ex = FakeExecutor(modify_results=[False])
    state = ReconcileState()
    clock = FakeClock(t=1_000.0)
    res = _run_reconcile(ex, desired, my_orders, state=state, clock=clock)

    assert res.modified == 0
    assert res.cancelled == 1
    assert res.placed == 1
    assert state.modify_fail_until["BTC"] == 1_000.0 + SETTINGS.modify_fail_ttl_s
    ops = [r[0] for r in ex.records]
    assert ops == ["modify", "cancel", "place"]
    assert ex.records[1] == ("cancel", "BTC", 2)
    assert ex.records[2] == ("place", desired[0])


# ── 3. TTL 未過 → 零 modify；假 clock 推進過期 → 恢復 modify ─────────
def test_ttl_active_skips_modify_and_expiry_restores_it():
    desired = [_spec(coin="BTC", limit_px="50100", sz="0.5")]
    my_orders = [_open_order(coin="BTC", limit_px="50000", sz="0.5", oid=2)]
    state = ReconcileState(modify_fail_until={"BTC": 1_050.0})
    clock = FakeClock(t=1_000.0)

    ex1 = FakeExecutor()
    res1 = _run_reconcile(ex1, desired, my_orders, state=state, clock=clock)
    assert res1.modified == 0
    assert [r[0] for r in ex1.records] == ["cancel", "place"]  # TTL 內：直接降級

    clock.t = 1_300.0  # 推進超過 1050 → TTL 過期
    ex2 = FakeExecutor()
    res2 = _run_reconcile(ex2, desired, my_orders, state=state, clock=clock)
    assert res2.modified == 1
    assert [r[0] for r in ex2.records] == ["modify"]
    assert "BTC" not in state.modify_fail_until  # 成功後清除登記（hl orders.py:222）


# ── 4. cancel-place policy → 零 modify，全部降級 ─────────────────────
def test_cancel_place_policy_degrades_all_modifies():
    settings = CopySettings(modify_policy="cancel-place")
    desired = [_spec(coin="BTC", limit_px="50100", sz="0.5")]
    my_orders = [_open_order(coin="BTC", limit_px="50000", sz="0.5", oid=2)]
    ex = FakeExecutor()
    res = _run_reconcile(ex, desired, my_orders, settings=settings)

    assert res.modified == 0
    assert res.cancelled == 1
    assert res.placed == 1
    assert [r[0] for r in ex.records] == ["cancel", "place"]


# ── 5. settle 分支（hl orders.py:253-271，hl 未測的缺口）─────────────
def test_settle_missing_one_replaces_and_second_fetch_clean():
    d1 = _spec(coin="ETH", limit_px="2000")
    d2 = _spec(coin="BTC", limit_px="50000", sz="0.5")
    after_first = [_open_order(coin="ETH", limit_px="2000", oid=11)]  # 缺 d2
    after_second = [
        _open_order(coin="ETH", limit_px="2000", oid=11),
        _open_order(coin="BTC", limit_px="50000", sz="0.5", oid=12),
    ]
    ex = FakeExecutor(open_orders_seq=[after_first, after_second])
    notifier = RecordingNotifier()
    sleeps: list = []
    res = _run_reconcile(ex, [d1, d2], [], live=True, notifier=notifier, sleeps=sleeps)

    assert res.sync_failed is False
    assert res.placed == 3  # 初掛 2 + 補缺 1
    assert [r[0] for r in ex.records].count("get_open_orders") == 2
    assert sleeps == [SETTINGS.settle_seconds, SETTINGS.settle_seconds]
    assert notifier.records == []  # 修正一次即收斂，不告警
    # 補缺的 place 發生在第一次重抓之後
    ops = [r[0] for r in ex.records]
    assert ops.index("get_open_orders") < len(ops) - 1 - ops[::-1].index("place")


def test_settle_still_missing_after_retry_sets_sync_failed_and_criticals():
    d1 = _spec(coin="ETH", limit_px="2000")
    d2 = _spec(coin="BTC", limit_px="50000", sz="0.5")
    ex = FakeExecutor(open_orders_seq=[[], []])  # 兩次重抓都空 → 缺 2
    notifier = RecordingNotifier()
    res = _run_reconcile(ex, [d1, d2], [], live=True, notifier=notifier)

    assert res.sync_failed is True
    crits = [r for r in notifier.records if r[0] == "critical"]
    assert len(crits) == 1
    level, category, text, _dedup = crits[0]
    assert category == "orders"
    assert "缺少 2" in text and "多餘 0" in text  # 含 missing/extra 摘要
    assert "ETH" in text and "BTC" in text


# ── 6. extra 多 1 → 撤 → 二驗乾淨 ───────────────────────────────────
def test_settle_extra_one_cancels_and_second_fetch_clean():
    d1 = _spec(coin="ETH", limit_px="2000")
    mine = _open_order(coin="ETH", limit_px="2000", oid=1)  # 與 d1 完全相符 → 零動作
    stray = _open_order(coin="DOGE", limit_px="0.1", sz="100", oid=99)
    after_first = [_open_order(coin="ETH", limit_px="2000", oid=1), stray]
    after_second = [_open_order(coin="ETH", limit_px="2000", oid=1)]
    ex = FakeExecutor(open_orders_seq=[after_first, after_second])
    notifier = RecordingNotifier()
    res = _run_reconcile(ex, [d1], [mine], live=True, notifier=notifier)

    assert res.sync_failed is False
    assert res.cancelled == 1
    assert ("cancel", "DOGE", 99) in ex.records
    assert notifier.records == []


# ── 7. live=False → 零 get_open_orders、零 sleep ─────────────────────
def test_dry_run_never_fetches_or_sleeps():
    d1 = _spec(coin="ETH", limit_px="2000")
    ex = FakeExecutor()
    sleeps: list = []
    res = _run_reconcile(ex, [d1], [], live=False, sleeps=sleeps)

    assert res.placed == 1
    assert res.sync_failed is False
    assert sleeps == []
    assert all(r[0] != "get_open_orders" for r in ex.records)


# ── 10. sleep_fn 收到 settings.settle_seconds ────────────────────────
def test_sleep_fn_receives_settle_seconds_from_settings():
    settings = CopySettings(settle_seconds=7)
    d1 = _spec(coin="ETH", limit_px="2000")
    after = [_open_order(coin="ETH", limit_px="2000", oid=1)]
    ex = FakeExecutor(open_orders_seq=[after])
    sleeps: list = []
    res = _run_reconcile(ex, [d1], [], settings=settings, live=True, sleeps=sleeps)

    assert res.sync_failed is False
    assert sleeps == [7]  # 一驗即乾淨 → 只 sleep 一次
    assert [r[0] for r in ex.records].count("get_open_orders") == 1


# ── _set_entry_leverage（hl orders.py:187-193）───────────────────────
def test_set_entry_leverage_skips_reduce_only():
    ex = FakeExecutor()
    _set_entry_leverage(ex, _spec(coin="ETH", reduce_only=True), {"ETH": (5, True)},
                        RecordingNotifier())
    assert ex.records == []


def test_set_entry_leverage_uses_mapping_values():
    ex = FakeExecutor()
    notifier = RecordingNotifier()
    _set_entry_leverage(ex, _spec(coin="ETH"), {"ETH": (10, False)}, notifier)
    assert ex.records == [("update_leverage", "ETH", 10, False)]
    assert notifier.records == []  # 成功不告警


def test_set_entry_leverage_unknown_coin_without_fallback_is_noop():
    """相容路徑：`leverage_fallback=None`（預設）時 map 查無維持既有靜默跳過語意。"""
    ex = FakeExecutor()
    _set_entry_leverage(ex, _spec(coin="SOL"), {"ETH": (10, True)}, RecordingNotifier(),
                        leverage_fallback=None)
    assert ex.records == []


def test_set_entry_leverage_unknown_coin_uses_fallback():
    """2026-07-25 首航盲區修法本體：map 查無 → 查 fallback（activeAssetData）→ 照設。"""
    ex = FakeExecutor()
    notifier = RecordingNotifier()
    calls: list[str] = []

    def fb(coin):
        calls.append(coin)
        return (25, True)

    _set_entry_leverage(ex, _spec(coin="ETH"), {}, notifier, leverage_fallback=fb)
    assert calls == ["ETH"]
    assert ex.records == [("update_leverage", "ETH", 25, True)]
    assert notifier.records == []  # 成功不告警


def test_set_entry_leverage_map_hit_never_calls_fallback():
    """持倉 map 優先：命中就不查 fallback（省查詢，值也同源於部位欄位）。"""
    ex = FakeExecutor()

    def fb(coin):
        raise AssertionError("map 命中時不得查 fallback")

    _set_entry_leverage(ex, _spec(coin="ETH"), {"ETH": (10, False)}, RecordingNotifier(),
                        leverage_fallback=fb)
    assert ex.records == [("update_leverage", "ETH", 10, False)]


def test_set_entry_leverage_fallback_error_warns_and_skips():
    """fallback 拋例外 → warn（dedup 含 coin）＋跳過該幣，不吞、不炸整輪。"""
    ex = FakeExecutor()
    notifier = RecordingNotifier()

    def fb(coin):
        raise ValueError(f"activeAssetData missing leverage for {coin}")

    _set_entry_leverage(ex, _spec(coin="ETH"), {}, notifier, leverage_fallback=fb)
    assert ex.records == []  # 該幣本輪不設槓桿
    warns = [r for r in notifier.records if r[0] == "warn"]
    assert len(warns) == 1
    assert warns[0][1] == "orders"
    assert "ETH" in warns[0][2] and "槓桿" in warns[0][2]
    assert "ETH" in warns[0][3]  # dedup key 含 coin


def test_set_entry_leverage_failure_warns_with_dedup_key():
    """update_leverage 回 False → warn 告警（不吞），流程繼續（best-effort）。"""
    ex = FakeExecutor(update_leverage_ok=False)
    notifier = RecordingNotifier()
    _set_entry_leverage(ex, _spec(coin="ETH"), {"ETH": (10, True)}, notifier)
    assert ex.records == [("update_leverage", "ETH", 10, True)]
    warns = [r for r in notifier.records if r[0] == "warn"]
    assert len(warns) == 1
    assert warns[0][1] == "orders"
    assert "ETH" in warns[0][2] and "槓桿" in warns[0][2]
    assert warns[0][3] == "lev_fail:ETH"


def test_sync_leverage_failure_warns_and_reconcile_proceeds():
    """槓桿設定失敗只告警，對帳照常執行（下單不被阻擋）。"""
    leader = [_open_order(coin="ETH", limit_px="2000", sz="1", oid=1)]
    ex = FakeExecutor(update_leverage_ok=False)
    notifier = RecordingNotifier()
    report = _run_sync(ex, leader, [], notifier=notifier,
                       leverage_by_coin={"ETH": (10, True)})

    assert ("warn", "orders",
            "槓桿設定失敗 ETH leverage=10x is_cross=True", "lev_fail:ETH") in notifier.records
    assert report.reconcile.placed == 1  # 對帳照常
    ops = [r[0] for r in ex.records]
    assert ops == ["update_leverage", "place"]


# ── transient 例外傳播（工程原則 2：不吞、上層 runner 計錯）──────────
def test_place_transient_exception_propagates_without_rollback():
    """place 拋 ConnectionError → 乾淨上拋（不被吞）；拋出前已執行的 cancel 不回滾；
    modify_fail_until 只含 modify 失敗的正確登記（無誤登記）。
    runner（Task 12）必須 try/except 本例外並計入 max_consecutive_errors。"""
    desired = [_spec(coin="BTC", limit_px="50100", sz="0.5")]
    my_orders = [_open_order(coin="BTC", limit_px="50000", sz="0.5", oid=2)]
    ex = FakeExecutor(modify_results=[False], place_exc=ConnectionError("reset"))
    state = ReconcileState()
    clock = FakeClock(t=1_000.0)

    with pytest.raises(ConnectionError):
        _run_reconcile(ex, desired, my_orders, state=state, clock=clock)

    # 已執行的 cancel 不回滾（冪等寫入，交由下一輪對帳自癒）
    assert ("cancel", "BTC", 2) in ex.records
    assert all(r[0] != "place" for r in ex.records)  # place 未成功記錄任何動作
    # TTL 登記只反映 modify 失敗，place 例外不產生誤登記
    assert state.modify_fail_until == {"BTC": 1_000.0 + SETTINGS.modify_fail_ttl_s}


# ── 二驗剩 extra（非 missing）→ sync_failed + critical ───────────────
def test_settle_extra_only_after_retry_sets_sync_failed_and_criticals():
    """兩驗皆多出雜單（缺 0、多 1）→ sync_failed=True + critical 含 extra 摘要。"""
    d1 = _spec(coin="ETH", limit_px="2000")
    mine = _open_order(coin="ETH", limit_px="2000", oid=1)  # 與 d1 相符 → 零動作
    stray1 = _open_order(coin="DOGE", limit_px="0.1", sz="100", oid=99)
    stray2 = _open_order(coin="DOGE", limit_px="0.1", sz="100", oid=100)  # 撤了又冒出來
    after_first = [_open_order(coin="ETH", limit_px="2000", oid=1), stray1]
    after_second = [_open_order(coin="ETH", limit_px="2000", oid=1), stray2]
    ex = FakeExecutor(open_orders_seq=[after_first, after_second])
    notifier = RecordingNotifier()
    res = _run_reconcile(ex, [d1], [mine], live=True, notifier=notifier)

    assert res.sync_failed is True
    assert ("cancel", "DOGE", 99) in ex.records  # 一驗後有撤 extra
    crits = [r for r in notifier.records if r[0] == "critical"]
    assert len(crits) == 1
    _level, category, text, _dedup = crits[0]
    assert category == "orders"
    assert "缺少 0" in text and "多餘 1" in text
    assert "oid=100" in text and "DOGE" in text  # extra 摘要可定位到單


# ── sync_open_orders ─────────────────────────────────────────────────
def _run_sync(ex, leader_orders, my_orders, *, my_positions=None, scale=Decimal("1"),
              settings=SETTINGS, notifier=None, state=None, live=False,
              protected=frozenset(), leverage_by_coin=None, leverage_fallback=None,
              safety_net=None, skip_safety_net=False, sleeps=None) -> CycleReport:
    return sync_open_orders(
        ex, leader_orders, my_orders, my_positions or {}, scale,
        settings=settings, notifier=notifier or RecordingNotifier(),
        state=state or ReconcileState(), live=live, protected=protected,
        leverage_by_coin=leverage_by_coin, leverage_fallback=leverage_fallback,
        safety_net=safety_net,
        skip_safety_net=skip_safety_net, clock=FakeClock(),
        sleep_fn=(sleeps.append if sleeps is not None else lambda s: None),
    )


# ── 8. skipped_small → warn 帶 dedup_key ─────────────────────────────
def test_skipped_small_warns_with_dedup_key_and_lands_in_report():
    leader = [_open_order(coin="BTC", limit_px="50000", sz="0.0001", oid=1)]  # 名目 5 < 10
    ex = FakeExecutor()
    notifier = RecordingNotifier()
    report = _run_sync(ex, leader, [], notifier=notifier)

    warns = [r for r in notifier.records if r[0] == "warn"]
    assert len(warns) == 1
    level, category, text, dedup = warns[0]
    assert category == "orders"
    assert dedup == "skipped_small:BTC"
    assert "BTC" in text
    assert report.reconcile.skipped_small == (
        SkippedOrder(coin="BTC", notional=Decimal("5.0000"), reason="small"),
    )
    assert all(r[0] != "place" for r in ex.records)  # 太小的單不掛


# ── 9. safety_net：None → skipped；callable → 回報進 CycleReport ─────
def test_safety_net_none_reports_skipped():
    report = _run_sync(FakeExecutor(), [], [])
    assert report.safety_net == {"skipped": True}
    assert report.scale == Decimal("1")
    assert report.tripped is False


def test_safety_net_callable_result_lands_in_cycle_report():
    report = _run_sync(FakeExecutor(), [], [], safety_net=lambda: {"actions": 3})
    assert report.safety_net == {"actions": 3}


def test_skip_safety_net_true_never_invokes_callable():
    def _boom() -> dict:
        raise AssertionError("safety_net 不應被呼叫")

    report = _run_sync(FakeExecutor(), [], [], safety_net=_boom, skip_safety_net=True)
    assert report.safety_net == {"skipped": True}


# ── sync_open_orders：槓桿前置設定（照 hl 呼叫點語意，per-coin 去重）──
def test_sync_sets_leverage_once_per_coin_skipping_reduce_only():
    leader = [
        _open_order(coin="ETH", limit_px="2000", sz="1", oid=1),
        _open_order(coin="ETH", limit_px="2100", sz="1", oid=2),
        _open_order(coin="BTC", is_buy=False, reduce_only=True,
                    limit_px="50000", sz="0.5", oid=3),
    ]
    ex = FakeExecutor()
    _run_sync(ex, leader, [], my_positions={"BTC": _position("BTC")},
              leverage_by_coin={"ETH": (10, True), "BTC": (20, True)})

    lev_calls = [r for r in ex.records if r[0] == "update_leverage"]
    assert lev_calls == [("update_leverage", "ETH", 10, True)]  # ETH 去重一次；BTC 全 RO 不設
    # 槓桿設定先於任何下單動作（hl：進場單下單前設槓桿）
    ops = [r[0] for r in ex.records]
    assert ops.index("update_leverage") < ops.index("place")


def test_sync_without_leverage_map_sets_no_leverage():
    leader = [_open_order(coin="ETH", limit_px="2000", sz="1", oid=1)]
    ex = FakeExecutor()
    _run_sync(ex, leader, [], leverage_by_coin=None)
    assert all(r[0] != "update_leverage" for r in ex.records)


# ── sync_open_orders：槓桿 fallback（2026-07-25 首航盲區修復）─────────
def test_sync_first_voyage_regression_empty_map_uses_fallback():
    """首航回歸（2026-07-25 主網 CRIT 根因）：leader 空手＋純掛單網格 → map 空 →
    舊版靜默跳過設槓桿、follower 停在帳戶預設 → 保證金不足、ETH 階梯 6 筆只進 1。
    修法後：fallback 查 activeAssetData → 槓桿先設好、掛單照常送出。"""
    leader = [_open_order(coin="ETH", limit_px="2000", sz="1", oid=1)]
    ex = FakeExecutor()
    report = _run_sync(ex, leader, [], leverage_by_coin={},
                       leverage_fallback=lambda c: (25, True))

    assert ("update_leverage", "ETH", 25, True) in ex.records
    ops = [r[0] for r in ex.records]
    assert ops.index("update_leverage") < ops.index("place")  # 設槓桿先於下單
    assert report.reconcile.placed == 1  # 掛單照常


def test_sync_fallback_error_warns_and_other_coins_still_set():
    """fallback 對 ETH 拋例外 → warn（含 coin）＋ETH 不設槓桿；BTC 照常設、兩單照掛。"""
    leader = [
        _open_order(coin="ETH", limit_px="2000", sz="1", oid=1),
        _open_order(coin="BTC", limit_px="50000", sz="0.5", oid=2),
    ]
    ex = FakeExecutor()
    notifier = RecordingNotifier()

    def fb(coin):
        if coin == "ETH":
            raise ValueError("bad activeAssetData payload")
        return (20, False)

    report = _run_sync(ex, leader, [], notifier=notifier, leverage_by_coin={},
                       leverage_fallback=fb)

    lev_calls = [r for r in ex.records if r[0] == "update_leverage"]
    assert lev_calls == [("update_leverage", "BTC", 20, False)]
    warns = [r for r in notifier.records if r[0] == "warn"]
    assert len(warns) == 1
    assert "ETH" in warns[0][2] and "ETH" in warns[0][3]
    assert report.reconcile.placed == 2  # 槓桿查詢失敗不阻擋下單（best-effort 沿用）


def test_sync_fallback_queried_at_most_once_per_coin():
    """同幣多筆 desired → seen 去重 → fallback 每幣每輪至多查一次。"""
    leader = [
        _open_order(coin="ETH", limit_px="2000", sz="1", oid=1),
        _open_order(coin="ETH", limit_px="2100", sz="1", oid=2),
        _open_order(coin="ETH", limit_px="2200", sz="1", oid=3),
    ]
    ex = FakeExecutor()
    calls: list[str] = []

    def fb(coin):
        calls.append(coin)
        return (25, True)

    _run_sync(ex, leader, [], leverage_by_coin={}, leverage_fallback=fb)
    assert calls == ["ETH"]


# ── sync_open_orders：end-to-end 串接 _build_desired → _reconcile ────
def test_sync_end_to_end_places_scaled_order_and_reports():
    leader = [_open_order(coin="ETH", limit_px="2000", sz="2", oid=1)]
    ex = FakeExecutor()
    report = _run_sync(ex, leader, [], scale=Decimal("0.5"))

    places = [r for r in ex.records if r[0] == "place"]
    assert len(places) == 1
    assert places[0][1].sz == Decimal("1.0000")  # 2 * 0.5，4 位小數
    assert report.reconcile.placed == 1
    assert report.scale == Decimal("0.5")


def test_sync_protected_coin_warns_and_is_not_placed():
    leader = [_open_order(coin="ETH", limit_px="2000", sz="1", oid=1)]
    ex = FakeExecutor()
    notifier = RecordingNotifier()
    _run_sync(ex, leader, [], notifier=notifier, protected={"ETH"})

    assert all(r[0] != "place" for r in ex.records)
    warns = [r for r in notifier.records if r[0] == "warn"]
    assert len(warns) == 1
    assert warns[0][3] == "protected:ETH"
    assert "ETH" in warns[0][2]
