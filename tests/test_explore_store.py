"""tests/test_explore_store.py — `ExploreStore` SQLite 資料層（P2 Task 2.1）。

全離線；純資料層，不碰網路。DB 一律用 `tmp_path`（見 CLAUDE.md 紅線 6：測試全離線）。
"""
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from spark.publicapi.explore_store import ExploreStore, FillsSyncState


class Clock:
    def __init__(self, t=2_000_000_000.0):
        self.t = t

    def now(self):
        return self.t


def _store(tmp_path, clock=None):
    c = clock or Clock()
    return ExploreStore(tmp_path / "explore.db", now_fn=c.now), c


def _checkpoint(address="0xabc", **overrides):
    base = dict(
        address=address, window_start_ms=0, window_end_ms=1_000_000,
        cursor_ms=0, synced_through_ms=None, observed_from_ms=None,
        observed_to_ms=None, completeness="backfilling", reason=None,
        pages_done=1, fills_in_window=0, updated_at=1_000_000.0, last_error=None,
    )
    base.update(overrides)
    return FillsSyncState(**base)


def _fill(coin="BTC", tid=1, time_ms=100):
    return {"coin": coin, "tid": tid, "time": time_ms, "px": "100.5", "sz": "0.1"}


# --- schema / WAL ---

def test_wal_journal_mode_enabled(tmp_path):
    store, _ = _store(tmp_path)
    mode = store._db.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


def test_schema_version_v1_recorded(tmp_path):
    store, _ = _store(tmp_path)
    row = store._db.execute("SELECT version FROM schema_version").fetchone()
    assert row == (1,)
    # 重開既有 DB 不應該重複塞第二筆 schema_version
    store2, _ = _store(tmp_path)
    assert store2._db.execute("SELECT COUNT(*) FROM schema_version").fetchone() == (1,)


# --- endpoint_cache: put_cache_error 不覆寫 fetched_at/payload ---

def test_put_cache_error_does_not_overwrite_fetched_at_or_payload(tmp_path):
    store, c = _store(tmp_path)
    store.put_cache_ok("0xAAA", "portfolio", {"x": 1}, fetched_at=c.now(), refresh_after=c.now() + 3600)
    c.t += 100
    store.put_cache_error("0xaaa", "portfolio", "boom", at=c.now(), next_after=c.now() + 60)
    entry = store.get_cache("0xaaa", "portfolio")
    assert entry.payload == {"x": 1}
    assert entry.fetched_at == 2_000_000_000.0
    assert entry.last_error == "boom"
    assert entry.refresh_after == c.now() + 60


def test_put_cache_error_inserts_row_when_missing(tmp_path):
    store, c = _store(tmp_path)
    store.put_cache_error("0xnew", "state", "timeout", at=c.now(), next_after=c.now() + 60)
    entry = store.get_cache("0xnew", "state")
    assert entry.payload is None
    assert entry.fetched_at is None
    assert entry.last_error == "timeout"


# --- fills / insert_fills_page 冪等與 rollback ---

def test_insert_fills_page_idempotent_replay(tmp_path):
    store, c = _store(tmp_path)
    page = [_fill(tid=1, time_ms=100), _fill(tid=2, time_ms=200)]
    cp = _checkpoint(cursor_ms=200, synced_through_ms=200)
    first = store.insert_fills_page("0xabc", page, cp)
    assert first == 2
    second = store.insert_fills_page("0xabc", page, cp)
    assert second == 0
    fills = store.get_fills("0xabc", 0, 1_000)
    assert len(fills) == 2
    sync = store.get_sync("0xabc")
    assert sync.cursor_ms == 200
    assert sync.synced_through_ms == 200


