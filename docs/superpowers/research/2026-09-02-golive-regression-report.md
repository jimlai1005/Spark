# 2026-09-02 對外上線前最終回歸報告

plan：`docs/superpowers/plans/2026-09-02-golive-regression.md`（含每個 task 的狀態、裁決、證據）。
所有「主線程親跑」的數字都是主線程本人在本機／正式機（唯讀）／HL testnet 實跑的輸出，
builder／reviewer 的回報只作參考，不作證據。

## 1. 判定

**GO（有條件）**：程式碼層面可以對外上線；條件是 §5 的三個部署動作與兩個使用者裁決先完成。
信心標註：對「已測到的面」信心高（真鏈、真瀏覽器、正式機唯讀都跑過）；對 §6 列的未覆蓋面
是誠實的未知，不是「應該沒問題」。

## 2. 四層回歸結果（主線程親跑，最終狀態）

| 層 | 指令 | 結果 |
|---|---|---|
| 離線 pytest | `uv run pytest -q` | **2741 passed**（基線 2677 → 新增 64；41 個 integration 測試依設定 deselect） |
| lint | `uv run ruff check src tests scripts` | clean |
| 前端 vitest | `cd web && npm test` | **664 passed**（基線 658 → 新增 6） |
| tsc | `npx tsc --noEmit` | 17 個**既有**錯誤（`explore/page.test.tsx`、`EquityCurve.test.tsx`），零新增 |
| HL 契約（testnet＋mainnet 唯讀） | `pytest -m integration tests/integration/test_hl_contract.py` | **24 passed**；12 個 `/info` type、35 個欄位 baseline；未發現欄位缺席 |
| 非託管 E2E（真 testnet） | `pytest -m integration tests/integration/test_e2e_noncustodial.py tests/integration/test_adapter_testnet.py` | **17 passed**（主線程三次親跑皆綠：S1–S13＋A1–A4） |
| 瀏覽器 smoke（公開頁） | `npm run test:e2e`（public-smoke） | **19 passed** |
| 瀏覽器錢包精靈（真 testnet 上鏈） | `npm run test:e2e:wallet` | **通過**，且「全新錢包」與「已核准後重新整理」兩條路徑都由 T10 auto-verify 落地 pending |
| 正式機唯讀回歸 | `filet_regression_check --http --ssh` | **66 條全 PASS**（部署 T0 前為 62/64；`explore` 空榜那條在快取落地後轉綠） |

E2E 情境涵蓋（每條都對真 HL testnet）：SIWE 真簽 → keysvc 真產 agent（600 權限）→ 後端產的
approveAgent／approveBuilderFee typed data 由客戶主鑰簽後**直送 HL 被接受** → `extraAgents`／
`maxBuilderFee` 鏈上驗證 → verify 落 pending → leader 選擇簽章落交換目錄 → 風控簽章 →
watcher `run_once` 產 env（`COPY_LIVE_TRADING=true`、`COPY_RISK_CONTROLS_ENABLED=true`）→
引擎 `--once` 鏡像 leader 多單（scale 1.24 vs 預期 1.25）→ 反手 → pause 不跟／恢復跟上 →
close-all 簽章 → 引擎收尾全平＋`halted` → dashboard/fees/fills/authorizations 讀真資料；
adapter 直連：GTC 掛單 → modify 不丟 builder 歸屬 → market open → reduce-only 全平 →
builder accrued 增加（0.62 → 0.65）。

## 3. 抓到並修好的問題（依嚴重度）

### F1 正式機日報自 2026-07-28 起連續 36 天失敗（線上故障，已修復並部署）
- 症狀：`filet-daily-report.service` 每日 failed，依序三種錯誤：`builder_accrued_snapshot.json`
  權限（7/28）→ `reports/` 是 root:root 700（8/14 起）→ `var/copytrade/accrued_history.jsonl`
  Read-only FS（9/1 起，commit `dc8133e`）。北極星／builder 合規／換 leader 對帳／營收告警
  這段期間全部沒跑；8/31 那份是人工 root 補跑。沒人發現，因為沒人監控 `systemctl --failed`。
- 修法（T0，已部署 03:01 UTC）：路徑改由 `FILET_ACCRUED_HISTORY_PATH` 決定（日報與 API 同一個
  env）、寫入原子替換；prod 以 drop-in `accrued-history.conf` 宣告；`reports/` 改
  `filet-engine:filet-api 2750`（setgid，日報寫、API 讀）。證據：`systemctl start` → Finished、
  `2026-09-02.md` 落檔、history 多一點、`/api/public/stats` 路由量 153,315 → 157,455。
