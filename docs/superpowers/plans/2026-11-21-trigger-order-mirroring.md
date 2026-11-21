# Plan：鏡射 leader 的止盈／止損觸發單（trigger order mirroring）

> 狀態（2026-11-21）：**使用者已全數確認（「可以，GO」），依序派工中。** 進度：T1 完成（腳本 `90c6751`；testnet 實跑結果見 research 檔，結論已寫回 §4 D-P1…D-P13）；T2 完成（`a35ebbe`）；T3 完成（`40e827f`）；T4 完成（`cf104dd`）；T5 派工中。
> 確認內容：D1、D2、D3（改判：條件進場單也要跟）、D5（開關預設關＋告警降級）、D13、D14、R7、
> T2 先獨立部署、T1／T5 在 testnet 下單。
> 分支（確認後才開）：`feat/trigger-order-mirroring`（自 main `46c8d99` 切出；逐 task commit、**不 push**）。
> 部署不在本 plan 授權範圍內：實作與審核完成後另行徵詢（§9）。

## 1. 背景與證據

名詞：**觸發單**＝價格碰到觸發價才生效的單（止損、止盈）。**倉位止盈止損**＝交易所介面上數量顯示
「Close Position」的那種，數量跟著倉位走。**follower**＝跟單的客戶帳戶；**leader**＝被跟的帳戶。

2026-11-21 leader `0xfB9C…9760` 掛了三種減倉單，follower `0x438b…dce9`（account `f438b3…`）
與 `0x6b3e…232c`（`f6b3e7…`）的鏡射結果：

| leader 掛的單（主網 `frontendOpenOrders` 實測欄位） | follower | 程式路徑 |
|---|---|---|
| reduce-only 限價買 0.015 @ 57,840（`isTrigger:false`） | ✅ 15 秒內鏡射 | 既有路徑 |
| 倉位止損／止盈：`orderType:"Stop Market"`／`"Take Profit Market"`、`sz:"0.0"`、`isPositionTpsl:true`、`reduceOnly:true`、`limitPx` 為滑點保護價（67100／63624） | ❌ 沒有，**也沒有任何告警** | `orders.py:303-306`：`size = _round_size(0 × scale) = 0` → `continue`（靜默） |
| 固定數量止損限價：`orderType:"Stop Limit"`、`sz:"0.015"`、`triggerPx:"61000.0"`、`limitPx:"61000.0"`、`isPositionTpsl:false`、`reduceOnly:true`、`tif:null` | ❌ 沒有 | 數量非 0 → 進 desired → `executor.py:230-233` 拒絕（M1 限制）→ settle 重試再拒 → `orders.py:864` 每輪判同步失敗發 CRIT（900 秒去重）＋ `loop.py:440` warn |

- 引擎本身健康：同一時間的限價單都有鏡射。這是移植自 hl-copytrader 時就帶來的缺口，不是回歸。
- 替代保護現況：leader 止損觸發、倉位歸零後，部位安全網（每 60 秒一輪）會市價平掉 follower。
  缺口是「最多晚一輪＋市價滑點」以及「引擎或 API 異常時完全沒有止損」。
- 告警只送 Telegram、不落檔（`notifier.py`），正式機 journal 無法驗證 CRIT 是否送達。
- HL 官方文件（2026-11-21 讀取）：`grouping` 允許 `"na" | "normalTpsl" | "positionTpsl"`；
  觸發單線格式 `"trigger": {"isMarket": Boolean, "triggerPx": String, "tpsl": "tp" | "sl"}`；
  「The mark price is used to trigger TP/SL orders」；「TP/SL market orders have a slippage tolerance of 10%」；
  「If a specific size is configured on these TP/SL orders, they will be fixed-sized」。
  **文件沒寫** `positionTpsl` 的數量欄該送什麼、成功回應的形狀、`builder` 能否併用 → 由 T1 實測。
- SDK 0.24.0：`Exchange.order(..., order_type, reduce_only, cloid, builder)` 支援 trigger；
  分組要走 `Exchange.bulk_orders(order_requests, builder, grouping)`（`exchange.py:163`）。

## 2. 目標

1. leader 的觸發單在 follower 帳戶以同型態掛上交易所，leader 改價／撤單時跟著改／撤。涵蓋：
   倉位止盈止損、固定數量的止損／止盈（市價與限價型）、以及**條件進場單**（非 reduce-only，觸發後開新倉）。
2. 任何沒被鏡射的觸發單都有記錄與告警，不再靜默，也不再每 15 分鐘發一則無法處理的 CRIT。
3. 以預設關閉的開關上線，逐個 follower 啟用。

## 3. 不做（範圍外，請勿順手改）

- 「掛單時附帶的止盈止損」（`grouping="normalTpsl"` 的子單）在母單成交前不鏡射；母單成交、
  子單變成獨立觸發單後才依一般規則鏡射（D14；follower 端一律不用 `normalTpsl` 分組）。
- `/Users/jim/projects/hl-copytrader`：紅線，唯讀，不碰。
- 倍率（scale）公式、`size_tolerance`、`settle_seconds`、部位安全網、風控熔斷。
- `_slot_key`、`_pair_residuals`、`_actionable_missing` 的既有語意（觸發單沿用，不改）。
- 前端顯示、TWAP 單、`shadow.py` 對照邏輯（只更新過時註解）。
- 主網部署與任何主網寫入；memory 與 RUNBOOK 部署記錄（部署時一起寫）。

## 4. 裁決表（builder 遇歧義以此為準；plan 沒涵蓋的決策先回報主線程）

