"""scripts/set_follower_leader.py
管理端 CLI：更新 followers.json **既有**條目的 leader_address（預設 dry-run）。

為什麼需要這支獨立 CLI
----------------------
`scripts/filet_activate.py` 對名冊已有條目**一律拒絕**（見該檔「拒絕重複啟用」）——
它是「新增」流程，不是「更新」流程。客戶換 leader 時，名冊**刻意**不被 API／引擎
自動更新（`src/spark/filet/leader_change_apply.py` 檔頭的安全邊界：API 被打穿也
改不了「客戶的錢跟誰」），所以「把名冊既有條目的 leader 改對」只能是一支人工 CLI，
本檔就是它。與 filet_activate 同形：走同一個 safe_fs 寫入邊界、白名單判定重用
引擎（`leader_resolve.py`）同一套合併與述詞、預設 dry-run 保護誤操作。

用法:
  uv run python -m scripts.set_follower_leader --account-id <id> --leader 0x... \\
      [--manifest <路徑>] [--leaders <白名單絕對路徑>] [--user-leaders <registry 路徑>] \\
      [--exchange-dir <路徑>] [--allow-engine-mismatch] [--yes]

⚠️ `--manifest` 的預設值是 **CWD 相對**的（同 filet_activate 的告誡）：在錯的目錄跑
會讀寫到一份引擎讀不到的 manifest。正式部署請用絕對路徑或先 `cd /opt/filet/spark`。

⭐⭐ W2（2026-09-30 reviewer）：`--exchange-dir` 選填，給了就在寫入前讀一次引擎心跳
（`<exchange_dir>/engine/health/<account_id>.json`，與 `publicapi.ops`／`me_leader`
同一份讀法 `engine_health.read_heartbeat`，不另開第二種解析路徑）並印出
`status`／`leader.address`／`leader.source`。dry-run 只印心跳資訊，不做任何攔阻
（dry-run 本來就不寫入，沒有東西可攔）。**未給 `--exchange-dir` 時本段檢查整段跳過**
（沿用舊行為，正式機部署當下先用 dry-run 觀察）。給了 `--exchange-dir` 時，
`--yes` 依心跳 `status` 分三種情況（第二輪修正 C2／S3）：
- `ok` 且回報的 leader 與要寫入的新 leader **不同**（含「明確回報沒有 leader」，
  例如撤銷後——見 C2）→ 拒絕（exit 4）。撤銷後的重新啟用必須走客戶簽章流程
  （CLAUDE.md 紅線 5），本 CLI 不得繞過。
- `stale`／`unreadable`（S3）→ 一律拒絕（exit 4）：這兩種狀態代表「不知道引擎
  目前實際在跟誰」，不是「引擎沒在跑」，貿然寫入等於在看不到的情況下賭一把。
- `missing`（交換目錄從未有過這份心跳＝引擎從未啟動過）→ 放行，只印 status。

以上任一拒絕都可以用 `--allow-engine-mismatch` 明確承認風險後略過（例如確定
要繞過客戶簽章流程直接改 manifest）。

⭐ 白名單路徑沒有預設值（同 filet_activate／revoke_leader，2026-07-20 起的既有裁決）：
`--leaders` 或 env `FILET_LEADERS_PATH` 必須給一個絕對路徑，兩者皆無 → exit 2。

白名單判定用**引擎持續驗證用的述詞** `is_still_permitted`（只看 `enabled`，不看
`accepting_new`）：這是「已在跟的人改跟另一個 leader」的語意，跟 activate 的
「新客戶能不能選這個 leader」（`is_selectable`）不同——一個已下架但仍 enabled 的
leader，既有客戶手動切過去應該仍然放行（同引擎每輪重驗的邏輯一致）。合併精選
白名單與 user registry 的方式**照搬**引擎 `leader_resolve.resolve_leader` 的做法
（`user_leaders.merge_leaders`），不在這裡另寫一份合併邏輯（同源，工程原則 1）。

⭐ 本 CLI **不**重啟引擎、**不**碰 leader_changes.json／pending：引擎每輪重讀名冊，
名冊 leader 與帳本 `applied` 相同時不會產生變更事件（`LeaderWatch.refresh` 見
`leader_resolve.py`：leader 位址變更才 critical＋收斂部位）；auto-activate watcher
會在它自己下一輪跑到「重新啟用」分支時，因 manifest 現值已等於目標而回收
`leader_changes.json` 的舊記錄（`remove_satisfied_leader_change`，見
`scripts/filet_auto_activate.py:575-577` 的 `manifest_leader is not None and
leader == manifest_leader` 判斷）——本 CLI 把 manifest 改對之後，這段既有機制
自己會清乾淨，不需要本 CLI 插手。

不印任何私鑰／簽章欄位：manifest 本來就沒有這些欄位，本 CLI 也只碰
`leader_address` 一欄，其餘欄位逐字保留。

退出碼：0＝成功（含「新舊相同、無變更」）；2＝account_id 不存在於 manifest、
`--leader` 格式不合法、缺少白名單路徑，或 manifest／白名單／user registry
讀取失敗（壞 JSON 等）；3＝新 leader 未通過白名單判定；4＝給了
`--exchange-dir` 且未加 `--allow-engine-mismatch`，心跳 `status=="ok"` 但回報
的 leader 與要寫入的新 leader 不同（含明確回報沒有 leader，C2），或
`status` 為 `stale`／`unreadable`（S3，看不到引擎目前實際在跟誰）。
"""
import argparse
import json
import os
import pwd
import stat
import time
from pathlib import Path

