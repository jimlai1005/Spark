# Copytrade testnet E2E 整合測試（T4.2）——執行前置與判讀指南

**Date:** 2026-07-16
**Branch:** `feat/builder-code-verification`
**任務:** Task 17（spec T4.2）
**狀態: BLOCKED —— testnet 帳戶未注資，測試尚未實跑，僅完成撰寫 + collect-only 驗證**

---

## 1. 測試涵蓋範圍

`tests/integration/test_copytrade_testnet.py::test_copytrade_e2e_testnet`（`@pytest.mark.integration`，
預設被 `-m 'not integration'` 跳過）涵蓋 spec T4.2：

> Onboard testnet follower（重用 `spark.onboarding.onboard`）→ 跑單 →
> `wait_for_accrual` 判定；覆蓋 place 與 modify 兩路徑。

對應 M1 驗收 gate（`docs/superpowers/specs/2026-07-16-copytrade-orchestrator-m1-spec.md:85`）：

> Builder fee：主網 100% fills 有 accrual（**place 與 modify 分開驗**）

測試步驟：

1. 環境變數檢查：`SPARK_ACCOUNT_ID` / `SPARK_USER_ADDR` / `SPARK_BUILDER_ADDR` 缺一即
   帶訊息失敗。
2. Onboarding 冪等重用（沿用 `tests/integration/test_testnet_flow.py` 的 `has_agent`
   判斷：Keychain 已有 agent key 就跳過 `approve_agent`，避免無謂 rotate）。
3. **modify 路徑 accrual**：`baseline_1` → 遠端 GTC 掛單（`mid×0.7`，帶 builder，不會
   立即成交）→ `modify_order` 改價至可成交（`mid×1.05`, `Ioc`）→ `get_user_fills`
   輪詢確認真的成交（`modify_order()` 只回傳 bool，不足以區分「沒成交」與「成交但
   歸屬遺失」）→ `wait_for_accrual(baseline_1)`。**若逾時（增量為 0），測試不 fail**，
   只印警告——這是 T1.3 的政策資料點，見第 3 節判讀指南。
4. **place 路徑 accrual**：重取 mid（步驟 3 的輪詢最多耗 70 秒以上，沿用舊 mid 若
   行情已動 >0.5% 會使 `Ioc` 假性「未成交」）→ `baseline_2` = 步驟 3 後的當前
   accrued → marketable `Ioc` 單（沿 `orchestrator.place_marketable_order`，builder
   走 `"order"` action，已知歸屬正確）→ `wait_for_accrual(baseline_2)`，**斷言增量
   > 0**（此路徑不允許逾時，逾時代表 builder 完全未生效或注資/授權有問題，屬測試
   前提失效，應讓例外照常炸開）。
   步驟 3-5 全程包 `try/finally`：中途失敗時對步驟 3 殘留的 GTC 掛單 best-effort
   撤單（cancel 失敗只印警告，不改變測試結果），見 §3.3 第 3 條。
5. **reduce-only 平倉**：對步驟 3+4 累積的殘留部位（兩次皆 `is_buy=True`，故為多頭）
   全量 `close_reduce_only`，斷言平倉後 `get_positions` 查無該 coin 部位。
6. 全程只印數字與 `oid`，不印私鑰／簽章物件。

步驟 3 與 4 分開建立各自的 baseline、分開斷言，直接對應 gate 的「place 與 modify 分開
驗」——不是圖方便合併成一次 baseline/一次斷言，是刻意保留兩條路徑各自的因果鏈可追溯性。

---

## 2. 執行前置：兩地址注資需求

沿用 `docs/superpowers/research/2026-07-16-modify-builder-attribution.md`（T1.3 探針
報告）已查明的同一組地址與需求金額——本測試與 T1.3 探針共用同一組 testnet 帳戶，注資
一次即可覆蓋兩者：

| 角色 | 地址 | 需求金額（testnet USDC） |
|---|---|---|
| Follower | `0x5579b5Ab953d59fc4d40fDA3199a13E91b680B5d` | ≥ 500 |
| Builder | `0x63e64A1c73b28Ca4FdFAC409DeE3e2BeE4B84847` | ≥ 100（`src/spark/config.py:13` `MIN_BUILDER_BALANCE`——低於此門檻 `onboard()` 的 `BuilderNotEligible` 檢查會直接拒絕，見 `src/spark/onboarding.py:56-61`） |

