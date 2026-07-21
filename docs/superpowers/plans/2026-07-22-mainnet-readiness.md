# Filet 主網 dogfood 上線準備狀態評估（2026-07-22）

**評估對象**：使用者明天讓錢包 `0xbAC652A5Fb611c1BdC3B9D244cc7E0cC03123662` 當 follower，
用 1000 USDC 真錢跟隨 leader `0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1`。

**證據紀律**：以下 HL 主網查詢為 2026-07-22 唯讀 curl 公開端點的真實回傳；程式碼結論附
`檔案:行號`；未親自查證處明確標「未查證」。

---

## ⛔ 頭號封鎖項（未列在題目背景，但實查後最嚴重）

**follower 錢包 `0xbAC652…3662` 主網上正被使用者自己的 Momentum 實盤引擎交易中。**

主網唯讀查詢（2026-07-22）：
- `extraAgents` → `[{"name":"Momentum","address":"0xfa4868…d2f5","validUntil":1798552309505}]`
  = 這錢包唯一的已授權 agent 叫 **Momentum**（有效到 2026-12-29 UTC），**不是 Filet**。
- `userFillsByTime` 近 30 天 → **44 筆成交**，最新一筆 **2026-07-21 14:26 UTC（昨天）**，
  幣種 HYPE 31 / XRP 5 / DOGE 5 / @107(spot) 3。
- `clearinghouseState` → 此刻 accountValue 1000.003237、**零部位**（Momentum 目前空手但仍在循環）。

若把 Filet copytrade 指向**同一顆錢包**開 live，等於兩個獨立引擎共用同一個 perp 帳戶：
- Filet 的 `sync_positions` 第二段（`positions.py:268-279`）會把**任何「我有、leader 沒有」的
  部位 reduce-only 平掉**——Momentum 開的 XRP/DOGE/HYPE 會被 Filet 當成殘留部位強制平倉。
- 回撤 kill switch 與成本熔斷器量的是**合併後**的權益與換手率（`use_full_equity=True` 預設，
  scale 用整顆 1000 權益，`config.py:109`）——Momentum 的虧損會觸發 Filet 的 kill switch
  並平掉一切，Momentum 的換手率會灌爆成本閘。
- 這是不可調參數能修的：**兩個控制器一個帳戶＝必然互毀**。

**修法（硬性）**：Filet follower 必須用一顆**專屬、沒有其他引擎碰**的新錢包。若堅持用這顆，
必須先確認 Momentum 引擎已對此錢包停機且撤銷其 agent——但那與「dogfood 跟單」是兩件事，
不該混在同一顆錢包同一晚上線。**此項未解決前，其餘一切都不該開 live。**

---

## 逐項評估

### 1. mainnet onboarding 路徑 — ✗ 未就緒

**已查證的完整流程**（程式碼）：
1. SIWE 登入 → session。
2. `POST /api/onboard/agent` → `keysvc.generate()` → `Account.create()`（os.urandom keypair，
   `keysvc/server.py:24`），私鑰寫入 EnvFileKeyStore `/etc/filet/keys/<id>/agent.key`(600)，
   只回地址；`store.set_agent_address`（`app.py:1036-1068`）。
3. `POST /api/onboard/payload/approve-builder-fee` → `build_approve_builder_fee(is_mainnet=
   cfg.is_mainnet)`（`app.py:1559-1577`、`approvals.py:49`）。pre-flight：builder 餘額 ≥100
   否則 503（`app.py:1568`）。
4. `POST /api/onboard/payload/approve-agent` → `build_approve_agent`（`app.py:1542-1557`）；
   需先 generate 過 agent，否則 409。
5. 前端用**使用者主錢包**簽 typed_data（eth_signTypedData_v4）後**直送 HL /exchange**
   （`web/src/lib/hl.ts:98 submitToHl`）；後端結構上碰不到簽名（紅線 3）。
6. status 端點以鏈上查詢確認：`maxBuilderFee!=0` ＋ agent 在 `extraAgents` ＋ funded ≥100 → ready。

