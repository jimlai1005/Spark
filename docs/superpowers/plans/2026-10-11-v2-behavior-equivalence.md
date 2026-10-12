> **狀態（2026-10-12）：驗證完成；結論導致 v2 整體不採用。** 差分測試 `tests/differential/`（131 條）只存在分支 `archive/2026-10-copytrade-safety-net-v2`，main 上沒有。

# v2 行為等價與連續下跌驗證（只加測試，不動程式碼）

> 使用者 2026-10-11 要求：比較修改前（main `7e4b509`）與修改後（v2 `2c9bdfd`）的行為邏輯是否一致；
> 確認本次改動只影響「價格急跌」情境；其他原有行為不變；再繼續下跌時不再反覆開關單；
> 以 f438 的真實歷史驗證。**驗證期間不動既有程式碼，只新增測試與資料夾具。**

## 0. 三個問題 → 可判定的命題

| 問題 | 命題（可機器判定） | 判準 |
|---|---|---|
| Q1 修法前後邏輯一致？ | 在**非急跌**情境（leader 在一輪內沒有成交、價格靜止或緩動）下，新舊兩版對同一劇本產生**相同的動作序列** | 動作日誌逐筆相等（kind／coin／方向／量／價）；價格緩動時允許「決策相同、量差 ≤ 1 格點」並逐筆列出 |
| Q2 只影響急跌？ | 新舊動作出現差異的輪次，**只**落在「快照拍完到 B 段讀自己之間 leader 部位或 scale 已變」的輪次（D3），或 A 段遇到 500／consumed／post-only 被拒（D16–D18） | 差異輪次集合 ⊆ 上述條件成立的輪次集合；反向：條件不成立的輪次 0 差異 |
| Q3 繼續下跌不再反覆開關？ | 連續 N 輪 leader 沿階梯被成交（每輪快照都過期）時，B 段「賣出後下一輪買回」的來回次數：舊 > 0、新 = 0（允許 §6 W2 一格） | 來回次數＝同幣相鄰兩輪 B 段動作方向相反且第二筆量 ≤ 第一筆量 |

**T-A 實測修正（2026-10-11，builder 回報）**：非急跌劇本**不是**逐筆相等——兩類有意或既定的差異會出現，Q1／Q2 改為「差異集合＝預期差異登錄，不多不少」：
- **F1 讀值守衛（D11／D28／D32）**：一邊讀到空部位、另一邊有部位或掛單 ⇒ 需兩個讀點確認 ⇒ 冷啟動（空手 follower＋只有掛單的 leader）第一次掛鏡射單晚一輪；leader 全平後跟平晚一輪（§6 W1）；重啟後同形。測試須登錄跳過輪次，並斷言新版在登錄的輪次之後**追上**舊版（輪後部位與 resting 掛單按 coin／side／px／sz 比對相等，**不比 oid**——新版晚一輪掛單的 oid 序號必然不同）。
- **F2 B 段容差基準**：舊版 `|t−m|/m > tol`（相對目前部位），新版 `m < lo·(1−tol)`／`m > hi·(1+tol)`（相對目標，D3 原文）。tol=0.08 時加倉門檻 8.0%→8.7%、減倉 8.0%→7.4%。平靜時在容差邊界會出現「舊動新不動」或反之（劇本 11）。**驗證期間不改碼**：登錄為預期差異、以新規則的精確閾值斷言新版行為，寫進報告 §Q1，交使用者決定是否把基準改回舊定義（一行等級、需再過一輪複審）。
- 其餘非急跌劇本（5–10、12 追上後、13）必須逐筆相等（不比 oid）。

Q1／Q2 用**合成劇本**在兩版引擎上差分；Q3 用合成連續下跌＋**f438 真實資料**兩路驗證。

## 1. 資料盤點（2026-10-11，Tokyo 主機唯讀抓取，scratchpad `f438/`）