| # | 裁決 | 狀態 |
|---|---|---|
| D1 | 範圍：倉位止盈止損＋固定數量觸發單（市價型、限價型）都鏡射，走同一條路徑。 | 使用者 2026-11-21 確認 |
| D2 | 倉位止盈止損在 follower 端也掛成倉位止盈止損（`grouping="positionTpsl"`），數量由交易所跟著 follower 倉位走，不做倍率換算。數量欄實際送的值由 T1 的 P3 決定。 | 使用者 2026-11-21 確認 |
| D3 | 鏡射所有 `tpsl ∈ {"tp","sl"}` 的觸發單，**含非 reduce-only 的條件進場單**。只有 `orderType` 判讀不出 tp/sl 的 → `SkippedOrder(reason="trigger_unsupported")`＋warn。 | 使用者 2026-11-21 改判 |
| D13 | 條件進場單（非 reduce-only 觸發單）的規則**與現行非 reduce-only 限價單完全相同**：數量＝`_round_size(leader.sz × scale)`；成本熔斷（`no_new_exposure`）與抗單保護（`protected`）期間不掛、已掛的會被撤；名目 < `min_order_notional` → `small`；不需要有倉位；下單前由 `sync_open_orders` 既有迴圈設定該幣槓桿；寫入分類為非冪等（單次嘗試、不盲重試，靠下一輪對帳自癒）。 | 使用者 2026-11-21 確認 |
| D14 | leader「掛單時附帶的止盈止損」子單在母單成交前**不得**被當成獨立的 reduce-only 觸發單鏡射（否則 follower 會提早出現 leader 尚未生效的止損）。辨識方式由 T1 的 P12 決定；辨識不出就整類略過＋warn。 | 使用者 2026-11-21 確認（P12 後補細節） |
| D4 | 觸發單**一律撤單重掛，不走 modify**：SDK modify 無 builder 欄位（紅線 4）、也無法更換分組。觸發單不登記 `modify_fail_until`（不得連帶把同幣限價單降級）。 | 定案 |
| D5 | 開關 `COPY_MIRROR_TRIGGERS`（`CopySettings.mirror_triggers`，預設 `False`）。關閉時所有觸發單在 `_build_desired` 就略過（`reason="trigger_disabled"`）＋warn。**這會把固定數量觸發單現行的「每輪 CRIT」降為 warn**。 | 使用者 2026-11-21 確認 |
| D15 | **使用者 2026-11-22 改判（睡前交辦）：全部部署並驗證完成後，開關預設改為開**（`CopySettings.mirror_triggers` 預設 `True`），`f438b3…`（0x438b）也打開。執行順序：(1) T3＋T4 以預設關部署並驗證；(2) 只對 `f438b3…` 的 env 加 `COPY_MIRROR_TRIGGERS=true`、重啟、確認引擎正常推進；(3) 改預設為 `True`（T7）→ 測試 → 審核 → 部署，其餘 follower 隨之啟用。關閉方式改為在該帳戶 env 設 `COPY_MIRROR_TRIGGERS=false`。**停損條件**：T6 有修不掉的 Critical／Warning 或需使用者裁決的分岔、T5 的 E6／E9 反覆無法驗證、任何部署後驗證不乾淨 → 停在當下、開關維持關、留晨間檢查點。 | 使用者 2026-11-22 確認 |
| D6 | warn 去重：以「該幣 leader 觸發單的 oid 集合」為準，集合沒變就不重發（存在 `ReconcileState`，仿 `risk_off_warned_at` 先例）；不用 300 秒 TTL，避免洗版。 | 定案 |
| D7 | 固定數量觸發單：數量＝`_round_size(leader.sz × scale)`，**不**以 follower 倉位封頂（沿用 `orders.py:260-262` 既有規則；觸發時由交易所的 reduce-only 封頂）。 | 定案（T1 的 P6 複核） |
| D8 | 觸發價與限價 1:1 照抄 leader（含市價型的 `limitPx` 滑點保護價），兩者都經 `_round_px`。 | 定案 |
| D9 | reduce-only 觸發單遇 follower 無倉位、或方向不符（買單需空倉、賣單需多倉）→ `reduce_only_no_pos` 略過。 | 定案 |
| D10 | 啟用後觸發單下單失敗維持大聲：走既有 settle 重試 → CRIT 帶交易所拒因。 | 定案 |
| D11 | 固定數量觸發單換算後數量為 0 或名目 < `min_order_notional` → `reason="small"`＋既有 warn（不再靜默）。是否豁免最小名目由 T1 的 P11 複核。 | 定案 |
| D12 | `Order`（`exchange/base.py`）擴充 trigger 欄位，不另開新方法：`place_order` 仍是唯一掛單入口，builder 必帶。`ExchangeAdapter` 不新增任何 withdraw／transfer 類方法（紅線 3）。 | 定案 |
| D-P1 | 成功回應有兩種形狀：`grouping="na"` 的觸發單回 `{"resting": {"oid": …}}`（與限價單相同，現行解析可用）；`grouping="positionTpsl"` 回**字串** `"waitingForTrigger"`、沒有 oid。現行 `_parse_order_response` 遇字串會在 `status.get` 拋 `AttributeError`，T3 必修：`"waitingForTrigger"` → `ok=True`、`filled_size=0`；其他不認得的字串或形狀 → `ok=False`、保留 `raw`、不拋例外。 | T1 實測（research §3 P1、P3） |
| D-P3 | 倉位型一律送 `s=0`＋`grouping="positionTpsl"`。同分組但數量非 0 會變成固定數量單（`isPositionTpsl:false`），**禁止**這種組合：adapter 遇 `position_tpsl=True` 一律把數量送 0，不看 `order.size`。 | T1 實測（P3） |
| D-P4 | 倉位型可帶 builder（紅線 4 守得住）；非 reduce-only 觸發單也可帶 builder（P13）。 | T1 實測 |
| D-P5 | 交易所不會替同一倉位的多張倉位止損去重（兩張並存）。去重只靠引擎的 desired／mine 配對：多出來的那張在下一輪被當 extra 撤掉。reduce-only 觸發單因暫時性錯誤重試而重複掛出，屬可自癒，維持「reduce-only＝冪等可重試」分類。 | T1 實測（P5） |
| D-P6 | 確認 D7：未觸發的 reduce-only 觸發單數量大於倉位不會被修剪，引擎不封頂。 | T1 實測（P6） |
| D-P7 | 倉位歸零後，倉位型與固定數量 reduce-only 觸發單都在 1 秒內自動消失。引擎不需額外清理；與「無倉位就不放進 desired」（D9）一致，不會互相打架。 | T1 實測（P7） |
| D-P8 | 交易所**不拒**無倉位或方向不符的 reduce-only 觸發單，所以 D9 的引擎端過濾是唯一防線，必須保留並有測試。 | T1 實測（P8） |
| D-P9 | 撤觸發單與撤限價單回應相同，`cancel_order` 不必改。 | T1 實測（P9） |
| D-P10 | 觸發條件已成立的單不會被拒（P10），本次沒有取得任何觸發單拒因字串；D10 的 CRIT 走既有通用拒因路徑，不做字串特判。 | T1 實測（P10） |
| D-P11 | **reduce-only 固定數量觸發單豁免 `min_order_notional`**（$4.8 的單掛得上；不豁免會讓小帳戶沒有止損）。換算捨入後數量為 0 仍記 `small`。條件進場單（非 reduce-only）**不豁免**，維持與限價進場單相同的最小名目過濾。 | 主線程依 P11 裁決（修改 D11）；使用者 2026-11-22 確認 |
| D-P12 | D14 的辨識式：`frontendOpenOrders` 中，凡 oid 出現在任何一張最上層單的 `children` 陣列內者，即為「母單未完成的附帶子單」。讀取層標記 `OpenOrder.is_pending_child=True`；`_build_desired` 在現貨過濾之後、開關檢查之前略過（`reason="trigger_pending_child"`，帶 oid），**不發 warn**（這是預期行為：母單完成後它會變成獨立觸發單並被鏡射）。follower 端永不使用 `normalTpsl` 分組。 | T1 實測（P12） |
| D-P13 | 條件進場單：無倉位可掛、可帶 builder、未觸發不佔保證金、成功形狀 `resting`＋oid。D13 規則不變。 | T1 實測（P13） |

