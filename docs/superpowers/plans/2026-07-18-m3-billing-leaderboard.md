# M3 首批後端：Stripe 計費骨幹（測試模式）+ Leaderboard Snapshot Cron 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建「定價無關」的 Stripe 計費骨幹（全程測試模式、價格 ID 參數化，使用者拍板定價選項 A/B/C 後只填設定不改碼），並把 leaderboard snapshot cron 落地成 systemd timer（每日快照全站 top-N ＋ leader watchlist 逐錢包狀態，為 M3 leader 選人提前累積 ≥2 個月資料）。

**Architecture:** billing 全部 additive 掛進既有 `publicapi` app：`StripeGateway` 是對 Stripe 的唯一出口（單一 resilience boundary，工程原則 5；checkout 建立為**非冪等寫入→絕不盲重試**）；SQLite 新增 `billing` 表（CREATE IF NOT EXISTS、無敏感資料）；webhook 驗簽（`stripe.Webhook.construct_event`，本地 HMAC）必過才碰 DB。三個 Stripe env 全 optional——未設時 billing 端點回 501，onboarding 主流程完全不受影響。watchlist 快照為純函式（注入 `state_fn`）＋ CLI 落檔（原子寫、同日冪等覆寫），systemd timer 每日 00:10 UTC 連跑既有全站快照與新 watchlist 快照。

**Tech Stack:** Python 3.11 + uv、FastAPI（既有）、stripe python SDK（新依賴，僅測試模式 key）、sqlite3（stdlib）、hyperliquid /info 唯讀查詢（既有 `HLGateway`）、systemd timer、pytest（全離線，autouse socket-ban）。

**上游依據:**
- M3 規劃（vault）：三個定價選項尚待使用者拍板 → 本計畫價格 ID 參數化（`FILET_STRIPE_PRICE_ID`）；「leaderboard snapshot cron 建議提前開跑累積 ≥2 個月資料」→ Part B。
- **硬約束：律師條款背書是收費前置條件（M0 未結案）——全程 Stripe 測試模式，`sk_test_` 前綴結構性強制，正式收費是使用者的人工決策，本計畫不含任何真實收費開關。**
- 慣例先例：`docs/superpowers/plans/2026-07-17-m2-publicapi.md`（依賴注入、單一邊界、紅線/review gate 格式）。
- **修訂（2026-07-18）**：opus 對抗審 REVISE_THEN_GO 已套用——webhook 亂序狀態守衛（event.created 單調水位，必改 1）、stripe 三元組 `__post_init__` 結構性成組驗證（Finding 2）、payment_status 未檢與 reconcile 缺口誠實標註（Finding 3/4，「刻意不做」11/12）、失敗分類註解精準化（Finding 5）。

---

## 執行狀態（2026-07-18 完成）

**全 11 task + 2 追加修正實作 + fresh-context 雙審完成；`uv run pytest -q` = 744 passed, 2 deselected；ruff clean。** 分支 `feat/m3-billing`（自 `feat/m2-publicapi` 分出，13 commits），未 push、未動 main。opus 三輪把關：計畫級對抗審（REVISE_THEN_GO→修訂）＋完工總審（NEEDS_FIXES F1→修）＋各 ⭐ task 紅線審。

| Task | commit | 備註 |
|---|---|---|
| 0 依賴+基線 | `57f1c92` | stripe 12.5.1，SDK 表面探針過 |
| 1 billing 表 ⭐ | `2451a3f` | 無敏感欄位白名單斷言；單調守衛 |
| 2 config ⭐ | `7dcf8f2` | sk_test_ `__post_init__` 結構性強制+三元組同設同缺 |
| 3 billing.py | `b18b9ba` + fix `fa29060` | review 抓 metadata 注入縫→validate 挪進 store 單一邊界 |
| 4 webhook 端點 ⭐ | `0b05b65` | 唯一免 session；驗簽失敗不碰 DB |
| 5 checkout/status | `c08be79` | active 409；501 隔離有測試 |
| 6 run_api 接線 | `a3bc831` | 未設 stripe 時與 M2 完全相同 |
| 7 leaderboard 模組 | `53f9501` | clearinghouse_state 唯讀；逐地址失敗隔離 |
| 8 watchlist CLI | `cc8c707` | 落 `leaderboard/watchlist/` 不撞全站快照 |
| 9 systemd timer | `ffcd55f` | 每日 00:10 UTC，oneshot 兩快照 |
| 10 全量+順手項 | `2572ad0` | except 收窄；驗簽失敗補 log |
| opus F1 修 | `536bebe` | **同秒事件互蓋縫**：event.id 去重+created 嚴格比較+同秒平手只降不升（active 不得復活）；欄位級 ALTER TABLE migration |

**opus 總審結論**：測試模式下合併安全——無真收費路徑（sk_test 結構性）、無繞過驗簽的 billing 寫入（grep 全呼叫點證實）、entitlement 只查不動、billing 全 additive、leaderboard 唯讀原子。

**觀察項（非阻擋，記錄）**：O1 Stripe >5min 重試若沿用原簽章會被 300s tolerance 拒（未確證 Stripe 重試是否重簽，實務靠續重試自癒）；O2 快照 position_count 未濾零倉（原始快照非比較用途）；webhook 掉包後 DB 漂移無 reconcile（DB-only 語意，**移交正式收費前計畫**）；payment_status 未檢（test-mode 接受，同上移交）；canceled/none 同 rank 互蓋為純理論路徑（無呼叫端寫 "none"）。

**使用者側啟用步驟**（要試 billing 時）：Stripe 測試帳號建 product+price → 填 `FILET_STRIPE_SECRET_KEY`（sk_test_）/`FILET_STRIPE_WEBHOOK_SECRET`/`FILET_STRIPE_PRICE_ID` 三元組（同設）→ 定價 A/B/C 拍板後換 price id 即可，不改碼。

---

## 全域紅線（每個任務的實作者與 reviewer 都先讀）

1. ⭐ **絕不真 key**：`FILET_STRIPE_SECRET_KEY` 必須 `sk_test_` 前綴——`ApiConfig.__post_init__` 結構性驗證（任何建構路徑都擋，不只 `from_env`），非測試前綴直接拒啟動（raise）。測試斷言 `sk_live_` 被拒。任何 key 不進 log/repr/例外訊息。
2. ⭐ **webhook 必驗簽**：`POST /api/billing/webhook` 是全 app 唯一不走 session auth 的端點（Stripe 伺服器對伺服器回呼，無 cookie；授權由 Stripe-Signature HMAC 取代——secret 只有 Stripe 與本服務知道）。驗簽不過一律 400、**不碰 DB**。偽造 webhook = 免費開通，這是本計畫最高風險面。
3. **billing 失敗不影響 onboarding**：billing 端點與 onboarding 端點無共享路徑；billing 未設定（env 缺）→ billing 端點 501、其餘 app 行為與 M2 完全相同（隔離有測試背書）。M2 closed alpha 免費跑不受任何影響。
4. **Stripe 外呼失敗分類**（工程原則 2/5）：checkout session 建立是**非冪等寫入**→ 單次嘗試、絕不盲重試；transient（連線/限流）轉譯 `ConnectionError` → 既有 502 handler「請稍後重試」（人肉重試天然去重）；semantic（請求被拒/設定錯）→ `BillingError` → 502 專屬 handler，log error 大聲告警。分類活在 `StripeGateway` 一處。
5. **不動既有 M2 行為，全部 additive**：store 新表 CREATE IF NOT EXISTS（不動舊表）；`create_app` 新參數帶預設值 `billing=None`；`HLGateway` 只加唯讀方法；既有測試一個都不許改斷言。
6. **entitlement 只查不動**：`has_active_subscription` 只提供查詢；**不接任何自動停用跟單邏輯**（停用是政策決策，留使用者人工裁決）。
7. `billing` 表無敏感資料：只有 account_id、stripe customer/subscription id、status、updated_at——無金額、無卡號、無 email（結構性測試斷言欄位集合）。
8. 測試全離線：autouse socket-ban（tests/conftest.py）不修改；stripe SDK 一律 monkeypatch 或注入 fake；webhook 驗簽測試用真 HMAC（`construct_event` 本地運算，不觸網）。HL 一律注入 fake。
9. 不 push、不動 main；`~/projects/hl-copytrader` 唯讀不碰；**絕不 `git add` 工作樹裡未 commit 的 `docs/superpowers/plans/2026-07-17-m2-frontend.md`**（每次 commit 用顯式路徑，禁 `git add -A`/`git add .`）。

## 設計定案（上游未定處，本計畫拍板；審查時重點盯）

1. **Stripe SDK 介面採 legacy resource 形式**（`stripe.checkout.Session.create(api_key=..., **params)`，per-call api_key、無全域狀態）而非 `StripeClient`——資源介面自 v3 穩定至今，per-call key 避免模組級全域 `stripe.api_key` 汙染測試。Task 0 有 SDK 表面探針，若版本漂移（如錯誤類別搬家）由探針當場抓到再調整 import。
2. **三個 Stripe env 綁定成組**：`FILET_STRIPE_SECRET_KEY` / `FILET_STRIPE_WEBHOOK_SECRET` / `FILET_STRIPE_PRICE_ID` 三個一起設或都不設；只設部分 → 拒啟動（fail loud，防「webhook secret 忘了設→驗簽必失敗」的半開狀態）。驗證兩層：`from_env`（訊息點名缺的 env var）＋ `__post_init__`（opus Finding 2：結構性背書，任何建構路徑都擋）。
3. **success/cancel URL 從 `cfg.siwe_uri` 衍生**（`{siwe_uri}/billing?checkout=success|cancel`），不加新 env——siwe_uri 就是 dashboard origin，少兩個設定項；前端計畫接手該路由。
4. **subscription 事件對回 account 的雙保險**：checkout 建立時同時塞 `client_reference_id=account_id` 與 `subscription_data.metadata.account_id`——`customer.subscription.updated/deleted` 事件從 metadata 取 account_id，取不到再 fallback 查 DB `stripe_subscription_id`；都對不到 → log warning 忽略（外部手建訂閱，不 4xx 引發 Stripe 重送風暴）。**注意**：雙保險只解「歸屬哪個帳號」；狀態亂序另由 `event.created` 單調守衛擋（opus 必改 1——Stripe 不保證事件順序，`upsert_billing` 的 WHERE 水位條件讓較舊事件整筆 no-op）。
5. **stripe 訂閱狀態映射白名單**：`active|trialing → active`；`past_due|unpaid → past_due`；其餘（canceled/incomplete/incomplete_expired/paused/未知新值）一律 `canceled`——保守不給權益。
6. **watchlist 快照與既有全站快照互補、分目錄**：既有 `scripts/leaderboard_snapshot.py`（2026-07-17 已存在，全站 top-N，stats-data 端點）落 `<FILET_DATA_DIR>/leaderboard/<day>.json`；新 watchlist 快照落 `<FILET_DATA_DIR>/leaderboard/watchlist/<day>.json`——子目錄隔離，檔名不撞。
7. **新腳本命名 `scripts/watchlist_snapshot.py`**（指揮官 prompt 寫 `snapshot_leaderboard.py`，但 repo 已有 `leaderboard_snapshot.py`——兩名互為顛倒是可預見的誤用坑；改用語意精確的 `watchlist_snapshot.py`，沿既有「名詞_snapshot」慣例）。
8. **`FILET_DATA_DIR` 新 env（預設 `var/filet`）**：repo 目前無此 env；新腳本用它，既有 `leaderboard_snapshot.py` 的 `main()` 亦 additive 支援（未設時行為不變）——systemd 下兩支腳本共用 `/var/lib/filet-api`（filet-api user 既有可寫目錄，ProtectSystem=strict 相容）。
9. **watchlist 查詢欄位以 `clearinghouseState` 實際可得為準**：accountValue / totalMarginUsed / totalNtlPos / withdrawable / 逐倉 unrealizedPnl 總和 / 持倉數。日 PnL 由日序列差分近似（出入金會混入——快照存原始欄位，衍生計算留 M3 分析，檔內註記極限）。
10. **逐地址失敗隔離＋大聲上報**：單一 leader 查掛不弄丟整批——error 條目寫進快照＋`error_count`＋logger.error；`error_count > 0` 時 CLI exit 1（systemd unit 顯示 failed，快照檔仍已寫出）。
11. **timer 內全站快照 best-effort**：`ExecStart=-`（容忍失敗）——它用的是未進官方文件的 stats-data 端點，schema 可能無預告變動；watchlist 快照（官方 /info）嚴格判定成敗。
12. **checkout 冪等擋板**：DB status 已 `active` → 409。同帳號並發兩次 checkout 可能各建一個 session——測試模式無真錢，且 customer_id 重用（已有 customer 傳 `customer=` 參數）讓 Stripe 端可見重複；上線前的嚴格鎖留給定價拍板後的收費計畫。

## 檔案結構（本計畫鎖定）

