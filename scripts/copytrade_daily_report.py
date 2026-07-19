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
  ＋印 stdout → 更新快照 ＋ 附加 accrued 歷史序列
  （var/copytrade/accrued_history.jsonl，供營運後台多日對帳）。

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
HISTORY_PATH = VAR_DIR / "accrued_history.jsonl"


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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def append_accrued_history(day_iso: str, accrued: Decimal, *,
                           now_fn=_utc_now) -> None:
    """附加當日 accrued 到歷史序列（jsonl，一天一行）——單一快照只夠今昨比較，
    歷史序列讓營運後台能做多日對帳（src/spark/publicapi/ops.py 讀它）。

    ⭐ 每行必帶 `captured_at`（快照時刻，UTC ISO8601）——**這是下游對帳窗口的唯一
    合法基準**（工程原則 1）：accrued 是「查詢當下」的鏈上累積量，故相鄰兩筆的差
    涵蓋 `(captured_at[前], captured_at[後]]`，**不是**日曆日。只存日期會讓營運後台
    拿「昨天一整天的實收」去比「今天 0 點到現在的應收」——cron 排在 00:10 時這是
    整整一天的錯配，健康帳戶會被判成巨大差異並誤報漏財（opus 對抗審查 Critical）。
    `now_fn` 可注入：呼叫端傳入 accrued **實際查詢當下**的時刻，測試可釘死。

    ⭐ 冪等：同日重跑覆蓋該日那一行（不是再 append 一筆）——重跑日報不該讓
    同一天出現兩個 accrued 值，那會讓下游的今昨差算成 0。覆蓋時 accrued 與
    captured_at **一起換**：兩者必須同時來自同一次查詢，拆開更新等於自造混源比較。
    ⭐ 舊行（無 captured_at）原樣保留、不回填猜測值——猜出來的窗口比沒有窗口更危險，
    下游看到缺值會拒絕硬算（ops.load_accrued_series → app.ops_revenue 的 basis_unknown）。
    ⭐ additive：不動 save_accrued_snapshot 的既有行為（向後相容）。
    """
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    rows: dict[str, dict] = {}
    if HISTORY_PATH.exists():
        for line in HISTORY_PATH.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                row = {"date": str(rec["date"])}
                if rec.get("captured_at"):
                    row["captured_at"] = str(rec["captured_at"])
                row["accrued"] = str(rec["accrued"])
                rows[row["date"]] = row
            except (ValueError, KeyError, TypeError):
                continue  # 壞行跳過，不讓一行壞資料擋掉今天的落檔
    rows[day_iso] = {"date": day_iso, "captured_at": now_fn().isoformat(),
                     "accrued": str(accrued)}  # 同日重跑覆蓋（值與時刻同源）
    HISTORY_PATH.write_text(
        "".join(json.dumps(rows[d]) + "\n" for d in sorted(rows)))


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
    # ⭐ 快照時刻取自 accrued 查詢**當下**（不是 main() 開頭的 now）：下游用它當對帳
    # 窗口界，值與時刻必須同源同時刻（工程原則 1）
    accrued_captured_at = datetime.now(timezone.utc)
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
    append_accrued_history(day.isoformat(), accrued_today,
                           now_fn=lambda: accrued_captured_at)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
