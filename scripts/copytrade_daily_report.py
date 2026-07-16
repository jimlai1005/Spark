"""每日 TE + fee 對帳報告（copytrade）。

用法:
  SPARK_USER_ADDR=0x.. SPARK_BUILDER_ADDR=0x.. \\
  [COPY_LEADER_ADDRESS=0x..] [SPARK_NETWORK=testnet] \\
  uv run python -m scripts.copytrade_daily_report

環境變數:
  SPARK_USER_ADDR      我方地址（必填）
  SPARK_BUILDER_ADDR   builder 地址（必填；accrued 查詢與 CSV 對帳用）
  COPY_LEADER_ADDRESS  leader 地址（預設 0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1）
  SPARK_NETWORK        網路（預設 testnet）

流程:
  取兩邊當日 UTC 0點~now 的 user fills → 讀昨日 accrued 快照
  （var/copytrade/accrued_snapshot.json，無檔視為 0）→ query_builder_accrued
  → reconcile 取 csv_report → build_daily_report → 寫 var/copytrade/reports/YYYY-MM-DD.md
  ＋印 stdout → 更新快照。

注意:
  - skipped 資料本版讀 var/copytrade/skipped/YYYY-MM-DD.json（無檔 → 空 list）；
    該檔的寫入方屬主迴圈任務（Task 12/16），非本腳本。
  - TelegramNotifier 接線不在本腳本範圍（屬 Task 12/16 整合）。
  - import 階段不觸網：所有網路呼叫都在 main() 內。
"""
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from hyperliquid.info import Info

from spark.config import Settings
from spark.copytrade.report import build_daily_report, render_report
from spark.exchange.hyperliquid import HyperliquidAdapter
from spark.verification.reconcile import reconcile

DEFAULT_LEADER = "0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1"
VAR_DIR = Path("var/copytrade")
SNAPSHOT_PATH = VAR_DIR / "accrued_snapshot.json"


def _usage() -> str:
    return ("用法: SPARK_USER_ADDR=0x.. SPARK_BUILDER_ADDR=0x.. "
            "[COPY_LEADER_ADDRESS=0x..] [SPARK_NETWORK=testnet] "
            "uv run python -m scripts.copytrade_daily_report")


def load_accrued_snapshot() -> Decimal:
    """讀昨日 accrued 快照；無檔視為 0。"""
    if not SNAPSHOT_PATH.exists():
        return Decimal("0")
    data = json.loads(SNAPSHOT_PATH.read_text())
    return Decimal(str(data["accrued"]))


def save_accrued_snapshot(day_iso: str, accrued: Decimal) -> None:
    """更新 accrued 快照。"""
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(
        json.dumps({"date": day_iso, "accrued": str(accrued)}, indent=2))


def load_skipped(day_iso: str) -> list[tuple[str, Decimal]]:
    """讀當日 skipped 小額資料；無檔 → 空 list。

    格式：[{"coin": "ETH", "notional": "123.4", "reason": "..."} , ...]
    （reason 可有可無；寫入方屬主迴圈任務。）
    """
    path = VAR_DIR / "skipped" / f"{day_iso}.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return [(item["coin"], Decimal(str(item["notional"]))) for item in data]


def main():
    user_addr = os.environ.get("SPARK_USER_ADDR")
    builder_addr = os.environ.get("SPARK_BUILDER_ADDR")
    if not user_addr or not builder_addr:
        print(_usage())
        raise SystemExit(2)
    leader_addr = os.environ.get("COPY_LEADER_ADDRESS", DEFAULT_LEADER)
    network = os.environ.get("SPARK_NETWORK", "testnet")

    settings = Settings(builder_address=builder_addr,
                        account_id=os.environ.get("SPARK_ACCOUNT_ID", "acct"),
                        network=network)
    adapter = HyperliquidAdapter(network, info=Info(settings.api_url, skip_ws=True),
                                 exchange=None)

    # 當日 UTC 0點 ~ now
    now = datetime.now(timezone.utc)
    day = now.date()
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)

    leader_fills = adapter.get_user_fills(leader_addr, start, now)
    my_fills = adapter.get_user_fills(user_addr, start, now)

    accrued_prev = load_accrued_snapshot()
    accrued_today = adapter.query_builder_accrued(builder_addr)
    csv_report = reconcile(adapter, builder_addr, day)
    skipped = load_skipped(day.isoformat())

    report = build_daily_report(day, leader_fills, my_fills, skipped,
                                accrued_today, accrued_prev, csv_report)
    text = render_report(report)

    reports_dir = VAR_DIR / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    out_path = reports_dir / f"{day.isoformat()}.md"
    out_path.write_text(text + "\n")

    print(text)
    print(f"\n[written] {out_path}", file=sys.stderr)

    save_accrued_snapshot(day.isoformat(), accrued_today)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
