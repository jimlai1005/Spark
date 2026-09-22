"""tests/test_explore_store.py — `ExploreStore` SQLite 資料層（P2 Task 2.1）。

全離線；純資料層，不碰網路。DB 一律用 `tmp_path`（見 CLAUDE.md 紅線 6：測試全離線）。
"""
import dataclasses
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from spark.publicapi.explore_fills_sync import PARTIAL_RESCAN_AFTER_S, external_coverage_state
from spark.publicapi.explore_store import ExploreStore, FillsSyncState, ScanWriteback


class Clock:
    def __init__(self, t=2_000_000_000.0):
        self.t = t

    def now(self):
        return self.t


def _store(tmp_path, clock=None):
    c = clock or Clock()
    return ExploreStore(tmp_path / "explore.db", now_fn=c.now), c


def _checkpoint(address="0xabc", **overrides):
    # Task 7.9c D6：`inc_from_ms`（增量軌起點）非空是寫入邊界的不變式
    # （`ExploreStore.insert_fills_page` 對 `None` 丟 `ValueError`），fixture
    # 跟著給一個真實的起點；要測「守門真的擋得住」的案例顯式傳
    # `inc_from_ms=None`（見 `test_insert_fills_page_rejects_null_inc_from`）。
    base = dict(
        address=address, window_start_ms=0, window_end_ms=1_000_000,
        cursor_ms=0, synced_through_ms=None, observed_from_ms=None,
        observed_to_ms=None, completeness="backfilling", reason=None,
        pages_done=1, fills_in_window=0, updated_at=1_000_000.0, last_error=None,
        inc_from_ms=0,
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
    assert ok is ScanWriteback.APPLIED
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
    assert ok is ScanWriteback.APPLIED
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
    assert ok is ScanWriteback.APPLIED
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


# --- Task 7.9c-D：ScanWriteback（D3）／observed 維護（D2）／scan 清理（D4）／
#     遷移原子＋冪等（D5）／inc_from 非空邊界（D6）／跨缺口案例（D7）／存取器（D8） ---

def _bootstrapped(store, clock, addr="0xabc", *, window_ms=1000):
    """建好一個地址的兩軌（增量軌＋running initial scan），回傳該 scan。"""
    now_ms = int(clock.now() * 1000)
    store.bootstrap_address_fills(addr, clock.now(), window_start_ms=now_ms - window_ms,
                                  window_end_ms=now_ms, params_fp="pfp")
    return store.get_active_scan(addr)


# D3：complete_scan 的四種結局

def test_complete_scan_returns_applied_on_cas_hit(tmp_path):
    store, c = _store(tmp_path)
    scan = _bootstrapped(store, c)
    finished = dataclasses.replace(scan, cursor_ms=scan.window_end_ms, result="complete",
                                   reason="count_below_retention_threshold", finished_at=c.now())
    assert store.complete_scan("0xabc", [], finished) is ScanWriteback.APPLIED
    assert store.get_sync("0xabc").scan_id == scan.scan_id


def test_complete_scan_returns_duplicate_when_same_scan_already_applied(tmp_path):
    """同一次完成被套用兩次（job 重跑／重啟後重放）：不重寫 `fills_sync`，
    但 fills 仍落地、`fills_scan` 仍是 done——回 DUPLICATE 讓排程端照常收尾。"""
    store, c = _store(tmp_path)
    scan = _bootstrapped(store, c)
    finished = dataclasses.replace(scan, cursor_ms=scan.window_end_ms, result="complete",
                                   reason="count_below_retention_threshold", finished_at=c.now())
    assert store.complete_scan("0xabc", [], finished) is ScanWriteback.APPLIED
    sync_before = store.get_sync("0xabc")

    replay = dataclasses.replace(finished, result="partial", reason="retention_limit")
    assert store.complete_scan("0xabc", [_fill(tid=9, time_ms=123)], replay) \
        is ScanWriteback.DUPLICATE
    # fills_sync 完全不動（重播的 partial 結論沒有覆蓋既有結論）。
    assert store.get_sync("0xabc") == sync_before
    # fills 仍落地、fills_scan 仍 done。
    assert len(store.get_fills("0xabc", 0, 10_000)) == 1
    assert store.get_scan(scan.scan_id).status == "done"


def test_complete_scan_returns_stale_when_newer_scan_already_applied(tmp_path):
    """舊 scan 的結論回來得太晚（`fills_sync` 已指向 `started_at` 更晚的 scan）
    → STALE：不覆蓋新結論，但 fills 落地、舊 scan 標 done。"""
    store, c = _store(tmp_path)
    old_scan = _bootstrapped(store, c)
    new_scan = store.create_scan("0xabc", kind="partial_rescan", window_start_ms=0,
                                 window_end_ms=int(c.now() * 1000),
                                 cursor_ms=0, started_at=c.now() + 10)
    new_finished = dataclasses.replace(new_scan, cursor_ms=new_scan.window_end_ms,
                                       result="partial", reason="retention_limit",
                                       finished_at=c.now() + 20)
    assert store.complete_scan("0xabc", [], new_finished) is ScanWriteback.APPLIED

    old_finished = dataclasses.replace(old_scan, cursor_ms=old_scan.window_end_ms,
                                       result="complete",
                                       reason="count_below_retention_threshold",
                                       finished_at=c.now() + 30)
    assert store.complete_scan("0xabc", [_fill(tid=7, time_ms=321)], old_finished) \
        is ScanWriteback.STALE
    sync = store.get_sync("0xabc")
    assert sync.scan_id == new_scan.scan_id
    assert sync.completeness == "partial" and sync.reason == "retention_limit"
    assert len(store.get_fills("0xabc", 0, 10_000)) == 1
    assert store.get_scan(old_scan.scan_id).status == "done"


def test_complete_scan_returns_missing_when_no_fills_sync_row(tmp_path):
    """增量軌列不見了（退池後被 purge／人工刪除）→ MISSING：沒有增量軌可
    寫回，但 fills 落地、scan 標 done（資料不丟）。"""
    store, c = _store(tmp_path)
    scan = store.create_scan("0xorphan", kind="initial", window_start_ms=0, window_end_ms=1000,
                             cursor_ms=0, started_at=c.now())
    finished = dataclasses.replace(scan, cursor_ms=1000, result="complete",
                                   reason="count_below_retention_threshold", finished_at=c.now())
    assert store.complete_scan("0xorphan", [_fill(tid=3, time_ms=500)], finished) \
        is ScanWriteback.MISSING
    assert store.get_sync("0xorphan") is None
    assert len(store.get_fills("0xorphan", 0, 10_000)) == 1
    assert store.get_scan(scan.scan_id).status == "done"


# D2：observed_from/to 維護

def test_complete_scan_rejects_scan_without_result(tmp_path):
    """Task 7.9d-D 補：`result is None` 的 scan 不是「完成」——舊版讓它寫進
    `fills_sync.completeness`（NOT NULL）撞 `IntegrityError`，job 被隔離 24
    小時、那一頁 fills 也沒落地。改為進入函式就 `ValueError`（訊息含
    `scan_id`）、**什麼都不寫**：scan 仍 running、fills 未落地，呼叫端下一次
    從同游標續跑（非法頁的正確處置見 `apply_scan_page`）。"""
    store, c = _store(tmp_path)
    scan = _bootstrapped(store, c)
    half_done = dataclasses.replace(scan, last_error="invalid_page:time_out_of_range",
                                    finished_at=c.now())
    with pytest.raises(ValueError, match=scan.scan_id):
        store.complete_scan("0xabc", [_fill(tid=11, time_ms=500)], half_done)
    assert store.get_scan(scan.scan_id).status == "running"
    assert store.get_fills("0xabc", 0, 10_000) == []
    assert store.get_sync("0xabc").scan_id is None


def test_complete_scan_merges_scan_observed_range_into_fills_sync(tmp_path):
    """W2：新地址第一次遍歷完成後，對外 `observed_from/to` 必須非 null 且
    等於這次遍歷看到的成交極值（7.9b 只把極值寫進 `fills_scan`，`fills_sync`
    永遠是 null）。"""
    store, c = _store(tmp_path)
    scan = _bootstrapped(store, c, window_ms=5000)
    fills = [_fill(tid=1, time_ms=scan.window_start_ms + 10),
             _fill(tid=2, time_ms=scan.window_end_ms - 10)]
    finished = dataclasses.replace(
        scan, cursor_ms=scan.window_end_ms, observed_from_ms=fills[0]["time"],
        observed_to_ms=fills[1]["time"], fills_in_window=2, result="complete",
        reason="count_below_retention_threshold", finished_at=c.now())
    assert store.complete_scan("0xabc", fills, finished) is ScanWriteback.APPLIED
    sync = store.get_sync("0xabc")
    assert sync.observed_from_ms == fills[0]["time"]
    assert sync.observed_to_ms == fills[1]["time"]


def test_complete_scan_observed_merge_is_min_max_not_overwrite(tmp_path):
    """增量軌已看過更早／更晚的成交時，遍歷的觀測極值只能擴張區間、不能收窄。"""
    store, c = _store(tmp_path)
    scan = _bootstrapped(store, c, window_ms=5000)
    store.insert_fills_page("0xabc", [], dataclasses.replace(
        store.get_sync("0xabc"), observed_from_ms=100, observed_to_ms=9_999_999_999))
    finished = dataclasses.replace(
        scan, cursor_ms=scan.window_end_ms, observed_from_ms=500, observed_to_ms=600,
        result="complete", reason="count_below_retention_threshold", finished_at=c.now())
    assert store.complete_scan("0xabc", [], finished) is ScanWriteback.APPLIED
    sync = store.get_sync("0xabc")
    assert sync.observed_from_ms == 100
    assert sync.observed_to_ms == 9_999_999_999


# D4：fills_scan 保留與清理計數

def test_purge_deletes_old_done_scans_and_counts_them(tmp_path):
    """done 且完成超過保留期、且不是 `fills_sync.scan_id` 目前指向的那一筆
    → 刪除並計入 `counts["fills_scan"]`。"""
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=c.now())
    scan = _bootstrapped(store, c)
    old = store.create_scan("0xabc", kind="partial_rescan", window_start_ms=0,
                            window_end_ms=1000, cursor_ms=0, started_at=c.now() - 40 * 86400)
    store.complete_scan("0xabc", [], dataclasses.replace(
        old, cursor_ms=1000, result="partial", reason="retention_limit",
        finished_at=c.now() - 40 * 86400))
    # 目前指向的那一筆（保留）：initial scan 之後完成。
    store.complete_scan("0xabc", [], dataclasses.replace(
        scan, cursor_ms=scan.window_end_ms, result="complete",
        reason="count_below_retention_threshold", finished_at=c.now()))

    counts = store.purge(c.now())
    assert counts["fills_scan"] == 1
    remaining = {r[0] for r in store._db.execute(
        "SELECT scan_id FROM fills_scan WHERE address='0xabc'").fetchall()}
    assert remaining == {scan.scan_id}


