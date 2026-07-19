"""tests/test_filet_leaderboard.py — watchlist 快照純函式（不觸網、不寫檔除了 tmp）。"""
import json
from datetime import date
from decimal import Decimal

from spark.filet.leaderboard import (DEFAULT_WATCHLIST, snapshot_watchlist,
                                     write_snapshot)

ADDR1 = "0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1"
ADDR2 = "0x" + "22" * 20

STATE1 = {"marginSummary": {"accountValue": "1000.5", "totalMarginUsed": "200",
                            "totalNtlPos": "800"},
          "withdrawable": "300.25",
          "assetPositions": [
              {"position": {"coin": "ETH", "unrealizedPnl": "12.5"}},
              {"position": {"coin": "BTC", "unrealizedPnl": "-2.5"}},
          ]}


def test_default_watchlist_contains_m1_leader():
    assert ADDR1 in DEFAULT_WATCHLIST


def test_snapshot_normalizes_fields():
    snap = snapshot_watchlist(lambda a: STATE1, [ADDR1], date(2026, 7, 18))
    assert snap["day"] == "2026-07-18"
    assert snap["source"] == "clearinghouseState"
    assert snap["row_count"] == 1 and snap["error_count"] == 0
    row = snap["rows"][0]
    assert row["address"] == ADDR1
    assert Decimal(row["account_value"]) == Decimal("1000.5")
    assert Decimal(row["total_margin_used"]) == Decimal("200")
    assert Decimal(row["total_ntl_pos"]) == Decimal("800")
    assert Decimal(row["withdrawable"]) == Decimal("300.25")
    assert Decimal(row["unrealized_pnl"]) == Decimal("10.0")  # 12.5 + (-2.5)
    assert row["position_count"] == 2


def test_snapshot_isolates_per_address_failure():
    """一個 leader 查掛不弄丟整批（定案 10）：error 條目 + error_count，其餘照常。"""
    def state_fn(addr):
        if addr == ADDR2:
            raise ConnectionError("boom")
        return STATE1

    snap = snapshot_watchlist(state_fn, [ADDR1, ADDR2], date(2026, 7, 18))
    assert snap["row_count"] == 1 and snap["error_count"] == 1
    ok = [r for r in snap["rows"] if "error" not in r]
    bad = [r for r in snap["rows"] if "error" in r]
    assert ok[0]["address"] == ADDR1
    assert bad[0]["address"] == ADDR2 and "boom" in bad[0]["error"]


def test_snapshot_malformed_state_is_isolated_too():
    snap = snapshot_watchlist(lambda a: {"unexpected": True}, [ADDR1], date(2026, 7, 18))
    assert snap["error_count"] == 1 and snap["row_count"] == 0


def test_write_snapshot_atomic_and_idempotent(tmp_path):
    """同日重跑覆寫同檔（冪等）；寫完無 .tmp 殘檔（原子：tmp + os.replace）。"""
    out = tmp_path / "watchlist"
    snap1 = snapshot_watchlist(lambda a: STATE1, [ADDR1], date(2026, 7, 18))
    p1 = write_snapshot(out, snap1)
    assert p1 == out / "2026-07-18.json"
    snap2 = snapshot_watchlist(lambda a: STATE1, [ADDR1, ADDR1], date(2026, 7, 18))
    p2 = write_snapshot(out, snap2)
    assert p2 == p1                                  # 同檔覆寫
    data = json.loads(p1.read_text())
    assert data["row_count"] == 2                    # 內容是第二次的
    assert list(out.glob("*")) == [p1]               # 目錄裡只有正式檔，無 tmp 殘檔


# --- perp 績效併抓（2026-07-19） -------------------------------------------
# 40 天窗、AV 因入金單調上升但 I_t 下跌（與 tests/test_leader_perf.py 同一組手算
# 資料）：快照層只驗「有沒有把值原樣帶下來」，公式正確性由 leader_perf 的測試負責。
_DAY_MS = 86_400_000
_PORTFOLIO = [
    ["month", {"accountValueHistory": [[0, "1"]], "pnlHistory": [[0, "1"]]}],
    ["perpMonth", {"accountValueHistory": [[0, "1000"], [20 * _DAY_MS, "2900"],
                                           [40 * _DAY_MS, "5510"]],
                   "pnlHistory": [[0, "0"], [20 * _DAY_MS, "-100"],
                                  [40 * _DAY_MS, "-390"]]}],
]


