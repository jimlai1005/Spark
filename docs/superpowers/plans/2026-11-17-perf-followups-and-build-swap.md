# 績效頁後續修正＋前端「另目錄 build 再換入」部署流程

狀態：**實作與審核完成（本機已 commit）；未部署、未 push**（2026-11-17）。
Task A／A2／F／F2 與首頁改動兩輪審核 PASS；部署腳本七輪審核，第七輪 PASS（§13），Task L 為採納的建議並由主線程驗收。
下一步需使用者同意：§14 正式機演練 → push → §5 部署。

前置：`docs/superpowers/plans/2026-11-16-strategy-return-hl-roi.md`（commit `a6d4ef8`，兩輪審核 PASS，使用者 2026-11-17 確認 D4–D7）。

## 0. 裁決

| # | 裁決 | 來源 |
|---|---|---|
| E1 | 淨值曲線的期間切換（30D／7D）、X 軸日期、對照線對齊，改用**每個取樣點的真實時間戳**，不再假設「一點＝一天」 | 使用者 2026-11-17「要處理」 |
| E2 | 「N=」顯示的數字改為**區間報酬樣本數**（取樣點數 − 1），與勝率／Sharpe 標準誤公式裡的 N 一致 | 使用者 2026-11-17「要處理」 |
| E3 | 部署後 `/api/leaders` 目錄頁最多一天舊公式：部署步驟加一步，重啟後立即手動觸發一次每日快照（`filet-leaderboard.service`） | 使用者 2026-11-17「要處理」；做法為主線程裁決 |
| E4 | 前端部署改為「在另一個目錄 build 成功後再換入」，build 失敗不影響線上 | 使用者 2026-11-17「請派工」 |
| E5 | 首頁「可跟單策略」區只列 `listable === true` 的策略；暫停開放的卡仍在 `/strategies` | 使用者 2026-11-17；主線程已直接完成（`web/src/app/page.tsx`＋2 個測試，變異驗證通過） |
| E6 | 期間切片內取樣點少於 2 個時，該期間按鈕停用並給提示文案，不畫單點曲線 | 主線程裁決 |
| E7 | build 目錄用「原始碼副本＋`node_modules` 符號連結」，不用 Next 的 `distDir` 設定（`distDir` 會讓 Next 自動改寫 `tsconfig.json`） | 主線程裁決 |
| E8 | `BUILD_DIR` 預設放在部署樹外 `/opt/filet/web-build`：放樹內會破壞 RUNBOOK §3.2「`/opt/filet/spark`（`var/` 除外）無非 root 檔案」的不變量，且會被第二段 rsync 的 `--delete` 清掉 | 主線程裁決（2026-11-17，讀完腳本後） |

## 1. Task A @inline 曲線時間戳與 N（後端＋前端）

範圍：`src/spark/filet/leader_perf.py`、`src/spark/filet/strategies.py`、`src/spark/publicapi/app.py`（策略詳情與交易員詳情兩個端點）、
`web/src/components/EquityCurve.tsx`、`web/src/components/MethodologyCard.tsx`、`web/src/app/strategies/[slug]/page.tsx`、
`web/src/app/traders/[address]/page.tsx`、`web/src/lib/publicApi.ts`、`web/src/lib/copy.ts`，以及對應測試。
不動 `web/src/app/page.tsx`、`deploy/`、`src/spark/copytrade/`、`src/spark/exchange/`、`src/spark/publicapi/hl_explore.py`。
若發現必須改 `ExploreRow`／`WindowStats` 欄位或 `EXPLORE_INDEX_VERSION` 才能完成，**停下回報**，不要自行改。

### A1 後端
- `compute_window_performance`（`status == "ok"`）新增兩鍵：
  - `sample_ts_ms`：tuple[int]，每個取樣點的毫秒時間戳，長度＝`len(roi_index)`＝取樣點數。
  - `return_sample_count`：int＝`len(pnl) − 1`（＝比率指標的樣本數 N）。
- `jsonable_performance`：`sample_ts_ms` 比照 `roi_index`／`equity_index` 處理（不整串落檔）。
- `strategies.build_metrics`：輸出 `return_sample_count`（perf 非 ok → 0）。既有 `sample_count`（取樣點數）不變。
- `strategies` 新增 `build_equity_index_ts(perf) -> list[int] | None`：回 `sample_ts_ms`；perf 非 ok 或缺鍵 → `None`。
- `/api/public/strategies/{slug}` 與 `/api/public/traders/{address}`：凡輸出 `equity_index` 之處，同層新增 `equity_index_ts`
  （與 `equity_index` 等長；`equity_index` 為 null 時亦為 null）。兩者必須出自**同一個 perf**。
- **錨例 B1**：plan 2026-11-16 的 A2 序列，時間戳取 `[0, 864000000, 1728000000, 2592000000]`（每 10 天一點）→
  `sample_ts_ms == (0, 864000000, 1728000000, 2592000000)`、`return_sample_count == 3`、`sample_count == 4`。

### A2 前端
- `publicApi.ts`：策略詳情與交易員詳情型別新增可選 `equity_index_ts: number[] | null`；`metrics` 新增可選 `return_sample_count: number`。
- `EquityCurve` 新增可選 prop `timestamps?: number[] | null`。當 `timestamps` 存在且長度等於序列長度：
  - 期間切片依時間：`ALL`＝全部；`30D`＝`ts >= lastTs − 30×86400000` 的點；`7D` 同理。
  - X 軸標籤用各點真實日期（UTC）。
  - 對照線（BTC／ETH／S&P 500）以各點真實日期對齊收盤價。
  - 切片內點數 < 2 → 該期間按鈕 `disabled`，`title`／`aria-label` 帶提示文案（E6）；目前選中的期間若變成不可用，退回 `ALL`。
  - `timestamps` 缺席或長度不符 → 維持現行行為（相容舊回應），不得拋例外。
- 兩個詳情頁把 `equity_index_ts` 傳給 `EquityCurve`。
- N（E2）：區間勝率註記與方法論卡 Sharpe 句的數字改用 `metrics.return_sample_count`；
  缺席時退回 `Math.max(metrics.sample_count − 1, 0)`。「涵蓋 N 個取樣點」那句仍用 `methodology.sample_count`，不變。
- 文案（ZH／EN 對稱）：

| key | ZH | EN |
|---|---|---|
| `winRateNoteSuffix` | （空格）個區間報酬樣本 | （空格）period return samples |
| `sampleSuffix` | （空格）個區間報酬樣本）。 | （空格）period return samples). |
| `strategyDetail.equity.periodUnavailable`（新增） | 此期間取樣點不足 | Not enough samples in this period |
| `daysSuffix` | 不變（個取樣點（） | 不變 |

- **錨例 B2**（前端單元測試）：10 個點、時間戳每 10 天一點（末點為 `T`）。`30D` 切片＝末 4 點（`T−30d` 含端點）；`7D` 切片＝1 點 → `7D` 按鈕 disabled；
  X 軸首末標籤等於切片首末點的 UTC 日期。同一組資料不給 `timestamps` 時，`30D` 維持舊行為（不拋例外）。

