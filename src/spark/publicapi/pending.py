"""src/spark/publicapi/pending.py
Pending follower 佇列（pending.json，filet-api 擁有）——與引擎 followers.json 刻意
分檔：web 層只寫 pending；followers.json 只由人工 activate CLI（管理端）寫。
權限拓撲：filet-api 對引擎 manifest 本就不該有寫權。條目的 user_address 綁 SIWE
session、builder_address 是伺服器常數（app 層保證；CLI 再核對一次）。"""
import json
from pathlib import Path

from spark.filet.followers import (VALID_NETWORKS, normalize_hex_address,
                                   validate_account_id)
from spark.filet.safe_fs import dir_owner_ids, write_json_atomic


def load_pending(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return json.loads(p.read_text()).get("pending", [])


def _atomic_write(p: Path, entries: list[dict]) -> None:
    # 兩個寫者（filet-api 與 root watcher）＋ filet-api 擁有的目錄 → 走 safe_fs
    # （symlink 提權，見該檔頭）。root 寫完把 owner 還給 filet-api，否則它下次寫不動。
    # read-modify-write 的極窄競態（雙方快照互蓋）已知且自癒，見 filet_auto_activate 檔頭。
    write_json_atomic(p, {"pending": entries}, mode=0o640,
                      owner_ids=dir_owner_ids(p.parent))


def write_pending_entry(path: str | Path, *, account_id: str, user_address: str,
                        builder_address: str, network: str, agent_address: str,
                        label: str = "", leader_address: str | None = None) -> None:
    """冪等：同 account_id 已在佇列即 no-op。寫入前驗證（縱深防禦）。

    leader_address（選填）＝客戶在 web 層選的 leader。**有值才寫入該鍵**——沿既有
    慣例保持向後相容：舊條目不長出這個鍵，activate CLI 以 `entry.get("leader_address")`
    讀取，缺鍵＝未指定＝引擎沿用 env `COPY_LEADER_ADDRESS`。空字串同樣視為未指定
    （與 followers._parse_leader 同一慣例，避免兩處對「空」的判定漂移）。

    **這裡只驗格式，不驗白名單——分工是刻意的**：pending.json 由 filet-api 進程
    擁有並寫入，該進程若被打穿，攻擊者就在這個函式的**呼叫端之內**，任何在此做的
    白名單檢查他都能繞過（直接改 pending.json 即可）。真正擋得住的閘門必須放在
    攻擊者寫不到的那一側，共兩道：(1) 人工 activate CLI（scripts/filet_activate.py
    的 _resolve_leader，寫 manifest 前核對白名單）、(2) 引擎啟動與每輪的二次驗證
    （spark.filet.leader_resolve.resolve_leader）。在此重複一次不增加安全性，
    只會製造「寫入時已驗過」的假安全感，誘使下游省略真正有效的那兩道。
    格式驗證則保留：壞格式的位址進了佇列會讓後續每一步都炸，早驗早報。
    """
    validate_account_id(account_id)
    if network not in VALID_NETWORKS:
        raise ValueError(f"network 須為 {VALID_NETWORKS}: {network!r}")
    leader = (normalize_hex_address("leader_address", leader_address)
              if leader_address else None)
    p = Path(path)
    entries = load_pending(p)
    if any(e.get("account_id") == account_id for e in entries):
        return
    entry = {"account_id": account_id, "user_address": user_address,
             "builder_address": builder_address, "network": network,
             "agent_address": agent_address, "label": label}
    if leader is not None:
        entry["leader_address"] = leader
    entries.append(entry)
    _atomic_write(p, entries)


# ⚠️ 這裡曾有一個 `set_pending_risk`（把風控偏好寫進 pending 條目），2026-07-30
# 隨「客戶簽章的風控設定」上線後移除。理由是**單一來源**：風控意圖現在住在
# `filet/risk_settings.py` 的簽章記錄裡（API 寫、引擎每輪讀並重新驗章），pending
# 條目再存一份就會與它漂移，而漂移的那一份沒有簽章、卻長得同樣權威。
# 要改風控 → `POST /api/me/risk`（見 app.py 該節）。


def remove_pending_entry(path: str | Path, account_id: str) -> None:
    p = Path(path)
    _atomic_write(p, [e for e in load_pending(p)
                      if e.get("account_id") != account_id])
