"""src/spark/filet/referral_apply.py
引擎側：**每輪冪等套用客戶簽章的推薦碼 opt-in**（`referral_optin.py` 的執行端）。

本模組是 `risk_settings_apply.py` 的姊妹檔，威脅模型與信任分級完全相同：
- 引擎**自己重新驗章**，不因為「API 已經驗過」而省略。
- `user_address` 一律取自 **manifest**（`_trusted_user_address`），絕不取自記錄。
- 時效放行（`max_age_s=None`）：opt-in 是持續意圖，同一筆記錄每輪都會被重新讀到。

⭐⭐ 與風控設定最大的不同：**這裡的動作是不可逆的鏈上寫入，不是本地狀態切換**
--------------------------------------------------------------------------------
`RiskSettingsApplier.effective()` 失敗時「沿用現狀」永遠是安全的（本地的
`CopySettings` 換一個值，下一輪還能再換）。本模組一旦成功送出 `setReferrer`，
Hyperliquid 端**不可撤銷**（同一帳號只能設一次）。所以：
- 冪等靠「先查鏈上再送」＋「送完再查一次確認」，不是靠本地狀態機猜測。
- `Referrer already set` 這個特定的 err 字串**不是失敗**——它是「鏈上已經是我們
  要的終態」的另一種說法（可能是上一輪送出但回應遺失，也可能是巧合），重查一次
  即可，不告警成失敗。
- 除此之外的任何 err（例如推薦碼不存在、格式錯）視為**永久性拒絕**：重試同一筆
  記錄必定再次失敗，記 `status="rejected"` 並不再嘗試，同時 critical 留痕讓人工
  介入（沿工程原則 2：semantic 失敗不重試）。

⭐ 本模組**絕不 raise、絕不中斷跟單**（`apply_once` 的每一步都在 return，公開方法
另包一層防禦性 try/except）：推薦碼是可選功能，它的任何失敗都不該影響跟單本身。

⭐ 沒有 nonce 帳本、也沒有單調 issued_at 護欄
----------------------------------------------
與風控設定不同，本模組不需要靠 issued_at 判斷「新舊」——它的冪等性直接來自鏈上
狀態本身（`query_referred_by` 一旦回非 None，狀態就已經是終態，不會再變回
未設定）。記憶體旗標 `self._done` 只是省掉每輪重複打 API 的優化，不是安全機制；
即使進程重啟、旗標歸零，下一輪重新查一次鏈上狀態仍會得到同樣的「已設定」結論
而安靜跳過——不會重複送出 `setReferrer`（HL 端本身也會用
`Referrer already set` 擋下重複的寫入嘗試）。

`self._rejected_issued_at` 記住「這一筆記錄已經判定壞掉」（驗章失敗或推薦碼與
本引擎的 `COPY_REFERRAL_CODE` 不符），避免同一筆記錄每輪重複告警；客戶重新簽一筆
新的（issued_at 不同）會自然重新嘗試。
"""
from __future__ import annotations

import logging
from pathlib import Path

from spark.copytrade.notifier import Notifier
from spark.exchange.base import ExchangeAdapter
from spark.filet.followers import load_followers
from spark.filet.referral_optin import (ReferralOptinError, load_referral_optins,
                                        verify_referral_optin)

logger = logging.getLogger(__name__)