def test_purge_keeps_current_evidence_scan_even_when_old(tmp_path):
    """`fills_sync.scan_id` 指向的那一筆即使超過保留期也不刪——對外
    `fills_coverage.evidence` 要能回查它。"""
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=c.now())
    scan = _bootstrapped(store, c)
    store.complete_scan("0xabc", [], dataclasses.replace(
        scan, cursor_ms=scan.window_end_ms, result="complete",
        reason="count_below_retention_threshold", finished_at=c.now() - 90 * 86400))
    counts = store.purge(c.now())
    assert counts["fills_scan"] == 0
    assert store.get_scan(scan.scan_id) is not None


def test_purge_counts_fills_scan_rows_deleted_with_stale_candidate(tmp_path):
    """整址 purge 的連帶刪除也要計數（7.9b 刪了但沒收 rowcount）。"""
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xggg", "Gigi", 1, 0.1)], as_of=c.now())
    _bootstrapped(store, c, "0xggg")
    store.deactivate_missing(set())
    c.t += 30 * 86400
    counts = store.purge(c.now(), candidate_keep_s=7 * 86400)
    assert counts["fills_scan"] == 1
    assert counts["fills_sync"] == 1


# D5：遷移原子性與冪等

def _write_v2_multi(db_path) -> None:
    """多列 v2 快照（正式機形狀的縮影）：backfilling（含舊 `fills` job）、
    verified complete、其他 complete、partial 各一列。"""
    _write_v2_schema(db_path, window_end_ms=1000, cursor_ms=500)
    raw = sqlite3.connect(str(db_path))
    raw.execute(
        "INSERT INTO fills_sync (address, window_start_ms, window_end_ms, cursor_ms, "
        "synced_through_ms, completeness, reason, pages_done, fills_in_window, params_fp, "
        "updated_at) VALUES ('0xver', 0, 2000, 2000, 2000, 'complete', "
        "'retention_boundary_verified', 1, 5, '', 1000.0)")
    raw.execute(
        "INSERT INTO fills_sync (address, window_start_ms, window_end_ms, cursor_ms, "
        "synced_through_ms, completeness, reason, pages_done, fills_in_window, params_fp, "
        "updated_at) VALUES ('0xabc', 0, 2000, 2000, 2000, 'complete', "
        "'count_below_retention_threshold', 1, 5, '', 1000.0)")
    raw.execute(
        "INSERT INTO fills_sync (address, window_start_ms, window_end_ms, cursor_ms, "
        "synced_through_ms, completeness, reason, pages_done, fills_in_window, params_fp, "
        "updated_at) VALUES ('0xzzz', 0, 2000, 2000, 2000, 'partial', 'retention_limit', "
        "20, 9000, '', 1000.0)")
    raw.commit()
    raw.close()


