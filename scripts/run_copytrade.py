"""跟單引擎 CLI 入口。

用法:
  SPARK_USER_ADDR=0x.. SPARK_BUILDER_ADDR=0x.. [SPARK_NETWORK=testnet] \\
  [SPARK_ACCOUNT_ID=..（live 模式必填）] [COPY_*..] \\
  uv run python -m scripts.run_copytrade [--once] [--dry-run] [--shadow] [--status]

旗標:
  --once     跑一輪同步、印 CycleReport 後退出
  --dry-run  強制 live=False（即使 COPY_LIVE_TRADING=true）
  --shadow   dry-run + VirtualBook + 每輪 ActionRecords 以 JSONL 追加到
             var/copytrade/shadow/YYYYMMDD.jsonl（Decimal 已存 str）
  --status   只讀報狀態（equity/回撤/部位/掛單/killswitch）後退出，零寫入

狀態根隔離:
  FILET_STATE_DIR  kill switch ARM 檔／alerts.log／shadow JSONL 的狀態根目錄。
                    缺省＝repo 根（保留 M1 單實例行為不變）。多 follower 共用
                    同一份 repo 時務必各自指定不同路徑——否則 follower A 觸發
                    的 kill switch ARM 檔會被 follower B 讀到而連坐停單。

leader 解析（per-follower ＋ 白名單二次驗證）:
  FILET_FOLLOWERS     follower manifest 路徑（預設＝repo 根下的
                      var/filet/followers.json，**絕對路徑**，不隨 CWD 漂移）。
  FILET_LEADERS_PATH  策劃 leader 白名單路徑。⭐ **必填、無預設、必須絕對路徑**
                      （2026-07-20；漏設即拒絕啟動，見 leader_resolve.require_leaders_path）
                      ——白名單同時餵 API、本引擎、activate CLI 與兩個快照 timer，
                      任一邊退回自己的預設值就會出現「兩份白名單」而沒有任何錯誤訊息。
  絕對路徑是硬性的：管理端 CLI 與引擎的 CWD 不同，相對路徑會讓兩邊驗不同檔，
  下架在一邊生效、引擎那邊仍放行（fail-open）。部署另見 deploy/RUNBOOK.md §5.5。
  本進程跟誰＝manifest 內自己那筆的 leader_address，缺值才回退 env
  COPY_LEADER_ADDRESS（**未移除，既有部署照舊可用**）。兩條路徑都要過白名單
  （例外只有「白名單檔不存在」時的 env 回退）。啟動時解析失敗即拒絕啟動；
  執行中每 cycle 重新解析：transient 失敗沿用上一個已驗證 leader ＋ critical；
  **撤銷**（enabled=false／條目被移除）則受控收尾（平倉＋鎖死 kill switch）。
  威脅模型與逐條規則見 spark/filet/leader_resolve.py 檔頭。

客戶簽章的換 leader（疊在上面那層解析之上）:
  FILET_PENDING_PATH  filet-api 的 pending.json 路徑；變更記錄檔＝它的同目錄
                      leader_changes.json（**與 API 共用同一個 env**，不另開旋鈕：
                      兩個 env 就有兩個能被半邊誤設的地方，設錯的症狀是「客戶按了
                      換 leader 卻永遠不生效」而兩邊 log 都正常）。未設則用
                      repo 根下的 var/filet/leader_changes.json。
  引擎每輪自己重新驗章（user_address 取自 manifest，不取自記錄），通過後再過一次
  白名單（is_still_permitted）才套用，並把已兌現的 nonce 記在自己的帳本
  <FILET_STATE_DIR>/var/filet/leader_change_ledger.json（原子寫）——API 的 nonce 存
  在它自己的 SQLite，引擎讀不到，沒有這份帳本同一筆記錄會每輪重複套用。
  **撤銷優先**：同一輪既有撤銷又有合法變更時，先收尾、不套用變更。
  威脅模型見 spark/filet/leader_change_apply.py 檔頭。

keystore 選擇:
  FILET_KEYSTORE   缺省／keychain → MacKeychainBackend（Mac 開發）；
                    envfile → EnvFileKeyStore(FILET_KEYS_DIR，預設 /etc/filet/keys，VPS 用)。

安全設計:
  - live 條件 = COPY_LIVE_TRADING=true 且未加 --dry-run/--shadow/--status；啟動前
    印大字警告並再驗 env 確實存在（紅線 5：live 是人工決策）。
  - dry/shadow/status 完全不碰 Keychain（不取 signer、不需 SPARK_ACCOUNT_ID），
    adapter 以 exchange=None 建構——結構性保證零寫入。
  - import 階段不觸網：hyperliquid/keystore 皆延後到 main() 內 import。
  - 通知：COPY_TG_BOT_TOKEN 存在 → TelegramNotifier.from_env()，否則 NullNotifier；
    account_id 有值時再包一層 TaggedNotifier（多 follower 共頻道可歸屬告警）。
"""
import argparse
import json
import logging
import os
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from spark.config import Settings
from spark.copytrade.config import CopySettings
from spark.copytrade.equity import perp_equity_view, sample_coverage, update_lifetime_peak
from spark.copytrade.executor import ActionExecutor, ActionRecord, VirtualBook
from spark.copytrade.killswitch import (ALERTS_LOG_RELPATH, DrawdownStatus,
                                        check_drawdown, count_alerts, is_tripped, trip)