```
src/spark/publicapi/
├── config.py                    # Task 2（Modify）：+ stripe 三欄位、__post_init__ sk_test_ 強制 ⭐
├── store.py                     # Task 1（Modify）：+ billing 表（additive）、BillingRecord
├── billing.py                   # Task 3（Create）：StripeGateway、驗簽、事件處理、entitlement 查詢
├── app.py                       # Task 4/5（Modify）：+ /api/billing/{checkout,status,webhook} ⭐
└── hl.py                        # Task 7（Modify）：+ clearinghouse_state()（唯讀）
src/spark/filet/leaderboard.py   # Task 7（Create）：watchlist 快照純函式 + 原子落檔
scripts/watchlist_snapshot.py    # Task 8（Create）：watchlist 快照 CLI
scripts/leaderboard_snapshot.py  # Task 8（Modify）：main() 支援 FILET_DATA_DIR（additive）
scripts/run_api.py               # Task 6（Modify）：billing gateway 接線（設定存在才建）
deploy/filet-api.service         # Task 6（Modify）：註解示範 stripe env（預設停用）
deploy/filet-leaderboard.service # Task 9（Create）：oneshot 連跑兩支快照
deploy/filet-leaderboard.timer   # Task 9（Create）：每日 00:10 UTC
tests/
├── test_publicapi_store.py      # Task 1（Modify）：billing 表 + 無敏感欄位結構性斷言 ⭐
├── test_publicapi_config.py     # Task 2（Modify）：sk_test_ 強制 + 成組驗證 ⭐
├── test_publicapi_billing.py    # Task 3（Create）：gateway 分類、映射、事件處理、entitlement
├── test_api_billing.py          # Task 4/5（Create）：端點 + 真 HMAC 驗簽 + 501 + 隔離 ⭐
├── publicapi_helpers.py         # Task 4（Modify）：make_app 加 billing 參數（additive）
├── test_filet_leaderboard.py    # Task 7（Create）
├── test_watchlist_snapshot.py   # Task 8（Create）
└── test_leaderboard_snapshot.py # Task 8（Modify）：FILET_DATA_DIR 支援
```

## 模型分工與 review gate

| Task | 主題 | 實作 | 驗收 | 加驗 |
|---|---|---|---|---|
| 0 | 分支+stripe 依賴+SDK 探針+基線 | haiku | sonnet read-back | — |
| 1 | store billing 表 ⭐ | sonnet | sonnet fresh | ⭐ 紅線 7（無敏感欄位結構性斷言）|
| 2 | config sk_test_ 強制 ⭐ | sonnet | sonnet fresh | ⭐ 紅線 1（__post_init__ 任何建構路徑都擋）|
| 3 | billing.py（gateway+事件處理）| sonnet | sonnet fresh | 紅線 4（非冪等不盲重試）|
| 4 | webhook 端點 ⭐ | sonnet | sonnet fresh | ⭐ 紅線 2（真 HMAC 正反例、驗簽失敗不碰 DB、auth 豁免論證）|
| 5 | checkout/status 端點+隔離 | sonnet | sonnet fresh | 紅線 3（billing 未設 501、onboarding 不受影響）|
| 6 | run_api 接線+deploy env | sonnet | sonnet read-back | 紅線 1（unit 檔預設不含真值）|
| 7 | HLGateway 擴充+leaderboard 純函式 | sonnet | sonnet fresh | 紅線 5（hl.py 既有方法行為不變）|
| 8 | watchlist CLI+既有腳本 env 化 | sonnet | sonnet fresh | 定案 10（失敗隔離+exit code）|
| 9 | systemd timer/service | haiku | sonnet read-back | 定案 11（ExecStart=- 語意）|
| 10 | 全量整合驗證 ⭐ | sonnet | **opus 總審** | ⭐ 紅線 1-9 整條走一遍 |

- 每任務：實作 → fresh-context 驗收 → commit（顯式路徑 add，紅線 9）。全部 commit 落 `feat/m3-billing`（自 `feat/m2-publicapi` 分出）。不 push、不動 main。

---

### Task 0: 分支、stripe 依賴、SDK 表面探針、基線

工作樹若有未 commit 的檔案（撰寫本計畫時 `2026-07-17-m2-frontend.md` 曾是 untracked，
其後已由前端計畫 session commit 為 `86cd77d`；本計畫檔自身在執行時也可能未 commit）：
**不需要 stash**（untracked 檔跨 checkout 自然保留），只要之後每個 commit 都用
顯式路徑 add、絕不 `git add -A`（紅線 9）。

- [ ] **Step 1: 開分支**

```bash
cd /Users/jim/projects/spark
git checkout -b feat/m3-billing feat/m2-publicapi
git branch --show-current   # 應輸出 feat/m3-billing
git status --short          # 只允許 ?? 開頭的 untracked 計畫檔；不得有已改動的 tracked 檔
```

- [ ] **Step 2: 基線**

```bash
uv run pytest -q
```
Expected: `662 passed, 2 deselected`（M2 publicapi 完成基線）。`uv run ruff check src tests scripts` 乾淨。不符則停下回報，不繼續。

- [ ] **Step 3: 加 stripe 依賴（鎖 major）**

```bash
uv add "stripe>=12,<13"
```
若 resolve 失敗（PyPI 最新 major 非 12），改跑 `uv add stripe`，再看 `uv run python -c "import stripe; print(stripe.VERSION)"` 的實際 major，把 pyproject.toml 的 bound 改成 `stripe>=<major>,<major+1>`，並在 commit message 註記實際版本。

- [ ] **Step 4: SDK 表面探針**（本計畫所有 stripe import 依賴的表面，一次驗清）

```bash
uv run python -c "
import stripe
print('stripe', stripe.VERSION)
assert hasattr(stripe.checkout.Session, 'create')
assert hasattr(stripe.Webhook, 'construct_event')
for name in ('StripeError', 'APIConnectionError', 'RateLimitError', 'SignatureVerificationError'):
    assert hasattr(stripe, name), name
print('SDK surface OK')
"
```
Expected: `SDK surface OK`。若任一 assert 失敗（版本漂移，例如錯誤類別只在 `stripe.error.*`），在後續 task 以探針實況調整 import 路徑（例如 `from stripe.error import SignatureVerificationError`），並在該 task 的 commit message 註記偏差。

- [ ] **Step 5: 確認離線測試不被新依賴汙染**

```bash
uv run pytest -q
```
Expected: 仍 `662 passed, 2 deselected`（加依賴不改行為）。

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml
git commit -m "feat: add stripe SDK dependency (test-mode billing skeleton, M3)"
```
（uv.lock 依 repo 慣例 gitignored，不 add。）

---

### Task 1: store 加 `billing` 表 ⭐（additive migration）

**Files:**
- Modify: `src/spark/publicapi/store.py`
- Test: `tests/test_publicapi_store.py`（追加，不改既有測試）

先讀 `src/spark/publicapi/store.py` 全檔（113 行）：`_SCHEMA` executescript 於每次 `ApiStore.__init__` 執行，全部 `CREATE TABLE IF NOT EXISTS` → 對既有 DB 自動 additive migration，不需要版本機制。

- [ ] **Step 1: 失敗測試**（追加到 `tests/test_publicapi_store.py` 檔尾）

```python
# ---------- billing（M3 計費骨幹） ----------

def _mkstore(tmp_path):
    from spark.publicapi.store import ApiStore
    return ApiStore(tmp_path / "api.db")


def test_billing_get_missing_returns_none(tmp_path):
    store = _mkstore(tmp_path)
    assert store.get_billing("f" + "ab" * 20) is None


def test_billing_upsert_and_get_roundtrip(tmp_path):
    store = _mkstore(tmp_path)
    acct = "f" + "ab" * 20
    store.upsert_billing(acct, status="active", stripe_customer_id="cus_1",
                         stripe_subscription_id="sub_1", now_s=1000.0, event_created=500)
    rec = store.get_billing(acct)
    assert rec.account_id == acct
    assert rec.stripe_customer_id == "cus_1"
    assert rec.stripe_subscription_id == "sub_1"
    assert rec.status == "active"
    assert rec.updated_at == 1000.0
    assert rec.last_event_created == 500


def test_billing_upsert_is_idempotent_and_keeps_ids_on_none(tmp_path):
    """webhook 事件可能重送：upsert 冪等；後續事件未帶 id（None）不得清掉已存 id。"""
    store = _mkstore(tmp_path)
    acct = "f" + "ab" * 20
    store.upsert_billing(acct, status="active", stripe_customer_id="cus_1",
                         stripe_subscription_id="sub_1", now_s=1000.0, event_created=500)
    store.upsert_billing(acct, status="past_due", now_s=2000.0, event_created=600)  # id 未帶
    rec = store.get_billing(acct)
    assert rec.status == "past_due"
    assert rec.stripe_customer_id == "cus_1"          # COALESCE 保留
    assert rec.stripe_subscription_id == "sub_1"
    assert rec.updated_at == 2000.0


def test_billing_upsert_monotonic_guard_rejects_stale(tmp_path):
    """⭐ 亂序守衛（opus 必改 1）：event_created 較舊的 upsert 整筆 no-op——已取消的
    訂閱不因晚到的舊 active 事件復活；`>=` 允許同值＝重放（同 event）冪等仍成立。"""
    store = _mkstore(tmp_path)
    acct = "f" + "ab" * 20
    store.upsert_billing(acct, status="canceled", now_s=2.0, event_created=200)
    store.upsert_billing(acct, status="active", now_s=3.0, event_created=100)  # 舊事件晚到
    rec = store.get_billing(acct)
    assert rec.status == "canceled"                   # 不復活
    assert rec.last_event_created == 200
    assert rec.updated_at == 2.0                      # 整筆 no-op，非只擋 status
    store.upsert_billing(acct, status="active", now_s=4.0, event_created=200)  # 同值允許
    assert store.get_billing(acct).status == "active"


def test_billing_rejects_unknown_status(tmp_path):
    import pytest
    store = _mkstore(tmp_path)
    with pytest.raises(ValueError):
        store.upsert_billing("f" + "ab" * 20, status="paid", now_s=1.0)


def test_billing_lookup_by_subscription(tmp_path):
    store = _mkstore(tmp_path)
    acct = "f" + "cd" * 20
    store.upsert_billing(acct, status="active", stripe_subscription_id="sub_9", now_s=1.0)
    assert store.get_billing_by_subscription("sub_9").account_id == acct
    assert store.get_billing_by_subscription("sub_none") is None


def test_billing_migration_is_additive_on_existing_db(tmp_path):
    """對「舊 schema DB」重開 store → billing 表自動出現，舊表資料不動。"""
    from spark.publicapi.store import ApiStore
    db = tmp_path / "api.db"
    s1 = ApiStore(db)
    s1.ensure_onboarding("f" + "ab" * 20, "0x" + "ab" * 20)
    s2 = ApiStore(db)  # 重開＝migration 路徑
    assert s2.get_agent_address("f" + "ab" * 20) is None  # 舊表仍在、資料不動
    assert s2.get_billing("f" + "ab" * 20) is None        # 新表可用


def test_billing_table_has_no_sensitive_columns(tmp_path):
    """⭐ 紅線 7 結構性斷言：billing 表欄位集合精確等於白名單——
    無金額、無卡號、無 email；新增欄位必須回這裡改白名單（強迫審視）。"""
    store = _mkstore(tmp_path)
    cols = {row[1] for row in store._db.execute("PRAGMA table_info(billing)")}
    assert cols == {"account_id", "stripe_customer_id", "stripe_subscription_id",
                    "status", "updated_at", "last_event_created"}
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
uv run pytest tests/test_publicapi_store.py -q
```
Expected: 新測試 FAIL（`AttributeError: 'ApiStore' object has no attribute 'get_billing'` 等），既有測試全過。

- [ ] **Step 3: 實作**（`src/spark/publicapi/store.py`）

`_SCHEMA` 字串尾端（`onboarding` 表之後）追加：

```sql
CREATE TABLE IF NOT EXISTS billing (
    account_id TEXT PRIMARY KEY,
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    status TEXT NOT NULL DEFAULT 'none',
    updated_at REAL NOT NULL DEFAULT 0,
    last_event_created INTEGER NOT NULL DEFAULT 0
);
```

`NonceRecord` dataclass 之後追加：

```python
BILLING_STATUSES = frozenset({"none", "active", "past_due", "canceled"})


@dataclass(frozen=True)
class BillingRecord:
    account_id: str
    stripe_customer_id: str | None
    stripe_subscription_id: str | None
    status: str
    updated_at: float
    last_event_created: int  # 已套用的最新 Stripe event.created（亂序守衛水位）
```

`ApiStore` 類尾（`set_agent_address` 之後）追加：

```python
    # --- billing（M3 計費骨幹；無金額/卡號等敏感資料——紅線 7） ---
    def get_billing(self, account_id: str) -> BillingRecord | None:
        with self._lock:
            row = self._db.execute(
                "SELECT account_id, stripe_customer_id, stripe_subscription_id, "
                "status, updated_at, last_event_created FROM billing "
                "WHERE account_id = ?", (account_id,)).fetchone()
        return BillingRecord(*row) if row else None

    def get_billing_by_subscription(self, subscription_id: str) -> BillingRecord | None:
        """webhook subscription 事件無 metadata 時的 fallback 對應（設計定案 4）。"""
        with self._lock:
            row = self._db.execute(
                "SELECT account_id, stripe_customer_id, stripe_subscription_id, "
                "status, updated_at, last_event_created FROM billing "
                "WHERE stripe_subscription_id = ?", (subscription_id,)).fetchone()
        return BillingRecord(*row) if row else None

    def upsert_billing(self, account_id: str, *, status: str,
                       stripe_customer_id: str | None = None,
                       stripe_subscription_id: str | None = None,
                       now_s: float, event_created: int = 0) -> None:
        """upsert：重放（同 event）冪等；**亂序由 event_created 單調守衛擋**（opus 必改 1）
        ——`WHERE excluded.last_event_created >= billing.last_event_created`，較舊事件
        整筆 no-op（含 id 欄），已取消訂閱不因晚到的舊 active 事件復活；`>=` 允許同值
        ＝重放仍冪等。id 欄 None 時 COALESCE 保留既有值。status 白名單強制——
        webhook 映射層是唯一寫入者，這裡是縱深防禦。"""
        if status not in BILLING_STATUSES:
            raise ValueError(f"未知 billing status: {status!r}（須為 {sorted(BILLING_STATUSES)}）")
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO billing (account_id, stripe_customer_id, "
                "stripe_subscription_id, status, updated_at, last_event_created) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(account_id) DO UPDATE SET "
                "stripe_customer_id = COALESCE(excluded.stripe_customer_id, "
                "                              billing.stripe_customer_id), "
                "stripe_subscription_id = COALESCE(excluded.stripe_subscription_id, "
                "                                  billing.stripe_subscription_id), "
                "status = excluded.status, updated_at = excluded.updated_at, "
                "last_event_created = excluded.last_event_created "
                "WHERE excluded.last_event_created >= billing.last_event_created",
                (account_id, stripe_customer_id, stripe_subscription_id, status,
                 now_s, event_created))
