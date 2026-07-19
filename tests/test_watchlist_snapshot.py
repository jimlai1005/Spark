"""tests/test_watchlist_snapshot.py — watchlist 快照 CLI（state_fn 注入，不觸網）。"""
import json
from datetime import date

import pytest

from scripts.watchlist_snapshot import main, parse_watchlist
from spark.filet.leaderboard import DEFAULT_WATCHLIST

STATE = {"marginSummary": {"accountValue": "10", "totalMarginUsed": "1",
                           "totalNtlPos": "5"},
         "withdrawable": "4", "assetPositions": []}


def _NO_WHITELIST(tmp_path) -> dict:
    """把白名單路徑釘在 tmp 的不存在檔上。

    抓取對象改成「白名單 ∪ env」之後（見本檔下半部的單一來源測試），未指定
    FILET_LEADERS_PATH 的測試會落到 DEFAULT_LEADERS_PATH ＝ **repo 工作樹裡的**
    `var/filet/leaders.json`。那個檔現在不存在所以測試會過——但只要有人在本機建了
    它，這些測試就會靜默改抓真實 leader 位址（工程原則 4：測試不得依賴工作樹的
    真實檔案）。顯式釘死，讓「有沒有那個檔」對測試結果不再有任何影響。
    """
    return {"FILET_LEADERS_PATH": str(tmp_path / "no-such-leaders.json")}


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
           "FILET_DATA_DIR": str(tmp_path), **_NO_WHITELIST(tmp_path)}
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

    env = {"FILET_LEADER_WATCHLIST": "0x" + "ab" * 20, "FILET_DATA_DIR": str(tmp_path),
           **_NO_WHITELIST(tmp_path)}
    with pytest.raises(SystemExit) as ei:
        main(state_fn=state_fn, today=date(2026, 7, 18), env=env)
    assert ei.value.code == 1
    data = json.loads((tmp_path / "leaderboard" / "watchlist" / "2026-07-18.json").read_text())
    assert data["error_count"] == 1


def test_main_idempotent_same_day_overwrite(tmp_path):
    env = {"FILET_LEADER_WATCHLIST": "0x" + "ab" * 20, "FILET_DATA_DIR": str(tmp_path),
           **_NO_WHITELIST(tmp_path)}
    for _ in range(2):
        with pytest.raises(SystemExit):
            main(state_fn=lambda a: STATE, today=date(2026, 7, 18), env=env)
    files = list((tmp_path / "leaderboard" / "watchlist").glob("*"))
    assert [f.name for f in files] == ["2026-07-18.json"]


# --- ⭐ 單一來源：白名單即抓取對象（2026-07-19 缺口修補） -------------------
# 修補前：抓取對象只來自 FILET_LEADER_WATCHLIST，與 leaders.json **無任何同步**。
# 白名單新增一個 leader 卻忘了改 env → 目錄頁永遠 null 統計且零告警。
from scripts.watchlist_snapshot import resolve_targets  # noqa: E402

_W1 = "0x" + "11" * 20
_W2 = "0x" + "22" * 20
_EXTRA = "0x" + "33" * 20


def write_leaders(tmp_path, entries) -> str:
    p = tmp_path / "leaders.json"
    p.write_text(json.dumps({"leaders": entries}))
    return str(p)


def test_whitelist_is_the_fetch_target(tmp_path):
    """⭐ 白名單新增 leader → 抓取對象自動涵蓋，不必同步任何 env。"""
    path = write_leaders(tmp_path, [{"address": _W1, "name": "A"},
                                    {"address": _W2, "name": "B"}])
    addrs, err = resolve_targets({"FILET_LEADERS_PATH": path})
    assert err is None
    assert addrs == [_W1, _W2]


def test_whitelist_includes_governance_disabled_leaders(tmp_path):
    """已撤銷／不收新客戶的 leader 仍要繼續記錄：序列斷了無法回填（HL day 窗只留 24h），
    而治理旗標決定的是「客戶能不能選他」，不是「要不要記錄他」。"""
    path = write_leaders(tmp_path, [{"address": _W1, "name": "A", "enabled": False},
                                    {"address": _W2, "name": "B",
                                     "accepting_new": False}])
    addrs, err = resolve_targets({"FILET_LEADERS_PATH": path})
    assert err is None and addrs == [_W1, _W2]


