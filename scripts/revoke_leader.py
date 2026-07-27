"""scripts/revoke_leader.py
operator 對某個 leader 執行**安全撤銷**的唯一正確做法（冪等，且自己驗收）。

為什麼要有這支 CLI（2026-07-27 Finding 2）
------------------------------------------
引擎驗的是**合併清單**：精選白名單優先，缺的位址才由 user registry 遞補
（`user_leaders.merge_leaders`）。於是 leaders.py 檔頭早期那條「把條目整筆刪掉
≡ enabled:false」的等價關係，在 registry 出現之後就**不再恆成立**——同一位址若
兩檔都有，刪掉精選那筆只是把 registry 那筆 `enabled:true` 放出來，`is_still_permitted`
從 False 翻回 True，撤銷靜默失效（follower 繼續跟一個已出事的 leader，而 operator
以為已經止血）。

修法刻意不是「在 RUNBOOK 再加一條提醒」（工程原則的 meta-rule：同一類 bug 連續穿過
數道審查時，問題不在少一條 checklist，而在缺一個不會用錯的工具）。本 CLI 把恆定有效
的撤銷做成一個動作：

1. **精選檔**：把該位址設成 `enabled:false`；沒有這筆就補一筆（`accepting_new` 一併
   設 false，name 標明是撤銷用）。精選條目在合併時一律優先 → 這一筆就是承重點。
2. **registry**：同位址存在時一併設 `enabled:false`（縱深防禦：日後若有人刪掉精選
   那筆，遞補上來的也是停用狀態）。read-modify-write 取 API 寫端**同一把 flock**
   （`registry_lock_path`），否則會被下一次 API 寫入拿舊快照無聲蓋掉。
3. **自我驗收**：重新載入兩檔 → 合併 → 斷言 `is_still_permitted(address) is False`，
   印出結論。任何一步失敗一律非零退出（`SystemExit`），絕不報 OK。

⚠️ 本 CLI 只負責「白名單狀態」。`enabled:false` 之後**引擎那邊是否真的收尾了**要另外
確認（RUNBOOK §5.5「下架一個 leader 時」小節：journalctl 找撤銷字樣、看
`killswitch.tripped` ARM 檔）——「我已下架＝已止血」正是這條紅線最早的事故成因。

⚠️ 以能讀寫兩個檔的身分執行（實機：root 或 `sudo -u filet-api`；精選檔的寫入需要
其**目錄**的寫權，這正是 filet-api 刻意沒有的權限，所以這是人工 CLI 不是 API 端點）。

用法:
  FILET_LEADERS_PATH=/opt/filet/spark/var/filet/leaders.json \\
  FILET_EXCHANGE_DIR=/var/lib/filet-exchange \\
  uv run python -m scripts.revoke_leader 0x<leader 位址>
  # 兩個路徑也可用 --leaders / --registry 明確指定（絕對路徑）
"""
import argparse
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from spark.filet.followers import normalize_hex_address
# 路徑取得與引擎／API 共用單一定義：CLI 撤銷 A 檔、引擎驗 B 檔的漂移，症狀正好是
# 「撤銷了但引擎照跟」——與本 CLI 要修的 bug 同一個形狀。
from spark.filet.leader_change_apply import resolve_user_leaders_path
from spark.filet.leader_resolve import LEADERS_PATH_ENV, require_leaders_path
from spark.filet.leaders import is_still_permitted, load_leaders
from spark.filet.user_leaders import (load_user_leaders, merge_leaders,
                                      registry_init_marker_path, registry_lock,
                                      registry_lock_path)

# 補進精選檔的撤銷條目長這樣（name 必須非空，load_leaders 硬性要求）。
REVOKED_NAME_PREFIX = "REVOKED"


@dataclass(frozen=True)
class RevokeResult:
    """撤銷結果。`changed=False` ＝ 兩檔本來就已是撤銷狀態（冪等重跑）。"""

    address: str
    curated_changed: bool
    registry_changed: bool
    registry_present: bool     # registry 裡本來就有這個位址嗎（供訊息用）

    @property
    def changed(self) -> bool:
        return self.curated_changed or self.registry_changed


