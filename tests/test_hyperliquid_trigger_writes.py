"""T3（plan 2026-11-21-trigger-order-mirroring）：交易所寫入層支援觸發單。

假 SDK exchange 只記錄呼叫參數；回應與 frontendOpenOrders 的測試資料取自 testnet 探針
原始輸出（docs/superpowers/research/2026-11-21-trigger-order-probe.md §3、§4），不自編形狀。
測試全離線（conftest autouse socket-ban）。
"""
import copy
import json
from decimal import Decimal

import pytest

from spark.exchange.base import BuilderCode, Order
from spark.exchange.fakes import FakeAdapter
from spark.exchange.hyperliquid import HyperliquidAdapter
from spark.resilience import ResilientExchange

BUILDER = BuilderCode(b="0xbuilder", f=20)
_BUILDER_DICT = {"b": "0xbuilder", "f": 20}

# BTC szDecimals=5 → 價格小數上限 1 位（plan T3 測試 1）。
_META = {"universe": [{"name": "BTC", "szDecimals": 5}, {"name": "ETH", "szDecimals": 4}]}

# research §4：成功回應原文（statuses 形狀）。
RESTING = [{"resting": {"oid": 65739864215}}]            # P1 行 6
WAITING_FOR_TRIGGER = ["waitingForTrigger"]              # P3 行 24
NORMAL_TPSL = [{"resting": {"oid": 65739942235}}, "waitingForFill", "waitingForFill"]  # P12 行 126


def _ok(statuses):
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": statuses}}}


class FakeInfo:
    def __init__(self, frontend_orders=None):
        self._frontend_orders = frontend_orders or []

    def meta(self):
        return _META

    def frontend_open_orders(self, address):
        return self._frontend_orders


class FakeSdkExchange:
    """記錄 order／bulk_orders 呼叫；statuses 可注入。"""

    def __init__(self, statuses=None, exc_times=0, exc=None):
        self.order_calls = []
        self.bulk_calls = []
        self._statuses = statuses if statuses is not None else RESTING
        self._exc_times = exc_times
        self._exc = exc

    def _maybe_raise(self):
        if self._exc_times > 0:
            self._exc_times -= 1
            raise self._exc

    def order(self, name, is_buy, sz, limit_px, order_type, reduce_only=False,
              cloid=None, builder=None):
        self.order_calls.append({
            "name": name, "is_buy": is_buy, "sz": sz, "limit_px": limit_px,
            "order_type": order_type, "reduce_only": reduce_only, "builder": builder})
        self._maybe_raise()
        return _ok(self._statuses)

    def bulk_orders(self, order_requests, builder=None, grouping="na"):
        self.bulk_calls.append({
            "order_requests": copy.deepcopy(order_requests), "builder": builder,
            "grouping": grouping})
        self._maybe_raise()
        return _ok(self._statuses)


def _adapter(sdk=None, frontend_orders=None, resilient=None):
    sdk = sdk or FakeSdkExchange()
    return HyperliquidAdapter(network="testnet", info=FakeInfo(frontend_orders),
                              exchange=resilient or sdk)


def _trigger(**kw):
    base = dict(coin="BTC", is_buy=False, size=Decimal("0.00116"),
                limit_px=Decimal("48043"), tif="Gtc", reduce_only=True,
                trigger_px=Decimal("48043"), tpsl="sl", is_market=False)
    base.update(kw)
    return Order(**base)


# --- 測試 1：止損限價 -------------------------------------------------------

def test_stop_limit_goes_through_order_with_trigger_type_reduce_only_and_builder():
    sdk = FakeSdkExchange()
    ad = _adapter(sdk)
    res = ad.place_order(None, _trigger(trigger_px=Decimal("61000.04"),
                                        limit_px=Decimal("61000.04")), BUILDER)
    assert res.ok is True
    assert sdk.bulk_calls == []
    (c,) = sdk.order_calls
    # triggerPx 經 _round_px：BTC szDecimals=5 → 1 位小數 → 61000.04 → 61000.0
    assert c["order_type"] == {"trigger": {"triggerPx": 61000.0, "isMarket": False, "tpsl": "sl"}}
    assert c["limit_px"] == 61000.0
    assert c["reduce_only"] is True
    assert c["builder"] == _BUILDER_DICT           # b、f 兩鍵齊全
    assert c["sz"] == pytest.approx(0.00116)


# --- 測試 2：市價型 ---------------------------------------------------------

