# 探索 HFT 錢包過濾器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 探索管線在候選准入時（H1，零 IO）與 fills 遍歷逐頁時（H2，索引計數）判定 HFT 錢包，命中即除名、清資料、30 天冷卻；退池保留期改為可設定。

**Architecture:** 兩道閘都落在既有的 `ExploreScheduler` 單 thread 迴圈：H1 在 `_run_candidates` 把 leaderboard 月成交量 ≥ 門檻的位址併進 `excluded`；H2 在 `_run_scan` 每寫入一頁後用 `COUNT(*)`（走 `(address, time_ms)` 索引）判定，命中呼叫 `ExploreStore.flag_hft()`（`candidate.active=0`＋四個 `hft_*` 欄位＋刪除該位址的 fills／scan／sync／cache／job）。冷卻由 `candidate.hft_until` 承載，`_run_candidates` 每輪把冷卻中的位址一併排除；`purge()` 不刪冷卻中的 candidate 列。發布器只讀 `active=1`，不需改動。

**Tech Stack:** Python 3.11、SQLite（`ExploreStore`）、pytest（`uv run pytest`，離線）。

**Spec:** `docs/superpowers/specs/2026-10-05-explore-hft-filter.md`（§2 定義、§6 裁決、§7.3 逐頁評估）。

**與 spec 的一處簡化（主線程裁決）：** H2 只用原始計數，不做「觀測跨度折算」。逐頁評估下，任何 30 天真的 ≥ 6,000 筆的錢包都會在遍歷途中跨過 6,000；折算會讓單日爆量的正常錢包被誤判。spec §2 H2 的折算句由 Task 6 改掉。

---

## 檔案結構

| 檔案 | 責任 | 動作 |
|---|---|---|
| `src/spark/publicapi/explore_store.py` | schema v6（`candidate.hft_*` 四欄）、`count_fills_since`、`hft_cooldown_addresses`、`flag_hft`、`get_candidate`；`purge` 保留冷卻列；`upsert_candidates` 回池清旗標 | 修改 |
| `src/spark/publicapi/hl_explore.py` | `ExploreConfig` 三個 HFT 參數＋env；純函式 `hft_by_volume(payload, max_vlm)` | 修改 |
| `src/spark/publicapi/explore_scheduler.py` | H1 併入 `excluded`；H2 逐頁／完成時判定；`status()["hft"]`；`candidate_keep_s` 參數傳給 `purge` | 修改 |
| `src/spark/publicapi/config.py` | `ApiConfig.explore_candidate_keep_s`（env `FILET_EXPLORE_CANDIDATE_KEEP_S`） | 修改 |
| `scripts/run_api.py` | 把 `cfg.explore_candidate_keep_s` 傳進 `ExploreScheduler` | 修改 |
| `tests/test_explore_store.py` | store 新方法與遷移 | 修改 |
| `tests/test_explore_hft.py` | `hft_by_volume`、`ExploreConfig.from_env` 新欄位 | 新建 |
| `tests/test_explore_scheduler.py` | H1／H2／冷卻／`candidate_keep_s` 整合 | 修改 |
| `tests/test_publicapi_config.py` | `FILET_EXPLORE_CANDIDATE_KEEP_S` | 修改 |
| `deploy/RUNBOOK.md` | §5.8l 部署與觀測 | 修改 |
| `docs/superpowers/specs/2026-10-05-explore-hft-filter.md` | §2 H2 折算句改掉 | 修改 |

每個 task 的驗收指令都是 `uv run pytest <檔> -q`；全部完成後 `uv run pytest -q` 與 `uv run ruff check src tests scripts` 必須全綠。

---

### Task 1 `@inline`：ExploreStore——schema v6 與 HFT 旗標方法

**Files:**
- Modify: `src/spark/publicapi/explore_store.py:199-207`（`_SCHEMA_VERSION`、candidate DDL）、`:349-356`（`Candidate`）、`:514-528`（遷移分派）、`:586-611` 之後（新增 `_migrate_v5_to_v6`）、`:1069-1081`（`upsert_candidates`）、`:1104-1121`（candidate 讀取方法附近）、`:2116-2193`（`purge`）
- Test: `tests/test_explore_store.py`

- [ ] **Step 1: 寫失敗測試（追加到 `tests/test_explore_store.py` 末尾）**

```python
# ---------------------------------------------------------------------------
# HFT 過濾器（plan 2026-10-05-explore-hft-filter Task 1）
# ---------------------------------------------------------------------------
def test_schema_v6_adds_hft_columns_and_migrates_v5_db(tmp_path):
    import sqlite3
    db = tmp_path / "explore.db"
    store, _ = _store(tmp_path)
    assert store.schema_version() == 6
    cols = {r[1] for r in store._db.execute("PRAGMA table_info(candidate)").fetchall()}
    assert {"hft_reason", "hft_value", "hft_flagged_at", "hft_until"} <= cols
    # 模擬 v5 舊 DB：拿掉版本號與欄位後重開必須補上
    store._db.close()
    raw = sqlite3.connect(db)
    raw.execute("ALTER TABLE candidate DROP COLUMN hft_reason")
    raw.execute("ALTER TABLE candidate DROP COLUMN hft_value")
    raw.execute("ALTER TABLE candidate DROP COLUMN hft_flagged_at")
    raw.execute("ALTER TABLE candidate DROP COLUMN hft_until")
    raw.execute("UPDATE schema_version SET version=5")
    raw.commit(); raw.close()
    reopened = ExploreStore(db, now_fn=Clock().now)
    assert reopened.schema_version() == 6
    cols = {r[1] for r in reopened._db.execute("PRAGMA table_info(candidate)").fetchall()}
    assert {"hft_reason", "hft_value", "hft_flagged_at", "hft_until"} <= cols


def test_count_fills_since_uses_time_bound(tmp_path):
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=c.now())
    store.bootstrap_address_fills("0xabc", c.now(), window_start_ms=0,
                                  window_end_ms=10_000, params_fp="")
    scan = store.running_scan("0xabc")   # bootstrap 回傳 bool，scan 物件要另外取
    fills = [{"coin": "BTC", "tid": i, "time": i * 1000} for i in range(10)]
    store.insert_scan_page("0xabc", fills, scan)
    assert store.count_fills_since("0xabc", 0) == 10
    assert store.count_fills_since("0xabc", 5000) == 5
    assert store.count_fills_since("0xabc", 99_999) == 0
    assert store.count_fills_since("0xnobody", 0) == 0


def test_flag_hft_deactivates_and_deletes_everything_for_address(tmp_path):
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xabc", "A", 1, 0.5), ("0xdef", "D", 2, 0.4)], as_of=c.now())
    store.bootstrap_address_fills("0xabc", c.now(), window_start_ms=0,
                                  window_end_ms=10_000, params_fp="")
    scan = store.running_scan("0xabc")
    store.insert_scan_page("0xabc", [{"coin": "BTC", "tid": 1, "time": 1}], scan)
    store.put_cache_ok("0xabc", "portfolio", {"x": 1}, fetched_at=c.now(), refresh_after=c.now())
    store.enqueue("0xabc:state", "0xabc", "state", 0, c.now())
    store.enqueue("0xdef:state", "0xdef", "state", 0, c.now())

    counts = store.flag_hft("0xabc", reason="fills", value=6000.0, now=c.now(),
                            cooldown_s=30 * 86400)

    assert counts == {"endpoint_cache": 1, "fills": 1, "fills_scan": 1, "fills_sync": 1,
                      "refresh_job": 1}
    cand = store.get_candidate("0xabc")
    assert cand is not None and cand.active is False
    assert cand.hft_reason == "fills" and cand.hft_value == 6000.0
    assert cand.hft_flagged_at == c.now() and cand.hft_until == c.now() + 30 * 86400
    assert store.get_fills("0xabc", 0, 10_000) == []
    assert store.get_sync("0xabc") is None
    assert store.get_cache("0xabc", "portfolio") is None
    assert [a.address for a in store.active_candidates()] == ["0xdef"]
    # 鄰居不受影響
    assert store.stats()["refresh_job"] == 1


def test_hft_cooldown_addresses_only_returns_unexpired(tmp_path):
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xabc", None, 1, None), ("0xdef", None, 2, None)], as_of=c.now())
    store.flag_hft("0xabc", reason="fills", value=1.0, now=c.now(), cooldown_s=100)
    store.flag_hft("0xdef", reason="fills", value=1.0, now=c.now(), cooldown_s=10)
    assert store.hft_cooldown_addresses(c.now()) == {"0xabc", "0xdef"}
    assert store.hft_cooldown_addresses(c.now() + 50) == {"0xabc"}
    assert store.hft_cooldown_addresses(c.now() + 1000) == set()


def test_upsert_candidates_clears_hft_flags_on_re_entry(tmp_path):
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=c.now())
    store.flag_hft("0xabc", reason="fills", value=1.0, now=c.now(), cooldown_s=10)
    c.t += 100
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=c.now())
    cand = store.get_candidate("0xabc")
    assert cand.active is True
    assert (cand.hft_reason, cand.hft_value, cand.hft_flagged_at, cand.hft_until) == (None,) * 4


def test_purge_keeps_candidate_row_while_hft_cooldown_active(tmp_path):
    store, c = _store(tmp_path)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=c.now())
    store.flag_hft("0xabc", reason="fills", value=1.0, now=c.now(), cooldown_s=30 * 86400)
    c.t += 10 * 86400  # 超過 candidate_keep_s=7d，但冷卻還有 20 天
    counts = store.purge(c.now(), candidate_keep_s=7 * 86400)
    assert counts["candidate"] == 0
    assert store.get_candidate("0xabc") is not None
    c.t += 25 * 86400  # 冷卻到期
    counts = store.purge(c.now(), candidate_keep_s=7 * 86400)
    assert counts["candidate"] == 1
    assert store.get_candidate("0xabc") is None
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_explore_store.py -q -k "hft or count_fills_since or schema_v6"`
Expected: FAIL（`AttributeError: 'ExploreStore' object has no attribute 'count_fills_since'`／`schema_version() == 5`）

