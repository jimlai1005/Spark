> **狀態（2026-10-12）：未採用。** 第一版修法（分支 `archive/2026-10-fresh-leader-read-v1`，57 commits、7 輪複審）由使用者整體放棄；程式碼從未合併或部署。留底供回顧。

# Plan：安全網改用新鮮 leader 讀值、掛單對帳不重下已消耗的單、HL 5xx 韌性

> 狀態：使用者 2026-10-09 確認（§10）。**進度：T0–T6 完成並本地 commit（2e7edaf）；T7 起依序派工中。**
> 2026-10-09 Fable 複審後補強：T7 中性化 helper 與呼叫計數方式、T8 時間戳欄位與 warn-once 狀態、
> T9 consumed 語意、§8 面板顯示註記（見各段「複審補強」）。
> 背景與證據全文：`docs/superpowers/research/2026-10-09-crash-0207-stale-snapshot-walkthrough.md`
> （以下簡稱「回顧文件」）。決策已與使用者逐項取得共識（回顧文件 §7）。
> 分支：`fix/fresh-leader-read-order-resilience`（自 main 切出；逐 task commit、**不 push**）。

## 1. 目標

修兩則 2026-10-07 CRIT 的根因，不改風險政策：

1. **A1**：部位安全網（B 段）不再拿「一輪開頭的過期 leader 部位與 scale」去比「新鮮的自己部位」。
2. **W2＋A2**：掛單對帳（A 段）對「已成交／已撤」的單不再退回重掛；post-only 單被判會吃單時視為
   市場已越過，延到下一輪，不發 CRIT。
3. **B1**：`modify` 遇到例外（含 HTTP 500）只標記該單為「結果不明」，不中止整輪、不盲目重掛。
4. **B2**：5 連敗退出的 CRIT 文案如實說明「已自動重啟」。
5. **附帶**：每輪寫 `cycle_trace-YYYYMMDD.jsonl`（解開回顧文件 §6 的兩個未解項）。

## 2. 不做（範圍外，請勿順手改）

- **W4**：波動權重／leader 有效槓桿造成的政策性降倉。V1 與 W2 不減少它（回顧文件 §2 末表）。
- **V2**：「重抓與快照差距過大就延後減倉」。先蒐集 cycle_trace 再決定。
- `size_tolerance`（0.08）、`settle_seconds`、`_settle_verify` 的配對語意、波動權重公式、資金使用率。
- `place_outcome`／`cancel` 本身拋例外時仍中止整輪（只有 `modify` 納入 B1）；記為殘留風險（§8）。
- 部署與 memory 紀錄：部署後另行徵詢使用者授權並一起寫。
- 撤單延遲造成的「多持一格」（leader 撤單後 follower 晚一輪才撤）。

## 3. 裁決表（builder 遇歧義時以此為準；plan 沒涵蓋的決策先回報主線程）

| # | 裁決 |
|---|---|
| D1 | `modify` 拋例外 ⇒ **不退回 cancel+place**（結果不明：可能已落地並換 oid，重掛會重複，同 2c5e591 類別）。只做：登記該 coin 的 `modify_fail_until` TTL、把該 oid 加進 `touched_oids`、計數、`notifier.warn`（去重）。下一輪依最新掛單重算。 |
| D2 | HTTP 500／502／503／504 一律視為暫時性錯誤，以 `status_code` 屬性判斷（`ServerError.status_code`），**不用字串比對 "500"**（會誤判含 500 的語意錯誤）。只影響本來就會重試的冪等寫入（cancel、reduce-only、market_close…）；`modify_order` 與非冪等 `order` 仍單次嘗試。 |
| D3 | 「已消耗」由 HL 拒因字串判定，**純函式**放 `src/spark/exchange/hl_reasons.py`；字串不認得 ⇒ 保持現行行為（不當作已消耗）。 |
| D4 | 新鮮 scale ＝ 新鮮 leader `account_value`／`total_ntl_pos` ＋ 新鮮自己 `account_value`，**波動權重沿用開頭那份**（portfolio 權重 20，不重抓）。vault leader 開啟流量中性化時，新鮮 `leader_equity` 用開頭取得的 `flows` 重算 `adjusted_leader_equity`。 |
| D5 | 可疑讀值規則見 T4 真值表。重讀間隔／警告／critical 門檻為 `CopySettings` 欄位（預設 2 秒／3 次／10 次），**env 不需新增任何鍵**。 |
| D6 | 每輪開頭的 leader 讀取套同一套規則（使用者 2026-10-09 同意）。開頭被跳過 ⇒ 該輪**整輪**不做對帳也不做校正，回報 `skipped_reason`，**不計入連續錯誤**。 |
| D7 | trace 寫入失敗**不得**影響交易，但要可見：計數器＋每個進程最多一則 `notifier.warn`。trace 單日檔有硬上限（16 MB），超過後只丟「明細」行，保留「彙總」行。 |
| D8 | `ExecutorPort` Protocol **不加方法**（避免十幾個測試替身全部壞掉）；`orders.py` 以 `getattr(ex, "modify_outcome", None)` 取新介面，缺則退回舊的 bool 介面（kind 推為 `"ok"`／`"rejected"`、reason 空）。 |
| D9 | `ExchangeAdapter` 新增方法一律給**非抽象的預設實作**（串既有方法），`FakeAdapter` 與所有測試替身不用改；只有 `HyperliquidAdapter` 覆寫成單一 HTTP。 |
| D10 | `run_cycle` 新增 keyword-only 參數 `sleep_fn: Callable[[float], None] = time.sleep`（守衛重讀等待用；測試注入假睡眠）。既有呼叫點不用改。 |
| D11（2026-10-09 審查 W1，使用者選 B） | 部位安全網**增加曝險**的動作（新開倉 `open`、同向加倉 `increase`）先扣掉我方該幣**同方向、非 reduce-only、非 trigger** 的未成交掛單量（A 段結束後在 `_safety_net` 內重讀一次 `ex.get_open_orders()`）；扣完 ≤0 ⇒ 跳過（branch `covered_by_resting_orders`），>0 ⇒ 只補淨量（min_notional gate 套在淨量上）。**減倉／flip／flatten 不扣**：延後去風險是 fail-open 方向，且 HL 會自動把 reduce-only 掛單修剪到不超過部位，不會重複。 |
| D13（2026-10-09 複審 Critical 1，使用者選 B；**取代 D11 的扣法**） | 安全網增加曝險的動作只扣「**真正落後的鏡射單**」：B 段多讀一次 leader 的新鮮掛單（`adapter.get_open_orders(leader)`，權重 20），我方同向、非 reduce-only、非 trigger 的掛單中，**在 leader 新鮮掛單簿上找不到同幣、同方向、同價位（`px_rel_tol`）對應單**的才扣；有對應單的是正常網格／階梯層，不扣。dry 路徑用開頭的 `my_orders` 與 `leader_orders`（同一份快照 ⇒ 全部有對應 ⇒ 扣 0，行為與 T13 前相同）。理由：D11 把「leader 單已成交、我方鏡射落後」與「leader 單也還掛著」混為一談，對階梯掛單型 leader（Filet Alpha 即是）會整個部位長期不跟且無聲。 |
| D14（複審 W-A） | B 段讀取順序固定為：**我方掛單 → 我方快照 → leader 快照（守衛）→ leader 掛單**。鏡射單若在「讀掛單」與「讀部位」之間成交，會被同時算進 resting 與部位 ⇒ 少跟一輪（fail-safe）；反過來先讀部位會多補一次（fail-open，W1 殘留）。 |
| D15（複審 W-B） | `positions._try_close_reduce_only` 接住 `ex.close_reduce_only` 的**任何例外**：記 `result["failed"]`（action `close_reduce_only`、附 `exc` repr）、`logger.warning`、`notifier.warn("execution", …, dedup_key=f"close_exc:{coin}")`，回傳 False，**不**上拋——讓同輪其餘幣的校正與「leader 已平的跟平」照常執行，下一輪重試。T14 讓部分減倉不重試後，第一次暫時性錯誤就會拋，不接住等於整個 B 段（含去風險）延後一輪並計入連敗。kill switch flatten 走自己的 `resilient_run`，不受影響。 |
| D16（複審 3 W1，使用者選 A；**細化 D13**） | 對應判定改為**依量**：對每個 leader 有部位的幣，把我方同向、非 ro、非 trigger 的掛單依價位對到 leader 同幣同方向非 trigger 掛單的價位（`px_rel_tol`）；每個 leader 價位 L：`lag_L = max(0, Σ我方該價位量 − scale_fresh × Σleader 該價位剩餘量)`；對不到任何 leader 價位的我方掛單全量算落後。`落後量 = Σ lag_L ＋ 無對應者`。lag 與 target 同用 `scale_fresh`，以 fresh 目標為基準是精確的（複審 4 更正：原「scale 誤差落在少跟一側」不精確；殘留偏差來自 size_tolerance，見 D19）。理由：D13 只看「價位有沒有單」，leader 單部分成交或同價多張時漏扣，W1 以縮小形狀重現（上限＝leader 已成交量×scale）。 |
| D17（複審 3 S2） | D15 接住減倉例外後要有升級路徑：`ReconcileState.close_exc_streak: dict[str, int]`（coin → 連續有 `close_reduce_only` 例外的輪數），由 `loop.py` 在 `_safety_net` 回傳後依 `result["failed"]` 中帶 `exc` 的幣更新（有 ⇒ +1，無 ⇒ 歸 0）；`>= settings.close_exc_crit_after`（新欄位，預設 **5**，env `COPY_CLOSE_EXC_CRIT_AFTER`）⇒ `notifier.critical("execution", …, dedup_key=f"close_exc_crit:{coin}")`。 |
| D18（複審 3 S1） | dry 路徵的 resting 來源改為 **A 段之後的虛擬簿** `ex.get_open_orders()`（dry 下零 HTTP）＋開頭的 `leader_orders`，與 live 同一條程式路徑（差別只在 leader 掛單不重讀）。原「用開頭 `my_orders`」的前提不成立：dry 虛擬簿是上一輪建的，leader 在兩輪間成交／撤單後殘留虛擬單會被誤判為落後。 |
| D19（複審 4 W1，主線程裁決） | 落後量計算套用 A 段同一個 `size_tolerance`：每群 `lag = max(0, Σ我方量 − scale × Σleader 剩餘量 × (1 + size_tolerance))`。理由：A 段把「比期望量大不到 8%」的鏡射單視為正確、不改單，B 段卻用精確值把這 8% 算成落後，兩段對「鏡射單量對不對」用兩套標準（工程原則 #1 變形），對階梯型 leader 造成每輪少補約 8%×整個階梯量且無聲。代價：leader 某層部分成交不到 8% 時不會被偵測（≤ 8%×該層×scale 的多買，下一輪自修），遠小於它消除的系統性少跟。 |
| D20（複審 5 W1，使用者選 A） | 落後量允許量改為與 A 段 `_orders_match` 等價：每群 `lag = max(0, Σ我方量 − scale × Σleader 剩餘量 ÷ (1 − size_tolerance))`（A 段判 `|d−m|/m ≤ tol` ⇒ 放行到 m ≤ d/(1−tol) ≈ 1.087d；D19 的 `×(1+tol)` 只到 1.08d，1.08–1.087 區間每輪少補 ≤0.7%×階梯量）。D19 其餘不變。 |
| D21（複審 5 W2，使用者選 A；main 既有缺口） | flip（方向反轉）的**開倉腿**也套用 open 分支同一套扣法：`open_size = target_size − resting`；≤0 ⇒ 平倉腿照做、開倉腿跳過（`skipped` reason／decision branch `covered_by_resting_orders`，不記 adjusted）；>0 ⇒ 套 `_adjust_gate` 後以淨量開倉。平倉腿**不變**（D11 的 fail-open 理由只適用平倉腿）。理由：leader 靠限價單翻向、我方同價鏡射單未成交時，B 段市價開到目標而鏡射單仍掛著，成交後可達目標 3 倍。 |
| D22（複審 6，使用者 2026-10-10 選 C：**撤回「扣落後鏡射單」整段**） | 移除 D11／D13／D16／D18／D19／D20／D21 的全部實作與測試（`resting_entry_qty_by_coin`、`sync_positions` 的 `resting_entry_qty` 參數與 open／increase／flip 扣法、`_safety_net` 內為此新增的我方掛單與 leader 掛單重讀、回傳 dict 的 `resting_entry_qty`）。**保留**：D12（部分減倉不重試）、D15（減倉例外不中止 B 段）、D17（連續例外升 critical）、`size_tolerance ∈ [0,1)` 驗證（保護 A 段）、T8 的 `decisions`。D14 的讀取順序因不再讀我方掛單而失去對象，B 段回到 T7 的「自己快照 → leader 守衛快照」。原 W1（leader 單已成交、我方同價鏡射單落後 ⇒ B 段市價補齊、鏡射單再成交 ⇒ 多一輪 taker 來回、上限＝鏡射單量、下一輪自修）**接受為殘留風險寫進 §8**。理由：D11 起每一輪修法都帶出下一個問題（價位→量→容差→基準→格點），核心修法 A1／W2／B1／B2／trace 自第三輪起已穩定；此段邏輯的複雜度與帶來的收益不成比例，先撤回、重新診斷，再另案規劃。 |
| D12（2026-10-09 審查 W2，使用者選 B） | 帶明確數量的**部分**減倉 reduce-only IOC 不冪等 ⇒ 暫時性錯誤**不重試**（單次嘗試、失敗交下一輪）；**全平**（size＝整個部位；flip 的平倉腿、leader 已平的跟平、kill switch flatten）天然冪等 ⇒ 維持重試。由呼叫端以 `full_close: bool` 宣告，一路傳到 `ResilientExchange.order(..., _idempotent=...)`。HTTP 500 仍留在暫時性錯誤集合（D2 不變）。 |

