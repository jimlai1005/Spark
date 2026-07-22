# Filet 上線前最終對抗性審查（2026-07-22，fresh opus reviewer）

**一句話總評：找不到「還沒被抓到的碰錢邏輯洞」——引擎程式碼本身可上線；不能安全 dogfood
的原因純粹是三個既有的營運/設定阻擋（頭號=共用錢包），與前三份報告一致。另外實查
prod 解掉/更新了三處文件矛盾，並發現一個「安全網尚未武裝」的新營運 gap。**

方法：親自讀核心碰錢路徑（positions / killswitch / equity / config / loop / sizing /
costbreaker / builder_compliance）、SSH 唯讀查 prod 實際狀態、外部驗 TLS、對 5 個碰錢
不變量做變異測試。全程逐檔 `cp` 備份還原，收尾 `git status --porcelain` 證明零改動。

證據紀律：以下每條實跑輸出可複驗。SSH 指令只讀不寫、未讀 .env/telegram.env/私鑰內容。

---

## Critical：無

六輪審查 + 今晚 testnet 端到端已把引擎硬化到很高水準。這一輪**沒有發現任何新的
Critical 級程式碼洞**。混源比較、兩進程推導路徑、保護靜默失效、文件災難、驗證失效
五類反覆出事型態，逐一再掃一遍，engine 內未見 N+1 實例（細節見下）。

---

## 五類反覆 bug 的 N+1 掃描結果

### 第 1 類 混源比較 — 未發現第 5 個
- `equity.perp_equity_view`：current 與 peak 同源 `get_account_value`（`equity.py:162,179-186`）。
- scale 分子分母：`compute_scale_factor` 用 `my_state.account_value` / `leader_state.account_value`
  （`loop.py:166-167`）。實查 adapter：`get_account_value` 讀 `marginSummary.accountValue`
  （`hyperliquid.py:44`），`get_account_state` 讀 `ms["accountValue"]`（`hyperliquid.py:168`）
  ——**同一 endpoint 同一欄位**，同源確認。
- 成本熔斷器：分子分母都限主 DEX perp（`costbreaker.py:186,207-211`），雙向同基準。
- builder 合規：`get_account_value`＝perp accountValue，與官方門檻同量（`builder_compliance.py:112-117`）。
- 變異測試證實 costbreaker 的分子範圍過濾有測試咬（見下）。

### 第 2 類 兩進程各自推導同一路徑 — 未發現第 4 個
交換目錄 / 狀態根 / 白名單路徑 / keys 目錄皆走「必填無預設 fail-closed」
（`filet-follower@.service:15-30`、`require_exchange_dir`/`require_leaders_path`）。
實查 prod：filet-api 與 follower unit 的 `FILET_LEADERS_PATH`/`FILET_EXCHANGE_DIR`
逐字元一致（見矛盾 3）。

### 第 3 類 保護靜默失效 — 未發現新的「該擋沒擋」
- killswitch lock-first、ARM 寫失敗上拋不吞（`killswitch.py:198-208`）。
- 成本熔斷器 fills 查不到沿用上輪判定、截斷 fail-closed（`costbreaker.py:343-376`）。
- builder 合規三態、未知一律 fail-closed（`builder_compliance.py:79-82`）。
- 但見下方「Important：安全網尚未武裝」——不是靜默失效，是監控範圍目前為空集合。

### 第 4 類 文件會造成災難 — RUNBOOK 未見第 5 個地雷
follower.env 範本出貨安全值（testnet/live=false，`follower.env.example:15-16`）、rsync
exclude 是機密邊界且 `--exclude var` 保護白名單、`uv.lock` 兩段都 exclude、network 佔位符
（`RUNBOOK.md:161-213,294-295,630`）——前述地雷都已拆。

### 第 5 類 驗證本身失效 — 變異測試：5/5 全被咬，無裸露不變量
逐檔 `cp` 備份 → 植入變異 → 跑對應測試 → 還原。實跑輸出：