```

- [ ] **Step 4: 跑測試確認通過**

```bash
uv run pytest tests/test_publicapi_store.py -q && uv run ruff check src tests
```
Expected: 全 PASS、ruff 乾淨。

- [ ] **Step 5: Commit**

```bash
git add src/spark/publicapi/store.py tests/test_publicapi_store.py
git commit -m "feat: billing table in api store (additive migration, no sensitive columns)"
```

---

### Task 2: config 加 stripe 設定 ⭐（sk_test_ 結構性強制）

**Files:**
- Modify: `src/spark/publicapi/config.py`
- Test: `tests/test_publicapi_config.py`（追加）

先讀 `src/spark/publicapi/config.py` 全檔（82 行）：`ApiConfig` 是 frozen dataclass、`from_env` 統一入口。

- [ ] **Step 1: 失敗測試**（追加到 `tests/test_publicapi_config.py` 檔尾。
`import pytest` 與 `ApiConfig` 併入**檔頭**既有 import 區（已存在就不重複）——
測試檔尾不放 import，避免 ruff E402）

```python
# ---------- stripe 設定（M3 計費骨幹） ----------

def _env(**over):
    base = {"FILET_API_NETWORK": "testnet", "FILET_BUILDER_ADDR": "0x" + "b1" * 20,
            "FILET_SIWE_DOMAIN": "filet.example", "FILET_SIWE_URI": "https://filet.example",
            "FILET_API_DB": "x.db", "FILET_KEYSVC_SOCK": "x.sock",
            "FILET_PENDING_PATH": "p.json"}
    base.update(over)
    return base


def test_stripe_unset_means_billing_disabled():
    cfg = ApiConfig.from_env(_env())
    assert cfg.stripe_secret_key is None
    assert cfg.stripe_webhook_secret is None
    assert cfg.stripe_price_id is None
    assert cfg.billing_enabled is False


def test_stripe_full_set_enables_billing():
    cfg = ApiConfig.from_env(_env(FILET_STRIPE_SECRET_KEY="sk_test_abc",
                                  FILET_STRIPE_WEBHOOK_SECRET="whsec_x",
                                  FILET_STRIPE_PRICE_ID="price_x"))
    assert cfg.billing_enabled is True
    assert cfg.stripe_price_id == "price_x"


def test_stripe_partial_set_refuses_startup():
    """三個一起設或都不設——半開狀態（如漏 webhook secret）直接拒啟動（設計定案 2）。"""
    with pytest.raises(ValueError, match="Stripe"):
        ApiConfig.from_env(_env(FILET_STRIPE_SECRET_KEY="sk_test_abc"))


def test_live_key_refused_at_startup():
    """⭐ 紅線 1：非 sk_test_ 前綴（含 sk_live_）直接拒啟動——真實收費是人工決策。"""
    with pytest.raises(ValueError, match="sk_test_"):
        ApiConfig.from_env(_env(FILET_STRIPE_SECRET_KEY="sk_live_abc",
                                FILET_STRIPE_WEBHOOK_SECRET="whsec_x",
                                FILET_STRIPE_PRICE_ID="price_x"))


def test_live_key_refused_on_direct_construction(tmp_path):
    """⭐ 結構性：不經 from_env 直接建構 ApiConfig 也擋（__post_init__）。"""
    with pytest.raises(ValueError, match="sk_test_"):
        ApiConfig(network="testnet", builder_address="0x" + "b1" * 20,
                  siwe_domain="d", siwe_uri="https://d", db_path="x.db",
                  keysvc_sock="x.sock", pending_path="p.json",
                  admin_addresses=frozenset(),
                  stripe_secret_key="sk_live_abc",
                  stripe_webhook_secret="whsec_x", stripe_price_id="price_x")


def test_partial_set_refused_on_direct_construction():
    """⭐ opus Finding 2：半開狀態（只設 key、缺 webhook secret/price）在直接建構
    路徑也拒——__post_init__ 三元組驗證，不只 from_env。"""
    with pytest.raises(ValueError, match="Stripe"):
        ApiConfig(network="testnet", builder_address="0x" + "b1" * 20,
                  siwe_domain="d", siwe_uri="https://d", db_path="x.db",
                  keysvc_sock="x.sock", pending_path="p.json",
                  admin_addresses=frozenset(),
                  stripe_secret_key="sk_test_abc")


def test_key_not_in_config_repr():
    """secret 不進 repr/log（縱深防禦；dataclass 預設 repr 會印全部欄位——必須遮）。"""
    cfg = ApiConfig.from_env(_env(FILET_STRIPE_SECRET_KEY="sk_test_secret123",
                                  FILET_STRIPE_WEBHOOK_SECRET="whsec_secret456",
                                  FILET_STRIPE_PRICE_ID="price_x"))
    assert "sk_test_secret123" not in repr(cfg)
    assert "whsec_secret456" not in repr(cfg)
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
uv run pytest tests/test_publicapi_config.py -q
```
Expected: 新測試 FAIL（`TypeError: unexpected keyword argument 'stripe_secret_key'` 等）。

- [ ] **Step 3: 實作**（`src/spark/publicapi/config.py`）

`ApiConfig` 欄位區（`nonce_ttl_s: int = 300` 之後）追加：

```python
    # --- M3 計費骨幹（全 optional；未設＝billing 停用，onboarding 不受影響） ---
    # ⭐ 紅線 1：只收 Stripe 測試 key（sk_test_）——真實收費是人工決策（M0 律師條款
    # 未結案），__post_init__ 結構性拒收非測試 key。repr=False：secret 不進 log/repr。
    stripe_secret_key: str | None = field(default=None, repr=False)
    stripe_webhook_secret: str | None = field(default=None, repr=False)
    stripe_price_id: str | None = None

    def __post_init__(self):
        if self.stripe_secret_key is not None and \
                not self.stripe_secret_key.startswith("sk_test_"):
            raise ValueError(
                "FILET_STRIPE_SECRET_KEY 必須是 Stripe 測試 key（sk_test_ 前綴）——"
                "真實收費是人工決策（M0 律師條款未結案），結構性拒收非測試 key")
        # 三元組同設或同缺（opus Finding 2）：不只 from_env——任何建構路徑的半開
        # 狀態（如漏 webhook secret → 驗簽必失敗）都直接拒，結構性收斂
        trio = (self.stripe_secret_key, self.stripe_webhook_secret, self.stripe_price_id)
        if any(v is not None for v in trio) and not all(v is not None for v in trio):
            raise ValueError("Stripe 設定不完整（secret key / webhook secret / price id "
                             "三個一起設或都不設）")

    @property
    def billing_enabled(self) -> bool:
        return self.stripe_secret_key is not None
```

檔頭 import 改為：

```python
from dataclasses import dataclass, field
```

`from_env` 內（`admins = ...` 之後、`return cls(...)` 之前）追加：

```python
        stripe_env = {k: (env.get(k) or None)
                      for k in ("FILET_STRIPE_SECRET_KEY", "FILET_STRIPE_WEBHOOK_SECRET",
                                "FILET_STRIPE_PRICE_ID")}
        present = [k for k, v in stripe_env.items() if v]
        if present and len(present) != 3:
            missing = sorted(set(stripe_env) - set(present))
            raise ValueError(
                f"Stripe 設定不完整（三個一起設或都不設）: 缺少 {', '.join(missing)}")
```

`return cls(...)` 的參數清單追加：

```python
                   stripe_secret_key=stripe_env["FILET_STRIPE_SECRET_KEY"],
                   stripe_webhook_secret=stripe_env["FILET_STRIPE_WEBHOOK_SECRET"],
                   stripe_price_id=stripe_env["FILET_STRIPE_PRICE_ID"],
```

- [ ] **Step 4: 跑測試確認通過**（含既有測試——`tests/publicapi_helpers.py` 的 `make_cfg` 不帶 stripe 欄位，預設 None，應不受影響）

```bash
uv run pytest tests/test_publicapi_config.py tests/test_api_auth.py -q && uv run ruff check src tests
```
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/spark/publicapi/config.py tests/test_publicapi_config.py
git commit -m "feat: stripe config fields with structural sk_test_-only enforcement"
```

---

### Task 3: `billing.py` —— StripeGateway、驗簽、事件處理、entitlement 查詢

**Files:**
- Create: `src/spark/publicapi/billing.py`
- Test: `tests/test_publicapi_billing.py`（Create）

- [ ] **Step 1: 失敗測試**（Create `tests/test_publicapi_billing.py`）

