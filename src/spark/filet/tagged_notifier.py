"""TaggedNotifier — per-follower alert attribution and dedup namespacing."""
from spark.copytrade.notifier import Notifier


class TaggedNotifier(Notifier):
    """多 follower 匯同一頻道時為每則告警加 follower 標籤，並將 dedup_key
    納入 follower 命名空間，使告警可歸屬、跨 follower 不互相去重。"""

    def __init__(self, inner: Notifier, follower_id: str):
        self._inner = inner
        self._tag = follower_id

    def _key(self, k):
        # is not None（非真值判斷）：空字串 dedup_key 是「有意義但空」的 key，應
        # 命名空間化為 "<tag>:" 而非被當成 falsy 吞成 None（T3 reviewer minor，
        # 與 TelegramNotifier 的 is not None 慣例一致）。
        return f"{self._tag}:{k}" if k is not None else None

    def info(self, category, text, dedup_key=None):
        return self._inner.info(category, f"[{self._tag}] {text}", self._key(dedup_key))

    def warn(self, category, text, dedup_key=None):
        return self._inner.warn(category, f"[{self._tag}] {text}", self._key(dedup_key))

    def critical(self, category, text, dedup_key=None):
        return self._inner.critical(category, f"[{self._tag}] {text}", self._key(dedup_key))
