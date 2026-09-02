# 2026-09-02 對外上線前最終回歸：重寫功能／對接測試（拋棄式錢包 × Hyperliquid testnet）

> 使用者指令（原文要旨）：重寫功能與對接測試、做完整回歸，含拋棄式錢包與 Hyperliquid 對接；
> 這是對外上線前最後一次檢查，**寧可錯殺，不要有緊急線上問題**。

## 執行狀態（主線程維護；每個 task 完成後更新）

| Task | 狀態 | commit / 證據 |
|---|---|---|
| T0 日報 Read-only FS 修復 | ✅ 已部署正式機（2026-09-02 03:01 UTC） | 離線 2679 passed；prod `systemctl start filet-daily-report` → Finished、`2026-09-02.md` 落檔、history 新增一點；api drop-in `filet-api.service.d/accrued-history.conf`；reports 目錄改 `filet-engine:filet-api 2750`（setgid，讓日報新檔自動可被 API 讀）；`/api/public/stats` 路由量 153,315 → 157,455。**追加發現**：日報自 **2026-07-28** 起連續 36 天失敗（依序 `builder_accrued_snapshot.json` 權限、`reports/` 權限、history 路徑三種錯誤），8/31 那份是人工 root 補跑；`FILET_LEADERS_PATH` 在 prod 是 drop-in `leaders-path.conf`（grep unit 主檔看不到，要用 `systemctl show -p Environment`）。 |
| T1 testnet 拋棄式錢包 harness | ✅（網路函式待 T2 首跑驗證） | 主線程親跑：離線 2679 passed、ruff clean、collect-only OK、`faucet-status` 印出水龍頭 spot 300 |
| T2 非託管全流程 E2E（testnet） | ✅ builder 連跑 3 輪 17/17；**主線程親跑 04:19 UTC 17 passed（T9 後：A4 看到 inbound internalTransfer、S13 testnet explorer 查到 approveBuilderFee）** | S1–S13＋A1–A4 全在真 testnet；發現 4 項：**internalTransfer 漏收（→T9）**、新地址首轉 $1 費、`/api/me/authorizations` explorer 硬編主網（→T9）、manifest 不存在時 `/api/me/leader` 503（設計） |
| T9 ledger `internalTransfer` 修復 | ✅ builder 完成，待 reviewer 審 | 主線程親跑：離線 2732 passed、ruff clean、`-m integration test_hl_contract.py` 24 passed（testnet 真實 ledger 觀察到 internalTransfer 欄位）；`sum_ledger_deposits` 是否對 outbound internalTransfer 倒扣列為待裁決（未實作，理由見 strategies.py docstring） |
| T3 HL 真實 payload 契約測試（testnet + mainnet 唯讀） | ✅ | 主線程親跑：`24 passed, 32 warnings`；fixtures 35 個；**未發現欄位缺席**；發現 mainnet builder 錢包 `userFillsByTime` 自 0 起查會回滿 2000 筆觸發 `FillsTruncatedError`（安全閥有效） |
| T4 `filet_regression_check.py` 重寫對齊現行產品 | ✅ | 64→66 條；主線程親跑 prod：抓到 (1) `/leaderboard` 是 client-side redirect（主線程裁決接受，檢查改為兩形態皆可）、(2) `/api/public/explore` 重啟後空榜 12 分鐘 → 追查出 **`FILET_EXPLORE_CACHE_PATH` 從未在 prod 生效**（快照落檔 Read-only FS）→ 已加 drop-in `explore-cache.conf` 指向 `/var/lib/filet-api/explore_index.json` 並重啟 API；新增兩條 SSH 檢查釘住。03:27 UTC 快照檔落地（441 KB，`filet-api:filet-api 644`），`/api/public/explore` 25 rows、building=False——下次重啟不再冷建空榜 |
| T5 Playwright 公開頁瀏覽器 smoke | ✅ 主線程親跑 19/19（本機 stack：keysvc-serve＋run_api 8700＋next 3100；⚠️ keysvc socket 要放短路徑如 `/tmp/spark-t7/`，scratchpad 路徑超過 AF_UNIX 104 字元會 `path too long`） | 路由自 `page.tsx` glob 推導＋分類自檢；CJK 檢查排除 `<script>`/RSC payload/`.lang-toggle`；唯一白名單＝Header 的 `/api/me` 401 探測（瀏覽器產生的 console error） |
| T6 Playwright 注入錢包走 onboarding | ✅ builder 20/20（主線程於 T7 重跑複核） | 注入 EIP-1193 mock、Node 端 viem 簽；approveAgent／approveBuilderFee 真上 testnet；**抓到精靈跳過 verify 的靜默失敗（→T10）**；另兩個觀察：非支援鏈簽章錯誤被歸類為「使用者拒簽」；SIWE cookie `secure=True` 本機必須用 `localhost` 不能用 `127.0.0.1` |
| T9b T9 審查修正 | ✅ 主線程親跑 2738 passed、ruff clean；opus 第二輪：**引擎側可部署** | W1 接線測試（變異已殺死，reviewer 親測）、W2 自轉自 → −fee、W3 `strategies.py` 還原（公開入金語意留 T8 裁決）。殘留疑慮 W5「spot send 是否同型別」主線程實測釘死：spot 轉帳 ledger 型別＝`send`（含 sourceDex/destinationDex/usdcValue），`internalTransfer` 只出現在 perp `usdSend`；HL 對自轉自回 `Cannot self-transfer.`（守衛分支實務上不可達） |
| T10 精靈跳過 verify 修復 | ✅ 主線程瀏覽器複核：全新錢包與「已核准後重載」兩條路徑皆由 auto-verify 落地 pending（`途徑=auto-verify`），全套 20/20；離線 2738 / vitest 664 | opus 第二輪 4 個 Warning（補寫失敗無告警、`_progress` 順序、`manifest_degraded` 被丟、只捕 OSError）→ T10b |
| T10b T10 審查修正 | ✅ 主線程親讀 diff＋親跑 | notifier.critical 兩處、先查 manifest 再打 HL、`manifest_degraded` 不寫、捕 `ValueError`、useRef 守衛；離線 2741 / vitest 664 |
| T7 全量執行＋報告＋opus 審查＋verdict | ✅ 2026-09-02 04:5x UTC 主線程親跑終態 | 離線 2741 passed、ruff clean、vitest 664、tsc 零新增（17 既有）；契約 24 passed；真鏈 E2E 17 passed（第三次）；瀏覽器 20/20 ×2（post-T10b build，兩條路徑皆 auto-verify）；prod regression_check **66/66 PASS**。報告 `docs/superpowers/research/2026-09-02-golive-regression-report.md`；RUNBOOK §8 驗收 4 |
| T8 檢查點 | ✅ 部署／commit 完成（2026-09-02 05:09 UTC，使用者放行）；裁決 A／B 待使用者 | 六個 commit 推上 origin/main（`82bc8bb`）；prod rsync＋build＋restart api/dashboard/follower；regression_check 66/66、failed 0、`/explore` 25 rows、`DEPLOYED_VERSION=82bc8bb`。剩：裁決 A（真實入金是否計入 Send）、裁決 B（`/leaderboard` redirect 形態） |

