# 觸發單 testnet 探針（P1–P13）

> 狀態：**P1–P13 已於 2026-11-21 16:14–16:16 UTC 在 testnet 實跑（run 2）**，結論已寫回 plan §4 的 D-P*。
> 本檔 §3 只陳述觀察；推論另標「推論」。
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
| 1 | 2026-11-21 16:12 | 3662 | BTC／0.00116 | 全部 | `~/filet-dev/trigger-probe/run1.jsonl` | 作廢：dev 錢包的 agent key 已失效，所有寫入回 `User or API Wallet … does not exist.`，零成交、零掛單（腳本仍回 exit 0 與 VERIFIED CLEAN——見 §5） |
| 2 | 2026-11-21 16:14–16:16 | 1657（拋棄式，`harness mint-wallet` 從水龍頭注資 150 USDC，事後 `sweep-wallet` 掃回並刪鑰） | BTC／0.00116（約 $70） | 全部 | `~/filet-dev/trigger-probe/run2.jsonl`（157 行） | VERIFIED CLEAN；27 筆下單全數被接受、0 筆 item_error |

## 3. 結果表（P1–P13）

全部出自 run 2。builder＝`0xbac6…3662`、`f=20`，每筆都帶。帳戶模式 `default`（非 unifiedAccount）。「行」＝記錄檔行號。

| # | variant | 接受？（`statuses` 原文） | 回讀 `frontendOpenOrders` | 觀察 |
|---|---|---|---|---|
| P1 | `sl_limit`（行 6）／`tp_limit` | 是。`[{"resting": {"oid": 65739864215}}]` | `orderType:"Stop Limit"`／`"Take Profit Limit"`、`sz=origSz="0.00116"`、`limitPx=triggerPx`、`isPositionTpsl:false`、`reduceOnly:true`、`tif:null` | 固定數量 reduce-only 觸發限價單（`grouping:"na"`）可帶 builder；成功形狀與限價單相同（`resting`＋oid）。 |
| P2 | `sl_market`／`tp_market` | 是。`[{"resting": {"oid": …}}]` | `orderType:"Stop Market"`／`"Take Profit Market"`；`limitPx` 回讀＝送出的 `p`（43267／64901），`triggerPx` 另列 | 市價型的 `p` 原樣保存為 `limitPx`。 |
| P3 | `s_zero`（行 24） | 是。`["waitingForTrigger"]`（**字串、無 oid**） | `sz:"0.0"`、`origSz:"0.0"`、`isPositionTpsl:true` | `grouping:"positionTpsl"`＋`s:"0"` 產生倉位型。 |
| P3 | `s_position`（行 27） | 是。`["waitingForTrigger"]` | `sz:"0.00116"`、`isPositionTpsl:false` | 同分組但 `s`＝倉位量 → 變成**固定數量**單，不是倉位型。 |
| P4 | `tp_single`／`tp_sl_pair` | 是。`["waitingForTrigger"]`／`["waitingForTrigger","waitingForTrigger"]` | 皆 `sz:"0.0"`、`isPositionTpsl:true` | 倉位型可帶 builder；一次送兩張亦可。 |
| P5 | `first_sl`／`second_sl_other_trigger` | 兩張皆是 | 第二張掛上後兩張並存（oid 65739891697 與 65739889579） | 同倉位可有多張倉位止損，交易所不取代、不拒絕。 |
| P6 | `sl_limit_2x_position` | 是。`resting` | `sz:"0.00232"`（倉位 0.00116 的 2 倍）原樣保留 | 未觸發的 reduce-only 觸發單不會被修剪到倉位量。 |
| P7 | `position_tpsl_sl`＋`fixed_size_ro_sl_limit` | 兩張皆是 | 市價平倉後 1 秒輪詢（行 64）：`open_orders: []` | 倉位歸零後，倉位型與固定數量 reduce-only 觸發單都在 1 秒內自動消失。 |
| P8 | `a_flat_ro_sell`／`b_long_ro_buy`／`c_short_ro_sell` | 三種皆是。`resting` | 皆正常回讀、`reduceOnly:true` | 無倉位、方向不符的 reduce-only 觸發單交易所**都不拒**。 |
| P9 | `plain_limit`（行 94）／`trigger_sl_limit`（行 95） | cancel 回應皆 `{"statuses": ["success"]}` | — | 以 oid 撤觸發單與撤限價單回應相同。 |
| P10 | `sl_buy_limit_crossed`／`sl_buy_market_crossed` | 兩張皆是。`resting` | 回讀時仍在簿上（`triggerCondition:"Price above 54101"`，當時 mid≈60,112） | 觸發條件當下已成立的單不被拒；掛上後 0.8 秒內未見觸發（隨即被探針撤掉，**更長時間的行為未測**）。 |
| P11 | `sl_limit_small`／`sl_market_small` | 兩張皆是。`resting` | `sz:"0.00008"`（約 $4.8） | 名目 < $10 的 reduce-only 觸發單掛得上（觸發後是否成交未測）。 |
| P12 | `normal_tpsl_parent_unfilled`（行 126） | 是。`[{"resting": {"oid": 65739942235}}, "waitingForFill", "waitingForFill"]` | 兩張子單**同時**出現在最上層（`isTrigger:true`、`reduceOnly:true`、有數量、`isPositionTpsl:false`）**與**母單的 `children` 陣列內 | 最上層看起來與已生效的固定數量 reduce-only 觸發單完全相同；唯一區別是它的 oid 出現在某張最上層單的 `children` 裡。只撤母單，3 秒後 `open_orders: []`（子單一併消失）。 |
| P13 | `sl_market_buy`（行 137）／`sl_limit_buy`／`tp_market_buy`／`tp_limit_buy` | 四種皆是。`[{"resting": {"oid": …}}]` | `reduceOnly:false`、`isPositionTpsl:false`；`orderType` 依 tpsl 與 isMarket 為四種字串 | 無倉位時可掛非 reduce-only 觸發單、可帶 builder。 |

