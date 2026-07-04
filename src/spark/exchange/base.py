"""核心型別與 ExchangeAdapter 抽象介面。刻意不含任何 withdraw/transfer（非託管不變量）。"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

# Signer：keystore 回傳、可被 adapter 拿去簽 EIP-712 的物件（Phase 1 為 eth_account LocalAccount）。
Signer = Any


@dataclass(frozen=True)
class BuilderCode:
    b: str   # builder 地址
    f: int   # 十分之一 bp（Phase 1 = 20）


@dataclass(frozen=True)
class Order:
    coin: str
    is_buy: bool
    size: Decimal
    limit_px: Decimal
    tif: str  # "Ioc"


@dataclass(frozen=True)
class Fill:
    time: datetime
    coin: str
    px: Decimal
    sz: Decimal
    side: str
    builder_fee: Decimal


@dataclass(frozen=True)
class TxResult:
    ok: bool
    raw: dict
    # approve_agent 生成的 agent 私鑰（僅該動作使用）。repr=False：絕不出現在 log/repr。
    agent_key: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class OrderResult:
    ok: bool
    filled_size: Decimal
    avg_px: Decimal
    raw: dict


class ExchangeAdapter(ABC):
    # --- reads ---
    @abstractmethod
    def get_account_value(self, address: str) -> Decimal: ...
    @abstractmethod
    def query_max_builder_fee(self, user: str, builder: str) -> int: ...
    @abstractmethod
    def query_builder_accrued(self, builder: str) -> Decimal: ...
    @abstractmethod
    def fetch_builder_fills(self, builder: str, day: date) -> list[Fill]: ...

    # --- writes（approve_* 概念上屬主錢包；Phase 1 testnet 由 test harness 簽）---
    @abstractmethod
    def approve_builder_fee(self, main_signer: Signer, builder: str, max_rate: str) -> TxResult: ...
    @abstractmethod
    def approve_agent(self, main_signer: Signer, agent_name: str) -> TxResult: ...
    @abstractmethod
    def place_order(self, agent_signer: Signer, order: Order, builder: BuilderCode) -> OrderResult: ...
