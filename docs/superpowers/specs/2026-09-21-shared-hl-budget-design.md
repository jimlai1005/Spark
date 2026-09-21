# 同出口 IP 共用 HL 權重准入：設計文件（Task 7.3，v2）

日期：2026-09-21。狀態：設計 v2（使用者裁決已併入，見 §7），未實作。對應 plan `docs/superpowers/plans/2026-09-20-explore-rate-limit-refactor.md` Task 7.3。

<!-- v2（2026-09-21 使用者裁決）：全域 900 不升 1,100；Explore 300 不動；下單／撤單一律記帳＋准入，撤單與減倉有保留額度且優先於新開倉與背景查詢；
櫃檯故障時本地預算必須有界且不得每個 follower 程序各拿一份完整保底；三個每日排程也走受控 client；驗收至少含多 follower 併發、櫃檯重啟、回覆遺失、故障時 Explore 確實停發。 -->

**官方規格（引用，2026-09-21 讀取 hyperliquid-docs「Rate limits and user limits」）**：
- "REST requests share an aggregated weight limit of 1200 per minute."（同 IP 聚合）
- "All documented `exchange` API requests have a weight of `1 + floor(batch_length / 40)`."——**下單、撤單、修改都有權重**，不是免費。
- info 權重：`l2Book, allMids, clearinghouseState, orderStatus, spotClearinghouseState, exchangeStatus` 為 2；`userRole` 為 60；其餘 20；`userFills, userFillsByTime, historicalOrders, userFunding…` 每回傳 20 筆再加 1。
- 另有位址級限制（每累計交易 1 USDC 得 1 個請求、初始 10,000 緩衝；開放掛單上限 1,000 起）——與本設計的 IP 權重是兩套獨立限制，本設計只處理 IP 權重。

## 0. 問題與目標

filet 正式機一顆出口 IP 上有四個會打 Hyperliquid REST 的行程：`filet-api`（互動請求＋Explore 背景刷新，已有 `WeightLimiter`）、
`filet-follower@*`（實盤引擎，SDK，自己不計帳）、三個每日 timer。HL 限制是每 IP 每分鐘 1,200 權重的聚合值；目前只有 filet-api
計帳（全域 900、留 300 給其他行程），**無法證明總量**，publicapi 零 429 不證明引擎受保護（使用者 2026-09-21 裁決：未結案）。

目標（使用者裁決）：同出口 IP 共用**原子**權重准入；優先序 交易／撤單 → 引擎必要查詢 → dashboard → Explore；Explore 維持子預算；
**通訊故障時引擎不得無上限放行、Explore 不得照跑**；額度不足與通訊故障要分開處理；已扣額度但回覆遺失要冪等。

## 1. 元件與拓撲

```
                 ┌──────────────────────────┐
  filet-api ───▶ │  budgetd（協調器，獨立 unit）│ ◀─── filet-follower@*
  (interactive,  │  WeightLimiter 帳本         │      (engine scope)
   explore/*)    │  unix socket /run/filet/budget.sock │
                 └──────────────────────────┘ ◀─── timers（可選）
```

- **協調器獨立於 filet-api**（`filet-budgetd.service`，與 keysvc 同款的最小 daemon）：網站部署重啟不牽動交易准入；帳本狀態只在記憶體（重啟＝清空視窗，
  最壞情況是重啟後第一分鐘略微超額，由各 client 的本地保底限流兜住，見 §4）。
- 唯一帳本＝現有 `hl_budget.WeightLimiter`（父子 scope）搬進 budgetd；filet-api 內原本的 limiter 降級為**本地保底**（見 §4）。
- Scope 與 cap（v2，使用者裁決）：**全域 900／分鐘**（＝網站＋全部 follower 程序＋三個每日排程的合計；1,200 留 300 當安全餘量，暫不升到 1,100）。
  `engine` 父 scope **保留 300**，底下三個子 scope：`engine_cancel_reduce`（撤單、reduce-only 減倉、平倉；**保留 120**，別人不能用）、
  `engine_open`（新開倉、加倉；父內不另設上限）、`engine_query`（對帳、fills、掛單查詢；上限 150）。`interactive` 200；`explore` 300
  （子 `explore_base` 180／`explore_fills` 120，現況不動）；`timers` 100。**全部 scope 一律記帳＋准入**，沒有任何呼叫免記帳。
  保留＝父子 scope 語義：`engine` 的 300 與 `engine_cancel_reduce` 的 120 是別人不能用的下限，不是上限；`engine_open` 只受父 300 與全域 900 限制。

