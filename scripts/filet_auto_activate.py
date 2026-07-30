"""scripts/filet_auto_activate.py
全自動啟用 watcher（2026-07-30 使用者裁決：產品僅內部使用，移除人工審核；
CLAUDE.md 紅線 5 之例外條款即本路徑）。由 systemd timer 以特權身分定期執行一次性掃描：

    pending.json 條目 ∩ 該用戶已簽章選定 leader  →  自動啟用

每個符合條件的條目依序：
  1. 結構性核對（validate_pending_entry：builder pin／account_id 衍生／network 合法）
     ——⭐ 在驗章與任何 root 檔案動作**之前**（opus 審查 F6：user_address 來自
     filet-api 可寫的 pending，必須先用 derive_account_id 綁定 account↔address，
     驗章的可信 user_address 才成立；也順帶杜絕以偽條目誘使 root 建檔）。
  2. 重驗用戶簽章（verify_leader_change）——拆掉人工審核後的結構性替代防線：
     pending、registry、leader_changes 三個檔 filet-api 都寫得到，被打穿即可全部偽造，
     唯獨**用戶私鑰簽出的 leader 選擇**偽造不了。沒有驗章通過的選擇＝不啟用。
  3. 組 per-follower env（範本＋自動代入 SPARK_NETWORK/ACCOUNT_ID/USER_ADDR/
     BUILDER_ADDR——這四個是引擎啟動必填，見 run_copytrade 的 env 檢查；審查 F1）、
     建 state 目錄。範本殘留 REPLACE_ 佔位或含 SPARK_ 欄位 → 整輪拒跑（fail-closed）。
  4. activate(remove_pending=False)（與人工 CLI 同一實作）→ systemctl start →
     記 watcher state（started）→ 最後才清 pending（審查 F2：start 失敗時條目
     必須還在，下一輪才有東西可重試）。

watcher state 檔（--state-file，root 私有）記錄「哪些帳號是**本 watcher** 啟動的」：
  - 只有標記為本 watcher 啟動中（starting）的帳號，補救路徑才會重試 systemctl start；
  - 已標記 started 的帳號**絕不**自動重啟（審查 F3：`systemctl stop` 是 RUNBOOK 的
    正式人工處置，用戶重按「完成綁定」產生的新 pending 條目不得復活被停掉的引擎）；
  - manifest 有但 state 無（人工 CLI 啟用的帳號）同樣不碰引擎，只清佇列＋告警。

告警走 Notifier 注入（專案慣例）：啟用失敗 critical、佇列異常 warn，dedup 由
notifier 處理；journal 的 INFO 只在條目狀態轉換時記一次（審查 F4、F9）。

freshness／nonce 刻意放行（now_s 取記錄自身 issued_at、consume_nonce 恆真）：
activation 只問「這位用戶是否真的簽過要跟這個 leader」；引擎啟動後對同一筆記錄
的處理是「leader 相同 → 忽略不告警」（leader_change_apply 檔頭），不會產生假告警。
殘餘風險（審查 F7B，已接受）：簽章選擇不過期——被打穿的 API 可為「曾簽過選擇但
從未被本 watcher 啟動」的用戶重造 pending 使其被啟用；started 標記擋掉重啟情境，
且 leader 仍限於用戶自己簽過的那一個。

用法（RUNBOOK §5.6a）：
    FILET_BUILDER_ADDR=0x... FILET_LEADERS_PATH=/abs/leaders.json \\
    python -m scripts.filet_auto_activate \\
        --pending /var/lib/filet-api/pending.json \\
        --manifest /opt/filet/spark/var/filet/followers.json \\
        --exchange-dir /var/lib/filet-exchange \\
        --env-template /etc/filet/follower.env.template \\
        --env-dir /etc/filet/followers \\
        --state-base /opt/filet/state \\
        --state-file /var/lib/filet-auto-activate/state.json
"""
import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from scripts.filet_activate import activate, validate_pending_entry
from spark.copytrade.notifier import Notifier, NullNotifier, TelegramNotifier
from spark.filet.followers import load_followers_tolerant
from spark.filet.leader_change import (LeaderChangeError, leader_changes_path_for,
                                       load_leader_changes, parse_issued_at,
                                       remove_satisfied_leader_change,
                                       verify_leader_change)
