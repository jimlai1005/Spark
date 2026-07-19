"""src/spark/filet/capital_settings_apply.py
引擎側：**讀取並套用客戶簽章的資金設定**，外加引擎自己的已兌現帳本。

本模組與 leader_change_apply.py 是同一個形狀的兩個實例（先讀那份檔頭，這裡只寫
**不同**的部分）。共同的骨架：
- 引擎**自己重新驗章**，不因為「API 已經驗過」而省略。
- `user_address` 一律取自 **manifest**，絕不取自記錄（記錄整份都在攻擊者的寫入
  範圍內，用它自帶的身分宣稱去驗它自己的簽章是假驗證）。
- 引擎有**自己的**已兌現 nonce 帳本（API 的 nonce 存在它的 SQLite 裡，引擎讀不到）。
- 帳本**遺失**（檔案曾存在、現在不見了）→ fail-closed，**絕不靜默退回預設值**。

⭐⭐ 為什麼引擎要**再驗一次邊界**（本模組與換 leader 最大的不同）
--------------------------------------------------------------
換 leader 的引擎端二次檢查是「這個 leader 還在白名單嗎」；資金設定的對應物是
**數值邊界**（`capital_utilization ∈ (0,1]`，以及本金依 `use_full_equity` 模式
二選一：固定本金必須 > 0、全部權益必須恰為 0——見 capital_settings.py 檔頭）。

為什麼不能只信 API 驗過：這兩個值**直接乘進部位大小**
（sizing.compute_scale_factor:88-91 `cap * capital_utilization * weight / leader_equity`）。
一個壞值就是一次超額曝險——`capital_utilization = 10` 會讓部位變成十倍，而這件事
在下單那一刻看起來完全正常（每一筆單都合法、每一個 log 都乾淨）。API 是一個可能
被打穿、可能有 bug、可能被繞過（直接寫交換目錄的檔）的進程；把「這個數字安不安全」
的唯一判斷放在它身上，等於讓引擎的曝險上限由一個它管不到的進程決定。

所以邊界檢查刻意**不在** `verify_capital_settings` 裡（見 capital_settings.py 的
`validate_capital_bounds` docstring）：驗簽回答「是不是客戶本人的意圖」，邊界回答
「這個意圖可不可以執行」。兩者分開，引擎端的再驗才是一道**獨立**的閘，而不是
一句「反正 verify 驗過了」。

超界的處置是**拒絕 ＋ critical ＋ 沿用現狀**，絕不夾取（clamp）。夾取是最誘人的
錯誤選項——它讓流程順利跑完，代價是引擎執行了一個客戶沒有簽署的數字。

⭐ 「下一 cycle 生效」＝**不做即時強制再平衡**
--------------------------------------------
套用一筆新的資金設定**不觸發任何交易動作**。本模組只把 `CopySettings` 換掉，
下一輪 `run_cycle` 的 sizing 自然用新的比例算出新的目標部位，而收斂由既有的
reconcile 路徑在 leader 下次動作時自然完成。

刻意不主動平掉差額：客戶把使用比例從 1.0 調到 0.9，強制再平衡會立刻產生一筆
10% 的 taker 賣單——一個純粹的設定調整換來一次真實的交易成本。與換 leader 相反
（那裡的收斂是**必要的**，因為要跟的部位整組換人了），這裡新舊目標高度重疊，
等它自然收斂幾乎沒有代價。這個語意在 API 回應裡對客戶明講（consequences 欄位）。

⚠️ 已知極限（誠實標註）：使用比例大幅調降時，部位會維持在舊水位直到 leader 下次
動作。若 leader 長期不動，實際曝險會比客戶期望的高，且時間不確定。這是「不付出
無謂 taker 成本」的代價，不是 bug——但客戶必須被告知（API 回應已寫明）。真要
立即降曝險，客戶手上有 panic 路徑（scripts/panic.py），那是一個顯式的決定。

⭐ 帳本：**與換 leader 分開一個檔**（決策記錄）
---------------------------------------------
可以共用同一份帳本檔（只要 nonce 能區分動作類型），但這裡選擇分開，理由三條：
1. **失效不連坐**。`load_ledger` 系列對格式壞掉一律 fail-closed（raise）——這是對的，
   但共用一個檔意味著資金設定的一個寫入 bug 會讓**換 leader 的帳本**也讀不出來，
   於是引擎連 leader 都解析不了。兩個各自都能造成資金損失的機制不該共命運。
2. **遺失告警要能指名事件**。帳本遺失的 critical 必須說清楚「遺失的是哪一種已授權
   override」（操作者的處置完全不同）。共用一個檔，這則告警只能說「有東西不見了」。
3. **nonce 命名空間天然分離**。分開檔案之後，「A 動作兌現過的 nonce 擋掉 B 動作的
   合法記錄」結構上不可能發生，不需要額外的 key 前綴紀律（少一件要記得的事）。
   跨動作的重放本來就由簽章擋下（訊息模板綁死動作類型），帳本分離不是那道防線，
   而是避免**誤擋**。
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

from spark.copytrade.config import CopySettings
from spark.copytrade.notifier import Notifier
from spark.filet.capital_settings import (CapitalSettingsError,
                                          capital_settings_path_for,
                                          canonical_capital_values,
                                          load_capital_settings,
                                          require_bool_flag,
                                          validate_capital_bounds,
                                          verify_capital_settings)
from spark.filet.followers import load_followers
from spark.filet.leader_change_apply import require_exchange_dir
from spark.filet.leader_resolve import DEFAULT_EXCHANGE_DIR

logger = logging.getLogger(__name__)

# 帳本落點：掛在引擎狀態根之下（沿 leader_change_ledger 的同一慣例）。
# per-follower 隔離由 FILET_STATE_DIR 提供——兩個 follower 共用一份帳本會讓 A 兌現過
# 的 nonce 把 B 的合法設定擋掉（症狀是「調了沒反應」，而且完全不告警）。
LEDGER_RELPATH = Path("var/filet/capital_settings_ledger.json")

# 記錄檔在 dev 交換目錄下的位置（測試與文件引用；正式部署一律由 env 決定）。
DEFAULT_CAPITAL_SETTINGS_PATH = capital_settings_path_for(DEFAULT_EXCHANGE_DIR)


class CapitalSettingsUnavailable(RuntimeError):
    """帳本讀不到**或遺失** ⇒ 本輪無法判定該用哪組資金設定。

    ⭐ 這**不是**「用預設值繼續」的同義詞——正好相反。`applied` 是客戶已授權的
    override 唯一的持久化來源（`CopySettings` 的 env 值不會被更新），所以帳本不見時
    退回 env 預設等於在客戶沒有授權的情況下改變他的曝險，而且全程安靜。呼叫端必須
    把本例外處理成「本輪不做任何交易動作」，不得吞掉後照舊跑。
    """


def resolve_capital_settings_path(env=None) -> str:
    """引擎要讀的資金設定記錄檔路徑（**啟動時呼叫一次**，未設 env ⇒ 拒絕啟動）。

    錨點與換 leader 記錄共用 `require_exchange_dir`（同一個 `FILET_EXCHANGE_DIR`、
    同一段「漏設就拒絕啟動」的理由，見 leader_change_apply.require_exchange_dir）。
    ⚠️ 沒有「取不到就回預設值」這條路：那正是 C3 的成因。
    """
    return capital_settings_path_for(require_exchange_dir(env))


@dataclass(frozen=True)
class AppliedCapital:
    """目前生效中的客戶簽章資金設定（帳本的 `applied` 區塊）。

    數值以 **canonical 字串**存放（不是 float、也不是 Decimal 的 repr）：帳本會被
    人打開來看，也會被 json round-trip，字串是唯一在這兩件事之後仍然逐位元組相同
    的表示法。讀回來時再轉 Decimal（`as_decimals`）。
    """

    nonce: str
    allocated_capital: str
    capital_utilization: str
    issued_at: str
    applied_at: float     # epoch 秒，稽核用
    # ⭐ 本金模式（見 capital_settings.py 檔頭）。舊帳本沒有這個鍵 → False，
    # 那是舊記錄的正確語意（本旗標問世前一律是固定本金且 > 0），所以既有帳本
    # 不因新欄位而失效，也不會被誤讀成「全部權益」。
    use_full_equity: bool = False

    def as_decimals(self) -> tuple[Decimal, Decimal]:
        return Decimal(self.allocated_capital), Decimal(self.capital_utilization)

    @property
    def fingerprint(self) -> str:
        """「這顆 nonce 當初兌現成什麼」的單一表示（帳本 `redeemed` 的值）。

        存指紋而不是只存 nonce：同一顆 nonce 配上**不同數值**再次出現＝竄改，
        必須與「正常的檔案殘留」分開處置（沿 leader_change_apply 對 nonce 重用的
        同一個判別）。

        ⭐ 指紋**必須含 `use_full_equity`**：它與金額一樣決定曝險基準，漏掉它會讓
        「同一顆 nonce、同樣的 0 元、旗標從 False 翻成 True」被判成正常的檔案殘留
        而靜默忽略——竄改偵測在最關鍵的那個欄位上失明。
        """
        return (f"{self.allocated_capital}|{self.capital_utilization}"
                f"|{'full' if self.use_full_equity else 'fixed'}")


@dataclass(frozen=True)
class CapitalLedger:
    """引擎自己的已兌現帳本。空帳本（首次啟動）＝ `CapitalLedger({}, None)`。"""

    redeemed: dict[str, str]           # nonce → 兌現時套用的指紋
    applied: AppliedCapital | None


def ledger_init_marker_path(path: str | Path) -> Path:
    """初始化標記的落點，由帳本路徑推導（**單一定義**，寫端與讀端不會漂移）。

    存在的唯一理由與換 leader 帳本相同：帳本檔沒辦法當自己「曾經存在過」的證據。
    少了這個標記，「檔案不存在」就同時是首次啟動與帳本遺失兩種語意，而這兩者的
    正確處置完全相反（安靜繼續 vs 拒絕退回預設 ＋ 告警）。
    """
    p = Path(path)
    return p.with_name(p.name + ".init")


def load_ledger(path: str | Path) -> CapitalLedger:
    """讀帳本（**純讀取**）。檔案不存在 → 空帳本。

    ⚠️ 「檔案不存在」在這一層**沒有**語意判定（首次啟動 vs 遺失要靠初始化標記）；
    那個政策住在 `CapitalSettingsApplier._ledger_or_raise`。

    **格式壞掉一律 raise（fail-closed），絕不當成空帳本**：把壞檔當空帳本會同時
    造成兩個災難——(1) 已兌現的 nonce 全部忘記，同一筆記錄可以被重放；(2) `applied`
    的 override 消失，引擎悄悄退回 env 的預設曝險，那是一次沒有客戶授權的曝險變更。
    """
    p = Path(path)
    if not p.exists():
        return CapitalLedger({}, None)
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"資金設定帳本不是合法 JSON（{p}）: {e}") from e
    if not isinstance(raw, dict):
        raise ValueError(f"資金設定帳本頂層須為物件（{p}）")
    redeemed_raw = raw.get("redeemed", {})
    if not isinstance(redeemed_raw, dict):
        raise ValueError(f"資金設定帳本的 redeemed 須為物件（{p}）")
    redeemed: dict[str, str] = {}
    for nonce, fp in redeemed_raw.items():
        if not isinstance(nonce, str) or not isinstance(fp, str):
            raise ValueError(f"資金設定帳本的 redeemed 條目格式錯（{p}）")
        redeemed[nonce] = fp
    applied_raw = raw.get("applied")
    applied = None
    if applied_raw is not None:
        if not isinstance(applied_raw, dict):
            raise ValueError(f"資金設定帳本的 applied 須為物件或 null（{p}）")
        try:
            flag = applied_raw.get("use_full_equity", False)
            if not isinstance(flag, bool):
                # 嚴格型別（同記錄層的 require_bool_flag）：這個旗標決定本金基準，
                # 寬鬆解析等於替它多開幾個等價寫法，而帳本是每輪都會被讀的。
                raise ValueError("use_full_equity 必須是布林值")
            applied = AppliedCapital(
                nonce=str(applied_raw["nonce"]),
                allocated_capital=str(applied_raw["allocated_capital"]),
                capital_utilization=str(applied_raw["capital_utilization"]),
                issued_at=str(applied_raw["issued_at"]),
                applied_at=float(applied_raw.get("applied_at", 0.0)),
                use_full_equity=flag)
            # ⭐ 落檔的數值必須仍然解得出 Decimal。壞掉的話 fail-closed 比「套用一個
            # 解不出來的值」安全得多——後者會在 sizing 那裡以一個完全無關的例外炸掉。
            applied.as_decimals()
        except (KeyError, TypeError, ValueError, ArithmeticError) as e:
            raise ValueError(f"資金設定帳本的 applied 欄位不完整（{p}）: {e}") from e
    return CapitalLedger(redeemed, applied)


def save_ledger(path: str | Path, ledger: CapitalLedger) -> None:
    """原子落檔（tmp + os.replace，同目錄；tmp 檔名帶 pid，沿 equity.py `_save`）。

    原子性在這裡不是潔癖：帳本寫到一半被 kill／斷電，重啟後讀到的是撕裂檔 →
    `load_ledger` raise → 引擎每輪告警且不再套用任何設定，直到有人處理。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {"redeemed": dict(ledger.redeemed), "applied": None}
    if ledger.applied is not None:
        a = ledger.applied
        payload["applied"] = {"nonce": a.nonce,
                              "allocated_capital": a.allocated_capital,
                              "use_full_equity": bool(a.use_full_equity),
                              "capital_utilization": a.capital_utilization,
                              "issued_at": a.issued_at, "applied_at": a.applied_at}
    tmp = p.parent / f"{p.name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    os.replace(tmp, p)


