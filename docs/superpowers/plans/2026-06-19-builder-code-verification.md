# Builder Code 金流驗證 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Hyperliquid testnet 端到端證明「主錢包授權 builder fee + 授權 agent + agent 下單夾帶 builder code → builder fee 實累計入我的 builder 地址且可程式驗證」，再於 mainnet 極小額複驗一次。

**Architecture:** 所有業務模組（onboarding / orchestrator / verification）只依賴 `ExchangeAdapter` 介面與 `keystore` 介面，不直接碰 SDK；測試以 `FakeAdapter` 離線執行，真 SDK 只在標記跳過的 integration 測試與手動 mainnet 複驗時連網。狀態判定全靠 API 查詢以保持冪等可重跑。驗證分兩段：即時 `query_builder_accrued > 0`（主成功）+ 可獨立重跑的 builder_fills CSV 對帳。

**Tech Stack:** Python 3.11、uv（venv+deps）、pytest、ruff、`hyperliquid-python-sdk`、`eth-account`、`keyring`（macOS Keychain）、`lz4`、`Decimal`。

---

## 設計來源

Spec：[docs/superpowers/specs/2026-06-19-builder-code-verification-design.md](../specs/2026-06-19-builder-code-verification-design.md)

關鍵決定：D1 可成交 `Ioc` 限價單；D2 兩段式驗證；D3 uv/pytest/ruff/py3.11；D4 可抽換 keystore + MacKeychain；D5 testnet 主錢包 key 只活在 test harness；D6 `f=20`（2bp）、`maxRate="0.1%"`。

## 檔案結構（責任邊界）

| 檔案 | 責任 |
|---|---|
| `pyproject.toml` | uv 專案、deps、ruff/pytest 設定 |
| `src/spark/config.py` | `Settings` dataclass + 載入（builder 地址、f、max_rate、network、endpoint、coin、order_size、account_id） |
| `src/spark/money.py` | `Decimal` 工具 + 費率換算（f ↔ %）、費率上限驗證 |
| `src/spark/exchange/base.py` | 核心型別（`Signer`/`BuilderCode`/`Order`/`Fill`/`TxResult`/`OrderResult`）+ `ExchangeAdapter` ABC |
| `src/spark/exchange/fakes.py` | `FakeAdapter`（離線測試用，可編程回應與記錄呼叫） |
| `src/spark/exchange/csv_fills.py` | builder_fills LZ4 CSV 解析（header-driven）→ `list[Fill]` |
| `src/spark/exchange/hyperliquid.py` | `HyperliquidAdapter`：包 SDK（單元測試 mock SDK client） |
| `src/spark/keystore/base.py` | `KeyStore` 介面 `get_main_signer/get_agent_signer(account_id)` |
| `src/spark/keystore/keychain.py` | `MacKeychainBackend`（keyring） |
| `src/spark/onboarding.py` | 狀態機：FUNDED→BUILDER_APPROVED→AGENT_AUTHORIZED→READY（用 main_signer，test harness） |
| `src/spark/orchestrator.py` | `place_marketable_order`（用 agent_signer，夾帶 builder） |
| `src/spark/verification/accrued.py` | 即時：輪詢 `query_builder_accrued > 0` |
| `src/spark/verification/reconcile.py` | CSV 對帳（可獨立重跑）+ 對帳報告 |
| `scripts/bootstrap_keys.py` | 互動式把 main/agent 私鑰匯入 Keychain（不 echo、不 log） |
| `scripts/run_testnet_flow.py` | 串起 onboarding→orchestrator→accrued 的 CLI 入口 |
| `tests/...` | 各模組單元測試 + `@pytest.mark.integration` testnet 測試 |

---

## Task 0: 研究 spike — 釘住 §11 外部假設（不寫產品碼）

**Files:**
- Create: `docs/superpowers/research/2026-06-19-hl-sdk-findings.md`

> 目的：在寫 `HyperliquidAdapter` 與 CSV parser 前，用真實 SDK / testnet 確認簽名與格式，避免整份 plan 建在猜測上。此 task 不產生產品碼，只產生一份 findings 文件。

- [ ] **Step 1: 安裝 SDK 到臨時環境並記錄 API**

Run:
```bash
uv run --with hyperliquid-python-sdk --with eth-account python -c "
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
print('Exchange.approve_builder_fee:', Exchange.approve_builder_fee.__doc__, Exchange.approve_builder_fee.__code__.co_varnames[:Exchange.approve_builder_fee.__code__.co_argcount])
print('Exchange.approve_agent:', Exchange.approve_agent.__code__.co_varnames[:Exchange.approve_agent.__code__.co_argcount])
print('Exchange.order:', Exchange.order.__code__.co_varnames[:Exchange.order.__code__.co_argcount])
print('Info methods:', [m for m in dir(Info) if not m.startswith('_')])
"
```
Expected: 印出三個 write 方法的參數名與 `Info` 的方法清單。把結果貼進 findings 文件。

- [ ] **Step 2: 確認 max builder fee 查詢方式**

在 findings 記錄：`Info` 是否有 `max_builder_fee(user, builder)`；若無，記錄要 POST `{"type":"maxBuilderFee","user":..,"builder":..}` 到 info endpoint 的事實。同時記錄累計 builder fee 從哪取得（referral / builder state，POST `{"type":"referral"...}` 或對應 endpoint）。

- [ ] **Step 3: 抓一份真實 builder_fills CSV 確認表頭與 testnet 可用性**

Run（builder 地址用任一已知有量的 mainnet builder；testnet 換 base url）:
```bash
uv run --with lz4 python -c "
import urllib.request, lz4.frame, sys
base='https://stats-data.hyperliquid.xyz/Mainnet/builder_fills'
# 用一個已知 builder 與近日日期測試；testnet 改 .../Testnet/...
url=f'{base}/<KNOWN_BUILDER_ADDR>/20260618.csv.lz4'
try:
    raw=urllib.request.urlopen(url, timeout=20).read()
    print('first line:', lz4.frame.decompress(raw).splitlines()[0])
except Exception as e:
    print('FAILED', e)
"
```
記錄：① CSV header 真實欄位名（決定 `Fill` 對應）② testnet 路徑是否回 200（若 404 → testnet 對帳僅在 mainnet 複驗做，testnet 只認即時累計，更新 spec §8/§11）。

- [ ] **Step 4: Commit findings**

```bash
git add docs/superpowers/research/2026-06-19-hl-sdk-findings.md
git commit -m "docs: HL SDK + builder_fills CSV research findings"
```

> 後續 Task 8 / 11 / 12 的 SDK 呼叫與 CSV 欄位對應，以本 findings 為準微調。

---

## Task 1: 專案 scaffold

**Files:**
- Create: `pyproject.toml`, `src/spark/__init__.py`, `tests/__init__.py`, `.gitignore`

- [ ] **Step 1: 建立 pyproject.toml**

```toml
[project]
name = "spark"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "hyperliquid-python-sdk",
    "eth-account",
    "keyring",
    "lz4",
]

[dependency-groups]
dev = ["pytest", "ruff"]

[tool.pytest.ini_options]
markers = ["integration: 需連 testnet/mainnet，預設跳過"]
addopts = "-m 'not integration'"
testpaths = ["tests"]

[tool.ruff]
target-version = "py311"
line-length = 100

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/spark"]
```

- [ ] **Step 2: 建立套件骨架與 .gitignore**

```bash
mkdir -p src/spark tests
touch src/spark/__init__.py tests/__init__.py
printf '.venv/\n__pycache__/\n*.pyc\n.pytest_cache/\nuv.lock\n' > .gitignore
```

