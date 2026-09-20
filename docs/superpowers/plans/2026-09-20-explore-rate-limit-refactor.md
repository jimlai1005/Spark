# Explore 限流／持久化重構 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 Explore 排行榜只讀本地資料、由背景 scheduler 在同 IP 共用權重預算內分欄位更新並持久化，成交同步可續跑且揭露完整性；同時修掉本次事故暴露的 dashboard 四個小洞。

**Architecture:** filet-api 是單 worker uvicorn 進程，所有互動流量（dashboard／onboard／traders 詳情／explore）都經 `HLGateway`；在這一層加一個進程內滑動視窗權重限流器（全域 900、explore 子預算 300），並讓 Explore 走獨立 scope。Explore 的資料改存 SQLite（`explore.db`，WAL）：候選、每地址每 endpoint 快取、原始 fills、同步游標、工作佇列；一條進程內背景 thread 依到期時間與優先級逐 job 執行（一個 job＝一次 HL 呼叫或一頁 fills），publisher 每分鐘合併成相容的 `ExploreRow` 原子換版並落快照。follower 引擎與四個 timer 不在本進程，**不接入限流**（全域 900 留 300 給它們），plan 內明列。

**Tech Stack:** Python 3.11、FastAPI、httpx、sqlite3（stdlib，WAL）、threading；測試 pytest（autouse socket-ban）＋ FakeHL／fake clock；前端 Next.js＋vitest。

**Spec:** `docs/superpowers/specs/2026-09-20-hl-leaderboard-refactor-spec.md`（使用者提供，§ 編號以下沿用）。

---

## 0. 盤點結果（spec §2，已由主線程複核）

| 項目 | 事實 | 依據 |
|---|---|---|
| Explore 入口 | `GET /api/public/explore` → `ExploreIndex.query()` → `_maybe_trigger_build()` 開 daemon thread 跑 `build_sync()`；TTL 10 分鐘；single-flight 只在進程內 | `src/spark/publicapi/hl_explore.py:1056-1110`、`app.py:2304-2347` |
| 候選池 | `LeaderboardCache`（stats-data，36MB，TTL 600s，進程內、不落盤）月 ROI 前 300，排除精選白名單 | `hl_leaderboard.py:30-32`、`hl_explore.py:829-841` |
| enrich 成本 | 每地址 `portfolio`(20) + `userFillsByTime` ≤3 頁(每頁 20＋筆數/20，滿頁 120) + `clearinghouseState`(2)；0.7s 間隔；429 退避 2/8/30s；實測 300 池 98 分鐘、每輪 100+ 次 429 | `hl_explore.py:909-994`、正式機 journal 2026-09-19 |
| 快取缺陷 | enrich LRU 256 < 池 300 → 每輪全冷建；TTL 30 分鐘 < 建置時間 | `hl_explore.py:226,255-256` |
| 快照 | `/var/lib/filet-api/explore_index.json`（`FILET_EXPLORE_CACHE_PATH`），只存 rows／built_at／total_scanned／version=3 | `hl_explore.py:1049-1054`、`config.py:337` |
| 打 HL 的進程 | filet-api（HLGateway，httpx，`resilience.run` 3 次重試，429 視為語意錯不重試）；filet-follower@（SDK，獨立進程）；timer：leaderboard／perf-series／daily-report（`scripts/*_snapshot.py`，各自 import HLGateway，每日 00:00–00:25 UTC 跑一次） | `hl.py:99-129`、`resilience.py:28-70`、`deploy/*.service` |
| 進程拓撲 | filet-api 單 worker（`scripts/run_api.py:30-33`，無 `--workers`）；無 Redis／Postgres；SQLite 已用於 `api.db`（`store.py:95`，無 WAL）；`safe_fs.write_json_atomic` 可用 | scout A，主線程複核 |
| 詳情頁 | `/api/traders/{address}` 走 `_cached_trader_data`（5 分鐘 TTL，256 LRU）：`portfolio`+`clearinghouse_state`+`non_funding_ledger_updates`+`get_fills_raw_paged`，冷讀約 400 權重 | `app.py:2399-2484` |
| 前端消費 | `web/src/lib/publicApi.ts:524-556` 讀 `rows/page/page_size/total_qualified/total_scanned/pool/updated_at/building/sort/order`；row 用 `windows[w]`、`coins`、`live_days`、`order_count_30d`、`close_win_rate_pct`、`exposure_*`、`tags`、`fills_truncated` | scout B，主線程抽查 |
| dex／spot／subaccount | 無任何 dex 參數；explore 只算 perp fills（`PERP_DIRS`） | `trader_stats.py:136-173` |
| create_app | 無 lifespan／startup 掛鉤；`hl`、`now_fn` 以參數注入；測試用 `tests/test_public_explore.py:1021 _app()` 建 app（74 個測試） | `app.py:1335` |

未接入共享預算的呼叫來源（spec §5.2「如實標明」）：`filet-follower@*`（SDK Info，每 cycle 少量 weight-2／20 呼叫）、`filet-leaderboard`／`filet-perf-series`／`filet-daily-report` timer（每日一次、UTC 00:00–00:25）。全域上限設 900 的 300 餘量就是給它們的；plan 不改引擎與 timer。

## 1. 裁決點（使用者確認後才派工；裁決結果回寫此表）

| # | 題目 | 建議 | 狀態 |
|---|---|---|---|
| D1 | 限流範圍：只做 filet-api 進程內（涵蓋 dashboard／onboard／traders／explore），引擎與 timer 不接入、以 900 上限留餘量 | 採用；跨進程檔案鎖限流留待有第二個高流量進程時再做 | 2026-09-20 使用者同意 |
| D2 | 執行載體：scheduler／worker 為 filet-api 進程內背景 thread（單 worker），lease 欄位仍持久化供重啟接續；不另開 systemd unit | 採用 | 2026-09-20 使用者同意 |
| D3 | 持久化：新檔 `/var/lib/filet-api/explore.db`（SQLite WAL，`FILET_EXPLORE_DB`），不塞進 `api.db` | 採用 | 2026-09-20 使用者同意 |
| D4 | 詳情頁 `/api/traders/{address}`：池內地址讀本地＋入列刷新；池外地址維持同步抓取但經 interactive 限流 | 採用 | 2026-09-20 使用者同意 |
| D5 | API 對外新增 `fills_coverage {state: backfilling/partial/complete, observed_from, observed_to, reason}` 與 `as_of {portfolio, state, fills}`；前端本輪只把 `fills_truncated` 提示改讀 `fills_coverage.state != "complete"`，不做深度 UI | 採用 | 2026-09-20 使用者同意 |
| D6 | 舊 `build_sync`／0.7s 節流／2/8/30 退避在 P3 完成後刪除，測試改寫 | 採用 | 2026-09-20 使用者同意 |
| D7 | 遷移：舊 `explore_index.json` 的 rows 匯入為首版 published，`as_of` 全部＝`built_at`，`fills_coverage.state="backfilling"`；不推導 fills 與 endpoint cache | 採用 | 2026-09-20 使用者同意 |
| D8 | 部署節奏：P0＋P1 完成即先部署一次（開探索頁不再燒 90 分鐘；dashboard 有限流保護），P2–P5 完成再部署第二次。`EXPLORE_UPSTREAM_REFRESH` 預設 off，第二次部署後觀察 `/api/ops/health.hl_budget` 再開 | 採用（spec §0.1 說不必分階段上線，但正式機現在每次開頁就滿載 90 分鐘，先止血代價最低） | 2026-09-20 使用者同意 |
| D9 | interactive scope 等額度上限 2 秒；等不到 → `BudgetExhausted`（非 transient，不重試）→ dashboard 該塊 null、其他端點經全域 handler 回 502（2026-09-20 與 Task 0.4 統一） | 採用 | 2026-09-20 使用者同意 |

---

## 2. 檔案結構

| 檔案 | 責任 | 階段 |
|---|---|---|
| `src/spark/publicapi/hl_budget.py`（新） | `WeightLimiter`：滑動視窗權重帳、scope 子預算、429 暫停、快照 | P1 |
| `src/spark/publicapi/hl.py`（改） | `HLGateway` 接 limiter：每次嘗試先預留、429 回報、`scoped()` | P1 |
| `src/spark/publicapi/hl_explore.py`（改） | P0 拆掉請求觸發；P1 `_call_hl` 改走 limiter；P3/P4 只留 `ExploreRow`／`qualify`／`sort_rows`／`_apply_tags`／`query()`／snapshot | P0–P4 |
| `src/spark/publicapi/explore_store.py`（新） | SQLite schema、migration、candidate／endpoint_cache／fills／fills_sync／refresh_job 的 CRUD（純資料層，無網路） | P2 |
| `src/spark/publicapi/explore_fills_sync.py`（新） | 單頁 fills 同步演算法（純函式＋store 寫入）、完整性判定、no-progress guard | P2 |
| `src/spark/publicapi/explore_scheduler.py`（新） | 到期計算、job 入列去重、優先級、單 thread worker loop、lease | P3 |
| `src/spark/publicapi/explore_publisher.py`（新） | 從 store 合成 `ExploreRow`＋metadata，原子換版、寫快照 v4、舊 v3 匯入 | P4 |
| `src/spark/publicapi/config.py`（改） | 新環境變數 | P1/P2 |
| `src/spark/publicapi/app.py`（改） | 接線、`/api/ops/health` 加 `hl_budget`／`explore_refresh`、dashboard 四個小修、traders 詳情改讀本地 | P0–P4 |
| `scripts/run_api.py`（改） | 建 limiter、store、scheduler 並注入 | P1/P3 |
| `web/src/lib/copy.ts`（改） | 文案 | P0 |
| `deploy/RUNBOOK.md`、`deploy/filet-api.service.d/`（改） | 新 env、啟停刷新、回退步驟 | P5 |
| `tests/test_hl_budget.py`、`tests/test_explore_store.py`、`tests/test_explore_fills_sync.py`、`tests/test_explore_scheduler.py`、`tests/test_explore_publisher.py`（新）；`tests/test_public_explore.py`、`tests/test_me_dashboard.py`、`tests/test_dashboard_sync.py`（改） | 測試 | 各階段 |