```python
"""tests/test_publicapi_billing.py — billing 模組單元測試（全離線）。
StripeGateway 的 SDK 呼叫用 monkeypatch stripe.checkout.Session.create；
驗簽用真 HMAC（stripe.Webhook.construct_event 本地運算）。socket-ban 是 backstop：
漏 mock 的真外呼會直接炸 RuntimeError。"""
import hashlib
import hmac
import json
import time

import pytest
import stripe

from spark.publicapi.billing import (BillingError, BillingSignatureError, StripeGateway,
                                     apply_webhook_event, has_active_subscription,
                                     map_stripe_status, verify_webhook_event)
from spark.publicapi.store import ApiStore

ACCT = "f" + "ab" * 20
WEBHOOK_SECRET = "whsec_test_secret"


def _store(tmp_path):
    return ApiStore(tmp_path / "api.db")


def _sig(payload: bytes, secret: str = WEBHOOK_SECRET, t: int | None = None) -> str:
    """照 Stripe 簽名規格手工組合法簽名：v1 = HMAC-SHA256(secret, f"{t}.{payload}")。"""
    t = int(time.time()) if t is None else t
    mac = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def _event_payload(etype: str, obj: dict) -> bytes:
    return json.dumps({"id": "evt_1", "object": "event", "type": etype,
                       "data": {"object": obj}}).encode()


# ---------- status 映射（設計定案 5：白名單，未知歸 canceled） ----------

@pytest.mark.parametrize("stripe_status,expected", [
    ("active", "active"), ("trialing", "active"),
    ("past_due", "past_due"), ("unpaid", "past_due"),
    ("canceled", "canceled"), ("incomplete", "canceled"),
    ("incomplete_expired", "canceled"), ("paused", "canceled"),
    ("some_future_status", "canceled"),
])
def test_map_stripe_status(stripe_status, expected):
    assert map_stripe_status(stripe_status) == expected


# ---------- 驗簽（⭐ 紅線 2） ----------

def test_verify_accepts_valid_signature():
    payload = _event_payload("checkout.session.completed", {"id": "cs_1"})
    event = verify_webhook_event(payload, _sig(payload), WEBHOOK_SECRET)
    assert event["type"] == "checkout.session.completed"


def test_verify_rejects_bad_signature():
    payload = _event_payload("checkout.session.completed", {"id": "cs_1"})
    with pytest.raises(BillingSignatureError):
        verify_webhook_event(payload, _sig(payload, secret="whsec_WRONG"), WEBHOOK_SECRET)


def test_verify_rejects_tampered_payload():
    payload = _event_payload("checkout.session.completed", {"id": "cs_1"})
    sig = _sig(payload)
    tampered = payload.replace(b"cs_1", b"cs_2")
    with pytest.raises(BillingSignatureError):
        verify_webhook_event(tampered, sig, WEBHOOK_SECRET)


def test_verify_rejects_stale_timestamp():
    """重放防護：construct_event 預設容忍 300s，過期簽名拒收。"""
    payload = _event_payload("checkout.session.completed", {"id": "cs_1"})
    old = _sig(payload, t=int(time.time()) - 3600)
    with pytest.raises(BillingSignatureError):
        verify_webhook_event(payload, old, WEBHOOK_SECRET)


def test_verify_rejects_garbage_header():
    payload = _event_payload("checkout.session.completed", {"id": "cs_1"})
    with pytest.raises(BillingSignatureError):
        verify_webhook_event(payload, "not-a-signature", WEBHOOK_SECRET)


# ---------- 事件處理（重放冪等、event.created 亂序守衛、對不到帳） ----------

def test_checkout_completed_activates(tmp_path):
    store = _store(tmp_path)
    payload = _event_payload("checkout.session.completed",
                             {"id": "cs_1", "client_reference_id": ACCT,
                              "customer": "cus_1", "subscription": "sub_1"})
    event = verify_webhook_event(payload, _sig(payload), WEBHOOK_SECRET)
    assert apply_webhook_event(store, event, event_created=100, now_s=1000.0) == "activated"
    rec = store.get_billing(ACCT)
    assert rec.status == "active"
    assert rec.stripe_customer_id == "cus_1"
    assert rec.stripe_subscription_id == "sub_1"
    assert rec.last_event_created == 100
    assert has_active_subscription(store, ACCT) is True


def test_subscription_updated_past_due_via_metadata(tmp_path):
    store = _store(tmp_path)
    payload = _event_payload("customer.subscription.updated",
                             {"id": "sub_1", "status": "past_due", "customer": "cus_1",
                              "metadata": {"account_id": ACCT}})
    event = verify_webhook_event(payload, _sig(payload), WEBHOOK_SECRET)
    assert apply_webhook_event(store, event, event_created=110, now_s=1.0) == "updated"
    assert store.get_billing(ACCT).status == "past_due"
    assert has_active_subscription(store, ACCT) is False


def test_subscription_deleted_via_db_fallback(tmp_path):
    """metadata 缺 account_id → fallback 用 DB 的 subscription_id 對回（設計定案 4）。"""
    store = _store(tmp_path)
    store.upsert_billing(ACCT, status="active", stripe_subscription_id="sub_1",
                         now_s=1.0, event_created=100)
    payload = _event_payload("customer.subscription.deleted",
                             {"id": "sub_1", "status": "canceled", "metadata": {}})
    event = verify_webhook_event(payload, _sig(payload), WEBHOOK_SECRET)
    assert apply_webhook_event(store, event, event_created=120, now_s=2.0) == "updated"
    assert store.get_billing(ACCT).status == "canceled"


def test_subscription_event_unmatched_is_ignored(tmp_path):
    store = _store(tmp_path)
    payload = _event_payload("customer.subscription.updated",
                             {"id": "sub_unknown", "status": "active", "metadata": {}})
    event = verify_webhook_event(payload, _sig(payload), WEBHOOK_SECRET)
    assert apply_webhook_event(store, event, event_created=1, now_s=1.0) == "unmatched"
    assert store.get_billing(ACCT) is None


def test_unknown_event_type_ignored(tmp_path):
    store = _store(tmp_path)
    payload = _event_payload("invoice.paid", {"id": "in_1"})
    event = verify_webhook_event(payload, _sig(payload), WEBHOOK_SECRET)
    assert apply_webhook_event(store, event, event_created=1, now_s=1.0) == "ignored"


def test_checkout_completed_bad_account_id_refused(tmp_path):
    """縱深防禦：client_reference_id 不合法（非本系統 account_id 格式）→ 不寫 DB。"""
    store = _store(tmp_path)
    payload = _event_payload("checkout.session.completed",
                             {"id": "cs_1", "client_reference_id": "../etc/passwd",
                              "customer": "cus_1", "subscription": "sub_1"})
    event = verify_webhook_event(payload, _sig(payload), WEBHOOK_SECRET)
    assert apply_webhook_event(store, event, event_created=1, now_s=1.0) == "bad_account"
    assert store.get_billing_by_subscription("sub_1") is None


def test_replayed_event_is_idempotent(tmp_path):
    """Stripe 可能重送同一事件（同 event.created）：兩次 apply 結果相同（`>=` 放行）。"""
    store = _store(tmp_path)
    payload = _event_payload("checkout.session.completed",
                             {"id": "cs_1", "client_reference_id": ACCT,
                              "customer": "cus_1", "subscription": "sub_1"})
    event = verify_webhook_event(payload, _sig(payload), WEBHOOK_SECRET)
    apply_webhook_event(store, event, event_created=100, now_s=1.0)
    apply_webhook_event(store, event, event_created=100, now_s=2.0)
    rec = store.get_billing(ACCT)
    assert rec.status == "active" and rec.updated_at == 2.0


def test_out_of_order_stale_active_does_not_resurrect(tmp_path):
    """⭐ opus 必改 1(a)：canceled(created=T2) 已套用後，晚到的 active(created=T1)
    不得復活權益——status 仍 canceled（event.created 單調守衛）。"""
    store = _store(tmp_path)
    p_cancel = _event_payload("customer.subscription.deleted",
                              {"id": "sub_1", "status": "canceled",
                               "metadata": {"account_id": ACCT}})
    ev = verify_webhook_event(p_cancel, _sig(p_cancel), WEBHOOK_SECRET)
    apply_webhook_event(store, ev, event_created=200, now_s=1.0)
    p_active = _event_payload("customer.subscription.updated",
                              {"id": "sub_1", "status": "active", "customer": "cus_1",
                               "metadata": {"account_id": ACCT}})
    ev2 = verify_webhook_event(p_active, _sig(p_active), WEBHOOK_SECRET)
    assert apply_webhook_event(store, ev2, event_created=100, now_s=2.0) == "updated"
    assert store.get_billing(ACCT).status == "canceled"          # 舊事件 no-op
    assert has_active_subscription(store, ACCT) is False


def test_in_order_cancel_after_active(tmp_path):
    """opus 必改 1(b)：順序正常 active(T1) → canceled(T2) → 最終 canceled。"""
    store = _store(tmp_path)
    p_active = _event_payload("customer.subscription.updated",
                              {"id": "sub_1", "status": "active", "customer": "cus_1",
                               "metadata": {"account_id": ACCT}})
    ev1 = verify_webhook_event(p_active, _sig(p_active), WEBHOOK_SECRET)
    apply_webhook_event(store, ev1, event_created=100, now_s=1.0)
    assert store.get_billing(ACCT).status == "active"
    p_cancel = _event_payload("customer.subscription.deleted",
                              {"id": "sub_1", "status": "canceled",
                               "metadata": {"account_id": ACCT}})
    ev2 = verify_webhook_event(p_cancel, _sig(p_cancel), WEBHOOK_SECRET)
    apply_webhook_event(store, ev2, event_created=200, now_s=2.0)
    assert store.get_billing(ACCT).status == "canceled"


# ---------- StripeGateway（紅線 4：非冪等不盲重試、失敗分類） ----------

def _gateway_call(gw):
    return gw.create_checkout_session(account_id=ACCT, price_id="price_x",
                                      success_url="https://d/ok", cancel_url="https://d/no")


def test_checkout_session_params_and_url(monkeypatch):
    seen = {}

    def fake_create(api_key=None, **params):
        seen.update(params, api_key=api_key)
        return {"id": "cs_1", "url": "https://checkout.stripe.com/c/pay/cs_1"}

    monkeypatch.setattr(stripe.checkout.Session, "create", fake_create)
    gw = StripeGateway("sk_test_abc")
    url = _gateway_call(gw)
    assert url == "https://checkout.stripe.com/c/pay/cs_1"
    assert seen["api_key"] == "sk_test_abc"
    assert seen["mode"] == "subscription"
    assert seen["line_items"] == [{"price": "price_x", "quantity": 1}]
    assert seen["client_reference_id"] == ACCT
    assert seen["subscription_data"] == {"metadata": {"account_id": ACCT}}  # 設計定案 4
    assert seen["success_url"] == "https://d/ok"
    assert seen["cancel_url"] == "https://d/no"
    assert "customer" not in seen  # 無既有 customer 不帶


def test_checkout_reuses_existing_customer(monkeypatch):
    seen = {}
    monkeypatch.setattr(stripe.checkout.Session, "create",
                        lambda api_key=None, **p: (seen.update(p),
                                                   {"id": "cs", "url": "https://u"})[1])
    gw = StripeGateway("sk_test_abc")
    gw.create_checkout_session(account_id=ACCT, price_id="price_x",
                               success_url="https://d/ok", cancel_url="https://d/no",
                               customer_id="cus_1")
    assert seen["customer"] == "cus_1"


def test_transient_error_translated_no_retry(monkeypatch):
    """APIConnectionError → ConnectionError（app 統一 502「稍後重試」）；
    且**只呼叫一次**——checkout 建立非冪等，絕不盲重試（工程原則 2）。"""
    calls = []

    def boom(api_key=None, **p):
        calls.append(1)
        raise stripe.APIConnectionError("conn reset")

    monkeypatch.setattr(stripe.checkout.Session, "create", boom)
    with pytest.raises(ConnectionError):
        _gateway_call(StripeGateway("sk_test_abc"))
    assert len(calls) == 1


def test_rate_limit_is_transient(monkeypatch):
    def boom(api_key=None, **p):
        raise stripe.RateLimitError("slow down")
    monkeypatch.setattr(stripe.checkout.Session, "create", boom)
    with pytest.raises(ConnectionError):
        _gateway_call(StripeGateway("sk_test_abc"))


def test_semantic_error_is_billing_error(monkeypatch):
    def boom(api_key=None, **p):
        raise stripe.StripeError("no such price")
    monkeypatch.setattr(stripe.checkout.Session, "create", boom)
    with pytest.raises(BillingError):
        _gateway_call(StripeGateway("sk_test_abc"))


def test_missing_url_is_billing_error(monkeypatch):
    monkeypatch.setattr(stripe.checkout.Session, "create",
                        lambda api_key=None, **p: {"id": "cs_1", "url": None})
    with pytest.raises(BillingError):
        _gateway_call(StripeGateway("sk_test_abc"))


def test_secret_key_not_in_gateway_repr_or_errors(monkeypatch):
    """key 不進 repr 與例外訊息（紅線 1 縱深防禦）。"""
    def boom(api_key=None, **p):
        raise stripe.StripeError("bad request")
    monkeypatch.setattr(stripe.checkout.Session, "create", boom)
    gw = StripeGateway("sk_test_supersecret")
    assert "sk_test_supersecret" not in repr(gw)
    with pytest.raises(BillingError) as ei:
        _gateway_call(gw)
    assert "sk_test_supersecret" not in str(ei.value)


# ---------- entitlement（紅線 6：只查不動） ----------

def test_has_active_subscription_states(tmp_path):
    store = _store(tmp_path)
    assert has_active_subscription(store, ACCT) is False        # 無紀錄
    store.upsert_billing(ACCT, status="past_due", now_s=1.0)
    assert has_active_subscription(store, ACCT) is False
    store.upsert_billing(ACCT, status="active", now_s=2.0)
    assert has_active_subscription(store, ACCT) is True
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
uv run pytest tests/test_publicapi_billing.py -q
```
Expected: FAIL（`ModuleNotFoundError: No module named 'spark.publicapi.billing'`）。

- [ ] **Step 3: 實作**（Create `src/spark/publicapi/billing.py`）

