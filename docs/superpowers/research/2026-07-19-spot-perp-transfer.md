# spot↔perp 劃轉、帳戶型態，與績效數字正確性

日期：2026-07-19｜SDK：hyperliquid-python-sdk **0.24.0**（`.venv/lib/python3.14/site-packages/hyperliquid/`）
動機：(1) 客戶入金門檻——錢落在 spot 要不要客戶自己劃轉；(2) perp 窗 `pnlHistory` 會不會把內部劃轉當入金。

---

## 結論先行

1. **HL 確實有「帳戶型態」，使用者沒記錯**——官方稱 account abstraction modes，選項字面就叫 **Manual / Standard**、**Unified account**、**Portfolio margin**。不是 cross/isolated（那是單一部位的保證金模式，不同軸）。
2. **spot→perp 劃轉（`usdClassTransfer`）是 user-signed action，結構上必須主鑰簽**——agent key 做不到，我們**無法代客戶劃轉**，只能教學＋偵測提示。這反而是非託管不變量的加強，不是衝突。
3. **內部劃轉會不會污染 perp 窗 `pnlHistory`：證據不足以判定**——官方文件只定義 deposits/withdrawals，對 `accountClassTransfer` 隻字未提。實驗設計見第 5 節，**尚未執行**。

---

## 1. HL 的帳戶型態：account abstraction modes

官方獨立頁面：[Account abstraction modes](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/account-abstraction-modes)。逐字引用：

> "Manual / Standard (recommended for market makers, high volume automated users, and deployers/builders): separate perp and spot balances, separate DEX balances."

模式清單（官方頁面）：Unified account、Portfolio margin、Manual / Standard、DEX abstraction（最後者是另一條軸：是否跨 DEX 共用餘額）。

SDK 端的型別定義（**已實際查證，非憑印象**）：
- `utils/types.py:186`：`Abstraction = Literal["unifiedAccount", "portfolioMargin", "disabled"]`
- `utils/types.py:187`：`AgentAbstraction = Literal["u", "p", "i"]`
- `exchange.py:59-63`：`USER_SET_ABSTRACTION_WIRE_VALUES = {"disabled": "i", "unifiedAccount": "u", "portfolioMargin": "p"}`
- 唯讀查詢：`info.py:634` `query_user_abstraction_state(user)` → `{"type": "userAbstraction", "user": ...}`

