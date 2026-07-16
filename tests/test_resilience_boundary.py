"""結構守門：交易執行一定走 resilience 邊界，且重試邏輯只存在一處。
1:1 移植自 hl-copytrader tests/test_resilience_boundary.py（唯讀參考），
把 Trader 換成 spark 的 HyperliquidAdapter（本 repo 的寫入接線點）。
"""
import pathlib
import re
from spark.exchange.hyperliquid import HyperliquidAdapter
from spark.resilience import ResilientExchange

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


class FakeExchange:
    """記錄呼叫；供透傳測試使用（approve_* 不在 ResilientExchange 的顯式包裝方法內）。"""
    def __init__(self):
        self.calls = []

    def approve_builder_fee(self, builder, max_fee_rate):
        self.calls.append(("approve_builder_fee", builder, max_fee_rate))
        return {"status": "ok"}

    def approve_agent(self, name=None):
        self.calls.append(("approve_agent", name))
        return ({"status": "ok"}, "0xagentkey")


def _adapter(exchange=None):
    return HyperliquidAdapter(network="testnet", info=None, exchange=exchange or FakeExchange())


def test_adapter_wraps_exchange_in_resilient_boundary():
    ad = _adapter()
    assert type(ad._exchange).__name__ == "ResilientExchange"


def test_adapter_keeps_none_exchange():
    ad = HyperliquidAdapter(network="testnet", info=None, exchange=None)
    assert ad._exchange is None


def test_no_double_wrap_when_exchange_already_resilient():
    inner = FakeExchange()
    already_wrapped = ResilientExchange(inner)
    ad = HyperliquidAdapter(network="testnet", info=None, exchange=already_wrapped)
    assert ad._exchange is already_wrapped     # 同一個物件，沒有再包一層
    assert ad._exchange._ex is inner           # 內層仍是原始 exchange，不是雙層 ResilientExchange


def test_passthrough_delegates_unwrapped_methods_to_inner_exchange():
    """approve_builder_fee/approve_agent 未被 ResilientExchange 顯式包裝，
    透過 __getattr__ 直接穿透到內層 exchange。"""
    ex = FakeExchange()
    ad = _adapter(exchange=ex)
    ad.approve_builder_fee(main_signer=None, builder="0xbuilder", max_rate="0.001")
    ad.approve_agent(main_signer=None, agent_name="spark-agent")
    assert ("approve_builder_fee", "0xbuilder", "0.001") in ex.calls
    assert ("approve_agent", "spark-agent") in ex.calls


# __getattr__ 透傳白名單：onboarding 一次性動作（main 錢包簽章、非交易寫入），
# 刻意不經重試包裝。任何「交易寫入」都不准出現在這裡——寫入要在 ResilientExchange
# 加顯式包裝方法。
PASSTHROUGH_WHITELIST = {"approve_builder_fee", "approve_agent"}


def test_every_adapter_exchange_call_is_wrapped_or_whitelisted():
    """__getattr__ 透傳的結構守門：掃 hyperliquid.py 全部 `self._exchange.<name>(`
    呼叫，每個 <name> 必須是 ResilientExchange 的顯式包裝方法，或在透傳白名單內。
    否則新增的寫入會經 __getattr__ 靜默繞過 resilience 邊界（無重試、無分類）——
    本測試讓漏包裝在 CI 就炸，而不是在實盤斷線時才發現。"""
    text = (SRC / "spark" / "exchange" / "hyperliquid.py").read_text(encoding="utf-8")
    called = set(re.findall(r"self\._exchange\.(\w+)\(", text))
    # 空轉防呆：adapter 的既有呼叫面必須被抓到，regex 失效時這裡先炸。
    assert {"order", "market_open", "modify_order", "cancel", "update_leverage"} <= called
    explicit = {
        name for name, member in vars(ResilientExchange).items()
        if callable(member) and not name.startswith("_")
    }
    unwrapped = called - explicit - PASSTHROUGH_WHITELIST
    assert unwrapped == set(), (
        f"這些 self._exchange 呼叫未被 ResilientExchange 顯式包裝、也不在透傳白名單："
        f"{sorted(unwrapped)}——交易寫入必須在 ResilientExchange 加顯式包裝方法；"
        f"唯讀/一次性動作才可加入 PASSTHROUGH_WHITELIST 並附理由")


def test_no_stray_retry_helper_outside_resilience():
    """_is_transient_error 是本邊界唯一的錯誤分類器；若在 resilience.py 之外的
    原始碼再次出現，代表有第二個平行的重試實作長出來——結構守門，禁止。"""
    offenders = [
        str(p.relative_to(SRC))
        for p in SRC.rglob("*.py")
        if p.name != "resilience.py" and "_is_transient_error" in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"重試分類邏輯應只在 resilience.py，發現殘留: {offenders}"
