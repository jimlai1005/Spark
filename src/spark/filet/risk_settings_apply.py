"""src/spark/filet/risk_settings_apply.py
引擎側：**每輪讀取並套用客戶簽章的風控設定**，以及消化「立即解除熔斷」請求。

本模組與 capital_settings_apply.py 是同一個形狀的兩個實例（先讀那份檔頭，這裡只寫
**不同**的部分）。共同的骨架：
- 引擎**自己重新驗章**，不因為「API 已經驗過」而省略。
- `user_address` 一律取自 **manifest**，絕不取自記錄（記錄整份都在攻擊者的寫入
  範圍內，用它自帶的身分宣稱去驗它自己的簽章是假驗證）。
- 驗簽通過後**再驗一次數值邊界**（`risk_prefs.canonical_prefs`），超界一律拒絕、
  絕不 clamp。

⭐⭐ 與資金設定最大的不同（一）：**失效的安全方向是「沿用現狀」，不是「停止交易」**
--------------------------------------------------------------------------------
資金設定的帳本讀不到時，`CapitalSettingsApplier` 會 raise 讓引擎本輪零交易動作——
因為那兩個值直接乘進部位大小，不知道該押多大就不該押。

風控設定相反：本管線失效時的現狀是**上一份客戶親自簽過的門檻**，或部署當下由
auto-activate watcher 依客戶偏好寫進 env 的值（`risk_prefs.risk_env_lines`）。
兩者都是客戶自己表達過的意圖，而且都仍然在執法。此時停止跟單會讓「風控偏好的
讀取管線壞掉」升級成「已開的部位沒有人管理」——那是拿一個更大的風險去換一個
已經有安全預設值的小風險。所以本模組**任何情況都不 raise、不中斷跟單**。

⭐⭐ 與資金設定最大的不同（二）：**沒有 nonce 帳本，改用單調 issued_at 護欄**
----------------------------------------------------------------------------
資金設定用「已兌現 nonce 帳本」達成冪等。風控設定的記錄是**持續意圖**：同一筆記錄
會在每一輪被重新讀到、重新驗章，而它每一輪都應該產生同一個結果。冪等因此不是靠
「記得這顆 nonce 兌現過」，而是靠「這份意圖與目前生效的是同一份」。

判定基準是 `issued_at`（不是內容指紋）：
- **早於**已套用的 → **拒絕 ＋ critical**。這是回滾攻擊的形狀：有人把客戶三天前
  那份「風控全關」的舊記錄放回交換目錄，想讓引擎退回較弱的保護。內容指紋擋不住
  它（舊記錄的簽章是真的、內容也是客戶簽過的），只有時間順序擋得住。
- **相等** → 靜默沿用（正常狀態：同一份記錄躺在那裡不會被清掉，每輪都會讀到）。
- **晚於** → 套用。

護欄的持久化落點是**引擎自己的狀態根**（`FILET_STATE_DIR`，正式部署
`0700 filet-engine`），不是交換目錄——交換目錄的 owner 是 filet-api，把「最後套用的
時間」放在那裡等於讓被打穿的 API 自己決定「什麼算舊記錄」，護欄就不存在了。

⭐ 「下一 cycle 生效」＝**不做任何交易動作**
------------------------------------------
套用一組新的風控門檻只是換掉 `CopySettings` 的幾個欄位；下一輪的 `evaluate()` 自然
用新門檻判定。刻意不主動重算歷史、不主動平倉，也不因為新門檻較嚴就立刻熔斷——
新門檻在下一輪的判定裡自然生效，而那條判定路徑（killswitch.evaluate）已經是唯一的
執法點。反過來說，客戶調寬門檻**不會**解除一個已經觸發的熔斷：解鎖是另一個動作，
需要另一份簽章（見 `consume_unlock_request`）。
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, fields, replace
from decimal import Decimal
from pathlib import Path

from spark.copytrade.config import CopySettings
from spark.copytrade.killswitch import manual_rearm
from spark.copytrade.notifier import Notifier
from spark.filet.followers import load_followers
from spark.filet.leader_change_apply import require_exchange_dir
from spark.filet.risk_prefs import RISK_PARAM_SPECS, RiskPrefsError, canonical_prefs
from spark.filet.risk_settings import (RISK_SETTINGS_MAX_AGE_S, RiskSettingsError,
                                       load_risk_settings, load_risk_unlocks,
                                       risk_settings_path_for, risk_unlock_path_for,
                                       verify_risk_settings, verify_risk_unlock)

logger = logging.getLogger(__name__)

# 單調 issued_at 護欄的落點：**引擎自己的狀態根之下**（見檔頭）。與 kill switch 的
# ARM 檔同一個 `var/copytrade` 目錄——兩者都是「這顆引擎的風控狀態」，而且都必須
# 位於 filet-api 寫不到的地方。
APPLIED_STATE_RELPATH = Path("var/copytrade/risk_applied.json")
# 已消化的解鎖請求（一次性護欄，審查 F3）。同樣落在引擎自己的狀態根。
CONSUMED_UNLOCK_RELPATH = Path("var/copytrade/risk_unlock_consumed.json")


def resolve_risk_settings_path(env=None) -> str:
    """引擎要讀的風控設定記錄檔路徑（**啟動時呼叫一次**，未設 env ⇒ 拒絕啟動）。

    錨點與換 leader／資金設定共用 `require_exchange_dir`（同一個 `FILET_EXCHANGE_DIR`、
    同一段「漏設就拒絕啟動」的理由）。⚠️ 沒有「取不到就回預設值」這條路：那會讓
    API 寫的記錄引擎永遠讀不到，而兩邊的 log 都正常（C3 的形狀）。
    """
    return risk_settings_path_for(require_exchange_dir(env))


def resolve_risk_unlock_path(env=None) -> str:
    """解鎖記錄檔路徑（同一個交換目錄，同一條 require 檢查）。"""
    return risk_unlock_path_for(require_exchange_dir(env))


def copy_settings_field(spec: dict) -> str:
    """風控參數 spec → 它對應的 `CopySettings` 欄位名（**單一推導規則**）。

    ⭐ 由 spec 的 **env 鍵**推導（`COPY_RISK_COOLDOWN_HOURS` → `risk_cooldown_hours`），
    而不是另外維護一張 name→field 對照表：`CopySettings.from_env` 已經用同一個對應
    把 env 讀進欄位，多寫一張表就是第二份會漂移的定義，而漂移的症狀是「客戶調了
    某一項、其他項都生效、就那一項沒反應」——沒有任何錯誤訊息。
    （`name` 本身不夠用：`cooldown_hours` 對應的欄位是 `risk_cooldown_hours`。）

    推導不出真實欄位 → raise：那代表有人改了 spec 的 env 鍵卻沒改 CopySettings，
    在**建構時**炸掉遠好過在某一輪靜默地少套用一個門檻。
    """
    field = spec["env"].removeprefix("COPY_").lower()
    if field not in {f.name for f in fields(CopySettings)}:
        raise ValueError(
            f"風控參數 {spec['name']}（env {spec['env']}）推導出的 CopySettings 欄位 "
            f"{field!r} 不存在——spec 與 CopySettings 已漂移")
    return field


def prefs_to_settings_overrides(prefs: dict) -> dict:
    """canonical prefs → `dataclasses.replace` 用的欄位覆寫 dict。

    數值一律轉 `Decimal`（內部一律 Decimal 是專案慣例；prefs 存的是 canonical
    **字串**，那是落檔與重建訊息用的形式）。總開關另外映到 `risk_controls_enabled`
    ——它不在 `RISK_PARAM_SPECS` 裡（不是可調參數，是「要不要執法」）。
    """
    out: dict = {"risk_controls_enabled": bool(prefs["enabled"])}
    for spec in RISK_PARAM_SPECS:
        v = prefs[spec["name"]]
        out[copy_settings_field(spec)] = (bool(v) if spec["type"] == "bool"
                                          else Decimal(str(v)))
    return out


@dataclass(frozen=True)
class AppliedRiskSettings:
    """目前生效中的客戶簽章風控設定（狀態檔的內容，也是心跳的來源）。

    `prefs` 以 canonical **字串**存放（不是 Decimal 的 repr）：狀態檔會被人打開來看，
    也會被 json round-trip，字串是唯一在這兩件事之後仍然逐位元組相同的表示法
    （沿 `AppliedCapital` 的同一個決定）。
    """

    issued_at: str
    issued_at_s: float    # 單調護欄的比較基準（與 issued_at 由同一次解析產生）
    applied_at: float     # epoch 秒，稽核用
    prefs: dict


class RiskSettingsApplier:
    """每 cycle 把「客戶簽章的風控設定」疊在基礎 `CopySettings` 之上。

    用法（見 scripts/run_copytrade.make_risk_settings_applier）：

        applier.consume_unlock_request(state_root)   # 先處理「立即恢復」
        cs = applier.effective(base_settings)        # 再套用門檻

    ⚠️ 與 `CapitalSettingsApplier` 的關鍵差異：本類別的 `effective()` **絕不 raise**
    （見檔頭），所以呼叫端不需要、也不該為它準備「本輪零交易動作」的分支。
    """

    def __init__(self, *, account_id: str, manifest_path: str | Path,
                 settings_path: str | Path, unlock_path: str | Path,
                 state_root: Path, notifier: Notifier, now_fn=time.time):
        self._account_id = account_id
        self._manifest_path = Path(manifest_path)
        self._settings_path = Path(settings_path)
        self._unlock_path = Path(unlock_path)
        self._state_path = Path(state_root) / APPLIED_STATE_RELPATH
        self._consumed_path = Path(state_root) / CONSUMED_UNLOCK_RELPATH
        self._notifier = notifier
        self._now_fn = now_fn
        # 目前生效中的客戶簽章設定（跨 cycle 保留：狀態檔讀不到時仍能沿用現狀，
        # 而不是靜默退回 env 值）。
        self._applied: AppliedRiskSettings | None = None
        self._last_applied: AppliedRiskSettings | None = None

    @property
    def last_applied(self) -> AppliedRiskSettings | None:
        """上一次 `effective()` 實際採用的客戶簽章設定；**None ＝沿用環境預設**。

        ⭐ 理由與 `CapitalSettingsApplier.last_applied` 完全相同（先讀那份 docstring）：
        心跳要回報「目前生效的風控設定**與它的來源**」，而那個來源必須出自產生本輪
        `CopySettings` 的**同一次**求值。心跳若自己再讀一次狀態檔，兩次讀取之間狀態
        可能已經變了，於是心跳會宣稱一組本輪根本沒有被用來判定風險的門檻。

        設定非 None 值的地方只有 `_current`（所有「本輪用哪組設定」的路徑都收斂在
        那裡），清成 None 的地方是 `effective()` 的進入點與 `_current` 的開頭。
        """
        return self._last_applied

    # ---------- 告警 ----------

    def _critical(self, text: str, *, dedup_key: str) -> None:
        """發 critical，且**告警失敗不得中斷跟單**（沿 CapitalSettingsApplier._critical）。

        注入的 Notifier 沒有「自己吞例外」的保證。這裡若讓例外逸出，「Telegram 掛了」
        會一路變成「引擎停機、部位無人看管」——觀測層壞掉絕不能弄停被觀測的系統。
        """
        try:
            self._notifier.critical("risk", text, dedup_key=dedup_key)
        except Exception:  # noqa: BLE001 —— 見 docstring：告警失敗只 log
            logger.exception("風控設定告警發送失敗（跟單不受影響）")

    # ---------- 讀取 ----------

    def _my_record(self, path: Path, loader) -> dict | None:
        """本帳號的記錄；無記錄或讀取失敗皆回 None（**只 log 不告警**）。

        讀取失敗是 transient（IO 打嗝、檔案正在被換、格式壞）→ 絕大多數 cycle 根本
        沒有新記錄，這條路徑一旦會告警就會變成每輪洗版，真正的 critical 反而被淹沒
        （沿 capital_settings_apply 的同一條慣例）。不套用本身已是安全的一側。
        """
        try:
            records = loader(path)
            mine = [r for r in records
                    if isinstance(r, dict) and r.get("account_id") == self._account_id]
        except (OSError, ValueError, TypeError, AttributeError) as e:
            logger.warning("風控記錄讀取失敗（%s），沿用現狀: %r", path, e)
            return None
        # 寫端是「同 account 覆蓋」，正常情況至多一筆；取最後一筆＝取最新意圖。
        return mine[-1] if mine else None

    def _trusted_user_address(self) -> str | None:
        """manifest 登錄的 user_address＝**唯一**可信的簽章者比對基準。

        拿不到（manifest 缺席／查無此帳號／讀不到／格式壞）→ None ＝不套用 ＋ critical。
        沒有可信基準時「先驗了再說」等於用記錄自己的內容驗它自己，那是假驗證；
        而這條路徑會讓客戶的每一次調整都無聲地不生效，所以要大聲。
        """
        try:
            refs = load_followers(self._manifest_path)
        except (OSError, ValueError) as e:
            logger.warning("風控設定：follower manifest 讀取失敗（%s）: %r",
                           self._manifest_path, e)
            refs = None
        if refs is not None:
            for r in refs:
                if r.account_id == self._account_id:
                    return r.user_address
        self._critical(
            f"**風控設定：取不到可信的 user_address**（manifest {self._manifest_path}"
            f" 讀取失敗或查無 account={self._account_id}）——**不套用**任何客戶簽章的"
            f"風控設定，沿用目前的值。沒有可信的比對基準時驗章是假驗證",
            dedup_key="risk_settings_no_trusted_user")
        return None

    # ---------- 單調護欄的狀態檔 ----------

    def _load_state(self) -> AppliedRiskSettings | None:
        """讀「最後套用的設定」；檔案不存在 → None（尚未套用過任何客戶簽章設定）。

        格式壞掉一律 raise（fail-closed）：把壞檔當成「沒套用過」會讓單調護欄失憶，
        於是一筆舊記錄可以被重放回去（回滾攻擊）。呼叫端把它處理成 critical ＋
        沿用記憶體中的現狀，**不重置護欄**。
        """
        p = self._state_path
        if not p.exists():
            return None
        raw = json.loads(p.read_text())
        if not isinstance(raw, dict):
            raise ValueError(f"風控套用狀態檔頂層須為物件（{p}）")
        prefs = raw.get("prefs")
        if not isinstance(prefs, dict):
            raise ValueError(f"風控套用狀態檔缺少 prefs（{p}）")
        return AppliedRiskSettings(issued_at=str(raw["issued_at"]),
                                   issued_at_s=float(raw["issued_at_s"]),
                                   applied_at=float(raw.get("applied_at", 0.0)),
                                   prefs=prefs)

    def _save_state(self, applied: AppliedRiskSettings) -> None:
        """原子落檔（tmp + os.replace，同目錄；tmp 檔名帶 pid，沿 equity.py `_save`）。

        原子性不是潔癖：寫到一半被 kill／斷電，重啟後讀到撕裂檔 → `_load_state`
        raise → 單調護欄本輪判不出來（而護欄是擋回滾攻擊的唯一機制）。
        """
        p = self._state_path
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {"issued_at": applied.issued_at,
                   "issued_at_s": applied.issued_at_s,
                   "applied_at": applied.applied_at,
                   "prefs": dict(applied.prefs)}
        tmp = p.parent / f"{p.name}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        os.replace(tmp, p)

    # ---------- 現狀 ----------

    def _current(self, base: CopySettings,
                 applied: AppliedRiskSettings | None) -> CopySettings:
        """本輪該用的設定，在「沒有新記錄可套用」的所有路徑上共用。

        `applied` 為 None → 原樣回傳 base（從未有客戶簽章的風控設定；base 的值來自
        env，那是 auto-activate watcher 依客戶啟用當下的偏好寫進去的）。

        有 override → 套上去，且**每輪重驗邊界**：狀態檔裡的值也可能被竄改（狀態
        目錄權限出問題、備份還原了一份壞檔），而它每一輪都決定要不要熔斷。
        「寫進狀態檔時驗過了」不足以保證「現在讀出來的還是那個值」。
        超界 → critical ＋ 退回 base（env 的部署值仍是客戶啟用當下的選擇，
        而且仍在執法），**絕不 clamp**。
        """
        self._last_applied = None
        if applied is None:
            return base
        try:
            overrides = prefs_to_settings_overrides(canonical_prefs(applied.prefs))
            settings = replace(base, **overrides)
        except (RiskPrefsError, ValueError, TypeError, ArithmeticError, KeyError):
            # ⚠️ 只帶結構性描述，不帶記錄／狀態檔內容（紅線 2）。
            self._critical(
                f"**風控套用狀態檔內的值不合法或超出允許範圍**（疑遭竄改，"
                f"{self._state_path}）——**拒絕套用**，本輪改用部署時寫入的風控設定"
                f"（客戶啟用當下的選擇，仍在執法）。**不夾取**任何超界值：夾取會讓"
                f"引擎執行一組客戶沒有簽署的門檻。請檢查該檔的寫入權限與內容",
                dedup_key="risk_settings_state_invalid")
            return base
        self._last_applied = applied
        return settings

    # ---------- 主流程 ----------

    def effective(self, base: CopySettings) -> CopySettings:
        """回傳本輪真正該用的 `CopySettings`。**絕不 raise、絕不中斷跟單**。

        所有失敗（記錄讀不到、驗簽不過、數值超界、回滾、狀態檔讀寫失敗）一律是
        **不套用 ＋ 沿用現狀**（見檔頭：現狀是上一份客戶簽過的設定，或部署當下依
        客戶偏好寫入的 env 值——兩者都仍在執法）。
        """
        # 進入點就先清掉 `last_applied`（沿 CapitalSettingsApplier 的同一條教訓：
        # 提前退出的路徑若留著上一輪的值，心跳會發布一組本輪沒有生效的設定）。
        self._last_applied = None
        try:
            stored = self._load_state()
        except (OSError, ValueError, TypeError, KeyError) as e:
            logger.error("風控套用狀態檔無法載入（%s）: %r", self._state_path, e)
            self._critical(
                f"**風控套用狀態檔無法載入**（{self._state_path}）——本輪沿用目前的"
                f"風控設定，且**不套用**任何新記錄：這個檔是擋「舊記錄被放回去」"
                f"（回滾攻擊）的唯一依據，讀不到就沒有辦法判斷一筆記錄是新是舊。"
                f"請檢查狀態目錄的權限與內容",
                dedup_key="risk_settings_state_unreadable")
            return self._current(base, self._applied)
        if stored is None and self._applied is not None:
            # 檔案曾經存在（本進程寫過）、現在不見了 ⇒ 護欄失憶。沿用記憶體中的現狀，
            # 且本輪不套用新記錄——「檔案不見了」與「從未套用過」在這裡是兩件事，
            # 而把前者當成後者正好打開回滾的門。
            self._critical(
                f"**風控套用狀態檔消失**（{self._state_path} 曾經存在）——沿用目前"
                f"的風控設定，本輪不套用新記錄。請確認狀態目錄未被重建",
                dedup_key="risk_settings_state_lost")
            return self._current(base, self._applied)
        if stored is not None:
            self._applied = stored
        applied = self._applied

        rec = self._my_record(self._settings_path, load_risk_settings)
        if rec is None:
            # 最常見的路徑（客戶還沒簽過、或記錄沒變）：完全安靜。
            return self._current(base, applied)

        user_address = self._trusted_user_address()
        if user_address is None:
            return self._current(base, applied)      # 已在該函式告警

        try:
            verified = verify_risk_settings(
                rec, account_id=self._account_id, user_address=user_address,
                now_s=float(self._now_fn()),
                # ⭐ 時效刻意放行（見 risk_settings 檔頭）：這是持續意圖，不是一次性
                # 動作。引擎停機超過 10 分鐘後若因過期而套不上，客戶的設定會**無聲地**
                # 退回 env 值。既有先例：filet_auto_activate._latest_signed_leader。
                max_age_s=None,
                # nonce 的一次性由單調 issued_at 護欄取代（見檔頭）：同一筆記錄每輪
                # 都會被讀到，用「兌現過就拒絕」會讓持續意圖只生效一輪。
                consume_nonce=lambda _n: True)
        except RiskSettingsError as e:
            # semantic：重試同一筆記錄必定再次失敗 → 不重試、不套用、大聲。
            # ⚠️ 告警與 log **只帶 reason 這個機器可讀碼**，不帶 str(e)、更不帶記錄
            # 內容——簽章與任何密鑰材料不得進 log／告警／例外訊息（紅線 2）。
            logger.error("風控設定驗簽失敗 account=%s reason=%s",
                         self._account_id, e.reason)
            self._critical(
                f"**風控設定記錄驗簽失敗**（reason=`{e.reason}`）——**不套用**，"
                f"沿用目前的風控設定。這是 semantic 失敗，重試同一筆記錄必定再次"
                f"失敗；若客戶確實要調整，請他重新取得待簽原文並重簽",
                dedup_key=f"risk_settings_verify_failed:{e.reason}")
            return self._current(base, applied)

        # ⭐⭐ 引擎端的**獨立**邊界重驗（不信任 API 驗過：它是一個可能被打穿、可能有
        # bug、可能被繞過的進程）。擺在驗簽**之後**：先確認這是客戶本人的意圖，再判斷
        # 這個意圖能不能執行——順序反過來的話，一筆偽造的超界記錄會得到「超界」的
        # 告警，把一次偽造探測誤報成一次客戶的手滑。
        # ⚠️ **誠實標註**：以目前的 `risk_settings.verify_risk_settings` 實作，超界值
        # 在**重建待簽訊息時**就已經被 `canonical_prefs` 擋成 `malformed`（訊息必須用
        # canonical 值重建，而 canonical 化與區間檢查在 risk_prefs 是同一個函式），
        # 所以這一格在正常情況下不會被觸發——它是縱深防禦，不是主要防線。留著的理由：
        # 「哪一層負責擋超界」不該由另一個模組的內部實作細節決定；驗簽那一層若哪天
        # 改成寬鬆正規化，少了這一格就是靜默放行一組客戶沒簽過的門檻。
        try:
            prefs = canonical_prefs(verified.prefs)
        except RiskPrefsError as e:
            logger.error("風控設定超出範圍 account=%s reason=%s",
                         self._account_id, e.reason)
            self._critical(
                f"**客戶簽章的風控設定超出允許範圍**（reason=`{e.reason}`）——"
                f"**拒絕套用**，沿用目前的風控設定，且**絕不夾取**到邊界內：夾取會讓"
                f"客戶簽了 A、引擎執行 B，而且沒有人會知道。簽章本身可能是有效的；"
                f"擋下它的是引擎自己的邊界檢查（API 也擋，但引擎不因為 API 驗過而省略）",
                dedup_key="risk_settings_out_of_range")
            return self._current(base, applied)

        if applied is not None:
            if verified.issued_at_s < applied.issued_at_s:
                # ⭐⭐ 回滾攻擊的形狀：一份**客戶真的簽過**的舊記錄被放回交換目錄，
                # 想讓引擎退回較弱的保護。簽章是真的、內容也是客戶的——只有時間順序
                # 擋得住它，所以這一格必須大聲。
                self._critical(
                    f"**收到較舊的風控設定記錄**（簽署於 {verified.issued_at}，"
                    f"目前生效的簽署於 {applied.issued_at}）——**拒絕套用**，維持目前"
                    f"的風控設定。合法的調整永遠比目前生效的新；較舊的記錄被放回交換"
                    f"目錄是回滾攻擊的形狀（用一份客戶真的簽過的舊授權換回較弱的保護）。"
                    f"請檢查 {self._settings_path} 的寫入權限",
                    dedup_key=f"risk_settings_rollback:{verified.issued_at}")
                return self._current(base, applied)
            if verified.issued_at_s == applied.issued_at_s:
                # 冪等：同一份意圖躺在交換目錄裡不會被清掉，每輪都會讀到。靜默。
                return self._current(base, applied)

        new = AppliedRiskSettings(issued_at=verified.issued_at,
                                  issued_at_s=verified.issued_at_s,
                                  applied_at=float(self._now_fn()), prefs=prefs)
        try:
            # ⭐ 先寫狀態檔再套用（順序沿 capital 的「先 save_ledger 再套用」）：
            # 反過來的話，這一輪用了新設定卻沒記下 issued_at，下一輪會再套用一次、
            # 再發一次告警，而且單調護欄對這段期間完全失效。
            self._save_state(new)
        except OSError as e:
            logger.error("風控套用狀態檔落檔失敗（%s）: %r", self._state_path, e)
            self._critical(
                f"**風控套用狀態檔落檔失敗**（{self._state_path}）——**不套用**該筆"
                f"設定，沿用目前的值。先寫狀態檔再套用是刻意的：套了卻沒記下 "
                f"issued_at，下一輪會重複套用，且回滾護欄失效。請檢查狀態目錄的寫入權限",
                dedup_key="risk_settings_state_write_failed")
            return self._current(base, applied)

        self._applied = new
        old = (applied.prefs if applied is not None else "環境預設")
        self._critical(
            f"**已套用客戶簽章授權的風控設定**（account={self._account_id}，"
            f"簽署於 {verified.issued_at}）：{old} → {prefs}。新的門檻自**下一個 "
            f"cycle** 的風險判定起生效；本次變更不觸發任何交易動作，也不會解除一個"
            f"已經觸發的熔斷（那需要另一份簽章）。本次變更已由引擎獨立重新驗章"
            f"並重新檢查數值範圍",
            dedup_key=f"risk_settings_applied:{verified.issued_at}")
        logger.info("風控設定已套用 account=%s issued_at=%s", self._account_id,
                    verified.issued_at)
        return self._current(base, new)

    # ---------- 立即解除熔斷 ----------

    # ── 已消化的解鎖請求（一次性護欄，審查 F3）──────────────────────────
    def _load_consumed_unlock(self) -> str | None:
        """上一次被消化的解鎖請求 `issued_at`；讀不到一律 None（＝不擋）。

        讀不到就不擋的方向是刻意的：這道護欄防的是**重放**，而它讀不到時的替代
        防線（ARM 檔已刪、請求須晚於 tripped_at、600 秒時效）都還在。若改成讀不到
        就拒絕，一次檔案權限問題會讓客戶永遠按不動「立即恢復」——那是把防重放
        升級成阻斷客戶的正當操作。
        """
        try:
            return json.loads(self._consumed_path.read_text()).get("issued_at")
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def _save_consumed_unlock(self, issued_at: str) -> None:
        """記下已消化的 `issued_at`；寫失敗只 critical，不影響已完成的解鎖。"""
        try:
            self._consumed_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._consumed_path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"issued_at": issued_at}))
            os.replace(tmp, self._consumed_path)
        except OSError as e:
            self._critical(
                f"解除熔斷的一次性記錄寫入失敗（{self._consumed_path}: {e!r}）"
                f"——鎖已解除，但同一份簽章在時效內可能可以再次解鎖，請留意",
                dedup_key="risk_unlock_consumed_write_failed")

    def consume_unlock_request(self, root: Path) -> bool:
        """消化一筆「立即解除熔斷鎖定」請求；回傳是否真的解除了。**絕不 raise**。

        流程（任何一關不過就回 False 並**維持鎖定**）：
        1. 本帳號有解鎖記錄嗎（沒有＝最常見的正常狀態，完全安靜）。
        2. manifest 拿得到可信的 user_address 嗎。
        3. 驗章 ＋ **強制時效**（`RISK_SETTINGS_MAX_AGE_S`＝600 秒，與資金設定同一個
           常數語意）：⭐ 這是與風控設定最重要的差別。解鎖是**一次性動作**，「現在
           恢復跟單」是一個當下的決定；放行時效等於客戶簽一次就永久放棄熔斷保護。
        4. `killswitch.manual_rearm` 檢查 ARM 檔那一側：請求必須**晚於** `tripped_at`
           （擋熔斷前預簽的重放），且觸發原因必須可恢復（`leader_revoked` 不得由
           客戶自助解除——那是治理動作）。ARM 檔的判定與刪除全在 killswitch，
           本模組只負責證明「這是本人、而且是剛剛簽的」。

        5. **一次性**：已消化的 `issued_at` 記在引擎自己的狀態根
           （`risk_unlock_consumed.json`，filet-api 寫不到），同一份簽章只能開一次鎖。

        ⚠️⚠️ 第 5 步是 2026-07-30 獨立審查 F3 的修正。原本靠「ARM 檔已刪」＋
        「請求須晚於 tripped_at」＋「600 秒時效」三道疊加，以為足夠——**不夠**：
        `issued_at` 由請求內容決定，而驗章允許它落在未來 600 秒內（時鐘偏移容差）。
        被打穿的 API 把待簽原文的 `issued_at` 蓋成 +599 秒，客戶按一次正當的
        「立即恢復」，接下來約十分鐘內**每一次**熔斷都會被同一份記錄自動解開
        （實測一份記錄連開三次鎖）。待簽原文對客戶承諾的是「This authorises one
        resume only」，那句承諾必須有東西撐著。
        """
        try:
            rec = self._my_record(self._unlock_path, load_risk_unlocks)
            if rec is None:
                return False
            user_address = self._trusted_user_address()
            if user_address is None:
                return False
            try:
                verified = verify_risk_unlock(
                    rec, account_id=self._account_id, user_address=user_address,
                    now_s=float(self._now_fn()),
                    # ⭐ 強制時效（不傳 None）：見 docstring 第 3 點。
                    max_age_s=RISK_SETTINGS_MAX_AGE_S,
                    consume_nonce=lambda _n: True)
            except RiskSettingsError as e:
                if e.reason == "expired":
                    # 過期是**預期會發生的常態**（記錄沒人清，客戶十分鐘前按過一次），
                    # 不是事故 → 只 log，避免每輪 critical 洗版把真事件淹掉。
                    logger.info("解除熔斷請求已過期，維持鎖定 account=%s",
                                self._account_id)
                    return False
                logger.error("解除熔斷請求驗簽失敗 account=%s reason=%s",
                             self._account_id, e.reason)
                self._critical(
                    f"**解除熔斷請求驗簽失敗**（reason=`{e.reason}`）——**維持鎖定**。"
                    f"這是 semantic 失敗，重試同一筆記錄必定再次失敗；若客戶確實要"
                    f"恢復跟單，請他重新取得待簽原文並重簽",
                    dedup_key=f"risk_unlock_verify_failed:{e.reason}")
                return False
            # 一次性護欄（見 docstring 第 5 點）：同一份簽章只能開一次鎖。
            consumed = self._load_consumed_unlock()
            if consumed is not None and verified.issued_at <= consumed:
                logger.info("解除熔斷請求已被消化過，維持鎖定 account=%s",
                            self._account_id)
                return False
            # ARM 檔那一側的判定與刪除都在 killswitch（誰擁有鎖，誰負責開鎖）。
            # 成功時的 critical 也由它發（連同持久告警檔），這裡不重複發一則。
            opened = manual_rearm(root, self._notifier,
                                  requested_at_iso=verified.issued_at)
            if opened:
                # ⭐ 落檔在**解鎖成功之後**：寫失敗時最壞情況是同一份簽章還能再開一次
                # （回到修正前的行為），而先寫後開的最壞情況是「記為已用、實際沒開」
                # ——客戶按了沒反應且再也按不動，那更糟。
                self._save_consumed_unlock(verified.issued_at)
            return opened
        except Exception:  # noqa: BLE001 —— 解鎖管線壞掉絕不能中斷跟單
            logger.exception("解除熔斷請求處理失敗（維持鎖定，跟單不受影響）")
            return False