### 驗收
- `uv run pytest -q -p no:cacheprovider tests/test_leader_perf.py tests/test_leader_perf_ratios.py tests/test_leader_perf_roi.py tests/test_public_strategies.py tests/test_public_traders.py tests/test_trader_stats.py tests/test_strategies_metric_spec.py tests/test_filet_leaderboard.py tests/test_public_explore.py tests/test_explore_snapshot_v6.py` 全綠。
- `uv run ruff check src tests scripts` 無錯。
- `cd web && npm test` 全綠。
- 錨例 B1、B2 各有測試直接斷言。
- 變異：把 `EquityCurve` 的時間切片改回「取最後 N 點」→ B2 測試轉紅（貼紅燈輸出後還原）。
- `git diff --stat -- src/spark/publicapi/hl_explore.py` 無輸出。

## 2. Task B @inline 前端「另目錄 build 再換入」腳本

範圍：新增 `deploy/ops/web_build_swap.sh`、新增 `tests/test_web_build_swap.py`、更新 `deploy/RUNBOOK.md` §4.2（及引用它的部署段落需要的最小修改）。
不動 `web/`（含 `next.config.ts`、`package.json`）、`src/`、其他 `deploy/` 檔。

### 行為規格
在正式機以 `ubuntu` 執行（需要 sudo 的步驟由腳本內 `$SUDO` 前綴執行）。所有路徑與外部指令可由環境變數覆寫，供離線測試：

| 變數 | 預設 | 用途 |
|---|---|---|
| `WEB_DIR` | `/opt/filet/spark/web` | 線上目錄（`next start` 的工作目錄） |
| `BUILD_DIR` | `/opt/filet/web-build`（E8；原訂 `/opt/filet/spark/web-build`） | build 用副本 |
| `NEXT_PUBLIC_SITE_ORIGIN` | **無預設，未設即失敗** | 烤進 bundle 的對外網域 |
| `SUDO` | `sudo` | 測試時設為空字串 |
| `NPM_BIN` | `npm` | 測試時換成假指令 |
| `RESTART_CMD` | `sudo systemctl restart filet-dashboard` | 重啟線上服務 |
| `HEALTH_URL` | `http://127.0.0.1:3000/` | 健康檢查 |
| `HEALTH_TIMEOUT_S` | `30` | 健康檢查總等待秒數 |
| `MAINTENANCE_FLAG` | `/var/www/maintenance/ON` | 存在即拒絕執行（除非 `--force`） |
| `API_UNIT` | `filet-api` | 見步驟 1 |

步驟（任一步失敗即非零退出並印出原因；**步驟 5 之前的任何失敗都不得改動 `WEB_DIR/.next`**）：

1. 前置檢查：`NEXT_PUBLIC_SITE_ORIGIN` 已設；`WEB_DIR/.next/BUILD_ID` 存在；維護旗標不存在；以 `flock` 取得單一執行鎖；
   若 `API_UNIT` 目前非 active 則拒絕（`filet-dashboard` 的 `Wants=filet-api` 會在重啟時把被刻意停掉的 api 拉起，2026-10-04 事故），除非 `--allow-api-start`。
2. 準備 `BUILD_DIR`（owner 為執行者）：把 `WEB_DIR` 的原始碼同步過去（排除 `node_modules`、`.next`），
   `BUILD_DIR/node_modules` 為指向 `WEB_DIR/node_modules` 的符號連結；把 `WEB_DIR/.next/cache` 複製到 `BUILD_DIR/.next/cache`（存在才複製，用來保留增量編譯快取）。
3. 在 `BUILD_DIR` 執行 `$NPM_BIN run build`，輸出寫到 log 檔；失敗 → 印 log 末 30 行、非零退出。
4. 驗證產物：`BUILD_DIR/.next/BUILD_ID` 存在且非空、`BUILD_DIR/.next/static` 非空。
5. 換入：刪除舊的 `WEB_DIR/.next-prev`；`mv WEB_DIR/.next WEB_DIR/.next-prev`；`mv BUILD_DIR/.next WEB_DIR/.next`（同一檔案系統的 rename）；
   `WEB_DIR/.next` 遞迴設為 `root:root`、`go-w`（`$SUDO` 為空時跳過 chown）。
6. 執行 `RESTART_CMD`。
7. 健康檢查：在 `HEALTH_TIMEOUT_S` 內輪詢 `HEALTH_URL` 直到 200；再從回應 HTML 取出一個 `/_next/static/...` 資源，確認同主機回 200。
8. 健康檢查失敗 → **自動回退**：把 `.next` 換回 `.next-prev`、再執行 `RESTART_CMD`、印出醒目的失敗訊息、非零退出。
9. 成功 → 印出摘要（舊／新 `BUILD_ID`、build 秒數、`.next-prev` 保留位置與手動回退指令）。

### 測試（`tests/test_web_build_swap.py`，離線、不連網、不需要 node）
以暫存目錄與假的 `NPM_BIN`／`RESTART_CMD`／本機假 HTTP 端點（或把健康檢查指令抽成可覆寫的變數）驗證：
- build 失敗 → 非零退出、`WEB_DIR/.next/BUILD_ID` 內容不變、沒有 `.next-prev`。
- build 成功 → `WEB_DIR/.next/BUILD_ID` 為新值、`.next-prev/BUILD_ID` 為舊值、重啟指令被呼叫恰一次。
- 健康檢查失敗 → 自動回退：`WEB_DIR/.next/BUILD_ID` 回到舊值、重啟指令被呼叫兩次、非零退出。
- 未設 `NEXT_PUBLIC_SITE_ORIGIN`、維護旗標存在、`API_UNIT` 非 active：各自拒絕且不動 `.next`。
- 不得違反專案紅線 6（測試不連外網；本機 loopback 假端點若被 autouse socket-ban 擋住，改用可覆寫的健康檢查指令變數）。

### 實機等價驗證（實作者在本機做一次，需 node 與網路）
用 scratchpad 內的兩個暫存目錄模擬（不得動 repo 的 `web/.next`）：`LIVE`＝`web/` 原始碼副本＋`node_modules` 符號連結、先 build 一次當「線上版」；
改一行可辨識的文案後，以腳本（`WEB_DIR=LIVE`、`SUDO=""`、`RESTART_CMD` 換成重啟本機 `next start -p <port>` 的指令）跑完整流程，
確認：換入後 `next start` 服務的首頁 200、首頁引用的全部 `/_next/static` 資源 200、新文案出現在回應或 chunk 中。
另做一次「build 中途失敗」（例如暫時讓 `NPM_BIN` 指向必敗指令），確認線上版仍可正常服務。貼出指令與結果摘要。

### RUNBOOK
§4.2 改寫為：前端 build 一律用 `deploy/ops/web_build_swap.sh`；保留一段「腳本不可用時的舊流程」並標明其空窗風險；
寫明回退指令與 `.next-prev` 的保留語意；註明不再需要 build 前把整個 `web/` chown 給 ubuntu。

### 驗收
- `uv run pytest -q -p no:cacheprovider tests/test_web_build_swap.py` 全綠。
- `bash -n deploy/ops/web_build_swap.sh` 無錯；若本機有 `shellcheck` 則無 error 級訊息。
- 實機等價驗證兩個情境的結果摘要。
- `git status --short -- web/` 無輸出（Task B 不得改 `web/`）。

## 3. Task C 主線程驗收
- 重跑 Task A、B 全部驗收指令；全套 `uv run pytest -q` 與 `npm test` 在無其他測試並行時各跑一次。
- 以真實位址（Alpha）實算：`equity_index_ts` 與 `equity_index` 等長、時間戳遞增、末點在近兩週內；`return_sample_count == sample_count − 1`。