### 3.1 「讀到 0／可疑讀值」與先前建議的對照（2026-10-09 使用者要求核對）

| 先前建議 | plan 位置 | 一致性 |
|---|---|---|
| 呼叫例外／回應解析失敗 ⇒ 不可信 | T4 `read_leader_with_confirmation`（例外 ⇒ `read_failed:<ExcType>`） | 一致 |
| 有部位但 perp accountValue≤0 或總名目≤0 ⇒ 不可信 | T4 真值表第 2 列 | 一致 |
| 快照有部位、重抓卻全空 ⇒ 可疑，要再確認 | T4 真值表第 4 列（`prev_had_positions` 或 `my_has_exposure`） | 一致 |
| 可疑 ⇒ 等 2 秒再讀一次（`settle_seconds` 慣例） | T4／T5：獨立設定 `fresh_read_retry_s=2`（值相同、不與對帳耦合） | 一致 |
| 第二次仍全空且一致 ⇒ 視為 leader 真的清空照常跟平 | T4：**改為連續兩輪**重讀後仍空才跟平（使用者 2026-10-09 選定的合併版） | **刻意不同** |
| 第二次仍矛盾或失敗 ⇒ 跳過這一輪部位校正；掛單對帳不受影響；下一輪重來 | T7（安全網只略過校正，A 段已完成） | 一致 |
| 連續跳過 3 次警告、10 次 critical，去重 | T4 `end_cycle`＋T5 設定＋T6 通知（notifier 內建去重：warn 300 秒、critical 900 秒） | 一致（計數改以「輪」為單位，修掉逐讀取點計數會被洗掉的 bug） |
| 每輪開頭的讀取套同一套規則 | T6 | 一致；**開頭 skip 為整輪**（原因見 T6 第 4 點），與「掛單對帳不受影響」只適用安全網的情形不同 |
| （新增）空部位但名目>0 或權益<0 ⇒ 不可信 | T4 真值表第 3 列 | 我補的矛盾規則 |

## 4. 紅線與共通驗收

- 紅線（CLAUDE.md）：不讀或印 `.env*`；`ExchangeAdapter` 不得出現 withdraw／transfer；新增的
  寫入路徑若會產生掛單必帶 builder（本 plan **不新增**任何會建單的寫入）；測試全離線
  （autouse socket-ban）；不得動 `/Users/jim/projects/hl-copytrader`；**不部署、不重啟任何正式機服務**。
- Python：正式機是 3.11，本機 `.venv` 是 3.14。**程式碼必須 3.11 相容**（不用 3.12+ 語法）。
- 內部一律 `Decimal`；float 只在 adapter↔SDK 邊界。
- 每個 task 完成前：`uv run ruff check src tests scripts` 無新增錯誤；該 task 的測試通過
  **且貼出輸出末尾**；全套 `uv run pytest -q` 不得比基線（T0 記錄）多失敗。
- 每個 task 先寫失敗測試、親眼看到它因正確原因失敗，再實作（TDD）。
- 回報合約：(a) 結論一段話；(b) `檔案:行號` 依據；(c) 改動檔案清單＋每條驗收的指令與輸出摘要。
  **你必須親自執行全部步驟，不得使用 Agent 工具再委派。**

## 5. 任務

標記：`@inline`→`builder`（sonnet）；`@sdd`→`impl-worker`（haiku）。依序執行（T2 之後多個 task
動到同一批檔案，**不平行**）。

模型分配（2026-10-09 使用者要求「Haiku 只拿明確、不需推理的指令」）：
- Haiku（`impl-worker`）：T1、T3、T4、T5、T10。
- Sonnet（`builder`）：T2、T6、T7、T8、T9、T11、T12（需要讀懂既有架構、保持行為不變、或有整合判斷）。
- 主線程：T0、每個 task 的親跑驗收、審查派工、最終驗收。

**升級協定（所有子 agent 派工 prompt 都要原文帶上）：**
> 遇到 plan 沒寫明的判斷、規格互相矛盾、或測試失敗原因你說不清楚時：**立刻停止**，回報「卡在哪、你看到的選項、
> 你傾向哪個及理由」，不要自行裁決、不要反覆嘗試。機械性小誤（漏 import、typo）可自行補一次；補完仍失敗就停止回報。
> 主線程裁決後會把結論寫回 plan 再重派。

### T0（主線程）建分支與基線
- `git switch -c fix/fresh-leader-read-order-resilience`；`uv run pytest -q` 與
  `uv run ruff check src tests scripts` 各跑一次，把「通過／失敗／跳過數」寫進本 plan §9。
- 驗收：兩條指令輸出貼進 §9。

### T1 `@sdd` 拒因分類純函式
- 檔案：新增 `src/spark/exchange/hl_reasons.py`、`tests/test_hl_reasons.py`。
- 簽章：
  ```python
  def is_consumed_reason(reason: str) -> bool: ...        # 單已成交／已撤／從未掛上
  def is_post_only_cross_reason(reason: str) -> bool: ...  # ALO 會吃單被拒
  ```
- 規則（不分大小寫、子字串）：consumed＝含 `canceled or filled`、`already canceled, or filled`、
  `never placed` 任一；post-only＝含 `post only order would have immediately matched`。
- 測試用 2026-10-07 實際字串，至少 8 個案例：
  `"Cannot modify canceled or filled order"`→consumed；
  `"Order was never placed, already canceled, or filled. asset=1"`→consumed；
  `"Post only order would have immediately matched, bbo was 2611.4@2612.8. asset=1"`→post-only；
  `"Insufficient margin to place order"`→兩者皆 False；`""`→兩者皆 False；
  consumed 字串不得被判為 post-only，反之亦然。
- 驗收：`uv run pytest tests/test_hl_reasons.py -q` 全綠。

### T2 `@inline` 單一呼叫快照與寫入拒因
- 檔案：`src/spark/exchange/base.py`、`src/spark/exchange/hyperliquid.py`、`src/spark/exchange/fakes.py`；
  新增測試 `tests/test_hl_adapter_snapshot.py`。
- 在 `base.py` 新增（frozen dataclass）：
  ```python
  @dataclass(frozen=True)
  class UserSnapshot:
      positions: list[Position]        # szi != 0 者（同 get_positions 語意）
      state: AccountSnapshot
      fetched_at_ms: int               # 收到回應的 wall clock（int(time.time()*1000)）

  @dataclass(frozen=True)
  class WriteResult:
      ok: bool
      reason: str = ""                 # 交易所拒因原文；成功為 ""
  ```
- `ExchangeAdapter` 新增**非抽象**方法（預設實作串既有方法，D9）：
  ```python
  def get_user_snapshot(self, address: str) -> UserSnapshot: ...   # 預設：get_positions + get_account_state
  def modify_order_result(self, agent_signer, oid: int, order: Order) -> WriteResult: ...  # 預設：WriteResult(self.modify_order(...))
  def cancel_order_result(self, agent_signer, coin: str, oid: int) -> WriteResult: ...     # 預設同上
  ```
- `HyperliquidAdapter`：
  - 抽出私有 `_parse_positions(state)`／`_parse_account(state)`，`get_positions`／`get_account_state`
    改用它們（**行為逐位元不變**，現有測試必須仍綠）；`get_user_snapshot` 只呼叫一次
    `self._info.user_state(address)`。
  - `modify_order_result`／`cancel_order_result` 覆寫：回傳 HL 的 `statuses[0]["error"]` 原文為 `reason`；
    頂層 `status != "ok"` 時 reason 取 `str(res.get("response", res))`；`modify_order`／`cancel_order`
    改為委派 `.ok`（保留現有 `logger.warning` 內容）。
- `FakeAdapter`：`get_user_snapshot` 用預設實作即可；新增可設定的 `modify_reason`／`cancel_reason`
  讓測試能模擬拒因（預設 `""`）。
- 測試（全離線，用 canned `clearinghouseState` 形狀：`marginSummary{accountValue,totalMarginUsed,totalNtlPos}`、
  `withdrawable`、`assetPositions[].position{coin,szi,entryPx,leverage{type,value},unrealizedPnl,marginUsed}`）：
  單次 `user_state` 呼叫數＝1；與舊的兩個方法對同一份資料結果相等；modify／cancel 的 error 字串
  原樣進 `WriteResult.reason`；成功時 reason 為空。
- 驗收：`uv run pytest tests/test_hl_adapter_snapshot.py tests/test_fake_adapter.py -q` 全綠，
  且 `uv run pytest -q -k "adapter or hyperliquid"` 無新增失敗。

### T3 `@sdd` cycle_trace 寫入器
- 檔案：新增 `src/spark/copytrade/cycle_trace.py`、`tests/test_cycle_trace.py`。
- 簽章：
  ```python
  class CycleTraceWriter:
      def __init__(self, directory: Path, *, keep_days: int = 14,
                   daily_cap_bytes: int = 16 * 1024 * 1024, clock=time.time) -> None: ...
      def write(self, record: dict, *, essential: bool = False) -> bool: ...  # 永不拋例外
      def prune(self) -> int: ...                                              # 回傳刪除檔數
      @property
      def failures(self) -> int: ...
      @property
      def dropped(self) -> int: ...
  ```
