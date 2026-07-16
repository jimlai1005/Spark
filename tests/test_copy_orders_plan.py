"""copytrade/orders.py 純函式層 characterization 測試。容忍度數字對齊 CopySettings 預設
（px_rel_tol=1e-4、size_tolerance=0.02）。#8 的 reduce-only 案例 port 自 hl-copytrader
tests/test_reduce_only_guard.py。"""
from decimal import Decimal

from spark.copytrade.orders import (
    OrderSpec,
    SkippedOrder,
    _build_desired,
    _plan,
    spec_from_open_order,
)
from spark.exchange.base import OpenOrder, Position

PX_TOL = Decimal("1e-4")
SIZE_TOL = Decimal("0.02")


def _spec(coin="ETH", is_buy=True, sz="1.0", limit_px="2000", reduce_only=False,
          is_trigger=False, tpsl=None, trigger_px=None, is_market=False) -> OrderSpec:
    return OrderSpec(
        coin=coin,
        is_buy=is_buy,
        sz=Decimal(sz),
        limit_px=Decimal(limit_px),
        reduce_only=reduce_only,
        is_trigger=is_trigger,
        tpsl=tpsl,
        trigger_px=Decimal(trigger_px) if trigger_px is not None else None,
        is_market=is_market,
    )


def _open_order(coin="ETH", is_buy=True, sz="1.0", limit_px="2000", reduce_only=False,
                 is_trigger=False, tpsl=None, trigger_px=None, oid=1) -> OpenOrder:
    return OpenOrder(
        oid=oid,
        coin=coin,
        is_buy=is_buy,
        limit_px=Decimal(limit_px),
        sz=Decimal(sz),
        reduce_only=reduce_only,
        is_trigger=is_trigger,
        trigger_px=Decimal(trigger_px) if trigger_px is not None else None,
        tpsl=tpsl,
    )


def _position(coin="ETH") -> Position:
    return Position(
        coin=coin, szi=Decimal("1"), entry_px=Decimal("1900"), leverage=5,
        is_cross=True, unrealized_pnl=Decimal("0"), margin_used=Decimal("100"),
    )


def _plan_(desired, mine):
    return _plan(desired, mine, px_rel_tol=PX_TOL, size_tol=SIZE_TOL)


# ── 1. 完全相符 → 全 matched 零動作 ──────────────────────────────────
def test_exact_match_is_matched_with_no_actions():
    d = _spec(coin="ETH", is_buy=True, sz="1.0", limit_px="2000")
    plan = _plan_([d], [(7, d)])
    assert plan.matched == frozenset({7})
    assert plan.modifies == ()
    assert plan.to_place == ()
    assert plan.to_cancel == ()


# ── 2. 同 slot 價移 → modify ─────────────────────────────────────────
def test_same_slot_price_move_produces_modify():
    mine = _spec(limit_px="2000")
    desired = _spec(limit_px="2010")
    plan = _plan_([desired], [(7, mine)])
    assert plan.modifies == ((7, desired),)
    assert plan.matched == frozenset()
    assert plan.to_place == ()
    assert plan.to_cancel == ()


# ── 3. px 相對容忍度邊界 ─────────────────────────────────────────────
def test_px_rel_diff_9_5e5_within_tolerance_matches():
    mine = _spec(limit_px="2000")
    desired = _spec(limit_px="2000.19")  # diff/max ≈ 9.4998e-5 < 1e-4
    plan = _plan_([desired], [(7, mine)])
    assert plan.matched == frozenset({7})
    assert plan.modifies == ()


def test_px_rel_diff_2_5e4_outside_tolerance_modifies():
    mine = _spec(limit_px="2000")
    desired = _spec(limit_px="2000.5")  # diff/max ≈ 2.4994e-4 > 1e-4
    plan = _plan_([desired], [(7, mine)])
    assert plan.modifies == ((7, desired),)
    assert plan.matched == frozenset()


# ── 4. size 容忍度邊界 ───────────────────────────────────────────────
def test_size_diff_within_tolerance_matches():
    mine = _spec(sz="1.0")
    desired = _spec(sz="1.015")  # 1.5% <= 2%
    plan = _plan_([desired], [(7, mine)])
    assert plan.matched == frozenset({7})
    assert plan.modifies == ()


