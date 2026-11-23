# leader 權益基準修正 Implementation Plan

> **For agentic workers:** 逐 task 由主線程派工（`builder`／`reviewer`），只讀本檔；plan 沒涵蓋的決策先回報主線程。
> 狀態（2026-11-23）：**重啟、已獲使用者同意開工。** 2026-11-22 使用者曾裁決不採用（「覺得目前開單的數字正常」）；2026-11-23 使用者回報 follower 槓桿不合理，主線程查證為同一原因（recon §7），使用者原話：「如果是，我選A，我們需要開工。」→ D1＝A。D2 使用者未另行指定，採主線程建議的 X（可事後改）。 **T1 完成（`0c07006`，主線程已驗收）。** 同日使用者追加要求（D7）：leader 現貨有 BTC，分母會每分鐘變動，follower 不得因此每分鐘調整大小或開關單；解不了就可能退回修改前。→ 新增 T2b 倍率鎖定。使用者另要求接著做統一帳戶 follower（`fd149b…`），另開 plan。 **T2 完成（`5bc7d82`，主線程重現變異 (a)(c) 並重跑相關測試）；使用者已確認 D7。** T2 驗收時發現 D3 的缺口（見 D3a），併入 T2b 派工。 **D3a（`89b1d67`）與 T2b（`91553f0`）完成，全套 3864 passed；主線程重現兩條鎖定變異。** 進行中：T3 探針、T4 審核（範圍 `4d3d476..91553f0`）。 **T3 探針全數 PASS（recon §9）。T4 第一輪審核：0 Critical、3 Warning、1 Suggestion → 裁決 D8／D9，派 T4-fix，修完由 fresh reviewer 複審。** **T4-fix 完成（`bcbf532`、`2c0c9d9`、`db42109`；全套 3911 passed）。使用者確認波動權重照鎖定版本（leader 動作時即時重算，不做每日一次）。新增 D10（指紋與曝險只看會被鏡射的永續）→ T4-fix2，之後 fresh reviewer 複審 `91553f0..HEAD`。** **T4-fix2 完成（`b2c413a`，全套 3918 passed）。第二輪審核（`91553f0..b2c413a`）：0 Critical、1 Warning（D8 在中性化開啟下沒有測試——程式正確、變異存活）、1 Suggestion（非預設 dex 掛單算可鏡射沒有測試）；第一輪四項皆確認已修。→ T4-fix3 只補測試，再複審。** **T4-fix3 完成（`b1cd3ee`，全套 3920 passed）。第三輪審核：0 Critical；`src/` 正確、可部署；1 Warning＋1 Suggestion 皆為測試缺口（`cap_binding` 的 `max_target_leverage > 0` 與 `leader_equity > 0` 兩個守衛拿掉後測試不轉紅）→ T4-fix4 只補測試；其驗收是機械性的（兩條變異必須轉紅），由主線程親自重現後結案，不再開第四輪審查。**

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
| D1 | **leader 權益算什麼**。A（建議）：交易所顯示的帳戶總值 `T`——含現貨所有代幣、質押 HYPE、vault 存款。B：只算可當永續保證金的 USDC。在 leader `0xfb9c…` 上：A＝$707（名目 1.72 倍）、B＝$209（5.80 倍）；`f438b3…` 的目標倉位 A＝0.0665 BTC、B＝0.225 BTC（recon §4）。A 的理由：一個端點、一個數字、與交易所畫面及本站策略頁績效同一基準、四種模式通用（B 在 portfolio margin 下 USDC 可為負，需另外處理，等於那 17% 的 leader 不能跟）；A 偏小的方向是安全方向。A 的代價：leader 若有大量與交易無關的現貨或質押，跟單倉位會偏小。 | **A（使用者 2026-11-23 確認）** |
| D2 | **傳統模式（`disabled`）的 leader 要不要一起改**。X（建議）：所有模式一律用 `T`，不讀帳戶模式——少一次查詢、沒有模式切換的不連續、沒有「不認得的模式」這條路徑。代價：傳統模式 leader 若在現貨錢包或質押放了大量閒置資金，跟單倉位會比現在小（目前沒有任何 follower 在跟傳統模式的 leader）。Y：`disabled` 維持 perp `accountValue`，其餘模式用 `T`，每輪多查一次模式。 | **X（使用者未指定，採建議；回報時交代）** |
| D3 | 讀不到／過期（最後一點時間戳距今超過 180 秒）／值 ≤ 0 而 leader 有倉位或掛單 → 本輪零交易動作（不下單、不撤單、不調倉）＋ critical（`dedup_key="leader_equity_unavailable"`），心跳 `detail` 寫原因。值 ≤ 0 且 leader 無倉位無掛單 → 視為空帳戶，沿用既有行為（倍率 0、無事可做、不告警）。 | 主線程定 |
| D3a | **leader 無倉位且無掛單時，leader 權益永遠不得讓本輪提前返回**（T2 驗收發現、主線程裁決 2026-11-23）：此時所有目標都是 0，倍率無關緊要；若因過期或回應形狀錯誤而提前返回，follower 殘留的倉位與掛單那一輪就收不掉。規則：`leader_has_exposure` 為 False 時，過期／形狀錯誤／非正值一律回 `LeaderEquity(value=0)`、不告警、不提前返回（網路類例外仍照既有方式往外拋）。T2 的 `resolve_leader_equity` 目前對「過期且無曝險」會 raise，需修。 | 主線程定 |
| D4 | 絕不退回 perp `accountValue`（那正是事故的來源）。暫時性網路錯誤照既有讀取的處理方式往外拋（`main_loop` 的連續錯誤計數）。 | 主線程定 |
| D5 | `target_notional`（`max_target_leverage` 的分子）維持 `leader_state.total_ntl_pos`（預設 dex，與被鏡射的倉位同範圍）。 | 主線程定 |
| D6 | 部署時機：leader 與 follower 都無倉位時部署（使用者 2026-11-22 表示會先關掉 leader 倉位）。若部署當下仍有倉位，重啟後第一輪會以市價把 follower 倉位縮到新目標——部署前主線程先查，有倉位就停下問。 | 主線程定 |
| D7 | **倍率鎖定（使用者 2026-11-23 追加要求）**：follower 只在「leader 有動作」時才重算倍率；leader 沒動作的期間沿用上一次的倍率，分母（含 leader 現貨 BTC 的價格波動、leader 浮動損益）與分子（follower 自己的浮動損益）怎麼漂都不調倉、不重掛單。「leader 有動作」＝leader 的倉位或掛單集合與上一輪不同。另外三種情況也重算：(a) follower 的資金設定（使用率／本金／權重／槓桿上限）或 leader 位址變了；(b) follower 自己無倉位也無掛單（沒有東西會被擾動）；(c) 引擎重啟後第一輪（鎖定值只存在記憶體）。算出的倍率 ≤ 0 時不鎖定，照既有行為。**已知後果（要告知使用者）**：follower 自己入金／出金、leader 入金／出金，都要等 leader 下一次動作才反映到倉位大小；重啟（含部署）後第一輪若漂移已超過 8% 會有一次調整。依據：30 天模擬（recon §8）顯示只靠既有 8% 容差，leader 抱單期間仍會有 0–23 次因分母漂移的調倉。 | **使用者 2026-11-23 確認**（看過機制與三項後果後原話：「倍率鎖定可以接受，請繼續」） |
| D8 | **倍率鎖定不得凍結槓桿上限的「縮倉」方向**（T4 審核 Warning 1；主線程裁決 2026-11-23）：`max_target_leverage > 0` 且本輪 leader 有效槓桿（`leader_state.total_ntl_pos ÷ leader_equity`，與 `sizing.py:104-106` 同一算式、同一個 `leader_equity` 變數）超過上限，且新算倍率**小於**鎖定值 → 當輪重算並重新鎖定（只會縮、不會放；上限解除後不自動加回，等 leader 動作）。目前只有 vault leader 會被強制套 20 倍上限，其餘 follower 的上限是 0（停用）→ 對現有三個 follower 無行為差異。**波動權重維持凍結**：鎖定期間每日波動權重的變化不反映（它正是 2026-10-21 夜間來回調倉的來源），leader 下次動作時才套用——回報使用者（R8）。 | 主線程定；回報使用者 |
| D9 | **流量中性化的流量基準要跟著分母換**（T4 審核 Warning 2；主線程裁決）：分母現在是帳戶總值，現貨↔永續的內部劃轉（`accountClassTransfer`）不改變帳戶總值，不得再被當成流量從分母扣除。`get_ledger_flows` 增加 `basis` 參數（`"perp"` 預設＝現行行為；`"total"`＝`accountClassTransfer` 視為中性：不計入流量、也不列入 unknown）。leader 中性化（`loop.py` 3.5 節）傳 `"total"`；follower 出入金校正維持 `"perp"`。`send`（現貨轉帳，會改變帳戶總值）仍不在白名單——維持既有「未知型別只 warn」的方向，不在本 plan 擴充。 | 主線程定 |
| D10 | **「leader 有動作」與「leader 有曝險」只看會被鏡射的永續倉位與掛單**（主線程裁決 2026-11-23，已告知使用者）：引擎讀 leader 掛單時現貨單也一起讀到（`frontendOpenOrders` 不分現貨永續）。leader `0xfb9c…` 持有現貨 BTC，掛／撤現貨單不該觸發倍率重算，也不該讓「leader 只有一張現貨掛單」被當成有曝險。規則：`leader_fingerprint` 與 `leader_has_exposure` 都先以引擎既有的「不鏡射」條件過濾，且**倉位與掛單各照引擎實際的略過條件**（實作時查明兩者不同）：倉位濾掉現貨與非預設 dex（`_is_spot_coin(coin) or _coin_dex(coin)`，同 `positions.py` 的 `sync_positions`）；掛單只濾現貨（`_is_spot_coin(coin)`，同 `orders.py` 的 `_build_desired`——它不略過非預設 dex 的掛單）。述詞一律從 `src/spark/copytrade/instrument.py` import，不得另寫一份。 | 主線程定 |
| D7a | **波動權重在鎖定下的行為**（使用者 2026-11-23 確認；曾討論改為「每日依昨日波動定一次」，使用者不採用——原話：「反應慢一天不是我要的…leader 沒動作就完全不調整 比較即時」「如果負擔不大，那我贊成你的建議…我們照這樣改」）：leader 每次動作時以當下的波動權重（今日至今 \|PnL\| 相對 14 日基準）重算整個倍率；leader 不動的期間權重變化不反映。負擔評估見 recon §10。 | **使用者確認** |

