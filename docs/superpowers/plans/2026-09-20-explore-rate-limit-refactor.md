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

<!-- 2026-09-20 裁決（2.2 驗收）：增量輪 `fills_in_window` 重置為 0，`completeness` 保留——留存門檻只看本輪查詢區間內的觀測筆數，跨輪累加會在數天後誤標 partial。 -->
留存判準（誠實標註於 docstring）：HL 只保留最近 10,000 筆可查。區間內的成交比區間外更新，所以若區間內觀測筆數 < 10,000 − 一頁，區間內成交必然全在可查範圍內 → `complete`；達到門檻 → `partial`／`reason='retention_limit'`。初次回補在遍歷完成前恆為 `backfilling`（使用者裁決 2026-09-20：不用 `unknown`，這個字像「不清楚狀況」；`backfilling` 明確表示第一輪回補尚未完成）。空頁與 HTTP 200 不改變 completeness。

驗收：`tests/test_explore_fills_sync.py`——滿頁續抓 cursor＝最後一筆時間（無 +1）；重疊頁去重；同毫秒溢出 → partial＋有界終止；短頁 → complete 或 partial（依門檻）；亂序頁 → invalid 不推進；增量模式沿用 synced_through 重疊。

### Task 2.3 @sdd：`config.py` 加 `explore_db_path`（`FILET_EXPLORE_DB`；<!-- 2026-09-20 裁決：P2 階段**可選**、未設→None 不建 store，避免正式機先加 env；P3 上線（Task 3.4）起改必填 -->）；`run_api.py` 建 `ExploreStore` 注入 `create_app(explore_store=...)`；`/api/ops/health` 加 `explore_store` 統計；測試用 `tmp_path`。

**P2 驗收：** 三個新測試檔全綠；`uv run python -c "import sqlite3; print(sqlite3.sqlite_version)"` ≥ 3.24（UPSERT）。

---

## P3 分層更新（任務卡）

### Task 3.1 @inline：`explore_scheduler.py`

<!-- 2026-09-20 展開（2.1/2.2/2.3 完成後）：介面與 tick 演算法釘死，builder 不需再做設計判斷。 -->

```python
class ExploreScheduler:
    """單 thread、逐 job 執行；每個 job＝一次 HL 呼叫（或一頁 fills）。所有持久化走 ExploreStore，
    所有 HL 呼叫走 `hl`（必須是 gateway.scoped("explore")，wait_s=0）。"""
    def __init__(self, *, store: ExploreStore, hl, leaderboard_source_fn, excluded_fn,
                 cfg: ExploreConfig, now_fn, sleep_fn, on_dirty: Callable[[], None],
                 owner: str = "api", lease_s: float = 60.0,
                 candidates_every_s=600, state_every_s=900, portfolio_every_s=3600,
                 fills_every_s=14400, hot_rank=50, jitter_pct=0.10, rng=random.random)
    def tick(self) -> str      # 'idle'|'ran:<kind>'|'no_budget'|'paused'|'rate_limited'|'retry'|'quarantined'
    def run_forever(self, stop: threading.Event) -> None
    def status(self) -> dict   # last_tick_at, last_result, ticks, queue_depth, oldest_due_age_s, per-result counters
```

`tick()` 演算法：
1. 首次：`store.enqueue("candidates:candidates", None, "candidates", 0, now)`（去重，已存在不動）。
2. `job = store.claim_due(now, owner, lease_s)`；`None` → 回 `idle`。
3. 依 `job.kind`：
   - `candidates`：`payload = leaderboard_source_fn()`；`None` → `reschedule(now+60, err="no payload")`。否則 `rows = candidate_addresses(payload, cfg.candidate_pool, excluded)`（既有純函式，回 `[(address, display_name)]`；rank＝list 索引＋1，roi 從 payload row 取 `_roi_sort_key`），`store.upsert_candidates(...)`、`deactivate_missing(seen)`；對每個 active 地址 `enqueue(f"{addr}:state", addr, "state", 0, now + spread(addr, state_every_s))`、`portfolio`（priority 1）、`fills`（rank ≤ hot_rank → priority 2，否則 3；`next = now + spread(addr, fills_every_s)`）。`spread(addr, T) = (int(addr[-8:], 16) % T)`（首次到期分散，spec §6）。準入上限：`store.stats()` 的 `refresh_job` 列數 > 4 × active 數 → 本輪不再新增、log 一行。`complete(job)`；`enqueue("candidates:candidates", ..., now + candidates_every_s)`。
   - `state`：`payload = hl.clearinghouse_state(addr)` → `put_cache_ok(addr, "clearinghouseState", payload, now, now + jit(state_every_s))`；`complete`；`enqueue(同 key, next=refresh_after)`；`on_dirty()`。
   - `portfolio`：同上，`hl.portfolio(addr)` → endpoint `"portfolio"`，週期 `portfolio_every_s`。
   - `ledger`（<!-- 2026-09-20 追加，供 3.3 詳情頁讀本地 -->）：`hl.non_funding_ledger_updates(addr, 0)` → endpoint `"ledger"`，priority 1，週期 `ledger_every_s=3600`。容量：state 40＋portfolio 100＋ledger 100＝240／分鐘，fills 用剩餘 60；準入上限 5×active。
   - `fills`：`st = store.get_sync(addr)`；`plan = plan_page(st, address=addr, now_ms=now*1000)`；`plan.is_noop` → `complete`＋`enqueue(next = plan.state.window_end_ms/1000 + fills_every_s)`。否則 `page = hl.get_fills_page(addr, plan.start_ms, plan.end_ms)`（Task 3.2）；`res = apply_page(plan, page, now_ms=...)`；`store.insert_fills_page(addr, res.accepted, res.state)`；`on_dirty()`；`res.done` → `complete`＋`enqueue(next = now + jit(fills_every_s))`；未完成 → `reschedule(next=now, bump_attempts=False)`（讓位給其他 job，下一 tick 再續）。
   `jit(T) = T * (1 + (rng()*2-1) * jitter_pct)`。
4. 例外分類（每個 kind 共用一個 `_run_job` 包裝）：
   - `BudgetExhausted` → `reschedule(now+5, bump=False)` → `no_budget`。
   - `ScopePaused as e` → `reschedule(max(now+5, e.until_wall))`，其中 `e.until` 是 limiter 時基（monotonic）——**不要**拿它當 wall clock；改讀 `hl` 的 limiter `snapshot()["paused_remaining_s"]["explore"]`（若 `hl` 沒有 limiter 屬性就用 60）→ `paused`。
   - `is_rate_limited(e)` → `reschedule(now+60)` → `rate_limited`（gateway 已 `note_429`）。
   - `ConnectionError`／`TimeoutError`／`httpx.HTTPStatusError`（5xx）→ `attempts+1`，`next = now + min(30 * 2**attempts, 900) + rng()*10` → `retry`。
   - 其他 `Exception`（4xx 非 429、資料形狀錯）→ `put_cache_error(addr, endpoint, repr(e), now, now+86400)`（fills kind 則寫 `fills_sync.last_error`：用 `insert_fills_page(addr, [], replace(st, last_error=...))` 或 store 提供的 `set_sync_error`——2.1 沒有就在本 task 加一個最小方法）＋`reschedule(now+86400)` → `quarantined`。
   - `lease` 過期後被別人領走的 `complete`／`reschedule` 回 False → log warning，不重試。
5. `run_forever`：`while not stop.is_set(): r = tick(); sleep_fn(1.0 if r in ("idle","no_budget","retry") else 5.0 if r == "paused" else 0.0)`；每個 tick 結尾呼叫 `self._on_tick()`（P4 掛 `publisher.maybe_publish`；預設 no-op）。所有例外在 `run_forever` 層 `except Exception: logger.exception(...)` 後繼續（scheduler thread 不得因單一 bug 死掉；spec §3 條件五）。

驗收測試（`tests/test_explore_scheduler.py`，fake clock、FakeHL 計數、`ExploreStore(tmp_path)`）：
- 首 tick 只建 candidates job；第二 tick 跑 candidates 後每個地址有 state／portfolio／fills 三個 job，`next_attempt_at` 分散（不全等於 now）。
- 300 地址初次 state＋portfolio（6,600 權重）在 `WeightLimiter(scope_caps={"explore":300})` 下：逐 tick 驅動 fake clock，任一 60 秒切片 `used["explore"] ≤ 300`，且完成時間 ≥ 22 分鐘（spec §6）。
- `ScopePaused` 期間 tick 回 `paused` 且零上游呼叫。
- fills 三頁滿頁＋一短頁：分 4 個 tick 完成，期間另一地址的 state job 有機會執行（優先級 0 先於 fills 的 2/3）。
- 重啟：同一 `tmp_path` 新建 scheduler，`get_sync` 的 cursor 續接，不從頭。
- 候選進出：第二輪 candidates 少了一個地址 → 該地址 `active=0` 但 cache／fills 仍在；多一個地址只新增它的三個 job。
- `hl.portfolio` 拋 `RuntimeError("bad shape")` → `quarantined`，`endpoint_cache.last_error` 有值、`payload` 仍 NULL、job 的 `next_attempt_at ≈ now+86400`。
- `run_forever` 內 tick 拋任意例外不會結束 thread（用 stop event 與計數驗）。

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

<!-- 2026-09-20 展開。scheduler（3.1）已含 kind：state／portfolio／ledger／fills。 -->

**Files:** `src/spark/publicapi/app.py`（`_cached_trader_data` 約 2443-2557、`public_trader_detail` 約 2540-2650）；`tests/test_public_traders.py`。

- **判定池內**：`explore_store is not None` 且 `store.get_cache(addr, "portfolio")` 存在且 `payload is not None` → 本地路徑；否則（未注入 store、非候選、候選但尚未抓到 portfolio）→ 既有 upstream 路徑（interactive scope、經 limiter，行為不變）。
- **本地路徑**（不打 HL、不進 `_trader_portfolio_cache`）：
  - `rows = cache.portfolio.payload`；`ch_state = cache.clearinghouseState.payload`（可 None → `account_value=None`）；`deposit = sum_ledger_deposits(cache.ledger.payload)`（cache 缺或 payload None → `deposit=None`）；
  - `fills = store.get_fills(addr, now_ms-30d, now_ms)`；`sync = store.get_sync(addr)`；`fills_truncated = (sync is None) or sync.completeness != "complete"`；`fills_coverage = {"state": sync.completeness if sync else "backfilling", "observed_from": sync.observed_from_ms, "observed_to": sync.observed_to_ms, "reason": sync.reason}`（sync None → 三欄 None）。
  - **stale → 入列刷新、不等待**：對 `("portfolio","clearinghouseState","ledger")` 逐一：`entry is None or now >= entry.refresh_after` → `store.enqueue(f"{addr}:{kind}", addr, kind, 1, now)`（kind 對應 `portfolio`／`state`／`ledger`；去重由 store 保證）；`sync is None or now - sync.updated_at >= 4h` → `enqueue(f"{addr}:fills", addr, "fills", 2, now)`。準入：`store.stats()` 的 refresh_job 數 ≥ 5 × active 候選數 + 20 → 不入列、log warning。任一入列 → `refreshing=True`。
  - 回應新增鍵（`public_trader_detail`）：`source: "local"|"upstream"`、`refreshing: bool`、`as_of: {portfolio, state, ledger, fills}`（wall epoch 秒或 None；fills 取 `sync.updated_at`）、`fills_coverage`（upstream 路徑：`{"state": "complete" if not fills_truncated else "partial", "observed_from": None, "observed_to": None, "reason": "page_cap" if fills_truncated else None}`）。既有鍵一個不改。
  - `_cached_trader_data` 回傳 tuple 已有 6 欄；本地路徑需要多回 `coverage`／`as_of`／`refreshing`／`source` → 改回一個小 dataclass `TraderData`（既有 6 欄＋4 新欄），呼叫端同步改；upstream 路徑填 `source="upstream"`、`refreshing=False`、`as_of` 用 `now`。
- **測試**（`tests/test_public_traders.py`，沿既有 `make_app`／FakeHL；`create_app(..., explore_store=ExploreStore(tmp_path/"e.db"))`）：
  1. 池內且新鮮：先用 store 塞 portfolio／state／ledger 快取（`put_cache_ok`，`refresh_after=now+3600`）與兩筆 fills＋`insert_fills_page` 的 complete sync → 1000 次 GET 零上游呼叫、`source=="local"`、`refreshing is False`、`fills_coverage.state=="complete"`、`as_of.portfolio` 等於塞入的 `fetched_at`。
  2. 池內但 portfolio stale（`refresh_after=now-1`）→ 200、`refreshing is True`、`store.stats()` 多恰好 1 個 job（key `addr:portfolio`），連打 50 次仍只有 1 個。
  3. 池內但只有 portfolio、無 state／ledger／sync → 200、`account_value is None`、`deposit is None`、`fills_coverage.state=="backfilling"`、三個對應 job 入列。
  4. 非候選地址 → `source=="upstream"`、走既有路徑（既有測試全部不變）。
  5. 準入上限：預先塞滿 job → `refreshing is False` 且不新增。

### Task 3.4 @inline：接線與 D6 清理

<!-- 2026-09-20 展開；在 4.1 之後做（同改 hl_explore.py）。 -->

**Files:** `src/spark/publicapi/config.py`、`scripts/run_api.py`、`src/spark/publicapi/app.py`、`src/spark/publicapi/hl_explore.py`；`tests/test_publicapi_config.py`、`tests/test_api_ops.py`、`tests/test_public_explore.py`、`tests/test_run_api_wiring.py`（新，若無既有）。

1. **config**：`explore_upstream_refresh: bool`（env `EXPLORE_UPSTREAM_REFRESH`，`"1"/"true"` → True，預設 False）；`explore_db_path` 在 `explore_upstream_refresh=True` 時**必填**（缺 → `ValueError`，訊息含兩個 env 名）。
2. **run_api.py**：`explore_store` 存在時建 `ExplorePublisher(store=..., index=app.state.explore_index, cfg=hl_explore.ExploreConfig.from_env(), now_fn=time.time, snapshot_path=cfg.explore_cache_path)` 與 `ExploreScheduler(store=..., hl=gateway.scoped("explore"), leaderboard_source_fn=<同 app 內 `_leaderboard_cache.get`——把它暴露成 `app.state.leaderboard_get`>, excluded_fn=<同 app 內 `_explore_excluded_addresses`——暴露成 `app.state.explore_excluded_fn`>, cfg=..., now_fn=time.time, sleep_fn=time.sleep, on_dirty=publisher.mark_dirty, on_tick=publisher.maybe_publish)`；`app.state.explore_scheduler`／`app.state.explore_publisher`；`cfg.explore_upstream_refresh` 為 True 才 `threading.Thread(target=scheduler.run_forever, args=(stop_event,), daemon=True, name="explore-scheduler").start()`，並在 uvicorn 結束後 `stop_event.set()`（`try/finally` 包 `uvicorn.run`）。**不在 `create_app` 內起 thread**（測試建 app 不得有背景執行）。
3. **app.py**：`create_app` 不新增 thread；`/api/ops/health` 加 `"explore_refresh": {"enabled": cfg.explore_upstream_refresh, **scheduler.status()} if scheduler else None` 與 `"explore_publisher": publisher.status() if publisher else None`（讀 `app.state`，未注入 → null）。
4. **D6 清理（hl_explore.py）**：刪 `build_sync`、`_maybe_trigger_build`、`_enrich_one`、`_call_hl`、`_RateLimitedAbort`、`_BudgetUnavailable`、`_enrich_cache`／`_enrich_cache_max`／`_enrich_ttl_s`／`_building`、建構子的 `hl`／`sleep_fn`／`leaderboard_source_fn`／`excluded_fn`／`cfg` 中不再被讀路徑使用的參數（`query()` 仍需 `cfg` 做 qualify 門檻；`candidate_addresses` 純函式保留給 scheduler）；`query()` 的 `building` 改由 `initializing` 推導。`app.py:2325` 的 `ExploreIndex(...)` 建構改為新簽名。
5. **測試**：`tests/test_public_explore.py` 中測 build_sync／429 中止／enrich 快取的測試刪除（功能已由 scheduler／publisher 測試覆蓋——回報列出刪了哪些與對應的新測試名）；query／qualify／sort／snapshot 測試保留。config 新增 3 條（預設 False、開啟需 db path、開啟且有 db path 通過）。ops/health 新增 2 條（未注入 null；注入 scheduler＋publisher → 兩個 dict 含 `enabled`／`last_tick_at`／`dirty` 等鍵）。`run_api` 接線：若無既有測試檔，加 `tests/test_run_api_wiring.py` 用 monkeypatch 把 `uvicorn.run` 換成 no-op、env 設 `EXPLORE_UPSTREAM_REFRESH=1`＋`FILET_EXPLORE_DB=tmp`，斷言 `threading.enumerate()` 內出現名為 `explore-scheduler` 的 thread（或斷言 `Thread.start` 被呼叫一次），且 `EXPLORE_UPSTREAM_REFRESH` 未設時沒有。
6. **驗收**：`rg -n "build_sync|_enrich_one|_call_hl|_RateLimitedAbort|_BudgetUnavailable|_maybe_trigger_build" src` 零命中；`rg -n "explore-scheduler" scripts/run_api.py` 命中；全測試綠；ruff 乾淨；`uv run python -c "import scripts.run_api"` OK。

---

### Task 3.5 @inline：P2–P5 reviewer 修正（2026-09-20 opus 審查：2 Critical／5 Warning／4 Suggestion）

<!-- 裁決：C1 候選退池 job 洩漏（預算漏、準入卡死、purge 失效）→ 退池即刪 job＋續排前查 active。
C2 第一次發布以全空 portfolio 列蓋掉現役榜單並覆寫 v3 快照 → 發布門檻（80% 候選有 portfolio 才換版；
從未有版本例外）＋首次覆寫前備份 .v3.bak。W1/W2 緊迴圈 → 例外後 sleep、失敗也節流。W3 詳情頁每請求
COUNT 五表 → 專用兩個 COUNT。W4 PAGE_LIMIT 同源。W5 db 0600。S1 暫停不計 attempts。S3 docstring。
S4 candidates 週期 1800s（36MB 下載每 30 分鐘一次）。S2（地址小寫）接受、記觀察。 -->

**Files:** `explore_store.py`、`explore_scheduler.py`、`explore_publisher.py`、`explore_fills_sync.py`、`hl.py`（docstring）、`app.py`（`_local_trader_data`）、`deploy/RUNBOOK.md` §5.8e；對應測試。