## 4. Task D 審核
派 `reviewer`：輸入＝`git diff`（相對 `a6d4ef8`）、本 plan、測試輸出。持續修到 0 Critical／0 Warning。

## 5. 部署步驟（使用者同意後才執行）
1. 前置三查；`git push`；乾淨 worktree → §3.2 兩段 rsync → chown（prune var）。
2. `filet-api` 重啟（新公式、`EXPLORE_INDEX_VERSION` 6；依 RUNBOOK §5.8c／§5.8e 處理探索快照）。
3. 前端：`deploy/ops/web_build_swap.sh`（首次實機使用；失敗時線上版不受影響，可退回舊流程）。
4. E3：`sudo systemctl start filet-leaderboard.service`，確認最新 watchlist 快照時間戳更新、`/api/leaders` 數字為新公式。
5. `DEPLOYED_VERSION`、回歸檢查、follower 未受影響、瀏覽器實看三張策略卡與首頁（Legacy 不在首頁）。

## 6. Task A 完成後的主線程裁決（2026-11-17）

builder 回報的現況差異與未涵蓋項，裁決如下：

1. **交易員詳情頁沒有 `EquityCurve`**（用的是 `PnlCurve`，端點也不輸出 `equity_index`）：`equity_index_ts` 只加在策略詳情端點，追認。交易員頁只改 N。
2. **`tests/test_public_strategies.py:356` 鍵集合斷言加入 `return_sample_count`**：契約新增鍵，追認。
3. **Task A2 @inline 對照線與 X 軸補強**（改用真實日期後才浮現，屬 E1 的必要收尾）。範圍：`web/src/components/EquityCurve.tsx` 與其測試、`web/src/lib/copy.ts`（僅在需要新文案時）。
   - **收盤價查找改為「當日或之前最近一筆」**：某取樣點當日沒有收盤價（週末、假日）時，取該日之前最近的一筆，回溯上限 4 個日曆日；超過則為 `null`。
   - **rebase 錨點改為「切片內第一個查得到收盤價的點」**，不是固定切片首點；錨點之前的點為 `null`（對照線從有資料處開始畫）。
     全部點都查不到 → 整條為 `null`（維持現行不畫）。
   - `OVERLAY_FETCH_DAYS` 與後端 `MAX_DAYS` 不動：超過可得範圍的舊點由上一條規則自然處理（線從有資料處開始）。
   - **X 軸標籤**：切片首末點相距超過 300 天時，標籤格式改 `YYYY-MM`；否則維持 `MM-DD`。
   - **錨例 B3**：取樣點日期 `2026-01-03`（六）、`2026-01-10`（六）、`2026-01-17`（六）；某對照標的只有 `2026-01-02`、`2026-01-09`、`2026-01-16` 的收盤價 `100`、`110`、`121`
     → 三點分別取到 `100`、`110`、`121`，rebase 後為 `1`、`1.1`、`1.21`。
   - **錨例 B4**：同上三個取樣點，但該標的最早的收盤價是 `2026-01-09`（`110`）、`2026-01-16`（`121`）→ 第一點為 `null`，錨點是第二點：`null`、`1`、`1.1`。
   - **錨例 B5**：切片首末點相距 400 天 → 首末標籤為 `YYYY-MM` 格式；相距 60 天 → `MM-DD`。
   - 驗收：`cd web && npm test` 全綠；B3／B4／B5 各有測試；把「當日或之前」改回「必須當日」時 B3 測試轉紅（變異後還原）。

## 7. 第一輪審核（reviewer，2026-11-17）：2 Critical／2 Warning／1 Suggestion → Task E、Task F 修正

### Task E @inline 部署腳本與 RUNBOOK（C1、C2、S1、觀察）
範圍：`deploy/ops/web_build_swap.sh`、`tests/test_web_build_swap.py`、`deploy/RUNBOOK.md`（§4.2 與回滾章節引用處）。其餘不動。

- **C1（`BUILD_DIR` 指錯會以 sudo 改 owner 並 `rsync --delete` 刪掉整棵樹）**：在**任何** `sudo`、`chown`、`rsync`、`rm` 動到 `BUILD_DIR` 之前加結構性護欄，全部以實體路徑（解析符號連結後）判斷：
  1. `BUILD_DIR` 不得為 `/`、不得等於 `WEB_DIR`、不得是 `WEB_DIR` 的上層目錄、不得位於 `WEB_DIR` 之內。
  2. **標記檔**：腳本只操作「自己建立的目錄」。`BUILD_DIR` 不存在 → 建立並寫入標記檔 `BUILD_DIR/.web_build_swap_dir`；
     已存在且為空 → 寫入標記檔；已存在、非空、且沒有標記檔 → **拒絕**（訊息說明這不是本腳本建立的目錄）。
  3. 拒絕時非零退出，`BUILD_DIR` 與 `WEB_DIR` 內容、owner 皆不變。
  - 測試（每條都要斷言「旁邊的無關目錄與檔案仍在、`.next` 的 digest 不變」）：`BUILD_DIR` = `WEB_DIR`；= `WEB_DIR` 的上層（上層內放一個 `precious/` 目錄）；在 `WEB_DIR` 之內；
    指向一個已有內容但無標記檔的目錄；指向已存在的空目錄（應通過並寫入標記檔）；第二次執行（有標記檔，應通過）。
- **C2（照文件手動回退會把線上 `.next` 搬走）**：
  1. 腳本新增 `--rollback` 模式：前置檢查 `WEB_DIR/.next-prev/BUILD_ID` 存在且非空，否則拒絕並說明「沒有可回退的版本，請走整包重部署」，**不動任何東西**；
     通過則沿用既有的回退函式（`.next` → `.next-failed`、`.next-prev` → `.next`、重啟、健康檢查），成功回 0，回退後健康檢查失敗回 5。`--rollback` 同樣受鎖與維護旗標約束。
  2. 成功摘要與 RUNBOOK 的「手動回退」一律改成呼叫 `web_build_swap.sh --rollback`，不再列裸的四行 `mv`。
  3. RUNBOOK 回滾章節改正：§3.2 的 rsync 之後 `.next-prev` 已不存在，該句刪除或改寫為正確的適用時機（「新版剛由腳本換入、尚未跑過下一次 §3.2」）；
     `.next-prev` 語意補上「腳本自動回退（結束碼 4）之後不存在 `.next-prev`」。
  - 測試：無 `.next-prev` → 拒絕、`.next` digest 不變、重啟次數 0；有 `.next-prev` → `.next/BUILD_ID` 變回舊值、重啟恰 1 次、原本的新版在 `.next-failed`。
- **S1（換入完成到 `STATE=swapped` 之間收到訊號）**：`swapping` 分支若發現 `BUILD_DIR/.next` 已不存在且 `WEB_DIR/.next`、`WEB_DIR/.next-prev` 都在，視同已換入，走回退路徑。附測試或說明為何無法離線測。
- **觀察（`package-lock.json` 有變動時的原地 `npm ci`）**：RUNBOOK 該段加一句——這個情形下「build 失敗線上完全不受影響」的保證不成立（舊 `.next` 會配上新依賴），並建議在維護窗進行。

### Task F @inline 對照線錨點基準與 X 軸時間比例
範圍：`web/src/components/EquityCurve.tsx` 與其測試。其餘不動。

