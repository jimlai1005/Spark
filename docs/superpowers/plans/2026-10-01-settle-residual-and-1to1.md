# settle 驗證：部分成交殘單與 1:1 配對（F1／F2／F3）

日期：2026-10-01　狀態：已實作（C1–C15，C11 由 C13 取代），四輪 opus 審查＋2026-10-03 Fable 第二意見複核，未部署；backlog：W-chain、跨輪重鏡射
前置：`docs/superpowers/plans/2026-09-30-settle-retry-no-duplicate-fill.md`（commit 2c5e591，§4b 列出本 plan 的 F1–F3）

## 1. 問題（皆由 2c5e591 第二輪審查發現、主線程親跑 probe 重現；修改前即存在）

檔案：`src/spark/copytrade/orders.py`，函式 `_verify_diff`、`_actionable_missing`、`_reconcile_orders` 步驟 4。

- **F1 reduce-only 部分成交會多減**：部位 5000、ro 賣單 1000 在 settle 期間成交 600 → 殘單 400 變成同 slot extra →
  `_actionable_missing` 條款 2（ro＋同 slot extra）判可行動 → 撤殘單、重下 1000 → 實際減 1600。
  probe：`places=[1000 ro, 1000 ro] cancels=[3] crit=0`。
- **F2 相同 spec 一成一敗時失敗靜默**：`_verify_diff` 的 missing 用 `any(...)`，簿上一張單可同時滿足兩張相同 desired →
  失敗那張不算 missing → 不重試、不 CRIT（違反工程原則 #3）。probe：`places=[ok, fail] crit=0 sync_failed=False`。
- **F3 第二個 settle 窗內部分成交的非 ro 單被當 extra**：發出誤導 CRIT「多餘 1」，且殘單不撤。
  probe：`crit=1 多餘 1［oid=1 PUMP S 300］`。第一輪亦有同形：部分成交殘單被當 extra 撤掉，剩餘鏡射量白白消失 60 秒。

## 2. 根因分析與關鍵前提

F1、F3 同一個根因：**驗證把「部分成交後剩下的殘單」當成形狀錯誤的單**（extra），於是撤掉、重下或告警。
殘單其實就是「desired 已被消耗一部分後的剩餘」，正確處理是保留它、兩邊都不動。

關鍵前提（決定 F1 能怎麼修）：2026-07-28 commit 40d00e7 起，`_build_desired` 已把每幣 ro 期望量封頂在本輪部位快照。
交易所修剪只在「實際部位 < 掛單量」時發生；同一輪內部位只會因**我方掛單成交**（或強平）而縮小。
所以封頂之後，同一輪內出現「ro 單剩餘量 < desired」幾乎必然是部分成交，不是 7/28 那種部位落後的修剪。
2c5e591 的條款 2（ro＋同 slot extra 就重下）保護的情境已不存在，留下的只有 F1 的副作用。

F2 是 `_verify_diff` 的配對語意錯誤：`_plan` 步驟 1 已是 1:1 配對（`used` 集合），`_verify_diff` 卻不是，兩者不同源。

## 3. 裁決（主線程，實作不得偏離）

- **C1（F2）`_verify_diff` 改為 1:1 配對**：與 `_plan` 步驟 1 同形——依 desired 順序，各自取第一張尚未被用掉、且
  `_orders_match` 為真的 after 單；用掉即標記。missing＝沒配到的 desired；extra＝沒被用掉的 after。
  簽名與回傳型別不變。
- **C2（F1、F3）殘單配對**：新增私有純函式
  ```python
  def _pair_residuals(
      missing: list[OrderSpec], extra: list[OpenOrder], *, px_rel_tol: Decimal,
  ) -> tuple[list[OrderSpec], list[OpenOrder]]:
  ```
  1:1 配對：extra 單 E 與 missing 單 d 滿足「`_slot_key` 相同、皆非 trigger、`_prices_equal(d.limit_px, E.limit_px, px_rel_tol)`、
  `E.sz < d.sz`」即為殘單對，兩者都從清單移除。回傳（剩下的 missing, 剩下的 extra）。多張可配時依清單順序貪婪配對。