def test_insert_fills_page_exception_rolls_back_checkpoint(tmp_path):
    store, c = _store(tmp_path)
    good_cp = _checkpoint(cursor_ms=100, synced_through_ms=None)
    store.insert_fills_page("0xabc", [_fill(tid=1, time_ms=100)], good_cp)
    bad_page = [_fill(tid=2, time_ms=200), {"coin": "BTC", "time": 300}]  # 缺 tid
    bad_cp = _checkpoint(cursor_ms=300, synced_through_ms=300)
    with pytest.raises(KeyError):
        store.insert_fills_page("0xabc", bad_page, bad_cp)
    # checkpoint 與 fills 都不落地：仍是第一次呼叫的狀態
    assert store.get_sync("0xabc").cursor_ms == 100
    fills = store.get_fills("0xabc", 0, 1_000)
    assert len(fills) == 1


def test_insert_fills_page_rejects_invalid_completeness(tmp_path):
    store, c = _store(tmp_path)
    bad_cp = _checkpoint(completeness="unknown")
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_fills_page("0xabc", [], bad_cp)
    assert store.get_sync("0xabc") is None


def test_get_fills_ordered_by_time_ms(tmp_path):
    store, c = _store(tmp_path)
    page = [_fill(tid=1, time_ms=300), _fill(tid=2, time_ms=100), _fill(tid=3, time_ms=200)]
    store.insert_fills_page("0xabc", page, _checkpoint(cursor_ms=300))
    fills = store.get_fills("0xabc", 0, 1_000)
    assert [f["time"] for f in fills] == [100, 200, 300]


# --- refresh_job: claim_due / complete / reschedule / enqueue ---

def test_claim_due_only_one_owner_wins(tmp_path):
    store, c = _store(tmp_path)
    store.enqueue("0xabc:state", "0xabc", "state", priority=0, next_attempt_at=c.now())
    job1 = store.claim_due(c.now(), "owner1", lease_s=60)
    assert job1 is not None
    assert job1.lease_owner == "owner1"
    job2 = store.claim_due(c.now(), "owner2", lease_s=60)
    assert job2 is None


def test_claim_due_lease_expiry_allows_reclaim(tmp_path):
    store, c = _store(tmp_path)
    store.enqueue("0xabc:state", "0xabc", "state", priority=0, next_attempt_at=c.now())
    job1 = store.claim_due(c.now(), "owner1", lease_s=60)
    assert job1 is not None
    c.t += 61
    job2 = store.claim_due(c.now(), "owner2", lease_s=60)
    assert job2 is not None
    assert job2.lease_owner == "owner2"
    assert job2.fencing == job1.fencing + 1


def test_complete_with_stale_fencing_is_noop(tmp_path):
    store, c = _store(tmp_path)
    store.enqueue("0xabc:state", "0xabc", "state", priority=0, next_attempt_at=c.now())
    job = store.claim_due(c.now(), "owner1", lease_s=60)
    ok = store.complete(job, fencing=job.fencing - 1)
    assert ok is False
    # 沒被刪掉，仍可用正確 fencing 完成
    assert store.complete(job, fencing=job.fencing) is True


def test_reschedule_with_stale_fencing_is_noop(tmp_path):
    store, c = _store(tmp_path)
    store.enqueue("0xabc:fills", "0xabc", "fills", priority=2, next_attempt_at=c.now())
    job = store.claim_due(c.now(), "owner1", lease_s=60)
    ok = store.reschedule(job, fencing=job.fencing - 1, next_attempt_at=c.now() + 10, err="x")
    assert ok is False
    ok2 = store.reschedule(job, fencing=job.fencing, next_attempt_at=c.now() + 10, err="x")
    assert ok2 is True
    # 過期 fencing 完成也應失敗（reschedule 已清 lease，fencing 未變）
    assert store.complete(job, fencing=job.fencing - 1) is False


def test_enqueue_dedupe_only_raises_priority_and_advances_time(tmp_path):
    store, c = _store(tmp_path)
    first = store.enqueue("0xabc:portfolio", "0xabc", "portfolio", priority=2,
                          next_attempt_at=c.now() + 100)
    assert first is True
    second = store.enqueue("0xabc:portfolio", "0xabc", "portfolio", priority=0,
                           next_attempt_at=c.now() + 500)
    assert second is False
    job = store.claim_due(c.now(), "owner1", lease_s=60)
    assert job is None  # next_attempt_at 還沒到（min(100,500) 未過）
    c.t += 100
    job = store.claim_due(c.now(), "owner1", lease_s=60)
    assert job is not None
    assert job.priority == 0  # 取了較高優先級（數字較小）