> 下文以 D1＝A、D2＝X 撰寫（即定案）。

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
| `src/spark/copytrade/scale_latch.py` | 新增（T2b） | 倍率鎖定純函式；`ReconcileState.scale_latch` 欄位 |
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

### T2b `@inline` — 倍率鎖定（D7；依賴 T2）

**目標**：leader 沒有動作的期間，follower 用的倍率不變。**動機**：使用者要求 follower 不得因為 leader 權益每分鐘的小幅變動（leader 現貨 BTC 價格、浮動損益）而調整大小或開關單；T2 改完後分母仍會隨行情漂移，既有的 8% 容差只能降低頻率、不能消除（recon §8）。遇到歧義往「leader 沒動作就不動 follower」取捨。

**先做一個小修（D3a，獨立 commit，再做倍率鎖定）**：`src/spark/copytrade/leader_equity.py`——
`resolve_leader_equity`：`leader_has_exposure` 為 False 時不檢查新鮮度，值 ≤ 0 或過期都回 `LeaderEquity(value=max(total.value, 0)…)`（過期但值 > 0 也照回該值，反正目標全為 0）；
`read_leader_equity`：`get_account_total` 丟 `ValueError` 且 `leader_has_exposure` 為 False → 回 `LeaderEquity(value=Decimal("0"), as_of_ms=0, source="exchange_total")`，不 raise。
有曝險時的行為完全不變。測試（`tests/test_leader_equity.py`、`tests/test_copy_loop.py`）：無曝險＋過期 181 秒 → 不 raise；無曝險＋`ValueError` → 值 0、不 raise；
迴圈層：leader 無倉位無掛單、帳戶總值讀取丟 `ValueError`、follower 有一個殘留倉位與一張殘留掛單 → 該輪**有**平倉與撤單動作、沒有 `leader_equity` 類 critical、`skip_detail` 為 None。
變異：把 `read_leader_equity` 的無曝險分支拿掉 → 迴圈層那條測試轉紅。Commit：`fix: leader 無曝險時權益讀取失敗不得擋下 follower 收尾（plan 2026-11-22 D3a）`。

