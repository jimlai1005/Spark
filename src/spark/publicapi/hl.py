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
