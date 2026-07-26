"""src/spark/filet/leader_change_apply.py
引擎側：**讀取並套用客戶簽章的換 leader 記錄**，外加引擎自己的已兌現 nonce 帳本。

本模組是 leader_change.py 那套威脅模型的**執行端**。API 只把一筆帶客戶簽章的記錄
落在檔案裡（它對引擎的 manifest 沒有寫權，也刻意不該有）；真正決定「客戶的錢跟誰
交易」的那一刻在這裡，所以這裡**自己重新驗章**，不因為「API 已經驗過」而省略。
API 進程被打穿也偽造不出客戶的簽章，它能做的最壞的事是丟掉／延遲一筆合法記錄
（可用性問題），而不是無中生有一次換手（資金問題）。

⭐ 信任來源：user_address 一律取自 **manifest**，絕不取自記錄
--------------------------------------------------------
`verify_leader_change` 的 `user_address` 參數必須來自可信來源。本模組取
`FollowerRef.user_address`（manifest，只有人工 activate CLI 寫得到），**不是**記錄
裡的任何欄位——記錄整份都在攻擊者的寫入範圍內，用它自帶的身分宣稱去驗它自己的
簽章是假驗證（記錄格式因此刻意沒有 signer 欄位，見 leader_change.py 檔頭）。
拿不到 manifest 的 user_address（manifest 缺席／查無此帳號／讀不到）＝**沒有可信
的比對基準** → 一律不套用（fail-closed），不得退而求其次用別的來源湊。

⭐⭐ 為什麼引擎需要**自己的**已兌現 nonce 帳本（本模組的另一半）
--------------------------------------------------------------
API 的一次性 nonce 存在它自己的 SQLite 裡，**引擎讀不到**（兩個進程、兩套權限、
部署上甚至可以不同機）。所以 API 側的「這顆 nonce 已消耗」對引擎完全不可見。
若不自己記帳，同一筆記錄會被**每個 cycle 重複套用**——而「套用」不是改一個設定值，
它會讓引擎收斂到新 leader 的部位（平舊開新，有實際 taker 成本）。更糟的是備份還原：
把三天前的 leader_changes.json 放回去，客戶就被送回他早已離開的 leader。

帳本落在引擎自己的狀態根（`FILET_STATE_DIR`，per-follower 隔離），內容：
- `redeemed`：nonce → 當初兌現時套用的 leader（正規化小寫）。
- `applied`：目前生效中的那一筆（讓 override 在重啟後仍然成立——否則每次重啟
  都會退回 manifest 的舊 leader，而那是一次**沒有客戶授權**的換手）。

同一顆 nonce 再次出現的兩種情形必須分開（工程原則 2 的變形）：
- **leader 相同** → 正常的檔案殘留（記錄檔本來就不會有人清）→ 忽略，**不告警**。
  這是絕大多數 cycle 的路徑，吵一次就等於永遠在吵。
- **leader 不同** → 有人拿一顆已兌現的 nonce 配上另一個 leader → **竄改**，
  拒絕並告警（簽章比對其實也會擋下它，但那會報成籠統的「驗簽失敗」，
  分不出「客戶簽壞了」與「有人在動這個檔」）。

⚠️ 與撤銷路徑的互動：**撤銷優先**
--------------------------------
`enabled=false` 是安全撤銷（這個 leader 出事了），會觸發受控收尾——平掉部位、鎖死
kill switch（見 leaders.py 與 leader_resolve.py）。若同一輪既偵測到撤銷、又有一筆
合法的客戶簽章變更指向另一個 leader，**撤銷優先**：安全動作優先於便利動作，因為
兩者的錯誤代價不對稱（晚一輪換 leader＝客戶多等幾十秒；晚一輪收尾＝繼續跟著一個
已知出事的 leader 下單）。

這個優先序是**結構性**的，不靠任何一行 if：接線是
`applier.effective(base_resolve())`，`base_resolve()`（＝resolve_leader）在參數位置
先被求值，撤銷時它直接 raise `LeaderRevokedError`，本模組的程式碼一行都不會執行。
另一半在 `_current()`：已套用的 override 每輪都要重過 `is_still_permitted`，一旦
那個 leader 被撤銷同樣 raise `LeaderRevokedError` → 收尾。**刻意不是**「退回
manifest 的 leader」——那會變成「你跟的 leader 出事了，所以我們自作主張把你換到
另一個」，一次沒有客戶授權的換手，正是本模組存在的理由所要防的事。

述詞用 `is_still_permitted`（只看 `enabled`），**不是** `is_selectable`：後者是選單
端的述詞（另含 `accepting_new`），用在這裡會讓一個例行下架（名額滿）的 leader 把
正在跟他的人趕走——用一次真實的平倉成本去執行一個純行銷決策。

⭐⭐ 帳本**遺失**（檔案不存在）≠ 首次啟動——兩者必須可區分
--------------------------------------------------------
`applied` 是「客戶已授權的 override」**唯一**的持久化來源（manifest 不會被更新）。
所以帳本一旦消失，`_current()` 會拿 `applied is None` 當成「從未有過變更」而回退到
manifest 的舊 leader——一次**沒有客戶授權的換手**，外加真實的平舊開新成本，而且
全程安靜。這正是本模組宣稱要防的事，卻只對「格式壞」fail-closed，對「檔案不存在」
沒有防線（2026-07-19 opus 對抗性審查 C2，實跑重現：刪帳本 → 下一輪 source=manifest、
零告警）。

**怎麼區分**：引擎在**真正的首次啟動**就無條件落一份空帳本，並在同目錄留下一個
**初始化標記**（`<帳本名>.init`，見 `ledger_init_marker_path`）。此後：

| 帳本 | 標記 | 判定 |
|---|---|---|
| 有 | — | 正常，照讀 |
| 無 | 無 | 真正的首次啟動 → 落初始帳本＋標記，安靜繼續（此時沒有 override 可失去）|
| 無 | **有** | **遺失** → `LeaderResolutionError` ＋ critical，**絕不回退 base** |

落檔順序是「先帳本、後標記」且刻意不可對調：中途斷電時，「有帳本無標記」下一輪會
被判成正常（照讀，正確）；反過來「有標記無帳本」會被判成遺失（假告警）。

⚠️ 已知極限（誠實標註）：**整個狀態根被重建**時標記與帳本一起消失，看起來就是一次
首次啟動——這一格分辨不出來。它不是本機制的漏洞而是它的邊界：狀態根同時裝著
kill switch ARM 檔與 equity 樣本，整根消失是更大的事故，且此時 `initialize_ledger`
會 log 一行「已建立初始帳本」，同一個 account 第二次出現這行就是狀態根被重建的證據。
本機制覆蓋的是真正常見的那格：帳本檔被單獨刪除／輪替／備份還原漏檔。
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from spark.copytrade.notifier import Notifier
from spark.filet.followers import load_followers, normalize_hex_address
from spark.filet.leader_change import (LeaderChangeError, leader_changes_path_for,
                                       load_leader_changes, parse_issued_at,
                                       verify_leader_change)
from spark.filet.leader_resolve import (DEFAULT_EXCHANGE_DIR, SOURCE_CUSTOMER_SIGNED,
                                        LeaderResolution, LeaderResolutionError,
                                        LeaderRevokedError)
from spark.filet.leaders import is_still_permitted, load_leaders
from spark.filet.user_leaders import (load_user_leaders, merge_leaders,
                                      user_leaders_path_for)

logger = logging.getLogger(__name__)

# 帳本落點：掛在引擎狀態根之下（沿 equity_samples.json / killswitch ARM 檔的同一慣例）。
# per-follower 隔離由 FILET_STATE_DIR 提供——兩個 follower 共用一份帳本會讓 A 兌現過的
# nonce 把 B 的合法變更擋掉（症狀是「換了沒反應」，而且完全不告警）。
LEDGER_RELPATH = Path("var/filet/leader_change_ledger.json")

# 記錄檔在 dev 交換目錄下的位置（測試與文件引用；正式部署一律由 env 決定）。
# DEFAULT_EXCHANGE_DIR 由 leader_resolve 提供（所有 var/filet 預設路徑的單一錨點）。
DEFAULT_LEADER_CHANGES_PATH = leader_changes_path_for(DEFAULT_EXCHANGE_DIR)

# 兩個 unit 都必須宣告的交換目錄環境變數（filet-api 寫、filet-follower@ 讀）。
EXCHANGE_DIR_ENV = "FILET_EXCHANGE_DIR"


def require_exchange_dir(env=None) -> Path:
    """引擎啟動時解出交換目錄；**未設或不可讀一律 raise ⇒ 拒絕啟動**。

    ⭐⭐ 這個函式的存在理由是「半邊漏設不得靜默無效」（opus 審查 I2）
    ----------------------------------------------------------------
    交換目錄有兩個使用者、兩個 systemd unit，所以有兩個能被半邊漏設的旋鈕。舊設計
    給了隱含 fallback（未設 → repo 內的 var/filet），於是漏設的症狀是**靜默的**：
    API 寫 `/var/lib/filet-exchange/leader_changes.json`、引擎讀 repo 裡那份，
    客戶按了換 leader、API 回 200 說「下一個 cycle 生效」，而它永遠不會生效——
    兩邊的 log 都完全正常。這正是 C3 實際發生過的事。

    修法是**拿掉 fallback**：兩邊都把這個 env 列為必填（API 側是
    `ApiConfig.from_env` 的 required 清單），漏設的那一邊直接起不來。
    - 引擎漏設 → 本函式 raise → 拒絕啟動（systemd Restart=on-failure ＋
      StartLimitBurst=5 → unit 進 failed，是可監控的狀態）。
    - API 漏設 → `ApiConfig.from_env` raise → 拒絕啟動。
    「起不來」刻意優先於「起來但功能靜默失效」：前者在部署當下就被看見（那一刻有人
    在看），後者可能好幾天沒人發現，而期間客戶每一次換 leader 都石沉大海。這也沿
    leader_resolve 既有的「啟動時解析失敗＝拒絕啟動」慣例。

    「兩邊都設了但**設成不同目錄**」是本函式擋不住的最後一格（兩個進程各自看自己的
    env，沒有共同的仲裁者）。那一格由 RUNBOOK §5.5.1 的驗收指令（實際寫入再實際讀出）
    與 scripts/filet_daily_report.py 的「已寫入但未被套用」對帳告警接住。

    目錄可讀性也在此檢查：路徑打錯、目錄還沒建、group 權限給錯，症狀同樣是靜默
    的「永遠沒有記錄」，同樣必須在啟動時就喊出來，而不是等到第一個客戶按下換 leader。
    """
    env = os.environ if env is None else env
    raw = env.get(EXCHANGE_DIR_ENV)
    if not raw:
        raise ValueError(
            f"缺少環境變數 {EXCHANGE_DIR_ENV}（客戶簽章換 leader 記錄的交換目錄，"
            f"正式部署為 /var/lib/filet-exchange，開發機用 {DEFAULT_EXCHANGE_DIR}）。"
            f"filet-api.service 與 filet-follower@.service **兩個 unit 都必須宣告**"
            f"同一個值——刻意沒有預設值：漏設一邊會讓 API 寫的記錄引擎永遠讀不到，"
            f"而兩邊的 log 都正常。建目錄與權限見 deploy/RUNBOOK.md §5.5.1")
    p = Path(raw)
    if not p.is_dir():
        raise ValueError(
            f"{EXCHANGE_DIR_ENV}={raw} 不是一個存在的目錄——引擎讀不到任何換 leader "
            f"記錄，且症狀是靜默的（客戶按了永遠不生效）。見 deploy/RUNBOOK.md §5.5.1")
    if not os.access(p, os.R_OK | os.X_OK):
        raise ValueError(
            f"{EXCHANGE_DIR_ENV}={raw} 對引擎不可讀——請確認目錄為 "
            f"filet-api:filet-engine 0750 且引擎使用者在 filet-engine 群組內。"
            f"見 deploy/RUNBOOK.md §5.5.1")
    return p


def resolve_changes_path(env=None) -> str:
    """引擎要讀的變更記錄檔路徑（**啟動時呼叫一次**，未設 env ⇒ 拒絕啟動）。

    路徑推導與 API 的寫端共用 `leader_changes_path_for`（單一定義），錨點是
    `FILET_EXCHANGE_DIR` 指的**專屬交換目錄**——不是 API 私有的 pending.json 所在
    目錄，理由見 leader_change.leader_changes_path_for 的 docstring。

    ⚠️ 沒有「取不到就回預設值」這條路：那正是 C3 的成因（見 require_exchange_dir）。
    """
    return leader_changes_path_for(require_exchange_dir(env))


# ⭐ 「已寫入但未被套用」的判定門檻（秒）。一筆記錄落地超過這麼久，引擎的已兌現帳本
# 卻還沒有對應的 nonce ⇒ 這條鏈路壞了。30 分鐘遠大於引擎的 cycle 間隔（數十秒）與
# 任何合理的重啟窗，所以逾時不可能是「還沒輪到」；它是真的沒發生。
LEADER_CHANGE_STALE_S = 1800


@dataclass(frozen=True)
class UnappliedChange:
    """一筆「客戶簽了、API 收了，但引擎從沒套用」的變更（或一個讓人無法確認的障礙）。

    `reason` 是機器可讀碼，呼叫端據此決定怎麼呈現：
    - `not_redeemed`——記錄逾時，帳本裡沒有對應的已兌現 nonce（**本體**）。
    - `bad_issued_at`——記錄的 issued_at 不合法，引擎會拒絕它。
    - `ledger_unreadable`——帳本讀不到，**無法確認**是否已套用（不是「未套用」）。
    """

    account_id: str
    nonce: str | None
    age_s: float | None
    ledger_path: str | None
    reason: str
    detail: str | None = None


def scan_unapplied_leader_changes(refs, now_s: float, *, changes_path=None,
                                  ledger_for, stale_s: float = LEADER_CHANGE_STALE_S
                                  ) -> tuple[list[UnappliedChange], list[str]]:
    """對帳「客戶簽了、API 收了，但引擎從沒套用」的整類失敗（**結構化結果**）。

    ⭐ 為什麼需要這一項（2026-07-19 opus 審查 I2）
    ----------------------------------------------
    換 leader 這條鏈路橫跨兩個進程、兩套權限、兩個目錄。它的可用性失敗是**靜默**的：
    API 驗完章、寫檔、回客戶 200「於引擎的下一個 cycle 生效」，而引擎那邊可能根本
    讀不到那個檔（路徑不通、group 權限錯、引擎沒在跑）。三種原因症狀完全一樣——
    什麼都不會發生，兩邊的 log 都正常，客戶以為換好了，實際上還跟著舊 leader。

    判定用的兩個值刻意同源同單位（工程原則 1）：記錄的年齡由**記錄自己的**
    `issued_at` 換算成 epoch 秒，與傳入的 `now_s` 相減；帳本則用引擎寫入時的同一個
    `load_ledger`。跨進程比對「這顆 nonce 兌現了沒」只看 `redeemed`——那正是引擎
    冪等性的唯一述詞，用別的欄位推斷會與引擎的實際行為漂移。

    ⭐⭐ **本函式是這個判定的單一定義**，有兩個消費端：每日對帳報告
    （`scripts/filet_daily_report.leader_change_warnings`，渲染成人看的字串）與
    營運健康面板（`/api/ops/health`，即時視圖）。兩邊各寫一次判定式，其中一邊遲早
    會與引擎的實際行為漂移——而一個對帳工具最糟的失效是狼來了，因為它會訓練
    操作者忽略它。

    回傳 `(findings, errors)`：`errors` 是「這條鏈路本身查不下去」的障礙
    （交換目錄沒設好、記錄檔讀不到），**不 raise**——呼叫端的其他部分不該被
    這一項的失敗擋住。
    """
    try:
        path = changes_path or resolve_changes_path()
    except ValueError as e:
        # 交換目錄本身沒設好 ⇒ 這條鏈路必定不通。這**就是**要報的事，不是略過的理由。
        return [], [f"換 leader 記錄無法定位（交換目錄設定有問題）：{e}"]
    try:
        records = load_leader_changes(path)
    except (OSError, ValueError, TypeError, AttributeError) as e:
        return [], [f"換 leader 記錄檔讀取失敗（{path}）：{e!r}"
                    f"——無法確認是否有未套用的變更"]

    known = {r.account_id for r in refs}
    findings: list[UnappliedChange] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        account_id, nonce = rec.get("account_id"), rec.get("nonce")
        if not isinstance(account_id, str) or not isinstance(nonce, str):
            continue
        if account_id not in known:
            # manifest 沒有這個帳號＝尚未 activate。引擎本來就不會套用它，不是鏈路故障。
            continue
        try:
            age_s = now_s - parse_issued_at(rec.get("issued_at")).timestamp()
        except (LeaderChangeError, AttributeError):
            findings.append(UnappliedChange(account_id, nonce, None, None,
                                            "bad_issued_at"))
            continue
        if age_s <= stale_s:
            continue        # 還新，引擎可能只是還沒跑到下一輪
        ledger_path = ledger_for(account_id)
        try:
            ledger = load_ledger(ledger_path)
        except (OSError, ValueError) as e:
            findings.append(UnappliedChange(account_id, nonce, age_s,
                                            str(ledger_path), "ledger_unreadable",
                                            detail=repr(e)))
            continue
        if nonce not in ledger.redeemed:
            findings.append(UnappliedChange(account_id, nonce, age_s,
                                            str(ledger_path), "not_redeemed"))
    return findings, []


@dataclass(frozen=True)
class AppliedChange:
    """目前生效中的客戶簽章變更（帳本的 `applied` 區塊）。"""

    nonce: str
    leader_address: str   # 正規化小寫
    issued_at: str
    applied_at: float     # epoch 秒，稽核用


@dataclass(frozen=True)
class Ledger:
    """引擎自己的已兌現帳本。空帳本（首次啟動）＝ `Ledger({}, None)`，不是錯誤。"""

    redeemed: dict[str, str]        # nonce → 兌現時套用的 leader（正規化小寫）
    applied: AppliedChange | None


def ledger_init_marker_path(path: str | Path) -> Path:
    """初始化標記的落點，由帳本路徑推導（**單一定義**，寫端與讀端不會漂移）。

    存在的唯一理由：帳本檔沒辦法當自己「曾經存在過」的證據。少了這個標記，
    「檔案不存在」就同時是首次啟動與帳本遺失兩種語意，而這兩者的正確處置完全相反
    （安靜繼續 vs 拒絕退回 base ＋ 告警）。見模組 docstring 的判定表。
    """
    p = Path(path)
    return p.with_name(p.name + ".init")


def load_ledger(path: str | Path) -> Ledger:
    """讀帳本（**純讀取**）。檔案不存在 → 空帳本。

    ⚠️ 「檔案不存在」在這一層**沒有**語意判定：它究竟是首次啟動還是帳本遺失，
    要靠初始化標記才分得出來，而那個政策住在
    `LeaderChangeApplier._ledger_or_raise`（見模組 docstring 的判定表）。
    引擎路徑一律走 applier，不要繞過它直接用本函式的空帳本回退值當結論。

    **格式壞掉一律 raise（fail-closed），絕不當成空帳本**：把壞檔當空帳本會同時
    造成兩個災難——(1) 已兌現的 nonce 全部忘記，同一筆記錄可以被重放；(2) `applied`
    的 override 消失，引擎悄悄退回 manifest 的舊 leader，那是一次沒有客戶授權的換手。
    呼叫端把它當 transient 處理（沿用上一個已驗證 leader ＋ 大聲告警）。
    """
    p = Path(path)
    if not p.exists():
        return Ledger({}, None)
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"換 leader 帳本不是合法 JSON（{p}）: {e}") from e
    if not isinstance(raw, dict):
        raise ValueError(f"換 leader 帳本頂層須為物件（{p}）")
    redeemed_raw = raw.get("redeemed", {})
    if not isinstance(redeemed_raw, dict):
        raise ValueError(f"換 leader 帳本的 redeemed 須為物件（{p}）")
    redeemed: dict[str, str] = {}
    for nonce, leader in redeemed_raw.items():
        if not isinstance(nonce, str) or not isinstance(leader, str):
            raise ValueError(f"換 leader 帳本的 redeemed 條目格式錯（{p}）")
        redeemed[nonce] = leader
    applied_raw = raw.get("applied")
    applied = None
    if applied_raw is not None:
        if not isinstance(applied_raw, dict):
            raise ValueError(f"換 leader 帳本的 applied 須為物件或 null（{p}）")
        try:
            applied = AppliedChange(
                nonce=str(applied_raw["nonce"]),
                leader_address=normalize_hex_address("applied.leader_address",
                                                     applied_raw["leader_address"]),
                issued_at=str(applied_raw["issued_at"]),
                applied_at=float(applied_raw.get("applied_at", 0.0)))
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"換 leader 帳本的 applied 欄位不完整（{p}）: {e}") from e
    return Ledger(redeemed, applied)


def save_ledger(path: str | Path, ledger: Ledger) -> None:
    """原子落檔（tmp + os.replace，同目錄；tmp 檔名帶 pid，沿 equity.py `_save`）。

    原子性在這裡不是潔癖：帳本寫到一半被 kill／斷電，重啟後讀到的是撕裂檔 →
    `load_ledger` raise → 引擎每輪告警且不再套用任何變更，直到有人手動處理。
    tmp 名帶 pid 的理由同 equity.py：兩個行程誤共用同一個狀態根時，固定 tmp 名會
    互相覆寫，rename 出去的是一份內容交錯的檔案。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {"redeemed": dict(ledger.redeemed), "applied": None}
    if ledger.applied is not None:
        a = ledger.applied
        payload["applied"] = {"nonce": a.nonce, "leader_address": a.leader_address,
                              "issued_at": a.issued_at, "applied_at": a.applied_at}
    tmp = p.parent / f"{p.name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    os.replace(tmp, p)


