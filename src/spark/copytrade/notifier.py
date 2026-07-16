"""Notifier ABC——通知介面。"""
import os
import time
from abc import ABC, abstractmethod
from typing import Callable


class Notifier(ABC):
    """通知抽象基類。三個 level：info/warn/critical。

    Task 8/11/13 的 fake notifier 與 Task 12 的實作 notifier 都必須繼承此類。
    回傳值：True = 發送成功，False = 發送失敗（或未發送）。
    """

    @abstractmethod
    def info(self, category: str, text: str, dedup_key: str | None = None) -> bool:
        """記錄 info 級通知。"""
        pass

    @abstractmethod
    def warn(self, category: str, text: str, dedup_key: str | None = None) -> bool:
        """記錄 warn 級通知。"""
        pass

    @abstractmethod
    def critical(self, category: str, text: str, dedup_key: str | None = None) -> bool:
        """記錄 critical 級通知。"""
        pass


class NullNotifier(Notifier):
    """無操作 notifier——全吞，回 False。用於測試或安全關閉通知。"""

    def info(self, category: str, text: str, dedup_key: str | None = None) -> bool:
        """接受但不發送，回 False。"""
        return False

    def warn(self, category: str, text: str, dedup_key: str | None = None) -> bool:
        """接受但不發送，回 False。"""
        return False

    def critical(self, category: str, text: str, dedup_key: str | None = None) -> bool:
        """接受但不發送，回 False。"""
        return False


class RecordingNotifier(Notifier):
    """測試用 notifier——記錄所有通知為 (level, category, text, dedup_key) 元組。"""

    def __init__(self) -> None:
        """初始化記錄列表。"""
        self.records: list[tuple[str, str, str, str | None]] = []

    def info(self, category: str, text: str, dedup_key: str | None = None) -> bool:
        """記錄 info 級通知，回 True。"""
        self.records.append(("info", category, text, dedup_key))
        return True

    def warn(self, category: str, text: str, dedup_key: str | None = None) -> bool:
        """記錄 warn 級通知，回 True。"""
        self.records.append(("warn", category, text, dedup_key))
        return True

    def critical(self, category: str, text: str, dedup_key: str | None = None) -> bool:
        """記錄 critical 級通知，回 True。"""
        self.records.append(("critical", category, text, dedup_key))
        return True


class TelegramNotifier(Notifier):
    """分級 Telegram 通知。無 token/chat → 全靜默回 False（不 raise）。
    dedup：同 dedup_key 在 TTL 內第二則吞掉（TTL=300s）。
    分類靜音：muted_categories 內的 info/warn 吞掉；critical 永不可靜音。
    發送失敗（send_fn 拋例外或回 False）→ 吞掉回 False，絕不讓通知失敗炸引擎。
    """

    DEDUP_TTL = 300.0

    def __init__(
        self,
        token: str = "",
        chat_id: str = "",
        *,
        send_fn: Callable[[str], bool] | None = None,
        muted_categories: frozenset[str] = frozenset(),
        clock: Callable[[], float] = time.time,
    ):
        """初始化 TelegramNotifier。

        Args:
            token: Telegram bot token（空字串時禁用）。
            chat_id: Telegram chat ID（空字串時禁用）。
            send_fn: 自訂傳送函數，簽名 (text: str) -> bool。
                未注入且有 token → 使用預設 HTTP 實作（lazy import）。
            muted_categories: 靜音分類集合（info/warn 級別吞掉，critical 不受影響）。
            clock: 時間源（測試用；預設 time.time）。
        """
        self._token = token
        self._chat_id = chat_id
        self._send_fn = send_fn
        self._muted_categories = muted_categories
        self._clock = clock
        self._dedup_times: dict[str, float] = {}
        # 無 token 或 chat_id 且未注入 send_fn → 禁用
        self._enabled = bool(token and chat_id) or send_fn is not None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "TelegramNotifier":
        """從環境變數建構 TelegramNotifier。

        Args:
            env: 環境變數字典（未指定時讀 os.environ）。

        Returns:
            TelegramNotifier 實例。
            - 缺 COPY_TG_BOT_TOKEN → 靜默實例（_enabled=False）。
            - COPY_TG_MUTED 為逗號分隔的分類清單（例 "orders,positions"）。
        """
        if env is None:
            env = os.environ
        token = env.get("COPY_TG_BOT_TOKEN", "")
        chat_id = env.get("COPY_TG_CHAT_ID", "")
        muted_str = env.get("COPY_TG_MUTED", "")
        muted_categories = frozenset(
            cat.strip() for cat in muted_str.split(",") if cat.strip()
        )
        return cls(token=token, chat_id=chat_id, muted_categories=muted_categories)

    _LEVEL_PREFIXES = {"info": "INFO", "warn": "WARN", "critical": "CRIT"}

    def _format_message(self, level: str, category: str, text: str) -> str:
        """格式化通知訊息。前綴依 spec：INFO/WARN/CRIT。"""
        prefix = self._LEVEL_PREFIXES[level]
        return f"[{prefix}] {category} | {text}"

    def _http_send(self, text: str) -> bool:
        """預設 HTTP 實作（lazy import urllib）。成功回 True，否則 False。"""
        try:
            import json
            import urllib.request

            api_url = f"https://api.telegram.org/bot{self._token}/sendMessage"
            data = json.dumps(
                {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"}
            ).encode("utf-8")
            req = urllib.request.Request(
                api_url, data=data, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.status == 200
        except Exception:
            # 失敗吞掉，不拋異常
            return False

    def _send(self, level: str, category: str, text: str, dedup_key: str | None = None) -> bool:
        """發送訊息（內部）。

        流程：
        1. 未啟用 → False
        2. level != critical 且 category in muted → False
        3. dedup_key 且 clock()-上次 < TTL → False
        4. 使用 send_fn 或 _http_send 發送
        5. 成功則記錄時間戳，失敗（例外/False）→ False
        """
        if not self._enabled:
            return False

        # 分類靜音：非 critical 的 muted 分類吞掉
        if level != "critical" and category in self._muted_categories:
            return False

        # Dedup 檢查
        if dedup_key is not None:
            now = self._clock()
            # 順手清掉過期項，避免字典無限成長（照 hl telegram.py 模式）
            for k in [k for k, ts in self._dedup_times.items() if now - ts > self.DEDUP_TTL]:
                self._dedup_times.pop(k, None)
            if dedup_key in self._dedup_times:
                last_sent = self._dedup_times[dedup_key]
                if now - last_sent < self.DEDUP_TTL:
                    return False
            # Dedup 檢查通過，稍後成功時才更新時間戳

        # 格式化訊息
        message = self._format_message(level, category, text)

        # 發送（捕捉所有例外）
        try:
            send_fn = self._send_fn or self._http_send
            result = send_fn(message)
        except Exception:
            # 例外吞掉
            return False

        # 只在成功時更新 dedup 時間戳（對應 hl telegram.py 的「成功才前進」語意）
        if result and dedup_key is not None:
            self._dedup_times[dedup_key] = self._clock()

        return bool(result)

    def info(self, category: str, text: str, dedup_key: str | None = None) -> bool:
        """發送 info 級通知。"""
        return self._send("info", category, text, dedup_key)

    def warn(self, category: str, text: str, dedup_key: str | None = None) -> bool:
        """發送 warn 級通知。"""
        return self._send("warn", category, text, dedup_key)

    def critical(self, category: str, text: str, dedup_key: str | None = None) -> bool:
        """發送 critical 級通知（永遠不被靜音）。"""
        return self._send("critical", category, text, dedup_key)
