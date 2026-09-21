"""tests/test_explore_store.py — `ExploreStore` SQLite 資料層（P2 Task 2.1）。

全離線；純資料層，不碰網路。DB 一律用 `tmp_path`（見 CLAUDE.md 紅線 6：測試全離線）。
"""
import dataclasses
import sqlite3
import time
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


def test_schema_version_v3_recorded_on_fresh_db(tmp_path):
    """Task 7.9b：schema bump 2→3（`fills_scan` 新表＋`fills_sync` 四個新欄）
    ——全新 DB 直接落地版本 3（`_SCHEMA` 已含新表／新欄，不需要跑遷移）。"""
    store, _ = _store(tmp_path)
    row = store._db.execute("SELECT version FROM schema_version").fetchone()
    assert row == (3,)
    cols = {r[1] for r in store._db.execute("PRAGMA table_info(fills_sync)").fetchall()}
    assert {"inc_from_ms", "scan_id", "evidence_unknown", "coverage_gap"} <= cols
    tables = {r[0] for r in store._db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "fills_scan" in tables
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


# --- set_sync_reason / params_fp / schema v1→v2 migration (Task 7.5 加) ---

# Task 7.9b：`set_sync_reason`（直接 UPDATE `fills_sync.reason`，無版本保護）
# 已被 `apply_probe_result` 的雙 CAS 取代（B4：探測回寫必須確認地址仍指向探測
# 當下的那個 `scan_id`，見 `test_apply_probe_result_cas_*` 系列），舊方法與
# 對應測試一併移除——這是本次架構重構的必要淘汰，不是為了讓測試通過而砍測試。


def test_insert_fills_page_round_trips_params_fp(tmp_path):
    store, _ = _store(tmp_path)
    store.insert_fills_page("0xabc", [], _checkpoint(
        params_fp="aggregateByTime=default(false)"))
    sync = store.get_sync("0xabc")
    assert sync.params_fp == "aggregateByTime=default(false)"


def test_fills_sync_state_params_fp_defaults_to_empty_string():
    """既有呼叫端／測試 fixture 不必逐一加 `params_fp` 就能繼續建構
    `FillsSyncState`（frozen dataclass，`params_fp` 是唯一有預設值、排在最後
    的欄位）。"""
    state = _checkpoint()
    assert state.params_fp == ""


def _write_v1_schema(db_path) -> None:
    """手刻一份 Task 7.5 之前的 v1 DB（`fills_sync` 無 `params_fp` 欄）："""
    raw = sqlite3.connect(str(db_path))
    raw.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (1);
        CREATE TABLE candidate (
          address TEXT PRIMARY KEY, display_name TEXT, source_rank INTEGER,
          source_roi REAL, source_as_of REAL NOT NULL,
          active INTEGER NOT NULL DEFAULT 1, last_seen_at REAL NOT NULL);
        CREATE TABLE endpoint_cache (
          address TEXT NOT NULL, endpoint TEXT NOT NULL,
          params_fp TEXT NOT NULL DEFAULT '', payload TEXT,
          fetched_at REAL, refresh_after REAL NOT NULL,
          last_error TEXT, last_error_at REAL,
          PRIMARY KEY (address, endpoint, params_fp));
        CREATE TABLE fills (
          address TEXT NOT NULL, coin TEXT NOT NULL, tid INTEGER NOT NULL,
          time_ms INTEGER NOT NULL, raw TEXT NOT NULL,
          PRIMARY KEY (address, coin, tid));
        CREATE TABLE fills_sync (
          address TEXT PRIMARY KEY,
          window_start_ms INTEGER NOT NULL, window_end_ms INTEGER NOT NULL,
          cursor_ms INTEGER NOT NULL, synced_through_ms INTEGER,
          observed_from_ms INTEGER, observed_to_ms INTEGER,
          completeness TEXT NOT NULL DEFAULT 'backfilling',
          reason TEXT, pages_done INTEGER NOT NULL DEFAULT 0,
          fills_in_window INTEGER NOT NULL DEFAULT 0,
          updated_at REAL NOT NULL, last_error TEXT);
        CREATE TABLE refresh_job (
          key TEXT PRIMARY KEY, address TEXT, kind TEXT NOT NULL,
          priority INTEGER NOT NULL, created_at REAL NOT NULL,
          next_attempt_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          lease_until REAL, lease_owner TEXT, fencing INTEGER NOT NULL DEFAULT 0,
          last_error TEXT);
    """)
    raw.execute(
        "INSERT INTO fills_sync (address, window_start_ms, window_end_ms, cursor_ms, "
        "synced_through_ms, completeness, reason, pages_done, fills_in_window, updated_at) "
        "VALUES ('0xabc', 0, 1000, 1000, 1000, 'complete', NULL, 1, 5, 1000.0)")
    raw.execute(
        "INSERT INTO fills_sync (address, window_start_ms, window_end_ms, cursor_ms, "
        "synced_through_ms, completeness, reason, pages_done, fills_in_window, updated_at) "
        "VALUES ('0xdef', 0, 1000, 500, NULL, 'backfilling', NULL, 0, 0, 1000.0)")
    raw.execute(
        "INSERT INTO fills_sync (address, window_start_ms, window_end_ms, cursor_ms, "
        "synced_through_ms, completeness, reason, pages_done, fills_in_window, updated_at) "
        "VALUES ('0xzzz', 0, 1000, 1000, 1000, 'partial', 'retention_limit', 20, 9000, 1000.0)")
    raw.execute(
        "INSERT INTO fills_sync (address, window_start_ms, window_end_ms, cursor_ms, "
        "synced_through_ms, completeness, reason, pages_done, fills_in_window, updated_at) "
        "VALUES ('0xver', 0, 1000, 1000, 1000, 'complete', 'retention_boundary_verified', "
        "1, 5, 1000.0)")
    raw.execute(
        "INSERT INTO refresh_job (key, address, kind, priority, created_at, next_attempt_at) "
        "VALUES ('0xdef:fills', '0xdef', 'fills', 2, 0.0, 500.0)")
    raw.commit()
    raw.close()


def test_migration_v1_to_v2_adds_params_fp_column_defaulted_empty(tmp_path):
    db_path = tmp_path / "explore.db"
    _write_v1_schema(db_path)

    store = ExploreStore(db_path)

    version = store._db.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == 3
    cols = {r[1] for r in store._db.execute("PRAGMA table_info(fills_sync)").fetchall()}
    assert "params_fp" in cols
    params_fp_values = {r[0] for r in store._db.execute(
        "SELECT params_fp FROM fills_sync").fetchall()}
    assert params_fp_values == {""}


def test_migration_v1_to_v2_backfills_reason_only_for_complete_null_rows(tmp_path):
    db_path = tmp_path / "explore.db"
    _write_v1_schema(db_path)

    store = ExploreStore(db_path)

    rows = {r[0]: r[1] for r in store._db.execute(
        "SELECT address, reason FROM fills_sync").fetchall()}
    # complete/reason=NULL → 補標。
    assert rows["0xabc"] == "count_below_retention_threshold"
    # backfilling/reason=NULL → 不補（尚未跑完一輪，沒有可歸因的判準）。
    assert rows["0xdef"] is None
    # partial/reason 原本就有值 → 不覆寫。
    assert rows["0xzzz"] == "retention_limit"

    remaining = store._db.execute(
        "SELECT COUNT(*) FROM fills_sync WHERE completeness='complete' AND reason IS NULL"
    ).fetchone()[0]
    assert remaining == 0


def test_migration_is_idempotent_on_reopen(tmp_path):
    db_path = tmp_path / "explore.db"
    _write_v1_schema(db_path)
    ExploreStore(db_path)
    # 第二次開啟（版本已是 3）不應該再嘗試 ALTER TABLE／CREATE TABLE（會因
    # 欄位／表已存在而炸掉，或重複建立歷史 fills_scan／verify job）。
    store2 = ExploreStore(db_path)
    version = store2._db.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == 3


# --- Task 7.9b：schema v2→v3 遷移（遍歷軌／增量軌分離） ---

def test_migration_v2_to_v3_backfilling_row_becomes_running_initial_scan_and_renames_job(
        tmp_path):
    db_path = tmp_path / "explore.db"
    _write_v1_schema(db_path)
    store = ExploreStore(db_path)

    sync = store.get_sync("0xdef")
    assert sync.completeness == "backfilling"
    assert sync.evidence_unknown is False
    assert sync.scan_id is None
    scan = store.get_active_scan("0xdef")
    assert scan is not None
    assert scan.kind == "initial"
    assert scan.window_start_ms == 0 and scan.window_end_ms == 1000
    assert scan.cursor_ms == 500  # 游標沿用舊列

    # 舊 `0xdef:fills` job 改名為 `fills_scan`；另有一筆全新的增量 `fills` job。
    row = store._db.execute(
        "SELECT kind FROM refresh_job WHERE key='0xdef:fills_scan'").fetchone()
    assert row is not None and row[0] == "fills_scan"
    row2 = store._db.execute(
        "SELECT kind FROM refresh_job WHERE key='0xdef:fills'").fetchone()
    assert row2 is not None and row2[0] == "fills"


def test_migration_v2_to_v3_verified_row_evidence_known_no_verify_job(tmp_path):
    db_path = tmp_path / "explore.db"
    _write_v1_schema(db_path)
    store = ExploreStore(db_path)

    sync = store.get_sync("0xver")
    assert sync.evidence_unknown is False
    assert sync.scan_id is not None
    scan = store.get_scan(sync.scan_id)
    assert scan.status == "done" and scan.result == "complete"
    assert scan.reason == "retention_boundary_verified"
    job = store._db.execute(
        "SELECT 1 FROM refresh_job WHERE address='0xver' AND kind='fills_verify'").fetchone()
    assert job is None


def test_migration_v2_to_v3_other_complete_row_evidence_unknown_schedules_verify_job(tmp_path):
    db_path = tmp_path / "explore.db"
    _write_v1_schema(db_path)
    now = time.time()
    store = ExploreStore(db_path, now_fn=lambda: now)

    sync = store.get_sync("0xabc")
    assert sync.evidence_unknown is True
    row = store._db.execute(
        "SELECT next_attempt_at FROM refresh_job WHERE address='0xabc' "
        "AND kind='fills_verify'").fetchone()
    assert row is not None
    assert now <= row[0] <= now + 48 * 3600


def test_migration_v2_to_v3_partial_row_also_gets_evidence_unknown_and_verify_job(tmp_path):
    db_path = tmp_path / "explore.db"
    _write_v1_schema(db_path)
    store = ExploreStore(db_path)

    sync = store.get_sync("0xzzz")
    assert sync.completeness == "partial"
    assert sync.evidence_unknown is True
    job = store._db.execute(
        "SELECT 1 FROM refresh_job WHERE address='0xzzz' AND kind='fills_verify'").fetchone()
    assert job is not None


def _write_v2_schema(db_path, *, window_start_ms=0, window_end_ms, cursor_ms) -> None:
    """手刻一份 Task 7.9b 之前的 v2 DB（含 `params_fp`，尚無遍歷軌新欄位）：
    單一 backfilling 列，`window_end_ms` 由呼叫端指定（用來重現「舊 scan 開輪
    時刻早於遷移當下」的缺口情境）。"""
    raw = sqlite3.connect(str(db_path))
    raw.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (2);
        CREATE TABLE candidate (
          address TEXT PRIMARY KEY, display_name TEXT, source_rank INTEGER,
          source_roi REAL, source_as_of REAL NOT NULL,
          active INTEGER NOT NULL DEFAULT 1, last_seen_at REAL NOT NULL);
        CREATE TABLE endpoint_cache (
          address TEXT NOT NULL, endpoint TEXT NOT NULL,
          params_fp TEXT NOT NULL DEFAULT '', payload TEXT,
          fetched_at REAL, refresh_after REAL NOT NULL,
          last_error TEXT, last_error_at REAL,
          PRIMARY KEY (address, endpoint, params_fp));
        CREATE TABLE fills (
          address TEXT NOT NULL, coin TEXT NOT NULL, tid INTEGER NOT NULL,
          time_ms INTEGER NOT NULL, raw TEXT NOT NULL,
          PRIMARY KEY (address, coin, tid));
        CREATE TABLE fills_sync (
          address TEXT PRIMARY KEY,
          window_start_ms INTEGER NOT NULL, window_end_ms INTEGER NOT NULL,
          cursor_ms INTEGER NOT NULL, synced_through_ms INTEGER,
          observed_from_ms INTEGER, observed_to_ms INTEGER,
          completeness TEXT NOT NULL DEFAULT 'backfilling',
          reason TEXT, pages_done INTEGER NOT NULL DEFAULT 0,
          fills_in_window INTEGER NOT NULL DEFAULT 0,
          params_fp TEXT NOT NULL DEFAULT '',
          updated_at REAL NOT NULL, last_error TEXT);
        CREATE TABLE refresh_job (
          key TEXT PRIMARY KEY, address TEXT, kind TEXT NOT NULL,
          priority INTEGER NOT NULL, created_at REAL NOT NULL,
          next_attempt_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          lease_until REAL, lease_owner TEXT, fencing INTEGER NOT NULL DEFAULT 0,
          last_error TEXT);
    """)
    raw.execute(
        "INSERT INTO fills_sync (address, window_start_ms, window_end_ms, cursor_ms, "
        "synced_through_ms, completeness, reason, pages_done, fills_in_window, params_fp, "
        "updated_at) VALUES ('0xgap', ?, ?, ?, NULL, 'backfilling', NULL, 2, 0, '', 1000.0)",
        (window_start_ms, window_end_ms, cursor_ms))
    raw.execute(
        "INSERT INTO refresh_job (key, address, kind, priority, created_at, next_attempt_at) "
        "VALUES ('0xgap:fills', '0xgap', 'fills', 2, 0.0, 500.0)")
    raw.commit()
    raw.close()