from spark.copytrade.loop import main_loop, run_cycle, tripped_report
from spark.copytrade.notifier import NullNotifier, Notifier, TelegramNotifier
from spark.copytrade.orders import ReconcileState
from spark.exchange.base import BuilderCode
from spark.filet.capital_settings_apply import (
    LEDGER_RELPATH as CAPITAL_LEDGER_RELPATH,
)
from spark.filet.capital_settings import canonical_capital_values
from spark.filet.capital_settings_apply import (
    CapitalSettingsApplier,
    CapitalSettingsUnavailable,
    resolve_capital_settings_path,
)
from spark.filet.engine_health import (
    build_heartbeat,
    heartbeat_path_for,
    publish_heartbeat,
)
from spark.filet.leader_change_apply import (
    LEDGER_RELPATH,
    LeaderChangeApplier,
    require_exchange_dir,
    resolve_changes_path,
)
from spark.filet.leader_resolve import (
    DEFAULT_MANIFEST_PATH,
    LeaderResolution,
    LeaderResolutionError,
    LeaderWatch,
    require_leaders_path,
    resolve_leader,
)
from spark.filet.tagged_notifier import TaggedNotifier
from spark.keystore.base import KeyStore

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]


def resolve_state_dir() -> Path:
    """狀態根：讀 env FILET_STATE_DIR，缺省回 _REPO_ROOT（保留 M1 單實例行為）。
    kill switch ARM 檔／alerts.log／shadow JSONL 全部掛在此根之下（per-follower 隔離）。
    相對路徑 → .resolve() 成絕對（T4 reviewer minor：避免 CWD 依賴——同一個相對路徑
    在不同啟動目錄下會靜默指向不同狀態根，讓 kill switch ARM 檔悄悄失聯）。"""
    raw = os.environ.get("FILET_STATE_DIR")
    return Path(raw).resolve() if raw else _REPO_ROOT


def select_keystore() -> KeyStore:
    """keystore 後端依 env FILET_KEYSTORE 選擇：
    未設／keychain（預設，Mac 開發）→ MacKeychainBackend；
    envfile（VPS）→ EnvFileKeyStore(root=FILET_KEYS_DIR，預設 /etc/filet/keys)。
    import 延後到函式內（保留 import 階段零網路/零 macOS 依賴）。"""
    backend = os.environ.get("FILET_KEYSTORE", "keychain")
    if backend == "envfile":
        from spark.keystore.envfile import EnvFileKeyStore
        keys_dir = os.environ.get("FILET_KEYS_DIR", "/etc/filet/keys")
        return EnvFileKeyStore(keys_dir)
    from spark.keystore.keychain import MacKeychainBackend
    return MacKeychainBackend()


