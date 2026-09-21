"""tests/test_explore_scheduler.py — Task 3.1：ExploreScheduler（單 thread、逐 job、
分層更新、限流讓位、lease/fencing）。plan
docs/superpowers/plans/2026-09-20-explore-rate-limit-refactor.md Task 3.1 卡片。

全離線；HL 呼叫一律走假物件或 `HLGateway`＋假 `post_fn`（tests/test_hl_gateway_budget.py
的 fake clock 慣例），DB 一律 `tmp_path`。
"""
from __future__ import annotations

import threading

import pytest

from spark.publicapi.explore_scheduler import FILLS_PAGE_WEIGHT, ExploreScheduler, _spread
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
    # Task 7.6 B5：探測回應要先通過 `validate_page(page, probe_start, probe_end)`
    # 才會升級 reason——這裡的 fill 時間必須落在探測窗內（`window_start_ms` 由
    # `plan_page` 首次開新輪算出＝`now_ms - WINDOW_DAYS*一天`），不能再用任意
    # 時間戳（舊版沒有驗證，任意值也會被誤判成「驗證成功」）。
    window_start_ms = int(clock.t * 1000) - 30 * 86_400_000
    probe_time = window_start_ms - 1  # 落在探測窗 [window_start-1天, window_start-1] 內
    store, hl, sched = _single_short_round(
        tmp_path, clock, probe_result=[{"coin": "BTC", "tid": 1, "time": probe_time}])

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


def test_retention_boundary_probe_negative_marks_probe_empty_reason(tmp_path):
    """Task 7.7 W3（正確性修正）：探測回空頁不再維持原 reason 不變——記為
    `count_below_retention_threshold_probe_empty`，讓探測條件天然排除它，
    每次全區間遍歷後至多探到有結論為止（見同檔案
    `test_probe_not_called_when_reason_already_probe_empty`）。"""
    clock = Clock(t=40 * 86400.0)
    store, hl, sched = _single_short_round(tmp_path, clock, probe_result=[])

    r = sched.tick()
    assert r == "ran:fills"

    sync = store.get_sync("0xabc")
    assert sync.completeness == "complete"
    assert sync.reason == "count_below_retention_threshold_probe_empty"
    assert len(hl.probe_calls) == 1
    assert sched.status()["probe"] == {
        "total": 1, "verified": 0, "empty": 1, "failed": 0, "deferred": 0, "deferred_total": 0, "dropped": 0,
    }


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


# --- Task 7.6 B：探測驗證、順序、計數器（複審 W1/W2/S2/S3 修法） ---

def test_probe_response_out_of_window_does_not_upgrade_and_counts_failed(tmp_path):
    """B5：探測回應必須通過 `validate_page`——時間落在探測窗外（例如上游回應
    格式跑掉、或誤回全部歷史成交）不得升級 reason，計入 `_probe_failed`。"""
    clock = Clock(t=40 * 86400.0)
    store, hl, sched = _single_short_round(
        tmp_path, clock, probe_result=[{"coin": "BTC", "tid": 1, "time": 1}])  # 遠在窗外

    r = sched.tick()
    assert r == "ran:fills"

    sync = store.get_sync("0xabc")
    assert sync.completeness == "complete"
    assert sync.reason == "count_below_retention_threshold"  # 不升級
    assert len(hl.probe_calls) == 1
    assert sched.status()["probe"] == {
        "total": 1, "verified": 0, "empty": 0, "failed": 1, "deferred": 0, "deferred_total": 0, "dropped": 0,
    }


