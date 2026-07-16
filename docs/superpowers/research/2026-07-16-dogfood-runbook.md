# 主網 Dogfood Runbook（M1 驗收）

> 2026-07-16 Filet M1 — 跟單主錢包 `0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1`（hl-copytrader 同源）
> 資金規模 1000 USDC，新錢包獨立於線上部署和 gridbot；驗收 gate 見 spec §M1。

## 0. 前置：錢包與入金

### 0.1 新錢包生成 [人工]

生成一個**全新錢包**（拍板 #3：與線上部署、gridbot 錢包完全分離；用任何離線
錢包工具生成即可）。Keychain 帳戶命名建議 `filet-mainnet`。

- **Follower 錢包地址**（user_addr）：`___________________`
- **Agent Key**：稍後由 onboarding 自動生成並存入 Keychain，無需人工處理

### 0.2 匯入 Main Key [人工 + CLI]

把新錢包的主私鑰匯入 macOS Keychain（getpass 讀取，絕不 echo、絕不 log）：

```bash
uv run python -m scripts.bootstrap_keys filet-mainnet main
```

系統會提示貼上私鑰（不會顯示或印出）。驗證：
- ✅ 成功輸出：`已匯入 filet-mainnet:main 至 Keychain`

### 0.3 錢包注資 1000 USDC [人工]

從交易所或鏈上轉入 **1000 USDC** 至上述 follower 錢包地址（USDC 需橋入 Hyperliquid
——直接在 HL 介面用 Deposit，或經 Arbitrum 打入官方 bridge；到帳後確認出現在 perp 帳戶）。

- **入金方式**：___________________
- **交易哈希或確認碼**：___________________
- **入金時間**：___________________
- **驗證**：登入 Hyperliquid，確認 Wallet Balance 顯示 ≥1000 USDC

### 0.4 Builder 主網地址檢查 [人工]

確認 builder 主網地址的 Hyperliquid 帳戶權益 ≥100 USDC——這是 builder code 的啟用
門檻（spec §4；`MIN_BUILDER_BALANCE`，`src/spark/config.py:13`），低於此值 fills 不會
產生 builder fee，onboarding preflight 也會直接失敗。

```bash
# 一行查詢 builder 主網帳戶權益（get_account_value）
uv run python -c "
from hyperliquid.info import Info
from spark.exchange.hyperliquid import HyperliquidAdapter
a = HyperliquidAdapter('mainnet', info=Info('https://api.hyperliquid.xyz', skip_ws=True))
print(a.get_account_value('0x_____________________'))  # 填 builder 地址
"
```

> 註：onboarding 本身已內建這道 preflight（`src/spark/onboarding.py:52-60`）——
> user 地址與 builder 地址任一 < 100 USDC 會直接失敗並報明確錯誤，不會走到簽章步驟。
> 此處先手動查一次是為了避免走到 §1 才發現要回頭補資金。

- **Builder Address**：`___________________`
- **當前餘額（USDC）**：`___________________` ✅ (需 ≥ 100)

---

## 1. Onboarding 階段（Testnet 驗證過的流程搬主網）

此步之後 **agent key 已可下單** ——但 `COPY_LIVE_TRADING` 仍是 false，**不會有任何自動交易**。

### 1.1 設定環境變數 [人工]

在終端設定以下環境變數（或寫入 repo 外的 `~/.spark_mainnet.env` 後 `source`——
不要放進工作樹，避免與紅線 2 的 `.env*` 慣例混淆）：

```bash
export SPARK_NETWORK=mainnet
export SPARK_ACCOUNT_ID=filet-mainnet
export SPARK_USER_ADDR=0x_______________________  # §0.1 的 follower 錢包地址
export SPARK_BUILDER_ADDR=0x_____________________  # builder 主網地址
```

### 1.2 執行 Onboarding [自動；含一筆真實驗證單]

```bash
uv run python -m scripts.run_testnet_flow
```

