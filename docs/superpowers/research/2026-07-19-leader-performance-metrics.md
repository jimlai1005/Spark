# Leader 績效指標：如何算出不被出入金污染的數字（2026-07-19）

**研究問題**：在 Hyperliquid 上，如何為一個 leader 位址計算可信的績效指標（報酬率／回撤），且不被出入金污染？
**動機**：`/leaders` 頁要顯示策劃清單的績效；數字錯了 = 客戶照捏造報酬率投真錢。
**方法**：SDK 原始碼（`.venv` hyperliquid-python-sdk **0.24.0**）＋官方 GitBook ＋本 repo 既有實測紀錄。**全程未發出任何真實 HL API 請求**（依紅線）。

---

## 結論總覽

1. **HL 已經提供乾淨的資料——`portfolio()` 的 `pnlHistory` 官方定義就是「已扣除出入金」**：官方文件明文 `Pnl is defined as account value plus net deposits, i.e. account value + deposits - withdrawals`，且序列**在出入金當下額外取樣**。第 1 題到此結束，不必自建出入金扣除管線。
2. **但「乾淨的 PnL（美元）」不等於「乾淨的報酬率（%）」**。分母（資本）會隨出入金變動，仍須自建 TWR。好消息：`accountValueHistory` 與 `pnlHistory` **同一次回應、同一組時間戳**，兩者相減即得每區間的淨外部現金流，**不需要第二個端點**——這正好滿足工程原則 1（同源、同基準、同處計算）。
3. **最大的真實風險不是出入金，是 basis**：本 repo 已實測 `portfolio()` 的 `day/week/month/allTime` 窗 = **spot + perp 總和**，而 copytrade 只鏡像 perp。用總值算出來的績效**不是客戶能複製的績效**。必須改用 `perpDay/perpWeek/perpMonth/perpAllTime` 窗。

---

## Q1. HL API 是否直接提供乾淨的績效資料？→ **是（決定性證據）**

### 1a. `portfolio()` — ✅ 已扣除出入金，這是主力資料源

**SDK**：`Info.portfolio(user)` 存在於 0.24.0，`.venv/lib/python3.14/site-packages/hyperliquid/info.py:671-683`，送 `{"type": "portfolio", "user": address}` 到 `POST /info`。
**官方文件收錄**：`portfolio` 在 GitBook info-endpoint 的請求型別清單內（已文件化，非未公開端點）。

**決定性引文**（官方 GitBook,「Portfolio graphs」）：

> Pnl is defined as `account value` plus net deposits, i.e. `account value + deposits - withdrawals`.
> Account value includes unrealized pnl from cross and isolated margin positions, as well as vault balances.
> （取樣）on deposits and withdrawals and also every 15 minutes
> not recommended to precise accounting purposes, as the interpolation between samples may not reflect the actual change in unrealized pnl

三個推論：
- **`pnlHistory` 是出入金中性的累積 PnL 曲線**。入金 1 萬鎂會讓 `accountValueHistory` 跳 +10000，但 `pnlHistory` **不動**。這正是我們要的。
- **funding 與手續費自動計入**（見 Q2c 推導）。
- **出入金當下會額外插一個取樣點** → 現金流被隔離在取樣邊界上，這使 TWR 的分段近似誤差極小（見 Q2a）。

**回應形狀**（已於本 repo 兩處落地實作，非憑印象）：
`[[period, {accountValueHistory: [[ts_ms, val_str], ...], pnlHistory: [[ts_ms, cum_pnl_str], ...], vlm: str}], ...]`
8 個 period：`day/week/month/allTime` ＋ `perpDay/perpWeek/perpMonth/perpAllTime`。
- 既有實作：`src/spark/exchange/hyperliquid.py:148-177`（`get_equity_view`）、`:211-234`（`get_daily_abs_pnl`，已在做 `pnlHistory` 差分）
- 既有 fixture：`tests/test_hyperliquid_reads.py:251-254`、`tests/test_copy_executor.py:292-293`

⚠️ **`pnlHistory` 是「累積」值**（`hyperliquid.py:214` 註解已如此標明），取區間 PnL 要相鄰相減。差分對「每個窗是否從 0 重新起算」免疫，故無需確認 rebase 慣例。

### 1b. `userNonFundingLedgerUpdates` — ✅ 存在，但**降級為對帳用，非主路徑**