- [ ] **A. store**：`delete_jobs(address) -> int`；`is_active(address) -> bool`；`admission_counts() -> tuple[int, int]`（refresh_job 數、active 候選數，兩個 COUNT）；`count_with_payload(endpoint) -> int`；`__init__` 連線後 `os.chmod(db_path, 0o600)`（best effort）。
- [ ] **B. scheduler**：(1) `_run_candidates` 在 `deactivate_missing(seen)` 後對每個退池地址 `delete_jobs`；(2) `_run_cache_kind`／`_run_fills` 續排前 `if not store.is_active(job.address): complete(job); return "dropped"`；(3) `run_forever` 的 `except` 後 `self._sleep(1.0)`；(4) `ScopePaused` → `bump_attempts=False`；(5) `candidates_every_s` 預設 1800，docstring 註明 stats-data 36MB 每 30 分鐘至多一次；(6) 準入計數改用 `admission_counts()`。
- [ ] **C. publisher**：(1) 建構子加 `min_portfolio_ratio: float = 0.8`；`maybe_publish` 在 compose **之前**：`with_pf = store.count_with_payload("portfolio")`、`n = len(active)`（用 `admission_counts()[1]`）；若 `index.status()["rows"] is not None and with_pf < min_portfolio_ratio * n` → 不發布、`_gate_skips += 1`、`_last_gate = f"{with_pf}/{n}"`、`_last_attempt_at = now`、回 False（`_dirty` 保留）；index 從未有版本（`rows is None`）→ 不套門檻；(2) 節流改看 `_last_attempt_at`（成功與失敗都更新），`_last_published_at` 只在成功時更新（status 用）；(3) 首次以 v4 覆寫既有快照前：若檔案存在且 `load_snapshot` 判為 v3 來源（或直接讀 JSON `version == 3`）且 `<path>.v3.bak` 不存在 → `shutil.copyfile` 備份一次；(4) `status()` 加 `gate_skips`、`last_gate`、`last_attempt_at`。
- [ ] **D. fills_sync**：`PAGE_LIMIT` 改 import 自 `hl.py` 使用的同一常數來源（`rg -n "USER_FILLS_PAGE_LIMIT" src/spark/publicapi/hl.py` 看它從哪 import），刪本地定義。
- [ ] **E. app.py `_local_trader_data`**：準入判定改 `admission_counts()`，不再呼叫 `active_candidates()`＋`stats()`。
- [ ] **F. hl.py `get_fills_page` docstring**：刪「含 aggregateByTime 設定」（請求體沒有這個欄位；欄位名是假設不是事實）。
- [ ] **G. RUNBOOK §5.8e**：加三行——開 flag 前 `sudo cp explore_index.json explore_index.json.pre-refresh.bak`；冷啟動預算約 12,600 權重（state 2＋portfolio 20＋ledger 20 × 300）≈ 42 分鐘才會第一次換版；換版門檻＝80% 候選已有 portfolio，之前 `/api/public/explore` 續端舊快照、`explore_publisher.gate_skips` 會遞增屬正常。
- [ ] **H. 測試**：store 四個新方法＋0600；scheduler：退池地址 job 全刪、之後零上游、`purge(now+30d)` 能刪它、準入不再卡死、`run_forever` 例外後有 sleep（sleep_fn 記錄）、`ScopePaused` 不計 attempts；publisher：門檻擋下（index 有版本＋10% portfolio → False、`gate_skips==1`、index 未變、快照未寫）、無版本不套門檻、80% 以上通過、失敗路徑 60s 內不重算（compose 計數）、v3 備份檔存在且只備一次；fills_sync 常數同源（`PAGE_LIMIT is USER_FILLS_PAGE_LIMIT`）；traders 既有五條綠。
- [ ] **I. Commit** `fix: P2–P5 審查修正（job 生命週期、發布門檻＋v3 備份、緊迴圈、準入 COUNT、常數同源、db 0600）`

### Task 3.6 @inline：3.5 複審修正（2026-09-20 opus 複審：1 Critical／3 Warning／4 Suggestion）

<!-- 裁決：C1 門檻 n==0 旁路 → 有現役版本時 with_pf==0 或 n==0 一律不換版；候選輪拿到空 rows 視為失敗不停用任何人。
W1 非候選地址多頁 fills 續頁被 dropped → 續頁不看 active，只在整輪 done 後決定是否排下一輪。W2 WAL/SHM 側檔 0644 →
連線後 chmod 側檔。W3 補「job 地址不在 candidate 表」測試。S1 發布順序（先快照後換版）寫回：接受。S2 force 繞過門檻＋
status 露 min_portfolio_ratio。S3 備份失敗 log。S4 count_with_payload 用 COUNT(DISTINCT address)。 -->

**Files:** `explore_publisher.py`、`explore_scheduler.py`、`explore_store.py`；`tests/test_explore_publisher.py`、`tests/test_explore_scheduler.py`、`tests/test_explore_store.py`。

- [ ] **A. publisher 門檻**：`has_version = index.status()["rows"] is not None`；`gate_blocked = has_version and (n == 0 or with_pf == 0 or with_pf < ratio * n)`；`force=True` 繞過門檻（但仍走節流以外的所有步驟）；`status()` 加 `min_portfolio_ratio`；備份讀檔／解析失敗 `logger.warning` 一行後繼續。發布順序維持「先 dump_snapshot 再 set_published」（3.5 實作的偏離，接受並記錄：快照沒落地就不換記憶體版本）。
- [ ] **B. scheduler**：(1) `_run_candidates`：`candidate_addresses(...)` 回空 list → 不呼叫 `upsert_candidates`／`deactivate_missing`，`reschedule(now+60, err="empty candidate rows")`、回 `retry`（候選來源壞掉不能等於「所有人退池」）；(2) `_run_fills`：`res.done is False`（續頁）→ 直接 `reschedule(next=now, bump_attempts=False)`，**不看** `is_active`；只有 `res.done` 後排下一輪前才查 `is_active`，非 active → `complete`、回 `dropped`。`_run_cache_kind` 維持 3.5 行為。
- [ ] **C. store**：`__init__` 在 PRAGMA 與建表後，對 `db_path`、`db_path + "-wal"`、`db_path + "-shm"` 存在者逐一 `os.chmod(0o600)`（best effort）；`count_with_payload` 改 `COUNT(DISTINCT e.address)`。
- [ ] **D. 測試**：publisher：有版本＋`n==0` → 不發布、`gate_skips+1`、快照未寫；有版本＋`with_pf==0` 同；`force=True` 在 10% 下仍發布；`status()["min_portfolio_ratio"] == 0.8`。scheduler：非候選地址（未 `upsert_candidates`）的 fills job 三頁滿頁 → 三個 tick 都續頁（`pages_done` 到 3），第四 tick 短頁 done 後才 `dropped`；候選輪空 rows → 300 候選仍 active、job 數不變、tick 回 `retry`。store：`explore.db-wal`／`-shm` 若存在為 0600（用一次寫入觸發 WAL 後檢查）；`count_with_payload` 對同地址兩個 `params_fp` 只算 1。
- [ ] **E. Commit** `fix: 3.5 複審修正（門檻 n==0/with_pf==0 不換版、候選空 rows 不停用、續頁不看 active、WAL 側檔 0600、force 繞過門檻）`

### Task 3.7 @inline：3.6 複審修正（2026-09-20 opus 第三輪：1 Critical／2 Warning／2 Suggestion）

<!-- 裁決：C 門檻量輸入（payload 數）、換版看輸出（compose 列數）不同源 → 門檻改判在 compose 輸出：有版本時
rows 為空或 meta.with_portfolio < ratio×candidates 一律不換版；輸入端預檢只當省算的捷徑。每次覆寫快照前保留 .prev
（v3.bak 只保護第一次）。W1 候選來源長期壞掉無外部證據 → warning log＋status last_candidates_ok_at。
W2 count_with_payload 與 compose 基礎不同（params_fp）→ 限 params_fp=''。S1 單輪續頁加硬上限 20 頁→partial/page_cap。 -->

**Files:** `explore_publisher.py`、`explore_scheduler.py`、`explore_store.py`、`explore_fills_sync.py`；對應測試。

- [ ] **A. publisher**：compose 之後、換版之前：`if has_version and not force and (not rows or meta["with_portfolio"] < ratio * max(meta["candidates"], 1) or meta["with_portfolio"] == 0)` → `gate_skips+1`、`last_gate=f"out:{meta['with_portfolio']}/{meta['candidates']}"`、`_last_attempt_at=now`、回 False（不寫快照、不換版）。輸入端預檢保留（同條件的捷徑，`last_gate` 用 `in:` 前綴）。`force=True` 仍繞過兩道門檻。每次 `dump_snapshot` 前：若目標檔存在 → `shutil.copyfile(path, path + ".prev")`（best effort、warning on failure）；`.v3.bak` 邏輯保留。
- [ ] **B. scheduler**：空 rows → `logger.warning("explore scheduler: 候選來源回空 rows（%s），60s 後重試；候選池維持不動", ...)`；`status()` 加 `last_candidates_ok_at`（成功 upsert 的 wall 時間，None 表示從未）與 `candidates_empty_streak`（連續空 rows 次數，成功歸零）。
- [ ] **C. store**：`count_with_payload(endpoint)` 加 `AND e.params_fp = ''`（與 `compose_rows` 讀的同一基礎，工程原則 #1），維持 `COUNT(DISTINCT e.address)`。
- [ ] **D. fills_sync**：`apply_page` 加參數 `max_pages_per_round: int = 20`；`state.pages_done + 1 >= max_pages_per_round` 且仍滿頁 → `completeness="partial"`、`reason="page_cap"`、`synced_through=cursor`、`done=True`（20 頁＝40,000 筆 > 留存上限 10,000，正常資料到不了）。
- [ ] **E. 測試**：publisher：`with_pf/n = 10/10` 但 compose 出 0 列（monkeypatch `compose_rows` 回 `([], {"published_at":..,"candidates":10,"with_portfolio":0})`）→ 不換版、快照未寫、`last_gate` 以 `out:` 開頭；有版本且 compose 正常 → 換版且 `.prev` 存在、內容＝前一版；`force` 仍發布。scheduler：空 rows 三次 → `candidates_empty_streak 3`、`last_candidates_ok_at None`，成功一次後歸零並有時間。store：`params_fp='x'` 有 payload、`''` 無 → 計 0。fills_sync：連續 20 滿頁 → 第 20 頁 `partial/page_cap/done`。
- [ ] **F. Commit** `fix: 3.6 複審修正（門檻判 compose 輸出、快照 .prev、候選空 rows 可觀測、count 同基礎、續頁硬上限）`

## P4 漸進發布（任務卡）

### Task 4.1 @inline：`explore_publisher.py`

<!-- 2026-09-20 展開。順序：4.1 在 3.4（刪舊 build_sync）之前做，兩者都改 hl_explore.py 故不平行。 -->

```python
# src/spark/publicapi/explore_publisher.py
def compose_rows(store: ExploreStore, *, now: float, cfg: ExploreConfig,
                 fills_window_days: int = FILLS_WINDOW_DAYS) -> tuple[list[ExploreRow], dict]:
    """純函式（只讀 store，零網路）。對每個 active 候選：
    portfolio = get_cache(addr,"portfolio").payload or None；ch_state 同理；
    fills = get_fills(addr, now_ms-window, now_ms)；sync = get_sync(addr)；
    coverage = {"state": sync.completeness if sync else "backfilling", "observed_from": ..., "observed_to": ..., "reason": ...}
    row = enrich_candidate(addr, display_name, portfolio, fills, ch_state,
                           fills_truncated=(coverage["state"] != "complete"),
                           as_of={"portfolio": pf.fetched_at, "state": st.fetched_at, "fills": sync.updated_at}, fills_coverage=coverage)
    portfolio 為 None → 仍出列：windows 全 None、live_days None（qualify 會因 live_days None 不合格→這是「分析待完成」的正確狀態，不是 0）。
    rows = _apply_tags(rows, cfg)；meta = {"published_at": now, "candidates": n, "with_portfolio": k,
    "coverage_counts": Counter(state), "as_of_oldest": min fetched_at or None}。"""

class ExplorePublisher:
    def __init__(self, *, store, index: ExploreIndex, cfg, now_fn, snapshot_path: str | None,
                 min_interval_s: float = 60.0)
    def mark_dirty(self) -> None                 # scheduler 的 on_dirty
    def maybe_publish(self, *, force: bool = False) -> bool
        # not dirty and not force → False；now - last_published < min_interval and not force → False
        # rows, meta = compose_rows(...)；index.set_published(rows, meta)；snapshot_path → dump_snapshot(v4)；dirty=False；True
        # compose 或 dump 例外 → logger.error，保留 index 舊版（spec §9.2：整體失敗保留最後成功版本），回 False
    def status(self) -> dict                     # last_published_at, dirty, publishes, failures, last_error
```

`hl_explore.py` 改動：
- `enrich_candidate(address, display_name, portfolio_raw, fills, ch_state, *, fills_truncated=False, as_of=None, fills_coverage=None)`：`portfolio_raw is None` → `windows={k: None}`、`live_days=None`；`ch_state is None` → `account_bucket=None`、`exposure_dir=None`、`exposure_pct=None`；`fills` 為空 list 照常算（0 筆是合法值）。
- `ExploreRow` 新增 `as_of: dict[str, float | None]`（預設 `{}`）與 `fills_coverage: dict`（預設 `{"state": "backfilling", "observed_from": None, "observed_to": None, "reason": None}`）；`to_dict()` 輸出兩鍵；`fills_truncated` 保留＝`fills_coverage["state"] != "complete"`（前端相容）。
- `EXPLORE_INDEX_VERSION = 4`；`dump_snapshot` 寫 `as_of`／`fills_coverage`；`load_snapshot` 讀到 `version == 3` → 逐列補 `as_of = {portfolio: built_at, state: built_at, fills: built_at}`、`fills_coverage = backfilling` 後**當作 v4 載入**（D7：不丟棄舊快照）；其他版本 → None（既有語意）。
- `ExploreIndex.set_published(rows, meta)`：持鎖設 `_rows`、`_rows_version=EXPLORE_INDEX_VERSION`、`_built_at=meta["published_at"]`、`_total_scanned=meta["candidates"]`、`_meta=meta`。
- `query()` 回應加 `published_at`（＝`_built_at`）、`initializing`（＝`_rows is None`）、`coverage_counts`（meta，無則 `{}`）；`building` 保留＝`initializing`（前端相容）。

驗收測試（`tests/test_explore_publisher.py`）：
1. 單地址只有 portfolio 快取、無 state／fills → 出列、`exposure_pct is None`、`fills_coverage.state=="backfilling"`、`order_count_30d==0`。
2. 完整地址（三種快取＋complete sync）→ `to_dict()` 含 `as_of`（三鍵等於塞入的 fetched_at）與 `fills_coverage.state=="complete"`、`fills_truncated is False`。
3. `maybe_publish`：未 dirty → False；dirty 但 60s 內第二次 → False；`force=True` → True。
4. compose 途中 store 拋例外（monkeypatch `get_fills`）→ 回 False、`index.query()` 仍是上一版、`status().failures==1`。
5. v3 快照檔（用既有 `dump_snapshot` 的 v3 形狀手寫 JSON）→ `load_snapshot` 回 v4 結構、每列 `as_of.portfolio == built_at`、`fills_coverage.state == "backfilling"`；`query()["published_at"]` 等於 built_at、`initializing is False`。
6. 快照落檔用 `safe_fs.write_json_atomic`（或既有 `dump_snapshot` 已是原子寫；讀它確認並在測試斷言目錄內無殘留 tmp 檔）。
7. `tests/test_public_explore.py` 既有測試全綠（`to_dict` 多鍵不破壞既有斷言；若有 `==` 整 dict 比對的斷言，改為包含式比對並在回報說明）。
- `enrich_candidate` 改為接受 `portfolio_raw=None`／`ch_state=None`：對應欄位（`windows`、`live_days`／`account_bucket`、`exposure_*`）為 None，不當成 0；`fills_stats` 吃 store 的 fills（30 天窗）＋`coverage`；`ExploreRow` 新增 `as_of: dict[str, float | None]`、`fills_coverage: dict`（`to_dict` 輸出，D5），`fills_truncated` 保留＝`coverage.state != "complete"`（前端相容）。
- `EXPLORE_INDEX_VERSION` 3→4；`load_snapshot` 讀到 v3 → 轉為 v4（`as_of` 全＝`built_at`、coverage backfilling，D7），不丟棄。
- `query()` 回應加 `published_at`、`initializing: bool`（＝從未有版本）；`building` 保留＝`initializing`（前端相容）。
- scheduler 的 `notify_dirty` → publisher；publisher 由 scheduler thread 每 tick 末呼叫 `maybe_publish()`。
- 驗收：`tests/test_explore_publisher.py`——單地址無 portfolio 仍出列且 windows 全 None；來源整體失敗保留上一版；v3 快照可讀且 `as_of` 正確；`published_at` ≠ 欄位 `as_of`；原子寫（`safe_fs.write_json_atomic`）。

### Task 4.2 @sdd：前端 `web/src/lib/publicApi.ts` 型別加 `as_of`、`fills_coverage`、`published_at`、`initializing`（optional）；explore／traders 頁的「成交資料可能不完整」提示改讀 `fills_coverage.state !== "complete"`（`fills_truncated` 為 fallback）；vitest 對應測試更新。

---

## P6 資料語義修正（2026-09-20 使用者裁決；恢復發布的前置）

<!-- 背景：第二次部署開 flag 後主線程發現「成交回補未完成＝0 筆＝落榜」會讓首次換版把公開榜從 19 列砍到個位數，
15:11 UTC 先關 flag 保住現役榜。使用者裁決：不用數量門檻掩蓋資料語義問題。 -->

**使用者裁決（逐字精神）**
- D12 取消 portfolio 覆蓋率與「新版列數 ≥ 舊版 80%」兩種發布門檻。合格人數真的下降就讓榜縮小甚至為空。發布只檢查：來源取得成功、資料格式與組版成功；來源失敗保留舊版；來源有效但結果為空則正常發布。
- D13 缺成交採 None、**不沿用舊成交統計**（舊值是不同 30 天窗口）。資格三態：`eligible`（成交完整且符合條件）／`pending`（成交不足以判定，列表可見、標「資格待確認」）／`ineligible`（已有充分資料證明不符合）。`qualify(None)` 不得回傳合格；其他已知條件明確不合格時不因成交未知而放行。預設頁同時顯示合格與待確認但分組；勝率排名只對可比較資料排序，待確認放後面、不給名次；「僅合格」查詢不混入 pending。
- D14 探索頁提示必做：coverage 非 complete 時勝率、成交推導的幣種／集中度顯示「分析待完成」，不產生 `concentrated`。詳情頁可保留已觀測統計但標明範圍。
- D15 觀測補齊：等待時間按 scope p50/p95、HTTP 計數含重試與 timeout、dashboard 延遲附樣本數。**follower 引擎的限流關係是未結案項目**：同主機同出口 IP、獨立計數；publicapi 零 429 不證明引擎受保護；本輪不改引擎程式，只在文件與 health 明示。
- D16 恢復發布前三個重現驗收（Task 6.4）；之後觀察連續兩次更新（pending 逐步轉合格／不合格、各欄位時間正確）才開始 24 小時觀測期；預算不動。

### P6 契約（2026-09-20 使用者要求，前後端共同遵守；先於實作固定）

**A. 列（`ExploreRow.to_dict()`）**
| 欄位 | 型別 | 語義 |
|---|---|---|
| `eligibility` | `"eligible"｜"pending"｜"ineligible"` | 由後端 `classify` 決定；列表 API **不回傳** ineligible 列 |
| `eligibility_reason` | string｜null | `live_days`／`max_dd`／`min_fills`／`concentration`／`portfolio_missing`／`fills_unknown`／`enrich_error`；eligible 為 null |
| `fills_coverage` | `{state: "backfilling"｜"partial"｜"complete", observed_from: epoch_ms｜null, observed_to: epoch_ms｜null, reason: string｜null, synced_through: epoch_ms｜null, last_success_at: epoch_s｜null, window_start: epoch_ms｜null, window_end: epoch_ms｜null, params_fp: string｜null, evidence: {scan_id, kind, window_start, window_end, finished_at, reason, gap: bool, unknown: bool}｜null}`（後五鍵 Task 7.1／7.5；`evidence` Task 7.9b B6） | 成交資料完整性；`synced_through`＝已確認同步到的游標（回補中常落後）、`last_success_at`＝最近一次抓頁成功時間（＝`as_of.fills`，只證明「最近打過招呼」不證明「同步到哪」，2026-09-21 Task 7.1）；`state`／`reason` 為對外判定（Task 7.9b：`complete` 需同時 `result==complete AND gap==0 AND evidence_unknown==0`，否則降級為 `partial`／`coverage_gap` 或 `partial`／`evidence_unknown`；`reason` 值域同前——`count_below_retention_threshold`＝門檻推論、`retention_boundary_verified`＝實測起點前仍有可查成交、`count_below_retention_threshold_probe_empty`＝探過起點前一天無可查成交門檻推論成立但無法升級）；**`window_start`／`window_end`（Task 7.9b 起語義變更）改為增量軌覆蓋區間 `[inc_from, synced_through]`**（不再是「目前這一輪」的查詢區間）；`evidence` 回溯「建立目前 completeness 的那次全區間遍歷」——`kind`（initial｜partial_rescan｜verify）、`window_start`／`window_end` 是那次遍歷的查詢區間、`finished_at` 是完成時間、`reason` 是那次遍歷的結論、`gap` 為 true 代表那次遍歷沒有伸進增量軌起點（覆蓋不連續）、`unknown` 為 true 代表這是遷移前缺乏可追溯窗口證據、尚待核驗的舊資料；整個鍵可為 `null`（尚無任何完成的遍歷）。 |
| `as_of` | `{portfolio, state, ledger, fills}`，各 epoch 秒｜null | 各欄位**來源取得時間**，不是發布時間；缺該來源為 null |
| `close_win_rate_pct`、`concentration_pct`、`closed_positions_30d`、`realized_pnl_30d_usd` | number｜null | coverage ≠ complete 時**一律 null**（未知≠0） |
| `coins` | string[] | coverage ≠ complete 時 `[]` |
| `order_count_30d` | int | **已觀測筆數（下限）**；coverage ≠ complete 時只作下限用 |
| `live_days`、`windows[w]` | number／object｜null | portfolio 缺 → null |
| `fills_truncated` | bool | ＝`fills_coverage.state != "complete"`（相容） |
**前端不得**把 null 轉成 0、不得自行推算 eligibility、不得對 pending 列給名次。

