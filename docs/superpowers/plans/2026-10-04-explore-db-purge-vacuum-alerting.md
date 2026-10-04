# explore.db 清理接線、VACUUM、主機告警 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 2026-10-07 使用者出國 demo 前，讓正式機脫離 IO 飽和：把已寫好但從未被呼叫的 `ExploreStore.purge()` 接上排程（有界、每輪一小口）、做一次 VACUUM 讓檔案真的縮回 ~1.1 GB、補 DB／IO 監控，並建立 TG 主動告警。

**Architecture:** (1) `purge()` 加兩個上界參數（每輪最多 N 個退池位址、最多 M 筆過期 fills），scheduler 在 candidates 輪（每 30 分）末尾呼叫一次，結果進 `status()`／journal，失敗不得拖垮 candidates job；env `FILET_EXPLORE_PURGE`（預設開）當 kill switch。(2) VACUUM 是一次性運維（停 api 幾分鐘），寫進 RUNBOOK §5.8k。(3) 主機取樣器 v2 加 DB／WAL 大小、PSI io、nginx 499 計數。(4) 告警腳本純函式判斷＋TG 送出，token 從 `filet-api` unit env 讀、不落地。

**Tech Stack:** Python 3.11 + uv、SQLite（正式機 3.37.2、無 sqlite3 CLI → 用 python）、pytest 全離線、systemd、ubuntu crontab。

---

## 背景（2026-10-04 唯讀查證）

- `explore.db` 2,259 MB＋WAL 101 MB，09-23 部署時 1,016 MB；fills rowid 405 萬（+12 萬/天）；freelist 0、auto_vacuum 0。
- `candidate`：300 active／606 inactive，其中 **344 個退池 >7 天且無殘留 job**＝`purge()` 第一輪對象。09-30 量過：退池位址持有約 51% 的 fills。
- `ExploreStore.purge()`（`explore_store.py:2116`）有 8 條測試，**正式程式碼零呼叫者**（`git grep "\.purge("` 只命中 tests）。
- IO：`/proc/pressure/io` some avg300 76–79%、vmstat wa 45–72%、swap 持續換出；`filet-api` 3.8 天累計讀 2.74 TB（發布器每分鐘對 300 列各做 `get_fills()` 讀整窗 `raw`，DB 大於 page cache 後全部打磁碟）。
- 用戶可見：nginx 499/日 4→15→12→25→79→26→16；公開站 `/` 實測一次 11.2 s。
- 主機 1.9 GB RAM、available ~1.0 GB；三個 follower 在跑（f438／fb8c／f6b3）；`/var/run/reboot-required` 自 10/1 存在（**不重開機**）。

## ⚠️ 2026-10-04 11:26 UTC 事故與緊急處置（主線程，plan 寫完後 30 分鐘內發生）

- 症狀：nginx 連續 502／504（`upstream timed out … reading response header`）打在 `/`、`/dashboard`、`/api/me`、
  `/api/auth/nonce`、`/api/public/status`、`/api/public/strategies`；15 分鐘內 499＝15、5xx＝19（205 請求）。
  使用者本人（HiNet IPv6）登入失敗。`/proc/pressure/io` **full avg300 82%**（所有任務同時卡 IO 的時間佔八成）。
- 機制：發布器每分鐘對 300 列各做 `get_fills()`（持 store lock、讀整窗 raw），DB 2.26 GB > page cache → 每次讀都打磁碟；
  API handler 要拿同一把 lock／同一顆磁碟 → 超過 nginx 60 秒逾時。
- 處置（11:33:47 UTC，§5.8f Step 7-pre）：drop-in `explore-refresh.conf` 的 `EXPLORE_UPSTREAM_REFRESH` 1 → 0、daemon-reload、
  **只重啟 `filet-api`**（11 秒就緒）；三個 follower `ActiveEnterTimestamp` 不變（10-03 00:50:50／53／56）；失敗 unit 0。
  重啟後公開站 `/` 0.17 s、`/api/public/status` 0.06 s。原 drop-in 備份 `/tmp/explore-refresh.conf.bak-202610041133`。