- 行為：檔名 `cycle_trace-YYYYMMDD.jsonl`（UTC 日期，以注入的 `clock` 決定）；每筆一行 JSON
  （`ensure_ascii=False`、`default=str` 以序列化 `Decimal`）；`directory` 不存在則建立；
  每個 UTC 日第一次寫入時自動 `prune()`；單日檔已達 `daily_cap_bytes` 且 `essential=False`
  ⇒ 丟棄並 `dropped += 1`，`essential=True` 仍寫；任何 `OSError`／序列化錯誤 ⇒ 回傳 `False`、
  `failures += 1`，**不拋例外**。
- 測試：寫入後逐行 `json.loads`；跨 UTC 日換檔；`prune` 刪 >14 天的檔、保留較新與無關檔名；
  目錄是檔案或唯讀時 `write` 回 `False` 且不拋；Decimal 序列化；超過上限時 essential 區分。
- 驗收：`uv run pytest tests/test_cycle_trace.py -q` 全綠。

### T4 `@sdd` 可疑讀值判定（純邏輯）
- 檔案：新增 `src/spark/copytrade/fresh_read.py`、`tests/test_fresh_read.py`。
- 型別與簽章：
  ```python
  class ReadVerdict(str, Enum):  OK = "ok"; INVALID = "invalid"; SUSPICIOUS_FLAT = "suspicious_flat"

  @dataclass
  class FreshReadState:          # 跨輪（掛在 ReconcileState 上，T6）
      consecutive_skips: int = 0         # 「連續幾輪有被跳過」（以輪為單位，不是以讀取點為單位）
      consecutive_flat: int = 0          # 連續幾輪（重讀後）仍讀到空
      last_leader_had_positions: bool = False
      skipped_in_cycle: bool = False     # 本輪是否有任何一個讀取點被跳過

  @dataclass(frozen=True)
  class ReadDecision:
      proceed: bool
      reason: str                         # 例 "ok" "recovered_on_retry" "confirmed_flat_two_cycles"
                                          #   "suspicious_flat_first" "invalid" "read_failed:<ExcType>"
      snapshot: UserSnapshot | None       # proceed 時為採用的那份讀值
      retried: bool

  def classify_leader_read(snap: UserSnapshot, *, prev_had_positions: bool,
                           my_has_exposure: bool) -> ReadVerdict: ...
  def read_leader_with_confirmation(read: Callable[[], UserSnapshot], *, state: FreshReadState,
                                    my_has_exposure: bool, retry_s: float,
                                    sleep_fn: Callable[[float], None]) -> ReadDecision: ...
  def begin_cycle(state: FreshReadState) -> None: ...        # skipped_in_cycle = False
  def mark_skip(state: FreshReadState) -> None: ...          # skipped_in_cycle = True
  def end_cycle(state: FreshReadState, *, warn_after: int, crit_after: int) -> str: ...
      # 本輪有跳過 ⇒ consecutive_skips += 1；沒有 ⇒ consecutive_skips = 0。
      # 回傳 "" | "warn"（>=warn_after 且 <crit_after）| "critical"（>=crit_after）
  ```
  ⚠️ 為什麼「以輪為單位」：每輪有兩個讀取點（開頭、安全網）。若各自 `+1`／`歸零`，開頭讀取成功時的歸零
  會把安全網連續被跳過的計數洗掉，警告永遠不會觸發。
- `classify_leader_read` 真值表（`ntl`＝`state.total_ntl_pos`、`av`＝`state.account_value`）：

  | positions | av / ntl | prev_had_positions 或 my_has_exposure | 結果 |
  |---|---|---|---|
  | 非空 | av>0 且 ntl>0 | — | OK |
  | 非空 | av≤0 或 ntl≤0 | — | INVALID |
  | 空 | ntl>0 或 av<0 | — | INVALID（矛盾） |
  | 空 | ntl==0 且 av≥0 | 是 | SUSPICIOUS_FLAT |
  | 空 | ntl==0 且 av≥0 | 否 | OK（沒東西可平） |

- `read_leader_with_confirmation` 流程：`read()` 例外 ⇒ 視為 INVALID(`read_failed:<ExcType>`)。
  第一次 OK ⇒ `consecutive_flat=0`、更新 `last_leader_had_positions`、proceed。否則
  `sleep_fn(retry_s)` 後**重讀一次**：OK ⇒ proceed（`recovered_on_retry`）；
  SUSPICIOUS_FLAT ⇒ `consecutive_flat += 1`，`>=2`（連續兩輪重讀後仍空）⇒ proceed
  （`confirmed_flat_two_cycles`），否則 skip（`suspicious_flat_first`）；INVALID／例外 ⇒
  `consecutive_flat=0`、skip。**skip 時不改 `last_leader_had_positions`。**
  本函式遇到 skip 時呼叫 `mark_skip(state)`；**不**動 `consecutive_skips`（由 `end_cycle` 統一累計）。
- 測試涵蓋真值表每一列、重讀成功、連續兩輪確認空、第一輪空第二輪恢復後 `consecutive_flat` 歸零、
  `end_cycle` 在連續 2／3／9／10／11 輪有跳過時的回傳（第 3 輪起 `warn`、第 10 輪起 `critical`），
  以及「開頭讀取成功、只有安全網被跳過」連續 3 輪仍會在第 3 輪回 `warn`（上面那個 bug 的回歸測試）；
  `sleep_fn` 只在需要重讀時被呼叫且收到 `retry_s`。
- 驗收：`uv run pytest tests/test_fresh_read.py -q` 全綠。

### T5 `@sdd` 設定欄位
- 檔案：`src/spark/copytrade/config.py`、`tests/test_copy_config.py`（補測試）。
- 做法：**照抄 `settle_seconds`（欄位）與 `max_consecutive_errors`（`_env_int` 讀法、`__post_init__` 驗證）
  的既有寫法**，不要發明新模式；找不到驗證區塊該放哪裡 ⇒ 停止並回報，不要自己決定。
- 新增 `CopySettings` 欄位（皆有預設，放在 `settle_seconds` 附近）：
  `fresh_read_retry_s: int = 2`、`fresh_read_warn_after: int = 3`、`fresh_read_crit_after: int = 10`；
  `from_env` 以 `_env_int` 讀 `COPY_FRESH_READ_RETRY_S`／`_WARN_AFTER`／`_CRIT_AFTER`；
  `__post_init__`（或既有驗證處）要求 `retry_s >= 0`、`1 <= warn_after <= crit_after`。
- 驗收：新增測試覆蓋預設值、env 覆寫、非法值被拒；`uv run pytest tests/test_copy_config.py -q` 全綠。

### T6 `@inline` A1-開頭：合併讀取與可疑讀值守衛
- 檔案：`src/spark/copytrade/loop.py`（約 318–324 行）、`src/spark/copytrade/orders.py`
  （`ReconcileState` 加 `fresh: FreshReadState = field(default_factory=FreshReadState)`；
  `CycleReport` 加 `skipped_reason: str | None = None`）、`scripts/run_copytrade.py`
  （心跳 `hb["result"]` 增加 `"skipped"`，detail 帶 `skipped_reason`）。
- 改動：
  1. 開頭四次讀取（leader/自己各 `get_positions`＋`get_account_state`）改為
     `adapter.get_user_snapshot(leader)`／`adapter.get_user_snapshot(ex.my_address)` 兩次；
     後續仍以 `leader_positions`（dict）、`leader_state`、`my_positions`、`my_state` 為名取用，
     下游邏輯不變。leader 掛單、自己掛單讀取不變。
     **讀取順序改為**：leader 掛單 → 自己掛單 → 自己快照 → leader 快照（含守衛）。理由：守衛的
     `my_has_exposure`（自己有部位或掛單）必須在判定 leader 讀值前就知道。
  2. leader 的那次讀取包進 `read_leader_with_confirmation(...)`（`my_has_exposure` ＝自己讀到的
     部位或掛單非空）。結果 skip ⇒ 本輪**整輪**不做對帳與校正：回傳
     `CycleReport(reconcile=_EMPTY_RECONCILE, safety_net={"skipped": True}, scale=Decimal("0"),
     skipped_reason=<reason>)`（**不是** `tripped=True`）。
  3. 這個 skip **不是例外**，不計入 `main_loop` 的連續錯誤。
  4. 計數與通知統一走輪級帳：進入讀取前 `begin_cycle(state.fresh)`；skip 回傳前與正常結束回傳前各呼叫一次
     `end_cycle(...)`（抽成 `loop.py` 內的小 helper，兩處共用）；回傳 `warn`／`critical` 時才發 `notifier`
     （category `"fresh_read"`、dedup_key `fresh_read_skip_warn`／`fresh_read_skip_crit`，訊息含連續跳過輪數、
     最近一次原因、`last_leader_had_positions`）。cycle 中途拋例外時不呼叫 `end_cycle`（計數不變）。
  為什麼開頭 skip 要「整輪」不做事、而安全網 skip 只略過校正：開頭讀值同時決定 scale 與對帳用的
  期望掛單量，讀值壞（例如 `account_value=0` ⇒ scale=0）會讓對帳把自己所有掛單撤光；安全網 skip 時 A 段已用
  先前驗證過的快照做完，所以只需略過部位校正。
- 測試（`tests/test_copy_loop.py` 風格，用 `FakeAdapter`＋假 `sleep_fn`）：leader 讀值正常 ⇒ 行為與
  改前一致；空讀值第一輪 ⇒ skip 且**零寫入**（`ex.records` 無新增）、第二輪仍空 ⇒ 照常跟平；
  `account_value=0` 且有部位 ⇒ skip（**不得**走到 scale=0 的全平／撤單路徑）；讀取拋例外 ⇒ skip、不拋；
  第 3／10 輪連續 skip 發 warn／critical；skip 的輪次 `CycleReport.skipped_reason` 有值、心跳 result 為 `skipped`。
- 驗收：`uv run pytest tests/test_copy_loop.py tests/test_fresh_read.py -q` 全綠；
  `grep -n "get_positions(leader)\|get_account_state(leader)" src/spark/copytrade/loop.py` 無輸出。

### T7 `@inline` A1-安全網：新鮮讀值與新鮮 scale
- 檔案：`src/spark/copytrade/loop.py`（`_safety_net`，約 410–425 行）。
- 改動：`_safety_net()` 改為：
  1. 以 `read_leader_with_confirmation` 重抓 leader（`adapter.get_user_snapshot(leader)`）；skip ⇒
     回傳 `{"skipped": True, "reason": <reason>}`（輪級計數與通知由 T6 的 `end_cycle` 統一處理，此處不另發）；
  2. proceed ⇒ 自己改用 `adapter.get_user_snapshot(ex.my_address)`（取代原本單獨的
     `get_positions`；呼叫數不變）；
  3. 依 D4 重算 scale：把 `compute_scale_factor` 的呼叫抽成區域函式，開頭與此處共用同一個
     （`leader_equity`／`my_equity`／`target_notional` 用各自那次的讀值，`weight` 沿用開頭）；
  4. `sync_positions(ex, fresh_leader_positions, fresh_my_positions, fresh_scale, ...)`；
  5. 回傳的 dict 附 `snapshot_age_s`（新鮮讀值 `fetched_at_ms` 減開頭 leader 讀值 `fetched_at_ms`）、
     `scale_snapshot`、`scale_fresh` 供 T8 寫 trace。
