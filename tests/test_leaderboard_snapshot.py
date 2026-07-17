"""tests/test_leaderboard_snapshot.py
純函式測試（snapshot_from_rows / append_snapshot）＋ main() 注入 fetcher 的離線 wiring 測試。
Stub row 形狀比照 scripts/leaderboard_snapshot.py 模組 docstring 記載的實測回應
（ethAddress / accountValue / windowPerformances），不憑空造欄位。"""
import json
import logging
from datetime import date

import scripts.leaderboard_snapshot as lb


def _row(addr: str, account_value: str, day_pnl: str, *, week_pnl: str = "0") -> dict:
    """比照真實 HL leaderboard 回應形狀組出一筆 stub row。"""
    return {
        "ethAddress": addr,
        "accountValue": account_value,
        "windowPerformances": [
            ["day", {"pnl": day_pnl, "roi": "0.01", "vlm": "1000"}],
            ["week", {"pnl": week_pnl, "roi": "0.02", "vlm": "5000"}],
            ["month", {"pnl": "0", "roi": "0", "vlm": "0"}],
            ["allTime", {"pnl": "0", "roi": "0", "vlm": "0"}],
        ],
        "prize": 0,
        "displayName": None,
    }


def test_snapshot_from_rows_sorts_desc_by_pnl_and_decimal_izes():
    rows = [
        _row("0xaaa", "1000.5", "10.1"),
        _row("0xbbb", "2000.25", "99.9"),
        _row("0xccc", "500", "-5.5"),
    ]
    snap = lb.snapshot_from_rows(rows, date(2026, 7, 17))
    assert [r["address"] for r in snap["rows"]] == ["0xbbb", "0xaaa", "0xccc"]
    assert snap["rows"][0]["pnl"] == "99.9"
    assert snap["rows"][0]["account_value"] == "2000.25"
    assert snap["day"] == "2026-07-17"
    assert snap["skipped_count"] == 0
    assert snap["row_count"] == 3


def test_snapshot_from_rows_truncates_to_top_n():
    rows = [_row(f"0x{i:03d}", "100", str(i)) for i in range(10)]
    snap = lb.snapshot_from_rows(rows, date(2026, 7, 17), top_n=3)
    assert snap["row_count"] == 3
    # 降冪：pnl 9,8,7 排最前
    assert [r["pnl"] for r in snap["rows"]] == ["9", "8", "7"]


def test_snapshot_from_rows_empty_rows_no_crash():
    snap = lb.snapshot_from_rows([], date(2026, 7, 17))
    assert snap["rows"] == []
    assert snap["row_count"] == 0
    assert snap["skipped_count"] == 0


def test_snapshot_from_rows_skips_missing_field_loudly(caplog):
    good = _row("0xaaa", "1000", "10")
    missing_account_value = {
        "ethAddress": "0xbad1",
        "windowPerformances": good["windowPerformances"],
    }
    missing_window = {"ethAddress": "0xbad2", "accountValue": "1", "windowPerformances": []}
    rows = [good, missing_account_value, missing_window]
    with caplog.at_level(logging.WARNING):
        snap = lb.snapshot_from_rows(rows, date(2026, 7, 17))
    assert snap["skipped_count"] == 2
    assert snap["row_count"] == 1
    assert snap["rows"][0]["address"] == "0xaaa"
    assert len(caplog.records) == 2
    assert "跳過" in caplog.text


def test_snapshot_from_rows_skips_nan_pnl_without_crashing_batch(caplog):
    """pnl='NaN' 是合法 Decimal 建構、不拋例外，卻會在排序比較時觸發
    InvalidOperation 崩潰整批——必須在來源處當格式錯誤跳過，其餘正常 row 照常輸出。"""
    rows = [
        _row("0xaaa", "1000", "10"),
        _row("0xnan", "500", "NaN"),
        _row("0xbbb", "2000", "20"),
    ]
    with caplog.at_level(logging.WARNING):
        snap = lb.snapshot_from_rows(rows, date(2026, 7, 17))
    assert snap["skipped_count"] == 1
    assert snap["row_count"] == 2
    assert [r["address"] for r in snap["rows"]] == ["0xbbb", "0xaaa"]
    assert "0xnan" not in [r["address"] for r in snap["rows"]]
    assert "非 finite" in caplog.text


def test_snapshot_from_rows_skips_infinity_account_value(caplog):
    """account_value='Infinity' 同樣是合法建構的非 finite 值 → 跳過。"""
    rows = [_row("0xaaa", "1000", "10"), _row("0xinf", "Infinity", "5")]
    with caplog.at_level(logging.WARNING):
        snap = lb.snapshot_from_rows(rows, date(2026, 7, 17))
    assert snap["skipped_count"] == 1
    assert snap["row_count"] == 1
    assert snap["rows"][0]["address"] == "0xaaa"


