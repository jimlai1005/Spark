# 主計畫表：部署 → 營運後台 → Billing → 多 leader → slider

日期：2026-07-19 起｜授權：使用者已全權授權自主執行（多日連續，遇 token 限額暫停後自行喚醒續作）
**本檔是長時執行的錨點**——context 壓縮後由此恢復進度。

## 使用者已拍板

| 項目 | 裁決 |
|---|---|
| 定價結構 | **方案 B**（免費層＋月費）。付費解鎖「多 leader」「比例 slider」 |
| 免費層限制 | **A3：免費版不受限制**。Billing UI 先推出，多 leader/slider 開發完成後再開啟其頁面 |
| 營運儀表板 | 放 `/ops` 同一個 Next.js app（非獨立應用） |
| 部署 | Lightsail 已開：`ssh -i ~/Downloads/LightsailDefaultKey-ap-northeast-1-spark.pem ubuntu@52.197.137.3`（Ubuntu 22.04.5 / 2 vCPU / 2GB / 58GB，東京） |

## 執行紀律（每個 Phase 都適用）

- 逐 task 派 subagent（機械照抄→haiku，需理解→sonnet），指令寫到「不需推理」的程度
- **指令必含**：「照抄後測試失敗就回報，**絕不修改斷言遷就**」——這條是本專案兩次抓到假完成的關鍵防線
- 每 task 完成後**指揮官親自檢查**（不只看測試綠：逐項對規格、必要時變異測試）
- ⭐ 碰錢／碰安全的 task 加 opus 對抗性審查
- 一次只有一個 agent 在 commit（避免 git race）；review 唯讀可併行
- 每個 Phase 結束：全套測試＋lint＋實機驗證＋更新本檔進度

---

## Phase 0：部署前置與上線 【已完成，剩一個使用者動作】

- [x] P0.1 Next.js CVE-2025-29927 bump → 15.5.20（commit 0d4877d）
- [x] P0.2 部署到 Lightsail（rsync 推碼，HEAD 5a4ede2）
- [x] P0.3 實機驗收**全數通過**：filet-api 生得出 key 但讀不到 key 檔（Permission denied）、SO_PEERCRED 拒絕非白名單 uid、後端埠外部不可達（且只綁 127.0.0.1）、http→https 301、安全 headers 齊全
- [x] P0.3b **實際重開機測試**：20 秒回來、四服務自啟、socket 重建、零錯誤
- [ ] **P0.4 ⚠️ 需使用者操作**：Lightsail Console → Networking → 開 IPv4 firewall 的 **80 與 443**（雲防火牆獨立於 ufw，目前只開 22）。IAM user `claude` 無 lightsail 權限，無法代勞。
      開完後跑：`sudo certbot --nginx -d 52-197-137-3.sslip.io --redirect --agree-tos -m jimlai1005@gmail.com` 即可把自簽換成真憑證。
- [x] P0.4b 修 repo 的兩個真 bug（keysvc 缺 WorkingDirectory、nginx 1.25 語法）＋ RUNBOOK 8 處修正
- [ ] P0.5 升 wagmi/viem 修 20 個錢包相依漏洞（4 high）→ 之後重新部署前端

**部署現況**：testnet 模式、keystore 空、billing 停用、TLS 為**自簽**（LE 因雲防火牆擋住 HTTP-01 而失敗）。
**容量實測**：四服務合計 203MB，available 1372MB，每 follower 約 55-60MB → **可容納 15-20 個 follower**（先前估 8-12 太保守）。磁碟 6G/58G。
**待重新部署**：伺服器上是 5a4ede2，本地已推進到更新的 commit。P1/P2 完成後一併重佈。

**已知風險：TLS 需要網域**。session cookie 是 `secure=True`，純 http（IP）登入會失敗。
處置順序：① 試 `52-197-137-3.sslip.io` 之類的 wildcard DNS ＋ Let's Encrypt（真實憑證）
② 失敗則自簽憑證（瀏覽器警告但功能可用）③ 兩者皆不可行 → **叫醒使用者**要網域。