- 不變：`protected`、`no_new_exposure`、`mids=adapter.get_all_mids()` 的取法。
- **複審補強（2026-10-09）**：
  - 中性化（3.5 節）抽成 `run_cycle` 內的區域函式 `_leader_equity_for(raw: Decimal) -> Decimal`：
    `flows` 只在開頭取一次（停用或取數失敗 ⇒ `None`）；函式內做 `adjusted_leader_equity` 與既有
    `adj <= 0 < raw` 幻影歸零守衛（critical 去重鍵不變）；開頭與安全網都呼叫它。既有 warn／critical
    文案與 dedup_key 一字不改。
  - 安全網的守衛呼叫：`read_leader_with_confirmation(lambda: adapter.get_user_snapshot(leader), state=state.fresh,
    my_has_exposure=<新鮮自己快照有部位 or my_orders 非空>, retry_s=settings.fresh_read_retry_s, sleep_fn=sleep_fn)`；
    自己的新鮮快照在 leader 守衛**之前**讀（順序同 T6）。
  - 回傳 dict 的補充鍵用 `net = sync_positions(...)` 後 `net.update({...})` 加上，**不改 `sync_positions` 簽章**
    （T8 才加 `decisions`）。補充鍵：`snapshot_age_s`（Decimal，兩個 `fetched_at_ms` 之差／1000）、
    `scale_snapshot`、`scale_fresh`、`safety_net_start_ms`（進入 `_safety_net` 時的 `int(time.time()*1000)`）、
    `fresh_read`（`{"reason": decision.reason, "retried": decision.retried}`）。
  - 呼叫計數：`FakeAdapter.get_user_snapshot` 走基底預設實作（不記 `calls["get_user_snapshot"]`），
    測試以 `fa.calls["get_positions"]`／`fa.calls["get_account_state"]` 的 address 序列斷言：
    一輪為 `[MY, LEADER, MY, LEADER]`（開頭自己、開頭 leader、安全網自己、安全網 leader；守衛重讀時 leader 會多出現）。
- 測試（用 §6 的事故數字；`FakeAdapter` 需要能讓第 2 次讀 leader 回不同的部位與帳戶——用子類覆寫
  `get_positions`／`get_account_state` 依呼叫序回傳不同值，同 T6 測試的做法）：開頭 leader ETH=0.0913／
  scale 5.18，安全網時 leader ETH=0.2463／scale 3.43、自己 ETH=1.1958 ⇒ 校正為 **減倉約 0.351**
  （不是 0.7226）；HYPE 同理（約 3.09，不是 5.87）。另測：安全網 leader 讀值 INVALID ⇒ `safety_net`
  回 `{"skipped": True, "reason": ...}`、零校正寫入、A 段結果不受影響、`state.fresh.skipped_in_cycle` 為 True
  且 `_finish_fresh_read_cycle` 帶到該 reason。
- 驗收：`uv run pytest tests/test_copy_loop.py tests/test_fresh_read.py -q` 全綠。

### T8 `@inline` 可觀測性：逐幣決策與 cycle_trace
- 檔案：`src/spark/copytrade/positions.py`（`sync_positions` 加可選參數
  `decisions: list[dict] | None = None`，**回傳值不變**）、`src/spark/copytrade/orders.py`
  （`ReconcileState` 加 `trace: CycleTraceWriter | None = None`）、`src/spark/copytrade/loop.py`
  （組裝並寫 trace）、`scripts/run_copytrade.py`（約 718 行：
  `ReconcileState(trace=CycleTraceWriter(state_root / "var/copytrade"))`）。
- `decisions` 每幣一筆：`{coin, leader_szi, scale, target_size, my_size, diff, branch}`，
  `branch ∈ {"min_notional","open","flip","within_tolerance","increase","decrease","min_notional_adjust","protected_open","protected_increase","cost_breaker_open","cost_breaker_flip_open","cost_breaker_increase","flatten","skipped_spot_or_dex"}`。
- 每輪寫一筆 `kind="cycle"` 彙總（`essential=True`）：`ts`（ISO）、各讀取的 wall-clock 毫秒
  （`leader_orders_ms`＝讀 leader 掛單前後各取一次 `int(time.time()*1000)`、`leader_snapshot_ms`／
  `my_snapshot_ms`＝`UserSnapshot.fetched_at_ms`、`safety_net_start_ms`＝T7 回傳 dict 的同名鍵）、`snapshot_age_s`、leader／自己的
  `account_value`、`total_ntl_pos`、逐幣 `szi`、資金使用率、波動權重與其輸入（今日 |PnL|、mu、sigma、z）、
  `scale_snapshot`／`scale_fresh`、`fresh_read`（verdict、是否重讀、`consecutive_skips`、`consecutive_flat`）、
  對帳計數（placed／cancelled／modified／consumed_skipped／deferred_alo_cross／ambiguous_modify）、
  `trace_failures`／`trace_dropped`。skip 的輪次也要寫。
- 有任何校正動作、或新鮮與快照的 leader 部位／scale 不同時，另寫 `kind="decision"` 明細
  （含 `decisions` 與新鮮／快照兩份 leader 逐幣 `szi`）。
- 寫入失敗 ⇒ `writer.failures` 增加；每個進程第一次失敗發一則 `notifier.warn`（dedup_key `cycle_trace_write_failed`）；
  「已發過」的旗標放 `ReconcileState.trace_warned: bool = False`（與 `risk_off_warned_at` 同一種跨輪狀態，不用模組全域）。
- **複審補強（2026-10-09）**：開頭 skip 的早退路徑也必須寫 `kind="cycle"`（欄位缺的填 `None`），寫在
  `_finish_fresh_read_cycle` 之後、`return` 之前；trace 組裝與寫入包在一個 `_write_trace(...)` 區域函式裡，
  任何例外吞掉並 `failures` 計數（D7），**絕不**讓 trace 影響 `run_cycle` 的回傳。
- 測試：假 `CycleTraceWriter`（收集 records）驗證欄位齊全、skip 輪次也寫、明細只在有動作／差異時寫、
  `sync_positions` 的 `decisions` 每幣一筆且回傳值與改前相同（既有 `tests/test_copy_sync_positions.py` 全綠）。
- 驗收：`uv run pytest tests/test_copy_loop.py tests/test_cycle_trace.py tests/test_copy_sync_positions.py -q` 全綠。

### T9 `@inline` W2＋A2＋B1（掛單對帳與韌性）
- 檔案：`src/spark/copytrade/orders.py`（`_reconcile_orders`，約 745–870 行）、
  `src/spark/copytrade/executor.py`、`src/spark/resilience.py`；測試新增
  `tests/test_copy_orders_consumed_and_5xx.py`。
- `executor.py`：新增 frozen dataclass `WriteOutcome(ok: bool, kind: str, reason: str = "")`
  （`kind ∈ {"ok","consumed","rejected"}`）；`ActionExecutor.modify_outcome(oid, spec) -> WriteOutcome`、
  `cancel_outcome(coin, oid) -> WriteOutcome`（呼叫 T2 的 `*_result`，用 T1 的
  `is_consumed_reason` 決定 kind）；既有 `modify`／`cancel` 改為委派 `.ok`，**每次呼叫仍只記一筆
  `ActionRecord`**，payload 增 `reason`、`kind`。dry 模式（虛擬簿）kind 恆為 `"ok"`／`"rejected"`。
  **不修改 `ExecutorPort`（D8）。**
- `orders.py`：新增 `_modify_outcome(ex, oid, spec)`／`_cancel_outcome(ex, coin, oid)`（`getattr` 鴨子型別，D8）。
  `_reconcile_orders` 改動：
  1. 步驟 1 迴圈：`try` 包住 modify。例外 ⇒ D1（`ambiguous` 計數、TTL、`touched_oids`、
     `notifier.warn("orders", …, dedup_key=f"modify_ambiguous:{coin}")`、`logger.warning`），**繼續處理下一張**；
     `kind=="consumed"` ⇒ 該 spec **本輪不下單**（`consumed_skipped += 1`；**不**登記 TTL、**不**進 fallback、
     oid 進 `touched_oids`；下一輪若 leader 該價位仍有單會自然重掛）；其餘 False ⇒ 維持現行（TTL＋fallback）。
  2. 步驟 2 撤單：fallback 的撤單若 `kind=="consumed"` ⇒ 該 spec **不再下單**（計入 `consumed_skipped`）；
     其他失敗維持現行（照樣下單）。`plan.to_cancel` 的撤單結果不影響下單。
  3. 步驟 3 掛新單：失敗且 `is_post_only_cross_reason(reason)` ⇒ 計入 `deferred_alo_cross`，**不**進 `failed`。
  4. 步驟 4 settle 補單：補單失敗且為 post-only 吃單 ⇒ 計入 `deferred`，**不**進 `place_fails`，
     並比照補單成功的方式扣減對應 `failed` 預算（讓第二次驗證視為已消耗）；最終 CRIT 的 `missing`
     與「補單失敗」清單都不含這些 spec。其他拒因（保證金不足等）**行為不變**，仍發 CRIT。
  5. `ReconcileResult` 末端新增（皆預設 0）：`consumed_skipped`、`deferred_alo_cross`、`ambiguous_modify`。
- `resilience.py`：`_is_transient_error` 增加 `getattr(exc, "status_code", None) in (500, 502, 503, 504)`（D2）。
- 測試（假 `ex`／`FakeAdapter`，離線）：
  a. modify→consumed：不撤、不下該 spec、無 CRIT、`consumed_skipped==1`、不登記 TTL；
  b. modify→一般拒（未知字串）：維持 TTL＋fallback＋下單（回歸）；
  c. fallback 撤單→consumed：不下單；撤單→未知拒因：照下單（回歸）；
  d. 3 張 modify 第 1 張拋 `ServerError(500,'null')`：後 2 張照常、整輪不拋、`ambiguous_modify==1`、
     TTL 已登記、無 CRIT、有一則 warn；
  e. 首輪下單被 post-only 拒：不進 failed、settle 不補、無 CRIT、`deferred_alo_cross==1`；
  f. settle 補單被 post-only 拒：無 CRIT；
  g. 保證金不足拒因：仍發 CRIT（回歸）；
  h. `tests/test_resilience*.py` 增：`ServerError(500)` 對冪等 `cancel` 會重試、對 `modify_order` 不重試仍拋、
     `ClientError(400)` 不視為暫時性、含 "500" 字樣的語意錯誤字串不被誤判。
- 驗收：`uv run pytest tests/test_copy_orders_consumed_and_5xx.py tests/test_resilience.py tests/test_resilience_boundary.py tests/test_copy_orders_settle_no_dup.py tests/test_copy_orders_reconcile.py tests/test_copy_orders_crit_alert.py tests/test_copy_executor.py -q` 全綠。

### T10 `@sdd` B2：連敗退出的 CRIT 文案
- 檔案：`src/spark/copytrade/loop.py`（`main_loop`，約 472–496 行）、`tests/test_copy_loop.py`。
- 改動：以區域變數 `first_error_at: float | None`（`consecutive_errors` 由 0→1 時記 `clock()`）；
  CRIT 文案改為：
  `連續 {n} 次同步失敗（最後錯誤：{e!r}；首次失敗 {first_error_at 的 UTC 時間 HH:MM:SS}）。引擎將退出，systemd 約 10 秒後自動重啟；通常無需人工介入，若本訊息重複出現或引擎未恢復才需處理`。
  `SystemExit(1)`、閾值語意、`dedup_key` 都不變。
