"""TelegramNotifier 單元測試。"""
from spark.copytrade.notifier import CRITICAL_DEDUP_TTL_S, TelegramNotifier


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

    def test_critical_honours_dedup_key(self):
        """⭐ critical 也吃 dedup_key：同 key 在 TTL 內第二則被抑制。

        2026-07-19 修正（opus 審查 I2）：原本 critical 完全忽略 dedup_key。後果不是吵
        而是**遮蔽**——kill switch 的 dd_breach／tripped 每分鐘重發，把真正的新事件洗掉，
        多 follower 共頻道還會撞 Telegram 429（撞了就是所有告警都送不出去）。
        """
        sent = []
        notifier = TelegramNotifier(token="test", chat_id="123",
                                    send_fn=lambda t: bool(sent.append(t)) or True)
        assert notifier.critical("alert", "Critical issue 1", dedup_key="crit_1") is True
        assert notifier.critical("alert", "Critical issue 2", dedup_key="crit_1") is False
        assert len(sent) == 1

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


class TestCriticalDedup:
    """⭐ critical 的去重與抑制計數（2026-07-19 opus 審查 I2）。

    設計判準：critical 必須「大聲」，但大聲 ≠ 洗版。去重讓它不淹沒別的告警，
    抑制計數讓「這件事在窗內又發生了 N 次」不會因為去重而消失——兩者缺一，
    就會回到「要嘛洗版、要嘛假裝只發生一次」的二選一。
    """

    @staticmethod
    def _notifier(sent, clock=None):
        def _send(text: str) -> bool:
            sent.append(text)
            return True

        kw = {"clock": clock} if clock is not None else {}
        return TelegramNotifier(token="test", chat_id="123", send_fn=_send, **kw)

    def test_same_key_within_ttl_sends_once(self):
        """同 key 在 TTL 內連發 5 次 → 只送 1 則（原行為是 5 則全送）。"""
        sent = []
        n = self._notifier(sent)
        results = [n.critical("killswitch", f"回撤破線 #{i}", dedup_key="dd_breach")
                   for i in range(5)]
        assert results == [True, False, False, False, False]
        assert len(sent) == 1

    def test_resend_after_ttl_carries_suppression_count(self):
        """⭐ TTL 過後重送，且訊息帶出這段期間被抑制的次數——「持續發生」不得被吃掉。"""
        sent, t = [], [0.0]
        n = self._notifier(sent, clock=lambda: t[0])
        n.critical("killswitch", "回撤破線", dedup_key="dd_breach")   # 送出
        for _ in range(4):
            n.critical("killswitch", "回撤破線", dedup_key="dd_breach")  # 抑制 4 則
        assert len(sent) == 1

        t[0] = CRITICAL_DEDUP_TTL_S + 1
        assert n.critical("killswitch", "回撤破線", dedup_key="dd_breach") is True
        assert len(sent) == 2
        assert "第 5 次" in sent[1] and "前 4 次已抑制" in sent[1]

    def test_suppression_count_resets_after_successful_send(self):
        """成功送出即歸零：下一輪的計數從新的抑制期重新算，不累積歷史。"""
        sent, t = [], [0.0]
        n = self._notifier(sent, clock=lambda: t[0])
        n.critical("k", "x", dedup_key="c")
        n.critical("k", "x", dedup_key="c")            # 抑制 1
        t[0] = CRITICAL_DEDUP_TTL_S + 1
        n.critical("k", "x", dedup_key="c")            # 重送（帶「前 1 次」）
        t[0] = 2 * CRITICAL_DEDUP_TTL_S + 2
        n.critical("k", "x", dedup_key="c")            # 再重送：中間沒被抑制過
        assert "已抑制" in sent[1]
        assert "已抑制" not in sent[2]

    def test_different_keys_are_independent(self):
        """不同 key 各自獨立——去重不得讓一個持續狀態遮蔽另一個新事件。"""
        sent = []
        n = self._notifier(sent)
        assert n.critical("killswitch", "回撤破線", dedup_key="dd_breach") is True
        assert n.critical("killswitch", "已 tripped", dedup_key="tripped") is True
        assert n.critical("leader", "leader 被撤銷", dedup_key="leader_revoked") is True
        assert len(sent) == 3

    def test_no_dedup_key_never_suppressed(self):
        """未帶 dedup_key 的 critical 一律照送（沿用既有語意，不受本次修改影響）。"""
        sent = []
        n = self._notifier(sent)
        for _ in range(3):
            assert n.critical("loop", "連續同步失敗") is True
        assert len(sent) == 3

    def test_critical_window_is_longer_than_warn_window(self):
        """critical 與 warn 用各自的 TTL：warn 在 301s 已可重送，critical 還在窗內。"""
        sent, t = [], [0.0]
        n = self._notifier(sent, clock=lambda: t[0])
        n.warn("orders", "w", dedup_key="w1")
        n.critical("killswitch", "c", dedup_key="c1")
        t[0] = TelegramNotifier.DEDUP_TTL + 1
        assert n.warn("orders", "w", dedup_key="w1") is True       # warn 行為不變
        assert n.critical("killswitch", "c", dedup_key="c1") is False  # critical 仍在窗內

    def test_info_sweep_does_not_shorten_critical_window(self):
        """⭐ 清掃過期項時每個條目用自己的 TTL：一則 info 的清掃不得把還在窗內的
        critical 條目掃掉（否則 critical 的長窗被無聲降級成 300s）。"""
        sent, t = [], [0.0]
        n = self._notifier(sent, clock=lambda: t[0])
        n.critical("killswitch", "c", dedup_key="c1")
        t[0] = TelegramNotifier.DEDUP_TTL + 1
        n.info("orders", "i", dedup_key="i1")          # 觸發清掃
        assert "c1" in n._dedup_times
        assert n.critical("killswitch", "c", dedup_key="c1") is False