## 5. 檔案地圖

| 檔案 | 動作 | task |
|---|---|---|
| `scripts/testnet_trigger_probe.py` | 新增 | T1 |
| `docs/superpowers/research/2026-11-21-trigger-order-probe.md` | 新增 | T1、T5 |
| `src/spark/exchange/base.py`（`Order` 42-50、`OpenOrder` 78-96） | 修改 | T2、T3 |
| `src/spark/exchange/hyperliquid.py`（`get_open_orders` 152-175、`_parse_order_response` 419-446、`place_order` 448-457、`_round_px` docstring） | 修改 | T2、T3 |
| `src/spark/resilience.py`（`ResilientExchange` 92-142） | 修改 | T3 |
| `src/spark/exchange/fakes.py`（`FakeAdapter.place_order` 108） | 修改 | T3 |
| `src/spark/copytrade/config.py`（`from_env` 約 277-312） | 修改 | T2 |
| `src/spark/copytrade/orders.py`（`OrderSpec` 43、`SkippedOrder` 59、`spec_from_open_order` 78、`_orders_match` 101、`_build_desired` 218、`ReconcileState` 355、`_fmt_missing` 627、`_fmt_extra` 635、`_reconcile_orders` 673、`sync_open_orders` 882） | 修改 | T2、T4 |
| `src/spark/copytrade/executor.py`（檔頭 docstring、`_order_from_spec` 119、`_order_from` 208、`place_outcome` 220、`modify` 267） | 修改 | T4 |
| `src/spark/copytrade/loop.py`（436-444） | 修改 | T4 |
| `tests/test_copy_orders_triggers.py`、`tests/test_hyperliquid_trigger_writes.py` | 新增 | T2–T4 |
| 既有測試（§6 各 task 列名） | 修改 | T2、T4 |

## 6. Tasks

共同規則（每個 task 都適用）：
- 先讀專案根目錄 `CLAUDE.md` 的紅線；不讀取、不印出任何 `.env*`；測試全離線（`tests/conftest.py` autouse socket-ban）。
- 內部一律 `Decimal`；`float` 只在 adapter↔SDK 邊界。
- 驗收指令：`uv run pytest -q`（全綠）＋ `uv run ruff check src tests scripts`（0 錯）。
- 改到既有測試時，回報要逐條列出「哪個測試、原斷言、新斷言、為什麼」；不得刪掉斷言而不補等強度的替代。
- commit 格式 `feat:`／`fix:`／`test:`／`docs:` 一行敘述；不 push。
- 回報格式：(a) 結論一段；(b) 改動檔案清單（路徑:行號範圍）；(c) 每條驗收的指令與輸出末尾；(d) plan 沒涵蓋而你做了的決定。

### T1 `@inline` — testnet 探針：觸發單的真實行為

**動機**：文件沒寫的行為（§1）是假設不是事實；單元測試用的假資料照同一個假設寫，測不出錯
（工程原則 #1 欄位名教訓）。T3／T4 的實作細節取決於這裡的結果。

**檔案**：新增 `scripts/testnet_trigger_probe.py`；新增 `docs/superpowers/research/2026-11-21-trigger-order-probe.md`。

**做法**：
- 先讀 `scripts/testnet_modify_probe.py`（環境變數、keystore 選擇、輸出慣例）與
  `scripts/testnet_leader_drive.py`（`_assert_testnet`、單筆名目上限）。沿用同一套；
  base url 不含 `testnet` 直接 raise；單筆名目上限 $200；私鑰不進 log／print／例外。