- [ ] **Step 3: 實作**

(a) `_SCHEMA_VERSION = 6`；candidate DDL 加四欄（新建 DB 直接帶欄位）：

```sql
CREATE TABLE IF NOT EXISTS candidate (
  address TEXT PRIMARY KEY,            -- 小寫正規化
  display_name TEXT, source_rank INTEGER, source_roi REAL,
  source_as_of REAL NOT NULL,          -- stats-data payload 取得時刻
  active INTEGER NOT NULL DEFAULT 1, last_seen_at REAL NOT NULL,
  -- HFT 過濾器（2026-10-05 plan）：命中 H2 時寫入；冷卻由 hft_until 承載
  hft_reason TEXT, hft_value REAL, hft_flagged_at REAL, hft_until REAL);
```

(b) `Candidate` dataclass 末尾加四個有預設值的欄位（其他建構點不必改）：

```python
    hft_reason: str | None = None
    hft_value: float | None = None
    hft_flagged_at: float | None = None
    hft_until: float | None = None
```

(c) 遷移：`__init__` 的分派在 `if row[0] < 5:` 區塊後加

```python
                if row[0] < 6:
                    with self._db:
                        self._migrate_v5_to_v6()
```

並在 `_migrate_v1_to_v2` 後新增（與 v1→v2 同款：`PRAGMA table_info` 判斷、冪等、DDL 自動 commit）：

```python
    def _migrate_v5_to_v6(self) -> None:
        """HFT 過濾器（plan 2026-10-05-explore-hft-filter Task 1）：`candidate` 補
        `hft_reason`／`hft_value`／`hft_flagged_at`／`hft_until` 四欄。與
        `_migrate_v1_to_v2` 同款：先查 `PRAGMA table_info`，缺才 `ALTER`，重跑冪等；
        DDL 自動 COMMIT，版本號最後更新。"""
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(candidate)").fetchall()}
        for col, typ in (("hft_reason", "TEXT"), ("hft_value", "REAL"),
                         ("hft_flagged_at", "REAL"), ("hft_until", "REAL")):
            if col not in cols:
                self._db.execute(f"ALTER TABLE candidate ADD COLUMN {col} {typ}")
        self._db.execute("UPDATE schema_version SET version=6")
```

(d) `upsert_candidates` 的 `ON CONFLICT ... DO UPDATE SET` 尾端加 `, hft_reason=NULL, hft_value=NULL, hft_flagged_at=NULL, hft_until=NULL`（回池即清旗標；冷卻中的位址由 scheduler 在呼叫前排除，不會進到這裡）。

(e) 在 `is_active` 後新增四個方法：

```python
    def get_candidate(self, address: str) -> Candidate | None:
        """單一候選列（含 HFT 旗標欄位）；不存在 → `None`。觀測／測試用。"""
        addr = _norm(address)
        with self._lock, self._db:
            r = self._db.execute(
                "SELECT address, display_name, source_rank, source_roi, source_as_of, "
                "active, last_seen_at, hft_reason, hft_value, hft_flagged_at, hft_until "
                "FROM candidate WHERE address=?", (addr,)).fetchone()
        if r is None:
            return None
        return Candidate(address=r[0], display_name=r[1], source_rank=r[2], source_roi=r[3],
                         source_as_of=r[4], active=bool(r[5]), last_seen_at=r[6],
                         hft_reason=r[7], hft_value=r[8], hft_flagged_at=r[9], hft_until=r[10])

    def count_fills_since(self, address: str, since_ms: int) -> int:
        """`time_ms >= since_ms` 的 fills 筆數——走 `(address, time_ms)` 索引，不讀
        `raw`（HFT 過濾器 H2 的唯一資料來源；spec §2）。"""
        addr = _norm(address)
        with self._lock, self._db:
            return self._db.execute(
                "SELECT COUNT(*) FROM fills WHERE address=? AND time_ms>=?",
                (addr, since_ms)).fetchone()[0]

    def hft_cooldown_addresses(self, now: float) -> set[str]:
        """`hft_until > now` 的位址（小寫）——`_run_candidates` 每輪併進 excluded。"""
        with self._lock, self._db:
            return {r[0] for r in self._db.execute(
                "SELECT address FROM candidate WHERE hft_until IS NOT NULL AND hft_until > ?",
                (now,)).fetchall()}

    def flag_hft(self, address: str, *, reason: str, value: float, now: float,
                 cooldown_s: float) -> dict[str, int]:
        """HFT 判定落地（spec §3）：`active=0`＋四個 `hft_*` 欄位，並刪除該位址在
        `endpoint_cache`／`fills`／`fills_scan`／`fills_sync`／`refresh_job` 的全部列
        （回收磁碟與 page cache、停止一切上游支出）。`candidate` 列保留以承載冷卻；
        `purge()` 在 `hft_until` 到期前不會刪它。回傳各表刪除筆數。"""
        addr = _norm(address)
        counts: dict[str, int] = {}
        with self._lock, self._db:
            self._db.execute(
                "UPDATE candidate SET active=0, hft_reason=?, hft_value=?, hft_flagged_at=?, "
                "hft_until=? WHERE address=?",
                (reason, value, now, now + cooldown_s, addr))
            for table in ("endpoint_cache", "fills", "fills_scan", "fills_sync", "refresh_job"):
                cur = self._db.execute(f"DELETE FROM {table} WHERE address=?", (addr,))
                counts[table] = cur.rowcount
        return counts
```

