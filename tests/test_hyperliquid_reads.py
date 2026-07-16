"""HyperliquidAdapter 讀側（Task 2）：七個方法的映射測試。
FakeInfo 手法沿用 tests/test_hyperliquid_adapter.py；餵真實形狀（欄位名/缺鍵行為經
SDK hyperliquid/info.py docstring 與 Hyperliquid 官方 frontendOpenOrders 文件交叉確認，
frontendOpenOrders 回應無字面 "tpsl" 鍵——tpsl 由 orderType 文字判讀衍生，
語意移植自 hl-copytrader src/monitor.py:181-191 的 _parse_orders 邏輯）。"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from spark.exchange.hyperliquid import HyperliquidAdapter


class FakeInfo:
    """七個讀側方法各自需要的假回應；呼叫次數計數供快取/同源測試使用。"""

    def __init__(self, frontend_orders=None, user_state_resp=None, portfolio_resp=None,
                 fills=None, mids=None, meta_resp=None):
        self._frontend_orders = frontend_orders if frontend_orders is not None else []
        self._user_state_resp = user_state_resp
        self._portfolio_resp = portfolio_resp if portfolio_resp is not None else []
        self._fills = fills if fills is not None else []
        self._mids = mids if mids is not None else {}
        self._meta_resp = meta_resp if meta_resp is not None else {"universe": []}
        self.portfolio_calls = 0
        self.meta_calls = 0
        self.user_fills_by_time_calls = []

    def frontend_open_orders(self, address):
        return self._frontend_orders

    def user_state(self, address):
        return self._user_state_resp

    def portfolio(self, address):
        self.portfolio_calls += 1
        return self._portfolio_resp

    def user_fills_by_time(self, address, start_time, end_time):
        self.user_fills_by_time_calls.append((address, start_time, end_time))
        return self._fills

    def all_mids(self):
        return self._mids

    def meta(self):
        self.meta_calls += 1
        return self._meta_resp


def _adapter(**fake_kwargs):
    return HyperliquidAdapter(network="testnet", info=FakeInfo(**fake_kwargs))


# --- 1. get_open_orders ---

def test_get_open_orders_maps_limit_order():
    orders_raw = [{
        "coin": "ETH", "oid": 123, "side": "B", "limitPx": "4000.5", "sz": "0.3",
        "reduceOnly": False, "isTrigger": False, "orderType": "Limit",
        "triggerPx": "0.0", "tif": "Gtc", "timestamp": 1750000000000,
        "triggerCondition": "N/A", "origSz": "0.3", "isPositionTpsl": False,
    }]
    ad = _adapter(frontend_orders=orders_raw)
    result = ad.get_open_orders("0xuser")
    assert len(result) == 1
    o = result[0]
    assert o.oid == 123
    assert o.coin == "ETH"
    assert o.is_buy is True
    assert o.limit_px == Decimal("4000.5")
    assert isinstance(o.limit_px, Decimal)
    assert o.sz == Decimal("0.3")
    assert o.reduce_only is False
    assert o.is_trigger is False
    assert o.trigger_px is None  # 非 trigger 一律映射 None，即便 raw 是 "0.0"
    assert o.tpsl is None


def test_get_open_orders_trigger_stop_loss_missing_reduce_only_defaults_false():
    orders_raw = [{
        "coin": "BTC", "oid": 456, "side": "A", "limitPx": "60000", "sz": "0.01",
        "isTrigger": True, "orderType": "Stop Market", "triggerPx": "59000",
        "triggerCondition": "Mark Price <= Trigger", "timestamp": 1750000001000,
        "origSz": "0.01", "isPositionTpsl": True,
        # reduceOnly 缺鍵 → 應預設 False
    }]
    ad = _adapter(frontend_orders=orders_raw)
    o = ad.get_open_orders("0xuser")[0]
    assert o.reduce_only is False
    assert o.is_trigger is True
    assert o.trigger_px == Decimal("59000")
    assert isinstance(o.trigger_px, Decimal)
    assert o.tpsl == "sl"
    assert o.is_buy is False  # side "A" = 賣


def test_get_open_orders_trigger_take_profit_tpsl_tp():
    orders_raw = [{
        "coin": "BTC", "oid": 789, "side": "A", "limitPx": "65000", "sz": "0.01",
        "isTrigger": True, "reduceOnly": True, "orderType": "Take Profit Limit",
        "triggerPx": "64000", "timestamp": 1750000002000,
    }]
    ad = _adapter(frontend_orders=orders_raw)
    o = ad.get_open_orders("0xuser")[0]
    assert o.tpsl == "tp"
    assert o.reduce_only is True


def test_tpsl_faithful_port_covers_tp_prefix_and_sl_substring():
    # 1:1 移植 hl-copytrader monitor.py:187-191 的完整析取項：
    # "tp..." 開頭 → tp；含 "sl" → sl（即便不含 "stop"/"take profit" 全稱）。
    assert HyperliquidAdapter._tpsl_from_order_type("TP Market") == "tp"
    assert HyperliquidAdapter._tpsl_from_order_type("SL Limit") == "sl"
    assert HyperliquidAdapter._tpsl_from_order_type("Take Profit Market") == "tp"
    assert HyperliquidAdapter._tpsl_from_order_type("Stop Limit") == "sl"
    assert HyperliquidAdapter._tpsl_from_order_type("Limit") is None


# --- 2. get_positions ---

_USER_STATE = {
    "assetPositions": [
        {"position": {
            "coin": "ETH", "szi": "1.5", "entryPx": "3000",
            "leverage": {"type": "cross", "value": 5},
            "unrealizedPnl": "150.25", "marginUsed": "900",
        }, "type": "oneWay"},
        {"position": {
            "coin": "DOGE", "szi": "0", "entryPx": None,
            "leverage": {"type": "isolated", "value": 2},
            "unrealizedPnl": "0", "marginUsed": "0",
        }, "type": "oneWay"},  # szi == 0 → 應被跳過
        {"position": {
            "coin": "SOL", "szi": "-10", "entryPx": None,
            "leverage": {"type": "isolated", "value": 3},
            "unrealizedPnl": "-5", "marginUsed": "20",
        }, "type": "oneWay"},  # entryPx None → 映射 Decimal("0")
    ],
    "marginSummary": {
        "accountValue": "5000", "totalMarginUsed": "920", "totalNtlPos": "4500",
    },
    "withdrawable": "4080",
}


def test_get_positions_maps_cross_position_and_skips_zero_size():
    ad = _adapter(user_state_resp=_USER_STATE)
    positions = ad.get_positions("0xuser")
    coins = [p.coin for p in positions]
    assert "DOGE" not in coins  # szi == 0 跳過
    assert len(positions) == 2
    eth = next(p for p in positions if p.coin == "ETH")
    assert eth.szi == Decimal("1.5")
    assert isinstance(eth.szi, Decimal)
    assert eth.entry_px == Decimal("3000")
    assert eth.leverage == 5
    assert eth.is_cross is True
    assert eth.unrealized_pnl == Decimal("150.25")
    assert eth.margin_used == Decimal("900")


def test_get_positions_none_entry_px_maps_to_zero_and_isolated_not_cross():
    ad = _adapter(user_state_resp=_USER_STATE)
    sol = next(p for p in ad.get_positions("0xuser") if p.coin == "SOL")
    assert sol.entry_px == Decimal("0")
    assert isinstance(sol.entry_px, Decimal)
    assert sol.is_cross is False
    assert sol.szi == Decimal("-10")  # 空單為負


# --- 3. get_account_state ---

def test_get_account_state_maps_margin_summary_and_withdrawable():
    ad = _adapter(user_state_resp=_USER_STATE)
    snap = ad.get_account_state("0xuser")
    assert snap.account_value == Decimal("5000")
    assert snap.total_margin_used == Decimal("920")
    assert snap.withdrawable == Decimal("4080")
    assert snap.total_ntl_pos == Decimal("4500")
    assert isinstance(snap.account_value, Decimal)


def test_get_account_state_zero_positions_still_reads_margin_summary():
    empty_state = {
        "assetPositions": [],
        "marginSummary": {"accountValue": "0", "totalMarginUsed": "0", "totalNtlPos": "0"},
        "withdrawable": "0",
    }
    ad = _adapter(user_state_resp=empty_state)
    snap = ad.get_account_state("0xuser")
    assert snap.account_value == Decimal("0")
    assert snap.withdrawable == Decimal("0")


# --- 4. get_equity_view（同源不變量：單一次 portfolio 呼叫） ---

_PORTFOLIO_RESP = [
    ["day", {"accountValueHistory": [[1000, "990"], [2000, "1005.5"]]}],
    ["week", {"accountValueHistory": [[500, "1010"], [1500, "1200"], [1800, "1150"]]}],
    ["month", {"accountValueHistory": [[100, "800"]]}],
    ["allTime", {"accountValueHistory": [[10, "500"]]}],
]


def test_get_equity_view_current_from_latest_ts_peak_from_week_single_call():
    ad = _adapter(portfolio_resp=_PORTFOLIO_RESP)
    ev = ad.get_equity_view("0xuser")
    # current = 全部時間窗中時間戳最新的一點（day 的 ts=2000）
    assert ev.current == Decimal("1005.5")
    # recent_peak = 僅 "week" 時間窗序列的最大值（hl-copytrader monitor.py:124-158 語意）
    assert ev.recent_peak == Decimal("1200")
    assert isinstance(ev.current, Decimal)
    assert isinstance(ev.recent_peak, Decimal)
    assert ad._info.portfolio_calls == 1  # 同源不變量：current 與 peak 出自單一次呼叫


def test_get_equity_view_no_week_row_peak_falls_back_to_current():
    resp = [
        ["day", {"accountValueHistory": [[100, "700"], [200, "750"]]}],
        ["month", {"accountValueHistory": [[50, "600"]]}],
    ]
    ad = _adapter(portfolio_resp=resp)
    ev = ad.get_equity_view("0xuser")
    assert ev.current == Decimal("750")
    assert ev.recent_peak == Decimal("750")  # 查無 week 資料 → peak 退回 current
    assert ad._info.portfolio_calls == 1


# --- 5. get_user_fills ---

def test_get_user_fills_maps_and_converts_datetime_to_ms():
    fills_raw = [
        {"coin": "ETH", "px": "4000.25", "sz": "0.1", "side": "B", "time": 1750000000000,
         "crossed": True, "oid": 111, "fee": "0.008"},
    ]
    ad = _adapter(fills=fills_raw)
    start = datetime(2025, 6, 15, tzinfo=timezone.utc)
    end = datetime(2025, 6, 16, tzinfo=timezone.utc)
    result = ad.get_user_fills("0xuser", start, end)
    assert len(result) == 1
    f = result[0]
    assert f.coin == "ETH"
    assert f.px == Decimal("4000.25")
    assert isinstance(f.px, Decimal)
    assert f.sz == Decimal("0.1")
    assert f.side == "B"
    assert f.crossed is True
    assert f.oid == 111
    assert f.fee == Decimal("0.008")
    assert f.time == datetime.fromtimestamp(1750000000000 / 1000, tz=timezone.utc)
    # start/end 必須轉為毫秒 epoch（UTC）餵給 SDK
    _address, start_ms, end_ms = ad._info.user_fills_by_time_calls[-1]
    assert start_ms == int(start.timestamp() * 1000)
    assert end_ms == int(end.timestamp() * 1000)


def test_to_ms_utc_integer_exact_with_microseconds():
    # 純整數轉換釘死精確值：2025-06-15 12:34:56.789123 UTC
    # epoch 秒 = 1749990896（date -u 可驗），毫秒 = *1000 + 789（微秒截斷至 ms）。
    dt = datetime(2025, 6, 15, 12, 34, 56, 789123, tzinfo=timezone.utc)
    assert HyperliquidAdapter._to_ms_utc(dt) == 1749990896789
    # naive datetime 視為 UTC，結果相同
    assert HyperliquidAdapter._to_ms_utc(dt.replace(tzinfo=None)) == 1749990896789
    assert isinstance(HyperliquidAdapter._to_ms_utc(dt), int)


def test_get_user_fills_missing_fee_defaults_zero_and_maker_not_crossed():
    fills_raw = [
        {"coin": "BTC", "px": "60000", "sz": "0.01", "side": "A", "time": 1750000005000,
         "crossed": False, "oid": 222},  # fee 缺鍵
    ]
    ad = _adapter(fills=fills_raw)
    f = ad.get_user_fills(
        "0xuser", datetime(2025, 6, 15, tzinfo=timezone.utc), datetime(2025, 6, 16, tzinfo=timezone.utc)
    )[0]
    assert f.fee == Decimal("0")
    assert isinstance(f.fee, Decimal)
    assert f.crossed is False


# --- 6. get_all_mids ---

def test_get_all_mids_converts_to_decimal():
    ad = _adapter(mids={"ETH": "4000.5", "BTC": "60000"})
    mids = ad.get_all_mids()
    assert mids == {"ETH": Decimal("4000.5"), "BTC": Decimal("60000")}
    assert all(isinstance(v, Decimal) for v in mids.values())


def test_get_all_mids_filters_at_prefixed_spot_index_keys():
    ad = _adapter(mids={"ETH": "4000.5", "@1": "1.0002", "@107": "0.999"})
    mids = ad.get_all_mids()
    assert mids == {"ETH": Decimal("4000.5")}
    assert "@1" not in mids


# --- 7. get_size_decimals ---

_META = {"universe": [{"name": "ETH", "szDecimals": 4}, {"name": "BTC", "szDecimals": 5}]}


def test_get_size_decimals_found_and_cached_across_same_coin():
    ad = _adapter(meta_resp=_META)
    assert ad.get_size_decimals("ETH") == 4
    assert ad.get_size_decimals("ETH") == 4
    assert ad._info.meta_calls == 1  # 同一 coin 第二次不再呼叫 meta


def test_get_size_decimals_cache_covers_other_coins_too_and_unknown_raises():
    ad = _adapter(meta_resp=_META)
    assert ad.get_size_decimals("ETH") == 4
    assert ad.get_size_decimals("BTC") == 5
    assert ad._info.meta_calls == 1  # 首次已快取整個 universe，查其他已知幣種不再呼叫 meta
    with pytest.raises(ValueError):
        ad.get_size_decimals("DOGE")
    assert ad._info.meta_calls == 1  # 找不到也不重打 meta