```python
"""src/spark/publicapi/billing.py
M3 計費骨幹（**全程 Stripe 測試模式**；sk_test_ 強制在 ApiConfig）。
- StripeGateway：publicapi 對 Stripe 的唯一出口（單一 resilience boundary，工程原則 5）。
  checkout session 建立是**非冪等寫入** → 單次嘗試、絕不盲重試（工程原則 2）；
  transient 轉譯內建 ConnectionError（沿 hl.py 慣例，app 統一 502「稍後重試」——
  由前端使用者重按，人肉重試天然去重）；semantic → BillingError（502 專屬 handler）。
- verify_webhook_event：⭐ 驗簽必過才回 Event（偽造 webhook = 免費開通）。本地 HMAC。
- apply_webhook_event：已驗簽事件 → billing 表 upsert。帳號歸屬由 metadata/DB 雙保險；
  狀態覆蓋由 event.created 單調守衛防亂序；重放（同 event）冪等。
- has_active_subscription：entitlement **只查不動**——不接任何自動停用跟單邏輯
  （停用是政策決策，留使用者人工裁決；紅線 6）。"""
import logging

from spark.filet.followers import validate_account_id
from spark.publicapi.store import ApiStore

logger = logging.getLogger(__name__)

# stripe 訂閱狀態 → 本地 status（設計定案 5：白名單映射，未知值歸 canceled——保守不給權益）
_STRIPE_STATUS_MAP = {
    "active": "active",
    "trialing": "active",
    "past_due": "past_due",
    "unpaid": "past_due",
}


def map_stripe_status(stripe_status: str) -> str:
    return _STRIPE_STATUS_MAP.get(stripe_status, "canceled")


class BillingError(RuntimeError):
    """Stripe 語意失敗（semantic，不重試）：請求被拒、設定錯、回應缺欄位。"""


class BillingSignatureError(BillingError):
    """webhook 驗簽失敗（⭐ 紅線 2）——呼叫端一律 400、不碰 DB。"""


class StripeGateway:
    """create_fn 可注入（測試給 fake）；預設走 stripe SDK、per-call api_key
    （無全域 stripe.api_key 狀態）。失敗分類集中在 create_checkout_session 一處，
    注入 fake 也繞不開（結構性）。"""

    def __init__(self, secret_key: str, create_fn=None):
        self._secret_key = secret_key
        self._create = create_fn or self._default_create

    def __repr__(self) -> str:  # key 不進 repr/log（紅線 1）
        return "<StripeGateway test-mode>"

    def _default_create(self, **params):
        import stripe
        return stripe.checkout.Session.create(api_key=self._secret_key, **params)

    def create_checkout_session(self, *, account_id: str, price_id: str,
                                success_url: str, cancel_url: str,
                                customer_id: str | None = None) -> str:
        """建 Checkout Session（mode=subscription），回 checkout URL。
        client_reference_id 與 subscription metadata 雙塞 account_id（設計定案 4）。"""
        import stripe
        params = dict(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            client_reference_id=account_id,
            subscription_data={"metadata": {"account_id": account_id}},
            success_url=success_url,
            cancel_url=cancel_url,
        )
        if customer_id:
            params["customer"] = customer_id
        try:
            session = self._create(**params)
        except (stripe.APIConnectionError, stripe.RateLimitError) as e:
            # transient；但 checkout 建立非冪等 → 不在邊界重試（工程原則 2），
            # 轉譯 ConnectionError → 502「稍後重試」由前端使用者重按（人肉重試天然去重）
            raise ConnectionError(f"stripe 連線失敗: {type(e).__name__}") from e
        except stripe.StripeError as e:
            # 注意：APIError（Stripe 端 5xx）語意上屬 transient，但落在這個分支——
            # 刻意的：非冪等寫入無論 transient/semantic 都統一單次嘗試不重試，
            # 分支差異只在 log 語氣與例外型別，不在重試行為（opus Finding 5）
            raise BillingError(f"stripe checkout 建立被拒: {type(e).__name__}: "
                               f"{getattr(e, 'user_message', None) or e}") from e
        url = session["url"] if isinstance(session, dict) else getattr(session, "url", None)
        if not url:
            raise BillingError("stripe 回應缺 checkout url")
        return url


def verify_webhook_event(payload: bytes, sig_header: str, webhook_secret: str):
    """⭐ 驗簽必過才回 Event（本地 HMAC-SHA256 + 時戳容忍，construct_event 不觸網）。
    格式壞/簽錯/過期一律 BillingSignatureError——呼叫端 400、不碰 DB。"""
    import stripe
    try:
        return stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except (stripe.SignatureVerificationError, ValueError) as e:
        raise BillingSignatureError(f"webhook 驗簽失敗: {type(e).__name__}") from e


def apply_webhook_event(store: ApiStore, event, *, event_created: int,
                        now_s: float) -> str:
    """處理**已驗簽**事件 → billing 表。回傳結果標籤（log/測試用；標籤代表
    「已處理該類事件」，較舊事件經守衛 no-op 時仍回 "updated"——DB 才是真相）。
    - 帳號歸屬：metadata/DB 雙保險（設計定案 4）。
    - 狀態覆蓋：event.created 單調守衛防亂序（upsert 的 WHERE 條件，opus 必改 1）
      ——Stripe 不保證事件順序，晚到的舊 active 不得復活已取消訂閱。
    - 重放（同 event、同 created）：冪等（`>=` 放行）。
    - 未知事件類型 → "ignored"（回 200 ack，不累積 Stripe 重送佇列）。
    event_created 由呼叫端從 event["created"]（epoch 秒）取。"""
    etype = event["type"]
    obj = event["data"]["object"]
    if etype == "checkout.session.completed":
        account_id = obj.get("client_reference_id")
        try:
            validate_account_id(account_id or "")
        except Exception:  # noqa: BLE001 — 縱深防禦：格式不對就拒寫，大聲留痕
            logger.error("checkout.session.completed 的 client_reference_id 不合法，"
                         "拒絕入帳: session=%s", obj.get("id"))
            return "bad_account"
        # test-mode 刻意接受未檢 payment_status（收到已驗簽 completed 即開通）；
        # 正式收費前與 reconcile 計畫一併收（opus Finding 3；見「刻意不做」12）
        store.upsert_billing(account_id, status="active",
                             stripe_customer_id=obj.get("customer"),
                             stripe_subscription_id=obj.get("subscription"),
                             now_s=now_s, event_created=event_created)
        logger.info("billing 開通 account=%s", account_id)
        return "activated"
    if etype in ("customer.subscription.updated", "customer.subscription.deleted"):
        status = ("canceled" if etype.endswith("deleted")
                  else map_stripe_status(obj.get("status", "")))
        account_id = (obj.get("metadata") or {}).get("account_id")
        if not account_id:
            rec = store.get_billing_by_subscription(obj.get("id", ""))
            account_id = rec.account_id if rec else None
        if not account_id:
            logger.warning("subscription 事件對不到 account（sub=%s）——"
                           "可能是外部手建訂閱，忽略", obj.get("id"))
            return "unmatched"
        store.upsert_billing(account_id, status=status,
                             stripe_customer_id=obj.get("customer"),
                             stripe_subscription_id=obj.get("id"),
                             now_s=now_s, event_created=event_created)
        logger.info("billing 狀態更新 account=%s status=%s", account_id, status)
        return "updated"
    return "ignored"


def has_active_subscription(store: ApiStore, account_id: str) -> bool:
    """entitlement 查詢（唯讀）。⭐ 刻意只有查詢：跟單停用是政策決策（人工），
    本函式不得接任何自動停用邏輯（紅線 6）。"""
    rec = store.get_billing(account_id)
    return rec is not None and rec.status == "active"
```

- [ ] **Step 4: 跑測試確認通過**

```bash
uv run pytest tests/test_publicapi_billing.py -q && uv run ruff check src tests
```
Expected: 全 PASS。若 stripe 錯誤類別 import 失敗（Task 0 探針已預警的版本漂移），改 `from stripe.error import ...` 對應調整並在 commit message 註記。

- [ ] **Step 5: Commit**

```bash
git add src/spark/publicapi/billing.py tests/test_publicapi_billing.py
git commit -m "feat: billing module — stripe gateway, webhook verify/apply, entitlement query"
```

---

### Task 4: webhook 端點 ⭐（app.py 掛線 + 真 HMAC 端到端）

**Files:**
- Modify: `src/spark/publicapi/app.py`
- Modify: `tests/publicapi_helpers.py`（`make_app` 加 billing 參數，additive）
- Test: `tests/test_api_billing.py`（Create；本 task 先寫 webhook 段，Task 5 續寫 checkout/status 段）

- [ ] **Step 1: helpers 擴充**（`tests/publicapi_helpers.py`）

`make_cfg` 不動。`make_app` 改為（additive 參數，既有呼叫者不受影響）：

```python
def make_app(tmp_path, cfg=None, billing=None):
    cfg = cfg or make_cfg(tmp_path)
    store = ApiStore(cfg.db_path)
    keysvc, hl = FakeKeysvc(), FakeHL()
    return create_app(cfg, store, keysvc, hl, billing=billing), cfg, store, keysvc, hl
```

檔尾追加共用常數與簽名 helper（Task 5 也用）：

```python
# ---------- billing 測試共用（M3） ----------
STRIPE_WEBHOOK_SECRET = "whsec_test_secret"


def billing_cfg(tmp_path, **over):
    return make_cfg(tmp_path, stripe_secret_key="sk_test_abc",
                    stripe_webhook_secret=STRIPE_WEBHOOK_SECRET,
                    stripe_price_id="price_test_1", **over)


def stripe_sig(payload: bytes, secret: str = STRIPE_WEBHOOK_SECRET,
               t: int | None = None) -> str:
    import hashlib
    import hmac
    import time as _time
    t = int(_time.time()) if t is None else t
    mac = hmac.new(secret.encode(), f"{t}.".encode() + payload,
                   hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"
```

- [ ] **Step 2: 失敗測試**（Create `tests/test_api_billing.py`，先寫 webhook 段）

```python
"""tests/test_api_billing.py — billing 端點測試（全離線）。
webhook 驗簽走真 HMAC；stripe checkout 用注入 create_fn 的 StripeGateway。"""
import json

from fastapi.testclient import TestClient

from spark.publicapi.billing import StripeGateway
from tests.publicapi_helpers import billing_cfg, make_app, stripe_sig

# （helpers 的 import 路徑沿既有測試慣例——先看 tests/test_api_auth.py 檔頭，
# 若既有寫法是 `from publicapi_helpers import ...` 就照抄該形式。）


def _billing_app(tmp_path, create_fn=None):
    cfg = billing_cfg(tmp_path)
    gw = StripeGateway("sk_test_abc", create_fn=create_fn or
                       (lambda **p: {"id": "cs_1", "url": "https://checkout.example/cs_1"}))
    app, cfg, store, keysvc, hl = make_app(tmp_path, cfg=cfg, billing=gw)
    return app, cfg, store


def _event(etype: str, obj: dict, created: int = 1_700_000_000) -> bytes:
    return json.dumps({"id": "evt_1", "object": "event", "type": etype,
                       "created": created, "data": {"object": obj}}).encode()


# ---------- webhook（⭐ 紅線 2：驗簽必過；唯一 session-auth 豁免端點） ----------

def test_webhook_valid_signature_activates(tmp_path):
    app, cfg, store = _billing_app(tmp_path)
    client = TestClient(app)
    acct = "f" + "ab" * 20
    payload = _event("checkout.session.completed",
                     {"id": "cs_1", "client_reference_id": acct,
                      "customer": "cus_1", "subscription": "sub_1"})
    r = client.post("/api/billing/webhook", content=payload,
                    headers={"stripe-signature": stripe_sig(payload)})
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "activated"
    assert store.get_billing(acct).status == "active"


def test_webhook_bad_signature_400_and_no_db_write(tmp_path):
    """⭐ 偽造 webhook = 免費開通：簽錯 → 400，且 DB 一個 byte 都不動。"""
    app, cfg, store = _billing_app(tmp_path)
    client = TestClient(app)
    acct = "f" + "ab" * 20
    payload = _event("checkout.session.completed",
                     {"id": "cs_1", "client_reference_id": acct,
                      "customer": "cus_1", "subscription": "sub_1"})
    r = client.post("/api/billing/webhook", content=payload,
                    headers={"stripe-signature": stripe_sig(payload, secret="whsec_WRONG")})
    assert r.status_code == 400
    assert store.get_billing(acct) is None


def test_webhook_missing_signature_header_400(tmp_path):
    app, cfg, store = _billing_app(tmp_path)
    client = TestClient(app)
    payload = _event("checkout.session.completed", {"id": "cs_1"})
    r = client.post("/api/billing/webhook", content=payload)
    assert r.status_code == 400


def test_webhook_needs_no_session(tmp_path):
    """webhook 是 Stripe 伺服器回呼——**不帶 cookie 也要能進驗簽**（豁免是刻意的，
    授權由 HMAC 取代）。用合法簽名、無 session 驗證通路存在。"""
    app, cfg, store = _billing_app(tmp_path)
    client = TestClient(app)  # 無 login
    payload = _event("invoice.paid", {"id": "in_1"})
    r = client.post("/api/billing/webhook", content=payload,
                    headers={"stripe-signature": stripe_sig(payload)})
    assert r.status_code == 200
    assert r.json()["outcome"] == "ignored"


def test_webhook_subscription_lifecycle(tmp_path):
    """completed → updated(past_due) → deleted：DB 狀態逐步跟進。"""
    app, cfg, store = _billing_app(tmp_path)
    client = TestClient(app)
    acct = "f" + "cd" * 20

    p1 = _event("checkout.session.completed",
                {"id": "cs_1", "client_reference_id": acct,
                 "customer": "cus_9", "subscription": "sub_9"}, created=1000)
    client.post("/api/billing/webhook", content=p1,
                headers={"stripe-signature": stripe_sig(p1)})
    assert store.get_billing(acct).status == "active"

    p2 = _event("customer.subscription.updated",
                {"id": "sub_9", "status": "past_due", "customer": "cus_9",
                 "metadata": {"account_id": acct}}, created=1001)
    client.post("/api/billing/webhook", content=p2,
                headers={"stripe-signature": stripe_sig(p2)})
    assert store.get_billing(acct).status == "past_due"

    p3 = _event("customer.subscription.deleted",
                {"id": "sub_9", "status": "canceled", "metadata": {}}, created=1002)
    client.post("/api/billing/webhook", content=p3,
                headers={"stripe-signature": stripe_sig(p3)})
    assert store.get_billing(acct).status == "canceled"


def test_webhook_501_when_billing_disabled(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)  # 無 stripe 設定、無 gateway
    client = TestClient(app)
    r = client.post("/api/billing/webhook", content=b"{}",
                    headers={"stripe-signature": "t=1,v1=x"})
    assert r.status_code == 501
```

- [ ] **Step 3: 跑測試確認失敗**

```bash
uv run pytest tests/test_api_billing.py -q
```
Expected: FAIL（404 —— webhook 路由不存在；`make_app` 若未先改會 TypeError）。

- [ ] **Step 4: 實作**（`src/spark/publicapi/app.py`）

檔頭 import 區追加：

```python
from spark.publicapi.billing import (BillingError, BillingSignatureError,
                                     apply_webhook_event, has_active_subscription,
                                     verify_webhook_event)
```

`create_app` 簽名改為（additive 預設參數，既有呼叫者不受影響——紅線 5）：

```python
def create_app(cfg: ApiConfig, store: ApiStore, keysvc, hl, now_fn=time.time,
               billing=None) -> FastAPI:
```

既有兩個 exception handler 之後追加：

```python
    @app.exception_handler(BillingError)
    async def _billing_error(request, exc):
        # semantic 失敗（設定錯/請求被拒）：不重試、大聲留痕（工程原則 3）
        logger.error("stripe 語意失敗: %s", exc)
        return JSONResponse(status_code=502,
                            content={"detail": "計費服務錯誤，請稍後重試或聯絡管理員"})
```

`_require_session` 之後追加：

```python
    def _require_billing() -> None:
        if billing is None or not cfg.billing_enabled:
            raise HTTPException(status_code=501, detail="計費未啟用")
```

檔尾（`payload_approve_builder_fee` 之後、`return app` 之前）追加 webhook 端點：

