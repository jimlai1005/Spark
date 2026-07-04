from decimal import Decimal
from datetime import datetime
from dataclasses import FrozenInstanceError
import pytest
from spark.exchange.base import BuilderCode, Order, Fill, ExchangeAdapter, TxResult, OrderResult


def test_builder_code_is_frozen():
    bc = BuilderCode(b="0xabc", f=20)
    with pytest.raises(FrozenInstanceError):
        bc.f = 30  # frozen dataclass


def test_order_holds_decimal_fields():
    o = Order(coin="ETH", is_buy=True, size=Decimal("0.01"), limit_px=Decimal("4000"), tif="Ioc")
    assert o.tif == "Ioc"
    assert isinstance(o.size, Decimal)


def test_fill_holds_builder_fee():
    f = Fill(time=datetime(2026, 6, 18), coin="ETH", px=Decimal("4000"),
             sz=Decimal("0.01"), side="B", builder_fee=Decimal("0.008"))
    assert f.builder_fee == Decimal("0.008")


def test_adapter_is_abstract_and_has_no_withdraw():
    # 非託管不變量：介面刻意不存在喚款方法
    assert not hasattr(ExchangeAdapter, "withdraw")
    assert not hasattr(ExchangeAdapter, "transfer")
    with pytest.raises(TypeError):
        ExchangeAdapter()  # ABC 不可實例化


def test_result_types_construct():
    tx = TxResult(ok=True, raw={"status": "ok"})
    assert tx.ok is True
    assert tx.raw == {"status": "ok"}
    od = OrderResult(ok=True, filled_size=Decimal("0.01"), avg_px=Decimal("4000"),
                     raw={"status": "filled"})
    assert od.ok is True
    assert od.filled_size == Decimal("0.01")
    assert od.avg_px == Decimal("4000")


def test_tx_result_agent_key_never_in_repr():
    tx = TxResult(ok=True, raw={"status": "ok"}, agent_key="0xsecret")
    assert tx.agent_key == "0xsecret"
    assert "0xsecret" not in repr(tx)