- **W1（錨點不在切片首點時對照線起點水位錯）**：rebase 的乘數改用**錨點當天的主曲線值**（不是切片首點的值），使對照線在錨點那天與主曲線重合。
  - **錨例 B6**：主曲線切片值 `[100, 120, 150]`，某標的收盤價對齊後 `[null, 110, 121]` → 對照線 `[null, 120, 132]`。
  - 錨點就是切片首點時，結果與現行相同（B3 不變）。
- **X 軸依時間比例排點（E1 的收尾；審核觀察）**：有 `timestamps` 時，各點的 x 位置＝`(ts_i − ts_first) / (ts_last − ts_first)` 乘以繪圖寬度，主曲線與對照線共用；`ts_last == ts_first` 的退化情形不得除以零。
  無 `timestamps` 的舊路徑維持等距。
  - **錨例 B7**：三個點的時間戳為第 0、10、30 天 → x 比例 `0`、`1/3`、`1`。
- 變異：把乘數改回切片首點值 → B6 測試轉紅；把 x 改回等距 → B7 測試轉紅（各貼紅燈後還原）。

### 主線程自行處理
- 首頁 `pickFeatured`：沒有任何策略標 `featured` 時，優先從 `listable` 的策略挑第一筆，再退回全部的第一筆（避免 hero 顯示暫停中的策略）。附測試。

### Task F2 @inline X 軸刻度標籤與時間比例一致（Task F 回報後的主線程裁決）
範圍：`web/src/components/EquityCurve.tsx` 與其測試。不動 CSS、後端、`copy.ts`。

點改依時間比例排列後，刻度標籤仍以「等距索引挑點＋CSS 等距排列」產生，中間的標籤會偏離它標示的時間位置。裁決：
有 `timestamps` 時，刻度改為**按時間等距**——刻度數 `k` 沿用現行挑點邏輯算出的數量，第 `j` 個刻度的時間＝`ts_first + j/(k−1) × (ts_last − ts_first)`，
標籤顯示**該時間**的 UTC 日期（不是最近資料點的日期）；格式沿用 300 天規則（`YYYY-MM`／`MM-DD`）。`k == 1` 或 `ts_last == ts_first` 時只顯示首點日期，不除以零。
無 `timestamps` 的舊路徑不變。

- **錨例 B8**：時間戳為 `2026-01-01`、`2026-01-11`、`2026-01-31`（UTC 00:00），刻度數 3 → 標籤 `01-01`、`01-16`、`01-31`。
- 既有 B2「X 軸首末標籤等於切片首末點的 UTC 日期」、B5 的首末標籤格式測試必須仍成立。
- 變異：把刻度改回「挑資料點的日期」→ B8 轉紅（中間標籤會是 `01-11`）；還原後轉綠。
- 驗收：`cd web && npm test` 全綠。

## 8. 第二輪審核（fresh reviewer，2026-11-17）：0 Critical／2 Warning／1 Suggestion → Task G 修正

Task F／F2、後端、首頁未發現問題；C1 護欄經 12 種繞過嘗試皆擋下；S1 與第一輪已確認的性質未被破壞。兩個 Warning 都在 `--rollback`。

### Task G @inline `--rollback` 的中斷安全與缺 `.next` 復原
範圍：`deploy/ops/web_build_swap.sh`、`tests/test_web_build_swap.py`、`deploy/RUNBOOK.md` §4.2（及回滾章節引用處）。其餘不動。

- **W1（兩次 rename 之間被訊號打斷 → 線上沒有 `.next`）**：把每一組「成對的 rename」做成臨界區——進入前忽略 `INT`／`TERM`／`HUP`，兩個 rename 都結束（或失敗處理完）後才恢復原本的訊號處理。
  適用三處：一般模式步驟 5 的換入、自動回退、`--rollback`。`on_exit` 內執行回退時訊號也必須維持被忽略，直到回退的 rename 完成。
  臨界區內不得包含 build、重啟或健康檢查（只有 rename 與其失敗補救）。
  - 測試：以假的 `SUDO` 包裝在「`.next` → `.next-failed` 之後、`.next-prev` → `.next` 之前」對腳本送 `SIGTERM`（`--rollback` 模式），斷言結束後 `WEB_DIR/.next/BUILD_ID` 存在且為舊版（rename 成對完成）；一般模式步驟 5 兩個 rename 之間送 `SIGTERM` 亦同（結束後 `.next` 必存在）。
  - 變異：移除臨界區 → 上述測試轉紅（`.next` 缺席）；還原後轉綠。
- **W2（`.next` 缺席、`.next-prev` 存在時 `--rollback` 失敗且先刪了 `.next-failed`）**：`--rollback` 與回退函式改為：
  1. `WEB_DIR/.next` **存在** → 現行流程（刪舊 `.next-failed`、`.next` → `.next-failed`、`.next-prev` → `.next`）。
  2. `WEB_DIR/.next` **不存在**且 `.next-prev/BUILD_ID` 有效 → **復原模式**：不碰 `.next-failed`，直接 `.next-prev` → `.next`，重啟、健康檢查；訊息明講「線上原本沒有 `.next`，已由 `.next-prev` 復原」。
  3. 任何情況下，在確認第二個 rename 的來源（`.next-prev`）有效之前，不得刪除既有的 `.next-failed`。
  - 腳本裡叫人「立刻人工處理」且舊版在 `.next-prev` 的訊息，改為明確指示執行 `--rollback`。
  - 測試：`.next` 缺席＋`.next-prev` 有效（另放一個既有的 `.next-failed` 當證據）→ 結束碼 0、`.next/BUILD_ID` 為 `.next-prev` 的值、既有 `.next-failed` 內容不變、重啟恰 1 次。
- **Suggestion（測試補強）**：「空目錄被認領」「第二次執行」兩個會通過的 C1 測試補上「旁邊無關目錄與檔案仍在」的斷言；新增護欄形狀測試：標記檔是符號連結、標記檔是目錄、目錄只含隱藏檔、`BUILD_DIR` 是一般檔案、`BUILD_DIR` 是懸空符號連結（皆應拒絕且不動任何東西）。
- **RUNBOOK 用語修正（審核觀察）**：
  - `.next-prev` 何時存在改寫為單一規則：只在「腳本成功換入之後，到下一次 §3.2 部署、下一次腳本執行、或任何一次回退之前」存在；是否可回退一律以 `--rollback` 的前置檢查為準，不靠人判斷。
  - 結束碼說明補上：`130`／`143`／`129` 表示被訊號中斷；換入之後被中斷會自動回退（與結束碼 4 相同，`.next-prev` 已不存在）。
  - 補上復原模式的說明（`.next` 缺席時 `--rollback` 會由 `.next-prev` 復原）。

驗收：`uv run pytest -q -p no:cacheprovider tests/test_web_build_swap.py tests/test_deploy_artifacts.py` 全綠；`bash -n` 無錯；既有測試不得刪除或放寬。

## 9. 第三輪審核（fresh reviewer，2026-11-17）：1 Critical／5 Warning／2 Suggestion → Task H（改結構，不再逐點修）

三輪審核各在不同位置抓到同一類問題（訊號／中斷下的狀態與結束碼）。依 judgment.md §4「同形錯誤跨 call site 重現＝缺邊界」，
**停止逐點修補，改成單一結構**：

