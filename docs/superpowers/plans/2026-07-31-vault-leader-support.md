# Vault leader 支援 — 施工計畫（2026-07-31）

Spec（設計與裁決）：`~/projects/obsidian/pandora/filet/2026-07-31-vault-leader-support-spec.md`。
本檔是 TDD 任務書：每個 wave 先寫測試（紅）→ 實作（綠）→ 不動既有測試。
分支：feat/m2-frontend。全離線（conftest socket-ban）。內部一律 Decimal。

## Wave 1 — `LeaderRef.kind`（機械）

檔案：`src/spark/filet/leaders.py`、`tests/test_filet_leaders.py`（若無則找既有 leaders 測試檔）。

1. `LeaderRef` 尾端新增 `kind: str = "standard"`（frozen dataclass，預設值保住既有建構點）。
2. `load_leaders()` 條目迴圈：`kind = entry.get("kind", "standard")`；
   非 str 或不在 `{"standard", "vault"}` → `ValueError`（訊息含檔名與位址，照既有 fail-fast 風格）。
3. 私有 `_lookup` 轉正為 `find_leader(address, leaders) -> LeaderRef | None`（保留舊名 alias 或改呼叫點）。

測試（先寫）：
- kind 缺欄位 → `standard`；`"vault"` → 解析成功；`"yolo"` → ValueError；`kind: 1` → ValueError。
- registry 路徑：`load_user_leaders` 對含 `kind: "vault"` 的條目同語意（重用驗證）。
- `find_leader` 命中／未命中。
- 既有測試零修改通過。

## Wave 2 — 雙層執行（watcher 注入＋引擎自衛）

### 2a 常數與引擎層

檔案：`src/spark/copytrade/vault_policy.py`（新）、`scripts/run_copytrade.py`、
`src/spark/filet/leader_resolve.py`、`tests/test_vault_policy.py`（新）。

1. `vault_policy.py`：`VAULT_MAX_TARGET_LEVERAGE = Decimal("20")`、`KIND_VAULT = "vault"`、
   `apply_vault_policy(settings: CopySettings, kind: str) -> CopySettings`：
   - `kind != "vault"` → 原樣回傳（同一物件）。
   - vault → `dataclasses.replace`：`max_target_leverage = 20 if 現值 == 0 else min(現值, 20)`；
     `leader_flow_neutralization_enabled = True`。
2. `LeaderResolution` 新增 `kind: str = "standard"`；`resolve_leader()` 回傳前
   `ref_hit = find_leader(candidate, leaders)`，`kind = ref_hit.kind if ref_hit else "standard"`。
   注意 `allowlist_absent` 的 env 回退路徑 → kind 維持 "standard"。
3. `run_copytrade.py` 兩處接線（同一形狀）：啟動時 `:580` 附近與每輪 `:663` 附近的
   `replace(copy_settings, leader_address=…)` 之後套 `apply_vault_policy(cs, res.kind)`。
   兩處都套（工程原則 1：本輪判定與本輪解析同源）。

依賴：Wave 4 的 `leader_flow_neutralization_enabled` 欄位——**本 wave 先在
`CopySettings` 加兩個欄位**（見 Wave 4 規格），欄位先行、引擎邏輯後行，避免交叉依賴。

測試（先寫）：
- `apply_vault_policy`：(0→20)、(25→20)、(10→10)、standard 不動、
  vault 時 `leader_flow_neutralization_enabled` 強制 True。
- `resolve_leader` 回傳 kind：白名單 vault 條目 → "vault"；standard → "standard"；
  env 回退＋白名單缺檔 → "standard"。

### 2b watcher 注入

檔案：`scripts/filet_auto_activate.py`、`tests/test_filet_auto_activate.py`。

1. `run_once`：載入 `load_leaders(leaders_path)` ＋ `load_user_leaders(user_leaders_path)`
   → `merge_leaders` 一次，傳入 `process_entry`（新參數 `leaders: list[LeaderRef]`）。
   載入失敗 → 照既有「範本先驗」同級處理（critical ＋上拋，該輪不做半套）。
2. `process_entry`：拿到 leader 位址後 `ref = find_leader(leader, leaders)`；
   `is_vault = ref is not None and ref.kind == "vault"`，傳入 `_compose_env(…, vault_leader=is_vault)`。
3. `_compose_env` 代入區塊：`vault_leader=True` 時多寫
   `COPY_MAX_TARGET_LEVERAGE=20`（值 import 自 `vault_policy.VAULT_MAX_TARGET_LEVERAGE`，
   f-string 用 `str()`）與 `COPY_LEADER_FLOW_NEUTRALIZATION=true`。
4. `GENERATED_KEYS` 加兩鍵（範本含之即 fail-closed，維持既有語意）。

測試（先寫）：
- vault leader 條目 → env 檔含兩鍵正確值（用 `_env_kv` helper）。
- standard leader → env 檔**不含**兩鍵。
- 範本含 `COPY_MAX_TARGET_LEVERAGE` → 該輪 SystemExit（重複定義 fail-closed 既有測試模式）。
- `test_generated_env_has_every_engine_required_var` 的 expected dict 補兩鍵
  （僅 vault 情境；確認該測試的 leaders fixture 要不要分兩例——照既有測試結構決定）。

## Wave 3 — adapter 讀方法（機械）

檔案：`src/spark/exchange/base.py`、`hyperliquid.py`、`fakes.py`、
`tests/test_exchange_read_types.py`。

1. `base.py`：`@dataclass(frozen=True) class LedgerFlow: time_ms: int; usdc: Decimal`
   （有號：入 +、出 −）＋抽象方法
   `get_ledger_flows(self, address: str, start_ms: int) -> tuple[list[LedgerFlow], list[str]]`
   ——第二回傳值＝白名單外的 delta type 清單（去重），呼叫端決定告警。
