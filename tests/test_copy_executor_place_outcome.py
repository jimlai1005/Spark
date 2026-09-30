"""ActionExecutor.place_outcome：保留 place 的成功形態（resting／filled／unknown／rejected）。

2026-09-30 settle 重下事故 Task 1：決策只看 ok，status/filled_sz 為稽核證據。全離線。
"""
from decimal import Decimal

from spark.copytrade.config import CopySettings
from spark.copytrade.executor import ActionExecutor, ExecutorPort, PlaceOutcome
from spark.copytrade.orders import OrderSpec
from spark.exchange.base import BuilderCode, OrderResult
from spark.exchange.fakes import FakeAdapter

BUILDER = BuilderCode(b="0xbuilder", f=20)


class _Adapter(FakeAdapter):
    def __init__(self, result: OrderResult):
        super().__init__()
        self._result = result

    def place_order(self, agent_signer, order, builder):
        self.calls["place_order"].append({"order": order})
        return self._result


def _spec(**kw) -> OrderSpec:
    base = dict(coin="ETH", is_buy=True, sz=Decimal("1.0"), limit_px=Decimal("2000"),
                reduce_only=False, is_trigger=False, tpsl=None, trigger_px=None,
                is_market=False, tif="Gtc")
    base.update(kw)
    return OrderSpec(**base)


def _ex(result: OrderResult | None = None, *, live=True) -> ActionExecutor:
    adapter = _Adapter(result) if result is not None else FakeAdapter()
    return ActionExecutor(adapter, "SIGNER" if live else None, BUILDER, live=live,
                          my_address="0xme", settings=CopySettings(), clock=lambda: 1.0)


Z = Decimal("0")


def test_resting():
    ex = _ex(OrderResult(ok=True, filled_size=Z, avg_px=Z,
                         raw={"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}}))
    o = ex.place_outcome(_spec())
    assert o == PlaceOutcome(ok=True, reason="", status="resting", filled_sz=Z)


def test_filled_carries_filled_sz():
    ex = _ex(OrderResult(ok=True, filled_size=Decimal("168927"), avg_px=Decimal("0.00576"),
                         raw={"status": "ok"}))
    o = ex.place_outcome(_spec())
    assert o.ok and o.status == "filled" and o.filled_sz == Decimal("168927")
    assert ex.records[0].payload["status"] == "filled"
    assert ex.records[0].payload["filled_sz"] == "168927"


def test_rejected_reason_extraction_not_degraded():
    raw = {"status": "ok", "response": {"data": {"statuses": [{"error": "Insufficient margin"}]}}}
    ex = _ex(OrderResult(ok=False, filled_size=Z, avg_px=Z, raw=raw))
    o = ex.place_outcome(_spec())
    assert not o.ok and o.status == "rejected" and o.filled_sz == Z
    assert "Insufficient margin" in o.reason
    assert (o.ok, o.reason) == ex.place_with_reason(_spec())


def test_rejected_reason_from_adapter_own_error():
    ex = _ex(OrderResult(ok=False, filled_size=Z, avg_px=Z, raw={"error": "boom"}))
    o = ex.place_outcome(_spec())
    assert o.status == "rejected" and o.reason == "boom"


def test_verified_short_circuit_is_unknown():
    ex = _ex(OrderResult(ok=True, filled_size=Z, avg_px=Z, raw={"_resilience": "verified"}))
    o = ex.place_outcome(_spec())
    assert o == PlaceOutcome(ok=True, reason="", status="unknown", filled_sz=Z)
    assert ex.records[0].payload["status"] == "unknown"


def test_dry_is_resting():
    ex = _ex(live=False)
    o = ex.place_outcome(_spec())
    assert o == PlaceOutcome(ok=True, reason="", status="resting", filled_sz=Z)


def test_trigger_is_rejected_with_existing_reason():
    ex = _ex(live=False)
    o = ex.place_outcome(_spec(is_trigger=True, trigger_px=Decimal("1900"), tpsl="sl"))
    assert not o.ok and o.status == "rejected" and o.filled_sz == Z
    assert o.reason == "trigger 單 M1 尚不支援（adapter 無 trigger 下單）"
    assert [r.kind for r in ex.records] == ["skip_trigger"]


def test_one_place_records_once_with_status_and_wrappers_agree():
    ex = _ex(live=False)
    ex.place_outcome(_spec())
    assert len(ex.records) == 1
    assert ex.records[0].kind == "place"
    assert ex.records[0].payload["status"] == "resting"
    assert ex.records[0].payload["filled_sz"] == "0"
    ex.place(_spec())
    ex.place_with_reason(_spec())
    assert len(ex.records) == 3  # 每次 place 只記一筆


def test_port_declares_place_outcome():
    assert isinstance(_ex(live=False), ExecutorPort)
