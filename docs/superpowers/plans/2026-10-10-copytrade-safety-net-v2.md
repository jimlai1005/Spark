> **狀態（2026-10-12）：未採用。** 第二版修法（分支 `archive/2026-10-copytrade-safety-net-v2`，T0–T13、複審 7 輪達 0 Critical／0 Warning）經行為等價驗證後，使用者決定不採用：顯著改變原有行為（F1／F2／W2 劣化）且未處理 10-08 來回的主體（scale 震盪）。程式碼從未合併或部署，正式機維持事故前版本。見 `docs/superpowers/research/2026-10-11-v2-behavior-equivalence.md`。

# Plan v2：copytrade 引擎——2026-10-07 暴跌事故的修法（重新規劃）

> 狀態：**草稿，待使用者取得共識後才派工**（2026-10-10，Fable 5.1 主線程撰寫）。
> 這是第二版。第一版分支 `archive/2026-10-fresh-leader-read-v1`（57 commits、7 輪 opus 複審）
> 已被使用者整體放棄：**不帶任何程式碼**，只把它的失敗經驗寫成本 plan 的裁決與檢查表。
> 事故證據與回顧文件也只在該分支：`docs/superpowers/research/2026-10-09-crash-0207-stale-snapshot-walkthrough.md`、
> 分析工具 `docs/superpowers/research/2026-10-09-crash-review-tools/`（需要時 `git show archive/2026-10-fresh-leader-read-v1:<path>`）。
> 基準：`main@7e4b509`（＝正式機 10-03 部署版 3994e35 之後的 docs 提交；引擎程式碼相同）。

---

## 0. 第一版為什麼失敗（必讀，避免重蹈）

| 教訓 | 本 plan 的對策 |
|---|---|
| 「安全網扣掉落後鏡射單」每輪修補都帶出新維度（價位→量→A 段容差基準→szDecimals 格點→flip→分群），6 輪都沒收斂 | **不做鏡射單扣法**。改用「增減方向不對稱」規則（D3）一次定義 B 段的比較基準，不需要看掛單 |
| 主線程逐輪局部裁決，沒有先把比較規則的全部維度定義清楚 | §3 一次列完：兩個時點、兩個 scale、增／減／flip／flatten 四種動作各用哪個目標、死區、格點、守衛 |
| 保留項也有錯（D17 用 `net.get("skipped")` 判整輪略過，但正常路徑那是清單） | 回傳形狀用**顯式布林鍵**（`b_phase_skipped: bool`），測試用真實回傳形狀 |
| Haiku 做 @sdd 時為了讓自己的測試過而偏離 plan（假檔名、漏重讀） | 本 plan **不派 Haiku**；全部 `builder`（Sonnet 5.5）；純函式也要用 plan 給的真值表寫測試 |
| reviewer 證據一次假陰性（macOS `sed '0,/re/'` 不生效） | 主線程重跑 reviewer 證據時，先確認變異真的套用（`git diff --stat` 非空）再看結果 |
| 事故重播測試改成「容差比較」後把不在格點的量寫成預期值 | 重播測試的 size 斷言一律用**精確值**，並加「所有送出 size 都在 szDecimals 格點」的通用斷言 |
| 變異驗證兩種假陰性：(1) `sed` 命中註解／docstring 而非程式碼行（主線程踩兩次）；(2) macOS 上同長度、同秒內的變異會沿用上一個變異的 `.pyc`（T6 builder 踩到） | 每次變異：套用後**印出被改的那一行**、`git diff --stat -- src` 非空、`PYTHONDONTWRITEBYTECODE=1` 並清 `src` 下 `__pycache__`，再跑測試 |
| 匯出副本（`git archive`＋`.venv` symlink）跑測試時，`import spark` 載入的是**真實 repo 的 src**（editable 安裝；`pyproject` 的 `pythonpath=["."]` 不含 `src`）——在副本 `src/` 做的變異根本沒被跑到、每個都「存活」；`tests/test_script_default_paths.py` 6 條會因路徑錨到真實 repo 而假紅（T10 驗收時主線程踩到） | 副本裡一律 `PYTHONPATH="$PWD/src"`；每次變異前先印 `python -c "import spark.copytrade.fresh_read as m; print(m.__file__)"` 確認路徑在副本內。複審 1–3 的 reviewer 變異計數**未驗證**是否在此設定下取得，只當參考；builder 與主線程的變異都在真實工作樹做，不受影響 |
| 修法範圍膨脹（T13–T18 都是修修法） | 完成定義（§9）：0 Critical；Warning 修完再審；**五輪**仍有 Warning 就停下問使用者；plan 外的裁決一律寫回本檔 |

---

## 1. Issue 評估（在 `main@7e4b509` 上重新確認）

| # | Issue | 證據 | 嚴重度 | main 現況 |
|---|---|---|---|---|
| I1 | **部位安全網（B 段）用一輪開頭的 leader 部位與 scale，對上新鮮的自己部位**。連環成交時 leader 已長大，B 段把自己當成多了 2.5 倍而市價過度賣出，下一輪買回，反覆來回 | 10-07 02:01Z：f6b3e7 第一輪賣 ETH 0.7226（新鮮目標只需減 0.351）、HYPE 5.87（應 3.09）；f438 同形，估損 $0.5k–1.9k。`loop.py:415-425` `_safety_net` 只重抓自己（`:418`），`leader_positions`／`scale` 來自 `:319`／`:387` | **Critical**（真實損失） | 未修 |
| I2 | **掛單對帳（A 段）對已成交的單 modify 被拒後退回「撤單＋重掛」**，把早已成交的單重新掛出；重掛被 post-only 拒時仍進 settle 補單並發誤報 CRIT | 10-07 02:01Z journal `Cannot modify canceled or filled order`×2、`already canceled, or filled`×2、`historicalOrders` 8 筆 `badAloPxRejected`；若價格沒穿過，重複單會被接受＝約 +0.48 ETH 暴險（運氣擋住）。`orders.py:757-762` modify False ⇒ fallback；`:770-785` fallback 一律重掛 | **Critical**（重複下單路徑） | 未修 |
| I3 | **HL `/exchange` 回 500 'null' 時 `modify_order` 拋例外，整輪中止**（含安全網），五連敗退出 | 10-07 15:53–15:57Z f6b3e7 5/5 退出、f438 4/5。`resilience.py:140-142` modify 單次嘗試；`_TRANSIENT_MARKERS`（`:29`）只有 502/503/504；`orders.py:757` 無 try；`loop.py:490-496` 五連敗 `SystemExit` | High | 未修 |
| I4 | **讀值壞掉被當成歸零**：(a) leader `account_value≤0` ⇒ `compute_scale_factor` 回 0 ⇒ 撤光掛單；leader 部位讀空 ⇒ `sync_positions` 第二段把自己全平。(b) **自己的部位**讀空 ⇒ B 段把每個 leader 幣當新開倉再開一次 ⇒ 曝險翻倍 | 工程原則 #1 事故 #4／#6 同型；v1 第 7 輪探針：已持 4.0、讀空 ⇒ 再買 4.0。沒有發生過的證據，但路徑存在 | High（潛在、量級＝整個部位） | 未修 |
| I5 | **帶明確數量的部分減倉 reduce-only IOC 被當冪等重試**：減倉已成交但回應 5xx ⇒ 再減一次 | `resilience.py:139-141` `idem = bool(reduce_only)`；`hyperliquid.py` `close_reduce_only` 走 `order(reduce_only=True)` | Medium（有界：最多再減一次，下一輪加回；5xx 才觸發） | 未修 |
| I6 | **沒有逐輪紀錄**：崩盤當下 scale 5.72→3.4 怎麼拆、BTC 目標為何對不齊都無法回答；root logger 停在 WARNING，INFO 不進 journald | v1 回顧文件 §6 | Medium（可觀測性） | 未修 |
| I7 | 五連敗 CRIT 文案「引擎停止，需人工介入」不實——unit `Restart=on-failure`，10 秒自動重啟 | `loop.py:494`；journal 15:57:12 自動拉起 | Low | 未修 |

**不在範圍（明列，避免再膨脹）**
- W4 波動權重／leader 有效槓桿造成的政策性降倉（風險政策，另案）。
- **鏡射單落後（v1 的 W1）**：leader 進場單在 A 段成交、我方同價鏡射單排隊落後 ⇒ B 段若用新鮮目標加倉會多一格。本 plan 用 D3 的不對稱規則讓 B 段的**加倉**仍以開頭快照為準（＝main 現行時機），所以**不會發生**，不需要任何掛單扣法。
- A 段送出的部分量 reduce-only **GTC 掛單**仍冪等重送（v1 複審 5 S2）：方向去風險、下一輪撤多餘單自修 → backlog。
- unified account 的 perp `accountValue` 隨自己保證金浮動造成 scale 抖動 → 部署後以 trace 觀察，不改碼。
- 撤單延遲造成的多持一格（leader 撤單後 follower 晚一輪才撤）。
- **leader 全平的跟平延遲一輪**（複審 1 W1，使用者 2026-10-09 選定的兩輪確認規則的直接後果）：leader 平掉**最後一個幣**後，第一次讀到空被判可疑 ⇒ 該輪（或 B 段）跳過，下一輪確認後全平；延遲約 60 秒＋2×2 秒重讀。leader 若仍持有其他幣，A 段期間平掉的幣當輪即 flatten（D5）。接受理由：讀空誤判成全平會把自己全平（10-07 反方向的另一種錯），兩輪確認是刻意的保守；代價寫進 RUNBOOK 讓值班者知道。
- **自己帳戶的外部手動交易**（複審 2 探針的「鏡射單成交」模型其實是「部位憑空出現、簿上沒有任何掛單消失」）：owner 在 follower 帳戶手動下單不在守衛涵蓋範圍——沒有任何可觀測事件（寫入嘗試、進場掛單消失／縿量）能告訴引擎帳戶變了；若接著連續兩輪 HL 回假空讀值，仍會按目標重開。copytrade 帳戶本來就假設只有引擎在交易；寫進 RUNBOOK 當使用須知。真實的鏡射單成交（掛單在簿上 ⇒ 消失）由 D32(ii) 涵蓋，T9 測試 (a)／(d2)／(d3) 鎖住。
- ~~modify 換 oid 後立即成交的時窗~~（T10 接受 → **複審 4 C1 推翻**：時窗是整個輪距、且 settle 已重讀簿可觀測；主線程誤判 ⇒ D36 修掉，不再是殘餘）。
- **部分成交後我方撤掉剩餘量**（複審 4 W1，使用者 2026-10-11 裁決選 R 時接受）：開頭讀簿到 A 段撤單之間（數秒）鏡射單部分成交、接著我方因 leader 撤單而撤掉剩餘量且撤單 ok ⇒ 該 oid 不在 `after` 簿、也無 `sz` 變小的證據 ⇒ 這次部分成交不可觀測（HL 撤單回應不含已成交量）；要造成傷害需**跨兩輪連續三次**假空讀（探針 R4-8 0.2→0.4）。**同形的改單變體**（複審 5 觀察）：舊 oid 在開頭讀簿後部分成交、接著被我方改單換掉（新 oid 的量＝改後量，簿上無部分成交痕跡）⇒ 同樣不可觀測，同屬此項。
- **價格鏈狀相近的階梯下 D40 配對可能配錯張**（複審 7 S1，接受）：`_match_new_oids_to_written` 先到先得、價格容差 `px_rel_tol` 不具遞移性，三層以上價差小於容差時中間層可能搶走低層的 spec，低層新 oid 退回 `origSz` 規則。要漏標需同時：HL 保留改前 `origSz`（B2，未實測）＋改單加量 ≥ 成交量＋同一輪兩張容差內部分成交＋B 段一次假空讀；reviewer fuzz（B1 約 3 萬組 0 漏、B2 最壞 `orig ≤ 剩餘量` 37–49 組漏，**單張成交 0 組**）。最壞多開一次鏡射量。候選修法：以價格完全相等分群或最大配對；列部署後 backlog。
- **settle `after` 讀到 B 段讀自己之間的次秒窗**（D36a）：剛掛／改好的進場單若在這個窗內成交，當輪 B 段沒有證據；下一輪開頭基準比對會補偵測（oid 消失）。前提：settle 讀簿看得到 resting 單（settle 驗證本就依賴）。結構性解法是每輪讀自己的成交紀錄（`userFillsByTime`），列為部署後的候選 plan，不納入 v2。
- **D32／D35 的代價**：只有「真實成交／部分成交／外部撤單」發生的那一輪，空手 follower 的自己側讀值多一次重讀＋2 秒並跳過該輪（接著兩輪確認）；我方自己的改單／撤單不觸發（D35）。
- **減倉方向的鏡射單落後**（複審 1 W2）：leader 的限價減倉單在 A 段到 B 段之間成交、我方同價 reduce-only 鏡射單仍掛著 ⇒ B 段依 `t1` 市價減到位、鏡射單之後成交 ⇒ 多減一張鏡射單量、下一輪加回（先去風險再買回；一張 taker 來回）。不改 `hi = t1`：若改用 `max(t0,t1)`，leader 市價減倉時我方會晚一輪才去風險，更差。部署後的驗收指紋要排除這個情境（看 trace 的 decision 行是否伴隨同價 RO 鏡射單）。
- `place_outcome`／`cancel` 拋例外仍中止整輪（只有 modify 納入 I3；cancel 因 D5 會重試）。

