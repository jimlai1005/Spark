from decimal import Decimal
from datetime import date, datetime
from spark.exchange.base import AccountSnapshot, AccountTotal, Order, BuilderCode, Fill, ExchangeAdapter
from spark.exchange.fakes import FakeAdapter


def test_fake_records_approve_and_flips_max_fee():
    fake = FakeAdapter(account_value=Decimal("150"))
    assert fake.query_max_builder_fee("0xuser", "0xbuilder") == 0
    fake.approve_builder_fee(main_signer="MAIN", builder="0xbuilder", max_rate="0.1%")
    assert fake.query_max_builder_fee("0xuser", "0xbuilder") == 100
    assert fake.calls["approve_builder_fee"][0]["main_signer"] == "MAIN"


def test_fake_place_order_accrues_builder_fee():
    fake = FakeAdapter(account_value=Decimal("150"))
    res = fake.place_order("AGENT", Order("ETH", True, Decimal("0.01"), Decimal("4000"), "Ioc"),
                           BuilderCode(b="0xbuilder", f=20))
    assert res.ok and res.filled_size == Decimal("0.01")
    # notional 40 * f(20)/100000 = 0.008
    assert fake.query_builder_accrued("0xbuilder") == Decimal("0.008")


def test_fake_fetch_fills_returns_seeded():
    fill = Fill(datetime(2026, 6, 18), "ETH", Decimal("4000"), Decimal("0.01"), "B", Decimal("0.008"))
    fake = FakeAdapter(account_value=Decimal("150"), seeded_fills=[fill])
    assert fake.fetch_builder_fills("0xbuilder", date(2026, 6, 18)) == [fill]


def _snap(av: str) -> AccountSnapshot:
    return AccountSnapshot(account_value=Decimal(av), total_margin_used=Decimal("0"),
                           withdrawable=Decimal("0"), total_ntl_pos=Decimal("0"))


def test_fake_account_totals_hit_and_case_insensitive():
    tot = AccountTotal(value=Decimal("707.37"), as_of_ms=1_000)
    fake = FakeAdapter(account_totals={"0xAbC": tot})
    assert fake.get_account_total("0xabc") == tot
    assert fake.get_account_total("0xABC") == tot


def test_fake_account_totals_miss_raises_value_error():
    import pytest
    fake = FakeAdapter(account_totals={"0xabc": AccountTotal(Decimal("1"), 1)})
    with pytest.raises(ValueError):
        fake.get_account_total("0xother")
    with pytest.raises(ValueError):
        FakeAdapter().get_account_total("0xabc")  # 預設沒有 account_totals


def test_fake_account_states_per_address_override_with_fallback():
    default = _snap("4704.22")
    leader = _snap("120.58")
    fake = FakeAdapter(account=default, account_states={"0xLeader": leader})
    assert fake.get_account_state("0xleader") == leader
    assert fake.get_account_state("0xLEADER") == leader
    assert fake.get_account_state("0xfollower") == default  # 沒有該位址 → 原本單一 _account


def test_exchange_adapter_default_get_account_total_raises_not_implemented():
    # 非 abstract：只實作 abstract 方法的替身不被迫實作；呼叫時預設 raise（不回預設值）
    assert "get_account_total" not in ExchangeAdapter.__abstractmethods__
    import pytest
    with pytest.raises(NotImplementedError):
        ExchangeAdapter.get_account_total(object(), "0xabc")
