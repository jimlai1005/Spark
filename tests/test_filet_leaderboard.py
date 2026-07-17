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