---

## Phase 1：ops 後端 ＋ 客戶損益 ＋ 收入對帳

- [x] P1.1 `/api/ops/*` admin-gated ⭐（commit 90e85b7）——結構性測試遍歷 FastAPI 相依樹驗證所有 ops/admin 路由都掛閘（**變異測試確認**：拿掉閘 → 2 個測試立刻紅）
- [x] P1.2 客戶損益（資料層 5a4ede2：`UserFill` 加 `builder_fee`、`FollowerSummary` 加 notional／builder_fee；**變異測試確認**「北極星不加總」紅線受保護）
- [x] P1.3 收入對帳（含 174a2d3：應收 0 但實收非 0 時仍告警，不因算不出比例而靜默放行）
- [x] P1.4 `/ops` 前端頁（commit 166d704，前端 102 tests）

**Phase 1 完成**。實作過程中 subagent 對規格提出的四項有理反駁均已採納：accrued 來源改讀歷史檔（不擴大 HLGateway 唯讀面）、fills 窗口釘成與 accrued 同一 UTC 日（同基準，工程原則 1）、manifest 改 tolerant 載入（一個壞條目不得讓整張報表 500）、缺歷史時不硬算（避免把累積量當單日增量）。

**待辦（Phase 1 尾巴）**：ops customers 端點支援 `day=YYYY-MM-DD`——目前收入對帳固定取「最新 accrued 快照的 UTC 日」、客戶表卻是 now 往回 N 天，**兩張表的 builder fee 不可相減**（前端已標註但仍有誤讀風險）。

**為何先做**：不依賴定價與律師；客戶損益數據**直接餵未來的定價微調**。

---

## Phase 2：Billing UI 全套 ＋ 訂閱對帳 【實作完成，修復中】

- [x] P2.1/2.2 plans + portal（commit 9688243）
- [x] P2.6 訂閱對帳（commit 1d3261f）——**補上 opus 點名的 reconcile 缺口**
- [x] P2.3-2.5 `/pricing`＋`/billing`＋Header chip＋checkout 接線（commit 61ce6f6，前端 130 tests）

**旗標**：billing env 未設時後端 501、前端整組隱藏——可安全合併主線。

### opus 對抗性總審結果（Phase 1+2）：NEEDS_FIXES
**授權與資料外洩查無問題**（opus 不信任既有測試，自行枚舉全部 18 條路由驗證）：4 條 admin 端點全掛閘、僅 5 條 session 豁免（3 auth＋webhook HMAC＋plans 純記憶體常數）、`customer_id`/`subscription_id` 不進客戶端、白名單投影確為白名單。

必修（修復中）：
- [ ] **C1 Critical 同基準**：accrued 快照只存日期不存時刻 → 增量涵蓋 `(D-1 T, D T]` 卻與 `[D 00:00, now]` 的 fills 相比。**實測健康帳戶被判 199 倍差異並告警**。修法＝存 `captured_at` 並用它當窗界；缺時刻的舊資料標 `basis_unknown` 不硬算。**與 phantom drawdown 事故同形狀。**
- [ ] **I2 回鍋客戶假漏財**：Stripe `status="all"` 永久回舊訂閱，同 account 被算兩次（新的 in_sync、舊的落進「收不到錢」清單）→ admin 可能停掉正常付費客戶。修法＝先精確比對、第二輪才 metadata fallback。
- [ ] **I3 重複結帳**：webhook 落地前可建第二個 session → 兩張訂閱兩次扣款。測試模式無真金流故非 Critical，**開真收費前必修**，現在就補 pending-checkout TTL 擋板。
- [ ] I4 drift 無前端（`truncated` 不可信警告到不了人眼前）
- [ ] M5 admin 閘測試靠路徑前綴（換前綴即失效）→ 改釘**資料來源**（`list_billing`/`customer_pnl`/`_load_followers`）
- [ ] M6 `postcss` overrides；M7 白名單多收了沒人用的 `customer` 欄位

