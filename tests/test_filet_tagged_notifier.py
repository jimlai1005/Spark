"""TaggedNotifier — per-follower alert attribution and dedup namespacing."""
from spark.copytrade.notifier import RecordingNotifier
from spark.filet.tagged_notifier import TaggedNotifier


def test_info_warn_critical_with_tag_prefix():
    """Test 1: info/warn/critical → inner level、text 前綴 [<id>]。"""
    inner = RecordingNotifier()
    notifier = TaggedNotifier(inner, "alice")

    notifier.info("category1", "message1")
    notifier.warn("category2", "message2")
    notifier.critical("category3", "message3")

    assert len(inner.records) == 3
    assert inner.records[0] == ("info", "category1", "[alice] message1", None)
    assert inner.records[1] == ("warn", "category2", "[alice] message2", None)
    assert inner.records[2] == ("critical", "category3", "[alice] message3", None)


def test_dedup_key_namespacing():
    """Test 2: dedup_key 非 None → <id>:<key>；None → None。"""
    inner = RecordingNotifier()
    notifier = TaggedNotifier(inner, "alice")

    notifier.info("cat", "msg1", dedup_key="key1")
    notifier.info("cat", "msg2", dedup_key=None)

    assert len(inner.records) == 2
    assert inner.records[0][3] == "alice:key1"  # dedup_key 變成 <id>:<key>
    assert inner.records[1][3] is None          # None 傳 None


def test_critical_level_unchanged():
    """Test 3: critical 照樣 critical（不改 level）。"""
    inner = RecordingNotifier()
    notifier = TaggedNotifier(inner, "alice")

    notifier.critical("cat", "msg", dedup_key="key1")

    assert inner.records[0][0] == "critical"  # level 保持 critical
    assert inner.records[0][3] == "alice:key1"  # dedup_key 仍然納入命名空間


def test_different_followers_isolated_dedup():
    """Test 4: 兩個不同 id 的 TaggedNotifier 共用同一 inner → 同 raw key 不互相去重。"""
    inner = RecordingNotifier()
    alice_notifier = TaggedNotifier(inner, "alice")
    bob_notifier = TaggedNotifier(inner, "bob")

    # 兩個不同 follower 用相同的 raw key
    alice_notifier.info("cat", "alice_msg", dedup_key="key1")
    bob_notifier.info("cat", "bob_msg", dedup_key="key1")

    # 兩則都應該記錄（命名空間隔離）
    assert len(inner.records) == 2
    assert inner.records[0][3] == "alice:key1"  # alice 的 key 在 alice 命名空間
    assert inner.records[1][3] == "bob:key1"    # bob 的 key 在 bob 命名空間
    # 即使 raw key 相同，但 dedup_key 不同（alice:key1 vs bob:key1）所以不去重