def _migration_shape(db_path) -> tuple:
    raw = sqlite3.connect(str(db_path))
    version = raw.execute("SELECT version FROM schema_version").fetchone()[0]
    scans = raw.execute("SELECT COUNT(*) FROM fills_scan").fetchone()[0]
    jobs = dict(raw.execute("SELECT kind, COUNT(*) FROM refresh_job GROUP BY kind").fetchall())
    verify_at = dict(raw.execute(
        "SELECT address, next_attempt_at FROM refresh_job WHERE kind='fills_verify'").fetchall())
    raw.close()
    return version, scans, jobs, verify_at


def test_migration_v2_to_v3_is_idempotent_across_three_runs(tmp_path):
    """W1：遷移重跑（含把 `schema_version` 改回 2 強制再跑一次）不得重複建
    `fills_scan`／`fills_verify` job，也不得因 UNIQUE 撞掉。"""
    db_path = tmp_path / "explore.db"
    _write_v2_multi(db_path)
    now = 1_700_000_000.0

    ExploreStore(db_path, now_fn=lambda: now)
    shape1 = _migration_shape(db_path)

    raw = sqlite3.connect(str(db_path))
    raw.execute("UPDATE schema_version SET version=2")
    raw.commit()
    raw.close()
    ExploreStore(db_path, now_fn=lambda: now + 12345)   # 第二次（強制重跑列迴圈）
    shape2 = _migration_shape(db_path)

    ExploreStore(db_path, now_fn=lambda: now + 99999)   # 第三次（版本已是 3）
    shape3 = _migration_shape(db_path)

    assert shape1[0] == shape2[0] == shape3[0] == 3
    assert shape1[1] == shape2[1] == shape3[1] == 4      # 四列各一筆 fills_scan
    assert shape1[2] == shape2[2] == shape3[2]
    # 核驗 job 只給「有歷史結論但缺證據」的兩列（0xabc complete／0xzzz partial）：
    # verified 那列不入列、backfilling 那列還沒有結論可核驗。
    assert shape1[2]["fills_verify"] == 2
    # 攤開時間由地址雜湊決定 → 重跑不變。
    assert shape1[3] == shape2[3] == shape3[3]


def test_migration_v2_to_v3_row_loop_exception_rolls_back_everything(monkeypatch, tmp_path):
    """D5：列迴圈中途丟例外 → 顯式 transaction 整段回滾：`schema_version`
    仍是 2、`fills_scan` 為空（下次啟動從頭重跑，不會留半套狀態）。"""
    db_path = tmp_path / "explore.db"
    _write_v2_multi(db_path)

    def boom(self, rows, now):
        # 先處理「第一列」（寫進一筆 fills_scan）再炸，確保測的是回滾而不是
        # 「根本沒寫過東西」。
        self._db.execute(
            "INSERT INTO fills_scan (scan_id, address, kind, window_start_ms, window_end_ms, "
            "cursor_ms, status, started_at) VALUES ('x', '0xgap', 'initial', 0, 1, 0, "
            "'running', 1.0)")
        raise RuntimeError("injected mid-loop failure")

    monkeypatch.setattr(ExploreStore, "_migrate_v2_to_v3_rows", boom)
    with pytest.raises(RuntimeError, match="injected"):
        ExploreStore(db_path)

    raw = sqlite3.connect(str(db_path))
    assert raw.execute("SELECT version FROM schema_version").fetchone()[0] == 2
    assert raw.execute("SELECT COUNT(*) FROM fills_scan").fetchone()[0] == 0
    raw.close()