from spark.filet.leader_resolve import require_leaders_path
from spark.filet.safe_fs import (ensure_dir_secure, named_owner_ids,
                                 write_json_atomic, write_text_atomic)
from spark.filet.user_leaders import user_leaders_path_for
from spark.publicapi.pending import load_pending, remove_pending_entry

logger = logging.getLogger("filet.auto_activate")

# 檔案權限：與 RUNBOOK §「檔案與權限拓撲」一致（env 640、state 700，owner 皆引擎戶）。
ENV_FILE_MODE = 0o640
STATE_DIR_MODE = 0o700

# 引擎啟動必填的 per-follower 變數（run_copytrade：缺前兩者 exit 2；live 模式缺
# ACCOUNT_ID 也 exit 2；NETWORK 不填會**靜默預設 testnet**——LIVE=true 配 testnet
# 正是審查 F1 指出的靜默壞局，所以四個全列必填、由本 watcher 代入，不留給範本）。
GENERATED_KEYS = ("SPARK_NETWORK", "SPARK_ACCOUNT_ID",
                  "SPARK_USER_ADDR", "SPARK_BUILDER_ADDR")


def _latest_signed_leader(records: list[dict], *, account_id: str,
                          user_address: str) -> str | None:
    """回傳該帳號**驗章通過**的 leader 選擇；沒有 → None（尚未選）。

    記錄檔的合約是每帳號單筆覆蓋（write_leader_change：「當前意圖」而非流水帳），
    所以正常情況 mine 至多一筆：驗章過就用、被竄改／偽造就視同未選（fail-closed，
    攻擊者塞不進自己的 leader）。仍以反向迴圈寫（防手改檔案出現多筆時取最新）。
    ⚠️ user_address 必須已通過 validate_pending_entry 的 derive 綁定（呼叫端負責）。
    """
    mine = [r for r in records if r.get("account_id") == account_id]
    for rec in reversed(mine):
        try:
            verified = verify_leader_change(
                rec, account_id=account_id, user_address=user_address,
                # freshness／nonce 刻意放行（檔頭說明）；簽章與 account/leader 綁定照驗。
                now_s=parse_issued_at(rec.get("issued_at")).timestamp(),
                consume_nonce=lambda n: True)
        except LeaderChangeError as e:
            # 只帶機器可讀 reason，不帶記錄內容（紅線：簽章材料不進 log）。
            logger.warning("略過一筆驗章失敗的 leader 選擇 account=%s reason=%s",
                           account_id, getattr(e, "reason", "unknown"))
            continue
        return verified.leader_address
    return None


