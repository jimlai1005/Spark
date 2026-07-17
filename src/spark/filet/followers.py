"""src/spark/filet/followers.py
Follower 登錄：manifest（JSON）→ list[FollowerRef]。
FollowerRef 是跨 follower 工具（匯總、全域 panic）用的最小身分；
per-follower 完整跟單參數走各自進程的 env（CopySettings.from_env）。"""
import json
from dataclasses import dataclass
from pathlib import Path

_NETWORKS = {"testnet", "mainnet"}
_HEX = set("0123456789abcdefABCDEF")


@dataclass(frozen=True)
class FollowerRef:
    account_id: str
    user_address: str
    builder_address: str
    network: str
    label: str = ""


def _check_addr(field: str, value: str) -> None:
    ok = (isinstance(value, str) and value.startswith("0x") and len(value) == 42
          and all(c in _HEX for c in value[2:]))
    if not ok:
        raise ValueError(f"{field} 不是合法地址（0x + 40 hex）: {value!r}")


def _parse_one(i: int, f: dict, seen: set[str]) -> FollowerRef:
    acct = f.get("account_id", "")
    if not acct:
        raise ValueError(f"followers[{i}] account_id 不得為空")
    if acct in seen:
        raise ValueError(f"followers[{i}] account_id 重複: {acct!r}")
    _check_addr(f"followers[{i}].user_address", f.get("user_address", ""))
    _check_addr(f"followers[{i}].builder_address", f.get("builder_address", ""))
    net = f.get("network", "")
    if net not in _NETWORKS:
        raise ValueError(f"followers[{i}].network 須為 {_NETWORKS}: {net!r}")
    return FollowerRef(acct, f["user_address"], f["builder_address"],
                       net, f.get("label", ""))


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
