# 自訂 vault 無痛上線＋follower 出入金校正 — 施工任務書（2026-07-31 第二批）

決策 spec：`~/projects/obsidian/pandora/filet/2026-07-31-custom-vault-follower-flow-spec.md`。
TDD：每 wave 先寫測試看紅再實作。全離線（socket-ban）。內部 Decimal。
基線：`5458ce6`，`uv run pytest` 2177 passed。

## 跨 wave 契約（先鎖定，W2/W3 平行的依據）

Preview 回應（`/api/leaders/preview` 與 select 的 `custom` dict）新增：
```json
"kind": "standard" | "vault",
"vault_checks": null | {"passed": bool, "failures": [{"name": str, "detail": str}]}
```
`vault_checks` 僅 kind=="vault" 時非 null；failures 只列 FAIL 項（PASS 不回傳細節）。
資訊性 WARN（flow-stats）不算 FAIL。

## Wave 1 — 流量映射統一（#3；先行，W2/W5 依賴）

檔案：`src/spark/exchange/ledger_flows.py`（新）、`hyperliquid.py`、
`scripts/vault_preflight.py`、`tests/test_ledger_flows.py`（新）。

1. 新模組單一定義：`FLOW_FIELDS = {"vaultDeposit": ("usdc", +1), "deposit": ("usdc", +1),
   "withdraw": ("usdc", -1), "vaultWithdraw": ("netWithdrawnUsd", -1)}` ＋
   `classify_delta(delta: dict) -> Decimal | None | str`——回 Decimal（有號流量）、
   None（缺金額欄位，呼叫端記 `{type}:missing-amount`）或原樣不認得的型別字串…
   ——**不要**用三態魔法回傳：實作成兩個函式
   `signed_flow(delta) -> Decimal | None`（白名單且欄位齊 → 有號值；否則 None）與
   `flow_anomaly(delta) -> str | None`（白名單外 → type 字串；白名單但缺欄位 →
   `"{type}:missing-amount"`；正常 → None）。
2. `hyperliquid.get_ledger_flows` 與 `vault_preflight._signed_flow` 改為 import 此模組，
   刪除各自的映射。行為不變（既有測試零修改通過是證據）。
3. 同源釘死測試：直接斷言兩個呼叫端模組**不再含**任何 `netWithdrawnUsd` 字面
   （`grep 式測試：import ast 或讀原始碼字串`，簡單做：讀兩檔原文 assert
   `"netWithdrawnUsd" not in source`，映射字面只允許存在於 ledger_flows.py 與測試）。

驗收：`uv run pytest -q` 全綠（2177 不減）、ruff 乾淨、grep 證據。

## Wave 2 — 准入 vault 偵測＋advisory 檢查（#1 後端）

檔案：`src/spark/publicapi/app.py`、`src/spark/publicapi/config.py`、
`src/spark/publicapi/hl.py`（若需）、`src/spark/filet/user_leaders.py`、
`scripts/vault_preflight.py`（檢查邏輯若需抽函式供 app 重用）、
`tests/publicapi_helpers.py`、`tests/test_publicapi_leaders.py`（找既有准入測試檔）。

1. 檢查邏輯重用：把 `vault_preflight.run_checks`（純函式）與資料抓取拆開，
   app 端重用同一 `run_checks`——**禁止**在 app.py 重寫檢查（W1 同源原則的延伸）。
2. `_admit_custom_leader`：`clearinghouse_state` 之後打 `hl.vault_details(addr)`；
   回應非 dict 或缺 name → kind="standard"、`vault_checks=null`；是 vault →
   抓 portfolio＋ledger（同 preflight 的資料面）→ `run_checks` → 契約欄位。
   transient 例外照 `_chain_activity` 既有語意上拋（502）。
3. FAIL 告警：任一 FAIL → `notifier.critical`（訊息含位址、FAIL 項名與 detail、
   「用戶可能已繼續，請人工核對」）＋ `logger.warning`。dedup key 含位址。
   Notifier 注入：`ApiConfig` 加 `tg_bot_token/tg_chat_id`（env
   `FILET_API_TG_BOT_TOKEN/FILET_API_TG_CHAT_ID`，預設空 → NullNotifier=log-only），
   照 CLAUDE.md「通知一律走 Notifier 注入」慣例；找 app 既有的依賴注入點掛。