### Task H @inline 單一不可中斷階段＋依實際狀態決定結束碼
範圍：`deploy/ops/web_build_swap.sh`、`tests/test_web_build_swap.py`、`deploy/RUNBOOK.md` §4.2（及回滾章節引用處）。其餘不動。

**H1 訊號模型（取代 §8 W1 的臨界區與 §7 S1 的特判）**
- 腳本分兩個階段：
  - **可中斷階段**（前置檢查、準備 `BUILD_DIR`、build、驗證產物）：INT／TERM／HUP 照常結束腳本；此階段不改動 `WEB_DIR`。
  - **不可中斷階段**：從「第一個會改動 `WEB_DIR` 的指令之前」到腳本結束（換入、重啟、健康檢查、自動回退、最終狀態報告），以及 `--rollback` 從第一個 rename 之前到結束。
    進入時以**單一函式**（例如 `enter_uninterruptible`）執行 `trap '' INT TERM HUP PIPE`，此後不再恢復。子行程（`sudo`、`mv`、`systemctl`、`curl`）繼承被忽略的訊號。
    進入時印一行：「從這裡開始不可中斷，最長約 N 秒」（N 由 `HEALTH_TIMEOUT_S` 推得）。
- 不可中斷階段內所有輸出都不得因寫入失敗（讀端已關閉）而讓腳本結束：輸出函式吞掉寫入錯誤，且**同時附加寫入 `LOG_FILE`**，終端機不在了記錄仍在。
- `crit_begin`／`crit_end`／`crit_mv`／`CRIT_SIG`／`IN_EXIT` 與 S1 的「視同已換入」特判全部移除。
  驗收：`grep -c -E "crit_begin|crit_end|crit_mv|CRIT_SIG" deploy/ops/web_build_swap.sh` 為 0；設定不可中斷的 `trap ''` 只出現在那一個函式內。
- flock 的 fd 9 不得被 build 子行程繼承（build 指令加 `9>&-`）。

**H2 結束碼＝最終實際狀態（預註冊表；由檢查檔案系統得出，不只靠流程變數）**

| 結束碼 | 模式 | 最終狀態（腳本結束時必為真） |
|---|---|---|
| 0 | 一般 | `.next` 是新版、已重啟、健康檢查通過；`.next-prev` 是舊版 |
| 0 | `--rollback` | `.next` 是原 `.next-prev` 的版本、已重啟、健康檢查通過 |
| 1 | 兩者 | 沒有改動 `WEB_DIR`；或換入中途失敗且已還原成舊版（服務未重啟、內容與執行前相同） |
| 2 | 兩者 | 用法錯誤，未動任何東西 |
| 4 | 一般 | 新版曾換入但失敗，已自動回退：`.next` 是舊版、已重啟、健康檢查通過；失敗的新版在 `.next-failed` |
| 5 | 兩者 | 其他一切：需要人處理（`.next` 缺席、回退的 rename 失敗、回退後重啟失敗、回退後健康檢查失敗、`--rollback` 的重啟或健康檢查失敗…） |
| 129／130／143／141 | 兩者 | 只可能發生在可中斷階段：未改動 `WEB_DIR`（語意同 1） |

- 換入步驟的任何指令回報失敗時，以檔案系統實況決定下一步：`.next/BUILD_ID` 已是新版 → 視為已換入、走自動回退；`.next` 缺席且 `.next-prev` 有效 → 還原；兩者皆非 → 5。
- **最終狀態報告**（每次結束都印，stdout＋`LOG_FILE`）：結束碼、`.next`／`.next-prev`／`.next-failed` 各自的 `BUILD_ID`（或「不存在」）、服務是否已重啟、健康檢查結果，以及**下一步該執行的確切指令**：
  - `.next` 缺席且 `.next-prev` 有效 → `web_build_swap.sh --rollback`
  - 檔案已換回舊版但重啟失敗 → 印出 `RESTART_CMD` 本身（只差重啟，不要叫人重部署）
  - 回退無法進行（`.next-prev` 無效）且 `.next` 是新版 → 整包重部署（RUNBOOK §9.3）
  - 其餘 → 明講目前線上是哪個 `BUILD_ID`、建議檢查項目
- `--rollback` 的前置檢查除 `.next-prev/BUILD_ID` 非空外，另要求 `.next-prev/static` 存在且非空（與步驟 4 的產物驗證同一判準）。

**H3 RUNBOOK**
- §4.2 的結束碼表改成 H2 的表；訊號說明改成「build 期間可中斷且不影響線上；開始換入後不可中斷（最長約 N 秒）」。
- 移除本輪之前在「舊流程」尾端新增的 `sudo systemctl restart filet-dashboard`（plan 範圍外；首次部署時 unit 尚未安裝，且繞過 `Wants=filet-api` 檢查）——舊流程段落還原為原本的指令，只保留「空窗風險」說明。
- 「首次使用檢查清單」的待確認項新增：Ubuntu 的 sudo（`use_pty`）是否會把訊號轉發給被忽略訊號的子行程、bash 5 下 `trap ''` 的繼承。

**H4 測試（全離線；訊號測試必須能證明訊號確實在指定時機送達——假指令在送訊號前寫入旗標檔，測試斷言旗標檔存在）**
- 可中斷階段：build 期間送 TERM → 結束碼 143、`WEB_DIR` digest 不變、沒有重啟。
- 不可中斷階段，各送一次 TERM、INT、HUP（至少各一個情境，合計涵蓋下列每個時機）：換入的兩個 rename 之間；重啟期間；健康檢查期間；自動回退刪除 `.next-failed` 期間；自動回退的兩個 rename 之間；自動回退的重啟期間；`--rollback` 的兩個 rename 之間；`--rollback` 的重啟期間。
  每個情境斷言：腳本照常跑完、結束碼與「沒有收到訊號時」相同（0 或 4）、最終狀態符合 H2 表。
- 對整個行程群組送訊號（測試以新 session 啟動腳本，對群組送 TERM）：`mv` 與重啟不被打斷。
- SIGPIPE：stdout／stderr 讀端在換入後關閉，健康檢查失敗 → 仍完成自動回退（`.next` 為舊版、結束碼 4），`LOG_FILE` 內有最終狀態報告。
- H2 表每一列至少一個測試直接斷言結束碼與最終狀態；特別是：`.next` 缺席 → 5（不是 1）；回退的重啟失敗 → 5 且報告含 `RESTART_CMD`、**不含**「重部署」；rename 成功但指令回報失敗（假 sudo 先 rename 再 `exit 1`）→ 4。
- 變異：移除 `enter_uninterruptible` 的呼叫 → 不可中斷階段的訊號測試轉紅；把「`.next` 缺席」的結束碼改回 1 → 對應測試轉紅。
- 既有測試：因訊號模型改變而不再適用的（§7 S1、§8 W1 的臨界區測試）改寫成新模型下的對應斷言，逐一列出改寫前後對照；其餘不得刪除或放寬。

驗收：`uv run pytest -q -p no:cacheprovider tests/test_web_build_swap.py tests/test_deploy_artifacts.py` 全綠；`bash -n` 無錯；上述兩個 grep 判準。

### 部署前演練（新增到 §5 部署步驟的第 0 步，使用者同意後才做）
在正式機的 `/tmp` 底下建立假的 `WEB_DIR`／`BUILD_DIR`，以**真的 `sudo`**、假的 `NPM_BIN` 與 `RESTART_CMD`、真的 `systemctl is-active`（唯讀）執行腳本，
演練：成功換入、build 失敗、健康檢查失敗自動回退、`--rollback`、不可中斷階段送 TERM／HUP。目的：在目標環境（Ubuntu、bash 5、真 sudo、flock、`stat -c`）確認行為與本機一致。
演練只寫 `/tmp`，不碰 `/opt/filet`、不重啟任何服務。

