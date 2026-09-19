# Hyperliquid 推薦碼 opt-in（客戶簽章 → 引擎代設 setReferrer）Implementation Plan

> **For agentic workers:** 逐 task 派工（`@inline` → builder）。每個 task 的驗收指令由主線程親跑確認後才派下一個。Steps 用 `- [ ]` 追蹤。

**Goal:** 讓跟單用戶可**選擇性**簽署一份授權，之後由該用戶的跟單引擎用已授權的 agent key 對 Hyperliquid 送 `setReferrer`，把用戶的推薦人設成 Filet 的推薦碼。不簽不影響跟單；既有正在跑的 follower 一律不動。

**Architecture:** 完全複製 `risk_settings` 那條「API 發原文 → 錢包 personal_sign → API 驗章落記錄 → 引擎每輪自己重新驗章後套用」的管線（新模組是它的姊妹檔，同一個 `recover_personal_sign_address`、同一個 nonce 帳本、同一個 manifest 信任錨）。引擎在**每輪 `run_cycle` 之前**做一次冪等的「查鏈上 referredBy → 未設才送 setReferrer → 重查驗證」，做完就在記憶體記 done。推薦碼**單一來源** `/etc/filet/referral.env`（`FILET_REFERRAL_CODE`），filet-api 與 watcher 兩個 unit 都用 `EnvironmentFile` 載入：API 拿它發原文，watcher 啟用新用戶時把它寫成該引擎 env 的 `COPY_REFERRAL_CODE`（納入 `GENERATED_KEYS`，範本不得自帶）。引擎以記錄裡客戶簽的 code 與自己 env 的 code 相等為套用前提，不等就 critical 而不是靜默（防被打穿的 API 換碼）。

**Tech Stack:** Python 3.11 + hyperliquid-python-sdk（`Exchange.set_referrer`、`Info.query_referral_state`）、FastAPI、Next.js + wagmi personal_sign、vitest。

---

## 0. 已確認的事實（研究與 testnet 實測，2026-09-18/19）

規則（官方 docs `hyperliquid.gitbook.io/hyperliquid-docs/referrals`、`/trading/fees`）：
- 推薦人收被推薦人手續費的 10%（扣掉對方折扣後），上限：被推薦人前 $1B 成交量。
- 被推薦人手續費 4% off，上限前 $25M 成交量。建碼門檻：推薦人錢包累積 $10,000 成交。
- 推薦獎勵與 builder fee 走同一個領取流程（>$1 可領到 spot）。

SDK（`.venv/lib/python3.14/site-packages/hyperliquid/exchange.py:434-452`）：
- `Exchange.set_referrer(code)` 走 `sign_l1_action` → **agent wallet 可簽**（與下單同一種簽法）。
- `Info.query_referral_state(user)` 回 `{"referredBy": {"referrer", "code"} | null, "cumVlm", ...}`。

testnet 實測（scratchpad `referral_probe.py` / `referral_probe2.py`，水龍頭錢包 0x4229…9CE2）：

| 情境 | 回應 |
|---|---|
| agent 簽、碼不存在 | `{'status': 'err', 'response': 'Referral code not registered'}`（主錢包簽得到**同一句**） |
| agent 簽、碼存在（`HYPERLIQUID`） | `{'status': 'ok', 'response': {'type': 'default'}}`，重查 `referredBy.code == "HYPERLIQUID"` |
| 已設過再設 | `{'status': 'err', 'response': 'Referrer already set'}` |
| 全新錢包先成交 $29.74 再設 | **成功**（推翻第三方指南「必須在第一筆交易前」的說法） |

主網盤點（2026-09-19，唯讀）：正式機 manifest 兩位 follower 都已有推薦人（`RABBYWALLET`、`JIMLAI1005`），pending 為空。⇒ 既有用戶碰不到本功能；只對新啟用且尚無推薦人的錢包有效。

使用者裁決（2026-09-19）：(1) 推薦人錢包與推薦碼由使用者提供，準備好交付；(2) 做成**可選簽署**，不簽不影響跟單；(3) 唯讀盤點已做；(4) testnet 驗證已做；(5) 放置點改到**引擎**（watcher 無 agent 私鑰）；(6) 推薦碼**走 watcher 寫進 follower env**，動 watcher 程式碼，換取單一設定來源。

