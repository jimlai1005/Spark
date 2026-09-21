# 同出口 IP 共用 HL 權重准入：設計文件（Task 7.3，草案 v1）

日期：2026-09-21。狀態：設計，未實作。對應 plan `docs/superpowers/plans/2026-09-20-explore-rate-limit-refactor.md` Task 7.3。

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
- Scope 與 cap（起始值）：全域 1,200 − 安全餘量 100 ＝ **1,100**；`engine` 父 scope **保留 300**（子：`engine_trade` 無上限於父內、
  `engine_query`）；`interactive` 200；`explore` 300（子 `explore_base` 180／`explore_fills` 120，現況）；`timers` 100。保留＝父子 scope 語義：
  `engine` 的 300 是別人不能用的下限，不是引擎的上限（引擎超過 300 時只受 1,100 全域限制，但那時 Explore 已被壓到自己的子預算內）。

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
- 引擎內部：`engine_trade`（下單／撤單／平倉）不設子上限，`engine_query`（對帳、fills 查詢）設子上限 150，避免對帳把交易的保留吃掉。

## 4. 故障情境（使用者要求必須涵蓋）

| 情境 | 引擎 | Explore | 互動請求 |
|---|---|---|---|
| budgetd 正常、額度不足 | `engine_trade` 幾乎不會遇到（有保留）；遇到就依 `retry_after_ms` 等（等待計入既有 resilience 邊界） | 依 `retry_after_ms` 讓位（現況行為） | 依 `retry_after_ms` 最多等 2 秒後回 502 |
| budgetd 通訊故障（連不上／逾時／壞回覆） | **本地保底限流器**接管：`engine` 本地 cap **150／分鐘**（比保留的 300 更保守），`engine_trade` 永不被本地限流擋（交易不可被自己擋）、`engine_query` 受本地 cap；bypass 計數＋告警（TG） | **停發**：Explore scheduler 在 budgetd 不可用時不領任何 job（`explore_paused_reason="budgetd_unavailable"`，health 可見、可驗證） | 本地保底 cap 100／分鐘，超過回 502 |
| budgetd 重啟（帳本清空） | 各 client 本地保底仍在；重啟後第一分鐘理論上限＝各 client 本地 cap 合計 ≤ 1,200 | 同上，停發直到 budgetd 回應 | 同上 |
| 回覆遺失 | 同 `req_id` 重送，冪等 | 同 | 同 |
| HL 回 429（任一 client） | client `note_429` 回報 → budgetd 暫停 `explore` 父 scope（現況語義），`engine` 不暫停但 `engine_query` 自身退避 | 暫停 ≥60 秒 | 不暫停，既有失敗路徑 |

驗證方式（每一格都要有測試或演練）：關掉 budgetd → Explore 的 `explore_refresh.results` 只剩 `paused`、`hl_budget` 快照為 null、
引擎 journal 出現 `budgetd unavailable, local fallback` 且交易路徑不受影響（testnet 演練）。

## 5. 引擎側接入（不改交易邏輯）

- `src/spark/exchange/hyperliquid.py` 的 SDK `Info`／`Exchange` 呼叫前加一個 `BudgetClient.reserve(scope, weight)`：`post`／`order`／`cancel` 用 `engine_trade`、
  `clearinghouseState`／`userFillsByTime`／`frontendOpenOrders` 用 `engine_query`；權重表沿 `hl_budget.ENDPOINT_WEIGHTS`（exchange 動作權重依官方表另補）。
- 接入點集中在 `HyperliquidAdapter`（單一邊界，工程原則 #5），不散到策略碼。
- 交易路徑的失敗語義不變：`reserve` 對 `engine_trade` 永遠不阻擋（只計帳），本地保底也不擋交易。

## 6. 分階段

1. budgetd（daemon＋協定＋冪等快取＋測試，含通訊故障演練）。
2. filet-api 改為 client＋本地保底；Explore 停發機制與 health 揭露。
3. 引擎 client 接入（testnet 演練：關 budgetd、殺 budgetd 中途、模擬回覆遺失）。
4. timers 接入（可選）。
5. 正式機：先部署 budgetd 與 filet-api client（引擎未接入前，`engine` 保留 300 就是現在的 900 上限餘量）；引擎接入需獨立的實盤變更審查。

## 7. 未決事項（需使用者裁決）

- 全域安全餘量 100 是否足夠（HL 對 1,200 的計算窗口是否與我方 60 秒滑動一致，需實測）。
- `engine_trade` 是否真的完全不擋（風險：引擎失控迴圈也不被限流）——建議加「單分鐘 >500 權重就告警」而非硬擋。
- budgetd 與 keysvc 同機同 unit 風格；是否需要 socket 權限只給 filet-api／filet-engine 兩個 user（同 keysvc 的 SO_PEERCRED 白名單）。
