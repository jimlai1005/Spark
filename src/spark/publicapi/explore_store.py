"""src/spark/publicapi/explore_store.py
Explore 排行榜的持久化資料層（spec `docs/superpowers/specs/2026-09-20-hl-leaderboard-refactor-spec.md`
§7 六種資料、§9.1 lease／fencing／dedupe／admission）。

純資料層：只有 SQL 與 dataclass 轉換，**零網路、零業務判斷**。P3 scheduler、P4 publisher
只透過本模組讀寫 `explore.db`（SQLite WAL）。

六張表：
- `candidate`：候選池快照（來自 stats-data leaderboard，每輪覆蓋 rank/roi，`active` 標記本輪
  是否仍在池內；被踢出的候選不立刻刪，交給 `purge` 依保留期處理）。
- `endpoint_cache`：每地址每 endpoint（portfolio／clearinghouseState／ledger）一份最新結果，
  `payload` 為 NULL 代表從未成功過；`put_cache_error` 只動 refresh_after／last_error*，
  刻意不覆寫上一次成功的 `payload`／`fetched_at`（spec 條件 4：失敗不能讓資料倒退成缺值）。
- `fills`：原始 HL fill JSON（`raw`，字串原樣、不轉 float，PK 天然去重）。
- `fills_sync`：每地址一份同步游標／完整性判定（`FillsSyncState`，Task 2.2 的
  `explore_fills_sync.py` 消費本表）。
- `refresh_job`：待辦工作佇列，lease＋fencing 防重複執行（`claim_due`／`complete`／`reschedule`）。

Transaction 慣例：全模組沿用 `store.py`（`ApiStore`）既有寫法——預設 `isolation_level`
（非 None）＋`with self._db:` 隱式 transaction（進入自動 BEGIN、正常結束 COMMIT、例外
ROLLBACK），寫入方法一律 `with self._lock, self._db:`。這是本 task 卡片給的兩個選項
（isolation_level=None 手動 BEGIN IMMEDIATE／COMMIT，或沿用隱式 transaction）之一，選擇
理由：`threading.Lock` 已在 Python 層序列化所有寫入，SQLite 層不需要 BEGIN IMMEDIATE
搶鎖來避免多寫入者競爭；沿用既有 `ApiStore` 慣例可讓兩個 store 模組風格一致。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Task 7.5（2026-09-21，使用者裁決：complete 判定的證據與標示）：completeness
# 的 reason 碼——`complete` 也要有原因，不再是「留白代表沒問題」。
# `REASON_COUNT_BELOW_RETENTION_THRESHOLD`：現行判準（`explore_fills_sync.apply_page`
# 短頁收尾、本輪觀測筆數低於 `RETENTION_SAFETY_THRESHOLD`）；
# `REASON_RETENTION_BOUNDARY_VERIFIED`：留存邊界探測（`explore_scheduler._run_fills`）
# 實測區間起點之前一天仍有可查成交，代表留存邊界早於窗口起點，比門檻推論更強的證據。
# 兩個 reason 碼與（`ExploreStore`）schema migration 共用，放在資料層而非
# `explore_fills_sync`（後者反向 import 本模組的 `FillsSyncState`，避免循環 import）。
REASON_COUNT_BELOW_RETENTION_THRESHOLD = "count_below_retention_threshold"
REASON_RETENTION_BOUNDARY_VERIFIED = "retention_boundary_verified"

# Task 7.7 W3（2026-09-21，7.6 複審）：探測回空頁只證明「這一次探測沒看到更早
# 的成交」，門檻推論（`REASON_COUNT_BELOW_RETENTION_THRESHOLD`）本身沒有變得
# 更弱也沒有變強——但若不記下「已經探過」，`explore_scheduler._run_fills` 的
# 探測條件（`reason == REASON_COUNT_BELOW_RETENTION_THRESHOLD`）會讓同一個
# 地址每次全區間遍歷後都重探一次，永遠打不出結論就永遠再試。這個第三個 reason
# 碼把「探過、沒有更早成交、無法升級」記下來，探測條件天然排除它，讓每次全
# 區間遍歷後至多探到有結論（`verified` 或本碼）為止。
REASON_PROBE_NO_EARLIER_FILLS = "count_below_retention_threshold_probe_empty"

# schema_version：1（初版）→2（Task 7.5：`fills_sync.params_fp` 欄位＋既有
# complete／reason=NULL 列補標）。
_SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS candidate (
  address TEXT PRIMARY KEY,            -- 小寫正規化
  display_name TEXT, source_rank INTEGER, source_roi REAL,
  source_as_of REAL NOT NULL,          -- stats-data payload 取得時刻
  active INTEGER NOT NULL DEFAULT 1, last_seen_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS endpoint_cache (
  address TEXT NOT NULL, endpoint TEXT NOT NULL,   -- 'portfolio' | 'clearinghouseState'
  params_fp TEXT NOT NULL DEFAULT '',              -- dex／參數指紋（目前 ''）
  payload TEXT,                                    -- JSON；NULL＝從未成功
  fetched_at REAL, refresh_after REAL NOT NULL,
  last_error TEXT, last_error_at REAL,
  PRIMARY KEY (address, endpoint, params_fp));
CREATE TABLE IF NOT EXISTS fills (
  address TEXT NOT NULL, coin TEXT NOT NULL, tid INTEGER NOT NULL,
  time_ms INTEGER NOT NULL, raw TEXT NOT NULL,     -- 原始 HL fill JSON（Decimal 字串原樣）
  PRIMARY KEY (address, coin, tid));
CREATE INDEX IF NOT EXISTS fills_addr_time ON fills(address, time_ms);
CREATE TABLE IF NOT EXISTS fills_sync (
  address TEXT PRIMARY KEY,
  window_start_ms INTEGER NOT NULL, window_end_ms INTEGER NOT NULL,  -- 本輪固定區間
  cursor_ms INTEGER NOT NULL,          -- 下一頁 startTime（inclusive，含重疊）
  synced_through_ms INTEGER,           -- 已確認遍歷到此
  observed_from_ms INTEGER, observed_to_ms INTEGER,
  completeness TEXT NOT NULL DEFAULT 'backfilling'   -- backfilling|partial|complete
      CHECK (completeness IN ('backfilling', 'partial', 'complete')),
  reason TEXT, pages_done INTEGER NOT NULL DEFAULT 0,
  fills_in_window INTEGER NOT NULL DEFAULT 0,
  params_fp TEXT NOT NULL DEFAULT '',   -- Task 7.5：查詢參數留證（見 explore_fills_sync.PARAMS_FP）
  updated_at REAL NOT NULL, last_error TEXT);
CREATE TABLE IF NOT EXISTS refresh_job (
  key TEXT PRIMARY KEY,                -- f"{address}:{kind}"  kind∈portfolio|state|fills|candidates
  address TEXT, kind TEXT NOT NULL, priority INTEGER NOT NULL,   -- 0 最高
  created_at REAL NOT NULL, next_attempt_at REAL NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_until REAL, lease_owner TEXT, fencing INTEGER NOT NULL DEFAULT 0,
  last_error TEXT);
CREATE INDEX IF NOT EXISTS job_due ON refresh_job(next_attempt_at, priority);
"""


