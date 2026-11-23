"""倍率鎖定純函式（plan 2026-11-22 T2b／D7）：指紋、設定鍵、`apply_latch` 四條規則。"""
from decimal import Decimal

from spark.copytrade.config import CopySettings
from spark.copytrade.scale_latch import (
    ScaleLatch,
    apply_latch,
    leader_fingerprint,
    leader_has_mirrorable_exposure,
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


# ── D10：指紋只看會被鏡射的永續 ────────────────────────────────────────
# 對應 `orders._build_desired`（掛單只略過現貨）與 `positions.sync_positions`
# （倉位略過現貨與非預設 dex）各自的實際條件。
def test_D10_fingerprint_ignores_added_or_changed_spot_orders():
    base = _fp(orders=[_order(1)])
    for spot in ("@107", "UBTC/USDC", "PURR/USDC"):
        assert _fp(orders=[_order(1), _order(9, coin=spot)]) == base, spot
        # 現貨單剩餘量／價格改變也不動指紋
        a = _fp(orders=[_order(1), _order(9, coin=spot, sz="1")])
        b = _fp(orders=[_order(1), _order(9, coin=spot, sz="0.4", px="2100")])
        assert a == b, spot


def test_D10_fingerprint_changes_when_a_perp_order_is_added():
    assert _fp(orders=[_order(1)]) != _fp(orders=[_order(1), _order(2, coin="SOL")])


def test_D10_fingerprint_ignores_spot_and_non_default_dex_positions():
    base = _fp([_pos("BTC")])
    assert _fp([_pos("BTC"), _pos("xyz:NVDA", szi="3")]) == base
    assert _fp([_pos("BTC"), _pos("@107", szi="3")]) == base
    assert _fp([_pos("BTC"), _pos("UBTC/USDC", szi="3")]) == base
    # 對照：多一個預設 dex 的永續倉位會改變指紋
    assert _fp([_pos("BTC"), _pos("ETH", szi="3")]) != base


def test_D10_fingerprint_of_only_spot_and_dex_items_is_empty_pair():
    fp = leader_fingerprint({"xyz:NVDA": _pos("xyz:NVDA", szi="3")},
                            [_order(9, coin="@107")])
    assert fp == ((), ())


def test_D10_has_mirrorable_exposure_matches_engine_skip_rules():
    spot_order = _order(9, coin="UBTC/USDC")
    assert leader_has_mirrorable_exposure({}, []) is False
    assert leader_has_mirrorable_exposure({}, [spot_order]) is False
    assert leader_has_mirrorable_exposure({"@107": _pos("@107")}, []) is False
    assert leader_has_mirrorable_exposure({"xyz:NVDA": _pos("xyz:NVDA")}, []) is False
    assert leader_has_mirrorable_exposure({}, [_order(1)]) is True
    assert leader_has_mirrorable_exposure({"BTC": _pos("BTC")}, []) is True


def test_D10_non_default_dex_order_is_mirrorable_but_position_is_not():
    """不對稱的來源：`orders._build_desired` 只略過現貨（不略過非預設 dex 的掛單），
    `positions.sync_positions` 則現貨與非預設 dex 都略過。故 `xyz:NVDA` 的**掛單**算可鏡射
    （算曝險、進指紋），**倉位**則不算（見上方既有測試）。"""
    dex_order = _order(9, coin="xyz:NVDA")
    assert leader_has_mirrorable_exposure({}, [dex_order]) is True
    assert leader_has_mirrorable_exposure({"xyz:NVDA": _pos("xyz:NVDA")}, []) is False
    # 進指紋：多這張單 → 指紋改變；剩餘量改變 → 指紋再改變
    base = _fp(orders=[_order(1)])
    with_dex = _fp(orders=[_order(1), dex_order])
    assert with_dex != base
    assert _fp(orders=[_order(1), _order(9, coin="xyz:NVDA", sz="0.4")]) != with_dex
    # 只有一張非預設 dex 掛單時指紋不是空（對照：只有 dex 倉位時指紋為空）
    assert leader_fingerprint({}, [dex_order]) != ((), ())
    assert leader_fingerprint({"xyz:NVDA": _pos("xyz:NVDA", szi="3")}, []) == ((), ())


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


# ── D8：槓桿上限生效時，縮倉方向不受鎖定（plan 2026-11-22 T4-fix）──────────
def _apply_cap(fresh, prev, *, cap_binding):
    return apply_latch(D(fresh), prev, leader=LEADER, fingerprint=_fp(), key=KEY,
                       follower_has_exposure=True, cap_binding=cap_binding)


def test_D8_cap_binding_and_smaller_fresh_recomputes_and_relocks():
    scale, new = _apply_cap("2.0", _latch("3.36"), cap_binding=True)
    assert scale == D("2.0")
    assert new == _latch("2.0")


def test_D8_cap_binding_but_larger_fresh_keeps_previous():
    prev = _latch("3.36")
    scale, new = _apply_cap("4.2", prev, cap_binding=True)
    assert scale == D("3.36") and new is prev      # 只縮不放


def test_D8_cap_binding_equal_fresh_keeps_previous():
    prev = _latch("3.36")
    scale, new = _apply_cap("3.36", prev, cap_binding=True)
    assert scale == D("3.36") and new is prev


def test_D8_not_cap_binding_smaller_fresh_still_keeps_previous():
    prev = _latch("3.36")
    scale, new = _apply_cap("2.0", prev, cap_binding=False)
    assert scale == D("3.36") and new is prev


def test_D8_cap_binding_defaults_to_false():
    prev = _latch("3.36")
    assert _apply("2.0", prev) == (D("3.36"), prev)
