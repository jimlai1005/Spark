"""src/spark/filet/close_all_apply.py
引擎側：**每 cycle 消化客戶簽章的「平倉並撤銷」請求**（Task 15 kill switch 第二級）。

與 `risk_settings_apply.py` 的 `consume_unlock_request` 同一個信任錨、同一套驗證
流程（先讀該檔頭），但方向相反且**不可逆**：解鎖是拿掉一道保護，這裡是主動要求
引擎進入受控收尾（`killswitch.trip`，reason=`owner_close`）——**不新造平倉邏輯**，
`wind_down` 由呼叫端（`scripts/run_copytrade.make_revocation_wind_down`，已推廣
支援自訂 reason）注入，本模組只負責「這份請求真的是本人簽的、且尚未處理過」。

⭐⭐ 冪等靠 `killswitch.is_tripped`，不是另一層一次性消耗檔
------------------------------------------------------------
`risk_unlock` 需要獨立的一次性消耗檔（`risk_unlock_consumed.json`），因為熔斷
可以被觸發、解除、再觸發、再解除——同一份簽章不能被重放去反覆開鎖。
本模組觸發的動作（`trip()`）是**終態**：ARM 檔一旦寫下，`REASON_OWNER_CLOSE`
結構性地不在任何 rearm 清單裡（`killswitch._MANUAL_REARM_REASONS` 與
`_AUTO_REARM_REASONS` 皆不含它），沒有「解鎖後又要能再次解鎖」的情境——
`consume()` 進場先檢查 `is_tripped(root)`，已經 tripped 就直接不處理，
天然冪等，不需要再造一份狀態檔。

⭐ 引擎端**獨立重新驗章**（不信任 API 已驗過）：與換 leader／資金／風控設定同一個
決定——filet-api 是一個可能被打穿、可能有 bug 的進程，引擎自己重建訊息、重新
recover，才是真正擋得住偽造請求的那一層。

⭐ 失敗的安全方向：任何一關不過（記錄讀不到、驗簽失敗、manifest 查無此帳號）一律
**不觸發**收尾，維持現狀繼續正常跟單——這與 leader 撤銷的受控收尾不同（那條路徑
一旦判定撤銷就必須立刻收尾），因為這裡的觸發條件本身就需要驗證是否成立；驗證
不過不代表「應該收尾但收尾失敗」，代表「這份請求不可信，忽略它」。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from spark.copytrade.killswitch import is_tripped
from spark.copytrade.notifier import Notifier
from spark.filet.close_all import (CloseAllError, close_all_path_for,
                                   load_close_all_requests, verify_close_all)
from spark.filet.followers import load_followers
from spark.filet.leader_change_apply import require_exchange_dir

logger = logging.getLogger(__name__)


def resolve_close_all_path(env=None) -> str:
    """引擎要讀的請求檔路徑（**啟動時呼叫一次**，未設 env ⇒ 拒絕啟動）。

    錨點與換 leader／資金／風控設定共用 `require_exchange_dir`（同一個
    `FILET_EXCHANGE_DIR`、同一段「漏設就拒絕啟動」的理由）。
    """
    return close_all_path_for(require_exchange_dir(env))


class CloseAllApplier:
    """每 cycle 消化一筆「平倉並撤銷」請求；命中即觸發受控收尾。"""

    def __init__(self, *, account_id: str, manifest_path: str | Path,
                request_path: str | Path, notifier: Notifier, now_fn=time.time):
        self._account_id = account_id
        self._manifest_path = Path(manifest_path)
        self._request_path = Path(request_path)
        self._notifier = notifier
        self._now_fn = now_fn

    def _critical(self, text: str, *, dedup_key: str) -> None:
        """發 critical，且**告警失敗不得中斷跟單**（沿 RiskSettingsApplier._critical
        的既有慣例）。"""
        try:
            self._notifier.critical("close_all", text, dedup_key=dedup_key)
        except Exception:  # noqa: BLE001 —— 見上：觀測層壞掉不得弄停被觀測的系統
            logger.exception("平倉並撤銷告警發送失敗（跟單不受影響）")

    def _my_record(self) -> dict | None:
        """本帳號的請求；無記錄或讀取失敗皆回 None（只 log 不告警——絕大多數 cycle
        根本沒有新請求，這條路徑會告警就會變成每輪洗版）。"""
        try:
            records = load_close_all_requests(self._request_path)
            mine = [r for r in records
                    if isinstance(r, dict) and r.get("account_id") == self._account_id]
        except (OSError, ValueError, TypeError, AttributeError) as e:
            logger.warning("平倉並撤銷請求讀取失敗（%s），本輪不處理: %r",
                          self._request_path, e)
            return None
        return mine[-1] if mine else None

    def _trusted_user_address(self) -> str | None:
        """manifest 登錄的 user_address＝**唯一**可信的簽章者比對基準（沿
        RiskSettingsApplier._trusted_user_address 的既有決定）。"""
        try:
            refs = load_followers(self._manifest_path)
        except (OSError, ValueError) as e:
            logger.warning("平倉並撤銷：follower manifest 讀取失敗（%s）: %r",
                          self._manifest_path, e)
            refs = None
        if refs is not None:
            for r in refs:
                if r.account_id == self._account_id:
                    return r.user_address
        self._critical(
            f"**平倉並撤銷：取不到可信的 user_address**（manifest "
            f"{self._manifest_path} 讀取失敗或查無 account={self._account_id}）"
            f"——**不處理**任何請求。沒有可信的比對基準時驗章是假驗證",
            dedup_key="close_all_no_trusted_user")
        return None

    def consume(self, root: Path, wind_down: Callable[[], None]) -> bool:
        """回傳本輪是否真的觸發了收尾。**絕不 raise**（觀測/處理層壞掉不得中斷
        跟單——沿 `RiskSettingsApplier.consume_unlock_request` 的既有慣例）。
        """
        try:
            if is_tripped(root):
                return False  # 已在收尾/熔斷狀態，冪等：不重複觸發
            rec = self._my_record()
            if rec is None:
                return False
            user_address = self._trusted_user_address()
            if user_address is None:
                return False
            try:
                verify_close_all(rec, account_id=self._account_id,
                                 user_address=user_address,
                                 now_s=float(self._now_fn()),
                                 consume_nonce=lambda _n: True)
            except CloseAllError as e:
                if e.reason == "expired":
                    # 過期是**預期會發生的常態**（記錄沒人清、客戶很久以前按過一次），
                    # 不是事故 → 只 log，避免每輪 critical 洗版把真事件淹掉。
                    logger.info("平倉並撤銷請求已過期，本輪不處理 account=%s",
                               self._account_id)
                    return False
                logger.error("平倉並撤銷請求驗簽失敗 account=%s reason=%s",
                            self._account_id, e.reason)
                self._critical(
                    f"**平倉並撤銷請求驗簽失敗**（reason=`{e.reason}`）——本輪不處理。"
                    f"這是 semantic 失敗，重試同一筆請求必定再次失敗；若客戶確實要"
                    f"平倉並撤銷，請他重新取得待簽原文並重簽",
                    dedup_key=f"close_all_verify_failed:{e.reason}")
                return False
            self._critical(
                f"**收到客戶簽章的「平倉並撤銷」請求**（account={self._account_id}）"
                f"——觸發既有受控收尾路徑（撤單 → reduce-only 全平 → halt，"
                f"reason=owner_close）。恢復僅能由人工 re-arm（見 RUNBOOK）",
                dedup_key="close_all_triggered")
            wind_down()
            return True
        except Exception:  # noqa: BLE001 —— 處理層壞掉絕不能中斷跟單
            logger.exception("平倉並撤銷請求處理失敗（跟單不受影響）")
            return False