> 注意：此腳本除 onboarding（approve builder fee + approve agent）外，會**真實下一筆
> 小額 marketable 單**（預設 0.01 ETH，`Settings.order_size`）並等待 builder accrual
> 增量——這是主網 place 路徑的驗證，是人工執行的一次性動作，不屬於自動交易。

**預期輸出**：
- 若 agent key 已存在（上次 onboard 遺留）：
  ```
  onboarding OK（agent 沿用既有）
  order filled_size=... avg_px=...
  ✅ 累計 builder fee ... → ...（增量 ...）
  ```
- 若 agent key 首次生成：
  ```
  onboarding OK（agent 新生成並已入 Keychain）
  order filled_size=... avg_px=...
  ✅ 累計 builder fee ... → ...（增量 ...）
  ```

**檢查點**：
- ✅ Keychain 中已存在 `filet-mainnet:agent`
- ✅ Builder fee 有增長（數字>0 代表 place 路徑已驗證）
- ✅ 沒有異常日誌或私鑰洩露

---

## 2. Shadow 觀察期（≥3 交易日）

此期間 `COPY_LIVE_TRADING=false`，引擎以 dry-run 模式對 leader `0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1` 產出意圖動作清單，與 hl-copytrader 線上日誌逐輪對比。

### 2.1 啟動 Shadow 模式 [自動]

```bash
SPARK_NETWORK=mainnet SPARK_ACCOUNT_ID=filet-mainnet \
SPARK_USER_ADDR=0x_______________________ \
SPARK_BUILDER_ADDR=0x_____________________ \
COPY_LEADER_ADDRESS=0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1 \
COPY_LIVE_TRADING=false \
uv run python -m scripts.run_copytrade --shadow
```

> **待交付**（Task 12）：`scripts.run_copytrade` CLI 工具與 `--shadow` 旗標實裝。
> 上述指令樣板為規格預期；實際 CLI 請以 Task 12 交付為準。

**預期行為**：
- 每分鐘檢查一次 leader 變動
- Dry-run：計算意圖動作（place/modify/cancel）但不執行
- 日誌記錄每輪對比結果（差異分類為「可解釋」或「不可解釋」）

### 2.2 hl-copytrader 日誌對比 [人工]

每日查看 hl-copytrader 線上日誌，與 spark shadow 輸出逐項對比：

```
可解釋差異範例：
- 權益快照時間差（1 分鐘內允許）
- 參數不同（capital_utilization / position_weight）
- 容忍度內的量級差異（size_tolerance = 2%）

不可解釋差異（紅燈，需停止）：
- 邏輯選擇不同（該 place 卻 modify、該 cancel 卻沒動作）
- 訂單方向反向
- 量級差異超容忍度
```

### 2.3 Shadow 驗收門檻 [人工判定]

**條件**：連續 3 個交易日 **不可解釋差異 = 0**

- 第一個交易日：日期 ___________，結果 ✅ / ❌
- 第二個交易日：日期 ___________，結果 ✅ / ❌
- 第三個交易日：日期 ___________，結果 ✅ / ❌

✅ **若三天皆通過**，進入第 3 階段（LIVE 開啟）。
❌ **若有任何一天出現不可解釋差異**，停止、回報問題、修復後重新計天。

---

## 3. LIVE 開啟（人工決策點）

### ⚠️ **開啟後引擎會自動動用真實資金下單——這是整份 runbook 唯一由人工做 GO 決策的步驟** ⚠️

**`COPY_LIVE_TRADING=true` 的明確語意**：引擎的每一輪同步（預設每 60 秒）將真實送出
place / modify / cancel / reduce-only close 到主網，不再只是 dry-run 記錄意圖。
這是 CLAUDE.md 紅線 5 所指的「人工決策」——不得由任何自動流程設定此變數。

### 3.1 決策 Checklist [人工]

開啟前必須確認以下所有項目：