- 測試：n 次例外後 `notifier.critical` 的文字**包含**「自動重啟」與首次失敗時間、**不包含**「需人工介入」；
  成功一輪後 `first_error_at` 重置（再連敗時用新的時間）。
- 驗收：`uv run pytest tests/test_copy_loop.py -q` 全綠。

### T11 `@inline` 事故重播回歸測試與變異驗證
- 檔案：新增 `tests/test_copy_stale_snapshot_replay.py`。
- 用 §6 的數字整合 `run_cycle`（`FakeAdapter`＋假時鐘／假 `sleep_fn`）重播三段：
  R1 B 段新鮮輸入（ETH 減倉約 0.351、HYPE 約 3.09，**不得**出現 0.7226／5.87）；
  R2 掛單對帳遇到「已成交」（modify→consumed、cancel→consumed）⇒ 零下單、零 CRIT；
  R3 重掛被 post-only 拒 ⇒ 零 CRIT、`deferred_alo_cross`>0；
  R4 modify 拋 500 ⇒ 整輪完成。外加讀值守衛：空→空（兩輪）才跟平、`account_value=0`→skip。
- 每個測試要附**路徑覆蓋計數**（斷言 `get_user_snapshot` 被呼叫的次數、`decisions` 非空、
  `consumed_skipped`／`deferred_alo_cross`／`ambiguous_modify` > 0），避免空轉通過（judgment.md §2）。
- **變異驗證**（至少這 5 個，逐一暫時還原、確認對應測試**失敗**、再還原；在回報裡貼每個變異的
  失敗輸出一行）：
  M1 `_safety_net` 改回沿用開頭 `leader_positions`／`scale` ⇒ R1 失敗；
  M2 modify→consumed 仍退回 fallback ⇒ R2 失敗；
  M3 post-only 拒因仍進 `failed` ⇒ R3 失敗；
  M4 守衛把空讀值當 leader 全平 ⇒ 守衛測試失敗；
  M5 modify 例外不接 ⇒ R4 失敗。
- 驗收：`uv run pytest tests/test_copy_stale_snapshot_replay.py -q` 全綠＋回報含 5 個變異的失敗證據。

### T12 `@inline` 文件
- 檔案：`deploy/RUNBOOK.md` 新增 §5.8m（`cycle_trace` 位置／欄位／保留與上限、fresh-read 守衛行為與三個設定、
  warn／critical 的意義與處置、B2 新文案、部署前檢查清單〔§7〕）；`docs/superpowers/research/2026-10-09-crash-0207-stale-snapshot-walkthrough.md`
  §8 補上「cycle_trace 查詢範例」（`jq` 一行）。不改其他章節。
- 驗收：`grep -n "5.8m" deploy/RUNBOOK.md` 有標題；回顧文件 §8 含 `cycle_trace` 範例。

### T13 `@inline` W1：安全網加倉先扣未成交同向掛單（D11）
- 檔案：`src/spark/copytrade/positions.py`（`sync_positions` 加可選參數
  `resting_entry_qty: Mapping[str, Decimal] | None = None`：coin → 該幣同方向非 ro 非 trigger 掛單總量；
  None ⇒ 行為與現在完全相同）、`src/spark/copytrade/loop.py`（`_safety_net` live 路徑：守衛 proceed 後、
  `sync_positions` 前重讀 `ex.get_open_orders()`，依**新鮮 leader 部位的方向**彙總每幣的同向進場掛單量；
  dry 路徑用開頭的 `my_orders` 彙總；回傳 dict 加 `resting_entry_qty`）、測試。
- `sync_positions` 行為：`open` 分支 `net = target_size − resting`；`increase` 分支 `net = diff − resting`；
  `net <= 0` ⇒ `result["skipped"].append({"coin", "reason": "covered_by_resting_orders"})`、decision branch 同名、
  **不下單**；`net > 0` ⇒ 以 `net` 走既有 min_notional_adjust gate 與下單；`decisions` 記錄多一鍵 `resting`。
  `decrease`／`flip`／`flatten`／protected／cost_breaker 分支**一字不改**。
- 測試（新檔 `tests/test_copy_positions_resting_net.py`＋在 `tests/test_copy_loop_safety_net_fresh.py` 加整合案例）：
  (a) reviewer 的探針情境（leader 1.0→1.5、自己 4.0、掛著同價買單 2.0）⇒ **零** `market_open`、skipped reason
  `covered_by_resting_orders`；(b) 掛單 1.0 時 ⇒ `market_open` 1.0（淨量）；(c) 反向掛單（賣單）不扣；(d) reduce-only
  掛單不扣；(e) 減倉情境有 ro 掛單仍照減（不扣）；(f) `resting_entry_qty=None` 時與改前逐位元相同（既有
  `tests/test_copy_sync_positions.py` 全綠）；(g) 呼叫計數：live 一輪 `get_open_orders` 對自己的呼叫數比改前多 1。
- 驗收：`uv run pytest tests/test_copy_positions_resting_net.py tests/test_copy_sync_positions.py tests/test_copy_loop_safety_net_fresh.py tests/test_copy_stale_snapshot_replay.py tests/test_copy_loop_trace.py -q` 全綠；全套 0 failed。

### T14 `@inline` W2：部分減倉不重試、全平維持重試（D12）
- 檔案：`src/spark/resilience.py`（`ResilientExchange.order(self, *a, reduce_only=False, _verify=None, _idempotent=None, **k)`：
  `_idempotent` 非 None 時以它為準，否則沿用 `bool(reduce_only)`；**不得**把 `_idempotent` 傳給 SDK）、
  `src/spark/exchange/base.py`（`close_reduce_only(..., full_close: bool = False)`，abstract 簽章加預設參數）、
  `src/spark/exchange/hyperliquid.py`（傳 `_idempotent=full_close`）、`src/spark/exchange/fakes.py`（接受並記錄 `full_close`）、
  `src/spark/copytrade/executor.py`（`ExecutorPort.close_reduce_only` 與 `ActionExecutor.close_reduce_only` 加
  `full_close: bool = False` keyword，ActionRecord payload 記錄）、`src/spark/copytrade/positions.py`
  （`_try_close_reduce_only` 加 `full_close` 參數：`decrease` 分支傳 False；flip 平倉腿與 flatten 傳 True）、
  `src/spark/copytrade/killswitch.py:792`（flatten 傳 `full_close=True`）、測試。
- 既有測試替身若沒有 `full_close` 參數 ⇒ 呼叫端一律以 keyword 傳，替身用 `**kw` 吸收者不用改；替身簽章寫死者
  才需最小修改（逐一列出）。
- **主線程裁決（2026-10-09，builder 依升級協定回報）**：`scripts/panic.py` 的 `_AdapterExecutor.close_reduce_only` 納入 T14
  範圍：加 `full_close: bool = False` 並轉給 `self._adapter.close_reduce_only(..., full_close=full_close)`；
  `tests/test_panic_all.py` 的 adapter 替身簽章做最小修改。理由：kill switch flatten 傳 `full_close=True` 會打到
  panic 的 executor，不改 ⇒ TypeError 被 `trip()` 當成平倉失敗 ⇒ **緊急平倉整個失效**（真實 bug，不是測試問題）；
  改成只在 True 時才傳參數是 hack；讓 panic 走預設 False 則是把全平降成單次嘗試（fail-open）。
- 測試：(a) `ServerError(500)` 下 `order(reduce_only=True, _idempotent=False)` 單次嘗試、拋出；`_idempotent=True` 重試；
  (b) `positions.decrease` 走 `full_close=False`、flip／flatten 走 `True`（以 FakeAdapter calls 斷言）；
  (c) killswitch flatten 走 `True`；(d) `tests/test_resilience*.py` 既有案例全綠。
- 驗收：`uv run pytest tests/test_resilience.py tests/test_resilience_boundary.py tests/test_copy_sync_positions.py tests/test_copy_killswitch.py tests/test_kill_switch.py tests/test_copy_executor.py tests/test_fake_adapter.py -q` 全綠；全套 0 failed。

### T15 `@inline` 複審修正：精確扣法（D13）、讀取順序（D14）、減倉例外不中止 B 段（D15）
- 檔案：`src/spark/copytrade/positions.py`（`resting_entry_qty_by_coin` 改簽章：
  `(open_orders, leader_positions, leader_orders, *, px_rel_tol) -> dict[str, Decimal]`，只計「同幣、同方向、
  非 ro、非 trigger、且 leader_orders 中沒有同幣同方向價格在 `px_rel_tol` 內的對應單」的掛單量；價格比較可
  import `orders._prices_equal`（若會循環 import 則在 positions.py 內寫等價的私有函式）；`_try_close_reduce_only`
  加 try/except（D15））、`src/spark/copytrade/loop.py`（`_safety_net` live 路徑依 D14 重排：`fresh_orders =
  ex.get_open_orders()` → `fresh_my` → 守衛讀 leader → `fresh_leader_orders = adapter.get_open_orders(leader)`
  → 彙總 → `sync_positions`；dry 路徑傳開頭的 `my_orders`、`leader_orders`；回傳 dict 的 `resting_entry_qty`
  語意改為「落後鏡射單量」）、測試。`sync_positions` 的 `resting_entry_qty` 參數與 open／increase 分支邏輯**不變**
  （T13 已做），只是輸入的彙總規則改了。
- 測試（把 reviewer 三支探針改寫為測試，放 `tests/test_copy_loop_safety_net_fresh.py` 或新檔
  `tests/test_copy_safety_net_lagging_mirror.py`；原探針在
  `/private/tmp/claude-501/-Users-jim-projects-spark/62c4f3f9-be01-44bf-8a9c-7011431cdadc/scratchpad/`
  的 `probe_stale_orders.py`、`probe_ladder_underfollow.py`、`probe_read_order_race.py`）：
  (a) 階梯情境（leader 多 1.0＋遠價買單 2.0 一直掛著；我方無部位、鏡射買單 8.0）⇒ 第一輪 `market_open` 4.0
  （target 1.0×4），resting 為 {}（有對應單不扣）；(b) W1 情境（leader 單成交、我方同價鏡射 2.0 仍掛，leader 新鮮
  掛單簿已無該價）⇒ `market_open []`、resting {ETH: 2.0}；(c) leader 市價加倉＋我方階梯單存在 ⇒ 立即跟（增量全補）；
  (d) 讀取競態：鏡射單在「讀掛單」與「讀部位」之間成交 ⇒ 零 market_open（少跟一輪，而非多補）；讀取順序斷言
  `get_open_orders` 的 address 序列為 `[LEADER, MY, MY(settle), MY(B), LEADER(B)]`（依實際 settle 次數調整）；
  (e) D15：`close_reduce_only` 對第一個幣拋 `ServerError(500,'null')` ⇒ 第二個幣的減倉與第三個幣的 flatten 仍執行、
  `result["failed"]` 有該幣、有一則 `close_exc:` warn、`run_cycle` 不拋；(f) dry 路徑 resting 為 {}；
  (g) 既有 `tests/test_copy_positions_resting_net.py` 依新簽章最小調整（只改呼叫方式，不改行為斷言；需要
  `leader_orders` 參數的測試給空清單＝全部無對應＝與原測試語意相同）。
