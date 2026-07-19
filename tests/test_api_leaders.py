"""tests/test_api_leaders.py — GET /api/leaders（客戶選 leader 的目錄端點）。

盯住三件事：(1) 不可選的 leader **連存在都不外流**；(2) 統計拿不到時目錄仍可用；
(3) 投影白名單不夾帶未列舉欄位（尤其是 enabled/accepting_new 兩個治理旗標）。
"""
import json
import socket

import pytest
from fastapi.testclient import TestClient

from tests.publicapi_helpers import login, make_app, make_cfg

_REAL_SOCKET = socket.socket  # import 期捕捉，早於 autouse 斷網 fixture（沿 test_api_auth）


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    """TestClient 的 anyio 事件迴圈需本機 socketpair（self-pipe，不出網）。
    HL/keysvc 全為注入 fake——測試內無任何可觸網的程式路徑。"""
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _client(app):
    return TestClient(app, base_url="https://testserver")  # secure cookie 需 https scheme


_A = "0x" + "a1" * 20   # 正常營運
_B = "0x" + "b2" * 20   # 例行下架（accepting_new=false）
_C = "0x" + "c3" * 20   # 安全撤銷（enabled=false）

# 端點承諾的 leader 物件欄位集（多一個少一個都要有人主動改這行）
# 2026-07-19：新增 `performance`（perp 基準績效，獨立子物件——刻意不與上面的**規模**
# 欄位平鋪在同一層，見 app.py 的 _LEADER_PERF_WINDOWS 註解）。
_EXPECTED_KEYS = {"address", "name", "description",
                  "account_value", "total_ntl_pos", "unrealized_pnl",
                  "position_count", "performance"}
# 回應信封的鍵集（同上，改動要主動改這行）
_EXPECTED_ENVELOPE = {"leaders", "stats_available", "stats_day", "stats_as_of", "note",
                      "performance_available", "performance_basis",
                      "performance_windows", "performance_notes"}


def write_leaders(tmp_path, entries) -> str:
    p = tmp_path / "leaders.json"
    p.write_text(json.dumps({"leaders": entries}))
    return str(p)


def write_snapshot(tmp_path, rows, day="2026-07-19",
                   generated_at="2026-07-19T00:10:00+00:00") -> str:
    d = tmp_path / "watchlist"
    d.mkdir(parents=True, exist_ok=True)
    # perf_source 跟著列上有沒有 perf 走，鏡像 snapshot_watchlist 的真實行為：
    # 有抓績效才寫來源標記，沒抓就是 None（＝「cron 未啟用績效」，與「抓了但失敗」
    # 是兩件事，見 leaderboard.snapshot_watchlist）。
    perf_source = ("portfolio(perp windows)"
                   if any(isinstance(r, dict) and "perf" in r for r in rows) else None)
    (d / f"{day}.json").write_text(json.dumps({
        "day": day, "generated_at": generated_at, "source": "clearinghouseState",
        "perf_source": perf_source,
        "row_count": len(rows), "error_count": 0, "perf_error_count": 0,
        "rows": rows}))
    return str(d)


def stat_row(addr, account_value="10000.5"):
    return {"address": addr, "account_value": account_value,
            "total_margin_used": "2000", "total_ntl_pos": "50000",
            "withdrawable": "8000.5", "unrealized_pnl": "125.25",
            "position_count": 3}


def make_leader_app(tmp_path, entries, snapshot_rows=None, *, with_snapshot=True):
    """cfg 指向 tmp 的白名單與快照目錄；with_snapshot=False 則指向不存在的目錄。"""
    wl_dir = (write_snapshot(tmp_path, snapshot_rows or [])
              if with_snapshot else str(tmp_path / "missing"))
    cfg = make_cfg(tmp_path, leaders_path=write_leaders(tmp_path, entries),
                   watchlist_dir=wl_dir)
    app, *_ = make_app(tmp_path, cfg=cfg)
    return app