- [ ] **Testnet E2E 通過**：T4.2 onboard → 下單 → `wait_for_accrual`，place 與 modify 兩路徑皆驗過
- [ ] **Shadow 對照通過**：連續 3 交易日不可解釋差異 = 0（見 §2.3）
- [ ] **T1.3 政策已裁決**：modify 喪失 builder 歸屬時的行為（modify-first vs cancel-place）
      已由使用者根據 `scripts/testnet_modify_probe.py` 的數據裁決（該探針僅限 testnet，
      對主網執行會被拒絕）；裁決結果反映在 `COPY_MODIFY_POLICY`
- [ ] **Kill switch 演練通過**：已在 testnet 環境驗證 flatten 邏輯、告警升級、re-arm（主網實彈演練見 §5）
- [ ] **Telegram 通知已接**：`COPY_TG_BOT_TOKEN` 與 `COPY_TG_CHAT_ID` 已設定
      （`TelegramNotifier.from_env`，見附錄 C）；測試一條 critical 消息確實收到
- [ ] **資金與風控確認**：
  - 主錢包 balance ≥ 1000 USDC（未被他人挪用）
  - `COPY_MAX_DRAWDOWN_PCT=0.20`（預設；若擬調整需明確說明理由）
  - `COPY_FLATTEN_ON_BREACH=true`（回撤自動全平）

### 3.2 啟動 LIVE 模式 [人工 + CLI]

```bash
SPARK_NETWORK=mainnet SPARK_ACCOUNT_ID=filet-mainnet \
SPARK_USER_ADDR=0x_______________________ \
SPARK_BUILDER_ADDR=0x_____________________ \
COPY_LEADER_ADDRESS=0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1 \
COPY_LIVE_TRADING=true \
uv run python -m scripts.run_copytrade
```

> **待交付**（Task 12）：`scripts.run_copytrade` CLI 工具。

**立即檢查**：
- ✅ 日誌顯示 `live_trading=true`（而非 dry_run/shadow）
- ✅ 第一筆訂單已下達（Hyperliquid 檢查 open orders / orders history）
- ✅ Telegram 收到 info 級啟動通知（格式 `[INFO] <category> | ...`；措辭以 Task 12 實作為準）

### 3.3 運行記錄 [人工]

- **LIVE 開啟時間**：___________ （UTC）
- **初始 balance**：___________ USDC
- **備註**：___________________

---

## 4. 日常觀察清單（每日）

### 4.1 每日啟動與檢查 [自動啟動 + 人工確認]

保持引擎持續運轉（推薦使用 `systemd` 或 `tmux`）。每日檢查點如下：

#### a) Sync 失敗告警 [查看日誌]

```bash
# 查最新 N 行日誌，搜尋 "sync_failed" 或 "reconcile_error"
tail -100 <copytrade.log> | grep -E "(sync_failed|reconcile_error|CRIT)"
```

**門檻**：連續監控期間內 `sync_failed` 告警計數 = 0
- ✅ 通過：每日日誌內無此告警
- ❌ 觸發：單次出現 → 檢查網絡、重試一次；持續出現 → 停止並回報

#### b) Taker Share（吃價占比）[查詢日報]

```bash
# 日報腳本（scripts/copytrade_daily_report.py）：報告當日 UTC 0點~now，不吃日期參數
SPARK_NETWORK=mainnet \
SPARK_USER_ADDR=0x_______________________ \
SPARK_BUILDER_ADDR=0x_____________________ \
uv run python -m scripts.copytrade_daily_report
```

> 報告同時寫入 `var/copytrade/reports/YYYY-MM-DD.md` 並印 stdout；內容含 safety net
> 占比、滑價 bp、skipped-small 占比與 accrued 增量（快照存 `var/copytrade/accrued_snapshot.json`）。
> Telegram 推送與 skipped 資料寫入屬主迴圈整合（Task 12/16 交付）。

**門檻**：< 30% 總成交量
- ✅ 通過：報告顯示 `taker_share < 30%`
- ⚠️  邊界：20–29% 可接受（正常波動）
- ❌ 觸發：> 30% → 檢查 leader 交易量、leverage 設置；可能需調整 `capital_utilization`

#### c) Accrued 日增量 [查詢日報]

同上日報腳本輸出（`accrued` 相對昨日快照的增量）。

