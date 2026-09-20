"""tests/test_hl_fills_page.py — Task 3.2：HLGateway.get_fills_page 單頁成交查詢（scheduler 用）。
spec §8.2、plan Task 3.2：請求體與 get_fills_raw_paged 第一頁完全相同，
回原始 list、不翻頁、不裁切欄位。
"""
import pytest

from spark.publicapi.hl import HLGateway
from spark.publicapi.hl_budget import WeightLimiter


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _gw(post_fn, clock):
    """建立帶限流器的 gateway（一致於 test_hl_gateway_budget.py 的模式）。"""
    lim = WeightLimiter(global_cap=900, scope_caps={"explore": 300},
                        now_fn=clock.now, sleep_fn=clock.sleep, rng=lambda: 0.0)
    return HLGateway("https://x", post_fn=post_fn, sleep_fn=clock.sleep, limiter=lim), lim


def test_get_fills_page_body_matches_paged_first_page():
    """body 形狀與 get_fills_raw_paged 第一次送出的 body 完全相等（忽略順序）。"""
    c = Clock()
    recorded = {"first_page_body": None}

    def post(url, body):
        if recorded["first_page_body"] is None:
            recorded["first_page_body"] = body.copy()
        # 返回一頁的 fill 列表（已滿 2000 筆，會觸發分頁）
        return [
            {"tid": str(i), "time": 1000 + i, "coin": "BTC", "px": "50000",
             "sz": "0.1", "side": "A", "oid": str(i)}
            for i in range(2000)
        ]

    gw, _ = _gw(post, c)

    # 調用 get_fills_raw_paged，會產生第一次的請求
    from datetime import datetime, timezone
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 1, 2, tzinfo=timezone.utc)
    _ = gw.get_fills_raw_paged("0xabc", start, end, max_pages=1)

    # 清空記錄，調用 get_fills_page（應該產生相同的請求）
    recorded["first_page_body"] = None
    result = gw.get_fills_page("0xabc", 1735689600000, 1735776000000)

    # 驗證 body 形狀相等
    assert recorded["first_page_body"] is not None
    expected_body = {
        "type": "userFillsByTime",
        "user": "0xabc",
        "startTime": 1735689600000,
        "endTime": 1735776000000,
    }
    assert recorded["first_page_body"] == expected_body
    assert isinstance(result, list)
    assert len(result) == 2000


def test_get_fills_page_returns_raw_list():
    """回傳就是 post 回的 list 物件內容，不裁切。"""
    c = Clock()
    raw_fills = [
        {"tid": "t1", "time": 1000, "coin": "BTC", "px": "50000", "sz": "0.1", "side": "A"},
        {"tid": "t2", "time": 2000, "coin": "ETH", "px": "3000", "sz": "1.0", "side": "B"},
    ]

    def post(url, body):
        return raw_fills

    gw, _ = _gw(post, c)
    result = gw.get_fills_page("0xabc", 1000, 2000)

    assert result is raw_fills
    assert len(result) == 2


def test_get_fills_page_raises_on_non_list():
    """post 回 dict（或非 list）→ ValueError。"""
    c = Clock()

    def post(url, body):
        return {"error": "something"}

    gw, _ = _gw(post, c)

    with pytest.raises(ValueError, match="userFillsByTime 回應非 list"):
        gw.get_fills_page("0xabc", 1000, 2000)


def test_get_fills_page_budgets_correctly():
    """經 WeightLimiter 預留 120，回 50 筆後結算為 23（20 + ceil(50/20)）。"""
    c = Clock()

    def post(url, body):
        return [
            {"tid": str(i), "time": 1000 + i, "coin": "BTC"}
            for i in range(50)
        ]

    gw, lim = _gw(post, c)
    result = gw.get_fills_page("0xabc", 1000, 2000)

    assert result is not None

    # 驗證回傳結果
    assert len(result) == 50

    # 驗證限流器的狀態：預留 120、結算為 23、退款 97
    snapshot = lim.snapshot()
    assert snapshot["used"]["interactive"] == 23
    assert snapshot["counters"]["reserved_weight"] == 120
    assert snapshot["counters"]["refunded_weight"] == 97


def test_get_fills_page_scope_interactive_by_default():
    """預設 scope 是 interactive（20 +ceil(n/20) 而非 120）。"""
    c = Clock()

    def post(url, body):
        return [{"tid": str(i), "time": 1000 + i} for i in range(10)]

    gw, lim = _gw(post, c)
    gw.get_fills_page("0xabc", 1000, 2000)

    snapshot = lim.snapshot()
    assert snapshot["used"] == {"interactive": 20 + 1}  # 20 + ceil(10/20)


def test_get_fills_page_with_scoped_gateway_uses_explore_scope():
    """透過 scoped("explore") 的 gateway → explore scope、無等待。"""
    c = Clock()

    def post(url, body):
        return [{"tid": str(i), "time": 1000 + i} for i in range(50)]

    gw, lim = _gw(post, c)
    gw_explore = gw.scoped("explore", wait_s=0.0)
    gw_explore.get_fills_page("0xabc", 1000, 2000)

    snapshot = lim.snapshot()
    assert snapshot["used"] == {"explore": 23}
