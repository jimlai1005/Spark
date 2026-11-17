# 策略績效數字修正：總報酬改 Hyperliquid ROI 定義、入金放大修正、依取樣間隔年化

狀態：**已 push、已部署**（commit `a6d4ef8`；2026-11-17 13:54 UTC 隨 `dd0346c` 部署，記錄在 `deploy/RUNBOOK.md` 附錄 B；實作與兩輪審核詳見 §6）。
上線後實測：Filet Alpha 總報酬 +1055.95%（修正前約 +3800%）、Ultron −15.77%（修正前 +399%）、Filet Alpha (Legacy) −22.17%。

## 0. 背景與裁決

Filet Alpha 改指 `0xf97a…ddd1` 後，策略頁顯示總報酬 +3800%，與同頁的入金／淨值數字放在一起不合理。
追查（2026-11-16，資料來自 HL `portfolio` 的 `allTime` 窗）發現三個獨立問題：

1. **入金放大**：分段報酬 `r = ΔPnL / 上一點淨值`，同一取樣區間內先入金再有損益時分母偏小。
   Ultron 累計損益 −$816,543，現行公式卻顯示總報酬 +399%。
2. **年化倍數錯誤**：`allTime` 窗約 6–11 天一筆，比率指標卻一律乘 √365，Sharpe／Sortino／年化波動被放大約 √(間隔天數) 倍
   （Alpha：Sharpe 3.97 → 約 1.21、年化波動 638% → 約 195%）；「單日最佳 +290%」其實是約一週的區間。
3. **逐段複利的總報酬**對「大部分資金是賺來的」帳戶會得到極大的數字（Alpha 修掉入金放大後反而是 +4455%）。

| # | 裁決 | 來源 |
|---|---|---|
| D1 | 策略卡「總報酬」改用 **Hyperliquid 排行榜 ROI 定義**（方程式見 §1.3） | 使用者 2026-11-16 |
| D2 | 入金放大修正套用在**全站共用公式**（策略卡、探索／交易員頁回撤、儀表板隱藏數字一起變） | 使用者 2026-11-16 |
| D3 | Sharpe／Sortino／年化波動改**依實際取樣間隔年化**；「單日最佳／最差」改稱「單期」 | 使用者 2026-11-16 |
| D4 | 策略／交易員詳情頁的淨值曲線與「起訖等效淨值」改為 **ROI 基準**：曲線值＝`D + 累計損益`，`D`＝ROI 分母；原本用「ledger 入金加總」當本金（Alpha 為 $10.5M，會畫出數億美元的等效淨值）停用 | 主線程裁決（與 D1 同源） |
| D5 | CAGR 改由 ROI 年化：`(1+ROI)^(365/涵蓋天數) − 1`；ROI ≤ −1 時無定義 | 主線程裁決（與 D1 同源） |
| D6 | 最大回撤、Sharpe、Sortino、波動、勝率、單期最佳／最差**仍以修正後的分段報酬**計算（與交易員頁同一函式） | 主線程裁決 |
| D7 | 公開 JSON 鍵名一律不改（`total_return_pct`、`best_day_pct`、`worst_day_pct`、`win_rate_pct`、`equity_index`、`methodology.initial_deposit_usd`…），只改語意與前端文案；「日勝率」文案改「區間勝率」 | 主線程裁決（避免 API／快照欄位連鎖改動） |

D4–D7 是主線程裁決，交付時向使用者說明，使用者可推翻。

## 1. 規格（顯式方程式＋數值錨例）

記號：窗內取樣點 `t = 0..n`，`AV_t`＝`accountValueHistory`，`P_t`＝`pnlHistory`，
`ΔP_t = P_t − P_{t−1}`，當段淨外部現金流 `ΔF_t = (AV_t − AV_{t−1}) − ΔP_t`。

### 1.1 分段報酬（D2）

$$ r_t = \frac{\Delta P_t}{AV_{t-1} + \max(\Delta F_t,\ 0)} $$

