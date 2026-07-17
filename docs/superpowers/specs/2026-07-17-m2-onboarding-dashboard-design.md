# M2 Onboarding 後端 + Dashboard — 設計文件

> 2026-07-17 brainstorm 拍板（使用者核准）。視覺 v1（鮭色）已定（`docs/superpowers/design/2026-07-17-m2-dashboard-prototype.html`），
> 產品流程沿 vault `filet/M2-closed-alpha-設計.md` 元件二。本文件聚焦**後端安全架構與前後端契約**。
> 這是 M2 設計的元件二（dashboard）＋元件三的 onboarding 後端部分；引擎多實例（元件一）已完成（`feat/m2-multiinstance`，546 tests）。

## 一句話

非託管 copytrading 的 onboarding：朋友用自己的瀏覽器錢包簽兩筆授權（ApproveAgent / ApproveBuilderFee），
我方生成並持有 trade-only agent key（無提款權），引擎據此跟單。**主鑰全程只在朋友的瀏覽器，永不進我方系統。**

## 不變量（凡設計衝突以此裁決）

1. ⭐ **非託管**：客戶主鑰永不落我方任何進程/儲存/log。授權由客戶瀏覽器錢包簽；我方只持 trade-only agent key（無提款/轉帳權）。
2. ⭐ **金鑰生成/寫入與對外 web 層拆開**：只有不對外的 key-service（本機 socket）能生成/寫 agent key；public API 與 dashboard 物理上不能寫金鑰。
3. ⭐ **agent key 不出 key-service**：生成後私鑰只經 `EnvFileKeyStore` 落檔（600），API 只拿到 agent **地址**；私鑰不進 API 回應/log/session。
4. **語言紅線**（沿 2026-06-18）：不得出現「固定收益／保證／存款／代操」。
5. M2 引擎全部工程紅線沿用（builder 參數強制、Decimal、測試離線、account_id 路徑安全——已於引擎層 keystore/registry 雙層驗證，本後端生成 account_id 時繼承 `validate_account_id`）。

## 元件拓撲（同一 Lightsail，分不同 OS user）

| 元件 | OS user | 職責 | **不能做** |
|---|---|---|---|
| Dashboard（Next.js） | filet-dashboard | 呼叫 API、顯示唯讀績效、wizard UI | 讀金鑰、直接簽任何鏈上動作 |
| Public API（FastAPI，Python，在 spark） | filet-api | SIWE 登入、產待簽 payload、查 HL(唯讀)、管理端核准 | **寫金鑰**（須經 key-service socket）|
| Key-service（daemon，Python，在 spark） | filet-engine | 生成 agent keypair → `EnvFileKeyStore` 寫 600；只回地址 | 對外 web（只聽本機 unix socket，socket 權限只允 filet-api 連）|
| 引擎 follower 進程 | filet-engine | 讀 agent key 跟單（元件一已完成） | 碰主鑰（`get_main_signer` 一律拒，已完成）|

- key-service 與引擎同為 filet-engine（都是金鑰擁有側）；agent key 檔 600、filet-engine 擁有 → filet-api/filet-dashboard 讀不到。
- key-service 的 unix socket（如 `/run/filet/keysvc.sock`）權限設為只有 filet-api 可連（socket 檔 owner filet-engine、group 含 filet-api、660）。
- **爆炸半徑**：web 層被打穿也碰不到金鑰生成/簽單；agent key 洩漏也僅 trade-only（無提款）。

## 非託管 onboarding 流程（後端產 payload、瀏覽器簽）

**步驟對照**（opus 審查 m2：釐清使用者 wizard vs 後端序列）：使用者面的 wizard 是**原型 v1 的 4 階段**（01 連接錢包 → 02 風險確認 → 03 簽署授權 → 04 入金啟用）；下面的 1–7 是後端**詳細序列**，其中步驟 7（activate）是**管理端動作、不在使用者 wizard 內**。前端狀態機照 4 階段蓋，斷點續走以 verify 查詢結果為準。