## 10. 第四輪審核（fresh reviewer，2026-11-17）：0 Critical／3 Warning／5 Suggestion → Task I

H1 訊號模型經審核確認成立（移除進入點 → 訊號測試轉紅；12 個自設情境實測）。剩下三個不同性質的 Warning。

### Task I @inline 每次執行獨立的 build 目錄、刪除前先失效、報告如實
範圍：`deploy/ops/web_build_swap.sh`、`tests/test_web_build_swap.py`、`deploy/RUNBOOK.md` §4.2（及引用處）。其餘不動。

- **I1（W1：被中斷的舊 build 殘留行程與下一次 build 同時寫 `BUILD_DIR/.next`，混雜產物被換上線且回報 0）**——結構性修法：
  1. **每次執行用獨立的工作目錄** `BUILD_DIR/run-<UTC 時間戳>-<PID>/`（原始碼副本、`node_modules` 符號連結、`.next/cache` 副本都放在裡面）；build 與產物驗證都在這個目錄。
     任何先前執行殘留的行程只會寫它自己的 `run-*` 目錄，結構上不可能混進本次產物。標記檔仍在 `BUILD_DIR` 根目錄。
  2. 步驟 2 開頭清掉舊的 `BUILD_DIR/run-*`（刪不乾淨不算失敗：殘留行程可能還在寫，下次再清；但要印一行警告，說明有先前的 build 可能仍在執行）。
     成功換入後刪除本次的 `run-*`（`.next` 已被 rename 走）；失敗時保留本次的 `run-*` 供排查並在報告中寫出路徑。
  3. 可中斷階段收到訊號時，盡力終止整棵 build 行程樹：有 `setsid` 時以新 session 啟動 build 並對整個行程群組送 TERM；沒有 `setsid`（macOS）維持現行為。這一步只為了省資源，正確性由第 1 點保證。
  4. 檔頭與 RUNBOOK 中「終止進行中的 build」的說法改成如實描述（盡力終止；殘留行程不影響下一次執行的產物）。
  - 測試：假 `NPM_BIN` 留下一個延遲寫檔的孫行程（寫到它啟動時的工作目錄的 `.next/server/stale.js`），TERM 腳本後立刻重跑 →
    重跑結束碼 0 且**換上線的 `.next` 內沒有 `stale.js`**；兩次執行的 `run-*` 目錄名不同。
    變異：把工作目錄改回固定的 `BUILD_DIR` → 該測試轉紅。
- **I2（W2：刪 `.next-prev` 只刪一半 → 殘骸仍被判定為有效、報告引導 `--rollback` 把殘缺版換上線）**——「刪除前先失效」：
  1. 任何要刪除 `.next-prev` 或 `.next-failed` 的地方，一律**先 rename 成 `<名稱>.deleting-<PID>` 再刪**。rename 失敗 → 該步驟中止，原目錄完好；rename 成功後刪除失敗只留下垃圾目錄（不是回退來源），印警告、流程繼續，下次執行時清掉。
  2. 步驟 2 開頭一併清掉 `WEB_DIR` 內殘留的 `.next-prev.deleting-*`、`.next-failed.deleting-*`（刪不掉只警告）。原始碼同步到 build 目錄時排除 `WEB_DIR` 內所有 `.next*` 目錄。
  3. 結束碼 1 的「下一步」固定為：線上未變，排除原因後直接重跑；**明講不要執行 `--rollback`**。只有結束碼 5 且 `.next` 缺席、或操作者事後自行判斷新版有問題時才用 `--rollback`。
  - 測試：假 sudo 讓「刪除 `.next-prev.deleting-*`」只刪一部分並回報失敗 → 換入照常完成（結束碼 0）、不存在名為 `.next-prev` 的殘缺目錄可供 `--rollback` 誤用（`.next-prev` 是剛換下來的完整舊版）；
    假 sudo 讓「`.next-prev` → `.next-prev.deleting-*` 的 rename」失敗 → 結束碼 1、`.next` 與 `.next-prev` 的 digest 都與執行前相同、報告的「下一步」含「不要」與「--rollback」且不是建議執行它。
- **I3（W3：報告的「服務重啟」一行在回退失敗時誤報）**：記錄「最後一次成功重啟當下 `.next` 的 `BUILD_ID`」（沒有成功重啟過則為空），不得在未實際重啟時清除。
  報告改為兩行：`最後一次成功重啟：<當時的 BUILD_ID｜本次執行未成功重啟過>`；`目前 .next 與正在服務的版本：<一致｜不一致——檔案已變更但尚未重啟>`。
  - 測試：健康檢查失敗、自動回退時既有 `.next-failed` 無法移走 → 結束碼 5，報告寫明最後一次成功重啟的是新版的 `BUILD_ID`、目前 `.next` 仍是新版（一致）。
- **I4（Suggestion 採納）**：
  1. 判定「回退的檔案層完成」另需 `.next-prev` 已不存在（新舊 `BUILD_ID` 相同時不得偽陽性）。附測試。
  2. `LOG_FILE` 無法建立或寫入 → 前置檢查階段直接失敗（結束碼 1、未動任何東西、訊息說明），不得靜默降級成 `/dev/null`。預設 log 路徑改用 `mktemp` 產生（權限 0600）。附測試。
  3. 可中斷階段因 `set -e` 靜默結束的路徑：加 `ERR` trap 印出「ERROR：未預期的指令失敗（行號）」，使報告中「排除上面的 ERROR 原因」恆有對應訊息。附測試。
  4. 健康檢查的靜態資源逐一檢查也受同一個總時限約束，使「最長約 N 秒」成立。
  5. 腳本註解中關於結束碼 141 的敘述改成與實際行為一致（保留碼）。
  6. RUNBOOK「舊流程」段落的指令**與註解**還原成 `git show HEAD:deploy/RUNBOOK.md` 中 §4.2 的原文（含 `npm ci` 那行的原註解），只在段落開頭保留一句空窗風險說明。
- H2 結束碼表不變；RUNBOOK 同步更新（`run-*` 目錄、`.deleting-*` 殘骸、結束碼 1 不要 `--rollback`、報告的兩行重啟資訊）。

驗收：`uv run pytest -q -p no:cacheprovider tests/test_web_build_swap.py tests/test_deploy_artifacts.py` 連跑三次全綠；`bash -n` 無錯；
`grep -c -E "crit_begin|crit_end|crit_mv|CRIT_SIG" deploy/ops/web_build_swap.sh` 仍為 0、`trap ''` 仍只在單一函式內；既有測試不得刪除或放寬（因 I1 目錄結構改變而需調整路徑的測試，逐一列出）。

## 11. 第五輪審核（fresh reviewer，2026-11-17）：0 Critical／2 Warning／4 Suggestion → Task J

I1／I2／I4 確認落地（符號連結、同秒執行、殘骸、總時限等 13 個自設情境實測）；結束碼皆正確。剩下的兩個 Warning 都是最終報告的文字不符事實。

### Task J @inline 報告文字只陳述已知事實
範圍：`deploy/ops/web_build_swap.sh`、`tests/test_web_build_swap.py`、`deploy/RUNBOOK.md` §4.2（及引用處）。其餘不動。