---

## P0 止血＋dashboard 小修（全部完成後一起 commit 也可，每 task 各自可驗）

### Task 0.1 @inline：`query()` 不再觸發重建

**Files:**
- Modify: `src/spark/publicapi/hl_explore.py:1079-1110`（`query()`），`hl_explore.py:1056-1077`（`_maybe_trigger_build` 保留供 P3 前的測試呼叫）
- Test: `tests/test_public_explore.py`

- [ ] **Step 1: 寫失敗測試**（加在 `tests/test_public_explore.py` 末尾；`_app`／`FakeHL` 沿用該檔既有 helper，`FakeHL` 需有呼叫計數器，若沒有就在 FakeHL 加 `self.calls: list[str]`，每個方法 append 自己的名字）

```python
def test_explore_get_never_calls_upstream_nor_builds(tmp_path):
    """spec §3 不變條件一：刷新 Explore 頁面不發 HL info、不開 rebuild。"""
    hl = FakeHL()
    client = TestClient(_app(tmp_path, hl=hl))
    for _ in range(1000):
        r = client.get("/api/public/explore")
        assert r.status_code == 200
    assert hl.calls == []
    idx = client.app.state.explore_index
    assert idx._building is False
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_public_explore.py::test_explore_get_never_calls_upstream_nor_builds -q`
Expected: FAIL（`hl.calls` 非空，或 `_building` 為 True）

- [ ] **Step 3: 改 `query()`**：刪掉函式開頭的 `self._maybe_trigger_build()` 那一行，docstring 首段改為：

```python
        """讀路徑：**只讀本地已建置版本，永不觸發上游**（2026-09-20 spec §3／§9.2：
        Explore 頁面刷新不得發 HL info、不得開 rebuild——2026-09-19 事故：請求觸發
        的 300 池重建把同 IP 額度燒到 429，dashboard／onboard 一起失效）。
        上游更新改由 explore_scheduler（P3）負責；本函式在那之前只會端出啟動時
        從磁碟快照載入的資料。
```

- [ ] **Step 4: 跑測試確認通過，並跑全檔看哪些舊測試依賴「查詢會觸發建置」**

Run: `uv run pytest tests/test_public_explore.py -q`
Expected: 新測試 PASS；依賴觸發的舊測試 FAIL——把它們改成直接呼叫 `idx.build_sync()`（同步）後再 `query()`，不要刪測試；`building: True` 語意改為「尚無任何可用版本」。

- [ ] **Step 5: Commit**

```bash
git add src/spark/publicapi/hl_explore.py tests/test_public_explore.py
git commit -m "fix: explore query 不再觸發上游重建（止血 2026-09-19 429 事故）"
```

### Task 0.2 @sdd：`signal_source_ok` 接受 `no_action`

**Files:**
- Modify: `src/spark/publicapi/app.py:720`
- Test: `tests/test_me_dashboard.py`

- [ ] **Step 1: 失敗測試**（沿用該檔 `_hb(...)` helper，`last_cycle="no_action"` 為預設值，見 `tests/test_me_dashboard.py:88`）

```python
def test_signal_source_ok_true_when_last_cycle_no_action(tmp_path):
    """引擎 `no_action`＝跑完沒事做（絕大多數 cycle，scripts/run_copytrade.py:828），
    是健康態；只認 "ok" 會讓面板幾乎永遠顯示「訊號來源狀態未知」（2026-09-19 事故）。"""
    client = _client_following(tmp_path, last_cycle="no_action")  # 沿用檔內建 following 態的 helper 名稱
    body = client.get("/api/me/dashboard").json()
    assert body["status"]["signal_source_ok"] is True
```

（helper 名稱以檔內既有為準：找建立「已活化＋心跳新鮮」場景的那個 fixture／函式，把 `last_cycle` 傳進去。）

- [ ] **Step 2: 跑，確認 FAIL（`is False`）**

- [ ] **Step 3: 改 `app.py:720`**

```python
    _HEALTHY_CYCLE_RESULTS = ("ok", "no_action")
    last_cycle_ok = (((hb.data or {}).get("last_cycle") or {}).get("result")
                     in _HEALTHY_CYCLE_RESULTS)
```

（`_HEALTHY_CYCLE_RESULTS` 放模組層常數，緊鄰 `_dashboard_status` 上方，附註解：`tripped` 不在內。）

- [ ] **Step 4: 跑 `uv run pytest tests/test_me_dashboard.py -q` 全綠**
- [ ] **Step 5: Commit** `fix: dashboard 訊號來源判定接受 no_action`

### Task 0.3 @sdd：sync 快取不快取 error 結果

**Files:**
- Modify: `src/spark/publicapi/app.py:824-841`
- Test: `tests/test_dashboard_sync.py`

- [ ] **Step 1: 失敗測試**

```python
def test_sync_cache_skips_error_results():
    """docstring 說只快取成功結果，但 data_state="error" 也被釘 60 秒，使「重試」鍵無效。"""
    cache: dict = {}
    calls = {"n": 0}
    def fake_compute(*a, **k):
        calls["n"] += 1
        return {"data_state": "error", "latency_median_ms": None}
    with patch("spark.publicapi.app._compute_dashboard_sync", fake_compute):
        app_mod._dashboard_sync(ref, hl, mine, hb, None, 1000.0, cache=cache)
        app_mod._dashboard_sync(ref, hl, mine, hb, None, 1001.0, cache=cache)
    assert calls["n"] == 2
    assert cache == {}
```

（`ref/hl/mine/hb` 用檔內既有的 fixture 建；`app_mod` 是 `spark.publicapi.app`。）

- [ ] **Step 2: 跑，FAIL（`calls == 1`）**
- [ ] **Step 3: 改 `_dashboard_sync`**

```python
    result = _compute_dashboard_sync(ref, hl, mine, hb, follower_positions, now_s)
    if result is not None and result.get("data_state") != "error":
        cache[ref.account_id] = (now_s, result)
    return result
```

並把 docstring 的「只快取成功結果」段改為「只快取 `data_state != "error"` 的結果；error 表示這個帳號自己的成交查詢失敗，下一次請求（或使用者按重試）要立刻重算」。

- [ ] **Step 4: 跑 `uv run pytest tests/test_dashboard_sync.py -q` 全綠**
- [ ] **Step 5: Commit** `fix: dashboard sync 快取不釘住 error 結果`

### Task 0.4 @inline：HL 4xx/5xx（含 429）統一轉 502，不再 500

<!-- 2026-09-20 裁決（builder 回報）：原版在 `_progress` 局部 try/except 轉 503 會撞既有
全域 handler（app.py:1363-1371 把 ConnectionError/TimeoutError 轉 502）與兩條 pinned 測試
（test_status_hl_down_502、test_perp_value_read_failure_is_502_not_a_zero_balance）。
改為沿單一邊界慣例（工程原則 5）：全域 handler 加 httpx.HTTPStatusError → 502，
覆蓋所有端點；P1 的 BudgetExhausted 也在同處加。 -->

**Files:**
- Modify: `src/spark/publicapi/app.py:1363-1371`（全域 exception handler 區）
- Test: `tests/test_api_onboard.py`

- [ ] **Step 1: 失敗測試**（沿該檔 `test_status_hl_down_502` 第 73-84 行的寫法）

```python
def test_status_hl_429_is_502_not_500(tmp_path, monkeypatch):
    """HL 回 429／5xx → httpx.HTTPStatusError；之前沒有任何 handler 接住 → 500 帶
    traceback（2026-09-19 事故）。與 ConnectionError/TimeoutError 同一個邊界、同 502。"""
    client, hl = ...  # 同 test_status_hl_down_502 的場景建立
    def _429(*a, **k):
        req = httpx.Request("POST", "https://x/info")
        raise httpx.HTTPStatusError("429", request=req, response=httpx.Response(429, request=req))
    monkeypatch.setattr(hl, "max_builder_fee", _429)
    r = client.get("/api/onboard/status")
    assert r.status_code == 502
    assert "429" in r.json()["detail"]

def test_status_programming_error_is_still_500(tmp_path, monkeypatch):
    """反面：非上游例外不得被吃成 502。"""
    monkeypatch.setattr(hl, "max_builder_fee", lambda *a, **k: (_ for _ in ()).throw(KeyError("x")))
    with pytest.raises(KeyError):          # TestClient 預設 raise_server_exceptions=True；若 _client 關閉了，改斷言 500
        client.get("/api/onboard/status")
```

- [ ] **Step 2: 跑，第一個 FAIL（500 或直接拋 HTTPStatusError）**
- [ ] **Step 3: 在 `app.py:1371` 之後加**

```python
    @app.exception_handler(httpx.HTTPStatusError)
    async def _hl_http_status(request, exc):
        # 429／5xx 等上游 HTTP 錯誤：resilience 邊界視 429 為語意錯不重試、直接上拋，
        # 之前沒有 handler → 500（2026-09-19 事故）。與上面兩個 handler 同一個邊界、同 502。
        code = getattr(getattr(exc, "response", None), "status_code", "?")
        logger.warning("HL 上游 HTTP %s: %s %s", code, request.method, request.url.path)
        return JSONResponse(status_code=502,
                            content={"detail": f"上游服務回應 HTTP {code}，請稍後重試"})
```

`app.py` 若未 `import httpx`，在檔頭 import 區加上。既有測試 `test_status_hl_down_502`／`test_perp_value_read_failure_is_502_not_a_zero_balance` **不動**。

- [ ] **Step 4: `uv run pytest tests/test_api_onboard.py tests/test_api_billing.py -q` 全綠**
- [ ] **Step 5: Commit** `fix: HL 上游 HTTP 錯誤（含 429）統一轉 502，不再 500`

### Task 0.5 @sdd：文案「引擎狀態讀取失敗」→「鏈上資料讀取失敗」

