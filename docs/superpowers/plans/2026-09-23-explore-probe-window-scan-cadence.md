# Explore 探測窗全史化、遍歷排程即時化、v3 游標正規化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓第八次部署（`docs/superpowers/plans/2026-09-22-explore-fills-coverage-verdict-fix.md`）之後仍卡在
「分析待完成」的三群位址在一次部署後收斂：約 120 個「疑似截斷」（探測窗太窄）、35 個遍歷被排到未來（排程缺陷）、
95 個 v3 遺留列（游標偏移 ~5h 被判未抵達終點）。目標：重啟後 6–8 小時內，池內 300 個位址「能完成的都完成」
（預估 complete 121 → ~250），剩下的是證據真的不足者。

**Architecture:** 三處各自獨立、都只影響「快慢」不改判準的嚴格方向：(1) 左界探測的查詢窗從
`[window_start − 1d, window_start)` 改為 `[0, window_start)`——問的本來就是「窗口起點之前有沒有**任何**可查成交」，
全史窗一頁就能回答且成本相同；(2) `fills_scan` job 的首次到期一律 `now`（遍歷節奏是逐頁，不該套增量週期的分散）；
(3) schema v5 遷移：把既有 `truncation_suspected` 重設為 `unknown` 讓新探測重跑、把 v3 遺留 scan 的游標正規化到
`window_end_ms` 並就地重算結論。全部經既有的 `scan_verdict` 單一來源，不新增第二個結論路徑。

**Tech Stack:** Python 3.11 + uv、SQLite `explore.db`（schema v4 → v5）、pytest 全離線、systemd `filet-api`。

---

## ⚠️ 正式機狀態（2026-09-23）

- 第八次部署（`bbd7adf`，schema v4）2026-09-22 15:45 UTC 上線，24h 觀測至 09-23 15:45 UTC；至 02:00 UTC 為止
  429＝0、Traceback（非已知類）＝0、follower 兩個 unit 時間戳不變。觀測日誌在前一份 plan 的「部署後觀測日誌」節。
- **正式機仍有真實用戶跟單中**。本次同樣只重啟 `filet-api`，follower 與 timer 不動；回退＝前一份 plan §5.8f Step 7
  的同一套（本次備份檔名 `explore.db.pre-v5.bak`）。
- drop-in `explore-v4-verdict.conf` 的 `FILET_EXPLORE_SPECIAL_SERVE_RATIO_UNTIL=2026-09-24T00:00:00Z`——本次會新增
  ~120 個探測，部署時把到期時間**順延 24h**（Task 5）。
- **2026-09-23 15:57 UTC 第九次已部署**（見文末「部署紀錄」）；`_UNTIL` 已順延為 `2026-09-24T15:55:53Z`；
  回退＝RUNBOOK §5.8g Step 7（`explore.db.pre-v5.bak` 已建，1.07 GB）。

## 背景：三個現象與各自根因（均已在正式機唯讀查證，2026-09-23 00:55–02:30 UTC）

| 群組（池內 300） | 數量 | 根因 | 證據 |
|---|---|---|---|
| `truncation_suspected`（80 個 `traversal_incomplete`＋41 個 `left_boundary_truncated`） | ~121 | 探測窗只查 `[ws−1d, ws)`，低頻帳戶那一天沒交易就回空，再配合「首次活動早於窗口」被判疑似截斷 | 對 `0x5cee0ca5…` 實測：1 天窗 **0 筆**；全史窗 `[0, ws)` **2000 筆、最早 2025-02-26**（HL 保留一年）。本地 `fills` 表無法反證（0/23 有更早成交，因為我們只抓過 30 天窗） |
| `backfilling`（16 個 0 頁、19 個未起步） | 35 | `_enqueue_address_jobs` 把 `fills_scan` 與 `fills` 一起用 `now + _spread(address, 增量週期)` 排；Task 5b 後冷門週期最長 24h → 第一頁遍歷可等 24h；再入池的地址 job 先被刪再被同算式重建，遍歷到一半也被延後；同輪 `_ensure_scan_job` 後跑、見 job 已存在而跳過 | 60 個 `fills_scan` job 全部排在 33 分鐘～23.9h 後、0 個到期；fills 額度每 15 分鐘只用 0–8 頁（上限 ~15） |
| `traversal_incomplete`（v3 遺留） | 95 | v3 短頁收尾寫的 `cursor_ms` 比列上 `window_end_ms` 少 ~0.2 天；Task 10 W3 的 `scan_verdict` 嚴格要求游標抵達終點 | 95 個全是 `initial/done/pages_done=0/reason=count_below_retention_threshold_probe_empty`，`window_end − cursor ≈ 0.2d` |

三者都是**方向安全**的缺陷（只少判 complete，沒有錯判完整）。

## 需要使用者確認的裁決（寫 plan 時提出，開工前確認）