**B. 列表回應（`GET /api/public/explore`）**：後端依序 **篩選 → classify → 分組（eligible 前、pending 後）→ 組內排序（sort key；次排序鍵 `address` 升冪，穩定）→ 分頁**；`page`／`page_size` 對合併後序列切片。回應：`rows`、`total_qualified`（＝eligible 數）、`total_pending`、`total_ineligible`、`published_at`（版本生成時間）、`initializing`、`coverage_counts`、`sort`／`order`、`eligibility`（echo）。`eligibility=eligible` 時 rows 只含 eligible。勝率排序：null 恆在組尾。

**C. 發布語義（publisher）**：
- 有效空結果：候選來源有效、組版成功、但 eligible 為 0 → **正常發布**（榜可為空）。
- 來源故障：`active_candidates` 為空（候選從未載入或全數停用）、或 compose 整體例外（store 讀取失敗）→ **保留最後成功版本**、`source_failures+1`、`last_skip_reason`。
- 單一地址 enrich 例外（payload 格式錯、timeout 留下的壞資料）→ 該列以 `pending/enrich_error` 出列、`meta.row_errors+1`、不阻擋其他列；**不得**把它靜默轉成空列或丟棄。
- 快照備份 `.prev`／`.daily`／`.v3.bak` 不變。

**D. 驗收順序**：各分支測試綠 → 合併到同一 commit → 在該 commit 跑 6.4 三案例＋前後端整合（本機起 API＋web，探索頁看得到兩組、僅合格切換、詳情頁範圍）→ 部署並確認 `DEPLOYED_VERSION` 等於該 commit → 觀察連續兩次更新 → 起算 24 小時。

### Task 6.1 @inline：三態資格、成交欄位 None、發布只看來源與組版

**Files:** `src/spark/publicapi/hl_explore.py`（`ExploreRow`、`enrich_candidate`、`qualify`→`classify`、`sort_rows`、`_apply_tags`、`ExploreIndex.query`）、`src/spark/publicapi/explore_publisher.py`、`src/spark/publicapi/app.py`（`/api/public/explore` 參數）；`tests/test_public_explore.py`、`tests/test_explore_publisher.py`。

1. **`ExploreRow`** 新增 `eligibility: str`（`eligible|pending|ineligible`）、`eligibility_reason: str | None`；`to_dict` 輸出。
2. **成交欄位語義**：`enrich_candidate` 在 `fills_coverage.state != "complete"` 時：`close_win_rate_pct=None`、`coins=[]`、`concentration_pct=None`、`closed_positions_30d=None`、`realized_pnl_30d_usd=None`；`order_count_30d` 保留已觀測筆數（下限，語意由 coverage 標示）。`complete` 時照舊。
3. **`classify(row, cfg, *, window, min_live_days, min_fills, max_dd_pct, max_concentration_pct) -> tuple[str, str | None]`**（取代 `qualify` 的布林；保留 `qualify()` 為 `classify(...)[0] == "eligible"` 的薄包裝供既有呼叫端）：
   - 已知條件先判：`live_days is not None and live_days < min_live_days` → `ineligible/live_days`；`windows[window].max_dd_pct is not None and 超過` → `ineligible/max_dd`；`coverage == complete`：`order_count_30d < min_fills` → `ineligible/min_fills`、`concentration_pct > max` → `ineligible/concentration`。
   - `coverage != complete`：`order_count_30d >= min_fills` 視為該條已滿足（下限）；否則該條「未定」。集中度未定，除非 `max_concentration_pct >= 100`（等於不過濾）。
   - 任一條 ineligible → `ineligible`；無 ineligible 但有未定（含 `live_days is None`／portfolio 缺）→ `pending/<哪一條未定>`；否則 `eligible`。
4. **`_apply_tags`**：`concentrated` 只對 `coverage == complete` 判；`low_drawdown` 不變。
5. **`sort_rows`**：先分組（eligible 在前、pending 在後、ineligible 不列），組內依 sort key；勝率排序時 `None` 恆在組尾。
6. **`ExploreIndex.query(..., eligibility: str = "all")`**：`"all"` → rows＝eligible＋pending（分組順序）；`"eligible"` → 只 eligible。回應加 `total_pending`、`total_ineligible`；`total_qualified` 維持＝eligible 數（前端相容）。`app.py` 端點加同名 query 參數（只接受 `all|eligible`，其他 400）。
7. **publisher**：刪 `min_portfolio_ratio`／`_gate_reason`／輸入輸出兩道門檻／`gate_skips`／`last_gate`。`maybe_publish`：`candidates = store.active_candidates()`；`len(candidates) == 0 and index 已有版本` → `source_failures += 1`、`last_skip_reason="no_active_candidates"`、保留舊版、回 False（來源失敗）；否則 compose → 快照備份（`.prev`／`.daily`／`.v3.bak` 不變）→ dump → `set_published`。compose 空列＝正常發布。`status()`：`source_failures`、`last_skip_reason`、`last_published_at`、`publishes`、`failures`、`last_error`、`last_attempt_at`、`dirty`。
8. **測試**：`classify` 三態各情境（含「已知不合格＋成交未知 → ineligible」、「全部已知通過＋成交未知 → pending」、「complete 但 min_fills 不足 → ineligible」、「partial 且 order_count ≥ min_fills 且集中度不過濾 → eligible？」→ 否，集中度未定→ pending，除非 `max_concentration_pct>=100`）；`sort_rows` 分組與 None 在尾；`query(eligibility="eligible")` 不含 pending；publisher：無門檻（10% portfolio 照發）、空候選保留舊版、compose 空列正常發布且 `total_qualified==0`。

### Task 6.2 @inline：探索頁分組、「分析待完成」標示、「僅合格」切換；詳情頁範圍

**Files:** `web/src/lib/publicApi.ts`、`web/src/lib/copy.ts`、`web/src/app/explore/page.tsx`（及其列元件）、`web/src/app/traders/[address]/page.tsx`；對應 vitest。

1. 型別：`ExploreRow.eligibility?`、`eligibility_reason?`；回應 `total_pending?`、`total_ineligible?`；`getPublicExplore` 加 `eligibility` 參數。
2. 探索頁：rows 依 `eligibility` 分兩組渲染（組標題文案鍵 `explore.groupEligible`／`explore.groupPending`，ZH「合格」／「資格待確認」、EN "Eligible"／"Pending review"）；pending 列不顯示名次。勝率、幣種、集中度相關欄在 `fillsIncomplete(row)` 時顯示「分析待完成」（文案鍵 `explore.analysisPending`）。新增「僅合格」切換（文案鍵 `explore.onlyEligible`），開啟時查詢帶 `eligibility=eligible`。
3. 詳情頁：coverage 非 complete 時，成交統計區塊標題附範圍文字（`observed_from`～`observed_to` 的日期，文案鍵 `trader.fillsObservedRange`），現有提示保留。
4. vitest：分組渲染、pending 無名次、分析待完成標示、僅合格切換帶參數、詳情頁範圍文字。`npm test` 全綠、`npm run build` 成功。

### Task 6.3 @inline：觀測補齊（6.1 之後，同改 app.py）

**Files:** `src/spark/publicapi/hl_budget.py`、`hl.py`、`app.py`；`tests/test_hl_budget.py`、`tests/test_hl_gateway_budget.py`、`tests/test_api_ops.py`；`deploy/RUNBOOK.md` §5.8e。

1. `WeightLimiter.reserve` 記錄每次等待秒數（per scope，rolling 最近 500 筆）；`snapshot()` 加 `wait_ms: {scope: {n, p50, p95, max}}`。
2. `HLGateway._info` 每次嘗試後記 `http_counts[scope][class]`，class ∈ `2xx|4xx|429|5xx|timeout|conn_error|budget_exhausted|scope_paused`（含重試每次各計）；掛在 limiter 上（`limiter.note_http(scope, cls)`），`snapshot()` 加 `http: {scope: {class: n}}`。
3. `app.py` middleware：對 `/api/me/dashboard` 記錄耗時（rolling 200 筆），`/api/ops/health` 加 `dashboard_latency: {n, p50_ms, p95_ms}`。
4. `/api/ops/health` 加 `follower_budget_note: "follower 引擎同主機同出口 IP、不經本限流器、獨立計數；本頁零 429 不證明引擎受保護；看 journalctl -u 'filet-follower@*'"`（常數字串，D15）。RUNBOOK §5.8e 同句。
5. 測試：等待統計、HTTP 計數含重試與 timeout、dashboard 延遲有 n。

### Task 6.5 @inline：契約 A 的輸出層遮罩（主線程整合檢查發現）

<!-- 2026-09-21 本機以正式機 v3 快照起 API 實測：pending 列（coverage=backfilling）仍輸出 close_win_rate_pct=13.33、
concentration_pct=28.6、coins=[...]——遮罩只在 compose 時做，遷移列與任何非 compose 來源的列沒經過。契約 A 要求
coverage≠complete 時這些欄位一律 null／[]。修法：遮罩移到唯一出口 ExploreRow.to_dict()（結構性，工程原則 #5），
compose 時的遮罩保留（雙保險）；tags 的 concentrated 同樣在輸出層剔除。 -->

**Files:** `src/spark/publicapi/hl_explore.py`（`ExploreRow.to_dict`、`load_snapshot` v3 遷移）；`tests/test_public_explore.py`、`tests/test_explore_publisher.py`。

1. `ExploreRow.to_dict()`：若 `fills_coverage["state"] != "complete"` → 輸出 `close_win_rate_pct=None`、`concentration_pct=None`、`closed_positions_30d=None`、`realized_pnl_30d_usd=None`、`coins=[]`、`tags` 去掉 `"concentrated"`；`order_count_30d` 保留（下限）。`complete` 照舊。
2. `load_snapshot` 的 v3→v4 遷移：列 dict 同樣遮罩（讓快照檔本身也符合契約，`.v3.bak` 不動）。
3. `sort_rows` 依勝率排序時用**遮罩後**的值（非 complete 視為 None → 組尾）。
4. 測試：v3 快照載入後 `query()` 的 pending 列四欄為 None、coins 為 []、tags 無 concentrated；compose 的 complete 列不受影響；勝率排序時 backfilling 列在組尾；既有 `test_load_snapshot_v3_migrates_*` 更新斷言。
5. 主線程驗收：本機 API（正式機 v3 快照）`GET /api/public/explore` 第一列 `close_win_rate_pct is None`、`coins == []`。

### Task 6.6 @inline：前端契約修正（P6 reviewer：1 Critical／2 Warning）

<!-- 2026-09-21 裁決：C1 `live_days` null→0 違反契約 A；W1 `closed_positions_30d`／`realized_pnl_30d_usd` 同型且型別說謊；
W5 `.explore-group-header`／`.explore-pending-reason` 無 CSS、組標題落在格線外窄螢幕錯位。 -->

**Files:** `web/src/lib/publicApi.ts`、`web/src/app/explore/page.tsx`、`web/src/styles/globals.css`（或該頁既有樣式檔）；對應 vitest。
1. `normalizeExploreRow`：`live_days`、`closed_positions_30d`、`realized_pnl_30d_usd`、`close_win_rate_pct`、`concentration_pct` 一律 `number | null`（非 number → null，**不得**補 0）；型別同步改 `number | null`。
2. 探索頁：`live_days` null 顯示 NO_VALUE（「—」）；其餘欄位既有 null 處理沿用。
3. CSS：`.explore-group-header`／`.explore-pending-reason` 補樣式（沿該頁既有 token）；組標題列放進 `.explore-table` 的格線內（同 `min-width`），窄螢幕橫捲時與列對齊。
4. vitest：pending/portfolio_missing 列 `live_days` 顯示「—」而非 0；normalize 對 null 保持 null；組標題在表格容器內。

### Task 6.7 @inline：後端契約修正（P6 reviewer W2／W3；6.5 之後）

<!-- W2 快照檔每列硬寫 eligibility=eligible（query 才重算）→ 讀檔的人被誤導；W3 6.4(b)(c) 用 force 無鑑別力。 -->

**Files:** `src/spark/publicapi/hl_explore.py`、`explore_publisher.py`；`tests/test_explore_publisher.py`、`tests/test_public_explore.py`。
1. `compose_rows` 末尾對每列以 `classify(row, cfg, **預設門檻)` 設 `eligibility`／`eligibility_reason`（快照反映預設門檻下的分類；`query()` 仍依請求門檻重算）；`load_snapshot` v3→v4 遷移同樣以預設門檻分類後再寫入 rows。
2. 6.4(b)(c) 改為非 `force`：推進 fake clock ≥ `min_interval_s` 後 `maybe_publish()`。
3. `enrich_candidate` docstring 與 P6 行為對齊（省略 coverage＝complete）。
4. 測試：dump 後快照第一列 `eligibility` 與 `query()` 預設門檻結果一致；v3 遷移列亦然。

### Task 6.8 @inline：探索頁總數與標題用「合格＋待確認」（主線程整合截圖發現）

<!-- 2026-09-21 本機整合截圖：待確認 25 列在榜上，分頁文字「顯示 1–25 / 0」、標題「（目前 0 檔）」都拿 total_qualified 當總數。 -->

**Files:** `web/src/app/explore/page.tsx`（`totalPages`、範圍文字、pool note）、`web/src/lib/copy.ts`；vitest。
1. 榜上總列數 `shownTotal = total_qualified + (僅合格開啟 ? 0 : (total_pending ?? 0))`；`totalPages` 與「顯示 a–b / N」都用 `shownTotal`。
2. pool note 改為「自 {pool} 個候選帳戶中：合格 {total_qualified}、資格待確認 {total_pending}；未列入者為鏈上資料不合格或缺失」（ZH／EN 對稱，新增或改既有文案鍵；`total_pending` 缺（舊後端）時只顯示合格數、行為同今天）。
3. vitest：pending 25 列＋qualified 0 → 範圍文字 `1–25 / 25`、頁數 ≥1、pool note 含「待確認 25」；僅合格開啟時總數只算 qualified。

### Task 6.4（主線程＋builder 測試）：三個重現驗收與恢復程序

- 測試（放 `tests/test_explore_publisher.py`，用真 `ExploreStore`＋`ExploreIndex`）：
  (a) 300 候選、全部有 portfolio（live_days 足、dd 合格）、只有 11 個 fills complete（其中 8 個 ≥200 筆、3 個 <200 筆）→ 發布後 rows＝297（eligible 8＋pending 289；3 個 complete 但不足 200 筆＝ineligible，依契約不入 rows）；`total_qualified==8`、`total_pending==289`、`total_ineligible==3`。<!-- 2026-09-21 依 6.1 實作修正數字 -->
  (b) 已有版本 300 列 → 候選換掉 80 個（新地址無任何資料）→ 發布成功，新 80 個為 pending（reason 含 live_days／portfolio 缺），舊 80 個不在 rows。
  (c) 已有版本 eligible 20 → 新資料完整且證明只剩 12 個合格 → 發布成功、`total_qualified==12`（榜縮小，不被擋）。
- 恢復程序（主線程）：6.1–6.4 全綠＋opus 複審通過 → 第三次部署（flag 0 → 驗證 → flag 1）→ 觀察連續兩次發布（`published_at` 前進、pending 數下降、eligible/ineligible 上升、rows 的 `as_of.fills` 等於該地址 sync 時間而非 published_at）→ 之後才起算 24 小時觀測期；預算不動。

## P7 觀測期間平行工作（2026-09-21 使用者裁決：不打斷觀測、不調預算；實作後**觀測期滿再部署**）

觀測：<!-- 2026-09-21 校正 -->7.4 部署後自 2026-09-21 01:12 UTC 重新起算，期末 2026-09-22 01:12 UTC（台北 9/22 09:12）。取樣：正式機 `/home/ubuntu/explore-obs/sample.py`（ubuntu crontab 每 15 分鐘，唯讀，
輸出 `samples.jsonl`；cohort 基線 `cohort_t0.json`＝flag 開啟後 300 候選的名單與狀態）。決策依「資料新鮮度、回補進展、queue 積壓、
引擎健康」，不追求 300 人全 complete。回報五項：回補 vs 無法補齊（completeness／reason 分佈）、同批地址進度（原候選完成數、新增候選、
游標推進數、最老到期趨勢）、fills 時間語義（last_success vs synced_through）、null→0 稽核、follower 同 IP 缺口（引擎 429／重試／延遲）。

### Task 7.1 @inline：契約補 `fills_coverage.synced_through`／`last_success_at`

<!-- 使用者第 3 點：as_of.fills 目前＝sync.updated_at＝最近一次抓頁成功時間，不代表「確認同步到哪裡」。 -->
- `fills_coverage` 加 `synced_through`（epoch ms｜null，＝`fills_sync.synced_through_ms`）與 `last_success_at`（epoch 秒｜null，＝`updated_at`）；
  `as_of.fills` 維持＝`last_success_at`（相容）並在契約表註明語義。詳情頁 `TraderData` 同步。前端型別 optional；詳情頁「觀測期間」文案改用
  `observed_from`～`synced_through`（缺則沿用現行）。測試：compose 列兩欄等於 store 值；v3 遷移列兩欄 null。

### Task 7.2 @inline：詳情頁／策略頁 null→0 稽核與修正

<!-- 使用者第 4 點：影響勝率、績效或策略資格者列對外驗收前必修；純展示也不得把未知顯示成 0。 -->
- 盤點 `web/src/lib/publicApi.ts` 的 `getPublicStrategies`（`live_days`）、`getPublicTraderDetail`（`FillsStats` 的 `closed_positions`／`realized_pnl_usd`／
  `live_days`）與其他 `: 0` 補值；對每一處判定「參與資格／排序／指標計算」或「純展示」，前者改 null 並修消費端，後者改 null 且顯示 NO_VALUE。
- 產出對照表寫進本卡片；vitest 覆蓋 null 保持 null 與畫面「—」。

**2026-09-21 builder 回報（實作完成）：**

命名澄清：程式碼中實際持有 `live_days: 0` 補值的策略頁函式是 `getPublicStrategy`
（單數，`/api/public/strategies/{slug}` 詳情端點；`PublicStrategyDetail extends PublicStrategy`
共用 `live_days` 欄位定義）——`getPublicStrategies`（複數，列表端點）本身不逐項 normalize，
直接透傳 `body.strategies`。兩者共用同一個 `PublicStrategy.live_days` 型別，故本次一併把
該型別改為 `number | null`，其唯一實際消費端（首頁 `page.tsx` 主推策略卡）一併修正。

對照表（`rg -n ": 0[,)]|\? [a-z_.]* : 0" web/src/lib/publicApi.ts` 全量掃描結果）：

