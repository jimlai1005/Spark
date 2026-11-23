"""倍率鎖定純函式（plan 2026-11-22 T2b／D7）：指紋、設定鍵、`apply_latch` 四條規則。"""
from decimal import Decimal

from spark.copytrade.config import CopySettings
from spark.copytrade.scale_latch import (
    ScaleLatch,
    apply_latch,
    leader_fingerprint,
    settings_key,
)
from spark.exchange.base import OpenOrder, Position

D = Decimal
LEADER = "0xleader"


def _pos(coin="BTC", szi="-0.02", upnl="0", margin="10", entry="100000", lev=5):
    return Position(coin=coin, szi=D(szi), entry_px=D(entry), leverage=lev,
                    is_cross=True, unrealized_pnl=D(upnl), margin_used=D(margin))


def _order(oid=1, coin="ETH", sz="1", px="2000", is_buy=True, trigger_px=None,
           reduce_only=False):
    return OpenOrder(oid=oid, coin=coin, is_buy=is_buy, limit_px=D(px), sz=D(sz),
                     reduce_only=reduce_only, is_trigger=trigger_px is not None,
                     trigger_px=D(trigger_px) if trigger_px else None, tpsl=None)


def _fp(positions=None, orders=()):
    ps = positions if positions is not None else [_pos()]
    return leader_fingerprint({p.coin: p for p in ps}, list(orders))


KEY = settings_key(CopySettings())


# ── 指紋 ──────────────────────────────────────────────────────────────
def test_fingerprint_changes_when_position_size_changes():
    assert _fp([_pos(szi="-0.02")]) != _fp([_pos(szi="-0.03")])


def test_fingerprint_changes_when_an_order_is_added():
    assert _fp(orders=[]) != _fp(orders=[_order()])


def test_fingerprint_changes_when_order_remaining_size_changes():
    assert _fp(orders=[_order(sz="1")]) != _fp(orders=[_order(sz="0.6")])


def test_fingerprint_changes_when_order_price_or_trigger_changes():
    assert _fp(orders=[_order(px="2000")]) != _fp(orders=[_order(px="2001")])
    assert (_fp(orders=[_order(trigger_px="1900")])
            != _fp(orders=[_order(trigger_px="1800")]))


def test_fingerprint_ignores_unrealized_pnl_margin_entry_and_leverage():
    """只有會自己變動的值（未實現損益、保證金…）變了：指紋不變。"""
    a = _fp([_pos(upnl="0", margin="10", entry="100000", lev=5)])
    b = _fp([_pos(upnl="-35.5", margin="14", entry="100500", lev=10)])
    assert a == b


def test_fingerprint_is_order_independent_and_empty_is_empty_pair():
    p1, p2 = _pos("BTC", "-0.02"), _pos("ETH", "1")
    o1, o2 = _order(1), _order(2, coin="SOL")
    assert (leader_fingerprint({"BTC": p1, "ETH": p2}, [o1, o2])
            == leader_fingerprint({"ETH": p2, "BTC": p1}, [o2, o1]))
    assert leader_fingerprint({}, []) == ((), ())


# ── settings_key ──────────────────────────────────────────────────────
def test_settings_key_reacts_to_utilization_and_other_sizing_fields():
    base = settings_key(CopySettings(capital_utilization=D("0.5")))
    assert settings_key(CopySettings(capital_utilization=D("0.25"))) != base
    assert settings_key(CopySettings(capital_utilization=D("0.5"),
                                     position_weight=D("0.5"))) != base
    assert settings_key(CopySettings(capital_utilization=D("0.5"),
                                     max_target_leverage=D("3"))) != base
    assert settings_key(CopySettings(capital_utilization=D("0.5"))) == base


# ── apply_latch 規則 1–4 ───────────────────────────────────────────────
def _latch(scale="3.36", fp=None, key=KEY, leader=LEADER) -> ScaleLatch:
    return ScaleLatch(D(scale), leader, fp if fp is not None else _fp(), key)


def _apply(fresh, prev, *, fp=None, key=KEY, leader=LEADER, has_exposure=True):
    return apply_latch(D(fresh), prev, leader=leader,
                       fingerprint=fp if fp is not None else _fp(), key=key,
                       follower_has_exposure=has_exposure)


def test_rule1_nonpositive_fresh_scale_passes_through_and_clears_latch():
    assert _apply("0", _latch()) == (D("0"), None)
    assert _apply("-1", _latch()) == (D("-1"), None)


def test_rule2_empty_fingerprint_passes_through_and_clears_latch():
    assert _apply("4.2", _latch(), fp=((), ())) == (D("4.2"), None)


def test_rule3_no_previous_latch_locks_fresh_value():
    scale, new = _apply("3.36", None)
    assert scale == D("3.36")
    assert new == ScaleLatch(D("3.36"), LEADER, _fp(), KEY)


def test_rule3_leader_changed_recomputes():
    scale, new = _apply("4.2", _latch(leader="0xother"))
    assert scale == D("4.2") and new.scale == D("4.2") and new.leader == LEADER


def test_rule3_settings_changed_recomputes():
    other = settings_key(CopySettings(capital_utilization=D("0.25")))
    scale, new = _apply("1.68", _latch(), key=other)
    assert scale == D("1.68") and new.settings_key == other


def test_rule3_fingerprint_changed_recomputes():
    fp2 = _fp([_pos(szi="-0.03")])
    scale, new = _apply("4.2", _latch(), fp=fp2)
    assert scale == D("4.2") and new.fingerprint == fp2


def test_rule3_follower_without_exposure_recomputes():
    scale, new = _apply("4.2", _latch(), has_exposure=False)
    assert scale == D("4.2") and new.scale == D("4.2")


def test_rule4_everything_unchanged_keeps_previous_scale_and_latch_object():
    prev = _latch("3.36")
    scale, new = _apply("4.2", prev)  # 新算出來漂移 +25%，仍用舊的
    assert scale == D("3.36")
    assert new is prev


def test_apply_latch_is_pure_does_not_mutate_inputs():
    prev = _latch("3.36")
    _apply("4.2", prev)
    assert prev == _latch("3.36")
