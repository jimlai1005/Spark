# Filet M1：Copytrade Orchestrator 規格（v3，引擎移植定案）

> 來源：使用者 2026-07-15 交付的 orchestrator prompt v3。
> v3 說明：方向性變動定案（2026-07-15）。hl-copytrader 上有線上產品，**全程唯讀、一行不改**；
> 改為把已驗證過的引擎邏輯**移植（port，非 import）**進 spark。v2 的 Option A 作廢。

## 已拍板

1. **Repo 策略 = Option B**：引擎移植進 spark；hl-copytrader 唯讀（僅允許 test-only import 純函式做交叉比對，任何寫入/修改禁止）。
2. **回撤觸發自動 reduce-only 全平：旗標預設開**。
3. **Dogfood 資金：1000 USDC**；follower 用新錢包，與線上部署、gridbot 錢包分離。
4. **M1 leader：`0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1`**（即 hl-copytrader 線上跟的同一個網格交易員——使 shadow 對照成為可能）。

**後擋板（保留一項）**：modify 若丟失 builder 歸屬時的政策（容忍漏繳 vs 擋掉改強制 cancel+place）——等 T1.3 testnet 數據，**交使用者，不得自行決定**。

## 為什麼是 port 不是 import

hl-copytrader 的模組經 import 有副作用（`config.py` 載入時讀自己的 `.env`）。且 orders/sync/trader 與其 config 和 Telegram 耦合。直接 import 會讓 spark 的行為被另一個專案的環境隱性控制。移植進 spark 的 Decimal / adapter ABC 架構才是 Filet 長期沉澱的資產，不是繞路。

**工時**：~3–3.5 週（每週 15–20h）。比 v2 的 Option A 多 ~1 週，買到：線上產品零風險、M2 多 follower 的自然沉澱、長期架構統一。

## 背景

Filet M1：在 spark 內建成 copytrade orchestrator，主網跟單自己的錢包（leader = `0xf97ad…`，follower = 全新帳戶，1000 USDC），每筆訂單經 builder code（f=20）路由，量測 tracking error 與 fee accrual，作為 M2 closed alpha 的 go/no-go。spark 已有：onboarding 狀態機、BuilderCode 下單（Ioc）、Keychain keystore、accrual/對帳驗證層、交易所 adapter ABC。hl-copytrader 是引擎邏輯的參考實作（美股類別 + 部位安全網，主網長期運轉驗證過）。

## 紅線（違反即回退；⭐ = review 必檢）

1. ⭐ `hl-copytrader/` 唯讀。任何對其檔案的寫入、重構、格式化都是錯誤。
2. ⭐ agent key 不得具備提款權；ApproveBuilderFee 只得主錢包簽；`ExchangeAdapter` ABC 維持不含 withdraw/transfer。
3. ⭐ **每一筆會產生掛單的寫入（place / marketable open / reduce-only close）都必須帶 builder 參數**，注入點集中在 adapter 層；離線測試 mock SDK、assert 全部下單路徑 kwargs 含 builder，進 CI。
4. 不讀取或印出任何 `.env*`；key 不得出現在 log / repr / 例外訊息（沿 `TxResult.agent_key` 慣例）。
5. `LIVE_TRADING=false` 預設；主網開啟是人工動作。
6. M1 只跟 crypto（不碰 xyz / builder-deployed DEX）；xyz 的 builder accrual 屬 T4.2 獨立驗證項，在那之前不解禁。
7. 內部一律 Decimal；float 轉換只發生在 adapter 與 SDK 的邊界（沿 `_round_px` 慣例）。

## Phase 1 — Adapter 補齊與 resilience 移植（W1）

### T1.1 ⭐ ExchangeAdapter 補齊（Sonnet）
Reads 補齊：open orders、positions/clearinghouseState、帳戶權益（unified，含 spot）、fills（by time）、mid price。Writes 補齊：cancel、modify、reduce-only IoC close、update_leverage、marketable open——**order-creating writes 全帶 builder**（紅線 3）。`HyperliquidAdapter` 對應實作 + 離線測試。

### T1.2 ⭐ Resilience 邊界移植（Sonnet）
`hl-copytrader/src/resilience.py` 幾乎零耦合，照原樣移植做為 spark adapter 的寫入語感。語意 1:1 保留：冪等/reduce-only 直接重試；非冪等 verify-then-retry、查不出來偏向「當已送達」——寧漏跟不重複下單。移植其測試。

### T1.3 ⭐ modify 的 builder 歸屬驗證（Sonnet + testnet 實測）
引擎主動路徑是 modify-first——若 modify 後訂單丟失 builder 歸屬，主動路徑 fee 全在漏繳且帳面無感。testnet：place（帶 builder）→ modify → 成交 → `wait_for_accrual`（帶 baseline）驗增量。產出數據報告 + 政策選項，**交使用者，不得自行決定**。