系統結構名詞（follower／leader／watcher／交換目錄）見 `docs/architecture.md`。

**未驗證、殘餘風險**：設碼是否有成交量上限（testnet 只驗到 $29.74）——官方無此規則，實作上以 `Referrer already set` 以外的 err 一律 critical 留痕處理。

---

## 1. 檔案地圖

| 檔案 | 責任 |
|---|---|
| Create `src/spark/filet/referral_optin.py` | 記錄格式、待簽原文、驗章、路徑、讀寫（risk_settings 的姊妹檔） |
| Create `src/spark/filet/referral_apply.py` | 引擎端 `ReferralOptinApplier`：每輪冪等套用，絕不 raise |
| Modify `src/spark/exchange/base.py`、`hyperliquid.py`、`fakes.py` | 新增 `set_referrer(code)`、`query_referred_by(user)` |
| Modify `src/spark/copytrade/config.py` | `CopySettings.referral_code`（env `COPY_REFERRAL_CODE`，可缺） |
| Modify `scripts/run_copytrade.py` | `make_referral_applier`＋在 `cycle()` 的 `run_cycle` 之前呼叫 |
| Modify `src/spark/publicapi/config.py`、`app.py` | `ApiConfig.referral_code`（env `FILET_REFERRAL_CODE`）、三個端點 |
| Modify `web/src/lib/api.ts`、Create `web/src/lib/referralFlow.ts` | 型別、fetch、簽章編排（沿 riskSettingsFlow） |
| Modify `web/src/components/wizard/StepConfirm.tsx`、`web/src/app/settings/page.tsx`、`web/src/lib/copy.ts`、`web/src/content/legal.ts` | 可選簽署卡片＋文案＋法務揭露 |
| Modify `scripts/filet_auto_activate.py` | `COPY_REFERRAL_CODE` 納入 `GENERATED_KEYS`；`_compose_env` 依 watcher 自己的 `FILET_REFERRAL_CODE` 條件式代入 |
| Modify `deploy/filet-api.service`、`deploy/filet-auto-activate.service`、`deploy/RUNBOOK.md`、`deploy/follower.env*.example` | `EnvironmentFile=-/etc/filet/referral.env` 與部署說明 |
| Tests | `tests/test_referral_optin.py`、`tests/test_referral_apply.py`、`tests/test_api_referral.py`、`tests/test_hyperliquid_adapter.py`（追加）、`tests/test_filet_auto_activate.py`（追加）、`web/src/lib/referralFlow.test.ts` |

不動：既有 follower 的 env（watcher 只在啟用新用戶時寫 env）。

---

## 2. 共用規格（每個 task 都要遵守）

- 動作常數 `ACTION_REFERRAL_OPTIN = "referral_optin"`。記錄欄位（順序固定）：
  `REFERRAL_OPTIN_FIELDS = ("action", "account_id", "code", "nonce", "issued_at", "signature", "message")`。**沒有 signer 欄位**（同 risk_settings）。
- 推薦碼合法格式：`^[A-Z0-9_]{1,32}$`（HL 碼為大寫英數；API 設定值先 `.strip().upper()`；不合法 → API 拒絕啟動時**不**拒絕，只在端點回 503「推薦功能未設定」；引擎端記錄裡的 code 不合法 → `malformed`）。
- 待簽原文（**唯一版型**，寫端與讀端都用 `build_referral_optin_message`）：

```
Filet: set Hyperliquid referral code

Signing this authorises Filet to set the referral code below on your
Hyperliquid account, using the trading agent you already approved.
Hyperliquid gives referred accounts a 4% fee discount on their first $25M
of trading volume, and pays Filet a share of the trading fees you pay.
A referral code can be set only once per account and cannot be changed
later. If your account already has a referral code, nothing changes.
This is optional: copy-trading works the same whether or not you sign.
No positions are opened or closed by this action.

Account: {account_id}
Referral Code: {code}
Nonce: {nonce}
Issued At: {issued_at}
```

  第一行 `"Filet: set Hyperliquid referral code"` 是域分隔符，與現有四個字面量（換 leader／資金／風控／解鎖）兩兩不等，且沒有任何呼叫端輸入能到達第一行。
