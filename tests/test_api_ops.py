"""tests/test_api_ops.py
營運後台 /api/ops/*（admin only）：每客戶損益、收入對帳，以及 accrued 歷史序列。

⭐ 本檔的核心是 **admin 閘的結構性斷言**：/ops 是全 repo 唯一的跨客戶聚合存取模式
（其餘端點皆 session-scoped），漏掉一道閘＝任何登入者都能看到所有客戶的部位與收入。
test_all_admin_scoped_routes_are_gated 走 FastAPI 的 dependant 樹逐路由檢查——
未來新增的 /api/ops/* 端點若忘了掛閘，該測試會直接紅，不靠人記得。
"""
import json
import socket
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from spark.exchange.base import UserFill
from spark.filet.followers import FollowerRef
from spark.publicapi.ops import (customer_pnl, load_accrued_series,
                                 revenue_reconciliation)
from tests.publicapi_helpers import BUILDER, login, make_app, make_cfg

_REAL_SOCKET = socket.socket


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    """TestClient 走本機 socket；外呼仍不存在（HL/store 皆為 fake）。"""
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _client(app):
    return TestClient(app, base_url="https://testserver")


ADDR_A = "0x" + "a1" * 20
ADDR_B = "0x" + "b2" * 20
ACCT_A = "f" + "a1" * 20
ACCT_B = "f" + "b2" * 20


def _ref(acct=ACCT_A, addr=ADDR_A, label="A"):
    return FollowerRef(acct, addr, BUILDER, "testnet", label)


def _fill(sz="2", px="100", crossed=True, builder_fee="0.5", t=None):
    return UserFill(time=t or datetime(2026, 7, 19, tzinfo=timezone.utc), coin="ETH",
                    px=Decimal(px), sz=Decimal(sz), side="B", crossed=crossed,
                    oid=1, fee=Decimal("0.1"), builder_fee=Decimal(builder_fee))


def _manifest(tmp_path, refs):
    p = tmp_path / "followers.json"
    p.write_text(json.dumps({"followers": [
        {"account_id": r.account_id, "user_address": r.user_address,
         "builder_address": r.builder_address, "network": r.network,
         "label": r.label} for r in refs]}))
    return p


def _hist_line(entry) -> str:
    """history 條目 → jsonl 一行。三元組 (date, accrued, captured_at) 寫新格式；
    二元組 (date, accrued) 寫**舊格式**（無 captured_at）——舊資料相容性靠它測。"""
    if len(entry) == 3:
        d, a, cap = entry
        return json.dumps({"date": d, "captured_at": cap.isoformat(),
                           "accrued": str(a)}) + "\n"
    d, a = entry
    return json.dumps({"date": d, "accrued": str(a)}) + "\n"


def _ops_cfg(tmp_path, admin_addresses=frozenset(), refs=None, history=None):
    refs = refs if refs is not None else [_ref()]
    hist = tmp_path / "accrued_history.jsonl"
    if history is not None:
        hist.write_text("".join(_hist_line(e) for e in history))
    return make_cfg(tmp_path, admin_addresses=admin_addresses,
                    followers_path=str(_manifest(tmp_path, refs)),
                    accrued_history_path=str(hist))


def _admin_app(tmp_path, refs=None, history=None):
    wallet = Account.create()
    cfg = _ops_cfg(tmp_path, frozenset({wallet.address.lower()}), refs, history)
    app, cfg, store, keysvc, hl = make_app(tmp_path, cfg=cfg)
    client = _client(app)
    login(client, wallet=wallet)
    return client, cfg, store, hl


# ---------- ⭐ admin 閘（紅線 1） ----------

def _dep_call_names(dependant) -> set[str]:
    names = {getattr(dependant.call, "__name__", "")}
    for sub in dependant.dependencies:
        names |= _dep_call_names(sub)
    return names


# ⭐ 跨客戶資料來源註冊表：全 repo「一次讀到多個客戶資料」的入口符號。
# 用途：admin 閘的結構性檢查以**資料來源**為鍵，而非路徑前綴——前綴檢查防得住
# 「新增 /api/ops/* 忘了掛閘」，卻完全看不到寫在別的前綴下的跨客戶端點
# （例如 /api/reports/all）。任何新的跨客戶讀取入口都必須登記在此，
# test_cross_customer_source_registry_is_complete 會強迫做這個決定。
CROSS_CUSTOMER_SOURCES = {"list_billing", "customer_pnl", "_load_followers",
                          "list_subscriptions", "trade_quality_rows"}


def test_cross_customer_source_registry_is_complete(tmp_path):
    """釘住 CROSS_CUSTOMER_SOURCES 就是**目前全部**的跨客戶讀取入口。

    新增一個跨客戶資料來源 → 本測試紅 → 被迫在此登記，登記後
    test_cross_customer_sources_are_admin_gated 立刻開始檢查用到它的每個路由。
    （沿 web/src/lib/api.test.ts 手寫清單長度那條測試的精神：清單本身要被釘住，
    否則「檢查清單內的東西」只是檢查了一個會自己縮水的清單。）

    極限：本測試只釘集合內容，不會自己去發現新的來源——它換來的是「新增時會撞牆」，
    不是「自動偵測」。"""
    import inspect

    from spark.publicapi import app as app_mod
    from spark.publicapi import ops as ops_mod
    from spark.publicapi import store as store_mod

    # 跨客戶＝回傳「多個客戶」的資料。逐模組列出候選，與註冊表比對。
    candidates = set()
    for mod, names in ((ops_mod, ("customer_pnl", "subscription_drift",
                                  "trade_quality_rows")),
                       (store_mod, ("list_billing",)),
                       (app_mod, ("_load_followers",))):
        for n in names:
            if n == "_load_followers":
                candidates.add(n)      # create_app 內的閉包，靠原始碼字串比對
            elif hasattr(mod, n) or hasattr(getattr(mod, "ApiStore", object), n):
                candidates.add(n)
    # subscription_drift 本身不讀資料（純函式，資料由 list_billing/list_subscriptions
    # 餵入），故不列入註冊表——註冊表要的是**讀取入口**，不是處理函式。
    candidates.discard("subscription_drift")
    candidates.add("list_subscriptions")   # StripeGateway：跨客戶讀 Stripe 訂閱
    assert candidates == CROSS_CUSTOMER_SOURCES, (
        "跨客戶資料來源有增減——新增的入口必須登記進 CROSS_CUSTOMER_SOURCES，"
        "並確認用到它的每個路由都掛了 admin 閘")
    assert inspect.isfunction(ops_mod.customer_pnl)


def test_cross_customer_sources_are_admin_gated(tmp_path):
    """⭐ 以**資料來源**為鍵的閘檢查：任何 handler 只要在自己的原始碼裡引用了
    CROSS_CUSTOMER_SOURCES 的符號，其 dependant 樹就必須含 _require_admin。

    這比路徑前綴嚴格：跨客戶端點就算寫在 /api/reports/all 也逃不掉。

    ⚠️ **本檢查的極限（誠實標註，不要以為它保證了它不保證的事）**：
    只看 handler **自己**的原始碼（inspect.getsource）。若 handler 呼叫某個 helper、
    由那個 helper 間接碰到跨客戶資料，本檢查看不到——這**不是**完備的呼叫圖分析。
    要做到完備需要靜態呼叫圖或執行期追蹤；目前的取捨是「零依賴、涵蓋直接引用」。
    與 test_all_admin_scoped_routes_are_gated（路徑前綴）互補，兩條都保留：
    前綴那條抓「/ops 下新端點忘了掛閘」，本條抓「跨客戶端點藏在別的前綴下」。
    """
    import inspect

    app, *_ = make_app(tmp_path)
    checked = []
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        dependant = getattr(route, "dependant", None)
        if endpoint is None or dependant is None:
            continue
        try:
            source = inspect.getsource(endpoint)
        except (OSError, TypeError):        # 內建/動態產生的路由，無原始碼可讀
            continue
        used = {s for s in CROSS_CUSTOMER_SOURCES if s in source}
        if not used:
            continue
        path = getattr(route, "path", "")
        assert "_require_admin" in _dep_call_names(dependant), (
            f"{path} 直接讀跨客戶資料 {sorted(used)} 卻未掛 admin 閘")
        checked.append(path)
    # 檢查真的有跑到東西（空迴圈全綠是最糟的假保證）
    assert set(checked) == {"/api/ops/customers", "/api/ops/revenue",
                            "/api/ops/subscriptions", "/api/ops/trade-quality",
                            "/api/ops/health"}