- [ ] **Step 3: 同步環境並確認可跑**

Run: `uv sync && uv run pytest`
Expected: pytest 啟動、收集到 0 個測試、exit 0（"no tests ran"）。

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml src tests .gitignore
git commit -m "chore: scaffold uv + pytest + ruff project"
```

---

## Task 2: money 工具 — 費率換算與上限驗證

**Files:**
- Create: `src/spark/money.py`
- Test: `tests/test_money.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_money.py
from decimal import Decimal
import pytest
from spark.money import f_to_percent_str, assert_fee_within_cap, FEE_CAP_TENTHS_BP


def test_f_to_percent_str_basic():
    assert f_to_percent_str(20) == "0.02%"      # 2 bp
    assert f_to_percent_str(100) == "0.1%"      # 協議上限 0.1%
    assert f_to_percent_str(10) == "0.01%"      # 1 bp


def test_fee_cap_constant_is_protocol_limit():
    assert FEE_CAP_TENTHS_BP == 100


def test_assert_fee_within_cap_passes_at_and_below_cap():
    assert_fee_within_cap(20)
    assert_fee_within_cap(100)


def test_assert_fee_within_cap_rejects_above_cap():
    with pytest.raises(ValueError):
        assert_fee_within_cap(101)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_money.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'spark.money'`

- [ ] **Step 3: 實作**

```python
# src/spark/money.py
"""費率換算與金額工具。f 單位為「十分之一個 bp」：f=10 → 1bp → 0.01%。"""
from decimal import Decimal

FEE_CAP_TENTHS_BP = 100  # 協議上限 0.1%


def f_to_percent_str(f: int) -> str:
    """把 builder fee f（十分之一 bp）轉成 ApproveBuilderFee 用的百分比字串。f/1000 (%)。"""
    pct = Decimal(f) / Decimal(1000)
    return f"{pct.normalize()}%"


def assert_fee_within_cap(f: int) -> None:
    if not (0 < f <= FEE_CAP_TENTHS_BP):
        raise ValueError(f"builder fee f={f} 超出協議上限 {FEE_CAP_TENTHS_BP}（0.1%）")
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_money.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/spark/money.py tests/test_money.py
git commit -m "feat: fee rate conversion and protocol cap validation"
```

---

## Task 3: 核心型別 + ExchangeAdapter ABC

**Files:**
- Create: `src/spark/exchange/__init__.py`, `src/spark/exchange/base.py`
- Test: `tests/test_base_types.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_base_types.py
from decimal import Decimal
from datetime import datetime
import pytest
from spark.exchange.base import BuilderCode, Order, Fill, ExchangeAdapter


def test_builder_code_is_frozen():
    bc = BuilderCode(b="0xabc", f=20)
    with pytest.raises(Exception):
        bc.f = 30  # frozen dataclass


def test_order_holds_decimal_fields():
    o = Order(coin="ETH", is_buy=True, size=Decimal("0.01"), limit_px=Decimal("4000"), tif="Ioc")
    assert o.tif == "Ioc"
    assert isinstance(o.size, Decimal)


def test_fill_holds_builder_fee():
    f = Fill(time=datetime(2026, 6, 18), coin="ETH", px=Decimal("4000"),
             sz=Decimal("0.01"), side="B", builder_fee=Decimal("0.008"))
    assert f.builder_fee == Decimal("0.008")