## 0. 盤點結論（2026-09-02 10:40，主線程親跑）

基線（`uv run pytest -q` / `ruff` / `npm test`）：**2677 passed、2 deselected、ruff clean、vitest 658 passed**。
每一條 publicapi 路由都至少有一個離線測試（主線程 grep 逐條核對過，scout 的「18 條無測試」是檔名對照誤判）。

真正的缺口不在離線層，在**對接層**：

1. `tests/integration/` 只有 2 個測試，都是 M1 時代「自有錢包 + Keychain 主鑰」模式寫的，
   早於 keysvc／watcher／簽章風控／pause／close-all／leader change。現行非託管產品流程
   **從未**在真實 HL testnet 上被自動化測試跑過（2026-07-19 那次是人工＋瀏覽器）。
2. **正式機現在就有一個線上故障**（主線程 ssh 唯讀查得）：`filet-daily-report.service`
   自 2026-09-01 起每日 failed——`OSError: [Errno 30] Read-only file system:
   'var/copytrade/accrued_history.jsonl'`。原因：commit `dc8133e` 讓日報寫入
   `var/copytrade/accrued_history.jsonl`（相對路徑、root:root 644），但 unit 是
   `ProtectSystem=strict` 且 `ReadWritePaths` 只放行 `var/filet/reports` 與
   `builder_accrued_snapshot.json`。後果：北極星／builder 合規／換 leader 對帳／營收告警
   已兩天沒跑，首頁路由量卡在播種值。**這正是「離線全綠但線上壞」的樣板**。
3. `scripts/filet_regression_check.py` 已漂移：HOST 還是 sslip、前端路由是改版前的九條
   （`/leaders /capital /performance /pricing /billing /ops /admin`）、硬性斷言
   `FILET_API_NETWORK=testnet`（正式機現在是 mainnet，主線程查過 unit 檔）、沒有
   `systemctl --failed` 檢查（有的話第 2 點兩天前就會被抓到）。
4. 前端只有 vitest，沒有任何瀏覽器層測試。

testnet 資源（主線程親查 2026-09-02）：

| 用途 | 位址 | Keychain（service=spark） | testnet perp / spot USDC |
|---|---|---|---|
| builder（同時是 prod 主網 follower 的錢包） | `0xbAC652a5fb611c1bdc3b9d244cc7e0cc03123662` | `filet-testnet-builder:main` | 398.8 / 599.0 |
| 舊客戶 | `0xfb9c52f56f03d786ad5d435aa70fe45d80569760` | `filet-testnet:main` | 199.9 / 798.2 |

⚠️ 這兩把主鑰**同時控制主網真錢**（同位址在 prod 跑 mainnet follower）。本計畫的 harness
**不得**直接用它們下單；只允許（經使用者裁決後）用其中一把做**一次性** testnet `usdSend`
播種專用水龍頭錢包，之後 harness 永遠只碰水龍頭錢包與拋棄式錢包。

## 1. 硬規則（所有 task 適用，違反即 Critical）

- R1 **零主網寫入**。任何對 `api.hyperliquid.xyz` 的呼叫只能是 `/info`。harness 在建構
  時斷言 `base_url == constants.TESTNET_API_URL`，主網 URL 直接 `raise`。
- R2 **零正式機變更**。`ssh` 只做唯讀（`cat`/`stat`/`systemctl is-active`/`journalctl`）。
  部署 T0 修復、restart 任何 unit 都是 T8 使用者裁決事項。
- R3 `/Users/jim/projects/hl-copytrader` 唯讀（專案紅線 1）。
- R4 私鑰不進 log／repr／例外訊息／測試輸出；harness 的 wallet 物件 `__repr__` 只印位址。
  Keychain 讀取只在 harness 內、只讀 `service="spark"`。
