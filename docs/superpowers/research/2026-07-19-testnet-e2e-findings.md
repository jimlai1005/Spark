# Testnet 端到端實測發現（2026-07-19）

**環境**：HL testnet｜Leader = Builder3662 `0xbAC652…3662`（perp 500）｜Follower = Follower9760 `0xfB9C52…9760`（perp 500、spot 499）｜builder = 3662｜引擎 branch `feat/m2-frontend`
**背景**：M1 的 T1.3 探針與 T4.2 E2E 一直卡在 testnet 無資金，本次為首次真實跑。

---

## 結論總覽

**M1 一直卡著沒跑的 testnet 端到端（T4.2）與 modify 探針（T1.3），今晚全部完成。核心價值鏈實際跑通：leader 交易 → 引擎鏡像 → builder fee 入帳。**

**通過（有鏈上獨立驗證，非僅引擎自報）**
- **V1** 完整生命週期：開倉/加倉/部分平/反手/全平 五種操作**全部精確鏡像**
- **V2** Builder fee 實收 **0.02%**（＝f=20），北極星 `query_builder_accrued` 正常，累積入帳
- **V3** 非託管不變量：全程只用 agent key，主鑰零接觸
- **V4** Kill switch 全鏈路：偵測→自動平倉→ARM 鎖死→**參數調回也不解鎖**→人工 re-arm 恢復
- **V5** 日報/TE telemetry：配對 10 筆、中位延遲 9.7s；CSV 對帳**誠實回報「未驗證」**
- **T1.3** modify **不丟失** builder 歸屬（ratio 0.9998，二次獨立重現＋對照組）

**需要裁決（3 項）**
| # | 議題 | 影響 | 我的建議 |
|---|---|---|---|
| **F1** | 回撤熔斷的 equity basis 含 spot，稀釋保護（下單大小的基準是**對的**） | 客戶在同錢包留大額 spot 時，回撤保護接近失效 | 傾向改用 perp basis（與 sizing 同源）；屬風控語意決策，未擅改 |
| **T1.3 政策** | 「容忍漏繳 vs 強制 cancel+place」 | — | **問題消解**：modify 不丟歸屬，現行 `modify-first` 可保留 |
| **F2** | M1 腳本綁主鑰、與非託管不相容 | 僅測試工具；`run_testnet_flow.py` 同樣問題待決定是否一併改 | 探針已改造完成；另一支請裁示 |

**可自行處理（低風險，未動）**：F3/F4——dry-run 假件的 modify 語意（oid 保留）與真交易所（oid 重配發、失敗非原子）分歧。目前每輪重讀掛單故自癒，但假件會給出假信心，建議修正以維持測試保真度。

**測試成本**：兩錢包各約 $0.7-0.8 testnet 手續費，結束時皆已平倉、無殘留掛單。

---

## F1 ⚠️ 回撤熔斷的 equity basis 含 spot，稀釋保護靈敏度

**現象**：`run_copytrade --status` 回報 `equity: current=$999.0`，但該錢包 perp accountValue 僅 500（另 499 在 spot）。

**根因**：`src/spark/copytrade/loop.py:74` 的回撤判定走 `adapter.get_equity_view()`（`src/spark/exchange/hyperliquid.py:148`），其資料源是 HL `portfolio()` 的 `accountValueHistory`——實測該值 = **spot + perp 總和**（999 = 499 + 500，精確吻合）。而 `get_account_value()`（同檔 :43）走 `clearinghouseState.marginSummary.accountValue` = **perp only** = 500。

**已確認正確的部分**：下單大小（sizing）用的是 `my_state.account_value`（perp only），見 `loop.py:107` 並附「同源」註解——**不會超額下單**，這條沒問題。

**問題**：回撤 = (peak − current) / peak 以含 spot 的總值為基準，但交易盈虧只發生在 perp。
- 數值示例：perp 500 → 400（**實際交易虧損 20%**，應觸發 `max_drawdown_pct=0.20` 熔斷），總值 999 → 899，回撤僅 **10%** → **熔斷不觸發**。
- 稀釋倍率 = 總權益 / perp 權益。本測試錢包約 2×；客戶若 100 perp + 10000 spot 則稀釋 101×，**回撤保護實質失效**。