**門檻**：> 0（每日應有正淨累計）
- ✅ 通過：`accrual_delta > 0` USDC
- ❌ 觸發：= 0 或 < 0 → 檢查是否所有成交都記錄了 builder fee；可能需 re-verify modify 路徑

#### d) 對帳檢查 [隔日 CSV 對帳]

每日營業日結束後（或次日），執行隔日對帳：

```bash
SPARK_NETWORK=mainnet SPARK_BUILDER_ADDR=0x_____________________ \
SPARK_ACCOUNT_ID=filet-mainnet \
uv run python -m scripts.reconcile_day $(date -v-1d +%Y%m%d)  # macOS；Linux 用 date -d yesterday
```

**預期**：輸出含 `matched: True  fills: N`，exit code 0。

**門檻**：drift = 0（帳目完全相符）
- ✅ 通過：`matched: True`（exit 0）
- ❌ 觸發：`matched: False`（exit 1）→ 停止交易、核對 builder fills CSV 與 accrual，必要時人工對帳

### 4.2 日誌摘要表（一週範本）

| 日期       | sync_failed | taker_share | accrued_delta | reconcile | 備註 |
|-----------|-------------|-------------|--------------|-----------|------|
| 2026-07-__ | 0           | 18%         | +12.34 USDC  | ✅        |      |
| 2026-07-__ | 0           | 22%         | +15.62 USDC  | ✅        |      |
| 2026-07-__ | 0           | 19%         | +10.89 USDC  | ✅        |      |
| ...        | ...         | ...         | ...          | ...       | ...  |

---

## 5. Kill Switch 實彈演練

### ⚠️ **此步會故意觸發緊急停止邏輯——請在非交易時段執行** ⚠️

目標：驗證回撤超限時的自動全平與告警機制。

### 5.1 基準記錄 [人工]

在執行測試前記錄：

- **當前時間**：___________ （UTC）
- **當前 balance**：___________ USDC
- **當前最高點權益**（high water mark）：___________ USDC
- **容許回撤（COPY_MAX_DRAWDOWN_PCT）**：預設 0.20（即 20%）

### 5.2 故意觸發回撤告警 [自動 + 人工監控]

執行一個臨時配置，將 `COPY_MAX_DRAWDOWN_PCT` 降低到當前回撤已觸發的水位：

**計算目標 MDD**：

若當前權益 E，高水位 H，已回撤 (H-E)/H = D%，選擇 `COPY_MAX_DRAWDOWN_PCT=（D-2）%` 來故意觸發。

例：若 H=1000 USDC，E=950 USDC，D=5%，則設 `COPY_MAX_DRAWDOWN_PCT=0.03`（3%）。

> 邊界：config 要求 `0 < max_drawdown_pct < 1`，不能設 0 或負值。若當前 D≈0（幾乎無
> 回撤），先讓引擎正常跑到出現少量自然回撤（手續費即會造成），再挑 D 之下的值觸發。

```bash
# 啟動引擎，但覆蓋 MDD 設置
COPY_MAX_DRAWDOWN_PCT=0.03 \
SPARK_NETWORK=mainnet SPARK_ACCOUNT_ID=filet-mainnet \
SPARK_USER_ADDR=0x_______________________ \
SPARK_BUILDER_ADDR=0x_____________________ \
COPY_LIVE_TRADING=true \
uv run python -m scripts.run_copytrade --once
```

> `--once` 為 spec T2.3 規格的單輪執行旗標；`scripts.run_copytrade` 屬 Task 12 交付，
> 指令樣板以實際 CLI 為準。若 Task 12 未交付而需先演練 trip 動作，可用
> `scripts.panic`（同一套 killswitch.trip，dry-run 先看動作清單）。

**預期行為**（`src/spark/copytrade/killswitch.py` 的 `trip`，順序是紅線不得重排）：
1. 檢測到 drawdown > 0.03（`check_drawdown`）→ 觸發 trip
2. **自動 1**：撤銷全部 resting orders
3. **自動 2**：reduce-only 全平所有持倉
4. **自動 3**：寫入 `var/copytrade/killswitch.tripped`（ARM 檔，含時間戳＋狀態＋失敗清單；
   **部分失敗也會寫**——鎖死交易優先）