## Phase 2 — 引擎移植（W1–W2）

### T2.1 ⭐ 對帳引擎（Sonnet）
移植 `_orders_match` / `_slot_key` / `_plan` / `_reconcile_orders` 語意：完全相符保留 → 同 slot modify 就地改（失敗降級 cancel+place，含 fail-TTL）→ 先取消釋放保證金 → 後補新單 → settle 後驗證。重試一次、仍不符告警。通知改為 notifier 介面注入，不得硬編。**Parity 測試**：以 hl-copytrader 既有離線測試為藍本移植 characterization tests；純函式（`_plan`、`_orders_match`）可 test-only import 做模組交叉比對輸出。

### T2.2 Sizing 與部位安全網（Sonnet）
`compute_scale_factor`（權益 × 使用率 × 權重 / leader 淨值；MAX_TARGET_LEVERAGE 超標等比縮放）與 `sync_positions` 三分支（新開/調整/趨平）移植。波動抑制槓桿（weight）一併移植。抗單預設卻用；持倉保護（protection）移植但旗標預設關（私募預設）。MIN_ORDER_NOTIONAL 過濾保留，但**被跳過的量要進遙測**（見 T3.3）。

### T2.3 主迴圈與 CLI（Haiku，規格鎖死）
Crypto 固定頻率照用（預設每分鐘），**不移植美股時段邏輯**。CLI 對齊既有慣例：`--dry-run` / `--once` / `--status`，外加 `--shadow`（見 T4.1）。連續錯誤斷路器邏輯移植。

## Phase 3 — 風控與遙測（W2–W3）

### T3.1 ⭐ Kill switch（Sonnet）
回撤超限（權益 vs 高點，MAX_DRAWDOWN_PCT）→ 取消全部 resting → reduce-only 全平（**旗標預設開，已拍板**）→ 告警 → 人工 re-arm（檔案旗標），絕不自動恢復。獨立 `scripts/panic.py` 可手動執行同等動作。flatten 失敗 = 告警升級，不得靜默重試。

### T3.2 Notifier（Haiku）
移植 telegram.py 精簡版：事件分級 info / warn / critical，可靜音類別。

### T3.3 TE + fee 日報（Haiku，規格鎖死）
按批次：leader fills vs 我方 fills 配對 → 頂層延遲、滑價 bp（鏡像掛單成交 vs safety net 吃價）、safety net 成交量占比。**skipped-small 占比**（1000 USDC 對格網 leader 的縮放會變出大量 rung < $10 名目被跳過，屬固有失真，必須量化以免誤判引擎）。fee accrual 日報重用 `spark.verification.reconcile` + `query_builder_accrued`。全部進 Telegram。

## Phase 4 — 驗證與 dogfood（W3–W3.5）

### T4.1 Shadow 對照（移植正確性的主判定）
`--shadow` 模式：spark dry-run 對 leader `0xf97ad` 產出「意圖動作清單」，與線上 hl-copytrader 的實際動作（其 log）逐輪 diff。差異分類：可解釋（權益/權重/容忍度參數差）vs 不可解釋（邏輯錯誤）。**連續 3 個交易日不可解釋差異 = 0 才准進 T4.3。**

### T4.2 Testnet E2E
Onboard testnet follower（重用 `spark.onboarding.onboard`）→ 跑單 → `wait_for_accrual` 判定；覆蓋 place 與 modify 兩路徑。xyz accrual 獨立驗證亦在此。

### T4.3 主網 dogfood
1000 USDC、新錢包、runbook（注資 → onboarding → LIVE_TRADING 開啟 → 回滾）、kill switch 實彈演練（含故意觸發 flatten 失敗驗證告警升級）。

## M1 驗收 gate

| 指標 | 門檻 |
|---|---|
| Shadow 對照 | 連續 3 交易日不可解釋差異 = 0 |
| Builder fee | 主網 100% fills 有 accrual（place 與 modify 分開驗） |
| Safety net 吃價滑價 | median ≤ 10bp（majors） |
| Safety net 占比 | < 30% 總成交量 |
| Skipped-small | 占比已量化並寫入日報（數字本身不設限，屬已知失真） |
| 對帳 | sync_failed 持續告警 = 0；日對帳 drift = 0 |
| 安全 | kill switch 演練成功；全程 agent key 無提款權 |

## 使用方式（工作模式備忘）

在 `~/projects` 開 Claude Code，主要工作 repo 是 `spark/`（python 3.11，uv）。餵 brainstorm → write-plan → execute-plan。模型分工沿用慣例：規劃 Opus；對帳/簽章/併發 Sonnet；鎖死規格的樣板 Haiku；review gate Sonnet 以上。⭐ = review 必檢。
