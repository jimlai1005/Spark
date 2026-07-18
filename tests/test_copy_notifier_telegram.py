"""TelegramNotifier 單元測試。"""
from spark.copytrade.notifier import TelegramNotifier


class TestTelegramNotifierDisabled:
    """測試禁用狀態（無 token，無 send_fn）。"""

    def test_no_token_no_send_fn_all_levels_return_false(self):
        """無 token 無 send_fn → 三 level 全回 False。"""
        notifier = TelegramNotifier(token="", chat_id="", send_fn=None)
        assert notifier.info("test", "msg") is False
        assert notifier.warn("test", "msg") is False
        assert notifier.critical("test", "msg") is False

    def test_no_token_no_send_fn_no_send_calls(self):
        """無 token 無 send_fn → 零 send 呼叫。"""
        call_count = 0

        def fake_send(text: str) -> bool:
            nonlocal call_count
            call_count += 1
            return True

        notifier = TelegramNotifier(token="", chat_id="", send_fn=None)
        notifier.info("cat", "text")
        notifier.warn("cat", "text")
        notifier.critical("cat", "text")
        assert call_count == 0


class TestTelegramNotifierBasic:
    """測試基本發送功能。"""

    def test_info_success_with_send_fn(self):
        """注入 send_fn：info 成功 → True、訊息含 "[INFO]" 與 category。"""
        sent_messages = []

        def fake_send(text: str) -> bool:
            sent_messages.append(text)
            return True

        notifier = TelegramNotifier(token="test", chat_id="123", send_fn=fake_send)
        result = notifier.info("orders", "Order placed")
        assert result is True
        assert len(sent_messages) == 1
        assert "[INFO]" in sent_messages[0]
        assert "orders" in sent_messages[0]
        assert "Order placed" in sent_messages[0]

    def test_warn_message_format(self):
        """warn 級別訊息格式正確。"""
        sent_messages = []

        def fake_send(text: str) -> bool:
            sent_messages.append(text)
            return True

        notifier = TelegramNotifier(token="test", chat_id="123", send_fn=fake_send)
        notifier.warn("positions", "Position warning")
        assert "[WARN]" in sent_messages[0]
        assert "positions" in sent_messages[0]

    def test_critical_message_format(self):
        """critical 級別訊息格式正確。"""
        sent_messages = []

        def fake_send(text: str) -> bool:
            sent_messages.append(text)
            return True

        notifier = TelegramNotifier(token="test", chat_id="123", send_fn=fake_send)
        notifier.critical("risk", "Critical alert")
        assert "[CRIT]" in sent_messages[0]
        assert "risk" in sent_messages[0]


