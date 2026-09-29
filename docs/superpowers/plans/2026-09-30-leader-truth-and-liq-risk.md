# leader 顯示真相化 ＋ leader 強平風險指標 Implementation Plan

> **For agentic workers:** 依 `~/.claude/CLAUDE.md` 單 session 派工制執行：每個 task 標 `@inline`，派 `builder`；主線程逐 task 親跑驗收指令後才派下一個。Steps 用 `- [ ]` 追蹤。

狀態：**2026-09-30 使用者確認共識，派工中**。裁決：風險指標照 plan（距強平 15/30 分級＋交易員頁維持保證金比）；探索列在曝險欄旁加一欄；部署順序照 plan（另一條線先 commit 乾淨再整包部署）。

**Goal:** (1) 客戶儀表板顯示引擎實際生效的 leader，而不是名冊（followers.json）裡的初始值；(2) 管理端能一次性把名冊改對；(3) 交易員頁與探索頁顯示 leader 的「距強平幅度」與「維持保證金比」，讓客戶自己看得到 leader 的爆倉距離。

**Architecture:** 名冊仍只由管理端寫（安全邊界不動），新增一支管理 CLI 做「更新既有條目的 leader」。`/api/me/leader` 改為優先讀引擎心跳（`<exchange_dir>/engine/health/<account_id>.json`，600 秒內有效），心跳缺席／過期才退回名冊，並回傳 `leader_source`。風險指標從**已經在抓**的 `clearinghouseState` 同一次回應推導（零新增 HL 額度）：每個部位交易所回報的 `liquidationPx` 對 mark（`positionValue/|szi|`）的距離取最近者，加上 `crossMaintenanceMarginUsed/accountValue`。探索快照 schema 變動 → `EXPLORE_INDEX_VERSION` 升版，部署照 RUNBOOK §5.8c 先預熱。

**Tech Stack:** Python 3.11／FastAPI（`src/spark/publicapi`）、Next.js（`web/`，vitest）、pytest（全離線，socket-ban）。

---

## 背景與既有裁決（builder 必讀）

- 名冊（`followers.json`）**刻意**不被 API／引擎更新（`src/spark/filet/leader_change_apply.py` 檔頭）：API 被打穿也改不了「客戶的錢跟誰」。本 plan 不改這條邊界。引擎實際生效的 leader 只活在引擎帳本 `state/<id>/var/filet/leader_change_ledger.json` 的 `applied`，並每輪寫進心跳 `leader.address/source/kind`（`src/spark/filet/engine_health.py:213`）。
- 客戶儀表板 `/api/me/leader`（`src/spark/publicapi/app.py:1637-1676`）目前讀名冊，所以 f438 看到「跟隨 Filet Alpha，換到 0xedea 處理中」永遠不會消失。ops 面板已經讀心跳（`src/spark/publicapi/ops.py:396-473`），儀表板要跟它同源。
- `scripts/filet_activate.py:133-134` 對名冊已有條目**一律拒絕**，所以「更新既有條目」需要新 CLI。
- 既有裁決一（`src/spark/publicapi/app.py:357`）：leader 目錄**刻意不外流** `withdrawable`／`total_margin_used`。本 plan 不外流這兩個欄位；風險指標用**強平價距離**與**維持保證金比**，兩者都是交易所回報或同一次回應的代數推導。
- 既有裁決二（2026-09-29 使用者，memory `follower-copy-no-scary-warnings`）：介面不放嚇人紅框／急迫文案，資訊用數字顏色與中性 hint 表達。風險指標只做「數字＋顏色分級」，不加警語。
- 探索列與交易員頁的績效數字一律出自 `src/spark/filet/trader_stats.py`／`hl_explore` 的同一組函式（專案 CLAUDE.md 慣例）；本 plan 新增的風險函式同樣**只有一個定義點**，探索與交易員頁共用。

## 檔案地圖

