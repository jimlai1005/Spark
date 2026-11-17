"""tests/test_strategies_metric_spec.py — plan 2026-11-16-strategy-return-hl-roi D1／D7：
公開 JSON 鍵名一律不改，只改 `total_return_pct` 讀的 perf 鍵（twr → roi）。"""
from spark.filet.strategies import _METRIC_SPEC

# 改動前（2026-11-16 之前）的公開鍵序列，逐字來自舊 `_METRIC_SPEC` 第一欄。
_PUBLIC_KEYS_BEFORE = (
    "total_return_pct", "max_drawdown_pct", "sharpe", "sharpe_se", "win_rate_pct",
    "annualized_vol_pct", "sortino", "best_day_pct", "worst_day_pct")


def test_public_metric_keys_unchanged():
    assert tuple(row[0] for row in _METRIC_SPEC) == _PUBLIC_KEYS_BEFORE


def test_total_return_reads_roi_and_its_own_insufficient_flag():
    row = next(r for r in _METRIC_SPEC if r[0] == "total_return_pct")
    assert row[1:3] == ("roi", "roi_insufficient_data")
