"""緊急手動 kill switch：撤全部掛單 → reduce-only 全平 → 寫 ARM_FILE → 告警。

用法: SPARK_ACCOUNT_ID=.. SPARK_USER_ADDR=0x.. SPARK_BUILDER_ADDR=0x.. \\
      [SPARK_NETWORK=testnet] uv run python -m scripts.panic [--yes]

- 預設 **dry-run**：只讀取並列印將執行的動作（撤單張數、各部位平倉方向與全量），
  不做任何寫入。加 `--yes` 才真的執行 trip 全流程。
- SPARK_NETWORK 預設 testnet，但**不擋 mainnet**——這是緊急工具，主網出事時必須
  能直接對主網執行；要打主網請顯式 SPARK_NETWORK=mainnet。
- re-arm＝人工刪 `var/copytrade/killswitch.tripped`（相對專案根）；本腳本不提供刪除路徑。
- import 階段不建 Info/Exchange（不打網路）；缺環境變數印用法退出非零。
- `_AdapterExecutor` 是 Task 12 正式 ActionExecutor 就緒前的薄轉接（只實作 trip 需要的
  三個方法，builder/slippage/signer 建構時注入）；Task 12 完成後由 ActionExecutor 取代。
"""
import os
import sys
from decimal import Decimal
from pathlib import Path

from spark.config import Settings
from spark.copytrade.config import CopySettings
from spark.copytrade.killswitch import evaluate, is_tripped, plan_close_actions, trip
from spark.copytrade.notifier import Notifier
from spark.exchange.base import BuilderCode, OpenOrder, OrderResult, Position, Signer

_REPO_ROOT = Path(__file__).resolve().parents[1]

USAGE = (
    "用法: SPARK_ACCOUNT_ID=.. SPARK_USER_ADDR=0x.. SPARK_BUILDER_ADDR=0x.. \\\n"
    "      [SPARK_NETWORK=testnet] uv run python -m scripts.panic [--yes]\n"
    "預設 dry-run 只列動作；--yes 才執行（撤單→reduce-only 全平→寫 ARM_FILE→告警）。"
)


def _exit_code(report) -> int:
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


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    execute = "--yes" in args

    required = ("SPARK_ACCOUNT_ID", "SPARK_USER_ADDR", "SPARK_BUILDER_ADDR")
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(USAGE)
        print(f"缺少環境變數: {', '.join(missing)}")
        raise SystemExit(2)

    # 網路依賴延後到這裡才 import/建構（import 階段零網路）。
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info

    from spark.exchange.hyperliquid import HyperliquidAdapter
    from spark.keystore.keychain import MacKeychainBackend

    network = os.environ.get("SPARK_NETWORK", "testnet")
    account_id = os.environ["SPARK_ACCOUNT_ID"]
    user_addr = os.environ["SPARK_USER_ADDR"]
    settings = Settings(builder_address=os.environ["SPARK_BUILDER_ADDR"],
                        account_id=account_id, network=network)
    copy_settings = CopySettings.from_env()
    info = Info(settings.api_url, skip_ws=True)

    if is_tripped(_REPO_ROOT):
        print(f"注意：kill switch 已 tripped（{_REPO_ROOT / 'var/copytrade/killswitch.tripped'}）。"
              f"panic 可重複執行以再次撤單/平倉。")

    if not execute:
        # dry-run：唯讀 adapter（exchange=None 結構性保證零寫入），列印計畫後退出。
        reader = HyperliquidAdapter(network, info=info, exchange=None)
        open_orders = reader.get_open_orders(user_addr)
        positions = reader.get_positions(user_addr)
        print(f"[DRY-RUN] network={network} user={user_addr}")
        for line in _plan_actions(open_orders, positions):
            print(line)
        print("以上僅為計畫，未執行任何動作。加 --yes 才會真的撤單/平倉/寫 ARM_FILE。")
        return

    # --yes：真執行 trip 全流程。
    ks = MacKeychainBackend()
    agent = ks.get_agent_signer(account_id)
    adapter = HyperliquidAdapter(
        network, info=info,
        exchange=Exchange(agent, settings.api_url, account_address=user_addr))
    executor = _AdapterExecutor(
        adapter, agent, user_addr,
        builder=BuilderCode(b=settings.builder_address, f=settings.f),
        slippage=copy_settings.slippage)
    notifier = _StdoutNotifier()

    ev = adapter.get_equity_view(user_addr)  # current/peak 同源（工程原則 1）
    # evaluate（非直呼 check_drawdown）：peak<=0 的 degenerate warn 結構性內建。
    # 手動 panic 不看 breached——照常執行；status 數字如實寫進 ARM_FILE。
    status = evaluate(ev, copy_settings, notifier)
    positions = {p.coin: p for p in adapter.get_positions(user_addr)}

    print(f"network={network} user={user_addr} 開始執行 kill switch trip ...")
    report = trip(executor, positions, notifier, _REPO_ROOT, status)
    print(f"完成：撤單 {report.cancelled} 張"
          f"{'（掛單清單未知，一張未撤）' if report.orders_not_cancelled else ''}｜"
          f"平倉成功 {list(report.closed) or '無'}｜"
          f"失敗 {list(report.failures) or '無'}｜ARM_FILE={report.arm_file}")
    raise SystemExit(_exit_code(report))


if __name__ == "__main__":
    main()