- 代價：探索榜停在最後一份快照（complete 292／partial 8，內容仍正確，只是不再更新），直到下面的維護窗完成。
- **因此 Task 2 的順序改為**：維護窗內 **停 api → 用已部署程式碼跑一次「無上界」purge（離線、沒人跟它搶 lock）→ VACUUM →
  部署 Task 1 → `EXPLORE_UPSTREAM_REFRESH=1` → start api**。原本「上線後有界 purge 慢慢刪 7 小時」的做法會讓 IO 飽和再持續
  數小時並重演 504。Task 1 的有界 purge 改為**穩態維護**（之後每輪只會有幾個退池位址）。

## 裁決（主線程，2026-10-04；使用者授權「1–5 幫我搞定」）

| 代號 | 裁決 | 理由 |
|---|---|---|
| D-P1 | purge **有界**：每輪最多 25 個退池位址、最多 50,000 筆過期 fills | 一次刪 344 個位址（~50 萬筆＋索引）在 IO 飽和的機器上會抱著 store lock 數十秒到數分鐘，API／發布器全部停等；分 14 輪（7 小時）攤平 |
| D-P2 | 呼叫點＝`_run_candidates` 末尾、對帳之後；try/except 不得讓 candidates job 進隔離；計數進 `status()["purged"]`、`purge_errors`；有刪到東西才 `logger.warning`（root logger 停在 WARNING，info 不進 journald） | 與既有對帳（7.9e-S S3）同形 |
| D-P3 | kill switch `FILET_EXPLORE_PURGE`（`"0"`／`"false"` 關，其餘開）；關閉時 `status()["purge_enabled"]=False` | demo 期間若懷疑它，改 drop-in＋restart 即可停，不必回退程式 |
| D-P4 | purge 末尾 `PRAGMA incremental_vacuum(4000)`（只在 `auto_vacuum=2` 時有效，否則 no-op）；VACUUM 當晚一併把 `auto_vacuum` 設為 INCREMENTAL | 之後的刪除能逐步回收頁，不必再手動 VACUUM |
| D-P5 | 部署前先做一次 sqlite backup `explore.db.pre-purge.bak`，並先刪四個舊備份（pre-75/78/79/v4，1.65 GB） | 清理是不可逆的；但 pre-v5（09-23）已過時，需要新基線 |
| D-P6 | 告警 TG token 來源＝`systemctl show filet-api -p Environment` 在程式內解析（root cron），不寫檔、不印；chat id 同 | 不新增機密存放點；RUNBOOK 已記這兩個 key 在 unit env |
| D-P7 | 告警去重：同一條件首次立刻發、持續中每 6 小時提醒一次、恢復時發一則 recovered；狀態檔 `/home/ubuntu/explore-obs/alert_state.json` | 避免每小時洗版 |

---

## File Structure

| 檔案 | 責任 |
|---|---|
| `src/spark/publicapi/explore_store.py` | Task 1：`purge()` 加 `max_candidates`／`max_fills` 上界與 `incremental_vacuum` |
| `src/spark/publicapi/explore_scheduler.py` | Task 1：candidates 輪呼叫 purge、計數、status |
| `src/spark/publicapi/config.py` | Task 1：`explore_purge_enabled` 欄位（env `FILET_EXPLORE_PURGE`） |
| `scripts/run_api.py` | Task 1：若 scheduler 透過 cfg 讀旗標則不必改；確認 cfg 已傳入 |
| `tests/test_explore_store.py`、`tests/test_explore_scheduler.py` | Task 1 測試 |
| `deploy/ops/host_sample.py` | Task 4：主機取樣器 v2（repo 存檔；正式機 `/home/ubuntu/explore-obs/host_sample.py`） |
| `deploy/ops/filet_alert.py`、`tests/test_filet_alert.py` | Task 5：告警判斷＋TG 送出 |
| `deploy/RUNBOOK.md` | Task 3：§5.8k 部署／VACUUM／備份清理／回退／demo 自救卡 |

