from decimal import Decimal
import pytest
from spark.config import Settings, API_URLS, CSV_BASE_URLS


def test_settings_defaults_phase1():
    s = Settings(builder_address="0xbuilder", account_id="testacct", network="testnet")
    assert s.f == 20
    assert s.max_rate == "0.1%"
    assert s.coin == "ETH"
    assert isinstance(s.order_size, Decimal)


def test_network_switches_urls():
    assert API_URLS["testnet"] != API_URLS["mainnet"]
    assert "Testnet" in CSV_BASE_URLS["testnet"]
    assert "Mainnet" in CSV_BASE_URLS["mainnet"]


def test_rejects_unknown_network():
    with pytest.raises(ValueError):
        Settings(builder_address="0xb", account_id="a", network="devnet")


def test_fee_validated_against_cap():
    with pytest.raises(ValueError):
        Settings(builder_address="0xb", account_id="a", network="testnet", f=200)


def test_url_properties_resolve_by_network():
    assert Settings(builder_address="0xb", account_id="a",
                    network="mainnet").api_url == "https://api.hyperliquid.xyz"
    assert "Testnet" in Settings(builder_address="0xb", account_id="a",
                                 network="testnet").csv_base_url


def test_charged_fee_must_not_exceed_approved_ceiling():
    # 預設 f=20 (0.02%) <= max_rate 0.1% → ok
    Settings(builder_address="0xb", account_id="a", network="testnet")
    # f=50 (0.05%) 但只授權 0.02% → 應 raise
    with pytest.raises(ValueError):
        Settings(builder_address="0xb", account_id="a", network="testnet",
                 f=50, max_rate="0.02%")
