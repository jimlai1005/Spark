# Copytrade Orchestrator M1（引擎移植）實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 spark 內建成 copytrade orchestrator：主網跟單 leader `0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1`，每筆會產生掛單的寫入都經 builder code（f=20）路由，具 kill switch 與 TE/fee 遙測，作為 M2 的 go/no-go。

**Architecture:** 把 hl-copytrader（`/Users/jim/projects/hl-copytrader`，**全程唯讀**）已在主網驗證的引擎邏輯**移植（port，非 import）**進 spark 的 Decimal / adapter-ABC / notifier-注入架構。對帳狀態機、resilience 邊界、sizing 語意 1:1 保留；環境耦合（config 全域、telegram 硬編、模組層可變狀態）全部重構為顯式注入。

**Tech Stack:** Python 3.11 + uv、hyperliquid-python-sdk 0.24.0、pytest（離線、socket-ban）、Decimal 全程（float 只在 SDK 邊界）。

**Spec:** `docs/superpowers/specs/2026-07-16-copytrade-orchestrator-m1-spec.md`（紅線 1–7、驗收 gate 表以該檔為準）。

## 執行狀態（2026-07-16 過夜執行完畢）

**Task 0–18 全部完成**，每任務經 fresh-context 雙階段審查（T8/T13 加 opus 第二意見），全部 APPROVED。
整合驗證：`uv run pytest -q` = **453 passed, 2 deselected**；`uv run ruff check src tests scripts` 乾淨。
主線 commit 對照：T0=14151d2、T1=7ae30e4、T2=40f739f+c87ab6c、T3=fcbba54+285be23、T4=21a4257+269639c、
T5=cde11f2+22973b9、T6=14f2716+218be00、T7=e5898dc+89bd160、T8=e097d59+4fa1bf8、T9=d27f9b0+5aa1a35、
T10=09df513、T11=c57ac12+eb1ad68、T12=bc870a5+8670234、T13=9e3ff99+e45d612、T14=f76f410+e61695d、
T15=2151fb2、T16=d7280b4、T17=c6b675e+aa109ed、T18=edb1f9b+99d6ae3。

**未完成（非 code）**：T5/T17 的 testnet 實跑 BLOCKED 待注資（follower `0x5579b5Ab953d59fc4d40fDA3199a13E91b680B5d` ≥500、
builder `0x63e64A1c73b28Ca4FdFAC409DeE3e2BeE4B84847` ≥100 testnet USDC）；shadow 3 交易日對照屬日曆時間；
主網 dogfood 屬人工關卡（見 runbook）。

**待使用者裁決（2026-07-19 更新）**：~~①T1.3 modify 政策~~ → **已結案**（testnet 實測：modify 不丟失 builder 歸屬，ratio 0.9998；且 HL batchModify 為 post-only 語意，原風險情境結構上不存在 → 保留 `modify-first` 預設。見 `docs/superpowers/research/2026-07-19-testnet-e2e-findings.md`）　②kill switch 緊急平倉 slippage 5% 是否加寬（仍待裁決）
③hl-copytrader 線上機 user@IP（shadow differ 校準用）④trigger 單 M1 不支援（executor 記 skip_trigger，
leader 若掛 trigger 會在 shadow 浮現——煙霧 3 輪未見）⑤sync_failed critical 每輪必發（無 dedup）是否改長 TTL 去重。

---

## 全域紅線（每個任務的實作者與 reviewer 都必須先讀）

1. ⭐ `/Users/jim/projects/hl-copytrader` **唯讀**：不寫入、不重構、不格式化；**不執行其任何程式或測試**（其 `config.py` import 時載入真實 `.env`）。唯一例外：測試中 test-only import 其純函式做交叉比對（Task 9 的受控作法）。
2. ⭐ `ExchangeAdapter` ABC 維持不含 withdraw/transfer；agent key 無提款權；ApproveBuilderFee 只得主錢包簽。
3. ⭐ 每一筆會產生掛單的寫入（place / marketable open / reduce-only close）**必須帶 builder 參數**，注入點集中在 adapter 層。**已知例外：SDK 0.24.0 `modify_order` 簽章沒有 builder 欄位**（已驗證：`hyperliquid/exchange.py:190-199`）——modify 不帶 builder 是 SDK 結構限制，其 fee 歸屬由 Task 5 實測，政策交使用者。
4. 不讀取或印出任何 `.env*`；key 不得出現在 log / repr / 例外訊息（沿 `TxResult.agent_key` 的 `repr=False` 慣例）。
5. `live_trading` 預設 `False`；主網開啟是人工動作。tests 全離線（spark 已有 autouse socket-ban：`tests/conftest.py:1-24`）。
6. M1 只跟 crypto（主 perp DEX）；不碰 xyz / builder-deployed DEX、不碰 spot 鏡像。
7. 內部一律 Decimal；float 轉換只發生在 adapter 與 SDK 的邊界（沿 `_round_px` 慣例，`src/spark/exchange/hyperliquid.py:24-26`）。

## 移植轉換規則（所有 port 任務共用）

| hl-copytrader 慣例 | spark 目標 | 備註 |
|---|---|---|
| dict + float | frozen dataclass + Decimal | API 字串 → `Decimal(str_val)`，在 adapter 邊界完成 |
| `config.py` 模組層全域（load_dotenv） | `CopySettings` frozen dataclass，`from_env()` 顯式建構 | 預設值**逐一照抄** hl `src/config.py:33-115`，不得自創數值 |
| `telegram.py` 模組級呼叫 | `Notifier` ABC 注入 | 引擎不 import 任何通知實作 |
| 模組層可變狀態（`orders._modify_fail_until`） | 顯式 `ReconcileState` 物件傳入 | 語意（120s TTL）不變 |
| `trader.Trader` 方法內建 live gate | `ActionExecutor`（包 adapter 寫入 + live gate + 動作記錄） | dry-run 動作記錄同時餵 shadow |
| `time.time()` / `time.sleep()` 直呼 | 參數注入 `clock` / `sleep_fn`（預設用真的） | 測試不真睡 |
| 讀取失敗回 `failed_dexs` 容錯 | M1 單 DEX：讀取失敗 → 本輪跳過 + warn | 不移植多 DEX 容錯 |

**來源精確位置（實作者按行號讀原始碼移植，不要憑記憶重寫）：**
resilience `src/resilience.py:1-105`；對帳 `src/orders.py`（`_prices_equal:40`、`_orders_match:46`、`_build_desired:79`、`_slot_key:130`、`_ref_px:137`、`_plan:141`、`_set_entry_leverage:187`、`_reconcile_orders:196-274`、`sync_open_orders:277-368`、TTL 常數 `:33,36-37`）；sizing `src/sync.py`（`resolve_capital:21`、`compute_scale_factor:29`、`sync_positions:54-172`）；權重 `src/weight.py:33-98`；保護 `src/protection.py:31-108`；工具 `src/instrument.py`（`_is_spot_coin:13`、`_round_size:23`、`_coin_dex:28`、`_order_type_and_px:33`、`_extract_order_error:50`）；主迴圈 `main.py:122-131,291-292,322-365`（移植排程骨架與斷路器；**頻率語意刻意改為「每次醒來即執行」＝每分鐘**（spec T2.3 拍板），hl 的 `CHECK_MINUTE` hourly 分支（`:337-338`）與 `:324-336` 時段邏輯、`src/market.py` 都**不移植**）；通知 `src/telegram.py:16-51`（dedup/靜音核心）；平倉 `src/trader.py:302-320`（reduce-only IoC 模式）、槓桿快取 `src/trader.py:110-130`；回撤權益 `src/monitor.py` 的 portfolio 總淨值與近期高點查詢（實作者自行定位，同一回應取 current 與 peak——同源比較）。