**不得改動**：`scan_verdict`、`_applicable_boundary`、`hl_budget.py`、`explore_publisher.py`（讀取量的根治是另一題）、`web/`、`src/spark/copytrade/`、`src/spark/filet/`。

---

## Task 1: purge 有界化＋接上 candidates 輪 `@inline`

**Files:** `src/spark/publicapi/explore_store.py:2116-2166`、`src/spark/publicapi/explore_scheduler.py:1027-1100`（`_run_candidates`）與 `:555-620`（`status()`）、`src/spark/publicapi/config.py`（`explore_upstream_refresh` 旁）、測試兩檔。

- [ ] **Step 1: 失敗測試（store）** — `tests/test_explore_store.py`：
  - `test_purge_max_candidates_bounds_stale_deletion`：建 5 個退池 >7d 無 job 的候選，各有 fills／fills_sync；`purge(now, max_candidates=2)` → `counts["candidate"] == 2`，剩 3 個候選列仍在；再呼叫兩次後全清。刪除順序＝`last_seen_at` 最舊優先（SQL `ORDER BY c.last_seen_at ASC LIMIT ?`）。
  - `test_purge_max_fills_bounds_retention_deletion`：一個 active 候選，塞 10 筆 `time_ms` 早於 35 天且早於 `window_start_ms` 的 fills；`purge(now, max_fills=4)` → `counts["fills"] == 4`，再呼叫 → 4、2、0。實作用 `DELETE FROM fills WHERE rowid IN (SELECT rowid FROM fills WHERE … LIMIT ?)`（SQLite 預設不支援 `DELETE … LIMIT`）。
  - `test_purge_default_unbounded_keeps_existing_behaviour`：既有 8 條 purge 測試一字不改照過（上界預設 `None`）。
  - `test_purge_runs_incremental_vacuum_without_error_when_auto_vacuum_off`：`auto_vacuum=0` 的 DB 呼叫 purge 不丟例外（PRAGMA 是 no-op）。
- [ ] **Step 2: 失敗測試（scheduler）** — `tests/test_explore_scheduler.py`，用 `SchedulerHarness`：
  - `test_candidates_round_purges_stale_candidates_and_reports`：某地址進池→退池（候選來源不再回它）→ harness 時鐘推進 8 天 → 再跑一輪 candidates → 該地址 `fills_sync`／`fills` 列消失；`scheduler.status()["purged"]["candidate"] >= 1`、`["purge_enabled"] is True`。
  - `test_candidates_round_skips_purge_when_disabled`：`cfg.explore_purge_enabled=False` → 同情境列仍在、`status()["purged"] is None`、`purge_enabled False`。
  - `test_purge_failure_does_not_quarantine_candidates_job`：monkeypatch `store.purge` 丟 `RuntimeError` → candidates job 照常 `_complete` 並重排（`refresh_job` 裡 candidates 的 `last_error` 為 None、`attempts` 未增）、`status()["purge_errors"] == 1`。
  - `test_candidates_round_passes_bounds_to_purge`：monkeypatch 記錄呼叫參數 → `max_candidates == 25`、`max_fills == 50_000`（常數 `PURGE_MAX_CANDIDATES`／`PURGE_MAX_FILLS` 定義在 scheduler 模組頂層，附 D-P1 理由註解）。