**Files:**
- Modify: `web/src/lib/copy.ts:1257`（COPY_ZH）與 COPY_EN 對稱鍵
- Test: `web/src/components/dashboard/SyncCard.test.tsx`（既有測試用 `c.errorLine` 取值，不需改）

- [ ] **Step 1**: ZH `errorLine: "鏈上資料讀取失敗"`；EN 對稱鍵改 `"On-chain data unavailable"`。
- [ ] **Step 2**: `export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH" && cd web && npm test -- SyncCard` 全綠。
- [ ] **Step 3: Commit** `fix: dashboard 同步卡錯誤文案改指向鏈上資料`

**P0 驗收（主線程親跑）：** `uv run pytest -q` 全綠；`uv run ruff check src tests scripts` 乾淨；`rg -n "_maybe_trigger_build\(\)" src/spark/publicapi/hl_explore.py` 只剩定義處、`query()` 內零命中。

---

## P1 權重准入

### Task 1.1 @inline：`WeightLimiter`

**Files:**
- Create: `src/spark/publicapi/hl_budget.py`
- Test: `tests/test_hl_budget.py`

- [ ] **Step 1: 失敗測試**

```python
from spark.publicapi.hl_budget import (WeightLimiter, BudgetExhausted, ScopePaused,
                                       weight_for, ENDPOINT_WEIGHTS)

class Clock:
    def __init__(self): self.t = 1000.0
    def now(self): return self.t
    def sleep(self, s): self.t += s

def _lim(**kw):
    c = Clock()
    return WeightLimiter(global_cap=900, scope_caps={"explore": 300},
                         now_fn=c.now, sleep_fn=c.sleep, rng=lambda: 0.0, **kw), c

def test_weight_table_matches_hl_docs():
    assert weight_for("clearinghouseState") == 2
    assert weight_for("portfolio") == 20
    assert weight_for("userFillsByTime") == 120
    assert weight_for("nonFundingLedgerUpdates") == 20
    assert weight_for("somethingUndocumented") == 20

def test_sliding_window_never_exceeds_global_cap():
    lim, c = _lim()
    for _ in range(45):
        assert lim.try_reserve(20, "interactive")
    assert not lim.try_reserve(20, "interactive")       # 900 用完
    c.t += 59.9
    assert not lim.try_reserve(20, "interactive")       # 還在視窗內
    c.t += 0.2
    assert lim.try_reserve(20, "interactive")           # 最早那筆滑出

def test_any_60s_window_bounded_by_cap():
    """任意 60 秒切片總和 ≤ 900（spec §5.1：不是 token bucket 的突發語意）。"""
    lim, c = _lim()
    ledger = []
    for step in range(600):
        c.t += 0.5
        if lim.try_reserve(20, "interactive"):
            ledger.append((c.t, 20))
    for t0, _ in ledger:
        assert sum(w for t, w in ledger if t0 < t <= t0 + 60) <= 900

def test_scope_cap_applies_and_global_still_applies():
    lim, c = _lim()
    for _ in range(15):
        assert lim.try_reserve(20, "explore")
    assert not lim.try_reserve(20, "explore")            # explore 300 用完
    assert lim.try_reserve(20, "interactive")            # 其他 scope 仍可用
    for _ in range(29):
        lim.try_reserve(20, "interactive")
    assert not lim.try_reserve(20, "interactive")        # 全域 900

def test_reserve_waits_then_raises():
    lim, c = _lim()
    for _ in range(45): lim.try_reserve(20, "interactive")
    with pytest.raises(BudgetExhausted):
        lim.reserve(20, "interactive", wait_s=2.0)
    assert c.t - 1000.0 == pytest.approx(2.0, abs=0.3)

def test_429_pauses_explore_scope_with_escalation_and_retry_after():
    lim, c = _lim()
    lim.note_429("explore")
    with pytest.raises(ScopePaused):
        lim.try_reserve(2, "explore")
    assert lim.snapshot()["paused_until"]["explore"] == pytest.approx(1060.0)
    c.t = 1100.0
    lim.note_429("explore", retry_after_s=90)           # 第二次：max(60,90)*2 = 180
    assert lim.snapshot()["paused_until"]["explore"] == pytest.approx(1280.0)
    c.t = 1300.0
    lim.note_ok("explore")
    assert lim.try_reserve(2, "explore")

def test_interactive_429_also_pauses_explore():
    lim, c = _lim()
    lim.note_429("interactive")
    with pytest.raises(ScopePaused):
        lim.try_reserve(2, "explore")
    assert lim.try_reserve(2, "interactive")            # interactive 自己不暫停（spec §5.2：不停必要查詢）

def test_snapshot_shape():
    lim, c = _lim()
    lim.try_reserve(20, "explore"); lim.try_reserve(2, "interactive")
    s = lim.snapshot()
    assert s["window_s"] == 60 and s["global_cap"] == 900
    assert s["used"] == {"explore": 20, "interactive": 2}
    assert s["counters"]["reservations"] == 2 and s["counters"]["rate_limited"] == 0
```

- [ ] **Step 2: 跑，FAIL（ImportError）**

Run: `uv run pytest tests/test_hl_budget.py -q`

- [ ] **Step 3: 實作 `src/spark/publicapi/hl_budget.py`**

```python
"""同出口 IP 的 Hyperliquid REST 權重限流器（spec §5，2026-09-20）。

事故背景（2026-09-19）：Explore 300 池重建以「每請求睡 0.7 秒」節流，但 HL 是
每 IP 每分鐘 1200 **權重**——fills 一頁 120、portfolio 20，等於拿 429 當節流器，
同 IP 的 dashboard／onboard 一起被打掉。本模組是**唯一**的權重帳本：
每次 HTTP 嘗試（含重試、分頁）發送前都要先 `reserve`。

演算法：滑動視窗帳本（deque 存 (ts, weight, scope)），每次預留時剪掉 60 秒前的
項目、檢查全域與 scope 上限。預留在發送前登記、不因完成而提前釋放（保守：
inflight 持續占額，完成後仍留在視窗內，spec §5.1）。任意 60 秒切片的總和 ≤ cap
——證明：任一切片 (t-60, t] 內的項目都包含於「最後一筆預留 t_last 時檢查過的
視窗 (t_last-60, t_last]」，該次檢查已保證 ≤ cap。

scope：`interactive`（dashboard／onboard／traders 詳情等使用者請求）與 `explore`
（背景更新）。`explore` 有子預算；任何 scope 觀察到 429 都會暫停 `explore`
（spec §5.2「其他服務觀察到 429 也應通知 Explore 暫停」），`interactive` 自己不暫停
（必要查詢不停，由呼叫端的既有失敗路徑處理）。

覆蓋範圍（誠實標註）：只有 filet-api 這個進程。follower 引擎與三個每日 timer 各自是
獨立進程、不經本模組；全域上限取 900 而非 1200 就是給它們留的餘量。
"""
from __future__ import annotations

import random
import threading
import time
from collections import Counter, deque

WINDOW_S = 60.0
DEFAULT_GLOBAL_CAP = 900
DEFAULT_SCOPE_CAPS: dict[str, int] = {"explore": 300}
PAUSE_MIN_S = 60.0
PAUSE_MAX_S = 900.0
PAUSE_JITTER_MAX_S = 5.0
DEFERRABLE_SCOPE = "explore"
INTERACTIVE_SCOPE = "interactive"
INTERACTIVE_WAIT_S = 2.0   # D9：使用者請求最多等 2 秒額度，等不到就走既有失敗路徑

# HL 官方權重（rate-limits 文件，2026-09-19 查閱）：clearinghouseState 等 2；
# 其餘 documented info 20；userRole 60；fills 類另按每 20 筆 +1——一頁 2000 筆＝
# 20 + 100，第一版每頁固定預留 120、不依實際筆數退款（spec §5.1）。
ENDPOINT_WEIGHTS: dict[str, int] = {
    "clearinghouseState": 2, "spotClearinghouseState": 2, "l2Book": 2,
    "allMids": 2, "orderStatus": 2, "exchangeStatus": 2,
    "userFillsByTime": 120, "userFills": 120,
    "userRole": 60,
}
DEFAULT_WEIGHT = 20


def weight_for(info_type: str) -> int:
    return ENDPOINT_WEIGHTS.get(info_type, DEFAULT_WEIGHT)


class BudgetExhausted(RuntimeError):
    """等候額度逾時。刻意**不是** ConnectionError/TimeoutError 子類：resilience 邊界
    會把它當語意錯誤直接上拋，不再重試（重試只會再等一次）。"""


class ScopePaused(RuntimeError):
    """該 scope 因 429 暫停中；`until` 為 now_fn 時基的解除時刻。"""

    def __init__(self, scope: str, until: float):
        super().__init__(f"scope {scope} paused until {until:.0f}")
        self.scope, self.until = scope, until


class WeightLimiter:
    def __init__(self, *, global_cap: int = DEFAULT_GLOBAL_CAP,
                 scope_caps: dict[str, int] | None = None,
                 now_fn=time.monotonic, sleep_fn=time.sleep, rng=random.random):
        self._global_cap = global_cap
        self._scope_caps = dict(DEFAULT_SCOPE_CAPS if scope_caps is None else scope_caps)
        self._now, self._sleep, self._rng = now_fn, sleep_fn, rng
        self._lock = threading.Lock()
        self._log: deque[tuple[float, int, str]] = deque()
        self._paused_until: dict[str, float] = {}
        self._consecutive_429: Counter[str] = Counter()
        self._counters: Counter[str] = Counter()

    # ---- 內部（呼叫端須持鎖） ----
    def _prune(self, now: float) -> None:
        cutoff = now - WINDOW_S
        while self._log and self._log[0][0] <= cutoff:
            self._log.popleft()

    def _used(self, scope: str | None = None) -> int:
        return sum(w for _, w, s in self._log if scope is None or s == scope)

    # ---- 公開 ----
    def try_reserve(self, weight: int, scope: str) -> bool:
        with self._lock:
            now = self._now()
            self._prune(now)
            until = self._paused_until.get(scope, 0.0)
            if until > now:
                raise ScopePaused(scope, until)
            if self._used() + weight > self._global_cap:
                self._counters["denied_global"] += 1
                return False
            cap = self._scope_caps.get(scope)
            if cap is not None and self._used(scope) + weight > cap:
                self._counters["denied_scope"] += 1
                return False
            self._log.append((now, weight, scope))
            self._counters["reservations"] += 1
            self._counters["reserved_weight"] += weight
            return True

    def reserve(self, weight: int, scope: str, *, wait_s: float = 0.0) -> None:
        """取得額度或拋 BudgetExhausted；最多等 wait_s 秒（0.25 秒輪詢）。
        取得後呼叫端必須**立即**發送（spec §5.1：不先扣額再排長隊）。"""
        deadline = self._now() + wait_s
        while True:
            if self.try_reserve(weight, scope):
                return
            remaining = deadline - self._now()
            if remaining <= 0:
                self._counters["exhausted"] += 1
                raise BudgetExhausted(f"hl budget exhausted for scope={scope} weight={weight}")
            self._sleep(min(0.25, remaining))

    def note_429(self, scope: str, retry_after_s: float | None = None) -> None:
        """任一 scope 的 429 → 暫停 explore；連續 429 指數延長至 PAUSE_MAX_S，
        加 0–5 秒正向 jitter；Retry-After 存在時取較長者。"""
        with self._lock:
            now = self._now()
            self._counters["rate_limited"] += 1
            self._consecutive_429[DEFERRABLE_SCOPE] += 1
            n = self._consecutive_429[DEFERRABLE_SCOPE]
            base = max(PAUSE_MIN_S, float(retry_after_s or 0.0))
            pause = min(base * (2 ** (n - 1)), PAUSE_MAX_S) + self._rng() * PAUSE_JITTER_MAX_S
            self._paused_until[DEFERRABLE_SCOPE] = max(
                self._paused_until.get(DEFERRABLE_SCOPE, 0.0), now + pause)

    def note_ok(self, scope: str) -> None:
        with self._lock:
            if scope == DEFERRABLE_SCOPE:
                self._consecutive_429[DEFERRABLE_SCOPE] = 0

    # <!-- 2026-09-20 builder 回報修正：dict(Counter) 對未出現的鍵不回 0，
    #      快照改為固定鍵集合逐一取值，「未觸發＝0」的語意才成立。 -->
    _COUNTER_KEYS = ("reservations", "reserved_weight", "denied_global",
                     "denied_scope", "rate_limited", "exhausted")

    def snapshot(self) -> dict:
        with self._lock:
            now = self._now()
            self._prune(now)
            used: Counter[str] = Counter()
            for _, w, s in self._log:
                used[s] += w
            return {
                "window_s": int(WINDOW_S), "global_cap": self._global_cap,
                "scope_caps": dict(self._scope_caps), "used": dict(used),
                "paused_until": dict(self._paused_until),
                "consecutive_429": dict(self._consecutive_429),
                "counters": {k: self._counters.get(k, 0) for k in self._COUNTER_KEYS},
            }
```