## 檔案結構（本計畫鎖定）

```
src/spark/
├── resilience.py                    # Task 4：IO resilience 邊界（port）
├── exchange/base.py                 # Task 1,3：型別 + ABC 擴充
├── exchange/hyperliquid.py          # Task 2,3,4：讀寫實作 + ResilientExchange 接線
├── exchange/fakes.py                # Task 1,3：FakeAdapter 擴充
└── copytrade/
    ├── __init__.py                  # Task 6
    ├── config.py                    # Task 6：CopySettings
    ├── notifier.py                  # Task 6：Notifier ABC + Null/Recording；Task 14：Telegram
    ├── instrument.py                # Task 7：純工具 port
    ├── orders.py                    # Task 7,8：對帳引擎
    ├── sizing.py                    # Task 10：scale factor + weight + protection
    ├── positions.py                 # Task 11：sync_positions 安全網
    ├── executor.py                  # Task 12：ActionExecutor（live gate + 動作記錄 + 虛擬簿）
    ├── loop.py                      # Task 12：run_cycle + 主迴圈
    ├── killswitch.py                # Task 13
    ├── shadow.py                    # Task 16：diff 分類核心
    └── report.py                    # Task 15：TE/fee 日報計算
scripts/
├── run_copytrade.py                 # Task 12：CLI（--dry-run/--once/--status/--shadow）
├── panic.py                         # Task 13
├── copytrade_daily_report.py        # Task 15
├── shadow_diff.py                   # Task 16
└── testnet_modify_probe.py          # Task 5
tests/（每個 src 檔對應 test_copy_*.py / test_exchange_*.py，見各任務）
```

## 模型分工與 review gate（執行協定，指揮官用）

| Task | 實作 model | 驗收 agent | 加驗 |
|---|---|---|---|
| 0,6,14,15,18 | haiku（規格鎖死） | sonnet read-back/實跑 | — |
| 1,2,5,9,10,11,16,17 | sonnet | sonnet fresh-context | — |
| 3,4,7 | sonnet | sonnet | ⭐ 紅線逐條檢 |
| 8,13 | sonnet | sonnet | ⭐ + **opus 第二意見**（對帳與 kill switch 是碰錢邏輯） |
| 12 | haiku 起，卡住升 sonnet | sonnet | 紅線 5（live gate）必檢 |

- 每個任務：實作 agent → 跑驗收 → fresh-context 驗收 agent（只拿驗收條件，不拿實作推理）→ commit。同一任務失敗兩輪 → 換方法或升級，不硬撞第三輪。
- 全部 commit 落在 `feat/copytrade-m1` 分支（自 `feat/builder-code-verification` 分出）。不 push、不動 main。
- **今晚可完成**：Task 0–16（code + 離線測試 + mainnet 唯讀 shadow 煙霧測試）。**條件執行**：Task 5 實測、Task 17 實跑需 testnet 三個環境變數（見計畫尾「晨間檢查點」）。**不在今晚**：T4.1 的 3 交易日對照、T4.3 主網 dogfood。

---

### Task 0: 分支、專案 CLAUDE.md、基線

**Files:**
- Create: `CLAUDE.md`
- Modify: `.gitignore`

- [ ] **Step 1: 開分支**

```bash
cd /Users/jim/projects/spark
git checkout -b feat/copytrade-m1 feat/builder-code-verification
```

- [ ] **Step 2: 基線驗證**

Run: `uv run pytest` → Expected: `60 passed, 1 deselected`。
Run: `uv run ruff check src tests scripts` → 記錄結果；若基線本來就有 findings，記下數量，後續任務不得增加。

- [ ] **Step 3: 寫 `CLAUDE.md`**（完整內容如下，一字不差）

```markdown
# spark

Hyperliquid builder-code 基礎設施 + copytrade orchestrator（Filet M1）。Python 3.11 + uv。

## 指令
- 測試（離線）：`uv run pytest`（integration 標記預設跳過）
- Lint：`uv run ruff check src tests scripts`
- Testnet 流程：`uv run python -m scripts.run_testnet_flow`（需 SPARK_ACCOUNT_ID/SPARK_USER_ADDR/SPARK_BUILDER_ADDR）

## 紅線（動之前必問使用者）
1. `/Users/jim/projects/hl-copytrader` 上有線上實盤產品：**唯讀**，不寫入、不執行其程式或測試。
2. 不讀取或印出任何 `.env*`；私鑰不得出現在 log/repr/例外訊息（`TxResult.agent_key` 為 repr=False 慣例）。
3. `ExchangeAdapter` 不含 withdraw/transfer（非託管不變量，tests/test_base_types.py 結構性斷言）。
4. 所有會產生掛單的寫入必帶 builder 參數（SDK `modify_order` 無此欄位為已知例外）。
5. copytrade `live_trading` 預設 False；任何主網寫入（下單/開平倉）是人工決策，不得自動開啟。
6. 測試全離線：autouse socket-ban（tests/conftest.py）；新測試不得連網、不得真發通知。

## 慣例
- 內部一律 Decimal；float 只在 adapter↔SDK 邊界（`_round_px`/`_round_size`）。
- 文件流：docs/superpowers/{specs,plans,research}/，檔名 YYYY-MM-DD-<slug>.md。
- Commit 格式：feat:/fix:/test:/docs: 一行敘述（見 git log）。
- 通知一律走 `spark.copytrade.notifier.Notifier` 注入，引擎不 import 具體實作。
```

- [ ] **Step 4: `.gitignore` 追加 `var/`（copytrade 執行期狀態/報表目錄）與 `.env*`（防呆）**
- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md .gitignore docs/superpowers/specs/2026-07-16-copytrade-orchestrator-m1-spec.md docs/superpowers/plans/2026-07-16-copytrade-orchestrator-m1.md
git commit -m "docs: M1 copytrade spec + implementation plan; project CLAUDE.md"
```

---

### Task 1: 讀側型別 + ABC 擴充 + FakeAdapter（T1.1 上半）

**Files:**
- Modify: `src/spark/exchange/base.py`
- Modify: `src/spark/exchange/fakes.py`
- Test: `tests/test_exchange_read_types.py`

- [ ] **Step 1: 失敗測試** —— 新型別的建構、frozen、Decimal 欄位型別，與 ABC 抽象方法存在性：

```python
"""tests/test_exchange_read_types.py"""
from datetime import datetime, timezone
from decimal import Decimal
import pytest
from spark.exchange.base import (
    OpenOrder, Position, AccountSnapshot, EquityView, UserFill, ExchangeAdapter)

def test_open_order_frozen_decimal():
    o = OpenOrder(oid=1, coin="ETH", is_buy=True, limit_px=Decimal("2000"),
                  sz=Decimal("0.5"), reduce_only=False, is_trigger=False,
                  trigger_px=None, tpsl=None)
    with pytest.raises(Exception):
        o.oid = 2
    assert isinstance(o.limit_px, Decimal)