**為何 M1 沒暴露**：M1 dogfood 錢包是專用錢包、資金全在 perp，spot≈0，兩個 basis 數值相同。**M2 客戶錢包的餘額形態不由我們控制**，這才讓差異浮現。

**對照工程原則 #1**：本案 current 與 peak 同源（皆出自單次 `portfolio()`），**不會產生假回撤**（無誤判方向的風險）；問題在 basis 選擇——把「交易碰不到的錢」算進保護分母，使保護偏鬆。屬事故 #3「equity basis 是錢包形態專屬的」同型問題。

**候選修法（需使用者裁決，涉及實盤風控語意，未擅自改）**：
1. **回撤改用 perp accountValue**（與 sizing 同基準）——保護回歸「交易帳戶」語意；但失去 portfolio() 的歷史 peak 序列，需自行維護 peak 狀態。
2. **維持 portfolio 但按 perp 占比校正**——複雜、易出錯，不建議。
3. **接受現況並明確標註**：文件寫明「回撤保護以總資產計」，並在 onboarding 建議客戶把跟單資金放 perp、勿在同錢包留大額 spot。成本最低但保護仍偏鬆。

**建議**：傾向 1（保護與下單同基準才自洽），但這是風控語意決策，呈使用者裁決。

---

## ✅ V1 完整生命週期鏡像——全數通過（M1 T4.2 E2E 首次真實完成）

每步：leader 動作 → 引擎 `--once`（LIVE）→ 鏈上獨立驗證 follower 持倉。scale ≈ 1.0002（兩錢包 perp 各 500）。

| # | leader 動作 | 引擎判定 | leader szi | follower szi | 結果 |
|---|---|---|---|---|---|
| 1 | 開多 $100 | `opened` | 0.0537 | 0.0537 | ✅ |
| 2 | 加倉 +$60 | `adjusted kind=increase diff=+0.0322` | 0.0859 | 0.0859 | ✅ |
| 3 | 平 50% | `adjusted kind=decrease diff=-0.0429` | 0.0430 | 0.0430 | ✅ |
| 4 | 反手做空 $100 | `adjusted kind=flip long→short` | -0.0537 | -0.0537 | ✅ |
| 5 | 全平 | `flattened short 0.0537` | (無) | (無) | ✅ |

**結論**：position-mirror 收斂、增減倉差異計算、反手（平+反開同一輪）、全平，四類動作在真實 testnet 上皆精確鏡像，無偏差、無殘留。

## ✅ V2 Builder fee（北極星指標）——實際入帳，費率正確

- follower 首筆成交拆兩筆部分成交，**兩筆都帶 `builderFee`**：0.018713 / 93.57 = **0.02%**；0.001264 / 6.32 = **0.02%** → 精確等於 f=20（tenths of bps）。
- 生產碼 `HyperliquidAdapter.query_builder_accrued()`（referral state 的 `builderRewards`）實跑正常，全劇本後累積 **0.207739 USDC**。
- 鏈上核准上限 `maxBuilderFee = 100`（0.1%），實收 0.02% 在額度內，無拒單。
- **註記**：本測試中 leader 錢包 == builder 錢包，故累積值含 leader 自身成交產生的費用，非純 follower 貢獻。正式量測北極星時 leader 與 builder 應分離。

## ✅ V3 非託管不變量（實機）

- 引擎全程只用 agent key 簽名（`EnvFileKeyStore` 600 權限強制），使用者主鑰未在任何環節出現。
- agent 為 trade-only：本次所有操作皆為下單/平倉，`ExchangeAdapter` 結構上無 withdraw/transfer（既有結構性測試 + 本次實機無任何提領路徑）。

## F2 ⚠️ M1 測試腳本綁定託管式 onboarding，與 M2 非託管模型不相容

**現象**：跑 `scripts/testnet_modify_probe.py`（T1.3 探針）直接失敗：
```
KeyError: 'no main key for account fbac652a5fb611c1bdc3b9d244cc7e0cc03123662'
  at scripts/testnet_modify_probe.py:110  main_signer = ks.get_main_signer(account_id)
```

**根因**：腳本 `main()` 硬寫 `MacKeychainBackend()` 並取**主鑰**，用途僅為呼叫 `onboard()`（approve agent + approve builder fee）。但 M2 的非託管模型下**我們永遠不持有客戶主鑰**——`EnvFileKeyStore.get_main_signer` 是結構性 `PermissionError`（M2 刻意設計的核心不變量）。