(f) `purge()` 的 stale 查詢加冷卻保護（兩個分支的參數都要跟著改）：

```python
            stale = [r[0] for r in self._db.execute(
                "SELECT c.address FROM candidate c WHERE c.active=0 AND c.last_seen_at < ? "
                "AND (c.hft_until IS NULL OR c.hft_until <= ?) "
                "AND NOT EXISTS (SELECT 1 FROM refresh_job j WHERE j.address = c.address) "
                "ORDER BY c.last_seen_at ASC"
                + ("" if max_candidates is None else " LIMIT ?"),
                (cutoff_candidate, now) if max_candidates is None
                else (cutoff_candidate, now, max_candidates)).fetchall()]
```

docstring 補一句：「`hft_until` 未到期的候選列不刪（冷卻由該列承載，見 `flag_hft`）」。

- [ ] **Step 3b: 既有測試的版本號斷言升到 6（主線程裁決 2026-10-05，builder 回報）**

`tests/test_explore_store.py` 有 5 處把「目前 schema 版本」寫死成 5，版本升級後必然失敗；
這些斷言的語意是「最新版本號」，不是「v5 特定行為」，只把 `5` 改成 `6`，不動其他斷言：
`:72`（`row == (5,)`）、`:550`、`:600`（`version == 5`）、`:1778`（`shape… == 5`）、
`:1962`（`schema_version() == 5`）。測試名 `test_schema_version_v5_recorded_on_fresh_db`
改為 `test_schema_version_latest_recorded_on_fresh_db`。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_explore_store.py -q`
Expected: 全部 PASS（含既有 purge 測試）

- [ ] **Step 5: Commit**

```bash
git add src/spark/publicapi/explore_store.py tests/test_explore_store.py
git commit -m "feat: explore store schema v6——candidate HFT 旗標、count_fills_since、flag_hft、purge 保留冷卻列"
```

---

### Task 2 `@inline`：ExploreConfig 參數與 `hft_by_volume()`

**Files:**
- Modify: `src/spark/publicapi/hl_explore.py:204-214`（DEFAULT 常數）、`:338-380`（`ExploreConfig`）、`:1254-1282` 附近（新增純函式）
- Test: `tests/test_explore_hft.py`（新建）

- [ ] **Step 1: 寫失敗測試**

```python
"""HFT 過濾器（plan docs/superpowers/plans/2026-10-05-explore-hft-filter.md Task 2）：
H1 純函式與 ExploreConfig 參數。"""
from decimal import Decimal

from spark.publicapi.hl_explore import (DEFAULT_HFT_COOLDOWN_S, DEFAULT_HFT_MAX_FILLS_30D,
                                        DEFAULT_HFT_MAX_VLM_MONTH_USD, ExploreConfig,
                                        hft_by_volume)


def _row(addr: str, vlm):
    perf = {"roi": "0.5"}
    if vlm is not None:
        perf["vlm"] = vlm
    return {"ethAddress": addr, "windowPerformances": [["month", perf]]}


def test_defaults_match_spec():
    assert DEFAULT_HFT_MAX_VLM_MONTH_USD == Decimal("100000000")
    assert DEFAULT_HFT_MAX_FILLS_30D == 6000
    assert DEFAULT_HFT_COOLDOWN_S == 30 * 86400
    cfg = ExploreConfig()
    assert cfg.hft_max_vlm_month_usd == DEFAULT_HFT_MAX_VLM_MONTH_USD
    assert cfg.hft_max_fills_30d == DEFAULT_HFT_MAX_FILLS_30D
    assert cfg.hft_cooldown_s == DEFAULT_HFT_COOLDOWN_S


def test_from_env_reads_three_hft_vars():
    cfg = ExploreConfig.from_env({"EXPLORE_HFT_MAX_VLM_MONTH_USD": "50000000",
                                  "EXPLORE_HFT_MAX_FILLS_30D": "3000",
                                  "EXPLORE_HFT_COOLDOWN_S": "86400"})
    assert cfg.hft_max_vlm_month_usd == Decimal("50000000")
    assert cfg.hft_max_fills_30d == 3000
    assert cfg.hft_cooldown_s == 86400


def test_hft_by_volume_flags_at_or_above_threshold_lowercased():
    payload = {"leaderboardRows": [
        _row("0xAAA", "100000000"),      # 等於門檻 → 命中
        _row("0xBBB", "99999999.99"),    # 低於 → 不命中
        _row("0xCCC", "1.5e9"),          # 科學記號字串 → 命中
        _row("0xDDD", None),             # 缺 vlm → 不命中
        _row("0xEEE", "NaN"),            # NaN → 不命中
        {"ethAddress": "", "windowPerformances": []},  # 壞列 → 跳過
        "garbage",
    ]}
    assert hft_by_volume(payload, Decimal("100000000")) == {"0xaaa", "0xccc"}


def test_hft_by_volume_disabled_when_threshold_non_positive():
    payload = {"leaderboardRows": [_row("0xAAA", "1e12")]}
    assert hft_by_volume(payload, Decimal("0")) == set()
    assert hft_by_volume(None, Decimal("1")) == set()
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_explore_hft.py -q`
Expected: FAIL（`ImportError: cannot import name 'hft_by_volume'`）

- [ ] **Step 3: 實作**

(a) `DEFAULT_FILLS_MAX_PAGES` 之後加：

```python
# HFT 過濾器（docs/superpowers/specs/2026-10-05-explore-hft-filter.md §2／§6，
# 使用者 2026-10-05 裁決）：H1 月成交量門檻、H2 30 天筆數門檻、冷卻秒數。
# 0（或負）＝該閘停用。
DEFAULT_HFT_MAX_VLM_MONTH_USD = Decimal("100000000")
DEFAULT_HFT_MAX_FILLS_30D = 6000
DEFAULT_HFT_COOLDOWN_S = 30 * 86400
```

(b) `ExploreConfig` 加三個欄位（放在 `fills_max_pages` 之後）：

```python
    hft_max_vlm_month_usd: Decimal = DEFAULT_HFT_MAX_VLM_MONTH_USD
    hft_max_fills_30d: int = DEFAULT_HFT_MAX_FILLS_30D
    hft_cooldown_s: int = DEFAULT_HFT_COOLDOWN_S
```

`from_env` 的 `return cls(...)` 加：

```python
            hft_max_vlm_month_usd=_dec("EXPLORE_HFT_MAX_VLM_MONTH_USD",
                                       DEFAULT_HFT_MAX_VLM_MONTH_USD),
            hft_max_fills_30d=_int("EXPLORE_HFT_MAX_FILLS_30D", DEFAULT_HFT_MAX_FILLS_30D),
            hft_cooldown_s=_int("EXPLORE_HFT_COOLDOWN_S", DEFAULT_HFT_COOLDOWN_S),