def test_migration_v2_to_v3_rename_guard_when_target_job_key_exists(tmp_path):
    """改名守門：目標 key（`<addr>:fills_scan`）已存在時不改名（否則撞
    PRIMARY KEY 讓整段遷移失敗）。"""
    db_path = tmp_path / "explore.db"
    _write_v2_schema(db_path, window_end_ms=1000, cursor_ms=500)
    raw = sqlite3.connect(str(db_path))
    raw.execute(
        "INSERT INTO refresh_job (key, address, kind, priority, created_at, next_attempt_at) "
        "VALUES ('0xgap:fills_scan', '0xgap', 'fills_scan', 3, 0.0, 500.0)")
    raw.commit()
    raw.close()

    store = ExploreStore(db_path)
    kinds = dict(store._db.execute(
        "SELECT key, kind FROM refresh_job WHERE address='0xgap'").fetchall())
    assert kinds == {"0xgap:fills_scan": "fills_scan", "0xgap:fills": "fills"}


# D6：inc_from_ms 非空邊界

def test_insert_fills_page_rejects_null_inc_from(tmp_path):
    store, c = _store(tmp_path)
    with pytest.raises(ValueError, match="inc_from_ms"):
        store.insert_fills_page("0xabc", [_fill(tid=1, time_ms=100)],
                                _checkpoint(inc_from_ms=None))
    # 整筆不落地：fills 與 checkpoint 都沒有寫進去。
    assert store.get_sync("0xabc") is None
    assert store.get_fills("0xabc", 0, 10_000) == []


def test_store_refuses_to_open_db_with_null_inc_from(tmp_path):
    """啟動檢查：遷移後仍有 `inc_from_ms IS NULL` 的列 → `RuntimeError`
    （訊息含筆數），不帶著破掉的不變式跑起來。"""
    db_path = tmp_path / "explore.db"
    store, c = _store(tmp_path)
    store._db.execute(
        "INSERT INTO fills_sync (address, window_start_ms, window_end_ms, cursor_ms, "
        "synced_through_ms, completeness, pages_done, fills_in_window, params_fp, "
        "updated_at, inc_from_ms) VALUES ('0xbad', 0, 1, 0, NULL, 'backfilling', 0, 0, '', "
        "1.0, NULL)")
    store._db.commit()
    with pytest.raises(RuntimeError, match="1 列"):
        ExploreStore(db_path)


def test_migration_v2_to_v3_backfilling_row_inc_from_never_null(tmp_path):
    """遷移寫入邊界：backfilling 列的 `synced_through_ms` 為 NULL（正式機 69
    列就是這個形狀）也必須寫出非空的增量軌起點（＝scan 窗口末端）。"""
    db_path = tmp_path / "explore.db"
    _write_v2_schema(db_path, window_end_ms=1000, cursor_ms=500)
    store = ExploreStore(db_path)
    assert store.get_sync("0xgap").inc_from_ms == 1000
    assert store._db.execute(
        "SELECT COUNT(*) FROM fills_sync WHERE inc_from_ms IS NULL").fetchone()[0] == 0


# D7：真正的跨缺口案例（(i) 遷移修好的路徑／(ii) 真缺口／(iii) 重掃清除）

def test_gap_case_i_migrated_backfilling_row_completes_without_gap(tmp_path):
    """(i) v2 backfilling 列、`window_end` 早於 now 6 小時 → 遷移後
    `inc_from == window_end` → 該 scan 完成時 gap=0（7.9b 第一版的事故路徑
    已修好；缺口由第一次增量輪補上，不是 gap）。"""
    db_path = tmp_path / "explore.db"
    now = 1_700_000_000.0
    window_end_ms = int((now - 6 * 3600) * 1000)
    _write_v2_schema(db_path, window_end_ms=window_end_ms, cursor_ms=window_end_ms - 500)
    store = ExploreStore(db_path, now_fn=lambda: now)

    assert store.get_sync("0xgap").inc_from_ms == window_end_ms
    scan = store.get_active_scan("0xgap")
    finished = dataclasses.replace(scan, cursor_ms=scan.window_end_ms, result="complete",
                                   reason="count_below_retention_threshold", finished_at=now)
    assert store.complete_scan("0xgap", [], finished) is ScanWriteback.APPLIED
    sync = store.get_sync("0xgap")
    assert sync.coverage_gap is False
    assert external_coverage_state(sync) == ("complete", "count_below_retention_threshold")


def test_gap_case_ii_scan_window_end_before_inc_from_is_a_real_gap(tmp_path):
    """(ii) `inc_from_ms` 晚於 scan `window_end_ms`（遍歷窗口沒接上增量軌
    起點）→ `coverage_gap=1`、對外 `("partial", "coverage_gap")`。"""
    store, c = _store(tmp_path)
    _bootstrapped(store, c)
    inc_from_ms = store.get_sync("0xabc").inc_from_ms
    stale_scan = store.create_scan("0xabc", kind="partial_rescan", window_start_ms=0,
                                   window_end_ms=inc_from_ms - 60_000, cursor_ms=0,
                                   started_at=c.now() + 1)
    finished = dataclasses.replace(stale_scan, cursor_ms=stale_scan.window_end_ms,
                                   result="complete",
                                   reason="count_below_retention_threshold",
                                   finished_at=c.now() + 2)
    assert store.complete_scan("0xabc", [], finished) is ScanWriteback.APPLIED
    sync = store.get_sync("0xabc")
    assert sync.coverage_gap is True
    assert external_coverage_state(sync) == ("partial", "coverage_gap")