**Mainnet 版 typed data 怎麼產**：`is_mainnet ← FILET_API_NETWORK`（`config.py:157-158`），
`approvals.py:39` 據此填 `hyperliquidChain="Mainnet"/"Testnet"`。**只有伺服器 env
設 `FILET_API_NETWORK=mainnet` 時**才產 Mainnet payload。

**未就緒的具體項**：
- 伺服器現為 testnet（roadmap:37「部署現況：testnet 模式」；`filet_regression_check.py:286-289`
  斷言 `FILET_API_NETWORK=testnet`）。此時 onboarding 產的是 **Testnet** typed data，
  使用者簽了對主網無效。**必須**把 `FILET_API_NETWORK=mainnet`＋`FILET_BUILDER_ADDR=新builder`
  重新部署 filet-api。（伺服器實機當前值未親自 SSH 查證——不碰基礎設施。）
- follower 主網**尚未有 Filet agent**（extraAgents 只有 Momentum）、對新 builder 的
  **maxBuilderFee=0**（實查）→ ApproveAgent 與 ApproveBuilderFee **兩筆都得在主網重新走一次**
  （testnet 上簽過的不算）。
- Web onboarding 的可達性：P0.4「開 Lightsail 80/443 防火牆」仍未做、TLS 為自簽、
  session cookie `secure=True` → 純 http 登入會失敗（roadmap:32,37,41-43）。**web 流程可能
  根本連不上**。上線前需確認 HTTPS 真憑證與防火牆（未親自查證伺服器現況）。

### 2. follower 主網設定 — ✗ 未就緒（範本齊，值未填、白名單缺、狀態根需清）

依 `deploy/follower.env.example` + `deploy/filet-follower@.service`：
- `SPARK_NETWORK=mainnet`（範本安全預設 testnet）、`COPY_LIVE_TRADING=true`（範本預設 false，
  紅線 5，人工決策）。
- `SPARK_ACCOUNT_ID=fbac652a5fb611c1bdc3b9d244cc7e0cc03123662`（=「f」+ 地址去 0x，
  `config.py:29`；對得上 e2e findings:88 的 KeyError）。
- `SPARK_USER_ADDR=0xbAC652…3662`、`SPARK_BUILDER_ADDR=0x81E9…1183`。
- 本金：wallet 恰 1000。`use_full_equity=True`（預設）用整顆權益；或設 `COPY_ALLOCATED_CAPITAL=1000`
  ＋`use_full_equity=false`。⚠️ 見頭號封鎖項：整顆 1000 與 Momentum 共用。
- **leader 白名單（mainnet 版）缺**：leader 現在來自 `FILET_LEADERS_PATH` 的 `leaders.json`
  白名單（`run_copytrade.py:452-467`，env 只是 fallback）。repo 內 `find leaders.json` **查無此檔**。
  主網部署的 `/opt/filet/spark/var/filet/leaders.json`（root:root 644）**必須存在且含此 leader**，
  否則引擎啟動時 `LeaderResolutionError` 拒絕啟動。
- keystore：agent key 走 `/etc/filet/keys/<id>/agent.key`（EnvFileKeyStore）——必須是
  **主網已授權的那把 Filet agent**（即 onboarding step 2 keysvc 生成、step 4 在主網 approve 的同一把）。
- ⚠️ **狀態根需清空**：`FILET_STATE_DIR=/opt/filet/state/<account_id>`，而 account_id 在
  testnet/mainnet 相同。若 testnet run 的殘留（7 天權益樣本 peak≈500、lifetime peak≈500、
  或 `killswitch.tripped`）留在同一路徑，主網第一輪的回撤基準會被污染（甚至 tripped 檔會
  讓引擎靜默不交易）。主網上線前必須確認該狀態根是乾淨的。（伺服器實際檔案未 SSH 查證。）

### 3. 風險參數對主網是否合理 — ⚠️ 對「這個 leader」尚可，但有結構性盲區

