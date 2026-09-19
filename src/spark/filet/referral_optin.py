"""src/spark/filet/referral_optin.py
**客戶簽章的 Hyperliquid 推薦碼 opt-in 記錄**——格式、驗證原語、落檔。

本模組是 `risk_settings.py` 的姊妹檔：**同一套信任錨、同一組威脅模型、同一個驗證
原語**（`signing.recover_personal_sign_address`，全 repo 唯一的 recover 實作）。
先讀 `risk_settings.py` 的檔頭，這裡只寫**不同**的部分。

⭐ 為什麼是第五個域分隔字面量，不是重用既有的
------------------------------------------------
`risk_settings.py` 檔頭已經論證過換 leader／資金設定／風控設定／解除熔斷四個模板
第一行兩兩不等、且沒有任何呼叫端輸入能到達第一行，因此結構上不可能碰撞。本模組的
待簽原文第一行 `"Filet: set Hyperliquid referral code"` 是**第五個**這樣的字面量：
與既有四個都不同，同樣是固定字面量、同樣沒有呼叫端輸入能到達它。加入這一個模板
之後，五個模板仍然兩兩不可碰撞——新增一個域分隔符不會削弱既有四個之間的分隔，
因為分隔性是逐對成立的（任兩個字面量不相等），不是靠模板總數的某種全域性質。

⭐ 為什麼推薦碼要進待簽原文，不是只簽一個雜湊或帳號
------------------------------------------------------
與風控設定同一個理由（見 `risk_settings.py` 檔頭「客戶簽的是完整內容」）：客戶在
錢包裡看到的就是這段文字，「我簽的是什麼」是這道防線唯一的價值來源。推薦碼一旦
設定即**不可改**（HL 規則），所以這裡的風險方向與風控設定相反——不是「日後可能被
削弱保護」，而是「設錯了就永久生效」，原文把碼寫清楚同樣是必要的。

⭐⭐ 時效語意：與風控設定同構的「持續意圖」
--------------------------------------------
opt-in 是**持續意圖**（不是一次性動作，這裡沒有 unlock 那種相反語意的姊妹記錄）：
「我同意 Filet 幫我設這個推薦碼」在客戶簽完之後、引擎真正執行之前的任意時間都仍然
成立——引擎可能停機、可能排隊等下一輪冪等檢查，過期而拒絕套用等於讓客戶的意圖
無聲地消失。所以：
- API 端（`POST /api/me/referral`）強制 `max_age_s=REFERRAL_OPTIN_MAX_AGE_S`：
  驗的是「客戶剛剛按下的那一次」。
- 引擎端（`ReferralOptinApplier`）用 `max_age_s=None` 放行：沿用
  `verify_risk_settings` 對持續意圖的同一個理由。

檔案格式：`{exchange_dir}/referral_optin.json`，頂層 `{"optins": [...]}`。與
`risk_settings.json`／`risk_unlock.json` 各自一個檔是同一個決定：三份記錄的讀者是
三個獨立的套用器，共用一個檔會讓其中一方的格式問題連坐另外兩方。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from spark.filet.followers import normalize_hex_address, validate_account_id
from spark.filet.leader_change import (LEADER_CHANGE_MAX_AGE_S, LeaderChangeError,
                                       parse_issued_at)
from spark.filet.safe_fs import write_json_atomic
from spark.filet.signing import recover_personal_sign_address

# ⭐ 動作類型標識。落進記錄、且驗證端**顯式比對**（見檔頭域分隔）。
ACTION_REFERRAL_OPTIN = "referral_optin"

# 簽章時效上限（API 端強制）：沿用換 leader／風控設定的同一個常數（同一個 nonce 域、
# 同一個發放端點、同一種「客戶當下的意圖」語意）。引擎端刻意以 `max_age_s=None`
# 放行（見檔頭「時效語意」）。
REFERRAL_OPTIN_MAX_AGE_S = LEADER_CHANGE_MAX_AGE_S

# 記錄的鍵集（多一個少一個都要有人主動改這行）。強制它的是測試不是註解
# （tests/test_referral_optin.py::test_record_field_set_is_pinned_by_the_constant）。
# ⚠️ **沒有 signer 欄位**（同 risk_settings：期望簽章者一律取自可信來源）。
REFERRAL_OPTIN_FIELDS = ("action", "account_id", "code", "nonce", "issued_at",
                         "signature", "message")

# 推薦碼合法格式：HL 碼為大寫英數＋底線（見 plan §2）。API 設定值與記錄裡的 code
# 一律先 `.strip().upper()` 再比對此規則——正規化與合法區間的**唯一**定義點在這裡。
REFERRAL_CODE_RE = re.compile(r"^[A-Z0-9_]{1,32}$")

# nonce 字元集：與 leader_change／risk_settings 同一條規則。
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class ReferralOptinError(ValueError):
    """推薦碼 opt-in 記錄驗證失敗（**一律 semantic：不重試**）。

    `reason` 是機器可讀碼（不要比對訊息字串），值域比照 risk_settings：
    `malformed` / `action_mismatch` / `account_mismatch` / `expired` /
    `bad_signature` / `signer_mismatch` / `nonce_unusable`。
    重試同一筆記錄必定再次失敗：API 轉 4xx、引擎轉「拒絕套用並留痕」。
    """

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class VerifiedReferralOptin:
    """驗證通過的推薦碼 opt-in 意圖。`code` 是 normalize 後（大寫）的推薦碼。"""

    account_id: str
    user_address: str        # 驗章通過的簽章者＝可信來源給的 user_address
    code: str
    nonce: str
    issued_at: str
    issued_at_s: float


def _require_str(record: dict, key: str) -> str:
    v = record.get(key)
    if not isinstance(v, str) or not v:
        raise ReferralOptinError("malformed", f"{key} 缺漏或非字串: {v!r}")
    return v


def normalize_referral_code(code: object) -> str:
    """`.strip().upper()` 再驗證格式（唯一定義點）；不合法 → `malformed`。

    型別不是字串（例如 None、數字）同樣視為 `malformed`——記錄裡的 code 完全不可信，
    呼叫端只該處理一種失敗型別。
    """
    if not isinstance(code, str):
        raise ReferralOptinError("malformed", f"code 必須是字串: {code!r}")
    normalized = code.strip().upper()
    if not REFERRAL_CODE_RE.fullmatch(normalized):
        raise ReferralOptinError(
            "malformed", f"推薦碼格式不合法: {code!r}（僅允許大寫英數與底線，1-32 字）")
    return normalized


def build_referral_optin_message(*, account_id: str, code: object, nonce: str,
                                 issued_at: str) -> str:
    """待簽訊息的**唯一**版型（伺服器與引擎都用它重建，客戶端照此組字串簽名）。

    ⭐⭐ 第一行 `"Filet: set Hyperliquid referral code"` 是**域分隔符**：固定字面量，
    沒有任何呼叫端輸入能到達它；與既有四個模板（換 leader、資金設定、風控設定、
    解除熔斷）的第一行皆不同。完整論證見檔頭。

    `code` 在此 normalize 化（`normalize_referral_code`），與 risk_settings 把
    prefs canonical 化放進模板是同一個決定：正規化在模板這一層，兩邊才結構上不可能
    組出不同的字串。
    """
    normalized_code = normalize_referral_code(code)
    return (
        "Filet: set Hyperliquid referral code\n"
        "\n"
        "Signing this authorises Filet to set the referral code below on your\n"
        "Hyperliquid account, using the trading agent you already approved.\n"
        "Hyperliquid gives referred accounts a 4% fee discount on their first $25M\n"
        "of trading volume, and pays Filet a share of the trading fees you pay.\n"
        "A referral code can be set only once per account and cannot be changed\n"
        "later. If your account already has a referral code, nothing changes.\n"
        "This is optional: copy-trading works the same whether or not you sign.\n"
        "No positions are opened or closed by this action.\n"
        "\n"
        f"Account: {account_id}\n"
        f"Referral Code: {normalized_code}\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued_at}"
    )


def build_referral_optin_record(*, account_id: str, code: object, nonce: str,
                                issued_at: str, signature: str,
                                message: str) -> dict:
    """組出落檔用的記錄 dict（欄位順序固定＝REFERRAL_OPTIN_FIELDS，diff 友善）。

    `action` 由本函式**寫死**成 `ACTION_REFERRAL_OPTIN`，不從參數收（同 risk_settings：
    呼叫端能指定動作類型的話，一個手滑就能造出一筆自稱是別的動作的記錄）。`code`
    一律 normalize 後落檔——落進檔案的值與待簽訊息裡的值必須是同一個。
    """
    return {"action": ACTION_REFERRAL_OPTIN, "account_id": account_id,
            "code": normalize_referral_code(code), "nonce": nonce,
            "issued_at": issued_at, "signature": signature, "message": message}


def verify_referral_optin(record: dict, *, account_id: str, user_address: str,
                          now_s: float, consume_nonce: Callable[[str], bool],
                          max_age_s: float | None = None) -> VerifiedReferralOptin:
    """驗證一筆推薦碼 opt-in 記錄；通過回 VerifiedReferralOptin，否則拋
    ReferralOptinError。

    參數的信任分級**與 verify_risk_settings 完全相同**：`account_id`／
    `user_address` 來自可信來源（引擎取 manifest、API 取 session），`record` 完全
    不可信，`consume_nonce` 是唯一的副作用且擺在最後（在格式錯或簽章錯時就燒掉
    nonce，等於讓任何人送一筆垃圾記錄就能作廢客戶手上的合法授權）。

    ⭐⭐ `max_age_s` 預設 **None ＝不檢查時效**（持續意圖，見檔頭）。API 側應傳
    `REFERRAL_OPTIN_MAX_AGE_S` 收緊；引擎側一律放行時效。

    檢查順序：動作類型 → 帳號 → 格式（nonce／issued_at）→ 時效 → 推薦碼格式
    → 重建訊息 → recover → 比對簽章者 → 消耗 nonce。
    """
    validate_account_id(account_id)

    got = record.get("action")
    if got != ACTION_REFERRAL_OPTIN:
        raise ReferralOptinError(
            "action_mismatch",
            f"記錄的動作類型不是 {ACTION_REFERRAL_OPTIN}（收到 {got!r}）——拒絕。"
            f"一筆別的授權絕不能被當成一次推薦碼 opt-in（反向亦然）")

    expected_user = normalize_hex_address("user_address", user_address)

    claimed_account = _require_str(record, "account_id")
    if claimed_account != account_id:
        raise ReferralOptinError(
            "account_mismatch",
            f"記錄的 account_id（{claimed_account!r}）與待驗帳號（{account_id!r}）不符")

    nonce = _require_str(record, "nonce")
    if not _NONCE_RE.fullmatch(nonce):
        raise ReferralOptinError("malformed", f"nonce 格式不合法: {nonce!r}")
    issued_at = _require_str(record, "issued_at")

    # 時效：兩側都換算成 epoch 秒再比（同源同單位，工程原則 1）。
    try:
        issued_ts = parse_issued_at(issued_at).timestamp()
    except LeaderChangeError as e:
        raise ReferralOptinError(e.reason, str(e)) from e
    if max_age_s is not None:
        age_s = now_s - issued_ts
        if age_s > max_age_s:
            raise ReferralOptinError(
                "expired",
                f"簽章已過期（{age_s:.0f}s > {max_age_s}s）——"
                f"請重新取得待簽原文並重簽")
        if age_s < -max_age_s:
            raise ReferralOptinError(
                "expired",
                f"issued_at 位於未來（{-age_s:.0f}s）——拒絕；"
                f"合法流程的時間戳由伺服器發 nonce 時決定")

    code = normalize_referral_code(record.get("code"))
    signature = _require_str(record, "signature")

    expected_message = build_referral_optin_message(
        account_id=account_id, code=code, nonce=nonce, issued_at=issued_at)
    try:
        signer = recover_personal_sign_address(expected_message, signature)
    except Exception as e:  # noqa: BLE001 —— 壞簽名格式一律轉 semantic 拒絕
        raise ReferralOptinError("bad_signature",
                                 "簽章無法還原（格式錯誤或損毀）") from e
    if signer != expected_user:
        raise ReferralOptinError(
            "signer_mismatch",
            "簽章者不是該帳號的持有人——拒絕（能授權設推薦碼的人就能讓引擎"
            "用你的 agent 對主網送 setReferrer）")

    if not consume_nonce(nonce):
        raise ReferralOptinError(
            "nonce_unusable",
            "nonce 不存在、已用過或已過期——同一份簽章只能兌現一次")

    return VerifiedReferralOptin(account_id=account_id, user_address=expected_user,
                                 code=code, nonce=nonce, issued_at=issued_at,
                                 issued_at_s=issued_ts)


def referral_optin_path_for(exchange_dir: str | Path) -> str:
    """推薦碼 opt-in 記錄檔的路徑（**寫端與讀端的單一定義**，同 risk_settings_path_for）。

    與風控設定、資金設定、換 leader **各自一個檔**：三份記錄的讀者是三個獨立的套用
    器，共用一個檔會讓其中一方的格式問題連坐另外兩方。
    """
    return str(Path(exchange_dir) / "referral_optin.json")


def load_referral_optins(path: str | Path) -> list[dict]:
    """讀推薦碼 opt-in 記錄檔；不存在 → 空清單（尚無人簽過是正常狀態）。

    **刻意不在載入時驗證**（同 load_risk_settings）：驗證需要可信來源的
    user_address，載入層拿不到。分開之後就不會有人以為「載入成功＝已驗過」。
    """
    p = Path(path)
    if not p.exists():
        return []
    return json.loads(p.read_text()).get("optins", [])


def write_referral_optin(path: str | Path, record: dict) -> None:
    """落檔（原子換檔，走 safe_fs；沿 write_risk_settings 的慣例）。

    **同 account_id 覆蓋而非附加**：檔案代表「每位客戶當前是否已授權」，不是流水帳。
    附加會留下一堆舊記錄，而套用端只要挑錯一筆就可能重複兌現。
    """
    p = Path(path)
    entries = [e for e in load_referral_optins(p)
               if e.get("account_id") != record["account_id"]]
    entries.append(record)
    # 0644：讀端 filet-engine 是另一個 user（交換目錄無 setgid，見 user_leaders.py）。
    write_json_atomic(p, {"optins": entries}, mode=0o644)