| 代號 | 裁決 | 建議 |
|---|---|---|
| **D-K** | 探測窗改為 `[0, window_start)`（全史）。語義不變：「窗口起點之前有沒有任何可查成交」；HL 留存若是「最近 N 筆」（後綴截斷），全史窗回非空即代表截斷邊界早於窗口起點，窗內不可能被截。 | 採用 |
| **D-L** | 探測回空時的判斷維持 Task 10 W5 的對稱模糊帶（`first_activity ≥ ws+1d` → `no_earlier_activity`；`< ws−1d` → `truncation_suspected`；其間 unknown）。全史窗回空＝「窗口前確無可查成交」，此時 `truncation_suspected` 的嫌疑才是實質的。 | 維持 |
| **D-M** | 既有 `truncation_suspected` 列（池內 ~121）在遷移時**重設為 unknown**，由獨立探測用新窗重跑（每個 1 頁）；不等 24h 重掃。 | 採用 |
| **D-N** | v3 遺留列（`pages_done=0` 且 v3 reason 為 `count_below_retention_threshold[_probe_empty]`、`window_end − cursor ≤ 1d`）在遷移時把 `cursor_ms` 正規化為 `window_end_ms` 並就地重算結論——而不是等 24h 重掃（95 頁）。 | 採用 |
| **D-O** | `fills_scan` 首次到期改 `now`（≤60s jitter）；`fills`（增量）維持 `_spread(週期)`。 | 採用 |

---

## File Structure

| 檔案 | 責任 |
|---|---|
| `src/spark/publicapi/explore_scheduler.py` | Task 1（`_enqueue_address_jobs` 的 `fills_scan` 到期）、Task 2（`_run_probe` 的探測窗） |
| `src/spark/publicapi/explore_store.py` | Task 3（schema v5 遷移：重設 truncation_suspected、v3 游標正規化、就地重算、報告） |
| `tests/test_explore_scheduler.py`、`tests/test_explore_store.py` | 對應測試；Task 4 整合驗收 |
| `deploy/RUNBOOK.md` | Task 5：§5.8g 第九次部署程序 |

**不得改動**：`explore_fills_sync.scan_verdict`／`_applicable_boundary` 的規則本身（判準已定案）、`hl_budget.py`、`web/`、
`src/spark/copytrade/`、`src/spark/filet/`。

---

## Task 1: `fills_scan` 首次到期改為 `now` `@inline`

**Files:** `src/spark/publicapi/explore_scheduler.py:1033-1054`；`tests/test_explore_scheduler.py`

- [ ] **Step 1: 寫失敗測試**

```python
def test_new_address_initial_scan_job_is_due_within_60s(tmp_path):
    """D-O：遍歷節奏是逐頁，第一頁不該等增量週期的分散。"""
    sched = _scheduler_with_cold_candidate(tmp_path, address=ADDR, rank=250)   # 冷門：週期 24h
    sched._run_candidates(now=NOW)                                             # 觸發 _enqueue_address_jobs
    job = sched._store.get_job(f"{ADDR}:fills_scan")
    assert job is not None and job.next_attempt_at <= NOW + 60


def test_fills_incremental_job_still_spread_by_period(tmp_path):
    """反向護欄：不得順手改壞增量的分散。"""
    sched = _scheduler_with_cold_candidate(tmp_path, address=ADDR, rank=250)
    sched._run_candidates(now=NOW)
    job = sched._store.get_job(f"{ADDR}:fills")
    assert job.next_attempt_at == NOW + _spread(ADDR, sched.fills_period_s_for(ADDR))


def test_reentering_address_resumes_scan_immediately(tmp_path):
    """候選池換血：退池（job 被刪、running scan 保留）→ 再入池 → scan job 60 秒內到期且續跑同一 scan_id。"""
    h = _t7a_harness(tmp_path); h.set_fill_count(ADDR, window_fills=20_000)
    h.run_for(hours=1); sid = h.scan_id(ADDR); cur = h.scan_cursor(ADDR)
    h.churn_out(ADDR); h.run_for(minutes=31)          # 一輪 candidates：job 被刪
    h.churn_in(ADDR);  h.run_for(minutes=31)          # 再入池
    job = h.store.get_job(f"{ADDR}:fills_scan")
    assert job.next_attempt_at <= h.now + 60
    assert h.scan_id(ADDR) == sid and h.scan_cursor(ADDR) >= cur
```

- [ ] **Step 2: 執行確認失敗** — `uv run pytest tests/test_explore_scheduler.py -k "initial_scan_job_is_due or still_spread or reentering" -v` → FAIL。
- [ ] **Step 3: 實作** — `_enqueue_address_jobs` 的 `for kind, priority, period_s in needed:` 迴圈內，`kind == "fills_scan"` 時
  到期為 `now + self._jit(60.0)`（既有 jitter helper），其餘維持 `now + _spread(address, period_s)`。註解寫明 D-O 與根因。
- [ ] **Step 4: 全套** — `uv run pytest -q` 全綠、`ruff` 過。
- [ ] **Step 5: Commit** — `fix: fills_scan 首次到期改為 now——遍歷節奏逐頁，不套增量週期分散（D-O）`

---

## Task 1b: `resume_running` 一律把 job 拉到 `now`（MIN 語義）`@inline`

