# settle 重試不得重下已成交的鏡射單（D1／D2／D3）

日期：2026-09-30　狀態：已實作並通過兩輪審查；未部署（使用者已裁決 D1 廣義、D2 不通知、D3 一併做）

## 1. 事故與根因（證據已由主線程以 HL 公開 API 取得）

follower f438（0x438b3bbc83b44c483f6c6c8d2d5664453791dce9，主網真錢）兩則 CRIT「掛單重試後仍不符」：

| 事件 | 第一張（步驟 3 place） | 第二張（步驟 4 settle 重試 place） |
|---|---|---|
| 09-29 16:16 PUMP S 168927@0.00576 | oid 560330169083，+733ms 全額成交，`crossed=False`（maker） | oid 560330204326，+2659ms 下出，同毫秒全額成交，`crossed=True`（taker） |
| 09-30 14:49 ENA B 2319@0.2652 | oid 561369641265，+300ms 成交，`crossed=False` | oid 561369687236，+2717ms 下出，同毫秒成交，`crossed=True` |

根因：`src/spark/copytrade/orders.py` `_reconcile_orders` 步驟 4（約 664-746 行）。`_verify_diff` 的 `missing`
只看「desired 單此刻在不在我方簿上」，不區分「沒下成」與「下成了但已成交」。settle 期間（`settle_seconds=2`）
成交的鏡射單被當成沒下成而**重下一次**＝非冪等寫入盲重試（全域工程原則 #2），造成重複成交，再發一則誤導的 CRIT。
重下的單 `reduce_only=False`：follower 部位接近零時，重複的賣單會開出非預期空單。

## 2. 裁決（實作不得偏離）

- **D1 廣義**：settle 驗證中，一張 missing desired spec 只有在「可行動」時才補下、才算入 CRIT。
  可行動判準（單一謂詞，兩輪驗證共用）：
  1. 本輪對這張 spec 最近一次 place 嘗試回傳失敗（`ok=False`，含 trigger skip），**或**
  2. `after`（該輪重抓的我方掛單）中存在同 slot（`_slot_key` 相同：coin／方向／reduce_only／trigger／tpsl）的 extra 單。
  其餘 missing（本輪 place 成功後消失、matched 或 modify 成功後消失、從未嘗試）一律視為「已被消耗」
  （成交或外部撤銷）：**不補、不 CRIT**，交給下一輪（60 秒）依 leader 最新狀態重算。
  - 條款 2 是主線程裁決（非使用者原文）：保留 2026-07-28 reduce-only 修剪修法的既有語意——
    同 slot 有 extra 代表簿上有「形狀不符」的單，照舊 cancel extra＋補 missing，修剪診斷行照舊。
  - extra 的處理完全不變（照舊撤、照舊入 CRIT）。
- **D2**：被判為「已消耗」的 missing 不發任何通知（不 warn、不 critical）。
- **D3**：place 的成功形態不再丟棄。新增 `PlaceOutcome`，`_reconcile_orders` 所有 place 改走它；
  resting／filled 形態寫進 `ActionRecord` payload 作為稽核證據。決策謂詞只用 `ok`，不依賴 filled 形態
  （filled 形態是正面證據與稽核用途）。
- CRIT「原因未能自動判定」那行的排查提示改為同時指向成交紀錄：
  `原因未能自動判定，排查：POST /info historicalOrders 查被拒單與修剪；userFillsByTime 的 crossed 分辨 maker/taker 成交`。

## 3. 範圍

改動限於：
- `src/spark/copytrade/executor.py`
- `src/spark/copytrade/orders.py`（僅 `_reconcile_orders` 與其 docstring、必要的私有 helper）
- `tests/` 下與 place／_reconcile_orders 相關的測試與假件

不得動：`src/spark/exchange/`（adapter 已回傳 `OrderResult`，不需改）、`positions.py`、`loop.py`、
部署檔、任何 `.env*`、`/Users/jim/projects/hl-copytrader`（唯讀紅線）。
工作樹有其他 session 的未提交改動（`src/spark/publicapi/`、`web/`、若干 tests），**不得碰、不得 commit 它們**。

## 4. Tasks

### Task 1 @inline — `PlaceOutcome` 與 `ExecutorPort.place_outcome`

檔案：`src/spark/copytrade/executor.py`

1. 新增 frozen dataclass：
   ```python
   @dataclass(frozen=True)
   class PlaceOutcome:
       ok: bool
       reason: str          # 失敗原因；成功為 ""
       status: str          # "resting" | "filled" | "unknown" | "rejected"
       filled_sz: Decimal   # 下單當下即成交量；非 filled 為 Decimal("0")
   ```
   status 對照 adapter `_parse_order_response`（`src/spark/exchange/hyperliquid.py` 約 430-448 行）：
   `ok=False` → `"rejected"`；`filled_size>0` → `"filled"`；`raw` 含 `_resilience=="verified"` → `"unknown"`；
   其餘成功 → `"resting"`。dry（live=False，虛擬簿）→ `"resting"`。trigger skip → `ok=False, status="rejected"`，
   reason 維持現有文字。