def test_all_admin_scoped_routes_are_gated(tmp_path):
    """結構性：每個 /api/ops/* 與 /api/admin/* 路由的 dependant 樹裡都必須有
    _require_admin。新端點忘了掛閘 → 本測試紅（不靠 code review 記得）。

    與 test_cross_customer_sources_are_admin_gated 互補、刻意不合併：本條以**路徑**
    為鍵（抓 /ops 下的新端點），那條以**資料來源**為鍵（抓藏在別的前綴下的跨客戶端點）。
    """
    app, *_ = make_app(tmp_path)
    seen = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if not (path.startswith("/api/ops/") or path.startswith("/api/admin/")):
            continue
        assert "_require_admin" in _dep_call_names(route.dependant), \
            f"{path} 未掛 admin 閘"
        seen.add(path)
    assert seen == {"/api/ops/customers", "/api/ops/revenue", "/api/ops/subscriptions",
                    "/api/ops/trade-quality", "/api/ops/health", "/api/admin/pending"}


@pytest.mark.parametrize("path", ["/api/ops/customers", "/api/ops/revenue",
                                  "/api/ops/subscriptions"])
def test_ops_requires_session(tmp_path, path):
    cfg = _ops_cfg(tmp_path)
    app, *_ = make_app(tmp_path, cfg=cfg)
    assert _client(app).get(path).status_code == 401


@pytest.mark.parametrize("path", ["/api/ops/customers", "/api/ops/revenue",
                                  "/api/ops/subscriptions"])
def test_ops_403_for_non_admin(tmp_path, path):
    """已登入但不在白名單 → 403（跨客戶資料絕不外洩給一般客戶）。"""
    cfg = _ops_cfg(tmp_path)
    app, *_ = make_app(tmp_path, cfg=cfg)
    client = _client(app)
    login(client)
    assert client.get(path).status_code == 403


def test_ops_customers_ok_for_admin(tmp_path):
    client, cfg, store, hl = _admin_app(tmp_path)
    hl.fills[ADDR_A] = [_fill(sz="2", px="100", crossed=True, builder_fee="0.5")]
    hl.account_values[ADDR_A] = Decimal("1234.5")
    r = client.get("/api/ops/customers")
    assert r.status_code == 200, r.text
    body = r.json()
    row = body["customers"][0]
    assert row["account_id"] == ACCT_A
    assert row["notional"] == "200"           # 2 × 100，字串無損（非 float）
    assert row["builder_fee"] == "0.5"
    assert row["taker_share"] == "1"
    assert row["account_value"] == "1234.5"
    assert row["subscription"] == "none"      # 有 store、查無 billing 記錄
    assert row["error"] is None


def test_ops_customers_days_bounds(tmp_path):
    client, *_ = _admin_app(tmp_path)
    assert client.get("/api/ops/customers", params={"days": 0}).status_code == 400
    assert client.get("/api/ops/customers", params={"days": 91}).status_code == 400
    assert client.get("/api/ops/customers", params={"days": 90}).status_code == 200


# ---------- ⭐ 跨 follower 隔離 ----------

def test_fills_failure_of_one_follower_does_not_break_others(tmp_path):
    """一個客戶的 fills 查詢炸掉，其他客戶照樣有資料（整張報表不變 500）。"""
    client, cfg, store, hl = _admin_app(tmp_path, refs=[_ref(), _ref(ACCT_B, ADDR_B, "B")])
    hl.fills_error[ADDR_A] = RuntimeError("boom-a")
    hl.fills[ADDR_B] = [_fill(sz="1", px="50", builder_fee="0.25")]
    hl.account_values[ADDR_B] = Decimal("77")
    body = client.get("/api/ops/customers").json()
    rows = {r["account_id"]: r for r in body["customers"]}
    assert "boom-a" in rows[ACCT_A]["error"]
    assert rows[ACCT_A]["notional"] == "0"
    assert rows[ACCT_B]["error"] is None      # 鄰居未受影響
    assert rows[ACCT_B]["notional"] == "50"
    assert rows[ACCT_B]["account_value"] == "77"


def test_account_value_failure_isolated_per_row(tmp_path):
    """account_value 查詢各自 try/except：失敗只讓該列 account_value=null＋記 error。"""
    client, cfg, store, hl = _admin_app(tmp_path, refs=[_ref(), _ref(ACCT_B, ADDR_B, "B")])
    hl.account_value_error[ADDR_A] = RuntimeError("av-down")
    hl.fills[ADDR_A] = [_fill(builder_fee="0.5")]
    hl.account_values[ADDR_B] = Decimal("10")
    rows = {r["account_id"]: r for r in client.get("/api/ops/customers").json()["customers"]}
    assert rows[ACCT_A]["account_value"] is None
    assert "av-down" in rows[ACCT_A]["error"]
    assert rows[ACCT_A]["builder_fee"] == "0.5"   # fills 那半仍然有值
    assert rows[ACCT_B]["account_value"] == "10" and rows[ACCT_B]["error"] is None


def test_customer_pnl_subscription_unknown_without_store():
    """未給 store → "unknown"（不假裝是 "none"：沒查過與查過沒有是兩件事）。"""

    class _Ad:
        def get_user_fills(self, a, s, e):
            return []

        def get_account_value(self, a):
            return Decimal("1")

    rows = customer_pnl([_ref()], _Ad(), datetime.now(timezone.utc),
                        datetime.now(timezone.utc))
    assert rows[0]["subscription"] == "unknown"


def test_customer_pnl_store_failure_isolated():
    class _Ad:
        def get_user_fills(self, a, s, e):
            return []

        def get_account_value(self, a):
            return Decimal("1")

    class _BadStore:
        def get_billing(self, account_id):
            raise RuntimeError("db-locked")

    rows = customer_pnl([_ref()], _Ad(), datetime.now(timezone.utc),
                        datetime.now(timezone.utc), store=_BadStore())
    assert rows[0]["subscription"] == "unknown"
    assert "db-locked" in rows[0]["error"]


# ---------- 收入對帳（純函式） ----------

def _rows(*fees):
    return [{"builder_fee": Decimal(f)} for f in fees]


def test_reconciliation_basic():
    out = revenue_reconciliation(_rows("1", "2"), Decimal("10"), Decimal("6"),
                                 threshold_pct=Decimal("0.1"))
    assert out["attributed"] == Decimal("3")
    assert out["accrued_delta"] == Decimal("4")
    assert out["discrepancy"] == Decimal("1")
    assert out["discrepancy_pct"] == Decimal("1") / Decimal("3")
    assert out["over_threshold"] is True


def test_reconciliation_under_threshold():
    out = revenue_reconciliation(_rows("100"), Decimal("100.5"), Decimal("0"),
                                 threshold_pct=Decimal("0.1"))
    assert out["over_threshold"] is False


def test_reconciliation_zero_attributed_no_zero_division():
    """⭐ 除零防護：attributed 為 0 → pct 回 None（不炸、不假裝 0%）；
    但若 accrued_delta 非 0 則 over_threshold 為 True（異常不得靜默放行）。"""
    out = revenue_reconciliation([], Decimal("5"), Decimal("0"),
                                 threshold_pct=Decimal("0.01"))
    assert out["attributed"] == Decimal("0")
    assert out["discrepancy_pct"] is None
    assert out["over_threshold"] is True  # 收到費用卻無歸屬，必須告警
    assert out["discrepancy"] == Decimal("5")


def test_zero_attributed_with_accrued_flags_anomaly():
    """應收 0 但實收非 0：算不出百分比，但仍必須判為異常（不得靜默放行）。"""
    out = revenue_reconciliation([], Decimal("5"), Decimal("0"),
                                 threshold_pct=Decimal("0.01"))
    assert out["discrepancy_pct"] is None
    assert out["over_threshold"] is True, "收到費用卻歸屬不到客戶，必須告警"