def test_migration_v2_to_v3_backfilling_row_inc_from_uses_scan_window_end_not_now(tmp_path):
    """主線程二次裁決（2026-09-21，正式機 287 列快照實跑抓到的缺口）：
    backfilling 列的增量軌起點不能設成遷移當下的 `now`——那會在舊 scan 的
    `window_end_ms`（幾小時前開輪時的值）與 `now` 之間留下一段沒有任何一軌
    會抓的成交，scan 完成時 `window_end < inc_from` 會被誤判 `coverage_gap`。
    修法：`inc_from_ms = synced_through_ms = cursor_ms = window_end_ms`
    （增量軌從 scan 窗口末端起算）。"""
    db_path = tmp_path / "explore.db"
    now = time.time()
    window_end_ms = int((now - 6 * 3600) * 1000)  # 6 小時前開的輪
    cursor_ms = window_end_ms - 500
    _write_v2_schema(db_path, window_end_ms=window_end_ms, cursor_ms=cursor_ms)
    store = ExploreStore(db_path, now_fn=lambda: now)

    sync = store.get_sync("0xgap")
    assert sync.inc_from_ms == window_end_ms
    assert sync.synced_through_ms == window_end_ms
    assert sync.cursor_ms == window_end_ms
    assert sync.window_end_ms == window_end_ms

    # 增量軌從 scan 窗口末端起算：`now` 立即開增量輪（1 小時週期遠小於 6
    # 小時已過的間隔），抓 `[window_end_ms, now]` 把缺口補上——`start_ms`
    # 沒有再減 1ms overlap，因為 `inc_from_ms` 本身就等於 `window_end_ms`，
    # `plan_incremental` 的 `max(inc_from_ms, baseline-overlap_ms)` 地板擋住
    # 了往回超過增量軌起點（見該函式）。
    from spark.publicapi.explore_fills_sync import plan_incremental
    plan = plan_incremental(sync, now_ms=int(now * 1000), period_s=3600)
    assert not plan.is_noop
    assert plan.start_ms == window_end_ms

    # scan 完成時窗口末端＝inc_from，不會被誤判 gap。
    scan = store.get_active_scan("0xgap")
    assert scan.window_end_ms == window_end_ms
    finished = dataclasses.replace(scan, cursor_ms=scan.window_end_ms, result="complete",
                                   reason="count_below_retention_threshold", finished_at=now)
    ok = store.complete_scan("0xgap", [], finished)
    assert ok is True
    assert store.get_sync("0xgap").coverage_gap is False


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


