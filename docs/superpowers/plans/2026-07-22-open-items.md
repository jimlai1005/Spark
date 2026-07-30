# Filet 遺留項與待辦總盤點

日期：2026-07-22｜方法：掃過 plans / research / legal / deploy / 程式碼 docstring，能查證的以 `檔案:行號` 或 commit 查證。
狀態欄一律標**已解決 / 未解決 / 部分 / 已知可接受**，並註明依據；roadmap 勾選框與程式碼不一致處以**程式碼/commit 為準**（roadmap 勾選框多處未同步）。

> ⚠️ 重要時間差：`test-plan`（2026-07-20）的 §D 未驗證清單，有數項在 **2026-07-21** 已被後續 commit
> 解決（端到端里程碑 6d01f09、劃轉有部位驗證、部署產物測試 c1dd529）。本盤點以最新狀態為準，
> 並在文末「近日已解決（文件未同步）」列出，避免讀舊文件的人誤把已解決項當待辦。

---

## 一、上線阻擋（mainnet／收客戶前必須解）

### 1. 律師／法遵 gate — **未解決（諮詢文件已備妥，尚未取得律師意見）**
- 內容：`docs/legal/2026-07-19-法遵諮詢文件.md` 是給律師的 46 題諮詢文件，**尚未有回覆**。
- 最高優先兩題直接決定「要不要繼續投入開發」：
  - A 類（是否須執照／業務定性）— `docs/legal/2026-07-19-法遵諮詢文件.md:267-272,569`
  - B1（非託管是否足以避免「保管客戶資產」認定）— `:303-306,569`
- 排程關鍵題：**G3（封閉測試是否有法規空間）** — `:468-469,570`。答案決定「8 月下旬封閉測試」能否照跑；
  若封閉測試與公開營運同受規範 → **必須延後封閉測試直到法遵到位（排程延後約 2–3 個月）**。
- 自有策略作 leader（A5/A6，`:289-296`）可能改變定性 → 若是，放棄自有策略上架或獨立為另一產品線。
- roadmap 已列入「需叫醒使用者」#4（`master-roadmap.md:191`）。
- **邊界判斷**：主網 dogfood 用**自己的錢包**嚴格說非「收客戶」，法遵風險較低；**收真實客戶（含封閉測試）前這是硬 gate**。dogfood vs 收客戶哪個受 G3 約束，需律師定位。

### 2. TLS 真憑證／網域 + 雲防火牆 80/443 — **未解決（待使用者操作，需確認現況）**
- P0.4：Lightsail 雲防火牆目前只開 22，**80/443 未開**；IAM user `claude` 無 lightsail 權限無法代勞。
  開完才能 `certbot` 把自簽換真憑證。來源 `master-roadmap.md:32-33`。
- 風險：session cookie 是 `secure=True`，**純 http（IP）登入會失敗**；目前 TLS 為**自簽**（LE 因防火牆擋 HTTP-01 失敗）。來源 `master-roadmap.md:41-43`。
- 收真實客戶前需要真憑證（自簽會有瀏覽器警告）。兩者皆不可行 → 叫醒使用者要網域（wake-up #1）。
- ⚠️ 需確認：2026-07-21 端到端里程碑在部署環境登入成功（sslip.io + 錢包簽章），可能表示防火牆/憑證已有進展；**現況請向使用者確認**。

### 3. 主網 dogfood 全套 — **未解決（碰真錢，需使用者決策）**
- test-plan D-12：shadow 3 天、滑價 ≤10bp、隔日 CSV 對帳、熔斷實彈。來源 `test-plan.md:326`。
- 注意：**testnet 端到端已於 2026-07-21 跑通**（見文末），但**主網小額覆測仍未做**。
  modify 收費路徑的 testnet 樣本是 `builder == 使用者自身地址`、單筆成交；主網 builder≠user 情形未測。
  來源 `testnet-e2e-findings.md:153`、`spot-perp-transfer.md:485`。
- roadmap wake-up #2（任何動主網/真錢的操作）。