**⚠️ 順序**：`day=` 對齊**必須等 C1 修完**——否則兩張表只是「看起來」同基準而實際仍錯開，比現在更危險（opus 指出）。

---

## Phase 3：多 leader（付費功能一）

**架構決策（我裁決，使用者可推翻）**：「多 leader」實作為**從策劃清單中選一個 leader**（可切換），非「同時跟多個」。理由：同時跟多個需跨 leader 資金配置邏輯＋每 leader 一個引擎進程（55-60MB），2GB 機器上 5 客戶×3 leader 即滿載。資料模型不排除未來擴充。**若使用者要的是「同時跟多個」，需重新規劃。**

**資安設計**：客戶之後可經 API 改 leader → API 被打穿即可指向惡意 leader（瘋狂交易榨乾 builder fee／反向交易）。**防線＝策劃白名單，且引擎使用前必須自己再驗一次**（不得因「API 已驗過」而省略）。

- [x] P3.1a 白名單＋manifest leader 欄位（commit 9459279，915 tests）——向後相容、activate 硬閘拒絕非白名單 leader
- [ ] P3.1b `pending.json` 支援 `leader_address`（**目前 `write_pending_entry()` 無此參數，客戶選 leader 的鏈路斷在這裡**）
- [ ] P3.2 引擎讀 per-follower leader ⭐ + **白名單二次驗證**（目前只有 activate 一道閘）
- [ ] P3.3 leader 目錄 API（清單＋leaderboard 統計，資料來自已在跑的 watchlist snapshot）
- [ ] P3.4 客戶選 leader 的 API（訂閱狀態 gate）⭐ **必須帶客戶 SIWE 簽章，見下方**
- [ ] P3.5 `/leaders` 前端頁

### ⭐ 換 leader 的信任錨：客戶簽章（opus 審查逼出的架構決定）

**我的決定（使用者可推翻）**：P3.4 的換 leader 請求**必須攜帶客戶的 SIWE 簽章**（含 nonce 與目標 leader 位址），引擎驗章後才接受。機具已存在（`src/spark/publicapi/siwe.py`）。代價＝每次換 leader 要簽一次錢包，有 UX 摩擦。

**為什麼值得這個摩擦**——白名單回答的是「這個 leader 一般而言可不可接受」，**完全沒有回答「這位客戶真的要求換到他嗎」**。opus 找到兩個白名單結構上擋不住的攻擊：

1. **合法 leader 之間的無認證重導向**（比抖動嚴重）：能寫 manifest 的人可把 follower 指向白名單內、但對他完全不適合的 leader（例如 10x 槓桿高波動策略）。白名單全程放行，因為那個 leader **確實**在白名單裡。**一次切換就能造成實質損失**，不需要抖動。
2. **抖動**：在合法 leader 之間高頻切換，每次都付真實 taker 成本。（成本比原估低——`positions.py:188-243` 對重疊 coin 走差額調整而非平掉重開。）

**⚠️ 激勵錯位（最該讓使用者知道的一點）**：客戶每次換 leader 產生的額外成交，都會讓**我方的 builder fee 收入增加**（`accrued` 鍵在 builder 位址）。**從 churn 中獲利的那一方，正是被指定來守住 churn 的那一方。** 這是為什麼客戶簽章比任何營運端冷卻期都更該優先——守門人不該是受益人。營運端冷卻期仍值得做（引擎是唯一知道成本是否真的付出去的元件），但當成唯一解就是把守門責任放在守不住的位置。

**第三個建議（opus 認為最值得先做，尚未實作）**：**成本熔斷器**——對「每日複製成交筆數／累計 taker 成本佔權益比」設上限，超過即停開新倉並告警。好處是**與 leader 是誰無關**：抖動、白名單內的自營對敲、單純一個換手率爆炸的合法 leader，全被同一道閘門蓋住。這是原則 5 的味道（結構性，不靠人記得檢查）。**待使用者裁決是否納入 P3/P4。**