def test_snapshot_from_rows_skips_non_numeric_pnl_string(caplog):
    """pnl 是非數字字串（Decimal 建構就拋 InvalidOperation）→ 跳過，不炸。"""
    rows = [_row("0xaaa", "1000", "10"), _row("0xbad", "500", "not-a-number")]
    with caplog.at_level(logging.WARNING):
        snap = lb.snapshot_from_rows(rows, date(2026, 7, 17))
    assert snap["skipped_count"] == 1
    assert snap["row_count"] == 1
    assert snap["rows"][0]["address"] == "0xaaa"


def test_snapshot_from_rows_skips_row_missing_the_day_window(caplog):
    """windowPerformances 存在但缺 'day' 窗（只有 week/month）→ 跳過。"""
    row_no_day = {
        "ethAddress": "0xnoday",
        "accountValue": "1000",
        "windowPerformances": [
            ["week", {"pnl": "1", "roi": "0", "vlm": "0"}],
            ["month", {"pnl": "2", "roi": "0", "vlm": "0"}],
        ],
    }
    rows = [_row("0xaaa", "1000", "10"), row_no_day]
    with caplog.at_level(logging.WARNING):
        snap = lb.snapshot_from_rows(rows, date(2026, 7, 17))
    assert snap["skipped_count"] == 1
    assert snap["row_count"] == 1
    assert snap["rows"][0]["address"] == "0xaaa"
    assert "缺 'day'" in caplog.text


def test_append_snapshot_writes_expected_path_and_content(tmp_path):
    snap = lb.snapshot_from_rows([_row("0xaaa", "1000", "10")], date(2026, 7, 17))
    out_path = lb.append_snapshot(tmp_path, snap)
    assert out_path == tmp_path / "2026-07-17.json"
    written = json.loads(out_path.read_text())
    assert written["day"] == "2026-07-17"
    assert written["rows"][0]["address"] == "0xaaa"


def test_append_snapshot_same_day_rerun_overwrites_idempotently(tmp_path):
    day = date(2026, 7, 17)
    snap1 = lb.snapshot_from_rows([_row("0xaaa", "1000", "10")], day)
    path1 = lb.append_snapshot(tmp_path, snap1)

    snap2 = lb.snapshot_from_rows([_row("0xbbb", "2000", "20")], day)
    path2 = lb.append_snapshot(tmp_path, snap2)

    assert path1 == path2
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    written = json.loads(path2.read_text())
    assert written["rows"][0]["address"] == "0xbbb"


def test_main_uses_injected_fetch_fn_and_writes_snapshot(tmp_path):
    """main() 以注入的 fetch_fn 隔離網路，wiring 一路到落檔皆離線可驗。"""
    calls = []

    def fake_fetch(network: str) -> list[dict]:
        calls.append(network)
        return [_row("0xaaa", "1000", "42")]

    try:
        lb.main(fetch_fn=fake_fetch, out_dir=tmp_path, network="testnet",
               today=date(2026, 7, 17))
    except SystemExit as e:
        assert e.code == 0
    else:
        raise AssertionError("main() should raise SystemExit(0)")

    assert calls == ["testnet"]
    out_path = tmp_path / "2026-07-17.json"
    assert out_path.exists()
    written = json.loads(out_path.read_text())
    assert written["rows"][0]["address"] == "0xaaa"


def test_fetch_leaderboard_rejects_unknown_network():
    import pytest
    with pytest.raises(ValueError):
        lb.fetch_leaderboard("unknownnet")


def test_module_import_and_main_are_zero_network():
    """import 已在本檔頂部發生（受 conftest 全域斷網 fixture 保護，若有頂層網路呼叫
    這裡就已經炸了）；本測試只再次斷言關鍵物件存在，記錄這個不變量。"""
    assert callable(lb.main)
    assert callable(lb.fetch_leaderboard)
    assert callable(lb.snapshot_from_rows)
    assert callable(lb.append_snapshot)


def test_main_out_dir_honors_filet_data_dir(tmp_path, monkeypatch):
    """M3 additive：FILET_DATA_DIR 設定時全站快照落 <dir>/leaderboard/（unset 行為不變）。"""
    monkeypatch.setenv("FILET_DATA_DIR", str(tmp_path))
    from scripts.leaderboard_snapshot import main
    rows = [{"ethAddress": "0x" + "ab" * 20, "accountValue": "1",
             "windowPerformances": [["day", {"pnl": "2"}]]}]
    import pytest as _pytest
    from datetime import date as _date
    with _pytest.raises(SystemExit):
        main(fetch_fn=lambda network: rows, today=_date(2026, 7, 18))
    assert (tmp_path / "leaderboard" / "2026-07-18.json").exists()
