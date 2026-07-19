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
2. `ref.leader_address` 有值 → **必須通過 is_allowed_leader**，否則拒絕。
   白名單檔不存在時本路徑**一樣拒絕**：明確指定了 leader 卻沒有白名單可驗，
   正是被竄改後的樣子，不豁免。
3. `ref.leader_address` 為 None → 回退 env `COPY_LEADER_ADDRESS`；**env 預設也要
   過白名單**（一致性：不能因為是預設值就豁免）。⚠️ 唯一例外：白名單**檔案不存在**
   時放行並 log warning——否則所有既有部署（尚未策劃白名單）升級後立刻停擺。
   檔案存在但為空清單 = 管理端明確表態「目前沒有可選的 leader」→ 照樣拒絕
   （「不存在」與「明確空」是兩種語意，不可混為一談）。
4. leader **不得等於 follower 自己的位址**（自己跟自己無意義，且會形成回饋迴圈：
   本方下的單會被下一輪當成 leader 目標再放大）→ 拒絕。

失敗語意（工程原則 2、3）
----------------------
- **啟動時**解析失敗 → 呼叫端拒絕啟動（LeaderResolutionError 上拋），
  **不得靜默回退 env**：靜默回退正是攻擊者想要的降級路徑。
- **執行中**解析失敗（白名單檔壞掉、manifest 暫時讀不到、新 leader 不在白名單）
  → `LeaderWatch.refresh()` **保持使用上一個已驗證通過的 leader** ＋ 發 critical，
  **絕不**停止跟單、**絕不**靜默切換。理由：已在跑的部位需要有人繼續管理，
  停止跟單＝部位無人看管；靜默切換＝安全關鍵事件被吞掉（原則 3）。
- **leader 實際變更** → critical，訊息含舊 leader／新 leader／來源。換 leader 會讓
  引擎收斂到新 leader 的部位（平舊開新，有實際 taker 成本），屬重大事件必須留痕。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from spark.copytrade.notifier import Notifier
from spark.filet.followers import FollowerRef, load_followers, normalize_hex_address
from spark.filet.leaders import is_allowed_leader, load_leaders

logger = logging.getLogger(__name__)

DEFAULT_MANIFEST_PATH = "var/filet/followers.json"
DEFAULT_LEADERS_PATH = "var/filet/leaders.json"

SOURCE_MANIFEST = "manifest"
SOURCE_ENV_DEFAULT = "env_default"


class LeaderResolutionError(ValueError):
    """leader 解析失敗。啟動時＝拒絕啟動；執行中＝沿用上一個已驗證 leader＋critical。"""


@dataclass(frozen=True)
class LeaderResolution:
    """解析結果。address 一律正規化小寫（同基準比較，工程原則 1）。"""

    address: str
    source: str  # SOURCE_MANIFEST | SOURCE_ENV_DEFAULT


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
                   self_address: str = "") -> LeaderResolution:
    """解析本 follower 要跟的 leader，並在回傳前過白名單（純函式，只讀檔）。

    規則與威脅模型見模組 docstring。任何一條不通過即 raise LeaderResolutionError，
    **絕不回傳「降級」的結果**——呼叫端無從分辨降級與正常，故不提供該可能。
    """
    ref = _find_ref(account_id, manifest_path)
    leaders_file = Path(leaders_path)
    # 「檔案不存在」與「明確的空清單」是兩種語意，只有前者享有 env 回退豁免。
    allowlist_absent = not leaders_file.exists()
    try:
        leaders = load_leaders(leaders_file)
    except (ValueError, OSError) as e:
        raise LeaderResolutionError(f"leader 白名單無法載入（{leaders_file}）: {e}") from e

    if ref is not None and ref.leader_address is not None:
        candidate, source = ref.leader_address, SOURCE_MANIFEST
        if not is_allowed_leader(candidate, leaders):
            raise LeaderResolutionError(
                f"manifest 指定的 leader {candidate} 不在白名單（{leaders_file}）或已下架"
                f"——拒絕跟單。manifest 明確指定卻無法通過白名單，正是被竄改的樣子；"
                f"要新增 leader 請由管理端編輯白名單檔，不要繞過本檢查")
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
        elif not is_allowed_leader(candidate, leaders):
            raise LeaderResolutionError(
                f"env 預設 leader {candidate} 不在白名單（{leaders_file}）或已下架"
                f"——拒絕跟單。預設值不因為是預設就豁免驗證（一致性）")

    if candidate in _self_addresses(self_address, ref):
        raise LeaderResolutionError(
            f"leader {candidate} 等於 follower 自己的位址——拒絕跟單"
            f"（自己跟自己無意義，且會形成回饋迴圈：本方下的單會在下一輪被當成"
            f"leader 目標再放大）")
    return LeaderResolution(candidate, source)


class LeaderWatch:
    """啟動時已驗證的 leader ＋ 每輪重新解析的守門人。

    建構子收「啟動時已解析成功」的結果——啟動失敗不該走到這裡（呼叫端拒絕啟動），
    所以本類別**沒有**「還沒有 leader」的狀態，refresh() 永遠有可沿用的值。
    """

    def __init__(self, initial: LeaderResolution,
                 resolve: Callable[[], LeaderResolution], notifier: Notifier):
        self.current = initial
        self._resolve = resolve
        self._notifier = notifier

    def refresh(self) -> LeaderResolution:
        """重新解析並回傳本輪該用的 leader。**本函式不會 raise**——跟單不中斷。

        - 解析失敗 → critical ＋ 保留上一個已驗證 leader（原則 3：大聲，不吞）。
          刻意攔 `Exception` 而非只攔 LeaderResolutionError：此處是「引擎必須繼續
          管理已開部位」的安全關鍵路徑，任何未預期的例外（權限、IO、型別）都不該
          讓 leader 解析炸掉整個 cycle；代價是必須大聲——所以一律 critical＋log。
        - leader 位址變更 → critical（含舊/新/來源），這是有實際成本的重大事件。
        - 只有來源變（位址相同）→ 交易行為不變、無收斂成本，故只 log info 不吵。
        """
        try:
            new = self._resolve()
        except Exception as e:  # noqa: BLE001 —— 見 docstring：安全關鍵路徑，不得中斷跟單
            logger.error("leader 重新解析失敗，沿用 %s: %s", self.current.address, e,
                         exc_info=True)
            self._notifier.critical(
                "leader",
                f"leader 重新解析失敗（{e}）——**沿用上一個已驗證的 leader "
                f"{self.current.address}（來源 {self.current.source}）**，跟單不中斷；"
                f"請檢查 follower manifest 與 leader 白名單檔",
                dedup_key="leader_resolve_failed")
            return self.current
        if new.address != self.current.address:
            old = self.current
            self.current = new
            self._notifier.critical(
                "leader",
                f"**leader 已變更**：{old.address}（來源 {old.source}）→ "
                f"{new.address}（來源 {new.source}）——引擎將收斂到新 leader 的部位"
                f"（平掉舊部位、開新部位，有實際 taker 成本）",
                dedup_key=f"leader_changed:{old.address}->{new.address}")
        elif new.source != self.current.source:
            logger.info("leader 位址不變（%s），來源 %s → %s", new.address,
                        self.current.source, new.source)
            self.current = new
        return self.current