| 來源 | 內容 | 覆蓋 | 限制 |
|---|---|---|---|
| `hl_w1.json` | f438／f6b3／leader fills＋historicalOrders＋state；1 分 K（ETH／BTC／HYPE／SOL） | fills 10-07 01:53→10-11 22:04；leader hist 10-08 11:04→10-11（1411）；f438 hist 10-08 19:26→10-11（2000 上限）；K 線 10-08 ~10:00→10-11 | `historicalOrders` 只回最近 2000；K 線回看上限約 5000 根 |
| `hl.json`／`hl2.json`／`hl4.json`（10-08 抓） | 10-07 事故窗：leader hist 10-05→10-08 03:41；f438 fills 01:53→02:19＋hist 10-07 02:02→10-08；K 線 10-07 12:00→10-08 03:46 與 01:53→02:10（36 根） | 事故窗完整 | 10-07 02:30→12:00 無 K 線 |
| `hl_w2.json` | f438 fills 09-22→09-29（2000 上限截斷）；leader fills 34 | 平靜期 | 09-29→10-06 的 f438 fills 未分頁抓；K 線為空（超出回看） |
| `f438_equity_samples.json` | f438 權益每分鐘（10-04 22:18→10-11 22:17，10078 點，47.1k–60.4k） | 事故窗與之後 | leader 權益歷史**沒有**來源 |

**逐日成交（f438 vs leader）**：

| 日 | ETH 日變化／振幅 | leader fills | f438 fills（taker） | 解讀 |
|---|---|---|---|---|
| 10-07 | −0.2%／1.8%（12:00 後；凌晨急跌無 K） | 61 | 345（218） | 事故日 |
| **10-08** | **−3.9%／7.5%** | 45 | **535（445）** | **繼續下跌日**：taker 成交是 leader 的 10 倍 |
| 10-09 | +0.5%／2.0% | 4 | 109（93） | **平靜日仍有大量 taker**——舊引擎本來就有的來回（疑與 scale 漂移／容差有關，非本次修法標的） |
| 10-10 | +0.7%／1.3% | 2 | 7（5） | 平靜 |
| 10-11 | +1.1%／2.3% | 6 | 39（21） | 平靜 |
| 09-28／09-29 | — | 0（該 leader） | 692／1137（PUMP／NEAR／kPEPE） | **排除**：幣種不屬此 leader，屬 09-29 PUMP 送單價事故期（另案） |

## 2. 工具：版本無關的差分重播 harness（`tests/differential/`）

