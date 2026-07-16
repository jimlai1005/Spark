# modify 路徑的 builder fee 歸屬——靜態分析 + testnet 探針設計（T1.3）

**Date:** 2026-07-16
**Branch:** `feat/copytrade-m1`
**任務:** Task 5（spec T1.3）
**狀態: BLOCKED —— testnet 帳戶未注資，尚未實跑，僅完成靜態分析 + 探針腳本**

---

## 1. 問題

Spec `docs/superpowers/specs/2026-07-16-copytrade-orchestrator-m1-spec.md:44-45`（T1.3）：

> 引擎主動路徑是 modify-first——若 modify 後訂單丟失 builder 歸屬，主動路徑 fee 全在漏繳且帳面無感。testnet：place（帶 builder）→ modify → 成交 → `wait_for_accrual`（帶 baseline）驗增量。產出數據報告 + 政策選項，**交使用者，不得自行決定**。

同一份 spec 在 `:14` 把政策選項列為明確的「後擋板」：

> **後擋板（保留一項）**：modify 若丟失 builder 歸屬時的政策（容忍漏繳 vs 擋掉改強制 cancel+place）——等 T1.3 testnet 數據，**交使用者，不得自行決定**。

跟單引擎的主動路徑（`_reconcile_orders` 語意，spec `:50`）在「完全相符保留」以外的情況會走「同 slot modify 就地改」——也就是說，**正常運作下大多數改單都經過 `modify_order()`**，不是邊角案例。若 modify 丟失 builder 歸屬，漏繳的不是零星訂單，而是主動路徑的常態流量，且因為 fee 歸屬失敗不會讓下單本身失敗（訂單依然成交、策略依然運作正常），這是一種**帳面無感的漏繳**——不會觸發任何現有的錯誤路徑或告警，必須靠本探針主動偵測。

`src/spark/exchange/base.py:140-145`（`ExchangeAdapter.modify_order` 的 ABC docstring）已經記載了這個已知例外：

```python
def modify_order(self, agent_signer: Signer, oid: int, order: Order) -> bool:
    """改單。⭐ 已知例外：hyperliquid-python-sdk 0.24.0 的 modify_order() 簽章無 builder
    參數（結構限制，非本專案疏漏）——改單走 batchModify action，SDK 該層未接 builder
    欄位。故本方法**不帶** builder，紅線「order-creating writes 全帶 builder」在此不適用
    （改單本身不建立新訂單意圖，只調整既有掛單的價格/數量）。"""
```

`src/spark/exchange/hyperliquid.py:282-300`（`HyperliquidAdapter.modify_order` 實作）確認呼叫 `self._exchange.modify_order(...)` 時刻意不傳 `builder` kwarg——與 ABC docstring 一致，是有意識的設計，不是漏寫。但**這個 docstring 本身沒有回答歸屬是否真的丟失**，它只說明 SDK 簽章層面「無法在 modify 時重新指定 builder」。這正是本任務要補的空缺。

---

## 2. 靜態分析

### 2.1 `order` action（place 路徑）——builder 欄位存在

`.venv/lib/python3.14/site-packages/hyperliquid/exchange.py:140-162`（`Exchange.order`）呼叫 `bulk_orders(order_requests, builder)`；`bulk_orders`（`exchange.py:163-183`）把 `builder` 原樣傳給 `order_wires_to_order_action`。

`.venv/lib/python3.14/site-packages/hyperliquid/utils/signing.py:519-527`：

```python
def order_wires_to_order_action(order_wires: list[OrderWire], builder: Any = None, grouping: Grouping = "na") -> Any:
    action = {
        "type": "order",
        "orders": order_wires,
        "grouping": grouping,
    }
    if builder:
        action["builder"] = builder
    return action
```

`"order"` action 的頂層 payload **明確帶 `builder` 欄位**（`{"b": ..., "f": ...}`），這是 `place_order`／`market_open`／`close_reduce_only` 三個既有 adapter 方法都會用到的路徑，也是 `HyperliquidAdapter.place_order`（`src/spark/exchange/hyperliquid.py:259-265`）目前的實作方式。

### 2.2 `batchModify` action（modify 路徑）——builder 欄位不存在

`.venv/lib/python3.14/site-packages/hyperliquid/exchange.py:190-213`（`Exchange.modify_order`）：