- 記錄檔：`{exchange_dir}/referral_optin.json`，頂層 `{"optins": [...]}`，同 account 覆蓋，mode 0644（`write_json_atomic`）。
- 時效：API 端強制 `REFERRAL_OPTIN_MAX_AGE_S = LEADER_CHANGE_MAX_AGE_S`；引擎端 `max_age_s=None`（持續意圖，沿 `_risk_lines` 的放行理由）。
- 紅線 2：任何 log／告警不得帶 signature／message 原文；只帶 account_id、reason、code。
- 引擎失敗分類（工程原則 2）：網路例外＝transient → 本輪放棄、下一輪再試（warn，dedup）；HL 回 `status: err` ＝ semantic → 不重試；其中 `Referrer already set` 視同「鏈上已設」重查一次即可，其餘 critical。
- 告警一律 `try/except` 包住（沿 `RiskSettingsApplier._critical`），告警失敗不得中斷跟單。

---

### Task 1 `@inline`：`referral_optin.py` 記錄模組 + 單元測試

**Files:**
- Create: `src/spark/filet/referral_optin.py`
- Create: `tests/test_referral_optin.py`
- 範本：`src/spark/filet/risk_settings.py`（整檔照抄結構）、`tests/test_risk_settings.py:60-130`（wallet fixture、記錄產生器）

公開介面（簽名固定，Task 3/4 依賴）：

```python
ACTION_REFERRAL_OPTIN = "referral_optin"
REFERRAL_OPTIN_FIELDS = ("action", "account_id", "code", "nonce", "issued_at", "signature", "message")
REFERRAL_OPTIN_MAX_AGE_S = LEADER_CHANGE_MAX_AGE_S
REFERRAL_CODE_RE = re.compile(r"^[A-Z0-9_]{1,32}$")

class ReferralOptinError(ValueError):   # .reason ∈ malformed/action_mismatch/account_mismatch/expired/bad_signature/signer_mismatch/nonce_unusable
@dataclass(frozen=True)
class VerifiedReferralOptin: account_id: str; user_address: str; code: str; nonce: str; issued_at: str; issued_at_s: float

def normalize_referral_code(code: object) -> str          # strip+upper；不合法 raise ReferralOptinError("malformed")
def build_referral_optin_message(*, account_id, code, nonce, issued_at) -> str
def build_referral_optin_record(*, account_id, code, nonce, issued_at, signature, message) -> dict   # action 寫死
def verify_referral_optin(record, *, account_id, user_address, now_s, consume_nonce, max_age_s=None) -> VerifiedReferralOptin
def referral_optin_path_for(exchange_dir) -> str
def load_referral_optins(path) -> list[dict]
def write_referral_optin(path, record) -> None
```

- [ ] **Step 1**：先寫 `tests/test_referral_optin.py`（照 `tests/test_risk_settings.py` 的 fixture 形狀）至少涵蓋：合法記錄驗過並回 code；`action` 改成 `risk_settings` → `action_mismatch`；account 不符 → `account_mismatch`；code 小寫進來被 normalize 成大寫且原文含 `Referral Code: XXX`；code 含 `-` 或 33 字 → `malformed`；簽章者不是 user_address → `signer_mismatch`；API 模式 `max_age_s=600` 過期 → `expired`；引擎模式 `max_age_s=None` 舊記錄照過；`consume_nonce` 回 False → `nonce_unusable` 且 nonce 是**最後**才消耗（前面失敗時 consume 不被呼叫）；`write_referral_optin` 同 account 覆蓋、檔案 mode 0644；記錄鍵集 == `REFERRAL_OPTIN_FIELDS`。
- [ ] **Step 2**：跑 `uv run pytest tests/test_referral_optin.py -q` 確認 import 失敗（紅）。
- [ ] **Step 3**：實作模組。檔頭 docstring 說明：為何是姊妹檔、域分隔第五個字面量、為何 code 進原文、時效兩側語意。
- [ ] **Step 4**：`uv run pytest tests/test_referral_optin.py tests/test_risk_settings.py -q` 全綠；`uv run ruff check src tests`。
- [ ] **Step 5**：commit `feat: referral opt-in 簽章記錄模組（risk_settings 姊妹檔）`。

**驗收指令**（主線程親跑）：`uv run pytest tests/test_referral_optin.py -q` 全綠且 ≥ 10 個測試；`uv run ruff check src tests` 無輸出。

---