| 檔案 | 動作 | 職責 |
|---|---|---|
| `scripts/set_follower_leader.py` | 新增 | 管理端 CLI：更新名冊既有條目的 leader_address（預設 dry-run） |
| `tests/test_set_follower_leader.py` | 新增 | 上述 CLI 的離線測試 |
| `src/spark/publicapi/app.py:1637-1676, 1696-1726` | 修改 | `/api/me/leader` 優先心跳、回 `leader_source`；`_pending_leader_change` 以有效 leader 比對 |
| `tests/test_api_me_leader*.py`（既有測試檔，builder 以 `rg "me/leader" tests` 定位） | 修改 | 心跳優先／退回名冊／過期心跳三情境 |
| `web/src/lib/api.ts`、`web/src/app/settings/page.tsx`、`web/src/lib/copy.ts` | 修改 | 型別加 `leader_source`；來源為名冊時顯示中性 hint |
| `src/spark/publicapi/hl_explore.py` | 修改 | 新增 `risk_from_clearinghouse()`；`ExploreRow` 加三欄；enrich 填值；`EXPLORE_INDEX_VERSION` 4→5 |
| `src/spark/publicapi/explore_publisher.py:112` | 修改 | 缺席列的預設值補三欄 |
| `src/spark/publicapi/app.py`（ExploreRow→JSON 投影處、`public_trader_detail` :2822 附近） | 修改 | 探索列與交易員詳情回 `risk` 物件 |
| `tests/test_hl_explore*.py`、`tests/test_api_public_trader*.py` | 修改 | 風險函式單元測試＋端點投影測試 |
| `web/src/lib/publicApi.ts`、`web/src/app/explore/page.tsx`、`web/src/app/traders/[address]/page.tsx`、`web/src/lib/copy.ts`、`web/src/styles/globals.css` | 修改 | 型別／解析、探索列「距強平」欄、交易員頁風險列、文案、顏色 class |

---

## Part 1：leader 顯示真相化

### Task 1 `@inline`：管理端 CLI `scripts/set_follower_leader.py`

**Files:** Create `scripts/set_follower_leader.py`；Test `tests/test_set_follower_leader.py`。
既有慣例先讀：`scripts/filet_activate.py`（`_resolve_leader`、`write_json_atomic`、`load_followers` 重讀驗證、`require_leaders_path`）、`src/spark/filet/leaders.py:229 is_still_permitted`、`src/spark/filet/followers.py`。

行為：
```
用法: uv run python -m scripts.set_follower_leader --account-id <id> --leader 0x... \
        [--manifest <路徑>] [--leaders <白名單絕對路徑>] [--user-leaders <registry 路徑>] [--yes]
```
- 預設 dry-run：印「account_id／user_address／舊 leader → 新 leader／白名單判定」，不寫檔。`--yes` 才寫。
- 名冊找不到該 account_id → exit 2（本 CLI 只更新，不新增；新增走 filet_activate）。
- 新 leader 經 `normalize_hex_address` 正規化；必須通過 `is_still_permitted`（合併精選白名單＋user registry，合併方式**照引擎** `leader_resolve.py` 的做法，builder 讀該檔找出引擎怎麼合併兩份清單並重用同一函式，不得自己重寫合併邏輯）；不通過 → exit 3、不寫。
- 新 leader == 舊 leader → 印「無變更」exit 0，不寫。
- 寫入：`write_json_atomic(manifest, data, mode=0o644)` 後 `load_followers(manifest)` 重讀驗證（與 filet_activate 同形）。
- **不**重啟引擎、**不**碰 leader_changes.json／pending：引擎每輪重讀名冊，名冊 leader 與帳本 `applied` 相同時不產生變更事件；auto-activate watcher 會在下一輪因 manifest == 目標而回收 leader_changes.json 的記錄（`scripts/filet_auto_activate.py:575-577`）。docstring 要把這段因果寫清楚。
- 不印任何私鑰／簽章欄位（名冊本來就沒有，但 docstring 註明本 CLI 只碰 leader_address 一欄）。

測試（`tests/test_set_follower_leader.py`，tmp_path 建名冊＋白名單）：
1. dry-run 不改檔（mtime／內容不變）且輸出含舊→新。
2. `--yes` 寫入後 `load_followers` 讀到新 leader，其餘欄位逐字不變。
3. 新 leader 不在白名單 → exit 3、檔案不變。
4. account_id 不存在 → exit 2。
5. 新舊相同 → exit 0、檔案不變。

- [ ] 寫測試 → 跑確認紅 → 實作 → 綠 → `uv run ruff check scripts tests`。