### 威脅模型校準（誠實標註，opus 查證）
我原本論證的攻擊「filet-api 被打穿 → 竄改 manifest 的 leader」**在出貨組態下不可達**：`filet-api.service` 的 `ReadWritePaths` ＋ `ProtectSystem=strict`，而 manifest 在 root 擁有的目錄。攻擊者只寫得到 `pending.json`，而 pending→manifest 之間隔著 activate 的白名單硬閘**與一個人類**。**真正承重的控制是檔案系統拓撲**，引擎側二次驗證是成本近零的縱深防禦（值得留著，尤其若日後 manifest 搬家），但**不要高估它今天的邊際價值**。
另注（既有性質、非本次引入）：所有 follower 共用 `filet-engine` user 且 `/etc/filet/keys` 為共用目錄——引擎側任一進程被打穿即可讀**所有** follower 的 agent key。白名單對這條路徑零保護。

**注意**：最大架構變更且碰引擎核心。P3.2 必須 opus 審。

---

## Phase 4：比例 slider（付費功能二）

- [ ] P4.1 per-follower 資金設定持久化（`allocated_capital`／`capital_utilization`）
- [ ] P4.2 設定 API ＋「下一 cycle 生效」語意（**不做即時強制再平衡**，避免無謂 taker 成本）
- [ ] P4.3 引擎讀取 per-follower 設定 ⭐
- [ ] P4.4 slider UI（含「下一輪生效」的明確提示）

---

## Phase 5：付費功能開啟 ＋ 交易品質儀表板

- [ ] P5.1 `/pricing` 的付費功能從「開發中」改為可用；訂閱 gate 實際生效
- [ ] P5.2 交易品質聚合（TE／滑價／延遲／skipped，日報已有雛形）
- [ ] P5.3 系統健康面板（follower 進程、kill switch、樣本覆蓋度、快照時間、告警數）

---

## 需要叫醒使用者的情況（其餘一律自行決定）

1. 部署需要網域而 wildcard DNS 方案都失敗
2. 任何需要動主網／真錢的操作
3. 發現的問題需要改變已拍板的產品決策（定價結構、免費層政策）
4. 律師回覆需要調整架構
5. 連續兩輪修復同一問題仍失敗（依 judgment.md 的換路訊號）

## 進度紀錄

- 2026-07-19 ~05:0x：計畫建立；SSH 驗證通過
- 2026-07-19 ~06:3x：**Phase 0 部署完成**（全驗收+重開機測試通過）
- 2026-07-19 ~07:0x：**Phase 1 完成**（818 Python / 102 前端 tests）；deploy 檔 bug 修復 0b9c9a8
- 2026-07-19 ~08:0x：P0.5 wagmi 2.19.5/viem 2.55.2（342ee72，**4 個 high 全清**，簽名檔未動、14 條真密碼學測試綠）
- 2026-07-19 ~09:0x：**Phase 2 實作完成**（915 Python / 130 前端）；**opus 總審 NEEDS_FIXES**（C1 Critical 同基準＋I2/I3 Important，修復中）；**Phase 3 開工**（P3.1a 白名單落地）

### 給未來 session 的提醒
- 我方每一個關鍵宣稱都做過**變異測試**（故意改壞→確認測試轉紅）：admin 閘、北極星不加總、price_id 不外流、緊急平倉滑價、panic 來源、插針守衛。沿用這個習慣，不要只看綠燈。
- subagent 多次對規格提出**有理反駁並被採納**（免費卡不該標「即將開放」、`list[dict]` 無法承載 `truncated` 旗標、危害優先於分類整齊、`enabled` 不收字串）。**指令要留反駁空間，不要逼它照抄壞規格。**
