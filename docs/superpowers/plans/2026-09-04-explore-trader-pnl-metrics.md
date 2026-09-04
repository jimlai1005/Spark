# 探索清單與交易員詳情頁指標統一（PnL 金額＋權益指數回撤）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 探索頁清單（`/explore`）與交易員詳情頁（`/traders/[address]`）改吃同一個純函式模組算出的同一組指標：期間損益改用**金額**（HL `pnlHistory` 末值−首值）、最大回撤改用**權益指數**（`leader_perf`）並加兩道無效閘門、成交統計改用 Hyperbot 已驗證的定義（distinct 訂單數／部位歸零生命週期／生命週期勝率／Σ closedPnl），兩頁數字逐位一致。

**Architecture:** 新增 `src/spark/filet/trader_stats.py`（零網路純函式）：`window_stats()` 吃 `portfolio()` 原始回應算單窗 `WindowStats(pnl_usd, max_dd_pct|None, max_dd_reason, spark)`；`fills_stats()` 吃 fills 清單算 `FillsStats`；`live_days_from_av()` 算實盤天數。`hl_explore.enrich_candidate` 與 `app.public_trader_detail` 都只呼叫這個模組，各自不再有自己的公式。`leader_perf.compute_window_performance` 加兩道閘門（任一區間 `r_t <= -1` → `flow_dominated_interval`；跳過區間比例 > 30% → `too_many_skipped_intervals`），回傳既有的 `insufficient` 形狀。前端：探索表格「報酬率」欄改「損益（USD）」、sparkline 改畫損益曲線；詳情頁改成與清單相同的四窗切換＋同一組欄位，**保留** TWR 衍生指標（Sharpe／Sortino／年化波動／日勝率／最佳最差日）但改為逐窗計算、跟著所選窗切換，CAGR 維持 allTime；算不出的窗由既有 `*_insufficient` 標記顯示「樣本不足」——策略頁（`/strategies*`）**不動**。

**Tech Stack:** Python 3.11 + uv + pytest（離線，autouse socket-ban）；Next.js + vitest。測試錨例來自真實地址 `0x6648f8dd041ed689de7bf501efb3b827cf15b1f3` 的 fixture（已與 Hyperbot 逐位對帳：訂單 221／平倉 27／勝 15／已實現 $40,225.792264／month PnL $33,055.25879）。

---

## 背景（builder 必讀，三分鐘）

- 根因調查結論（2026-09-04 主線程）：探索頁 `_return_and_drawdown` 用原始 `accountValueHistory` 算報酬與回撤，含出入金 → 「入金→交易→提光」型帳戶算出 +10¹² % 與 −100%。詳情頁用 `leader_perf` TWR，但 `allTime` 窗降採樣 6–9 天一點，入金與虧損落在同一區間 → `r_t < -1` → 權益指數轉負 → TWR −1938%／MDD 10109%。兩頁公式、窗口都不同。
- 使用者裁決（2026-09-04）：(1) 報酬率改**金額**；(4) 回撤走 **HL 公開序列**（不接 Hyperbot API）。詳情頁一併更新，兩頁數字必須一致。
- Hyperbot 定義（已用 HL 資料精確重現，見 memory `hyperbot-metrics-reference`）：
  - Trades ＝ `userFillsByTime` 的 **distinct `oid`**（不是 fills 數）。
  - Closed Positions ＝ 部位歸零的生命週期數：`dir` 以 `Close` 開頭且 `|startPosition| == sz`，**或** `dir` 含 `>`（翻倉 `Short > Long`／`Long > Short`）。
  - Win Rate ＝ 生命週期累積 `closedPnl > 0` 的比例。
  - Total PnL（統計卡）＝ Σ `closedPnl`（未扣手續費／funding）；圖表 PnL ＝ HL `pnlHistory` 末值。
  - Max Drawdown 對不上（他們的輸入是自家快照），本 plan 用權益指數 MDD 並在 UI 標明定義。
- 工程原則 1（同源同基準）：同一窗的 `pnl_usd`／`max_dd_pct`／`spark` 全部出自同一次 `portfolio()` 回應的同一個窗。
- 紅線：測試全離線；不讀 `.env*`；不碰 `/Users/jim/projects/hl-copytrader`。
- 指令：後端測試 `uv run pytest`；lint `uv run ruff check src tests scripts`；前端 `export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH" && cd web && npm test`。
- Commit 格式：`feat:`/`fix:`/`test:`/`docs:` 一行，結尾加 `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`。

## 檔案地圖

| 檔案 | 動作 | 責任 |
|---|---|---|
| `tests/fixtures/trader_stats_0x6648_portfolio.json` | 新增 | 真實 `portfolio()` 回應（四合併窗＋四 perp 窗） |
| `tests/fixtures/trader_stats_0x6648_fills30d.json` | 新增 | 真實 30 天 fills（1254 筆，已裁欄位） |
| `src/spark/filet/leader_perf.py` | 修改 | 兩道新閘門（Task 1） |
| `src/spark/filet/trader_stats.py` | 新增 | `WindowStats`／`window_stats`／`FillsStats`／`fills_stats`／`live_days_from_av`／`downsample` |
| `tests/test_trader_stats.py` | 新增 | fixture 錨例測試 |
| `src/spark/publicapi/hl_explore.py` | 修改 | 刪自家公式，改呼叫 `trader_stats`；`ExploreRow` 欄位；`sort_key`；分頁 fills；版本 3 |
| `tests/test_public_explore.py` | 修改 | 跟隨欄位／公式改動 |
| `src/spark/publicapi/app.py` | 修改 | `/api/public/traders/{address}` 新回傳形狀；`_cached_trader_data` 多抓 fills |
| `tests/test_public_trader_detail.py`（或既有同主題測試檔） | 修改/新增 | 詳情端點形狀＋與 explore 同值 |
| `web/src/lib/publicApi.ts` | 修改 | TS 型別 |
| `web/src/lib/copy.ts` | 修改 | 文案（zh/en 對稱） |
| `web/src/app/explore/page.tsx` ＋ `page.test.tsx` | 修改 | 欄位與格式 |
| `web/src/components/PnlCurve.tsx` | 新增 | 損益曲線（帶零線） |
| `web/src/app/traders/[address]/page.tsx` ＋ `page.test.tsx` | 修改 | 四窗＋同組欄位；比率型指標網格逐窗；CAGR 保留；保留跟單面板 |

## 主線程裁決（builder 不得改，遇到衝突回報）

- D1 `WindowStats.max_dd_pct` 慣例：**≤ 0 的 float 或 `None`**（沿探索頁既有負值慣例）；`None` 時 `max_dd_reason` 必填（`leader_perf` 的 `reason` 字串原樣）。
- D2 探索排序鍵改為**所選窗 `pnl_usd` 降冪**（缺該窗退回 `month`）；風險由「最大回撤 <」chip 過濾，不再做 ret/dd 比值。
- D3 `fill_count_30d` 改名 `order_count_30d`（語意＝distinct 訂單）；HTTP 查詢參數 `min_fills` **名稱不變**（契約），門檻比對對象改成 `order_count_30d`。
- D4 成交統計只算 **perp fills**（`dir` ∈ `{Open Long, Open Short, Close Long, Close Short, Long > Short, Short > Long}`），spot 成交（`Buy`/`Sell`/`Spot Dust Conversion`）一律排除。
- D5 fills 改走分頁；`ExploreConfig.fills_max_pages` 預設 **3**（≤ 6000 筆）；詳情端點同值。
  ⚠️ 2026-09-05 修正（Task 3 builder 發現）：`hl.get_fills_detail_paged` 最後一步經 `_fill_detail_dict` 裁切，**丟掉了 `dir`／`oid`／`startPosition`、`closedPnl` 改名 `closed_pnl`**，而 `trader_stats.fills_stats` 需要原始 HL 形狀（fixture 也是原始形狀）。裁決：在 `hl.py` 新增公開方法 `get_fills_raw_paged(address, start, end, *, max_pages=None) -> tuple[list[dict], bool]`，直接回傳 `_paged_fills_raw` 的原始 dict（不裁切），探索與詳情端點一律呼叫它；`get_fills_detail_paged` 與 `/api/me/fills` 不動。見 Task 3a。
- D6（2026-09-04 使用者否決移除版，改為保留）詳情頁**保留** `metrics`（`build_metrics` 輸出：TWR/MDD/Sharpe/Sortino/年化波動/日勝率/最佳最差日）與 `cagr_pct`/`sample_days`/`sample_threshold`，但 `metrics` 改成**逐窗**：`metrics: {day, week, month, allTime}`，每窗各自 `build_metrics(compute_window_performance(rows, period))`；CAGR 只算 allTime（年化需 ≥ 90 天）。前端指標網格只渲染比率型指標（Sharpe/Sortino/年化波動/日勝率/最佳最差日），**不重複渲染** `total_return_pct`／`max_drawdown_pct`——損益與回撤由窗卡（`windows[w]`）顯示，避免同頁兩個回撤數字（窗卡的 `max_dd_pct` 是該窗事實上的峰谷跌幅，`metrics` 的 `max_drawdown_pct` 在 < 30 天窗會被 `*_insufficient` 標成樣本不足，兩者語意不同）。`equity_index` 移除（由損益曲線 `windows[w].spark` 取代）。策略頁與 `strategies.py` 不動。
- D9 **不做 7D 退路**：2026-09-04 抽樣探索池前 40 地址，30D 算不出的 13 列在 7D 下**全部**仍算不出（跳過區間比例 90% 以上），24H 只救回 2 列且 24H 對回撤無意義。算不出就顯示「—」＋原因，不用別的窗冒充。
- D10 詳情頁**預設窗改為 `month`**（原 allTime）：同一抽樣 allTime 有 27/40 列算不出（降採樣 6–9 天一點，入金與損益同區間），其中 15 列在 30D 是好的；30D 是最穩的窗，且與探索清單預設一致。`?window=` 可切四窗。
- D7 `EXPLORE_INDEX_VERSION` 2 → **3**（部署後索引強制重建，期間 `building: True`）。
- D8 閘門常數 `MAX_SKIPPED_RATIO = Decimal("0.30")`，先檢查 `flow_dominated_interval`，再檢查跳過比例。

---

### Task 0: Fixtures 落地 `@sdd`

**Files:**
- Create: `tests/fixtures/trader_stats_0x6648_portfolio.json`
- Create: `tests/fixtures/trader_stats_0x6648_fills30d.json`

- [ ] **Step 1: 複製 fixture**

```bash
SP=/private/tmp/claude-501/-Users-jim-projects-spark/328acfa9-017c-4201-b1a2-20d26d0a5423/scratchpad
cp "$SP/fixture_portfolio_0x6648.json" tests/fixtures/trader_stats_0x6648_portfolio.json
cp "$SP/fixture_fills30d_0x6648.json"  tests/fixtures/trader_stats_0x6648_fills30d.json
```

- [ ] **Step 2: 驗收**

