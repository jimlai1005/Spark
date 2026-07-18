"""緊急手動 kill switch：撤全部掛單 → reduce-only 全平 → 寫 ARM_FILE → 告警。

用法: SPARK_ACCOUNT_ID=.. SPARK_USER_ADDR=0x.. SPARK_BUILDER_ADDR=0x.. \\
      [SPARK_NETWORK=testnet] [FILET_STATE_DIR=..] uv run python -m scripts.panic [--yes]

- 預設 **dry-run**：只讀取並列印將執行的動作（撤單張數、各部位平倉方向與全量），
  不做任何寫入。加 `--yes` 才真的執行 trip 全流程。
- SPARK_NETWORK 預設 testnet，但**不擋 mainnet**——這是緊急工具，主網出事時必須
  能直接對主網執行；要打主網請顯式 SPARK_NETWORK=mainnet。
- 狀態根：FILET_STATE_DIR（缺省＝repo 根，與 run_copytrade.py 同慣例，保留單實例
  行為）。多 follower 部署時本檔仍是「單一 follower」CLI——請指定該 follower 專屬
  的狀態根；跨 follower 一次性平倉見 scripts/panic_all.py（其 state root 由
  FILET_STATE_BASE/<account_id> 推導，per-follower 強制隔離，不經本旗標）。
- re-arm＝人工刪 ARM_FILE（相對狀態根 var/copytrade/killswitch.tripped）；本腳本
  不提供刪除路徑。
- import 階段不建 Info/Exchange（不打網路）；缺環境變數印用法退出非零。
- `_AdapterExecutor` 是 Task 12 正式 ActionExecutor 就緒前的薄轉接（只實作 trip 需要的
  三個方法，builder/slippage/signer 建構時注入）；Task 12 完成後由 ActionExecutor 取代。
- `run_single_follower` 是單一帳號 panic 全流程（dry 或 --yes）的可複用核心，本檔
  main() 與 scripts/panic_all.py（Task 8 全域 panic）共用——state_root 一律由呼叫端
  算好傳入，本函式不自行猜根（⭐ load-bearing：跨 follower 呼叫端絕不可傳同一個根，
  否則 ARM 落錯地方、被引擎下個 cycle 讀不到而抹掉緊急平倉）。
"""
import os
import sys
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from spark.config import Settings
from spark.copytrade.config import CopySettings
from spark.copytrade.equity import perp_equity_view, reset_samples
from spark.copytrade.killswitch import (
    DrawdownStatus,
    FlattenReport,
    evaluate,
    is_tripped,
    plan_close_actions,
    trip,
)
from spark.copytrade.notifier import Notifier
from spark.exchange.base import BuilderCode, OpenOrder, OrderResult, Position, Signer
from spark.keystore.base import KeyStore
from spark.resilience import run as resilient_run

_REPO_ROOT = Path(__file__).resolve().parents[1]

USAGE = (
    "用法: SPARK_ACCOUNT_ID=.. SPARK_USER_ADDR=0x.. SPARK_BUILDER_ADDR=0x.. \\\n"
    "      [SPARK_NETWORK=testnet] [FILET_STATE_DIR=..] uv run python -m scripts.panic [--yes]\n"
    "預設 dry-run 只列動作；--yes 才執行（撤單→reduce-only 全平→寫 ARM_FILE→告警）。"
)


def resolve_state_dir() -> Path:
    """狀態根：讀 env FILET_STATE_DIR，缺省回 _REPO_ROOT（與 run_copytrade.py
    resolve_state_dir() 同慣例、保留單實例行為）。本檔持有自己模組層級的
    `_REPO_ROOT`（獨立於 run_copytrade 的同名常數），讓既有測試對
    `panic._REPO_ROOT` 的 monkeypatch 繼續生效。

    相對路徑 → .resolve() 成絕對（T4 reviewer minor，與 run_copytrade.py 孿生
    函式一致）：避免 CWD 依賴——同一相對路徑在不同啟動目錄下會靜默指向不同狀態
    根，讓緊急平倉的 ARM 檔落錯地方、被引擎下個 cycle 讀不到。"""
    raw = os.environ.get("FILET_STATE_DIR")
    return Path(raw).resolve() if raw else _REPO_ROOT


def _exit_code(report: FlattenReport) -> int:
    """退出碼＝殘留暴險訊號（純函式，供測試）：平倉失敗**或**掛單清單未知
    （orders_not_cancelled——一張未撤，殘留掛單可能繼續成交）都算未乾淨收場 → 1。"""
    return 1 if (report.failures or report.orders_not_cancelled) else 0


def _plan_actions(open_orders: list[OpenOrder], positions: list[Position]) -> list[str]:
    """純函式：由讀側狀態產生「將執行的動作」清單（dry-run 輸出）。零副作用。

    平倉動作**共用** killswitch.plan_close_actions——與 trip() 實際執行同一份規劃，
    預覽與實際不得雙實作（雙審 finding：漂移＝預覽騙人）。
    """
    lines = [f"將撤銷 {len(open_orders)} 張掛單:"]
    for o in open_orders:
        lines.append(f"  cancel {o.coin} oid={o.oid}")
    actions = plan_close_actions(positions)
    lines.append(f"將平倉 {len(actions)} 個部位（reduce-only 全量）:")
    for a in actions:
        side = "buy" if a.is_buy else "sell"  # 平空 → 買回；平多 → 賣出
        lines.append(f"  close {a.coin} {side} size={a.size}")
    return lines


