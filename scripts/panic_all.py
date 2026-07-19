"""scripts/panic_all.py
全域緊急平倉（跨全部 follower 一次觸發 kill switch）：逐 follower 撤單 →
reduce-only 全平 → 寫各自的 ARM_FILE → 告警。

用法:
  [FILET_FOLLOWERS=var/filet/followers.json] [FILET_STATE_BASE=/opt/filet/state] \\
  [FILET_KEYSTORE=envfile] [FILET_KEYS_DIR=/etc/filet/keys] \\
  uv run python -m scripts.panic_all [--yes]

- 預設 **dry-run**：逐 follower 只列印將執行的動作，零寫入、零 ARM 檔。
  加 `--yes` 才對**全部** follower 真的執行 trip 全流程。
- manifest 讀取用 `load_followers_tolerant`（壞條目跳過並記錄，不擋其他 follower
  的緊急平倉——災難工具的容錯優先於嚴格驗證，見 followers.py docstring）。
- 不擋 mainnet follower——這是緊急工具，主網出事時必須能直接對主網執行。

⭐ per-follower state root（load-bearing——2026-07-17 opus 交互審查抓出）：
  每個 follower 的 ARM 檔必須落在 `<FILET_STATE_BASE>/<account_id>/` 之下
  （對齊 Task 7 systemd unit 的 `FILET_STATE_DIR=/opt/filet/state/%i`），
  **絕不可**共用單一 repo 根。否則主網出事跑 `panic_all --yes` 平了倉，但 ARM
  落錯地方，引擎下個 cycle `is_tripped()=False` → 依 leader 重新開倉，緊急平倉
  被一個 cycle 抹掉（違反 killswitch lock-first 設計）。本檔對每個 follower都
  在迴圈內重新算 `state_root = FILET_STATE_BASE / ref.account_id`——不得挪到
  迴圈外共用一份。

- 單一 follower 失敗（manifest 壞條目、連線/建構例外、平倉部分失敗）**不得**
  中止其他 follower（工程原則 4：跨 follower 工具失敗要大聲、不能連坐）；
  退出碼＝各 follower `panic._exit_code` 語意跨 follower做 OR（任一
  failures/orders_not_cancelled/不可達/壞 manifest 條目 → 整體非 0）。
- keystore 走 `scripts.run_copytrade.select_keystore()`（`FILET_KEYSTORE=envfile`
  用於 VPS）；只在 `--yes` 時才建立（dry-run 完全不碰 Keychain/envfile）；
  絕不印任何 key／signer 內容，只印帳號/network/動作摘要。
- 告警經 `TaggedNotifier` 依 account_id 標籤（多 follower 共用終端輸出時可歸屬
  是哪個 follower 觸發的 critical）。
- import 階段不觸網：所有網路依賴延後到 `run_single_follower()` 內部 import。
"""
import os
import sys
from pathlib import Path

from spark.copytrade.config import CopySettings
from spark.filet.followers import load_followers_tolerant
from spark.filet.leader_resolve import DEFAULT_MANIFEST_PATH
from spark.filet.tagged_notifier import TaggedNotifier

from scripts.panic import _StdoutNotifier, _exit_code, run_single_follower
from scripts.run_copytrade import select_keystore

# ⭐ repo 根錨定，**不是** CWD 相對（沿 leader_resolve.DEFAULT_MANIFEST_PATH 的
# I4 修法）。本檔是**緊急工具**：出事時操作者是從哪個目錄 ssh 進來的無法預期，
# 而 CWD 相對路徑會讓 `panic_all --yes` 在最需要它的那一刻 FileNotFoundError
# 找不到 manifest（大聲失敗、非 fail-open，但緊急工具停擺本身就不可接受）。
# 直接引用 leader_resolve 的常數，不另寫一份字面量——同一份檔兩個預設值必定漂移。
DEFAULT_MANIFEST = Path(DEFAULT_MANIFEST_PATH)
DEFAULT_STATE_BASE = "/opt/filet/state"

USAGE = (
    "用法: [FILET_FOLLOWERS=var/filet/followers.json] "
    "[FILET_STATE_BASE=/opt/filet/state] \\\n"
    "      uv run python -m scripts.panic_all [--yes]\n"
    "預設 dry-run 逐 follower 只列動作；--yes 才對全部 follower 真的執行 trip。"
)