## 2. 協定（unix socket，JSON 行）

請求：`{"op":"reserve","req_id":"<uuid>","scope":"engine_trade","weight":1,"wait_ms":0}`
回覆：`{"ok":true,"granted":true,"token":"<id>"}` 或 `{"ok":true,"granted":false,"retry_after_ms":850,"reason":"scope_cap|parent_cap|global_cap|paused"}`
或錯誤 `{"ok":false,"error":"..."}`。
其他 op：`settle`（fills 依實際筆數下修）、`note_http`（狀態碼分類）、`note_429`、`snapshot`（ops/health 用）。

- **額度不足**是正常回覆（`granted:false` ＋ `retry_after_ms`），不是故障；client 依 `retry_after_ms` 等或依自己的策略放棄。
- **通訊故障**＝連線失敗、逾時（**50 ms 是 socket 通訊逾時**，不是等額度）、非法回覆。兩者走不同路徑（§4）。
- **冪等**：`req_id` 由 client 生成；budgetd 保留最近 N 秒的 `req_id → 決定` 快取；回覆遺失時 client 用**同一個 req_id** 重送，
  budgetd 回同一決定、**不重複扣額**。client 對「已送出但未收到回覆」的請求一律視為「可能已扣」，重試而非新開 req_id。

## 3. 優先序如何成立

- 準入時不排隊（budgetd 無等待佇列）：先到先得，但**保留**讓高優先序永遠有額度：`engine` 父 scope 300 保留＋`explore` 子預算上限，
  互動請求 200。同一秒同時到達時，engine 因保留不會被 Explore 擠掉；Explore 自己的子預算永遠不會超過 300。
- 引擎內部優先序（v2）：`engine_cancel_reduce`（撤單／減倉／平倉，保留 120）> `engine_open`（新開倉）> `engine_query`（上限 150）。
  優先序靠保留額度成立：撤單與減倉永遠有自己的 120 可用，且可再借父 scope 剩餘；新開倉只能用父 scope 扣掉 120 保留後的餘量；查詢受 150 子上限。
  額度不足時 `engine_cancel_reduce` 依 `retry_after_ms`（通常 <1 秒）等待後重試，等待計入引擎既有 resilience 邊界，**不得繞過准入**。

## 4. 故障情境（使用者要求必須涵蓋）

| 情境 | 引擎 | Explore | 互動請求 |
|---|---|---|---|
| budgetd 正常、額度不足 | `engine_trade` 幾乎不會遇到（有保留）；遇到就依 `retry_after_ms` 等（等待計入既有 resilience 邊界） | 依 `retry_after_ms` 讓位（現況行為） | 依 `retry_after_ms` 最多等 2 秒後回 502 |
| budgetd 通訊故障（連不上／逾時／壞回覆） | **有界本地預算**接管（v2）：每個 follower **程序**的本地 cap ＝ `floor(300 / N_max)`，`N_max`＝該機允許的 follower 程序上限（RUNBOOK §4.0 容量估算；目前設 6 → 每程序 **50／分鐘**），由 unit 環境變數釘死，不是每個程序各拿一份 300；本地 cap 內仍照 `cancel_reduce > open > query` 分配（cancel_reduce 保留本地 cap 的 40%）；超出本地 cap 的呼叫一律等待或放棄（`engine_query` 先讓）；bypass 計數＋告警（TG）。**沒有任何呼叫不記帳。** | **停發**：Explore scheduler 在 budgetd 不可用時不領任何 job（`explore_paused_reason="budgetd_unavailable"`，health 可見、可驗證） | 本地保底 cap 100／分鐘，超過回 502 |
| budgetd 重啟（帳本清空） | 各 client 本地保底仍在；重啟後第一分鐘理論上限＝各 client 本地 cap 合計：引擎 ≤300＋互動 100＋timers 各 20（見下）＋Explore 0（停發）≤ 900 | 同上，停發直到 budgetd 回應 | 同上 |
| 三個每日排程（daily-report／leaderboard／perf-series） | — | — | **同樣經受控 client**（`timers` scope，各自本地保底 20／分鐘），以授權的服務帳號連 socket；不得直接打 HL 成為旁路 |
| 回覆遺失 | 同 `req_id` 重送，冪等 | 同 | 同 |
| HL 回 429（任一 client） | client `note_429` 回報 → budgetd 暫停 `explore` 父 scope（現況語義），`engine` 不暫停但 `engine_query` 自身退避 | 暫停 ≥60 秒 | 不暫停，既有失敗路徑 |

