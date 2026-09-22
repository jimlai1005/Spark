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
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple

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

# Task 7.9d-D D2：`refresh_job.kind` 的已知集合——彙總查詢（`count_jobs_by_kind`／
# `count_due_by_kind`）用它把已知 kind 一律補成 0，讓觀測端的鍵集合固定（缺鍵與
# 「這個 kind 現在是 0」在儀表板上長得一樣，但後者才是真的；`scheduler.status()`
# 在 7.9d 之前就是逐 kind 查出七個固定鍵，彙總不該讓鍵集合縮水）。DB 裡出現不在
# 本清單的 kind（例如未來新增而忘了同步）仍會照實回報，不會被吃掉。
JOB_KINDS = ("candidates", "state", "portfolio", "ledger", "fills", "fills_scan",
             "fills_verify")

# Task 7.9e-D D3（7.9d 複審 S3）：準入上限＝`refresh_job` 總數 ≤
# `ADMISSION_MULTIPLIER` × active 候選數。每個地址最多 6 種 per-address kind
# （`JOB_KINDS` 扣掉全域的 `candidates`）＋ `candidates` 本身 ＋ 餘裕，故為 7。
# 放在資料層是因為它是純常數、且兩個消費端（`explore_scheduler` 的排程准入與
# `app.py` 的詳情頁補排）都需要它——`app.py` 不該為了一個常數 import 整個
# scheduler（那會把排程模組拉進 web 層的 import 圖）。
ADMISSION_MULTIPLIER = 7

# 推進某個 `fills_scan.kind` 所需的 `refresh_job.kind`（Task 7.9e-D D1）：
# `verify` 遍歷由 `fills_verify` job 推進，`initial`／`partial_rescan` 由
# `fills_scan` job 推進。孤兒判準（`count_scans`）必須照這張對照表比對，不能
# 寫死 `fills_scan`——否則「running verify ＋ 正確的 fills_verify job」會被算
# 成孤兒，對帳跟著重複建工作。
JOB_KIND_FOR_SCAN_KIND = {"verify": "fills_verify", "initial": "fills_scan",
                          "partial_rescan": "fills_scan"}

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


def _require_inc_from(value: int | None, address: str) -> int:
    """Task 7.9c D6：`fills_sync.inc_from_ms`（增量軌起點）的寫入邊界守門。

    任何會寫 `fills_sync` 的路徑（`bootstrap_address_fills`／
    `insert_fills_page`／v2→v3 遷移）都先經過這裡，`None` 一律
    `ValueError`（整筆寫入不落地）——非空性靠**寫入邊界**保證，不靠讀取端
    各自加 `is None` 退路（工程原則 5：結構性守門優於「記得檢查」）。"""
    if value is None:
        raise ValueError(
            f"explore store: fills_sync.inc_from_ms 不得為 NULL（address={address}）")
    return value