**影響範圍**：僅測試腳本，**生產路徑不受影響**（引擎全程只用 agent key，本次 V1/V2/V3 已實證）。但它意味著：M1 遺留的 testnet 工具在 M2 世界跑不動，而這支正好是使用者待裁決事項的數據來源。

**關鍵觀察**：探針的**核心 A/B 探測邏輯完全不需要主鑰**——只有 setup（onboarding）需要。而 M2 的 onboarding 早已由瀏覽器錢包在鏈上完成，腳本重做一次是多餘的。

**處置**：改造為非託管相容（keystore 可選 + 可跳過 onboarding 改為驗證鏈上前置條件），A/B 邏輯原樣不動。詳見下方 T1.3 數據節。

**教訓（可推廣）**：M1→M2 的模型轉換（託管→非託管）會使「假設持有主鑰」的既有工具靜默失效。建議盤點 `scripts/` 下其他仍呼叫 `get_main_signer` 的腳本，標註其僅適用 M1 錢包或一併改造。

## ✅ V4 Kill switch（回撤熔斷）——完整鏈路實機驗證

手法：把 `COPY_MAX_DRAWDOWN_PCT` 臨時調到 `0.0001`，讓既有的微小回撤（手續費造成 dd=0.00049）觸發**真實偵測路徑**（非僅測檔案機制）。follower 當時持有 ETH 0.043。

| 環節 | 實測結果 |
|---|---|
| 越線偵測 | dd=0.0004894 > 0.0001 → breach ✅ |
| **自動全平**（flatten_on_breach）| ARM 檔記 `closed:["ETH"]`；鏈上確認 follower 無持倉 ✅ |
| ARM 檔鑑識資料 | `tripped_at/current/peak/drawdown_pct/breached/phase=complete/cancelled/closed/failures` 齊全 ✅ |
| 告警 | alerts.log 人類可讀單行摘要，含 re-arm 指示 ✅ |
| **門檻恢復後仍鎖死** | 門檻改回 0.20 再跑 → `tripped=True, skipped` → **無自動 re-arm** ✅ |
| 人工 re-arm | 刪 ARM 檔後再跑 → `tripped=False` 並正常恢復鏡像 ✅ |
| 狀態隔離 | ARM 落在 per-follower 的 `FILET_STATE_DIR` 下（M2 Phase A 的隔離修正實機生效）✅ |

**評價**：符合工程原則 3（安全關鍵失敗大聲、鎖死、要求人工介入），且「鎖死狀態不因參數回復而自動解除」這條特別重要——它防止了「調參數就繞過熔斷」。

## ✅ V5 日報 / TE telemetry

`scripts/copytrade_daily_report.py` 實跑產出 `var/copytrade/reports/2026-07-18.md`：
- 成交配對 10 筆、**中位延遲 9.744s**、taker 滑價中位 0 bp、taker 佔比 1
- Builder fee accrued 增量 **0.279122**
- CSV 對帳：明確回報「無資料，**不得視為相符**」——**誠實標註而非假裝通過**，符合品質底線要求

## 測試方法論註記（非產品問題）

過程中一度觀察到 follower(0.0529) 與記憶中的 leader(0.043) 不符，查證後發現是**我自己的測試干擾**：T1.3 探針 agent 正在同一個 leader 錢包下測試單，leader 實際已變為 0.053，follower 0.0529 = 0.053 × scale 0.9998 後 ROUND_DOWN，**鏡像正確**。
**教訓**：探針類腳本與鏡像驗證不可並行跑同一錢包，否則觀測值互相污染。

## ✅ T1.3 modify builder 歸屬——**有答案了，且問題本身需要重新定義**

（M1 待裁決事項「modify 丟失 builder 歸屬時的政策：容忍漏繳 vs 強制 cancel+place」的數據來源）

### 結論 1：modify **不會**丟失 builder 歸屬

| 組別 | 路徑 | size | notional | expected_fee | actual_delta | ratio |
|---|---|---|---|---|---|---|
| A（對照）| place 可成交單（builder 走 order action）| 0.01 | 18.599 | 0.0037198 | 0.003719 | **0.99978** |
| B（實驗）| place 遠端 GTC → `modify_order` 改價 → 成交 | 0.01 | 18.598 | 0.0037196 | 0.003719 | **0.99984** |

