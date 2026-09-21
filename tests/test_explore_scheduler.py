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


def test_fills_multi_page_completes_and_state_also_gets_a_turn(tmp_path):
    """Task 7.4b（2026-09-21 主線程裁決）：舊斷言釘住的是「state（priority 0）
    嚴格優先於 fills（priority 2）」——這正是本 task 用 fills 保留額度
    （`explore_fills`）取代嚴格優先級所要改變的行為（嚴格優先級曾讓 fills
    在正式機一小時內完全拿不到額度）。改為行為級斷言：fills 四頁在有限
    tick 內全部完成、且過程中 state job 至少執行一次一一兩個方向都不能被
    對方餓死（`_fills_available()` 無 limiter 時交替 120/0，見同檔案
    `test_fills_available_alternates_without_limiter_neither_side_starves`）。"""
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

    results: list[str] = []
    for _ in range(20):
        r = sched.tick()
        results.append(r)
        sync = store.get_sync("0xabc")
        if sync is not None and sync.completeness == "complete":
            break

    assert results.count("ran:fills") == 4
    assert results.count("ran:state") >= 1

    sync = store.get_sync("0xabc")
    assert sync.completeness == "complete"
    assert sync.pages_done == 3
    # 相鄰兩頁 inclusive 重疊 1 筆（上一頁最後一筆＝下一頁第一筆，同 tid），
    # 3 個頁界共去重 3 筆：2000*3+500-3。
    assert len(store.get_fills("0xabc", 0, cursor4 + 500)) == PAGE_LIMIT * 3 + 500 - 3


def test_fills_available_alternates_without_limiter_neither_side_starves(tmp_path):
    """裁決 2（2026-09-21）：`_fills_available()` 沒有 limiter 時不再恆回
    `FILLS_PAGE_WEIGHT`（那會讓有 fills 待處理的 tick 永遠選中 fills、反向
    餓死基礎類別）——改成每次呼叫交替 120/0。無 limiter、fills 與 state
    同時到期，4 個 tick 內兩類各至少領工 2 次。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    store.upsert_candidates(
        [("0xaaa", None, 1, None), ("0xbbb", None, 2, None), ("0xfff", None, 3, None)],
        as_of=clock.now())
    store.enqueue("0xaaa:state", "0xaaa", "state", 0, clock.now())
    store.enqueue("0xbbb:state", "0xbbb", "state", 0, clock.now())
    store.enqueue("0xfff:fills", "0xfff", "fills", 2, clock.now())

    now_ms = int(clock.now() * 1000)
    window_start_ms = now_ms - 30 * 86_400_000
    page1 = _fills_page(PAGE_LIMIT, window_start_ms)
    cursor2 = page1[-1]["time"]
    page2 = _fills_page(500, cursor2)  # 短頁，觸發 done

    hl = SequencedFillsHL([page1, page2])
    sched = _sched(store, hl, clock=clock)  # 不傳 hl_base/hl_fills：無 limiter
    sched._bootstrapped = True

    results = [sched.tick() for _ in range(4)]

    assert results.count("ran:state") >= 2
    assert results.count("ran:fills") >= 2


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
    """無 fills 待處理時，基礎類別（此測試用 portfolio）走父 scope `explore`，
    可用到超過 `explore_base`(180) 的上限。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    addresses = [f"0x{'0' * 24}{(i * 2654435761) % (2**32):08x}" for i in range(30)]
    store.upsert_candidates([(a, None, i + 1, None) for i, a in enumerate(addresses)],
                            as_of=clock.now())
    for a in addresses:
        store.enqueue(f"{a}:portfolio", a, "portfolio", 1, clock.now())
    # 刻意不排任何 fills job：due_count("fills", now) 永遠是 0。

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