> **主線程 2026-09-23 03:10 追加**（Task 1 builder 的反向護欄實測揭露前一份 plan 的機制敘述有誤）：
> 15 個「遍歷到一半」的 scan 其實已抵達終點，卡住的是 v4 遷移重新打開 scan 時**沒有處理的舊 24h 重掃 job**
> （部署前排下，`due − created = 24.0h`）。`_ensure_scan_job` 的 `resume_running` 分支只在「沒有 job」時補排，
> job 存在但排在很遠的未來就不管——這是缺口。

**Files:** `src/spark/publicapi/explore_scheduler.py`（`_ensure_scan_job`／`_needs_scan_job` 的 `resume_running`）；測試同檔。

- [ ] **Step 1: 失敗測試** — `test_resume_running_pulls_a_far_future_scan_job_to_now`：有 running scan、且已存在一個
  `next_attempt_at = now + 24h` 的 `fills_scan` job → 一輪對帳後該 job 的 `next_attempt_at <= now + 60`（`store.enqueue`
  的 MIN 語義），`created_at`／`attempts` 不變。反向護欄：把「一律 enqueue」改回「只在缺 job 時」→ 轉紅。
- [ ] **Step 2: 實作** — `resume_running` 時**無論 job 是否存在**都呼叫 `store.enqueue(key, address, "fills_scan", priority, now)`
  （`enqueue` 對既有 job 只做 `MIN`，冪等）。`initial_missing` 同理。`partial_due`／`verify_needed` 不動。
  日誌「對帳補排」只在真的新建時印（回傳 True），避免每輪 300 行。
- [ ] **Step 3: 全套綠、ruff 過。Commit** — `fix: resume_running 一律以 MIN 語義把 fills_scan job 拉到 now——v4 遷移遺留的 24h 舊 job 不再擋住已重開的遍歷`

---

## Task 2: 探測窗改為全史 `[0, window_start)` `@inline`

**Files:** `src/spark/publicapi/explore_scheduler.py`（`_run_probe` 的 `probe_start`）、`tests/test_explore_scheduler.py`

- [ ] **Step 1: 寫失敗測試**

```python
def test_probe_queries_full_history_before_window_start():
    """D-K：探測窗起點為 0（全史），終點為 window_start − 1。"""
    sched, hl = _scheduler_with_probe_capture(ADDR, window_start_ms=WS)
    sched._run_probe((ADDR, SCAN_ID), now=NOW)
    assert hl.last_probe_range == (0, WS - 1)


def test_low_frequency_account_with_old_fills_is_earlier_fills_seen():
    """0x5cee0ca5 的實況：ws 前一天沒交易、但一年前有 → 全史窗非空 → earlier_fills_seen，不再是疑似截斷。"""
    sched, store = _scheduler_with_fills_at(ADDR, fill_times=[WS - 200 * DAY], first_activity_ms=WS - 300 * DAY)
    sched._run_probe((ADDR, SCAN_ID), now=NOW)
    assert store.get_left_boundary(ADDR, WS).state == "earlier_fills_seen"


def test_empty_full_history_with_old_account_is_truncation_suspected():
    """全史窗都回空且帳戶明顯更老 → 這時的截斷嫌疑才是實質的（D-L 維持）。"""
    sched, store = _scheduler_with_fills_at(ADDR, fill_times=[], first_activity_ms=WS - 300 * DAY)
    sched._run_probe((ADDR, SCAN_ID), now=NOW)
    assert store.get_left_boundary(ADDR, WS).state == "truncation_suspected"


def test_probe_page_settles_by_returned_count():
    """成本：全史窗可能回滿 2000 筆，權重預留 120、依實際筆數結算——與一般 fills 頁同一套，不得繞過限流器。"""
```

- [ ] **Step 2: 執行確認失敗**。
- [ ] **Step 3: 實作** — `_run_probe`：`probe_start = 0`、`probe_end = scan.window_start_ms - 1`；`validate_page` 的區間參數同步；
  docstring 改寫語義（「窗口起點之前有沒有任何可查成交」）與 D-K 的截斷推理。`_PROBE_WINDOW_MS` 保留給 D-L 的模糊帶
  （`first_activity` ± 1d），改名 `_FIRST_ACTIVITY_BAND_MS` 以免誤解為探測窗。
- [ ] **Step 4: 既有測試** — Task 3／10／11 的探測測試若 fixture 依賴 1 天窗（例如把成交放在 `ws−1d` 內），改成放在
  任何 `< ws` 的時間即可；斷言語義不變。
