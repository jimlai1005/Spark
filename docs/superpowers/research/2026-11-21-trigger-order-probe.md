# 觸發單 testnet 探針（P1–P13）

> 狀態：**P1–P13 已於 2026-11-21 16:14–16:16 UTC 在 testnet 實跑（run 2）；T5 端到端 E1–E9 已於 17:52–18:16 UTC 實跑、九個情境全部 PASS（§6）**，結論已寫回 plan §4 的 D-P*。
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

## 6. T5 端到端（E1–E9）

> 狀態：**腳本已寫好、尚未實跑**（`scripts/testnet_trigger_e2e.py`，離線驗證：`--help`、純函式、
> 以記憶體內假交易所／假引擎跑過九個情境的判定邏輯；實際行為待主線程實跑）。下列表格的結果欄一律留白，
> 由實跑後填入，不得以離線驗證代替。Plan：`docs/superpowers/plans/2026-11-21-trigger-order-mirroring.md` §6 T5。

### 6.1 執行方式

兩個拋棄式 testnet 錢包（leader、follower），私鑰檔路徑必須含 `scratchpad`。follower 私鑰會被複製成
引擎 keystore 內的 `agent.key`（拋棄式錢包的主鑰直接當簽章者，**不測 agent 授權流程**），腳本結束時刪除。

```bash
export S=<scratchpad 目錄的絕對路徑>        # 例：/private/tmp/claude-501/.../scratchpad
OUT=~/filet-dev/trigger-e2e           # 必須在 repo 外
mkdir -p "$OUT"

# 1. 建兩個錢包（從水龍頭各注資 150 USDC；stdout 只印位址）
uv run python -m tests.integration.harness mint-wallet --usdc 150 --pk-file "$S/e2e-leader.pk"
uv run python -m tests.integration.harness mint-wallet --usdc 150 --pk-file "$S/e2e-follower.pk"

# 2. 兩個錢包各核准 builder fee（腳本啟動前置檢查會驗 maxBuilderFee ≥ 20）
export BUILDER=0xbAC652A5Fb611c1BdC3B9D244cc7E0cC03123662   # dev testnet builder（Builder3662）
uv run python - <<'PY'
import os
from pathlib import Path
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
S, B = Path(os.environ["S"]), os.environ["BUILDER"]
for name in ("e2e-leader.pk", "e2e-follower.pk"):
    a = Account.from_key((S / name).read_text().strip())
    print(a.address, Exchange(a, constants.TESTNET_API_URL).approve_builder_fee(B, "0.1%")["status"])
PY

# 3. 實跑（約 40 輪引擎＋E6／E9 各最多等 10 分鐘；全程約 30–60 分鐘）
uv run python -m scripts.testnet_trigger_e2e --leader-pk-file "$S/e2e-leader.pk" \
    --follower-pk-file "$S/e2e-follower.pk" --builder "$BUILDER" --out "$OUT/run1.jsonl"
# 只重跑部分情境：--only E1,E3 ；幣種／名目：--coin BTC --notional 60（範圍 20–100）
# E6／E9 追價起始距離：--chase-frac 0.001（預設 0.1%；範圍 0.0002–0.005）
```

- 結束碼：`0`＝所有被選情境 PASS、每個情境至少一筆成功的 leader 下單、清理後兩邊無倉位無掛單、keys 副本已刪；
  `2`＝前置檢查未過（零寫入）；`1`＝其他（任何 FAIL／UNVERIFIED／INCONCLUSIVE／清理不乾淨／私鑰副本未刪）。
- verdict：`PASS`／`FAIL`／`UNVERIFIED`（有 check 無法觀測）／`INCONCLUSIVE`（E6／E9 三次嘗試皆是 leader
  先觸發而 follower 尚未鏡射，實驗沒有測到東西）。只有 `PASS` 算通過。
