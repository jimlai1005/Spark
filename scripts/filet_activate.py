"""scripts/filet_activate.py
管理端人工核可 CLI（spec：activate 不做成 API 端點——對外 web 層若能拉 systemd 需提權，
被打穿即取得 unit 控制；危險 OS 動作收斂在人工 CLI）。
流程：讀 pending 條目 → 結構性核對 builder_address == FILET_BUILDER_ADDR（杜絕 web 層
被打穿後注入指向攻擊者的 builder 條目）→ 寫入 followers.json（拒絕重複）→ 以
load_followers fail-fast 重讀驗證（驗證但不回滾：os.replace 已提交，重讀失敗時
manifest 保留新版本、pending 條目也保留以便可重跑排查）→ 自 pending 移除 → 印出
（或 --start 執行）systemctl 啟動指令。
用法: FILET_BUILDER_ADDR=0x... uv run python -m scripts.filet_activate <account_id> \\
      [--pending var/filet/pending.json] [--manifest var/filet/followers.json] [--start]
"""
import argparse
import json
import os
import subprocess
from pathlib import Path

from spark.filet.followers import load_followers
from spark.publicapi.config import normalize_address
from spark.publicapi.pending import load_pending, remove_pending_entry


def activate(account_id: str, pending_path: str, manifest_path: str,
             expected_builder: str, *, start: bool) -> str:
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
    args = ap.parse_args()
    builder = os.environ.get("FILET_BUILDER_ADDR")
    if not builder:
        print(__doc__)
        print("缺少環境變數 FILET_BUILDER_ADDR（核對 pending 條目的 builder 用）")
        raise SystemExit(2)
    print(activate(args.account_id, args.pending, args.manifest, builder,
                   start=args.start))


if __name__ == "__main__":
    main()