def _atomic_write_json(path: Path, doc: dict) -> None:
    """temp + os.replace（同目錄 → 同檔案系統）。引擎每輪都在讀這兩個檔，絕不能讓它
    讀到半份 JSON——半份 JSON 會被 fail-fast 讀成「白名單壞掉」（全體 transient）。

    ⭐⭐ **保留原檔的 mode 與 owner**（2026-07-27 部署前抓到）。本工具由 operator 以
    **root** 執行，而它改寫的兩個檔平常是別的 user 在讀：
    - `leaders.json` 是 root:root **0644**，filet-api 與 filet-engine 都靠 other 位讀；
    - `user_leaders.json` 由 filet-api 建立（0644），引擎同樣靠 other 位讀。

    `os.replace` 換上去的是 **tmp 的 metadata**，而 mkstemp 建檔是 root:root **0600**。
    不保留的話，跑一次撤銷就把白名單變成只有 root 讀得到：API 的 `_load_leaders_or_503`
    → 503，引擎的 `load_leaders` → OSError → **transient → 沿用上一個已驗證的 leader**。
    也就是說——**這支工具會親手廢掉它自己要執行的那次撤銷**，而且自我驗收那一步是以
    root 身分讀檔，會照樣印出「已撤銷」。這是「安全動作靜默失效」最惡劣的形狀。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    prev = path.stat() if path.exists() else None
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + "-",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        if prev is not None:
            os.chmod(tmp, stat.S_IMODE(prev.st_mode))
            try:
                os.chown(tmp, prev.st_uid, prev.st_gid)
            except PermissionError:
                # 非 root 執行（開發機／測試）時 chown 不被允許——此時 owner 本來就
                # 是同一個人，不需要換。正式機以 root 跑，這一步一定成功。
                pass
        else:
            # 新建檔（該位址是第一筆、白名單檔尚不存在）：用與既有檔一致的 0644，
            # 理由同 user_leaders.record_user_leader 的註解（目錄 0750 才是防線，
            # 檔案 other 讀位是引擎唯一的讀取途徑——目錄沒有 setgid）。
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_raw_entries(path: Path) -> list:
    """已通過 load_leaders 驗證的檔 → 原始條目陣列（保留人工維護的欄位原樣）。"""
    if not path.exists():
        return []
    return json.loads(path.read_text()).get("leaders", [])


def _disable_in_place(entries: list, addr: str) -> tuple[list, bool]:
    """把 addr 的條目 `enabled` 翻成 false，其餘欄位原封不動。

    回 (新陣列, 是否有變動)。位址比較一律正規化（同基準，工程原則 1）——原始檔可能
    是 checksum 大小寫，字面比較會漏掉那一筆而讓撤銷靜默失敗。
    """
    out, changed = [], False
    for e in entries:
        try:
            same = normalize_hex_address("address", e.get("address", "")) == addr
        except ValueError:
            same = False       # 壞位址：load_leaders 已先驗過，理論上到不了這裡
        if same and e.get("enabled") is not False:
            out.append({**e, "enabled": False})
            changed = True
        else:
            out.append(e)
    return out, changed


def _revoke_curated(path: Path, addr: str) -> bool:
    """精選檔：確保存在一筆 `enabled:false` 的條目（沒有就補）。回傳是否有變動。

    ⭐ 這是承重的一步：精選條目在 merge 時一律優先，所以只要這一筆在，registry 那邊
    是什麼狀態都壓不過它。**不刪條目**——刪除正是失效的那條路。
    """
    try:
        load_leaders(path)     # fail-fast：壞檔不得被覆寫（沿 record_user_leader 政策）
    except ValueError as e:
        raise SystemExit(
            f"精選白名單載入失敗，拒絕覆寫（覆寫等於把整份白名單靜默清空＝全體撤銷）："
            f"{path}: {e}") from e
    entries = _load_raw_entries(path)
    updated, changed = _disable_in_place(entries, addr)
    if not any(_same_address(e, addr) for e in entries):
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        updated = updated + [{
            "address": addr,
            "name": f"{REVOKED_NAME_PREFIX} {addr}",
            "description": f"安全撤銷，scripts/revoke_leader.py 於 {stamp} 建立",
            "enabled": False,
            # 撤銷的位址當然也不收新客戶；兩個旗標都關掉，選單與引擎兩側一致。
            "accepting_new": False,
        }]
        changed = True
    if changed:
        _atomic_write_json(path, {"leaders": updated})
        load_leaders(path)     # 寫出去的東西必須通過自己的載入器（驗證但不回滾）
    return changed


def _same_address(entry: dict, addr: str) -> bool:
    try:
        return normalize_hex_address("address", entry.get("address", "")) == addr
    except ValueError:
        return False


def _revoke_registry(path: Path, addr: str) -> tuple[bool, bool]:
    """registry：同位址存在時一併 `enabled:false`。回 (是否有變動, 位址是否在檔內)。

    ⭐ 整段 read-modify-write 取 **API 寫端同一把 flock**（`registry_lock_path`）：
    不取的話，operator 的停用位會被下一次 `record_user_leader` 拿取鎖前的舊快照無聲
    蓋掉——掉的正是撤銷（模組 docstring 的 review F3）。有界等待逾時 → TimeoutError
    → 這裡轉成 SystemExit（大聲失敗，不是靜默略過）。

    ⭐ registry **從未建立**（檔、init-marker、lock 三者皆不存在）→ 完全不碰交換目錄：
    不 mkdir、不建 lock 檔。理由是權限——交換目錄是 `filet-api:filet-engine`，root
    在那裡建出的 lock 檔會變成 root 所有，之後 API 寫端可能開不了它（把一個撤銷動作
    變成 API 的寫入故障）。沒有 registry 就沒有要停用的條目，精選那筆已經夠了。
    """
    if not (path.exists() or registry_init_marker_path(path).exists()
            or registry_lock_path(path).exists()):
        return False, False
    try:
        with registry_lock(path):
            # 取得鎖之後才讀（鎖前的快照可能已被別的寫者換掉 → 拿它合併就是蓋寫）。
            load_user_leaders(path)          # fail-fast：壞檔／遺失 → 大聲失敗
            entries = _load_raw_entries(path)
            present = any(_same_address(e, addr) for e in entries)
            updated, changed = _disable_in_place(entries, addr)
            if changed:
                _atomic_write_json(path, {"leaders": updated})
                load_user_leaders(path)      # 寫出去的必須通過自己的載入器
    except (OSError, ValueError) as e:
        # ⚠️ registry 讀寫失敗**不能**當成「沒事」：引擎每輪也讀同一個檔，它載入不了
        # 就是 LeaderResolutionError（transient）→ 沿用上一個 leader ⇒ 撤銷不生效。
        # operator 必須看到這件事（工程原則 3：安全關鍵動作失敗要大聲）。
        raise SystemExit(
            f"user registry 處理失敗（{path}）：{e}\n"
            f"⚠️ 精選檔可能已改好，但引擎每輪也載入這個檔——它壞著就是 transient 失敗，"
            f"引擎會**沿用上一個 leader**，撤銷不會生效。請先修好 registry 再重跑本指令。"
        ) from e
    return changed, present


def revoke(address: str, *, leaders_path: str | Path,
           registry_path: str | Path) -> RevokeResult:
    """冪等撤銷 ＋ 自我驗收。任何一步不成立 → SystemExit（非零退出）。

    順序刻意是「精選 → registry → 驗收」：精選那筆一落地，合併結果就已經是撤銷
    （精選優先），所以即使 registry 那步失敗，撤銷本身也已成立——失敗訊息只需要
    告訴 operator「引擎可能讀不到 registry」，而不是「你的撤銷做了一半」。
    """
    try:
        addr = normalize_hex_address("address", address)
    except ValueError as e:
        raise SystemExit(f"位址不合法: {address!r} —— {e}") from e
    curated_p, registry_p = Path(leaders_path), Path(registry_path)
    curated_changed = _revoke_curated(curated_p, addr)
    registry_changed, registry_present = _revoke_registry(registry_p, addr)
    # ⭐ 自我驗收：**重新載入**兩檔（不是複用上面的記憶體狀態）並走引擎自己的述詞。
    # 「我寫了什麼」與「引擎會讀到什麼」必須同源比較（工程原則 1）——中間有原子換檔、
    # 有另一個寫者、有格式驗證，寫入成功不等於引擎眼中已撤銷。
    try:
        merged = merge_leaders(load_leaders(curated_p),
                               load_user_leaders(registry_p))
    except (OSError, ValueError) as e:
        raise SystemExit(f"撤銷後重新載入失敗，無法確認結果（{e}）"
                         f"—— 請人工檢查兩個檔") from e
    if is_still_permitted(addr, merged):
        raise SystemExit(
            f"⚠️ 撤銷未生效：合併後 is_still_permitted({addr}) 仍為 True。"
            f"精選={curated_p} registry={registry_p} —— 請人工檢查，不要當成已止血")
    return RevokeResult(addr, curated_changed, registry_changed, registry_present)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="安全撤銷一個 leader（精選檔 enabled:false ＋ registry 同步，冪等自驗）")
    ap.add_argument("address", help="要撤銷的 leader 位址（0x + 40 hex）")
    ap.add_argument("--leaders", default=None,
                    help="精選白名單路徑（絕對路徑；未給則取 env FILET_LEADERS_PATH）")
    ap.add_argument("--registry", default=None,
                    help="user registry 路徑（絕對路徑；未給則由 env FILET_EXCHANGE_DIR 推導）")
    args = ap.parse_args()
    try:
        leaders_path = require_leaders_path(
            {LEADERS_PATH_ENV: args.leaders} if args.leaders else os.environ)
    except ValueError as e:
        print(__doc__)
        raise SystemExit(f"{e}\n（本 CLI 也可用 --leaders <絕對路徑> 明確指定）") from e
    try:
        registry_path = args.registry or resolve_user_leaders_path(os.environ)
    except ValueError as e:
        print(__doc__)
        raise SystemExit(f"{e}\n（本 CLI 也可用 --registry <絕對路徑> 明確指定）") from e
    res = revoke(args.address, leaders_path=leaders_path, registry_path=registry_path)
    print(f"✅ 已撤銷 {res.address}")
    print(f"   精選白名單 {leaders_path}: "
          f"{'已寫入 enabled:false' if res.curated_changed else '本來就已是 enabled:false（冪等）'}")
    print(f"   user registry {registry_path}: "
          f"{'已寫入 enabled:false' if res.registry_changed else ('本來就已停用（冪等）' if res.registry_present else '無同位址條目，未改動')}")
    print(f"   ✅ 自我驗收：重載兩檔合併後 is_still_permitted({res.address}) = False")
    print("   ⚠️ 白名單狀態已撤銷 ≠ 已止血：請依 RUNBOOK §5.5「下架一個 leader 時」"
          "確認引擎真的收尾了（journalctl 找撤銷字樣、killswitch.tripped ARM 檔）。")


if __name__ == "__main__":
    main()