Run: `python3 -c "import json;p=json.load(open('tests/fixtures/trader_stats_0x6648_portfolio.json'));f=json.load(open('tests/fixtures/trader_stats_0x6648_fills30d.json'));print(len(p),len(f['fills']),len({x['oid'] for x in f['fills']}))"`
Expected: `8 1254 221`

- [ ] **Step 3: Commit**

```bash
git add tests/fixtures/trader_stats_0x6648_portfolio.json tests/fixtures/trader_stats_0x6648_fills30d.json
git commit -m "test: 新增 0x6648 portfolio/fills fixture（trader_stats 錨例）"
```

---

### Task 1: `leader_perf` 兩道無效閘門 `@inline`

**Files:**
- Modify: `src/spark/filet/leader_perf.py`（`compute_window_performance`，約 373–396 行的分段報酬迴圈）
- Test: `tests/test_leader_perf.py`

- [ ] **Step 1: 寫失敗測試**（附在 `tests/test_leader_perf.py` 末尾；`_rows` 這類 helper 若檔內已有就沿用，否則用下面的 `_portfolio`）

```python
from decimal import Decimal
from spark.filet.leader_perf import (compute_window_performance, MAX_SKIPPED_RATIO,
                                     STATUS_INSUFFICIENT)


def _portfolio(period, av, pnl):
    ts = [1_700_000_000_000 + i * 3_600_000 for i in range(len(av))]
    return [[period, {"accountValueHistory": [[t, str(a)] for t, a in zip(ts, av)],
                      "pnlHistory": [[t, str(p)] for t, p in zip(ts, pnl)]}]]


def test_flow_dominated_interval_marks_window_insufficient():
    # 前值 1000，同一區間入金 5000 且虧 1500：r = -1500/1000 = -1.5 <= -1 → 整窗無效
    rows = _portfolio("month", av=[1000, 4500, 4600], pnl=[0, -1500, -1400])
    perf = compute_window_performance(rows, "month")
    assert perf["status"] == STATUS_INSUFFICIENT
    assert perf["reason"] == "flow_dominated_interval"
    assert "twr" not in perf and "max_drawdown" not in perf


def test_too_many_skipped_intervals_marks_window_insufficient():
    # 10 點 → 9 區間；av[0..4] = 10 → 區間 i=1..5 的前值 < 100（跳過 5 段）→ 5/9 = 0.556 > 0.30
    av = [10, 10, 10, 10, 10, 1000, 1010, 1020, 1030, 1040]
    pnl = [0, 0, 0, 0, 0, 0, 10, 20, 30, 40]
    perf = compute_window_performance(_portfolio("month", av, pnl), "month")
    assert perf["status"] == STATUS_INSUFFICIENT
    assert perf["reason"] == "too_many_skipped_intervals"
    assert perf["skipped_intervals"] == 5


def test_exact_total_loss_is_not_flow_dominated():
    # r == -1（上一期淨值全部虧光，無入金）是合法的歸零，不是資金流主導：狀態 ok、TWR = -1
    rows = _portfolio("month", av=[1000, 0], pnl=[0, -1000])
    perf = compute_window_performance(rows, "month")
    assert perf["status"] == "ok" and perf["twr"] == Decimal("-1")


def test_skipped_ratio_exactly_at_threshold_passes():
    # 11 點 → 10 區間；3 個跳過 = 0.30，不大於門檻 → ok
    av = [10, 10, 10, 1000, 1010, 1020, 1030, 1040, 1050, 1060, 1070]
    pnl = [0, 0, 0, 0, 10, 20, 30, 40, 50, 60, 70]
    perf = compute_window_performance(_portfolio("month", av, pnl), "month")
    assert perf["status"] == "ok"
    assert perf["skipped_intervals"] == 3
    assert MAX_SKIPPED_RATIO == Decimal("0.30")
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_leader_perf.py -k "flow_dominated or skipped" -v`
Expected: 3 FAIL（`ImportError: MAX_SKIPPED_RATIO` 或 status == "ok"）

- [ ] **Step 3: 實作**

在常數區（`DENOMINATOR_FLOOR` 下方）加：

```python
# 2026-09-04 閘門 4／5（explore/detail 指標統一 plan，D8）：
# - 任一區間 r_t <= -1（虧損 >= 上一期 AV）代表入金與虧損落在同一取樣區間，
#   權益指數會轉負、之後全是垃圾——整窗判無效（reason="flow_dominated_interval"）。
#   allTime 窗降採樣 6–9 天一點時常見（實證 0xbf73…5d58 2024-12-25 區間 r=-1.36）。
# - 被分母地板跳過的區間比例 > MAX_SKIPPED_RATIO 代表這顆帳戶大半時間淨值 < 100
#   USDC（入金→交易→提光型），剩下的區間不足以代表整窗（reason="too_many_skipped_intervals"）。
MAX_SKIPPED_RATIO = Decimal("0.30")
```

在 `compute_window_performance` 的迴圈裡，`r = d_pnl / prev_av` 成功算出之後、`equity_index.append(...)` 之前加：

```python
        if r < Decimal("-1"):
            # 嚴格小於：r == -1 是「這段把上一期淨值全部虧光」，無需入金即可發生
            # （既有測試 test_annualized_markers_never_appear_without_the_number 就是這個
            # 案例，TWR = −1 是正確答案）；r < -1 代表虧損 > 上一期淨值，沒有同區間入金
            # 在數學上不可能，才是「資金流主導」的證據。
            logger.warning("portfolio %s 窗第 %d 區間 r=%s < -1（prev_av=%s, d_pnl=%s）"
                           "——入金與虧損同區間，整窗判無效", period, i, r, prev_av, d_pnl)
            return _insufficient(period, "flow_dominated_interval", sample_count=len(pnl))
```

迴圈結束後、`cum_pnl = ...` 之前加：

```python
    if Decimal(skipped) > MAX_SKIPPED_RATIO * Decimal(len(pnl) - 1):
        out = _insufficient(period, "too_many_skipped_intervals", sample_count=len(pnl))
        out["skipped_intervals"] = skipped
        return out
```

- [ ] **Step 3b: 調整既有測試 `test_denominator_floor_blocks_exploding_returns` 的 fixture**（主線程 2026-09-05 裁決：原 3 點序列只有 2 個區間，1 個被地板跳過就是 50% > 30%，會被新的比例閘門判無效；這條測試的目的是驗「地板擋住爆炸報酬」，補兩個平盤區間讓比例降到 25%，斷言值不變）：

```python
def test_denominator_floor_blocks_exploding_returns():
    """提領到近乎 0 後的小額交易不得產生爆炸性報酬；跳過的區間要誠實計數。
    2026-09-05：補兩個平盤點（r=0），讓跳過比例 1/4 = 25% 落在 MAX_SKIPPED_RATIO 之內，
    這條測的是地板，不是比例閘門（比例閘門另有 test_too_many_skipped_intervals_*）。"""
    r = compute_window_performance(
        rows([(0, "10000", "0"), (20, "5", "-9995"), (40, "105", "-9895"),
              (60, "105", "-9895"), (80, "105", "-9895")]),
        "perpMonth")
    assert r["skipped_intervals"] == 1          # 第二段分母 5 < 100，不計入
    # 若不設地板，第二段 r = 100/5 = +2000%，TWR 會從 −99.95% 反彈成正的。
    assert r["twr"] == Decimal("-0.9995")
```

- [ ] **Step 4: 跑全檔測試**

Run: `uv run pytest tests/test_leader_perf.py tests/test_public_strategies.py -q`
Expected: 全綠。若 `test_public_strategies.py` 有既有 fixture 因新閘門翻成 insufficient 而紅，**不要放寬閘門**，回報主線程（該 fixture 本身可能就是含大現金流的假資料）。

- [ ] **Step 5: Commit**

```bash
git add src/spark/filet/leader_perf.py tests/test_leader_perf.py
git commit -m "fix: leader_perf 加 flow_dominated_interval／too_many_skipped_intervals 兩道閘門"
```

---

### Task 2: `trader_stats` 純函式模組 `@inline`

**Files:**
- Create: `src/spark/filet/trader_stats.py`
- Test: `tests/test_trader_stats.py`

- [ ] **Step 1: 寫失敗測試**

