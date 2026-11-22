# leader 權益基準修正 Implementation Plan

> **For agentic workers:** 逐 task 由主線程派工（`builder`／`reviewer`），只讀本檔；plan 沒涵蓋的決策先回報主線程。
> 狀態（2026-11-22）：**草稿，等使用者確認 §4 的 D1、D2 後才派工。**

**Goal:** 跟單倍率的分母（leader 權益）改用交易所自己顯示的帳戶總值，讓統一帳戶／portfolio margin 的 leader 不再造成倍率放大與開倉暴衝。

**Architecture:** adapter 新增一個唯讀讀取（`portfolio` 最後一點＝帳戶總值＋時間戳）；新純函式模組 `leader_equity.py` 是「leader 權益」的唯一產生點，`run_cycle` 的三個使用處（倍率分母、流量中性化、波動權重）都改從它取值；讀不到或可疑時本輪零交易動作並告警，**絕不退回 perp `accountValue`**。

**Tech Stack:** Python 3.11、uv、pytest（全離線）、hyperliquid-python-sdk 0.24.0（`Info.portfolio`、`Info.query_user_abstraction_state`）。

---

## 1. 背景與證據

事故與對帳全文：`docs/superpowers/research/2026-11-22-leader-equity-basis-recon.md`（以下以「recon §N」引用）。

- 2026-11-22 13:49 UTC leader `0xfb9c…9760`（統一帳戶）開空 0.015 BTC，`f438b3…` 被放大成開空 1.6788 BTC（$101,665，帳戶 21 倍），兩分鐘內平回大半；3 分鐘 −$147.61（recon §1）。
- 根因：`src/spark/copytrade/loop.py:333` 用 perp `accountValue` 當分母；統一帳戶的錢在現貨帳本，perp `accountValue` 空倉為 0、開倉後只是保證金。
- 交易所帳戶總值 `T`（`portfolio` 最後一點）在四種模式、12 個帳戶上與獨立重建值吻合（recon §2）；探索池 60 個位址有 49 個不是傳統模式（recon §3）。

## 2. 目標

1. leader 權益＝`T`（依 D2 決定是否對傳統模式保留現行算法）。
2. 「leader 權益」只有一個產生點；倍率分母、`max_target_leverage` 的有效槓桿、流量中性化的 `raw`、波動權重的參數全部同源。
3. 讀不到、過期、非正值而 leader 有倉位或掛單 → 本輪零交易動作＋critical 告警；不得以 0 或 perp `accountValue` 繼續算。
4. 上線前用正式程式路徑對真實帳戶實測對帳（T3）。

## 3. 不做（範圍外，請勿順手改）

- **follower 側的權益（倍率分子、回撤 kill switch、成本熔斷）不動**：現有 follower 都是傳統模式。統一帳戶的 follower 目前會因分子為 0 而靜默不跟單——另案處理，本 plan 只在 §7 記為殘留風險。
- 探索頁／交易員頁以 perp `accountValue` 算的指標（`src/spark/publicapi/hl_explore.py:691`、`:825-836`）不動，另案。
- 部位安全網（`positions.py`）、掛單對帳（`orders.py` 的 `_build_desired`／`_plan`／`_reconcile_orders`）、觸發單鏡射不動。
- 不新增任何 `CopySettings` 欄位或 env；不改 `COPY_MAX_TARGET_LEVERAGE` 預設。
- 不碰 `/Users/jim/projects/hl-copytrader`；不讀 `.env*`。

## 4. 裁決表

