"""src/spark/publicapi/explore_store.py
Explore 排行榜的持久化資料層（spec `docs/superpowers/specs/2026-09-20-hl-leaderboard-refactor-spec.md`
§7 六種資料、§9.1 lease／fencing／dedupe／admission）。

純資料層：只有 SQL 與 dataclass 轉換，**零網路、零業務判斷**。P3 scheduler、P4 publisher
只透過本模組讀寫 `explore.db`（SQLite WAL）。

七張表（Task 7.9b 新增 `fills_scan`）：
- `candidate`：候選池快照（來自 stats-data leaderboard，每輪覆蓋 rank/roi，`active` 標記本輪
  是否仍在池內；被踢出的候選不立刻刪，交給 `purge` 依保留期處理）。
- `endpoint_cache`：每地址每 endpoint（portfolio／clearinghouseState／ledger）一份最新結果，
  `payload` 為 NULL 代表從未成功過；`put_cache_error` 只動 refresh_after／last_error*，
  刻意不覆寫上一次成功的 `payload`／`fetched_at`（spec 條件 4：失敗不能讓資料倒退成缺值）。
- `fills`：原始 HL fill JSON（`raw`，字串原樣、不轉 float，PK 天然去重）。
- `fills_sync`：**增量軌**（Task 7.9b 起——只負責持續延伸 `synced_through_ms`，
  永不被重掃覆蓋；`completeness`／`reason` 改為「由最近一次完成的 `fills_scan`
  透過 CAS 寫入」，不是增量軌自己判定的）。
- `fills_scan`（Task 7.9b 新增）：**遍歷軌**——每次全區間遍歷（`initial`／
  `partial_rescan`／`verify`）各自一個不可重用的 `scan_id`（uuid4），一個地址
  同時最多一個 `status='running'` 的列。`fills_sync.scan_id` 指向「最近一次
  完成」的那一筆，供對外契約的 `fills_coverage.evidence` 回溯查詢用。
- `refresh_job`：待辦工作佇列，lease＋fencing 防重複執行（`claim_due`／`complete`／`reschedule`）。

Transaction 慣例：全模組沿用 `store.py`（`ApiStore`）既有寫法——預設 `isolation_level`
（非 None）＋`with self._db:` 隱式 transaction（進入自動 BEGIN、正常結束 COMMIT、例外
ROLLBACK），寫入方法一律 `with self._lock, self._db:`。這是本 task 卡片給的兩個選項
（isolation_level=None 手動 BEGIN IMMEDIATE／COMMIT，或沿用隱式 transaction）之一，選擇
理由：`threading.Lock` 已在 Python 層序列化所有寫入，SQLite 層不需要 BEGIN IMMEDIATE
搶鎖來避免多寫入者競爭；沿用既有 `ApiStore` 慣例可讓兩個 store 模組風格一致。

CAS（Task 7.9b B3／B4）：`complete_scan`／`apply_probe_result` 都用「先 UPDATE 再檢查
rowcount」的模式，rowcount 不如預期時在 `with self._db:` 區塊內丟一個內部例外觸發
ROLLBACK——`sqlite3` 的隱式 transaction 沒有「條件式回滾」語法，用例外驅動回滾是
標準做法（`with self._db:` 捕捉到例外會自動 ROLLBACK，呼叫端再把這個內部例外轉換成
布林回傳，不逸出）。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Task 7.5（2026-09-21，使用者裁決：complete 判定的證據與標示）：completeness
# 的 reason 碼——`complete` 也要有原因，不再是「留白代表沒問題」。
# `REASON_COUNT_BELOW_RETENTION_THRESHOLD`：現行判準（`explore_fills_sync.apply_scan_page`
# 短頁收尾、本輪觀測筆數低於 `RETENTION_SAFETY_THRESHOLD`）；
# `REASON_RETENTION_BOUNDARY_VERIFIED`：留存邊界探測（`explore_scheduler._run_probe`）
# 實測區間起點之前一天仍有可查成交，代表留存邊界早於窗口起點，比門檻推論更強的證據。
# 兩個 reason 碼與（`ExploreStore`）schema migration 共用，放在資料層而非
# `explore_fills_sync`（後者反向 import 本模組的 `FillsSyncState`，避免循環 import）。
REASON_COUNT_BELOW_RETENTION_THRESHOLD = "count_below_retention_threshold"
REASON_RETENTION_BOUNDARY_VERIFIED = "retention_boundary_verified"

# Task 7.7 W3（2026-09-21，7.6 複審）：探測回空頁只證明「這一次探測沒看到更早
# 的成交」，門檻推論（`REASON_COUNT_BELOW_RETENTION_THRESHOLD`）本身沒有變得
# 更弱也沒有變強——但若不記下「已經探過」，探測條件（`reason ==
# REASON_COUNT_BELOW_RETENTION_THRESHOLD`）會讓同一次遍歷被重探。這個第三個
# reason 碼把「探過、沒有更早成交、無法升級」記下來，探測候選查詢天然排除它。
REASON_PROBE_NO_EARLIER_FILLS = "count_below_retention_threshold_probe_empty"

# schema_version：1（初版）→2（Task 7.5：`fills_sync.params_fp` 欄位＋既有
# complete／reason=NULL 列補標）→3（Task 7.9b：遍歷軌／增量軌分離，見
# `_migrate_v2_to_v3`）。
_SCHEMA_VERSION = 3

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
  window_start_ms INTEGER NOT NULL, window_end_ms INTEGER NOT NULL,  -- 增量軌本輪查詢區間
  cursor_ms INTEGER NOT NULL,          -- 增量軌下一頁 startTime（inclusive，含重疊）
  synced_through_ms INTEGER,           -- 增量軌已確認遍歷到此
  observed_from_ms INTEGER, observed_to_ms INTEGER,
  completeness TEXT NOT NULL DEFAULT 'backfilling'   -- backfilling|partial|complete
      CHECK (completeness IN ('backfilling', 'partial', 'complete')),
  reason TEXT, pages_done INTEGER NOT NULL DEFAULT 0,
  fills_in_window INTEGER NOT NULL DEFAULT 0,
  params_fp TEXT NOT NULL DEFAULT '',   -- Task 7.5：查詢參數留證（見 explore_fills_sync.PARAMS_FP）
  updated_at REAL NOT NULL, last_error TEXT,
  -- Task 7.9b：增量軌起點（B1）／最近一次完成的遍歷（B3）／證據狀態（B1／B5）。
  inc_from_ms INTEGER,
  scan_id TEXT,
  evidence_unknown INTEGER NOT NULL DEFAULT 0,
  coverage_gap INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS fills_scan (
  scan_id TEXT PRIMARY KEY,
  address TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('initial', 'partial_rescan', 'verify')),
  window_start_ms INTEGER NOT NULL, window_end_ms INTEGER NOT NULL,
  cursor_ms INTEGER NOT NULL,
  pages_done INTEGER NOT NULL DEFAULT 0,
  fills_in_window INTEGER NOT NULL DEFAULT 0,
  observed_from_ms INTEGER, observed_to_ms INTEGER,
  status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done')),
  result TEXT, reason TEXT,
  started_at REAL NOT NULL, finished_at REAL, last_error TEXT,
  params_fp TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS fills_scan_addr_status ON fills_scan(address, status);
CREATE TABLE IF NOT EXISTS refresh_job (
  key TEXT PRIMARY KEY,                -- f"{address}:{kind}"  kind∈portfolio|state|fills|
                                        -- fills_scan|fills_verify|candidates
  address TEXT, kind TEXT NOT NULL, priority INTEGER NOT NULL,   -- 0 最高
  created_at REAL NOT NULL, next_attempt_at REAL NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_until REAL, lease_owner TEXT, fencing INTEGER NOT NULL DEFAULT 0,
  last_error TEXT);
CREATE INDEX IF NOT EXISTS job_due ON refresh_job(next_attempt_at, priority);
"""


