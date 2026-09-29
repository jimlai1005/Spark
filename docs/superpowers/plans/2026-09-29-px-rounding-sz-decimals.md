# 修法：adapter 送單價依 szDecimals 捨入（2026-09-29 PUMP 平倉失敗事故）

狀態：**草稿，待使用者確認後派工**。

## 事故摘要

- follower `f438b3…dce9`（主網真錢）跟 leader `0xedea…d773`（09-28 13:46 UTC 換的）持有
  PUMP 多單 23,544,508 顆（名目約 $112k）。leader 減倉時 follower 的 reduce-only IOC
  反覆被交易所拒：`Order has invalid price`。
- 根因：`HyperliquidAdapter._round_px`（`src/spark/exchange/hyperliquid.py:24,38-40`）只做
  **5 位有效數字**，沒做 HL 第二條規則 **小數位 ≤ 6 − szDecimals（perp）／8 − szDecimals（spot）**。
  PUMP szDecimals=0 → 最多 6 位小數；`mid × (1 − 0.05)` 例：0.004752 × 0.95 = 0.0045144，
  5 sf 後仍是 7 位小數 → 拒單。SDK 自己的 `_slippage_price`（`hyperliquid/exchange.py:132`）
  是 `round(float(f"{px:.5g}"), 6 − szDecimals)`，`market_open` 走 SDK 所以開倉正常；
  `close_reduce_only`、`place_order`、`modify_order` 自算價／自捨入，全部繞過 SDK 規則。
- 間歇性：mid 位數少時（如 0.0048 × 0.95 = 0.00456）恰好合規、可成交，所以 09-28 有 10 張
  平倉單成功，其餘被拒。這也是為什麼問題到現在才浮現：既有 follower 只跟 BTC/ETH/HYPE，
  價位夠高，5 sf 永遠塞得進 6 位小數。
- 影響面：**所有**經 adapter 自算價的寫入（跟單減倉／全平／反轉平倉腿、kill switch 與
  `scripts/panic.py` 的緊急全平、鏡射掛單 `place_order`／`modify_order`）。任何
  「5 sf 需要的小數位 > 6 − szDecimals」的幣都會中——低價幣（<0.01）最典型。

## 設計裁決

1. 捨入邏輯集中在 adapter 一個函式：`_round_px(coin, px)`，規則＝先 5 sf，再
   `quantize` 到 `6 − szDecimals`（spot 8 − szDecimals；本 repo 跟單只做 perp，但函式要
   對 `_is_spot_coin` 正確分支）。**不**改成呼叫 SDK `market_close`——adapter 的
   mid 來源、builder、reduce-only 語意都要維持在同一處（工程原則 5：單一邊界）。
2. szDecimals 來源 = 既有 `get_size_decimals(coin)`（有 meta 快取、自癒）。
3. 只改捨入，不改 slippage、不改流程、不順手修別的。
4. 交易所拒單訊息已可見（`_fail_detail`），不用再加告警。

## 全 repo 送單價路徑盤點（2026-09-29 主線程 rg 實查，使用者要求逐種訂單確認）

所有對 SDK 的寫入都只在 `src/spark/exchange/hyperliquid.py`（`rg "\._exchange\.(order|modify_order|market_open|cancel|update_leverage)\("`
只命中此檔六處）。帶價格的寫入與其上游：

