"""跨 follower 日報：正確北極星（builder fee 查一次，不重複計）。

用法:
  [FILET_FOLLOWERS=var/filet/followers.json] uv run python -m scripts.filet_daily_report

環境變數:
  FILET_FOLLOWERS   follower manifest 路徑（預設 var/filet/followers.json）

流程:
  load_followers_tolerant(manifest) → refs, errors（壞條目跳過，不擋整份日報）
  → 北極星：對 refs 中出現的每個相異 mainnet builder 位址各查一次
    query_builder_accrued，減去 builder 層級快照
    （var/filet/builder_accrued_snapshot.json，無檔視為 0）→ builder_fee_delta；
    加總（跨「相異 builder」相加合法——M2 通常只有一個 builder，跨「follower」
    才是紅線禁止的重複計）；更新快照。
  → per-follower：collect_follower_summary(ref, adapter, start, end)
    （當日 UTC 0 點～now；函式內建錯誤隔離，單一 follower 失敗不中止其他）。
  → aggregate → render_aggregate 印 stdout ＋寫 var/filet/reports/YYYY-MM-DD.md。

注意:
  - testnet follower 不計入北極星（builder fee 對 testnet 無真實經濟意義），
    但仍在明細列出（FollowerSummary 標 network）。
  - manifest 壞條目與 per-follower 查詢失敗一律大聲印出（工程原則 4：失敗不得靜默），
    但不中止其他 follower 的匯總。
  - import 階段不觸網：所有網路呼叫都在 main() 內。
"""
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from hyperliquid.info import Info

from spark.config import API_URLS
from spark.exchange.hyperliquid import HyperliquidAdapter
from spark.filet.aggregate import aggregate, builder_fee_delta, collect_follower_summary, render_aggregate
from spark.filet.followers import load_followers_tolerant

VAR_DIR = Path("var/filet")
DEFAULT_MANIFEST = VAR_DIR / "followers.json"
SNAPSHOT_PATH = VAR_DIR / "builder_accrued_snapshot.json"
REPORTS_DIR = VAR_DIR / "reports"


def _usage() -> str:
    return ("用法: [FILET_FOLLOWERS=var/filet/followers.json] "
            "uv run python -m scripts.filet_daily_report")


def load_builder_snapshot() -> dict[str, Decimal]:
    """讀 builder 層級 accrued 快照（跨 follower 共用的全域量）；無檔視為空。"""
    if not SNAPSHOT_PATH.exists():
        return {}
    data = json.loads(SNAPSHOT_PATH.read_text())
    return {addr: Decimal(str(v)) for addr, v in data.get("builders", {}).items()}


def save_builder_snapshot(day_iso: str, accrued_by_builder: dict[str, Decimal]) -> None:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps({
        "date": day_iso,
        "builders": {addr: str(v) for addr, v in accrued_by_builder.items()},
    }, indent=2))


def _mainnet_builders(refs) -> list[str]:
    """北極星要查的相異 mainnet builder 位址（小寫正規化後去重）。

    以太坊位址大小寫不敏感（checksum 只是顯示層），若不正規化，同一位址的兩種
    大小寫會被當成兩個 builder 各查一次 `query_builder_accrued`——但兩者回同一個
    **全域**累積值，加總即北極星重複計，正是本任務要根除的。（先例 c624d2e。）
    """
    return sorted({r.builder_address.lower() for r in refs if r.network == "mainnet"})


def generate_report(refs, load_errors, adapter_for, now):
    """核心 wiring（adapter_for 注入以便離線測試）。回 AggregateReport。

    adapter_for: Callable[[str], adapter]——依 network 回一個 exchange adapter。
    北極星：對每個相異 mainnet builder 查一次 accrued（testnet 不計入），減快照 →
    builder_fee_delta；per-follower：collect_follower_summary（不查 accrued）。
    寫報表與快照為副作用。查詢失敗的 builder 大聲告警、其當日增量不計入（低估方向）。
    """
    day = now.date()
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)

    # --- 北極星：mainnet builder 層級查一次（絕不跨 follower 加總）---
    mainnet_builders = _mainnet_builders(refs)
    prev_snapshot = load_builder_snapshot()
    today_accrued: dict[str, Decimal] = {}
    failed_builders = 0
    north_star_delta = Decimal("0")
    for builder in mainnet_builders:
        adapter = adapter_for("mainnet")
        try:
            accrued_today = adapter.query_builder_accrued(builder)
        except Exception as e:  # noqa: BLE001 — 北極星查詢失敗大聲告警，不吞掉
            print(f"[WARN] builder accrued 查詢失敗 {builder}: {e}", file=sys.stderr)
            failed_builders += 1
            continue
        prev = prev_snapshot.get(builder, Decimal("0"))
        north_star_delta += builder_fee_delta(accrued_today, prev)
        today_accrued[builder] = accrued_today

    # --- per-follower summary（fills 衍生，不查 accrued）---
    summaries = []
    for ref in refs:
        adapter = adapter_for(ref.network)
        summaries.append(collect_follower_summary(ref, adapter, start, now))

    report = aggregate(day, summaries, north_star_fee_delta=north_star_delta)
    text = render_aggregate(report)
    if failed_builders:
        text += (f"\n\n> 注意：北極星不含 {failed_builders} 個查詢失敗的 builder，"
                 "當日增量為低估值。")
    if load_errors:
        text += "\n\n## Manifest 錯誤\n" + "\n".join(f"- {e}" for e in load_errors)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"{day.isoformat()}.md"
    out_path.write_text(text + "\n")

    print(text)
    print(f"\n[written] {out_path}", file=sys.stderr)

    if today_accrued:
        merged = {**prev_snapshot, **today_accrued}
        save_builder_snapshot(day.isoformat(), merged)

    return report


def main():
    manifest_path = Path(os.environ.get("FILET_FOLLOWERS", str(DEFAULT_MANIFEST)))
    try:
        refs, load_errors = load_followers_tolerant(manifest_path)
    except FileNotFoundError:
        print(_usage())
        raise SystemExit(2)

    for e in load_errors:
        print(f"[WARN] follower manifest 條目跳過: {e}", file=sys.stderr)

    now = datetime.now(timezone.utc)
    adapters: dict[str, HyperliquidAdapter] = {}

    def adapter_for(network: str) -> HyperliquidAdapter:
        """每網路一個 Info client（Info 綁定單一 API URL，mainnet/testnet 不可共用）。"""
        if network not in adapters:
            adapters[network] = HyperliquidAdapter(
                network, info=Info(API_URLS[network], skip_ws=True), exchange=None)
        return adapters[network]

    generate_report(refs, load_errors, adapter_for, now)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