---

## 2. 目標

修 I1–I7。每一項的驗收都要能用 10-07 的真實數字重播（§7）並通過變異驗證。**不改任何風險政策、不改 scale 公式、不改 A 段配對語意。**

---

## 3. 裁決表（一次定義完；builder 遇歧義以此為準，plan 沒寫的先回報主線程）

### 3.1 B 段比較基準（I1 ＋ v1 W1 的根本解法）

| # | 裁決 |
|---|---|
| D1 | B 段（部位安全網）執行前，**重讀 leader 快照與自己快照**（各一次 `clearinghouseState`，合併取部位＋帳戶，見 D9），並以新鮮 leader 權益／名目、新鮮自己權益、**開頭的波動權重**重算 `scale_fresh`。開頭快照的 `scale_snapshot` 保留。 |
| D2 | 每幣兩個目標：`t0 = round(|leader_snapshot_szi| × scale_snapshot)`（開頭）、`t1 = round(|leader_fresh_szi| × scale_fresh)`（新鮮），皆以該幣 `szDecimals` **ROUND_DOWN**。方向各取各自快照的符號。 |
| D3 | **增減方向不對稱**：令 `lo = min(t0, t1)`、`hi = t1`、`m = |自己新鮮部位|`。<br>• `m > hi × (1 + size_tolerance)` ⇒ **減倉到 `hi`**（量 `m − hi`，reduce-only，`full_close=False`）。<br>• `m < lo × (1 − size_tolerance)` ⇒ **加倉到 `lo`**（量 `lo − m`，market）。<br>• 否則不動。<br>理由：減倉用新鮮目標＝10-07 修法（不多賣）；加倉只到 `min(t0,t1)`＝不超過開頭快照（與 main 時機相同，鏡射單落後時不會補那一格）、也不超過新鮮（leader 在 A 段減倉時不會先加再減）。`lo ≤ hi` 恆成立，死區非空。 |
| D4 | **方向**：leader 新鮮方向 ≠ 開頭方向（A 段期間翻向中）⇒ 本輪該幣**跳過**（decision branch `direction_changed_in_cycle`），下一輪兩個快照一致再做。leader 方向一致但 ≠ 自己方向 ⇒ flip：先全平（`full_close=True`）再開到 `lo`（開倉腿屬加倉，用 `lo`）。 |
| D5 | **flatten**（leader 新鮮快照無此幣、自己有）：全平（`full_close=True`）。leader 開頭有、新鮮沒有＝A 段期間平倉，照 flatten（屬減倉方向，用新鮮）。 |
| D6 | **min_notional gate**：加倉／減倉的校正量名目 `< min_order_notional` ⇒ 跳過並 warn（沿用現行 `min_notional_adjust`）；全平腿不 gate（現行 D5 硬規則）。 |
| D7 | **格點**：所有送出的 size 必須等於 `_round_size(size, szDecimals)`；`t0`／`t1` 已捨入、`m` 來自交易所，差額自然在格點上。在 `ActionExecutor.market_open`／`close_reduce_only` 加**斷言**（dev 期 raise，live 期 round-down 並 `logger.error`——不得送出不在格點的量）。 |
| D8 | `no_new_exposure`（成本熔斷／暫停）語意不變：加倉與 flip 開倉腿跳過，減倉／全平照做。`protected` 語意不變。 |

### 3.2 讀值守衛（I4）