- 跳過閘門**不變**：`AV_{t−1} < DENOMINATOR_FLOOR(100)` → 該段跳過（`skipped`、`leading_unfunded` 計數語意不變）。
- `r_t < −1` 判整窗 `flow_dominated_interval`、權益指數歸零判 `equity_index_collapsed`、跳過比例閘門：全部保留。
- 只加正的流入；流出不調整分母。
- **錨例 A1**：`AV=[8800, 49700]`、`P=[0, −6100]` → `ΔF = 47000`，`r = −6100 / 55800 = −0.109319`（舊公式 −0.693182）。

### 1.2 比率指標年化（D3）

`compute_ratio_metrics(returns, periods_per_year=DAYS_PER_YEAR)`：公式裡每個 `√365` 換成 `√periods_per_year`。
`compute_window_performance` 傳入

$$ \text{periods\_per\_year} = \frac{365}{\text{covered\_days} / (n)} $$

（`n`＝區間數＝`len(pnl) − 1`；即以平均取樣間隔年化。取樣不等距時為近似，於 docstring 註明。）

- 預設值 365 保持既有錨例（`tests/test_leader_perf_ratios.py`）不變：`r=[0.01, −0.005, 0.02]` → Sharpe 12.6526／SE 12.1798／vol 0.2404／Sortino 55.1513。
- **錨例 A3**：同一組 `r`，`periods_per_year = 36.5`（平均 10 天一筆）→ Sharpe **4.0011**／SE **3.8516**／vol **0.0760**／Sortino **17.4404**。
- perf 新增鍵 `periods_per_year`（Decimal）供揭露。
- `best_day_return`／`worst_day_return`／`win_rate` 內部鍵名不改；docstring 改寫為「單一取樣區間」語意。

### 1.3 Hyperliquid ROI（D1）

累計淨流入 `F_t = Σ_{i≤t} ΔF_i`（`F_0 = 0`）。

$$ D = \max\bigl(100,\ AV_0 + \max_{t} F_t\bigr), \qquad ROI = \frac{P_n - P_0}{D} $$

- perf 新增鍵：`roi`、`roi_denominator`、`roi_insufficient_data`（＝`covered_days < MIN_DAYS_FOR_RETURN`，與 `twr_insufficient_data` 同判定）。
- `roi_index`：tuple，`roi_index[t] = 1 + (P_t − P_0) / D`（長度＝取樣點數，首值 1）。
- `roi_annualized`（D5）：`ROI > −1` 時＝`_annualize(1 + ROI, covered_days)`；否則鍵缺席。
  其不足標記與外推天數沿用 `annualized_return_insufficient_data`／`annualized_return_extrapolated_from_days` 的判定式（只依 `covered_days`）。
- 既有 `twr`／`equity_index`／`max_drawdown`／`annualized_return` 保留（改用 §1.1 的分段報酬）。
- **錨例 A2**：`AV=[1000, 1500, 3000, 2200]`、`P=[0, 100, 300, 500]` → `ΔF=[400, 1300, −1000]`、`F=[0, 400, 1700, 700]`、
  `D = 2700`、`ROI = 500/2700 = 0.185185`、`roi_index = [1, 1.037037, 1.111111, 1.185185]`。
- **實測對帳（2026-11-16）**：HL 官方排行榜 `allTime` ROI：Alpha `10.4312`（pnl 3,510,481）、Ultron `−0.1583`（pnl −822,350）；
  本定義同時點算得 Alpha 分母 336,535、Ultron 分母 5,194,765，兩者與官方值一致（差異只來自抓取時點的 pnl）。

### 1.4 策略視圖投影（D1、D4、D5）

- `strategies._METRIC_SPEC`：`total_return_pct` 改讀 `roi`（不足旗標 `roi_insufficient_data`）。其餘列不變。
- `build_cagr_pct`／`build_cagr_fields`：改讀 `roi_annualized`。
- `build_equity_index`：改輸出 `roi_index`。
- `build_methodology`：`initial_deposit_usd` 改輸出 `roi_denominator`（鍵名不變）；`start_equity_usd`／`end_equity_usd` 維持實際帳戶淨值。
  新增 `periods_per_year`、`return_basis: "hl_roi"`。
- `/api/public/strategies/{slug}` 與 `/api/public/traders/{address}` 不再為 methodology 查 ledger 入金（`_cached_ledger_deposit` 的呼叫點移除；
  若因此變成無人使用，連同快取一起移除；`sum_ledger_deposits` 若仍有其他使用者或測試則保留）。