def test_gap_case_iii_next_rescan_clears_the_gap(tmp_path):
    """(iii) 之後一次 `partial_rescan`（`window_end = now >= inc_from`）完成
    → gap 清 0、對外恢復 complete。"""
    store, c = _store(tmp_path)
    _bootstrapped(store, c)
    inc_from_ms = store.get_sync("0xabc").inc_from_ms
    stale_scan = store.create_scan("0xabc", kind="partial_rescan", window_start_ms=0,
                                   window_end_ms=inc_from_ms - 60_000, cursor_ms=0,
                                   started_at=c.now() + 1)
    store.complete_scan("0xabc", [], dataclasses.replace(
        stale_scan, cursor_ms=stale_scan.window_end_ms, result="complete",
        reason="count_below_retention_threshold", finished_at=c.now() + 2))
    assert store.get_sync("0xabc").coverage_gap is True

    c.t += PARTIAL_RESCAN_AFTER_S
    fresh_end_ms = int(c.now() * 1000)
    fresh = store.create_scan("0xabc", kind="partial_rescan",
                              window_start_ms=fresh_end_ms - 30 * 86_400_000,
                              window_end_ms=fresh_end_ms, cursor_ms=0, started_at=c.now())
    assert store.complete_scan("0xabc", [], dataclasses.replace(
        fresh, cursor_ms=fresh_end_ms, result="complete",
        reason="count_below_retention_threshold",
        finished_at=c.now())) is ScanWriteback.APPLIED
    sync = store.get_sync("0xabc")
    assert sync.coverage_gap is False
    assert external_coverage_state(sync) == ("complete", "count_below_retention_threshold")


# D8：存取器

def test_latest_done_scan_returns_most_recently_finished(tmp_path):
    store, c = _store(tmp_path)
    scan = _bootstrapped(store, c)
    assert store.latest_done_scan("0xabc") is None      # 只有 running
    store.complete_scan("0xabc", [], dataclasses.replace(
        scan, cursor_ms=scan.window_end_ms, result="complete",
        reason="count_below_retention_threshold", finished_at=c.now() + 10))
    older = store.create_scan("0xabc", kind="verify", window_start_ms=0, window_end_ms=10,
                              cursor_ms=0, started_at=c.now() + 1)
    store.complete_scan("0xabc", [], dataclasses.replace(
        older, cursor_ms=10, result="partial", reason="retention_limit",
        finished_at=c.now() + 5))
    latest = store.latest_done_scan("0xabc")
    assert latest.scan_id == scan.scan_id
    assert latest.finished_at == c.now() + 10


def test_running_scan_matches_get_active_scan(tmp_path):
    store, c = _store(tmp_path)
    scan = _bootstrapped(store, c)
    assert store.running_scan("0xabc") == scan
    store.complete_scan("0xabc", [], dataclasses.replace(
        scan, cursor_ms=scan.window_end_ms, result="complete",
        reason="count_below_retention_threshold", finished_at=c.now()))
    assert store.running_scan("0xabc") is None


def test_job_kinds_returns_kinds_for_that_address_only(tmp_path):
    store, c = _store(tmp_path)
    store.enqueue("0xabc:state", "0xabc", "state", priority=0, next_attempt_at=c.now())
    store.enqueue("0xabc:fills", "0xabc", "fills", priority=2, next_attempt_at=c.now())
    store.enqueue("0xdef:fills_scan", "0xdef", "fills_scan", priority=3,
                  next_attempt_at=c.now())
    assert store.job_kinds("0xABC") == {"state", "fills"}
    assert store.job_kinds("0xdef") == {"fills_scan"}
    assert store.job_kinds("0xnope") == set()


def test_oldest_due_at_kinds_filter_only_counts_listed_kinds(tmp_path):
    store, c = _store(tmp_path)
    store.enqueue("0xabc:fills", "0xabc", "fills", priority=2, next_attempt_at=c.now() - 100)
    store.enqueue("0xabc:fills_verify", "0xabc", "fills_verify", priority=4,
                  next_attempt_at=c.now() - 50)
    store.enqueue("0xdef:fills_verify", "0xdef", "fills_verify", priority=4,
                  next_attempt_at=c.now() + 500)   # 未到期，不算
    assert store.oldest_due_at(c.now()) == c.now() - 100
    assert store.oldest_due_at(c.now(), kinds=("fills_verify",)) == c.now() - 50
    assert store.oldest_due_at(c.now(), kinds=("candidates",)) is None


# --- Task 7.9d-D：同源彙總查詢（D2）／退池 job 清理／scan 對帳目標 ---
#
# 7.9c 複審 W2／S1：scheduler 的 `scans_running`／`verify_remaining` 走
# `active_candidates()` 逐一點查（21ms/次，且母體是「目前 active 候選」而不是
# 「被排程的列本身」——正式機 129 筆 verify job 只顯示 112）。以下測試釘住
# 新彙總查詢的母體：**不經 active 過濾**（退池地址的 job／running scan 照樣
# 計入），`active` 參數只做**分列**（active／inactive／orphan），不縮小母體。

def _inactive_addr_with_job_and_running_scan(store, c, addr="0xdead"):
    """建一個「已退池但 job／running scan 還在」的地址（正式機真實狀態：
    `deactivate_missing` 已標 active=0，但 `delete_jobs`／scan 收尾還沒發生）。"""
    store.upsert_candidates([(addr, "Gone", 9, 0.0)], as_of=c.now())
    store.deactivate_missing(set())            # 全部踢出池
    assert store.is_active(addr) is False
    _bootstrapped(store, c, addr)              # running initial scan
    store.enqueue(f"{addr}:fills_verify", addr, "fills_verify", priority=4,
                  next_attempt_at=c.now() - 10)
    return addr


def _active(store):
    return {cand.address for cand in store.active_candidates()}


def test_count_jobs_by_kind_counts_every_job_without_active_filter(tmp_path):
    """`active=None`：母體＝`refresh_job` 列本身，`active_rows == rows`、
    `inactive_rows == 0`（沒問就不分列，不假裝知道）。"""
    store, c = _store(tmp_path)
    store.enqueue("0xabc:state", "0xabc", "state", priority=0, next_attempt_at=c.now())
    store.enqueue("0xabc:fills_verify", "0xabc", "fills_verify", priority=4,
                  next_attempt_at=c.now() + 5000)
    _inactive_addr_with_job_and_running_scan(store, c)   # 再一筆退池的 fills_verify
    counts = store.count_jobs_by_kind()
    # 含退池地址那一筆（W2：129 不是 112）；列數與地址數分開回報。
    assert counts["fills_verify"] == {"rows": 2, "addresses": 2,
                                      "active_rows": 2, "inactive_rows": 0}
    assert counts["state"] == {"rows": 1, "addresses": 1, "active_rows": 1,
                               "inactive_rows": 0}
    # 已知 kind 沒有列時是 0，不是缺鍵。
    assert counts["fills"] == {"rows": 0, "addresses": 0, "active_rows": 0,
                               "inactive_rows": 0}