```

(c) `candidate_addresses` 之後新增純函式：

```python
def hft_by_volume(payload: dict | None, max_vlm_month_usd: Decimal) -> set[str]:
    """HFT 過濾器 H1（spec §2）：leaderboard 月窗 `vlm` ≥ 門檻的位址（小寫）。
    零 IO——與 `candidate_addresses` 讀同一份 payload，呼叫端把回傳集合併進
    `excluded`，這些位址從頭就不入池、不同步。門檻 ≤ 0 ＝ 停用（回空集合）。
    缺窗／缺 `vlm`／解析失敗／NaN 一律不命中（寧可漏放進 H2 再擋，不可誤殺）。"""
    if max_vlm_month_usd <= 0:
        return set()
    out: set[str] = set()
    for row in (payload or {}).get("leaderboardRows") or []:
        if not isinstance(row, dict) or not row.get("ethAddress"):
            continue
        perf = hl_leaderboard._window_perf(row, "month")
        try:
            vlm = Decimal(str(perf.get("vlm", "")))
        except (InvalidOperation, ValueError):
            continue
        if vlm.is_nan():
            continue
        if vlm >= max_vlm_month_usd:
            out.add(row["ethAddress"].lower())
    return out
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_explore_hft.py tests/test_public_explore.py -q`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add src/spark/publicapi/hl_explore.py tests/test_explore_hft.py
git commit -m "feat: ExploreConfig HFT 三參數＋hft_by_volume（H1 零 IO 閘）"
```

---

### Task 3 `@inline`：ExploreScheduler——H1 併入 excluded、H2 逐頁判定、status、candidate_keep_s

**Files:**
- Modify: `src/spark/publicapi/explore_scheduler.py:291-336`（`__init__`）、`:571-649`（`status`）、`:1047-1138`（`_run_candidates`）、`:1401-1496`（`_run_scan`）、import 區
- Test: `tests/test_explore_scheduler.py`

- [ ] **Step 1: 寫失敗測試（追加到 `tests/test_explore_scheduler.py` 末尾）**

```python
# ---------------------------------------------------------------------------
# HFT 過濾器（plan 2026-10-05-explore-hft-filter Task 3）
# ---------------------------------------------------------------------------
_HFT_A = "0xAAA0000000000000000000000000000000AAA1"
_HFT_B = "0xBBB0000000000000000000000000000000BBB2"


def _hft_payload(rows: list[tuple[str, str | None]]) -> dict:
    """`(address, vlm)`；roi 依序遞減。"""
    out = []
    for i, (addr, vlm) in enumerate(rows):
        perf = {"roi": str(1.0 - i * 0.001)}
        if vlm is not None:
            perf["vlm"] = vlm
        out.append({"ethAddress": addr, "displayName": f"t{i}",
                    "windowPerformances": [["month", perf]]})
    return {"leaderboardRows": out}


def _run_candidates_once(sched, store, clock) -> str:
    sched._bootstrapped = True
    store.enqueue("candidates:candidates", None, "candidates", 0, clock.now())
    r = sched.tick()
    assert r == "ran:candidates"
    return r


def test_h1_volume_gate_excludes_address_before_admission(tmp_path):
    clock = Clock(t=1_700_000_000.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    payload = _hft_payload([(_HFT_A, "150000000"), (_HFT_B, "1000")])
    sched = _sched(store, FakeHL(), leaderboard_source_fn=lambda: payload, clock=clock,
                  cfg=ExploreConfig(candidate_pool=5))
    _run_candidates_once(sched, store, clock)
    assert [c.address for c in store.active_candidates()] == [_HFT_B.lower()]
    assert store.get_candidate(_HFT_A) is None          # 從頭沒入池
    assert store._db.execute("SELECT COUNT(*) FROM refresh_job WHERE address=?",
                             (_HFT_A.lower(),)).fetchone() == (0,)   # 也沒排任何 job
    st = sched.status()["hft"]
    assert st["vlm_excluded_last_round"] == 1 and st["cooldown_last_round"] == 0


def test_h1_gate_disabled_when_threshold_zero(tmp_path):
    clock = Clock(t=1_700_000_000.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    payload = _hft_payload([(_HFT_A, "150000000")])
    sched = _sched(store, FakeHL(), leaderboard_source_fn=lambda: payload, clock=clock,
                  cfg=ExploreConfig(candidate_pool=5, hft_max_vlm_month_usd=Decimal("0")))
    _run_candidates_once(sched, store, clock)
    assert [c.address for c in store.active_candidates()] == [_HFT_A.lower()]


class _FullPagesHL(FakeHL):
    """每頁滿頁 2000 筆、時間遞增落在 plan 區間內——永遠不收尾，供 H2 逐頁判定。"""

    def get_fills_page(self, address, start_ms, end_ms):
        self.calls.append(("fills", address, start_ms, end_ms))
        return [{"coin": "BTC", "tid": start_ms + i, "time": start_ms + i}
                for i in range(PAGE_LIMIT)]


def _scan_only_scheduler(tmp_path, hl, cfg):
    clock = Clock(t=1_700_000_000.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    store.upsert_candidates([(_HFT_A, None, 1, None)], as_of=clock.now())
    now_ms = int(clock.now() * 1000)
    store.bootstrap_address_fills(_HFT_A, clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                  window_end_ms=now_ms, params_fp="")
    store.enqueue(f"{_HFT_A.lower()}:fills_scan", _HFT_A.lower(), "fills_scan", 2, clock.now())
    sched = _sched(store, hl, clock=clock, cfg=cfg,
                  state_every_s=10**9, portfolio_every_s=10**9, ledger_every_s=10**9,
                  fills_min_period_s=10**9, fills_max_period_s=10**9, rng=lambda: 0.0)
    sched._bootstrapped = True
    return sched, store, clock


def test_h2_flags_mid_scan_at_threshold_and_stops_scanning(tmp_path):
    hl = _FullPagesHL()
    sched, store, clock = _scan_only_scheduler(
        tmp_path, hl, ExploreConfig(candidate_pool=5, hft_max_fills_30d=6000, hft_cooldown_s=30 * 86400))
    results = []
    for _ in range(20):
        r = sched.tick()
        results.append(r)
        if r == "dropped:hft":
            break
    assert "dropped:hft" in results
    fills_calls = [c for c in hl.calls if c[0] == "fills"]
    assert len(fills_calls) == 3                        # 第 3 頁達 6000 即停，不抓第 4 頁
    cand = store.get_candidate(_HFT_A)
    assert cand.active is False and cand.hft_reason == "fills" and cand.hft_value == 6000.0
    assert cand.hft_until == clock.now() + 30 * 86400
    assert store.count_fills_since(_HFT_A, 0) == 0      # 成交列已清
    assert store.get_sync(_HFT_A) is None
    assert store.stats()["refresh_job"] == 0            # job 已清，不再續排
    assert sched.status()["hft"]["flagged_total"] == 1
    assert sched.status()["hft"]["last_flagged"] == {"address": _HFT_A.lower(), "fills_30d": 6000}
    # 之後 tick 不再對它打上游
    for _ in range(5):
        sched.tick()
    assert len([c for c in hl.calls if c[0] == "fills"]) == 3


def test_h2_disabled_when_threshold_zero(tmp_path):
    hl = _FullPagesHL()
    sched, store, clock = _scan_only_scheduler(
        tmp_path, hl, ExploreConfig(candidate_pool=5, hft_max_fills_30d=0))
    for _ in range(6):
        sched.tick()
    assert store.get_candidate(_HFT_A).active is True
    assert store.count_fills_since(_HFT_A, 0) >= 6000


def test_h2_cooldown_blocks_readmission_until_expiry_then_clears_flags(tmp_path):
    clock = Clock(t=1_700_000_000.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    store.upsert_candidates([(_HFT_A, None, 1, None)], as_of=clock.now())
    store.flag_hft(_HFT_A, reason="fills", value=9000.0, now=clock.now(), cooldown_s=30 * 86400)
    payload = _hft_payload([(_HFT_A, "1000"), (_HFT_B, "1000")])
    sched = _sched(store, FakeHL(), leaderboard_source_fn=lambda: payload, clock=clock,
                  cfg=ExploreConfig(candidate_pool=5))

    _run_candidates_once(sched, store, clock)
    assert [c.address for c in store.active_candidates()] == [_HFT_B.lower()]
    assert sched.status()["hft"]["cooldown_last_round"] == 1
    assert store.get_candidate(_HFT_A).hft_until is not None   # 冷卻中、旗標未被清

    clock.t += 31 * 86400
    _run_candidates_once(sched, store, clock)
    assert {c.address for c in store.active_candidates()} == {_HFT_A.lower(), _HFT_B.lower()}
    assert store.get_candidate(_HFT_A).hft_until is None
    assert sched.status()["hft"]["cooldown_last_round"] == 0


def test_candidate_keep_s_is_passed_to_purge(tmp_path, monkeypatch):
    clock = Clock(t=1_700_000_000.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    seen = {}
    real_purge = store.purge

    def spy(now, **kw):
        seen.update(kw)
        return real_purge(now, **kw)

    monkeypatch.setattr(store, "purge", spy)
    payload = _hft_payload([(_HFT_B, "1000")])
    sched = _sched(store, FakeHL(), leaderboard_source_fn=lambda: payload, clock=clock,
                  cfg=ExploreConfig(candidate_pool=5), candidate_keep_s=86400.0)
    _run_candidates_once(sched, store, clock)
    assert seen["candidate_keep_s"] == 86400.0
```

