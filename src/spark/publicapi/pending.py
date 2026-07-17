"""src/spark/publicapi/pending.py
Pending follower 佇列（pending.json，filet-api 擁有）——與引擎 followers.json 刻意
分檔：web 層只寫 pending；followers.json 只由人工 activate CLI（管理端）寫。
權限拓撲：filet-api 對引擎 manifest 本就不該有寫權。條目的 user_address 綁 SIWE
session、builder_address 是伺服器常數（app 層保證；CLI 再核對一次）。"""
import json
import os
from pathlib import Path

from spark.filet.followers import validate_account_id

_NETWORKS = {"testnet", "mainnet"}


def load_pending(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return json.loads(p.read_text()).get("pending", [])


def _atomic_write(p: Path, entries: list[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"pending": entries}, indent=2))
    os.replace(tmp, p)  # 原子換檔，不留半寫


def write_pending_entry(path: str | Path, *, account_id: str, user_address: str,
                        builder_address: str, network: str, agent_address: str,
                        label: str = "") -> None:
    """冪等：同 account_id 已在佇列即 no-op。寫入前驗證（縱深防禦）。"""
    validate_account_id(account_id)
    if network not in _NETWORKS:
        raise ValueError(f"network 須為 {_NETWORKS}: {network!r}")
    p = Path(path)
    entries = load_pending(p)
    if any(e.get("account_id") == account_id for e in entries):
        return
    entries.append({"account_id": account_id, "user_address": user_address,
                    "builder_address": builder_address, "network": network,
                    "agent_address": agent_address, "label": label})
    _atomic_write(p, entries)


def remove_pending_entry(path: str | Path, account_id: str) -> None:
    p = Path(path)
    _atomic_write(p, [e for e in load_pending(p)
                      if e.get("account_id") != account_id])
