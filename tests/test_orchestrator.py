from decimal import Decimal
from spark.exchange.fakes import FakeAdapter
from spark.config import Settings
from spark.orchestrator import place_marketable_order


def _settings():
    return Settings(builder_address="0xbuilder", account_id="acct1", network="testnet")


def test_places_ioc_order_with_builder_code():
    fake = FakeAdapter(account_value=Decimal("150"))
    res = place_marketable_order(fake, _settings(), agent_signer="AGENT",
                                 is_buy=True, best_opposite_px=Decimal("4000"))
    assert res.ok
    call = fake.calls["place_order"][0]
    assert call["order"].tif == "Ioc"
    assert call["builder"].b == "0xbuilder"
    assert call["builder"].f == 20


def test_buy_price_crosses_above_opposite():
    fake = FakeAdapter(account_value=Decimal("150"))
    place_marketable_order(fake, _settings(), agent_signer="AGENT",
                           is_buy=True, best_opposite_px=Decimal("4000"))
    order = fake.calls["place_order"][0]["order"]
    assert order.limit_px > Decimal("4000")  # 買單抬高以穿過賣一


def test_sell_price_crosses_below_opposite():
    fake = FakeAdapter(account_value=Decimal("150"))
    place_marketable_order(fake, _settings(), agent_signer="AGENT",
                           is_buy=False, best_opposite_px=Decimal("4000"))
    order = fake.calls["place_order"][0]["order"]
    assert order.limit_px < Decimal("4000")  # 賣單壓低以穿過買一
