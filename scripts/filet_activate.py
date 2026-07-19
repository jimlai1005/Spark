"""scripts/filet_activate.py
管理端人工核可 CLI（spec：activate 不做成 API 端點——對外 web 層若能拉 systemd 需提權，
被打穿即取得 unit 控制；危險 OS 動作收斂在人工 CLI）。
流程：讀 pending 條目 → 結構性核對 builder_address == FILET_BUILDER_ADDR（杜絕 web 層
被打穿後注入指向攻擊者的 builder 條目）→ 核對 leader 在策劃白名單內 → 寫入
followers.json（拒絕重複）→ 以 load_followers fail-fast 重讀驗證（驗證但不回滾：
os.replace 已提交，重讀失敗時 manifest 保留新版本、pending 條目也保留以便可重跑排查）
→ 自 pending 移除 → 印出（或 --start 執行）systemctl 啟動指令。
用法: FILET_BUILDER_ADDR=0x... uv run python -m scripts.filet_activate <account_id> \\
      [--pending var/filet/pending.json] [--manifest var/filet/followers.json] \\
      [--leader 0x...] [--leaders var/filet/leaders.json] [--start]
"""
import argparse
import json
import os
import subprocess
from pathlib import Path

from spark.filet.followers import load_followers
from spark.filet.leaders import is_allowed_leader, load_leaders
from spark.publicapi.config import normalize_address, derive_account_id
from spark.publicapi.pending import load_pending, remove_pending_entry

DEFAULT_LEADERS_PATH = "var/filet/leaders.json"


def _resolve_leader(entry: dict, override: str | None, leaders_path: str) -> str | None:
    """決定寫進 manifest 的 leader，並擋下不在白名單的地址。

    來源優先序：CLI --leader（管理端當下的明確指示）> pending 條目的 leader_address
    （客戶在 web 層選的）。兩者皆無 → None，引擎沿用 env COPY_LEADER_ADDRESS（向後相容）。

    ⭐ 白名單是硬閘：pending 條目由 filet-api 寫入，該進程被打穿即可在條目裡塞任意
    leader（瘋狂交易榨 builder fee／反向交易）。白名單檔只有管理端能寫，所以這裡
    的核對才是真正把關的一道——與 builder pin 核對同性質，都在寫 manifest **之前**做，
    失敗時 manifest 不被碰、pending 條目保留供調查。
    （引擎啟動時仍要再驗一次：本 CLI 驗過不代表 manifest 事後沒被改。）
    """
    raw = override or entry.get("leader_address")
    if not raw:
        return None
    try:
        leader = normalize_address(raw)
    except ValueError as e:
        raise SystemExit(f"leader 位址不合法: {raw!r} —— {e}") from e
    # 白名單檔不存在 → 空清單 → 任何指定的 leader 都被拒（安全預設：
    # 尚未策劃任何 leader 時，沒有人是可選的）。格式壞掉 → load_leaders fail-fast。
    if not is_allowed_leader(leader, load_leaders(leaders_path)):
        raise SystemExit(
            f"leader {leader} 不在策劃白名單（{leaders_path}）或已下架 —— 拒絕啟用。"
            f"要新增 leader 請由管理端編輯白名單檔，不要繞過本檢查")
    return leader


def activate(account_id: str, pending_path: str, manifest_path: str,
             expected_builder: str, *, start: bool, leader: str | None = None,
             leaders_path: str = DEFAULT_LEADERS_PATH) -> str:
    matches = [e for e in load_pending(pending_path)
               if e.get("account_id") == account_id]
    if not matches:
        raise SystemExit(f"pending 中找不到 account_id={account_id}")
    entry = matches[0]
    # ⭐ 結構性核對（紅線 6）：builder 必須等於部署設定常數；比對前同 normalize 基準。
    if normalize_address(entry["builder_address"]) != normalize_address(expected_builder):
        raise SystemExit(
            f"builder_address 不符！pending={entry['builder_address']} "
            f"期望={expected_builder} —— 條目可疑，拒絕啟用（條目保留供調查）")
    # ⭐ 結構性核對：account_id 必須是 user_address 衍生物。
    derived_account_id = derive_account_id(entry["user_address"])
    if account_id != derived_account_id:
        raise SystemExit(
            f"account_id 不符！pending={account_id} "
            f"衍生={derived_account_id} —— 條目可疑，拒絕啟用（條目保留供調查）")
    # ⭐ 白名單硬閘：在碰 manifest 之前擋掉，與上面兩道核對同一失敗語意。
    leader_address = _resolve_leader(entry, leader, leaders_path)
    manifest = Path(manifest_path)
    data = json.loads(manifest.read_text()) if manifest.exists() else {"followers": []}
    if any(f.get("account_id") == account_id for f in data["followers"]):
        raise SystemExit(f"{account_id} 已在 followers.json，拒絕重複啟用")
    data["followers"].append({
        "account_id": account_id,
        "user_address": normalize_address(entry["user_address"]),
        "builder_address": normalize_address(entry["builder_address"]),
        "network": entry["network"],
        "label": entry.get("label", ""),
        # None → JSON null；load_followers 視為未指定（引擎沿用 env 預設）。
        "leader_address": leader_address,
    })
    manifest.parent.mkdir(parents=True, exist_ok=True)
    tmp = manifest.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, manifest)
    load_followers(manifest)  # fail-fast 重讀驗證（不回滾——os.replace 已提交；寫壞
                              # 立刻大聲炸，manifest 留新版本、pending 條目留供重跑）
    remove_pending_entry(pending_path, account_id)
    cmd = f"systemctl start filet-follower@{account_id}"
    if start:
        subprocess.run(["systemctl", "start", f"filet-follower@{account_id}"],
                       check=True)
        return f"已寫入 manifest 並啟動: {cmd}"
    return f"已寫入 manifest。請人工啟動: {cmd}"


def main() -> None:
    ap = argparse.ArgumentParser(description="人工核可 pending follower 並寫入 manifest")
    ap.add_argument("account_id")
    ap.add_argument("--pending", default="var/filet/pending.json")
    ap.add_argument("--manifest", default="var/filet/followers.json")
    ap.add_argument("--start", action="store_true",
                    help="寫入後直接 systemctl start（預設只印指令）")
    ap.add_argument("--leader", default=None,
                    help="覆寫此 follower 跟隨的 leader（須在白名單內）；"
                         "省略則用 pending 條目的 leader_address，兩者皆無則沿用 env 預設")
    ap.add_argument("--leaders",
                    default=os.environ.get("FILET_LEADERS_PATH", DEFAULT_LEADERS_PATH),
                    help="策劃 leader 白名單路徑（預設取 env FILET_LEADERS_PATH）")
    args = ap.parse_args()
    builder = os.environ.get("FILET_BUILDER_ADDR")
    if not builder:
        print(__doc__)
        print("缺少環境變數 FILET_BUILDER_ADDR（核對 pending 條目的 builder 用）")
        raise SystemExit(2)
    print(activate(args.account_id, args.pending, args.manifest, builder,
                   start=args.start, leader=args.leader, leaders_path=args.leaders))


if __name__ == "__main__":
    main()