- 釘住：`tests/test_deploy_artifacts.py` 兩條結構性斷言；regression_check 新增 `--failed`、
  timer 新鮮度、reports 目錄權限、API 可讀性四條。RUNBOOK §5.8a。

### F2 explore 磁碟快取從未在正式機生效（已修復並部署）
- 症狀：`FILET_EXPLORE_CACHE_PATH` 不在 API 有效環境 → 預設相對路徑在 `ProtectSystem=strict`
  下落檔失敗（journal 只有一行 warning）→ 每次 API 重啟 `/explore` 冷建空榜約 12 分鐘。
- 修法：drop-in `explore-cache.conf` → `/var/lib/filet-api/explore_index.json`（唯一同時在
  ReadWritePaths 內且 filet-api 可寫的位置）；03:27 UTC 快照落地 441 KB、25 rows。
  釘住：`test_deploy_artifacts.py` 一條、regression_check 兩條、RUNBOOK §5.3 改為必填。

### F3 ledger 白名單漏收 `internalTransfer`（實盤引擎，已修，待部署）
- 事實（真 payload）：HL「Send」／`usdSend` 在雙方 ledger 都是
  `{"type":"internalTransfer","usdc","user","destination","fee"}`；收方實收 `usdc−fee`
  （新地址首轉 fee=1.0），送方 perp 減 `usdc`。白名單沒有這個型別 → 只進 unknown_types。
- 後果：`follower_flow.py` 回撤基準校正看不到 Send **轉出** → 被當虧損 → 幻影回撤 → kill switch
  可能 flatten＋halt（工程原則 1 事故 #4 同型）；轉入不抬基準 → 之後真虧損被稀釋（fail-open）。
- 修法（T9＋T9b）：`signed_flow(delta, *, address)` 依查詢位址判方向；三個消費端傳 address；
  自轉自守衛（HL 實測回 `Cannot self-transfer.`，純防禦）；髒金額 → anomaly 不拋；接線回歸測試
  （reviewer 親測變異已殺死）。實測釘死：spot 轉帳型別是 `send` 不是 internalTransfer，本修法
  只影響 perp 桶，方向正確。opus 兩輪審查：**引擎側可部署**。
- 未併入：策略頁「真實入金」是否納入 Send（inbound 計入會讓 leader 自己兩個錢包來回 Send 灌大
  公開數字）→ 還原為只算 `deposit`，列 §5 裁決。

### F4 onboarding 精靈重新整理後跳過 verify → 客戶永遠不被啟用（前端＋API，已修，待部署）
- 事實：`deriveStep` 只看 status 是否 READY；`postVerify` 唯一呼叫點是 step 2 的按鈕；pending.json
  只在 `POST /api/onboard/verify` 寫。客戶簽完授權、入金後重新整理／換頁／隔天回來 → 從 step 3
  開始 → 走完精靈、簽了 leader → dashboard 顯示等待啟用 → watcher 永遠撿不到，**無任何錯誤畫面**。
  瀏覽器 E2E 第二次跑同一錢包就重現。
- 修法（T10＋T10b）：前端「已 READY 但本地無 step2Verified」→ 自動補打 verify 再放行（useRef
  只打一次）；後端 `POST /api/leaders/select`（精靈必經最後一步）在簽章落地後，不在 manifest 且
  READY → 補寫 pending（冪等；manifest 壞條目時不猜；HL／磁碟／JSON 失敗 → 200＋`pending_written=false`
  ＋`notifier.critical`）。瀏覽器實測兩條路徑都由 auto-verify 落地。opus 審查 W1–W4 全修。

### F5 `/api/me/authorizations` explorer URL 硬編主網（已修）
- testnet 部署下永遠查不到；改依 `network` 切換（`EXPLORER_URLS`），E2E S13 在 testnet 查到記錄。

### F6 `filet_regression_check.py` 漂移（已重寫）
- 舊版：sslip 主機、改版前九條路由、硬性預期 testnet、無 `--failed`。新版 66 條（路由 12＋動態／
  redirect、robots／sitemap host、HSTS、5 組公開 API 契約、12 條 GET＋3 條 POST 未授權 401、
  有效環境（drop-in）、timer 新鮮度、目錄權限、機密檔、磁碟）。`/leaderboard` 是 client-side
  redirect（主線程裁決接受，檢查改為兩形態皆可）。

