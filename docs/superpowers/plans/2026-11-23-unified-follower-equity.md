# 統一帳戶跟單帳戶（follower）支援 Implementation Plan

> **For agentic workers:** 逐 task 由主線程派工（`builder`／`reviewer`），只讀本檔；plan 沒涵蓋的決策先回報主線程。
> 狀態（2026-11-23）：**草稿，等使用者確認 §4 的 F1–F4 後才派工。** 前置：`docs/superpowers/plans/2026-11-22-leader-equity-basis.md`（leader 側）先完成並部署。

**Goal:** 讓 Hyperliquid「統一帳戶」模式的跟單帳戶能正常被啟用、跟單、受風控保護，且同一個帳戶在引擎、啟用檢查、儀表板看到的是同一個權益數字。

**Architecture:** 新增一個純函式「follower 權益基準」（依帳戶模式決定讀哪個數字），引擎的兩個讀取點（風控用的 `perp_equity_view`、倍率分子 `my_state`）與 API 的三個讀取點（啟用入金檢查、儀表板、營運面板）全部改經它；帳戶模式每輪讀取並落檔，模式改變時重設回撤基準並告警。

**Tech Stack:** Python 3.11、uv、pytest（全離線）、hyperliquid-python-sdk 0.24.0（`Info.spot_user_state`、`Info.query_user_abstraction_state`）、FastAPI（`src/spark/publicapi`）。

---

## 1. 背景與證據

- `fd149b…`（`0xd149…5e58`）是 `unifiedAccount`：現貨 USDC `total` 290.02、perp `accountValue` 0。引擎的 follower 權益全部讀 perp `accountValue` → 本金 0 → 倍率 0 → 完全不跟單，無任何告警。
  它 2026-10-11 以一筆 `send`（`sourceDex: "spot"`）入金 299.84；`portfolio.perpMonth` 顯示 10 月起 perp `accountValue` 只有 $2–4（當時就是保證金而非權益）；40 天內只有 4 筆成交（最後一筆 11-14）；帳戶總值 296 → 290。也就是它從啟用起就沒有被正確跟單過。
- 實測恆等式（`docs/superpowers/research/2026-11-22-leader-equity-basis-recon.md` §2）：統一帳戶下 perp 未實現損益已記在現貨 USDC `total` 裡（leader 三次取樣 `USDC total − upnl` 不變）；perp `accountValue` 沒有意義（官方文件同此說法）。
- HL 上統一帳戶是多數（探索池前 60 個位址 39 個）；新客戶很可能就是這個模式。
- 盤點（scout，2026-11-23；`loop.py` 行號可能因 leader 側修改位移）：
  - 引擎讀取點兩處：`loop.py:156` `perp_equity_view`（→ 回撤 kill switch、lifetime peak、成本熔斷分母 `costbreaker.py:217`）；`loop.py:347` `my_state.account_value`（→ 倍率分子，`sizing.py:58-102`）。另有 `panic.py:212`、`run_copytrade.py:587`（`--status`）、`publicapi/ops.py:54`。
  - 回撤基準的狀態檔：`var/copytrade/equity_samples.json`、`equity_lifetime_peak.json`（`equity.py:30`、`:112`）；出入金校正標記 `follower_flow.json`（`follower_flow.py:29`）。
  - 出入金校正白名單（`src/spark/exchange/ledger_flows.py:13-97`）：`deposit`、`withdraw`、`vaultDeposit`、`vaultWithdraw`、`accountClassTransfer`、`internalTransfer`；`send` 不在白名單（只發 warn）。
  - 成本熔斷在 `current <= 0` 時**停用**換手率判定（`costbreaker.py:387-393`）——統一帳戶空倉時現在就是這個狀態。
  - 啟用入金檢查只看 perp `accountValue`（`publicapi/app.py:3777-3778`，門檻 100 USDC）；統一帳戶客戶永遠過不了，還會收到「請把錢從 spot 劃轉到 perp」的錯誤提示（`app.py:3701-3757`）。
  - 儀表板直接讀 `clearinghouseState`（`app.py:4555`、`:463-491`、`:682-685`），統一帳戶下顯示 0 或誤導值。
  - follower 端完全沒有讀帳戶模式的程式。

## 2. 目標

1. 統一帳戶的 follower：權益＝現貨 USDC `total`（F1），用於倍率分子、回撤、成本熔斷——三者同一輪同一個數字。
2. 傳統模式的 follower：行為與現在逐位元相同（perp `accountValue`）。
3. 帳戶模式每輪讀取、落檔；模式改變 → 重設回撤基準＋critical 告警＋當輪零交易動作。
4. 不支援的模式（`portfolioMargin`、不認得的值）：引擎零交易動作＋告警；啟用檢查擋下並說明原因。
5. 啟用入金檢查、儀表板、營運面板與引擎用同一個基準函式。
6. 上線前在 testnet 用統一帳戶實測：權益讀值、入出金在 ledger 的型別、下單與保證金行為。