- **C3 可行動判準收斂**：`_actionable_missing` 只保留「failed 多重集合預算」一條；**刪除條款 2**（ro＋同 slot extra）。
  兩輪驗證的處理順序固定為：
  1. `_verify_diff`（1:1）得 missing、extra；
  2. 先取可行動 missing＝依 failed 預算放行（不變）；
  3. 對**非可行動**的 missing 與 extra 跑 `_pair_residuals`，配到的殘單對兩邊都丟棄（不撤、不補、不 CRIT）；
  4. 非可行動且未配對的 missing 丟棄（已消耗，D2 不通知，同 2c5e591）；
  5. 剩下的 extra 照舊撤單、照舊入 CRIT；可行動 missing 照舊補下、重試仍失敗入 CRIT。
  可行動 missing 優先於殘單配對：本輪下單失敗的 spec，即使簿上有同價較小的舊單，仍要補下，舊單照 extra 撤掉。
- **C4 修剪診斷移除**：C2 之後「ro missing＋同價較小 ro extra」不會再進 CRIT，`_diagnose_trimmed_coins` 與 CRIT 的
  「診斷：… reduce-only 掛單已被交易所修剪…」那一行成為死碼——一併刪除，連同只測該函式的單元測試。
  **行為改變（要在回報中明說）**：7/28 那類「修剪形狀」從「撤單＋重下＋CRIT（附診斷）」變成「保留殘單、不告警」。
  依據是第 2 節的前提；跨輪由下一輪依新部位重算。
- **不變**：extra 的撤單語意、CRIT 格式（逐單價量、部位行、補單拒因、截斷、dedup_key）、D2、`PlaceOutcome`。

### 審查後裁決（主線程，2026-10-01，三項皆親跑 reviewer probe 重現）

- **C5 殘單必須核對原始下單量**：C2 只看「同價、較小」太寬，會吞掉該修的單：
  Q1 leader 同價加量 600→1000、modify 回 True 但簿上仍 600 → 新版 crit=0（HEAD crit=1）；
  Q2 cancel-place 撤舊 600 回 False 仍在、新 1000 全成交 → 新版保留舊 600、crit=0；
  Q3 同價兩張 1000/500 都部分成交、簿順序 [100,900] → 900 被撤。
  `_pair_residuals` 的配對條件改為（全部成立）：同 `_slot_key`、皆非 trigger、價格相等（px_rel_tol）、
  `E.orig_sz is not None`、`abs(E.orig_sz - d.sz) <= size_tol * d.sz`、`E.sz < d.sz`。
  `_pair_residuals` 簽名加 `size_tol: Decimal` 關鍵字參數。
  - `orig_sz` 來源：adapter 由 frontendOpenOrders 的 `origSz` 填入（`src/spark/exchange/hyperliquid.py` 約 174 行）；
    HL historicalOrders 實測部分／全部成交後 `origSz` 保留原值。
  - `orig_sz is None` → 不視為殘單，照 extra 撤＋入 CRIT（大聲的保守退路）。
  - 未實測：HL modify 後 `origSz` 是否更新為新量。若不更新，被 modify 過又部分成交的單不算殘單、被當 extra 撤掉；
    撤單生效時第二輪已不在簿上，所以實際是「撤、不補、不 CRIT」（下一輪補回），不會重複成交。（2026-10-01 第三輪審查 W3 更正原「撤＋CRIT」敘述）
- **C6 補測試**（reviewer Critical：拿掉 `E.sz < d.sz` 全套仍綠）：
  T-larger（同價、較大 1500 vs desired 1000 → 撤＋第二輪仍在則 CRIT）、T-Q1、T-Q2、T-Q3（簿順序 [100,900] 與 [900,100] 兩種皆 0 撤單）、
  T-orig-none（同價較小但 `orig_sz=None` → 撤＋CRIT）。既有殘單測試的 fixture 補上 `orig_sz=desired sz`。
- **C7** 清掉 `_reconcile_orders` 中仍提到「自動診斷」的過時註解。

### 第二輪審查後裁決（主線程，2026-10-01，W1、W2 親跑 probe 重現）

