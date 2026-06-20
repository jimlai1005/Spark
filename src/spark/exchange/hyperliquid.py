"""hyperliquid-python-sdk 實作。Info/Exchange 可注入以便測試。
方法名以 Task 0 findings 為準。"""
import urllib.request
from datetime import date
from decimal import Decimal, Context, ROUND_HALF_EVEN
from spark.config import CSV_BASE_URLS
from spark.exchange.base import (
    ExchangeAdapter, Order, BuilderCode, Fill, TxResult, OrderResult, Signer,
)
from spark.exchange.csv_fills import parse_builder_fills


class HyperliquidAdapter(ExchangeAdapter):
    # HL perp 價格規則：最多 5 位有效數字。送單前必須四捨五入，否則交易所拒單。
    # （Phase 1 ETH ~數千元，5 sig figs 同時滿足小數位上限；極低價幣種的 tick 細則延後。）
    _PX_CTX = Context(prec=5, rounding=ROUND_HALF_EVEN)

    def __init__(self, network: str, info=None, exchange=None):
        self._network = network
        self._info = info        # hyperliquid.info.Info
        self._exchange = exchange  # hyperliquid.exchange.Exchange（已綁 agent 錢包）

    def _round_px(self, px: Decimal) -> float:
        """把 orchestrator 算出的意圖價四捨五入到 HL 接受的格式（5 位有效數字）。"""
        return float(self._PX_CTX.create_decimal(px))

    # --- reads ---
    def get_account_value(self, address: str) -> Decimal:
        state = self._info.user_state(address)
        return Decimal(state["marginSummary"]["accountValue"])

    def query_max_builder_fee(self, user: str, builder: str) -> int:
        # Task 0 findings: SDK 無 wrapper，需 raw post（回傳 int，十分之一 bp）
        return int(self._info.post("/info", {"type": "maxBuilderFee",
                                             "user": user, "builder": builder}))

    def query_builder_accrued(self, builder: str) -> Decimal:
        # Task 0 findings: 累計 builder fee = referral state 的 builderRewards
        state = self._info.query_referral_state(builder)
        return Decimal(str(state["builderRewards"]))

    def fetch_builder_fills(self, builder: str, day: date) -> list[Fill]:
        url = f"{CSV_BASE_URLS[self._network]}/{builder}/{day:%Y%m%d}.csv.lz4"
        raw = urllib.request.urlopen(url, timeout=30).read()
        return parse_builder_fills(raw, compressed=True)

    # --- writes ---
    def approve_builder_fee(self, main_signer: Signer, builder: str, max_rate: str) -> TxResult:
        res = self._exchange.approve_builder_fee(builder, max_rate)
        return TxResult(ok=res.get("status") == "ok", raw=res)

    def approve_agent(self, main_signer: Signer, agent_address: str) -> TxResult:
        res, _agent_key = self._exchange.approve_agent()
        return TxResult(ok=res.get("status") == "ok", raw=res)

    def place_order(self, agent_signer: Signer, order: Order, builder: BuilderCode) -> OrderResult:
        res = self._exchange.order(
            order.coin, order.is_buy, float(order.size), self._round_px(order.limit_px),
            {"limit": {"tif": order.tif}}, reduce_only=False,
            builder={"b": builder.b, "f": builder.f},
        )
        status = res["response"]["data"]["statuses"][0].get("filled", {})
        return OrderResult(
            ok=bool(status),
            filled_size=Decimal(status.get("totalSz", "0")),
            avg_px=Decimal(status.get("avgPx", "0")),
            raw=res,
        )