### P13 保證金對照（`margin_before`／`margin_after`）

| variant | withdrawable 前 | 後 | totalMarginUsed 前 | 後 |
|---|---|---|---|---|
| `sl_market_buy` | 148.000417 | 148.000417 | 0.0 | 0.0 |
| `sl_limit_buy` | 148.000417 | 148.000417 | 0.0 | 0.0 |
| `tp_market_buy` | 148.000417 | 148.000417 | 0.0 | 0.0 |
| `tp_limit_buy` | 148.000417 | 148.000417 | 0.0 | 0.0 |

未觸發的條件進場單不佔保證金（帳戶模式 `default`；unifiedAccount 未測）。

## 4. 原始回應摘錄

### 送出的 action 與成功回應

```json
P1 sl_limit（行 6）
{"type": "order", "orders": [{"a": 3, "b": false, "p": "48043", "s": "0.00116", "r": true, "t": {"trigger": {"isMarket": false, "triggerPx": "48043", "tpsl": "sl"}}}], "grouping": "na", "builder": {"b": "0xbac652a5fb611c1bdc3b9d244cc7e0cc03123662", "f": 20}}
{"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 65739864215}}]}}}

P3 s_zero（行 24）
{"type": "order", "orders": [{"a": 3, "b": false, "p": "43265", "s": "0", "r": true, "t": {"trigger": {"isMarket": true, "triggerPx": "48072", "tpsl": "sl"}}}], "grouping": "positionTpsl", "builder": {"b": "0xbac652a5fb611c1bdc3b9d244cc7e0cc03123662", "f": 20}}
{"status": "ok", "response": {"type": "order", "data": {"statuses": ["waitingForTrigger"]}}}

P12（行 126）
{"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 65739942235}}, "waitingForFill", "waitingForFill"]}}}

P13 sl_market_buy（行 137）
{"type": "order", "orders": [{"a": 3, "b": true, "p": "79404", "s": "0.00116", "r": false, "t": {"trigger": {"isMarket": true, "triggerPx": "72185", "tpsl": "sl"}}}], "grouping": "na", "builder": {"b": "0xbac652a5fb611c1bdc3b9d244cc7e0cc03123662", "f": 20}}
{"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 65739948414}}]}}}
```

### P3 `frontendOpenOrders` 回讀

```json
s_zero：    {"oid": 65739876257, "side": "A", "sz": "0.0", "origSz": "0.0", "limitPx": "43265.0", "orderType": "Stop Market", "isTrigger": true, "triggerPx": "48072.0", "triggerCondition": "Price below 48072", "reduceOnly": true, "isPositionTpsl": true, "tif": null}
s_position：{"oid": 65739878912, "side": "A", "sz": "0.00116", "origSz": "0.00116", "limitPx": "43265.0", "orderType": "Stop Market", "isTrigger": true, "triggerPx": "48072.0", "triggerCondition": "Price below 48072", "reduceOnly": true, "isPositionTpsl": false, "tif": null}
```

### P12 母單（含 `children`；兩張子單另以相同內容出現在最上層）

```json
{"coin": "BTC", "side": "B", "limitPx": "42100.0", "sz": "0.00116", "oid": 65739942235, "timestamp": 1795277746920, "triggerCondition": "N/A", "isTrigger": false, "triggerPx": "0.0", "children": [{"coin": "BTC", "side": "A", "limitPx": "59541.0", "sz": "0.00116", "oid": 65739942236, "timestamp": 1795277746920, "triggerCondition": "Price above 66157", "isTrigger": true, "triggerPx": "66157.0", "children": [], "isPositionTpsl": false, "reduceOnly": true, "orderType": "Take Profit Market", "origSz": "0.00116", "tif": null, "cloid": null}, {"coin": "BTC", "side": "A", "limitPx": "32477.0", "sz": "0.00116", "oid": 65739942237, "timestamp": 1795277746920, "triggerCondition": "Price below 36086", "isTrigger": true, "triggerPx": "36086.0", "children": [], "isPositionTpsl": false, "reduceOnly": true, "orderType": "Stop Market", "origSz": "0.00116", "tif": null, "cloid": null}], "isPositionTpsl": false, "reduceOnly": false, "orderType": "Limit", "origSz": "0.00116", "tif": "Gtc", "cloid": null}
```

### 拒因字串

run 2 的 27 筆下單沒有任何一筆被交易所拒絕，因此沒有取得任何觸發單拒因字串。

## 5. 資料極限與未驗證項

- testnet 行為不保證等同主網（撮合、流動性、費率、限制可能不同）；本探針零主網寫入。
- 帳戶模式為 `default`；unifiedAccount／portfolioMargin 下的保證金與接受行為未測。
- 未測：觸發單真的被觸發後的行為（plan T5 的 E6／E9）；P10「條件已成立」的單在 0.8 秒之後會不會觸發；
  P11 小額單觸發後能否成交；母單**部分成交**時子單的狀態；倉位反向時觸發單的去留。
- **探針腳本的已知缺口**：run 1 所有寫入都失敗（agent key 失效），腳本仍以 exit 0 與 `VERIFIED CLEAN` 結束——
  「乾淨」只代表沒留下倉位與掛單，不代表有測到東西。判讀時必須另看 `placement` 筆數與 `item_error`。
- P5 的 note 欄 `first_oids`／`second_oids` 為空：倉位型的回應沒有 oid，腳本從回應抽不到；並存的證據是 `open_orders_after` 的兩個 oid。
