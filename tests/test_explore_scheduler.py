"""tests/test_explore_scheduler.py — Task 3.1／7.9b：ExploreScheduler（單 thread、
逐 job、分層更新、限流讓位、lease/fencing、遍歷軌／增量軌分離、探測 9:1）。plan
docs/superpowers/plans/2026-09-20-explore-rate-limit-refactor.md Task 3.1／7.9b 卡片。

全離線；HL 呼叫一律走假物件或 `HLGateway`＋假 `post_fn`（tests/test_hl_gateway_budget.py
的 fake clock 慣例），DB 一律 `tmp_path`。
"""
from __future__ import annotations

import threading

import pytest

from spark.publicapi.explore_scheduler import ExploreScheduler, _spread
from spark.publicapi.explore_store import ExploreStore, FillsSyncState, ScanWriteback
from spark.publicapi.hl import HLGateway
from spark.publicapi.hl_budget import WeightLimiter
from spark.publicapi.hl_explore import ExploreConfig

PAGE_LIMIT = 2000


class Clock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class FakeHL:
    """`clearinghouse_state`／`portfolio`／`non_funding_ledger_updates`／`get_fills_page`
    的最小假實作，不經 HLGateway／WeightLimiter（那部分由 test_hl_gateway_budget.py
    與本檔的 weight-budget 測試分開覆蓋）。`get_fills_page` 預設回空頁——足以讓
    增量／遍歷軌任一輪立即收尾，不用逐一另建假物件。"""

    def __init__(self):
        self.calls: list[tuple] = []
        self.portfolio_exc: Exception | None = None

    def clearinghouse_state(self, address):
        self.calls.append(("state", address))
        return {"marginSummary": {"accountValue": "1"}}

    def portfolio(self, address):
        self.calls.append(("portfolio", address))
        if self.portfolio_exc is not None:
            raise self.portfolio_exc
        return [["day", {}]]

    def non_funding_ledger_updates(self, address, start_ms):
        self.calls.append(("ledger", address, start_ms))
        return [{"delta": {"type": "deposit"}}]

    def get_fills_page(self, address, start_ms, end_ms):
        self.calls.append(("fills", address, start_ms, end_ms))
        return []


def _payload(addresses: list[str]) -> dict:
    """`address` 依傳入順序視為 roi 降冪（roi 逐一遞減，避免 `candidate_addresses`
    內部排序改變測試預期的名次）。"""
    rows = []
    for i, addr in enumerate(addresses):
        rows.append({
            "ethAddress": addr, "displayName": f"trader{i}",
            "windowPerformances": [["month", {"roi": str(1.0 - i * 0.001)}]],
        })
    return {"leaderboardRows": rows}


def _sched(store, hl, *, leaderboard_source_fn=lambda: None, excluded_fn=lambda: set(),
          cfg=None, clock=None, on_dirty=lambda: None, **kw) -> ExploreScheduler:
    clock = clock or Clock()
    return ExploreScheduler(
        store=store, hl=hl, leaderboard_source_fn=leaderboard_source_fn,
        excluded_fn=excluded_fn, cfg=cfg or ExploreConfig(candidate_pool=300),
        now_fn=clock.now, sleep_fn=clock.sleep, on_dirty=on_dirty, **kw)


# --- 1. 首 tick 只建 candidates job；第二 tick 跑 candidates 後每個地址五個 job ---

def test_bootstrap_then_candidates_creates_five_jobs_per_address(tmp_path):
    """Task 7.9b：`_enqueue_address_jobs` 新增 `fills_scan`（遍歷軌初始
    scan）——每個地址現在有 5 種 job kind（`fills` 增量軌＋`fills_scan` 遍歷軌
    分開排程，見模組檔頭）。"""
    clock = Clock()
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    hl = FakeHL()
    payload = _payload(["0xAAA0000000000000000000000000000000AAA1",
                       "0xBBB0000000000000000000000000000000BBB2",
                       "0xCCC0000000000000000000000000000000CCC3"])
    sched = _sched(store, hl, leaderboard_source_fn=lambda: payload, clock=clock,
                  cfg=ExploreConfig(candidate_pool=3))

    r1 = sched.tick()
    assert r1 == "idle"
    assert store.stats()["refresh_job"] == 1  # 只有 candidates job

    r2 = sched.tick()
    assert r2 == "ran:candidates"

    rows = store._db.execute(
        "SELECT address, kind, next_attempt_at FROM refresh_job WHERE kind != 'candidates'"
    ).fetchall()
    by_addr: dict[str, set[str]] = {}
    times: set[float] = set()
    for addr, kind, next_at in rows:
        by_addr.setdefault(addr, set()).add(kind)
        times.add(next_at)
    assert len(by_addr) == 3
    for kinds in by_addr.values():
        assert kinds == {"state", "portfolio", "ledger", "fills", "fills_scan"}
    assert len(times) > 1  # 分散，不全等於 now

    # 每個地址同時建好增量軌（backfilling）與一個 running 的 initial scan。
    for addr in by_addr:
        sync = store.get_sync(addr)
        assert sync is not None and sync.completeness == "backfilling"
        scan = store.get_active_scan(addr)
        assert scan is not None and scan.kind == "initial"


# --- 2. 300 地址初次 state+portfolio（6,600 權重）：任一 60 秒切片 <= 300、完成 >= 22 分鐘 ---

def test_weight_budget_bounded_and_takes_at_least_22_minutes(tmp_path):
    clock = Clock()
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)

    def post(url, body):
        if body["type"] == "clearinghouseState":
            return {"marginSummary": {"accountValue": "1"}}
        if body["type"] == "portfolio":
            return [["day", {}]]
        raise AssertionError(f"unexpected type {body['type']}")

    lim = WeightLimiter(global_cap=900, scope_caps={"explore": 300},
                        now_fn=clock.now, sleep_fn=clock.sleep, rng=lambda: 0.0)
    hl = HLGateway("https://x", post_fn=post, sleep_fn=clock.sleep,
                  limiter=lim).scoped("explore", wait_s=0.0)

    addresses = [f"0x{'0' * 24}{(i * 2654435761) % (2**32):08x}" for i in range(300)]
    store.upsert_candidates([(addr, None, i + 1, None) for i, addr in enumerate(addresses)],
                            as_of=clock.now())
    for addr in addresses:
        store.enqueue(f"{addr}:state", addr, "state", 0, _spread(addr, 900))
        store.enqueue(f"{addr}:portfolio", addr, "portfolio", 1, _spread(addr, 3600))

    sched = _sched(store, hl, clock=clock,
                  # 週期設超大：完成後的重新排程不會在測試視窗內再度到期干擾計數。
                  state_every_s=10**9, portfolio_every_s=10**9,
                  ledger_every_s=10**9, fills_every_s=10**9, rng=lambda: 0.0)
    sched._bootstrapped = True  # 略過 candidates bootstrap，只驗證 state/portfolio 的節流

    events: list[tuple[float, int]] = []
    remaining = len(addresses) * 2
    ticks = 0
    while remaining > 0:
        ticks += 1
        assert ticks < 2_000_000, "測試迴圈超過安全上限，可能卡住"
        r = sched.tick()
        if r == "ran:state":
            events.append((clock.now(), 2))
            remaining -= 1
        elif r == "ran:portfolio":
            events.append((clock.now(), 20))
            remaining -= 1
        elif r in ("idle", "no_budget"):
            clock.t += 1.0
        else:
            pytest.fail(f"unexpected tick result {r!r}")

    assert clock.now() >= 22 * 60
    for t, _ in events:
        window_total = sum(w for (tt, w) in events if t - 60 < tt <= t)
        assert window_total <= 300


# --- 3. ScopePaused 期間 tick 回 paused 且零上游呼叫 ---

def test_scope_paused_returns_paused_with_zero_upstream_calls(tmp_path):
    clock = Clock()
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    calls: list[str] = []

    def post(url, body):
        calls.append(body["type"])
        return {"marginSummary": {"accountValue": "1"}}

    lim = WeightLimiter(global_cap=900, scope_caps={"explore": 300},
                        now_fn=clock.now, sleep_fn=clock.sleep, rng=lambda: 0.0)
    hl = HLGateway("https://x", post_fn=post, sleep_fn=clock.sleep,
                  limiter=lim).scoped("explore", wait_s=0.0)
    lim.note_429("interactive")  # 任何 scope 的 429 都會暫停 explore（spec §5.2）

    # Task 7.9d-S S2（2026-09-22 使用者裁決點 3）：非 active 地址的 job 在對帳時
    # 會被掃除、領到也會在發送前丟棄——這個測試要測的是限流暫停，所以地址必須
    # 真的在候選池內（生產上 job 就是 candidates 輪替每個候選建的）。
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store.enqueue("0xabc:state", "0xabc", "state", 0, clock.now())
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True

    r = sched.tick()
    assert r == "paused"
    assert calls == []


# --- 4. 遍歷軌多頁：三頁滿頁＋一短頁分 4 tick 完成，期間 priority 0 的 state job 有機會先跑 ---

def _fills_page(n: int, start_time_ms: int) -> list[dict]:
    return [{"coin": "BTC", "tid": start_time_ms + i, "time": start_time_ms + i}
           for i in range(n)]


class SequencedFillsHL:
    def __init__(self, pages: list[list[dict]]):
        self._pages = list(pages)
        self.calls: list[tuple] = []

    def get_fills_page(self, address, start_ms, end_ms):
        self.calls.append((address, start_ms, end_ms))
        return self._pages.pop(0)

    def clearinghouse_state(self, address):
        self.calls.append(("state", address))
        return {"marginSummary": {"accountValue": "1"}}

    def portfolio(self, address):
        self.calls.append(("portfolio", address))
        return [["day", {}]]

    def non_funding_ledger_updates(self, address, start_ms):
        self.calls.append(("ledger", address, start_ms))
        return []


def test_scan_multi_page_completes_and_state_also_gets_a_turn(tmp_path):
    """Task 7.9b：多頁回補現在走 `fills_scan`（遍歷軌）job，不是 `fills`
    （增量軌）——`ExploreStore.bootstrap_address_fills` 建好兩軌後，
    `fills_scan` job 逐頁推進，`fills_sync.completeness` 只在遍歷完成的那一刻
    透過 CAS 被寫入。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    window_start_ms = now_ms - 30 * 86_400_000

    page1 = _fills_page(PAGE_LIMIT, window_start_ms)
    cursor2 = page1[-1]["time"]
    page2 = _fills_page(PAGE_LIMIT, cursor2)
    cursor3 = page2[-1]["time"]
    page3 = _fills_page(PAGE_LIMIT, cursor3)
    cursor4 = page3[-1]["time"]
    page4 = _fills_page(500, cursor4)  # 短頁，觸發 done

    hl = SequencedFillsHL([page1, page2, page3, page4])
    store.upsert_candidates([("0xabc", None, 1, None), ("0xdef", None, 2, None)],
                            as_of=clock.now())
    store.bootstrap_address_fills("0xabc", clock.now(), window_start_ms=window_start_ms,
                                  window_end_ms=now_ms, params_fp="")
    store.enqueue("0xabc:fills_scan", "0xabc", "fills_scan", 2, clock.now())
    store.enqueue("0xdef:state", "0xdef", "state", 0, clock.now())

    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True

    results: list[str] = []
    for _ in range(20):
        r = sched.tick()
        results.append(r)
        sync = store.get_sync("0xabc")
        if sync is not None and sync.completeness == "complete":
            break

    assert results.count("ran:fills_scan") == 4
    assert results.count("ran:state") >= 1

    sync = store.get_sync("0xabc")
    assert sync.completeness == "complete"
    # 相鄰兩頁 inclusive 重疊 1 筆（上一頁最後一筆＝下一頁第一筆，同 tid），
    # 3 個頁界共去重 3 筆：2000*3+500-3。
    assert len(store.get_fills("0xabc", 0, cursor4 + 500)) == PAGE_LIMIT * 3 + 500 - 3


def test_fills_available_alternates_without_limiter_neither_side_starves(tmp_path):
    """裁決 2（2026-09-21）：`_fills_available()` 沒有 limiter 時不再恆回
    `FILLS_PAGE_WEIGHT`（那會讓有 fills-like 待處理的 tick 永遠選中它、反向
    餓死基礎類別）——改成每次呼叫交替 120/0。無 limiter、fills-like 與 state
    同時到期，4 個 tick 內兩類各至少領工 2 次。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    store.upsert_candidates(
        [("0xaaa", None, 1, None), ("0xbbb", None, 2, None), ("0xfff", None, 3, None)],
        as_of=clock.now())
    store.enqueue("0xaaa:state", "0xaaa", "state", 0, clock.now())
    store.enqueue("0xbbb:state", "0xbbb", "state", 0, clock.now())

    now_ms = int(clock.now() * 1000)
    window_start_ms = now_ms - 30 * 86_400_000
    store.bootstrap_address_fills("0xfff", clock.now(), window_start_ms=window_start_ms,
                                  window_end_ms=now_ms, params_fp="")
    store.enqueue("0xfff:fills_scan", "0xfff", "fills_scan", 2, clock.now())

    page1 = _fills_page(PAGE_LIMIT, window_start_ms)
    cursor2 = page1[-1]["time"]
    page2 = _fills_page(500, cursor2)  # 短頁，觸發 done

    hl = SequencedFillsHL([page1, page2])
    sched = _sched(store, hl, clock=clock)  # 不傳 hl_base/hl_fills：無 limiter
    sched._bootstrapped = True

    results = [sched.tick() for _ in range(4)]

    assert results.count("ran:state") >= 2
    assert results.count("ran:fills_scan") >= 2


# --- 5. 重啟：新 scheduler 接續 scan 游標，不從頭 ---

def test_restart_continues_scan_cursor_not_from_scratch(tmp_path):
    clock = Clock(t=40 * 86400.0)
    db_path = tmp_path / "explore.db"
    now_ms = int(clock.now() * 1000)
    window_start_ms = now_ms - 30 * 86_400_000

    store1 = ExploreStore(db_path, now_fn=clock.now)
    page1 = _fills_page(PAGE_LIMIT, window_start_ms)
    hl1 = SequencedFillsHL([page1])
    store1.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store1.bootstrap_address_fills("0xabc", clock.now(), window_start_ms=window_start_ms,
                                   window_end_ms=now_ms, params_fp="")
    store1.enqueue("0xabc:fills_scan", "0xabc", "fills_scan", 2, clock.now())
    sched1 = _sched(store1, hl1, clock=clock)
    sched1._bootstrapped = True

    r1 = sched1.tick()
    assert r1 == "ran:fills_scan"
    scan_before = store1.get_active_scan("0xabc")
    assert scan_before.pages_done == 1
    assert scan_before.status == "running"

    # 重啟：新 store／scheduler 指向同一個 DB 檔。
    store2 = ExploreStore(db_path, now_fn=clock.now)
    cursor = scan_before.cursor_ms
    page2 = _fills_page(500, cursor)  # 短頁，結束本輪
    hl2 = SequencedFillsHL([page2])
    sched2 = _sched(store2, hl2, clock=clock)
    sched2._bootstrapped = True

    r2 = sched2.tick()
    assert r2 == "ran:fills_scan"
    assert hl2.calls[0][1] == cursor  # 從上次游標續抓，不是從 window_start 重新開始
    sync_after = store2.get_sync("0xabc")
    assert sync_after.completeness == "complete"