def test_market_type_sets_is_market_and_passes_limit_px_through():
    sdk = FakeSdkExchange()
    ad = _adapter(sdk)
    ad.place_order(None, _trigger(is_market=True, tpsl="tp", limit_px=Decimal("43265"),
                                  trigger_px=Decimal("48072")), BUILDER)
    (c,) = sdk.order_calls
    assert c["order_type"]["trigger"] == {"triggerPx": 48072.0, "isMarket": True, "tpsl": "tp"}
    assert c["limit_px"] == 43265.0
    assert c["builder"] == _BUILDER_DICT


# --- 測試 3：倉位止盈止損走 bulk_orders -------------------------------------

def test_position_tpsl_uses_bulk_orders_with_grouping_and_builder():
    sdk = FakeSdkExchange(statuses=WAITING_FOR_TRIGGER)
    ad = _adapter(sdk)
    res = ad.place_order(None, _trigger(position_tpsl=True, is_market=True,
                                        size=Decimal("0"), limit_px=Decimal("43265"),
                                        trigger_px=Decimal("48072")), BUILDER)
    assert res.ok is True
    assert sdk.order_calls == []
    (c,) = sdk.bulk_calls
    assert c["grouping"] == "positionTpsl"
    assert c["builder"] == _BUILDER_DICT
    (req,) = c["order_requests"]
    assert req["coin"] == "BTC" and req["is_buy"] is False
    assert req["reduce_only"] is True
    assert req["sz"] == 0.0
    assert req["limit_px"] == 43265.0
    assert req["order_type"] == {"trigger": {"triggerPx": 48072.0, "isMarket": True, "tpsl": "sl"}}


# --- 測試 4：fail-loud 與條件進場 -------------------------------------------

def test_tpsl_none_trigger_raises_and_sdk_not_called():
    sdk = FakeSdkExchange()
    ad = _adapter(sdk)
    with pytest.raises(ValueError):
        ad.place_order(None, _trigger(tpsl=None), BUILDER)
    with pytest.raises(ValueError):
        ad.place_order(None, _trigger(tpsl="stop"), BUILDER)
    assert sdk.order_calls == [] and sdk.bulk_calls == []


def test_position_tpsl_without_reduce_only_raises_and_sdk_not_called():
    sdk = FakeSdkExchange()
    ad = _adapter(sdk)
    with pytest.raises(ValueError):
        ad.place_order(None, _trigger(position_tpsl=True, reduce_only=False), BUILDER)
    assert sdk.order_calls == [] and sdk.bulk_calls == []


@pytest.mark.parametrize("kw", [{"tpsl": "sl"}, {"position_tpsl": True}])
def test_trigger_flags_without_trigger_px_raise_instead_of_becoming_limit_order(kw):
    """殘留觸發旗標但沒有 trigger_px：不得默默送成普通限價單（倉位型會用 size 開出部位）。"""
    sdk = FakeSdkExchange()
    ad = _adapter(sdk)
    o = Order("BTC", False, Decimal("0.01"), Decimal("48000"), "Gtc", reduce_only=True, **kw)
    with pytest.raises(ValueError):
        ad.place_order(None, o, BUILDER)
    assert sdk.order_calls == [] and sdk.bulk_calls == []


def test_conditional_entry_trigger_passes_reduce_only_false_with_builder():
    sdk = FakeSdkExchange()
    ad = _adapter(sdk)
    res = ad.place_order(None, _trigger(is_buy=True, reduce_only=False, tpsl="sl",
                                        is_market=True, trigger_px=Decimal("72185"),
                                        limit_px=Decimal("79404")), BUILDER)
    assert res.ok is True
    (c,) = sdk.order_calls
    assert c["reduce_only"] is False
    assert c["builder"] == _BUILDER_DICT
    assert c["order_type"]["trigger"]["isMarket"] is True


def test_conditional_entry_not_blindly_retried_through_resilient_exchange():
    """非 reduce-only 觸發單＝非冪等：ConnectionError 只呼叫一次、例外上拋（靠下一輪對帳自癒）。"""
    sdk = FakeSdkExchange(exc_times=5, exc=ConnectionError("Connection reset by peer"))
    ad = _adapter(resilient=ResilientExchange(sdk, sleep_fn=lambda d: None))
    with pytest.raises(ConnectionError):
        ad.place_order(None, _trigger(is_buy=True, reduce_only=False), BUILDER)
    assert len(sdk.order_calls) == 1


