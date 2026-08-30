"""tests/test_dashboard_sync.py — `/api/me/dashboard` 的 `sync` 塊（M3 R4-1）。

盯住 R4-1 規格的四件事：
(1) 配對/延遲(p95)/價差手算錨例（leader/follower fills 寫死、手算比對）。
(2) 空 fills（未跟單）→ 整塊 `None`，端點不 500。
(3) 60s in-process 快取：TTL 窗內上游只叫一次；過窗重新查詢。
(4) 未同步倉位數／部位比例偏差錨例（心跳 capital_utilization 當 scale）。

零落盤：本檔全程不斷言／不依賴任何檔案寫入（sync 專屬）——只驗證 in-memory 行為。
"""
import json
import socket
import time as _time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from spark.exchange.base import UserFill
from spark.filet.engine_health import build_heartbeat, heartbeat_path_for, write_heartbeat
from spark.publicapi.store import ApiStore
from tests.publicapi_helpers import BUILDER, FakeKeysvc, FakeHL, login, make_cfg
from spark.publicapi.app import create_app

_REAL_SOCKET = socket.socket


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


def follower(wallet, **over):
    f = {"account_id": acct(wallet), "user_address": wallet.address.lower(),
         "builder_address": BUILDER, "network": "testnet", "label": "t",
         "leader_address": _LEADER}
    f.update(over)
    return f


def _make_app(tmp_path, *, followers, now_fn=None):
    cfg = make_cfg(tmp_path, followers_path=_write_manifest(tmp_path, followers))
    store = ApiStore(cfg.db_path)
    keysvc, hl = FakeKeysvc(), FakeHL()
    kw = {} if now_fn is None else {"now_fn": now_fn}
    app = create_app(cfg, store, keysvc, hl, **kw)
    return app, cfg, hl


def _logged_in(tmp_path, *, activated=True, now_fn=None):
    wallet = Account.create()
    rows = [follower(wallet)] if activated else []
    app, cfg, hl = _make_app(tmp_path, followers=rows, now_fn=now_fn)
    client = _client(app)
    login(client, wallet=wallet)
    return client, cfg, hl, wallet


def write_hb(cfg, account_id, *, util="0.5000", capital_source="customer_signed",
            now_s, age_s=5.0):
    payload = build_heartbeat(
        account_id=account_id, now_s=now_s - age_s,
        killswitch_tripped=False, coverage=None, alerts_count=0,
        leader_address=_LEADER, leader_source="manifest", leader_kind="standard",
        allocated_capital="0", capital_utilization=util, use_full_equity=True,
        capital_source=capital_source, capital_changed_at=None,
        risk_controls_enabled=True, risk_source="customer_signed",
        risk_changed_at=None, risk_prefs={"max_drawdown_pct": "0.10"},
        risk_halt=None, cycle_result="no_action", cycle_detail=None)
    write_heartbeat(heartbeat_path_for(cfg.exchange_dir, account_id), payload)


def clearinghouse(positions=None, account_value="1000.00"):
    return {
        "marginSummary": {"accountValue": account_value,
                          "totalMarginUsed": "0", "totalNtlPos": "0"},
        "withdrawable": "0",
        "assetPositions": positions or [],
    }


def position(coin, szi, leverage, margin_used, entry="100", upnl="0"):
    return {"position": {"coin": coin, "szi": szi, "entryPx": entry,
                         "leverage": {"value": leverage, "type": "cross"},
                         "unrealizedPnl": upnl, "marginUsed": margin_used}}


def mkfill(coin, px, t, side="B", crossed=True):
    return UserFill(time=t, coin=coin, px=Decimal(px), sz=Decimal("1"), side=side,
                    crossed=crossed, oid=1, fee=Decimal("0.1"),
                    builder_fee=Decimal("0"))


T0 = datetime(2026, 8, 20, 0, 0, 0, tzinfo=timezone.utc)


# ── 未跟單 → 整塊 None ───────────────────────────────────────────────

def test_sync_null_when_not_activated(tmp_path):
    client, cfg, hl, wallet = _logged_in(tmp_path, activated=False)
    body = client.get("/api/me/dashboard").json()
    assert body["sync"] is None


def test_sync_not_500_with_empty_fills(tmp_path):
    client, cfg, hl, wallet = _logged_in(tmp_path)
    write_hb(cfg, acct(wallet), now_s=_time.time())
    r = client.get("/api/me/dashboard")
    assert r.status_code == 200, r.text
    sync = r.json()["sync"]
    assert sync["data_state"] == "ok"
    assert sync["latency_median_ms"] is None
    assert sync["latency_p95_ms"] is None
    assert sync["missed_signals_24h"] == 0
    assert sync["missed_reason"] is None


# ── 配對／延遲(p95)／價差手算錨例 ────────────────────────────────────