```python
import json
from pathlib import Path

import pytest

from spark.filet.trader_stats import (FillsStats, WindowStats, downsample, fills_stats,
                                      live_days_from_av, window_stats)

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def portfolio():
    return json.load(open(FIX / "trader_stats_0x6648_portfolio.json"))


@pytest.fixture(scope="module")
def fills():
    return json.load(open(FIX / "trader_stats_0x6648_fills30d.json"))["fills"]


# --- window_stats：錨例來自真實 0x6648（2026-09-04 抓取），與 Hyperbot 圖表末值逐位一致 ---
def test_window_stats_month_pnl_usd_and_spark_from_pnl_history(portfolio):
    ws = window_stats(portfolio, "month")
    assert isinstance(ws, WindowStats)
    assert ws.pnl_usd == 33055.26                    # pnlHistory 末值 33055.25879 − 首值 0，quantize 0.01
    assert ws.max_dd_pct is None
    assert ws.max_dd_reason == "too_many_skipped_intervals"   # 31/56 區間淨值 < 100 USDC
    assert len(ws.spark) == 30
    assert ws.spark[0] == 0.0 and ws.spark[-1] == 33055.25879


def test_window_stats_day_has_drawdown(portfolio):
    ws = window_stats(portfolio, "day")
    assert ws.pnl_usd == -2181.94
    assert ws.max_dd_pct == pytest.approx(-74.07, abs=0.01)   # 權益指數 MDD，負值慣例（D1）
    assert ws.max_dd_reason is None
    assert len(ws.spark) == 21                       # 21 點 <= 30，不補點


def test_window_stats_all_time_is_flow_dominated(portfolio):
    ws = window_stats(portfolio, "allTime")
    assert ws.pnl_usd == 27504.48
    assert ws.max_dd_pct is None
    assert ws.max_dd_reason == "flow_dominated_interval"


def test_window_stats_week(portfolio):
    ws = window_stats(portfolio, "week")
    assert ws.pnl_usd == 764.18
    assert ws.max_dd_reason == "too_many_skipped_intervals"


def test_window_stats_missing_window_is_none():
    assert window_stats([["day", {"accountValueHistory": [], "pnlHistory": []}]], "month") is None


def test_window_stats_single_point_is_none():
    rows = [["month", {"accountValueHistory": [[1, "10"]], "pnlHistory": [[1, "0"]]}]]
    assert window_stats(rows, "month") is None


def test_window_stats_to_dict_shape(portfolio):
    d = window_stats(portfolio, "month").to_dict()
    assert set(d) == {"pnl_usd", "max_dd_pct", "max_dd_reason", "spark"}
    assert d["max_dd_pct"] is None and isinstance(d["spark"], list)


# --- live_days ---
def test_live_days_from_all_time_calendar_span(portfolio):
    av = dict(portfolio)["allTime"]["accountValueHistory"]
    pts = [(int(t), v) for t, v in av]
    assert live_days_from_av(pts) == 1003   # 2023-12-07 → 2026-09-04（fixture 末點）


def test_live_days_empty_is_zero():
    assert live_days_from_av([]) == 0


# --- downsample ---
def test_downsample_keeps_short_series_and_caps_long():
    assert downsample([1.0, 2.0, 3.0], n=30) == [1.0, 2.0, 3.0]
    out = downsample([float(i) for i in range(100)], n=30)
    assert len(out) == 30 and out[0] == 0.0 and out[-1] == 99.0


# --- fills_stats：錨例與 Hyperbot query-addr-stat period=30 逐位一致 ---
def test_fills_stats_matches_hyperbot_definitions(fills):
    fs = fills_stats(fills, truncated=False)
    assert isinstance(fs, FillsStats)
    assert fs.order_count == 221           # distinct oid
    assert fs.closed_positions == 27       # 部位歸零生命週期（含翻倉 Short > Long）
    assert fs.wins == 15
    assert fs.win_rate_pct == 55.56        # 15/27，quantize 0.01
    assert fs.realized_pnl_usd == 40225.79 # Σ closedPnl
    assert fs.truncated is False
    assert len(fs.coins) <= 3 and 0 <= fs.concentration_pct <= 100


def test_fills_stats_excludes_spot_fills():
    perp = [{"coin": "BTC", "oid": 1, "dir": "Open Long", "startPosition": "0", "sz": "1", "px": "100",
             "closedPnl": "0", "time": 1},
            {"coin": "BTC", "oid": 2, "dir": "Close Long", "startPosition": "1", "sz": "1", "px": "110",
             "closedPnl": "10", "time": 2}]
    spot = [{"coin": "PURR/USDC", "oid": 3, "dir": "Buy", "startPosition": "0", "sz": "5", "px": "1",
             "closedPnl": "0", "time": 3}]
    fs = fills_stats(perp + spot, truncated=False)
    assert fs.order_count == 2 and fs.closed_positions == 1 and fs.wins == 1
    assert fs.win_rate_pct == 100.0 and fs.realized_pnl_usd == 10.0


def test_fills_stats_flip_counts_as_close_and_partial_close_does_not():
    f = [{"coin": "ETH", "oid": 1, "dir": "Open Long", "startPosition": "0", "sz": "2", "px": "100",
          "closedPnl": "0", "time": 1},
         {"coin": "ETH", "oid": 2, "dir": "Close Long", "startPosition": "2", "sz": "1", "px": "90",
          "closedPnl": "-10", "time": 2},                      # 部分平倉，不算生命週期結束
         {"coin": "ETH", "oid": 3, "dir": "Long > Short", "startPosition": "1", "sz": "3", "px": "120",
          "closedPnl": "20", "time": 3}]                       # 翻倉：關掉剩餘 1 顆，算一次
    fs = fills_stats(f, truncated=False)
    assert fs.closed_positions == 1 and fs.wins == 1          # 累積 -10 + 20 = +10 > 0
    assert fs.order_count == 3


def test_fills_stats_no_closes_win_rate_none():
    f = [{"coin": "ETH", "oid": 1, "dir": "Open Long", "startPosition": "0", "sz": "2", "px": "100",
          "closedPnl": "0", "time": 1}]
    fs = fills_stats(f, truncated=True)
    assert fs.closed_positions == 0 and fs.win_rate_pct is None and fs.truncated is True


def test_fills_stats_empty():
    fs = fills_stats([], truncated=False)
    assert fs.order_count == 0 and fs.realized_pnl_usd == 0.0 and fs.coins == () \
        and fs.concentration_pct is None
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_trader_stats.py -q`
Expected: 全部 ERROR/FAIL（`ModuleNotFoundError: spark.filet.trader_stats`）

- [ ] **Step 3: 實作 `src/spark/filet/trader_stats.py`**

```python
"""src/spark/filet/trader_stats.py
探索清單（/api/public/explore）與交易員詳情（/api/public/traders/{address}）**共用**
的指標純函式。零網路、零檔案 IO。兩個端點的每一個數字都只能從這裡出來——
這是 2026-09-04 事故（兩頁各自一套公式、各自錯法不同）的結構性修法：
一個函式、一組錨例測試，兩頁自然一致。

單窗指標（`window_stats`）：
- `pnl_usd`      ＝ 該窗 `pnlHistory` 末值 − 首值。HL 官方定義 pnlHistory 已扣除出入金
                   （P(t) = AV(t) − F(t)），含未實現損益、funding、手續費——與 Hyperbot
                   圖表「Total PnL」逐位一致（實證 0x6648 perpWeek 764.19）。
- `max_dd_pct`   ＝ `leader_perf.compute_window_performance` 的權益指數 MDD ×100，取負值
                   （≤ 0；沿探索頁既有慣例）。perf 非 ok → `None`，`max_dd_reason` 帶
                   leader_perf 的 reason（`flow_dominated_interval`／`too_many_skipped_intervals`
                   ／`need_at_least_two_samples`…），前端顯示「—」並可 tooltip 原因。
                   永不算在 accountValue 上（leader_perf 檔頭閘門 2）。
- `spark`        ＝ 同一 `pnlHistory` 等距降採樣 ≤ 30 點（不補點）。
三者出自同一次 `portfolio()` 回應的同一個窗（工程原則 1）。

成交統計（`fills_stats`，Hyperbot 已驗證定義，見 memory hyperbot-metrics-reference）：
- `order_count`      ＝ distinct `oid`（不是 fills 數；HL 一張單可拆多筆 fill）。
- `closed_positions` ＝ 部位歸零的生命週期數：`dir` 以 "Close" 開頭且 |startPosition| == sz，
                       或 `dir` 含 ">"（翻倉）。
- `wins`             ＝ 生命週期累積 closedPnl > 0 的次數；`win_rate_pct` = wins/closed×100。
- `realized_pnl_usd` ＝ Σ closedPnl（未扣手續費／funding，與 Hyperbot totalPnl 同定義）。
- 只算 perp fills（D4）：spot 成交（Buy/Sell/Spot Dust Conversion）跟單複製不到，排除。
錨例：0x6648…b1f3 2026-09-04 30 天 → 221／27／15／40225.79，與 Hyperbot period=30 逐位一致。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from spark.filet.leader_perf import STATUS_OK, compute_window_performance, extract_window

SPARK_POINTS = 30
PERP_DIRS = frozenset({"Open Long", "Open Short", "Close Long", "Close Short",
                       "Long > Short", "Short > Long"})
_CENTS = Decimal("0.01")


def _q2(v: Decimal) -> float:
    return float(v.quantize(_CENTS, rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class WindowStats:
    pnl_usd: float
    max_dd_pct: float | None          # ≤ 0；None = 該窗算不出（見 max_dd_reason）
    max_dd_reason: str | None         # None 當且僅當 max_dd_pct 非 None
    spark: tuple[float, ...]          # pnlHistory 降採樣

    def to_dict(self) -> dict[str, Any]:
        return {"pnl_usd": self.pnl_usd, "max_dd_pct": self.max_dd_pct,
                "max_dd_reason": self.max_dd_reason, "spark": list(self.spark)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WindowStats":
        return cls(pnl_usd=float(d["pnl_usd"]),
                   max_dd_pct=None if d.get("max_dd_pct") is None else float(d["max_dd_pct"]),
                   max_dd_reason=d.get("max_dd_reason"),
                   spark=tuple(float(x) for x in d.get("spark", [])))


def downsample(values: list[float], n: int = SPARK_POINTS) -> list[float]:
    """等距抽樣至最多 n 點；點數 <= n 全部回傳，不補點（補點＝編造沒發生過的損益）。"""
    if not values:
        return []
    if len(values) <= n:
        return [float(v) for v in values]
    # 2026-09-05 Task 2 實作修正：分母用 n-1、round() 取樣，讓最後一點精確落在序列末端
    # （原 len/n + int() 對 100→30 會停在索引 96，丟掉末值）。Task 3 換掉 hl_explore 的
    # `_downsample_floats` 時，既有測試若斷言舊索引，改成斷言「首末點保留、長度 ≤ n」。
    step = (len(values) - 1) / (n - 1)
    idxs = [min(len(values) - 1, round(i * step)) for i in range(n)]
    return [float(values[i]) for i in idxs]


def window_stats(portfolio_raw: Any, period: str) -> WindowStats | None:
    """`portfolio()` 原始回應 + 期別 → `WindowStats`；窗缺席／形狀不符／不足兩點 → None。"""
    extracted = extract_window(portfolio_raw, period)
    if extracted is None:
        return None
    _av, pnl = extracted
    if len(pnl) < 2:
        return None
    pnl_usd = _q2(pnl[-1][1] - pnl[0][1])
    spark = tuple(downsample([float(v) for _, v in pnl]))
    perf = compute_window_performance(portfolio_raw, period)
    if perf.get("status") == STATUS_OK and "max_drawdown" in perf:
        mdd = -(Decimal(perf["max_drawdown"]) * Decimal("100"))
        return WindowStats(pnl_usd=pnl_usd, max_dd_pct=_q2(mdd), max_dd_reason=None, spark=spark)
    return WindowStats(pnl_usd=pnl_usd, max_dd_pct=None,
                       max_dd_reason=str(perf.get("reason") or "insufficient"), spark=spark)


def live_days_from_av(av_points: list[tuple[int, Any]]) -> int:
    """實盤天數＝allTime accountValueHistory 首末點日曆跨距（沿探索頁 W1 定義）。"""
    if not av_points:
        return 0
    first = datetime.fromtimestamp(av_points[0][0] / 1000, tz=timezone.utc).date()
    last = datetime.fromtimestamp(av_points[-1][0] / 1000, tz=timezone.utc).date()
    return (last - first).days


@dataclass(frozen=True)
class FillsStats:
    order_count: int
    closed_positions: int
    wins: int
    win_rate_pct: float | None        # closed_positions == 0 → None
    realized_pnl_usd: float
    concentration_pct: float | None   # 最大單幣名目佔比；無 perp 成交 → None
    coins: tuple[str, ...]            # 名目降冪前 3
    truncated: bool                   # fills 分頁到上限仍滿頁 → 以上皆為下限值

    def to_dict(self) -> dict[str, Any]:
        return {"order_count": self.order_count, "closed_positions": self.closed_positions,
                "wins": self.wins, "win_rate_pct": self.win_rate_pct,
                "realized_pnl_usd": self.realized_pnl_usd,
                "concentration_pct": self.concentration_pct, "coins": list(self.coins),
                "truncated": self.truncated}


def _is_flat_close(fill: dict) -> bool:
    d = str(fill.get("dir", ""))
    if ">" in d:
        return True
    if not d.startswith("Close"):
        return False
    try:
        return abs(abs(Decimal(str(fill["startPosition"]))) - Decimal(str(fill["sz"]))) < Decimal("1e-9")
    except (KeyError, ArithmeticError, TypeError, ValueError):
        return False


def fills_stats(fills: list[dict], *, truncated: bool) -> FillsStats:
    perp = sorted((f for f in fills if str(f.get("dir", "")) in PERP_DIRS),
                  key=lambda f: int(f.get("time", 0)))
    oids: set[Any] = set()
    closed = wins = 0
    realized = Decimal("0")
    acc: dict[str, Decimal] = {}
    notional: dict[str, Decimal] = {}
    for f in perp:
        oids.add(f.get("oid"))
        coin = str(f.get("coin", ""))
        try:
            pnl = Decimal(str(f.get("closedPnl", "0") or "0"))
            n = abs(Decimal(str(f.get("px", "0"))) * Decimal(str(f.get("sz", "0"))))
        except (ArithmeticError, TypeError, ValueError):
            continue
        realized += pnl
        acc[coin] = acc.get(coin, Decimal("0")) + pnl
        notional[coin] = notional.get(coin, Decimal("0")) + n
        if _is_flat_close(f):
            closed += 1
            if acc[coin] > 0:
                wins += 1
            acc[coin] = Decimal("0")
    total_n = sum(notional.values(), Decimal("0"))
    ranked = sorted(notional.items(), key=lambda kv: kv[1], reverse=True)
    concentration = _q2(ranked[0][1] / total_n * 100) if ranked and total_n > 0 else None
    win_rate = _q2(Decimal(wins) / Decimal(closed) * 100) if closed > 0 else None
    return FillsStats(order_count=len(oids), closed_positions=closed, wins=wins,
                      win_rate_pct=win_rate, realized_pnl_usd=_q2(realized),
                      concentration_pct=concentration,
                      coins=tuple(c for c, _ in ranked[:3]), truncated=truncated)
```