**新模組** `src/spark/copytrade/scale_latch.py`（純函式，無 IO）：

```python
@dataclass(frozen=True)
class ScaleLatch:
    scale: Decimal
    leader: str                 # 小寫位址
    fingerprint: tuple          # leader 曝險指紋
    settings_key: tuple         # 影響倍率的 follower 設定

def leader_fingerprint(leader_positions: Mapping[str, Position],
                       leader_orders: Sequence[OpenOrder]) -> tuple:
    """(tuple(sorted((coin, szi))), tuple(sorted((oid, coin, is_buy, size, limit_px, trigger_px, reduce_only))))。
    只取這些欄位；不得含時間戳、未實現損益、保證金等會自己變動的值。"""

def settings_key(settings: CopySettings) -> tuple:
    """(capital_utilization, allocated_capital, use_full_equity, position_weight,
    max_target_leverage, volatility_weight_enabled, leader_flow_neutralization_enabled)。"""

def apply_latch(fresh_scale: Decimal, prev: ScaleLatch | None, *, leader: str,
                fingerprint: tuple, key: tuple,
                follower_has_exposure: bool) -> tuple[Decimal, ScaleLatch | None]:
    """回傳 (本輪要用的倍率, 新的鎖定狀態)。規則依序：
    1. fresh_scale <= 0 → (fresh_scale, None)。            # 既有 scale=0 語意不變
    2. fingerprint 為空（leader 無倉位無掛單）→ (fresh_scale, None)。
    3. prev is None，或 prev.leader != leader，或 prev.settings_key != key，
       或 prev.fingerprint != fingerprint，或 not follower_has_exposure
       → 重算：(fresh_scale, ScaleLatch(fresh_scale, leader, fingerprint, key))。
    4. 其餘 → (prev.scale, prev)。"""
```