**關鍵差異**：M1 的 `spark.onboarding.onboard()` 用我方持有的 `main_signer` **伺服器端簽** ApproveBuilderFee/ApproveAgent——M2 非託管下主鑰在瀏覽器，**此路徑不沿用**。M2 後端改為**產出待簽 payload**，重用的只有：`EnvFileKeyStore`（存 agent key）、驗證查詢（`query_max_builder_fee`、agent 狀態、`get_account_value`）、HL SDK 的 action 建構。

1. **連錢包 + SIWE 登入**：dashboard 連 MetaMask/Rabby → API 發 nonce → 錢包簽 SIWE 訊息 → API 驗簽 → 建 session（httpOnly, Secure, SameSite=Lax cookie）。地址即身分（免密碼）。
2. **風險確認**：前端三勾（虧損風險／trade-only 無提款／費用說明）。全勾才進下一步。
3. **生成 agent**：`POST /onboard/{account}/agent` → API 經 socket 呼 key-service `generate(account_id)` → key-service 生成 keypair、`import_agent_key` 寫入、回 agent 地址 → API 回地址給前端。account_id 由 API 從登入地址衍生（見資料模型），生成前過 `validate_account_id`。
4. **產待簽 payload**：`GET /onboard/{account}/approvals` → 後端建兩個 EIP-712 typed-data（ApproveAgent 授權 agent 地址、ApproveBuilderFee maxRate 0.1% 給我方 builder 地址），依 HL SDK 的 action wire 格式建構。回 typed-data。
5. **瀏覽器簽 + 提交**：前端請錢包簽兩筆 → **前端直送 HL exchange endpoint**（簽好的 action 從瀏覽器直接送 HL，我方後端不經手已簽交易——最貼合非託管，後端只在步驟 6 驗證效果）。
6. **驗證**：`POST /onboard/{account}/verify` → 後端輪詢 `query_max_builder_fee(user, builder) != 0`＋agent 授權生效＋`get_account_value(user) ≥ 100 USDC`（builder 門檻，重用 spark verification 語意）。全過 → 狀態 READY（pending 核准）。
7. **啟用（管理端核准，不自動 live）**：verify 通過 → follower entry 寫 pending manifest（`user_address` **綁定已驗的 SIWE session 地址、非自由填入**；`builder_address` = **伺服器設定的固定我方 builder 常數、非使用者輸入**——opus 審查 m3，杜絕 web 被打穿後注入 builder 指向攻擊者的合法條目）。管理端（你）在 admin 頁**檢視** pending → **核准動作走人工 CLI**（`scripts/filet_activate.py <account_id>`，拉起 `filet-follower@<account_id>`）。**M2 不做「API 端點直接拉 systemd」**（opus 審查 M2：對外 filet-api 若能觸發 systemd start 需提權，被打穿即取得 unit 控制或提權路徑；比照 key-service，把危險 OS 動作收斂在人工 CLI，不暴露給 web 層）。COPY_LIVE_TRADING 依 config。

## API 契約（Public API，FastAPI）

| 端點 | 作用 | 回傳 |
|---|---|---|
| `POST /auth/nonce` | 取 SIWE nonce（綁地址+過期） | nonce |
| `POST /auth/verify` | 驗 SIWE 簽名 → 建 session | set-cookie；me |
| `POST /auth/logout` | 清 session | — |
| `GET /me` | 目前登入身分＋onboarding 狀態 | address, state |
| `POST /onboard/{account}/agent` | 生成 agent（經 key-service）| agent_address |
| `GET /onboard/{account}/approvals` | 兩筆待簽 EIP-712 payload | typed_data×2 |
| `POST /onboard/{account}/verify` | 驗兩筆授權生效＋入金 | state（READY/待補） |
| `GET /perf/{account}` | 唯讀績效（權益/部位/掛單/心跳/accrued/回撤）| 直查 HL + 引擎狀態快照 |
| `GET /admin/pending` ⭐ | 管理端：**檢視**待核准 follower 清單（唯讀）| list |