def test_equity_view_same_source_pair():
    ev = EquityView(current=Decimal("990"), recent_peak=Decimal("1000"))
    assert ev.recent_peak >= ev.current

def test_abc_has_read_methods_and_still_no_withdraw():
    for m in ("get_open_orders", "get_positions", "get_account_state",
              "get_equity_view", "get_user_fills", "get_all_mids",
              "get_size_decimals"):
        assert getattr(ExchangeAdapter, m).__isabstractmethod__
    assert not hasattr(ExchangeAdapter, "withdraw")
    assert not hasattr(ExchangeAdapter, "transfer")
```

- [ ] **Step 2: 跑到失敗** `uv run pytest tests/test_exchange_read_types.py -v` → ImportError
- [ ] **Step 3: 實作** —— `base.py` 追加（全 frozen dataclass、全 Decimal）：

```python
@dataclass(frozen=True)
class OpenOrder:
    oid: int
    coin: str
    is_buy: bool
    limit_px: Decimal
    sz: Decimal
    reduce_only: bool
    is_trigger: bool
    trigger_px: Decimal | None
    tpsl: str | None      # "tp" / "sl" / None

@dataclass(frozen=True)
class Position:
    coin: str
    szi: Decimal          # 有號：多正空負
    entry_px: Decimal
    leverage: int
    is_cross: bool
    unrealized_pnl: Decimal
    margin_used: Decimal

@dataclass(frozen=True)
class AccountSnapshot:
    account_value: Decimal
    total_margin_used: Decimal
    withdrawable: Decimal
    total_ntl_pos: Decimal

@dataclass(frozen=True)
class EquityView:
    """回撤判定用：current 與 recent_peak 必須出自同一次 portfolio 回應（同源比較）。"""
    current: Decimal
    recent_peak: Decimal

@dataclass(frozen=True)
class UserFill:
    time: datetime
    coin: str
    px: Decimal
    sz: Decimal
    side: str             # "B" / "A"
    crossed: bool         # True = taker
    oid: int
    fee: Decimal
```

ABC 追加抽象 reads：`get_open_orders(address) -> list[OpenOrder]`、`get_positions(address) -> list[Position]`、`get_account_state(address) -> AccountSnapshot`、`get_equity_view(address) -> EquityView`、`get_user_fills(address, start: datetime, end: datetime) -> list[UserFill]`、`get_all_mids() -> dict[str, Decimal]`、`get_size_decimals(coin) -> int`。

`FakeAdapter` 補對應實作：建構子收 `open_orders=()、positions=()、account=None、equity=None、fills=()、mids=None、sz_decimals=None`，方法回傳注入值並記錄呼叫（沿 fakes.py 既有記錄風格）。**注意**：既有測試建構 `FakeAdapter` 的呼叫點不得壞——新參數全部給預設值。

- [ ] **Step 4: 全綠** `uv run pytest -q` → 既有 60 + 新測試全過
- [ ] **Step 5: Commit** `git commit -m "feat: typed read-side state + ExchangeAdapter read methods (copytrade M1)"`

---

### Task 2: HyperliquidAdapter 讀側實作（T1.1）

**Files:**
- Modify: `src/spark/exchange/hyperliquid.py`
- Test: `tests/test_hyperliquid_reads.py`

- [ ] **Step 1: 失敗測試** —— 沿 `tests/test_hyperliquid_adapter.py:7-32` 的 FakeInfo 手法，餵 HL API 真實形狀的假回應（欄位名照 SDK/API：`frontend_open_orders` 回傳的 `oid/coin/side/limitPx/sz/reduceOnly/isTrigger/triggerPx/orderType`；`user_state` 的 `assetPositions[].position.{coin,szi,entryPx,leverage,unrealizedPnl,marginUsed}` 與 `marginSummary`；`portfolio` 的權益序列；`user_fills_by_time`；`all_mids`；`meta.universe[].szDecimals`）。斷言：字串數值 → Decimal、side 映射 is_buy、`EquityView.current/recent_peak` 出自**同一次** portfolio 呼叫（FakeInfo 記錄呼叫次數 == 1）。
- [ ] **Step 2: 跑到失敗**
- [ ] **Step 3: 實作** —— 每個 read 對應 SDK Info 呼叫，字串一律 `Decimal(str(x))`；`get_size_decimals` 加 per-coin 快取（port `trader.py:118-130` 快取模式）；`get_equity_view` 用 portfolio 回應同時取 current 與近期峰值（同源，對照 hl `test_drawdown.py` 的「portfolio 總淨值而非 perp 子帳」characterization）。
- [ ] **Step 4: 全綠** + `uv run ruff check src tests`
- [ ] **Step 5: Commit** `git commit -m "feat: HyperliquidAdapter read-side (orders/positions/equity/fills/mids)"`

---

### Task 3: 寫側 ABC + 實作 + builder 強制 CI 測試（T1.1 下半）⭐

**Files:**
- Modify: `src/spark/exchange/base.py`（`Order` 加 `reduce_only: bool = False`；ABC 加 writes）
- Modify: `src/spark/exchange/hyperliquid.py`、`src/spark/exchange/fakes.py`
- Test: `tests/test_exchange_writes_builder.py`

ABC 新增 writes（簽章鎖死）：

```python
@abstractmethod
def cancel_order(self, agent_signer: Signer, coin: str, oid: int) -> bool: ...
@abstractmethod
def modify_order(self, agent_signer: Signer, oid: int, order: Order) -> bool: ...
    # SDK 0.24.0 modify_order 無 builder 參數（結構限制，紅線 3 已知例外）
@abstractmethod
def market_open(self, agent_signer: Signer, coin: str, is_buy: bool, size: Decimal,
                slippage: Decimal, builder: BuilderCode) -> OrderResult: ...
@abstractmethod
def close_reduce_only(self, agent_signer: Signer, coin: str, is_buy: bool, size: Decimal,
                      slippage: Decimal, builder: BuilderCode) -> OrderResult: ...
    # port trader.py:302-320 模式：取 mid ± slippage 的 reduce-only Ioc 限價單。
    # 語意鎖死：is_buy = 平倉「下單」方向（平多倉 → is_buy=False；平空倉 → is_buy=True），
    # 呼叫端自己算 not position_is_long，adapter 不再反轉（hl 原始碼的 close_is_buy=not is_buy
    # 是「部位側」入參慣例，spark 統一用下單側，Task 11/13 呼叫端遵此）。
    # mid 來源固定 get_all_mids()（主 perp DEX；不移植 xyz 專屬 mid 查詢）。
    # 取不到 mid → 回 OrderResult(ok=False, ...)，由呼叫端告警
@abstractmethod
def update_leverage(self, agent_signer: Signer, coin: str, leverage: int,
                    is_cross: bool) -> bool: ...
