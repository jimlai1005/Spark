"""tests/test_hl_gateway_budget.py — Task 1.2：HLGateway 每次嘗試經權重限流。
plan docs/superpowers/plans/2026-09-20-explore-rate-limit-refactor.md Task 1.2 Step 1 原碼照抄。
"""
import httpx
import pytest

from spark.publicapi.hl import HLGateway
from spark.publicapi.hl_budget import BudgetExhausted, ScopePaused, WeightLimiter


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _gw(post, clock, **kw):
    lim = WeightLimiter(global_cap=900, scope_caps={"explore": 300},
                        now_fn=clock.now, sleep_fn=clock.sleep, rng=lambda: 0.0)
    return HLGateway("https://x", post_fn=post, sleep_fn=clock.sleep, limiter=lim, **kw), lim


def _http_error(status):
    req = httpx.Request("POST", "https://x/info")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(status, request=req))


def test_each_attempt_reserves_weight_by_info_type():
    c = Clock()
    seen = []

    def post(url, body):
        seen.append(body["type"])
        return {"marginSummary": {"accountValue": "1"}}
    gw, lim = _gw(post, c)
    gw.clearinghouse_state("0xabc")
    gw.portfolio("0xabc")
    assert lim.snapshot()["used"] == {"interactive": 22}


def test_retry_attempts_each_pay():
    """resilience.run 對 transient 重試 3 次——每次嘗試都要各自預留（spec §5.2）。"""
    c = Clock()
    n = {"i": 0}

    def post(url, body):
        n["i"] += 1
        raise ConnectionError("connection reset")
    gw, lim = _gw(post, c)
    with pytest.raises(ConnectionError):
        gw.clearinghouse_state("0xabc")
    assert n["i"] == 3
    assert lim.snapshot()["used"] == {"interactive": 6}


def test_429_reported_and_not_retried():
    c = Clock()
    n = {"i": 0}

    def post(url, body):
        n["i"] += 1
        raise _http_error(429)
    gw, lim = _gw(post, c)
    with pytest.raises(httpx.HTTPStatusError):
        gw.portfolio("0xabc")
    assert n["i"] == 1
    s = lim.snapshot()
    assert s["counters"]["rate_limited"] == 1 and s["paused_until"]["explore"] == pytest.approx(60.0)


def test_budget_exhausted_is_not_retried_and_waits_bounded():
    c = Clock()

    def post(url, body):
        return {}
    gw, lim = _gw(post, c, wait_s=2.0)
    for _ in range(45):
        lim.try_reserve(20, "interactive")
    with pytest.raises(BudgetExhausted):
        gw.portfolio("0xabc")
    assert c.t == pytest.approx(2.0, abs=0.3)


def test_scoped_gateway_uses_scope_and_no_wait():
    c = Clock()

    def post(url, body):
        return {}
    gw, lim = _gw(post, c)
    ex = gw.scoped("explore")
    ex.portfolio("0xabc")
    assert lim.snapshot()["used"] == {"explore": 20}
    for _ in range(14):
        lim.try_reserve(20, "explore")
    with pytest.raises(BudgetExhausted):
        ex.portfolio("0xabc")          # explore 子預算滿、wait_s=0 立刻拋
    assert c.t == 0.0


def test_scoped_gateway_raises_scope_paused():
    c = Clock()

    def post(url, body):
        return {}
    gw, lim = _gw(post, c)
    lim.note_429("interactive")
    with pytest.raises(ScopePaused):
        gw.scoped("explore").clearinghouse_state("0xabc")


def test_no_limiter_keeps_old_behaviour():
    c = Clock()
    gw = HLGateway("https://x", post_fn=lambda u, b: {}, sleep_fn=c.sleep)
    assert gw.portfolio("0xabc") == {}