```python
    # ---------- billing（M3 計費骨幹；測試模式 only，sk_test_ 由 ApiConfig 強制） ----------
    @app.post("/api/billing/webhook")
    async def billing_webhook(request: Request):
        # ⭐ 全 app 唯一不走 session auth 的端點（紅線 2）：Stripe 伺服器對伺服器
        # 回呼無 cookie；授權由 Stripe-Signature HMAC 驗簽取代（secret 只有 Stripe
        # 與本服務知道）。驗簽不過一律 400、不碰 DB。async：需先取 raw body 驗簽。
        _require_billing()
        payload = await request.body()
        sig = request.headers.get("stripe-signature", "")
        try:
            event = verify_webhook_event(payload, sig, cfg.stripe_webhook_secret)
        except BillingSignatureError:
            # 不進 BillingError 的 502 handler：簽名壞是呼叫者的錯（400），
            # 且刻意不回洩簽名失敗細節
            raise HTTPException(status_code=400, detail="webhook 驗簽失敗") from None
        outcome = apply_webhook_event(store, event,
                                      event_created=int(event.get("created", 0)),
                                      now_s=now_fn())
        return {"received": True, "outcome": outcome}
```

- [ ] **Step 5: 跑測試確認通過**（含既有全量——create_app 簽名變更不得掃到任何舊測試）

```bash
uv run pytest tests/test_api_billing.py -q && uv run pytest -q && uv run ruff check src tests
```
Expected: 新測試全 PASS；全量 = 基線 + 新增數，0 failed。

- [ ] **Step 6: Commit**

```bash
git add src/spark/publicapi/app.py tests/publicapi_helpers.py tests/test_api_billing.py
git commit -m "feat: stripe webhook endpoint — signature-verified, session-auth exempt by design"
```

---

### Task 5: checkout / status 端點 + 501 隔離

**Files:**
- Modify: `src/spark/publicapi/app.py`
- Test: `tests/test_api_billing.py`（追加）

- [ ] **Step 1: 失敗測試**（追加到 `tests/test_api_billing.py` 檔尾；同時在檔頭 import 區
補上本段需要的兩個名字：`from spark.publicapi.config import derive_account_id` 與
helpers 的 `login`——Task 4 刻意沒先 import，避免當時 ruff F401）

```python
# ---------- checkout / status（紅線 3：未啟用 501、onboarding 隔離） ----------

def test_checkout_returns_url_bound_to_session(tmp_path):
    seen = {}

    def create_fn(**p):
        seen.update(p)
        return {"id": "cs_1", "url": "https://checkout.example/cs_1"}

    app, cfg, store = _billing_app(tmp_path, create_fn=create_fn)
    client = TestClient(app)
    wallet = login(client)
    r = client.post("/api/billing/checkout")
    assert r.status_code == 200, r.text
    assert r.json() == {"checkout_url": "https://checkout.example/cs_1"}
    # ⭐ account 綁 session（沿紅線「別人不能替你 onboard」精神）：無 body 參數，
    # client_reference_id 只能來自 session 衍生
    assert seen["client_reference_id"] == derive_account_id(wallet.address)
    # success/cancel URL 從 siwe_uri 衍生（設計定案 3）
    assert seen["success_url"] == f"{cfg.siwe_uri}/billing?checkout=success"
    assert seen["cancel_url"] == f"{cfg.siwe_uri}/billing?checkout=cancel"


def test_checkout_409_when_already_active(tmp_path):
    app, cfg, store = _billing_app(tmp_path)
    client = TestClient(app)
    wallet = login(client)
    store.upsert_billing(derive_account_id(wallet.address), status="active", now_s=1.0)
    r = client.post("/api/billing/checkout")
    assert r.status_code == 409


def test_checkout_allows_retry_after_cancel_and_reuses_customer(tmp_path):
    seen = {}

    def create_fn(**p):
        seen.update(p)
        return {"id": "cs_2", "url": "https://checkout.example/cs_2"}

    app, cfg, store = _billing_app(tmp_path, create_fn=create_fn)
    client = TestClient(app)
    wallet = login(client)
    store.upsert_billing(derive_account_id(wallet.address), status="canceled",
                         stripe_customer_id="cus_1", now_s=1.0)
    r = client.post("/api/billing/checkout")
    assert r.status_code == 200
    assert seen["customer"] == "cus_1"  # 既有 customer 重用（設計定案 12）


def test_checkout_requires_session(tmp_path):
    app, cfg, store = _billing_app(tmp_path)
    r = TestClient(app).post("/api/billing/checkout")
    assert r.status_code == 401


def test_checkout_transient_stripe_failure_is_502(tmp_path):
    def create_fn(**p):
        raise ConnectionError("stripe 連線失敗")  # gateway 對 transient 轉譯後的形態

    app, cfg, store = _billing_app(tmp_path, create_fn=create_fn)
    client = TestClient(app, raise_server_exceptions=False)
    login(client)
    r = client.post("/api/billing/checkout")
    assert r.status_code == 502  # 既有 ConnectionError handler：前端「稍後重試」


def test_status_reads_db(tmp_path):
    app, cfg, store = _billing_app(tmp_path)
    client = TestClient(app)
    wallet = login(client)
    acct = derive_account_id(wallet.address)
    r = client.get("/api/billing/status")
    assert r.status_code == 200
    assert r.json() == {"account_id": acct, "status": "none", "active": False}
    store.upsert_billing(acct, status="active", now_s=1.0)
    r = client.get("/api/billing/status")
    assert r.json() == {"account_id": acct, "status": "active", "active": True}


def test_status_requires_session(tmp_path):
    app, cfg, store = _billing_app(tmp_path)
    assert TestClient(app).get("/api/billing/status").status_code == 401


def test_billing_endpoints_501_when_disabled(tmp_path):
    """紅線 3：三個 stripe env 未設 → billing 端點 501「計費未啟用」。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = TestClient(app)
    login(client)
    assert client.post("/api/billing/checkout").status_code == 501
    assert client.get("/api/billing/status").status_code == 501


def test_onboarding_unaffected_when_billing_disabled(tmp_path):
    """紅線 3 的另一半：billing 未設時 onboarding 全流程行為與 M2 相同（隔離）。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = TestClient(app)
    wallet = login(client)
    r = client.post("/api/onboard/agent")
    assert r.status_code == 200
    r = client.get("/api/onboard/status")
    assert r.status_code == 200
    assert r.json()["agent_generated"] is True
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
uv run pytest tests/test_api_billing.py -q
```
Expected: webhook 段 PASS、新段 FAIL（404）。

- [ ] **Step 3: 實作**（`src/spark/publicapi/app.py`，webhook 端點之後追加）

```python
    @app.post("/api/billing/checkout")
    def billing_checkout(address: str = Depends(_require_session)):
        """建 Checkout Session、回 URL。session 綁定：account_id 由 session 衍生，
        端點無輸入參數。冪等擋板：已 active → 409。
        Stripe 失敗分類（紅線 4）：transient=ConnectionError→502 稍後重試（人肉重試，
        非冪等寫入不在後端盲重試）；semantic=BillingError→502 專屬 handler。"""
        _require_billing()
        account_id = derive_account_id(address)
        rec = store.get_billing(account_id)
        if rec is not None and rec.status == "active":
            raise HTTPException(status_code=409, detail="已有生效訂閱")
        url = billing.create_checkout_session(
            account_id=account_id, price_id=cfg.stripe_price_id,
            success_url=f"{cfg.siwe_uri}/billing?checkout=success",
            cancel_url=f"{cfg.siwe_uri}/billing?checkout=cancel",
            customer_id=rec.stripe_customer_id if rec else None)
        return {"checkout_url": url}

    @app.get("/api/billing/status")
    def billing_status(address: str = Depends(_require_session)):
        """讀 DB（webhook 是唯一寫入者）。active 欄位 = entitlement 查詢結果——
        僅供前端顯示；不接任何自動停用邏輯（紅線 6）。"""
        _require_billing()
        account_id = derive_account_id(address)
        rec = store.get_billing(account_id)
        return {"account_id": account_id,
                "status": rec.status if rec else "none",
                "active": has_active_subscription(store, account_id)}
```

- [ ] **Step 4: 跑測試確認通過**

```bash
uv run pytest tests/test_api_billing.py -q && uv run pytest -q && uv run ruff check src tests
```
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/spark/publicapi/app.py tests/test_api_billing.py
git commit -m "feat: billing checkout/status endpoints — session-bound, 409 idempotency, 501 when disabled"
```

---

### Task 6: run_api 接線 + deploy env 註解

**Files:**
- Modify: `scripts/run_api.py`
- Modify: `deploy/filet-api.service`

- [ ] **Step 1: run_api 接線**（`scripts/run_api.py` `main()` 內，`app = create_app(...)` 改為）

```python
    from spark.publicapi.billing import StripeGateway
    billing = (StripeGateway(cfg.stripe_secret_key) if cfg.billing_enabled else None)
    app = create_app(cfg, ApiStore(cfg.db_path), KeysvcClient(cfg.keysvc_sock),
                     HLGateway(cfg.api_url), billing=billing)
```

模組 docstring 的用法區追加一行（env 說明）：

```
      [FILET_STRIPE_SECRET_KEY=sk_test_..（僅測試 key；三個 stripe env 一起設或都不設）] \
      [FILET_STRIPE_WEBHOOK_SECRET=whsec_..] [FILET_STRIPE_PRICE_ID=price_..] \
```

- [ ] **Step 2: deploy/filet-api.service**——`Environment=FILET_API_PORT=8700` 之後追加**註解**（預設停用；填了非 sk_test_ 值 app 會拒啟動，所以不放 REPLACE 佔位真值）：

```ini
# M3 計費（測試模式）：要啟用時取消註解並填 Stripe **測試** key（sk_test_ 前綴強制，
# 非測試 key 會拒啟動）。三個一起設或都不設。正式收費是人工決策（M0 律師條款未結案）。
#Environment=FILET_STRIPE_SECRET_KEY=sk_test_REPLACE
#Environment=FILET_STRIPE_WEBHOOK_SECRET=whsec_REPLACE
#Environment=FILET_STRIPE_PRICE_ID=price_REPLACE
```

- [ ] **Step 3: 驗證**——接線是 wiring 無單測（沿 run_api 慣例，`tests/test_scripts_import.py` 會蓋 import 面）：

```bash
uv run pytest -q && uv run ruff check src tests scripts
uv run python -c "import scripts.run_api"   # import 階段零副作用
```
Expected: 全 PASS、import 無輸出無錯。

- [ ] **Step 4: Commit**

```bash
git add scripts/run_api.py deploy/filet-api.service
git commit -m "feat: wire stripe gateway into run_api; document test-mode env in systemd unit"
```

---

### Task 7: `HLGateway.clearinghouse_state` + `filet/leaderboard.py`（watchlist 快照純函式）

**Files:**
- Modify: `src/spark/publicapi/hl.py`
- Create: `src/spark/filet/leaderboard.py`
- Test: `tests/test_filet_leaderboard.py`（Create）；`tests/test_publicapi_hl.py`（追加一測）

- [ ] **Step 1: 失敗測試 — hl 擴充**（追加到 `tests/test_publicapi_hl.py` 檔尾；先讀該檔沿用其 fake post 慣例）

```python
def test_clearinghouse_state_returns_full_state():
    """M3 watchlist 快照用：回完整 state（get_account_value 只取其中一欄，行為不變）。"""
    state = {"marginSummary": {"accountValue": "123.5", "totalMarginUsed": "10",
                               "totalNtlPos": "50"},
             "withdrawable": "100", "assetPositions": []}

    def fake_post(url, body):
        assert body == {"type": "clearinghouseState", "user": "0xabc"}
        return state

    from spark.publicapi.hl import HLGateway
    gw = HLGateway("https://api.example", post_fn=fake_post, sleep_fn=lambda s: None)
    assert gw.clearinghouse_state("0xabc") == state
    from decimal import Decimal
    assert gw.get_account_value("0xabc") == Decimal("123.5")
```

- [ ] **Step 2: 失敗測試 — leaderboard 純函式**（Create `tests/test_filet_leaderboard.py`）

```python
"""tests/test_filet_leaderboard.py — watchlist 快照純函式（不觸網、不寫檔除了 tmp）。"""
import json
from datetime import date
from decimal import Decimal

from spark.filet.leaderboard import (DEFAULT_WATCHLIST, snapshot_watchlist,
                                     write_snapshot)

ADDR1 = "0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1"
ADDR2 = "0x" + "22" * 20

STATE1 = {"marginSummary": {"accountValue": "1000.5", "totalMarginUsed": "200",
                            "totalNtlPos": "800"},
          "withdrawable": "300.25",
          "assetPositions": [
              {"position": {"coin": "ETH", "unrealizedPnl": "12.5"}},
              {"position": {"coin": "BTC", "unrealizedPnl": "-2.5"}},
          ]}


def test_default_watchlist_contains_m1_leader():
    assert ADDR1 in DEFAULT_WATCHLIST


def test_snapshot_normalizes_fields():
    snap = snapshot_watchlist(lambda a: STATE1, [ADDR1], date(2026, 7, 18))
    assert snap["day"] == "2026-07-18"
    assert snap["source"] == "clearinghouseState"
    assert snap["row_count"] == 1 and snap["error_count"] == 0
    row = snap["rows"][0]
    assert row["address"] == ADDR1
    assert Decimal(row["account_value"]) == Decimal("1000.5")
    assert Decimal(row["total_margin_used"]) == Decimal("200")
    assert Decimal(row["total_ntl_pos"]) == Decimal("800")
    assert Decimal(row["withdrawable"]) == Decimal("300.25")
    assert Decimal(row["unrealized_pnl"]) == Decimal("10.0")  # 12.5 + (-2.5)
    assert row["position_count"] == 2