```

- [ ] **Step 1: 失敗測試** —— 核心是**紅線 3 的結構性測試**：

```python
"""tests/test_exchange_writes_builder.py —— 紅線 3 CI 守門"""
# FakeExchange 記錄 (method, args, kwargs)。對每一個 order-creating 寫入：
def test_all_order_creating_writes_carry_builder(adapter, fake_exchange, builder):
    adapter.place_order(sig, order, builder)
    adapter.market_open(sig, "ETH", True, Decimal("0.1"), Decimal("0.05"), builder)
    adapter.close_reduce_only(sig, "ETH", False, Decimal("0.1"), Decimal("0.05"), builder)
    creating = [c for c in fake_exchange.calls if c.method in ("order", "market_open")]
    assert creating, "沒有捕捉到任何下單呼叫"
    for c in creating:
        assert c.kwargs.get("builder") == {"b": builder.b, "f": builder.f}, \
            f"{c.method} 未帶 builder"
def test_modify_has_no_builder_kwarg_documented(...):  # SDK 限制的顯式紀錄
def test_close_reduce_only_sets_reduce_only_and_ioc(...)
def test_close_reduce_only_no_mid_returns_not_ok_without_raising(...)
def test_abc_still_has_no_withdraw_transfer(...)      # 紅線 2 重申
```

- [ ] **Step 2: 跑到失敗**
- [ ] **Step 3: 實作** —— SDK 映射：`cancel(name, oid)`、`modify_order(oid, name, is_buy, sz, limit_px, order_type, reduce_only)`、`market_open(name, is_buy, sz, px=None, slippage, builder=...)`、`order(..., reduce_only=True, builder=...)`（close）、`update_leverage(leverage, name, is_cross)`。Decimal→float 只在此處（`_round_px`/`float()`）。`FakeAdapter` 同步補齊並記錄動作。
- [ ] **Step 4: 全綠**；**Step 5: Commit** `git commit -m "feat: write-side adapter methods; builder param enforced on all order-creating paths"`

---

### Task 4: resilience 邊界移植 + 接線（T1.2）⭐

**Files:**
- Create: `src/spark/resilience.py`
- Modify: `src/spark/exchange/hyperliquid.py`（建構時把 `_exchange` 包進 `ResilientExchange`）
- Test: `tests/test_resilience.py`、`tests/test_resilience_boundary.py`

- [ ] **Step 1: 失敗測試** —— 以 hl `tests/test_resilience.py`（153 行）為藍本逐案移植 characterization：transient 分類（connection reset/aborted/broken、timeout、502/503/504 → True；語意錯誤 → False）、冪等重試 3 次指數退避、verify-then-retry 三態（verify False → 重送；True → 不重送；**verify 拋例外 → 當已送達不重送**——「寧漏跟不重複下單」，對照 hl `resilience.py:40` docstring 與 `docs/superpowers/specs/2026-06-21-io-resilience-boundary-design.md:95-103`）、`reduce_only=True` 自動判冪等、modify 不重試。邊界測試 port hl `test_resilience_boundary.py`：斷言 `HyperliquidAdapter` 的 `_exchange` 型別為 `ResilientExchange`。`sleep_fn` 注入，測試不真睡。
- [ ] **Step 2: 跑到失敗**；**Step 3: 實作**（port `src/resilience.py:1-105`，1:1 語意；`RETRY_ATTEMPTS=3`、TTL/延遲常數照抄）；**Step 4: 全綠**
- [ ] **Step 5: Commit** `git commit -m "feat: IO resilience boundary (port) wired into HyperliquidAdapter writes"`

---

### Task 5: modify 的 builder 歸屬探針（T1.3）⭐ —— 腳本 + 靜態分析；實測需 testnet 環境變數

**Files:**
- Create: `scripts/testnet_modify_probe.py`
- Create: `docs/superpowers/research/2026-07-16-modify-builder-attribution.md`

- [ ] **Step 1: 寫探針腳本**（模式沿 `scripts/run_testnet_flow.py`）。流程：
  1. baseline = `query_builder_accrued`
  2. **對照組**：place 帶 builder 的 marketable Ioc（沿 orchestrator）→ `wait_for_accrual(baseline)` → 記錄增量 Δ_place
  3. **實驗組**：place 帶 builder 的**遠端掛單**（mid×0.7，GTC）→ `modify_order` 改價到可成交（mid×1.05）→ 等成交 → `wait_for_accrual(新 baseline)` → 記錄增量 Δ_modify
  4. 印出結構化結果（兩組的 size、名目、預期費、實際增量），**絕不印 key**
- [ ] **Step 2: 靜態分析落檔**到 research 報告：SDK 0.24.0 `modify_order` 簽章（`hyperliquid/exchange.py:190-199`）無 builder 欄位 → 兩個假說：(a) modify 後的新單繼承原單 builder 歸屬（Δ_modify ≈ 預期費）；(b) 歸屬丟失（Δ_modify ≈ 0）。報告含政策選項：**容忍漏繳**（modify-first 保留，接受主動路徑 fee 缺口）vs **強制 cancel+place**（`modify_policy="cancel-place"`，多一次往返、犧牲佇列位置）。**政策由使用者裁決，本計畫一律不得自行切換預設。**
- [ ] **Step 3（條件）**：若 testnet 三變數已提供 → `SPARK_...=... uv run python -m scripts.testnet_modify_probe` 實跑，數據寫進報告。未提供 → 報告標記 `BLOCKED: 待 testnet 憑證`，此任務其餘部分照常 commit。
- [ ] **Step 4: Commit** `git commit -m "feat: testnet probe for modify builder-fee attribution (T1.3)"`

---

### Task 6: CopySettings + Notifier 介面（引擎前置）

**Files:**
- Create: `src/spark/copytrade/__init__.py`、`src/spark/copytrade/config.py`、`src/spark/copytrade/notifier.py`、`src/spark/copytrade/executor.py`（本任務只放 `ExecutorPort` Protocol；`ActionExecutor` 實作在 Task 12 同檔補上）
- Test: `tests/test_copy_config.py`、`tests/test_copy_notifier_base.py`

`ExecutorPort`（`typing.Protocol`, `@runtime_checkable`，簽章鎖死——Task 8/11/13 的 FakeExecutor 與 Task 12 的 `ActionExecutor` 都必須符合，各測試加 `isinstance` 斷言防漂移）：

```python
@runtime_checkable
class ExecutorPort(Protocol):
    records: list  # ActionRecord（Task 12 定義；fake 可用同形 dict）
    def place(self, spec: OrderSpec) -> bool: ...
    def modify(self, oid: int, spec: OrderSpec) -> bool: ...
    def cancel(self, coin: str, oid: int) -> bool: ...
    def market_open(self, coin: str, is_buy: bool, size: Decimal) -> OrderResult: ...
    def close_reduce_only(self, coin: str, is_buy: bool, size: Decimal) -> OrderResult: ...
    def update_leverage(self, coin: str, leverage: int, is_cross: bool) -> bool: ...
    def get_open_orders(self) -> list[OpenOrder]: ...   # settle 驗證用（live 讀真、dry 讀虛擬簿）