# --- 6. 候選進出：移除的地址 active=0 但 cache/fills 保留；新地址只新增它的五個 job ---

def test_candidate_churn_preserves_removed_cache_and_adds_new_address_jobs(tmp_path):
    clock = Clock()
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)

    store.upsert_candidates([("0xaaa", "Alice", 1, 0.5), ("0xbbb", "Bob", 2, 0.4)],
                            as_of=clock.now())
    store.put_cache_ok("0xaaa", "clearinghouseState", {"x": 1}, fetched_at=clock.now(),
                       refresh_after=clock.now() + 900)
    # Task 7.9c-D D6：`inc_from_ms` 的非空由**寫入邊界**保證（`insert_fills_page`
    # 對 `None` 拋 `ValueError`），測試的假狀態也必須帶上它。
    store.insert_fills_page("0xaaa", [{"coin": "BTC", "tid": 1, "time": 100}],
                            FillsSyncState(
                                address="0xaaa", window_start_ms=0, window_end_ms=1000,
                                cursor_ms=1000, synced_through_ms=1000, observed_from_ms=100,
                                observed_to_ms=100, completeness="complete", reason=None,
                                pages_done=1, fills_in_window=1, updated_at=clock.now(),
                                last_error=None, inc_from_ms=0))
    far_future = clock.now() + 10**8
    for addr in ("0xaaa", "0xbbb"):
        for kind, prio in (("state", 0), ("portfolio", 1), ("ledger", 1), ("fills", 2)):
            store.enqueue(f"{addr}:{kind}", addr, kind, prio, far_future)

    payload2 = {"leaderboardRows": [
        {"ethAddress": "0xBBB", "displayName": "Bob",
         "windowPerformances": [["month", {"roi": "0.4"}]]},
        {"ethAddress": "0xCCC", "displayName": "Carl",
         "windowPerformances": [["month", {"roi": "0.3"}]]},
    ]}
    hl = FakeHL()
    sched = _sched(store, hl, leaderboard_source_fn=lambda: payload2, clock=clock,
                  cfg=ExploreConfig(candidate_pool=5))
    sched._bootstrapped = True
    store.enqueue("candidates:candidates", None, "candidates", 0, clock.now())

    r = sched.tick()
    assert r == "ran:candidates"

    rows = dict(store._db.execute("SELECT address, active FROM candidate").fetchall())
    assert rows == {"0xaaa": 0, "0xbbb": 1, "0xccc": 1}

    # 0xaaa 被踢出，但快取與 fills 都還在。
    entry = store.get_cache("0xaaa", "clearinghouseState")
    assert entry is not None and entry.payload == {"x": 1}
    assert len(store.get_fills("0xaaa", 0, 1000)) == 1

    # 0xccc 是新地址，只新增它的五個 job。
    ccc_kinds = {k for (k,) in store._db.execute(
        "SELECT kind FROM refresh_job WHERE address='0xccc'").fetchall()}
    assert ccc_kinds == {"state", "portfolio", "ledger", "fills", "fills_scan"}


# --- 7. portfolio 拋例外 → quarantined，cache 落地 last_error、payload 仍 NULL ---

def test_portfolio_exception_quarantines_job(tmp_path):
    clock = Clock()
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    hl = FakeHL()
    hl.portfolio_exc = RuntimeError("bad shape")
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())  # 7.9d-S S2
    store.enqueue("0xabc:portfolio", "0xabc", "portfolio", 1, clock.now())
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True

    r = sched.tick()
    assert r == "quarantined"

    entry = store.get_cache("0xabc", "portfolio")
    assert entry is not None
    assert entry.payload is None
    assert "bad shape" in entry.last_error

    row = store._db.execute(
        "SELECT next_attempt_at FROM refresh_job WHERE key='0xabc:portfolio'").fetchone()
    assert row[0] == pytest.approx(clock.now() + 86400)


# --- 8. run_forever 內 tick 拋任意例外不會結束 thread ---

class ExplodingStore(ExploreStore):
    def __init__(self, *a, boom_times: int = 3, **kw):
        super().__init__(*a, **kw)
        self._boom_left = boom_times

    def claim_due(self, *a, **kw):
        if self._boom_left > 0:
            self._boom_left -= 1
            raise RuntimeError("boom")
        return super().claim_due(*a, **kw)


# --- Task 3.5 B：job 生命週期／緊迴圈／準入 COUNT／ScopePaused 不計 attempts ---

def test_candidate_churn_deletes_dropped_addresses_jobs(tmp_path):
    """B(1)：候選退池後，它的（state/portfolio/ledger/fills）job 全部被刪除
    （C1 修法——不刪會永久洩漏預算並卡死準入與 purge）。"""
    clock = Clock()
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    store.upsert_candidates([("0xaaa", "Alice", 1, 0.5), ("0xbbb", "Bob", 2, 0.4)],
                            as_of=clock.now())
    far_future = clock.now() + 10**8
    for addr in ("0xaaa", "0xbbb"):
        for kind, prio in (("state", 0), ("portfolio", 1), ("ledger", 1), ("fills", 2)):
            store.enqueue(f"{addr}:{kind}", addr, kind, prio, far_future)

    payload = {"leaderboardRows": [
        {"ethAddress": "0xBBB", "displayName": "Bob",
         "windowPerformances": [["month", {"roi": "0.4"}]]},
    ]}
    sched = _sched(store, FakeHL(), leaderboard_source_fn=lambda: payload, clock=clock,
                  cfg=ExploreConfig(candidate_pool=5))
    sched._bootstrapped = True
    store.enqueue("candidates:candidates", None, "candidates", 0, clock.now())

    r = sched.tick()
    assert r == "ran:candidates"

    assert store._db.execute(
        "SELECT COUNT(*) FROM refresh_job WHERE address='0xaaa'").fetchone()[0] == 0
    # 0xbbb 仍在池內，它原本手動塞的四個 job 仍在，且 candidates job 重新
    # `_enqueue_address_jobs` 補上第五種（`fills_scan`，Task 7.9b 新增）。
    assert store._db.execute(
        "SELECT COUNT(*) FROM refresh_job WHERE address='0xbbb'").fetchone()[0] == 5
    # 退池地址的 job 洩漏被清除後，purge 才能真正刪掉候選（C1 的下游效果）。
    store.deactivate_missing({"0xbbb"})  # 已由 candidates job 完成，這裡僅確認 is_active
    assert store.is_active("0xaaa") is False


def test_dropped_address_job_does_not_reenqueue_itself(tmp_path):
    """B(2)：`_run_cache_kind` 續排前查 `is_active`——候選已退池 → 回 `"dropped"`，
    不再自我續排（否則預算永遠漏給非候選）。"""
    clock = Clock()
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    store.upsert_candidates([("0xabc", "Alice", 1, 0.5)], as_of=clock.now())
    store.deactivate_missing(set())  # 0xabc 退池
    store.enqueue("0xabc:state", "0xabc", "state", 0, clock.now())
    sched = _sched(store, FakeHL(), clock=clock)
    sched._bootstrapped = True
    # Task 7.9d-S S2：首 tick 的對帳會**主動掃除**退池地址的殘留 job（另見
    # `test_s2_reconcile_sweeps_inactive_jobs`）——這裡要測的是「已經領到手上」
    # 那一刻的閘門，所以跳過首 tick 的對帳。
    sched._first_tick_done = True

    r = sched.tick()
    assert r == "dropped"
    assert store._db.execute(
        "SELECT COUNT(*) FROM refresh_job WHERE key='0xabc:state'").fetchone()[0] == 0


def test_dropped_address_fills_job_does_not_reenqueue(tmp_path):
    """同上，`_run_increment` 版本（`plan.is_noop` 分支）——增量軌不因
    `completeness` 為何而拒絕執行，`is_active` 才是唯一的續排判準。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    store.upsert_candidates([("0xabc", "Alice", 1, 0.5)], as_of=clock.now())
    now_ms = int(clock.now() * 1000)
    window_start_ms = now_ms - 30 * 86_400_000
    store.insert_fills_page("0xabc", [], FillsSyncState(
        address="0xabc", window_start_ms=window_start_ms, window_end_ms=now_ms,
        cursor_ms=now_ms, synced_through_ms=now_ms, observed_from_ms=None,
        observed_to_ms=None, completeness="complete", reason=None,
        pages_done=1, fills_in_window=0, updated_at=clock.now(), last_error=None,
        inc_from_ms=window_start_ms))  # 7.9c-D D6：寫入邊界要求非 NULL
    store.deactivate_missing(set())  # 0xabc 退池
    store.enqueue("0xabc:fills", "0xabc", "fills", 2, clock.now())
    hl = SequencedFillsHL([])
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True
    sched._first_tick_done = True   # 見上一個測試：這裡測領到手上那一刻的閘門

    r = sched.tick()
    assert r == "dropped"
    # Task 7.9d-S S2：發送前就丟棄——退池地址一頁都不抓。
    assert hl.calls == []
    assert store._db.execute(
        "SELECT COUNT(*) FROM refresh_job WHERE key='0xabc:fills'").fetchone()[0] == 0


def test_run_forever_sleeps_after_unexpected_exception(tmp_path):
    """B(3)：`run_forever` 的例外路徑也要 `self._sleep(1.0)`，否則是緊迴圈
    （W1/W2 修法）。用一個獨立於 `sleep_fn` 的 tick 計數器判定何時停止，
    確保無論本項有沒有修好都會在有限步數內結束（不會真的卡死整個測試）：
    改好後每一次 tick（含例外）都呼叫一次 `sleep_fn`，`len(sleeps) == ticks`；
    沒修好時前三次例外 tick 不呼叫 `sleep_fn`，兩者會不相等。"""
    clock = Clock()
    store = ExplodingStore(tmp_path / "explore.db", now_fn=clock.now, boom_times=3)
    orig_claim = store.claim_due
    ticks = {"n": 0}

    def counting_claim(*a, **kw):
        ticks["n"] += 1
        return orig_claim(*a, **kw)
    store.claim_due = counting_claim

    stop = threading.Event()
    sleeps: list[float] = []

    def sleep_fn(s):
        sleeps.append(s)
        if ticks["n"] >= 5:
            stop.set()

    sched = ExploreScheduler(
        store=store, hl=FakeHL(), leaderboard_source_fn=lambda: None,
        excluded_fn=lambda: set(), cfg=ExploreConfig(), now_fn=clock.now,
        sleep_fn=sleep_fn, on_dirty=lambda: None)

    thread = threading.Thread(target=sched.run_forever, args=(stop,))
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    # +1：bootstrap 那一次 tick 從不呼叫 claim_due（因此不計入 ticks），但它走的是
    # 正常完成路徑，一定會呼叫一次 sleep_fn——這是唯一的固定位移。
    assert len(sleeps) == ticks["n"] + 1


def test_scope_paused_does_not_bump_attempts(tmp_path):
    """B(4)：`ScopePaused` 重排不計 attempts（S1 裁決：暫停不是失敗）。"""
    clock = Clock()
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    lim = WeightLimiter(global_cap=900, scope_caps={"explore": 300},
                        now_fn=clock.now, sleep_fn=clock.sleep, rng=lambda: 0.0)
    hl = HLGateway("https://x", post_fn=lambda url, body: {}, sleep_fn=clock.sleep,
                  limiter=lim).scoped("explore", wait_s=0.0)
    lim.note_429("interactive")

    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())  # 7.9d-S S2
    store.enqueue("0xabc:state", "0xabc", "state", 0, clock.now())
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True

    r = sched.tick()
    assert r == "paused"
    row = store._db.execute(
        "SELECT attempts FROM refresh_job WHERE key='0xabc:state'").fetchone()
    assert row[0] == 0


def test_candidates_every_s_default_is_1800(tmp_path):
    """B(5)：`candidates_every_s` 預設 1800（stats-data 36MB 每 30 分鐘至多一次）。"""
    clock = Clock()
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    sched = _sched(store, FakeHL(), clock=clock)
    assert sched._candidates_every_s == 1800


def test_fills_every_s_default_is_default_fills_period_s_constant(tmp_path):
    """Task 7.9a 補（2026-09-21 主線程裁決）：`ExploreScheduler` 不傳
    `fills_every_s` 時，走的是 `explore_fills_sync.DEFAULT_FILLS_PERIOD_S`
    這個單一來源常數（21600＝6 小時），不是排程端另外寫死的字面值。"""
    from spark.publicapi.explore_fills_sync import DEFAULT_FILLS_PERIOD_S

    clock = Clock()
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    sched = _sched(store, FakeHL(), clock=clock)
    assert sched._fills_every_s == DEFAULT_FILLS_PERIOD_S


def test_admission_cap_uses_admission_counts_not_stale_len_seen(tmp_path):
    """B(6)：準入計數改用 `admission_counts()`（同一 lock 內兩個 COUNT，與
    `active_n = len(seen)` 語意等價但走共用口徑）。

    Task 7.9c-S S3：準入改為逐項——cap 撐爆時**既有地址**的補建被擋（每擋一個
    計一次 `admission_skipped`），新候選不受限（另見
    `test_s7e_admission_skips_single_job_for_existing_address_new_candidate_exempt`）。
    這裡的地址刻意先 `bootstrap_address_fills` 成既有地址，才是被 cap 擋的那一類。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    # 預先塞大量非候選 job，撐爆準入上限（ADMISSION_MULTIPLIER * active_n）。
    for i in range(100):
        store.enqueue(f"dummy:{i}", None, "dummy", 5, clock.now())

    addr = "0xAAA0000000000000000000000000000000AAA1"
    now_ms = int(clock.now() * 1000)
    store.bootstrap_address_fills(addr, clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                  window_end_ms=now_ms, params_fp="")  # 既有地址
    payload = _payload([addr])
    sched = _sched(store, FakeHL(), leaderboard_source_fn=lambda: payload, clock=clock,
                  cfg=ExploreConfig(candidate_pool=1))
    sched._bootstrapped = True
    store.enqueue("candidates:candidates", None, "candidates", 0, clock.now())

    r = sched.tick()
    assert r == "ran:candidates"
    # 準入上限被撐爆（101 job > 7*1 active）→ 本輪不新增這個既有地址的 job。
    assert sched.status()["admission_skipped"] == 4  # state/portfolio/ledger/fills
    # Task 7.9d-S S1：唯一的例外是對帳補的 `fills_scan`——`bootstrap_address_fills`
    # 留下一個 running 的 initial scan 卻沒有任何 job 會推進它（7.9c 的 Critical），
    # 這條修復路徑不受 cap 限制（每個 active 地址最多一個，結構上有界）。
    assert store.job_kinds(addr) == {"fills_scan"}
    assert sched.status()["reconciled"]["resume_running"] == 1