4. `record_user_leader(path, *, address, added_by, kind="standard")`：條目 dict 多
   `kind` 鍵；app.py 呼叫點傳 `custom["kind"]`。冪等分支行為不變（不回填——backfill 的職責）。
5. `tests/publicapi_helpers.py` 的 `FakeHL` 補 `vault_details`（預設回 `None` 模擬
   非 vault——這正是實際 API 對非 vault 的行為）＋可注入 vault fixture。

測試：非 vault 位址 → kind=standard、零額外欄位破壞（既有測試不改即過）；
vault 全 PASS → kind=vault、vault_checks.passed=true、registry 條目含 kind、無告警；
vault 有 FAIL → 用戶仍可 select 成功、vault_checks.failures 非空、notifier 收到
critical；vaultDetails transient 例外 → 502；vault_details 回 None → 不炸。

## Wave 3 — 前端（#1 UI；依賴契約，不依賴 W2 實作）

檔案：`web/src/lib/api.ts`、`web/src/lib/copy.ts`、`web/src/app/leaders/page.tsx`、
`web/src/app/leaders/page.test.tsx`、`web/src/lib/customLeader.ts`（僅若 reject 契約變）。

1. `LeaderPreviewResp` 加契約兩欄位。
2. preview 卡：kind=vault → 資訊性 badge「Vault」＋一句說明（自動套 20x 帽與
   申贖中性化——語氣中性，不是警告）；`vault_checks.failures` 非空 → `ops-alert`
   風格警語列出 FAIL 項＋「此 vault 的帳本形態引擎可能算不準，繼續前請理解風險」。
   **不新增任何步驟**（checkbox／確認流程照舊）。
3. `copy.ts` 新增 `vaultBadge`／`vaultNote`／`vaultCheckWarning` 文案（繁中，照
   該檔既有語氣）。
4. 測試：CUSTOM_PREVIEW fixture 加欄位；vault badge 渲染、FAIL 警語渲染、
   standard 不顯示任何 vault 元素。

驗收：`export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH" && cd web && npm test` 全綠。

## Wave 4 — backfill CLI（#1 存量）

檔案：`scripts/backfill_leader_kinds.py`（新）、`tests/test_backfill_leader_kinds.py`（新）。

沿 `scripts/revoke_leader.py` 形狀：argparse（`--registry` 路徑、`--dry-run`）、
`registry_lock` 內 read-modify-write、原子寫、逐條目打 `HLGateway.vault_details`
（可注入 fake）、是 vault 且 kind!=vault → 改寫；自我驗收：重載
`merge_leaders(load_leaders, load_user_leaders)` 斷言每筆 kind 與探測結果一致；
任一步失敗非零退出。輸出：每條目一行（位址、原 kind、新 kind、動作）。
冪等：重跑零改動。網路失敗：該條目標 error、繼續其餘、結尾非零退出（部分成功要看得出來）。

測試：fake gateway 三情境（vault／非 vault／探測失敗）、冪等重跑、dry-run 不寫檔、
flock 與原子寫沿用（讀 revoke_leader 測試怎麼錨的）。

## Wave 5 — follower 出入金校正（#2 核心）

檔案：`src/spark/copytrade/follower_flow.py`（新）、`config.py`、`loop.py`、
`web/src/lib/copy.ts`（警語改寫，與 W3 同檔不同 key——W3 先做完再做本項，避免衝突：
**W5 的前端部分等 W3 回報後執行**，或由主對話最後統一改）、
`tests/test_follower_flow.py`（新）、`tests/test_copy_loop.py` 追加。

1. `config.py`：`follower_flow_correction_enabled: bool = True`
   （env `COPY_FOLLOWER_FLOW_CORRECTION`）。預設**開**——這是正確性修復；
   逃生閥語意寫進 docstring。