def make_leader_resolver(account_id: str | None, user_addr: str,
                         env_default: str) -> Callable[[], LeaderResolution]:
    """產生「解析本 follower 該跟的 leader」的閉包（每次呼叫重讀檔案）。

    路徑取自 env（沿 panic_all／filet_daily_report 的 FILET_FOLLOWERS 慣例與
    filet_activate 的 FILET_LEADERS_PATH 慣例），在**建構時**讀一次即可——
    env 在進程生命期內不變；每輪要重讀的是**檔案內容**，那由閉包內的
    resolve_leader 每次呼叫負責。回傳的閉包供 LeaderWatch 每 cycle 呼叫。
    """
    manifest_path = os.environ.get("FILET_FOLLOWERS", DEFAULT_MANIFEST_PATH)
    # ⭐ 白名單路徑**必填無預設**（同 require_exchange_dir 的處理）：漏設就拒絕啟動，
    # 不得靜默退回 repo 內的預設檔——那會讓引擎放行的清單與 API／activate 讀的不是
    # 同一份，且錯的方向是 fail-open（已撤銷的 leader 仍被放行）。
    leaders_path = require_leaders_path()

    def _resolve() -> LeaderResolution:
        return resolve_leader(account_id=account_id, manifest_path=manifest_path,
                              leaders_path=leaders_path, env_default=env_default,
                              self_address=user_addr)

    return _resolve


def make_effective_leader_resolver(base_resolve: Callable[[], LeaderResolution],
                                   *, account_id: str | None, notifier: Notifier,
                                   state_root: Path) -> Callable[[], LeaderResolution]:
    """把「客戶簽章的換 leader 記錄」疊在基礎解析之上，回傳每 cycle 用的解析閉包。

    ⭐ 撤銷優先是**結構性**的，不靠任何一行 if：`base_resolve()` 在參數位置先被
    求值，leader 被撤銷時它直接 raise LeaderRevokedError，applier 一行都不會跑
    （見 leader_change_apply 檔頭）。安全動作優先於便利動作——晚一輪換 leader 是
    客戶多等幾十秒，晚一輪收尾是繼續跟著一個已知出事的 leader 下單。

    `account_id` 為 None（dry/shadow 無身分）→ 原樣回傳 base_resolve：沒有帳號就
    沒有「哪一筆記錄是我的」，也沒有可信的 user_address 可比對簽章者。
    """
    if account_id is None:
        return base_resolve
    applier = LeaderChangeApplier(
        account_id=account_id,
        manifest_path=os.environ.get("FILET_FOLLOWERS", DEFAULT_MANIFEST_PATH),
        leaders_path=require_leaders_path(),   # 同上：必填無預設
        changes_path=resolve_changes_path(),
        ledger_path=state_root / LEDGER_RELPATH,
        notifier=notifier)

    def _resolve() -> LeaderResolution:
        return applier.effective(base_resolve())

    return _resolve


def make_capital_settings_applier(*, account_id: str | None, notifier: Notifier,
                                  state_root: Path):
    """建立每 cycle 套用「客戶簽章資金設定」的 applier；無 account_id → None。

    `account_id` 為 None（dry/shadow 無身分）→ 回 None：沒有帳號就沒有「哪一筆記錄
    是我的」，也沒有可信的 user_address 可比對簽章者（沿 make_effective_leader_resolver
    的同一個決定）。

    形狀與換 leader 的 applier 一致，但**不是**同一個接線點：換 leader 疊在
    `LeaderResolution` 上（由 LeaderWatch 每輪解析），資金設定疊在 `CopySettings` 上
    （由 cycle 直接套）。兩者互不依賴——同一輪內兩者都能各自套用，見 cycle()。
    """
    if account_id is None:
        return None
    return CapitalSettingsApplier(
        account_id=account_id,
        manifest_path=os.environ.get("FILET_FOLLOWERS", DEFAULT_MANIFEST_PATH),
        settings_path=resolve_capital_settings_path(),
        ledger_path=state_root / CAPITAL_LEDGER_RELPATH,
        notifier=notifier)