### 4. Stripe 真收費前必修的三個 billing 缺口 — **部分（C1 已修，I2/I3 待確認）**
- billing 目前 `billing_enabled=false`（前端整組隱藏、後端 501），故非 mainnet copytrade 的 gate，
  但**開真金流前必修**。來源 `master-roadmap.md:68,74-79`。
- **C1 同基準（accrued 快照 captured_at）→ 已解決**：程式碼已含 `captured_at` + `basis_unknown` 分支
  （`src/spark/publicapi/ops.py:756-803`，opus Critical 修法）。原症狀「健康帳戶被判 199 倍差異告警」。
- **I2 回鍋客戶假漏財**（Stripe `status="all"` 回舊訂閱被算兩次）、**I3 重複結帳**（webhook 落地前建第二 session → 兩次扣款，需 pending-checkout TTL 擋板）：roadmap 仍列 `[ ]` 未勾選，程式碼在 `src/spark/publicapi/billing.py`；**是否已修需向使用者確認**。來源 `master-roadmap.md:75-76`。
- I4/M5/M6/M7 為 Important/Minor（drift 前端、admin 閘改釘資料來源、postcss、多餘白名單欄位）。

---

## 二、營運重要（上線後短期要補）

### 5. builder fee 資格門檻：只看主 dex，跨 dex 加總未向 HL 確認 — **未解決**
- 監控（`builder_compliance.py`）用 `get_account_value(builder)`＝**主 perp dex** 的 `clearinghouseState.marginSummary.accountValue`（`src/spark/filet/builder_compliance.py:112-117`）。
- HL 已有多個 perp dex（SDK 有 `perp_dexs`/`query_user_dex_abstraction_state`，`hl-sdk-findings.md:139,142`）。
  官方「≥100 USDC」門檻是**只看主 dex 還是跨 dex 加總，未向 HL 確認**——目前監控只看主 dex，若實際是加總則可能誤判合規/不合規。

### 6. builder 資格「≥100 USDC + standard mode」監控已接日報，但 `default` 語意未經官方確認 — **部分（保守處理）**
- 監控已實作並接 Telegram（`filet_daily_report.py:214-241`、`builder_compliance.py`）。
- `default` 模式**刻意一律視為不合規**（只放行 `"disabled"`），因語意未經官方確認，猜錯代價不對稱。
  來源 `builder_compliance.py:22-36`、`spot-perp-transfer.md:395,520`。**已知可接受的保守取捨**，但拿到官方確認前會多發人工告警。

### 7. Telegram BotFather token 已入對話歷史 — **未解決（營運安全）**
- 今晚設定用的 telegram bot token 已進入對話歷史（`build_notifier` 讀 `COPY_TG_BOT_TOKEN`，`filet_daily_report.py:276-287`、follower 引擎 `run_copytrade.py:60`）。
- **建議 BotFather `/revoke` 重產 token**，並更新 `/etc/filet/followers/*.env` 與日報 unit 的 EnvironmentFile。

### 8. day=YYYY-MM-DD 對齊（Phase 1 尾巴）— **未解決（前置 C1 已完成，可動工）**
- 現況：收入對帳取「最新 accrued 快照的 UTC 日」、客戶表卻是 now 往回 N 天 → **兩張表 builder fee 不可相減**（前端已標註但仍有誤讀風險）。來源 `master-roadmap.md:56,81`。
- roadmap 註明「必須等 C1 修完」；C1 已修（見 #4），故此項現在可安全實作。

### 9. builder fee「已向客戶收取」≠「已入帳至 builder」對帳 — **未解決（上線後第一週對帳項）**
- testnet `userFills.builderFee` 只證明**已向客戶收取**，未直接證明已撥入 builder `builderRewards`。
  唯一閉合法：主網上線後拿一位 unifiedAccount 客戶成交去對 `query_builder_accrued` 增量。
  來源 `spot-perp-transfer.md:485,507`。建議列上線後第一週，非上線前阻塞。

### 10. spot 卡住提示只在 onboarding 頁，已活化客戶看不到 — **未解決**
- 「錢卡在 spot」提示只畫在 onboarding（`web/src/app/onboarding/page.tsx:70,118-120`）。
  已完成 onboarding 的客戶日後從 CEX 提幣落在 spot 時**看不到提醒**（我方只鏡像 perp）。