# --- Task 7.4b: claim_due(kinds=) / due_count / rebalance_overdue ---

def test_claim_due_kinds_filter_only_claims_listed_kinds(tmp_path):
    store, c = _store(tmp_path)
    store.enqueue("0xabc:fills", "0xabc", "fills", priority=2, next_attempt_at=c.now())
    store.enqueue("0xabc:state", "0xabc", "state", priority=0, next_attempt_at=c.now())

    job = store.claim_due(c.now(), "owner1", lease_s=60, kinds=("fills",))
    assert job is not None
    assert job.kind == "fills"
    # 只剩 state（fills 已被領走並上鎖）；再限定 kinds=("fills",) 應拿不到。
    assert store.claim_due(c.now(), "owner1", lease_s=60, kinds=("fills",)) is None
    job2 = store.claim_due(c.now(), "owner1", lease_s=60, kinds=("state",))
    assert job2 is not None
    assert job2.kind == "state"


def test_claim_due_without_kinds_still_works(tmp_path):
    """向後相容：不傳 `kinds` 行為與改動前相同。"""
    store, c = _store(tmp_path)
    store.enqueue("0xabc:state", "0xabc", "state", priority=0, next_attempt_at=c.now())
    job = store.claim_due(c.now(), "owner1", lease_s=60)
    assert job is not None
    assert job.kind == "state"