- **C8 本輪動過的 oid 不得當殘單**（W1，本輪新引入）：上一輪殘單 600（orig 1000）在本輪 cancel-place 中撤單回 False 但仍在，
  新 1000 於 settle 全成交 → 舊殘單 orig 等於 d.sz 被配成殘單吞掉、0 CRIT（probe：`[cancel 1, place 1000], crit=0`）。
  `_reconcile_orders` 收集 `touched_oids`＝本輪嘗試過 cancel 或 modify 的所有 oid（含 fallback 舊 oid、`plan.to_cancel`、
  modify 的舊 oid，不論成敗），傳入 `_settle_verify`／`_pair_residuals`（新增關鍵字參數 `exclude_oids: frozenset[int]`）；
  oid 在其中的 extra 一律不當殘單。matched（本輪未動）的既有單仍可當殘單（F3 情境）。
- **C9 `_verify_diff` 兩段配對**（W2）：desired 同價兩張、量差在 size_tol 內（b=1050 排在 a=1000 前、a 成功 b 失敗）時，
  b 搶走 a 的單 → a 被當已消耗、b 的失敗靜默（probe：順序 [b,a] crit=0，[a,b] crit=1）。
  改為兩段：第一段只配「`_orders_match` 為真且 `sz` 完全相等」；第二段才用容忍度配剩下的。仍為 1:1。
- **C10 測試敘述對齊實盤**（W3）：Q1／Q2／orig-none 測試的 FakeExecutor `cancel` 回 True 但簿上仍在，等同「撤單謊報成功」；
  測試名稱與 docstring 不得暗示實盤會 CRIT。改名或改 docstring 寫明此前提，並新增一支「撤單真的生效」版本：
  簿上不符的單被撤、desired 不補（非可行動）、無 CRIT——寫明這是接受的行為：下一輪（約 60 秒）由 `_plan` 重算補回。
### 第三輪審查後裁決（主線程，2026-10-01；審查結論可 commit、無 Critical）

- **C11 失敗預算改以「同 slot 同價」群組計**（W1，F2 的延伸，HEAD 既有）：同價兩張量差在 size_tol 內的 desired
  （例 b=1050、a=1000），一成一敗時第二段容忍度配對可能把成功那張的單配給失敗那張，於是 missing 的是成功那張、
  `failed[spec]` 預算對不上 → 失敗靜默。`_plan` 步驟 1 與 `_verify_diff` 對近似量單的配對誰配誰本來就不唯一，
  所以預算不能綁單一 spec 值。改為：`failed` 的鍵＝`(_slot_key(d), d.limit_px, d.trigger_px)`；place 失敗 +1、
  settle 重試成功 −1（同鍵）；`_actionable_missing` 對每個 missing 依其鍵扣預算放行。補下的是 missing 那張 spec
  （量與失敗那張在 size_tol 內，鏡射總量等價）。
  測試：(a) desired [b,a]、a 成功後小量成交到 990、b 失敗 → 重試 1 次、重試仍敗則 CRIT；
  (b) 簿上已有 A=1000、desired [b=1050, a=1000]、補下 a 失敗 → 重試 1 次、重試仍敗則 CRIT。兩支在目前程式上須為紅。
- **C13 撤回 C11 的純群組鍵（主線程自查發現，已親跑重現）**：同價 a=1000、b=50（量差遠超 size_tol），
  b place 失敗、a 成功後全額成交 → missing=[a,b]、群組預算 1 → `_actionable_missing` 放行 **a**（已成交的 1000）＝重複成交，
  b 的失敗反而沒補。群組鍵只在量差於 size_tol 內才等價，量差大時是錯的。改為：
  - `failed` 回到 `Counter[OrderSpec]`（spec 值）。
  - `_actionable_missing` 回傳 `list[tuple[OrderSpec, OrderSpec]]`＝（要補的 missing, 對應消耗的 failed spec），1:1 兩段：
    第一段 missing 與 failed **spec 值完全相等**者配對；第二段剩下的 missing 只與「同 `_failed_key`、且
    `abs(m.sz - f.sz) <= size_tol * f.sz`」的剩餘 failed 配對。配不到的 missing 一律非可行動。
  - settle 重試補下 missing 成功時，扣減它所配對的那個 failed spec；失敗則不扣。
  - `_settle_verify` 與 CRIT 處只取配對的第一個元素作為可行動 missing。
  - 測試：T-size-gap（上述 1000/50 情境）→ 只重試 50、絕不重下 1000；重試仍敗則 CRIT 含拒因。C11 (a)(b) 兩支保留且須仍綠。
    變異「第二段拿掉 size_tol 條件」須讓 T-size-gap 轉紅。
