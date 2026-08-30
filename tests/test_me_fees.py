"""tests/test_me_fees.py — M3 round3 Task 2：費用明細逐日聚合＋期間切換。

盯住 plan Task 2 的三件事：
(1) `daily_bars`／`daily` 每列擴充為 `{date, fill_count, routed_volume, builder_fee,
    effective_rate_bps}`；無成交日不產生列，`builder_fee=0` 但有成交的日子照實列出
    （$0.00 與「無成交」語意分開，R2·B）。
(2) `GET /api/me/fees?period=this_month|last_month|all`：同一個 `collect_follower_summary`
    資料源（不另拼第二來源），`pnl_share_pct` 在無同基準 PnL 來源時為 `None`。
(3) `/api/me/dashboard` 既有欄位（`fees_month.fill_count` 等）不被破壞。

全離線（tests/conftest.py 的 autouse socket-ban；計算層測試直接呼叫純 Python 函式，
端點層測試用 TestClient + 真實 SIWE 登入，不經真實網路——見 `_allow_local_sockets`
fixture，同 test_me_dashboard.py 慣例：TestClient 走 loopback，需要放行 real socket）。
"""
import socket
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from spark.exchange.base import UserFill
from spark.filet.followers import FollowerRef
from spark.publicapi.app import (
    _dashboard_fees_month,
    _dashboard_fees_period,
    _fee_daily_bars,
    _fees_all_time_start,
    _month_bounds,
)
from tests.publicapi_helpers import BUILDER, login, make_app, make_cfg

_REAL_SOCKET = socket.socket


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _client(app):
    return TestClient(app, base_url="https://testserver")


def _ref(account_id="fabc", address=None) -> FollowerRef:
    return FollowerRef(account_id=account_id, user_address=address or "0x" + "ab" * 20,
                       builder_address=BUILDER, network="testnet")


def _fill(day: datetime, *, sz="1", px="100", builder_fee="0.5") -> UserFill:
    return UserFill(time=day, coin="ETH", px=Decimal(px), sz=Decimal(sz),
                    side="B", crossed=True, oid=1, fee=Decimal("0.01"),
                    builder_fee=Decimal(builder_fee))


class _WindowAwareHL:
    """比 FakeHL 更接近真實行為的最小替身：`get_user_fills` 一律照 [start, end]
    過濾（`FakeHL.window_aware` 開了也一樣，但這裡不需要其他 FakeHL 的能力，
    寫一個最小替身讓測試意圖更直接）。"""

    def __init__(self, fills: list[UserFill], *, portfolio_rows=None):
        self._fills = fills
        self._portfolio_rows = portfolio_rows if portfolio_rows is not None else []
        self.calls = 0

    def get_user_fills(self, address, start, end) -> list:
        self.calls += 1
        return [f for f in self._fills if start <= f.time <= end]

    def portfolio(self, address: str) -> list:
        return self._portfolio_rows


_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc).timestamp()


# ── _fee_daily_bars：欄位擴充＋無成交日不產生列 ─────────────────────────

def test_daily_bars_expanded_fields_and_skips_no_fill_days():
    ref = _ref()
    fills = [
        _fill(datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc),
             sz="1", px="100", builder_fee="0.3"),
        _fill(datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc),
             sz="1", px="100", builder_fee="0.2"),
        # Aug 2：無成交（不產生列）
        _fill(datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
             sz="1", px="50", builder_fee="0"),  # 有成交但 fee=0
    ]
    hl = _WindowAwareHL(fills)
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)

    bars = _fee_daily_bars(ref, hl, start, end)

    assert [b["date"] for b in bars] == ["2026-08-01", "2026-08-03"]  # Aug 2 不出現

    day1 = bars[0]
    assert day1["fill_count"] == 2
    assert day1["routed_volume"] == Decimal("200")
    assert day1["builder_fee"] == Decimal("0.5")
    # 25.00 bps = 0.5 / 200 * 10000
    assert day1["effective_rate_bps"] == Decimal("25.00")

    day3 = bars[1]
    assert day3["fill_count"] == 1
    assert day3["routed_volume"] == Decimal("50")
    assert day3["builder_fee"] == Decimal("0")  # $0.00 有成交，非「無成交」
    assert day3["effective_rate_bps"] == Decimal("0.00")