def test_returns_only_selectable_leaders_with_stats(tmp_path):
    app = make_leader_app(
        tmp_path,
        [{"address": _A, "name": "Alpha", "description": "穩健"}],
        snapshot_rows=[stat_row(_A)])
    c = _client(app)
    login(c)
    r = c.get("/api/leaders")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [x["address"] for x in body["leaders"]] == [_A]
    row = body["leaders"][0]
    assert row["name"] == "Alpha" and row["description"] == "穩健"
    assert row["account_value"] == "10000.5"
    assert row["unrealized_pnl"] == "125.25"
    assert row["position_count"] == 3
    assert body["stats_available"] is True
    assert body["stats_day"] == "2026-07-19"
    assert body["stats_as_of"] == "2026-07-19T00:10:00+00:00"
    assert body["note"] is None


def test_disabled_leader_is_absent_entirely(tmp_path):
    """⭐ enabled=false（安全撤銷）：連 address 都不得出現在回應任何角落。

    洩漏的代價不只是多一列——「白名單有他但你選不到」等於對外公告哪個 leader
    剛出事，而那是純內部治理資訊。
    """
    app = make_leader_app(
        tmp_path,
        [{"address": _A, "name": "Alpha"},
         {"address": _C, "name": "Charlie", "enabled": False}],
        snapshot_rows=[stat_row(_A), stat_row(_C)])
    c = _client(app)
    login(c)
    r = c.get("/api/leaders")
    assert r.status_code == 200, r.text
    assert [x["address"] for x in r.json()["leaders"]] == [_A]
    assert _C not in r.text          # 整份回應（含統計、note）都不得提及
    assert "Charlie" not in r.text


def test_not_accepting_new_leader_is_absent_entirely(tmp_path):
    """⭐ accepting_new=false（例行下架）：不收新客戶 → 目錄裡不該看得到。

    這條與上一條是兩個不同的旗標，必須各自釘住：只看 enabled 的實作會讓一個
    「名額已滿」的 leader 繼續出現在選單，客戶選了才發現不能用。
    """
    app = make_leader_app(
        tmp_path,
        [{"address": _A, "name": "Alpha"},
         {"address": _B, "name": "Bravo", "accepting_new": False}],
        snapshot_rows=[stat_row(_A), stat_row(_B)])
    c = _client(app)
    login(c)
    r = c.get("/api/leaders")
    assert r.status_code == 200, r.text
    assert [x["address"] for x in r.json()["leaders"]] == [_A]
    assert _B not in r.text
    assert "Bravo" not in r.text


def test_missing_snapshot_still_serves_directory(tmp_path):
    """快照缺失 → 清單照回、統計全 null、stats_available=false ＋ note。
    統計拿不到只是少幾個數字；回不出清單則是客戶完全無法選 leader。"""
    app = make_leader_app(
        tmp_path, [{"address": _A, "name": "Alpha"}], with_snapshot=False)
    c = _client(app)
    login(c)
    r = c.get("/api/leaders")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [x["address"] for x in body["leaders"]] == [_A]
    row = body["leaders"][0]
    assert row["account_value"] is None
    assert row["total_ntl_pos"] is None
    assert row["unrealized_pnl"] is None
    assert row["position_count"] is None      # 不得退化成 0（0 是有意義的錯訊息）
    assert body["stats_available"] is False
    assert body["stats_day"] is None and body["stats_as_of"] is None
    assert body["note"]


def test_corrupt_snapshot_degrades_but_serves_directory(tmp_path):
    """快照 JSON 壞掉 → 同缺失處理（不 raise、不 500）。"""
    d = tmp_path / "watchlist"
    d.mkdir()
    (d / "2026-07-19.json").write_text("{ not json")
    cfg = make_cfg(tmp_path,
                   leaders_path=write_leaders(tmp_path,
                                              [{"address": _A, "name": "Alpha"}]),
                   watchlist_dir=str(d))
    app, *_ = make_app(tmp_path, cfg=cfg)
    c = _client(app)
    login(c)
    r = c.get("/api/leaders")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [x["address"] for x in body["leaders"]] == [_A]
    assert body["stats_available"] is False