2. `ExecutorPort` Protocol 新增 `place_outcome(self, spec) -> PlaceOutcome`。
3. `ActionExecutor.place_outcome` 為唯一實作本體；`place_with_reason` 改為 `o = self.place_outcome(spec); return o.ok, o.reason`，
   `place` 維持回 bool。**不得重複記 ActionRecord**（一次 place 只記一筆）。
4. `ActionRecord` 的 place payload 新增 `"status"` 與 `"filled_sz"`（str），既有欄位不變。

驗收：
```
uv run pytest tests/test_copy_executor.py tests/test_copy_orders_crit_alert.py -q
```
全綠，且新增測試至少涵蓋：resting、filled（filled_sz 正確）、rejected（reason 萃取不退化）、
verified 短路 → unknown、dry → resting、trigger → rejected、一次 place 只記一筆 record 且帶 status。

### Task 2 @inline — `_reconcile_orders` 可行動判準（D1／D2）

檔案：`src/spark/copytrade/orders.py`、相關測試假件

1. 步驟 3 與步驟 4 的所有 place 改呼叫 `ex.place_outcome(spec)`；步驟 3 的 `placed` 計數語意不變。
2. 維護本輪「最近一次 place 失敗」的 spec 集合（以 spec 值相等判定；OrderSpec 為 frozen dataclass 可 hash）。
   步驟 4 重試成功則自集合移除，失敗則保留。
3. 實作單一私有謂詞（名稱自訂，例如 `_actionable_missing(missing, after, failed) -> list[OrderSpec]`），
   依第 2 節 D1 判準過濾；**兩輪驗證都只對謂詞結果動作**：
   - 第一輪：extra 照舊全撤；只補可行動 missing。可行動 missing 與 extra 皆為空 → 不重試、不 sleep 第二輪、不 CRIT。
   - 第二輪：CRIT 的 missing 清單只列可行動 missing（重抓後重新套謂詞）；extra 照舊。兩者皆空 → 不 CRIT。
4. `sync_failed` 只在第二輪仍有可行動 missing 或 extra 時為 True。
5. 更新 `_reconcile_orders` docstring 步驟 4 的描述，寫明本事故（2026-09-30，兩個 oid 對）與謂詞，並註明違反的是工程原則 #2。
6. CRIT「原因未能自動判定」那行改為第 2 節的新文字。
7. 所有被傳進 `_reconcile_orders`／`sync_open_orders` 的測試假 executor 補上 `place_outcome`（grep `def place_with_reason` 與 `def place(` 找全）。

新增測試（`tests/test_copy_orders_reconcile.py` 或 `tests/test_copy_orders_crit_alert.py`，用假 executor＋可程式化的 `get_open_orders` 序列，全離線）：

- **T-PUMP 重現**：desired 一張非 ro 賣單；步驟 3 place 成功；settle 後 `get_open_orders` 回空（已成交）。
  斷言：place 只被呼叫 **1 次**、notifier 無 critical、`sync_failed=False`。（修前此測試必須紅：place 2 次＋CRIT。）
- **T-ENA 重現**：同上但為買單。
- **T-matched 消失**：desired 單在 `my_orders` 已存在（matched，本輪未 place）；settle 後消失。斷言：0 次 place、無 CRIT。
- **T-place 失敗仍補**：步驟 3 place 回 `ok=False`；settle 後缺。斷言：步驟 4 補下 1 次；若補下仍失敗 → CRIT 含拒因（既有行為）。
- **T-trigger 仍大聲**：trigger desired → place 回 rejected → 兩輪後 CRIT（executor 模組 docstring 的刻意大聲失敗保留）。
- **T-修剪保留**：既有 reduce-only 修剪 CRIT 測試（`_diagnose_trimmed_coins` 相關）一條不改、全綠。
- **T-extra 不變**：只有 extra 無 missing → 照舊撤單＋CRIT。

驗收：
```
uv run pytest -q
uv run ruff check src tests scripts
```
全綠；並附「T-PUMP 在修改前的程式上會失敗」的證據（先寫測試跑紅，貼失敗輸出末尾，再實作）。

### 裁決記錄（主線程，2026-09-30 實作中）