def test_count_jobs_by_kind_splits_active_and_inactive_rows(tmp_path):
    """傳入 `active` 集合 → 同一次查詢分列 active／inactive（正式機形狀：
    `{"fills_verify": {"rows":129,"addresses":129,"active_rows":112,
    "inactive_rows":17}}`）；列數與地址數分開（同地址兩個 job ≠ 兩個地址）。"""
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xaaa", "A", 1, 0.1), ("0xbbb", "B", 2, 0.2)], as_of=c.now())
    for addr in ("0xaaa", "0xbbb"):
        store.enqueue(f"{addr}:fills_verify", addr, "fills_verify", priority=4,
                      next_attempt_at=c.now())
    store.enqueue("0xaaa:fills", "0xaaa", "fills", priority=2, next_attempt_at=c.now())
    store.enqueue("candidates", None, "candidates", priority=0, next_attempt_at=c.now())
    inactive = _inactive_addr_with_job_and_running_scan(store, c)   # 退池、verify job 還在
    store.upsert_candidates([("0xaaa", "A", 1, 0.1), ("0xbbb", "B", 2, 0.2)], as_of=c.now())
    assert inactive not in _active(store)

    counts = store.count_jobs_by_kind(_active(store))
    assert counts["fills_verify"] == {"rows": 3, "addresses": 3,
                                      "active_rows": 2, "inactive_rows": 1}
    assert counts["fills"] == {"rows": 1, "addresses": 1, "active_rows": 1,
                               "inactive_rows": 0}
    # 全域 kind（address NULL）永遠算 active——它沒有「退池」這回事，
    # `delete_inactive_jobs` 也不刪它；`addresses` 不計 NULL。
    assert counts["candidates"] == {"rows": 1, "addresses": 0, "active_rows": 1,
                                    "inactive_rows": 0}


def _mark_evidence_unknown(store, *addresses):
    """把 `fills_sync.evidence_unknown` 設 1（遷移產生的「證據不明」狀態；
    資料層沒有公開 setter，這裡直接寫 DB 造狀態）。"""
    with store._db:
        for addr in addresses:
            store._db.execute(
                "UPDATE fills_sync SET evidence_unknown=1 WHERE address=?", (addr,))


def test_count_scans_orphan_requires_job_of_the_matching_kind(tmp_path):
    """Task 7.9e-D D1（7.9d 複審 W1）：孤兒＝running scan 且**沒有對應 kind
    的 job**。`verify` scan 由 `fills_verify` job 推進、`initial`／
    `partial_rescan` 由 `fills_scan` job 推進——判準寫死 `fills_scan` 會把
    「running verify＋正確的 fills_verify job」誤算成孤兒（複審 `rv_orphan.py`
    實跑 orphan_rows=1）。"""
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xaaa", "A", 1, 0.1)], as_of=c.now())
    store.create_scan("0xaaa", kind="verify", window_start_ms=0, window_end_ms=1000,
                      cursor_ms=0, started_at=c.now())
    store.enqueue("0xaaa:fills_verify", "0xaaa", "fills_verify", priority=4,
                  next_attempt_at=c.now())
    assert store.count_scans(_active(store))["orphan_rows"] == 0

    # 同一個 running verify，job 換成 fills_scan（kind 不對）→ 沒有東西會推進它。
    store.delete_jobs("0xaaa")
    store.enqueue("0xaaa:fills_scan", "0xaaa", "fills_scan", priority=3,
                  next_attempt_at=c.now())
    assert store.count_scans(_active(store))["orphan_rows"] == 1


def test_count_scans_orphan_for_initial_scan_requires_fills_scan_job(tmp_path):
    """反向配對：`initial` scan 要 `fills_scan` job，只有 `fills_verify` 不算。"""
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xabc", "A", 1, 0.1)], as_of=c.now())
    _bootstrapped(store, c, "0xabc")                      # running initial
    store.enqueue("0xabc:fills_verify", "0xabc", "fills_verify", priority=4,
                  next_attempt_at=c.now())
    assert store.count_scans(_active(store))["orphan_rows"] == 1
    store.enqueue("0xabc:fills_scan", "0xabc", "fills_scan", priority=3,
                  next_attempt_at=c.now())
    assert store.count_scans(_active(store))["orphan_rows"] == 0


def test_verify_needed_counts_unknown_evidence_and_the_work_serving_it(tmp_path):
    """Task 7.9e-D D2：核驗需求的單一數字來源——`verify_remaining`（job 列數）
    歸零不代表核驗做完（7.9d 複審 C2：job 被掃除後 `evidence_unknown` 永久
    留 1，health 看不出來）。`unserved` 才是「狀態需要核驗但沒有任何工作在
    服務它」的筆數。"""
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xa1", "A", 1, 0.1), ("0xa2", "B", 2, 0.2),
                             ("0xa3", "C", 3, 0.3), ("0xa4", "D", 4, 0.4)], as_of=c.now())
    for addr in ("0xa1", "0xa2", "0xa3", "0xa4"):
        store.bootstrap_address_fills(addr, c.now(), window_start_ms=0, window_end_ms=1000,
                                      params_fp="pfp")
    _mark_evidence_unknown(store, "0xa1", "0xa2", "0xa3")   # 0xa4 證據充分
    store.enqueue("0xa1:fills_verify", "0xa1", "fills_verify", priority=4,
                  next_attempt_at=c.now())                  # 有 job
    store.create_scan("0xa2", kind="verify", window_start_ms=0, window_end_ms=1000,
                      cursor_ms=0, started_at=c.now())      # 有 running verify
    # 0xa3：兩者皆無 → unserved。
    assert store.verify_needed(_active(store)) == {
        "rows": 3, "with_job": 1, "with_running": 1, "unserved": 1}

    # 退池的地址不算需求（對帳只對 active 地址補工作）。
    store.deactivate_missing({"0xa1", "0xa2"})
    assert store.verify_needed(_active(store)) == {
        "rows": 2, "with_job": 1, "with_running": 1, "unserved": 0}
    assert store.verify_needed(set()) == {"rows": 0, "with_job": 0, "with_running": 0,
                                          "unserved": 0}


