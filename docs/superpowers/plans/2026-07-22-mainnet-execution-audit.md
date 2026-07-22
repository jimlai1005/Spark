# Filet 主網 dogfood 執行鏈逐步稽核（2026-07-22）

**審查者**：fresh、對抗性上線審查。目標＝從乾淨錢包到 follower 實盤鏡像，逐步找出「還不存在」或「主網從沒驗過」的環節。

**場景（本次已定，與前一份 `2026-07-22-mainnet-readiness.md` 不同）**：
- Follower：**一顆全新專屬錢包**（使用者會準備），放 1000 USDC 於主 dex perp。位址尚未提供。
- Leader：`0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1`（主網）
- Builder：`0x81E9Dd1eF3B2c2E96a4572175F3035db3fC91183`（主網）
- 引擎只鏡像主 perp dex 的 crypto。
- 部署伺服器 `52.197.137.3` 目前是 testnet 設定。

**證據紀律**：本 repo 曾出現 agent 虛構實跑證據（含 opus 級）。以下每個判定的依據都是**本審查親自**跑的：主網唯讀 curl（2026-07-22）、`檔案:行號` 實讀、離線測試實跑。**無法查證處明說「未查證」，不 SSH 碰基礎設施、不碰資金、不讀 .env、hl-copytrader 唯讀。**

---

## ⭐ 與前一份文件的關鍵差異（必須先講清楚）

前一份 `2026-07-22-mainnet-readiness.md` 的**頭號封鎖項**是「follower 錢包 `0xbAC652…3662` 正被 Momentum 引擎交易，兩引擎共帳戶必然互毀」。

**本次場景用一顆全新專屬錢包 → 那個封鎖項在前提上就消失了。** 連帶：

- **狀態根天然乾淨**（見步驟 6）：`account_id = "f" + 位址`（`config.py:29-32`），全新位址＝從未用過的 account_id＝`/opt/filet/state/<account_id>` 是全新目錄，不可能有 testnet 殘留 peak／`killswitch.tripped`。前一份文件「清空狀態根」那條**對新錢包不適用**。
- **首輪不會誤平他倉**（見步驟 7）：新錢包零部位，`sync_positions` 第二段（`positions.py:269-279`「我有 leader 沒有 → 跟平」）遍歷空 dict＝no-op。

**前提條件（使用者必須真的做到）**：這顆錢包**只有 Filet 一個引擎在碰**，沒有其他 agent、沒有其他自動化。若圖省事拿現有錢包，前一份文件的互毀分析立即回歸。

---

## 逐步執行鏈判定

圖例：✓ 存在且就緒｜✗ 不存在／缺件｜⚠️ 存在但主網未驗／有前置

### 步驟 1 — 產 mainnet agent key ✓（機制就緒）／⚠️（主網伺服器未跑過）

- **怎麼產**：`POST /api/onboard/agent` → keysvc `handle_generate`（`keysvc/server.py:20-34`）→ `eth_account.Account.create()`（os.urandom 亂數，私鑰只存區域變數）→ `EnvFileKeyStore.import_agent_key` 以 **O_EXCL** 寫入，存在即 `FileExistsError` 不重生。
- **放哪／權限**：`/etc/filet/keys/<account_id>/agent.key`，600（`follower.env.example:3` 註明）。socket 由 SO_PEERCRED 守（`keysvc/server.py:79`）。私鑰絕不進回應／log／例外訊息。
- **主網 context 有沒有 testnet 沒暴露的問題**：**沒有結構性新風險**。agent key 本身與網路無關（同一把 key 在哪條鏈生效由 approve 決定，不由 key 決定）。唯一「升級」的是這把 key 現在管真錢，故檔案權限與 keysvc 存取控制的**實機狀態**值得再確認——**未 SSH 查證**。
- 判定：**機制存在，但這條路徑從沒在主網伺服器跑過**（伺服器是 testnet）。

### 步驟 2 — ApproveAgent（主網簽）✓（工具存在）／⚠️（server 需切 mainnet）