def test_pairing_latency_p95_price_diff_and_missed_signals_anchor(tmp_path):
    """三組配對（延遲 1s/2s/10s；價差 50/100/150 bp）＋一筆 leader 獨有的 BTC 成交
    （follower 完全沒有 BTC 成交，保證配不到，不受配對時間窗影響）。

    - median_delay_s：sorted [1,2,10] 取中間 → 2s → latency_median_ms=2000。
    - p95（奈斯排名法 ⌈0.95×3⌉=3 → 第 3 個，1-based）→ 10s → latency_p95_ms=10000。
    - taker_slippage_bp_median：sorted [50,100,150] 取中間 → 100bp。
    - missed_signals_24h：leader 4 筆 − 配對到 3 筆 = 1。
    """
    client, cfg, hl, wallet = _logged_in(tmp_path)
    addr = wallet.address.lower()
    write_hb(cfg, acct(wallet), now_s=_time.time())

    hl.fills[_LEADER] = [
        mkfill("ETH", "100", T0),
        mkfill("ETH", "200", T0 + timedelta(seconds=1000)),
        mkfill("ETH", "300", T0 + timedelta(seconds=2000)),
        mkfill("BTC", "50000", T0 + timedelta(seconds=3000)),
    ]
    hl.fills[addr] = [
        mkfill("ETH", "100.5", T0 + timedelta(seconds=1)),
        mkfill("ETH", "202", T0 + timedelta(seconds=1002)),
        mkfill("ETH", "304.5", T0 + timedelta(seconds=2010)),
    ]

    body = client.get("/api/me/dashboard").json()
    sync = body["sync"]
    assert sync["data_state"] == "ok"
    assert sync["latency_median_ms"] == 2000
    assert sync["latency_p95_ms"] == 10000
    assert Decimal(sync["price_diff_bp"]) == Decimal("100")
    assert sync["missed_signals_24h"] == 1
    # ⭐ 沒有既有來源能診斷「為什麼」漏配 → 維持 None，不虛構分類
    assert sync["missed_reason"] is None


# ── 未同步倉位數／部位比例偏差錨例 ───────────────────────────────────

def test_unsynced_positions_and_scale_deviation_anchor(tmp_path):
    """scale=0.5（心跳 capital_utilization）。leader 持有 ETH long（value=1000）
    與 BTC short（value=100，follower 沒有）；follower 持有 ETH long（value=550）。

    - ETH：期望值＝1000×0.5＝500；實際 550 → 偏差 = |550-500|/500×100 = 10.00%。
    - BTC：leader 有、follower 沒有 → 未同步 +1。
    - 只有 ETH 可比對（唯一雙邊同方向的 coin）→ scale_deviation_pct 取該值（最差值
      在只有一個可比對象時就是該值本身）。
    """
    client, cfg, hl, wallet = _logged_in(tmp_path)
    addr = wallet.address.lower()
    write_hb(cfg, acct(wallet), now_s=_time.time(), util="0.5000")

    hl.clearinghouse[_LEADER] = clearinghouse(positions=[
        position("ETH", szi="4", leverage=10, margin_used="100"),   # value=1000
        position("BTC", szi="-2", leverage=5, margin_used="20"),    # value=100 (short)
    ])
    hl.clearinghouse[addr] = clearinghouse(positions=[
        position("ETH", szi="4", leverage=10, margin_used="55"),    # value=550
    ])

    body = client.get("/api/me/dashboard").json()
    sync = body["sync"]
    assert sync["unsynced_positions"] == 1
    assert sync["scale_deviation_pct"] == "10.00"


def test_scale_deviation_null_when_heartbeat_missing_capital_source(tmp_path):
    """心跳存在但 `capital.source` 不是 customer_signed/env_default（此處直接不寫
    心跳＝status "missing"）→ scale 未知 → `scale_deviation_pct` 為 None，
    但兩側持倉都讀得到時 `unsynced_positions` 仍可算（判準不同，見 docstring）。"""
    client, cfg, hl, wallet = _logged_in(tmp_path)
    addr = wallet.address.lower()
    # 刻意不寫心跳。

    hl.clearinghouse[_LEADER] = clearinghouse(positions=[
        position("ETH", szi="4", leverage=10, margin_used="100"),
    ])
    hl.clearinghouse[addr] = clearinghouse(positions=[
        position("ETH", szi="4", leverage=10, margin_used="55"),
    ])

    body = client.get("/api/me/dashboard").json()
    sync = body["sync"]
    assert sync["data_state"] == "warming"
    assert sync["scale_deviation_pct"] is None
    assert sync["unsynced_positions"] == 0


# ── 60s in-process 快取：上游只叫一次 ─────────────────────────────────

def test_sync_upstream_called_once_within_60s_cache_window(tmp_path, monkeypatch):
    clock = {"t": 1_000_000.0}
    client, cfg, hl, wallet = _logged_in(tmp_path, now_fn=lambda: clock["t"])
    addr = wallet.address.lower()
    write_hb(cfg, acct(wallet), now_s=clock["t"])
    hl.fills[_LEADER] = [mkfill("ETH", "100", T0)]
    hl.fills[addr] = [mkfill("ETH", "100.5", T0 + timedelta(seconds=1))]

    # ⭐ 只數「查 leader 成交」這一種呼叫——它**只**發生在 `_compute_dashboard_sync`
    # 內部（`fees_month`／`positions` 等其他區塊只查 follower 自己的位址），
    # 這樣才能不受同一次請求裡其他區塊各自快取層（例如 fees_month 的 300s TTL）
    # 干擾，乾淨地只驗證 sync 這一層的 60s 快取語意。
    calls = {"n": 0}
    real_get_user_fills = hl.get_user_fills

    def _counting_get_user_fills(address, start, end):
        if address == _LEADER:
            calls["n"] += 1
        return real_get_user_fills(address, start, end)

    monkeypatch.setattr(hl, "get_user_fills", _counting_get_user_fills)

    r1 = client.get("/api/me/dashboard")
    assert r1.status_code == 200, r1.text
    first_recon = r1.json()["sync"]["last_recon_ts"]
    assert calls["n"] == 1

    clock["t"] += 30.0
    r2 = client.get("/api/me/dashboard")
    assert r2.status_code == 200, r2.text
    assert calls["n"] == 1          # 仍在 60s 窗內，快取命中
    assert r2.json()["sync"]["last_recon_ts"] == first_recon

    clock["t"] += 31.0
    r3 = client.get("/api/me/dashboard")
    assert r3.status_code == 200, r3.text
    assert calls["n"] == 2          # 累計 61s，重新查詢
    assert r3.json()["sync"]["last_recon_ts"] != first_recon
