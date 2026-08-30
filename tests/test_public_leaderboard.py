"""tests/test_public_leaderboard.py — `hl_leaderboard` 純函式 ＋
`GET /api/public/leaderboard`（M3 round2 Task 5）。

上游 stats-data 全量 JSON 絕不真連網：`fetch_leaderboard` 的 GET 一律靠
`leaderboard_get_fn` 注入假資料（沿 `HLGateway` post_fn 慣例）。全離線
（autouse socket-ban，見 conftest.py）。
"""
import socket
import threading
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from spark.publicapi import hl_leaderboard
from spark.publicapi.app import create_app
from spark.publicapi.hl_leaderboard import (LeaderboardCache, WINDOWS, top_rows)
from spark.publicapi.store import ApiStore
from tests.publicapi_helpers import FakeKeysvc, make_cfg

_REAL_SOCKET = socket.socket  # import 期捕捉，早於 autouse 斷網 fixture（沿既有慣例）


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    """TestClient 的 anyio 事件迴圈需本機 socketpair；上游 GET 全靠 get_fn 注入
    假資料，結構上不會真連網（見 `hl_leaderboard.LeaderboardCache` 呼叫點）。"""
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


_A = "0x" + "a1" * 20
_B = "0x" + "b2" * 20
_C = "0x" + "c3" * 20


def _row(address, display_name=None, account_value="1000.00", **window_pnl):
    """`window_pnl` 例：`day="12.5"` → 該 window 的 pnl（roi/vlm 給固定值，
    測試不關心）。缺的 window 不出現在 `windowPerformances`。"""
    perfs = [[w, {"pnl": pnl, "roi": "0.05", "vlm": "100.0"}]
            for w, pnl in window_pnl.items()]
    return {"ethAddress": address, "accountValue": account_value,
           "windowPerformances": perfs, "prize": 0, "displayName": display_name}


def _payload(*rows):
    return {"leaderboardRows": list(rows)}


# ============================================================
# top_rows：純函式排序／裁切
# ============================================================

def test_top_rows_sorts_desc_by_window_pnl():
    payload = _payload(
        _row(_A, month="10.00"),
        _row(_B, month="50.00"),
        _row(_C, month="30.00"),
    )
    out = top_rows(payload, "month", limit=100)
    assert [r["address"] for r in out] == [_B, _C, _A]


def test_top_rows_uses_decimal_not_float_for_precision():
    """兩個大整數 pnl 在 float 下會被判成相等（2**53 邊界丟精度），Decimal 下
    不會——回歸測試工程原則「金額比較不得用 float」。"""
    big_hi = "9007199254740993"
    big_lo = "9007199254740992"
    assert float(big_hi) == float(big_lo)          # 錨例：float 在此確實會相等
    assert Decimal(big_hi) > Decimal(big_lo)        # Decimal 下仍可分辨
    payload = _payload(_row(_A, month=big_lo), _row(_B, month=big_hi))
    out = top_rows(payload, "month", limit=100)
    assert [r["address"] for r in out] == [_B, _A]


def test_top_rows_respects_limit():
    payload = _payload(*[_row(f"0x{i:040x}", month=str(i)) for i in range(10)])
    out = top_rows(payload, "month", limit=3)
    assert len(out) == 3
    assert [r["pnl"] for r in out] == ["9", "8", "7"]


def test_top_rows_missing_window_sorts_last_not_crash():
    payload = _payload(
        _row(_A, month="5.00"),
        _row(_B, day="999.00"),          # 沒有 month 資料
    )
    out = top_rows(payload, "month", limit=100)
    assert [r["address"] for r in out] == [_A, _B]
    assert out[1]["pnl"] is None


def test_top_rows_rejects_unknown_window():
    with pytest.raises(ValueError):
        top_rows(_payload(_row(_A, month="1")), "not-a-window", limit=10)


def test_top_rows_output_shape():
    payload = _payload(_row(_A, display_name="Alice", account_value="1234.5", month="10.00"))
    out = top_rows(payload, "month", limit=10)
    assert out == [{"address": _A, "display_name": "Alice", "account_value": "1234.5",
                   "pnl": "10.00", "roi": "0.05", "vlm": "100.0"}]