- **啟用（activate）不做成 API 端點**——走人工 CLI `scripts/filet_activate.py`（opus 審查 M2：不讓對外 web 層握有拉 systemd unit 的特權）。admin 頁只讀 pending 清單供你核對（尤其逐筆核對 builder_address）。
- 帳戶級端點驗 session 地址 == account 對應地址（授權檢查）。admin 端點限管理員地址白名單。
- 所有寫入端點（生成 agent、activate）非冪等的要有防重（生成 agent 對已存在者拒絕 rotate，沿 M1 approve_agent 語意——已有 key 就不重生）。

## Key-service socket 協定

- 傳輸：本機 unix domain socket（`/run/filet/keysvc.sock`，660，filet-engine:filet-api）。
- 唯一操作：`{"op":"generate","account_id":"<id>"}` → `{"ok":true,"agent_address":"0x..."}` 或 `{"ok":false,"error":"..."}`。
- key-service 內部：`validate_account_id` → 若 agent.key 已存在則拒絕（不 rotate，避免作廢既有授權）→ 生成 keypair → `EnvFileKeyStore.import_agent_key` → 回地址。**私鑰永不出此進程**（不回、不 log）。
- ⭐ **「絕不覆寫」須是寫入原語的結構性保證，非 TOCTOU 檢查**（opus 審查 M1）：`EnvFileKeyStore.import_agent_key` 目前用 `os.open(..., O_CREAT|O_TRUNC)`（無 `O_EXCL`），會靜默截斷既有金鑰——並行 generate 或誤呼會讓 keystore 的 key 與鏈上已 ApproveAgent 的 agent 地址失聯、引擎持一把簽不出有效交易的死 key。**實作時 `import_agent_key` 改用 `O_EXCL`（存在即失敗）**，把不覆寫變成原語保證，不倚賴呼叫端先查。（這是對元件一 envfile.py 的一個小加固，含測試。）
- socket 除 file mode 660(filet-engine:filet-api) 外，**加 `SO_PEERCRED` 檢查連線者 uid/gid**（縱深防禦，避開 umask race 與 group 混入假設）。
- 無其他操作（不提供讀金鑰、不提供簽名）——最小攻擊面。

## 資料模型

- **Session store**：session id → {address, expiry}。M2 用 SQLite（單檔零運維）。**nonce 單次使用**（用過即消耗，防有效期內重放）＋ **SIWE 訊息綁 domain/URI**（防跨站釣魚重放）——資料模型明列 nonce 表（nonce, address, expiry, consumed）。
- **account_id 衍生（定死）**：由登入地址**確定性衍生**——`"f" + 地址小寫去 0x 的完整 40 hex`（41 字元，仍 ≤64 過 `validate_account_id`）。**用完整 40 hex 不截斷**（opus 審查 m1：截前 16 hex 只用 64-bit，相異地址前 16 hex 相同即映射同一目錄/碰撞；完整 40 hex 對地址 1:1、零成本）。恆為 `validate_account_id` 合法（`^[a-zA-Z0-9_-]{1,64}$`），無使用者輸入、無路徑穿越（引擎 keystore/狀態目錄/systemd %i 都吃它）。朋友的可讀暱稱另存 `FollowerRef.label`（純顯示，不進路徑；**dashboard 顯示 label 須轉義防 stored XSS**）。
- **Follower manifest**：沿元件一 `var/filet/followers.json`（`FollowerRef`）。onboarding 完成寫 pending 條目；activate 後轉 active。per-follower 完整跟單參數（allocated_capital 等）走各自 env（元件一慣例）。
- **Pending 佇列**：manifest 內 `status: pending|active`，或獨立 pending 表。

## 錯誤處理

- SIWE 驗簽失敗／nonce 過期 → 401，明確訊息（不洩內部）。
- 生成 agent：key-service 不可達 → 500 + 大聲告警（不靜默）；已存在 → 明確「已有 agent，不重生」。
- 產 payload：builder 地址門檻未達（<100 USDC）→ 明確擋下（沿 M1 `BuilderNotEligible`，症狀是「成交但 fee 不累計」）。
- verify：授權未生效/入金不足 → 回可斷點續走的狀態（冪等，沿 M1 onboarding 狀態靠查詢的精神）；輪詢逾時 → 明確提示重試，不誤判成功。
- 任何金鑰路徑失敗大聲告警，絕不靜默（工程原則 3）。