- [ ] **Step 3: 跑測試確認紅** — `uv run pytest tests/test_explore_store.py tests/test_explore_scheduler.py -k "purge" -v`。
- [ ] **Step 4: 實作**
  - `config.py`：`explore_purge_enabled: bool = True`；`from_env` 讀 `FILET_EXPLORE_PURGE`（`"0"`／`"false"` 不分大小寫 → False，其餘含未設 → True）；寫在 `explore_upstream_refresh` 附近並加註解（D-P3）。
  - `explore_store.purge(now, *, candidate_keep_s, fills_keep_s, scan_keep_s, max_candidates: int | None = None, max_fills: int | None = None)`：stale 查詢加 `ORDER BY c.last_seen_at ASC` 與可選 `LIMIT`；retention DELETE 改 `rowid IN (SELECT … LIMIT ?)` 形式（`max_fills is None` 時不加 LIMIT）；末尾 `self._db.execute("PRAGMA incremental_vacuum(4000)")`；docstring 補 D-P1／D-P4。
  - `explore_scheduler.py`：模組常數 `PURGE_MAX_CANDIDATES = 25`、`PURGE_MAX_FILLS = 50_000`；`__init__` 加 `self._purged: dict[str, int] | None = None`、`self._purge_errors = 0`；`_run_candidates` 在對帳 try/except **之後、`_complete` 之前**：
    ```python
    if self._cfg.explore_purge_enabled:
        try:
            self._purged = self._store.purge(
                now, max_candidates=PURGE_MAX_CANDIDATES, max_fills=PURGE_MAX_FILLS)
            if any(self._purged.values()):
                logger.warning("explore scheduler: purge 本輪刪除 %s", self._purged)
        except Exception:
            self._purged = None
            self._purge_errors += 1
            logger.error("explore scheduler: purge 失敗（candidates job 照常收尾）", exc_info=True)
    ```
    `status()` 加 `"purged"`、`"purge_errors"`、`"purge_enabled": self._cfg.explore_purge_enabled`。
  - `scripts/run_api.py`：確認 `ExploreScheduler(cfg=cfg…)` 已傳 cfg（是），不必改。
- [ ] **Step 5: 全套綠、ruff** — `uv run pytest -q`、`uv run ruff check src tests scripts`。
- [ ] **Step 6: Commit** — `feat: explore purge 接上 candidates 輪（有界 25 位址／5 萬筆每輪、FILET_EXPLORE_PURGE kill switch、status 計數）`。

**驗收（主線程親跑）**：`uv run pytest -q` 全綠數字；`git grep -n "\.purge(" src` 恰一處在 `_run_candidates`；`grep -n PURGE_MAX src/spark/publicapi/explore_scheduler.py`。

---

## Task 2: 正式機部署＋VACUUM（主線程親做，依 Task 3 的 §5.8k）

（順序已依 11:26 事故改寫，見上方「事故與緊急處置」；程序細節在 RUNBOOK §5.8k。）

- [ ] **維護窗**（一次做完，停 api 預估 10–20 分鐘；建議 2026-10-04 15:00 UTC 之後＝台北 23:00 後，使用者在線時）：
  1. 基線：follower 三個時間戳、DB／WAL 大小、PSI io、`host.jsonl` 最後一筆。
  2. 刪四個舊備份（pre-75/78/79/v4）；`systemctl stop filet-api`；sqlite backup API 建 `explore.db.pre-purge.bak`。
  3. 離線 purge：以 `filet-api` 身分用**已部署**的 venv 執行 `deploy/ops/explore_offline_maintenance.py`（`ExploreStore(path).purge(now)` 無上界＋印計數）；預期 candidate 344、fills 數十萬～百萬級。
  4. VACUUM：同腳本 `--vacuum`（`wal_checkpoint(TRUNCATE)` → `auto_vacuum=INCREMENTAL` → `VACUUM` → `integrity_check` → 印前後 page_count）。
  5. rsync Task 1 程式碼（§3.2 兩段）、`EXPLORE_UPSTREAM_REFRESH=1`、daemon-reload、`start filet-api`、follower 比對、公開站延遲。
  6. 30 分內 journal 應出現第一輪有界 purge 的 warning（可能全 0＝沒東西可刪，正常）；`status()["purged"]` 經 health 可見。
- [ ] 驗收：explore.db 2,259 MB → ~1.0–1.3 GB；`/proc/pressure/io` some avg300 一小時內 <30%；nginx 499/15m 回到個位數；三個 follower 未動。

---

## Task 3: RUNBOOK §5.8k（主線程親寫）

