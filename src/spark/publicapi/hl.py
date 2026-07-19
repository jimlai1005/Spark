"""src/spark/publicapi/hl.py
Public API 對 HL 的唯一出口（單一 resilience boundary，工程原則 5）——**唯讀**。
分類在呼叫點強制宣告（沿 spark.resilience.run）：讀取（/info）＝冪等 → transient 重試。
本模組刻意沒有任何 /exchange 提交路徑：已簽授權由前端直送 HL（設計定案 1），
後端結構上無法經手簽名（紅線 5，Task 13 有結構性測試）。"""
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from spark.exchange.base import UserFill
from spark.resilience import run

_TIMEOUT_S = 10.0
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _to_ms_utc(dt: datetime) -> int:
    """datetime → epoch 毫秒。刻意鏡像 HyperliquidAdapter._to_ms_utc 的兩條慣例
    （不 import 它：那會把 hyperliquid SDK 拉進 API 進程，本模組只用 httpx）：
    naive 視為 UTC、aware 先轉 UTC；純整數運算（timedelta // timedelta），
    不走 `timestamp()*1000` 的 float 中間值（±1ms 捨入偏差）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - _EPOCH) // timedelta(milliseconds=1)


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
        的欄位語意才是同基準（工程原則 1）。
        ⚠️ 唯讀：只 POST /info，本 gateway 結構上無任何 /exchange 提交面（紅線 5）。"""
        raw = self._info({"type": "userFillsByTime", "user": address,
                          "startTime": _to_ms_utc(start), "endTime": _to_ms_utc(end)},
                         "HL userFillsByTime 查詢")
        return [UserFill(
            time=_EPOCH + timedelta(milliseconds=int(f["time"])),
            coin=f["coin"],
            px=Decimal(str(f["px"])),
            sz=Decimal(str(f["sz"])),
            side=f["side"],
            crossed=bool(f["crossed"]),
            oid=f["oid"],
            fee=Decimal(str(f.get("fee", "0") or "0")),
            builder_fee=Decimal(str(f.get("builderFee", "0") or "0")),
        ) for f in raw]

    def agent_addresses(self, user: str) -> list[str]:
        """使用者已授權的 agent 地址清單（extraAgents）；小寫正規化供同基準比對。"""
        agents = self._info({"type": "extraAgents", "user": user}, "HL extraAgents 查詢")
        return [a["address"].lower() for a in agents if a.get("address")]