## 3. 不做（範圍外）

- leader 側的權益（另一份 plan）。
- 倍率鎖定（另一份 plan 的 T2b）。
- 客戶自己買賣現貨代幣造成的 USDC 變動不做校正（見 §7 R2）。
- 不替客戶切換帳戶模式、不做任何劃轉（紅線 3）。
- 探索頁指標。

## 4. 裁決表

| # | 裁決 | 狀態 |
|---|---|---|
| F1 | **統一帳戶 follower 的權益算什麼**。甲（建議）：現貨 USDC `total`——就是能拿來當永續保證金的錢，已含永續的浮動損益；客戶持有的其他現貨代幣不算。乙：交易所帳戶總值（含其他代幣）。選甲的理由：引擎按這個數字開倉，算進不能當保證金的代幣會開出保證金不足的單；回撤熔斷也不該被客戶自己持有的其他幣的漲跌觸發。甲的代價見 R2。`fd149b…` 兩者目前相同（只有 USDC，290.02）。 | **待使用者確認** |
| F2 | **portfolio margin 模式的跟單帳戶不支援**：啟用檢查顯示「此帳戶模式尚不支援」，已在跑的引擎零交易動作並告警。理由：該模式下 USDC 可為負（借貸），抵押品是多種資產按折扣計算，另需設計。 | **待使用者確認** |
| F3 | **跟單途中客戶切換帳戶模式**：引擎每輪讀模式（每個 follower 每分鐘多一次查詢）。發現改變 → 當輪不交易、發 critical、把回撤基準（7 天高點樣本與歷史高點）重設為新基準下的當前值，下一輪起照新模式運作。代價：客戶可以藉切換模式重設自己的回撤高點（那是他自己的帳戶與風控）。 | **待使用者確認** |
| F4 | **`fd149b…` 的處置**：部署後它會開始真的跟單（$290、依其使用率設定）。部署前由使用者決定是否先通知該帳戶主人或先暫停該引擎。 | **待使用者確認** |
| F5 | 傳統模式判定只認 `"disabled"`；`"default"`／`"dexAbstraction"` 的實際行為在 T1 探針確認後由主線程寫回本表，確認前一律當不支援（零交易動作）。 | 主線程定 |
| F6 | 讀不到模式或現貨餘額：照既有讀取失敗的處理（例外往外拋、`main_loop` 連續錯誤計數）；權益讀值 ≤ 0 時不得觸發任何 flatten／halt（既有原則，工程原則 #1 事故 #4）。 | 主線程定 |
| F7 | 首次部署的既有 follower：狀態根沒有模式檔 → 以當下模式初始化。當下是 `disabled` → 不重設任何基準（數字不變）；當下是 `unifiedAccount` → 重設基準（舊樣本是 perp `accountValue`，不同基準）。 | 主線程定 |

### 方程式與數值錨例（預註冊）

$$E_{\text{follower}} = \begin{cases} \text{perp accountValue} & \text{mode} = \texttt{disabled} \\ \text{spot USDC total} & \text{mode} = \texttt{unifiedAccount} \\ \text{不支援（raise）} & \text{其他} \end{cases}$$

| 錨例 | 輸入 | 預期 |
|---|---|---|
| G1（`fd149b…`，2026-11-23 14:30Z） | mode＝unifiedAccount、perp av＝0、USDC total＝290.019982、hold＝0 | 290.019982 |
| G2（leader `0xfb9c…` 當 follower 看，11-22 13:57:10Z） | mode＝unifiedAccount、perp av＝120.58、USDC total＝209.48、hold＝121.57、UBTC 值 497.89 | 209.48（不加 perp av、不扣 hold、不含 UBTC） |
| G3（`f438b3…`，11-23 14:30Z） | mode＝disabled、perp av＝4808.87、USDC total＝0.006975 | 4808.87（不讀現貨） |
| G4 | mode＝portfolioMargin | `UnsupportedAccountMode` |
| G5 | mode＝unifiedAccount、現貨餘額清單裡沒有 USDC | 0（空帳戶；呼叫端不得據此 flatten） |

## 5. 檔案地圖

| 檔案 | 動作 | 責任 |
|---|---|---|
| `scripts/testnet_unified_probe.py` | 新增（T1） | testnet 統一帳戶實測 |
| `src/spark/exchange/base.py`／`hyperliquid.py`／`fakes.py` | 改（T2） | `SpotBalance`、`get_spot_balances` |
| `src/spark/account_basis.py` | 新增（T2） | 純函式：模式＋讀值 → follower 權益；引擎與 API 共用 |
| `src/spark/copytrade/account_mode.py` | 新增（T3） | 模式落檔、改變偵測、基準重設 |
| `src/spark/copytrade/equity.py`／`loop.py`／`panic.py`、`scripts/run_copytrade.py` | 改（T3） | 兩個讀取點改經基準函式 |
| `src/spark/exchange/ledger_flows.py`／`src/spark/copytrade/follower_flow.py` | 改（T4，內容依 T1 結果） | 統一帳戶的入出金型別 |
| `src/spark/publicapi/hl.py`／`app.py`／`ops.py` | 改（T5） | 啟用入金檢查、提示、儀表板、營運面板 |
| `web/src/lib/copy.ts` 等 | 改（T5，若需新文案） | 「此帳戶模式尚不支援」 |

