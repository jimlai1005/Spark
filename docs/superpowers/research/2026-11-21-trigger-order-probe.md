# 觸發單 testnet 探針（P1–P13）

> 狀態：**骨架，尚未實跑。** 結果欄與原始回應摘錄由主線程實跑 `scripts/testnet_trigger_probe.py` 後填入；
> 本檔不含任何推測結果。結論再由主線程寫回 plan §4 的 D-P*（未寫回前不得派 T3／T4）。
> Plan：`docs/superpowers/plans/2026-11-21-trigger-order-mirroring.md` §1、§6 T1。T5（E1–E9）結果日後另開章節於本檔。

## 1. 目的

Hyperliquid 官方文件沒寫清楚：`positionTpsl` 的數量欄該送什麼、觸發單成功回應的形狀、`builder` 能否併用、
`normalTpsl` 子單在 `frontendOpenOrders` 的呈現等（plan §1）。假設不是事實，單元測試的假資料照同一個假設寫，
測不出錯（工程原則 #1 欄位名教訓）。本探針在 testnet 實測，**只記錄原始回應，不替我們下結論**。

## 2. 執行方式

- 腳本：`scripts/testnet_trigger_probe.py`（僅限 testnet：base url 不含 `testnet` 直接 raise；單筆名目上限 $200；
  每筆下單帶 builder；私鑰與簽章不進任何輸出）。
- 前置：探測幣（預設 BTC）在該錢包上必須無倉位、無掛單，否則腳本零寫入退出。
- 完整指令與環境變數：

```bash
# 環境變數（沿用 scripts/testnet_leader_drive.py 慣例；以下為預設值，通常不必設）
#   FILET_KEYS_DIR=~/filet-dev/keys   SPARK_LEADER_ADDR=<探測錢包>   SPARK_LEADER_ACCOUNT_ID   SPARK_BUILDER_ADDR
#   FILET_KEYSTORE=envfile|keychain
uv run python -m scripts.testnet_trigger_probe --out <repo 外的路徑>/trigger-probe-<run>.jsonl
# 只重跑單項：--only P3,P5 ；探針倉位名目：--notional 70 ；探測幣：--coin BTC
# 加 --diagnostic-no-builder 會在 P4 多測一個不帶 builder 的 positionTpsl 對照（預設關）
```

- 記錄檔：JSON lines；`kind=placement` 一行一個下單嘗試（`request`、`grouping`、去簽章的 `action`、原始 `response`、
  `open_orders_after`＝該幣 `frontendOpenOrders` 完整原始 dict〔含母單 `children`〕、`target_orders`＝其中屬於本單 oid 的；
  P13 另有 `margin_before`／`margin_after`）；另有 `cancel`、`stance`、`poll`、`note`、`item_error`、`final_verification`。
- 實跑紀錄：

| run | 日期（UTC） | 錢包末四碼 | 幣種／探測倉位 | `--only` | 記錄檔路徑 | 最終驗證（VERIFIED CLEAN?） |
|---|---|---|---|---|---|---|
| 1 |  |  |  |  |  |  |

## 3. 結果表（P1–P13）

欄位說明：**variant**＝腳本記錄的 variant 名；**接受？**＝回應是否無 error（附 `statuses[0]` 確切形狀或拒因字串，逐字貼）；
**回讀欄位**＝`frontendOpenOrders` 回讀該單的 `orderType`／`sz`／`origSz`／`limitPx`／`triggerPx`／`isPositionTpsl`／`reduceOnly` 等；
**對應 D**＝此結果會決定 plan 哪一條裁決／T3／T4 哪個細節。空白＝尚未實跑。