def _norm(address: str) -> str:
    return address.lower()


@dataclass(frozen=True)
class Candidate:
    address: str
    display_name: str | None
    source_rank: int | None
    source_roi: float | None
    source_as_of: float
    active: bool
    last_seen_at: float


@dataclass(frozen=True)
class CacheEntry:
    address: str
    endpoint: str
    params_fp: str
    payload: Any | None
    fetched_at: float | None
    refresh_after: float
    last_error: str | None
    last_error_at: float | None


@dataclass(frozen=True)
class FillsSyncState:
    """對映 `fills_sync` 一列（Task 2.2 `explore_fills_sync.py` 會 import 本 dataclass）。"""
    address: str
    window_start_ms: int
    window_end_ms: int
    cursor_ms: int
    synced_through_ms: int | None
    observed_from_ms: int | None
    observed_to_ms: int | None
    completeness: str
    reason: str | None
    pages_done: int
    fills_in_window: int
    updated_at: float
    last_error: str | None
    # Task 7.5：查詢參數留證（例如 `"aggregateByTime=default(false)"`）——放在
    # dataclass 最後一個欄位並給預設值 `""`，讓既有呼叫端（測試 fixture／舊資料）
    # 不必逐一改動就能繼續建構本類別（frozen dataclass 的欄位預設值規則：
    # 有預設值的欄位必須排在最後）。
    params_fp: str = ""