def initialize_ledger(path: str | Path) -> bool:
    """真正的首次啟動：無條件落一份空帳本 ＋ 初始化標記。回傳是否成功落地。

    **順序（先帳本、後標記）是刻意的**，不可對調：兩步之間斷電時，「有帳本無標記」
    下一輪被判成正常（照讀，正確）；「有標記無帳本」會被判成遺失而發出假告警。
    把不可避免的中斷偏向假陰性（少一次告警）而不是假陽性（狼來了），是因為這個
    告警必須保持稀有才有人看——而真正的遺失下一輪仍會被抓到（標記那時已經在了）。

    寫入失敗 → 回 False ＋ log，**不 raise**：首次啟動的定義就是「還沒有任何客戶
    授權的 override」，此刻沒有東西可以失去，把它升級成解析失敗只會讓一個寫不進去
    的狀態目錄擋住跟單。真正該大聲的是稍後套用時的落帳失敗（那條路徑本來就會
    critical 並拒絕套用）。
    """
    p = Path(path)
    try:
        save_ledger(p, Ledger({}, None))
        marker = ledger_init_marker_path(p)
        tmp = marker.parent / f"{marker.name}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps({"initialized_at": time.time()}, indent=2))
        os.replace(tmp, marker)
    except OSError as e:
        logger.warning("換 leader 初始帳本落檔失敗（%s），本輪以空帳本繼續: %r", p, e)
        return False
    logger.info("換 leader：已建立初始帳本與初始化標記（%s）——此後帳本缺席一律"
                "視為遺失。同一個 account 第二次看到這行代表狀態根被重建了", p)
    return True