5. **自動 4**：發送 critical 級 Telegram 通知（格式 `[CRIT] killswitch | ...`，
   前綴見 notifier.py `_LEVEL_PREFIXES`）

之後每輪 cycle 開頭 `is_tripped` 為 True → 只讀報狀態，不再交易，直到人工 re-arm。

### 5.3 驗收 Kill Switch [人工檢查]

執行後檢查以下項目：

- [ ] **持倉已清零**：
  ```bash
  uv run python -c "
  from hyperliquid.info import Info
  from spark.exchange.hyperliquid import HyperliquidAdapter
  a = HyperliquidAdapter('mainnet', info=Info('https://api.hyperliquid.xyz', skip_ws=True))
  print(a.get_positions('0x_______________________'))  # 填 SPARK_USER_ADDR
  print(a.get_open_orders('0x_______________________'))
  "
  ```
  應輸出：`[]`（無持倉）與 `[]`（無掛單）

- [ ] **ARM 檔已建立**：
  ```bash
  ls -la var/copytrade/killswitch.tripped
  ```
  應顯示：檔案存在

- [ ] **日誌記錄完整**：
  ```bash
  tail -20 <copytrade.log> | grep -E "(CRIT|flatten|tripped)"
  ```
  應包含 `[CRIT]` 及 flatten 動作記錄

- [ ] **Telegram 通知已收**：
  應收到 `[CRIT] killswitch | ...` 開頭的總結消息（含回撤數字與各步結果）

### 5.4 故意失敗場景：斷網驗證告警升級 [人工模擬]

此步驗證 flatten 失敗時的告警升級機制。

**操作**：在 trip 執行 reduce-only 全平期間故意中斷網絡（macOS：`sudo ifconfig en0 down`，
另一終端執行；或在測試環境 mock adapter 拋例外）。

**實際語意**（`killswitch.py` trip 步驟 2）：resilience 層的重試在 adapter 內已耗盡，
trip 這一層**不再重試**（絕不靜默重試是刻意設計）；單一 coin 平倉失敗會：
1. 記入 `failures` 清單，**繼續平下一個 coin**（一個失敗不擋其他部位）
2. 逐 coin 發 critical：`[CRIT] killswitch | 平倉失敗 <coin> size=... ——殘留暴險，需人工處置`
3. ARM 檔照寫（**部分失敗也寫**——鎖死交易優先於完美平倉），`failures` 欄位記錄殘留
4. 總結 critical：`KILL SWITCH TRIPPED：...｜平倉失敗 [<coin>...]｜已鎖死 ...`

**驗收**：
- [ ] 收到逐 coin 的平倉失敗 critical 通知（不是靜默）
- [ ] `var/copytrade/killswitch.tripped` 內 `failures` 欄位含失敗的 coin
- [ ] 恢復網絡後，殘留部位以 `scripts.panic --yes` 或手動平掉（人工處置，引擎不自動補平）

### 5.5 Re-arm 程序 [人工]

驗證完成後，重新啟用引擎：

```bash
# 1. 刪除 ARM 檔案（重置 kill-switch 狀態）
rm var/copytrade/killswitch.tripped

# 2. 確認持倉已清（或人工補倉回到預期狀態）
# [人工檢查] ...

# 3. 重啟引擎（回到正常 COPY_LIVE_TRADING=true 配置）
SPARK_NETWORK=mainnet SPARK_ACCOUNT_ID=filet-mainnet \
SPARK_USER_ADDR=0x_______________________ \
SPARK_BUILDER_ADDR=0x_____________________ \
COPY_LIVE_TRADING=true \
uv run python -m scripts.run_copytrade
```

- [ ] Re-arm 時間：___________
- [ ] 新訂單已正常下達：___________________（首筆訂單 txn hash 或 order id）

---

## 6. 緊急回滾程序