def _norm(address: str) -> str:
    return address.lower()


def _migration_spread_s(address: str, period_s: float) -> float:
    """Task 7.9b B5：遷移時把新入列的 `fills_verify` job 均勻攤在
    `[0, period_s)` 內（同 `explore_scheduler._spread` 的雜湊手法，這裡獨立
    複製一份極小函式——`explore_store` 不得 import `explore_scheduler`
    （會循環：scheduler 已 import `explore_store`），一次性 migration 用途
    不需要與排程端共用同一份實作。地址理論上恆為合法 hex（40 碼），但防禦性地
    容忍非 hex 尾碼（例如測試 fixture）——退回 `hash()`，不讓遷移本身因為
    無關的格式問題整段失敗。"""
    if period_s <= 0:
        return 0.0
    try:
        n = int(address[-8:], 16)
    except ValueError:
        n = abs(hash(address))
    return float(n % int(period_s))


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
    """對映 `fills_sync` 一列——**增量軌**（Task 7.9b 起）：`window_start_ms`／
    `window_end_ms`／`cursor_ms`／`synced_through_ms`／`observed_*`／
    `pages_done`／`fills_in_window`／`params_fp` 描述的是增量軌自己的查詢區間
    與進度，`completeness`／`reason` 則是「最近一次完成的 `fills_scan`」透過
    `ExploreStore.complete_scan` 的 CAS 寫入的結果，增量軌本身從不判定它們
    （見 `explore_fills_sync.plan_incremental`／`apply_incremental_page`）。"""
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
    # Task 7.5：查詢參數留證（例如 `"aggregateByTime=<omitted>"`）——放在
    # dataclass 最後一個欄位並給預設值 `""`，讓既有呼叫端（測試 fixture／舊資料）
    # 不必逐一改動就能繼續建構本類別（frozen dataclass 的欄位預設值規則：
    # 有預設值的欄位必須排在最後）。
    params_fp: str = ""
    # Task 7.9b B1：增量軌起點（genesis）——覆蓋連續性檢查
    # （`scan.window_end_ms >= inc_from_ms`）的基準。
    inc_from_ms: int | None = None
    # Task 7.9b B3：最近一次完成的 `fills_scan.scan_id`，`fills_coverage.evidence`
    # 據此回查那一次遍歷的窗口／完成時間／reason。
    scan_id: str | None = None
    # Task 7.9b B1／B5：`True` 代表目前的 completeness／reason 來自遷移前
    # 缺乏可追溯窗口證據的舊資料（或尚未完成的核驗），對外必須降級為 partial。
    evidence_unknown: bool = False
    # Task 7.9b B1：`True` 代表最近一次完成的遍歷沒有伸進增量軌起點
    # （`scan.window_end_ms < inc_from_ms`），覆蓋不連續，對外必須降級為 partial。
    coverage_gap: bool = False