def make_heartbeat_publisher(*, account_id: str | None, state_root: Path,
                             now_fn=time.time):
    """建立「每 cycle 發布一份健康摘要到交換目錄」的閉包；無 account_id → None。

    ⭐⭐ 為什麼引擎要主動發布，而不是讓面板去讀狀態根（完整論證見
    `spark.filet.engine_health` 檔頭）：面板需要五個摘要值，狀態根裡裝的是引擎的
    全部狀態（equity 樣本、kill switch ARM 檔、兩份已兌現帳本、告警流水）。
    為了讀五個數字而把整包交給另一個進程，是拿廣泛的讀取權換窄的資訊需求。
    **發布一份窄的產物，優於開放廣泛的讀取權**——與換 leader 的設計同構，只是通道反向。

    `account_id` 為 None（dry/shadow 無身分）→ 回 None：心跳是 per-follower 的，
    沒有帳號就沒有「這是誰的心跳」（沿 make_capital_settings_applier 的同一個決定）。

    落點在建構時解出一次（`require_exchange_dir` 未設 env 即 raise ⇒ 拒絕啟動，與
    換 leader／資金設定同一條「半邊漏設不得靜默無效」的規則）；每輪重讀的是狀態根
    的內容，那由閉包內每次呼叫負責。
    """
    if account_id is None:
        return None
    path = heartbeat_path_for(require_exchange_dir(), account_id)

    def _publish(*, leader, settings, applied_capital, result: str,
                 detail: str | None) -> bool:
        """⚠️ **絕不 raise**：心跳是可觀測性，不是安全關鍵路徑（工程原則 3 的邊界）。

        `settings` 為 None ⇒ 本輪根本沒解出設定（leader 撤銷／資金設定不可判定），
        資金那一格回 `source="unavailable"` ＋ 全 null，**不回上一輪的值**：面板顯示
        一組本輪沒有被用來下單的數字，比顯示「不知道」危險。
        """
        try:
            now_s = float(now_fn())
            # kill switch 與樣本覆蓋度由引擎**自己**讀自己的狀態根（它讀得到，
            # 這正是本機制的重點）；讀取失敗一律回 None ＝未知，不回「沒觸發」。
            try:
                tripped = bool(is_tripped(state_root))
            except OSError:
                tripped = None
            try:
                cov = sample_coverage(state_root, now_fn=lambda: now_s)
            except OSError:
                cov = None
            # ⭐ 告警數同理由引擎自己數（面板那一側在狀態根不可讀／路徑漂移時
            # 恆為未知，心跳是唯一來源）。共用 `killswitch.count_alerts`——直讀端
            # 與心跳端各數一次會漂移，而面板會並排顯示這兩個數（工程原則 1）。
            # 它已把「讀不到」回成 None（不是 0），這裡不再補一層 try。
            alerts_count = count_alerts(state_root / ALERTS_LOG_RELPATH)
            alloc = util = None
            full = None
            capital_source = "unavailable"
            changed_at = None
            if settings is not None:
                # canonical 化：與客戶簽章記錄／帳本裡的字串形式同一個產生器，
                # 兩邊結構上不可能組出不同的字串（工程原則 1）——`/api/me/capital`
                # 要拿它與「已提交但未套用」的記錄相減，兩側必須同基準。
                _, alloc, _, util = canonical_capital_values(
                    settings.allocated_capital, settings.capital_utilization)
                full = bool(settings.use_full_equity)
                capital_source = ("customer_signed" if applied_capital is not None
                                  else "env_default")
                if applied_capital is not None:
                    changed_at = datetime.fromtimestamp(
                        applied_capital.applied_at, timezone.utc).isoformat()
            payload = build_heartbeat(
                account_id=account_id, now_s=now_s, killswitch_tripped=tripped,
                coverage=cov, alerts_count=alerts_count,
                leader_address=(leader.address if leader is not None else None),
                leader_source=(leader.source if leader is not None else None),
                allocated_capital=alloc, capital_utilization=util,
                use_full_equity=full, capital_source=capital_source,
                capital_changed_at=changed_at, cycle_result=result,
                cycle_detail=detail)
        except Exception:  # noqa: BLE001 — 見 docstring：組不出心跳也不得中斷跟單
            logger.exception("心跳組裝失敗（跟單不受影響）")
            return False
        return publish_heartbeat(path, payload)

    return _publish


