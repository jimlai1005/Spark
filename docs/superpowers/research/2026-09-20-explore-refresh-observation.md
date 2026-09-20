# Explore 背景刷新本機觀測（Task 5.2，2026-09-20）

對應 plan：`docs/superpowers/plans/2026-09-20-explore-rate-limit-refactor.md` Task 5.2。
目的：spec §12 第 4 點——測試通過 ≠ production 驗證；上線前要有一段真實資料流的觀測窗口（工程原則 #6）。

## 設定

- 程式：branch `feat/explore-rate-limit` @ 4d851e5（P2–P5 完成、Task 3.5 修正**之前**）。
- 上游：Hyperliquid 主網唯讀 info 端點（`https://api.hyperliquid.xyz`）＋ stats-data 排行榜，從本機（住宅 IP）發出，與正式機 IP 無關。
- 限流：`WeightLimiter(global_cap=900, scope_caps={"explore": 300})`；scheduler 走 `gateway.scoped("explore")`。
- 池：`ExploreConfig.from_env({})` 預設 300 候選；scheduler 預設週期（state 900s／portfolio 3600s／ledger 3600s／fills 14400s）；publisher `min_interval_s=60`。
- 時長 600 秒，每 30 秒取樣 limiter／scheduler／publisher／index／store。
- Harness：session scratchpad `observe_explore_refresh.py`（不進 repo）；DB 與快照都在 scratchpad，零寫入 repo。

## 結果（10 分鐘）

| 指標 | 值 | 判讀 |
|---|---|---|
| HTTP 429 | **0** | 事故根因已消除：整段沒有任何一次撞到 HL 額度 |
| explore 60 秒視窗用量（20 個取樣） | 92→300，多數在 250–300，最大 **300** | 子預算被頂滿但**沒有超過**；限流是主動的，不是靠 429 |
| `no_budget` tick | 387 / 838 | scheduler 在額度用盡時讓位（sleep 1s 再試），行為正確 |
| 預留權重 / 結算退回 | 3,142 / 574 | fills 頁依實際筆數結算生效（reviewer C1 修法有效） |
| 上游成功 | state 191、portfolio 39、ledger 39、fills 10 頁、candidates 1 | 優先級 0 的 state 先跑（2 權重），20 權重的 portfolio／ledger 各 39 |
| endpoint_cache 列 | 10→269 | 線性成長，無停滯 |
| fills | 8,432 筆，9 個地址有 sync，7 complete／2 backfilling | 留存判準與單頁續抓在真資料上運作 |
| publisher | 10 次發布、0 失敗 | 每分鐘一次，符合 spec §9.2 |
| index | rows 300、v4、`published_at` 前進 | |
| `query()` | `total_qualified: 1`、`coverage_counts {backfilling: 293, complete: 7}` | ⚠️ 見下 |
| scheduler thread | 600 秒內無例外、`oldest_due_age_s` 0.29s | 沒有 job 飢餓 |
| harness.log | 60 行，全部是既有 `leader_perf` 的「portfolio 窗 r<-1 判無效」warning | 與本次改動無關，舊行為 |

## 發現

1. **第一次發布會端出幾乎空的榜單**（reviewer C2 的實證）：portfolio 每 10 分鐘只抓到約 39 個，
   `qualify` 需要 `live_days`（來自 portfolio），所以 300 列只有 1 列合格。若正式機直接開 flag，
   探索頁會顯示「0 筆符合」直到 portfolio 覆蓋率夠高。Task 3.5 加了換版門檻（80% 候選有 portfolio）
   與 v3 快照備份。
2. **冷啟動時間比 plan 估的長**：plan 用「6,600 權重 ÷ 300 ≈ 22 分鐘」算 state＋portfolio；實測
   portfolio 39 個／10 分鐘（state、ledger、fills 都在分同一個 300），推估 **300 個 portfolio 約 60–80 分鐘**
   才能全部到位，第一次換版（門檻 80%）約 50–65 分鐘。RUNBOOK §5.8e 要以此為準。
3. 預算分配：state 是 priority 0 且只要 2 權重，所以先被掃完；portfolio／ledger 同為 priority 1 平分；
   fills（priority 2/3）在前 10 分鐘只拿到 10 頁。這符合 spec §6「優先基礎資料」，但意味 fills 完整性
   在冷啟動後數小時內多為 backfilling——前端以 `fills_coverage` 判斷，不會誤標。

## 極限與未涵蓋

- 本機 IP ≠ 正式機 IP；正式機還有 follower 引擎與三個 timer 共用額度（不經限流，全域 900 留 300）。
- 只跑 10 分鐘，未觀測到 portfolio 週期（3600s）到期後的第二輪，也未觀測候選進出。
- 未觀測 `ScopePaused`／429 路徑（因為沒發生）；該路徑只有單元測試覆蓋。
- 觀測用的是 Task 3.5 之前的程式；3.5 落地後應以 5 分鐘短跑確認門檻行為（預期：無舊版本時照常發布；
  有舊版本時 `gate_skips` 遞增直到 80%）。

## 補跑：Task 3.5 之後、以正式機 v3 快照為種子的 5 分鐘短跑（同日）

程式 3c70459；種子＝正式機 `/var/lib/filet-api/explore_index.json`（v3，299 列，2026-09-19T15:00）。

| 指標 | 值 | 判讀 |
|---|---|---|
| 429 | 0；explore 視窗最高 286 | 同上，限流主動生效 |
| 上游 | state 96、portfolio 22、ledger 22、fills 1 頁、`no_budget` 163 | 與 10 分鐘跑同節奏 |
| publisher | `publishes 0`、`gate_skips 5`、`last_gate "20/300"`、`failures 0` | **門檻擋住空榜**：20/300 未達 80%，五次都不換版 |
| index | rows 299、`built_at`＝種子的 built_at、`published_at` 同 | 讀路徑端出遷移後的 v3 榜單，`total_qualified 19` 與正式機現況一致 |
| 磁碟快照 | `version 3`、mtime 未變 | 未被覆寫；`.v3.bak` 尚未產生（只在第一次真正覆寫前才備份，符合設計） |
| explore.db | mode 0600 | W5 修法生效 |

結論不變；第二次部署開 flag 後的預期畫面：探索頁維持 9/19 的榜單、`gate_skips` 每分鐘 +1、
`last_gate` 分子逐步上升，約 50–65 分鐘後第一次換版並產生 `.v3.bak`。

## 結論

可進入第二次部署，條件：Task 3.5 完成並複審通過；RUNBOOK §5.8e 冷啟動時間改為 60–80 分鐘；
開 flag 前備份 `explore_index.json`；開 flag 後前 90 分鐘用 `/api/ops/health` 看 `explore_refresh.results`、
`explore_publisher.gate_skips`／`last_published_at`、`explore_index.built_at`，任何一項不動即視為 unhealthy。
