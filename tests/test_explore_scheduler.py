"""tests/test_explore_scheduler.py — Task 3.1：ExploreScheduler（單 thread、逐 job、
分層更新、限流讓位、lease/fencing）。plan
docs/superpowers/plans/2026-09-20-explore-rate-limit-refactor.md Task 3.1 卡片。

全離線；HL 呼叫一律走假物件或 `HLGateway`＋假 `post_fn`（tests/test_hl_gateway_budget.py
的 fake clock 慣例），DB 一律 `tmp_path`。
"""
from __future__ import annotations

import threading

import pytest

from spark.publicapi.explore_scheduler import ExploreScheduler, _spread
from spark.publicapi.explore_store import ExploreStore, FillsSyncState
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
    與本檔的 weight-budget 測試分開覆蓋）。"""

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


# --- 1. 首 tick 只建 candidates job；第二 tick 跑 candidates 後每個地址四個 job ---

def test_bootstrap_then_candidates_creates_four_jobs_per_address(tmp_path):
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
        assert kinds == {"state", "portfolio", "ledger", "fills"}
    assert len(times) > 1  # 分散，不全等於 now


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

    # 初次到期時間用真實預設週期算 spread（900s／3600s），複製 candidates 輪產生
    # per-address job 時的實際行為——如果全部塞在 t=0，滑動視窗允許的「第一批
    # 免費 burst」會讓完成時間掉到理論下限（~21 分鐘）而非 spec §6 描述的
    # ≥22 分鐘（沒有初始 burst 時，需求到達率長期超過補充率，兩者共同作用才會
    # 逼近 22 分鐘，見 plan 卡片驗收數字）。
    # 位址尾碼用乘法雜湊打散（模擬真實地址近似均勻分布在 8 hex 碼空間），
    # 不用循序 i——循序 i 的 spread 幾乎等於 i 本身，前段地址會扎堆在同一小段
    # 時間內全部到期，退化成一次性 burst，量不出 §6 描述的穩態節流時間。
    addresses = [f"0x{'0' * 24}{(i * 2654435761) % (2**32):08x}" for i in range(300)]
    # is_active gate（Task 3.5 B(2)）要求 job 的地址真的是 active 候選，否則第一次
    # 抓完就被判為 "dropped" 不再續排——這裡先把 300 個地址登記成候選，複製真實
    # candidates job 跑過一輪後的狀態。
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

    store.enqueue("0xabc:state", "0xabc", "state", 0, clock.now())
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True

    r = sched.tick()
    assert r == "paused"
    assert calls == []


# --- 4. fills 三頁滿頁＋一短頁分 4 tick 完成，期間 priority 0 的 state job 有機會先跑 ---

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


def test_fills_multi_page_completes_over_four_ticks_and_state_gets_a_turn(tmp_path):
    clock = Clock(t=40 * 86400.0)  # window 起點在 epoch 之後，避免負時間戳
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
    store.enqueue("0xabc:fills", "0xabc", "fills", 2, clock.now())
    store.enqueue("0xdef:state", "0xdef", "state", 0, clock.now())

    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True

    results = [sched.tick() for _ in range(5)]
    assert results.count("ran:state") == 1
    assert results.count("ran:fills") == 4
    # state（priority 0）比 fills（priority 2）先跑
    assert results.index("ran:state") < results.index("ran:fills")

    sync = store.get_sync("0xabc")
    assert sync.completeness == "complete"
    assert sync.pages_done == 3
    # 相鄰兩頁 inclusive 重疊 1 筆（上一頁最後一筆＝下一頁第一筆，同 tid），
    # 3 個頁界共去重 3 筆：2000*3+500-3。
    assert len(store.get_fills("0xabc", 0, cursor4 + 500)) == PAGE_LIMIT * 3 + 500 - 3


# --- 5. 重啟：新 scheduler 接續 cursor，不從頭 ---

def test_restart_continues_fills_cursor_not_from_scratch(tmp_path):
    clock = Clock(t=40 * 86400.0)
    db_path = tmp_path / "explore.db"
    now_ms = int(clock.now() * 1000)
    window_start_ms = now_ms - 30 * 86_400_000

    store1 = ExploreStore(db_path, now_fn=clock.now)
    page1 = _fills_page(PAGE_LIMIT, window_start_ms)
    hl1 = SequencedFillsHL([page1])
    store1.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store1.enqueue("0xabc:fills", "0xabc", "fills", 2, clock.now())
    sched1 = _sched(store1, hl1, clock=clock)
    sched1._bootstrapped = True

    r1 = sched1.tick()
    assert r1 == "ran:fills"
    sync_before = store1.get_sync("0xabc")
    assert sync_before.pages_done == 1
    assert sync_before.completeness == "backfilling"

    # 重啟：新 store／scheduler 指向同一個 DB 檔。
    store2 = ExploreStore(db_path, now_fn=clock.now)
    cursor = sync_before.cursor_ms
    page2 = _fills_page(500, cursor)  # 短頁，結束本輪
    hl2 = SequencedFillsHL([page2])
    sched2 = _sched(store2, hl2, clock=clock)
    sched2._bootstrapped = True

    r2 = sched2.tick()
    assert r2 == "ran:fills"
    assert hl2.calls[0][1] == cursor  # 從上次游標續抓，不是從 window_start 重新開始
    sync_after = store2.get_sync("0xabc")
    assert sync_after.completeness == "complete"


# --- 6. 候選進出：移除的地址 active=0 但 cache/fills 保留；新地址只新增它的四個 job ---

def test_candidate_churn_preserves_removed_cache_and_adds_new_address_jobs(tmp_path):
    clock = Clock()
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)

    store.upsert_candidates([("0xaaa", "Alice", 1, 0.5), ("0xbbb", "Bob", 2, 0.4)],
                            as_of=clock.now())
    store.put_cache_ok("0xaaa", "clearinghouseState", {"x": 1}, fetched_at=clock.now(),
                       refresh_after=clock.now() + 900)
    store.insert_fills_page("0xaaa", [{"coin": "BTC", "tid": 1, "time": 100}],
                            FillsSyncState(
                                address="0xaaa", window_start_ms=0, window_end_ms=1000,
                                cursor_ms=1000, synced_through_ms=1000, observed_from_ms=100,
                                observed_to_ms=100, completeness="complete", reason=None,
                                pages_done=1, fills_in_window=1, updated_at=clock.now(),
                                last_error=None))
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

    # 0xccc 是新地址，只新增它的四個 job。
    ccc_kinds = {k for (k,) in store._db.execute(
        "SELECT kind FROM refresh_job WHERE address='0xccc'").fetchall()}
    assert ccc_kinds == {"state", "portfolio", "ledger", "fills"}


# --- 7. portfolio 拋例外 → quarantined，cache 落地 last_error、payload 仍 NULL ---

def test_portfolio_exception_quarantines_job(tmp_path):
    clock = Clock()
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    hl = FakeHL()
    hl.portfolio_exc = RuntimeError("bad shape")
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
    # 0xbbb 仍在池內，它原本的四個 job（本輪又補新的一批，key 相同只會更新時間）仍存在。
    assert store._db.execute(
        "SELECT COUNT(*) FROM refresh_job WHERE address='0xbbb'").fetchone()[0] == 4
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

    r = sched.tick()
    assert r == "dropped"
    assert store._db.execute(
        "SELECT COUNT(*) FROM refresh_job WHERE key='0xabc:state'").fetchone()[0] == 0


def test_dropped_address_fills_job_does_not_reenqueue(tmp_path):
    """同上，`_run_fills` 版本（`plan.is_noop` 分支）。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    store.upsert_candidates([("0xabc", "Alice", 1, 0.5)], as_of=clock.now())
    now_ms = int(clock.now() * 1000)
    window_start_ms = now_ms - 30 * 86_400_000
    store.insert_fills_page("0xabc", [], FillsSyncState(
        address="0xabc", window_start_ms=window_start_ms, window_end_ms=now_ms,
        cursor_ms=now_ms, synced_through_ms=now_ms, observed_from_ms=None,
        observed_to_ms=None, completeness="complete", reason=None,
        pages_done=1, fills_in_window=0, updated_at=clock.now(), last_error=None))
    store.deactivate_missing(set())  # 0xabc 退池
    store.enqueue("0xabc:fills", "0xabc", "fills", 2, clock.now())
    sched = _sched(store, SequencedFillsHL([]), clock=clock)
    sched._bootstrapped = True

    r = sched.tick()
    assert r == "dropped"
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


