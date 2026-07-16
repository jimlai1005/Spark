"""Shadow diff 分類器測試（Task 16）。

hl-copytrader（`/Users/jim/projects/hl-copytrader`，絕對唯讀來源）log 行格式出處：
  行前綴 `main.py:43`：`"%(asctime)s [%(levelname)s] %(name)s: %(message)s"`
  掛單 place      `src/trader.py:355`（dry）/`:366`（live）
  改單 modify     `src/trader.py:404`（dry）/`:416`（live）
  取消 cancel     `src/trader.py:426`（dry）/`:430`（live）
  開倉 market_open `src/trader.py:158`（dry）/`:173`（live）
  平倉 close      `src/trader.py:219`（dry）/`:232`（live）
  設槓桿 update_leverage `src/trader.py:114`（dry）/`:129`（live）
  彙總統計行（非動作行）`src/orders.py:362-364`
以上格式字串已與該 repo 的 `logs/copytrader.log` 實際輸出核對過（該檔含真實帳戶
資料，故本檔測試樣本一律用合成座標值，只照抄格式、不抄真實數值）。
"""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from spark.copytrade.shadow import (
    classify_diff,
    load_action_records,
    parse_hl_log_line,
)

# ═══════════════════════════════════════════════════════════════════════
# 1. load_action_records：JSONL round-trip
# ═══════════════════════════════════════════════════════════════════════

def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_load_action_records_round_trip(tmp_path):
    path = tmp_path / "20260716.jsonl"
    records = [
        {"ts": 1000.0, "kind": "place", "coin": "ETH",
         "payload": {"is_buy": True, "sz": "1.5", "limit_px": "2000.25",
                     "reduce_only": False, "tif": "Gtc", "oid": "1", "ok": True}},
        {"ts": 1001.0, "kind": "cancel", "coin": "BTC",
         "payload": {"oid": "7", "ok": True}},
    ]
    _write_jsonl(path, records)
    loaded = load_action_records(path)
    assert loaded == records
    # Decimal 字串保留 str，不被悄悄轉型
    assert isinstance(loaded[0]["payload"]["sz"], str)
    assert isinstance(loaded[0]["payload"]["limit_px"], str)


def test_load_action_records_skips_blank_lines(tmp_path):
    path = tmp_path / "20260716.jsonl"
    path.write_text(
        '{"ts": 1.0, "kind": "cancel", "coin": "ETH", "payload": {"oid": "1"}}\n'
        "\n"
        "   \n"
        '{"ts": 2.0, "kind": "cancel", "coin": "BTC", "payload": {"oid": "2"}}\n',
        encoding="utf-8",
    )
    loaded = load_action_records(path)
    assert len(loaded) == 2
    assert [r["coin"] for r in loaded] == ["ETH", "BTC"]