驗收：`uv run pytest tests/test_set_follower_leader.py -q` 全綠；`uv run python -m scripts.set_follower_leader --help` 可印用法（import 階段不觸網）。

### Task 2 `@inline`：`/api/me/leader` 優先引擎心跳

**Files:** Modify `src/spark/publicapi/app.py:1637-1676`（`me_leader`）、`:1696-1726`（`_pending_leader_change`）；Test：既有 `me/leader` 測試檔（`rg -n "me/leader" tests -l`）。
先讀：`src/spark/filet/engine_health.py:287-330`（`HeartbeatRead`：`status in {"ok","missing","stale","unreadable"}`，`data` 只在 ok 時非 None）、`:120-132 heartbeat_path_for(exchange_dir, account_id)`、`src/spark/publicapi/ops.py:396-473`（心跳如何被投影，**重用它的讀法**，不要第二種讀心跳的方式）。

改法：
```python
# me_leader 內，取得 mine 之後：
hb = read_heartbeat(heartbeat_path_for(cfg.exchange_dir, account_id), now_s=time.time())
engine_leader = None
if hb.status == "ok":
    engine_leader = (hb.data.get("leader") or {}).get("address")  # 正規化小寫比對
if engine_leader:
    leader, leader_source = engine_leader, "engine"
else:
    leader, leader_source = mine.leader_address, "manifest"
```
- 回應新增 `"leader_source": "engine" | "manifest"`；`pending_change` 改用**有效** leader 比對（`_pending_leader_change(account_id, leader)` 不變，只是傳進去的 leader 變了）。
- `note` 三種：engine →「這是引擎目前為你跟隨的 leader。」（不變）；manifest 且心跳 missing/stale/unreadable →「引擎回報暫時不可用，以下為登記的 leader；引擎恢復回報後會自動更新。」；`mine is None` 分支不變。
- 名稱查詢：`load_leaders(cfg.leaders_path)` 查不到時再查 user registry（0xedea 這種客戶自加的 leader 在精選白名單裡沒有名字；registry 的 `name` 欄位就是位址本身，查到也只是位址 → 前端照舊顯示縮寫）。builder 先確認 registry 的載入函式名（`src/spark/filet/user_leaders.py`）。
- 心跳讀取的任何例外不得讓端點 5xx：`read_heartbeat` 已對 IO／JSON 錯誤回 `unreadable`，不再包 try。

測試（三情境，心跳檔寫在 tmp exchange_dir，`written_at_s` 用注入的 now）：
1. 心跳 ok 且 leader=0xedea、名冊=0xfb9c → 回 0xedea、`leader_source="engine"`、`pending_change` 為 None（記錄目標==有效 leader）。
2. 心跳缺席 → 回名冊 0xfb9c、`leader_source="manifest"`、note 為「暫時不可用」句。
3. 心跳過期（age > 600）→ 同 2。
4. 既有測試全部維持通過（`indeterminate`／`not_activated` 分支不動）。

驗收：`uv run pytest tests/ -q -k "me_leader or api_me"` 全綠；`uv run pytest -q` 全綠；`uv run ruff check src tests`。

### Task 3 `@inline`：前端 settings 頁顯示來源

**Files:** Modify `web/src/lib/api.ts`（`/api/me/leader` 回應型別加 `leader_source: "engine" | "manifest"`，解析時缺鍵視為 `"manifest"`）、`web/src/app/settings/page.tsx:850-870`（leader 區塊）、`web/src/lib/copy.ts`（`settings` 區塊 ZH/EN 對稱加 `leaderSourceManifestHint`）、對應測試 `web/src/app/settings/page.test.tsx`、`web/src/lib/api.test.ts`。

- `leader_source === "manifest"` 且 `leader_address !== null` 時，在現有 leader 行下方多一行 `className="hint"` 中性文案：ZH「引擎回報暫時不可用，顯示登記值」／EN "Engine report temporarily unavailable; showing the registered leader"。engine 時不顯示任何多餘東西。
- 不加顏色、不加 icon（既有裁決二）。

驗收：`export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH" && cd web && npm test -- --run` 全綠；`copy.test.ts` 的 EN 無 CJK 檢查通過。