- `scenario.py`：劇本 DSL 與驅動，**只用兩版共有的介面**：`run_cycle(adapter, ex, settings, notifier, state, root)`（不傳 `sleep_fn`）、`ScriptedAdapter` 子類覆寫 `get_positions`／`get_account_state`／`get_open_orders`／`get_all_mids`（新版的 `get_user_snapshot` 預設實作會串這兩個讀取，舊版直接呼叫；同一劇本兩版都吃得下）、`ActionExecutor(fa, object(), BUILDER, live=True, my_address=…, settings=…)`、`ReconcileState()`、`RecordingNotifier`。`CopySettings` 只傳兩版共有的欄位（`volatility_weight_enabled=False`、`settle_seconds=0`、`risk_controls_enabled=False`、`size_tolerance`、`capital_utilization`／`use_full_equity` 等；先 `git show 7e4b509:src/spark/copytrade/config.py` 核對欄位交集）。
- 劇本＝每輪的 (a) leader 部位／accountValue／掛單，(b) 我方權益，(c) mids 價格路徑；**模擬成交引擎**：兩輪之間若價格路徑（K 線高低或指定路徑）穿過我方 resting 掛單價 ⇒ 該單成交、部位更新、單從簿上消失；B 段市價單立即以 mid 成交。這個引擎是劇本的一部分（兩版共用），不是被測物。
- **急跌劇本的過期快照**：劇本可指定「本輪開頭快照」與「B 段新鮮讀值」兩組 leader 狀態（同 `test_copy_incident_20261007_replay.py` 的 `script(LEADER, snap, fresh)` 做法）；非急跌劇本兩組相同。
- 動作日誌：每輪收 `ex.records`（kind／coin／is_buy／size／px）＋ notifier 紀錄（warn／critical 類別與 dedup_key）→ 正規化 JSON（Decimal 轉字串）。
- `runner.py`：`run_scenario(scenario_name, version_src_dir) -> dict`——以 `subprocess` 跑 `python -c` 載入 `scenario.py`（`PYTHONPATH=<version>/src:<tests dir>`、`PYTHONDONTWRITEBYTECODE=1`），輸出 JSON。舊版 src 由 `git archive 7e4b509 src` 解到 `tmp_path`（測試內做；`git` 本機可用；socket-ban 不影響 subprocess）。新版直接用工作樹 `src`（同樣經 subprocess，避免 import 混淆）。
- `test_differential.py`：對每個劇本跑兩版、比對；差異輪次須滿足 Q2 的條件集合，否則 fail；輸出差異摘要到 `tmp_path/diff_<scenario>.json` 並在失敗訊息列出前 10 筆。
- 兩版對同一劇本的「我方成交模擬」必須一致：模擬引擎只依 resting 掛單與價格路徑，與引擎版本無關；B 段市價單的成交結果反饋到下一輪我方部位（兩版各自演化——這是差分的意義）。

## 3. 劇本

### 3.1 L1 非急跌（Q1／Q2：期望兩版動作日誌**逐筆相等**）
1. 階梯 leader 每輪改價 60 輪、無成交（空手 follower）。
2. 階梯 leader 每輪新增一層 20 輪、無成交。
3. leader 逐層被成交建倉（每 3 輪成交一層，成交發生在**兩輪之間**、快照與新鮮一致）10 層。
4. leader 分批止盈（reduce-only 單逐層成交）至空手。
5. leader 翻向（多→平→空）。
6. 三幣同時（ETH／BTC／HYPE）混合 1–4。
7. follower 權益變動（入金 +20%／出金 −20%）⇒ scale 變 ⇒ 兩版同樣調整鏡射單量與部位。
8. leader 撤掉全部掛單再重掛。
9. leader 市價開倉（兩輪之間）⇒ 兩版 B 段同樣加倉。
10. 價格緩動（每輪 ±0.1%）、leader 不動 20 輪 ⇒ 兩版 0 動作（容差內）；若有差異逐筆列出並說明（D3 用新鮮 scale 的量差）。
11. 容差邊界：leader 部位變動恰在 `size_tolerance` 邊界 ±1 格點。
12. 重啟：第 10 輪換新 `ReconcileState`（舊版無記憶檔）⇒ 兩版後續動作相同（非急跌、讀值真實）。
13. 寫入失敗（非 500）：modify 被拒（margin）⇒ 兩版同走 fallback 撤＋重掛。

### 3.2 L2 急跌（Q2／Q3）
14. **10-07 單輪**：以 `test_copy_incident_20261007_replay.py` 的預註冊數字（f6b3e7：ETH 減 0.3510／HYPE 3.09／BTC 0.01593）跑兩版 ⇒ 舊版過賣、新版 `hi == t1`；再以 f438 的比例（權益 53k、util 0.50）重跑。
15. **連續下跌 N=12 輪**：每輪 leader 沿階梯再被成交一層，**每輪快照都比 B 段新鮮值舊一層**（符合實況：成交集中在輪與輪之間與 A 段期間）；價格每輪 −0.8%。度量：每幣 B 段來回次數（賣後下一輪買回）、累計 taker 名目、終點部位 vs leader×scale 偏差。期望舊版來回 > 0、新版 = 0（§6 W2 一格例外要單獨計數）。
16. 連續下跌後**反彈** 6 輪（leader 止盈逐層）：新版無反向過賣。
17. 連續下跌＋HL 500 間歇（每 3 輪一次 modify 500）：舊版整輪中止／五連敗；新版繼續（D16–D18）——這是**預期差異**，列入 Q2 允許集合。
18. 連續下跌＋假空讀（每 4 輪一次 bogus 空讀）：舊版重開／翻倍；新版跳過。預期差異。