class ReferralOptinApplier:
    """每 cycle 冪等地把客戶簽章的推薦碼 opt-in 套用到鏈上（`setReferrer`）。

    用法（見 `scripts/run_copytrade.make_referral_applier`）：

        if referral_applier is not None:
            referral_applier.apply_once()

    `apply_once()` 沒有回傳值：本引擎的下單邏輯不依賴它有沒有成功，觀察狀態走
    `.status`（心跳／`--status` 用；本 plan 不接心跳）。
    """

    def __init__(self, *, account_id: str, manifest_path: str | Path,
                 optin_path: str | Path, expected_code: str,
                 adapter: ExchangeAdapter, notifier: Notifier):
        self._account_id = account_id
        self._manifest_path = Path(manifest_path)
        self._optin_path = Path(optin_path)
        self._expected_code = expected_code
        self._adapter = adapter
        self._notifier = notifier
        self._done = False
        self._status = "pending"
        # 已判定壞掉（驗章失敗／推薦碼不符）的記錄的 issued_at，避免每輪重複告警。
        self._rejected_issued_at: str | None = None

    @property
    def status(self) -> str:
        """`"pending"` | `"applied"` | `"already_referred"` | `"rejected"`。"""
        return self._status

    # ---------- 告警（絕不讓告警失敗中斷跟單） ----------

    def _critical(self, text: str, *, dedup_key: str) -> None:
        try:
            self._notifier.critical("referral", text, dedup_key=dedup_key)
        except Exception:  # noqa: BLE001
            logger.exception("推薦碼告警發送失敗（跟單不受影響）")

    def _warn(self, text: str, *, dedup_key: str) -> None:
        try:
            self._notifier.warn("referral", text, dedup_key=dedup_key)
        except Exception:  # noqa: BLE001
            logger.exception("推薦碼告警發送失敗（跟單不受影響）")

    def _info(self, text: str) -> None:
        try:
            self._notifier.info("referral", text)
        except Exception:  # noqa: BLE001
            logger.exception("推薦碼告警發送失敗（跟單不受影響）")

    # ---------- 讀取（沿 RiskSettingsApplier 同一套慣例） ----------

    def _my_record(self) -> dict | None:
        """本帳號的 opt-in 記錄；無記錄或讀取失敗皆回 None（**只 log 不告警**）。

        讀取失敗多半是 transient（IO 打嗝、檔案正在被換）——絕大多數 cycle 根本
        沒有新記錄，這條路徑一旦告警就會洗版。
        """
        try:
            records = load_referral_optins(self._optin_path)
            mine = [r for r in records
                    if isinstance(r, dict) and r.get("account_id") == self._account_id]
        except (OSError, ValueError, TypeError, AttributeError) as e:
            logger.warning("推薦碼 opt-in 記錄讀取失敗（%s），本輪不動作: %r",
                           self._optin_path, e)
            return None
        return mine[-1] if mine else None

    def _trusted_user_address(self) -> str | None:
        """manifest 登錄的 user_address＝唯一可信的簽章者比對基準。"""
        try:
            refs = load_followers(self._manifest_path)
        except (OSError, ValueError) as e:
            logger.warning("推薦碼 opt-in：follower manifest 讀取失敗（%s）: %r",
                           self._manifest_path, e)
            refs = None
        if refs is not None:
            for r in refs:
                if r.account_id == self._account_id:
                    return r.user_address
        self._critical(
            f"**推薦碼 opt-in：取不到可信的 user_address**（manifest "
            f"{self._manifest_path} 讀取失敗或查無 account={self._account_id}）——"
            f"**不套用**，沒有可信的比對基準時驗章是假驗證",
            dedup_key="referral_no_trusted_user")
        return None

    # ---------- 鏈上查詢（step 7/8，也供 setReferrer 之後的兩處重查重用） ----------

    def _check_onchain(self, user_address: str) -> str:
        """查一次鏈上 `referredBy`。回傳 `"referred"` / `"not_referred"` /
        `"query_failed"`。

        `"referred"`：已記 `done=True`、`status="already_referred"` 並發 info
        （同碼或他碼皆是——推薦碼一旦設定即不可改，本引擎不需要、也不該再嘗試）。
        `"query_failed"`：transient，已發 warn dedup，呼叫端應直接 return（下一輪
        重試）。`"not_referred"`：呼叫端可以繼續往下走（送 `setReferrer`）。
        """
        try:
            current = self._adapter.query_referred_by(user_address)
        except Exception as e:  # noqa: BLE001 —— 網路例外一律 transient
            logger.warning("推薦碼鏈上查詢失敗 account=%s: %r", self._account_id, e)
            self._warn(
                f"推薦碼鏈上查詢失敗（account={self._account_id}），下一輪重試",
                dedup_key="referral_query_failed")
            return "query_failed"
        if current is not None:
            self._done = True
            self._status = "already_referred"
            which = "Filet" if current == self._expected_code else "其他推薦人"
            self._info(f"account={self._account_id} 鏈上已有推薦碼（{which}），"
                      f"不再嘗試")
            return "referred"
        return "not_referred"

    # ---------- 主流程 ----------

    def apply_once(self) -> None:
        """本輪冪等套用一次。**絕不 raise**（見檔頭）。"""
        try:
            self._apply_once()
        except Exception:  # noqa: BLE001 —— 推薦碼管線壞掉絕不能中斷跟單
            logger.exception("推薦碼 opt-in 套用失敗（跟單不受影響）")

    def _apply_once(self) -> None:
        if self._done:
            return
        rec = self._my_record()
        if rec is None:
            return
        if rec.get("issued_at") == self._rejected_issued_at:
            return
        user_address = self._trusted_user_address()
        if user_address is None:
            return

        try:
            verified = verify_referral_optin(
                rec, account_id=self._account_id, user_address=user_address,
                now_s=0.0, consume_nonce=lambda _n: True, max_age_s=None)
        except ReferralOptinError as e:
            logger.error("推薦碼 opt-in 記錄驗簽失敗 account=%s reason=%s",
                         self._account_id, e.reason)
            self._critical(
                f"**推薦碼 opt-in 記錄驗簽失敗**（reason=`{e.reason}`）——不套用。"
                f"這是 semantic 失敗，重試同一筆記錄必定再次失敗；若客戶確實要"
                f"授權，請他重新取得待簽原文並重簽",
                dedup_key=f"referral_verify_failed:{e.reason}")
            self._rejected_issued_at = rec.get("issued_at")
            return

        if verified.code != self._expected_code:
            logger.error("推薦碼 opt-in 記錄的推薦碼與引擎設定不符 account=%s",
                         self._account_id)
            self._critical(
                f"**推薦碼 opt-in 記錄的推薦碼與引擎設定的 COPY_REFERRAL_CODE 不符**"
                f"（account={self._account_id}）——兩處設定漂移，不套用。請確認客戶"
                f"簽署的推薦碼與本引擎部署設定一致",
                dedup_key="referral_code_mismatch")
            self._rejected_issued_at = verified.issued_at
            return

        outcome = self._check_onchain(user_address)
        if outcome != "not_referred":
            return  # "referred" ⇒ 已完成；"query_failed" ⇒ 下一輪重試

        try:
            res = self._adapter.set_referrer(self._expected_code)
        except Exception as e:  # noqa: BLE001 —— 網路例外一律 transient
            logger.warning("推薦碼設定呼叫失敗 account=%s: %r", self._account_id, e)
            self._warn(
                f"推薦碼設定呼叫失敗（account={self._account_id}），下一輪重試",
                dedup_key="referral_set_failed")
            return

        if not res.ok:
            response = res.raw.get("response") if isinstance(res.raw, dict) else None
            if isinstance(response, str) and "already set" in response.lower():
                # 送達但鏈上早已是終態（可能是上一輪送出、回應遺失）：重查一次即可，
                # 不是失敗（見檔頭）。
                self._check_onchain(user_address)
                return
            self._done = True
            self._status = "rejected"
            self._critical(
                f"**推薦碼設定失敗**（account={self._account_id}，"
                f"response={response!r}）——不再重試，請人工檢查",
                dedup_key="referral_set_rejected")
            return

        # 送出成功：重查一次確認鏈上真的是我們要的碼（不信任回應本身）。
        try:
            confirmed = self._adapter.query_referred_by(user_address)
        except Exception as e:  # noqa: BLE001
            logger.warning("推薦碼設定後重查失敗 account=%s: %r",
                           self._account_id, e)
            self._warn(
                f"推薦碼設定後重查失敗（account={self._account_id}），下一輪重試",
                dedup_key="referral_verify_query_failed")
            return
        if confirmed == self._expected_code:
            self._done = True
            self._status = "applied"
            self._info(f"account={self._account_id} 推薦碼已設定為 "
                      f"{self._expected_code}")
            return
        self._warn(
            f"推薦碼設定後重查不符（account={self._account_id}，收到 "
            f"{confirmed!r}），下一輪重試",
            dedup_key="referral_verify_mismatch")