> **主線程裁決（2026-09-23，builder 回報 harness 時鐘衝突）**：`SchedulerHarness` 用 `Clock(t=0.0)` 起算，
> `window_start_ms = now − 30d` 為**負數**；正式機永遠是 ~1.79e12 的正數。這是 harness 保真度缺陷（Task 11 那次
> `first_activity_ms=0` 把 300 個地址全判成 `no_earlier_activity` 也是同一根源），與 D-K 的「探測起點為絕對 0」衝突。
> **選項 1，並補一個配套**：
> (a) `SchedulerHarness` 的時鐘起點改為真實量級（`Clock(t=1_700_000_000.0)`，2023-11-14），並把 `fresh_scan_window`
>     等用到的 `now_ms` 一律由該時鐘導出；harness 內任何寫死的相對時間（`seed_dense_fills(start_ms=-32*DAY...)`、
>     `seed_stale_partial_row`、`window_start_ms=0/1` 之類）改成相對 `h.now`／`ws` 的表達式。
> (b) 預設候選的 portfolio 首次活動時間不得再是 `0`：`_t7a_default_portfolio` 對**所有**候選一律 `ws + 2*DAY`
>     （Task 10 W5 已對 `set_fill_count`／`set_fills_all_same_ms` 這樣做，現在推廣到 bootstrap 的 297 個）——
>     否則時鐘轉正後 `first_activity=0 < ws−1d` 會把 297 個空地址全判成 `truncation_suspected`，那是 fixture 不真實，
>     不是判準錯。
> (c) 改完後 Task 7a／7b／8／8b／10／11 整個家族逐條重跑；**任何轉紅都要分析原因並回報**——若某條測試原本依賴
>     「t=0／負窗口」才成立，那條測試本身就有問題，回報後由主線程裁決，不得靜默調整斷言。
> (d) 在 harness 的 `Clock` 建構處加一行註解與一條測試 `test_harness_window_start_is_positive_epoch`，防止再退回 t=0。

- [ ] **Step 5: 全套綠、ruff 過。Commit** — `fix: 左界探測改查全史 [0, window_start)——低頻帳戶不再被 1 天窗誤判疑似截斷（D-K）`

---

## Task 3: schema v5 遷移——重設 truncation_suspected、v3 游標正規化、就地重算 `@inline`

**Files:** `src/spark/publicapi/explore_store.py`（`_SCHEMA_VERSION=5`、`_migrate_v4_to_v5`、報告）、`tests/test_explore_store.py`

**原則（沿用 D-G）**：原子＋冪等＋可重跑；不刪 `fills`；不建 job；輸出工作量報告；只改「結論」與「證據狀態」，
游標只在 D-N 明定的條件下正規化。

- [ ] **Step 1: 寫失敗測試**

```python
def test_migrate_v4_to_v5_resets_truncation_suspected_to_unknown():
    """D-M：池內 truncation_suspected → unknown，且 left_boundary_window_start_ms/at 清空，成為探測候選。"""

def test_migrate_v4_to_v5_normalizes_v3_cursor_and_recomputes():
    """D-N：pages_done=0、v3 reason、window_end − cursor ≤ 1d → cursor := window_end；結論經 scan_verdict 重算。
    有正面證據者 → complete；證據 unknown 者 → partial/left_boundary_unknown（等探測）。"""

def test_migrate_v4_to_v5_does_not_touch_other_cursors():
    """反向護欄：pages_done>0 或差距 >1d 或非 v3 reason 的 scan 游標一字不動。"""

def test_migrate_v4_to_v5_is_rerunnable_and_creates_no_jobs():

def test_migrate_v4_to_v5_reports_workload():
    """report.work: probes_needed（重設後 unknown 數）、cursors_normalized、verdicts_recomputed。"""
```

- [ ] **Step 2: 執行確認失敗**。
- [ ] **Step 3: 實作** — 比照 `_migrate_v3_to_v4`（顯式 transaction、版本閘門）。SQL 骨架：

```sql
-- D-M：重設疑似截斷（只限池內 active；退池列不動）
UPDATE fills_sync SET left_boundary='unknown', left_boundary_window_start_ms=NULL, left_boundary_at=NULL
 WHERE left_boundary='truncation_suspected'
   AND address IN (SELECT address FROM candidate WHERE active=1);
-- D-N：v3 遺留游標正規化
UPDATE fills_scan SET cursor_ms=window_end_ms
 WHERE status='done' AND pages_done=0
   AND reason IN ('count_below_retention_threshold','count_below_retention_threshold_probe_empty')
   AND window_end_ms - cursor_ms BETWEEN 0 AND 86400000;
-- Task 1b 的資料面：running scan 若還掛著排在未來的 fills_scan job，拉到遷移當下（純狀態修正，不建 job）
UPDATE refresh_job SET next_attempt_at=:now
 WHERE kind='fills_scan' AND next_attempt_at > :now + 60
   AND address IN (SELECT address FROM fills_scan WHERE status='running');
```

  報告新增 `scan_jobs_advanced`。
  之後對「游標被正規化」與「證據被重設」的每個 `fills_sync` 列呼叫既有的 `_recompute_verdict_locked`（同一 transaction 內，
  結論仍只出自 `scan_verdict`）。**不得**在遷移裡另寫一套判斷。
> **遷移 dry-run 用的複本已備妥（主線程 2026-09-23 02:32 UTC，唯讀備份）**：
> `/private/tmp/claude-501/-Users-jim-projects-spark/0c67e915-40ca-429f-9b6d-7a60afa4e12a/scratchpad/v4snap.db`
> （schema 4、`fills` 1,557,151）。**不要動它本體**，`cp` 一份再跑。基線：池內 `left_boundary`
> earlier_fills_seen 124／no_earlier_activity 20／**truncation_suspected 122**／unknown 34；符合 D-N 條件的 v3 遺留 scan
> **164 個**（含退池地址；池內約 95）。預期 report：`probes_needed` ≈ 122+34、`cursors_normalized` ≈ 164。