def test_fills_starvation_reproduction_bounded_and_progressing(tmp_path):
    """使用者第 4 點的飢餓重現：300 地址、`state_every_s=1`（基礎永遠有積壓）、
    fake HL 全部成功、fills 每地址 3 滿頁＋1 短頁；驅動 fake clock 30 分鐘 →
    fills 頁數 >= 25、state 抓取在前後 15 分鐘都持續發生、任一 60 秒切片：
    explore 合計 <=300 且（fills 全程待處理）base <=180。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    addresses = [f"0x{'0' * 24}{(i * 2654435761) % (2**32):08x}" for i in range(300)]
    store.upsert_candidates([(a, None, i + 1, None) for i, a in enumerate(addresses)],
                            as_of=clock.now())
    for a in addresses:
        store.enqueue(f"{a}:state", a, "state", 0, clock.now())
        store.enqueue(f"{a}:fills", a, "fills", 2, clock.now())
    # 刻意不排 portfolio/ledger job（本次重現聚焦 state vs fills 的類別級飢餓；
    # portfolio/ledger 已由 Task 3.1 的權重預算測試覆蓋）。

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
    # Task 7.5 點 3：`fills_pages_total` 只計本輪實際分頁抓取，不含留存邊界
    # 探測（每輪 complete 收尾多打一次 `get_fills_page`，也會被 `pager` 算進
    # `fills_calls`，但探測不是「一頁分頁」，不計進這個計數器）——
    # `pager.fills_calls` 因此 >= `fills_pages_total`，不再嚴格相等。
    assert status["fills_pages_total"] <= pager.fills_calls
    assert status["last_fills_at"] is not None
    assert status["base_scope_in_use"] == "explore_base"  # 全程 fills 都待處理


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


# --- Task 7.5 點 3：留存邊界探測（正／負／例外三路徑） ---

class ProbeAwareHL:
    """先吐 `real_pages`（本輪實際分頁），吐完之後任何呼叫都視為「留存邊界
    探測」——回傳 `probe_result`（`list` 或 `Exception` 實例，後者會被 raise）。"""

    def __init__(self, real_pages: list[list[dict]], probe_result):
        self._real_pages = list(real_pages)
        self._probe_result = probe_result
        self.probe_calls: list[tuple] = []

    def get_fills_page(self, address, start_ms, end_ms):
        if self._real_pages:
            return self._real_pages.pop(0)
        self.probe_calls.append((address, start_ms, end_ms))
        if isinstance(self._probe_result, Exception):
            raise self._probe_result
        return self._probe_result


def _single_short_round(tmp_path, clock, probe_result):
    """單一短頁立刻 complete 的最小場景（`state=None` 起手，第一頁就短頁），
    供三個探測測試共用：一個 tick 內完成整輪＋探測。"""
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store.enqueue("0xabc:fills", "0xabc", "fills", 2, clock.now())
    hl = ProbeAwareHL(real_pages=[[]], probe_result=probe_result)
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True
    return store, hl, sched


def test_retention_boundary_probe_positive_upgrades_reason(tmp_path):
    clock = Clock(t=40 * 86400.0)
    store, hl, sched = _single_short_round(
        tmp_path, clock, probe_result=[{"coin": "BTC", "tid": 1, "time": 1}])

    r = sched.tick()
    assert r == "ran:fills"

    sync = store.get_sync("0xabc")
    assert sync.completeness == "complete"
    assert sync.reason == "retention_boundary_verified"
    assert len(hl.probe_calls) == 1
    addr, probe_start, probe_end = hl.probe_calls[0]
    assert addr == "0xabc"
    assert probe_end == sync.window_start_ms - 1
    assert probe_start == sync.window_start_ms - 86_400_000


def test_retention_boundary_probe_negative_keeps_threshold_reason(tmp_path):
    clock = Clock(t=40 * 86400.0)
    store, hl, sched = _single_short_round(tmp_path, clock, probe_result=[])

    r = sched.tick()
    assert r == "ran:fills"

    sync = store.get_sync("0xabc")
    assert sync.completeness == "complete"
    assert sync.reason == "count_below_retention_threshold"
    assert len(hl.probe_calls) == 1


def test_retention_boundary_probe_exception_does_not_fail_round(tmp_path):
    clock = Clock(t=40 * 86400.0)
    store, hl, sched = _single_short_round(
        tmp_path, clock, probe_result=ConnectionError("boom"))

    r = sched.tick()
    assert r == "ran:fills"          # 探測失敗不影響本輪已完成的事實

    sync = store.get_sync("0xabc")
    assert sync.completeness == "complete"
    assert sync.reason == "count_below_retention_threshold"  # 探測失敗不改 reason
    assert len(hl.probe_calls) == 1