def test_probe_not_called_when_address_dropped(tmp_path):
    """B4：順序改為 complete → is_active 檢查 → 探測——退池地址在 is_active
    檢查就回 "dropped"，不值得再花預算探測。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    # 刻意不呼叫 upsert_candidates：地址從一開始就不是 active 候選。
    store.enqueue("0xabc:fills", "0xabc", "fills", 2, clock.now())
    hl = ProbeAwareHL(real_pages=[[]], probe_result=[{"coin": "BTC", "tid": 1, "time": 1}])
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True

    r = sched.tick()
    assert r == "dropped"
    assert hl.probe_calls == []
    assert sched.status()["probe"]["total"] == 0


def test_probe_not_called_when_reason_already_verified(tmp_path):
    """B4：`REASON_RETENTION_BOUNDARY_VERIFIED` 跨輪存活（A2）——已經驗證過的
    地址不該每輪都重新探測，探測條件收緊為 reason 仍是門檻推論時才探。"""
    from spark.publicapi.explore_store import FillsSyncState

    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    now_ms = int(clock.now() * 1000)
    window_start_ms = now_ms - 30 * 86_400_000
    checkpoint = FillsSyncState(
        address="0xabc", window_start_ms=window_start_ms, window_end_ms=now_ms,
        cursor_ms=now_ms, synced_through_ms=now_ms, observed_from_ms=None,
        observed_to_ms=None, completeness="complete",
        reason="retention_boundary_verified", pages_done=0, fills_in_window=0,
        updated_at=clock.now(), last_error=None, params_fp="",
    )
    store.insert_fills_page("0xabc", [], checkpoint)
    store.enqueue("0xabc:fills", "0xabc", "fills", 2, clock.now())
    hl = ProbeAwareHL(real_pages=[[]], probe_result=[{"coin": "BTC", "tid": 1, "time": 1}])
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True
    clock.t += 5 * 3600  # 過寬限期，開新增量輪

    r = sched.tick()
    assert r == "ran:fills"
    assert hl.probe_calls == []          # 已 verified，不再探
    sync = store.get_sync("0xabc")
    assert sync.reason == "retention_boundary_verified"   # 維持
    assert sched.status()["probe"]["total"] == 0


def test_probe_not_called_when_reason_already_probe_empty(tmp_path):
    """Task 7.7 W3：`REASON_PROBE_NO_EARLIER_FILLS` 同樣跨輪存活——已經探過、
    確認沒有更早成交的地址，下一輪增量收尾也不該再探一次（探測條件收緊為
    reason 仍是門檻推論本身，這個 reason 值自然被排除）。"""
    from spark.publicapi.explore_store import FillsSyncState

    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    now_ms = int(clock.now() * 1000)
    window_start_ms = now_ms - 30 * 86_400_000
    checkpoint = FillsSyncState(
        address="0xabc", window_start_ms=window_start_ms, window_end_ms=now_ms,
        cursor_ms=now_ms, synced_through_ms=now_ms, observed_from_ms=None,
        observed_to_ms=None, completeness="complete",
        reason="count_below_retention_threshold_probe_empty", pages_done=0,
        fills_in_window=0, updated_at=clock.now(), last_error=None, params_fp="",
    )
    store.insert_fills_page("0xabc", [], checkpoint)
    store.enqueue("0xabc:fills", "0xabc", "fills", 2, clock.now())
    hl = ProbeAwareHL(real_pages=[[]], probe_result=[{"coin": "BTC", "tid": 1, "time": 1}])
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True
    clock.t += 5 * 3600  # 過寬限期，開新增量輪

    r = sched.tick()
    assert r == "ran:fills"
    assert hl.probe_calls == []
    sync = store.get_sync("0xabc")
    assert sync.reason == "count_below_retention_threshold_probe_empty"   # 維持
    assert sched.status()["probe"]["total"] == 0


def test_probe_verified_does_not_change_updated_at(tmp_path):
    """B6/B7：探測命中只補強 reason，不得讓 `updated_at`（對外＝
    `last_success_at`）看起來像剛抓過一頁——store 用一個與 scheduler 時鐘脫鉤
    的 `now_fn`，若 `set_sync_reason` 誤用自己的鐘改 `updated_at` 會立刻露餡。"""
    clock = Clock(t=40 * 86400.0)
    window_start_ms = int(clock.t * 1000) - 30 * 86_400_000
    probe_time = window_start_ms - 1  # 落在探測窗內
    store = ExploreStore(tmp_path / "explore.db", now_fn=lambda: 999_999.0)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store.enqueue("0xabc:fills", "0xabc", "fills", 2, clock.now())
    hl = ProbeAwareHL(real_pages=[[]], probe_result=[{"coin": "BTC", "tid": 1, "time": probe_time}])
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True

    r = sched.tick()
    assert r == "ran:fills"

    sync = store.get_sync("0xabc")
    assert sync.reason == "retention_boundary_verified"
    assert sync.updated_at == clock.now()   # 抓頁完成當下寫入的值，不是 store 自己的鐘
    assert sched.status()["probe"] == {
        "total": 1, "verified": 1, "empty": 0, "failed": 0, "deferred": 0, "deferred_total": 0, "dropped": 0,
    }


# --- Task 7.7 點 8：探測在 explore_fills 保留額度下的延後佇列 ---

class ControllableLimiter:
    """`available(scope)` 的回傳值由測試直接控制（不模擬真實滑動視窗帳本，
    只用來讓 `_fills_available()` 讀到可控的可用額度）。"""

    def __init__(self, value: int = 0):
        self.value = value

    def available(self, scope: str) -> int:
        return self.value


class ProbeAwareHLWithLimiter:
    """`ProbeAwareHL` 加上 `_limiter`／`_scope`，讓 `_fills_available()` 走
    `getattr(self._hl_fills, "_limiter", None)` 那條真實 limiter 路徑，而不是
    無 limiter 時的交替 fallback。"""

    def __init__(self, real_pages: list[list[dict]], probe_result, limiter,
                scope: str = "explore_fills"):
        self._real_pages = list(real_pages)
        self._probe_result = probe_result
        self._limiter = limiter
        self._scope = scope
        self.probe_calls: list[tuple] = []

    def get_fills_page(self, address, start_ms, end_ms):
        if self._real_pages:
            return self._real_pages.pop(0)
        self.probe_calls.append((address, start_ms, end_ms))
        if isinstance(self._probe_result, Exception):
            raise self._probe_result
        return self._probe_result


def test_probe_deferred_when_budget_insufficient_then_drained_next_tick(tmp_path, caplog):
    """點 8：`_fills_available()` 回 0（不足一整頁 120）→ 探測進 deferred、無
    上游呼叫、無 warning、不計入 `_probe_total`；下一 tick 額度回來（回
    `FILLS_PAGE_WEIGHT`）→ `_tick_once` 領工前先補打，探測送出、佇列清空。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store.enqueue("0xabc:fills", "0xabc", "fills", 2, clock.now())

    window_start_ms = int(clock.t * 1000) - 30 * 86_400_000
    probe_time = window_start_ms - 1  # 落在探測窗內，補打成功時應驗證升級
    limiter = ControllableLimiter(value=0)
    hl_fills = ProbeAwareHLWithLimiter(
        real_pages=[[]], probe_result=[{"coin": "BTC", "tid": 1, "time": probe_time}],
        limiter=limiter)
    sched = _sched(store, hl_fills, hl_fills=hl_fills, clock=clock)
    sched._bootstrapped = True

    with caplog.at_level("WARNING"):
        r = sched.tick()
    assert r == "ran:fills"
    assert hl_fills.probe_calls == []   # 額度不足，沒有真的發送探測
    assert sched.status()["probe"] == {
        "total": 0, "verified": 0, "empty": 0, "failed": 0, "deferred": 1, "deferred_total": 1, "dropped": 0,
    }
    assert not any("探測" in rec.message for rec in caplog.records)   # 不 log

    sync = store.get_sync("0xabc")
    assert sync.reason == "count_below_retention_threshold"   # 尚未探測，維持原值

    limiter.value = FILLS_PAGE_WEIGHT   # 下一 tick 額度回來
    r2 = sched.tick()
    assert r2 == "idle"   # 沒有其他到期 job；drain 補打探測後正常領工拿不到工作
    assert len(hl_fills.probe_calls) == 1
    assert sched.status()["probe"]["deferred"] == 0
    assert sched.status()["probe"]["deferred_total"] == 1   # 只進過一次佇列
    assert sched.status()["probe"]["verified"] == 1

    sync2 = store.get_sync("0xabc")
    assert sync2.reason == "retention_boundary_verified"