| # | 要回答的問題 | variant | 接受？（statuses[0] 形狀／拒因原文） | 回讀欄位 | 結論（一句，僅陳述觀察） | 對應 D／task |
|---|---|---|---|---|---|---|
| P1 | reduce-only 止損限價（na＋builder）是否接受？statuses[0] 形狀？ | `sl_limit`／`tp_limit` |  |  |  | D-P1／T3 `_parse_order_response` |
| P2 | reduce-only 止損市價（isMarket，p＝觸發價 −10%）是否接受？回讀 limitPx？ | `sl_market`／`tp_market` |  |  |  | D8 |
| P3 | positionTpsl：(a) s=0、(b) s＝倉位量，各自是否接受？回讀 sz／origSz／isPositionTpsl？ | `s_zero`／`s_position` |  |  |  | D2、D-P3／T3 數量欄 |
| P4 | positionTpsl 併帶 builder 是否接受？（止盈型、止盈＋止損成對；選用：不帶 builder 對照） | `tp_single`／`tp_sl_pair`／`diagnostic_no_builder` |  |  |  | D12、紅線 4 |
| P5 | 已有倉位止損再掛第二張（不同觸發價）：並存／取代／被拒？ | `first_sl`／`second_sl_other_trigger` |  |  |  | T4 `_orders_match`、重掛行為 |
| P6 | 固定數量 reduce-only 觸發單數量 > 倉位量（2×）：接受？被修剪？ | `sl_limit_2x_position` |  |  |  | D7 |
| P7 | 市價平倉後，倉位型與固定數量 reduce-only 觸發單是否自動消失（輪詢 10 秒）？ | `position_tpsl_sl`／`fixed_size_ro_sl_limit` |  |  |  | D9 之外的殘留單處理 |
| P8 | 方向不符的 reduce-only 觸發單：接受或拒絕？拒因？ | `a_flat_ro_sell`／`b_long_ro_buy`／`c_short_ro_sell` |  |  |  | D9 |
| P9 | 以 oid 撤觸發單（`Exchange.cancel`）的回應形狀是否與限價單相同？ | `plain_limit`／`trigger_sl_limit` |  |  |  | T3 `cancel_order` 是否需改 |
| P10 | 觸發價已被越過（空倉的止損買單觸發價低於現價）：拒因字串？ | `sl_buy_limit_crossed`／`sl_buy_market_crossed` |  |  |  | D10 拒因呈現 |
| P11 | 名目 < $10 的固定數量 reduce-only 觸發單：接受或拒絕？ | `sl_limit_small`／`sl_market_small` |  |  |  | D11 |
| P12 | normalTpsl 母單未成交：子單在 `frontendOpenOrders` 是巢狀 `children`、還是也在最上層？可區分欄位？撤母單後子單是否一併消失？ | `normal_tpsl_parent_unfilled` |  |  |  | D14／T4 `_build_desired` 辨識式 |
| P13 | 非 reduce-only 條件進場（na＋builder、遠離現價、無倉位）：接受？statuses[0] 形狀？未觸發是否佔保證金（withdrawable 前後）？ | `sl_market_buy`／`sl_limit_buy`／`tp_market_buy`／`tp_limit_buy` |  |  |  | D3、D13 |

### P13 保證金對照（每個 variant 一列；數字出自記錄檔 `margin_before`／`margin_after`）

| variant | withdrawable 前 | withdrawable 後 | totalMarginUsed 前 | 後 | spot USDC hold 前 | 後 | 撤單後是否回復 |
|---|---|---|---|---|---|---|---|
| `sl_market_buy` |  |  |  |  |  |  |  |
| `sl_limit_buy` |  |  |  |  |  |  |  |
| `tp_market_buy` |  |  |  |  |  |  |  |
| `tp_limit_buy` |  |  |  |  |  |  |  |

帳戶模式（記錄檔 `start.account_abstraction`）：＿＿＿（unifiedAccount 的保證金從 spot `hold` 扣，withdrawable 可能恆為 0，需看 spot hold）。

## 4. 原始回應摘錄

每項貼 1–2 則最能代表形狀的原始 JSON（從記錄檔原樣複製，不改寫、不縮寫欄位）。標明記錄檔行號或 `ts` 以便回查。

### P1／P2 成功回應形狀（`statuses[0]`）

```json
（待填）
```

### P3 `frontendOpenOrders` 回讀（s=0 與 s=倉位量各一）

```json
（待填）
```

### P12 母單與子單（含 `children`；撤母單後的 open_orders）

```json
（待填）
```

### 拒因字串原文（P4／P6／P8／P10／P11 若有被拒）

| 項目／variant | 拒因原文 |
|---|---|
|  |  |

### P9 cancel 回應（普通限價 vs 觸發單）

```json
（待填）
```

## 5. 資料極限與未驗證項

- testnet 行為不保證等同主網（撮合、流動性、費率、限制可能不同）；本探針零主網寫入。
- 探針使用的錢包帳戶模式會影響 P13 的保證金讀值（見上）。
- 本檔只記錄「觀察到什麼」；每條對 D-P* 的推論由主線程另寫並標明依據的記錄檔行。
- 未覆蓋：觸發單真的被觸發後的行為（plan T5 的 E6／E9 才驗）、TWAP、spot。