### 3.3 L3 f438 真實資料（Q3 經驗驗證；夾具進 repo）
- 夾具 `tests/differential/data/f438_2026-10.json.gz`：從 scratchpad 抽**最小欄位**（fills：time／coin／side／sz／px／crossed／startPosition／oid；hist：oid／coin／side／sz／origSz／limitPx／timestamp／statusTimestamp／status／reduceOnly；K 線：t／o／h／l／c；equity_samples：t／v），只保留三個窗：W-crash 10-07 01:30–03:00、W-decline 10-08 00:00–23:59、W-calm 10-09 00:00–23:59。目標 < 500 KB。生成腳本 `tests/differential/data/build_fixture.py`（讀 scratchpad 路徑，記錄來源檔 sha256 進夾具 metadata）。
- **重建 leader 時間線**（每分鐘一輪）：部位＝fills 的 `startPosition + sz` 累積；resting 掛單＝hist 中 `timestamp ≤ t < statusTimestamp`（filled／canceled）或 `status==open`；**scale**＝從 f438 與 leader **同時間同幣的鏡射成交**反推（f438 sz ÷ leader sz，取每小時中位數；波動權重併入），再以 f438 權益（equity_samples）回推 leader accountValue 供 adapter 使用——因 leader 權益歷史無來源，這是**近似**，寫進限制。
- **f438 舊引擎的真實行為**＝其 fills：按小時統計 taker 成交筆數／名目、來回次數（同幣相鄰 taker 成交方向相反）。
- **新引擎重播**：把重建的 leader 時間線＋f438 權益＋K 線價格路徑餵 harness（只跑新版），統計同一組指標。
- 判準：W-decline 新版來回次數與 taker 名目**顯著低於** f438 真實（舊版）；W-calm 新版 taker 名目 ≤ f438 真實（若 f438 真實在平靜日仍有大量 taker 而新版沒有，**要分析那是本次修法改掉的、還是重建誤差**——兩種結論都要寫明證據）；W-crash 新版無反向過賣。
- 10-07 02:30→12:00 無 K 線、09-29→10-06 fills 未分頁：寫進限制，不硬補。

## 4. 產出
1. `tests/differential/`（harness＋劇本＋夾具＋測試），全部離線、預設納入 `uv run pytest`（git archive＋subprocess 總時間目標 < 60 s；超過則加 marker `differential` 並在 README 說明）。
2. 報告 `docs/superpowers/research/2026-10-11-v2-behavior-equivalence.md`：Q1／Q2／Q3 逐題結論（數字可追溯到測試輸出與夾具）、差異輪次清單與歸因、f438 三窗指標表（舊真實 vs 新重播）、**平靜日 taker 成交的歸因**（本次修法 vs 既有 scale 漂移）、資料極限。
3. 不改 `src/`、不改既有測試；夾具生成腳本只讀 scratchpad。

## 5. 任務