def test_claim_due_orders_by_priority_with_wait_time_escalation(tmp_path):
    """priority 3 等了 20 分鐘（1200s）→ 升 2 級（floor(1200/600)=2，夾 3）
    ＝有效 priority 1；priority 2 剛到期（wait=0）→ 有效 priority 2。
    等待較久的（priority 3、已升級）先被領。"""
    store, c = _store(tmp_path)
    store.enqueue("0xhot:fills", "0xhot", "fills", priority=2, next_attempt_at=c.now())
    store.enqueue("0xcold:fills", "0xcold", "fills", priority=3,
                 next_attempt_at=c.now() - 1200.0)

    job = store.claim_due(c.now(), "owner1", lease_s=60, kinds=("fills",))
    assert job is not None
    assert job.key == "0xcold:fills"


def test_due_count_counts_only_matching_kind_and_due(tmp_path):
    store, c = _store(tmp_path)
    store.enqueue("0xabc:fills", "0xabc", "fills", priority=2, next_attempt_at=c.now())
    store.enqueue("0xdef:fills", "0xdef", "fills", priority=2, next_attempt_at=c.now() + 100)
    store.enqueue("0xabc:state", "0xabc", "state", priority=0, next_attempt_at=c.now())

    assert store.due_count("fills", c.now()) == 1
    assert store.due_count("state", c.now()) == 1
    assert store.due_count("portfolio", c.now()) == 0