獨立第二次實驗重現（ratio 1.0000），並設**非 modify 的 maker 對照組**（ratio 0.9997）排除「maker 成交本來就不計 builder fee」這個競爭解釋。**信心：高。**

### 結論 2（更重要）：原本設想的風險情境**結構上不可能發生**

HL 的 `batchModify` 帶 **post-only 語意**——同一張單、同一個非穿價價格：modify→`Gtc` 成功、modify→`Ioc` 失敗；穿價 modify 直接被拒（`"Post only order would have immediately matched, bbo was 1860.2@1860.7"`）。

**意即：被 modify 的單只能「掛著等」，永遠無法在 modify 當下吃單成交。** T1.3 原本要量測的「modify 成 taker 立即成交是否丟失歸屬」在 HL 上不存在這條路徑。實際可達的唯一路徑是 modify → 掛單 → 之後以 maker 成交，而該路徑**完整保留歸屬**。

### 對政策裁決的意涵

**現行預設 `modify_policy="modify-first"` 可安全保留**，無需為了 builder 歸屬而強制 cancel+place。「容忍漏繳 vs 強制 cancel+place」這道選擇題**因前提不成立而消解**。

**誠實標註的極限**：全部數據為 testnet ETH、`f=20`、`size=0.01`、單筆成交樣本、且 **builder == 使用者自身地址**（測試錢包配置）。未測：連續多次 modify、modify 放大 size、builder ≠ user 的情形。若要對主網收費路徑有完全信心，建議主網小額覆測一次。

### F3 ⚠️ dry-run 假件與真交易所的 modify 語意分歧（潛在測試信心問題）

- `VirtualBook.modify`（`src/spark/copytrade/executor.py:102+`，dry-run/shadow/單元測試用的假件）docstring 明寫「**就地改單（oid 保留）**」並實作為保留 oid。
- **但實測 HL 在 modify 後會重新配發 oid**（探針記錄：改單前 oid=56668613525，成交 oid=56668613884）。
- 真 adapter `HyperliquidAdapter.modify_order`（`src/spark/exchange/hyperliquid.py:321`）只回 `bool`，**丟棄新 oid**——與假件的契約一致，但該契約不符現實。

**目前為何沒出事**：`loop.py:94` 每輪 `ex.get_open_orders()` **重新讀取**掛單，過期 oid 每個週期自動修正 → **不是 live bug、會自癒**。

**F4 ⚠️ modify 失敗時舊單可能已消失（非原子）**

探針實測：被拒的穿價 modify 發生後，**原本掛著的舊單也一併消失**——`batchModify` 行為近似 cancel-then-place，place 失敗就兩頭落空。

- 引擎現行邏輯（`orders.py:441-446`）：`modify` 回 False → 登記 TTL → 放進 fallback → 稍後 `ex.cancel(coin, oid)` 撤舊單 → 再 place 新單。
- 舊單若已不存在，`cancel` 回 False 被優雅忽略（`if ex.cancel(...): cancelled += 1`），接著照常 place 新單 → **終態正確、會自癒**。
- **但**：(a) `orders.py:398` 註解隱含「modify 回 False ⇒ 舊單仍在」的模型是**錯的**；(b) `cancelled` 計數會低報；(c) 若未來有程式碼依賴「modify 失敗⇒舊單仍在」做決策，會踩雷。

**建議（低風險、非緊急）**：修正 F3/F4 的 docstring 與註解使其符合實測語意；`VirtualBook.modify` 改為**也重新配發 oid**，讓假件與真交易所行為一致（否則單元測試對 oid 穩定性給出假信心）。這兩項是「測試保真度」修正，不改變生產行為，可自行處理；但因涉及引擎核心對帳路徑，仍列出供裁決。


---

## 處置紀錄（2026-07-19，使用者裁決後執行）

使用者裁決：F1 改、F2 加守衛不全改、F3/F4 修。以下為實際處置與驗證。