def test_admission_cap_uses_admission_counts_not_stale_len_seen(tmp_path):
    """B(6)：準入計數改用 `admission_counts()`（同一 lock 內兩個 COUNT，與
    `active_n = len(seen)` 語意等價但走共用口徑）。"""
    clock = Clock()
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    # 預先塞大量非候選 job，撐爆準入上限（ADMISSION_MULTIPLIER * active_n）。
    for i in range(100):
        store.enqueue(f"dummy:{i}", None, "dummy", 5, clock.now())

    payload = _payload(["0xAAA0000000000000000000000000000000AAA1"])
    sched = _sched(store, FakeHL(), leaderboard_source_fn=lambda: payload, clock=clock,
                  cfg=ExploreConfig(candidate_pool=1))
    sched._bootstrapped = True
    store.enqueue("candidates:candidates", None, "candidates", 0, clock.now())

    r = sched.tick()
    assert r == "ran:candidates"
    # 準入上限被撐爆（100 job > 5*1 active）→ 本輪不新增 per-address job。
    assert store._db.execute(
        "SELECT COUNT(*) FROM refresh_job WHERE kind != 'candidates' AND kind != 'dummy'"
    ).fetchone()[0] == 0


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


def test_fills_non_candidate_continues_paging_until_done_then_dropped(tmp_path):
    """B(2)（W1 修法）：非候選地址（例如詳情頁按需入列，未 `upsert_candidates`）
    的多頁 fills 不因中途檢查 `is_active` 而提早被判 `dropped`——只有整輪
    `res.done` 之後才查 `is_active` 決定要不要排下一輪。"""
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
    page4 = _fills_page(100, cursor4)  # 短頁，觸發 done

    hl = SequencedFillsHL([page1, page2, page3, page4])
    store.enqueue("0xabc:fills", "0xabc", "fills", 2, clock.now())
    # 刻意不 upsert_candidates("0xabc")：非候選地址（詳情頁入列）。

    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True

    results = [sched.tick() for _ in range(4)]
    assert results == ["ran:fills", "ran:fills", "ran:fills", "dropped"]

    sync = store.get_sync("0xabc")
    assert sync.pages_done == 3
    assert store._db.execute(
        "SELECT COUNT(*) FROM refresh_job WHERE key='0xabc:fills'").fetchone()[0] == 0


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