- 本探針**直接呼叫 SDK**（`Exchange.order`／`bulk_orders`），因為 adapter 還不支援 trigger；每筆都帶 builder。
- 流程：開一個小倉位（BTC 或 ETH，名目約 $50–100）→ 逐項執行 P1–P13 → 每項後撤掉自己掛的單 →
  結束時市價平倉並驗證「無倉位、無掛單」。任何一步失敗也要執行清理（`try/finally`）。
- 每一項記錄：送出的 action（去簽章）、原始回應 JSON、之後 `frontendOpenOrders` 中該單的完整欄位。

| # | 要回答的問題 |
|---|---|
| P1 | reduce-only 止損**限價**單（`grouping="na"`、帶 builder）是否被接受？成功時 `statuses[0]` 的確切形狀？ |
| P2 | reduce-only 止損**市價**單（`isMarket:true`，`p` 送觸發價 ±10% 的滑點價）是否被接受？回讀的 `limitPx`？ |
| P3 | `grouping="positionTpsl"`：(a) `s="0"`、(b) `s`＝目前倉位量，各自是否被接受？回讀的 `sz`／`origSz`／`isPositionTpsl`？ |
| P4 | `positionTpsl` 併帶 builder 是否被接受？ |
| P5 | 已有一張倉位止損時再掛第二張（不同觸發價）：並存、取代、還是被拒？ |
| P6 | 固定數量 reduce-only 觸發單的數量 > 倉位量：是否被接受？會不會被修剪？ |
| P7 | 市價平倉後：倉位止盈止損與固定數量 reduce-only 觸發單是否自動消失（輪詢 10 秒）？ |
| P8 | 方向不符的 reduce-only 觸發單（空倉掛 reduce-only 賣）：接受或拒絕？拒因字串？ |
| P9 | 以 oid 撤觸發單（`Exchange.cancel`）的回應形狀是否與限價單相同？ |
| P10 | 觸發價已被越過（空倉的止損買單觸發價低於現價）：拒因字串？ |
| P11 | 名目 < $10 的固定數量 reduce-only 觸發單：接受或拒絕？ |
| P12 | 掛一張遠離市價的限價單並附帶止盈止損（`grouping="normalTpsl"`）：母單未成交時，子單在 `frontendOpenOrders` 是巢狀在母單的 `children` 內、還是也出現在最上層？若在最上層，有哪個欄位能和「已生效的觸發單」區分？（測完撤掉母單，確認子單一併消失） |
| P13 | **非** reduce-only 的止損市價單與止損限價單（條件進場，`grouping="na"`、帶 builder，觸發價遠離現價）：是否被接受？成功時 `statuses[0]` 形狀？未觸發時是否佔用保證金（比對下單前後的 `withdrawable`）？無倉位時可否掛？ |

**驗收**：
1. `uv run python -m scripts.testnet_trigger_probe --help` 可執行；`uv run ruff check scripts` 0 錯；`uv run pytest -q` 全綠。
2. 腳本內有 testnet 斷言與 `try/finally` 清理（回報附行號）。
3. builder **不執行**實際探測（需要 Keychain）。主線程親跑後把 P1–P13 結果表與原始回應摘錄寫進 research 檔，
   並把結論寫回 §4 的 D-P*。**D-P* 未寫回前不得派 T3／T4。**

### T2 `@inline` — 告警先行：觸發單不再靜默（可獨立部署）

**動機**：倉位止盈止損現在被靜默丟掉；固定數量觸發單每輪發無法處理的 CRIT 並多等兩次 settle。
本 task 讓開關關閉時的行為變成「一律略過＋一則說明清楚的 warn」。不依賴 T1。

**檔案與簽名**：
- `src/spark/exchange/base.py`：`OpenOrder` 尾端加 `is_position_tpsl: bool = False`。
- `src/spark/exchange/hyperliquid.py` `get_open_orders`：`is_position_tpsl=bool(o.get("isPositionTpsl", False))`
  （欄位名已由主網回應驗證，見 §1）。
- `src/spark/copytrade/config.py`：`mirror_triggers: bool = False`；`from_env` 加
  `mirror_triggers=_env_bool("COPY_MIRROR_TRIGGERS", str(cls.mirror_triggers).lower(), env)`（比照 `live_trading` 寫法）。
- `src/spark/copytrade/orders.py`：
  - `SkippedOrder` 加 `oid: int | None = None`；reason 新增 `"trigger_disabled"`、`"trigger_unsupported"`。
  - `_build_desired(..., mirror_triggers: bool = False)`：緊接現貨過濾之後——
    `if o.is_trigger and not mirror_triggers:` → `SkippedOrder(coin, Decimal("0"), "trigger_disabled", oid=o.oid)`；`continue`。
  - `ReconcileState` 加 `trigger_skip_seen: dict[str, frozenset[int]] = field(default_factory=dict)`（coin → 上次告警時的 leader 觸發單 oid 集合）。
  - `sync_open_orders`：把 `settings.mirror_triggers` 傳進 `_build_desired`；對 `trigger_disabled`／`trigger_unsupported`
    依 coin 聚合 oid 集合，與 `state.trigger_skip_seen[coin]` 不同才 `notifier.warn("orders", …)` 並更新；
    本輪沒有被略過觸發單的 coin 從 dict 移除。文案：
    - disabled：`[未鏡射] leader 在 {coin} 有 {n} 張止盈／止損觸發單，本帳戶未跟單（觸發單鏡射未啟用）。leader 平倉後由部位同步跟進，最多延遲一輪。`
    - unsupported：`[未鏡射] leader 在 {coin} 有 {n} 張型態無法判讀的觸發單，已略過。`

**測試**（新增 `tests/test_copy_orders_triggers.py`；adapter 讀取加在 `tests/test_hyperliquid_reads.py`）：
1. 倉位止損（`sz=0`、`is_position_tpsl=True`）＋開關關 → desired 為空、skipped 有一筆 `trigger_disabled` 且帶 oid。
2. 固定數量止損限價（`sz=0.015`）＋開關關 → 同上；經 `sync_open_orders`（live、fake executor、`RecordingNotifier`）
   → `ex.place_outcome` 呼叫 0 次、`critical` 0 則、`warn` 恰 1 則。