| # | 裁決 |
|---|---|
| D9 | 新增 `UserSnapshot(positions, state, fetched_at_ms)`，一次 `user_state` 同時取部位與帳戶（同源同時點）。`ExchangeAdapter.get_user_snapshot` 給非抽象預設實作（串既有兩方法），`HyperliquidAdapter` 覆寫為單一 HTTP。 |
| D10 | **可信度真值表**（leader 與自己共用同一函式）：<br>`positions 非空 ∧ av>0 ∧ ntl>0` ⇒ OK；`positions 非空 ∧ (av≤0 ∨ ntl≤0)` ⇒ INVALID；`positions 空 ∧ (ntl>0 ∨ av<0)` ⇒ INVALID（矛盾）；`positions 空 ∧ ntl==0 ∧ av≥0 ∧ (上次有部位 ∨ 對方有部位或掛單)` ⇒ SUSPICIOUS_FLAT；否則 OK。 |
| D11 | **流程**：第一次非 OK ⇒ 等 `fresh_read_retry_s`（預設 2）重讀一次；OK ⇒ 採用；SUSPICIOUS_FLAT ⇒ `consecutive_flat += 1`，`≥2`（連續兩個讀取點重讀後仍空）⇒ 當真（leader 空＝跟平；自己空＝沒部位）；否則跳過；INVALID／例外 ⇒ 跳過。 |
| D12 | **跳過的範圍**：開頭 leader 讀值跳過 ⇒ **整輪**不做對帳與校正（讀值同時決定 scale 與期望掛單量）；開頭自己讀值跳過 ⇒ 同樣整輪（我方部位決定 reduce-only 上限）；B 段任一快照跳過 ⇒ 只略過 B 段。跳過不是例外，不計入連敗。 |
| D13 | **輪級計數**：`FreshReadState.skipped_in_cycle` 由任一讀取點 skip 設 True；每輪結束 `end_cycle` 累計 `consecutive_skips`（有跳過 +1，否則歸 0）；`≥ warn_after`（3）warn、`≥ crit_after`（10）critical（去重）。設定欄位 `fresh_read_retry_s／warn_after／crit_after` 有預設，env 不需新增。 |
| D10a（T1 回報補充，2026-10-10） | 真值表補一列：`positions 空 ∧ ntl < 0` ⇒ INVALID（名目不可能為負；T1 照字面歸 OK，T3 改判並補測試）。 |
| D13a（T1 回報補充） | `FreshReadState` 是**單一讀取對象**的記憶（`consecutive_flat`／`last_had_positions`），leader 與自己各持一個（`ReconcileState.fresh_leader`／`fresh_me`）；輪級跳過帳只有一份：T3 在 `end_cycle` 前把兩個 state 的 `skipped_in_cycle` 以 OR 併到 `fresh_leader`（指定為 ledger），`consecutive_skips` 只在 ledger 上累計。 |
| D11a（T3 回報，主線程裁決 2026-10-10） | 空讀值**已確認**後不再每輪重讀：`read_with_confirmation` 在 `state.consecutive_flat >= 2` 且本次仍為 SUSPICIOUS_FLAT 時直接採用（reason `confirmed_flat`），不 sleep、不重讀；任何一次讀到部位（`last_had_positions=True`）即重新武裝。否則空手 follower 只要 leader 有掛單，每輪都多一次 HTTP 與 2 秒等待。在 T4 開頭實作＋測試。 |
| D12a（T3 回報） | kill switch／成本熔斷 tripped 的輪次在讀取前就 return，不經 `begin_cycle`／`end_cycle`，輪級跳過計數不變——符合「中途 return 不計」，明寫於此。 |
| D10b（T4 回報，主線程裁決 2026-10-10） | **自己側**守衛的 `counterparty_has_exposure` 在開頭與 B 段**都用 `bool(leader_orders)`**（開頭快照的 leader 掛單）。T4 第 5 點原文（B 段用「leader 快照有部位 or 掛單」）會讓空手 follower 在開頭判 OK（歸零）、B 段判可疑（+1）來回震盪，`consecutive_flat` 永遠到不了 2 ⇒ 永遠開不了第一個倉。leader 側 B 段維持原文（自己新鮮有部位 or `my_orders`），兩個讀取點依據一致，無此問題。 |
| D7a（T4 回報） | 格點斷言只在 adapter 有 `get_size_decimals` 時執行（`getattr(..., None)`；缺者＝測試替身，略過並 `logger.debug` 一次）；`STRICT_SIZE_GRID` 亦同。 |
| D20b（T4 回報） | `killswitch.py` 約 792 行的 `ex.close_reduce_only(..., emergency=True)` 加 `full_close=True`（kill switch 恆為全平；雖有外層 `resilient_run` 重試，仍應正確宣告）。 |
| D23a（T4 回報） | `snapshot_age_s = (safety_net_start_ms − leader_snapshot.fetched_at_ms) / 1000`＝B 段執行時開頭 leader 快照的年齡（10-07 約 28 秒）。v1 的驗收指紋「<5 秒」在 v2 不適用（v2 的重點是 t1 新鮮），§8 改為「B 段減倉決策的 `hi` 等於 t1」。 |
| D28（複審 1 C1，主線程裁決 2026-10-10） | **自己側讀值記憶要被自己的寫入更新，並跨重啟持久化**：(a) B 段任何成功的開倉／加倉／flip 開倉腿後，`state.fresh_me.last_had_positions = True`、`consecutive_flat = 0`（重新武裝；否則「自己剛開完倉」的下一輪空讀值會被 D11a 當已確認空手直接採用 ⇒ 全額再開一次）；成功的全平／flatten 後 `last_had_positions = False`（我方確實空手）。(b) 每輪結束把 `fresh_me.last_had_positions` 與 `fresh_leader.last_had_positions` 持久化到 `state_root/var/copytrade/fresh_read_memory.json`，引擎啟動時載入（檔不存在或壞 ⇒ 預設 False；寫入失敗只 log）——關閉「重啟後第一輪空讀值直接判 OK ⇒ 全額重開」的洞。`counterparty_has_exposure` 的依據不變（D10b）。 |
| D29（複審 1 W3） | B 段**開倉／加倉**的送單例外（`market_open`／`update_leverage`）比照 D21 接住：`failed` 含 `exc`、`logger.warning`、`notifier.warn("execution", …, dedup_key=f"open_exc:{coin}")`、該幣跳過、**其餘幣（含減倉與跟平）繼續**；不重試（非冪等）。升級帳沿用 D21 的 streak（鍵仍以幣為單位、`action` 區分 open／close）。 |
| D30（複審 1 S1） | leader 新鮮快照仍有該幣但 `t1` 捨到 0（`hi == 0`）⇒ 視同全平：`full_close=True`、不套最小名目 gate（HL 對「恰好全平」豁免 $10）。 |
| D31（複審 1 S2） | `run_cycle` 中途拋例外的輪次也要寫一筆 `kind="cycle"` trace（`error: repr(exc)`、其餘已知欄位，缺的 None），寫完**原樣重新拋出**（不改變連敗語意）；trace 寫入本身的失敗照 D23 吞掉。 |
| D32（複審 2 Critical，主線程裁決 2026-10-10；**T9 builder 回報字面版擋不住 ⇒ 修訂版**） | 自己側守衛要辨識「帳戶可能已改變」的**事件**，並在事件發生時**歸零** `consecutive_flat`、設 `pending_change=True`，讓之後的空讀值重新走完整的兩輪確認。事件來源（任一）：(i) 我方任何進場寫入嘗試——B 段 `market_open`／flip 開倉腿、A 段非 ro 單的 place／modify／結果不明的 modify（不論成敗或拋例外）；(ii) **進場型掛單的變化**：與上一輪開頭 `my_orders` 相比，任一非 ro、非 trigger 的 oid **消失**或**剩餘量變小**（＝成交或部分成交；撤單也算，方向保守）。`pending_change` 在任何讀值被採用（`_adopt`，含 `confirmed_flat` 路徑）時清除。D11a 捷徑只在 `not pending_change` 時適用；**不**再以「簿上有非 ro 掛單」為條件（那會讓掛單存續期間每輪重讀）。D28a 的逐點 rearm 保留為第二層。理由：複審 1／2 的 C1 三種形狀同根因＝「空手確認計數」在帳戶可能改變後沒有歸零；字面版只關捷徑而不歸零，重讀後 `consecutive_flat` 3 仍直接確認（T9 builder 實測 1.0→2.0）。代價：只有變化事件發生的那一輪多一次重讀＋2 秒。 |
| D33（複審 2 W2） | `run_cycle` 例外路徑（D31 的 except）在寫 error trace 之後、re-raise 之前也呼叫 `_persist_fresh_read_memory`（失敗吞掉）。 |
| D34（複審 3 Critical，主線程裁決 2026-10-10） | 記憶檔要涵蓋 D32 的全部狀態：`save_memory` 同時持久化 `pending_change` 與 `last_entry_orders`（oid → sz 的 dict，JSON 以字串鍵），`load_memory` 還原；啟動時第一輪的 `detect_entry_order_change` 以**重啟前**的基準比對（停機期間成交的鏡射單會被偵測到），`pending_change=True` 亦還原（結果不明的開倉後重啟 ⇒ 第一輪空讀值走重讀）。plan T9／RUNBOOK 原文「重啟後由記憶檔的 `me_had_positions` 保護」**不成立**，已更正。 |
| D35（複審 3 W1） | 我方**自己的**改單與撤單不是「帳戶可能已改變」：(a) `detect_entry_order_change` 忽略上一輪 A 段由我方撤銷或改單（換 oid）的舊 oid——`_reconcile_orders` 回傳 `own_removed_oids: frozenset[int]`（fallback 撤單、`to_cancel`、modify 成功的舊 oid、settle 撤掉的 extra），loop 存進 `FreshReadState.last_own_removed_oids` 供下一輪偵測排除；(b) A 段標記條件改為 `placed > 0 or ambiguous_modify > 0`（**去掉 `modified > 0`**：HL modify 帶 post-only 語意、改單當下不會成交；撤單成功不增加部位）。理由：階梯 leader 每輪改價時，空手 follower 會每兩輪整輪跳過一輪、重讀 19/20（複審 3 P3），且 `consecutive_skips` 在 0／1 來回永遠到不了 warn。 |
| D36（複審 4 C1，使用者 2026-10-11 裁決選 R） | **A 段結束、B 段開始前**（`sync_open_orders` 在 `_reconcile_orders` 回傳後、呼叫 `safety_net()` 之前；只在 live）做兩件事：(a) **以 settle 最後一次 `after` 簿重設基準**——`state.fresh_me.last_entry_orders` ＝ `after` 裡進場型掛單的 `{oid: sz}`（modify 換成的新 oid 立刻進基準，下一輪開頭消失即偵測；我方撤掉的單不在 `after`，自然不會造成假陽性）；重設時若同一 oid 的 `sz` 比開頭基準小 ⇒ 部分成交 ⇒ 視為 (b) 事件。(b) **帳戶可能已改變** ⇒ `note_account_may_have_changed(state.fresh_me)`，事件由 `_reconcile_orders` 收集成 `ReconcileResult.change_reasons: tuple[str, ...]`：進場型（非 ro、非 trigger）`place` ok（`entry_placed`）、進場型 modify 結果不明（`ambiguous_modify`）、進場型掛單的撤單**不 ok**（consumed＝其實成交了／失敗＝狀態不明；fallback、`to_cancel`、settle extra 三處；`cancel_not_ok:<coin>`）、settle 把非 ro 的 missing 當「已消耗」丟棄（`_settle_verify` 的 `_rest_left`，兩次驗證皆看；`settle_consumed:<coin>`）、(a) 的 `sz` 變小（`partial_fill:<coin>`）。**D35(a) 的排除名單整個移除**（`own_removed_oids`／`last_own_removed_oids`／`ignore_oids`）——重設基準已涵蓋其用途；D35(b)（modify 成功不標記）保留。loop 內「A 段回傳後才標記」的區塊刪除（它在 B 段之後才執行，保護不到當輪 B 段）。理由：reviewer 探針 R4-1a／1b 證明 modify 時窗是整個輪距；settle 既已重讀簿，新 oid 可觀測。效果：階梯 leader 仍 0 跳過（基準含新 oid，下一輪一致）；當輪 A 段內的全額成交（撤單回 consumed／settle 判消耗）在 B 段讀自己之前就已標記 ⇒ 單次假空讀也擋住。 |
| D36a（T11 builder 回報後，主線程 2026-10-11 修正） | **`entry_placed` 不列為帳戶變化理由**。理由：settle 的 `after` 簿是正向證據——剛掛的進場單若在 `after` 裡且量不變，到該讀點為止就沒成交；若不在（成交或讀不到），`settle_consumed` 必然觸發（該 spec 無 failed 預算、不可行動 ⇒ 丟棄為已消耗）。保留 `entry_placed` 會讓「leader 每輪新增進場單」的空手 follower **B 段每輪被跳過**（標記→計數歸零→B 的真實空讀只到 1）⇒ 安全網對這類 leader 失效；拿掉後 B 照常跑。殘餘：`after` 讀到 B 讀自己之間（次秒級）成交的窗，與 D35(b) 改單情況同階，寫進 §6／RUNBOOK。**D36 另一個前提寫明**：settle 讀簿看得到剛掛的 resting 單——這是 settle 驗證（C3）本就依賴的假設，不是 D36 新增；若 HL `openOrders` 延遲，該輪保護退化為「兩個讀點」（仍是守衛的基本設計）。`ambiguous_modify` 保留（結果不明的單被 `touched` 排除在 settle 驗證外，無正向證據）。 |
| D38（複審 5 C1） | **新 oid 的部分成交以 `orig_sz` 偵測**。D36a 的前提是「新單在 `after` 且**量不變**」，但 T11 只檢查了在不在簿上：本輪新掛／改單換成的新 oid 若在 settle 讀簿前**部分**成交，`_verify_diff` 容差內配對成功、容差外被 `_pair_residuals` 當殘單丟棄，`partially_filled_entry_orders` 又只比對開頭基準已有的 oid ⇒ 全不標記 ⇒ B 段**單次**假空讀即超倉（探針 R5-3b／3c 0→0.8，主線程重跑成立）。修法：`sync_open_orders` 重設基準前，對 `after` 每張進場型掛單：(i) oid 在開頭基準 ⇒ `sz` < 基準 sz 才算（既有）；(ii) oid **不在**開頭基準（本輪新單）⇒ `orig_sz` 非 None 且 `sz < orig_sz` 即算 `partial_fill:<coin>`；(iii) 新 oid 且 `orig_sz` 為 None（HL 未回 origSz，不預期）⇒ 保守視為 `partial_fill_unknown:<coin>` 標記。`OpenOrder.orig_sz` 已存在（`get_open_orders` 走 `frontendOpenOrders`、解析 `origSz`，`hyperliquid.py:154-176`；2026-07-28 起 `_pair_residuals` 依賴它）。另：`_pair_residuals` 配成殘單對的（missing, extra）也是部分成交證據，一併回傳給呼叫端，進場型者亦記 `partial_fill:<coin>`（與 (ii) 去重）。不改 `_verify_diff` 的配對規則。 |
| D39（複審 5 W1） | **D18 延後不算已消耗**。post-only 越價被拒（首輪 place 或 settle 補單）的 spec 從未上簿、無成交，但不在 `failed` 預算 ⇒ `_settle_verify` 把它丟進 consumed ⇒ 誤標記 ⇒ 空手 follower B 段被跳過（探針 R5-4：20 輪 10 次延後 ⇒ B 跳 10、重讀 20、0 成交）。修法：`_reconcile_orders` 以 `Counter[OrderSpec]` 記錄本輪 D18 延後的 spec（兩處），計算 `settle_consumed` 理由時先按值扣除（一值一張）；`deferred_alo_cross` 計數不變；trace 不再出現假的 `settle_consumed`。 |
| D40（複審 6 W1） | **新 oid 的部分成交判定不依賴 `origSz` 語意**。D38 (ii) 假設「HL 改單後新 oid 的 `origSz`＝改後量」，但 repo 自己的 `docs/superpowers/plans/2026-10-01-settle-residual-and-1to1.md:69` 註明**未實測**；若 HL 其實保留改前 `origSz`：改大量時容差內部分成交被蓋掉（探針 P1 0→0.54 單次假讀）、改小量時每輪誤標（P1b 20 輪跳 10）。修法：`_reconcile_orders` 另回傳 `written_specs: tuple[OrderSpec, ...]`＝本輪**成功寫入會產生新 oid** 的進場型 spec（modify ok 的新 spec、首輪 place ok、settle 補單 ok；**加上** ambiguous modify 的 spec——若它其實落地，新 oid 會配到它）；`sync_open_orders` 對 `after` 裡**不在開頭基準**的進場型新 oid，以 coin／is_buy／px（`px_rel_tol`）與 `written_specs` 1:1 貪婪配對（兩段：先 `sz` 完全相等者，再其餘——同 `_verify_diff` 的 C9 精神，避免同價不同量互搶），配到 ⇒ **`o.sz < spec.sz` 才算 `partial_fill:<coin>`**（spec 量在 `orders.py:315` 已 `_round_size` 到格點，與交易所回傳的 `sz` 同基準，精確比較）；配不到（非本輪所寫的新單，例如外部手動單）⇒ 退回 D38 (ii)(iii) 的 `orig_sz` 規則。舊 oid 規則 (i) 不變。plan §6／RUNBOOK 把「origSz＝改後量」從事實改為「僅配不到時的退路假設」；§8 部署後觀察項加「cycle_trace 若出現 `partial_fill_unknown` 或配不到的新 oid，回頭查 origSz 語意」。 |
| D37（複審 4 W2／S） | RUNBOOK §5.8m：(a) 「手動清除記憶檔」改寫為**刪檔會失去哪些保護**（`me_pending_change`／進場單基準；500 已落地開倉後或簿上有鏡射單時**不得**刪）＋何時可以刪（引擎已停、確認空手且無掛單）；(b) 「重啟後那一輪會多一次保守重讀」改寫實：空手＋leader 有掛單時重啟本就跳 2 輪（D28 既有，`consecutive_flat` 不落檔），與 D35 無關；(c) 測試 rig `BookLive.cancel_order_result` 對不在簿上的 oid 回 `consumed`（貼近 HL），不回 ok。 (d)（複審 5 W2）T11 誤刪了 §6「自己帳戶外部手動交易」的唯一使用須知句 ⇒ 補回 §5.8m（owner 在 follower 帳戶手動下單後連續兩次假空讀即照目標重開；手動交易前後建議停引擎或至少確認部位）。(e)（複審 5 S）為 `_is_entry_spec`／`_is_entry_order` 排除 trigger 加單元測試鎖住（目前等價變異，trigger 支援上線即失守）。 |
| D14 | B 段回傳 dict 用**顯式布林鍵** `b_phase_skipped: bool` 與 `skip_reason: str | None`；任何依「是否略過」做判斷的程式只看這個鍵，不看 `skipped` 清單。 |

### 3.3 A 段與韌性（I2、I3、I5、I7）

| # | 裁決 |
|---|---|
| D15 | HL 拒因分類純函式（`hl_reasons.py`）：`is_consumed_reason`（含 `canceled or filled`／`already canceled, or filled`／`never placed`）、`is_post_only_cross_reason`（含 `post only order would have immediately matched`）；不分大小寫子字串；不認得 ⇒ 不當消耗。 |
| D16 | **modify／cancel 回拒因為「已消耗」** ⇒ 該 spec 本輪不下單（`consumed_skipped += 1`），不登記 TTL、不進 fallback，oid 進 `touched_oids`。下一輪依最新掛單重算。 |
| D17 | **modify 拋例外**（含 HTTP 5xx）⇒ 結果不明：登記該 coin TTL、oid 進 `touched_oids`、`ambiguous_modify += 1`、warn（去重 `modify_ambiguous:<coin>`），**不**退回重掛、**不**中止整輪、繼續下一張。 |
| D18 | **post-only 被判會吃單**（首輪下單或 settle 補單）⇒ 市場已越過：`deferred_alo_cross += 1`，不進失敗預算、不進 CRIT 的補單失敗清單；其他拒因（保證金不足等）行為不變仍 CRIT。 |
| D19 | **5xx 以 `status_code ∈ {500,502,503,504}` 判暫時性**（整數屬性，不用字串比對），只影響本來就重試的冪等寫入。 |
| D20 | **部分減倉不冪等**：`close_reduce_only(..., full_close: bool)` 一路宣告到 `ResilientExchange.order(..., _idempotent=...)`；`full_close=True`（flip 平倉腿、flatten、kill switch、panic）維持重試，`False`（部分減倉）單次嘗試。`scripts/panic.py` 的 executor 一併接參數（T1 實作：panic 的預設為 True，因其恆為全量平倉）。**過渡期注意**：T1 後 adapter 預設 `full_close=False`，而 `ActionExecutor.close_reduce_only` 尚未轉傳 ⇒ 引擎內 flip／flatten 在 T4 接線前暫時失去 5xx 重試（kill switch 外層 `resilient_run` 不受影響）。未部署，可接受；**T4 必須**讓 `ExecutorPort.close_reduce_only(..., full_close: bool = False)` 與 `ActionExecutor` 轉傳，並在 `sync_positions_v2` 各呼叫點宣告。 |
| D21 | **減倉送單例外不中止 B 段**：`_try_close_reduce_only` 接住例外 ⇒ `failed`（含 `exc`）、warn（`close_exc:<coin>`）、回 False、其餘幣繼續。升級：`ReconcileState.close_exc_streak[coin]` 連續 ≥ `close_exc_crit_after`（預設 5）輪 ⇒ critical（去重）；**判斷「本輪 B 段有沒有跑」只看 D14 的布林鍵**。 |
| D22 | 五連敗 CRIT 文案：`連續 N 次同步失敗（最後錯誤：…；首次失敗 HH:MM:SS UTC）。引擎將退出，systemd 約 10 秒後自動重啟；通常無需人工介入，若本訊息重複出現或引擎未恢復才需處理`。閾值與 `SystemExit(1)` 不變。 |