**狀態存放**：`ReconcileState`（`orders.py`）加一個欄位 `scale_latch: "ScaleLatch | None" = None`（只加欄位；注意循環 import，必要時用 `TYPE_CHECKING` 或 `Any` 註記）。只存在記憶體，不落檔。

**`loop.py` 接線**（第 4 節 `compute_scale_factor` 之後、第 5 節之前；只加這一段）：
`fresh = compute_scale_factor(...)` → `scale, state.scale_latch = apply_latch(fresh, state.scale_latch, leader=leader.lower(), fingerprint=leader_fingerprint(leader_positions, leader_orders), key=settings_key(settings), follower_has_exposure=bool(my_positions) or bool(my_orders))`。之後所有用到 `scale` 的地方（`sync_open_orders`、`sync_positions`、`CycleReport.scale`）都用鎖定後的值。`state` 為 None 的呼叫路徑（若存在）→ 視同 `prev=None` 且不保存。T2 的 unavailable 提前返回發生在這段之前，不得動到 `state.scale_latch`（下一輪恢復後照舊判斷）。

**測試**：
- `tests/test_scale_latch.py`（新）：規則 1–4 各一條以上；指紋對「倉位大小變了／多一張掛單／掛單剩餘數量變了／只有未實現損益變了（不變）」的反應；`settings_key` 對使用率變動的反應。
- `tests/test_copy_loop.py`（沿用 T2 的 helper，連跑多輪共用同一個 `ReconcileState`）：
  1. **只有分母漂移**：第 1 輪 leader −0.02 BTC、total 700 → follower 建倉；第 2 輪 leader 倉位與掛單完全相同、total 改成 560（−20%，遠超 8% 容差）、FakeAdapter 的 follower 倉位＝第 1 輪目標 → 第 2 輪 **0 筆** place／modify／cancel／調倉，`report.scale` 等於第 1 輪。
  2. **leader 有動作才重算**：承上，第 3 輪 leader 倉位變 −0.03、total 560 → `report.scale` 等於以 560 算出的新值，follower 目標依新倍率。
  3. **follower 設定變了**：leader 不變、`capital_utilization` 0.5→0.25 → 當輪重算。
  4. **follower 無曝險**：leader 不變、follower 無倉位無掛單 → 當輪用新值。
  5. **分子漂移**：leader 不變、follower `account_value` +30% → 0 筆動作。
  6. **T2 的 unavailable 一輪之後**：鎖定值保留，恢復後的那一輪 leader 不變 → 仍用舊倍率、0 筆動作。

