"""_build_desired 的 reduce-only 每幣累計封頂測試（2026-07-28 00:33 CRIT 事故修法）。

事故背景：Hyperliquid 會把 reduce-only 掛單修剪到「每幣 ro 總量 ≤ 目前部位」
（實測：follower 部位 ETH 0.0289 時，鏡射 leader 的 ro 賣單 0.0457 被剪到 0.0289；
部位補到 0.0918 後兩張 ro 賣單 0.0456+0.0461=0.0917 全額存活——每幣累計封頂，
非逐單）。desired 若不做同樣的封頂，_verify_diff 會把同一張單同時判進
「缺少」與「多餘」→ 誤發 CRIT。

修法核心（工程原則 1 同源同基準）：desired 與交易所實際單都以 follower 部位為約束。
"""
from decimal import Decimal

from spark.copytrade.orders import (
    SkippedOrder,
    _build_desired,
    _verify_diff,
)
from spark.exchange.base import OpenOrder, Position

PX_TOL = Decimal("1e-4")
SIZE_TOL = Decimal("0.08")


def _open_order(coin="ETH", is_buy=True, sz="1.0", limit_px="2000", reduce_only=False,
                is_trigger=False, tpsl=None, trigger_px=None, oid=1,
                is_market=False, tif="Gtc") -> OpenOrder:
    return OpenOrder(
        oid=oid, coin=coin, is_buy=is_buy, limit_px=Decimal(limit_px), sz=Decimal(sz),
        reduce_only=reduce_only, is_trigger=is_trigger,
        trigger_px=Decimal(trigger_px) if trigger_px is not None else None,
        tpsl=tpsl, is_market=is_market, tif=tif,
    )


def _position(coin="ETH", szi="1") -> Position:
    return Position(
        coin=coin, szi=Decimal(szi), entry_px=Decimal("1900"), leverage=5,
        is_cross=True, unrealized_pnl=Decimal("0"), margin_used=Decimal("100"),
    )


def _build(leader, positions, *, scale="1", min_notional="10", sz_dec=4):
    return _build_desired(
        leader, scale=Decimal(scale), min_notional=Decimal(min_notional),
        size_decimals=lambda coin: sz_dec, my_positions=positions, protected=set(),
    )


# ── (a) 單張 ro 超過部位 → 剪到部位量 ────────────────────────────────
def test_single_ro_capped_to_position_size():
    leader = [_open_order(coin="ETH", is_buy=False, reduce_only=True,
                          sz="0.0457", limit_px="2000", oid=1)]
    desired, skipped = _build(leader, {"ETH": _position("ETH", szi="0.0289")})
    assert skipped == []
    assert len(desired) == 1
    assert desired[0].sz == Decimal("0.0289")
    assert desired[0].reduce_only is True


# ── (b) 兩張 ro 累計超過部位：接近成交側先拿容量、另一張拿殘量 ────────
def test_two_ro_sells_cumulative_cap_lower_price_takes_capacity_first():
    """減多倉（賣）價低者接近成交 → 先分配；高價單拿殘量。"""
    leader = [
        _open_order(coin="ETH", is_buy=False, reduce_only=True,
                    sz="0.04", limit_px="2100", oid=1),  # 較遠
        _open_order(coin="ETH", is_buy=False, reduce_only=True,
                    sz="0.04", limit_px="2000", oid=2),  # 較近成交（價低）
    ]
    desired, skipped = _build(leader, {"ETH": _position("ETH", szi="0.05")})
    assert skipped == []
    by_px = {d.limit_px: d.sz for d in desired}
    assert by_px[Decimal("2000")] == Decimal("0.04")  # 價低者全額
    assert by_px[Decimal("2100")] == Decimal("0.01")  # 殘量


def test_two_ro_buys_cumulative_cap_higher_price_takes_capacity_first():
    """減空倉（買）價高者接近成交 → 先分配；低價單拿殘量。"""
    leader = [
        _open_order(coin="ETH", is_buy=True, reduce_only=True,
                    sz="0.04", limit_px="1800", oid=1),  # 較遠
        _open_order(coin="ETH", is_buy=True, reduce_only=True,
                    sz="0.04", limit_px="1900", oid=2),  # 較近成交（價高）
    ]
    desired, skipped = _build(leader, {"ETH": _position("ETH", szi="-0.05")})
    assert skipped == []
    by_px = {d.limit_px: d.sz for d in desired}
    assert by_px[Decimal("1900")] == Decimal("0.04")
    assert by_px[Decimal("1800")] == Decimal("0.01")


# ── (c) 容量歸零後第三張被略過 ───────────────────────────────────────
def test_third_ro_dropped_after_capacity_exhausted():
    leader = [
        _open_order(coin="ETH", is_buy=False, reduce_only=True,
                    sz="0.03", limit_px="2000", oid=1),
        _open_order(coin="ETH", is_buy=False, reduce_only=True,
                    sz="0.02", limit_px="2100", oid=2),
        _open_order(coin="ETH", is_buy=False, reduce_only=True,
                    sz="0.02", limit_px="2200", oid=3),  # 容量已歸零 → 略過
    ]
    desired, _skipped = _build(leader, {"ETH": _position("ETH", szi="0.05")})
    assert len(desired) == 2
    assert {d.limit_px for d in desired} == {Decimal("2000"), Decimal("2100")}
    assert sum(d.sz for d in desired) == Decimal("0.05")  # 恰好等於部位