def test_run_candidates_empty_rows_keeps_existing_pool_active(tmp_path):
    """B(1)（Critical C1 修法）：候選來源整批回空 rows（例如上游壞掉、payload
    格式跑掉）不能當成「所有人退池」——不呼叫 `upsert_candidates`／
    `deactivate_missing`，既有候選仍 active、`refresh_job` 數不變，
    tick 回 `retry`。"""
    clock = Clock()
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    addresses = ["0xAAA0000000000000000000000000000000AAA1",
                "0xBBB0000000000000000000000000000000BBB2",
                "0xCCC0000000000000000000000000000000CCC3"]
    store.upsert_candidates([(a, None, i + 1, None) for i, a in enumerate(addresses)],
                            as_of=clock.now())
    for a in addresses:
        for kind, prio in (("state", 0), ("portfolio", 1), ("ledger", 1), ("fills", 2)):
            store.enqueue(f"{a}:{kind}", a, kind, prio, clock.now() + 10**6)

    sched = _sched(store, FakeHL(), leaderboard_source_fn=lambda: {"leaderboardRows": []},
                  clock=clock, cfg=ExploreConfig(candidate_pool=300))
    sched._bootstrapped = True
    store.enqueue("candidates:candidates", None, "candidates", 0, clock.now())
    jobs_before = store.stats()["refresh_job"]

    r = sched.tick()

    assert r == "retry"
    for a in addresses:
        assert store.is_active(a.lower()) is True
    assert store.stats()["refresh_job"] == jobs_before


def test_run_candidates_empty_rows_tracks_streak_and_resets_on_success(tmp_path):
    """Task 3.7 B（W1 修法）：候選來源長期回空 rows 之前無外部證據——
    連續空 rows 要能從 `status()` 讀到 `candidates_empty_streak` 遞增、
    `last_candidates_ok_at` 維持 `None`；成功一次後歸零並記錄時間。"""
    clock = Clock()
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    empty_payload = {"leaderboardRows": []}
    sched = _sched(store, FakeHL(), leaderboard_source_fn=lambda: empty_payload,
                  clock=clock, cfg=ExploreConfig(candidate_pool=300))
    sched._bootstrapped = True
    store.enqueue("candidates:candidates", None, "candidates", 0, clock.now())

    for _ in range(3):
        assert sched.tick() == "retry"
        store.enqueue("candidates:candidates", None, "candidates", 0, clock.now())

    status = sched.status()
    assert status["candidates_empty_streak"] == 3
    assert status["last_candidates_ok_at"] is None

    ok_payload = _payload(["0xAAA0000000000000000000000000000000AAA1"])
    sched._leaderboard_source_fn = lambda: ok_payload
    assert sched.tick() == "ran:candidates"

    status = sched.status()
    assert status["candidates_empty_streak"] == 0
    assert status["last_candidates_ok_at"] == clock.now()


def test_scan_non_candidate_sends_nothing_and_job_is_dropped(tmp_path):
    """Task 7.9d-S S2（2026-09-22 使用者裁決點 3，**取代**舊的
    `test_scan_non_candidate_continues_paging_until_done_then_dropped`）：非
    active 地址（退池，或從未 `upsert_candidates`）的遍歷 job 在**發送前**就被
    丟棄——一頁都不抓、續頁也不抓，計 `inactive_job_dropped`。

    舊契約（7.9b W1）是「多頁遍歷不因中途 `is_active` 檢查提早中斷」；7.9d 的
    裁決把它反轉：inactive 地址不得再發出任何新請求（含續頁）。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    window_start_ms = now_ms - 30 * 86_400_000

    hl = SequencedFillsHL([_fills_page(PAGE_LIMIT, window_start_ms)])
    store.bootstrap_address_fills("0xabc", clock.now(), window_start_ms=window_start_ms,
                                  window_end_ms=now_ms, params_fp="")
    store.enqueue("0xabc:fills_scan", "0xabc", "fills_scan", 2, clock.now())
    # 刻意不 upsert_candidates("0xabc")：非候選地址。

    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True
    sched._first_tick_done = True   # 首 tick 的對帳會直接掃除這個 job（另有測試）

    assert sched.tick() == "dropped"

    assert hl.calls == []                                   # 零上游呼叫
    assert sched.status()["inactive_job_dropped"] == 1
    assert store._db.execute(
        "SELECT COUNT(*) FROM refresh_job WHERE key='0xabc:fills_scan'").fetchone()[0] == 0


# --- Task 7.4b: 週期放寬、重排 overdue、類別感知領工、等待加權（飢餓重現） ---

def test_scheduler_default_periods_widened(tmp_path):
    clock = Clock()
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    sched = _sched(store, FakeHL(), clock=clock)
    assert sched._state_every_s == 1800
    assert sched._portfolio_every_s == 7200
    assert sched._ledger_every_s == 7200


def test_first_tick_rebalances_overdue_state_jobs_and_reports_status(tmp_path):
    """Task 7.4b「重排」：啟動時 100 個嚴重逾期（超過新週期 1800s）的 state job，
    第一個 tick 後全部被攤到 `(now, now+1800]` 內、不全部相同，`status()["rebalanced"]`
    揭露筆數。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    addresses = [f"0x{'0' * 24}{(i * 2654435761) % (2**32):08x}" for i in range(100)]
    store.upsert_candidates([(a, None, i + 1, None) for i, a in enumerate(addresses)],
                            as_of=clock.now())
    for a in addresses:
        store.enqueue(f"{a}:state", a, "state", 0, clock.now() - 4000.0)

    sched = _sched(store, FakeHL(), clock=clock, rng=lambda: 0.0)
    sched._bootstrapped = True  # 略過 candidates bootstrap，直接驗證第一個 tick 的重排

    sched.tick()

    rows = store._db.execute(
        "SELECT next_attempt_at FROM refresh_job WHERE kind='state'").fetchall()
    assert len(rows) == 100
    times = {r[0] for r in rows}
    for (t,) in rows:
        assert clock.now() <= t <= clock.now() + 1800.0
    assert len(times) > 1  # 攤平後不再全部相同

    status = sched.status()
    assert status["rebalanced"]["state"] == 100

    # 第二次 tick 不再重排（只做一次）。
    store.enqueue("0xzzz:state", "0xzzz", "state", 0, clock.now() - 4000.0)
    sched.tick()
    status2 = sched.status()
    assert status2["rebalanced"]["state"] == 100  # 沒有再把新塞進去的那筆算進來


def test_no_fills_pending_base_jobs_use_parent_scope_up_to_300(tmp_path):
    """無 fills-like 待處理時，基礎類別（此測試用 portfolio）走父 scope
    `explore`，可用到超過 `explore_base`(180) 的上限。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    addresses = [f"0x{'0' * 24}{(i * 2654435761) % (2**32):08x}" for i in range(30)]
    store.upsert_candidates([(a, None, i + 1, None) for i, a in enumerate(addresses)],
                            as_of=clock.now())
    for a in addresses:
        store.enqueue(f"{a}:portfolio", a, "portfolio", 1, clock.now())
    # 刻意不排任何 fills／fills_scan job：due_count 永遠是 0。

    def post(url, body):
        assert body["type"] == "portfolio"
        return [["day", {}]]

    lim = WeightLimiter(global_cap=900,
                        scope_caps={"explore": 300, "explore_base": 180, "explore_fills": 120},
                        scope_parents={"explore_base": "explore", "explore_fills": "explore"},
                        now_fn=clock.now, sleep_fn=clock.sleep, rng=lambda: 0.0)
    gw = HLGateway("https://x", post_fn=post, sleep_fn=clock.sleep, limiter=lim)
    hl = gw.scoped("explore", wait_s=0.0)
    hl_base = gw.scoped("explore_base", wait_s=0.0)
    hl_fills = gw.scoped("explore_fills", wait_s=0.0)

    sched = _sched(store, hl, hl_base=hl_base, hl_fills=hl_fills, clock=clock,
                  state_every_s=10**9, portfolio_every_s=10**9, ledger_every_s=10**9,
                  fills_every_s=10**9, rng=lambda: 0.0)
    sched._bootstrapped = True

    max_explore_used = 0
    for _ in range(200):
        r = sched.tick()
        if r in ("idle", "no_budget"):
            break
        snap = lim.snapshot()
        max_explore_used = max(max_explore_used, snap["used"].get("explore", 0))
        assert snap["used"].get("explore_base", 0) == 0

    assert max_explore_used >= 240  # 遠超 explore_base 的 180 上限
    assert sched.status()["base_scope_in_use"] == "explore"


class _FillsPager:
    """依實際請求的 `start_ms` 動態產生分頁內容（避免預先算好的頁內容因為
    測試迴圈的虛擬時間不對齊而落在 plan 的 `[start_ms, end_ms]` 之外）：
    每個地址前 3 頁滿頁（`PAGE_LIMIT`），第 4 頁短頁（500 筆）觸發 `done`。"""

    def __init__(self):
        self._page_idx: dict[str, int] = {}
        self.state_calls = 0
        self.fills_calls = 0

    def next_fills_page(self, address: str, start_ms: int, end_ms: int) -> list[dict]:
        self.fills_calls += 1
        idx = self._page_idx.get(address, 0)
        self._page_idx[address] = idx + 1
        n = PAGE_LIMIT if idx < 3 else 500
        return [{"coin": "BTC", "tid": start_ms + i, "time": start_ms + i} for i in range(n)]


def test_scan_starvation_reproduction_bounded_and_progressing(tmp_path):
    """使用者第 4 點的飢餓重現（改用 `fills_scan`，遍歷軌是現在真正會連續
    多頁抓取的 kind）：300 地址、`state_every_s=1`（基礎永遠有積壓）、fake HL
    全部成功、遍歷每地址 3 滿頁＋1 短頁；驅動 fake clock 30 分鐘 → 遍歷頁數
    >= 25、state 抓取在前後 15 分鐘都持續發生、任一 60 秒切片：explore 合計
    <=300 且（fills-like 全程待處理）base <=180。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    addresses = [f"0x{'0' * 24}{(i * 2654435761) % (2**32):08x}" for i in range(300)]
    store.upsert_candidates([(a, None, i + 1, None) for i, a in enumerate(addresses)],
                            as_of=clock.now())
    now_ms = int(clock.now() * 1000)
    window_start_ms = now_ms - 30 * 86_400_000
    for a in addresses:
        store.enqueue(f"{a}:state", a, "state", 0, clock.now())
        store.bootstrap_address_fills(a, clock.now(), window_start_ms=window_start_ms,
                                      window_end_ms=now_ms, params_fp="")
        store.enqueue(f"{a}:fills_scan", a, "fills_scan", 2, clock.now())
    # 刻意不排 portfolio/ledger job（本次重現聚焦 state vs fills-like 的類別級
    # 飢餓；portfolio/ledger 已由 Task 3.1 的權重預算測試覆蓋）。

    pager = _FillsPager()

    def post(url, body):
        t = body["type"]
        if t == "clearinghouseState":
            pager.state_calls += 1
            return {"marginSummary": {"accountValue": "1"}}
        if t == "userFillsByTime":
            return pager.next_fills_page(body["user"], body["startTime"], body["endTime"])
        raise AssertionError(f"unexpected type {t}")

    lim = WeightLimiter(global_cap=900,
                        scope_caps={"explore": 300, "explore_base": 180, "explore_fills": 120},
                        scope_parents={"explore_base": "explore", "explore_fills": "explore"},
                        now_fn=clock.now, sleep_fn=clock.sleep, rng=lambda: 0.0)
    gw = HLGateway("https://x", post_fn=post, sleep_fn=clock.sleep, limiter=lim)
    hl = gw.scoped("explore", wait_s=0.0)
    hl_base = gw.scoped("explore_base", wait_s=0.0)
    hl_fills = gw.scoped("explore_fills", wait_s=0.0)

    sched = _sched(store, hl, hl_base=hl_base, hl_fills=hl_fills, clock=clock,
                  state_every_s=1, portfolio_every_s=10**9, ledger_every_s=10**9,
                  fills_every_s=10**9, rng=lambda: 0.0)
    sched._bootstrapped = True

    start_time = clock.now()
    end_time = start_time + 1800.0
    state_times: list[float] = []
    ticks = 0
    while clock.now() < end_time:
        ticks += 1
        assert ticks < 5_000_000, "測試迴圈超過安全上限，可能卡住"
        r = sched.tick()
        if r == "ran:state":
            state_times.append(clock.now() - start_time)
        if r in ("idle", "no_budget"):
            clock.t += 1.0
        snap = lim.snapshot()
        assert snap["used"].get("explore", 0) <= 300
        assert snap["used"].get("explore_base", 0) <= 180

    assert pager.fills_calls >= 25
    assert any(t < 900.0 for t in state_times), "前 15 分鐘應有 state 抓取"
    assert any(t >= 900.0 for t in state_times), "後 15 分鐘應持續有 state 抓取"

    status = sched.status()
    assert status["fills_pages_total"] <= pager.fills_calls
    assert status["last_fills_at"] is not None
    assert status["base_scope_in_use"] == "explore_base"  # 全程 fills-like 都待處理


def test_run_forever_survives_tick_exceptions(tmp_path):
    clock = Clock()
    store = ExplodingStore(tmp_path / "explore.db", now_fn=clock.now, boom_times=3)
    stop = threading.Event()
    tick_count = {"n": 0}

    def sleep_fn(s):
        tick_count["n"] += 1
        if tick_count["n"] >= 6:
            stop.set()

    sched = ExploreScheduler(
        store=store, hl=FakeHL(), leaderboard_source_fn=lambda: None,
        excluded_fn=lambda: set(), cfg=ExploreConfig(), now_fn=clock.now,
        sleep_fn=sleep_fn, on_dirty=lambda: None)

    thread = threading.Thread(target=sched.run_forever, args=(stop,))
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert store._boom_left == 0
    assert tick_count["n"] >= 6