def test_daily_bars_empty_when_no_fills_in_range():
    ref = _ref()
    hl = _WindowAwareHL([])
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = datetime(2026, 8, 3, tzinfo=timezone.utc)
    assert _fee_daily_bars(ref, hl, start, end) == []


# ── /api/me/dashboard 的 fees_month.daily_bars 沿用同一個聚合層 ─────────

def test_dashboard_fees_month_daily_bars_use_expanded_shape():
    ref = _ref()
    hl = _WindowAwareHL([
        _fill(datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc), builder_fee="0.5"),
    ])
    result = _dashboard_fees_month(ref, hl, _NOW, cache={})
    assert result["daily_bars"] == [{
        "date": "2026-08-01", "fill_count": 1,
        "routed_volume": Decimal("100"), "builder_fee": Decimal("0.5"),
        "effective_rate_bps": Decimal("50.00"),
    }]
    # 既有頂層欄位不變（不破壞既有形狀）
    assert result["fill_count"] == 1
    assert result["routed_volume"] == Decimal("100")
    assert result["builder_fees"] == Decimal("0.5")


# ── _month_bounds：this_month / last_month 邊界 ──────────────────────────

def test_month_bounds_this_month():
    now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    start, end = _month_bounds(now, months_back=0)
    assert start == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert end == now


def test_month_bounds_last_month():
    now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    start, end = _month_bounds(now, months_back=1)
    assert start == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_month_bounds_last_month_crosses_year():
    now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    start, end = _month_bounds(now, months_back=1)
    assert start == datetime(2025, 12, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 1, 1, tzinfo=timezone.utc)


# ── _dashboard_fees_period：三個 period 各自資料範圍正確 ─────────────────

def test_period_this_month_excludes_last_month_fill():
    # ⭐ 刻意用非午夜時刻（09:00）：`userFillsByTime` 的 start/end 兩端皆含（同真實
    # HL `userFillsByTime` 語意，見 hl.py get_user_fills），若成交剛好落在
    # UTC 午夜整，會同時落進前一天與當天兩個 [day, day+1) 查詢窗——這是既有
    # 逐日迴圈設計就有的邊界重疊（本 task 未變更此語意，見回報「未涵蓋事項」），
    # 用非午夜時刻的成交時間避免測試踩到這個已知邊界而非本 task 要驗證的行為。
    ref = _ref()
    hl = _WindowAwareHL([
        _fill(datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc), builder_fee="1.0"),  # 本月
        _fill(datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc), builder_fee="9.0"),  # 上月
    ])
    result = _dashboard_fees_period(ref, hl, _NOW, "this_month", cache={})
    assert result["summary"]["fill_count"] == 1
    assert result["summary"]["builder_fees"] == Decimal("1.0")
    assert [b["date"] for b in result["daily"]] == ["2026-08-05"]


def test_period_last_month_excludes_this_month_fill():
    ref = _ref()
    hl = _WindowAwareHL([
        _fill(datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc), builder_fee="1.0"),  # 本月
        _fill(datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc), builder_fee="9.0"),  # 上月
    ])
    result = _dashboard_fees_period(ref, hl, _NOW, "last_month", cache={})
    assert result["summary"]["fill_count"] == 1
    assert result["summary"]["builder_fees"] == Decimal("9.0")
    assert [b["date"] for b in result["daily"]] == ["2026-07-20"]


def test_period_all_uses_portfolio_all_time_start():
    """`all` 的起點＝perpAllTime accountValueHistory 首點；早於該點的成交不計入
    （帳戶實際交易起點之前不該有資料，即使 FakeHL 塞了假成交）。"""
    ref = _ref()
    all_time_start_ms = int(datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp() * 1000)
    portfolio_rows = [["perpAllTime", {
        "accountValueHistory": [[all_time_start_ms, "1000"],
                                [all_time_start_ms + 86_400_000, "1010"]],
        "pnlHistory": [[all_time_start_ms, "0"], [all_time_start_ms + 86_400_000, "10"]],
    }]]
    hl = _WindowAwareHL([
        _fill(datetime(2026, 4, 15, 9, 0, tzinfo=timezone.utc), builder_fee="99"),  # 起點之前
        _fill(datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc), builder_fee="2.0"),  # 起點之後
    ], portfolio_rows=portfolio_rows)

    result = _dashboard_fees_period(ref, hl, _NOW, "all", cache={})
    assert result["summary"]["fill_count"] == 1
    assert result["summary"]["builder_fees"] == Decimal("2.0")
    assert [b["date"] for b in result["daily"]] == ["2026-06-01"]