def test_leader_missing_from_snapshot_gets_null_stats(tmp_path):
    """leader 不在 watchlist（或該列是失敗列）→ 只有他的統計為 null，
    stats_available 仍為 true（快照本身是可用的）。"""
    app = make_leader_app(
        tmp_path,
        [{"address": _A, "name": "Alpha"}, {"address": _B, "name": "Bravo"}],
        snapshot_rows=[stat_row(_A),
                       {"address": _B, "error": "KeyError: marginSummary"}])
    c = _client(app)
    login(c)
    r = c.get("/api/leaders")
    body = r.json()
    by_addr = {x["address"]: x for x in body["leaders"]}
    assert by_addr[_A]["account_value"] == "10000.5"
    assert by_addr[_B]["account_value"] is None
    assert by_addr[_B]["position_count"] is None
    assert body["stats_available"] is True
    assert "KeyError" not in r.text        # 上游例外文字不外流給客戶


def test_requires_session(tmp_path):
    app = make_leader_app(tmp_path, [{"address": _A, "name": "Alpha"}])
    r = _client(app).get("/api/leaders")   # 未登入
    assert r.status_code == 401


def test_projection_whitelist_no_extra_fields(tmp_path):
    """⭐ 投影白名單：leader 物件的鍵集必須恰為列舉的那幾個。
    特別是 enabled／accepting_new 兩個治理旗標不得外流。"""
    app = make_leader_app(
        tmp_path,
        [{"address": _A, "name": "Alpha", "description": "穩健",
          "enabled": True, "accepting_new": True}],
        snapshot_rows=[stat_row(_A)])
    c = _client(app)
    login(c)
    body = c.get("/api/leaders").json()
    assert set(body["leaders"][0]) == _EXPECTED_KEYS
    # 快照的內部欄位不得夾帶外流（withdrawable/total_margin_used 刻意不列）
    assert "withdrawable" not in body["leaders"][0]
    assert "total_margin_used" not in body["leaders"][0]
    assert set(body) == _EXPECTED_ENVELOPE


def test_broken_allowlist_returns_503_not_empty_list(tmp_path):
    """白名單壞掉 → 503。**不得**回空清單：空清單看起來像「目前沒有 leader」的
    正常狀態，會讓一個手滑的編輯靜默變成全站無 leader（fail-fast 沿 load_leaders）。"""
    p = tmp_path / "leaders.json"
    p.write_text("{ not json")
    cfg = make_cfg(tmp_path, leaders_path=str(p),
                   watchlist_dir=write_snapshot(tmp_path, []))
    app, *_ = make_app(tmp_path, cfg=cfg)
    c = _client(app)
    login(c)
    assert c.get("/api/leaders").status_code == 503


def test_absent_allowlist_file_returns_empty_directory(tmp_path):
    """白名單檔不存在 ＝ 尚未策劃任何 leader（合法狀態，load_leaders 回 []）→ 200 空清單。"""
    cfg = make_cfg(tmp_path, leaders_path=str(tmp_path / "nope.json"),
                   watchlist_dir=write_snapshot(tmp_path, []))
    app, *_ = make_app(tmp_path, cfg=cfg)
    c = _client(app)
    login(c)
    r = c.get("/api/leaders")
    assert r.status_code == 200
    assert r.json()["leaders"] == []


def test_latest_snapshot_wins_no_stale_fallback(tmp_path):
    """多份快照 → 取最新一天（檔名 ISO 日期字典序即時序）。"""
    write_snapshot(tmp_path, [stat_row(_A, "111")], day="2026-07-18",
                   generated_at="2026-07-18T00:10:00+00:00")
    wl_dir = write_snapshot(tmp_path, [stat_row(_A, "222")], day="2026-07-19")
    cfg = make_cfg(tmp_path,
                   leaders_path=write_leaders(tmp_path,
                                              [{"address": _A, "name": "Alpha"}]),
                   watchlist_dir=wl_dir)
    app, *_ = make_app(tmp_path, cfg=cfg)
    c = _client(app)
    login(c)
    body = c.get("/api/leaders").json()
    assert body["stats_day"] == "2026-07-19"
    assert body["leaders"][0]["account_value"] == "222"