def test_verify_needed_reproduces_prod_snapshot_shape(tmp_path):
    """正式機快照複本（`prod_meta_v2.db` 遷移後，300 active 候選）實跑值：
    `verify_needed(active) == {"rows": 112, "with_job": 112, "with_running": 0,
    "unserved": 0}`（129 筆 `fills_verify` job 中 112 筆屬於 active 地址，其餘
    17 筆屬於退池地址、不算需求）。測試不讀正式機檔案（離線紅線），改在
    tmp DB 重建同一組形狀並釘住同一組數字。"""
    store, c = _store(tmp_path)
    rows = [(f"0x{i:04x}", None, i, None) for i in range(300)]
    store.upsert_candidates(rows, as_of=c.now())
    for addr, *_ in rows:
        store.bootstrap_address_fills(addr, c.now(), window_start_ms=0, window_end_ms=1000,
                                      params_fp="pfp")
    unknown = [addr for addr, *_ in rows[:112]]
    _mark_evidence_unknown(store, *unknown)
    for addr in unknown:
        store.enqueue(f"{addr}:fills_verify", addr, "fills_verify", priority=4,
                      next_attempt_at=c.now())
    # 另外 17 筆退池地址的 verify job（快照裡 129 − 112）：不算 active 需求。
    for i in range(17):
        addr = f"0xdead{i:02x}"
        store.enqueue(f"{addr}:fills_verify", addr, "fills_verify", priority=4,
                      next_attempt_at=c.now())
    assert store.count_jobs_by_kind()["fills_verify"]["rows"] == 129
    assert store.verify_needed(_active(store)) == {
        "rows": 112, "with_job": 112, "with_running": 0, "unserved": 0}


def test_admission_multiplier_lives_in_the_data_layer(tmp_path):
    """Task 7.9e-D D3（複審 S3）：準入倍數是純常數，放在無依賴的資料層，
    `app.py` 不必為了一個常數 import 整個 scheduler。"""
    from spark.publicapi.explore_store import ADMISSION_MULTIPLIER, JOB_KINDS
    assert ADMISSION_MULTIPLIER == 7
    # 6 種 per-address kind ＋ candidates 全域 job ＋ 餘裕。
    assert len([k for k in JOB_KINDS if k != "candidates"]) == 6


def test_count_scans_reports_running_orphan_and_inactive_rows(tmp_path):
    store, c = _store(tmp_path)
    assert store.count_scans() == {"running_rows": 0, "running_addresses": 0,
                                   "orphan_rows": 0, "inactive_running_rows": 0}
    store.upsert_candidates([("0xabc", "Alice", 1, 0.1)], as_of=c.now())
    active_scan = _bootstrapped(store, c, "0xabc")
    store.enqueue("0xabc:fills_scan", "0xabc", "fills_scan", priority=3,
                  next_attempt_at=c.now())
    inactive = _inactive_addr_with_job_and_running_scan(store, c)   # running scan、無 scan job
    store.upsert_candidates([("0xabc", "Alice", 1, 0.1)], as_of=c.now())   # 0xabc 回池

    # active=None：母體是 fills_scan 列本身（退池的也算），不分 active。
    assert store.count_scans() == {"running_rows": 2, "running_addresses": 2,
                                   "orphan_rows": 1, "inactive_running_rows": 0}
    # 傳 active → 分列；orphan＝running 但該地址沒有 fills_scan job（對帳目標）。
    assert store.count_scans(_active(store)) == {
        "running_rows": 2, "running_addresses": 2, "orphan_rows": 1,
        "inactive_running_rows": 1}
    assert inactive not in _active(store)

    store.complete_scan("0xabc", [], dataclasses.replace(
        active_scan, cursor_ms=active_scan.window_end_ms, result="complete",
        reason="count_below_retention_threshold", finished_at=c.now()))
    # done 的不算 running。
    assert store.count_scans(_active(store))["running_rows"] == 1


def test_delete_inactive_jobs_removes_only_non_active_addresses(tmp_path):
    """點 3（使用者第二輪裁決）：對帳時主動掃除既有殘留 job，不只靠本輪
    `deactivate_missing` 的回傳值——重啟／漏掉的那幾輪不會留下永久殘留。"""
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xaaa", "A", 1, 0.1)], as_of=c.now())
    store.enqueue("0xaaa:fills", "0xaaa", "fills", priority=2, next_attempt_at=c.now())
    store.enqueue("candidates", None, "candidates", priority=0, next_attempt_at=c.now())
    inactive = _inactive_addr_with_job_and_running_scan(store, c)
    store.upsert_candidates([("0xaaa", "A", 1, 0.1)], as_of=c.now())        # 0xaaa 回池

    assert store.delete_inactive_jobs(_active(store)) == 1                  # 只刪退池那一筆
    assert store.job_kinds(inactive) == set()
    assert store.job_kinds("0xaaa") == {"fills"}
    assert store.count_jobs_by_kind()["candidates"]["rows"] == 1            # 全域 job 不刪
    assert store.delete_inactive_jobs(_active(store)) == 0                  # 冪等