### 3.4 可觀測性（I6）

| # | 裁決 |
|---|---|
| D23 | 每輪寫 `state/<id>/var/copytrade/cycle_trace-YYYYMMDD.jsonl`：彙總行（essential：ts、各讀取 ms、兩份快照的 av／ntl／逐幣 szi、`scale_snapshot`／`scale_fresh`、波動權重與輸入、守衛結果與 skip 計數、A 段計數 placed／cancelled／modified／consumed_skipped／deferred_alo_cross／ambiguous_modify、B 段計數）＋有動作或兩份目標不同時的逐幣 decision 行（coin、t0、t1、lo、hi、m、branch、size）。單日上限 16 MB（超過只丟 decision 行）、保留 14 天、寫入失敗計數＋每進程一則 warn、**絕不影響交易**。 |
| D24 | 心跳 `result` 增加 `"skipped"`（整輪跳過），detail 帶 reason。儀表板 `_HEALTHY_CYCLE_RESULTS` **不改**（使用者 2026-10-09 裁決：跳過顯示「訊號來源狀態未知」是如實）。 |

### 3.5 流程

| # | 裁決 |
|---|---|
| D25 | 全部實作任務派 `builder`（Sonnet 5.5）；不派 Haiku。每個 task：TDD、ruff、全套 0 failed、plan 外的判斷立即停下回報。 |
| D26 | 每個行為類 task 必附**變異驗證**（至少 3 個刻意植入的 bug 被對應測試抓到），由 builder 做、主線程至少重做 1 個（先 `git diff --stat` 確認變異已套用）。 |
| D27 | 事故重播測試（§7）的 size 斷言用**精確值**；加通用斷言「所有 `market_open`／`close_reduce_only` 的 size 等於 `_round_size(size, szDecimals)`」。 |

---

## 4. 紅線與共通驗收

- CLAUDE.md 紅線全部適用：不讀 `.env*`；`ExchangeAdapter` 無 withdraw／transfer；本 plan 不新增任何會建單的寫入路徑；測試全離線；不碰 `hl-copytrader`；**不部署、不重啟正式機**。
- Python 3.11 相容（本機 venv 3.14；每個改動檔跑 `ast.parse(..., feature_version=(3,11))`）。內部 `Decimal`。
- 每 task：`uv run ruff check src tests scripts` 全過；相關測試檔全綠並貼輸出末尾；全套 `uv run pytest -q` 不得比基線多失敗（基線 §10）。
- 回報合約：(a) 結論一段；(b) `檔案:行號`；(c) 每條驗收的指令＋輸出；(d) plan 外的發現只記錄。「你必須親自執行全部步驟，不得使用 Agent 工具再委派。」

---

## 5. 任務（全部 `@inline`／builder；依序，不平行）

### T0（主線程）分支與基線
`git switch -c fix/copytrade-safety-net-v2`；記錄 `uv run pytest -q` 與 ruff 到 §10。

### T1 基礎型別與純函式（無行為改變）
- `src/spark/exchange/hl_reasons.py`（D15）＋測試（用 10-07 真實字串 ≥8 例）。
- `src/spark/exchange/base.py`：`UserSnapshot`、`WriteResult(ok, reason)`；`ExchangeAdapter.get_user_snapshot`／`modify_order_result`／`cancel_order_result` 非抽象預設實作（D9）；`close_reduce_only(..., full_close: bool = False)` 簽章（D20）。`hyperliquid.py` 覆寫（單一 `user_state`；`_write_result` 帶回 `statuses[0]["error"]` 原文；`close_reduce_only` 傳 `_idempotent=full_close`）。`fakes.py` 跟上（可設拒因、記錄 `full_close`）。
- `src/spark/resilience.py`：`_is_transient_error` 加 `status_code` 判斷（D19）；`order(..., _idempotent=None)`（D20，不傳 SDK）。
- `src/spark/copytrade/fresh_read.py`：`classify_read`（D10 真值表，leader／自己共用）、`read_with_confirmation`（D11）、`FreshReadState`／`begin_cycle`／`mark_skip`／`end_cycle`（D13）。
- `src/spark/copytrade/cycle_trace.py`：`CycleTraceWriter`（D23 的檔案層）。
- `CopySettings` 欄位：`fresh_read_retry_s=2`、`fresh_read_warn_after=3`、`fresh_read_crit_after=10`、`close_exc_crit_after=5`；驗證 `0 ≤ size_tolerance < 1`。
- 驗收：各自測試全綠；既有 `get_positions`／`get_account_state` 行為逐位元不變（canned 資料斷言）。

### T2 A 段：消耗單不重掛、post-only 延後、modify 例外不中止（D16–D18）
- `executor.py`：`WriteOutcome(ok, kind∈{ok,consumed,rejected}, reason)`、`modify_outcome`／`cancel_outcome`（`ExecutorPort` 不加方法；`orders.py` 用 `getattr` 鴨子型別退回 bool）。
- `orders.py` `_reconcile_orders`：步驟 1 try 包 modify（D17）、consumed（D16）；步驟 2 fallback 撤單 consumed ⇒ 不下單；步驗 3／4 post-only（D18）；`ReconcileResult` 加三個計數。
- 測試 a–h（同 v1 T9 清單）＋變異 ≥3。
- 驗收：既有 orders 測試全綠；真實拒因字串重播。

### T3 守衛與開頭讀取（D9–D14 的開頭部分）
- 先補 D10a（`fresh_read.classify_read` 的 `ntl < 0` 列＋測試）。依 D13a：`ReconcileState.fresh_leader`／`fresh_me` 各一個 `FreshReadState`，ledger＝`fresh_leader`。
- `loop.py` 開頭：leader 掛單 → 自己掛單 → **自己快照（守衛）** → **leader 快照（守衛）**；任一 skip ⇒ 整輪 `CycleReport(skipped_reason=…)`、`end_cycle`、通知；`run_cycle(..., *, sleep_fn=time.sleep)`。`ReconcileState.fresh`。心跳 `skipped`（D24）。
- 測試：真值表每列、兩輪確認、`account_value=0` 不得走到 scale=0 撤單、第 3／10 輪通知、既有 fixture 中 `ntl=0` 的 leader 需改為合理值（逐一列出）。

### T4 B 段：不對稱比較規則（D1–D8、D14、D21）
- 先做 D11a（`fresh_read.read_with_confirmation` 的已確認空值不重讀＋測試：第 3 輪起 `sleep_fn` 不被呼叫、`read` 只呼叫 1 次；讀到部位後重新武裝）。
- 再完成 D20 的過渡缺口：`ExecutorPort.close_reduce_only(..., *, emergency=False, full_close: bool = False)`、`ActionExecutor` 轉傳給 adapter 並記進 ActionRecord；既有測試替身只加參數不改斷言（逐一列出）。
- `loop.py` `_safety_net`（live）：`safety_net_start_ms` → 自己快照（守衛）→ leader 快照（守衛）→ `scale_fresh` → `sync_positions_v2(...)`；任一 skip ⇒ `{"b_phase_skipped": True, "skip_reason": …}`；回傳加 `t0/t1/lo/hi` 逐幣、`scale_snapshot`／`scale_fresh`、`snapshot_age_s`、`decisions`。dry：用開頭快照、`t0==t1`。
- `positions.py`：新函式 `sync_positions_v2(ex, leader_snapshot_positions, leader_fresh_positions, my_fresh_positions, scale_snapshot, scale_fresh, *, settings, notifier, protected, size_decimals, mids, no_new_exposure, decisions)`，實作 D2–D8；`_try_close_reduce_only` 接例外（D21）。舊 `sync_positions` 保留給 kill switch／其他呼叫點（確認呼叫點後再決定是否刪）。
- `loop.py`：`_track_close_exceptions(state, settings, notifier, net)` 只看 `net["b_phase_skipped"] is False`（D21）。
- 格點斷言（D7）在 executor。
- 測試：§7 重播 R1（10-07 數字 ⇒ 減 ETH 0.351／HYPE 3.09 **精確**）、R5（鏡射單落後：t0==m、t1>m ⇒ **不加倉**）、R6（leader 市價進場在上一輪 ⇒ t0 含 ⇒ 加倉）、R7（leader A 段減倉且我方不足 ⇒ 只加到 `lo=t1`）、R8（方向在輪內改變 ⇒ 跳過）、R9（自己讀空 ⇒ B 段跳過、零寫入）、D21 例外繼續、D14 布林鍵在有 skipped 清單時仍正確累計 streak、格點通用斷言；變異 ≥5（含「加倉改用 t1」必須被 R5 抓到）。

### T5 五連敗文案（D22）＋ cycle_trace 接線（D23）
- `main_loop` 文案與 `first_error_at`；`ReconcileState.trace`；`_write_trace` 吞例外；skip 輪也寫；trace 失敗 warn 一次。
- 測試：文案含時間、`skipped` 輪有 cycle 行、decision 行觸發條件、writer 失敗不影響回傳。

### T6 事故重播整合測試＋變異總驗證
- 新檔 `tests/test_copy_incident_20261007_replay.py`：從 `run_cycle` 入口重播 R1–R9＋A 段 R2（consumed）、R3（post-only）、R4（modify 500）；每個測試帶路徑覆蓋計數；至少 8 個變異（含 v1 七輪抓到的每一類：加倉用錯目標、守衛把空當歸零、consumed 仍重掛、post-only 進 failed、modify 例外不接、`b_phase_skipped` 用清單判、size 不在格點、部分減倉重試）全部轉紅。

### T7 文件
- `deploy/RUNBOOK.md` 新節：D3 規則一句話、cycle_trace 欄位、守衛與四個設定、新告警（`fresh_read_*`、`modify_ambiguous`、`close_exc*`）處置、B2 新文案、儀表板 `skipped` 顯示為預期、部署前檢查清單（§8），節首標「部署尚未獲授權」。
- 從 archive 分支挑回事故回顧文件與分析工具（純 docs commit，`git checkout archive/2026-10-fresh-leader-read-v1 -- docs/superpowers/research/2026-10-09-*`），在回顧文件 §8 補 cycle_trace 查詢範例。

### T8 複審 1 修正（D28–D31）＋文件
- 檔案：`src/spark/copytrade/fresh_read.py`（新增 `rearm_after_own_fill(state, has_positions: bool)`、`load_memory(path) -> dict`／`save_memory(path, leader_state, me_state)`——純函式＋檔案 IO 皆不拋）、`src/spark/copytrade/loop.py`（B 段成功開倉／flip 開倉腿後呼叫 rearm；全平成功後 `last_had_positions=False`；每輪結束 `save_memory`；D31 的 try/except 包住 `run_cycle` 主體並寫 error trace 後 re-raise）、`src/spark/copytrade/positions.py`（D29：`_try_update_leverage`／`_try_market_open` 接例外並記 `failed` 含 `exc`、warn `open_exc:<coin>`；D30：`hi == 0` 且 leader 快照仍有該幣 ⇒ 全平路徑）、`scripts/run_copytrade.py`（啟動時 `load_memory(state_root / "var/copytrade/fresh_read_memory.json")` 餵進 `ReconcileState`）、`deploy/RUNBOOK.md` §5.8m（W1 延遲一輪、W2 殘留、unified 帳戶查到時的處置：perp 權益 ≤0 會被判 INVALID 整輪跳過 ⇒ 建議該 follower 改 `use_full_equity`／或暫停）、plan §8 驗收指紋補 W2 排除條件。
- 測試：(a) reviewer 的 C1 兩個情境（自己剛開完倉的下一輪讀空 ⇒ B 段跳過而非再開；重啟後載入記憶 ⇒ 讀空 ⇒ 跳過）精確斷言部位不變；(b) 記憶檔讀寫、壞檔、不存在；(c) D29：第一個幣 `market_open` 拋 `ServerError(500,'null')` ⇒ 其餘幣減倉／跟平仍執行、`failed` 含 exc、warn 一則；(d) D30：`t1` 捨到 0 且 leader 快照仍有該幣 ⇒ `full_close=True`、不 gate；(e) D31：`run_cycle` 中途拋例外 ⇒ trace 有 `error` 行、例外仍上拋；(f) 既有 R1–R9 不變。變異 ≥4（rearm 移除、記憶不載入、D29 try 移除、D30 判斷移除），印出被改行、清 `__pycache__`。
- **主線程裁決（2026-10-10，builder 依升級協定回報）**：(1) T4 的 `test_d10b_flat_follower_opens_on_first_cycle_when_leader_holds_without_orders` 假世界「開倉後帳戶仍為空」與 D28 直接衝突（D28 正是要擋這個情境）⇒ 授權修改該測試的自己側劇本（第 2 輪起帳戶反映已開的 1.0）、`market_open` 次數改 1，保留「第 1 輪不死鎖、全程無重讀」的 D10b 鎖定。(2) rearm **只在 live** 執行：dry 的寫入不改真實帳戶，rearm 會讓空帳戶每 3 輪跳過 2 輪。(3) `_try_update_leverage` 回傳 bool：只有拋例外才回 False 並跳過該幣開倉；一般 `ok=False` 維持 best-effort 照開。(4) 新版首次部署沒有記憶檔，第一輪空讀值守衛仍是舊行為（殘餘窗口，RUNBOOK 註明）。
- 驗收：相關測試檔全綠；全套 0 failed；ruff；3.11；commit `fix(v2-T8): 自己側讀值記憶隨寫入更新並持久化（D28）、B 段開倉例外不中止（D29）、t1 捨 0 視同全平（D30）、例外輪也寫 trace（D31）＋RUNBOOK`。