> **主線程裁決（2026-09-23）**：既有把 schema 終點版本寫死為 4 的五條測試，照 Task 4（v3→v4）前例更新為 5
> （測試名 `_v4_`→`_v5_`、精確相等斷言 4→5、遷移鏈延長到 v5），docstring 註明版本升級；不得改成 `>= 4` 之類
> 版本無關的迴避寫法；若任何一條需要改動「4→5 以外」的期望值，停下來報。

- [ ] **Step 4: 對正式機複本實跑**（唯讀取得複本，本機執行；不連正式機做任何寫入）——回報遷移前後分佈、
  `probes_needed`、`cursors_normalized`、`verdicts_recomputed`，以及 `fills` 筆數前後相同、非目標 scan 游標零漂移。
- [ ] **Step 5: 全套綠、ruff 過。Commit** — `feat: explore.db schema v5——重設疑似截斷、v3 游標正規化、就地重算（D-M／D-N）`

---

## Task 3c: 審核修正——`resume_running` 的 MIN 拉近不得拆掉隔離（C1）＋對帳計數語義（W1）`@inline`

> **reviewer（opus，fresh）2026-09-23 對 `bbd7adf..HEAD -- src/` 的審查**：報告在
> `<scratchpad>/review-v5-2026-09-23.md`，重現腳本 `repro_e2e.py`（主線程已親跑重現）。判定「可部署，條件：C1 進觀測
> 清單並在下一輪修」——主線程裁決**改為部署前修**：修法小、部署尚未發生，而且失敗模式是無限迴圈打 HL（工程原則 2），
> 不該帶著已知的迴圈上線。

**C1（Critical，本次 diff 引入的回歸）**：Task 1b 讓 `resume_running`／`initial_missing` 無論 job 是否存在都 `enqueue(now)`
（MIN 語義），但 `_quarantine` 對 `fills_scan` 只把 job 推到 `now+86400` 並寫 `set_scan_error`，**scan 仍是 `running`**
→ 下一輪對帳（~30 分鐘）把隔離中的 job 拉回 `now`；隔離時刻意不 bump `attempts`，所以「放行 → 失敗 → 再隔離 → 再拉回」
無限迴圈。舊版（`bbd7adf`）對帳不會拉回，確認為回歸。正式機現況 0 個隔離中的 `fills_scan` job（潛伏）。

**修法**：`_ensure_scan_job` 的 `resume_running`／`initial_missing` 在 job **已存在且 `job.last_error is not None`**（隔離或退避中）
時**不拉近**；其他情況維持 Task 1b 的 MIN 拉近。理由：`last_error` 是「這個 job 正在受失敗處理」的唯一狀態標記，
隔離／退避的到期時間由失敗處理路徑擁有，對帳不得覆蓋。測試：
- `test_reconcile_does_not_pull_forward_a_quarantined_scan_job`：reviewer 的 e2e 情境——tick 隔離（`next_at = now+86400`）
  → 對帳一輪 → `next_at` 仍為 `now+86400`。反向護欄：拿掉 `last_error` 檢查 → 轉紅。
- `test_reconcile_still_pulls_forward_a_healthy_far_future_job`：Task 1b 的既有測試不得轉紅（無 `last_error` 的遠期 job 仍被拉到 now）。
- 既有 `test_quarantine_of_fills_scan_job_writes_scan_error` 擴充：tick → 對帳 → 斷言隔離仍在。

**W1（一併修）**：對帳回傳的 `resume_running` 計數現在數的是「狀態上需要」而非「真的補排」，每輪穩定回報 ~72，
會成為部署後觀測的假訊號。改成兩個計數：`resume_running_created`（`enqueue` 回 True）與 `resume_running_pulled_forward`
（既有 job 被拉近的次數，由 `enqueue` 前後 `next_attempt_at` 比較得出）；docstring 同步（「冪等、重跑零變更」改為
「重跑零新建」）。RUNBOOK §5.8g Step 6 的觀測用新名稱。

**W3（不在本 task，列待議）**：`fills_verify` 的 `resume_running` 仍以「job 存在」為門檻（Task 1b 範圍收斂）；
`VERIFY_SPREAD_S = 48h`，正式機 running verify = 0，未觸發。

**W2（寫進資料極限）**：`earlier_fills_seen` 只及於 `userFillsByTime` 可見的成交；該 endpoint **不回 TWAP 分片成交**
（見 `~/.claude/rules/wallet-analysis.md` 與 memory）。「complete」語義是「對 `userFillsByTime` 完整」，用 TWAP 的錢包
其成交統計仍會少算，這是 v3 起就存在的限制，本 plan 不擴大。

