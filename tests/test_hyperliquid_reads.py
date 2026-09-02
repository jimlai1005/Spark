"""HyperliquidAdapter 讀側（Task 2）：七個方法的映射測試。
FakeInfo 手法沿用 tests/test_hyperliquid_adapter.py；餵真實形狀（欄位名/缺鍵行為經
SDK hyperliquid/info.py docstring 與 Hyperliquid 官方 frontendOpenOrders 文件交叉確認，
frontendOpenOrders 回應無字面 "tpsl" 鍵——tpsl 由 orderType 文字判讀衍生，
語意移植自 hl-copytrader src/monitor.py:181-191 的 _parse_orders 邏輯）。"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from spark.exchange.base import USER_FILLS_PAGE_LIMIT, FillsTruncatedError
from spark.exchange.hyperliquid import HyperliquidAdapter
from spark.resilience import _is_transient_error


class FakeInfo:
    """八個讀側方法各自需要的假回應；呼叫次數計數供快取/同源測試使用。"""

    def __init__(self, frontend_orders=None, user_state_resp=None, portfolio_resp=None,
                 fills=None, mids=None, meta_resp=None, ledger_updates=None, active_asset_leverage=None):
        self._frontend_orders = frontend_orders if frontend_orders is not None else []
        self._user_state_resp = user_state_resp
        self._portfolio_resp = portfolio_resp if portfolio_resp is not None else []
        self._fills = fills if fills is not None else []
        self._mids = mids if mids is not None else {}
        self._meta_resp = meta_resp if meta_resp is not None else {"universe": []}
        self._ledger_updates = ledger_updates if ledger_updates is not None else []
        self._active_asset_leverage = active_asset_leverage
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

    def post(self, endpoint, body):
        if endpoint == "/info" and body.get("type") == "userNonFundingLedgerUpdates":
            return self._ledger_updates
        if endpoint == "/info" and body.get("type") == "activeAssetData":
            return self._active_asset_leverage
        raise NotImplementedError(f"FakeInfo.post({endpoint}, {body})")


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
    assert o.is_market is False  # 非 trigger 恆 False（hl monitor.py:184-186）
    assert o.tif == "Gtc"


def test_get_open_orders_parses_orig_sz_decimal():
    # 2026-07-28 事故修法：CRIT extra 側要能顯示「原量」，才看得出 ro 單被交易所
    # 修剪（sz=剩餘量、origSz=下單原量）。
    orders_raw = [{
        "coin": "ETH", "oid": 500, "side": "A", "limitPx": "2000", "sz": "0.0289",
        "reduceOnly": True, "isTrigger": False, "orderType": "Limit",
        "triggerPx": "0.0", "tif": "Gtc", "timestamp": 1750000005000,
        "origSz": "0.0457",
    }]
    ad = _adapter(frontend_orders=orders_raw)
    o = ad.get_open_orders("0xuser")[0]
    assert o.orig_sz == Decimal("0.0457")
    assert isinstance(o.orig_sz, Decimal)


def test_get_open_orders_missing_orig_sz_maps_none():
    orders_raw = [{
        "coin": "ETH", "oid": 501, "side": "B", "limitPx": "1900", "sz": "1.0",
        "reduceOnly": False, "isTrigger": False, "orderType": "Limit",
        "triggerPx": "0.0", "tif": "Gtc", "timestamp": 1750000006000,
        # origSz 缺鍵 → None（不得假造 0 或抄 sz）
    }]
    ad = _adapter(frontend_orders=orders_raw)
    assert ad.get_open_orders("0xuser")[0].orig_sz is None


def test_get_open_orders_alo_tif_passes_through():
    # maker 單（Alo = add liquidity only）的 tif 必須原樣傳遞，不得被壓成 Gtc——
    # 否則鏡射會把 leader 的 post-only 單變成可吃單的 Gtc 單。
    orders_raw = [{
        "coin": "ETH", "oid": 321, "side": "B", "limitPx": "3900", "sz": "1.0",
        "reduceOnly": False, "isTrigger": False, "orderType": "Limit",
        "triggerPx": "0.0", "tif": "Alo", "timestamp": 1750000003000,
    }]
    ad = _adapter(frontend_orders=orders_raw)
    o = ad.get_open_orders("0xuser")[0]
    assert o.tif == "Alo"
    assert o.is_market is False


def test_get_open_orders_null_tif_defaults_gtc():
    # trigger 單的 tif 常為 None（API 回 null）→ 1:1 hl monitor.py:205 的 `or "Gtc"`。
    orders_raw = [{
        "coin": "ETH", "oid": 322, "side": "B", "limitPx": "3900", "sz": "1.0",
        "reduceOnly": False, "isTrigger": False, "orderType": "Limit",
        "triggerPx": "0.0", "tif": None, "timestamp": 1750000004000,
    }]
    ad = _adapter(frontend_orders=orders_raw)
    assert ad.get_open_orders("0xuser")[0].tif == "Gtc"


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
    # "Stop Market" → trigger 市價單（hl monitor.py:186 "Market" in orderType）；
    # 漏掉這個旗標會把 leader 的止損市價單鏡射成止損限價單。
    assert o.is_market is True
    assert o.tif == "Gtc"  # tif 缺鍵 → 預設 "Gtc"


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
    assert o.is_market is False  # "Take Profit Limit" 不含 "Market" → 觸發限價單


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


def test_get_positions_missing_guaranteed_field_raises_loudly():
    # SDK 保證存在的欄位（如 marginUsed）缺鍵 = schema 漂移 → 必須大聲 KeyError，
    # 不得靜默給預設值（工程原則 3：財務資料寧可炸不可給錯值）。
    broken_state = {
        "assetPositions": [
            {"position": {
                "coin": "ETH", "szi": "1", "entryPx": "3000",
                "leverage": {"type": "cross", "value": 5},
                "unrealizedPnl": "10",
                # marginUsed 缺鍵
            }, "type": "oneWay"},
        ],
        "marginSummary": {"accountValue": "0", "totalMarginUsed": "0", "totalNtlPos": "0"},
        "withdrawable": "0",
    }
    ad = _adapter(user_state_resp=broken_state)
    with pytest.raises(KeyError):
        ad.get_positions("0xuser")


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
    # expected 用字面值寫死（1750000000000 ms 經 calendar.timegm 獨立驗算），
    # 不得用 float 除法算 expected（那是拿實作驗自己）。
    assert f.time == datetime(2025, 6, 15, 15, 6, 40, tzinfo=timezone.utc)
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
    # builderFee 亦缺鍵 → 預設 0（本筆非我方路由的成交）
    assert f.builder_fee == Decimal("0")


def test_get_user_fills_parses_builder_fee():
    fills_raw = [
        {"coin": "ETH", "px": "4000.25", "sz": "0.1", "side": "B", "time": 1750000000000,
         "crossed": True, "oid": 111, "fee": "0.008", "builderFee": "0.002"},
    ]
    ad = _adapter(fills=fills_raw)
    f = ad.get_user_fills(
        "0xuser", datetime(2025, 6, 15, tzinfo=timezone.utc), datetime(2025, 6, 16, tzinfo=timezone.utc)
    )[0]
    assert f.builder_fee == Decimal("0.002")
    assert isinstance(f.builder_fee, Decimal)


def test_get_user_fills_builder_fee_none_defaults_zero():
    # HL 回應可能給 builderFee=None（而非缺鍵）——"or "0"" 需接住，不得炸
    fills_raw = [
        {"coin": "ETH", "px": "4000.25", "sz": "0.1", "side": "B", "time": 1750000000000,
         "crossed": True, "oid": 111, "fee": "0.008", "builderFee": None},
    ]
    ad = _adapter(fills=fills_raw)
    f = ad.get_user_fills(
        "0xuser", datetime(2025, 6, 15, tzinfo=timezone.utc), datetime(2025, 6, 16, tzinfo=timezone.utc)
    )[0]
    assert f.builder_fee == Decimal("0")


def _raw_fill(i: int) -> dict:
    return {"coin": "ETH", "px": "4000", "sz": "0.1", "side": "B",
            "time": 1750000000000 + i, "crossed": True, "oid": i, "fee": "0.008"}


def test_get_user_fills_raises_when_page_limit_reached():
    """⭐ 達 API 單次上限 ⇒ 視窗內成交沒取全 ⇒ **絕不靜默回傳半份**。

    截斷後的清單看起來完全正常（筆數少、名目小），沒有任何呼叫端能從結果本身看出
    它不完整；而下游對「偏低」的反應恰好都是最危險的那個——成本熔斷器的換手率分子
    被低估 ⇒ 該擋的不擋。變異測試靶：拿掉上限檢查，本測試必轉紅。
    """
    ad = _adapter(fills=[_raw_fill(i) for i in range(USER_FILLS_PAGE_LIMIT)])
    with pytest.raises(FillsTruncatedError) as ei:
        ad.get_user_fills("0xuser", datetime(2025, 6, 15, tzinfo=timezone.utc),
                          datetime(2025, 6, 16, tzinfo=timezone.utc))
    assert str(USER_FILLS_PAGE_LIMIT) in str(ei.value), "訊息須帶實際筆數"


def test_truncation_error_is_not_classified_as_transient():
    """截斷是語意錯誤：重試同一窗口只會再拿回同樣的 2000 筆（工程原則 2）。

    訊息刻意不含位址／時間戳——十六進位位址可能剛好含 "503" 等 transient marker
    而讓 resilience 邊界誤判成可重試。
    """
    ad = _adapter(fills=[_raw_fill(i) for i in range(USER_FILLS_PAGE_LIMIT)])
    try:
        ad.get_user_fills("0x503abc", datetime(2025, 6, 15, tzinfo=timezone.utc),
                          datetime(2025, 6, 16, tzinfo=timezone.utc))
    except FillsTruncatedError as e:
        assert _is_transient_error(e) is False
    else:  # pragma: no cover
        pytest.fail("未拋 FillsTruncatedError")


def test_get_user_fills_just_below_page_limit_is_returned_normally():
    """邊界對偶：上限 - 1 筆屬正常回應，照常回傳、不得誤拋。"""
    ad = _adapter(fills=[_raw_fill(i) for i in range(USER_FILLS_PAGE_LIMIT - 1)])
    out = ad.get_user_fills("0xuser", datetime(2025, 6, 15, tzinfo=timezone.utc),
                            datetime(2025, 6, 16, tzinfo=timezone.utc))
    assert len(out) == USER_FILLS_PAGE_LIMIT - 1


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


def test_get_size_decimals_cache_covers_other_coins_without_refetch():
    ad = _adapter(meta_resp=_META)
    assert ad.get_size_decimals("ETH") == 4
    assert ad.get_size_decimals("BTC") == 5
    assert ad._info.meta_calls == 1  # 首次已快取整個 universe，查其他已知幣種不再呼叫 meta


def test_get_size_decimals_cache_self_heals_for_newly_listed_coin():
    ad = _adapter(meta_resp=_META)
    assert ad.get_size_decimals("ETH") == 4
    assert ad._info.meta_calls == 1
    # 新上市幣：第二次 meta 回應多了 DOGE → cache miss 重打 meta 自癒，無需重啟
    ad._info._meta_resp = {
        "universe": _META["universe"] + [{"name": "DOGE", "szDecimals": 0}],
    }
    assert ad.get_size_decimals("DOGE") == 0
    assert ad._info.meta_calls == 2
    # 已知幣種（含剛自癒進快取的 DOGE）仍走快取，不再重打
    assert ad.get_size_decimals("ETH") == 4
    assert ad.get_size_decimals("DOGE") == 0
    assert ad._info.meta_calls == 2


def test_get_size_decimals_unknown_after_refresh_raises_and_retries_next_call():
    ad = _adapter(meta_resp=_META)
    assert ad.get_size_decimals("ETH") == 4
    assert ad._info.meta_calls == 1
    with pytest.raises(ValueError):
        ad.get_size_decimals("XYZ")
    assert ad._info.meta_calls == 2  # miss 先重打一次 meta，更新後仍查無才 raise
    with pytest.raises(ValueError):
        ad.get_size_decimals("XYZ")
    assert ad._info.meta_calls == 3  # 失敗不入快取：下次同 coin 呼叫再重試 meta


# --- 8. get_ledger_flows ---

def test_get_ledger_flows_maps_four_whitelisted_types():
    """四種白名單型別：vaultDeposit (+), deposit (+), withdraw (-), vaultWithdraw (-)."""
    ledger_raw = [
        {"time": 1626998400000, "delta": {"type": "vaultDeposit", "usdc": "100.50"}},
        {"time": 1626998401000, "delta": {"type": "deposit", "usdc": "50.00"}},
        {"time": 1626998402000, "delta": {"type": "withdraw", "usdc": "30.25"}},
        {"time": 1626998403000, "delta": {"type": "vaultWithdraw", "netWithdrawnUsd": "20.75"}},
    ]
    ad = _adapter(ledger_updates=ledger_raw)
    flows, unknown = ad.get_ledger_flows("0xuser", 0)

    assert len(flows) == 4
    assert flows[0].time_ms == 1626998400000
    assert flows[0].usdc == Decimal("100.50")
    assert flows[1].usdc == Decimal("50.00")
    assert flows[2].usdc == Decimal("-30.25")  # withdraw 為負
    assert flows[3].usdc == Decimal("-20.75")  # vaultWithdraw 為負
    assert all(isinstance(f.usdc, Decimal) for f in flows)
    assert unknown == []


def test_get_ledger_flows_collects_unknown_types_deduplicated():
    """未知型別收進第二回傳值、去重、升冪排序。"""
    ledger_raw = [
        {"time": 1000, "delta": {"type": "vaultDeposit", "usdc": "100"}},
        {"time": 2000, "delta": {"type": "accountTransfer", "amount": "50"}},
        {"time": 3000, "delta": {"type": "withdraw", "usdc": "30"}},
        {"time": 4000, "delta": {"type": "accountTransfer", "amount": "25"}},  # 重複
        {"time": 5000, "delta": {"type": "feeRebate", "amount": "5"}},
    ]
    ad = _adapter(ledger_updates=ledger_raw)
    flows, unknown = ad.get_ledger_flows("0xuser", 0)

    assert len(flows) == 2  # 只有白名單 2 種
    assert sorted(unknown) == unknown  # 升冪排序
    assert set(unknown) == {"accountTransfer", "feeRebate"}  # 去重
    assert unknown == ["accountTransfer", "feeRebate"]


def test_get_ledger_flows_missing_type_is_stringified_not_typeerror():
    """回歸：delta 缺 type（None）曾原樣進 set → 下游 sorted/join 混型 TypeError。
    修法：unknown 一律存字串——None 進來變 "None"，排序與 join 永不炸。"""
    ledger_raw = [
        {"time": 1000, "delta": {"usdc": "100"}},                    # 缺 type
        {"time": 2000, "delta": {"type": "deposit", "usdc": "50"}},  # 正常
    ]
    ad = _adapter(ledger_updates=ledger_raw)
    flows, unknown = ad.get_ledger_flows("0xuser", 0)
    assert len(flows) == 1
    assert flows[0].usdc == Decimal("50")
    assert unknown == ["None"]
    assert all(isinstance(t, str) for t in unknown)
    ", ".join(unknown)  # 下游 loop 的 join 用法必須可行


def test_get_ledger_flows_whitelisted_type_missing_amount_is_flagged_and_skipped():
    """回歸：白名單型別缺金額欄位曾被靜默補 "0"——無聲少算一筆流量。
    修法：記 "<type>:missing-amount" 進 unknown（大聲），該筆不入 flows。"""
    ledger_raw = [
        {"time": 1000, "delta": {"type": "vaultDeposit"}},              # 缺 usdc
        {"time": 2000, "delta": {"type": "vaultWithdraw"}},             # 缺 netWithdrawnUsd
        {"time": 3000, "delta": {"type": "deposit", "usdc": "50"}},     # 正常
    ]
    ad = _adapter(ledger_updates=ledger_raw)
    flows, unknown = ad.get_ledger_flows("0xuser", 0)
    assert len(flows) == 1  # 缺金額的兩筆不入 flows（也不以 0 偽裝）
    assert flows[0].usdc == Decimal("50")
    assert "vaultDeposit:missing-amount" in unknown
    assert "vaultWithdraw:missing-amount" in unknown


def test_get_ledger_flows_wires_query_address_into_internal_transfer_direction():
    """T9b W1（接線回歸保護）：get_ledger_flows 必須把查詢位址傳給
    flow_anomaly/signed_flow，否則 internalTransfer 永遠落回「方向不明」。
    這個案例故意只驗證接線本身（不重測 ledger_flows 的方向公式，那是
    test_ledger_flows.py 的職責）——拿掉 hyperliquid.py 的 `address=address`
    必須讓它 FAIL（見 T9b 驗收的變異驗證）。"""
    other = "0xother0000000000000000000000000000000000"
    outbound = {"time": 1000, "delta": {"type": "internalTransfer", "usdc": "150.0",
                                         "user": "0xuser", "destination": other,
                                         "fee": "1.0"}}
    ad = _adapter(ledger_updates=[outbound])
    flows, unknown = ad.get_ledger_flows("0xuser", 0)
    assert unknown == []
    assert len(flows) == 1
    assert flows[0].usdc == Decimal("-150.0")  # outbound：查詢位址是 user

    inbound = {"time": 2000, "delta": {"type": "internalTransfer", "usdc": "150.0",
                                        "user": other, "destination": "0xuser",
                                        "fee": "1.0"}}
    ad2 = _adapter(ledger_updates=[inbound])
    flows2, unknown2 = ad2.get_ledger_flows("0xuser", 0)
    assert unknown2 == []
    assert len(flows2) == 1
    assert flows2[0].usdc == Decimal("149.0")  # inbound：查詢位址是 destination


# --- 9. get_active_asset_leverage ---

def test_get_active_asset_leverage_cross_position():
    """空手幣的 activeAssetData 回傳該帳戶現行槓桿設定（不限持倉）。"""
    resp = {"leverage": {"type": "cross", "value": 25}}
    ad = _adapter(active_asset_leverage=resp)
    lev, is_cross = ad.get_active_asset_leverage("0xuser", "ETH")
    assert lev == 25
    assert is_cross is True


def test_get_active_asset_leverage_isolated_position():
    """isolated 槓桿回傳 (value, False)。"""
    resp = {"leverage": {"type": "isolated", "value": 10}}
    ad = _adapter(active_asset_leverage=resp)
    lev, is_cross = ad.get_active_asset_leverage("0xuser", "DOGE")
    assert lev == 10
    assert is_cross is False


def test_get_active_asset_leverage_missing_leverage_key_raises():
    """回應缺 leverage 鍵 → raise ValueError（呼叫端負責降級與告警）。"""
    resp = {}
    ad = _adapter(active_asset_leverage=resp)
    with pytest.raises(ValueError) as ei:
        ad.get_active_asset_leverage("0xuser", "ETH")
    assert "ETH" in str(ei.value)


def test_get_active_asset_leverage_unknown_type_raises():
    """O6（2026-08-01 第三批審查）：leverage.type 不在 {cross, isolated} →
    ValueError——不得靜默當 isolated（`== "cross"` 的布林降級會把未知型別
    映成 isolated 送進 update_leverage）。"""
    resp = {"leverage": {"type": "weird", "value": 10}}
    ad = _adapter(active_asset_leverage=resp)
    with pytest.raises(ValueError) as ei:
        ad.get_active_asset_leverage("0xuser", "ETH")
    assert "weird" in str(ei.value)


def test_get_active_asset_leverage_missing_type_or_value_raises():
    """leverage 物件缺鍵（type 或 value）→ raise ValueError。"""
    resp = {"leverage": {"type": "cross"}}  # 缺 value
    ad = _adapter(active_asset_leverage=resp)
    with pytest.raises(ValueError) as ei:
        ad.get_active_asset_leverage("0xuser", "BTC")
    assert "BTC" in str(ei.value)