3. 同一組 oid 連跑第二輪 → 不再 warn；oid 集合改變 → 再 warn 一次；觸發單消失 → `trigger_skip_seen` 清空。
4. `get_open_orders` 解析 `isPositionTpsl:true`／缺鍵 → `True`／`False`。
5. `COPY_MIRROR_TRIGGERS` 缺省 `False`、`"true"` → `True`。

**既有測試預期要改**：`tests/test_copy_loop.py::test_leader_trigger_order_surfaces_as_skip_trigger_warn`
（開關關閉時改由新路徑告警）。其餘 executor 層測試在本 task 不應變動。

### T3 `@inline` — 交易所寫入層支援觸發單（依賴 T1 的 D-P*）

**檔案與簽名**：
- `src/spark/exchange/base.py` `Order` 尾端加（皆有預設值，既有建構點不壞）：
  `trigger_px: Decimal | None = None`、`tpsl: str | None = None`、`is_market: bool = False`、`position_tpsl: bool = False`。
- `src/spark/exchange/hyperliquid.py`：
  - `place_order`：`order.trigger_px is None` → 現行行為完全不變。否則——
    `tpsl not in ("tp","sl")` → `raise ValueError`（D3，fail-loud，不猜）；
    `position_tpsl=True` 且 `not order.reduce_only` → `raise ValueError`（倉位止盈止損必為 reduce-only）；
    `reduce_only` 原樣傳遞（條件進場單為 `False`）；
    `order_type = {"trigger": {"triggerPx": self._round_px(coin, trigger_px), "isMarket": order.is_market, "tpsl": order.tpsl}}`；
    `position_tpsl=False` → `self._exchange.order(...)`（帶 `builder`）；
    `position_tpsl=True` → `self._exchange.bulk_orders([req], {"b":…, "f":…}, grouping="positionTpsl")`，
    request 的 `sz` 固定送 `0.0`（D-P3，不看 `order.size`）。
  - `_parse_order_response`（D-P1）：`statuses[0] == "waitingForTrigger"` → `ok=True`、`filled_size=0`、`avg_px=0`；
    其他字串（含 `"waitingForFill"`）或不認得的形狀 → `ok=False` 並保留 `raw`，**不得拋 AttributeError**
    （現行程式對字串會在 `status.get` 拋例外）。`{"resting": …}`／`{"filled": …}`／`{"error": …}` 行為不變。
  - `get_open_orders`（D-P12）：先掃一遍 `raw`，收集所有 `o.get("children") or []` 內的 oid；最上層單的 oid 在該集合內者
    `is_pending_child=True`。`children` 缺鍵或為空 → 集合為空。
- `src/spark/exchange/base.py` `OpenOrder` 尾端另加 `is_pending_child: bool = False`。
  - `_round_px` docstring 中「日後若加 trigger」那段改為現況。
- `src/spark/resilience.py`：新增顯式包裝 `bulk_orders(self, order_requests, builder=None, grouping="na")`——
  全部 request 的 `reduce_only` 為真 → `idempotent=True` 直接重試；否則單次嘗試（與 `order` 同分類）。
- `src/spark/exchange/fakes.py` `FakeAdapter.place_order`：`order.trigger_px is not None` → 回
  `OrderResult(ok=True, filled_size=0, avg_px=0, raw=…)`，不累計 builder fee（未成交）。

**測試**（新增 `tests/test_hyperliquid_trigger_writes.py`，以假 SDK exchange 斷言呼叫參數）：
1. 止損限價 → `order` 收到 trigger `order_type`、`reduce_only=True`、`builder` 兩鍵齊全；`triggerPx` 經 `_round_px`
   （BTC szDecimals=5 → 小數上限 1 位：`61000.04` → `61000.0`）。
2. 市價型 → `isMarket=True`，`limit_px` 照傳。
3. 倉位止盈止損 → 走 `bulk_orders`，`grouping="positionTpsl"`、帶 builder、數量符合 D-P3。
4. `tpsl=None` 的 trigger、或非 reduce-only 的倉位型 → `ValueError`，SDK 呼叫 0 次。
   非 reduce-only 的固定數量 trigger（條件進場）→ `order` 收到 `reduce_only=False`、帶 builder；
   經 `ResilientExchange.order` 遇 `ConnectionError` **只呼叫一次**（非冪等，不盲重試）。
5. 回應解析（用 research 檔 §4 的原始回應當測試資料）：`[{"resting": {"oid": 65739864215}}]` → `ok=True`；
   `["waitingForTrigger"]` → `ok=True`、`filled_size == 0`；`["waitingForFill"]`、`["somethingNew"]` → `ok=False` 且不拋例外；
   `[{"error": "x"}]` → `ok=False`。
8. `get_open_orders`：以 research 檔 §4「P12 母單」的原始 JSON（母單＋兩張同 oid 的最上層子單）為輸入 →
   兩張子單 `is_pending_child=True`、母單 `False`；沒有 `children` 鍵的一般回應 → 全部 `False`。
9. 倉位型下單：`Order.size` 給非 0 值時，送進 SDK 的 `sz` 仍是 `0.0`。
6. `ResilientExchange.bulk_orders`：全 reduce-only 遇 `ConnectionError` 會重試；含非 reduce-only 只呼叫一次。
7. `tests/test_base_types.py` 的結構性斷言仍通過。

### T4 `@inline` — 引擎鏡射觸發單（依賴 T2、T3）