| # | 裁決 | 狀態 |
|---|---|---|
| D1 | **leader 權益算什麼**。A（建議）：交易所顯示的帳戶總值 `T`——含現貨所有代幣、質押 HYPE、vault 存款。B：只算可當永續保證金的 USDC。在 leader `0xfb9c…` 上：A＝$707（名目 1.72 倍）、B＝$209（5.80 倍）；`f438b3…` 的目標倉位 A＝0.0665 BTC、B＝0.225 BTC（recon §4）。A 的理由：一個端點、一個數字、與交易所畫面及本站策略頁績效同一基準、四種模式通用（B 在 portfolio margin 下 USDC 可為負，需另外處理，等於那 17% 的 leader 不能跟）；A 偏小的方向是安全方向。A 的代價：leader 若有大量與交易無關的現貨或質押，跟單倉位會偏小。 | **待使用者確認** |
| D2 | **傳統模式（`disabled`）的 leader 要不要一起改**。X（建議）：所有模式一律用 `T`，不讀帳戶模式——少一次查詢、沒有模式切換的不連續、沒有「不認得的模式」這條路徑。代價：傳統模式 leader 若在現貨錢包或質押放了大量閒置資金，跟單倉位會比現在小（目前沒有任何 follower 在跟傳統模式的 leader）。Y：`disabled` 維持 perp `accountValue`，其餘模式用 `T`，每輪多查一次模式。 | **待使用者確認** |
| D3 | 讀不到／過期（最後一點時間戳距今超過 180 秒）／值 ≤ 0 而 leader 有倉位或掛單 → 本輪零交易動作（不下單、不撤單、不調倉）＋ critical（`dedup_key="leader_equity_unavailable"`），心跳 `detail` 寫原因。值 ≤ 0 且 leader 無倉位無掛單 → 視為空帳戶，沿用既有行為（倍率 0、無事可做、不告警）。 | 主線程定 |
| D4 | 絕不退回 perp `accountValue`（那正是事故的來源）。暫時性網路錯誤照既有讀取的處理方式往外拋（`main_loop` 的連續錯誤計數）。 | 主線程定 |
| D5 | `target_notional`（`max_target_leverage` 的分子）維持 `leader_state.total_ntl_pos`（預設 dex，與被鏡射的倉位同範圍）。 | 主線程定 |
| D6 | 部署時機：leader 與 follower 都無倉位時部署（使用者 2026-11-22 表示會先關掉 leader 倉位）。若部署當下仍有倉位，重啟後第一輪會以市價把 follower 倉位縮到新目標——部署前主線程先查，有倉位就停下問。 | 主線程定 |

> 下文以 D1＝A、D2＝X 撰寫。使用者若選 B 或 Y，主線程先改寫 T1／T2 再派工。

### 方程式與數值錨例（預註冊，驗收以此為準）

$$\text{leader\_equity} = T = \text{portfolio 全部視窗中時間戳最新一點的 accountValue}$$

$$\text{scale} = \frac{\text{capital} \times \text{utilization} \times \text{weight}}{\text{leader\_equity}}$$

| 錨例 | 輸入 | 預期 |
|---|---|---|
| E1（recon §4，13:57:10Z） | `T`＝707.37、perp av＝120.58；follower 權益 4704.22、使用率 0.5、weight 1 | leader_equity＝707.37；scale＝2352.11÷707.37＝**3.3252**（±0.0001）；leader −0.02 BTC → 目標 **−0.0665 BTC**。舊算法為 19.5066 |
| E2（事故第一輪，13:49:03Z） | `T`＝708、perp av＝21.2；follower 權益 4744.73、使用率 0.5 | scale＝2372.365÷708＝**3.3508**；leader −0.015 → 目標 **−0.0503 BTC**（事故實際 −1.6788） |
| E3 | `T` 的時間戳比 now 早 181 秒，leader 有倉位 | `LeaderEquityUnavailable`；本輪 0 筆下單／撤單／調倉 |
| E4 | `T`＝0、leader 無倉位無掛單 | leader_equity＝0、scale＝0、無告警 |
| E5 | `T`＝0、leader 有 1 個倉位 | `LeaderEquityUnavailable` |

## 5. 檔案地圖