驗收（v2，使用者要求的最低集合；每一項要有自動化測試或有記錄的演練，不得以「零 429」替代）：
1. **多 follower 併發**：N（≥3）個 client 程序同時對 budgetd 發 `reserve`，總准入量在任一 60 秒滑動窗內 ≤ 900、`engine` 合計 ≤ 父上限、`engine_cancel_reduce` 在其他 scope 打滿時仍能拿到 120；測試以真 unix socket＋多程序跑。
2. **櫃檯重啟**：budgetd 中途 kill → client 進本地有界預算（每程序 cap 生效，合計不超 300）→ budgetd 回來後 client 自動接回、帳本從零開始、第一分鐘合計不超 900。
3. **回覆遺失**：client 送出後在收到回覆前斷線 → 用同一 `req_id` 重送 → budgetd 回同一決定、帳本只扣一次（斷言 `snapshot()` 用量）。
4. **故障時 Explore 確實停發**：關 budgetd → `explore_refresh.results` 只剩 `paused`、`explore_paused_reason="budgetd_unavailable"`、`fills_pages_total` 不動、上游零呼叫（fake HL 計數）；budgetd 回來 → 自動恢復。
5. 引擎側 testnet 演練：關 budgetd、殺 budgetd 中途、模擬回覆遺失，交易路徑（撤單／減倉）在本地 cap 內仍可執行且 journal 出現 `budgetd unavailable, local fallback`。

## 5. 引擎側接入（不改交易邏輯）

- `src/spark/exchange/hyperliquid.py` 的 SDK `Info`／`Exchange` 呼叫前加一個 `BudgetClient.reserve(scope, weight)`：`cancel`／reduce-only `order`／平倉用 `engine_cancel_reduce`、
  其他 `order`／`modify` 用 `engine_open`、`clearinghouseState`／`userFillsByTime`／`frontendOpenOrders` 用 `engine_query`；權重表沿 `hl_budget.ENDPOINT_WEIGHTS`，
  exchange 動作依官方 `1 + floor(batch_length/40)`（單筆＝1）。
- 接入點集中在 `HyperliquidAdapter`（單一邊界，工程原則 #5），不散到策略碼。
- 交易路徑（v2）：撤單／減倉**也要記帳與准入**；靠保留額度 120 幾乎不會被拒，被拒時依 `retry_after_ms` 短等重試；本地有界預算下同樣受 cap（cancel_reduce 保留本地 cap 的 40%）。
  不再有「永不阻擋」的路徑。

## 6. 分階段

1. budgetd（daemon＋協定＋冪等快取＋測試，含通訊故障演練）。
2. filet-api 改為 client＋本地保底；Explore 停發機制與 health 揭露。
3. 引擎 client 接入（testnet 演練：關 budgetd、殺 budgetd 中途、模擬回覆遺失）。
4. timers 接入（可選）。
5. 正式機：先部署 budgetd 與 filet-api client（引擎未接入前，`engine` 保留 300 就是現在的 900 上限餘量）；引擎接入需獨立的實盤變更審查。

## 7. 裁決記錄（2026-09-21 使用者）

| 問題 | 裁決 |
|---|---|
| 全域上限 | **900／分鐘**（不升 1,100；1,200 留 300 餘量），為網站＋全部 follower＋三個每日排程合計；Explore 子上限維持 300 |
| 引擎交易是否不擋 | **不同意「完全不擋」**：撤單與減倉有保留額度（120）、優先於新開倉與背景查詢，但全部記帳＋准入；櫃檯故障時本地預算有界，且每個 follower 程序不得各拿一份完整保底（每程序 `floor(300/N_max)`） |
| socket 權限 | 只授權必要服務帳號（SO_PEERCRED 白名單）；三個每日排程也經授權帳號／受控 client 接入，不得成為旁路 |
| 驗收最低集合 | 多 follower 併發、櫃檯重啟、回覆遺失、故障時 Explore 確實停發（§4 驗收 1–4）＋引擎 testnet 演練 |

仍待實測：HL 對 1,200 的計算窗口是否與我方 60 秒滑動一致（部署 budgetd 後以 `snapshot()` 與實際 429 對照）。