### Task 2 `@inline`：adapter 新增 `set_referrer` 與 `query_referred_by`

**Files:**
- Modify: `src/spark/exchange/base.py`（ABC 新增兩個抽象方法，docstring 註明「非託管不變量不受影響：不是 withdraw/transfer」）
- Modify: `src/spark/exchange/hyperliquid.py`（`query_builder_accrued` 附近加讀；`approve_agent` 附近加寫）
- Modify: `src/spark/exchange/fakes.py`（fake 記錄呼叫、可預設回值／注入錯誤）
- Modify: `tests/test_hyperliquid_adapter.py`（追加）
- Modify: `src/spark/resilience.py`（**2026-09-19 主線程裁決**，執行時發現 `tests/test_resilience_boundary.py` 的結構守門：所有 `self._exchange.<name>(` 都必須是 `ResilientExchange` 的顯式包裝方法或在唯讀白名單）：`set_referrer` 是交易所寫入，**不得走透傳白名單**，在 `ResilientExchange` 加顯式包裝，分類為**冪等**（送達但回應遺失時重送只會得到語意錯 `Referrer already set`，不會重複產生任何效果）：
  ```python
  def set_referrer(self, *a, **k):
      return run(lambda: self._ex.set_referrer(*a, **k),
                 what="設定推薦碼", idempotent=True, sleep_fn=self._sleep_fn)
  ```
  並在 `tests/test_resilience.py`（或該檔既有的 ResilientExchange 測試檔）照 `update_leverage` 的既有測試追加：transient 例外會重試、成功回傳原 dict。Task 3 的 applier 對「例外」的處理（下一輪再試）維持不變，作為邊界之外的第二層。

```python
# base.py
@abstractmethod
def query_referred_by(self, user: str) -> str | None: ...   # 鏈上 referredBy.code；null → None（唯讀）
@abstractmethod
def set_referrer(self, code: str) -> TxResult: ...          # L1 action，agent 可簽；ok 依 status=="ok"

# hyperliquid.py
def query_referred_by(self, user):
    state = self._info.query_referral_state(user)
    rb = state.get("referredBy")
    return str(rb["code"]) if isinstance(rb, dict) and rb.get("code") else None

def set_referrer(self, code):
    res = self._exchange.set_referrer(code)     # ResilientExchange.__getattr__ 直通 SDK；重試決策留給 applier
    return TxResult(ok=res.get("status") == "ok", raw=res)
```

- [ ] **Step 1**：測試先行——用既有 test_hyperliquid_adapter 的 stub Info/Exchange 形狀：`referredBy: null` → None；`{"referrer":..,"code":"X"}` → "X"；`set_referrer` 回 `{'status':'ok',...}` → `ok=True`；回 `{'status':'err','response':'Referrer already set'}` → `ok=False` 且 `raw` 保留原字串。
- [ ] **Step 2**：跑紅 → 實作 → `uv run pytest tests/test_hyperliquid_adapter.py tests/test_base_types.py tests/test_fake_adapter.py -q` 全綠（`test_adapter_is_abstract_and_has_no_withdraw` 必須仍綠）。
- [ ] **Step 3**：全量 `uv run pytest -q`（確認沒有其他 ExchangeAdapter 子類因新抽象方法而無法實例化；若有，補最小實作）。
- [ ] **Step 4**：commit `feat: adapter 新增 set_referrer／query_referred_by`。

**驗收指令**：`uv run pytest -q` 全綠；`grep -n "def set_referrer\|def query_referred_by" src/spark/exchange/*.py` 三個檔各兩個命中。

---

### Task 3 `@inline`：引擎端 applier + `COPY_REFERRAL_CODE` + cycle 掛點

**Files:**
- Modify: `src/spark/copytrade/config.py`（`CopySettings.referral_code: str | None = None`，`from_env` 讀 `COPY_REFERRAL_CODE`，用既有 `_clean`；空字串視為 None）
- Create: `src/spark/filet/referral_apply.py`
- Modify: `scripts/run_copytrade.py`（`make_referral_applier` 放在 `make_risk_settings_applier` 之後；`cycle()` 內 `hb["settings"] = cs` 之後、`run_cycle` 之前呼叫）
- Create: `tests/test_referral_apply.py`
- 範本：`src/spark/filet/risk_settings_apply.py:144-240`（`_critical`、`_my_record`、`_trusted_user_address` 三個方法照抄）