### F1 → 已修（回撤基準改 perp + 滾動 7 天峰值）
- 新增 `src/spark/copytrade/equity.py`：`perp_equity_view()` 以 `get_account_value()`（perp accountValue，**與 sizing 同一個數字**）為 current；peak = 本地滾動 7 天樣本最大值（原子寫 tmp+replace，壞檔退回 current 不阻斷交易）。保留 hl 原本的 week-window 語意而非終身高水位——後者會讓慢跌後貼著門檻反覆熔斷。
- `killswitch.trip()` 觸發時 `reset_samples(root)`：否則人工 re-arm 後崩跌前的舊 peak 仍在窗內會立刻再熔斷。
- **覆蓋面修正（指揮官驗收時發現初版遺漏）**：`--status` 顯示與 `panic.py` 的 ARM 記錄原本仍讀舊基準——顯示與判定不同基準會讓操作者誤判緩衝。兩者改用 `perp_equity_view(persist=False)`（維持 `--status` 的零寫入契約）。
- 迴歸測試：`tests/test_copy_equity.py` 6 例（含 F1 核心迴歸：perp 500→400 必須算出 0.2 回撤，舊基準只會算出約 0.1）。既有 breach 測試改以樣本播種建立情境。
- **實機驗證**：`--status` 由 `$998.30`（含 spot）→ **`$499.30`**（perp），且執行後無樣本檔（零寫入成立）。
- commits：`d025a79`、`6f908eb`、`99d6827`、`bda19a1`

### F2 → 已加守衛（不做雙模改造）
- `scripts/run_testnet_flow.py` 標註「M1 自有錢包模式專用」，`FILET_KEYSTORE=envfile` 時明確拒絕並指路；`get_main_signer` 的 `KeyError` 包成人話訊息。
- 判定理由：M1 主網 dogfood 仍在待辦，該場景**自有錢包、主鑰在 Keychain 合法**——腳本是 M1 專用工具，不是壞掉的工具；其驗證路徑已被 dashboard onboarding 與改造後的探針取代，雙模改造屬 YAGNI。
- **實機驗證**：`FILET_KEYSTORE=envfile` 執行 → 印出指路訊息、`exit=1`（非 KeyError traceback）。
- commit：`4645a16`

### F3/F4 → 已修（測試保真度）
- `VirtualBook.modify` 改為**重新配發 oid**（對齊 HL batchModify 實測語意），並加測試釘住假件契約本身。
- 既有測試 `test_virtual_book_evolves_place_modify_cancel` 的斷言（`oid 保留`）編碼了錯誤模型，已修正為驗證 oid 重配發；**修正後斷言更嚴格**——多驗了「拿過期 oid 撤單會失敗」這個真實行為。
- `hyperliquid.modify_order` / `orders.py` 補上實測語意註解：oid 重配發、非原子（回 False 不保證舊單仍在）、post-only。明文寫出「安全性依賴呼叫端每輪重讀掛單」這條**結構性依賴**，使其從習慣變成契約。
- commits：`8466482`、`dd16883`

### 最終驗證（指揮官親跑）
- `uv run pytest -q` → **757 passed, 0 failed**；`ruff check src tests scripts` → clean
- 實機：equity 基準 `$499.30`（perp）｜F2 守衛 `exit=1`｜`--status` 零寫入成立

**驗收過程中攔截到的初版缺失**：F1 首版只改引擎主迴圈，遺漏 `--status` 與 `panic.py`（單元測試全綠但實機顯示仍是舊基準）；另發現 panic 有一處重複的 `reset_samples` 且註解誤導 `trip()` 未清理——皆已修正。**教訓：測試綠不等於覆蓋完整，同一語意變更要盤點所有呼叫點。**

---

## I1 處置：資金轉出誤觸發

**使用者裁決（2026-07-19）**：兩者都做——馬上加客戶端警示，同時把 ledger 校正正式排入 public beta。

**已做**：`web/src/lib/copy.ts` 新增 `wizard.fundsWarning` 與 `perf.fundsWarning`，顯示於 onboarding 風險確認步驟與績效頁；`copy.test.ts` 釘住其存在。

**排入 public beta（正式待辦，非註解裡的一句話）**：
- **項目**：出入金 ledger 校正——用 HL 的資金流端點取得 perp 帳戶的存提記錄，從回撤計算中剔除，使「客戶轉出資金」不再被誤判為虧損。
- **為何 closed alpha 先不做**：客戶數少、可用文案警示涵蓋；ledger 端點需先做 research（HL 該端點的欄位與分頁行為未驗證），屬 load-bearing 未知。
- **完成前的殘餘風險**：客戶若忽略警示轉出資金，會被保護性平倉（fail-safe 方向，不會虧錢，但會吃滑價並需人工 re-arm）。