- **typed data 誰產**：`approvals.build_approve_agent`（`approvals.py:31-46`），站在 SDK `user_signed_payload` 上，零手工 EIP-712。sign types 常數抄自 SDK（`approvals.py:11-16`）。
- **端點依什麼決定 Testnet/Mainnet**：`app.py:1553-1555` 傳 `is_mainnet=cfg.is_mainnet`，而 `cfg.is_mainnet ← FILET_API_NETWORK`（`publicapi/config.py:157`，`from_env` required 清單 line 180/187）。→ `approvals.py:39` 填 `hyperliquidChain="Mainnet"/"Testnet"`。**只有伺服器 env `FILET_API_NETWORK=mainnet` 時才產 Mainnet payload。**
- **前端如何送**：使用者主錢包簽 typed data 後，前端 `submitToHl` 直送 `hlBaseUrl(hyperliquidChain)/exchange`（`web/src/lib/hl.ts:103`；`hlBaseUrl` 對 `"Mainnet"` 回 `https://api.hyperliquid.xyz`，line 69/73）。後端結構上碰不到簽名（紅線 2）。
- **能不能繞過我方前端、在 HL 官方 app 授權 agent**：HL 官方 app 確有「API wallets」可自行產生／授權 agent——**但那會授權 app 自己產的 agent 位址，不是我方 keysvc 產的那把**。引擎 live 時用 `EnvFileKeyStore` 裡 keysvc 產的 key 簽單（`run_copytrade.py:491-492`），所以使用者必須授權**我方給的 agentAddress**，實務上得走我方前端拿到那個位址。（**此段為推測**：未實測 HL app 是否允許貼入外部 agent 位址授權；不影響結論——不論走哪條，主網 server 必須先是 mainnet，否則 payload 是 Testnet、簽了對主網無效。）
- 判定：**工具鏈完整**；前置＝伺服器 `FILET_API_NETWORK=mainnet`。**實查**：此 follower 主網 `extraAgents` 目前無 Filet agent（前一份文件查為只有 Momentum，本次是新錢包故必為空）→ 主網 ApproveAgent **必須實走一次**。

### 步驟 3 — ApproveBuilderFee（主網簽）✓（工具存在）／⚠️（無 HL app 後路 → 最脆弱）

- **工具存不存在**：**存在**。`approvals.build_approve_builder_fee`（`approvals.py:49-61`）＋端點 `app.py:1559-1577`＋前端 `submitToHl` 同步驟 2。
- **maxFeeRate 設多少**：`cfg.max_fee_rate = Settings.max_rate = "0.1%"`（`publicapi/config.py:87`、`config.py:22`）。**實收 f=20**（十分之一 bp＝0.02%，`config.py:21`）。授權上限 0.1% 給足協議上限，日後調 f 免重簽（D6）；不變量 `charged(0.02%) ≤ approved(0.1%)` 由 `config.py:31-35` 強制。builder／maxFeeRate 出自伺服器常數，不收使用者輸入（紅線 6，`app.py:1573`）。
- **HL 官方 app 有沒有「授權任意 builder」介面**：據我所知，ApproveBuilderFee 是 **API-only 的簽章動作，HL 官方 app 沒有對應 UI**（**標記推測**，未逐一查證 HL 前端當前版本）。若屬實，**這一步沒有第一方後路**——使用者無法繞過我方前端自行完成。→ **我方 web onboarding 必須可達**（TLS＋防火牆），否則使用者卡在這步且沒有替代路徑。
- **pre-flight**：`app.py:1568` 要求 builder 餘額 ≥ 100 USDC 否則 503。**主網實查（2026-07-22 curl）：builder `0x81E9Dd` accountValue = 110.22 USDC**——高於門檻僅 **10 USDC**。一旦跌破 100，onboarding 直接 503，且既有跟單「成交但 builder fee 靜默不累計」（`onboarding.py:74-78` 同語意）。
- 判定：**工具存在**，但這是**最可能斷的一步**（理由見文末）。前置＝server mainnet＋web 可達＋builder 補到有緩衝。

### 步驟 4 — follower env（mainnet）⚠️（純 env 驅動，無結構性阻擋，但需人工填對）

- **有沒有結構性東西阻擋主網**：**沒有**。`SPARK_NETWORK` 純讀 env（`run_copytrade.py:427`），mainnet 是合法值（`config.py:5-8`）。紅線 5 的「live 預設 False」是**預設值＋範本安全值**，不是結構閂：
  - `CopySettings.live_trading` 預設 False（`copytrade/config.py:85`）；`COPY_LIVE_TRADING=true` 即開。
  - `follower.env.example` 刻意出貨 `SPARK_NETWORK=testnet`＋`COPY_LIVE_TRADING=false`（範本 line 21-22 有大段警語，說明原本出貨 mainnet+true 被改成安全值）。
  - live 啟動時印大字警告並二次驗 env（`run_copytrade.py:482-488`）。
  - 即：**開主網真錢是人工改兩個 env 的決策**，符合紅線；沒有任何 assert 會擋 mainnet。