### Task 1-op（主線程親跑，不派工）：正式機更新 f438 名冊

部署 Task 1 的 CLI 後，在正式機：
```bash
cd /opt/filet/spark && sudo -u filet-api .venv/bin/python -m scripts.set_follower_leader \
  --account-id f438b3bbc83b44c483f6c6c8d2d5664453791dce9 \
  --leader 0xedeaa967b0100a4df613533df634ffe6cb5fd773 \
  --manifest /opt/filet/spark/var/filet/followers.json \
  --leaders /opt/filet/spark/var/filet/leaders.json \
  --user-leaders /var/lib/filet-exchange/user_leaders.json          # 先 dry-run 看輸出
# 確認後加 --yes；執行身分要能寫名冊（名冊目前 owner 見 RUNBOOK §2 權限表，builder 不必管，主線程現場查 ls -l 決定用 root 或 filet-api）
```
驗收：`/api/me/leader`（以 f438 的 session）回 0xedea 且 `pending_change` 為 null；`/var/lib/filet-exchange/leader_changes.json` 內 f438 的記錄在 auto-activate 下一輪被回收；引擎 journal 無 critical、部位無變動（`clearinghouseState` 前後比對）。

## Part 2：leader 強平風險指標

### Task 4 `@inline`：單一定義點 `risk_from_clearinghouse()`

**Files:** Modify `src/spark/publicapi/hl_explore.py`（放在 `exposure_from_clearinghouse` :699 旁）；Test `tests/test_hl_explore_risk.py`（新檔）。
先讀：`hl_explore.py` 的 `_parse_positions`（部位解析已有，重用它取 `szi`／`coin`；若它沒有保留 `liquidationPx`／`positionValue`，擴充它而不是另寫一個解析器）、`src/spark/publicapi/app.py:496`（`positionValue/|szi|` 推 mark 的既有寫法與註解，沿用同一推導）。

```python
@dataclass(frozen=True)
class RiskSummary:
    liq_distance_pct: float | None   # 最近一個部位距強平的百分比（正數；None＝無部位或交易所未回 liquidationPx）
    liq_coin: str | None             # 那個部位的幣
    maint_ratio: float | None        # crossMaintenanceMarginUsed / accountValue（None＝缺欄位或 accountValue<=0）

def risk_from_clearinghouse(ch_state: dict | None) -> RiskSummary:
    ...
```
規則（全部 Decimal 計算，最後才轉 float，與 repo 慣例一致）：
- 對每個 `assetPositions[i].position`：`szi != 0` 且 `liquidationPx` 不是 None；`mark = positionValue / |szi|`；多單 `dist = (mark − liq) / mark`，空單 `dist = (liq − mark) / mark`；`dist` 取所有部位最小值（負值也照實回，代表已越過強平線，交易所尚未執行）。
- `maint_ratio = crossMaintenanceMarginUsed / accountValue`；任一缺鍵或 accountValue ≤ 0 → None。**欄位名是假設不是事實**（工程原則 1 事故 #6）：測試 fixture 必須用**真實回應**——用 2026-09-30 抓到的 0xedea 回應形狀：`marginSummary.accountValue="1278231.985074"`、`crossMaintenanceMarginUsed="378974.694601"`、PUMP `szi="961627241.0"`、`liquidationPx="0.0041602315"`、`positionValue`＝builder 用 `szi × 0.005738` 造一個；預期 `liq_distance_pct ≈ 27.5`、`maint_ratio ≈ 0.296`。
- 百分比輸出四捨五入到 1 位小數（`quantize(Decimal("0.1"))`），ratio 到 3 位。
- 只用 `_parse_positions` 已經解析的 perp 部位；`ch_state is None` → 全 None。

測試：(1) 上述真實形狀；(2) 空 `assetPositions` → 全 None；(3) `liquidationPx: null`（follower kPEPE 那種）的部位被跳過、其他部位照算；(4) 空單方向公式；(5) 已越線（mark < liq）回負數。

驗收：`uv run pytest tests/test_hl_explore_risk.py -q` 全綠。

### Task 5 `@inline`：探索列加三欄 ＋ 快照版本升級