2. `follower_flow.py`：
   - 狀態檔 `var/copytrade/follower_flow.json`：`{"last_processed_ms": int}`，
     load/save 照 costbreaker 三件套慣例（壞檔回 None＋告警、原子寫、save 不拋）。
   - `apply_follower_flows(root, adapter, address, notifier, *, now_ms) -> None`：
     - 標記缺失 → 寫 `now_ms` 後 return（初始化，不回溯）。
     - `flows, anomalies = adapter.get_ledger_flows(address, last+1)`；
       例外 → warn（dedup）→ return（下輪自 last 重試，事件不丟）。
     - anomalies 非空 → warn（含清單）。
     - 過濾 `time_ms > last`；空 → 標記推進到 now_ms、return。
     - `net = Σ usdc`。**順序：先寫標記（推進到 now_ms），再調整**——crash 最壞
       方向＝該筆未校正（回到今天行為，fail-safe），絕不雙套（fail-open）。
     - 調整：equity samples 檔內全部樣本 value `+net`（讀-改-寫，原子）；
       lifetime peak `+net` 後 floor 於 0（`max(Decimal("0"), …)`）。
       調整後發 `notifier.warn`（資訊性：偵測到出入金 net=…，回撤基準已校正）。
   - **不動** `evaluate()`／`check_drawdown`／`check_total_drawdown`；
     唯一新斷言：確認 killswitch 對 `lifetime_peak <= 0` 已有除零守衛，缺就補
     （守衛語意：peak ≤ 0 → 不判 total dd）。
3. `loop.py` 步驟 2：`perp_equity_view` **之前**呼叫
   `if settings.follower_flow_correction_enabled: apply_follower_flows(...)`（
   now_ms 同檔既有時間源）。`risk_controls_enabled=False` 時**仍要跑**（樣本照舊
   持續累積的同一理由：未來開風控時基準要正確）。
4. 前端警語（由主對話在 W3 合流後統一改）：`copy.ts` 兩處「轉出會被視為虧損」
   改為「出入金會自動校正回撤基準，不再視為虧損；但轉出仍會等比縮小跟單倉位，
   建議先停止跟單再大額轉出」。

數值錨例（預註冊，測試硬編）：樣本 `[(t1, 1000), (t2, 1200)]`、lifetime peak 1200：
- 出金 300（net=-300）→ 樣本 `[700, 900]`、peak 900；current=900 時 dd=0（原會誤判 25%）。
- 入金 500（net=+500）→ 樣本 `[1500, 1700]`、peak 1700；current=1700 → dd=0；
  current=1400 → rolling dd=(1700-1400)/1700≈17.6%（入金不得掩蓋既有虧損）。
- 出金 1500（net=-1500，超過 peak）→ peak floor 0、樣本 `[-500, -300]`；
  killswitch 對 peak≤0 不判 total dd（除零守衛測試）。
- 標記缺失首輪：只初始化、零調整。
- 例外輪：標記不動；下輪同批流量恰好套用一次（exactly-once 測試）。
- 逃生閥 false：`get_ledger_flows` 不被呼叫。

## Wave 6 — heartbeat kind＋文件（#4＋收尾）

1. `run_copytrade.make_heartbeat_publisher._publish`：payload 加 `leader_kind`
   （來源＝本輪 resolution.kind——與 apply_vault_policy 同源）。`engine_health`
   讀端若有 schema 驗證同步補；dashboard 顯示點若一行內可加就加，否則 payload 為準。
2. `deploy/RUNBOOK.md`：§5.5 vault 節補「自訂路徑已支援自動偵測（advisory）」＋
   backfill 一次性步驟＋API TG 兩個新 env 鍵（§5.3 佔位符表同步）；
   `filet_auto_activate.py:329` 過期註解修正。
3. `docs/superpowers/plans/2026-07-22-open-items.md`：銷帳四筆（1 自訂偵測、
   2 follower 債、3 映射雙實作、4 heartbeat）——標記已償＋commit 參照，留
   transient 窗口與 vault→standard 不還原兩筆。
4. `deploy/follower.env.example`／`.autoactivate.example`：註解補
   `COPY_FOLLOWER_FLOW_CORRECTION`（預設 true，一般不用設）。

## 收尾（主對話）

opus fresh 審查（重點：follower_flow 的 exactly-once 與符號方向、准入 advisory
路徑的告警可達性、映射統一後三端行為不變）→ 逐條親驗 → 修復 → 親跑
pytest/ruff/npm test → 部署 prod（rsync 流程照 §3.2＋`.venv/bin/python` 直跑）→
prod backfill 實跑回報 → 服務重啟與健康驗證 → git 整理（merge main、tag）→ 文件收尾。