```python
def modify_order(
    self, oid: OidOrCloid, name: str, is_buy: bool, sz: float, limit_px: float,
    order_type: OrderType, reduce_only: bool = False, cloid: Optional[Cloid] = None,
) -> Any:
    modify: ModifyRequest = {
        "oid": oid,
        "order": {
            "coin": name, "is_buy": is_buy, "sz": sz, "limit_px": limit_px,
            "order_type": order_type, "reduce_only": reduce_only, "cloid": cloid,
        },
    }
    return self.bulk_modify_orders_new([modify])
```

簽章上完全沒有 `builder` 參數——`ModifyRequest` 這個 TypedDict 本身就沒有 builder 欄位可填。

`exchange.py:215-228`（`bulk_modify_orders_new`）：

```python
def bulk_modify_orders_new(self, modify_requests: List[ModifyRequest]) -> Any:
    ...
    modify_wires = [
        {
            "oid": modify["oid"].to_raw() if isinstance(modify["oid"], Cloid) else modify["oid"],
            "order": order_request_to_order_wire(modify["order"], self.info.name_to_asset(modify["order"]["coin"])),
        }
        for modify in modify_requests
    ]
    modify_action = {
        "type": "batchModify",
        "modifies": modify_wires,
    }
    ...
```

`modify_action` 頂層只有 `"type"` 與 `"modifies"` 兩個鍵——**沒有任何位置可以放 builder**（對照 2.1 的 `"order"` action 明確有 `if builder: action["builder"] = builder`）。往下一層，`order_request_to_order_wire`（`signing.py:505-516`）把每筆 order 轉成 wire 格式：

```python
def order_request_to_order_wire(order: OrderRequest, asset: int) -> OrderWire:
    order_wire: OrderWire = {
        "a": asset, "b": order["is_buy"], "p": float_to_wire(order["limit_px"]),
        "s": float_to_wire(order["sz"]), "r": order["reduce_only"],
        "t": order_type_to_wire(order["order_type"]),
    }
    if "cloid" in order and order["cloid"] is not None:
        order_wire["c"] = order["cloid"].to_raw()
    return order_wire
```

單筆 wire 格式（`a`/`b`/`p`/`s`/`r`/`t`/`c`）同樣沒有 builder 欄位。**結論：`batchModify` action 的整條資料路徑（`ModifyRequest` → `modify_wires` → `modify_action` → 簽章 payload）在結構上完全不存在任何可以攜帶 builder 資訊的欄位**，不是 SDK wrapper 沒接、而是 L1 action 本身的 schema 沒有這個位置。

### 2.3 兩個假說——SDK 簽章證明不了歸屬語意

第 2.2 節只證明了一件事：**modify 動作本身無法在送出當下重新指定 builder**。它**不能**回答「Hyperliquid 交易所後端在改價後，是否仍把該訂單記在原始下單時綁定的 builder 名下」——這是撮合引擎/後端資料模型的行為，SDK 客戶端程式碼看不到，必須靠實測觀察後果（`query_referral_state` 的 `builderRewards` 累計是否隨改單後的成交而增加）。

兩個互斥假說：

- **假說 (a) 歸屬繼承**：交易所後端把 builder 綁定記在訂單本身（下單當下寫入訂單的內部狀態），`modify`（`batchModify`）只是改價/改量，不影響這個綁定——訂單成交時仍照原 builder 計費。預期：`Δ_modify ≈` 該筆成交的預期費。
- **假說 (b) 歸屬遺失**：`batchModify` 在協議層面被視為足夠接近「新訂單」的操作，後端在改單當下清空 builder 綁定（或者 builder 綁定本來就只在 `"order"` action 的簽章驗證路徑才會被寫入/續期，`batchModify` 完全繞過這條路徑）。預期：`Δ_modify ≈ 0`。

兩者都是自洽的實作假設，且都能解釋「SDK 0.24.0 modify_order() 無 builder 參數」這個表面現象——這正是為什麼靜態分析無法收斂到單一結論，必須用第 3 節的實驗來區分。

---

## 3. 實驗設計

腳本：`scripts/testnet_modify_probe.py`。流程摘要（細節見腳本內註解與 docstring）：