## opus F1 複審的處置總表（2026-07-19）

F1 首版修好了「基準含 spot」，但 opus 對抗性複審用實跑腳本證實**引入了新的不安全**。全部處置如下。

| # | 問題 | 處置 | 驗證 |
|---|---|---|---|
| **C1** | 空樣本＝靜默 fail-open。`peak = max(樣本 ∪ current)` 恆 ≥ current，故既有 `peak<=0` degenerate 告警在新路徑上結構性死掉；檔案遺失／容器無持久化／停機>7天/權限問題 → dd 恆 0 且無聲，`--status` 顯示「健康」 | 新增 `SampleCoverage`／`sample_coverage()`；`evaluate()` 覆蓋不足時發 **critical**（非 warn）；`_load` 的 OSError 與「檔案不存在」分開；`--status` 顯示覆蓋度。**裁決：大聲告警但繼續跟單**（首次部署即拒交易會使系統不可用，而操作者已被大聲告知） | 實跑：檔案遺失／內容毀損兩情境皆發 critical「回撤保護尚未生效」（舊版完全靜默）|
| **C2** | 7 天滾動窗只量「虧損速度」：每 7 天跌 19% × 8 週 → 累計虧 81.5%，**熔斷從未觸發**；且與「20% 回撤保護」的產品承諾語意不符 | 新增「自開始跟單以來高水位」與 `max_total_drawdown_pct`（**預設 0.40**，`COPY_MAX_TOTAL_DRAWDOWN_PCT` 可調）；快速閘（7天窗 20%）與慢速閘任一觸發即熔斷；`trip()`／re-arm 一併重置 | 實跑同一情境：**第 3 週（累計 46.9%）即熔斷**（引擎實際每 60s 取樣，會更貼近 40% 觸發）|
| **I1** | 客戶自 perp 轉出資金被算成回撤 → `flatten_on_breach=True` 直接平倉；客戶端**零警示** | **裁決：兩者都做**——onboarding 風險步驟與績效頁加警示文案（測試釘住）；ledger 校正正式排入 public beta 待辦（含 research 前置說明與殘餘風險） | 前端 87 tests 綠、build 成功 |
| **I3** | `reset_samples` 拋錯會吃掉 kill switch 的總結 critical（位於 ARM 落地與告警之間）| 改為絕不拋例外（`unlink(missing_ok=True)` + try/except → logger.warning）；加測試 | 測試釘住「unlink 失敗時不得拋出」|
| **I4** | 未來時間戳（時鐘跳動／NTP）永不出窗，污染期＝跳動幅度＋7天 | 過濾改 `0 <= now - ts <= window_s`；加測試 | 遠未來高點樣本不影響 peak |
| **I5** | 固定 `.tmp` 檔名，多行程併發可產生撕裂檔 → `_load` 回空 → 回饋成 C1 | tmp 檔名加 `os.getpid()` | — |
| **M1** | `base.py`／`killswitch.py` 的同源契約 docstring 已與實作矛盾 | 兩處更新為新語意 | — |
| **M2** | `persist=False` 的零寫入契約無測試 | 加 2 個迴歸測試（不建檔、不改動既有檔）| — |

**opus 確認無問題的面向**：同源不變量（current 與 peak 都是 `marginSummary.accountValue` 的時間序列，**比舊版更乾淨**——舊版 current 取四窗最新、peak 只取 week 窗，其實是混窗）；多 follower 隔離；`persist=False` 的顯示偏差方向保守不誤導；sizing 未受影響。

### 刻意延後（記錄理由，非遺漏）

- **I2 單筆插針污染 peak**：unrealizedPnl 受 mark price 影響，冷門幣插針可造成假 peak 並在 7 天內導致假平倉。**延到 public beta**，理由：closed alpha 的 leader 交易主流幣種（深度足夠）；失敗方向是 fail-safe（保住本金，代價是時機不佳＋滑價＋需人工 re-arm）；正確修法（分位數或連續確認）會增加保護語意複雜度，不值得在客戶數個位數時引入。**若 leader 名單納入冷門幣種，此項須先做。**
- **M3 樣本檔無上限**：`interval_s=60` → 7 天約 10080 筆 ≈ 354 KB，每輪全量讀寫。closed alpha 規模可接受；public beta 前加降頻或筆數上限。
- **M4 `--status` 直呼 `check_drawdown`**：該路徑無 notifier，結構上呼不了 `evaluate()`；屬 docstring 措辭問題，非行為缺陷。