```
（builder/agent_signer/slippage 不在 Port 簽章上——它們是 executor 建構時注入的，引擎不經手，紅線 3 的注入點維持在 adapter/executor 層。）

- [ ] **Step 1: 失敗測試**：`CopySettings.from_env({})` 全預設可建構、`live_trading is False`、`leader_address == "0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1"`、`modify_policy == "modify-first"`、`flatten_on_breach is True`、`allocated_capital == 0`（碰錢預設，斷言釘死）、非法值（負 interval、dd_pct≥1）raise。Notifier：`RecordingNotifier` 收 `(level, category, text, dedup_key)`；`NullNotifier` 全吞。`ExecutorPort` Protocol（見下）可被最小 fake 滿足且 `runtime_checkable`。
- [ ] **Step 2: 跑到失敗**；**Step 3: 實作**：

```python
@dataclass(frozen=True)
class CopySettings:
    leader_address: str = "0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1"
    live_trading: bool = False            # 紅線 5（刻意覆蓋）
    interval_s: int = 60                  # spec T2.3：預設每分鐘（刻意覆蓋 hl 的 hourly CHECK_MINUTE）
    modify_policy: str = "modify-first"   # 或 "cancel-place"；預設不得改（等 T1.3 裁決）
    flatten_on_breach: bool = True        # 拍板 #2：回撤觸發自動 reduce-only 全平，預設開
    allocated_capital: Decimal = Decimal("0")  # 刻意覆蓋 hl 的 5000（那是它自己的部署值）：
                                          # 0=用全權益；dogfood 1000 USDC 全額即權益本身
    # 其餘數值欄位的預設值：照抄 hl src/config.py:33-115 對應常數，不得自創——
    # capital_utilization / position_weight / max_target_leverage / min_order_notional /
    # size_tolerance / max_drawdown_pct / settle_seconds(=2) / modify_fail_ttl_s(=120) /
    # max_consecutive_errors(=5) / volatility_weight_enabled / holding_protection_enabled(False)
    # 兩個例外（值在函式層預設而非 config.py）：px_rel_tol=Decimal("1e-4")（hl orders.py:40）、
    # slippage=Decimal("0.05")（hl trader.py:312 硬編）——照抄這兩個數值
    # 刻意覆蓋 hl 預設的欄位僅限上列五個有註明者；其餘一律照抄，reviewer 必檢
```

`from_env(env: Mapping)` 用 `COPY_*` 變數名；**行內註解容錯**移植 hl `_env_bool/_env_float/_env_int/_env_str`（`src/config.py` 對應；測試 port `test_config_inline_comment.py` 案例）。Notifier ABC：`info/warn/critical(category, text, dedup_key=None)`。
- [ ] **Step 4: 全綠**；**Step 5: Commit** `git commit -m "feat: CopySettings (env-driven, live off by default) + Notifier ABC"`

---

### Task 7: 對帳純函式移植 + characterization（T2.1 上半）⭐

**Files:**
- Create: `src/spark/copytrade/instrument.py`、`src/spark/copytrade/orders.py`（純函式部分）
- Test: `tests/test_copy_instrument.py`、`tests/test_copy_orders_plan.py`

簽章鎖死（`OrderSpec` 是引擎內部的統一表示，`OpenOrder.to_spec()` 轉換）：

```python
@dataclass(frozen=True)
class OrderSpec:
    coin: str; is_buy: bool; sz: Decimal; limit_px: Decimal
    reduce_only: bool = False; is_trigger: bool = False
    tpsl: str | None = None; trigger_px: Decimal | None = None; is_market: bool = False

def _prices_equal(a: Decimal, b: Decimal, rel: Decimal) -> bool
def _orders_match(desired: OrderSpec, mine: OrderSpec, *, px_rel_tol: Decimal, size_tol: Decimal) -> bool
def _slot_key(o: OrderSpec) -> tuple      # (coin, is_buy, reduce_only, is_trigger, tpsl|None)
def _ref_px(o: OrderSpec) -> Decimal
@dataclass(frozen=True)
class ReconcilePlan:
    modifies: tuple[tuple[int, OrderSpec], ...]
    to_place: tuple[OrderSpec, ...]
    to_cancel: tuple[int, ...]
    matched: frozenset[int]
def _plan(desired: list[OrderSpec], mine: list[tuple[int, OrderSpec]],
          *, px_rel_tol: Decimal, size_tol: Decimal) -> ReconcilePlan
def _build_desired(leader_orders: list[OpenOrder], scale: Decimal, *, min_notional: Decimal,
                   size_decimals: Callable[[str], int], my_positions: dict[str, Position],
                   protected: set[str]) -> tuple[list[OrderSpec], list[SkippedOrder], list[str]]
    # SkippedOrder(coin: str, notional: Decimal, reason: str)  reason ∈ {"small","spot","protected","reduce_only_no_pos"}
```

- [ ] **Step 1: 失敗測試** —— characterization 場景（各自獨立測試函式，數字具體）：
  1. 完全相符 → 全 matched、零動作（ETH buy 1.0@2000 vs 同單 oid=7）
  2. 同 slot 價移 → `modifies==((7, spec@2010),)`（mine @2000）
  3. px 相對差 9.5e-5（2000 vs 2000.19，同 size）→ matched；相對差 2.5e-4（2000 vs 2000.5）→ modify
  4. size 差在 `size_tol` 內 → matched；超過 → modify
  5. 同 slot desired 多一張 → 1 modify + 1 place（依 `_ref_px` 排序後依索引配對——用三張不同價驗證配對順序）
  6. mine 多 → cancel；不同 slot（buy vs sell、reduce_only 異）→ 絕不 modify，走 place+cancel
  7. `_build_desired`：縮放後名目 < min_notional → 進 skipped_small 且記 notional；spot coin 跳過；reduce-only 但無對應部位跳過（port hl `test_reduce_only_guard.py` 案例）；protected coin 跳過
  8. `_round_size` / `_is_spot_coin` / `_coin_dex` / `_order_type_and_px` / `_extract_order_error`（port hl `test_order_error_parse.py`、`test_meta_lookup.py` 相關案例；xyz 專屬分支不移植）
- [ ] **Step 2: 跑到失敗**；**Step 3: 移植實作**（來源 `src/orders.py:40-184`、`src/instrument.py`；語意 1:1，容忍度改參數注入）；**Step 4: 全綠**
- [ ] **Step 5: Commit** `git commit -m "feat: reconcile pure functions ported with characterization tests (T2.1a)"`

---

### Task 8: `_reconcile_orders` + `sync_open_orders`（T2.1 下半）⭐（驗收加 opus 第二意見）

**Files:**
- Modify: `src/spark/copytrade/orders.py`
- Test: `tests/test_copy_orders_reconcile.py`

簽章鎖死：

```python
@dataclass
class ReconcileState:
    modify_fail_until: dict[str, float]   # coin -> epoch；TTL=settings.modify_fail_ttl_s

@dataclass(frozen=True)
class ReconcileResult:
    placed: int; cancelled: int; modified: int; matched: int; sync_failed: bool
    skipped_small: tuple[SkippedOrder, ...]

@dataclass(frozen=True)
class CycleReport:
    reconcile: ReconcileResult
    safety_net: dict          # sync_positions 回報（Task 11）
    scale: Decimal
    tripped: bool = False     # kill switch 狀態（Task 13 接入）

def _reconcile_orders(ex: ExecutorPort, desired: list[OrderSpec],
                      my_orders: list[OpenOrder], *, settings: CopySettings,
                      notifier: Notifier, state: ReconcileState,
                      clock=time.time, sleep_fn=time.sleep) -> ReconcileResult
def sync_open_orders(ex, leader_orders, my_orders, my_positions, scale, *,
                     settings, notifier, state, skip_safety_net: bool = False, ...) -> CycleReport