| 檔案 | 動作 | 責任 |
|---|---|---|
| `src/spark/exchange/base.py` | 改 | 新增 `AccountTotal` dataclass、`ExchangeAdapter.get_account_total` |
| `src/spark/exchange/hyperliquid.py` | 改 | `get_account_total` 實作（`Info.portfolio`） |
| `src/spark/exchange/fakes.py` | 改 | `FakeAdapter` 依位址回 `AccountTotal`；`get_account_state` 可依位址覆寫 |
| `src/spark/copytrade/leader_equity.py` | 新增 | `LeaderEquity`、`LeaderEquityUnavailable`、`resolve_leader_equity`（純）、`read_leader_equity`（薄讀取層） |
| `src/spark/copytrade/loop.py` | 改 | 第 3 節改用 `read_leader_equity`；3.5、4 節三個使用處改同源；unavailable 提前返回 |
| `src/spark/copytrade/orders.py` | 改 | `CycleReport` 加 `skip_detail: str \| None = None`（只加欄位） |
| `scripts/run_copytrade.py` | 改 | 心跳 `detail` 取 `report.skip_detail` |
| `scripts/leader_equity_probe.py` | 新增 | 唯讀主網探針（T3） |
| `tests/test_leader_equity.py` | 新增 | 純函式＋讀取層 |
| `tests/test_hyperliquid_reads.py`、`tests/test_copy_loop.py` | 改 | adapter 解析、迴圈整合 |

## 6. Tasks

### T1 `@inline` — adapter 讀交易所帳戶總值

**目標**：提供「交易所顯示的帳戶總值＋它的時間戳」這一個讀取。**動機**：它是新分母的唯一資料來源；時間戳讓呼叫端能拒絕過期值。

**簽名**（`src/spark/exchange/base.py`）：

```python
@dataclass(frozen=True)
class AccountTotal:
    """交易所顯示的帳戶總值（HL `portfolio` 最新一點）。value 含現貨、perp、質押、vault。"""
    value: Decimal
    as_of_ms: int

class ExchangeAdapter(ABC):
    def get_account_total(self, address: str) -> AccountTotal:
        """唯讀。查無任何資料點時 raise ValueError（不回 0）。"""
        raise NotImplementedError
```

寫法比照同檔既有的 `query_user_abstraction`（非 abstract、預設 raise），避免其他測試替身被迫實作。

**Hyperliquid 實作**（`src/spark/exchange/hyperliquid.py`）：`rows = self._info.portfolio(address)`；掃 `day／week／month／allTime` 四個視窗的 `accountValueHistory`，取**時間戳最大**的那一點（掃描邏輯與同檔 `get_equity_view` 相同，可抽成檔內私有 helper 共用，但 `get_equity_view` 的對外行為不得改變）。回應不是 list、四個視窗都沒有點、或值無法轉 `Decimal` → `raise ValueError(...)`（訊息不含位址以外的資料）。`perpDay` 等 `perp*` 視窗**不得**納入。

**FakeAdapter**（`src/spark/exchange/fakes.py`）：建構參數加 `account_totals: dict[str, AccountTotal] | None = None`（位址小寫比對；查無 → `raise ValueError`）與 `account_states: dict[str, AccountSnapshot] | None = None`（有該位址就回它，否則維持現行單一 `_account`）。既有呼叫方式零改動。

**測試**（`tests/test_hyperliquid_reads.py`，沿用該檔既有的假 `Info` 寫法）：
1. 四個視窗最後一點時間戳不同 → 取最大者的值與時間戳。
2. 回應含 `perpDay`／`perpWeek` 且其時間戳更大 → 仍不取。
3. 空回應／非 list／值為 `"abc"` → `ValueError`。
4. `get_equity_view` 既有測試全綠（行為不變）。

**驗收**：`uv run pytest -q tests/test_hyperliquid_reads.py tests/test_base_types.py` 全綠；`uv run ruff check src tests scripts` 0 錯；變異——把「取時間戳最大」改成「取 `day` 視窗最後一點」→ 測試 1 轉紅。Commit：`feat: adapter 讀交易所帳戶總值（plan 2026-11-22 T1）`。

### T2 `@inline` — leader 權益單一產生點＋接進引擎（依賴 T1）

**目標**：`run_cycle` 的 leader 權益改由 `leader_equity.py` 產生。**動機**：同一個數字被三處使用，各自取值就會再出現不同基準（工程原則 #1、#5）。

**新模組** `src/spark/copytrade/leader_equity.py`：