**驗收**：
1. `uv run pytest -q` 全綠；`uv run ruff check src tests scripts` 0 錯。
2. 變異三條各至少一條測試轉紅，列出測試名後還原：(a) `apply_latch` 規則 4 改成回 `fresh_scale`（等於沒鎖）；(b) 指紋拿掉掛單部分；(c) `loop.py` 把未鎖定的 `fresh` 傳給 `sync_positions`。
3. `git diff --stat` 只含 `scale_latch.py`、`loop.py`、`orders.py`（僅 `ReconcileState` 加欄位）、兩個測試檔；`git diff -- src/spark/copytrade/positions.py` 為空。
4. Commit：`feat: 倍率鎖定——leader 沒有動作時 follower 不因權益漂移調倉（plan 2026-11-22 T2b／D7）`。

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
7. 倍率鎖定（T2b）：有沒有任何 leader 動作不會改變指紋（漏重算）；鎖定值會不會在 follower 已清空、leader 已換、設定已改之後仍被沿用；`scale ≤ 0` 與 unavailable 路徑是否維持既有語意；是否有任何下游仍拿到未鎖定的倍率。
修到 0 Critical／0 Warning 為止；只有設計分岔才停下問使用者。

### T4-fix `@inline` — 第一輪審核的修正（D8、D9＋測試缺口；依賴 T2b）

逐項各自成 commit 亦可，合成一個 commit 亦可；每項都要有「改回去就轉紅」的測試。

1. **D8 槓桿上限的縮倉方向不受鎖定**：`scale_latch.apply_latch` 增加 keyword 參數 `cap_binding: bool = False`；規則 3 的重算條件多一項 `(cap_binding and fresh_scale < prev.scale)`。`loop.py` 在呼叫處算 `cap_binding = settings.max_target_leverage > 0 and leader_equity > 0 and (leader_state.total_ntl_pos / leader_equity) > settings.max_target_leverage`（`leader_equity` 就是傳給 `compute_scale_factor` 的那個變數）。模組 docstring 的規則同步。
   測試（純函式）：cap_binding＋fresh < prev → 用 fresh 並重新鎖定；cap_binding＋fresh > prev → 仍用 prev；cap_binding=False＋fresh < prev → 仍用 prev。
   測試（迴圈，錨例出自審核實跑）：`max_target_leverage=10`、follower `capital_utilization` 使第 1 輪倍率＝0.5（leader 空 1 BTC、帳戶總值 8,000、mark 100,000 → 有效槓桿 12.5 倍，上限已生效；follower 權益 10,000）；第 2 輪 leader 倉位掛單不變、mark 105,000、leader 帳戶總值 3,000、follower 權益 7,500 → `report.scale` ≈ 0.3571（±0.0005），不是 0.5，且 follower 有一筆減倉。第 3 輪 leader 帳戶總值回到 8,000、mark 100,000、其餘不變 → `report.scale` 仍是第 2 輪的值、0 筆動作（不加回）。
   變異：拿掉 `cap_binding` 條件 → 迴圈測試轉紅。
