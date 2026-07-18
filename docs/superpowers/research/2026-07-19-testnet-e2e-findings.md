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