- E6／E9 追價：觸發價從現價 `--chase-frac` 起，等 follower 鏡射後輪詢，最多等 10 分鐘，每 2 分鐘距離乘 0.6。
  「follower 已鏡射」的證據＝該步引擎 action 記錄有成功的 `place`（觸發價與型態相符）**或**快照有對應掛單
  （觸發後才拍的快照可能看不到已成交的單）。leader 先觸發而兩種證據都沒有＝無效嘗試，reset 後重來
  （起始距離乘 1.5，最多 3 次，每次的追價過程都留在記錄檔 `attempts`）。有效嘗試另查 follower 位址的
  `historicalOrders`／`userFillsByTime` 作「由交易所端觸發」的直接證據。
- 記錄檔：JSON lines。`kind=scenario` 一行一個情境（`leader_actions`〔含原始回應〕、`engine_rounds`〔結束碼、
  stdout／stderr 末段、引擎動作與告警〕、`snapshots`〔兩邊 `frontendOpenOrders` 原始 dict＋倉位〕、`checks`
  〔`{check, expected, actual, pass}`〕、`verdict`）；另有 `start`（前置檢查事實）、`reset`、`summary`。
  `pass` 為 `null`＝無法觀測（情境判 UNVERIFIED，不算通過）。
- 前綴「(前置)」＝實驗自身的前置條件；「(補強)」＝plan 預期欄沒有明寫、由腳本加上的佐證檢查。
- 引擎在 `--once` 子行程跑，**每輪都是新行程**：`ReconcileState`（含 D6 的 warn 去重集合）不跨輪保留，
  所以 E7 驗的是「單一行程內 warn 恰一則」，D6 的「跨輪不重發」只有離線單元測試覆蓋。
- 引擎告警／動作的觀測：腳本以 `_engine_child` 模式載入 `scripts.run_copytrade`，只把 `NullNotifier` 換成
  `RecordingNotifier`、`run_cycle` 換成會記 `ex.records` 的薄包裝，再呼叫原本的 `main(["--once"])`；
  引擎邏輯不改、不送 Telegram。引擎對 `COPY_RISK_CONTROLS_ENABLED=false`、`COPY_CAPITAL_UTILIZATION=0.9`
  （新錢包上線預設）以外取產品預設。

### 6.2 實跑紀錄

兩個拋棄式 testnet 錢包（`harness mint-wallet` 各注資 150 USDC、帳戶模式 `default`、事後 `sweep-wallet` 掃回並刪鑰）；
builder＝`0xbac6…3662`、`f=20`；引擎程式＝分支 `feat/trigger-order-mirroring`（run 1 在 `10737f9`、run 2 在 `9f293f3`，兩者 `src/` 的引擎邏輯相同）。

| run | 日期（UTC） | leader／follower 末四碼 | 幣種／名目 | `--only` | 記錄檔路徑 | 結束碼 | 最終驗證 | keys 已刪？ |
|---|---|---|---|---|---|---|---|---|
| 1 | 2026-11-21 17:52–18:00 | C4d0／f129 | BTC／$60 | 全部 | `~/filet-dev/trigger-e2e/run1.jsonl` | 1 | VERIFIED CLEAN | 是 |
| 2 | 2026-11-21 18:07–18:16 | C4d0／f129 | BTC／$60 | E6,E7,E9 | `~/filet-dev/trigger-e2e/run2.jsonl` | 0 | VERIFIED CLEAN | 是 |

run 1 的 E6／E7／E9 被判 FAIL，逐一查證後都不是引擎的問題（詳見 §6.5），腳本於 `9f293f3` 修正判定方式後以 run 2 重跑。
**九個情境的有效結果：E1–E5、E8 取 run 1；E6、E7、E9 取 run 2；全部 PASS。**

### 6.3 結果表（E1–E9）

