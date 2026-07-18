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
    reduce_only: bool = False


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
    # 帶預設值加在尾端（既有建構點不壞）。語意 1:1 對照 hl-copytrader monitor.py:184-186/205。
    is_market: bool = False   # 僅 trigger 單有意義："Market" in orderType；非 trigger 恆 False
    tif: str = "Gtc"          # API 回應 tif 欄位；缺鍵/None → "Gtc"


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
    """回撤判定用：current 與 recent_peak 必須同源同單位（工程原則 1）。

    2026-07-19 起引擎改由 `spark.copytrade.equity.perp_equity_view()` 產生本型別：
    current = perp accountValue 即時值、recent_peak = 同一欄位的本地滾動樣本最大值
    ——同一量測方式的時間序列，仍滿足同源。`HyperliquidAdapter.get_equity_view()`
    的 portfolio 版本仍存在但引擎不再使用（其值含 spot，會稀釋回撤保護，findings F1）。
    """
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
    def query_agent_addresses(self, user: str) -> list[str]:
        """使用者鏈上目前已授權的 agent 地址清單（extraAgents），小寫正規化供同基準比對
        （工程原則 1）。onboarding 用此查詢判定本機持有的 agent key 是否仍是當前有效
        授權——查詢驅動而非信任呼叫端旗標，才是真正冪等（B1-F1 修正）。"""
        ...
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
    @abstractmethod
    def get_daily_abs_pnl(self, address: str) -> list[Decimal]:
        """帳戶每日 |PnL| 數列，時間升冪、最後一筆為今日。供 sizing.compute_volatility_stats
        的呼叫端注入使用（Task 12 loop.py）。"""
        ...

    # --- writes（approve_* 概念上屬主錢包；Phase 1 testnet 由 test harness 簽）---
    @abstractmethod
    def approve_builder_fee(self, main_signer: Signer, builder: str, max_rate: str) -> TxResult: ...
    @abstractmethod
    def approve_agent(self, main_signer: Signer, agent_name: str) -> TxResult: ...
    @abstractmethod
    def place_order(self, agent_signer: Signer, order: Order, builder: BuilderCode) -> OrderResult: ...
    @abstractmethod
    def cancel_order(self, agent_signer: Signer, coin: str, oid: int) -> bool: ...
    @abstractmethod
    def modify_order(self, agent_signer: Signer, oid: int, order: Order) -> bool:
        """改單。⭐ 已知例外：hyperliquid-python-sdk 0.24.0 的 modify_order() 簽章無 builder
        參數（結構限制，非本專案疏漏）——改單走 batchModify action，SDK 該層未接 builder
        欄位。故本方法**不帶** builder，紅線「order-creating writes 全帶 builder」在此不適用
        （改單本身不建立新訂單意圖，只調整既有掛單的價格/數量）。"""
        ...
    @abstractmethod
    def market_open(self, agent_signer: Signer, coin: str, is_buy: bool, size: Decimal,
                    slippage: Decimal, builder: BuilderCode) -> OrderResult: ...
    @abstractmethod
    def close_reduce_only(self, agent_signer: Signer, coin: str, is_buy: bool, size: Decimal,
                          slippage: Decimal, builder: BuilderCode) -> OrderResult:
        """Reduce-only IOC 平倉單。

        語意鎖死（呼叫端責任，adapter 不反轉方向）：
        - `is_buy` = 平倉**下單方向**，不是持倉方向。呼叫端自行算好
          `is_buy = not position_is_long`（平多倉 → is_buy=False；平空倉 → is_buy=True）
          再傳入；adapter 原樣傳給 SDK，不做任何反轉。
        - mid 來源固定為 `get_all_mids()`（主 perp DEX 的當前 mid）。
        - 價格：`px = mid * (1 + slippage)`（is_buy=True）或 `mid * (1 - slippage)`
          （is_buy=False），全程 Decimal 運算後才用 `_round_px` 轉 float。
        - 送 SDK：`order(coin, is_buy, float(size), px, {"limit": {"tif": "Ioc"}},
          reduce_only=True, builder={"b":..., "f":...})`。
        - 取不到 mid（coin 不在 `get_all_mids()` 回傳的 dict 內）→ 回
          `OrderResult(ok=False, filled_size=0, avg_px=0, raw={"error": "no mid for <coin>"})`，
          **不 raise**；呼叫端負責看到 ok=False 後告警。
        """
        ...
    @abstractmethod
    def update_leverage(self, agent_signer: Signer, coin: str, leverage: int,
                        is_cross: bool) -> bool: ...