內容：(a) 基線（含三個 follower 時間戳、DB／WAL 大小、PSI io、499 當日計數）；(b) 刪四個舊備份＋sqlite backup `pre-purge.bak`；(c) rsync→restart api→follower 比對；(d) 第一輪 purge 核對；(e) VACUUM 程序：`systemctl stop filet-api` → python `PRAGMA wal_checkpoint(TRUNCATE); PRAGMA auto_vacuum=INCREMENTAL; VACUUM; PRAGMA integrity_check`（以 filet-api 身分、`sudo -u filet-api`）→ 記錄前後 page_count → `start` → follower 比對；(f) 回退：`FILET_EXPLORE_PURGE=0` drop-in＋restart（資料不可逆，備份在 pre-purge.bak）；(g) **demo 自救卡**（開演前 30 分檢查一條指令；慢→`restart filet-api`；再慢→`EXPLORE_UPSTREAM_REFRESH=0`）；(h) 凍結提醒（10/6 起不部署、不重開機）。

---

## Task 4: 主機取樣器 v2（主線程親做；repo 存 `deploy/ops/host_sample.py`）

新增欄位：`explore_db_mb`、`explore_wal_mb`（`os.path.getsize`）、`psi_io`（some avg60/avg300）、`nginx_499_15m`／`nginx_5xx_15m`（讀 `/var/log/nginx/access.log` 最後 5,000 行、解析 `[dd/Mon/yyyy:HH:MM:SS` 落在 15 分內者）、`reboot_required`（`/var/run/reboot-required` 存在）。access.log 若 ubuntu 讀不到 → crontab 改 `sudo /usr/bin/python3`（與 sample.py 同形）。驗收：手動跑兩次、`tail -1 host.jsonl` 含新欄位、`host_cron.err` 0。

---

## Task 5: TG 告警 `deploy/ops/filet_alert.py` `@inline`

**Files:** `deploy/ops/filet_alert.py`（新）、`tests/test_filet_alert.py`（新）。純標準庫（urllib、json、subprocess），不依賴 spark 套件（正式機用系統 python3 跑）。

- [ ] **Step 1: 失敗測試** — `evaluate(sample: dict | None, sample_age_s: float, units: dict[str, str], failed_units: int, state: dict, now: float) -> tuple[list[str], dict]`：
  - 條件（任一成立即一條告警，訊息含數值）：`sample is None or sample_age_s > 1200` → `sampler_stale`；`mem_mb.available < 400` → `low_memory`；`psi_io.some_avg300 > 50` → `io_pressure`；`nginx_499_15m > 10` → `client_timeouts`；`explore_db_mb > 2500` → `db_size`；任一 `units[name] != "active"` → `unit_down:<name>`；`failed_units > 0` → `failed_units`。
  - 去重（D-P7）：`state[key] = {"since": t, "last_sent": t}`；首次 → 發；持續且 `now - last_sent >= 6h` → 發（訊息前綴「仍在」）；條件消失且 state 內有 → 發「recovered」並移除。
  - 測試至少 8 條：每個條件各一、6h 提醒、恢復、無告警時 state 不變、sample None。
- [ ] **Step 2: 實作** — `main()`：讀 `/home/ubuntu/explore-obs/host.jsonl` 最後一行與 mtime；`systemctl is-active` 固定 unit（filet-api、filet-dashboard、filet-keysvc、nginx）＋動態 `filet-follower@*`；`systemctl --failed --no-legend | wc -l`；`evaluate`；有訊息就 `send_telegram(text)`：token／chat id 從 `systemctl show filet-api -p Environment --value` 解析 `FILET_API_TG_BOT_TOKEN`／`FILET_API_TG_CHAT_ID`（只存在局部變數；任何例外訊息不得含 token；`--dry-run` 印訊息不送）；寫回 state 檔。送出失敗 → stderr 一行、exit 1（cron 會進 `alert_cron.err`）。
- [ ] **Step 3: 測試綠** — `uv run pytest tests/test_filet_alert.py -v`（offline；`send_telegram` 不在測試中呼叫）。
- [ ] **Step 4: Commit** — `feat: deploy/ops/filet_alert.py——主機／服務門檻 TG 告警（去重 6h、恢復通知、dry-run）`。