## 6. Tasks（摘要；F1–F4 確認後主線程補齊每個 task 的簽名、測試清單與驗收指令再派工）

### T1 `@inline` — testnet 統一帳戶探針（主線程親跑）
拋棄式 testnet 錢包切成 `unifiedAccount`，逐項實測並記錄原始回應：(1) 空倉／有掛單／有倉位／有浮動損益時，現貨 USDC `total`、`hold` 與 perp `accountValue` 各是多少，`total − upnl` 是否不變；(2) 入金、出金、`send` 轉入轉出、現貨↔永續劃轉（若該模式仍允許）在 `userNonFundingLedgerUpdates` 的型別與欄位；(3) 以 agent＋builder 下單是否與傳統模式相同、保證金不足時的錯誤訊息；(4) `"default"` 模式新錢包的實際行為；(5) 模式切換前後各讀值的變化；(6) 引擎一輪的 info 查詢權重實數。產出寫進 research 檔，並把 F5 與 T4 的型別白名單定案。

### T2 `@inline` — 讀取層與基準純函式
`ExchangeAdapter.get_spot_balances(address) -> list[SpotBalance(coin, total, hold)]`；`account_basis.follower_equity(mode, perp_account_value, spot_balances) -> Decimal`（錨例 G1–G5）；`UnsupportedAccountMode`。

### T3 `@inline` — 引擎接線與模式落檔（依賴 T2）
每輪：讀模式 → 與 `var/copytrade/account_mode.json` 比對（F3、F7）→ 算出本輪唯一的 follower 權益 → `perp_equity_view`（改名為中性名稱或加參數，由主線程補規格）與倍率分子都用它。不支援模式／模式改變的那一輪零交易動作。`panic`、`--status` 同步。變異驗收：任一讀取點改回 perp `accountValue` 必有測試轉紅。

### T4 `@inline` — 統一帳戶的出入金校正（依賴 T1 結果）
依 T1 實測到的 ledger 型別調整白名單與方向；統一帳戶下現貨↔永續劃轉視為中性（若存在）。

### T5 `@inline` — API：啟用檢查、提示、儀表板、營運面板（依賴 T2）
`_progress` 的 `funded` 改用 `account_basis.follower_equity`；統一帳戶不再顯示「請劃轉到 perp」；不支援模式顯示原因；儀表板的權益／可用資金／槓桿改用同一基準。前端若需新文案走 `copy.ts`（中英對稱），改後跑 vitest 與 typecheck。

### T6 — testnet 端到端（主線程親跑）
統一帳戶 follower 跟一個 testnet leader：開倉數量符合倍率公式、回撤樣本以新基準累積、模式切換那一輪零動作＋告警、傳統模式 follower 對照組行為不變。

### T7 — 審核（`reviewer`）→ T8 — 部署（主線程；`filet-api` 與 follower 都要重啟；F4 的處置先做）

## 7. 殘留風險

- **R1 換基準時的假回撤**：舊的高點樣本是 perp `accountValue`。F7 的重設若漏做，統一帳戶 follower 會以新舊數字相比而誤判；T3 以測試與變異守住。
- **R2 客戶自己買現貨代幣會被看成虧損**（F1＝甲）：USDC 變少而引擎不知道那是換成了別的幣。有開回撤風控的統一帳戶 follower 可能因此被熔斷平倉。緩解：啟用頁說明跟單帳戶建議只放 USDC；本 plan 不做自動校正。
- **R3 查詢量增加**：每個 follower 每輪多一次模式查詢（權重 20）；統一帳戶再多一次現貨餘額（權重 2）。加上 leader 側的 +20，三個 follower 每分鐘共約 +120 權重。T1 量出現況實數後評估是否需要降頻。
- **R4 統一帳戶與 builder fee**：官方文件只要求 builder 位址是傳統模式；follower 是統一帳戶時 builder fee 是否照常累積，T1 實測。
- **R5 統一帳戶每日動作上限**：官方文件載明統一帳戶與 portfolio margin 每日 50,000 個使用者動作；每分鐘一輪的引擎遠低於此，網格型 leader 的大量掛單需留意。

## 8. 待使用者確認

1. F1：統一帳戶跟單帳戶的權益用現貨 USDC（甲，建議）或帳戶總值（乙）。
2. F2：portfolio margin 的跟單帳戶先不支援。
3. F3：客戶切換模式時重設回撤基準並告警。
4. F4：`fd149b…` 部署後會開始真的跟單——要不要先通知帳戶主人或先暫停。