def test_adapter_is_abstract_and_has_no_withdraw():
    # 非託管不變量：介面刻意不存在喚款方法
    assert not hasattr(ExchangeAdapter, "withdraw")
    assert not hasattr(ExchangeAdapter, "transfer")
    with pytest.raises(TypeError):
        ExchangeAdapter()  # ABC 不可實例化
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_base_types.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'spark.exchange'`

- [ ] **Step 3: 實作**

```python
# src/spark/exchange/__init__.py
```
```python
# src/spark/exchange/base.py
"""核心型別與 ExchangeAdapter 抽象介面。刻意不含任何 withdraw/transfer（非託管不變量）。"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

# Signer：keystore 回傳、可被 adapter 拿去簽 EIP-712 的物件（Phase 1 為 eth_account LocalAccount）。
Signer = Any


@dataclass(frozen=True)
class BuilderCode:
    b: str   # builder 地址
    f: int   # 十分之一 bp（Phase 1 = 20）


@dataclass(frozen=True)
class Order:
    coin: str
    is_buy: bool
    size: Decimal
    limit_px: Decimal
    tif: str  # "Ioc"


@dataclass(frozen=True)
class Fill:
    time: datetime
    coin: str
    px: Decimal
    sz: Decimal
    side: str
    builder_fee: Decimal


@dataclass(frozen=True)
class TxResult:
    ok: bool
    raw: dict


@dataclass(frozen=True)
class OrderResult:
    ok: bool
    filled_size: Decimal
    avg_px: Decimal
    raw: dict


class ExchangeAdapter(ABC):
    # --- reads ---
    @abstractmethod
    def get_account_value(self, address: str) -> Decimal: ...
    @abstractmethod
    def query_max_builder_fee(self, user: str, builder: str) -> int: ...
    @abstractmethod
    def query_builder_accrued(self, builder: str) -> Decimal: ...
    @abstractmethod
    def fetch_builder_fills(self, builder: str, day: date) -> list[Fill]: ...

    # --- writes（approve_* 概念上屬主錢包；Phase 1 testnet 由 test harness 簽）---
    @abstractmethod
    def approve_builder_fee(self, main_signer: Signer, builder: str, max_rate: str) -> TxResult: ...
    @abstractmethod
    def approve_agent(self, main_signer: Signer, agent_address: str) -> TxResult: ...
    @abstractmethod
    def place_order(self, agent_signer: Signer, order: Order, builder: BuilderCode) -> OrderResult: ...
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_base_types.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/spark/exchange/__init__.py src/spark/exchange/base.py tests/test_base_types.py
git commit -m "feat: core exchange types and ExchangeAdapter ABC (no withdraw/transfer)"
```

---

## Task 4: FakeAdapter（離線測試用）

**Files:**
- Create: `src/spark/exchange/fakes.py`
- Test: `tests/test_fake_adapter.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_fake_adapter.py
from decimal import Decimal
from datetime import date, datetime
from spark.exchange.base import Order, BuilderCode, Fill
from spark.exchange.fakes import FakeAdapter


def test_fake_records_approve_and_flips_max_fee():
    fake = FakeAdapter(account_value=Decimal("150"))
    assert fake.query_max_builder_fee("0xuser", "0xbuilder") == 0
    fake.approve_builder_fee(main_signer="MAIN", builder="0xbuilder", max_rate="0.1%")
    assert fake.query_max_builder_fee("0xuser", "0xbuilder") == 100
    assert fake.calls["approve_builder_fee"][0]["main_signer"] == "MAIN"


def test_fake_place_order_accrues_builder_fee():
    fake = FakeAdapter(account_value=Decimal("150"))
    res = fake.place_order("AGENT", Order("ETH", True, Decimal("0.01"), Decimal("4000"), "Ioc"),
                           BuilderCode(b="0xbuilder", f=20))
    assert res.ok and res.filled_size == Decimal("0.01")
    assert fake.query_builder_accrued("0xbuilder") > 0


def test_fake_fetch_fills_returns_seeded():
    fill = Fill(datetime(2026, 6, 18), "ETH", Decimal("4000"), Decimal("0.01"), "B", Decimal("0.008"))
    fake = FakeAdapter(account_value=Decimal("150"), seeded_fills=[fill])
    assert fake.fetch_builder_fills("0xbuilder", date(2026, 6, 18)) == [fill]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_fake_adapter.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'spark.exchange.fakes'`

- [ ] **Step 3: 實作**

```python
# src/spark/exchange/fakes.py
"""可編程的離線假交易所，記錄所有呼叫供斷言。"""
from collections import defaultdict
from datetime import date
from decimal import Decimal
from spark.exchange.base import (
    ExchangeAdapter, Order, BuilderCode, Fill, TxResult, OrderResult,
)


class FakeAdapter(ExchangeAdapter):
    def __init__(self, account_value=Decimal("0"), seeded_fills=None):
        self._account_value = Decimal(account_value)
        self._max_fee = 0
        self._accrued = Decimal("0")
        self._seeded_fills = list(seeded_fills or [])
        self.calls = defaultdict(list)

    def get_account_value(self, address: str) -> Decimal:
        return self._account_value

    def query_max_builder_fee(self, user: str, builder: str) -> int:
        return self._max_fee

    def query_builder_accrued(self, builder: str) -> Decimal:
        return self._accrued

    def fetch_builder_fills(self, builder: str, day: date) -> list[Fill]:
        return list(self._seeded_fills)

    def approve_builder_fee(self, main_signer, builder, max_rate) -> TxResult:
        self.calls["approve_builder_fee"].append(
            {"main_signer": main_signer, "builder": builder, "max_rate": max_rate})
        self._max_fee = 100  # 模擬 "0.1%" 授權
        return TxResult(ok=True, raw={"status": "ok"})

    def approve_agent(self, main_signer, agent_address) -> TxResult:
        self.calls["approve_agent"].append(
            {"main_signer": main_signer, "agent_address": agent_address})
        return TxResult(ok=True, raw={"status": "ok"})

    def place_order(self, agent_signer, order: Order, builder: BuilderCode) -> OrderResult:
        self.calls["place_order"].append(
            {"agent_signer": agent_signer, "order": order, "builder": builder})
        notional = order.size * order.limit_px
        self._accrued += notional * Decimal(builder.f) / Decimal(100000)  # f/1000 % = f/100000
        return OrderResult(ok=True, filled_size=order.size, avg_px=order.limit_px,
                           raw={"status": "filled"})
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_fake_adapter.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/spark/exchange/fakes.py tests/test_fake_adapter.py
git commit -m "feat: FakeAdapter for offline tests"
```

---

## Task 5: config — Settings

**Files:**
- Create: `src/spark/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_config.py
from decimal import Decimal
import pytest
from spark.config import Settings, API_URLS, CSV_BASE_URLS


def test_settings_defaults_phase1():
    s = Settings(builder_address="0xbuilder", account_id="testacct", network="testnet")
    assert s.f == 20
    assert s.max_rate == "0.1%"
    assert s.coin == "ETH"
    assert isinstance(s.order_size, Decimal)


def test_network_switches_urls():
    assert API_URLS["testnet"] != API_URLS["mainnet"]
    assert "Testnet" in CSV_BASE_URLS["testnet"]
    assert "Mainnet" in CSV_BASE_URLS["mainnet"]


def test_rejects_unknown_network():
    with pytest.raises(ValueError):
        Settings(builder_address="0xb", account_id="a", network="devnet")


def test_fee_validated_against_cap():
    with pytest.raises(ValueError):
        Settings(builder_address="0xb", account_id="a", network="testnet", f=200)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'spark.config'`

- [ ] **Step 3: 實作**

```python
# src/spark/config.py
from dataclasses import dataclass, field
from decimal import Decimal
from spark.money import assert_fee_within_cap

API_URLS = {
    "testnet": "https://api.hyperliquid-testnet.xyz",
    "mainnet": "https://api.hyperliquid.xyz",
}
CSV_BASE_URLS = {
    "testnet": "https://stats-data.hyperliquid.xyz/Testnet/builder_fills",
    "mainnet": "https://stats-data.hyperliquid.xyz/Mainnet/builder_fills",
}
MIN_BUILDER_BALANCE = Decimal("100")  # builder 啟用門檻 USDC


@dataclass(frozen=True)
class Settings:
    builder_address: str
    account_id: str
    network: str
    f: int = 20
    max_rate: str = "0.1%"
    coin: str = "ETH"
    order_size: Decimal = field(default_factory=lambda: Decimal("0.01"))

    def __post_init__(self):
        if self.network not in API_URLS:
            raise ValueError(f"unknown network: {self.network}")
        assert_fee_within_cap(self.f)

    @property
    def api_url(self) -> str:
        return API_URLS[self.network]

    @property
    def csv_base_url(self) -> str:
        return CSV_BASE_URLS[self.network]
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_config.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/spark/config.py tests/test_config.py
git commit -m "feat: Settings config with network switch and fee validation"
```

---

## Task 6: keystore 介面 + MacKeychainBackend

**Files:**
- Create: `src/spark/keystore/__init__.py`, `src/spark/keystore/base.py`, `src/spark/keystore/keychain.py`
- Test: `tests/test_keychain.py`

- [ ] **Step 1: 寫失敗測試（mock keyring，不碰真 Keychain）**

```python
# tests/test_keychain.py
import pytest
from spark.keystore.keychain import MacKeychainBackend

SERVICE = "spark-test"
# 一個合法的 testnet 私鑰（測試用固定值，非真資產）
PRIV = "0x4c0883a69102937d6231471b5dbb6204fe512961708279f1f6e3d2b7c1f0f2aa"


@pytest.fixture
def fake_keyring(monkeypatch):
    store = {}
    monkeypatch.setattr("spark.keystore.keychain.keyring.get_password",
                        lambda svc, key: store.get((svc, key)))
    monkeypatch.setattr("spark.keystore.keychain.keyring.set_password",
                        lambda svc, key, val: store.__setitem__((svc, key), val))
    return store


def test_get_agent_signer_loads_account_from_keychain(fake_keyring):
    ks = MacKeychainBackend(service=SERVICE)
    ks.import_key("acct1", "agent", PRIV)
    signer = ks.get_agent_signer("acct1")
    assert signer.address.lower().startswith("0x")


def test_main_and_agent_are_separate_roles(fake_keyring):
    ks = MacKeychainBackend(service=SERVICE)
    ks.import_key("acct1", "main", PRIV)
    with pytest.raises(KeyError):
        ks.get_agent_signer("acct1")  # 只匯入了 main，沒有 agent


def test_missing_key_raises(fake_keyring):
    ks = MacKeychainBackend(service=SERVICE)
    with pytest.raises(KeyError):
        ks.get_main_signer("nope")
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_keychain.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'spark.keystore'`

- [ ] **Step 3: 實作**

```python
# src/spark/keystore/__init__.py
```
```python
# src/spark/keystore/base.py
from abc import ABC, abstractmethod
from typing import Any


class KeyStore(ABC):
    @abstractmethod
    def get_main_signer(self, account_id: str) -> Any: ...
    @abstractmethod
    def get_agent_signer(self, account_id: str) -> Any: ...
```
```python
# src/spark/keystore/keychain.py
"""macOS Keychain 後端。私鑰只存 Keychain，不落 repo/log。"""
import keyring
from eth_account import Account
from spark.keystore.base import KeyStore


class MacKeychainBackend(KeyStore):
    def __init__(self, service: str = "spark"):
        self._service = service

    def _entry(self, account_id: str, role: str) -> str:
        return f"{account_id}:{role}"

    def import_key(self, account_id: str, role: str, private_key: str) -> None:
        """一次性匯入。role ∈ {'main','agent'}。"""
        if role not in ("main", "agent"):
            raise ValueError(f"role must be main/agent, got {role}")
        keyring.set_password(self._service, self._entry(account_id, role), private_key)

    def _load(self, account_id: str, role: str):
        pk = keyring.get_password(self._service, self._entry(account_id, role))
        if not pk:
            raise KeyError(f"no {role} key for account {account_id}")
        return Account.from_key(pk)

    def get_main_signer(self, account_id: str):
        return self._load(account_id, "main")

    def get_agent_signer(self, account_id: str):
        return self._load(account_id, "agent")
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_keychain.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/spark/keystore tests/test_keychain.py
git commit -m "feat: pluggable keystore interface + macOS Keychain backend"
```

---

## Task 7: CSV parser（header-driven，LZ4）

**Files:**
- Create: `src/spark/exchange/csv_fills.py`
- Test: `tests/test_csv_fills.py`, `tests/fixtures/builder_fills_sample.csv`

> **Task 0 findings 註記**：`stats-data` S3 對假地址一律回 403（S3 對「不存在 key 且無 ListBucket 權限」的標準行為，非 bucket 私有），所以真實 CSV 表頭**尚未確認**，留待 Task 14 用我們自己有成交的 builder 地址驗證。本 task 因此用 **alias map 容錯** 的 header-driven parser + fixture 黃金測試先把解析邏輯做對；真實表頭與假設不同時，Task 14 只需擴充 alias map。即時驗證主判定走 Task 10/12 的 `query_builder_accrued`（referral state），不依賴 CSV。

- [ ] **Step 1: 建立 fixture（明文 CSV；表頭以 findings 為準，先用合理欄位名）**

```bash
mkdir -p tests/fixtures
cat > tests/fixtures/builder_fills_sample.csv <<'CSV'
time,coin,side,px,sz,builderFee
2026-06-18T10:00:00,ETH,B,4000.5,0.01,0.008
2026-06-18T10:05:00,ETH,A,4001.0,0.02,0.016
CSV
```

- [ ] **Step 2: 寫失敗測試**

```python
# tests/test_csv_fills.py
from decimal import Decimal
from datetime import datetime
import lz4.frame
from pathlib import Path
from spark.exchange.csv_fills import parse_builder_fills

FIXTURE = Path(__file__).parent / "fixtures" / "builder_fills_sample.csv"


def test_parse_plain_csv_bytes():
    fills = parse_builder_fills(FIXTURE.read_bytes(), compressed=False)
    assert len(fills) == 2
    assert fills[0].coin == "ETH"
    assert fills[0].px == Decimal("4000.5")
    assert fills[0].sz == Decimal("0.01")
    assert fills[0].builder_fee == Decimal("0.008")
    assert fills[0].time == datetime.fromisoformat("2026-06-18T10:00:00")


def test_parse_lz4_roundtrip():
    raw = lz4.frame.compress(FIXTURE.read_bytes())
    fills = parse_builder_fills(raw, compressed=True)
    assert len(fills) == 2
    assert fills[1].sz == Decimal("0.02")


def test_total_builder_fee_helper():
    from spark.exchange.csv_fills import total_builder_fee
    fills = parse_builder_fills(FIXTURE.read_bytes(), compressed=False)
    assert total_builder_fee(fills) == Decimal("0.024")
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `uv run pytest tests/test_csv_fills.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'spark.exchange.csv_fills'`

- [ ] **Step 4: 實作**

```python
# src/spark/exchange/csv_fills.py
"""解析 builder_fills CSV（LZ4）。header-driven + alias map 容錯。"""
import csv
import io
from datetime import datetime
from decimal import Decimal
import lz4.frame
from spark.exchange.base import Fill

# 真實表頭以 Task 0 findings 為準；此 map 容納可能的命名差異。
ALIASES = {
    "time": ["time", "timestamp", "ts"],
    "coin": ["coin", "asset"],
    "side": ["side", "dir"],
    "px": ["px", "price"],
    "sz": ["sz", "size"],
    "builder_fee": ["builderFee", "builder_fee", "fee"],
}


def _pick(row: dict, names: list[str]) -> str:
    for n in names:
        if n in row and row[n] != "":
            return row[n]
    raise KeyError(f"none of {names} in CSV header {list(row)}")


def _parse_time(v: str) -> datetime:
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return datetime.fromtimestamp(int(v) / 1000)  # epoch ms 後備


def parse_builder_fills(data: bytes, compressed: bool = True) -> list[Fill]:
    text = lz4.frame.decompress(data).decode() if compressed else data.decode()
    reader = csv.DictReader(io.StringIO(text))
    out: list[Fill] = []
    for row in reader:
        out.append(Fill(
            time=_parse_time(_pick(row, ALIASES["time"])),
            coin=_pick(row, ALIASES["coin"]),
            side=_pick(row, ALIASES["side"]),
            px=Decimal(_pick(row, ALIASES["px"])),
            sz=Decimal(_pick(row, ALIASES["sz"])),
            builder_fee=Decimal(_pick(row, ALIASES["builder_fee"])),
        ))
    return out


def total_builder_fee(fills: list[Fill]) -> Decimal:
    return sum((f.builder_fee for f in fills), Decimal("0"))
```

- [ ] **Step 5: 跑測試確認通過**

Run: `uv run pytest tests/test_csv_fills.py -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add src/spark/exchange/csv_fills.py tests/test_csv_fills.py tests/fixtures/builder_fills_sample.csv
git commit -m "feat: header-driven builder_fills LZ4 CSV parser"
```

---

## Task 8: onboarding 狀態機

**Files:**
- Create: `src/spark/onboarding.py`
- Test: `tests/test_onboarding.py`

> main_signer 由 keystore 取得；此模組屬 test harness 用途，是唯一觸碰 main 簽章器的業務碼。

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_onboarding.py
from decimal import Decimal
import pytest
from spark.exchange.fakes import FakeAdapter
from spark.config import Settings
from spark.onboarding import onboard, OnboardingState, InsufficientFunds


def _settings():
    return Settings(builder_address="0xbuilder", account_id="acct1", network="testnet")


def test_onboard_reaches_ready_when_funded():
    fake = FakeAdapter(account_value=Decimal("150"))
    result = onboard(fake, _settings(), main_signer="MAIN", agent_address="0xagent",
                     user_address="0xuser")
    assert result.state == OnboardingState.READY
    assert fake.calls["approve_builder_fee"][0]["max_rate"] == "0.1%"
    assert fake.calls["approve_agent"][0]["agent_address"] == "0xagent"


def test_onboard_rejects_below_min_balance():
    fake = FakeAdapter(account_value=Decimal("50"))  # < 100 門檻
    with pytest.raises(InsufficientFunds):
        onboard(fake, _settings(), main_signer="MAIN", agent_address="0xagent",
                user_address="0xuser")


def test_onboard_idempotent_when_already_approved():
    fake = FakeAdapter(account_value=Decimal("150"))
    onboard(fake, _settings(), "MAIN", "0xagent", "0xuser")
    onboard(fake, _settings(), "MAIN", "0xagent", "0xuser")  # 再跑一次
    # 已授權則不重複送 approve_builder_fee
    assert len(fake.calls["approve_builder_fee"]) == 1
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_onboarding.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'spark.onboarding'`

- [ ] **Step 3: 實作**

```python
# src/spark/onboarding.py
"""onboarding 狀態機：FUNDED→BUILDER_APPROVED→AGENT_AUTHORIZED→READY。
狀態靠 API 查詢判定 → 冪等可重跑。只有此模組使用 main_signer（test harness）。"""
from dataclasses import dataclass
from enum import Enum
from spark.config import Settings, MIN_BUILDER_BALANCE
from spark.exchange.base import ExchangeAdapter, Signer


class OnboardingState(str, Enum):
    UNFUNDED = "UNFUNDED"
    BUILDER_APPROVED = "BUILDER_APPROVED"
    AGENT_AUTHORIZED = "AGENT_AUTHORIZED"
    READY = "READY"


class InsufficientFunds(Exception):
    pass


@dataclass
class OnboardingResult:
    state: OnboardingState


def onboard(adapter: ExchangeAdapter, settings: Settings, main_signer: Signer,
            agent_address: str, user_address: str) -> OnboardingResult:
    # FUNDED gate（builder 啟用門檻 ≥ 100 USDC）
    if adapter.get_account_value(user_address) < MIN_BUILDER_BALANCE:
        raise InsufficientFunds(
            f"account value < {MIN_BUILDER_BALANCE} USDC builder 門檻")

    # ApproveBuilderFee（冪等：已授權則跳過）
    if adapter.query_max_builder_fee(user_address, settings.builder_address) == 0:
        adapter.approve_builder_fee(main_signer, settings.builder_address, settings.max_rate)
        if adapter.query_max_builder_fee(user_address, settings.builder_address) == 0:
            raise RuntimeError("approve_builder_fee 後 maxBuilderFee 仍為 0")

    # ApproveAgent（每次重跑都送；HL approve 同 agent 為冪等動作）
    adapter.approve_agent(main_signer, agent_address)
    return OnboardingResult(state=OnboardingState.READY)
```

> 注意：`test_onboard_idempotent_when_already_approved` 斷言 `approve_builder_fee` 只送一次——靠上面的 `query_max_builder_fee == 0` 條件達成（FakeAdapter 第一次 approve 後回 100）。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_onboarding.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/spark/onboarding.py tests/test_onboarding.py
git commit -m "feat: idempotent onboarding state machine"
```

---

## Task 9: orchestrator — 下可成交限價單

**Files:**
- Create: `src/spark/orchestrator.py`
- Test: `tests/test_orchestrator.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_orchestrator.py
from decimal import Decimal
import pytest
from spark.exchange.fakes import FakeAdapter
from spark.config import Settings
from spark.orchestrator import place_marketable_order


def _settings():
    return Settings(builder_address="0xbuilder", account_id="acct1", network="testnet")


def test_places_ioc_order_with_builder_code():
    fake = FakeAdapter(account_value=Decimal("150"))
    res = place_marketable_order(fake, _settings(), agent_signer="AGENT",
                                 is_buy=True, best_opposite_px=Decimal("4000"))
    assert res.ok
    call = fake.calls["place_order"][0]
    assert call["order"].tif == "Ioc"
    assert call["builder"].b == "0xbuilder"
    assert call["builder"].f == 20


def test_buy_price_crosses_above_opposite():
    fake = FakeAdapter(account_value=Decimal("150"))
    place_marketable_order(fake, _settings(), agent_signer="AGENT",
                           is_buy=True, best_opposite_px=Decimal("4000"))
    order = fake.calls["place_order"][0]["order"]
    assert order.limit_px > Decimal("4000")  # 買單抬高以穿過賣一


def test_sell_price_crosses_below_opposite():
    fake = FakeAdapter(account_value=Decimal("150"))
    place_marketable_order(fake, _settings(), agent_signer="AGENT",
                           is_buy=False, best_opposite_px=Decimal("4000"))
    order = fake.calls["place_order"][0]["order"]
    assert order.limit_px < Decimal("4000")  # 賣單壓低以穿過買一
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_orchestrator.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'spark.orchestrator'`

- [ ] **Step 3: 實作**

```python
# src/spark/orchestrator.py
"""Phase 1 orchestrator：用 agent_signer 下可成交（Ioc）限價單，夾帶 builder code。
唯一觸碰 agent_signer 的業務碼；不碰 main key，不含 cancel/rebalance。"""
from decimal import Decimal
from spark.config import Settings
from spark.exchange.base import ExchangeAdapter, Order, BuilderCode, OrderResult, Signer

CROSS_BPS = Decimal("0.001")  # 穿價 buffer：0.1% 確保吃到對手盤


def place_marketable_order(adapter: ExchangeAdapter, settings: Settings, agent_signer: Signer,
                           is_buy: bool, best_opposite_px: Decimal) -> OrderResult:
    if is_buy:
        limit_px = best_opposite_px * (Decimal("1") + CROSS_BPS)
    else:
        limit_px = best_opposite_px * (Decimal("1") - CROSS_BPS)
    order = Order(coin=settings.coin, is_buy=is_buy, size=settings.order_size,
                  limit_px=limit_px, tif="Ioc")
    builder = BuilderCode(b=settings.builder_address, f=settings.f)
    return adapter.place_order(agent_signer, order, builder)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_orchestrator.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/spark/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: orchestrator placing marketable Ioc order with builder code"
```

---

## Task 10: verification/accrued — 即時輪詢

**Files:**
- Create: `src/spark/verification/__init__.py`, `src/spark/verification/accrued.py`
- Test: `tests/test_accrued.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_accrued.py
from decimal import Decimal
import pytest
from spark.exchange.fakes import FakeAdapter
from spark.verification.accrued import wait_for_accrual, AccrualTimeout


def test_returns_accrued_when_positive():
    fake = FakeAdapter(account_value=Decimal("150"))
    fake._accrued = Decimal("0.008")  # 模擬已累計
    got = wait_for_accrual(fake, "0xbuilder", attempts=3, sleep_s=0)
    assert got == Decimal("0.008")


def test_polls_until_positive():
    fake = FakeAdapter(account_value=Decimal("150"))
    seq = [Decimal("0"), Decimal("0"), Decimal("0.005")]
    fake.query_builder_accrued = lambda builder: seq.pop(0)
    got = wait_for_accrual(fake, "0xbuilder", attempts=5, sleep_s=0)
    assert got == Decimal("0.005")


def test_times_out_when_never_positive():
    fake = FakeAdapter(account_value=Decimal("150"))  # 一直回 0
    with pytest.raises(AccrualTimeout):
        wait_for_accrual(fake, "0xbuilder", attempts=3, sleep_s=0)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_accrued.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'spark.verification'`

- [ ] **Step 3: 實作**

```python
# src/spark/verification/__init__.py
```
```python
# src/spark/verification/accrued.py
"""即時驗證：輪詢 query_builder_accrued 直到 > 0（Phase 1 主成功判定）。"""
import time
from decimal import Decimal
from spark.exchange.base import ExchangeAdapter


class AccrualTimeout(Exception):
    pass


def wait_for_accrual(adapter: ExchangeAdapter, builder: str,
                     attempts: int = 10, sleep_s: float = 3.0) -> Decimal:
    last = Decimal("0")
    for _ in range(attempts):
        last = adapter.query_builder_accrued(builder)
        if last > 0:
            return last
        if sleep_s:
            time.sleep(sleep_s)
    raise AccrualTimeout(f"builder {builder} accrued 仍為 {last}，輪詢 {attempts} 次後逾時")
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_accrued.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/spark/verification tests/test_accrued.py
git commit -m "feat: realtime builder-fee accrual polling"
```

---

## Task 11: verification/reconcile — CSV 對帳 + 報告

**Files:**
- Create: `src/spark/verification/reconcile.py`
- Test: `tests/test_reconcile.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_reconcile.py
from decimal import Decimal
from datetime import date, datetime
from spark.exchange.fakes import FakeAdapter
from spark.exchange.base import Fill
from spark.verification.reconcile import reconcile, ReconcileReport


def _fill():
    return Fill(datetime(2026, 6, 18), "ETH", Decimal("4000"), Decimal("0.01"), "B",
                Decimal("0.008"))


def test_reconcile_found_match():
    fake = FakeAdapter(account_value=Decimal("150"), seeded_fills=[_fill()])
    report = reconcile(fake, "0xbuilder", day=date(2026, 6, 18), expected_coin="ETH")
    assert report.matched is True
    assert report.fill_count == 1
    assert report.total_builder_fee == Decimal("0.008")


def test_reconcile_no_fills_means_unmatched():
    fake = FakeAdapter(account_value=Decimal("150"), seeded_fills=[])
    report = reconcile(fake, "0xbuilder", day=date(2026, 6, 18), expected_coin="ETH")
    assert report.matched is False
    assert report.fill_count == 0


def test_report_renders_text():
    fake = FakeAdapter(account_value=Decimal("150"), seeded_fills=[_fill()])
    report = reconcile(fake, "0xbuilder", day=date(2026, 6, 18), expected_coin="ETH")
    text = report.render()
    assert "ETH" in text and "0.008" in text
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_reconcile.py -v`
Expected: FAIL，`ImportError: cannot import name 'reconcile'`

- [ ] **Step 3: 實作**

```python
# src/spark/verification/reconcile.py
"""CSV 對帳（可獨立重跑）：解 builder_fills 找對應成交，輸出小型對帳報告。"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from spark.exchange.base import ExchangeAdapter, Fill
from spark.exchange.csv_fills import total_builder_fee