**檔案與簽名**：
- `src/spark/copytrade/orders.py`：
  - `OrderSpec` 尾端加 `is_position_tpsl: bool = False`；`spec_from_open_order` 與 `_build_desired._spec_of` 直通。
  - `_orders_match`：`is_trigger` 檢查之後加 `if desired.is_position_tpsl != mine.is_position_tpsl: return False`；
    數量比較之前加 `if desired.is_position_tpsl: return True`（兩邊都是倉位止盈止損，數量恆 0，不比）。
  - `_build_desired`（開關開啟時；過濾順序寫進 docstring）：
    1. 現貨 → `spot`（不變）。
    1.5 `o.is_pending_child` → `SkippedOrder(coin, Decimal("0"), "trigger_pending_child", oid=o.oid)`；`continue`（D-P12；
        不論開關、不發 warn——`sync_open_orders` 的告警聚合不得把這個 reason 算進去）。
    2. `is_trigger` 且開關關 → `trigger_disabled`（T2）。
    3. `is_trigger` 且 `tpsl not in ("tp","sl")` → `trigger_unsupported`（帶 oid）。
       （母單未完成的附帶子單已在 1.5 略過。）
    4. 成本熔斷、抗單保護、無倉位（不變；reduce-only 一律放行）。條件進場單在這一步
       與非 reduce-only 限價單同待遇：熔斷→`cost_breaker`、保護中→`protected`（D13）。
    5. reduce-only 觸發單方向不符 → `reduce_only_no_pos`（D9；與非 trigger 同一判式）。
    6. `is_position_tpsl` → `trigger_px > 0` 才 `entries.append(_spec_of(o, Decimal("0")))`，**跳過**數量與最小名目檢查；`continue`。
    7. 固定數量觸發單：`size = _round_size(o.sz * scale)`；`size <= 0` → `SkippedOrder(coin, Decimal("0"), "small")`（不再靜默）；
       名目 < `min_notional` → **只有非 reduce-only（條件進場單）**記 `small`；reduce-only 觸發單豁免最小名目（D-P11）；
       不進 `ro_pending`、不封頂（D7）。
  - `_reconcile_orders` 改單迴圈最前面加：`if spec.is_trigger: fallback.append((oid, coin, spec)); continue`
    （D4；不呼叫 `ex.modify`、不寫 `state.modify_fail_until`）。
  - `_fmt_missing`／`_fmt_extra`：倉位止盈止損顯示「倉位TP/SL」而非數量 0，CRIT 才讀得懂。
- `src/spark/copytrade/executor.py`：
  - `_order_from`：帶上 `trigger_px=spec.trigger_px if spec.is_trigger else None`、`tpsl`、`is_market`、`position_tpsl=spec.is_position_tpsl`。
  - `place_outcome`：觸發單改為正常下單（payload 另記 `trigger_px`／`tpsl`／`is_market`／`is_position_tpsl`）。
    防線保留：`is_trigger` 且 tpsl 非 tp／sl 仍走 `_skip_trigger`＋`rejected`。
  - `modify`：trigger 分支維持回 `False`（正常流程已不會走到）。
  - `_order_from_spec`（VirtualBook）：直通 `is_position_tpsl`。
  - 檔頭「M1 已知限制」docstring 改寫為現況。
- `src/spark/copytrade/loop.py:440-444`：文案改為「出現不支援的 trigger 單型態，已跳過」。

**測試**（`tests/test_copy_orders_triggers.py` 續寫；executor 部分加在 `tests/test_copy_executor.py`）：
1. 空倉＋leader 倉位止損（買）＋開關開 → desired 一筆：`sz=0`、`is_position_tpsl=True`、`tpsl="sl"`、觸發價與限價照抄。
2. reduce-only 觸發單：無倉位 → `reduce_only_no_pos`；多倉配買單 → `reduce_only_no_pos`。`tpsl=None` 的 trigger → `trigger_unsupported`。
3. 固定數量止損限價：`scale=Decimal("26.91")`、leader `0.015`、szDecimals 5 → `sz == _round_size(Decimal("0.015")*scale, 5)`，
   `limit_px == trigger_px == Decimal("61000")`、`is_market=False`；數量未被倉位封頂（倉位小於該數量時仍是全額）。
4. `_orders_match`：倉位對倉位（數量皆 0）→ True；倉位對固定數量 → False；固定數量在 `size_tol` 內 → True。
5. 兩輪收斂（dry、VirtualBook）：第一輪 place 觸發單與同幣 reduce-only 限價單；第二輪零動作、全 matched。倉位型與固定數量型各一案。
6. live、fake executor：leader 改觸發價 → 舊單 `cancel` 1 次＋新單 `place_outcome` 1 次、`ex.modify` 0 次、`state.modify_fail_until == {}`。
7. live settle：新掛的觸發單出現在重抓結果 → `critical` 0 則。觸發單被交易所拒 → `critical` 1 則且含拒因（D10）。
8. leader 撤掉觸發單 → follower 的對應觸發單被撤。
9. 倉位止損改成固定數量止損（本次事故的實際操作順序）→ 舊倉位單被撤、新固定數量單被掛。
10. executor live：`adapter.place_order` 收到的 `Order` 帶齊 trigger 欄位與 builder；dry：VirtualBook 內有該單。
11. 條件進場單（非 reduce-only 止損市價買，無倉位）＋開關開 → desired 一筆：`reduce_only=False`、數量＝leader×倍率、
    觸發價與限價照抄；經 `sync_open_orders` 該幣槓桿先被設定一次（`_set_entry_leverage`）。
12. 條件進場單在 `no_new_exposure=True` → `cost_breaker` 略過；幣在 `protected` → `protected` 略過；
    名目 < `min_notional` → `small`。同一輪的 reduce-only 觸發單不受這三者影響。
13. 已掛著的條件進場單，下一輪 `no_new_exposure` 變 `True` → 該單被撤（desired 不再包含它）。
14. `is_pending_child=True` 的 reduce-only 觸發單（follower 有同方向倉位、開關開）→ 不進 desired、skipped 為
    `trigger_pending_child`、`warn` 0 則；同一張單 `is_pending_child=False` 時 → 進 desired（D-P12）。