def test_top_rows_ignores_entries_without_address():
    payload = _payload({"windowPerformances": []}, _row(_A, month="1.00"))
    out = top_rows(payload, "month", limit=10)
    assert [r["address"] for r in out] == [_A]


def test_windows_whitelist_is_the_four_documented_periods():
    assert WINDOWS == ("day", "week", "month", "allTime")


def test_top_rows_nan_pnl_sorts_last_not_crash():
    """[S3] `Decimal("NaN")` 建構不會炸，但拿去跟其他 Decimal 排序比較是未定義
    行為——`_pnl_sort_key` 必須把它攔下來，排到最後，不得讓整份排序炸掉或亂序。"""
    payload = _payload(_row(_A, month="5.00"), _row(_B, month="NaN"))
    out = top_rows(payload, "month", limit=100)
    assert [r["address"] for r in out] == [_A, _B]


# ============================================================
# LeaderboardCache：single-flight（[C2]）
# ============================================================

def test_cache_single_flight_first_fetch_concurrent_callers_share_one_download():
    """首次抓取（無舊值可回退）：兩個並發呼叫必須共用同一次下載結果，
    `get_fn` 只能被觸發一次，兩邊拿到同一份資料。"""
    calls = {"n": 0}
    started = threading.Event()
    release = threading.Event()

    def get_fn(url):
        calls["n"] += 1
        started.set()
        assert release.wait(timeout=5), "release 逾時未被觸發"
        return _payload(_row(_A, month="1.00"))

    cache = LeaderboardCache(now_fn=lambda: 1000.0, get_fn=get_fn, sleep_fn=lambda s: None)
    results: list = []

    def worker():
        results.append(cache.get())

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    assert started.wait(timeout=5), "第一條 thread 未進入 fetch"
    t2.start()
    time.sleep(0.05)  # 讓 t2 有機會排進「等待中」分支，而不是也去搶著當 fetcher
    release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert calls["n"] == 1
    assert len(results) == 2
    assert results[0] is not None and results[1] is not None
    assert results[0] == results[1]


def test_cache_stale_value_returned_immediately_without_waiting_for_refresh():
    """TTL 過期後、有舊值可回退：背景那條 thread 卡在下載中時，另一個呼叫必須
    立刻拿到舊值，不被下載中的那條 thread 卡住（不阻塞語意）。"""
    calls = {"n": 0}
    blocked = threading.Event()
    release = threading.Event()

    def get_fn(url):
        n = calls["n"]
        calls["n"] += 1
        if n == 0:
            return _payload(_row(_A, month="1.00"))  # 首抓，立即回傳
        blocked.set()
        assert release.wait(timeout=5), "release 逾時未被觸發"
        return _payload(_row(_A, month="2.00"))

    clock = {"t": 1000.0}
    cache = LeaderboardCache(now_fn=lambda: clock["t"], get_fn=get_fn,
                             sleep_fn=lambda s: None, ttl_s=600.0)
    first = cache.get()
    assert first is not None

    clock["t"] += 601.0  # TTL 過期
    result_holder = {}

    def refresher():
        result_holder["v"] = cache.get()

    t = threading.Thread(target=refresher)
    t.start()
    assert blocked.wait(timeout=5), "背景抓取未進入卡住狀態"

    start = time.monotonic()
    stale = cache.get()
    elapsed = time.monotonic() - start

    assert stale == first
    assert elapsed < 0.5, f"讀取舊值被卡住了（耗時 {elapsed}s）"

    release.set()
    t.join(timeout=5)
    assert calls["n"] == 2
    assert result_holder["v"] is not None


# ============================================================
# LeaderboardCache.top_rows：排序記憶化（[8b-6]）
# ============================================================

def test_top_rows_same_generation_second_call_does_not_resort(monkeypatch):
    """同一世代（同一份 payload）第二次呼叫 `top_rows` 必須命中記憶化——
    底層的 `_sorted_rows`（真正做排序的函式）只能被呼叫一次。"""
    calls = {"n": 0}
    orig = hl_leaderboard._sorted_rows

    def counting(payload, window):
        calls["n"] += 1
        return orig(payload, window)
    monkeypatch.setattr(hl_leaderboard, "_sorted_rows", counting)

    payload = _payload(_row(_A, month="1.00"), _row(_B, month="2.00"))
    cache = LeaderboardCache(now_fn=lambda: 1000.0, get_fn=lambda url: payload,
                             sleep_fn=lambda s: None)
    out1 = cache.top_rows("month", limit=10)
    out2 = cache.top_rows("month", limit=10)
    assert calls["n"] == 1
    assert out1 == out2 == [
        {"address": _B, "display_name": None, "account_value": "1000.00",
         "pnl": "2.00", "roi": "0.05", "vlm": "100.0"},
        {"address": _A, "display_name": None, "account_value": "1000.00",
         "pnl": "1.00", "roi": "0.05", "vlm": "100.0"},
    ]


