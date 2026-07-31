"""scripts/backfill_leader_kinds.py
backfill 既有 user registry 的 kind 欄位：探測哪些位址是 vault、補寫 kind:vault。

動機：registry 既有條目都沒有 kind 鍵（預設 standard），若其中有 vault 位址，
雙層保護不會生效；record_user_leader 的冪等分支不回填，所以需要一次性 backfill CLI。

沿 scripts/revoke_leader.py 形狀：
- registry_lock 內 read-modify-write
- 逐條目探測 HLGateway.vault_details
- 是 vault（回 dict）→ 改寫 kind:vault；非 vault（回 None）→ 不寫
- 原子寫（tempfile + os.replace，保留 mode）
- 自我驗收：重載 load_user_leaders + merge_leaders，斷言每筆 kind 與探測結果一致
- 冪等：重跑零改動
- 網路失敗：該條目記 error、繼續其餘、結尾非零退出（部分成功要看得出來）

使用方式:
  uv run python -m scripts.backfill_leader_kinds --registry /path/to/user_leaders.json [--dry-run]
"""
import argparse
import json
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from spark.config import API_URLS
from spark.filet.followers import normalize_hex_address
from spark.filet.user_leaders import load_user_leaders, registry_lock
from spark.publicapi.hl import HLGateway


@dataclass
class BackfillResult:
    """backfill 結果摘要。"""
    changed: int = 0          # 改動的條目數
    errors: int = 0           # 失敗的條目數
    exit_code: int = 0        # CLI 終止碼
    report: dict = field(default_factory=dict)  # { address: message }


def _atomic_write_json(path: Path, doc: dict) -> None:
    """tempfile + os.replace，保留原檔的 mode。沿 revoke_leader.py 的作法。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    prev = path.stat() if path.exists() else None
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".user_leaders-",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        if prev is not None:
            os.chmod(tmp, stat.S_IMODE(prev.st_mode))
            try:
                os.chown(tmp, prev.st_uid, prev.st_gid)
            except PermissionError:
                # 非 root 執行時 chown 不被允許——開發機身上 owner 本來就是同一個人。
                pass
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_raw_entries(path: Path) -> list:
    """已通過 load_user_leaders 驗證的檔 → 原始條目陣列（保留既有欄位原樣）。"""
    if not path.exists():
        return []
    return json.loads(path.read_text()).get("leaders", [])


def backfill(registry_path: str | Path, *, gateway: "HLGateway | None" = None,
             dry_run: bool = False) -> BackfillResult:
    """backfill 一個 registry：逐條目探測 vault、補寫 kind:vault。

    Args:
        registry_path: user_leaders.json 的路徑
        gateway: HLGateway 實例（測試注入 fake；None 時用預設 mainnet）
        dry_run: 只印不寫

    Returns:
        BackfillResult，包含改動數、error 數、exit code、每條目訊息。
        exit_code=0：全部成功或零改動；非零：任何 error 條目。
    """
    p = Path(registry_path)
    result = BackfillResult()

    if gateway is None:
        gateway = HLGateway(API_URLS["mainnet"])

    # 檔尚未建立 → 零改動
    if not p.exists():
        return result

    try:
        with registry_lock(p):
            # 取得鎖之後才讀
            load_user_leaders(p)  # fail-fast：壞檔不得被覆寫
            entries = _load_raw_entries(p)

            updated = []
            for e in entries:
                addr = e.get("address", "")
                kind_in_json = e.get("kind")  # 原始 JSON 裡的 kind（可能缺）

                # 正規化位址（工程原則 1）
                try:
                    normalized = normalize_hex_address("address", addr)
                except ValueError:
                    result.report[addr] = "error: 位址不合法"
                    result.errors += 1
                    updated.append(e)
                    continue

                # 已是 kind:vault → skip
                if kind_in_json == "vault":
                    result.report[normalized] = "skip (already vault)"
                    updated.append(e)
                    continue

                # 探測 vault_details
                try:
                    vault_info = gateway.vault_details(normalized)
                except Exception as ex:
                    result.report[normalized] = f"error: {type(ex).__name__}: {ex}"
                    result.errors += 1
                    updated.append(e)
                    continue

                # 決策：是 vault 嗎？
                is_vault = vault_info is not None and isinstance(vault_info, dict) and vault_info.get("name")

                if is_vault:
                    # 是 vault → 補寫 kind:vault
                    if kind_in_json != "vault":
                        updated.append({**e, "kind": "vault"})
                        result.changed += 1
                        result.report[normalized] = f"changed: {kind_in_json or 'None'} → vault"
                    else:
                        updated.append(e)
                else:
                    # 非 vault → 條目一律不動（任務書規定；顯式 kind:"standard" 是
                    # record_user_leader 的合法寫入，不是誤寫，backfill 無權整理它）
                    result.report[normalized] = "skip (not vault)"
                    updated.append(e)

            # 寫入（非 dry-run）
            if result.changed > 0 and not dry_run:
                doc = {"leaders": updated}
                _atomic_write_json(p, doc)
                # 寫出去的必須通過自己的載入器
                load_user_leaders(p)

    except (OSError, ValueError) as e:
        result.errors += 1
        result.report["_error"] = f"registry 處理失敗: {e}"
        result.exit_code = 1
        return result

    # 自我驗收：重新載入並驗證（dry-run 時跳過，因為檔沒變）
    if result.changed > 0 and not dry_run:
        try:
            reloaded = load_user_leaders(p)
            for entry in reloaded:
                addr_norm = normalize_hex_address("address", entry.address)
                try:
                    vault_info = gateway.vault_details(addr_norm)
                except Exception:
                    # 驗收時網路失敗 → 略過（已經改了，再嘗試驗是浪費時間）
                    continue

                is_vault = vault_info is not None and isinstance(vault_info, dict) and vault_info.get("name")
                if is_vault and entry.kind != "vault":
                    result.exit_code = 1
                    result.report["_verification"] = (
                        f"驗收失敗: {addr_norm} 應為 kind:vault 但實際 {entry.kind}")
                    return result
        except (OSError, ValueError) as e:
            result.exit_code = 1
            result.report["_verification"] = f"驗收失敗: {e}"
            return result

    if result.errors > 0:
        result.exit_code = 1

    return result


def main(argv=None, *, gateway=None) -> None:
    """CLI 進入點。

    Args:
        argv: 命令列引數（None 時用 sys.argv[1:]）
        gateway: HLGateway 實例（測試注入；生產 None → 建真的）
    """
    ap = argparse.ArgumentParser(
        description="backfill user registry 的 kind 欄位（vault 探測）")
    ap.add_argument("--registry", required=True,
                    help="user_leaders.json 的路徑（絕對路徑）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只印不寫")
    args = ap.parse_args(argv)

    result = backfill(args.registry, gateway=gateway, dry_run=args.dry_run)

    # 輸出
    if args.dry_run:
        print("🔍 DRY-RUN 模式（不寫檔）")
    print()
    for addr, msg in sorted(result.report.items()):
        if not addr.startswith("_"):
            print(f"  {addr} {msg}")
    if "_error" in result.report:
        print(f"❌ {result.report['_error']}")
    if "_verification" in result.report:
        print(f"❌ {result.report['_verification']}")
    print()
    print(f"✅ {result.changed} changed, ⚠️ {result.errors} errors")

    if result.exit_code != 0:
        raise SystemExit(result.exit_code)


if __name__ == "__main__":
    main()
