"""紅線 CI 守門：每一筆會產生掛單的寫入（place / market_open / reduce-only close）
必須帶 builder 參數。modify_order 因 SDK 0.24.0 結構限制無 builder，是唯一已知例外
（本檔顯式測試並記錄該例外，而非放任漏檢）。"""
from decimal import Decimal
from spark.exchange.base import ExchangeAdapter, Order, BuilderCode
from spark.exchange.hyperliquid import HyperliquidAdapter


class FakeInfo:
    """離線假 Info：all_mids 固定回傳可控字典，其餘讀側不在本檔測試範圍。"""
    def __init__(self, mids=None):
        self._mids = mids or {"ETH": "4000", "BTC": "65000"}
    def all_mids(self):
        return dict(self._mids)


class FakeExchange:
    """記錄 (method, args, kwargs) —— 供斷言每一筆 order-creating write 都帶 builder。

    回應形狀忠於真實 HL API（頂層 ok、內層 statuses[0] 帶 filled/resting/error/
    "success"）；order_response/modify_response/cancel_response 可注入覆蓋回應，
    供拒單形態測試。"""
    def __init__(self, order_response=None, modify_response=None, cancel_response=None):
        self.calls = []
        self.order_response = order_response
        self.modify_response = modify_response
        self.cancel_response = cancel_response

    def order(self, coin, is_buy, sz, limit_px, order_type, reduce_only=False,
              cloid=None, builder=None):
        self.calls.append(("order", (coin, is_buy, sz, limit_px, order_type),
                           {"reduce_only": reduce_only, "cloid": cloid, "builder": builder}))
        return self.order_response or {"status": "ok", "response": {"data": {"statuses": [
            {"filled": {"totalSz": str(sz), "avgPx": str(limit_px)}}]}}}

    def market_open(self, name, is_buy, sz, px=None, slippage=0.05, cloid=None, builder=None):
        self.calls.append(("market_open", (name, is_buy, sz, px, slippage),
                           {"cloid": cloid, "builder": builder}))
        return {"status": "ok", "response": {"data": {"statuses": [
            {"filled": {"totalSz": str(sz), "avgPx": "4000"}}]}}}

    def modify_order(self, oid, name, is_buy, sz, limit_px, order_type, reduce_only=False,
                     cloid=None):
        self.calls.append(("modify_order", (oid, name, is_buy, sz, limit_px, order_type),
                           {"reduce_only": reduce_only, "cloid": cloid}))
        return self.modify_response or {"status": "ok", "response": {"data": {"statuses": [
            {"resting": {"oid": oid}}]}}}

    def cancel(self, name, oid):
        self.calls.append(("cancel", (name, oid), {}))
        return self.cancel_response or {"status": "ok", "response": {
            "type": "cancel", "data": {"statuses": ["success"]}}}

    def update_leverage(self, leverage, name, is_cross=True):
        self.calls.append(("update_leverage", (leverage, name, is_cross), {}))
        return {"status": "ok"}


def _adapter(exchange=None, info=None):
    return HyperliquidAdapter(network="testnet", info=info or FakeInfo(), exchange=exchange or FakeExchange())


BUILDER = BuilderCode(b="0xbuilder", f=20)


def test_all_order_creating_writes_carry_builder():
    ex = FakeExchange()
    ad = _adapter(exchange=ex)

    ad.place_order(agent_signer=None,
                   order=Order("ETH", True, Decimal("0.01"), Decimal("4000"), "Ioc"),
                   builder=BUILDER)
    ad.market_open(agent_signer=None, coin="ETH", is_buy=True, size=Decimal("0.01"),
                   slippage=Decimal("0.05"), builder=BUILDER)
    ad.close_reduce_only(agent_signer=None, coin="ETH", is_buy=False, size=Decimal("0.01"),
                         slippage=Decimal("0.05"), builder=BUILDER)

    order_creating_calls = [c for c in ex.calls if c[0] in ("order", "market_open")]
    assert len(order_creating_calls) >= 3, "測試空轉防呆：至少要有 3 筆 order-creating writes"
    for method, args, kwargs in order_creating_calls:
        assert kwargs["builder"] == {"b": BUILDER.b, "f": BUILDER.f}, (
            f"{method} 呼叫缺少或錯誤的 builder 參數：{kwargs}")