第三方文件（[Dwellir](https://www.dwellir.com/docs/hyperliquid/user-abstraction)）指回應值另有 `default`，即 SDK 三值之外還有一個「未設定」狀態——與 SDK example 註解 "the account must be in \"default\" mode to succeed" 互相印證。

**對照使用者的用詞**：「united」＝ **unifiedAccount**；「manual」＝ HL 官方頁面字面上的 **Manual / Standard**。對應關係成立。

各模式下 spot USDC 是否算 perp 保證金：
| 模式 | spot USDC 可當 perp 保證金 | 客戶要不要劃轉 |
|---|---|---|
| Manual / Standard | ❌ 分離 | 要 |
| Unified account | ✅ 單一保證金池 | 不用 |
| Portfolio margin | ✅ 全組合（HYPE/BTC/USDC/USDT） | 不用 |

### ⚠️ 業務關鍵：builder fee 與 standard mode

同一頁逐字：

> "Builder code addresses must be in standard mode to accrue builder fees"

以及：unified account 與 portfolio margin 各**限 50k user actions/day**，standard 無此限制。

我方 builder 地址**必須留在 standard mode**。

> ✅ **主詞歧義已於 2026-07-19 解決 → 見文末「第 8 節」。結論：主詞是我方 builder 地址（讀法 A），客戶的帳戶模式不影響 builder fee。** 官方 builder-codes 頁另有一句無歧義的表述，並經 mainnet 實測反證讀法 B。原「反面證據 2」已作廢。

---

## 2. spot↔perp 劃轉的 API

**Action type：`usdClassTransfer`**。SDK 封裝：`exchange.py:474-491`

```
def usd_class_transfer(self, amount: float, to_perp: bool) -> Any
```
- `to_perp=True` → spot→perp；`False` → perp→spot
- action 欄位：`{"type": "usdClassTransfer", "amount": str, "toPerp": bool, "nonce": ms}`
- 子帳戶語法特殊：`exchange.py:477-478` 把 `f" subaccount:{vault_address}"` **附加在 amount 字串後面**（不是獨立欄位）
- `exchange.py:106`：`_post_action` 對 `usdClassTransfer` 與 `sendAsset` **強制把 `vaultAddress` 設為 None**——與上一點是同一件事的兩半

相關但不同的 action：`send_asset`（`exchange.py:492-515`，`type: "sendAsset"`）用於跨 DEX／跨 perp dex 搬運，perp 用空字串 `""`、spot 用 `"spot"` 當 dex 名。

官方文件對 `amount` 的逐字說明（[Exchange endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint)）：
> "USD amount to transfer as a string, e.g. '1' for 1 usd. If you want to use this action for a subaccount, you can include subaccount: address after the amount"

**本 repo 目前封裝了什麼：什麼都沒有。** `grep` 確認 `src/spark/exchange/hyperliquid.py` 與 `base.py` 完全不含 class transfer／spot 餘額讀取。`base.py:1` 的模組 docstring 明寫「刻意不含任何 withdraw/transfer（非託管不變量）」。

---

## 3. ⭐ 這個劃轉需要什麼金鑰？→ **主鑰。agent key 做不到。**

### 證據鏈（四條獨立線索指向同一結論）

**(a) SDK 走的是 user-signed 分支，不是 L1 分支**
`exchange.py:486` → `sign_usd_class_transfer_action(self.wallet, ...)`
→ `utils/signing.py:362-369` → `sign_user_signed_action(wallet, action, USD_CLASS_TRANSFER_SIGN_TYPES, "HyperliquidTransaction:UsdClassTransfer", is_mainnet)`

對比：下單走 `sign_l1_action`（`utils/signing.py:240`）。兩條路徑在 SDK 裡從頭到尾不交會。

**(b) SDK 自己的 recover 函式命名就寫明了差別**
- `utils/signing.py:458`：`recover_agent_or_user_from_l1_action(...)` ← L1 action 可以還原成 **agent 或 user**
- `utils/signing.py:467`：`recover_user_from_user_signed_action(...)` ← user-signed action 只還原成 **user**

用 agent key 簽 user-signed action，HL 還原出來的地址就是 agent 自己的地址（一個沒有資金的空帳戶），不會被歸屬到主帳戶。

**(c) SDK 官方 example 用守衛式明講**
[`examples/user_abstraction.py`](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/master/examples/user_abstraction.py)（`user_set_abstraction` 與 `usd_class_transfer` 同屬 user-signed 類）：

```python
if user == exchange.wallet.address:
    user_set_abstraction_result = exchange.user_set_abstraction(user, "unifiedAccount")
    ...
else:
    print("not performing user set abstraction because not user", exchange.account_address, exchange.wallet.address)
```

官方 example 作者刻意在「簽章錢包 ≠ 使用者本人」時跳過 user-signed 呼叫。這是 SDK 維護者對這條規則的直接背書。

**(d) 本 repo 的介面早就編碼了這個區分**
- `src/spark/exchange/base.py:153`、`:155`：`approve_builder_fee(main_signer, ...)`、`approve_agent(main_signer, ...)` ← 兩者都是 user-signed action
- `src/spark/exchange/base.py:157`、`:159`、`:168`、`:171`、`:190`：`place_order/cancel_order/market_open/close_reduce_only/update_leverage(agent_signer, ...)` ← 全是 L1 action

`usdClassTransfer` 與 `approve_agent`／`approve_builder_fee` 是同一類。若要加，簽名必須是 `main_signer`——而我們**沒有**主鑰。

補充：官方 [Exchange endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint) 也承認這個類別存在：「User-signed actions such as Core USDC transfer do not support the `expiresAfter` field」，SDK `exchange.py:135` 有對應註解。

### 與非託管不變量的關係

**三個層次要分開講，混在一起會得出錯誤結論：**

1. **HL 協議層**：agent key 簽不了任何 user-signed action，涵蓋 `withdraw3`（提款到 Arbitrum）、`usdSend`、`spotSend`（轉給別的地址）、`usdClassTransfer`。**客戶資金離不開客戶地址，這是 HL 保證的，不是我們保證的。** 我們的不變量因此有協議層背書，比只靠自律強得多。

2. **語意層（重要細節）**：`usdClassTransfer` **本質上不是提款**——spot 與 perp 是**同一個地址**的兩個餘額桶，劃轉後錢還在客戶自己名下。真正的託管邊界是 `withdraw3`/`usdSend`/`spotSend`（錢會離開地址），這三個才是「能不能動客戶的錢」。所以即使假設性地 agent 能劃轉，也**不構成託管**。結論不衝突。

3. **我們的自律層**：`tests/test_base_types.py:26-29` 斷言 `ExchangeAdapter` 沒有 `withdraw`／`transfer` 屬性。
   ⚠️ **觀察（非 finding）**：這條測試是**按方法名**斷言的，不是語意的。名為 `usd_class_transfer` 的方法可以通過這條測試。若未來要加任何劃轉能力，這條測試擋不住，需要另外設計判準。目前無需處理（我們本來就沒有主鑰，加了也不能用）。

**產品結論：我們結構上無法代客戶劃轉。教學是唯一選項**（見第 6 節，另建議加唯讀偵測提示）。

---

## 4. 能不能代客戶調整帳戶設定？→ **部分可以，而且比預期危險**

官方 [Account abstraction modes](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/account-abstraction-modes) 逐字：
> "See Python SDK and API docs for examples on the agent- and user-signed actions for changing account abstraction modes."

**官方明講有 agent-signed 路徑。** SDK 對照：

| 方法 | 位置 | 簽章類型 | 誰能簽 |
|---|---|---|---|
| `agent_set_abstraction(abstraction)` | `exchange.py:1166-1183` | **`sign_l1_action`** | **agent key 可以** |
| `user_set_abstraction(user, abstraction)` | `exchange.py:1201-1216` | `sign_user_set_abstraction_action`（user-signed） | 只有主鑰 |
| `agent_enable_dex_abstraction()` | `exchange.py:1147-1164` | `sign_l1_action` | agent key 可以 |
| `user_dex_abstraction(user, enabled)` | `exchange.py:1186-1199` | user-signed | 只有主鑰 |

`agent_set_abstraction` 收 `AgentAbstraction = Literal["u","p","i"]`（`types.py:187`），即 unifiedAccount／portfolioMargin／disabled 的簡碼。

**限制**：SDK example 註解逐字——「set abstraction for user via agent / **Note: the account must be in "default" mode to succeed**」。即 agent 只能把「尚未設定」的帳戶推進某個模式，不能任意改回。

### 建議：不要用，但要讀

- **不要呼叫 `agent_set_abstraction`。** 理由三條：(i) 這會改變客戶帳戶的風險模型（unified/portfolio margin 讓 spot 資產暴露在 perp 爆倉風險下），客戶沒有授權我們做這件事；(ii) 觸發 50k actions/day 上限；(iii) 與 builder fee 的 standard-mode 要求可能直接衝突（見反面證據 2）。這屬於 CLAUDE.md 紅線 5 的精神——碰客戶帳戶狀態的動作是人工決策。
- **建議加唯讀查詢** `info.query_user_abstraction_state(user)`（`info.py:634`）。這是純讀取、零風險，能讓 onboarding 直接判斷「這個客戶到底需不需要劃轉」，把第 6 節的教學從「一律照本宣科」變成「只在需要時提示」。

---

## 5. ⭐ 內部劃轉會不會被 perp 窗 `pnlHistory` 當入金扣除？

### 結論：**證據不足以判定。文件無法解答，必須實測。**

（本 repo 前一份研究 `docs/superpowers/research/2026-07-19-leader-performance-metrics.md:175` 已把這條列為「最關鍵的未驗證假設」。本次查證**沒有推翻也沒有證實它**，只是把邊界劃清楚。）

### 查了什麼、找到什麼

**官方 PnL 定義**（[Portfolio graphs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/portfolio-graphs)）逐字：
> "Pnl is defined as `account value` plus net deposits, i.e. `account value + deposits - withdrawals`."
> "Account value includes unrealized pnl from cross and isolated margin positions, as well as vault balances."

⚠️ **這句公式字面上是反的**：若真的 `+deposits`，入金會直接變成獲利。合理讀法是 `account value − (deposits − withdrawals)`。**官方文件在這個關鍵點上寫錯或寫得極不精確**——這本身就是「不能只靠文件、必須實測」的理由（見反面證據 5）。

**定義只涵蓋 deposits 與 withdrawals，完全沒提內部劃轉。** 而在 HL 自己的帳本裡，內部劃轉是**獨立的 delta 型別**：`userNonFundingLedgerUpdates`（`info.py:652`）的 `accountClassTransfer`（欄位 `usdc`、`toPerp`），與 `deposit`、`withdraw` 並列而非歸類其下（[Chainstack](https://docs.chainstack.com/reference/hyperliquid-info-user-non-funding-ledger-updates)，第三方）。

**兩個方向的推理（都只是推理，標記為推測）**：
- 傾向「有扣除」：perp 窗存在的意義就是 perp-only 績效；不扣除的話，任何做過劃轉的用戶績效都會失真，HL 沒理由留這個洞。
- 傾向「沒扣除」：官方定義用詞是 deposits/withdrawals，而 HL 在帳本層明確把 `accountClassTransfer` 與 `deposit` 分成兩個型別——若實作沿用同一套分類，內部劃轉就不在扣除範圍內。

兩邊都講得通 ⇒ **不判定**。

**已知的相關事實（已實測，非推測）**：總值窗（`day/week/month/allTime`）含 spot——本 repo testnet 實測 999 = 499 spot + 500 perp（`docs/superpowers/research/2026-07-19-testnet-e2e-findings.md:33-54`，F1）。因此**對總值窗而言劃轉是內部搬運、淨額為零**，問題只存在於 perp 窗。

### 實驗設計（**未執行**；執行與否由使用者決定）

**前置條件**
- **testnet** 錢包；帳戶**無未平倉部位**（排除 unrealized PnL 噪音）、**無掛單**（掛單佔用保證金會破壞 `accountValue == totalMarginUsed + withdrawable` 恆等式——全域工程原則 1 的既有事故）
- spot 與 perp 兩邊都要有 USDC
- **金鑰：testnet 主鑰**。`usdClassTransfer` 是 user-signed（第 3 題），**agent key 跑不了這個實驗**。這件事本身就是實驗的一部分結論。
- 劃轉金額 X 要**相對帳戶夠大**（例如 500 的帳戶劃 100），否則訊號會被 15 分鐘取樣的插值噪音蓋掉

**步驟**
1. **T0 快照**：`info.portfolio(addr)` 取全部 8 個窗；對 `perpDay`／`perpAllTime`／`day`／`allTime` 各記下 `accountValueHistory` 與 `pnlHistory` 的**最後一點 (ts, val)**。同時記 `info.user_state(addr)` 的 `marginSummary.accountValue`、`info.spot_user_state(addr)` 的 USDC 餘額（`info.py:130`）、`info.user_non_funding_ledger_updates(addr, startTime=T0−1h)`。
2. **劃轉**：`exchange.usd_class_transfer(X, True)`（spot→perp）。記下 wall-clock 時間。
3. **等待 ≥ 20 分鐘**。portfolio 每 15 分鐘取樣一次；官方說明取樣會在 deposits/withdrawals 時額外觸發，但**「內部劃轉會不會觸發取樣」正是未知數之一**，所以不能假設，要等滿一個完整取樣週期。
4. **T1 快照**：重複步驟 1 的全部查詢。
5. **比對**

| 觀測 | 結果 A | 結果 B |
|---|---|---|
| Δ perp `accountValueHistory` | ≈ **+X**（兩種結果都應如此——這是 sanity check，若不成立代表劃轉沒到位，實驗作廢） | 同左 |
| **Δ perp `pnlHistory`** | **≈ 0** ⇒ HL **有**把內部劃轉扣除 | **≈ +X** ⇒ HL **沒有**扣除 |
| Δ 總值窗 `accountValueHistory`／`pnlHistory` | ≈ 0（對照組：確認劃轉對總值 basis 是內部搬運） | 同左 |
| ledger | 應出現一筆 `accountClassTransfer {usdc: X, toPerp: true}` ⇒ 順帶確認第三方文件的型別名與 schema 正確 | 同左 |

6. **反向對照（不可省略）**：再跑一次 `usd_class_transfer(X, False)`（perp→spot），確認 perp `pnlHistory` 對稱反應。**單向實驗無法區分「HL 有扣除」與「剛好有其他 PnL 抵銷了 X」**；若正向 ≈ +X 而反向不是 ≈ −X，代表處理不對稱，結論必須保留、不得下判定。

**兩種結果的產品意義**
- **結果 A（≈ 0，有扣除）**：perp 窗 `pnlHistory` 可直接當績效來源，現有規劃不用改。
- **結果 B（≈ +X，沒扣除）**：**內部劃轉會顯示成假獲利**。必須接 `userNonFundingLedgerUpdates`，把區間內所有 `accountClassTransfer` 的淨額從 perp 窗 PnL 差分中扣掉。這會把「單一 portfolio 回應」變成「兩個端點對帳」——正是前一份研究刻意迴避的架構（工程原則 1：比較的兩側不該來自不同來源）。屬於需要重新設計的分支，工作量不小。

---

## 6. 客戶入金的實際流程

### 路徑 A：官方 bridge 入金（建議主推）→ **0 步劃轉**

客戶把 **Arbitrum 原生 USDC** 送到 HL bridge → 直接入 **perp** 帳戶 → 可立即跟單。

證據強度：**中等偏強，但官方文件未明說 class**。
- 官方 [HyperCore Bridge](https://hyperliquid.gitbook.io/hyperliquid-docs/hypercore/bridge) 只說 "Deposits to the bridge are signed by the validators and are credited when more than 2/3 of the staking power has signed the deposit"——**沒講是 perp 還是 spot**。
- 官方 [FAQ: Transfer or deposit to USDC (Perps) missing](https://hyperliquid.gitbook.io/hyperliquid-docs/support/faq/deposit-or-transfer-issues-missing-lost/transfer-or-deposit-to-usdc-perps-missing) 逐字：「If you have open positions on cross margin with negative unrealized P&L, **your deposits and Spot to Perp transfers** will go toward collateral for those open positions.」——把 **deposits** 與 **Spot→Perp transfers** 並列為 perps 餘額的兩個入口，**間接但有力地支持「deposit 直接入 perp」**。
- 多個第三方一致（[OneKey](https://onekey.so/blog/ecosystem/complete-guide-to-hyperliquid-deposits-withdrawals-2026-fbe041/)、[Eco](https://eco.com/support/en/articles/15191997-hyperliquid-bridge-deposit-usdc-and-cross-chain-routes-2026)）："Hyperliquid credits your perps account once the Arbitrum block is finalized"。

⚠️ **最低入金 5 USDC，低於此永久遺失**（官方 Bridge2 文件；第三方一致引用）。**教學必須寫這句。**

### 路徑 B：錢落在 spot → **需 1 步劃轉（客戶自己做）**

常見成因：從 CEX 提幣走非 Arbitrum 路線、用第三方跨鏈橋、或客戶先做過 spot 交易。
客戶操作：HL 前端 Transfer 把 USDC 從 Spot 移到 Perps（一鍵、免手續費）。
**我們無法代勞**（第 3 題）。

### 路徑 C：客戶已在 unifiedAccount／portfolioMargin 模式 → **不需劃轉**

spot USDC 直接算 perp 保證金。但與 builder fee 的 standard-mode 要求可能衝突（反面證據 2），**不要主動推薦客戶切換**。

### 教學要寫的最小集合（5 條）

1. **主推路徑**：用 Arbitrum 原生 USDC 走官方 bridge → 直接進 perp，不需任何劃轉
2. **最低 5 USDC**，低於此不會入帳且無法找回
3. **若餘額出現在 Spot**：到 HL 前端按 Transfer 移到 Perps（一鍵、免費）——附截圖
4. **明說我們不能代勞**：agent key 在協議層就沒有這個權限。這句話要正面寫成信任賣點（「我們連你的錢都碰不到」），不要寫得像功能缺失
5. **建議一併做（唯讀、零風險）**：dashboard 用 `info.spot_user_state`（`info.py:130`）＋ `info.user_state`（`info.py:86`）讀出兩邊餘額，偵測到「N USDC 卡在 Spot」就主動提示；用 `info.query_user_abstraction_state`（`info.py:634`）判斷該客戶是否根本不需要劃轉。**這能把流失步驟從「客戶要自己發現」降級成「我們提醒、客戶點一下」**——這是本次研究對入金門檻最實際的可執行改善。

---

## 7. 最強的反面證據（主動列出）

1. **官方從未明文寫「agent 不能簽 user-signed action」**。第 3 題的結論建立在：SDK 程式碼結構、SDK 官方 example 的守衛式、recover 函式命名、以及多個第三方一致陳述——**四條間接證據，零條官方明文**。我認為結論成立，但若要當作產品承諾對客戶宣稱「我們碰不到你的錢」，**建議用 testnet agent key 實打一次 `usd_class_transfer` 看錯誤訊息**，把它變成實跑輸出。這是唯讀查詢做不到的，必須實際送一筆（testnet、金額極小、預期失敗）。

2. ~~**「Builder code addresses must be in standard mode to accrue builder fees」的主詞有歧義**~~
   **❌ 已作廢（2026-07-19）**：主詞已判定為**我方 builder 地址**（讀法 A），官方另一頁有無歧義表述，並經 mainnet 實測反證讀法 B。**詳見第 8 節。** 原文保留於 git history。

3. **Q5 的「HL 大概率有處理」是演繹推測，不是證據**。前一份研究已標為最關鍵未驗證假設，本次仍未證實。若實測落在結果 B，績效顯示邏輯要重寫並引入第二個端點對帳，違反目前刻意維持的單一來源架構。

4. **比劃轉問題更根本的限制**：官方自承 portfolio graphs "not recommended for precise accounting purposes"，且 "interpolation between samples may not reflect the actual change in unrealized pnl"（15 分鐘取樣）。**即使劃轉語意查清楚了，perp 窗本身就不適合當精確績效來源**。對「績效數字正確性」這個動機而言，這條的殺傷力大於劃轉問題——劃轉是可校正的偏差，取樣間隔的資訊損失是不可回復的。

5. **官方 PnL 公式字面寫反**（`account value + deposits - withdrawals`，照字面實作會系統性做反）。這說明 HL 文件在此處不可全信，**強化了「所有績效語意都必須實測驗證，不得從文件演繹」的結論**。

---

## 來源

**SDK（hyperliquid-python-sdk 0.24.0，`.venv/lib/python3.14/site-packages/hyperliquid/`）**
- `exchange.py:59-63` 模式簡碼對照｜`:106` vaultAddress 排除｜`:135` user-signed 不支援 expires_after
- `exchange.py:474-491` `usd_class_transfer`｜`:492-515` `send_asset`
- `exchange.py:1147-1164` `agent_enable_dex_abstraction`｜`:1166-1183` `agent_set_abstraction`（L1）｜`:1186-1199` `user_dex_abstraction`｜`:1201-1216` `user_set_abstraction`（user-signed）
- `utils/signing.py:103` `USD_CLASS_TRANSFER_SIGN_TYPES`｜`:240` `sign_l1_action`｜`:247` `sign_user_signed_action`｜`:362-369` `sign_usd_class_transfer_action`｜`:458` `recover_agent_or_user_from_l1_action`｜`:467` `recover_user_from_user_signed_action`
- `utils/types.py:186-187` `Abstraction` / `AgentAbstraction`
- `info.py:86` `user_state`｜`:130` `spot_user_state`｜`:634` `query_user_abstraction_state`｜`:652` `user_non_funding_ledger_updates`｜`:671` `portfolio`
- SDK 官方 example（GitHub master）：`examples/user_abstraction.py`、`examples/basic_spot_to_perp.py`

**本 repo（`檔案:行號`）**
- `src/spark/exchange/base.py:1`（非託管 docstring）｜`:153,155`（main_signer）｜`:157,159,168,171,190`（agent_signer）
- `tests/test_base_types.py:26-29`（結構性斷言）
- `src/spark/exchange/hyperliquid.py:158,221`（portfolio 既有用法）｜`src/spark/copytrade/equity.py:1-14`（perp basis 決策）
- `docs/superpowers/research/2026-07-19-leader-performance-metrics.md:175`（Q5 前次標記）｜`2026-07-19-testnet-e2e-findings.md:33-54`（F1 實測）

**官方文件**
- [Account abstraction modes](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/account-abstraction-modes)（模式清單、builder standard mode、50k 上限、agent-/user-signed 並存）
- [Portfolio graphs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/portfolio-graphs)（PnL 定義、15 分鐘取樣、精確會計警語）
- [Exchange endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint)（usdClassTransfer 參數、user-signed 類別）
- [Nonces and API wallets](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets)（agent 定位；**未載明簽章限制**）
- [FAQ: Transfer or deposit to USDC (Perps) missing](https://hyperliquid.gitbook.io/hyperliquid-docs/support/faq/deposit-or-transfer-issues-missing-lost/transfer-or-deposit-to-usdc-perps-missing)
- [HyperCore Bridge](https://hyperliquid.gitbook.io/hyperliquid-docs/hypercore/bridge)｜[Portfolio margin](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/portfolio-margin)

**第三方（可信度較低，僅用於 schema 細節與交叉印證）**
- [Chainstack userNonFundingLedgerUpdates](https://docs.chainstack.com/reference/hyperliquid-info-user-non-funding-ledger-updates)（delta 型別表）｜[Chainstack Spot↔Perp transfer](https://docs.chainstack.com/reference/hyperliquid-exchange-spot-perp-transfer)
- [Dwellir userAbstraction](https://www.dwellir.com/docs/hyperliquid/user-abstraction)（`default` 值）｜[Privy agent wallets](https://docs.privy.io/recipes/hyperliquid/agents-and-subaccounts)
- [OneKey 入金指南](https://onekey.so/blog/ecosystem/complete-guide-to-hyperliquid-deposits-withdrawals-2026-fbe041/)｜[Eco bridge 指南](https://eco.com/support/en/articles/15191997-hyperliquid-bridge-deposit-usdc-and-cross-chain-routes-2026)

**標記為推測／未驗證**：perp 窗對 `accountClassTransfer` 的處理（Q5，未實測）｜agent key 實際被 HL 拒絕的錯誤訊息（Q3，未實打）｜builder standard-mode 要求的主詞（反面證據 2，文件歧義未消解）｜bridge deposit 落點為 perp（官方未明說 class，靠 FAQ 並列句與第三方一致性推得）

---

## 建議下一步（依重要性）

1. ~~**釐清 builder standard-mode 的主詞**~~ → **✅ 已完成（見第 8 節）**。結論：讀法 A，客戶模式不影響收入。改為新的第 1 順位：**把「我方 builder 地址 == standard mode」做成部署前斷言＋定期監控**（第 8 節第 5 題）。
2. **執行第 5 節實驗**（需 testnet 主鑰）——決定績效顯示邏輯要不要重寫。
3. **加唯讀偵測**：`spot_user_state` + `query_user_abstraction_state` → dashboard 提示「N USDC 卡在 Spot」。零風險、直接降低入金流失。
4. **寫入金教學**（第 6 節 5 條），主推 Arbitrum 原生 USDC 走官方 bridge。
5. （選配）testnet 用 agent key 實打 `usd_class_transfer` 取得拒絕訊息，把 Q3 從「四條間接證據」升級為「實跑輸出」。

---

## ⭐ 第 5 題已解決（2026-07-19，實測，使用者操作＋主對話查證）

**結論：內部劃轉（spot→perp）被正確當成入金扣除，`pnlHistory` 不受污染。現有單一來源架構可保留，不需要接第二個端點對帳。**

實驗：使用者在 Follower9760（`0xfB9C52…9760`）於 HL testnet UI 做一次 **100 USDC spot→perp 劃轉**；主對話以唯讀公開端點查 `portfolio()` 的 `perpDay` 窗，逐點比對。

| 時刻 (UTC) | AV | PnL | ΔAV | ΔPnL | 判定 |
|---|---|---|---|---|---|
| 07-18 19:14 | 999.00 | 0.000 | **+999.00** | +0.000 | 初始注資，未計入 PnL ✅ |
| 07-18 19:14 | 500.00 | 0.000 | **−499.00** | +0.000 | perp→spot 劃轉，未計入 ✅ |
| 07-18 21:32 | 499.19 | −0.809 | −0.81 | **−0.809** | 真實交易虧損，**有**計入 ✅ |
| 07-19 15:02 | 599.19 | −0.809 | **+100.00** | **+0.000** | ⭐ **本次劃轉，未計入 PnL** |

**對照組成立**是這次判讀可信的關鍵：同一序列裡「資金流動不計入」與「交易損益計入」兩種行為都出現了，所以不是「剛好都是 0」。

⚠️ 仍未驗證：反方向（perp→spot）在**有部位**時的行為、以及跨 `perpWeek`／`perpMonth` 窗的一致性。本次帳戶無部位無掛單。

---

# ⭐ 第 8 節：builder standard-mode 的主詞——**已判定（2026-07-19）**

**動機**：builder fee 是本產品唯一營收來源。若主詞是「客戶」，任何客戶自行切到 unified account 都會靜默斷我方營收。

## 結論先行

1. **主詞是「我方 builder 地址」（讀法 A）。** 官方 builder-codes 頁有一句**無語法歧義**的表述：「**The builder** must have at least 100 USDC in perps account value and must use standard as the account abstraction mode.」——主詞明寫 "The builder"，且與同段前文的 "the end user" 刻意分開。
2. **讀法 B 經 mainnet 實測直接證偽**：找到一個**目前處於 `unifiedAccount` 模式**的 mainnet 地址，其最近 2000 筆 fills **全部帶 `builderFee`**，最新一筆發生在查詢前 **4.5 分鐘**。客戶模式不影響 builder fee 收取。
3. **模式可唯讀查詢**：`POST /info {"type":"userAbstraction","user":"0x..."}`，回傳 `"default"｜"disabled"｜"unifiedAccount"｜"portfolioMargin"`。我方 testnet builder 與 follower 皆為 `"disabled"`（＝standard），**目前合規**。

**商業影響：原先列為「最大商業風險」的項目消失。** 營收模型不受客戶帳戶模式影響；需要控管的只有**我方自己的 builder 地址**。

---

## 第 1 題：那句話的主詞是誰？→ **我方 builder 地址**

### 證據 1（最強）：官方另一頁的無歧義表述

[Builder codes](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/builder-codes)（以 `.md` 取得原文），逐字：

> "In order to use builder codes, **the end user** would first approve a max fee for the builder address via the `ApproveBuilderFee` action. This action must be signed by the user's main wallet, not an agent/API wallet. **The builder** must have at least 100 USDC in perps account value and must use standard as the account abstraction mode."

**三點決定性**：
- 主詞明寫 **"The builder"**，不是 "the user"、不是 "addresses using builder codes"。
- 同一段前兩句的主詞是 **"the end user"**——作者在第三句**刻意換主詞**，這不是疏漏而是區分。
- 綁在同一句的另一條件是「**至少 100 USDC perps 帳戶淨值**」——這顯然是**對 builder 的資格門檻（反濫發）**，不可能是對每一位客戶的要求（否則小額客戶全部無法交易，與 HL 現實不符）。兩個條件共用一個主詞，主詞只能是 builder。

### 證據 2：account abstraction modes 頁的模式描述自洽

[Account abstraction modes](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/account-abstraction-modes) 逐字，模式 3：

> "Manual / Standard (recommended for market makers, high volume automated users, and **deployers/builders**): separate perp and spot balances, separate DEX balances."

官方把 standard 標為「**推薦給 builders**」——與「builder 必須用 standard」同向；若該要求是針對一般下單客戶，這句「推薦給 builders」就成了廢話。

### 證據 3：詞法——"Builder code address" 是「builder 的地址」

- 官方 builder-codes 頁：「`b` is **the address of the builder**」
- SDK `utils/types.py:184`（`.venv/lib/python3.14/site-packages/hyperliquid/`）逐字註解：`# b is the public address of the builder, f is the amount of the fee in tenths of basis points.`
- 官方 CSV 路徑參數名即 `{builder_address}`：`https://stats-data.hyperliquid.xyz/Mainnet/builder_fills/{builder_address}/{YYYYMMDD}.csv.lz4`

「builder code address」在 HL 術語體系中固定指**收費方地址**，不曾用來指下單者。原句 "Builder code addresses must be in standard mode" 即「登記為 builder code 的那些地址」。

### 證據 4：第三方一致（弱，僅交叉印證）

[Dwellir](https://www.dwellir.com/blog/hyperliquid-builder-codes)、[HypeBasis](https://hypebasis.io/builder-fees) 等轉述皆作「**the builder** must ... use standard as the account abstraction mode」；某開源 builder 範本文件把它寫成「`NEXT_PUBLIC_BUILDER_ADDRESS` ... **that account** must have minimum 100 USDC in Perps Available」——同樣指向 builder 自己的帳戶。

**判定信心：高。** 這不是「證據不足」——官方有無歧義表述，且下方有實測反證。**無需向 HL 官方提問。**

---

## 第 2 題：standard mode 的定義，與前一份研究的核對

**前一份研究的說法正確**，但需補一個第四值。官方 [Account abstraction modes](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/account-abstraction-modes) 完整四模式（逐字節錄）：

| # | 官方字面 | 語意 |
|---|---|---|
| 1 | **Unified account** (recommended for most users) | 每個資產單一餘額，spot 與 perp 統一為單一保證金池 |
| 2 | **Portfolio margin** (most capital efficient) | 全組合統一（HYPE / BTC / USDC / USDT） |
| 3 | **Manual / Standard** (recommended for market makers, high volume automated users, and deployers/builders) | perp 與 spot 餘額分離、各 DEX 餘額分離 |
| 4 | **DEX abstraction** (to be discontinued) | 另一條軸；官方已標示將淘汰 |

**「standard mode」＝ 官方模式 3「Manual / Standard」**，即**不啟用任何 abstraction** 的原始行為。

### API 值 ↔ 官方名稱對照（**注意有第四值 `default`，SDK 型別未涵蓋**）

SDK 寫入端型別 `utils/types.py:186`：`Abstraction = Literal["unifiedAccount", "portfolioMargin", "disabled"]`；wire 對照 `exchange.py:59-63`：`{"disabled": "i", "unifiedAccount": "u", "portfolioMargin": "p"}`。

但**唯讀端實測回傳值多一個 `"default"`**（本次實測，見下）：

| API 回傳 | 對應官方模式 | 說明 |
|---|---|---|
| `"disabled"` | Manual / Standard | abstraction 已明確關閉 ✅ builder 合規 |
| `"default"` | 推定 ≈ Standard（**未經官方確認**） | 從未設定過。實測全新地址即回此值；SDK example 註解「the account must be in "default" mode to succeed」佐證＝「未設定」 |
| `"unifiedAccount"` | Unified account | |
| `"portfolioMargin"` | Portfolio margin | |

⚠️ **`"default"` 是否等同 standard，官方文件未明說——標記為推定。** 對我方無實務影響：我方 builder 地址實測為 `"disabled"`（明確 standard），不落在這個灰區。**但若日後更換 mainnet builder 地址，建議明確設為 `disabled` 而非依賴 `default`**（見第 5 題）。

---

## 第 3 題：能不能查某地址的模式？→ **能，唯讀公開端點，無需金鑰**

**端點**：`POST https://api.hyperliquid.xyz/info`（testnet：`https://api.hyperliquid-testnet.xyz/info`）
**Body**：`{"type": "userAbstraction", "user": "0x..."}`
**SDK 封裝**：`info.py:634-635`（`.venv/lib/python3.14/site-packages/hyperliquid/info.py`）逐字：

```python
def query_user_abstraction_state(self, user: str) -> Any:
    return self.post("/info", {"type": "userAbstraction", "user": user})
```

**本 repo 尚未接線**：`grep -rn "abstraction" src/ scripts/ web/src/ tests/` → 零命中。

### 實測輸出（2026-07-19，真實執行）

我方 testnet 兩個地址（**驗收條件 3**）：

```
$ curl -s -X POST "https://api.hyperliquid-testnet.xyz/info" -H "Content-Type: application/json" \
    -d '{"type":"userAbstraction","user":"0xfB9C52f56F03D786AD5D435aa70fe45D80569760"}'
"disabled"

$ curl -s -X POST "https://api.hyperliquid-testnet.xyz/info" -H "Content-Type: application/json" \
    -d '{"type":"userAbstraction","user":"0xbAC652A5Fb611c1BdC3B9D244cc7E0cC03123662"}'
"disabled"
```

- Follower9760 `0xfB9C52…9760` → `"disabled"`（standard）
- **Leader＝Builder3662 `0xbAC652…3662` → `"disabled"`（standard）→ 我方 builder 地址合規** ✅

**端點確實能區分模式**（否則「偵測」是假的）——同一端點的對照輸出：

```
0x0000000000000000000000000000000000000001 (testnet, 全新)  => "default"
0xdfc24b077bc1425ad1dea75bcb6f8158e10df303 (mainnet, HLP)   => "default"
0x5078c2fbea2b2ad61bc840bc023e35fce56bedb6 (mainnet)        => "unifiedAccount"
```

100 個 mainnet 真實地址（取自 HLP vault `followers`）的模式分布實測：
`Counter({'unifiedAccount': 58, 'default': 41, 'disabled': 1})` ——三種值都出現，**判別力已證實**。

---

## 第 4 題：直接驗證 → **做到了，且直接證偽讀法 B**

### 為什麼不能用我方 testnet 資料回答

`docs/superpowers/research/2026-07-19-testnet-e2e-findings.md:72-77`（V2）記錄 builder fee 實收 0.02%。但本次實測顯示 **follower 當時就是 `"disabled"`（standard）**——該實驗**無法區分兩種讀法**（讀法 B 下 standard 客戶本來就該收得到）。**既有 testnet 資料對本題是無效證據**，不得引用為支持。

### 實際做的實驗：mainnet 反例搜尋

**設計**：若讀法 B 成立，則「處於 unifiedAccount 模式的地址」**不可能**有 `builderFee` > 0 的成交。找到一個反例即證偽。

**方法**：取 HLP vault `followers` 的 100 個真實 mainnet 地址 → 逐一查 `userAbstraction` → 再查 `userFills` 掃 `builderFee` 欄位。腳本：`scratchpad/probe.py`（暫存，未入 repo）。

**結果（真實輸出）**：

```
addresses: 100
mode distribution: Counter({'unifiedAccount': 58, 'default': 41, 'disabled': 1})

=== addresses WITH builder-fee fills, by mode ===
0x7bfee91193d9df2ac0bfe90191d40f23c773c060  mode=unifiedAccount   fills=2000  builderFee_fills=2000
0xd9b0a1568c93cea0ab03f1d3597ab1becaba400e  mode=unifiedAccount   fills=2000  builderFee_fills=6

summary (addresses with >=1 builderFee fill): {'unifiedAccount': 2}
total scanned: 95
```

**時序驗證（排除「先收費、後改模式」這個競爭解釋）**——對 `0x7bfee9…c060` 複查：

```
mode NOW: unifiedAccount
builderFee fills: 2000 of 2000
earliest: 2026-07-19 12:06:47 UTC
latest  : 2026-07-19 15:08:51 UTC
now     : 2026-07-19 15:13:21 UTC
latest builderFee fill: {"coin": "para:CRDO", "px": "199.69", "sz": "0.09",
                         "fee": "0.002142", "builderFee": "0.001797", "time": 1784473731185}
implied builder fee rate: 0.009999%
```

**判讀**：該地址**此刻**處於 `unifiedAccount`，而最近一筆帶 builder fee 的成交在 **4.5 分鐘前**；連續 3 小時、2000 筆成交**每一筆**都帶 builder fee，費率精確 0.01%（f=10）。「模式是在收費之後才切換的」這個解釋在 4.5 分鐘的間隔下不成立。

**⇒ 讀法 B 證偽。客戶處於 unified account 模式，builder fee 照收。**

**誠實標註（此實驗的極限）**：`userFills` 的 `builderFee` 證明**費用已向客戶收取**，嚴格說並未直接證明**該筆費用已入帳至 builder 的 `builderRewards`**。理論上仍存在「收了但沒撥給 builder」的殘餘可能。但 (a) 官方明文說 builder codes「processed entirely onchain as part of the fee logic」，(b) 官方把資格條件明確歸屬於 builder 而非 user，(c) 若成立則等同 HL 靜默侵吞——三者使該殘餘可能性極低。**若要 100% 閉合，唯一方法是我方 mainnet 上線後，拿一位 unifiedAccount 客戶的成交去對帳 `query_builder_accrued` 的增量**（`src/spark/exchange/hyperliquid.py:50-53`）。建議列為上線後第一週的對帳項，而非上線前的阻塞項。

---

## 第 5 題：緩解方案 → **讀法 B 不成立，防護重心移到「我方 builder 地址」**

原設想的「客戶側偵測＋提示」**不再是收入保護的必需品**。真正需要的是**我方單點的自我監控**——這反而更好，因為它是我們完全可控的單一物件。

### 建議 A（**應做，高價值低成本**）：builder 地址模式的部署前斷言＋定期監控

- **檢查**：`POST /info {"type":"userAbstraction","user":<我方 builder>}` 必須回 `"disabled"`（或經確認的 `"default"`）。
- **落點**：(i) `onboarding` / 啟動時的前置檢查一次；(ii) 既有日報 `scripts/copytrade_daily_report.py` 加一行。
- **失敗處理**：屬**安全關鍵**（直接斷營收）→ 依全域工程原則 3，**大聲告警**，不得 log 完吞掉。
- **可靠度：高**。單一地址、唯讀、確定性回傳、我方完全可控。
- **順帶一併檢查**：builder 地址 perps 帳戶淨值 **≥ 100 USDC**（官方同句要求，同樣是斷營收條件，且我方可能因提領而跌破——比模式更容易無聲觸發）。這條**目前完全沒有監控**，建議與模式檢查一起做。

### 建議 B（**應做，但動機改變**）：客戶側模式查詢仍值得接

不再為了保護 builder fee，而是為了**入金體驗**（第 6 節第 5 條）：`unifiedAccount`／`portfolioMargin` 的客戶**不需要** spot→perp 劃轉，可略過該教學步驟。
- **可靠度：高**（唯讀端點，已實測可區分）。
- **優先度：中**（體驗優化，非收入保護）。

### 建議 C（**上線後對帳**，閉合第 4 題的殘餘不確定性）

mainnet 首週：挑一位模式為 `unifiedAccount` 的實際客戶，比對其成交的 `builderFee` 總和 vs `query_builder_accrued` 增量。若一致 → 殘餘可能性歸零。
- **可靠度：高，但需要真實客戶流量**，故只能事後做。

### 不建議

- ❌ **強制或勸說客戶切換帳戶模式**。已證實無必要；且切換會改變客戶的風險模型（spot 資產暴露於 perp 爆倉風險），我方無權亦不應誘導。第 4 節「不要呼叫 `agent_set_abstraction`」的結論**維持不變**，且理由更強了——原本理由之一「與 builder fee 的 standard-mode 要求可能衝突」現已證明不存在，但其餘兩條（改變客戶風險模型、50k actions/day 上限）仍然成立。

---

## 第 8 節的反面證據（主動列出）

1. **`"default"` 的語意未經官方確認**（第 2 題）。若 `default` 實際上**不**算 standard，而我方某日新建的 mainnet builder 地址停在 `default`，則建議 A 的斷言若寫成「`disabled` 或 `default` 皆放行」就會漏抓。**緩解：斷言只放行 `"disabled"`，強制明確設定**——寧可多一次人工設定，不要賭語意。
2. **實測樣本有偏**：100 個地址取自 HLP vault followers，是「會存 vault 的活躍用戶」，非隨機。但這**不影響證偽邏輯**——證偽只需要一個反例，且反例本身（4.5 分鐘前、2000/2000 筆）品質極高。取樣偏差只影響「模式分布比例」的代表性，那個數字本報告不作結論用。
3. **`builderFee` 已收取 ≠ builder 已入帳**（第 4 題誠實標註）。這是本節唯一未完全閉合的環節，處置見建議 C。
4. **官方文件在他處已被證實會寫錯**（第 7 節反面證據 5：PnL 公式字面寫反）。因此本節**沒有只靠文件**——實測是獨立於文件的第二條腿，兩腿同向才下判定。
5. **時間點依賴**：HL 正在演進（模式 4 標示 "to be discontinued"、portfolio margin 為新功能）。本結論成立於 **2026-07-19**。若 HL 日後改變 builder fee 與模式的互動，建議 A 的監控會是**第一個發現的人**——這是把一次性研究結論轉成持續性保障的理由。

---

## 第 8 節來源

**官方文件**
- [Builder codes](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/builder-codes)（`.md` 版取得原文）——「The builder must have at least 100 USDC in perps account value and must use standard as the account abstraction mode.」＋「`b` is the address of the builder」＋ `{builder_address}` CSV 路徑
- [Account abstraction modes](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/account-abstraction-modes)——四模式全文、"recommended for ... deployers/builders"、"Builder code addresses must be in standard mode to accrue builder fees"

**SDK（hyperliquid-python-sdk 0.24.0，`.venv/lib/python3.14/site-packages/hyperliquid/`）**
- `info.py:634-635` `query_user_abstraction_state`｜`utils/types.py:184-187` `BuilderInfo` 註解與 `Abstraction`｜`exchange.py:59-63` wire 對照

**本 repo**
- `src/spark/exchange/hyperliquid.py:47-53`（`maxBuilderFee` / `query_builder_accrued`）｜`docs/superpowers/research/2026-07-19-testnet-e2e-findings.md:72-77`（V2，**本題無效證據**，理由見第 4 題）

**實測（2026-07-19，唯讀公開端點，無金鑰）**
- testnet `userAbstraction`：`0xfB9C52…9760` → `"disabled"`；`0xbAC652…3662` → `"disabled"`
- mainnet 100 地址掃描：`unifiedAccount` 58 / `default` 41 / `disabled` 1
- mainnet 反例：`0x7bfee91193d9df2ac0bfe90191d40f23c773c060`（`unifiedAccount`，2000/2000 筆帶 `builderFee`，最新距查詢 4.5 分鐘）

**第三方（僅交叉印證）**：[Dwellir builder codes](https://www.dwellir.com/blog/hyperliquid-builder-codes)｜[HypeBasis](https://hypebasis.io/builder-fees)

## ⭐ 有部位時的劃轉驗證（2026-07-21，端到端測試中補完）

**結論：即使帳戶有開倉部位、有移動中的浮動盈虧，內部劃轉仍被正確排除於 `pnlHistory` 之外。單一來源架構最終確認成立，不需接第二端點對帳。**

實驗（Builder3662，引擎暫停以免 follower 反應）：開 BTC 0.00075 部位 → 記錄有部位錨點 → perp→spot 劃轉 100 USDC → 量測。

| 時刻 | AV | PnL | ΔAV | ΔPnL |
|---|---|---|---|---|
| 劃轉前錨點 13:16 | 498.923 | −0.113 | — | — |
| **劃轉後 13:17:13** | 398.883 | −0.153 | **−100.15** | **−0.153**（非 −100） |

**兩個效應乾淨分離**：那 100 只進 AV、完全不進 PnL；同期部位浮動盈虧（−0.006→−0.040）如實反映在 PnL 的 −0.153。這比空倉版更強——證明「劃轉排除」與「浮動盈虧計入」兩條路徑互不干擾。

**併帶端到端里程碑**（M1 卡最久的驗證，首次在部署環境跑通）：activate 三閘全過｜引擎心跳實寫｜開/加/平生命週期每步 follower 精確 0.40× 鏡像｜平倉走 reduce-only｜builder fee 實收 0.0200%｜簽章授權換 leader 鏈路經使用者無意間簽署而驗證（過期正確不套用）｜零告警。