def _compose_env(template_path: Path, *, network: str, account_id: str,
                 user_address: str, builder: str) -> str:
    """範本＋watcher 代入的 per-follower 區塊。兩類錯誤整輪 fail-closed：
    - 殘留 REPLACE_WITH：部署者還沒完成安裝，用半成品設定開實盤是拿真錢冒險；
    - 範本自帶 SPARK_*：會與代入區塊重複定義，哪個生效取決於 EnvironmentFile
      的載入細節——歧義本身就是錯，直接拒收。
    """
    text = template_path.read_text()
    # ⭐ 只檢查**非註解行**（2026-07-30 實機部署踩到）：範本與部署者的註解本來就會
    # 提到「REPLACE_WITH」與 SPARK_* 這些字樣（說明它們的存在正是註解的職責），
    # 全文子字串比對會讓 watcher 被自己的說明文字永久 fail-closed。
    value_lines = [ln for ln in text.splitlines()
                   if ln.strip() and not ln.lstrip().startswith("#")]
    if any("REPLACE_WITH" in ln for ln in value_lines):
        raise SystemExit(
            f"env 範本 {template_path} 仍有 REPLACE_WITH 佔位符未填——"
            f"拒絕啟用任何 follower。請先完成 RUNBOOK §5.6a 的範本設定。")
    for key in GENERATED_KEYS:
        if any(ln.split("=", 1)[0].strip() == key for ln in value_lines):
            raise SystemExit(
                f"env 範本 {template_path} 不得設定 {key}（由 watcher 逐用戶代入）"
                f"——拒絕啟用任何 follower。")
    block = ("\n# --- 以下由 auto-activate watcher 逐用戶代入（勿手改；"
             "要改風險參數改上面的範本區）---\n"
             f"SPARK_NETWORK={network}\n"
             f"SPARK_ACCOUNT_ID={account_id}\n"
             f"SPARK_USER_ADDR={user_address}\n"
             f"SPARK_BUILDER_ADDR={builder}\n")
    return text + block


def _ensure_env_file(env_path: Path, content: str, owner: str, group: str) -> None:
    if env_path.exists():
        return  # 已存在＝上一輪（或人工）建過；不覆寫既有風險參數。
    write_text_atomic(env_path, content, mode=ENV_FILE_MODE,
                      owner_ids=named_owner_ids(owner, group))


class WatcherState:
    """watcher 私有 state：{account_id: {"phase": "starting"|"started",
    "last_result": str}}。phase 回答「這顆引擎是不是本 watcher 拉起的」——
    是（starting）才能重試 start；已 started 絕不重啟（F3）。"""

    def __init__(self, path: Path):
        self._path = path
        self._data: dict = (json.loads(path.read_text())
                            if path.exists() else {})

    def phase(self, account_id: str) -> str | None:
        return self._data.get(account_id, {}).get("phase")

    def set_phase(self, account_id: str, phase: str) -> None:
        self._data.setdefault(account_id, {})["phase"] = phase
        self._save()

    def note_result(self, account_id: str, result: str) -> bool:
        """記錄本輪結果；回傳「與上一輪不同」（True 才值得記 INFO，F9 防洗版）。"""
        prev = self._data.get(account_id, {}).get("last_result")
        if prev == result:
            return False        # 沒變化就不落檔：等待中的條目每分鐘重寫整檔沒有意義
        self._data.setdefault(account_id, {})["last_result"] = result
        self._save()
        return True

    def _save(self) -> None:
        write_json_atomic(self._path, self._data, mode=0o600)


def _manifest_ref(manifest_path: str, account_id: str):
    """回傳 (是否在 manifest, 該筆的 leader_address 或 None)。

    tolerant 版（審查 F5）：一筆壞條目不得讓其他所有帳號的啟用連坐卡死；
    壞條目由引擎側與 activate 的 fail-fast 各自把關。leader 一併回傳給
    heal 路徑做「意圖已滿足」記錄回收（審查 F7A）。
    """
    p = Path(manifest_path)
    if not p.exists():
        return False, None
    refs, _bad = load_followers_tolerant(p)
    for f in refs:
        if f.account_id == account_id:
            return True, f.leader_address
    return False, None