- **`COPY_TG_*` 不設會怎樣**：`notifier = TelegramNotifier if COPY_TG_BOT_TOKEN else NullNotifier`（`run_copytrade.py:441-442`）。kill switch／成本熔斷的 critical 告警**照發**，但 Null 就是**沒人即時收到**——唯一殘留是本地 `var/copytrade/alerts.log`（`killswitch.py:163-172`），要人工去讀。**半夜熔斷會靜默。** → 主網必設 `COPY_TG_BOT_TOKEN/CHAT_ID`（範本 line 29-30 有位＋警語）。
- **門檻預設對真錢合不合理**：
  - `max_drawdown_pct=0.20`（1000→跌 800，7 天滾動）＋`max_total_drawdown_pct=0.40`（lifetime peak 絕對底線，`copytrade/config.py:117/131`）。dogfood 尺度合理。
  - `cost_max_turnover_24h=20`：自述未經真實資料校準（`config.py:142`）。對此 leader 換手率極低（見步驟 7），**不會誤觸**，方向偏鬆而非緊，dogfood 可接受。
  - **`use_full_equity=True` 預設**（`config.py:109`）→ scale 用整顆 perp 權益。新專屬錢包整顆 1000 就是本金，合理。或設 `COPY_ALLOCATED_CAPITAL=1000`＋`use_full_equity=false`（範本走這條，line 27）。兩欄矛盾組合會 fail-fast 拒啟動（`config.py:311-321`）。
- 判定：純 env，無結構阻擋；必填項見文末清單。

### 步驟 5 — mainnet 白名單 ✗（leaders.json 缺，引擎會拒啟動）

- **要不要 mainnet 專屬**：`FILET_LEADERS_PATH` **必填、無預設、必須絕對路徑**（`leader_resolve.py:100-150` `require_leaders_path`）。**實查 repo：`find leaders.json` 查無此檔**（只有 `deploy/leaders.json.example`）。正式部署需 `/opt/filet/spark/var/filet/leaders.json`（root:root 644，filet-api 寫不到——這是整道防線承重點）。
- **兩述詞**（`leaders.json.example` 註解＋`leader_resolve.py` 引用 `spark/filet/leaders.py`）：
  - `is_selectable`（看 `accepting_new`）：**新客戶能不能選**——API 目錄頁用。
  - `is_still_permitted`（看 `enabled`）：**已在跟的人還能不能繼續**——引擎每輪二次驗證用（`leader_resolve.py:263`）。
- **引擎二次驗證**：每 cycle 重新解析＋過白名單（`leader_resolve.py` 檔頭威脅模型：API 被打穿也擋得住指向惡意 leader）。leader `0xf97ad6` **必須在 leaders.json 且 `enabled:true`**。
  - 白名單**檔存在但缺此 leader** → `LeaderRevokedError` → 引擎拒啟動（啟動時）／受控收尾（執行中）。
  - 唯一 fail-open：白名單**檔案不存在**時 env 預設 leader 放行（`leader_resolve.py:275-279` 向後相容）——但那不是我們要的狀態，白名單本就該存在。
- 判定：**必須建立含此 leader 的 mainnet leaders.json**，否則引擎起不來（或不交易）。

### 步驟 6 — 狀態根 ✓（新錢包天然乾淨）

- `account_id = derive_account_id(user_addr) = "f" + 小寫位址去0x`（`publicapi/config.py:29-32`，41 字元不截斷）。
- `FILET_STATE_DIR=/opt/filet/state/<account_id>`（systemd 注入）。**全新錢包＝從未用過的 account_id＝全新空目錄**，結構上不可能有 testnet 殘留（7 天樣本、lifetime peak、`killswitch.tripped`）。
- 判定：**新錢包天然乾淨，無需清空動作**（此為本場景相對前一份文件的實質改善）。唯一要確認：運維別手滑把某個既有 state dir 指過來——但正常流程不會。

### 步驟 7 — 第一輪 run_cycle ⚠️（會先空手，且可能空手一陣子）

**leader 主網現況（本審查 curl 實查 2026-07-22）**：
- `clearinghouseState`：accountValue **176,968.23**、`totalNtlPos=0`、**assetPositions 空（零部位）**。
- 近 7 天 `userFillsByTime`：**56 筆**——HYPE 25（crypto，會鏡像）＋ xyz:SNDK/MU/INTC/SPCX 共 31（美股，`positions.py:166` `_coin_dex` 非空即 skip）。
- **⚠️ 最新一筆成交 UTC 2026-07-16 22:48——距今約 6 天。leader 已靜默 6 天。**（前一份文件稱「近 7 天平均約 9 筆/日、應該不久」與此不符：那 25 筆 HYPE 都在 07-16 之前，之後無新倉。）

