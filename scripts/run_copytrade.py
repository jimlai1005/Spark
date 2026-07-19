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
  FILET_FOLLOWERS     follower manifest 路徑（預設 var/filet/followers.json）。
  FILET_LEADERS_PATH  策劃 leader 白名單路徑（預設 var/filet/leaders.json）。
  本進程跟誰＝manifest 內自己那筆的 leader_address，缺值才回退 env
  COPY_LEADER_ADDRESS（**未移除，既有部署照舊可用**）。兩條路徑都要過白名單
  （例外只有「白名單檔不存在」時的 env 回退）。啟動時解析失敗即拒絕啟動；
  執行中每 cycle 重新解析，失敗則沿用上一個已驗證 leader ＋ critical 告警。
  威脅模型與逐條規則見 spark/filet/leader_resolve.py 檔頭。

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
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from spark.config import Settings
from spark.copytrade.config import CopySettings
from spark.copytrade.equity import perp_equity_view, sample_coverage, update_lifetime_peak
from spark.copytrade.executor import ActionExecutor, ActionRecord, VirtualBook
from spark.copytrade.killswitch import check_drawdown, is_tripped
from spark.copytrade.loop import main_loop, run_cycle
from spark.copytrade.notifier import NullNotifier, Notifier, TelegramNotifier
from spark.copytrade.orders import ReconcileState
from spark.exchange.base import BuilderCode
from spark.filet.leader_resolve import (
    DEFAULT_LEADERS_PATH,
    DEFAULT_MANIFEST_PATH,
    LeaderResolution,
    LeaderResolutionError,
    LeaderWatch,
    resolve_leader,
)
from spark.filet.tagged_notifier import TaggedNotifier
from spark.keystore.base import KeyStore

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
    leaders_path = os.environ.get("FILET_LEADERS_PATH", DEFAULT_LEADERS_PATH)

    def _resolve() -> LeaderResolution:
        return resolve_leader(account_id=account_id, manifest_path=manifest_path,
                              leaders_path=leaders_path, env_default=env_default,
                              self_address=user_addr)

    return _resolve


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
    from decimal import Decimal

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

    # ⭐ leader 解析＋白名單二次驗證，刻意放在建立 Info／取 key 之前：純檔案 IO，
    # 失敗要在碰網路與 Keychain 之前就拒絕啟動（威脅模型見 leader_resolve.py 檔頭）。
    # --status 是零寫入的診斷路徑，刻意豁免：設定壞掉的時候正是最需要它能跑的時候，
    # 讓它因 leader 解析失敗而停擺等於拿走唯一的排查工具（它也不會下任何單）。
    resolve_leader_fn = make_leader_resolver(
        account_id, user_addr, copy_settings.leader_address)
    resolution: LeaderResolution | None = None
    if not args.status:
        try:
            resolution = resolve_leader_fn()
        except LeaderResolutionError as e:
            print(f"leader 解析失敗，拒絕啟動：{e}")
            raise SystemExit(2) from e
        # 解析結果覆蓋 env 值，之後所有讀 settings.leader_address 的路徑
        # （live 警告、run_cycle）都吃同一個已驗證位址——單一真相，不並存兩個來源。
        copy_settings = replace(copy_settings, leader_address=resolution.address)
        print(f"[leader] {resolution.address}（來源 {resolution.source}）")

    # 網路依賴延後到這裡才 import/建構（import 階段零網路）。
    from hyperliquid.info import Info

    from spark.exchange.hyperliquid import HyperliquidAdapter

    info = Info(settings.api_url, skip_ws=True)
    state_root = resolve_state_dir()

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
    notifier = (TelegramNotifier.from_env()
                if os.environ.get("COPY_TG_BOT_TOKEN") else NullNotifier())
    notifier = wrap_notifier(notifier, account_id)
    state = ReconcileState()

    mode = "LIVE" if live else ("SHADOW" if args.shadow else "DRY-RUN")
    print(f"[{mode}] network={network} leader={copy_settings.leader_address} "
          f"me={user_addr} interval={copy_settings.interval_s}s")

    shadow_dir = state_root / "var" / "copytrade" / "shadow"
    # 每 cycle 重新解析 leader：客戶換 leader 不必重啟服務、不必給 web 層提權；
    # 解析失敗沿用上一個已驗證值＋critical（refresh() 不 raise，跟單不中斷）。
    watch = LeaderWatch(resolution, resolve_leader_fn, notifier)

    def cycle():
        res = watch.refresh()
        # 位址沒變就沿用同一個 settings 物件（避免每輪無謂重建）；變了才 replace，
        # 讓 run_cycle 的 leader 讀取與本輪解析結果同源（工程原則 1）。
        cs = (copy_settings if res.address == copy_settings.leader_address
              else replace(copy_settings, leader_address=res.address))
        start = len(ex.records)
        report = run_cycle(adapter, ex, cs, notifier, state, state_root)
        if args.shadow:
            _append_shadow(ex.records[start:], shadow_dir)
        return report

    if args.once:
        print(cycle())
        return
    main_loop(cycle, copy_settings, notifier)


if __name__ == "__main__":
    main()
