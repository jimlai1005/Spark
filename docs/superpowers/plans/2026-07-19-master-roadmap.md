# 主計畫表：部署 → 營運後台 → Billing → 多 leader → slider

日期：2026-07-19 起｜授權：使用者已全權授權自主執行（多日連續，遇 token 限額暫停後自行喚醒續作）
**本檔是長時執行的錨點**——context 壓縮後由此恢復進度。

## 使用者已拍板

| 項目 | 裁決 |
|---|---|
| 定價結構 | **方案 B**（免費層＋月費）。付費解鎖「多 leader」「比例 slider」 |
| 免費層限制 | **A3：免費版不受限制**。Billing UI 先推出，多 leader/slider 開發完成後再開啟其頁面 |
| 營運儀表板 | 放 `/ops` 同一個 Next.js app（非獨立應用） |
| 部署 | Lightsail 已開：`ssh -i ~/Downloads/LightsailDefaultKey-ap-northeast-1-spark.pem ubuntu@52.197.137.3`（Ubuntu 22.04.5 / 2 vCPU / 2GB / 58GB，東京） |

## 執行紀律（每個 Phase 都適用）

- 逐 task 派 subagent（機械照抄→haiku，需理解→sonnet），指令寫到「不需推理」的程度
- **指令必含**：「照抄後測試失敗就回報，**絕不修改斷言遷就**」——這條是本專案兩次抓到假完成的關鍵防線
- 每 task 完成後**指揮官親自檢查**（不只看測試綠：逐項對規格、必要時變異測試）
- ⭐ 碰錢／碰安全的 task 加 opus 對抗性審查
- 一次只有一個 agent 在 commit（避免 git race）；review 唯讀可併行
- 每個 Phase 結束：全套測試＋lint＋實機驗證＋更新本檔進度

---

## Phase 0：部署前置與上線 【進行中】

- [x] P0.1 Next.js CVE-2025-29927 bump（部署前必做）
- [ ] P0.2 部署到 Lightsail（照 `deploy/RUNBOOK.md`）
- [ ] P0.3 實機驗收：filet-api 讀不到 keys、SO_PEERCRED 生效、外網只走 nginx
- [ ] P0.4 端到端煙霧測試（瀏覽器連上、SIWE 登入、看得到頁面）

**已知風險：TLS 需要網域**。session cookie 是 `secure=True`，純 http（IP）登入會失敗。
處置順序：① 試 `52-197-137-3.sslip.io` 之類的 wildcard DNS ＋ Let's Encrypt（真實憑證）
② 失敗則自簽憑證（瀏覽器警告但功能可用）③ 兩者皆不可行 → **叫醒使用者**要網域。

---

## Phase 1：ops 後端 ＋ 客戶損益 ＋ 收入對帳

- [ ] P1.1 `/api/ops/*` admin-gated 端點骨架 ⭐（跨客戶聚合，資安新存取模式；結構性測試：非 admin 一律 403）
- [ ] P1.2 客戶損益聚合（per-follower 路由量／實收 builder fee／本金／訂閱狀態／淨貢獻）
- [ ] P1.3 收入對帳（應收 Σ成交×f vs 實收鏈上 accrued，差額門檻告警）
- [ ] P1.4 `/ops` 前端頁（admin 白名單、表格優先於圖表）

**為何先做**：不依賴定價與律師；客戶損益數據**直接餵未來的定價微調**。

---

## Phase 2：Billing UI 全套 ＋ 訂閱對帳

- [ ] P2.1 `GET /api/billing/plans`（方案定義走設定，改設定即改頁面）
- [ ] P2.2 `POST /api/billing/portal`（Stripe Customer Portal，取消訂閱交給 Stripe）
- [ ] P2.3 `/pricing` 方案選擇頁（免費層**不受限制**；付費功能標「開發中」直到 Phase 3/4 完成）
- [ ] P2.4 `/billing` 訂閱管理頁 ＋ Header 訂閱狀態 chip
- [ ] P2.5 checkout 流程接線（既有 `POST /api/billing/checkout`）
- [ ] P2.6 訂閱對帳（Stripe `Subscription.list` vs 本地 billing 表，抓 webhook 掉包漂移）→ **補上 opus 點名的 reconcile 缺口**

**旗標**：billing env 未設時後端 501、前端整組隱藏——可安全合併主線。

---

## Phase 3：多 leader（付費功能一）

- [ ] P3.1 資料模型 ⭐：`FollowerRef` 加 leader 欄位；manifest／activate CLI 支援（**破壞性變更，需向後相容既有 manifest**）
- [ ] P3.2 引擎讀 per-follower leader（取代 `COPY_LEADER_ADDRESS` env 單一來源）⭐
- [ ] P3.3 leader 目錄 API（可選清單＋各自的 leaderboard 統計，資料來自已在跑的 watchlist snapshot）
- [ ] P3.4 客戶選 leader 的 API（訂閱狀態 gate：免費層單一 leader、付費多 leader）
- [ ] P3.5 `/leaders` 前端頁

**注意**：這是整份計畫裡最大的架構變更，且碰引擎核心。P3.1/P3.2 必須 opus 審。

---

## Phase 4：比例 slider（付費功能二）

- [ ] P4.1 per-follower 資金設定持久化（`allocated_capital`／`capital_utilization`）
- [ ] P4.2 設定 API ＋「下一 cycle 生效」語意（**不做即時強制再平衡**，避免無謂 taker 成本）
- [ ] P4.3 引擎讀取 per-follower 設定 ⭐
- [ ] P4.4 slider UI（含「下一輪生效」的明確提示）

---

## Phase 5：付費功能開啟 ＋ 交易品質儀表板

- [ ] P5.1 `/pricing` 的付費功能從「開發中」改為可用；訂閱 gate 實際生效
- [ ] P5.2 交易品質聚合（TE／滑價／延遲／skipped，日報已有雛形）
- [ ] P5.3 系統健康面板（follower 進程、kill switch、樣本覆蓋度、快照時間、告警數）

---

## 需要叫醒使用者的情況（其餘一律自行決定）

1. 部署需要網域而 wildcard DNS 方案都失敗
2. 任何需要動主網／真錢的操作
3. 發現的問題需要改變已拍板的產品決策（定價結構、免費層政策）
4. 律師回覆需要調整架構
5. 連續兩輪修復同一問題仍失敗（依 judgment.md 的換路訊號）

## 進度紀錄

- 2026-07-19 ~05:0x：計畫建立；SSH 驗證通過；P0.1 派工中
