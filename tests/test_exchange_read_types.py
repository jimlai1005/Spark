"""讀側型別（OpenOrder/Position/AccountSnapshot/EquityView/UserFill）與 ExchangeAdapter
新增抽象讀取方法。frozen dataclass、全 Decimal 欄位；FakeAdapter 需回傳注入值。"""
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal
import pytest
from spark.exchange.base import (
    OpenOrder, Position, AccountSnapshot, EquityView, UserFill, ExchangeAdapter,
)
from spark.exchange.fakes import FakeAdapter


def test_open_order_frozen_decimal():
    o = OpenOrder(oid=1, coin="ETH", is_buy=True, limit_px=Decimal("2000"),
                  sz=Decimal("0.5"), reduce_only=False, is_trigger=False,
                  trigger_px=None, tpsl=None)
    with pytest.raises(Exception):
        o.oid = 2
    assert isinstance(o.limit_px, Decimal)


def test_position_frozen_decimal_and_signed_szi():
    p = Position(coin="ETH", szi=Decimal("-0.5"), entry_px=Decimal("2000"),
                 leverage=5, is_cross=True, unrealized_pnl=Decimal("-10"),
                 margin_used=Decimal("200"))
    with pytest.raises(FrozenInstanceError):
        p.szi = Decimal("1")
    assert isinstance(p.szi, Decimal)
    assert p.szi < 0  # 空單為負


def test_account_snapshot_frozen_decimal():
    a = AccountSnapshot(account_value=Decimal("1000"), total_margin_used=Decimal("200"),
                         withdrawable=Decimal("800"), total_ntl_pos=Decimal("500"))
    with pytest.raises(FrozenInstanceError):
        a.account_value = Decimal("0")
    assert isinstance(a.withdrawable, Decimal)


def test_equity_view_same_source_pair():
    ev = EquityView(current=Decimal("990"), recent_peak=Decimal("1000"))
    assert ev.recent_peak >= ev.current
    with pytest.raises(FrozenInstanceError):
        ev.current = Decimal("0")


def test_user_fill_frozen_decimal():
    f = UserFill(time=datetime(2026, 6, 18, tzinfo=timezone.utc), coin="ETH",
                 px=Decimal("4000"), sz=Decimal("0.01"), side="B", crossed=True,
                 oid=42, fee=Decimal("0.008"))
    with pytest.raises(FrozenInstanceError):
        f.fee = Decimal("0")
    assert isinstance(f.fee, Decimal)


def test_abc_has_read_methods_and_still_no_withdraw():
    for m in ("get_open_orders", "get_positions", "get_account_state",
              "get_equity_view", "get_user_fills", "get_all_mids",
              "get_size_decimals"):
        assert getattr(ExchangeAdapter, m).__isabstractmethod__
    assert not hasattr(ExchangeAdapter, "withdraw")
    assert not hasattr(ExchangeAdapter, "transfer")


# --- FakeAdapter 讀側：回傳注入值 + 沿既有記錄風格記錄呼叫 ---

def test_fake_adapter_defaults_dont_break_existing_construction():
    # 既有測試以 FakeAdapter(account_value=..., seeded_fills=..., account_values=...) 建構，
    # 新參數必須全部有預設值，不得動到既有呼叫點。
    fa = FakeAdapter(account_value=Decimal("100"))
    assert fa.get_open_orders("0xuser") == []
    assert fa.get_positions("0xuser") == []
    assert fa.get_account_state("0xuser") == AccountSnapshot(
        account_value=Decimal("0"), total_margin_used=Decimal("0"),
        withdrawable=Decimal("0"), total_ntl_pos=Decimal("0"))
    assert fa.get_equity_view("0xuser") == EquityView(current=Decimal("0"), recent_peak=Decimal("0"))
    assert fa.get_user_fills("0xuser", datetime(2026, 1, 1), datetime(2026, 1, 2)) == []
    assert fa.get_all_mids() == {}
    assert fa.get_size_decimals("ETH") == 4


def test_fake_adapter_returns_injected_open_orders_and_records_call():
    oo = OpenOrder(oid=1, coin="ETH", is_buy=True, limit_px=Decimal("2000"),
                   sz=Decimal("0.5"), reduce_only=False, is_trigger=False,
                   trigger_px=None, tpsl=None)
    fa = FakeAdapter(open_orders=[oo])
    assert fa.get_open_orders("0xuser") == [oo]
    assert fa.calls["get_open_orders"] == [{"address": "0xuser"}]


def test_fake_adapter_returns_injected_positions():
    pos = Position(coin="ETH", szi=Decimal("1"), entry_px=Decimal("2000"), leverage=3,
                   is_cross=True, unrealized_pnl=Decimal("5"), margin_used=Decimal("100"))
    fa = FakeAdapter(positions=[pos])
    assert fa.get_positions("0xuser") == [pos]
    assert fa.calls["get_positions"] == [{"address": "0xuser"}]


def test_fake_adapter_returns_injected_account_and_equity():
    acc = AccountSnapshot(account_value=Decimal("1000"), total_margin_used=Decimal("200"),
                          withdrawable=Decimal("800"), total_ntl_pos=Decimal("500"))
    ev = EquityView(current=Decimal("990"), recent_peak=Decimal("1000"))
    fa = FakeAdapter(account=acc, equity=ev)
    assert fa.get_account_state("0xuser") == acc
    assert fa.get_equity_view("0xuser") == ev
    assert fa.calls["get_account_state"] == [{"address": "0xuser"}]
    assert fa.calls["get_equity_view"] == [{"address": "0xuser"}]


def test_fake_adapter_returns_injected_fills_and_records_range():
    fill = UserFill(time=datetime(2026, 6, 18, tzinfo=timezone.utc), coin="ETH",
                    px=Decimal("4000"), sz=Decimal("0.01"), side="B", crossed=True,
                    oid=42, fee=Decimal("0.008"))
    fa = FakeAdapter(fills=[fill])
    start = datetime(2026, 6, 18, tzinfo=timezone.utc)
    end = datetime(2026, 6, 19, tzinfo=timezone.utc)
    assert fa.get_user_fills("0xuser", start, end) == [fill]
    assert fa.calls["get_user_fills"] == [{"address": "0xuser", "start": start, "end": end}]


def test_fake_adapter_returns_injected_mids_and_size_decimals():
    fa = FakeAdapter(mids={"ETH": Decimal("2000")}, sz_decimals={"ETH": 4})
    assert fa.get_all_mids() == {"ETH": Decimal("2000")}
    assert fa.get_size_decimals("ETH") == 4
    assert fa.get_size_decimals("BTC") == 4  # 未知幣種回退預設 4