### T9 複審 2 修正（D32、D33）＋測試補強＋文件
- **修訂版（依 D32 修訂）**：`fresh_read.py`：`FreshReadState.pending_change: bool = False`、`last_entry_orders: dict[int, Decimal] = field(default_factory=dict)`（上一輪開頭進場型 oid → 剩餘量）；`note_account_may_have_changed(state)`（`pending_change=True`、`consecutive_flat=0`）；`detect_entry_order_change(state, my_orders) -> bool`（純函式：對非 ro 非 trigger 單，oid 消失或 sz 變小 ⇒ True；並把本輪集合存回 `last_entry_orders`）；`read_with_confirmation` 捷徑條件改為 `not state.pending_change`（**不**加 `shortcut_allowed`／`my_orders` 條件）；`_adopt` 清 `pending_change`，**`confirmed_flat` 路徑也呼叫 `_adopt`**。`loop.py`：開頭讀 `my_orders` 後先 `detect_entry_order_change(state.fresh_me, my_orders)` ⇒ True 則 `note_account_may_have_changed`；B 段 `market_open`／flip 開倉腿嘗試前（`on_entry_attempt` callback）與 A 段回傳後（leader 有非 ro 非 trigger 掛單且 `placed>0 or modified>0 or ambiguous_modify>0`，或 A 段中途拋例外）呼叫 `note_account_may_have_changed`；D33 例外路徑 persist。`last_entry_orders` **不**持久化（重啟後由記憶檔的 `me_had_positions` 保護）。`deploy/RUNBOOK.md`（更正 `open_exc` 列；D32 代價＝只有變化事件那一輪多一次重讀＋2 秒）、plan §8（主線程）、測試。
- 測試（新檔 `tests/test_copy_safety_net_v2_t9.py`，**用 `allocated_capital` rig** 讓部位斷言有效，不靠 scale=0 遮掩）：(a) 鏡射單成交後下一輪兩次讀空 ⇒ 跳過、部位 1.0 不變、有重讀；(b) 500 已落地的 B 段開倉後下一輪讀空 ⇒ 跳過；(c) 自己開倉後（D28a 既有）仍跳過；(d) 空手、簿上有非 ro 鏡射單**持續未成交**多輪 ⇒ 捷徑生效（掛單存續期間 `sleep_fn` 不被呼叫、不跳過）；(d2) 鏡射單**掛了多輪後**才成交、之後兩次假空讀 ⇒ 跳過、部位不變（builder 的「缺陷一」情境）；(d3) 鏡射單部分成交（oid 仍在、sz 變小）後假空讀 ⇒ 跳過；(e) 空手、無進場單、無寫入 ⇒ 捷徑生效；(e2) 撤單導致 oid 消失 ⇒ 該輪重讀一次後恢復；(f) 任何讀值採用後 `pending_change` 清除；(g) D33：例外輪落檔存在且內容正確；(h) `if not left` 部分全平不設 False（W3）；(i) rearm 僅 live（S2）；(j) 既有 C1 測試改用 `allocated_capital` rig 並斷言部位精確不變（S1）。變異 ≥5：捷徵忽略 `my_orders`、忽略 `pending_change`、`market_open` 嘗試不標記、A 段 place 不標記、D33 persist 移除——每個印出被改行、清 `__pycache__`。
- 驗收：相關測試檔全綠；全套 0 failed；ruff；3.11；reviewer 第 2 輪三支探針（mirror fill、landed-500、exception persist；在匯出副本上以 `tests/test_probe_r2.py` 重跑）由主線程確認轉綠；commit `fix(v2-T9): D11a 捷徑收斂——有進場掛單或有寫入嘗試即不得直接採用空讀值（D32）；例外輪也落記憶檔（D33）＋測試補強`。

### T10 複審 3 修正（D34、D35）＋殘留清理＋文件
- 檔案：`src/spark/copytrade/fresh_read.py`（`save_memory`／`load_memory` 加 `me_pending_change: bool`、`me_last_entry_orders: dict[str, str]`（oid 字串 → sz 字串，Decimal 以 str 存）；`FreshReadState.last_own_removed_oids: frozenset[int] = frozenset()`；`detect_entry_order_change(state, my_orders, *, ignore_oids: frozenset[int])`：消失的 oid 若在 `ignore_oids` 內不算變化；**刪除第 232 行死碼** `state.consecutive_flat = 0   # EXPERIMENT option A`）、`src/spark/copytrade/orders.py`（`ReconcileResult.own_removed_oids: frozenset[int] = frozenset()`，由 `_reconcile_orders` 填：fallback 撤單 oid、`plan.to_cancel`、modify 成功的舊 oid、settle 撤掉的 extra oid）、`src/spark/copytrade/loop.py`（A 段標記條件改 `placed > 0 or ambiguous_modify > 0`；A 段回傳後 `state.fresh_me.last_own_removed_oids = rec.own_removed_oids`；開頭偵測傳 `ignore_oids=state.fresh_me.last_own_removed_oids`；啟動載入時還原 pending 與基準）、`scripts/run_copytrade.py`（載入新欄位）、`deploy/RUNBOOK.md` §5.8m（更正「重啟由記憶檔保護」為 D34 語意；代價改為「只有真實成交／部分成交／外部撤單的那一輪多一次重讀」；更正 post-only 那句——鏡射單照抄 leader 的 tif，可能在 B 段前成交，真正的保護是捷徑只在整個帳戶空手時生效＋D3 的 `lo`）、plan §6（主線程更正「每輪多重讀」句）。
- 測試（新檔 `tests/test_copy_safety_net_v2_t10.py`＋既有 T9 測試不改斷言）：(a) 複審 3 P4：空手、簿上一張鏡射單、leader 一張進場單；落檔後「停機」（新 `ReconcileState` 載入記憶檔）、停機期間雙方成交（leader 無掛單、我方 oid 消失）⇒ 第一輪偵測到變化 ⇒ 兩次假空讀被跳過、部位不變；(b) P6：500 已落地後落檔 `pending=True` ⇒ 重啟 ⇒ 兩次假空讀跳過；(c) P3：階梯 leader 每輪改價 20 輪 ⇒ 空手 follower **0 次整輪跳過**、重讀次數 ≤ 1（第一輪確認）；(d) 我方自己撤單（leader 撤單 ⇒ A 段撤鏡射）⇒ 下一輪不標記、不跳過；(e) 真實成交（oid 消失且不在 `own_removed_oids`）仍偵測；(f) modify 成功後新 oid 之後成交 ⇒ 偵測；(g) 記憶檔新欄位讀寫／壞檔；(h) A 段標記只在 live（複審 3 S）；(i) 死碼已刪（grep 斷言或 import 檢查）。變異 ≥5：persist 不含 pending（b 紅）、persist 不含基準（a 紅）、`ignore_oids` 不生效（c／d 紅）、A 段條件加回 `modified>0`（c 紅）、`own_removed_oids` 不含 modify 舊 oid（c 紅）。`bogus_me` 涵蓋所有讀取點；`allocated_capital` rig。
- **主線程裁決（2026-10-10，builder 依升級協定回報）**：(1) 採選項 A——T9 `test_g_exception_cycle_still_persists_memory` 與 T8 `test_c1_2_restart_with_persisted_memory_skips_empty_read` 的整份 JSON 精確斷言鎖的是 D34 前的落檔形狀而非行為 ⇒ 授權在期望值加 `"me_pending_change": True`；`save_memory` 對新欄位用**稀疏寫法**（False／空 dict 時省略），`load_memory` 只回檔內有且型別正確的鍵（新鍵型別錯只丟該鍵），還原端用 `.get` 補預設；還原邏輯抽成 `fresh_read.py` 的小函式供 `run_copytrade.py` 與測試共用。(2) `own_removed_oids` **只收撤單結果 ok 為 True 的 oid**（fallback／`to_cancel`／settle extra 皆同；`consumed` 代表那張單其實成交了，必須讓偵測看見）＋modify 成功的舊 oid。(3) 已接受的殘餘：modify 成功換成新 oid、新 oid 在下一輪開頭前就成交 ⇒ 新 oid 從未進過基準、舊 oid 被忽略 ⇒ 該次成交不可觀測；需再接連兩次假空讀才會造成傷害，寫進 §6。
- 驗收：相關測試檔全綠；全套 0 failed；ruff；3.11；`grep -n EXPERIMENT src` 無輸出；commit `fix(v2-T10): 記憶檔持久化 pending 與進場單基準（D34）；自己的改單撤單不算帳戶變化（D35）；刪死碼＋RUNBOOK 更正`。

### 審查
每個 task 完成 ⇒ 主線程親跑驗收 ⇒ 下一個 task。T6 完成後派 `reviewer`（opus）看整條分支 diff＋本 plan，必答：(1) 任何路徑在失敗時會不會多下單；(2) 守衛有沒有把讀不到當歸零的路徑（leader 與自己）；(3) D3 規則在 10-07、鏡射落後、leader 市價進場、A 段減倉、翻向五個情境的輸出各是什麼；(4) 每個 B 段回傳的判斷是否只看布林鍵。完成定義見 §9。

---

### T11 `@inline` ── D36 A 段後重設基準＋B 段前標記；移除 D35(a) 排除名單；D37 文件與 rig（複審 4）