### 何時執行

- Kill switch 觸發且 flatten 持續失敗無法自動恢復
- 人工決定終止 dogfood（任何原因）
- 觀測到不可預期的異常行為

### 6.1 執行 Panic [人工觸發，動作自動]

`scripts/panic.py` 預設 **dry-run**（只列印將執行的動作，零寫入）；加 `--yes` 才真的
執行 trip 全流程。主網必須顯式 `SPARK_NETWORK=mainnet`（緊急工具，不擋主網）。

```bash
# 第一步：dry-run 預覽（列出將撤幾張掛單、各部位平倉方向與量）
SPARK_NETWORK=mainnet SPARK_ACCOUNT_ID=filet-mainnet \
SPARK_USER_ADDR=0x_______________________ \
SPARK_BUILDER_ADDR=0x_____________________ \
uv run python -m scripts.panic

# 第二步：確認清單無誤後，實際執行
SPARK_NETWORK=mainnet SPARK_ACCOUNT_ID=filet-mainnet \
SPARK_USER_ADDR=0x_______________________ \
SPARK_BUILDER_ADDR=0x_____________________ \
uv run python -m scripts.panic --yes
```

**`--yes` 執行內容**：撤全部掛單 → reduce-only 全平 → 寫 `var/copytrade/killswitch.tripped`
→ 告警。ARM 檔寫入後引擎拒絕交易，直到人工 re-arm。

### 6.2 驗證清零 [人工]

```bash
uv run python -c "
from hyperliquid.info import Info
from spark.exchange.hyperliquid import HyperliquidAdapter
a = HyperliquidAdapter('mainnet', info=Info('https://api.hyperliquid.xyz', skip_ws=True))
addr = '0x_______________________'  # 填 SPARK_USER_ADDR
print('positions:', a.get_positions(addr))
print('open orders:', a.get_open_orders(addr))
print('account value:', a.get_account_value(addr), 'USDC')
"
```

**確認**：
- [ ] positions = `[]`（無持倉）、open orders = `[]`（無掛單）
- [ ] account value 合理（初始 1000 USDC ± 手續費與已實現損益）

### 6.3 停止引擎 [人工]

```bash
# 如果引擎在 systemd service 內運行
sudo systemctl stop spark-copytrade

# 如果在 tmux 或前台運行
# Ctrl+C（優雅關閉）
```

### 6.4 禁用 LIVE 模式 [人工]

確保下次重啟不會自動進入 live 模式：

```bash
# 編輯 .env / systemd EnvironmentFile，改為：
COPY_LIVE_TRADING=false
```

> 注意：**不要**在此時刪 `var/copytrade/killswitch.tripped`——該檔存在時引擎拒絕交易，
> 是回滾期間的額外保險。只有確定要恢復運轉時才依 §5.5 re-arm。

### 6.5 資金撤出 [人工]

將剩餘資金從 follower 錢包轉回主賬戶或交易所：

```
轉出金額：___________ USDC
接收地址：___________________
交易哈希：___________________
確認時間：___________ （UTC）
```

### 6.6 事後回報 [人工]

記錄：
- **回滾原因**：___________________
- **運行時長**：___________ 小時
- **總累計費用**：___________ USDC
- **最大回撤**：___________ %
- **問題排查記錄**：___________________

---

## 7. 故障排查

### 症狀 A：`sync_failed` 持續告警

**可能原因**：
- hl-copytrader leader 信息延遲或不可用
- 本地網絡連接不穩定
- API 額度不足

**排查步驟**：
1. 檢查 hl-copytrader 線上狀態（查 Discord/Telegram）
2. `curl https://api.hyperliquid.xyz/info -d '{"type":"allMids"}'` 驗證 API 連通
3. 檢查本地日誌是否有具體的 HTTP 錯誤碼

### 症狀 B：`taker_share` 突然升高 (> 40%)

**可能原因**：
- leader 交易量激增，超過本地權益能跟
- 參數配置誤差（capital_utilization 設置過高）