**首輪行為**：leader 零 crypto 倉 → `sync_positions` 第一段（`positions.py:165` 遍歷 `leader_positions`）空＝no-op；第二段（`positions.py:269` 遍歷新錢包 `my_positions`）空＝no-op → **follower 上線後先空手，直到 leader 開下一個 HYPE 倉**。這是預期行為非 bug；第一個鏡像動作會是一筆 OPEN。**因 leader 已靜默 6 天，follower 可能空手等待較久**——不是故障，但使用者盯首輪時要知道「沒有動作」是正常的。

**「leader 空手 → follower 空手」測試覆蓋**：`tests/test_copy_sync_positions.py` 有 leader 有倉/follower 空（`_sync(leader, {})`）與 follower 有倉/leader 空（`_sync({}, mine)`）兩方向，**但無「兩邊皆空 `_sync({}, {})`」的顯式案例**（grep 實查無）。邏輯上兩迴圈跑空 dict 必為 no-op，已被兩個單邊案例間接覆蓋——**空缺是測試完整性的小洞，非功能缺陷**。離線實跑 `pytest tests/test_copy_sync_positions.py tests/test_copy_config.py tests/test_onboarding.py` → **70 passed**（本審查親跑）。

**leader 突然開大 crypto 倉時 scale 算對嗎**：scale = follower 權益 ÷ leader 權益。follower 側 equity 取自 `perp_equity_view`（主 dex perp accountValue 滾動樣本，`run_copytrade.py:382`／`equity.py`）——**用主 dex 權益，同基準正確**（工程原則 1）。scale ≈ 1000/176,968 ≈ 0.00565。leader 單筆 HYPE 名目（近 7 天 HYPE 總名目 ÷ 25 約 6,300）→ follower 約 35 USDC/筆，高於 `min_order_notional=10`（`config.py:115`）。極小額再平衡可能 <10 被 skip（單向保守，`positions.py:177-180`）——微幅欠追蹤，無害。

### 步驟 8 — 主網特有、testnet 沒驗的 ⚠️（全部未驗，本質上離線無法驗）

- **整套 mainnet onboarding 從沒在主網跑過**（伺服器 testnet）。
- 真實滑價／成交深度（testnet 流動性 ≠ 主網；HYPE 主網深度未測）。
- 成本熔斷器在**主網真實資料**下的行為（唯一觀測是 n=1 testnet，`config.py:143-147` 自述）。
- builder fee 真的入 `builderRewards`：`query_builder_accrued` 讀 referral state `builderRewards`（`hyperliquid.py:51-54`）——**是累計值，需另行 claim，不會自動入袋**（`ExchangeAdapter` 無提款/transfer，紅線 3）。主網從沒累計過真實 builder fee。
- **agent 下第一筆真單**：place_order 帶 builder 參數（`hyperliquid.py:341-349`）、經 resilience 邊界分類重試——主網從沒送過真單。
- **首把主網真錢 agent key 的檔案權限/keysvc 存取控制實機狀態**（未 SSH 查證）。

---

## 明天上線前「必須先做／先建」的依序清單

### A. 我方要建／要改的（工程＋運維）

1. **建立 mainnet `leaders.json`**（步驟 5，✗ 現在缺）：`/opt/filet/spark/var/filet/leaders.json`，root:root 644，含
   `{"address":"0xf97ad6…ddd1","name":...,"enabled":true,"accepting_new":true}`。**五個消費端**（filet-api／filet-follower@／filet_activate／兩個快照 timer）的 `FILET_LEADERS_PATH` 必須全部指到同一個絕對路徑（`leader_resolve.py:109-124`）。缺檔或缺此 leader → 引擎拒啟動。