2. **D9 中性化流量基準**：`src/spark/exchange/ledger_flows.py` 的 `signed_flow`／`flow_anomaly` 增加 keyword 參數 `basis: str = "perp"`；`basis == "total"` 時 `accountClassTransfer` → `signed_flow` 回 `Decimal("0")`（中性）且 `flow_anomaly` 不回報異常；其他型別兩種 basis 行為相同；`basis` 不是 `"perp"`／`"total"` → `ValueError`。`ExchangeAdapter.get_ledger_flows(address, start_ms, *, basis="perp")`（`base.py`、`hyperliquid.py`、`fakes.py` 同步；值為 0 的流量不放進回傳清單）。`loop.py` 3.5 節的呼叫改 `basis="total"`；`follower_flow.py`、`vault_preflight.py` 等其他呼叫端**不改**（維持預設）。該檔檔頭寫明三端共用——改完三端測試都要綠（`tests/test_ledger_flows.py`、`tests/test_follower_flow.py`、vault preflight 的測試）。
   測試：`signed_flow` 對 `accountClassTransfer` 在兩種 basis 的值；`"total"` 下不列 unknown／anomaly；非法 basis raise；迴圈層——中性化開啟、leader 有一筆 `accountClassTransfer` toPerp 2,000（FakeAdapter 以原始 delta 或既有手法餵入，依該檔既有寫法）、帳戶總值 10,000 → `adjusted_leader_equity` 收到的流量清單不含它（`report.scale` 以 10,000 為分母，不是 8,000）。
   變異：`loop.py` 的呼叫拿掉 `basis="total"` → 迴圈測試轉紅。
3. **Warning 3 測試缺口**：迴圈測試——leader **只有掛單、沒有倉位**，帳戶總值過期（或 `ValueError`）→ 走 unavailable（零動作＋critical），不是 D3a 的收尾路徑；follower 既有的鏡射掛單沒有被撤。變異：`leader_has_exposure` 拿掉 `bool(leader_orders)` → 此測試轉紅。
4. **Suggestion——心跳 `detail`**：在 `tests/test_run_copytrade_*.py` 既有的 wiring 測試旁加一條：`run_cycle` 回 `skip_detail="X"` 的報告時，發布的心跳 `detail == "X"`。變異：該行改回 `None` → 轉紅。
5. **防禦性收斂（審核「觀察」中成本極低的兩項）**：(a) `resolve_leader_equity`：`total.as_of_ms − now_ms > MAX_AGE_MS`（時間戳在未來超過 180 秒）且 leader 有曝險 → `LeaderEquityUnavailable("帳戶總值時間戳在未來…")`；無曝險不 raise（D3a）。(b) `HyperliquidAdapter.get_account_total`：`row[0]` 不是 `str` 的列直接略過（避免不可 hash 型別丟 `TypeError`）。各一條測試。

**範圍**：`scale_latch.py`、`loop.py`、`leader_equity.py`、`src/spark/exchange/{ledger_flows,base,hyperliquid,fakes}.py`、對應測試檔。`positions.py`、`sizing.py`、`follower_flow.py` 的邏輯不動。既有斷言不准改。
**驗收**：`uv run pytest -q` 全綠；ruff 0 錯；上列五項的變異逐條列出轉紅的測試名並還原；`git diff --stat` 只含範圍內檔案。

### T4-fix2 `@inline` — 指紋與曝險只看會被鏡射的永續（D10）