`info.py:652-669`，`{"type": "userNonFundingLedgerUpdates", "user", "startTime"(必填), "endTime"(選填)}`。
delta 型別（Chainstack 參考文件，**第三方**）：`deposit`(usdc)、`withdraw`(usdc/nonce/fee)、`accountClassTransfer`(usdc/toPerp)、`spotTransfer`、`spotGenesis`、`subAccountTransfer`、`internalTransfer`、`liquidation`、`cStakingTransfer`。

**為什麼降級**：既然 `ΔF = ΔAV − ΔP` 已能從單一 portfolio 回應解出現金流，再拉第二個端點就是**把比較的兩側拆成兩個來源**——恰恰是工程原則 1 禁止的事（時間戳對齊誤差、端點延遲不一致都會製造幻影）。留給**定期對帳**：獨立算一次 ΣΔF 與 ledger 加總比對，不一致就告警。
⚠️ 未文件化於官方 GitBook 的 info-endpoint 清單（僅存在於 SDK 與第三方文件），schema 可能無預告變動——與 `scripts/leaderboard_snapshot.py:31-33` 對 stats-data 端點的處置同一風險等級。

### 1c. `userFills` — ❌ 不建議作為績效主路徑

`info.py:201-228`（`userFills`）、`:230-271`（`userFillsByTime`，本 repo 已包成 `get_user_fills`，`hyperliquid.py:191-209`）。
**否決理由**：官方文件載明 `userFills` **最多回傳 2000 筆最近成交**；要從成交重建損益還須自行併入 funding（另一端點 `userFunding`，`info.py:430-446`）、手續費、builder fee、以及未平倉部位的未實現損益。**用三個端點重建 HL 已經算好的東西**，工程量大且每個接縫都是 bug 面。只在需要「逐筆歸因」（哪個幣賺的、勝率）時才用。

### 1d. HL 官方 leaderboard — ⚠️ 可及但未文件化；ROI 定義**不是** TWR

本 repo 已查證並落地：`scripts/leaderboard_snapshot.py:4-33` 記載 `GET https://stats-data.hyperliquid.xyz/{Mainnet,Testnet}/leaderboard`（2026-07-17 實測），回傳 `leaderboardRows[].windowPerformances = [[window, {pnl, roi, vlm}], ...]`。**不在官方文件、不在 SDK**。

**HL 自己的 ROI 定義**（來源：app.hyperliquid.xyz/leaderboard 頁面說明文字，**非 GitBook**，可信度中等）：

> ROI = PNL / max(100, starting account value + maximum net deposits) for the time window

這是**保守的固定分母法**（近似 modified-Dietz 的悲觀版），不是 TWR：分母用「期初淨值 + 期間最大淨入金」，等於假設所有入金從期初就在帳上，**系統性低估**入金頻繁者的報酬。它防污染有效，但不是路徑正確的報酬率。**若我們顯示 TWR，數字會與 HL 官網不同**——這是 Q5 的反面證據之一。

---

## Q2. 正確的自建算法

### 2a. 用 TWR，且**現金流從同一回應推導**（推薦）

設 `AV(t)` = `accountValueHistory`、`P(t)` = `pnlHistory`，同一次 `portfolio()` 回應、同一組時間戳。
由官方定義 `P(t) = AV(t) − F(t)`（`F` = 累積淨入金）得：

```
ΔP_t = P(t) − P(t−1)          # 純交易損益（已含 funding、手續費）
ΔF_t = ΔAV_t − ΔP_t           # 該區間淨外部現金流（不必查 ledger）
r_t  = ΔP_t / AV(t−1)         # 分段報酬（現金流視為期末發生）
TWR  = Π(1 + r_t) − 1
權益指數 I_t = Π(1 + r_i)      # 正規化淨值曲線，出入金已中性化
```

**為什麼分段近似誤差小**：官方明載「出入金當下額外取樣」，故現金流落在取樣邊界上，含現金流那一段的交易時間趨近 0（`ΔP_t ≈ 0`），分母選 `AV(t−1)` 或 `AV(t−1)+ΔF_t` 的差異可忽略。
⚠️ 但**該取樣點在入金前或入金後未文件化**，殘留一個區間的歧義；`|ΔF_t|` 相對 `AV(t−1)` 很大時應標記該區間。