15. reduce-only 固定數量觸發單名目 $4.8（< `min_notional`）→ 進 desired；同名目的條件進場單 → `small`（D-P11）。
16. follower 已有兩張相同觸發價的倉位止損、leader 只有一張 → 一張 matched、另一張被撤（D-P5 的去重）。

**變異驗收（judgment.md §2）**：回報須附「刻意改壞 → 哪個測試轉紅」至少 4 條：
(a) 拿掉 `_orders_match` 的倉位型提前回傳 → 測試 5；(b) 拿掉 `_reconcile_orders` 的 trigger→fallback → 測試 6；
(c) 拿掉方向檢查 → 測試 2；(d) 讓條件進場單繞過成本熔斷過濾 → 測試 12。改壞後要還原並重跑全綠。

**既有測試預期要改**（逐條回報）：`test_copy_executor.py::test_place_trigger_skips_and_records_in_both_modes`、
`::test_modify_trigger_skips_and_records`、`test_copy_executor_place_outcome.py::test_trigger_is_rejected_with_existing_reason`、
`test_copy_orders_crit_alert.py::test_place_with_reason_trigger_skip_gives_reason`、
`test_copy_orders_settle_no_dup.py::test_trigger_rejection_stays_loud`、
`test_copy_orders_ro_cap.py::test_trigger_ro_not_capped_and_consumes_no_capacity`（預期仍通過，確認即可）。

### T5 `@inline` — testnet 端到端（主線程親跑）

**檔案**（2026-11-22 主線程裁決，取代原「擴充 `testnet_leader_drive.py` 子命令」的寫法——九個情境要交錯三十多個
leader 動作與引擎輪次，手動逐條下指令不可重現）：新增編排腳本 `scripts/testnet_trigger_e2e.py`；結果寫進 T1 的 research 檔新章節。

**做法**：
- 輸入：`--leader-pk-file`、`--follower-pk-file`（兩個拋棄式 testnet 錢包的私鑰檔，路徑必須含 `scratchpad`，
  由主線程先用 `tests/integration/harness.py mint-wallet` 建好並完成 builder fee 核准）、`--builder`、`--out`（JSON lines，不得在 repo 內）、
  `--only E1,E3`、`--coin`（預設 BTC）、`--notional`（leader 倉位名目，預設 60，上限 100）。
- 僅限 testnet：沿用 `scripts/testnet_leader_drive.py` 的 `_assert_testnet`（建構任何簽章者前與每次寫入前）；私鑰不進任何輸出。
- leader 的下單一律走產品程式 `HyperliquidAdapter.place_order`（T3 的觸發單路徑）與 `cancel_order`——重用才是在測產品。
- follower 引擎一律以子行程跑 `scripts.run_copytrade --once`（參考 `tests/integration/harness.py` 的 `run_engine_once` 與
  `tests/integration/test_e2e_noncustodial.py` 的 env 組法），`COPY_LIVE_TRADING=true`、`COPY_MIRROR_TRIGGERS` 依情境、
  強制 `SPARK_NETWORK=testnet`；狀態目錄與 keys 目錄放在 `--out` 同層的暫存目錄。告警不得真的送出（不設 TG 變數）。
- 每個情境：執行 leader 動作 → 跑 N 輪引擎 → 讀兩邊 `frontendOpenOrders` 與倉位 → 逐條判定預期（PASS／FAIL＋差異）→ 寫一行記錄
  （含引擎子行程的 stdout／stderr 末段、兩邊原始掛單 dict）。情境失敗不中止後續情境。
- E6／E9 要讓觸發單真的觸發：把觸發價設在離現價很近的位置（例如 0.03%），等兩邊都鏡射好後輪詢；最多等 10 分鐘，
  期間每 2 分鐘把觸發價往現價再靠近一次（leader 撤單重掛 → 跑一輪引擎）。逾時未觸發就如實記錄「未觸發」，不得判 PASS。
- 整段 `try/finally` 清理：兩個錢包撤掉該幣全部掛單、市價平倉，最後驗證兩邊「無倉位、無掛單」。
- **有測到東西才算數**（T1 探針的教訓）：結束碼 0 的條件是「所有被選情境皆 PASS 且每個情境至少有一筆成功的 leader 下單」；
  任何 FAIL、leader 寫入全失敗、或清理後不乾淨 → 非 0。

**情境**（follower 引擎在 testnet、`COPY_MIRROR_TRIGGERS=true`）：

| # | leader 動作 | 預期 follower（一輪內） |
|---|---|---|
| E1 | 開倉後掛倉位止損＋止盈 | 兩張倉位型觸發單，`isPositionTpsl:true` |
| E2 | 改止損觸發價 | 舊單消失、新單出現（oid 不同），無 CRIT |
| E3 | 撤掉倉位型、改掛固定數量止損限價 | 倉位型消失、固定數量單出現，數量≈leader×倍率 |
| E4 | 連續 3 輪不動 | 零下單、零撤單（不 churn） |
| E5 | 撤掉全部觸發單 | follower 觸發單全撤 |
| E6 | 把止損觸發價設到現價附近讓它觸發 | 兩邊皆平倉；之後無殘留觸發單、無 CRIT |
| E7 | 引擎以 `COPY_MIRROR_TRIGGERS=false` 重跑 E1 | 不掛單、warn 恰一則、無 CRIT |
| E8 | 無倉位時掛條件進場單（止損市價買，觸發價高於現價）；3 輪後撤單 | 同型單出現（非 reduce-only、數量≈leader×倍率）；3 輪零動作；leader 撤後 follower 撤 |
| E9 | 把 E8 的觸發價設到現價附近讓它觸發 | 兩邊都開倉，follower 倉位≈leader×倍率（超出容忍帶的差由安全網收斂），**沒有重複開倉**、無 CRIT |