- R5 `ExchangeAdapter` 不得新增 transfer/withdraw（紅線 3）。harness 的資金搬運**直接用
  `hyperliquid.exchange.Exchange.usd_transfer`**，放在 `tests/integration/harness.py`，
  不進 `src/`。
- R6 離線測試套件（`uv run pytest`）維持全綠且不連網；所有新對接測試一律
  `@pytest.mark.integration`，缺憑證時 `pytest.skip` 並給明確原因（不是假 PASS）。
- R7 每個 task 的驗收指令由主線程親跑；builder 回報不算證據。

## 2. 待使用者裁決（T8 之前已問；答案回填於此）

- Q1 水龍頭錢包資金來源：
  (a) harness 生成 `filet-testnet-faucet` 錢包（Keychain `filet-testnet-faucet:main`），
  由程式用 `filet-testnet:main` 在 **testnet** 做一次 `usdSend` 300 USDC 播種
  （在程序內、有 R1 閘門、鑰匙不出 process）；
  (b) 同上但由使用者手動從 HL testnet 網頁介面轉帳到 harness 印出的水龍頭位址；
  (c) harness 直接用 `filet-testnet:main` 當水龍頭（不建議：與主網共用主鑰）。
  → 裁決（2026-09-02 使用者）：**(a)**；但主線程隨後查明 Keychain 的 `filet-testnet:main`／
  `filet-testnet-builder:main` 是 M1 時代的另外兩個位址（`0x5579…0B5d`／`0x63e6…4847`），
  兩網餘額皆 0，(a) 不可行 → 改走 **(b)**：主線程已生成水龍頭錢包
  `0x4229ea4BaDf01D7517FBf8B7EC83aE6927DB9CE2`（Keychain `filet-testnet-faucet:main`），
  請使用者從 9760 在 testnet UI 轉 300 USDC 過去；harness 輪詢到款即開跑。
- Q2 T0 修復完成後是否**今天**部署到正式機（rsync 兩個檔＋`daemon-reload`＋
  `systemctl start filet-daily-report` 補跑一次）？不碰引擎、不碰資金。
  → 裁決（2026-09-02 使用者）：**修好就部署**（離線驗證通過後主線程部署並貼 journal 證據）。
- Q3 T6（瀏覽器注入錢包走完 onboarding，會在 testnet 上鏈 approveAgent／approveBuilderFee）
  是否要做？→ 裁決（2026-09-02 使用者）：**做**。

## 3. Tasks

### T0 @inline — 修復日報 Read-only FS（線上故障）

**檔案**：`scripts/copytrade_daily_report.py`、`scripts/filet_daily_report.py`、
`deploy/filet-daily-report.service`、`deploy/filet-api.service`、`tests/test_deploy_artifacts.py`、
`deploy/RUNBOOK.md`（§5.7a 追加一段）。

**做法**（單一來源原則）：
1. 歷史檔路徑改由 env `FILET_ACCRUED_HISTORY_PATH` 決定（與 `src/spark/publicapi/config.py:291`
   API 讀的是**同一個** env 名），預設維持 `var/copytrade/accrued_history.jsonl`（本機開發不變）。
   `copytrade_daily_report.append_accrued_history` 改成接受 `path` 參數（或讀 env 的模組層
   函式），`filet_daily_report.py` 傳入；寫入用「寫暫存檔 + `os.replace`」原子替換。
2. `deploy/filet-daily-report.service`：加
   `Environment=FILET_ACCRUED_HISTORY_PATH=/opt/filet/spark/var/filet/reports/accrued_history.jsonl`
   （落在既有 `ReadWritePaths=/opt/filet/spark/var/filet/reports` 底下，不必新增放行）。
3. `deploy/filet-api.service`：加**同值**的 `Environment=FILET_ACCRUED_HISTORY_PATH=...`
   （API 目前讀的是相對路徑 `var/copytrade/...`＝播種檔；日報寫到新位置後 API 必須讀同一個檔）。
4. `tests/test_deploy_artifacts.py` 新增結構性斷言：兩個 unit 宣告的
   `FILET_ACCRUED_HISTORY_PATH` 逐字元相同，且該路徑以 daily-report 某條 `ReadWritePaths`
   為前綴。
5. RUNBOOK §5.7a 追加：部署步驟（`sudo cp` 播種檔到新路徑並 `chown filet-engine:filet-engine`、
   `daemon-reload`、`systemctl start filet-daily-report` 驗證 `journalctl` 無 Traceback）。

**驗收**：
- `uv run pytest tests/test_deploy_artifacts.py tests/test_accrued.py -q` 全綠；
- `FILET_ACCRUED_HISTORY_PATH=/tmp/x/h.jsonl uv run python -c "from scripts.copytrade_daily_report import append_accrued_history as f; f('2026-09-02', __import__('decimal').Decimal('1'), ...)"`
  （builder 依實際簽名調整）後 `/tmp/x/h.jsonl` 存在且 `var/copytrade/accrued_history.jsonl` 未變；
- `git diff --stat` 只含上列檔案。

### T1 @inline — testnet 拋棄式錢包 harness

**檔案**：`tests/integration/harness.py`（新）、`tests/integration/conftest.py`（新）、
刪除 `tests/integration/test_testnet_flow.py` 與 `tests/integration/test_copytrade_testnet.py`
（被 T2 取代；其「modify 不丟 builder 歸屬」情境併入 T2 的 A 組）。

**harness.py 元件**（全部型別標註；私鑰只存在 `eth_account.LocalAccount` 物件內）：
- `TESTNET_URL = hyperliquid.utils.constants.TESTNET_API_URL`；模組載入即
  `assert "testnet" in TESTNET_URL`；任何建構 `Exchange`/`Info`/`HyperliquidAdapter`/`HLGateway`
  的地方都固定傳這個常數。