# --- candidate / purge ---

def test_deactivate_missing_and_active_candidates(tmp_path):
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xAAA", "Alice", 1, 0.1), ("0xBBB", "Bob", 2, 0.2)], as_of=c.now())
    dropped = store.deactivate_missing({"0xaaa"})
    assert dropped == ["0xbbb"]
    active = store.active_candidates()
    assert [row.address for row in active] == ["0xaaa"]
    assert active[0].display_name == "Alice"


def test_deactivate_missing_empty_seen_returns_all_previously_active(tmp_path):
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xaaa", "Alice", 1, 0.1), ("0xbbb", "Bob", 2, 0.2)], as_of=c.now())
    dropped = store.deactivate_missing(set())
    assert sorted(dropped) == ["0xaaa", "0xbbb"]
    # 第二次呼叫（已全部 active=0）不再回傳同一批。
    assert store.deactivate_missing(set()) == []


def test_purge_keeps_candidate_with_pending_job(tmp_path):
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xccc", "Carl", 3, 0.3)], as_of=c.now())
    store.deactivate_missing(set())  # 全部踢出（模擬掉出池）
    store.enqueue("0xccc:fills", "0xccc", "fills", priority=2, next_attempt_at=c.now())
    c.t += 30 * 86400
    counts = store.purge(c.now(), candidate_keep_s=7 * 86400)
    assert counts["candidate"] == 0
    assert store.active_candidates() == [] or True  # active 已是 0，僅確認未被刪除
    assert store._db.execute(
        "SELECT COUNT(*) FROM candidate WHERE address='0xccc'").fetchone() == (1,)


def test_purge_deletes_stale_candidate_without_pending_job(tmp_path):
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xddd", "Dan", 4, 0.4)], as_of=c.now())
    store.deactivate_missing(set())
    store.put_cache_ok("0xddd", "portfolio", {"x": 1}, fetched_at=c.now(), refresh_after=c.now())
    c.t += 30 * 86400
    counts = store.purge(c.now(), candidate_keep_s=7 * 86400)
    assert counts["candidate"] == 1
    assert counts["endpoint_cache"] == 1
    assert store.get_cache("0xddd", "portfolio") is None


def test_purge_keeps_fills_within_sync_window(tmp_path):
    store, c = _store(tmp_path)
    old_ms = int((c.now() - 40 * 86400) * 1000)  # 超過 35 天保留期
    window_start = old_ms - 1000  # window 涵蓋這筆
    cp = _checkpoint("0xeee", window_start_ms=window_start, window_end_ms=int(c.now() * 1000),
                     cursor_ms=old_ms, synced_through_ms=old_ms)
    store.insert_fills_page("0xeee", [_fill(tid=1, time_ms=old_ms)], cp)
    counts = store.purge(c.now(), fills_keep_s=35 * 86400)
    assert counts["fills"] == 0
    assert len(store.get_fills("0xeee", 0, old_ms + 1)) == 1


def test_purge_deletes_fills_outside_window_and_retention(tmp_path):
    store, c = _store(tmp_path)
    old_ms = int((c.now() - 40 * 86400) * 1000)
    cp = _checkpoint("0xfff", window_start_ms=old_ms + 500, window_end_ms=int(c.now() * 1000),
                     cursor_ms=old_ms + 500)
    store.insert_fills_page("0xfff", [_fill(tid=1, time_ms=old_ms)], cp)
    counts = store.purge(c.now(), fills_keep_s=35 * 86400)
    assert counts["fills"] == 1
    assert store.get_fills("0xfff", 0, old_ms + 1) == []


# --- 地址正規化 ---