```python
MAX_AGE_MS = 180_000

class LeaderEquityUnavailable(Exception):
    """leader 權益無法判定——呼叫端必須讓本輪零交易動作。reason 是給人看的一句話。"""
    def __init__(self, reason: str) -> None: ...

@dataclass(frozen=True)
class LeaderEquity:
    value: Decimal          # 倍率分母
    as_of_ms: int
    source: str             # 恆為 "exchange_total"（保留欄位供日後擴充）

def resolve_leader_equity(total: AccountTotal, *, now_ms: int,
                          leader_has_exposure: bool) -> LeaderEquity:
    """純函式。
    - now_ms - total.as_of_ms > MAX_AGE_MS → raise（過期）。as_of 在未來（時鐘偏差）不算過期。
    - total.value <= 0 且 leader_has_exposure → raise（讀不到錢 ≠ 錢虧光）。
    - total.value <= 0 且無曝險 → LeaderEquity(value=Decimal("0"), ...)（空帳戶，沿用既有 scale=0）。
    """

def read_leader_equity(adapter, leader: str, *, now_ms: int,
                       leader_has_exposure: bool) -> LeaderEquity:
    """adapter.get_account_total → resolve。ValueError（回應形狀不對）轉成
    LeaderEquityUnavailable；其餘例外（網路）原樣往外拋（D4）。"""
```

**`loop.py` 改動**（只動第 3、3.5、4 節；第 0–2.6、5–7 節一行都不改）：
1. 第 3 節讀完 `leader_orders`／`leader_positions`／`leader_state` 後：
   `leader_has_exposure = bool(leader_positions) or bool(leader_orders)`；
   `leader_eq = read_leader_equity(adapter, leader, now_ms=int(time.time()*1000), leader_has_exposure=...)`。
   捕捉 `LeaderEquityUnavailable as e` → `notifier.critical("leader_equity", f"leader 權益無法判定（{e.reason}），本輪零交易動作", dedup_key="leader_equity_unavailable")`，
   回傳 `CycleReport(reconcile=_EMPTY_RECONCILE, safety_net={"skipped": True}, scale=Decimal("0"), skip_detail=f"leader 權益無法判定：{e.reason}")`（`tripped` 維持 False）。
   這個提前返回必須在 `my_orders`／`my_positions` 讀取之前或之後皆可，但**在任何 `ex.` 寫入之前**。
2. 3.5 節：`leader_equity = leader_eq.value`、`raw = leader_eq.value`（原本兩處 `leader_state.account_value`）。
3. 第 4 節：`compute_volatility_stats(daily, leader_eq.value)`；`compute_scale_factor(leader_equity=leader_equity, ...)` 不變（變數來源已換）；`target_notional=leader_state.total_ntl_pos` 不變（D5）。
4. 註解同步：第 3.5、4 節原本寫「同源：account_state.account_value」的註解改寫為現況——分母出自 `leader_equity.read_leader_equity`（交易所帳戶總值），分子仍是 follower 的 perp `accountValue`；兩者不同來源是刻意的，理由引 recon §2。
5. 改完後 `loop.py` 內不得再有 `leader_state.account_value`（`grep -c "leader_state.account_value" src/spark/copytrade/loop.py` → 0）。

**`CycleReport`**（`orders.py`）：加 `skip_detail: str | None = None`，放在最後一個欄位之後；不改任何既有建構點。

**`scripts/run_copytrade.py`**：`hb["detail"] = None` 那一行改為 `hb["detail"] = getattr(report, "skip_detail", None)`；`hb["result"]` 的算法不動。

**`sizing.py`**：只改 `compute_scale_factor` docstring 中「對方帳戶淨值」的說明（現為交易所帳戶總值，由呼叫端傳入）；函式本體不動。