def test_zero_attributed_zero_accrued_is_not_anomaly():
    """應收 0 且實收 0：安靜的一天，不得誤報。"""
    out = revenue_reconciliation([], Decimal("0"), Decimal("0"),
                                 threshold_pct=Decimal("0.01"))
    assert out["over_threshold"] is False


def test_north_star_never_derived_from_rows():
    """⭐ 紅線 2：accrued_delta 只能來自參數，不得由 rows 推導／加總。
    同一組 rows 配不同 accrued 參數 → accrued_delta 必須跟著參數走；
    accrued 兩參數相等時 delta 必為 0（哪怕 rows 的 builder_fee 加起來很大）。"""
    rows = _rows("7", "3")  # Σ = 10
    same = revenue_reconciliation(rows, Decimal("100"), Decimal("100"),
                                  threshold_pct=Decimal("0.01"))
    assert same["accrued_delta"] == Decimal("0")      # 不是 10 → 沒有偷用 rows
    assert same["discrepancy"] == Decimal("-10")
    other = revenue_reconciliation(rows, Decimal("100"), Decimal("94"),
                                   threshold_pct=Decimal("0.01"))
    assert other["accrued_delta"] == Decimal("6")
    assert other["attributed"] == same["attributed"] == Decimal("10")


# ---------- accrued 歷史序列 ----------

def test_load_accrued_series_sorted_and_tolerant(tmp_path):
    """壞行跳過、依日期升冪。⭐ 斷言形狀改為 AccruedPoint 三元組（新增 captured_at）：
    這不是遷就測試，是被比較的兩個值必須同基準所要求的欄位（工程原則 1）。"""
    p = tmp_path / "h.jsonl"
    p.write_text('{"date": "2026-07-19", "captured_at": "2026-07-19T00:10:00+00:00",'
                 ' "accrued": "2"}\n'
                 'not-json\n'
                 '\n'
                 '{"date": "2026-07-18", "captured_at": "2026-07-18T00:10:00+00:00",'
                 ' "accrued": "1"}\n'
                 '{"accrued": "9"}\n')
    assert load_accrued_series(p) == [
        ("2026-07-18", Decimal("1"), datetime(2026, 7, 18, 0, 10, tzinfo=timezone.utc)),
        ("2026-07-19", Decimal("2"), datetime(2026, 7, 19, 0, 10, tzinfo=timezone.utc))]


def test_load_accrued_series_mixed_old_and_new_format(tmp_path):
    """⭐ 新舊格式混合的歷史檔要能載入：舊行（無 captured_at）→ None，不猜、不丟掉
    accrued 值；壞掉的 captured_at 也降級成 None（拒絕硬算比猜一個時刻安全）。"""
    p = tmp_path / "h.jsonl"
    p.write_text('{"date": "2026-07-17", "accrued": "1"}\n'
                 '{"date": "2026-07-18", "captured_at": "garbage", "accrued": "2"}\n'
                 '{"date": "2026-07-19", "captured_at": "2026-07-19T00:10:00+00:00",'
                 ' "accrued": "3"}\n')
    series = load_accrued_series(p)
    assert [pt.date for pt in series] == ["2026-07-17", "2026-07-18", "2026-07-19"]
    assert [pt.accrued for pt in series] == [Decimal("1"), Decimal("2"), Decimal("3")]
    assert series[0].captured_at is None and series[1].captured_at is None
    assert series[2].captured_at == datetime(2026, 7, 19, 0, 10, tzinfo=timezone.utc)


def test_load_accrued_series_naive_captured_at_is_utc(tmp_path):
    """無時區的 captured_at 視為 UTC（唯一寫入者一律寫 UTC）——不得回 naive
    datetime，否則與 tz-aware 的 fills 時間比較會直接 TypeError。"""
    p = tmp_path / "h.jsonl"
    p.write_text('{"date": "2026-07-19", "captured_at": "2026-07-19T00:10:00",'
                 ' "accrued": "3"}\n')
    assert load_accrued_series(p)[0].captured_at == datetime(
        2026, 7, 19, 0, 10, tzinfo=timezone.utc)


def test_load_accrued_series_missing_file(tmp_path):
    assert load_accrued_series(tmp_path / "nope.jsonl") == []


def test_append_accrued_history_idempotent(tmp_path, monkeypatch):
    """⭐ 同日重跑覆蓋該日那一行（不是再 append 一筆）——否則今昨差會被算成 0。
    覆蓋時 captured_at 跟著換：值與時刻同源（重跑的 accrued 屬於重跑那一刻）。"""
    import scripts.copytrade_daily_report as rpt
    monkeypatch.setattr(rpt, "HISTORY_PATH", tmp_path / "accrued_history.jsonl")
    t18 = datetime(2026, 7, 18, 0, 10, tzinfo=timezone.utc)
    t19 = datetime(2026, 7, 19, 0, 10, tzinfo=timezone.utc)
    t19b = datetime(2026, 7, 19, 6, 30, tzinfo=timezone.utc)
    rpt.append_accrued_history("2026-07-18", Decimal("1.5"), now_fn=lambda: t18)
    rpt.append_accrued_history("2026-07-19", Decimal("2.5"), now_fn=lambda: t19)
    rpt.append_accrued_history("2026-07-19", Decimal("2.75"), now_fn=lambda: t19b)
    assert load_accrued_series(rpt.HISTORY_PATH) == [
        ("2026-07-18", Decimal("1.5"), t18),
        ("2026-07-19", Decimal("2.75"), t19b)]   # accrued 與 captured_at 一起換
    assert len(rpt.HISTORY_PATH.read_text().strip().splitlines()) == 2


def test_append_accrued_history_records_capture_time(tmp_path, monkeypatch):
    """⭐ Critical 修法的核心：每行必須落下快照時刻——下游拿它當對帳窗口界。
    預設值走真時鐘（此處只驗欄位存在且可解析），可注入以便釘死。"""
    import scripts.copytrade_daily_report as rpt
    monkeypatch.setattr(rpt, "HISTORY_PATH", tmp_path / "h.jsonl")
    rpt.append_accrued_history("2026-07-19", Decimal("3"))
    rec = json.loads(rpt.HISTORY_PATH.read_text().strip())
    assert set(rec) == {"date", "captured_at", "accrued"}
    assert datetime.fromisoformat(rec["captured_at"]).tzinfo is not None


def test_append_accrued_history_preserves_legacy_rows(tmp_path, monkeypatch):
    """舊行（無 captured_at）原樣保留、**不回填猜測值**——猜出來的窗口比沒有窗口
    危險；下游看到缺值會拒絕對帳（basis_unknown）而不是算出假數字。"""
    import scripts.copytrade_daily_report as rpt
    hist = tmp_path / "h.jsonl"
    monkeypatch.setattr(rpt, "HISTORY_PATH", hist)
    hist.write_text('{"date": "2026-07-18", "accrued": "1"}\n')
    t19 = datetime(2026, 7, 19, 0, 10, tzinfo=timezone.utc)
    rpt.append_accrued_history("2026-07-19", Decimal("2"), now_fn=lambda: t19)
    series = load_accrued_series(hist)
    assert series[0].captured_at is None      # 舊行不被回填
    assert series[1].captured_at == t19


def test_append_accrued_history_does_not_touch_snapshot(tmp_path, monkeypatch):
    """additive：既有快照行為不變（向後相容）。"""
    import scripts.copytrade_daily_report as rpt
    monkeypatch.setattr(rpt, "HISTORY_PATH", tmp_path / "h.jsonl")
    monkeypatch.setattr(rpt, "SNAPSHOT_PATH", tmp_path / "snap.json")
    rpt.save_accrued_snapshot("2026-07-19", Decimal("3"))
    rpt.append_accrued_history("2026-07-19", Decimal("3"))
    assert json.loads((tmp_path / "snap.json").read_text()) == {"date": "2026-07-19",
                                                               "accrued": "3"}
    assert rpt.load_accrued_snapshot() == Decimal("3")