## 測試

- **key-service**：金鑰生成＋`import_agent_key` 寫入＋socket 協定（離線，tmp keystore）；已存在拒絕重生；私鑰不進回應/log。⭐ **非託管不變量權限測試**：以 filet-api 身分（或模擬不同 user 的檔案權限）確認讀不到 agent.key。
- **Public API**：SIWE nonce/驗簽（含過期、錯簽、重放）；payload 建構正確性（對照 HL SDK 的 ApproveAgent/ApproveBuilderFee action wire 格式，assert typed-data 欄位）；session（cookie 屬性 httpOnly/Secure/SameSite）；授權檢查（跨帳戶存取擋下、admin 白名單）。mock HL，不觸網。
- **前端**：wizard 8 步流程、mock wallet 簽名、斷點續走；語言紅線 grep 零命中。
- **testnet 整合**：全流程（連 testnet 錢包 → SIWE → 生成 agent → 雙簽 → 驗證 → 管理端核准 → 引擎起）真跑至少 2 個不同錢包；agent key 落 keystore、引擎讀得到。
- **部署驗收**：filet-dashboard/filet-api user 對 agent.key 讀取必須失敗（結構性權限測試進部署腳本）。

## 部署（Lightsail：Ubuntu 22.04、2GB/2vCPU、東京）

- systemd units：`filet-api.service`（filet-api）、`filet-keysvc.service`（filet-engine，聽 socket）、`filet-follower@.service`（元件一已完成）、Next.js（filet-dashboard，或靜態匯出 + 反向代理）。
- 反向代理（nginx/caddy）：TLS、路由 dashboard 與 /api。
- 硬化沿元件一基線（NoNewPrivileges、ProtectSystem=strict、key 目錄唯讀給引擎、僅 socket 給 API）。
- 加州行前需可遠端管；行前 code freeze + kill-switch 演練（沿 roadmap）。

## 開放項（開工前/中拍板）

1. Session store：SQLite vs 其他；與元件一 var/filet 的整合（傾向 SQLite 單檔）。
2. 律師背書（M0 gate）——真錢上線前置，與本後端並行推進。
3. admin 核准 UI 的最小形態（M2 可先 CLI + 簡頁）。
4. HL SDK 對「用外部（瀏覽器）簽名的 ApproveAgent/ApproveBuilderFee action」的建構/送出 API 形態——實作前需查證 SDK 是否有現成的「只建 action typed-data 不簽」路徑，或需自建 EIP-712（`spark/docs/superpowers/research/` 記錄）。

## 實作計畫拆解（給 writing-plans）

opus 審查建議：作為設計文件三塊合併可接受，但作為**實作**應拆成獨立計畫、依安全等級與依賴排序：
1. **key-service + envfile O_EXCL 加固**（最高安全等級，需獨立權限測試——filet-api user 讀不到 key、O_EXCL 防覆寫、socket SO_PEERCRED）——**先落地**。
2. **Public API**（SIWE、產 payload、verify、admin 唯讀、activate CLI）——次之，依賴 key-service socket。HL SDK 外部簽名路徑須實作前查證落 research。
3. **Dashboard 前端**（Next.js，v1 token，wizard 4 階段）——依賴 API 契約。
4. **部署**（systemd units、反代 TLS、權限驗收）——最後。

## 開工前必查證（research，落 spark docs/superpowers/research/）

- HL SDK 是否有「只建 ApproveAgent/ApproveBuilderFee typed-data 不簽」的路徑，或需自建 EIP-712；agent wallet 無提款/轉帳權的 HL 事實確認；斷點續走時對同一 agent 地址/agentName 重發 ApproveAgent 的 HL 語意（no-op/覆蓋/rotate）。**這是整個非託管流程的 load-bearing 未知，key-service 之後、API 之前必須先查證。**

## 不在本設計（各自後續）

- Next.js 前端的**元件級實作細節**（用 writing-plans 展開；視覺照 v1 token）。
- M3 的 Stripe 訂閱、多 leader、slider——M3 範疇。
- M1 收尾（testnet 實測/shadow/dogfood）並行。