> **主線程裁決（2026-09-23，builder 回報範圍衝突）**：`ExploreStore` 沒有「唯讀取單一 job」的方法，C1／W1 都需要。
> 放寬範圍：新增 `ExploreStore.get_job(key) -> Job | None`（lock 下 SELECT、沿用 `claim_due` 的 row→Job 映射、不 claim 不改
> lease）＋ `test_get_job_is_read_only`。C1 主判斷用 `job.last_error`（隔離是 job 層級事實；`running_scan.last_error` 只當第二道保險）；
> W1 以 `enqueue` 前後 `next_attempt_at` 比較區分 `_created`／`_pulled_forward`／不計。diff 範圍加入 `explore_store.py` 與其測試檔。

**Commit**：`fix: 對帳不得拉近隔離中的 fills_scan job（C1 回歸）；對帳計數分為新建／拉近（W1）`

---

## Task 4: 整合驗收（harness）`@inline`

**Files:** `tests/test_explore_scheduler.py`

- [x] `test_reset_truncation_rows_resolve_via_full_history_probe`：種 20 個「遷移後 unknown、scan done、無 job」且上游
  在很久以前有成交的位址 → 24h 內全部 `complete`、零遍歷頁、`probes_executed ≥ 20`；只破壞探測窗（改回 1 天）→ 轉紅。
  ✅ 一次寫成即綠：20/20 complete、`scan_pages_for==0`、`probes_executed==24`；反向護欄（`probe_start` 改回
  `window_start_ms-1天`）→ 0/20 轉紅（`probes_executed=304`）；revert 後 `git diff --stat src/` 清空。
- [x] ~~`test_cold_addresses_start_traversal_immediately_after_bootstrap`：5 分鐘內抓到~~ → **主線程裁決（2026-09-23，builder 實測「5 分鐘」
  在 300 地址冷啟動下不可能達成、且該錨例未經量測）改為 `test_new_cold_address_first_page_is_not_deferred_by_period_spread`**：
  harness 暖機 24h 到穩態後新增**一個真正的新地址**（rank 250、走 bootstrap 路徑），斷言 (i) job 建立後 `next_attempt_at <= now+60`
  （D-O 直接證據，不依賴吞吐）、(ii) 之後 1 小時內第一頁抓到；反向護欄：改回 `_spread` 並挑 `_spread(addr,24h) > 1h` 的地址 → 兩項轉紅。
  若暖機穩態下 1 小時仍抓不到第一頁 → 停下來報（`claim_due` 領工順序的真實吞吐問題，不准放寬）。
  builder 冷啟動實測：300 地址同時新入池時全系統 fills 類約 1 頁/分鐘（`explore_fills` cap 120 = 一頁權重，前一份 plan Task 6 已實測、
  使用者裁決接受）——寫進資料極限：harness 冷啟動不代表正式機。
  ✅ **驗收指標教訓**：原打算用 `scan_pages_for([cold]) >= 1` 當「第一頁真的抓到」的證據，實測踩坑——`apply_scan_page`
  （`explore_fills_sync.py:498-505`）對「短頁／終止頁」（`len(page) < PAGE_LIMIT`，游標直接跳到 `window_end_ms`）刻意
  **不**遞增 `pages_done`；冷門地址的整趟遍歷幾乎必然一頁內終止，`pages_done` 因此永遠停在 0，即使遍歷已 `complete`、成交
  已真的落地——首次跑出「11 分鐘內 `stored_fill_count=299`、`published_row` state=complete」卻被 `scan_pages_for` 判紅。
  改用 `stored_fill_count(cold) >= 1`（`fills` 表實際落地筆數）後：暖機穩態下 11 分鐘內解決（1 小時預算內）。
  ✅ **反向護欄教訓**：只改 `_enqueue_address_jobs` 的 D-O 本體不足以轉紅——`_run_candidates` 收尾必呼叫
  `reconcile_scan_jobs`，對剛 `bootstrap_address_fills` 建立 running scan 的新地址，`_ensure_scan_job` 判 `resume_running`，
  Task 1b（`explore_scheduler.py:755`，另一個獨立修法）「無論 job 是否存在都用 MIN 語義拉到 now」同一輪內把 D-O 被破壞的
  效果蓋掉——兩個修法對『新地址第一輪就把 job 排到 now』形成防禦縱深。同時暫時改回 Task 1b 之前的舊語意（`_needs_scan_job`
  的 `resume_running` 分支）後，兩項斷言才真的轉紅（`next_attempt_at=now+13600.0`）；revert 後 `git diff --stat src/` 清空。
  測試因此驗的是『新地址第一輪就有 job 排到 now』這個由 D-O 與 Task 1b **共同**保證的整體性質，非 D-O 排他證明。
- [x] 沿用既有 Task 7a／7b／8／8b／11 測試全綠（尤其 `test_evidence_unknown_rows_actually_leave_unknown_via_verify`
  與 `test_migrated_rows_reach_complete_via_standalone_probe_path` 一字不改）。✅ 全套 3391 passed（家族含這兩條皆綠、
  一字未改）、ruff 過；`git diff --stat` 只有 `tests/test_explore_scheduler.py`。
- [x] Commit — `97f84e3` `test: 全史探測解出疑似截斷、冷門地址即時開跑（D-K／D-O）`

---

## Task 5: 審核、RUNBOOK §5.8g、部署 `@inline` ＋ 主線程