# ---------- /api/ops/revenue 端點 ----------

def test_revenue_insufficient_history(tmp_path):
    """歷史不足兩點 → 不硬算（把整段累積量當單日增量會造出天文數字假 delta）。"""
    client, *_ = _admin_app(tmp_path, history=[("2026-07-18", "5")])
    body = client.get("/api/ops/revenue").json()
    assert body["insufficient_accrued_history"] is True
    assert body["history_points"] == 1
    assert "accrued_delta" not in body


def test_revenue_computes_from_history_and_rows(tmp_path):
    """history 條目改為三元組（含 captured_at）：不是遷就測試——沒有快照時刻，
    端點依設計就會拒絕對帳（見下面的 basis_unknown 測試）。"""
    cap_prev = datetime(2026, 7, 1, 0, 10, tzinfo=timezone.utc)
    cap_now = datetime(2026, 7, 19, 0, 10, tzinfo=timezone.utc)
    client, cfg, store, hl = _admin_app(
        tmp_path, refs=[_ref(), _ref(ACCT_B, ADDR_B, "B")],
        history=[("2026-07-01", "10", cap_prev), ("2026-07-19", "16", cap_now)])
    hl.fills[ADDR_A] = [_fill(builder_fee="2")]
    hl.fills[ADDR_B] = [_fill(builder_fee="3")]
    body = client.get("/api/ops/revenue", params={"threshold_pct": 0.1}).json()
    assert body["insufficient_accrued_history"] is False
    assert body["basis_unknown"] is False
    assert body["attributed"] == "5"          # 2 + 3（歸屬／應收）
    assert body["accrued_delta"] == "6"       # 16 − 10（北極星／實收，查一次不加總）
    assert body["discrepancy"] == "1"
    assert body["over_threshold"] is True     # 1/5 = 20% > 10%
    assert body["day"] == "2026-07-19" and body["prev_day"] == "2026-07-01"
    assert len(body["customers"]) == 2


# ---------- ⭐ 對帳窗口＝快照時刻（opus 對抗審查 Critical，工程原則 1） ----------

def test_revenue_window_comes_from_capture_time_not_calendar_day(tmp_path):
    """⭐ 複現 opus 的情境：cron 排在每日 00:10、帳目**完全正常**的錢包不得誤報。

    accrued 增量涵蓋 (07-18 00:10, 07-19 00:10]——那是「昨天一整天」。舊實作拿它去
    比 [07-19 00:00, now] 的 fills，只看得到最後十幾分鐘的成交（此例 2 / 6），
    差 66% → 觸發「收入對帳超標」告警並叫醒管理員去查不存在的漏財。
    修法：兩側窗口都取自 captured_at，同源同基準 → 應收 6 == 實收 6，不告警。"""
    cap_prev = datetime(2026, 7, 18, 0, 10, tzinfo=timezone.utc)
    cap_now = datetime(2026, 7, 19, 0, 10, tzinfo=timezone.utc)
    client, cfg, store, hl = _admin_app(
        tmp_path, history=[("2026-07-18", "10", cap_prev), ("2026-07-19", "16", cap_now)])
    hl.window_aware = True   # 只有真的依窗口過濾，窗口取錯才會被測出來
    hl.fills[ADDR_A] = [
        _fill(builder_fee="4", t=datetime(2026, 7, 18, 9, 0, tzinfo=timezone.utc)),
        _fill(builder_fee="2", t=datetime(2026, 7, 19, 0, 5, tzinfo=timezone.utc)),
    ]
    body = client.get("/api/ops/revenue", params={"threshold_pct": 0.01}).json()
    assert body["basis_unknown"] is False
    # 窗口界＝快照時刻本身（不是 00:00，也不是 now）
    assert body["window_start"] == cap_prev.isoformat()
    assert body["window_end"] == cap_now.isoformat()
    assert body["attributed"] == "6"        # 兩筆都在窗口內（4 + 2）
    assert body["accrued_delta"] == "6"     # 16 − 10
    assert body["discrepancy"] == "0"
    assert body["over_threshold"] is False, "健康帳戶不得誤報漏財"


def test_revenue_window_excludes_fills_outside_capture_window(tmp_path):
    """窗口確實會排除界外成交（證明上一個測試的「全數落在窗內」不是因為沒過濾）。"""
    cap_prev = datetime(2026, 7, 18, 0, 10, tzinfo=timezone.utc)
    cap_now = datetime(2026, 7, 19, 0, 10, tzinfo=timezone.utc)
    client, cfg, store, hl = _admin_app(
        tmp_path, history=[("2026-07-18", "10", cap_prev), ("2026-07-19", "12", cap_now)])
    hl.window_aware = True
    hl.fills[ADDR_A] = [
        _fill(builder_fee="2", t=datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)),
        _fill(builder_fee="99", t=datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)),
        _fill(builder_fee="99", t=datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)),
    ]
    body = client.get("/api/ops/revenue", params={"threshold_pct": 0.01}).json()
    assert body["attributed"] == "2"       # 界外的 99 兩筆都不計
    assert body["over_threshold"] is False


def test_revenue_basis_unknown_when_capture_time_missing(tmp_path):
    """⭐ 舊資料（無 captured_at）→ 明確標 basis_unknown、**不硬算、不告警**。
    算錯的數字比沒有數字危險：用日期猜窗口會整整錯開一天。"""
    client, cfg, store, hl = _admin_app(
        tmp_path, history=[("2026-07-18", "10"), ("2026-07-19", "16")])
    hl.fills[ADDR_A] = [_fill(builder_fee="2")]
    body = client.get("/api/ops/revenue").json()
    assert body["basis_unknown"] is True
    assert body["over_threshold"] is False
    assert "captured_at" in body["note"]
    for k in ("discrepancy", "discrepancy_pct", "attributed", "accrued_delta"):
        assert k not in body, f"{k} 不得在缺基準時被算出來"
    assert body["window_start"] is None and body["window_end"] is None


def test_revenue_basis_unknown_when_only_one_side_has_capture_time(tmp_path):
    """只有一側有快照時刻同樣不可比（混源比較正是事故形狀）。"""
    cap_now = datetime(2026, 7, 19, 0, 10, tzinfo=timezone.utc)
    client, *_ = _admin_app(
        tmp_path, history=[("2026-07-18", "10"), ("2026-07-19", "16", cap_now)])
    body = client.get("/api/ops/revenue").json()
    assert body["basis_unknown"] is True and body["over_threshold"] is False


def test_revenue_basis_unknown_when_capture_times_not_increasing(tmp_path):
    """快照時刻非嚴格遞增（回填／時鐘倒退）→ 窗口無意義，一樣拒算。"""
    cap_prev = datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc)   # 比後一筆還晚
    cap_now = datetime(2026, 7, 19, 0, 10, tzinfo=timezone.utc)
    client, *_ = _admin_app(
        tmp_path, history=[("2026-07-18", "10", cap_prev), ("2026-07-19", "16", cap_now)])
    body = client.get("/api/ops/revenue").json()
    assert body["basis_unknown"] is True and body["over_threshold"] is False
    assert "遞增" in body["note"]


def test_revenue_no_false_alarm_logged_when_basis_unknown(tmp_path, caplog):
    """basis_unknown 時不得留下「收入對帳超標」的告警痕跡（誤報會消耗信任）。"""
    import logging
    client, *_ = _admin_app(tmp_path, history=[("2026-07-18", "10"), ("2026-07-19", "99")])
    with caplog.at_level(logging.WARNING):
        client.get("/api/ops/revenue")
    assert "收入對帳超標" not in caplog.text


# ---------- ⭐ 兩張表共用同一個窗口（工程原則 1 的結構性修法） ----------