**測試**：
- `tests/test_leader_equity.py`（新）：錨例 E3、E4、E5；新鮮（180 秒整不 raise、181 秒 raise）；`as_of` 在未來不 raise；`read_leader_equity` 對 `ValueError` 轉 `LeaderEquityUnavailable`、對 `ConnectionError` 原樣拋出。
- `tests/test_copy_loop.py`（沿用該檔 `_run`／`_settings`／`_account` helper，leader 與 follower 用 T1 的 `account_states`／`account_totals` 分開給值）：
  1. **E1**：leader perp av 120.58、`AccountTotal(707.37, now)`、倉位 −0.02 BTC；follower av 4704.22、`capital_utilization=0.5` → `report.scale` 與 3.3252 相差 < 0.0001；產生的調倉數量對應 −0.0665 BTC（依該檔既有的數量捨入方式斷言）。
  2. **E2**：leader perp av 21.2、total 708、倉位 −0.015 → scale ≈ 3.3508（不是 111.9）。
  3. unavailable（過期）：本輪 `ex.records` 沒有任何 place／modify／cancel／調倉；`notifier` 收到一則 critical 且文字含「leader 權益無法判定」；`report.skip_detail` 非空；`report.tripped is False`。
  4. leader 無倉位無掛單、total 0：無 critical、scale 0。
  5. 流量中性化開啟時 `adjusted_leader_equity` 收到的 `raw` 是 total 不是 perp av（用該檔既有的中性化測試寫法）。
  6. 既有測試因 `FakeAdapter` 預設沒有 `account_totals` 而轉紅者：在該檔 helper 補上「leader 的 total＝原本 `_account()` 的 `account_value`」，使既有斷言數值不變；**不得改任何既有斷言**。其他測試檔若轉紅，同法處理並在回報列出。

**驗收**：
1. `uv run pytest -q` 全綠；`uv run ruff check src tests scripts` 0 錯。
2. `grep -c "leader_state.account_value" src/spark/copytrade/loop.py` 輸出 0。
3. 變異三條，各自至少一條測試轉紅，列出測試名後還原：(a) `loop.py` 分母改回 `leader_state.account_value`；(b) 拿掉過期檢查；(c) unavailable 時不提前返回（改成 `leader_equity = Decimal("0")` 繼續跑）。
4. `git diff --stat` 只含 §5 檔案地圖內的檔案；`git diff -- src/spark/copytrade/positions.py` 為空。
5. Commit：`fix: 跟單倍率分母改用交易所帳戶總值（統一帳戶 leader 倍率放大；plan 2026-11-22 T2）`。

### T3 `@inline` — 主網唯讀探針（builder 寫、主線程跑）

**目標**：用**正式程式路徑**（`HyperliquidAdapter.get_account_total`＋`resolve_leader_equity`）對真實帳戶取值，並與獨立重建值對帳。**動機**：離線測試的假資料照同一個假設寫，測不出欄位名或語意假設錯（工程原則 #1 第二個教訓）。

`scripts/leader_equity_probe.py`：參數 `addresses...`、`--out <jsonl>`。只打 HL 主網 `info`（唯讀、不需要任何金鑰、不讀 `.env*`）。每個位址輸出一行：`userAbstraction`、perp `accountValue`（預設 dex）、`get_account_total` 的值與時間戳年齡、獨立重建值（現貨各代幣×mark＋質押 HYPE×mark＋vault 權益，`disabled` 模式再加所有 perp dex 的 `accountValue`）、兩者差額與百分比、`PASS`（|差| ≤ max($1, 0.1%)）／`FAIL`。至少一個位址讀取失敗或 `FAIL` → exit 1。重建邏輯寫在腳本裡，不得呼叫 `get_account_total`（兩邊必須獨立）。

**驗收**：(1) builder：`uv run python -m scripts.leader_equity_probe --help` 可執行且不連網；ruff 0 錯；**不實跑**。(2) 主線程：對 leader `0xfb9c…`、`0xf97a…`、正式機 `leaders.json` 全部位址、recon §2 的 portfolioMargin／disabled 樣本各一實跑，結果表追加到 recon 檔 §6；全部 `PASS` 才進 T5。Commit：`feat: leader 權益主網唯讀探針（plan 2026-11-22 T3）`。

### T4 — 審核（`reviewer`）

輸入：`git diff main...<分支>`、本 plan、recon 檔、測試輸出。重點依序：
1. 有沒有任何路徑在 unavailable 時仍下單／撤單／調倉，或退回 perp `accountValue`／0 繼續算。
2. leader 權益是否只有一個產生點；倍率分母、`eff_lev`、流量中性化 `raw`、波動權重是否同源。
3. `get_account_total` 對回應形狀的假設（視窗名、點的形狀、`perp*` 視窗排除）；過期判定的邊界與時鐘偏差。
4. 提前返回是否繞過了任何既有的安全動作（第 0–2.6 節的 kill switch、回撤、成本熔斷必須照常執行）。
5. 既有測試是否被放寬；三條變異是否真的被抓到（親自重現至少兩條）。
6. 工程原則 #1、#3、#5、#6 逐條對照；專案紅線。
修到 0 Critical／0 Warning 為止；只有設計分岔才停下問使用者。

