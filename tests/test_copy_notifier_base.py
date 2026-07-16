"""Notifier ABC 與 ExecutorPort Protocol 測試。"""
from decimal import Decimal

from spark.copytrade.notifier import NullNotifier, RecordingNotifier
from spark.exchange.base import OrderResult


class TestRecordingNotifier:
    """測試 RecordingNotifier——記錄全部通知。"""

    def test_info_records_and_returns_true(self):
        """info() 應記錄並回 True。"""
        notifier = RecordingNotifier()
        result = notifier.info("orders", "Order placed")
        assert result is True
        assert notifier.records == [("info", "orders", "Order placed", None)]

    def test_warn_records_and_returns_true(self):
        """warn() 應記錄並回 True。"""
        notifier = RecordingNotifier()
        result = notifier.warn("errors", "Warning message")
        assert result is True
        assert notifier.records == [("warn", "errors", "Warning message", None)]

    def test_critical_records_and_returns_true(self):
        """critical() 應記錄並回 True。"""
        notifier = RecordingNotifier()
        result = notifier.critical("system", "Critical issue")
        assert result is True
        assert notifier.records == [("critical", "system", "Critical issue", None)]

    def test_dedup_key_recorded(self):
        """dedup_key 應被記錄。"""
        notifier = RecordingNotifier()
        notifier.info("orders", "msg", dedup_key="key123")
        assert notifier.records == [("info", "orders", "msg", "key123")]

    def test_multiple_notifications_accumulate(self):
        """多個通知應累積。"""
        notifier = RecordingNotifier()
        notifier.info("cat1", "msg1")
        notifier.warn("cat2", "msg2")
        notifier.critical("cat3", "msg3", dedup_key="k3")
        assert len(notifier.records) == 3
        assert notifier.records[0] == ("info", "cat1", "msg1", None)
        assert notifier.records[1] == ("warn", "cat2", "msg2", None)
        assert notifier.records[2] == ("critical", "cat3", "msg3", "k3")


class TestNullNotifier:
    """測試 NullNotifier——全吞，回 False。"""

    def test_info_returns_false(self):
        """info() 應回 False。"""
        notifier = NullNotifier()
        result = notifier.info("orders", "msg")
        assert result is False

    def test_warn_returns_false(self):
        """warn() 應回 False。"""
        notifier = NullNotifier()
        result = notifier.warn("errors", "msg")
        assert result is False

    def test_critical_returns_false(self):
        """critical() 應回 False。"""
        notifier = NullNotifier()
        result = notifier.critical("system", "msg")
        assert result is False

    def test_accepts_all_parameters(self):
        """應接受所有參數組合。"""
        notifier = NullNotifier()
        # 都不應拋例外
        notifier.info("cat", "msg")
        notifier.info("cat", "msg", dedup_key="key")
        notifier.warn("cat", "msg")
        notifier.warn("cat", "msg", dedup_key="key")
        notifier.critical("cat", "msg")
        notifier.critical("cat", "msg", dedup_key="key")


class TestExecutorPortProtocol:
    """測試 ExecutorPort Protocol 的 runtime_checkable 語義。"""

    def test_minimal_fake_implements_protocol(self):
        """最小實作應符合 Protocol。"""
        from spark.copytrade.executor import ExecutorPort

        class FakeExecutor:
            def __init__(self):
                self.records = []

            def place(self, spec):
                return True

            def modify(self, oid, spec):
                return True

            def cancel(self, coin, oid):
                return True

            def market_open(self, coin, is_buy, size):
                return OrderResult(ok=True, filled_size=size, avg_px=Decimal("1.0"), raw={})

            def close_reduce_only(self, coin, is_buy, size):
                return OrderResult(ok=True, filled_size=size, avg_px=Decimal("1.0"), raw={})

            def update_leverage(self, coin, leverage, is_cross):
                return True

            def get_open_orders(self):
                return []

        fake = FakeExecutor()
        assert isinstance(fake, ExecutorPort)

    def test_missing_method_fails_protocol_check(self):
        """缺少一個方法應不符合 Protocol。"""
        from spark.copytrade.executor import ExecutorPort

        class IncompleteExecutor:
            records = []

            def place(self, spec):
                return True

            def modify(self, oid, spec):
                return True

            # 缺少 cancel 等其他方法
            def get_open_orders(self):
                return []

        incomplete = IncompleteExecutor()
        assert not isinstance(incomplete, ExecutorPort)

    def test_missing_records_attribute_fails(self):
        """缺少 records 屬性應不符合 Protocol。"""
        from spark.copytrade.executor import ExecutorPort

        class NoRecordsExecutor:
            def place(self, spec):
                return True

            def modify(self, oid, spec):
                return True

            def cancel(self, coin, oid):
                return True

            def market_open(self, coin, is_buy, size):
                return OrderResult(ok=True, filled_size=size, avg_px=Decimal("1.0"), raw={})

            def close_reduce_only(self, coin, is_buy, size):
                return OrderResult(ok=True, filled_size=size, avg_px=Decimal("1.0"), raw={})

            def update_leverage(self, coin, leverage, is_cross):
                return True

            def get_open_orders(self):
                return []

        no_records = NoRecordsExecutor()
        assert not isinstance(no_records, ExecutorPort)