def test_snapshot_includes_perp_performance_when_portfolio_fn_given():
    snap = snapshot_watchlist(lambda a: STATE1, [ADDR1], date(2026, 7, 18),
                              portfolio_fn=lambda a: _PORTFOLIO)
    assert snap["perf_source"] == "portfolio(perp windows)"
    assert snap["perf_error_count"] == 0
    row = snap["rows"][0]
    assert row["account_value"] == str(Decimal(STATE1["marginSummary"]["accountValue"]))
    win = row["perf"]["windows"]["perpMonth"]
    assert win["twr"] == "-0.19"
    assert win["max_drawdown"] == "0.19"     # 算在 I_t 上（AV 遞增，AV 基準會是 0）
    assert win["covered_days"] == "40.0000" and win["sample_count"] == 3
    assert win["disclosure_tier"] == "window_return"
    # ⚠️ 2026-07-19 揭露模型改版（使用者裁決「顯示但註記」）：40 天窗**照樣**有年化，
    # 但必須連同不足標記與外推天數一起落進快照——快照是目錄頁的資料源，標記在這一層
    # 掉了，前端就再也拿不到警示（見 filet/leader_perf.py 檔頭「揭露模型改版」）。
    assert win["annualized_return_insufficient_data"] is True
    assert win["annualized_return_extrapolated_from_days"] == "40.0000"
    assert win["twr_insufficient_data"] is False      # 40 天 ≥ 30 天門檻
    assert "equity_index" not in win and win["equity_index_len"] == 3


def test_snapshot_only_reads_perp_windows_never_the_default_ones():
    """⭐ basis：預設窗（含 spot 與 vault）不得混進快照。上面 fixture 的 "month" 列
    是誘餌——若實作抓錯窗，perpMonth 的數字會變成那一列的值。"""
    snap = snapshot_watchlist(lambda a: STATE1, [ADDR1], date(2026, 7, 18),
                              portfolio_fn=lambda a: _PORTFOLIO)
    assert set(snap["rows"][0]["perf"]["windows"]) == {
        "perpDay", "perpWeek", "perpMonth", "perpAllTime"}
    assert snap["rows"][0]["perf"]["windows"]["perpDay"]["status"] == "insufficient"


def test_perf_failure_does_not_lose_scale_data():
    """績效查詢失敗 → 該列仍保有 clearinghouse 規模欄位，只是多一個 perf_error。
    兩個計數分開：error_count（整列沒了）vs perf_error_count（部分降級）。"""
    def boom(addr):
        raise ConnectionError("portfolio down")

    snap = snapshot_watchlist(lambda a: STATE1, [ADDR1], date(2026, 7, 18),
                              portfolio_fn=boom)
    assert snap["error_count"] == 0 and snap["perf_error_count"] == 1
    assert snap["row_count"] == 1
    row = snap["rows"][0]
    assert row["account_value"] and "perf" not in row
    assert "portfolio down" in row["perf_error"]


def test_no_portfolio_fn_keeps_old_shape():
    """未啟用績效抓取 → perf_source 為 None（≠「抓了但全失敗」），列上無 perf 欄。"""
    snap = snapshot_watchlist(lambda a: STATE1, [ADDR1], date(2026, 7, 18))
    assert snap["perf_source"] is None and snap["perf_error_count"] == 0
    assert "perf" not in snap["rows"][0]


def test_state_failure_skips_perf_entirely():
    """clearinghouse 就失敗的列不該再去打 portfolio（沒有規模資料，績效也無處可掛）。"""
    calls = []

    def state_fn(addr):
        raise ConnectionError("state down")

    snap = snapshot_watchlist(state_fn, [ADDR1], date(2026, 7, 18),
                              portfolio_fn=lambda a: calls.append(a) or _PORTFOLIO)
    assert snap["error_count"] == 1 and snap["perf_error_count"] == 0
    assert calls == []