**必要防護（否則會產生假數字）**：
- **分母地板**：`AV(t−1) < FLOOR`（建議 100 USDC，與 HL 自己的 `max(100, …)` 同精神）→ 該區間 `r_t` 不計入。否則 leader 提領到近乎 0 再入金，會產生爆炸性假報酬。
- **`ΔF_t` 的殘差門檻**：`|ΔF_t|` 應該只在真有出入金時非零。若在無 ledger 事件的區間持續出現非零 `ΔF_t`，代表兩序列不同步 → **大聲告警**，不要靜默採用（工程原則 3）。
- **最大回撤必須算在 `I_t`（權益指數）上，不能算在 `AV(t)` 上**。leader 提領 50% 會讓 `AV` 腰斬 → 用 `AV` 算 MDD 直接產生幻影回撤，**與本專案事故 #1 完全同型**。

**替代方案「淨值扣除外部現金流」（modified Dietz）**：只需期初／期末淨值＋現金流清單，資料需求更低，但對期間內大額現金流敏感、且需要現金流的**發生時點權重**。既然我們有完整的分段序列，TWR 嚴格較優。**不建議**用 Dietz。

### 2b. 出入金事件：**不用另外取**

見 2a——`ΔF_t = ΔAV_t − ΔP_t` 即得。`userNonFundingLedgerUpdates` 只做定期對帳。
延遲／遺漏風險：兩序列同一回應，無跨端點延遲；遺漏風險轉化為「HL 取樣本身遺漏」，對兩序列同時作用，故不產生偏差方向（只損失解析度）。

### 2c. funding **算**績效的一部分（且已自動含入）

推導：`P = AV − F`。funding 支付／收取會改變 `AV`，且 funding **不是** deposit/withdraw（`userNonFundingLedgerUpdates` 的命名本身就說明 funding 被歸為另一類），故不會被 `F` 抵銷 → **必然落入 `P`**。手續費、builder fee 同理。
（標記：**演繹推論**，由官方公式與端點命名推出，非文件明文。）
**這是正確的**：對 perp 策略 funding 常是主要損益來源之一，排除它會系統性誤導。⚠️ 注意這與 HL 前端的 "closed pnl" 是**不同**概念——後者官方明載為 `purely frontend components provided for user convenience`（見「Entry price and pnl」），不要混用。

### 2d. 需要多久資料 → **分級揭露，禁止短樣本年化**

| 歷史長度 | 可顯示 | 禁止 |
|---|---|---|
| < 30 天 | 累積 PnL（$）、當前淨值、交易天數 | **任何 %報酬率**、年化、Sharpe、MDD |
| 30–90 天 | 窗口 TWR（**明標窗口起訖日**）、MDD（附解析度警語） | **年化**、Sharpe |
| ≥ 90 天 | ＋ 日報酬 Sharpe（明標「年化係數 √252」） | 仍需標註樣本長度 |
| ≥ 180 天 | ＋ 跨 regime 的信心敘述 | — |

**硬規則**：`< 90 天一律不年化`。7 天賺 3% 顯示成「年化 365%」是本專案最容易犯的誤導。
**端點敏感度**（承 `judgment.md` 2026-07-12 教訓）：任何 headline 數字都應附「窗口端點 ±3 日」的敏感度，或直接顯示滾動序列而非單點估計——端點移幾天就翻盤的數字是雜訊不是 edge。

---

## Q3. 一日一點夠嗎？→ **不夠，但解法不是提高輪詢頻率**

**現況缺陷**（`src/spark/filet/leaderboard.py:10-12` 自己已註明）：watchlist 快照走 `clearinghouseState`，只存**純量** `account_value` 等；日差分必然混入出入金。**資料本身不足以事後補救**——存下來的東西裡沒有任何能區分「入金」與「獲利」的資訊。

**關鍵洞察**：`portfolio()` 的 `day` 窗**伺服器端已保存 15 分鐘解析度的過去 24 小時**。所以只要**每日抓一次 `day` 窗並把新點附加到自有序列**，就能免費取得 15 分鐘解析度的永久檔案——**不需要提高輪詢頻率**。輪詢頻率決定的是「會不會漏」，不是解析度。

**一日一純量會漏掉**：日內回撤（perp 策略最致命的風險特徵）、當日出入金、爆倉事件的深度、日內波動（Sharpe 分母）。

