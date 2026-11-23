"""leader 權益單一產生點（plan 2026-11-22 T2）：純函式＋薄讀取層。

錨例（預註冊）：E3＝過期 181 秒且有倉位 → Unavailable；E4＝total 0 且無曝險 →
value 0、不 raise；E5＝total 0 且有曝險 → Unavailable。"""
from decimal import Decimal

import pytest

from spark.copytrade.leader_equity import (
    MAX_AGE_MS,
    LeaderEquity,
    LeaderEquityUnavailable,
    read_leader_equity,
    resolve_leader_equity,
)
from spark.exchange.base import AccountTotal
from spark.exchange.fakes import FakeAdapter

NOW_MS = 1_763_900_000_000
LEADER = "0xAbCdEf0000000000000000000000000000000001"


def _total(value="707.37", age_ms=0) -> AccountTotal:
    return AccountTotal(value=Decimal(value), as_of_ms=NOW_MS - age_ms)


# ── resolve_leader_equity（純函式）────────────────────────────────────
def test_fresh_positive_total_passes_through():
    eq = resolve_leader_equity(_total("707.37", 5_000), now_ms=NOW_MS,
                               leader_has_exposure=True)
    assert eq == LeaderEquity(value=Decimal("707.37"), as_of_ms=NOW_MS - 5_000,
                              source="exchange_total")


def test_E3_stale_181s_with_position_raises():
    with pytest.raises(LeaderEquityUnavailable) as ei:
        resolve_leader_equity(_total("707.37", 181_000), now_ms=NOW_MS,
                              leader_has_exposure=True)
    assert "過期" in ei.value.reason


def test_age_exactly_180s_is_still_fresh():
    assert MAX_AGE_MS == 180_000
    eq = resolve_leader_equity(_total("707.37", 180_000), now_ms=NOW_MS,
                               leader_has_exposure=True)
    assert eq.value == Decimal("707.37")


def test_as_of_in_the_future_is_not_stale():
    """時鐘偏差：資料點時間戳在本機 now 之後，不算過期。"""
    eq = resolve_leader_equity(_total("707.37", -600_000), now_ms=NOW_MS,
                               leader_has_exposure=True)
    assert eq.value == Decimal("707.37")


def test_E4_zero_total_without_exposure_is_empty_account():
    eq = resolve_leader_equity(_total("0"), now_ms=NOW_MS, leader_has_exposure=False)
    assert eq.value == Decimal("0")
    assert eq.source == "exchange_total"


def test_E5_zero_total_with_exposure_raises():
    with pytest.raises(LeaderEquityUnavailable):
        resolve_leader_equity(_total("0"), now_ms=NOW_MS, leader_has_exposure=True)


def test_negative_total_with_exposure_raises_and_without_is_zero():
    with pytest.raises(LeaderEquityUnavailable):
        resolve_leader_equity(_total("-5"), now_ms=NOW_MS, leader_has_exposure=True)
    eq = resolve_leader_equity(_total("-5"), now_ms=NOW_MS, leader_has_exposure=False)
    assert eq.value == Decimal("0")


# ── read_leader_equity（薄讀取層）─────────────────────────────────────
def test_read_uses_adapter_total_for_the_leader_address():
    fa = FakeAdapter(account_totals={LEADER: _total("707.37", 1_000)})
    eq = read_leader_equity(fa, LEADER, now_ms=NOW_MS, leader_has_exposure=True)
    assert eq.value == Decimal("707.37")
    assert fa.calls["get_account_total"] == [{"address": LEADER}]


def test_read_converts_value_error_to_unavailable():
    fa = FakeAdapter()  # 沒有任何 account_totals → get_account_total 回 ValueError

    with pytest.raises(LeaderEquityUnavailable) as ei:
        read_leader_equity(fa, LEADER, now_ms=NOW_MS, leader_has_exposure=True)
    assert ei.value.reason  # 給人看的一句話非空


def test_read_lets_network_errors_propagate_unchanged():
    class _Down(FakeAdapter):
        def get_account_total(self, address):
            raise ConnectionError("reset by peer")

    with pytest.raises(ConnectionError):
        read_leader_equity(_Down(), LEADER, now_ms=NOW_MS, leader_has_exposure=True)


# ── D3a：leader 無曝險時永不 raise（plan 2026-11-22 T2b 前置小修）───────────
def test_D3a_no_exposure_stale_181s_does_not_raise():
    """無曝險＋過期 181 秒：所有目標都是 0，不得因權益過期讓整輪提前返回。"""
    eq = resolve_leader_equity(_total("707.37", 181_000), now_ms=NOW_MS,
                               leader_has_exposure=False)
    assert eq.value == Decimal("707.37")  # 過期但值 > 0：照回該值
    assert eq.source == "exchange_total"


def test_D3a_no_exposure_stale_and_nonpositive_is_zero():
    eq = resolve_leader_equity(_total("-5", 600_000), now_ms=NOW_MS,
                               leader_has_exposure=False)
    assert eq.value == Decimal("0")


def test_D3a_no_exposure_value_error_returns_zero_without_raising():
    fa = FakeAdapter()  # 沒有 account_totals → get_account_total 丟 ValueError
    eq = read_leader_equity(fa, LEADER, now_ms=NOW_MS, leader_has_exposure=False)
    assert eq == LeaderEquity(value=Decimal("0"), as_of_ms=0, source="exchange_total")


def test_D3a_no_exposure_network_error_still_propagates():
    class _Down(FakeAdapter):
        def get_account_total(self, address):
            raise ConnectionError("reset by peer")

    with pytest.raises(ConnectionError):
        read_leader_equity(_Down(), LEADER, now_ms=NOW_MS, leader_has_exposure=False)


def test_D3a_with_exposure_behaviour_unchanged_stale_and_value_error():
    with pytest.raises(LeaderEquityUnavailable):
        resolve_leader_equity(_total("707.37", 181_000), now_ms=NOW_MS,
                              leader_has_exposure=True)
    with pytest.raises(LeaderEquityUnavailable):
        read_leader_equity(FakeAdapter(), LEADER, now_ms=NOW_MS,
                           leader_has_exposure=True)