- 相關建議（唯讀、零風險，尚未接）：dashboard 用 `info.spot_user_state`/`info.user_state` 偵測 stranded spot、
  `info.query_user_abstraction_state` 判斷客戶是否需劃轉。來源 `spot-perp-transfer.md:149,236`。

### 11. leaderboard perf-series 90 天序列無法回填 — **部分（採集器已上線，樣本 n=1）**
- ⚠️ 採集器**已上線**：`filet-perf-series.timer` enabled 並實跑成功（`master-roadmap.md:164`）。
- 但 `perf_series` 實測**只有 1 筆**（單一 leader、1 行 jsonl、12h 取樣，`test-plan.md:325`）。
  **結構性限制：今天不開始，90 天後仍沒有 90 天資料**——這是「已在跑但需要時間累積」，非缺工。
- 相關：`allTime` 窗是降採樣（約 93 點，未實測，`leader-performance-metrics.md:134`）→ 不可用來算 MDD/Sharpe，
  這正是必須自建拼接序列的理由。

### 12. 成本熔斷器門檻未經真實資料校準（n=1）— **部分（機制已實作，門檻待校準）**
- 熔斷器已實作，同基準分母已釘變異測試（commit 7a01280、`test-plan.md:131`）。
- 門檻預設值**未經真實跟單資料校準**（樣本 n=1，testnet 單次），依 D7 選保守值 + docstring 標註。
  來源 `cost-circuit-breaker.md:49-53`、`test-plan.md:320`（D-6）。累積真實資料後以日報回算換手率分布再訂。

### 13. 伺服器 filet-api.service 缺 FILET_LEADERS_PATH — **未解決（需確認 server 現況）**
- test-plan D-1：實測伺服器上 filet-api.service **缺** `FILET_LEADERS_PATH`（其餘三個 unit 都有），
  本機 HEAD 已列為必填且 fail-closed（commit 67c31ed）→ **下次重新部署 filet-api 會拒絕啟動**。來源 `test-plan.md:315`。
- ⚠️ 2026-07-21 端到端里程碑在部署環境跑通（含換 leader 鏈路），可能已重新部署；**server 當前 unit 內容請以 SSH 查證為準**。

### 14. 換 leader 對帳 + kill switch critical 告警接線 — **已解決（兩者皆已接 Telegram）**
- 日報接線：builder 合規 + 換 leader 對帳 → `notifier.critical`（`filet_daily_report.py:232-260`）。
- **follower 引擎 kill switch critical 也已接**：引擎 `run_copytrade.py:60-61` 依 `COPY_TG_BOT_TOKEN` 建 `TelegramNotifier`（多 follower 再包 `TaggedNotifier`），`killswitch.evaluate/trip` 呼叫 `notifier.critical`（`src/spark/copytrade/killswitch.py`）。follower.env 範本已備 `COPY_TG_BOT_TOKEN`/`COPY_TG_CHAT_ID`（`deploy/follower.env.example:29-30`）。
- ⚠️ 依賴：每個 `/etc/filet/followers/*.env` 需**實際填入** TG 憑證（範本是佔位符）；未填則降級為 NullNotifier（不推播）。屬部署檢查項。

---

## 三、技術債（可延後）

### 15. 前端 PerfShown 判定是語法啟發式非型別推論 — **未解決**
- `redline.test.ts` 用 regex 認 `const s = view.shown`/`x: PerfShown`，**換寫法就繞過**。
  來源 `test-plan.md:321`（D-7）。修法：branded type 或 AST 分析取代 regex。

### 16. 站台非 git repo，無版本檔，站台全綠 ≠ 本機 HEAD 可部署 — **未解決**
- 伺服器 `/opt/filet/spark` 非 git repo，無從得知跑的是哪個 commit（`test-plan.md:324` D-10）。
  建議部署時寫 `/opt/filet/spark/DEPLOYED_COMMIT`（`test-plan.md:382` E.4）。

### 17. dry-run 假件 modify 語意與真交易所分歧（F3/F4）— **未解決（測試保真度，非生產行為）**
- 假件 `VirtualBook.modify` 保留 oid，真交易所重配發 oid 且失敗非原子 → 假件給假信心。
  來源 `testnet-e2e-findings.md:155-171`。建議修 docstring + 假件也重配 oid；可自行處理。