def process_entry(entry: dict, *, pending_path: str, manifest_path: str,
                  builder: str, leaders_path: str, user_leaders_path: str,
                  leader_changes: list[dict], changes_path: str,
                  env_dir: Path, env_template: Path,
                  state_base: Path, owner: str, group: str, state: WatcherState,
                  notifier: Notifier, run_cmd=subprocess.run) -> str:
    """處理單一 pending 條目。回傳結果碼：activated／healed_*／waiting_leader。
    失敗以例外冒出，由呼叫端隔離。"""
    account_id = entry["account_id"]
    start_cmd = ["systemctl", "start", f"filet-follower@{account_id}"]

    # ⭐ 三道結構性核對最先做（F6）：之後的每一步（驗章的 user_address、root 建檔、
    # manifest 寫入）都以「這是一筆自洽的條目」為前提。
    validate_pending_entry(entry, account_id, builder)

    in_manifest, manifest_leader = _manifest_ref(manifest_path, account_id)
    if in_manifest:
        phase = state.phase(account_id)
        # 意圖已滿足的記錄一律回收（F7A）：不分分支——記錄留著只會讓引擎每 cycle
        # 發假 expired critical、對帳工具永久列 not_redeemed。
        if manifest_leader is not None:
            remove_satisfied_leader_change(changes_path, account_id=account_id,
                                           leader_address=manifest_leader)
        if phase == "starting":
            # 本 watcher 的 crash 窗口（activate 後、start 確認前）→ 重試 start。
            run_cmd(start_cmd, check=True)
            state.set_phase(account_id, "started")
            remove_pending_entry(pending_path, account_id)
            return "healed_started"
        # started（曾由本 watcher 啟動）或 None（人工 CLI 啟用）：**不碰引擎**——
        # `systemctl stop` 是正式的人工處置（F3），重按「完成綁定」不得復活它。
        remove_pending_entry(pending_path, account_id)
        notifier.warn(
            "auto-activate",
            f"account={account_id} 已在 manifest 卻又出現 pending 條目"
            f"（phase={phase}）；已清佇列、未動引擎——若該引擎應在跑，請人工 "
            f"systemctl start filet-follower@{account_id}",
            dedup_key=f"auto-activate:dup-pending:{account_id}")
        return "healed_no_start"

    leader = _latest_signed_leader(
        leader_changes, account_id=account_id,
        user_address=entry["user_address"])
    if leader is None:
        return "waiting_leader"  # 產品語意：選了 leader 才啟用。

    # env 與 state 先就位（unit 起得來的前置條件），manifest 之後、pending 最後——
    # 每一步失敗時，前面的產物都讓下一輪可安全重入。
    env_content = _compose_env(env_template, network=entry["network"],
                               account_id=account_id,
                               user_address=entry["user_address"],
                               builder=builder)
    _ensure_env_file(env_dir / f"{account_id}.env", env_content, owner, group)
    ensure_dir_secure(state_base / account_id, mode=STATE_DIR_MODE,
                      owner_ids=named_owner_ids(owner, group))

    # remove_pending=False（F2）：pending 是重試的載體，start 成功前不得清。
    activate(account_id, pending_path, manifest_path, builder,
             start=False, leader=leader, leaders_path=leaders_path,
             user_leaders_path=user_leaders_path, remove_pending=False)
    state.set_phase(account_id, "starting")
    run_cmd(start_cmd, check=True)
    state.set_phase(account_id, "started")
    # 記錄回收在清 pending **之前**（F7A）：兩步之間 crash 時，pending 還在 →
    # 下一輪 heal 分支會再做一次回收；順序反過來則記錄可能永遠沒人清。
    remove_satisfied_leader_change(changes_path, account_id=account_id,
                                   leader_address=leader)
    remove_pending_entry(pending_path, account_id)
    return "activated"