`src/spark/copytrade/scale_latch.py` 的 `leader_fingerprint`：倉位與掛單都先濾掉 `_is_spot_coin(coin) or _coin_dex(coin)` 為真者（述詞從 `spark.copytrade.instrument` import，不得複製邏輯）。
`src/spark/copytrade/loop.py` 的 `leader_has_exposure`：同一個過濾後再判斷是否非空。為避免兩處各寫一次過濾，在 `scale_latch.py`（或 `instrument.py`，擇一，回報說明）提供一個小函式 `mirrorable(coin) -> bool`，兩處共用。
測試：(1) 純函式——leader 多一張現貨掛單（coin 形如 `@107` 或 `UBTC/USDC`，以 `instrument.py` 認得的寫法為準）、或其剩餘量改變 → 指紋不變；多一張永續掛單 → 指紋改變；非預設 dex 的倉位（coin 形如 `xyz:NVDA`）不進指紋。(2) 迴圈——leader 倉位掛單不變、只多了一張現貨掛單、帳戶總值 −20% → 0 筆動作、`report.scale` 不變。(3) 迴圈——leader **只有**一張現貨掛單、無永續倉位掛單、帳戶總值過期 → 不發 `leader_equity` critical、`skip_detail is None`（走 D3a）。既有 `test_W3_*`（leader 只有一張**永續**掛單）必須仍綠。
變異：(a) 指紋不過濾 → 測試 (1)(2) 轉紅；(b) `leader_has_exposure` 不過濾 → 測試 (3) 轉紅。
驗收：`uv run pytest -q` 全綠（≥ 3911）；ruff 0 錯；`git diff --stat` 只含 `scale_latch.py`（或 `instrument.py`）、`loop.py`、兩個測試檔；`positions.py`、`orders.py` 零改動。Commit：`fix: 倍率鎖定的指紋與 leader 曝險只看會被鏡射的永續（plan 2026-11-22 D10）`。

### T4-fix3 `@inline` — 第二輪審核的測試缺口（只加測試，不改 `src/`）

1. **D8 × 流量中性化**（`tests/test_copy_loop.py`）：`leader_flow_neutralization_enabled=True`、`max_target_leverage=10`、波動權重關。leader 空 1 BTC、`total_ntl_pos` 100,000、帳戶總值 12,000、衰減窗內有一筆剛發生的入金流量 4,000（權重≈1，使中性化後分母≈8,000）；follower 權益 10,000、`capital_utilization=0.5`。
   第 1 輪：有效槓桿以**中性化後**分母計＝12.5 > 10 → `report.scale` ≈ 5,000÷8,000×(10÷12.5)＝0.5（±0.005，容許流量衰減的微小時間差——或把時鐘釘住使其精確）。
   第 2 輪：leader 倉位掛單不變、`total_ntl_pos` 105,000、帳戶總值 7,000（中性化後≈3,000）、follower 權益 7,500 → `report.scale` ≈ 3,750÷3,000×(10÷35)＝0.3571（±0.005），不是 0.5，且有一筆 follower 減倉。
   關鍵斷言設計：第 2 輪若以**未中性化**的 7,000 判斷上限，有效槓桿＝105,000÷7,000＝15 > 10 仍為 True——這樣抓不到變異。所以另加**第 2' 情境**（獨立一條測試或同測試的另一組數字）：帳戶總值 11,000、入金流量 4,000（中性化後 7,000）、`total_ntl_pos` 100,000 → 以中性化後分母有效槓桿 14.3 > 10（上限生效），以原值 100,000÷11,000＝9.09 < 10（未生效）；先用一輪建立較大的鎖定值，再進入此狀態且新倍率較小 → 必須重算。
   變異：`loop.py` 的 `cap_binding` 改用 `leader_eq.value`（未中性化）→ 第 2' 情境的測試轉紅。