### T5 — 部署（主線程）

1. 前置：leader 與所有 follower 無倉位無掛單（D6）；正式機 `DEPLOYED_VERSION`＝`de4a816`、待換檔案 md5 與該 commit 一致；無維護旗標、無 `/tmp/spark-sync`。
2. RUNBOOK §5.8i 多檔熱修：`git archive` 打包 §5 的 `src/`＋`scripts/run_copytrade.py` → 備份 → `install` → `filet-engine` user import 驗算（含對 leader 位址實呼叫一次 `get_account_total` 並印出值與年齡）→ `reload_follower.sh` → `DEPLOYED_VERSION`。
3. 驗證：三個 follower `active`、`NRestarts` 0、心跳連續兩輪推進、journal 無錯誤、`filet_regression_check --http --ssh` 全過。
4. leader 下次開倉時核對：follower 第一筆成交數量÷leader 成交數量 ≈ follower 本金×使用率÷leader 帳戶總值（誤差 < 8%）。這一步要等使用者實際開倉，寫進回報的待觀察項。
5. RUNBOOK 附錄 B、plan 狀態、memory。

## 7. 殘留風險（上線前要讓使用者知道）

- **R1 跟單倉位會明顯變小**：以 leader `0xfb9c…` 現況，同樣的 leader 倉位，`f438b3…` 的目標從約 0.39 BTC 變成約 0.067 BTC（約六分之一）。這是修正，不是副作用；想要更大的倉位要調資金使用率。
- **R2 leader 帳戶裡與交易無關的資產會稀釋倍率**（D1＝A）：現貨持幣、質押 HYPE、vault 存款都算進分母。leader 的現貨幣價波動也會讓倍率緩慢漂移，超過容差（8%）時 follower 會調倉。
- **R3 每輪多一次 `portfolio` 查詢**（HL info 權重 20）：三個 follower 每分鐘共 +60 權重。HL 每 IP 上限 1200／分，`filet-api` 自限 900。follower 數量成長到十個以上時要重新檢視（可改為同一 leader 共用快取）。
- **R4 「`portfolio` 最後一點是即時值」是實測而非文件保證**：以 180 秒新鮮度檢查防守；若 HL 改成延遲更新，引擎會進入「零交易動作＋critical」而不是用舊值下單。這段期間 follower 的既有倉位不會被調整（交易所端的止盈止損仍在）。
- **R5 統一帳戶的 follower 仍不支援**：分子是 follower 的 perp `accountValue`，統一帳戶 follower 空倉時為 0 → 靜默不跟單。HL 上多數新帳戶是統一帳戶，這會影響新客戶，需另案。
- **R6 探索頁的槓桿／強平風險指標仍以 perp `accountValue` 計算**，對統一帳戶交易員不準，另案。
- **R7 分子分母不同來源**：follower 用 perp `accountValue`、leader 用帳戶總值。對傳統模式且只把錢放 perp 的帳戶兩者相等；差異來自帳戶模式，recon §2 有恆等式。

## 8. 驗收總表

| 項目 | 證據 |
|---|---|
| 離線回歸 | `uv run pytest -q` 全綠；`uv run ruff check src tests scripts` 0 錯（主線程親跑） |
| 數值錨例 | E1–E5 各有對應測試 |
| 變異 | T1 一條、T2 三條；主線程親自重現 T2 至少兩條 |
| 主網對帳 | T3 探針全部 `PASS`，結果在 recon §6 |
| 審核 | reviewer 0 Critical／0 Warning |

## 9. 待使用者確認

1. **D1**：leader 權益用交易所帳戶總值（A，建議）還是只算 USDC 保證金（B）。
2. **D2**：所有模式一律用帳戶總值（X，建議）還是傳統模式維持現行算法（Y）。
3. 知悉 R1：修正後同樣的 leader 倉位，跟單倉位約為現在的六分之一。