- [ ] **Step 4: 跑 `uv run pytest tests/test_hl_budget.py -q` 全綠；`uv run ruff check src/spark/publicapi/hl_budget.py tests/test_hl_budget.py`**
- [ ] **Step 5: Commit** `feat: HL 權重限流器（滑動視窗、scope 子預算、429 暫停）`

### Task 1.2 @inline：`HLGateway` 接 limiter

**Files:**
- Modify: `src/spark/publicapi/hl.py:115-130`
- Modify: `src/spark/publicapi/app.py:3386-3410`（Task 0.4 的 except 加 `BudgetExhausted`）
- Test: `tests/test_hl_gateway_budget.py`（新）

- [ ] **Step 1: 失敗測試**

```python
import httpx, pytest
from spark.publicapi.hl import HLGateway
from spark.publicapi.hl_budget import WeightLimiter, BudgetExhausted, ScopePaused

class Clock:
    def __init__(self): self.t = 0.0
    def now(self): return self.t
    def sleep(self, s): self.t += s

def _gw(post, clock, **kw):
    lim = WeightLimiter(global_cap=900, scope_caps={"explore": 300},
                        now_fn=clock.now, sleep_fn=clock.sleep, rng=lambda: 0.0)
    return HLGateway("https://x", post_fn=post, sleep_fn=clock.sleep, limiter=lim, **kw), lim

def _http_error(status):
    req = httpx.Request("POST", "https://x/info")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(status, request=req))

def test_each_attempt_reserves_weight_by_info_type():
    c = Clock(); seen = []
    def post(url, body): seen.append(body["type"]); return {"marginSummary": {"accountValue": "1"}}
    gw, lim = _gw(post, c)
    gw.clearinghouse_state("0xabc")
    gw.portfolio("0xabc")
    assert lim.snapshot()["used"] == {"interactive": 22}

def test_retry_attempts_each_pay():
    """resilience.run 對 transient 重試 3 次——每次嘗試都要各自預留（spec §5.2）。"""
    c = Clock(); n = {"i": 0}
    def post(url, body):
        n["i"] += 1
        raise ConnectionError("connection reset")
    gw, lim = _gw(post, c)
    with pytest.raises(ConnectionError):
        gw.clearinghouse_state("0xabc")
    assert n["i"] == 3
    assert lim.snapshot()["used"] == {"interactive": 6}

def test_429_reported_and_not_retried():
    c = Clock(); n = {"i": 0}
    def post(url, body):
        n["i"] += 1
        raise _http_error(429)
    gw, lim = _gw(post, c)
    with pytest.raises(httpx.HTTPStatusError):
        gw.portfolio("0xabc")
    assert n["i"] == 1
    s = lim.snapshot()
    assert s["counters"]["rate_limited"] == 1 and s["paused_until"]["explore"] == pytest.approx(60.0)

def test_budget_exhausted_is_not_retried_and_waits_bounded():
    c = Clock()
    def post(url, body): return {}
    gw, lim = _gw(post, c, wait_s=2.0)
    for _ in range(45): lim.try_reserve(20, "interactive")
    with pytest.raises(BudgetExhausted):
        gw.portfolio("0xabc")
    assert c.t == pytest.approx(2.0, abs=0.3)

def test_scoped_gateway_uses_scope_and_no_wait():
    c = Clock()
    def post(url, body): return {}
    gw, lim = _gw(post, c)
    ex = gw.scoped("explore")
    ex.portfolio("0xabc")
    assert lim.snapshot()["used"] == {"explore": 20}
    for _ in range(14): lim.try_reserve(20, "explore")
    with pytest.raises(BudgetExhausted):
        ex.portfolio("0xabc")          # explore 子預算滿、wait_s=0 立刻拋
    assert c.t == 0.0

def test_scoped_gateway_raises_scope_paused():
    c = Clock()
    def post(url, body): return {}
    gw, lim = _gw(post, c)
    lim.note_429("interactive")
    with pytest.raises(ScopePaused):
        gw.scoped("explore").clearinghouse_state("0xabc")

def test_no_limiter_keeps_old_behaviour():
    c = Clock()
    gw = HLGateway("https://x", post_fn=lambda u, b: {}, sleep_fn=c.sleep)
    assert gw.portfolio("0xabc") == {}
```

- [ ] **Step 2: 跑，FAIL（`limiter` 不是合法參數）**

- [ ] **Step 3: 改 `HLGateway`**

```python
import copy
from spark.publicapi.hl_budget import (WeightLimiter, weight_for,
                                       INTERACTIVE_SCOPE, INTERACTIVE_WAIT_S)
```

（兩個常數已在 Task 1.1 的 `hl_budget.py` 定義。）

```python
class HLGateway:
    """post_fn / sleep_fn 可注入：測試給 fake post 與不真睡的 sleep（沿 resilience 慣例）。

    `limiter`（2026-09-20 spec §5）：同 IP 權重限流器；`None` 維持舊行為（測試與
    timer 腳本）。`scope`／`wait_s`：本 gateway 發出的請求記在哪個 scope、等額度
    最多幾秒。`scoped()` 產生共用同一個 limiter 與 post_fn 的另一個 scope 視圖。
    """

    def __init__(self, base_url: str, post_fn=None, sleep_fn=time.sleep, *,
                 limiter: WeightLimiter | None = None,
                 scope: str = INTERACTIVE_SCOPE, wait_s: float = INTERACTIVE_WAIT_S):
        self._base = base_url.rstrip("/")
        self._explorer_url = _explorer_url_for(self._base)
        self._post = post_fn or _default_post
        self._sleep = sleep_fn
        self._limiter, self._scope, self._wait_s = limiter, scope, wait_s

    def scoped(self, scope: str, *, wait_s: float = 0.0) -> "HLGateway":
        g = copy.copy(self)
        g._scope, g._wait_s = scope, wait_s
        return g

    def _info(self, body: dict, what: str):
        def attempt():
            if self._limiter is not None:
                # 每一次嘗試（含 resilience 的重試）各自預留；預留後立即發送。
                self._limiter.reserve(weight_for(body["type"]), self._scope, wait_s=self._wait_s)
            try:
                result = self._post(f"{self._base}/info", body)
            except Exception as e:
                if self._limiter is not None and _is_429(e):
                    self._limiter.note_429(self._scope, _retry_after_s(e))
                raise
            if self._limiter is not None:
                self._limiter.note_ok(self._scope)
            return result
        return run(attempt, what=what, idempotent=True, sleep_fn=self._sleep)
```

```python
def _is_429(exc: Exception) -> bool:
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 429:
        return True
    return "429" in str(exc)          # 與 hl_explore._is_rate_limited 同一判準（fake post 用字串）


def _retry_after_s(exc: Exception) -> float | None:
    resp = getattr(exc, "response", None)
    raw = getattr(getattr(resp, "headers", None), "get", lambda k, d=None: None)("retry-after")
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None
```