- **J1（W1：`--rollback` 被拒絕時報告卻叫人「不要執行 --rollback」）**：結束碼 1 且未改動 `WEB_DIR` 時，「下一步」依模式區分：
  - 一般模式 → 維持現行文字（線上未變，排除原因後直接重跑；不要執行 `--rollback`）。
  - `--rollback` 模式 → 「回退**沒有執行**，線上未變。依上面 ERROR 的指示處理後重跑 `--rollback`：維護旗標 → 確認後加 `--force`；`filet-api` 非 active → 確認後加 `--allow-api-start`；
    另一個執行持有鎖 → 等它結束；`.next-prev` 不是完整版本 → 沒有可回退的版本，走整包重部署（RUNBOOK §9.3）。」不得出現「不要執行 --rollback」「沒有任何東西需要回退」。
  - RUNBOOK 結束碼 1 的說明同步改成依模式區分。
  - 測試：`--rollback` 分別因無 `.next-prev`、維護旗標存在而回 1 → 報告的「下一步」不含「不要執行 --rollback」，且含對應的處理方式（`§9.3`／`--force`）。
- **J2（W2：重啟失敗後報告把「不知道」寫成「一致」）**：新增「最近一次重啟嘗試的結果」事實（未嘗試／成功／失敗）。「目前 .next 與正在服務的版本」一行只能是三種之一：
  1. 最近一次重啟嘗試**成功** → 與現行相同：比較當時的 `BUILD_ID` 與目前 `.next`，寫「一致」或「不一致——檔案已變更但尚未重啟」。
  2. 最近一次重啟嘗試**失敗** → 「未知——最近一次重啟指令失敗，服務可能已停止；以 `systemctl is-active filet-dashboard` 與健康檢查為準」。不得寫「一致」，也不得斷言正在服務的是哪一版。
  3. 本次執行**未嘗試**重啟 → 「服務未被本次執行重啟（仍是執行前啟動的行程）」；若 `.next` 已與執行開始時不同，加註「檔案已變更但尚未重啟」。
  - 測試：一般模式兩次重啟都失敗 → 結束碼 5、該行含「未知」且不含「一致」；`--rollback` 重啟失敗 → 該行含「未知」、不含「正在服務的是」；換入中途失敗已還原（未嘗試重啟）→ 第 3 種文字。
- **J3（Suggestion 採納）**：
  1. 復原模式（`.next` 缺席、由 `.next-prev` 復原）下，任何補救分支都**不得**把 `.next-failed` 搬成 `.next`（§8 W2.2）；復原失敗就以結束碼 5 結束並如實報告 `.next` 缺席。非復原模式的補救（把剛搬走的那一版放回去）維持。附測試：`.next` 缺席＋`.next-prev` 的 rename 持續失敗＋既有 `.next-failed` → 結束碼 5、`.next` 仍缺席、`.next-failed` 內容不變。
  2. `ERR` trap 不直接印訊息，改為只記錄（行號與指令）；`on_exit` 在「結束碼非 0、且沒有任何 `die` 印過 ERROR」時才印出那一筆。使 `$(...)` 內函式的正常失敗不再多印一行誤導的 ERROR。附測試：`BUILD_DIR` 的上層是一般檔案 → 輸出中 `ERROR` 開頭的行恰一行（`die` 的訊息）。
  3. 清理舊 `run-*` 改用 `${SUDO}`（先前換入中止時留下的 `run-*/.next` 可能已是 root 擁有）；`run-*` 若是符號連結，只移除連結本身、不跟隨。警告文字改成不臆測原因（「沒有刪乾淨；下次執行再清」）。
  4. 兩個靠固定延遲的 I1 測試改成握手式：殘留的孫行程等待旗標檔出現才寫檔，測試在第二次執行確實進入 build 之後才放行；`setsid` 測試以輪詢行程是否存在取代固定等待。
  5. RUNBOOK 中「本機測試全以 `SUDO=""` 跑」改成如實描述（部分測試使用假的 sudo 包裝；真 sudo 僅能在正式機演練時確認）。

驗收：`uv run pytest -q -p no:cacheprovider tests/test_web_build_swap.py tests/test_deploy_artifacts.py` 連跑三次全綠；`bash -n` 無錯；
`grep -c -E "crit_begin|crit_end|crit_mv|CRIT_SIG" deploy/ops/web_build_swap.sh` 為 0、`trap ''` 只在單一函式內；H2 結束碼表不變且每列仍有測試；既有測試不得刪除或放寬。

## 12. 第六輪審核（fresh reviewer，2026-11-17）：0 Critical／1 Warning／4 Suggestion → Task K

矩陣 25 格逐一確認（其中 16 格實測、6 格「下一步」實際照做）：結束碼 0／1／4 無偽陽性，報告三行為真，「下一步」可照做。只剩用法錯誤那一格。

### Task K @inline 用法錯誤與被訊號中斷時的「下一步」、零星修正
範圍：`deploy/ops/web_build_swap.sh`、`tests/test_web_build_swap.py`、`deploy/RUNBOOK.md` §4.2。其餘不動。

- **K1（W：參數打錯時報告叫想回退的人「不要執行 --rollback」）**：結束碼 2（用法錯誤）的報告不得對線上狀態或是否需要回退做任何斷言。
  「模式」一行寫「未定（參數錯誤）」；「下一步」固定為：「參數錯誤，未動任何東西。修正參數後重跑（`--help` 看用法）。」
  不得含「不要執行 --rollback」「沒有任何東西需要回退」「回退沒有執行」。
  - 測試：`--rollbak`（打錯字）、`--force --bogus --rollback`（未知參數在 `--rollback` 之前）、`--rollback --bogus` 三種都回 2，報告符合上述。
- **K2（S：被訊號中斷時「下一步」指向不存在的 ERROR）**：結束碼 129／130／143（只可能發生在可中斷階段、未改動 `WEB_DIR`）的「下一步」改為：
  「被訊號中斷，線上未受影響；直接重跑即可。」兩種模式共用，不提 ERROR。附測試（一般模式 build 期間 TERM；`--rollback` 前置階段若無法穩定送達訊號則以一般模式的測試為準並說明）。
- **K3（S：`HEALTH_TIMEOUT_S=08` 進入不可中斷階段後算術錯誤、健康檢查被跳過）**：前置檢查把 `HEALTH_TIMEOUT_S` 正規化成十進位整數（去除前導零；空字串、非數字、0 皆拒絕並回 2）。
  附測試：`08` → 當成 8 正常運作（健康檢查有執行）；`0`、`abc` → 回 2、未動任何東西。
- **K4（S：文字與行為不一致）**：
  1. 腳本註解中殘留的「`rsync --delete` 操作 `BUILD_DIR`」敘述改成現況（清理舊 `run-*`、同步到本次 `run-*`）。
  2. RUNBOOK 引用的舊警告字樣改成腳本現行的警告文字。
  3. RUNBOOK 結束碼 1 的說明改精確：「線上的 `.next` 未變、服務未重啟；若在換入步驟中止，更早的 `.next-prev` 可能已被移除（`--rollback` 的前置檢查會如實拒絕）」。腳本報告中結束碼 1 的「含意」文字同步。
  4. 鎖的測試（第二個執行被拒）改成握手式，不靠第一個執行的固定睡眠時間。

