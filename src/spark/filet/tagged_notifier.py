"""TaggedNotifier — per-follower alert attribution and dedup namespacing."""
from spark.copytrade.notifier import Notifier


class TaggedNotifier(Notifier):
    """多 follower 匯同一頻道時為每則告警加 follower 標籤，並將 dedup_key
    納入 follower 命名空間，使告警可歸屬、跨 follower 不互相去重。"""

    def __init__(self, inner: Notifier, follower_id: str):
        self._inner = inner
        self._tag = follower_id

    def _key(self, k):
        return f"{self._tag}:{k}" if k else None

    def info(self, category, text, dedup_key=None):
        return self._inner.info(category, f"[{self._tag}] {text}", self._key(dedup_key))

    def warn(self, category, text, dedup_key=None):
        return self._inner.warn(category, f"[{self._tag}] {text}", self._key(dedup_key))

    def critical(self, category, text, dedup_key=None):
        return self._inner.critical(category, f"[{self._tag}] {text}", self._key(dedup_key))