def test_size_diff_outside_tolerance_modifies():
    mine = _spec(sz="1.0")
    desired = _spec(sz="1.03")  # 3% > 2%
    plan = _plan_([desired], [(7, mine)])
    assert plan.modifies == ((7, desired),)
    assert plan.matched == frozenset()


# ── 5. 同 slot 多一張 desired → 1 modify + 1 place；_ref_px 排序配對 ──
def test_same_slot_extra_desired_yields_modify_and_place():
    mine = _spec(limit_px="2000")
    d_close = _spec(limit_px="2005")
    d_far = _spec(limit_px="2100")
    plan = _plan_([d_close, d_far], [(7, mine)])
    assert plan.modifies == ((7, d_close),)
    assert plan.to_place == (d_far,)
    assert plan.to_cancel == ()
    assert plan.matched == frozenset()


def test_ref_px_sort_pairs_lowest_with_lowest_regardless_of_oid_or_input_order():
    """三張不同價：mine 依 oid 遞減但價格遞增排列於輸入清單，驗證配對依「參考價排序」
    而非輸入順序或 oid 大小。"""
    mine_high = _spec(limit_px="2050")   # oid=10，價格較高
    mine_low = _spec(limit_px="2000")    # oid=20，價格較低
    d_lowest = _spec(limit_px="1990")
    d_mid = _spec(limit_px="2010")
    d_highest = _spec(limit_px="2200")
    plan = _plan_(
        [d_lowest, d_mid, d_highest],
        [(10, mine_high), (20, mine_low)],  # 輸入順序刻意與價格排序相反
    )
    # 價格排序後：mine [2000(oid20), 2050(oid10)] 對 desired [1990, 2010, 2200]
    assert plan.modifies == ((20, d_lowest), (10, d_mid))
    assert plan.to_place == (d_highest,)
    assert plan.to_cancel == ()


# ── 6. mine 多 → cancel；不同 slot 絕不 modify ───────────────────────
def test_mine_extra_in_same_slot_is_cancelled():
    mine_a = _spec(limit_px="2000")
    mine_b = _spec(limit_px="2100")
    desired = _spec(limit_px="2050")
    plan = _plan_([desired], [(7, mine_a), (8, mine_b)])
    assert plan.modifies == ((7, desired),)
    assert plan.to_cancel == (8,)
    assert plan.to_place == ()


def test_different_slot_buy_vs_sell_never_modifies():
    mine_sell = _spec(is_buy=False, limit_px="2000")
    desired_buy = _spec(is_buy=True, limit_px="2000")
    plan = _plan_([desired_buy], [(7, mine_sell)])
    assert plan.modifies == ()
    assert plan.to_place == (desired_buy,)
    assert plan.to_cancel == (7,)


def test_different_slot_reduce_only_mismatch_never_modifies():
    mine_ro = _spec(reduce_only=True, limit_px="2000")
    desired_non_ro = _spec(reduce_only=False, limit_px="2000")
    plan = _plan_([desired_non_ro], [(7, mine_ro)])
    assert plan.modifies == ()
    assert plan.to_place == (desired_non_ro,)
    assert plan.to_cancel == (7,)


# ── 7. trigger 單與限價單不同 slot；tpsl 不同 → 不同 slot ────────────
def test_trigger_and_limit_order_are_different_slots():
    mine_limit = _spec(limit_px="2000")
    desired_trigger = _spec(limit_px="2000", is_trigger=True, tpsl="sl", trigger_px="2000")
    plan = _plan_([desired_trigger], [(7, mine_limit)])
    assert plan.modifies == ()
    assert plan.to_place == (desired_trigger,)
    assert plan.to_cancel == (7,)


def test_tpsl_mismatch_is_different_slot():
    mine_tp = _spec(is_buy=False, is_trigger=True, tpsl="tp", trigger_px="2000", limit_px="2000")
    desired_sl = _spec(is_buy=False, is_trigger=True, tpsl="sl", trigger_px="2000", limit_px="2000")
    plan = _plan_([desired_sl], [(7, mine_tp)])
    assert plan.modifies == ()
    assert plan.to_place == (desired_sl,)
    assert plan.to_cancel == (7,)


# ── 8. _build_desired 過濾與縮放 ──────────────────────────────────────
def test_build_desired_small_notional_skipped_with_correct_value():
    leader = [_open_order(coin="BTC", limit_px="50000", sz="0.0001", oid=1)]
    desired, skipped = _build_desired(
        leader, scale=Decimal("1"), min_notional=Decimal("10"),
        size_decimals=lambda coin: 4, my_positions={}, protected=set(),
    )
    assert desired == []
    assert skipped == [SkippedOrder(coin="BTC", notional=Decimal("5.0000"), reason="small")]