| 檔案:行號（修正前） | 欄位 | 用途 | 分類 | 處置 |
|---|---|---|---|---|
| `publicApi.ts:276`（`getPublicStrategy`） | `PublicStrategy.live_days` | 首頁主推策略卡 3 處純展示（`page.tsx:163,180,195` 原 `featured.live_days`） | (b) 純展示 | **改 null**；型別 `number\|null`；消費端改 `featuredLiveDaysText`（null→NO_VALUE） |
| `publicApi.ts:847`（`getPublicTraderDetail`） | `PublicTraderDetail.live_days` | 交易員詳情頁純展示（`traders/[address]/page.tsx:387`） | (b) 純展示 | **改 null**；型別 `number\|null`；消費端 null→NO_VALUE |
| `publicApi.ts:711`（`normalizeTraderFillsStats`） | `TraderFillsStats.closed_positions` | 交易員詳情頁純展示（`page.tsx:463`） | (b) 純展示 | **改 null**；型別 `number\|null`；消費端 null→NO_VALUE |
| `publicApi.ts:714`（`normalizeTraderFillsStats`） | `TraderFillsStats.realized_pnl_usd` | 交易員詳情頁純展示＋CSS pos/neg 判斷（`page.tsx:473-474`） | (b) 純展示（但發現隱藏計算陷阱：`null >= 0` 在 JS 為 `true`，會誤套 `pos` 樣式） | **改 null**；消費端 null→NO_VALUE，且 CSS class 判斷加 null 短路，不落入 `>= 0` 比較 |
| `publicApi.ts:719`（同函式） | `TraderFillsStats.order_count` | 同一物件的手足欄位，理論上與 `closed_positions` 同款 coverage 依賴 | 未命名於本 task 範圍 | **維持現狀（`: 0`）**——本 task 明確只列 `closed_positions`／`realized_pnl_usd`；與 `wins` 一併記錄為後續稽核候選，避免超出派工範圍 |
| `publicApi.ts:720`（同函式） | `TraderFillsStats.wins` | 同上 | 未命名於本 task 範圍 | **維持現狀（`: 0`）**，同上理由，記錄待後續 task |
| `publicApi.ts:284,853`（`getPublicStrategy`／`getPublicTraderDetail`） | `sample_days` | CAGR 摺疊門檻比較（`sample_days < sample_threshold`）——真正的 (a) 類（資格計算），但 `publicApi.test.ts:187` 有既有 pinned 測試斷言缺鍵時 `sample_days` 為 `0` | 未命名於本 task 範圍 | **維持現狀**——本 task 未列名，且改動會破壞既有 pinned 測試（不得動既有測試）；記錄為需要主線程裁決的候選（0 在此語意上與「近端後端 `sample_days_from_perf` 失敗即 0」一致，是否需要區分「未知」與「confirmed 0」待裁決） |
| `publicApi.ts:161,178,193,229,249,356,666-668,752,764,872,909,945,968` | `updated_at`／`sample_count`（metrics/methodology）／`total_qualified`／`total_scanned`／`pool` | 時間戳或計數，均非本 task 命名欄位、非 `live_days`/`closed_positions`/`realized_pnl_usd` | 未命名於本 task 範圍 | **維持現狀**——不在委派 prompt 明列範圍內（`getPublicStrategies`／`getPublicTraderDetail` 的 `live_days`／`FillsStats`），避免與 Task 7.1 同時編輯 `publicApi.ts` 造成衝突面擴大 |

實作檔案：`web/src/lib/publicApi.ts`（型別＋normalize）、`web/src/app/page.tsx`（首頁主推卡消費端）、
`web/src/app/traders/[address]/page.tsx`（交易員詳情頁消費端）；測試：`web/src/lib/publicApi.test.ts`、
`web/src/app/page.test.tsx`、`web/src/app/traders/[address]/page.test.tsx`。

### Task 7.3（主線程）：follower 同 IP 缺口
- 觀測期間每筆取樣含引擎 429／同步錯誤／重試／心跳年齡；期滿一併呈報。
- 共享預算修復候選（不在本輪實作）：(a) 引擎改經 publicapi 同一個 `WeightLimiter`——需跨進程，走檔案鎖或 unix socket；(b) 引擎自帶同規格
  限流器並各自留餘量（現行做法，靠 900 上限的 300 餘量）；(c) 引擎讀 publicapi 的 `hl_budget` 快照做退讓。待使用者裁決。

### 觀測發現 O-1（2026-09-20 19:13 UTC，主線程；<!-- 2026-09-21 校正日期 -->）：基礎資料重抓佔滿 explore 預算，fills 類別級飢餓

實測 15 分鐘：state 298 次（596 權重）、ledger 100（2,000）、portfolio 85（1,700）＝約 286／分鐘，貼著 300 上限；
fills 60 分鐘 0 頁、最老 fills job 已到期 16,216 秒；ledger／portfolio 到期積壓 52／63 持續不歸零。原因：spec §6 估 240／分鐘用的是
名目週期，實際 jitter −10%＋15 分鐘 state 讓穩態約 280／分鐘，剩餘 <20／分鐘塞不進一頁 fills 的 120 預留；優先級 0/1 永遠先於 2/3。
**結論：現行參數下 fills 在 24 小時內不會推進**，觀測期會一直看到 backfilling 293。

候選修法（Task 7.4，待使用者裁決，觀測期內不動）：
(a) 週期放寬：state 900→1800s、portfolio／ledger 3600→7200s → 基礎約 20＋50＋50＝120／分鐘，留 ~180 給 fills（~1.5 頁／分鐘）。
(b) 公平配額：scheduler 每 N 個 job 至少領一個 fills（或 fills 到期超過 T 分鐘即臨時提升優先級），避免類別級飢餓。
(c) 提高 explore 預算——使用者已裁決不做。
建議 (a)＋(b) 併行；(a) 是參數、(b) 是行為，都需部署。

### Task 7.4（2026-09-21 使用者裁決：立即做，部署後觀測期重新起算；預算維持 300）

**原因修正**：已證實的機制是「嚴格優先級＋沒有為大請求（fills 一頁 120）保留額度」；286／分鐘可能含清理積壓、jitter 不必然拉高長期均值，不當成穩態結論。

**設計**：
- 限流器父子 scope：`explore`（父，cap 300）、`explore_base`（子，cap 180）、`explore_fills`（子，cap 120）。子 scope 的預留同時計入父與全域；父 scope 暫停（429）時子 scope 一併暫停。有 fills 待處理時基礎類別走 `explore_base`（≤180）、fills 走 `explore_fills`（保證 120＝每分鐘至少一頁）；無 fills 待處理時基礎類別走父 scope `explore`（可用到 300）。
- 週期：state 900→1800s、portfolio／ledger 3600→7200s（各欄位 `as_of`／`fetched_at` 保持真實時間）。
- 重排既有 overdue：scheduler 啟動後第一個 tick 對基礎類別 job 中 `next_attempt_at < now − period` 者重設為 `now + spread(addr, period)`（把積壓攤平到一個週期內，避免改週期後仍先被舊積壓占滿）；job key 為 (address, kind) 主鍵，結構上無重複。
- 類別感知的領工：tick 先看 `store.due_count("fills") > 0` 且 `limiter.available("explore_fills") >= 120` → 只從 fills 領；否則從基礎類別領（fills 不領）。fills 類別內：每地址一頁後 `reschedule(next=now)` 回到 FIFO 尾端；領工排序加等待時間加權（`priority − min(3, floor(wait_s/600))`，等 10 分鐘升一級），避免 hot 地址持續占用保留額度。無法補齊者已由 fills_sync 標 partial/reason，不重試同頁。

#### Task 7.4a @inline：限流器父子 scope＋`available()`
**Files:** `src/spark/publicapi/hl_budget.py`、`tests/test_hl_budget.py`。
- `WeightLimiter(..., scope_parents: dict[str, str] | None = None)`；`_used(scope)` 含子 scope 的項目（項目記錄自己的 scope，查父時把 `scope_parents[s] == parent` 的也算）；`try_reserve` 依序檢查：暫停（自身或父）、全域、父 cap（若有）、自身 cap。`available(scope) -> int` ＝ min(自身餘量, 父餘量, 全域餘量)。`note_429(scope)` 對子 scope 的 429 一律暫停父 `explore`（子自動跟著）。`snapshot()` 的 `used` 含子 scope 各自數字與父的合計。
- 測試：子預留計入父與全域；父滿時子被拒；子滿時另一子仍可（父未滿）；父暫停子拋 `ScopePaused`；`available` 三者取最小；並發不超父 cap。

#### Task 7.4b @inline：scheduler 週期、重排、類別感知領工、等待加權
**Files:** `src/spark/publicapi/explore_scheduler.py`、`src/spark/publicapi/explore_store.py`；`tests/test_explore_scheduler.py`、`tests/test_explore_store.py`。
- store：`claim_due(now, owner, lease_s, *, kinds: tuple[str, ...] | None = None)`（`kinds` 限定領哪些 kind）；排序 `ORDER BY (priority - MIN(3, CAST((? - next_attempt_at)/600 AS INTEGER))) , next_attempt_at`；`due_count(kind, now) -> int`；`rebalance_overdue(kind, now, period_s, spread_fn) -> int`（只動 `next_attempt_at < now − period_s` 者）。
- scheduler：建構子 `hl_base`／`hl_fills` 兩個 scoped gateway（`hl.scoped("explore_base")`／`hl.scoped("explore_fills")`）＋既有 `hl`（父 scope）；`state_every_s=1800`、`portfolio_every_s=7200`、`ledger_every_s=7200`；第一個 tick 先 `rebalance_overdue` 三個基礎 kind；tick 領工邏輯如「設計」；基礎 job 執行時依「fills 是否待處理」選 `hl_base` 或 `hl`；fills 用 `hl_fills`。`status()` 加 `fills_pages_total`、`last_fills_at`、`base_scope_in_use`。
- 測試（含使用者第 4 點的飢餓重現）：300 地址、state 週期設極短讓基礎類別永遠有積壓、fake HL 正常、全域 900／explore 300／base 180／fills 120，fake clock 驅動 30 分鐘 → fills 頁數 ≥ 25（≈每分鐘一頁）、基礎類別仍持續推進（state 抓取數 > 0 且遞增）、任一 60 秒切片：explore 合計 ≤300、base ≤180（fills 待處理期間）；無 fills 待處理時基礎可用到 300；等待加權：priority 3 的 fills 等 20 分鐘後排在剛到期的 priority 2 前面；重排：啟動時 100 個 overdue state job 被攤到未來 1800s 內、不在同一秒。

#### Task 7.4c @inline：接線、設定、health、RUNBOOK
**Files:** `src/spark/publicapi/config.py`、`scripts/run_api.py`、`src/spark/publicapi/app.py`、`deploy/RUNBOOK.md` §5.8e、`deploy/filet-api.service.d/explore-refresh.conf.example`；`tests/test_publicapi_config.py`、`tests/test_api_ops.py`。
- config：`hl_explore_base_weight_cap`（env `FILET_HL_EXPLORE_BASE_WEIGHT_CAP`，預設 180）、`hl_explore_fills_weight_cap`（`FILET_HL_EXPLORE_FILLS_WEIGHT_CAP`，預設 120）；驗證 base＋fills ≤ explore ≤ global。run_api：`WeightLimiter(global_cap, scope_caps={"explore":300,"explore_base":180,"explore_fills":120}, scope_parents={"explore_base":"explore","explore_fills":"explore"})`；scheduler 傳三個 gateway。health：`hl_budget.used` 已含子 scope；`explore_refresh` 加 `fills_pages_total`／`last_fills_at`／`base_scope_in_use`。RUNBOOK §5.8e：新 env、新週期、保留額度說明、部署後 15–30 分鐘檢查項（fills 頁數、不同地址進展、最老等待）。
- 部署程序（主線程）：flag 0 部署→驗證→flag 1→15–30 分鐘看 `fills_pages_15m > 0`、多個不同地址推進、最老到期回落→重新起算 24h 觀測。

### Task 7.5 @inline：complete 判定的證據與標示（正確性缺陷，2026-09-21 使用者裁決立即修正標示）

<!-- 現況：complete＝遍歷到區間末＋fills_in_window < 8,000（內部保守門檻，未命名）；沒有留存邊界證據；官方留存上限是 10,000。
使用者：相同 API 重抓零差異只證明同步一致，不證明完整 30 天；需確認查詢窗口、aggregation 設定、留存邊界證據。 -->

**Files:** `src/spark/publicapi/explore_fills_sync.py`、`explore_scheduler.py`、`explore_store.py`、`hl_explore.py`（契約鍵）、`explore_publisher.py`／`app.py`（coverage 組裝）；對應測試；plan 契約表。

1. **命名**：`HL_FILLS_RETENTION_LIMIT = 10_000`（官方）、`RETENTION_SAFETY_MARGIN = USER_FILLS_PAGE_LIMIT`、`RETENTION_SAFETY_THRESHOLD = 8_000`（＝上限 − 一頁；docstring 說明為何保守）。舊名 `RETENTION_LIMIT` 移除。
2. **reason 碼**（`fills_sync.reason`，complete 也要有原因）：`count_below_retention_threshold`（現行判準）、`retention_boundary_verified`（探測到區間起點之前仍有可查成交 ⇒ 留存邊界早於窗口起點）；partial 的 `retention_limit`／`same_ms_overflow`／`no_progress`／`page_cap` 不變。`apply_page` 短頁收尾時先給 `count_below_retention_threshold`。
3. **留存邊界探測**（scheduler `_run_fills`，第一輪 `res.done` 且 completeness==complete 時做一次）：`hl_fills.get_fills_page(addr, window_start − 86_400_000, window_start − 1)`；回 ≥1 筆 → `store.set_sync_reason(addr, "retention_boundary_verified")`；回空 → 維持 `count_below_retention_threshold`；探測拋例外（額度／429／網路）→ 不改 reason、log 一行、不重試（下一輪增量時若 reason 仍是 count_below… 再探一次）。探測走 `explore_fills` scope、計入預算。
4. **查詢參數留證**：`fills_sync` 加欄位 `params_fp TEXT NOT NULL DEFAULT ''`（schema v2 migration）；新輪寫 `"aggregateByTime=default(false)"`（與 `hl.get_fills_page` 實際請求體一致，不多不少）；`fills_coverage` 契約加 `window_start`／`window_end`（epoch ms）與 `params_fp`；plan 契約表 A 同步。
5. **既有列補標**：store 啟動 migration：`UPDATE fills_sync SET reason='count_below_retention_threshold' WHERE completeness='complete' AND reason IS NULL`。
6. **測試**：`apply_page` 完成時 reason 為 `count_below_retention_threshold`；scheduler 探測正／負／例外三路徑（fake HL 可控）；migration 補標與 params_fp 預設；publisher／詳情頁 coverage 含 `window_start`／`window_end`／`params_fp`／`reason`；`rg -n "RETENTION_LIMIT\b" src` 零命中。
7. **前端**：`FillsCoverage` 型別加三個 optional 欄位；不改 UI（reason 文案下一輪）。

### Task 7.6 @inline：7.5 複審修正＋增量輪語義缺陷（2026-09-21 主線程裁決；與 7.5 同批部署）

<!-- 來源：7.5 fresh review（opus）W1–W4／S1–S4，主線程逐條在 code 核實；另主線程實跑重現兩個複審沒抓到的缺陷：
(a) 增量輪第一頁滿頁 → `completeness` 仍是 complete → 下一 tick `plan_page` 判 noop（`is_noop True`）→ 4 小時後再從舊 `synced_through` 重抓同一頁，游標永遠不前進（重度帳戶 4h+ 內 >2,000 筆才觸發；正式機 2026-09-21 05:30 UTC 查無卡住列，但屬正確性缺陷）；
(b) `partial`（`retention_limit`）地址在增量輪跑一頁短頁就被改成 `complete`／`count_below_retention_threshold`（與 `plan_page` docstring「partial 仍是 partial」相反；正式機現有 2 列 partial 會在下一輪增量被誤標）。 -->

**Files:** `src/spark/publicapi/explore_fills_sync.py`、`explore_scheduler.py`、`explore_store.py`、`hl_explore.py`（`_row_from_dict`）；`tests/test_explore_fills_sync.py`、`tests/test_explore_scheduler.py`、`tests/test_explore_store.py`、`tests/test_hl_explore.py`（或既有 v4 快照測試檔）。不動前端、不動契約鍵集合、不動 schema（仍是 v2）。

**A. 增量輪語義（修 (a)(b) 與複審 W3）**
1. `plan_page` 的 `complete|partial` 分支，在增量寬限期判斷**之前**加「輪進行中」判斷：`state.synced_through_ms is not None and state.cursor_ms > state.synced_through_ms` → 回 `PagePlan(start_ms=state.cursor_ms, end_ms=state.window_end_ms, state=state)`（續抓本輪，不看寬限期、不改 state）。理由：輪開始時 `cursor = synced_through − overlap ≤ synced_through`；滿頁後 `cursor = 最後一筆時間 ≥ synced_through`（`==` 只在該毫秒溢出時發生，由 `same_ms_overflow` 終止為 partial）；短頁收尾不改 cursor；`synced_through` 只在輪結束才推進。所以 `cursor > synced_through` ⇒ 本輪未結束；**反向不成立**（`==` 的兩種狀態都是輪已結束，改成 `>=` 會讓下一 tick 無限重抓 `[end,end]`，見 7.7 點 3）。<!-- 2026-09-21 7.7 W1 校正：原寫 ⇔ -->。
2. `apply_page` 短頁分支改為兩種輪：
   - `state.completeness == "backfilling"`（首輪全區間遍歷）→ 現行：`fills_in_window >= threshold` → `partial`／`retention_limit`，否則 `complete`／`REASON_COUNT_BELOW_RETENTION_THRESHOLD`。
   - 否則（增量輪；前態 `complete` 或 `partial`）→ `fills_in_window >= threshold` → `partial`／`retention_limit`（本輪缺口本身超標，合法降級）；否則 **`completeness` 與 `reason` 原樣保留**（增量輪只延伸 `synced_through`，不重新宣稱整個窗口；`partial` 仍 `partial`；`retention_boundary_verified` 跨輪存活）。其餘欄位（`synced_through=end_ms`、observed、`fills_in_window`、`last_error=None`、`updated_at`）照舊。
   - `same_ms_overflow`／`no_progress`／`page_cap` 三條降級路徑不變。
3. 語義文件化（模組檔頭與 `REASON_*` 註解、plan 契約表 A）：`reason` 描述的是**建立該 completeness 的那次全區間遍歷**所依據的證據；之後的增量輪只延伸 `synced_through`，本地已持有的區段不受 HL 留存影響，故不重新量測、不重寫 reason。`window_start`／`window_end` 是目前輪的查詢區間。

**B. 留存邊界探測（複審 W1、W2、S2、S3）**
4. `_run_fills` 探測條件改為：`res.state.completeness == "complete" and res.state.reason == REASON_COUNT_BELOW_RETENTION_THRESHOLD`（已 verified 者不再探——A2 讓 verified 跨輪存活，所以每地址最多探到成功為止）；順序改為 `_complete(job)` → `is_active` 檢查（退池 → `"dropped"`，不探）→ 探測 → enqueue。
5. 探測回應必須通過 `validate_page(page, probe_start, probe_end)`（把 `explore_fills_sync._validate_page` 改為公開名 `validate_page`，所有引用一起改，不留 alias）：`None` 且 `len(page) >= 1` → `set_sync_reason(addr, REASON_RETENTION_BOUNDARY_VERIFIED)`；非法（含 `time_out_of_range`）→ warning 一行、不升級。
6. 計數器：scheduler 加 `_probe_total`／`_probe_verified`／`_probe_empty`／`_probe_failed`（例外＋非法回應都算 failed），`stats()` 輸出 `"probe": {"total","verified","empty","failed"}`；確認 `/api/ops/health` 的 `explore_refresh` 直接帶出 `stats()`（若是挑鍵輸出則補這一鍵）。
7. `ExploreStore.set_sync_reason` 只 UPDATE `reason`，**不動 `updated_at`**（該欄對外＝`last_success_at`＝最近一次抓頁成功時間；探測不是抓頁）。docstring 同步。