def make_revocation_wind_down(adapter, ex, notifier: Notifier,
                              root: Path) -> Callable[[], None]:
    """leader 被白名單撤銷時的**受控收尾**閉包（交給 LeaderWatch 在偵測到撤銷時呼叫）。

    ⭐ 刻意重用 `killswitch.trip`，不另寫一套收尾：trip 的順序（lock-first 寫 ARM →
    撤全部掛單 → reduce-only 全平 → 覆寫 ARM → 持久告警）是紅線，兩套實作必然漂移，
    而漂移的那一套只在真出事時才會被執行到——那正是最不能出錯的時刻。

    reason="leader_revoked" 讓 ARM 檔寫出鎖死的真正理由（回撤數字在這條路徑上全是 0，
    沒有 reason 的話操作者只會看到一份看不出所以然的 payload）。
    """
    def _wind_down() -> None:
        positions = {p.coin: p for p in adapter.get_positions(ex.my_address)}
        trip(ex, positions, notifier, root,
             DrawdownStatus(current=Decimal("0"), peak=Decimal("0"),
                            drawdown_pct=Decimal("0"), breached=False),
             reason="leader_revoked")

    return _wind_down


def wrap_notifier(inner: Notifier, account_id: str | None) -> Notifier:
    """account_id 有值 → 包 TaggedNotifier（多 follower 共頻道時可歸屬告警）；
    account_id 為 None（dry/shadow 無 account）→ 原樣回傳 inner，不炸。"""
    if account_id is None:
        return inner
    return TaggedNotifier(inner, account_id)

USAGE = (
    "用法: SPARK_USER_ADDR=0x.. SPARK_BUILDER_ADDR=0x.. [SPARK_NETWORK=testnet] \\\n"
    "      [SPARK_ACCOUNT_ID=..（live 必填）] [COPY_*..] \\\n"
    "      uv run python -m scripts.run_copytrade [--once] [--dry-run] [--shadow] [--status]\n"
    "live 需 COPY_LIVE_TRADING=true 且不加 --dry-run/--shadow；dry/shadow/status 不碰 Keychain。"
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_copytrade", description="Hyperliquid 跟單引擎（live 預設關）")
    p.add_argument("--once", action="store_true", help="跑一輪同步後退出")
    p.add_argument("--dry-run", action="store_true",
                   help="強制 live=False（即使 COPY_LIVE_TRADING=true）")
    p.add_argument("--shadow", action="store_true",
                   help="dry-run + 虛擬掛單簿 + ActionRecords 落 JSONL")
    p.add_argument("--status", action="store_true", help="只讀報狀態後退出（零寫入）")
    return p


def _resolve_live(args: argparse.Namespace, settings: CopySettings) -> bool:
    """live 判定：任一唯讀/乾跑旗標都強制關閉，否則依 COPY_LIVE_TRADING。"""
    if args.dry_run or args.shadow or args.status:
        return False
    return settings.live_trading


def _append_shadow(records: list[ActionRecord], shadow_dir: Path,
                   day=None) -> Path:
    """本輪 ActionRecords 追加到 shadow_dir/YYYYMMDD.jsonl（UTC 日切）。
    payload 數值已為 str（ActionRecord 紅線），直接 json.dumps 不需 Decimal encoder。"""
    day = day if day is not None else datetime.now(timezone.utc).date()
    shadow_dir.mkdir(parents=True, exist_ok=True)
    path = shadow_dir / f"{day:%Y%m%d}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(
                {"ts": r.ts, "kind": r.kind, "coin": r.coin, "payload": r.payload},
                ensure_ascii=False) + "\n")
    return path


def _print_live_warning(network: str, settings: CopySettings) -> None:
    print("=" * 64)
    print("  ██  LIVE TRADING — 真實下單模式  ██")
    print(f"  network={network}  leader={settings.leader_address}")
    print(f"  max_drawdown={settings.max_drawdown_pct}  "
          f"flatten_on_breach={settings.flatten_on_breach}")
    print("  中止請 Ctrl-C；緊急全平：uv run python -m scripts.panic --yes")
    print("=" * 64)