# ── (d) 封頂後名目 < min_notional → 過濾（先封頂、再過名目）──────────
def test_capped_ro_below_min_notional_is_filtered_as_small():
    """部分減倉單無 $10 豁免（豁免只適用全平）：封頂成小額後照樣要被擋。"""
    leader = [_open_order(coin="ETH", is_buy=False, reduce_only=True,
                          sz="0.05", limit_px="2000", oid=1)]
    desired, skipped = _build(leader, {"ETH": _position("ETH", szi="0.004")})
    assert desired == []
    assert len(skipped) == 1
    assert skipped[0].reason == "small"
    # 名目用封頂後的量算（0.004×2000=8），不是原量（0.05×2000=100）
    assert skipped[0].notional == Decimal("8")


# ── (e) 部位充足 → 完全不封頂（回歸；事故實測第二段證據）─────────────
def test_sufficient_position_no_capping_regression():
    """部位 0.0918 時兩張 ro 賣單 0.0456+0.0461=0.0917 全額存活（實測證據）。"""
    leader = [
        _open_order(coin="ETH", is_buy=False, reduce_only=True,
                    sz="0.0456", limit_px="2000", oid=1),
        _open_order(coin="ETH", is_buy=False, reduce_only=True,
                    sz="0.0461", limit_px="2100", oid=2),
    ]
    desired, skipped = _build(leader, {"ETH": _position("ETH", szi="0.0918")})
    assert skipped == []
    assert sorted(d.sz for d in desired) == [Decimal("0.0456"), Decimal("0.0461")]


# ── (f) 非 ro 單完全不受影響（回歸）──────────────────────────────────
def test_non_ro_orders_unaffected_by_position_cap():
    leader = [
        _open_order(coin="ETH", is_buy=True, reduce_only=False,
                    sz="5", limit_px="2000", oid=1),  # 遠超部位，但非 ro → 不封頂
        _open_order(coin="ETH", is_buy=False, reduce_only=True,
                    sz="0.04", limit_px="2100", oid=2),
    ]
    desired, skipped = _build(leader, {"ETH": _position("ETH", szi="0.01")})
    assert skipped == []
    by_ro = {d.reduce_only: d for d in desired}
    assert by_ro[False].sz == Decimal("5")       # 非 ro 原樣
    assert by_ro[True].sz == Decimal("0.01")     # ro 封頂到部位


# ── (g) 事故重現：desired 與交易所修剪後實際單相符，_verify_diff 不誤報 ──
def test_incident_20260728_replay_verify_diff_clean():
    """2026-07-28 00:33：leader ro 賣 0.0457、follower 部位 0.0289 →
    交易所把實際單修剪到 0.0289。修法後 desired 也是 0.0289 →
    _verify_diff 應為零 missing/零 extra（原本會同一張單同進「缺少」與「多餘」）。"""
    leader = [_open_order(coin="ETH", is_buy=False, reduce_only=True,
                          sz="0.0457", limit_px="2000", oid=1)]
    desired, _ = _build(leader, {"ETH": _position("ETH", szi="0.0289")})
    assert [d.sz for d in desired] == [Decimal("0.0289")]

    # 交易所上的實際單（已被修剪到部位量）
    after = [_open_order(coin="ETH", is_buy=False, reduce_only=True,
                         sz="0.0289", limit_px="2000", oid=77)]
    missing, extra = _verify_diff(desired, after, px_rel_tol=PX_TOL, size_tol=SIZE_TOL)
    assert missing == []
    assert extra == []


# ── 方向不符：ro 賣配空倉／ro 買配多倉 → 跳過（同無部位語意）─────────
def test_ro_sell_against_short_position_is_skipped():
    leader = [_open_order(coin="ETH", is_buy=False, reduce_only=True,
                          sz="0.04", limit_px="2000", oid=1)]
    desired, skipped = _build(leader, {"ETH": _position("ETH", szi="-0.05")})
    assert desired == []
    assert skipped == [
        SkippedOrder(coin="ETH", notional=Decimal("0"), reason="reduce_only_no_pos")
    ]


def test_ro_buy_against_long_position_is_skipped():
    leader = [_open_order(coin="ETH", is_buy=True, reduce_only=True,
                          sz="0.04", limit_px="1900", oid=1)]
    desired, skipped = _build(leader, {"ETH": _position("ETH", szi="0.05")})
    assert desired == []
    assert skipped == [
        SkippedOrder(coin="ETH", notional=Decimal("0"), reason="reduce_only_no_pos")
    ]