- **C14 `_verify_diff` 排除本輪動過的 oid**（第四輪審查 W1，本次新引入的重複成交路徑）：撤單回 False 仍在簿上的舊單 Z，
  在 settle 中部分成交到恰等於失敗的 b → 被 b 在第一段配走 → 已全額成交的 a 經第二段吃到 b 的預算而被重下
  （probe：`places=['1040','1000','1000'] cancels=[9] crit=0`）。本輪嘗試過撤銷或改單的 oid 本來就不該存在，
  不得滿足任何 desired：`_verify_diff` 新增關鍵字參數 `exclude_oids: frozenset[int] = frozenset()`，這些 oid 的單
  兩段配對都跳過、一律進 extra。`_settle_verify` 把 `touched` 同時傳給 `_verify_diff`。
  測試：上述 Z 情境 → a 不被重下；Z 照 extra 撤。變異「拿掉 `_verify_diff` 的 exclude」須讓它轉紅。
  **docstring 補正**（審查 Suggestion 2）：`_reconcile_orders` docstring 的殘單條件補上 orig_sz 核對與 exclude_oids。
- **C15 `touched_oids` 只收「本輪嘗試撤銷」的 oid**（2026-10-03 主線程第二意見複核，Fable；親跑 probe）：
  現行 `touched_oids` 把 `plan.modifies` 全部舊 oid 都算進去，等於依賴「HL modify 成功後必換新 oid」這個假設
  （testnet 2026-07-19 實測成立、主網 f438 歷史一致）。但若假設不成立（HL 原 oid 就地改量），每一張 modify 成功的單
  都會被 `_verify_diff` 排除 → 當 extra 撤掉 → desired 被當已消耗不補 → 每輪靜默少掛一輪。
  probe（FakeExecutor，modify 回 True、簿上同 oid 已是新量）：`[modify 1, cancel PUMP] crit=1`；真實 HL（換 oid）：`[modify 1] crit=0`。
  改法：`touched_oids` ＝ fallback 舊 oid（含 modify 失敗、TTL 內、cancel-place 政策三種來源）∪ `plan.to_cancel`；
  **modify 回 True 的 oid 不加入**。理由：modify 成功＝這張單已是 desired 形狀，若 oid 未換它會被正常配對，若換了舊 oid 本就不在簿上，
  排除它沒有任何保護價值。C8 要擋的「撤單回 False 仍在簿上」情境全部來自 fallback／to_cancel，不受影響。
  測試：H1 情境（modify 回 True、簿上同 oid 新量）→ 0 撤單、0 CRIT；既有 `test_modify_lies_success_size_unchanged_is_loud_not_residual`
  與 `test_stale_residual_touched_by_failed_cancel_is_not_swallowed` 須仍綠；變異「把 modify 成功 oid 加回 touched」須讓 H1 測試轉紅。
- **backlog（不修）W-chain**：同價 f=1050 失敗、o=1150 成功後部分成交到 1000 → f 用容忍度配走 o 的單、o 的量差超過 size_tol
  配不到預算 → 少掛約 1050 一輪（約 60 秒）且無告警。HEAD 相同行為；不會重複成交。
- **C12 T-priority 測試改為真正釘住優先序**（W2）：舊單 `orig_sz` 設為等於 d.sz（同價較小、原量相符的殘單形狀），
  斷言 place 失敗的 spec 仍補下 1 次、舊單被撤。變異「先配殘單、再算可行動」須讓它轉紅。
- **W3 不修**：前提是 HL modify 保留原 oid，但 `src/spark/exchange/hyperliquid.py` modify_order 註解記錄 2026-07-19 testnet 實測
  modify 會重新配發 oid，舊 oid 消失、新 oid 不在 `touched_oids`，故 modify 成功的單仍可當殘單。plan C5 敘述已更正。

- **backlog（不修）**：跨輪重鏡射——殘單在下一輪會被 `_plan` modify 回 desired（leader 未成交時），ro 只有部位封頂保護。
  這是移植自 hl-copytrader 的「鏡射 leader 剩餘掛單」設計，需另案討論。

## 4. 範圍

