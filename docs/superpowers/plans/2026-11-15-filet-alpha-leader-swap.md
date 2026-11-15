# Filet Alpha 跟單位址更換（0xfB9C…9760 → 0xf97a…ddd1）

狀態：**已執行**（2026-11-15 03:05 UTC 安裝；使用者同日確認 plan、舊條目名稱與 R1／R2 兩個風險）。
執行記錄與驗證結果見 `deploy/RUNBOOK.md` 附錄 B「2026-11-15 白名單變更」。

## 0. 需求與裁決

需求（使用者 2026-11-15）：把 Filet 的跟單位址換成
`0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1`，首頁與各跟單頁面顯示的 Filet Alpha 都指向它。

| # | 裁決 | 來源 |
|---|---|---|
| D1 | **只換展示**：Filet Alpha 卡改指新位址，新客戶選到新位址；三個運行中的 follower 繼續跟舊位址，不搬、不撤銷、不產生交易 | 使用者 2026-11-15 |
| D2 | 名稱 `Filet Alpha`、slug `alpha`、tagline、首頁主推全部沿用；description 拿掉「自營訊號」，改「Filet 精選訊號（mainnet）。正常營運中，開放新客戶。」 | 使用者 2026-11-15 |
| D3 | 舊位址條目保留在白名單、`enabled: true`、`accepting_new: false`，名稱 `Filet Alpha (Legacy)`、slug `alpha-legacy` | 使用者 2026-11-15 確認 |
| D4 | **repo 程式碼不改**（三處寫死舊位址的預設值維持原樣，理由見 §4） | 使用者 2026-11-15 確認 |

## 1. 現況（2026-11-15 實測）

- 網站上的「Filet Alpha」不在程式碼裡。它是正式機
  `/opt/filet/spark/var/filet/leaders.json`（root:root 644）的一筆條目：
  位址 `0xfb9c52f56f03d786ad5d435aa70fe45d80569760`、slug `alpha`、`featured: true`。
  前端沒有寫死任何 slug、名稱或位址（`web/src/app/page.tsx:37-39` 以 `featured` 挑主推卡）。
- 運行中的 follower 三個：`f438b3…`（leader 來源＝客戶簽章）、`f6b3e7…`、`fd149b…`
  （來源＝名冊），心跳顯示三者都跟 `0xfb9c…9760`。`followers.json` 有三筆指向舊位址、零筆指向新位址。
- 新位址已在客戶自訂名單 `user_leaders.json`（`fb8c35…` 加入，該 follower 目前沒在跑）。
  加進精選白名單後，精選條目優先（`src/spark/filet/user_leaders.py:73` `merge_leaders`）。
- 舊位址目前 perp 權益 0、無部位，現貨 USDC 約 245。
- 新位址：perp accountValue 379,768、HYPE 多單名目 1,190,232；現貨 USDC total 910,542／hold 608,028；
  帳戶模式 `unifiedAccount`；最近成交 2026-11-14 21:43 UTC。

## 2. 要寫進正式機 `leaders.json` 的內容

`leaders` 陣列改成三筆（Ultron 一字不動）：

```json
{
  "address": "0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1",
  "name": "Filet Alpha",
  "description": "Filet 精選訊號（mainnet）。正常營運中，開放新客戶。",
  "enabled": true,
  "accepting_new": true,
  "slug": "alpha",
  "tagline": "多資產動能 · 永續合約",
  "tagline_en": "Multi-asset momentum · Perpetuals",
  "featured": true
},
{
  "address": "0xfb9c52f56f03d786ad5d435aa70fe45d80569760",
  "name": "Filet Alpha (Legacy)",
  "description": "Filet 自營訊號（mainnet）。已停止開放新客戶；既有跟單不受影響。",
  "enabled": true,
  "accepting_new": false,
  "slug": "alpha-legacy",
  "tagline": "多資產動能 · 永續合約",
  "tagline_en": "Multi-asset momentum · Perpetuals"
}
```


## 3. 上線後網站會變成什麼樣

| 位置 | 變化 |
|---|---|
| 首頁主推卡、`/strategies/alpha` | 指向新位址；錢包連結、績效數字全部換成新位址的 |
| `/strategies` 清單 | 多一張「Filet Alpha (Legacy)」，狀態「已暫停」（`accepting_new: false` 仍會列出，`src/spark/publicapi/app.py:2248`、`src/spark/filet/strategies.py:121-122`） |
| 三個既有 follower 的儀表板 | 策略名稱從「Filet Alpha」變成「Filet Alpha (Legacy)」（`app.py:730-737` 以位址對名稱） |
| 跟單人數 | Filet Alpha 卡變 0；Legacy 卡 3（`app.py:2224-2246` 以名冊的 leader 位址計數） |
| `fb8c35…` 的儀表板 | 策略名稱由預設字樣變成「Filet Alpha」 |

## 4. 不改的東西

- **repo 程式碼三處預設值**（`src/spark/copytrade/config.py:88`、`src/spark/filet/leaderboard.py:25`、
  `scripts/copytrade_daily_report.py:41`）維持舊位址：
  - 網站顯示完全不經過它們。
  - 日報 `scripts/copytrade_daily_report.py:144` 在正式機沒設 `COPY_LEADER_ADDRESS`，用的就是這個預設值；
    三個 follower 仍跟舊位址，改掉會讓日報拿錯的 leader 對帳。
  - `config.py` 的預設是「名冊沒指定 leader 時的回退」，改掉有機會讓未明確指定的 follower 靜默換 leader，違反 D1。
  - 等既有 follower 之後搬到新位址，再另開一輪一起改（含對應測試）。
