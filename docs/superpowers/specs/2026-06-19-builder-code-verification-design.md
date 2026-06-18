# Phase 1 設計 — Builder Code 金流驗證（Hyperliquid）

> 狀態：已通過 brainstorm，待 write-plan。
> 來源：`phase1-builder-code-spec.md`（工程規格草稿）+ 本次 brainstorm 釐清的 5 個決定。
> 日期：2026-06-19

---

## 1. Goal（唯一要證明的事）

在 Hyperliquid 上，端到端證明：透過「客戶主錢包授權 builder fee + 授權 agent + 下單夾帶 builder code」這條路徑，**一筆 builder fee 的實累會計入我的 builder 地址，且可被程式驗證**。

驗收：先在 **testnet** 用一個專用測試帳號跑通完整流程（授權 → agent 下單成交 → 即時累計費 > 0 → 隔日 CSV 對帳），再在 **mainnet** 用極小額複驗一次。

## 2. Non-goals（Phase 1 明確不做）

allocation engine、Bitfinex 出借、多客戶 / slider、完整 dashboard、計息、法幣 on-ramp、place-before-cancel race 處理（已知、延後，見 §9）。

---

## 3. brainstorm 釐清的關鍵決定

| # | 決定 | 理由 |
|---|------|------|
| D1 下單成交策略 | **可成交限價單（marketable limit）**，`tif="Ioc"` | builder fee 只在實際成交才產生；Ioc 保證能成交即成交、不留殘單，天然避開 §9 race |
| D2 驗證收尾 | **兩段式**：即時 `query_builder_accrued > 0` = Phase 1 主成功；CSV 對帳做成**可獨立重跑**指令 | builder_fills CSV 每日彙整、隔日才就緒，不該卡住主流程 |
| D3 工具鏈 | **uv + pytest + ruff，Python 3.11** | 現代、快、arm64（M1 / 未來 AWS Graviton）相容好；可平移到 Linux VPS |
| D4 keystore | **可抽換介面 + MacKeychainBackend（Phase 1）**；介面預留 env 注入後端（未來 VPS） | macOS Keychain 無法搬到 Linux；介面把「搬家」擋在後面，符合 §6 secret 硬規範 |
| D5 testnet 主錢包 key | 同樣存 Keychain，**只**由 onboarding/bootstrap（test harness）使用，與 orchestrator 隔離 | 維持「agent 只能 place/cancel、不碰 main key」的不變量於程式結構成立 |
| D6 費率 | `f=20`（= 2 bp = 0.02%）；`ApproveBuilderFee` 的 `maxRate="0.1%"`（協議上限一次給足） | f 之後可調而不需重簽 |

---

## 4. 架構：模組邊界與依賴方向

```
config/         # builder 地址、f=20、max_rate="0.1%"、network 切換、endpoint、coin、order_size、account_id
keystore/       # 介面 get_main_signer / get_agent_signer(account_id, role)
                #   後端: MacKeychainBackend（Phase 1）；介面預留 EnvInjectedBackend（未來 VPS）
exchange/
  base.py       # ExchangeAdapter ABC（reads + writes；無 withdraw/transfer）+ 核心型別
  hyperliquid.py# hyperliquid-python-sdk 實作
  fakes.py      # FakeAdapter，讓大部分測試離線可跑
onboarding/     # 腳本化：main_signer 簽 ApproveBuilderFee + ApproveAgent（test harness 專屬）
orchestrator/   # agent_signer 下可成交限價單，夾帶 builder={b,f}
verification/
  accrued.py    # 即時：輪詢 query_builder_accrued > 0（Phase 1 主成功）
  reconcile.py  # 對帳：可獨立重跑，解 builder_fills LZ4 CSV 找對應成交
tests/          # 單元測試（FakeAdapter）+ 標記跳過的 integration 測試
```

**依賴方向**：`onboarding` / `orchestrator` / `verification` 只依賴 `ExchangeAdapter` 介面與 `keystore` 介面，不直接碰 SDK。測試以 `FakeAdapter` 離線執行。

### 4.1 ExchangeAdapter 介面（Phase 1 最小集合）

```python
class ExchangeAdapter(ABC):
    # --- reads ---
    def get_account_value(self, address: str) -> Decimal: ...
    def query_max_builder_fee(self, user: str, builder: str) -> int: ...     # 0 = 未授權
    def query_builder_accrued(self, builder: str) -> Decimal: ...            # 累計 builder fee
    def fetch_builder_fills(self, builder: str, day: date) -> list[Fill]: ...# 解 LZ4 CSV

    # --- writes ---
    # approve_builder_fee / approve_agent 概念上屬「客戶主錢包」動作。
    # Phase 1 testnet 由 test harness 用測試主錢包私鑰簽；正式環境由前端錢包簽。
    def approve_builder_fee(self, main_signer: Signer, builder: str, max_rate: str) -> TxResult: ...
    def approve_agent(self, main_signer: Signer, agent_address: str) -> TxResult: ...
    def place_order(self, agent_signer: Signer, order: Order,
                    builder: BuilderCode) -> OrderResult: ...                # agent 簽

    # 刻意「不存在」withdraw / transfer —— 非託管不變量
```

### 4.2 核心型別

```python
@dataclass(frozen=True)
class BuilderCode:
    b: str          # builder 地址（統一小寫/checksum 處理）
    f: int          # 20（十分之一 bp）

@dataclass(frozen=True)
class Order:
    coin: str
    is_buy: bool
    size: Decimal
    limit_px: Decimal   # 可成交價（穿過對手盤）
    tif: str            # "Ioc"

@dataclass(frozen=True)
class Fill:
    time: datetime; coin: str; px: Decimal; sz: Decimal
    builder_fee: Decimal     # 其餘欄位以實際 CSV 表頭為準
```