- `EXPLORE_INDEX_VERSION` 5 → 6（`max_dd_pct` 數值語意改變；載入端相容版本清單同步處理，舊版快照不得被當成新版）。

### 1.5 前端文案（`web/src/lib/copy.ts`，ZH／EN 對稱）

| key | ZH 新值 | EN 新值 |
|---|---|---|
| `totalReturnNote` | Hyperliquid 排行榜 ROI 定義 | Hyperliquid leaderboard ROI definition |
| `annualizedVolNote` | 依取樣間隔年化 | Annualized by sampling interval |
| `bestWorstLabel` | 最佳 / 最差單期 | Best / worst period |
| `bestWorstNote` | 單一取樣區間 % | Single sampling interval % |
| `metricWinRate`（及詳情頁同義 key） | 區間勝率 | Period win rate |
| `depositPrefix` | 以投入本金 $ | From invested capital of $ |
| `startEndEquityNote` | 投入本金 → 本金加累計損益 | Invested capital → capital plus cumulative PnL |
| `insufficientGroupLabel` | 「最佳最差日」改「最佳最差單期」 | 對應改 period |

方法論段落（`strategyDetail.methodology`，`/docs#methodology` 共用）新增三句，ZH：

- 「總報酬採 Hyperliquid 排行榜的 ROI 定義：累計損益 ÷（起始淨值＋期間最大累計淨入金），不做逐段複利。」
- 「最大回撤、Sharpe、Sortino 與波動以逐段報酬計算；每段報酬的分母為上一個取樣點的淨值加上該段的淨入金。」
- 「Hyperliquid 的全期序列約每 1–2 週取樣一次；比率指標依實際取樣間隔年化，最佳／最差為單一取樣區間的報酬。」

EN 對應翻譯由實作者寫，語意逐句對齊。淨值曲線標題若仍寫「帳戶淨值曲線」，改為「投入本金加累計損益（USD）」／"Invested capital plus cumulative PnL (USD)"。

## 2. Tasks

### Task 1 @inline 後端公式與投影
範圍：`src/spark/filet/leader_perf.py`、`src/spark/filet/strategies.py`、`src/spark/filet/trader_stats.py`（僅在必要時）、
`src/spark/publicapi/app.py`（策略／交易員端點接線與 ledger 入金呼叫點）、`src/spark/publicapi/hl_explore.py`（版本常數與相容清單）、
對應 `tests/`。不動 `src/spark/copytrade/`、`src/spark/exchange/`、`deploy/`、`web/`。

1. 先寫失敗測試：錨例 A1、A2、A3；「入金同區間的虧損帳戶 ROI 與 TWR 皆為負」；預設 `periods_per_year` 下既有比率錨例不變。
2. 實作 §1.1–§1.4。
3. 既有測試中鎖定舊數值的（`tests/test_public_strategies.py:113` `"20.35"`、`tests/test_public_traders.py:92` `"20.00"` 等）
   依新定義**手算**後更新，並在測試旁註明算式；不得為了過測試而放寬斷言型態。
4. `leader_perf.py` 檔頭與相關 docstring 更新成新公式（LaTeX），舊公式的敘述不得殘留。

驗收：
- `uv run pytest -q` 全綠（貼輸出末行）。
- `uv run ruff check src tests scripts` 無錯。
- `uv run pytest -q tests/test_leader_perf.py tests/test_leader_perf_ratios.py tests/test_public_strategies.py tests/test_public_traders.py tests/test_trader_stats.py -q` 全綠。
- `git grep -n "EXPLORE_INDEX_VERSION = 6" src/spark/publicapi/hl_explore.py` 命中 1 行。

### Task 2 @inline 前端文案與顯示
範圍：`web/src/lib/copy.ts`、`web/src/components/`（`MethodologyCard`、`EquityCurve`、`StrategyCard`、`CagrCard` 視需要）、
`web/src/app/strategies/[slug]/page.tsx`、`web/src/app/traders/[address]/page.tsx`、`web/src/lib/strategyMetrics.ts`、`web/src/lib/publicApi.ts`（型別新增欄位）與對應測試。
不動後端、不動 `web/src/content/legal.ts`。