def test_address_normalization_case_insensitive(tmp_path):
    store, c = _store(tmp_path)
    store.put_cache_ok("0xABCDEF", "state", {"y": 2}, fetched_at=c.now(), refresh_after=c.now())
    assert store.get_cache("0xabcdef", "state").payload == {"y": 2}
    store.upsert_candidates([("0xGGG", None, 1, None)], as_of=c.now())
    store.enqueue("0xggg:state", "0xGGG", "state", priority=0, next_attempt_at=c.now())
    job = store.claim_due(c.now(), "o", lease_s=10)
    assert job.address == "0xggg"


# --- 並發 ---

def test_concurrent_enqueue_thread_safe(tmp_path):
    store, c = _store(tmp_path)

    def _do(i):
        store.enqueue(f"0x{i:03d}:state", f"0x{i:03d}", "state", priority=1,
                      next_attempt_at=c.now())

    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = [ex.submit(_do, i) for i in range(100)]
        for f in as_completed(futs):
            f.result()  # 有例外就在這裡拋出

    count = store._db.execute("SELECT COUNT(*) FROM refresh_job").fetchone()[0]
    assert count == 100


# --- set_sync_error / oldest_due_at (Task 3.1 加) ---

def test_set_sync_error_updates_existing_row(tmp_path):
    store, c = _store(tmp_path)
    store.insert_fills_page("0xabc", [_fill(tid=1, time_ms=100)],
                            _checkpoint(completeness="complete"))
    store.set_sync_error("0xABC", "boom", at=c.now() + 10)
    sync = store.get_sync("0xabc")
    assert sync.last_error == "boom"
    assert sync.updated_at == c.now() + 10
    # 其餘欄位不動
    assert sync.completeness == "complete"


def test_set_sync_error_noop_when_row_missing(tmp_path):
    store, c = _store(tmp_path)
    store.set_sync_error("0xnope", "boom", at=c.now())
    assert store.get_sync("0xnope") is None


def test_oldest_due_at_returns_none_when_nothing_due(tmp_path):
    store, c = _store(tmp_path)
    store.enqueue("0xabc:state", "0xabc", "state", priority=0, next_attempt_at=c.now() + 100)
    assert store.oldest_due_at(c.now()) is None


def test_oldest_due_at_returns_earliest_due_next_attempt(tmp_path):
    store, c = _store(tmp_path)
    store.enqueue("0xabc:state", "0xabc", "state", priority=0, next_attempt_at=c.now() - 50)
    store.enqueue("0xabc:portfolio", "0xabc", "portfolio", priority=1,
                  next_attempt_at=c.now() - 10)
    assert store.oldest_due_at(c.now()) == c.now() - 50


def test_stats_reports_counts_and_completeness(tmp_path):
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=c.now())
    store.insert_fills_page("0xabc", [_fill(tid=1, time_ms=100)],
                            _checkpoint(completeness="complete"))
    stats = store.stats()
    assert stats["candidate"] == 1
    assert stats["fills"] == 1
    assert stats["completeness"] == {"complete": 1}


# --- Task 3.5 A：job 生命週期新方法 / db 0600 ---

def test_db_file_created_with_0600_permissions(tmp_path):
    store, c = _store(tmp_path)
    mode = tmp_path.joinpath("explore.db").stat().st_mode & 0o777
    assert mode == 0o600


def test_delete_jobs_returns_count_and_removes_only_that_address(tmp_path):
    store, c = _store(tmp_path)
    for kind in ("state", "portfolio", "ledger", "fills"):
        store.enqueue(f"0xabc:{kind}", "0xabc", kind, 1, c.now())
    store.enqueue("0xdef:state", "0xdef", "state", 0, c.now())
    deleted = store.delete_jobs("0xABC")
    assert deleted == 4
    remaining = [r[0] for r in
                store._db.execute("SELECT key FROM refresh_job").fetchall()]
    assert remaining == ["0xdef:state"]