**C. 其他複審項**
8. `PARAMS_FP = "aggregateByTime=<omitted>"`（只描述事實：請求體沒送這個參數；不再宣稱上游預設值）；註解同步。
9. `_migrate_v1_to_v2` docstring 改寫：`ALTER TABLE` 是 DDL，Python sqlite3 會自動提交、**不在** `with self._db` transaction 內；本 migration 兩步都冪等（欄位存在則跳過、UPDATE 條件式），中途失敗的中間態下次啟動自癒；未來多 DDL＋搬資料的 migration 需顯式 `BEGIN`／分步驗證。
10. `hl_explore._row_from_dict`：`fills_coverage=({**DEFAULT_FILLS_COVERAGE, **(d.get("fills_coverage") or {})})`，先確認 `DEFAULT_FILLS_COVERAGE` 含 7.1／7.5 的五個新鍵（`synced_through`、`last_success_at`、`window_start`、`window_end`、`params_fp`）皆為 None，缺則補。

**測試（新增或改寫，全部離線）**
- `test_explore_fills_sync.py`：(i) complete 態＋`cursor > synced_through` → `plan_page` 非 noop、`start==cursor`、`end==window_end`；`synced_through None` 不觸發。(ii) 端到端：complete → 5h 後增量輪、第一頁 2,000 筆滿頁（`done False`）→ 1 分鐘後 `plan_page` **非 noop** → 短頁 → `complete`、reason 仍原值、`synced_through == window_end`。(iii) `partial`／`retention_limit` 增量短頁 → 仍 `partial`／`retention_limit`。(iv) `retention_boundary_verified` 經增量短頁保留。(v) 增量輪 `fills_in_window >= 8_000` → `partial`／`retention_limit`。(vi) 首輪行為不變（既有測試）。
- `test_explore_scheduler.py`：reason 已 verified → fake HL 不收到探測呼叫；探測回傳 time 在探測窗外 → 不升級、`stats()["probe"]["failed"]==1`；退池地址不探；正常命中 → `verified==1` 且 `fills_sync.updated_at` 不變。
- `test_explore_store.py`：`set_sync_reason` 後 `updated_at` 等於原值。
- 快照測試：v4 快照列缺五個新鍵 → `_row_from_dict` 後五鍵為 None。

**驗收（主線程親跑）**：`uv run ruff check src tests scripts`；`uv run pytest -q`（全綠，數量 > 3159）；`rg -n "_validate_page" src tests` 零命中；主線程重跑重現腳本（scratchpad `repro_76.py`）：增量滿頁後 `is_noop False`、partial 增量後仍 partial。

### Task 7.7 @inline：7.6 複審修正——partial 復原路徑、探測只做一次、docstring 不變量（2026-09-21 主線程裁決）

<!-- 來源：7.6 fresh review（opus）W1–W3／S1–S4。7.6 已於 2026-09-21 05:54 UTC 隨 7.5 部署（使用者授權），本 task 是部署後的修正批，
完成後再部署一次（無 schema 變更）。主線程裁決：
W1（`cursor == synced_through` 邊界）——不改述語為 `>=`：短頁收尾不改 cursor，`cursor == synced_through == end_ms` 是合法的「輪已結束」狀態，
改 `>=` 會讓下一 tick 無限重抓 `[end,end]`。複審重現的卡死情境（同一毫秒 ≥2,000 筆）是 `same_ms_overflow` 既有的吸收態，7.6 前後皆然，
不在本批修；只改 docstring／plan 把不變量寫準確。
W2（partial 成吸收態）——7.6 前 partial 會在增量短頁被（錯誤地）升回 complete；7.6 移除後缺復原路徑。裁決：partial 地址不做增量輪，
每 24 小時整窗重掃一次（正確的復原：重新全區間遍歷，門檻重新判）。成本：每個 partial 地址每日約 4–5 頁（≤600 權重），正式機現有 3 列。
W3（探測回空每輪重探）——加第三個 reason 碼記錄「已探過、無更早成交」，探測條件排除它 ⇒ 每次全區間遍歷後至多探一次。 -->

**Files:** `src/spark/publicapi/explore_fills_sync.py`、`explore_scheduler.py`、`explore_store.py`；對應測試；plan 契約表 A 的 reason 說明。不動 schema、不動前端（前端不解讀 reason 字串）。

1. **partial 復原（W2）**：`explore_fills_sync` 加常數 `PARTIAL_RESCAN_AFTER_MS = 24 * 3600 * 1000`；`plan_page` 的 `partial` 分支改為：`now_ms - state.window_end_ms >= PARTIAL_RESCAN_AFTER_MS` → 回傳與 `state is None` 相同形狀的全新首輪（新窗 `[now-30d, now]`、`cursor=window_start`、`completeness="backfilling"`、`reason=None`、`pages_done=0`、`fills_in_window=0`、`synced_through_ms=None`、observed 兩欄清 None、`params_fp=PARAMS_FP`）；未滿 24 小時 → noop（`start==end==synced_through`）。「輪進行中」判斷（A1）仍在最前面，partial 多頁重掃中途不受影響。`complete` 分支不變（仍 4 小時增量）。docstring 寫明：partial 不做增量，因為增量只延伸尾端、無法證明先前無法證明的部分；復原只能靠整窗重掃。
2. **探測只做一次（W3）**：`explore_store` 加 `REASON_PROBE_NO_EARLIER_FILLS = "count_below_retention_threshold_probe_empty"`（語義：門檻推論成立，且探測起點前一天無可查成交、無法升級）；scheduler 探測回空頁 → `set_sync_reason(addr, REASON_PROBE_NO_EARLIER_FILLS)`；探測條件維持 `completeness=="complete" and reason==REASON_COUNT_BELOW_RETENTION_THRESHOLD`（新碼自然排除）；探測失敗（例外／非法回應）仍不改 reason（下輪再試）。`_run_fills` 註解改成「每次全區間遍歷後至多探到有結論（verified 或 probe_empty）為止」。
3. **docstring 不變量（W1）**：`plan_page` docstring 與 plan 7.6 A1 的敘述改為：「輪開始時 `cursor ≤ synced_through`（overlap）；滿頁後 `cursor = 最後一筆時間 ≥ synced_through`，其中 `==` 只在該毫秒溢出時發生並由 `same_ms_overflow` 終止為 partial；短頁收尾不改 cursor。故 `cursor > synced_through` ⇒ 輪進行中；反向不成立（`==` 的兩種狀態都是輪已結束）。」
4. **S2**：`_probe_retention_boundary` 任一次 `set_sync_reason` 後呼叫 `self._on_dirty()`。
5. **S4**：`set_sync_reason` 移入 try（store 寫入失敗 → warning、`_probe_failed += 1`、不逸出）。
6. **S1**：探測回空測試斷言 `status()["probe"] == {"total":1,"verified":0,"empty":1,"failed":0}` 且 reason 變為 `count_below_retention_threshold_probe_empty`；再跑一輪增量 → fake HL 不再收到探測呼叫、`probe.total` 仍 1。
7. **測試**：partial 未滿 24h → noop；partial 滿 24h → 首輪形狀（backfilling、reason None、cursor=window_start、synced_through None）；重掃後筆數低於門檻 → complete／count_below（復原成功）；重掃仍超標 → partial／retention_limit；partial 重掃多頁中途 A1 續抓；`complete` 分支行為不變。
8. **探測在保留額度下永遠失敗（正式機 2026-09-21 05:54–05:57 實證，主線程裁決）**：`explore_fills` 子預算 cap 120，本輪 fills 頁剛預留／結算過（短頁結算後仍佔 20+），同一分鐘內再預留 120 必然 `BudgetExhausted`——正式機每個 complete 收尾都印一行「留存邊界探測失敗 … BudgetExhausted」，探測從未成功過。修法：
   - `_probe_retention_boundary` 改成只在**額度足夠時**發送：先 `self._fills_available() >= FILLS_PAGE_WEIGHT`（沿用 7.4b 同源判斷）才打；不足 → 把 `(address, window_start_ms)` 放進 `self._probe_deferred: collections.deque`（maxlen 64，同地址去重），計數 `probe.deferred += 1`，**不 log**。
   - `_tick_once` 開頭（領工之前）：若 `_probe_deferred` 非空且 `_fills_available() >= FILLS_PAGE_WEIGHT` → pop 一個做探測（用 deque 裡記的 `window_start_ms` 算探測窗；探測前重讀 `get_sync`，若該列已不是 `complete`／`count_below_retention_threshold` 或地址已退池則丟棄）；每 tick 至多一個，然後照常領工。這讓探測與 fills 頁公平輪流分同一保留額度，不會餓死任何一方。
   - 例外分類：`BudgetExhausted`／`ScopePaused`（`hl_budget.is_rate_limited` 或型別判斷）→ 視同「額度不足」進 deferred、不 log；其他例外 → warning＋`probe.failed`。
   - `status()["probe"]` 加 `deferred`（目前佇列長度）與 `deferred_total`。
   - 測試：fake limiter 讓 `_fills_available()` 先回 0 → 探測進 deferred、無上游呼叫、無 warning；下一 tick 回 120 → 探測送出、佇列清空；同地址兩次 complete 只留一筆；退池／狀態改變者被丟棄。

**驗收（主線程親跑）**：ruff 乾淨；`uv run pytest -q` 全綠且 > 3171；`rg -n "probe_empty" src tests` 有命中；scratchpad `repro_77.py`（主線程寫）：partial 狀態 25h 後 `plan_page` 回 backfilling 首輪、短頁後 complete；探測回空後第二輪不再探；部署後正式機 journal 不再每分鐘出現「探測失敗 … BudgetExhausted」，且 `explore_refresh.probe.verified` 或 `empty` 在 30 分鐘內 > 0。

### Task 7.8 @inline：7.7 複審 Critical——noop 重排時間與 noop 期限同源（2026-09-21 主線程裁決；7.7 不得先於本 task 部署）

<!-- 來源：7.7 fresh review（opus）Critical 1，主線程實跑 reviewer 腳本核實：200 tick 全是 noop 的 `ran:fills`，state job 一次都領不到。
根因：`_run_fills` noop 分支重排 `next_at = window_end + fills_every_s（4h）`，7.7 之前它恰好等於 noop 失效時刻；7.7 把 partial 的 noop 期限改成 24h
但重排常數沒跟著改 → `window_end+4h` 之後每次 noop 都排到過去 → 等待加權讓它永遠贏 → 零睡眠緊迴圈、餓死所有 job（工程原則 #1：比較的兩個量要同源）。
同批順修 7.7 複審 W1（已由主線程改 plan 7.6 A1）、W3、S1、S3、S4；W2（探測與 fills 頁輪流分同一保留額度、冷啟動時吞吐減半）為已接受的暫時代價：
每地址一生至多一次探測，300 地址上限 300 次，部署後盯 `queue_depth`／`oldest_due_age_s`／`probe.deferred`。 -->

**Files:** `src/spark/publicapi/explore_fills_sync.py`（`PagePlan`、`plan_page`）、`explore_scheduler.py`（`_run_fills` noop 分支、`_drain_one_deferred_probe`、`_enqueue_probe_deferred`、`status`）；`tests/test_explore_fills_sync.py`、`tests/test_explore_scheduler.py`、`tests/test_api_ops.py`。不動 schema、不動前端。

1. **同源（Critical）**：`PagePlan` 加欄位 `next_due_ms: int | None = None`（NamedTuple 預設值，既有建構呼叫不必改）。`plan_page` 產生 noop 計畫時一律填：`complete` → `state.window_end_ms + incremental_after_ms`；`partial` → `state.window_end_ms + PARTIAL_RESCAN_AFTER_MS`。非 noop 計畫維持 `None`。docstring：「noop 計畫自帶失效時刻，排程端只能用它重排，不得另算」。
2. **排程端**：`_run_fills` noop 分支改為 `next_at = max(plan.next_due_ms / 1000, now + self._jit(60.0))`——`next_due_ms` 為 None 時（防禦，不應發生）記 `logger.error` 並用 `now + self._fills_every_s`。任何路徑都不得把 `next_attempt_at` 排到 `now` 之前。
3. **W3 溢位可觀測**：`_enqueue_probe_deferred` 在 `len(deque) == maxlen` 且要 append 新項時 `_probe_dropped += 1` 並記一行 warning（含地址）；`status()["probe"]` 加 `dropped`。
4. **S1 過期視窗**：deferred 項記 `(address, window_start_ms)`；drain 時重讀 `get_sync`，若 `st.window_start_ms != 佇列值` → 丟棄（計入 `dropped`，不算 failed）。
5. **S3**：移除 `_drain_one_deferred_probe` 未使用的 `now` 參數（或真的用它）。
6. **S4 測試（回歸守門）**：(i) partial 地址 noop 後 `refresh_job.next_attempt_at > now`，且等於 `window_end + 24h`；(ii) complete 地址 noop 後等於 `window_end + 4h`（既有行為不變）；(iii) 一個 partial（在 4h–24h 區間）＋兩個 state job 的 store，連跑 50 tick：`ran:fills` 恰好出現 ≤1 次、兩個 state job 都被領過、其餘 tick 為 `idle` 或 `ran:state`（用複審腳本 `scratchpad/repro_partial_starves_base.py` 的形狀，但寫成正式測試）；(iv) `plan_page` noop 計畫 `next_due_ms` 兩種值；(v) deferred 溢位 `dropped` 遞增；(vi) 視窗前移的 deferred 項被丟棄且不探。

**驗收（主線程親跑）**：ruff 乾淨；`uv run pytest -q` 全綠且 > 3183；主線程重跑 `scratchpad/repro_partial_starves_base.py`：200 tick 中 `ran:fills` ≤ 2 且 state 有被領；`rg -n "next_due_ms" src` 有命中。

### 前次合併裁決逐項對帳（2026-09-21 使用者要求；「證據」欄只列主線程親跑過或正式機查得到的東西，零 429 不算證據）

| 裁決 | 狀態 | 證據 | 缺口 → 去向 |
|---|---|---|---|
| 7.4(a) 週期 30 分／2 小時、預算 300 不動、逾期重排、入列去重 | 完成、已上線 | `test_first_tick_rebalances_overdue_state_jobs_and_reports_status`、`test_rebalance_overdue_*`、`test_enqueue_dedupe_only_raises_priority_and_advances_time`；正式機四基礎類別逾期 p95 ≤ 600 秒（取樣器） | 無 |
| 7.4(b) 保證權重：base ≤180、fills 保留 120、皆在全域限流下 | 完成、已上線 | 7.4a limiter 父子 scope 測試；主線程實跑 base 3×60 後第 4 次拒、fills 仍 120、父滿時兩子歸零 | 正式機持續證據只在 `/api/ops/health.hl_budget`（需 admin），取樣器未記 → 7.9 取樣器補 health 快照 |
| 7.4(c) fills 類別公平：一地址一頁即重排、等待加權、不可修原因標記、無無限重試 | 前三項完成；第四項**未完成**（暫時性錯誤退避封頂 900 秒但無嘗試上限，`explore_scheduler.py:355-359`） | `test_fills_starvation_reproduction_bounded_and_progressing`、`test_claim_due_orders_by_priority_with_wait_time_escalation`、`test_budget_exhausted_keeps_original_due_time_so_wait_keeps_accumulating`、`same_ms_overflow`／`no_progress`／`page_cap` 各有測試 | 例外路徑 `_reschedule(bump_attempts=True)` 是否有嘗試上限→隔離，尚未確認 → 7.9 點 9 |
| 7.4(d) 部署前飢餓重現驗收 | 完成 | 上述 804 行測試；正式機 O-1 後 fills 每分鐘一頁（60／小時，期中量測） | 無 |
| 7.4(e) 原因描述改正 | 完成 | 研究報告與 RUNBOOK 已改為「嚴格優先級＋未保留大請求額度」 | 無 |
| P6 D12–D16（無門檻、三態、null 不轉 0、分組排序分頁、可觀測） | 完成、第三次部署 | 6.4 三案例、6.x vitest；正式機公開榜三態 | 無 |
| 7.5 命名／reason／探測／`params_fp`／補標 | 完成、第五次部署 | migration 實跑 version 2、complete 零 NULL reason；正式機 `retention_boundary_verified` 79 | 探測「每地址一生一次」語義錯（見下） |
| 7.6 增量輪保留 completeness／reason | 完成、第五次部署 | `repro_76.py` 修前 FAIL 修後 PASS | 無 |
| 7.7 partial 復原 | **語義偏離**：實作為「partial 停抓 24 小時再整窗重掃」 | `repro_77_main.py` | 使用者：partial 期間仍須持續增量保存新成交 → 7.9 點 3 |
| 7.7 探測只做一次 | **語義偏離**：以 reason 碼實作成「每地址一生一次」 | `test_retention_boundary_probe_*` | 使用者：探測有效性應限定於它所驗證的那次全區間遍歷（窗口）→ 7.9 點 4 |
| 7.7 探測延後佇列 | 完成但**不耐重啟／溢位**：in-memory deque、滿 64 丟最舊 | `test_*deferred*`（12 條）；正式機第六次部署後零探測失敗、177 次探測 | 重啟即清空、溢位靜默丟（只計數）→ 7.9 點 6（改由 DB 推導） |
| 7.7 S2/S4 探測回寫 | 部分 | `set_sync_reason` 已進 try | 回寫無版本保護（列可能已進新輪）→ 7.9 點 5；`_on_dirty` 在 try 外、job 可能遺失 → 7.9 點 7 |
| 7.8 noop 同源 | 完成、第六次部署 | 複審重現 200 tick 修前全 `ran:fills`、修後 2；`test_run_fills_partial_noop_does_not_starve_other_jobs_over_50_ticks` | 無 |
| 公平排程的正式機證據 | **缺** | 只有單元測試；正式機無「每地址兩輪間隔分佈」量測 | 7.9 驗收：部署後量 per-address 輪間隔 p50/p95 |

### Task 7.9 v2（2026-09-21 使用者第二輪裁決後改寫；拆 7.9a／7.9b 兩批派工，a 親驗後才派 b）

<!-- 使用者修正（原文要點）：(6) 探測與 fills 不可各半——雙方都有積壓時每 10 次至少 9 次給 fills、1 次給 probe，另一方無工作時可借用；需求要算入多頁增量與 partial 重掃。
(2,4,5) 每次全區間遍歷要有持久化、不可重用的 scan_id／generation，探測回寫以它作條件更新；歷史 evidence 可保留但不自動代表目前滾動 30 天完整，還要確認增量覆蓋連續無 gap。
(3,7,8) 增量與整窗重掃分別保存游標，重掃不得覆蓋增量進度或阻斷新成交保存；8 次上限只計實際嘗試的暫時性失敗（預算不足、scope 暫停不計；429 走共享 cooldown）；隔離期滿只釋放一次恢復嘗試。
complete 不定期重掃；遷移前缺證據的列標 evidence unknown、低優先級一次性核驗；有可追溯窗口與覆蓋證據者可沿用；重掃仍無法證明就不升級。
驗收以行為為主：注入不同週期驗證排程／planner／詳情補排／refreshing 一起變；fills／probe 同時積壓；重掃期間增量持續；舊探測回應不覆蓋新遍歷。部署後 6 小時是最低門檻，要看到跨輪更新與積壓走勢。 -->

#### 7.9a @inline：週期單一來源＋重試上限＋callback 不丟工作（先派）

**Files:** `src/spark/publicapi/config.py`、`scripts/run_api.py`、`src/spark/publicapi/app.py`（詳情頁補排）、`explore_fills_sync.py`（預設值）、`explore_scheduler.py`；`deploy/filet-api.service.d/explore-refresh.conf.example`；RUNBOOK §5.8e；對應測試。不動 schema、不動前端。