1. Keychain 取 main/agent signer；`onboard()` 冪等重用既有 agent（沿用 `scripts/run_testnet_flow.py` 的 `has_agent` 判斷）。
2. **對照組 A（place 路徑）**：`baseline_a = query_builder_accrued(builder)` → `orchestrator.place_marketable_order(...)` 下一筆帶 builder 的 IOC 成交單 → `wait_for_accrual(baseline=baseline_a)` → 記 `Δ_place`。這組已知會歸屬正確（走 `"order"` action，第 2.1 節已證明 builder 欄位存在），作用是驗證整條 baseline→下單→累計輪詢的量測機制本身是可信的（若連對照組都量不到累計，代表 builder 未正確 approve、或帳戶未注資、或量測邏輯有 bug——不是 modify 路徑的問題，必須先排除）。
3. **實驗組 B（modify 路徑）**：`baseline_b = query_builder_accrued(builder)` → `place_order` 下一筆遠端 GTC 掛單（帶 builder，`limit_px = mid × 0.7`，不會立即成交）→ 從回應 `raw["response"]["data"]["statuses"][0]["resting"]["oid"]` 取 `oid` → `modify_order(agent, oid, Order(同 coin/size, limit_px = mid × 1.05, tif="Ioc"))` 把它改成可立即成交 → 用 `get_user_fills` 輪詢確認該 `oid` 真的成交（見腳本 `_confirm_fill`：`modify_order()` 只回傳 bool，不足以區分「沒成交」與「成交但沒歸屬」）→ `wait_for_accrual(baseline=baseline_b)` → 記 `Δ_modify`。
4. 兩組各印出 `size` / `notional`（成交量 × 均價）/ `expected_fee`（`notional × f/100000`，`f=20` 時 `= notional × 0.0002`，對應 `f` 的定義「十分之一 bp」，即 `f=20 → 0.02%`，見 `src/spark/config.py:21` 的欄位註解）/ 實際增量 `Δ` / `ratio = Δ/expected_fee`。所有輸出只用 `Decimal` 數字與 `OrderResult`/`UserFill` 的公開欄位，不印私鑰、不印簽章物件。
5. 任一步驟失敗（下單被拒、`modify_order` 回 `False`、對照組 A 累計逾時）：印清楚的錯誤訊息與已完成步驟，`SystemExit` 非零碼退出，不吞例外。唯一的例外攔截點是實驗組 B 的 `wait_for_accrual` 逾時——這個逾時本身就是假說 (b) 的直接證據（不是腳本錯誤），攔截後把 `AccrualTimeout` 的訊息原樣印出（不是靜默吞掉），`Δ_modify` 記為 0，讓腳本能繼續印出完整的結構化比較，而不是在關鍵的那一步直接崩潰、拿不到任何數據。對照組 A 若逾時則不攔截，直接讓例外往外炸——因為對照組是已知會成功的路徑，逾時代表實驗前提（builder 已生效、帳戶已注資、accrual 查詢正常）本身有問題，必須先修好才能重跑，不該被腳本悄悄吸收。

### 判定準則

```
ratio_b = Δ_modify / expected_fee_B

ratio_b > 0.9   → 支持假說 (a)：modify 後訂單仍歸屬 builder（歸屬保留）
ratio_b < 0.1   → 支持假說 (b)：modify 後訂單 builder 歸屬遺失
0.1 ≤ ratio_b ≤ 0.9 → 異常區間，需人工深查（可能是部分成交、輪詢時序競態、
                       f/notional 算錯等，不能直接套用兩個假說中任一個）
```

腳本會自動印出這個分類（`testnet_modify_probe.py` 的 `=== 判定 ===` 區塊），但**這只是對「數據落在哪個假說」的描述**，不涉及第 4 節的政策選擇——兩者是不同層次的問題：前者是「歸屬有沒有丟」的事實判定，後者是「丟了要怎麼辦」的成本/效益取捨。

---

## 4. 政策選項（不下結論——等實測數據後由使用者裁決）

**只有在假說 (b)（歸屬遺失）成立時，以下取捨才有意義**；若假說 (a) 成立，modify-first 沒有 fee 缺口，維持現狀即可，不需要在這兩個選項間選。

### 選項一：容忍漏繳（維持 modify-first）