- [ ] **Step 4: 跑測試**

Run: `uv run pytest tests/test_trader_stats.py -v`
Expected: 全綠。若 `test_window_stats_day_has_drawdown` 的符號或數值不合（例如 `leader_perf._max_drawdown` 回傳的是負值），**不要改錨例**，先跑 `uv run python -c "import json;from spark.filet.leader_perf import compute_window_performance as c;p=json.load(open('tests/fixtures/trader_stats_0x6648_portfolio.json'));print(c(p,'day')['max_drawdown'])"` 看原始值，確認 `leader_perf` 回傳正比例 0.7407 後修 `window_stats` 的取負邏輯。

- [ ] **Step 5: lint + commit**

```bash
uv run ruff check src/spark/filet/trader_stats.py tests/test_trader_stats.py
git add src/spark/filet/trader_stats.py tests/test_trader_stats.py
git commit -m "feat: trader_stats 共用指標模組（pnl_usd／權益指數回撤／Hyperbot 定義成交統計）"
```

---

### Task 3a: `hl.py` 新增原始形狀分頁出口 `get_fills_raw_paged` `@inline`

**Files:**
- Modify: `src/spark/publicapi/hl.py`（`get_fills_detail_paged` 下方）
- Test: `tests/test_publicapi_hl.py`（沿既有分頁測試的假 `post_fn` 寫法，約 252／281 行）

- [ ] **Step 1: 寫失敗測試**（沿該檔既有的假 gateway／`post_fn` fixture 慣例；`_gw()` 若不存在就照既有分頁測試的建法）

```python
def test_get_fills_raw_paged_preserves_raw_hl_fields():
    """trader_stats.fills_stats 需要 dir／oid／startPosition／closedPnl 原始欄位；
    本方法不得經 _fill_detail_dict 裁切。"""
    raw = [{"time": 1, "coin": "BTC", "side": "B", "px": "100", "sz": "1", "fee": "0.1",
            "closedPnl": "0", "hash": "0xh", "oid": 11, "tid": 1, "dir": "Open Long",
            "startPosition": "0.0"},
           {"time": 2, "coin": "BTC", "side": "A", "px": "110", "sz": "1", "fee": "0.1",
            "closedPnl": "10", "hash": "0xh2", "oid": 12, "tid": 2, "dir": "Close Long",
            "startPosition": "1.0"}]
    gw = _gw(post_fn=lambda url, body: raw)          # 單頁 < 2000 → 不續抓
    fills, truncated = gw.get_fills_raw_paged("0xabc", _dt(0), _dt(10), max_pages=3)
    assert truncated is False
    assert [f["dir"] for f in fills] == ["Open Long", "Close Long"]
    assert fills[0]["oid"] == 11 and fills[1]["startPosition"] == "1.0"
    assert "closedPnl" in fills[1] and "closed_pnl" not in fills[1]
```

- [ ] **Step 2: 跑確認失敗**

Run: `uv run pytest tests/test_publicapi_hl.py -k raw_paged -v`
Expected: FAIL（`AttributeError: get_fills_raw_paged`）

- [ ] **Step 3: 實作**（放在 `get_fills_detail_paged` 之後）

```python
    def get_fills_raw_paged(self, address: str, start: datetime, end: datetime, *,
                            max_pages: int | None = None) -> tuple[list[dict], bool]:
        """`_paged_fills_raw` 的公開出口：回傳**原始** `userFillsByTime` dict（升冪、
        `tid` 去重），**不**經 `_fill_detail_dict` 裁切。2026-09-05 新增，供
        `spark.filet.trader_stats.fills_stats`（探索清單與交易員詳情共用）使用——它需要
        `dir`（開/平倉/翻倉語意）、`oid`（distinct 訂單數）、`startPosition`（部位歸零判斷）、
        `closedPnl`，這些在 `_fill_detail_dict` 都被丟掉或改名，而 `dir`/`startPosition`
        無法從裁切後的 `side` 重建。`/api/me/fills` 仍走 `get_fills_detail_paged`，不受影響。
        `truncated` 語意同 `_paged_fills_raw`。"""
        return self._paged_fills_raw(address, start, end, max_pages=max_pages)
```

- [ ] **Step 4: 跑綠 + commit**

Run: `uv run pytest tests/test_publicapi_hl.py -q`
Expected: 全綠。

```bash
git add src/spark/publicapi/hl.py tests/test_publicapi_hl.py
git commit -m "feat: hl.get_fills_raw_paged 原始形狀分頁出口（供 trader_stats.fills_stats）"
```

---

### Task 3: `hl_explore` 改吃 `trader_stats` `@inline`

**Files:**
- Modify: `src/spark/publicapi/hl_explore.py`
- Modify: `tests/test_public_explore.py`

先讀 `hl_explore.py` 檔頭（1–260 行）與 `WindowStats`／`ExploreRow`／`_window_stats`／`_fills_stats`／`enrich_candidate`／`_apply_tags`／`qualify`／`sort_key`／`_enrich_one`／`load_snapshot` 各段。

- [ ] **Step 1: 先改測試（讓失敗告訴你要改哪裡）**

在 `tests/test_public_explore.py`：
1. `_row()` helper（約 102–116 行）：`WindowStats(ret_pct=..., max_dd_pct=..., spark=())` 改成 `WindowStats(pnl_usd=..., max_dd_pct=..., max_dd_reason=None, spark=())`；`over.pop("ret_pct", 10.0)` 改 `over.pop("pnl_usd", 1000.0)`；`fill_count_30d=200` 改 `order_count_30d=200`，並補 `closed_positions_30d=10, realized_pnl_30d_usd=0.0`。
2. `test_enrich_candidate_computes_return_drawdown_live_days_and_win_rate`（216 行）與 `test_enrich_candidate_computes_all_four_windows_from_single_portfolio_response`（292 行）：把 `ret_pct` 斷言改成 `pnl_usd`（值＝該假 portfolio 的 pnlHistory 末值−首值；若該測試的假資料沒有 `pnlHistory`，補上與 accountValueHistory 同時間戳、從 0 起算的 pnlHistory）；`max_dd_pct` 斷言改成權益指數 MDD（假資料無現金流時，權益指數 MDD ＝ AV 的 running-peak 回撤，數值不變）。
3. `test_sort_key_*`（426–449 行）：改成「所選窗 `pnl_usd` 大者排前；缺窗退回 month」。例：

```python
def test_sort_key_orders_by_selected_window_pnl_desc():
    high = _row(pnl_usd=5000.0)
    low = _row(pnl_usd=-200.0)
    assert sort_key(high) > sort_key(low)


def test_sort_key_falls_back_to_month_when_window_missing():
    row = _row(pnl_usd=123.0)
    row.windows["day"] = None
    assert sort_key(row, window="day") == sort_key(row, window="month")
```

4. `_fills_stats` 相關測試改成呼叫 `trader_stats.fills_stats` 的語意（wins 以生命週期計；`fill_count` 改 `order_count`）。若有測試斷言「closedPnl != 0 的每筆 fill 算一次 closed」，改寫成生命週期版（見 Task 2 的 `test_fills_stats_flip_counts_as_close_and_partial_close_does_not`）。直接測 `_win_rate_pct`（R2-02 值域校驗）的測試整條刪除：新實作 `wins` 只在 `closed` 遞增時遞增，結構上不可能落到 [0,100] 之外，值域校驗由 Task 2 的 fixture 錨例涵蓋。
5. `qualify` 測試：`fill_count_30d=` 全部改 `order_count_30d=`；新增一條「所選窗 `max_dd_pct is None` → max_dd 過濾視為無證據、通過」。
6. `test_index_query_window_selects_ranking_and_response_row_content`（813 行）：回應列欄位改 `pnl_usd`／`max_dd_pct`／`max_dd_reason`／`spark`／`order_count_30d`／`closed_positions_30d`／`realized_pnl_30d_usd`。
7. 新增快照版本測試：

```python
def test_snapshot_version_bumped_to_3():
    from spark.publicapi.hl_explore import EXPLORE_INDEX_VERSION
    assert EXPLORE_INDEX_VERSION == 3
```

Run: `uv run pytest tests/test_public_explore.py -q`
Expected: 大量 FAIL（欄位不存在）。

- [ ] **Step 2: 實作 `hl_explore.py`**

逐項：

a. import：`from spark.filet.trader_stats import (WindowStats, FillsStats, window_stats, fills_stats, live_days_from_av, downsample)`。**刪除**本檔的 `WindowStats` dataclass、`_return_and_drawdown`、`_calendar_span_days`、`_downsample_floats`、`_window_stats`、`_optional_window_stats`、`_fills_stats`、`_win_rate_pct`（若 `_win_rate_pct` 有其他呼叫點，改用 `FillsStats.win_rate_pct`）。`SPARK_POINTS` 改從 `trader_stats` import（保留名稱給既有測試）。

b. `EXPLORE_INDEX_VERSION = 3`。

c. `ExploreConfig` 加欄位 `fills_max_pages: int = 3`（docstring：D5，配合 `hl.get_fills_raw_paged`；每頁 2000 筆，3 頁上限；`_call_hl` 的節流包住整個分頁呼叫，頁與頁之間沒有額外間隔，這是已知的 burst 面，上限 3 就是為了壓它）。

