"""tests/test_me_dashboard.py — GET /api/me/dashboard（客戶儀表板唯一資料源）。

盯住 Task 13 規格的四件事：
(1) 六塊＋持倉**每塊獨立 nullable**——子資料源丟例外只讓對應塊回 None，端點不 500。
(2) `available_pct` 數值錨例（withdrawable/margin_used，固定假資料手算釘死）。
(3) `fee_share_of_pnl_pct` 分母（|net+fees_paid|）為 0 → None，不得除零。
(4) 只回登入 session 自己的資料；未登入 401；pause 旗標翻轉 state。
"""
import json
import socket
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from spark.exchange.base import UserFill
from spark.filet.engine_health import build_heartbeat, heartbeat_path_for, write_heartbeat
from tests.publicapi_helpers import BUILDER, login, make_app, make_cfg

_REAL_SOCKET = socket.socket

DAY_MS = 86_400_000


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _client(app):
    return TestClient(app, base_url="https://testserver")


_LEADER = "0x" + "d4" * 20


def acct(wallet) -> str:
    return "f" + wallet.address[2:].lower()


def _write_manifest(tmp_path, followers):
    p = tmp_path / "followers.json"
    p.write_text(json.dumps({"followers": followers}))
    return str(p)


def _write_leaders(tmp_path, entries):
    p = tmp_path / "leaders.json"
    p.write_text(json.dumps({"leaders": entries}))
    return str(p)


def follower(wallet, **over):
    f = {"account_id": acct(wallet), "user_address": wallet.address.lower(),
         "builder_address": BUILDER, "network": "testnet", "label": "t",
         "leader_address": _LEADER}
    f.update(over)
    return f


def make_dash_app(tmp_path, *, followers=None, leaders=None):
    kwargs = dict(followers_path=_write_manifest(tmp_path, followers or []))
    if leaders is not None:
        kwargs["leaders_path"] = _write_leaders(tmp_path, leaders)
    cfg = make_cfg(tmp_path, **kwargs)
    app, cfg, store, keysvc, hl = make_app(tmp_path, cfg=cfg)
    return app, cfg, hl


def _logged_in(tmp_path, *, activated=True, leaders=None):
    wallet = Account.create()
    rows = [follower(wallet)] if activated else []
    app, cfg, hl = make_dash_app(tmp_path, followers=rows, leaders=leaders)
    client = _client(app)
    login(client, wallet=wallet)
    return client, cfg, hl, wallet


def write_hb(cfg, account_id, *, killswitch_tripped=False, util="0.2500",
            max_dd="0.10", risk_enabled=True, risk_source="customer_signed",
            capital_source="customer_signed", last_cycle="no_action", age_s=5.0):
    payload = build_heartbeat(
        account_id=account_id, now_s=time.time() - age_s,
        killswitch_tripped=killswitch_tripped, coverage=None, alerts_count=0,
        leader_address=_LEADER, leader_source="manifest", leader_kind="standard",
        allocated_capital="0", capital_utilization=util, use_full_equity=True,
        capital_source=capital_source, capital_changed_at=None,
        risk_controls_enabled=risk_enabled, risk_source=risk_source,
        risk_changed_at=None,
        risk_prefs={"max_drawdown_pct": max_dd} if risk_source != "unavailable" else None,
        risk_halt=None, cycle_result=last_cycle, cycle_detail=None)
    write_heartbeat(heartbeat_path_for(cfg.exchange_dir, account_id), payload)


def clearinghouse(account_value="1206.67", margin_used="418.05",
                  withdrawable="2.69", total_ntl_pos="521.20", positions=None):
    return {
        "marginSummary": {"accountValue": account_value,
                          "totalMarginUsed": margin_used,
                          "totalNtlPos": total_ntl_pos},
        "withdrawable": withdrawable,
        "assetPositions": positions or [],
    }


def position(coin="ETH", szi="4", entry="2452.76", leverage=25, lev_type="cross",
            upnl="1.59", margin_used="99.70"):
    return {"position": {"coin": coin, "szi": szi, "entryPx": entry,
                         "leverage": {"value": leverage, "type": lev_type},
                         "unrealizedPnl": upnl, "marginUsed": margin_used}}


def portfolio_rows(points, period="perpMonth"):
    """points = [(day_offset, account_value, cum_pnl)] → portfolio() 形狀。"""
    av = [[d * DAY_MS, str(a)] for d, a, _ in points]
    pnl = [[d * DAY_MS, str(p)] for d, _, p in points]
    return [[period, {"accountValueHistory": av, "pnlHistory": pnl}]]


def fill(addr, sz="1", px="100", crossed=True, builder_fee="0.5", t=None):
    return UserFill(time=t or datetime(2026, 8, 1, tzinfo=timezone.utc), coin="ETH",
                    px=Decimal(px), sz=Decimal(sz), side="B", crossed=crossed,
                    oid=1, fee=Decimal("0.1"), builder_fee=Decimal(builder_fee))