@dataclass(frozen=True)
class FillsScan:
    """對映 `fills_scan` 一列——**遍歷軌**（Task 7.9b）：一次全區間遍歷
    （`initial`／`partial_rescan`／`verify`）的完整生命週期，`scan_id`
    （uuid4）一經建立即不可重用。"""
    scan_id: str
    address: str
    kind: str                      # initial | partial_rescan | verify
    window_start_ms: int
    window_end_ms: int
    cursor_ms: int
    pages_done: int
    fills_in_window: int
    observed_from_ms: int | None
    observed_to_ms: int | None
    status: str                    # running | done
    result: str | None             # complete | partial | None（running 時）
    reason: str | None
    started_at: float
    finished_at: float | None
    last_error: str | None
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


class _CasMiss(Exception):
    """Task 7.9b：CAS 未命中時用來觸發 `with self._db:` 隱式 transaction
    的 ROLLBACK（見模組檔頭）。只在 store 內部使用，不逸出到呼叫端。"""


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
                if row[0] < 2:
                    self._migrate_v1_to_v2()
                if row[0] < 3:
                    self._migrate_v2_to_v3()
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
        `explore_fills_sync` 檔頭）。

        Task 7.6 點 9（複審 S3 修法，docstring 不實）：呼叫端（`__init__`）雖然
        包在 `with self._lock, self._db:` 底下，但這**不代表**下面兩步 SQL 是同一個
        transaction——`ALTER TABLE` 是 DDL，Python `sqlite3` 遇到 DDL 會自動
        COMMIT 目前的隱式 transaction（stdlib 行為，與呼叫端有沒有包 `with` 無關），
        所以本 migration 實際上**不在**一個 transaction 內執行，中途（例如
        `ALTER TABLE` 成功、行程在 `UPDATE` 之前被殺）可能停在中間態。這是安全的，
        因為兩步都各自冪等：`ALTER TABLE` 前先查 `PRAGMA table_info` 判斷欄位是否
        已存在（存在則跳過，不會重複 `ALTER` 出錯）；`UPDATE` 的 `WHERE` 條件式
        （`completeness='complete' AND reason IS NULL`）本身就是「還沒補過」的
        判斷，重跑不會誤傷已經補過（`reason` 非 NULL）的列。"""
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(fills_sync)").fetchall()}
        if "params_fp" not in cols:
            self._db.execute(
                "ALTER TABLE fills_sync ADD COLUMN params_fp TEXT NOT NULL DEFAULT ''")
        self._db.execute(
            "UPDATE fills_sync SET reason=? WHERE completeness='complete' AND reason IS NULL",
            (REASON_COUNT_BELOW_RETENTION_THRESHOLD,))

    def _migrate_v2_to_v3(self) -> None:
        """Task 7.9b B5：遍歷軌／增量軌分離。同 `_migrate_v1_to_v2`——`ALTER
        TABLE`／`CREATE TABLE` 是 DDL，各步各自冪等（欄位／表已存在則跳過），
        不假設整段在同一個 transaction 內（同上函式 docstring 的教訓）。

        逐列處理既有 `fills_sync`（v2 形狀，尚無新欄位）：
        - `completeness == 'backfilling'`（首輪回補尚未完成）：舊列本身就是
          「一次尚未完成的遍歷」——轉成一筆 `status='running'` 的 `initial`
          `fills_scan`（游標／窗口／已抓頁數／觀測範圍原樣沿用），並把原本
          指向這個地址的 `refresh_job`（`key=f"{addr}:fills"`，舊語意是
          「backfill 進度」）**改名**為 `fills_scan` kind——它接下來要推進的
          是遍歷軌，不是增量軌。同時另外插入一筆全新的 `fills` 增量 job
          （`next_attempt_at=now`，讓它下一輪自然算出 noop／到期）。

          2026-09-21 主線程二次裁決（正式機 287 列快照實跑抓到的缺口）：
          增量軌（`fills_sync` 本列）的 `inc_from_ms`／`synced_through_ms`／
          `cursor_ms`／`window_end_ms` **不得**設為遷移當下的 `now`——那會在
          「舊 scan 的 `window_end_ms`（幾小時前開輪時的值）」與「遷移當下
          的 `now`」之間留下一段沒有任何一軌會抓的成交（scan 完成時
          `scan.window_end_ms < inc_from_ms` 被判 `coverage_gap`，對外
          `partial` 直到 24 小時後 `partial_rescan` 才修復——這是遷移造成的
          真實缺口，不該靠重掃收拾）。改為 `inc_from_ms = synced_through_ms
          = cursor_ms = win_end`（沿用該 running scan 的 `window_end_ms`，
          `fills_sync.window_end_ms` 同步設為同一個值，維持「同一列自身欄位
          彼此一致」）——增量軌從 scan 窗口末端起算，第一次增量輪會抓
          `[win_end-1, now]` 把這段缺口補上，與 `bootstrap_address_fills`
          「新地址 `scan.window_end == inc_from`」的連續性原則一致。
        - `completeness in ('complete', 'partial')`：舊列的 `window_*`／
          `cursor_ms`／`synced_through_ms`／`observed_*`／`pages_done`／
          `fills_in_window` 這些欄位**原樣保留**，直接變成增量軌自己的欄位
          （不需要改值——這批欄位在 v2 時期本來就是「目前這一輪」的查詢
          進度，遷移後語意等價於「增量軌目前這一輪」）；`inc_from_ms` 設為
          舊 `synced_through_ms`（B5 字面：「增量軌從現在的前沿起算」）。
          另外建一筆 `status='done'` 的歷史 `fills_scan`（`kind='initial'`，
          窗口／reason／完成時間沿用舊列），`fills_sync.scan_id` 指向它：
          - `reason == REASON_RETENTION_BOUNDARY_VERIFIED`（有可追溯窗口與
            探測證據）→ `evidence_unknown=0`，不需要核驗。
          - 其他 reason（含 NULL、`retention_limit`、`same_ms_overflow`……）
            → `evidence_unknown=1`，對外降級為 partial（見
            `explore_fills_sync.external_coverage_state`），並入列一筆
            `fills_verify` job，`next_attempt_at` 在遷移當下起 48 小時內
            均勻攤開（`_migration_spread_s`，避免全部同時到期造成尖峰）。
          `coverage_gap` 一律 0（遷移當下無法回溯判斷，且這批列本來就有
          真實的 `synced_through_ms` 可用，覆蓋連續性由之後的排程正常維護）。

        兩種分支都不刪除／不重建 `fills`（原始成交）表——資料原封不動。"""
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(fills_sync)").fetchall()}
        for col, ddl in (
            ("inc_from_ms", "ALTER TABLE fills_sync ADD COLUMN inc_from_ms INTEGER"),
            ("scan_id", "ALTER TABLE fills_sync ADD COLUMN scan_id TEXT"),
            ("evidence_unknown",
             "ALTER TABLE fills_sync ADD COLUMN evidence_unknown INTEGER NOT NULL DEFAULT 0"),
            ("coverage_gap",
             "ALTER TABLE fills_sync ADD COLUMN coverage_gap INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in cols:
                self._db.execute(ddl)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS fills_scan ("
            "scan_id TEXT PRIMARY KEY, address TEXT NOT NULL, "
            "kind TEXT NOT NULL CHECK (kind IN ('initial', 'partial_rescan', 'verify')), "
            "window_start_ms INTEGER NOT NULL, window_end_ms INTEGER NOT NULL, "
            "cursor_ms INTEGER NOT NULL, pages_done INTEGER NOT NULL DEFAULT 0, "
            "fills_in_window INTEGER NOT NULL DEFAULT 0, "
            "observed_from_ms INTEGER, observed_to_ms INTEGER, "
            "status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done')), "
            "result TEXT, reason TEXT, started_at REAL NOT NULL, finished_at REAL, "
            "last_error TEXT, params_fp TEXT NOT NULL DEFAULT '')")
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS fills_scan_addr_status ON fills_scan(address, status)")

        now = self._now()
        rows = self._db.execute(
            "SELECT address, window_start_ms, window_end_ms, cursor_ms, synced_through_ms, "
            "observed_from_ms, observed_to_ms, completeness, reason, pages_done, "
            "fills_in_window, params_fp, updated_at FROM fills_sync"
        ).fetchall()
        for (addr, win_start, win_end, cursor_ms, synced_through, obs_from, obs_to,
             completeness, reason, pages_done, fills_in_window, params_fp,
             updated_at) in rows:
            if completeness == "backfilling":
                scan_id = uuid.uuid4().hex
                self._db.execute(
                    "INSERT INTO fills_scan (scan_id, address, kind, window_start_ms, "
                    "window_end_ms, cursor_ms, pages_done, fills_in_window, observed_from_ms, "
                    "observed_to_ms, status, result, reason, started_at, finished_at, "
                    "last_error, params_fp) VALUES (?,?,?,?,?,?,?,?,?,?,'running',NULL,NULL,"
                    "?,NULL,NULL,?)",
                    (scan_id, addr, "initial", win_start, win_end, cursor_ms, pages_done,
                     fills_in_window, obs_from, obs_to, updated_at, params_fp))
                # 2026-09-21 主線程二次裁決：增量軌起點＝該 running scan 的
                # `window_end_ms`（`win_end`，舊值，不是遷移當下的 `now`）——
                # 見上方 docstring「正式機 287 列快照實跑抓到的缺口」。
                # `window_start_ms` 維持舊值（`win_start`）不動，僅
                # `window_end_ms` 同步到 `win_end`（增量軌與遍歷軌此刻共享
                # 同一個「目前前沿」，之後兩軌各自推進、互不覆蓋）。
                self._db.execute(
                    "UPDATE fills_sync SET inc_from_ms=?, synced_through_ms=?, cursor_ms=?, "
                    "window_end_ms=?, scan_id=NULL, evidence_unknown=0, "
                    "coverage_gap=0 WHERE address=?",
                    (win_end, win_end, win_end, win_end, addr))
                old_job = self._db.execute(
                    "SELECT priority, next_attempt_at, attempts, created_at FROM refresh_job "
                    "WHERE key=?", (f"{addr}:fills",)).fetchone()
                if old_job is not None:
                    priority, next_attempt_at, attempts, created_at = old_job
                    self._db.execute(
                        "UPDATE refresh_job SET key=?, kind='fills_scan' WHERE key=?",
                        (f"{addr}:fills_scan", f"{addr}:fills"))
                    self._db.execute(
                        "INSERT INTO refresh_job (key, address, kind, priority, created_at, "
                        "next_attempt_at, attempts) VALUES (?, ?, 'fills', ?, ?, ?, 0)",
                        (f"{addr}:fills", addr, priority, created_at, now))
            else:  # complete | partial
                scan_id = uuid.uuid4().hex
                verified = reason == REASON_RETENTION_BOUNDARY_VERIFIED
                self._db.execute(
                    "INSERT INTO fills_scan (scan_id, address, kind, window_start_ms, "
                    "window_end_ms, cursor_ms, pages_done, fills_in_window, observed_from_ms, "
                    "observed_to_ms, status, result, reason, started_at, finished_at, "
                    "last_error, params_fp) VALUES (?,?,?,?,?,?,?,?,?,?,'done',?,?,?,?,NULL,?)",
                    (scan_id, addr, "initial", win_start, win_end, cursor_ms, pages_done,
                     fills_in_window, obs_from, obs_to, completeness, reason, updated_at,
                     updated_at, params_fp))
                self._db.execute(
                    "UPDATE fills_sync SET inc_from_ms=?, scan_id=?, evidence_unknown=?, "
                    "coverage_gap=0 WHERE address=?",
                    (synced_through, scan_id, 0 if verified else 1, addr))
                if not verified:
                    self._db.execute(
                        "INSERT OR IGNORE INTO refresh_job (key, address, kind, priority, "
                        "created_at, next_attempt_at) VALUES (?, ?, 'fills_verify', 4, ?, ?)",
                        (f"{addr}:fills_verify", addr, now,
                         now + _migration_spread_s(addr, 48 * 3600)))

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

    # --- fills / fills_sync（增量軌） ---
    def insert_fills_page(self, address: str, fills: list[dict],
                           checkpoint: FillsSyncState) -> int:
        """同一 transaction 內 `INSERT OR IGNORE` 全部 fills＋`INSERT OR REPLACE`
        （實作用 UPSERT）checkpoint；任何例外（缺欄位、非法 completeness）→ 整個 transaction
        rollback，fills 與 checkpoint 都不落地。回傳實際新增的 fills 筆數（用每筆
        `INSERT OR IGNORE` 的 `rowcount` 加總，PK 撞到就是 0）。

        Task 7.9b：`checkpoint` 是**增量軌**狀態（`explore_fills_sync
        .apply_incremental_page` 的輸出）——`completeness`／`reason`／`scan_id`／
        `evidence_unknown`／`coverage_gap` 這幾個「遍歷軌擁有」的欄位必須原樣
        帶著目前 DB 裡的值一起寫回（呼叫端從 `get_sync` 讀出、`dataclasses.replace`
        只動增量軌自己的欄位，見 `explore_scheduler._run_increment`），本方法
        不做任何欄位級的保護——欄位級保護（CAS）只在 `complete_scan`／
        `apply_probe_result` 這兩個真正會被競爭寫入的路徑才需要。"""
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
            payload["evidence_unknown"] = int(bool(payload["evidence_unknown"]))
            payload["coverage_gap"] = int(bool(payload["coverage_gap"]))
            self._db.execute(
                "INSERT INTO fills_sync (address, window_start_ms, window_end_ms, cursor_ms, "
                "synced_through_ms, observed_from_ms, observed_to_ms, completeness, reason, "
                "pages_done, fills_in_window, params_fp, updated_at, last_error, "
                "inc_from_ms, scan_id, evidence_unknown, coverage_gap) "
                "VALUES (:address, :window_start_ms, :window_end_ms, :cursor_ms, "
                ":synced_through_ms, :observed_from_ms, :observed_to_ms, :completeness, "
                ":reason, :pages_done, :fills_in_window, :params_fp, :updated_at, :last_error, "
                ":inc_from_ms, :scan_id, :evidence_unknown, :coverage_gap) "
                "ON CONFLICT(address) DO UPDATE SET "
                "window_start_ms=excluded.window_start_ms, "
                "window_end_ms=excluded.window_end_ms, cursor_ms=excluded.cursor_ms, "
                "synced_through_ms=excluded.synced_through_ms, "
                "observed_from_ms=excluded.observed_from_ms, "
                "observed_to_ms=excluded.observed_to_ms, completeness=excluded.completeness, "
                "reason=excluded.reason, pages_done=excluded.pages_done, "
                "fills_in_window=excluded.fills_in_window, params_fp=excluded.params_fp, "
                "updated_at=excluded.updated_at, last_error=excluded.last_error, "
                "inc_from_ms=excluded.inc_from_ms, scan_id=excluded.scan_id, "
                "evidence_unknown=excluded.evidence_unknown, "
                "coverage_gap=excluded.coverage_gap",
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
                "pages_done, fills_in_window, updated_at, last_error, params_fp, "
                "inc_from_ms, scan_id, evidence_unknown, coverage_gap "
                "FROM fills_sync WHERE address=?", (addr,)).fetchone()
        if row is None:
            return None
        (address_, window_start_ms, window_end_ms, cursor_ms, synced_through_ms,
         observed_from_ms, observed_to_ms, completeness, reason, pages_done, fills_in_window,
         updated_at, last_error, params_fp, inc_from_ms, scan_id, evidence_unknown,
         coverage_gap) = row
        return FillsSyncState(
            address=address_, window_start_ms=window_start_ms, window_end_ms=window_end_ms,
            cursor_ms=cursor_ms, synced_through_ms=synced_through_ms,
            observed_from_ms=observed_from_ms, observed_to_ms=observed_to_ms,
            completeness=completeness, reason=reason, pages_done=pages_done,
            fills_in_window=fills_in_window, updated_at=updated_at, last_error=last_error,
            params_fp=params_fp, inc_from_ms=inc_from_ms, scan_id=scan_id,
            evidence_unknown=bool(evidence_unknown), coverage_gap=bool(coverage_gap))

    def bootstrap_address_fills(self, address: str, now: float, *, window_start_ms: int,
                                 window_end_ms: int, params_fp: str) -> bool:
        """Task 7.9b B1：新地址一次建立增量軌（`fills_sync`，`inc_from_ms=
        synced_through_ms=now`、`completeness="backfilling"`）＋一個 `initial`
        `fills_scan`（`[window_start_ms, window_end_ms]`）——同一 transaction，
        避免任一方單獨落地留下不一致的一半狀態。`fills_sync` 列已存在（同一
        地址重新入池、或候選抖動重複呼叫）→ 不動、回 `False`；新建 → 回
        `True`（呼叫端據此判斷要不要另外入列 `fills_scan` job，見
        `explore_scheduler._enqueue_address_jobs`）。"""
        addr = _norm(address)
        now_ms = int(now * 1000)
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT 1 FROM fills_sync WHERE address=?", (addr,)).fetchone()
            if row is not None:
                return False
            self._db.execute(
                "INSERT INTO fills_sync (address, window_start_ms, window_end_ms, cursor_ms, "
                "synced_through_ms, observed_from_ms, observed_to_ms, completeness, reason, "
                "pages_done, fills_in_window, params_fp, updated_at, last_error, inc_from_ms, "
                "scan_id, evidence_unknown, coverage_gap) VALUES "
                "(?, ?, ?, ?, ?, NULL, NULL, 'backfilling', NULL, 0, 0, ?, ?, NULL, ?, NULL, "
                "0, 0)",
                (addr, now_ms, now_ms, now_ms, now_ms, params_fp, now, now_ms))
            scan_id = uuid.uuid4().hex
            self._db.execute(
                "INSERT INTO fills_scan (scan_id, address, kind, window_start_ms, "
                "window_end_ms, cursor_ms, pages_done, fills_in_window, observed_from_ms, "
                "observed_to_ms, status, result, reason, started_at, finished_at, last_error, "
                "params_fp) VALUES (?,?,?,?,?,?,0,0,NULL,NULL,'running',NULL,NULL,?,NULL,NULL,"
                "?)",
                (scan_id, addr, "initial", window_start_ms, window_end_ms, window_start_ms,
                 now, params_fp))
        return True

    # --- fills_scan（遍歷軌） ---
    def _scan_from_row(self, row: tuple) -> FillsScan:
        (scan_id, address, kind, window_start_ms, window_end_ms, cursor_ms, pages_done,
         fills_in_window, observed_from_ms, observed_to_ms, status, result, reason,
         started_at, finished_at, last_error, params_fp) = row
        return FillsScan(
            scan_id=scan_id, address=address, kind=kind, window_start_ms=window_start_ms,
            window_end_ms=window_end_ms, cursor_ms=cursor_ms, pages_done=pages_done,
            fills_in_window=fills_in_window, observed_from_ms=observed_from_ms,
            observed_to_ms=observed_to_ms, status=status, result=result, reason=reason,
            started_at=started_at, finished_at=finished_at, last_error=last_error,
            params_fp=params_fp)

    _SCAN_COLUMNS = (
        "scan_id, address, kind, window_start_ms, window_end_ms, cursor_ms, pages_done, "
        "fills_in_window, observed_from_ms, observed_to_ms, status, result, reason, "
        "started_at, finished_at, last_error, params_fp")

    def get_scan(self, scan_id: str) -> FillsScan | None:
        with self._lock, self._db:
            row = self._db.execute(
                f"SELECT {self._SCAN_COLUMNS} FROM fills_scan WHERE scan_id=?",
                (scan_id,)).fetchone()
        return None if row is None else self._scan_from_row(row)

    def get_active_scan(self, address: str) -> FillsScan | None:
        """該地址目前 `status='running'` 的 `fills_scan`（同一地址同時最多一筆，
        由呼叫端 `explore_scheduler._run_scan` 保證——只在此方法回 `None` 時
        才建立新 scan）。"""
        addr = _norm(address)
        with self._lock, self._db:
            row = self._db.execute(
                f"SELECT {self._SCAN_COLUMNS} FROM fills_scan "
                "WHERE address=? AND status='running'", (addr,)).fetchone()
        return None if row is None else self._scan_from_row(row)

    def create_scan(self, address: str, *, kind: str, window_start_ms: int, window_end_ms: int,
                     cursor_ms: int, started_at: float, params_fp: str = "") -> FillsScan:
        addr = _norm(address)
        scan_id = uuid.uuid4().hex
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO fills_scan (scan_id, address, kind, window_start_ms, "
                "window_end_ms, cursor_ms, pages_done, fills_in_window, observed_from_ms, "
                "observed_to_ms, status, result, reason, started_at, finished_at, last_error, "
                "params_fp) VALUES (?,?,?,?,?,?,0,0,NULL,NULL,'running',NULL,NULL,?,NULL,NULL,"
                "?)",
                (scan_id, addr, kind, window_start_ms, window_end_ms, cursor_ms, started_at,
                 params_fp))
        return FillsScan(
            scan_id=scan_id, address=addr, kind=kind, window_start_ms=window_start_ms,
            window_end_ms=window_end_ms, cursor_ms=cursor_ms, pages_done=0, fills_in_window=0,
            observed_from_ms=None, observed_to_ms=None, status="running", result=None,
            reason=None, started_at=started_at, finished_at=None, last_error=None,
            params_fp=params_fp)

    def insert_scan_page(self, address: str, fills: list[dict], scan: FillsScan) -> int:
        """遍歷軌續頁（`done=False`）：落地本頁 fills＋更新 `fills_scan` 進度，
        **不動** `fills_sync`（遍歷完成前，增量軌完全不受影響——B7 (i)）。"""
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
            self._db.execute(
                "UPDATE fills_scan SET cursor_ms=?, pages_done=?, fills_in_window=?, "
                "observed_from_ms=?, observed_to_ms=?, last_error=? WHERE scan_id=?",
                (scan.cursor_ms, scan.pages_done, scan.fills_in_window, scan.observed_from_ms,
                 scan.observed_to_ms, scan.last_error, scan.scan_id))
        return new_count

    def complete_scan(self, address: str, fills: list[dict], scan: FillsScan) -> bool:
        """遍歷軌完成（Task 7.9b B3）：落地終止頁 fills＋標記 `fills_scan`
        `status='done'`＋**CAS** 寫回 `fills_sync`（`UPDATE ... WHERE address=?
        AND (scan_id IS NULL OR scan_id != ?)`——只防同一個 `scan_id` 被重複
        套用兩次，同一地址同時只有一個 running scan，實務上不會有兩個不同
        `scan_id` 競爭同一次 apply，這裡按 plan 字面實作）。覆蓋連續性
        （B1）：`scan.window_end_ms < fills_sync.inc_from_ms` → `coverage_gap=1`。
        `evidence_unknown` 無條件清 0（任何一次完成的遍歷都是新鮮證據，不論
        `initial`／`partial_rescan`／`verify`，也不論 `result` 是 `complete`
        還是 `partial`——B5：「核驗 scan 完成……不強行升級」指的是不強改
        `result`，不是不清除 unknown 旗標）。回傳 CAS 是否命中。"""
        addr = _norm(address)
        with self._lock, self._db:
            for f in fills:
                coin = f["coin"]
                tid = int(f["tid"])
                time_ms = int(f["time"])
                raw = json.dumps(f, separators=(",", ":"))
                self._db.execute(
                    "INSERT OR IGNORE INTO fills (address, coin, tid, time_ms, raw) "
                    "VALUES (?, ?, ?, ?, ?)", (addr, coin, tid, time_ms, raw))
            self._db.execute(
                "UPDATE fills_scan SET status='done', cursor_ms=?, pages_done=?, "
                "fills_in_window=?, observed_from_ms=?, observed_to_ms=?, result=?, reason=?, "
                "finished_at=?, last_error=? WHERE scan_id=?",
                (scan.cursor_ms, scan.pages_done, scan.fills_in_window, scan.observed_from_ms,
                 scan.observed_to_ms, scan.result, scan.reason, scan.finished_at,
                 scan.last_error, scan.scan_id))
            inc_row = self._db.execute(
                "SELECT inc_from_ms FROM fills_sync WHERE address=?", (addr,)).fetchone()
            inc_from_ms = inc_row[0] if inc_row is not None else None
            gap = 1 if (inc_from_ms is not None and scan.window_end_ms < inc_from_ms) else 0
            cur = self._db.execute(
                "UPDATE fills_sync SET completeness=?, reason=?, scan_id=?, coverage_gap=?, "
                "evidence_unknown=0 WHERE address=? AND (scan_id IS NULL OR scan_id != ?)",
                (scan.result, scan.reason, scan.scan_id, gap, addr, scan.scan_id))
        return cur.rowcount == 1

    def apply_probe_result(self, scan_id: str, address: str, *, old_reason: str,
                            new_reason: str) -> bool:
        """留存邊界探測回寫（Task 7.9b B4）：兩個 CAS UPDATE 包在同一 transaction——
        `fills_scan`（`scan_id=? AND reason=?`，只在這筆 scan 的 reason 還是
        探測當下讀到的 `old_reason` 時才改）與 `fills_sync`（`address=? AND
        scan_id=?`，只在該地址目前仍指向這筆 scan 時才改——地址在探測發出
        後、回寫前完成了新的一次遍歷，`fills_sync.scan_id` 會指向新 scan，
        這裡的條件天然為假）。任一 rowcount 不是 1 → 兩邊都不落地（藉由丟
        `_CasMiss` 觸發 `with self._db:` 的 ROLLBACK），回 `False`（呼叫端計
        `probe.stale`）。"""
        addr = _norm(address)
        try:
            with self._lock, self._db:
                cur1 = self._db.execute(
                    "UPDATE fills_scan SET reason=? WHERE scan_id=? AND reason=?",
                    (new_reason, scan_id, old_reason))
                cur2 = self._db.execute(
                    "UPDATE fills_sync SET reason=? WHERE address=? AND scan_id=?",
                    (new_reason, addr, scan_id))
                if cur1.rowcount != 1 or cur2.rowcount != 1:
                    raise _CasMiss()
        except _CasMiss:
            return False
        return True

    def next_probe_candidate(self) -> tuple[str, str] | None:
        """留存邊界探測候選（Task 7.9b B4）：`fills_sync.scan_id` 指向的那筆
        `fills_scan` 結果為 `complete` 且 `reason` 仍是門檻推論（尚未探過、
        `evidence_unknown=0`），依該 scan `finished_at` 最舊者一筆——純 DB
        推導，無記憶體佇列，重啟後從 DB 重新查詢即可繼續（B7 (vii)）。回傳
        `(address, scan_id) | None`。"""
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT s.address, s.scan_id FROM fills_sync s "
                "JOIN fills_scan sc ON sc.scan_id = s.scan_id "
                "JOIN candidate c ON c.address = s.address "
                "WHERE c.active=1 AND s.evidence_unknown=0 AND sc.status='done' "
                "AND sc.result='complete' AND sc.reason=? "
                "ORDER BY sc.finished_at ASC LIMIT 1",
                (REASON_COUNT_BELOW_RETENTION_THRESHOLD,)).fetchone()
        return None if row is None else (row[0], row[1])

    def count_probe_candidates(self) -> int:
        """觀測用（`status()["probe"]["candidates"]`）：目前待探候選數，見
        `next_probe_candidate` 同一份查詢條件。"""
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT COUNT(*) FROM fills_sync s "
                "JOIN fills_scan sc ON sc.scan_id = s.scan_id "
                "JOIN candidate c ON c.address = s.address "
                "WHERE c.active=1 AND s.evidence_unknown=0 AND sc.status='done' "
                "AND sc.result='complete' AND sc.reason=?",
                (REASON_COUNT_BELOW_RETENTION_THRESHOLD,)).fetchone()
        return row[0]

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
          `next_attempt_at <= now` 的列，差值恆 >= 0）。Task 7.9b B3：`verify`
          kind 的job 一律不靠這條路徑取得優先權——排程端只在 `fills`／
          `fills_scan`／基礎類別本輪都沒有到期 job 時才另外用
          `kinds=("fills_verify",)` 呼叫本方法（見 `explore_scheduler
          ._tick_once`），等待加權對它一樣適用但因為呼叫時機已經是「最後
          手段」，不會讓它搶到本該給其他類別的 tick。
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
        列不存在（從未成功跑過一輪增量）則不動——與 `put_cache_error` 對
        「從未成功過」補一列的語意不同，`fills_sync` 沒有「只有錯誤沒有游標」
        這種中間狀態可插入。"""
        addr = _norm(address)
        with self._lock, self._db:
            self._db.execute(
                "UPDATE fills_sync SET last_error=?, updated_at=? WHERE address=?",
                (err, at, addr))

    def set_scan_error(self, scan_id: str, err: str) -> None:
        """遍歷軌隔離時的錯誤落地（`fills_scan` 版的 `set_sync_error`）——只
        UPDATE `last_error`，不改 `status`（隔離走 `refresh_job` 本身的
        `next_attempt_at`，scan 本身仍是 `running`，下次領到 job 會繼續從
        `cursor_ms` 續抓）。"""
        with self._lock, self._db:
            self._db.execute(
                "UPDATE fills_scan SET last_error=? WHERE scan_id=?", (err, scan_id))

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
        fills_sync／fills_scan）——但若該地址仍有 `refresh_job` 列則跳過（避免刪掉
        正在跑的工作依賴的資料）。另外刪除超過 `fills_keep_s` 的 fills，但保留仍落在
        該地址目前同步視窗內（`time_ms >= fills_sync.window_start_ms`）的列。回傳
        各表刪除筆數。"""
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
                cur = self._db.execute("DELETE FROM fills_scan WHERE address=?", (addr,))
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
            for t in ("candidate", "endpoint_cache", "fills", "fills_sync", "fills_scan",
                      "refresh_job"):
                out[t] = self._db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            out["completeness"] = dict(self._db.execute(
                "SELECT completeness, COUNT(*) FROM fills_sync GROUP BY completeness"
            ).fetchall())
        return out
