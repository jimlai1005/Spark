"""src/spark/publicapi/hl.py
Public API 對 HL 的唯一出口（單一 resilience boundary，工程原則 5）——**唯讀**。
分類在呼叫點強制宣告（沿 spark.resilience.run）：讀取（/info）＝冪等 → transient 重試。
本模組刻意沒有任何 /exchange 提交路徑：已簽授權由前端直送 HL（設計定案 1），
後端結構上無法經手簽名（紅線 5，Task 13 有結構性測試）。"""
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from spark.exchange.base import USER_FILLS_PAGE_LIMIT, UserFill
from spark.resilience import run

_TIMEOUT_S = 10.0
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
EXPLORER_URL = "https://rpc.hyperliquid.xyz/explorer"

# R-A（2026-08-30 opus 審查 C2/C3 修法）：`get_user_fills_paged` 的分頁頁數上限，
# 保護「查一次極活躍帳戶的全史」不會無界地打上游。環境變數可覆寫（沿
# `ExploreConfig.from_env` 的慣例：可選、缺就用模組預設，展示/計費用途不該因為
# 漏設一個門檻常數就拒絕啟動）。刻意在**呼叫時**才讀 env（不是模組載入時的頂層
# 常數），測試才能用 monkeypatch 覆寫而不必重新 import 本模組。
DEFAULT_FILLS_MAX_PAGES = 10


def _fills_max_pages_from_env() -> int:
    v = os.environ.get("FILET_FILLS_MAX_PAGES")
    return int(v) if v else DEFAULT_FILLS_MAX_PAGES