def test_rebalance_overdue_only_touches_overdue_rows_and_spreads_them(tmp_path):
    store, c = _store(tmp_path)
    # A 嚴重逾期（超過 period 4000s）；B 只是普通到期（未逾期 period）。
    store.enqueue("0xa:state", "0xa", "state", priority=0, next_attempt_at=c.now() - 4000.0)
    store.enqueue("0xb:state", "0xb", "state", priority=0, next_attempt_at=c.now())
    lease_before = store._db.execute(
        "SELECT lease_until, fencing FROM refresh_job WHERE key='0xb:state'").fetchone()

    n = store.rebalance_overdue("state", c.now(), 1800.0, lambda addr: 42.0)

    assert n == 1
    row_a = store._db.execute(
        "SELECT next_attempt_at FROM refresh_job WHERE key='0xa:state'").fetchone()
    assert row_a[0] == pytest.approx(c.now() + 42.0)
    row_b = store._db.execute(
        "SELECT next_attempt_at, lease_until, fencing FROM refresh_job "
        "WHERE key='0xb:state'").fetchone()
    assert row_b[0] == c.now()  # 未逾期者不動
    assert (row_b[1], row_b[2]) == lease_before  # lease/fencing 不受影響


# --- Task 7.9b：fills_scan（遍歷軌）CRUD／CAS ---