@dataclass
class ReconcileReport:
    builder: str
    day: date
    matched: bool
    fill_count: int
    total_builder_fee: Decimal
    fills: list[Fill]

    def render(self) -> str:
        lines = [
            f"== Builder 對帳報告 {self.day} ==",
            f"builder: {self.builder}",
            f"matched: {self.matched}  fills: {self.fill_count}",
            f"total_builder_fee: {self.total_builder_fee}",
        ]
        for f in self.fills:
            lines.append(f"  {f.time} {f.coin} {f.side} px={f.px} sz={f.sz} "
                         f"builder_fee={f.builder_fee}")
        return "\n".join(lines)


def reconcile(adapter: ExchangeAdapter, builder: str, day: date,
              expected_coin: str | None = None) -> ReconcileReport:
    fills = adapter.fetch_builder_fills(builder, day)
    if expected_coin is not None:
        fills = [f for f in fills if f.coin == expected_coin]
    return ReconcileReport(
        builder=builder, day=day, matched=len(fills) > 0, fill_count=len(fills),
        total_builder_fee=total_builder_fee(fills), fills=fills,
    )
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_reconcile.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/spark/verification/reconcile.py tests/test_reconcile.py
git commit -m "feat: CSV reconciliation with mini settlement report"
```

---

## Task 12: HyperliquidAdapter — 包 SDK（單元測試 mock client）

**Files:**
- Create: `src/spark/exchange/hyperliquid.py`
- Test: `tests/test_hyperliquid_adapter.py`

> SDK 確切方法名/參數以 Task 0 findings 為準；若 findings 與下方不同，依 findings 調整呼叫並同步調整測試的 mock。`get_account_value` / `query_*` 走 `Info`，`approve_*` / `place_order` 走 `Exchange`。CSV 透過 `csv_fills.parse_builder_fills` + HTTP 下載。

- [ ] **Step 1: 寫失敗測試（注入假 Info/Exchange/下載器）**

```python
# tests/test_hyperliquid_adapter.py
from decimal import Decimal
from datetime import date
from spark.exchange.base import Order, BuilderCode
from spark.exchange.hyperliquid import HyperliquidAdapter