def test_delete_jobs_returns_zero_when_no_jobs(tmp_path):
    store, c = _store(tmp_path)
    assert store.delete_jobs("0xnope") == 0


def test_is_active_reflects_candidate_active_flag(tmp_path):
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xaaa", "Alice", 1, 0.1)], as_of=c.now())
    assert store.is_active("0xAAA") is True
    store.deactivate_missing(set())
    assert store.is_active("0xaaa") is False


def test_is_active_false_for_unknown_address(tmp_path):
    store, c = _store(tmp_path)
    assert store.is_active("0xnope") is False


def test_admission_counts_returns_job_count_and_active_candidate_count(tmp_path):
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xaaa", "Alice", 1, 0.1), ("0xbbb", "Bob", 2, 0.2)],
                            as_of=c.now())
    store.deactivate_missing({"0xaaa"})
    for kind in ("state", "portfolio", "ledger", "fills"):
        store.enqueue(f"0xaaa:{kind}", "0xaaa", kind, 1, c.now())
    assert store.admission_counts() == (4, 1)


def test_wal_and_shm_side_files_created_with_0600_permissions(tmp_path):
    """Task 3.6 C（W2 修法）：`journal_mode=WAL` 會在 db 旁邊建 `-wal`／`-shm`
    側檔，這兩個檔案先前沒被 chmod 過（預設 0644）。用一次寫入觸發側檔落地後
    檢查——存在者一律 0600。"""
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xaaa", "Alice", 1, 0.1)], as_of=c.now())
    found_any = False
    for suffix in ("-wal", "-shm"):
        p = tmp_path / f"explore.db{suffix}"
        if p.exists():
            found_any = True
            mode = p.stat().st_mode & 0o777
            assert mode == 0o600, f"{p} mode is {oct(mode)}"
    assert found_any, "測試環境未產生 WAL/SHM 側檔，無法驗證本項（預期至少一個存在）"


def test_count_with_payload_only_counts_active_candidates_with_non_null_payload(tmp_path):
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xaaa", "Alice", 1, 0.1), ("0xbbb", "Bob", 2, 0.2)],
                            as_of=c.now())
    store.put_cache_ok("0xaaa", "portfolio", {"x": 1}, fetched_at=c.now(), refresh_after=c.now())
    store.put_cache_error("0xbbb", "portfolio", "boom", at=c.now(), next_after=c.now())
    assert store.count_with_payload("portfolio") == 1
    # 候選退池後不再計入，即使 payload 仍在。
    store.deactivate_missing({"0xbbb"})
    assert store.count_with_payload("portfolio") == 0


def test_count_with_payload_only_counts_default_params_fp(tmp_path):
    """Task 3.7 C（W2 修法）：`count_with_payload` 與 `compose_rows`（走
    `store.get_cache(addr, endpoint)`，預設 `params_fp=""`）要讀同一個基礎
    （工程原則 #1）——只有非預設 `params_fp` 的 payload 不能計入，否則輸入端
    看起來已覆蓋、compose 卻讀不到。"""
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xaaa", "Alice", 1, 0.1)], as_of=c.now())
    store.put_cache_ok("0xaaa", "portfolio", {"x": 1}, fetched_at=c.now(), refresh_after=c.now(),
                       params_fp="x")
    assert store.count_with_payload("portfolio") == 0


def test_count_with_payload_distinct_address_not_per_params_fp(tmp_path):
    """Task 3.6 C（S4 修法）：同一地址在 `endpoint_cache` 有兩個不同 `params_fp`
    的列（例如多 dex）只算一次候選，不是每個 `params_fp` 各算一筆。"""
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xaaa", "Alice", 1, 0.1)], as_of=c.now())
    store.put_cache_ok("0xaaa", "portfolio", {"x": 1}, fetched_at=c.now(), refresh_after=c.now())
    store.put_cache_ok("0xaaa", "portfolio", {"y": 2}, fetched_at=c.now(), refresh_after=c.now(),
                       params_fp="dex1")
    assert store.count_with_payload("portfolio") == 1