def test_bootstrap_address_fills_creates_fills_sync_and_initial_scan_once(tmp_path):
    store, c = _store(tmp_path)
    now_ms = int(c.now() * 1000)
    created = store.bootstrap_address_fills(
        "0xNEW", c.now(), window_start_ms=now_ms - 1000, window_end_ms=now_ms,
        params_fp="pfp")
    assert created is True
    sync = store.get_sync("0xnew")
    assert sync.completeness == "backfilling"
    assert sync.inc_from_ms == now_ms
    assert sync.synced_through_ms == now_ms
    assert sync.scan_id is None
    scan = store.get_active_scan("0xnew")
    assert scan is not None
    assert scan.kind == "initial"
    assert scan.window_start_ms == now_ms - 1000
    assert scan.window_end_ms == now_ms
    assert scan.params_fp == "pfp"

    # 已存在則不動、回 False。
    created2 = store.bootstrap_address_fills(
        "0xnew", c.now() + 100, window_start_ms=0, window_end_ms=0, params_fp="other")
    assert created2 is False
    assert store.get_sync("0xnew").inc_from_ms == now_ms  # 未被覆蓋


def test_create_scan_and_get_active_scan(tmp_path):
    store, c = _store(tmp_path)
    scan = store.create_scan("0xabc", kind="initial", window_start_ms=0, window_end_ms=1000,
                             cursor_ms=0, started_at=c.now(), params_fp="pfp")
    assert scan.status == "running"
    active = store.get_active_scan("0xabc")
    assert active == scan
    assert store.get_scan(scan.scan_id) == scan


def test_get_active_scan_none_when_no_running_scan(tmp_path):
    store, c = _store(tmp_path)
    assert store.get_active_scan("0xnope") is None


def test_insert_scan_page_updates_progress_not_fills_sync(tmp_path):
    store, c = _store(tmp_path)
    store.bootstrap_address_fills("0xabc", c.now(), window_start_ms=0, window_end_ms=1000,
                                  params_fp="pfp")
    scan = store.get_active_scan("0xabc")
    sync_before = store.get_sync("0xabc")
    updated_scan = dataclasses.replace(scan, cursor_ms=500, pages_done=1, fills_in_window=3)
    n = store.insert_scan_page("0xabc", [_fill(tid=1, time_ms=100)], updated_scan)
    assert n == 1
    reloaded = store.get_scan(scan.scan_id)
    assert reloaded.cursor_ms == 500
    assert reloaded.pages_done == 1
    # fills_sync（增量軌）完全不受影響。
    assert store.get_sync("0xabc") == sync_before


def test_complete_scan_cas_writes_back_to_fills_sync_with_gap_check(tmp_path):
    store, c = _store(tmp_path)
    now_ms = int(c.now() * 1000)
    # `window_end_ms` 與 `bootstrap_address_fills` 算出的 `inc_from_ms`
    # （＝呼叫當下的 `now_ms`）相等，滿足「scan.window_end_ms >= inc_from_ms」
    # → 無 gap（見下方斷言）。
    store.bootstrap_address_fills("0xabc", c.now(), window_start_ms=now_ms - 1000,
                                  window_end_ms=now_ms, params_fp="pfp")
    scan = store.get_active_scan("0xabc")
    finished = dataclasses.replace(scan, cursor_ms=now_ms, result="complete",
                                   reason="count_below_retention_threshold",
                                   finished_at=c.now() + 5)
    ok = store.complete_scan("0xabc", [], finished)
    assert ok is True
    sync = store.get_sync("0xabc")
    assert sync.completeness == "complete"
    assert sync.reason == "count_below_retention_threshold"
    assert sync.scan_id == scan.scan_id
    assert sync.evidence_unknown is False
    assert sync.coverage_gap is False


