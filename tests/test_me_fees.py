"""tests/test_me_fees.py — M3 round3 Task 2＋2b：費用明細逐日聚合＋期間切換＋
佔已實現淨 PnL 百分比＋午夜邊界修正。

盯住 plan Task 2／2b 的四件事：
(1) `daily_bars`／`daily` 每列擴充為 `{date, fill_count, routed_volume, builder_fee,
    effective_rate_bps}`；無成交日不產生列，`builder_fee=0` 但有成交的日子照實列出
    （$0.00 與「無成交」語意分開，R2·B）。
(2) `GET /api/me/fees?period=this_month|last_month|all`：同一個 `collect_follower_summary`
    資料源（不另拼第二來源）。
(3) `pnl_share_pct`＝builder_fees ÷（Σ closedPnl − Σ fee），同一批 fills 同源同基準
    （Task 2b／D12）；沒有 closedPnl 資料或分母 ≤0 → `None`。數值錨例：
    fee(builder)=2.00、closedPnl 合計=10.00、fee 合計=3.50 → 淨 6.50 → 30.77%。
(4) 逐日聚合用半開區間 `[day, day+1)`：成交恰在 UTC 午夜整只入當日、不重複計入前一天
    （Task 2b／D13，`collect_follower_summary(..., end_exclusive=True)`）。
(5) `/api/me/dashboard` 既有欄位（`fees_month.fill_count` 等）不被破壞。

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
from spark.filet.aggregate import collect_follower_summary
from spark.filet.followers import FollowerRef
from spark.publicapi.app import (
    _dashboard_fees_month,
    _dashboard_fees_period,
    _fee_daily_bars,
    _fees_all_time_start,
    _month_bounds,
    _pnl_share_pct,
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


def _fill(day: datetime, *, sz="1", px="100", builder_fee="0.5", fee="0.01",
         closed_pnl=None) -> UserFill:
    return UserFill(time=day, coin="ETH", px=Decimal(px), sz=Decimal(sz),
                    side="B", crossed=True, oid=1, fee=Decimal(fee),
                    builder_fee=Decimal(builder_fee),
                    closed_pnl=Decimal(closed_pnl) if closed_pnl is not None else None)


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

    def get_user_fills_paged(self, address, start, end, *, max_pages=None):
        """R-A（2026-08-30，C2/C3 修法）：`_dashboard_fees_month`／
        `_dashboard_fees_period` 現在走這個分頁介面（一次抓好整個期間，
        不再逐日呼叫）。這個 fixture 不需要模擬真的分頁/截斷（那條路徑在
        `tests/test_publicapi_hl.py` 對 `HLGateway` 直測），委派給既有
        `get_user_fills` 即可，回傳 `truncated=False`。"""
        return self.get_user_fills(address, start, end), False

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
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)

    # R-A（2026-08-30 C2/C3 修法）：`_fee_daily_bars` 改吃已抓好的 fills 清單，
    # 不再自己打 HL——呼叫端（`_dashboard_fees_month`/`_dashboard_fees_period`）
    # 負責一次抓好整期間；本測試直接餵同一份 fills，驗證聚合邏輯本身。
    bars = _fee_daily_bars(ref, fills, start, end)

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
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = datetime(2026, 8, 3, tzinfo=timezone.utc)
    assert _fee_daily_bars(ref, [], start, end) == []


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


# ── pnl_share_pct：Task 2b 同源已實現淨 PnL 分母 ─────────────────────────

def test_pnl_share_pct_numeric_anchor():
    """plan Task 2b 錨例：fee(builder)=2.00、closedPnl 合計=10.00、
    fee 合計=3.50 → 淨 6.50 → 2.00/6.50*100 ≈ 30.77%（quantize 0.01 HALF_UP）。"""
    got = _pnl_share_pct(Decimal("2.00"), Decimal("10.00"), Decimal("3.50"))
    assert got == Decimal("30.77")


def test_pnl_share_pct_null_when_no_closed_pnl_data():
    assert _pnl_share_pct(Decimal("1.0"), None, Decimal("0")) is None


@pytest.mark.parametrize("realized_pnl,total_fee", [
    (Decimal("0"), Decimal("0")),      # 淨 = 0
    (Decimal("1.0"), Decimal("2.0")),  # 淨 = -1.0
])
def test_pnl_share_pct_null_when_net_realized_not_positive(realized_pnl, total_fee):
    assert _pnl_share_pct(Decimal("1.0"), realized_pnl, total_fee) is None


@pytest.mark.parametrize("period", ["this_month", "last_month", "all"])
def test_pnl_share_pct_null_end_to_end_when_fills_carry_no_closed_pnl(period):
    """`_fill` 預設不帶 closed_pnl（None）——同一批 fills 完全沒有 closedPnl
    資料時，`pnl_share_pct` 仍是 `None`（不是永遠 null，是這批資料真的沒有）。"""
    ref = _ref()
    hl = _WindowAwareHL([
        _fill(datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc), builder_fee="1.0"),
    ])
    result = _dashboard_fees_period(ref, hl, _NOW, period, cache={})
    assert result["summary"]["pnl_share_pct"] is None


def test_pnl_share_pct_end_to_end_this_month():
    """端到端：兩筆本月成交合計 builder_fee=2.00／fee=3.50／closedPnl=10.00，
    與純函式錨例同一組數字，經 `_dashboard_fees_period` 算出同一個 30.77%。"""
    ref = _ref()
    hl = _WindowAwareHL([
        _fill(datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc),
             builder_fee="1.00", fee="2.00", closed_pnl="6.00"),
        _fill(datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc),
             builder_fee="1.00", fee="1.50", closed_pnl="4.00"),
    ])
    result = _dashboard_fees_period(ref, hl, _NOW, "this_month", cache={})
    assert result["summary"]["builder_fees"] == Decimal("2.00")
    assert result["summary"]["pnl_share_pct"] == Decimal("30.77")


# ── R-A（2026-08-30 opus 審查 C2/C3）：分頁截斷旗標＋合計恆等於逐日加總 ────

class _RawPagePost:
    """`HLGateway` 的假 `post_fn`：依序吐出整頁原始 fills（未經 UserFill 轉換），
    用來驅動真實的 `get_user_fills_paged` 分頁邏輯（不是 `_WindowAwareHL` 那種
    已經算好結果的替身）。"""

    def __init__(self, pages: list[list[dict]]):
        self._pages = list(pages)
        self.calls: list[dict] = []

    def __call__(self, url, body):
        self.calls.append(body)
        return self._pages.pop(0)


def test_dashboard_fees_period_truncated_flag_and_daily_sum_matches_summary(monkeypatch):
    """驗收條件 3：頁上限觸頂 → `truncated=True`，且合計（`summary.fill_count`）
    仍恰好等於逐日 bar 加總——兩者是同一份已截斷資料的不同切片（同源同基準），
    截斷不會讓它們兜不起來。驗收條件 4：`FILET_FILLS_MAX_PAGES=1`＋單頁 2000 筆
    整批落在同一天，只打**一次**上游（call 數不是被天數放大）。"""
    from spark.publicapi.hl import HLGateway, _to_ms_utc

    monkeypatch.setenv("FILET_FILLS_MAX_PAGES", "1")
    ref = _ref()
    base_ms = _to_ms_utc(datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc))
    raw_page = [{"time": base_ms + i, "coin": "ETH", "px": "100", "sz": "1",
                "side": "B", "crossed": True, "oid": i, "fee": "0.01",
                "builderFee": "0.02", "tid": i} for i in range(2000)]
    post = _RawPagePost([raw_page])
    hl = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)

    result = _dashboard_fees_period(ref, hl, _NOW, "this_month", cache={})

    assert result["summary"]["truncated"] is True
    assert result["summary"]["fill_count"] == 2000
    assert sum(b["fill_count"] for b in result["daily"]) == result["summary"]["fill_count"]
    assert len(post.calls) == 1   # 不是 ∝ 天數；2000 筆在單頁上限內只打一次


def test_dashboard_fees_period_not_truncated_reports_false():
    ref = _ref()
    hl = _WindowAwareHL([_fill(datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc))])
    result = _dashboard_fees_period(ref, hl, _NOW, "this_month", cache={})
    assert result["summary"]["truncated"] is False


# ── 午夜邊界（Task 2b／D13）：半開區間 [day, day+1) ───────────────────────

def test_fee_daily_bars_midnight_fill_counted_once_in_next_day():
    """成交恰好落在 2026-08-06 00:00:00 UTC（前一天 8/5 的收盤邊界＝次日
    8/6 的起點）：半開區間下只入 8/6，不會同時也被 8/5 那根 bar 算一次。"""
    ref = _ref()
    midnight_fill = _fill(datetime(2026, 8, 6, 0, 0, tzinfo=timezone.utc),
                          builder_fee="1.0")
    start = datetime(2026, 8, 5, tzinfo=timezone.utc)
    end = datetime(2026, 8, 7, tzinfo=timezone.utc)

    bars = _fee_daily_bars(ref, [midnight_fill], start, end)

    assert [b["date"] for b in bars] == ["2026-08-06"]
    assert bars[0]["fill_count"] == 1
    total_fill_count = sum(b["fill_count"] for b in bars)
    assert total_fill_count == 1  # 不是 2（沒有被前一天重複記一次）


def test_collect_follower_summary_end_exclusive_drops_boundary_fill():
    """`end_exclusive=True`：恰好等於 `end` 的成交被過濾掉（半開區間上界）；
    預設（`end_exclusive=False`，既有呼叫端如 `/api/ops/revenue` 的行為）
    仍然兩端皆含，不受影響——驗證新參數不動舊路徑。"""
    ref = _ref()
    boundary = datetime(2026, 8, 6, 0, 0, tzinfo=timezone.utc)
    hl = _WindowAwareHL([_fill(boundary, builder_fee="1.0")])

    inclusive = collect_follower_summary(ref, hl, datetime(2026, 8, 5, tzinfo=timezone.utc),
                                         boundary)
    assert inclusive.fills == 1  # 舊行為：兩端皆含

    exclusive = collect_follower_summary(ref, hl, datetime(2026, 8, 5, tzinfo=timezone.utc),
                                         boundary, end_exclusive=True)
    assert exclusive.fills == 0  # 新行為：上界排他


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
    # R-A（2026-08-30 C2/C3 修法）：新增 `truncated` 欄位——分頁抓取達上限仍
    # 滿頁時標示 True，本次固定資料量遠低於上限，預期 False。
    assert set(body["summary"].keys()) == {
        "builder_fees", "routed_volume", "fill_count", "pnl_share_pct", "truncated"}
    assert body["summary"]["truncated"] is False
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