class LeaderChangeApplier:
    """每 cycle 把「客戶簽章的換 leader 意圖」疊在基礎解析結果之上。

    用法（見 scripts/run_copytrade.make_effective_leader_resolver）：

        def _resolve() -> LeaderResolution:
            return applier.effective(base_resolve())

    `base_resolve` 先被求值 ⇒ 撤銷（LeaderRevokedError）在本類別跑起來之前就上拋，
    這就是「撤銷優先」的結構性保證（見模組 docstring）。
    """

    def __init__(self, *, account_id: str, manifest_path: str | Path,
                 leaders_path: str | Path, changes_path: str | Path,
                 ledger_path: str | Path, notifier: Notifier, now_fn=time.time):
        self._account_id = account_id
        self._manifest_path = Path(manifest_path)
        self._leaders_path = Path(leaders_path)
        self._changes_path = Path(changes_path)
        self._ledger_path = Path(ledger_path)
        self._notifier = notifier
        self._now_fn = now_fn

    # ---------- 告警 ----------

    def _critical(self, text: str, *, dedup_key: str) -> None:
        """發 critical，且**告警失敗不得中斷跟單**（沿 LeaderWatch._critical）。

        注入的 Notifier 由呼叫端提供，沒有「自己吞例外」的保證。這裡若讓例外逸出，
        「Telegram 掛了」會一路變成「引擎停機、部位無人看管」——觀測層壞掉絕不能
        弄停被觀測的系統（工程原則 3 的反面：大聲，但不要用大聲把系統喊倒）。
        """
        try:
            self._notifier.critical("leader", text, dedup_key=dedup_key)
        except Exception:  # noqa: BLE001 —— 見 docstring：告警失敗只 log
            logger.exception("換 leader 告警發送失敗（跟單不受影響）")

    # ---------- 讀取（全部是「讀不到就 fail-closed」的輔助） ----------

    def _my_record(self) -> dict | None:
        """本帳號的變更記錄；無記錄或讀取失敗皆回 None（兩者都不套用）。

        讀取失敗是 **transient**（IO 打嗝、檔案正在被換、格式壞）→ 只 log 不告警：
        絕大多數 cycle 根本沒有記錄，這條路徑一旦會告警就會變成每輪洗版，真正的
        critical 反而被淹沒（沿既有 transient 慣例）。不套用本身已是安全的一側。
        """
        try:
            records = load_leader_changes(self._changes_path)
            mine = [r for r in records
                    if isinstance(r, dict) and r.get("account_id") == self._account_id]
        except (OSError, ValueError, TypeError, AttributeError) as e:
            logger.warning("換 leader 記錄讀取失敗（%s），沿用現狀: %r",
                           self._changes_path, e)
            return None
        # write_leader_change 是「同 account 覆蓋」，正常情況至多一筆；取最後一筆
        # ＝取最新意圖（手工編輯出多筆時也不會挑到早已作廢的舊意圖）。
        return mine[-1] if mine else None

    def _trusted_user_address(self) -> str | None:
        """manifest 登錄的 user_address＝**唯一**可信的簽章者比對基準。

        拿不到（manifest 缺席／查無此帳號／讀不到／格式壞）→ None ＝ 不套用。
        這是 fail-closed 的關鍵一步：沒有可信基準時「先驗了再說」等於用記錄自己的
        內容驗它自己，那是假驗證（見模組與 leader_change.py 檔頭）。
        """
        try:
            refs = load_followers(self._manifest_path)
        except (OSError, ValueError) as e:
            logger.warning("換 leader：follower manifest 讀取失敗（%s），不套用: %r",
                           self._manifest_path, e)
            return None
        for r in refs:
            if r.account_id == self._account_id:
                return r.user_address
        logger.warning("換 leader：manifest（%s）查無 account_id=%r，不套用",
                       self._manifest_path, self._account_id)
        return None

    def _leaders(self) -> list | None:
        """白名單；**讀不到／檔案不存在 → None（transient，不是「被撤銷」）**。

        誤把「檔案暫時讀不到」當成撤銷，代價是拿真實的平倉成本去回應一次 IO 打嗝
        （沿 leader_resolve 對同一件事的既有分類）。None 的呼叫端一律「不套用／
        沿用現狀」，不升級成收尾。
        """
        # ⭐ 驗證來源＝精選白名單 ＋ user registry 的**合併清單**（2026-07-27 自訂
        # leader 自動准入）：客戶合法准入的自訂 leader 若不在這裡被看見，簽了、
        # 准入了，套用端卻永遠拒絕。合併優先序（精選一律優先，撤銷不可被繞過）
        # 的單一定義在 user_leaders.merge_leaders；registry 路徑由白名單路徑推導
        # （與 leader_resolve／API 共用同一份推導）。
        user_path = Path(user_leaders_path_for(self._leaders_path))
        if not self._leaders_path.exists() and not user_path.exists():
            return None
        try:
            return merge_leaders(load_leaders(self._leaders_path),
                                 load_user_leaders(user_path))
        except (OSError, ValueError) as e:
            logger.warning("換 leader：白名單讀取失敗（%s／%s）: %r",
                           self._leaders_path, user_path, e)
            return None

    # ---------- 已兌現帳本 ----------

    def _already_redeemed(self, ledger: Ledger, nonce: str) -> bool:
        """⭐⭐ 這顆 nonce 是否已被本引擎兌現過。**冪等性只靠這一個述詞**。

        拿掉它（恆回 False），同一筆記錄會每個 cycle 重新套用一次：每輪一則
        「已套用」critical、每輪一次真實的部位收斂（平舊開新）。單一定義是刻意的
        ——早期出口檢查與 `verify_leader_change` 的 `consume_nonce` 各寫一份的話，
        改了一邊沒改另一邊就會得到「半個冪等」。
        """
        return nonce in ledger.redeemed

    # ---------- 帳本載入政策（缺席＝首次啟動 vs 遺失） ----------

    def _ledger_or_raise(self) -> Ledger:
        """讀帳本，並在「檔案不存在」時判定它是首次啟動還是**遺失**。

        判定表與理由見模組 docstring。這裡只重申最關鍵的一句：帳本缺席時回退到
        base（manifest 的 leader）是**錯的**——`applied` 是客戶授權的 override
        唯一的持久化來源，回退等於在客戶沒有授權的情況下把他換回舊 leader，並付出
        一次真實的平舊開新成本。所以遺失一律上拋，由 LeaderWatch 沿用上一個已驗證
        的值（transient 處置）。
        """
        if self._ledger_path.exists():
            return load_ledger(self._ledger_path)
        if not ledger_init_marker_path(self._ledger_path).exists():
            initialize_ledger(self._ledger_path)   # 失敗只 log，見該函式 docstring
            return Ledger({}, None)
        # 標記在、帳本不在 ⇒ 這個引擎確實建過帳本，現在它不見了。
        # ⚠️ 告警文字**指名真正的事件**，刻意不複用 verify_failed 之類的既有 reason：
        # 「某筆記錄驗簽失敗」會被操作者讀成「一筆過期記錄被忽略」（無害），而實際
        # 發生的是「客戶已授權的 override 消失，差一點就無授權換手」（要立刻處理）。
        self._critical(
            f"**換 leader 帳本遺失**（{self._ledger_path} 曾經存在，現在不見了）——"
            f"**已授權 override 遺失，拒絕退回 manifest 的 leader**。回退會是一次"
            f"沒有客戶授權的換手，外加真實的平舊開新成本。本輪不解析，引擎沿用上一個"
            f"已驗證的 leader；請從備份還原帳本，或確認狀態目錄未被重建",
            dedup_key="leader_change_ledger_lost")
        raise LeaderResolutionError(
            f"換 leader 帳本遺失（{self._ledger_path}）——已授權 override 遺失，"
            f"拒絕退回 manifest 的 leader")

    # ---------- 現狀（＝已套用的 override，或基礎解析結果） ----------

    def _current(self, base: LeaderResolution,
                 applied: AppliedChange | None) -> LeaderResolution:
        """本輪該用的 leader，在「沒有新變更可套用」的所有路徑上共用。

        `applied` 為 None → 原樣回傳 base（從未有客戶簽章變更）。
        有 override → **每輪重過白名單**（`is_still_permitted`）：
        - 通過 → 沿用 override（重啟後也成立，這正是 `applied` 要落檔的理由）。
        - 白名單讀不到 → transient，沿用 override（現狀），只 log。
        - 明確拒絕 → raise `LeaderRevokedError` ⇒ 受控收尾。**不退回 base**：
          客戶授權跟的 leader 出事了，自作主張把他換到另一個人，本身就是一次
          沒有授權的換手（見模組 docstring 的撤銷段）。
        """
        if applied is None:
            return base
        leaders = self._leaders()
        if leaders is None:
            logger.warning("換 leader：白名單暫時不可用，沿用已套用的 leader %s",
                           applied.leader_address)
            return LeaderResolution(applied.leader_address, SOURCE_CUSTOMER_SIGNED)
        if not is_still_permitted(applied.leader_address, leaders):
            raise LeaderRevokedError(
                f"客戶簽章授權的 leader {applied.leader_address} 已被白名單撤銷"
                f"（enabled=false／條目已移除）——拒絕跟單。**不退回 manifest 的舊"
                f"leader**：那會是一次沒有客戶授權的換手；要換人請由客戶重新簽署")
        return LeaderResolution(applied.leader_address, SOURCE_CUSTOMER_SIGNED)

    # ---------- 主流程 ----------

    def effective(self, base: LeaderResolution) -> LeaderResolution:
        """回傳本輪真正該跟的 leader。

        raise 的情形只有兩種，且都由 `LeaderWatch` 接手處理：
        - `LeaderResolutionError`（帳本讀不到**或遺失**）→ transient：沿用上一輪
          ＋ critical。遺失那一格另有本模組自己發的、指名事件的 critical。
        - `LeaderRevokedError`（已套用的 leader 被撤銷）→ 受控收尾。

        其餘所有失敗（驗簽不過、leader 不被允許、記錄讀不到、落帳失敗）都是
        **不套用 ＋ 沿用現狀**，絕不中斷跟單——已開的部位需要有人繼續管理。
        """
        try:
            ledger = self._ledger_or_raise()
        except LeaderResolutionError:
            # 帳本**遺失**（_ledger_or_raise 已告警且已指名事件）——原樣上拋，
            # 不要再包一層把「遺失」模糊成籠統的「無法載入」。
            raise
        except (OSError, ValueError) as e:
            # 帳本讀不到 ⇒ 既不知道哪些 nonce 兌現過，也不知道目前生效的 override
            # 是誰。此時「回退 base」是錯的（可能是一次無授權換手），只能宣告
            # 本輪解析沒有結論，交給 LeaderWatch 沿用上一個已驗證值並大聲告警。
            raise LeaderResolutionError(
                f"換 leader 帳本無法載入（{self._ledger_path}）: {e}") from e

        rec = self._my_record()
        if rec is None:
            # ⭐ 最常見的路徑（絕大多數 cycle 都沒有待套用的變更）：完全安靜。
            return self._current(base, ledger.applied)

        nonce = rec.get("nonce")
        if isinstance(nonce, str) and self._already_redeemed(ledger, nonce):
            if self._record_leader(rec) == ledger.redeemed[nonce]:
                # 正常的檔案殘留（記錄檔沒有人負責清）→ 忽略且**不告警**。
                return self._current(base, ledger.applied)
            self._critical(
                f"**換 leader 記錄疑遭竄改**：nonce `{nonce}` 已於先前兌現為 "
                f"{ledger.redeemed[nonce]}，現在同一顆 nonce 卻配上另一個 leader —— "
                f"**拒絕套用**，繼續跟目前的 leader。請檢查記錄檔 "
                f"{self._changes_path} 的寫入權限與內容",
                dedup_key=f"leader_change_nonce_reuse:{nonce}")
            return self._current(base, ledger.applied)

        user_address = self._trusted_user_address()
        if user_address is None:
            return self._current(base, ledger.applied)   # 已在 _trusted_user_address log

        try:
            verified = verify_leader_change(
                rec, account_id=self._account_id, user_address=user_address,
                now_s=float(self._now_fn()),
                consume_nonce=lambda n: not self._already_redeemed(ledger, n))
        except LeaderChangeError as e:
            # ⭐ semantic：重試同一筆記錄必定再次失敗 → 不重試、不套用、大聲。
            # ⚠️ 告警與 log **只帶 reason 這個機器可讀碼**，不帶 str(e)、更不帶
            # 記錄內容——簽章與任何密鑰材料不得進 log／告警／例外訊息（紅線 3）。
            # 做成「只允許 reason 通過」而不是「記得別把 signature 塞進去」，
            # 是為了讓它結構性成立而不是靠記性（工程原則 5 的精神）。
            logger.error("換 leader 驗簽失敗 account=%s reason=%s",
                         self._account_id, e.reason)
            self._critical(
                f"**換 leader 記錄驗簽失敗**（reason=`{e.reason}`）——**不套用**，"
                f"繼續跟目前的 leader。這是 semantic 失敗，重試同一筆記錄必定再次"
                f"失敗；若客戶確實要換 leader，請他重新取得待簽原文並重簽",
                dedup_key=f"leader_change_verify_failed:{e.reason}")
            return self._current(base, ledger.applied)

        leaders = self._leaders()
        if leaders is None:
            logger.warning("換 leader：白名單暫時不可用，本輪不套用 %s",
                           verified.leader_address)
            return self._current(base, ledger.applied)
        # ⚠️ 引擎的述詞是 is_still_permitted（只看 enabled），不是 is_selectable。
        if not is_still_permitted(verified.leader_address, leaders):
            self._critical(
                f"**客戶簽章要求換到 {verified.leader_address}，但該 leader 不在白名單"
                f"或已被撤銷** —— **不套用**，繼續跟目前的 leader。簽章本身是有效的；"
                f"擋下它的是白名單（能寫記錄的人與能寫白名單的人是兩個信任域）",
                dedup_key=f"leader_change_not_permitted:{verified.leader_address}")
            return self._current(base, ledger.applied)

        old = ledger.applied.leader_address if ledger.applied else base.address
        try:
            save_ledger(self._ledger_path, Ledger(
                redeemed={**ledger.redeemed, verified.nonce: verified.leader_address},
                applied=AppliedChange(nonce=verified.nonce,
                                      leader_address=verified.leader_address,
                                      issued_at=verified.issued_at,
                                      applied_at=float(self._now_fn()))))
        except OSError as e:
            # ⭐ 落帳失敗 ⇒ **不套用**。順序是刻意的：先落帳再套用。反過來的話，
            # 這一輪換了 leader（真實的平倉開倉）卻沒記下 nonce，下一輪會再套用
            # 一次、再收斂一次——一個寫不進去的狀態目錄會變成無限次的交易成本。
            logger.error("換 leader 帳本落檔失敗（%s）: %r", self._ledger_path, e)
            self._critical(
                f"**換 leader 帳本落檔失敗**（{self._ledger_path}）——**不套用**該筆"
                f"變更，繼續跟目前的 leader。先落帳再套用是刻意的：套了卻沒記下 "
                f"nonce，下一輪會重複套用並重複付出收斂成本。請檢查狀態目錄的寫入權限",
                dedup_key="leader_change_ledger_write_failed")
            return self._current(base, ledger.applied)

        self._critical(
            f"**已套用客戶簽章授權的換 leader**：{old} → {verified.leader_address}"
            f"（來源：**客戶簽章授權**，account={self._account_id}）——引擎將收斂到"
            f"新 leader 的部位（平掉舊部位、開新部位，有實際 taker 成本）。"
            f"本次變更已由引擎獨立重新驗章並通過白名單",
            dedup_key=f"leader_change_applied:{verified.nonce}")
        logger.info("換 leader 已套用 account=%s %s → %s", self._account_id, old,
                    verified.leader_address)
        return LeaderResolution(verified.leader_address, SOURCE_CUSTOMER_SIGNED)

    # ---------- 小工具 ----------

    @staticmethod
    def _record_leader(rec: dict) -> str | None:
        """記錄自稱的 leader，正規化小寫；不合法回 None（→ 一律當成不相符）。"""
        try:
            return normalize_hex_address("leader_address",
                                         rec.get("leader_address", ""))
        except (ValueError, TypeError):
            return None