def test_reduce_only_trigger_is_retried_on_connection_error():
    sdk = FakeSdkExchange(exc_times=1, exc=ConnectionError("Connection reset by peer"))
    ad = _adapter(resilient=ResilientExchange(sdk, sleep_fn=lambda d: None))
    res = ad.place_order(None, _trigger(), BUILDER)
    assert res.ok is True
    assert len(sdk.order_calls) == 2


# --- 測試 5：回應解析（research §3／§4 原始 statuses）-----------------------

@pytest.mark.parametrize("statuses,ok", [
    (RESTING, True),                                  # P1：resting＋oid
    (WAITING_FOR_TRIGGER, True),                      # P3：字串、無 oid
    (["waitingForFill"], False),                      # normalTpsl 子單狀態：不是成功終態
    (["somethingNew"], False),                        # 不認得的字串
    ([{"error": "x"}], False),                        # 單筆委託錯誤
    ([None], False),                                  # 不認得的形狀（非 str／dict）
    ([["waitingForTrigger"]], False),                 # 不認得的形狀（巢狀）
])
def test_parse_order_response_status_shapes(statuses, ok):
    ad = _adapter()
    raw = _ok(statuses)
    res = ad._parse_order_response(raw)       # 不得拋例外
    assert res.ok is ok
    assert res.filled_size == Decimal("0") and res.avg_px == Decimal("0")
    assert res.raw == raw                     # 失敗與成功都保留 raw


def test_place_order_position_tpsl_string_status_is_ok_end_to_end():
    ad = _adapter(FakeSdkExchange(statuses=WAITING_FOR_TRIGGER))
    res = ad.place_order(None, _trigger(position_tpsl=True), BUILDER)
    assert res.ok is True and res.filled_size == Decimal("0")


def test_place_order_unknown_string_status_not_ok_not_raising():
    ad = _adapter(FakeSdkExchange(statuses=["somethingNew"]))
    res = ad.place_order(None, _trigger(position_tpsl=True), BUILDER)
    assert res.ok is False
    assert res.raw["response"]["data"]["statuses"] == ["somethingNew"]


# --- 測試 8：get_open_orders 的 is_pending_child（research §4 P12 原始 JSON）-

_P12_PARENT = json.loads(
    '{"coin": "BTC", "side": "B", "limitPx": "42100.0", "sz": "0.00116", "oid": 65739942235, '
    '"timestamp": 1795277746920, "triggerCondition": "N/A", "isTrigger": false, "triggerPx": "0.0", '
    '"children": [{"coin": "BTC", "side": "A", "limitPx": "59541.0", "sz": "0.00116", '
    '"oid": 65739942236, "timestamp": 1795277746920, "triggerCondition": "Price above 66157", '
    '"isTrigger": true, "triggerPx": "66157.0", "children": [], "isPositionTpsl": false, '
    '"reduceOnly": true, "orderType": "Take Profit Market", "origSz": "0.00116", "tif": null, '
    '"cloid": null}, {"coin": "BTC", "side": "A", "limitPx": "32477.0", "sz": "0.00116", '
    '"oid": 65739942237, "timestamp": 1795277746920, "triggerCondition": "Price below 36086", '
    '"isTrigger": true, "triggerPx": "36086.0", "children": [], "isPositionTpsl": false, '
    '"reduceOnly": true, "orderType": "Stop Market", "origSz": "0.00116", "tif": null, '
    '"cloid": null}], "isPositionTpsl": false, "reduceOnly": false, "orderType": "Limit", '
    '"origSz": "0.00116", "tif": "Gtc", "cloid": null}')


def test_get_open_orders_marks_pending_children_from_p12_raw_json():
    # research P12：兩張子單「同時」以相同內容出現在最上層（與母單 children 內）。
    top_level_children = copy.deepcopy(_P12_PARENT["children"])
    raw = [copy.deepcopy(_P12_PARENT)] + top_level_children
    by_oid = {o.oid: o for o in _adapter(frontend_orders=raw).get_open_orders("0xuser")}
    assert set(by_oid) == {65739942235, 65739942236, 65739942237}
    assert by_oid[65739942236].is_pending_child is True
    assert by_oid[65739942237].is_pending_child is True
    assert by_oid[65739942235].is_pending_child is False
    # 子單最上層的其餘解析不受影響（仍是 reduce-only 觸發單）。
    assert by_oid[65739942237].is_trigger is True and by_oid[65739942237].tpsl == "sl"


