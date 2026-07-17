"""src/spark/publicapi/hl.py
Public API 對 HL 的唯一出口（單一 resilience boundary，工程原則 5）——**唯讀**。
分類在呼叫點強制宣告（沿 spark.resilience.run）：讀取（/info）＝冪等 → transient 重試。
本模組刻意沒有任何 /exchange 提交路徑：已簽授權由前端直送 HL（設計定案 1），
後端結構上無法經手簽名（紅線 5，Task 13 有結構性測試）。"""
import time
from decimal import Decimal

import httpx

from spark.resilience import run

_TIMEOUT_S = 10.0


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

    def max_builder_fee(self, user: str, builder: str) -> int:
        """使用者已核給 builder 的費率上限（十分之一 bp；0 = 未核）。verify/status 用
        != 0 判 builder fee approval 已上鏈；同時是 maxFeeRate 生效的鏈上真相。"""
        return int(self._info({"type": "maxBuilderFee", "user": user, "builder": builder},
                              "HL maxBuilderFee 查詢"))

    def agent_addresses(self, user: str) -> list[str]:
        """使用者已授權的 agent 地址清單（extraAgents）；小寫正規化供同基準比對。"""
        agents = self._info({"type": "extraAgents", "user": user}, "HL extraAgents 查詢")
        return [a["address"].lower() for a in agents if a.get("address")]