1. §1.5 文案表逐列落地（ZH／EN 結構對稱，元件經 `useCopy()` 取用，不內嵌中文）。
2. 詳情頁「起訖等效淨值」與曲線 Y 軸改用 `methodology.initial_deposit_usd`（現為 ROI 分母）× `equity_index`（現為 `roi_index`）；
   既有算式若已是「本金 × 末值/首值」則只需確認文案與測試。
3. 方法論卡顯示新增的三句說明。

驗收：
- `export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH" && cd web && npm test` 全綠（貼 Test Files／Tests 兩行）。
- `git grep -n -E "單日 %|最佳 / 最差日|真實入金起算|Best / worst day|From real deposit" -- web/src ':!web/src/**/*.test.*'` 零命中。

### Task 3 主線程驗收（親跑）
- Task 1、2 的驗收指令全部重跑。
- 以本機程式對三個真實位址即時計算（唯讀 HL 公開 API），確認：Alpha `total_return_pct` 與 HL 排行榜 `allTime` ROI 相對誤差 < 2%；
  Ultron 為負且與排行榜相對誤差 < 2%；Alpha Sharpe 落在 1.0–1.5、年化波動 150%–250%；起訖等效淨值不再出現億級數字。

### Task 4 審核
派 `reviewer`：輸入＝`git diff`、本 plan、測試輸出。

## 3. 部署注意（本 plan 不執行部署）

- `EXPLORE_INDEX_VERSION` 升版：依 CLAUDE.md 部署慣例與 RUNBOOK §5.8c／§5.8e 處理探索快照。
- 後端＋前端都有改動：需重啟 `filet-api` 與 `filet-dashboard`。
- `perf_series` 既有歷史列是舊公式算的，升版後新舊列混在同一檔；是否影響任何顯示由 Task 1 回報。

## 4. Task 1 完成後的主線程裁決（2026-11-16）

builder 回報三個 plan 未涵蓋的點，裁決如下（皆為「不擴大範圍」）：

1. **探索快照 v3 相容保留**：載入端仍接受 `(6, 3)`。正式機現行是 v5（升版後被拒、由 publisher 重建）；v3 只存在於手動備份 `.v3.bak`，
   有既有遷移測試鎖定。不在本 plan 移除；風險（還原 `.v3.bak` 會端出舊公式回撤）記入交付說明。
2. **交易員端點仍會抓一次 ledger**（`_cached_trader_data` 路徑，結果已不用於 methodology）：與池內快取、排程 job、`as_of.ledger` 糾纏，本 plan 不動，列為後續清理。
3. **交易員頁 `methodology` 不加 `periods_per_year`／`return_basis`**：只有策略詳情有這兩個鍵。Task 2 的三句方法論說明是靜態文案，不依賴這兩個鍵。

Task 1 另新增 perf 鍵 `roi_annualized_insufficient_data`、`roi_annualized_extrapolated_from_days`（判定式同 `annualized_return_*`），追認。

Task 3 實測（2026-11-16，本機新程式 × HL 即時資料）：

| | 總報酬（新） | HL 排行榜 ROI | 最大回撤 | Sharpe | 年化波動 | CAGR | 等效淨值 起→訖 |
|---|---|---|---|---|---|---|---|
| Filet Alpha | +1040.62% | +1043.12% | −31.96% | 1.32 | 170.30% | +172.38% | $336,535 → $3,838,588 |
| Legacy | −22.53% | 不在榜 | −43.27% | −0.96 | 55.65% | −49.17% | $1,000 → $775 |
| Ultron | −15.86% | −15.83% | −26.90% | −0.20 | 42.63% | −16.58% | $5,194,765 → $4,370,806 |

`/api/leaders` 目錄頁的 perp 窗數字來自每日 watchlist 快照（`leaderboard.py:78-82`），部署後到下一次每日快照前仍是舊公式（最多約 1 天）。

## 5. 第一輪審核（reviewer，2026-11-17）：0 Critical／3 Warning／1 Suggestion → Task 5 修正

### Task 5 @inline 審核修正
範圍：`src/spark/filet/leader_perf.py`、`tests/`、`web/src/lib/copy.ts`、`web/src/components/MethodologyCard.test.tsx`、`web/src/app/docs/methodologyDefinitions.test.tsx`（及因文案變動而需同步的前端測試）。其餘不動。