- [ ] **Step 4: 跑 `uv run pytest tests/test_hl_gateway_budget.py tests/test_public_explore.py -q` 全綠**（既有 FakeHL 走 post_fn 注入、無 limiter → 舊行為不變）
- [ ] **Step 5: `app.py` Task 0.4 的 except 元組加入 `BudgetExhausted`（import 自 `spark.publicapi.hl_budget`）；跑 onboard 測試**
- [ ] **Step 6: Commit** `feat: HLGateway 每次嘗試經權重限流；429 回報限流器`

### Task 1.3 @inline：Explore 的 `_call_hl` 改走 limiter，移除 sleep 節流與 2/8/30 退避

**Files:**
- Modify: `src/spark/publicapi/hl_explore.py:259-262`（常數）、`:327-343`（`_RateLimitedAbort`／`_is_rate_limited`）、`:866-895`（建構子）、`:909-951`（`_call_hl`）、`:996-1035`（`build_sync`）
- Test: `tests/test_public_explore.py`（既有 429 退避測試改寫）

- [ ] **Step 1: 失敗測試**（替換既有「429 退避 2/8/30」測試；FakeHL 的 429 用字串含 "429" 的例外）

```python
def test_build_uses_explore_scope_and_aborts_on_429_without_sleeping(tmp_path):
    clock = Clock()
    lim = WeightLimiter(global_cap=900, scope_caps={"explore": 300},
                        now_fn=clock.now, sleep_fn=clock.sleep, rng=lambda: 0.0)
    hl = FakeHL(fail_fills_with=RuntimeError("429 Too Many Requests"))
    gw = HLGateway("https://x", post_fn=hl.post, sleep_fn=clock.sleep, limiter=lim)
    idx = ExploreIndex(leaderboard_source_fn=lambda: _payload(3), hl=gw.scoped("explore"),
                       excluded_fn=set, cfg=ExploreConfig(), now_fn=clock.now, sleep_fn=clock.sleep)
    idx.build_sync()
    assert idx._rows is None                                   # 中止、保舊（舊＝None）
    assert clock.t == 0.0                                      # 不再 sleep 2/8/30
    assert lim.snapshot()["paused_until"]["explore"] == pytest.approx(60.0)
    assert lim.snapshot()["used"]["explore"] == 20 + 120       # portfolio + 一頁 fills 各付一次

def test_build_stops_when_explore_budget_exhausted_and_keeps_old_rows(tmp_path):
    clock = Clock()
    lim = WeightLimiter(global_cap=900, scope_caps={"explore": 300},
                        now_fn=clock.now, sleep_fn=clock.sleep, rng=lambda: 0.0)
    for _ in range(15): lim.try_reserve(20, "explore")         # 子預算先用光
    hl = FakeHL()
    gw = HLGateway("https://x", post_fn=hl.post, sleep_fn=clock.sleep, limiter=lim)
    idx = ExploreIndex(leaderboard_source_fn=lambda: _payload(3), hl=gw.scoped("explore"),
                       excluded_fn=set, cfg=ExploreConfig(), now_fn=clock.now, sleep_fn=clock.sleep)
    idx._rows, idx._rows_version = [], EXPLORE_INDEX_VERSION
    idx.build_sync()
    assert idx._rows == [] and hl.calls == []
```

（`Clock`、`_payload(n)` 若檔內沒有就加在檔頭 helper 區；`FakeHL` 需提供 `post(url, body)` 介面版本——若既有 FakeHL 是方法級假物件，新增一個 `FakePost` 類別把 `body["type"]` 分派到既有方法即可。）

- [ ] **Step 2: 跑，FAIL**
- [ ] **Step 3: 改 hl_explore**
  - 刪 `DEFAULT_ENRICH_CALL_INTERVAL_S`、`RATE_LIMIT_RETRY_DELAYS_S`；`ExploreConfig.enrich_call_interval_s` 欄位與 `EXPLORE_ENRICH_CALL_INTERVAL_S` 讀取一起刪（grep 確認無其他使用者）。
  - `_call_hl` 改為：

```python
    def _call_hl(self, fn: Callable[[], object], *, what: str) -> object:
        """單一 HL 呼叫。節流與 429 處理已**全部**移到 `HLGateway`＋`WeightLimiter`
        （spec §5；本物件拿到的 `hl` 必須是 `gateway.scoped("explore")`）：
        - 額度不足（`BudgetExhausted`）或 scope 暫停（`ScopePaused`）→ `_BudgetUnavailable`
          → `build_sync` 中止本輪、保留舊版（P3 的 worker 改為逐 job 讓位）。
        - 429 → gateway 已向 limiter 回報並暫停 explore scope；這裡同樣以
          `_RateLimitedAbort` 中止本輪。舊版 2/8/30 秒退避與 0.7 秒 sleep 已刪：
          它們把 429 當節流器用，正是 2026-09-19 事故的根因。
        - 其他錯誤 → 原樣上拋（`_enrich_one` 的「跳過該列」語意不變）。
        """
        try:
            return fn()
        except (BudgetExhausted, ScopePaused) as e:
            raise _BudgetUnavailable(what) from e
        except Exception as e:
            if _is_rate_limited(e):
                logger.error("build aborted: rate limited（%s）", what)
                raise _RateLimitedAbort(what) from e
            raise
```

  - 新增 `class _BudgetUnavailable(Exception)`（緊鄰 `_RateLimitedAbort`，docstring 同型）；`_enrich_one` 的 `except _RateLimitedAbort: raise` 改為 `except (_RateLimitedAbort, _BudgetUnavailable): raise`；`build_sync` 的 `except _RateLimitedAbort as e:` 改為 `except (_RateLimitedAbort, _BudgetUnavailable) as e:`，log 文字改「中止本輪建置（%s），保留舊 snapshot」。
  - 建構子 `sleep_fn` 參數保留但不再於 `_call_hl` 使用（P3 刪）。
  - `from spark.publicapi.hl_budget import BudgetExhausted, ScopePaused`。

- [ ] **Step 4: 跑 `uv run pytest tests/test_public_explore.py -q`**：改寫所有依賴 `sleep_fn` 收到 0.7／2／8／30 的斷言；全綠。
- [ ] **Step 5: Commit** `refactor: explore 建置改經權重限流，移除 sleep 節流與 429 退避`

### Task 1.4 @inline：接線與觀測

**Files:**
- Modify: `src/spark/publicapi/config.py:330-340` 附近（新增 `hl_global_weight_cap`、`hl_explore_weight_cap`，env `FILET_HL_GLOBAL_WEIGHT_CAP=900`、`FILET_HL_EXPLORE_WEIGHT_CAP=300`，int）
- Modify: `scripts/run_api.py:30`：`limiter = WeightLimiter(global_cap=cfg.hl_global_weight_cap, scope_caps={"explore": cfg.hl_explore_weight_cap})`；`gateway = HLGateway(cfg.api_url, limiter=limiter)`
- Modify: `src/spark/publicapi/app.py:1335`（`create_app` 加 `hl_limiter: WeightLimiter | None = None`，存 `app.state.hl_limiter`）、`:2292`（`ExploreIndex(hl=hl.scoped("explore") if hasattr(hl, "scoped") else hl, ...)`）、`:4453`（`/api/ops/health` 回應加 `"hl_budget": limiter.snapshot() if limiter else None`）
- Test: `tests/test_ops_health*.py`（找既有）：注入 limiter 後 `hl_budget.global_cap == 900`；未注入 → `null`

- [ ] **Step 1: 失敗測試** → **Step 2: FAIL** → **Step 3: 實作** → **Step 4: `uv run pytest -q` 全綠、`ruff` 乾淨**
- [ ] **Step 5: Commit** `feat: filet-api 接上 HL 權重限流；ops/health 揭露預算快照`

### Task 1.5 @inline：P1 reviewer 修正（2026-09-20 opus 審查，2 Critical／5 Warning）

<!-- 裁決記錄：C1 fills 固定預留 120 使自訂帳本比 HL 真實計費高 3–6 倍 → 採「預留後依實際筆數結算退回」
（spec §5.1「第一版不退款」是簡化而非禁令；同源同基準優先）。C2 額度不足被寫進詳情頁負面快取 → 視為 transient
上拋 502。W1/W2 兩處字串判型假陽性。W3 快照凍結期間需 ops 可見。D9 統一為 502。 -->

**Files:** `src/spark/publicapi/hl_budget.py`、`hl.py`、`hl_explore.py`、`app.py`；`tests/test_hl_budget.py`、`tests/test_hl_gateway_budget.py`、`tests/test_api_ops.py`、`tests/test_public_explore.py`、traders 詳情測試檔。

- [ ] **A. `hl_budget.py`**
  1. 帳本項目改為可變 `list[float, int, str]`，`try_reserve`／`reserve` 回傳該項目作為 token（`try_reserve` 失敗回 `None`；`reserve` 成功回 token）。新增：
     ```python
     def settle(self, token, actual_weight: int) -> None:
         """回應到手後依實際計費**下修**預留（只降不升）。fills 類預留 120 是上限，
         實際 = 20 + ceil(筆數/20)；不結算會讓自訂帳本比 HL 真實計費高 3–6 倍，
         造成 HL 沒擋、我們自己先擋的假陽性（reviewer C1）。"""
         with self._lock:
             if token is None or actual_weight >= token[1]:
                 return
             self._counters["refunded_weight"] += token[1] - actual_weight
             token[1] = actual_weight
     ```
     `_COUNTER_KEYS` 加 `"refunded_weight"`。
  2. `ScopePaused.__init__` 訊息改為 `f"scope {scope} paused: upstream rate limited"`（**不含任何數字**——`resilience._TRANSIENT_MARKERS` 含 "502/503/504"，`until` 的十進位若含這些子串會被誤判 transient 重試 3 次；reviewer W1，主線程實跑確認）。`until` 仍存屬性。
  3. `note_429`：暫停已在生效中（`now < paused_until[explore]`）就**不升級**，只取 `max(既有, now+base)`；升級公式改為 `pause = min(max(retry_after or 0, PAUSE_MIN_S * 2**(n-1)), PAUSE_MAX_S) + jitter`（Retry-After 是「至少等這麼久」，不再被乘上去；docstring 同步）。
  4. `note_ok(scope)`：任何 scope 成功都把 `_consecutive_429[explore]` 歸零（上游恢復的證據不分 scope）。
  5. 新增模組層 `is_rate_limited(exc) -> bool`：`getattr(getattr(exc,"response",None),"status_code",None) == 429`，否則 `re.search(r"^429\b|429 Too Many Requests", str(exc))`（不再用 `"429" in str`；reviewer W2：`JSONDecodeError ... column 429` 會誤判）。
  6. 模組 docstring「唯一的權重帳本」改為「所有 `/info` 呼叫的權重帳本（`user_details` 走 explorer host，不計）」。