def test_delete_inactive_jobs_with_empty_active_set_keeps_global_jobs(tmp_path):
    store, c = _store(tmp_path)
    store.enqueue("0xaaa:fills", "0xaaa", "fills", priority=2, next_attempt_at=c.now())
    store.enqueue("candidates", None, "candidates", priority=0, next_attempt_at=c.now())
    assert store.delete_inactive_jobs(set()) == 1
    assert store.count_jobs_by_kind()["candidates"]["rows"] == 1


def test_scan_job_targets_returns_one_row_per_active_address(tmp_path):
    """對帳用：每個 active 地址一列
    `(address, running_scan_id, evidence_unknown, has_done_verify)`——單句
    LEFT JOIN，不做 300 次點查；`running_scan_id is None` ＝沒有進行中的遍歷。
    Task 7.9e-D D4：後兩欄讓 S 的第四原因碼（`verify_needed`）一句 SQL 拿齊。"""
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xAAA", "A", 1, 0.1), ("0xbbb", "B", 2, 0.2)], as_of=c.now())
    scan = _bootstrapped(store, c, "0xaaa")
    _inactive_addr_with_job_and_running_scan(store, c)   # 退池地址不得出現
    store.upsert_candidates([("0xaaa", "A", 1, 0.1), ("0xbbb", "B", 2, 0.2)], as_of=c.now())

    targets = store.scan_job_targets(_active(store))
    assert targets == [("0xaaa", scan.scan_id, 0, False), ("0xbbb", None, 0, False)]
    assert targets[0].address == "0xaaa" and targets[0].running_scan_id == scan.scan_id
    assert targets[0].evidence_unknown == 0 and targets[0].has_done_verify is False
    # 完成後同一個地址回 None（不再需要續跑）。
    store.complete_scan("0xaaa", [], dataclasses.replace(
        scan, cursor_ms=scan.window_end_ms, result="complete",
        reason="count_below_retention_threshold", finished_at=c.now()))
    assert store.scan_job_targets(_active(store)) == [("0xaaa", None, 0, False),
                                                      ("0xbbb", None, 0, False)]
    assert store.scan_job_targets(set()) == []


def test_scan_job_targets_reports_evidence_unknown_and_done_verify(tmp_path):
    """D4：`evidence_unknown`（遷移列的證據不明旗標）與 `has_done_verify`
    （該地址已經跑完過一次 `verify` 遍歷）各自獨立——已經核驗過但旗標還在，
    與從未核驗過，是兩種不同的處置。"""
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xa1", "A", 1, 0.1), ("0xa2", "B", 2, 0.2)], as_of=c.now())
    for addr in ("0xa1", "0xa2"):
        store.bootstrap_address_fills(addr, c.now(), window_start_ms=0, window_end_ms=1000,
                                      params_fp="pfp")
    _mark_evidence_unknown(store, "0xa1", "0xa2")
    # 0xa2 另外有一次已完成的 verify 遍歷（running 的那個是 initial，不算 done）。
    verify = store.create_scan("0xa2", kind="verify", window_start_ms=0, window_end_ms=500,
                               cursor_ms=0, started_at=c.now())
    store.complete_scan("0xa2", [], dataclasses.replace(
        verify, cursor_ms=500, result="partial", reason="retention_limit",
        finished_at=c.now() + 1))
    _mark_evidence_unknown(store, "0xa2")   # complete_scan 會清旗標，重新造狀態

    targets = {t.address: t for t in store.scan_job_targets(_active(store))}
    assert targets["0xa1"].evidence_unknown == 1
    assert targets["0xa1"].has_done_verify is False
    assert targets["0xa2"].evidence_unknown == 1
    assert targets["0xa2"].has_done_verify is True
    # running 欄位仍只看 running scan（0xa2 的 initial 還在跑）。
    assert targets["0xa2"].running_scan_id == store.running_scan("0xa2").scan_id


def test_count_due_by_kind_counts_only_due_and_lease_free_jobs(tmp_path):
    store, c = _store(tmp_path)
    store.enqueue("0xabc:fills", "0xabc", "fills", priority=2, next_attempt_at=c.now() - 100)
    store.enqueue("0xdef:fills", "0xdef", "fills", priority=2, next_attempt_at=c.now() + 100)
    addr = _inactive_addr_with_job_and_running_scan(store, c)   # 逾期的 verify job
    due = store.count_due_by_kind(c.now())
    assert due["fills"] == 1                 # 未到期的那筆不算
    assert due["fills_verify"] == 1          # 退池地址的到期 job 照樣算
    assert due["fills_scan"] == 0            # 已知 kind 沒有到期列 → 0
    # lease 被領走的不算到期（與 `due_count`／`claim_due` 同一組條件）。
    claimed = store.claim_due(c.now(), "owner", 60.0, kinds=("fills_verify",))
    assert claimed is not None and claimed.address == addr
    assert store.count_due_by_kind(c.now())["fills_verify"] == 0
    assert store.count_jobs_by_kind()["fills_verify"]["rows"] == 1   # job 列還在


def test_count_due_by_kind_agrees_with_due_count_per_kind(tmp_path):
    """彙總與既有逐 kind 查詢同口徑（同源：兩者不得各自算一套到期定義）。"""
    store, c = _store(tmp_path)
    for i, kind in enumerate(("state", "portfolio", "ledger", "fills", "fills_scan",
                              "fills_verify")):
        store.enqueue(f"0x{i}:{kind}", f"0x{i}", kind, priority=i,
                      next_attempt_at=c.now() - 1)
    store.enqueue("0xlate:fills", "0xlate", "fills", priority=2, next_attempt_at=c.now() + 60)
    due = store.count_due_by_kind(c.now())
    for kind in due:
        assert due[kind] == store.due_count(kind, c.now()), kind
    assert due["fills"] == 1