- `class Wallet`：`account: LocalAccount`、`address`、`__repr__ -> f"<Wallet {address}>"`；
  `sign_text(msg) -> hexsig`（personal_sign，與 `tests/publicapi_helpers.login` 同法）；
  `sign_typed(typed_data) -> {r,s,v}`（`Account.sign_typed_data(full_message=...)`）。
- `faucet_wallet() -> Wallet`：從 Keychain `spark` / `{FILET_TESTNET_FAUCET_ACCOUNT:-filet-testnet-faucet}:main`
  讀；沒有就 `pytest.skip("缺 testnet 水龍頭錢包，見 plan §2 Q1")`。
- `new_wallet() -> Wallet`：`Account.create()`。
- `fund(faucet, dest, usdc: Decimal)`：`Exchange(faucet.account, TESTNET_URL).usd_transfer(float(usdc), dest)`；
  之後輪詢 `Info.user_state(dest)["marginSummary"]["accountValue"]` 直到 ≥ usdc×0.99（最多 60s）。
- `sweep(wallet, faucet)`：讀 `withdrawable`，若 > 1 就 `usd_transfer` 回水龍頭（best-effort，失敗只 warn）。
- `flatten(wallet)`：對每個非零 `assetPositions` 用 `Exchange.market_close`；用於 leader teardown。
- `submit_user_signed(action, signature)`：POST `{TESTNET_URL}/exchange`
  `{"action": action, "nonce": action["nonce"], "signature": sig}`，回 JSON；`status != "ok"` 就 raise。
  **這是前端「直送 HL」路徑的 Python 鏡像**——用它證明 `spark.publicapi.approvals.build_approve_agent`
  產生的 typed data 能被 HL 接受（typed-data builder 與鏈的契約）。
- `class KeysvcThread`：在 `tmp_path/keysvc.sock` 上用真 `spark.keysvc.server` 起 server
  （thread，daemon），`authorize_peer` 以 `monkeypatch` 換成恆 True（macOS 沒有 SO_PEERCRED，
  peercred 本身有離線測試＋RUNBOOK §8 驗收 2 實機驗）；keys 目錄 `tmp_path/keys`；
  `stop()` 收尾。
- `make_real_app(tmp_path, *, builder, leaders: list[dict], keysvc_sock) -> (TestClient, cfg, store)`：
  用 `tests.publicapi_helpers.make_cfg` 取 cfg（network="testnet"、`builder_address=builder`、
  `keysvc_sock` 指 thread 的 socket、`leaders_path` 指寫好的 tmp `leaders.json`）；
  keysvc 用真 `spark.keysvc.client.KeysvcClient`、hl 用真 `HLGateway(TESTNET_URL)`；
  `create_app(cfg, ApiStore(cfg.db_path), keysvc, hl)`。
- `engine_env_from_file(path) -> dict`、`run_engine_once(env_file, keys_dir, extra_env) -> CompletedProcess`：
  subprocess `uv run python -m scripts.run_copytrade --once`，env＝檔案內容＋`FILET_KEYS_DIR`
  ＋`SPARK_NETWORK=testnet`；stdout/stderr 收進回傳值供斷言；timeout 120s。
- `leader_trade(wallet, coin, is_buy, notional_usd)`：以 leader **主鑰**建 `Exchange` 做
  `market_open`（size = notional / mid，按 `szDecimals` 捨入；notional ≥ 12 USD 避開 $10 門檻）。
- `wait_until(pred, timeout, interval)`。

**conftest.py**：session-scoped fixtures `faucet`、`customer`（fund 150）、`leader`（fund 120）、
`builder_address`（env `SPARK_BUILDER_ADDR`，預設 `0xbAC652a5fb611c1bdc3b9d244cc7e0cc03123662`）、
`keysvc`、`app`；session 結束一律 `flatten(leader)`→`sweep(customer)`→`sweep(leader)`。
沒有水龍頭錢包時整個目錄 skip（`pytest -m integration` 顯示 skip 原因）。

**驗收**：
- `uv run pytest -q`（離線）仍全綠、無新連網（conftest 的 socket-ban 會抓）；
- `uv run ruff check tests/integration`；
- `uv run pytest -m integration tests/integration -q --collect-only` 能收集；
- 在沒有 Keychain 水龍頭項目的 shell 跑 `uv run pytest -m integration tests/integration -q`
  → 全部 skipped、原因文字含「水龍頭」。

### T2 @inline — 非託管全流程 E2E（真 testnet）

**檔案**：`tests/integration/test_e2e_noncustodial.py`（新）、`tests/integration/test_adapter_testnet.py`（新）。
**先讀**：`scripts/filet_auto_activate.py:341-506`（`process_entry`/`run_once` 的參數與 `run_cmd`
注入）、`scripts/run_copytrade.py:1-87`、`src/spark/filet/close_all.py` 檔頭、
`src/spark/filet/risk_settings.py:191-260`、`tests/test_filet_auto_activate.py`（注入範例）、
`tests/test_kill_switch.py`（pause/close-all 的 API 用法）。

**E2E 情境**（同一個 module 內按序執行；用 module-scoped 狀態物件串接；每條都是獨立 test
function，前置未達成就 `pytest.fail` 不是 skip）：