def test_probe_call_raising_budget_exhausted_is_deferred_not_failed(tmp_path):
    """點 8 第 3 條（例外分類）：pre-check 通過但實際呼叫本身拋
    `BudgetExhausted`（pre-check 與實際發送之間的競態，單 thread scheduler
    理論上不會發生，但要防禦）→ 視同額度不足進 deferred，不計入
    `_probe_total`／`_probe_failed`。"""
    from spark.publicapi.hl_budget import BudgetExhausted

    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store.enqueue("0xabc:fills", "0xabc", "fills", 2, clock.now())
    limiter = ControllableLimiter(value=FILLS_PAGE_WEIGHT)   # pre-check 通過
    hl_fills = ProbeAwareHLWithLimiter(
        real_pages=[[]], probe_result=BudgetExhausted("boom"), limiter=limiter)
    sched = _sched(store, hl_fills, hl_fills=hl_fills, clock=clock)
    sched._bootstrapped = True

    r = sched.tick()
    assert r == "ran:fills"
    assert len(hl_fills.probe_calls) == 1   # 有實際打，但拋例外
    assert sched.status()["probe"] == {
        "total": 0, "verified": 0, "empty": 0, "failed": 0, "deferred": 1, "deferred_total": 1, "dropped": 0,
    }


def test_enqueue_probe_deferred_dedupes_same_address(tmp_path):
    """點 8：同地址進兩次 deferred queue 只留一筆（新覆舊），`deferred_total`
    （累計進過佇列的次數）照樣遞增兩次——對應「同地址兩次 complete 只留一筆」
    的驗收要求。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    sched = _sched(store, FakeHL(), clock=clock)

    sched._enqueue_probe_deferred("0xabc", 100)
    sched._enqueue_probe_deferred("0xabc", 200)

    assert list(sched._probe_deferred) == [("0xabc", 200)]
    assert sched.status()["probe"]["deferred"] == 1
    assert sched.status()["probe"]["deferred_total"] == 2


def test_drain_deferred_probe_discards_when_address_dropped(tmp_path):
    """點 8：補打前重讀狀態——地址已退池（`is_active` 為 False）→ 丟棄，不
    探測、不重排回佇列。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    window_start_ms = now_ms - 30 * 86_400_000
    store.insert_fills_page("0xabc", [], FillsSyncState(
        address="0xabc", window_start_ms=window_start_ms, window_end_ms=now_ms,
        cursor_ms=now_ms, synced_through_ms=now_ms, observed_from_ms=None,
        observed_to_ms=None, completeness="complete",
        reason="count_below_retention_threshold", pages_done=0, fills_in_window=0,
        updated_at=clock.now(), last_error=None))
    # 刻意不 upsert_candidates：地址不是 active 候選。
    hl = ProbeAwareHL(real_pages=[], probe_result=[{"coin": "BTC", "tid": 1, "time": 1}])
    sched = _sched(store, hl, clock=clock)
    sched._enqueue_probe_deferred("0xabc", window_start_ms)

    sched._drain_one_deferred_probe(clock.now())

    assert hl.probe_calls == []
    assert sched.status()["probe"]["deferred"] == 0
    assert sched.status()["probe"]["deferred_total"] == 1