- [ ] **審核**：派 `reviewer`（opus，fresh）看 `git diff <第八次部署 commit>..HEAD -- src/`，重點：(1) 探測窗改動有沒有引入
  錯判完整路徑；(2) 遷移是否只動 D-M／D-N 明定的列；(3) `fills_scan` 到期改 `now` 是否可能讓遍歷軌把增量軌餓死
  （限流器父子 scope 仍在，理論上不會，要看證據）。
- [ ] **RUNBOOK §5.8g**：沿 §5.8f 寫法；備份檔名 `explore.db.pre-v5.bak`；drop-in `explore-v4-verdict.conf` 的
  `_UNTIL` 順延到部署後 +24h；遷移報告核對項改為 v5 的三個數字；觀測門檻加「`truncation_suspected` 應在 6 小時內
  降到個位數」與「`scan_pages_15m` 不得連續 4 筆為 0 且同時有到期的 `fills_scan` job」。
- [ ] **部署**（使用者授權後，主線程親自逐步執行）：基線 → 本機用 pre-v5 備份跑新程式合成快照 → rsync → drop-in →
  stop → 裝快照 → start → 比對 follower → 核對遷移報告 → 24h 觀測（沿用每小時排程）。

## 預期效果（部署後）

| 時點 | 預期 |
|---|---|
| +1h | 遷移當下池內 `truncation_suspected` 已為 0（D-M 重設）；**`unknown` 從 ~156 開始下降**（每小時約 12–15 個，輔助份額 3:1），對應的 complete 同步上升 |
| +2–3h | 35 個 `backfilling` 全部開跑並多數完成 |
| +6–8h | 池內 complete 121 → ~250；剩下為證據真的不足（全史窗仍空且帳戶更老）或 `unresolved_gap` |
| +24h | 觀測結束；第二份 24h 日誌 |

## 資料極限與未決事項

1. 「HL 保留一年」是兩個地址的實測（最早 2025-02-26、2025-09-23），不是保證；D-K 的推理只依賴「留存是後綴截斷」
   這個性質，不依賴保留多久。
2. 全史窗回滿 2000 筆時，探測頁的結算權重最高 120（與一般 fills 頁相同），約 121 個探測 ≈ 3–4 小時的輔助份額。
3. W4（`truncation_suspected → unknown` 降級不重算）在本次 D-M 之後實務上不再觸發（疑似截斷大幅減少），仍列後續待議。
4. ops.py:157 讀舊 follower state 的 PermissionError（第八次部署觀測期發現）不在本 plan 範圍，另開。

## 狀態表

| Task | 狀態 | 驗收證據 |
|---|---|---|
| 1 fills_scan 到期改 now | ✅ `abd595b`；主線程複跑 3379 passed、ruff 過 | 三條測試；反向護欄第一條轉紅；第三條改標為再入池續跑迴歸測試（非 D-O 護欄） |
| 1b resume_running MIN 拉近 | ✅ `36fcbfb`；主線程複跑同上、五條新測試 5 passed | **範圍收斂（builder 裁決，主線程接受）**：只對 `fills_scan` 套用一律 enqueue；`fills_verify` 的 resume_running 維持「同類 job 已存在不重排」（7.9e-S1 不變量、既有測試 `test_s1_reconcile_resumes_running_verify_scan_with_verify_job`）。日誌只在真的新建時印（300 地址 → 3 行）。 |
| 2 探測窗全史 | ✅ `0815e10`；主線程複跑 3384 passed、ruff 過、目標測試 6 passed | 四條測試＋harness 護欄；反向護欄兩組轉紅；harness 時鐘改 1.7e9、預設候選首次活動 ws+2d；139 條家族測試零轉紅、零斷言放寬 |
| 3 schema v5 遷移 | ✅ `e669b31`；主線程複跑 3389 passed、ruff 過 | 主線程在乾淨複本獨立重現：report 完全一致（probes_needed 156、cursors_normalized 164、verdicts_recomputed 172、scan_jobs_advanced 25）；fills 1,557,151 前後相同；游標實際變動 131 個且全屬 D-N 條件；池內 truncation_suspected 122→0、complete 128→139；probe candidates 122；running scan 無殘留未來 job |
| 3c 審核修正 C1／W1 | ✅ `f4124ed`；主線程複跑 3394 passed、ruff 過、重現腳本 `survived? True` | 新增唯讀 `get_job`；C1 以 `job.last_error` 擋拉近，reviewer 重現腳本修正後 `survived? True`；W1 計數分 `_created`／`_pulled_forward`；反向護欄轉紅 | reviewer 判可部署但主線程改為部署前修 |
| 4 整合驗收 | ✅ `97f84e3`；builder 複跑 3391 passed（含新增 2 條）、ruff 過 | 兩條測試皆通過＋反向護欄轉紅＋revert 後 `git diff --stat src/` 清空；家族回歸（含兩條「一字不改」測試）全綠；發現並記錄兩個指標／機制教訓（見下） |
| 5 審核／RUNBOOK／部署 | RUNBOOK §5.8g ✅；reviewer（opus）✅ 無 Critical 殘留（C1 已由 Task 3c 封、W1 已修、W2/W3 記錄）；**✅ 已部署 2026-09-23 15:57:04 UTC**（commit `831e632`＝程式碼 `f4124ed`＋docs；API 停機約 4 秒） | 遷移報告：probes_needed 140（＝池內 trunc 127＋unknown 13）、cursors_normalized 164、verdicts_recomputed 176、scan_jobs_advanced 12；池內 truncation_suspected 127 → **0**、unknown 13 → 139；fills 1,810,517 → 1,811,710（未減）；抽查 3 個非目標游標一字不動；follower 時間戳部署前後相同；快照 v4／300 列預熱裝入；Traceback 0。詳見文末「部署紀錄」 |