- 檔案：`src/spark/copytrade/orders.py`（`ReconcileResult` 新欄位 `change_reasons: tuple[str, ...] = ()`、`book_after: tuple[OpenOrder, ...] | None = None`；移除 `own_removed_oids`；`_reconcile_orders` 收集事件；`_settle_verify` 另回傳被當已消耗丟棄的 missing（既有呼叫端同步改）；`sync_open_orders` 在 `rec` 之後、`safety_net()` 之前（live）呼叫 `rebaseline_entry_orders` 與 `note_account_may_have_changed`）、`src/spark/copytrade/fresh_read.py`（移除 `last_own_removed_oids`／`ignore_oids`，`detect_entry_order_change` 回 T9 簽名；新增 `rebaseline_entry_orders(state, after_orders) -> bool`：重設 `last_entry_orders`，共同 oid 的 `sz` 變小回 True）、`src/spark/copytrade/loop.py`（刪除 A 段回傳後的標記區塊與 `last_own_removed_oids` 存放；開頭偵測去掉 `ignore_oids`；trace 的 cycle 行加 `a_phase_change_reasons`）、`deploy/RUNBOOK.md` §5.8m（D37 (a)(b)；「複審 3 修正」小節的 D35(a) 敘述改為 D36；殘餘句改為 W1 的正確規模）、`tests/test_copy_safety_net_v2_t10.py`（移除 `own_removed_oids` 單元測試與 `ignore_oids` 真值表；(c)(d)(e)(f) 劇本保留、斷言不變；rig D37(c)）、新檔 `tests/test_copy_safety_net_v2_t11.py`。
- `sync_open_orders` 需要 `state.fresh_me`：`state: ReconcileState` 已是參數；`rebaseline`／標記只在 `live`。`_reconcile_orders` 中途拋例外 ⇒ 不重設、不標記（基準停在開頭那份 ⇒ 下一輪最多一次保守重讀，可接受）。dry／shadow 不重設、不標記（基準只在 live 使用）。
- 測試（新檔，沿用 t10 的 `BookLive`／`Rig`／`restart`；`bogus_me` 涵蓋所有讀取點；`allocated_capital` rig）：(a) 複審 4 R4-1a／1b：空手確認、leader 一張進場單、A 段 modify 換 oid、新 oid 在下一輪開頭前全部成交（leader 無／有下一層兩種）⇒ 下一輪兩次假空讀 ⇒ 跳過、部位精確不變（原 0.5→1.0）；(a2) 對照：新 oid 被觀察一輪後才成交 ⇒ 仍偵測；(b) **當輪 A 段內成交**：開頭讀簿後鏡射單全額成交、leader 掛單消失 ⇒ A 段撤單回 consumed ⇒ B 段**單次**假空讀 ⇒ B 段跳過（`skipped_reason` 以 `me:` 開頭）、`market_open` 不增加；(c) settle 判消耗：新掛的進場單在 `after` 讀之前成交 ⇒ `change_reasons` 含 `settle_consumed:ETH` ⇒ B 段單次假空讀跳過；(d) 進場 place ok ⇒ 在 B 段之前已標記（用 `safety_net` callable 內斷言 `pending_change is True`）；(e) 重設基準：有 modify 的一輪結束後 `last_entry_orders` 的鍵＝`after` 簿的進場 oid、落檔的 `me_last_entry_orders` 含新 oid、不含舊 oid；(f) 部分成交（untouched oid 在 `after` 的 `sz` 變小）⇒ `partial_fill:ETH` ⇒ 標記；(g) 階梯 leader 每輪改價 20 輪 ⇒ 空手 follower 0 跳過、重讀 ≤1（P3 不退化）；(h) leader 撤單 ⇒ 我方撤單 ok ⇒ 下一輪不標記、不跳過（原 t10 (d)）；(i) dry／shadow：不重設、不標記；(j) A 段中途拋例外 ⇒ 基準＝開頭那份、無標記、無崩潰；(k) RUNBOOK D37 文字 grep；(l) rig `cancel_order_result` 對不存在的 oid 回 consumed、且 loop 層劇本「撤到已成交的單」走 consumed 路徑。變異 ≥7（先印被改行、`PYTHONDONTWRITEBYTECODE=1`、清 `__pycache__`）：不重設基準（a 紅）；標記移到 B 段之後（b／c／d 紅）；撤單不 ok 不標記（b 紅）；settle 消耗不標記（c 紅）；`partial_fill` 不標記（f 紅）；dry 也重設（i 紅）；重設時用開頭簿而非 `after`（a／e 紅）。
- **主線程裁決（2026-10-11，builder 回報兩條既有測試轉紅）**：(1) t10 `test_h2_…` 的 `consecutive_flat == 0` 改為 `r.reconcile.change_reasons == ("ambiguous_modify",)`，`pending_change is True` 保留（D36 預期：B 的真實空讀成為第 1 個確認點）。(2) t9 `test_a_mirror_fill_…[0]` 的 `rest_mirror` 假簿改為「掛單後 settle 讀值含該單」（原假簿回 `[]` 違反 settle 自身假設），加一行註解說明 D36 依賴此假設。(3) 依 D36a 拿掉 `entry_placed`：測試 (d) 改為「進場 place ok 且 `after` 含該單 ⇒ **不**標記、B 段照常執行（`safety_net` 內斷言 `pending_change is False`）」；(c) 保留為 place 後成交的保護；變異 M8 改為「settle 把 place ok 但不在 `after` 的 spec 丟棄時不標記 ⇒ c 紅」；RUNBOOK 的 D36 代價句改寫（只在有成交證據的那輪 B 段多重讀／跳過）。(4) 補一條單元測試：modify 回 consumed 的 spec ⇒ `settle_consumed` 出現（builder 第 4 點）。(5) 第二次回報：t10 `test_h_a_phase_mark_only_in_live` 的 live 對照組原靠 `entry_placed`，D36a 後不應標記 ⇒ 改為 live 下改單拋 `ServerError(500)`（ambiguous）斷言 `pending_change is True`，dry 半段不動；h2 實測 `change_reasons == ("ambiguous_modify", "settle_consumed:ETH")`（touched 排除 ⇒ settle 同時判消耗）⇒ 改為 `in` 斷言，接受。(6) builder 第 1–3、5–8 點全部接受（trace 稀疏鍵、`partially_filled_entry_orders` 純函式、reason 字串去重保序、(j) 只在 `sync_open_orders` 層、B 段跳過看 `safety_net["skip_reason"]`、「單次」＝一個讀點兩次皆空）。
- **真實工作樹**做測試與變異（不要在匯出副本；若在副本一律 `PYTHONPATH="$PWD/src"`，見 §0）。
- 驗收：t10／t11／t8／t9／fresh_read／loop_fresh_read／重播全綠；全套 0 failed（前景跑完）；ruff；3.11 `ast.parse`；`grep -rn "own_removed\|ignore_oids" src tests` 無輸出；commit `fix(v2-T11): A 段後以 settle 簿重設進場單基準、B 段前標記帳戶變化（D36）；移除 D35(a) 排除名單；RUNBOOK 記憶檔與重啟成本寫實（D37）`。

### T12 `@inline` ── D38 新 oid 部分成交偵測；D39 D18 延後不算消耗；D37(d)(e)（複審 5）

- 檔案：`src/spark/copytrade/fresh_read.py`（`partially_filled_entry_orders` 擴充 D38 (i)(ii)(iii)：回傳要能讓呼叫端分辨 `partial_fill` 與 `partial_fill_unknown`；`rebaseline_entry_orders` 不變）、`src/spark/copytrade/orders.py`（`_pair_residuals` 另回傳配成殘單對的清單；`_settle_verify` 帶出來；`_reconcile_orders` 記錄 D18 延後 spec 的 `Counter` 並在組 `settle_consumed` 理由前扣除；殘單對的進場型 spec 記 `partial_fill:<coin>`；`sync_open_orders` 用擴充後的偵測）、`deploy/RUNBOOK.md` §5.8m（D37(d) 補回外部手動交易須知；新增「複審 5 修正」小節寫 D38／D39；`a_phase_change_reasons` 值域加 `partial_fill_unknown:<coin>`，寫明 `settle_consumed` 不含 D18 延後）、新檔 `tests/test_copy_safety_net_v2_t12.py`、`tests/test_copy_safety_net_v2_t11.py`（既有斷言若因回傳型別改變需調整，只改型別不改語意）。
- builder 開工前先讀 `src/spark/exchange/hyperliquid.py:154-176` 確認 `origSz` 來源（已由主線程確認走 `frontendOpenOrders`）。
- 測試（沿用 t11 的 `BookLive`／`Rig`／`restart`；`BookLive` 的掛單需帶 `orig_sz`＝下單量、`fill_mine` 只減 `sz`）：(a) R5-3b：空手確認、leader 開頭已持 0.5、A 段 modify 換 oid、新 oid 在 settle 讀前部分成交 60%／90% ⇒ `change_reasons` 含 `partial_fill:ETH` ⇒ B 段**單次**假空讀跳過、`market_open` 不增（原 0→0.8／0.95）；(b) R5-3c：place 新單部分成交 60% ⇒ 同上（原 0→0.8）；(c) R5-3a：容差內 4% 部分成交 ⇒ 標記（原 0.02→0.52）；(d) 新 oid `orig_sz=None` ⇒ `partial_fill_unknown:ETH`；(e) 舊 oid 上一輪已部分成交、本輪量不變 ⇒ **不**再標記；(f) 殘單對路徑（部分成交超過容差）⇒ `partial_fill:ETH`，與 (ii) 去重只出現一次；(g) R5-4：20 輪中 10 輪 D18 延後、0 成交 ⇒ `change_reasons` 全空、B 段 0 跳過、重讀 0、`deferred_alo_cross` 仍正確；settle 補單被延後亦同；(h) 全額成交仍 `settle_consumed`（回歸）；(i) 20 輪混合 0 標記（回歸）；(j) `_is_entry_spec`／`_is_entry_order` 對 trigger 回 False；(k) RUNBOOK D37(d) 文字 grep。變異 ≥6：(ii) 不比 `orig_sz`（a／b／c 紅）；(iii) None 不標記（d 紅）；舊 oid 也用 `orig_sz` 比（e 紅）；殘單對不記（f 紅）；延後不扣除（g 紅）；trigger 納入進場（j 紅）。
- **真實工作樹**做；`PYTHONDONTWRITEBYTECODE=1`、印被改行、清 `__pycache__`。
- 驗收：t8–t12／fresh_read／loop_fresh_read／重播／consumed_and_5xx 全綠；全套 0 failed（前景，最終程式碼）；ruff；3.11；commit `fix(v2-T12): 新 oid 以 orig_sz 偵測部分成交（D38）；D18 延後不算已消耗（D39）；RUNBOOK 補外部手動交易須知`。

### T13 `@inline` ── D40 新 oid 部分成交改以本輪送出的 spec 量判定；origSz 僅作退路（複審 6）

- 檔案：`src/spark/copytrade/orders.py`（`ReconcileResult.written_specs: tuple[OrderSpec, ...] = ()`；`_reconcile_orders` 在 modify ok／ambiguous modify／首輪 place ok／settle 補單 ok 四處收集進場型 spec；新增純函式 `_match_new_oids_to_written(new_orders, written_specs, *, px_rel_tol) -> dict[int, Decimal]`（oid → 期望量，兩段 1:1 貪婪）；`sync_open_orders` 先算新 oid 集合（`after` 進場型且不在 `state.fresh_me.last_entry_orders`）→ 配對 → 傳給偵測）、`src/spark/copytrade/fresh_read.py`（`partially_filled_entry_orders(state, after_orders, *, expected_sz: Mapping[int, Decimal] = {})`：新 oid 若在 `expected_sz` ⇒ `sz < expected` 才 `partial_fill`；否則退回 (ii)(iii)）、`deploy/RUNBOOK.md` §5.8m（「複審 5 修正」的 D38 句改寫；新增「複審 6 修正」寫 D40；§8 部署後觀察項）、新檔 `tests/test_copy_safety_net_v2_t13.py`、`tests/test_copy_safety_net_v2_t12.py`（既有斷言若因簽名改變需調整只改呼叫方式）。
- 測試 rig：`BookLive` 加一個開關模擬「modify 保留改前 `orig_sz`」（B2），預設維持現行（B1）。
- 測試：(a) B2 下改大量（0.5→0.54）且新 oid 部分成交 0.02／0.04（容差內）⇒ `partial_fill:ETH`、B 段單次假空讀跳過、部位不變（原 reviewer P1 0→0.54）；(b) B2 下 20 輪改小量交替（0.5／0.46）0 成交 ⇒ 0 標記、0 跳過、0 重讀（原 P1b 跳 10）；(c) B1 下 T12 (a)(b)(c) 全部維持原結果（回歸）；(d) 配不到的新 oid（rig 直接塞一張外部單）⇒ 退回 `orig_sz` 規則：`orig_sz` 缺 ⇒ `partial_fill_unknown`、`sz < orig_sz` ⇒ `partial_fill`、相等 ⇒ 不標；(e) ambiguous modify 實際落地（rig 讓 modify 拋例外但仍換 oid）⇒ 新 oid 配到 ambiguous spec，部分成交可判、未成交不誤標（`ambiguous_modify` 本身仍標記——斷言 reasons 內容）；(f) 同幣同價兩張不同量（0.5／0.3）皆新掛、其中一張部分成交 ⇒ 兩段配對正確、只標記該張且不誤標另一張；(g) settle 補單 ok 的新 oid 也在 `written_specs`；(h) 20 輪混合（改價、加層、撤層、D18 延後）0 成交 ⇒ 0 標記（回歸 R5-5／R5-4）；(i) RUNBOOK 文字 grep。變異 ≥6：配對結果不用（a 紅）；`written_specs` 漏 modify ok（a／b 紅）；漏 settle 補單（g 紅）；配不到時不退回 orig_sz（d 紅）；兩段配對改單段（f 紅）；`<` 改 `<=`（b／h 紅）。
- **主線程裁決（2026-10-11，builder 回報 t12 (d) 語意衝突）**：選項 A——t12 `test_d_new_oid_without_orig_sz_marks_partial_fill_unknown` 的劇本改為該單是「本輪沒寫入的既有單」（`my_orders` 含 `_lo(500)`），讓它配不到 `written_specs` 而退回 `orig_sz` 規則；斷言不動。不採選項 B（配到仍看 `origSz` 會讓 D40 重新依賴該欄位）。`rebaseline_entry_orders` 加選用參數 `expected_sz`（預設空）原樣傳遞，可做。
- **真實工作樹**做；`PYTHONDONTWRITEBYTECODE=1`、印被改行、清 `__pycache__`。
- 驗收：t8–t13／fresh_read／loop_fresh_read／重播／consumed_and_5xx 全綠；全套 0 failed（前景，最終程式碼）；ruff；3.11；commit `fix(v2-T13): 新 oid 部分成交以本輪送出的 spec 量判定、origSz 僅作退路（D40）`。

## 6. 不做清單（複述，派工 prompt 要帶）

鏡射單扣法／讀我方或 leader 掛單進 B 段；W4；GTC RO 重送；unified account scale 抖動；撤單延遲多持一格；`place`／`cancel` 例外中止整輪；儀表板健康判定。

---

## 7. 預註冊的重播數字（來自 10-07 真實資料；驗收用精確值）

