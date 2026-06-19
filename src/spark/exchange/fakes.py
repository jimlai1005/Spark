"""可編程的離線假交易所，記錄所有呼叫供斷言。"""
from collections import defaultdict
from datetime import date
from decimal import Decimal
from spark.exchange.base import (
    ExchangeAdapter, Order, BuilderCode, Fill, TxResult, OrderResult,
)


class FakeAdapter(ExchangeAdapter):
    """離線假交易所（Phase 1：單 builder、單帳號）。

    刻意用單一狀態槽，非 per-builder/per-user map —— Phase 1 範圍只有一個 builder
    與一個帳號（多客戶為 non-goal）。多情境測試請各自 new 一個 FakeAdapter。
    `approve_builder_fee` 不解析 max_rate，固定把 _max_fee 設為 100（代表 0.1% 上限）；
    max_rate 原值仍記錄在 self.calls 供斷言。
    """
    def __init__(self, account_value=Decimal("0"), seeded_fills=None):
        self._account_value = Decimal(account_value)
        self._max_fee = 0
        self._accrued = Decimal("0")
        self._seeded_fills = list(seeded_fills or [])
        self.calls = defaultdict(list)

    def get_account_value(self, address: str) -> Decimal:
        return self._account_value

    def query_max_builder_fee(self, user: str, builder: str) -> int:
        return self._max_fee

    def query_builder_accrued(self, builder: str) -> Decimal:
        return self._accrued

    def fetch_builder_fills(self, builder: str, day: date) -> list[Fill]:
        return list(self._seeded_fills)

    def approve_builder_fee(self, main_signer, builder, max_rate) -> TxResult:
        self.calls["approve_builder_fee"].append(
            {"main_signer": main_signer, "builder": builder, "max_rate": max_rate})
        self._max_fee = 100  # 固定代表 0.1%；不解析 max_rate（見 class docstring）
        return TxResult(ok=True, raw={"status": "ok"})

    def approve_agent(self, main_signer, agent_address) -> TxResult:
        self.calls["approve_agent"].append(
            {"main_signer": main_signer, "agent_address": agent_address})
        return TxResult(ok=True, raw={"status": "ok"})

    def place_order(self, agent_signer, order: Order, builder: BuilderCode) -> OrderResult:
        self.calls["place_order"].append(
            {"agent_signer": agent_signer, "order": order, "builder": builder})
        notional = order.size * order.limit_px
        self._accrued += notional * Decimal(builder.f) / Decimal(100000)  # f/1000 % = f/100000
        return OrderResult(ok=True, filled_size=order.size, avg_px=order.limit_px,
                           raw={"status": "filled"})