| # | 變異（碰錢不變量） | 檔案 | 結果 |
|---|---|---|---|
| 1 | `_in_perp_scope` 恆真（分子混入現貨/非主 DEX） | costbreaker.py:186 | **4 tests FAILED**（`test_spot_*`、`test_*_inflate_perp_turnover`）|
| 2 | 回撤門檻 `>` 改 `>=`（門檻語意鬆動） | killswitch.py:83 | **1 FAILED**（`test_check_drawdown_exactly_at_threshold_not_breached`）|
| 3 | `resolve_capital` 無視 allocated 上限、用全權益 | sizing.py:76 | **3 FAILED**（`test_allocated_*`、`test_fixed_mode_ignores_equity`）|
| 4 | 移除 use_full_equity/allocated 矛盾拒絕 | config.py:312 | **2 FAILED**（`test_contradictory_env_refuses_to_start` 等）|
| 5 | 成本熔斷時把「leader已平跟平」也擋掉（困住客戶，違 D5） | positions.py:277 | **2 FAILED**（`test_d5_gated_positions_still_flatten`、`test_follow_flatten_runs_during_breach`）|

變異 5 值得記一筆：它在 `test_copy_sync_positions.py`（28 passed）**存活**，是被
`test_copy_costbreaker.py` / `test_cost_breaker_spec.py` 咬住的——D5「reduce-only 一律放行」
的不變量測試住在成本熔斷器的整合測試檔，不在 positions 單元檔。覆蓋存在，位置合理，不是漏。

基線：變異前後全套皆 `1755 passed, 2 deselected`（實跑，非「應該會過」）。

---

## 矛盾清單（文件 / 碼 / 部署三者，哪個對）

### 矛盾 1：TLS/防火牆——go-live-summary 對，另兩份過期
- `mainnet-readiness.md:68` 與 `open-items.md:26-28`：「TLS 自簽、80/443 未開、web 可能連不上」。
- `go-live-summary.md:59-60`：更正為「已開雲防火牆、Let's Encrypt 真憑證、到期 10/17」。
- **外部實查（我親跑）**：`https://52-197-137-3.sslip.io/` → **HTTP 200**；`http://` → **301**
  （轉 HTTPS）；`openssl` 憑證 = **Let's Encrypt，CN=52-197-137-3.sslip.io，
  notBefore Jul 19 / notAfter Oct 17 2026**。
- **裁決：go-live-summary 正確；mainnet-readiness 與 open-items 在此點過期。** web onboarding
  可達性不再是阻擋項（用 sslip.io hostname，不能用裸 IP——那只會憑證名不符，非防火牆問題）。

### 矛盾 2：leaders.json——存在，但是 testnet 遺物且含 follower 錢包當 leader
- `mainnet-readiness.md:82`：「repo 內 find leaders.json 查無此檔」。
- **實查 prod**：`/opt/filet/spark/var/filet/leaders.json` **存在**（root:root 644，Jul 21），
  內含兩個 enabled+accepting_new 的 leader：`0xf97ad6…ddd1`（目標 leader）**與
  `0xbac652…3662`（＝ follower/Momentum 錢包本身！）**。
- **裁決**：readiness「查無此檔」指的是 **repo 內**（正確——它在 gitignore 的 var/ 下，只在
  server）。**但這是 testnet 佈景**：3662 同時被列為可選 leader。主網 dogfood 換新專屬
  follower 錢包時，這份白名單必須**重建**，且**絕不可把 3662 帶進去**（否則有人可能誤選）。

### 矛盾 3：filet-api 缺 FILET_LEADERS_PATH——open-items 過期，已補上
- `open-items.md:92-94`：「伺服器 filet-api.service 缺 FILET_LEADERS_PATH → 下次重部署拒啟動」。
- **實查 prod**（`systemctl show/cat filet-api`）：filet-api unit **已有**
  `FILET_LEADERS_PATH=/opt/filet/spark/var/filet/leaders.json`。
- **裁決**：open-items 該項「未解決」過期（Jul 21 端到端里程碑已重部署）；其 ⚠️「需 SSH 查證」
  的保留是對的。

### prod 一致性（無矛盾，確認三份報告）
- `FILET_API_NETWORK=testnet`、`FILET_BUILDER_ADDR=0xbAC652…3662`（testnet builder==follower 錢包）
  ——與「伺服器是 testnet、切主網要重配」完全一致。
- **無 filet-follower@ 單元在跑**（follower 停著，合規）。四常駐服務 active、三 timer active、
  `systemctl --failed` 空、日報今日 00:22 `Result=success`。
- 非 git repo、無 `DEPLOYED_COMMIT`（open-items #16 技術債，確認屬實）。

---

## Important（新發現，非程式碼 bug，是營運 gap）