d. `ExploreRow`：`fill_count_30d: int` → `order_count_30d: int`；新增 `closed_positions_30d: int`、`realized_pnl_30d_usd: float`；`to_dict` 對應輸出這三個鍵（移除 `fill_count_30d`）；`from_dict`／`_row_from_dict`（snapshot 讀回）對應改；`windows` 讀回改用 `WindowStats.from_dict`。

e. `enrich_candidate`：

```python
    month = window_stats(portfolio_raw, WINDOW_TO_PERIOD["month"])
    if month is None:
        return None
    all_time_window = extract_window(portfolio_raw, WINDOW_TO_PERIOD["allTime"])
    if all_time_window is None:
        return None
    av_all, _ = all_time_window
    live_days = live_days_from_av(av_all)
    windows = {
        "day": window_stats(portfolio_raw, WINDOW_TO_PERIOD["day"]),
        "week": window_stats(portfolio_raw, WINDOW_TO_PERIOD["week"]),
        "month": month,
        "allTime": window_stats(portfolio_raw, WINDOW_TO_PERIOD["allTime"]),
    }
    fs = fills_stats(fills or [], truncated=fills_truncated)
    ...
    return ExploreRow(..., windows=windows, live_days=live_days,
                      order_count_30d=fs.order_count, closed_positions_30d=fs.closed_positions,
                      close_win_rate_pct=fs.win_rate_pct, realized_pnl_30d_usd=fs.realized_pnl_usd,
                      concentration_pct=fs.concentration_pct, coins=fs.coins, ...,
                      fills_truncated=fs.truncated)
```

`enrich_candidate` 簽名加 `fills_truncated: bool = False` 關鍵字參數（由 `_enrich_one` 傳入分頁結果的 truncated）。gating 只剩「month 或 allTime 窗缺席／不足兩點 → 整列跳過」，**刪除**「首點 ≤ 0 剔除」的文字與邏輯（pnl 基準不需要）。更新 docstring。

f. `_apply_tags`：`low_drawdown` 分位數只用 `windows["month"].max_dd_pct is not None` 的列計算；`max_dd_pct is None` 的列永不掛 `low_drawdown`。若合格列為 0 → 沒有人掛此 tag。

g. `qualify`：`row.fill_count_30d` → `row.order_count_30d`；max_dd 過濾：`stats is None or stats.max_dd_pct is None` → 無證據通過（沿既有慣例，docstring 補一句）。

h. `sort_key`：

```python
def sort_key(row: ExploreRow, *, window: str = DEFAULT_WINDOW) -> Decimal:
    """D2（2026-09-04）：所選窗 pnl_usd 降冪（金額，不再做報酬÷回撤比值——分母來自
    另一個指標且可能為 None）。缺該窗退回 month（enrich_candidate 保證恆非 None）。"""
    stats = row.windows.get(window) or row.windows["month"]
    return Decimal(str(stats.pnl_usd))
```
刪除 `_DD_FLOOR_PCT`（若無其他引用）。

i. `_enrich_one`：

```python
        fills, fills_truncated = self._call_hl(
            lambda: self._hl.get_fills_raw_paged(address, start, end,
                                                 max_pages=self._cfg.fills_max_pages),
            address, "fills")   # 原始 HL 形狀（含 dir/oid/startPosition），見 Task 3a
        ...
        return enrich_candidate(address, display_name, portfolio_raw, fills, ch_state,
                                fills_truncated=fills_truncated)
```
（`_call_hl` 的第二、三個參數照既有簽名傳；先讀它。）

j. 檔頭：在「工程原則 1」段落把「`accountValueHistory` 序列」改成「`pnlHistory` 序列（via `trader_stats.window_stats`）」；W2 段落標記「2026-09-04 已改走 `get_fills_detail_paged`（D5）」；加一段 `<!-- 2026-09-04: 指標統一，公式移至 spark/filet/trader_stats.py，見 docs/superpowers/plans/2026-09-04-explore-trader-pnl-metrics.md -->`。

- [ ] **Step 3: 跑測試到全綠**

Run: `uv run pytest tests/test_public_explore.py tests/test_trader_stats.py -q`
Expected: 全綠。`uv run pytest -q` 全套也要綠（其他檔若引用 `fill_count_30d`／`ret_pct`，例如 `tests/test_publicapi_hl.py` 或 endpoint 測試，一併改）。

- [ ] **Step 4: 機械檢查**

Run: `grep -n "ret_pct\|fill_count_30d\|_return_and_drawdown\|_downsample_floats\|_calendar_span_days" src/spark/publicapi/hl_explore.py src/spark/publicapi/app.py`
Expected: 0 命中（檔頭歷史註解若提到舊名，改寫成「原 `ret_pct`，2026-09-04 改 `pnl_usd`」可保留，但函式名必須為 0 命中）。

- [ ] **Step 5: lint + commit**

```bash
uv run ruff check src tests scripts
git add src/spark/publicapi/hl_explore.py tests/test_public_explore.py
git commit -m "feat: explore 改吃 trader_stats（pnl_usd／權益指數回撤／分頁 fills／版本 3）"
```

---

### Task 4: 交易員詳情端點改同一組指標 `@inline`

**Files:**
- Modify: `src/spark/publicapi/app.py`（`_cached_trader_data` 約 2321–2382；`public_trader_detail` 約 2404–2475）
- Test: 找既有詳情端點測試：`grep -ln "public/traders" tests/*.py`；沒有就新建 `tests/test_public_trader_detail.py`

先讀 `_cached_trader_data` 全文、`public_trader_detail` 全文、以及 `tests/test_public_explore.py` 裡建 `TestClient`／假 `HLGateway` 的方式（沿用同一套 fixture 慣例）。

- [ ] **Step 1: 寫失敗測試**

```python
def test_trader_detail_shape_matches_explore_windows(client_with_fake_hl):
    # client_with_fake_hl：假 HL 回 tests/fixtures/trader_stats_0x6648_portfolio.json、
    # fills fixture、以及一個 accountValue=0 無持倉的 clearinghouseState。
    # 建法沿 tests/test_public_explore.py 的既有 fixture；若該檔沒有可重用的，
    # 在本檔用 app 工廠 + 假 gateway 物件（portfolio/get_fills_detail_paged/
    # clearinghouse_state/non_funding_ledger_updates 四個方法）自建。
    r = client_with_fake_hl.get("/api/public/traders/0x6648f8dd041ed689de7bf501efb3b827cf15b1f3")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"address", "account_value", "follow_blocked", "live_days", "exposure",
                         "windows", "fills_30d", "methodology", "metrics",
                         "sample_days", "sample_threshold"}
    assert set(body["windows"]) == {"day", "week", "month", "allTime"}
    assert set(body["metrics"]) == {"day", "week", "month", "allTime"}
    # month/allTime 兩窗在 0x6648 上都被閘門判無效 → 該窗 metrics 全部 insufficient
    assert body["metrics"]["month"]["sharpe_insufficient"] is True
    assert body["metrics"]["month"]["sharpe"] is None
    assert body["metrics"]["allTime"]["total_return_pct_insufficient"] is True
    # day 窗 perf ok，但 covered_days < 30 → 比率型指標仍標不足（RATIO_MIN_DAYS）
    assert body["metrics"]["day"]["sharpe_insufficient"] is True
    assert body["metrics"]["day"]["win_rate_pct"] is not None      # N>=1 即存在，不設閘
    assert body["sample_days"] == 0 and "cagr_pct" not in body      # allTime 無效 → 無 CAGR
    m = body["windows"]["month"]
    assert m["pnl_usd"] == 33055.26 and m["max_dd_pct"] is None \
        and m["max_dd_reason"] == "too_many_skipped_intervals" and len(m["spark"]) == 30
    assert body["windows"]["day"]["max_dd_pct"] == pytest.approx(-74.07, abs=0.01)
    assert body["live_days"] == 1003
    f = body["fills_30d"]
    assert (f["order_count"], f["closed_positions"], f["wins"], f["win_rate_pct"],
            f["realized_pnl_usd"], f["truncated"]) == (221, 27, 15, 55.56, 40225.79, False)
    assert set(body["methodology"]) == {"basis", "updated_at", "start_equity_usd",
                                        "end_equity_usd", "initial_deposit_usd", "mdd_note"}
    assert body["methodology"]["basis"] == "combined"
    assert "equity_index" not in body


def test_trader_detail_and_explore_row_agree_on_same_address(client_with_fake_hl, explore_row_for_same_fake):
    # explore_row_for_same_fake：用同一份假 HL 資料跑 hl_explore.enrich_candidate 得到的 ExploreRow.to_dict()
    detail = client_with_fake_hl.get("/api/public/traders/0x6648f8dd041ed689de7bf501efb3b827cf15b1f3").json()
    row = explore_row_for_same_fake
    for w in ("day", "week", "month", "allTime"):
        assert detail["windows"][w] == row["windows"][w]
    assert detail["live_days"] == row["live_days"]
    assert detail["fills_30d"]["order_count"] == row["order_count_30d"]
    assert detail["fills_30d"]["closed_positions"] == row["closed_positions_30d"]
    assert detail["fills_30d"]["win_rate_pct"] == row["close_win_rate_pct"]
    assert detail["fills_30d"]["realized_pnl_usd"] == row["realized_pnl_30d_usd"]
```

Run: `uv run pytest tests/test_public_trader_detail.py -q`（或既有檔）
Expected: FAIL（形狀不符）。

- [ ] **Step 2: 實作**

a. `_cached_trader_data`：快取條目多抓兩樣：`fills, fills_truncated = hl.get_fills_raw_paged(addr, now-30d, now, max_pages=3)`（原始形狀，Task 3a），以及保留原始 `ch_state`（目前只抽 `account_value`）。回傳 tuple 擴成 `(rows, account_value, initial_deposit_usd, ch_state, fills, fills_truncated)`；fills 抓失敗 → `fills=[]`、`fills_truncated=False` 並 log（降級該區塊，不拖累整頁，沿 `account_value` 既有語意）。`max_pages` 常數 `TRADER_FILLS_MAX_PAGES = 3`（與 `ExploreConfig.fills_max_pages` 預設同值，各自宣告、註解互指）。

b. `public_trader_detail`：