| 訂單種類 | 價格從哪來 | 進 adapter 的方法 | 現況 | 修後 |
|---|---|---|---|---|
| 跟單減倉／全平／反轉平倉腿（reduce-only IOC） | `positions.py` → `executor.close_reduce_only` → adapter 內 `mid × (1 ± slippage)` | `close_reduce_only` :490 | **壞**（自算價，位數溢出） | `_round_px(coin, px)` |
| kill switch／`scripts/panic.py`／`panic_all.py` 緊急全平 | 同上，slippage 換 `flatten_slippage` | `close_reduce_only` :490 | **壞** | 同上 |
| 鏡射 leader 掛單（GTC/ALO/IOC limit） | `orders.py` 原樣傳 leader 的 `limit_px` | `place_order` :423 | 目前合規（leader 價本就合規，5 sf 不增位） | 同上（結構性保護） |
| 改單 | `orders.py` 原樣傳 leader 的 `limit_px` | `modify_order` :455 | 同上 | 同上 |
| Phase 1 orchestrator 穿價單 | `orchestrator.py:14-16` `best_opposite_px × (1 ± 0.5%)`，註解明寫「由 adapter `_round_px` 處理」 | `place_order` :423 | 潛在壞（自算價） | 同上 |
| 新開倉／加倉／反轉重開 | SDK `market_open` → SDK `_slippage_price` 自己做兩條規則 | `market_open` :474 | 正確 | 不動 |
| trigger（TP/SL）單 | `executor._skip_trigger` 一律不送 | — | 不送 | 不動；**日後加 trigger 支援時 `trigger_px` 必須同走 `_round_px`**（寫進 adapter 註解） |
| cancel／update_leverage | 無價格 | :434 / :499 | — | 不動 |
| testnet 腳本 `testnet_leader_drive.py`、`run_testnet_flow.py` | 經 adapter `market_open`／`close_reduce_only`／orchestrator | 同上 | 隨 adapter 修好 | 不動 |

size 捨入是獨立規則（`instrument._round_size`：ROUND_DOWN 到 szDecimals），與 HL「Sizes are rounded to szDecimals」一致，不在本次範圍。

結論：修 `_round_px` 一處＋三個呼叫點傳 coin，即覆蓋全部帶價格的訂單種類；沒有任何旁路直接呼叫 SDK。

## Tasks

### Task 1 `@inline`：`_round_px` 依 szDecimals 捨入

檔案：`src/spark/exchange/hyperliquid.py`

- 簽名改為 `def _round_px(self, coin: str, px: Decimal) -> float`。
- 規則：`d = self._PX_CTX.create_decimal(px)`（5 sf）→ `max_dec = (8 if spot else 6) − self.get_size_decimals(coin)`
  → `d = d.quantize(Decimal(1).scaleb(-max_dec), rounding=ROUND_HALF_EVEN)`（`max_dec < 0` 時用 `scaleb(-max_dec)` 仍成立，
  例 szDecimals=7 極端情形要有測試）→ `float(d)`。spot 判斷用 `spark.copytrade.instrument._is_spot_coin`
  或 coin 含 `/`／以 `@` 開頭（與 SDK `asset >= 10_000` 等價的字面判斷，寫明依據）。
- 三個呼叫點都改傳 coin：`place_order`（:424）、`modify_order`（:456）、`close_reduce_only`（:491）。
- 更新類別頂端註解（:22-23，「極低價幣種的 tick 細則延後」這句刪掉，改寫成兩條規則）。
- `get_size_decimals` 若 raise（未知幣）→ 讓它 raise，不吞（原本開倉路徑對未知幣也是 raise）。
- 在 `_round_px` docstring 註明：日後若加 trigger 單支援，`trigger_px` 必須同走本函式。

驗收：
```
uv run pytest tests/test_hyperliquid_adapter.py -q
uv run ruff check src tests
```

### Task 2 `@inline`：測試

檔案：`tests/test_hyperliquid_adapter.py`

- `FakeInfo` 加 `meta()` 回 `{"universe": [{"name":"ETH","szDecimals":4}, {"name":"PUMP","szDecimals":0}, {"name":"XYZ","szDecimals":5}]}`。
- 既有 `test_round_px_to_5_sig_figs` 改成傳 coin（ETH 的兩個斷言不變）。
- 新增：
  - `PUMP`, `Decimal("0.004752") * (1 - Decimal("0.05"))` → `0.004514`（不是 0.0045144）。
  - `PUMP`, `Decimal("0.004752") * (1 - Decimal("0.30"))` → `0.003326`（flatten_slippage 路徑）。
  - `XYZ`（szDecimals=5，最多 1 位小數）, `Decimal("12.345")` → `12.3`。
  - `test_close_reduce_only_sends_sz_decimals_rounded_px`：FakeInfo `all_mids` 回 `{"PUMP": "0.004752"}`，
    呼叫 `close_reduce_only(..., "PUMP", is_buy=False, size=Decimal("1798266"), slippage=Decimal("0.05"), ...)`，
    斷言 `FakeExchange.calls` 裡的 limit_px == 0.004514 且 `hyperliquid.utils.signing.float_to_wire(limit_px)` 小數位 ≤ 6。
  - 同形一則 `place_order`（Order.limit_px=Decimal("0.0045144") → 0.004514）。