def test_drain_deferred_probe_discards_when_reason_changed(tmp_path):
    """點 8：補打前重讀狀態——reason 已經不是門檻推論（例如已經探到有結論
    或降級成別的狀態）→ 這筆延後探測已經沒有意義，丟棄。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    now_ms = int(clock.now() * 1000)
    window_start_ms = now_ms - 30 * 86_400_000
    store.insert_fills_page("0xabc", [], FillsSyncState(
        address="0xabc", window_start_ms=window_start_ms, window_end_ms=now_ms,
        cursor_ms=now_ms, synced_through_ms=now_ms, observed_from_ms=None,
        observed_to_ms=None, completeness="complete",
        reason="retention_boundary_verified", pages_done=0, fills_in_window=0,
        updated_at=clock.now(), last_error=None))
    hl = ProbeAwareHL(real_pages=[], probe_result=[{"coin": "BTC", "tid": 1, "time": 1}])
    sched = _sched(store, hl, clock=clock)
    sched._enqueue_probe_deferred("0xabc", window_start_ms)

    sched._drain_one_deferred_probe(clock.now())

    assert hl.probe_calls == []
    assert sched.status()["probe"]["deferred"] == 0


def test_probe_write_failure_counts_as_failed_not_verified(tmp_path):
    """S4：`set_sync_reason` 拋例外（store 寫入失敗）→ 不逸出，記警告、計入
    `_probe_failed`，不算 verified／empty。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store.enqueue("0xabc:fills", "0xabc", "fills", 2, clock.now())
    window_start_ms = int(clock.t * 1000) - 30 * 86_400_000
    probe_time = window_start_ms - 1
    hl = ProbeAwareHL(real_pages=[[]], probe_result=[{"coin": "BTC", "tid": 1, "time": probe_time}])
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True

    def boom(*a, **kw):
        raise RuntimeError("disk full")
    store.set_sync_reason = boom

    r = sched.tick()
    assert r == "ran:fills"
    assert sched.status()["probe"] == {
        "total": 1, "verified": 0, "empty": 0, "failed": 1, "deferred": 0, "deferred_total": 0, "dropped": 0,
    }
    sync = store.get_sync("0xabc")
    assert sync.reason == "count_below_retention_threshold"   # 落地失敗，reason 未變