## 4. 觀察（未改，記錄）

- 正式機 unit 環境變數住在 drop-in（`filet-api.service.d/*.conf`），grep 主檔會誤判；已改用
  `systemctl show -p Environment`。
- 前端連在 wagmi 未配置的鏈上時，簽章失敗被歸類成「使用者拒簽」，訊息誤導；正式客戶在
  Arbitrum／Ethereum 上不會遇到。
- SIWE cookie `secure=True`：本機測試必須用 `localhost`，`127.0.0.1` 會靜默掉 session。
- `/api/me/leader` 在 followers manifest 檔案不存在時是 503（刻意 fail-loud）；只影響全新環境。
- HL 對新地址首次 `usdSend` 收 $1；`userFillsByTime` 對活躍主網帳戶自 0 起查會回滿 2000 筆
  觸發 `FillsTruncatedError`（安全閥有效）。
- `pending_error` 目前只給 ops 告警與 API 回應，前端未顯示（若要顯示需走 copy.ts 雙語）。
- Playwright headless 下精靈的 5s 輪詢對 builder fee 確認明顯延遲（1–3 分鐘），測試改為
  直接輪詢後端；正式瀏覽器前景分頁不受影響。

## 5. 待使用者裁決／部署（T8）

1. **部署 F3（引擎）**：rsync `src/spark/exchange/ledger_flows.py`、`hyperliquid.py`、
   `scripts/vault_preflight.py` 後重啟 `filet-follower@fbac652…`（紅線 5：引擎重啟需使用者放行）。
2. **部署 F4＋F5（API＋前端）**：rsync `src/spark/publicapi/*`、`src/spark/config.py`、`web/`
   重 build（`NEXT_PUBLIC_SITE_ORIGIN=https://trade.filet.app`）→ restart `filet-api`、
   `filet-dashboard`。restart API 會讓 `/explore` 走磁碟快取（F2 已生效，不再空榜）。
3. **commit**：工作樹 31 檔改動＋新檔（tests/integration、web/e2e、fixtures）未 commit；
   建議一次 commit 並 push。
4. **裁決 A**：策略頁／trader 頁的「真實入金」要不要把 inbound `internalTransfer` 計入
   （若計入需同時倒扣 outbound，否則可被灌水）；目前維持只算 `deposit`。
5. **裁決 B**：`/leaderboard` 維持 client-side redirect（現狀）或改成伺服器層 30x。
6. 水龍頭錢包 `0x4229…9CE2` 目前約 260 USDC testnet，留給日後回歸；低於 ~280 前補一次。

## 6. 未覆蓋（誠實標註）

- 回撤 kill switch **真的被虧損觸發** 的路徑：testnet 上不可控，本輪只驗「簽章 → env → 引擎讀到
  參數」與 close-all 的收尾；熔斷全鏈路仍以 2026-07-19 人工驗證＋離線測試為準。
- 換 leader（已啟用帳號切換到另一個 leader）的引擎收斂路徑：E2E 未涵蓋。
- Stripe billing 端到端（測試模式）未跑。
- 多 follower 並行、長時間運行（>1 輪）的穩定性；本輪引擎皆 `--once`。
- 主網任何寫入：零（設計如此）。
- watcher 真的 `systemctl start` 在 Linux 上的行為：以注入 recorder 驗到指令字串為止；正式機
  既有 follower 是 7 月啟用的，本輪未新增真實客戶。

## 7. 產物

- 測試：`tests/integration/{harness,conftest,test_e2e_noncustodial,test_adapter_testnet,test_hl_contract}.py`、
  `tests/fixtures/hl_payload_keys/*.json`（35）、`web/e2e/{public-smoke,wallet-onboarding}.spec.ts`、
  `web/playwright.config.ts`、`tests/test_regression_check.py`。
- 工具：`scripts/filet_regression_check.py`（重寫）、harness CLI（`faucet-status`／`seed-faucet`／
  `keysvc-serve`／`mint-wallet`／`sweep-wallet`）。
- 文件：RUNBOOK §5.3（explore 快取必填）、§5.8a（日報部署）、§8 驗收 4（四層回歸流程）。