- [ ] **B. `hl.py`**：`_info` 的 `attempt()` 保存 `token = self._limiter.reserve(...)`；`_post` 成功後若 `body["type"] in ("userFillsByTime", "userFills")` 且 `isinstance(result, list)` → `self._limiter.settle(token, 20 + (len(result) + 19) // 20)`。`_is_429` 改為呼叫 `hl_budget.is_rate_limited`（刪本地版本）。
- [ ] **C. `hl_explore.py`**：`_is_rate_limited` 改為 `return is_rate_limited(exc)`（import 自 hl_budget；保留函式名與 docstring 改一句）。
- [ ] **D. `app.py`**
  1. `_cached_trader_data`（約 2479-2510）四個 `try` 各在既有 `except Exception` **之前**加 `except (BudgetExhausted, ScopePaused): raise`，並在第一個加註解：額度不足是 transient，不得進負面快取或部分結果快取（reviewer C2），由全域 handler 回 502。
  2. 全域 handler：`BudgetExhausted` 之後再註冊 `ScopePaused` → 502，detail `"上游額度暫停中，請稍後重試"`。
  3. `/api/ops/health` 加 `"explore_index": explore_index.status()`；在 `hl_explore.ExploreIndex` 新增
     ```python
     def status(self) -> dict:
         with self._lock:
             return {"rows": None if self._rows is None else len(self._rows),
                     "built_at": self._built_at, "version": self._rows_version,
                     "building": self._building}
     ```
     （reviewer W3：P1-only 部署期間 Explore 凍結在磁碟快照，ops 需看得到年齡。）
- [ ] **E. 測試**（先寫、FAIL、改、PASS）
  - `test_hl_budget.py`：`test_429_pauses_explore_scope_with_escalation_and_retry_after` 第二段改 `retry_after_s=90` → `paused_until == 1100 + max(90, 120) = 1220`；新增 `test_settle_refunds_down_only`（reserve 120 → settle 23 → used 23、`refunded_weight == 97`；settle 200 不變）；`test_note_429_during_active_pause_does_not_escalate`；`test_scope_paused_message_has_no_digits`（`not any(ch.isdigit() for ch in str(ScopePaused("explore", 250238.0)))` 且 `resilience._is_transient_error(...)` 為 False）；`test_is_rate_limited_ignores_json_column_429`。
  - `test_hl_gateway_budget.py`：`test_fills_page_settles_to_actual_weight`（post 回 50 筆 list → `used["interactive"] == 23`）；`test_json_decode_error_is_not_reported_as_429`（post 拋 `json.JSONDecodeError("Expecting value", " "*428, 428)` → `rate_limited == 0`）。
  - traders 詳情測試檔（`rg -ln "_trader_portfolio_negative_cache|/api/traders/" tests`）：`hl.portfolio` 拋 `BudgetExhausted` → 502；隨後 `hl.portfolio` 恢復正常 → 下一次 GET 200（未被負面快取釘 60 秒）。
  - `test_api_ops.py`：health 回應含 `explore_index.rows/built_at/version/building`。
  - `test_public_explore.py:1152-1157, 1206`：刪 `EXPLORE_ENRICH_CALL_INTERVAL_S` 殘留與「真睡」docstring（reviewer S1）。
- [ ] **F. plan D9** 那列的「onboard/status 回 503」改為 502（本檔）。
- [ ] **G. Commit** `fix: P1 審查修正（fills 實際結算、額度不足不進負快取、429/暫停判型）`

### Task 1.6 @inline：複審修正（2026-09-20 opus 複審：0 Critical／4 Warning／3 Suggestion）

<!-- 裁決：W1 Retry-After 不得被 PAUSE_MAX_S 夾住；W2 撤回 1.5-A4（任何 scope 成功歸零）——interactive 的
weight-2 成功不代表 explore 的 120 權重請求也能過，升級階梯要靠 explore 自己的 429 累積；W3 快照加相對秒數；
W4 詳情頁次要欄位「降級但不快取」而非整頁 502；S1 過期 token 不計退款；S2/S3 補測試。 -->

**Files:** `src/spark/publicapi/hl_budget.py`、`app.py`；`tests/test_hl_budget.py`、`tests/test_public_traders.py`、`tests/test_api_ops.py`。

- [ ] **A. `hl_budget.py`**
  1. `note_429` 升級公式改 `pause = max(retry_after or 0.0, min(PAUSE_MIN_S * 2**(n-1), PAUSE_MAX_S)) + jitter`（上限只夾指數部分，Retry-After 是下限）；「暫停生效中」分支同樣 `max(既有, now + max(PAUSE_MIN_S, retry_after or 0) + jitter)`（已如此，確認）。
  2. `note_ok(scope)`：**只有 `scope == DEFERRABLE_SCOPE`** 才歸零 `_consecutive_429`（撤回 1.5-A4；docstring 寫明理由：interactive 的低權重成功不證明 explore 的 120 權重能過，階梯要靠 explore 自己的 429 累積、自己的成功歸零）。
  3. `settle`：token 已滑出視窗（`token[0] <= now - WINDOW_S`）→ 直接 return，不計 `refunded_weight`。
  4. `snapshot()` 加 `"paused_remaining_s": {scope: max(0.0, until - now) for scope, until in self._paused_until.items()}`；docstring 註明 `paused_until` 是 `now_fn` 時基（正式路徑 monotonic），ops 看 `paused_remaining_s`。
- [ ] **B. `app.py` `_cached_trader_data`**：ledger 與 fills 兩個 try 的 `except (BudgetExhausted, ScopePaused): raise` 改為「降級該欄位為 None＋設 `skip_cache = True`」（在函式開頭 `skip_cache = False`）；函式末尾 `if skip_cache: return rows, account_value, deposit, ch_state, fills, fills_truncated`（**不寫** `_trader_portfolio_cache`、但仍 pop 負面快取）。portfolio 與 clearinghouse 兩處維持上拋（主欄位缺就是整頁不可用）。註解：額度不足是 transient，次要欄位降級但這份不完整結果不得進 5 分鐘快取（複審 W4）。
- [ ] **C. 測試**
  - `test_hl_budget.py`：`test_retry_after_is_floor_not_clamped`（t=2000、`retry_after_s=1800` → `paused_until == 3800 + jitter(0)`）；`test_escalation_survives_interactive_success`（429 → t+61 `note_ok("interactive")` → 再 429 → `consecutive_429["explore"] == 2`、暫停 120）；`test_explore_success_resets_escalation`；`test_settle_after_prune_does_not_count_refund`；`test_snapshot_has_paused_remaining_s`。
  - `test_public_traders.py`：`test_fills_budget_exhausted_degrades_without_caching`——`hl.get_fills_raw_paged` 拋 `BudgetExhausted` → 200、`fills_30d is None`（或該檔既有的缺值表示）；隨後恢復 → 下一次 GET 重新打上游（`portfolio_calls == 2`，證明沒進 5 分鐘快取）。
  - `test_api_ops.py`：`explore_index` 在 `idx.build_sync()` 後 `rows`／`built_at` 非 None；`hl_budget.paused_remaining_s` 鍵存在。
- [ ] **D. Commit** `fix: P1 複審修正（Retry-After 下限、explore 自身歸零、詳情頁降級不快取、快照剩餘秒數）`

**P1 驗收（主線程親跑）：** 全測試綠；`rg -n "enrich_call_interval_s|RATE_LIMIT_RETRY_DELAYS_S" src` 零命中；`rg -n "limiter" scripts/run_api.py` 命中；`uv run python -c "from spark.publicapi.app import create_app"` 可 import。D8 通過則此時做第一次部署（RUNBOOK §5.8a 流程，env 新增兩個 cap 變數，drop-in `hl-budget.conf`）。

---

## P2 持久化（任務卡；步驟級於 P1 驗收後展開）

### Task 2.1 @inline：`explore_store.py` schema 與 migration

`ExploreStore(db_path, now_fn)`，`sqlite3.connect(check_same_thread=False)`，啟動時 `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON`，`schema_version` 表（v1）。所有寫入 `with self._db:`（單一 transaction），所有讀取回 dataclass。

```sql
CREATE TABLE candidate (
  address TEXT PRIMARY KEY,            -- 小寫正規化
  display_name TEXT, source_rank INTEGER, source_roi REAL,
  source_as_of REAL NOT NULL,          -- stats-data payload 取得時刻
  active INTEGER NOT NULL DEFAULT 1, last_seen_at REAL NOT NULL);
CREATE TABLE endpoint_cache (
  address TEXT NOT NULL, endpoint TEXT NOT NULL,   -- 'portfolio' | 'clearinghouseState'
  params_fp TEXT NOT NULL DEFAULT '',              -- dex／參數指紋（目前 ''）
  payload TEXT,                                    -- JSON；NULL＝從未成功
  fetched_at REAL, refresh_after REAL NOT NULL,
  last_error TEXT, last_error_at REAL,
  PRIMARY KEY (address, endpoint, params_fp));
CREATE TABLE fills (
  address TEXT NOT NULL, coin TEXT NOT NULL, tid INTEGER NOT NULL,
  time_ms INTEGER NOT NULL, raw TEXT NOT NULL,     -- 原始 HL fill JSON（Decimal 字串原樣）
  PRIMARY KEY (address, coin, tid));
CREATE INDEX fills_addr_time ON fills(address, time_ms);
CREATE TABLE fills_sync (
  address TEXT PRIMARY KEY,
  window_start_ms INTEGER NOT NULL, window_end_ms INTEGER NOT NULL,  -- 本輪固定區間
  cursor_ms INTEGER NOT NULL,          -- 下一頁 startTime（inclusive，含重疊）
  synced_through_ms INTEGER,           -- 已確認遍歷到此
  observed_from_ms INTEGER, observed_to_ms INTEGER,
  completeness TEXT NOT NULL DEFAULT 'backfilling',   -- backfilling|partial|complete（2026-09-20 使用者裁決：不用 unknown）
  reason TEXT, pages_done INTEGER NOT NULL DEFAULT 0,
  fills_in_window INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL, last_error TEXT);
CREATE TABLE refresh_job (
  key TEXT PRIMARY KEY,                -- f"{address}:{kind}"  kind∈portfolio|state|fills|candidates
  address TEXT, kind TEXT NOT NULL, priority INTEGER NOT NULL,   -- 0 最高
  created_at REAL NOT NULL, next_attempt_at REAL NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_until REAL, lease_owner TEXT, fencing INTEGER NOT NULL DEFAULT 0,
  last_error TEXT);
CREATE INDEX job_due ON refresh_job(next_attempt_at, priority);
```

