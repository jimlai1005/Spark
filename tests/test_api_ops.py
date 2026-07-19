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


def _fill(sz="2", px="100", crossed=True, builder_fee="0.5"):
    return UserFill(time=datetime(2026, 7, 19, tzinfo=timezone.utc), coin="ETH",
                    px=Decimal(px), sz=Decimal(sz), side="B", crossed=crossed,
                    oid=1, fee=Decimal("0.1"), builder_fee=Decimal(builder_fee))


def _manifest(tmp_path, refs):
    p = tmp_path / "followers.json"
    p.write_text(json.dumps({"followers": [
        {"account_id": r.account_id, "user_address": r.user_address,
         "builder_address": r.builder_address, "network": r.network,
         "label": r.label} for r in refs]}))
    return p


def _ops_cfg(tmp_path, admin_addresses=frozenset(), refs=None, history=None):
    refs = refs if refs is not None else [_ref()]
    hist = tmp_path / "accrued_history.jsonl"
    if history is not None:
        hist.write_text("".join(
            json.dumps({"date": d, "accrued": str(a)}) + "\n" for d, a in history))
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


def test_all_admin_scoped_routes_are_gated(tmp_path):
    """結構性：每個 /api/ops/* 與 /api/admin/* 路由的 dependant 樹裡都必須有
    _require_admin。新端點忘了掛閘 → 本測試紅（不靠 code review 記得）。"""
    app, *_ = make_app(tmp_path)
    seen = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if not (path.startswith("/api/ops/") or path.startswith("/api/admin/")):
            continue
        assert "_require_admin" in _dep_call_names(route.dependant), \
            f"{path} 未掛 admin 閘"
        seen.add(path)
    assert seen == {"/api/ops/customers", "/api/ops/revenue", "/api/admin/pending"}


@pytest.mark.parametrize("path", ["/api/ops/customers", "/api/ops/revenue"])
def test_ops_requires_session(tmp_path, path):
    cfg = _ops_cfg(tmp_path)
    app, *_ = make_app(tmp_path, cfg=cfg)
    assert _client(app).get(path).status_code == 401


@pytest.mark.parametrize("path", ["/api/ops/customers", "/api/ops/revenue"])
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
    p = tmp_path / "h.jsonl"
    p.write_text('{"date": "2026-07-19", "accrued": "2"}\n'
                 'not-json\n'
                 '\n'
                 '{"date": "2026-07-18", "accrued": "1"}\n'
                 '{"accrued": "9"}\n')
    assert load_accrued_series(p) == [("2026-07-18", Decimal("1")),
                                      ("2026-07-19", Decimal("2"))]


def test_load_accrued_series_missing_file(tmp_path):
    assert load_accrued_series(tmp_path / "nope.jsonl") == []


def test_append_accrued_history_idempotent(tmp_path, monkeypatch):
    """⭐ 同日重跑覆蓋該日那一行（不是再 append 一筆）——否則今昨差會被算成 0。"""
    import scripts.copytrade_daily_report as rpt
    monkeypatch.setattr(rpt, "HISTORY_PATH", tmp_path / "accrued_history.jsonl")
    rpt.append_accrued_history("2026-07-18", Decimal("1.5"))
    rpt.append_accrued_history("2026-07-19", Decimal("2.5"))
    rpt.append_accrued_history("2026-07-19", Decimal("2.75"))   # 同日重跑
    assert load_accrued_series(rpt.HISTORY_PATH) == [("2026-07-18", Decimal("1.5")),
                                                     ("2026-07-19", Decimal("2.75"))]
    assert len(rpt.HISTORY_PATH.read_text().strip().splitlines()) == 2


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
    today = datetime.now(timezone.utc).date().isoformat()
    client, cfg, store, hl = _admin_app(
        tmp_path, refs=[_ref(), _ref(ACCT_B, ADDR_B, "B")],
        history=[("2026-07-01", "10"), (today, "16")])
    hl.fills[ADDR_A] = [_fill(builder_fee="2")]
    hl.fills[ADDR_B] = [_fill(builder_fee="3")]
    body = client.get("/api/ops/revenue", params={"threshold_pct": 0.1}).json()
    assert body["insufficient_accrued_history"] is False
    assert body["attributed"] == "5"          # 2 + 3（歸屬／應收）
    assert body["accrued_delta"] == "6"       # 16 − 10（北極星／實收，查一次不加總）
    assert body["discrepancy"] == "1"
    assert body["over_threshold"] is True     # 1/5 = 20% > 10%
    assert body["day"] == today and body["prev_day"] == "2026-07-01"
    assert len(body["customers"]) == 2


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