```

狀態機（port `src/orders.py:196-368`，順序不得動）：matched 保留 → modify（TTL 內跳過；`modify_policy=="cancel-place"` 時全部降級）→ modify 失敗登記 TTL 入 fallback → **先 cancel（fallback 舊單 + to_cancel）釋放保證金** → 後 place（to_place + fallback 新規格）→ live 時 settle（`settle_seconds`）後重抓驗證 → 缺/多修正一次 → 再驗 → 仍不符 `sync_failed=True` + `notifier.critical`。

- [ ] **Step 1: 失敗測試**（用 `FakeExecutor`/RecordingNotifier、假 clock、`sleep_fn=lambda s: None`）：
  1. happy path：modify 成功，動作序列正確
  2. modify 回 False → TTL 登記 + 同輪 cancel+place 補位；**cancel 全部先於 place**（斷言動作順序）
  3. TTL 未過 → 直接跳過 modify 走 cancel+place；TTL 過期 → 恢復 modify
  4. `modify_policy="cancel-place"` → 零 modify 呼叫
  5. **settle 驗證分支（hl 未測的 `orders.py:253-271`，此處必須補上）**：live=True、settle 後仍缺一張 → 重試補placed；重試後仍不符 → `sync_failed=True` + critical 通知被記錄
  6. live=False → executor 零真實寫入、動作全進記錄（dry-run 語意）
- [ ] **Step 2: 跑到失敗**；**Step 3: 移植**；**Step 4: 全綠**
- [ ] **Step 5: Commit** `git commit -m "feat: reconcile state machine + sync_open_orders ported; settle-verify branch covered (T2.1b)"`

---

### Task 9: Parity 交叉比對（T2.1 收尾）

**Files:**
- Modify: `pyproject.toml`（dev 依賴加 `python-dotenv`——hl config import 需要；僅測試用）
- Test: `tests/test_copy_parity.py`

受控 test-only import（紅線 1 的唯一例外通道）：

```python
HL = Path("/Users/jim/projects/hl-copytrader")
pytestmark = pytest.mark.skipif(not HL.exists(), reason="hl-copytrader 不在此機器")

@pytest.fixture
def hl_plan():
    snapshot = dict(os.environ)
    sys.path.insert(0, str(HL))
    try:
        from src.orders import _plan as hl_plan_fn, _orders_match as hl_match_fn
        yield hl_plan_fn, hl_match_fn
    finally:
        sys.path.remove(str(HL))
        os.environ.clear(); os.environ.update(snapshot)   # 洗掉 load_dotenv 汙染
```

安全補強（紅線 4）：import 完成後**立即**從 `os.environ` 刪除敏感鍵（`WALLET_PRIVATE_KEY` 等，
在 yield 之前就洗，不等 teardown）；parity 測試任何斷言訊息不得包含 `os.environ` 內容或
hl 設定值；只比對 `_plan`/`_orders_match` 的輸入輸出。

- [ ] **Step 1: 寫比對測試**：產生 ≥200 個偽隨機場景（固定 seed；價格/數量/方向/reduce_only/多 slot 組合），每個場景同時餵 hl `_plan`（dict+float）與 spark `_plan`（OrderSpec+Decimal，容忍度用 hl 常數同值），斷言動作多重集等價（modify 配對的 (oid, 目標價±1e-9)、place 集合、cancel 集合一一相等）。`_orders_match` 另做 500 組單點比對。
- [ ] **Step 2: 跑**——注意 socket-ban fixture 全程有效（import hl 模組不觸網；telegram 發送被 spark 測試永不呼叫）。過 → **Step 3: Commit** `git commit -m "test: cross-implementation parity for _plan/_orders_match vs hl-copytrader"`
- 若比對揭露語意分歧：**這是移植 bug**，回 Task 7/8 修 spark 側，不得調測試遷就。

---

### Task 10: Sizing + weight + protection（T2.2 上半）

**Files:**
- Create: `src/spark/copytrade/sizing.py`
- Test: `tests/test_copy_sizing.py`

```python
def resolve_capital(my_equity: Decimal, allocated: Decimal) -> Decimal
def compute_scale_factor(leader_equity: Decimal, my_equity: Decimal, *,
                         target_notional: Decimal, settings: CopySettings,
                         weight: Decimal) -> Decimal
@dataclass(frozen=True)
class VolStats:
    mu: Decimal; sigma: Decimal; z: Decimal; weight: Decimal   # 欄位語意照 hl weight.py:33-54
def compute_volatility_stats(daily_abs_pnl: list[Decimal], equity: Decimal) -> VolStats | None
def position_weight(settings: CopySettings, vol: VolStats | None) -> Decimal
def anti_holding_flags(...)   # port protection.py:31-108；settings.holding_protection_enabled=False 預設短路
```

- [ ] **Step 1: 失敗測試**（數字對照 hl 公式手算）：`scale = resolve_capital × utilization × weight ÷ leader_equity`；`MAX_TARGET_LEVERAGE>0` 且 `eff_lev=target_notional/leader_equity` 超標 → `scale ×= max_lev/eff_lev`（給一組具體數字：leader_equity=100000、notional=800000、max_lev=4 → eff_lev=8 → scale 減半）；`allocated>0` 蓋過 my_equity；weight 純數學案例 port hl `test_vol_partial.py`（含資料不足天數回 None → weight 退回 position_weight）；protection 預設關 → flags 全 False。
- [ ] **Step 2–4: 紅→實作→綠**（來源 `src/sync.py:21-51`、`src/weight.py:33-98`、`src/protection.py`；`_daily_abs_pnl` 的網路呼叫**不在此模組**——由呼叫端經 adapter 取數列注入，維持本模組純函式）
- [ ] **Step 5: Commit** `git commit -m "feat: scale factor + volatility weight + holding protection (pure, injected inputs)"`

---

### Task 11: `sync_positions` 部位安全網（T2.2 下半）

**Files:**
- Create: `src/spark/copytrade/positions.py`
- Test: `tests/test_copy_sync_positions.py`

- [ ] **Step 1: 失敗測試**（FakeExecutor 記錄動作；三分支各至少 2 案例，port `src/sync.py:54-172` 語意）：
  1. 新開：leader 有 ETH 多倉、我無 → `market_open`（帶 builder——斷言 kwargs）；protection flag 命中 → 跳過並 warn
  2. 調整：同向 size 差 > tol → 加/減倉；反向 → 全平再開（port `trader.py:269-279` 決策，執行動作為 close_reduce_only + market_open）；protection 只擋同向加倉
  3. 趨平：leader 已平我還有 → `close_reduce_only`；名目 < min_notional 的目標 → 跳過只 debug（`sync.py:100-102` 語意）
  4. 開倉前 `update_leverage`（per-coin 快取：同 coin 第二次不再呼叫——port `trader.py:118-130`）
- [ ] **Step 2–4: 紅→移植→綠**；**Step 5: Commit** `git commit -m "feat: position safety net ported (open/adjust/flatten, builder on all opens/closes)"`

---

### Task 12: ActionExecutor + 主迴圈 + CLI（T2.3；紅線 5 gate）

**Files:**
- Create: `src/spark/copytrade/executor.py`、`src/spark/copytrade/loop.py`
- Create: `scripts/run_copytrade.py`
- Test: `tests/test_copy_executor.py`、`tests/test_copy_loop.py`

```python
@dataclass(frozen=True)
class ActionRecord:
    ts: float; kind: str   # "place"|"modify"|"cancel"|"market_open"|"close"|"update_leverage"
    coin: str; payload: dict   # 全 str/Decimal-str，不含任何 key