class TestTelegramNotifierDedup:
    """測試去重機制（dedup_key 與 TTL）。"""

    def test_dedup_same_key_second_call_ignored(self):
        """同 key 兩連發 → 第二則 False 且 send 只叫一次。"""
        call_count = 0

        def fake_send(text: str) -> bool:
            nonlocal call_count
            call_count += 1
            return True

        notifier = TelegramNotifier(token="test", chat_id="123", send_fn=fake_send)
        result1 = notifier.info("orders", "msg1", dedup_key="order_1")
        result2 = notifier.info("orders", "msg2", dedup_key="order_1")
        assert result1 is True
        assert result2 is False
        assert call_count == 1

    def test_dedup_ttl_expiry_allows_retry(self):
        """假 clock 推進 301 秒 → 再送成功。"""
        call_count = 0
        fake_time = [0.0]

        def fake_clock() -> float:
            return fake_time[0]

        def fake_send(text: str) -> bool:
            nonlocal call_count
            call_count += 1
            return True

        notifier = TelegramNotifier(
            token="test", chat_id="123", send_fn=fake_send, clock=fake_clock
        )
        result1 = notifier.info("orders", "msg1", dedup_key="order_1")
        assert result1 is True
        assert call_count == 1

        # 時間推進 301 秒（超過 TTL=300）
        fake_time[0] = 301.0
        result2 = notifier.info("orders", "msg2", dedup_key="order_1")
        assert result2 is True
        assert call_count == 2

    def test_different_keys_no_interference(self):
        """不同 key 不互相去重。"""
        call_count = 0

        def fake_send(text: str) -> bool:
            nonlocal call_count
            call_count += 1
            return True

        notifier = TelegramNotifier(token="test", chat_id="123", send_fn=fake_send)
        notifier.info("orders", "msg1", dedup_key="key1")
        notifier.info("orders", "msg2", dedup_key="key2")
        notifier.info("orders", "msg3", dedup_key="key1")
        # key1: send, key2: send, key1 again: blocked
        assert call_count == 2

    def test_no_dedup_key_no_dedup(self):
        """無 dedup_key 不去重（連發都送）。"""
        call_count = 0

        def fake_send(text: str) -> bool:
            nonlocal call_count
            call_count += 1
            return True

        notifier = TelegramNotifier(token="test", chat_id="123", send_fn=fake_send)
        notifier.info("orders", "msg1")
        notifier.info("orders", "msg2")
        notifier.info("orders", "msg3")
        assert call_count == 3

    def test_dedup_expired_keys_swept_on_send(self):
        """過期 key 在下次發送時被清掉（防字典無限成長）。"""
        fake_time = [0.0]

        def fake_clock() -> float:
            return fake_time[0]

        def fake_send(text: str) -> bool:
            return True

        notifier = TelegramNotifier(
            token="test", chat_id="123", send_fn=fake_send, clock=fake_clock
        )
        # t=0：送出 stale_key，時間戳記錄為 0
        assert notifier.info("orders", "msg1", dedup_key="stale_key") is True
        assert "stale_key" in notifier._dedup_times

        # t=301：TTL 已過，用另一個 key 觸發一次發送 → 清掃應移除 stale_key
        fake_time[0] = 301.0
        assert notifier.info("orders", "msg2", dedup_key="fresh_key") is True
        assert "stale_key" not in notifier._dedup_times
        assert "fresh_key" in notifier._dedup_times

    def test_critical_bypasses_dedup(self):
        """critical 訊息不受 dedup 影響：同 key 連發兩次都送出。"""
        call_count = 0

        def fake_send(text: str) -> bool:
            nonlocal call_count
            call_count += 1
            return True

        notifier = TelegramNotifier(token="test", chat_id="123", send_fn=fake_send)
        result1 = notifier.critical("alert", "Critical issue 1", dedup_key="crit_1")
        result2 = notifier.critical("alert", "Critical issue 2", dedup_key="crit_1")
        assert result1 is True
        assert result2 is True
        assert call_count == 2

    def test_non_critical_still_deduplicated(self):
        """對照組：non-critical 訊息同 dedup_key 仍被去重。"""
        call_count = 0

        def fake_send(text: str) -> bool:
            nonlocal call_count
            call_count += 1
            return True

        notifier = TelegramNotifier(token="test", chat_id="123", send_fn=fake_send)
        result1 = notifier.info("alert", "Info 1", dedup_key="info_1")
        result2 = notifier.info("alert", "Info 2", dedup_key="info_1")
        assert result1 is True
        assert result2 is False
        assert call_count == 1


class TestTelegramNotifierMute:
    """測試分類靜音。"""

    def test_muted_info_and_warn_ignored(self):
        """muted_categories={"orders"}：info(orders) 吞、warn(orders) 吞。"""
        call_count = 0

        def fake_send(text: str) -> bool:
            nonlocal call_count
            call_count += 1
            return True

        notifier = TelegramNotifier(
            token="test",
            chat_id="123",
            send_fn=fake_send,
            muted_categories=frozenset(["orders"]),
        )
        result1 = notifier.info("orders", "msg1")
        result2 = notifier.warn("orders", "msg2")
        assert result1 is False
        assert result2 is False
        assert call_count == 0

    def test_critical_unmutable(self):
        """critical(orders) 照送（不受靜音影響）。"""
        call_count = 0

        def fake_send(text: str) -> bool:
            nonlocal call_count
            call_count += 1
            return True

        notifier = TelegramNotifier(
            token="test",
            chat_id="123",
            send_fn=fake_send,
            muted_categories=frozenset(["orders"]),
        )
        result = notifier.critical("orders", "CRITICAL")
        assert result is True
        assert call_count == 1

    def test_unmuted_category_still_sends(self):
        """info(其他) 照送（不在靜音清單）。"""
        call_count = 0

        def fake_send(text: str) -> bool:
            nonlocal call_count
            call_count += 1
            return True

        notifier = TelegramNotifier(
            token="test",
            chat_id="123",
            send_fn=fake_send,
            muted_categories=frozenset(["orders"]),
        )
        result = notifier.info("positions", "msg")
        assert result is True
        assert call_count == 1


