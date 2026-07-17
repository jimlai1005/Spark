"""tests/test_watchlist_snapshot.py — watchlist 快照 CLI（state_fn 注入，不觸網）。"""
import json
from datetime import date

import pytest

from scripts.watchlist_snapshot import main, parse_watchlist
from spark.filet.leaderboard import DEFAULT_WATCHLIST

STATE = {"marginSummary": {"accountValue": "10", "totalMarginUsed": "1",
                           "totalNtlPos": "5"},
         "withdrawable": "4", "assetPositions": []}


def test_parse_watchlist_default():
    assert parse_watchlist(None) == list(DEFAULT_WATCHLIST)
    assert parse_watchlist("") == list(DEFAULT_WATCHLIST)


def test_parse_watchlist_normalizes_and_dedupes():
    a = "0x" + "AB" * 20
    b = "0x" + "cd" * 20
    got = parse_watchlist(f" {a} , {b}, {a.lower()} ")
    assert got == [a.lower(), b]  # 小寫、去空白、去重保序


def test_parse_watchlist_rejects_bad_address():
    """格式錯大聲整批失敗（工程原則 3）——寧可 cron 告警也不靜默漏 leader。"""
    with pytest.raises(ValueError):
        parse_watchlist("0x123,not-an-address")


def test_main_writes_snapshot_and_exits_0(tmp_path, capsys):
    env = {"FILET_LEADER_WATCHLIST": "0x" + "ab" * 20,
           "FILET_DATA_DIR": str(tmp_path)}
    with pytest.raises(SystemExit) as ei:
        main(state_fn=lambda a: STATE, today=date(2026, 7, 18), env=env)
    assert ei.value.code == 0
    out = tmp_path / "leaderboard" / "watchlist" / "2026-07-18.json"  # 定案 6/8 路徑
    data = json.loads(out.read_text())
    assert data["rows"][0]["address"] == "0x" + "ab" * 20
    assert "2026-07-18.json" in capsys.readouterr().err


def test_main_exit_1_when_any_address_fails(tmp_path):
    """定案 10：error_count > 0 → exit 1（systemd 顯示 failed），快照檔仍已寫出。"""
    def state_fn(addr):
        raise ConnectionError("down")

    env = {"FILET_LEADER_WATCHLIST": "0x" + "ab" * 20, "FILET_DATA_DIR": str(tmp_path)}
    with pytest.raises(SystemExit) as ei:
        main(state_fn=state_fn, today=date(2026, 7, 18), env=env)
    assert ei.value.code == 1
    data = json.loads((tmp_path / "leaderboard" / "watchlist" / "2026-07-18.json").read_text())
    assert data["error_count"] == 1


def test_main_idempotent_same_day_overwrite(tmp_path):
    env = {"FILET_LEADER_WATCHLIST": "0x" + "ab" * 20, "FILET_DATA_DIR": str(tmp_path)}
    for _ in range(2):
        with pytest.raises(SystemExit):
            main(state_fn=lambda a: STATE, today=date(2026, 7, 18), env=env)
    files = list((tmp_path / "leaderboard" / "watchlist").glob("*"))
    assert [f.name for f in files] == ["2026-07-18.json"]