from spark.filet.engine_health import heartbeat_path_for, read_heartbeat
from spark.filet.followers import load_followers, normalize_hex_address
# 白名單路徑的取得與檢查與引擎、filet_activate、revoke_leader 共用單一定義：
# CLI 驗一份檔、引擎驗另一份檔的漂移，會讓「已核可的 leader」與「引擎眼中合法的
# leader」悄悄分家（安全關鍵，見 leader_resolve.py 檔頭）。
from spark.filet.leader_resolve import LEADERS_PATH_ENV, require_leaders_path
from spark.filet.leaders import is_still_permitted, load_leaders
from spark.filet.safe_fs import write_json_atomic
from spark.filet.user_leaders import load_user_leaders, merge_leaders


def _fail(code: int, message: str) -> None:
    print(message)
    raise SystemExit(code)


def _print_manifest_ownership(manifest: Path) -> None:
    """S3（2026-09-30 reviewer）：dry-run 印名冊檔的 owner／mode——`os.replace`
    落地時會沿用「誰在跑這支 CLI」而不是原檔案的 owner，執行身分選錯會讓下一次
    引擎讀取（或另一支工具寫入）撞權限問題，最好在寫入前就看到。"""
    try:
        st = manifest.stat()
    except OSError as e:
        print(f"（無法讀取 manifest 檔案權限資訊: {e}）")
        return
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = f"uid={st.st_uid}"
    mode = stat.filemode(st.st_mode)
    print(f"manifest 檔案: owner={owner} mode={mode}")
    euid = os.geteuid()
    if euid != st.st_uid:
        try:
            euid_name = pwd.getpwuid(euid).pw_name
        except KeyError:
            euid_name = f"uid={euid}"
        print(f"⚠️ 目前執行身分（{euid_name}）與檔案 owner（{owner}）不同——"
              f"`--yes` 寫入（os.replace）後檔案 owner 會變成 {euid_name}，"
              f"可能讓引擎或其他工具之後讀寫撞權限問題")