@pytest.mark.parametrize("env_key,expect_suffix", [
    ("FILET_WATCHLIST_DIR", "explicit"),
    ("FILET_DATA_DIR", "leaderboard/watchlist"),
])
def test_config_watchlist_dir_from_env(env_key, expect_suffix):
    """cron（FILET_DATA_DIR）與 API 讀同一版面；顯式 FILET_WATCHLIST_DIR 優先。"""
    from spark.publicapi.config import ApiConfig

    env = {"FILET_API_NETWORK": "testnet", "FILET_BUILDER_ADDR": "0x" + "b1" * 20,
           "FILET_SIWE_DOMAIN": "d", "FILET_SIWE_URI": "u", "FILET_API_DB": "db",
           "FILET_KEYSVC_SOCK": "s", "FILET_PENDING_PATH": "p",
           "FILET_EXCHANGE_DIR": "/var/lib/filet-exchange",
           "FILET_STATE_BASE": "/opt/filet/state",
           env_key: "/data/x" if env_key == "FILET_DATA_DIR" else "/explicit"}
    cfg = ApiConfig.from_env(env)
    assert cfg.watchlist_dir.endswith(expect_suffix)


def test_config_leaders_path_defaults_to_engine_path():
    """⭐ API 的預設白名單路徑必須就是引擎驗證用的那一份（同源，絕對路徑）。"""
    from pathlib import Path

    from spark.filet.leader_resolve import DEFAULT_LEADERS_PATH
    from spark.publicapi.config import ApiConfig

    assert ApiConfig.leaders_path == DEFAULT_LEADERS_PATH
    assert Path(ApiConfig.watchlist_dir).is_absolute()


# --- perp 績效欄位（2026-07-19） -------------------------------------------
# 快照層算好的績效如何投影到目錄頁。公式正確性歸 tests/test_leader_perf.py；
# 這裡只盯「投影有沒有把結構性保證弄丟」與「缺資料會不會 500」。
def perf_window(**over) -> dict:
    """一個 window_return 層級的績效窗（40 天 → 有 twr/MDD，**無**年化）。"""
    base = {"period": "perpMonth", "basis": "perp", "status": "ok", "reason": None,
            "disclosure_tier": "window_return", "sample_count": "3",
            "covered_days": "40.0000", "first_ts_ms": 0, "last_ts_ms": 3456000000,
            "skipped_intervals": 0, "cum_pnl": "-390", "twr": "-0.19",
            "max_drawdown": "0.19", "equity_index_len": 3,
            # 快照裡的內部欄位——不在 _LEADER_PERF_FIELDS 白名單內，不得外流
            "net_external_flow": "4900", "mdd_note": "…", "upper_bound_note": "…"}
    base.update(over)
    return base


def stat_row_with_perf(addr, windows=None, **over):
    row = stat_row(addr)
    row["perf"] = {"source": "portfolio(perp windows)",
                   "windows": windows or {"perpMonth": perf_window()}}
    row.update(over)
    return row


def test_performance_fields_surface_on_directory(tmp_path):
    app = make_leader_app(tmp_path, [{"address": _A, "name": "Alpha"}],
                          snapshot_rows=[stat_row_with_perf(_A)])
    c = _client(app)
    login(c)
    body = c.get("/api/leaders").json()
    perf = body["leaders"][0]["performance"]
    assert perf["perpMonth"]["twr"] == "-0.19"
    assert perf["perpMonth"]["max_drawdown"] == "0.19"
    assert perf["perpMonth"]["basis"] == "perp"
    # ⭐ 資料充足度必須到得了前端（誠實呈現的核心）
    assert perf["perpMonth"]["covered_days"] == "40.0000"
    assert perf["perpMonth"]["sample_count"] == "3"
    assert perf["perpMonth"]["disclosure_tier"] == "window_return"
    # ⭐ 「leader 報酬是上界非期望值」＋ basis ＋ MDD 極限，三段揭露都在信封裡
    assert body["performance_available"] is True
    assert body["performance_basis"] == "perp"
    assert "上界" in body["performance_notes"]["upper_bound"]
    assert "perp" in body["performance_notes"]["basis"]
    assert "低估" in body["performance_notes"]["max_drawdown"]
    assert "covered_days" in body["performance_notes"]["sufficiency"]