```python
class ReferralOptinApplier:
    def __init__(self, *, account_id: str, manifest_path, optin_path, expected_code: str,
                 adapter: ExchangeAdapter, notifier: Notifier): ...
    @property
    def status(self) -> str: ...        # "pending" | "applied" | "already_referred" | "rejected"（心跳／--status 用；本 plan 不接心跳）
    def apply_once(self) -> None: ...   # 絕不 raise
```

`apply_once` 邏輯（依序，任何一步 return 就是本輪結束）：
1. `self._done` 為 True → return。
2. 讀本帳號記錄（`_my_record`，讀失敗只 log）；沒有 → return（客戶還沒簽，**不 log 不告警**，每輪一次檔案讀取可接受）。
3. 記錄的 `issued_at` 等於 `self._rejected_issued_at` → return（同一筆已判定壞掉，不重複告警）。
4. 可信 `user_address`（`_trusted_user_address`；拿不到 → critical dedup `referral_no_trusted_user` → return）。
5. `verify_referral_optin(rec, account_id, user_address, now_s=0.0, consume_nonce=lambda n: True, max_age_s=None)`；失敗 → critical（只帶 reason）、`_rejected_issued_at = rec["issued_at"]` → return。
6. `verified.code != expected_code` → critical「記錄的推薦碼與引擎設定的 COPY_REFERRAL_CODE 不符（兩處設定漂移）」、記 rejected → return。
7. `current = adapter.query_referred_by(user_address)`；例外 → warn dedup `referral_query_failed` → return（下一輪再試）。
8. `current is not None` → `_done=True`；`status = "already_referred"`；`notifier.info("referral", f"account={account_id} 鏈上已有推薦碼（{'Filet' if current == expected_code else '其他推薦人'}），不再嘗試")` → return。
9. `res = adapter.set_referrer(expected_code)`；例外 → warn dedup → return。
10. `not res.ok`：`raw.response` 含 `already set` → 回到第 7 步邏輯（重查一次，結果同第 8 步）；其他 → critical（帶 response 字串，它不是簽章材料）＋ `_done=True`、`status="rejected"` → return。
11. 重查 `query_referred_by`；等於 `expected_code` → `_done=True`、`status="applied"`、`notifier.info(...)`；否則 warn dedup `referral_verify_mismatch` → return（下一輪重跑第 7 步起）。

`make_referral_applier(*, account_id, settings: CopySettings, adapter, notifier, live: bool)`：`account_id is None` 或 `not live` 或 `settings.referral_code is None` → None（dry/shadow 與未設定一律不做）。`optin_path = referral_optin_path_for(require_exchange_dir())`。

- [ ] **Step 1**：`tests/test_referral_apply.py` 用 `fakes.py` 的 fake adapter＋記錄用 Task 1 的產生器，覆蓋上面 2、5、6、7（例外）、8（同碼／他碼）、9→11 成功、10（already set → already_referred；其他 err → rejected 且第二次 `apply_once` 不再打 adapter）、11 不符→下一輪重試。斷言：fake adapter 的 `set_referrer` 呼叫次數（成功後為 1、再呼叫 `apply_once` 仍為 1）。
- [ ] **Step 2**：跑紅 → 實作 → `uv run pytest tests/test_referral_apply.py tests/test_copytrade_config.py -q`（後者若不存在就跑 `tests/ -k config`）。
- [ ] **Step 3**：`run_copytrade.py` 掛點。既有 `tests/test_run_copytrade*.py`（若有）全綠；`--status` 路徑不受影響（applier 只在 live 建）。
- [ ] **Step 4**：`uv run pytest -q` 全綠；ruff 乾淨。
- [ ] **Step 5**：commit `feat: 引擎每輪冪等套用客戶簽章的推薦碼 opt-in`。

**驗收指令**：`uv run pytest tests/test_referral_apply.py -q` ≥ 9 個測試全綠；`uv run pytest -q` 全綠；`grep -n "referral_applier" scripts/run_copytrade.py` 至少 3 個命中（建構、cycle 呼叫）。

---

### Task 4 `@inline`：API 設定與三個端點 + 測試