def test_budget_exhausted_keeps_original_due_time_so_wait_keeps_accumulating(tmp_path):
    """2026-09-21 複審 W3：`BudgetExhausted` 退避不得把 `next_attempt_at` 推到未來
    （那會把等待加權歸零）。"""
    from spark.publicapi.hl_budget import BudgetExhausted

    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    addr = "0x" + "ab" * 20
    store.upsert_candidates([(addr, None, 1, None)], as_of=clock.now())
    due_at = clock.now() - 1200.0
    store.enqueue(f"{addr}:state", addr, "state", 0, due_at)

    class ExhaustedHL:
        def clearinghouse_state(self, address):
            raise BudgetExhausted("hl budget exhausted for scope=explore_base weight=2")

    sched = ExploreScheduler(store=store, hl=ExhaustedHL(), leaderboard_source_fn=lambda: None,
                             excluded_fn=set, cfg=ExploreConfig(), now_fn=clock.now,
                             sleep_fn=clock.sleep, on_dirty=lambda: None)
    sched.tick()                      # bootstrap
    clock.t += 1.0
    assert sched.tick() == "no_budget"
    row = store._db.execute("select next_attempt_at, lease_until from refresh_job where key=?",
                            (f"{addr}:state",)).fetchone()
    assert row[0] == due_at           # 原到期時間保留
    assert row[1] is None or row[1] < clock.now()   # lease 已釋放


# ============================================================
# Task 7.9b B7：遍歷軌／增量軌分離的行為級驗收 (i)-(viii)
# ============================================================

def _complete_scan(store, addr, *, result="complete",
                   reason="count_below_retention_threshold", cursor_ms=None,
                   window_end_ms=None, finished_at=0.0):
    import dataclasses
    scan = store.get_active_scan(addr)
    scan = dataclasses.replace(
        scan, cursor_ms=cursor_ms if cursor_ms is not None else scan.window_end_ms,
        window_end_ms=window_end_ms if window_end_ms is not None else scan.window_end_ms,
        result=result, reason=reason, finished_at=finished_at)
    store.complete_scan(addr, [], scan)
    return scan


def test_b7_i_incremental_continues_while_rescan_in_progress(tmp_path):
    """(i) 重掃期間增量持續前進：partial 地址開 `partial_rescan` 多頁中途，
    增量到期 → 增量頁照抓、`synced_through` 前進、scan 游標不受影響。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store.bootstrap_address_fills("0xabc", clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                  window_end_ms=now_ms, params_fp="")
    # 首次遍歷判為 partial（模擬既有歷史），接著開一個 partial_rescan 並停在
    # 多頁中途（未完成）。
    _complete_scan(store, "0xabc", result="partial", reason="retention_limit",
                  window_end_ms=now_ms)
    rescan_start, rescan_end = now_ms, now_ms + 1000
    scan = store.create_scan("0xabc", kind="partial_rescan", window_start_ms=rescan_start,
                             window_end_ms=rescan_end, cursor_ms=rescan_start + 500,
                             started_at=clock.now())
    sync_before = store.get_sync("0xabc")

    # 增量到期，跑增量 job：不看 scan 是否進行中，照常抓一頁並前進。
    store.enqueue("0xabc:fills", "0xabc", "fills", 2, clock.now())
    hl = SequencedFillsHL([[]])  # 空頁，立刻結束增量本輪
    sched = _sched(store, hl, clock=clock, fills_every_s=1)
    sched._bootstrapped = True
    r = sched.tick()

    assert r == "ran:fills"
    sync_after = store.get_sync("0xabc")
    assert sync_after.synced_through_ms is not None
    assert sync_after.synced_through_ms >= sync_before.synced_through_ms
    # scan 游標完全不受影響。
    scan_after = store.get_active_scan("0xabc")
    assert scan_after.scan_id == scan.scan_id
    assert scan_after.cursor_ms == rescan_start + 500


def test_b7_ii_stale_probe_writeback_does_not_overwrite_new_scan(tmp_path):
    """(ii) 舊探測回應不覆蓋新遍歷：探測發出後、回寫前，該地址完成新 scan
    （新 `scan_id`）→ 回寫 rowcount 0、`probe.stale == 1`，新 scan 的 reason
    不變。用一個會在「探測抓頁」呼叫當下、順便完成一次新遍歷的假 HL 模擬
    race（單執行緒下唯一能重現「探測發出後、回寫前」這個時間點的方式）。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store.bootstrap_address_fills("0xabc", clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                  window_end_ms=now_ms, params_fp="")
    old_scan = _complete_scan(store, "0xabc", result="complete",
                              reason="count_below_retention_threshold", window_end_ms=now_ms,
                              finished_at=1.0)

    class RaceHL:
        """`get_fills_page` 第一次呼叫（探測）時，順便完成一次新的遍歷——
        模擬「探測發出後、回寫前，該地址完成新 scan」。"""

        def __init__(self, store, addr):
            self._store = store
            self._addr = addr
            self.calls = 0

        def get_fills_page(self, address, start_ms, end_ms):
            self.calls += 1
            # 7.9c-D D3：`complete_scan` 用 `started_at` 判斷新舊（指向
            # `started_at` 更晚的 scan 才算 STALE）——這裡的「新遍歷」必須真的
            # 比舊 scan 晚開始，否則它自己會被判成過期而不寫回。
            new_scan = self._store.create_scan(
                self._addr, kind="partial_rescan", window_start_ms=0, window_end_ms=999,
                cursor_ms=0, started_at=self._store._now() + 2.0)
            import dataclasses
            finished = dataclasses.replace(new_scan, cursor_ms=999, result="partial",
                                           reason="retention_limit",
                                           finished_at=self._store._now() + 3.0)
            self._store.complete_scan(self._addr, [], finished)
            return []

    hl = RaceHL(store, "0xabc")
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True

    sched._run_probe((old_scan.address, old_scan.scan_id), clock.now())

    assert sched.status()["probe"]["stale"] == 1
    # 新遍歷的結論不被覆蓋。
    assert store.get_sync("0xabc").reason == "retention_limit"
    assert store.get_scan(old_scan.scan_id).reason == "count_below_retention_threshold"


class _ContinuingFillsHL:
    """`get_fills_page` 恆回滿頁（`PAGE_LIMIT` 筆，時間嚴格遞增、落在
    `[start_ms, end_ms]` 內）——供「持續積壓」測試用：只要呼叫端的
    `window_end_ms` 夠大，滿頁會讓 `apply_incremental_page`／`apply_scan_page`
    的 `done` 恆為 `False`，job 每次都以 `_reschedule(job, now, ...)` 立刻續排，
    造就穩定不消退的積壓（不依賴 period／clock 推進，見呼叫端 docstring）。"""

    def get_fills_page(self, address, start_ms, end_ms):
        n = min(PAGE_LIMIT, max(1, end_ms - start_ms))
        return [{"coin": "BTC", "tid": start_ms + i, "time": start_ms + i} for i in range(n)]


def test_b7_iii_fills_and_probe_backlog_9_to_1_ratio(tmp_path):
    """(iii) fills-like／probe 同時積壓：10 個持續積壓的增量 job＋10 個探測
    候選，30 tick 後 probe 次數 <= 3 且 >= 1，fills >= 27。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    addrs = [f"0x{'0' * 24}{i:016x}" for i in range(10)]
    store.upsert_candidates([(a, None, i + 1, None) for i, a in enumerate(addrs)],
                            as_of=clock.now())
    for a in addrs:
        store.bootstrap_address_fills(a, clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                      window_end_ms=now_ms, params_fp="")
        _complete_scan(store, a, result="complete", reason="count_below_retention_threshold",
                       window_end_ms=now_ms, finished_at=float(hash(a) % 1000))
        # 直接把增量軌狀態改成「輪進行中」（`cursor_ms > synced_through_ms`）、
        # `window_end_ms` 設得很大——`_ContinuingFillsHL` 每次都回滿頁，這個
        # 輪永遠不會結束，job 每次都立刻續排（見上方 HL docstring），造就穩定
        # 不依賴時鐘推進的積壓。
        sync = store.get_sync(a)
        import dataclasses as _dc
        big_end = now_ms + 10**9
        continuing = _dc.replace(sync, cursor_ms=now_ms + 1, window_end_ms=big_end)
        store.insert_fills_page(a, [], continuing)
        store.enqueue(f"{a}:fills", a, "fills", 2, clock.now())

    hl = _ContinuingFillsHL()
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True

    results = [sched.tick() for _ in range(30)]
    probe_n = results.count("ran:probe")
    fills_n = results.count("ran:fills")
    assert 1 <= probe_n <= 3
    assert fills_n >= 27


def test_b7_iii_only_probe_candidates_serves_one_per_tick(tmp_path):
    """(iii) 只有探測候選、沒有 fills-like 積壓時，每 tick 一個 probe（借用，
    不空等）。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    addrs = [f"0x{'0' * 24}{i:016x}" for i in range(3)]
    store.upsert_candidates([(a, None, i + 1, None) for i, a in enumerate(addrs)],
                            as_of=clock.now())
    for a in addrs:
        store.bootstrap_address_fills(a, clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                      window_end_ms=now_ms, params_fp="")
        _complete_scan(store, a, result="complete", reason="count_below_retention_threshold",
                       window_end_ms=now_ms, finished_at=float(hash(a) % 1000))

    hl = FakeHL()
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True

    results = [sched.tick() for _ in range(3)]
    assert results == ["ran:probe"] * 3
    assert sched.status()["probe"]["candidates"] == 0


def test_b7_vi_verify_job_strictly_yields_to_due_increment(tmp_path):
    """(vi) `fills_verify` 讓位：同一 tick 若有到期的增量 `fills` job，一律先
    領增量，`fills_verify` 不被領。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    store.upsert_candidates([("0xabc", None, 1, None), ("0xdef", None, 2, None)],
                            as_of=clock.now())
    store.bootstrap_address_fills("0xabc", clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                  window_end_ms=now_ms, params_fp="")
    store.enqueue("0xabc:fills", "0xabc", "fills", 2, clock.now())
    store.enqueue("0xdef:fills_verify", "0xdef", "fills_verify", 4, clock.now())

    hl = FakeHL()
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True

    r = sched.tick()
    assert r == "ran:fills"
    # verify job 仍在，沒被這個 tick 領走。
    row = store._db.execute(
        "SELECT lease_until FROM refresh_job WHERE key='0xdef:fills_verify'").fetchone()
    assert row is not None and row[0] is None


def test_b7_vi_verify_job_runs_when_nothing_else_due(tmp_path):
    """verify job 只在該地址**沒有進行中的 scan**時才建立新的 `verify` scan
    ——這裡先把 `bootstrap_address_fills` 自動建立的 `initial` scan 收尾成
    `complete`（模擬遷移產生 `fills_verify` job 的真實前提：地址已有歷史，
    只是證據不明），才符合 `_run_scan` 建立 `verify` scan 的前提。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    store.upsert_candidates([("0xdef", None, 1, None)], as_of=clock.now())
    now_ms = int(clock.now() * 1000)
    store.bootstrap_address_fills("0xdef", clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                  window_end_ms=now_ms, params_fp="")
    _complete_scan(store, "0xdef", result="complete", reason="retention_boundary_verified",
                   window_end_ms=now_ms)
    store._db.execute("UPDATE fills_sync SET evidence_unknown=1 WHERE address='0xdef'")
    store.enqueue("0xdef:fills_verify", "0xdef", "fills_verify", 4, clock.now())

    # 滿頁（非短頁）：verify scan 建立後續抓一頁但不立刻完成，才能在斷言時
    # 觀察到它仍是 `status='running'` 的 `verify` kind。
    full_page = _fills_page(PAGE_LIMIT, now_ms - 30 * 86_400_000)
    hl = SequencedFillsHL([full_page])
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True

    r = sched.tick()
    assert r == "ran:fills_verify"
    scan = store.get_active_scan("0xdef")
    assert scan is not None and scan.kind == "verify"


def test_b7_vii_probe_candidate_survives_restart(tmp_path):
    """(vii) 重啟：新建 scheduler 實例後探測候選由 DB 推導照常進行
    （`next_probe_candidate` 純 DB 查詢，無記憶體佇列）。"""
    clock = Clock(t=40 * 86400.0)
    db_path = tmp_path / "explore.db"
    now_ms = int(clock.now() * 1000)
    store1 = ExploreStore(db_path, now_fn=clock.now)
    store1.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store1.bootstrap_address_fills("0xabc", clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                   window_end_ms=now_ms, params_fp="")
    _complete_scan(store1, "0xabc", result="complete",
                   reason="count_below_retention_threshold", window_end_ms=now_ms)

    # 重啟：新的 store／scheduler 物件，共用同一個 DB 檔。
    store2 = ExploreStore(db_path, now_fn=clock.now)
    hl = FakeHL()
    sched2 = _sched(store2, hl, clock=clock)
    sched2._bootstrapped = True

    r = sched2.tick()
    assert r == "ran:probe"
    assert sched2.status()["probe"]["total"] == 1