驗收：`uv run pytest -q -p no:cacheprovider tests/test_web_build_swap.py tests/test_deploy_artifacts.py` 連跑三次全綠；`bash -n` 無錯；既有測試不得刪除或放寬；
`grep -n "rsync --delete" deploy/ops/web_build_swap.sh` 零命中。

## 13. 第七輪審核（fresh reviewer，2026-11-17）：**PASS**，0 Critical／0 Warning／4 Suggestion → Task L（採納建議）

結論原文要點：完成正式機演練後，以目前狀態可用於正式機前端部署；演練需加做一項「真 build」（R6）。
破壞性指令逐條確認作用範圍受限；路徑含空白、`*`、開頭 `-` 實測安全；線上不變量、H2 結束碼表、換入產物權限、前置檢查皆成立。

### 主線程裁決
- **S3 追認**：`HEALTH_TIMEOUT_S` 為空字串時使用預設值 30（與腳本其他變數「空字串＝預設」的慣例一致）；§12 K3「空字串拒絕」一語作廢。
- 主線程另修：`tests/test_web_build_swap.py` 移除未使用的 `import threading`（ruff F401）。

### Task L @inline 採納三個建議（都在前置檢查或報告文字，不動換入／回退邏輯）
範圍：`deploy/ops/web_build_swap.sh`、`tests/test_web_build_swap.py`、`deploy/RUNBOOK.md` §4.2。
- **L1（S1）**：一般模式的前置檢查改用與回退來源相同的判準 `valid_bundle`（`BUILD_ID` 非空且 `static/` 非空）檢查線上 `WEB_DIR/.next`；不符 → 結束碼 1、未動任何東西，訊息說明「線上 `.next` 不是完整版本，無法作為回退來源；本腳本不處理這種狀態，走 RUNBOOK §4.2 舊流程或 §9.3」。
  測試：線上 `.next` 有 `BUILD_ID` 但無 `static/` → 結束碼 1、`WEB_DIR` digest 不變、沒有 build。
- **L2（S2）**：`HEALTH_TIMEOUT_S` 正規化後必須在 1–600 之間；先以字串長度（最多 4 位數字）擋下會溢位的值，再做十進位轉換。超出範圍 → 結束碼 2（參數錯誤）。
  測試：`601`、`3000`、`99999999999999999999`、`9223372036854775808` → 結束碼 2、訊息含允許範圍、`WEB_DIR` digest 不變；`600` → 接受。
- **L3（S4）**：結束碼 2 的報告不印任何關於服務行程的斷言——「最後一次成功重啟」「目前 .next 與正在服務的版本」兩行在用法錯誤時改印「（參數錯誤，未檢查）」；
  腳本註解與 RUNBOOK 中「腳本什麼都沒看」改成如實（「未做任何檢查與改動；報告僅列出目錄現況」）。
  測試：用法錯誤的報告不含「仍是執行前啟動的行程」。

驗收：`uv run pytest -q -p no:cacheprovider tests/test_web_build_swap.py tests/test_deploy_artifacts.py` 連跑兩次全綠；`bash -n` 無錯；`uv run ruff check src tests scripts` 無錯；既有測試不得刪除或放寬。

## 14. 部署前演練清單（取自第七輪審核的目標環境風險表；使用者同意後才做）

全部在正式機 `/tmp/wbs/` 底下，以假的 `WEB_DIR`／`BUILD_DIR`、`LOCK_FILE=/tmp/wbs/lock`、`MAINTENANCE_FLAG=/tmp/wbs/flag`、真的 `SUDO=sudo` 進行；不寫 `/opt/filet`、不重啟任何服務、不使用 port 3000。

| # | 要確認的事 | 做法 | 預期 |
|---|---|---|---|
| R10 | sudo 免密碼、不需 tty | `sudo -n true` | 成功 |
| R4 | `stat -c %d` 同檔案系統檢查 | `stat -c %d /opt/filet/spark/web /opt/filet`（唯讀）；演練時把 `BUILD_DIR` 指到另一個檔案系統 | 兩者相同；另一檔案系統被拒 |
| R3 | `flock` 與 `/run/lock` | `sysctl fs.protected_regular`；兩個執行同時跑 | 第二個被拒「另一個 web_build_swap 正在執行」 |
| R7 | `filet-dashboard` 讀得到 `root:root` 的 `.next` | `systemctl show filet-dashboard -p ReadWritePaths -p ProtectSystem`；`sudo -u filet-dashboard test -r /opt/filet/spark/web/.next/BUILD_ID`（唯讀） | 可讀 |
| R8 | 健康檢查的正則抽得到真首頁的資源 | 對 `http://127.0.0.1:3000/` 一次 GET 後套同一個正則 | 資源數 > 0 |
| R11／R12 | 磁碟與記憶體 | `df -h /opt /tmp`、`free -m` | 足夠 |
| — | 基本流程（假 build） | 成功換入；build 失敗；健康檢查失敗自動回退；`--rollback`；`--rollback` 無舊版被拒 | 結束碼 0／1／4／0／1，報告與檔案狀態相符 |
| R2 | util-linux `setsid` 行為 | 假 `NPM_BIN` 記錄自己的 pid／pgid／sid；build 期間送 TERM | pid＝pgid＝sid；孫行程被終止；結束碼 143 |
| R1／R9 | 真 sudo（`use_pty`）與 bash 5 下的不可中斷階段 | 有 tty 的連線，`RESTART_CMD` 內 `sudo sleep 15`，期間對行程群組送 TERM、再試 HUP；另做一次「直接關掉 ssh 連線」與「輸出讀端提早關閉」 | 流程照常跑完，結束碼與無訊號時相同；`LOG_FILE` 有完整報告 |
| R5 | `chown -R` 不跟隨符號連結 | `BUILD_DIR` 內放一個指向 root 擁有檔案的符號連結 | 目標擁有者不變 |
| R6 | **真的 `next build`**：在 `run-*` 內 build、`node_modules` 為符號連結、換入後 `run-*` 被刪 | 複製 `web/` 原始碼到 `/tmp/wbs/web`（`node_modules` 連到正式的、唯讀使用），`RESTART_CMD` 啟動 `next start -p 3999`，`HEALTH_URL=http://127.0.0.1:3999/` | build 成功、換入後首頁與資源 200、`.next/server` 內不引用 `run-*` 路徑 |

任一項與預期不同 → 不使用腳本部署，改回舊流程或先修腳本。

## 15. Task L 驗收（主線程，2026-11-17）
- `bash -n` 無錯；`uv run pytest -q -p no:cacheprovider tests/test_web_build_swap.py tests/test_deploy_artifacts.py` → 136 passed；ruff 無錯。
- 主線程自行重現：線上 `.next` 無 `static/` → 結束碼 1、未建立 `BUILD_DIR`；`HEALTH_TIMEOUT_S=3000`／`601`／`99999999999999999999` → 結束碼 2。
- builder 回報七個受保護函式（`swap_in`、`swap_back`、`auto_rollback`、`decide_code`、`enter_uninterruptible`、`retire_dir`、`guard_build_dir`）本體雜湊前後相同。
- 全量：`uv run pytest -q` 3698 passed／42 deselected；`npm test` 861 passed；`ruff check` 無錯。
- Task L 之後未再送第八輪審核（改動限於前置檢查與報告文字，屬第七輪的 Suggestion）；§14 的正式機演練會再實際執行一次全流程。