def run_once(*, pending_path: str, manifest_path: str, builder: str,
             leaders_path: str, exchange_dir: str, env_template: Path,
             env_dir: Path, state_base: Path, owner: str, group: str,
             state_file: Path, notifier: Notifier | None = None,
             run_cmd=subprocess.run) -> int:
    """掃一輪。回傳 exit code（0＝無失敗；1＝至少一條目失敗，全部條目都已嘗試）。"""
    notifier = notifier or NullNotifier()
    # 範本永遠先驗（F10）：部署錯誤要在安裝當下的驗收就爆，不是第一個用戶出現才爆。
    # 用假值組一次即可觸發兩類範本檢查。
    _compose_env(env_template, network="mainnet", account_id="f0",
                 user_address="0x" + "0" * 40, builder=builder)
    entries = load_pending(pending_path)
    if not entries:
        return 0
    changes_path = leader_changes_path_for(exchange_dir)
    leader_changes = (load_leader_changes(changes_path)
                      if Path(changes_path).exists() else [])
    user_leaders_path = user_leaders_path_for(exchange_dir)
    state = WatcherState(state_file)

    failures = 0
    for entry in entries:
        account_id = entry.get("account_id", "<缺 account_id>")
        try:
            result = process_entry(
                entry, pending_path=pending_path, manifest_path=manifest_path,
                builder=builder, leaders_path=leaders_path,
                user_leaders_path=user_leaders_path,
                leader_changes=leader_changes, changes_path=changes_path,
                env_dir=env_dir,
                env_template=env_template, state_base=state_base,
                owner=owner, group=group, state=state, notifier=notifier,
                run_cmd=run_cmd)
        except (SystemExit, Exception) as e:  # noqa: BLE001 — 逐條目隔離是本函式的職責
            failures += 1
            # CRIT＋條目保留：下輪自動重試，人工可從 journal／TG 追（工程原則 3）。
            # journal 每輪照記（unit failed 也每輪可見）；TG 由 dedup 防洗版。
            logger.critical("啟用失敗 account=%s: %s", account_id, e)
            notifier.critical("auto-activate",
                              f"啟用失敗 account={account_id}: {e}",
                              dedup_key=f"auto-activate:fail:{account_id}")
            state.note_result(account_id, "failed")
            continue
        if state.note_result(account_id, result):
            logger.info("account=%s result=%s", account_id, result)
    return 1 if failures else 0


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(
        description="自動啟用 pending followers（selected-leader 驅動；timer 執行）")
    ap.add_argument("--pending", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--exchange-dir", required=True,
                    help="filet-exchange 根目錄（leader_changes 與 user registry 由此推導）")
    ap.add_argument("--env-template", required=True)
    ap.add_argument("--env-dir", required=True)
    ap.add_argument("--state-base", required=True)
    ap.add_argument("--state-file", required=True,
                    help="watcher 私有狀態檔（記錄哪些帳號由本 watcher 啟動）")
    ap.add_argument("--owner", default="filet-engine")
    ap.add_argument("--group", default="filet-engine")
    args = ap.parse_args()

    builder = os.environ.get("FILET_BUILDER_ADDR")
    # ⭐ REPLACE 佔位與範本同一失敗語意（審查 deploy finding）：unit 檔沒填好時
    # 要的是一句人話＋exit 2，不是每個條目各炸一次 ValueError。
    if not builder or "REPLACE_WITH" in builder:
        print(__doc__)
        print("缺少或未填妥環境變數 FILET_BUILDER_ADDR"
              "（deploy/filet-auto-activate.service 的 REPLACE_WITH 佔位符）")
        raise SystemExit(2)
    try:
        leaders_path = require_leaders_path(os.environ)
    except ValueError as e:
        print(__doc__)
        print(str(e))
        raise SystemExit(2) from e

    # 告警通道與其他 filet 服務同源（/etc/filet/telegram.env 由 unit 注入）；
    # 無憑證 → TelegramNotifier 自身全靜默，等同 NullNotifier。
    notifier = TelegramNotifier(token=os.environ.get("COPY_TG_BOT_TOKEN", ""),
                                chat_id=os.environ.get("COPY_TG_CHAT_ID", ""))
    sys.exit(run_once(
        pending_path=args.pending, manifest_path=args.manifest, builder=builder,
        leaders_path=leaders_path, exchange_dir=args.exchange_dir,
        env_template=Path(args.env_template), env_dir=Path(args.env_dir),
        state_base=Path(args.state_base), owner=args.owner, group=args.group,
        state_file=Path(args.state_file), notifier=notifier))


if __name__ == "__main__":
    main()