def test_top_rows_new_generation_recomputes_and_returns_fresh_order(monkeypatch):
    """TTL 過期後成功換代（新的 payload 物件）：記憶化必須失效，`top_rows`
    重新排序一次並回傳新世代的順序，不是沿用舊世代快取的排序結果。"""
    calls = {"n": 0}
    orig = hl_leaderboard._sorted_rows

    def counting(payload, window):
        calls["n"] += 1
        return orig(payload, window)
    monkeypatch.setattr(hl_leaderboard, "_sorted_rows", counting)

    gen = {"n": 1}

    def get_fn(url):
        if gen["n"] == 1:
            return _payload(_row(_A, month="1.00"), _row(_B, month="2.00"))
        return _payload(_row(_A, month="99.00"), _row(_B, month="2.00"))

    clock = {"t": 1000.0}
    cache = LeaderboardCache(now_fn=lambda: clock["t"], get_fn=get_fn,
                             sleep_fn=lambda s: None, ttl_s=600.0)

    out1 = cache.top_rows("month", limit=10)
    assert [r["address"] for r in out1] == [_B, _A]  # B(2.00) > A(1.00)
    assert calls["n"] == 1

    clock["t"] += 601.0  # 超過 TTL，觸發重抓
    gen["n"] = 2
    out2 = cache.top_rows("month", limit=10)
    assert [r["address"] for r in out2] == [_A, _B]  # 換代後 A(99.00) > B(2.00)
    assert calls["n"] == 2  # 新世代確實重排了一次，不是誤用舊世代的記憶化結果


def test_top_rows_limit_over_memoized_cap_raises():
    """[8b-6] 記憶化只存前 100 筆（公開端點本就不允許 limit > 100）——呼叫端若
    傳超過上限的 limit，fail-fast 拋 `ValueError`，不得悄悄回傳被砍短的清單。"""
    payload = _payload(_row(_A, month="1.00"))
    cache = LeaderboardCache(now_fn=lambda: 1000.0, get_fn=lambda url: payload,
                             sleep_fn=lambda s: None)
    with pytest.raises(ValueError):
        cache.top_rows("month", limit=101)


# ============================================================
# LeaderboardCache：TTL ＋ fail-open 到舊值
# ============================================================

def test_cache_returns_fetched_value_and_reuses_within_ttl():
    calls = {"n": 0}

    def get_fn(url):
        calls["n"] += 1
        return _payload(_row(_A, month="1.00"))

    clock = {"t": 1000.0}
    cache = LeaderboardCache(now_fn=lambda: clock["t"], get_fn=get_fn,
                             sleep_fn=lambda s: None, ttl_s=600.0)
    assert cache.get() is not None
    assert calls["n"] == 1
    clock["t"] += 300.0                     # 仍在 TTL 內
    assert cache.get() is not None
    assert calls["n"] == 1                  # 快取命中，未再打上游

    clock["t"] += 301.0                     # 累計 601s，超過 TTL
    assert cache.get() is not None
    assert calls["n"] == 2


def test_cache_fails_open_to_stale_value_on_refetch_failure(caplog):
    state = {"fail": False}

    def get_fn(url):
        if state["fail"]:
            raise ConnectionError("upstream down")
        return _payload(_row(_A, month="1.00"))

    clock = {"t": 1000.0}
    cache = LeaderboardCache(now_fn=lambda: clock["t"], get_fn=get_fn,
                             sleep_fn=lambda s: None, ttl_s=600.0)
    first = cache.get()
    assert first is not None

    clock["t"] += 601.0
    state["fail"] = True
    with caplog.at_level("ERROR"):
        second = cache.get()
    assert second == first                  # 舊值續用，不是 None／不是拋例外
    assert "leaderboard" in caplog.text


