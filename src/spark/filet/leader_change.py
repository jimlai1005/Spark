"""src/spark/filet/leader_change.py
**客戶簽章的換 leader 記錄**——格式、驗證原語、落檔讀寫。

⭐ 為什麼信任錨是客戶的簽章，不是營運端的自律
---------------------------------------------
客戶要能自己換 leader。但**營運方從客戶的換手中賺 builder fee**——每一次換 leader
都會讓引擎收斂到新 leader 的部位（平舊開新，產生實際的 taker 量），而 taker 量正是
營運方的收入。從 churn 獲利的一方不該同時是守住 churn 的那一方；「我們不會亂改客戶
的 leader」是一句無法被驗證的承諾。

白名單（leaders.py）**回答不了這個問題**。它回答的是「這個 leader 一般而言可不可
接受」，不是「這位客戶真的要求換到他嗎」。沒有簽章的話，任何能寫入變更的人（被打穿
的 API 進程、內部誤操作、惡意營運）都能把客戶指向白名單內、但對他完全不適合的 leader
（例如把保守客戶指向 10x 槓桿高波動策略），白名單全程放行——**一次切換就能造成實質
損失**，而事後看紀錄一切合規。

所以：API 只寫一筆**帶客戶簽章的變更記錄**，引擎在套用前**自己重新驗章**。
API 進程被打穿也偽造不出客戶的簽章；它能做的最壞的事是丟掉／延遲一筆合法記錄
（可用性問題），而不是無中生有一次換手（資金問題）。這與 leader_resolve.py 的
「引擎二次驗白名單」是同一個結構：**能寫的人與能驗的人必須是兩個信任域**。

⭐ 驗證規則：不得信任記錄裡的任何身分宣稱
-----------------------------------------
`verify_leader_change` 的正確用法（照這個順序想）：

1. `user_address` 由**呼叫端從可信來源**取得——引擎取 manifest 的
   `FollowerRef.user_address`，API 取 SIWE session 綁定的位址。**不是**從記錄裡讀。
2. `account_id` 同上（呼叫端宣告「我正在驗哪個帳號的記錄」）。
3. 用「可信的 account_id」＋「記錄裡的 leader_address / nonce / issued_at」
   **重建**預期的訊息原文。
4. 對 `signature` recover 出簽章者，斷言 **recover 出的簽章者 == user_address**。

記錄裡的 `message` 欄位**純為稽核留存，驗證完全不看它**（見 `_MESSAGE_IS_AUDIT_ONLY`
的測試）。直接驗「這個 signature 有簽這個 message」是**假驗證**：攻擊者同時提供
message 與 signature，兩者當然自洽——那樣驗了等於沒驗。同理不得用記錄自帶的 signer
欄位當比對基準（本格式因此**刻意沒有** signer 欄位：沒有這個欄位，就沒有人能誤用它）。

其餘綁定：訊息綁 `account_id`＋`leader_address`＋`nonce`＋`issued_at`，任一不同即
重建出不同訊息 → recover 出不同位址 → 拒絕。所以「把記錄挪到別的帳號」「把 leader
換掉沿用舊簽章」兩種重放都由**同一條**簽章者比對擋下，不需要各自寫一道檢查。

nonce 一次性由呼叫端注入的 `consume_nonce` 提供（見該參數說明）——做成**必填**而非
選填：一次性是這份記錄「只能被兌現一次」的唯一保證，做成可省略的參數等於允許呼叫端
安靜地關掉它（工程原則 5：分類要在呼叫點被結構性地強制宣告）。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from spark.filet.followers import normalize_hex_address, validate_account_id
from spark.filet.signing import recover_personal_sign_address

# 簽章時效上限（秒）。客戶簽下的意圖只在這段時間內可兌現——過期的簽章不該還能換
# leader（換手有實際交易成本，一筆三小時前的意圖不代表現在的意圖）。
# 對稱地也擋「未來時間」：合法流程的 issued_at 由伺服器發 nonce 時決定，明顯未來
# 的時間戳只可能來自竄改或時鐘漂移，兩者都不該放行。
LEADER_CHANGE_MAX_AGE_S = 600

# 記錄的鍵集（多一個少一個都要有人主動改這行，沿 _LEADER_STAT_FIELDS 的慣例）。
LEADER_CHANGE_FIELDS = ("account_id", "leader_address", "nonce", "issued_at",
                        "signature", "message")

# nonce 字元集：伺服器發的是 secrets.token_hex(16)，這裡放寬到 URL-safe 但**必須**
# 收斂——nonce 會被拼進待簽訊息的一行，允許換行等於允許攻擊者自己捏造額外欄位。
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class LeaderChangeError(ValueError):
    """換 leader 記錄驗證失敗（**一律 semantic：不重試**）。

    `reason` 是給呼叫端做失敗分類用的機器可讀碼，不要比對訊息字串（訊息會改）。
    所有 reason 的共同性質：重試同一筆記錄必定再次失敗——它不是 IO 打嗝，是
    「這筆記錄不該被套用」。API 轉 4xx、引擎轉「拒絕套用並留痕」，都不得重試。
    """

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class VerifiedLeaderChange:
    """驗證通過的換 leader 意圖。欄位皆為**已驗證後的正規化值**，呼叫端直接用這個，
    不要回頭讀原始 record（回頭讀就有機會讀到未經正規化／未經驗證的那一份）。"""

    account_id: str
    user_address: str      # 驗章通過的簽章者＝可信來源給的 user_address
    leader_address: str    # 正規化小寫
    nonce: str
    issued_at: str


def build_leader_change_message(*, account_id: str, leader_address: str,
                                nonce: str, issued_at: str) -> str:
    """待簽訊息的**唯一**版型（伺服器與引擎都用它重建，客戶端照此組字串簽名）。

    刻意與 SIWE 版型（publicapi/siwe.py）長得完全不一樣、且開頭就寫明用途：兩種訊息
    共用同一把私鑰與同一個 nonce 池，版型若可互相解讀，一次登入簽名就能被當成一次
    換 leader 授權（跨協定重放）。開頭的固定字串就是域分隔符。

    第一行寫明後果（不是裝飾）：客戶在錢包裡看到的就是這段文字，「會平掉舊部位、
    開新部位、有實際交易成本」必須出現在他按下簽名之前。

    ⭐ `leader_address` 在此**正規化小寫**：以太坊位址大小寫不敏感，但簽的是位元組。
    若讓呼叫端各自決定大小寫，客戶端用 checksum 形式簽、伺服器用小寫重建，就會得到
    兩份不同的訊息、recover 出不同的位址，症狀是「明明是本人簽的卻一直被拒」。
    正規化放在版型這一層＝兩邊結構上不可能組出不同的字串（工程原則 1）。
    """
    leader_address = normalize_hex_address("leader_address", leader_address)
    return (
        "Filet: change copy-trading leader\n"
        "\n"
        "Signing this authorises Filet to switch the leader your account copies.\n"
        "The engine will converge to the new leader's positions on its next cycle:\n"
        "existing positions are closed and new ones opened, at real trading cost.\n"
        "\n"
        f"Account: {account_id}\n"
        f"Leader: {leader_address}\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued_at}"
    )


def _parse_issued_at(raw: object) -> datetime:
    """ISO8601 → aware UTC datetime。壞格式／裸 naive 時間一律拒絕。

    naive（無時區）也拒絕：把它當成 UTC 是一個**看不見的假設**，客戶端若送的是本地
    時間，時效檢查就會憑空多／少幾小時——而時效檢查正是擋重放的那一半。
    """
    if not isinstance(raw, str) or not raw:
        raise LeaderChangeError("malformed", f"issued_at 缺漏或非字串: {raw!r}")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as e:
        raise LeaderChangeError("malformed",
                                f"issued_at 不是合法 ISO8601: {raw!r}") from e
    if dt.tzinfo is None:
        raise LeaderChangeError("malformed",
                                f"issued_at 必須帶時區（UTC）: {raw!r}")
    return dt.astimezone(timezone.utc)


def _require_str(record: dict, key: str) -> str:
    v = record.get(key)
    if not isinstance(v, str) or not v:
        raise LeaderChangeError("malformed", f"{key} 缺漏或非字串: {v!r}")
    return v


def verify_leader_change(record: dict, *, account_id: str, user_address: str,
                         now_s: float,
                         consume_nonce: Callable[[str], bool]) -> VerifiedLeaderChange:
    """驗證一筆換 leader 記錄；通過回 VerifiedLeaderChange，否則拋 LeaderChangeError。

    參數的信任分級（**最重要的一段，改這個函式前先讀**）：
    - `account_id` / `user_address`：**可信來源**。引擎給 manifest 的值、API 給
      session 綁定的值。絕不是從 `record` 讀出來的。
    - `record`：**完全不可信**。攻擊者若能寫這個檔，整份內容都由他決定——包含
      `account_id`、`message` 這些看起來像身分宣稱的欄位。唯一擋得住他的是
      「recover 出的簽章者 == 可信來源的 user_address」這一條。
    - `now_s`：epoch 秒（與 issued_at 換算後同單位比較，工程原則 1）。
    - `consume_nonce(nonce) -> bool`：**原子地**把這個 nonce 標記為已用；回 False
      代表不存在／已用過／已過期 → 拒絕。API 側接 ApiStore.consume_nonce（單一 UPDATE，
      非 TOCTOU）；引擎側接自己的已兌現帳本。

    檢查順序刻意是「無副作用的先做、消耗 nonce 最後做」：nonce 是一次性資源，
    在格式錯或簽章錯時就把它燒掉，等於讓任何人送一筆垃圾記錄就能作廢客戶手上那張
    合法的授權（自我 DoS）。這與 auth_verify 先 consume 再驗的順序不同，理由是那邊
    **必須**先讀 nonce 記錄才拿得到重建訊息所需的 address/issued_at；本記錄自帶
    issued_at，沒有這個依賴，所以能把唯一的副作用擺到最後。
    """
    validate_account_id(account_id)
    expected_user = normalize_hex_address("user_address", user_address)

    # 記錄自稱的 account_id 必須與可信來源一致。這條**不是**主要防線（攻擊者當然會
    # 把它改成受害者的 account_id）——它擋的是誤放：一筆合法記錄被寫錯槽位時
    # 早點喊出來，而不是讓它一路走到簽章比對才以「簽章者不符」的模糊訊息失敗。
    claimed_account = _require_str(record, "account_id")
    if claimed_account != account_id:
        raise LeaderChangeError(
            "account_mismatch",
            f"記錄的 account_id（{claimed_account!r}）與待驗帳號（{account_id!r}）不符")

    # record 側的格式錯一律轉成 LeaderChangeError：呼叫端只該需要處理**一種**失敗
    # 型別；讓 normalize_hex_address 的裸 ValueError 逸出去，遲早有一個呼叫端只接了
    # LeaderChangeError 而在壞位址上炸成 500（該回 4xx 的 semantic 失敗）。
    try:
        leader_address = normalize_hex_address("leader_address",
                                               _require_str(record, "leader_address"))
    except LeaderChangeError:
        raise
    except ValueError as e:
        raise LeaderChangeError("malformed", f"leader_address 不合法: {e}") from e
    nonce = _require_str(record, "nonce")
    if not _NONCE_RE.fullmatch(nonce):
        raise LeaderChangeError("malformed", f"nonce 格式不合法: {nonce!r}")
    issued_at = _require_str(record, "issued_at")
    signature = _require_str(record, "signature")

    # 時效：兩側都換算成 epoch 秒再比（同源同單位，工程原則 1）。
    age_s = now_s - _parse_issued_at(issued_at).timestamp()
    if age_s > LEADER_CHANGE_MAX_AGE_S:
        raise LeaderChangeError(
            "expired",
            f"簽章已過期（{age_s:.0f}s > {LEADER_CHANGE_MAX_AGE_S}s）——"
            f"請重新取得 nonce 並重簽")
    if age_s < -LEADER_CHANGE_MAX_AGE_S:
        raise LeaderChangeError(
            "expired",
            f"issued_at 位於未來（{-age_s:.0f}s）——拒絕；"
            f"合法流程的時間戳由伺服器發 nonce 時決定")

    # ⭐ 伺服器權威重建（沿 SIWE 的精神）：訊息由**可信的 account_id** 與記錄裡的
    # 意圖欄位組出來，`record["message"]` 只是稽核留存，這裡刻意不讀。
    expected_message = build_leader_change_message(
        account_id=account_id, leader_address=leader_address,
        nonce=nonce, issued_at=issued_at)
    try:
        signer = recover_personal_sign_address(expected_message, signature)
    except Exception as e:  # noqa: BLE001 —— 壞簽名格式一律轉 semantic 拒絕，不外洩內部
        raise LeaderChangeError("bad_signature", "簽章無法還原（格式錯誤或損毀）") from e

    # ⭐⭐ 全檔最關鍵的一行。拿掉它，上面所有「重建訊息」的工都白做——攻擊者自簽一筆
    # 格式完美的記錄就能換掉任何客戶的 leader。改動此處請先讀檔頭威脅模型。
    if signer != expected_user:
        raise LeaderChangeError(
            "signer_mismatch",
            "簽章者不是該帳號的持有人——拒絕。這是本模組存在的理由：白名單管得了"
            "「換到誰」，管不了「是不是本人要求的」")

    # 最後才動唯一的副作用（見 docstring 的順序理由）。
    if not consume_nonce(nonce):
        raise LeaderChangeError(
            "nonce_unusable",
            "nonce 不存在、已用過或已過期——同一份簽章只能兌現一次")

    return VerifiedLeaderChange(account_id=account_id, user_address=expected_user,
                                leader_address=leader_address, nonce=nonce,
                                issued_at=issued_at)


def build_leader_change_record(*, account_id: str, leader_address: str, nonce: str,
                               issued_at: str, signature: str, message: str) -> dict:
    """組出落檔用的記錄 dict（欄位順序固定，diff 友善）。

    `message` 原樣留存**只為稽核**：驗證端重建自己的版本（見 verify_leader_change），
    兩者不一致時這個欄位就是唯一能看出「客戶當初到底簽了什麼」的證據。
    """
    return {"account_id": account_id, "leader_address": leader_address,
            "nonce": nonce, "issued_at": issued_at, "signature": signature,
            "message": message}


def leader_changes_path_for(exchange_dir: str | Path) -> str:
    """由**交換目錄**推導出變更記錄檔的路徑（**寫端與讀端的單一定義**）。

    ⭐ 為什麼是一個函式而不是兩邊各寫一行字串拼接：這個檔有兩個不同進程的使用者
    ——filet-api 寫（ApiConfig.leader_changes_path）、引擎讀
    （leader_change_apply.resolve_changes_path）。兩邊各自拼一次就是兩份會漂移的
    定義，而漂移的症狀是**靜默的**：客戶按了換 leader、API 回 200、記錄確實落地，
    引擎卻永遠讀不到那個檔——兩邊的 log 都顯示一切正常（工程原則 1：被比較／被配對
    的兩個值必須同源、同處計算）。

    ⭐⭐ 錨點是**交換目錄本身**，不是 pending.json（2026-07-19 opus 審查 C3 的修法）
    ------------------------------------------------------------------------
    舊版由 `pending.json` 推導。那個錨點選錯了：pending.json 是 **API 私有**的產物
    （內含活化前的客戶資料，必須只有 filet-api 讀得到），而變更記錄是 **API 與引擎
    共享**的產物。把共享物錨在私有物身上，兩者被迫同目錄，而該目錄的權限只能滿足
    其中一方——實際部署的結果是 API 寫 `/var/lib/filet-api/leader_changes.json`、
    引擎讀 repo 內的 `var/filet/leader_changes.json`，**路徑根本不一致**，功能完全
    不通，而 API 仍回客戶「於引擎的下一個 cycle 生效」（它永遠不會生效）。

    修法是給共享物一個**專屬目錄**（`FILET_EXCHANGE_DIR`，正式部署
    `/var/lib/filet-exchange`，`filet-api:filet-engine` 0750）：API 寫、引擎讀、
    引擎無寫權。方向單向，被打穿的引擎污染不了 API 狀態；權限拓撲與部署步驟見
    deploy/RUNBOOK.md §5.5.1。
    """
    return str(Path(exchange_dir) / "leader_changes.json")


def load_leader_changes(path: str | Path) -> list[dict]:
    """讀變更記錄檔；不存在 → 空清單（尚無人換 leader 是完全正常的狀態）。

    **刻意不在載入時驗證**：驗證需要可信來源的 user_address，載入層拿不到；把
    「載入」與「驗證」分開，就不會有人以為「載入成功＝已驗過」。呼叫端一律要對每筆
    記錄呼叫 verify_leader_change。
    """
    p = Path(path)
    if not p.exists():
        return []
    return json.loads(p.read_text()).get("changes", [])


def write_leader_change(path: str | Path, record: dict) -> None:
    """落檔（原子換檔，沿 publicapi/pending.py 的 _atomic_write 慣例）。

    **同 account_id 覆蓋而非附加**：檔案代表「每位客戶當前的換手意圖」，不是流水帳。
    附加會留下一堆舊意圖，而套用端只要挑錯一筆就是把客戶送回他三天前想離開的
    leader——一個純為稽核而留的舊記錄，代價是一次真實的平倉開倉。稽核痕跡由套用端
    的 critical 告警與 log 提供（leader 變更本來就是必須留痕的重大事件）。
    """
    p = Path(path)
    entries = [e for e in load_leader_changes(p)
               if e.get("account_id") != record["account_id"]]
    entries.append(record)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"changes": entries}, indent=2))
    os.replace(tmp, p)  # 原子換檔，不留半寫
