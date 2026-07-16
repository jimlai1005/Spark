"""1:1 characterization 移植自 hl-copytrader tests/test_resilience.py（唯讀參考）。
結構偏差：hl 原版用 monkeypatch 打掉 time.sleep；spark 版把 sleep_fn 參數化，
測試改用 RecordingSleep 記錄呼叫的延遲序列，斷言退避而不真睡。
"""
import pytest
from spark import resilience
from spark.resilience import run, ResilientExchange, _is_transient_error, VERIFIED_OK, RETRY_ATTEMPTS

CONN = ConnectionError(
    "('Connection aborted.', ConnectionResetError(54, 'Connection reset by peer'))"
)


class RecordingSleep:
    """替身 sleep_fn：不真睡，只記錄收到的延遲序列。"""
    def __init__(self):
        self.delays = []

    def __call__(self, delay):
        self.delays.append(delay)


class Counter:
    """前 fail_times 次呼叫丟 exc，之後回 ret；記錄被呼叫次數。"""
    def __init__(self, fail_times, exc, ret="OK"):
        self.n = 0
        self.fail_times = fail_times
        self.exc = exc
        self.ret = ret

    def __call__(self):
        self.n += 1
        if self.n <= self.fail_times:
            raise self.exc
        return self.ret


# --- 分類器：transient marker 逐案 + 語意錯誤不重試 ---

@pytest.mark.parametrize("exc", [
    CONN,
    TimeoutError("timed out"),
    ConnectionResetError(54, "Connection reset by peer"),
    Exception("Connection aborted"),
    Exception("Connection broken"),
    Exception("remote end closed by peer"),
    Exception("Read timed out"),
    Exception("timeout while waiting"),
    Exception("max retries exceeded"),
    Exception("Service temporarily unavailable"),
    Exception("502 Bad Gateway"),
    Exception("503 Service Unavailable"),
    Exception("504 Gateway Timeout"),
])
def test_classifier_transient_markers(exc):
    assert _is_transient_error(exc)


@pytest.mark.parametrize("exc", [
    ValueError("Insufficient margin"),
    ValueError("Order rejected"),
    Exception("invalid signature"),
])
def test_classifier_semantic_not_transient(exc):
    assert not _is_transient_error(exc)


# --- run()：idempotent 重試 ---

def test_idempotent_retries_then_succeeds():
    fn = Counter(fail_times=2, exc=CONN)
    sleeper = RecordingSleep()
    assert run(fn, what="x", idempotent=True, sleep_fn=sleeper) == "OK"
    assert fn.n == 3


def test_idempotent_gives_up_after_attempts():
    fn = Counter(fail_times=99, exc=CONN)
    sleeper = RecordingSleep()
    with pytest.raises(ConnectionError):
        run(fn, what="x", idempotent=True, sleep_fn=sleeper)
    assert fn.n == RETRY_ATTEMPTS


def test_semantic_not_retried():
    fn = Counter(fail_times=99, exc=ValueError("rejected"))
    sleeper = RecordingSleep()
    with pytest.raises(ValueError):
        run(fn, what="x", idempotent=True, sleep_fn=sleeper)
    assert fn.n == 1
    assert sleeper.delays == []


def test_non_idempotent_no_verify_runs_once():
    fn = Counter(fail_times=99, exc=CONN)
    sleeper = RecordingSleep()
    with pytest.raises(ConnectionError):
        run(fn, what="x", idempotent=False, sleep_fn=sleeper)
    assert fn.n == 1
    assert sleeper.delays == []


# --- run()：verify 三態 ---

def test_verify_landed_returns_sentinel_without_resend():
    fn = Counter(fail_times=99, exc=CONN)
    result = run(fn, what="x", idempotent=False, verify=lambda: True, sleep_fn=RecordingSleep())
    assert result == VERIFIED_OK
    assert fn.n == 1


def test_verify_not_landed_resends_then_succeeds():
    fn = Counter(fail_times=1, exc=CONN)
    sleeper = RecordingSleep()
    result = run(fn, what="x", idempotent=False, verify=lambda: False, sleep_fn=sleeper)
    assert result == "OK"
    assert fn.n == 2
    assert sleeper.delays == [resilience.RETRY_BASE_DELAY]  # 第 1 次失敗後延遲一次即成功