Follower 端 500 USDC 同時滿足 `onboard()` 的 `InsufficientFunds` 檢查（同一常數，
`src/spark/onboarding.py:51-54`）與本測試實際下單所需保證金（M1 `order_size` 預設
`Decimal("0.01")` ETH，本測試在步驟 3+4 共下兩筆買單、步驟 5 一筆平倉單，含滑價緩衝，
500 USDC 綽綽有餘）。

2026-07-16 查詢結果（沿用 T1.3 報告）：兩個地址的 perp/spot 餘額皆為 0，尚未注資。

**執行指令**（注資後跑，`ACCOUNT_ID` 需與 Keychain 已存的 main/agent key 對應）：

```bash
SPARK_ACCOUNT_ID=<既有帳號 ID> \
SPARK_USER_ADDR=0x5579b5Ab953d59fc4d40fDA3199a13E91b680B5d \
SPARK_BUILDER_ADDR=0x63e64A1c73b28Ca4FdFAC409DeE3e2BeE4B84847 \
uv run pytest tests/integration/test_copytrade_testnet.py -m integration -v -s
```

（`-s` 保留測試內的 `print` 診斷輸出；`-m integration` 覆蓋 `pyproject.toml` 的預設
`addopts = "-m 'not integration'"`，否則會被 deselect。）

---

## 3. 判讀指南

### 3.1 步驟 4（place 路徑）失敗

place 路徑走 `"order"` action，builder 欄位在協議層確定存在（見 T1.3 報告 §2.1 的靜態
分析）。若步驟 4 的 `wait_for_accrual` 逾時或 `assert delta_place > 0` 失敗，代表的不是
「modify 語意問題」，而是更基本的前提失效，依序排查：

1. `settings.builder_address` 是否確實已 `approve_agent`／`approve_builder_fee`（步驟 2
   的 onboarding 斷言若已通過，這步應已排除）。
2. Builder 地址餘額是否 ≥ `MIN_BUILDER_BALANCE`（100 USDC）——低於門檻時 fee 不會計費，
   但 `onboard()` 應已在此之前擋下（`BuilderNotEligible`），若繞過此檢查代表 onboarding
   邏輯有 regression。
3. `query_builder_accrued` 讀的 `builderRewards` 是否有延遲（見
   `src/spark/verification/accrued.py` 的輪詢設計，預設 10 次 × 3 秒，若 testnet 延遲更長
   需要調大 `attempts`/`sleep_s`）。

### 3.2 步驟 3（modify 路徑）accrual 增量為 0

**這不是本測試要抓的 bug，是 T1.3 要收集的政策資料點。** 對照
`docs/superpowers/research/2026-07-16-modify-builder-attribution.md` 第 3 節判定準則：

```
ratio = Δ_modify / expected_fee

ratio > 0.9   → 支持假說 (a)：modify 後訂單仍歸屬 builder（歸屬保留）
ratio < 0.1   → 支持假說 (b)：modify 後訂單 builder 歸屬遺失
0.1 ≤ ratio ≤ 0.9 → 異常區間，需人工深查
```

本測試只印增量數字與警告訊息，**不計算 ratio**（該計算屬於
`scripts/testnet_modify_probe.py` 的職責，兩者互補：探針腳本產生 T1.3 決策用的結構化
數據＋ratio，本整合測試把同一條 modify 路徑釘進 CI 回歸範圍，長期監控歸屬行為是否
隨 SDK/協議版本改變而漂移）。若增量為 0：

1. 先確認 `_confirm_fills` 有查到成交（測試若在這步就 `assert fills` 失敗，代表
   「根本沒成交」而非「成交但歸屬遺失」，兩者對應完全不同的後續動作——前者是流動性/
   價位問題，重跑或調整 `mid×1.05` 的乘數；後者才是歸屬問題）。
2. 若 `_confirm_fills` 已確認成交、但 `wait_for_accrual` 仍逾時，對照 T1.3 報告第 4 節
   的兩個政策選項（容忍漏繳 vs 強制 `cancel+place`），**由使用者裁決**——不得由 agent
   自行決定（spec `:14`、`:45` 明文禁止）。