def test_b7_viii_new_address_lifecycle(tmp_path):
    """(viii) 新地址生命週期：建列 → initial scan 完成 → complete → 增量 →
    三者順序與欄位。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    payload = _payload(["0xAAA0000000000000000000000000000000AAA1"])
    hl = SequencedFillsHL([[]])  # initial scan 立刻短頁收尾（空頁）
    sched = _sched(store, hl, leaderboard_source_fn=lambda: payload, clock=clock,
                  cfg=ExploreConfig(candidate_pool=1))

    sched.tick()  # bootstrap
    r = sched.tick()
    assert r == "ran:candidates"
    addr = "0xaaa0000000000000000000000000000000aaa1"
    sync = store.get_sync(addr)
    assert sync.completeness == "backfilling"
    scan = store.get_active_scan(addr)
    assert scan is not None and scan.kind == "initial" and scan.status == "running"

    # 推進到 fills_scan job 到期並執行：initial scan 完成 → CAS 寫回 complete。
    store._db.execute("UPDATE refresh_job SET next_attempt_at=? WHERE key=?",
                      (clock.now(), f"{addr}:fills_scan"))
    r2 = sched.tick()
    assert r2 == "ran:fills_scan"
    sync2 = store.get_sync(addr)
    assert sync2.completeness == "complete"
    assert sync2.scan_id == scan.scan_id

    # 增量 job 到期並執行：不影響 completeness，只延伸 synced_through。
    store._db.execute("UPDATE refresh_job SET next_attempt_at=? WHERE key=?",
                      (clock.now(), f"{addr}:fills"))
    hl._pages.append([])
    r3 = sched.tick()
    assert r3 == "ran:fills"
    sync3 = store.get_sync(addr)
    assert sync3.completeness == "complete"  # 增量不改變 completeness


# ============================================================
# Task 7.9a A1：fills 週期單一來源——scheduler 重排間隔與
# `explore_fills_sync.plan_incremental` 的增量寬限期必須讀同一個 `fills_every_s`。
# ============================================================

@pytest.mark.parametrize("period", [7200, 21600])
def test_fills_period_single_source_ties_reschedule_and_plan_incremental(tmp_path, period):
    """完成一輪增量後：(i) 重排間隔 ≈ period；(ii) `period − 1s` 仍是 noop
    （不打上游）；(iii) `period + 1s` 開新的增量輪（打上游）。三者都隨同一個
    `fills_every_s` 值變化，不是各自一份常數。"""
    clock = Clock(t=40 * 86400.0)  # window 起點在 epoch 之後，避免負時間戳
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    addr = "0xabc"
    now_ms = int(clock.now() * 1000)
    store.upsert_candidates([(addr, None, 1, None)], as_of=clock.now())
    store.bootstrap_address_fills(addr, clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                  window_end_ms=now_ms, params_fp="")
    # Task 7.9d-S S1：`bootstrap_address_fills` 會建一個 running 的 initial scan，
    # 而對帳（首 tick／每個 candidates 輪）會為它補一個 `fills_scan` job 去續跑
    # ——本測試只量增量軌的週期，先把首次遍歷收尾掉，免得遍歷軌搶走 tick。
    _complete_scan(store, addr, result="complete", reason="retention_boundary_verified",
                   finished_at=clock.now())
    store.enqueue(f"{addr}:fills", addr, "fills", 2, clock.now())
    hl = SequencedFillsHL([[], []])
    sched = _sched(store, hl, clock=clock, fills_every_s=period)
    sched._bootstrapped = True

    r1 = sched.tick()
    assert r1 == "ran:fills"

    row = store._db.execute(
        "select next_attempt_at from refresh_job where key=?", (f"{addr}:fills",)).fetchone()
    # (i) 完成收尾後的重排間隔 ≈ period（±10% jitter）。
    assert row[0] == pytest.approx(clock.now() + period, rel=0.15)

    sync = store.get_sync(addr)
    window_end_ms = sync.window_end_ms
    calls_after_round1 = len(hl.calls)

    # (ii) period − 1s：仍是 noop，不打上游。
    clock.t = (window_end_ms + period * 1000 - 1000) / 1000
    store._db.execute("UPDATE refresh_job SET next_attempt_at=? WHERE key=?",
                      (clock.now(), f"{addr}:fills"))
    r2 = sched.tick()
    assert r2 == "ran:fills"
    assert len(hl.calls) == calls_after_round1

    # (iii) period + 1s：開新的增量輪，打上游。
    clock.t = (window_end_ms + period * 1000 + 1000) / 1000
    store._db.execute("UPDATE refresh_job SET next_attempt_at=? WHERE key=?",
                      (clock.now(), f"{addr}:fills"))
    r3 = sched.tick()
    assert r3 == "ran:fills"
    assert len(hl.calls) == calls_after_round1 + 1


def test_run_api_passes_configured_fills_period_to_scheduler(tmp_path, monkeypatch):
    """`scripts.run_api` 把 `cfg.explore_fills_period_s` 原樣傳給
    `ExploreScheduler(fills_every_s=...)`（沿 `test_run_api_wiring.py` 的
    `__init__` 攔截慣例）。"""
    import threading

    import scripts.run_api as run_api

    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: None)
    started: list = []
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self))

    import spark.publicapi.explore_scheduler as explore_scheduler_mod
    captured: dict = {}
    real_init = explore_scheduler_mod.ExploreScheduler.__init__

    def fake_init(self, **kwargs):
        captured.update(kwargs)
        real_init(self, **kwargs)
    monkeypatch.setattr(explore_scheduler_mod.ExploreScheduler, "__init__", fake_init)

    env = {
        "FILET_API_NETWORK": "testnet", "FILET_BUILDER_ADDR": "0x" + "b1" * 20,
        "FILET_SIWE_DOMAIN": "filet.example", "FILET_SIWE_URI": "https://filet.example",
        "FILET_API_DB": str(tmp_path / "api.db"),
        "FILET_KEYSVC_SOCK": str(tmp_path / "keysvc.sock"),
        "FILET_PENDING_PATH": str(tmp_path / "pending.json"),
        "FILET_EXCHANGE_DIR": str(tmp_path / "exchange"),
        "FILET_STATE_BASE": str(tmp_path / "state"),
        "FILET_LEADERS_PATH": str(tmp_path / "leaders.json"),
        "EXPLORE_UPSTREAM_REFRESH": "1",
        "FILET_EXPLORE_DB": str(tmp_path / "explore.db"),
        "FILET_EXPLORE_FILLS_PERIOD_S": "7200",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    run_api.main()

    assert captured["fills_every_s"] == 7200


# ============================================================
# Task 7.9a A2：重試上限——只計實際嘗試過的暫時性失敗，第 8 次隔離 24 小時，
# 隔離期滿只釋放一次恢復嘗試。
# ============================================================

class FlakyStateHL:
    """`clearinghouse_state` 前 `fail_times` 次拋 `exc_factory()`，之後成功。"""

    def __init__(self, fail_times: int, exc_factory=lambda: ConnectionError("boom")):
        self.fail_times = fail_times
        self.exc_factory = exc_factory
        self.calls = 0

    def clearinghouse_state(self, address):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc_factory()
        return {"marginSummary": {"accountValue": "1"}}


def test_max_job_attempts_quarantines_on_eighth_transient_failure(tmp_path):
    clock = Clock()
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())  # 7.9d-S S2
    store.enqueue("0xabc:state", "0xabc", "state", 0, clock.now())
    hl = FlakyStateHL(fail_times=8)
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True

    results = []
    for _ in range(8):
        results.append(sched.tick())
        if results[-1] == "retry":
            store._db.execute(
                "UPDATE refresh_job SET next_attempt_at=? WHERE key=?",
                (clock.now(), "0xabc:state"))

    assert results[:7] == ["retry"] * 7
    assert results[7] == "quarantined"

    row = store._db.execute(
        "SELECT attempts, next_attempt_at, last_error FROM refresh_job "
        "WHERE key='0xabc:state'").fetchone()
    assert row[0] == 7
    assert row[1] == pytest.approx(clock.now() + 86400)
    assert row[2].startswith("max_attempts:")
    assert sched.status()["quarantined_max_attempts"] == 1


def test_quarantine_release_gives_exactly_one_retry_before_requarantine(tmp_path):
    """隔離期滿後 `attempts` 仍是 `MAX_JOB_ATTEMPTS - 1`（7）——失敗一次立刻
    再達到上限、再隔離，不會重新給滿 8 次。"""
    clock = Clock(t=1000.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())  # 7.9d-S S2
    store.enqueue("0xabc:state", "0xabc", "state", 0, clock.now())
    store._db.execute("UPDATE refresh_job SET attempts=7 WHERE key='0xabc:state'")
    hl = FlakyStateHL(fail_times=1)
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True

    r = sched.tick()

    assert r == "quarantined"
    row = store._db.execute(
        "SELECT attempts, next_attempt_at, last_error FROM refresh_job "
        "WHERE key='0xabc:state'").fetchone()
    assert row[0] == 7
    assert row[1] == pytest.approx(clock.now() + 86400)
    assert row[2].startswith("max_attempts:")
    assert sched.status()["quarantined_max_attempts"] == 1


def test_quarantine_release_success_resets_attempts_to_zero(tmp_path):
    clock = Clock(t=1000.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store.enqueue("0xabc:state", "0xabc", "state", 0, clock.now())
    store._db.execute("UPDATE refresh_job SET attempts=7 WHERE key='0xabc:state'")
    hl = FakeHL()
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True

    r = sched.tick()

    assert r == "ran:state"
    row = store._db.execute(
        "SELECT attempts FROM refresh_job WHERE key='0xabc:state'").fetchone()
    assert row[0] == 0


def test_budget_exhausted_and_scope_paused_interleaved_do_not_advance_attempts(tmp_path):
    """`BudgetExhausted`／`ScopePaused` 穿插在暫時性失敗之間，不推進
    attempts——只有真的發送過的 `ConnectionError` 才算一次嘗試。"""
    from spark.publicapi.hl_budget import BudgetExhausted, ScopePaused

    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    addr = "0x" + "ab" * 20
    store.upsert_candidates([(addr, None, 1, None)], as_of=clock.now())  # 7.9d-S S2
    store.enqueue(f"{addr}:state", addr, "state", 0, clock.now())

    class FlakyHL:
        def __init__(self):
            self._plan = iter([
                ConnectionError("boom"),
                BudgetExhausted("x"),
                BudgetExhausted("x"),
                ScopePaused("explore", clock.now() + 1),
            ])

        def clearinghouse_state(self, address):
            raise next(self._plan)

    sched = _sched(store, FlakyHL(), clock=clock)
    sched._bootstrapped = True

    results = []
    for _ in range(4):
        results.append(sched.tick())
        store._db.execute(
            "UPDATE refresh_job SET next_attempt_at=?, lease_until=NULL WHERE key=?",
            (clock.now(), f"{addr}:state"))

    assert results == ["retry", "no_budget", "no_budget", "paused"]
    row = store._db.execute(
        "SELECT attempts FROM refresh_job WHERE key=?", (f"{addr}:state",)).fetchone()
    assert row[0] == 1


def test_rate_limited_does_not_advance_attempts(tmp_path):
    """429（`is_rate_limited`）走共享 cooldown，不算一次嘗試（7.9a 使用者修正
    ——舊版預設 `bump_attempts=True` 會讓 429 悄悄推進 attempts）。"""
    clock = Clock()
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())  # 7.9d-S S2
    store.enqueue("0xabc:state", "0xabc", "state", 0, clock.now())

    class RateLimitedHL:
        def clearinghouse_state(self, address):
            raise RuntimeError("429 Too Many Requests")

    sched = _sched(store, RateLimitedHL(), clock=clock)
    sched._bootstrapped = True

    r = sched.tick()

    assert r == "rate_limited"
    row = store._db.execute(
        "SELECT attempts FROM refresh_job WHERE key='0xabc:state'").fetchone()
    assert row[0] == 0


# ============================================================
# Task 7.9a A3：callback 不丟工作——`on_dirty` 拋例外不得讓已完成的 job
# 漏排；例外吞下並計入 `dirty_errors`。
# ============================================================

def test_notify_dirty_swallows_exception_without_losing_job(tmp_path):
    clock = Clock()
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    addr = "0xabc"
    store.upsert_candidates([(addr, None, 1, None)], as_of=clock.now())
    store.enqueue(f"{addr}:state", addr, "state", 0, clock.now())

    def boom():
        raise RuntimeError("dirty boom")

    sched = _sched(store, FakeHL(), clock=clock, on_dirty=boom)
    sched._bootstrapped = True

    for i in range(20):
        r = sched.tick()
        assert r == "ran:state"
        if i < 19:
            store._db.execute(
                "UPDATE refresh_job SET next_attempt_at=? WHERE key=?",
                (clock.now(), f"{addr}:state"))

    row = store._db.execute(
        "SELECT next_attempt_at FROM refresh_job WHERE key=?", (f"{addr}:state",)).fetchone()
    assert row is not None
    assert row[0] > clock.now()
    assert sched.status()["dirty_errors"] == 20


def test_quarantine_of_fills_scan_job_writes_scan_error(tmp_path):
    """`_quarantine` 對 `fills_scan`／`fills_verify` kind 走
    `ExploreStore.set_scan_error`（該 scan 的 `last_error`），不是
    `set_sync_error`（增量軌）——遍歷軌與增量軌各自有各自的錯誤欄位。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store.bootstrap_address_fills("0xabc", clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                  window_end_ms=now_ms, params_fp="")
    store.enqueue("0xabc:fills_scan", "0xabc", "fills_scan", 2, clock.now())
    scan_before = store.get_active_scan("0xabc")

    class BoomHL:
        def get_fills_page(self, address, start_ms, end_ms):
            raise RuntimeError("weird failure")

    sched = _sched(store, BoomHL(), clock=clock)
    sched._bootstrapped = True

    r = sched.tick()
    assert r == "quarantined"
    scan_after = store.get_scan(scan_before.scan_id)
    assert scan_after.last_error is not None
    assert "weird failure" in scan_after.last_error
    assert scan_after.status == "running"  # 隔離不改變 scan 本身的狀態


def test_notify_dirty_exception_during_probe_does_not_lose_probe_result(tmp_path):
    """`_run_probe` 的 dirty 通知也吞例外——store 寫入（`apply_probe_result`）
    已經在通知之前完成，callback 失敗不影響探測結果落地。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store.bootstrap_address_fills("0xabc", clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                  window_end_ms=now_ms, params_fp="")
    _complete_scan(store, "0xabc", result="complete",
                   reason="count_below_retention_threshold", window_end_ms=now_ms)

    def boom():
        raise RuntimeError("dirty boom")

    hl = FakeHL()
    sched = _sched(store, hl, clock=clock, on_dirty=boom)
    sched._bootstrapped = True

    r = sched.tick()

    assert r == "ran:probe"
    assert sched.status()["dirty_errors"] == 1
    assert store.get_sync("0xabc").reason == "count_below_retention_threshold_probe_empty"


# ============================================================
# Task 7.9c-S S7：遍歷軌生命週期（Critical C1／C2 的守門回歸測試）、逐項準入、
# `fills_verify` 有界等待、`ScanWriteback` 四種收尾、探測行為測試恢復。
# ============================================================

ADDR_A = "0xAAA0000000000000000000000000000000AAA1"


class _SameMsFillsHL:
    """`get_fills_page` 恆回一整頁、且整頁時間戳都等於 `start_ms` ——
    `apply_scan_page` 的「同毫秒溢位」終止條件會讓遍歷**一頁就以 `partial`
    收尾**（`reason='same_ms_overflow'`），是測 partial 重掃排程最短的合法
    路徑（不必真的抓滿 8,000 筆）。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def get_fills_page(self, address, start_ms, end_ms):
        self.calls.append((address, start_ms, end_ms))
        return [{"coin": "BTC", "tid": start_ms + i, "time": start_ms} for i in range(PAGE_LIMIT)]


