"""tests/test_publicapi_hl.py
HLGateway：唯讀查詢的解析與 transient 重試（用**真實 httpx 例外**驗轉譯——
內建 ConnectionError 驗重試是假信心，opus 審 I1）；結構性斷言後端無 /exchange 寫入面。
monkeypatch httpx.post，不觸網。"""
from decimal import Decimal

import httpx
import pytest

from spark.publicapi.hl import HLGateway
from spark.resilience import RETRY_BASE_DELAY


class _Resp:
    """httpx.Response 替身：只要 raise_for_status/json 兩個面。"""

    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakePost:
    def __init__(self, results):
        self.results = list(results)  # 每呼叫吐一個；Exception 則 raise
        self.calls = []

    def __call__(self, url, body):
        self.calls.append((url, body))
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_gateway_reads_parse():
    post = _FakePost([
        {"marginSummary": {"accountValue": "123.45"}},
        50,
        [{"address": "0xAB" + "cd" * 19, "name": "filet"}],
    ])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    assert gw.get_account_value("0x" + "ab" * 20) == Decimal("123.45")
    assert gw.max_builder_fee("0x" + "ab" * 20, "0x" + "cd" * 20) == 50
    assert gw.agent_addresses("0x" + "ab" * 20) == [("0xAB" + "cd" * 19).lower()]
    assert all(u.endswith("/info") for u, _ in post.calls)


