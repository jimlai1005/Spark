from datetime import date
from decimal import Decimal
from spark.exchange.base import Order, BuilderCode
from spark.exchange.hyperliquid import HyperliquidAdapter


class FakeInfo:
    def __init__(self):
        self.posts = []
    def user_state(self, address):
        return {"marginSummary": {"accountValue": "150.5"}}
    def post(self, url_path, payload=None):
        self.posts.append((url_path, payload))
        assert payload["type"] == "maxBuilderFee"
        return 100
    def query_referral_state(self, address):
        return {"builderRewards": "0.008"}


class FakeExchange:
    def __init__(self):
        self.calls = []
    def approve_builder_fee(self, builder, max_fee_rate):
        self.calls.append(("approve_builder_fee", builder, max_fee_rate))
        return {"status": "ok"}
    def approve_agent(self, name=None):
        self.calls.append(("approve_agent", name))
        return ({"status": "ok"}, "0xagentkey")
    def order(self, coin, is_buy, sz, limit_px, order_type, reduce_only=False, builder=None):
        self.calls.append(("order", coin, is_buy, sz, limit_px, order_type, builder))
        return {"status": "ok", "response": {"data": {"statuses": [
            {"filled": {"totalSz": str(sz), "avgPx": str(limit_px)}}]}}}


def _adapter():
    return HyperliquidAdapter(network="testnet", info=FakeInfo(), exchange=FakeExchange())


def test_get_account_value_parses_margin_summary():
    assert _adapter().get_account_value("0xuser") == Decimal("150.5")


def test_query_max_builder_fee_via_raw_post():
    ad = _adapter()
    assert ad.query_max_builder_fee("0xuser", "0xbuilder") == 100
    url_path, payload = ad._info.posts[-1]
    assert url_path == "/info"
    assert payload == {"type": "maxBuilderFee", "user": "0xuser", "builder": "0xbuilder"}


def test_query_builder_accrued_from_referral_state():
    assert _adapter().query_builder_accrued("0xbuilder") == Decimal("0.008")


def test_place_order_passes_builder_dict_and_ioc():
    ad = _adapter()
    ad.place_order(agent_signer=None,
                   order=Order("ETH", True, Decimal("0.01"), Decimal("4000"), "Ioc"),
                   builder=BuilderCode(b="0xbuilder", f=20))
    name, coin, is_buy, sz, px, otype, builder = ad._exchange.calls[-1]
    assert otype == {"limit": {"tif": "Ioc"}}
    assert builder == {"b": "0xbuilder", "f": 20}


def test_round_px_to_5_sig_figs():
    ad = _adapter()
    assert ad._round_px(Decimal("3530.9274")) == 3530.9
    assert ad._round_px(Decimal("4000")) == 4000.0


def test_place_order_rejected_returns_not_ok():
    class RejectingExchange(FakeExchange):
        def order(self, *a, **k):
            return {"status": "err", "response": "Insufficient margin"}
    ad = HyperliquidAdapter(network="testnet", info=FakeInfo(), exchange=RejectingExchange())
    res = ad.place_order(agent_signer=None,
                         order=Order("ETH", True, Decimal("0.01"), Decimal("4000"), "Ioc"),
                         builder=BuilderCode(b="0xbuilder", f=20))
    assert res.ok is False
    assert res.filled_size == Decimal("0")
    assert res.raw == {"status": "err", "response": "Insufficient margin"}


def test_parse_order_response_accepts_verified_ok_sentinel():
    """resilience 邊界的 VERIFIED_OK（連線中斷但 verify 確認已送達）是第三種成功終態：
    無 statuses 可挖，必須短路成 ok=True、成交明細為 0——否則 KeyError。"""
    from spark.resilience import VERIFIED_OK
    res = _adapter()._parse_order_response(dict(VERIFIED_OK))
    assert res.ok is True
    assert res.filled_size == Decimal("0")
    assert res.avg_px == Decimal("0")
    assert res.raw == VERIFIED_OK


def test_place_order_treats_verified_ok_sentinel_as_success():
    """走完整 place_order 路徑：內層 exchange 回 VERIFIED_OK（模擬 ResilientExchange
    verify-then-skip-resend 的回傳）→ OrderResult.ok=True 而非 KeyError。"""
    from spark.resilience import VERIFIED_OK

    class SentinelExchange(FakeExchange):
        def order(self, *a, **k):
            return dict(VERIFIED_OK)

    ad = HyperliquidAdapter(network="testnet", info=FakeInfo(), exchange=SentinelExchange())
    res = ad.place_order(agent_signer=None,
                         order=Order("ETH", True, Decimal("0.01"), Decimal("4000"), "Ioc"),
                         builder=BuilderCode(b="0xbuilder", f=20))
    assert res.ok is True
    assert res.filled_size == Decimal("0")
    assert res.raw.get("_resilience") == "verified"


def test_approve_agent_returns_generated_key_and_never_reprs_it():
    ad = _adapter()
    res = ad.approve_agent(main_signer=None, agent_name="spark-agent")
    assert res.ok is True
    assert res.agent_key == "0xagentkey"
    assert "0xagentkey" not in repr(res)  # repr=False：key 不得出現在 repr/log


def test_fetch_builder_fills_empty_on_404(monkeypatch):
    import urllib.error
    def raise_404(url, timeout=30):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    monkeypatch.setattr("spark.exchange.hyperliquid.urllib.request.urlopen", raise_404)
    ad = _adapter()
    assert ad.fetch_builder_fills("0xbuilder", date(2026, 6, 18)) == []


def test_fetch_builder_fills_lowercases_address_in_url(monkeypatch):
    import urllib.error
    captured = {}
    def fake_urlopen(url, timeout=30):
        captured["url"] = url
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    monkeypatch.setattr("spark.exchange.hyperliquid.urllib.request.urlopen", fake_urlopen)
    ad = _adapter()
    ad.fetch_builder_fills("0xABCdef", date(2026, 6, 18))
    assert "/0xabcdef/" in captured["url"]