- **R1 既有 8 條測試**：`tests/test_copy_orders_crit_alert.py` 六條（crit_position_line_shows_no_position、
  non_trim_shape_without_reasons_falls_back_to_investigation_hint、crit_message_bounded_with_many_mismatches、
  crit_message_hard_truncated_at_safe_length、dedup_key_stable_when_missing_size_drifts、
  sync_open_orders_passes_positions_into_crit）與 `tests/test_copy_orders_reconcile.py` 兩條
  （settle_missing_one_replaces_and_second_fetch_clean、settle_still_missing_after_retry_sets_sync_failed_and_criticals）
  的前提是「place 成功、settle 後消失仍應重下／CRIT」，正是 D1 要移除的行為。**准許修改**：把情境改為
  place 回 `ok=False`（讓 missing 可行動），各測試的斷言意圖（CRIT 格式、截斷、dedup、部位行、重試後乾淨）保持不變；
  不得刪測試、不得放寬斷言。`non_trim_shape_without_reasons_falls_back_to_investigation_hint` 若因 place 失敗
  而改走「補單失敗」分支，改用「同 slot 有 extra」情境讓它仍命中新提示文字。

- **R2 條款 2 只適用 reduce-only（審查 Critical，主線程已親跑 probe 重現）**：非 ro 單在 settle 期間部分成交時，
  殘單變成同 slot extra，舊條款 2 讓 desired 可行動 → 撤殘單再重下全量＝已成交部分重複執行
  （probe：desired PUMP S 1000、殘 400 → places=[1000,1000]；同 slot 兩張時 A 全成交也被連帶重下）。
  修剪只發生在 ro 單，且 ro 重下受交易所部位封頂、不會超額，所以條款 2 收斂為：
  `d.reduce_only and not d.is_trigger and 同 slot 有 extra`。非 ro 的 extra 處理不變（照舊撤、照舊入 CRIT）。
- **R3 `failed` 改為多重集合（審查 Warning）**：值相同的兩張 desired 會互相覆寫（先敗後成 → 真失敗靜默；
  先成後敗 → 兩張都重下）。改用 `collections.Counter[OrderSpec]`：每次 place 失敗 +1，place 成功**不**扣減；
  settle 重試某張可行動 spec 成功時 −1（降到 0 移除），失敗維持。謂詞對每個 spec 值最多放行
  `min(missing 中該值出現次數, failed[該值])` 張，ro 同 slot extra 條款另計、不受此上限。
- **R4 測試補強**：新增 (a) 非 ro 部分成交 → 不重下、殘單照撤、無 CRIT；(b) 同 slot 兩張非 ro、A 全成交 B 部分成交 → 0 次重下；
  (c) 兩張相同 spec 先敗後成 → 重試 1 次、重試仍敗則 CRIT 含拒因；(d) 先成後敗 → 只重試 1 次；
  (e) T-PUMP／T-ENA 補斷言 `get_open_orders` 呼叫 1 次、sleep 1 次（可行動為空時不跑第二輪）。
- **範圍外觀察（不修，記錄備查）**：跨輪行為——follower 鏡射單先成交而 leader 那張仍掛著時，下一輪 desired 仍含它、
  會再下一次；部位由安全網對齊 leader×scale。這是移植自 hl-copytrader 的既有鏡射語意，非本 plan 範圍。
  D2 使交易所主動撤單（例如 ro 單因部位歸零被撤）不再 CRIT，由下一輪重算處理——使用者已裁決的取捨。

## 4b. 實作結果（2026-09-30）

Task 1、Task 2、R1–R4 完成，未部署。主線程親跑：`uv run pytest -q` 3484 passed；ruff 乾淨；
第一輪 reviewer probe 三情境修後皆不重下已成交單。第二輪 fresh reviewer（opus）PASS，六個變異皆被測試抓到。

**複審留下的三條既有問題（修改前即存在，主線程親跑 probe 重現；不在 D1–D3 範圍，待使用者排序）**：
- **F1 ro 部分成交仍會多減**：部位 5000、ro 單 1000 在 settle 期間成交 600 → 殘單 400 被當修剪形狀 → 撤殘單、重下 1000 → 實際減 1600。
  部位封頂只保證不翻向，不保證不多減。是 R2 保留 7/28 修剪修法的代價；根治需分辨「被修剪」與「部分成交」（剩餘量與部位比對，或查 fills）。
- **F2 相同 spec 一成一敗時失敗靜默**：`_verify_diff` 不做 1:1 配對，簿上一張可同時對上兩張相同 desired → 失敗那張不算 missing、不重試、不 CRIT。
  需 leader 掛兩張價量完全相同的單才會觸發。
- **F3 第二輪 settle 期間部分成交的非 ro 單被當 extra**：發誤導 CRIT（多餘 1），且該殘單不撤。需第一輪因別的原因已進重試才會觸發。

## 5. 不做（範圍外，記錄備查）

- 不改 `settle_seconds`（任何窗口長度都有同樣問題，調參不是修法）。
- 不新增查 fills 的 API 呼叫。
- 不部署；部署會重啟實盤 follower，由使用者另行裁決。

## 6. 回報格式（給 builder）

(a) 一段話：做了什麼、關鍵取捨；(b) 改動檔案:行號範圍；(c) 每條驗收的指令與輸出末尾；
(d) plan 沒涵蓋而你做了判斷的地方（逐條列）。未滿足的驗收明說。