# ── 排序確定性：同價位以原始順序 tie-break，兩次結果一致 ─────────────
def test_same_price_tie_break_is_deterministic_by_original_order():
    leader = [
        _open_order(coin="ETH", is_buy=False, reduce_only=True,
                    sz="0.03", limit_px="2000", oid=11),
        _open_order(coin="ETH", is_buy=False, reduce_only=True,
                    sz="0.03", limit_px="2000", oid=12),  # 同價 → 原始順序在後
    ]
    positions = {"ETH": _position("ETH", szi="0.05")}
    d1, _ = _build(leader, positions)
    d2, _ = _build(leader, positions)
    assert d1 == d2  # 逐輪分配不得翻轉（避免 modify churn）
    assert [d.sz for d in d1] == [Decimal("0.03"), Decimal("0.02")]


# ── 封頂只縮不放大 ───────────────────────────────────────────────────
def test_cap_never_enlarges_order_size():
    leader = [_open_order(coin="ETH", is_buy=False, reduce_only=True,
                          sz="0.01", limit_px="2000", oid=1)]
    desired, _ = _build(leader, {"ETH": _position("ETH", szi="5")})
    assert desired[0].sz == Decimal("0.01")  # 容量再大也不放大


# ── trigger ro（止盈止損）不參與封頂（維持現行行為）──────────────────
def test_trigger_ro_not_capped_and_consumes_no_capacity():
    """交易所修剪的實測證據只涵蓋掛在簿上的 ro 限價單；trigger 單未觸發前不佔簿，
    若對其封頂而交易所不剪，反而製造永久 mismatch → churn。維持現行不封頂。"""
    leader = [
        _open_order(coin="ETH", is_buy=False, reduce_only=True, is_trigger=True,
                    tpsl="sl", trigger_px="1800", limit_px="1800", sz="0.5", oid=1),
        _open_order(coin="ETH", is_buy=False, reduce_only=True,
                    sz="0.04", limit_px="2000", oid=2),
    ]
    desired, skipped = _build(leader, {"ETH": _position("ETH", szi="0.05")})
    assert skipped == []
    by_trigger = {d.is_trigger: d for d in desired}
    assert by_trigger[True].sz == Decimal("0.5")    # trigger 不封頂
    assert by_trigger[False].sz == Decimal("0.04")  # 限價 ro 容量不被 trigger 消耗


# ── Finding 1（2026-07-28 審查）：被名目濾掉的 ro 單不得先扣容量 ─────
def test_small_filtered_ro_does_not_consume_capacity():
    """被 min_notional 濾掉的單不會被送到交易所，交易所端容量完整——
    desired 側若先扣容量，會把後續正確的保護性減倉單無故改小。"""
    leader = [
        _open_order(coin="ETH", is_buy=False, reduce_only=True,
                    sz="0.0045", limit_px="2000", oid=1),  # 名目 $9 → small 濾掉
        _open_order(coin="ETH", is_buy=False, reduce_only=True,
                    sz="0.03", limit_px="2100", oid=2),
    ]
    desired, skipped = _build(leader, {"ETH": _position("ETH", szi="0.03")})
    assert [d.sz for d in desired] == [Decimal("0.03")]  # 容量完整，不是 0.0255
    assert [s.reason for s in skipped] == ["small"]


# ── Finding 4：容量歸零的 ro 單要記 SkippedOrder，不得靜默丟棄 ───────
def test_capacity_exhausted_ro_recorded_as_skipped():
    leader = [
        _open_order(coin="ETH", is_buy=False, reduce_only=True,
                    sz="0.03", limit_px="2000", oid=1),
        _open_order(coin="ETH", is_buy=False, reduce_only=True,
                    sz="0.02", limit_px="2100", oid=2),
        _open_order(coin="ETH", is_buy=False, reduce_only=True,
                    sz="0.02", limit_px="2200", oid=3),  # 容量歸零 → 記錄後略過
    ]
    _desired, skipped = _build(leader, {"ETH": _position("ETH", szi="0.05")})
    assert skipped == [
        SkippedOrder(coin="ETH", notional=Decimal("0"), reason="ro_capped_out")
    ]


# ── 部位 map 讀不到該幣 → 維持現行 reduce_only_no_pos 跳過（回歸）────
def test_ro_without_position_entry_still_skipped_regression():
    leader = [_open_order(coin="ETH", is_buy=False, reduce_only=True,
                          sz="0.04", limit_px="2000", oid=1)]
    desired, skipped = _build(leader, {})
    assert desired == []
    assert skipped == [
        SkippedOrder(coin="ETH", notional=Decimal("0"), reason="reduce_only_no_pos")
    ]


# ── 封頂發生在 scale 縮放與捨入之後 ──────────────────────────────────
def test_cap_applies_after_scaling():
    leader = [_open_order(coin="ETH", is_buy=False, reduce_only=True,
                          sz="0.2", limit_px="2000", oid=1)]
    desired, _ = _build(leader, {"ETH": _position("ETH", szi="0.03")}, scale="0.5")
    # 0.2×0.5=0.1 → 封頂到 0.03
    assert [d.sz for d in desired] == [Decimal("0.03")]