| # | 情境 | 斷言（對真鏈／真 API） |
|---|---|---|
| S1 | SIWE 登入（customer 主鑰真簽） | `/api/me` 200、address 相符 |
| S2 | `POST /api/onboard/agent` | 回 `agent_address`；`keys/<account_id>/agent.key` 存在且 mode 600；再呼一次 409 `agent_exists` |
| S3 | approveAgent：`build_approve_agent(... is_mainnet=False, wallet_chain_id=421614)` → customer 簽 → `submit_user_signed` | HL 回 ok；`Info.extra_agents(customer)` 含該 agent |
| S4 | approveBuilderFee（rate 取 `cfg`/產品常數） | `Info.query_max_builder_fee(customer, builder)` == 預期 |
| S5 | `/api/onboard/status` → `/api/onboard/verify` | `funded/agent_approved/builder_fee_approved/ready` 全 true；`pending.json` 有條目且 builder pin＝builder、network＝testnet |
| S6 | leader 選擇（leaders.json 白名單含拋棄式 leader；`GET /api/leaders/select/message` → 簽 → `POST`） | `/api/me/leader` 回該 leader；exchange dir 有簽章記錄 |
| S7 | 風控簽章（啟用回撤 kill switch，`max_drawdown_pct` 取合法值） | `/api/me/risk` 顯示已啟用＋參數 |
| S8 | watcher `run_once(..., run_cmd=recorder)` | env 檔生成於 `env-dir`：`SPARK_NETWORK=testnet`、`COPY_LIVE_TRADING=true`、`COPY_LEADER_ADDRESS=<leader>`、`COPY_RISK_CONTROLS_ENABLED=true`、`SPARK_USER_ADDR`/`SPARK_BUILDER_ADDR`；recorder 收到 `systemctl start filet-follower@<id>`；pending 已消費；manifest 有條目 |
| S9 | leader `market_open` 多單（notional ≈ 20 USD）→ `run_engine_once` | follower 出現同向部位；`abs(follower_sz/leader_sz - expected_scale)` 在容差內（expected_scale 由引擎 sizing 規則算，builder 讀 `src/spark/copytrade/sizing.py`）；`Info.user_fills(customer)` 最新成交 `builderFee > 0`；`query_builder_accrued(builder)` 較 S1 前增加 |
| S10 | leader 反手（先 close 再反向開）→ 引擎一輪 | follower 方向翻轉 |
| S11 | `POST /api/me/pause` → leader 加倉 → 引擎一輪 | follower 部位不變；恢復後再一輪 → 跟上 |
| S12 | close-all（message → 簽 → POST）→ 引擎一輪 | follower 全平（`assetPositions` 空）；引擎狀態顯示已停止／撤銷（依 `close_all_apply.py` 實際語意斷言） |
| S13 | 交易後讀面板：`/api/me/dashboard` `/api/me/fees` `/api/me/fills` `/api/me/authorizations` | 皆 200；fills ≥ 1 筆且含 builder fee 欄位；authorizations 列出 agent；fees 合計 > 0 |

**A 組（adapter 直連，取代舊 `test_copytrade_testnet.py`）**：用 customer 的 agent key
（從 `keys/<id>/agent.key` 經 `EnvFileKeyStore` 取）建 `HyperliquidAdapter`：
A1 `place_order` GTC 遠價掛單 → `get_open_orders` 看得到 → `modify_order` 改到可成交價 →
成交 `builderFee > 0`（modify 不丟 builder 歸屬，2026-07-19 結論再驗）；
A2 `place_marketable`／`market_open` 成交帶 builder；A3 `close_reduce_only` 全平；
A4 `get_equity_view`/`get_account_state`/`get_ledger_flows`（含入金那筆 `usdcValue`）解析不拋、
欄位型別為 Decimal。

**驗收**：`uv run pytest -m integration tests/integration -q -x -p no:cacheprovider`
全綠（主線程親跑，並貼 testnet explorer 可查的 customer 位址供人工複核）；
離線 `uv run pytest -q` 仍 2677+ 綠。

### T3 @inline — HL 真實 payload 契約測試（testnet + mainnet 皆唯讀）

**檔案**：`tests/integration/test_hl_contract.py`（新）。
**動機**：工程原則 1 事故 #5「欄位名是假設不是事實」；離線測試的假資料是照同一份假設寫的。
**做法**：對每個 adapter/HLGateway 會解析的 `/info` type，各打一次 testnet 與 mainnet
（mainnet 只讀，位址用 `0xbAC652…3662` 與 `0xfb9c52…9760`，皆有歷史），把**原始 JSON**
餵給我們的解析函式（`HyperliquidAdapter` 的 `get_positions/get_open_orders/get_user_fills/
get_equity_view/get_ledger_flows/get_active_asset_leverage/query_agent_addresses/
query_max_builder_fee/query_builder_accrued`、`HLGateway` 全部方法、`public_stats`/`explore`/
`traders` 會碰的 `portfolio`/`accountValueHistory`/`userNonFundingLedgerUpdates`/`meta`/
`allMids`/`spotClearinghouseState`），斷言：不拋、回傳型別正確、Decimal 欄位不為 NaN、
時間欄位單調。另外把每個 type 第一筆原始回應的**欄位名集合**寫進
`tests/fixtures/hl_payload_keys/<type>.json`（若不存在則建立；存在則比對——多出欄位只 warn，
**缺欄位 fail**），作為之後上游改欄位名的警報器。
mainnet 呼叫節流 0.7s/請求（與 explore 相同），總請求數 < 40。

**驗收**：`uv run pytest -m integration tests/integration/test_hl_contract.py -q` 全綠；
`ls tests/fixtures/hl_payload_keys | wc -l` ≥ 12。