def test_verify_read_failure_assumes_landed():
    fn = Counter(fail_times=99, exc=CONN)

    def boom():
        raise RuntimeError("read failed")

    result = run(fn, what="x", idempotent=False, verify=boom, sleep_fn=RecordingSleep())
    assert result == VERIFIED_OK
    assert fn.n == 1


# --- log 語意：verify 兩種信心層級分開記；重試耗盡有專屬告警 ---

def test_verify_true_logs_verified_delivery(caplog):
    fn = Counter(fail_times=99, exc=CONN)
    with caplog.at_level("WARNING"):
        result = run(fn, what="開倉", idempotent=False, verify=lambda: True,
                     sleep_fn=RecordingSleep())
    assert result == VERIFIED_OK
    assert "已驗證送達" in caplog.text            # 真的查過、確認送達
    assert "verify 查詢失敗" not in caplog.text


def test_verify_exception_logs_conservative_assumption(caplog):
    fn = Counter(fail_times=99, exc=CONN)

    def boom():
        raise RuntimeError("read failed")

    with caplog.at_level("WARNING"):
        result = run(fn, what="開倉", idempotent=False, verify=boom,
                     sleep_fn=RecordingSleep())
    assert result == VERIFIED_OK
    assert "verify 查詢失敗" in caplog.text       # 查不出來、保守假設——非真實驗證
    assert "read failed" in caplog.text           # verify 例外內容要進 log
    assert "未真實驗證" in caplog.text
    assert "已驗證送達" not in caplog.text


def test_retry_exhaustion_logs_before_raise(caplog):
    fn = Counter(fail_times=99, exc=CONN)
    with caplog.at_level("WARNING"):
        with pytest.raises(ConnectionError):
            run(fn, what="平倉", idempotent=True, sleep_fn=RecordingSleep())
    assert f"重試 {RETRY_ATTEMPTS} 次仍失敗，放棄並上拋" in caplog.text


def test_semantic_error_has_no_exhaustion_log(caplog):
    fn = Counter(fail_times=99, exc=ValueError("rejected"))
    with caplog.at_level("WARNING"):
        with pytest.raises(ValueError):
            run(fn, what="平倉", idempotent=True, sleep_fn=RecordingSleep())
    assert "仍失敗" not in caplog.text            # 語意錯誤 ≠ 重試打光，不得混用告警


def test_single_run_transient_has_no_exhaustion_log(caplog):
    fn = Counter(fail_times=99, exc=CONN)
    with caplog.at_level("WARNING"):
        with pytest.raises(ConnectionError):
            run(fn, what="改單", idempotent=False, sleep_fn=RecordingSleep())
    assert "仍失敗" not in caplog.text            # 單次嘗試（不可重試）≠ 重試打光


# --- 指數退避序列 ---

def test_exponential_backoff_sequence_recorded_by_sleep_fn():
    fn = Counter(fail_times=2, exc=CONN)
    sleeper = RecordingSleep()
    run(fn, what="x", idempotent=True, base_delay=0.6, sleep_fn=sleeper)
    assert sleeper.delays == [0.6, 1.2]  # base * 2**(i-1)：第 1、2 次失敗後


# --- ResilientExchange wrapper ---

class FakeSDK:
    """記錄每次呼叫；可設定哪個方法前幾次丟暫時性錯誤。"""
    def __init__(self, fail=None, exc=CONN):
        self.calls = []
        self.fail = fail or {}      # {"market_open": 99, ...}
        self.exc = exc
        self.counts = {}

    def _do(self, name, a, k, ret):
        self.counts[name] = self.counts.get(name, 0) + 1
        self.calls.append((name, a, k))
        if self.counts[name] <= self.fail.get(name, 0):
            raise self.exc
        return ret

    def market_close(self, *a, **k): return self._do("market_close", a, k, "closed")
    def market_open(self, *a, **k):  return self._do("market_open", a, k, "opened")
    def order(self, *a, **k):        return self._do("order", a, k, "ordered")
    def update_leverage(self, *a, **k): return self._do("update_leverage", a, k, "lev")
    def cancel(self, *a, **k):       return self._do("cancel", a, k, "cancelled")
    def modify_order(self, *a, **k): return self._do("modify_order", a, k, "modified")

    def approve_builder_fee(self, builder, max_fee_rate):
        self.calls.append(("approve_builder_fee", (builder, max_fee_rate), {}))
        return {"status": "ok"}


