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


@dataclass(frozen=True)
class OpenOrder:
    oid: int
    coin: str
    is_buy: bool
    limit_px: Decimal
    sz: Decimal
    reduce_only: bool
    is_trigger: bool
    trigger_px: Decimal | None
    tpsl: str | None      # "tp" / "sl" / None


@dataclass(frozen=True)
class Position:
    coin: str
    szi: Decimal          # 有號：多正空負
    entry_px: Decimal
    leverage: int
    is_cross: bool
    unrealized_pnl: Decimal
    margin_used: Decimal


@dataclass(frozen=True)
class AccountSnapshot:
    account_value: Decimal
    total_margin_used: Decimal
    withdrawable: Decimal
    total_ntl_pos: Decimal


@dataclass(frozen=True)
class EquityView:
    """回撤判定用：current 與 recent_peak 必須出自同一次 portfolio 回應（同源比較）。"""
    current: Decimal
    recent_peak: Decimal


@dataclass(frozen=True)
class UserFill:
    time: datetime
    coin: str
    px: Decimal
    sz: Decimal
    side: str             # "B" / "A"
    crossed: bool         # True = taker
    oid: int
    fee: Decimal


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
    @abstractmethod
    def get_open_orders(self, address: str) -> list[OpenOrder]: ...
    @abstractmethod
    def get_positions(self, address: str) -> list[Position]: ...
    @abstractmethod
    def get_account_state(self, address: str) -> AccountSnapshot: ...
    @abstractmethod
    def get_equity_view(self, address: str) -> EquityView: ...
    @abstractmethod
    def get_user_fills(self, address: str, start: datetime, end: datetime) -> list[UserFill]: ...
    @abstractmethod
    def get_all_mids(self) -> dict[str, Decimal]: ...
    @abstractmethod
    def get_size_decimals(self, coin: str) -> int: ...

    # --- writes（approve_* 概念上屬主錢包；Phase 1 testnet 由 test harness 簽）---
    @abstractmethod
    def approve_builder_fee(self, main_signer: Signer, builder: str, max_rate: str) -> TxResult: ...
    @abstractmethod
    def approve_agent(self, main_signer: Signer, agent_name: str) -> TxResult: ...
    @abstractmethod
    def place_order(self, agent_signer: Signer, order: Order, builder: BuilderCode) -> OrderResult: ...