- **A1 週期單一來源**：同下方原點 1（config `explore_fills_period_s`＝env `FILET_EXPLORE_FILLS_PERIOD_S`，預設 21600，驗證 ≥ 3600；run_api → scheduler `fills_every_s`；scheduler → `plan_page(incremental_after_ms=int(fills_every_s*1000))`；`app.py` 詳情頁補排條件用 cfg；`_DEFAULT_INCREMENTAL_AFTER_MS = 6h` 僅為無 config 預設）。**驗收以行為為主**：測試用同一個 cfg 建 app＋scheduler，注入 `explore_fills_period_s=7200` 與 `=21600` 兩組，斷言 (i) scheduler complete 收尾後 `next_attempt_at ≈ now + period`；(ii) `plan_page` 在 `period − 1s` 為 noop、`period + 1s` 開增量輪；(iii) 詳情頁在 `updated_at` 距今 `period − 1s` 不補排、`refreshing False`，`period + 1s` 補排、`refreshing True`——四者隨同一個值一起變。字面值掃描（`rg "4 \* 3600|14400" src/spark/publicapi` 零命中）只作輔助。
- **A2 重試上限**（原點 9，加使用者修正）：`MAX_JOB_ATTEMPTS = 8`，**只計實際嘗試過且為暫時性失敗**（`ConnectionError`／`TimeoutError`／5xx 這條分支）；`BudgetExhausted`／`ScopePaused` 不計（既有 `bump_attempts=False`）；429 仍走 `note_429` 共享 cooldown 且不計。第 8 次 → `_quarantine`（24h、`last_error` 前綴 `max_attempts:`、計數 `quarantined_max_attempts`）。**隔離期滿只釋放一次恢復嘗試**：隔離時把 `attempts` 設為 `MAX_JOB_ATTEMPTS − 1`（期滿後失敗一次即再隔離，不會重新給 8 次）；成功一次 → attempts 歸零（確認 store 成功路徑歸零，否則補）。測試：連續 8 次 `ConnectionError` → 第 8 次隔離、`next_attempt_at ≈ now + 86400`、attempts == 7；期滿後再失敗 1 次 → 立即再隔離；期滿後成功 → attempts 0；穿插 `BudgetExhausted` 不推進 attempts。
- **A3 callback 不丟工作**（原點 7）：`_notify_dirty()` try/except → `logger.error`＋`dirty_errors`（health 可見）；`_run_fills`／`_run_cache_kind` 順序「寫 store → enqueue 下一個 job → notify」。測試：`on_dirty` 每次拋例外，連跑 20 tick，job 仍在且 `next_attempt_at > now`、`dirty_errors == 20`、`run_forever` 不死（用 stop_event 收尾）。
- drop-in example 加 `FILET_EXPLORE_FILLS_PERIOD_S=21600`；RUNBOOK §5.8e 週期表改 6 小時＋容量算式：需求＝300/6h（50）＋新增（≈1）＋多頁增量＋partial 重掃（4/日×~5 頁）＋核驗掃描＋探測（≤1/10）；消化上限 60，探測佔 ≤6 → fills ≥54。

**驗收（主線程親跑）**：ruff；`uv run pytest -q` 全綠 > 3191；A1 行為測試名列出；`rg -n "14400|fills_every_s: float = [0-9]" src/spark/publicapi scripts/run_api.py` 零命中<!-- 2026-09-21 主線程裁決：builder 首輪把 scheduler 類別預設留 14400（理由：既有 7.8 測試釘舊值）→ 不接受；改為唯一常數 `explore_fills_sync.DEFAULT_FILLS_PERIOD_S = 6*3600`，config 預設與 scheduler 預設皆 import 它，該測試改釘常數。`PARTIAL_RESCAN_AFTER_MS`（24h 重掃）與 `session_ttl_s`（SIWE）與本裁決無關，維持 -->；`repro_79a.py`（主線程寫：period 注入兩值四處同變）。

#### 7.9b @inline：獨立遍歷版本 `scan_id`＋雙游標＋探測 9:1＋證據窗口與 gap 檢查＋遷移核驗（7.9a 親驗後派）

**Files:** `explore_store.py`（schema v3、新表 `fills_scan`、CAS）、`explore_fills_sync.py`（scan planner 與增量 planner 分離）、`explore_scheduler.py`（`fills_scan` job kind、探測 9:1、核驗排程）、`explore_publisher.py`／`app.py`／`hl_explore.py`（`fills_coverage.evidence`）、`web/src/lib/publicApi.ts`（型別＋normalize）；對應測試；plan 契約表 A。

- **B1 資料模型（雙游標）**：
  - `fills_sync`（**增量軌**，持續前進，永不被重掃覆蓋）：保留 `synced_through_ms`／`cursor_ms`／`window_end_ms`（增量頻率＝7.9a 週期）；新增 `inc_from_ms INTEGER NULL`（增量軌的起點＝這條軌第一次建立時的 `now`；覆蓋區間＝`[inc_from_ms, synced_through_ms]`）；新增 `scan_id TEXT NULL`（最近一次**完成**的全區間遍歷）、`evidence_unknown INTEGER NOT NULL DEFAULT 0`。`completeness`／`reason` 改為「由最近完成的 scan 結果寫入」，增量輪不改。
  - 新表 `fills_scan`（**遍歷軌**，一個地址同時最多一個進行中）：`scan_id TEXT PRIMARY KEY`（uuid4，不可重用）、`address`、`kind`（`initial`｜`partial_rescan`｜`verify`）、`window_start_ms`、`window_end_ms`（＝開始遍歷時的 now）、`cursor_ms`、`pages_done`、`fills_in_window`、`status`（`running`｜`done`）、`result`（`complete`｜`partial`｜NULL）、`reason`、`started_at`、`finished_at`、`last_error`、`params_fp`。索引 `(address, status)`。
  - 覆蓋連續性：scan 完成時檢查 `scan.window_end_ms >= inc_from_ms`（遍歷伸進增量軌起點）→ `gap = False`；否則 `gap = True`（寫入 `fills_sync.coverage_gap`，新增欄位 `coverage_gap INTEGER NOT NULL DEFAULT 0`）。對外 `complete` 需同時 `result == complete AND gap == 0 AND evidence_unknown == 0`；否則 `partial`（reason `coverage_gap`／`evidence_unknown`）或原 reason。
  - 新地址：建 `fills_sync`（`inc_from_ms = synced_through_ms = now`、`completeness = backfilling`）＋一個 `initial` scan `[now−30d, now]`；增量與 scan 兩個 job 各自入列。
- **B2 planner 分離**：`plan_incremental(state, now, period)`（只做增量：到期開 `[synced_through−1, now]`、noop 自帶 `next_due`）與 `plan_scan(scan, now)`（從 `cursor` 抓到 `window_end`；短頁或既有終止條件收尾 → `result`／`reason`；門檻用 scan 自己的 `fills_in_window`）。`apply_page` 拆成兩個純函式。A1 的「輪進行中」語義保留在各自軌內。
- **B3 scheduler**：job kind 新增 `fills_scan`（priority：`initial` 2、`partial_rescan` 3、`verify` 4；`verify` 只在同 tick 沒有到期的 `fills`／`initial`／`partial_rescan` 時才領——嚴格讓位，且等待加權對 `verify` 不生效）。`fills`（增量）與 `fills_scan` 都走 `explore_fills` 保留額度、一頁即重排。partial：`fills_sync.completeness == partial AND 沒有進行中的 scan AND now − last_scan.finished_at ≥ 24h` → 建 `partial_rescan` scan。scan 完成 → **CAS** 寫回 `fills_sync`：`UPDATE fills_sync SET completeness=?, reason=?, scan_id=?, coverage_gap=?, evidence_unknown=0 WHERE address=? AND (scan_id IS NULL OR scan_id != ?)`；並保留舊 evidence（`fills_scan` 列不刪，`purge` 只清 `finished_at` 早於 30 天且非最新的）。
- **B4 探測**：候選＝`fills_sync.scan_id` 對應的 `fills_scan.result == complete AND reason == count_below_retention_threshold`（含 `evidence_unknown == 0`），依 `finished_at` 最舊者；探測窗口＝該 scan 的 `window_start_ms`；回寫 `UPDATE fills_scan SET reason=? WHERE scan_id=? AND reason=?` ＋ `UPDATE fills_sync SET reason=? WHERE address=? AND scan_id=?`（同一 transaction；任一 rowcount 0 → 回滾、計 `probe.stale`）。**9:1**：`_tick_once` 領工前，若探測候選存在且 `_fills_available() >= 120`：雙方都有積壓（有到期 fills 類 job）→ 用計數器 `_fills_served_since_probe`，≥ 9 才給 probe 一次並歸零；fills 類無到期 job → probe 可直接用；無探測候選 → fills 全拿。計數器 `probe.{total,verified,empty,failed,stale,candidates}`；移除 deque 與 `deferred*`／`dropped`。
- **B5 遷移 v2→v3**：建 `fills_scan`；每個既有 `fills_sync` 列：`inc_from_ms = synced_through_ms`（增量軌從現在的前沿起算）<!-- 2026-09-22 主線程裁決（正式機 287 列快照實跑發現）：backfilling 列的 `synced_through` 為 NULL，不得用遷移當下的 now 當增量起點——scan `window_end` 到 now 之間會成為無人抓的真實缺口、scan 完成即被判 coverage_gap；改為 `inc_from = synced_through = 該 running scan 的 window_end_ms`，由第一次增量輪補上 -->；若 `reason == retention_boundary_verified`（有可追溯窗口：探測時窗口即當時 `window_start_ms`，且探測在增量連續的同一列上）→ 建一筆 `done` scan（`kind=initial`、窗口＝該列現有 `window_start/end`、`result=complete`、`reason` 沿用、`finished_at=updated_at`）、`scan_id` 指向它、`evidence_unknown=0`；其他 complete／partial 列 → 同樣建 `done` scan 保存歷史標籤，但 `evidence_unknown=1`，對外 `partial`／reason `evidence_unknown`，並入列一個 `verify` scan（priority 4，`next_attempt_at` 在 48 小時內均勻攤開）；backfilling 列 → 轉成 `running` 的 `initial` scan（游標沿用）。核驗 scan 完成：`result complete` → 升級（`evidence_unknown=0`）；仍 `partial` → 維持 partial、reason 為核驗結果，不強行升級。docstring 如實：migration 各步冪等、DDL 自動提交。
- **B6 契約**：`fills_coverage.evidence = {scan_id, kind, window_start, window_end, finished_at, reason, gap: bool, unknown: bool}`（全部可 null）；頂層 `state`／`reason` 維持＝對外判定（含 gap／unknown 降級）；`window_start/window_end` 改為**增量軌覆蓋區間** `[inc_from, synced_through]`（契約表註明語義變更）。前端型別＋normalize，不改 UI。
- **B7 測試（行為級）**：(i) 重掃期間增量持續前進：partial 地址開 `partial_rescan` 多頁中途，增量到期 → 增量頁照抓、`synced_through` 前進、scan 游標不受影響；(ii) 舊探測回應不覆蓋新遍歷：探測發出後、回寫前，該地址完成新 scan（新 `scan_id`）→ 回寫 rowcount 0、`probe.stale == 1`、新 scan 的 reason 不變；(iii) fills／probe 同時積壓：10 個到期 fills＋10 個探測候選，30 tick 後 probe 次數 ≤ 3 且 ≥ 1，fills ≥ 27；只有探測候選時每 tick 一個 probe；(iv) gap：`inc_from` 晚於 scan `window_end` → `coverage_gap=1`、對外 partial／`coverage_gap`；(v) 遷移 seed DB 複本：verified 列 `evidence_unknown=0` 且無 verify job；其餘 complete 列 `evidence_unknown=1`、對外 partial、verify job 在 48h 內攤開；backfilling 列轉 running scan；(vi) verify scan 讓位：有到期增量時不被領；(vii) 重啟：新建 scheduler 實例後探測候選由 DB 推導照常進行；(viii) 新地址生命週期：建列 → initial scan 完成 → complete → 增量 → 三者順序與欄位。
- **B8 需求算式（RUNBOOK＋研究報告）**：需求／小時＝增量 300/6h（50）＋新增候選 initial（≈1×1.2）＋多頁增量（量測）＋partial 重掃（4/日）＋核驗掃描（遷移後 48h 內約 130×1.2 頁攤平≈3.3/h）＋探測（≤ 消化的 1/10）；消化上限 60（120 保留額度＝每分鐘一次）。部署後量測兩曲線。

**驗收（主線程親跑）**：ruff；pytest 全綠；vitest 全綠；seed DB migration 實跑（version 3、`fills_scan` 列數＝fills_sync 列數、verified 列 unknown=0、其餘 unknown=1、verify job 攤開）；`repro_79b.py`（主線程寫：重掃期間增量前進；舊探測不覆蓋新 scan；9:1）。**部署**：schema v3＋drop-in＋取樣器換版（取樣器加 `fills_scan` 統計、每地址增量輪間隔 p50/p95、health `hl_budget`）→ **至少 6 小時＝一個完整增量週期**，要看到：每個地址至少一次跨輪更新（`window_end_ms` 前進）、積壓（到期 fills 類 job 數與 `oldest_due`）走勢、核驗 scan 進度、探測比例 ≤ 1/10、零 429／Traceback。9/22 09:12 只出分版本階段報告。

#### 7.9c @inline：7.9b 複審修正（2 Critical＋5 Warning＋守門測試；2026-09-22 主線程裁決；升級 opus 派工）

<!-- 失敗軌跡（給接手者）：7.9b 由 sonnet builder 實作，兩輪都有理解性錯誤——第一輪遷移把 backfilling 列的增量起點設成遷移當下（真實缺口），
第二輪複審抓到：(C1) `_run_scan` partial 收尾 `enqueue(..., now + PARTIAL_RESCAN_AFTER_MS)` 把毫秒常數加到秒制時間（重掃排到 1,000 天後）；
(C2) `_enqueue_address_jobs` 每個 candidates 輪（30 分）都無條件 `enqueue(fills_scan)`（忽略 `bootstrap_address_fills` 回傳值），而 `ExploreStore.complete` 是 DELETE，
job 消失後下一輪又建、`_run_scan` 見 completeness!=backfilling 就開 `partial_rescan` → 主線程實跑複審腳本：單一地址 3 天 144 次 partial_rescan＋224 次增量。
正式機推估 300 地址每 ~3.5h 一次 30 天整窗遍歷 ≈ 2,000 次/日，fills 保留額度 60 頁/h 完全吃光、`fills_verify` 永久餓死。
兩次錯誤都是「排程觸發條件從 job 存在與否推導、而不是從狀態推導」這個形狀。 -->

**Files:** `explore_scheduler.py`、`explore_store.py`、`explore_fills_sync.py`；`tests/test_explore_scheduler.py`、`tests/test_explore_store.py`、`tests/test_explore_fills_sync.py`。不動契約鍵、不動前端。

1. **C1 單位**：`explore_fills_sync` 改為秒制單一常數 `PARTIAL_RESCAN_AFTER_S = 24 * 3600`，`PARTIAL_RESCAN_AFTER_MS = PARTIAL_RESCAN_AFTER_S * 1000`（若仍有毫秒用途）；scheduler 一律 `now + PARTIAL_RESCAN_AFTER_S`。測試：partial 收尾 → `fills_scan` job 的 `next_attempt_at` 在 `[now + 86400 − 1, now + 86400 + 1]`。
2. **C2 觸發條件改由狀態推導**：
   - `_enqueue_address_jobs`：只有 `bootstrap_address_fills(...)` 回 `True`（真的新建增量軌）時才入列 `fills_scan`（initial）；既有地址一律不入列 scan job。
   - `_run_scan` 領到 `fills_scan` job 但沒有 running scan 時：只在 `fills_sync.completeness == "partial"` **且** `now − 最近 done scan 的 finished_at ≥ PARTIAL_RESCAN_AFTER_S` 才開 `partial_rescan`；否則 `_complete(job)`、計數 `scan_job_dropped`、回 `"dropped"`（不開 scan、不排下一個）。`complete` 地址永遠不會被 scan job 觸發重掃。
   - 復原路徑（job 遺失時）：`_run_increment` 每次跑完，若 `completeness == "partial"` 且無 running scan 且 `now − finished_at ≥ PARTIAL_RESCAN_AFTER_S` 且無 `fills_scan` job → `enqueue(fills_scan, priority 3, now)`。partial 收尾時的 `now + 24h` 入列保留（主路徑），復原路徑是保險。
   - 守門測試：(a) complete 地址跑過 5 個 candidates 輪＋3 天時間，`fills_scan` 列恰好 1（initial）、`partial_rescan` 0、探測 ≤ 1；(b) partial 地址 → 第一次重掃在 +24h（±spread），3 天內 `partial_rescan` ≤ 3；(c) 刪掉 partial 地址的 `fills_scan` job 後，一個增量週期內被重新入列並執行；(d) 用主線程／複審的 `scratchpad/repro_rescan.py` 情境寫成正式測試（3 天、每 30 分 candidates 輪）。
3. **W1 遷移冪等**：`_migrate_v2_to_v3` 每列先查 `fills_scan` 是否已有該地址的列（有則跳過整列）；`INSERT OR IGNORE` 建 scan／verify job；`refresh_job` 改名用 `UPDATE ... WHERE key=? AND NOT EXISTS (SELECT 1 FROM refresh_job WHERE key=?)`。測試：正式機快照複本遷移後把 `schema_version` 改回 2 再開啟 → 不拋例外、列數與 job 數不變。
4. **W2 `observed_from/to`**：`apply_incremental_page` 維護 `observed_*`（min/max 併入）；`complete_scan` 把 scan 的 `observed_*` 併入 `fills_sync`（min/max）。測試：新地址 scan 完成後 coverage `observed_from/to` 非 null 且等於 fills 極值；增量後 `observed_to` 前進。
5. **W3 `purge`**：刪除 `fills_scan` 中 `status='done' AND finished_at < now − 30d AND scan_id != fills_sync.scan_id` 的列，計入 `counts["fills_scan"]`；整個地址 purge 時的連帶刪除也計數。測試。
6. **W4 準入**：`ADMISSION_MULTIPLIER` 改 7（每地址最多 6 種 kind＋餘裕），並在 `_tick_once` 的準入檢查改為「只擋既有地址的補建，不擋新候選 bootstrap」：新候選（`bootstrap_address_fills` 回 True）永遠允許建 job。測試：300 地址穩態（6 kind）＋129 verify 不觸發跳過；新候選在 cap 邊緣仍拿到 job。
7. **W5**：`complete_scan` CAS 回 False → `logger.warning`＋`status()["scan_writeback_failed"]`。
8. **S1 補回五條探測行為測試**（升級／probe_empty／窗外不升級且 failed 計數／例外不中斷 tick／退池不探）；**S2** `status()` 加 `scans_running`、`verify_remaining`、`due_by_kind`；**S3** gap 檢查 docstring 寫明只防遷移／時鐘異常；**S4** `inc_from_ms is None` 防禦（視同 `synced_through_ms`，不拋例外）。

**驗收（主線程親跑）**：ruff；pytest 全綠 > 3212；`repro_rescan.py`（複審腳本）3 天情境：`partial_rescan` ≤ 3、rescan job 到期 ≈ +1 天；`repro_79b.py` 仍 PASS；正式機快照遷移三次（含 version 改回 2 重跑）一致。

##### 7.9c 第二輪裁決（2026-09-22 使用者）：拆成 7.9c-D（資料層）與 7.9c-S（排程）平行派工，介面先釘死；最後主線程用正式機資料庫複本整合驗收

<!-- 使用者裁決要點：兩個 Critical 一起修、守住排程生命週期（秒／毫秒轉換集中在排程邊界、partial 重掃期限單一來源、fills_scan 只能因首次回補／partial 到期／明確修復需求建立，不能因 job 列已刪就重建）；
「partial 24h 後重掃」「complete 跨多次 candidates 輪不重掃」升為正式回歸測試；準入與核驗飢餓列為部署阻擋項（W4 不能只提高 cap：先依狀態決定需要哪些 job、逐項去重與准入，容量滿不能讓整批候選失去補排；
fills_verify 不能永久嚴格讓位，要有界等待或服務份額並納入額度與需求算式）；資料層 Warning 本批補齊（遷移保留原子交易並實作冪等；恢復 observed_*；scan 保留與清理計數；complete_scan 回傳區分成功／重複／過期／異常，CAS 落空不得被排程器當成正常收尾）；
coverage gap 不能改成「同步落後＝缺口」（落後＝stale；是否漏資料要看覆蓋區間與留存證據），補真正跨缺口案例，恢復仍存在的 probe 行為測試；inc_from_ms 非空在寫入／遷移邊界保證，不靠執行時隔離；
舊 complete 因缺證據轉 pending 符合契約，不以「維持約 214 個合格者」為驗收目標。 -->

