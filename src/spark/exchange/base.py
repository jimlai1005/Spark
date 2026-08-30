"""核心型別與 ExchangeAdapter 抽象介面。刻意不含任何 withdraw/transfer（非託管不變量）。"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

# Signer：keystore 回傳、可被 adapter 拿去簽 EIP-712 的物件（Phase 1 為 eth_account LocalAccount）。
Signer = Any

# Hyperliquid `userFillsByTime` 的單次回傳上限。達到此筆數 ⇒ 視窗內還有沒被回傳的成交。
USER_FILLS_PAGE_LIMIT = 2000


class FillsTruncatedError(RuntimeError):
    """`get_user_fills` 回傳筆數達 API 單次上限 ⇒ 視窗內的成交**沒被取全**。

    ⭐ 為什麼要拋而不是照樣回傳：截斷後的清單看起來完全正常——筆數少、名目小，
    沒有任何一個呼叫端能從結果本身看出它不完整。而每一個下游用途對「偏低」的
    反應都恰好是最危險的那個：成本熔斷器的換手率分子被低估 ⇒ **該擋的不擋**；
    builder fee 匯總少算 ⇒ 對帳數字錯得毫無徵兆。靜默低估的後果是保護在最該
    作用時失效（工程原則 3：安全關鍵路徑不得吞掉失敗）。

    語意錯誤（不是暫時性）：重試同一個窗口只會再拿回同樣被截斷的 2000 筆，
    所以訊息刻意不含 `resilience._TRANSIENT_MARKERS`，且不嵌入位址／時間戳
    （十六進位位址可能剛好含 "503" 之類的字串而被誤判成暫時性錯誤）。
    正確的處置是縮小查詢窗口或分頁，不是重試（工程原則 2）。

    既有呼叫端（`filet.aggregate.collect_follower_summary`、
    `publicapi.ops` 的成交品質列）都已把 `get_user_fills` 的例外轉成明確的
    「error／未知」欄位而非 0，所以拋出在那些路徑上是升級成「說不知道」，
    不是新的崩潰面。
    """


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
    # 下單原量（frontendOpenOrders 的 origSz）；sz 是剩餘量。2026-07-28 事故修法：
    # 兩者的差是「部分成交或被交易所修剪」的觀測點，CRIT 告警靠它顯示原量。
    # 缺鍵 → None（不得假造 0 或抄 sz）。
    orig_sz: Decimal | None = None


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
    builder_fee: Decimal = Decimal("0")   # 該筆成交歸屬我方 builder 的費用（HL fills 的 builderFee；無此欄位視為 0）
    # M3 round3 Task 2b：加法擴充（optional，預設 None），不改變任何既有讀者的行為
    # ——舊 code 完全不讀這個欄位。HL fills 的 closedPnl；缺欄／解析不到 → None
    # （不是 0——沒資料就是沒資料，讀者不可把 None 當「當筆已實現 0」處理）。
    closed_pnl: Decimal | None = None


@dataclass(frozen=True)
class LedgerFlow:
    """申贖流量紀錄：入金 (+usdc) 或出金 (-usdc)。"""
    time_ms: int
    usdc: Decimal


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
    def query_user_abstraction(self, user: str) -> str:
        """地址的 account abstraction 模式（HL `userAbstraction`）——**唯讀**。

        為什麼 adapter 需要這個查詢：HL 官方文件明載 builder code 生效的條件是
        「**the builder** must have at least 100 USDC in perps account value and
        must use standard as the account abstraction mode」。主詞是 builder 而非
        客戶，而模式若不是 standard，builder fee 會**無聲**停止累積——成交照常、
        客戶無感、只有收入曲線變平。這是營運層的唯一觀測點（見
        `spark.filet.builder_compliance`）。

        回傳交易所的**原始模式字串**，不在此判定合規：判定門檻屬營運政策，
        住在 `builder_compliance`（單一定義）。無法解讀的回應形狀 → ValueError，
        由呼叫端轉成「未知 → 告警」，絕不回一個看起來合規的預設值。
        """
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
    @abstractmethod
    def get_ledger_flows(self, address: str, start_ms: int) -> tuple[list[LedgerFlow], list[str]]:
        """帳戶的申贖流量（起始時間毫秒起）。

        回傳 (流量清單, 白名單外的 delta type 名稱清單——去重、穩定排序)。
        白名單 delta type：vaultDeposit, deposit, withdraw, vaultWithdraw。
        呼叫端負責判定未知型別是否觸發告警。
        """
        ...
    @abstractmethod
    def get_active_asset_leverage(self, address: str, coin: str) -> tuple[int, bool]:
        """查詢帳戶對該幣的槓桿設定。

        即便該幣無持倉（空手），API 仍回傳該帳戶現行的槓桿設定。
        走 HL `activeAssetData` endpoint，對空手幣也能查得到。

        回傳 (槓桿倍數, is_cross)。is_cross=True 時為全倉、False 為逐倉。

        回應缺鍵或形狀不符 → raise ValueError（含幣名於訊息），
        呼叫端負責捕獲後降級與告警。
        """
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
