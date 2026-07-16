"""hyperliquid-python-sdk 實作。Info/Exchange 可注入以便測試。
方法名以 Task 0 findings 為準。"""
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, Context, ROUND_HALF_EVEN
from spark.config import CSV_BASE_URLS
from spark.exchange.base import (
    ExchangeAdapter, Order, BuilderCode, Fill, TxResult, OrderResult, Signer,
    OpenOrder, Position, AccountSnapshot, EquityView, UserFill,
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
        # get_size_decimals 的 per-coin 快取：None 代表尚未打過 meta()；打過之後即便
        # 查無某 coin 也不重打（避免對不存在的 coin 反覆打 API）。
        self._sz_decimals_cache: dict[str, int] | None = None

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
        # stats-data S3 key 為小寫；checksum（mixed-case）地址在 URL 上會 403 → 誤判無 fills。
        url = f"{CSV_BASE_URLS[self._network]}/{builder.lower()}/{day:%Y%m%d}.csv.lz4"
        try:
            raw = urllib.request.urlopen(url, timeout=30).read()
        except urllib.error.HTTPError as e:
            # 該日無成交 → S3 回 403/404（無此 key）。視為「無 fills」回空清單，
            # 讓 reconcile 產出 matched=False 的誠實報告，而非拋例外。
            if e.code in (403, 404):
                return []
            raise
        return parse_builder_fills(raw, compressed=True)

    # --- reads（copytrade M1）---
    @staticmethod
    def _tpsl_from_order_type(order_type_name: str) -> str | None:
        # frontendOpenOrders 回應無字面 "tpsl" 鍵（已與 SDK docstring 及 Hyperliquid 官方文件
        # 交叉確認：欄位只有 orderType 這種人類可讀字串，如 "Limit"/"Stop Market"/
        # "Take Profit Limit"）。tpsl 分類需從 orderType 文字判讀衍生，1:1 忠實移植自
        # hl-copytrader src/monitor.py:187-191 的 _parse_orders（含 startswith("tp") 與
        # "sl" in low 兩個析取項——HL 前端若改字串格式，行為須與線上引擎一致）。
        low = order_type_name.lower()
        if "take profit" in low or low.startswith("tp"):
            return "tp"
        if "stop" in low or "sl" in low:
            return "sl"
        return None

    def get_open_orders(self, address: str) -> list[OpenOrder]:
        raw = self._info.frontend_open_orders(address)
        orders = []
        for o in raw:
            is_trigger = bool(o.get("isTrigger", False))
            orders.append(OpenOrder(
                oid=o["oid"],
                coin=o["coin"],
                is_buy=o["side"] == "B",
                limit_px=Decimal(str(o["limitPx"])),
                sz=Decimal(str(o["sz"])),
                reduce_only=bool(o.get("reduceOnly", False)),
                is_trigger=is_trigger,
                # 非 trigger 一律映射 None（即便 raw 是 "0.0" 或缺鍵）。
                trigger_px=Decimal(str(o.get("triggerPx", "0"))) if is_trigger else None,
                tpsl=self._tpsl_from_order_type(o.get("orderType", "")) if is_trigger else None,
            ))
        return orders

    def get_positions(self, address: str) -> list[Position]:
        state = self._info.user_state(address)
        positions = []
        for item in state.get("assetPositions", []):
            pos = item["position"]
            szi = Decimal(str(pos["szi"]))
            if szi == 0:
                continue
            leverage = pos.get("leverage") or {}
            entry_px_raw = pos.get("entryPx")
            positions.append(Position(
                coin=pos["coin"],
                szi=szi,
                entry_px=Decimal(str(entry_px_raw)) if entry_px_raw is not None else Decimal("0"),
                leverage=int(leverage.get("value", 1)),
                is_cross=(leverage.get("type") == "cross"),
                unrealized_pnl=Decimal(str(pos.get("unrealizedPnl", "0"))),
                margin_used=Decimal(str(pos.get("marginUsed", "0"))),
            ))
        return positions

    def get_account_state(self, address: str) -> AccountSnapshot:
        state = self._info.user_state(address)
        ms = state["marginSummary"]
        return AccountSnapshot(
            account_value=Decimal(str(ms["accountValue"])),
            total_margin_used=Decimal(str(ms["totalMarginUsed"])),
            withdrawable=Decimal(str(state["withdrawable"])),
            total_ntl_pos=Decimal(str(ms["totalNtlPos"])),
        )

    def get_equity_view(self, address: str) -> EquityView:
        """回撤判定用 current/recent_peak；同源不變量＝兩者出自單一次 portfolio() 呼叫。

        語意移植自 hl-copytrader src/monitor.py:124-158（get_account_equity），非簡單的
        「單一時間窗序列的 last/max」：portfolio() 回傳 [period, {accountValueHistory:[[ts,val],...]}]
        的清單，涵蓋 day/week/month/allTime 四個時間窗。
        - current = 掃描全部四個時間窗後，時間戳最新那一點的值（不侷限於 day）。
        - recent_peak = 只在 "week" 時間窗序列中取最大值；查無 week 資料則退回 current
          （避免把「沒有 peak 資料」誤判成「peak 低於 current」的假回撤）。
        """
        rows = self._info.portfolio(address)
        total_periods = {"day", "week", "month", "allTime"}
        current = Decimal("0")
        peak = Decimal("0")
        latest_ts = Decimal("-1")
        for row in rows:
            if not (isinstance(row, list) and len(row) == 2 and row[0] in total_periods):
                continue
            period, payload = row
            for ts, val in payload.get("accountValueHistory", []):
                v = Decimal(str(val))
                if period == "week":
                    peak = max(peak, v)
                ts_dec = Decimal(str(ts))
                if ts_dec > latest_ts:
                    latest_ts = ts_dec
                    current = v
        if peak <= 0:
            peak = current
        return EquityView(current=current, recent_peak=peak)

    _EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

    @staticmethod
    def _to_ms_utc(dt: datetime) -> int:
        # naive datetime 視為 UTC（本 adapter 的呼叫端慣例）；aware datetime 一律先轉 UTC
        # 再取 epoch，避免用本機時區誤解讀。
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # 純整數運算（timedelta // timedelta 精確）：timestamp()*1000 走 float 會有 ±1ms
        # 捨入偏差，違反「無 float 中間值」紅線。
        return (dt - HyperliquidAdapter._EPOCH) // timedelta(milliseconds=1)

    def get_user_fills(self, address: str, start: datetime, end: datetime) -> list[UserFill]:
        raw = self._info.user_fills_by_time(
            address, self._to_ms_utc(start), self._to_ms_utc(end)
        )
        fills = []
        for f in raw:
            fills.append(UserFill(
                time=datetime.fromtimestamp(f["time"] / 1000, tz=timezone.utc),
                coin=f["coin"],
                px=Decimal(str(f["px"])),
                sz=Decimal(str(f["sz"])),
                side=f["side"],
                crossed=bool(f["crossed"]),
                oid=f["oid"],
                fee=Decimal(str(f.get("fee", "0"))),
            ))
        return fills

    def get_all_mids(self) -> dict[str, Decimal]:
        raw = self._info.all_mids()
        # "@" 開頭的 key 是 spot/index 內部標記（非 perp 幣名），M1 只要 perp 幣名。
        return {k: Decimal(str(v)) for k, v in raw.items() if not k.startswith("@")}

    def get_size_decimals(self, coin: str) -> int:
        if self._sz_decimals_cache is None:
            meta = self._info.meta()
            self._sz_decimals_cache = {
                u["name"]: int(u["szDecimals"]) for u in meta["universe"]
            }
        if coin not in self._sz_decimals_cache:
            raise ValueError(f"get_size_decimals: 未知幣種 {coin!r}（不在 meta() universe 內）")
        return self._sz_decimals_cache[coin]

    # --- writes ---
    # 以下 main_signer / agent_signer 參數為介面文件性質；實際簽章者 = 建構時綁定
    # self._exchange 的錢包。onboarding 必須注入 main-bound Exchange 呼叫
    # approve_builder_fee/approve_agent（協議層亦會拒絕 agent 代簽 approve）。
    def approve_builder_fee(self, main_signer: Signer, builder: str, max_rate: str) -> TxResult:
        res = self._exchange.approve_builder_fee(builder, max_rate)
        return TxResult(ok=res.get("status") == "ok", raw=res)

    def approve_agent(self, main_signer: Signer, agent_name: str) -> TxResult:
        # SDK 語意：生成一把新 agent key 並以 main 錢包簽名授權（named agent）。
        # 重複呼叫會 rotate key —— 是否呼叫由 onboarding 依「是否已有 key」決定。
        res, agent_key = self._exchange.approve_agent(agent_name)
        return TxResult(ok=res.get("status") == "ok", raw=res, agent_key=agent_key)

    def place_order(self, agent_signer: Signer, order: Order, builder: BuilderCode) -> OrderResult:
        res = self._exchange.order(
            order.coin, order.is_buy, float(order.size), self._round_px(order.limit_px),
            {"limit": {"tif": order.tif}}, reduce_only=False,
            builder={"b": builder.b, "f": builder.f},
        )
        # 被拒單（IOC 未成交、保證金不足等）是正常結果而非例外：HL 回 {"status":"err",...}，
        # 直接挖 response.data 會 TypeError。先檢查 status，非 ok 則回 ok=False（原始回應留 raw）。
        if res.get("status") != "ok":
            return OrderResult(ok=False, filled_size=Decimal("0"), avg_px=Decimal("0"), raw=res)
        status = res["response"]["data"]["statuses"][0].get("filled", {})
        return OrderResult(
            ok=bool(status),
            filled_size=Decimal(status.get("totalSz", "0")),
            avg_px=Decimal(status.get("avgPx", "0")),
            raw=res,
        )