class TestTelegramNotifierFailure:
    """測試失敗處理。"""

    def test_send_fn_exception_caught(self):
        """send_fn 拋例外 → 回 False 不拋。"""

        def bad_send(text: str) -> bool:
            raise RuntimeError("Network error")

        notifier = TelegramNotifier(token="test", chat_id="123", send_fn=bad_send)
        result = notifier.info("test", "msg")
        assert result is False

    def test_send_fn_returns_false_no_dedup_advance(self):
        """send_fn 回 False → False 且 dedup 時間戳不前進（下次同 key 仍會嘗試）。"""
        call_count = 0
        fake_time = [0.0]

        def fake_clock() -> float:
            return fake_time[0]

        def failing_send(text: str) -> bool:
            nonlocal call_count
            call_count += 1
            return False

        notifier = TelegramNotifier(
            token="test", chat_id="123", send_fn=failing_send, clock=fake_clock
        )
        result1 = notifier.info("orders", "msg1", dedup_key="order_1")
        assert result1 is False
        assert call_count == 1

        # 同 key 再試，dedup 未前進，所以仍會嘗試
        result2 = notifier.info("orders", "msg2", dedup_key="order_1")
        assert result2 is False
        assert call_count == 2

    def test_send_fn_returns_none_coerced_to_false(self):
        """send_fn 回 None → 回 False（bool 強制轉型，不外洩 None）。"""

        def none_send(text: str):
            return None

        notifier = TelegramNotifier(token="test", chat_id="123", send_fn=none_send)
        result = notifier.info("test", "msg", dedup_key="k1")
        assert result is False
        # None 視同失敗，dedup 時間戳不前進
        assert "k1" not in notifier._dedup_times


class TestTelegramNotifierFromEnv:
    """測試 from_env 類方法。"""

    def test_from_env_missing_token_creates_disabled_instance(self):
        """缺 COPY_TG_BOT_TOKEN → 靜默實例。"""
        env = {"COPY_TG_CHAT_ID": "123"}
        notifier = TelegramNotifier.from_env(env)
        assert notifier.info("test", "msg") is False
        assert notifier.warn("test", "msg") is False
        assert notifier.critical("test", "msg") is False

    def test_from_env_with_token_and_chat_id(self):
        """有 token 和 chat_id → 啟用實例。"""
        sent_messages = []

        def fake_send(text: str) -> bool:
            sent_messages.append(text)
            return True

        env = {"COPY_TG_BOT_TOKEN": "token123", "COPY_TG_CHAT_ID": "chat456"}
        notifier = TelegramNotifier.from_env(env)
        # 手動注入 send_fn 以避免真實網路呼叫
        notifier._send_fn = fake_send
        result = notifier.info("test", "msg")
        assert result is True
        assert len(sent_messages) == 1

    def test_from_env_muted_categories_parsing(self):
        """COPY_TG_MUTED 解析正確。"""
        env = {
            "COPY_TG_BOT_TOKEN": "token123",
            "COPY_TG_CHAT_ID": "chat456",
            "COPY_TG_MUTED": "orders, positions, risk",
        }
        notifier = TelegramNotifier.from_env(env)
        assert "orders" in notifier._muted_categories
        assert "positions" in notifier._muted_categories
        assert "risk" in notifier._muted_categories
        assert len(notifier._muted_categories) == 3

    def test_from_env_empty_muted(self):
        """COPY_TG_MUTED 缺失或空 → 空靜音集合。"""
        env = {
            "COPY_TG_BOT_TOKEN": "token123",
            "COPY_TG_CHAT_ID": "chat456",
        }
        notifier = TelegramNotifier.from_env(env)
        assert len(notifier._muted_categories) == 0

    def test_from_env_muted_with_whitespace(self):
        """COPY_TG_MUTED 中的空白被正確修剪。"""
        env = {
            "COPY_TG_BOT_TOKEN": "token123",
            "COPY_TG_CHAT_ID": "chat456",
            "COPY_TG_MUTED": "  orders  ,  positions  ",
        }
        notifier = TelegramNotifier.from_env(env)
        assert "orders" in notifier._muted_categories
        assert "positions" in notifier._muted_categories
        # 確保沒有多出帶空白的版本
        assert "  orders  " not in notifier._muted_categories