### T-A `@inline`（builder）── harness＋L1 劇本 1–13
- 檔案：`tests/differential/{__init__.py,scenario.py,runner.py,scenarios_l1.py,test_differential_l1.py}`。
- 開工前：`git show 7e4b509:src/spark/copytrade/config.py`、`…/exchange/fakes.py`、`…/copytrade/loop.py` 核對共有介面；舊版 `run_cycle` 無 `sleep_fn`，settle 用 `settle_seconds=0`。
- **主線程裁決（2026-10-11，builder 回報 F1／F2）**：採 builder 選項 1——`scenarios_l1.py` 每劇本附「預期差異登錄」（輪次＋歸因 `F1`／`F2`），測試斷言實際差異輪次集合 == 登錄集合；F1 登錄輪之後斷言新版追上（部位＋resting 掛單 by coin／side／px／sz，不比 oid）；F2 登錄輪以新規則閾值精確斷言。比對一律**不比 oid**、記錄欄位取兩版交集。s01／s02 若去掉 oid 後仍 60 輪不同，要找出根因回報（不得登錄為「整段不同」）。新增 s05b：leader 整帳戶全空的翻向（多→全平→空），F1 登錄全平那一輪。不採暖啟動藏差異。harness 的 `PYTHONHASHSEED=0`、import 前 `time.sleep` no-op、`get_active_asset_leverage` 固定 `(5, True)`、`sim.py`／`scenario.py` 拆檔皆接受；README 要寫這些。
- 驗收：13（＋s05b）個劇本的差異集合 == 預期差異登錄；非登錄輪逐筆相等（不比 oid）；`uv run pytest tests/differential -q` 全綠；ruff；3.11；總時間印出。
- 自我檢查（防假陽性）：故意在新版副本改一行 B 段邏輯（例如 `hi` 改用 `t0`）確認劇本 3／9 會紅——證明差分真的在比新版。

### T-B `@inline`（builder）── L2 劇本 14–18
- 檔案：`tests/differential/{scenarios_l2.py,test_differential_l2.py,metrics.py}`。
- 驗收：14 舊版過賣／新版 `hi==t1`；15 舊版來回 > 0、新版 = 0（列出每輪 B 段動作表）；16 新版無反向過賣；17／18 差異輪次全部落在允許集合；報表 JSON 落 `tmp_path`。

### T-C `@inline`（builder）── f438 夾具＋重建＋重播
- 檔案：`tests/differential/data/{build_fixture.py,f438_2026-10.json.gz}`、`tests/differential/{reconstruct.py,test_f438_replay.py}`。
- 驗收：夾具 < 500 KB、metadata 含來源 sha256 與三窗邊界；重建的 leader 部位終點與 `ld_state` 當時值對得上（10-11 22:17：BTC 0.00172／ETH 0.2189／HYPE …，允許 1 格點）；scale 反推的每小時中位數列表；三窗指標表（舊真實 vs 新重播）；判準見 §3.3。

### T-D（主線程）── 報告＋complete
- 讀三個任務的輸出寫報告；主線程親跑 `uv run pytest tests/differential -q` 與全套；派 reviewer（opus）審 harness 的「兩版吃同一劇本」是否真的成立（最大風險：劇本偷偷用了新版才有的介面，或模擬成交引擎對兩版不公平）。

## 5a. T-C 實測與裁決（2026-10-11）