### T4 @inline — `scripts/filet_regression_check.py` 對齊現行產品

**先讀**：本檔 §0 第 3 點、`deploy/RUNBOOK.md` §4.2、`web/src/app/**/page.tsx` 路由清單、
`src/spark/publicapi/app.py` 的 `/api/public/*` 回應欄位。
**改動**（維持唯讀；沿用既有 Report/PASS-FAIL-SKIP 架構）：
- `HOST` 預設 `trade.filet.app`；新增 `FILET_RC_EXPECT_NETWORK`（預設 `mainnet`，與 unit 檔實值比對，不符 FAIL——「網路模式必須是刻意的人工決策」的語意保留，只是預期值可宣告）。
- 前端路由改成現行集合：`/ /strategies /explore /advanced /docs /terms /privacy /risk /status /onboarding /dashboard /settings` 皆 200；`/leaderboard` 必須 30x 到 `/explore`；`/strategies/<slug>`（slug 從 `/api/public/strategies` 取第一個）200。
- `robots.txt`、`sitemap.xml` 200 且 sitemap 內 host == HOST（抓 `NEXT_PUBLIC_SITE_ORIGIN` 沒重 build 的地雷）。
- 公開 API 契約：`/api/public/stats`（含 routed_volume 且 > 0）、`/api/public/status`、`/api/public/strategies`（≥1 條且欄位齊）、`/api/public/benchmarks`、`/api/public/explore`（entries ≥ 1，抓「空榜」）、`/api/billing/plans`（沿用）。
- 未授權必 401：`/api/me /api/me/capital /api/me/leader /api/me/risk /api/me/dashboard /api/me/fees /api/me/fills /api/me/authorizations /api/leaders /api/ops/health /api/ops/revenue /api/admin/pending`；未授權 **POST** `/api/me/pause`、`/api/leaders/select`、`/api/me/close-all` 也必 401。
- HSTS header 存在；http→https 沿用。
- SSH：`systemctl --failed` 必須為空（⭐）；三常駐 + `filet-follower@*` active 數量列出；**四個** timer enabled；`filet-daily-report`/`filet-leaderboard` 最近一次 `Finished` 在 26h 內、`filet-perf-series` 在 13h 內（`journalctl -u X --since -2d | grep Finished | tail -1`）；`FILET_ACCRUED_HISTORY_PATH` 在 api 與 daily-report 兩個 unit 同值（T0）；其餘既有檢查（交換目錄方向性、白名單權限、`FILET_LEADERS_PATH` 五個 unit 含 auto-activate 與 daily-report、機密檔零命中）保留；磁碟使用率 < 85%。
- `--local` 段沿用。

**驗收**：`uv run python -m scripts.filet_regression_check --http --ssh` 對正式機跑一次：
除「systemctl --failed 為空」與 `FILET_ACCRUED_HISTORY_PATH`（T0 未部署前預期 FAIL）外全 PASS；
`--local --fast` PASS；`uv run pytest tests/test_regression_check.py -q`（若既有）綠。

### T5 @inline — Playwright 公開頁瀏覽器 smoke

**檔案**：`web/e2e/public-smoke.spec.ts`、`web/playwright.config.ts`、`web/package.json`
（devDependency `@playwright/test` 釘版本、script `test:e2e`）。
**做法**：`baseURL` 由 `E2E_BASE_URL` 決定（預設 `http://127.0.0.1:3000`）。主線程負責起
本機 stack（keysvc thread 不需要；`scripts/run_api.py` 用 testnet env + tmp db、`npm run build && npm start`）。
每條公開路由：HTTP 200、有 `h1`、**零 console error / pageerror**（監聽 `page.on('console')`
與 `page.on('pageerror')`）、無 "Application error" 文字；切到 EN 模式後 `document.body.innerText`
不含 CJK（沿用 vitest `enNoCjk` 的正規式）；nav 每個連結點過去都 200；`/leaderboard` 落到 `/explore`；
`/strategies/[slug]` 用 API 第一個 slug；`/dashboard` 未登入→顯示登入 CTA 或導向（依現行行為斷言）。
瀏覽器只裝 chromium（`npx playwright install chromium`）。

**驗收**：`cd web && npm run test:e2e` 全綠（主線程親跑，需先起本機 stack）；`npm test` 不受影響。

### T6 @inline（P2，Q3 裁決後）— Playwright 注入錢包走 onboarding

`web/e2e/wallet-onboarding.spec.ts`：`page.addInitScript` 注入最小 EIP-1193 provider
（`eth_requestAccounts`/`eth_chainId`（421614）/`personal_sign`/`eth_signTypedData_v4`/
`wallet_switchEthereumChain`），簽章由 `page.exposeFunction` 交給 Node 端 viem
`privateKeyToAccount`（私鑰從 env `E2E_WALLET_PK` 讀，由主線程以 harness 生成並 fund 的拋棄式
錢包提供；不落檔）。走 `/onboarding` 直到 pending（agent 產生、兩個鏈上授權真的上 testnet），
然後 `/dashboard` 渲染成功。這是**瀏覽器路徑**與 T2 Python 路徑的交叉驗證。

### T9 @inline — 修 ledger 白名單漏 `internalTransfer`（T2 在真鏈抓到的產品缺口）