- 這些測試在改 Task 1 之前必須是紅的（先跑一次確認），改完轉綠。

驗收：`uv run pytest tests/test_hyperliquid_adapter.py -q` 全綠；`uv run pytest -q` 全綠（其他測試對 `_round_px` 的簽名若有引用一併更新）。

### Task 3 `@inline`：全域回歸 + 呼叫點盤點

- `rg -n "_round_px" src scripts tests` 應只剩 adapter 內定義與三個呼叫點＋測試；沒有第四個自算價路徑。
- `uv run pytest -q` 全綠、`uv run ruff check src tests scripts` 乾淨。

## 審查後修正（2026-09-29 reviewer 第一輪，主線程裁決）

採納：
- **W1／S2（spot 分支是死碼＋分層倒置）**：拿掉 `_is_spot_coin` 分支與 `from spark.copytrade.instrument import _is_spot_coin`。
  `max_dec = 6 − szDecimals` 固定 perp。docstring 註明：本 adapter 只送 perp；spot 標的不在 perp `meta()` universe，
  `get_size_decimals` 會 raise（fail-loud），上游 `orders.py`／`positions.py` 已過濾 spot。Task 1 原「對 `_is_spot_coin` 正確分支」
  一句作廢。
- **S1**：類別註解「本函式與其等價」改為「規則等價；tie 情形末位捨入可能與 SDK 差一檔（Decimal 精確值 vs 二進位 float），兩者皆合法價」。
- **S3(a)**：加測試——未知幣（不在 universe）呼叫 `place_order` 時 `ValueError` 往外拋、不吞。

不採納（既有問題，範圍外，另行追蹤）：
- **W2** builder dex 幣（`xyz:NVDA`）：引擎建 `Info(api_url, skip_ws=True)` 未帶 `perp_dexs`，SDK `name_to_coin` 只載主 dex，
  `order()` 本來就 KeyError；`orders.py` 鏡射路徑缺 `_coin_dex` 過濾與 `get_size_decimals` 對未知幣每次重打 `meta()` 皆為既有行為。
- **W3** `_round_px` 內 `meta()` 讀不在 resilience 邊界：同路徑既有 `all_mids()` 裸讀同性質；且快取在 `positions.py:204`
  同步時已預熱整個 universe，只有不在 universe 的幣才會在送單前重打。

### Task 4 `@inline`：套用上述採納項

檔案：`src/spark/exchange/hyperliquid.py`、`tests/test_hyperliquid_adapter.py`。
驗收：`uv run pytest tests/test_hyperliquid_adapter.py tests/test_exchange_writes_builder.py -q` 全綠；
`uv run ruff check src tests scripts` 乾淨；`rg -n "^from spark\.copytrade|^import spark\.copytrade" src/spark/exchange/` 零命中
（判準是 import 依賴歸零，註解／docstring 提及不算；2026-09-29 第二輪 reviewer 指出原字面條款不可能成立，已改）。

## 第二輪審查結果（2026-09-29）

結論「可部署」，無 Critical／Warning。主線程自行採納三項 Suggestion：未知幣測試加 `match="未知幣種"` 與
`ad._exchange.calls == []`；`__init__` 快取註解改成與實作一致（miss 會重打）；本節驗收條款改 import-only。
另案（未動，待使用者裁決）：(1) `_round_px` 結果 ≤ 0 時 raise 的護欄（目前不可達）；(2) szDecimals ≥ 7 時
`max_dec` 為負的行為與 SDK 相同、HL 規則未定義，是否改 `max(0, …)` 需先查 HL 側行為。

## 部署（主線程親跑，不派工）

RUNBOOK 一般部署流程 + **重啟三個 `filet-follower@*`**（不是只重啟 filet-api）；
重啟前 `systemctl list-units "filet-follower@*"` 動態取清單。部署後觀測：下一輪 PUMP
校正腿若還有殘量應成交，TG 不再出現 `invalid price`。

## 不在範圍

- follower 部位是否全平／是否換 leader：使用者裁決（事故處置，非本 plan）。
- `followers.json` 的 `leader_address` 與 ledger 不一致（registry 仍寫 0xfb9c）：另開 issue，不在此修。