# ── 授權 ──────────────────────────────────────────────────────────────

def test_requires_session(tmp_path):
    app, _cfg, _hl = make_dash_app(tmp_path)
    assert _client(app).get("/api/me/dashboard").status_code == 401


def test_only_returns_own_data(tmp_path):
    """⭐ 兩個帳號的鏈上資料都在，各自只看得到自己的那一份（結構上沒有 account
    參數，account_id 完全由 session 衍生）。"""
    a, b = Account.create(), Account.create()
    app, cfg, hl = make_dash_app(tmp_path, followers=[follower(a), follower(b)])
    hl.clearinghouse[a.address.lower()] = clearinghouse(account_value="1000.00")
    hl.clearinghouse[b.address.lower()] = clearinghouse(account_value="9999999.00")

    ca = _client(app)
    login(ca, wallet=a)
    body = ca.get("/api/me/dashboard").json()
    assert body["equity"]["account_value"] == "1000.00"
    assert "9999999.00" not in json.dumps(body)


# ── 完整形狀 ──────────────────────────────────────────────────────────

def test_full_shape_happy_path(tmp_path):
    leaders = [{"address": _LEADER, "name": "Filet Core", "max_leverage": "3.0"}]
    client, cfg, hl, wallet = _logged_in(tmp_path, leaders=leaders)
    addr = wallet.address.lower()
    write_hb(cfg, acct(wallet))
    hl.clearinghouse[addr] = clearinghouse(positions=[position()])
    hl.portfolios[addr] = portfolio_rows(
        [(0, "1000", "0"), (10, "1050", "50")])
    hl.fills[addr] = [fill(addr)]

    r = client.get("/api/me/dashboard")
    assert r.status_code == 200, r.text
    body = r.json()

    for key in ("status", "equity", "exposure", "pnl", "sync", "fees_month",
               "positions", "updated_at"):
        assert key in body

    assert body["status"]["strategy_name"] == "Filet Core"
    assert body["status"]["state"] == "following"
    assert body["status"]["guards"]["leverage"]["max"] == "3.0"
    assert body["status"]["guards"]["scale"]["max"] == "0.2500"
    assert body["status"]["guards"]["drawdown"]["max"] == "-0.10"
    assert body["status"]["guards"]["drawdown"]["enabled"] is True
    # ⭐⭐ drawdown.now 刻意恆為 null（見 app.py _dashboard_guards docstring）：
    # 沒有同基準（引擎 7 天滾動高水位）的來源可用。
    assert body["status"]["guards"]["drawdown"]["now"] is None

    assert body["equity"]["account_value"] == "1206.67"
    assert body["equity"]["margin_used"] == "418.05"
    assert body["equity"]["withdrawable"] == "2.69"
    assert body["equity"]["ret_30d_pct"] is not None

    assert body["exposure"]["notional"] == "521.20"
    assert body["exposure"]["position_count"] == 1
    assert body["exposure"]["max_position"]["symbol"] == "ETH"

    assert len(body["positions"]) == 1
    p = body["positions"][0]
    assert p["symbol"] == "ETH" and p["side"] == "long"
    # value = marginUsed(99.70) * leverage(25) = 2492.50（同源代數推導，見
    # app.py _dashboard_positions_raw docstring）
    assert p["value"] == "2492.50"
    # mark = entry(2452.76) + upnl(1.59)/szi(4) = 2453.1575
    assert p["mark"] == "2453.1575"

    assert body["pnl"]["net"] is not None
    assert body["sync"] is not None
    assert body["fees_month"] is not None
    assert body["fees_month"]["fill_count"] == 1


# ── available_pct 錨例 ───────────────────────────────────────────────

def test_available_pct_anchor(tmp_path):
    """2.69 / 418.05 = 0.00643469...，quantize 4 位 → 0.0064（Task 13 規格
    自帶的錨例：`available_pct 錨例（2.69/418.05...）`）。"""
    client, cfg, hl, wallet = _logged_in(tmp_path)
    addr = wallet.address.lower()
    hl.clearinghouse[addr] = clearinghouse(
        account_value="1206.67", margin_used="418.05", withdrawable="2.69")

    body = client.get("/api/me/dashboard").json()
    assert body["equity"]["available_pct"] == "0.0064"


def test_available_pct_null_when_margin_used_zero(tmp_path):
    client, cfg, hl, wallet = _logged_in(tmp_path)
    addr = wallet.address.lower()
    hl.clearinghouse[addr] = clearinghouse(margin_used="0", withdrawable="5.00")

    body = client.get("/api/me/dashboard").json()
    assert body["equity"]["available_pct"] is None


# ── fee_share 分母 0 → null ──────────────────────────────────────────