**安裝（主線程）**：scp 到 `/home/ubuntu/explore-obs/filet_alert.py`；root crontab（`sudo crontab -l`）加 `5,20,35,50 * * * * /usr/bin/python3 /home/ubuntu/explore-obs/filet_alert.py >/dev/null 2>>/home/ubuntu/explore-obs/alert_cron.err`；先 `--dry-run` 看輸出，再送一則測試訊息（`--test`：送「filet 告警通道測試」）請使用者確認 TG 收到。

---

## 時程（UTC／台北）

| 時間 | 事 |
|---|---|
| 10/4 12:00–16:00 UTC | Task 1、5 實作＋審核；Task 4 上機；Task 3 RUNBOOK |
| 10/4 晚（台北 10/5 00:00 前後） | 部署 Task 1（api 重啟 4 秒）；刪舊備份、建 pre-purge.bak；告警 cron 上線＋測試訊息 |
| 10/5 全天 | purge 逐輪清（14 輪）；觀測 PSI io／499 |
| 10/5 15:00 UTC（台北 23:00） | VACUUM（停 api ≤10 分） |
| 10/6 | 看曲線；使用者決定是否升 4 GB（項目 6）；凍結開始（項目 7） |

## 狀態表

| Task | 狀態 | 證據 |
|---|---|---|
| 1 purge 接線 | ✅ `7383773`（builder）；主線程親跑 purge/alert 目標測試 28 passed、ruff 過、`git grep "\.purge(" src` 恰一處 `:1116`；全套跑中；reviewer 派出 | **builder 偏離（主線程接受）**：scheduler 的 `cfg` 是 `hl_explore.ExploreConfig` 不是 `ApiConfig`，kill switch 改走 ctor kwarg `purge_enabled`（與 `fills_min_period_s` 同形），`scripts/run_api.py` 多傳一行 `purge_enabled=cfg.explore_purge_enabled`；scheduler 測試用 `_sched`+`Clock` 而非 harness（退池 8 天在 harness 難表達）；`from_env` 的 `FILET_EXPLORE_PURGE` 解析無單元測試（上機用 `/proc/<pid>/environ`＋health 的 `purge_enabled` 實證） |
| 1b 審核 | ✅ reviewer（opus）兩輪：W1 incremental_vacuum 只回收 1 頁（`.fetchall()`＋變異測試轉紅）、W2 磁碟門檻、W3 告警誤報 recovered、W4 purge 耗時進 status、兩條 Suggestion（去 `--all`、kill switch 解析測試）→ `3ef6df8`；複審 PASS；再依建議拆 `--allow-active`／`--allow-low-disk` → `5acd220` | 主線程親跑全套 **3532 passed**（3ef6df8）、ruff 過；已 push |
| 2 部署＋VACUUM | **就緒，待使用者定維護窗**；乾淨 worktree `5acd220` 已 stage-1 rsync 到正式機 `/tmp/spark-sync/`（機密邊界 0 命中，src 只差 config／scheduler／store／run_api 四檔） | 離線腳本在本機合成 DB 驗過：purge 計數正確、VACUUM 814 → 20 頁、integrity ok、auto_vacuum=2；無旗標時拒絕（exit 2） |
| 3 RUNBOOK §5.8k | ✅ `a0be03d`、`d170e43`（乾淨 worktree 推碼） | |
| 4 取樣器 v2 | ✅ 正式機 11:35 UTC 裝入（v1 備份 `host_sample.v1.bak.py`）；repo `deploy/ops/host_sample.py` | 11:47 起每筆含 `psi_io`／`nginx_499_15m`／`explore_db_mb` |
| 5 告警 | ✅ `ff400b2`（builder，12 tests）；主線程親跑 12 passed、ruff 過、token grep 5 處皆非輸出路徑、本機 dry-run exit 0；正式機 `--dry-run` 零告警、`--test` 已送一則（11:41 UTC）**待使用者確認收到**後裝 root cron | |