def test_complete_scan_detects_gap_when_scan_window_end_before_inc_from(tmp_path):
    store, c = _store(tmp_path)
    store.bootstrap_address_fills("0xabc", c.now(), window_start_ms=0, window_end_ms=1000,
                                  params_fp="pfp")
    # inc_from_ms 目前 = int(c.now()*1000)（bootstrap 當下）；建一個 window_end
    # 早於它的 scan，模擬「遍歷完成時窗口沒有伸進增量軌起點」。
    inc_from_ms = store.get_sync("0xabc").inc_from_ms
    scan = store.create_scan("0xabc", kind="partial_rescan", window_start_ms=0,
                             window_end_ms=inc_from_ms - 1, cursor_ms=0, started_at=c.now())
    finished = dataclasses.replace(scan, cursor_ms=inc_from_ms - 1, result="complete",
                                   reason="count_below_retention_threshold",
                                   finished_at=c.now())
    ok = store.complete_scan("0xabc", [], finished)
    assert ok is True
    sync = store.get_sync("0xabc")
    assert sync.coverage_gap is True


def test_apply_probe_result_cas_hits_when_reason_and_scan_id_match(tmp_path):
    store, c = _store(tmp_path)
    store.bootstrap_address_fills("0xabc", c.now(), window_start_ms=0, window_end_ms=1000,
                                  params_fp="pfp")
    scan = store.get_active_scan("0xabc")
    finished = dataclasses.replace(scan, cursor_ms=1000, result="complete",
                                   reason="count_below_retention_threshold",
                                   finished_at=c.now())
    store.complete_scan("0xabc", [], finished)
    ok = store.apply_probe_result(scan.scan_id, "0xabc",
                                  old_reason="count_below_retention_threshold",
                                  new_reason="retention_boundary_verified")
    assert ok is True
    assert store.get_sync("0xabc").reason == "retention_boundary_verified"
    assert store.get_scan(scan.scan_id).reason == "retention_boundary_verified"


def test_apply_probe_result_cas_misses_when_a_new_scan_already_completed(tmp_path):
    """Task 7.9b B7 (ii)：探測發出後、回寫前，該地址已完成新的一次遍歷
    （`fills_sync.scan_id` 指向新 scan）→ CAS 落空，舊探測不覆蓋新遍歷。"""
    store, c = _store(tmp_path)
    store.bootstrap_address_fills("0xabc", c.now(), window_start_ms=0, window_end_ms=1000,
                                  params_fp="pfp")
    old_scan = store.get_active_scan("0xabc")
    old_finished = dataclasses.replace(old_scan, cursor_ms=1000, result="complete",
                                       reason="count_below_retention_threshold",
                                       finished_at=c.now())
    store.complete_scan("0xabc", [], old_finished)

    # 新的一次遍歷完成，`fills_sync.scan_id` 改指向新 scan。
    new_scan = store.create_scan("0xabc", kind="partial_rescan", window_start_ms=0,
                                 window_end_ms=2000, cursor_ms=0, started_at=c.now())
    new_finished = dataclasses.replace(new_scan, cursor_ms=2000, result="partial",
                                       reason="retention_limit", finished_at=c.now() + 1)
    store.complete_scan("0xabc", [], new_finished)

    ok = store.apply_probe_result(old_scan.scan_id, "0xabc",
                                  old_reason="count_below_retention_threshold",
                                  new_reason="retention_boundary_verified")
    assert ok is False
    # 新遍歷的結論不被覆蓋。
    assert store.get_sync("0xabc").reason == "retention_limit"
    assert store.get_scan(old_scan.scan_id).reason == "count_below_retention_threshold"


def test_next_probe_candidate_picks_oldest_finished_and_excludes_evidence_unknown(tmp_path):
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xaaa", None, 1, None), ("0xbbb", None, 2, None)], as_of=c.now())
    for addr, finished_at in (("0xaaa", 100.0), ("0xbbb", 50.0)):
        store.bootstrap_address_fills(addr, c.now(), window_start_ms=0, window_end_ms=1000,
                                      params_fp="pfp")
        scan = store.get_active_scan(addr)
        finished = dataclasses.replace(scan, cursor_ms=1000, result="complete",
                                       reason="count_below_retention_threshold",
                                       finished_at=finished_at)
        store.complete_scan(addr, [], finished)

    candidate = store.next_probe_candidate()
    assert candidate is not None
    assert candidate[0] == "0xbbb"  # finished_at 較舊者優先
    assert store.count_probe_candidates() == 2