class VirtualBook:
    """dry-run 用的記憶體掛單簿：place/modify/cancel 施加於 list[OpenOrder]（oid 自增），
    使 --shadow 連續多輪時 desired vs 虛擬簿能收斂，模擬引擎穩態。"""

class ActionExecutor:
    """唯一寫入通道。live=False：記錄動作 + 更新虛擬簿，不碰 adapter 寫入。
    live=True：轉呼叫 adapter（其內已有 resilience），同樣記錄。"""
    def __init__(self, adapter, agent_signer, builder: BuilderCode, *,
                 live: bool, virtual_book: VirtualBook | None = None): ...
    # place/modify/cancel/market_open/close_reduce_only/update_leverage + records: list[ActionRecord]

def run_cycle(adapter, ex, settings, notifier, state) -> CycleReport
    # 讀 leader(orders/state) + 我方(orders/positions/equity) → killswitch 檢查（Task 13 接入）
    # → scale → sync_open_orders（A 掛單 + B 安全網）→ CycleReport
def main_loop(...)  # 對齊整分、minute-key 防重跑（port main.py:122-131）、
                    # 連續錯誤 ≥ max_consecutive_errors → critical + sys.exit(1)（port main.py:291-292,351-363）
```

- [ ] **Step 1: 失敗測試**：
  1. **live=False 時 adapter 寫入方法零呼叫**（FakeAdapter 記錄為證）、records 齊全、虛擬簿隨 place/cancel/modify 演化
  2. live=True 轉呼叫 + 記錄
  3. minute-key 同分鐘不重跑；連續 5 次例外 → critical + SystemExit；成功歸零
  4. CLI 參數解析：`--once/--dry-run/--status/--shadow` 組合（`--dry-run` 強制 live=False 即使 env 開了 live；`--status` 只讀不寫）
- [ ] **Step 2–4: 紅→實作→綠**（CLI 風格沿 `scripts/run_testnet_flow.py`：env 讀變數、`python -m scripts.run_copytrade`）
- [ ] **Step 5: Commit** `git commit -m "feat: action executor with live gate + fixed-interval loop + CLI (live off by default)"`

---

### Task 13: Kill switch + panic（T3.1）⭐（驗收加 opus 第二意見）

**Files:**
- Create: `src/spark/copytrade/killswitch.py`、`scripts/panic.py`
- Test: `tests/test_copy_killswitch.py`

```python
@dataclass(frozen=True)
class DrawdownStatus:
    current: Decimal; peak: Decimal; drawdown_pct: Decimal; breached: bool

def check_drawdown(ev: EquityView, max_dd_pct: Decimal) -> DrawdownStatus
    # 純函式；current 與 peak 同出一次 get_equity_view（同源，工程原則 1）
    # drawdown = (peak - current) / peak；peak<=0 → breached=False + 呼叫端 warn

ARM_FILE = "var/copytrade/killswitch.tripped"
def is_tripped(root: Path) -> bool
def trip(ex, my_positions, notifier, root: Path, status: DrawdownStatus) -> FlattenReport
    # 1) cancel 全部 resting  2) 每個部位 close_reduce_only（帶 builder）
    # 3) 寫 ARM_FILE（內容：時間戳+status 數字）  4) notifier.critical
    # 任何 close 失敗：該 coin 記入 FlattenReport.failures + 逐 coin critical——
    # 絕不靜默；不無限自動重試（resilience 的 transient 3 次重試除外）；
    # ARM_FILE 一樣要寫（部分平倉也要鎖死）
```

主迴圈接入（Task 12 的 `run_cycle` 開頭）：`is_tripped` → 本輪只讀報狀態 + 每小時一次 critical 提醒，**不做任何交易動作**；未 tripped → `check_drawdown`，breached 且 `settings.flatten_on_breach`（**預設 True，已拍板**）→ `trip()`。re-arm = 人工刪除 ARM_FILE（runbook 寫明），程式不提供自動恢復路徑。

- [ ] **Step 1: 失敗測試**：
  1. `check_drawdown` 數字案例：peak=1000/current=900/max=0.15 → 未觸；current=840 → 觸發（dd=0.16）
  2. trip 動作順序：cancel 全部 **先於** close；每倉 close_reduce_only 帶 builder；ARM_FILE 落地含數字
  3. 一個 close 失敗（ok=False）→ failures 記錄 + critical ≥2 則（總告警+逐 coin）+ ARM_FILE 仍寫
  4. is_tripped=True → run_cycle 零寫入動作（整合測試放 test_copy_loop.py）
  5. `panic.py`：無 `--yes` 只印將執行動作（dry）；`--yes` 走 trip 全流程（FakeAdapter 驗）
- [ ] **Step 2–4: 紅→實作→綠**；**Step 5: Commit** `git commit -m "feat: drawdown kill switch (flatten default on, manual re-arm) + panic script"`

---

### Task 14: TelegramNotifier（T3.2）

**Files:**
- Modify: `src/spark/copytrade/notifier.py`
- Test: `tests/test_copy_notifier_telegram.py`

- [ ] **Step 1: 失敗測試**：無 token/chat → 全部靜默回 False（不 raise）；dedup：同 key 300s 內第二則被吞、TTL 過再送（假 clock）；分類靜音：`muted_categories={"orders"}` → info(orders) 吞、critical **永不可靜音**；發送函式注入（`send_fn`）——測試斷言 payload 文字含 level 前綴（`[INFO]/[WARN]/[CRIT]`），**絕不真連網**（socket-ban 本來就會擋，測試不 monkeypatch socket 例外路徑）；send_fn 拋例外 → 吞掉 + 回 False（通知失敗不得炸引擎，port `telegram.py:38-51` 語意）。
- [ ] **Step 2–4: 紅→實作→綠**（dedup/靜音核心 port `src/telegram.py:16-51`；token/chat 從 `COPY_TG_BOT_TOKEN`/`COPY_TG_CHAT_ID` env 讀，缺省靜默）
- [ ] **Step 5: Commit** `git commit -m "feat: leveled Telegram notifier (dedup, category mute, critical unmutable)"`

---

### Task 15: TE + fee 日報（T3.3）

**Files:**
- Create: `src/spark/copytrade/report.py`、`scripts/copytrade_daily_report.py`
- Test: `tests/test_copy_report.py`

```python
@dataclass(frozen=True)
class DailyReport:
    day: date
    pair_count: int
    median_delay_s: Decimal | None
    taker_share: Decimal          # 我方 taker(crossed) 成交量 / 總成交量 —— safety-net 代理指標
    taker_slippage_bp_median: Decimal | None   # taker fill px vs 配對 leader fill px，方向修正
    skipped_small_notional: Decimal
    skipped_small_ratio: Decimal  # skipped / (mirrored+skipped)，1000 USDC 固有失真的量化
    accrued_delta: Decimal        # query_builder_accrued 今日增量（vs 昨日快照，var/ 持久化）
    csv_matched: bool | None      # 三態推導：ReconcileReport.fill_count > 0 → 用 .matched；
                                  # fill_count == 0 → None（「CSV 無資料或無成交」）。
                                  # 已知缺口：fetch_builder_fills 把 403/404 吞成空（hyperliquid.py:46-53），
                                  # 故 None 涵蓋「被拒/無檔/真無成交」三種，報告文字如實寫，不得標成相符
