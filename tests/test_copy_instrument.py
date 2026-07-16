"""copytrade/instrument.py characterization 測試。_extract_order_error 案例 port 自
hl-copytrader tests/test_order_error_parse.py；其餘為新增的純函式行為驗證。"""
from decimal import Decimal

from spark.copytrade.instrument import (
    _coin_dex,
    _extract_order_error,
    _is_spot_coin,
    _order_type_and_px,
    _round_size,
)
from spark.copytrade.orders import OrderSpec


class TestIsSpotCoin:
    def test_at_prefix_index_coin_is_spot(self):
        assert _is_spot_coin("@85") is True

    def test_slash_pair_is_spot(self):
        assert _is_spot_coin("PURR/USDC") is True

    def test_plain_perp_is_not_spot(self):
        assert _is_spot_coin("BTC") is False

    def test_dex_prefixed_perp_is_not_spot(self):
        assert _is_spot_coin("xyz:NVDA") is False


class TestCoinDex:
    def test_default_dex_is_empty_string(self):
        assert _coin_dex("BTC") == ""

    def test_named_dex_prefix_extracted(self):
        assert _coin_dex("xyz:NVDA") == "xyz"


class TestRoundSize:
    """捨入方向釘死：hl 用 math.floor(size*factor)/factor，等同「無條件捨去」而非四捨五入。"""

    def test_truncates_down_not_round_half_up(self):
        # 1.239 在 2 位精度下，四捨五入會給 1.24；無條件捨去給 1.23 —— 用此值鎖定捨入方向。
        assert _round_size(Decimal("1.239"), 2) == Decimal("1.23")

    def test_exact_value_at_precision_unchanged(self):
        assert _round_size(Decimal("2.50"), 2) == Decimal("2.50")

    def test_zero_decimals_truncates_to_integer(self):
        assert _round_size(Decimal("7.9"), 0) == Decimal("7")


class TestOrderTypeAndPx:
    def test_limit_order_uses_limit_px_and_default_gtc(self):
        spec = OrderSpec(coin="BTC", is_buy=True, sz=Decimal("1"), limit_px=Decimal("50000"))
        px, order_type = _order_type_and_px(spec)
        assert px == Decimal("50000")
        assert order_type == {"limit": {"tif": "Gtc"}}

    def test_limit_order_alo_tif_passes_through(self):
        # 1:1 hl instrument.py:46——tif 取自 spec，post-only（Alo）不得被壓成 Gtc。
        spec = OrderSpec(
            coin="BTC", is_buy=True, sz=Decimal("1"), limit_px=Decimal("50000"), tif="Alo"
        )
        _px, order_type = _order_type_and_px(spec)
        assert order_type == {"limit": {"tif": "Alo"}}

    def test_trigger_limit_order_uses_limit_px_as_px(self):
        spec = OrderSpec(
            coin="ETH", is_buy=False, sz=Decimal("2"), limit_px=Decimal("2990"),
            is_trigger=True, tpsl="tp", trigger_px=Decimal("3000"), is_market=False,
        )
        px, order_type = _order_type_and_px(spec)
        assert px == Decimal("2990")
        assert order_type == {
            "trigger": {"triggerPx": Decimal("3000"), "isMarket": False, "tpsl": "tp"}
        }

    def test_trigger_market_order_falls_back_to_trigger_px_when_no_limit(self):
        spec = OrderSpec(
            coin="ETH", is_buy=False, sz=Decimal("2"), limit_px=Decimal("0"),
            is_trigger=True, tpsl="sl", trigger_px=Decimal("2900"), is_market=True,
        )
        px, order_type = _order_type_and_px(spec)
        assert px == Decimal("2900")
        assert order_type["trigger"]["isMarket"] is True

    def test_trigger_tpsl_none_defaults_to_sl(self):
        spec = OrderSpec(
            coin="ETH", is_buy=False, sz=Decimal("2"), limit_px=Decimal("2900"),
            is_trigger=True, tpsl=None, trigger_px=Decimal("2900"),
        )
        _px, order_type = _order_type_and_px(spec)
        assert order_type["trigger"]["tpsl"] == "sl"


class TestExtractOrderError:
    """port 自 hl-copytrader tests/test_order_error_parse.py（雙形態解析案例）。"""

    RATE_LIMIT = (
        "Too many cumulative requests sent (43645 > 40140) for cumulative "
        "volume traded $30141.76. Place taker orders to free up 1 request per USDC traded."
    )

    def test_top_level_err_string_response(self):
        # 頂層錯誤（限流/簽名等）：response 直接是字串，不是 dict——過去對它 .get() 會 crash。
        r = {"status": "err", "response": self.RATE_LIMIT}
        assert _extract_order_error(r) == self.RATE_LIMIT

    def test_order_level_error_in_statuses(self):
        r = {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {"statuses": [{"error": "Insufficient margin"}]},
            },
        }
        assert _extract_order_error(r) == "Insufficient margin"

    def test_success_no_error_returns_empty_string(self):
        r = {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}}
        assert _extract_order_error(r) == ""

    def test_non_dict_or_missing_response_returns_empty_string(self):
        assert _extract_order_error(None) == ""
        assert _extract_order_error("weird") == ""
        assert _extract_order_error({"status": "ok"}) == ""