| # | leader 動作 | 來源（記錄檔行號） | 時間（UTC） | checks（通過/失敗） | 結果 | 觀察到的 follower 行為 |
|---|---|---|---|---|---|---|
| E1 | 開倉後掛倉位止損＋止盈 | run1:3 | 17:52:30–54 | 10/0 | PASS | 兩張倉位型觸發單（Stop Market、Take Profit Market，`isPositionTpsl:true`），觸發價與 leader 相同 |
| E2 | 改止損觸發價 | run1:5 | 17:52:54–17:53:35 | 14/0 | PASS | 舊止損 oid 消失、新止損恰一張且 oid 不同（撤單重掛），止盈不動，無 CRIT |
| E3 | 撤倉位型、改掛固定數量止損限價 | run1:7 | 17:53:35–17:54:16 | 16/0 | PASS | 倉位型全消失；一張 Stop Limit（非倉位型、reduce-only），數量＝leader×倍率（0.9） |
| E4 | 連續 3 輪不動 | run1:9 | 17:54:16–17:55:02 | 18/0 | PASS | 三輪引擎零下單、零撤單、零改單，掛單 oid 集合不變 |
| E5 | 撤掉全部觸發單 | run1:11 | 17:55:02–41 | 13/0 | PASS | follower 觸發單全撤 |
| E6 | 止損觸發價靠近現價讓它觸發 | run2:3 | 18:07:51–18:10:49 | 11/0 | PASS | follower 的鏡射止損（oid 65744586204）在交易所端 `triggered`，平倉成交的 oid 就是該單；引擎沒有任何平倉動作；兩邊皆平、無殘留單、無 CRIT |
| E7 | 引擎 `COPY_MIRROR_TRIGGERS=false` 重跑 E1 | run2:5 | 18:10:49–18:11:13 | 10/0 | PASS | 不掛單；`orders` 類 `[未鏡射]` warn 恰一則；無 CRIT（另有一則 `killswitch` 類「風控已關閉」提醒，來自測試環境設定，與本功能無關） |
| E8 | 無倉位掛條件進場單；3 輪後撤 | run1:17 | 17:58:44–17:59:31 | 21/0 | PASS | 同型單（Stop Market 買、`reduceOnly:false`、0.00089＝leader 0.00099×0.9）；3 輪零動作；leader 撤後 follower 撤 |
| E9 | 條件進場單觸發價靠近現價讓它觸發 | run2:7 | 18:11:13–18:16:39 | 12/0 | PASS | follower 的鏡射進場單（oid 65744797592）在交易所端 `triggered`，開倉成交的 oid 就是該單；引擎沒有 `market_open`；follower 倉位 0.00089（＝期望值）、**沒有重複開倉**、無 CRIT |

E6／E9 追價過程：

| # | 嘗試 | 每步觸發價（等待累計秒） | 觸發／逾時 | follower 由誰平倉／開倉 | R5／R8 觀察 |
|---|---|---|---|---|---|
| E6（run 2） | 第 1 次即有效 | 60544（0）→ 60582（121） | 觸發 | 自己的止損單（交易所端） | 觸發後各輪引擎無動作；未見重掛（R5 未出現：市價型觸發後直接成交，沒有「已觸發未成交」的狀態可觀察） |
| E9（run 2） | 第 1 次即有效 | 60641（0）→ 60648（121）→ 60638（242） | 觸發 | 自己的進場單（交易所端） | 觸發後 3 輪引擎零動作；無重複開倉（R8 這次沒有發生，不代表不會發生——見 plan §7 與 T6 審核的 Warning） |

### 6.4 原始摘錄