class FakeInfo:
    def __init__(self):
        self.posts = []
    def user_state(self, address):
        return {"marginSummary": {"accountValue": "150.5"}}
    def post(self, url_path, payload=None):
        # Task 0 findings: 無 max_builder_fee wrapper，需 raw post {"type":"maxBuilderFee",...}
        self.posts.append((url_path, payload))
        assert payload["type"] == "maxBuilderFee"
        return 100
    def query_referral_state(self, address):
        # Task 0 findings: 累計 builder fee 來自 referral state 的 builderRewards
        return {"builderRewards": "0.008"}


class FakeExchange:
    def __init__(self):
        self.calls = []
    def approve_builder_fee(self, builder, max_fee_rate):
        self.calls.append(("approve_builder_fee", builder, max_fee_rate))
        return {"status": "ok"}
    def approve_agent(self, name=None):
        self.calls.append(("approve_agent", name))
        return ({"status": "ok"}, "0xagentkey")
    def order(self, coin, is_buy, sz, limit_px, order_type, reduce_only=False, builder=None):
        self.calls.append(("order", coin, is_buy, sz, limit_px, order_type, builder))
        return {"status": "ok", "response": {"data": {"statuses": [
            {"filled": {"totalSz": str(sz), "avgPx": str(limit_px)}}]}}}


