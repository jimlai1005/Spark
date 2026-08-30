"""tests/test_dashboard_fees_cache.py
Opus 審查 Warning 1（2026-08-29）：`_dashboard_fees_month` 的 `daily_bars` 逐日
重呼 `collect_follower_summary`——一次 dashboard 請求打約「月內天數＋1」次
`get_user_fills`。本檔測試 per-account in-process 快取（TTL 300s）：
5 分鐘內同一帳號的重複請求不再重新打上游；跨帳號不共用；過期後重新計算；
失敗不快取（不把一次暫時性故障釘住 300 秒）。

⭐ R-A（2026-08-30 opus 審查 C2/C3）：Warning 1 的「快取」緩解不夠——TTL 過期後
單一 request 仍是逐日打。`_dashboard_fees_month` 已改為一次分頁抓好整個期間、
本地聚合（見 `spark.publicapi.app._fetch_period_fills`），單次呼叫只打**一次**
`get_user_fills_paged`（月總量與 daily_bars 現在吃同一份已抓資料），
`_CountingHL` 因此改實作 `get_user_fills_paged` 並計數它，第一次呼叫的
call 數斷言從「>1（月總量＋逐日）」改為「==1」。

全離線（tests/conftest.py 的 autouse socket-ban；本檔直接呼叫純 Python 函式，
不經 TestClient/網路）。
"""
from datetime import datetime, timezone
from decimal import Decimal

from spark.exchange.base import UserFill
from spark.filet.followers import FollowerRef
from spark.publicapi.app import _FEES_MONTH_CACHE_TTL_S, _dashboard_fees_month


class _CountingHL:
    """包一層呼叫計數，不改動既有 FakeHL 的既有介面/行為。"""

    def __init__(self, fills=None, error=None):
        self._fills = fills or []
        self._error = error
        self.calls = 0

    def get_user_fills(self, address, start, end):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return list(self._fills)

    def get_user_fills_paged(self, address, start, end, *, max_pages=None):
        # R-A（2026-08-30 C2/C3）：`_dashboard_fees_month` 現在打這個介面，
        # 一次 request 只呼叫一次（不再逐日）——委派給 `get_user_fills`
        # 才能沿用既有的 calls 計數與 error 注入。
        return self.get_user_fills(address, start, end), False


def _ref(account_id="fabc") -> FollowerRef:
    return FollowerRef(account_id=account_id, user_address="0x" + "ab" * 20,
                       builder_address="0x" + "22" * 20, network="mainnet")


def _fill(day: datetime) -> UserFill:
    return UserFill(time=day, coin="ETH", px=Decimal("100"), sz=Decimal("1"),
                    side="B", crossed=True, oid=1, fee=Decimal("0.01"),
                    builder_fee=Decimal("0.02"))


_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc).timestamp()


def test_second_call_within_ttl_does_not_refetch(tmp_path):
    hl = _CountingHL(fills=[_fill(datetime(2026, 8, 1, tzinfo=timezone.utc))])
    cache: dict = {}
    ref = _ref()

    r1 = _dashboard_fees_month(ref, hl, _NOW, cache=cache)
    calls_after_first = hl.calls
    # R-A（2026-08-30 C2/C3 修法）：月總量與逐日 daily_bars 現在吃同一次
    # `get_user_fills_paged` 抓到的資料，只打一次上游（不再是「月總量 + 逐日」
    # 的 N+1 次）。
    assert calls_after_first == 1

    r2 = _dashboard_fees_month(ref, hl, _NOW + 1, cache=cache)  # 1 秒後，仍在 TTL 內
    assert hl.calls == calls_after_first  # 完全沒有再打上游
    assert r2 == r1


def test_cache_expires_after_ttl(tmp_path):
    hl = _CountingHL(fills=[])
    cache: dict = {}
    ref = _ref()

    _dashboard_fees_month(ref, hl, _NOW, cache=cache)
    calls_after_first = hl.calls

    _dashboard_fees_month(ref, hl, _NOW + _FEES_MONTH_CACHE_TTL_S + 1, cache=cache)
    assert hl.calls > calls_after_first  # TTL 過期，重新計算


def test_cache_keyed_by_account_id_not_shared(tmp_path):
    hl = _CountingHL(fills=[])
    cache: dict = {}

    _dashboard_fees_month(_ref("fabc"), hl, _NOW, cache=cache)
    calls_after_first = hl.calls
    _dashboard_fees_month(_ref("fdef"), hl, _NOW, cache=cache)
    assert hl.calls > calls_after_first  # 不同帳號各自一份，不互相冒充


# ── W4（2026-08-30 opus 審查）：`_fees_period_cache` 256 上限＋近似 LRU 淘汰 ──

def test_fees_period_cache_evicts_oldest_when_over_max():
    """插入 257 個不同帳號（各自唯一 key）後，快取仍守住 256 上限，且被淘汰的
    是最舊寫入（`now_s` 最小）那一筆——不是隨機或最新一筆。"""
    from spark.publicapi.app import _FEES_PERIOD_CACHE_MAX, _dashboard_fees_period

    cache: dict = {}
    hl = _CountingHL(fills=[])
    base_now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc).timestamp()

    def ref(i):
        return FollowerRef(account_id=f"acct{i}", user_address="0x" + "ab" * 20,
                           builder_address="0x" + "22" * 20, network="mainnet")

    for i in range(_FEES_PERIOD_CACHE_MAX + 1):
        _dashboard_fees_period(ref(i), hl, base_now + i, "this_month", cache=cache)

    assert len(cache) == _FEES_PERIOD_CACHE_MAX
    assert ("acct0", "this_month") not in cache          # 最舊一筆被淘汰
    assert ("acct1", "this_month") in cache               # 第二舊一筆還在
    assert (f"acct{_FEES_PERIOD_CACHE_MAX}", "this_month") in cache  # 最新一筆在


def test_fees_period_cache_hit_does_not_trigger_eviction():
    """快取命中（同一個 key 在 TTL 內重打）不算「新增一筆」，不該觸發淘汰邏輯、
    也不該讓 dict 大小意外增減。"""
    from spark.publicapi.app import _dashboard_fees_period

    cache: dict = {}
    hl = _CountingHL(fills=[])
    ref = FollowerRef(account_id="acct-hit", user_address="0x" + "ab" * 20,
                      builder_address="0x" + "22" * 20, network="mainnet")
    now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc).timestamp()

    _dashboard_fees_period(ref, hl, now, "this_month", cache=cache)
    assert len(cache) == 1
    _dashboard_fees_period(ref, hl, now + 1, "this_month", cache=cache)  # TTL 內命中
    assert len(cache) == 1
    assert hl.calls == 1  # 第二次是快取命中，沒有再打上游


def test_failure_is_not_cached(tmp_path):
    hl = _CountingHL(error=RuntimeError("fills 查詢失敗"))
    cache: dict = {}
    ref = _ref()

    for _ in range(2):
        try:
            _dashboard_fees_month(ref, hl, _NOW, cache=cache)
        except RuntimeError:
            pass
    # 兩次都失敗都真的打了上游（第一次的失敗沒有被當成「已快取的結果」擋住第二次）。
    assert hl.calls == 2
    assert cache == {}