檔頭 import 加 `from decimal import Decimal`（該檔目前沒有）。

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_explore_scheduler.py -q -k "h1_ or h2_ or candidate_keep_s"`
Expected: FAIL（`KeyError: 'hft'`／`TypeError: unexpected keyword 'candidate_keep_s'`）

- [ ] **Step 3: 實作**

(a) import：`from spark.publicapi.hl_explore import ..., FILLS_WINDOW_DAYS, hft_by_volume`（沿用檔內既有的 `hl_explore` import 行）。

(b) `__init__` 簽名在 `purge_enabled: bool = True` 後加 `candidate_keep_s: float = 7 * 86400`，並初始化：

```python
        self._candidate_keep_s = candidate_keep_s
        # HFT 過濾器（plan 2026-10-05）觀測值，經 status() 進 /api/ops/health
        self._hft_flagged_total = 0
        self._hft_last_flagged: dict[str, int | str] | None = None   # 檔內無 `Any` import，不要引入
        self._hft_vlm_excluded_last = 0
        self._hft_cooldown_last = 0
```

(c) `_run_candidates`：把 `excluded = self._excluded_fn()` 改為

```python
        # HFT 過濾器 H1（零 IO）＋冷卻中的位址：併進 excluded，從頭不入池、不同步。
        vlm_excluded = hft_by_volume(payload, self._cfg.hft_max_vlm_month_usd)
        cooldown = self._store.hft_cooldown_addresses(now)
        self._hft_vlm_excluded_last = len(vlm_excluded)
        self._hft_cooldown_last = len(cooldown)
        excluded = set(self._excluded_fn()) | vlm_excluded | cooldown
```

`purge` 呼叫改為 `self._store.purge(now, candidate_keep_s=self._candidate_keep_s, max_candidates=PURGE_MAX_CANDIDATES, max_fills=PURGE_MAX_FILLS)`。

(d) 新增兩個方法（放在 `_run_scan` 前）：

```python
    def _hft_fills_exceeded(self, address: str, now: float) -> int | None:
        """HFT 過濾器 H2（spec §2／§7.3）：30 天 fills 索引計數 ≥ 門檻 → 回傳筆數，
        否則 `None`。門檻 ≤ 0 ＝ 停用。只在 scan 寫頁之後呼叫，不在發布器迴圈。"""
        thr = self._cfg.hft_max_fills_30d
        if thr <= 0:
            return None
        since_ms = int(now * 1000) - FILLS_WINDOW_DAYS * 86_400_000
        n = self._store.count_fills_since(address, since_ms)
        return n if n >= thr else None

    def _flag_hft_fills(self, address: str, n: int, now: float) -> None:
        """H2 命中落地：`flag_hft`（除名＋清資料＋冷卻）、計數、告警、標 dirty。
        呼叫端已先把手上的 job `_complete`。"""
        counts = self._store.flag_hft(address, reason="fills", value=float(n), now=now,
                                      cooldown_s=self._cfg.hft_cooldown_s)
        self._hft_flagged_total += 1
        self._hft_last_flagged = {"address": address.lower(), "fills_30d": n}
        logger.warning(
            "explore scheduler: %s 判定 HFT（30 天 %d 筆 ≥ %d）——除名、冷卻 %d 秒、清除 %s",
            address, n, self._cfg.hft_max_fills_30d, self._cfg.hft_cooldown_s, counts)
        self._notify_dirty()
```

(e) `_run_scan` 三個呼叫點：

續頁分支（`if not res.done:`）在 `self._store.insert_scan_page(...)` 之後、`invalid_page` 判斷之前：

```python
            n = self._hft_fills_exceeded(job.address, now)
            if n is not None:
                self._complete(job)
                self._flag_hft_fills(job.address, n, now)
                return "dropped:hft"
```

預算暫停分支（`cursor_ms < window_end_ms` 那段）在 `insert_scan_page` 之後同樣四行。

完成分支：在 `self._complete(job)`（約 1483 行）之後、`active = self._store.is_active(job.address)` 之前：

```python
        n = self._hft_fills_exceeded(job.address, now)
        if n is not None:
            self._flag_hft_fills(job.address, n, now)
            return "dropped:hft"
```

(f) `status()` 的 dict 加一鍵（放在 `"purge_last_s"` 之後）：

```python
            "hft": {
                "flagged_total": self._hft_flagged_total,
                "last_flagged": self._hft_last_flagged,
                "vlm_excluded_last_round": self._hft_vlm_excluded_last,
                "cooldown_last_round": self._hft_cooldown_last,
            },
```

- [ ] **Step 3b: 三個既有測試明確停用 H2（主線程裁決 2026-10-05，builder 回報）**

`test_scan_multi_page_completes_and_state_also_gets_a_turn`（約 :302）、
`test_whale_reaches_complete_within_24h_under_full_contention`（約 :4687）、
`test_restart_resumes_scan_from_persisted_cursor`（約 :4719）的假資料讓單一位址 30 天
超過 6,000 筆（實測 6,497／7,944），用預設設定會被 H2 除名、回傳 `"dropped:hft"`——這是
新功能的預期行為，測試本身測的是分頁／競爭／續掃，與 HFT 無關。修法：在這三個測試建構
scheduler 的 `cfg` 加 `hft_max_fills_30d=0`（若該測試經 `SchedulerHarness._build_scheduler`
建構、無法逐測試傳 cfg，則改 harness 的 cfg 一處，並加一行註解引用本 plan）。斷言本身不動。
**不要**改 `_sched` 的預設 cfg——HFT 測試與既有測試都要顯式表達自己要不要 H2。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_explore_scheduler.py -q`
Expected: 全部 PASS（既有測試數不得減少；`tick()` 回傳值新增 `"dropped:hft"`）

