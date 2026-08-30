"""tests/test_closed_pnl_parsing.py — M3 round3 Task 2b：`UserFill.closed_pnl`
加法擴充的解析測試。兩個 adapter（`spark.publicapi.hl.HLGateway`／
`spark.exchange.hyperliquid.HyperliquidAdapter`）各自解析 HL `userFillsByTime`
回應的 `closedPnl` 欄位，語意須同基準（工程原則 1，同 builderFee 慣例）：
有欄位就帶出 `Decimal`；缺欄或 `null` → `None`（**不是** `Decimal("0")`——
「沒資料」與「當筆已實現 0」是兩件事，讀者不可混淆，見 aggregate.py 的
`FollowerSummary.realized_pnl` docstring）。

全離線：兩個 adapter 都用假的 `info`/`post_fn`，不觸網。
"""
from datetime import datetime, timezone
from decimal import Decimal

from spark.exchange.hyperliquid import HyperliquidAdapter
from spark.publicapi.hl import HLGateway


class _FakeInfo:
    def __init__(self, fills):
        self._fills = fills
        self.user_fills_by_time_calls = []

    def user_fills_by_time(self, address, start_time, end_time):
        self.user_fills_by_time_calls.append((address, start_time, end_time))
        return self._fills


class _FakePost:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, url, body):
        self.calls.append((url, body))
        return self.results.pop(0)


_START = datetime(2025, 6, 15, tzinfo=timezone.utc)
_END = datetime(2025, 6, 16, tzinfo=timezone.utc)


# ── HyperliquidAdapter（引擎側；沿用 test_hyperliquid_reads.py 的 FakeInfo 手法）──

def test_hyperliquid_adapter_parses_closed_pnl():
    raw = [{"coin": "ETH", "px": "4000.25", "sz": "0.1", "side": "B",
           "time": 1750000000000, "crossed": True, "oid": 111, "fee": "0.008",
           "closedPnl": "12.5"}]
    ad = HyperliquidAdapter(network="testnet", info=_FakeInfo(raw))
    f = ad.get_user_fills("0xuser", _START, _END)[0]
    assert f.closed_pnl == Decimal("12.5")
    assert isinstance(f.closed_pnl, Decimal)


def test_hyperliquid_adapter_missing_closed_pnl_is_none_not_zero():
    raw = [{"coin": "ETH", "px": "4000.25", "sz": "0.1", "side": "B",
           "time": 1750000000000, "crossed": True, "oid": 111, "fee": "0.008"}]
    ad = HyperliquidAdapter(network="testnet", info=_FakeInfo(raw))
    f = ad.get_user_fills("0xuser", _START, _END)[0]
    assert f.closed_pnl is None


def test_hyperliquid_adapter_null_closed_pnl_is_none():
    raw = [{"coin": "ETH", "px": "4000.25", "sz": "0.1", "side": "B",
           "time": 1750000000000, "crossed": True, "oid": 111, "fee": "0.008",
           "closedPnl": None}]
    ad = HyperliquidAdapter(network="testnet", info=_FakeInfo(raw))
    f = ad.get_user_fills("0xuser", _START, _END)[0]
    assert f.closed_pnl is None


def test_hyperliquid_adapter_empty_string_closed_pnl_is_zero_not_error():
    """R-A（2026-08-30 opus 審查 C5）：空字串是「四態」之一（值／缺欄／null／
    空字串）——`f.get("closedPnl") is not None` 只擋 None，擋不住 `""`。
    修法前 `Decimal("")` 會拋 `InvalidOperation`，直接炸掉呼叫端（含
    costbreaker 取數路徑）。"""
    raw = [{"coin": "ETH", "px": "4000.25", "sz": "0.1", "side": "B",
           "time": 1750000000000, "crossed": True, "oid": 111, "fee": "0.008",
           "closedPnl": ""}]
    ad = HyperliquidAdapter(network="testnet", info=_FakeInfo(raw))
    f = ad.get_user_fills("0xuser", _START, _END)[0]
    assert f.closed_pnl == Decimal("0")
    assert f.closed_pnl is not None


def test_hyperliquid_adapter_zero_closed_pnl_is_decimal_zero_not_none():
    """`closedPnl` 明確給 "0"（例如開倉成交）→ `Decimal("0")`，與「缺資料」
    （None）不同一個值。"""
    raw = [{"coin": "ETH", "px": "4000.25", "sz": "0.1", "side": "B",
           "time": 1750000000000, "crossed": True, "oid": 111, "fee": "0.008",
           "closedPnl": "0"}]
    ad = HyperliquidAdapter(network="testnet", info=_FakeInfo(raw))
    f = ad.get_user_fills("0xuser", _START, _END)[0]
    assert f.closed_pnl == Decimal("0")
    assert f.closed_pnl is not None


# ── HLGateway（API 層；沿用 test_publicapi_hl.py 的 _FakePost 手法）──────────

def test_hl_gateway_parses_closed_pnl():
    raw = [{"time": 1_700_000_000_000, "coin": "ETH", "px": "2500.5", "sz": "0.4",
           "side": "B", "crossed": True, "oid": 1, "fee": "0.12", "builderFee": "0.03",
           "closedPnl": "217.36"}]
    post = _FakePost([raw])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    f = gw.get_user_fills("0x" + "ab" * 20, _START, _END)[0]
    assert f.closed_pnl == Decimal("217.36")


def test_hl_gateway_missing_closed_pnl_is_none():
    raw = [{"time": 1_700_000_000_000, "coin": "ETH", "px": "2500.5", "sz": "0.4",
           "side": "B", "crossed": True, "oid": 1}]
    post = _FakePost([raw])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    f = gw.get_user_fills("0x" + "ab" * 20, _START, _END)[0]
    assert f.closed_pnl is None


def test_hl_gateway_null_closed_pnl_is_none():
    raw = [{"time": 1_700_000_000_000, "coin": "ETH", "px": "2500.5", "sz": "0.4",
           "side": "B", "crossed": True, "oid": 1, "closedPnl": None}]
    post = _FakePost([raw])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    f = gw.get_user_fills("0x" + "ab" * 20, _START, _END)[0]
    assert f.closed_pnl is None


def test_hl_gateway_empty_string_closed_pnl_is_zero_not_error():
    """R-A（2026-08-30 opus 審查 C5）：`get_user_fills`（`collect_follower_summary`
    的資料源）對空字串 `closedPnl` 補 `or "0"` 護欄——同一份 `_parse_fill` 也
    餵 `get_user_fills_paged`（見 tests/test_publicapi_hl.py），兩者同一基準。"""
    raw = [{"time": 1_700_000_000_000, "coin": "ETH", "px": "2500.5", "sz": "0.4",
           "side": "B", "crossed": True, "oid": 1, "closedPnl": ""}]
    post = _FakePost([raw])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    f = gw.get_user_fills("0x" + "ab" * 20, _START, _END)[0]
    assert f.closed_pnl == Decimal("0")
    assert f.closed_pnl is not None
