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
from datetime import datetime, timezone
from pathlib import Path

from spark.config import Settings
from spark.copytrade.config import CopySettings
from spark.copytrade.executor import ActionExecutor, ActionRecord, VirtualBook
from spark.copytrade.killswitch import check_drawdown, is_tripped
from spark.copytrade.loop import main_loop, run_cycle
from spark.copytrade.notifier import NullNotifier, Notifier, TelegramNotifier
from spark.copytrade.orders import ReconcileState
from spark.exchange.base import BuilderCode
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
    """只讀報狀態（零寫入；adapter 以 exchange=None 建構，結構性保證）。"""
    ev = adapter.get_equity_view(user_addr)
    st = check_drawdown(ev, settings.max_drawdown_pct)
    breach_tag = "（已超過上限！）" if st.breached else ""
    print(f"equity: current=${ev.current} week_peak=${ev.recent_peak} "
          f"drawdown={st.drawdown_pct} / 上限 {settings.max_drawdown_pct}{breach_tag}")
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

    def cycle():
        start = len(ex.records)
        report = run_cycle(adapter, ex, copy_settings, notifier, state, state_root)
        if args.shadow:
            _append_shadow(ex.records[start:], shadow_dir)
        return report

    if args.once:
        print(cycle())
        return
    main_loop(cycle, copy_settings, notifier)


if __name__ == "__main__":
    main()