def test_cache_first_fetch_failure_has_no_stale_value_to_fall_back_to():
    def get_fn(url):
        raise ConnectionError("upstream down")

    cache = LeaderboardCache(now_fn=lambda: 1000.0, get_fn=get_fn,
                             sleep_fn=lambda s: None)
    assert cache.get() is None


# ============================================================
# 端點：GET /api/public/leaderboard
# ============================================================

def _app(tmp_path, get_fn, now_fn=None):
    cfg = make_cfg(tmp_path)
    store = ApiStore(cfg.db_path)
    keysvc = FakeKeysvc()
    kw = {} if now_fn is None else {"now_fn": now_fn}
    return create_app(cfg, store, keysvc, hl=None, leaderboard_get_fn=get_fn, **kw)


def _client(app):
    return TestClient(app, base_url="https://testserver")


def test_endpoint_default_window_and_limit(tmp_path):
    def get_fn(url):
        return _payload(*[_row(f"0x{i:040x}", month=str(i)) for i in range(150)])
    app = _app(tmp_path, get_fn)
    r = _client(app).get("/api/public/leaderboard")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window"] == "month"
    assert len(body["rows"]) == 100         # 預設 limit 100
    assert set(body) == {"window", "updated_at", "rows"}


def test_endpoint_custom_window_and_limit(tmp_path):
    def get_fn(url):
        return _payload(_row(_A, day="5.00", week="9.00"), _row(_B, day="1.00", week="20.00"))
    app = _app(tmp_path, get_fn)
    r = _client(app).get("/api/public/leaderboard", params={"window": "week", "limit": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window"] == "week"
    assert len(body["rows"]) == 1
    assert body["rows"][0]["address"] == _B    # week pnl 20 > 9


def test_endpoint_rejects_bad_window(tmp_path):
    app = _app(tmp_path, lambda url: _payload())
    r = _client(app).get("/api/public/leaderboard", params={"window": "year"})
    assert r.status_code == 422


@pytest.mark.parametrize("limit", [0, 101, -1])
def test_endpoint_rejects_bad_limit(tmp_path, limit):
    app = _app(tmp_path, lambda url: _payload())
    r = _client(app).get("/api/public/leaderboard", params={"limit": limit})
    assert r.status_code == 422


def test_endpoint_503_when_upstream_never_succeeded(tmp_path):
    def get_fn(url):
        raise ConnectionError("upstream down")
    app = _app(tmp_path, get_fn)
    r = _client(app).get("/api/public/leaderboard")
    assert r.status_code == 503


def test_endpoint_no_auth_required_and_no_cookie_side_effect(tmp_path):
    app = _app(tmp_path, lambda url: _payload(_row(_A, month="1.00")))
    r = _client(app).get("/api/public/leaderboard")
    assert r.status_code == 200
    assert r.cookies.get("filet_session") is None


def test_endpoint_updated_at_is_fetch_completion_time_not_request_time(tmp_path):
    """[8b-4] `updated_at` 必須是資料**實際抓取完成**的時間戳，不是請求當下的
    時間——fail-open 續用舊值時尤其重要：舊寫法（`int(now_fn())`）會讓客戶端
    誤以為資料剛更新過，實際上可能是十分鐘前抓到、上游持續故障中的舊值。"""
    state = {"fail": False}

    def get_fn(url):
        if state["fail"]:
            raise ConnectionError("upstream down")
        return _payload(_row(_A, month="1.00"))

    clock = {"t": 1_000_000.0}
    app = _app(tmp_path, get_fn, now_fn=lambda: clock["t"])
    c = _client(app)

    r1 = c.get("/api/public/leaderboard")
    assert r1.status_code == 200, r1.text
    assert r1.json()["updated_at"] == 1_000_000  # 首抓完成時間

    clock["t"] += 700.0  # 超過 600s TTL，觸發重抓；但上游這次故障
    state["fail"] = True
    r2 = c.get("/api/public/leaderboard")
    assert r2.status_code == 200, r2.text
    # fail-open 續用舊值：updated_at 仍是「上一次成功抓取完成」的時間戳
    # （1_000_000），不是這次請求當下的時間（1_000_700）。
    assert r2.json()["updated_at"] == 1_000_000