def test_customers_accrued_window_identical_to_revenue_window(tmp_path):
    """⭐ 本修復的核心斷言：`/api/ops/customers?window=accrued` 與 `/api/ops/revenue`
    的 window_start／window_end **完全相同**（同一個 `ops.accrued_window` 推導）。

    存在理由：兩張表的 builder fee 並排顯示，若窗口不同就不可相減，而「不可相減」
    在畫面上看不出來。前端文字標註擋不住誤讀，只有同源窗口擋得住。"""
    cap_prev = datetime(2026, 7, 18, 0, 10, tzinfo=timezone.utc)
    cap_now = datetime(2026, 7, 19, 0, 10, tzinfo=timezone.utc)
    client, cfg, store, hl = _admin_app(
        tmp_path, history=[("2026-07-18", "10", cap_prev), ("2026-07-19", "16", cap_now)])
    hl.window_aware = True
    hl.fills[ADDR_A] = [_fill(builder_fee="6",
                              t=datetime(2026, 7, 18, 9, 0, tzinfo=timezone.utc))]
    rev = client.get("/api/ops/revenue").json()
    cus = client.get("/api/ops/customers", params={"window": "accrued"}).json()
    assert cus["basis_unknown"] is False
    assert cus["window_start"] == rev["window_start"] == cap_prev.isoformat()
    assert cus["window_end"] == rev["window_end"] == cap_now.isoformat()
    # 同窗 → 兩表的 builder fee 真的可以相減（應收 6 對上實收 6）
    assert cus["customers"][0]["builder_fee"] == rev["attributed"] == "6"


def test_customers_accrued_window_differs_from_days_window(tmp_path):
    """反面：預設 days 窗與 accrued 窗**必定不同**（days 從 now 回推，accrued 取快照
    時刻）。若哪天兩者「碰巧相同」，代表 days 分支偷偷改用了快照時刻——那正是
    「看起來同基準」的假對齊，本測試要讓它紅。"""
    cap_prev = datetime(2026, 7, 18, 0, 10, tzinfo=timezone.utc)
    cap_now = datetime(2026, 7, 19, 0, 10, tzinfo=timezone.utc)
    client, *_ = _admin_app(
        tmp_path, history=[("2026-07-18", "10", cap_prev), ("2026-07-19", "16", cap_now)])
    accrued = client.get("/api/ops/customers", params={"window": "accrued"}).json()
    default = client.get("/api/ops/customers").json()
    assert default["window"] == "days" and default["days"] == 1
    assert default["window_end"] != accrued["window_end"]


def test_customers_accrued_window_basis_unknown_returns_no_numbers(tmp_path):
    """無法對齊（舊資料缺 captured_at）→ basis_unknown ＋ note，**不回數值欄**。
    不得退化成日曆日或 now 往回 N 天（沿收入對帳的既有慣例）。"""
    client, cfg, store, hl = _admin_app(
        tmp_path, history=[("2026-07-18", "10"), ("2026-07-19", "16")])
    hl.fills[ADDR_A] = [_fill(builder_fee="2")]
    body = client.get("/api/ops/customers", params={"window": "accrued"}).json()
    assert body["basis_unknown"] is True
    assert "captured_at" in body["note"]
    assert body["window_start"] is None and body["window_end"] is None
    for k in ("customers", "days"):
        assert k not in body, f"{k} 不得在缺基準時被回出來"


def test_customers_accrued_window_basis_unknown_when_history_too_short(tmp_path):
    """歷史不足兩點同樣無法對齊 → basis_unknown（不偷偷用 days 窗補位）。"""
    client, *_ = _admin_app(tmp_path, history=[("2026-07-19", "16")])
    body = client.get("/api/ops/customers", params={"window": "accrued"}).json()
    assert body["basis_unknown"] is True and "customers" not in body


def test_customers_days_and_accrued_window_are_mutually_exclusive(tmp_path):
    """兩者同時給 → 400。不靜默忽略其中一個：被忽略的那個會讓人以為自己看的是
    另一個基準，正是本修復要消滅的誤讀。"""
    client, *_ = _admin_app(tmp_path)
    r = client.get("/api/ops/customers", params={"days": 7, "window": "accrued"})
    assert r.status_code == 400
    assert "互斥" in r.json()["detail"]


def test_customers_rejects_unknown_window_value(tmp_path):
    """未知的 window 值 → 400（靜默退回 days 窗＝再造一次假對齊）。"""
    client, *_ = _admin_app(tmp_path)
    assert client.get("/api/ops/customers",
                      params={"window": "calendar"}).status_code == 400


def test_customers_default_behaviour_unchanged_without_window(tmp_path):
    """向後相容：未指定 window → 既有 days 行為完全不變（含預設 days=1）。"""
    client, cfg, store, hl = _admin_app(tmp_path)
    hl.fills[ADDR_A] = [_fill(builder_fee="0.5")]
    body = client.get("/api/ops/customers").json()
    assert body["days"] == 1
    assert body["customers"][0]["builder_fee"] == "0.5"
    assert body["start"] == body["window_start"]
    assert body["end"] == body["window_end"]


def test_accrued_window_is_single_source_for_both_endpoints():
    """單元層：窗口推導只有這一個入口，且無法對齊時回 None（呼叫端據此標
    basis_unknown，不得自行退化）。"""
    from spark.publicapi.ops import AccruedPoint, accrued_window

    cap_prev = datetime(2026, 7, 18, 0, 10, tzinfo=timezone.utc)
    cap_now = datetime(2026, 7, 19, 0, 10, tzinfo=timezone.utc)
    ok = [AccruedPoint("2026-07-18", Decimal("10"), cap_prev),
          AccruedPoint("2026-07-19", Decimal("16"), cap_now)]
    assert accrued_window(ok) == (cap_prev, cap_now)
    assert accrued_window(ok[:1]) is None                      # 不足兩點
    assert accrued_window([ok[0], AccruedPoint("2026-07-19", Decimal("16"), None)]) is None
    assert accrued_window([ok[1], ok[0]]) is None              # 非嚴格遞增


def test_revenue_rejects_negative_threshold(tmp_path):
    client, *_ = _admin_app(tmp_path, history=[("2026-07-18", "1"), ("2026-07-19", "2")])
    assert client.get("/api/ops/revenue",
                      params={"threshold_pct": -0.5}).status_code == 400


def test_ops_missing_manifest_is_loud(tmp_path):
    """manifest 不存在 → 503（回空清單會被誤讀成「沒有客戶」，工程原則 3）。"""
    wallet = Account.create()
    cfg = make_cfg(tmp_path, admin_addresses=frozenset({wallet.address.lower()}),
                   followers_path=str(tmp_path / "missing.json"))
    app, *_ = make_app(tmp_path, cfg=cfg)
    client = _client(app)
    login(client, wallet=wallet)
    assert client.get("/api/ops/customers").status_code == 503


def test_ops_customers_tolerates_bad_manifest_entry(tmp_path):
    """一個壞條目不該讓整張報表變空白：好條目照回，壞條目進 manifest_errors。"""
    wallet = Account.create()
    p = tmp_path / "followers.json"
    p.write_text(json.dumps({"followers": [
        {"account_id": ACCT_A, "user_address": ADDR_A, "builder_address": BUILDER,
         "network": "testnet"},
        {"account_id": "../evil", "user_address": ADDR_B, "builder_address": BUILDER,
         "network": "testnet"}]}))
    cfg = make_cfg(tmp_path, admin_addresses=frozenset({wallet.address.lower()}),
                   followers_path=str(p))
    app, *_ = make_app(tmp_path, cfg=cfg)
    client = _client(app)
    login(client, wallet=wallet)
    body = client.get("/api/ops/customers").json()
    assert [r["account_id"] for r in body["customers"]] == [ACCT_A]
    assert len(body["manifest_errors"]) == 1


# ---------- ⭐ 成交品質（跨 follower 聚合，admin only） ----------

LEADER = "0x" + "cc" * 20