2. `hyperliquid.py`：`self._info.post("/info", {"type": "userNonFundingLedgerUpdates",
   "user": address, "startTime": start_ms})`；映射
   `vaultDeposit → +usdc`、`deposit → +usdc`、`withdraw → −usdc`、
   `vaultWithdraw → −netWithdrawnUsd`；全部 `Decimal(str(…))`；未知型別收進第二回傳值。
3. `fakes.py`：建構子加 `ledger_flows=None` 注入槽（`(flows, unknown_types)` tuple 或預設 `([], [])`）。

測試（先寫）：frozen＋Decimal 檢查、FakeAdapter 回注入值、
hyperliquid 映射用假 info stub 驗四型別＋未知型別分流（照該檔既有 stub 模式）。

## Wave 4 — T2 中性化（核心，非機械）

檔案：`src/spark/copytrade/leader_flow.py`（新）、`config.py`、`loop.py`、
`tests/test_copy_leader_flow.py`（新）、`tests/test_copy_loop.py`。

1. `config.py` `CopySettings` 尾端：`leader_flow_neutralization_enabled: bool = False`
   （env `COPY_LEADER_FLOW_NEUTRALIZATION`，走既有 `_env_bool`）、
   `flow_decay_hours: Decimal = Decimal("36")`（env `COPY_FLOW_DECAY_HOURS`，
   `__post_init__` 驗 > 0）。（若 Wave 2 已加欄位，本步只補 from_env 與驗證。）
2. `leader_flow.py` 純函式：
   `adjusted_leader_equity(raw: Decimal, flows: list[LedgerFlow], now_ms: int,
   decay_ms: int) -> Decimal`——`adjustment = Σ usdc × max(0, 1 − age/decay)`（age<0 視為 0），
   `adjusted = raw − adjustment`；**不做**任何 IO；guard 不在此層（呼叫端做）。
3. `loop.py` 接線（`leader_state = adapter.get_account_state(leader)` 之後）：
   ```
   若 settings.leader_flow_neutralization_enabled:
       try: flows, unknown = adapter.get_ledger_flows(leader, now−decay)
       except Exception → warn（一輪一次，訊息含例外）→ 用 raw
       unknown 非空 → warn（含型別清單）
       adj = adjusted_leader_equity(raw, flows, now_ms, decay_ms)
       adj ≤ 0 < raw → notifier.critical（幻影歸零防護）→ 用 raw
       否則 leader_equity ← adj
   ```
   `now_ms` 來源與 loop 既有時間源一致（看 `main_loop` 的 clock 注入慣例，不得混 time.time 與注入 clock——同源）。
   adjusted 只餵 `compute_scale_factor` 的 `leader_equity` 參數（scale 與 eff_lev 自動同源）。
4. `sizing.py` **不動**。

測試（先寫）：
- 錨例逐條（spec §3.3 表格五例，數字硬編）。
- age ≥ decay → 零調整；age < 0（時鐘漂移）→ 視同 0（全額中性化）。
- loop 接線：FakeAdapter 注入 `account`（leader equity）＋ `ledger_flows`，
  斷言 orders/positions 收到的 scale 反映 adjusted 分母（沿 test_copy_loop 既有斷言方式）。
- 失敗路徑：fake 拋例外 → scale 用 raw ＋ notifier 收到 warn；
  adjusted ≤ 0 案例 → 用 raw ＋ critical；未知型別 → warn 含型別名。
- 預設關閉：`leader_flow_neutralization_enabled=False` 時 FakeAdapter 的
  `get_ledger_flows` **不被呼叫**（零行為變動證明）。

## Wave 5 — preflight 腳本

檔案：`src/spark/publicapi/hl.py`、`scripts/vault_preflight.py`（新）、
`tests/test_vault_preflight.py`（新）。

1. `hl.py` `HLGateway` 加 `vault_details(vault_address)`、
   `non_funding_ledger_updates(user, start_ms)`（照既有 `_info(body, what)` ＋
   `run(idempotent=True)` 模式）。
2. 腳本：檢查邏輯拆成可測純函式 `run_checks(data: PreflightData) -> list[CheckResult]`
   （六檢查見 spec §3.4，閾值硬編＋常數註明來源），main() 只負責抓資料＋印表＋exit code。
   位址為位置參數；`--window-days` 預設 30。輸出每檢查一行 PASS/FAIL＋數字。
3. 參考 `scripts/watchlist_snapshot.py` 的 gateway 用法與 env 慣例。

測試（先寫）：六檢查各一組 PASS fixture＋一組 FAIL fixture（fixture 數字取自 spec §1
Ultron 實測值，離線 JSON；恆等式 FAIL 例：把 pnl 改差 $100）。

## Wave 6 — 文件

1. `deploy/RUNBOOK.md` §5.5 內（`enabled/accepting_new` 抉擇之後）插
   `#### vault leader 上架前置檢查`：spec §5 四步驟＋§5.6a-1 式升級註記
   （既有機器範本不得含兩新鍵）＋§5.5.3 交叉註記（自訂路徑無 vault 偵測，vault 僅精選上架）。
2. `deploy/leaders.json.example`：`_comment` 說明 `kind`＋一個 vault 示例條目（`enabled: false` 示範）。
3. `deploy/follower.env.autoactivate.example` 禁鍵註解（:15-22 區塊）補兩鍵。
4. `docs/superpowers/plans/2026-07-22-open-items.md` 補兩筆（spec §7）。

## 驗收（主對話親跑）

`uv run pytest`（全綠）、`uv run ruff check src tests scripts`（乾淨）、
`grep -c` 抽查關鍵落點、fresh-context agent 對照 spec §4 逐條驗收。