def test_annualized_absence_survives_the_projection(tmp_path):
    """⭐⭐ 不足 90 天 → 投影後**仍然沒有** annualized_return 這個鍵。

    投影若寫成 `row.get(k)`，缺席的鍵會被補成 null 送到前端，而前端的 `?? 0`
    會把它變成一個數字——leader_perf 的結構性保證就在那一行退化成「前端記得檢查」。
    """
    app = make_leader_app(tmp_path, [{"address": _A, "name": "Alpha"}],
                          snapshot_rows=[stat_row_with_perf(_A)])
    c = _client(app)
    login(c)
    r = c.get("/api/leaders")
    win = r.json()["leaders"][0]["performance"]["perpMonth"]
    assert "annualized_return" not in win
    # leader 資料裡任何一層都不得出現這個鍵（信封的揭露文案會提到這個**字串**，
    # 那是刻意的說明文字，故只掃 leaders 這一段而非整份回應）。
    assert "annualized_return" not in json.dumps(r.json()["leaders"])


def test_annualized_passes_through_when_eligible(tmp_path):
    """≥90 天的窗則必須帶年化（閘門是「不足才擋」，不是「一律不給」）。"""
    win = perf_window(period="perpAllTime", covered_days="365.0000",
                      disclosure_tier="annualizable", annualized_return="0.21")
    app = make_leader_app(tmp_path, [{"address": _A, "name": "Alpha"}],
                          snapshot_rows=[stat_row_with_perf(
                              _A, windows={"perpAllTime": win})])
    c = _client(app)
    login(c)
    got = c.get("/api/leaders").json()["leaders"][0]["performance"]["perpAllTime"]
    assert got["annualized_return"] == "0.21"
    assert got["disclosure_tier"] == "annualizable"


def test_insufficiency_markers_survive_the_projection(tmp_path):
    """⭐⭐ 2026-07-19 揭露模型改版後**唯一**的警示載體：指標層級的不足標記。

    改版前「資料不足」由缺鍵承載，前端漏看就是少顯示一個欄位；改版後薄資料的
    `twr`／`annualized_return` 照樣外流，警示全靠這幾個標記。投影白名單漏掉任何
    一個 → 前端拿到一個沒有任何警示的外推數字，而畫面上完全看不出來。

    ⭐ 變異測試點：從 `_LEADER_PERF_FIELDS` 拿掉 `INSUFFICIENCY_MARKERS` → 本測試轉紅。
    """
    win = perf_window(covered_days="6.0000", disclosure_tier="pnl_only",
                      twr="0.38", max_drawdown="0",
                      twr_insufficient_data=True,
                      max_drawdown_insufficient_data=True,
                      annualized_return="365.0",
                      annualized_return_insufficient_data=True,
                      annualized_return_extrapolated_from_days="6.0000")
    app = make_leader_app(tmp_path, [{"address": _A, "name": "Alpha"}],
                          snapshot_rows=[stat_row_with_perf(
                              _A, windows={"perpMonth": win})])
    c = _client(app)
    login(c)
    got = c.get("/api/leaders").json()["leaders"][0]["performance"]["perpMonth"]
    assert got["twr"] == "0.38"                       # 數字有給
    assert got["twr_insufficient_data"] is True       # 警示也有給
    assert got["max_drawdown_insufficient_data"] is True
    assert got["annualized_return"] == "365.0"
    assert got["annualized_return_insufficient_data"] is True
    # ⭐ 前端要能寫出「由 6 天外推」——沒有這個天數，「年化 36500%」看起來像事實
    assert got["annualized_return_extrapolated_from_days"] == "6.0000"