def _adapter():
    return HyperliquidAdapter(network="testnet", info=FakeInfo(), exchange=FakeExchange())


def test_get_account_value_parses_margin_summary():
    assert _adapter().get_account_value("0xuser") == Decimal("150.5")


def test_query_max_builder_fee_via_raw_post():
    ad = _adapter()
    assert ad.query_max_builder_fee("0xuser", "0xbuilder") == 100
    url_path, payload = ad._info.posts[-1]
    assert url_path == "/info"
    assert payload == {"type": "maxBuilderFee", "user": "0xuser", "builder": "0xbuilder"}


def test_query_builder_accrued_from_referral_state():
    assert _adapter().query_builder_accrued("0xbuilder") == Decimal("0.008")


def test_place_order_passes_builder_dict_and_ioc():
    ad = _adapter()
    ad.place_order(agent_signer=None,
                   order=Order("ETH", True, Decimal("0.01"), Decimal("4000"), "Ioc"),
                   builder=BuilderCode(b="0xbuilder", f=20))
    name, coin, is_buy, sz, px, otype, builder = ad._exchange.calls[-1]
    assert otype == {"limit": {"tif": "Ioc"}}
    assert builder == {"b": "0xbuilder", "f": 20}
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_hyperliquid_adapter.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'spark.exchange.hyperliquid'`

- [ ] **Step 3: 實作**

```python
# src/spark/exchange/hyperliquid.py
"""hyperliquid-python-sdk 實作。Info/Exchange 可注入以便測試。
方法名以 Task 0 findings 為準；下方為基準實作。"""
import urllib.request
from datetime import date
from decimal import Decimal
from spark.config import API_URLS, CSV_BASE_URLS
from spark.exchange.base import (
    ExchangeAdapter, Order, BuilderCode, Fill, TxResult, OrderResult, Signer,
)
from spark.exchange.csv_fills import parse_builder_fills