- **leader 掛單歷史缺口 10-08 03:41–16:52**（非 plan 寫的 11:04；11:04 是一張長壽 reduce-only 單的掛單時間）：HL `historicalOrders` 只留最近 2000 筆，10-08 03:41 抓的檔與 10-11 抓的檔之間永久遺失；Tokyo journal 只有 WARNING 以上、state 目錄無逐輪快照 ⇒ **無法補回**。10-08 15–16 時（ETH 2530→2434、f438 真實 taker 128 張）整段在缺口內 ⇒ W-decline 重播只覆蓋當日 taker 的 21%（43／204）、leader 成交 4／45。**主線程裁決：接受缺口並標明；不用 f438 maker 成交價合成階梯（會循環論證）；該段改以「f438 真實成交特徵」（舊版實際行為：taker 筆數／名目／來回、scale 路徑）呈現，不做新版重播；Q3 的「繼續下跌」證據以 W-crash（舊版重播重現真實過賣、新版 `hi==t1`）＋合成 L2 為主，W-decline 重播降為旁證。**
- scale 兩種來源：`pairs`（規格：同幣同向鏡射成交比；W-calm 全天只 3 對、decline-2 只 3 對）、`quiet`（leader 5 分鐘無成交時的 f438÷leader 部位比，取自 f438 實際行為 ⇒ W-calm／decline-2 的新版重播**不是獨立預測**）。接受兩種並列，報告寫明。
- 重建驗證：leader 三幣部位在 10-11 22:17 與 `ld_state` 逐幣相等、部位鏈斷點 0；f438 側亦一致。
- **W-calm 歸因（真實 taker 43 張）**：67% 名目（31 張、11 群）是 **scale 漂移**——三幣同一秒各下一單、部位比 ±9% 交替（195.9→178.7→194.8→178.1…）剛好跨過 8% 死區；新版重播在 quiet 模式產生 37 張、36 張落在真實群 ±2 分鐘內 ⇒ **此類來回不是本次修法的標的、修法前後都存在**（候選另案：scale 死區加滯後／波動權重 W4）。leader 成交相關的 6 群：舊版重播在 14:07／14:20 各一次過期快照反向動作，新版沒有，真實 ±2 分鐘內也沒有 taker。
- commit `d0baf17` 的 Co-Authored-By 為 Sonnet 5.5（builder 實際模型），不 amend。

## 5b. 進度與結果（2026-10-11）

| 任務 | commit | 主線程驗收 | 備註 |
|---|---|---|---|
| T-A harness＋L1 | de579d9 | 24 passed；防假陽性：關加倉 ⇒ 8 紅；`hi→t0` 在 L1 等價（交 L2） | F1／F2 發現 ⇒ §0 修正、預期差異登錄 |
| T-B L2 | c99e916 | 61 passed；`hi→t0` ⇒ 13 紅 | 字面版 s15a 無來回 ⇒ 改 bursty；控制組 s15c／s17c／s18c |
| T-C L3 f438 | d0baf17 | 79 passed；r5／r6 探針皆綠；夾具 170 KB | 掛單歷史缺口 03:41–16:52（§5a）；W-calm 67% 為 scale 漂移 |
| T-E s19 | 9586028 | 91 passed | leader 不動、scale 震盪：兩版來回 5／4（差 F2 輪）⇒ 非 v2 範圍 |
| 複審（harness 公平性） | — | reviewer（opus）：harness 公平；**Critical** W2 未行使且新版劣化（主線程親跑重現）；W：`lo=min` 無測試、harmful 套套邏輯、L3 對 30 秒敏感、02:01 校準說過頭、Q2 措辭、D16/D18 未執行 | 報告 §7／§8；W2 修法交使用者 |
| T-F s20–s22＋前提 | 31358b6 | 129 passed；M5 變異 ⇒ 3 紅 | s20 c＝[2,1,3,2,1,2]；3 條 s19 全套紅、3.11 不決定 ⇒ T-G |
| T-G harness 修正 | 848eba6 | 131 passed（3.14／3.11）；s19＋leader_perf_ratios 配對 20 passed；全套見報告 | 根因：全域 Decimal prec 外漏；`hash(None)` 3.11 依位址 ⇒ **引擎 A 段跨幣順序在 3.11 不決定**（未改 src） |
| 報告 | — | `docs/superpowers/research/2026-10-11-v2-behavior-equivalence.md` | W2／F2／`_slot_key` 待使用者裁決 |

## 6. 不做／限制
- 不改 `src/`、不改既有測試、不部署。
- 09-28／09-29 的 PUMP／NEAR／kPEPE 成交（非此 leader、另案事故）不納入。
- leader 歷史權益無來源 ⇒ scale 以成交反推（近似）；10-07 02:30–12:00 無 K 線；09-29→10-06 f438 fills 未分頁。
- W4（波動權重政策）不在範圍；平靜日 taker 成交若歸因為既有 scale 漂移，只記錄不修。