def _q_app(tmp_path, refs=None, history=None, state_base=None):
    """成交品質端點的現場：admin session ＋ 可指定 state_base（skipped 落檔根）。"""
    wallet = Account.create()
    hist = tmp_path / "accrued_history.jsonl"
    if history is not None:
        hist.write_text("".join(_hist_line(e) for e in history))
    refs = refs if refs is not None else [_ref()]
    cfg = make_cfg(tmp_path, admin_addresses=frozenset({wallet.address.lower()}),
                   followers_path=str(_manifest_with_leader(tmp_path, refs)),
                   accrued_history_path=str(hist),
                   state_base=str(state_base or (tmp_path / "state")))
    app, cfg, store, keysvc, hl = make_app(tmp_path, cfg=cfg)
    client = _client(app)
    login(client, wallet=wallet)
    return client, cfg, hl


def _manifest_with_leader(tmp_path, refs):
    """manifest 帶 leader_address（TE 配對需要知道每個 follower 跟誰）。"""
    p = tmp_path / "followers.json"
    p.write_text(json.dumps({"followers": [
        {"account_id": r.account_id, "user_address": r.user_address,
         "builder_address": r.builder_address, "network": r.network,
         "label": r.label,
         **({"leader_address": r.leader_address} if r.leader_address else {})}
        for r in refs]}))
    return p


def _lref(acct=ACCT_A, addr=ADDR_A, label="A", leader=LEADER):
    return FollowerRef(acct, addr, BUILDER, "testnet", label, leader)


def _write_skipped(state_base, account_id, day_iso, items):
    p = (Path(state_base) / account_id / "var/copytrade/skipped"
         / f"{day_iso}.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items))


def test_trade_quality_requires_session(tmp_path):
    cfg = _ops_cfg(tmp_path)
    app, *_ = make_app(tmp_path, cfg=cfg)
    assert _client(app).get("/api/ops/trade-quality").status_code == 401


def test_trade_quality_403_for_non_admin(tmp_path):
    """成交品質是跨客戶資料，一般登入者一律 403。"""
    cfg = _ops_cfg(tmp_path)
    app, *_ = make_app(tmp_path, cfg=cfg)
    client = _client(app)
    login(client)
    assert client.get("/api/ops/trade-quality").status_code == 403


def test_trade_quality_computes_te_and_slippage(tmp_path):
    """⭐ 配對延遲與滑價由 leader/我方成交配對算出（與日報同一個算式）。

    leader 於 12:00:00 以 100 買進，我方於 12:00:02 以 101 成交（taker）：
    延遲 2 秒、滑價 (101-100)/100×10000 = 100 bp（買貴＝正＝不利）。"""
    client, cfg, hl = _q_app(tmp_path, refs=[_lref()])
    t0 = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
    hl.fills[LEADER] = [_fill(px="100", t=t0)]
    hl.fills[ADDR_A] = [_fill(px="101", t=t0 + timedelta(seconds=2))]

    body = client.get("/api/ops/trade-quality").json()
    row = body["followers"][0]
    assert row["te_available"] is True
    assert row["pair_count"] == 1
    assert row["median_delay_s"] == "2"
    assert Decimal(row["taker_slippage_bp_median"]) == Decimal("100")
    assert row["taker_share"] == "1"


def test_trade_quality_matches_the_daily_report_formula(tmp_path):
    """⭐⭐ 同源斷言：端點回的數字必須等於日報 `compute_trade_quality` 的輸出。

    兩份算式漂移時，兩張表並排顯示卻已不同基準，而畫面上看不出來（工程原則 1）。
    本測試把「同源」變成可執行的檢查，不是靠註解宣稱。"""
    from spark.copytrade.report import compute_trade_quality

    client, cfg, hl = _q_app(tmp_path, refs=[_lref()])
    t0 = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
    leader_fills = [_fill(px="100", t=t0), _fill(px="200", t=t0 + timedelta(minutes=1))]
    my_fills = [_fill(px="101", t=t0 + timedelta(seconds=3)),
                _fill(px="204", t=t0 + timedelta(minutes=1, seconds=5))]
    hl.fills[LEADER] = leader_fills
    hl.fills[ADDR_A] = my_fills

    row = client.get("/api/ops/trade-quality").json()["followers"][0]
    expected = compute_trade_quality(leader_fills, my_fills, [])
    assert row["pair_count"] == expected.pair_count
    assert row["median_delay_s"] == str(expected.median_delay_s)
    assert row["taker_slippage_bp_median"] == str(expected.taker_slippage_bp_median)
    assert row["taker_share"] == str(expected.taker_share)


def test_unknown_leader_yields_null_te_not_zero(tmp_path):
    """⭐⭐ manifest 未記 leader_address → TE／滑價回 null ＋ te_available=false。

    **絕不回 0**：「配對延遲 0 秒」讀起來是完美的跟單品質，實際上是「我們根本
    不知道要跟誰比」。健康面板謊報健康比沒有面板更危險。"""
    client, cfg, hl = _q_app(tmp_path, refs=[_lref(leader=None)])
    hl.fills[ADDR_A] = [_fill()]

    row = client.get("/api/ops/trade-quality").json()["followers"][0]
    assert row["te_available"] is False
    assert row["median_delay_s"] is None
    assert row["taker_slippage_bp_median"] is None
    assert row["pair_count"] is None
    assert "leader_address" in row["te_note"]
    # taker 佔比只由我方成交算出，與 leader 無關 → 仍然有效
    assert row["taker_share"] == "1"


def test_missing_skipped_file_is_unknown_not_zero(tmp_path):
    """⭐ skipped 檔不存在 → null ＋ skipped_available=false，不填 0。
    0 讀起來是「沒有跳過任何客戶的單」，正好是這個指標要抓的問題的反面。"""
    client, cfg, hl = _q_app(tmp_path, refs=[_lref()])
    hl.fills[ADDR_A] = [_fill()]

    row = client.get("/api/ops/trade-quality").json()["followers"][0]
    assert row["skipped_available"] is False
    assert row["skipped_small_notional"] is None
    assert row["skipped_small_ratio"] is None


def test_partial_skipped_days_are_reported_as_unknown(tmp_path):
    """⭐ 多日窗口只有部分天有檔 → 一律 None。部分合計會偏低，而偏低的 skipped
    讀起來就是「引擎沒在跳過單」——寧可說不知道，不給一個看不出偏低的數。"""
    client, cfg, hl = _q_app(tmp_path, refs=[_lref()])
    today = datetime.now(timezone.utc).date().isoformat()
    _write_skipped(cfg.state_base, ACCT_A, today, [{"coin": "ETH", "notional": "50"}])

    body = client.get("/api/ops/trade-quality", params={"days": 3}).json()
    assert len(body["skipped_days"]) == 4        # 3 天窗跨到第 4 個日曆日
    assert body["followers"][0]["skipped_available"] is False


def test_skipped_ratio_omitted_when_window_is_not_whole_days(tmp_path):
    """⭐⭐ 同基準：skipped 以日曆日落檔、成交依窗口過濾。窗口非整日時
    **只回名目、不回比例**——那個商看起來完全像一個正常的比例。"""
    cap_prev = datetime(2026, 7, 18, 0, 10, tzinfo=timezone.utc)
    cap_now = datetime(2026, 7, 19, 0, 10, tzinfo=timezone.utc)
    client, cfg, hl = _q_app(
        tmp_path, refs=[_lref()],
        history=[("2026-07-18", "10", cap_prev), ("2026-07-19", "16", cap_now)])
    for day in ("2026-07-18", "2026-07-19"):
        _write_skipped(cfg.state_base, ACCT_A, day, [{"coin": "ETH", "notional": "50"}])
    hl.window_aware = True
    hl.fills[ADDR_A] = [_fill(t=datetime(2026, 7, 18, 9, 0, tzinfo=timezone.utc))]

    row = client.get("/api/ops/trade-quality",
                     params={"window": "accrued"}).json()["followers"][0]
    assert row["skipped_available"] is True
    assert row["skipped_small_notional"] == "100"     # 兩天合計，名目仍然有意義
    assert row["skipped_small_ratio"] is None         # 比例不同基準 → 不算
    assert "不同基準" in row["skipped_note"]


def test_trade_quality_accrued_window_identical_to_revenue(tmp_path):
    """⭐ 品質面板與收入對帳並排讀 → 窗口必須出自同一個 ops.accrued_window。"""
    cap_prev = datetime(2026, 7, 18, 0, 10, tzinfo=timezone.utc)
    cap_now = datetime(2026, 7, 19, 0, 10, tzinfo=timezone.utc)
    client, cfg, hl = _q_app(
        tmp_path, refs=[_lref()],
        history=[("2026-07-18", "10", cap_prev), ("2026-07-19", "16", cap_now)])

    rev = client.get("/api/ops/revenue").json()
    q = client.get("/api/ops/trade-quality", params={"window": "accrued"}).json()
    assert q["window_start"] == rev["window_start"] == cap_prev.isoformat()
    assert q["window_end"] == rev["window_end"] == cap_now.isoformat()


def test_trade_quality_basis_unknown_returns_no_numbers(tmp_path):
    """舊資料缺 captured_at → basis_unknown ＋ 不回任何數值欄（沿既有慣例）。"""
    client, cfg, hl = _q_app(tmp_path, refs=[_lref()],
                             history=[("2026-07-18", "10"), ("2026-07-19", "16")])
    body = client.get("/api/ops/trade-quality", params={"window": "accrued"}).json()
    assert body["basis_unknown"] is True
    assert "followers" not in body
    assert body["window_start"] is None and body["window_end"] is None


def test_trade_quality_window_args_are_mutually_exclusive(tmp_path):
    client, *_ = _q_app(tmp_path, refs=[_lref()])
    assert client.get("/api/ops/trade-quality",
                      params={"days": 7, "window": "accrued"}).status_code == 400
    assert client.get("/api/ops/trade-quality",
                      params={"window": "calendar"}).status_code == 400
    assert client.get("/api/ops/trade-quality", params={"days": 0}).status_code == 400
    assert client.get("/api/ops/trade-quality", params={"days": 91}).status_code == 400


def test_one_follower_failure_does_not_break_others(tmp_path):
    """跨 follower 隔離：一個客戶的 fills 炸掉，其餘照常有品質資料。"""
    client, cfg, hl = _q_app(
        tmp_path, refs=[_lref(), _lref(ACCT_B, ADDR_B, "B")])
    hl.fills_error[ADDR_A] = RuntimeError("boom-a")
    t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
    hl.fills[LEADER] = [_fill(px="100", t=t0)]
    hl.fills[ADDR_B] = [_fill(px="100", t=t0 + timedelta(seconds=1))]

    rows = {r["account_id"]: r for r in
            client.get("/api/ops/trade-quality").json()["followers"]}
    assert "boom-a" in rows[ACCT_A]["error"]
    assert rows[ACCT_A]["quality_available"] is False
    assert rows[ACCT_B]["quality_available"] is True
    assert rows[ACCT_B]["median_delay_s"] == "1"


def test_leader_fills_are_fetched_once_per_distinct_leader(tmp_path):
    """⭐ 多個 follower 跟同一個 leader 時只查一次——否則一次面板載入變成 N 次外呼。"""
    client, cfg, hl = _q_app(
        tmp_path, refs=[_lref(), _lref(ACCT_B, ADDR_B, "B")])
    calls = []
    orig = hl.get_user_fills

    def _counting(address, start, end):
        calls.append(address.lower())
        return orig(address, start, end)

    hl.get_user_fills = _counting
    client.get("/api/ops/trade-quality")
    assert calls.count(LEADER.lower()) == 1


def test_summary_reports_sample_size_not_just_a_number(tmp_path):
    """⭐ 彙總必須附樣本數：只回一個最差值會讓「10 個客戶只有 1 個有資料」與
    「10 個都有資料」看起來一模一樣。"""
    client, cfg, hl = _q_app(
        tmp_path, refs=[_lref(), _lref(ACCT_B, ADDR_B, "B", leader=None)])
    t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
    hl.fills[LEADER] = [_fill(px="100", t=t0)]
    hl.fills[ADDR_A] = [_fill(px="100", t=t0 + timedelta(seconds=4))]
    hl.fills[ADDR_B] = [_fill(px="100", t=t0)]

    s = client.get("/api/ops/trade-quality").json()["summary"]
    assert s["followers"] == 2
    assert s["te_available_count"] == 1          # B 不知道跟誰
    assert s["delay_sample"] == 1
    assert s["worst_median_delay_s"] == "4"
    assert s["skipped_available_count"] == 0


# ---------- ⭐ 系統健康面板（admin only；謊報健康比沒有面板更危險） ----------

def _h_app(tmp_path, refs=None, state_base=None, exchange_dir=None):
    wallet = Account.create()
    refs = refs if refs is not None else [_lref()]
    cfg = make_cfg(tmp_path, admin_addresses=frozenset({wallet.address.lower()}),
                   followers_path=str(_manifest_with_leader(tmp_path, refs)),
                   state_base=str(state_base or (tmp_path / "state")),
                   exchange_dir=str(exchange_dir or (tmp_path / "exchange")))
    app, cfg, store, keysvc, hl = make_app(tmp_path, cfg=cfg)
    client = _client(app)
    login(client, wallet=wallet)
    return client, cfg


def _write_samples(state_base, account_id, samples):
    p = Path(state_base) / account_id / "var/copytrade/equity_samples.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([[ts, str(v)] for ts, v in samples]))


def _trip_killswitch(state_base, account_id):
    p = Path(state_base) / account_id / "var/copytrade/killswitch.tripped"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"status": "tripped"}))