- 驗收：`uv run pytest tests/test_copy_positions_resting_net.py tests/test_copy_sync_positions.py tests/test_copy_loop_safety_net_fresh.py tests/test_copy_stale_snapshot_replay.py tests/test_copy_loop_trace.py tests/test_full_close_idempotency.py -q` 全綠；全套 0 failed；三支探針由主線程重跑：階梯 ⇒ 有開倉、W1 ⇒ `market_open []`、競態 ⇒ `market_open []`。

### T16 `@inline` 複審 3 修正：依量扣（D16）、減倉例外升級（D17）、dry 路徑用虛擬簿（D18）
- 檔案：`src/spark/copytrade/positions.py`（`resting_entry_qty_by_coin(open_orders, leader_positions, leader_orders, *, px_rel_tol, scale) -> dict[str, Decimal]`，依 D16 實作；docstring 改寫）、`src/spark/copytrade/loop.py`（`_safety_net`：live 傳 `scale=scale_fresh`；dry 依 D18 改用 `ex.get_open_orders()` 與開頭 `leader_orders`、`scale=scale`；`_safety_net` 回傳後依 D17 更新 `state.close_exc_streak` 並視門檻發 critical——寫成 `loop.py` 的小 helper，放在 `_finish_fresh_read_cycle` 旁）、`src/spark/copytrade/orders.py`（`ReconcileState.close_exc_streak: dict[str, int] = field(default_factory=dict)`）、`src/spark/copytrade/config.py`（`close_exc_crit_after: int = 5`，`from_env` 讀 `COPY_CLOSE_EXC_CRIT_AFTER`，`__post_init__` 驗 `>= 1`，照 `fresh_read_crit_after` 寫法）、測試。
- 測試（補進 `tests/test_copy_safety_net_lagging_mirror.py` 或新檔）：(a) 部分成交：leader 2.0@2000 剩 1.5、我方 8.0@2000、scale 4 ⇒ lag = 8 − 6 = 2.0 ⇒ `market_open []`（reviewer 探針 partial_fill，在 `/private/tmp/claude-501/-Users-jim-projects-spark/62c4f3f9-be01-44bf-8a9c-7011431cdadc/scratchpad/probe_r3_d13_edges.py`，用 `PYTHONPATH=/Users/jim/projects/spark` 執行）；(b) 同價多張：leader 兩張 0.5 其中一張成交 ⇒ lag 2.0 ⇒ `market_open []`；(c) 階梯完整在簿 ⇒ lag 0 ⇒ 照常開倉（既有測試不變）；(d) 我方量 < scale×leader 量（leader 加量、我方還沒跟）⇒ lag 0；(e) 無對應價位 ⇒ 全量落後；(f) 相鄰兩層在 `px_rel_tol` 內（既有觀察）至少寫一個案例記錄現行行為；(g) D17：連續 5 輪同幣 `close_reduce_only` 例外 ⇒ 第 5 輪發 `close_exc_crit:<coin>` critical、第 4 輪不發、中間成功一輪歸零；(h) D18：dry 虛擬簿有一張 leader 已無對應的殘留單 ⇒ 被扣（少跟一輪）且 dry 不多做 HTTP（adapter `get_open_orders` 對自己的呼叫數不增）；原 `test_w1_dry_mode_uses_start_orders_for_resting` 與新檔 dry 測試依 D18 調整 fixture／斷言並逐一列出；(i) config 三態（預設 5、env 覆寫、0 被拒）。
- 驗收：`uv run pytest tests/test_copy_safety_net_lagging_mirror.py tests/test_copy_positions_resting_net.py tests/test_copy_sync_positions.py tests/test_copy_loop_safety_net_fresh.py tests/test_copy_stale_snapshot_replay.py tests/test_copy_loop_trace.py tests/test_copy_config.py tests/test_copy_loop.py -q` 全綠；全套 0 failed；主線程重跑 `probe_r3_d13_edges.py`：partial_fill／same_px_stack ⇒ `market_open []`、modify_plus_mkt_add 不變。
  **（D19／D20 生效後過時：**D20 下 partial_fill ⇒ resting ≈1.478／market_open ≈0.522、same_px_stack ⇒ ≈1.826／≈0.174，差額＝scale×該層 leader 剩餘量×(1/(1−tol)−1)，是 D19 承認、D20 校準的代價；見 T17／T18。）

### T17 `@inline` 複審 4 修正：lag 套用 size_tolerance（D19）、補 scale_fresh 鎖定測試（W2）、分群容差測試與命名
- 檔案：`src/spark/copytrade/positions.py`（`resting_entry_qty_by_coin` 加 keyword `size_tol: Decimal`，每群公式改為 D19；docstring 補 D19 段落）、`src/spark/copytrade/loop.py`（兩個呼叫點傳 `size_tol=settings.size_tolerance`）、測試。
- 測試：(a) reviewer 探針 `oversize_8.6_x5`（我方每層 8.6、leader 每層 2.0、scale 4、size_tolerance 0.08；leader 市價加倉 1.0→1.5）⇒ `market_open` 2.0、resting {}（探針在 scratchpad `probe_r4_sizetol.py`，用 `PYTHONPATH=/Users/jim/projects/spark uv run python` 跑，不要改它）；(b) 我方 9.0（超過 8%）⇒ lag = 9.0 − 8.64 = 0.36；(c) 部分成交 2.0→1.5 仍 lag = 8 − 6.48 = 1.52（> 0，仍被扣）；部分成交只有 2.0→1.9（5%）⇒ lag 0（已知代價，寫在測試 docstring）；(d) **W2**：整合測試讓開頭 scale（約 5.18）與 `scale_fresh`（約 3.43）明顯不同，斷言 B 段的 lag 用的是 `scale_fresh`——把 live 路徑的 `scale=scale_fresh` 換成 `scale=scale` 時必須變紅（在回報裡附這個變異的失敗行，變異後務必 `git checkout -- src` 還原）；(e) leader 側分群容差：leader 兩張 2000／2000.15 都在、我方 2000.15 一張 ⇒ 不扣（把分群的 `_prices_equal` 換成 `==` 時必須變紅，附失敗行並還原）；(f) 把 `test_w1_dry_mode_uses_start_orders_for_resting` 改名為 `test_dry_mode_reads_virtual_book_after_a_phase_zero_http`（純改名，斷言不動）。
- **主線程裁決（2026-10-09，builder 依升級協定回報）**：T16 的兩個整合測試 `test_d16_integration_partial_fill_no_market_open`／
  `test_d16_integration_same_price_stack_no_market_open` 的斷言在 D19 下必須改為 resting 1.52／market_open 0.48 與 1.84／0.16
  （差額＝0.08×scale×該層 leader 剩餘量；**D20 後再更新為 ≈1.478／≈0.522 與 ≈1.826／≈0.174**），docstring 註明為 D19 已知代價；(e) 的 fixture 改為我方單張 12.0@2000.15（8.0 抓不到
  `==` 變異：分兩群後落進第一群、allowance 8.64 ≥ 8.0）。
- 驗收：`uv run pytest tests/test_copy_safety_net_lagging_mirror.py tests/test_copy_positions_resting_net.py tests/test_copy_sync_positions.py tests/test_copy_loop_safety_net_fresh.py tests/test_copy_stale_snapshot_replay.py tests/test_copy_loop_trace.py tests/test_copy_loop.py -q` 全綠；全套 0 failed；主線程重跑 `probe_r4_sizetol.py`：`oversize_8.6_x5` 三輪皆 `market_open 2.0`／resting {}；重跑 `probe_r3_d13_edges.py`：partial_fill ⇒ 0.48、same_px_stack ⇒ 0.16、modify_plus_mkt_add ⇒ `[]`。

### T18 `@inline` 複審 5 修正：容差基準等價（D20）、flip 開倉腿扣法（D21）、dry size_tol 鎖定測試（S1）
- 檔案：`src/spark/copytrade/positions.py`（`resting_entry_qty_by_coin` 公式改 D20、docstring 更正「與 A 段等價」的推導；
  `sync_positions` flip 分支依 D21 改開倉腿）、測試。loop.py 不需改。
- **主線程裁決 2（2026-10-09）**：`tests/test_copy_positions_resting_net.py::test_flip_and_flatten_ignore_resting_orders` 斷言
  `market_open ETH` 出現，與 D21 直接衝突（D21 刻意反轉此舊行為）⇒ 允許最小修改：平倉腿／flatten 斷言不動、`market_open` 改為不應出現並斷言
  `covered_by_resting_orders`，更名並更新該檔註解「減倉／flip 平倉腿／flatten 不扣；flip 開倉腿依 D21 扣」。
- **主線程裁決（2026-10-09，builder 依升級協定回報：config 其實沒有驗 `size_tolerance` 範圍）**：T18 納入 `src/spark/copytrade/config.py`
  與 `tests/test_copy_config.py`——`__post_init__` 加 `0 <= size_tolerance < 1`（`0` 合法＝退回精確比較；`≥1` 會讓 D20 除以零或允許量為負，
  也讓 A 段 `_orders_match` 失去意義）並補三態測試（0 合法、0.08 合法、1 與 1.5 被拒）。啟動即拒（工程原則 #3），不在 B 段靜默夾值。
  正式機現值 0.08、filet 簽章設定上限 0.25，不受影響。
- 測試：(a) reviewer 探針 `probe_r5_basis.py`（5 層每層 8.69、leader 每層 2.0、scale 4、tol 0.08）⇒ resting {}、補 2.0；
  每層 8.70（A 段會改單）⇒ lag = 8.70 − 8.6957 ≈ 0.0043>0 被扣（與 A 段邊界一致；用 `_orders_match` 直接斷言兩側邊界同一點）；
  既有 D19 數值測試依新公式更新（partial_fill：lag = 8 − 1.5×4/0.92 = 1.4783 ⇒ 補 0.5217；same_px_stack：4 − 0.5×4/0.92 = 1.8261 ⇒ 補 0.1739；
  9.0 單張：9 − 8.6957 = 0.3043；逐一列出改動）；(b) D21：reviewer 探針 `probe_r5_flip.py`（leader −1.0→+1.0、我方 −4.0、鏡射買單 8.0@2000，
  leader 新鮮簿無該價）⇒ 平倉 4.0 照做、`market_open []`、skipped 含 `covered_by_resting_orders`、decision branch 同名、不記 adjusted；
  鏡射單 2.0 時 ⇒ 開倉淨量 2.0；無鏡射單 ⇒ 行為與改前相同（既有 flip 測試全綠）；`no_new_exposure` 時仍走 `cost_breaker_flip_open`（順序：先 cost_breaker 再扣法）；
  (c) S1：dry 路徑 `size_tol` 鎖定——dry 虛擬簿殘留單量在 1.0–1.087 倍區間 ⇒ 不扣；把 dry 呼叫的 `size_tol` 改 0 時該測試必須變紅（附失敗行、手動還原）。
- 驗收：`uv run pytest tests/test_copy_safety_net_lagging_mirror.py tests/test_copy_positions_resting_net.py tests/test_copy_sync_positions.py tests/test_copy_loop_safety_net_fresh.py tests/test_copy_stale_snapshot_replay.py tests/test_copy_loop_trace.py tests/test_copy_loop.py -q` 全綠；全套 0 failed；主線程重跑 `probe_r5_basis.py`（8.69 ⇒ 補 2.0）、`probe_r5_flip.py`（`market_open []`）、`probe_r4_sizetol.py`、`probe_ladder_underfollow.py`。