改動限於 `src/spark/copytrade/orders.py`（`_verify_diff`、`_actionable_missing`、新增 `_pair_residuals`、`_reconcile_orders` 步驟 4 與 docstring、
刪除 `_diagnose_trimmed_coins`）與 `tests/` 下相關測試。不得動 executor.py、`src/spark/exchange/`、positions.py、loop.py、部署檔、`.env*`、
`/Users/jim/projects/hl-copytrader`。不 commit（主線程驗收後 commit）。

既有測試改動規則：
- 測試前提與新語意直接衝突者（主要是 7/28 修剪形狀期待 CRIT／重下的測試、2c5e591 R4 中依賴條款 2 的測試、
  `test_settle_missing_one_replaces_and_second_fetch_clean`）**准許改**：改成新語意下的正確期待，或改用「place 失敗」表達可行動。
  斷言的意圖（例如「重試後收斂不告警」「CRIT 帶拒因」）要保留；計數類斷言（如 `placed == N`）可依新語意調整數值，
  但每一條調整要在回報 (d) 逐條列出「原值 → 新值、為什麼」。
- 只測 `_diagnose_trimmed_coins` 的測試隨函式刪除，回報列出刪了哪些。
- 不得為了變綠而刪除其他測試或放寬非計數斷言。

## 5. Tasks

### Task A @inline — C1：`_verify_diff` 1:1 配對（F2）

1. 先寫測試（`tests/test_copy_orders_settle_no_dup.py`）：
   - **T-F2a**：desired 兩張完全相同的 spec；步驟 3 第一張 ok、第二張 fail；settle 後簿上只有一張。
     斷言：補下 1 次；若補下仍失敗 → CRIT 含拒因、`sync_failed=True`。
   - **T-F2b**：`_verify_diff` 單元測試：desired [a, a]、after [a] → missing 長度 1、extra 長度 0；
     desired [a]、after [a, a] → missing 0、extra 1。
   在未改程式上確認 T-F2a、T-F2b 紅，保留失敗輸出末尾。
2. 實作 C1。
驗收：`uv run pytest tests/test_copy_orders_settle_no_dup.py tests/test_copy_orders_reconcile.py tests/test_copy_orders_crit_alert.py -q` 全綠。

### Task B @inline — C2／C3／C4：殘單配對與條款 2 移除（F1、F3）

1. 先寫測試（同檔）：
   - **T-F1**：ro 賣 1000、部位 5000；步驟 3 place ok；settle 後簿上同價 ro 殘單 400。斷言：0 次重下、0 次撤單、無 CRIT。
   - **T-F3**：第一輪因另一幣 place 失敗而進重試；第二次重抓時某張已掛的非 ro 單變成同價殘單 300。斷言：該殘單不被撤、CRIT 不列它為多餘
     （另一幣若重試仍失敗，CRIT 只含那一幣）。
   - **T-F3b**：第一輪即出現非 ro 同價殘單（無任何失敗）→ 0 撤單、0 重下、不跑第二輪（`get_open_orders` 1 次、sleep 1 次）、無 CRIT。
   - **T-trim-new**：7/28 形狀（ro desired 1000、簿上同價 ro 600、部位 600）→ 保留、無 CRIT、無撤單。
   - **T-priority**：步驟 3 place 失敗的 spec，簿上另有同價較小的舊單 → 仍補下 1 次、舊單被撤。
   - **T-price-diff**：同 slot 但價格不同的較小單 → 不算殘單，照 extra 撤、入 CRIT（若第二輪仍在）。
   在未改程式上確認 T-F1、T-F3、T-F3b、T-trim-new 紅。
2. 實作 C2、C3、C4；更新 `_reconcile_orders` 與 `_actionable_missing` docstring（寫明 F1–F3、第 2 節前提、C4 行為改變、日期 2026-10-01）。
3. 處理衝突的既有測試（第 4 節規則）。
驗收：
```
uv run pytest -q
uv run ruff check src tests scripts
```
全綠；`grep -n "_diagnose_trimmed_coins" -r src tests` 無結果。

## 6. 回報格式（給 builder）

(a) 一段話結論與取捨；(b) 改動檔案:行號範圍；(c) 每條驗收的指令與輸出末尾，含修前紅的輸出；
(d) 改動或刪除的既有測試逐條列出（函式名、改了什麼、計數類斷言原值→新值與理由）；(e) plan 沒涵蓋而你做了判斷的地方。