**成本**（官方 rate limits：IP 加總 **1200 weight/分鐘**；`portfolio` 屬預設類別 **weight 20**；`clearinghouseState` weight 2）：
- 每分鐘上限 ≈ **60 次 portfolio 呼叫**。
- 50 個 leader × 每日 1 次 = 50 次呼叫 ≈ **一分鐘預算的 83%**（應打散，勿同時發）。成本可忽略。
- ⚠️ **建議每 12 小時抓一次（每日 2 次、窗口重疊）**：`day` 窗只涵蓋 24 小時，**單次 cron 失敗就是永久的資料洞**。重疊擷取讓單次失敗可自愈。

**儲存量（推估，未實測）**：`day` 窗每日約 96 點 × 2 序列 ≈ 6 KB/leader/日 → 50 leaders ≈ 300 KB/日 ≈ **110 MB/年**。完全可接受。

⚠️ **`allTime` 窗是降採樣的**（第三方來源指約 93 點，**未經本專案實測，標記為待驗證**）。若屬實，`allTime` 對長帳戶等於雙週間隔，**不可用來算 MDD 或 Sharpe**，只能看長期形狀。這是必須自建拼接序列的根本理由。

---

## Q4. 最小可信版本

### ✅ 現在就能誠實計算（資料已在手，`portfolio()` 已封裝在 adapter）

| 指標 | 來源 | 備註 |
|---|---|---|
| **累積 PnL（$）** 30d / all-time | `pnlHistory` 首末相減 | 官方已扣出入金，**零額外工作** |
| **窗口 TWR（%）** | 2a 公式，單次 `portfolio()` 回應 | 需分母地板防護 |
| **權益指數曲線 `I_t`** | 同上 | 前端畫圖用，出入金已中性化 |
| **窗口內 MDD** | 算在 `I_t` 上 | **必附**「15 分鐘取樣、日內可能低估」警語 |
| **HL 官方 ROI**（`day/week/month/allTime`） | stats-data leaderboard，`leaderboard_snapshot.py` 已在跑 | 與官網一致，但定義非 TWR，需標明 |
| **當前淨值／持倉數／保證金用量** | 現有 `clearinghouseState` 快照 | 已有 |
| **資料充足度徽章**（歷史天數、樣本點數） | 序列長度 | **必須顯示**，這是誠實的核心 |

### 🔧 需要新工作

1. **【最高優先】basis 決策：改用 `perpDay/perpWeek/perpMonth/perpAllTime`**。現行 `hyperliquid.py:159` 的 `total_periods` **刻意只收非-perp 窗**。這與尚未裁決的 F1（`docs/superpowers/research/2026-07-19-testnet-e2e-findings.md:33-54`）是**同一個決策**，應一併裁決。
2. **自建拼接時間序列**：每 12 小時抓 `perpDay` 窗 → 去重附加到 append-only 檔案。長期 Sharpe/MDD 的唯一來源。**無法回填**——今天不開始，90 天後仍然沒有 90 天資料。
3. **對帳作業**：定期比對 `Σ ΔF_t` vs `userNonFundingLedgerUpdates` 加總，偏差超門檻即告警。
4. **Sharpe / Sortino / 年化**：等 ≥90 天拼接資料。
5. **`allTime` 降採樣的實測驗證**（Q3 的待驗證項）。
6. **前端揭露文案**：窗口起訖日、樣本數、「非投資建議」、「leader 績效 ≠ 跟單者績效」。

---

## Q5. 反面證據：這個方案在什麼情況下仍會給出誤導數字

按嚴重度排序。