- **W1（D4 無測試守護）**：新增測試，使用**含現金流**、`equity_index != roi_index` 的序列：
  (a) 單元層：`build_equity_index(perf)` 輸出等於 `roi_index`（逐點）且不等於 `equity_index`；
  (b) 端點層：`/api/public/strategies/{slug}` 回應的 `equity_index` 末值等於 `1 + ROI`，且 `methodology.initial_deposit_usd × equity_index[-1]` 等於 `D + 累計損益`。
  判準：把 `build_equity_index` 改回讀 `equity_index` 時，這兩個測試必須轉紅（實作者實際做一次變異並貼紅燈輸出，再還原）。
- **W2（差一）**：`sample_count` 是取樣**點**數，不是區間數。後端不改；文案改稱「取樣點」：
  `daysSuffix` ZH「 個取樣點（」／EN " sampling points ("；`sampleSuffix` ZH「 個取樣點）。」／EN " sampling points)."；
  `winRateNoteSuffix` ZH「 個取樣點」／EN " sampling points"。同步更新鎖定舊字串的測試。
- **W3（除以零）**：`periods_per_year` 改由原始毫秒時間戳計算，不經 quantize 過的 `covered_days`：
  $$ \text{periods\_per\_year} = \frac{365 \cdot n \cdot 86400000}{t_n - t_0} $$
  （既有閘門保證 `t_n > t_0`）。新增測試：兩個取樣點相距 1 ms 時 `compute_window_performance` 不拋例外、回 `status == "ok"`。
  錨例 A3 經 `compute_window_performance` 的測試數值不得改變。
- **S1（兩句年化說明並列易誤讀）**：`samplingNote` 改為
  ZH「Hyperliquid 的全期序列約每 1–2 週取樣一次；比率指標依實際取樣間隔換算為年化（一年以 365 日計），最佳／最差為單一取樣區間的報酬。」
  EN "Hyperliquid's all-time series is sampled roughly once every 1–2 weeks; ratio metrics are annualized from the actual sampling interval (using a 365-day year), and best/worst are the returns of a single sampling interval."

驗收：`uv run pytest -q tests/test_leader_perf.py tests/test_leader_perf_ratios.py tests/test_leader_perf_roi.py tests/test_public_strategies.py tests/test_public_traders.py tests/test_trader_stats.py tests/test_strategies_metric_spec.py` 全綠；
`uv run ruff check src tests scripts` 無錯；`cd web && npm test` 全綠；
`git grep -n -E "個取樣區間（|sampling intervals \(|個區間報酬樣本|period return samples" -- web/src/lib/copy.ts` 零命中。

## 6. 第二輪審核（fresh reviewer，2026-11-17）：PASS，0 Critical／0 Warning／2 Suggestion

Task 5 的 W1／W2／W3／S1 經複審確認逐條落地；主線程另親跑變異（`build_equity_index` 改回讀 `equity_index` → W1 兩個測試轉紅，還原後轉綠）。

未處理、留待使用者裁決的項目：

1. **「N=9 個取樣點」的 N**：字面正確，但勝率與 Sharpe 標準誤公式裡的 N 是區間數（點數 − 1），讀者用「勝場 ÷ N」對不出勝率。可選修法：拿掉「N=」前綴，或後端另外輸出區間數。
2. **淨值曲線的 30D／7D 切換與 X 軸日期假設「一點＝一天」**（`web/src/components/EquityCurve.tsx` 的 `sliceByPeriod`／`datesForSlice`）：
   HL 全期序列約每 1–2 週一點，按 30D 實際顯示約 6–11 個月，疊加 BTC 等對照線時日期會錯位。既有問題，非本次引進。
3. **`_annualize` 攔不到 `decimal.Overflow`**（窗長不到 1 分鐘且成長近 2 倍才觸發，實務上碰不到；改版前即存在）。
4. §4 的三點（v3 快照相容、交易員端點多一次 ledger 查詢、`/api/leaders` 目錄頁最多一天舊公式）。

狀態：實作與兩輪審核完成，**改動仍在工作樹未 commit**；待使用者確認後 commit、push、部署。