**驗收**：(1) builder：`--help` 可執行且不連網、不讀私鑰檔；`uv run ruff check scripts` 0 錯；`uv run pytest -q` 全綠；**不實跑**。
(2) 主線程：建錢包 → 實跑 → research 檔新增 E1–E9 的結果表（時間戳、兩邊 oid、`frontendOpenOrders` 摘錄、PASS／FAIL）→ 掃回水龍頭並刪鑰。

### T6 — 審核（`reviewer`）

輸入只有 `git diff main...feat/trigger-order-mirroring`、本 plan、測試輸出。重點依序：
1. 觸發單任何路徑是否可能不帶 builder（紅線 4）；條件進場單是否有任何路徑繞過成本熔斷／抗單保護，或被盲重試而重複掛單；倉位型是否可能以非 reduce-only 送出。
2. `_orders_match`／`_plan`／`_settle_verify` 對數量 0 的倉位型是否會每輪重掛或誤發 CRIT。
3. 開關關閉時是否有任何路徑仍會對觸發單下單。
4. `ResilientExchange.bulk_orders` 的冪等分類；`_parse_order_response` 對未知形狀是否 fail-closed。
5. 工程原則 #2／#3／#5 逐條對照。
修到 0 Critical／0 Warning 為止；只有設計分岔才停下問使用者。

## 7. 殘留風險（上線前要讓使用者知道）

- **R1 止損限價可能不成交**：觸發後是限價單，follower 數量是 leader 的數十倍，成交程度可能不同；分歧由部位安全網在下一輪市價收斂。
- **R2 撤單重掛的空窗**：leader 改觸發價時，follower 有數百毫秒到數秒沒有該張止損。
- **R3 引擎停機時的過期止損**：follower 的觸發單留在交易所（這是本功能的好處），但 leader 之後改／撤，follower 不會跟，直到引擎恢復。
- **R4 倍率漂移**：倍率變動超過 `size_tolerance`（8%）時固定數量觸發單會撤單重掛。
- **R5 已觸發未成交的止損限價**在 `frontendOpenOrders` 的呈現未驗證，可能造成一輪重掛。E6 觀察並記錄。
- **R6 同一張 leader 觸發單，三個 follower 會在同一個 mark price 同時觸發**，市價型會同時吃單。
- **R9 附帶止盈止損的空窗**：leader 的母單部分成交且仍掛著時，子單還沒生效（文件所述，testnet 未測）；follower 鏡射的母單
  若也部分成交，這段期間 follower 有倉位但沒有對應的止盈止損，直到 leader 的母單完成或被撤。
- **R10 testnet 未涵蓋的行為**：觸發條件已成立的單多久後觸發、小額觸發單觸發後能否成交、unifiedAccount 下的行為（research §5）。
- **R7 條件進場單會在引擎不在場時開倉**：觸發發生在交易所端，不經過引擎。引擎停機、或成本熔斷剛成立但還沒輪到撤單的那一輪內，
  follower 仍可能被觸發開新倉。風控熔斷與 kill switch 成立後的那一輪會把它撤掉（既有「撤全部掛單」路徑）。
- **R8 條件進場單觸發瞬間的一輪落差**：follower 的觸發單與 leader 的幾乎同時成交，但引擎該輪若用到觸發前的快照，
  可能多做或少做一次調整，下一輪收斂。這與現行限價進場單成交時的落差同類（2026-10-07 回顧過、修法未採用），本 plan 不解決。E9 專門觀察。

## 8. 驗收總表

| 項目 | 證據 |
|---|---|
| 離線回歸 | `uv run pytest -q` 全綠；`uv run ruff check src tests scripts` 0 錯（主線程親跑） |
| 變異驗收 | T4 回報的 4 條變異，主線程抽 2 條親自重現 |
| testnet 探針 | research 檔 P1–P13 |
| testnet 端到端 | research 檔 E1–E9 |
| 審核 | reviewer 報告 0 Critical／0 Warning |

## 9. 上線步驟（使用者 2026-11-21／22 已授權：確認沒問題後部署、合併 push、更新文件；D15）

1. **T2 告警先行**：已於 2026-11-21 17:53Z 部署 `eb4a677`（RUNBOOK 附錄 B）。
2. **T3＋T4**（開關預設關）：T5 重跑通過、T6 審核 0 Critical／0 Warning 後，合併 push、部署、滾動重啟 follower、回歸檢查。
3. **只對 `f438b3…` 開**：其 env 加 `COPY_MIRROR_TRIGGERS=true`（先備份 env 檔）、重啟該 follower、以進程 environ 驗證吃到新值、確認心跳持續推進且 journal 無錯誤。
4. **T7 預設改開**：`CopySettings.mirror_triggers` 預設 `True`；測試中依賴「預設關」的地方改為顯式傳 `mirror_triggers=False`；
   `deploy/` 下的 follower env 範本與 RUNBOOK 說明同步；審核後部署，三個 follower 滾動重啟。
5. 寫 RUNBOOK 附錄 B、更新 plan 進度與 memory。
6. 晨間回報：各步驟結果、開關現況（逐帳戶以進程 environ 與程式預設值說明）、尚無端到端證據的項目（leader 下次掛觸發單才看得到主網實況）。

## 10. 待使用者確認

使用者 2026-11-21 全數確認，無待確認項：

1. D1、D2：兩種減倉觸發單都做；倉位型在 follower 端掛成同型單。
2. D3（改判）：條件進場單也要跟。
3. D5：開關預設關；關閉時固定數量觸發單的告警從 CRIT 降為 warn。
4. D13：條件進場單套用與現行限價進場單相同的數量、熔斷、保護、槓桿規則。
5. D14：掛單附帶的止盈止損子單在母單成交前不鏡射。
6. R7：條件進場單會在引擎不在場時於交易所端觸發開倉，接受。
7. T2 先獨立部署（部署本身仍另行授權，§9）；T1／T5 在 testnet 下單，主網零寫入。