**Files:**
- Modify: `src/spark/publicapi/config.py`：`referral_code: str | None = None`（`from_env` 讀 `FILET_REFERRAL_CODE`，經 `normalize_referral_code`；不合法 → 啟動時 `ValueError`，理由：設定錯了寧可起不來）；`referral_optin_path` **不加欄位**，由 `referral_optin_path_for(cfg.exchange_dir)` 推導（同 risk）。
- Modify: `src/spark/publicapi/app.py`：body models（`ReferralOptinBody` 欄位：`account_id, code, nonce, issued_at, signature, message`）與端點：
  - `GET /api/me/referral` → `{"enabled": bool, "code": str|None, "signed": bool, "signed_at": str|None, "onchain_code": str|None, "onchain_error": bool}`。`signed` 只看記錄檔存在該 account（**不驗章**，這是顯示用；驗章是引擎的事）。`onchain_code` 用 API 既有的 Info 客戶端（找 `create_app` 裡現有的 info／builder 餘額檢查所用物件；若無可注入點，加一個 `referral_lookup: Callable[[str], str | None] | None` 建構參數，測試注入 stub）；查失敗 → `onchain_error: true`、不 5xx。
  - `POST /api/me/referral/message` → `enabled` False 時 503；否則發 nonce（`store.issue_nonce`，同 risk）回 `{"message","nonce","issued_at","account_id","code"}`。
  - `POST /api/me/referral` → 403（account 不符）、400（驗章失敗，`detail` 用機器碼對照表，同 `RISK_SETTINGS_DETAIL` 形狀）、500（落檔失敗）；成功 `{"ok": true, "account_id", "code", "effective": "next_engine_cycle"}`。驗章用 `verify_referral_optin(..., max_age_s=REFERRAL_OPTIN_MAX_AGE_S, consume_nonce=同 risk 的 _consume 閉包)`。落地欄位一律取自 verified。
- Create: `tests/test_api_referral.py`（fixture 照 `tests/test_api_risk_settings.py:40-95`）

- [ ] **Step 1**：測試：未設定 → message 端點 503、GET `enabled:false`；設定後 message 回 canonical 大寫 code 且原文含 `Referral Code:`；POST 成功落檔並 GET `signed:true`；nonce 重放 → 400；signer 不符 → 400 且**不**落檔；account 不符 → 403；`onchain_code` 走注入 stub。
- [ ] **Step 2**：跑紅 → 實作 → `uv run pytest tests/test_api_referral.py tests/test_api_risk_settings.py -q`。
- [ ] **Step 3**：全量 `uv run pytest -q`；ruff。
- [ ] **Step 4**：commit `feat: /api/me/referral 三端點（客戶簽章 opt-in）`。

**驗收指令**：`uv run pytest tests/test_api_referral.py -q` ≥ 8 個全綠；`uv run pytest -q` 全綠。

**Task 4b（2026-09-19 主線程裁決，補正式接線）**：Task 4 執行時 `create_app` 以 `referral_lookup` 注入實作，但正式入口 `scripts/run_api.py` 沒接，正式機 `onchain_code` 恆 null。修法：
- `src/spark/publicapi/hl.py` 的 `HLGateway` 新增 `referred_by(self, address: str) -> str | None`：`self._info({"type": "referral", "user": address}, "HL referral 查詢")` 取 `referredBy.code`，null → None（形狀對齊同檔 `max_builder_fee` 等唯讀方法）。
- `scripts/run_api.py`：`create_app(..., referral_lookup=gateway.referred_by)`（先把 `HLGateway(cfg.api_url)` 存成變數）。
- 測試：`tests/` 既有 HLGateway 測試檔追加 `referred_by` 兩例（有碼／null）。
- 驗收：`uv run pytest -q` 全綠；`grep -n "referral_lookup" scripts/run_api.py` 命中 1。

---

### Task 5 `@inline`：前端（api.ts、referralFlow.ts、StepConfirm 可選卡片、settings 卡片、文案、法務）

