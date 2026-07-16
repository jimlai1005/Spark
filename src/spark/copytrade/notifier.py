"""Notifier ABC——通知介面。"""
from abc import ABC, abstractmethod


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
