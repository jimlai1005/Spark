"""src/spark/filet/risk_settings.py
**客戶簽章的風控設定記錄**（風控門檻＋熔斷後的自助解鎖）——格式、驗證原語、落檔。

本模組是 capital_settings.py 的姊妹檔：**同一套信任錨、同一組威脅模型、同一個驗證
原語**（signing.recover_personal_sign_address，全 repo 唯一的 recover 實作）。
先讀 capital_settings.py 的檔頭，這裡只寫**不同**的部分。

⭐ 為什麼風控設定也必須是客戶簽章的（2026-07-30）
------------------------------------------------
在此之前，風控偏好（`risk_prefs.py`）只在**啟用那一刻**被 auto-activate watcher 讀出
並烙進 `/etc/filet/followers/<id>.env`——已啟用的客戶再也改不了，只能人工改 env。
risk_prefs.py 檔頭把這條路徑的信任缺口寫得很清楚：偏好由 filet-api 寫進 pending，
沒有客戶簽章；能寫 pending 的人就能削弱保護。當時的緩解是「暴露窗只有啟用前的一分鐘，
啟用後 filet-api 再也影響不了引擎」。

要讓**已啟用**的客戶隨時改風控，那個緩解就消失了：引擎必須每輪回頭讀一份 filet-api
寫得到的檔。所以這條新管線一開始就走簽章——與換 leader、資金設定同一個形狀，
把「誰有權改這顆引擎的風控姿態」從「誰能寫這個檔」換成「誰握有這顆錢包的私鑰」。

⚠️ 這裡調整的是**保護門檻**，不是部位大小（那仍由 `capital_settings` 決定）。
方向上被打穿的 filet-api 只能削弱保護、不能直接放大曝險——但削弱保護同樣是真錢，
所以防線一樣，不因為「危害小一點」而降級。

⭐⭐ 兩個動作、兩份記錄、兩個檔（本模組最重要的結構決定）
--------------------------------------------------------
1. `risk_settings`——**持續意圖**。「我的風控門檻是這幾個數字」。引擎每輪讀它，
   讀到的永遠是客戶最後一次表達的意圖。
2. `risk_unlock`——**一次性動作**。「現在立刻解除這次熔斷鎖定」。

兩者的時效語意因此**相反**，這是刻意的（見 `verify_risk_settings` 的 `max_age_s`）：
- 持續意圖**不檢查時效**：一份三天前簽的「回撤上限 20%」今天仍然是客戶的意圖。
  引擎若因為過期而不套用，客戶的設定會**無聲地退回 env 值**（既有先例：
  `scripts/filet_auto_activate._latest_signed_leader` 對 leader 選擇同樣放行時效）。
- 一次性動作**強制檢查時效**（600 秒，沿 `CAPITAL_SETTINGS_MAX_AGE_S` 的同一個常數
  語意）：「立刻恢復跟單」是一個**當下**的決定。一份三天前簽的解鎖記錄若還能生效，
  等於客戶簽一次就永久放棄了熔斷保護——今天熔斷、舊記錄自動把它解開。

⭐⭐ 域分隔：三種待簽訊息**結構上不可能碰撞**
---------------------------------------------
capital_settings.py 檔頭的完整論證在此原樣適用，只是多了兩個第一行字面量：
  - 換 leader：      `"Filet: change copy-trading leader"`
  - 資金設定：      `"Filet: update copy-trading capital allocation"`
  - **風控設定**：  `"Filet: update copy-trading risk settings"`
  - **解除熔斷**：  `"Filet: resume copy-trading after a risk halt"`
四個字面量兩兩不等，且沒有任何呼叫端輸入能到達第一行 ⇒ 四個模板產生的字串第一行
必定不同 ⇒ 不存在一組輸入能讓任兩個模板產生同一字串。第二層（顯式 `action` 檢查）
提供的是**指名事件**的拒絕理由（`action_mismatch`），不是那道結構性防線本身。

風控設定與解除熔斷之間的分隔尤其重要：客戶調一次門檻的簽章若能被兌換成一次解鎖，
攻擊者只要等客戶下次調整風控，就能拿那份簽章把熔斷鎖打開。

⭐ 客戶簽的是**完整內容**，不是一個雜湊、也不是部分欄位
--------------------------------------------------------
`build_risk_settings_message` 逐行列出 `risk_prefs.RISK_PARAM_SPECS` 的**每一個**參數
與它的 canonical 值。只簽雜湊（或只簽被改動的那幾項）的話，客戶在錢包裡看到的是
一串他無從驗證的字元，而「我簽的到底是什麼」正是簽章這道防線唯一的價值來源。

⭐ 數值一律走 `risk_prefs.canonical_prefs`（單一定義點）
-------------------------------------------------------
本模組**不**自訂任何區間、預設或正規化規則。合法區間住在 `risk_prefs.RISK_PARAM_SPECS`
——API、watcher、前端、本模組四個消費端共用它。本檔若另訂一份，就會出現「API 收下、
引擎拒絕」或反過來（而後者是 fail-open）。
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
from spark.filet.risk_prefs import RISK_PARAM_SPECS, RiskPrefsError, canonical_prefs
from spark.filet.safe_fs import write_json_atomic
from spark.filet.signing import recover_personal_sign_address

# ⭐ 動作類型標識。落進記錄、且驗證端**顯式比對**（見檔頭域分隔第 2 層）。
ACTION_RISK_SETTINGS = "risk_settings"
ACTION_RISK_UNLOCK = "risk_unlock"

# 簽章時效上限：沿用換 leader／資金設定的同一個常數（同一個 nonce 域、同一個發放
# 端點、同一種「客戶當下的意圖」語意）。⚠️ 這個上限只對 **unlock**（一次性動作）
# 強制；風控設定是持續意圖，引擎端刻意以 `max_age_s=None` 放行（見檔頭）。
RISK_SETTINGS_MAX_AGE_S = LEADER_CHANGE_MAX_AGE_S

# 記錄的鍵集（多一個少一個都要有人主動改這行）。強制它的是測試不是註解
# （tests/test_risk_settings.py::test_record_field_set_is_pinned_by_the_constant）。
# ⚠️ **沒有 signer 欄位**（同 capital_settings：期望簽章者一律取自可信來源）。
RISK_SETTINGS_FIELDS = ("action", "account_id", "prefs", "nonce", "issued_at",
                        "signature", "message")
RISK_UNLOCK_FIELDS = ("action", "account_id", "nonce", "issued_at",
                      "signature", "message")

# nonce 字元集：與 leader_change／capital_settings 同一條規則。
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# 待簽訊息裡代表風控總開關的那一行的標籤。單獨一個常數是因為它**不在**
# RISK_PARAM_SPECS 裡（`enabled` 是總開關，不是一個可調參數），而寫端與讀端
# 必須組出同一個字串。
ENABLED_MESSAGE_LABEL = "Risk Controls"


class RiskSettingsError(ValueError):
    """風控設定／解鎖記錄驗證失敗（**一律 semantic：不重試**）。

    `reason` 是機器可讀碼（不要比對訊息字串），值域比照 capital：
    `malformed` / `action_mismatch` / `account_mismatch` / `expired` /
    `bad_signature` / `signer_mismatch` / `nonce_unusable`。
    重試同一筆記錄必定再次失敗：API 轉 4xx、引擎轉「拒絕套用並留痕」。
    """

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class VerifiedRiskSettings:
    """驗證通過的風控設定意圖。`prefs` 是 **canonical 化後**的偏好 dict。

    `issued_at_s`（epoch 秒）與 `issued_at`（原字串）並存是刻意的：單調性比較必須
    用同一個解析器算出來的秒數（工程原則 1），呼叫端自己再解析一次就有機會解出
    第二個答案，而那個答案正好是擋回滾攻擊的那一半。
    """

    account_id: str
    user_address: str        # 驗章通過的簽章者＝可信來源給的 user_address
    prefs: dict
    nonce: str
    issued_at: str
    issued_at_s: float


@dataclass(frozen=True)
class VerifiedRiskUnlock:
    """驗證通過的「立即解除熔斷鎖定」請求（一次性動作）。"""

    account_id: str
    user_address: str
    nonce: str
    issued_at: str
    issued_at_s: float


def _require_str(record: dict, key: str) -> str:
    v = record.get(key)
    if not isinstance(v, str) or not v:
        raise RiskSettingsError("malformed", f"{key} 缺漏或非字串: {v!r}")
    return v


def _canonical_or_malformed(prefs: object) -> dict:
    """`risk_prefs.canonical_prefs` 的薄包裝，錯誤型別轉成本模組的 `malformed`。

    ⭐ 正規化與區間的**唯一**定義點是 `risk_prefs`（見檔頭）。本函式只做型別轉譯：
    呼叫端只該需要處理**一種**失敗型別（沿 capital_settings 對 LeaderChangeError
    的同一個處理）。
    """
    if prefs is not None and not isinstance(prefs, dict):
        raise RiskSettingsError("malformed", "prefs 必須是一個物件")
    try:
        return canonical_prefs(prefs)
    except RiskPrefsError as e:
        raise RiskSettingsError("malformed", f"風控偏好不合法（{e.reason}）") from e


def pref_message_lines(prefs: dict) -> list[str]:
    """待簽訊息裡逐項列出的風控參數（**寫端與讀端的單一定義**）。

    順序＝`RISK_PARAM_SPECS` 的順序，前面加一行 `Risk Controls:` 代表總開關。
    標籤用 spec 的 `name`（不是 `label`）：`label` 是給前端顯示的文案，改一次文案
    就會讓「同一組數值」產生另一份原文，而客戶手上剛簽好的記錄會突然驗不過。
    `name` 是這個參數的識別碼，改它本來就是一次破壞性變更。

    值一律取自**已 canonical 化**的 prefs（`canonical_prefs` 的產物）：
    `0.2`、`0.20`、`0.200` 是同一個數卻是三個不同的字串、三個不同的雜湊，
    而正規化的單一來源在 `risk_prefs`，不在這裡（工程原則 1）。
    """
    lines = [f"{ENABLED_MESSAGE_LABEL}: "
             f"{'enabled' if prefs['enabled'] else 'disabled'}"]
    for spec in RISK_PARAM_SPECS:
        v = prefs[spec["name"]]
        text = "true" if v is True else "false" if v is False else str(v)
        unit = spec.get("unit")
        lines.append(f"{spec['name']}: {text}{f' {unit}' if unit else ''}")
    return lines


def build_risk_settings_message(*, account_id: str, prefs: dict, nonce: str,
                                issued_at: str) -> str:
    """待簽訊息的**唯一**版型（伺服器與引擎都用它重建，客戶端照此組字串簽名）。

    ⭐⭐ 第一行 `"Filet: update copy-trading risk settings"` 是**域分隔符**：
    固定字面量，沒有任何呼叫端輸入能到達它；其餘三個模板（換 leader、資金設定、
    解除熔斷）的第一行是另外三個固定字面量。完整論證見檔頭「域分隔」。

    ⭐ 每一個參數都逐行寫出來（`pref_message_lines`），不是一個雜湊、也不是只寫
    被改動的那幾項：客戶在錢包裡看到的就是這段文字，「我簽的是什麼」是這道防線
    唯一的價值來源。

    `prefs` 在此 canonical 化（`risk_prefs.canonical_prefs`），與 capital 把金額
    canonical 化放進模板是同一個決定：正規化在模板這一層，兩邊才結構上不可能組出
    不同的字串。
    """
    p = _canonical_or_malformed(prefs)
    body = "\n".join(pref_message_lines(p))
    return (
        "Filet: update copy-trading risk settings\n"
        "\n"
        "Signing this authorises Filet to change the risk controls on your\n"
        "copy-trading account. These thresholds decide when trading is halted to\n"
        "protect you; loosening them means your account can lose more before the\n"
        "halt triggers, and turning risk controls off means it never triggers.\n"
        "They take effect on the engine's next cycle. No positions are opened or\n"
        "closed by this change itself.\n"
        "\n"
        f"Account: {account_id}\n"
        f"{body}\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued_at}"
    )


def build_risk_unlock_message(*, account_id: str, nonce: str,
                              issued_at: str) -> str:
    """「立即解除熔斷鎖定」的待簽訊息（**一次性動作**的唯一版型）。

    ⭐ 訊息用白話寫明後果，因為這是客戶**自己拿掉一道保護**的動作：解除之後引擎會
    在下一輪恢復交易；權益基準（7 天滾動樣本與全期高水位）已在熔斷當下重置，所以
    恢復後不會被崩跌前的舊 peak 立刻再熔斷——這一句必須讓客戶在按下簽名之前看到，
    否則他會以為「解鎖＝立刻又要再熔斷一次」而不敢用，或反過來以為保護還在原位。
    """
    return (
        "Filet: resume copy-trading after a risk halt\n"
        "\n"
        "Your copy-trading was halted by a risk control. Signing this tells Filet\n"
        "to lift that halt immediately, without waiting for the cooldown period.\n"
        "The engine resumes trading on its next cycle, which may open new positions\n"
        "that follow your leader again.\n"
        "Your equity baseline was already reset when the halt triggered, so the\n"
        "drawdown that caused it will not immediately halt you a second time.\n"
        "This authorises one resume only. It does not change your risk settings,\n"
        "and it does not apply to a halt caused by your leader being revoked.\n"
        "\n"
        f"Account: {account_id}\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued_at}"
    )


def build_risk_settings_record(*, account_id: str, prefs: dict, nonce: str,
                               issued_at: str, signature: str,
                               message: str) -> dict:
    """組出落檔用的記錄 dict（欄位順序固定＝RISK_SETTINGS_FIELDS，diff 友善）。

    `action` 由本函式**寫死**成 `ACTION_RISK_SETTINGS`，不從參數收：呼叫端能指定
    動作類型的話，一個手滑就能造出一筆自稱是解鎖請求的設定記錄。`prefs` 一律
    canonical 化後落檔——落進檔案的值與待簽訊息裡的值必須是同一個，否則引擎重建
    訊息時會得到另一份原文（工程原則 1）。
    """
    return {"action": ACTION_RISK_SETTINGS, "account_id": account_id,
            "prefs": _canonical_or_malformed(prefs), "nonce": nonce,
            "issued_at": issued_at, "signature": signature, "message": message}


def build_risk_unlock_record(*, account_id: str, nonce: str, issued_at: str,
                             signature: str, message: str) -> dict:
    """解鎖記錄（欄位順序＝RISK_UNLOCK_FIELDS）。`action` 同樣由本函式寫死。"""
    return {"action": ACTION_RISK_UNLOCK, "account_id": account_id, "nonce": nonce,
            "issued_at": issued_at, "signature": signature, "message": message}


def _verify_common(record: dict, *, action: str, account_id: str, user_address: str,
                   now_s: float, max_age_s: float | None
                   ) -> tuple[str, str, str, float]:
    """動作類型 → 帳號 → 格式 → 時效 的共同前段；回 `(user, nonce, issued_at, ts)`。

    順序與 `verify_capital_settings` 完全相同，且動作類型擺第一（讓跨域重放得到
    **指名事件**的拒絕理由 `action_mismatch`，而不是籠統的「格式錯」）。
    重建訊息、recover、比對簽章者、消耗 nonce 由兩個 verify 函式各自完成——那幾步
    的訊息版型不同，共用會逼兩種動作互相遷就。

    `max_age_s=None` ⇒ **不檢查時效**（持續意圖用；理由見檔頭）。
    """
    validate_account_id(account_id)

    got = record.get("action")
    if got != action:
        raise RiskSettingsError(
            "action_mismatch",
            f"記錄的動作類型不是 {action}（收到 {got!r}）——拒絕。一筆風控設定的"
            f"授權絕不能被當成一次解除熔斷鎖定的授權（反向亦然）")

    expected_user = normalize_hex_address("user_address", user_address)

    claimed_account = _require_str(record, "account_id")
    if claimed_account != account_id:
        raise RiskSettingsError(
            "account_mismatch",
            f"記錄的 account_id（{claimed_account!r}）與待驗帳號（{account_id!r}）不符")

    nonce = _require_str(record, "nonce")
    if not _NONCE_RE.fullmatch(nonce):
        raise RiskSettingsError("malformed", f"nonce 格式不合法: {nonce!r}")
    issued_at = _require_str(record, "issued_at")

    # 時效：兩側都換算成 epoch 秒再比（同源同單位，工程原則 1）。解析器沿用
    # leader_change.parse_issued_at——兩份解析器對「naive 時間算不算數」遲早會有
    # 兩個答案，而那半個答案正好是擋重放的那一半。
    try:
        issued_ts = parse_issued_at(issued_at).timestamp()
    except LeaderChangeError as e:
        raise RiskSettingsError(e.reason, str(e)) from e
    if max_age_s is not None:
        age_s = now_s - issued_ts
        if age_s > max_age_s:
            raise RiskSettingsError(
                "expired",
                f"簽章已過期（{age_s:.0f}s > {max_age_s}s）——"
                f"請重新取得待簽原文並重簽")
        if age_s < -max_age_s:
            raise RiskSettingsError(
                "expired",
                f"issued_at 位於未來（{-age_s:.0f}s）——拒絕；"
                f"合法流程的時間戳由伺服器發 nonce 時決定")
    return expected_user, nonce, issued_at, issued_ts


def _recover_or_reject(expected_message: str, signature: str,
                       expected_user: str, what: str) -> None:
    """recover ＋ 比對簽章者。**全檔最關鍵的一段**（同 capital_settings）。

    拿掉比對，攻擊者自簽一筆格式完美的記錄就能關掉任何客戶的風控、或把他的熔斷鎖
    打開。`expected_message` 由**可信的 account_id** 與記錄裡的 canonical 值重建，
    `record["message"]` 只是稽核留存，刻意不讀（直接驗「這個簽章有簽這個 message」
    是假驗證——攻擊者同時提供兩者，當然自洽）。
    """
    try:
        signer = recover_personal_sign_address(expected_message, signature)
    except Exception as e:  # noqa: BLE001 —— 壞簽名格式一律轉 semantic 拒絕
        raise RiskSettingsError("bad_signature",
                                "簽章無法還原（格式錯誤或損毀）") from e
    if signer != expected_user:
        raise RiskSettingsError(
            "signer_mismatch",
            f"簽章者不是該帳號的持有人——拒絕（{what}）")


def verify_risk_settings(record: dict, *, account_id: str, user_address: str,
                         now_s: float, consume_nonce: Callable[[str], bool],
                         max_age_s: float | None = None) -> VerifiedRiskSettings:
    """驗證一筆風控設定記錄；通過回 VerifiedRiskSettings，否則拋 RiskSettingsError。

    參數的信任分級**與 verify_capital_settings 完全相同**：`account_id`／
    `user_address` 來自可信來源（引擎取 manifest、API 取 session），`record` 完全
    不可信，`consume_nonce` 是唯一的副作用且擺在最後（在格式錯或簽章錯時就燒掉
    nonce，等於讓任何人送一筆垃圾記錄就能作廢客戶手上的合法授權）。

    ⭐⭐ `max_age_s` 預設 **None ＝不檢查時效**。風控設定是**持續意圖**，不是一次性
    動作：一份三天前簽的「回撤上限 20%」今天仍然是客戶的意圖。API 側可以（也應該）
    傳 `RISK_SETTINGS_MAX_AGE_S` 把它收緊，因為那裡驗的是「客戶剛剛按下的那一次」；
    引擎側一律放行時效——否則引擎停機超過 10 分鐘後，客戶的設定會因為過期而**無聲地
    退回 env 值**。既有先例：`scripts/filet_auto_activate._latest_signed_leader`。

    檢查順序：動作類型 → 帳號 → 格式 → 時效 → 重建訊息 → recover → 比對簽章者
    → 消耗 nonce。
    """
    expected_user, nonce, issued_at, issued_ts = _verify_common(
        record, action=ACTION_RISK_SETTINGS, account_id=account_id,
        user_address=user_address, now_s=now_s, max_age_s=max_age_s)

    prefs = _canonical_or_malformed(record.get("prefs"))
    signature = _require_str(record, "signature")

    _recover_or_reject(
        build_risk_settings_message(account_id=account_id, prefs=prefs,
                                    nonce=nonce, issued_at=issued_at),
        signature, expected_user,
        "能改這些門檻的人就能拿掉客戶帳戶的全部保護")

    if not consume_nonce(nonce):
        raise RiskSettingsError(
            "nonce_unusable",
            "nonce 不存在、已用過或已過期——同一份簽章只能兌現一次")

    return VerifiedRiskSettings(account_id=account_id, user_address=expected_user,
                                prefs=prefs, nonce=nonce, issued_at=issued_at,
                                issued_at_s=issued_ts)


def verify_risk_unlock(record: dict, *, account_id: str, user_address: str,
                       now_s: float, consume_nonce: Callable[[str], bool],
                       max_age_s: float | None = RISK_SETTINGS_MAX_AGE_S
                       ) -> VerifiedRiskUnlock:
    """驗證一筆「立即解除熔斷鎖定」請求；通過回 VerifiedRiskUnlock。

    ⭐⭐ 與 `verify_risk_settings` 相反，時效預設**強制**（`RISK_SETTINGS_MAX_AGE_S`
    ＝600 秒）：解鎖是一次性動作，「現在恢復跟單」是一個**當下**的決定。一份三天前
    的解鎖記錄若還能生效，客戶等於簽一次就永久放棄了熔斷保護——今天熔斷、舊記錄
    自動把它解開，而且完全安靜。呼叫端（引擎）不得傳 None 把它關掉。
    """
    expected_user, nonce, issued_at, issued_ts = _verify_common(
        record, action=ACTION_RISK_UNLOCK, account_id=account_id,
        user_address=user_address, now_s=now_s, max_age_s=max_age_s)

    signature = _require_str(record, "signature")
    _recover_or_reject(
        build_risk_unlock_message(account_id=account_id, nonce=nonce,
                                  issued_at=issued_at),
        signature, expected_user, "解除熔斷鎖定會讓引擎恢復用真錢下單")

    if not consume_nonce(nonce):
        raise RiskSettingsError(
            "nonce_unusable",
            "nonce 不存在、已用過或已過期——同一份簽章只能兌現一次")

    return VerifiedRiskUnlock(account_id=account_id, user_address=expected_user,
                              nonce=nonce, issued_at=issued_at,
                              issued_at_s=issued_ts)


def risk_settings_path_for(exchange_dir: str | Path) -> str:
    """風控設定記錄檔的路徑（**寫端與讀端的單一定義**，同 capital_settings_path_for）。

    ⭐ 與資金設定、換 leader **各自一個檔**：三份記錄的讀者是三個獨立的套用器，
    共用一個檔會讓其中一方的格式問題連坐另外兩方——而它們各自都能造成資金損失。
    """
    return str(Path(exchange_dir) / "risk_settings.json")


def risk_unlock_path_for(exchange_dir: str | Path) -> str:
    """解鎖記錄檔的路徑。與風控設定**分開一個檔**：一個是持續意圖、一個是一次性
    動作，時效語意相反；同一個檔會讓兩種語意共命運，而弄錯的方向是 fail-open
    （一份舊的解鎖記錄被當成持續意圖套用＝熔斷保護永久失效）。"""
    return str(Path(exchange_dir) / "risk_unlock.json")


def _load(path: str | Path, key: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return json.loads(p.read_text()).get(key, [])


def load_risk_settings(path: str | Path) -> list[dict]:
    """讀風控設定記錄檔；不存在 → 空清單（尚無人簽過風控設定是正常狀態）。

    **刻意不在載入時驗證**（同 load_capital_settings）：驗證需要可信來源的
    user_address，載入層拿不到。分開之後就不會有人以為「載入成功＝已驗過」。
    """
    return _load(path, "settings")


def load_risk_unlocks(path: str | Path) -> list[dict]:
    """讀解鎖記錄檔；不存在 → 空清單。"""
    return _load(path, "unlocks")


def write_risk_settings(path: str | Path, record: dict) -> None:
    """落檔（原子換檔，走 safe_fs；沿 write_capital_settings 的慣例）。

    **同 account_id 覆蓋而非附加**：檔案代表「每位客戶當前的風控意圖」，不是流水帳。
    附加會留下一堆舊意圖，而套用端只要挑錯一筆就是把客戶的保護設回他早已改掉的值。
    """
    p = Path(path)
    entries = [e for e in load_risk_settings(p)
               if e.get("account_id") != record["account_id"]]
    entries.append(record)
    # 0644：讀端 filet-engine 是另一個 user（交換目錄無 setgid，見 user_leaders.py）。
    write_json_atomic(p, {"settings": entries}, mode=0o644)


def write_risk_unlock(path: str | Path, record: dict) -> None:
    """落檔（同 account 覆蓋）。舊的解鎖請求被新的蓋掉是正確語意：解鎖是一次性
    動作，同一個客戶手上不該同時有兩筆有效的解鎖授權。"""
    p = Path(path)
    entries = [e for e in load_risk_unlocks(p)
               if e.get("account_id") != record["account_id"]]
    entries.append(record)
    write_json_atomic(p, {"unlocks": entries}, mode=0o644)