**Files:**
- Modify: `web/src/lib/api.ts`（`ReferralStatusResp`、`ReferralMessageResp`、`ReferralOptinResp`；`getReferralStatus()`、`getReferralMessage()`、`submitReferralOptin(payload, signature)`）
- Create: `web/src/lib/referralFlow.ts` + `web/src/lib/referralFlow.test.ts`
- Modify: `web/src/lib/riskSettingsFlow.ts`：把私有 `finish` 改為 `export async function signRecoverSubmit`（**只改名＋export，邏輯不動**；既有測試須全綠）
- Modify: `web/src/components/wizard/StepConfirm.tsx`、`web/src/app/settings/page.tsx`、`web/src/lib/copy.ts`（`referral.*` 群組，ZH/EN 對稱）、`web/src/content/legal.ts`（風險／條款頁各加一段：推薦碼揭露）

`runReferralOptinFlow(deps, { expectedSigner, expectedAccountId, expectedCode })` 預驗（進錢包前、零網路）：`payload.account_id === expectedAccountId`、`payload.code === expectedCode`、原文第一行 === `"Filet: set Hyperliquid referral code"`、原文含一行 `Referral Code: ${expectedCode}`、原文**不含**任何 `${riskParamName}:` 行（沿 unlock 的域分隔防線，參數名取後端 specs）。不符 → `content-mismatch`。之後走 `signRecoverSubmit`。

UI 行為：
- StepConfirm（費用確認頁）：`enabled` 為 true 且 `signed` 為 false 時顯示「推薦碼（選填）」區塊：說明 4% 折扣、Filet 分潤、只能設一次、已有推薦碼者不受影響；按鈕「簽署啟用」；不簽可直接完成 onboarding（原本的完成按鈕行為**不動**）。`enabled` 為 false → 整塊不渲染。
- settings 頁：一張「推薦碼」卡片：狀態三態顯示（未簽署→按鈕；已簽署待引擎套用；鏈上已設定 `onchain_code`，若不等於 Filet 的碼則顯示「你的帳戶已由其他推薦人推薦」）。`onchain_error` → 顯示「無法讀取鏈上狀態」但不擋按鈕。
- 錯誤文案沿 `wizard.errors.*` 既有鍵（walletRejected／signerMismatch／contentMismatch）。

- [ ] **Step 1**：`referralFlow.test.ts` 照 `riskSettingsFlow` 既有測試形狀：成功路徑；account 不符；code 不符；原文缺 `Referral Code:` 行；原文含風控參數行；錢包拒簽；recover 不符；submit 失敗不重試（submit 只被呼叫 1 次）。
- [ ] **Step 2**：`export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH" && cd web && npm test` 全綠（含既有 riskSettingsFlow 測試）。
- [ ] **Step 3**：`npm run build`（或專案既有的 typecheck 指令）通過；copy.ts ZH/EN 鍵集對稱（既有的對稱測試若存在須綠）。
- [ ] **Step 4**：commit `feat(web): 推薦碼可選簽署（onboarding 費用頁＋設定頁）`。

**驗收指令**：`cd web && npm test` 全綠；`grep -c "referral" web/src/lib/copy.ts` ≥ 10；`grep -n "signRecoverSubmit" web/src/lib/riskSettingsFlow.ts web/src/lib/referralFlow.ts` 兩檔都命中。

---

### Task 6 `@inline`：watcher 代入 `COPY_REFERRAL_CODE` + 部署文件

**Files:**
- Modify: `scripts/filet_auto_activate.py`
  - 新常數 `REFERRAL_ENV_KEYS = ("COPY_REFERRAL_CODE",)`，併入 `GENERATED_KEYS`（範本自帶即 fail-closed，沿 VAULT_ENV_KEYS 既有語意）。
  - `_compose_env(...)` 新增關鍵字參數 `referral_code: str | None = None`；非 None 時在 vault 區塊之後追加：
    ```
    # 推薦碼 opt-in（客戶簽章後由引擎代設 setReferrer；值來自 /etc/filet/referral.env）
    COPY_REFERRAL_CODE=<code>
    ```
    None 時**不寫任何行**（既有語意：缺鍵＝功能關閉）。
  - 呼叫端（約 611 行 `env_content = _compose_env(...)`）帶入 `referral_code=_referral_code_from_env()`；新函式讀 `os.environ.get("FILET_REFERRAL_CODE")`，經 `referral_optin.normalize_referral_code`，空→None，不合法→`SystemExit`（整輪 fail-closed，理由同 REPLACE_WITH：設定壞了寧可不啟用任何人）。
