"""hyperliquid-python-sdk 實作。Info/Exchange 可注入以便測試。
方法名以 Task 0 findings 為準。"""
import logging
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, Context, ROUND_HALF_EVEN
from spark.config import CSV_BASE_URLS
from spark.exchange.base import (
    USER_FILLS_PAGE_LIMIT, ExchangeAdapter, FillsTruncatedError, Order, BuilderCode,
    Fill, TxResult, OrderResult, Signer,
    OpenOrder, Position, AccountSnapshot, EquityView, UserFill,
)
from spark.exchange.csv_fills import parse_builder_fills
from spark.resilience import ResilientExchange

logger = logging.getLogger(__name__)


class HyperliquidAdapter(ExchangeAdapter):
    # HL perp 價格規則：最多 5 位有效數字。送單前必須四捨五入，否則交易所拒單。
    # （Phase 1 ETH ~數千元，5 sig figs 同時滿足小數位上限；極低價幣種的 tick 細則延後。）
    _PX_CTX = Context(prec=5, rounding=ROUND_HALF_EVEN)

    def __init__(self, network: str, info=None, exchange=None):
        self._network = network
        self._info = info        # hyperliquid.info.Info
        # 所有交易寫入經 resilience 邊界（src/spark/resilience.py）分類重試；
        # 僅當尚未包裝時才包，避免重複呼叫本建構子造成雙層包裝。
        if exchange is not None and not isinstance(exchange, ResilientExchange):
            exchange = ResilientExchange(exchange)
        self._exchange = exchange  # ResilientExchange 包住 hyperliquid.exchange.Exchange
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

    def query_user_abstraction(self, user: str) -> str:
        """`userAbstraction` 的原始模式字串（唯讀）。語意見 ABC docstring。

        SDK 有 wrapper：`hyperliquid/info.py:634` 的 `query_user_abstraction_state`
        → `POST /info {"type": "userAbstraction", "user": ...}`（已對照 .venv 內
        原始碼查證，非憑印象）。

        ⭐ 回應形狀防禦：實測回的是裸字串（如 `"disabled"`），但 SDK 的型別註記是
        `Any`。若哪天變成物件，`str(raw)` 會產出 `"{'mode': ...}"` 這種**永遠不合規**
        的字串——方向是安全的（誤報勝於漏報），但訊息會很難懂，所以顯式挑幾個可能的
        鍵；都不中就 raise，由呼叫端轉成「未知 → 告警」。**絕不**回一個預設值：
        對一個會無聲斷掉營收的條件，「查不到就當它沒事」是最糟的失效方向。
        """
        raw = self._info.query_user_abstraction_state(user)
        if isinstance(raw, str):
            return raw
        if isinstance(raw, dict):
            for k in ("userAbstraction", "abstraction", "mode"):
                v = raw.get(k)
                if isinstance(v, str):
                    return v
        raise ValueError(
            f"userAbstraction 回應形狀無法解讀（{type(raw).__name__}）——"
            "不猜測模式，請人工檢查上游 schema")

    def query_agent_addresses(self, user: str) -> list[str]:
        # SDK 無 wrapper，需 raw post（同 query_max_builder_fee 的 Task 0 findings）。
        # 正規化小寫：1:1 對照 publicapi/hl.py 的 agent_addresses——同一條鏈上事實
        # （extraAgents）由兩個獨立模組查詢，正規化方式須一致才能同基準比對（工程原則 1）。
        agents = self._info.post("/info", {"type": "extraAgents", "user": user})
        return [a["address"].lower() for a in agents if a.get("address")]

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
            order_type_name = o.get("orderType", "Limit")
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
                tpsl=self._tpsl_from_order_type(order_type_name) if is_trigger else None,
                # 1:1 hl monitor.py:184-186——非 trigger 恆 False；trigger 看 orderType 字串。
                is_market=("Market" in order_type_name) if is_trigger else False,
                # 1:1 hl monitor.py:205——API tif 欄位，缺鍵/None → "Gtc"。
                tif=o.get("tif") or "Gtc",
                # 下單原量（sz 是剩餘量）；缺鍵 → None，不假造。
                orig_sz=Decimal(str(o["origSz"])) if o.get("origSz") is not None else None,
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
            # SDK 保證存在的欄位一律直接下標：schema 漂移時大聲 KeyError，
            # 絕不靜默給預設值（工程原則 3：財務資料寧可炸不可給錯值）。
            # entryPx 例外——文件記載可為 None（合法值），顯式映射 Decimal("0")。
            leverage = pos["leverage"]
            entry_px_raw = pos["entryPx"]
            positions.append(Position(
                coin=pos["coin"],
                szi=szi,
                entry_px=Decimal(str(entry_px_raw)) if entry_px_raw is not None else Decimal("0"),
                leverage=int(leverage["value"]),
                is_cross=(leverage["type"] == "cross"),
                unrealized_pnl=Decimal(str(pos["unrealizedPnl"])),
                margin_used=Decimal(str(pos["marginUsed"])),
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
        """視窗內的成交。**達 API 單次上限即拋 FillsTruncatedError，絕不靜默回傳半份。**

        `userFillsByTime` 單次最多回 `USER_FILLS_PAGE_LIMIT` 筆且不提供「還有更多」
        的旗標，所以「剛好回滿上限」是唯一能觀測到的截斷徵兆——我們把它一律當成
        截斷（回滿上限而恰好沒有第 2001 筆的情形極少，且誤判的代價只是呼叫端說
        「不知道」，遠低於靜默低估的代價）。

        刻意**不做分頁**：分頁需要「回傳依時間升冪」與「以 tid 去重」兩個對 HL API
        的假設，本 repo 全離線測試無法對真實 API 驗證；假設錯的方向是重複計入 ⇒
        換手率分子被**灌大** ⇒ 誤觸成本熔斷 ⇒ 累犯升級 ⇒ 強制平掉客戶部位。
        拿一個未驗證的假設去換「少呼叫幾次」，賠率不對。要拉長窗口請由呼叫端切段。
        """
        raw = self._info.user_fills_by_time(
            address, self._to_ms_utc(start), self._to_ms_utc(end)
        )
        if len(raw) >= USER_FILLS_PAGE_LIMIT:
            # 訊息不含位址／時間戳：見 FillsTruncatedError（避免被誤分類為 transient）。
            raise FillsTruncatedError(
                f"成交查詢回傳筆數達單次上限（{len(raw)} 筆）——視窗內的成交未取全，"
                f"任何以此為分子的計算都會低估；請縮小查詢窗口"
            )
        fills = []
        for f in raw:
            fills.append(UserFill(
                # 純整數 ms→datetime（與 _to_ms_utc 鏡像一致），不走 float 除法。
                time=self._EPOCH + timedelta(milliseconds=f["time"]),
                coin=f["coin"],
                px=Decimal(str(f["px"])),
                sz=Decimal(str(f["sz"])),
                side=f["side"],
                crossed=bool(f["crossed"]),
                oid=f["oid"],
                fee=Decimal(str(f.get("fee", "0"))),
                builder_fee=Decimal(str(f.get("builderFee", "0") or "0")),
            ))
        return fills

    def get_daily_abs_pnl(self, address: str) -> list[Decimal]:
        """帳戶每日 |PnL| 數列（時間升冪，最後一筆＝今日）。唯讀對照 hl-copytrader
        weight.py:82-98（`_daily_abs_pnl`）：從 portfolio 回應的 "month" 時間窗
        `pnlHistory`（累積 PnL 序列）推每日 |PnL| = 相鄰累積值之差的絕對值。

        分桶依 ts（毫秒）轉整數天（`_EPOCH` + `timedelta(milliseconds=...)`，與
        `get_user_fills` 同一套「無 float 中間值」慣例）；同日多筆取最後一筆
        （1:1 hl 版 `OrderedDict` 覆寫語意，依賴來源已按 ts 升冪排列）。
        查無 "month" 列或 pnlHistory 為空 → 回空清單（hl weight.py:90-91 同語意）。
        """
        rows = self._info.portfolio(address)
        pnl_hist = None
        for row in rows:
            if isinstance(row, list) and len(row) == 2 and row[0] == "month":
                pnl_hist = row[1].get("pnlHistory", [])
                break
        if not pnl_hist:
            return []
        by_day: dict = {}
        for ts, val in pnl_hist:
            day = (self._EPOCH + timedelta(milliseconds=int(ts))).date()
            by_day[day] = Decimal(str(val))
        cum = list(by_day.values())
        return [abs(cum[i] - cum[i - 1]) for i in range(1, len(cum))]

    def get_all_mids(self) -> dict[str, Decimal]:
        raw = self._info.all_mids()
        # "@" 開頭的 key 是 spot/index 內部標記（非 perp 幣名），M1 只要 perp 幣名。
        return {k: Decimal(str(v)) for k, v in raw.items() if not k.startswith("@")}

    def get_size_decimals(self, coin: str) -> int:
        # 自癒快取：已知 coin 走快取不重打；cache miss（含首次與新上市幣）重打一次
        # meta() 更新整包快取，更新後仍查無才 raise。失敗不入快取——下次同 coin
        # 呼叫會再重試 meta，新幣上市後無需重啟即可查到。
        if self._sz_decimals_cache is None or coin not in self._sz_decimals_cache:
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

    def _parse_order_response(self, res: dict) -> OrderResult:
        """order()/market_open() 共用的回應解析。被拒單（IOC 未成交、保證金不足等）是
        正常結果而非例外，且 HL 有兩種錯誤形態（記載參照 hl-copytrader
        instrument.py:50-65，唯讀參考）：
        - 頂層錯誤（限流、簽名等）：{"status":"err","response":"<字串>"} —— response 是
          str，直接挖 data 會 TypeError，故先檢查 status。
        - 單筆委託錯誤：{"status":"ok",...,"statuses":[{"error":...}]} —— 頂層 ok
          不代表成功，statuses[0] 帶 "error" 鍵才是真相。
        成功終態兩種：filled（IOC 立即成交，有量有價）與 resting（GTC 掛上訂單簿，
        尚無成交 → ok=True 但 filled_size/avg_px 為 0）。Ioc 路徑不會出現 resting。
        第三種成功終態：resilience 邊界的 VERIFIED_OK 哨兵（連線中斷但 verify 確認
        已送達）——原始 SDK 回應已隨斷線遺失，無 statuses 可挖，必須先短路，
        否則會 KeyError（ok=True 但無成交明細；真相由下一輪對帳補齊）。"""
        if res.get("_resilience") == "verified":
            return OrderResult(ok=True, filled_size=Decimal("0"), avg_px=Decimal("0"), raw=res)
        if res.get("status") != "ok":
            return OrderResult(ok=False, filled_size=Decimal("0"), avg_px=Decimal("0"), raw=res)
        status = res["response"]["data"]["statuses"][0]
        if "resting" in status:
            return OrderResult(ok=True, filled_size=Decimal("0"), avg_px=Decimal("0"), raw=res)
        # 帶 "error" 鍵或其他未知形態 → filled 為空 dict → ok=False（原始回應留 raw）。
        filled = status.get("filled", {})
        return OrderResult(
            ok=bool(filled),
            filled_size=Decimal(filled.get("totalSz", "0")),
            avg_px=Decimal(filled.get("avgPx", "0")),
            raw=res,
        )

    def place_order(self, agent_signer: Signer, order: Order, builder: BuilderCode) -> OrderResult:
        # reduce_only 取自 Order 欄位（勿寫死 False——跟單鏡射的 leader reduce-only 掛單
        # 必須原樣傳遞，否則會變成可開新倉的普通掛單）。
        res = self._exchange.order(
            order.coin, order.is_buy, float(order.size), self._round_px(order.limit_px),
            {"limit": {"tif": order.tif}}, reduce_only=order.reduce_only,
            builder={"b": builder.b, "f": builder.f},
        )
        return self._parse_order_response(res)

    def cancel_order(self, agent_signer: Signer, coin: str, oid: int) -> bool:
        """撤單。回傳語意：False = **未確認撤單**——頂層 err 或單筆 error（如 oid 已成交/
        已撤/不存在）皆回 False，此時訂單可能仍掛在簿上；真相由上層 settle 重抓
        get_open_orders 驗證為準，本方法不代做對帳。"""
        res = self._exchange.cancel(coin, oid)
        if res.get("status") != "ok":
            return False
        # 單筆委託錯誤形態（同 _parse_order_response docstring）：頂層 ok 但
        # statuses[0] 是 {"error": ...}；成功時 statuses[0] 是字串 "success"。
        status = res["response"]["data"]["statuses"][0]
        if isinstance(status, dict) and "error" in status:
            logger.warning("cancel_order rejected: %s", status["error"])
            return False
        return True

    def modify_order(self, agent_signer: Signer, oid: int, order: Order) -> bool:
        # 2026-07-19 testnet 實測語意（scripts/testnet_modify_probe.py）：
        #   1. HL modify **會重新配發 oid**（改單前 oid≠成交 oid）；本方法只回 bool、
        #      刻意丟棄新 oid——安全性依賴呼叫端「每輪重讀 get_open_orders」（loop.py:94），
        #      這是結構性依賴，勿改成跨輪次快取 oid。
        #   2. modify **非原子**：被拒時舊單也可能已消失（batchModify 近似 cancel-then-place），
        #      故「回 False ⇒ 舊單仍在」是錯的假設。
        #   3. batchModify 帶 post-only 語意：改後的單只能掛著，無法在改單當下吃單成交。
        # ⭐ SDK 0.24.0 modify_order() 無 builder 參數（結構限制，見 ABC docstring 的
        # 紅線例外說明）——此呼叫刻意不帶 builder kwarg。
        res = self._exchange.modify_order(
            oid, order.coin, order.is_buy, float(order.size), self._round_px(order.limit_px),
            {"limit": {"tif": order.tif}}, reduce_only=order.reduce_only,
        )
        # HL 拒單雙形態（同 _parse_order_response docstring）：頂層 err，或頂層 ok 但
        # statuses[0] 帶 "error"（如 "Order was never placed, already canceled, or filled."）
        # ——後者絕不能判成功，要大聲記錄後回 False（工程原則 3：失敗不得靜默）。
        # resting（改價後仍掛簿）與 filled（改價後立即成交）皆為合法成功終態 → True。
        # 連線層例外（timeout、connection reset 等）不在此攔截，向上拋交由 resilience boundary 分類。
        if res.get("status") != "ok":
            return False
        status = res["response"]["data"]["statuses"][0]
        if isinstance(status, dict) and "error" in status:
            logger.warning("modify_order rejected: %s", status["error"])
            return False
        return True

    def market_open(self, agent_signer: Signer, coin: str, is_buy: bool, size: Decimal,
                    slippage: Decimal, builder: BuilderCode) -> OrderResult:
        res = self._exchange.market_open(
            coin, is_buy, float(size), None, float(slippage),
            builder={"b": builder.b, "f": builder.f},
        )
        return self._parse_order_response(res)

    def close_reduce_only(self, agent_signer: Signer, coin: str, is_buy: bool, size: Decimal,
                          slippage: Decimal, builder: BuilderCode) -> OrderResult:
        # 語意見 ABC docstring：is_buy 是平倉下單方向，呼叫端已算好 not position_is_long，
        # 這裡不做任何反轉。mid 來源固定 get_all_mids()（主 perp DEX）。
        mids = self.get_all_mids()
        if coin not in mids:
            return OrderResult(ok=False, filled_size=Decimal("0"), avg_px=Decimal("0"),
                               raw={"error": f"no mid for {coin}"})
        mid = mids[coin]
        px = mid * (1 + slippage) if is_buy else mid * (1 - slippage)
        res = self._exchange.order(
            coin, is_buy, float(size), self._round_px(px),
            {"limit": {"tif": "Ioc"}}, reduce_only=True,
            builder={"b": builder.b, "f": builder.f},
        )
        return self._parse_order_response(res)

    def update_leverage(self, agent_signer: Signer, coin: str, leverage: int,
                        is_cross: bool) -> bool:
        res = self._exchange.update_leverage(leverage, coin, is_cross)
        return res.get("status") == "ok"