def test_health_requires_session(tmp_path):
    cfg = _ops_cfg(tmp_path)
    app, *_ = make_app(tmp_path, cfg=cfg)
    assert _client(app).get("/api/ops/health").status_code == 401


def test_health_403_for_non_admin(tmp_path):
    cfg = _ops_cfg(tmp_path)
    app, *_ = make_app(tmp_path, cfg=cfg)
    client = _client(app)
    login(client)
    assert client.get("/api/ops/health").status_code == 403


def test_health_reports_alive_engine_with_fresh_samples(tmp_path):
    """引擎每 cycle 寫一筆 equity 樣本 → 有新樣本＝存活（代理指標）。"""
    client, cfg = _h_app(tmp_path)
    now = datetime.now(timezone.utc).timestamp()
    _write_samples(cfg.state_base, ACCT_A, [(now - 7200, "1000"), (now - 30, "1010")])

    row = client.get("/api/ops/health").json()["followers"][0]
    assert row["engine_alive"] is True
    assert row["last_sample_age_s"] is not None and row["last_sample_age_s"] < 120
    assert row["sample_count"] == 2
    assert row["sample_coverage_sufficient"] is True
    assert row["killswitch_tripped"] is False and row["killswitch_known"] is True


def test_health_reports_stale_engine(tmp_path):
    """樣本很舊（> ENGINE_STALE_S）→ engine_alive=false（明確的壞，不是未知）。"""
    from spark.publicapi.ops import ENGINE_STALE_S

    client, cfg = _h_app(tmp_path)
    now = datetime.now(timezone.utc).timestamp()
    _write_samples(cfg.state_base, ACCT_A, [(now - ENGINE_STALE_S - 300, "1000")])

    row = client.get("/api/ops/health").json()["followers"][0]
    assert row["engine_alive"] is False


def test_no_samples_is_unknown_not_dead_and_not_alive(tmp_path):
    """⭐⭐ 無樣本 → engine_alive=null。**不是 false**（那是「已確認沒回應」），
    更不是 true。三態必須分得開，否則面板無法區分「引擎掛了」與「我們看不到」。"""
    client, cfg = _h_app(tmp_path)

    row = client.get("/api/ops/health").json()["followers"][0]
    assert row["engine_alive"] is None
    assert row["last_sample_age_s"] is None
    assert row["sample_count"] == 0


def test_liveness_basis_is_declared_not_claimed_as_process_check(tmp_path):
    """⭐ 誠實標註：這是 equity 樣本的代理，不是 process 檢查。
    把代理當成真的存活檢查上呈，是另一種謊報健康。"""
    client, cfg = _h_app(tmp_path)
    body = client.get("/api/ops/health").json()
    assert body["followers"][0]["liveness_basis"] == "equity_sample"
    assert body["engine_stale_after_s"] > 0