- `cost_max_turnover_24h=20`：自述未校準、n=1（`config.py:142-154`）。對此 leader 實測（見項 4）
  crypto 換手率約 **0.32×/日**，比 20 的門檻低約 60 倍——**不會誤觸**。方向是「太鬆而非太緊」，
  dogfood 可接受。錯得太低才會誤觸 → 擋新開倉＋累犯升級 → kill switch 平倉。
- `max_drawdown_pct=0.20`（1000→跌到 800 觸發，滾動 7 天窗）＋`max_total_drawdown_pct=0.40`
  （自 lifetime peak 的絕對底線，`killswitch.py:115-126`）：dogfood 尺度合理。
- ⚠️ **第一段時間回撤保護實質關閉**：樣本覆蓋不足時 `evaluate()` 只發 critical
  「回撤保護尚未生效」但**不觸發**（`killswitch.py:106-114`）——主網上線初期（最高風險時刻）
  drawdown 熔斷是關的，只是有大聲告警。這是設計，但要知道。
- 耦合（`config.py:118-127`）：`max_drawdown_pct` 調高會等比放大成本閘誤觸面；維持 0.20 即可。

### 4. leader 適配性 — ⚠️（實查）follower 會先空手，但不會踩成本熔斷器

leader `0xf97ad6…ddd1` 主網（實查 2026-07-22）：
- accountValue **176,968 USDC**、**此刻零部位**（totalNtlPos=0）。
- 近 7 天成交：**HYPE 63 筆 / 名目約 396,269**（crypto，會跟）＋ xyz 美股
  SNDK/INTC/IBM/MU/SPCX 共 36 筆 / 名目約 185,271（**follower 跳過**，`positions.py:166`
  `_coin_dex` 非空即 skip）。
- 結論：follower **只鏡像 HYPE**。leader 目前空手 → **follower 上線後先空手**，直到 leader
  開下一個 HYPE 倉（近 7 天平均約 9 筆/日，應該不久）。這是預期行為非 bug；第一個鏡像動作是
  一筆 OPEN。
- 換手率：leader crypto 名目 396,269/7 ÷ 177k 權益 ≈ **0.32×/日**；等比鏡像保持比率 →
  follower 也約 0.32×/日，遠低於 20。成交筆數約 9/日 vs 400 門檻。**不會踩成本熔斷器**。
- scale = 1000/176,968 ≈ 0.00565。leader HYPE 單筆名目約 6,290 → follower 約 35 USDC/筆，
  高於 `min_order_notional=10`。極小額再平衡可能 <10 被 skip（單向保守，`positions.py:177`）——
  follower 在微調上可能略微欠追蹤，無害。

### 5. kill switch / 監控 — ⚠️ 齊備，但告警管道需在 env 開啟

- 回撤 kill switch：lock-first、撤單→reduce-only 全平→覆寫 ARM→總結（`killswitch.py:211-328`），
  `flatten_on_breach` 預設 True。✓
- 成本熔斷 → 累犯升級 → kill switch（`config.py:159-162`）。✓
- **Telegram**：follower 引擎**只有在 `COPY_TG_BOT_TOKEN` 有設時**用 TelegramNotifier，
  否則 **NullNotifier**（`run_copytrade.py:441-442`）。critical/kill-switch 告警**是有接
  notifier 的**（`killswitch.crit()`），但 notifier 是 Null 就等於**即時無人收到**。
  → follower env **必須設 `COPY_TG_BOT_TOKEN`/`COPY_TG_CHAT_ID`**（範本 line 29-30 有位子＋警語）。
  未設的話 kill switch 半夜觸發沒人知道（唯一殘留是本地 `var/copytrade/alerts.log`，
  `killswitch.py:163-172`，需人工去讀）。
- 心跳：引擎寫 `exchange_dir/engine` 健康心跳（systemd ReadWritePaths），ops 面板讀。✓
- 緊急工具：`scripts/panic.py`（預設 testnet，要打主網需顯式 `SPARK_NETWORK=mainnet`）。✓