def _run_for(sched, clock, horizon_s: float, *, idle_step: float = 30.0) -> list[str]:
    """驅動 scheduler 直到虛擬時鐘前進 `horizon_s`：有事做的 tick 前進 1 秒、
    `idle` 快轉 `idle_step` 秒——與複審重現腳本
    `scratchpad/repro_rescan.py` 同一個驅動方式。"""
    t0 = clock.t
    results: list[str] = []
    while clock.t - t0 < horizon_s:
        r = sched.tick()
        results.append(r)
        clock.t += idle_step if r == "idle" else 1.0
    return results


def _scan_rows(store, address: str) -> list[tuple]:
    return store._db.execute(
        "SELECT kind, status, result, reason, started_at, finished_at FROM fills_scan "
        "WHERE address=? ORDER BY started_at", (address.lower(),)).fetchall()


def test_s7a_complete_address_never_rescans_across_candidate_rounds(tmp_path):
    """(a) Critical C2 守門：`complete` 地址跨多個 candidates 輪（30 分一輪）
    ＋3 天虛擬時間，`fills_scan` 列**恰好 1**（initial）、`partial_rescan` 0、
    探測 <= 1。

    舊行為：每個 candidates 輪都無條件 `enqueue(fills_scan)`，而
    `ExploreStore.complete` 是 DELETE——job 消失後下一輪又建，`_run_scan` 看到
    `completeness != backfilling` 就開 `partial_rescan`，單一地址 3 天 144 次
    整窗重掃。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    payload = _payload([ADDR_A])
    sched = _sched(store, FakeHL(), leaderboard_source_fn=lambda: payload, clock=clock,
                  cfg=ExploreConfig(candidate_pool=1))

    # Task 7.9d-S S6：守到 **enqueue 那一半**——每個 candidates 輪（含其後的
    # 對帳）跑完，只要地址已經 `complete`，就不該有任何到期的 `fills_scan`
    # job。舊版是「每輪 enqueue、下游 `_run_scan` 再丟棄」，只看 scan 列的話
    # 兩者長得一樣，但額度與 job 佇列已經被吃掉。
    results: list[str] = []
    t0 = clock.t
    while clock.t - t0 < 3 * 86400.0:
        r = sched.tick()
        results.append(r)
        clock.t += 30.0 if r == "idle" else 1.0
        if r == "ran:candidates":
            sync = store.get_sync(ADDR_A)
            if sync is not None and sync.completeness == "complete":
                assert sched.status()["due_by_kind"]["fills_scan"] == 0
    assert sched.status()["scan_job_dropped"] == 0   # 根本不會有多餘的 scan job

    assert results.count("ran:candidates") >= 5      # 至少 5 個 candidates 輪
    rows = _scan_rows(store, ADDR_A)
    assert len(rows) == 1, rows
    assert rows[0][0] == "initial" and rows[0][1] == "done" and rows[0][2] == "complete"
    assert [r for r in rows if r[0] == "partial_rescan"] == []
    assert results.count("ran:fills_scan") == 1
    assert store.get_sync(ADDR_A).completeness == "complete"
    assert sched.status()["probe"]["total"] <= 1


def test_s7b_partial_address_rescan_due_in_24h_and_at_most_3_in_3_days(tmp_path):
    """(b) Critical C1 守門：`partial` 收尾排下一次重掃用的是**秒制**的
    `PARTIAL_RESCAN_AFTER_S`——到期時刻在 `[finished_at + 86400 - 60,
    finished_at + 86400 + 60]`（舊版把毫秒常數加到秒制 `now`，排到 1,000 天
    後）；3 天內 `partial_rescan` <= 3。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    payload = _payload([ADDR_A])
    sched = _sched(store, _SameMsFillsHL(), leaderboard_source_fn=lambda: payload, clock=clock,
                  cfg=ExploreConfig(candidate_pool=1))

    # 先跑到首次遍歷以 partial 收尾。
    for _ in range(200):
        sync = store.get_sync(ADDR_A)
        if sync is not None and sync.completeness == "partial":
            break
        r = sched.tick()
        clock.t += 30.0 if r == "idle" else 1.0
    else:                                            # pragma: no cover - 防呆
        raise AssertionError("首次遍歷未在時限內以 partial 收尾")

    finished_at = store._db.execute(
        "SELECT finished_at FROM fills_scan WHERE address=? AND status='done'",
        (ADDR_A.lower(),)).fetchone()[0]
    due = store._db.execute(
        "SELECT next_attempt_at FROM refresh_job WHERE key=?",
        (f"{ADDR_A.lower()}:fills_scan",)).fetchone()[0]
    assert finished_at + 86400 - 60 <= due <= finished_at + 86400 + 60

    results = _run_for(sched, clock, 3 * 86400.0)
    rescans = [r for r in _scan_rows(store, ADDR_A) if r[0] == "partial_rescan"]
    assert 1 <= len(rescans) <= 3, rescans
    assert results.count("ran:candidates") >= 5


def test_s7c_lost_scan_job_is_recovered_within_one_increment_period(tmp_path):
    """(c) 修復路徑：`partial` 地址的 `fills_scan` job 被外部刪掉（且重掃期限
    已到）→ 一個增量週期內由 `_run_increment` 補排並執行。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    payload = _payload([ADDR_A])
    sched = _sched(store, _SameMsFillsHL(), leaderboard_source_fn=lambda: payload, clock=clock,
                  cfg=ExploreConfig(candidate_pool=1))
    for _ in range(200):
        sync = store.get_sync(ADDR_A)
        if sync is not None and sync.completeness == "partial":
            break
        r = sched.tick()
        clock.t += 30.0 if r == "idle" else 1.0

    # job 遺失 ＋ 重掃期限已過。
    store._db.execute("DELETE FROM refresh_job WHERE key=?", (f"{ADDR_A.lower()}:fills_scan",))
    store._db.commit()
    clock.t += 86400.0 + 60.0
    assert "fills_scan" not in store.job_kinds(ADDR_A)

    results = _run_for(sched, clock, 6 * 3600.0)     # 一個增量週期

    assert "ran:fills_scan" in results
    assert [r for r in _scan_rows(store, ADDR_A) if r[0] == "partial_rescan"]


def test_s7d_repro_rescan_three_day_thirty_minute_rounds_no_repeated_full_scan(tmp_path):
    """(d) 複審重現腳本 `scratchpad/repro_rescan.py` 的情境照抄：單一地址、
    每 30 分鐘一個 candidates 輪、3 天虛擬時間、空頁（首次遍歷立刻
    `complete`）——`partial_rescan == 0`、initial scan == 1。腳本原本跑出
    144 次 `partial_rescan`＋224 次增量。"""
    clock = Clock(1_700_000_000.0)
    store = ExploreStore(tmp_path / "e.db", now_fn=clock.now)
    hl = FakeHL()
    payload = _payload([ADDR_A])
    sched = _sched(store, hl, leaderboard_source_fn=lambda: payload, clock=clock,
                  cfg=ExploreConfig(candidate_pool=1))

    results = _run_for(sched, clock, 3 * 86400.0)

    rows = _scan_rows(store, ADDR_A)
    kinds = [r[0] for r in rows]
    assert kinds.count("partial_rescan") == 0, rows
    assert kinds.count("initial") == 1, rows
    assert len(rows) == 1, rows
    assert results.count("ran:fills_scan") == 1
    assert sched.status()["scan_job_dropped"] == 0   # 根本不會有多餘的 scan job


def test_s7e_admission_skips_single_job_for_existing_address_new_candidate_exempt(tmp_path):
    """(e) 逐項準入（W4）：cap 滿時只跳過**既有地址缺的那一個 job**
    （`admission_skipped` 計一次），既有 job 不受影響；同一輪的新候選
    （`bootstrap_address_fills` 回 True）仍拿到完整的 5 個 job。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    existing = ADDR_A
    fresh = "0xBBB0000000000000000000000000000000BBB2"
    now_ms = int(clock.now() * 1000)
    store.bootstrap_address_fills(existing, clock.now(),
                                  window_start_ms=now_ms - 30 * 86_400_000,
                                  window_end_ms=now_ms, params_fp="")
    for kind, prio in (("state", 0), ("portfolio", 1), ("ledger", 1)):
        store.enqueue(f"{existing.lower()}:{kind}", existing, kind, prio, clock.now() + 10**6)
    # active_n=2 → cap=14；此刻 refresh_job 共 14 列（candidates 1＋既有 3＋dummy 10）
    for i in range(10):
        store.enqueue(f"dummy:{i}", None, "dummy", 5, clock.now() + 10**6)

    payload = _payload([existing, fresh])
    sched = _sched(store, FakeHL(), leaderboard_source_fn=lambda: payload, clock=clock,
                  cfg=ExploreConfig(candidate_pool=2))
    sched._bootstrapped = True
    store.enqueue("candidates:candidates", None, "candidates", 0, clock.now())

    assert sched.tick() == "ran:candidates"

    existing_kinds = store.job_kinds(existing)
    # fills 被 cap 擋下；`fills_scan` 由對帳補回（running 的 initial scan 沒有 job
    # 會推進它——7.9d-S S1 的修復路徑不受 cap 限制，見 reconcile_scan_jobs）。
    assert existing_kinds == {"state", "portfolio", "ledger", "fills_scan"}
    assert sched.status()["admission_skipped"] == 1
    assert store.job_kinds(fresh) == {"state", "portfolio", "ledger", "fills", "fills_scan"}


class _VerifyFixture:
    """`fills_verify` 有界等待測試的共用 setup：一個永遠有積壓的增量地址
    （`_ContinuingFillsHL` 恆回滿頁）＋一個帶 `fills_verify` job 的地址。"""

    def __init__(self, tmp_path, clock, *, verify_due_ago: float):
        self.store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
        now_ms = int(clock.now() * 1000)
        self.store.upsert_candidates([("0xbusy", None, 1, None), ("0xver", None, 2, None)],
                                     as_of=clock.now())
        self.store.bootstrap_address_fills("0xbusy", clock.now(),
                                           window_start_ms=now_ms - 30 * 86_400_000,
                                           window_end_ms=now_ms + 10**9, params_fp="")
        sync = self.store.get_sync("0xbusy")
        import dataclasses as _dc
        self.store.insert_fills_page(
            "0xbusy", [], _dc.replace(sync, cursor_ms=now_ms + 1, window_end_ms=now_ms + 10**9))
        self.store.enqueue("0xbusy:fills", "0xbusy", "fills", 2, clock.now())

        self.store.bootstrap_address_fills("0xver", clock.now(),
                                           window_start_ms=now_ms - 30 * 86_400_000,
                                           window_end_ms=now_ms, params_fp="")
        _complete_scan(self.store, "0xver", result="complete",
                       reason="retention_boundary_verified", window_end_ms=now_ms,
                       finished_at=clock.now())
        self.store._db.execute("UPDATE fills_sync SET evidence_unknown=1 WHERE address='0xver'")
        self.store.enqueue("0xver:fills_verify", "0xver", "fills_verify", 4,
                           clock.now() - verify_due_ago)


def test_s7f_overdue_verify_waits_for_its_page_share(tmp_path):
    """(f) Task 7.9d-S S3（**取代**舊的 `test_s7f_verify_served_after_bounded_wait`）：
    逾期 `VERIFY_MAX_WAIT_S` 的 verify **不**搶下一個 tick——它只在輔助份額
    （每 `SPECIAL_SERVE_RATIO` 頁 fills-like 一次）輪到時被服務，屆時再計
    `verify_served_by_deadline`。舊契約「逾期即 claim」讓 8 件逾期 verify 連佔
    8 個名額、搶光增量（主線程實跑核實）。"""
    from spark.publicapi.explore_scheduler import SPECIAL_SERVE_RATIO, VERIFY_MAX_WAIT_S

    assert VERIFY_MAX_WAIT_S == 2 * 3600
    assert SPECIAL_SERVE_RATIO == 9
    clock = Clock(t=40 * 86400.0)
    fx = _VerifyFixture(tmp_path, clock, verify_due_ago=3 * 3600.0)
    sched = _sched(fx.store, _ContinuingFillsHL(), clock=clock)
    sched._bootstrapped = True

    results = [sched.tick() for _ in range(SPECIAL_SERVE_RATIO + 1)]

    assert results[:SPECIAL_SERVE_RATIO] == ["ran:fills"] * SPECIAL_SERVE_RATIO
    assert results[SPECIAL_SERVE_RATIO] == "ran:fills_verify"
    assert sched.status()["verify_served_by_deadline"] == 1


def test_s7f_verify_strictly_yields_before_deadline(tmp_path):
    """(f) 未滿 `VERIFY_MAX_WAIT_S` 時維持嚴格讓位：有到期的增量 job 就先做
    增量，verify job 不被領（lease 仍是 NULL）。"""
    clock = Clock(t=40 * 86400.0)
    fx = _VerifyFixture(tmp_path, clock, verify_due_ago=3600.0)   # 只等了 1 小時
    sched = _sched(fx.store, _ContinuingFillsHL(), clock=clock)
    sched._bootstrapped = True

    assert [sched.tick() for _ in range(3)] == ["ran:fills"] * 3
    assert sched.status()["verify_served_by_deadline"] == 0
    row = fx.store._db.execute(
        "SELECT lease_until FROM refresh_job WHERE key='0xver:fills_verify'").fetchone()
    assert row is not None and row[0] is None


def _partial_scan_ready(tmp_path, clock):
    """一個即將以 `partial` 收尾的遍歷：候選地址＋到期的 `fills_scan` job，
    HL 回同毫秒滿頁（一頁收尾）。"""
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store.bootstrap_address_fills("0xabc", clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                  window_end_ms=now_ms, params_fp="")
    store.enqueue("0xabc:fills_scan", "0xabc", "fills_scan", 2, clock.now())
    sched = _sched(store, _SameMsFillsHL(), clock=clock)
    sched._bootstrapped = True
    return store, sched


def _rescan_job_due(store, address: str = "0xabc"):
    row = store._db.execute(
        "SELECT next_attempt_at FROM refresh_job WHERE key=?",
        (f"{address}:fills_scan",)).fetchone()
    return None if row is None else row[0]


def test_s7g_writeback_applied_schedules_next_rescan(tmp_path):
    """(g) `ScanWriteback.APPLIED`：既有收尾——結論寫回、24 小時後排下一次重掃。"""
    clock = Clock(t=40 * 86400.0)
    store, sched = _partial_scan_ready(tmp_path, clock)

    assert sched.tick() == "ran:fills_scan"

    assert store.get_sync("0xabc").completeness == "partial"
    assert _rescan_job_due(store) == pytest.approx(clock.now() + 86400, abs=60)
    st = sched.status()
    assert (st["scan_writeback_duplicate"], st["scan_writeback_stale"],
            st["scan_writeback_missing"]) == (0, 0, 0)


