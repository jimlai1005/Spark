"""settle 重試不得重下已成交的鏡射單（2026-09-30 PUMP／ENA 重複成交事故）。

D1 可行動 missing 謂詞：本輪最近一次 place 失敗，或同 slot 有 extra，才補下／CRIT；
其餘 missing 視為已消耗（成交或外部撤銷），不補、不通知。
"""
from decimal import Decimal

from spark.copytrade.config import CopySettings
from spark.copytrade.notifier import RecordingNotifier
from spark.copytrade.orders import OrderSpec, ReconcileState, _reconcile_orders
from spark.exchange.base import OpenOrder

from tests.test_copy_orders_crit_alert import FakeExecutor

SETTINGS = CopySettings(px_rel_tol=Decimal("1e-4"), size_tolerance=Decimal("0.08"))


def _spec(coin="PUMP", is_buy=False, sz="168927", px="0.00576", ro=False,
          **kw) -> OrderSpec:
    return OrderSpec(coin=coin, is_buy=is_buy, sz=Decimal(sz),
                     limit_px=Decimal(px), reduce_only=ro, **kw)


def _open(spec: OrderSpec, oid=1) -> OpenOrder:
    return OpenOrder(oid=oid, coin=spec.coin, is_buy=spec.is_buy,
                     limit_px=spec.limit_px, sz=spec.sz,
                     reduce_only=spec.reduce_only, is_trigger=False,
                     trigger_px=None, tpsl=None)


def _run(desired, my_orders, open_seq, place_seq=None):
    ex = FakeExecutor(open_orders_seq=open_seq, place_reason_seq=place_seq)
    n = RecordingNotifier()
    res = _reconcile_orders(
        ex, desired, my_orders, settings=SETTINGS, notifier=n,
        state=ReconcileState(), live=True, clock=lambda: 1000.0,
        sleep_fn=lambda s: None, my_positions={})
    return ex, n, res


def _places(ex):
    return [r for r in ex.records if r[0] == "place"]


def _crits(n):
    return [r for r in n.records if r[0] == "critical"]


def test_pump_filled_after_place_is_not_replaced():
    ex, n, res = _run([_spec()], [], [[], []])
    assert len(_places(ex)) == 1
    assert _crits(n) == []
    assert res.sync_failed is False


def test_ena_buy_filled_after_place_is_not_replaced():
    d = _spec(coin="ENA", is_buy=True, sz="2319", px="0.2652")
    ex, n, res = _run([d], [], [[], []])
    assert len(_places(ex)) == 1
    assert _crits(n) == []
    assert res.sync_failed is False


def test_matched_order_vanishing_is_not_replaced():
    d = _spec()
    ex, n, res = _run([d], [_open(d)], [[], []])
    assert _places(ex) == []
    assert _crits(n) == []
    assert res.sync_failed is False


def test_failed_place_is_retried_once_and_crit_on_second_failure():
    d = _spec()
    ex, n, res = _run([d], [], [[], []],
                      place_seq=[(False, "boom"), (False, "still boom")])
    assert len(_places(ex)) == 2
    assert res.sync_failed is True
    assert "still boom" in _crits(n)[0][2]


def test_failed_place_retry_success_then_consumed_no_crit():
    d = _spec()
    ex, n, res = _run([d], [], [[], []], place_seq=[(False, "boom"), (True, "")])
    assert len(_places(ex)) == 2
    assert _crits(n) == []
    assert res.sync_failed is False


def test_trigger_rejection_stays_loud():
    d = _spec(is_trigger=True, tpsl="sl", trigger_px=Decimal("0.006"))
    ex, n, res = _run([d], [], [[], []],
                      place_seq=[(False, "trigger skip"), (False, "trigger skip")])
    assert res.sync_failed is True
    assert len(_crits(n)) == 1


def test_extra_only_still_cancelled_and_crit():
    stray = _open(_spec(coin="ETH", px="2000", sz="1"), oid=7)
    ex, n, res = _run([], [], [[stray], [stray]])
    assert ("cancel", "ETH", 7) in ex.records
    assert res.sync_failed is True
    assert len(_crits(n)) == 1


# ── R2／R3／R4（審查補強）────────────────────────────────────────────
def _run_counted(desired, open_seq, place_seq=None):
    ex = FakeExecutor(open_orders_seq=open_seq, place_reason_seq=place_seq)
    gets: list = []
    orig_get = ex.get_open_orders

    def _get():
        gets.append(1)
        return orig_get()

    ex.get_open_orders = _get
    n = RecordingNotifier()
    sleeps: list = []
    res = _reconcile_orders(
        ex, desired, [], settings=SETTINGS, notifier=n, state=ReconcileState(),
        live=True, clock=lambda: 1000.0, sleep_fn=sleeps.append, my_positions={})
    return ex, n, res, gets, sleeps


def test_pump_ena_no_second_round_when_nothing_actionable():
    ex, n, res, gets, sleeps = _run_counted([_spec()], [[], []])
    assert len(gets) == 1 and len(sleeps) == 1
    ex, n, res, gets, sleeps = _run_counted(
        [_spec(coin="ENA", is_buy=True, sz="2319", px="0.2652")], [[], []])
    assert len(gets) == 1 and len(sleeps) == 1


def test_non_ro_partial_fill_not_replaced_residual_cancelled():
    d = _spec(sz="1000")
    residual = _open(_spec(sz="400"), oid=5)
    ex, n, res, _g, _s = _run_counted([d], [[residual], []])
    assert len(_places(ex)) == 1
    assert ("cancel", "PUMP", 5) in ex.records
    assert _crits(n) == []
    assert res.sync_failed is False


def test_same_slot_two_non_ro_one_full_one_partial_zero_replace():
    a = _spec(px="0.00576", sz="1000")
    b = _spec(px="0.00570", sz="1000")
    residual_b = _open(_spec(px="0.00570", sz="400"), oid=7)
    ex, n, res, _g, _s = _run_counted([a, b], [[residual_b], []])
    assert len(_places(ex)) == 2  # 只有步驟 3 的兩次初掛
    assert _crits(n) == []
    assert res.sync_failed is False


def test_duplicate_specs_fail_then_ok_retries_once():
    a = _spec()
    ex, n, res, _g, _s = _run_counted(
        [a, a], [[], []], place_seq=[(False, "margin"), (True, "")])
    assert len(_places(ex)) == 3  # 初掛 2 ＋ 重試 1
    assert _crits(n) == []
    assert res.sync_failed is False


def test_duplicate_specs_fail_then_ok_retry_fails_crits_with_reason():
    a = _spec()
    ex, n, res, _g, _s = _run_counted(
        [a, a], [[], []],
        place_seq=[(False, "margin"), (True, ""), (False, "boom")])
    assert len(_places(ex)) == 3
    assert res.sync_failed is True
    assert "boom" in _crits(n)[0][2]


def test_duplicate_specs_ok_then_fail_retries_once():
    a = _spec()
    ex, n, res, _g, _s = _run_counted(
        [a, a], [[], []], place_seq=[(True, ""), (False, "margin")])
    assert len(_places(ex)) == 3  # 只重試失敗的那 1 張
    assert _crits(n) == []