# --- Task 7.8 Critical：noop 重排時間與 noop 期限同源 ---

def test_run_fills_partial_noop_reschedules_using_plan_next_due_ms(tmp_path):
    """S4 (i)：partial 地址 noop 收尾後，`refresh_job.next_attempt_at` 必須
    等於 `plan.next_due_ms`（`window_end_ms + PARTIAL_RESCAN_AFTER_MS`，24
    小時），且必須晚於 `now`——這正是 7.7 之後餓死其他 job 的根因：舊版用
    `window_end + fills_every_s`（4h）重排，4 小時一到就把 job 排到過去。"""
    from spark.publicapi.explore_fills_sync import PARTIAL_RESCAN_AFTER_MS

    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    addr = "0xabc"
    store.upsert_candidates([(addr, None, 1, None)], as_of=clock.now())
    now_ms = int(clock.now() * 1000)
    window_end_ms = now_ms - 5 * 3600 * 1000  # 5 小時前：已過舊 4h 門檻，未過 24h
    window_start_ms = window_end_ms - 30 * 86_400_000
    store.insert_fills_page(addr, [], FillsSyncState(
        address=addr, window_start_ms=window_start_ms, window_end_ms=window_end_ms,
        cursor_ms=window_end_ms, synced_through_ms=window_end_ms, observed_from_ms=None,
        observed_to_ms=None, completeness="partial", reason="retention_limit",
        pages_done=0, fills_in_window=0, updated_at=clock.now(), last_error=None))
    store.enqueue(f"{addr}:fills", addr, "fills", 2, clock.now())
    sched = _sched(store, FakeHL(), clock=clock)
    sched._bootstrapped = True

    r = sched.tick()

    assert r == "ran:fills"
    row = store._db.execute(
        "select next_attempt_at from refresh_job where key=?", (f"{addr}:fills",)
    ).fetchone()
    expected = (window_end_ms + PARTIAL_RESCAN_AFTER_MS) / 1000
    assert row[0] == expected
    assert row[0] > clock.now()