def test_get_open_orders_without_children_key_all_false():
    raw = [
        {"coin": "BTC", "oid": 1, "side": "B", "limitPx": "57840", "sz": "0.015"},     # 缺 children
        {"coin": "BTC", "oid": 2, "side": "A", "limitPx": "61000", "sz": "0.015",
         "isTrigger": True, "reduceOnly": True, "orderType": "Stop Limit",
         "triggerPx": "61000", "children": []},                                       # 空 children
    ]
    orders = _adapter(frontend_orders=raw).get_open_orders("0xuser")
    assert [o.is_pending_child for o in orders] == [False, False]


# --- 測試 9：倉位型數量一律送 0（D-P3）--------------------------------------

@pytest.mark.parametrize("size", ["0.00116", "5", "0"])
def test_position_tpsl_always_sends_zero_size_regardless_of_order_size(size):
    sdk = FakeSdkExchange(statuses=WAITING_FOR_TRIGGER)
    ad = _adapter(sdk)
    ad.place_order(None, _trigger(position_tpsl=True, size=Decimal(size)), BUILDER)
    (c,) = sdk.bulk_calls
    assert c["order_requests"][0]["sz"] == 0.0
    assert sdk.order_calls == []


# --- 測試 6：ResilientExchange.bulk_orders 分類 ------------------------------

def _req(reduce_only):
    return {"coin": "BTC", "is_buy": False, "sz": 0.0, "limit_px": 1.0,
            "order_type": {"trigger": {"triggerPx": 1.0, "isMarket": True, "tpsl": "sl"}},
            "reduce_only": reduce_only}


def test_resilient_bulk_orders_all_reduce_only_retries_on_connection_error():
    sdk = FakeSdkExchange(exc_times=1, exc=ConnectionError("Connection reset by peer"))
    rx = ResilientExchange(sdk, sleep_fn=lambda d: None)
    res = rx.bulk_orders([_req(True)], _BUILDER_DICT, grouping="positionTpsl")
    assert res["status"] == "ok"
    assert len(sdk.bulk_calls) == 2
    assert sdk.bulk_calls[-1]["grouping"] == "positionTpsl"
    assert sdk.bulk_calls[-1]["builder"] == _BUILDER_DICT


def test_resilient_bulk_orders_with_non_reduce_only_is_single_attempt():
    sdk = FakeSdkExchange(exc_times=5, exc=ConnectionError("Connection reset by peer"))
    rx = ResilientExchange(sdk, sleep_fn=lambda d: None)
    with pytest.raises(ConnectionError):
        rx.bulk_orders([_req(True), _req(False)], _BUILDER_DICT, grouping="na")
    assert len(sdk.bulk_calls) == 1


def test_resilient_bulk_orders_empty_requests_not_treated_as_idempotent():
    sdk = FakeSdkExchange(exc_times=5, exc=ConnectionError("Connection reset by peer"))
    rx = ResilientExchange(sdk, sleep_fn=lambda d: None)
    with pytest.raises(ConnectionError):
        rx.bulk_orders([], _BUILDER_DICT)
    assert len(sdk.bulk_calls) == 1


def test_resilient_bulk_orders_semantic_error_not_retried():
    sdk = FakeSdkExchange(exc_times=5, exc=ValueError("Insufficient margin"))
    rx = ResilientExchange(sdk, sleep_fn=lambda d: None)
    with pytest.raises(ValueError):
        rx.bulk_orders([_req(True)], _BUILDER_DICT)
    assert len(sdk.bulk_calls) == 1


# --- 非觸發單行為不變 -------------------------------------------------------

def test_plain_limit_order_unchanged_by_trigger_support():
    sdk = FakeSdkExchange(statuses=[{"filled": {"totalSz": "0.01", "avgPx": "4000"}}])
    ad = _adapter(sdk)
    res = ad.place_order(None, Order("ETH", True, Decimal("0.01"), Decimal("4000"), "Ioc"), BUILDER)
    (c,) = sdk.order_calls
    assert c["order_type"] == {"limit": {"tif": "Ioc"}}
    assert c["reduce_only"] is False and c["builder"] == _BUILDER_DICT
    assert sdk.bulk_calls == []
    assert res.ok is True and res.filled_size == Decimal("0.01")


# --- FakeAdapter ------------------------------------------------------------

def test_fake_adapter_trigger_order_ok_unfilled_and_no_builder_fee_accrued():
    fa = FakeAdapter()
    before = fa.query_builder_accrued("0xb")
    res = fa.place_order(None, _trigger(), BUILDER)
    assert res.ok is True and res.filled_size == Decimal("0") and res.avg_px == Decimal("0")
    assert fa.query_builder_accrued("0xb") == before
    assert fa.calls["place_order"][-1]["order"].trigger_px == Decimal("48043")