class HyperliquidAdapter(ExchangeAdapter):
    def __init__(self, network: str, info=None, exchange=None):
        self._network = network
        self._info = info        # hyperliquid.info.Info
        self._exchange = exchange  # hyperliquid.exchange.Exchange（已綁 agent 錢包）

    # --- reads ---
    def get_account_value(self, address: str) -> Decimal:
        state = self._info.user_state(address)
        return Decimal(state["marginSummary"]["accountValue"])

    def query_max_builder_fee(self, user: str, builder: str) -> int:
        # Task 0 findings: SDK 無 wrapper，需 raw post（回傳 int，十分之一 bp）
        return int(self._info.post("/info", {"type": "maxBuilderFee",
                                             "user": user, "builder": builder}))

    def query_builder_accrued(self, builder: str) -> Decimal:
        # Task 0 findings: 累計 builder fee = referral state 的 builderRewards
        state = self._info.query_referral_state(builder)
        return Decimal(str(state["builderRewards"]))

    def fetch_builder_fills(self, builder: str, day: date) -> list[Fill]:
        url = f"{CSV_BASE_URLS[self._network]}/{builder}/{day:%Y%m%d}.csv.lz4"
        raw = urllib.request.urlopen(url, timeout=30).read()
        return parse_builder_fills(raw, compressed=True)

    # --- writes ---
    def approve_builder_fee(self, main_signer: Signer, builder: str, max_rate: str) -> TxResult:
        res = self._exchange.approve_builder_fee(builder, max_rate)
        return TxResult(ok=res.get("status") == "ok", raw=res)

    def approve_agent(self, main_signer: Signer, agent_address: str) -> TxResult:
        res, _agent_key = self._exchange.approve_agent()
        return TxResult(ok=res.get("status") == "ok", raw=res)

    def place_order(self, agent_signer: Signer, order: Order, builder: BuilderCode) -> OrderResult:
        res = self._exchange.order(
            order.coin, order.is_buy, float(order.size), float(order.limit_px),
            {"limit": {"tif": order.tif}}, reduce_only=False,
            builder={"b": builder.b, "f": builder.f},
        )
        status = res["response"]["data"]["statuses"][0].get("filled", {})
        return OrderResult(
            ok=bool(status),
            filled_size=Decimal(status.get("totalSz", "0")),
            avg_px=Decimal(status.get("avgPx", "0")),
            raw=res,
        )
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_hyperliquid_adapter.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/spark/exchange/hyperliquid.py tests/test_hyperliquid_adapter.py
git commit -m "feat: HyperliquidAdapter wrapping SDK (injectable for tests)"
```

---

## Task 13: bootstrap 腳本 — 匯入私鑰到 Keychain

**Files:**
- Create: `scripts/bootstrap_keys.py`
- Test: `tests/test_bootstrap_keys.py`

- [ ] **Step 1: 寫失敗測試（不印私鑰、寫進 backend）**

```python
# tests/test_bootstrap_keys.py
import builtins
from scripts.bootstrap_keys import import_key_interactive


class RecordingBackend:
    def __init__(self):
        self.saved = {}
    def import_key(self, account_id, role, private_key):
        self.saved[(account_id, role)] = private_key


def test_import_does_not_print_key(monkeypatch, capsys):
    backend = RecordingBackend()
    monkeypatch.setattr("scripts.bootstrap_keys.getpass.getpass",
                        lambda prompt="": "0xdeadbeef")
    import_key_interactive(backend, account_id="acct1", role="agent")
    out = capsys.readouterr().out
    assert "0xdeadbeef" not in out               # 絕不 echo 私鑰
    assert backend.saved[("acct1", "agent")] == "0xdeadbeef"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_bootstrap_keys.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'scripts.bootstrap_keys'`

- [ ] **Step 3: 實作**

```bash
mkdir -p scripts && touch scripts/__init__.py
```
```python
# scripts/bootstrap_keys.py
"""互動式把 main/agent 私鑰匯入 Keychain。私鑰用 getpass 讀取，絕不 echo、絕不 log。
用法: uv run python -m scripts.bootstrap_keys <account_id> <main|agent>"""
import getpass
import sys
from spark.keystore.keychain import MacKeychainBackend


def import_key_interactive(backend, account_id: str, role: str) -> None:
    pk = getpass.getpass(f"貼上 {account_id} 的 {role} 私鑰（不會顯示）: ")
    backend.import_key(account_id, role, pk.strip())
    print(f"已匯入 {account_id}:{role} 至 Keychain")  # 只印身分，不印 key


def main():
    if len(sys.argv) != 3 or sys.argv[2] not in ("main", "agent"):
        print("用法: python -m scripts.bootstrap_keys <account_id> <main|agent>")
        raise SystemExit(2)
    import_key_interactive(MacKeychainBackend(), sys.argv[1], sys.argv[2])


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_bootstrap_keys.py -v`
Expected: 1 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/__init__.py scripts/bootstrap_keys.py tests/test_bootstrap_keys.py
git commit -m "feat: interactive key bootstrap into Keychain (never echoes key)"
```

---

## Task 14: testnet 端到端整合測試（預設跳過）

**Files:**
- Create: `tests/integration/test_testnet_flow.py`

> 需真 testnet 帳號 + Keychain 已匯入 main/agent key。預設被 `-m 'not integration'` 跳過；顯式 `uv run pytest -m integration` 才跑。SDK 建構細節依 Task 0 findings 調整。

- [ ] **Step 1: 寫整合測試**

```python
# tests/integration/test_testnet_flow.py
import os
from datetime import date, timezone, datetime
from decimal import Decimal
import pytest

from spark.config import Settings
from spark.keystore.keychain import MacKeychainBackend
from spark.onboarding import onboard, OnboardingState
from spark.orchestrator import place_marketable_order
from spark.verification.accrued import wait_for_accrual

pytestmark = pytest.mark.integration

ACCOUNT_ID = os.environ.get("SPARK_ACCOUNT_ID", "testacct")
USER_ADDR = os.environ.get("SPARK_USER_ADDR")
AGENT_ADDR = os.environ.get("SPARK_AGENT_ADDR")
BUILDER_ADDR = os.environ.get("SPARK_BUILDER_ADDR")


def _build_adapter(settings, ks):
    # 依 Task 0 findings 構造 Info/Exchange（agent 錢包綁 Exchange）
    from hyperliquid.info import Info
    from hyperliquid.exchange import Exchange
    agent = ks.get_agent_signer(ACCOUNT_ID)
    main = ks.get_main_signer(ACCOUNT_ID)
    from spark.exchange.hyperliquid import HyperliquidAdapter
    info = Info(settings.api_url, skip_ws=True)
    exch_agent = Exchange(agent, settings.api_url)
    exch_main = Exchange(main, settings.api_url)
    return HyperliquidAdapter("testnet", info=info, exchange=exch_agent), exch_main, info


def test_end_to_end_testnet():
    assert all([USER_ADDR, AGENT_ADDR, BUILDER_ADDR]), "需設環境變數"
    settings = Settings(builder_address=BUILDER_ADDR, account_id=ACCOUNT_ID, network="testnet")
    ks = MacKeychainBackend()

    # onboarding 用 main 簽（test harness）；這裡示意用 main-bound Exchange 做 approve。
    main = ks.get_main_signer(ACCOUNT_ID)
    adapter, _exch_main, _info = _build_adapter(settings, ks)

    result = onboard(adapter, settings, main_signer=main,
                     agent_address=AGENT_ADDR, user_address=USER_ADDR)
    assert result.state == OnboardingState.READY

    # 取得對手盤價（findings 決定來源；此處用 mid 近似 + orchestrator 自帶穿價 buffer）
    best_px = Decimal(str(_info.all_mids()["ETH"]))
    order_res = place_marketable_order(adapter, settings, agent_signer=None,
                                       is_buy=True, best_opposite_px=best_px)
    assert order_res.ok and order_res.filled_size > 0

    accrued = wait_for_accrual(adapter, BUILDER_ADDR, attempts=10, sleep_s=3)
    assert accrued > 0
    print(f"✅ testnet 即時累計 builder fee = {accrued}")
```

- [ ] **Step 2: 確認預設被跳過**

Run: `uv run pytest tests/integration/test_testnet_flow.py -v`
Expected: `1 deselected`（被 `-m 'not integration'` 跳過）