介面（全部純 SQL、無網路）：`upsert_candidates(rows, as_of)`、`deactivate_missing(seen_addresses)`、`get_cache(address, endpoint)`、`put_cache_ok(address, endpoint, payload, fetched_at, refresh_after)`、`put_cache_error(address, endpoint, err, at, next_after)`（**不動 fetched_at／payload**）、`insert_fills_page(address, fills, checkpoint: FillsSyncState)`（同一 transaction）、`get_fills(address, start_ms, end_ms)`、`get_sync(address)`、`enqueue(key, address, kind, priority, next_attempt_at)`（已存在則只調高優先級／提早時間）、`claim_due(now, owner, lease_s) -> Job | None`（`UPDATE ... WHERE lease_until IS NULL OR lease_until < now` 條件更新＋fencing+1）、`complete(job, fencing)`／`reschedule(job, fencing, next_at, err)`（`WHERE fencing = ?` 防過期 owner 覆寫）、`purge(now, candidate_keep_s=7d, fills_keep_s=35d)`（不刪仍有未完成 job 或 sync 依賴的列）。

驗收：`tests/test_explore_store.py`——WAL 開啟；`put_cache_error` 不覆寫 `fetched_at`；`insert_fills_page` 重播同頁冪等（PRIMARY KEY 去重、checkpoint 不變）；`claim_due` 兩個 owner 競爭只有一個拿到；過期 fencing 的 `complete` 為 no-op；`purge` 保留被依賴列。

### Task 2.2 @inline：`explore_fills_sync.py` 單頁演算法（spec §8）

```python
@dataclass(frozen=True)
class FillsSyncState: ...   # 對映 fills_sync 一列
def plan_page(state: FillsSyncState | None, *, now_ms, window_days=30, overlap_ms=1) -> tuple[int, int, FillsSyncState]
    # 無 state 或上輪 complete/partial 且 window_end 過舊 → 新區間 [now-30d, now]，cursor=window_start，completeness=backfilling
    # 上輪 complete 且要增量 → cursor = synced_through - overlap，window_end = now
    # 未完成 → 沿用 window_end，cursor 不變
def apply_page(state, page: list[dict], *, page_limit=2000, retention_limit=10_000) -> tuple[FillsSyncState, list[dict], str | None]
    # 校驗：list、每筆有 coin/tid/time、time 非降冪且 ≥ cursor；否則回 ('invalid_page') 不推進
    # observed_from/to 更新；fills_in_window += 新筆數（呼叫端以 insert 結果回填實際新增數）
    # len(page) < page_limit → synced_through = window_end；completeness =
    #     'partial' if fills_in_window >= retention_limit - page_limit else 'complete'   （留存判準見下）
    # len(page) == page_limit：
    #     若所有筆 time 相同且 == cursor → no-progress：completeness='partial', reason='same_ms_overflow', 終止本輪
    #     否則 cursor = page[-1]['time']（inclusive，重疊由 PK 去重），pages_done += 1，continue
```

留存判準（誠實標註於 docstring）：HL 只保留最近 10,000 筆可查。區間內的成交比區間外更新，所以若區間內觀測筆數 < 10,000 − 一頁，區間內成交必然全在可查範圍內 → `complete`；達到門檻 → `partial`／`reason='retention_limit'`。初次回補在遍歷完成前恆為 `backfilling`（使用者裁決 2026-09-20：不用 `unknown`，這個字像「不清楚狀況」；`backfilling` 明確表示第一輪回補尚未完成）。空頁與 HTTP 200 不改變 completeness。

驗收：`tests/test_explore_fills_sync.py`——滿頁續抓 cursor＝最後一筆時間（無 +1）；重疊頁去重；同毫秒溢出 → partial＋有界終止；短頁 → complete 或 partial（依門檻）；亂序頁 → invalid 不推進；增量模式沿用 synced_through 重疊。

### Task 2.3 @sdd：`config.py` 加 `explore_db_path`（`FILET_EXPLORE_DB`，必填如 `explore_cache_path` 慣例）；`run_api.py` 建 `ExploreStore` 注入 `create_app(explore_store=...)`；測試用 `tmp_path`。

**P2 驗收：** 三個新測試檔全綠；`uv run python -c "import sqlite3; print(sqlite3.sqlite_version)"` ≥ 3.24（UPSERT）。

---

## P3 分層更新（任務卡）

### Task 3.1 @inline：`explore_scheduler.py`

```python
class ExploreScheduler:
    def __init__(self, *, store, hl_explore_scoped, leaderboard_source_fn, excluded_fn, cfg, now_fn, sleep_fn, notify_dirty: Callable[[], None])
    def tick(self) -> str          # 單步：'idle'|'ran:<kind>'|'paused'|'no_budget'；測試逐步驅動
    def run_forever(self, stop: threading.Event)   # thread target：while not stop: tick(); sleep(1 if idle else 0)
```

排程規則（spec §6）：
- `candidates` job：每 600s；呼叫 `leaderboard_source_fn()`（不計 info 權重），`upsert_candidates` 前 `cfg.candidate_pool` 名、`deactivate_missing`；新地址的 portfolio／state 到期時間用 `hash(address) % 分散區間` 打散。
- 每個 active 地址：`portfolio` 每 3600s ±10% jitter；`state` 每 900s ±10%；`fills` 目標 14400s，前 50 名（source_rank）優先級 1、其餘 2。基礎資料 priority 0（state）/1（portfolio），fills 2/3；同 priority 依 `next_attempt_at` 最早者先。
- 執行：`claim_due` → 依 kind 呼叫 `hl_explore_scoped.portfolio/clearinghouse_state/get_fills_raw_page`（**新方法**：`hl.get_fills_page(address, start_ms, end_ms) -> list[dict]`，單頁、不翻頁，Task 3.2）→ store 寫入 → `complete` 或（fills 未完）`reschedule(next_attempt_at=now)`；`BudgetExhausted` → `reschedule(now+5s)` 不計 attempts；`ScopePaused` → tick 回 'paused' 並 sleep 到 until；429（gateway 已暫停 scope）→ reschedule(now+60s)；5xx／timeout → attempts+1、`next = now + min(30 * 2**attempts, 900) + jitter`；非重試型 4xx → `put_cache_error`＋隔離 24h。
- 每次成功寫入呼叫 `notify_dirty()`（publisher 用）。
- lease 60s；同 process 單 thread，但 `claim_due` 的條件更新與 fencing 照 spec 實作（重啟後過期 lease 自動可領）。
- 準入上限：`refresh_job` 總數 ≤ 4 × active 候選數；詳情頁刷新請求（Task 3.3）另有 pending ≤ 20。

驗收：`tests/test_explore_scheduler.py`（fake clock、FakeHL 計數）：到期不暴衝（300 地址初次 6,600 權重在 300/min 子預算下 tick 序列 ≥ 22 分鐘且任一分鐘 ≤ 300）；`ScopePaused` 期間零上游呼叫；fills 三頁以上分多個 tick 續跑且其間別的地址有機會執行；重啟（新 scheduler 實例、同 store）續接 cursor；候選進出不清空既有 cache。

### Task 3.2 @sdd：`hl.py` 加 `get_fills_page(address, start_ms, end_ms) -> list[dict]`（單次 `userFillsByTime`，`aggregateByTime` 沿既有 `_paged_fills_raw` 所用值並寫進 docstring；回原始 list）。測試：body 形狀與既有分頁器第一頁一致。

### Task 3.3 @inline：`/api/traders/{address}` 改讀本地（D4）
- 池內地址：`_cached_trader_data` 先查 `store.get_cache` 三種＋`store.get_fills`；`fetched_at` 超過 300s → `store.enqueue(f"{addr}:portfolio", priority=1, next=now)` 等（去重、pending ≤ 20），回應加 `refreshing: true`、`as_of`；不等待。
- 池外地址：維持既有同步抓取（interactive scope、經 limiter）。
- `non_funding_ledger_updates` 池內地址也入 endpoint_cache（endpoint='ledger'，refresh 3600s）。
- 驗收：既有 traders 測試全綠；新測試：池內地址 1000 次 GET 零上游呼叫、只產生一個 pending job。

### Task 3.4 @inline：接線與 D6 清理
- `create_app(..., explore_scheduler=None)`；`run_api.py` 建 scheduler，`EXPLORE_UPSTREAM_REFRESH=1` 才 `threading.Thread(target=run_forever, daemon=True).start()`；`/api/ops/health` 加 `explore_refresh: {enabled, last_tick_at, last_result, queue_depth, oldest_due_age_s, paused_until}`。
- 刪 `ExploreIndex.build_sync`／`_maybe_trigger_build`／`_enrich_one`／`_call_hl`／`_RateLimitedAbort`／`_BudgetUnavailable`／enrich LRU；`ExploreIndex` 只剩讀路徑、`set_published(rows, meta)`（Task 4.1）、snapshot。`tests/test_public_explore.py` 對應測試改由 publisher 測試覆蓋後刪除。
- 驗收：`rg -n "build_sync|_enrich_one|_call_hl" src` 零命中；全測試綠。