- 推薦碼 `FILET`（推薦人錢包是舊位址）不動。
- `user_leaders.json`、`followers.json`、`leader_changes.json`、所有 follower env、所有 systemd unit 不動。不重啟任何服務。

## 5. 需要使用者知情的兩個風險

### R1 新位址的策略卡數字會很誇張
以現行公式（`compute_window_performance(rows, "allTime")` → `build_metrics`）對新位址預跑（2026-11-15）：

| 指標 | 新位址 | 舊位址（現在首頁顯示的） |
|---|---|---|
| 總報酬 | +3759.87% | −22.57% |
| 最大回撤 | −34.07% | −42.94% |
| Sharpe | 3.94 | −1.98 |
| 年化波動 | 637.79% | 109.35% |
| 單日最佳 | +290.02% | +19.91% |
| 涵蓋天數 | 885 | 136 |

單日 +290% 與年化波動 638% 很可能是早期小權益或入金落在同一區間造成的放大（同類問題見
memory `dashboard-twr-hidden-and-explore-subgrid`），不是真實單日績效。這些數字會直接出現在首頁。
本 plan 不修公式；是否接受由使用者決定。

### R2 新位址是 unified account，新 follower 的槓桿會高於 leader 的真實槓桿
引擎以 leader 的 perp accountValue 當權益（`src/spark/copytrade/loop.py:333`、`sizing.py:104`）。
unified account 的保證金放在現貨，perp accountValue 只反映部位保證金：

- 引擎看到的 leader 槓桿：1,190,232 ÷ 379,768 ≈ 3.13x
- 以 `(spot.total − spot.hold) + perp.accountValue`＝682,282 估算的槓桿 ≈ 1.74x（未計現貨 HYPE 約 6.6 萬）

新 follower 會以約 3.13x 鏡射，約為 leader 實際槓桿的 1.8 倍。`max_target_leverage` 預設 0＝不設上限
（`sizing.py:105`）。這是既有行為（`fb8c35…` 先前就是這樣跟），但掛上首頁主推後，新客戶完成綁定會被
auto-activate 自動以實盤啟用（專案紅線 5 的例外路徑）。本 plan 不修引擎；可選的緩解是另開一輪處理
（leader 權益 basis 依帳戶模式，或對此 leader 注入槓桿上限）。

## 6. 執行步驟（全部由主線程在正式機執行；需使用者明確同意才動）

### Task 1 @inline 前置檢查（唯讀）
1. `ls /var/www/maintenance/ON`、`ls -d /tmp/spark-sync`、`systemctl --failed`：確認沒有別的 session 在維護或部署。
2. `systemctl list-units "filet-follower@*"` 與各心跳檔：確認運行中的 follower 與其 leader 仍如 §1。
3. 重新 `cat leaders.json`，確認內容與 §1 相同（不同就停，回報使用者）。

### Task 2 @inline 產生候選檔並驗證（不動正式檔）
1. 在 `/tmp` 產生候選檔 `leaders.json.new`（內容＝§2）。
2. 用正式機的程式驗證候選檔（`cd /opt/filet/spark && sudo -u filet-api .venv/bin/python -c …`）：
   - `load_leaders(候選檔)` 不拋例外、筆數 3、slug 無重複。
   - `merge_leaders(候選, load_user_leaders(registry))` 之後：
     `is_still_permitted(0xfb9c…)` 為 True（三個 follower 不會被收尾）、
     `is_selectable(0xfb9c…)` 為 False、`is_selectable(0xf97a…)` 為 True、
     `is_still_permitted(0x45e7…)` 為 True。
   - 任一條不成立 → 停，不安裝。

### Task 3 @inline 安裝（唯一的寫入）
1. 備份：`sudo cp -p leaders.json leaders.json.bak-20261115-alpha-swap`。
2. 候選檔先複製到同目錄 `leaders.json.tmp`（root:root 644），再 `sudo mv` 覆蓋（同檔案系統 rename，
   引擎與 API 不會讀到寫一半的檔）。
3. `stat` 確認 root:root 644；`sudo -u filet-api test -w` 仍寫不到。

### Task 4 @inline 驗收（主線程親跑）
1. 本機 `curl https://trade.filet.app/api/public/strategies`：`alpha` → 新位址、`featured` true、`status` running；
   `alpha-legacy` → 舊位址、`status` paused；`ultron` 不變。
2. `curl …/api/public/strategies/alpha` 與 `…/alpha-legacy` 皆 200。
3. 首頁 HTML 或瀏覽器實看：主推卡的錢包連結是新位址。
4. 觀察至少 3 個引擎週期（約 3–5 分鐘）：三個 follower unit 仍 `active (running)`、心跳時間在推進、
   心跳 leader 仍是舊位址；`journalctl -u 'filet-follower@*' --since` 沒有撤銷／收尾／白名單載入失敗訊息。
5. RUNBOOK 的回歸檢查全綠。

### Task 5 @inline 記錄
- `deploy/RUNBOOK.md` 附錄 B 加一條 2026-11-15 記錄（改了什麼、備份檔名、回退指令）。
- 本檔狀態改為已執行；commit `docs:`。

## 7. 回退

`sudo cp -p leaders.json.bak-20261115-alpha-swap leaders.json.tmp && sudo mv leaders.json.tmp leaders.json`，
再跑一次 Task 4 的第 1、4 項。期間若已有新客戶選了新位址：回退後新位址只剩客戶自訂名單那一筆
（`enabled: true`），該 follower 繼續正常跟單，不會被收尾。