- [ ] **Step 5: Commit**

```bash
git add src/spark/publicapi/explore_scheduler.py tests/test_explore_scheduler.py
git commit -m "feat: explore scheduler HFT 過濾器——H1 併入 excluded、H2 逐頁判定除名、status.hft、candidate_keep_s"
```

---

### Task 4 `@sdd`：ApiConfig 與 run_api 接線 `candidate_keep_s`

**Files:**
- Modify: `src/spark/publicapi/config.py:203-205`（欄位）、`:472-473`（`from_env`）
- Modify: `scripts/run_api.py:71-85`（`ExploreScheduler(...)` 建構）
- Test: `tests/test_publicapi_config.py:450-455` 之後

- [ ] **Step 1: 寫失敗測試（追加在 `FILET_EXPLORE_PURGE` 測試之後）**

```python
def test_explore_candidate_keep_s_env():
    """HFT 過濾器 plan Task 4：退池候選保留期可設定，預設 7 天（spec §6 第 5 點）。"""
    assert ApiConfig.from_env(_env()).explore_candidate_keep_s == 7 * 86400
    assert ApiConfig.from_env(_env(FILET_EXPLORE_CANDIDATE_KEEP_S="86400")).explore_candidate_keep_s == 86400
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_publicapi_config.py -q -k candidate_keep_s`
Expected: FAIL（`AttributeError: ... 'explore_candidate_keep_s'`）

- [ ] **Step 3: 實作**

`config.py` 在 `explore_purge_enabled: bool = True` 後加：

```python
    # --- Explore 退池候選保留期（HFT 過濾器 plan 2026-10-05 Task 4）---
    # `FILET_EXPLORE_CANDIDATE_KEEP_S`：`ExploreStore.purge(candidate_keep_s=…)`，
    # 預設 7 天（spec §7.2：每天 30–70 顆換進換出、30 天內 129 顆回池，保留期省重回補）。
    explore_candidate_keep_s: int = 7 * 86400
```

`from_env` 的 `cls(...)` 在 `explore_purge_enabled=...` 後加：

```python
                   explore_candidate_keep_s=int(env.get("FILET_EXPLORE_CANDIDATE_KEEP_S")
                                                or cls.explore_candidate_keep_s),
```

`scripts/run_api.py` 的 `ExploreScheduler(...)` 呼叫，在既有 `purge_enabled=cfg.explore_purge_enabled` 那一行旁加 `candidate_keep_s=cfg.explore_candidate_keep_s,`。

- [ ] **Step 4: 驗收**

Run: `uv run pytest tests/test_publicapi_config.py -q && grep -n "candidate_keep_s=cfg.explore_candidate_keep_s" scripts/run_api.py`
Expected: PASS 且 grep 命中 1 行

- [ ] **Step 5: Commit**

```bash
git add src/spark/publicapi/config.py scripts/run_api.py tests/test_publicapi_config.py
git commit -m "feat: FILET_EXPLORE_CANDIDATE_KEEP_S——退池候選保留期可設定（預設 7 天）"
```

---

### Task 5 `@inline`：全量驗收

- [ ] **Step 1:** `uv run pytest -q` → 全綠（貼輸出末尾）。
- [ ] **Step 2:** `uv run ruff check src tests scripts` → 無錯。
- [ ] **Step 3:** 本機實跑一次 v5 DB 遷移：`uv run python -c "from spark.publicapi.explore_store import ExploreStore; s=ExploreStore('/tmp/hft-smoke.db'); print(s.schema_version())"` → `6`；再跑一次同指令 → 仍 `6`（冪等）。
- [ ] **Step 4:** 無 commit（本 task 只驗證）。

---

### Task 6 `@sdd`：文件

**Files:**
- Modify: `docs/superpowers/specs/2026-10-05-explore-hft-filter.md` §2 H2「資料」段
- Modify: `deploy/RUNBOOK.md`（§5.8k 之後新增 §5.8l）

- [ ] **Step 1: spec §2 H2 的「資料」條目改為**

```
- 資料：`SELECT COUNT(*) FROM fills WHERE address = ? AND time_ms >= now_ms − 30d`
  （走既有 `(address, time_ms)` 索引，不讀 `raw`）。**只用原始計數，不做觀測跨度折算**
  （2026-10-05 plan 裁決：逐頁評估下真 HFT 必在遍歷途中跨過門檻；折算會誤殺單日爆量的
  正常錢包）。
```

- [ ] **Step 2: RUNBOOK 新增 §5.8l**