- 開頭快照：leader ETH 0.0913、HYPE 0.53、BTC 0.00102（02:01:09 成交前）；`scale_snapshot = 5.18`（資金使用率 0.4、權重 1、自己權益 1090 ⇒ leader `account_value≈84.17`）。
- 安全網時（02:01:33）：leader ETH 0.2463、HYPE 1.61、BTC 0.00515；`scale_fresh = 3.43`（`account_value≈127.1`）。
- 自己（02:01:33）：ETH 1.1958、HYPE 8.61、BTC 0.03359。
- **R1 期望**：ETH `t0=0.4729→round 0.4729`（sz_dec 4 ⇒ 0.4729）、`t1=0.8448`、`lo=0.4729`、`hi=0.8448`、`m=1.1958 > hi×1.08=0.9124` ⇒ **減 0.3510**（1.1958−0.8448；精確）；HYPE `t1=5.52`（sz_dec 2）⇒ 減 **3.09**；BTC `t1=0.01766`（sz_dec 5）⇒ 減 **0.01593**。事故實際賣出 ETH 0.7226／HYPE 5.87／BTC 0.02298 **不得出現**。
- **R5（鏡射落後）**：leader 1.0→1.5、scale 4、自己 4.0、鏡射買單 2.0 掛著 ⇒ `t0=4.0、t1=6.0、lo=4.0、m=4.0` ⇒ **零動作**（v1 探針 `probe_stale_orders.py` 在 v1 會買 2.0）。
- **R2／R3／R4 字串**：`Cannot modify canceled or filled order`；`Order was never placed, already canceled, or filled. asset=1`；`Post only order would have immediately matched, bbo was 2611.4@2612.8. asset=1`；`hyperliquid.utils.error.ServerError(500, 'null')`。

---

## 8. 部署前檢查（主線程；**需使用者另行授權**）

避開週六 UTC 08:30–09:20；三查（維護旗標／spark-sync／Wants）；`systemctl --failed`；`df -h`（trace 16 MB×14 天×3）；確認三個 follower 帳戶模式（`userAbstraction`）；整包 rsync、`DEPLOYED_VERSION` 明確 hash、備份；**一次重啟一個 follower**，各觀察 ≥3 分鐘（無 ERROR、trace 每分鐘一筆 cycle 行、`consecutive_skips==0`、心跳正常）；驗收指紋：下一個波動事件的 cycle_trace 中，B 段減倉決策的 `hi == t1`（新鮮目標）、無反方向過賣；`snapshot_age_s` 只作參考（預期 10–30 秒）。**排除條件**（§6 W2）：若 decision 行的減倉伴隨同幣同價的 reduce-only 鏡射單在簿上，該筆「多減一張、下一輪買回」屬已接受殘留，不算指紋失敗。
**v2 新增項（複審 7 S2 補）**：首輪後記憶檔含 `leader_had_positions`／`me_had_positions`，視情況另有 `me_last_entry_orders`／`me_pending_change`（D34）；部署後 24 小時觀察 cycle_trace 的 `a_phase_change_reasons`（RUNBOOK 清單第 9 項）：`partial_fill_unknown` 或「`partial_fill` 卻查無成交」⇒ 回頭查 HL `origSz` 語意（D40 退路假設）；`settle_consumed` 要對得上成交／外部撤單；B 段 `me:` 跳過輪次對階梯 leader 應接近 0。任一不符先存 trace 片段、再開新 plan。

## 9. 完成定義

- reviewer **0 Critical** 且 **0 Warning**；Warning 一律修完再審，**不因輪數停下**（使用者 2026-10-11 裁決，取代原「五輪仍有 Warning ⇒ 停下請使用者」）。只有修法出現多個分岔且代價對使用者可見（新增 API 呼叫、功能退化、接受殘餘）才停下請裁決。
- 全套 0 failed、ruff 全過、每個行為 task 的變異驗證有紀錄且主線程重做過至少一個。
- plan 外的任何裁決都先寫回本檔再派工。
- **狀態（2026-10-11）：複審 7 達成 0 Critical／0 Warning（§13）。程式碼凍結於 `2c9bdfd`；待使用者授權部署（§8）。**

## 10. 基線（T0 填）

- `uv run pytest -q`（2026-10-10，分支 fix/copytrade-safety-net-v2 @3f08919，程式碼＝main@7e4b509）：**3556 passed, 42 deselected, 1 warning**（4 分 20 秒）
- `uv run ruff check src tests scripts`：**All checks passed!**

## 11. 待使用者確認的點

1. **D3 不對稱規則**（減倉用新鮮目標、加倉只到 `min(開頭, 新鮮)`）取代鏡射單扣法——同意？
2. **D4／D5**：輪內方向改變 ⇒ 該幣跳過一輪；leader 在 A 段平倉 ⇒ 照 flatten——同意？
3. **D10–D13 守衛同時套在 leader 與自己**；自己讀空 ⇒ 整輪（開頭）或 B 段（安全網）跳過——同意？
4. **D25 不派 Haiku**、全部 Sonnet 5.5——同意？
5. 任務切分 T1–T7 與不做清單——同意？

## 12. Haiku vs Sonnet：第一版（archive 分支）的派工成果對照（使用者 2026-10-10 要求）

資料來源：第一版 19 個 task 的 agent 回報（subagent tokens／tool 次數／秒數），加上主線程驗收與 7 輪 reviewer 的發現。
**不含**主線程為了修正偏差自己花的 tokens（那是最貴的部分，見結論）。樣本小（Haiku 5、Sonnet 13、Opus 1），只能看方向。

| 模型 | task | 平均 tokens／task | 平均耗時／task | 結果 |
|---|---|---|---|---|
| Haiku 4.5 | T1 拒因分類、T3 trace 寫入器、T4 守衛純邏輯、T5 設定欄位、T10 CRIT 文案 | ≈75k（60k–84k） | ≈2.7 分 | **3/5 需主線程修正**：T3 為了配合自己發明的假檔名測試在 prune 加了無意義解析；T4 第一次讀取拋例外時沒照 plan 重讀、也沒標記跳過（計數會少算）；T10 的重置測試沒有斷言時間值（reviewer S1，變異存活）。T1、T5 無瑕疵。另 T3 違反「不要背景跑全套」的指示 |
| Sonnet 5.5 | T2、T6–T9、T11–T18（13 個） | ≈125k（96k–157k；含兩次停下再續的合計 ≈200k–350k） | ≈8 分（停下再續者 15–25 分） | **0 次偏離 plan**；**4 次依升級協定正確停下**（T6 既有 fixture、T14 panic.py、T17 斷言衝突、T18 config 未驗＋舊測試衝突），每次都抓到 plan 本身的缺口。實作 bug 2 個：D17 用 `net.get("skipped")` 判整輪略過（真實回傳是清單；複審 7 Critical）、D16 起扣完的量未捨到格點（plan 未寫；複審 6 Critical）。另 W2 缺 `scale_fresh` 鎖定測試 |
| Opus 5.5 | T19 外科手術式撤回 | 121k | 7.7 分 | 無瑕疵；主動指出 trailer 與探針 import 問題 |

**解讀**
- Haiku 每個 task 省約 40% tokens、60% 時間，但 60% 的 task 需要主線程讀碼、修碼、重跑——主線程 context 是全 session 最貴的資源，而且 Haiku 的偏差型態是「改寫規格讓自己的測試過」，這種錯不會被它自己的測試抓到，只能靠主線程或 reviewer。
- Sonnet 的錯是「plan 沒寫就沒做」或「布林／清單這類型別直覺」，而且遇到 plan 沒涵蓋的判斷會停下來問，4 次停下每次都替 plan 補了一個真缺口。
- 兩個 Haiku 零瑕疵的 task（T1、T5）共同點：plan 給了**完整真值表＋照抄既有寫法**的規格，沒有任何需要理解既有架構的部分。

**建議（寫進 D25，供以後派工參考）**
1. 凡是有行為、要讀既有程式碼、或測試要自己設計情境的 task ⇒ **Sonnet**。這類 task 用 Haiku 的「省」會在主線程修正時全數吐回去，還外加一輪 reviewer。
2. Haiku 只留給「plan 已給完整真值表或逐字規格、照抄既有寫法、不碰既有行為」的純機械 task（例如新增拒因字串分類、新增有預設值的設定欄位），且 prompt 要明寫「不得為了讓測試通過而改規格」。
3. 高風險撤回／外科手術 ⇒ Opus（T19 一次到位）。
4. 不論模型，**主線程親跑驗收**與 reviewer 複審才是抓到問題的地方；Haiku 省下的 2–3 分鐘遠小於一次主線程修正（讀碼＋改碼＋重跑全套 ≈ 10 分鐘＋數十 k 主線程 tokens）。

## 13. 進度紀錄