def test_s7g_writeback_duplicate_counts_and_still_finishes(tmp_path):
    """(g) `DUPLICATE`（結論已經是目前的了）：記 info、照常收尾（job 完成、
    下一次重掃照排）。"""
    clock = Clock(t=40 * 86400.0)
    store, sched = _partial_scan_ready(tmp_path, clock)
    real = store.complete_scan
    store.complete_scan = lambda *a, **kw: (real(*a, **kw), ScanWriteback.DUPLICATE)[1]

    assert sched.tick() == "ran:fills_scan"

    assert sched.status()["scan_writeback_duplicate"] == 1
    assert _rescan_job_due(store) == pytest.approx(clock.now() + 86400, abs=60)


def test_s7g_writeback_stale_does_not_schedule_rescan(tmp_path):
    """(g) `STALE`（`fills_sync` 已指向更新的一次遍歷）：記 warning、**不**排
    下一次重掃——那一次遍歷的結論根本沒被採用。"""
    clock = Clock(t=40 * 86400.0)
    store, sched = _partial_scan_ready(tmp_path, clock)
    real = store.complete_scan
    store.complete_scan = lambda *a, **kw: (real(*a, **kw), ScanWriteback.STALE)[1]

    assert sched.tick() == "ran:fills_scan"

    assert sched.status()["scan_writeback_stale"] == 1
    assert _rescan_job_due(store) is None


def test_s7g_writeback_missing_drops_job(tmp_path):
    """(g) `MISSING`（沒有 `fills_sync` 列）：記 warning、`_complete(job)` 後
    回 `"dropped"`，不排下一次重掃。"""
    clock = Clock(t=40 * 86400.0)
    store, sched = _partial_scan_ready(tmp_path, clock)
    real = store.complete_scan
    store.complete_scan = lambda *a, **kw: (real(*a, **kw), ScanWriteback.MISSING)[1]

    assert sched.tick() == "dropped"

    assert sched.status()["scan_writeback_missing"] == 1
    assert _rescan_job_due(store) is None


def test_s7g_writeback_non_enum_raises_type_error(tmp_path):
    """(g) Task 7.9d-S S4（**取代**舊的
    `test_s7g_writeback_bool_true_is_treated_as_applied`）：相容層拆除後，
    `complete_scan` 回非 `ScanWriteback`（例如舊式 `bool`）一律拋
    `TypeError`——介面不符必須明確失敗，不得靜默映射成某一種收尾。"""
    clock = Clock(t=40 * 86400.0)
    store, sched = _partial_scan_ready(tmp_path, clock)
    real = store.complete_scan
    store.complete_scan = lambda *a, **kw: bool(real(*a, **kw)) or True

    with pytest.raises(TypeError):
        sched.tick()


# --- (h) 恢復的五條探測行為測試（7.9b 拆軌時被刪，改寫為「探測由 DB 推導」版） ---

class _ProbeHL:
    """探測專用假 HL：`get_fills_page` 回 `probe_result`（`Exception` 實例會被
    raise）。新架構下探測是獨立的 tick 決策（`next_probe_candidate` 純 DB
    推導），不再夾在某一輪 fills 之後，所以不需要舊版的 `real_pages`。"""

    def __init__(self, probe_result):
        self._probe_result = probe_result
        self.probe_calls: list[tuple] = []

    def get_fills_page(self, address, start_ms, end_ms):
        self.probe_calls.append((address, start_ms, end_ms))
        if isinstance(self._probe_result, Exception):
            raise self._probe_result
        return self._probe_result


def _probe_ready(tmp_path, clock, probe_result, *, active: bool = True):
    """一個「已完成遍歷、結論是門檻推論」的候選＝探測候選（見
    `ExploreStore.next_probe_candidate`）。`active=False` 時地址不是候選。"""
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    if active:
        store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store.bootstrap_address_fills("0xabc", clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                  window_end_ms=now_ms, params_fp="")
    scan = _complete_scan(store, "0xabc", result="complete",
                          reason="count_below_retention_threshold", window_end_ms=now_ms)
    hl = _ProbeHL(probe_result)
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True
    return store, hl, sched, scan


def test_s7h_probe_positive_upgrades_reason(tmp_path):
    """(h1) 探測命中（窗內仍有更早的成交）→ reason 升級為
    `retention_boundary_verified`，探測窗＝`[scan.window_start - 1 天,
    scan.window_start - 1]`。"""
    clock = Clock(t=40 * 86400.0)
    window_start_ms = int(clock.t * 1000) - 30 * 86_400_000
    store, hl, sched, scan = _probe_ready(
        tmp_path, clock, [{"coin": "BTC", "tid": 1, "time": window_start_ms - 1}])

    assert sched.tick() == "ran:probe"

    assert store.get_sync("0xabc").reason == "retention_boundary_verified"
    assert len(hl.probe_calls) == 1
    addr, probe_start, probe_end = hl.probe_calls[0]
    assert addr == "0xabc"
    assert probe_end == scan.window_start_ms - 1
    assert probe_start == scan.window_start_ms - 86_400_000
    assert sched.status()["probe"]["verified"] == 1


def test_s7h_probe_negative_marks_probe_empty_reason(tmp_path):
    """(h2) 探測回空頁 → reason 記為
    `count_below_retention_threshold_probe_empty`（該 reason 天然被探測候選
    查詢排除，不會每輪重探）。"""
    clock = Clock(t=40 * 86400.0)
    store, hl, sched, _ = _probe_ready(tmp_path, clock, [])

    assert sched.tick() == "ran:probe"

    assert store.get_sync("0xabc").reason == "count_below_retention_threshold_probe_empty"
    assert len(hl.probe_calls) == 1
    probe = sched.status()["probe"]
    assert (probe["total"], probe["verified"], probe["empty"], probe["failed"],
            probe["stale"], probe["candidates"]) == (1, 0, 1, 0, 0, 0)


def test_s7h_probe_exception_does_not_fail_tick(tmp_path):
    """(h3) 探測呼叫拋例外 → 不逸出、不改 reason、tick 照常結束。"""
    clock = Clock(t=40 * 86400.0)
    store, hl, sched, _ = _probe_ready(tmp_path, clock, ConnectionError("boom"))

    assert sched.tick() == "ran:probe"

    assert store.get_sync("0xabc").reason == "count_below_retention_threshold"
    assert len(hl.probe_calls) == 1
    assert sched.status()["probe"]["failed"] == 1


def test_s7h_probe_response_out_of_window_counts_failed(tmp_path):
    """(h4) 探測回應落在探測窗之外（上游回應跑掉）→ 不升級 reason、計入
    `probe.failed`。"""
    clock = Clock(t=40 * 86400.0)
    store, hl, sched, _ = _probe_ready(tmp_path, clock,
                                       [{"coin": "BTC", "tid": 1, "time": 1}])

    assert sched.tick() == "ran:probe"

    assert store.get_sync("0xabc").reason == "count_below_retention_threshold"
    probe = sched.status()["probe"]
    assert (probe["total"], probe["verified"], probe["empty"], probe["failed"]) == (1, 0, 0, 1)


def test_s7h_probe_not_called_when_address_dropped(tmp_path):
    """(h5) 退池地址不探（`next_probe_candidate` 的 `candidate.active=1`
    條件）——不值得為非候選花探測預算。"""
    clock = Clock(t=40 * 86400.0)
    store, hl, sched, _ = _probe_ready(tmp_path, clock,
                                       [{"coin": "BTC", "tid": 1, "time": 1}], active=False)

    assert sched.tick() == "idle"

    assert hl.probe_calls == []
    assert sched.status()["probe"]["total"] == 0


def test_s7_status_exposes_lifecycle_counters(tmp_path):
    """S6：`status()` 的新觀測鍵（`/api/ops/health` 的 `explore_refresh` 直接
    展開這份 dict）。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store.bootstrap_address_fills("0xabc", clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                  window_end_ms=now_ms, params_fp="")
    store.enqueue("0xabc:fills_verify", "0xabc", "fills_verify", 4, clock.now())
    sched = _sched(store, FakeHL(), clock=clock)

    st = sched.status()

    assert st["scans_running"] == 1          # bootstrap 建的 initial scan 還在跑
    assert st["verify_remaining"] == 1
    assert st["due_by_kind"]["fills_verify"] == 1
    assert st["due_by_kind"]["fills_scan"] == 0
    assert set(st["due_by_kind"]) == {"candidates", "state", "portfolio", "ledger", "fills",
                                      "fills_scan", "fills_verify"}
    assert st["scan_job_dropped"] == 0
    assert st["admission_skipped"] == 0
    assert st["verify_served_by_deadline"] == 0
    assert st["scan_writeback_duplicate"] == 0
    assert st["scan_writeback_stale"] == 0
    assert st["scan_writeback_missing"] == 0


def test_s7_scan_job_dropped_when_state_says_no_rescan(tmp_path):
    """S2：`complete` 地址的 `fills_scan` job（例如人工補排、或舊版留下的）
    被領到時直接丟棄——不開 `partial_rescan`、不排下一個，計
    `scan_job_dropped`。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store.bootstrap_address_fills("0xabc", clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                  window_end_ms=now_ms, params_fp="")
    _complete_scan(store, "0xabc", result="complete", reason="retention_boundary_verified",
                   window_end_ms=now_ms, finished_at=clock.now())
    store.enqueue("0xabc:fills_scan", "0xabc", "fills_scan", 3, clock.now())
    hl = FakeHL()
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True

    assert sched.tick() == "dropped"

    assert sched.status()["scan_job_dropped"] == 1
    assert hl.calls == []                                   # 沒有打任何一頁
    assert _rescan_job_due(store) is None
    assert [r for r in _scan_rows(store, "0xabc") if r[0] == "partial_rescan"] == []


def test_s7_partial_not_yet_due_scan_job_is_dropped_not_rescanned(tmp_path):
    """S2：`partial` 但距離上一次遍歷還不到 `PARTIAL_RESCAN_AFTER_S` 時，
    領到 scan job 一樣丟棄（重掃期限是單一來源，不因 job 存在而提前）。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store.bootstrap_address_fills("0xabc", clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                  window_end_ms=now_ms, params_fp="")
    _complete_scan(store, "0xabc", result="partial", reason="retention_limit",
                   window_end_ms=now_ms, finished_at=clock.now() - 3600.0)   # 1 小時前
    store.enqueue("0xabc:fills_scan", "0xabc", "fills_scan", 3, clock.now())
    hl = FakeHL()
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True

    assert sched.tick() == "dropped"
    assert sched.status()["scan_job_dropped"] == 1
    assert hl.calls == []

    # 期限一過，同樣的 job 被補排後就會真的重掃。
    clock.t += 86400.0
    store.enqueue("0xabc:fills_scan", "0xabc", "fills_scan", 3, clock.now())
    assert sched.tick() == "ran:fills_scan"
    assert [r for r in _scan_rows(store, "0xabc") if r[0] == "partial_rescan"]


# ============================================================
# Task 7.9d-S（2026-09-22 使用者第二輪裁決）：狀態與工作對帳（S1）、退池不打
# 上游（S2）、verify／探測的頁面份額（S3）、status 同源（S5）＋部署門檻五測
# ============================================================

class _ResumableFillsHL:
    """合法頁的假 HL：每個地址前 `full_pages` 次回滿頁（遍歷續跑、不收尾），
    之後回短頁（收尾）。所有時間戳嚴格遞增且落在請求窗口內，
    `explore_fills_sync.validate_page` 視為合法——`_ContinuingFillsHL` 永遠滿頁、
    `FakeHL` 永遠空頁，兩者都測不到「遍歷停在半路、之後續跑收尾」這條路徑。"""

    def __init__(self, full_pages: int = 1, short_page: int = 3):
        self._full_pages = full_pages
        self._short_page = short_page
        self._used: dict[str, int] = {}
        self.calls: list[tuple] = []

    def get_fills_page(self, address, start_ms, end_ms):
        self.calls.append((address, start_ms, end_ms))
        used = self._used.get(address, 0)
        self._used[address] = used + 1
        span = max(0, end_ms - start_ms)
        # 窗口剩餘寬度不足時只能回短頁（每一筆要有自己的毫秒，且不得超出
        # `end_ms`——`validate_page` 會擋，超出窗口的頁面不是「合法的滿頁」）。
        n = min(PAGE_LIMIT if used < self._full_pages else self._short_page, max(span - 1, 0))
        step = max(1, span // (n + 1)) if n else 1
        return [{"coin": "BTC", "tid": start_ms + (i + 1) * step,
                 "time": start_ms + (i + 1) * step} for i in range(n)]

    def clearinghouse_state(self, address):
        return {"marginSummary": {"accountValue": "1"}}

    def portfolio(self, address):
        return [["day", {}]]

    def non_funding_ledger_updates(self, address, start_ms):
        return []


def _drive_until(sched, clock, predicate, *, limit: int = 600, idle_step: float = 30.0) -> list[str]:
    """驅動 scheduler 直到 `predicate(results)` 為真（`_run_for` 的條件版）。"""
    results: list[str] = []
    for _ in range(limit):
        r = sched.tick()
        results.append(r)
        clock.t += idle_step if r == "idle" else 1.0
        if predicate(results):
            return results
    raise AssertionError(f"條件未在 {limit} 個 tick 內達成（最後 10 個：{results[-10:]}）")


def test_s1_churned_backfilling_address_regains_scan_job_and_finishes(tmp_path):
    """部署門檻 (i)＋7.9c 複審 Critical：**回補中**的地址掉出候選池
    （`delete_jobs` 刪掉 `fills_scan` job）再回池 → 對帳補回 job、**續跑同一個
    `scan_id` 與游標**（不建新 scan、不整窗重抓）並完成首次回補。

    舊行為：`bootstrap_address_fills` 回 `False`、修復路徑只認 `partial` →
    `backfilling` 地址永遠拿不回 scan job（5 天模擬仍 backfilling；正式機部署
    當下有 69 個回補中的地址）。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    other = "0xBBB0000000000000000000000000000000BBB2"
    src = {"payload": _payload([ADDR_A, other])}
    hl = _ResumableFillsHL(full_pages=3)
    sched = _sched(store, hl, leaderboard_source_fn=lambda: src["payload"], clock=clock,
                  cfg=ExploreConfig(candidate_pool=2))

    _drive_until(sched, clock, lambda rs: store.running_scan(ADDR_A) is not None
                 and store.running_scan(ADDR_A).pages_done >= 1)
    scan = store.running_scan(ADDR_A)
    assert scan.kind == "initial"
    scan_id, cursor_ms = scan.scan_id, scan.cursor_ms

    # ADDR_A 掉出候選池：`deactivate_missing` ＋ `delete_jobs`（job 全刪）。
    # 立刻排一個 candidates 輪（priority 0，下一個 tick 就會被領），讓退池發生在
    # 遍歷還沒收尾的時候——這正是正式機 69 個回補中地址的處境。
    src["payload"] = _payload([other])
    store.enqueue("candidates:candidates", None, "candidates", 0, clock.now())
    _drive_until(sched, clock, lambda rs: rs[-1] == "ran:candidates")
    assert store.is_active(ADDR_A) is False
    assert store.job_kinds(ADDR_A) == set()
    assert store.running_scan(ADDR_A).scan_id == scan_id      # 遍歷仍停在半路

    # 回池：`bootstrap_address_fills` 回 False（列已存在）→ 只有對帳會補回 job。
    src["payload"] = _payload([ADDR_A, other])
    store.enqueue("candidates:candidates", None, "candidates", 0, clock.now())
    _drive_until(sched, clock, lambda rs: rs[-1] == "ran:candidates")
    assert "fills_scan" in store.job_kinds(ADDR_A)
    assert sched.status()["reconciled"]["resume_running"] >= 1
    resumed = store.running_scan(ADDR_A)
    assert resumed.scan_id == scan_id and resumed.cursor_ms == cursor_ms

    _drive_until(sched, clock,
                 lambda rs: store.get_sync(ADDR_A).completeness != "backfilling")

    sync = store.get_sync(ADDR_A)
    assert sync.completeness == "complete"
    assert sync.scan_id == scan_id                            # 完成的就是原本那次遍歷
    assert [r[0] for r in _scan_rows(store, ADDR_A)] == ["initial"]   # 沒有第二次遍歷
    assert sched.status()["scan_job_dropped"] == 0