- **做法**：不改動——沿用 hl-copytrader 現狀的 `_reconcile_orders` 語意（「完全相符保留 → 同 slot modify 就地改」，spec `:50`），主動路徑繼續走 `modify_order()`。
- **優點**：保留 modify 的兩個結構性優勢——(1) 佇列位置（queue position）：改價不撤單，訂單在訂單簿上的排隊順序通常得以保留，重新 `cancel+place` 會排到隊尾；(2) 往返次數：一次 `modify` 呼叫 vs 兩次（`cancel` + `place`），對延遲與 rate limit 都更省。
- **代價**：**僅在假說 (b) 成立時存在**——主動路徑經 modify 撮合的訂單，builder fee 永久收不到，且沒有任何錯誤訊號（訂單正常成交，策略正常運作，唯一看得出來的地方是 `query_builder_accrued` 的累計成長速度低於「name notional × f」預期，需要額外監控才能發現）。

### 選項二：強制 cancel+place（`COPY_MODIFY_POLICY=cancel-place`）

- **做法**：新增一個環境變數／設定開關（例如 `COPY_MODIFY_POLICY=cancel-place`），主動路徑遇到「完全相符保留」以外、需要調整既有掛單的情況時，一律先 `cancel_order` 再 `place_order`（帶 builder），不呼叫 `modify_order`。一行 env 切換，不需要改動核心撮合邏輯，只需要在 `_reconcile_orders` 的「同 slot modify 就地改」分支加一個開關判斷。
- **優點**：fee 全保——`place_order` 一定帶 builder（第 2.1 節已證明 `"order"` action 有 builder 欄位），不存在歸屬疑慮。
- **代價**：多一次網路往返（撤單 + 下單，而非一次改單）；失去佇列位置（重新排隊，對高頻改價的策略可能實質影響成交機率/滑價）；`cancel` 與 `place` 之間有一段短暫的「裸缺口」（訂單完全不在簿上的時間窗），若剛好在這個窗口內行情大幅波動，可能錯過原本 modify 能立即捕捉到的成交機會，或反過來在裸缺口期間暴露非預期的無掛單風險（視策略的倉位管理邏輯而定）。

兩個選項的成本結構不同（選項一是持續性的、隱性的 fee 漏繳；選項二是一次性的、顯性的延遲/佇列成本），孰輕孰重取決於實際的 `Δ_modify/expected_fee` 比值有多低、以及主動路徑觸發 modify 的頻率有多高——這些都需要 testnet 實測數據，不是靜態分析能回答的，故本報告不下結論。

---

## 5. 狀態：BLOCKED——testnet 帳戶未注資

2026-07-16 查詢結果：兩個地址的 perp/spot 餘額皆為 0。

| 角色 | 地址 | 需求金額（testnet USDC） |
|---|---|---|
| Follower | `0x5579b5Ab953d59fc4d40fDA3199a13E91b680B5d` | ≥ 500 |
| Builder | `0x63e64A1c73b28Ca4FdFAC409DeE3e2BeE4B84847` | ≥ 100（`src/spark/config.py:13` `MIN_BUILDER_BALANCE`——低於此門檻 `onboard()` 的 `BuilderNotEligible` 檢查會直接拒絕，見 `src/spark/onboarding.py:56-61`） |

Follower 端門檻同時要滿足 `onboard()` 的 `InsufficientFunds` 檢查（`MIN_BUILDER_BALANCE` 同一常數，`src/spark/onboarding.py:51-54`）與腳本實際下單所需的保證金（M1 `order_size` 預設 `Decimal("0.01")` ETH，兩組共下兩筆單，含滑價緩衝，500 USDC 綽綽有餘）。

**注資後執行**（單行指令，地址已代入上表）：

```bash
SPARK_ACCOUNT_ID=<既有帳號 ID，需與 Keychain 已存的 main/agent key 對應> \
SPARK_USER_ADDR=0x5579b5Ab953d59fc4d40fDA3199a13E91b680B5d \
SPARK_BUILDER_ADDR=0x63e64A1c73b28Ca4FdFAC409DeE3e2BeE4B84847 \
SPARK_NETWORK=testnet \
uv run python -m scripts.testnet_modify_probe
```

跑出 `ratio_b` 後，回到第 3 節的判定準則分類，再回到第 4 節，由使用者在兩個政策選項間裁決。