def _merge_min(a: int | None, b: int | None) -> int | None:
    """兩個可為 `None` 的觀測極值取較小者（Task 7.9c D2）；都是 `None` → `None`。"""
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _merge_max(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


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


class ScanTarget(NamedTuple):
    """`ExploreStore.scan_job_targets` 的一列（Task 7.9d-D／7.9e-D D4）：對帳
    要判斷「這個地址需要哪一種遍歷工作」所需的全部狀態，一句 SQL 取齊。

    `NamedTuple`（不是 dataclass）是刻意的——既能用欄位名讀，也仍然是 tuple，
    既有以 tuple 比較的測試與呼叫端不必改寫。

    - `running_scan_id`：進行中的遍歷（續跑同一個 scan 與游標用）；`None` ＝無。
    - `evidence_unknown`：`fills_sync.evidence_unknown`（0／1；無 `fills_sync`
      列時 0）——1 代表這個地址的完整性結論缺可追溯證據，需要一次 `verify`。
    - `has_done_verify`：該地址是否已經有一次 **完成** 的 `verify` 遍歷
      （`fills_scan.kind='verify' AND status='done'`）。與 `evidence_unknown`
      獨立：「核驗過但旗標還在」與「從未核驗」是兩種不同的處置。
    """
    address: str
    running_scan_id: str | None
    evidence_unknown: int
    has_done_verify: bool


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


class ScanWriteback(str, Enum):
    """`ExploreStore.complete_scan` 的回傳（Task 7.9c D3）——CAS 落空不再是
    一個沉默的 `False`，呼叫端（`explore_scheduler._run_scan`）必須能分辨
    「正常收尾」「重複套用」「被更新的遍歷取代」「增量軌列不見了」四種
    結局並各自反應（S5）。`str` Enum：值可直接進 log／`status()` 計數鍵。

    - `APPLIED`：CAS 命中，`fills_sync` 的 completeness／reason／scan_id 已更新。
    - `DUPLICATE`：`fills_sync.scan_id` 已經是這一筆 scan（同一次完成被套用
      兩次，例如 job 重跑）——不重寫，視為正常收尾。
    - `STALE`：`fills_sync.scan_id` 指向 `started_at` 更晚的另一筆 scan（這次
      的結論已經過期）——不覆蓋新結論。
    - `MISSING`：該地址沒有 `fills_sync` 列（退池後被 purge、或資料被人工刪除）
      ——沒有增量軌可寫回。

    四種結局都**不影響**「fills 落地」與「`fills_scan` 標記 done」：資料不丟，
    只有對增量軌的寫回被擋下。"""

    APPLIED = "applied"
    DUPLICATE = "duplicate"
    STALE = "stale"
    MISSING = "missing"


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
        with self._lock:
            with self._db:
                self._db.executescript(_SCHEMA)
                row = self._db.execute("SELECT version FROM schema_version").fetchone()
                if row is None:
                    self._db.execute(
                        "INSERT INTO schema_version (version) VALUES (?)", (_SCHEMA_VERSION,))
            if row is not None and row[0] < _SCHEMA_VERSION:
                if row[0] < 2:
                    with self._db:
                        self._migrate_v1_to_v2()
                        self._db.execute("UPDATE schema_version SET version=2")
                if row[0] < 3:
                    # 版本更新在 `_migrate_v2_to_v3` 自己的顯式 transaction 內
                    # （Task 7.9c D5：列迴圈失敗 → 版本仍停在 2、整段回滾）。
                    self._migrate_v2_to_v3()
            self._assert_inc_from_not_null()
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

    def _assert_inc_from_not_null(self) -> None:
        """Task 7.9c D6：啟動檢查——`fills_sync.inc_from_ms` 是增量軌起點，
        整個覆蓋連續性判斷（`complete_scan` 的 gap 檢查）與增量規劃
        （`explore_fills_sync.plan_incremental`）都以它為基準。空值代表
        遷移沒跑完或有人繞過寫入邊界改過資料——**啟動即失敗**，不要帶著
        破掉的不變式跑起來然後在執行時到處補 `is None` 退路（工程原則 5：
        結構性的守門，不是每個呼叫點各自記得檢查）。

        呼叫端（`__init__`）已持有 `self._lock`，本方法不再自行取鎖。"""
        n = self._db.execute(
            "SELECT COUNT(*) FROM fills_sync WHERE inc_from_ms IS NULL").fetchone()[0]
        if n:
            raise RuntimeError(
                f"explore store: {n} 列 fills_sync.inc_from_ms 為 NULL（增量軌起點缺失）"
                "——遷移未完成或資料被外部改動，拒絕啟動")

    @contextmanager
    def _explicit_transaction(self) -> Iterator[None]:
        """顯式 `BEGIN IMMEDIATE … COMMIT`／例外時 `ROLLBACK`（Task 7.9c D5）。

        為什麼不能沿用模組檔頭的 `with self._db:` 隱式 transaction：Python
        `sqlite3` 的 legacy isolation 模式只在 DML 前自動 BEGIN，**DDL 不在
        transaction 內**，而遷移需要「列迴圈＋版本更新」這一整段要嘛全成功、
        要嘛全不留痕跡（7.9b 複審 W1：遷移到一半被殺 → 重開時撞
        UNIQUE／重複建 verify job）。做法：暫時把連線切到真正的 autocommit
        （`isolation_level=None`），自己下 `BEGIN IMMEDIATE`，離開時
        COMMIT／ROLLBACK 並還原原本的 isolation_level。"""
        prev = self._db.isolation_level
        self._db.isolation_level = None
        try:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")
        finally:
            self._db.isolation_level = prev

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
        """Task 7.9b B5：遍歷軌／增量軌分離。DDL（`ALTER TABLE`／`CREATE TABLE
        IF NOT EXISTS`）各自冪等（欄位／表已存在則跳過），且**不在** transaction
        內（Python `sqlite3` legacy isolation 模式只對 DML 自動 BEGIN）。

        Task 7.9c D5（7.9b 複審 W1）：DDL 之後的「列迴圈＋`schema_version`
        更新」包在一個顯式 `BEGIN IMMEDIATE … COMMIT`（`_explicit_transaction`）
        ——中途失敗則整段回滾、版本仍停在 2，下次啟動從頭重跑。同時列迴圈
        本身**實際**冪等（7.9b 只在 docstring 宣稱冪等，實作會重複建 scan 列
        與重複改 job）：
        - 該地址已有任何 `fills_scan` 列 → 整列跳過（這一列已遷移過）。
          Task 7.9d-D D3（7.9c 複審 S3，說清這道閘門的作用範圍）：**它擋的
          不是**「上一次遷移做了一半」——外層的 `BEGIN IMMEDIATE` 保證部分
          遷移狀態根本無法持久化（中途失敗＝整段回滾，`fills_scan` 為空、
          version 仍 2），所以正常路徑上重跑時這個閘門一定不會命中。它只擋
          一種情況：**遷移已成功提交，但 `schema_version` 被人工改回 2**
          （災難演練、降版回滾、手動修 DB）而讓遷移再跑一次——那時
          `fills_scan` 列已經在了，沒有這道閘門就會重複建 scan 列、重複把
          `fills` job 改名。下面的 `INSERT OR IGNORE` 與改名的 `NOT EXISTS`
          同理，都是為這條「人工改版本」路徑準備的第二層保險。
        - 新增 job 一律 `INSERT OR IGNORE`；`refresh_job` 改名加
          `NOT EXISTS`（目標 key 已存在就不改名，避免撞 PRIMARY KEY）。
        - `fills_verify` 的攤開時間用地址雜湊（`_migration_spread_s`）決定，
          重跑同一顆 DB 得到同一個時間。

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
        with self._explicit_transaction():
            self._migrate_v2_to_v3_rows(rows, now)
            self._db.execute("UPDATE schema_version SET version=3")

    def _migrate_v2_to_v3_rows(self, rows: list[tuple], now: float) -> None:
        """`_migrate_v2_to_v3` 的列迴圈（拆出來只是為了讓 transaction 的範圍
        在呼叫端一眼可見，並讓測試能對「列迴圈中途丟例外」注入）。"""
        for (addr, win_start, win_end, cursor_ms, synced_through, obs_from, obs_to,
             completeness, reason, pages_done, fills_in_window, params_fp,
             updated_at) in rows:
            already = self._db.execute(
                "SELECT 1 FROM fills_scan WHERE address=? LIMIT 1", (addr,)).fetchone()
            if already is not None:
                continue   # 這一列已經遷移過（冪等）。
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
                inc_from = _require_inc_from(win_end, addr)
                self._db.execute(
                    "UPDATE fills_sync SET inc_from_ms=?, synced_through_ms=?, cursor_ms=?, "
                    "window_end_ms=?, scan_id=NULL, evidence_unknown=0, "
                    "coverage_gap=0 WHERE address=?",
                    (inc_from, inc_from, inc_from, inc_from, addr))
                old_job = self._db.execute(
                    "SELECT priority, next_attempt_at, attempts, created_at FROM refresh_job "
                    "WHERE key=?", (f"{addr}:fills",)).fetchone()
                if old_job is not None:
                    priority, next_attempt_at, attempts, created_at = old_job
                    # 改名守門（D5）：目標 key 已存在（前一次遷移改過、但版本
                    # 沒寫成功）→ 不改，避免撞 PRIMARY KEY 讓整段遷移失敗。
                    self._db.execute(
                        "UPDATE refresh_job SET key=?, kind='fills_scan' WHERE key=? "
                        "AND NOT EXISTS (SELECT 1 FROM refresh_job WHERE key=?)",
                        (f"{addr}:fills_scan", f"{addr}:fills", f"{addr}:fills_scan"))
                    self._db.execute(
                        "INSERT OR IGNORE INTO refresh_job (key, address, kind, priority, "
                        "created_at, next_attempt_at, attempts) "
                        "VALUES (?, ?, 'fills', ?, ?, ?, 0)",
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
                # D6：增量軌起點＝舊 `synced_through_ms`（B5 字面：「從現在的
                # 前沿起算」）。舊列理論上恆有前沿（`complete`／`partial` 都是
                # 跑完至少一輪才會標上），但 v2 schema 允許 NULL——退回
                # `window_end_ms`（該輪窗口末端，與 backfilling 分支同一個
                # 原則），絕不讓 NULL 落地。
                inc_from = _require_inc_from(
                    synced_through if synced_through is not None else win_end, addr)
                self._db.execute(
                    "UPDATE fills_sync SET inc_from_ms=?, scan_id=?, evidence_unknown=?, "
                    "coverage_gap=0 WHERE address=?",
                    (inc_from, scan_id, 0 if verified else 1, addr))
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
        _require_inc_from(checkpoint.inc_from_ms, addr)   # D6：寫入邊界守門
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
        `explore_scheduler._enqueue_address_jobs`）。

        Task 7.9c D6：`inc_from_ms` 由 `now` 直接算出（`int(now * 1000)`），
        結構上不可能為 `None`——仍走 `_require_inc_from` 這道共同閘門，讓
        「寫 `fills_sync` 的路徑都經過同一個守門」是結構性的、不是記憶性的。"""
        addr = _norm(address)
        now_ms = _require_inc_from(int(now * 1000), addr)
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

    def running_scan(self, address: str) -> FillsScan | None:
        """Task 7.9c D8：`get_active_scan` 的契約名（S 端依狀態推導排程動作時
        用的名字，見 plan §7.9c 介面契約）。同一個查詢，不另開實作。"""
        return self.get_active_scan(address)

    def count_scans(self, active: set[str] | None = None) -> dict[str, int]:
        """進行中遍歷的彙總（單句 SQL，Task 7.9d-D D2；2026-09-22 使用者第二輪
        裁決點 3 的分列版）。回傳四個鍵：

        - `running_rows`：`status='running'` 的 `fills_scan` 列數。
        - `running_addresses`：上列涵蓋的**地址數**（同址理論上只有一筆
          running，但列數與地址數要分開回報才看得出異常）。
        - `orphan_rows`：running 但該地址**沒有對應 kind 的 job** 的列數
          ——這就是「遍歷停在半路、沒有任何 job 會推進它」的孤兒數（對帳
          `reconcile_scan_jobs` 要把它們補回 job，續跑同一個 `scan_id`）。
          Task 7.9e-D D1（7.9d 複審 W1）：對應關係由
          `JOB_KIND_FOR_SCAN_KIND` 決定（`verify` ↔ `fills_verify`，
          `initial`／`partial_rescan` ↔ `fills_scan`）——舊版寫死
          `fills_scan`，把「running verify ＋ 正確的 fills_verify job」誤算
          成孤兒（複審 `rv_orphan.py` 實跑 orphan_rows=1）。
        - `inactive_running_rows`：running 但地址已不在 `active` 集合內；
          `active=None`（沒問）→ 0，不臆測。

        **母體＝`fills_scan` 列本身，不經候選 active 過濾**：7.9c 的
        `status()` 走 `active_candidates()` 再逐一 `running_scan(addr)`（300 次
        點查、正式機實測 21ms/次），母體變成「目前 active 候選」——退池但
        遍歷還沒收尾的地址就從觀測值裡消失（工程原則 1：這個計數與
        `refresh_job`／`fills_scan` 的其他計數必須同母體才可比）。`active`
        只做**分列**，不縮小母體。"""
        addrs = tuple(sorted(_norm(a) for a in active)) if active is not None else ()
        if active is not None and addrs:
            placeholders = ",".join("?" * len(addrs))
            inactive_expr = f"SUM(CASE WHEN s.address IN ({placeholders}) THEN 0 ELSE 1 END)"
        elif active is not None:
            inactive_expr = "COUNT(*)"       # active 集合為空 → 全部都是 inactive
        else:
            inactive_expr = "0"              # 沒問就不分列
        pairs = tuple(sorted(JOB_KIND_FOR_SCAN_KIND.items()))
        kind_case = ("CASE s.kind " + " ".join(["WHEN ? THEN ?"] * len(pairs))
                     + " ELSE 'fills_scan' END")
        kind_params = tuple(v for pair in pairs for v in pair)
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT COUNT(*), COUNT(DISTINCT s.address), "
                "SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM refresh_job j "
                f"  WHERE j.address = s.address AND j.kind = {kind_case}) THEN 1 ELSE 0 END), "
                f"{inactive_expr} "
                "FROM fills_scan s WHERE s.status='running'",
                kind_params + addrs).fetchone()
        return {"running_rows": row[0], "running_addresses": row[1],
                "orphan_rows": row[2] or 0, "inactive_running_rows": row[3] or 0}

    def scan_job_targets(self, active: set[str]) -> list[ScanTarget]:
        """對帳用（2026-09-22 使用者第二輪裁決點 1／3；Task 7.9e-D D4 補兩欄）：
        每個 **active** 地址一列 `ScanTarget`，依地址排序。

        單句 SQL（`VALUES` CTE ＋ 兩個 `LEFT JOIN` ＋ 一個 `EXISTS`）——
        `reconcile_scan_jobs` 原本要對 300 個地址各做一次 `running_scan(addr)`
        點查（21ms/次），第四原因碼（`verify_needed`）又會再加兩次點查。
        `running_scan_id is None` ＝該地址目前沒有進行中的遍歷（對帳端再依
        `completeness`／partial 到期／`evidence_unknown` 決定要不要建新的；有
        `scan_id` 時一律**續跑同一個** scan 與游標，不得整窗重抓）。

        母體就是傳入的 `active` 集合本身（候選表裡沒有的地址也會回一列，
        `running_scan_id=None`／`evidence_unknown=0`，不靜默吃掉）；空集合
        → `[]`（不下 SQL）。"""
        addrs = sorted({_norm(a) for a in active})
        if not addrs:
            return []
        values = ",".join(["(?)"] * len(addrs))
        with self._lock, self._db:
            rows = self._db.execute(
                f"WITH a(address) AS (VALUES {values}) "
                "SELECT a.address, s.scan_id, COALESCE(f.evidence_unknown, 0), "
                "  EXISTS (SELECT 1 FROM fills_scan v WHERE v.address = a.address "
                "          AND v.kind='verify' AND v.status='done') "
                "FROM a "
                "LEFT JOIN fills_scan s ON s.address = a.address AND s.status='running' "
                "LEFT JOIN fills_sync f ON f.address = a.address "
                "ORDER BY a.address", tuple(addrs)).fetchall()
        return [ScanTarget(address=r[0], running_scan_id=r[1], evidence_unknown=r[2],
                           has_done_verify=bool(r[3])) for r in rows]

    def verify_needed(self, active: set[str]) -> dict[str, int]:
        """核驗需求的單一數字來源（Task 7.9e-D D2，7.9d 複審 C2）——母體＝
        `evidence_unknown=1` 且地址在 `active` 內的 `fills_sync` 列：

        - `rows`：需要一次核驗遍歷的 active 地址數。
        - `with_job`：其中已有 `fills_verify` job 的數。
        - `with_running`：其中已有**任何 kind** 進行中（`status='running'`）遍歷的數
          ——任何一次完成的遍歷（initial／partial_rescan／verify）都會由
          `complete_scan` 清 `evidence_unknown`，所以進行中的重掃也算「有工作在
          服務它」；排程的 `_needs_scan_job` 也是遍歷軌先於核驗軌（7.9e 複審 W1：
          兩邊必須同源，否則 `partial` 列在 partial_rescan 期間會被誤判 unserved）。
        - `unserved`：無 `fills_verify` job、無任何 running 遍歷、也無 `fills_scan`
          job（待跑的 initial／partial_rescan 同樣會清 unknown）——**狀態需要核驗
          但沒有任何工作在服務它**。

        為什麼需要這個而不是看 `count_jobs_by_kind()["fills_verify"]`：job 列數
        歸零可能是「核驗做完了」，也可能是「job 被退池掃除／kind 不相容閘門刪掉，
        而 `evidence_unknown` 永遠留在 1」（7.9d 複審 C2 的實況：3 天模擬收尾
        `verify_remaining == 0` 但 17 個地址仍 `evidence_unknown == 1`，health
        完全看不出來）。核驗真的做完的判準是 `rows == 0`；排程健康的判準是
        `unserved == 0`。

        單句 SQL；`active` 為空集合 → 四個 0（不下 SQL）。"""
        addrs = tuple(sorted({_norm(a) for a in active}))
        if not addrs:
            return {"rows": 0, "with_job": 0, "with_running": 0, "unserved": 0}
        placeholders = ",".join("?" * len(addrs))
        has_job = ("EXISTS (SELECT 1 FROM refresh_job j WHERE j.address = f.address "
                   "AND j.kind='fills_verify')")
        has_running = ("EXISTS (SELECT 1 FROM fills_scan v WHERE v.address = f.address "
                       "AND v.status='running')")
        has_scan_job = ("EXISTS (SELECT 1 FROM refresh_job j2 WHERE j2.address = f.address "
                        "AND j2.kind='fills_scan')")
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT COUNT(*), "
                f"SUM(CASE WHEN {has_job} THEN 1 ELSE 0 END), "
                f"SUM(CASE WHEN {has_running} THEN 1 ELSE 0 END), "
                f"SUM(CASE WHEN {has_job} OR {has_running} OR {has_scan_job} THEN 0 ELSE 1 END) "
                "FROM fills_sync f WHERE f.evidence_unknown=1 "
                f"AND f.address IN ({placeholders})", addrs).fetchone()
        return {"rows": row[0], "with_job": row[1] or 0, "with_running": row[2] or 0,
                "unserved": row[3] or 0}

    def latest_done_scan(self, address: str) -> FillsScan | None:
        """Task 7.9c D8：該地址 `status='done'` 中 `finished_at` 最大的一筆
        （`NULL` 的 finished_at 排最後）——排程端用它判斷 partial 重掃是否
        到期（`explore_fills_sync.partial_rescan_due`）。沒有任何完成過的
        遍歷 → `None`。"""
        addr = _norm(address)
        with self._lock, self._db:
            row = self._db.execute(
                f"SELECT {self._SCAN_COLUMNS} FROM fills_scan "
                "WHERE address=? AND status='done' "
                "ORDER BY finished_at IS NULL, finished_at DESC LIMIT 1", (addr,)).fetchone()
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

    def complete_scan(self, address: str, fills: list[dict],
                       scan: FillsScan) -> ScanWriteback:
        """遍歷軌完成（Task 7.9b B3／7.9c D3）：落地終止頁 fills＋標記
        `fills_scan` `status='done'`＋**CAS** 寫回 `fills_sync`。

        覆蓋連續性（B1）：`scan.window_end_ms < fills_sync.inc_from_ms` →
        `coverage_gap=1`（語義見 `explore_fills_sync.external_coverage_state`
        的 D7 段：落後是 stale，不是 gap）。`evidence_unknown` 無條件清 0
        （任何一次完成的遍歷都是新鮮證據，不論 `initial`／`partial_rescan`／
        `verify`，也不論 `result` 是 `complete` 還是 `partial`——B5：「核驗
        scan 完成……不強行升級」指的是不強改 `result`，不是不清除 unknown
        旗標）。D2：本次遍歷觀測到的 `observed_from/to_ms` 以 min／max 併入
        `fills_sync`（兩軌各自看到的極值合成對外的觀測區間）。

        回傳 `ScanWriteback`（7.9b 的 `bool` 讓 CAS 落空變成靜默的正常收尾）：
        `MISSING`（無 `fills_sync` 列）／`DUPLICATE`（已經指向這筆 scan）／
        `STALE`（目前指向 `started_at` 更晚的 scan）／`APPLIED`。三種非
        `APPLIED` 都不動 `fills_sync`，但 fills 仍落地、`fills_scan` 仍標
        `done`——資料不丟。DB 例外照常往外拋（不吞）。

        Task 7.9d-D 補（2026-09-22 主線程裁決）：`scan.result is None` 一律
        `ValueError`（訊息含 `scan_id`）且**不寫任何東西**——`result` 是寫進
        `fills_sync.completeness`（NOT NULL）的值，None 代表這次遍歷其實沒有
        結論（例如非法頁：舊版的 `apply_scan_page` 會回 `done=True`／
        `result=None`），讓它進來只會在 UPDATE 到一半時撞 `IntegrityError`
        ——那時 `fills_scan` 已經被標 `done`、job 被隔離 24 小時，狀態比一開始
        更糟。沒有結論的遍歷應該續跑（見 `explore_fills_sync.apply_scan_page`
        的非法頁分支），不是完成。"""
        if scan.result is None:
            raise ValueError(
                f"complete_scan 拒絕沒有結論的遍歷（result is None）：scan_id={scan.scan_id}"
                f" address={_norm(address)} last_error={scan.last_error!r}——"
                "沒有結論代表這一輪還沒跑完（例如非法頁），應該續跑而不是收尾")
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
            sync_row = self._db.execute(
                "SELECT inc_from_ms, scan_id, observed_from_ms, observed_to_ms "
                "FROM fills_sync WHERE address=?", (addr,)).fetchone()
            if sync_row is None:
                return ScanWriteback.MISSING
            inc_from_ms, current_scan_id, sync_obs_from, sync_obs_to = sync_row
            if current_scan_id == scan.scan_id:
                return ScanWriteback.DUPLICATE
            if current_scan_id is not None:
                started_row = self._db.execute(
                    "SELECT started_at FROM fills_scan WHERE scan_id=?",
                    (current_scan_id,)).fetchone()
                if started_row is not None and started_row[0] > scan.started_at:
                    return ScanWriteback.STALE
            gap = 1 if (inc_from_ms is not None and scan.window_end_ms < inc_from_ms) else 0
            obs_from = _merge_min(sync_obs_from, scan.observed_from_ms)
            obs_to = _merge_max(sync_obs_to, scan.observed_to_ms)
            cur = self._db.execute(
                "UPDATE fills_sync SET completeness=?, reason=?, scan_id=?, coverage_gap=?, "
                "evidence_unknown=0, observed_from_ms=?, observed_to_ms=? "
                "WHERE address=? AND (scan_id IS NULL OR scan_id != ?)",
                (scan.result, scan.reason, scan.scan_id, gap, obs_from, obs_to, addr,
                 scan.scan_id))
            # 讀與寫在同一把 `self._lock`＋同一個 transaction 內，理論上必然
            # 命中；不命中代表有人繞過 store 直接改了 DB——當成 STALE 處理
            # （寧可不覆蓋），並留下 warning 讓它可被觀測，不靜默。
            if cur.rowcount != 1:
                logger.warning(
                    "explore store: complete_scan CAS 落空（address=%s scan_id=%s）", addr,
                    scan.scan_id)
                return ScanWriteback.STALE
        return ScanWriteback.APPLIED

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

    def job_kinds(self, address: str) -> set[str]:
        """Task 7.9c D8：該地址目前 `refresh_job` 的 kind 集合——排程端
        「算需要的 job 集合 → 扣掉已存在的 → 逐項准入」用它做去重（W4／S3），
        不再用「job 列不存在」當成「該建一個新 scan」的觸發條件。"""
        addr = _norm(address)
        with self._lock, self._db:
            rows = self._db.execute(
                "SELECT kind FROM refresh_job WHERE address=?", (addr,)).fetchall()
        return {r[0] for r in rows}

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

    def count_jobs_by_kind(self, active: set[str] | None = None) -> dict[str, dict[str, int]]:
        """每個 `kind` 的 `refresh_job` 統計（單句 `GROUP BY`，Task 7.9d-D D2；
        2026-09-22 使用者第二輪裁決點 3 的分列版）。每個 kind 四個鍵：

        - `rows`：該 kind 的 job 列數。
        - `addresses`：涵蓋的**地址數**（`COUNT(DISTINCT address)`；列數與地址
          數必須分開回報——同址多 job 與多址各一 job 在單一數字下長得一樣）。
          全域 kind（`address IS NULL`，目前只有 `candidates`）不計入。
        - `active_rows`／`inactive_rows`：地址在／不在傳入的 `active` 集合內的
          列數。`active=None`（沒問）→ `active_rows == rows`、`inactive_rows
          == 0`，不臆測。`address IS NULL` 的全域 job 一律算 active——它沒有
          「退池」這回事（`delete_inactive_jobs` 同樣不刪它）。

        **母體＝`refresh_job` 列本身，不經候選 active 過濾**——7.9c 的
        `status()` 用 `active_candidates()` 逐址點查 job，正式機 129 筆
        `fills_verify` 只顯示 112（17 筆屬於已退池但 job 還沒刪的地址）：對
        「核驗軌還剩多少工作」這個問題來說，那 17 筆仍會被 `claim_due` 領走、
        仍會消耗名額，不該從觀測值裡消失（工程原則 1：被比較／被追蹤的量與
        排程實際作用的集合同源）。`active` 只做分列，不縮小母體。

        `JOB_KINDS` 的已知 kind 一律出現（沒有列時四個鍵都是 0，不是缺鍵）；
        DB 裡若有清單外的 kind 也會照實回報。"""
        addrs = tuple(sorted(_norm(a) for a in active)) if active is not None else ()
        if active is not None and addrs:
            placeholders = ",".join("?" * len(addrs))
            active_expr = ("SUM(CASE WHEN address IS NULL OR address IN "
                           f"({placeholders}) THEN 1 ELSE 0 END)")
        elif active is not None:
            active_expr = "SUM(CASE WHEN address IS NULL THEN 1 ELSE 0 END)"
        else:
            active_expr = "COUNT(*)"
        counts = {k: {"rows": 0, "addresses": 0, "active_rows": 0, "inactive_rows": 0}
                  for k in JOB_KINDS}
        with self._lock, self._db:
            rows = self._db.execute(
                "SELECT kind, COUNT(*), COUNT(DISTINCT address), "
                f"{active_expr} FROM refresh_job GROUP BY kind", addrs).fetchall()
        for kind, n_rows, n_addrs, n_active in rows:
            counts[kind] = {"rows": n_rows, "addresses": n_addrs,
                            "active_rows": n_active, "inactive_rows": n_rows - n_active}
        return counts

    def delete_inactive_jobs(self, active: set[str]) -> int:
        """刪除所有 `address` 不在 `active` 集合內的 `refresh_job`，回傳刪除列數
        （2026-09-22 使用者第二輪裁決點 3）。

        `delete_jobs` 只清「本輪 `deactivate_missing` 回傳的那幾個地址」——
        重啟、漏跑一輪、或人工改動之後留下的殘留 job 沒有任何路徑會清，它們
        照樣被 `claim_due` 領走、照樣打上游、照樣佔準入上限。對帳時用本方法
        以 active 集合為準做一次全表掃除（冪等：沒有殘留就回 0）。

        `address IS NULL` 的全域 job（目前只有 `candidates`，它就是產生 active
        集合本身的那個工作）**永不刪除**——刪了就再也沒有東西會更新候選池。
        `active` 為空集合時同樣只刪有地址的 job。"""
        addrs = tuple(sorted({_norm(a) for a in active}))
        sql = "DELETE FROM refresh_job WHERE address IS NOT NULL"
        if addrs:
            sql += f" AND address NOT IN ({','.join('?' * len(addrs))})"
        with self._lock, self._db:
            cur = self._db.execute(sql, addrs)
        return cur.rowcount

    def count_due_by_kind(self, now: float) -> dict[str, int]:
        """每個 `kind` 目前**到期且無有效 lease** 的 job 數（單句 SQL，
        Task 7.9d-D D2）——`due_count(kind, now)` 的彙總版，兩者共用同一組
        條件（`next_attempt_at <= now AND (lease_until IS NULL OR
        lease_until < now)`），不得各自寫一套「到期」定義。

        母體同 `count_jobs_by_kind`（`refresh_job` 列本身，不經 active 過濾）。
        `JOB_KINDS` 的已知 kind 一律出現（0 表示「這個 kind 現在沒有到期
        工作」，與缺鍵區分——飢餓觀測要看得出 0 與「沒問過」的差別）。"""
        counts = dict.fromkeys(JOB_KINDS, 0)
        with self._lock, self._db:
            rows = self._db.execute(
                "SELECT kind, COUNT(*) FROM refresh_job "
                "WHERE next_attempt_at <= ? AND (lease_until IS NULL OR lease_until < ?) "
                "GROUP BY kind", (now, now)).fetchall()
        counts.update({r[0]: r[1] for r in rows})
        return counts

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

    def oldest_due_at(self, now: float, *,
                      kinds: tuple[str, ...] | None = None) -> float | None:
        """目前到期（`next_attempt_at <= now`）的工作中最早的到期時刻；沒有到期
        工作回 `None`（scheduler `status()` 的 `oldest_due_age_s` 用）。

        Task 7.9c D8：`kinds` 限定 kind 集合（`None` ＝不限定，維持舊行為）
        ——S 端用 `kinds=("fills_verify",)` 量測核驗軌已經等多久，以實作
        「有界等待」（等 ≥ `VERIFY_MAX_WAIT_S` 就給一次名額），不讓嚴格讓位
        變成永久飢餓。"""
        kind_filter = ""
        kind_params: tuple = ()
        if kinds:
            placeholders = ",".join("?" * len(kinds))
            kind_filter = f" AND kind IN ({placeholders})"
            kind_params = tuple(kinds)
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT MIN(next_attempt_at) FROM refresh_job WHERE next_attempt_at <= ?"
                + kind_filter, (now, *kind_params)).fetchone()
        return row[0]

    # --- maintenance ---
    def purge(self, now: float, *, candidate_keep_s: float = 7 * 86400,
              fills_keep_s: float = 35 * 86400,
              scan_keep_s: float = 30 * 86400) -> dict[str, int]:
        """刪除已停用超過 `candidate_keep_s` 的候選（及其 endpoint_cache／fills／
        fills_sync／fills_scan）——但若該地址仍有 `refresh_job` 列則跳過（避免刪掉
        正在跑的工作依賴的資料）。另外刪除超過 `fills_keep_s` 的 fills，但保留仍落在
        該地址目前同步視窗內（`time_ms >= fills_sync.window_start_ms`）的列。回傳
        各表刪除筆數。

        Task 7.9c D4（7.9b 複審 W3：`fills_scan` 無界成長）：另外刪除
        `status='done'` 且 `finished_at` 早於 `now - scan_keep_s`（預設 30 天）
        的歷史遍歷列——但**永遠保留** `fills_sync.scan_id` 目前指向的那一筆
        （對外 `fills_coverage.evidence` 要能回查它，刪了證據就斷鏈）。
        兩條清理路徑（退池地址連帶刪除＋保留期清理）都計入
        `counts["fills_scan"]`——7.9b 的退池連帶刪除連 rowcount 都沒收，
        清了多少沒人知道。"""
        cutoff_candidate = now - candidate_keep_s
        cutoff_fills_ms = int((now - fills_keep_s) * 1000)
        cutoff_scan = now - scan_keep_s
        counts = {"candidate": 0, "endpoint_cache": 0, "fills": 0, "fills_sync": 0,
                  "fills_scan": 0}
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
                counts["fills_scan"] += cur.rowcount
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
            cur = self._db.execute(
                "DELETE FROM fills_scan WHERE status='done' AND finished_at IS NOT NULL "
                "AND finished_at < ? AND NOT EXISTS ("
                "SELECT 1 FROM fills_sync s WHERE s.address = fills_scan.address "
                "AND s.scan_id = fills_scan.scan_id)",
                (cutoff_scan,))
            counts["fills_scan"] += cur.rowcount
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