def initialize_ledger(path: str | Path) -> bool:
    """真正的首次啟動：無條件落一份空帳本 ＋ 初始化標記。回傳是否成功落地。

    **順序（先帳本、後標記）是刻意的**，不可對調：兩步之間斷電時，「有帳本無標記」
    下一輪被判成正常（照讀，正確）；「有標記無帳本」會被判成遺失而發出假告警。
    把不可避免的中斷偏向假陰性（少一次告警）而不是假陽性（狼來了），是因為這個
    告警必須保持稀有才有人看——而真正的遺失下一輪仍會被抓到。
    """
    p = Path(path)
    try:
        save_ledger(p, CapitalLedger({}, None))
        marker = ledger_init_marker_path(p)
        tmp = marker.parent / f"{marker.name}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps({"initialized_at": time.time()}, indent=2))
        os.replace(tmp, marker)
    except OSError as e:
        logger.warning("資金設定初始帳本落檔失敗（%s），本輪以空帳本繼續: %r", p, e)
        return False
    logger.info("資金設定：已建立初始帳本與初始化標記（%s）——此後帳本缺席一律"
                "視為遺失。同一個 account 第二次看到這行代表狀態根被重建了", p)
    return True


class CapitalSettingsApplier:
    """每 cycle 把「客戶簽章的資金設定」疊在基礎 `CopySettings` 之上。

    用法（見 scripts/run_copytrade.make_capital_settings_applier）：

        cs = applier.effective(base_settings)     # 可能 raise CapitalSettingsUnavailable

    形狀刻意與 `LeaderChangeApplier` 一致（同樣的 `effective(base) -> base'`），
    差別只在 base 的型別（CopySettings 而非 LeaderResolution）。
    """

    def __init__(self, *, account_id: str, manifest_path: str | Path,
                 settings_path: str | Path, ledger_path: str | Path,
                 notifier: Notifier, now_fn=time.time):
        self._account_id = account_id
        self._manifest_path = Path(manifest_path)
        self._settings_path = Path(settings_path)
        self._ledger_path = Path(ledger_path)
        self._notifier = notifier
        self._now_fn = now_fn
        # ⭐ 上一次 `effective()` 實際採用的客戶簽章設定（見 `last_applied`）。
        self._last_applied: AppliedCapital | None = None

    @property
    def last_applied(self) -> AppliedCapital | None:
        """上一次 `effective()` 實際採用的客戶簽章設定；**None ＝ 沿用環境預設**。

        ⭐⭐ 為什麼是一個由 `effective()` 順帶記下的值，而不是「需要時再讀一次帳本」
        （工程原則 1）：健康心跳要回報「目前生效的資金設定**與它的來源**」。若心跳
        自己去 `load_ledger` 讀一次，那是**第二次讀取**——兩次讀取之間帳本可能已經
        變了，於是心跳會宣稱一組本輪根本沒有被用來下單的數值，而且看起來完全正常。
        來源與數值都必須出自產生本輪 `CopySettings` 的那一次求值。

        寫入點只有 `_current`（所有「本輪用哪組設定」的路徑都收斂在那裡），所以這個
        值不可能與 `effective()` 的回傳值不一致。`_current` 提前 raise 的那一格會先
        清成 None——本輪沒有任何設定生效，回報「沿用環境預設」同樣是錯的，而呼叫端
        在那一格本來就會回報成錯誤（見 run_copytrade.cycle）。
        """
        return self._last_applied

    # ---------- 告警 ----------

    def _critical(self, text: str, *, dedup_key: str) -> None:
        """發 critical，且**告警失敗不得中斷跟單**（沿 LeaderChangeApplier._critical）。

        注入的 Notifier 沒有「自己吞例外」的保證。這裡若讓例外逸出，「Telegram 掛了」
        會一路變成「引擎停機、部位無人看管」——觀測層壞掉絕不能弄停被觀測的系統。
        """
        try:
            self._notifier.critical("capital", text, dedup_key=dedup_key)
        except Exception:  # noqa: BLE001 —— 見 docstring：告警失敗只 log
            logger.exception("資金設定告警發送失敗（跟單不受影響）")

    # ---------- 讀取 ----------

    def _my_record(self) -> dict | None:
        """本帳號的資金設定記錄；無記錄或讀取失敗皆回 None（兩者都不套用）。

        讀取失敗是 **transient**（IO 打嗝、檔案正在被換、格式壞）→ 只 log 不告警：
        絕大多數 cycle 根本沒有新記錄，這條路徑一旦會告警就會變成每輪洗版，真正的
        critical 反而被淹沒（沿既有 transient 慣例）。不套用本身已是安全的一側。
        """
        try:
            records = load_capital_settings(self._settings_path)
            mine = [r for r in records
                    if isinstance(r, dict) and r.get("account_id") == self._account_id]
        except (OSError, ValueError, TypeError, AttributeError) as e:
            logger.warning("資金設定記錄讀取失敗（%s），沿用現狀: %r",
                           self._settings_path, e)
            return None
        # write_capital_settings 是「同 account 覆蓋」，正常情況至多一筆；取最後一筆
        # ＝取最新意圖（手工編輯出多筆時也不會挑到早已作廢的舊意圖）。
        return mine[-1] if mine else None

    def _trusted_user_address(self) -> str | None:
        """manifest 登錄的 user_address＝**唯一**可信的簽章者比對基準。

        拿不到（manifest 缺席／查無此帳號／讀不到／格式壞）→ None ＝ 不套用。
        沒有可信基準時「先驗了再說」等於用記錄自己的內容驗它自己，那是假驗證。
        """
        try:
            refs = load_followers(self._manifest_path)
        except (OSError, ValueError) as e:
            logger.warning("資金設定：follower manifest 讀取失敗（%s），不套用: %r",
                           self._manifest_path, e)
            return None
        for r in refs:
            if r.account_id == self._account_id:
                return r.user_address
        logger.warning("資金設定：manifest（%s）查無 account_id=%r，不套用",
                       self._manifest_path, self._account_id)
        return None

    # ---------- 已兌現帳本 ----------

    def _already_redeemed(self, ledger: CapitalLedger, nonce: str) -> bool:
        """⭐⭐ 這顆 nonce 是否已被本引擎兌現過。**冪等性只靠這一個述詞**。

        拿掉它（恆回 False），同一筆記錄會每個 cycle 重新套用一次：每輪一則
        「已套用」critical，把真正的事件淹掉。單一定義是刻意的——早期出口檢查與
        `verify_capital_settings` 的 `consume_nonce` 各寫一份的話，改了一邊沒改
        另一邊就會得到「半個冪等」。
        """
        return nonce in ledger.redeemed

    def _ledger_or_raise(self) -> CapitalLedger:
        """讀帳本，並在「檔案不存在」時判定它是首次啟動還是**遺失**。

        判定表（沿 leader_change_apply 的同一張表）：

        | 帳本 | 標記 | 判定 |
        |---|---|---|
        | 有 | — | 正常，照讀 |
        | 無 | 無 | 真正的首次啟動 → 落初始帳本＋標記，安靜繼續 |
        | 無 | **有** | **遺失** → raise ＋ critical，**絕不退回 env 預設值** |

        最關鍵的一句：帳本缺席時回退到 base（`CopySettings` 的 env 值）是**錯的**
        ——`applied` 是客戶已授權 override 唯一的持久化來源，回退等於在客戶沒有
        授權的情況下改變他的曝險。
        """
        if self._ledger_path.exists():
            return load_ledger(self._ledger_path)
        if not ledger_init_marker_path(self._ledger_path).exists():
            initialize_ledger(self._ledger_path)   # 失敗只 log，見該函式 docstring
            return CapitalLedger({}, None)
        # 標記在、帳本不在 ⇒ 這個引擎確實建過帳本，現在它不見了。
        self._critical(
            f"**資金設定帳本遺失**（{self._ledger_path} 曾經存在，現在不見了）——"
            f"**已授權的資金設定遺失，拒絕退回環境預設值**。退回會是一次沒有客戶"
            f"授權的曝險變更（部位大小直接由這兩個值決定）。本輪不進行任何交易動作；"
            f"請從備份還原帳本，或確認狀態目錄未被重建",
            dedup_key="capital_settings_ledger_lost")
        raise CapitalSettingsUnavailable(
            f"資金設定帳本遺失（{self._ledger_path}）——已授權設定遺失，"
            f"拒絕退回環境預設值")

    # ---------- 現狀 ----------

    def _current(self, base: CopySettings,
                 applied: AppliedCapital | None) -> CopySettings:
        """本輪該用的設定，在「沒有新記錄可套用」的所有路徑上共用。

        `applied` 為 None → 原樣回傳 base（從未有客戶簽章的資金設定）。
        有 override → 套上去，且**每輪重驗邊界**：帳本裡的值也可能被竄改
        （狀態目錄的寫入權限出問題、備份還原了一份壞檔），而它每一輪都會被乘進
        部位大小。「寫進帳本時驗過了」不足以保證「現在讀出來的還是那個值」。

        ⭐ 本函式是「本輪用哪組設定」的**唯一**收斂點，所以也是 `last_applied` 的
        唯一寫入點——心跳因此拿得到與本輪 `CopySettings` 同一次求值的來源標記。
        先清成 None 再視情況設回，是為了讓任何提前 raise 的路徑不留下上一輪的值。
        """
        self._last_applied = None
        if applied is None:
            return base
        alloc, util = applied.as_decimals()
        try:
            # ⭐ 邊界依**帳本自己記下的模式**驗（同源：模式與數值出自同一次兌現）。
            # 拿另一個來源的旗標去驗這組數值就是混源比較（工程原則 1）。
            validate_capital_bounds(alloc, util,
                                    use_full_equity=applied.use_full_equity)
        except CapitalSettingsError:
            # ⭐ 帳本裡的值超界 ⇒ 帳本被動過。**不套用、也不退回 base**——退回 base
            # 同樣是一次無授權的曝險變更。本輪宣告無法判定，交由呼叫端停止交易動作。
            self._critical(
                f"**資金設定帳本內的數值超出允許範圍**（帳本疑遭竄改）——"
                f"**拒絕套用，且拒絕退回環境預設值**：兩者都會是一次沒有客戶授權的"
                f"曝險變更。本輪不進行任何交易動作。請檢查 {self._ledger_path} 的"
                f"寫入權限與內容",
                dedup_key="capital_settings_ledger_out_of_range")
            raise CapitalSettingsUnavailable(
                f"資金設定帳本內的數值超出允許範圍（{self._ledger_path}）") from None
        self._last_applied = applied
        # 三個欄位一起換：`CopySettings.__post_init__` 有同一條「模式與金額必須配對」
        # 的不變量，只換其中兩個會在那裡直接 raise（那是對的——半套的設定不該生效）。
        return replace(base, allocated_capital=alloc, capital_utilization=util,
                       use_full_equity=applied.use_full_equity)

    # ---------- 主流程 ----------

    def effective(self, base: CopySettings) -> CopySettings:
        """回傳本輪真正該用的 `CopySettings`。

        raise 的情形只有一種：`CapitalSettingsUnavailable`（帳本讀不到／遺失／
        內容超界）→ 呼叫端必須讓本輪**零交易動作**，不得退回 base 照舊跑。

        其餘所有失敗（驗簽不過、數值超界、記錄讀不到、落帳失敗）都是
        **不套用 ＋ 沿用現狀**，絕不中斷跟單——已開的部位需要有人繼續管理。
        """
        try:
            ledger = self._ledger_or_raise()
        except CapitalSettingsUnavailable:
            raise            # 已告警且已指名事件，不要再包一層模糊掉
        except (OSError, ValueError) as e:
            # 帳本讀不到 ⇒ 既不知道哪些 nonce 兌現過，也不知道目前生效的是哪組值。
            self._critical(
                f"**資金設定帳本無法載入**（{self._ledger_path}）——本輪不進行任何"
                f"交易動作。**不退回環境預設值**：那會是一次沒有客戶授權的曝險變更",
                dedup_key="capital_settings_ledger_unreadable")
            raise CapitalSettingsUnavailable(
                f"資金設定帳本無法載入（{self._ledger_path}）: {e}") from e

        rec = self._my_record()
        if rec is None:
            # ⭐ 最常見的路徑（絕大多數 cycle 都沒有待套用的記錄）：完全安靜。
            return self._current(base, ledger.applied)

        nonce = rec.get("nonce")
        if isinstance(nonce, str) and self._already_redeemed(ledger, nonce):
            if self._record_fingerprint(rec) == ledger.redeemed[nonce]:
                # 正常的檔案殘留（記錄檔沒有人負責清）→ 忽略且**不告警**。
                return self._current(base, ledger.applied)
            self._critical(
                f"**資金設定記錄疑遭竄改**：nonce `{nonce}` 已於先前兌現為 "
                f"`{ledger.redeemed[nonce]}`，現在同一顆 nonce 卻配上另一組數值 —— "
                f"**拒絕套用**，沿用目前的設定。請檢查記錄檔 {self._settings_path} "
                f"的寫入權限與內容",
                dedup_key=f"capital_settings_nonce_reuse:{nonce}")
            return self._current(base, ledger.applied)

        user_address = self._trusted_user_address()
        if user_address is None:
            return self._current(base, ledger.applied)   # 已在該函式 log

        try:
            verified = verify_capital_settings(
                rec, account_id=self._account_id, user_address=user_address,
                now_s=float(self._now_fn()),
                consume_nonce=lambda n: not self._already_redeemed(ledger, n))
        except CapitalSettingsError as e:
            # ⭐ semantic：重試同一筆記錄必定再次失敗 → 不重試、不套用、大聲。
            # ⚠️ 告警與 log **只帶 reason 這個機器可讀碼**，不帶 str(e)、更不帶記錄
            # 內容——簽章與任何密鑰材料不得進 log／告警／例外訊息（紅線 3）。
            logger.error("資金設定驗簽失敗 account=%s reason=%s",
                         self._account_id, e.reason)
            self._critical(
                f"**資金設定記錄驗簽失敗**（reason=`{e.reason}`）——**不套用**，"
                f"沿用目前的設定。這是 semantic 失敗，重試同一筆記錄必定再次失敗；"
                f"若客戶確實要調整，請他重新取得待簽原文並重簽",
                dedup_key=f"capital_settings_verify_failed:{e.reason}")
            return self._current(base, ledger.applied)

        # ⭐⭐ 引擎端的**獨立**邊界驗證（本模組存在的核心理由，見檔頭）。
        # 刻意擺在驗簽**之後**：先確認這是客戶本人的意圖，再判斷這個意圖能不能執行
        # ——順序反過來的話，一筆偽造的超界記錄會得到「超界」的告警，把一次偽造
        # 探測誤報成一次客戶的手滑。
        try:
            validate_capital_bounds(verified.allocated_capital,
                                    verified.capital_utilization,
                                    use_full_equity=verified.use_full_equity)
        except CapitalSettingsError as e:
            logger.error("資金設定超出範圍 account=%s reason=%s",
                         self._account_id, e.reason)
            self._critical(
                "**客戶簽章的資金設定超出允許範圍** —— **拒絕套用**，沿用目前的設定。"
                "簽章本身是有效的；擋下它的是引擎自己的邊界檢查（API 也擋，但引擎"
                "不因為 API 驗過而省略——這兩個值直接乘進部位大小，一個壞值就是一次"
                "超額曝險）。允許範圍：使用比例 ∈ (0, 1]；投入本金在固定本金模式"
                "（use_full_equity=false）必須 > 0，在全部權益模式必須為 0",
                dedup_key="capital_settings_out_of_range")
            return self._current(base, ledger.applied)

        applied = AppliedCapital(
            nonce=verified.nonce,
            allocated_capital=verified.allocated_capital_str,
            capital_utilization=verified.capital_utilization_str,
            issued_at=verified.issued_at,
            applied_at=float(self._now_fn()),
            use_full_equity=verified.use_full_equity)
        old = ledger.applied.fingerprint if ledger.applied else (
            f"{base.allocated_capital}|{base.capital_utilization}"
            f"|{'full' if base.use_full_equity else 'fixed'}（環境預設）")
        try:
            save_ledger(self._ledger_path, CapitalLedger(
                redeemed={**ledger.redeemed, verified.nonce: applied.fingerprint},
                applied=applied))
        except OSError as e:
            # ⭐ 落帳失敗 ⇒ **不套用**。順序是刻意的：先落帳再套用。反過來的話，
            # 這一輪用了新設定卻沒記下 nonce，下一輪會再套用一次、再發一次告警。
            logger.error("資金設定帳本落檔失敗（%s）: %r", self._ledger_path, e)
            self._critical(
                f"**資金設定帳本落檔失敗**（{self._ledger_path}）——**不套用**該筆"
                f"設定，沿用目前的值。先落帳再套用是刻意的：套了卻沒記下 nonce，"
                f"下一輪會重複套用。請檢查狀態目錄的寫入權限",
                dedup_key="capital_settings_ledger_write_failed")
            return self._current(base, ledger.applied)

        self._critical(
            f"**已套用客戶簽章授權的資金設定**：{old} → {applied.fingerprint}"
            f"（來源：**客戶簽章授權**，account={self._account_id}）——"
            f"新的部位大小自**下一個 cycle** 起套用；**不做即時強制再平衡**，"
            f"部位隨 leader 的後續動作自然收斂（避免一次無謂的 taker 成本）。"
            f"本次變更已由引擎獨立重新驗章並重新檢查數值範圍",
            dedup_key=f"capital_settings_applied:{verified.nonce}")
        logger.info("資金設定已套用 account=%s %s → %s", self._account_id, old,
                    applied.fingerprint)
        return self._current(base, applied)

    # ---------- 小工具 ----------

    @staticmethod
    def _record_fingerprint(rec: dict) -> str | None:
        """記錄自稱的數值指紋（canonical 化後）；不合法回 None（→ 一律當成不相符）。

        ⭐ 必須與 `AppliedCapital.fingerprint` **同式**（含 `use_full_equity`）：
        兩式一旦漂移，比對就恆為不相符（每輪一則假的竄改告警）或恆為相符
        （真的竄改被當成檔案殘留放過）。旗標型別不合法 → None ＝ 不相符，
        走告警路徑而不是靜默忽略。
        """
        try:
            _, alloc, _, util = canonical_capital_values(
                rec.get("allocated_capital"), rec.get("capital_utilization"))
            flag = require_bool_flag(rec, "use_full_equity")
        except (CapitalSettingsError, ValueError, TypeError):
            return None
        return f"{alloc}|{util}|{'full' if flag else 'fixed'}"