```json
E6（run2.jsonl 行 3）交易所端證據
{"check": "follower 鏡射止損在 historicalOrders 有 status==triggered", "actual": {"oid": 65744586204, "triggerPx": "60582.0", "orderType": "Stop Market", "timestamp": 1795284622150}}
{"check": "follower 平倉成交的 oid 等於該止損 oid", "actual": {"oid": 65744586204, "dir": "Close Long", "sz": "0.00089", "px": "60569.0", "time": 1795284640363}}
引擎該步的 place 動作：{"is_buy": false, "sz": "0", "limit_px": "54524.0", "reduce_only": true, "trigger_px": "60582.0", "tpsl": "sl", "is_market": true, "is_position_tpsl": true, "ok": true, "status": "resting"}

E9（run2.jsonl 行 7）交易所端證據
{"check": "follower 鏡射條件進場單在 historicalOrders 有 status==triggered", "actual": {"oid": 65744797592, "triggerPx": "60638.0", "orderType": "Stop Market", "timestamp": 1795284953544}}
{"check": "follower 開倉成交的 oid 等於該條件進場單 oid", "actual": {"oid": 65744797592, "dir": "Open Long", "sz": "0.00089", "px": "60661.0", "time": 1795284976610}}
引擎該步的 place 動作：{"is_buy": true, "sz": "0.00089", "limit_px": "66702.0", "reduce_only": false, "trigger_px": "60638.0", "tpsl": "sl", "is_market": true, "is_position_tpsl": false, "ok": true, "status": "resting"}
```

run 1 的 E6 留下一個額外的交易所行為觀察（testnet `historicalOrders`，2026-11-21）：leader 的止損（oid 65744109132，觸發價 60587）17:58:10.103 掛上、
17:58:13.123 被觸發；follower 的鏡射止損（oid 65744111887）在 **17:58:14.148 才掛上**（此時 leader 的已觸發、觸發條件已成立），
17:58:16.683 被觸發並平倉。也就是說**觸發條件已成立的止損單掛上後約 2.5 秒會被觸發**（補上 §5 的 P10 未驗證項），
而這正是「引擎讀到 leader 的單之後、掛出鏡射單之前，leader 的單已觸發」這個競態的實例——這次結果正確（follower 被自己的止損平倉）。

### 6.5 資料極限與判讀備註

run 1 三個 FAIL 的查證（都以 testnet `historicalOrders`／`userFillsByTime` 與記錄檔的引擎 action 為證）：
- **E6**：實際上 follower 自己的止損單在交易所端觸發並平倉（見 §6.4 末段）；判 FAIL 是因為舊判定只看「觸發後才拍的快照」，那時兩邊的單都已觸發、看不到。
- **E7**：`[未鏡射]` warn 實際恰一則；舊判定把測試環境另一則 `killswitch` 類提醒也算進去。
- **E9**：**確實沒驗到**——leader 的進場單距現價 0.03%，掛上 50 毫秒就觸發，follower 尚未鏡射，是部位安全網用 `market_open` 補開的（無重複開倉）。
  run 2 把起始距離放寬到 0.1% 後，follower 先鏡射、再由自己的單觸發。

- testnet 行為不保證等同主網；兩個錢包帳戶模式預期為 `default`（非 unifiedAccount），前置檢查會把
  `abstraction` 記進 `start` 行，unifiedAccount 下的行為仍未測（見 §5）。
- 引擎簽章者是拋棄式錢包的主鑰（非 agent key）；agent 授權流程由 golive-regression 的 S2–S4 覆蓋，不在本章範圍。
- E6／E9 的觸發取決於 testnet 行情：10 分鐘內價格沒走到觸發價就記「未觸發」並判 FAIL，不代表功能有問題——可重跑。
- E7 的 warn 判定：category 為 `orders` 且含「[未鏡射]」的 warn 恰一則、且沒有其他 category 為 `orders` 的 warn；
  其他 category（例如 `killswitch` 的「風控已由錢包主人關閉」提醒）不影響判定，全部列在 check 的 `actual`。
- E3／E8 的數量判定以引擎該輪回報的倍率（`CycleReport.scale`）重算期望值（容差一個 szDecimals 步長），
  另以「follower 權益×0.9÷leader 權益」交叉驗證倍率（容差 10%）。
- 本章結果以記錄檔為準；若與上方表格不一致，以記錄檔為準並更正表格。