```markdown
### 5.8l HFT 過濾器部署與觀測（2026-10-05 plan `docs/superpowers/plans/2026-10-05-explore-hft-filter.md`）

- 部署：§3.2 整包 rsync（從乾淨 worktree）→ `restart filet-api`。schema v5→v6 在啟動時自動遷移
  （只 `ALTER TABLE candidate ADD COLUMN` 四欄，秒級、冪等）；不需要維護窗、不需預熱快照
  （探索列結構未變）。
- 新 env（全部可省略）：`EXPLORE_HFT_MAX_VLM_MONTH_USD`（預設 1e8）、`EXPLORE_HFT_MAX_FILLS_30D`
  （6000）、`EXPLORE_HFT_COOLDOWN_S`（2592000）、`FILET_EXPLORE_CANDIDATE_KEEP_S`（604800）。
  任一 HFT 門檻設 0 ＝ 停用該閘（回退不必回退程式）。
- 觀測：`/api/ops/health` → `explore_refresh.hft`：`flagged_total`／`last_flagged`／
  `vlm_excluded_last_round`（預期 ≈10）／`cooldown_last_round`。journal 關鍵字 `判定 HFT`。
- 查被標記清單（唯讀）：
  `sudo python3 -c "import sqlite3;c=sqlite3.connect('file:/var/lib/filet-api/explore.db?mode=ro',uri=True);print(c.execute('SELECT address,hft_reason,hft_value,datetime(hft_until,\"unixepoch\") FROM candidate WHERE hft_until IS NOT NULL ORDER BY hft_value DESC').fetchall())"`
- 驗收（部署後 24 小時）：`/proc/pressure/io` some avg300 日級分布回到個位數；
  `/proc/$(systemctl show filet-api -p MainPID --value)/io` 的 `read_bytes` 增速 ≪ 8 MB/s；
  `host.jsonl` 的 `nginx_499_15m` 回到個位數；探索榜仍 300 列、`vlm_excluded_last_round` ≈ 10、
  首日 `flagged_total` 約 30–45（既有池內 ≥6,000 筆者會在各自下一次 scan 時被抓）。
- 既有池內已回補完成、短期內不會再 scan 的高頻位址：H2 要等它們下一次增量 scan 才評估；
  要立刻清掉可 `restart filet-api` 前先把 `EXPLORE_UPSTREAM_REFRESH` 維持 1，等一輪
  `fills_period_s`（高頻位址 6 小時）即可。
```

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-10-05-explore-hft-filter.md deploy/RUNBOOK.md
git commit -m "docs: HFT 過濾器 spec H2 改原始計數＋RUNBOOK §5.8l 部署觀測"
```

---

### Task 7 `@inline`：審查修正——增量軌也評估 H2、失敗路徑大聲、冷卻下限、同源窗口、分支測試

背景（reviewer 2026-10-05，主線程親驗）：H2 三個呼叫點全在 `_run_scan`；已回補完成
（`completeness=complete`）的位址之後只走增量 `fills` job（`_run_increment`），寫完頁沒有
評估 H2，所以部署當下池內 30 天 ≥ 6,000 筆但 vlm < $1e8 的約 33 顆永遠不會被抓（C1）。
另：預算暫停／完成分支無測試（W1）、`flag_hft` 拋例外會被泛用分類吃掉只留誤導的 CAS 警告（W2）、
H2 窗口常數與 scan 窗口常數不同源（S1）、`EXPLORE_HFT_COOLDOWN_S=0` 會形成每輪重抓重標的迴圈（S2）。

**Files:**
- Modify: `src/spark/publicapi/explore_scheduler.py`（`_run_increment` 約 1265 行後、`_hft_fills_exceeded`／`_flag_hft_fills` 約 1301–1321、`__init__` 計數器、`status()["hft"]`、import）
- Modify: `tests/test_explore_scheduler.py`（HFT 區塊，約 5403 行起）
- Modify: `docs/superpowers/specs/2026-10-05-explore-hft-filter.md` §2 H2「評估時機」、`deploy/RUNBOOK.md` §5.8l

- [ ] **Step 1: 寫失敗測試（追加到 HFT 區塊末尾）**

```python
class _PagesThenShortHL(FakeHL):
    """前 `full_pages` 頁滿頁，接著一頁 `short_n` 筆（短頁→scan 收尾），之後空頁。
    tid 全域遞增（與 `_FullPagesHL` 同理由）。"""

    def __init__(self, full_pages: int, short_n: int):
        super().__init__()
        self._full_pages = full_pages
        self._short_n = short_n
        self._pages = 0
        self._tid = 0

    def get_fills_page(self, address, start_ms, end_ms):
        self.calls.append(("fills", address, start_ms, end_ms))
        if start_ms == 0:                     # 左界探測，不算分頁
            return []
        self._pages += 1
        n = PAGE_LIMIT if self._pages <= self._full_pages else (
            self._short_n if self._pages == self._full_pages + 1 else 0)
        out = []
        for i in range(n):
            self._tid += 1
            out.append({"coin": "BTC", "tid": self._tid, "time": start_ms + i})
        return out


def _scan_fills_calls(hl) -> int:
    return len([c for c in hl.calls if c[0] == "fills" and c[2] > 0])


def test_h2_flags_on_scan_completion_branch(tmp_path):
    """門檻 5000：2 滿頁（4000，續頁分支不命中）＋短頁 1500 → scan 收尾 → 完成分支命中。"""
    hl = _PagesThenShortHL(full_pages=2, short_n=1500)
    sched, store, clock = _scan_only_scheduler(
        tmp_path, hl, ExploreConfig(candidate_pool=5, hft_max_fills_30d=5000))
    results = [sched.tick() for _ in range(12)]
    assert "dropped:hft" in results
    assert _scan_fills_calls(hl) == 3
    cand = store.get_candidate(_HFT_A)
    assert cand.active is False and cand.hft_value == 5500.0
    assert store.stats()["refresh_job"] == 0          # 沒排 partial_rescan／增量 job
    assert store.get_sync(_HFT_A) is None


def test_h2_flags_on_budget_pause_branch(tmp_path):
    """門檻 40000 ＝ 20 頁 × 2000：第 20 頁走 `max_pages_per_round` 暫停分支（`res.done`
    為 True、`result` 為 None），只有該分支的檢查能在這一頁命中。"""
    hl = _FullPagesHL()
    sched, store, clock = _scan_only_scheduler(
        tmp_path, hl, ExploreConfig(candidate_pool=5, hft_max_fills_30d=40_000))
    results = []
    for _ in range(60):
        r = sched.tick()
        results.append(r)
        if r == "dropped:hft":
            break
    assert results[-1] == "dropped:hft"
    assert _scan_fills_calls(hl) == 20
    assert store.get_candidate(_HFT_A).hft_value == 40_000.0