**Files:** Modify `src/spark/publicapi/hl_explore.py`（`ExploreRow` :385-458 加 `liq_distance_pct: float | None = None`、`liq_coin: str | None = None`、`maint_ratio: float | None = None`；enrich 填值處 :507-508 與 :851-852 兩處同形加入；`EXPLORE_INDEX_VERSION = 4` → `5` :274）、`src/spark/publicapi/explore_publisher.py:112`（缺席列預設 None）、`src/spark/publicapi/app.py`（ExploreRow → JSON 的投影函式：builder 以 `rg -n "exposure_pct" src/spark/publicapi/app.py src/spark/publicapi/explore_publisher.py src/spark/publicapi/explore_store.py` 定位，同形加 `"risk": {"liq_distance_pct":…, "liq_coin":…, "maint_ratio":…}`）。
Test：既有 `tests/test_hl_explore*.py` 中 enrich 的測試加斷言；投影測試加 `risk` 鍵。

- 值一律出自 Task 4 的 `risk_from_clearinghouse(ch_state)`，**不得**在 enrich 內另算。
- `EXPLORE_INDEX_VERSION` 升版的後果：舊快照作廢，部署必須照 RUNBOOK §5.8c 先本機預建 300 池快照再重啟（見本 plan 部署節）。builder 在 `hl_explore.py` 版本常數旁加一行日期註記。
- 若 `explore_store.py` 有 schema／欄位白名單（builder 以 `rg -n "exposure_pct" src/spark/publicapi/explore_store.py` 確認），同形補上。

驗收：`uv run pytest -q -k explore` 全綠；`uv run pytest -q` 全綠。

### Task 6 `@inline`：交易員詳情回 `risk`

**Files:** Modify `src/spark/publicapi/app.py:2822` 附近（`exp_dir, exp_pct = hl_explore.exposure_from_clearinghouse(ch_state)` 旁同形加 `risk = hl_explore.risk_from_clearinghouse(ch_state)`，回應加 `"risk": {...}` 三鍵，`ch_state` 為 None 時三鍵皆 null）。Test：既有 `public_trader_detail` 測試檔加斷言（`rg -n "public/traders" tests -l`）。
- 同一次 `ch_state`（已在 `_cached_trader_data` 內），零新增上游呼叫。

驗收：`uv run pytest -q -k trader` 全綠。

### Task 7 `@inline`：前端探索列與交易員頁

**Files:** Modify `web/src/lib/publicApi.ts:387-421`（`ExploreRow` 加 `risk: { liq_distance_pct: number | null; liq_coin: string | null; maint_ratio: number | null }`，解析器 :627-653 同形補、缺鍵全 null；`TraderDetail` 型別同樣加 `risk`）、`web/src/app/explore/page.tsx`（列內在曝險欄旁加「距強平」欄：`liq_distance_pct == null` 顯示 `—`；否則 `${pct.toFixed(1)}%`，`title` 帶 `liq_coin`）、`web/src/app/traders/[address]/page.tsx:293-297`（曝險行旁加風險行：距強平 `X%（幣）`、維持保證金比 `Y%`）、`web/src/lib/copy.ts`（`explore` 與 `traders` 區塊 ZH/EN 各加 `liqDistanceLabel`「距強平」/"To liquidation"、`maintRatioLabel`「維持保證金比」/"Maintenance ratio"、`liqDistanceHint`「以交易所回報的強平價對現價計算，取最近的一個部位」/"Exchange-reported liquidation price vs mark, nearest position"）、`web/src/styles/globals.css`（`.risk-num--warn`＝既有黃色變數、`.risk-num--danger`＝既有紅色變數；只改數字顏色，不加框）。
Tests：`web/src/lib/publicApi.test.ts`（解析：完整／缺鍵／null）、explore 與 traders 頁既有測試加渲染斷言（`—`、百分比、class）。

顏色分級（待使用者確認，見共識問題）：`liq_distance_pct < 15` → danger；`< 30` → warn；其餘不變色；`maint_ratio` 不變色只顯示。負值顯示為 `0.0%` 並套 danger（已越線）。

- 文案不得含「危險」「請注意」「爆倉」等急迫詞（既有裁決二）；label 用「距強平」這種中性名詞。

驗收：`export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH" && cd web && npm test -- --run` 全綠；`npm run build` 通過。