def test_snapshot_isolates_per_address_failure():
    """一個 leader 查掛不弄丟整批（定案 10）：error 條目 + error_count，其餘照常。"""
    def state_fn(addr):
        if addr == ADDR2:
            raise ConnectionError("boom")
        return STATE1

    snap = snapshot_watchlist(state_fn, [ADDR1, ADDR2], date(2026, 7, 18))
    assert snap["row_count"] == 1 and snap["error_count"] == 1
    ok = [r for r in snap["rows"] if "error" not in r]
    bad = [r for r in snap["rows"] if "error" in r]
    assert ok[0]["address"] == ADDR1
    assert bad[0]["address"] == ADDR2 and "boom" in bad[0]["error"]


def test_snapshot_malformed_state_is_isolated_too():
    snap = snapshot_watchlist(lambda a: {"unexpected": True}, [ADDR1], date(2026, 7, 18))
    assert snap["error_count"] == 1 and snap["row_count"] == 0


def test_write_snapshot_atomic_and_idempotent(tmp_path):
    """同日重跑覆寫同檔（冪等）；寫完無 .tmp 殘檔（原子：tmp + os.replace）。"""
    out = tmp_path / "watchlist"
    snap1 = snapshot_watchlist(lambda a: STATE1, [ADDR1], date(2026, 7, 18))
    p1 = write_snapshot(out, snap1)
    assert p1 == out / "2026-07-18.json"
    snap2 = snapshot_watchlist(lambda a: STATE1, [ADDR1, ADDR1], date(2026, 7, 18))
    p2 = write_snapshot(out, snap2)
    assert p2 == p1                                  # 同檔覆寫
    data = json.loads(p1.read_text())
    assert data["row_count"] == 2                    # 內容是第二次的
    assert list(out.glob("*")) == [p1]               # 目錄裡只有正式檔，無 tmp 殘檔
```

- [ ] **Step 3: 跑測試確認失敗**

```bash
uv run pytest tests/test_publicapi_hl.py tests/test_filet_leaderboard.py -q
```
Expected: FAIL（`clearinghouse_state` 不存在；`spark.filet.leaderboard` 不存在）。

- [ ] **Step 4: 實作 — hl.py**（`get_account_value` 改為經新方法取值，行為不變；紅線 5）

```python
    def clearinghouse_state(self, address: str) -> dict:
        """完整 clearinghouseState（唯讀、冪等 → transient 重試）。
        M3 watchlist 快照用；get_account_value 亦取道此處（單一查詢來源）。"""
        return self._info({"type": "clearinghouseState", "user": address},
                          "HL 帳戶查詢")

    def get_account_value(self, address: str) -> Decimal:
        return Decimal(self.clearinghouse_state(address)["marginSummary"]["accountValue"])
```

（取代原本的 `get_account_value`；`what` 標籤沿用「HL 帳戶查詢」不變，log 面無漂移。）

- [ ] **Step 5: 實作 — leaderboard.py**（Create `src/spark/filet/leaderboard.py`）

```python
"""src/spark/filet/leaderboard.py
Leader watchlist 每日快照（M3 leader 選人資料）。與 scripts/leaderboard_snapshot.py
的**全站 top-N** 快照互補：那份出自 stats-data 排行榜端點（未進官方文件），
這份是關注清單逐錢包的官方 /info clearinghouseState。落檔目錄也分開
（<data_dir>/leaderboard/ vs <data_dir>/leaderboard/watchlist/，定案 6）。

純函式 + 注入 state_fn（HLGateway.clearinghouse_state；transient 重試在 gateway
的 resilience 邊界，這裡不再重試）。不觸網；落檔只在 write_snapshot。

PnL 極限註記（工程原則 1）：日 PnL 由 account_value 日序列差分近似——出入金會混入
差分。快照存原始欄位（accountValue/totalMarginUsed/totalNtlPos/withdrawable/
unrealizedPnl 合計），衍生計算留給 M3 分析端做並自行對帳。"""
import json
import logging
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_WATCHLIST = ("0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1",)  # M1 leader


def snapshot_watchlist(state_fn: Callable[[str], dict], addresses: list[str] | tuple,
                       day: date) -> dict[str, Any]:
    """逐地址查 clearinghouseState → 正規化快照 dict（Decimal 過手後以 str 落地）。
    單一地址失敗：大聲隔離（logger.error + error 條目 + error_count），不弄丟整批
    （工程原則 3——「大聲」= 快照內可見 + log + CLI exit code，見 watchlist_snapshot）。"""
    rows: list[dict[str, Any]] = []
    errors = 0
    for addr in addresses:
        try:
            state = state_fn(addr)
            ms = state["marginSummary"]
            positions = state.get("assetPositions", [])
            upnl = sum((Decimal(str(p["position"]["unrealizedPnl"])) for p in positions),
                       Decimal("0"))
            rows.append({
                "address": addr,
                "account_value": str(Decimal(str(ms["accountValue"]))),
                "total_margin_used": str(Decimal(str(ms["totalMarginUsed"]))),
                "total_ntl_pos": str(Decimal(str(ms["totalNtlPos"]))),
                "withdrawable": str(Decimal(str(state["withdrawable"]))),
                "unrealized_pnl": str(upnl),
                "position_count": len(positions),
            })
        except Exception as e:  # noqa: BLE001 — 逐地址隔離；計數上報，絕不靜默
            errors += 1
            logger.error("watchlist 快照 %s 失敗: %s", addr, e)
            rows.append({"address": addr, "error": f"{type(e).__name__}: {e}"})
    return {
        "day": day.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "clearinghouseState",
        "row_count": len(rows) - errors,
        "error_count": errors,
        "rows": rows,
    }


def write_snapshot(out_dir: str | Path, snapshot: dict) -> Path:
    """原子寫 <out_dir>/<day>.json：同目錄 tmp + os.replace（同檔系統原子）；
    同日重跑覆寫同檔＝冪等（檔名 = day）。cron 中途被殺不留半寫檔。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{snapshot['day']}.json"
    tmp_path = out_dir / f".{snapshot['day']}.json.tmp"
    tmp_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp_path, out_path)
    return out_path
```

（tmp 檔名刻意用點開頭 `.{day}.json.tmp`：即使進程被殺殘留，也不會被讀快照的
`glob("*.json")` 類消費者誤讀成快照。）

- [ ] **Step 6: 跑測試確認通過**（含既有 hl 測試不破）

```bash
uv run pytest tests/test_publicapi_hl.py tests/test_filet_leaderboard.py tests/test_api_onboard.py -q && uv run ruff check src tests
```
Expected: 全 PASS。

- [ ] **Step 7: Commit**

```bash
git add src/spark/publicapi/hl.py src/spark/filet/leaderboard.py \
    tests/test_publicapi_hl.py tests/test_filet_leaderboard.py
git commit -m "feat: leader watchlist snapshot module + HLGateway.clearinghouse_state"
```

---

### Task 8: `scripts/watchlist_snapshot.py` CLI + 既有全站腳本 FILET_DATA_DIR 支援

**Files:**
- Create: `scripts/watchlist_snapshot.py`
- Modify: `scripts/leaderboard_snapshot.py`（只動 `main()` 的 out_dir 預設，additive）
- Test: `tests/test_watchlist_snapshot.py`（Create）、`tests/test_leaderboard_snapshot.py`（追加一測）

- [ ] **Step 1: 失敗測試**（Create `tests/test_watchlist_snapshot.py`）

```python
"""tests/test_watchlist_snapshot.py — watchlist 快照 CLI（state_fn 注入，不觸網）。"""
import json
from datetime import date

import pytest

from scripts.watchlist_snapshot import main, parse_watchlist
from spark.filet.leaderboard import DEFAULT_WATCHLIST

STATE = {"marginSummary": {"accountValue": "10", "totalMarginUsed": "1",
                           "totalNtlPos": "5"},
         "withdrawable": "4", "assetPositions": []}


def test_parse_watchlist_default():
    assert parse_watchlist(None) == list(DEFAULT_WATCHLIST)
    assert parse_watchlist("") == list(DEFAULT_WATCHLIST)


def test_parse_watchlist_normalizes_and_dedupes():
    a = "0x" + "AB" * 20
    b = "0x" + "cd" * 20
    got = parse_watchlist(f" {a} , {b}, {a.lower()} ")
    assert got == [a.lower(), b]  # 小寫、去空白、去重保序


def test_parse_watchlist_rejects_bad_address():
    """格式錯大聲整批失敗（工程原則 3）——寧可 cron 告警也不靜默漏 leader。"""
    with pytest.raises(ValueError):
        parse_watchlist("0x123,not-an-address")


def test_main_writes_snapshot_and_exits_0(tmp_path, capsys):
    env = {"FILET_LEADER_WATCHLIST": "0x" + "ab" * 20,
           "FILET_DATA_DIR": str(tmp_path)}
    with pytest.raises(SystemExit) as ei:
        main(state_fn=lambda a: STATE, today=date(2026, 7, 18), env=env)
    assert ei.value.code == 0
    out = tmp_path / "leaderboard" / "watchlist" / "2026-07-18.json"  # 定案 6/8 路徑
    data = json.loads(out.read_text())
    assert data["rows"][0]["address"] == "0x" + "ab" * 20
    assert "2026-07-18.json" in capsys.readouterr().err


def test_main_exit_1_when_any_address_fails(tmp_path):
    """定案 10：error_count > 0 → exit 1（systemd 顯示 failed），快照檔仍已寫出。"""
    def state_fn(addr):
        raise ConnectionError("down")

    env = {"FILET_LEADER_WATCHLIST": "0x" + "ab" * 20, "FILET_DATA_DIR": str(tmp_path)}
    with pytest.raises(SystemExit) as ei:
        main(state_fn=state_fn, today=date(2026, 7, 18), env=env)
    assert ei.value.code == 1
    data = json.loads((tmp_path / "leaderboard" / "watchlist" / "2026-07-18.json").read_text())
    assert data["error_count"] == 1


def test_main_idempotent_same_day_overwrite(tmp_path):
    env = {"FILET_LEADER_WATCHLIST": "0x" + "ab" * 20, "FILET_DATA_DIR": str(tmp_path)}
    for _ in range(2):
        with pytest.raises(SystemExit):
            main(state_fn=lambda a: STATE, today=date(2026, 7, 18), env=env)
    files = list((tmp_path / "leaderboard" / "watchlist").glob("*"))
    assert [f.name for f in files] == ["2026-07-18.json"]
```

追加到 `tests/test_leaderboard_snapshot.py` 檔尾（先讀該檔沿用其注入慣例）：

```python
def test_main_out_dir_honors_filet_data_dir(tmp_path, monkeypatch):
    """M3 additive：FILET_DATA_DIR 設定時全站快照落 <dir>/leaderboard/（unset 行為不變）。"""
    monkeypatch.setenv("FILET_DATA_DIR", str(tmp_path))
    from scripts.leaderboard_snapshot import main
    rows = [{"ethAddress": "0x" + "ab" * 20, "accountValue": "1",
             "windowPerformances": [["day", {"pnl": "2"}]]}]
    import pytest as _pytest
    from datetime import date as _date
    with _pytest.raises(SystemExit):
        main(fetch_fn=lambda network: rows, today=_date(2026, 7, 18))
    assert (tmp_path / "leaderboard" / "2026-07-18.json").exists()
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
uv run pytest tests/test_watchlist_snapshot.py tests/test_leaderboard_snapshot.py -q
```
Expected: 新測試 FAIL（module 不存在；FILET_DATA_DIR 未支援）。

- [ ] **Step 3: 實作 — 新 CLI**（Create `scripts/watchlist_snapshot.py`）

```python
"""scripts/watchlist_snapshot.py
每日 leader watchlist 快照 CLI —— 為 M3 leader 選人累積逐錢包日序列（≥2 個月）。
與 scripts/leaderboard_snapshot.py（全站 top-N，stats-data）互補；本腳本走官方
/info clearinghouseState（經 HLGateway 唯讀 resilience 邊界，transient 自動重試）。

用法（systemd timer 每日執行，見 deploy/filet-leaderboard.timer）：
  FILET_DATA_DIR=/var/lib/filet-api [FILET_LEADER_WATCHLIST=0x..,0x..] \\
  [SPARK_NETWORK=mainnet] uv run python -m scripts.watchlist_snapshot

環境變數:
  FILET_LEADER_WATCHLIST  逗號分隔 leader 地址；未設時用內建預設（M1 leader）
  FILET_DATA_DIR          資料根目錄（預設 var/filet）；落檔 <root>/leaderboard/watchlist/<day>.json
  SPARK_NETWORK           mainnet | testnet（預設 mainnet）

行為:
  - import 階段零網路（HLGateway 只在 main() 內建）；測試注入 state_fn。
  - 同日重跑覆寫同檔（冪等）；原子寫（tmp + os.replace）。
  - 逐地址失敗隔離：error 條目寫進快照；error_count > 0 → exit 1
    （systemd unit 顯示 failed = 大聲告警；快照檔仍已寫出，不丟資料）。