| task | commit | 主線程驗收 | 備註 |
|---|---|---|---|
| T0 | 3f08919／d83070b | 基線 3556 passed、ruff 過 | |
| T1 | 86c5248 | 316 個相關測試；全套 3713 passed（主線程親跑）；9 檔 3.11 相容；13 個變異皆抓到 | builder 回報 4 個 plan 缺口 ⇒ D10a／D13a／D20 過渡期（d37a887） |
| T2 | 3aa3d49 | 197 個相關測試；全套 3738 passed（主線程親跑）；7 個變異皆抓到；主線程重做 M7（第一次 sed 命中錯誤行＝假陰性，改行號後 5 個測試轉紅） | 無 plan 缺口回報 |
| T3 | 0c3efdf | 226 個相關測試；全套 3766 passed（主線程親跑）；7 個變異皆抓到；主線程重做 M3（ledger 不併 OR）轉紅 | builder 回報：空手 follower 每輪重讀＋2 秒 ⇒ D11a；tripped 輪不計 ⇒ D12a；4 個既有 fixture 的 ntl=0 改合理值、1 個呼叫序列斷言改新順序 |
| T4 | c150f69 | 453 個相關測試；精確重播斷言 ETH 0.3510／HYPE 3.09／BTC 0.01593 且 0.7226／5.87／0.02298 不出現；全套 3810 passed（主線程親跑）；9 個變異皆抓到；主線程重做 M1（第一次 sed 命中 docstring＝假陰性，改行號 418 後 R5 紅） | builder 開工前抓到守衛死鎖（空手 follower 永不開倉）⇒ D10b；另 D7a／D20b／D23a；兩個既有測試的 monkeypatch 目標由 `sync_positions` 改 `sync_positions_v2`（斷言不變） |
| T5 | f382ef2 | 186 個相關測試；全套 3834 passed（主線程親跑）；7 個變異皆抓到；主線程重做 M3（first_error_at 不重置，先印出被改行確認非註解）轉紅 | builder 自定 trace 欄位格式（`snapshots` 四組、`leader_orders_ms` before/after）；tripped／halt 早退不寫 trace（plan 未要求） |
| T6 | e898a21 | 18 個重播測試（含 A＋B 合併輪）；12＋2 個變異皆抓到；主線程重做 V8（`net.get("skipped")` 真假值 ⇒ D21 streak 紅）與 V2（減倉目標改 lo ⇒ R1 紅，BTC 送出 0.02831）；全套 3852 passed（主線程親跑） | builder 發現 macOS 上同長度變異在同一秒內會沿用舊 `.pyc` ⇒ 假陰性；對策：`PYTHONDONTWRITEBYTECODE=1`＋清 `__pycache__`（寫進 §0） || T7 | 1eae7ad | RUNBOOK §5.8m（3186–3379）；回顧文件與工具自 archive 挑回（只限 docs 路徑）；60＋11 個 trace 鍵全部 grep 命中；jq 範例以真實產出的 JSONL 實跑 | builder 列出 4 處 plan 與原始碼差異（env 鍵存在、額外驗證、decision 行形狀）⇒ 文件以原始碼為準，見 §14 |
| 複審 1 | — | reviewer（opus）：**Critical 1**（自己側守衛在「自己剛開完倉的下一輪」與「重啟後」失效 ⇒ 讀空再開一次，主線程重跑探針 1.0→2.0 成立）、W1（leader 全平晚一輪）、W2（減倉側鏡射落後多賣一格後買回）、W3（B 段開倉例外中止其餘減倉）、S1–S4；7 個變異皆抓到；D3 推導表 17 列無多買情境 | 裁決：D28（C1）、D29（W3）、D30（S1）、D31（S2）→ T8；W1／W2 明寫接受進 §6／§8；S3 進 RUNBOOK；S4（刪舊 sync_positions）backlog |
| T8 | 48f3459 | 271 個相關測試；全套 3886 passed（主線程親跑）；7 個變異皆抓到；主線程重跑 reviewer C1 探針（新匯出副本）：「自己剛開倉後讀空」⇒ 1.0 → 1.0、有重讀（修掉）；「重啟」探針仍 2.0——因該探針直接 `ReconcileState()` 繞過 `run_copytrade` 的 `load_memory`，且其 tmp root 無記憶檔，不構成反證（T8 有 `test_c1_2_restart_with_persisted_memory_skips_empty_read`／`test_main_loads_fresh_read_memory_into_state` 覆蓋真實路徑） | builder 停下一次（D10b 測試劇本衝突）⇒ 裁決選 A；rearm 僅 live；`update_leverage` 回傳 bool；首次部署無記憶檔的殘餘窗口寫入 RUNBOOK；開倉例外不計入升級帳（D29 原意） |
| 複審 2 | — | reviewer（opus）：C1 兩條路（自己開倉後、真實重啟路徑）已關；**Critical**：鏡射單成交後下一輪讀空仍 1.0→2.0（同根因第三種形狀；主線程重跑成立）；W1 500 已落地的開倉不 rearm（成立）、W2 例外輪不落記憶檔（成立）、W3 `if not left` 無測試（M7 存活）、W4 RUNBOOK 一句不實；S：C1 測試 rig 靠 scale=0 遮掩、rearm 僅 live 無測試、plan §8 未補 W2 | 裁決：**D32 收斂於守衛邊界**（有進場掛單或有寫入嘗試 ⇒ 不走捷徑）取代逐點 rearm、D33、測試補強 → T9；第 3 輪＝T9 複審 |
| T9 | 59ea8bb | 297 個相關測試；全套 3912 passed（主線程親跑）；11 個變異皆抓到；主線程重跑 reviewer r2 三支探針：landed-500、exception persist、真實重啟皆轉綠；`mirror_fill` 探針仍紅——但該探針把成交模擬成「部位憑空出現、簿上無任何掛單消失」，等同外部手動交易（§6 新增接受項），非真實鏡射成交路徑 | builder 兩度依升級協定停下（字面 D32 擋不住 ⇒ D32 修訂版）；自加兩判斷：`pending_change` 時空讀值即視為可疑、三個標記點僅 live；`confirmed_flat` 路徑只清 pending 不歸零計數（保留捷徑重新武裝）——皆接受 |
| 複審 3 | — | reviewer（opus）：C1 非重啟路徑確認關閉（真實鏡射成交三種變體皆擋住；主線程對 r2 探針的判定成立）；**Critical**：重啟丟失 `pending_change`／進場單基準 ⇒ 停機期間成交或結果不明開倉後重啟，兩次假空讀即翻倍（P4 0.5→1.0、P6 1.0→2.0）；W1 我方自己改單／撤單被當帳戶變化 ⇒ 階梯 leader 下空手 follower 每兩輪跳一輪（P3 20 輪跳 10）；W2 死碜 `# EXPERIMENT option A`；S A 段 live-only 無測試；13/14 變異抓到 | 裁決：D34（持久化 pending＋基準）、D35（排除自己的撤單／改單、A 段條件去 `modified`）→ T10；第 4 輪＝T10 複審 |
| T10 | 8809c83 | 182 個相關測試（t8／t9／t10／fresh_read／loop_fresh_read／重播）；全套：副本未設 `PYTHONPATH` 時 3937 passed＋6 failed（全是 `test_script_default_paths` 路徑錨定假象，見 §0；該 6 條在真實工作樹 6 passed）；設 `PYTHONPATH=src` 後重跑全套 **3943 passed, 42 deselected**（主線程親跑，240 s，與 builder 宣稱一致）；ruff／3.11／`grep EXPERIMENT src` 無輸出；主線程重跑 reviewer r3 探針（匯出副本）：P3 20 輪 0 跳過 0 重讀、P3b 0/10、P5 不標記；P4／P6 照原探針仍紅——因探針「照抄舊版 `run_copytrade.py:725-727`」手動只還原兩個舊鍵、繞過新接線 `restore_from_memory`（記憶檔內已有 `me_last_entry_orders`／`me_pending_change`）；改成真實接線後 P4 0.5→0.5、P6 1.0→1.0 皆 `me:suspicious_flat_unconfirmed` 跳過 | builder 依升級協定停下一次（D34 與兩條整份 JSON 精確斷言衝突）⇒ 裁決選項 A；實作時發現第三條同形斷言（t8 `test_run_cycle_persists_memory_on_normal_and_on_skip_exit`）同法最小修改；10 個變異中 M6／M10 第一次存活 ⇒ 補強 (h) 逐輪斷言、新增 (h2) 後轉紅；`own_removed_oids` 不落檔（重啟後第一輪可能多一次保守重讀，RUNBOOK 已寫） |
| 複審 4 | — | reviewer（opus，副本已設 `PYTHONPATH=src`）：D34 正確、接線完整；**Critical C1**：D35(b) 去掉 `modified>0` 後，modify 換成的新 oid 若在下一輪開頭前成交，基準裡只有被排除的舊 oid ⇒ 不偵測 ⇒ 同一輪兩次假空讀即翻倍（探針 R4-1a／1b 0.5→1.0；主線程重跑成立）。reviewer 指出 §6 接受它的前提不成立：時窗是**整個輪距**而非「立即」（每輪改價的階梯 leader 正是 D35 服務的族群，其鏡射單成交幾乎全落在此窗）；且 live 的 settle 階段已重讀一次簿（`orders.py:894`），新 oid 可觀測、只是被丟掉；**W1**：開頭讀簿後、A 段撤單前的部分成交被排除清單吃掉（R4-8 0.2→0.4，需三次假空讀、數秒窗）；**W2**：RUNBOOK 寫刪記憶檔無害，實測刪成舊格式後重啟 1.0→2.0（R4-6）；S：測試 rig `cancel_order_result` 對不在簿上的 oid 回 ok 失真、RUNBOOK 重啟成本句不實（空手＋leader 有掛單重啟本就跳 2 輪，D28 既有）；12 變異全抓到；全套 3943 passed；ruff 過 | 主線程判定：C1 成立，§6 的接受是**我的誤判**（低估時窗）；使用者 2026-10-11 裁決選 **R**（A 段後以 settle 簿重設基準＋B 段前標記；W1 接受進 §6；F＝每輪讀成交紀錄列為部署後候選）⇒ D36／D37 → T11；並取消「五輪停下」規則（§9） |
| T11 | c639006 | 8 檔目標測試 239 passed；全套 **3975 passed, 42 deselected**（主線程親跑，真實工作樹，241 s）；ruff／3.11／`grep own_removed\|ignore_oids src tests` 歸零；主線程重跑 reviewer r4 探針（r5 匯出副本、`PYTHONPATH=src`）：**R4-1a／1b 0.5→0.5 轉綠**（C1 關閉）、R4-1c／2b／3／4／5／6／7 綠；R4-2a 紅＝reviewer 自認斷言過嚴（D28 既有重啟跳 2 輪）；R4-8 因引用已移除的 `last_own_removed_oids` 報 AttributeError（W1 已接受，不評） | builder 兩度依升級協定停下：(1) t10 h2／t9 test_a[0] 轉紅 ⇒ 裁決改斷言＋修假簿；並由 builder 第 9 點代價（`entry_placed` 每輪標記 ⇒ leader 每輪加新單時 B 段每輪跳過）推出 **D36a 拿掉 `entry_placed`**；(2) t10 test_h live 對照組改 ambiguous modify。16 變異全抓到（M6 曾存活 ⇒ 補 i2）；trace 加稀疏鍵 `a_phase_change_reasons`；新增 d4（leader 6 輪連加新單、B 段 0 跳過） |
| 複審 5 | — | reviewer（opus，`PYTHONPATH=src`）：D36 對全額成交／撤單 consumed／R4-1 修正成立、P3／d4／20 輪混合 0 退化；**Critical C1**：新掛或改單換成的新 oid 在 settle 讀前**部分**成交 ⇒ 容差內配對成功或殘單對被丟棄、`partially_filled_entry_orders` 只看舊 oid ⇒ 不標記 ⇒ B 段單次假空讀超倉（R5-3b 60%／90% 0→0.8／0.95、R5-3c 0→0.8、R5-3a 4% 需三次 0.02→0.52；主線程重跑 6 支皆紅成立）；**W1** D18 延後被算成 `settle_consumed` ⇒ 誤標記（R5-4 20 輪跳 10）；**W2** RUNBOOK 外部手動交易須知被 T11 刪掉；S trigger 排除無測試（M6／M6b 存活）；11/13 變異抓到；全套 3975 passed | 裁決：D38（新 oid 以 `orig_sz` 偵測部分成交＋殘單對亦計）、D39（延後 spec 扣除）、D37(d)(e) → T12 |
| T12 | ed037e9 | 9 檔目標測試 266 passed；全套 **4002 passed, 42 deselected**（主線程親跑，真實工作樹，234 s）；ruff／3.11 過；主線程重跑 reviewer r5 全部 15 支探針（r6 匯出副本、`PYTHONPATH=src`）：**原紅 6 支（R5-3a 60%／4%、R5-3b 60%／90%、R5-3c 60%、R5-4）全部轉綠**，其餘 9 支維持綠 | builder 未停下；14 個變異全被抓到；回傳型別 `PartialFill(order, reason)` NamedTuple；`_pair_residuals` 回 3 元組、`_settle_verify` 回 4 元組；殘單對與 (ii) 設計重疊 ⇒ (a)(b) 由兩條規則各自抓到、M1 由容差內 4% 的 (c) 抓到；RUNBOOK 多補 §6 改單變體與 D37(e) 說明 |
| 複審 6 | — | reviewer（opus，`PYTHONPATH=src`）：**0 Critical**（指定 7 類假陰性情境全部有標記；D39 不扣過頭）；**W1**：D38 (ii) 依賴「改單後新 oid `origSz`＝改後量」未實測語意，plan §6／RUNBOOK 寫成事實；反面語意下改大量容差內部分成交被蓋（P1 0→0.54 單次假讀）、改小量每輪誤標（P1b 20 輪跳 10、P6 跳 6）；11 變異全抓到；r5 15 支全綠；全套 4002 passed；觀察：P3 settle 把正確的 0.3 新單當多餘撤掉（T12 前既有，下一輪重掛） | 裁決：D40（新 oid 以本輪送出的 spec 量判定，origSz 僅配不到時的退路）→ T13；實測 origSz 語意列為部署後觀察而非前置 |
| T13 | 2c9bdfd | 10 檔目標測試 303 passed；全套 **4039 passed, 42 deselected**（主線程親跑，真實工作樹，238 s）；ruff／3.11 過；主線程重跑 reviewer r5＋r6 全部 34 支探針（r7 匯出副本、`PYTHONPATH=src`）：**全綠**，含 r6 的 B2 語意 4 支（P1×2、P1b、P6 原紅）與 r5 15 支 | builder 依升級協定停下一次（t12 (d) 劇本被 D40 取代 ⇒ 裁決選項 A 改劇本為非本輪所寫的既有單）；14 變異全抓到（M12「新 oid 篩選不排除基準內舊 oid」第一次存活 ⇒ 補 g6）；B2 開關做在 t13 的 `ModBook` 子類、不動 t10 `BookLive`；ambiguous spec 取改後目標；`written_specs` dry 也收集但只在 live 使用 |
| 複審 7 | — | reviewer（opus，`PYTHONPATH=src`）：**PASS，0 Critical、0 Warning**；指定 7 類情境＋格點＋20 輪混合（B1／B2）全部安全；r6 P1／P1b／P6 重現綠；11/11 變異抓到；全套 4084（4039＋45 探針）；**S1** 價格鏈狀相近階梯下 D40 配對不遞移 ⇒ 低層新 oid 退回 origSz 規則，fuzz 顯示需 B2＋兩張同輪部分成交才漏（單張 0 組）；**S2** plan §8／RUNBOOK 清單缺 v2 新增觀察項；觀察：`_plan`／`_verify_diff` 同有不遞移問題（既有、2bp 內）；主線程重跑 S1 探針紅、P1／P1b／P6 綠 | 主線程：S1 寫進 §6 接受殘餘＋backlog；S2 補 plan §8 與 RUNBOOK 清單第 8／9 項（主線程自改，docs only）；**完成定義達成** |


## 14. T7 發現的 plan 與實作差異（文件以原始碼為準，不回頭改程式）

- D13 寫「env 不需新增」，意思是**不需要設定**就有預設值；實際 `config.py` 確實讀 `COPY_FRESH_READ_RETRY_S`／`_WARN_AFTER`／`_CRIT_AFTER`、`COPY_CLOSE_EXC_CRIT_AFTER`（可覆寫）。
- `config.py` 另有驗證：`fresh_read_retry_s ≥ 0`、`1 ≤ warn_after ≤ crit_after`、`close_exc_crit_after ≥ 1`、`0 ≤ size_tolerance < 1`。
- trace 的 decision 行是**每輪一筆**（內含 `decisions[]` 與 `targets{}`），不是逐幣多筆；分支名含 `within_band`，無 `diff` 欄位。
- 部署前檢查多一條：follower env 的 `COPY_SIZE_TOLERANCE` 必須在 `[0,1)`（否則引擎拒啟）。