```python
        from spark.filet.trader_stats import window_stats, fills_stats, live_days_from_av
        from spark.publicapi.hl_explore import WINDOW_TO_PERIOD, _exposure, _parse_positions
        from spark.filet.leader_perf import MDD_SAMPLING_NOTE

        windows = {k: (ws.to_dict() if (ws := window_stats(rows, p)) is not None else None)
                   for k, p in WINDOW_TO_PERIOD.items()}
        all_time = extract_window(rows, WINDOW_TO_PERIOD["allTime"])
        live_days = live_days_from_av(all_time[0]) if all_time is not None else 0
        start_equity_usd, end_equity_usd = build_equity_range(all_time[0]) if all_time is not None else (None, None)
        fs = fills_stats(fills, truncated=fills_truncated)
        exp_dir, exp_pct = _exposure(_parse_positions(ch_state)) if ch_state else (None, None)
        perfs = {}
        for k, p in WINDOW_TO_PERIOD.items():
            try:
                perfs[k] = compute_window_performance(rows, p)
            except Exception as e:  # noqa: BLE001 — schema 漂移不得炸掉整頁
                logger.error("交易員績效計算失敗 address=%s window=%s: %s", addr, k, e)
                perfs[k] = None
        metrics = {k: build_metrics(perf) for k, perf in perfs.items()}
        all_time_perf = perfs["allTime"]
        view = {
            "address": addr,
            "account_value": account_value,
            "follow_blocked": _trader_follow_blocked(addr),
            "live_days": live_days,
            "exposure": None if exp_dir is None else {"dir": exp_dir, "pct": exp_pct},
            "windows": windows,
            "metrics": metrics,
            "fills_30d": fs.to_dict(),
            "methodology": {
                "basis": "combined",
                "updated_at": int(now_fn()),
                "start_equity_usd": start_equity_usd,
                "end_equity_usd": end_equity_usd,
                "initial_deposit_usd": initial_deposit_usd,
                "mdd_note": MDD_SAMPLING_NOTE,
            },
        }
        view.update(build_cagr_fields(all_time_perf, sample_days=sample_days_from_perf(all_time_perf)))
        return view
```
（`_exposure`／`_parse_positions` 若是底線私有名，改成在 `hl_explore` 加公開別名 `exposure_from_clearinghouse(ch_state) -> tuple[str|None, float|None]`，app.py 只呼叫公開名。）本端點**移除**對 `build_equity_index`／`build_methodology` 的呼叫（`equity_index` 由 `windows[w].spark` 取代；methodology 改內聯），**保留** `build_metrics`／`build_cagr_fields`／`sample_days_from_perf`／`compute_window_performance`（D6 保留版：metrics 逐窗、CAGR allTime）。strategies 端點不動。更新 docstring：指向本 plan 的 D6／D10。

- [ ] **Step 3: 跑測試**

Run: `uv run pytest tests/test_public_trader_detail.py tests/test_public_strategies.py tests/test_public_explore.py -q`
Expected: 全綠。`tests/test_public_strategies.py` 裡的 `test_detail_*cagr*`／`test_detail_sample_days_*` 不論針對 strategies 或 traders 端點都必須維持綠（CAGR／sample_days 契約不變）；若某條因為 traders 端點的假 perf 被新閘門判無效而紅，把該測試的假資料改成無現金流的乾淨序列（AV 與 pnl 同步遞增），不得放寬閘門。

- [ ] **Step 4: lint + 全套 + commit**

```bash
uv run ruff check src tests scripts && uv run pytest -q
git add src/spark/publicapi/app.py tests/test_public_trader_detail.py tests/test_public_strategies.py
git commit -m "feat: /api/public/traders 改回傳與 explore 同源的 windows/fills_30d/live_days，metrics 逐窗、CAGR 維持 allTime"
```

---

### Task 5: 前端型別、文案、探索表格 `@inline`

**Files:**
- Modify: `web/src/lib/publicApi.ts:372-408`（`ExploreWindowStats`／`ExploreRow`）、`:524-541`（`PublicTraderDetail`）
- Modify: `web/src/lib/copy.ts`（`explore` zh 約 1697–1754 / en 約 3195；`traders` zh 約 1762–1783 / en 約 3244）
- Modify: `web/src/app/explore/page.tsx`（`ExploreRowView` 約 363–454、`fmtSignedPct` 95–97、表頭約 297）
- Modify: `web/src/app/explore/page.test.tsx`

- [ ] **Step 1: 型別**

```ts
export type ExploreWindowStats = {
  pnl_usd: number;
  max_dd_pct: number | null;       // ≤ 0；null = 該窗算不出，見 max_dd_reason
  max_dd_reason: string | null;
  spark: number[];                 // pnlHistory 降採樣
};
export type ExploreRow = {
  address: string; display_name: string | null; label: string; coins: string[];
  account_bucket: string;
  windows: Record<ExploreWindow, ExploreWindowStats | null>;
  live_days: number;
  order_count_30d: number;
  closed_positions_30d: number;
  realized_pnl_30d_usd: number;
  fills_truncated?: boolean;
  close_win_rate_pct: number | null;
  concentration_pct: number | null;
  exposure: { dir: "long" | "short" | null; pct: number | null };
  tags: string[];
};
export type TraderFillsStats = {
  order_count: number; closed_positions: number; wins: number;
  win_rate_pct: number | null; realized_pnl_usd: number;
  concentration_pct: number | null; coins: string[]; truncated: boolean;
};
export type PublicTraderDetail = {
  address: string;
  account_value: string | null;
  follow_blocked: boolean;
  live_days: number;
  exposure: { dir: "long" | "short"; pct: number | null } | null;
  windows: Record<ExploreWindow, ExploreWindowStats | null>;
  metrics: Record<ExploreWindow, PublicMetrics>;   // PublicMetrics ＝ 既有 metrics 型別（build_metrics 形狀），沿用不改
  sample_days: number;
  sample_threshold: number;
  cagr_pct?: number | null;                        // allTime 年化；未達門檻無此鍵（既有契約）
  fills_30d: TraderFillsStats;
  methodology: {
    basis: string; updated_at: number;
    start_equity_usd: string | null; end_equity_usd: string | null;
    initial_deposit_usd: string | null; mdd_note: string;
  };
};
```
（`account_value`／`*_equity_usd`／`initial_deposit_usd` 的既有型別若是 `number | null` 就沿用既有，不要改。）

- [ ] **Step 2: 文案（zh/en 對稱，只列 zh，en 自行對譯）**

`explore.table`：`sparkline: "損益走勢"`、`ret` 改名 `pnl: "損益（USD）"`、`dd: "最大回撤"`（不變）、新增 `ddUnavailable: "—"`、`ddUnavailableTitle: "此窗回撤無法可靠計算（出入金主導或淨值長期低於 100 USDC）"`；`filters.fills: "訂單 ≥ 200 筆"`；`ddDefinition: "回撤以出入金中性化的權益指數計算，與交易所／第三方工具的定義不同"`。

`traders` 新增：`windowsLabel: "統計窗"`、`pnlLabel: "損益（USD）"`、`ddLabel: "最大回撤"`、`ddUnavailable`／`ddUnavailableTitle`（同上）、`ddDefinition`（同上）、`liveDaysLabel: "實盤天數"`、`exposureLabel: "目前曝險"`、`fillsHeading: "近 30 天成交統計"`、`orders: "訂單"`、`closedPositions: "平倉次數"`、`winRate: "結倉勝率"`、`realizedPnl: "已實現損益（USD）"`、`fillsTruncatedNote: "成交筆數超過抓取上限，以上為下限值"`、`pnlCurveLabel: "損益曲線"`、`pnlSourceNote: "損益含未實現損益、資金費率與手續費，已排除出入金（HL pnlHistory）"`。

- [ ] **Step 3: 探索表格**

- 刪除 `fmtSignedPct`；在 `web/src/lib/format.ts` 新增並 export `fmtSignedUsd(n: number): string` → `` `${n > 0 ? "+" : n < 0 ? "−" : ""}$${Math.abs(n).toLocaleString("en-US", { maximumFractionDigits: 0 })}` ``（例：`+$33,055`、`−$2,182`、`$0`），探索頁與詳情頁（Task 6）都從 `format.ts` import；`web/src/lib/format.test.ts` 加三條錨例（33055.26 → `+$33,055`、-2181.94 → `−$2,182`、0 → `$0`）。
- 損益欄：`stats == null ? NO_VALUE : fmtSignedUsd(stats.pnl_usd)`，正負色沿 `stats.pnl_usd >= 0`。
- 回撤欄：`stats == null || stats.max_dd_pct == null ? <span title={c.table.ddUnavailableTitle}>{c.table.ddUnavailable}</span> : `${stats.max_dd_pct.toFixed(1)}%``。**注意** `max_dd_pct` 不是 redline 標記欄位，`== null` 判斷可用；但不得用 `??`/`||` 把 null 換成 0。
- sparkline：`sparkPoints(stats?.spark ?? [])` 沿用；線色改依 `stats.pnl_usd >= 0`。
- 表頭 `c.table.ret` → `c.table.pnl`。
- 頁面底部（`disclaimer`／`poolNote` 附近）加一行 `c.ddDefinition`。
- 損益欄 CSS：`.explore-ret` 加 `min-width: 7.5rem; text-align: right; white-space: nowrap;`（找到既有 class 定義處改，避免長數字溢欄）。

- [ ] **Step 4: 前端測試**

`page.test.tsx` 的假 row 改用新欄位（`pnl_usd`、`max_dd_pct: null`+`max_dd_reason`、`order_count_30d`…）。新增兩條：
```ts
it("損益以金額顯示：+$33,055 / −$2,182", ...)   // 兩列分別 pnl_usd 33055.26 與 -2181.94
it("max_dd_pct 為 null → 顯示「—」且帶 title 說明", ...)
```

Run: `export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH" && cd web && npm test -- explore`
Expected: 全綠。再跑 `npm test`（含 `redline.test.ts`、`copy.test.ts` 對稱檢查）全綠。

- [ ] **Step 5: Commit**

```bash
git add web/src/lib/publicApi.ts web/src/lib/copy.ts web/src/app/explore/page.tsx web/src/app/explore/page.test.tsx
git commit -m "feat: 探索表格改顯示損益金額與權益指數回撤（null → —）"
```

---

### Task 6: 交易員詳情頁改四窗＋同組欄位 `@inline`

**Files:**
- Create: `web/src/components/PnlCurve.tsx`
- Modify: `web/src/app/traders/[address]/page.tsx`（保留 breadcrumb、載入／404 狀態、**跟單面板整段不動**）
- Modify: `web/src/app/traders/[address]/page.test.tsx`

先讀 `page.tsx` 全文與 `web/src/components/EquityCurve.tsx`（沿用其 SVG 尺寸與 class 命名）。

- [ ] **Step 1: `PnlCurve` 元件**