**金額**：全程 `Decimal`，禁用 `float`；價格/size/費用/account value 一律 Decimal 或字串進出。

---

## 5. 端到端流程（狀態機，標出簽章器與模組）

```
[UNFUNDED]
  │  客戶自行入金（testnet: faucet / 手動轉入測試主錢包）
  ▼
[FUNDED]            驗證: get_account_value(main) > 0 且 ≥ 100 USDC（builder 啟用門檻）
  │  onboarding: main_signer 簽 ApproveBuilderFee(builder, "0.1%")   ← test harness
  ▼
[BUILDER_APPROVED] 驗證: query_max_builder_fee(user, builder) != 0
  │  onboarding: main_signer 簽 ApproveAgent(agent_address)          ← test harness
  ▼
[AGENT_AUTHORIZED]
  ▼
[READY]
  │  orchestrator: agent_signer 下 Ioc 可成交限價單, builder={b:<addr>, f:20}
  ▼
[ORDER_FILLED]
  │  verification/accrued: 輪詢 query_builder_accrued(builder) > 0   ← Phase 1 主成功
  ▼
[VERIFIED_REALTIME] ✅
  │  （之後可獨立重跑）
  ▼
verification/reconcile: 解 builder_fills CSV 找對應成交 → [RECONCILED] ✅
```

**設計取向**：狀態判定全靠鏈上/API 查詢（`get_account_value` / `query_max_builder_fee` / `query_builder_accrued`），不依賴本地記憶體狀態 → 流程**可中斷重跑、冪等**。

`agent_signer` 只出現在 `[READY]→下單`；`main_signer` 只出現在兩個 Approve 步驟（test harness）。兩者程式路徑不交叉。

---

## 6. Invariants & Constraints（硬規範，違反即 bug）

- **非託管不變量**：agent / API wallet 只能 `place` / `cancel`。**永遠不得**有任何 withdraw / transfer 路徑。adapter 介面刻意不存在喚款方法。
- **ApproveBuilderFee / ApproveAgent 必須由主錢包簽**，不得用 agent / API key 代簽。Phase 1 testnet 由 test harness（持測試主錢包私鑰）簽；正式環境由前端錢包簽。
- **費率上限**：`f ≤ 100`（0.1%）。Phase 1 固定 `f=20`。`maxRate="0.1%"`。
- **builder 啟用門檻**：主 builder 地址需維持 perps account ≥ 100 USDC，否則 builder code 不生效。
- **Secrets**：私鑰加密存放（Keychain），**絕不進 repo、絕不進 log**。log 對私鑰/簽章原料一律遮罩（地址可印）。
- **環境**：testnet 先行；mainnet 只在最後做一次極小額複驗。
- **Stablecoin**：只碰 Arbitrum 原生 USDC；USDT↔USDC、跨所入金屬後續階段。

---

## 7. 測試策略

- **單元/邏輯測試（離線）**：用 `FakeAdapter`。涵蓋狀態機轉換、費率換算（f ↔ %）、CSV 解析、冪等重跑。
- **CSV parser 黃金測試**：以真實格式 fixture（LZ4 或解壓 CSV 片段）驗證解析。
- **整合測試（testnet）**：`@pytest.mark.integration`，預設跳過，需顯式開啟才連網。
- 不連網即可跑過絕大多數測試（spec 成功條件 §4）。

---

## 8. Mainnet 極小額複驗

同一套程式、切 `network=mainnet`，跑**一次**最小可行金額（≥100 USDC 門檻 + 極小 size）：驗 `query_max_builder_fee != 0`、`query_builder_accrued > 0`、隔日 CSV 對帳。**手動觸發、不自動**，於 plan 中標為獨立關卡。

---

## 9. 已知但延後的風險（不在 Phase 1）

- **place-before-cancel race**：rebalance 時可能觸發 insufficient margin。Phase 1 用 Ioc 單筆下單天然避開；延至 orchestrator 多倉/rebalance 階段處理（place/cancel 順序與 margin 檢查）。
- **USDT ↔ USDC / 跨所入金**：屬後續階段。

---

## 10. 模型分工（寫進 plan 的 task frontmatter）

| 階段 / 任務 | 模型 |
|---|---|
| brainstorm / write-plan | Opus 4.8（`claude-opus-4-8`） |
| 安全 / 簽章 / 合併相關 task | Sonnet 4.6（`claude-sonnet-4-6`） |
| 一般實作（adapter 樣板、config、CSV parser、測試骨架） | Haiku 4.5（`claude-haiku-4-5`） |
| code review gate | Sonnet 4.6 / Opus 4.8 |

**Review 必檢項**：
1. 非託管不變量 —— agent 無 withdraw/transfer 路徑、orchestrator 不碰 main key。
2. ApproveBuilderFee / ApproveAgent 只由 main_signer（test harness）簽。

---

## 11. write-plan 前需驗證的外部假設（研究項，非阻擋設計）

- testnet 的 `stats-data.hyperliquid.xyz/Testnet/builder_fills/...` 是否確實提供 CSV（若否，testnet 的 §8 CSV 對帳改為僅在 mainnet 複驗時做，testnet 只認即時累計）。
- `hyperliquid-python-sdk` 對 `approve_builder_fee` / `approve_agent` / 下單夾帶 builder 的確切簽名與參數格式。
- builder_fills CSV 的實際欄位表頭（決定 `Fill` 的欄位對應）。