## 部署紀錄（第九次，2026-09-23，RUNBOOK §5.8g）

使用者 05:35 UTC 裁決「等 15:45 UTC 第八次 24h 觀測滿再部署、到時不再問」；15:52 UTC 排程觸發。

| 步驟 | 時刻（UTC） | 結果 |
|---|---|---|
| 0 第八次 24h 結案 | 15:52 | 97 樣本 429=0、非已知 Traceback 0、follower 不變（總結寫入前一份 plan，`831e632`） |
| 1 基線 | 15:53 | failed 0；follower f438 03:21:18 09-22／fb8c 05:21:18 09-18；timers 4；`explore.db.pre-v5.bak` 1,065,226,240 B（sqlite backup API）；池內 lb：earlier_fills_seen 141／no_earlier_activity 19／**truncation_suspected 127／unknown 13**；fills **1,810,517**；`_UNTIL` 原值 2026-09-24T00:00:00Z；主機 available 1,079 MB |
| 2 預熱 | 15:54–15:56 | 備份 gzip → 本機 `var/prewarm-v5/`（integrity ok、fills／池內分佈與基線相同）→ 新程式起 8700（本機遷移報告：probes_needed 140、cursors_normalized 164、verdicts_recomputed 177、scan_jobs_advanced 12；Traceback 0）→ 快照 version 4／300 列 → scp `/tmp/explore_index_new.json` |
| 3 env | 15:55 | `_UNTIL` → **2026-09-24T15:55:53Z**（舊檔留 `/tmp/explore-v4-verdict.conf.pre-v5`）；daemon-reload；12 個 drop-in env 全部在 `config.py`／`leader_resolve.py` 有讀取者 |
| 4 rsync／重啟 | 15:56:53–15:57:07 | 機密邊界 0 命中；src 只差 `explore_scheduler.py`／`explore_store.py`（與 `git diff bbd7adf..HEAD -- src` 一致）；uv.lock mtime 2026-07-17 未動、pyproject 未變故跳過 uv sync；chown root 排除 var/（非 root 4＝.venv symlink 例外，`builder_accrued_snapshot.json` 仍 filet-engine）；**stop 15:57:03 → 裝快照（舊檔 `.bak-20260923-pre-v5`）→ start 15:57:04 → API 3 秒就緒**；follower 兩個 unit 時間戳與基線完全相同、timers 4、failed 0；進程 environ `_UNTIL` 新值生效；主機 available 1,377 MB |
| 5 遷移核對 | 15:57:34 | journal 報告：before trunc 169／unknown 80 → after trunc 42（全在池外）／unknown 207；work probes_needed **140**、cursors_normalized 164、verdicts_recomputed 176、scan_jobs_advanced 12；池內 lb：earlier_fills_seen 142／no_earlier_activity 19／**unknown 139、truncation_suspected 0**；池內 complete 148 → 157；fills 1,811,710（≥ 基線）；抽查 3 個 `pages_done>0` 游標一字不動；running scans 166、其 fills_scan job 無一在未來 >60s、到期 126；隔離 0；`fills_verify` 24；獨立探測候選 25；Traceback 0 |
| DEPLOYED_VERSION | 15:57:58 | `commit=831e632…`、`describe=mainnet-launch-20260725-472-g831e632` |

- 24h 觀測：session 排程 13558302 每小時 :33，門檻依 §5.8g Step 6（池內 trunc 6h 內個位數、池內 unknown 自 139 兩小時 −20、
  遍歷軌不停擺、隔離不被拉近）；結束於 2026-09-24 15:57 UTC 之後那次。
- 注意：`systemctl show -p Environment` 在 daemon-reload 後即顯示新值，進程實際值以 `/proc/<MainPID>/environ` 為準（本次已驗）。
- 預期：126 個到期 `fills_scan` job 以 ≤1 頁/分推進，每頁前先做全史探測（`_run_scan` 每次推進都查左界），加上獨立探測 3:1 份額，
  池內 unknown 應每小時降 15 個以上。

## 部署後觀測日誌（第九次，每小時 :33）

| 時刻 | 429 | TB | 對外 complete／partial／backfilling | 池內 unknown／trunc | verify backlog | due fills_scan／scan_pages_15m | follower | 備註 |
|---|---|---|---|---|---|---|---|---|
| 15:57（部署） | 0 | 0 | 快照裝入（v4／300） | 139／0 | 24 | 126／— | ok | 基線 |