- [ ] **Step 3: 手動執行 testnet（需先 bootstrap key 與設環境變數）**

Run:
```bash
SPARK_USER_ADDR=0x... SPARK_AGENT_ADDR=0x... SPARK_BUILDER_ADDR=0x... \
  uv run pytest -m integration tests/integration/test_testnet_flow.py -v -s
```
Expected: PASS，印出 `✅ testnet 即時累計 builder fee = ...`（即 Spec §3 testnet 端到端驗收）

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_testnet_flow.py
git commit -m "test: testnet end-to-end integration (skipped by default)"
```

---

## Task 15: CLI 入口 + 隔日 CSV 對帳指令

**Files:**
- Create: `scripts/run_testnet_flow.py`, `scripts/reconcile_day.py`

> 串起既有模組成可執行入口。無新邏輯，故以 smoke import 測試取代 TDD（邏輯已在 Task 8–11 測過）。

- [ ] **Step 1: 寫 flow 入口**

```python
# scripts/run_testnet_flow.py
"""端到端跑 onboarding→下單→即時累計驗證。需先 bootstrap key 與設環境變數。"""
import os
from decimal import Decimal
from spark.config import Settings
from spark.keystore.keychain import MacKeychainBackend
from spark.onboarding import onboard
from spark.orchestrator import place_marketable_order
from spark.verification.accrued import wait_for_accrual
from spark.exchange.hyperliquid import HyperliquidAdapter
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange


def main():
    network = os.environ.get("SPARK_NETWORK", "testnet")
    account_id = os.environ["SPARK_ACCOUNT_ID"]
    settings = Settings(builder_address=os.environ["SPARK_BUILDER_ADDR"],
                        account_id=account_id, network=network)
    ks = MacKeychainBackend()
    main_signer = ks.get_main_signer(account_id)
    agent = ks.get_agent_signer(account_id)
    info = Info(settings.api_url, skip_ws=True)
    adapter = HyperliquidAdapter(network, info=info, exchange=Exchange(agent, settings.api_url))

    onboard(adapter, settings, main_signer=main_signer,
            agent_address=os.environ["SPARK_AGENT_ADDR"],
            user_address=os.environ["SPARK_USER_ADDR"])
    best_px = Decimal(str(info.all_mids()[settings.coin]))
    res = place_marketable_order(adapter, settings, agent_signer=agent,
                                 is_buy=True, best_opposite_px=best_px)
    print(f"order filled_size={res.filled_size} avg_px={res.avg_px}")
    accrued = wait_for_accrual(adapter, settings.builder_address)
    print(f"✅ 即時累計 builder fee = {accrued}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 寫對帳入口**

```python
# scripts/reconcile_day.py
"""可獨立重跑的隔日 CSV 對帳。用法: SPARK_*=... uv run python -m scripts.reconcile_day YYYYMMDD"""
import os
import sys
from datetime import datetime
from spark.config import Settings
from spark.exchange.hyperliquid import HyperliquidAdapter
from spark.verification.reconcile import reconcile
from hyperliquid.info import Info


def main():
    day = datetime.strptime(sys.argv[1], "%Y%m%d").date()
    network = os.environ.get("SPARK_NETWORK", "testnet")
    settings = Settings(builder_address=os.environ["SPARK_BUILDER_ADDR"],
                        account_id=os.environ.get("SPARK_ACCOUNT_ID", "acct"), network=network)
    adapter = HyperliquidAdapter(network, info=Info(settings.api_url, skip_ws=True), exchange=None)
    report = reconcile(adapter, settings.builder_address, day, expected_coin=settings.coin)
    print(report.render())
    raise SystemExit(0 if report.matched else 1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: smoke import 測試**

Run:
```bash
uv run python -c "import scripts.run_testnet_flow, scripts.reconcile_day; print('imports ok')"
```
Expected: `imports ok`（無 import error；ruff 也應通過 `uv run ruff check`）

- [ ] **Step 4: Commit**

```bash
git add scripts/run_testnet_flow.py scripts/reconcile_day.py
git commit -m "feat: CLI entrypoints for testnet flow and daily reconciliation"
```

---

## Task 16: Mainnet 極小額複驗（手動關卡，無新碼）

> Spec §3 / §8。同一套程式切 `SPARK_NETWORK=mainnet`，跑一次最小可行金額。**手動執行、需人盯著**。不寫自動化測試。

- [ ] **Step 1: 前置檢查**

確認：mainnet 主錢包已入金（≥ 100 USDC 門檻 + 極小下單額）；main/agent key 已 bootstrap 到 Keychain（用獨立 account_id，例如 `mainnet-acct`）。

- [ ] **Step 2: 執行端到端**

Run:
```bash
SPARK_NETWORK=mainnet SPARK_ACCOUNT_ID=mainnet-acct \
SPARK_USER_ADDR=0x... SPARK_AGENT_ADDR=0x... SPARK_BUILDER_ADDR=0x... \
  uv run python -m scripts.run_testnet_flow
```
Expected: 印出 order filled + `✅ 即時累計 builder fee = >0`

- [ ] **Step 3: 隔日 CSV 對帳**

Run（隔日）:
```bash
SPARK_NETWORK=mainnet SPARK_BUILDER_ADDR=0x... \
  uv run python -m scripts.reconcile_day <下單當日YYYYMMDD>
```
Expected: 報告 `matched: True`，列出對應成交與 builder_fee。

- [ ] **Step 4: 記錄結果**

把 mainnet 複驗的 order/accrued/對帳輸出貼進 `docs/superpowers/research/2026-06-19-hl-sdk-findings.md` 收尾，commit。

---

## Self-Review

**Spec coverage：**
- §3 成功條件 1（testnet 端到端）→ Task 8/9/10/14 ✅
- §3 成功條件 2（maxBuilderFee≠0 / accrued>0 / CSV 解析）→ Task 5(config) / 10(accrued) / 7+11(CSV) ✅
- §3 成功條件 3（mainnet 極小額複驗）→ Task 16 ✅
- §3 成功條件 4（離線可跑大部分測試）→ Task 4 FakeAdapter + 全單元測試預設不連網 ✅
- §6 非託管不變量（無 withdraw/transfer）→ Task 3 介面測試 `test_adapter_is_abstract_and_has_no_withdraw` ✅
- §6 ApproveBuilderFee 主錢包簽 → Task 8 只用 main_signer；orchestrator(Task 9) 只用 agent_signer，路徑隔離 ✅
- §6 費率上限 f≤100 / maxRate="0.1%" → Task 2 + Task 5 ✅
- §6 builder 啟用門檻 ≥100 USDC → Task 8 `InsufficientFunds` ✅
- §6 Secrets 不落 log → Task 13 `test_import_does_not_print_key` ✅
- D1 Ioc 可成交單 → Task 9/12 ✅
- D2 兩段式 → Task 10（即時）+ Task 11/15（CSV 可重跑）✅
- D4 可抽換 keystore → Task 6 介面 + Keychain 後端 ✅
- §11 研究項 → Task 0 spike ✅

**Placeholder scan：** 無 TBD/TODO；所有 code step 含完整可執行碼。CSV 表頭與 SDK 簽名的不確定性以 Task 0 findings + header alias map 明確處理，非佔位。

**Type consistency：** `Settings`、`BuilderCode(b,f)`、`Order(coin,is_buy,size,limit_px,tif)`、`Fill(time,coin,px,sz,side,builder_fee)`、`OnboardingState`、`ReconcileReport`、`wait_for_accrual` 等型別/簽名跨 task 一致；FakeAdapter 與 HyperliquidAdapter 同實作 `ExchangeAdapter` 全部抽象方法。