**檔案所有權（兩個 builder 不得互改對方檔案）**
- **7.9c-D（資料層）**：`src/spark/publicapi/explore_store.py`、`src/spark/publicapi/explore_fills_sync.py`、`tests/test_explore_store.py`、`tests/test_explore_fills_sync.py`。
- **7.9c-S（排程）**：`src/spark/publicapi/explore_scheduler.py`、`tests/test_explore_scheduler.py`、`tests/test_api_ops.py`（只改 health 斷言）。
- 主線程：整合驗收、`explore_publisher.py`／`app.py` 若需微調、B8 文件（RUNBOOK／研究報告）。

**介面契約（D 提供、S 依賴；兩邊都以此為準，S 在 D 未完成前可先用 monkeypatch／fake store 寫測試）**
```python
# explore_fills_sync.py（D）
PARTIAL_RESCAN_AFTER_S = 24 * 3600          # 唯一來源（秒）；不得再出現 *_MS 版本
def partial_rescan_due(finished_at: float | None, now: float) -> bool   # finished_at None → True
# explore_store.py（D）
class ScanWriteback(str, Enum): APPLIED = "applied"; DUPLICATE = "duplicate"; STALE = "stale"; MISSING = "missing"
def complete_scan(self, address, fills, scan) -> ScanWriteback   # DB 例外照常拋出（不吞）
def bootstrap_address_fills(...) -> bool                          # 只有真的新建增量軌才 True
def latest_done_scan(self, address) -> FillsScan | None           # finished_at 最大者
def running_scan(self, address) -> FillsScan | None
def job_kinds(self, address) -> set[str]                          # 該地址現有 refresh_job 的 kind 集合
def oldest_due_at(self, now, *, kinds: tuple[str, ...] | None = None) -> float | None   # 既有方法加 kinds 參數
def purge(...) -> dict   # counts 加 "fills_scan" 鍵
```

**7.9c-D 工作項**
- D1 常數與判斷：`PARTIAL_RESCAN_AFTER_S`、`partial_rescan_due()`；刪除 `PARTIAL_RESCAN_AFTER_MS`（全 repo 零命中，S 那邊由 S 改）。
- D2 `apply_incremental_page` 維護 `observed_from/to_ms`（min/max 併入）；`complete_scan` 把 scan 的 `observed_*` 併入 `fills_sync`（min/max）。
- D3 `complete_scan` 回 `ScanWriteback`：CAS 命中 → APPLIED；`fills_sync.scan_id == scan.scan_id` 已是目前 → DUPLICATE（不重寫）；`fills_sync.scan_id` 指向 `started_at` 更晚的 scan → STALE；無 `fills_sync` 列 → MISSING。三種非 APPLIED 都**不改** `fills_sync`，但 `fills_scan` 仍標 done、fills 仍落地（資料不丟）。
- D4 `purge`：`fills_scan` 刪 `status='done' AND finished_at < now − 30d AND scan_id != fills_sync.scan_id`；整址 purge 連帶刪除；兩者都計入 `counts["fills_scan"]`。
- D5 遷移 v2→v3：DDL（`ALTER`／`CREATE TABLE IF NOT EXISTS`）各自冪等；**列迴圈＋版本更新包在一個顯式 `BEGIN IMMEDIATE … COMMIT`**（失敗全回滾）；列迴圈冪等（該地址已有 `fills_scan` 列則整列跳過；`INSERT OR IGNORE` 建 job；`refresh_job` 改名用 `NOT EXISTS` 守門）；`verify` job 攤開用地址雜湊決定（重跑不變）。測試：v2 快照複本遷移後把 version 改回 2 再開 → 無例外、列數與各 kind job 數不變；列迴圈中途注入例外 → version 仍 2、`fills_scan` 為空（回滾）。
- D6 `inc_from_ms` 非空邊界：`bootstrap_address_fills`／`insert_fills_page`／遷移寫入時 `inc_from_ms is None` → `ValueError`（不落地）；`ExploreStore.__init__` 遷移後檢查 `SELECT count(*) FROM fills_sync WHERE inc_from_ms IS NULL`，>0 → 拋 `RuntimeError`（啟動失敗、訊息含筆數）。`plan_incremental` 不再對 None 做退路。
- D7 gap 語義維持「覆蓋區間」定義（`scan.window_end_ms >= inc_from_ms`），docstring 寫明：落後＝stale 不是 gap；gap 只在遍歷窗口未接上增量軌起點時成立（遷移異常、時鐘異常、人工修復）。**真正跨缺口案例測試**：(i) 手寫 v2 backfilling 列且 `window_end` 早於 now 6h → 遷移後 `inc_from == window_end` → scan 完成 gap=0（修好的路徑）；(ii) 直接寫入 `inc_from_ms` 晚於 scan `window_end_ms` 的列 → `complete_scan` 後 `coverage_gap=1`、`external_coverage_state` 回 `("partial","coverage_gap")`；(iii) 之後一次 `partial_rescan`（`window_end=now ≥ inc_from`）完成 → gap 清 0。
- D8 `latest_done_scan`／`running_scan`／`job_kinds`／`oldest_due_at(kinds=)`。
- 驗收：ruff；`uv run pytest -q tests/test_explore_store.py tests/test_explore_fills_sync.py` 全綠；正式機快照（`scratchpad/prod_meta_v2.db` cp 後）遷移三次（含 version 回 2）一致且無例外；`rg -n "PARTIAL_RESCAN_AFTER_MS" src tests` 只剩 scheduler（S 會清）。

**7.9c-S 工作項**
- S1 單位：所有 `now + …` 用 `PARTIAL_RESCAN_AFTER_S`（秒）；scheduler 內任何毫秒換算只在呼叫 planner 的邊界（`int(now*1000)`），不得出現其他 `* 1000`／`/ 1000` 於排程時間。
- S2 `fills_scan` 建立只有三種來源：(a) `_enqueue_address_jobs` 中 `bootstrap_address_fills` 回 True（首次回補）；(b) `_run_scan` partial 收尾排 `now + PARTIAL_RESCAN_AFTER_S`（partial 到期）；(c) 修復：`_run_increment` 跑完後 `completeness=="partial" and partial_rescan_due(latest_done_scan.finished_at, now) and running_scan is None and "fills_scan" not in job_kinds` → 立即入列。其他路徑一律不建。`_run_scan` 領到 job 但無 running scan 時：`completeness=="partial" and partial_rescan_due(...)` 才開 `partial_rescan`；否則 `_complete(job)`、`scan_job_dropped += 1`、回 `"dropped"`。
- S3 依狀態決定需要的 job（W4）：`_enqueue_address_jobs(address, rank, now)` 改為「算需要集合 → 去重 → 逐項准入」：需要集合＝`{state, portfolio, ledger, fills}` ∪ (`{fills_scan}` 若 bootstrap True)；去重＝扣掉 `store.job_kinds(address)`；逐項准入＝每入列一個 job 前檢查 `jobs < cap`，cap 到了就只跳過**這一個**並計 `admission_skipped`，不跳過整批候選；新候選（bootstrap True）的 4＋1 個 job **不受 cap 限制**（保證新地址永遠有補排機會）。`ADMISSION_MULTIPLIER` 改 7 並在 docstring 列出 6 種 kind。
- S4 `fills_verify` 有界等待：維持「先讓位」，但若 `store.oldest_due_at(now, kinds=("fills_verify",))` 的等待 ≥ `VERIFY_MAX_WAIT_S = 2 * 3600`，本 tick 的 fills 類名額改給一個 verify（與 probe 的 9:1 同一套名額計算：verify 佔用一次 fills-like 名額）；計數 `verify_served_by_deadline`。需求算式（B8，主線程寫）納入：verify 份額上限＝每 2h 至少 1 次 ⇒ 129 列最慢 ~11 天，實際在 fills 空檔會更快。
- S5 `complete_scan` 回傳處理：APPLIED → 既有收尾；DUPLICATE → `logger.info`＋`scan_writeback_duplicate`，照常收尾；STALE → `logger.warning`＋`scan_writeback_stale`，**不**排 partial_rescan、不探測；MISSING → `logger.warning`＋`scan_writeback_missing`、`_complete(job)` 回 `"dropped"`。
- S6 `status()` 加 `scans_running`、`verify_remaining`、`due_by_kind`（六種 kind 到期數）、`scan_job_dropped`、`admission_skipped`、`verify_served_by_deadline`、三個 writeback 計數。
- S7 測試（全部正式回歸測試，放 `tests/test_explore_scheduler.py`）：(a) **complete 地址跨 5 個 candidates 輪＋3 天：`fills_scan` 列恰 1（initial）、`partial_rescan` 0**；(b) **partial 地址 → 第一次重掃到期在 `[now+86400−60, now+86400+60]`，3 天內 partial_rescan ≤ 3**；(c) 刪掉 partial 地址的 `fills_scan` job → 一個增量週期內修復入列並執行；(d) 複審腳本 `scratchpad/repro_rescan.py` 的 3 天／每 30 分 candidates 輪情境照抄成測試（斷言同 (a)）；(e) 準入：cap 邊緣時既有地址只跳過超額的單一 job、新候選仍拿到全部 job；(f) verify 等待 ≥2h 後被服務（且不超過名額）、未滿 2h 時嚴格讓位；(g) 四種 `ScanWriteback` 各自的收尾行為（用 fake store 或 monkeypatch）；(h) 恢復五條 probe 行為測試（升級／probe_empty／窗外不升級且 failed 計數／例外不中斷 tick／退池不探）。
- 驗收：ruff；`uv run pytest -q tests/test_explore_scheduler.py tests/test_api_ops.py` 全綠（D 未完成時允許以 fake store 通過，整合後主線程重跑）；`rg -n "PARTIAL_RESCAN_AFTER_MS|\* 1000|/ 1000" src/spark/publicapi/explore_scheduler.py` 只剩呼叫 planner 的 `int(now * 1000)`。

**主線程整合驗收（兩批 commit 後）**：全量 pytest＋vitest＋ruff；正式機快照複本（`prod_meta_v2.db` cp）：遷移 → 關閉重開（模擬重啟）→ 以 fake HL 模擬 3 天運行（每 30 分 candidates 輪、6h 增量、真實 300 地址）→ 斷言：`fills_scan` 新增列數 ≤ 初始 running 完成數＋partial 重掃數（無反覆新增）、`fills_verify` 129 列持續遞減至 0、六種 kind 的 `oldest_due` 都有界（無飢餓）、探測 ≤ fills-like 的 1/10；`repro_rescan.py`／`repro_79b.py` PASS；B8 文件（需求算式含 verify 份額）。之後才派 fresh reviewer、再部署。

<!-- 原 v1 條文保留於下作對照；派工以上方 7.9a／7.9b／7.9c 為準，衝突時以 v2 為準。 -->
### Task 7.9 v1（已被 v2 取代，僅供對照）：fills 週期 6 小時單一來源＋partial 持續增量＋探測證據窗口化＋回寫保護＋探測排程耐重啟（2026-09-21 使用者裁決）

<!-- 裁決：選 (b) 6 小時（不選 5）：單頁基線 300/6h＝50／小時＋新增約 1，才對 60 留出空間，仍須扣多頁與探測成本；排程、詳情頁補排、前端「更新中」共用同一期限，不得留寫死 4 小時。
partial 不得停抓 24 小時（要持續增量保存）；探測有效性按窗口限定；探測回寫要版本保護；溢位／重啟要能復原；callback 不丟工作；公平排程要有證據。 -->

**Files:** `src/spark/publicapi/config.py`、`scripts/run_api.py`、`src/spark/publicapi/app.py`（詳情頁補排條件）、`explore_fills_sync.py`、`explore_scheduler.py`、`explore_store.py`（schema v3）、`explore_publisher.py`／`hl_explore.py`（契約 `fills_coverage.evidence`）、`web/src/lib/publicApi.ts`（型別＋normalize，不改 UI）、`deploy/filet-api.service.d/explore-refresh.conf.example`、RUNBOOK §5.8e；對應測試。

1. **週期單一來源**：`config.py` 加 `explore_fills_period_s: int = 21600`（env `FILET_EXPLORE_FILLS_PERIOD_S`，驗證 ≥ 3600）。`run_api.py` 傳 `fills_every_s=cfg.explore_fills_period_s`；scheduler 呼叫 `plan_page(..., incremental_after_ms=int(self._fills_every_s * 1000))`；`app.py:2565` 詳情頁補排條件改 `now - sync.updated_at >= cfg.explore_fills_period_s`（`create_app` 已有 cfg）；`explore_fills_sync._DEFAULT_INCREMENTAL_AFTER_MS` 改 `6 * 3600 * 1000` 並註明「只是無 config 時的預設，生產一律由 config 注入」。結構性守門：測試 `rg`／`grep` 斷言 `src/spark/publicapi` 內無 `4 * 3600`、`14400`、`4h`（字面）；一條測試同時建 app＋scheduler，斷言兩者讀到同一個值。drop-in example 加 `FILET_EXPLORE_FILLS_PERIOD_S=21600`；RUNBOOK §5.8e 週期表改 6 小時並寫容量算式（50＋1／小時，扣多頁與探測）。前端沒有獨立的「更新中」文案（`refreshing` 只由後端算、前端透傳），故無前端改動；契約表註明 `refreshing` 的 fills 條件＝同一 config。
2. **schema v3**：`fills_sync` 加 `full_scan_at REAL NULL`、`full_scan_window_start_ms INTEGER NULL`、`full_scan_window_end_ms INTEGER NULL`（「最近一次全區間遍歷」的完成時間與查詢窗口；reason 描述的就是這次遍歷）。`apply_page` 在 **backfilling 輪終止**（短頁 complete／partial，或 same_ms_overflow／no_progress／page_cap）時寫入三欄＝本輪 `window_start/end`＋`now`；增量輪不動。migration v2→v3：既有列 `full_scan_at = updated_at`、兩個窗口 NULL（遷移前資料，證據窗口未知；契約與文件如實標示）。migration 仍照 v2 的形狀（ALTER 自動提交、各步冪等、docstring 如實）。
3. **partial 持續增量＋定期重掃**：`plan_page` 的 `partial` 分支：(i) 輪進行中 → 續抓（A1 不變）；(ii) `now - full_scan_at >= PARTIAL_RESCAN_AFTER_MS (24h)`（或 `full_scan_at is None`）且不在輪中 → 整窗重掃（新 backfilling 輪，本地 fills 保留）；(iii) 否則與 complete 相同：到增量寬限期就開增量輪（保留 completeness／reason，A2）；(iv) 都不到 → noop，`next_due_ms = min(window_end + incremental_after, full_scan_at + 24h)`。complete 分支不做定期重掃（既有行為；若要一次性重掃遷移前的 complete 列，另開 task）。
4. **探測證據窗口化**：探測窗口由 `full_scan_window_start_ms` 決定（NULL → 用當時 `window_start_ms`，並在探測成功時把用到的窗口寫進 `full_scan_window_*`，as 證據窗口）；探測結論（`retention_boundary_verified`／`…_probe_empty`）只對該 `full_scan_at` 有效——新的全區間遍歷會重寫 reason（backfilling 輪終止時 reason 由 `apply_page` 決定，自動覆蓋）。契約：`fills_coverage` 加 `evidence: {window_start: epoch_ms|null, window_end: epoch_ms|null, at: epoch_s|null}`；`reason` 位置不變＝屬於 `evidence` 的結論；`window_start/window_end` 仍＝目前輪。前端型別＋normalize（null 保持 null），不改 UI。
5. **回寫版本保護**：`ExploreStore.set_sync_reason(address, reason, *, expect_full_scan_at, expect_reason)` → `UPDATE ... WHERE address=? AND full_scan_at IS ? AND reason IS ?`，rowcount 0 → 回 False；scheduler 計 `probe.stale += 1`、不視為失敗。
6. **探測排程耐重啟、無溢位**：刪除 in-memory deque；改為 store 查詢 `next_probe_candidate(now)`＝`completeness='complete' AND reason='count_below_retention_threshold' AND active` 依 `full_scan_at` 最舊者一筆。`_run_fills` 收尾**不再直接探測**（所有探測走同一路徑）。`_tick_once` 領工前：有候選且 `_fills_available() >= FILLS_PAGE_WEIGHT` 時，與到期 fills job **交替**（`self._probe_turn` 布林，雙方都有需求時各半；只有一方有需求時不交替），每 tick 至多一個探測。計數器 `probe.{total,verified,empty,failed,stale}`＋`candidates`（目前待探數）；移除 `deferred*`／`dropped`（改由 DB 推導，無佇列可溢位）。
7. **callback 不丟工作**：scheduler 加 `_notify_dirty()`：try/except 包 `on_dirty`，例外 → `logger.error`＋`dirty_errors` 計數（health 可見），不逸出；`_run_fills` 順序改為「寫 store → enqueue 下一個 job → notify_dirty」，所有 `_complete(job)` 之後、enqueue 之前不呼叫任何外部 callback。測試：`on_dirty` 每次拋例外，連跑 20 tick，`refresh_job` 仍有該地址的 fills job 且 `next_attempt_at > now`、`dirty_errors == 20`、scheduler thread 不死。
8. **詳情頁**：條件見點 1；partial 地址現在有增量，24 小時內 `updated_at` 會前進，不再每請求空轉。
9. **重試上限**（7.4c 第四項；主線程已查證 `explore_scheduler.py:351-361`）：語義錯誤 → 立即隔離 24 小時（有）；暫時性錯誤（連線／逾時／5xx）→ 指數退避封頂 900 秒＋計 `attempts`，**但沒有嘗試上限**＝持續故障的地址每 15 分鐘無限重試（成本有界但無終止）。加 `MAX_JOB_ATTEMPTS = 8`：`job.attempts + 1 >= MAX_JOB_ATTEMPTS` → 改走 `_quarantine`（24 小時＋`last_error` 前綴 `max_attempts:`），計數 `quarantined_max_attempts`；成功一次 attempts 歸零（確認 store `complete`／成功路徑已歸零，否則補）。測試：連續 8 次 `ConnectionError` → 第 8 次隔離、`next_attempt_at ≈ now + 86400`；第 7 次成功 → attempts 歸零。
10. **取樣器**：`/home/ubuntu/explore-obs/sample.py` 加 health 快照（`hl_budget.used`／`wait_ms`／`http` 三鍵，需 admin session 的話改讀本機 8700 的 ops 端點；builder 不動正式機，只在 repo `scripts/explore_obs_sample.py` 提供新版，主線程部署時替換）與「每地址上一輪到本輪的間隔」分佈（p50/p95，由 `fills_sync.window_end_ms` 差分）。

**測試**：點 1 結構性 grep 測試＋同值測試；點 2 migration（seed DB 複本：version 3、既有列 `full_scan_at == updated_at`、窗口 NULL）；點 3 partial 三路徑（重掃到期／增量到期／noop 的 `next_due_ms` 取 min）＋增量期間 fills 確實寫入；點 4 探測用 `full_scan_window_start`、NULL 時寫回；點 5 CAS 命中／未命中；點 6 重啟（新建 scheduler 實例）後候選仍被探、雙方都有需求時 20 tick 各 ≥ 8 次、只一方有需求時不空轉；點 7；點 9。前端 vitest：`evidence` normalize。

**驗收（主線程親跑）**：ruff 乾淨；`uv run pytest -q` > 3191 全綠；vitest 全綠；`rg -n "4 \* 3600|14400" src/spark/publicapi` 零命中；seed DB migration 實跑；`repro_79.py`（主線程寫：partial 5h 後開增量輪且保留 partial、25h 後重掃、evidence 欄位）。**部署**：schema v3＋drop-in `FILET_EXPLORE_FILLS_PERIOD_S=21600`＋取樣器換版 → 之後至少觀察**一個完整 6 小時週期**：消化／需求兩條曲線（需求＝50＋新增＋多頁＋探測）、`oldest_due` 趨勢、每地址輪間隔 p50/p95（公平排程證據）、探測計數、零 429／Traceback；臨時 cron 保留到正式採樣接替。9/22 09:12 只出**分版本階段報告**，不當作現役版完整 24 小時驗收。

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