def test_period_all_falls_back_when_portfolio_unavailable():
    """`portfolio()` 回空（無 perpAllTime 視窗）→ 退回安全上界（400 天），
    範圍內的成交仍然照算，不是整批消失。"""
    ref = _ref()
    hl = _WindowAwareHL([
        _fill(datetime(2026, 6, 1, tzinfo=timezone.utc), builder_fee="3.0"),
    ], portfolio_rows=[])
    result = _dashboard_fees_period(ref, hl, _NOW, "all", cache={})
    assert result["summary"]["fill_count"] == 1
    assert result["summary"]["builder_fees"] == Decimal("3.0")


def test_fees_all_time_start_falls_back_on_portfolio_error():
    ref = _ref()

    class _BrokenPortfolioHL(_WindowAwareHL):
        def portfolio(self, address):
            raise ConnectionError("boom")

    hl = _BrokenPortfolioHL([])
    now_dt = datetime.fromtimestamp(_NOW, timezone.utc)
    start = _fees_all_time_start(ref, hl, now_dt)
    assert start == now_dt - timedelta(days=400)


# ── pnl_share_pct：無同基準來源 → None ───────────────────────────────────

@pytest.mark.parametrize("period", ["this_month", "last_month", "all"])
def test_pnl_share_pct_is_null_for_every_period(period):
    ref = _ref()
    hl = _WindowAwareHL([
        _fill(datetime(2026, 8, 5, tzinfo=timezone.utc), builder_fee="1.0"),
    ])
    result = _dashboard_fees_period(ref, hl, _NOW, period, cache={})
    assert result["summary"]["pnl_share_pct"] is None


# ── 端點層：認證、422、happy path（TestClient） ──────────────────────────

def _write_manifest(tmp_path, followers):
    import json
    p = tmp_path / "followers.json"
    p.write_text(json.dumps({"followers": followers}))
    return str(p)


def _follower(wallet, **over):
    acct = "f" + wallet.address[2:].lower()
    f = {"account_id": acct, "user_address": wallet.address.lower(),
         "builder_address": BUILDER, "network": "testnet", "label": "t"}
    f.update(over)
    return f


def _make_dash_app(tmp_path, *, followers=None):
    cfg = make_cfg(tmp_path, followers_path=_write_manifest(tmp_path, followers or []))
    app, cfg, store, keysvc, hl = make_app(tmp_path, cfg=cfg)
    return app, cfg, hl


def test_requires_session(tmp_path):
    app, _cfg, _hl = _make_dash_app(tmp_path)
    assert _client(app).get("/api/me/fees").status_code == 401


def test_invalid_period_422(tmp_path):
    wallet = Account.create()
    app, cfg, hl = _make_dash_app(tmp_path, followers=[_follower(wallet)])
    client = _client(app)
    login(client, wallet=wallet)
    r = client.get("/api/me/fees", params={"period": "this_week"})
    assert r.status_code == 422


def test_happy_path_this_month(tmp_path):
    wallet = Account.create()
    app, cfg, hl = _make_dash_app(tmp_path, followers=[_follower(wallet)])
    addr = wallet.address.lower()
    hl.window_aware = True
    hl.fills[addr] = [_fill(datetime(2026, 8, 5, tzinfo=timezone.utc), builder_fee="1.0")]

    client = _client(app)
    login(client, wallet=wallet)
    r = client.get("/api/me/fees", params={"period": "this_month"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {"summary", "daily"}
    assert set(body["summary"].keys()) == {
        "builder_fees", "routed_volume", "fill_count", "pnl_share_pct"}
    assert body["summary"]["pnl_share_pct"] is None
    for row in body["daily"]:
        assert set(row.keys()) == {
            "date", "fill_count", "routed_volume", "builder_fee", "effective_rate_bps"}


def test_upstream_failure_returns_503(tmp_path):
    wallet = Account.create()
    app, cfg, hl = _make_dash_app(tmp_path, followers=[_follower(wallet)])
    addr = wallet.address.lower()
    hl.fills_error[addr] = ConnectionError("boom")

    client = _client(app)
    login(client, wallet=wallet)
    r = client.get("/api/me/fees", params={"period": "this_month"})
    assert r.status_code == 503