3. 若 T1.3 已有更新的政策決定（例如已切換 `COPY_MODIFY_POLICY=cancel-place`），本測試
   步驟 3 的 modify 路徑本身仍應保留跑（驗證「舊路徑歸屬確實有缺口」這件事本身是回歸
   測試的一部分，即便主動路徑已不再使用它）。

### 3.3 步驟 5（reduce-only 平倉）失敗

平倉失敗屬安全關鍵路徑（全域工程原則 3：安全關鍵動作失敗必須大聲告警，不得靜默）。
若 `close_reduce_only` 回傳 `ok=False` 或平倉後 `get_positions` 仍查到殘留部位：

1. **不要重跑測試了事**——testnet 帳戶會留有未平倉部位，下次測試執行的 baseline
   會受殘留部位影響（例如下次 `place_marketable_order` 若方向相反，可能被判定成
   加倉而非新倉）。
2. 先用 `get_open_orders` / 交易所 testnet 前端人工確認實際部位狀態，再決定是否手動
   平倉或調整測試的 `slippage` 參數（預設 `Decimal("0.01")`，thin testnet 流動性下
   可能不足以確保 reduce-only IOC 單完全成交）。
3. **任何步驟中途失敗後，須確認步驟 3 的 `mid×0.7` GTC 掛單已撤**（測試的 `finally`
   已自動嘗試 best-effort `cancel_order`，成功則無殘留；若印出「殘留掛單清理失敗」
   警告，須人工以 `get_open_orders` 確認並手動撤掉）。該掛單帶 builder、價位遠低於
   市價，若殘留簿上、日後行情下探成交，會在**未來執行**的 accrual baseline 之後產生
   非本次下單造成的增量（假陽性），污染 place/modify 兩路徑的分開驗證。

---

## 4. xyz accrual（spec T4.2「xyz accrual 獨立驗證亦在此」）——選配，deferred

Spec T4.2 原文（`docs/superpowers/specs/2026-07-16-copytrade-orchestrator-m1-spec.md:75`）：

> Onboard testnet follower（重用 `spark.onboarding.onboard`）→ 跑單 →
> `wait_for_accrual` 判定；覆蓋 place 與 modify 兩路徑。**xyz accrual 獨立驗證亦在此。**

同一份 spec 的紅線 6（`:33`）：

> M1 只跟 crypto（不碰 xyz / builder-deployed DEX）；xyz 的 builder accrual 屬 T4.2
> 獨立驗證項，在那之前不解禁。

本測試**刻意不包含 xyz accrual 驗證**，理由：

1. M1 範圍本身不解禁 xyz（紅線 6），本測試對應的是 M1 crypto 路徑的驗收，不是 M1
   解禁 xyz 的前置條件——兩者是獨立的任務邊界，不應該綁在同一支測試裡（綁在一起會讓
   crypto 路徑的 CI 回歸依賴一個 M1 本來就不需要的能力）。
2. **testnet 是否有 xyz 市場可供下單尚未查證**——`Info.meta()` 的 `universe` 是否包含
   builder-deployed DEX 的 xyz 市場、其下單/accrual 語意是否與 perp crypto 路徑一致
   （例如 builder fee 是否走同一個 `query_builder_accrued`／`builderRewards` 計數器，
   或有獨立的 accrual 機制），這些都需要另外的靜態調查，本次任務範圍（Task 17）未涵蓋。

**狀態：deferred，交使用者決定是否/何時開一個獨立任務調查。** 若後續要補齊，建議先
做一次 `Info.meta()` 查詢確認 testnet 是否列出 xyz universe，再決定驗證方式（是否能
沿用本測試同一套 onboarding/accrual 骨架，或需要完全不同的下單路徑）。

---

## 5. 狀態：BLOCKED——testnet 帳戶未注資

與 T1.3 探針共用同一組地址（見第 2 節），尚未注資，測試尚未實跑。`--collect-only`
與預設 `-m 'not integration'` 排除已驗證（見下方證據），但實際 accrual 數字（步驟 3/4
的增量、步驟 5 的平倉結果）待注資後才能取得。

驗證證據（2026-07-16）：

```
$ uv run pytest tests/integration/test_copytrade_testnet.py --collect-only -q -m integration
tests/integration/test_copytrade_testnet.py::test_copytrade_e2e_testnet
1 test collected in 0.18s

$ uv run pytest tests/integration/test_copytrade_testnet.py -q
1 deselected in 0.16s
```