def test_place_order_passes_reduce_only_through():
    """place_order 不得把 reduce_only 寫死 False——leader 的 reduce-only 掛單鏡射
    必須原樣傳遞，否則會變成可開新倉的普通掛單（方向錯誤的風險放大）。"""
    ex = FakeExchange()
    ad = _adapter(exchange=ex)
    ad.place_order(agent_signer=None,
                   order=Order("ETH", False, Decimal("0.01"), Decimal("4000"), "Gtc",
                               reduce_only=True),
                   builder=BUILDER)
    method, args, kwargs = ex.calls[-1]
    assert method == "order"
    assert kwargs["reduce_only"] is True
    assert kwargs["builder"] == {"b": BUILDER.b, "f": BUILDER.f}
    # 預設仍為 False（不帶 reduce_only 的 Order 不受影響）
    ad.place_order(agent_signer=None,
                   order=Order("ETH", True, Decimal("0.01"), Decimal("4000"), "Ioc"),
                   builder=BUILDER)
    assert ex.calls[-1][2]["reduce_only"] is False


def test_modify_has_no_builder_kwarg_by_sdk_constraint():
    """SDK 0.24.0 modify_order() 簽章無 builder（T1.3 實測歸屬）——這是紅線
    「order-creating writes 全帶 builder」的唯一已知例外，因為改單不建立新訂單意圖。"""
    ex = FakeExchange()
    ad = _adapter(exchange=ex)
    ad.modify_order(agent_signer=None, oid=123,
                    order=Order("ETH", True, Decimal("0.01"), Decimal("4000"), "Ioc"))
    method, args, kwargs = ex.calls[-1]
    assert method == "modify_order"
    assert "builder" not in kwargs, "modify_order 不應該有 builder kwarg（SDK 結構限制）"
    # 佐證：docstring 有明確記錄此例外。
    assert "builder" in ExchangeAdapter.modify_order.__doc__
    assert "0.24.0" in ExchangeAdapter.modify_order.__doc__


def test_close_reduce_only_sets_reduce_only_and_ioc_and_price_direction():
    ex = FakeExchange()
    ad = _adapter(exchange=ex, info=FakeInfo(mids={"ETH": "4000"}))

    res_buy = ad.close_reduce_only(agent_signer=None, coin="ETH", is_buy=True,
                                   size=Decimal("0.01"), slippage=Decimal("0.05"), builder=BUILDER)
    method, args, kwargs = ex.calls[-1]
    coin, is_buy, sz, px, order_type = args
    assert method == "order"
    assert is_buy is True
    assert kwargs["reduce_only"] is True
    assert order_type == {"limit": {"tif": "Ioc"}}
    assert px > 4000  # is_buy=True → px > mid
    assert res_buy.ok is True

    res_sell = ad.close_reduce_only(agent_signer=None, coin="ETH", is_buy=False,
                                    size=Decimal("0.01"), slippage=Decimal("0.05"), builder=BUILDER)
    method, args, kwargs = ex.calls[-1]
    coin, is_buy, sz, px, order_type = args
    assert is_buy is False
    assert kwargs["reduce_only"] is True
    assert px < 4000  # is_buy=False → px < mid
    assert res_sell.ok is True


def test_close_reduce_only_no_mid_returns_not_ok_without_raising():
    ex = FakeExchange()
    ad = _adapter(exchange=ex, info=FakeInfo(mids={"BTC": "65000"}))  # 無 ETH mid

    res = ad.close_reduce_only(agent_signer=None, coin="ETH", is_buy=True,
                               size=Decimal("0.01"), slippage=Decimal("0.05"), builder=BUILDER)
    assert res.ok is False
    assert res.filled_size == Decimal("0")
    assert res.avg_px == Decimal("0")
    assert res.raw == {"error": "no mid for ETH"}
    assert not any(c[0] == "order" for c in ex.calls), "取不到 mid 不應該真的送單"