def build_daily_report(leader_fills, my_fills, skipped_log, accrued_today, accrued_prev, csv_report) -> DailyReport
def pair_fills(leader_fills, my_fills, window_s: int) -> list[PairedFill]   # 同 coin 同向、時間窗內最近鄰
```

- [ ] **Step 1: 失敗測試**：配對（窗內配到/窗外不配）、延遲中位數、taker_share 手算案例、滑價 bp 方向（買貴=正滑價、賣低=正滑價）、skipped ratio、accrued delta、csv 403 → `csv_matched is None` 且報告文字含「CSV 無資料」。
- [ ] **Step 2–4: 紅→實作→綠**（腳本組裝：adapter fills + var/ 快照 + notifier.info 全文；報表落 `var/copytrade/reports/YYYY-MM-DD.md`）
- [ ] **Step 5: Commit** `git commit -m "feat: daily TE + fee accrual report (taker share, slippage, skipped-small telemetry)"`

---

### Task 16: Shadow 模式 + differ（T4.1 建置）

**Files:**
- Create: `src/spark/copytrade/shadow.py`、`scripts/shadow_diff.py`
- Modify: `scripts/run_copytrade.py`（`--shadow`：dry-run + ActionRecord 逐輪落 `var/copytrade/shadow/YYYYMMDD.jsonl`）
- Test: `tests/test_copy_shadow.py`

- [ ] **Step 1: 失敗測試**：ActionRecord JSONL round-trip（不含 key、Decimal 存字串）；diff 分類器——輸入兩份動作集，輸出三類：`match`（容忍度內）、`explainable`（純數值差且差比 == scale/weight 參數比）、`unexplained`（結構差：方向/coin/動作種類不一致）；hl log 解析器用 **synthetic 樣本行**（從 hl `src/orders.py`/`main.py`/`telegram.py` 的 log 字串格式逆推——實作者先讀原始碼確認格式，測試樣本照抄該格式，不得臆造）。
- [ ] **Step 2–4: 紅→實作→綠**
- [ ] **Step 5: 煙霧實測（唯讀主網，零憑證零風險）**：確認環境**未設** `COPY_TG_BOT_TOKEN`（通知走 NullNotifier/靜默，不真發），`uv run python -m scripts.run_copytrade --shadow --once` 對 leader `0xf97ad…` 實跑 3 輪（真實公開資料、dry-run、虛擬簿演化）。驗：跑通、JSONL 落地、`unexplained` 分類器對虛擬簿自我一致（第二輪起 desired vs 虛擬簿應收斂到低動作數）。輸出摘要記到 research 報告附錄。
- [ ] **Step 6: Commit** `git commit -m "feat: shadow mode with action-record diff harness; mainnet read-only smoke ok"`

---

### Task 17: Testnet E2E（T4.2）—— 測試 + runbook；實跑條件同 Task 5

**Files:**
- Create: `tests/integration/test_copytrade_testnet.py`（`@pytest.mark.integration`）
- Create: `docs/superpowers/research/2026-07-16-copytrade-testnet-e2e.md`

- [ ] **Step 1: 寫整合測試**（模式沿 `tests/integration/test_testnet_flow.py`）：onboard（冪等重用）→ 遠端掛單（帶 builder）→ modify 改價 → 成交 → `wait_for_accrual(baseline)`：place 與 modify 兩路徑的 accrual 分開斷言（呼應 gate「place 與 modify 分開驗」）→ reduce-only 平倉。
- [ ] **Step 1b（選配，晨間裁決）**：spec 把「xyz 的 builder accrual 獨立驗證」放在 T4.2——這是 testnet 探針實驗（xyz 掛單帶 builder 驗 accrual），**不是**開放引擎跟 xyz（紅線 6 的跟單禁令不變）。testnet 若無 xyz 市場或使用者未點頭，標記 deferred 並寫入 research 檔。
- [ ] **Step 2（條件）**：憑證在 → `uv run pytest tests/integration/test_copytrade_testnet.py -m integration -v` 實跑，結果寫 research 檔。
- [ ] **Step 3: Commit** `git commit -m "test: copytrade testnet e2e (place+modify accrual paths, reduce-only close)"`

---

### Task 18: Dogfood runbook（T4.3 文件）

**Files:**
- Create: `docs/superpowers/research/2026-07-16-dogfood-runbook.md`

- [ ] **Step 1: 寫 runbook**：新錢包注資 1000 USDC → `bootstrap_keys` → onboarding → shadow 觀察 3 交易日（gate：不可解釋差異=0）→ `COPY_LIVE_TRADING=true` 人工開啟（明列這是人工動作）→ 觀察清單（sync_failed、taker share、accrual 日增）→ kill switch 實彈演練步驟（含故意斷網驗 flatten 失敗告警升級）→ 回滾程序（panic → 撤資 → live=false）。每步含確切指令與預期輸出。
- [ ] **Step 2: Commit** `git commit -m "docs: mainnet dogfood runbook (M1 gate)"`

---

## 驗收 gate（M1 全程，非今晚）

沿 spec 表：Shadow 連續 3 交易日不可解釋差異=0｜主網 place 與 modify 分開驗 accrual 100%｜safety-net 滑價 median ≤10bp、占比 <30%｜skipped-small 已量化進日報｜sync_failed 持續告警=0、日對帳 drift=0｜kill switch 演練成功、agent key 全程無提款權。

## 晨間檢查點（使用者裁決，過夜執行不得代決）

1. ~~**T1.3 政策**：modify 丟失 builder 歸屬時——容忍漏繳 vs 強制 cancel+place~~ → **2026-07-19 結案**：testnet 實測 modify **不丟失**歸屬（A/B ratio 0.99978 / 0.99984，二次獨立重現＋非 modify maker 對照組排除競爭解釋）；更關鍵：HL `batchModify` 帶 **post-only 語意**（穿價 modify 直接被拒），modify 後的單只能掛著等 maker 成交，「modify 成 taker 立即成交而漏繳」這條路徑**結構上不存在**——本裁決題因前提不成立而消解。**處置：保留 `modify_policy="modify-first"` 預設。** 極限：數據為 testnet ETH / f=20 / size=0.01 / 單筆成交 / builder==user，未測連續多次 modify 與 size 放大；主網收費前建議小額覆測一次。
2. **Testnet 憑證**：`SPARK_ACCOUNT_ID / SPARK_USER_ADDR / SPARK_BUILDER_ADDR`（沿 Phase 1 既有 Keychain 帳戶即可）。提供後補跑 Task 5 實測與 Task 17。
3. **hl-copytrader 線上 log 路徑**（本機或遠端？systemd journal 或檔案？）——Task 16 differ 對真實 log 校準用。
4. Shadow 3 交易日的排程方式（本機常駐 or 手動每日跑）。
5. Dogfood 錢包與注資時點（W3.5，不急）。
