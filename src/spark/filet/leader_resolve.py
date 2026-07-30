"""src/spark/filet/leader_resolve.py
引擎側「這個 follower 跟誰」的解析 ＋ **白名單二次驗證**。

⭐ 為什麼引擎要自己再驗一次白名單（本模組存在的唯一理由）
--------------------------------------------------------
客戶會透過 filet-api 改自己的 leader（web 層 → pending.json → 人工 activate →
manifest）。**API 進程若被打穿，攻擊者能把 follower 指向惡意 leader**：例如與自己
對敲把 builder fee 榨乾、或反向交易把客戶資金洗掉。leader 白名單檔（leaders.json）
只有管理端能寫，filet-api 對它沒有寫權（權限拓撲見 leaders.py 檔頭）。

所以引擎**不得**因為「API 寫入時已驗過」「activate CLI 已驗過」而省略驗證——
這道防線的價值**正在於它獨立於 API**：攻擊者能改的（pending.json、乃至 manifest）
與能驗的（白名單）是兩個信任域，只有在使用 leader 的那一刻、由引擎自己拿白名單
比對，才擋得住「寫入時合法、事後被竄改」的路徑。省略它，前面兩道就退化成純裝飾。

同理，本模組**每個 cycle 重新解析**（不只啟動時一次）：讓客戶換 leader 不必重啟
服務、不必給 web 層提權，代價是竄改的時間窗從「到下次重啟」縮短到「一個 cycle」，
但**每一輪都會重新過白名單**——竄改成不在白名單的 leader 在下一輪就被擋下。

解析規則（resolve_leader）
------------------------
1. 讀 manifest 找到自己的 FollowerRef。
2. `ref.leader_address` 有值 → **必須通過 is_still_permitted**（引擎持續驗證用的
   述詞，只看 enabled；`accepting_new` 只擋新客戶——見 leaders.py），否則拒絕。
   白名單檔不存在時本路徑**一樣拒絕**：明確指定了 leader 卻沒有白名單可驗，
   正是被竄改後的樣子，不豁免。
3. `ref.leader_address` 為 None → 回退 env `COPY_LEADER_ADDRESS`；**env 預設也要
   過白名單**（一致性：不能因為是預設值就豁免）。⚠️ 唯一例外：白名單**檔案不存在**
   時放行並 log warning——否則所有既有部署（尚未策劃白名單）升級後立刻停擺。
   檔案存在但為空清單 = 管理端明確表態「目前沒有可選的 leader」→ 照樣拒絕
   （「不存在」與「明確空」是兩種語意，不可混為一談）。
4. leader **不得等於 follower 自己的位址**（自己跟自己無意義，且會形成回饋迴圈：
   本方下的單會被下一輪當成 leader 目標再放大）→ 拒絕。

⭐ 失敗語意：撤銷（semantic）與暫時失敗（transient）必須分開（工程原則 2、3）
--------------------------------------------------------------------------
2026-07-19 opus 對抗性審查 Critical：本模組原本用一個 `except Exception` 把所有
執行中失敗都當成「沿用上一個 leader ＋ critical」。但兩種失敗的性質完全相反：

- **transient**：白名單檔暫時讀不到、IO 錯、manifest 暫時損毀 → 解析**沒有結論**，
  沿用上一個已驗證值是對的（已在跑的部位需要有人繼續管理，停手＝部位無人看管）。
- **semantic（撤銷）**：解析成功，白名單**明確拒絕**這個 leader（`enabled:false`
  或整筆被移除）→ 沿用舊值是災難：管理端把出事的 leader（帳號被盜、對敲、策略
  失控）撤下來之後，跟他的人會**永遠繼續跟下去**，而操作者以為已經止血了。

所以撤銷走獨立的 `LeaderRevokedError`，由 `LeaderWatch.refresh()` 觸發**受控收尾**
（停止開新倉 → 平掉現有部位 → 鎖死 kill switch，重用 killswitch.trip）。
`accepting_new:false` 的**例行下架**不走這條路（leaders.py 檔頭有兩個旗標的分工）。

逐條：
- **啟動時**解析失敗（含撤銷）→ 呼叫端拒絕啟動（LeaderResolutionError 上拋），
  **不得靜默回退 env**：靜默回退正是攻擊者想要的降級路徑。啟動時撤銷不需收尾——
  本來就還沒開倉。
- **執行中撤銷**（LeaderRevokedError）→ 受控收尾＋critical，`refresh()` 回 `None`
  表示「本輪起不得交易」。
- **執行中其他失敗**（白名單檔壞掉/讀不到、manifest 暫時損毀）→ 沿用上一個已驗證
  leader ＋ critical，**絕不**停止跟單、**絕不**靜默切換（原則 3）。
- **leader 實際變更** → critical，訊息含舊 leader／新 leader／來源。換 leader 會讓
  引擎收斂到新 leader 的部位（平舊開新，有實際 taker 成本），屬重大事件必須留痕。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from spark.copytrade.notifier import Notifier
from spark.filet.followers import FollowerRef, load_followers, normalize_hex_address
from spark.filet.leaders import find_leader, is_still_permitted, load_leaders
from spark.filet.user_leaders import load_user_leaders, merge_leaders

logger = logging.getLogger(__name__)

# repo 根（.../src/spark/filet/leader_resolve.py → 上溯 3 層）。
_REPO_ROOT = Path(__file__).resolve().parents[3]

# ⭐ 預設路徑必須是**絕對**的（2026-07-19 opus 對抗性審查 I4）。原本是 CWD 相對的
# "var/filet/leaders.json"：引擎的 CWD 被 systemd 釘在 /opt/filet/spark，管理端跑
# activate CLI 的 CWD 是他當下所在的目錄——兩邊會驗**不同的檔案**。危險方向是
# fail-open：管理端在 A 檔把 leader 下架、引擎讀的 B 檔仍列著他 → 下架無聲失效
# （與撤銷收尾疊加後，連「已止血」的錯覺都還在）。沿用 resolve_state_dir() 對同一類
# 問題的既有解法（.resolve() 錨定，不靠啟動目錄）。
DEFAULT_MANIFEST_PATH = str(_REPO_ROOT / "var" / "filet" / "followers.json")
# ⚠️ 2026-07-20 起 `DEFAULT_LEADERS_PATH` **不再是任何消費端的隱含 fallback**——它只
# 是「開發機該把 FILET_LEADERS_PATH 設成什麼」的參考值（與 DEFAULT_EXCHANGE_DIR 同性質）。
# 理由見 require_leaders_path 的 docstring；取值一律走那個函式或 ApiConfig 的 required 清單。
DEFAULT_LEADERS_PATH = str(_REPO_ROOT / "var" / "filet" / "leaders.json")
# 客戶簽章換 leader 記錄的**交換目錄**在開發機該設成什麼（正式部署是
# /var/lib/filet-exchange）。⚠️ 這**不是**隱含 fallback——寫端（ApiConfig.from_env）
# 與讀端（leader_change_apply.require_exchange_dir）都把 FILET_EXCHANGE_DIR 列為
# 必填，漏設一邊直接拒絕啟動。放在本模組是因為這裡已經是所有 var/filet 預設路徑的
# 單一錨點；兩邊各自宣告一份「開發機預設」正是 C3 那類漂移的起點。
DEFAULT_EXCHANGE_DIR = str(_REPO_ROOT / "var" / "filet")

# 五個消費端共用的白名單環境變數名（API／引擎／activate CLI／兩個快照 timer）。
LEADERS_PATH_ENV = "FILET_LEADERS_PATH"


def require_leaders_path(env=None) -> str:
    """解出策劃 leader 白名單的路徑；**未設或非絕對路徑一律 raise ⇒ 拒絕啟動**。

    ⭐⭐ 這是同一個失敗模式的**第三次**（2026-07-20）
    ------------------------------------------------
    前兩次是 `FILET_EXCHANGE_DIR`（見 leader_change_apply.require_exchange_dir）與
    `FILET_STATE_BASE`（見 publicapi.config.state_base）：一條路徑、多個進程各自推導
    一次，靠字面量剛好相等而成立。本變數是第三個，而且共用它的進程最多：

    | 消費端 | 用途 | 讀取點 |
    |---|---|---|
    | `filet-api`（目錄頁／選 leader） | 客戶**能選誰** | `ApiConfig.from_env` 的 required 清單 |
    | `filet-follower@`（引擎每輪二次驗證） | 已在跟的人**還能不能繼續** | `run_copytrade.make_leader_resolver` |
    | `filet_activate`（人工 CLI 硬閘） | 管理端**核可誰** | `--leaders` ／ 本函式 |
    | `filet-leaderboard`／`filet-perf-series` | 快照與序列**抓誰** | `watchlist_snapshot.resolve_targets` |

    舊版每一個消費端都寫 `env.get(...) or DEFAULT_LEADERS_PATH`，於是漏設的症狀是
    **靜默的**：實機上 `filet-api.service` 根本沒宣告這個變數，只因為它的 CWD 是
    `/opt/filet/spark`、而 `DEFAULT_LEADERS_PATH` 又錨定 repo 根，兩邊才「恰好」指到
    同一個檔。任何一次改動（換部署路徑、改 WorkingDirectory、把白名單搬去 /etc）都會
    讓 API 與引擎讀**不同的白名單**——而危險方向是 fail-open：管理端在引擎那份撤銷了
    一個 leader，API 這份仍列著他，客戶照樣選得到、activate 照樣放行。

    修法與前兩次完全相同：**拿掉 fallback**，五個消費端全部必填，漏設的那一邊直接
    起不來。「起不來」在部署當下就被看見；「起來了但兩邊讀不同檔」沒有任何 log。

    **絕對路徑也在此強制**：這正是舊預設值當初被改成絕對路徑的理由（I4）——引擎的
    CWD 被 systemd 釘死、管理端跑 CLI 的 CWD 是他當下所在，相對路徑會讓兩邊驗不同檔。
    移除預設值若不把這個不變式一起搬過來，等於用一個新洞換掉舊洞。

    檔案**不存在不是錯誤**，故意不檢查（與 require_exchange_dir 的目錄檢查不同）：
    「尚未策劃任何 leader」是合法狀態，由 load_leaders 回空清單 → 全部拒絕（安全預設），
    而 resolve_leader 對「白名單檔不存在」另有明訂的向後相容豁免。在此檢查存在性會把
    那兩條既有語意悄悄改掉。
    """
    env = os.environ if env is None else env
    raw = env.get(LEADERS_PATH_ENV)
    if not raw:
        raise ValueError(
            f"缺少環境變數 {LEADERS_PATH_ENV}（策劃 leader 白名單路徑，正式部署為 "
            f"/opt/filet/spark/var/filet/leaders.json，開發機用 {DEFAULT_LEADERS_PATH}）。"
            f"filet-api／filet-follower@／filet-leaderboard／filet-perf-series 四個 unit "
            f"與人工 activate CLI **全部都要指到同一個值**——刻意沒有預設值：漏設一邊會讓"
            f"「客戶能選誰」與「引擎放行誰」讀不同的檔，而兩邊的 log 都正常。"
            f"見 deploy/RUNBOOK.md §5.5")
    if not Path(raw).is_absolute():
        raise ValueError(
            f"{LEADERS_PATH_ENV}={raw} 必須是絕對路徑——相對路徑會隨進程的 CWD 漂移"
            f"（引擎的 CWD 由 systemd 釘死，管理端跑 CLI 的 CWD 是他當下所在），"
            f"兩邊於是驗**不同的檔**，而危險方向是下架無聲失效。見 deploy/RUNBOOK.md §5.5")
    return raw


SOURCE_MANIFEST = "manifest"
SOURCE_ENV_DEFAULT = "env_default"
# ⭐ 客戶親自簽章授權的 leader（leader_change_apply 套用後的來源標記）。刻意與
# SOURCE_MANIFEST 分開：manifest 是「營運端登錄的」、customer_signed 是「客戶本人
# 用私鑰授權的」，兩者的信任來源完全不同。告警訊息會帶上這個字串，操作者看到
# leader 變更時第一眼就能分辨「這是客戶自己要求的」還是「有人改了 manifest」。
SOURCE_CUSTOMER_SIGNED = "customer_signed"


class LeaderResolutionError(ValueError):
    """leader 解析失敗（**transient**：讀不到、檔案缺失、格式壞）。

    啟動時＝拒絕啟動；執行中＝沿用上一個已驗證 leader＋critical（解析沒有結論，
    不代表現在跟的這個 leader 有問題）。
    """


class LeaderRevokedError(LeaderResolutionError):
    """⭐ **semantic**：解析成功，但白名單明確拒絕這個 leader（`enabled:false`
    或整筆被移除）。

    **只**在「白名單成功載入且明確拒絕」時 raise——白名單檔不存在／載入失敗一律是
    上面的 transient 基類，不得升級成撤銷（把「檔案暫時讀不到」誤判成「leader 被
    撤銷」會拿真實的平倉成本去回應一次 IO 打嗝）。

    執行中收到這個 → 受控收尾（見 LeaderWatch.refresh）。啟動時收到 → 拒絕啟動
    （繼承自基類的行為，本來就沒開倉，不需收尾）。
    """


@dataclass(frozen=True)
class LeaderResolution:
    """解析結果。address 一律正規化小寫（同基準比較，工程原則 1）。"""

    address: str
    source: str  # SOURCE_MANIFEST | SOURCE_ENV_DEFAULT | SOURCE_CUSTOMER_SIGNED
    # leader 種類（"standard"／"vault"，同 leaders.LeaderRef.kind）。引擎每輪
    # 按**本輪解析出的 kind** 套 vault 保護（apply_vault_policy）——kind 與
    # 白名單驗證出自同一份合併清單（同源，工程原則 1）。查不到條目（白名單
    # 缺檔的 env 回退豁免路徑）→ 維持 "standard"。
    kind: str = "standard"


def _find_ref(account_id: str | None, manifest_path: str | Path) -> FollowerRef | None:
    """從 manifest 找出自己的 FollowerRef。

    回 None（＝無 per-follower 設定，走 env 回退）的兩種情形，都刻意**不是**錯誤：
    - `account_id` 為 None：dry-run/shadow 不需要 SPARK_ACCOUNT_ID，本就沒有身分可查。
    - manifest 檔不存在：M1 單實例部署與本機開發從來沒有 manifest。

    manifest **存在但查無此 account_id** → raise：這是受管部署卻沒登錄自己，
    屬真實設定錯誤，不可降級成 env 回退（靜默降級＝繞過 per-follower 設定）。
    manifest 讀取/解析失敗 → raise：讀不到就不知道自己該跟誰，fail-fast。
    """
    if account_id is None:
        logger.info("未提供 account_id（dry/shadow 模式）——leader 走 env 預設")
        return None
    p = Path(manifest_path)
    if not p.exists():
        logger.warning("follower manifest 不存在（%s）——leader 走 env 預設", p)
        return None
    try:
        refs = load_followers(p)
    except (ValueError, OSError) as e:
        raise LeaderResolutionError(f"follower manifest 無法載入（{p}）: {e}") from e
    for r in refs:
        if r.account_id == account_id:
            return r
    raise LeaderResolutionError(
        f"follower manifest（{p}）內找不到 account_id={account_id!r}——"
        f"該 follower 尚未登錄或 manifest 被改動，拒絕以 env 預設降級跟單")


def _self_addresses(self_address: str, ref: FollowerRef | None) -> set[str]:
    """follower 自己的位址集合（env 的 SPARK_USER_ADDR ＋ manifest 登錄值）。

    兩者刻意都納入：正常情況兩者相同，不同就代表 env 與 manifest 已漂移——
    此時對**任一**方比中都該拒絕，比只挑一方比對安全。
    """
    out: set[str] = set()
    if self_address:
        # 格式壞掉就 raise：SPARK_USER_ADDR 不合法時，自我比對會靜默失效
        # （變成「沒有任何位址算自己」），等於這道閘門被無聲關掉。
        out.add(normalize_hex_address("self_address", self_address))
    if ref is not None:
        out.add(ref.user_address)
    return out


def resolve_leader(*, account_id: str | None, manifest_path: str | Path,
                   leaders_path: str | Path, env_default: str,
                   self_address: str = "",
                   user_leaders_path: str | Path | None = None) -> LeaderResolution:
    """解析本 follower 要跟的 leader，並在回傳前過白名單（純函式，只讀檔）。

    規則與威脅模型見模組 docstring。任何一條不通過即 raise LeaderResolutionError，
    **絕不回傳「降級」的結果**——呼叫端無從分辨降級與正常，故不提供該可能。
    """
    ref = _find_ref(account_id, manifest_path)
    leaders_file = Path(leaders_path)
    # ⭐ 驗證來源＝精選白名單 ＋ user registry 的**合併清單**（2026-07-27 自訂 leader
    # 自動准入）。述詞語義不變（is_still_permitted 仍只看 enabled）；合併優先序
    # （精選條目一律優先，撤銷不可被自訂路徑繞過）的單一定義在 user_leaders.merge_leaders。
    # registry 路徑由呼叫端**顯式接線**（由 FILET_EXCHANGE_DIR 導出，與 API 寫端共用
    # user_leaders_path_for 單一推導；接線見 run_copytrade.make_leader_resolver）。
    # ⚠️ 不由 leaders_path 推導 sibling（review F2）：那個佈局迫使 API 取得白名單
    # 目錄寫權。None（dry/shadow 無 exchange dir）＝無 registry，僅精選檔驗證。
    user_file = None if user_leaders_path is None else Path(user_leaders_path)
    # 「檔案不存在」與「明確的空清單」是兩種語意，只有前者享有 env 回退豁免。
    # ⭐ 缺席判準**只看精選檔**（2026-07-27 review F1）：精選檔缺失＝驗證來源不完整
    # →一律 transient，不論 registry 是否存在。舊版要求「兩檔皆缺」才算缺席，於是
    # 只缺精選檔時合併清單＝僅 user 條目，所有跟精選 leader 的 follower 會被誤判成
    # 撤銷（平倉＋鎖 kill switch）——「讀不到 ≠ 撤銷」（工程原則事故 #4 的判準），
    # 檔案層缺失絕不觸發安全動作。registry 缺失則維持合法空狀態（load 回空清單）。
    allowlist_absent = not leaders_file.exists()
    try:
        leaders = merge_leaders(
            load_leaders(leaders_file),
            load_user_leaders(user_file) if user_file is not None else [])
    except (ValueError, OSError) as e:
        # transient（含 user registry 損毀）：讀不到 ≠ 撤銷——不得升級成
        # LeaderRevokedError（把 IO 打嗝誤判成撤銷＝拿真實平倉成本回應一次檔案損毀）。
        raise LeaderResolutionError(
            f"leader 白名單無法載入（{leaders_file}／{user_file}）: {e}") from e

    if ref is not None and ref.leader_address is not None:
        candidate, source = ref.leader_address, SOURCE_MANIFEST
        if allowlist_absent:
            # transient：沒有白名單可比對 ≠ 白名單拒絕了他。明確指定卻驗不了照樣拒絕
            # （見規則 2），但**不是**撤銷——不得讓一個消失的檔案觸發全體收尾。
            raise LeaderResolutionError(
                f"manifest 指定的 leader {candidate} 無法驗證：白名單檔不存在"
                f"（{leaders_file}）——拒絕跟單。明確指定卻沒有白名單可驗，"
                f"正是被竄改後的樣子，不享有 env 回退豁免")
        if not is_still_permitted(candidate, leaders):
            raise LeaderRevokedError(
                f"manifest 指定的 leader {candidate} 不在白名單（{leaders_file}）或已被"
                f"撤銷（enabled=false／條目已移除）——拒絕跟單。manifest 明確指定卻無法"
                f"通過白名單，正是被竄改的樣子；要新增 leader 請由管理端編輯白名單檔，"
                f"不要繞過本檢查")
    else:
        source = SOURCE_ENV_DEFAULT
        try:
            candidate = normalize_hex_address("COPY_LEADER_ADDRESS", env_default)
        except ValueError as e:
            raise LeaderResolutionError(f"env 預設 leader 不合法: {e}") from e
        if allowlist_absent:
            logger.warning(
                "leader 白名單檔不存在（%s）——env 預設 leader %s 放行（向後相容："
                "既有部署尚未策劃白名單）；建檔後本路徑即恢復驗證",
                leaders_file, candidate)
        elif not is_still_permitted(candidate, leaders):
            raise LeaderRevokedError(
                f"env 預設 leader {candidate} 不在白名單（{leaders_file}）或已被撤銷"
                f"——拒絕跟單。預設值不因為是預設就豁免驗證（一致性）")

    if candidate in _self_addresses(self_address, ref):
        raise LeaderResolutionError(
            f"leader {candidate} 等於 follower 自己的位址——拒絕跟單"
            f"（自己跟自己無意義，且會形成回饋迴圈：本方下的單會在下一輪被當成"
            f"leader 目標再放大）")
    # kind 與上面的白名單驗證同一份合併清單（同源）。allowlist 缺檔的 env 回退
    # 豁免路徑查不到條目 → "standard"（無白名單即無 vault 標記可言）。
    ref_hit = find_leader(candidate, leaders)
    return LeaderResolution(candidate, source,
                            ref_hit.kind if ref_hit is not None else "standard")


class LeaderWatch:
    """啟動時已驗證的 leader ＋ 每輪重新解析的守門人。

    建構子收「啟動時已解析成功」的結果——啟動失敗不該走到這裡（呼叫端拒絕啟動），
    所以本類別**沒有**「還沒有 leader」的狀態，refresh() 永遠有可沿用的值。
    """

    def __init__(self, initial: LeaderResolution,
                 resolve: Callable[[], LeaderResolution], notifier: Notifier,
                 *, on_revoked: Callable[[], None] | None = None):
        """on_revoked：leader 被撤銷時的**受控收尾**機具（撤單→平倉→鎖死 kill switch）。

        刻意用注入而非在本模組裡直接呼叫 killswitch.trip：本模組是純檔案解析層，
        不該長出 exchange 依賴。實際接線見 scripts/run_copytrade.make_revocation_wind_down。
        為 None（dry/shadow、單元測試）→ 仍然停止交易，但不假裝收了尾（會 log）。
        """
        self.current = initial
        self._resolve = resolve
        self._notifier = notifier
        self._on_revoked = on_revoked

    def _critical(self, text: str, *, dedup_key: str) -> None:
        """發 critical，且**告警失敗不得升級成跟單中斷**。

        注入的 Notifier 由呼叫端提供（TelegramNotifier 自己吞例外，但 TaggedNotifier
        之類的包裝、或未來換的實作沒有這個保證）。原本 refresh() 的 except 區塊直接
        呼叫 notifier.critical()：那一行若拋例外，refresh() 就會 raise → main_loop
        累積連續錯誤計數 → SystemExit(1)。也就是「Telegram 掛了」會演變成「引擎停機、
        部位無人看管」，與本類別 docstring 宣稱的「不會 raise」直接矛盾。
        告警是觀測層，觀測層壞掉絕不能弄停被觀測的系統。
        """
        try:
            self._notifier.critical("leader", text, dedup_key=dedup_key)
        except Exception:  # noqa: BLE001 —— 見 docstring：告警失敗只 log
            logger.exception("leader critical 告警發送失敗（跟單不受影響）")

    def refresh(self) -> LeaderResolution | None:
        """重新解析並回傳本輪該用的 leader。**本函式不會 raise**——跟單不中斷。

        回傳 `None` ＝ **本輪起不得交易**（leader 已被撤銷，已執行受控收尾）。
        呼叫端必須把 None 當成「跳過本輪所有交易動作」，不得退化成沿用舊值。

        - **撤銷**（LeaderRevokedError）→ 受控收尾：critical（明說已撤銷、正在收尾）
          → 停止開新倉（回 None）→ flatten 現有部位 → trip kill switch。
        - **其他解析失敗** → critical ＋ 保留上一個已驗證 leader（原則 3：大聲，不吞）。
          刻意攔 `Exception` 而非只攔 LeaderResolutionError：此處是「引擎必須繼續
          管理已開部位」的安全關鍵路徑，任何未預期的例外（權限、IO、型別）都不該
          讓 leader 解析炸掉整個 cycle；代價是必須大聲——所以一律 critical＋log。
        - leader 位址變更 → critical（含舊/新/來源），這是有實際成本的重大事件。
        - 只有來源變（位址相同）→ 交易行為不變、無收斂成本，故只 log info 不吵。
        """
        try:
            new = self._resolve()
        except LeaderRevokedError as e:
            self._wind_down(e)
            return None
        except Exception as e:  # noqa: BLE001 —— 見 docstring：安全關鍵路徑，不得中斷跟單
            logger.error("leader 重新解析失敗，沿用 %s: %s", self.current.address, e,
                         exc_info=True)
            self._critical(
                f"leader 重新解析失敗（{e}）——**沿用上一個已驗證的 leader "
                f"{self.current.address}（來源 {self.current.source}）**，跟單不中斷；"
                f"請檢查 follower manifest 與 leader 白名單檔",
                dedup_key="leader_resolve_failed")
            return self.current
        if new.address != self.current.address:
            old = self.current
            self.current = new
            self._critical(
                f"**leader 已變更**：{old.address}（來源 {old.source}）→ "
                f"{new.address}（來源 {new.source}）——引擎將收斂到新 leader 的部位"
                f"（平掉舊部位、開新部位，有實際 taker 成本）",
                dedup_key=f"leader_changed:{old.address}->{new.address}")
        elif new.source != self.current.source:
            logger.info("leader 位址不變（%s），來源 %s → %s", new.address,
                        self.current.source, new.source)
            self.current = new
        return self.current

    def _wind_down(self, e: LeaderRevokedError) -> None:
        """leader 被撤銷 → 受控收尾。收尾失敗也絕不讓例外逃出 refresh()。

        `self.current` 刻意**不更新**：撤銷不是「換 leader」，沒有新的合法值可換；
        保留舊值讓後續每一輪都重新判定為撤銷、重新嘗試收尾（收尾第一次失敗時，
        下一輪還會再試——鎖不住／平不掉不會因為只喊一次就算了）。
        """
        logger.error("leader %s 已被白名單撤銷，開始受控收尾: %s",
                     self.current.address, e, exc_info=True)
        self._critical(
            f"**leader {self.current.address} 已被白名單撤銷**（{e}）——"
            f"**停止開新倉並受控收尾**：撤掉全部掛單、reduce-only 平掉現有部位、"
            f"鎖死 kill switch。跟單已終止，re-arm＝人工刪 ARM 檔",
            dedup_key=f"leader_revoked:{self.current.address}")
        if self._on_revoked is None:
            # 沒接收尾機具（dry/shadow 或單元測試）→ 停止交易，但不假裝收了尾。
            logger.error("未注入 on_revoked 收尾機具——僅停止交易，未平倉、未鎖 kill switch")
            return
        try:
            self._on_revoked()
        except Exception as ex:  # noqa: BLE001 —— 收尾失敗必須大聲，但不得炸掉 cycle
            logger.exception("leader 撤銷收尾失敗")
            self._critical(
                f"**leader 撤銷收尾失敗**（{ex!r}）——引擎已停止交易，但部位可能仍在、"
                f"kill switch 可能未鎖死，需**人工確認**；緊急全平："
                f"uv run python -m scripts.panic --yes",
                dedup_key=f"leader_revoke_wind_down_failed:{self.current.address}")