---

## 審查後修正（2026-09-30 reviewer 第一輪；主線程逐條複驗後裁決）

事實複驗：撤銷 leader 後引擎每輪心跳寫 `leader: {address: None, source: None, kind: None}` 且 `result: "revoked"`（`scripts/run_copytrade.py:759-770, 397`）；心跳 `leader.source` 正常值為 `"manifest"`／`"env_default"`／簽章 override 的來源名（builder 以 `rg -n "source=" src/spark/filet/leader_change_apply.py` 確認實際字串）；`clearinghouseState` 頂層同時有 `crossMaintenanceMarginUsed` 與 `crossMarginSummary`（fixture `tests/fixtures/hl_payload_keys/mainnet-clearinghouseState.json:4-5`）。

### Task 8 `@inline`：後端修正（C1 後端、C2、W1、W2、W3、S1、S2、S3、S5）

**Files:** `src/spark/publicapi/app.py`（`me_leader`）、`src/spark/publicapi/hl_explore.py`、`scripts/set_follower_leader.py`、對應測試。

- **C1＋W1＋W5（`me_leader` 狀態契約）**：心跳 `status == "ok"` 時分三路——
  1. `leader.address` 有值且 `leader.source == "env_default"` → `status="engine_default"`、`leader_address=該位址`、`leader_source="engine"`；
  2. `leader.address` 有值且 source 為其他 → `status="following"`、`leader_source="engine"`；
  3. `leader.address` 為 None（撤銷／收尾後）→ **新狀態** `status="engine_no_leader"`、`leader_address=None`、`leader_name=None`、`leader_source="engine"`、`pending_change=None`、note「引擎目前沒有跟隨任何 leader（跟單已撤銷或已停止）；請看本頁的風控與狀態區塊。」
  心跳非 ok → 名冊退回，`status` 照舊規則（名冊有 leader → following、無 → engine_default），`leader_source="manifest"`。docstring 的 `status` 定義同步改寫（`following` 不再等於「名冊明確指定」，而是「有效 leader 來自簽章或名冊」）。測試補：撤銷情境（心跳 ok、leader.address None、result revoked）回 `engine_no_leader`；env_default 情境回 `engine_default` 且帶位址。
- **C2（`_maint_ratio` 分母）**：改用 `ch_state["crossMarginSummary"]["accountValue"]`；缺鍵或 ≤ 0 → None。docstring 寫明「分子分母同為 cross 帳本（工程原則 1）；isolated 部位不在此比值內」。錨例（純 cross 帳戶）數值不變 0.296；補一個含 isolated 部位的 fixture（`marginSummary.accountValue` > `crossMarginSummary.accountValue`）證明用的是 cross 值。
- **W3（殘量部位）**：`_liq_distance` 跳過 `position_value < RISK_MIN_POSITION_FRACTION × marginSummary.accountValue`（`RISK_MIN_POSITION_FRACTION = Decimal("0.01")`，模組常數附註解：低於權益 1% 的部位無法實質傷害帳戶，不拿它塗色）；accountValue 缺或 ≤ 0 → 不過濾。測試：$1M 帳戶帶一條 $5、距強平 3% 的殘量與一條 $500k、距強平 40% 的主部位 → 回 40.0。
- **S1**：`_safe_decimal` 對 `Decimal` 非 `is_finite()` 回 None。測試：`liquidationPx: "NaN"` → 該部位跳過。
- **W2＋S2＋S3（CLI）**：加 `--exchange-dir <路徑>`（選填）：有給就 `engine_health.read_heartbeat(heartbeat_path_for(exchange_dir, account_id), now)`，dry-run 與 `--yes` 都印「引擎心跳：status／leader.address／leader.source」；心跳 ok 且 `leader.address` 與新 leader 不同 → `--yes` 拒絕（exit 4，訊息說明「寫入會讓引擎判定 leader 變更並收斂部位，動真錢；若確定要換請走客戶簽章流程」），除非 `--allow-engine-mismatch`。名冊／白名單／registry 讀取失敗 → exit 2＋一行訊息（無 traceback）。dry-run 印名冊檔的 owner／mode，euid 與 owner 不同時印一行提醒（`os.replace` 會改 owner）。docstring 退出碼表補 2／3／4。測試：心跳不符 → exit 4 不寫；`--allow-engine-mismatch` → 寫；壞 JSON 白名單 → exit 2。
- **S5**：`EXPLORE_INDEX_VERSION` 旁註解補一行：v3→v4 遷移路徑保留的理由（或若無理由則刪除該遷移並更新測試，builder 讀 `_row_from_dict`／遷移函式後擇一並在回報說明）。