@dataclass(frozen=True)
class Job:
    """對映 `refresh_job` 一列。"""
    key: str
    address: str | None
    kind: str
    priority: int
    created_at: float
    next_attempt_at: float
    attempts: int
    lease_until: float | None
    lease_owner: str | None
    fencing: int
    last_error: str | None


class ExploreStore:
    """單一連線 + lock（API thread 與 scheduler thread 會同時用，需 thread-safe）。"""

    def __init__(self, db_path: str | Path, *, now_fn=time.time):
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.Lock()
        self._now = now_fn
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)
            row = self._db.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (_SCHEMA_VERSION,))
            elif row[0] < _SCHEMA_VERSION:
                self._migrate_v1_to_v2()
                self._db.execute("UPDATE schema_version SET version=?", (_SCHEMA_VERSION,))
        if str(db_path) != ":memory:":
            # Task 3.6 C（W2 修法）：WAL 模式會在 db 旁邊建 `-wal`／`-shm` 側檔，
            # 這兩個檔案原本沒被 chmod 過（預設 0644，會外洩地址／portfolio 等
            # 資料到同機其他使用者）。建表（上面）已經觸發至少一次寫入，側檔
            # 此時應已落地；存在者一律 chmod 0600（best effort）。
            for suffix in ("", "-wal", "-shm"):
                side_path = f"{db_path}{suffix}"
                if not os.path.exists(side_path):
                    continue
                try:
                    os.chmod(side_path, 0o600)
                except OSError:
                    logger.warning(
                        "explore store: chmod 0600 失敗（best effort，例如 Windows）: %s",
                        side_path, exc_info=True)

    def _migrate_v1_to_v2(self) -> None:
        """Task 7.5 點 4／5：schema v1→v2——`fills_sync` 補 `params_fp`（既有 DB
        若缺此欄，`ALTER TABLE ADD COLUMN`；`CREATE TABLE IF NOT EXISTS` 不會幫
        既存的表補新欄，得自己判斷）；既有 `completeness='complete' AND reason
        IS NULL` 的列補 `REASON_COUNT_BELOW_RETENTION_THRESHOLD`——這批舊列是
        在本次修正前用同一個門檻判完的，只是當時沒有把判準寫進 `reason`（見
        `explore_fills_sync.apply_page` 檔頭）。

        Task 7.6 點 9（複審 S3 修法，docstring 不實）：呼叫端（`__init__`）雖然
        包在 `with self._lock, self._db:` 底下，但這**不代表**下面兩步 SQL 是同一個
        transaction——`ALTER TABLE` 是 DDL，Python `sqlite3` 遇到 DDL 會自動
        COMMIT 目前的隱式 transaction（stdlib 行為，與呼叫端有沒有包 `with` 無關），
        所以本 migration 實際上**不在**一個 transaction 內執行，中途（例如
        `ALTER TABLE` 成功、行程在 `UPDATE` 之前被殺）可能停在中間態。這是安全的，
        因為兩步都各自冪等：`ALTER TABLE` 前先查 `PRAGMA table_info` 判斷欄位是否
        已存在（存在則跳過，不會重複 `ALTER` 出錯）；`UPDATE` 的 `WHERE` 條件式
        （`completeness='complete' AND reason IS NULL`）本身就是「還沒補過」的
        判斷，重跑不會誤傷已經補過（`reason` 非 NULL）的列。下次啟動時
        `schema_version` 仍是舊值會重新呼叫本函式，兩步各自的冪等性讓中途失敗
        的中間態能自癒，不需要顯式 `BEGIN`。**未來**若新增涉及多個 DDL
        **且**需要搬移既有資料的 migration（例如新表＋把舊表資料搬過去），不能
        再套用同一份僥倖——DDL 一定會提交、資料搬移必須拆成獨立的、可重覆執行
        （冪等）的步驟，或改用「新建影子表→驗證→原子改名」的模式，不能假設
        整段可以一次 rollback。"""
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(fills_sync)").fetchall()}
        if "params_fp" not in cols:
            self._db.execute(
                "ALTER TABLE fills_sync ADD COLUMN params_fp TEXT NOT NULL DEFAULT ''")
        self._db.execute(
            "UPDATE fills_sync SET reason=? WHERE completeness='complete' AND reason IS NULL",
            (REASON_COUNT_BELOW_RETENTION_THRESHOLD,))

    # --- candidate ---
    def upsert_candidates(self, rows: list[tuple[str, str | None, int | None, float | None]],
                           as_of: float) -> None:
        with self._lock, self._db:
            for address, display_name, rank, roi in rows:
                addr = _norm(address)
                self._db.execute(
                    "INSERT INTO candidate (address, display_name, source_rank, source_roi, "
                    "source_as_of, active, last_seen_at) VALUES (?, ?, ?, ?, ?, 1, ?) "
                    "ON CONFLICT(address) DO UPDATE SET display_name=excluded.display_name, "
                    "source_rank=excluded.source_rank, source_roi=excluded.source_roi, "
                    "source_as_of=excluded.source_as_of, active=1, "
                    "last_seen_at=excluded.last_seen_at",
                    (addr, display_name, rank, roi, as_of, as_of))

    def deactivate_missing(self, seen: set[str]) -> list[str]:
        """把不在 `seen` 內的目前 active 候選標記 `active=0`。回傳被停用的地址清單
        （Task 3.5 B(1)：呼叫端用這份清單對每個退池地址 `delete_jobs`，停止一切
        對它的上游支出）。"""
        norm_seen = {_norm(a) for a in seen}
        with self._lock, self._db:
            if not norm_seen:
                dropped = [r[0] for r in self._db.execute(
                    "SELECT address FROM candidate WHERE active=1").fetchall()]
                self._db.execute("UPDATE candidate SET active=0 WHERE active=1")
                return dropped
            placeholders = ",".join("?" * len(norm_seen))
            dropped = [r[0] for r in self._db.execute(
                f"SELECT address FROM candidate WHERE active=1 "
                f"AND address NOT IN ({placeholders})", tuple(norm_seen)).fetchall()]
            self._db.execute(
                f"UPDATE candidate SET active=0 WHERE active=1 "
                f"AND address NOT IN ({placeholders})",
                tuple(norm_seen))
        return dropped

    def active_candidates(self) -> list[Candidate]:
        with self._lock, self._db:
            rows = self._db.execute(
                "SELECT address, display_name, source_rank, source_roi, source_as_of, "
                "active, last_seen_at FROM candidate WHERE active=1 ORDER BY address"
            ).fetchall()
        return [Candidate(address=r[0], display_name=r[1], source_rank=r[2], source_roi=r[3],
                           source_as_of=r[4], active=bool(r[5]), last_seen_at=r[6])
                for r in rows]

    def is_active(self, address: str) -> bool:
        """候選是否仍在池內（`active=1`）；地址未知 → `False`（Task 3.5 B(2)：
        續排前查此方法，退池即停止一切上游支出）。"""
        addr = _norm(address)
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT active FROM candidate WHERE address=?", (addr,)).fetchone()
        return row is not None and bool(row[0])

    def admission_counts(self) -> tuple[int, int]:
        """`(refresh_job 總數, active 候選數)`——同一 lock 內兩個 COUNT，供
        scheduler／`app.py` 的準入上限判斷共用同一份口徑（Task 3.5 A）。"""
        with self._lock, self._db:
            jobs = self._db.execute("SELECT COUNT(*) FROM refresh_job").fetchone()[0]
            active_n = self._db.execute(
                "SELECT COUNT(*) FROM candidate WHERE active=1").fetchone()[0]
        return jobs, active_n

    def count_with_payload(self, endpoint: str) -> int:
        """目前 active 候選中，`endpoint` 快取已有非 NULL payload 的數量
        （`ExplorePublisher` 的發布門檻用，Task 3.5 C）。`COUNT(DISTINCT
        e.address)`（Task 3.6 C，S4 修法）——同一地址可能有多個 `params_fp`
        （目前皆為 `''`，未來多 dex／參數指紋擴充時）的列，只算一次候選，
        不是每個 `params_fp` 各算一筆。`AND e.params_fp = ''`（Task 3.7 C，W2
        修法）：與 `compose_rows` 實際讀的基礎（`store.get_cache(addr, endpoint)`
        預設 `params_fp=""`）對齊（工程原則 #1）——否則輸入端門檻可能因為
        非預設 `params_fp` 的 payload 而誤判已覆蓋，但 compose 讀的是另一個
        基礎，讀不到任何東西。"""
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT COUNT(DISTINCT e.address) FROM endpoint_cache e JOIN candidate c "
                "USING(address) WHERE e.endpoint=? AND e.payload IS NOT NULL "
                "AND e.params_fp = '' AND c.active=1", (endpoint,)).fetchone()
        return row[0]

    # --- endpoint_cache ---
    def get_cache(self, address: str, endpoint: str, params_fp: str = "") -> CacheEntry | None:
        addr = _norm(address)
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT address, endpoint, params_fp, payload, fetched_at, refresh_after, "
                "last_error, last_error_at FROM endpoint_cache "
                "WHERE address=? AND endpoint=? AND params_fp=?",
                (addr, endpoint, params_fp)).fetchone()
        if row is None:
            return None
        payload = json.loads(row[3]) if row[3] is not None else None
        return CacheEntry(address=row[0], endpoint=row[1], params_fp=row[2], payload=payload,
                           fetched_at=row[4], refresh_after=row[5], last_error=row[6],
                           last_error_at=row[7])

    def put_cache_ok(self, address: str, endpoint: str, payload: Any, fetched_at: float,
                      refresh_after: float, *, params_fp: str = "") -> None:
        addr = _norm(address)
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO endpoint_cache (address, endpoint, params_fp, payload, "
                "fetched_at, refresh_after, last_error, last_error_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL) "
                "ON CONFLICT(address, endpoint, params_fp) DO UPDATE SET "
                "payload=excluded.payload, fetched_at=excluded.fetched_at, "
                "refresh_after=excluded.refresh_after, last_error=NULL, last_error_at=NULL",
                (addr, endpoint, params_fp, json.dumps(payload), fetched_at, refresh_after))

    def put_cache_error(self, address: str, endpoint: str, err: str, at: float,
                         next_after: float, *, params_fp: str = "") -> None:
        """只動 `refresh_after`／`last_error`／`last_error_at`——**不動** `payload`／`fetched_at`
        （spec 條件 4：失敗不能讓已成功過的資料倒退成缺值）。列不存在則補一列
        `payload=NULL, fetched_at=NULL`（代表「從未成功過」）。"""
        addr = _norm(address)
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO endpoint_cache (address, endpoint, params_fp, payload, "
                "fetched_at, refresh_after, last_error, last_error_at) "
                "VALUES (?, ?, ?, NULL, NULL, ?, ?, ?) "
                "ON CONFLICT(address, endpoint, params_fp) DO UPDATE SET "
                "refresh_after=excluded.refresh_after, last_error=excluded.last_error, "
                "last_error_at=excluded.last_error_at",
                (addr, endpoint, params_fp, next_after, err, at))

    # --- fills / fills_sync ---
    def insert_fills_page(self, address: str, fills: list[dict],
                           checkpoint: FillsSyncState) -> int:
        """同一 transaction 內 `INSERT OR IGNORE` 全部 fills＋`INSERT OR REPLACE`
        （實作用 UPSERT）checkpoint；任何例外（缺欄位、非法 completeness）→ 整個 transaction
        rollback，fills 與 checkpoint 都不落地。回傳實際新增的 fills 筆數（用每筆
        `INSERT OR IGNORE` 的 `rowcount` 加總，PK 撞到就是 0）。"""
        addr = _norm(address)
        new_count = 0
        with self._lock, self._db:
            for f in fills:
                coin = f["coin"]
                tid = int(f["tid"])
                time_ms = int(f["time"])
                raw = json.dumps(f, separators=(",", ":"))
                cur = self._db.execute(
                    "INSERT OR IGNORE INTO fills (address, coin, tid, time_ms, raw) "
                    "VALUES (?, ?, ?, ?, ?)", (addr, coin, tid, time_ms, raw))
                new_count += cur.rowcount
            payload = asdict(checkpoint)
            payload["address"] = addr
            self._db.execute(
                "INSERT INTO fills_sync (address, window_start_ms, window_end_ms, cursor_ms, "
                "synced_through_ms, observed_from_ms, observed_to_ms, completeness, reason, "
                "pages_done, fills_in_window, params_fp, updated_at, last_error) "
                "VALUES (:address, :window_start_ms, :window_end_ms, :cursor_ms, "
                ":synced_through_ms, :observed_from_ms, :observed_to_ms, :completeness, "
                ":reason, :pages_done, :fills_in_window, :params_fp, :updated_at, :last_error) "
                "ON CONFLICT(address) DO UPDATE SET "
                "window_start_ms=excluded.window_start_ms, "
                "window_end_ms=excluded.window_end_ms, cursor_ms=excluded.cursor_ms, "
                "synced_through_ms=excluded.synced_through_ms, "
                "observed_from_ms=excluded.observed_from_ms, "
                "observed_to_ms=excluded.observed_to_ms, completeness=excluded.completeness, "
                "reason=excluded.reason, pages_done=excluded.pages_done, "
                "fills_in_window=excluded.fills_in_window, params_fp=excluded.params_fp, "
                "updated_at=excluded.updated_at, last_error=excluded.last_error",
                payload)
        return new_count

    def get_fills(self, address: str, start_ms: int, end_ms: int) -> list[dict]:
        addr = _norm(address)
        with self._lock, self._db:
            rows = self._db.execute(
                "SELECT raw FROM fills WHERE address=? AND time_ms>=? AND time_ms<=? "
                "ORDER BY time_ms ASC", (addr, start_ms, end_ms)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def get_sync(self, address: str) -> FillsSyncState | None:
        addr = _norm(address)
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT address, window_start_ms, window_end_ms, cursor_ms, "
                "synced_through_ms, observed_from_ms, observed_to_ms, completeness, reason, "
                "pages_done, fills_in_window, updated_at, last_error, params_fp "
                "FROM fills_sync WHERE address=?", (addr,)).fetchone()
        if row is None:
            return None
        return FillsSyncState(*row)

    # --- refresh_job ---
    def enqueue(self, key: str, address: str | None, kind: str, priority: int,
                next_attempt_at: float) -> bool:
        """不存在 → INSERT 回 True；已存在 → 只調高優先級／提早時間（`MIN`），回 False。"""
        addr = _norm(address) if address is not None else None
        with self._lock, self._db:
            row = self._db.execute("SELECT 1 FROM refresh_job WHERE key=?", (key,)).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO refresh_job (key, address, kind, priority, created_at, "
                    "next_attempt_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (key, addr, kind, priority, self._now(), next_attempt_at))
                return True
            self._db.execute(
                "UPDATE refresh_job SET priority=MIN(priority, ?), "
                "next_attempt_at=MIN(next_attempt_at, ?) WHERE key=?",
                (priority, next_attempt_at, key))
            return False

    def delete_jobs(self, address: str) -> int:
        """刪除某地址所有 `refresh_job`（退池即刪，Task 3.5 B(1)：C1 修法——
        不刪的話 job 永不消失，預算漏給非候選、準入上限被洩漏的 job 卡死、
        `purge` 的 `NOT EXISTS(refresh_job)` 條件永久卡住）。回傳刪除筆數。"""
        addr = _norm(address)
        with self._lock, self._db:
            cur = self._db.execute("DELETE FROM refresh_job WHERE address=?", (addr,))
        return cur.rowcount

    def due_jobs_count(self, now: float) -> int:
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT COUNT(*) FROM refresh_job WHERE next_attempt_at <= ? "
                "AND (lease_until IS NULL OR lease_until < ?)", (now, now)).fetchone()
        return row[0]

    def claim_due(self, now: float, owner: str, lease_s: float, *,
                  kinds: tuple[str, ...] | None = None) -> Job | None:
        """單一條件式 `UPDATE ... RETURNING`：挑最早到期且無有效 lease 的一列，
        設 `lease_until`／`lease_owner`、`fencing+=1`。SQLite ≥ 3.35 支援 RETURNING
        （本環境 3.53.1，見驗收證據）。

        Task 7.4b（2026-09-21，fills 類別級飢餓修法）：
        - `kinds`：只在指定的 kind 集合內挑選（`AND kind IN (...)`）——scheduler
          用它區分「這一輪只領 fills」還是「這一輪只領基礎類別」，`None` 維持
          舊行為（不限定，向後相容既有呼叫端與測試）。
        - 排序改為等待時間加權：`priority - MIN(3, floor((now - next_attempt_at)
          / 600))`——每多等 10 分鐘、有效 priority 降一級（數字越小越優先），
          最多降 3 級，避免同一 priority 內先到期的 job 被持續插隊的新 job
          永久排擠（正式機觀測：hot 地址不斷有新 job 到期，讓老 job 一直排不到）。
          `CAST(... AS INTEGER)` 對非負值等於 floor（本查詢只會挑
          `next_attempt_at <= now` 的列，差值恆 >= 0）。
        """
        kind_filter = ""
        kind_params: tuple = ()
        if kinds:
            placeholders = ",".join("?" * len(kinds))
            kind_filter = f" AND kind IN ({placeholders})"
            kind_params = tuple(kinds)
        with self._lock, self._db:
            row = self._db.execute(
                "UPDATE refresh_job SET lease_until=?, lease_owner=?, fencing=fencing+1 "
                "WHERE key = (SELECT key FROM refresh_job WHERE next_attempt_at <= ? "
                "AND (lease_until IS NULL OR lease_until < ?)" + kind_filter +
                " ORDER BY (priority - MIN(3, CAST((? - next_attempt_at) / 600 AS INTEGER))), "
                "next_attempt_at LIMIT 1) "
                "RETURNING key, address, kind, priority, created_at, next_attempt_at, "
                "attempts, lease_until, lease_owner, fencing, last_error",
                (now + lease_s, owner, now, now, *kind_params, now)).fetchone()
        if row is None:
            return None
        return Job(*row)

    def due_count(self, kind: str, now: float) -> int:
        """`kind` 目前到期（`next_attempt_at <= now`）且無有效 lease 的工作筆數
        （Task 7.4b：scheduler 用它判斷「fills 有沒有待處理」）。"""
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT COUNT(*) FROM refresh_job WHERE kind=? AND next_attempt_at<=? "
                "AND (lease_until IS NULL OR lease_until < ?)", (kind, now, now)).fetchone()
        return row[0]

    def rebalance_overdue(self, kind: str, now: float, period_s: float,
                          spread_fn: Callable[[str], float]) -> int:
        """把 `kind` 且逾期超過一個週期（`next_attempt_at < now - period_s`）的
        job 重設到 `now + spread_fn(address)`（Task 7.4b：週期改長之後，舊積壓
        若不重排會先把新週期的額度占滿；未逾期的 job 不動、lease 不動）。回傳
        被重排的筆數。地址為 `NULL`（例如全域性 kind，本模組目前無此案例）時
        傳 `key` 給 `spread_fn`，避免 `spread_fn(None)` 出錯。"""
        cutoff = now - period_s
        with self._lock, self._db:
            rows = self._db.execute(
                "SELECT key, address FROM refresh_job WHERE kind=? AND next_attempt_at<?",
                (kind, cutoff)).fetchall()
            for key, address in rows:
                next_at = now + spread_fn(address if address is not None else key)
                self._db.execute(
                    "UPDATE refresh_job SET next_attempt_at=? WHERE key=?", (next_at, key))
        return len(rows)

    def complete(self, job: Job, fencing: int) -> bool:
        with self._lock, self._db:
            cur = self._db.execute(
                "DELETE FROM refresh_job WHERE key=? AND fencing=?", (job.key, fencing))
        return cur.rowcount == 1

    def reschedule(self, job: Job, fencing: int, next_attempt_at: float, err: str | None = None,
                   *, bump_attempts: bool = True) -> bool:
        with self._lock, self._db:
            cur = self._db.execute(
                "UPDATE refresh_job SET lease_until=NULL, lease_owner=NULL, "
                "next_attempt_at=?, attempts=attempts+?, last_error=? "
                "WHERE key=? AND fencing=?",
                (next_attempt_at, 1 if bump_attempts else 0, err, job.key, fencing))
        return cur.rowcount == 1

    def set_sync_error(self, address: str, err: str, at: float) -> None:
        """`fills` kind 隔離時的錯誤落地（Task 3.1 scheduler 用；2.1 未涵蓋，
        本 task 卡片允許新增的最小方法）：只 UPDATE `last_error`／`updated_at`，
        列不存在（從未成功跑過一輪 `plan_page`/`apply_page`）則不動——與
        `put_cache_error` 對「從未成功過」補一列的語意不同，`fills_sync` 沒有
        「只有錯誤沒有游標」這種中間狀態可插入。"""
        addr = _norm(address)
        with self._lock, self._db:
            self._db.execute(
                "UPDATE fills_sync SET last_error=?, updated_at=? WHERE address=?",
                (err, at, addr))

    def set_sync_reason(self, address: str, reason: str) -> None:
        """Task 7.5 點 3／7.6 點 7：留存邊界探測結果落地——只 UPDATE `reason`，
        **不動** `updated_at`（Task 7.6 修正：該欄對外＝`last_success_at`＝
        最近一次「抓頁成功」的時間，探測不是抓頁，不該讓它看起來像剛抓過
        一頁）；也不動游標／completeness／其他欄位（探測結果只補強證據描述，
        不改變 completeness 本身的判定）。列不存在則不動（同 `set_sync_error`
        慣例：探測只會在一輪 `apply_page` 已經跑完、`fills_sync` 列必然存在
        之後才呼叫，這裡的「不存在則不動」是防禦，不是預期路徑）。"""
        addr = _norm(address)
        with self._lock, self._db:
            self._db.execute(
                "UPDATE fills_sync SET reason=? WHERE address=?", (reason, addr))

    def oldest_due_at(self, now: float) -> float | None:
        """目前到期（`next_attempt_at <= now`）的工作中最早的到期時刻；沒有到期
        工作回 `None`（scheduler `status()` 的 `oldest_due_age_s` 用）。"""
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT MIN(next_attempt_at) FROM refresh_job WHERE next_attempt_at <= ?",
                (now,)).fetchone()
        return row[0]

    # --- maintenance ---
    def purge(self, now: float, *, candidate_keep_s: float = 7 * 86400,
              fills_keep_s: float = 35 * 86400) -> dict[str, int]:
        """刪除已停用超過 `candidate_keep_s` 的候選（及其 endpoint_cache／fills／
        fills_sync）——但若該地址仍有 `refresh_job` 列則跳過（避免刪掉正在跑的工作依賴的
        資料）。另外刪除超過 `fills_keep_s` 的 fills，但保留仍落在該地址目前同步視窗內
        （`time_ms >= fills_sync.window_start_ms`）的列。回傳各表刪除筆數。"""
        cutoff_candidate = now - candidate_keep_s
        cutoff_fills_ms = int((now - fills_keep_s) * 1000)
        counts = {"candidate": 0, "endpoint_cache": 0, "fills": 0, "fills_sync": 0}
        with self._lock, self._db:
            stale = [r[0] for r in self._db.execute(
                "SELECT c.address FROM candidate c WHERE c.active=0 AND c.last_seen_at < ? "
                "AND NOT EXISTS (SELECT 1 FROM refresh_job j WHERE j.address = c.address)",
                (cutoff_candidate,)).fetchall()]
            for addr in stale:
                cur = self._db.execute("DELETE FROM endpoint_cache WHERE address=?", (addr,))
                counts["endpoint_cache"] += cur.rowcount
                cur = self._db.execute("DELETE FROM fills WHERE address=?", (addr,))
                counts["fills"] += cur.rowcount
                cur = self._db.execute("DELETE FROM fills_sync WHERE address=?", (addr,))
                counts["fills_sync"] += cur.rowcount
                cur = self._db.execute("DELETE FROM candidate WHERE address=?", (addr,))
                counts["candidate"] += cur.rowcount
            cur = self._db.execute(
                "DELETE FROM fills WHERE time_ms < ? AND NOT EXISTS ("
                "SELECT 1 FROM fills_sync s WHERE s.address = fills.address "
                "AND fills.time_ms >= s.window_start_ms)",
                (cutoff_fills_ms,))
            counts["fills"] += cur.rowcount
        return counts

    def stats(self) -> dict:
        with self._lock, self._db:
            out: dict[str, Any] = {}
            for t in ("candidate", "endpoint_cache", "fills", "fills_sync", "refresh_job"):
                out[t] = self._db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            out["completeness"] = dict(self._db.execute(
                "SELECT completeness, COUNT(*) FROM fills_sync GROUP BY completeness"
            ).fetchall())
        return out
