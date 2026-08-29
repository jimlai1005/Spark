"""tests/test_dashboard_fees_cache.py
Opus 審查 Warning 1：`_dashboard_fees_month` 的 `daily_bars` 逐日重呼
`collect_follower_summary`——一次 dashboard 請求打約「月內天數＋1」次
`get_user_fills`。本檔測試 per-account in-process 快取（TTL 300s）：
5 分鐘內同一帳號的重複請求不再重新打上游；跨帳號不共用；過期後重新計算；
失敗不快取（不把一次暫時性故障釘住 300 秒）。

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
    assert calls_after_first > 1  # 月總量 + 逐日 daily_bars，不只一次

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
