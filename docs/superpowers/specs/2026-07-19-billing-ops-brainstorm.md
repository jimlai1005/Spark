# Billing UI + 營運後台 規劃（brainstorm，待使用者過目）

日期：2026-07-19｜狀態：**待裁決，尚未開工**
使用者已定：對帳四塊都要，優先序 **客戶損益 > 收入對帳 > 訂閱對帳 > 交易品質**；營運後台**另做獨立儀表板**；Billing UI **連方案選擇頁一起做（假設方案 B）**。

---

## 0. 開工前必須先解決的問題：方案 B 的付費內容不存在

**現況查證**：方案 B 是「免費跟單（我方賺 builder fee）＋ 付費解鎖多 leader / 比例 slider」。但——

- **多 leader 不存在**：`src/spark/filet/followers.py` 的 `FollowerRef` 沒有 leader 欄位；leader 是引擎的環境變數 `COPY_LEADER_ADDRESS`，一個引擎一個 leader。要做「客戶選 leader」需改資料模型＋新 API＋UI。
- **比例 slider 不存在**：`allocated_capital` / `capital_utilization` 只在 `CopySettings`，`publicapi` 無任何相關端點。

照原規劃做方案頁，等於**列出兩個不存在的付費功能**。

### 三個選項

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| **A1** | 方案頁的功能清單走**設定檔驅動**，先填「即將推出」 | 工程量最小 | 賣未來功能，對 alpha 用戶不誠實 |
| **A2 ⭐推薦** | 免費層改為**限制跟單本金上限**（如 $500），付費解鎖更高／不限 | **今天就能實作**（`allocated_capital` 已存在，只需 per-follower 化）；升級誘因直接；與北極星對齊（本金↑→路由量↑→builder fee↑） | 需在 manifest／activate 加 per-follower 上限欄位 |
| **A3** | 延後 Billing UI 到付費功能存在 | 最誠實 | 你要等 M3 主體做完才有訂閱線 |

**建議 A2**：它讓方案 B **今天就有真實可交付的差異**，不必等多 leader。免費層仍替你賺 builder fee（本金受限故較少），付費解除限制。實作＝ manifest 加 `capital_cap` 欄位 → 引擎讀取 → 訂閱狀態決定生效值。

---

## 1. Billing UI（客戶端）

**前提**：後端 API 已存在（checkout / status / webhook，測試模式）。前端目前**零 billing 介面**。

### 頁面與元件
1. **`/pricing` 方案選擇頁**：免費層 vs 付費層對照卡；功能與限制由**後端設定驅動**（`GET /api/billing/plans` 新端點回方案定義），改價改功能不用改前端。
2. **`/billing` 訂閱管理頁**：目前方案、狀態（active/past_due/canceled/none）、下次扣款日、管理訂閱（導向 Stripe Customer Portal）。
3. **Header 訂閱狀態 chip**：登入後顯示目前方案（免費／付費），點擊進 `/billing`。
4. **Checkout 流程**：`/pricing` 的付費按鈕 → 呼叫既有 `POST /api/billing/checkout` → 導向 Stripe → 回跳 `/billing?success=1`。

### 未設定時的行為（重要）
billing 三個 env 未設時後端回 **501**。前端據此**整組隱藏**（不顯示 pricing/billing 入口、不顯示 chip）——所以現在合併進主線不會影響任何東西，你填了 Stripe 設定就自動出現。

### 新增後端
- `GET /api/billing/plans`（公開）：回方案定義（名稱、價格、功能清單、限制），供前端渲染。定義來源＝設定檔，你改設定即改頁面。
- `POST /api/billing/portal`（需 session）：建 Stripe Customer Portal session，回 URL（讓客戶自行管理／取消訂閱——避免我們自建取消流程）。

---

## 2. 營運儀表板（獨立，管理端）

### 架構決策
- **位置**：同一個 Next.js app 的 `/ops` 路由（**不另建 app**——避免新增部署面與第二套認證）。
- **權限**：沿用 `cfg.admin_addresses` 白名單，與 `/api/admin/pending` 同一道結構性閘。
- **新端點**：`/api/ops/*`，全部 admin-only。
- ⚠️ **資安要點**：這些端點**跨客戶聚合資料**，是全新的存取模式。必須有結構性測試：非 admin 一律 403、且 `/api/ops/*` 不得出現在任何非 admin 可達路徑。

### 四塊對帳（依使用者指定優先序）

#### ① 客戶損益（優先 1）——「每個客戶賺我多少 / 花我多少」
| 欄位 | 來源 |
|---|---|
| 30 日路由量（notional） | `get_user_fills` per follower 加總 |
| 產生的 builder fee | fills 的 `builderFee` 加總（**實收**，非估算） |
| 目前本金 / 部位 | `clearinghouseState` |
| 訂閱狀態 | 本地 billing 表 |
| 資源占用 | follower 進程數（每個約 100-150MB）|
| **淨貢獻** | builder fee ＋ 訂閱費 − 分攤 infra 成本 |

**這塊直接餵你的定價決策**——A/B/C 哪個結構划算，看這張表就知道。

#### ② 收入對帳（優先 2）——「應收 vs 實收」
- **應收** = Σ(每筆成交 notional × f=20/100000)
- **實收** = 鏈上 `query_builder_accrued(builder)` 的期間增量
- **差額告警**：超過門檻（如 1%）即紅字＋原因候選（modify 路徑、非我方路由的成交、鏈上延遲）
- 日報 `copytrade_daily_report.py` 已有單 follower 雛形，本項是**跨 follower 聚合版**

#### ③ 訂閱對帳（優先 3）——「Stripe vs 本地 DB」
- 呼叫 Stripe `Subscription.list` 取真實狀態，與本地 billing 表逐筆比對
- 列出漂移：本地 active 但 Stripe 已取消（**收不到錢還在服務**）／本地無記錄但 Stripe 有（webhook 掉包）
- **這正是 opus 審查點名、目前完全沒有的 reconcile 缺口**
- 提供「以 Stripe 為準修正本地」的動作按鈕（需二次確認）

#### ④ 交易品質（優先 4）——「跟得準不準」
- 每 follower 的 TE（配對延遲中位數）、滑價、taker 佔比、skipped 小額比例
- 日報已算出這些，本項是聚合＋趨勢圖
- 用途：判斷 leader 好不好跟、引擎跑得好不好

### 系統健康（附加，低成本）
follower 進程存活、kill switch 狀態、**樣本覆蓋度**（我們今天才加的 C1 指標）、上次快照時間、告警數。

---

## 3. 分期建議

| 期 | 內容 | 理由 |
|---|---|---|
| **P1** | ops 後端 API（admin-gated）＋ 客戶損益 ＋ 收入對帳 | 你的優先 1、2；且**客戶損益直接餵定價決策**，越早有數據越好 |
| **P2** | Billing UI 全套（pricing/billing/chip/checkout/portal）＋ 訂閱對帳 | 需先定 A1/A2/A3；旗標關閉故可安全合併 |
| **P3** | 交易品質儀表板 ＋ 系統健康 | 優先度最低，且日報已可暫代 |

**P1 可以現在就開工**（不依賴定價拍板、不依賴律師）。P2 需要你先裁決第 0 節的 A1/A2/A3。

---

## 4. 待使用者裁決

1. **第 0 節：方案 B 的付費內容選 A1 / A2 / A3？**（建議 A2：免費層限本金上限）
2. 營運儀表板放 `/ops` 同 app（建議）還是堅持完全獨立的第二個應用？
3. P1 現在開工可以嗎？（不需等定價與律師）