---

## P4 漸進發布（任務卡）

### Task 4.1 @inline：`explore_publisher.py`
```python
def compose_rows(store, *, now, cfg) -> tuple[list[ExploreRow], dict]   # 純函式：從 store 讀 active 候選 → 每地址 enrich_candidate(portfolio or None, fills, state or None, coverage) → _apply_tags；meta={published_at, as_of_min/max per endpoint, coverage_counts}
class ExplorePublisher:
    def __init__(self, store, index: ExploreIndex, cfg, now_fn, snapshot_path)
    def maybe_publish(self, *, force=False) -> bool   # dirty 且距上次 ≥60s → compose → index.set_published → dump_snapshot v4
```
- `enrich_candidate` 改為接受 `portfolio_raw=None`／`ch_state=None`：對應欄位（`windows`、`live_days`／`account_bucket`、`exposure_*`）為 None，不當成 0；`fills_stats` 吃 store 的 fills（30 天窗）＋`coverage`；`ExploreRow` 新增 `as_of: dict[str, float | None]`、`fills_coverage: dict`（`to_dict` 輸出，D5），`fills_truncated` 保留＝`coverage.state != "complete"`（前端相容）。
- `EXPLORE_INDEX_VERSION` 3→4；`load_snapshot` 讀到 v3 → 轉為 v4（`as_of` 全＝`built_at`、coverage backfilling，D7），不丟棄。
- `query()` 回應加 `published_at`、`initializing: bool`（＝從未有版本）；`building` 保留＝`initializing`（前端相容）。
- scheduler 的 `notify_dirty` → publisher；publisher 由 scheduler thread 每 tick 末呼叫 `maybe_publish()`。
- 驗收：`tests/test_explore_publisher.py`——單地址無 portfolio 仍出列且 windows 全 None；來源整體失敗保留上一版；v3 快照可讀且 `as_of` 正確；`published_at` ≠ 欄位 `as_of`；原子寫（`safe_fs.write_json_atomic`）。

### Task 4.2 @sdd：前端 `web/src/lib/publicApi.ts` 型別加 `as_of`、`fills_coverage`、`published_at`、`initializing`（optional）；explore／traders 頁的「成交資料可能不完整」提示改讀 `fills_coverage.state !== "complete"`（`fills_truncated` 為 fallback）；vitest 對應測試更新。

---

## P5 驗收與啟用準備（任務卡）

- Task 5.1 @sdd：`deploy/RUNBOOK.md` 新節「Explore 背景刷新」：env（`FILET_HL_GLOBAL_WEIGHT_CAP`、`FILET_HL_EXPLORE_WEIGHT_CAP`、`FILET_EXPLORE_DB`、`EXPLORE_UPSTREAM_REFRESH`）、drop-in 檔名、觀察 `/api/ops/health.hl_budget`／`.explore_refresh`、停用刷新（設 `EXPLORE_UPSTREAM_REFRESH=0` 重啟，快照續讀）、回退（不重新啟用舊 rebuild；程式已刪）。`deploy/filet-api.service.d/explore-refresh.conf` 範本；`var/lib/filet-api/explore.db` 權限 `filet-api` 0600。
- Task 5.2（主線程）：本機以 `EXPLORE_UPSTREAM_REFRESH=1`＋testnet 或 mainnet 唯讀 gateway 跑 10 分鐘觀測：`hl_budget.used.explore` 任一分鐘 ≤ 300、零 429、queue 遞減、publish 發生；紀錄到 `docs/superpowers/research/2026-09-XX-explore-refresh-observation.md`。這是 spec §12 第 4 點：測試通過 ≠ production 驗證。
- Task 5.3（主線程＋使用者）：第二次部署（D8），先觀察 `hl_budget` 24 小時再把 `EXPLORE_UPSTREAM_REFRESH` 打開。

---

## 3. 與 spec 的對照（自審）

| spec | 對應 |
|---|---|
| §3 不變條件 1（開頁不發 info） | Task 0.1、3.3、4.1 |
| §3 條件 2（每次嘗試先取額度） | Task 1.2（含 resilience 重試）、3.1 |
| §3 條件 3（explore 子預算＋全域） | Task 1.1 |
| §3 條件 4–7 | Task 2.1（error 不覆寫 fetched_at）、2.2（completeness）、4.1（缺值≠0、保舊版） |
| §5.1 滑動視窗 / inflight 占額 / timeout | Task 1.1；`_TIMEOUT_S` 既有 |
| §5.2 429 暫停≥60s、Retry-After、指數、不在 worker sleep、其他 scope 通知 | Task 1.1、1.2、3.1 |
| §5.2 未接入流量如實標明 | §0 表；RUNBOOK（5.1） |
| §6 頻率／jitter／前 50 優先 | Task 3.1 |
| §7 六種資料、LRU 只作加速、保留期 | Task 2.1（LRU 交由 `_cached_trader_data` 既有 256，資料真身在 DB） |
| §8 單頁演算法、同毫秒、留存、completeness | Task 2.2、3.2 |
| §9.1 lease／fencing／dedupe／admission | Task 2.1、3.1、3.3 |
| §9.2 每分鐘發布、v3 相容、initializing | Task 4.1 |
| §9.3 排名語義不變 | `qualify`／`sort_rows`／`_apply_tags` 不動 |
| §10 P0–P5 | 同名階段 |
| §11 十一個測試情境 | 0.1、1.1、1.2、1.3、2.1、2.2、3.1、4.1 各自驗收列；「多 worker 並發」以 threading 版 `test_any_60s_window_bounded_by_cap` 補一個 8 thread 版本（Task 1.1 Step 3 之後加） |
| §12 交付格式 | 每階段驗收段＋Task 5.2 觀測報告 |

**已知未涵蓋／誠實標註：** 引擎與 timer 不接入限流（D1）；多 dex／spot／subaccount 不在範圍（現況無）；成交完整性只能保證到「留存門檻」判準，無法證明 HL 端沒有其他截斷。

## 4. 執行狀態

分支 `feat/explore-rate-limit`（自 main 54c038b 切出）。主線程逐 task 親跑驗收後記錄。

| Task | 日期 | commit | 主線程驗收 |
|---|---|---|---|
| 0.1 | 2026-09-20 | e7ee43d | `pytest tests/test_public_explore.py` 78 passed；ruff 乾淨；`query()` 內無 `_maybe_trigger_build()` 呼叫 |
| 0.2 | 2026-09-20 | ad6f82b | `pytest tests/test_me_dashboard.py tests/test_dashboard_sync.py` 31 passed；`_HEALTHY_CYCLE_RESULTS` 於 app.py:690/727 |
| 0.5 | 2026-09-20 | 270230f | copy.ts 1257/2917 新值；vitest SyncCard＋enNoCjk 10 passed |
| 0.3 | 2026-09-20 | 9b56058 | 同上兩檔 32 passed；`_dashboard_sync` 只在 `data_state != "error"` 寫快取 |
| 0.4 | 2026-09-20 | e5a6479 | 裁決改為全域 handler（見 Task 0.4 註）；`pytest tests/test_api_onboard.py tests/test_api_billing.py` 59 passed |
| **P0 驗收** | 2026-09-20 | — | `uv run pytest -q` 2933 passed；`uv run ruff check src tests scripts` 乾淨；`query()` 內無觸發呼叫。可做第一次部署（D8）待 P1 完成後一起 |
| 1.1 | 2026-09-20 | c01b217 | `pytest tests/test_hl_budget.py` 9 passed（含 8 thread 並發）；snapshot counters 修正寫回 plan（a00fac9） |
| 1.2 | 2026-09-20 | f8cece7 | 113 passed（gateway budget＋explore＋onboard）；`BudgetExhausted` 全域 handler app.py:1384 |
| 1.4 | 2026-09-20 | c9f4b66 | 133 passed（ops＋config）；run_api 注入 limiter；ExploreIndex 用 `scoped("explore")`；ops/health `hl_budget` |
| 1.3 | 2026-09-20 | 6cb73a8 | 86 passed；hl_explore 零 `time.sleep`／退避常數；`_BudgetUnavailable` 中止保舊 |
| **P1 驗收** | 2026-09-20 | — | `uv run pytest -q` 2957 passed；`ruff check src tests scripts` 乾淨；`rg limiter scripts/run_api.py` 3 命中 |
| P1 審查 | 2026-09-20 | — | opus reviewer：2 Critical／5 Warning／3 Suggestion，主線程逐條實跑確認 → Task 1.5 |
| 1.5 | 2026-09-20 | a41c189 | 三判準（ScopePaused 非 transient／JSON column 429 不誤判／真 429 命中）主線程實跑 False/False/True；settle 120→23 退 97；2965 passed；ruff 乾淨 |
| P1 複審 | 2026-09-20 | — | opus fresh reviewer：前輪七項全部「已關閉」（附實跑）；0 Critical／4 Warning／3 Suggestion；結論可部署 → 主線程裁決四項 Warning 部署前修（Task 1.6） |
| 1.6 | 2026-09-20 | cc28baa | 主線程實跑 `floor 3800.0`／`n 2 remaining 120.0`；app.py 只剩 2 處 raise；2973 passed；ruff 乾淨；vitest 716 passed |
| **P1 最終驗收** | 2026-09-20 | — | 全量 pytest 2973、vitest 716、ruff 乾淨；prod 快照 2026-09-19T15:00 v3 299 列、五個 drop-in 齊全、prod 版本 b9e5649 已含於本分支 |
| **第一次部署（D8）** | 2026-09-20 04:09 UTC | 4295ece | 使用者授權。rsync 兩段、web build、drop-in `hl-budget.conf`、restart api＋dashboard、DEPLOYED_VERSION；`filet_regression_check --http --ssh` 67/67；20 次 explore GET 零上游行。記錄：RUNBOOK 部署日誌 2026-09-20 條 |