def set_leader(account_id: str, leader_raw: str, manifest_path: str | Path, *,
               leaders_path: str, user_leaders_path: str | None,
               apply: bool, exchange_dir: str | Path | None = None,
               allow_engine_mismatch: bool = False,
               now_s: float | None = None) -> str:
    """更新 `manifest_path` 內既有 `account_id` 條目的 `leader_address`。

    `apply=False`（預設對應的行為）＝ dry-run：只印出判定結果，不寫檔。
    `apply=True` 才真的寫入（走 safe_fs 的 `write_json_atomic`，與 filet_activate
    同形），寫入後以 `load_followers` fail-fast 重讀驗證。

    終止路徑（皆 `raise SystemExit`，manifest 一律不被碰）：
    - account_id 不在 manifest（或 manifest 不存在）、`--leader` 格式不合法、
      manifest／白名單／user registry 讀取失敗（壞 JSON 等）→ exit 2。
    - 新 leader 未通過 `is_still_permitted`（合併精選白名單＋user registry）→ exit 3。
    - 新 leader 與舊 leader 相同（正規化後比較，同基準）→ 印「無變更」exit 0。
    - `apply=True` 且給了 `exchange_dir`、`allow_engine_mismatch` 為 False：
      心跳 `status=="ok"` 且回報的 leader 與 `new_leader` 不同（含明確回報沒有
      leader）→ exit 4（C2）；`status` 為 `stale`／`unreadable` → exit 4（S3，
      見檔頭）。

    成功（dry-run 或 apply）回傳一段人類可讀的訊息（供 main() 印出），不 raise。
    """
    manifest = Path(manifest_path)
    if not manifest.exists():
        _fail(2, f"manifest 不存在: {manifest}")
    try:
        data = json.loads(manifest.read_text())
    except (OSError, json.JSONDecodeError) as e:
        _fail(2, f"manifest 讀取失敗: {manifest} —— {e}")
    followers = data.get("followers", [])
    idx = next((i for i, f in enumerate(followers)
               if f.get("account_id") == account_id), None)
    if idx is None:
        _fail(2, f"account_id={account_id!r} 不存在於 manifest（{manifest}）—— "
                 f"本 CLI 只更新既有條目；新增 follower 請走 scripts.filet_activate")
    entry = followers[idx]

    try:
        new_leader = normalize_hex_address("--leader", leader_raw)
    except ValueError as e:
        _fail(2, f"leader 位址不合法: {leader_raw!r} —— {e}")

    old_leader_raw = entry.get("leader_address")
    old_leader = (normalize_hex_address("leader_address", old_leader_raw)
                 if old_leader_raw else None)

    print(f"account_id={account_id} user_address={entry.get('user_address')}")
    print(f"舊 leader: {old_leader or '(未指定，沿用 env 預設)'} → 新 leader: {new_leader}")

    if not apply:
        _print_manifest_ownership(manifest)

    # ⭐⭐ W2：與 `me_leader`／`publicapi.ops` 同一份心跳讀法（`read_heartbeat`
    # ＋`heartbeat_path_for`），不另開第二種解析路徑。dry-run 與 `--yes` 都印，
    # 攔阻（exit 4）只在 `apply=True` 時生效——見下方。
    hb = None
    hb_leader_address: str | None = None
    if exchange_dir is not None:
        hb = read_heartbeat(heartbeat_path_for(exchange_dir, account_id),
                            now_s if now_s is not None else time.time())
        hb_leader_source = None
        if hb.status == "ok":
            hb_leader_obj = hb.data.get("leader")
            if not isinstance(hb_leader_obj, dict):
                # `leader` 非 dict（心跳寫壞）→ 視同取不到位址：下方會因
                # address 為 None 而 exit 4（安全方向），不得 AttributeError 炸出 traceback。
                hb_leader_obj = {}
            hb_leader_raw = hb_leader_obj.get("address")
            hb_leader_source = hb_leader_obj.get("source")
            if isinstance(hb_leader_raw, str):
                try:
                    hb_leader_address = normalize_hex_address("engine_leader", hb_leader_raw)
                except ValueError:
                    hb_leader_address = None
        print(f"引擎心跳：status={hb.status} leader.address={hb_leader_address or '(none)'} "
              f"leader.source={hb_leader_source}")

    if old_leader == new_leader:
        _fail(0, "無變更：新 leader 與舊 leader 相同，未寫入")

    # ⭐ 合併方式照搬引擎 leader_resolve.resolve_leader：精選白名單 ＋ user
    # registry（同源，不在此另寫一份合併邏輯）。user_leaders_path 為 None（未給
    # `--user-leaders`）＝ 與空 registry 合併，效果等同只驗精選白名單。
    try:
        admissible = merge_leaders(
            load_leaders(leaders_path),
            load_user_leaders(user_leaders_path) if user_leaders_path is not None else [])
    except (OSError, ValueError) as e:
        registry_note = "" if user_leaders_path is None else f" / {user_leaders_path}"
        _fail(2, f"白名單或 user registry 讀取失敗（{leaders_path}{registry_note}）：{e}")
    if not is_still_permitted(new_leader, admissible):
        registry_note = ("" if user_leaders_path is None
                         else f" ∪ registry {user_leaders_path}")
        _fail(3, f"leader {new_leader} 不在准入集合（白名單 {leaders_path}"
                 f"{registry_note}）、已被撤銷或整筆不在合併清單內 —— 拒絕更新，"
                 f"manifest 未變動")
    print(f"白名單判定: {new_leader} 通過 is_still_permitted")

    # ⭐⭐ C2（2026-09-30 第二輪 reviewer）：舊版只在 `hb_leader_address is not
    # None` 時才核對——心跳 ok 但明確回報「沒有 leader」（撤銷後，`address`
    # 鍵存在且為 None）會整段跳過，等於繞過紅線 5「重新啟用必須走客戶簽章」：
    # 操作者可以直接用本 CLI 把一個已撤銷帳號的 manifest 改回某個 leader，
    # 不必經過簽章流程。撤銷後的 None 與「與新 leader 不同」都是「寫入會讓
    # 引擎判定 leader 變更」的同一種風險，都要擋，只是訊息不同。
    if apply and hb is not None and hb.status == "ok" and not allow_engine_mismatch:
        if hb_leader_address is None:
            _fail(4, "引擎目前沒有跟隨任何 leader（已撤銷或停止），重新啟用必須走客戶"
                     "簽章流程（CLAUDE.md 紅線 5）——本 CLI 不得繞過。若確定要用本 CLI "
                     "直接改 manifest，請明確加 --allow-engine-mismatch 承認風險。"
                     "manifest 未變動")
        if hb_leader_address is not None and hb_leader_address != new_leader:
            _fail(4, f"引擎心跳目前回報的 leader（{hb_leader_address}）與要寫入的新 leader"
                     f"（{new_leader}）不同——寫入會讓引擎判定 leader 變更並收斂部位，"
                     f"動真錢；若確定要換請走客戶簽章流程，或明確加 --allow-engine-mismatch "
                     f"略過本檢查。manifest 未變動")

    # ⭐ S3（2026-09-30 第二輪 reviewer）：心跳 `stale`／`unreadable` 時，我們
    # 完全不知道引擎目前實際在跟誰——舊版只核對 `status == "ok"`，讓這兩種
    # 情況直接放行，等於「看不到就當作安全」（危險方向的默認）。只有
    # `missing`（交換目錄從未有過這份心跳＝引擎從未啟動過）才代表「沒有
    # 正在跑的引擎需要擔心收斂」，放行並已在上面印出 status。
    if apply and hb is not None and hb.status in ("stale", "unreadable") and not allow_engine_mismatch:
        _fail(4, "引擎心跳過期或不可讀，無法確認寫入不會觸發收斂——引擎目前可能正在"
                 "跟隨某個 leader，貿然改 manifest 會讓它下一輪判定變更並收斂部位。"
                 "若確定要寫入，請明確加 --allow-engine-mismatch 承認風險。manifest 未變動")

    if not apply:
        return "dry-run：未寫入。確認無誤後加 --yes 才會真的寫入 manifest"

    followers[idx] = {**entry, "leader_address": new_leader}
    data["followers"] = followers
    # 走 safe_fs（唯一寫入邊界，與 filet_activate 同形）。mode 0644：引擎要讀。
    write_json_atomic(manifest, data, mode=0o644)
    load_followers(manifest)  # fail-fast 重讀驗證（不回滾——os.replace 已提交）
    return f"已寫入 {manifest}（account_id={account_id} 的 leader_address 已更新）"