2. **把 filet-api 切 mainnet 並重部署**（步驟 2/3 前置）：`FILET_API_NETWORK=mainnet`＋`FILET_BUILDER_ADDR=0x81E9…1183`。**未切之前，onboarding 產的是 Testnet typed data，使用者簽了對主網無效。**（伺服器實機當前值未 SSH 查證。）
3. **修好 web 可達性**（步驟 3 無後路，最關鍵）：TLS 真憑證＋開防火牆 80/443。ApproveBuilderFee 據推測無 HL app 後路，web 不可達＝使用者無法完成 builder 授權。（前一份文件 P0.4；伺服器現況未查證。）
4. **builder `0x81E9Dd` 補餘額**（步驟 3）：**實查僅 110.22 USDC**，高於 100 門檻僅 10。跌破即 onboarding 503＋既有跟單 builder fee 靜默不累計。上線前補到有緩衝。
5. **follower env 就緒**（步驟 4）：從 `follower.env.example` 複製後填
   `SPARK_NETWORK=mainnet`、`COPY_LIVE_TRADING=true`、`SPARK_ACCOUNT_ID=f<新錢包位址去0x>`、`SPARK_USER_ADDR`、`SPARK_BUILDER_ADDR=0x81E9…1183`、`COPY_ALLOCATED_CAPITAL=1000`（或用 `use_full_equity`）、**`COPY_TG_BOT_TOKEN`＋`COPY_TG_CHAT_ID` 必設**（否則 kill switch 靜默）。
6.（建議）補一個 `sync_positions({}, {})` 兩邊皆空的顯式測試，釘死首輪 no-op 行為（步驟 7，非阻擋項）。

### B. 使用者要做／要簽的（人工決策）

1. **準備一顆全新專屬錢包**（本場景前提，也是安全承重點）：確認**只有 Filet 一個引擎會碰**，無其他 agent／自動化。存入 1000 USDC 到**主 dex perp**（不是 spot——若錢落 spot，onboarding funded 判 False，需自行 spot→perp 劃轉，我方結構上無法代做，`hl.py:81-100`）。
2. **主網簽 ApproveAgent**（步驟 2）：透過我方前端（拿 keysvc 產的 agentAddress）用主錢包簽，前端直送 HL 主網。
3. **主網簽 ApproveBuilderFee**（步驟 3）：同上；maxFeeRate 0.1%（實收 0.02%）。**主網從沒簽過，testnet 簽過的不算。**
4. **開 live 是人工決策**：改 env 那兩個值＝真錢主網跟單的拍板，改後第一輪務必人工盯 `journalctl -fu filet-follower@<id>`。

---

## 最可能斷的一步 ＋ 為什麼

**步驟 3（ApproveBuilderFee），透過「web 不可達」而斷。**

- ApproveAgent（步驟 2）萬一我方前端出問題，理論上還有 HL app 的 API-wallet 路徑當備援（推測）。
- **ApproveBuilderFee 據我所知沒有 HL 官方 app 的對應 UI（API-only 簽章動作）**——一旦我方 web onboarding 不可達（TLS 自簽／防火牆未開，前一份文件 P0.4 標記未做），使用者**沒有任何第一方替代路徑**完成 builder 授權，直接卡死。
- 疊加放大：即使 web 可達，若伺服器 `FILET_API_NETWORK` 還停在 testnet，這步會**靜默錯**——端點回 200、使用者簽了一個 `hyperliquidChain="Testnet"` 的 payload、送到 testnet 成功，主網 `maxBuilderFee` 仍是 0。使用者以為簽完了，實際上主網跟單一開始就「成交但 builder fee 永不累計」，且沒有明顯報錯。
- 這步的失效方向最差：**要嘛完全卡死（web 不可達），要嘛靜默錯（server 沒切 mainnet）**，而它又是整個商業模式收入的來源（builder fee）。

次可能斷：**步驟 5（leaders.json 缺）**——但它是**大聲失敗**（引擎拒啟動），比步驟 3 的靜默錯容易發現。

---

## 一句話結論

**引擎核心邏輯與工具鏈都就緒**（onboarding 三端點、builder-fee typed data、crypto-only 鏡像、kill switch、成本閘、非託管、白名單二次驗證均已實作且離線測試綠），**本次「全新專屬錢包」場景也消除了前一份文件的頭號封鎖項（兩引擎共帳戶）與狀態根污染**；但**明天仍不能直接上線**，至少缺四件我方前置：**(a) 建 mainnet leaders.json 含此 leader**、**(b) filet-api 切 mainnet 重部署**、**(c) 修好 web TLS/防火牆讓 builder 授權可達**（最脆弱、無後路）、**(d) builder 補餘額＋follower env 補 Telegram**。伺服器實機狀態（FILET_API_NETWORK 現值、leaders.json 是否已在機上、TLS/防火牆）本審查刻意未 SSH 查證，上線前需人工在機上核對。