### T19 `@inline` 撤回「扣落後鏡射單」整段（D22）
- 目標狀態＝T13 之前的安全網邏輯 ＋ 保留 D12／D15／D17／size_tolerance 驗證／T8 decisions。對照基準：`git show f6146e0~1:src/spark/copytrade/positions.py`
  與 `git show f6146e0~1:src/spark/copytrade/loop.py`（T12 完成時的版本），在其上**只**疊加 T14（`full_close`）、T15 的 D15（`_try_close_reduce_only` try/except）、
  T16 的 D17（`_track_close_exceptions`＋`ReconcileState.close_exc_streak`＋`CopySettings.close_exc_crit_after`）、T18 的 `size_tolerance` 驗證。
- 檔案：`src/spark/copytrade/positions.py`（刪 `resting_entry_qty_by_coin`、`resting_entry_qty` 參數、`_adjust_gate`，open／increase／flip 分支恢復 T12 版本的形狀＋`full_close`＋D15；
  docstring 刪除 D11–D21 段落；`_dec` 的 `resting` 鍵移除；不再 import `orders._prices_equal`）、`src/spark/copytrade/loop.py`（`_safety_net` 恢復 T7／T8 形狀：live＝`fresh_my` 快照 → leader 守衛 → `_scale_for` → `sync_positions(decisions=…)` → `net.update(...)`，**不**讀我方掛單、**不**讀 leader 掛單、無 `resting_entry_qty` 鍵；dry 同 T8；保留 `_track_close_exceptions` 呼叫與 trace）、`tests/`（刪 `tests/test_copy_positions_resting_net.py`；`tests/test_copy_safety_net_lagging_mirror.py` 只保留 D15 測試 `test_close_reduce_only_exception_does_not_abort_rest_of_safety_net` 與 6 個 `test_d17_*`，搬到新檔 `tests/test_copy_safety_net_close_exc.py` 後刪原檔；`tests/test_copy_loop_safety_net_fresh.py` 刪 T13 加的 7 個測試（`test_w1_*` 6 個＋`test_dry_mode_reads_virtual_book_after_a_phase_zero_http`）與僅供它們使用的 fixture，T7 的 10 個測試不動）、`deploy/RUNBOOK.md` §5.8m 若提及扣法則刪（grep `resting`／`落後鏡射`），plan §8 加 W1 殘留風險（主線程做）。
- 驗收：(1) `grep -rn "resting_entry_qty\|covered_by_resting_orders\|_adjust_gate\|lagging_mirror" src scripts tests deploy` 無輸出；(2) `diff <(git show f6146e0~1:src/spark/copytrade/positions.py) src/spark/copytrade/positions.py` 的差異**只**有 `full_close`、D15 try/except（含 logger）、相關 docstring；`loop.py` 同法比對只剩 D17 helper 與其呼叫、`_finish_fresh_read_cycle` 的 S2 文案、`run_cycle` 的 `_track_close_exceptions` 呼叫（把 diff 貼在回報）；(3) 原 W1 探針 `probe_stale_orders.py` ⇒ `market_open [('ETH', True, 2.0)]`（接受的行為）；階梯探針 ⇒ 三輪開倉 4.0；`probe_r6_grid_e2e.py` ⇒ 不再拋 ValueError；(4) `uv run pytest tests/test_copy_loop_safety_net_fresh.py tests/test_copy_safety_net_close_exc.py tests/test_copy_sync_positions.py tests/test_copy_stale_snapshot_replay.py tests/test_copy_loop_trace.py tests/test_copy_loop.py tests/test_copy_config.py tests/test_full_close_idempotency.py -q` 全綠；全套 0 failed（測試數會比 3833 少，回報總數）；ruff 全過。

### 複審（主線程派工；完成定義見 §13）
每一輪修正完成後派 `reviewer`（opus）看該輪 diff＋對應裁決。T15 的重點：(1) D13 的「找不到對應價位」判定在
leader 改價（modify）、部分成交、`px_rel_tol` 邊界時是否會誤扣或漏扣；(2) D14 的順序是否真的讓所有競態都落在
fail-safe 側；(3) D15 接住例外後是否有任何去風險動作被靜默吞掉（failed 與 warn 是否必達）。

### 審查（主線程派工）
全部 task 完成後：主線程親跑 `uv run pytest -q` 與 `uv run ruff check src tests scripts`；再派 `reviewer`（opus）
只看 `git diff main...fix/fresh-leader-read-order-resilience`＋本 plan＋測試輸出。額外要求審查者逐條回答：
(1) D1 的「結果不明」是否仍可能造成重複下單；(2) 守衛是否有任何路徑把「讀不到」當成「歸零」；
(3) 新鮮 scale 與開頭 scale 並存是否產生新的混基準（工程原則 #1）；(4) 本 plan 是否有未覆蓋的呼叫點
（grep `get_positions(`／`get_account_state(` 的剩餘用法並逐一說明為何可保留）。
審查者的實跑證據一律由主線程自己重跑（judgment.md §2）。

## 6. 測試用的事故數字

- 開頭快照：leader ETH 0.0913、HYPE 0.53；`scale=5.18`（資金使用率 0.4、權重 1、自己權益 1090，
  反推 leader `account_value≈84.17`）。
- 安全網時：leader ETH 0.2463、HYPE 1.61；`scale=3.43`（leader `account_value≈127.1`）。
- 自己：ETH 1.1958、HYPE 8.61。新鮮目標 ETH≈0.8448、HYPE≈5.52 ⇒ 減倉 ETH≈0.351、HYPE≈3.09
  （依各幣 `sz_decimals` 捨入）。事故實際賣出：ETH 0.7226、HYPE 5.87。
- 消耗的單：ETH 買 0.1761@2635.3（自己的單，已成交）、期望 0.1596@2635.3；
  modify 拒因 `Cannot modify canceled or filled order`，cancel 拒因
  `Order was never placed, already canceled, or filled. asset=1`；
  post-only 拒因 `Post only order would have immediately matched, bbo was 2611.4@2612.8. asset=1`。

## 7. 部署前檢查（主線程；**需使用者另行授權**，本 plan 不含部署）

- 避開 **週六 UTC 08:30–09:20**（過去三個週六都有 502/504，回顧文件 §4）。
- 先跑「動正式機三查」：`ls /var/www/maintenance/ON`、`ls -d /tmp/spark-sync`、
  `systemctl show <unit> -p Wants -p Requires`；`systemctl --failed`；`df -h`（cycle_trace 單 follower 單日上限 16 MB × 14 天）。
- 用 §3.2 整包 rsync（`src/`、`scripts/`；無依賴變更，略過 `uv sync`），`DEPLOYED_VERSION` 寫明確 commit hash；
  備份舊檔；**一次只重啟一個 follower**，每個觀察 ≥3 分鐘（無 ERROR、`cycle_trace` 每分鐘一筆
  `kind="cycle"`、`consecutive_skips==0`、心跳 `result` 正常）再換下一個；`filet-api` 不需重啟。
- 驗收指紋：部署後第一個波動事件，用 `analyze_cascade.py fit` 檢查安全網的快照年齡應 <5 秒（修前約 28 秒）。

## 8. 殘留風險（誠實標註）

- `place_outcome`／`cancel` 拋例外仍會中止整輪；`cancel` 因 D2 會重試，`place` 不會。
- 波動權重不重抓，scale 的權重那一半仍可能落後最多一個 A 段的長度。
- 新讀取失敗時會跳過一輪校正（估計約每兩週一次、最多晚 1 分鐘）；連續 3 次警告、10 次 critical。
- 政策性降倉（W4）與撤單延遲的多持一格都沒有處理。
- **W1（原始版，D22 接受）**：leader 進場單在 A 段期間成交、我方同價鏡射單排隊落後時，B 段依新鮮 leader 部位市價補齊差額，鏡射單之後成交 ⇒ 超額
  至多一張鏡射單量、下一輪 A 段縿量／B 段減倉修回，代價一次 taker 來回。連環成交時最易發生（10-07 事故的反方向）。曾以 D11→D21 嘗試
  用「扣落後鏡射單」消除，六輪複審各帶出新問題後於 2026-10-10 撤回；另案規劃時需同時處理：價位對應、量、A 段容差基準、`szDecimals` 格點、flip 開倉腿、分群方式。
- backlog（複審 5 S2）：A 段送出的部分量 reduce-only **GTC 掛單**仍被 `ResilientExchange.order` 當冪等重送（`bool(reduce_only)`）；
  5xx 下若單已掛上會多一張減倉單，方向去風險、下一輪 A 段撤多餘單自修。要修需把 D12 的 `_idempotent` 宣告延伸到 `place_order`。
- 02:01:33 那一輪 BTC 目標對不齊的原因仍未解（回顧文件 §6.2），T8 的逐幣明細是為了下次能判定。
- 心跳 `result="skipped"` 不在 `publicapi/app.py` 的 `_HEALTHY_CYCLE_RESULTS` 內：被守衛跳過的那一輪，
  儀表板會顯示「訊號來源狀態未知」。**使用者 2026-10-09 裁決：本 plan 不改面板**（跳過＝當下讀值不可信，顯示未知是如實）；
  T12 在 RUNBOOK 記一行。

## 9. 基線紀錄（T0 填寫）

- `uv run pytest -q`（2026-10-09，main@7e4b509，分支 fix/fresh-leader-read-order-resilience）：**3556 passed, 42 deselected, 1 warning**（約 4 分鐘；本機 Python 3.14）
- `uv run ruff check src tests scripts`：**All checks passed!**

## 10. 待使用者確認

1. 本 plan 的任務切分、裁決表（D1–D10）與「不做」清單。——**已確認（2026-10-09）**
2. builder 在分支上**逐 task 本地 commit**（不 push；訊息格式 `fix:`／`test:`／`docs:` 一行敘述）。——**已確認**
3. 確認後才派工；派工與審查全程不碰正式機。——**已確認**；另：Haiku 只拿不需推理的 task，遇疑問立即回報主線程。

## 11. 進度紀錄