def test_fee_share_null_when_cum_pnl_is_zero(tmp_path):
    """net + fees_paid ≡ cum_pnl（net 的定義式）；cum_pnl=0 時分母恆為 0，
    fee_share_of_pnl_pct 必須是 None，不得除零。"""
    client, cfg, hl, wallet = _logged_in(tmp_path)
    addr = wallet.address.lower()
    hl.clearinghouse[addr] = clearinghouse()
    hl.portfolios[addr] = portfolio_rows([(0, "1000", "0"), (10, "1000", "0")])
    hl.fills[addr] = [fill(addr, builder_fee="1.2")]

    body = client.get("/api/me/dashboard").json()
    assert body["pnl"]["net"] == "-1.2"
    assert body["pnl"]["fee_share_of_pnl_pct"] is None


# ── 子塊獨立 nullable ────────────────────────────────────────────────

def test_clearinghouse_failure_nulls_only_equity_exposure_positions(tmp_path):
    leaders = [{"address": _LEADER, "name": "Filet Core", "max_leverage": "3.0"}]
    client, cfg, hl, wallet = _logged_in(tmp_path, leaders=leaders)
    addr = wallet.address.lower()
    hl.clearinghouse_error[addr] = ConnectionError("boom")
    write_hb(cfg, acct(wallet))
    hl.portfolios[addr] = portfolio_rows([(0, "1000", "0"), (10, "1050", "50")])

    body = client.get("/api/me/dashboard")
    assert body.status_code == 200, body.text
    body = body.json()

    assert body["equity"] is None
    assert body["exposure"] is None
    assert body["positions"] is None
    # 其餘塊不連坐：status 仍是一個物件（只有依賴 acct 的 guards 子欄位是 null）
    assert body["status"] is not None
    assert body["status"]["state"] == "following"
    assert body["status"]["guards"]["scale"]["now"] is None
    assert body["status"]["guards"]["leverage"]["now"] is None
    assert body["pnl"] is not None
    assert body["sync"] is not None


def test_fills_failure_nulls_fees_month_but_not_equity(tmp_path):
    client, cfg, hl, wallet = _logged_in(tmp_path)
    addr = wallet.address.lower()
    hl.clearinghouse[addr] = clearinghouse()
    hl.fills_error[addr] = ConnectionError("boom")

    body = client.get("/api/me/dashboard")
    assert body.status_code == 200, body.text
    body = body.json()
    assert body["fees_month"] is None
    assert body["equity"] is not None
    assert body["equity"]["account_value"] == "1206.67"


# ── inactive／pause／halted 狀態 ─────────────────────────────────────

def test_inactive_when_not_activated(tmp_path):
    client, cfg, hl, wallet = _logged_in(tmp_path, activated=False)
    body = client.get("/api/me/dashboard").json()
    assert body["status"]["state"] == "inactive"
    assert body["status"]["strategy_name"] is None
    assert body["status"]["signal_source_ok"] is None


def test_pause_flag_sets_state_paused(tmp_path):
    client, cfg, hl, wallet = _logged_in(tmp_path)
    write_hb(cfg, acct(wallet))
    p = Path(cfg.exchange_dir) / wallet.address.lower() / "pause.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"paused": True, "ts": 1, "by": "owner"}))

    body = client.get("/api/me/dashboard").json()
    assert body["status"]["state"] == "paused"


def test_missing_pause_file_is_not_paused(tmp_path):
    """讀不到 pause.json（不存在）→ 視為未暫停（Task 13 規格明文）。"""
    client, cfg, hl, wallet = _logged_in(tmp_path)
    write_hb(cfg, acct(wallet))

    body = client.get("/api/me/dashboard").json()
    assert body["status"]["state"] == "following"


def test_corrupt_pause_file_does_not_force_paused_but_flags_uncertainty(tmp_path):
    """⭐ pause 旗標讀取失敗（格式壞）→ 顯示層**不**比照引擎側直接判定暫停
    （那是 Task 15 動作側的 fail-safe 方向）；改用其他訊號判定 state，並在
    signal_source_ok 反映讀不準（Task 13 規格明文的行為差異）。"""
    client, cfg, hl, wallet = _logged_in(tmp_path)
    write_hb(cfg, acct(wallet))
    p = Path(cfg.exchange_dir) / wallet.address.lower() / "pause.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not json")

    body = client.get("/api/me/dashboard").json()
    assert body["status"]["state"] == "following"
    assert body["status"]["signal_source_ok"] is False


def test_halted_when_killswitch_tripped(tmp_path):
    client, cfg, hl, wallet = _logged_in(tmp_path)
    write_hb(cfg, acct(wallet), killswitch_tripped=True)

    body = client.get("/api/me/dashboard").json()
    assert body["status"]["state"] == "halted"


def test_missing_heartbeat_signal_source_not_ok_but_state_still_following(tmp_path):
    """沒有心跳（引擎從未回報）→ `signal_source_ok=False`，但 `state` 不因此
    自行判定為暫停或熔斷（那需要明確訊號）——維持預設 `following` 並讓
    signal_source_ok 反映不確定性。"""
    client, cfg, hl, wallet = _logged_in(tmp_path)

    body = client.get("/api/me/dashboard").json()
    assert body["status"]["state"] == "following"
    assert body["status"]["signal_source_ok"] is False
