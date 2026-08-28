"""src/spark/filet/close_all.py
**客戶簽章的「平倉並撤銷」一次性請求**（Task 15 kill switch 第二級）——格式、
驗證原語、落檔。

本模組是 `risk_settings.py` 的姊妹檔，與其中的 `risk_unlock`（一次性動作）同一個
信任錨、同一套驗證原語（`signing.recover_personal_sign_address`，全 repo 唯一的
recover 實作），先讀 `risk_settings.py` 檔頭再看這裡只寫**不同**的部分。

⭐ 與 `risk_unlock` 方向相反：解鎖是拿掉一道保護（讓引擎恢復交易），本模組是
owner **主動要求引擎進入受控收尾**（撤單 → reduce-only 全平 → halt，重用既有
`killswitch.trip`，reason=`owner_close`）——一旦套用**不可逆**（引擎端不提供
任何自動或簽章解鎖路徑，見 `killswitch.REASON_OWNER_CLOSE`）。前端的二次確認
在 UI 層（見 `web/src/components/dashboard`），本模組只管「這份請求真的是本人簽的」。

⭐ 域分隔：第一行 `"Filet: close all positions and revoke copy-trading"` 與既有
四個模板（換 leader／資金設定／風控設定／解除熔斷）的第一行兩兩不等，且沒有任何
呼叫端輸入能到達它——沿 `risk_settings.py` 檔頭的同一個結構性論證，五個模板產生
的字串第一行必定互不相同。

⭐ 時效**強制**（`CLOSE_ALL_MAX_AGE_S`，與 `RISK_SETTINGS_MAX_AGE_S` 同一個 600 秒
語意）：這是一次性的**當下**決定，一份三天前簽的「平倉並撤銷」不該在客戶忘記它
之後突然生效。

⭐ 沒有獨立的一次性消耗檔（與 `risk_unlock` 不同）：`risk_unlock` 需要防止「同一份
簽章被重放後反覆開鎖」（熔斷可以被觸發又解除、再觸發又解除），本模組的落地動作
（`killswitch.trip`）本身就是終態——`is_tripped(root)` 一旦為 True，
`close_all_apply.CloseAllApplier.consume` 就不會再呼叫 `wind_down`
（見該模組 docstring），冪等由 ARM 檔天然提供，不需要另一層護欄。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from spark.filet.followers import normalize_hex_address, validate_account_id
from spark.filet.leader_change import LeaderChangeError, parse_issued_at
from spark.filet.safe_fs import write_json_atomic
from spark.filet.signing import recover_personal_sign_address

ACTION_CLOSE_ALL = "close_all"

# 一次性動作，強制時效——與 risk_settings.py 的 RISK_SETTINGS_MAX_AGE_S 同一個語意
# 常數值（600 秒），但刻意不 import 它：兩個模組各自的時效上限若日後需要分開調整
# （例如平倉並撤銷這個更不可逆的動作想收得更緊），不該因為共用一個 import 而綁死。
CLOSE_ALL_MAX_AGE_S = 600

CLOSE_ALL_FIELDS = ("action", "account_id", "nonce", "issued_at",
                    "signature", "message")

_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class CloseAllError(ValueError):
    """平倉並撤銷請求驗證失敗（**一律 semantic：不重試**）。

    `reason` 是機器可讀碼：`malformed` / `action_mismatch` / `account_mismatch` /
    `expired` / `bad_signature` / `signer_mismatch` / `nonce_unusable`
    （值域沿 `risk_settings.RiskSettingsError` 的既有集合）。
    """

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class VerifiedCloseAll:
    account_id: str
    user_address: str
    nonce: str
    issued_at: str
    issued_at_s: float


def build_close_all_message(*, account_id: str, nonce: str, issued_at: str) -> str:
    """待簽訊息的**唯一**版型（伺服器與引擎都用它重建，客戶端照此組字串簽名）。

    白話寫明後果（同 `build_risk_unlock_message` 的理由）：這是客戶**自己終止**
    跟單關係的動作，且**不可逆**——引擎收尾後不會自動恢復，也明講本次簽章不代表
    鏈上撤銷 API wallet 權限（那一步仍需客戶自己到 Hyperliquid 官方介面操作，
    見前端「平倉並撤銷」完成後的指引卡）。
    """
    return (
        "Filet: close all positions and revoke copy-trading\n"
        "\n"
        "Signing this tells Filet to close all of your open copy-trading positions\n"
        "at market and stop following your strategy. This action is irreversible:\n"
        "once positions are closed, copy-trading for this account halts and does\n"
        "not resume automatically or by any other signed request.\n"
        "This does not revoke the API wallet's on-chain permissions by itself —\n"
        "after this completes, go to the official Hyperliquid interface to remove\n"
        "the API wallet yourself.\n"
        "\n"
        f"Account: {account_id}\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued_at}"
    )


def build_close_all_record(*, account_id: str, nonce: str, issued_at: str,
                           signature: str, message: str) -> dict:
    """`action` 由本函式**寫死**成 `ACTION_CLOSE_ALL`（沿 risk_settings 的既有理由：
    讓呼叫端指定動作類型，等於把域分隔的一半交還給不可信的請求內容）。"""
    return {"action": ACTION_CLOSE_ALL, "account_id": account_id, "nonce": nonce,
            "issued_at": issued_at, "signature": signature, "message": message}


def _require_str(record: dict, key: str) -> str:
    v = record.get(key)
    if not isinstance(v, str) or not v:
        raise CloseAllError("malformed", f"{key} 缺漏或非字串: {v!r}")
    return v


def verify_close_all(record: dict, *, account_id: str, user_address: str,
                     now_s: float, consume_nonce: Callable[[str], bool],
                     max_age_s: float = CLOSE_ALL_MAX_AGE_S) -> VerifiedCloseAll:
    """驗證一筆平倉並撤銷請求；通過回 VerifiedCloseAll，否則拋 CloseAllError。

    檢查順序（沿 `risk_settings._verify_common` 的既有順序）：動作類型 → 帳號 →
    格式 → 時效 → 重建訊息 → recover → 比對簽章者 → 消耗 nonce。
    `max_age_s` **不接受 None**（不像風控設定的「持續意圖」，這裡永遠是一次性
    當下動作，呼叫端不得放行時效）。
    """
    validate_account_id(account_id)

    got = record.get("action")
    if got != ACTION_CLOSE_ALL:
        raise CloseAllError(
            "action_mismatch",
            f"記錄的動作類型不是 {ACTION_CLOSE_ALL}（收到 {got!r}）——拒絕。一筆"
            f"其他動作的授權絕不能被當成一次「平倉並撤銷」（反向亦然）")

    expected_user = normalize_hex_address("user_address", user_address)

    claimed_account = _require_str(record, "account_id")
    if claimed_account != account_id:
        raise CloseAllError(
            "account_mismatch",
            f"記錄的 account_id（{claimed_account!r}）與待驗帳號（{account_id!r}）不符")

    nonce = _require_str(record, "nonce")
    if not _NONCE_RE.fullmatch(nonce):
        raise CloseAllError("malformed", f"nonce 格式不合法: {nonce!r}")
    issued_at = _require_str(record, "issued_at")

    try:
        issued_ts = parse_issued_at(issued_at).timestamp()
    except LeaderChangeError as e:
        raise CloseAllError(e.reason, str(e)) from e
    age_s = now_s - issued_ts
    if age_s > max_age_s:
        raise CloseAllError(
            "expired",
            f"簽章已過期（{age_s:.0f}s > {max_age_s}s）——請重新取得待簽原文並重簽")
    if age_s < -max_age_s:
        raise CloseAllError(
            "expired",
            f"issued_at 位於未來（{-age_s:.0f}s）——拒絕；合法流程的時間戳由伺服器"
            f"發 nonce 時決定")

    signature = _require_str(record, "signature")
    expected_message = build_close_all_message(
        account_id=account_id, nonce=nonce, issued_at=issued_at)
    try:
        signer = recover_personal_sign_address(expected_message, signature)
    except Exception as e:  # noqa: BLE001 —— 壞簽名格式一律轉 semantic 拒絕
        raise CloseAllError("bad_signature",
                            "簽章無法還原（格式錯誤或損毀）") from e
    if signer != expected_user:
        raise CloseAllError(
            "signer_mismatch",
            "簽章者不是該帳號的持有人——拒絕（平倉並撤銷會讓引擎平掉帳戶全部部位）")

    if not consume_nonce(nonce):
        raise CloseAllError(
            "nonce_unusable",
            "nonce 不存在、已用過或已過期——同一份簽章只能兌現一次")

    return VerifiedCloseAll(account_id=account_id, user_address=expected_user,
                            nonce=nonce, issued_at=issued_at, issued_at_s=issued_ts)


def close_all_path_for(exchange_dir: str | Path) -> str:
    """請求檔路徑（**寫端與讀端的單一定義**，同 `risk_settings_path_for` 的模式）。
    與換 leader／資金／風控設定**各自一個檔**：共用一個檔會讓其中一方的格式問題
    連坐另外幾方——而它們各自都能造成資金損失或不可逆的收尾。
    """
    return str(Path(exchange_dir) / "owner_close.json")


def load_close_all_requests(path: str | Path) -> list[dict]:
    """讀請求檔；不存在 → 空清單（尚無人請求過是正常狀態）。

    **刻意不在載入時驗證**（同 `load_risk_settings`）：驗證需要可信來源的
    user_address，載入層拿不到。
    """
    p = Path(path)
    if not p.exists():
        return []
    return json.loads(p.read_text()).get("requests", [])


def write_close_all_request(path: str | Path, record: dict) -> None:
    """落檔（同 account_id 覆蓋，非附加）：檔案代表「這位客戶目前有沒有一筆待
    處理的平倉並撤銷請求」，不是流水帳（同 `write_risk_settings` 的既有理由）。
    """
    p = Path(path)
    entries = [e for e in load_close_all_requests(p)
              if e.get("account_id") != record["account_id"]]
    entries.append(record)
    write_json_atomic(p, {"requests": entries}, mode=0o644)