1. **【最強】leader 績效 ≠ 跟單者能複製的績效**。即使 perp-only basis 正確，跟單者的滑價、延遲、資金規模、槓桿上限都不同。leader 在流動性差的幣種大額進出，跟單者拿不到同樣價格。**leader 報酬率是跟單者報酬率的上界**，不是預期值。這是整個 SaaS 最根本的誤導風險，任何 API 都解決不了。
2. **basis 污染（若不改 perp 窗）**：`day/week/month/allTime` 含 spot ＋ **vault 餘額**（官方明載 `Account value includes … vault balances`）。leader 存 HLP vault 的被動收益、或持有升值的現貨 HYPE，都會顯示成「交易績效」。本 repo 已實測此事（999 = 499 spot + 500 perp，`testnet-e2e-findings.md:37`）。**未改 perp 窗前，顯示的績效有一部分是客戶複製不到的。**
3. **官方自承不適合精確會計**：`interpolation between samples may not reflect the actual change in unrealized pnl`。15 分鐘之間的來回（爆倉邊緣走一遭又回來）完全隱形 → **MDD 系統性低估**。對「回撤看起來很小」的 leader 要特別存疑。
4. **小分母操縱**：leader 提領到近乎 0 → 小額交易 → TWR 爆炸。分母地板能擋住極端值，但**擋不住刻意在低淨值期做高風險交易來美化 %報酬**的行為。
5. **倖存者偏差**：策劃清單天然只留贏家；爆掉的 leader 被移除後，清單的歷史平均報酬會系統性高於「當初選中的期望值」。**應保留除名紀錄並顯示**。
6. **報酬率不含風險**：3 倍槓桿賺 40% 與 1 倍賺 40% 在頁面上長得一樣。**只顯示報酬率而不顯示槓桿／MDD／持倉集中度，本身就是誤導**。
7. **與 HL 官網數字不一致**：我們的 TWR ≠ HL leaderboard 的 `roi`（定義不同，見 1d）。客戶對照官網會認為我們造假。**必須明示計算定義**，或同時顯示兩者。
8. **多錢包／子帳戶**：leader 可能跨多個地址跑同一策略；單地址視角可能嚴重不具代表性（`subAccountTransfer` 在 ledger 型別中存在，即為此情境的證據）。
9. **perp 窗的出入金語意是推論**：perp 窗是否把 spot→perp 的 `accountClassTransfer` 當作「入金」來扣除，**未經文件確認也未實測**。若否，perp 窗的 `pnlHistory` 會被內部劃轉污染。**這是本方案最關鍵的未驗證假設，上線前必須實測**（可用 testnet 錢包做一次劃轉觀察兩序列反應）。
10. **regime 依賴**：90 天樣本大多落在單一市場環境。賺錢的 leader 可能只是押對了單一方向的 beta，不是 alpha。

---

## 建議下一步

1. **裁決 basis**（perp-only vs 總值）——與 F1 合併決策，這是所有數字的前提。
2. **立刻開始拼接資料收集**（不可回填），即使前端還沒要顯示。
3. **實測驗證第 9 點**（perp 窗的劃轉語意）——這是唯一會使核心公式失效的未知。
4. 最小前端只上「累積 PnL（$）＋ 資料充足度徽章 ＋ HL 官方 ROI」，TWR 等實測與拼接資料到位再上。

---

## 來源

**本 repo（`檔案:行號`）**
- `src/spark/exchange/hyperliquid.py:148-177`（`get_equity_view`）、`:211-234`（`get_daily_abs_pnl`）、`:191-209`（`get_user_fills`）
- `src/spark/filet/leaderboard.py:10-12`（出入金污染的既有註記）
- `scripts/leaderboard_snapshot.py:4-33`（stats-data leaderboard 端點查證紀錄，2026-07-17 實測）
- `docs/superpowers/research/2026-07-19-testnet-e2e-findings.md:33-54`（F1：portfolio 含 spot 的實測）
- `tests/test_hyperliquid_reads.py:251-254`、`tests/test_copy_executor.py:292-293`（回應形狀 fixture）

**SDK**（hyperliquid-python-sdk **0.24.0**，`.venv/lib/python3.14/site-packages/hyperliquid/info.py`）
- `:671-683` portfolio｜`:652-669` userNonFundingLedgerUpdates｜`:201-271` userFills/ByTime｜`:430-446` userFunding

**官方文件**
- Portfolio graphs（PnL 定義、15 分鐘取樣、精確會計警語）: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/portfolio-graphs
- Info endpoint（portfolio 已文件化）: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
- Rate limits（1200 weight/分鐘、weight 20/2）: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits
- Entry price and pnl（frontend-only 警語）: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/entry-price-and-pnl
- ROI 公式（**app 頁面說明文字，非 GitBook，可信度中等**）: https://app.hyperliquid.xyz/leaderboard

**第三方（可信度較低，僅用於 schema 細節）**
- Chainstack portfolio: https://docs.chainstack.com/reference/hyperliquid-info-portfolio
- Chainstack userNonFundingLedgerUpdates（delta 型別表）: https://docs.chainstack.com/reference/hyperliquid-info-user-non-funding-ledger-updates

**標記為推測／未驗證**：funding 計入 `pnlHistory`（演繹推論）｜`allTime` 約 93 點降採樣｜perp 窗對 `accountClassTransfer` 的處理｜儲存量估算。