2. **非預設 dex 掛單算可鏡射**（`tests/test_scale_latch.py`）：`leader_has_mirrorable_exposure({}, [一張 coin="xyz:NVDA" 的掛單])` 為 True；該掛單會進指紋（多這張單 → 指紋改變）。對照：`xyz:NVDA` 的**倉位**不算曝險、不進指紋（既有測試已涵蓋，新增測試放在它旁邊並在註解寫明不對稱的來源是 `positions.py` 與 `orders.py` 的略過條件不同）。
   變異：`_order_mirrored` 加上 `_coin_dex` 過濾 → 新測試轉紅。
驗收：兩條變異各列出轉紅的測試名並還原；`uv run pytest -q tests/test_copy_loop.py tests/test_scale_latch.py` 全綠；`uv run pytest -q` 全綠；ruff 0 錯；`git diff --stat HEAD~1` 只有這兩個測試檔。Commit：`test: D8 在流量中性化開啟下的上限判定、非預設 dex 掛單算可鏡射（plan 2026-11-22 T4-fix3）`。

### T4-fix4 `@inline` — 第三輪審核的測試缺口（只加測試，不改 `src/`）

背景：`tests/test_copy_loop.py` 的 `_LatchWorld` 讓 leader 持有 −0.02 BTC，卻把 leader 的 `total_ntl_pos` 設為 0（實盤不可能），而且鎖定測試只測了分母變小的方向；所以 `loop.py` 中 `cap_binding` 的兩個守衛沒有被測到。

1. **讓 `_LatchWorld` 貼近實盤**：leader 的 `AccountSnapshot.total_ntl_pos` 預設改為與其倉位一致的非零名目（例如 −0.02 BTC × 該檔使用的 mark；若既有測試以明確參數覆寫則尊重覆寫）。此改動不得改任何既有斷言；若有既有測試因此轉紅，停下回報（那代表它依賴了不可能的狀態）。
2. **守 `settings.max_target_leverage > 0`**（變異 M3）：`max_target_leverage=0`（預設）、leader 倉位掛單不變、名目 > 0。第 1 輪帳戶總值 700 建倉並鎖定；第 2 輪帳戶總值 840（＋20%，新算倍率變**小**）、follower 倉位＝第 1 輪目標 → `report.scale` 等於第 1 輪、0 筆 place／modify／cancel／調倉。再加一輪 follower 權益 −20%（新算倍率變小的另一個來源）→ 同樣不動。
   變異：`loop.py` 的 `cap_binding` 拿掉 `settings.max_target_leverage > 0 and` → 本測試轉紅。
3. **守 `leader_equity > 0`**（變異 M5）：`max_target_leverage=20`、leader 在可鏡射產品上無倉位無掛單、帳戶總值讀取丟 `ValueError`（D3a 路徑，權益為 0），follower 有一個殘留倉位 → 該輪不拋例外、殘留倉位被平掉。再一條：leader 無曝險、帳戶總值為 0。
   變異：`loop.py` 的 `cap_binding` 拿掉 `leader_equity > 0 and` → 本測試以 `decimal.InvalidOperation` 轉紅。
驗收：兩條變異各列出轉紅的測試名並還原；`uv run pytest -q` 全綠；ruff 0 錯；`git diff --stat HEAD~1` 只有 `tests/test_copy_loop.py`。Commit：`test: cap_binding 的上限啟用與正權益守衛（plan 2026-11-22 T4-fix4）`。

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
- **R8 倍率鎖定的後果（D7）**：follower 入金／出金、leader 入金／出金，都要等 leader 下一次動作（倉位或掛單有變）才反映到 follower 倉位大小；leader 抱單不動期間，即使 leader 權益大幅變化 follower 也不調整。重啟（含每次部署）後第一輪會以當下倍率重算，漂移超過 8% 時有一次調整。網格型 leader 幾乎每分鐘都有掛單變動，鎖定對它等於沒有作用（行為同現在，受 8% 容差約束）。鎖定期間**波動權重也凍結**（每日波動變化不會讓 follower 縮放，leader 下次動作才套用）；**槓桿上限只保留縮倉方向**（D8：leader 有效槓桿超過上限時 follower 仍會被縮，但之後不自動加回）。

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