class _StdoutNotifier(Notifier):
    """緊急工具的告警落點＝操作者的終端機（無 Telegram 依賴，斷網告警也看得到）。"""

    def _emit(self, level: str, category: str, text: str) -> bool:
        print(f"[{level.upper()}] {category}: {text}")
        return True

    def info(self, category: str, text: str, dedup_key: str | None = None) -> bool:
        return self._emit("info", category, text)

    def warn(self, category: str, text: str, dedup_key: str | None = None) -> bool:
        return self._emit("warn", category, text)

    def critical(self, category: str, text: str, dedup_key: str | None = None) -> bool:
        return self._emit("critical", category, text)


class _AdapterExecutor:
    """ExecutorPort 薄轉接（僅 trip 所需三方法）→ HyperliquidAdapter。

    builder/slippage/agent signer 建構時注入，不出現在方法簽章（紅線 3）。
    Task 12 的正式 ActionExecutor 就緒後由其取代。
    """

    def __init__(self, adapter, agent_signer: Signer, user_addr: str,
                 builder: BuilderCode, slippage: Decimal):
        self.records: list = []  # ExecutorPort 介面欄位
        self._adapter = adapter
        self._signer = agent_signer
        self._user_addr = user_addr
        self._builder = builder
        self._slippage = slippage

    def get_open_orders(self) -> list[OpenOrder]:
        return self._adapter.get_open_orders(self._user_addr)

    def cancel(self, coin: str, oid: int) -> bool:
        return self._adapter.cancel_order(self._signer, coin, oid)

    def close_reduce_only(self, coin: str, is_buy: bool, size: Decimal) -> OrderResult:
        return self._adapter.close_reduce_only(
            self._signer, coin, is_buy, size, self._slippage, self._builder)