### 18. M1 腳本綁託管式 onboarding，M2 非託管下靜默失效（F2）— **部分（documented，部分腳本 M1-only）**
- M1→M2（託管→非託管）使假設持有主鑰的既有工具靜默失效。
  建議盤點 `scripts/` 下其他仍呼叫 `get_main_signer` 的腳本，標註僅適用 M1 或改造。
  來源 `testnet-e2e-findings.md:84-100,190`。部分已判 YAGNI（測試路徑已被 dashboard onboarding + 改造探針取代）。

### 19. keysvc socket 協定只實作 generate op（address op deviation）— **需確認是否追認**
- keysvc 計畫實作為 "generate op only"（commit 訊息 `m2-keyservice.md:211`）；spec 拆解第 1 項。
  是否有 address op 相對 spec 的 deviation 已被明確**追認**，文件中未見明確記載 → 建議向使用者確認。

### 20. npm audit production 殘留 — **部分（4 high 已清，殘留待複查）**
- P0.5 wagmi 2.19.5/viem 2.55.2 已把**4 個 high 全清**（commit 342ee72，`master-roadmap.md:199`）；
  roadmap 勾選框 `[ ]` 未同步。**建議跑一次 `npm audit --omit=dev` 複查是否尚有 production 殘留**（本盤點未實跑 audit）。

---

## 四、已知可接受（刻意標註）

- **成本熔斷器不做強制平倉、reduce-only 一律放行**：刻意，回撤熔斷器才管平倉（`cost-circuit-breaker.md:39-42`）。
- **回撤保護「大聲告警但繼續跟單」（空樣本期）**：刻意取捨，已寫入法遵揭露（`法遵諮詢文件.md:197`、`testnet-e2e-findings.md:225` C1）；法遵 E3 題請律師評估是否需改。
- **15 分鐘取樣使 MDD 系統性低估**：已知極限，UI 須標註（`leader-performance-metrics.md:129`、`test-plan.md:286`）。
- **不足 90 天不年化 / leader 報酬率是跟單者上界非期望值**：誠信揭露要求，已釘變異測試 M1/W4。
- **`default` abstraction mode 視為不合規**：保守取捨（#6）。
- **域分隔前提（前端與 filet-api 不同信任域）**：C1 前端信任錨防線的前提；**動部署拓撲前必須重評**，否則靜默失效且無測試轉紅。來源 `master-roadmap.md:104-105`、`RUNBOOK.md:374,388`。
- **所有 follower 共用 `filet-engine` user 與 `/etc/filet/keys`**：引擎側任一進程被打穿可讀所有 follower agent key，白名單零保護（既有性質，非本次引入）。來源 `master-roadmap.md:135`。

---

## 近日已解決（文件未同步，避免誤當待辦）

- **端到端首次在部署環境跑通（2026-07-21）**：activate 三閘全過、引擎心跳實寫、開/加/平生命週期精確 0.40× 鏡像、reduce-only 平倉、builder fee 實收 0.0200%、換 leader 簽章鏈路驗證（過期正確不套用）、零告警。→ 解決 test-plan **D-2（端到端從未部署環境跑過）、D-3（引擎心跳）**。來源 `spot-perp-transfer.md:560`、commit 6d01f09。
- **有部位時 perp→spot 劃轉 verified clean（2026-07-21）**：100 USDC 只進 AV、不進 PnL，浮動盈虧如實反映；單一來源架構最終確認。→ 解決 **D-4**。來源 `spot-perp-transfer.md:547-561`。
- **spot→perp 劃轉被正確當入金扣除，pnlHistory 不受污染** → 解決 **D-5**（`spot-perp-transfer.md:301,308`）。
- **5 個裸露的部署產物不變量已補測試**：`tests/test_deploy_artifacts.py` 24 條斷言（follower 範本 testnet/live_trading=false、filet-api network fail-closed、rsync 擋機密/var/uv.lock 等）。→ 解決 **D-8**。來源 commit c1dd529。
- **C1 billing 同基準（captured_at）**：見 #4。
- **P0.5 wagmi/viem 4 high**：見 #20（殘留待複查）。