### 最終驗證（指揮官親跑）
- Python `uv run pytest -q` → **769 passed, 0 failed**；`ruff check src tests scripts` → clean
- 前端 `npm test` → **87 passed**；`npm run build` 成功；`npm run lint` clean
- 實機 `--status` → `$499.30`（perp 基準）＋「⚠️ 回撤保護尚未生效」＋「全期高水位／絕對底線 0.40」三項皆正確顯示

## 追加裁決處置（2026-07-19，使用者第二輪）

### 緊急平倉滑價——一般與緊急分離

**問題釐清**：`close_reduce_only` 以 slippage 算 IOC 限價（平多倉 = mid×(1−slippage)）。原本一般跟單平倉與 kill switch 緊急全平**共用 5%**——市場跳空超過 5% 時緊急平倉**掛不上、部位繼續曝險**。保護最需要成功的時刻，最可能失敗。

**使用者裁決**：要確實平倉。

**處置**：新增 `flatten_slippage`（預設 **0.30**，`COPY_FLATTEN_SLIPPAGE` 可調），與一般 `slippage`（維持 0.05）分離。`ExecutorPort.close_reduce_only` 加 `emergency: bool = False`；`killswitch.trip()` 與 `scripts/panic.py` 的執行器一律傳 `emergency=True`。

**語意澄清（重要，勿誤讀）**：IOC 限價是「**可接受的最差價**」，不是「成交在該價」——實際仍從最佳價開始吃。故 0.30 不代表接受虧 30%，而是「即使跳空 25% 也要出得去」。分離而非全面調寬的理由：一般平倉沒成交無代價（下一輪再試），緊急平倉沒成交＝保護失效。

### I2 單筆插針污染 peak——已實作（原定延後，使用者要求提前）

**修法**：樣本充足時 peak 取**次高值**而非最高值——單筆插針不會成為 peak；真實的持續高點必然有多筆樣本，次高≈最高。

**設計修正（首版規格錯誤，實作後由測試暴露）**：初版寫「>= 2 筆即取次高」，導致樣本少時次高＝最低，**摧毀真實 peak 使回撤歸零**，4 個既有回歸測試失敗。根因：**兩筆樣本無從判斷哪筆是離群值**。改為 `_WICK_GUARD_MIN_SAMPLES = 3`——少於 3 筆退回最高值（該區間覆蓋度告警本就在提醒「保護尚未生效」，不會讓操作者誤以為有保護）。

**四情境驗證（指揮官親跑，確認防護與偵測並存）**：

| 情境 | peak | dd | 熔斷 | 判定 |
|---|---|---|---|---|
| 穩定 1000 → 插針 1400 → 回 1000 | 1000 | 0.0000 | 否 | ✅ 插針被排除，無假平倉 |
| 穩定 1000 → 真跌到 780 | 1000 | 0.2200 | **是** | ✅ 真回撤照樣觸發 |
| 持續 1400（多筆）→ 跌到 1000 | 1400 | 0.2857 | **是** | ✅ 持續高點視為真實 |
| 冷啟動僅 2 筆（1000→800）| 1000 | 0.2000 | 否 | ✅ 退回最高值（dd 等於門檻，判定為 `>` 故不觸發，正確）|

### 最終驗證
- Python **774 passed, 0 failed**；`ruff check src tests scripts` clean
- 前端 **87 passed**
- commits：`4ca4056`（緊急滑價＋I2 初版）、`c126fdd`（I2 門檻修正）

### 流程教訓（值得記入制度）

本輪兩次「照抄我的規格 → 測試失敗 → agent 回報而非改斷言」都證明有價值：
1. **F1 首版覆蓋不全**：只改引擎主迴圈，漏 `--status` 與 `panic`；單元測試全綠但實機顯示仍是舊基準。
2. **I2 首版規格有設計缺陷**：「>= 2 筆取次高」在樣本少時摧毀真 peak——**是我的規格錯，不是實作錯**。

共同教訓：**指令要求「失敗就回報、不准改斷言遷就」是關鍵防線**——否則兩次都會被「把測試改綠」掩蓋成假完成。另：同一語意變更必須盤點所有呼叫點與所有樣本規模的邊界。