```tsx
"use client";
import { useMemo } from "react";

/** 損益曲線：輸入後端 `windows[w].spark`（pnlHistory 降採樣，USD）。
 *  畫零線；正段綠負段紅只靠終值決定線色（sparkline 同規則），不做分段著色。 */
export function PnlCurve({ values, ariaLabel }: { values: number[]; ariaLabel: string }) {
  const W = 640, H = 200, PAD = 8;
  const { points, zeroY, last } = useMemo(() => {
    const vs = values.filter((v) => Number.isFinite(v));
    if (vs.length < 2) return { points: "", zeroY: null as number | null, last: 0 };
    const min = Math.min(0, ...vs), max = Math.max(0, ...vs);
    const span = max - min || 1;
    const x = (i: number) => PAD + (i / (vs.length - 1)) * (W - 2 * PAD);
    const y = (v: number) => PAD + (1 - (v - min) / span) * (H - 2 * PAD);
    return {
      points: vs.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" "),
      zeroY: y(0),
      last: vs[vs.length - 1],
    };
  }, [values]);
  if (!points) return <div className="pnl-curve pnl-curve-empty">—</div>;
  return (
    <svg className="pnl-curve" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={ariaLabel}>
      {zeroY != null && <line x1={PAD} x2={W - PAD} y1={zeroY} y2={zeroY} className="pnl-curve-zero" />}
      <polyline points={points} fill="none" strokeWidth={2}
        stroke={last >= 0 ? "var(--pos)" : "var(--neg)"} />
    </svg>
  );
}
```

- [ ] **Step 2: 頁面**

- 讀 `useSearchParams().get("window")`，合法值 `day|week|month|allTime`，預設 `month`；四鈕 UI 直接複用探索頁的 `explore-window-group` 樣式與 `c.explore.windows[w]` 文案；切換只改本地 state 與 URL query，不重打 API（四窗資料已一次回來）。
- 主卡：`pnlLabel` → `fmtSignedUsd(stats.pnl_usd)`（從 `web/src/lib/format.ts` import，Task 5 已建）；`ddLabel` → 同探索頁規則（null → `—` + title）；`liveDaysLabel` → `trader.live_days`；`exposureLabel` → `trader.exposure` 為 null 顯示 `—`，否則 `多/空 xx%`（文案沿 `c.explore.exposureDir`）。
- 損益曲線：`<PnlCurve values={stats?.spark ?? []} ariaLabel={c.traders.pnlCurveLabel} />`，下方一行 `c.traders.pnlSourceNote` 與 `c.traders.ddDefinition`。
- 成交統計卡（30D 固定，不隨窗切換）：`orders`／`closedPositions`／`winRate`（null → `—`）／`realizedPnl`；`fills_30d.truncated` 為 true 時顯示 `fillsTruncatedNote`。
- 帳戶價值（既有 `accountValueLabel`）保留；`methodology.updated_at`／`basis` 的 as-of 行保留既有寫法。
- **指標網格保留但改逐窗**：資料來源改 `trader.metrics[window_]`；網格只渲染 `sharpe`／`sharpe_se`／`annualized_vol_pct`／`sortino`／`win_rate_pct`／`best_day_pct`／`worst_day_pct`（連同各自 `*_insufficient` → 既有「樣本不足」渲染），**不再渲染** `total_return_pct` 與 `max_drawdown_pct`（由窗卡顯示 `pnl_usd`／`max_dd_pct`，見 D6）。`CagrCard` 與 `sampleInsufficient`（`sample_days < sample_threshold`）邏輯**原樣保留**（allTime）。
- **移除**：`EquityCurve` 及其 import（損益曲線取代）。跟單面板（SIWE／approval／follow CTA）**一行不改**。
- 探索表格「查看」連結改成 `/traders/${address}?window=${window_}`，讓清單所選窗帶進詳情頁。
- **探索表格 grid 欄寬**（2026-09-05 Task 5 builder 發現）：`web/src/styles/globals.css` 探索表格的 `grid-template-columns` 中損益欄固定 88px，`.explore-ret` 的 `min-width` 會被 grid 蓋掉，`+$1,234,567` 仍會溢欄。把該欄改成 `minmax(7.5rem, auto)`（找到那條 `grid-template-columns`，只改損益那一欄，其他欄不動），並在探索頁測試補一條快照無關的 smoke（渲染 `pnl_usd: 1234567.89` 的列，斷言文字為 `+$1,234,568`）。

- [ ] **Step 3: 測試**

`page.test.tsx`：假 `PublicTraderDetail` 改新形狀（用 Task 4 錨例數值；`metrics` 四窗各給一份既有形狀的假 metrics）；`insufficient 指標`、`sample_days`、`cagr` 相關 5 條 `it` **保留**，只把假資料改成 `metrics.month.*`／`metrics.allTime.*` 的路徑；新增：
```ts
it("預設 month 窗：顯示 +$33,055、回撤「—」（too_many_skipped_intervals）、實盤 1003 天", ...)
it("?window=day → 顯示 −$2,182 與回撤 -74.1%，指標網格切到 metrics.day", ...)
it("指標網格不渲染 total_return_pct／max_drawdown_pct（同頁只有窗卡一個回撤數字）", ...)
it("成交統計卡：221 / 27 / 55.56% / +$40,226", ...)
it("fills_30d.truncated → 顯示下限值提示", ...)
it("跟單面板仍渲染（follow_blocked=false 時有 CTA）", ...)   // 既有測試若已涵蓋則保留原條
```

Run: `export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH" && cd web && npm test`
Expected: 全綠（含 redline／copy 對稱）。

- [ ] **Step 4: Commit**

```bash
git add web/src/components/PnlCurve.tsx "web/src/app/traders/[address]/page.tsx" "web/src/app/traders/[address]/page.test.tsx" web/src/lib/format.ts web/src/app/explore/page.tsx
git commit -m "feat: 交易員詳情頁改四窗切換＋與探索清單同源的損益/回撤/成交統計"
```

---

### Task 7: 收尾（起訖淨值補回、文件、部署註記、全量驗收） `@inline`

**Files:**
- Modify: `web/src/app/traders/[address]/page.tsx`＋`page.test.tsx`（Step 0）
- Modify: `deploy/RUNBOOK.md`（找 explore 索引／快取相關段落）
- Modify: `CLAUDE.md`（「慣例」節加一行）

- [ ] **Step 0: 補回起訖淨值一行**（2026-09-05 主線程裁決：Task 6 builder 因型別不相容移除了 `MethodologyCard` 與 `start_end_equity` 卡，但後端 `methodology.start_equity_usd`／`end_equity_usd`／`initial_deposit_usd` 仍回傳，且使用者要求「其餘資料全部保留」）。在帳戶價值那一行下方加一行，沿用既有 `strategyDetail` 的起訖淨值文案 key（Task 6 前 `page.tsx` 約 249 行 `key: "start_end_equity"` 用的那組；若 key 不存在就在 `copy.ts` 的 `traders` 加 `startEndEquityLabel: "起訖淨值（allTime）"` 與 `initialDepositLabel: "初始入金"`，zh/en 對稱）：

```tsx
<div className="trader-account-row">
  <span>{c.startEndEquityLabel}</span>
  <span className="mono">
    {fmtAmount(trader.methodology.start_equity_usd)} → {fmtAmount(trader.methodology.end_equity_usd)}
  </span>
</div>
{trader.methodology.initial_deposit_usd != null && (
  <div className="trader-account-row">
    <span>{c.initialDepositLabel}</span>
    <span className="mono">{fmtAmount(trader.methodology.initial_deposit_usd)}</span>
  </div>
)}
```
（`fmtAmount` 對 null 的既有處理沿用；不得用 `??`/`||` 把 null 換成 0。）測試加一條：假資料 `start_equity_usd: "28.70"`、`end_equity_usd: "1.40"`、`initial_deposit_usd: null` → 畫面出現 `→` 兩側金額且不出現初始入金列。

- [ ] **Step 1: RUNBOOK** 加一段：`EXPLORE_INDEX_VERSION` 升 3，部署後 `/explore` 會 `building: true` 直到背景重建完成（300 地址 × 每地址 3–5 個 HL 呼叫 × 0.7s ≈ 12–20 分鐘）；舊快照檔 `var/copytrade/explore_index.json` 版本不符會被忽略、不必手動刪。

- [ ] **Step 2: CLAUDE.md 慣例節** 加：`- 探索清單與交易員詳情的績效數字一律出自 `src/spark/filet/trader_stats.py`（pnl 金額＋權益指數回撤＋Hyperbot 定義成交統計）；兩頁不得各自加公式。策略頁仍走 leader_perf TWR。`

- [ ] **Step 3: 全量驗收（主線程也會親跑）**

```bash
uv run ruff check src tests scripts
uv run pytest -q
export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH" && cd web && npm test && npm run build
```
Expected: 三者全綠；`npm run build` 無型別錯誤。

- [ ] **Step 4: Commit**

```bash
git add deploy/RUNBOOK.md CLAUDE.md
git commit -m "docs: explore/trader 指標統一的部署註記與慣例"
```

---

### Task 8: reviewer 修正輪（後端） `@inline`

2026-09-05 opus reviewer 對 `62df325^..HEAD` 的審查結果，主線程已親自重現 Critical。全部修在後端；前端對應改動在 Task 9。

**Files:**
- Modify: `src/spark/filet/leader_perf.py`（跳過比例閘門）＋ `tests/test_leader_perf.py`
- Modify: `src/spark/filet/trader_stats.py`（訂單計數）＋ `tests/test_trader_stats.py`
- Modify: `src/spark/publicapi/hl_explore.py`（`fills_max_pages` 單一來源）
- Modify: `src/spark/publicapi/app.py`（`window_stats` 例外降級、fills 失敗 → `null`、`max_pages` 單一來源）＋ `tests/test_public_traders.py`
- Modify: `deploy/RUNBOOK.md`（`EXPLORE_FILLS_MAX_PAGES` env 說明）

- [ ] **Step 1（Critical）：跳過比例閘門排除「窗口開頭尚未入金」的連續段**

問題：新 follower 在 30D 窗中途才入金 → 窗前段 AV=0 的區間全被地板跳過 → 比例 > 30% → 整窗 `insufficient` → `/api/me/dashboard` 的 `net`／`fees_paid`／`win_rate`／`max_drawdown_pct` 全 `None`（主線程實跑重現：`av=[0]*20+[500..540]` → `insufficient too_many_skipped_intervals skipped=20`）。「還沒入金」不是「入金→提光」，不該算進比例。修法：比例的分子分母都扣掉開頭連續未入金段；`skipped_intervals` 欄位仍回報總跳過數（既有語意不變）。

先改測試（`tests/test_leader_perf.py`）：

```python
def test_leading_unfunded_run_is_not_counted_toward_skipped_ratio():
    # 新 follower：前 20 點 AV=0（尚未入金），入金後 8 個區間全部正常 → ok，cum_pnl 照算
    av = [0] * 20 + [500, 505, 510, 515, 520, 525, 530, 535, 540]
    pnl = [0] * 20 + [0, 5, 10, 15, 20, 25, 30, 35, 40]
    perf = compute_window_performance(_portfolio("perpMonth", av, pnl), "perpMonth")
    assert perf["status"] == "ok"
    assert perf["skipped_intervals"] == 20           # 總跳過數語意不變
    assert perf["cum_pnl"] == Decimal("40")
```

並把 Task 1 的兩條比例測試改成「未入金段在中間／入金之後」的形狀（開頭段現在會被排除，原資料測不到門檻）：