### 6. 非託管不變量在主網 — ✓ 結構上成立，但首把真錢 agent key 衛生更關鍵

- agent key 無提款權（HL agent 協議層即 trade-only）；`ExchangeAdapter` 無 withdraw/transfer
  （紅線 3，結構性測試）。主網與 testnet 同。✓
- builder 無 withdraw/transfer。✓
- 首把主網真錢 agent key：keysvc `Account.create()` os.urandom 生成、600 權限、SO_PEERCRED
  守 socket——機制與 testnet 相同，**無 testnet 沒暴露的新結構性風險**。唯一升級的是「這把
  key 現在管真錢」，檔案權限/keysvc 存取控制的實機狀態值得再確認（未 SSH 查證）。
- 注意：此錢包已有 Momentum agent；approve 一把叫 filet 的 named agent 會**並存**（不 rotate 掉
  Momentum）——這正是頭號封鎖項的成因。

### 7. 主網上線但今晚沒驗到（未驗證清單）

- 整套 mainnet onboarding 流程**從沒在主網跑過**（伺服器是 testnet）。
- 主網真實滑價/成交深度（testnet 流動性 ≠ 主網）。
- 成本熔斷器在**主網真實資料**下的行為（唯一觀測是 n=1 testnet）。
- leader **突然大額開倉**時的鏡像行為（主網）。
- spot→perp `accountClassTransfer` 在**主網** perp 窗是否被當入金扣除（roadmap:130 點名須測，
  只在 testnet 做過）。
- HTTPS/防火牆下 web onboarding 的可達性（P0.4 未做）。
- **兩引擎共帳戶（Momentum + Filet）** — 從沒測、也無法安全。

---

## 最該優先處理的三件事（上線前必須解決）

1. **⛔ 換一顆專屬 follower 錢包**（或確認 Momentum 已對此錢包停機且撤 agent）。
   `0xbAC652…3662` 昨天還在被 Momentum 交易；兩引擎共用一個 perp 帳戶會互相平倉、
   互相污染 kill switch 與成本閘。這是不可調參數解決的結構問題。
2. **⛔ 把 filet-api 切到 mainnet 並補完 onboarding 前置**：`FILET_API_NETWORK=mainnet`＋
   `FILET_BUILDER_ADDR=0x81E9…1183` 重新部署；在主網重走 ApproveAgent＋ApproveBuilderFee
   （實查兩者主網皆未做）；先解決 P0.4 防火牆/TLS 讓 web 流程可達。順帶：新 builder 主網餘額
   只有 **110 USDC**（實查），僅高於 100 門檻 10 USDC，一跌破就靜默停止累計 builder fee
   （`onboarding.py:75-78`）——上線前補到有緩衝。
3. **⛔ follower env 與狀態根就緒**：`COPY_TG_BOT_TOKEN/CHAT_ID` 必設（否則 kill switch 靜默）；
   建立含此 leader 的 mainnet `leaders.json`（現查無此檔，缺了引擎拒啟動）；清空
   `/opt/filet/state/<account_id>` 的 testnet 殘留（避免回撤基準被 500 權益污染或 tripped 檔卡死）。
   並知悉：上線初期覆蓋不足期間 drawdown 保護實質關閉（有告警）。

---

## 一句話結論

**明天不能安全上線。** 核心引擎邏輯與防護（kill switch、成本閘、非託管、crypto-only 鏡像）
本身就緒，且此 leader 的換手率不會踩熔斷；但至少缺三步硬前置：**(a) follower 必須換成
Momentum 碰不到的專屬錢包**、**(b) 伺服器切 mainnet＋在主網完成 agent/builder 兩筆授權＋
修好 TLS/防火牆**、**(c) follower env 補 Telegram 告警與 mainnet 白名單、清空 testnet 狀態根**。
在 (a) 解決前，其餘不該開 live。