def test_cancel_and_update_leverage_map_correctly():
    ex = FakeExchange()
    ad = _adapter(exchange=ex)

    ok = ad.cancel_order(agent_signer=None, coin="ETH", oid=999)
    assert ok is True
    method, args, kwargs = ex.calls[-1]
    assert method == "cancel"
    assert args == ("ETH", 999)  # SDK: cancel(name, oid)

    ok2 = ad.update_leverage(agent_signer=None, coin="ETH", leverage=5, is_cross=True)
    assert ok2 is True
    method, args, kwargs = ex.calls[-1]
    assert method == "update_leverage"
    assert args == (5, "ETH", True)  # SDK: update_leverage(leverage, name, is_cross)


def test_modify_order_per_item_rejection_returns_false(caplog):
    """HL 拒單雙形態之二：頂層 status=="ok" 但 statuses[0] 帶 "error"（參照
    hl-copytrader instrument.py:50-65 的記載）——絕不能因頂層 ok 判改單成功。"""
    ex = FakeExchange(modify_response={"status": "ok", "response": {"data": {"statuses": [
        {"error": "Order was never placed, already canceled, or filled."}]}}})
    ad = _adapter(exchange=ex)
    with caplog.at_level("WARNING"):
        ok = ad.modify_order(agent_signer=None, oid=123,
                             order=Order("ETH", True, Decimal("0.01"), Decimal("4000"), "Ioc"))
    assert ok is False
    assert "modify_order rejected" in caplog.text  # 工程原則 3：失敗要大聲記錄


def test_cancel_order_per_item_rejection_returns_false(caplog):
    """cancel 同雙形態：頂層 ok 但單筆 error → False（= 未確認撤單，訂單可能仍掛著；
    真相由上層 settle 重抓掛單為準）。"""
    ex = FakeExchange(cancel_response={"status": "ok", "response": {"data": {"statuses": [
        {"error": "Order was never placed, already canceled, or filled."}]}}})
    ad = _adapter(exchange=ex)
    with caplog.at_level("WARNING"):
        ok = ad.cancel_order(agent_signer=None, coin="ETH", oid=123)
    assert ok is False
    assert "cancel_order rejected" in caplog.text


def test_parse_order_response_accepts_resting_as_success():
    """resting（GTC 掛上訂單簿）是合法成功終態（Task 8 mirror 掛單會走 GTC）：
    ok=True 但 filled_size/avg_px 為 0。Ioc 路徑不受影響（Ioc 不會 resting）。"""
    ex = FakeExchange(order_response={"status": "ok", "response": {"data": {"statuses": [
        {"resting": {"oid": 777}}]}}})
    ad = _adapter(exchange=ex)
    res = ad.place_order(agent_signer=None,
                         order=Order("ETH", True, Decimal("0.01"), Decimal("4000"), "Gtc"),
                         builder=BUILDER)
    assert res.ok is True
    assert res.filled_size == Decimal("0")
    assert res.avg_px == Decimal("0")
    assert res.raw["response"]["data"]["statuses"][0] == {"resting": {"oid": 777}}


def test_cancel_and_update_leverage_report_failure():
    class RejectingExchange(FakeExchange):
        def cancel(self, name, oid):
            self.calls.append(("cancel", (name, oid), {}))
            return {"status": "err"}
        def update_leverage(self, leverage, name, is_cross=True):
            self.calls.append(("update_leverage", (leverage, name, is_cross), {}))
            return {"status": "err"}
    ad = _adapter(exchange=RejectingExchange())
    assert ad.cancel_order(agent_signer=None, coin="ETH", oid=1) is False
    assert ad.update_leverage(agent_signer=None, coin="ETH", leverage=5, is_cross=True) is False


def test_abc_still_has_no_withdraw_transfer():
    assert not hasattr(ExchangeAdapter, "withdraw")
    assert not hasattr(ExchangeAdapter, "transfer")
    for name in ("cancel_order", "modify_order", "market_open", "close_reduce_only",
                 "update_leverage"):
        assert name in ExchangeAdapter.__abstractmethods__, f"{name} 應為抽象方法"


def test_order_reduce_only_default_false():
    o = Order(coin="ETH", is_buy=True, size=Decimal("0.01"), limit_px=Decimal("4000"), tif="Ioc")
    assert o.reduce_only is False