def test_perf_projection_whitelist_no_internal_leakage(tmp_path):
    """投影白名單：快照的內部欄位（net_external_flow、各種 note）不得夾帶外流。"""
    app = make_leader_app(tmp_path, [{"address": _A, "name": "Alpha"}],
                          snapshot_rows=[stat_row_with_perf(_A)])
    c = _client(app)
    login(c)
    r = c.get("/api/leaders")
    win = r.json()["leaders"][0]["performance"]["perpMonth"]
    assert set(win) <= {"period", "basis", "status", "reason", "disclosure_tier",
                        "sample_count", "covered_days", "first_ts_ms", "last_ts_ms",
                        "skipped_intervals", "cum_pnl", "twr", "max_drawdown",
                        "annualized_return"}
    assert "net_external_flow" not in win and "equity_index_len" not in win
    # 只投影承諾的兩個窗（perpDay/perpWeek 噪音太大，不上目錄頁）
    assert set(r.json()["leaders"][0]["performance"]) <= {"perpMonth", "perpAllTime"}


def test_insufficient_window_projects_status_without_numbers(tmp_path):
    """資料不足的窗：投影出 status/reason，但**沒有**任何數值鍵（不補 0／null）。"""
    win = {"period": "perpMonth", "basis": "perp", "status": "insufficient",
           "reason": "need_at_least_two_samples", "disclosure_tier": "insufficient",
           "sample_count": 1, "covered_days": None, "skipped_intervals": 0}
    app = make_leader_app(tmp_path, [{"address": _A, "name": "Alpha"}],
                          snapshot_rows=[stat_row_with_perf(
                              _A, windows={"perpMonth": win})])
    c = _client(app)
    login(c)
    got = c.get("/api/leaders").json()["leaders"][0]["performance"]["perpMonth"]
    assert got["status"] == "insufficient" and got["reason"] == "need_at_least_two_samples"
    for k in ("cum_pnl", "twr", "max_drawdown", "annualized_return"):
        assert k not in got


def test_snapshot_without_perf_degrades_gracefully(tmp_path):
    """⚠️ 向後相容：舊格式快照（無 perf 欄）→ performance 為 null，
    既有的規模欄位與 stats_available 語意完全不變，**不 500**。"""
    app = make_leader_app(tmp_path, [{"address": _A, "name": "Alpha"}],
                          snapshot_rows=[stat_row(_A)])
    c = _client(app)
    login(c)
    r = c.get("/api/leaders")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["leaders"][0]["performance"] is None
    assert body["leaders"][0]["account_value"] == "10000.5"   # 規模欄位不受影響
    assert body["stats_available"] is True                    # 兩個旗標互相獨立
    assert body["performance_available"] is False


@pytest.mark.parametrize("perf", [
    "not-a-dict", None, {}, {"windows": "not-a-dict"}, {"windows": {}},
    {"windows": {"perpMonth": "not-a-dict"}},
])
def test_malformed_perf_does_not_500(tmp_path, perf):
    """schema 漂移／半寫的快照 → performance null，目錄照常可用（沿既有兩種降級）。"""
    row = stat_row(_A)
    row["perf"] = perf
    app = make_leader_app(tmp_path, [{"address": _A, "name": "Alpha"}],
                          snapshot_rows=[row])
    c = _client(app)
    login(c)
    r = c.get("/api/leaders")
    assert r.status_code == 200, r.text
    assert r.json()["leaders"][0]["performance"] is None


def test_missing_snapshot_leaves_performance_null(tmp_path):
    """快照整份缺失 → 規模與績效同時為 null，兩個 available 旗標都是 false。"""
    app = make_leader_app(tmp_path, [{"address": _A, "name": "Alpha"}],
                          with_snapshot=False)
    c = _client(app)
    login(c)
    body = c.get("/api/leaders").json()
    assert body["leaders"][0]["performance"] is None
    assert body["stats_available"] is False and body["performance_available"] is False