def test_next_probe_candidate_excludes_partial_result_and_verified_reason(tmp_path):
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xaaa", None, 1, None), ("0xbbb", None, 2, None)], as_of=c.now())
    # 0xaaa：result=partial（不合法候選——只有 complete 才算門檻推論成立）。
    store.bootstrap_address_fills("0xaaa", c.now(), window_start_ms=0, window_end_ms=1000,
                                  params_fp="pfp")
    scan_a = store.get_active_scan("0xaaa")
    finished_a = dataclasses.replace(scan_a, cursor_ms=1000, result="partial",
                                     reason="retention_limit", finished_at=1.0)
    store.complete_scan("0xaaa", [], finished_a)
    # 0xbbb：已升級為 retention_boundary_verified（不再是候選）。
    store.bootstrap_address_fills("0xbbb", c.now(), window_start_ms=0, window_end_ms=1000,
                                  params_fp="pfp")
    scan_b = store.get_active_scan("0xbbb")
    finished_b = dataclasses.replace(scan_b, cursor_ms=1000, result="complete",
                                     reason="retention_boundary_verified", finished_at=1.0)
    store.complete_scan("0xbbb", [], finished_b)

    assert store.next_probe_candidate() is None
    assert store.count_probe_candidates() == 0


def test_next_probe_candidate_none_when_no_candidates(tmp_path):
    store, c = _store(tmp_path)
    assert store.next_probe_candidate() is None
    assert store.count_probe_candidates() == 0


def test_get_scan_returns_none_for_unknown_scan_id(tmp_path):
    store, c = _store(tmp_path)
    assert store.get_scan("nope") is None


def test_purge_deletes_fills_scan_rows_for_stale_candidate(tmp_path):
    """Task 7.9b：`purge` 清掉退池滿保留期的候選時，連帶清掉它的
    `fills_scan`（遍歷軌歷史）——不然孤兒列會一直留在資料庫裡（`fills_sync`
    本身已有等價清理，`fills_scan` 是新表，需要同一份保護）。"""
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xggg", "Gigi", 1, 0.1)], as_of=c.now())
    store.bootstrap_address_fills("0xggg", c.now(), window_start_ms=0, window_end_ms=1000,
                                  params_fp="pfp")
    store.deactivate_missing(set())
    c.t += 30 * 86400
    store.purge(c.now(), candidate_keep_s=7 * 86400)
    assert store._db.execute(
        "SELECT COUNT(*) FROM fills_scan WHERE address='0xggg'").fetchone()[0] == 0


def test_set_scan_error_updates_last_error_only(tmp_path):
    store, c = _store(tmp_path)
    scan = store.create_scan("0xabc", kind="initial", window_start_ms=0, window_end_ms=1000,
                             cursor_ms=0, started_at=c.now())
    store.set_scan_error(scan.scan_id, "boom")
    reloaded = store.get_scan(scan.scan_id)
    assert reloaded.last_error == "boom"
    assert reloaded.status == "running"


def test_rebalance_overdue_spreads_many_jobs_not_all_same_time(tmp_path):
    store, c = _store(tmp_path)
    for i in range(20):
        addr = f"0x{i:040x}"
        store.enqueue(f"{addr}:state", addr, "state", priority=0,
                      next_attempt_at=c.now() - 4000.0)

    from spark.publicapi.explore_scheduler import _spread
    n = store.rebalance_overdue("state", c.now(), 1800.0, lambda a: _spread(a, 1800.0))

    assert n == 20
    rows = store._db.execute(
        "SELECT next_attempt_at FROM refresh_job WHERE kind='state'").fetchall()
    times = {r[0] for r in rows}
    assert len(times) > 1
    for (t,) in rows:
        assert c.now() <= t <= c.now() + 1800.0