def test_h2_evaluates_on_incremental_page_for_complete_address(tmp_path):
    """C1：已 complete 的位址只走增量軌，增量頁寫入後也要評估 H2。"""
    clock = Clock(t=1_700_000_000.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    store.upsert_candidates([(_HFT_A, None, 1, None)], as_of=clock.now())
    store.bootstrap_address_fills(_HFT_A, clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                  window_end_ms=now_ms, params_fp="")
    _complete_scan(store, _HFT_A, result="complete", reason="retention_boundary_verified",
                   window_end_ms=now_ms)
    clock.t += 3600                                   # 增量到期、窗口夠寬放得下 2000 筆
    store.enqueue(f"{_HFT_A.lower()}:fills", _HFT_A.lower(), "fills", 2, clock.now())
    hl = _FullPagesHL()
    sched = _sched(store, hl, clock=clock,
                  cfg=ExploreConfig(candidate_pool=5, hft_max_fills_30d=2000),
                  fills_min_period_s=1, fills_max_period_s=1,
                  state_every_s=10**9, portfolio_every_s=10**9, ledger_every_s=10**9)
    sched._bootstrapped = True
    r = sched.tick()
    assert r == "dropped:hft"
    cand = store.get_candidate(_HFT_A)
    assert cand.active is False and cand.hft_value == 2000.0
    assert store.stats()["refresh_job"] == 0
    assert store.count_fills_since(_HFT_A, 0) == 0


def test_h2_flag_failure_is_loud_and_counted(tmp_path, monkeypatch, caplog):
    """W2：`flag_hft` 拋例外 → ERROR log＋`status()["hft"]["errors"]`，不進泛用隔離路徑，
    位址暫留池內（下一輪對帳補 job 後重判）。"""
    import sqlite3
    hl = _FullPagesHL()
    sched, store, clock = _scan_only_scheduler(
        tmp_path, hl, ExploreConfig(candidate_pool=5, hft_max_fills_30d=6000))

    def boom(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "flag_hft", boom)
    with caplog.at_level(logging.ERROR, logger="spark.publicapi.explore_scheduler"):
        results = [sched.tick() for _ in range(6)]
    assert "dropped:hft" in results
    assert sched.status()["hft"]["errors"] == 1
    assert sched.status()["hft"]["flagged_total"] == 0
    assert store.get_candidate(_HFT_A).active is True
    assert any("flag_hft 失敗" in rec.getMessage() for rec in caplog.records)
    assert sched.status()["quarantined"] == 0 if "quarantined" in sched.status() else True


def test_h2_cooldown_has_one_hour_floor(tmp_path):
    """S2：冷卻設 0 會每輪重抓重標；下限 `HFT_MIN_COOLDOWN_S`（3600）。"""
    from spark.publicapi.explore_scheduler import HFT_MIN_COOLDOWN_S
    assert HFT_MIN_COOLDOWN_S == 3600
    hl = _FullPagesHL()
    sched, store, clock = _scan_only_scheduler(
        tmp_path, hl, ExploreConfig(candidate_pool=5, hft_max_fills_30d=6000, hft_cooldown_s=0))
    for _ in range(6):
        if sched.tick() == "dropped:hft":
            break
    assert store.get_candidate(_HFT_A).hft_until == clock.now() + HFT_MIN_COOLDOWN_S
```

`_complete_scan` 是檔內既有的測試 helper（`test_b7_i_incremental_continues_while_rescan_in_progress` 有用法）；
`logging` 已 import。`test_h2_flag_failure_is_loud_and_counted` 最後一行的 `quarantined` 鍵若 `status()` 沒有，請改為
斷言 `store.stats()["refresh_job"] == 0`（job 已 complete、未被隔離重排）。

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_explore_scheduler.py -q -k "completion_branch or budget_pause or incremental_page or loud_and_counted or one_hour_floor"`
Expected: 5 條中至少 `incremental_page`／`loud_and_counted`／`one_hour_floor` FAIL（`ImportError: HFT_MIN_COOLDOWN_S`、`KeyError: 'errors'`、`r == "ran:fills"`）

- [ ] **Step 3: 實作**

(a) 模組常數（放在 `PURGE_MAX_FILLS` 之後）：

```python
# HFT 過濾器 S2（reviewer 2026-10-05）：冷卻下限——設 0 會讓被標記位址下一輪回池、重抓
# 3 頁、再被標記，形成迴圈。環境變數可以更長，不能更短。
HFT_MIN_COOLDOWN_S = 3600
```

(b) import：`from spark.publicapi.explore_fills_sync import WINDOW_DAYS`（沿用檔內既有 import 行），
`_hft_fills_exceeded` 的 `since_ms` 改用 `WINDOW_DAYS`（與 scan 窗口同源，工程原則 #1）；
`hl_explore` 那行 import 若因此不再用到 `FILLS_WINDOW_DAYS` 就拿掉。

(c) `__init__` 加 `self._hft_errors = 0`；`status()["hft"]` 加 `"errors": self._hft_errors`。

(d) `_flag_hft_fills` 改為：

```python
    def _flag_hft_fills(self, address: str, n: int, now: float) -> bool:
        """H2 命中落地：`flag_hft`（除名＋清資料＋冷卻）、計數、告警、標 dirty。
        呼叫端已先把手上的 job `_complete`。失敗（例如 SQLite locked）**不往外拋**
        （工程原則 #3：往外拋會被 `_run_job` 的泛用分類吃掉、對已 complete 的 job 做
        reschedule，只留一行誤導的 CAS 警告）——改為 ERROR log＋`hft.errors` 計數；
        位址暫留池內，下一輪 candidates 的 `reconcile_scan_jobs` 會補回 job 再判一次。
        回傳是否落地成功。"""
        cooldown_s = max(self._cfg.hft_cooldown_s, HFT_MIN_COOLDOWN_S)
        try:
            counts = self._store.flag_hft(address, reason="fills", value=float(n), now=now,
                                          cooldown_s=cooldown_s)
        except Exception:  # noqa: BLE001 — 失敗路徑要大聲且可計數，不交給泛用隔離
            self._hft_errors += 1
            logger.error(
                "explore scheduler: %s 判定 HFT（30 天 %d 筆）但 flag_hft 失敗——位址暫留池內，"
                "等下一輪對帳補 job 後重判", address, n, exc_info=True)
            self._notify_dirty()
            return False
        self._hft_flagged_total += 1
        self._hft_last_flagged = {"address": address.lower(), "fills_30d": n}
        logger.warning(
            "explore scheduler: %s 判定 HFT（30 天 %d 筆 ≥ %d）——除名、冷卻 %d 秒、清除 %s",
            address, n, self._cfg.hft_max_fills_30d, cooldown_s, counts)
        self._notify_dirty()
        return True
```

(e) `_run_increment`：在 `self._store.insert_fills_page(job.address, res.accepted, res.state)` 之後、
`if not res.done:` 之前加第四個呼叫點：

```python
        # HFT 過濾器 H2 第四個評估點（reviewer C1，2026-10-05）：已 complete 的位址
        # 只走這條增量軌，不評估等於永遠不評估。
        n = self._hft_fills_exceeded(job.address, now)
        if n is not None:
            self._complete(job)
            self._flag_hft_fills(job.address, n, now)
            return "dropped:hft"
```

(f) `_hft_fills_exceeded` docstring 的「只在 scan 寫頁之後呼叫」改為「只在 scan／增量寫頁之後呼叫」。

(g) spec §2 H2「評估時機」改為：「scan **每寫入一頁**與**增量軌每寫入一頁**都評估一次（索引計數，
毫秒級），達標即中止並除名；scan 進入 done／partial 時再評估最後一次。**不在發布器的每分鐘迴圈裡評估**。」
RUNBOOK §5.8l：env 清單的 `EXPLORE_HFT_COOLDOWN_S` 後加「（下限 3600，設更小視同 3600）」；觀測段加
`errors`（flag 落地失敗計數，應為 0）；「既有池內已回補完成…」那條改為「既有池內已回補完成的位址在
下一次增量頁寫入時評估（高頻位址週期 1–6 小時），首日內應全部處理完」。

- [ ] **Step 3b: 再三個既有測試明確停用 H2（主線程裁決 2026-10-05，builder 回報）**

增量軌評估點讓 `test_b7_iii_fills_and_probe_backlog_9_to_1_ratio`、
`test_s7f_overdue_verify_waits_for_its_page_share`、`test_s3_overdue_verify_burst_never_starves_increments`
的假資料在增量軌累積超過 6,000 筆而回 `dropped:hft`。處理與 Task 3 Step 3b 相同：只在
`_sched(...)` 的 cfg 加 `hft_max_fills_30d=0` 並加一行註解，斷言不動。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_explore_scheduler.py -q`
Expected: 全部 PASS（153 ＋ 5 新增 ＝ 158）

- [ ] **Step 5: Commit**

```bash
git add src/spark/publicapi/explore_scheduler.py tests/test_explore_scheduler.py docs/superpowers/specs/2026-10-05-explore-hft-filter.md deploy/RUNBOOK.md
git commit -m "fix: HFT H2 增量軌也評估（reviewer C1）＋flag 失敗大聲計數、冷卻下限 1h、窗口同源、分支測試"
```

## 自我審查

- **Spec 覆蓋**：H1（Task 2＋3c）、H2 逐頁（Task 3d/e）、生命週期 §3.1–3.5（Task 1 flag_hft／purge／upsert、Task 3）、§3.6 保留期可設定（Task 1 purge 參數既有、Task 3b、Task 4）、§4 觀測（Task 3f、Task 6）、§6 裁決 1–3（預設值 Task 2）、裁決 4 詳情頁不動（無 task，刻意）、裁決 5（Task 4）。H3 不做。
- **型別一致**：`flag_hft(address, *, reason, value, now, cooldown_s) -> dict[str,int]`、`count_fills_since(address, since_ms) -> int`、`hft_cooldown_addresses(now) -> set[str]`、`get_candidate(address) -> Candidate | None`、`hft_by_volume(payload, max_vlm_month_usd: Decimal) -> set[str]`、`ExploreScheduler(candidate_keep_s: float)`、`status()["hft"]` 四鍵——Task 1/2/3/4 測試與實作用的是同一組名字。
- **風險**：`_run_scan` 三個呼叫點必須在 `insert_scan_page`／`complete_scan` **之後**（計數要含本頁）；完成分支必須在既有 `_complete(job)` 之後、`is_active` 之前（避免對已除名位址排 `partial_rescan`）。