**排查步驟**：
1. 檢查 leader 當日成交量
2. 驗證 `COPY_CAPITAL_UTILIZATION` 設置（預設 1.0）
3. 考慮臨時降低 `capital_utilization` 至 0.8

### 症狀 C：對帳不符（reconcile 失敗）

**可能原因**：
- builder fee 累計公式不一致
- modify 路徑遺漏了 builder 簽名

**排查步驟**：
1. 手動驗證當日所有成交記錄：
   ```bash
   SPARK_NETWORK=mainnet SPARK_BUILDER_ADDR=0x_____ \
   uv run python -m scripts.reconcile_day 20260716
   ```
2. 對比 builder accrual 端的官方數據
3. 檢查修改訂單的日誌，確認 builder 欄位未遺漏

### 症狀 D：收到 `[CRIT] killswitch | ...` 回撤告警但持倉未清

**可能原因**：
- 全平訂單被 reject（不足保證金 / 網絡中斷）
- `flatten_on_breach` 誤設為 false

**排查步驟**：
1. 檢查 `COPY_FLATTEN_ON_BREACH=true`
2. 查詢 order book，確認有成交機會
3. 手動執行 `scripts.panic --yes`

---

## 8. 運行記錄與檢查清單

### 完整運行流程檢查表

- [ ] **§0 前置**：新錢包生成 ✅ / 入金 ✅ / builder 地址驗證 ✅
- [ ] **§1 Onboarding**：環境變數設定 ✅ / 執行通過 ✅ / agent key 入 Keychain ✅
- [ ] **§2 Shadow**：連續 3 天不可解釋差異 = 0 ✅
- [ ] **§3 LIVE**：Checklist 全項通過 ✅ / 啟動成功 ✅ / 首筆訂單成交 ✅
- [ ] **§4 日常觀察**：≥7 日無 sync_failed ✅ / taker_share 常 < 30% ✅ / 日報都正常 ✅
- [ ] **§5 Kill switch**：演練成功 ✅ / flatten 驗證 ✅ / re-arm 通過 ✅
- [ ] **§6 回滾**：（如適用）panic 執行 ✅ / 持倉清零 ✅ / 資金撤出 ✅

### M1 驗收 Gate 最終檢查

| 指標 | 門檻 | 達成? |
|---|---|---|
| Shadow 對照 | 連續 3 交易日不可解釋差異 = 0 | ✅ / ❌ |
| Builder fee（place） | 100% fills 有 accrual | ✅ / ❌ |
| Builder fee（modify） | 100% fills 有 accrual | ✅ / ❌ |
| Safety net 吃價 | median ≤ 10bp （majors） | ✅ / ❌ |
| Safety net 占比 | < 30% 總成交量 | ✅ / ❌ |
| Skipped-small 量化 | 數字已寫入日報 | ✅ / ❌ |
| 對帳 | sync_failed=0 / 日對帳 drift=0 | ✅ / ❌ |
| Kill switch 演練 | 成功執行並 re-arm | ✅ / ❌ |
| Agent key 安全 | 無提款權 | ✅ / ❌ |

**M1 通過判定**：上表全項 ✅ → 驗收通過，可開始 M2 計畫。

---

## 附錄 A：環境變數速查表

| 變數名 | 說明 | 範例 / 預設值 |
|---|---|---|
| `SPARK_NETWORK` | 網絡環境 | `mainnet` / `testnet` |
| `SPARK_ACCOUNT_ID` | 帳戶標識（Keychain 查詢鑰匙） | `filet-mainnet` |
| `SPARK_USER_ADDR` | Follower 錢包地址 | `0x...` |
| `SPARK_BUILDER_ADDR` | Builder 主網地址 | `0x...` |
| `COPY_LEADER_ADDRESS` | 跟單對象錢包 | `0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1` |
| `COPY_LIVE_TRADING` | 是否真實下單 | `false` / `true` |
| `COPY_INTERVAL_S` | 同步頻率（秒） | `60` （每分鐘） |
| `COPY_MAX_DRAWDOWN_PCT` | Kill switch 回撤門檻 | `0.20` （20%） |
| `COPY_FLATTEN_ON_BREACH` | 回撤自動全平 | `true` / `false` |
| `COPY_CAPITAL_UTILIZATION` | 權益使用率 | `1.0` （100%） |
| `COPY_MODIFY_POLICY` | modify 政策（等 T1.3 裁決前不得改） | `modify-first` / `cancel-place` |
| `COPY_ALLOCATED_CAPITAL` | 分配資本（0=用全權益） | `0` |
| `COPY_TG_BOT_TOKEN` | Telegram bot token（空=靜默） | — |
| `COPY_TG_CHAT_ID` | Telegram chat ID | — |
| `COPY_TG_MUTED` | 靜音分類（逗號分隔；critical 不受影響） | 例 `orders` |