驗收：`uv run pytest -q` 全綠；`uv run ruff check src tests scripts`；`uv run pytest tests/test_api_me_leader.py tests/test_hl_explore_risk.py tests/test_set_follower_leader.py -q` 列出新增測試名。

### Task 9 `@inline`：前端修正（C1 前端、W4、S4）

**Files:** `web/src/lib/api.ts`（status 聯集加 `"engine_no_leader"`）、`web/src/lib/copy.ts`（`settings.leader.notesByStatus.engine_no_leader` ZH「引擎目前沒有跟隨任何 leader（跟單已撤銷或已停止）。」／EN "The engine is not following any leader right now (copy trading was revoked or stopped)."；刪除未渲染的 `explore.maintRatioLabel` ZH/EN）、`web/src/app/settings/page.tsx`（`engine_no_leader` 走 notesByStatus，不顯示 manifest hint，`showActions` 不含它）、`web/src/app/explore/page.test.tsx:330-340`（ROW_B 的 `—` 真的斷言到）、對應測試。

驗收：`cd web && npm test -- --run` 全綠；`rg -n "maintRatioLabel" web/src/lib/copy.ts` 只剩 `traders` 區塊 ZH/EN 各一；`rg -n "engine_no_leader" web/src` 命中 api.ts、copy.ts（ZH/EN）、settings 測試。

Task 1-op 更新：正式機指令加 `--exchange-dir /var/lib/filet-exchange`，dry-run 輸出必須顯示引擎心跳 leader == 0xedea 才加 `--yes`。

## 第二輪審查後修正（2026-09-30；主線程裁決：C1、C2、W1、W2、W3、S1、S3 採納；S2 不採納）

### Task 10 `@inline`：後端（C1、C2、W1、W2、S3）

**Files:** `src/spark/publicapi/hl_explore.py`、`src/spark/publicapi/app.py`（`me_leader`）、`scripts/set_follower_leader.py`、`tests/test_hl_explore_risk.py`、`tests/test_api_me_leader.py`、`tests/test_set_follower_leader.py`。

- **C1**：`_maint_ratio` 的兩個輸入改走 `_safe_decimal`（非有限值 → None），`account_value <= 0` 判斷放在取值之後、不在 try 之外拋。測試：`crossMarginSummary.accountValue="NaN"` 與 `"Infinity"` 皆回 `maint_ratio=None`（不是 0.0、不拋）。
- **C2**：CLI 心跳核對改為：心跳 `status=="ok"` 且（`leader.address` 為 None **或** 與新 leader 不同）→ exit 4；訊息對 None 情境另寫一句「引擎目前沒有跟隨任何 leader（已撤銷或停止），重新啟用必須走客戶簽章流程（CLAUDE.md 紅線 5）」。測試：revoked 心跳（`leader.address` None、`result="revoked"`）→ `--yes` exit 4 不寫。
- **S3**：心跳 `stale`／`unreadable` 也需要 `--allow-engine-mismatch` 才能 `--yes`（exit 4，訊息「引擎心跳過期或不可讀，無法確認寫入不會觸發收斂」）；只有 `missing`（從未有心跳＝從未啟動）放行並印 status。測試：stale 心跳 → exit 4；`--allow-engine-mismatch` → 寫入。
- **W1**：`me_leader` 分清「`leader.address` 鍵存在且為 None」（→ `engine_no_leader`）與「值是壞字串／非 str」（→ 名冊退回、`leader_source="manifest"`，note 用「引擎回報格式異常，以下為登記的 leader」並 `logger.error` 留痕）。測試：壞位址情境回名冊值且 status 依名冊，不是 `engine_no_leader`。
- **W2**：`tests/test_hl_explore_risk.py` 的 NaN 測試 fixture `position_value` 提到殘量門檻以上（例如 5000 對 accountValue 10000），並加變異防護：同一測試再斷言把 `liquidationPx` 改成合法值時該部位會被算進去（證明 fixture 確實通過殘量過濾）。