def _rex(sdk, **kw):
    return ResilientExchange(sdk, sleep_fn=RecordingSleep(), **kw)


def test_wrapper_strips_verify_before_sdk():
    sdk = FakeSDK()
    rex = _rex(sdk)
    rex.order("BTC", True, 0.1, 60000.0,
              order_type={"limit": {"tif": "Gtc"}}, reduce_only=False,
              _verify=lambda: True)
    name, a, k = sdk.calls[-1]
    assert name == "order"
    assert "_verify" not in k          # 包裝層吃掉 _verify，不傳給 SDK
    assert k["reduce_only"] is False
    assert a == ("BTC", True, 0.1, 60000.0)


def test_wrapper_close_retries_idempotent():
    sdk = FakeSDK(fail={"market_close": 2})
    rex = _rex(sdk)
    assert rex.market_close("BTC", 0.1) == "closed"
    assert sdk.counts["market_close"] == 3


def test_wrapper_update_leverage_retries_idempotent():
    sdk = FakeSDK(fail={"update_leverage": 2})
    rex = _rex(sdk)
    assert rex.update_leverage(5, "BTC", True) == "lev"
    assert sdk.counts["update_leverage"] == 3


def test_wrapper_cancel_retries_idempotent():
    sdk = FakeSDK(fail={"cancel": 2})
    rex = _rex(sdk)
    assert rex.cancel("BTC", 123) == "cancelled"
    assert sdk.counts["cancel"] == 3


def test_wrapper_reduce_only_order_retries_directly():
    sdk = FakeSDK(fail={"order": 2})
    rex = _rex(sdk)
    rex.order("xyz:NVDA", False, 1.0, 100.0,
              order_type={"limit": {"tif": "Ioc"}}, reduce_only=True)
    assert sdk.counts["order"] == 3    # reduce_only → 冪等 → 直接重試


def test_wrapper_non_reduce_only_order_no_verify_runs_once():
    sdk = FakeSDK(fail={"order": 99})
    rex = _rex(sdk)
    with pytest.raises(ConnectionError):
        rex.order("BTC", True, 0.1, 60000.0, order_type={"limit": {"tif": "Ioc"}},
                  reduce_only=False)
    assert sdk.counts["order"] == 1    # 無 _verify → 單次嘗試、不盲重送


def test_wrapper_market_open_verify_then_skip_resend():
    sdk = FakeSDK(fail={"market_open": 99})
    rex = _rex(sdk)
    result = rex.market_open("BTC", True, 0.1, _verify=lambda: True)
    assert result == VERIFIED_OK
    assert sdk.counts["market_open"] == 1   # 驗證已送達 → 不重送


def test_wrapper_market_open_verify_false_resends():
    sdk = FakeSDK(fail={"market_open": 1})
    rex = _rex(sdk)
    result = rex.market_open("BTC", True, 0.1, _verify=lambda: False)
    assert result == "opened"
    assert sdk.counts["market_open"] == 2   # 驗證未送達 → 重送成功


def test_wrapper_market_open_no_verify_runs_once():
    sdk = FakeSDK(fail={"market_open": 99})
    rex = _rex(sdk)
    with pytest.raises(ConnectionError):
        rex.market_open("BTC", True, 0.1)
    assert sdk.counts["market_open"] == 1


def test_wrapper_modify_does_not_retry():
    sdk = FakeSDK(fail={"modify_order": 99})
    rex = _rex(sdk)
    with pytest.raises(ConnectionError):
        rex.modify_order(123, "BTC", True, 0.1, 60000.0, {"limit": {"tif": "Gtc"}})
    assert sdk.counts["modify_order"] == 1


def test_wrapper_getattr_passthrough_to_inner_exchange():
    sdk = FakeSDK()
    rex = _rex(sdk)
    result = rex.approve_builder_fee("0xbuilder", "0.001")
    assert result == {"status": "ok"}
    assert ("approve_builder_fee", ("0xbuilder", "0.001"), {}) in sdk.calls


def test_wrapper_default_sleep_fn_is_time_sleep():
    # 未注入 sleep_fn 時，行為與 hl 原版一致（真呼叫 time.sleep）——
    # 用 fail_times=0（不觸發重試）驗證預設值可用，不真的觸發睡眠。
    sdk = FakeSDK()
    rex = ResilientExchange(sdk)
    assert rex.market_close("BTC", 0.1) == "closed"