def main() -> None:
    ap = argparse.ArgumentParser(
        description="管理端 CLI：更新 followers.json 既有條目的 leader_address（預設 dry-run）")
    ap.add_argument("--account-id", required=True)
    ap.add_argument("--leader", required=True,
                    help="新 leader 位址（須通過 is_still_permitted，見檔頭說明）")
    ap.add_argument("--manifest", default="var/filet/followers.json",
                    help="followers manifest 路徑（CWD 相對，正式部署請用絕對路徑）")
    ap.add_argument("--leaders", default=None,
                    help="策劃 leader 白名單路徑（絕對路徑；未給則取 env "
                         "FILET_LEADERS_PATH，兩者皆無則拒絕執行——無預設值）")
    ap.add_argument("--user-leaders", default=None,
                    help="user registry 路徑（給了才把自訂 leader 併入准入集合，"
                         "同引擎 resolve_leader 的合併方式；省略則只驗精選白名單）")
    ap.add_argument("--exchange-dir", default=None,
                    help="引擎交換目錄（選填；給了就在寫入前核對引擎心跳的目前 leader，"
                         "見檔頭 W2 說明）")
    ap.add_argument("--allow-engine-mismatch", action="store_true",
                    help="略過 --exchange-dir 的心跳核對（exit 4）——只在確定要繞過"
                         "客戶簽章流程直接改 manifest 時使用")
    ap.add_argument("--yes", action="store_true",
                    help="預設 dry-run，只印判定不寫檔；給這個旗標才真的寫入 manifest")
    args = ap.parse_args()
    try:
        leaders_path = require_leaders_path(
            {LEADERS_PATH_ENV: args.leaders} if args.leaders else os.environ)
    except ValueError as e:
        print(__doc__)
        print(f"{e}\n（本 CLI 也可用 --leaders <絕對路徑> 明確指定）")
        raise SystemExit(2) from e
    print(set_leader(args.account_id, args.leader, args.manifest,
                     leaders_path=leaders_path, user_leaders_path=args.user_leaders,
                     apply=args.yes, exchange_dir=args.exchange_dir,
                     allow_engine_mismatch=args.allow_engine_mismatch))


if __name__ == "__main__":
    main()