"""
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from spark.filet.leaderboard import DEFAULT_WATCHLIST, snapshot_watchlist, write_snapshot
from spark.publicapi.config import normalize_address


def parse_watchlist(raw: str | None) -> list[str]:
    """逗號分隔地址 → normalize（小寫）、去空白、去重保序。空/未設 → 預設清單。
    格式錯 → ValueError 整批失敗（工程原則 3：寧可 cron 告警也不靜默漏 leader）。"""
    if not raw or not raw.strip():
        return list(DEFAULT_WATCHLIST)
    seen: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        addr = normalize_address(part)  # 壞地址在此大聲 ValueError
        if addr not in seen:
            seen.append(addr)
    if not seen:
        return list(DEFAULT_WATCHLIST)
    return seen


def main(state_fn=None, out_dir=None, today: date | None = None, env=None) -> None:
    """CLI 入口。state_fn/out_dir/today/env 皆可注入（測試不觸網）。"""
    env = os.environ if env is None else env
    addresses = parse_watchlist(env.get("FILET_LEADER_WATCHLIST"))
    day = today or datetime.now(timezone.utc).date()
    if out_dir is None:
        out_dir = Path(env.get("FILET_DATA_DIR", "var/filet")) / "leaderboard" / "watchlist"
    if state_fn is None:  # 延後 import + 延後建線：import 階段零網路
        from spark.config import API_URLS
        from spark.publicapi.hl import HLGateway
        network = env.get("SPARK_NETWORK", "mainnet")
        if network not in API_URLS:
            raise SystemExit(f"unknown SPARK_NETWORK: {network!r}")
        state_fn = HLGateway(API_URLS[network]).clearinghouse_state

    snapshot = snapshot_watchlist(state_fn, addresses, day)
    out_path = write_snapshot(out_dir, snapshot)
    print(f"[watchlist_snapshot] day={snapshot['day']} ok={snapshot['row_count']} "
          f"errors={snapshot['error_count']} -> {out_path}", file=sys.stderr)
    raise SystemExit(0 if snapshot["error_count"] == 0 else 1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 實作 — 既有腳本 additive 修改**（`scripts/leaderboard_snapshot.py`）

`main()` 簽名與開頭改為（其餘不動；`DEFAULT_OUT_DIR` 常數保留）：

```python
def main(
    fetch_fn: Callable[[str], list[dict]] = fetch_leaderboard,
    out_dir: str | Path | None = None,
    network: str | None = None,
    today: date | None = None,
) -> None:
    """CLI 入口。fetch_fn/out_dir/today 皆可注入（測試用；不觸網）。
    out_dir 未指定時：FILET_DATA_DIR 設定 → <FILET_DATA_DIR>/leaderboard（M3 systemd
    共用資料根）；未設 → DEFAULT_OUT_DIR（行為與 M2 完全相同）。"""
    network = network or os.environ.get("SPARK_NETWORK", "mainnet")
    day = today or datetime.now(timezone.utc).date()
    if out_dir is None:
        data_dir = os.environ.get("FILET_DATA_DIR")
        out_dir = (Path(data_dir) / "leaderboard") if data_dir else DEFAULT_OUT_DIR
```

- [ ] **Step 5: 跑測試確認通過**

```bash
uv run pytest tests/test_watchlist_snapshot.py tests/test_leaderboard_snapshot.py \
    tests/test_scripts_import.py -q && uv run ruff check src tests scripts
```
Expected: 全 PASS（含 scripts import 面）。

- [ ] **Step 6: Commit**

```bash
git add scripts/watchlist_snapshot.py scripts/leaderboard_snapshot.py \
    tests/test_watchlist_snapshot.py tests/test_leaderboard_snapshot.py
git commit -m "feat: watchlist snapshot CLI + FILET_DATA_DIR support in leaderboard snapshot"
```

---

### Task 9: systemd timer + service

**Files:**
- Create: `deploy/filet-leaderboard.service`
- Create: `deploy/filet-leaderboard.timer`

- [ ] **Step 1: service**（Create `deploy/filet-leaderboard.service`；沿 `deploy/filet-api.service` 的 hardening 慣例）

```ini
[Unit]
Description=Filet daily leaderboard snapshots (global top-N + leader watchlist, read-only)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=filet-api
Group=filet-api
WorkingDirectory=/opt/filet/spark
Environment=SPARK_NETWORK=mainnet
Environment=FILET_DATA_DIR=/var/lib/filet-api
# watchlist 未設時用內建預設（M1 leader 0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1）。
# 擴充清單（逗號分隔）：
#Environment=FILET_LEADER_WATCHLIST=0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1,0x...
# 全站快照 best-effort（ExecStart 前綴 "-"）：stats-data 端點未進官方文件、schema
# 可能無預告變動——它掛掉不擋 watchlist 快照（定案 11）。
ExecStart=-/opt/filet/spark/.venv/bin/python -m scripts.leaderboard_snapshot
ExecStart=/opt/filet/spark/.venv/bin/python -m scripts.watchlist_snapshot
StateDirectory=filet-api
ReadWritePaths=/var/lib/filet-api
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: timer**（Create `deploy/filet-leaderboard.timer`）

```ini
[Unit]
Description=Daily filet leaderboard snapshots at 00:10 UTC

[Timer]
# 00:10 UTC：日界後緩衝 10 分鐘（沿 scripts/leaderboard_snapshot.py docstring 的
# crontab 慣例）；RandomizedDelay 避免整點雪崩。Persistent：關機錯過補跑。
OnCalendar=*-*-* 00:10:00 UTC
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: 驗證**（macOS 無 systemd——做語法級自檢：兩檔互相引用的路徑/名字一致、ExecStart 的 module 名真實存在）

```bash
uv run python -c "import scripts.leaderboard_snapshot, scripts.watchlist_snapshot; print('modules OK')"
grep -c "ExecStart" deploy/filet-leaderboard.service   # 應為 2
```
Expected: `modules OK`、`2`。實機 `systemctl enable --now filet-leaderboard.timer` 驗收移交部署計畫（同 M2 慣例）。

- [ ] **Step 4: Commit**

```bash
git add deploy/filet-leaderboard.service deploy/filet-leaderboard.timer
git commit -m "feat: systemd timer for daily leaderboard + watchlist snapshots"
```

---

### Task 10: 全量整合驗證 ⭐（最後 review gate：opus 總審）

**Files:** 無新檔（驗證與審查任務）。

- [ ] **Step 1: 全量測試與 lint**

```bash
uv run pytest -q
uv run ruff check src tests scripts
```
Expected: 0 failed（基線 662 + 本計畫新增約 45-55 測）；ruff 乾淨。記下實際數字供執行狀態表。

- [ ] **Step 2: 紅線逐條自查**（實作側先過一遍，證據留給 opus）

```bash
# 紅線 1：非 sk_test_ 拒啟動（含直接建構路徑）
uv run pytest tests/test_publicapi_config.py -q -k "live_key or partial"
# 紅線 2：驗簽正反例 + 不碰 DB
uv run pytest tests/test_api_billing.py -q -k "webhook"
# 紅線 3：隔離
uv run pytest tests/test_api_billing.py -q -k "disabled or unaffected"
# 紅線 5：M2 既有測試零修改——diff 佐證
git diff feat/m2-publicapi..HEAD --stat -- tests/ | cat
```
Expected: 全 PASS；tests/ diff 只有「追加」與本計畫新檔（`publicapi_helpers.py` 的 additive 參數與新 helper 除外，無任何既有斷言變動）。

- [ ] **Step 3: 敏感字串掃描**

```bash
grep -rn "sk_live_" src scripts deploy && echo "FAIL: 出現 sk_live_" || echo "OK"
grep -rn "sk_test_[A-Za-z0-9]\{8,\}" src scripts deploy && echo "FAIL: 疑似真測試 key 落檔" || echo "OK"
```
Expected: 兩個 OK（deploy 註解裡的 `sk_test_REPLACE` 不足 8 位英數不會誤中；若誤中則以人眼確認是佔位符）。

- [ ] **Step 4: opus 總審**（fresh context，只給驗收條件不給實作推理）

審查 prompt 要點：diff 範圍 `git diff feat/m2-publicapi..HEAD`；按序盯——
1. ⭐ webhook 驗簽路徑：伪造/重放/竄改/缺 header 是否全被 400 擋下且不碰 DB；auth 豁免的安全論證是否成立。
2. ⭐ sk_test_ 強制是否結構性（`__post_init__`，任何建構路徑都擋）；secret 是否可能進 log/repr/例外。
3. ⭐ billing 表無敏感資料；billing 未設時 onboarding 行為與 M2 位元級相同。
4. 工程原則逐條：checkout 非冪等不盲重試；webhook 重放冪等 + event.created 單調守衛防亂序（舊 active 不復活已取消訂閱——跑 `-k "out_of_order or monotonic"` 看證據）；快照逐地址失敗大聲隔離；比較同源同基準。
5. 專案紅線：`ExchangeAdapter` 未被碰；無 withdraw/transfer；hl-copytrader 未讀寫。
Expected: APPROVED 或列出必修項；必修項修完複審後才算完成。

- [ ] **Step 5: 更新本計畫檔頂部「執行狀態」節**（照 M2 慣例：表格列 task/commit/審查結果），commit：

```bash
git add docs/superpowers/plans/2026-07-18-m3-billing-leaderboard.md
git commit -m "docs: M3 billing+leaderboard plan execution status"
```

---

## M3 規劃對照 + 刻意不做清單

### 上游要求 → 本計畫落點

| 上游要求 | 落點 |
|---|---|
| 定價 A/B/C 未拍板 → 基礎設施「定價無關」 | `FILET_STRIPE_PRICE_ID` 參數化（Task 2）；拍板後只填 env 不改碼 |
| 律師條款是收費前置（M0 未結案）→ 絕不真實收費 | `sk_test_` 前綴 `__post_init__` 結構性強制（Task 2 ⭐）+ 測試斷言 + deploy 註解警語 |
| Stripe SDK 依賴、鎖版本 | Task 0（uv add 鎖 major + SDK 表面探針）|
| billing 全 optional、未設回 501、不影響 onboarding | Task 5（`_require_billing` + 隔離測試）|
| store billing 表（無敏感資料）| Task 1（additive migration + 欄位白名單結構性斷言 ⭐）|
| `POST /api/billing/checkout`（session 綁定、已 active 409）| Task 5 |
| `GET /api/billing/status`（讀 DB）| Task 5 |
| `POST /api/billing/webhook`（驗簽必過、三事件、豁免標注）| Task 4 ⭐ |
| `has_active_subscription` 只查不接停用 | Task 3（紅線 6）|
| 測試全離線、mock stripe、真 HMAC 驗簽案例 | Task 3/4（monkeypatch `stripe.checkout.Session.create`；`construct_event` 本地 HMAC 正反例）|
| watchlist env（預設含 M1 leader）每日 snapshot | Task 7/8（`FILET_LEADER_WATCHLIST`、`DEFAULT_WATCHLIST`）|
| HL /info 唯讀查詢（clearinghouseState 實際可得欄位）| Task 7（`HLGateway.clearinghouse_state`，經既有 resilience 邊界）|
| 落檔 `FILET_DATA_DIR/leaderboard/YYYY-MM-DD.json`、原子、冪等 | Task 7/8——**偏差：watchlist 落 `leaderboard/watchlist/` 子目錄**（定案 6：`leaderboard/` 根已被 2026-07-17 既有全站快照佔用，檔名同為 `<day>.json` 會互撞）|
| systemd timer（每日、filet-api user）| Task 9（timer + oneshot service，順帶把既有全站快照從 docstring 裡的 crontab 建議升級進同一 timer，best-effort）|
| 指揮官 prompt 的 `scripts/snapshot_leaderboard.py` 檔名 | **偏差：改名 `scripts/watchlist_snapshot.py`**（定案 7：repo 已有 `leaderboard_snapshot.py`，鏡像顛倒名是誤用坑）|

### 刻意不做（本計畫範圍外，動之前問使用者）

1. **真實收費開關**：無任何 `sk_live_` 路徑；解除 `sk_test_` 強制 = 修改紅線，必問使用者（且以 M0 律師條款結案為前置）。
2. **定價選擇（A/B/C）與免費層邏輯**：等使用者拍板；骨幹已定價無關。
3. **自動停用跟單**：`has_active_subscription` 只查詢；billing status 與引擎 manifest/followers 零接線。
4. **退款流、發票、稅務、金額顯示**：不做；DB 刻意無金額欄位。
5. **前端 billing UI**：卡 Node；`{siwe_uri}/billing` 路由與 checkout 跳轉留前端計畫。
6. **Stripe Customer Portal / 取消訂閱端點**：測試模式驗證期不需要；正式化時隨定價計畫一起做。
7. **checkout 並發嚴格鎖**（同帳號同時兩個 session）：測試模式無真錢；正式收費計畫再上（定案 12 已留 customer 重用緩解）。
8. **webhook 事件持久化／重放日誌表**：目前三事件直接 upsert 即冪等；需要審計軌跡時再加（additive）。
9. **leaderboard 快照的分析/選人邏輯**：本計畫只累積原始資料；PnL 差分、剔除出入金、排名邏輯是 M3 分析側。
10. **實機 systemd 驗收**：macOS 開發機無 systemd；`systemctl enable` 與實跑驗收移交部署計畫（沿 M2 慣例）。
11. **billing DB↔Stripe reconcile**（opus Finding 4 誠實標註）：webhook 掉包（Stripe 重送耗盡）後 DB 永久漂移，本計畫**無對帳**——`has_active_subscription` 是 **DB-only 語意**，不是 Stripe 即時真相；定期對 Stripe API 對帳（reconcile）移交正式收費前的計畫。過渡期的告警面暫由 Stripe dashboard 的 webhook 失敗通知代位。
12. **`checkout.session.completed` 的 payment_status 檢查**（opus Finding 3）：test-mode 驗證期接受未檢（收到已驗簽 completed 即開通，code 有註記）；正式收費前與 reconcile 計畫（第 11 條）一併收。
