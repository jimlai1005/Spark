"""tests/test_publicapi_hl.py
HLGateway：唯讀查詢的解析與 transient 重試（用**真實 httpx 例外**驗轉譯——
內建 ConnectionError 驗重試是假信心，opus 審 I1）；結構性斷言後端無 /exchange 寫入面。
monkeypatch httpx.post，不觸網。"""
from decimal import Decimal

import httpx

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