def test_env_watchlist_is_union_not_replacement(tmp_path):
    """env 保留為**額外**候選（尚未進白名單的研究對象），與白名單取聯集、去重保序。"""
    path = write_leaders(tmp_path, [{"address": _W1, "name": "A"}])
    addrs, err = resolve_targets({"FILET_LEADERS_PATH": path,
                                  "FILET_LEADER_WATCHLIST": f"{_EXTRA},{_W1}"})
    assert err is None and addrs == [_W1, _EXTRA]      # 白名單優先、_W1 不重複


def test_env_alone_does_not_drag_in_builtin_default(tmp_path):
    """env 有值時，內建 DEFAULT_WATCHLIST 不得混進來（parse_watchlist 的 default 覆寫）。"""
    addrs, err = resolve_targets({"FILET_LEADERS_PATH": str(tmp_path / "none.json"),
                                  "FILET_LEADER_WATCHLIST": _EXTRA})
    assert err is None and addrs == [_EXTRA]
    assert DEFAULT_WATCHLIST[0] not in addrs


def test_absent_whitelist_falls_back_to_builtin_default(tmp_path):
    """白名單檔不存在且 env 未設 → 內建預設（行為與修補前一致，不是錯誤狀態）。"""
    addrs, err = resolve_targets({"FILET_LEADERS_PATH": str(tmp_path / "none.json")})
    assert err is None and addrs == list(DEFAULT_WATCHLIST)


def test_broken_whitelist_alarms_but_still_collects(tmp_path):
    """白名單壞掉：大聲（error 字串）但**不中止**——中止會讓當天所有 leader 的
    資料點永久消失（序列無法回填）。"""
    p = tmp_path / "leaders.json"
    p.write_text("{ not json")
    addrs, err = resolve_targets({"FILET_LEADERS_PATH": str(p),
                                  "FILET_LEADER_WATCHLIST": _EXTRA})
    assert err is not None and "白名單載入失敗" in err
    assert addrs == [_EXTRA]


def test_main_records_leaders_error_and_exits_1(tmp_path):
    p = tmp_path / "leaders.json"
    p.write_text("{ not json")
    env = {"FILET_LEADERS_PATH": str(p), "FILET_LEADER_WATCHLIST": _EXTRA,
           "FILET_DATA_DIR": str(tmp_path)}
    with pytest.raises(SystemExit) as ei:
        main(state_fn=lambda a: STATE, today=date(2026, 7, 18), env=env)
    assert ei.value.code == 1
    data = json.loads((tmp_path / "leaderboard" / "watchlist"
                       / "2026-07-18.json").read_text())
    assert "白名單載入失敗" in data["leaders_error"]
    assert data["row_count"] == 1          # 擷取仍完成，資料沒丟


def test_main_snapshots_whitelisted_leaders_with_perf(tmp_path):
    """端到端（注入 fake，不觸網）：白名單的 leader 被抓到，且帶 perp 績效。"""
    path = write_leaders(tmp_path, [{"address": _W1, "name": "A"}])
    env = {"FILET_LEADERS_PATH": path, "FILET_DATA_DIR": str(tmp_path)}
    portfolio = [["perpMonth", {
        "accountValueHistory": [[0, "1000"], [40 * 86_400_000, "1100"]],
        "pnlHistory": [[0, "0"], [40 * 86_400_000, "100"]]}]]
    with pytest.raises(SystemExit) as ei:
        main(state_fn=lambda a: STATE, today=date(2026, 7, 18), env=env,
             portfolio_fn=lambda a: portfolio)
    assert ei.value.code == 0
    data = json.loads((tmp_path / "leaderboard" / "watchlist"
                       / "2026-07-18.json").read_text())
    assert [r["address"] for r in data["rows"]] == [_W1]
    assert data["rows"][0]["perf"]["windows"]["perpMonth"]["twr"] == "0.1"


def test_main_exits_1_on_perf_failure(tmp_path):
    """績效失敗也要 exit 1（systemd 顯示 failed），但規模資料仍落檔。"""
    path = write_leaders(tmp_path, [{"address": _W1, "name": "A"}])
    env = {"FILET_LEADERS_PATH": path, "FILET_DATA_DIR": str(tmp_path)}

    def boom(addr):
        raise ConnectionError("portfolio down")

    with pytest.raises(SystemExit) as ei:
        main(state_fn=lambda a: STATE, today=date(2026, 7, 18), env=env,
             portfolio_fn=boom)
    assert ei.value.code == 1
    data = json.loads((tmp_path / "leaderboard" / "watchlist"
                       / "2026-07-18.json").read_text())
    assert data["perf_error_count"] == 1 and data["rows"][0]["account_value"]