驗收：`uv run pytest tests/test_hl_explore_risk.py tests/test_api_me_leader.py tests/test_set_follower_leader.py -q` 全綠並列新測試名；`uv run pytest -q` 全綠；`uv run ruff check src tests scripts`；實跑 `risk_from_clearinghouse` 對 `"NaN"`／`"Infinity"` accountValue 印出 `maint_ratio=None`。

### Task 11 `@inline`：前端（W3、S1）

**Files:** Create `web/src/lib/liqDistance.ts`（`liqDistanceDisplay(pct: number | null): { text: string; className: string }`，門檻常數 `LIQ_DANGER_PCT = 15`、`LIQ_WARN_PCT = 30` 只在此檔）＋ `web/src/lib/liqDistance.test.ts`；Modify `web/src/app/explore/page.tsx`、`web/src/app/traders/[address]/page.tsx`（刪各自拷貝、改 import 共用）、`web/src/lib/copy.ts`（`settings.leader.noneTitles.engine_no_leader` ZH「引擎目前沒有跟隨任何 leader」／EN "The engine is not following any leader right now"）、`web/src/app/settings/page.test.tsx`（`engine_no_leader` 顯示專屬標題、**不**出現「狀態碼」逃生行）。

驗收：`cd web && npm test -- --run` 全綠；`rg -n "LIQ_DANGER_PCT|LIQ_WARN_PCT|liqDistanceDisplay" web/src` 顯示定義只在 `lib/liqDistance.ts`、兩頁只 import；`npm run build` 通過。

不採納 S2（殘量全被濾掉 vs 無部位的 `—` 同形）：純顯示層細節，留待有回饋再做。

## 第三輪審查（2026-09-30）：「可部署」

第二輪七項全部落地（reviewer 逐項做變異驗證）。唯一 Warning——心跳 `leader` 整個非 dict 時 `me_leader` 與 CLI 會 AttributeError——由主線程直接修：兩處加 `isinstance(…, dict)` 守門（app 視同格式異常退回名冊；CLI 視同取不到位址 exit 4），各補一個測試並做過變異驗證（拿掉守門 2 failed、還原 2 passed）。
未採納的 Suggestion：CLI 壞位址與撤銷共用同一句訊息（攔阻正確、只是措辭）、`unreadable` 心跳缺獨立測試（與 stale 同一條件）、explore 每列呼叫 `liqDistanceDisplay` 兩次（無行為差異）。
觀察備查：心跳 `leader` 鍵整個缺席會被歸為 `engine_no_leader`；目前無真實觸發源（心跳自誕生即含此鍵）。

## 部署（主線程親跑）

1. **前提**：工作樹乾淨（另一條線的 web/* 未 commit 改動要先由其 owner commit 或 stash；否則整包 rsync 會推上去）。`git describe --dirty` 不得帶 `-dirty`。
2. 照 RUNBOOK §5.8c 在本機用新程式預建 300 池快照、`install` 到 `/var/lib/filet-api/explore_index.json`。
3. RUNBOOK §3.2 兩段 rsync ＋ chown（`find -path var -prune` 版本，**不用** `chown -R`）；§4.2 前端 build；§9.2 重啟 `filet-api`、`filet-dashboard`。**不重啟 follower**（本 plan 不改引擎）。
4. 寫 `DEPLOYED_VERSION`。
5. 執行 Task 1-op（正式機更新 f438 名冊，先 dry-run）。
6. 驗收：`/api/public/explore` 任一列有 `risk` 鍵；`/api/public/traders/0xedea…` 的 `risk.liq_distance_pct` 與交易所 `liquidationPx` 手算一致；f438 儀表板顯示 0xedea 且無 pending；RUNBOOK §8 驗收 4 的四層回歸。

## 不在範圍

- 引擎行為、名冊寫入邊界、leader_changes.json 回收邏輯：全部不動。
- 對 leader 風險做任何自動動作（撤銷、通知客戶）：不做，只顯示。
- ops 面板：已讀心跳，不動。