**事實**（主線程 2026-09-02 親查 testnet `userNonFundingLedgerUpdates` 原始 payload）：
HL「Send」（錢包對錢包 USDC 轉帳，`usdSend`）在**雙方**的 ledger 都是
`{"type":"internalTransfer","usdc":"150.0","user":"<sender>","destination":"<receiver>","fee":"1.0"}`；
收方實收 `usdc − fee`（首次轉入新地址 fee=1.0，既有地址 fee=0.0），送方 perp 減少 `usdc`。
`spark.exchange.ledger_flows.FLOW_FIELDS` 沒有這個型別 → `signed_flow` 回 None、只進 unknown_types。
消費端：(1) `copytrade/follower_flow.py:147` 回撤基準出入金校正——客戶用 Send **轉出**資金會被
當成虧損（幻影回撤 → kill switch 可能誤觸發＝工程原則 1 事故 #4 同型）、**轉入**不抬基準
（之後真虧損被稀釋＝fail-open）；(2) `copytrade/loop.py:311` vault leader 申贖中性化；
(3) `scripts/vault_preflight.py` 恆等式；(4) `filet/strategies.py:253` 只加總 `deposit` 的
「真實入金」（dashboard 起訖淨值同法，builder 要 grep `"deposit"` 找齊）。

**做法**：
1. `ledger_flows.py`：`signed_flow(delta, *, address: str | None = None)`；`internalTransfer`
   分支：`destination == address`（小寫比對）→ `+ (usdc − fee)`；`user == address` → `− usdc`；
   address 為 None 或兩邊都不是 → None 並由 `flow_anomaly` 回新分類 `"internal-transfer-direction-unknown"`。
   型別字面只准出現在本檔與測試（檔頭既有規則）。
2. 三個呼叫端（`hyperliquid.py:308` 附近、`vault_preflight.py`、`fakes.py` 若有鏡像）傳入 address。
3. `filet/strategies.py` 與 dashboard 的「真實入金」加總：inbound internalTransfer 計入入金
   （淨額 usdc − fee）、outbound 計入出金——若該處刻意只算 `deposit`（讀 docstring 確認），
   把 internalTransfer 也納入並更新 docstring；不確定就在回報裡標「待裁決」而非自行決定。
4. 順手修 T2 發現 #3：`src/spark/publicapi/hl.py:18` `EXPLORER_URL` 依 `network` 切換
   （testnet＝`https://rpc.hyperliquid-testnet.xyz/explorer`），用既有的 network→URL 單點
   （`spark.config` 或 `publicapi/config.py` 的 `API_URLS` 同型）。
5. 離線測試：用本節的真實 payload（兩個方向、fee 1.0／0.0）寫 `tests/test_ledger_flows.py`
   案例；`tests/test_follower_flow*.py` 加「Send 轉出不得產生幻影回撤」案例（基準被正確平移）。
   `tests/fixtures/hl_payload_keys/*ledger*` 若需更新 REQUIRED_KEYS 一併改。

**驗收**：`uv run pytest -q` 全綠；`uv run ruff check src tests scripts`；
`uv run pytest -m integration tests/integration/test_hl_contract.py -q` 仍綠；
主線程另派 `reviewer`（opus）審這段 diff（碰實盤引擎熔斷基準）。
**部署到正式機 follower 引擎 = T8 使用者裁決**（引擎重啟是紅線 5 範圍）。

### T9b @inline — T9 審查修正（opus reviewer 2026-09-02 三個 Warning）

1. **接線回歸保護**：`tests/test_hyperliquid_reads.py`（既有 `get_ledger_flows` 案例 :509-573 旁）
   加一個 internalTransfer payload 案例：以 address 為 `user` 的 outbound → `flows` 含 `−usdc`、
   `unknown_types` 為空；以 address 為 `destination` 的 inbound → `+(usdc−fee)`。reviewer 實測把
   `hyperliquid.py:329,335` 的 `address=address` 拿掉整套仍全綠——這個案例必須讓那個變異死掉
   （builder 自己做一次同樣的變異驗證並貼結果）。
2. **自轉自守衛**：`ledger_flows._internal_transfer_flow` 先判 `user == destination == address`
   → 淨效果 `−fee`（reviewer 實測目前回 `+149`，方向錯在幻影回撤側）。加測試。
3. **公開「真實入金」與引擎修復脫鉤**：`src/spark/filet/strategies.py::sum_ledger_deposits` 與
   `src/spark/publicapi/app.py:1851,2238` 的 address 傳入**還原到 T9 之前**（只算 `deposit`），
   `tests/test_public_strategies.py` 的 internalTransfer 案例移除或改成「不計入」。理由：inbound
   計入、outbound 不倒扣＝leader 自己兩個錢包來回 Send 可無限灌大策略頁的入金數字；要不要改成
   淨額是產品語意，列 T8 使用者裁決，不隨引擎修復上線。
4. Suggestion 採納：`strategies.py` 若仍呼叫 `signed_flow`（還原後應不再呼叫）需與 `deposit`
   分支同樣包 `InvalidOperation/ValueError`；`flow_anomaly` 對髒 `fee`（非數字）回 anomaly
   分類而不是拋（保持「一筆髒紀錄不炸呼叫端」的檔頭承諾）。

**驗收**：`uv run pytest -q` 全綠；ruff；變異驗證輸出（拿掉 address 傳入 → 新案例 FAIL，還原 → PASS）；
`git diff HEAD -- src/spark/filet/strategies.py` 為空；主線程之後親跑 `-m integration test_adapter_testnet.py`。

### T10 @inline — 修 onboarding 精靈跳過 `POST /api/onboard/verify`（T6 瀏覽器路徑抓到）