def _print_status(adapter, user_addr: str, settings: CopySettings, root: Path) -> None:
    """只讀報狀態（零寫入：adapter 以 exchange=None 建構＋perp_equity_view(persist=False)）。

    equity 基準與引擎判定一致（perp accountValue + 滾動樣本，findings F1）——
    顯示與判定同基準才不會誤導操作者對緩衝的判斷。
    """
    ev = perp_equity_view(adapter, user_addr, root, persist=False)
    cov = sample_coverage(root)
    lifetime = update_lifetime_peak(root, ev.current, persist=False)
    st = check_drawdown(ev, settings.max_drawdown_pct)
    breach_tag = "（已超過上限！）" if st.breached else ""
    print(f"equity: current=${ev.current} week_peak=${ev.recent_peak} "
          f"drawdown={st.drawdown_pct} / 上限 {settings.max_drawdown_pct}{breach_tag}")
    if not cov.sufficient:
        print(f"  ⚠️ 回撤保護尚未生效：樣本 {cov.count} 筆／最舊 "
              f"{cov.oldest_age_s / 60:.0f} 分鐘"
              f"{'（樣本檔讀取失敗！）' if cov.read_error else ''}")
    if lifetime > 0 and settings.max_total_drawdown_pct > 0:
        total_dd = (lifetime - ev.current) / lifetime if lifetime > 0 else Decimal("0")
        print(f"  全期高水位={lifetime} 總回撤={total_dd} / 絕對底線 "
              f"{settings.max_total_drawdown_pct}")
    positions = adapter.get_positions(user_addr)
    print(f"positions ({len(positions)}):")
    for p in positions:
        side = "long" if p.szi > 0 else "short"
        print(f"  {p.coin:10s} {side:5s} size={abs(p.szi)} entry={p.entry_px} "
              f"lev={p.leverage}x{'C' if p.is_cross else 'I'} upnl={p.unrealized_pnl}")
    orders = adapter.get_open_orders(user_addr)
    print(f"open orders ({len(orders)}):")
    for o in orders:
        print(f"  oid={o.oid} {o.coin} {'B' if o.is_buy else 'S'} {o.sz}@{o.limit_px}"
              f"{' [RO]' if o.reduce_only else ''}{' [trigger]' if o.is_trigger else ''}")
    tripped = is_tripped(root)
    print(f"killswitch tripped: {tripped}"
          + ("（re-arm＝人工刪 var/copytrade/killswitch.tripped）" if tripped else ""))


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    user_addr = os.environ.get("SPARK_USER_ADDR")
    builder_addr = os.environ.get("SPARK_BUILDER_ADDR")
    if not user_addr or not builder_addr:
        missing = [k for k in ("SPARK_USER_ADDR", "SPARK_BUILDER_ADDR")
                   if not os.environ.get(k)]
        print(USAGE)
        print(f"缺少環境變數: {', '.join(missing)}")
        raise SystemExit(2)

    copy_settings = CopySettings.from_env()
    live = _resolve_live(args, copy_settings)
    network = os.environ.get("SPARK_NETWORK", "testnet")
    account_id = os.environ.get("SPARK_ACCOUNT_ID")
    if live and not account_id:
        print(USAGE)
        print("缺少環境變數: SPARK_ACCOUNT_ID（live 模式需 Keychain 的 agent key）")
        raise SystemExit(2)

    settings = Settings(builder_address=builder_addr,
                        account_id=account_id or "acct", network=network)

    # 狀態根與 notifier 都是純本地建構（讀 env／存欄位，零網路），刻意提到 leader
    # 解析之前：解析現在也要套用客戶簽章的換 leader 記錄，而那需要狀態根（引擎自己的
    # 已兌現 nonce 帳本落在那裡）與一個能發 critical 的 notifier。
    state_root = resolve_state_dir()
    notifier = (TelegramNotifier.from_env()
                if os.environ.get("COPY_TG_BOT_TOKEN") else NullNotifier())
    notifier = wrap_notifier(notifier, account_id)

    # ⭐ leader 解析＋白名單二次驗證，刻意放在建立 Info／取 key 之前：純檔案 IO，
    # 失敗要在碰網路與 Keychain 之前就拒絕啟動（威脅模型見 leader_resolve.py 檔頭）。
    # --status 是零寫入的診斷路徑，刻意豁免：設定壞掉的時候正是最需要它能跑的時候，
    # 讓它因 leader 解析失敗而停擺等於拿走唯一的排查工具（它也不會下任何單）。
    # ⭐ 啟動時就走**含客戶簽章變更**的完整解析（不只每 cycle）：否則每次重啟都會先
    # 退回 manifest 的舊 leader、第一輪再換回來——一次沒有意義的部位收斂，還會在每次
    # 重啟時發一則假的「leader 已變更」告警。
    resolve_leader_fn = make_effective_leader_resolver(
        make_leader_resolver(account_id, user_addr, copy_settings.leader_address),
        account_id=account_id, notifier=notifier, state_root=state_root)
    resolution: LeaderResolution | None = None
    if not args.status:
        try:
            resolution = resolve_leader_fn()
        except LeaderResolutionError as e:
            print(f"leader 解析失敗，拒絕啟動：{e}")
            raise SystemExit(2) from e
        # 解析結果覆蓋 env 值，之後所有讀 settings.leader_address 的路徑
        # （live 警告、run_cycle）都吃同一個已驗證位址——單一真相，不並存兩個來源。
        # 不變式：resolve_leader_fn() 只會回 LeaderResolution 或 raise，
        # 故此處 resolution 必非 None（見下方 LeaderWatch 前的 assert）。
        assert resolution is not None
        copy_settings = replace(copy_settings, leader_address=resolution.address)
        print(f"[leader] {resolution.address}（來源 {resolution.source}）")

    # 網路依賴延後到這裡才 import/建構（import 階段零網路）。
    from hyperliquid.info import Info

    from spark.exchange.hyperliquid import HyperliquidAdapter

    info = Info(settings.api_url, skip_ws=True)

    if args.status:
        adapter = HyperliquidAdapter(network, info=info, exchange=None)
        _print_status(adapter, user_addr, copy_settings, state_root)
        return

    if live:
        _print_live_warning(network, copy_settings)
        # 再驗 env 確實存在（防 from_env 注入路徑與 os.environ 不一致的漂移）。
        raw = (os.environ.get("COPY_LIVE_TRADING") or "").split("#", 1)[0].strip()
        if raw.lower() != "true":
            print("live 模式要求環境變數 COPY_LIVE_TRADING=true 確實存在，中止。")
            raise SystemExit(2)
        from hyperliquid.exchange import Exchange

        ks = select_keystore()
        signer = ks.get_agent_signer(account_id)
        adapter = HyperliquidAdapter(
            network, info=info,
            exchange=Exchange(signer, settings.api_url, account_address=user_addr))
    else:
        signer = None  # shadow/dry 零 Keychain
        adapter = HyperliquidAdapter(network, info=info, exchange=None)

    ex = ActionExecutor(
        adapter, signer, BuilderCode(b=builder_addr, f=settings.f),
        live=live, my_address=user_addr, settings=copy_settings,
        virtual_book=VirtualBook() if args.shadow else None)
    state = ReconcileState()

    mode = "LIVE" if live else ("SHADOW" if args.shadow else "DRY-RUN")
    print(f"[{mode}] network={network} leader={copy_settings.leader_address} "
          f"me={user_addr} interval={copy_settings.interval_s}s")

    shadow_dir = state_root / "var" / "copytrade" / "shadow"
    # 每 cycle 重新解析 leader：客戶換 leader 不必重啟服務、不必給 web 層提權；
    # transient 失敗沿用上一個已驗證值＋critical（refresh() 不 raise，跟單不中斷）；
    # **撤銷**（enabled=false／條目被移除）則走 on_revoked 受控收尾並回 None。
    # 不變式：走到這裡代表 args.status 為 False（--status 早已 return），所以上面的
    # `if not args.status` 區塊必定執行過、resolution 必非 None。這個不變式目前只靠
    # 「--status 提前 return」維持——把它寫成 assert，讓未來有人在中間插入新分支時
    # 是在這裡炸掉，而不是把 None 餵進 LeaderWatch 後在第一輪 refresh 才神祕失敗。
    assert resolution is not None
    watch = LeaderWatch(resolution, resolve_leader_fn, notifier,
                        on_revoked=make_revocation_wind_down(
                            adapter, ex, notifier, state_root))
    capital_applier = make_capital_settings_applier(
        account_id=account_id, notifier=notifier, state_root=state_root)
    publish_hb = make_heartbeat_publisher(account_id=account_id,
                                          state_root=state_root)

    def cycle():
        # ⭐ 心跳的本輪快照。每個出口在 return 之前更新它，`finally` 統一發布一次
        # ——包含例外離開的那條路徑（`result` 的初值就是 error，所以「引擎炸了」
        # 這件事本身也會被發布出去，而不是變成一段沉默）。
        # `settings` 與 `capital` **一起**在同一行設定：來源標記必須與本輪真正用來
        # 下單的那組值出自同一次求值（工程原則 1，見 CapitalSettingsApplier.last_applied）。
        hb = {"leader": None, "settings": None, "capital": None,
              "result": "error", "detail": "本輪未完成（例外中斷）"}
        try:
            res = watch.refresh()
            if res is None:
                # leader 已被撤銷、收尾已執行（或已大聲失敗）→ 本輪起零交易動作。
                # 刻意不倚賴「trip 寫了 ARM 檔所以 run_cycle 會自己短路」：ARM 寫入失敗時
                # 那個短路不存在，而「停止開新倉」不能建立在另一個動作成功的前提上。
                hb["result"] = "revoked"
                hb["detail"] = "leader 已被撤銷，收尾已執行；本輪起零交易動作"
                return tripped_report()
            hb["leader"] = res
            # 位址沒變就沿用同一個 settings 物件（避免每輪無謂重建）；變了才 replace，
            # 讓 run_cycle 的 leader 讀取與本輪解析結果同源（工程原則 1）。
            cs = (copy_settings if res.address == copy_settings.leader_address
                  else replace(copy_settings, leader_address=res.address))
            # ⭐ 資金設定疊在**已解析 leader 的** settings 之上。順序與換 leader 無關
            # （兩者改的是不同欄位），但擺在後面讓「本輪最終用的 settings」只有一個
            # 產生點——中間插一個 run_cycle 就會出現兩個都自稱是本輪設定的物件。
            if capital_applier is not None:
                try:
                    cs = capital_applier.effective(cs)
                except CapitalSettingsUnavailable:
                    # ⭐⭐ 帳本遺失／讀不到／內容超界 ⇒ **本輪零交易動作**，
                    # 絕不用 env 預設值繼續跑。applier 已發過指名事件的 critical。
                    # 退回預設是一次沒有客戶授權的曝險變更，而且全程安靜——這正是
                    # 整個模組要防的事（見 capital_settings_apply 檔頭）。
                    hb["detail"] = ("資金設定無法判定（帳本遺失／讀不到／內容超界），"
                                    "本輪零交易動作")
                    return tripped_report()
            hb["settings"] = cs
            hb["capital"] = (capital_applier.last_applied
                             if capital_applier is not None else None)
            start = len(ex.records)
            report = run_cycle(adapter, ex, cs, notifier, state, state_root)
            if args.shadow:
                _append_shadow(ex.records[start:], shadow_dir)
            # 三種正常結局分開回報：`tripped`（kill switch 已跳，零動作）、`ok`
            # （本輪真的下了單）、`no_action`（跑完但無事可做——絕大多數 cycle）。
            # 合成一個「成功」會讓面板分不出「引擎在跟單」與「引擎已熔斷但還活著」。
            # `getattr` 而非 `report.tripped`：心跳是可觀測性，不得因為 run_cycle 改了
            # 回傳型別（或測試替身回了別的東西）而把跟單這一輪打成 AttributeError。
            hb["result"] = ("tripped" if getattr(report, "tripped", False)
                            else "ok" if len(ex.records) > start else "no_action")
            hb["detail"] = None
            return report
        finally:
            if publish_hb is not None:
                publish_hb(leader=hb["leader"], settings=hb["settings"],
                           applied_capital=hb["capital"], result=hb["result"],
                           detail=hb["detail"])

    if args.once:
        print(cycle())
        return
    main_loop(cycle, copy_settings, notifier)


if __name__ == "__main__":
    main()