def test_run_fills_complete_noop_reschedule_unchanged_at_four_hours(tmp_path):
    """S4 (ii)：`complete` 地址 noop 收尾行為不變——仍等於
    `window_end_ms + 4h`（`plan_page` 預設的 `incremental_after_ms`），只是
    現在的值來源改成 `plan.next_due_ms`，不再是排程端自己算的常數。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    addr = "0xabc"
    store.upsert_candidates([(addr, None, 1, None)], as_of=clock.now())
    now_ms = int(clock.now() * 1000)
    window_end_ms = now_ms - 3600 * 1000  # 1 小時前，未過 4h 增量寬限期
    window_start_ms = window_end_ms - 30 * 86_400_000
    store.insert_fills_page(addr, [], FillsSyncState(
        address=addr, window_start_ms=window_start_ms, window_end_ms=window_end_ms,
        cursor_ms=window_end_ms, synced_through_ms=window_end_ms, observed_from_ms=None,
        observed_to_ms=None, completeness="complete",
        reason="count_below_retention_threshold", pages_done=0, fills_in_window=0,
        updated_at=clock.now(), last_error=None))
    store.enqueue(f"{addr}:fills", addr, "fills", 2, clock.now())
    sched = _sched(store, FakeHL(), clock=clock)
    sched._bootstrapped = True

    r = sched.tick()

    assert r == "ran:fills"
    row = store._db.execute(
        "select next_attempt_at from refresh_job where key=?", (f"{addr}:fills",)
    ).fetchone()
    expected = (window_end_ms + 4 * 3600 * 1000) / 1000
    assert row[0] == expected


def test_run_fills_partial_noop_does_not_starve_other_jobs_over_50_ticks(tmp_path):
    """S4 (iii)：複審重現腳本
    `scratchpad/repro_partial_starves_base.py` 的形狀，寫成正式回歸測試——
    一個 partial 地址（noop 視窗在 4h~24h 之間）＋兩個各自只有一個 state job
    的地址，連跑 50 tick：`ran:fills` 至多出現一次（noop 重排後 24 小時內不
    會再到期），兩個 state job 都要被領過，其餘 tick 只能是 `idle` 或
    `ran:state`——修前 `ran:fills` 會因為排到過去的時刻而佔滿全部 50 tick，
    兩個 state job 一次都領不到。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    partial_addr = "0xaaa0000000000000000000000000000000aaa1"
    state_addr_1 = "0xbbb0000000000000000000000000000000bbb2"
    state_addr_2 = "0xccc0000000000000000000000000000000ccc3"
    store.upsert_candidates(
        [(partial_addr, "a", 1, None), (state_addr_1, "b", 2, None),
         (state_addr_2, "c", 3, None)], as_of=clock.now())

    now_ms = int(clock.now() * 1000)
    window_end_ms = now_ms - 5 * 3600 * 1000  # 4h~24h 之間，noop 但未達重掃期限
    window_start_ms = window_end_ms - 30 * 86_400_000
    store.insert_fills_page(partial_addr, [], FillsSyncState(
        address=partial_addr, window_start_ms=window_start_ms, window_end_ms=window_end_ms,
        cursor_ms=window_end_ms, synced_through_ms=window_end_ms, observed_from_ms=None,
        observed_to_ms=None, completeness="partial", reason="retention_limit",
        pages_done=0, fills_in_window=0, updated_at=clock.now(), last_error=None))
    store.enqueue(f"{partial_addr}:fills", partial_addr, "fills", 2, clock.now())
    store.enqueue(f"{state_addr_1}:state", state_addr_1, "state", 0, clock.now())
    store.enqueue(f"{state_addr_2}:state", state_addr_2, "state", 0, clock.now())

    hl = FakeHL()
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True
    sched._first_tick_done = True   # 跳過首 tick 的 overdue 重排，避免干擾到期時間

    results: dict[str, int] = {}
    for _ in range(50):
        r = sched.tick()
        results[r] = results.get(r, 0) + 1

    assert results.get("ran:fills", 0) <= 1
    assert ("state", state_addr_1) in hl.calls
    assert ("state", state_addr_2) in hl.calls
    assert set(results) <= {"ran:fills", "ran:state", "idle"}


# --- Task 7.8 W3／S1：deferred probe 溢位可觀測、drain 時過期視窗被丟棄 ---

def test_enqueue_probe_deferred_overflow_increments_dropped_and_logs(tmp_path, caplog):
    """W3：deque 帶 `maxlen`，append 到滿的佇列會讓最舊項被靜默擠掉——加
    `_probe_dropped` 計數器與 warning 讓這個溢位可觀測。"""
    from spark.publicapi.explore_scheduler import _PROBE_DEFERRED_MAXLEN

    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    sched = _sched(store, FakeHL(), clock=clock)

    for i in range(_PROBE_DEFERRED_MAXLEN):
        sched._enqueue_probe_deferred(f"0xfill{i:04d}", 100)
    assert sched.status()["probe"]["dropped"] == 0
    assert len(sched._probe_deferred) == _PROBE_DEFERRED_MAXLEN

    with caplog.at_level("WARNING"):
        sched._enqueue_probe_deferred("0xoverflow", 200)

    assert sched.status()["probe"]["dropped"] == 1
    assert len(sched._probe_deferred) == _PROBE_DEFERRED_MAXLEN   # 仍是 maxlen，沒有無界增長
    assert list(sched._probe_deferred)[-1] == ("0xoverflow", 200)
    assert any("佇列已滿" in rec.message for rec in caplog.records)