def _to_ms_utc(dt: datetime) -> int:
    """datetime → epoch 毫秒。刻意鏡像 HyperliquidAdapter._to_ms_utc 的兩條慣例
    （不 import 它：那會把 hyperliquid SDK 拉進 API 進程，本模組只用 httpx）：
    naive 視為 UTC、aware 先轉 UTC；純整數運算（timedelta // timedelta），
    不走 `timestamp()*1000` 的 float 中間值（±1ms 捨入偏差）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - _EPOCH) // timedelta(milliseconds=1)


def _parse_fill(f: dict) -> UserFill:
    """單筆 `userFillsByTime` 原始字典 → `UserFill`（`get_user_fills`／
    `get_user_fills_paged` 共用，避免同一段解析邏輯散落兩處各自漂移）。

    ⭐ R-A（2026-08-30 opus 審查 C5）：`closedPnl` 補 `or "0"` 空字串護欄——
    `f.get("closedPnl") is not None` 只擋掉 `None`，擋不住上游回空字串 `""`
    的情形（`Decimal("")` → `InvalidOperation`，會直接炸掉呼叫端，包括
    costbreaker 取數路徑）；`builder_fee`／`fee` 兩欄已有同款護欄，這裡補齊
    成同一基準（工程原則 1）。空字串／None 語意不同：`None`＝這筆沒有
    closedPnl 資料（讀者不可當 0）；空字串視為上游的「0」表達方式，不是
    「沒資料」，故轉成 `Decimal("0")` 而非放行成 `None`（否則單一筆空字串
    會讓整批 `has_realized` 誤判成「這批沒有 closedPnl 資料」）。"""
    raw_pnl = f.get("closedPnl")
    return UserFill(
        time=_EPOCH + timedelta(milliseconds=int(f["time"])),
        coin=f["coin"],
        px=Decimal(str(f["px"])),
        sz=Decimal(str(f["sz"])),
        side=f["side"],
        crossed=bool(f["crossed"]),
        oid=f["oid"],
        fee=Decimal(str(f.get("fee", "0") or "0")),
        builder_fee=Decimal(str(f.get("builderFee", "0") or "0")),
        closed_pnl=(Decimal(str(raw_pnl) or "0") if raw_pnl is not None else None),
    )


def _fill_detail_dict(f: dict) -> dict:
    """單筆 `userFillsByTime` 原始字典 → `get_fills_detail`／`get_fills_detail_paged`
    共用的展示形狀（I-18：抽出成獨立函式，兩個方法都吃同一份欄位映射，不重複
    寫兩份會各自漂移的裁切邏輯）。欄位名直接對齊真實 `userFillsByTime` 回應
    （見 `HLGateway.get_fills_detail` docstring 的實測依據）；金額保留字串。"""
    return {
        "time": int(f["time"]),
        "coin": f["coin"],
        "side": f["side"],
        "px": str(f["px"]),
        "sz": str(f["sz"]),
        "fee": str(f.get("fee", "0") or "0"),
        "closed_pnl": str(f.get("closedPnl", "0") or "0"),
        "hash": f.get("hash", ""),
    }


def _default_post(url: str, body: dict):
    """httpx 的 ConnectError/ReadTimeout 等**不繼承**內建 ConnectionError/TimeoutError，
    訊息還可能是空字串——resilience 邊界的錯誤分類器認不得，真實連線失敗會被
    誤分類成 semantic 直接上拋（opus 審 I1）。修法：在這個唯一的 IO 邊界把 httpx
    例外轉譯成分類器認得的內建型別（不動引擎共用的 resilience.py）。
    TimeoutException 是 TransportError 子類：先窄後寬。"""
    try:
        resp = httpx.post(url, json=body, timeout=_TIMEOUT_S)
    except httpx.TimeoutException as e:
        raise TimeoutError(str(e) or "hl info timed out") from e
    except httpx.TransportError as e:
        raise ConnectionError(f"hl info transport error: {e}") from e
    resp.raise_for_status()
    return resp.json()


class HLGateway:
    """post_fn / sleep_fn 可注入：測試給 fake post 與不真睡的 sleep（沿 resilience 慣例）。"""

    def __init__(self, base_url: str, post_fn=None, sleep_fn=time.sleep):
        self._base = base_url.rstrip("/")
        self._post = post_fn or _default_post
        self._sleep = sleep_fn

    def _info(self, body: dict, what: str):
        return run(lambda: self._post(f"{self._base}/info", body),
                   what=what, idempotent=True, sleep_fn=self._sleep)

    def clearinghouse_state(self, address: str) -> dict:
        """完整 clearinghouseState（唯讀、冪等 → transient 重試）。
        M3 watchlist 快照用；get_account_value 亦取道此處（單一查詢來源）。"""
        return self._info({"type": "clearinghouseState", "user": address},
                          "HL 帳戶查詢")

    def get_account_value(self, address: str) -> Decimal:
        return Decimal(self.clearinghouse_state(address)["marginSummary"]["accountValue"])

    def portfolio(self, address: str) -> list:
        """`portfolio` 的原始回應（唯讀、冪等 → transient 重試）。

        形狀 `[[period, {accountValueHistory, pnlHistory, vlm}], ...]`，8 個 period：
        `day/week/month/allTime` ＋ `perpDay/perpWeek/perpMonth/perpAllTime`。
        請求體與 SDK 的 `Info.portfolio` 逐欄位相同（`.venv/.../hyperliquid/info.py:683`
        的 `{"type": "portfolio", "user": user}`）——本進程不 import SDK（只用 httpx，
        見檔頭），所以請求體是**照抄查證過的原始碼**，不是憑印象寫的。

        ⚠️ 本方法回**原始**回應，不在這裡挑窗：期別的取捨（只能用 perp 窗）是績效
        語意問題，屬於 `filet.leader_perf` 的職責。gateway 只負責 IO 與重試——
        把 basis 決策塞進 IO 層，會讓「為什麼不能用預設窗」的理由散落到兩個檔案。
        """
        return self._info({"type": "portfolio", "user": address}, "HL portfolio 查詢")

    def candle_snapshot(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list:
        """K 線快照（唯讀、冪等 → transient 重試）。`coin` 可以是一般幣種
        （`"BTC"`）或 xyz builder-dex 的合成市場（`"xyz:SP500"`／`"xyz:GOLD"`，
        代號本身即含 dex 前綴，請求體不需另帶 `dex` 欄位——2026-08-31 curl 實測，
        見 `publicapi/benchmarks.py` 檔頭）。

        回應形狀 `[{t, T, s, i, o, c, h, l, v, n}, ...]`（升冪排列）：`t`/`T` 為
        該根 K 棒的開/收盤時間 epoch 毫秒，`c` 為收盤價字串——本方法回原始清單，
        欄位挑選與型別轉換交給呼叫端（`benchmarks.py`），gateway 只負責 IO 與
        重試（沿 `portfolio`/`non_funding_ledger_updates` 不挑窗欄位的既有慣例）。
        請求體照 SDK 原始碼核對（`hyperliquid/info.py` 的 `candles_snapshot`：
        `{"coin": ..., "interval": ..., "startTime": ..., "endTime": ...}`）。"""
        return self._info({"type": "candleSnapshot",
                           "req": {"coin": coin, "interval": interval,
                                   "startTime": start_ms, "endTime": end_ms}},
                          "HL candleSnapshot 查詢")

    def spot_usdc_balance(self, address: str) -> Decimal:
        """spot 錢包的 USDC 餘額（唯讀、冪等 → transient 重試）。

        ⭐ 為什麼需要它（入金體驗，2026-07-19）：我方**只鏡像 perp**。客戶從 CEX
        提幣或走第三方橋入金時，錢會落在 **spot** 錢包，perp 帳戶仍是 0——onboarding
        的 `funded` 因此判 False，而客戶看著交易所頁面上明明有錢，不知道少了哪一步。
        spot → perp 的劃轉是 **user-signed action**，我方結構上無法代做（那需要主鑰，
        違反非託管不變量）。所以能做的只有**偵測並提示**，不能代勞。

        請求體照抄查證過的 SDK 原始碼（`hyperliquid/info.py:130` 的 `spot_user_state`
        → `{"type": "spotClearinghouseState", "user": address}`）；本進程不 import SDK
        （只用 httpx，見檔頭），所以是抄原始碼而非憑印象。

        回應形狀 `{"balances": [{"coin": "USDC", "token": 0, "total": "...",
        "hold": "...", ...}, ...]}`。取 `total`（**不是** `total - hold`）：hold 是
        spot 掛單佔用，那些錢一樣需要客戶自己處理，一樣屬於「卡在 spot」。
        查無 USDC 項 → 0（真的沒有這個幣種，不是錯誤）。
        形狀不符 → 0：本查詢的唯一用途是「要不要顯示一句提示」，猜不出來就不提示，
        而不是讓客戶的 onboarding 狀態頁 500。
        """
        raw = self._info({"type": "spotClearinghouseState", "user": address},
                         "HL spotClearinghouseState 查詢")
        if not isinstance(raw, dict):
            return Decimal("0")
        for b in raw.get("balances") or []:
            if isinstance(b, dict) and b.get("coin") == "USDC":
                try:
                    return Decimal(str(b.get("total", "0")))
                except (ValueError, ArithmeticError):
                    return Decimal("0")
        return Decimal("0")

    def vault_details(self, vault_address: str) -> dict:
        """vault 的公開細節（name/leaderFraction/maxDistributable/followers/isClosed…）
        （唯讀、冪等 → transient 重試）。vault preflight（scripts/vault_preflight.py）用：
        is-vault 驗身＋ maxDistributable 與 clearinghouseState.withdrawable 的恆等式。
        ⚠️ 請求鍵是 `vaultAddress`，不是其他 /info 查詢慣用的 `user`。"""
        return self._info({"type": "vaultDetails", "vaultAddress": vault_address},
                          "HL vaultDetails 查詢")

    def non_funding_ledger_updates(self, user: str, start_ms: int) -> list:
        """非資金費的帳本流水（deposit/withdraw/vaultDeposit/vaultWithdraw…）
        （唯讀、冪等 → transient 重試）。回**原始**清單，不在這裡挑型別或算淨流量：
        流量語意（哪個欄位是真流出、白名單外型別怎麼辦）屬呼叫端（preflight 檢查層），
        gateway 只負責 IO 與重試——與 `portfolio` 不挑窗同一條理由。"""
        return self._info({"type": "userNonFundingLedgerUpdates",
                           "user": user, "startTime": start_ms},
                          "HL userNonFundingLedgerUpdates 查詢")

    def max_builder_fee(self, user: str, builder: str) -> int:
        """使用者已核給 builder 的費率上限（十分之一 bp；0 = 未核）。verify/status 用
        != 0 判 builder fee approval 已上鏈；同時是 maxFeeRate 生效的鏈上真相。"""
        return int(self._info({"type": "maxBuilderFee", "user": user, "builder": builder},
                              "HL maxBuilderFee 查詢"))

    def get_user_fills(self, address: str, start: datetime, end: datetime) -> list[UserFill]:
        """時間窗成交明細（唯讀、冪等 → transient 重試）。營運後台每客戶損益用：
        `collect_follower_summary` 只吃 `.sz/.px/.crossed/.builder_fee`，故這裡回
        與 HyperliquidAdapter.get_user_fills 同型的 UserFill（同一份解析慣例：
        Decimal(str(...)) 進位、builderFee 缺欄或 null 視為 0），跨兩個 adapter
        的欄位語意才是同基準（工程原則 1）。單頁：若視窗內成交筆數達
        `USER_FILLS_PAGE_LIMIT`（2000）則靜默低估——需要涵蓋 >2000 筆的呼叫端
        改用 `get_user_fills_paged`（R-A C3 修法）。
        ⚠️ 唯讀：只 POST /info，本 gateway 結構上無任何 /exchange 提交面（紅線 5）。"""
        raw = self._info({"type": "userFillsByTime", "user": address,
                          "startTime": _to_ms_utc(start), "endTime": _to_ms_utc(end)},
                         "HL userFillsByTime 查詢")
        return [_parse_fill(f) for f in raw]

    def get_user_fills_paged(self, address: str, start: datetime, end: datetime, *,
                             max_pages: int | None = None
                             ) -> tuple[list[UserFill], bool]:
        """分頁抓時間窗成交（唯讀展示／計費用途，R-A 2026-08-30 opus 審查 C2/C3
        修法）——**不是**即時風控路徑：`HyperliquidAdapter.get_user_fills`
        （引擎 cost breaker 用）刻意不分頁，見該檔 docstring 的風險量級論證
        （分頁假設錯的方向是換手率分子灌大 ⇒ 誤觸熔斷 ⇒ 強制平倉）。本方法用於
        `/api/me/fees`／dashboard 費用明細等**顯示**數字，誤判方向只讓一個展示
        數字偏低（`truncated=True` 時已明確標示），不進任何下單/熔斷判斷。

        ⚠️ 假設（僅單頁 fixture 驗證過，未對真實 API 逐頁分頁驗證）：
        `userFillsByTime` 依時間**升冪**排列。頁界去重用原始回應的 `tid`
        （成交唯一識別碼；`UserFill` 型別本身不帶這個欄位，僅用於本方法內部
        分頁去重，不外流）——同一毫秒可能有多筆不同 `tid` 的成交，光用時間
        當去重鍵會錯殺；`tid` 缺席（理論上不會，防禦用）才退回
        `(oid, time, coin, px, sz, side)` 這個較弱的複合鍵。

        滿頁（`USER_FILLS_PAGE_LIMIT`＝2000）時以最後一筆的時間為下一頁
        `startTime` 續抓（`userFillsByTime` 的 `start`/`end` 兩端皆含，重疊的
        那一毫秒靠上述去重鍵過濾，不會被算兩次）；連續 `max_pages`
        （未指定則讀 `FILET_FILLS_MAX_PAGES` env，預設
        `DEFAULT_FILLS_MAX_PAGES`＝10）頁都滿頁 → 回傳 `truncated=True`，
        已抓到的部分照樣回傳（呼叫端合計基於這份資料是**下限值**，需標示
        「未涵蓋全期間」，不得當成完整合計）。"""
        raw_fills, truncated = self._paged_fills_raw(address, start, end, max_pages=max_pages)
        all_fills = sorted((_parse_fill(f) for f in raw_fills), key=lambda f: f.time)
        return all_fills, truncated

    def _paged_fills_raw(self, address: str, start: datetime, end: datetime, *,
                         max_pages: int | None = None) -> tuple[list[dict], bool]:
        """共用的時間游標分頁核心（I-18：`get_user_fills_paged`／
        `get_fills_detail_paged` 皆基於本方法，只是最後一步的欄位裁切／型別
        轉換不同——滿頁偵測、`tid` 去重、時間不前進防呆這幾條邏輯只寫一份，
        不讓兩個呼叫端各自維護一份可能漂移的複本，見類別所在模組檔頭
        `get_user_fills_paged` 的既有 docstring；本方法的行為與其完全相同，
        只是回傳**原始** fill dict（未轉 `UserFill`），供呼叫端自行裁切。"""
        max_pages = (max_pages if max_pages is not None
                    else _fills_max_pages_from_env())
        all_raw: list[dict] = []
        seen: set = set()
        cur_start_ms = _to_ms_utc(start)
        end_ms = _to_ms_utc(end)
        truncated = False
        for page_idx in range(max_pages):
            raw = self._info({"type": "userFillsByTime", "user": address,
                              "startTime": cur_start_ms, "endTime": end_ms},
                             "HL userFillsByTime 查詢（分頁）")
            for f in raw:
                key = f.get("tid")
                if key is None:
                    key = (f.get("oid"), f.get("time"), f.get("coin"),
                          str(f.get("px")), str(f.get("sz")), f.get("side"))
                if key in seen:
                    continue
                seen.add(key)
                all_raw.append(f)
            if len(raw) < USER_FILLS_PAGE_LIMIT:
                break
            last_ms = int(raw[-1]["time"])
            if last_ms <= cur_start_ms:
                # 整頁都卡在同一毫秒或時間沒有前進——不無限迴圈，視為已截斷。
                truncated = True
                break
            cur_start_ms = last_ms
            if page_idx == max_pages - 1:
                truncated = True
        all_raw.sort(key=lambda f: int(f["time"]))
        return all_raw, truncated

    def get_fills_detail_paged(self, address: str, start: datetime, end: datetime, *,
                               max_pages: int | None = None) -> tuple[list[dict], bool]:
        """`get_fills_detail` 的分頁版（I-18：`/api/me/fills` 改固定 30 天窗＋
        游標分頁抓滿，取代舊版單頁 `get_fills_detail` 呼叫——實測使用者錢包
        90 天窗 2820 筆成交被單頁 2000 上限截掉最新 8 天，見 issue log I-18）。
        游標迴圈與 `get_user_fills_paged` 共用同一份實作（`_paged_fills_raw`，
        不重造），只是最後一步用 `_fill_detail_dict` 裁切成展示形狀（含
        `hash`，`UserFill` 沒有這個欄位，故不能直接復用 `get_user_fills_paged`
        的輸出）。回傳升冪排列的 dict 清單 ＋ `truncated`（見
        `_paged_fills_raw` docstring：連續 `max_pages` 頁滿頁才會是 True，
        已抓到的部分是下限值）。"""
        raw_fills, truncated = self._paged_fills_raw(address, start, end, max_pages=max_pages)
        return [_fill_detail_dict(f) for f in raw_fills], truncated

    def agent_addresses(self, user: str) -> list[str]:
        """使用者已授權的 agent 地址清單（extraAgents）；小寫正規化供同基準比對。"""
        agents = self._info({"type": "extraAgents", "user": user}, "HL extraAgents 查詢")
        return [a["address"].lower() for a in agents if a.get("address")]

    def get_fills_detail(self, address: str, start: datetime, end: datetime) -> list[dict]:
        """時間窗成交明細，供客戶自助查帳頁用（`/api/me/fills`，M3 round2 Task 7）。

        ⚠️ 刻意**不**復用 `get_user_fills`／共用 `UserFill`：那個型別是給
        `collect_follower_summary` 等損益管線吃的（只需要 `.sz/.px/.crossed/
        .builder_fee`），塞進 `hash`/`closedPnl` 這種純展示欄位會讓一個核心財務
        型別多出跟風控無關的欄位面。這裡回傳的是**裁切後的原始字典**，欄位名
        直接對齊真實 `userFillsByTime` 回應（實測樣本：`coin/px/sz/side/time/
        closedPnl/fee/hash` 均存在，2026-08-29 curl 驗證，見 plan 檔頭與
        `tests/fixtures/hl_user_fills_sample.json`）：金額保留字串（前端格式化，
        不在這裡轉 float）。"""
        raw = self._info({"type": "userFillsByTime", "user": address,
                          "startTime": _to_ms_utc(start), "endTime": _to_ms_utc(end)},
                         "HL userFillsByTime 查詢")
        return [_fill_detail_dict(f) for f in raw]

    def user_details(self, address: str) -> dict:
        """explorer 的帳戶交易明細（唯讀、冪等 → transient 重試）；`/api/me/
        authorizations`（M3 round2 Task 7）用來過濾出 approveAgent／
        approveBuilderFee 兩類授權動作。

        ⚠️ domain 與 `/info` 不同（`rpc.hyperliquid.xyz` vs `api.hyperliquid.xyz`），
        不走 `_info` 的 `{base}/info` 組裝，直接打 `EXPLORER_URL`（絕對 URL，
        與 `self._base` 無關）。回應形狀 `{"txs": [{"time": ms, "user": "0x…",
        "action": {"type": "approveAgent"/"approveBuilderFee"/…, …}, "block": n,
        "hash": "0x…", "error": null}]}`（2026-08-29 curl 實測 `approveAgent`／
        `approveBuilderFee` 兩種 action 的確切欄位，見
        `tests/fixtures/hl_explorer_user_details_sample.json`）。查無資料的地址
        → `{"txs": []}`（真實行為，非錯誤）。"""
        return run(lambda: self._post(EXPLORER_URL,
                                      {"type": "userDetails", "user": address}),
                   what="HL explorer 帳戶明細查詢", idempotent=True, sleep_fn=self._sleep)