```python
def test_too_many_skipped_intervals_marks_window_insufficient():
    # 10 點 → 9 區間；入金後又提光 5 段（i=3..7 前值 10 < 100）→ 5/9 = 0.556 > 0.30
    av = [1000, 1010, 10, 10, 10, 10, 10, 1000, 1010, 1020]
    pnl = [0, 10, 10, 10, 10, 10, 10, 10, 20, 30]
    perf = compute_window_performance(_portfolio("month", av, pnl), "month")
    assert perf["status"] == STATUS_INSUFFICIENT
    assert perf["reason"] == "too_many_skipped_intervals"
    assert perf["skipped_intervals"] == 5


def test_skipped_ratio_exactly_at_threshold_passes():
    # 11 點 → 10 區間；入金後提光 3 段（i=2..4 前值 5）= 0.30，不大於門檻 → ok
    av = [1000, 5, 5, 5, 1000, 1010, 1020, 1030, 1040, 1050, 1060]
    pnl = [0, -995, -995, -995, -995, -985, -975, -965, -955, -945, -935]
    perf = compute_window_performance(_portfolio("month", av, pnl), "month")
    assert perf["status"] == "ok"
    assert perf["skipped_intervals"] == 3
    assert MAX_SKIPPED_RATIO == Decimal("0.30")
```

實作（`compute_window_performance` 迴圈）：

```python
    equity_index: list[Decimal] = [Decimal("1")]
    skipped = 0
    leading_unfunded = 0     # 窗口開頭連續「尚未入金」（prev_av < 地板）的區間數，不算進比例
    funded_seen = False
    net_flow = Decimal("0")
    for i in range(1, len(pnl)):
        ...
        if prev_av < DENOMINATOR_FLOOR:
            skipped += 1
            if not funded_seen:
                leading_unfunded += 1
            equity_index.append(equity_index[-1])
            continue
        funded_seen = True
        ...（r 的計算與 flow_dominated 閘門不變）

    # 閘門 5：比例只看「首次入金之後」的區間——開頭尚未入金不是入金→提光。
    effective_total = (len(pnl) - 1) - leading_unfunded
    effective_skipped = skipped - leading_unfunded
    if (effective_total > 0
            and Decimal(effective_skipped) > MAX_SKIPPED_RATIO * Decimal(effective_total)):
        out = _insufficient(period, "too_many_skipped_intervals", sample_count=len(pnl))
        out["skipped_intervals"] = skipped
        return out
```

驗證 fixture 錨例不變：`uv run pytest tests/test_trader_stats.py -q` 仍全綠（0x6648 month 扣掉開頭段後 20/45 = 0.44、week 46/66 = 0.70，仍被擋；主線程已算過）。

- [ ] **Step 2（Warning 1）：`public_trader_detail` 的 `windows` 推導式加例外降級**

```python
        windows = {}
        for k, p in WINDOW_TO_PERIOD.items():
            try:
                ws = window_stats(rows, p)
            except Exception as e:  # noqa: BLE001 — schema 漂移不得炸掉整頁（與 perfs 迴圈同款）
                logger.error("交易員窗指標計算失敗 address=%s window=%s: %s", addr, k, e)
                ws = None
            windows[k] = ws.to_dict() if ws is not None else None
```
測試：`FakeHL` 給一份 `portfolio` 回應，其中 `month` 窗的 `pnlHistory` 值是 `"not-a-number"` → 端點 200、`windows.month` 為 `null`、其他窗正常。

- [ ] **Step 3（Warning 2）：fills 抓取失敗 → `fills_30d: null`，不得偽造 0**

`_cached_trader_data`：`fills: list | None = None`，抓成功才賦值；快取 tuple 照存 `None`。`public_trader_detail`：`"fills_30d": fills_stats(fills, truncated=fills_truncated).to_dict() if fills is not None else None`。測試（用 `tests/publicapi_helpers.py:121` 既有的 `fills_raw_error` 鉤子）：

```python
def test_trader_detail_fills_failure_yields_null_not_zeros(client_with_fake_hl, fake_hl):
    fake_hl.fills_raw_error["0x6648f8dd041ed689de7bf501efb3b827cf15b1f3"] = RuntimeError("HL 500")
    body = client_with_fake_hl.get("/api/public/traders/0x6648f8dd041ed689de7bf501efb3b827cf15b1f3").json()
    assert body["fills_30d"] is None
    assert body["windows"]["month"]["pnl_usd"] == 33055.26     # 其他區塊不受影響
```

- [ ] **Step 4（Warning 3）：`max_pages` 單一來源**

`hl_explore.py` 新增公開函式：

```python
FILLS_MAX_PAGES_ENV = "EXPLORE_FILLS_MAX_PAGES"

def fills_max_pages_from_env() -> int:
    """D5：探索清單與交易員詳情**同一個**分頁上限（兩頁逐位一致的前提）。"""
    return _int(FILLS_MAX_PAGES_ENV, DEFAULT_FILLS_MAX_PAGES)
```
`ExploreConfig.from_env` 改呼叫它；`app.py` 刪除 `TRADER_FILLS_MAX_PAGES = 3`，改在抓 fills 時呼叫 `hl_explore.fills_max_pages_from_env()`。測試：`monkeypatch.setenv("EXPLORE_FILLS_MAX_PAGES", "5")` → `FakeHL` 記錄的 `max_pages` 為 5（explore 與 traders 兩條路徑各一條斷言，或共用一條）。RUNBOOK 在 Task 7 那段後補一行：`EXPLORE_FILLS_MAX_PAGES`（預設 3，每頁 2000 筆；探索與詳情共用；大戶 30 天成交常超過 6000 筆，實測 0xbf73 為 5887+ 筆，調高前先確認 429 情況）。

- [ ] **Step 5（Suggestion 3）：訂單數只算解析成功且 `oid` 非 None 的 fill**

`trader_stats.fills_stats`：把 `oids.add(f.get("oid"))` 移到 Decimal 解析成功之後，且 `if f.get("oid") is not None`。測試：一筆 `px` 為 `"x"` 的壞 fill 與一筆無 `oid` 的 fill → `order_count` 不計入。

- [ ] **Step 6：驗收與 commit**

```bash
uv run ruff check src tests scripts && uv run pytest -q
git add src/spark/filet/leader_perf.py tests/test_leader_perf.py src/spark/filet/trader_stats.py tests/test_trader_stats.py src/spark/publicapi/hl_explore.py src/spark/publicapi/app.py tests/test_public_traders.py tests/test_public_explore.py deploy/RUNBOOK.md
git commit -m "fix: reviewer 修正輪（後端）：跳過比例排除開頭未入金段、詳情頁例外降級與 fills null、max_pages 單一來源"
```

---

### Task 9: reviewer 修正輪（前端） `@inline`

**Files:**
- Modify: `web/src/lib/publicApi.ts`（`fills_30d: TraderFillsStats | null`）
- Modify: `web/src/lib/copy.ts`（`traders.fillsUnavailable`、`explore.ddFilterNoEvidenceNote`，zh/en 對稱）
- Modify: `web/src/app/traders/[address]/page.tsx` ＋ `page.test.tsx`
- Modify: `web/src/app/explore/page.tsx` ＋ `page.test.tsx`

- [ ] **Step 1（Warning 2 前端）**：`fills_30d` 為 `null` → 成交統計卡顯示一行 `c.fillsUnavailable`（zh「成交統計暫時無法取得」），不渲染四個 0。測試一條。
- [ ] **Step 2（Warning 4）**：`sampleInsufficient`（allTime 的 `sample_days < sample_threshold`）**只**控制 `CagrCard`，不再決定比率型指標網格顯示哪些卡；網格一律渲染該窗的全部比率型指標，各自依 `metrics[window_].<key>_insufficient` 顯示「樣本不足」。把 `page.test.tsx` 裡釘死「只剩 1 張小卡」的斷言改成「7 張卡全在、不足的顯示樣本不足」。
- [ ] **Step 3（Suggestion 4）**：回撤卡的 `neg` class 只在 `max_dd_pct < 0` 時加；null／0 用中性樣式。
- [ ] **Step 4（Suggestion 2）**：加一條測試：點四窗按鈕 → 損益數字與指標網格切到該窗（用 `userEvent.click` 或 `fireEvent.click`，沿該檔既有寫法）。
- [ ] **Step 5（Warning 5）**：探索頁「最大回撤 <」chip 啟用時，chip 下方顯示 `c.ddFilterNoEvidenceNote`（zh「回撤算不出的帳戶不在此過濾範圍，其回撤欄顯示 —」）。測試一條。
- [ ] **Step 6：驗收與 commit**

```bash
export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH" && cd web && npm test && npx tsc --noEmit && npm run build
git add web/src/lib/publicApi.ts web/src/lib/copy.ts "web/src/app/traders/[address]/page.tsx" "web/src/app/traders/[address]/page.test.tsx" web/src/app/explore/page.tsx web/src/app/explore/page.test.tsx
git commit -m "fix: reviewer 修正輪（前端）：fills null 顯示、指標網格逐窗不受 allTime 門檻牽制、回撤過濾提示"
```

（`npx tsc --noEmit` 的既有基線錯誤：`explore/page.test.tsx` 的 `.at(-1)?.[0] as string` 與 `EquityCurve.test.tsx:128`，本輪不處理。）

**reviewer 的 Suggestion 1（`min_fills` 預設 200 改以訂單數比對後實質收緊）**：留給使用者裁決，不在本輪改。

## 驗收條款（主線程 verdict 用，預註冊）

1. `uv run pytest -q` 全綠；`tests/test_trader_stats.py` 內 fixture 錨例（221／27／15／55.56／40225.79／33055.26／-2181.94／-74.07／1003）逐條通過。
2. `grep -c "_return_and_drawdown" src/spark/publicapi/hl_explore.py` ＝ 0。
3. 同一份假 HL 資料下，`/api/public/traders/{addr}` 的 `windows`／`live_days`／`fills_30d` 與 `enrich_candidate` 的 `ExploreRow.to_dict()` 對應欄位逐位相等（Task 4 第二條測試）。
4. `web` `npm test` 與 `npm run build` 全綠；`redline.test.ts` 不新增例外。
5. 本機起 API 對真實 HL（可選，主線程執行）：`0x6648…b1f3` 的 30D 損益顯示 `+$33,0xx`（隨時間微變）、回撤 `—`；`0xbf73…5d58` 的 30D 損益為七位數正值、回撤為 -7x% 量級，不再出現 `+1,268,635,318%`。

## 明確不做（YAGNI）

- 不接 Hyperbot OpenAPI；不顯示任何期間報酬率百分比。
- 策略頁（`/strategies*`、`build_strategy_view`、`build_metrics`）不動；`leader_perf` 只加閘門，公式不變。
- 不做「30D 算不出就退 7D」（D9，抽樣證明救不回任何一列）；不做跨窗借數字。
- 不做 fills 分頁的頁間節流（D5 以 `max_pages=3` 壓 burst；若實跑 429 再另開 task）。