### I-1 builder 餘額的「日報監控安全網」目前武裝在空集合上
- `go-live-summary.md:34` 為薄 builder 餘額（0x81E9 只有 110 USDC）背書：「雖然有日報
  Telegram 監控接著」。
- 但日報的 builder 合規只查 **mainnet** builder：`_mainnet_builders` 濾 `r.network=="mainnet"`
  （`filet_daily_report.py:92-99,197-202`）。
- **實查 prod followers.json**：唯一一筆 follower 是 **testnet**（user 0xfb9c52 跟 leader 3662、
  builder 3662）。故目前 `mainnet_builders = 空集合` → **builder 合規檢查現在監控 0 個 builder**。
- 觸發情境：把 builder 0x81E9 加值到 110、切 config 之後，**在把 mainnet follower 寫進
  followers.json 之前**，builder 餘額完全無監控；寫進去之後也只有每日 00:20 UTC 查一次。
- 修法：不要在 dogfood 首日把「餘額跌破 100 靜默斷 fee」的防護寄望在日報上——(a) builder
  加厚到 200+ 留緩衝（三份報告都已建議）；(b) 首日人工盯 builder 餘額；(c) 確認 mainnet
  follower 一註冊，其 builder_address 就進入日報監控。日報 Telegram 接線本身**已驗證存在**
  （`EnvironmentFile=-/etc/filet/telegram.env`，該檔 640 root:filet-engine 存在）。

---

## Minor / 觀察

### M-1 「使用者是否已入金」兩條路徑用不同常數（latent divergence）
- M1 腳本路徑 `onboarding.py:69` 用 `MIN_BUILDER_BALANCE`(100) 當 user funded 門檻；
  M2 dashboard 路徑 `app.py:1143` 用 `cfg.min_user_deposit`。兩個「使用者入金門檻」常數不同源。
- 觸發情境：只有當 `min_user_deposit != 100` 且客戶餘額落在兩者之間才分歧。dogfood 走 M2
  且本金 1000 遠高於兩者 → 明天不會觸發。屬 open-items #18（M1/M2 分歧）的一個具體點，記之。

### 觀察：prod 全面是 testnet 佈景，切主網是「全部重配」不是「改一個開關」
followers.json / leaders.json / FILET_BUILDER_ADDR / FILET_ADMIN_ADDRESSES 目前都指向
testnet 錢包（3662、fb9c52）。與三份報告一致，非新問題，但提醒：切主網的動作面比「改
FILET_API_NETWORK」大——builder 位址、白名單、follower manifest、狀態根都要換。

---

## Assessment：能不能上線 dogfood 真錢？

**引擎程式碼可上線**——碰錢邏輯、防護（回撤/成本雙閘、非託管、crypto-only 鏡像、
lock-first kill switch）、同基準不變量、驗證覆蓋都禁得起這一輪對抗與變異測試。

**但今天 AS-IS 不能安全開 live**，原因與前三份報告完全相同、非新增，全部是營運/設定：

必修清單（沿用三份報告，我實查後確認仍成立）：
1. ⛔ **換一顆專屬 follower 錢包**——3662 昨天還在被 Momentum 交易（兩引擎共帳戶必互毀）。
   這是頭號、不可調參數解決。
2. ⛔ **伺服器/實例切 mainnet**：`FILET_API_NETWORK=mainnet` + `FILET_BUILDER_ADDR=0x81E9…`；
   在主網重走 ApproveAgent + ApproveBuilderFee（實查 follower 主網無 Filet agent、
   maxBuilderFee=0）；builder 加厚到 200+。
3. ⛔ **follower env + 白名單 + 狀態根就緒**：`COPY_TG_BOT_TOKEN/CHAT_ID` 必填（否則 kill
   switch 靜默）；**重建 mainnet leaders.json（含目標 leader、剔除 3662）**；新錢包＝新
   account_id＝狀態根天然乾淨。
4. 🟡 知悉 I-1：dogfood 首日別把薄 builder 餘額的防護寄望在日報上，人工盯。

（法遵 gate 對「自有錢包 dogfood」屬判斷題，非本次技術審查範圍，沿用 open-items #1 的定位。）

---

## git 乾淨證明

收尾 `git status --porcelain`：
```
?? docs/.DS_Store
?? docs/gtm/
```
兩者皆為審查開始前就存在的 untracked 檔（非我造成）。**所有變異實驗已逐檔還原，
零原始碼改動殘留。**