def _state_root_for(account_id: str) -> Path:
    """⭐ load-bearing：每個 follower 各自的狀態根，絕不可共用單一根（見模組
    docstring）。呼叫端須每 follower 各呼叫一次，不得快取共用。

    F1（opus review, Critical）路徑穿越守衛：account_id 若含 `..`／`/`／絕對路徑，
    `Path(base) / account_id` 會把 ARM 寫到錯的地方 → 引擎讀自己的 %i 目錄讀不到 →
    is_tripped=False → 下個 cycle 重新開倉 → 靜默漏平＋假成功。有兩種攻擊面：
      (a) **逃出 base**：`base/"../bob"` → 兄弟目錄、`base/"/etc/x"` → 絕對路徑；
      (b) **base 內兄弟目錄誤導**（opus re-review 追加）：`"alice/../bob"` 互消
          resolve 成 `base/bob`，parent 仍是 base 卻寫進真正 bob follower 的
          killswitch.tripped（與 bob 共寫同一 ARM）；symlink `base/alice`→`base/bob`
          同根因。

    三道防線，任一不過即 raise ValueError（呼叫端拒絕該 follower、不寫任何 ARM、
    critical、計入非 0 退出碼）：
      1. **對輸入**：account_id 含 `/`（os.sep）、含 `..` 分段、或為空 → 直接拒。
         這是最清楚的一道——守的是輸入形狀而非 resolve 後的巧合。
      2. **對 resolve 輸出**：`sr.parent` 必須恰為 base（擋逃出 base）。
      3. **末段字面比對**：`sr.name` 必須等於字面 account_id（擋 X/../Y 互消與
         symlink 改指——resolve 後末段變成別人時 name 不再等於原輸入）。
    （account_id 字元集在 Phase C onboarding 前無 registry 層強制，故本救命工具
    自帶此縱深防禦，不倚賴上游驗證。）"""
    if not account_id or "/" in account_id or ".." in account_id.split("/"):
        raise ValueError(
            f"account_id {account_id!r} 含路徑分隔/..、或為空；拒絕執行（路徑安全）")
    base = Path(os.environ.get("FILET_STATE_BASE", DEFAULT_STATE_BASE))
    sr = (base / account_id).resolve()
    if sr.parent != base.resolve() or sr.name != account_id:
        raise ValueError(
            f"account_id {account_id!r} 使狀態根落在 base 外或別的 follower 目錄"
            f"（resolve→{sr}）；拒絕執行（路徑安全）")
    return sr


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    execute = "--yes" in args

    manifest_path = Path(os.environ.get("FILET_FOLLOWERS", str(DEFAULT_MANIFEST)))
    try:
        refs, errors = load_followers_tolerant(manifest_path)
    except FileNotFoundError:
        print(USAGE)
        print(f"follower manifest 不存在: {manifest_path}")
        raise SystemExit(2)

    copy_settings = CopySettings.from_env()
    base_notifier = _StdoutNotifier()
    # keystore 只在真執行時建立——dry-run 完全不碰 Keychain/envfile。
    keystore = select_keystore() if execute else None

    overall_exit = 0
    # F4（opus review, Important）：壞 manifest 條目可能藏裸露主網活倉卻完全未被平倉，
    # 只 print 會被以 grep CRITICAL 監控的 operator 漏看 → 升為 critical。
    for e in errors:
        base_notifier.critical(
            "killswitch",
            f"manifest 壞條目，此帳號**完全未被平倉、可能有裸露暴險**，需人工處置: {e}")
        overall_exit = 1

    for ref in refs:
        notifier = TaggedNotifier(base_notifier, ref.account_id)
        tag = f"[{ref.account_id}]"
        # ⭐ 每 follower 各自計算狀態根（不共用）；F1 路徑穿越 → 拒絕該 follower。
        try:
            state_root = _state_root_for(ref.account_id)
        except ValueError as e:
            notifier.critical(
                "killswitch",
                f"路徑穿越拒絕執行，此 follower **完全未被平倉、可能有裸露暴險**: {e}")
            print(f"{tag} 路徑穿越，拒絕執行（未寫任何 ARM）: {e}")
            overall_exit = 1
            continue
        try:
            plan_lines, report = run_single_follower(
                network=ref.network, account_id=ref.account_id,
                user_addr=ref.user_address, builder_addr=ref.builder_address,
                execute=execute, state_root=state_root,
                copy_settings=copy_settings, notifier=notifier, keystore=keystore)
        except Exception as e:  # noqa: BLE001 — 單一 follower 失敗不得中止其他（工程原則 4）
            notifier.critical(
                "killswitch",
                f"建構/連線例外，此 follower 被跳過、**未平倉**，需人工確認: {e!r}")
            print(f"{tag} 例外，跳過（其餘 follower 照常平倉）: {e!r}")
            overall_exit = 1
            continue

        if not execute:
            print(f"{tag} [DRY-RUN] network={ref.network} user={ref.user_address}")
            for line in plan_lines:
                print(f"{tag} {line}")
            continue

        print(f"{tag} 完成：撤單 {report.cancelled} 張"
              f"{'（掛單清單未知，一張未撤）' if report.orders_not_cancelled else ''}｜"
              f"平倉成功 {list(report.closed) or '無'}｜"
              f"失敗 {list(report.failures) or '無'}｜ARM_FILE={report.arm_file}")
        if _exit_code(report) != 0:
            overall_exit = 1

    raise SystemExit(overall_exit)


if __name__ == "__main__":
    main()