def test_drain_deferred_probe_discards_stale_window_and_counts_dropped(tmp_path):
    """S1：drain 前重讀 `get_sync`——`reason` 跨輪存活，不足以判斷佇列裡記的
    視窗是不是目前最新那一輪；`window_start_ms` 對不上時代表期間又跑過一輪
    新的增量／重掃，這筆延後探測驗證的邊界已經過期，丟棄、不探測，計入
    `dropped`（不算 `failed`）。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    now_ms = int(clock.now() * 1000)
    old_window_start_ms = now_ms - 30 * 86_400_000
    store.insert_fills_page("0xabc", [], FillsSyncState(
        address="0xabc", window_start_ms=old_window_start_ms, window_end_ms=now_ms,
        cursor_ms=now_ms, synced_through_ms=now_ms, observed_from_ms=None,
        observed_to_ms=None, completeness="complete",
        reason="count_below_retention_threshold", pages_done=0, fills_in_window=0,
        updated_at=clock.now(), last_error=None))
    hl = ProbeAwareHL(real_pages=[], probe_result=[{"coin": "BTC", "tid": 1, "time": 1}])
    sched = _sched(store, hl, clock=clock)
    sched._enqueue_probe_deferred("0xabc", old_window_start_ms)

    # 期間又跑了一輪增量：window_start_ms 前移，reason 維持不變（跨輪存活）。
    new_window_start_ms = old_window_start_ms + 3600_000
    store.insert_fills_page("0xabc", [], FillsSyncState(
        address="0xabc", window_start_ms=new_window_start_ms, window_end_ms=now_ms,
        cursor_ms=now_ms, synced_through_ms=now_ms, observed_from_ms=None,
        observed_to_ms=None, completeness="complete",
        reason="count_below_retention_threshold", pages_done=0, fills_in_window=0,
        updated_at=clock.now(), last_error=None))

    sched._drain_one_deferred_probe(clock.now())

    assert hl.probe_calls == []
    assert sched.status()["probe"]["deferred"] == 0
    assert sched.status()["probe"]["dropped"] == 1
    assert sched.status()["probe"]["failed"] == 0


# ============================================================
# Task 7.9a A1：fills 週期單一來源——scheduler 重排間隔與
# `explore_fills_sync.plan_page` 的增量寬限期必須讀同一個 `fills_every_s`。
# ============================================================

@pytest.mark.parametrize("period", [7200, 21600])
def test_fills_period_single_source_ties_reschedule_and_plan_page(tmp_path, period):
    """完成首輪回補後：(i) 重排間隔 ≈ period；(ii) `period − 1s` 仍是 noop
    （不打上游）；(iii) `period + 1s` 開新的增量輪（打上游）。三者都隨同一個
    `fills_every_s` 值變化，不是各自一份常數。"""
    clock = Clock(t=40 * 86400.0)  # window 起點在 epoch 之後，避免負時間戳
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    addr = "0xabc"
    store.upsert_candidates([(addr, None, 1, None)], as_of=clock.now())
    store.enqueue(f"{addr}:fills", addr, "fills", 2, clock.now())
    # 三頁：首輪回補（空頁即完成）、留存邊界探測回應（空頁）、下一輪增量（空頁）
    # ——首輪與增量輪收尾都會判定 completeness=complete／
    # reason=count_below_retention_threshold，觸發一次探測（見
    # `_run_fills`），本測試不驗證探測本身，只需要提供足夠的假頁。
    hl = SequencedFillsHL([[], [], []])
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


def test_notify_dirty_exception_during_probe_does_not_lose_fills_enqueue(tmp_path):
    """`_probe_retention_boundary` 內部的 dirty 通知也吞例外——探測後續的
    `enqueue` 一定會執行到（見 `_run_fills` 的呼叫順序：`_complete` → 探測
    → enqueue → `_notify_dirty()`）。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    addr = "0xabc"
    store.upsert_candidates([(addr, None, 1, None)], as_of=clock.now())
    store.enqueue(f"{addr}:fills", addr, "fills", 2, clock.now())
    hl = SequencedFillsHL([[], []])  # 首輪回補（空頁）＋探測回應（空頁）

    def boom():
        raise RuntimeError("dirty boom")

    sched = _sched(store, hl, clock=clock, on_dirty=boom)
    sched._bootstrapped = True

    r = sched.tick()

    assert r == "ran:fills"
    row = store._db.execute(
        "select next_attempt_at from refresh_job where key=?", (f"{addr}:fills",)).fetchone()
    assert row is not None and row[0] > clock.now()
    assert sched.status()["dirty_errors"] == 2   # 一次來自抓頁完成、一次來自探測回寫