def _partial_due_address(tmp_path, clock, addr: str = ADDR_A):
    """一個 `partial` 且重掃已到期（上一次遍歷是 2 天前）的 active 地址。"""
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    store.upsert_candidates([(addr, None, 1, None)], as_of=clock.now())
    store.bootstrap_address_fills(addr, clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                  window_end_ms=now_ms, params_fp="")
    _complete_scan(store, addr, result="partial", reason="retention_limit",
                   window_end_ms=now_ms, finished_at=clock.now() - 2 * 86400.0)
    return store


def test_s1_churned_partial_rescan_resumes_same_scan_id(tmp_path):
    """部署門檻 (i)：**partial 重掃**中途退池再回池 → 同樣續跑同一個 `scan_id`，
    不會留下第三筆 `fills_scan` 列（整窗重抓一次要 20 頁）。"""
    clock = Clock(t=40 * 86400.0)
    store = _partial_due_address(tmp_path, clock)
    hl = _ResumableFillsHL(full_pages=1)
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True
    sched._first_tick_done = True

    assert sched.reconcile_scan_jobs(clock.now()) == {"inactive_jobs_deleted": 0,
                                                      "partial_due": 1}
    assert sched.tick() == "ran:fills_scan"
    running = store.running_scan(ADDR_A)
    assert running.kind == "partial_rescan"
    scan_id, cursor_ms = running.scan_id, running.cursor_ms

    # 退池：對帳主動掃除殘留 job、但不動進行中的遍歷，也不補 job。
    store.deactivate_missing(set())
    assert sched.reconcile_scan_jobs(clock.now())["inactive_jobs_deleted"] >= 1
    assert store.job_kinds(ADDR_A) == set()

    # 回池。
    store.upsert_candidates([(ADDR_A, None, 1, None)], as_of=clock.now())
    assert sched.reconcile_scan_jobs(clock.now())["resume_running"] == 1
    resumed = store.running_scan(ADDR_A)
    assert (resumed.scan_id, resumed.cursor_ms) == (scan_id, cursor_ms)

    assert sched.tick() == "ran:fills_scan"                   # 短頁收尾
    assert store.running_scan(ADDR_A) is None
    assert [r[0] for r in _scan_rows(store, ADDR_A)] == ["initial", "partial_rescan"]
    assert store.get_sync(ADDR_A).scan_id == scan_id


def test_s1_reconcile_creates_initial_scan_for_orphan_backfilling_address(tmp_path):
    """S1：孤兒——`fills_sync` 是 `backfilling`、但既沒有進行中的遍歷也沒有
    scan 列（例如遷移／人工修復留下的狀態）→ 對帳建新的 `initial` 遍歷，
    不是丟棄（7.9c 在這個狀態下會 `scan_job_dropped`，回補永遠做不完）。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    store.upsert_candidates([(ADDR_A, None, 1, None)], as_of=clock.now())
    store.bootstrap_address_fills(ADDR_A, clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                  window_end_ms=now_ms, params_fp="")
    store._db.execute("DELETE FROM fills_scan WHERE address=?", (ADDR_A.lower(),))
    store._db.commit()
    hl = _ResumableFillsHL(full_pages=0)
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True
    sched._first_tick_done = True

    assert sched._needs_scan_job(ADDR_A, clock.now()) == "initial_missing"
    assert sched.reconcile_scan_jobs(clock.now())["initial_missing"] == 1

    assert sched.tick() == "ran:fills_scan"
    assert [r[0] for r in _scan_rows(store, ADDR_A)] == ["initial"]
    assert store.get_sync(ADDR_A).completeness == "complete"


def test_s1_reconcile_is_idempotent(tmp_path):
    """S1：對帳冪等——第二次連跑零變更（補過的 job 讓 `_needs_scan_job` 回
    `None`），`refresh_job` 的到期時刻也不被提前。"""
    clock = Clock(t=40 * 86400.0)
    store = _partial_due_address(tmp_path, clock)
    sched = _sched(store, FakeHL(), clock=clock)
    sched._bootstrapped = True
    sched._first_tick_done = True

    first = sched.reconcile_scan_jobs(clock.now())
    assert first == {"inactive_jobs_deleted": 0, "partial_due": 1}
    rows_before = store._db.execute(
        "SELECT key, kind, priority, next_attempt_at FROM refresh_job ORDER BY key").fetchall()

    clock.t += 5.0
    second = sched.reconcile_scan_jobs(clock.now())

    assert second == {"inactive_jobs_deleted": 0}
    assert store._db.execute(
        "SELECT key, kind, priority, next_attempt_at FROM refresh_job "
        "ORDER BY key").fetchall() == rows_before


def test_s2_reconcile_sweeps_inactive_jobs(tmp_path):
    """S2（裁決點 3）：對帳**主動掃除**所有非 active 地址的殘留 job——不只靠
    本輪 `deactivate_missing` 的回傳值（重啟、漏跑一輪、人工改動留下的 job
    沒有任何路徑會清，照樣被 `claim_due` 領走、照樣打上游）。`candidates`
    這個全域 job（`address IS NULL`）永不刪。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    keep, dropped, never = ADDR_A, "0xBBB0000000000000000000000000000000BBB2", "0xccc"
    store.upsert_candidates([(keep, None, 1, None), (dropped, None, 2, None)],
                            as_of=clock.now())
    store.deactivate_missing({keep.lower()})                  # dropped 退池
    for addr in (keep, dropped, never):
        store.enqueue(f"{addr.lower()}:state", addr, "state", 0, clock.now())
        store.enqueue(f"{addr.lower()}:fills_verify", addr, "fills_verify", 4, clock.now())
    store.enqueue("candidates:candidates", None, "candidates", 0, clock.now())
    sched = _sched(store, FakeHL(), clock=clock)

    out = sched.reconcile_scan_jobs(clock.now())

    assert out["inactive_jobs_deleted"] == 4                  # dropped 2 ＋ never 2
    assert store.job_kinds(keep) == {"state", "fills_verify"}
    assert store.job_kinds(dropped) == set()
    assert store.job_kinds(never) == set()
    assert store._db.execute(
        "SELECT COUNT(*) FROM refresh_job WHERE key='candidates:candidates'").fetchone()[0] == 1


def test_s2_inactive_address_jobs_send_nothing_upstream(tmp_path):
    """S2＋部署門檻 (iii)：退池地址的 `fills_verify`／`fills_scan`／`fills`
    （增量）／探測四條路徑，被領到時**零上游呼叫**——發送前就丟棄，計
    `inactive_job_dropped`。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store.bootstrap_address_fills("0xabc", clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                  window_end_ms=now_ms, params_fp="")
    scan = _complete_scan(store, "0xabc", result="complete",
                          reason="count_below_retention_threshold", window_end_ms=now_ms,
                          finished_at=clock.now())
    store.deactivate_missing(set())                           # 退池（job 刻意留著）
    for kind, prio in (("fills_verify", 4), ("fills_scan", 3), ("fills", 2)):
        store.enqueue(f"0xabc:{kind}", "0xabc", kind, prio, clock.now())
    hl = _ResumableFillsHL()
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True
    sched._first_tick_done = True   # 對帳的主動掃除另有測試，這裡測領到手上的閘門

    assert [sched.tick() for _ in range(3)] == ["dropped"] * 3
    sched._run_probe((scan.address, scan.scan_id), clock.now())

    assert hl.calls == []
    assert sched.status()["inactive_job_dropped"] == 4
    assert sched.status()["probe"]["total"] == 0
    assert store.job_kinds("0xabc") == set()


def _verify_burst_fixture(tmp_path, clock, *, n: int = 8, due_ago: float = 3 * 3600.0):
    """複審腳本 `scratchpad/rv_verify_burst.py` 的情境：`n` 個逾期 2 小時以上的
    `fills_verify` job ＋一個永遠有積壓的增量地址（`_ContinuingFillsHL` 恆滿頁）。"""
    import dataclasses as _dc
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    addrs = [f"0xver{i}" for i in range(n)]
    store.upsert_candidates([("0xbusy", None, 1, None)]
                            + [(a, None, i + 2, None) for i, a in enumerate(addrs)],
                            as_of=clock.now())
    store.bootstrap_address_fills("0xbusy", clock.now(),
                                  window_start_ms=now_ms - 30 * 86_400_000,
                                  window_end_ms=now_ms + 10**9, params_fp="")
    sync = store.get_sync("0xbusy")
    store.insert_fills_page("0xbusy", [], _dc.replace(sync, cursor_ms=now_ms + 1,
                                                      window_end_ms=now_ms + 10**9))
    store.enqueue("0xbusy:fills", "0xbusy", "fills", 2, clock.now())
    for a in addrs:
        store.bootstrap_address_fills(a, clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                      window_end_ms=now_ms, params_fp="")
        _complete_scan(store, a, result="complete", reason="retention_boundary_verified",
                       window_end_ms=now_ms, finished_at=clock.now())
        store._db.execute("UPDATE fills_sync SET evidence_unknown=1 WHERE address=?", (a,))
        store.enqueue(f"{a}:fills_verify", a, "fills_verify", 4, clock.now() - due_ago)
    return store


def test_s3_overdue_verify_burst_never_starves_increments(tmp_path):
    """S3＋部署門檻 (ii)：8 件逾期 2 小時以上的 `fills_verify` ＋持續積壓的增量
    → 前 20 個 tick 裡 verify <= 2、fills >= 18（每 10 次頁面准入至多 1 次輔助）。

    7.9c 的「逾期即 claim」在同一個情境下讓 8 件 verify 連佔前 8 個 tick
    （主線程實跑核實），等於把 fills 保留額度整批交給核驗軌。"""
    clock = Clock(t=40 * 86400.0)
    store = _verify_burst_fixture(tmp_path, clock)
    sched = _sched(store, _ContinuingFillsHL(), clock=clock)
    sched._bootstrapped = True
    sched._first_tick_done = True

    results = []
    for _ in range(20):
        results.append(sched.tick())
        clock.t += 1.0

    assert results.count("ran:fills_verify") <= 2
    assert results.count("ran:fills") >= 18


def test_s3_verify_runs_back_to_back_when_no_fills_due(tmp_path):
    """S3：fills 類沒有到期工作時，輔助份額可以連續——核驗軌不必空等 9 頁
    （`SPECIAL_SERVE_RATIO` 只在雙方都有積壓時生效）。"""
    clock = Clock(t=40 * 86400.0)
    store = _verify_burst_fixture(tmp_path, clock, n=3)
    store.delete_jobs("0xbusy")                               # 拿掉唯一的增量積壓
    sched = _sched(store, _ResumableFillsHL(full_pages=0), clock=clock)
    sched._bootstrapped = True
    sched._first_tick_done = True

    assert [sched.tick() for _ in range(3)] == ["ran:fills_verify"] * 3


def test_s5_status_counts_match_db_including_inactive_rows(tmp_path):
    """S5＋部署門檻 (iv)：`status()` 的母體＝`refresh_job`／`fills_scan` 表本身
    ——退池地址的 job 與遍歷仍計入（它們仍會被 `claim_due` 領走、仍消耗額度），
    只用 `active` 做分列。7.9c 用 `active_candidates()` 逐址點查，正式機 129 筆
    `fills_verify` 顯示 112。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    live, gone = ADDR_A, "0xBBB0000000000000000000000000000000BBB2"
    store.upsert_candidates([(live, None, 1, None), (gone, None, 2, None)], as_of=clock.now())
    for addr in (live, gone):
        store.bootstrap_address_fills(addr, clock.now(),
                                      window_start_ms=now_ms - 30 * 86_400_000,
                                      window_end_ms=now_ms, params_fp="")
        store.enqueue(f"{addr.lower()}:fills_verify", addr, "fills_verify", 4, clock.now())
    store.deactivate_missing({live.lower()})                  # gone 退池，job 還在
    sched = _sched(store, FakeHL(), clock=clock)

    st = sched.status()

    db_verify_rows = store._db.execute(
        "SELECT COUNT(*) FROM refresh_job WHERE kind='fills_verify'").fetchone()[0]
    db_running = store._db.execute(
        "SELECT COUNT(*) FROM fills_scan WHERE status='running'").fetchone()[0]
    assert db_verify_rows == 2 and db_running == 2
    assert st["verify_remaining"] == db_verify_rows           # 含退池地址
    assert st["scans_running"] == db_running
    assert st["jobs_by_kind"]["fills_verify"] == {"rows": 2, "addresses": 2,
                                                  "active_rows": 1, "inactive_rows": 1}
    assert st["scans"]["running_rows"] == 2
    assert st["scans"]["inactive_running_rows"] == 1
    assert st["scans"]["orphan_rows"] == 2                    # 兩個地址都還沒有 scan job
    assert set(st["due_by_kind"]) == {"candidates", "state", "portfolio", "ledger", "fills",
                                      "fills_scan", "fills_verify"}
    assert st["due_by_kind"]["fills_verify"] == 2
