"""Shadow diff differ CLI（Task 16）：比對 spark `--shadow` JSONL 與 hl-copytrader
線上 log，分類 match/explainable/unexplained 並輸出報告。

用法:
  uv run python -m scripts.shadow_diff --spark var/copytrade/shadow/YYYYMMDD.jsonl \\
      --hl-log <hl copytrader log 檔路徑> [--px-tol 0.002] [--size-tol 0.05]

輸出:
  stdout 印三類計數＋逐項 detail；另寫
  var/copytrade/shadow/diff-YYYYMMDD.md（YYYYMMDD = 執行當下 UTC 日期）。

無 --spark/--hl-log 任一者時印用法並 exit 2（不吞錯，讓呼叫方立刻知道少了什麼）。
本工具只讀兩份輸入檔、只寫 var/copytrade/shadow/ 底下的報告——不碰網路、不碰
hl-copytrader 原始碼（絕對唯讀來源，本工具只讀其 log 輸出）。
"""
import argparse
import sys
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from spark.copytrade.shadow import classify_diff, load_action_records, parse_hl_log_line

_REPO_ROOT = Path(__file__).resolve().parents[1]
SHADOW_DIR = _REPO_ROOT / "var" / "copytrade" / "shadow"

USAGE = (
    "用法: uv run python -m scripts.shadow_diff --spark <shadow jsonl> "
    "--hl-log <hl-copytrader log 檔> [--px-tol 0.002] [--size-tol 0.05]"
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="shadow_diff", description="spark shadow JSONL vs hl-copytrader log 差異分類器")
    p.add_argument("--spark", type=Path, help="spark --shadow 產出的 JSONL 路徑")
    p.add_argument("--hl-log", type=Path, help="hl-copytrader log 檔路徑")
    p.add_argument("--px-tol", type=str, default="0.002",
                   help="價格相對誤差容忍（預設 0.002 = 0.2%%）")
    p.add_argument("--size-tol", type=str, default="0.05",
                   help="size 比值一致性容忍（預設 0.05 = 5%%）")
    return p


def _parse_hl_log(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    actions = []
    for line in lines:
        parsed = parse_hl_log_line(line)
        if parsed is not None:
            actions.append(parsed)
    return actions


def render_report(items, *, spark_path: Path, hl_path: Path,
                  px_tol: Decimal, size_tol: Decimal) -> str:
    counts = Counter(item.kind for item in items)
    lines = [
        "# Shadow diff 分類報告",
        "",
        f"spark: `{spark_path}`",
        f"hl-log: `{hl_path}`",
        f"px_rel_tol={px_tol}  size_ratio_tol={size_tol}",
        "",
        f"match={counts.get('match', 0)}  "
        f"explainable={counts.get('explainable', 0)}  "
        f"unexplained={counts.get('unexplained', 0)}",
        "",
    ]
    for kind in ("match", "explainable", "unexplained"):
        subset = [i for i in items if i.kind == kind]
        if not subset:
            continue
        lines.append(f"## {kind} ({len(subset)})")
        lines.extend(f"- {i.detail}" for i in subset)
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(USAGE)
        raise SystemExit(2)

    args = build_parser().parse_args(argv)
    if not args.spark or not args.hl_log:
        print(USAGE)
        missing = [name for name, val in (("--spark", args.spark), ("--hl-log", args.hl_log))
                   if not val]
        print(f"缺少參數: {', '.join(missing)}")
        raise SystemExit(2)

    try:
        px_tol = Decimal(args.px_tol)
        size_tol = Decimal(args.size_tol)
    except InvalidOperation as e:
        print(USAGE)
        print(f"--px-tol/--size-tol 必須是數字: {e}")
        raise SystemExit(2) from e

    spark_actions = load_action_records(args.spark)
    hl_actions = _parse_hl_log(args.hl_log)

    items = classify_diff(spark_actions, hl_actions, px_rel_tol=px_tol, size_ratio_tol=size_tol)

    report = render_report(items, spark_path=args.spark, hl_path=args.hl_log,
                           px_tol=px_tol, size_tol=size_tol)
    print(report)

    day = datetime.now(timezone.utc).date()
    SHADOW_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SHADOW_DIR / f"diff-{day:%Y%m%d}.md"
    out_path.write_text(report + "\n", encoding="utf-8")
    print(f"\n[written] {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
