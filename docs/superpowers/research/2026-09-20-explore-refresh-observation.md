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

## 7.4 部署後期中量測（2026-09-21 04:15 UTC，部署後 3 小時）

**需求 vs 消化（使用者最關心的一項）**
- 新增候選：flag 開啟（9/20 16:43 UTC）到 7.4 部署（9/21 01:12）約 8.5 小時累積 74 個；部署後 3 小時只再增 3 個（77）→ **約 1 個／小時**，不是 27／小時（74 個大多發生在前一輪）。
- 消化：每 15 分鐘 15 頁＝60 頁／小時，全部是回補的滿頁（120 權重）。
- 每地址回補頁數（95 個 complete）：mean 1.19、p50 1、p95 2、max 4（`pages_done` 0/1/3 ＋短頁）；`fills_in_window` p50 219、p95 3,375、max 7,992。
- 結論（2026-09-21 使用者校正後）：**收斂趨勢成立，「長期收斂」待暖機後確認**。新增速度（約 1 個／小時）已解除輪動疑慮；但 1.19 頁／地址只取自已完成者，會低估尚未完成的重度帳戶；剩餘約 200 頁是**估計**；容量還要扣除既有地址每 4 小時的增量查詢（暖機後才量得到）。不宣稱「每小時 40 個才到上限」；暖機後以「每小時新增待處理頁 vs 實際消化頁」兩條曲線判定。

**首次服務等待與逾期**（取樣器自 04:14 起記錄每地址首次出現 sync 的時間；此前已開始的 118 個只能給上界 ≤3 小時）
- 基礎類別逾期 p95：state 572 s、portfolio 1,104 s、ledger 1,115 s（皆在各自週期內）；fills 到期 212 個、逾期 p95 41,525 s＝昨日積壓，隨第一輪輪替消化。

**complete 判定抽查（資料正確性）**：取 `fills_in_window` 最高的 3 個 complete 地址，本機以限流器直接向 HL 重抓同一 30 天區間：

| 地址 | HL distinct | 本地 | 差 | 頁數／末頁長度 |
|---|---|---|---|---|
| 0x5b5f798b… | 7,786 | 7,786 | 0 | 4／1,992 |
| 0xe2b92123… | 7,612 | 7,612 | 0 | 4／1,647 |
| 0x7c36139b… | 6,392 | 6,392 | 0 | 4／429 |

三者都是連續滿頁後以短頁收尾、去重後筆數與 HL 完全一致——這證明**同步一致**（本地＝同一 API 同一參數重抓），**不足以單獨證明完整 30 天**（使用者校正）。官方留存上限是 **10,000 筆**；程式用的 8,000 是內部保守門檻（10,000 − 一頁 2,000），先前文件未命名，Task 7.5 起命名為 `RETENTION_SAFETY_THRESHOLD` 並在 reason 碼揭露。目前 complete 判定＝「遍歷到區間末＋筆數 < 8,000」，沒有留存邊界證據，依使用者規則屬**正確性缺陷**→ Task 7.5：加留存邊界探測（查區間起點之前是否仍有可查成交）、記錄查詢窗口與 aggregation 設定、既有 complete 補原因碼。
**人工測試 429 記錄**：2026-09-21 04:08 UTC，來源＝本機開發機（住宅 IP，非正式機出口 IP），原因＝未帶限流器直抓 3 地址各 4 滿頁；不計入「正式服務零 429」統計。此後人工抽查一律經 `WeightLimiter`（本次第二輪即零 429）。

**時間軸校正**：第三次部署與 O-1 發生於 2026-09-20（非 09-21）；7.4 部署 2026-09-21 01:11 UTC；觀測期末 2026-09-22 01:12 UTC（台北 9/22 09:12）。
