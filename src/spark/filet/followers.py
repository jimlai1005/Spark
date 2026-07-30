"""src/spark/filet/followers.py
Follower 登錄：manifest（JSON）→ list[FollowerRef]。
FollowerRef 是跨 follower 工具（匯總、全域 panic）用的最小身分；
per-follower 完整跟單參數走各自進程的 env（CopySettings.from_env）。"""
import json
import re
from dataclasses import dataclass
from pathlib import Path

_NETWORKS = {"testnet", "mainnet"}
# 公開別名：activate／auto-activate 在寫 manifest **之前**的前置驗證用（單一定義；
# 2026-07-30 審查 F5——壞 network 條目一旦寫進 manifest，fail-fast 讀取端全體連坐）。
VALID_NETWORKS = _NETWORKS
_HEX = set("0123456789abcdefABCDEF")
_ACCOUNT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


@dataclass(frozen=True)
class FollowerRef:
    account_id: str
    user_address: str
    builder_address: str
    network: str
    label: str = ""
    # 該 follower 跟隨的 leader（正規化小寫）。None = 沿用進程 env 的
    # COPY_LEADER_ADDRESS——舊 manifest 沒有這個鍵，載入必須照常成功（向後相容）。
    # ⭐ 真相來源：manifest 是「這個 follower 跟誰」的唯一權威；引擎使用前必須再以
    # leaders.is_still_permitted 驗一次白名單（見 leaders.py 檔頭的資安論證與兩個
    # 旗標的分工；選擇時另有 is_selectable，兩者看的旗標不同）。
    leader_address: str | None = None


def validate_account_id(s: str) -> None:
    """account_id 縱深防禦（M2 Task 10）：僅允許 `[a-zA-Z0-9_-]`、長度 1-64。

    account_id 會流進檔案路徑（EnvFileKeyStore `<root>/<account_id>/agent.key`、
    狀態目錄 `FILET_STATE_BASE/<account_id>`、systemd `%i`）——目前由我方 manifest
    設定（非攻擊者可控），但 Phase C onboarding 後端將從使用者輸入生成 account_id。
    這裡把字元集鎖死，拒絕 `..`、`/`、`:`、空字串、超長，在該輸入變為使用者可控前
    先把載入邊界收斂。單一真相：envfile.py 直接 import 本函式使用（同一 regex，
    避免兩份定義漂移）。"""
    if not isinstance(s, str) or not _ACCOUNT_ID_RE.fullmatch(s):
        raise ValueError(f"account_id 不合法（僅允許英數字/_/-，長度 1-64）: {s!r}")


def _check_addr(field: str, value: str) -> None:
    ok = (isinstance(value, str) and value.startswith("0x") and len(value) == 42
          and all(c in _HEX for c in value[2:]))
    if not ok:
        raise ValueError(f"{field} 不是合法地址（0x + 40 hex）: {value!r}")


def normalize_hex_address(field: str, value: str) -> str:
    """校驗 0x + 40 hex 並回小寫正規化——位址比較的同一基準（工程原則 1）。

    刻意**不**重用 `spark.publicapi.config.normalize_address`：該模組已 import 本模組的
    `validate_account_id`（publicapi/config.py:13），反向 import 會造成循環相依。
    依賴方向是單向的：filet 是底層套件，publicapi 依賴 filet，filet 不回頭依賴 publicapi。
    兩份實作的規則相同（0x + 40 hex → lower）；此處是 filet 側的單一定義，
    filet.leaders 與本模組共用它，不再各寫一份。"""
    _check_addr(field, value)
    return value.lower()


def _parse_leader(i: int, f: dict) -> str | None:
    """manifest 的 leader_address 欄位 → 正規化位址或 None。

    向後相容：缺鍵、null、空字串一律視為「未指定」→ None（引擎沿用 env 的
    COPY_LEADER_ADDRESS）。舊 manifest 沒有這個鍵，載入不得因此失敗。
    「未指定」回退到的是管理端設定的 env 值，因此是安全預設；但只要有值，
    格式就必須合法，否則本條目算壞條目（fail-fast／進錯誤清單，沿既有慣例）。"""
    raw = f.get("leader_address")
    if raw is None or raw == "":
        return None
    return normalize_hex_address(f"followers[{i}].leader_address", raw)


def _parse_one(i: int, f: dict, seen: set[str]) -> FollowerRef:
    acct = f.get("account_id", "")
    try:
        validate_account_id(acct)
    except ValueError as e:
        raise ValueError(f"followers[{i}] {e}") from e
    if acct in seen:
        raise ValueError(f"followers[{i}] account_id 重複: {acct!r}")
    _check_addr(f"followers[{i}].user_address", f.get("user_address", ""))
    _check_addr(f"followers[{i}].builder_address", f.get("builder_address", ""))
    net = f.get("network", "")
    if net not in _NETWORKS:
        raise ValueError(f"followers[{i}].network 須為 {_NETWORKS}: {net!r}")
    # 位址小寫正規化（T6 reviewer Important）：以太坊位址大小寫不敏感，load
    # boundary 統一 canonical 化，避免北極星去重／跨 follower 比對因大小寫不同
    # 而重複計（dedup site 已於 c624d2e 修過，這是結構性的正解）。
    return FollowerRef(acct, f["user_address"].lower(), f["builder_address"].lower(),
                       net, f.get("label", ""), _parse_leader(i, f))


def load_followers(manifest_path: str | Path) -> list[FollowerRef]:
    """fail-fast：任一壞條目即 raise。一般載入用這個。"""
    path = Path(manifest_path)
    if not path.exists():
        raise FileNotFoundError(f"follower manifest 不存在: {path}")
    data = json.loads(path.read_text())
    refs: list[FollowerRef] = []
    seen: set[str] = set()
    for i, f in enumerate(data.get("followers", [])):
        ref = _parse_one(i, f, seen)
        seen.add(ref.account_id)
        refs.append(ref)
    return refs


def load_followers_tolerant(manifest_path: str | Path) -> tuple[list[FollowerRef], list[str]]:
    """容錯載入：壞條目跳過並收集錯誤訊息，回 (refs, errors)。
    災難工具（panic_all）與日報用這個——單一壞條目不該擋掉救其他 follower。"""
    path = Path(manifest_path)
    if not path.exists():
        raise FileNotFoundError(f"follower manifest 不存在: {path}")
    data = json.loads(path.read_text())
    refs: list[FollowerRef] = []
    errors: list[str] = []
    seen: set[str] = set()
    for i, f in enumerate(data.get("followers", [])):
        try:
            ref = _parse_one(i, f, seen)
        except ValueError as e:
            errors.append(str(e))
            continue
        seen.add(ref.account_id)
        refs.append(ref)
    return refs, errors