**事實**（主線程 2026-09-02 親讀程式碼確認）：
- `web/src/lib/wizard.ts:32` `deriveStep`：`status.state === "READY"` 就回 3（或 4），**不管**
  客戶有沒有按過 step 2 的「Complete setup」。
- `postVerify()`（`web/src/lib/api.ts:270`）**唯一**呼叫點是 `web/src/components/wizard/StepDeposit.tsx:40`
  ＝ step 2 的按鈕；`GET /api/onboard/status` 純讀（`app.py:3061`）；`write_pending_entry`
  只在 `POST /api/onboard/verify`（`app.py:3072`）。
- 後果：客戶簽完 agent＋builder fee、入金到位後**重新整理／換頁／隔天回來**（或入金在他離開
  後才到帳），精靈直接從 step 3 開始 → 走完 step 3、4、簽了 leader 選擇 → dashboard 顯示
  等待啟用 → pending.json 永遠沒有他 → watcher 永遠不啟用 → **無任何錯誤畫面**。
  T6 用同一個錢包第二次跑精靈就重現了。

**做法**（前後端各補一道，後端那道是結構性保證）：
1. 後端 `POST /api/leaders/select`（`app.py:2406`）：簽章記錄落地成功後，若
   `_progress(address)["state"] == "READY"` 且該 account 不在 followers manifest（用既有
   `_load_own_follower`，manifest 不存在視為不在）→ `write_pending_entry(...)`（與 verify
   同一組參數來源：session address、`cfg.builder_address`、`cfg.network`、agent_address）。
   冪等：pending 已有同 account 條目則不重複（讀 `spark/publicapi/pending.py` 的既有語意）。
   非 READY 不寫、不報錯（客戶可能還沒入金；回應加 `"pending_written": bool` 欄位供前端／測試）。
2. 前端 `web/src/app/onboarding/page.tsx`：`deriveStep` 判定為跳過 step 2（載入時已 READY 且
   本地進度沒有「step2 已 verify」旗標）→ 先呼叫一次 `postVerify()`（冪等）再進 step 3；
   `WizardProgress` 加 `step2Verified: boolean`（loadWizardProgress 的 schema 檢查同步加）。
   失敗（非 2xx）→ 停在 step 2 顯示既有錯誤 UI，不得靜默跳過。
3. 測試：後端 `tests/test_api_leader_select.py` 加「READY＋未啟用 → pending 寫入」、「READY＋
   已在 manifest → 不寫」、「非 READY → 不寫」；前端 `onboarding/page.test.tsx` 加
   「載入即 READY → postVerify 被呼叫一次」、`wizard.test.ts` schema 案例。

**驗收**：`uv run pytest -q` 全綠；`cd web && npm test` 全綠；`uv run ruff check`；
`git diff --stat` 只含上列檔案；主線程之後用 T6 spec 對「第二次跑精靈」重現 → pending 落地。

### T7 — 全量執行、報告、審查、verdict（主線程）

1. 主線程親跑：離線套件、`-m integration` 全部、`filet_regression_check --http --ssh`、`test:e2e`。
2. 派 `reviewer`（opus）審 `git diff main` + 本 plan：重點 R1–R6、harness 有沒有繞過 testnet
   閘門的路徑、私鑰外洩面、測試是否有「假 PASS」（skip 偽裝、容差過寬）。
3. 報告 `docs/superpowers/research/2026-09-02-golive-regression-report.md`：每條情境的
   證據（指令＋輸出尾巴＋testnet 位址／tx 可查）、發現的缺陷與修法、未覆蓋清單（誠實標註）、
   GO/NO-GO 判定與信心標註。
4. 更新 `deploy/RUNBOOK.md` 的驗收章節：新增「上線前跑 `-m integration` + regression_check」段。

### T8 — 晨間／收工檢查點（使用者）

- 部署 T0 修復（Q2）；`systemctl start filet-daily-report` 補跑；重跑 `filet_regression_check --ssh` 全 PASS。
- 報告中的 NO-GO 項目裁決。
- 水龍頭錢包收尾（留著給日後回歸用，或 sweep 回主錢包）。

## 4. 主線程裁決記錄

- D1 keysvc 在 macOS 沒有 SO_PEERCRED → harness 內以 monkeypatch 放行；理由：peercred 有離線測試與
  RUNBOOK §8 實機驗收，harness 目標是協定／keystore／API 三者的真實串接。
- D2 舊兩個 integration 測試刪除而非保留：它們假設持有客戶主鑰（M1 自有錢包模式），與現行
  非託管產品矛盾（2026-07-19 findings F2 早已指出）；其有效情境（builder 歸屬、modify）併入 T2 A 組。
- D3 leader 用拋棄式錢包＋tmp 白名單，而不是 prod 白名單裡的真 leader：可控、可反手、可平倉，
  且不會因為真 leader 靜止而測不到鏡像。
- D5（使用者，2026-09-02）裁決 A：「真實入金」不計入 Send 轉入，維持只算 `deposit`；問題與理由
  記在報告 §5.4（只加轉入會被灌水；改淨額則語意全變；現況只會低估本金）。
- D6（使用者，2026-09-02）裁決 B：`/leaderboard` 改伺服器層永久轉址（`next.config.ts` redirects，
  308）；移除 client-side 轉發頁；首頁「全部策略 →」直連 `/explore`。
- D4 T2 不做「真的觸發回撤熔斷」：需要真實虧損到門檻，testnet 上不可控；kill switch 全鏈路
  有 2026-07-19 人工驗證＋離線測試，本輪只驗「簽章→env→引擎讀到參數」這段接線（S7/S8）。
  報告的未覆蓋清單要寫明。