- Modify: `tests/test_filet_auto_activate.py`：(a) env 有 `FILET_REFERRAL_CODE=abc` → 產出的 env 含 `COPY_REFERRAL_CODE=ABC`；(b) 未設 → 不含該鍵；(c) 範本含 `COPY_REFERRAL_CODE=` 值行 → `SystemExit`；(d) `FILET_REFERRAL_CODE=bad-code` → `SystemExit`；(e) 既有的 `for key in GENERATED_KEYS` 測試自然涵蓋新鍵。
- Modify: `deploy/filet-api.service` 與 `deploy/filet-auto-activate.service`：各加一行 `EnvironmentFile=-/etc/filet/referral.env`（`-` 前綴＝檔案不存在不擋啟動），附註解說明這是推薦碼的單一來源。
- Modify: `deploy/follower.env.example`、`deploy/follower.env.autoactivate.example`：加註解 `# COPY_REFERRAL_CODE 由 watcher 代入（來源 /etc/filet/referral.env），範本不得設定`。
- Modify: `deploy/RUNBOOK.md`：新節「推薦碼 opt-in」：(a) 建 `/etc/filet/referral.env`（`FILET_REFERRAL_CODE=XXXX`，0644 即可，不是 secret）；(b) 兩個 unit 檔加 `EnvironmentFile`（照 §5.1a 備份還原慣例）→ `daemon-reload` → 重啟 `filet-api`（watcher 是 timer 拉起，下一次自然吃到）；(c) 只影響**之後**啟用的 follower，既有引擎不動；(d) 驗證：`systemctl show filet-api -p Environment` 看得到 `FILET_REFERRAL_CODE`；新啟用用戶的 `/etc/filet/followers/<id>.env` 含 `COPY_REFERRAL_CODE`；鏈上用 `curl /info referral` 查 `referredBy`（引擎 `logger.info` 不進 journal，見 memory）；(e) 推薦人錢包需先在主網有 $10k 成交並在 app 建碼——人工步驟。
- Modify: 本檔狀態區。

- [ ] **Step 1**：先加測試 (a)–(d) → `uv run pytest tests/test_filet_auto_activate.py -q` 紅。
- [ ] **Step 2**：實作 watcher 改動 → 該檔全綠；`uv run pytest -q` 全綠；ruff 乾淨。
- [ ] **Step 3**：改 unit 檔、example、RUNBOOK。
- [ ] **Step 4**：commit `feat: watcher 代入 COPY_REFERRAL_CODE（單一來源 /etc/filet/referral.env）＋部署步驟`。

**驗收指令**：`uv run pytest tests/test_filet_auto_activate.py -q` 全綠且比改前多 ≥ 4 個測試；`grep -c "referral.env" deploy/filet-api.service deploy/filet-auto-activate.service` 各 ≥ 1；`grep -n "REFERRAL" scripts/filet_auto_activate.py` ≥ 3 個命中。

---

### Task 7：審核（`reviewer`，opus）

輸入：`git diff main...HEAD`、本 plan、`uv run pytest -q` 與 `npm test` 輸出。特別盯：(1) 引擎 applier 的每條失敗路徑是否都有出口且不 raise；(2) 記錄與 log 是否洩漏簽章材料；(3) 域分隔第五個字面量是否與既有四個都不同；(4) `COPY_REFERRAL_CODE` 缺席時引擎行為與改動前逐位元組相同（既有 follower 不動）；(5) 前端預驗是否在進錢包之前。

---

## 3. 上線步驟（實作完成後，人工）

1. 使用者提供推薦人錢包與推薦碼；確認該碼在**主網**已註冊（`curl /info {"type":"referral","user":<推薦人>}` 的 `referrerState.stage == "ready"`）。
2. 部署程式碼（一般流程，不涉及探索快照）。
3. 建 `/etc/filet/referral.env`、兩個 unit 加 `EnvironmentFile` → `daemon-reload` → `systemctl restart filet-api`。
4. testnet 端到端一次：新錢包 onboarding → 費用頁簽署 → 引擎啟動後 `referredBy.code` 等於設定碼（可用 `tests/integration` harness 的 mint-wallet）。

## 4. 狀態

- 2026-09-19：plan 撰寫完成；使用者確認裁決 (5)(6) 後改為 watcher 代入版。推薦碼與推薦人錢包待使用者交付（不阻擋 Task 1–6 實作，只阻擋上線步驟 1）。