def test_load_action_records_malformed_line_raises_with_context(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"ts": 1.0, "kind": "cancel"\n', encoding="utf-8")  # 缺右括號
    with pytest.raises(ValueError, match=r"bad\.jsonl:1"):
        load_action_records(path)


# ═══════════════════════════════════════════════════════════════════════
# 2. parse_hl_log_line：真實 hl-copytrader 格式（合成座標值）
# ═══════════════════════════════════════════════════════════════════════

def test_parse_place_dry_run():
    # 格式來源: hl-copytrader src/trader.py:355
    line = ("2026-01-01 00:00:00,000 [INFO] src.trader: "
            "[DRY RUN] 掛單 ETH 買 size=1.5 @ $2000.2500 Limit")
    a = parse_hl_log_line(line)
    assert a == {"kind": "place", "coin": "ETH", "is_buy": True,
                 "sz": Decimal("1.5"), "limit_px": Decimal("2000.25"),
                 "order_type": "Limit", "reduce_only": False}


def test_parse_place_live_with_reduce_only_and_result_suffix():
    # 格式來源: hl-copytrader src/trader.py:366
    line = ("2026-01-01 00:00:01,000 [INFO] src.trader: "
            "掛單 BTC 賣 size=0.3 @ $50000.0000 Limit [reduceOnly]: {'status': 'ok'}")
    a = parse_hl_log_line(line)
    assert a["kind"] == "place" and a["coin"] == "BTC"
    assert a["is_buy"] is False
    assert a["sz"] == Decimal("0.3")
    assert a["limit_px"] == Decimal("50000")
    assert a["reduce_only"] is True


def test_parse_modify_dry_and_live():
    # 格式來源: hl-copytrader src/trader.py:404（dry）/:416（live）
    dry = ("2026-01-01 00:00:02,000 [INFO] src.trader: "
           "[DRY RUN] 改單 ETH oid=7 → 買 size=2.0 @ $1900.0000")
    live = ("2026-01-01 00:00:03,000 [INFO] src.trader: "
            "改單 ETH oid=7 → 買 size=2.0 @ $1900.0000: ok")
    for line in (dry, live):
        a = parse_hl_log_line(line)
        assert a == {"kind": "modify", "coin": "ETH", "oid": 7, "is_buy": True,
                     "sz": Decimal("2.0"), "limit_px": Decimal("1900")}


def test_parse_cancel_dry_and_live():
    # 格式來源: hl-copytrader src/trader.py:426（dry）/:430（live）
    dry = "2026-01-01 00:00:04,000 [INFO] src.trader: [DRY RUN] 取消掛單 ETH oid=7"
    live = "2026-01-01 00:00:05,000 [INFO] src.trader: 取消掛單 ETH oid=7"
    for line in (dry, live):
        assert parse_hl_log_line(line) == {"kind": "cancel", "coin": "ETH", "oid": 7}


def test_parse_market_open_long_and_short():
    # 格式來源: hl-copytrader src/trader.py:158（dry）/:173（live）
    long_line = ("2026-01-01 00:00:06,000 [INFO] src.trader: "
                 "[DRY RUN] 開倉 BNB 多 size=1.0 lev=10x")
    short_line = ("2026-01-01 00:00:07,000 [INFO] src.trader: "
                  "開倉 BNB 空 size=1.0: {'status': 'ok'}")
    a = parse_hl_log_line(long_line)
    assert a == {"kind": "market_open", "coin": "BNB", "is_buy": True, "sz": Decimal("1.0")}
    b = parse_hl_log_line(short_line)
    assert b == {"kind": "market_open", "coin": "BNB", "is_buy": False, "sz": Decimal("1.0")}


def test_parse_close_long_and_short_direction_inverted():
    # 格式來源: hl-copytrader src/trader.py:219（dry）/:232（live）
    # positions.py:206 語意：平多賣（is_buy=False）、平空買（is_buy=True）。
    close_long = ("2026-01-01 00:00:08,000 [INFO] src.trader: "
                  "[DRY RUN] 平倉 BNB 平多 size=0.5 pnl≈+12.34")
    close_short = ("2026-01-01 00:00:09,000 [INFO] src.trader: "
                   "平倉 BNB 平空 size=0.5: {'status': 'ok'}")
    a = parse_hl_log_line(close_long)
    assert a == {"kind": "close", "coin": "BNB", "is_buy": False, "sz": Decimal("0.5")}
    b = parse_hl_log_line(close_short)
    assert b == {"kind": "close", "coin": "BNB", "is_buy": True, "sz": Decimal("0.5")}


def test_parse_update_leverage():
    # 格式來源: hl-copytrader src/trader.py:114（dry）
    line = "2026-01-01 00:00:10,000 [INFO] src.trader: [DRY RUN] 設定 BNB 槓桿 10x cross"
    assert parse_hl_log_line(line) == {
        "kind": "update_leverage", "coin": "BNB", "leverage": 10, "is_cross": True}


def test_parse_non_action_line_returns_none():
    # 格式來源: hl-copytrader src/orders.py:362-364（彙總統計，非單一動作）
    line = ("2026-01-01 00:00:11,000 [INFO] src.orders: "
            "同步完成：掛單(保留 5、改單 0、新增 0、取消 0) | 部位調整 0 筆")
    assert parse_hl_log_line(line) is None


def test_parse_accepts_bare_message_without_log_prefix():
    assert parse_hl_log_line("取消掛單 ETH oid=7") == {
        "kind": "cancel", "coin": "ETH", "oid": 7}


# ═══════════════════════════════════════════════════════════════════════
# 3. classify_diff：match / explainable / unexplained
# ═══════════════════════════════════════════════════════════════════════

PX_TOL = Decimal("0.002")     # 0.2%
SIZE_TOL = Decimal("0.05")    # 5%


def _spark(kind, coin, **payload):
    return {"ts": 1.0, "kind": kind, "coin": coin, "payload": payload}


# ── match（2 案）──────────────────────────────────────────────────────
def test_classify_match_place_within_price_tolerance():
    spark_actions = [_spark("place", "ETH", is_buy=True, sz="1.0", limit_px="2000.00")]
    hl_actions = [{"kind": "place", "coin": "ETH", "is_buy": True,
                  "sz": Decimal("1.0"), "limit_px": Decimal("2000.30")}]  # 差 0.015%
    items = classify_diff(spark_actions, hl_actions,
                          px_rel_tol=PX_TOL, size_ratio_tol=SIZE_TOL)
    assert len(items) == 1 and items[0].kind == "match"


def test_classify_match_cancel_structural_only():
    spark_actions = [_spark("cancel", "BTC", oid="7", ok=True)]
    hl_actions = [{"kind": "cancel", "coin": "BTC", "oid": 7}]
    items = classify_diff(spark_actions, hl_actions,
                          px_rel_tol=PX_TOL, size_ratio_tol=SIZE_TOL)
    assert len(items) == 1 and items[0].kind == "match"


# ── explainable（2 案，size 比值彼此一致）───────────────────────────────
def test_classify_explainable_consistent_size_ratio():
    spark_actions = [
        _spark("market_open", "ETH", is_buy=True, sz="0.5"),
        _spark("market_open", "BTC", is_buy=True, sz="0.25"),
    ]
    hl_actions = [
        {"kind": "market_open", "coin": "ETH", "is_buy": True, "sz": Decimal("1.0")},
        {"kind": "market_open", "coin": "BTC", "is_buy": True, "sz": Decimal("0.5")},
    ]
    items = classify_diff(spark_actions, hl_actions,
                          px_rel_tol=PX_TOL, size_ratio_tol=SIZE_TOL)
    assert len(items) == 2
    assert all(i.kind == "explainable" for i in items), items
    assert all("scale/weight" in i.detail for i in items)


# ── unexplained（2+ 案）：結構差 ＋ 比值不一致 ──────────────────────────
def test_classify_unexplained_structural_kind_mismatch():
    # spark 因 M1 限制記 skip_trigger，hl 實際下了 trigger 掛單 → kind 對不上。
    spark_actions = [_spark("skip_trigger", "SOL", is_buy=True, sz="1.0",
                           trigger_px="100", tpsl="sl", is_market=True, op="place")]
    hl_actions = [{"kind": "place", "coin": "SOL", "is_buy": True,
                  "sz": Decimal("1.0"), "limit_px": Decimal("100"),
                  "order_type": "Trigger", "reduce_only": False}]
    items = classify_diff(spark_actions, hl_actions,
                          px_rel_tol=PX_TOL, size_ratio_tol=SIZE_TOL)
    assert len(items) == 2
    assert all(i.kind == "unexplained" for i in items)


def test_classify_unexplained_inconsistent_size_ratio():
    spark_actions = [
        _spark("market_open", "SOL", is_buy=True, sz="0.5"),
        _spark("market_open", "XRP", is_buy=True, sz="0.9"),
    ]
    hl_actions = [
        {"kind": "market_open", "coin": "SOL", "is_buy": True, "sz": Decimal("1.0")},
        {"kind": "market_open", "coin": "XRP", "is_buy": True, "sz": Decimal("1.0")},
    ]
    items = classify_diff(spark_actions, hl_actions,
                          px_rel_tol=PX_TOL, size_ratio_tol=SIZE_TOL)
    assert len(items) == 2
    assert all(i.kind == "unexplained" for i in items), items
    assert all("不一致" in i.detail for i in items)


def test_classify_count_mismatch_within_group_is_unexplained():
    """同 key（kind/coin/direction）組內數量不對等——多出的那筆結構性算 unexplained。"""
    spark_actions = [
        _spark("cancel", "ETH", oid="1"),
        _spark("cancel", "ETH", oid="2"),
    ]
    hl_actions = [{"kind": "cancel", "coin": "ETH", "oid": 1}]
    items = classify_diff(spark_actions, hl_actions,
                          px_rel_tol=PX_TOL, size_ratio_tol=SIZE_TOL)
    kinds = sorted(i.kind for i in items)
    assert kinds == ["match", "unexplained"]
