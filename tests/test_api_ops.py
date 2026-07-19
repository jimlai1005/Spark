"""tests/test_api_ops.py
營運後台 /api/ops/*（admin only）：每客戶損益、收入對帳，以及 accrued 歷史序列。

⭐ 本檔的核心是 **admin 閘的結構性斷言**：/ops 是全 repo 唯一的跨客戶聚合存取模式
（其餘端點皆 session-scoped），漏掉一道閘＝任何登入者都能看到所有客戶的部位與收入。
test_all_admin_scoped_routes_are_gated 走 FastAPI 的 dependant 樹逐路由檢查——
未來新增的 /api/ops/* 端點若忘了掛閘，該測試會直接紅，不靠人記得。
"""
import json
import socket
from datetime import datetime, timezone
from decimal import Decimal

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
                          "list_subscriptions"}


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
    for mod, names in ((ops_mod, ("customer_pnl", "subscription_drift")),
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
                            "/api/ops/subscriptions"}


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
                    "/api/admin/pending"}


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