def test_build_desired_spot_coin_skipped():
    leader = [_open_order(coin="@85", limit_px="1", sz="100", oid=1)]
    desired, skipped = _build_desired(
        leader, scale=Decimal("1"), min_notional=Decimal("10"),
        size_decimals=lambda coin: 4, my_positions={}, protected=set(),
    )
    assert desired == []
    assert skipped == [SkippedOrder(coin="@85", notional=Decimal("0"), reason="spot")]


def test_build_desired_reduce_only_without_position_skipped():
    """port 自 hl tests/test_reduce_only_guard.py::test_reduce_only_skipped_without_position。"""
    leader = [
        _open_order(coin="ETH", is_buy=False, reduce_only=True, oid=1),
        _open_order(coin="BTC", is_buy=False, reduce_only=False, limit_px="50000", sz="1", oid=2),
    ]
    desired, skipped = _build_desired(
        leader, scale=Decimal("1"), min_notional=Decimal("10"),
        size_decimals=lambda coin: 4, my_positions={}, protected=set(),
    )
    coins = {d.coin for d in desired}
    assert "ETH" not in coins
    assert "BTC" in coins
    assert SkippedOrder(coin="ETH", notional=Decimal("0"), reason="reduce_only_no_pos") in skipped


def test_build_desired_reduce_only_with_position_kept():
    """port 自 hl tests/test_reduce_only_guard.py::test_reduce_only_kept_with_position。"""
    leader = [_open_order(coin="ETH", is_buy=False, reduce_only=True, limit_px="2000", sz="1", oid=1)]
    desired, skipped = _build_desired(
        leader, scale=Decimal("1"), min_notional=Decimal("10"),
        size_decimals=lambda coin: 4, my_positions={"ETH": _position("ETH")}, protected=set(),
    )
    assert {d.coin for d in desired} == {"ETH"}
    assert skipped == []


def test_build_desired_protected_non_reduce_only_skipped():
    leader = [_open_order(coin="ETH", reduce_only=False, limit_px="2000", sz="1", oid=1)]
    desired, skipped = _build_desired(
        leader, scale=Decimal("1"), min_notional=Decimal("10"),
        size_decimals=lambda coin: 4, my_positions={}, protected={"ETH"},
    )
    assert desired == []
    assert skipped == [SkippedOrder(coin="ETH", notional=Decimal("0"), reason="protected")]


def test_build_desired_protected_reduce_only_kept():
    """抗單保護只擋補倉，不擋減倉/止盈止損（reduce-only 仍放行）。"""
    leader = [_open_order(coin="ETH", reduce_only=True, limit_px="2000", sz="1", oid=1)]
    desired, skipped = _build_desired(
        leader, scale=Decimal("1"), min_notional=Decimal("10"),
        size_decimals=lambda coin: 4, my_positions={"ETH": _position("ETH")}, protected={"ETH"},
    )
    assert {d.coin for d in desired} == {"ETH"}
    assert skipped == []


def test_build_desired_size_rounded_down_by_sz_decimals():
    leader = [_open_order(coin="BTC", limit_px="50000", sz="1.23456", oid=1)]
    desired, skipped = _build_desired(
        leader, scale=Decimal("1"), min_notional=Decimal("10"),
        size_decimals=lambda coin: 3, my_positions={}, protected=set(),
    )
    assert skipped == []
    assert len(desired) == 1
    assert desired[0].sz == Decimal("1.234")


# ── 9. spec_from_open_order 欄位映射 ──────────────────────────────────
def test_spec_from_open_order_field_mapping():
    o = OpenOrder(
        oid=42, coin="ETH", is_buy=True, limit_px=Decimal("2000"), sz=Decimal("1.5"),
        reduce_only=True, is_trigger=True, trigger_px=Decimal("1990"), tpsl="sl",
    )
    spec = spec_from_open_order(o)
    assert spec == OrderSpec(
        coin="ETH", is_buy=True, sz=Decimal("1.5"), limit_px=Decimal("2000"),
        reduce_only=True, is_trigger=True, tpsl="sl", trigger_px=Decimal("1990"),
        is_market=False,
    )