### 4.0 目前狀態（2026-09-20 15:00 UTC 更新）

- **P0–P7 全部完成並四次部署到正式機**（最新 7db0720，2026-09-21 01:11 UTC：父子 scope 保留額度、週期放寬；背景刷新已開、無門檻、三態資格）；`main`＝`feat/explore-rate-limit`（GitHub 同步）。
- 正式機：8ead8e8，`EXPLORE_UPSTREAM_REFRESH=1` 自 14:21 UTC 起刷新。14:58 讀值：portfolio 170/300、state 301、ledger 171、
  fills 14,247（7 complete／4 backfilling）、job 錯誤 0、零 Traceback、零 HTTP 429（journal grep「429」會誤中舊 PID 429066，看時間戳）。
  首次換版門檻 240/300，預計 15:10 前後；換版時 `explore_index.json.v3.bak` 出現、公開榜 `published_at` 前進。
- **後續待辦（非阻塞，下一輪再排）**：
  1. 探索頁本身沒有「成交資料不完整」提示（4.2 發現；`fills_coverage` 已進型別）——是否加 UI 由使用者決定。
  2. 第四輪／小審的 Suggestion：門檻 `out:` 訊息帶原因字串；`page_cap` 持續出現時調高上限的 RUNBOOK 提醒；`.daily` 只是「≥24h 前的版本」非 last-good（已改文字）。
  3. `tests/publicapi_helpers.py:36` 殘留 `build_sync` 文字（3.4 builder 回報，文件字串）。
  4. spec §11 指標中尚未做的：Prometheus 式 metrics（目前以 `/api/ops/health` 快照代替）。
  5. 觀察 24 小時後決定是否調整 `FILET_HL_EXPLORE_WEIGHT_CAP`／週期（spec §6 起始值）。

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
| 2.1 | 2026-09-20 | ea8acb7 | `pytest tests/test_explore_store.py` 21 passed；ruff 乾淨；`claim_due` 用 RETURNING（本機與 prod SQLite 皆 3.53.1）；transaction 採 `with self._db:` 隱式（builder 裁決，與 ApiStore 一致） |
| 2.3 | 2026-09-20 | aa8a292 | 140 passed（config＋ops）；`FILET_EXPLORE_DB` 可選（P3 起必填）；run_api 注入；health `explore_store` |
| 2.2 | 2026-09-20 | d57365b＋7add2b8 | 17 passed；零 `unknown`、cursor 零 `+1`；增量輪 `fills_in_window` 重置（主線程驗收時抓到、已修） |
| 3.2 | 2026-09-20 | 77ebea2 | `pytest tests/test_hl_fills_page.py` 6 passed；body 與分頁器第一頁相同；經 limiter 預留 120 結算 23 |
| 3.3 | 2026-09-20 | 97a8e92 | 110 passed（traders＋explore）；`TraderData` dataclass；池內零上游、stale 入列去重、準入上限；回應加 source／refreshing／as_of／fills_coverage |
| 3.1 | 2026-09-20 | 80155fc | 33 passed（scheduler 8＋store）；零 `time.sleep`／`logger.info`；含 ledger kind；store 加 `set_sync_error`／`oldest_due_at`；300 池 6,600 權重 ≥22 分鐘、任一分鐘 ≤300 |
| 4.2 | 2026-09-20 | 0b29710 | vitest 724 passed；`fillsIncomplete()` 單一判準、型別全 optional（新舊後端混跑窗口）。<!-- 裁決：探索頁原本就沒有完整性提示，本輪不加新 UI（D5 精神），欄位已進型別供後續使用 --> |
| 4.1 | 2026-09-20 | 8cdbf4e | 118 passed（publisher＋explore＋traders）；v4 快照、v3 相容載入；`enrich_candidate` 容忍 None（`_apply_tags`／`qualify` 加 None 守衛）；既有三條版本測試改為 `VERSION-2` 表「不相容」 |
| 5.1 | 2026-09-20 | 89957c9 | RUNBOOK §5.8e（env／drop-in／觀測／停用／回退）＋`deploy/filet-api.service.d/explore-refresh.conf.example`；`test_deploy_artifacts` 28 passed |
| 3.4 | 2026-09-20 | 4d851e5 | 舊 build_sync 路徑 `rg` 零命中；`run_api` 起 `explore-scheduler` thread（僅 `EXPLORE_UPSTREAM_REFRESH=1`）；config 開啟時 db 必填；ops/health `explore_refresh`／`explore_publisher`；216 passed（explore＋config＋ops＋wiring）；全專案 3043 |
| **P2–P5 程式完成** | 2026-09-20 | — | opus reviewer：2 Critical／5 Warning／4 Suggestion → 主線程逐條讀碼確認 → Task 3.5（派工中） |
| 3.5 | 2026-09-20 | 3c70459 | 主線程實跑五判準（0600、jobs (4,2)→退池刪 4→(0,1)、常數同源）全對；`self._sleep(1.0)` 於例外路徑；publisher 門檻／備份 13 命中；3064 passed；ruff 乾淨。複審派工中。5 分鐘短跑（正式機 v3 種子）：`gate_skips 5`／`last_gate 20/300`／`publishes 0`、快照未覆寫、`total_qualified 19` 與正式機一致、0 次 429、db 0600 |
| 3.5 複審 | 2026-09-20 | — | opus：前輪 11 項 10 關閉 1 部分；新 1 Critical（門檻 n==0 旁路）／3 Warning／4 Suggestion → Task 3.6 |
| 3.6 | 2026-09-20 | 4ff01cf | 主線程實跑：側檔三個 0600、`n==0` 不發布且快照未寫、`force` 發布、`empty candidate rows` 路徑存在；3071 passed；ruff 乾淨。第三輪複審派工中 |
| 3.6 複審 | 2026-09-20 | — | opus 第三輪：7 項全關閉；新 1 Critical（門檻輸入／輸出不同源）／2 Warning／2 Suggestion → Task 3.7 |
| 3.7 | 2026-09-20 | 1f0978f | 主線程驗：`_gate_reason`／`.prev` 9 命中、`params_fp=''`、`page_cap`、streak；3077 passed；ruff 乾淨。第四輪聚焦複審派工中 |
| 3.7 複審 | 2026-09-20 | — | opus 第四輪：5 項全關閉、0 Critical、2 Warning（`.prev` 一分鐘窗口、門檻擋下無 log）；結論**可部署** |
| 3.8（主線程） | 2026-09-20 | bb71e23＋3e71249 | 主線程自修兩個 Warning：`_note_gate` 第 1 次與每 10 次 warning；`.daily` 每 24h 至多輪替一次。小型 fresh reviewer PASS，其 2 Warning（時基混用、非原子 copy）已於 3e71249 修：以快照 mtime 同基底判斷、`.tmp`＋`os.replace`；3079 passed |
| 5.2 本機觀測 | 2026-09-20 | — | 10 分鐘主網唯讀實跑：0 次 429、explore 視窗最高 300 不超、退款 574、state 191／portfolio 39／fills 10 頁、7 地址 complete、publisher 10 次 0 失敗；**實證 C2**（300 列只 1 列合格）；冷啟動估 60–80 分鐘。報告：`docs/superpowers/research/2026-09-20-explore-refresh-observation.md` |
| 6.1＋6.4 測試 | 2026-09-20 | 2e0797c | 主線程實跑 classify 三判準（pending/fills_unknown、ineligible/live_days、ineligible/min_fills）；門檻殘留零；3093 passed；ruff 乾淨。⚠️ 以正式機 v3 快照本地驗：新規則下 qualified 0／pending 122／ineligible 177（D13 不沿用舊成交，遷移列 coverage=backfilling）——部署後公開榜會先顯示 122 列待確認，直到回補完成 |
| 6.3 | 2026-09-21 | edd3aea | wait_ms p50/p95、note_http 含重試、dashboard_latency、follower_budget_note；RUNBOOK §5.8e 去門檻語義；3107 passed |
| P6 複審 | 2026-09-21 | — | opus：後端契約符合、v3 分類獨立重現一致（177 ineligible 合理）；1 Critical（前端 live_days null→0）／5 Warning → Task 6.6（前端）、6.7（後端） |
| 6.5 | 2026-09-21 | 21357d5 | 主線程整合實測發現遷移列未遮罩→修：`to_dict` 統一遮罩、v3 遷移 `order_count_30d`=0；實跑 `pending None None [] [] 0`、頁面預設篩選 0／287／12；3112 passed |
| 6.6 | 2026-09-21 | 9156223 | vitest 743 passed；live_days／成交衍生欄位 null 不補 0、型別 `number|null`；組標題 CSS 與格線內；`getPublicStrategies`／詳情頁的既有補 0 未在範圍（記後續） |
| 6.7 | 2026-09-21 | 7da81e2 | compose／v3 遷移列帶預設門檻分類（主線程實跑 Counter ineligible 177／pending 122）；6.4(b)(c) 改非 force（推進 clock）；3114 passed；6.4 三案例在最終 commit 3 passed |
| 6.5–6.7 複審 | 2026-09-21 | — | opus：7 項全關閉（正式機 v3 快照實跑重現）；0 Critical、2 Warning（遷移列 fills_truncated 未同步、快照分類門檻來源）→ 主線程自修 718cc18；結論可部署 |
| 6.8 | 2026-09-21 | 6004632 | vitest 746 passed；總數／頁數／候選說明用合格＋待確認；舊後端行為不變 |
| **P6 最終驗收** | 2026-09-21 | 6004632 | 全量 pytest 3115、vitest 746、ruff 乾淨；6.4 三案例 3 passed；本機前後端整合（API 8700＋web 3100、正式機 v3 快照、flag 0）：嚴格與預設篩選皆 0 合格、第一頁「資格待確認」組、遮罩生效、無集中度標籤、僅合格切換帶參數且空榜、零 console 錯誤。**待第三次部署** |
| 6.2 | 2026-09-20 | 2d1e255 | vitest 736 passed；分組不重排、pending 無名次、分析待完成、僅合格切換、詳情頁觀測期間（epoch ms）；集中度目前無欄位故無需標示 |
| 7.1 | 2026-09-21 | 9d84c57 | `fills_coverage.synced_through`／`last_success_at` 全鏈（compose／詳情頁／v3 遷移 null／前端型別與觀測期間文案）；**未部署（觀測期）** |
| 7.2 | 2026-09-21 | b1aefff | 對照表：四處皆純展示 → null 保持 null、畫面「—」；順修 `null >= 0` 樣式 bug；vitest 757；**未部署** |
| 7.4a | 2026-09-21 | fc6a900 | 限流器父子 scope＋`available()`；主線程實跑 base 3×60 後第 4 次拒、fills 仍 120、父滿時兩子歸零；44 passed；3124 全綠 |
| 7.4b | 2026-09-21 | c184c03 | 週期 1800/7200/7200；首 tick 重排 overdue；類別感知領工＋同 tick 跨類 fallback；等待加權；無 limiter 時交替（僅雙方都到期）；飢餓重現：30 分鐘 30 頁、state 持續、切片 ≤300／base ≤180；94 passed、3135 全綠。<!-- 裁決：舊測試「state 先於 fills」改行為級斷言（被取代的嚴格優先級） --> |
| 7.4c | 2026-09-21 | aa65274 | config 兩個 cap＋驗證；run_api 三 scope＋parents、scheduler 三 gateway；health `explore_budget_note`；RUNBOOK §5.8e 新週期／保留額度／部署後檢查／觀測重新起算；3143 passed。<!-- 裁決：既有 test_hl_weight_caps_read_from_env 改 env 值使 base+fills≤explore --> |
| 7.5 | 2026-09-21 | 347c961 | 留存門檻命名、complete reason 碼、留存邊界探測、`params_fp`（schema v2 migration）、契約三鍵；主線程用種子 DB 驗 migration：version 2、complete 無 NULL reason；3159 pytest、759 vitest。**未部署**；複審（opus）：無 Critical、W1–W4／S1–S4 → 主線程核實後併入 Task 7.6；主線程另重現增量輪停滯與 partial 誤升兩缺陷（7.6 卡註解） |
| 7.6 | 2026-09-21 | 045472c | 增量輪「輪進行中」續抓（修停滯）；增量短頁保留 completeness／reason（partial 不誤升、verified 跨輪存活）；探測只在未 verified 且在池時做、回應經 `validate_page`、四計數器進 health；`set_sync_reason` 不動 `updated_at`；`PARAMS_FP="aggregateByTime=<omitted>"`；migration docstring 誠實化；`_row_from_dict` coverage 補鍵。主線程親驗：repro PASS（修前 FAIL）、ruff 乾淨、3171 passed、`_validate_page` 零命中。<!-- 裁決：plan 寫 `stats()` 實為 scheduler `status()`，builder 依既有慣例掛在 status()，health 經 `**scheduler.status()` 自然帶出 --> 複審（opus）：W1–W3／S1–S4 → Task 7.7；**已隨 7.5 於第五次部署上線（使用者授權，見下）** |
| **第五次部署（7.5＋7.6）** | 2026-09-21 05:54 UTC | 85942cc | 使用者授權。rsync 兩段 → import → build → chown → DB 備份 `explore.db.pre-75.bak`（sqlite backup API）＋快照 `.pre-75.bak` → restart（flag 維持 1）→ schema 2、complete 無 NULL reason、零 Traceback／429 → `DEPLOYED_VERSION` → 回歸 67/67。觀測期不重新起算。正式機實證：探測在 explore_fills 保留額度下每次 BudgetExhausted（→ 7.7 點 8） |
| 7.7 | 2026-09-21 | 6ca8c69 | partial 每 24h 整窗重掃（`PARTIAL_RESCAN_AFTER_MS`）、`…_probe_empty` reason、探測額度不足 → deferred 佇列下 tick 補打（`probe.deferred`／`deferred_total`）、S2 `_on_dirty`、S4 try、docstring 不變量。主線程親驗：`repro_77_main.py` PASS（partial 5h noop／25h 重掃／重掃復原 complete／重掃多頁續抓／complete 仍 4h 增量）、ruff 乾淨、3183 passed、probe 測試 14 passed。<!-- 裁決：`_probe_retention_boundary` 改吃 `window_start_ms`（佇列記進佇列當下的起點）；`probe` dict 加兩鍵，既有精確相等測試更新為含新鍵——屬點 8 必然結果 --> 複審（opus）：**Critical**（noop 重排到過去 → 緊迴圈餓死所有 job，主線程實跑 200 tick 全 `ran:fills` 核實）→ Task 7.8；W1 plan 7.6 A1 由主線程改；W2 探測與 fills 頁輪流分保留額度為已接受暫時代價；W3／S1／S3／S4 → 7.8。**未部署** |
| 7.8 | 2026-09-21 | 589b077 | `PagePlan.next_due_ms`（noop 自帶失效時刻：complete 4h／partial 24h）、`_run_fills` noop 重排 `max(next_due, now+jit)`＋None 防禦、`probe.dropped`（溢位＋過期視窗）、回歸守門測試（partial noop 不排到過去、50 tick 不餓死 state）。主線程親驗：複審重現腳本修前 `{'ran:fills': 200}` → 修後 `{'ran:fills': 2, 'ran:state': 2, 'idle': 196}`；`repro_77_main.py` PASS；ruff 乾淨；3191 passed。<!-- 裁決：S3 保留 `now` 參數改為真的使用（debug log）；`probe` 加 `dropped` 鍵，6 處精確相等測試補鍵 --> 合併複審（opus）：**可部署**、無 Critical；回歸測試在 6ca8c69 worktree 實跑會 FAIL（真的抓得到）；排到過去枚舉全部 ≥ now。Warning 兩條併下一批（Task 7.9 候選）：`test_incremental_round_short_page_preserves_partial_reason` 空測試；`app.py` 詳情頁按需入列對 partial 地址每請求拉回 now（無害 noop） |
| 7.9a | 2026-09-21 | effe96a＋c1d095f | 週期單一來源（`DEFAULT_FILLS_PERIOD_S=21600` → config 預設／scheduler 預設／`plan_page` 寬限／詳情頁補排／`refreshing`）；`MAX_JOB_ATTEMPTS=8` 只計暫時性實際失敗、隔離期滿一次恢復、429 分支補 `bump_attempts=False`；`_notify_dirty` 與「先 enqueue 後通知」。主線程親驗：`repro_79a.py` A/B/C 通過（D 為腳本自身缺完整 env，改以 config 測試佐證）、ruff 乾淨、`rg 14400` 零命中、3209 passed。**未部署（與 7.9b 同批）**<!-- 裁決：builder 首輪留 14400 預設不接受，補一輪改常數 --> |
| 7.9b | 2026-09-22 | 60ae3fa＋5af2c84 | 兩軌分離（`fills_scan` 新表、`scan_id`、CAS 寫回、gap／unknown 降級、`evidence` 契約）、探測 DB 推導＋雙 CAS＋9:1 借用、`fills_verify` 嚴格讓位、v2→v3 遷移（verified 沿用、其餘 unknown＋48h 攤開核驗、backfilling→running scan 且增量起點＝scan 窗口末端）。主線程親驗：正式機 287 列快照遷移兩次一致（69 backfilling 起點正確、218 done scan 無 gap、129 unknown／129 verify job 不同時間）；`repro_79b.py` 四項 PASS；ruff 乾淨；3212 passed、vitest 761。複審（opus）：**2 Critical**（partial 重掃排到 1,000 天後；每 candidates 輪重建 scan job → complete 地址反覆整窗重掃，主線程實跑 3 天 144 次）＋5 Warning → Task 7.9c（升級 opus 派工）。**未部署**。⚠️ 部署可見影響：對外 complete 190→89（129 列 unknown 降 partial），48h 內核驗恢復<!-- 裁決：遷移 backfilling 列增量起點改 scan window_end（原用 now 會留真實缺口）；builder 自決的 `fills_verify` 獨立 kind、observed_* 仍取增量軌、ADMISSION_MULTIPLIER 未調——待複審意見 --> |
| **第六次部署（7.7＋7.8）** | 2026-09-21 06:54 UTC | 7150d81 | rsync 兩段 → import → web／deps 無變動略過 build → chown → DB 備份 `.pre-78.bak` → 只 restart filet-api → active、零 Traceback → `DEPLOYED_VERSION` → 回歸 PASS。觀測期不重新起算 |
| **第四次部署（7.4＋7.1／7.2）** | 2026-09-21 01:11 UTC | 7db0720 | 複審可部署（3 Warning 已修：重排失敗大聲、scope 名同源、額度不足保留到期時間）；本機 6 分鐘實測 fills 6 頁；正式機 flag 1 後 100 秒 fills_sync 2 筆更新、零錯誤。**24h 觀測自 01:12 UTC 重新起算** |
| **第三次部署（P6）** | 2026-09-20 16:42 UTC<!-- 校正 --> | 5e2ec8e | flag 0 驗證與本機一致 → 67/67 → flag 1（16:43）→ 16:44 首次發布、16:45 第二次；合格 2／待確認 288／不合格 10；零錯誤。24h 觀測期自 16:45 UTC 起算 |
| **第二次部署（D8）** | 2026-09-20 14:20 UTC | 8ead8e8 | 使用者授權（「請繼續」）。flag 0 部署→驗證→67/67→flag 1（14:21）；90 秒後候選 300、快取 36、門檻擋下 in:0/300、快照未動、零 Traceback／429。冷啟動觀測進行中（每 10 分鐘），預期 50–65 分鐘首次換版 |
| **第一次部署（D8）** | 2026-09-20 04:09 UTC | 4295ece | 使用者授權。rsync 兩段、web build、drop-in `hl-budget.conf`、restart api＋dashboard、DEPLOYED_VERSION；`filet_regression_check --http --ssh` 67/67；20 次 explore GET 零上游行。記錄：RUNBOOK 部署日誌 2026-09-20 條 |