def run_single_follower(
    *,
    network: str,
    account_id: str,
    user_addr: str,
    builder_addr: str,
    execute: bool,
    state_root: Path,
    copy_settings: CopySettings,
    notifier: Notifier,
    keystore: KeyStore | None = None,
    sleep_fn=time.sleep,
) -> tuple[list[str] | None, FlattenReport | None]:
    """單一帳號 panic 全流程——本檔 main()（單 follower CLI）與 scripts/panic_all.py
    （跨 follower 全域 panic）共用的可複用核心。

    dry（execute=False）：唯讀 adapter 列印計畫，回 (plan_lines, None)，零寫入、零 ARM。
        keystore 不需要（唯讀不碰 Keychain/envfile）。
    --yes（execute=True）：keystore 必填，建 agent signer → 真 adapter → trip()，
        回 (None, FlattenReport)。

    ⭐ state_root 決定 ARM/alerts 落點，由**呼叫端**算好傳入，本函式不自行猜根：
    - panic.py 單一 follower CLI：resolve_state_dir()（FILET_STATE_DIR，缺省 repo 根）。
    - panic_all.py 跨 follower：FILET_STATE_BASE/<account_id>（每 follower 各自一份，
      絕不可共用單一根——否則 ARM 落錯地方，引擎下個 cycle is_tripped()=False，
      緊急平倉被一個 cycle 抹掉，違反 killswitch lock-first 設計）。

    前置讀取（get_equity_view/get_positions）經 spark.resilience 邊界重試（讀取
    冪等、重送安全）；重試耗盡仍失敗 → **degrade 而非跳過該 follower**：仍呼叫
    trip() 寫 ARM 鎖死該 follower（並撤可讀的掛單），只是該部位未平——鎖死優先於
    完美平倉（工程原則 3，對齊 killswitch lock-first）。degrade 會 notifier.critical
    並在回傳 report.failures 加 sentinel（equity_unavailable/positions_unavailable），
    使 _exit_code 非 0——絕不因讀不到而靜默 exit 0 假成功。

    網路依賴（Info/Exchange/HyperliquidAdapter）延後到函式內 import（維持 import
    階段零網路的既有慣例，供 panic_all.py 批次呼叫多次時仍保持該保證）。
    """
    if execute and keystore is None:
        raise ValueError("run_single_follower(execute=True) 需要 keystore 才能取得 agent signer")

    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info

    from spark.exchange.hyperliquid import HyperliquidAdapter

    settings = Settings(builder_address=builder_addr, account_id=account_id, network=network)
    info = Info(settings.api_url, skip_ws=True)

    if not execute:
        # dry-run：唯讀 adapter（exchange=None 結構性保證零寫入），列印計畫後回傳。
        reader = HyperliquidAdapter(network, info=info, exchange=None)
        open_orders = reader.get_open_orders(user_addr)
        positions = reader.get_positions(user_addr)
        return _plan_actions(open_orders, positions), None

    # --yes：真執行 trip 全流程。
    agent = keystore.get_agent_signer(account_id)
    adapter = HyperliquidAdapter(
        network, info=info,
        exchange=Exchange(agent, settings.api_url, account_address=user_addr))
    executor = _AdapterExecutor(
        adapter, agent, user_addr,
        builder=BuilderCode(b=builder_addr, f=settings.f),
        slippage=copy_settings.slippage)

    # 前置讀取經 resilience 邊界（讀取冪等可重試）；耗盡即 degrade——鎖死優先。
    degraded: list[str] = []
    try:
        # current/peak 同源（工程原則 1）。evaluate（非直呼 check_drawdown）：peak<=0 的
        # degenerate warn 結構性內建。手動 panic 不看 breached——照常執行；數字寫進 ARM。
        ev = resilient_run(
            lambda: perp_equity_view(adapter, user_addr, state_root, persist=False),
            what="panic perp_equity_view", idempotent=True, sleep_fn=sleep_fn)
        status = evaluate(ev, copy_settings, notifier)
    except Exception as e:  # noqa: BLE001 — 讀失敗也要繼續鎖死；不得跳過該 follower
        notifier.critical(
            "killswitch",
            f"equity 讀取失敗（重試耗盡）: {e!r}——仍寫 ARM 鎖死該 follower，"
            f"回撤數字未知（degrade）")
        status = DrawdownStatus(current=Decimal("0"), peak=Decimal("0"),
                                drawdown_pct=Decimal("0"), breached=False)
        degraded.append("equity_unavailable")
    try:
        positions = {p.coin: p for p in resilient_run(
            lambda: adapter.get_positions(user_addr),
            what="panic get_positions", idempotent=True, sleep_fn=sleep_fn)}
    except Exception as e:  # noqa: BLE001 — 讀失敗仍鎖死＋撤可讀掛單，未平倉需人工確認
        notifier.critical(
            "killswitch",
            f"positions 讀取失敗（重試耗盡）: {e!r}——已鎖死該 follower 但**未平倉**，"
            f"需人工確認殘留部位（degrade：鎖死優先於完美平倉）")
        positions = {}
        degraded.append("positions_unavailable")

    report = trip(executor, positions, notifier, state_root, status, sleep_fn=sleep_fn)
    # 對齊 killswitch.trip()：清空 perp 權益樣本，否則人工 re-arm 後舊 peak 仍在窗內會立刻再熔斷。
    reset_samples(state_root)
    if degraded:
        # degrade 反映進 report.failures → _exit_code 非 0，operator 不會誤判乾淨收場。
        report = replace(report, failures=report.failures + tuple(degraded))
    return None, report


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    execute = "--yes" in args

    required = ("SPARK_ACCOUNT_ID", "SPARK_USER_ADDR", "SPARK_BUILDER_ADDR")
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(USAGE)
        print(f"缺少環境變數: {', '.join(missing)}")
        raise SystemExit(2)

    network = os.environ.get("SPARK_NETWORK", "testnet")
    account_id = os.environ["SPARK_ACCOUNT_ID"]
    user_addr = os.environ["SPARK_USER_ADDR"]
    builder_addr = os.environ["SPARK_BUILDER_ADDR"]
    copy_settings = CopySettings.from_env()
    state_root = resolve_state_dir()
    notifier = _StdoutNotifier()

    if is_tripped(state_root):
        print(f"注意：kill switch 已 tripped（{state_root / 'var/copytrade/killswitch.tripped'}）。"
              f"panic 可重複執行以再次撤單/平倉。")

    if not execute:
        plan_lines, _ = run_single_follower(
            network=network, account_id=account_id, user_addr=user_addr,
            builder_addr=builder_addr, execute=False, state_root=state_root,
            copy_settings=copy_settings, notifier=notifier)
        print(f"[DRY-RUN] network={network} user={user_addr}")
        for line in plan_lines:
            print(line)
        print("以上僅為計畫，未執行任何動作。加 --yes 才會真的撤單/平倉/寫 ARM_FILE。")
        return

    # --yes：建 keystore 後交給 run_single_follower 執行全流程。
    from spark.keystore.keychain import MacKeychainBackend
    ks = MacKeychainBackend()

    print(f"network={network} user={user_addr} 開始執行 kill switch trip ...")
    _, report = run_single_follower(
        network=network, account_id=account_id, user_addr=user_addr,
        builder_addr=builder_addr, execute=True, state_root=state_root,
        copy_settings=copy_settings, notifier=notifier, keystore=ks)
    print(f"完成：撤單 {report.cancelled} 張"
          f"{'（掛單清單未知，一張未撤）' if report.orders_not_cancelled else ''}｜"
          f"平倉成功 {list(report.closed) or '無'}｜"
          f"失敗 {list(report.failures) or '無'}｜ARM_FILE={report.arm_file}")
    raise SystemExit(_exit_code(report))


if __name__ == "__main__":
    main()