| task | commit | 主線程驗收 | 備註 |
|---|---|---|---|
| T0 | 91af76e（docs） | 基線 3556 passed | |
| T1 | 62650d1 | 16 tests、ruff 過 | |
| T2 | 6fdcb92 | 全套 3595 passed | |
| T3 | ea5c3cb＋f2a1e01 | 15 tests | 主線程修正：prune 只認 YYYYMMDD、測試改真實檔名 |
| T4 | 5f5330a＋5f8c541 | 27 tests | 主線程修正：第一次讀取拋例外也先重讀並 mark_skip |
| T5 | 59fc4d8 | 40 tests | |
| T6 | 2e7edaf | 全套 3655 passed | builder 依升級協定停下；主線程授權改 4 個既有測試 fixture（ntl=0→100000）＋讀取順序斷言；探針確認無空轉測試 |
| T7 | 2ba107a | 全套 3665 passed；重播斷言 ETH 0.351±0.001、HYPE 3.09±0.01 | builder 另加「同一輪內同一則 leader_flow 警示只發一次」（plan 外、僅影響重複警示），主線程接受 |
| T8 | 595da12 | 全套 3682 passed（主線程親跑）；85 個相關測試 | builder 加了 live／leader／新鮮版帳戶欄位與 safety_net_counts（超出欄位清單、純增量） |
| T9 | ed9fdcb | 全套 3723 passed（主線程親跑）；204 個相關測試 | 行為註記：modify 500 但未落地 ⇒ 舊單於 settle 被撤、下一輪才重掛（D1 安全方向，有測試鎖住） |
| T10 | 66097a4 | test_copy_loop 46 passed；全套併入 T11 後親跑 | |
| T11 | 43c3934 | 6 個重播測試；變異 M1–M5 皆紅（主線程親自重做 M2／M4／M5 確認） | 註記：macOS sed 的 `0,/re/` 不生效，第一次 M2 重跑是假陰性，改行號後確認 |
| 審查 | — | reviewer（opus）：0 Critical、W1／W2／S1／S2；主線程重跑 W1 探針、S1 變異、W2 路徑皆成立 | 使用者裁決 W1→B（D11／T13）、W2→B（D12／T14）；S1／S2／帳戶模式檢查由主線程直接補 |
| T12 | 452b6b7 | RUNBOOK §5.8m（3186 行起）＋回顧文件 §8 jq 範例 | builder 列出 6 處 plan 與原始碼不一致，文件以原始碼為準（見 §12） |
| S1／S2／清單 | 249909c | 74 tests；S1 變異重做轉紅 | 主線程自做 |
| T13 | f6146e0 | 70 個相關測試；reviewer 探針重跑 ⇒ `market_open []`；全套併入 T14 後親跑 | builder 三個判斷：`resting` 鍵只在非 None 時加、open 分支 $10 gate 只在有扣時套、gate 抽成共用函式——皆接受 |
| 複審 2 | — | reviewer（opus）：**Critical 1**（D11 對階梯掛單 leader 長期不跟）、W-A（B 段讀取順序 fail-open）、W-B（部分減倉例外中止整個 B 段）；主線程重跑兩支探針皆成立 | 使用者裁決：D13 精確扣法（選 B）、D14、D15 一併修 → T15 |
| T15 | 3521b3c | 53 個相關測試；全套 3787 passed（主線程親跑）；主線程重跑探針：階梯 ⇒ 三輪都開倉 4.0、W1（正確 fixture）⇒ `market_open []` | 原 W1／競態探針的 leader 掛單簿是靜態的（leader 單永遠在簿上）⇒ 在 D13 語意下等同「市價加倉＋階梯單在」該跟，非缺陷；builder 以正確 fixture 另寫測試鎖住 |
| 裁決 | — | 使用者 2026-10-09：選 A（修）；S2 門檻 5 輪；§13 複審停下門檻 3→5 輪 | → D16／D17／D18、T16 |
| 複審 4 | — | reviewer（opus）：**0 Critical**、W1（A 段 8% 容差 vs B 段精確比較 ⇒ 階梯 leader 每輪少補約 8%×階梯量、無聲；主線程重跑探針 `oversize_8.6_x5` 三輪 `market_open []` 成立）、W2（live `scale_fresh` 無測試鎖住，變異 M5 存活）、S（leader 側分群容差無測試 M2 存活；plan D16 一句不精確）；7/9 變異被抓到 | 主線程裁決 D19（套用同一個 size_tolerance）、T17 |
| T17 | c7624ba | 80 個相關測試；主線程重跑探針：`oversize_8.6_x5` 三輪 `market_open 2.0`／resting {}、partial_fill 0.48、same_px_stack 0.16、階梯三輪開倉、W1 `[]`；全套 3817 passed（主線程親跑） | builder 依升級協定停下：T16 兩個整合斷言在 D19 下必改 ⇒ 主線程裁決選項 1（更正斷言＋docstring 註明代價）；(e) fixture 改 12.0；兩個變異（scale_fresh→scale、_prices_equal→==）皆轉紅後手動還原 |
| 裁決 | — | 使用者 2026-10-09：選 A（修 W1＋W2＋S1，S2 列 backlog，第六輪複審） | → D20／D21、T18 |
| T18 | 26b49cc | 156 個相關測試；主線程重跑六支探針：8.64／8.69×5 層皆補 2.0、flip ⇒ `market_open []`／`covered_by_resting_orders`、sizetol 三輪補 2.0、階梯三輪開倉、W1 `[]`、partial_fill ≈1.478；全套 3833 passed（主線程親跑） | builder 兩次依升級協定停下：(1) config 未驗 size_tolerance ⇒ 納入 `0≤x<1` 驗證；(2) 舊測試 `test_flip_and_flatten_ignore_resting_orders` 與 D21 衝突 ⇒ 最小修改並更名 |
| 裁決 | — | 使用者 2026-10-10：**選 C**——撤回 D11–D21 整段、重新診斷與測試後由主線程重新規劃；懷疑 Sonnet 實作品質 ⇒ T19 以 opus 執行 | → D22、T19 |
| T19 | e952afa | 對照 T12 基準：positions.py 差異 32 行（只剩 full_close／D15／logger）、loop.py 29 行（只剩 D17 helper＋呼叫）；netting 相關 grep 0 命中；階梯探針三輪開倉 4.0、原 W1 探針補 2.0（接受）；69 個相關測試；全套 3764 passed（主線程親跑） | opus 執行；刪 2 個測試檔、D15／D17 測試搬到 `tests/test_copy_safety_net_close_exc.py`；commit trailer 沿用 Fable（builder 詢問，主線程維持） |
| 複審 7 | — | reviewer（opus）：撤回乾淨（兩檔 diff 只剩允許清單、fresh 測試檔逐位元相同、`_safety_net`／trace 與 T8 逐字相同）；**Critical 1**：D17 `_track_close_exceptions` 以 `net.get("skipped")` 判「整輪略過」，但正常路徑 `skipped` 是**清單**（min_notional／cost_breaker／protected 任一幣被跳過即非空）⇒ streak 凍結、永不升 critical（主線程重跑：有 skipped 清單 ⇒ streak {}／crits 0；無 ⇒ {ETH:7}／crits 3，成立）；W（§8 W1 上限應為 Σ落後鏡射單量；§8 漏列「我方快照無守衛 ⇒ 空讀值重複開倉」main 既有；RUNBOOK §5.8m 第 4 點過時且缺 close_exc 告警／`COPY_CLOSE_EXC_CRIT_AFTER`／size_tolerance 拒啟）；S（config 註解仍提 D20） | 2026-10-10 停下，主線程重新診斷後提新 plan |
| 複審 6 | — | reviewer（opus）：**Critical 1**（扣完的開倉／加倉淨量未依 `szDecimals` 捨入 ⇒ SDK `float_to_wire` 拋 ValueError ⇒ 整個 B 段中止、同輪其他幣的減倉／跟平延後並計入連敗；D16 埋下、D20 必現、D21 帶進 flip；主線程重跑 e2e 探針 `ValueError(…, 0.5217)`／`closes None` 成立）、W1（B 段分群以第一張代表價歸群 vs A 段逐張配對：leader 相鄰層相距 1–2 bp 且我方單落在中間 ⇒ 每輪少補、無聲；探針 lag 3.30 成立；fail-closed、條件窄）、S（cost_breaker 順序測試 fixture 鎖不住變異 iii；flip 開倉腿跳過時 trace 看不出平倉腿）；變異 4 個抓到 3 個 | 2026-10-10 停下請使用者裁決 |
| 複審 5 | — | reviewer（opus）：**0 Critical**、W1（D19 容差基準與 A 段不等價：A 段 `|d−m|/m ≤ tol` ⇒ 放行到 1.087d，B 段只放到 1.08d ⇒ 1.08–1.087 區間每輪少補 ≤0.7%×階梯量、無聲；主線程重跑探針 8.69×5 層 ⇒ 補 1.75 成立）、W2（flip 開倉腿不扣落後鏡射單 ⇒ 鏡射單再成交可達目標 3 倍；**main 上即存在**，非本分支退化；探針成立）、S1（dry 路徑 size_tol 無測試）、S2（A 段部分量 reduce-only GTC 掛單仍冪等重送，500 下可能多掛一張減倉單，方向去風險）；9 個變異抓到 8 個；整條分支資金安全掃視：每條路徑不比 main 差 | 依 §13 **第五輪仍有 Warning ⇒ 停下請使用者裁決**（2026-10-09） |
| T16 | 06d3a38 | 115 個相關測試；主線程重跑探針：partial_fill／same_px_stack／adjacent ⇒ `market_open []`、階梯 ⇒ 三輪開倉、W1 ⇒ `[]`；全套 3809 passed（主線程親跑） | builder 註記：D16 的 scale 誤差不是單向 fail-safe——scale_fresh 偏大 ⇒ lag 偏小 ⇒ 偏多補（上限 |Δscale|×leader 剩餘量）；dry 真實虛擬簿會先被 A 段撤單，殘留單情境靠覆寫模擬 |
| 複審 3 | — | reviewer（opus）：**0 Critical**、1 Warning（D13 只看「價位有沒有單」不看量：leader 單部分成交或同價多張時漏扣，W1 以縮小形狀重現，上限＝leader 已成交量×scale、一輪 taker 來回）、S1（dry 註解前提不成立＋缺反例測試）、S2（D15 後持續例外無升級路徑）；主線程重跑 edge 探針：partial_fill／same_px_stack 皆 `market_open 2.0` 成立 | 依 §13 第三輪仍有 Warning ⇒ **停下來請使用者裁決**（2026-10-09） |
| T14 | 88bb8a5 | 274 個相關測試；全套 3770 passed（主線程親跑，含 T13） | builder 依升級協定停下：kill switch flatten 打到 scripts/panic.py 的 executor 會 TypeError ⇒ 主線程裁決納入 T14（5abea9b）；10 個既有替身簽章最小修改（只加參數、不改斷言） |

## 13. 完成定義（2026-10-09 使用者裁決）

- reviewer 回報 **0 Critical** 才算修完。
- Warning 一律修，修完再複審；**連續五輪複審後仍有 Warning 存在 ⇒ 停下來向使用者確認狀況**，不再自行迭代
  （2026-10-09 使用者把門檻從三輪改為五輪）。
- 複審輪次紀錄：第 1 輪（整體）W1／W2／S1／S2 → 已修；第 2 輪（T13／T14）Critical 1＋W-A／W-B → T15 修；
  第 3 輪（T15）0 Critical＋W1（依量）＋S1／S2 → 使用者裁決修（T16）；第 4 輪（T16）0 Critical＋W1（size_tolerance
  兩套標準）＋W2（缺 scale_fresh 鎖定測試）＋2 Suggestion → 主線程裁決 D19、T17；第 5 輪（T17）0 Critical＋W1（容差
  基準 1.08 vs 1.087）＋W2（flip 開倉腿不扣，main 既有）＋S1／S2 ⇒ 五輪門檻到，停下請使用者裁決 ⇒ **使用者選 A**：
  D20／D21／S1 → T18；S2 列 backlog（§8）；第 6 輪＝T18 複審。

## 12. T12 發現的 plan 與實作差異（文件以原始碼為準，不回頭改程式）

- `fresh_read` trace 欄位沒有 `verdict` 鍵，實際為 `{reason, retried, consecutive_skips, consecutive_flat}`。
- `capital_utilization` 寫入的是設定值，不是實測使用率。
- 安全網被略過的輪次，`fresh_read.retried` 取開頭那次讀取的值。
- 16 MB 單日上限只擋 `kind="decision"` 明細；`kind="cycle"`（essential）超過仍寫。
- kill switch／成本熔斷 tripped 的輪次與中途拋例外的輪次不寫 trace；「每分鐘一筆」只在正常跟單輪成立。
- §7 的逐一重啟不得用 `deploy/reload_follower.sh`（間隔 3 秒），改為逐一 `systemctl restart` 並各觀察 ≥3 分鐘。
