"""src/spark/filet/capital_settings.py
**客戶簽章的資金設定記錄**（investing capital ＋ 使用比例）——格式、驗證原語、落檔。

本模組是 leader_change.py 的姊妹檔：**同一套信任錨、同一組威脅模型、同一個驗證
原語**（signing.recover_personal_sign_address，全 repo 唯一的 recover 實作）。
先讀 leader_change.py 檔頭，這裡只寫**不同**的部分。

⭐ 為什麼資金設定也必須是客戶簽章的
----------------------------------
`allocated_capital`（投入本金）與 `capital_utilization`（使用比例）這兩個值直接
**乘進部位大小**（sizing.compute_scale_factor:88-91）。能改它們的人就能改客戶的
曝險倍數：把使用比例從 0.2 拉到 1.0，客戶的部位瞬間變成五倍，清算距離縮到五分之一
——而帳戶裡每一筆交易看起來都完全正常，白名單全程放行（它管的是「跟誰」，
不是「押多大」）。這與換 leader 是同一類危害（一次寫入 ⇒ 實質資金損失），
所以用同一套防線：API 只寫一筆帶客戶簽章的記錄，引擎套用前**自己重新驗章**。

簽章機制既然已經存在，多蓋一個動作的邊際成本近乎零；反過來說，把一個同等危害的
旋鈕留在「登入後按個鈕就改」的等級，等於在同一道牆上留一扇沒鎖的門。

⭐⭐ 域分隔：兩種待簽訊息**結構上不可能碰撞**（本模組最重要的安全性質）
--------------------------------------------------------------------
威脅：客戶簽過一筆換 leader 授權，攻擊者把那份簽章挪來當成一次資金設定授權
（或反向）。若成立，一次合法的換 leader 就能被兌換成「使用比例拉滿」。

防線有兩層，**兩層都是結構性的、且兩個方向都成立**：

1. **訊息模板不可能產生同一字串**（最終防線，不依賴任何欄位檢查）。
   兩個模板的第一行是**固定字面量**，且**沒有任何呼叫端輸入能到達第一行**：
     - 換 leader：`"Filet: change copy-trading leader"`
     - 資金設定：`"Filet: update copy-trading capital allocation"`
   兩個字面量不相等 ⇒ 兩個模板產生的字串**第一行必定不同** ⇒ 字串必定不相等。
   所有可變輸入（account_id／位址／金額／nonce／issued_at）都出現在第一行之後，
   而且都在 `f"Label: {value}"` 的位置上——沒有任何一個輸入能插入或改寫第一行
   （值裡就算塞換行也只會在自己那一行之後多產生行，改不了已經寫定的第一行）。
   既然字串不同，personal_sign 的雜湊不同，recover 出的位址就必定不是同一個
   → 拿 A 的簽章去驗 B 必然 `signer_mismatch`。

   欄位標籤集合另外提供第二重不可碰撞性（`Leader:` vs `Allocated Capital:` ＋
   `Capital Utilization:`），所以就算未來有人改動第一行，碰撞仍然寫不出來。

2. **顯式的動作類型檢查**（早期、可讀的拒絕理由）。記錄帶 `action` 欄位，
   `verify_capital_settings` 要求它**恰好**等於 `ACTION_CAPITAL_SETTINGS`；
   `verify_leader_change` 對應地拒絕任何帶著外來 `action` 的記錄。
   這一層的價值不是「擋住攻擊」（第 1 層已經擋住了），而是**讓拒絕的理由指名
   真正的事件**：`action_mismatch` 一眼看得出有人在跨域重放，而籠統的
   `signer_mismatch` 會被讀成「客戶簽壞了」而被忽略（同 leader_change_apply 對
   nonce 重用與驗簽失敗刻意分開告警的理由）。

   ⚠️ 不對稱是刻意的：`verify_leader_change` 接受 `action` **缺席**（既有記錄格式
   沒有這個欄位，加成必填會讓所有既存記錄一次失效），只拒絕「帶著別的動作類型」。
   把 `action` 從資金記錄裡拿掉再餵給換 leader 驗證，會落回第 1 層防線
   （訊息模板不同 → signer_mismatch），仍然是拒絕——這正是為什麼第 1 層必須是
   結構性的而不能只靠欄位檢查。兩個方向的雙向拒絕由
   tests/test_capital_settings.py 的域分隔測試釘住。

⭐ 記錄**沒有** `signer` 欄位（與 leader_change 同）
--------------------------------------------------
期望簽章者一律取自可信來源：引擎取 manifest 的 `FollowerRef.user_address`，
API 取 SIWE session 綁定的位址。記錄裡沒有這個欄位，就沒有人能誤用它當比對基準。

⭐ 金額的 canonical 形式（工程原則 1：兩邊必須組出同一個字串）
------------------------------------------------------------
`1000`、`1000.0`、`1000.00` 是同一個數，卻是三個不同的字串、三個不同的雜湊。
若讓客戶端與伺服器各自決定怎麼印，症狀會是「我本人簽的卻一直被拒」，而兩邊的
log 都完全正常——與位址大小寫是同一類 bug（leader_change.build_leader_change_message
的 `normalize_hex_address` 註解）。修法同樣是把正規化放進**模板這一層**：
`_canonical_decimal` 固定小數位數，兩邊結構上不可能組出不同的字串。

小數位超過上限一律**拒絕**（`malformed`），不 quantize 掉：靜默截斷是在客戶不知情
的狀況下改動他簽署的數字，而這個數字直接乘進部位大小。

⭐ 本金的兩種模式：`use_full_equity` 是**顯式旗標**（2026-07-19）
----------------------------------------------------------------
引擎一直支援「用全部權益跟單」，但舊版是用 `allocated_capital == 0` 這個**值**
兼任開關。本模組的政策邊界要求 `allocated_capital > 0`（正確：客戶不該簽一筆
「本金 0」的授權），兩者相撞的結果是客戶**無法透過 API 表達「用全部權益」**。

修法不是放寬邊界（放寬會讓「手滑送 0」與「我要用全部權益」變成同一筆記錄，
而它們的曝險差距是整個帳戶），而是加一個顯式旗標，並讓兩個欄位只有兩種合法組合：

| use_full_equity | allocated_capital | 語意 |
|---|---|---|
| False（預設／舊記錄） | > 0 | 固定本金 |
| True | **恰好 0** | 全部權益 |

「兩個都有值」被 `validate_capital_bounds` 拒絕，不是「後者覆蓋前者」——客戶簽的
是人看得懂的文字，兩句矛盾的授權同時出現在錢包裡，他無從知道系統會執行哪一句。

旗標**受簽章保護**：它編碼進待簽訊息的 `Allocated Capital:` 那一行的值
（見 `build_capital_settings_message`），竄改它會讓重建的原文對不上 → `signer_mismatch`。
記錄裡缺席該欄位 → False（舊記錄的正確語意，故既有記錄不因新欄位失效）。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

from spark.filet.followers import normalize_hex_address, validate_account_id
from spark.filet.leader_change import (LEADER_CHANGE_MAX_AGE_S, LeaderChangeError,
                                       parse_issued_at)
from spark.filet.signing import recover_personal_sign_address

# ⭐ 動作類型標識。落進記錄、且驗證端**顯式比對**（見檔頭域分隔第 2 層）。
ACTION_CAPITAL_SETTINGS = "capital_settings"

# 簽章時效上限：直接沿用換 leader 的常數（同一個 nonce 域、同一個發放端點、同一種
# 「客戶當下的意圖」語意）。各寫一個數字就會出現兩套過期規則，而其中一套遲早被
# 調鬆卻沒有人注意到。
CAPITAL_SETTINGS_MAX_AGE_S = LEADER_CHANGE_MAX_AGE_S

# 記錄的鍵集（多一個少一個都要有人主動改這行）。⭐ 強制它的是測試不是註解
# （沿 LEADER_CHANGE_FIELDS 的既有教訓）：
#   tests/test_capital_settings.py::test_record_field_set_is_pinned_by_the_constant
#   tests/test_api_capital_settings.py::test_api_request_body_carries_exactly_the_record_fields
# ⚠️ **沒有 signer 欄位**（見檔頭）。`action` 排在最前面：它是這筆記錄「是什麼」的
# 宣告，讀檔的人第一眼就該看到。
CAPITAL_SETTINGS_FIELDS = ("action", "account_id", "allocated_capital",
                           "use_full_equity", "capital_utilization", "nonce",
                           "issued_at", "signature", "message")

# nonce 字元集：與 leader_change 同一條規則（同一個 nonce 池、同一個拼進訊息的風險）。
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# canonical 小數位數。兩個值的精度需求不同，所以是兩個常數而不是一個：
# - 本金以 USDC 計，2 位（分）足夠，且與交易所顯示的粒度一致。
# - 使用比例是 (0,1] 的比率，4 位 ＝ 0.01% 的解析度。
CAPITAL_DECIMALS = 2
UTILIZATION_DECIMALS = 4

# 數值上限（**格式**的健全性檢查，不是政策邊界——政策邊界見 validate_capital_bounds）。
# 存在理由：Decimal 接受 "1e100000" 這種輸入，把它印進待簽訊息會產生一個十萬位數的
# 字串。這是格式層該擋的東西，不該讓它一路走到簽章運算。
_MAX_ABS = Decimal("1e15")

# 政策邊界。⭐ 寫成模組常數而不是散在判斷式裡：API 與引擎是**兩個**執行點，
# 兩處各寫一次數字就會漂移，而漂移的方向不對稱——寬鬆的那一端會放行嚴格的那端
# 擋下的值，且症狀是一次超額曝險。
MIN_ALLOCATED_CAPITAL = Decimal("0")   # 嚴格大於
MAX_CAPITAL_UTILIZATION = Decimal("1")  # 含上界

# ⭐ 「用全部權益」模式在待簽訊息裡的**固定字面量**（取代金額）。
# 為什麼不是多加一行 `Use Full Equity: yes`：多一行會改動**所有**既有模式的訊息
# 版型，讓改動前發出的原文全部對不上；把旗標編碼進既有那一行的**值**，
# `use_full_equity=False` 的訊息與改動前**逐位元組相同**（向後相容），而兩種模式
# 的字串又必定不同（金額經 `_canonical_decimal` 一律是數字，永遠寫不出這個字面量
# ——結構上不可能碰撞，不依賴任何欄位檢查）。
FULL_EQUITY_MESSAGE_VALUE = "full account equity"


class CapitalSettingsError(ValueError):
    """資金設定記錄驗證失敗（**一律 semantic：不重試**）。

    `reason` 是機器可讀碼（不要比對訊息字串）。共同性質同 LeaderChangeError：
    重試同一筆記錄必定再次失敗。API 轉 4xx、引擎轉「拒絕套用並留痕」。
    """

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class VerifiedCapitalSettings:
    """驗證通過的資金設定意圖。欄位皆為**已驗證後的正規化值**。

    同時保留 Decimal（給計算用）與 canonical 字串（給落檔／重建訊息用）：
    兩者由同一次正規化產生，呼叫端不必自己再轉一次——自己轉就有機會轉出
    第二種字串形式，那正是本模組 canonical 化要消滅的東西。
    """

    account_id: str
    user_address: str            # 驗章通過的簽章者＝可信來源給的 user_address
    allocated_capital: Decimal
    capital_utilization: Decimal
    allocated_capital_str: str   # canonical，固定 CAPITAL_DECIMALS 位
    capital_utilization_str: str  # canonical，固定 UTILIZATION_DECIMALS 位
    nonce: str
    issued_at: str
    # ⭐ True ⇒ 客戶授權的是「用全部權益」，此時 `allocated_capital` 恆為 0
    # （由 validate_capital_bounds 強制，不是「有值但被忽略」）。
    use_full_equity: bool = False


def _canonical_decimal(field: str, raw: object, decimals: int) -> tuple[Decimal, str]:
    """`raw` → `(Decimal, canonical 字串)`；固定小數位數，兩端結構上同源。

    ⭐ 小數位超過上限 → **拒絕**（`malformed`），不 quantize。quantize 掉等於在
    客戶不知情的情況下改掉他簽署的數字，而這個數字直接乘進部位大小。
    「送 0.12345 的人得到 0.1234」是一次靜默的曝險變更，方向還不確定。
    """
    if isinstance(raw, bool) or raw is None:
        raise CapitalSettingsError("malformed", f"{field} 型別不合法")
    try:
        dec = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError, TypeError) as e:
        raise CapitalSettingsError("malformed", f"{field} 不是合法數值") from e
    if not dec.is_finite():
        raise CapitalSettingsError("malformed", f"{field} 必須是有限數值")
    if abs(dec) > _MAX_ABS:
        raise CapitalSettingsError("malformed", f"{field} 數值超出可表示範圍")
    exponent = dec.as_tuple().exponent
    if isinstance(exponent, int) and -exponent > decimals:
        raise CapitalSettingsError(
            "malformed",
            f"{field} 的小數位數超過 {decimals} 位——拒絕而非四捨五入："
            f"截斷會在客戶不知情的情況下改掉他簽署的數字")
    return dec, format(dec.quantize(Decimal(1).scaleb(-decimals)), "f")


def canonical_capital_values(allocated_capital: object,
                             capital_utilization: object
                             ) -> tuple[Decimal, str, Decimal, str]:
    """`(raw, raw)` → `(alloc, alloc_str, util, util_str)`（**公開的 canonical 入口**）。

    存在理由：API 層要在「發待簽原文之前」就把客戶送來的字串正規化並檢查邊界，
    而它不該自己再寫一次小數位規則——那就是第二份會漂移的定義。本模組是這兩個值
    的 canonical 形式的唯一來源，對外只開這一個門。
    """
    alloc, alloc_str = _canonical_decimal("allocated_capital", allocated_capital,
                                          CAPITAL_DECIMALS)
    util, util_str = _canonical_decimal("capital_utilization", capital_utilization,
                                        UTILIZATION_DECIMALS)
    return alloc, alloc_str, util, util_str


def validate_capital_bounds(allocated_capital: Decimal,
                            capital_utilization: Decimal, *,
                            use_full_equity: bool = False) -> None:
    """政策邊界：`capital_utilization ∈ (0, 1]`，本金依模式二選一：

    - `use_full_equity=False`（預設）→ `allocated_capital > 0`。
    - `use_full_equity=True`  → `allocated_capital` 必須**恰好為 0**。

    ⭐ 為什麼「必須為 0」而不是「有值也無所謂，反正會被忽略」：客戶簽的是一段
    人看得懂的文字，而「本金 1000 USDC ＋ 用全部權益」這兩句同時出現在錢包裡是
    自相矛盾的授權——他無從知道系統會執行哪一句。拒絕它，客戶就只會簽出恰好一種
    語意的授權；「忽略」則是讓系統替他選一句，而選錯沒有任何人會發現。
    記錄層因此不存在「兩個都有值」的狀態（`CopySettings.__post_init__` 在引擎層
    有同一條不變量，兩層各自獨立強制）。

    ⭐ 預設值 `use_full_equity=False` 是刻意選**限制較嚴**的那一邊：忘記傳這個
    參數的呼叫端會落到「必須 > 0」的分支，也就是**不可能**因為漏傳而放行一筆
    原本該被擋下的授權。預設值只會誤擋，不會誤放。

    ⭐⭐ 這個函式是**每個執行點各自呼叫**的，不是驗簽的一部分——刻意如此。
    `verify_capital_settings` 回答的是「這是不是客戶本人的意圖」（真實性），
    本函式回答的是「這個意圖可不可以被執行」（政策）。兩者分開的理由：
    政策是可能改的（未來也許開放 utilization > 1 的槓桿模式），真實性不會改；
    更重要的是，分開之後**引擎能夠、也必須自己再驗一次**，而不是繼承 API 的判斷。
    引擎端再驗的必要性見 capital_settings_apply.py 檔頭。

    ⚠️ **不夾取、不修正、不四捨五入**：超界一律 raise。夾取（clamp）是最誘人的
    錯誤選項——它讓流程順利跑完，代價是客戶簽了 A、系統執行了 B，而且沒有人會知道。
    """
    if use_full_equity:
        if allocated_capital != 0:
            raise CapitalSettingsError(
                "out_of_range",
                f"use_full_equity=True 時 allocated_capital 必須為 0"
                f"（收到 {allocated_capital}）——「用全部權益」與「本金 X」是兩句"
                f"互相矛盾的授權，不得同時出現在同一份客戶簽章裡")
    elif allocated_capital <= MIN_ALLOCATED_CAPITAL:
        raise CapitalSettingsError(
            "out_of_range",
            f"allocated_capital 必須 > {MIN_ALLOCATED_CAPITAL}"
            f"（收到 {allocated_capital}）——若要用全部權益，請顯式送 "
            f"use_full_equity=true，不要用金額 0 代表它")
    if not (Decimal("0") < capital_utilization <= MAX_CAPITAL_UTILIZATION):
        raise CapitalSettingsError(
            "out_of_range",
            f"capital_utilization 必須落在 (0, {MAX_CAPITAL_UTILIZATION}]"
            f"（收到 {capital_utilization}）")


def build_capital_settings_message(*, account_id: str, allocated_capital: object,
                                   capital_utilization: object, nonce: str,
                                   issued_at: str,
                                   use_full_equity: bool = False) -> str:
    """待簽訊息的**唯一**版型（伺服器與引擎都用它重建，客戶端照此組字串簽名）。

    ⭐⭐ 第一行 `"Filet: update copy-trading capital allocation"` 是**域分隔符**：
    它是固定字面量，沒有任何呼叫端輸入能到達它，而換 leader 模板的第一行是另一個
    固定字面量。兩者不相等 ⇒ 兩個模板的輸出第一行必定不同 ⇒ **不存在一組輸入能讓
    兩個模板產生同一字串**。完整論證見檔頭「域分隔」第 1 層。

    金額在此 canonical 化（固定小數位）——與 leader_change 把位址正規化小寫放進
    模板是同一個決定：正規化在模板這一層，兩邊才結構上不可能組出不同的字串。

    第一行之後的兩句寫明**後果與生效時機**（不是裝飾）：客戶在錢包裡看到的就是這段
    文字，「這會改變部位大小」「下一個 cycle 生效」「不會立刻強制再平衡」必須出現
    在他按下簽名之前。

    ⭐ 本金模式編碼在 `Allocated Capital:` 那一行的**值**上，不另加一行
    （理由與不可碰撞性見 `FULL_EQUITY_MESSAGE_VALUE`）：
      - `use_full_equity=False` → `Allocated Capital: 1000.00 USDC`（與改動前逐位元組相同）
      - `use_full_equity=True`  → `Allocated Capital: full account equity`
    兩者必定不同 ⇒ 一份「固定本金」的簽章無法被改寫成「用全部權益」（反向亦然）：
    竄改旗標會讓驗證端重建出另一份原文，recover 出的位址對不上 → `signer_mismatch`。
    旗標因此**受簽章保護**，而不是一個記錄裡可以任意翻轉的布林值。

    `use_full_equity=True` 時 `allocated_capital` 仍會被 canonical 化並要求為 0
    （`validate_capital_bounds` 的政策），避免記錄裡留著一個不會生效卻看得到的金額。
    """
    _, cap = _canonical_decimal("allocated_capital", allocated_capital,
                                CAPITAL_DECIMALS)
    _, util = _canonical_decimal("capital_utilization", capital_utilization,
                                 UTILIZATION_DECIMALS)
    cap_line = (FULL_EQUITY_MESSAGE_VALUE if use_full_equity else f"{cap} USDC")
    return (
        "Filet: update copy-trading capital allocation\n"
        "\n"
        "Signing this authorises Filet to change how much capital mirrors your leader.\n"
        "These values scale your position sizes directly: a higher utilisation means\n"
        "larger positions and a closer liquidation distance.\n"
        "It takes effect on the engine's next cycle. No immediate forced rebalance is\n"
        "performed; positions converge naturally as the leader trades.\n"
        "\n"
        f"Account: {account_id}\n"
        f"Allocated Capital: {cap_line}\n"
        f"Capital Utilization: {util}\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued_at}"
    )


def require_bool_flag(record: dict, key: str) -> bool:
    """記錄裡的布林旗標，**嚴格解析**：只接受真正的 JSON bool，缺席 → False。

    ⭐ 為什麼不接受 `"true"` / `1` / `"yes"`：這個旗標決定客戶的本金基準是「一個
    固定數字」還是「整個帳戶」。寬鬆解析等於在旗標周圍多開幾個等價寫法，而每個
    等價寫法都是一次「攻擊者送了某個真值、驗證端與套用端對它的解讀不一致」的機會。
    嚴格解析讓記錄的表示法唯一——不合法的型別一律 `malformed`，早退早拒。

    缺席 → False 是**舊記錄的正確語意**（本旗標問世前，記錄一律帶 `allocated_capital
    > 0` 的固定本金），所以既有記錄不因新欄位而失效；且缺席落到的是限制較嚴的
    那一邊，漏帶欄位不可能放行一筆「用全部權益」的授權。
    """
    v = record.get(key)
    if v is None:
        return False
    if not isinstance(v, bool):
        raise CapitalSettingsError(
            "malformed",
            f"{key} 必須是布林值（收到 {type(v).__name__}）——"
            f"字串／數字一律拒絕，這個旗標決定本金基準，表示法必須唯一")
    return v


def _require_str(record: dict, key: str) -> str:
    v = record.get(key)
    if not isinstance(v, str) or not v:
        raise CapitalSettingsError("malformed", f"{key} 缺漏或非字串: {v!r}")
    return v


def verify_capital_settings(record: dict, *, account_id: str, user_address: str,
                            now_s: float,
                            consume_nonce: Callable[[str], bool]
                            ) -> VerifiedCapitalSettings:
    """驗證一筆資金設定記錄；通過回 VerifiedCapitalSettings，否則拋 CapitalSettingsError。

    參數的信任分級**與 verify_leader_change 完全相同**（先讀那份 docstring）：
    `account_id`／`user_address` 來自可信來源（引擎取 manifest、API 取 session），
    `record` 完全不可信，`consume_nonce` 是唯一的副作用且擺在最後。

    ⭐ 本函式**只驗真實性，不驗政策邊界**。`allocated_capital > 0` 與
    `capital_utilization ∈ (0,1]` 由 `validate_capital_bounds` 在**每個執行點**
    各自呼叫（API 一次、引擎一次）。理由見該函式 docstring——重點是引擎必須
    自己再驗一次，而不是繼承 API 的判斷。

    檢查順序：動作類型 → 帳號 → 格式 → 時效 → 重建訊息 → recover → 比對簽章者
    → 消耗 nonce。動作類型擺第一是為了讓跨域重放得到**指名事件**的拒絕理由
    （`action_mismatch`），而不是籠統的「格式錯」。
    """
    validate_account_id(account_id)

    # ⭐⭐ 域分隔的顯式檢查（第 2 層）。拿掉這一行，跨域重放仍然會被訊息模板
    # 結構性擋下（第 1 層），但拒絕理由會退化成 malformed／signer_mismatch，
    # 而那兩個碼會被操作者讀成「客戶簽壞了」而忽略。見檔頭域分隔段。
    action = record.get("action")
    if action != ACTION_CAPITAL_SETTINGS:
        raise CapitalSettingsError(
            "action_mismatch",
            f"記錄的動作類型不是 {ACTION_CAPITAL_SETTINGS}（收到 {action!r}）——"
            f"拒絕。一筆換 leader 的授權絕不能被當成一次資金設定授權")

    # ⚠️ normalize_hex_address 出自 spark.filet.followers，**不是** publicapi.config：
    # repo 的依賴方向是單向的 publicapi → filet（signing.py 檔頭），從這裡反向 import
    # 會把它掉頭成環，而引擎（filet）要用這個模組時就會連帶拖進整個 API 層。
    expected_user = normalize_hex_address("user_address", user_address)

    # 記錄自稱的 account_id 必須與可信來源一致（同 leader_change：這不是主要防線，
    # 而是讓「記錄被寫錯槽位」早點以明確的理由失敗）。
    claimed_account = _require_str(record, "account_id")
    if claimed_account != account_id:
        raise CapitalSettingsError(
            "account_mismatch",
            f"記錄的 account_id（{claimed_account!r}）與待驗帳號（{account_id!r}）不符")

    alloc, alloc_str = _canonical_decimal(
        "allocated_capital", record.get("allocated_capital"), CAPITAL_DECIMALS)
    util, util_str = _canonical_decimal(
        "capital_utilization", record.get("capital_utilization"),
        UTILIZATION_DECIMALS)
    # 旗標進得了重建的訊息，所以它與金額一樣受簽章保護（見 build_capital_settings_message）。
    use_full_equity = require_bool_flag(record, "use_full_equity")

    nonce = _require_str(record, "nonce")
    if not _NONCE_RE.fullmatch(nonce):
        raise CapitalSettingsError("malformed", f"nonce 格式不合法: {nonce!r}")
    issued_at = _require_str(record, "issued_at")
    signature = _require_str(record, "signature")

    # 時效：兩側都換算成 epoch 秒再比（同源同單位，工程原則 1）。解析器直接沿用
    # leader_change.parse_issued_at——兩份解析器對「naive 時間算不算數」遲早會有
    # 兩個答案，而那半個答案正好是擋重放的那一半。
    try:
        issued_ts = parse_issued_at(issued_at).timestamp()
    except LeaderChangeError as e:
        # 轉譯成本模組的錯誤型別：呼叫端只該需要處理**一種**失敗型別。
        raise CapitalSettingsError(e.reason, str(e)) from e
    age_s = now_s - issued_ts
    if age_s > CAPITAL_SETTINGS_MAX_AGE_S:
        raise CapitalSettingsError(
            "expired",
            f"簽章已過期（{age_s:.0f}s > {CAPITAL_SETTINGS_MAX_AGE_S}s）——"
            f"請重新取得待簽原文並重簽")
    if age_s < -CAPITAL_SETTINGS_MAX_AGE_S:
        raise CapitalSettingsError(
            "expired",
            f"issued_at 位於未來（{-age_s:.0f}s）——拒絕；"
            f"合法流程的時間戳由伺服器發 nonce 時決定")

    # ⭐ 伺服器權威重建：訊息由**可信的 account_id** 與記錄裡的 canonical 值組出來，
    # `record["message"]` 只是稽核留存，這裡刻意不讀（直接驗「這個簽章有簽這個
    # message」是假驗證——攻擊者同時提供兩者，當然自洽）。
    expected_message = build_capital_settings_message(
        account_id=account_id, allocated_capital=alloc_str,
        capital_utilization=util_str, nonce=nonce, issued_at=issued_at,
        use_full_equity=use_full_equity)
    try:
        signer = recover_personal_sign_address(expected_message, signature)
    except Exception as e:  # noqa: BLE001 —— 壞簽名格式一律轉 semantic 拒絕
        raise CapitalSettingsError("bad_signature",
                                   "簽章無法還原（格式錯誤或損毀）") from e

    # ⭐⭐ 全檔最關鍵的一行（同 leader_change）。拿掉它，攻擊者自簽一筆格式完美的
    # 記錄就能把任何客戶的使用比例拉滿。
    if signer != expected_user:
        raise CapitalSettingsError(
            "signer_mismatch",
            "簽章者不是該帳號的持有人——拒絕。這兩個值直接乘進部位大小，"
            "能改它們的人就能改客戶的曝險倍數")

    # 最後才動唯一的副作用（順序理由見 verify_leader_change：在格式錯或簽章錯時
    # 就燒掉 nonce，等於讓任何人送一筆垃圾記錄就能作廢客戶手上的合法授權）。
    if not consume_nonce(nonce):
        raise CapitalSettingsError(
            "nonce_unusable",
            "nonce 不存在、已用過或已過期——同一份簽章只能兌現一次")

    return VerifiedCapitalSettings(
        account_id=account_id, user_address=expected_user,
        allocated_capital=alloc, capital_utilization=util,
        allocated_capital_str=alloc_str, capital_utilization_str=util_str,
        nonce=nonce, issued_at=issued_at, use_full_equity=use_full_equity)


def build_capital_settings_record(*, account_id: str, allocated_capital: object,
                                  capital_utilization: object, nonce: str,
                                  issued_at: str, signature: str,
                                  message: str,
                                  use_full_equity: bool = False) -> dict:
    """組出落檔用的記錄 dict（欄位順序固定＝CAPITAL_SETTINGS_FIELDS，diff 友善）。

    `action` 由本函式**寫死**成 `ACTION_CAPITAL_SETTINGS`，不從參數收：呼叫端能指定
    動作類型的話，一個手滑就能造出一筆自稱是換 leader 的資金設定記錄。金額一律
    canonical 化後落檔——落進檔案的字串與待簽訊息裡的字串必須是同一個，否則引擎
    重建訊息時會得到另一份原文（工程原則 1）。
    """
    _, cap = _canonical_decimal("allocated_capital", allocated_capital,
                                CAPITAL_DECIMALS)
    _, util = _canonical_decimal("capital_utilization", capital_utilization,
                                 UTILIZATION_DECIMALS)
    # `use_full_equity` 一律以真正的 JSON bool 落檔（讀端 `require_bool_flag`
    # 嚴格比對型別）；順序跟著 CAPITAL_SETTINGS_FIELDS，緊接在金額之後——
    # 讀檔的人看到金額的下一眼就是「這個金額算不算數」。
    return {"action": ACTION_CAPITAL_SETTINGS, "account_id": account_id,
            "allocated_capital": cap, "use_full_equity": bool(use_full_equity),
            "capital_utilization": util,
            "nonce": nonce, "issued_at": issued_at, "signature": signature,
            "message": message}


def capital_settings_path_for(exchange_dir: str | Path) -> str:
    """由**交換目錄**推導出資金設定記錄檔的路徑（**寫端與讀端的單一定義**）。

    錨點與 `leader_changes_path_for` 相同（`FILET_EXCHANGE_DIR`，正式部署
    `/var/lib/filet-exchange`，`filet-api:filet-engine` 0750）：API 寫、引擎讀、
    引擎無寫權。兩邊各自拼一次字串就是兩份會漂移的定義，而漂移的症狀是靜默的
    ——客戶按了調整、API 回 200、記錄確實落地，引擎卻永遠讀不到（C3 本體）。

    ⭐ 與換 leader **分開一個檔**（不是同一個檔多一個欄位）：兩份記錄的讀者是兩個
    獨立的套用器，共用一個檔會讓其中一方的格式問題連坐另一方——而這兩件事各自
    都能造成資金損失，不該共命運。
    """
    return str(Path(exchange_dir) / "capital_settings.json")


def load_capital_settings(path: str | Path) -> list[dict]:
    """讀記錄檔；不存在 → 空清單（尚無人調整資金設定是完全正常的狀態）。

    **刻意不在載入時驗證**（同 load_leader_changes）：驗證需要可信來源的
    user_address，載入層拿不到。把「載入」與「驗證」分開，就不會有人以為
    「載入成功＝已驗過」。
    """
    p = Path(path)
    if not p.exists():
        return []
    return json.loads(p.read_text()).get("settings", [])


def write_capital_settings(path: str | Path, record: dict) -> None:
    """落檔（原子換檔，沿 write_leader_change 的慣例）。

    **同 account_id 覆蓋而非附加**：檔案代表「每位客戶當前的資金設定意圖」，
    不是流水帳。附加會留下一堆舊意圖，而套用端只要挑錯一筆就是把客戶的曝險
    設回他早已改掉的數字。稽核痕跡由套用端的 critical 告警與 log 提供。
    """
    p = Path(path)
    entries = [e for e in load_capital_settings(p)
               if e.get("account_id") != record["account_id"]]
    entries.append(record)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"settings": entries}, indent=2))
    os.replace(tmp, p)  # 原子換檔，不留半寫
