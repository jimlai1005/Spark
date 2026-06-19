from decimal import Decimal
from datetime import date, datetime
from spark.exchange.base import Order, BuilderCode, Fill
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
    assert fake.query_builder_accrued("0xbuilder") > 0


def test_fake_fetch_fills_returns_seeded():
    fill = Fill(datetime(2026, 6, 18), "ETH", Decimal("4000"), Decimal("0.01"), "B", Decimal("0.008"))
    fake = FakeAdapter(account_value=Decimal("150"), seeded_fills=[fill])
    assert fake.fetch_builder_fills("0xbuilder", date(2026, 6, 18)) == [fill]