def test_default_post_translates_httpx_connect_error_and_retries(monkeypatch):
    """I1：httpx.ConnectError 不繼承內建 ConnectionError——經 _default_post 轉譯後
    resilience 才會分類為 transient 並重試。走真實 httpx 例外，斷言重試與 backoff。"""
    calls = {"n": 0}

    def fake_post(url, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("[Errno 61] Connection refused")
        return _Resp(7)

    monkeypatch.setattr(httpx, "post", fake_post)
    sleeps = []
    gw = HLGateway("https://x", sleep_fn=sleeps.append)  # post_fn 不注入 → 走 _default_post
    assert gw.max_builder_fee("0x" + "ab" * 20, "0x" + "cd" * 20) == 7
    assert calls["n"] == 2
    assert sleeps == [RETRY_BASE_DELAY]  # 第一段 backoff 有被呼叫


def test_default_post_translates_empty_message_read_timeout(monkeypatch):
    """I1：httpx.ReadTimeout 訊息可為空字串——marker 比對救不了，靠型別轉譯。"""
    calls = {"n": 0}

    def fake_post(url, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ReadTimeout("")
        return _Resp(0)

    monkeypatch.setattr(httpx, "post", fake_post)
    gw = HLGateway("https://x", sleep_fn=lambda s: None)
    assert gw.max_builder_fee("0x" + "ab" * 20, "0x" + "cd" * 20) == 0
    assert calls["n"] == 3


def test_gateway_read_retries_5xx_marker():
    """5xx 走 resilience 的訊息 marker 分類（httpx.HTTPStatusError 訊息含狀態碼字樣）。"""
    post = _FakePost([RuntimeError("Server error '503 Service Unavailable' for url"), 7])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    assert gw.max_builder_fee("0x" + "ab" * 20, "0x" + "cd" * 20) == 7
    assert len(post.calls) == 2


def test_gateway_has_no_exchange_write_surface():
    """紅線 5 的結構性斷言：gateway 沒有任何提交/寫入方法——前端直送 HL，
    後端連 /exchange 的呼叫路徑都不存在。"""
    gw = HLGateway("https://x", post_fn=lambda u, b: {}, sleep_fn=lambda s: None)
    assert not any("submit" in name or "exchange" in name
                   for name in dir(gw) if not name.startswith("__"))


def test_gateway_only_posts_to_info():
    post = _FakePost([{"marginSummary": {"accountValue": "1"}}, 0, []])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    gw.get_account_value("0x" + "ab" * 20)
    gw.max_builder_fee("0x" + "ab" * 20, "0x" + "cd" * 20)
    gw.agent_addresses("0x" + "ab" * 20)
    assert all(u == "https://x/info" for u, _ in post.calls)


def test_get_user_fills_parses_and_only_posts_info():
    """ops 每客戶損益的資料源：解析 userFillsByTime，欄位語意與
    HyperliquidAdapter.get_user_fills 同基準；builderFee 缺欄／null 皆視為 0。"""
    from datetime import datetime, timezone
    raw = [
        {"time": 1_700_000_000_000, "coin": "ETH", "px": "2500.5", "sz": "0.4",
         "side": "B", "crossed": True, "oid": 1, "fee": "0.12", "builderFee": "0.03"},
        {"time": 1_700_000_060_000, "coin": "BTC", "px": "60000", "sz": "0.01",
         "side": "A", "crossed": False, "oid": 2, "fee": "0.5", "builderFee": None},
        {"time": 1_700_000_120_000, "coin": "SOL", "px": "100", "sz": "1",
         "side": "B", "crossed": True, "oid": 3},
    ]
    post = _FakePost([raw])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    start = datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    end = datetime(2023, 11, 14, 23, 0, 0, tzinfo=timezone.utc)
    fills = gw.get_user_fills("0x" + "ab" * 20, start, end)
    assert [f.coin for f in fills] == ["ETH", "BTC", "SOL"]
    assert fills[0].px == Decimal("2500.5") and fills[0].sz == Decimal("0.4")
    assert fills[0].crossed is True and fills[0].builder_fee == Decimal("0.03")
    assert fills[1].builder_fee == Decimal("0")   # null → 0
    assert fills[2].builder_fee == Decimal("0")   # 缺欄 → 0
    url, body = post.calls[0]
    assert url == "https://x/info"                # 唯讀：只 POST /info
    assert body["type"] == "userFillsByTime" and body["user"] == "0x" + "ab" * 20
    assert body["startTime"] == 1_700_000_000_000  # 整數 ms，無 float 中間值
    assert body["endTime"] == 1_700_002_800_000


# ── get_user_fills_paged（R-A 2026-08-30 opus 審查 C2/C3 修法）─────────────

def _raw_fill(t_ms: int, tid: int, **over) -> dict:
    d = {"time": t_ms, "coin": "ETH", "px": "100", "sz": "1", "side": "B",
        "crossed": True, "oid": tid, "fee": "0.01", "builderFee": "0.02",
        "tid": tid}
    d.update(over)
    return d


def test_get_user_fills_paged_single_page_under_limit_not_truncated():
    from datetime import datetime, timezone
    post = _FakePost([[_raw_fill(1_700_000_000_000, tid=1)]])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    fills, truncated = gw.get_user_fills_paged(
        "0x" + "ab" * 20,
        datetime(2023, 11, 14, tzinfo=timezone.utc),
        datetime(2023, 11, 15, tzinfo=timezone.utc))
    assert truncated is False
    assert len(fills) == 1
    assert len(post.calls) == 1


def test_get_user_fills_paged_advances_cursor_and_dedupes_boundary_fill():
    """滿頁（2000 筆）→ 游標前進到最後一筆的時間 → 下一頁重疊那一毫秒的同一筆
    （同 `tid`，模擬 `userFillsByTime` 兩端點皆含）不重複計，新筆正常併入。"""
    from datetime import datetime, timezone
    from spark.exchange.base import USER_FILLS_PAGE_LIMIT
    from spark.publicapi.hl import _to_ms_utc
    base_ms = _to_ms_utc(datetime(2026, 8, 1, tzinfo=timezone.utc))
    page1 = [_raw_fill(base_ms + i, tid=i) for i in range(USER_FILLS_PAGE_LIMIT)]
    page2 = [
        _raw_fill(base_ms + USER_FILLS_PAGE_LIMIT - 1, tid=USER_FILLS_PAGE_LIMIT - 1),
        _raw_fill(base_ms + USER_FILLS_PAGE_LIMIT, tid=USER_FILLS_PAGE_LIMIT),
        _raw_fill(base_ms + USER_FILLS_PAGE_LIMIT + 1, tid=USER_FILLS_PAGE_LIMIT + 1),
    ]
    post = _FakePost([page1, page2])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    fills, truncated = gw.get_user_fills_paged(
        "0x" + "ab" * 20,
        datetime(2026, 8, 1, tzinfo=timezone.utc),
        datetime(2026, 8, 2, tzinfo=timezone.utc))
    assert truncated is False
    assert len(fills) == USER_FILLS_PAGE_LIMIT + 2   # 2000 + 2 新筆，重疊那筆不重複計
    assert len(post.calls) == 2
    # 第二頁的 startTime＝第一頁最後一筆的時間（游標前進，兩端點皆含）
    assert post.calls[1][1]["startTime"] == base_ms + USER_FILLS_PAGE_LIMIT - 1
    assert [f.time for f in fills] == sorted(f.time for f in fills)  # 依時間升冪


def test_get_user_fills_paged_truncates_at_max_pages():
    from datetime import datetime, timezone
    from spark.exchange.base import USER_FILLS_PAGE_LIMIT
    from spark.publicapi.hl import _to_ms_utc
    base_ms = _to_ms_utc(datetime(2026, 8, 1, tzinfo=timezone.utc))
    page1 = [_raw_fill(base_ms + i, tid=i) for i in range(USER_FILLS_PAGE_LIMIT)]
    post = _FakePost([page1])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    fills, truncated = gw.get_user_fills_paged(
        "0x" + "ab" * 20,
        datetime(2026, 8, 1, tzinfo=timezone.utc),
        datetime(2026, 9, 1, tzinfo=timezone.utc),
        max_pages=1)
    assert truncated is True
    assert len(fills) == USER_FILLS_PAGE_LIMIT   # 已抓到的部分（下限值），照樣回傳
    assert len(post.calls) == 1


def test_get_user_fills_paged_call_count_bounded_by_fill_count_not_days():
    """驗收條件 4：`period=all` 場景下，呼叫次數 ≤ ceil(N/2000)+1，不再 ∝ 天數。
    N=4500 筆分三頁（滿、滿、未滿）；查詢視窗跨兩年多，呼叫數仍只有 3 次。"""
    import math
    from datetime import datetime, timezone
    from spark.exchange.base import USER_FILLS_PAGE_LIMIT
    from spark.publicapi.hl import _to_ms_utc
    base_ms = _to_ms_utc(datetime(2024, 1, 1, tzinfo=timezone.utc))
    page1 = [_raw_fill(base_ms + i, tid=i) for i in range(2000)]
    page2 = [_raw_fill(base_ms + 2000 + i, tid=2000 + i) for i in range(2000)]
    page3 = [_raw_fill(base_ms + 4000 + i, tid=4000 + i) for i in range(500)]
    post = _FakePost([page1, page2, page3])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    fills, truncated = gw.get_user_fills_paged(
        "0x" + "ab" * 20,
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2026, 8, 30, tzinfo=timezone.utc))  # 兩年多的窗口
    n = len(fills)
    assert n == 4500 and truncated is False
    assert len(post.calls) == 3
    assert len(post.calls) <= math.ceil(n / USER_FILLS_PAGE_LIMIT) + 1


def test_get_user_fills_paged_respects_env_max_pages(monkeypatch):
    from datetime import datetime, timezone
    from spark.exchange.base import USER_FILLS_PAGE_LIMIT
    from spark.publicapi.hl import _to_ms_utc
    monkeypatch.setenv("FILET_FILLS_MAX_PAGES", "1")
    base_ms = _to_ms_utc(datetime(2026, 8, 1, tzinfo=timezone.utc))
    page1 = [_raw_fill(base_ms + i, tid=i) for i in range(USER_FILLS_PAGE_LIMIT)]
    post = _FakePost([page1])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    fills, truncated = gw.get_user_fills_paged(
        "0x" + "ab" * 20,
        datetime(2026, 8, 1, tzinfo=timezone.utc),
        datetime(2026, 9, 1, tzinfo=timezone.utc))   # 未顯式傳 max_pages → 讀 env
    assert truncated is True
    assert len(post.calls) == 1


# ── get_fills_detail_paged（I-18：/api/me/fills 固定 30 天窗＋游標分頁）────

def test_get_fills_detail_paged_advances_cursor_across_2000_boundary():
    """I-18：與 `get_user_fills_paged` 共用同一份游標迴圈（`_paged_fills_raw`），
    這裡驗證 dict 展示形狀（含 `hash`，`UserFill` 沒有這個欄位）在跨 2000 筆
    邊界時同樣正確分頁抓滿、重疊筆不重複計、依時間升冪排列。"""
    from datetime import datetime, timezone
    from spark.exchange.base import USER_FILLS_PAGE_LIMIT
    from spark.publicapi.hl import _to_ms_utc
    base_ms = _to_ms_utc(datetime(2026, 8, 1, tzinfo=timezone.utc))
    page1 = [_raw_fill(base_ms + i, tid=i, hash=f"0x{i:x}")
            for i in range(USER_FILLS_PAGE_LIMIT)]
    page2 = [
        _raw_fill(base_ms + USER_FILLS_PAGE_LIMIT - 1, tid=USER_FILLS_PAGE_LIMIT - 1,
                 hash=f"0x{USER_FILLS_PAGE_LIMIT - 1:x}"),
        _raw_fill(base_ms + USER_FILLS_PAGE_LIMIT, tid=USER_FILLS_PAGE_LIMIT,
                 hash=f"0x{USER_FILLS_PAGE_LIMIT:x}"),
    ]
    post = _FakePost([page1, page2])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    fills, truncated = gw.get_fills_detail_paged(
        "0x" + "ab" * 20,
        datetime(2026, 8, 1, tzinfo=timezone.utc),
        datetime(2026, 8, 2, tzinfo=timezone.utc))
    assert truncated is False
    assert len(fills) == USER_FILLS_PAGE_LIMIT + 1   # 重疊那筆（同 tid）不重複計
    assert len(post.calls) == 2
    assert fills[0]["hash"] == "0x0"                 # 展示形狀保留 hash
    assert [f["time"] for f in fills] == sorted(f["time"] for f in fills)  # 依時間升冪


def test_get_fills_detail_paged_truncates_at_max_pages():
    from datetime import datetime, timezone
    from spark.exchange.base import USER_FILLS_PAGE_LIMIT
    from spark.publicapi.hl import _to_ms_utc
    base_ms = _to_ms_utc(datetime(2026, 8, 1, tzinfo=timezone.utc))
    page1 = [_raw_fill(base_ms + i, tid=i) for i in range(USER_FILLS_PAGE_LIMIT)]
    post = _FakePost([page1])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    fills, truncated = gw.get_fills_detail_paged(
        "0x" + "ab" * 20,
        datetime(2026, 8, 1, tzinfo=timezone.utc),
        datetime(2026, 9, 1, tzinfo=timezone.utc),
        max_pages=1)
    assert truncated is True
    assert len(fills) == USER_FILLS_PAGE_LIMIT


def test_get_fills_raw_paged_preserves_raw_hl_fields():
    """2026-09-05（D5 修正，見 plan trader-pnl-metrics）：`trader_stats.fills_stats`
    需要 `dir`／`oid`／`startPosition`／`closedPnl` 原始欄位；`get_fills_detail_paged`
    經 `_fill_detail_dict` 裁切會丟掉這些欄位，`get_fills_raw_paged` 不得裁切。"""
    from datetime import datetime, timezone
    raw = [{"time": 1, "coin": "BTC", "side": "B", "px": "100", "sz": "1", "fee": "0.1",
            "closedPnl": "0", "hash": "0xh", "oid": 11, "tid": 1, "dir": "Open Long",
            "startPosition": "0.0"},
           {"time": 2, "coin": "BTC", "side": "A", "px": "110", "sz": "1", "fee": "0.1",
            "closedPnl": "10", "hash": "0xh2", "oid": 12, "tid": 2, "dir": "Close Long",
            "startPosition": "1.0"}]
    post = _FakePost([raw])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    fills, truncated = gw.get_fills_raw_paged(
        "0x" + "ab" * 20,
        datetime(2026, 8, 1, tzinfo=timezone.utc),
        datetime(2026, 8, 2, tzinfo=timezone.utc),
        max_pages=3)
    assert truncated is False
    assert [f["dir"] for f in fills] == ["Open Long", "Close Long"]
    assert fills[0]["oid"] == 11 and fills[1]["startPosition"] == "1.0"
    assert "closedPnl" in fills[1] and "closed_pnl" not in fills[1]


def test_to_ms_utc_treats_naive_as_utc():
    from datetime import datetime, timedelta, timezone

    from spark.publicapi.hl import _to_ms_utc
    naive = datetime(2023, 11, 14, 22, 13, 20)
    aware = naive.replace(tzinfo=timezone.utc)
    assert _to_ms_utc(naive) == _to_ms_utc(aware) == 1_700_000_000_000
    # aware 非 UTC 時區先轉 UTC 再取 epoch（不以本機時區誤解讀）
    other = aware.astimezone(timezone(timedelta(hours=8)))
    assert _to_ms_utc(other) == 1_700_000_000_000


def test_clearinghouse_state_returns_full_state():
    """M3 watchlist 快照用：回完整 state（get_account_value 只取其中一欄，行為不變）。"""
    state = {"marginSummary": {"accountValue": "123.5", "totalMarginUsed": "10",
                               "totalNtlPos": "50"},
             "withdrawable": "100", "assetPositions": []}

    def fake_post(url, body):
        assert body == {"type": "clearinghouseState", "user": "0xabc"}
        return state

    from spark.publicapi.hl import HLGateway
    gw = HLGateway("https://api.example", post_fn=fake_post, sleep_fn=lambda s: None)
    assert gw.clearinghouse_state("0xabc") == state
    from decimal import Decimal
    assert gw.get_account_value("0xabc") == Decimal("123.5")


# ── spot USDC 餘額（「錢卡在 spot」偵測的資料源）──────────────────────────

def test_spot_usdc_balance_parses_and_only_posts_info():
    """解析 spotClearinghouseState 的 balances；請求體照抄 SDK info.py:130。"""
    post = _FakePost([{"balances": [
        {"coin": "HYPE", "token": 1, "total": "12.5", "hold": "0"},
        {"coin": "USDC", "token": 0, "total": "250.5", "hold": "10"},
    ]}])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    assert gw.spot_usdc_balance("0x" + "ab" * 20) == Decimal("250.5")
    url, body = post.calls[0]
    assert url == "https://x/info"
    assert body == {"type": "spotClearinghouseState", "user": "0x" + "ab" * 20}


def test_spot_usdc_balance_uses_total_not_total_minus_hold():
    """⭐ 取 `total`：`hold` 是 spot 掛單佔用，那些錢一樣需要客戶自己處理，
    一樣屬於「卡在 spot」。扣掉 hold 會讓一個把 USDC 全掛在 spot 單上的客戶
    看到「你沒有卡住的錢」——那正是他最需要看到提示的時候。"""
    post = _FakePost([{"balances": [
        {"coin": "USDC", "token": 0, "total": "100", "hold": "100"}]}])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    assert gw.spot_usdc_balance("0x" + "ab" * 20) == Decimal("100")


@pytest.mark.parametrize("payload", [
    {"balances": []},                                    # 完全沒有 spot 部位
    {"balances": [{"coin": "HYPE", "total": "9"}]},       # 有幣但沒有 USDC
    {"balances": [{"coin": "USDC", "total": "abc"}]},      # 值不可解析
    {},                                                   # 缺 balances
    {"balances": None},
    "not-a-dict",
])
def test_spot_usdc_balance_degrades_to_zero_never_raises(payload):
    """形狀不符 → 0（＝不提示）。這個查詢唯一的用途是「要不要顯示一句提示」，
    猜不出來就不提示，不該讓客戶的 onboarding 狀態頁 500。"""
    gw = HLGateway("https://x", post_fn=_FakePost([payload]),
                   sleep_fn=lambda s: None)
    assert gw.spot_usdc_balance("0x" + "ab" * 20) == Decimal("0")


def test_vault_details_and_ledger_updates_post_correct_bodies():
    """vault preflight 的兩個唯讀查詢：請求體逐欄位正確、只打 /info、原樣回傳。"""
    vault = "0x" + "ab" * 20
    ledger = [{"time": 1782774120062, "hash": "0x1",
               "delta": {"type": "deposit", "usdc": "500"}}]
    post = _FakePost([{"name": "Ultron", "maxDistributable": 645277.220236}, ledger])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    assert gw.vault_details(vault) == {"name": "Ultron", "maxDistributable": 645277.220236}
    assert gw.non_funding_ledger_updates(vault, 1782774120062) == ledger
    assert post.calls == [
        ("https://x/info", {"type": "vaultDetails", "vaultAddress": vault}),
        ("https://x/info", {"type": "userNonFundingLedgerUpdates",
                            "user": vault, "startTime": 1782774120062}),
    ]


def test_vault_reads_retry_transient():
    """兩個新方法走同一條 _info 邊界（idempotent read → transient 重試）。"""
    post = _FakePost([RuntimeError("Server error '503 Service Unavailable' for url"),
                      {"name": "Ultron"}])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    assert gw.vault_details("0x" + "ab" * 20) == {"name": "Ultron"}
    assert len(post.calls) == 2


# ── 自助查帳 tab：成交明細 ＋ explorer 授權歷程（M3 round2 Task 7） ─────────

def test_get_fills_detail_parses_real_sample_shape():
    """欄位對齊 2026-08-29 curl 對 userFillsByTime 的實測樣本（見
    tests/fixtures/hl_user_fills_sample.json）：coin/px/sz/side/time/closedPnl/
    fee/hash 皆存在，金額保留字串（不在 gateway 層轉 float）。"""
    raw = [{"coin": "ETH", "px": "2074.9", "sz": "41.4803", "side": "B",
           "time": 1774926504932, "closedPnl": "217.356772",
           "hash": "0x317e78012add56b532f80438128ac402033900e6c5d07587d5472353e9d1309f",
           "oid": 365940279977, "crossed": False, "fee": "-2.582024"}]
    post = _FakePost([raw])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    from datetime import datetime, timezone
    start = datetime(2026, 7, 31, tzinfo=timezone.utc)
    end = datetime(2026, 8, 29, tzinfo=timezone.utc)
    detail = gw.get_fills_detail("0x" + "ab" * 20, start, end)
    assert detail == [{
        "time": 1774926504932, "coin": "ETH", "side": "B", "px": "2074.9",
        "sz": "41.4803", "fee": "-2.582024", "closed_pnl": "217.356772",
        "hash": "0x317e78012add56b532f80438128ac402033900e6c5d07587d5472353e9d1309f",
    }]
    url, body = post.calls[0]
    assert url == "https://x/info"  # 唯讀：只 POST /info
    assert body["type"] == "userFillsByTime"


def test_get_fills_detail_missing_fee_and_closed_pnl_default_to_zero_string():
    raw = [{"coin": "ETH", "px": "1", "sz": "1", "side": "B", "time": 1, "hash": "0x1"}]
    post = _FakePost([raw])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    from datetime import datetime, timezone
    detail = gw.get_fills_detail("0x" + "ab" * 20,
                                 datetime(2026, 1, 1, tzinfo=timezone.utc),
                                 datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert detail[0]["fee"] == "0"
    assert detail[0]["closed_pnl"] == "0"


def test_user_details_posts_to_explorer_domain_not_info():
    """domain 與 /info 不同（rpc.hyperliquid.xyz/explorer vs api.hyperliquid.xyz/info）
    ——不走 base_url 組裝，直接打絕對的 EXPLORER_URL。"""
    from spark.publicapi.hl import EXPLORER_URL
    payload = {"type": "userDetails", "txs": [
        {"time": 1787752386163, "user": "0x85ec",
         "action": {"type": "approveAgent", "agentAddress": "0xaf22"},
         "block": 1, "hash": "0xdeadbeef", "error": None},
    ]}
    post = _FakePost([payload])
    gw = HLGateway("https://api.hyperliquid.xyz", post_fn=post, sleep_fn=lambda s: None)
    result = gw.user_details("0x85ec")
    assert result == payload
    url, body = post.calls[0]
    assert url == EXPLORER_URL
    assert url != "https://api.hyperliquid.xyz/info"
    assert body == {"type": "userDetails", "user": "0x85ec"}


def test_user_details_posts_to_testnet_explorer_when_base_url_is_testnet():
    """T9：base_url 反查到 testnet 時，explorer 打 `hyperliquid-testnet` 網域，
    不再一律硬編主網 explorer（修復前的行為，見 test_e2e_noncustodial.py S13
    對此已知限制的說明）。"""
    from spark.config import API_URLS, EXPLORER_URLS
    payload = {"type": "userDetails", "txs": []}
    post = _FakePost([payload])
    gw = HLGateway(API_URLS["testnet"], post_fn=post, sleep_fn=lambda s: None)
    gw.user_details("0x85ec")
    url, body = post.calls[0]
    assert url == EXPLORER_URLS["testnet"]
    assert "hyperliquid-testnet" in url
    assert body == {"type": "userDetails", "user": "0x85ec"}


def test_user_details_unknown_base_url_falls_back_to_mainnet_explorer():
    """未知 base_url（例如測試假網域）→ 落回舊有預設（主網 explorer），
    不因為反查不到網路就整段炸掉或猜一個 testnet。"""
    from spark.publicapi.hl import EXPLORER_URL
    payload = {"type": "userDetails", "txs": []}
    post = _FakePost([payload])
    gw = HLGateway("https://not-a-real-hl-host.example", post_fn=post, sleep_fn=lambda s: None)
    gw.user_details("0x85ec")
    url, _ = post.calls[0]
    assert url == EXPLORER_URL


def test_user_details_retries_transient():
    payload = {"type": "userDetails", "txs": []}
    post = _FakePost([RuntimeError("Server error '503 Service Unavailable' for url"),
                      payload])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    assert gw.user_details("0xabc") == payload
    assert len(post.calls) == 2