---

## 附：分類判斷的誠實標註
- 「未解決/部分/已解決」以**程式碼與 commit** 為第一證據，roadmap 勾選框多處落後，不採信其為狀態證據。
- 本盤點**未實跑** `npm audit`、未 SSH 查證 server 當前 unit 內容（#13/#2）、未確認 I2/I3 修復（#4）——這三處明確標為「需確認」，未當作已完成。
- 法遵是否構成 dogfood（自有錢包）的 gate 屬判斷題，已標明需律師定位，未自行裁定。

---

## 2026-07-25 mainnet 首航新增（上線當日實測發現）

- **引擎槓桿同步盲區（首航 CRIT 實例）**：`orders.py _set_entry_leverage` 的 `leverage_by_coin`
  只從 leader **持倉**推導；leader 空手＋純掛單網格時地圖為空 → 靜默跳過 → follower 停在
  新帳戶預設 20x → 保證金不足，ETH 階梯 6 筆只掛進 1 筆（CRIT「掛單重試後仍不符：缺少 5」）。
  當日處置：停引擎，走 adapter 代發 `update_leverage`（ETH 25x、BTC 40x cross）後恢復，18/18 掛齊。
  **修法方向**：leader 槓桿改由 `info` 的 `activeAssetData(user, coin)` 查詢（空手也查得到），
  對「desired orders 涉及的 coin」逐一同步；leader 有持倉時兩來源應一致（可互為驗證）。
  註：此為一次性 bootstrap 問題——follower 各 coin 槓桿一旦對齊，後續 leader 開倉路徑會接手同步。

---

## 2026-07-31 vault leader 支援殘餘（Wave 1-6 落地後的已知債，四筆）

- **自訂 registry 路徑無 vault 偵測 — 對外開放前必補**：owner 在 `/leaders` 頁自行輸入
  vault 地址走的是 user registry（§5.5.3），條目一律 `kind: "standard"`——**不會**獲得
  20x 帽與流量中性化；vault 目前僅精選白名單上架（RUNBOOK §5.5「vault leader 上架前置
  檢查」交叉註記）。與 CLAUDE.md 紅線 5 例外的「對外開放前必須重審」屬同一窗，同窗處理。
- **follower 側出入金校正債**：follower 自己的出入金仍會被回撤風控視為權益變動
  （`src/spark/copytrade/equity.py:16`「ledger-aware 的出入金校正延到 public beta」）。
  本次 leader 側落地的 `get_ledger_flows` ＋ `adjusted_leader_equity`（leader_flow.py）
  機制可直接複用到 follower 側。
- **運行中 vault→standard 換手不還原收緊值**：引擎每輪 `apply_vault_policy` 只收緊
  不放鬆——換回 standard leader 後 20x 帽與中性化不會自動解除。方向是**過度保護、
  非 fail-open**（錯的代價是保守），還原需要破壞「單一設定物件」不變式，留待使用者裁決。
- **簽章換手路徑的 transient kind 回退**：白名單暫時讀不到（transient 讀失敗）的窗口內
  `_kind_of` 查無條目回 `"standard"`（`src/spark/filet/leader_change_apply.py:122-131`
  註解）——vault 保護可能缺席一輪，下一輪白名單恢復即恢復。已知、方向短暫 fail-open，
  但窗口為單輪且需與 transient 讀失敗同時發生。
- **流量型別映射雙實作**：ledger delta type 的白名單＋計號邏輯在
  `src/spark/exchange/hyperliquid.py`（`get_ledger_flows`）與
  `scripts/vault_preflight.py`（`_signed_flow`／`ALLOWED_LEDGER_TYPES`）各自實作——
  語意目前一致，但一旦漂移，preflight 會繼續 PASS 而引擎算的是另一套。應抽單一函式共用。
- **heartbeat 未發布 leader kind**：vault 保護（20x 帽＋流量中性化）是否生效沒有觀測面——
  操作者無法從 heartbeat 確認引擎當下把 leader 當 vault 還是 standard，只能翻 log。