（完整清單與預設值見 `src/spark/copytrade/config.py` 的 `CopySettings.from_env`。）

---

## 附錄 B：常見指令集合

```bash
# 環境變數預設宣告（可寫入 ~/.spark_mainnet.env 後 source）
export SPARK_NETWORK=mainnet
export SPARK_ACCOUNT_ID=filet-mainnet
export SPARK_USER_ADDR=0x_______________________
export SPARK_BUILDER_ADDR=0x_____________________

# 1. 匯入 main key
uv run python -m scripts.bootstrap_keys filet-mainnet main

# 2. Onboarding
uv run python -m scripts.run_testnet_flow

# 3. 啟動 Shadow 模式（Task 12 交付後）
# uv run python -m scripts.run_copytrade --shadow

# 4. 啟動 LIVE 模式（Task 12 交付後）
# COPY_LIVE_TRADING=true uv run python -m scripts.run_copytrade

# 5. 隔日 CSV 對帳（macOS date 語法）
uv run python -m scripts.reconcile_day $(date -v-1d +%Y%m%d)

# 6. 日報檢查（當日 UTC；報告落 var/copytrade/reports/）
uv run python -m scripts.copytrade_daily_report

# 7. 緊急平倉（預設 dry-run；--yes 才執行）
uv run python -m scripts.panic
# uv run python -m scripts.panic --yes

# 8. 查詢持倉／掛單／權益（真實可跑；填入 SPARK_USER_ADDR）
uv run python -c "
from hyperliquid.info import Info
from spark.exchange.hyperliquid import HyperliquidAdapter
a = HyperliquidAdapter('mainnet', info=Info('https://api.hyperliquid.xyz', skip_ws=True))
addr = '0x_______________________'
print(a.get_positions(addr)); print(a.get_open_orders(addr)); print(a.get_account_value(addr))
"
```

---

## 附錄 C：Notifier（通知）整合

引擎經 `spark.copytrade.notifier.Notifier` 介面注入通知（CLAUDE.md 慣例：引擎不
import 具體實作）。實作為 `TelegramNotifier`，從環境變數建構：

- **`COPY_TG_BOT_TOKEN`**：Telegram bot token（缺省時 notifier 全靜默，不會 raise）
- **`COPY_TG_CHAT_ID`**：接收通知的群組或頻道 ID
- **`COPY_TG_MUTED`**：逗號分隔的靜音分類（只影響 info/warn；critical 永不可靜音）

Notifier 事件級別（`src/spark/copytrade/notifier.py`）：
- `info`：例行日報、訂單成交
- `warn`：對帳偏差、taker share 突升
- `critical`：kill switch 觸發、flatten 失敗、sync_failed 持續（不受靜音影響）

**測試通知**（真實可跑；token/chat_id 從環境變數讀）：

```bash
COPY_TG_BOT_TOKEN=... COPY_TG_CHAT_ID=... uv run python -c "
from spark.copytrade.notifier import TelegramNotifier
n = TelegramNotifier.from_env()
print(n.critical('runbook', 'notification test'))  # True = 已送達
"
```

---

**版本**：2026-07-16 M1
**撰寫人**：Task 18 Runbook Preparation
**引用**：Spec §T4.3, §M1, CLAUDE.md