def test_health_reports_tripped_killswitch(tmp_path):
    client, cfg = _h_app(tmp_path)
    _trip_killswitch(cfg.state_base, ACCT_A)

    row = client.get("/api/ops/health").json()["followers"][0]
    assert row["killswitch_tripped"] is True
    assert client.get("/api/ops/health").json()["summary"][
        "killswitch_tripped_count"] == 1


def test_unreadable_samples_do_not_report_healthy_coverage(tmp_path):
    """⭐ 樣本檔存在但讀不出來 → coverage_known=false ＋ null，不報一個健康的覆蓋度。
    read_error 與「首次啟動無檔」是兩件事，後者較輕微。"""
    client, cfg = _h_app(tmp_path)
    p = Path(cfg.state_base) / ACCT_A / "var/copytrade/equity_samples.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[]")
    p.chmod(0o000)
    try:
        row = client.get("/api/ops/health").json()["followers"][0]
    finally:
        p.chmod(0o644)
    assert row["coverage_known"] is False
    assert row["sample_coverage_sufficient"] is None
    assert row["error"] is not None


def test_alerts_count_and_zero_is_a_real_zero(tmp_path):
    """告警檔不存在＝引擎從未寫過告警，那確實是 0（與「讀不到」不同）。"""
    client, cfg = _h_app(tmp_path)
    assert client.get("/api/ops/health").json()["followers"][0]["alerts"] == 0

    p = Path(cfg.state_base) / ACCT_A / "var/copytrade/alerts.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("a\nb\n\nc\n")
    body = client.get("/api/ops/health").json()
    assert body["followers"][0]["alerts"] == 3          # 空行不計
    assert body["summary"]["alerts_total"] == 3


def test_unapplied_leader_change_backlog_is_reported(tmp_path):
    """⭐ 「客戶簽了、API 收了、引擎從沒套用」的即時視圖——與日報同一個判定
    （leader_change_apply.scan_unapplied_leader_changes）。"""
    from spark.filet.leader_change import build_leader_change_record

    exchange = tmp_path / "exchange"
    exchange.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    issued = (now - timedelta(hours=2)).isoformat()
    rec = build_leader_change_record(
        account_id=ACCT_A, leader_address=LEADER, nonce="n1",
        issued_at=issued, signature="0xsig", message="m")
    (exchange / "leader_changes.json").write_text(json.dumps({"changes": [rec]}))

    client, cfg = _h_app(tmp_path, exchange_dir=exchange)
    body = client.get("/api/ops/health").json()
    assert body["summary"]["unapplied_leader_changes"] == 1
    entry = body["unapplied_leader_changes"][0]
    assert entry["account_id"] == ACCT_A and entry["reason"] == "not_redeemed"


def test_fresh_leader_change_is_not_backlog(tmp_path):
    """還新的記錄不算積壓（引擎可能只是還沒跑到下一輪）——不得狼來了。"""
    from spark.filet.leader_change import build_leader_change_record

    exchange = tmp_path / "exchange"
    exchange.mkdir(parents=True, exist_ok=True)
    issued = datetime.now(timezone.utc).isoformat()
    rec = build_leader_change_record(
        account_id=ACCT_A, leader_address=LEADER, nonce="n1",
        issued_at=issued, signature="0xsig", message="m")
    (exchange / "leader_changes.json").write_text(json.dumps({"changes": [rec]}))

    client, cfg = _h_app(tmp_path, exchange_dir=exchange)
    assert client.get("/api/ops/health").json()[
        "summary"]["unapplied_leader_changes"] == 0


def test_unreadable_change_file_yields_null_backlog_not_zero(tmp_path):
    """⭐⭐ 記錄檔讀不到 → 積壓回 **null ＋ 錯誤原文**，不是 0。
    0 會被讀成「沒有積壓」，實際上是「無從得知有沒有積壓」——正好相反的兩件事。"""
    exchange = tmp_path / "exchange"
    exchange.mkdir(parents=True, exist_ok=True)
    (exchange / "leader_changes.json").write_text("{ not json")

    client, cfg = _h_app(tmp_path, exchange_dir=exchange)
    summary = client.get("/api/ops/health").json()["summary"]
    assert summary["unapplied_leader_changes"] is None
    assert summary["leader_change_errors"], "查不下去必須留下原因"


def test_summary_separates_unknown_from_healthy(tmp_path):
    """⭐⭐ 「0 個引擎沒回應」與「全部引擎狀態都讀不到」在單一計數上長得一樣。
    每個計數都必須有 *_unknown 兄弟欄，否則面板會把看不見畫成健康。"""
    client, cfg = _h_app(tmp_path, refs=[_lref(), _lref(ACCT_B, ADDR_B, "B")])
    now = datetime.now(timezone.utc).timestamp()
    _write_samples(cfg.state_base, ACCT_A, [(now - 7200, "1"), (now - 10, "1")])

    s = client.get("/api/ops/health").json()["summary"]
    assert s["followers"] == 2
    assert s["engine_alive_count"] == 1
    assert s["engine_stale_count"] == 0          # B 不是「沒回應」
    assert s["engine_unknown_count"] == 1        # B 是「不知道」
    # ⚠️ 覆蓋度與存活是**兩個不同的三態**，刻意不合併：B 沒有樣本檔，
    # 「回撤保護尚未生效」是一個**確定**的答案（insufficient），而「引擎在不在」
    # 才是不知道。unknown 只保留給真正讀不到的情形（read_error）。
    assert s["coverage_insufficient_count"] == 1
    assert s["coverage_unknown_count"] == 0


def test_health_survives_a_missing_state_root(tmp_path):
    """狀態根整個不存在（follower 剛 activate、尚未啟動）→ 未知，不是 500。"""
    client, cfg = _h_app(tmp_path, state_base=tmp_path / "nonexistent")
    body = client.get("/api/ops/health").json()
    assert body["followers"][0]["engine_alive"] is None
    assert body["followers"][0]["killswitch_tripped"] is False


def test_unreadable_state_root_never_reports_killswitch_as_untripped(tmp_path):
    """⭐⭐ 本面板最不能犯的錯，寫成回歸測試。

    引擎的狀態根是 `filet-engine:filet-engine 0700`，面板跑在 filet-api 進程——
    跨了一道權限邊界。`Path.exists()` 會把 PermissionError **吞成 False**，於是
    `is_tripped()` 會對一個**確實已經觸發**的 kill switch 回答「沒有觸發」：面板
    會在客戶的引擎已經熔斷、部位已被平掉的當下，告訴管理員一切正常。

    正確行為：整列標未知（null）＋ error，絕不落到任何看起來健康的預設值。
    """
    client, cfg = _h_app(tmp_path)
    root = Path(cfg.state_base) / ACCT_A
    root.mkdir(parents=True, exist_ok=True)
    _trip_killswitch(cfg.state_base, ACCT_A)      # 確實觸發了
    root.chmod(0o000)                              # 但面板讀不到
    try:
        body = client.get("/api/ops/health").json()
    finally:
        root.chmod(0o755)

    row = body["followers"][0]
    assert row["killswitch_tripped"] is None, "讀不到絕不可回報成「沒有觸發」"
    assert row["killswitch_known"] is False
    assert row["engine_alive"] is None
    assert row["alerts"] is None
    assert row["error"] is not None
    assert body["summary"]["killswitch_unknown_count"] == 1
    assert body["summary"]["killswitch_tripped_count"] == 0   # 未知不算成已觸發


def test_absent_state_root_is_distinguished_from_unreadable(tmp_path):
    """`absent`（剛 activate、引擎沒跑過）與 `unreadable`（有東西但看不到）必須
    分開：前者「沒有 ARM 檔」是**真的**沒觸發，後者只能說不知道。"""
    from spark.publicapi.ops import state_root_status

    missing = tmp_path / "nope" / "acct"
    assert state_root_status(missing) == "absent"

    root = tmp_path / "state" / "acct"
    root.mkdir(parents=True)
    assert state_root_status(root) == "readable"
    root.chmod(0o000)
    try:
        assert state_root_status(root) == "unreadable"
    finally:
        root.chmod(0o755)
