"""tests/test_explore_scheduler.py — Task 3.1／7.9b：ExploreScheduler（單 thread、
逐 job、分層更新、限流讓位、lease/fencing、遍歷軌／增量軌分離、探測 9:1）。plan
docs/superpowers/plans/2026-09-20-explore-rate-limit-refactor.md Task 3.1／7.9b 卡片。

全離線；HL 呼叫一律走假物件或 `HLGateway`＋假 `post_fn`（tests/test_hl_gateway_budget.py
的 fake clock 慣例），DB 一律 `tmp_path`。
"""
from __future__ import annotations

import bisect
import random
import re
import threading
from collections import Counter
from pathlib import Path

import pytest

from spark.publicapi.explore_fills_sync import (MAX_PERIOD_S, MIN_PERIOD_S, fills_period_s,
                                                fresh_scan_window)
from spark.publicapi.explore_publisher import compose_rows
from spark.publicapi.explore_scheduler import SPECIAL_SERVE_RATIO, ExploreScheduler, _spread
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


class _ProbeResolvesThenEmptyHL:
    """Task 3：與 `FakeHL` 同樣「真正的分頁窗立刻空頁收尾」，但探測窗
    （span 明顯短於 30 天遍歷窗）回一筆成交——讓左界證據探測前置一次解出
    `earlier_fills_seen`，不必每次都用 monkeypatch 頂替
    `ExploreStore.get_left_boundary`（那樣會繞過本模組真正要驗證的探測
    機制，讓 DB 裡的 `left_boundary` 欄位永遠停在預設值 `unknown`，使該地址
    變成永久的探測候選——見 `test_s7a_...` 的教訓）。用 span 而非固定窗口
    值判斷，因為呼叫端的 `window_start_ms` 隨測試的虛擬時間變動。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def get_fills_page(self, address, start_ms, end_ms):
        self.calls.append(("fills", address, start_ms, end_ms))
        if end_ms - start_ms <= 2 * 86_400_000:
            return [{"coin": "BTC", "tid": 1, "time": start_ms}]
        return []

    def clearinghouse_state(self, address):
        return {"marginSummary": {"accountValue": "1"}}

    def portfolio(self, address):
        return [["day", {}]]

    def non_funding_ledger_updates(self, address, start_ms):
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


def _set_fills_rate(store, addr: str, *, window_hours: float, fills_in_window: int) -> None:
    """Task 5 測試工具：直接改寫 `fills_sync.window_start_ms`／`fills_in_window`，
    模擬「這個位址已經觀測到某個近期成交速率」。`bootstrap_address_fills` 剛
    建立時 `window_start_ms == window_end_ms`（零寬度）——`fills_period_s_for`
    在這個狀態下視為「無速率資料」（見該方法），真實速率要等增量軌真的跑過
    幾輪、`window_end_ms` 被 `plan_incremental` 往前推才會自然出現；這裡跳過
    那個過程直接寫值，只保留 `window_end_ms`（bootstrap 當下的 now）不動，
    只往回改 `window_start_ms` 拉出寬度。"""
    row = store._db.execute(
        "SELECT window_end_ms FROM fills_sync WHERE address=?", (addr,)).fetchone()
    window_end_ms = row[0]
    window_start_ms = window_end_ms - int(window_hours * 3_600_000)
    store._db.execute(
        "UPDATE fills_sync SET window_start_ms=?, fills_in_window=? WHERE address=?",
        (window_start_ms, fills_in_window, addr))


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
                  ledger_every_s=10**9,
                  # Task 5：min==max 釘死週期為固定值（不論位址名次／速率），
                  # 與改版前 `fills_every_s=10**9` 的隔離效果等價。
                  fills_min_period_s=10**9, fills_max_period_s=10**9, rng=lambda: 0.0)
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
    # Task 3：左界證據先解出——`SequencedFillsHL` 的回應佇列是給這 4 頁真正
    # 的分頁用的，探測前置若也從隊列裡拿一筆會少一頁、湊不齊 4 次
    # `ran:fills_scan`；這個測試要驗的是多頁續抓本身，與左界證據怎麼來無關。
    store.set_left_boundary("0xabc", "no_earlier_activity", window_start_ms, clock.now())
    store.enqueue("0xabc:fills_scan", "0xabc", "fills_scan", 2, clock.now())
    store.enqueue("0xdef:state", "0xdef", "state", 0, clock.now())

    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True

    results: list[str] = []
    for _ in range(20):
        r = sched.tick()
        results.append(r)
        sync = store.get_sync("0xabc")
        if sync is not None and sync.completeness in ("partial", "complete"):
            break

    assert results.count("ran:fills_scan") == 4
    assert results.count("ran:state") >= 1

    sync = store.get_sync("0xabc")
    assert sync.completeness == "complete"
    assert sync.reason == "left_boundary_no_activity"
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
    # Task 3：左界證據先解出——這個測試驗的是重啟後游標從哪裡續抓，與左界
    # 證據怎麼來無關，先解出讓第一個 tick 直接打到 `hl1` 唯一那頁。
    store1.set_left_boundary("0xabc", "no_earlier_activity", window_start_ms, clock.now())
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
    # Task 3：左界證據已在 store1 階段解出（正面證據對滾動窗口單調有效，
    # 重啟＋續抓不需要重探），短頁抵達終點後合成為 complete。
    assert sync_after.completeness == "complete"
    assert sync_after.reason == "left_boundary_no_activity"


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


def test_fills_period_s_for_no_rate_data_is_default_min_period_constant(tmp_path):
    """Task 5（2026-09-22，D-B／D-H，主線程裁決改寫，原
    `test_fills_every_s_default_is_default_fills_period_s_constant`）：增量週期
    不再是單一建構子常數——`ExploreScheduler` 不傳 `fills_min_period_s` 時，
    對一個沒有 `fills_sync` 資料（速率未知）也沒有 candidates 名次的位址，
    `fills_period_s_for` 走保守值 `explore_fills_sync.MIN_PERIOD_S`（單一來源
    常數），不是排程端另外寫死的字面值。"""
    clock = Clock()
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    sched = _sched(store, FakeHL(), clock=clock)
    assert sched.fills_period_s_for("0xAAA0000000000000000000000000000000AAA1") == MIN_PERIOD_S


# ============================================================
# Task 5（2026-09-22，D-B／D-H）：`explore_fills_sync.fills_period_s` 的純函式
# 契約（plan Task 5 Step 1）＋排程端到期判斷與重排同源（7.8 教訓）。
# ============================================================

def test_period_keeps_expected_fills_within_one_page():
    """核心不變式：任何地址的預期單週期成交筆數 ≤ PAGE_LIMIT。`rank=None`
    （名次未知）刻意不套用下界，讓這個不變式在沒有名次可仰賴時仍然成立——見
    `fills_period_s` docstring。"""
    for fph in (1, 50, 500, 2_000, 20_000):
        p = fills_period_s(fills_per_hour=fph, rank=None)
        assert fph * (p / 3600) <= PAGE_LIMIT


def test_high_frequency_cold_address_is_not_blanket_24h():
    """D-H：名次在 50 名外但高頻的地址，不得一律延長到 24h；速率低則夾到上界。"""
    assert fills_period_s(fills_per_hour=600, rank=200) == MIN_PERIOD_S
    assert fills_period_s(fills_per_hour=2, rank=200) == MAX_PERIOD_S


def test_hot_rank_never_exceeds_min_period():
    """前 50 名的新鮮度需求優先於速率估計——即使速率很低也不得延長週期。"""
    assert fills_period_s(fills_per_hour=0.1, rank=1) == MIN_PERIOD_S


def test_unknown_rate_is_conservative():
    """沒有速率資料時（地址剛入池，尚無 `fills_sync` 觀測）取保守值：不確定就
    抓密一點，不論名次。"""
    assert fills_period_s(fills_per_hour=None, rank=200) == MIN_PERIOD_S


# ============================================================
# Task 5b（2026-09-22，主線程裁決：Task 5 完成後複查發現原版分母錯誤）：速率
# 分母改成「當前生效那次遍歷的觀測跨度」，否則對回補中位址嚴重低估速率
# （正式機實測 0xa483470a… 真實 897.5 筆/小時被算成 13.4，差 67 倍）。
# ============================================================

T0 = 40 * 86_400_000  # ms epoch，避免負時間戳（沿用其他測試 t=40*86400 的慣例）
DAY = 86_400_000  # ms
ADDR = "0xddd1"


def _scheduler_with_scan(tmp_path, addr, *, fills_in_window, observed_from_ms, observed_to_ms,
                         window_start_ms=None, window_end_ms=None, status="running", rank=None):
    """Task 5b 測試工具：直接寫入一筆 `fills_scan`（`status` 為 `'running'` 或
    `'done'`）並鏡射到 `fills_sync` 的名目窗口，模擬「這個位址目前正在回補」
    或「剛完成一次遍歷」時的觀測資料，不必真的跑一輪真實遍歷。
    `window_start_ms`／`window_end_ms` 省略時預設**零寬度**（等於 `T0`）——
    對映真實 `bootstrap_address_fills` 剛建立、尚無任何窗口資料的預設狀態
    （見 `test_no_data_at_all_stays_conservative`：省略窗口就是要驗證『真的
    什麼都不知道』的情形，不能悄悄退回一個看起來合理的 30 天窗口）。"""
    clock = Clock(t=T0 / 1000)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    store.upsert_candidates([(addr, None, rank, None)], as_of=clock.now())
    w_start = window_start_ms if window_start_ms is not None else T0
    w_end = window_end_ms if window_end_ms is not None else T0
    store.bootstrap_address_fills(addr, clock.now(), window_start_ms=w_start,
                                  window_end_ms=w_end, params_fp="")
    scan = store.get_active_scan(addr)
    store._db.execute(
        "UPDATE fills_scan SET fills_in_window=?, observed_from_ms=?, observed_to_ms=?, "
        "window_start_ms=?, window_end_ms=?, status=?, result=?, reason=?, finished_at=? "
        "WHERE scan_id=?",
        (fills_in_window, observed_from_ms, observed_to_ms, w_start, w_end, status,
         "complete" if status == "done" else None,
         "retention_boundary_verified" if status == "done" else None,
         clock.now() if status == "done" else None, scan.scan_id))
    # 鏡射到 fills_sync 的名目窗口——只有第一層（觀測跨度）拿不到資料時
    # （`observed_from_ms`／`observed_to_ms` 為 `None`）才會被第二層用到。
    store._db.execute(
        "UPDATE fills_sync SET window_start_ms=?, window_end_ms=?, fills_in_window=? "
        "WHERE address=?", (w_start, w_end, fills_in_window, addr))
    sched = _sched(store, FakeHL(), clock=clock)
    sched._bootstrapped = True
    sched._test_clock = clock  # 測試專用：讓需要推進時鐘的測試不必另外重建
    if rank is not None:
        sched._rank_by_address[addr.lower()] = rank
    return sched


def test_rate_uses_observed_span_not_nominal_window(tmp_path):
    """回補中的位址：4,000 筆分布在 2 天的觀測跨度內 → 2,000 筆/天 ≈ 83 筆/小時，
    不得因為除以 30 天名目窗口而被當成 5.6 筆/小時（4000/720h）——後者會讓
    這個位址被排成 24 小時，累積量遠超一頁。"""
    sched = _scheduler_with_scan(tmp_path, ADDR, fills_in_window=4_000,
                                 observed_from_ms=T0, observed_to_ms=T0 + 2 * DAY,
                                 window_start_ms=T0, window_end_ms=T0 + 30 * DAY,
                                 status="running", rank=200)
    assert sched.fills_period_s_for(ADDR) < MAX_PERIOD_S      # 關鍵：不得落在 24h


def test_high_density_partial_traversal_gets_min_period(tmp_path):
    """密度高到一個週期內必然超過一頁（8,000 筆／10 小時＝800 筆/小時）→
    直接壓到下界，不因為窗口名目上是 30 天就被稀釋。"""
    sched = _scheduler_with_scan(tmp_path, ADDR, fills_in_window=8_000,
                                 observed_from_ms=T0, observed_to_ms=T0 + 10 * 3_600_000,
                                 window_start_ms=T0, window_end_ms=T0 + 30 * DAY,
                                 status="running", rank=200)
    assert sched.fills_period_s_for(ADDR) == MIN_PERIOD_S


def test_falls_back_to_window_hours_when_no_observed_span(tmp_path):
    """遍歷已完成但沒有留下觀測跨度（`observed_from_ms`／`observed_to_ms`
    皆為 `None`，例如舊資料或帳戶全程零成交）→ 退回 `fills_sync` 名目窗口
    小時數，與 Task 5 原本的算法一致。"""
    sched = _scheduler_with_scan(tmp_path, ADDR, fills_in_window=720, observed_from_ms=None,
                                 observed_to_ms=None, window_start_ms=T0,
                                 window_end_ms=T0 + 30 * DAY, status="done", rank=200)
    assert sched.fills_period_s_for(ADDR) == fills_period_s(fills_per_hour=1.0, rank=200)


def test_no_data_at_all_stays_conservative(tmp_path):
    """完全沒有速率資料（沒有觀測跨度，名目窗口也是零寬度——省略
    `window_start_ms`／`window_end_ms`，見 `_scheduler_with_scan` docstring）
    → 兩層都拿不到資料，取保守值。"""
    sched = _scheduler_with_scan(tmp_path, ADDR, fills_in_window=0, observed_from_ms=None,
                                 observed_to_ms=None, status="running", rank=200)
    assert sched.fills_period_s_for(ADDR) == MIN_PERIOD_S


def test_period_source_is_still_single(tmp_path):
    """7.8 不變式不得因本次改動而失守：到期判斷與重排仍同源。沿用 Task 5 的
    `test_due_check_and_reschedule_share_one_period_source` 手法——這裡改用
    一個經 `_scheduler_with_scan` 設定過觀測密度（第一層）的位址，確認新的
    速率取得路徑一樣只有一個出口。"""
    # ⚠️ `window_end_ms=T0`（不是 `T0 + 30*DAY`）：`fills_sync.window_end_ms`
    # 是「名目窗口的終點＝建立當下的 now」（比照 `fresh_scan_window` 往回看
    # 30 天，見 explore_fills_sync.py），不是往未來看的終點——這裡要真的推進
    # 時鐘讓 `plan_incremental` 判定到期，`window_end_ms` 必須落在測試起始的
    # 「現在」，不能設在 30 天後（那會讓到期判斷恆假，測試變成沒在測
    # `_run_increment` 的 done 分支）。
    sched = _scheduler_with_scan(tmp_path, ADDR, fills_in_window=8_000,
                                 observed_from_ms=T0, observed_to_ms=T0 + 10 * 3_600_000,
                                 window_start_ms=T0 - 30 * DAY, window_end_ms=T0,
                                 status="running", rank=200)
    store = sched._store
    expected = sched.fills_period_s_for(ADDR)
    assert expected == MIN_PERIOD_S  # 高密度、非熱門名次 → 第一層命中，壓到下界

    clock = sched._test_clock
    clock.t += expected + 10
    store.enqueue(f"{ADDR}:fills", ADDR, "fills", 2, clock.now())

    r = sched.tick()
    assert r == "ran:fills"

    row = store._db.execute(
        "select next_attempt_at from refresh_job where key=?", (f"{ADDR}:fills",)).fetchone()
    assert row[0] - clock.now() == pytest.approx(expected, rel=0.15)


def test_due_check_and_reschedule_share_one_period_source(tmp_path):
    """7.8 教訓：改到期條件必同改重排時間，且必須同一來源
    （`sched.fills_period_s_for`）——不是排程端各自寫一份常數。

    刻意把時鐘推到「這一輪確實到期」（`now_ms - window_end_ms >= period_ms`），
    讓這一次 `_run_increment` 真的打上游、拿到空頁、**完成一輪**（走
    `apply_incremental_page` 的 `done` 分支），而不是原地的 noop 續等——
    7.8 的原始事故正是「noop 重排」與「完成一輪後的重排」各自讀了不同常數，
    只驗 noop 分支不會抓到那個錯（已用暫時改壞 `_run_increment` 的 done 分支
    重排這一行、確認本測試會紅之後才改回來，見派工回報）。用一個 cold
    （`rank=200`，落在 50 名外，速率 2 筆/小時）位址：完成這一輪增量後的實際
    重排間隔要對得上 `fills_period_s_for` 算出的值（速率公式夾到上界，非
    「剛好等於 MIN_PERIOD_S 保底值」的巧合——若沒有寫入速率資料，零寬度的
    `fills_sync` 窗口會讓 `fills_period_s_for` 直接走保守保底值，測不到
    `_run_increment` 真正讀的是不是同一個來源）。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    addr = "0xccc1"
    now_ms = int(clock.now() * 1000)
    store.upsert_candidates([(addr, None, 200, None)], as_of=clock.now())
    store.bootstrap_address_fills(addr, clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                  window_end_ms=now_ms, params_fp="")
    _complete_scan(store, addr, result="complete", reason="retention_boundary_verified",
                   finished_at=clock.now())
    _set_fills_rate(store, addr, window_hours=100, fills_in_window=200)  # 2 筆/小時
    sched = _sched(store, SequencedFillsHL([[]]), clock=clock)
    sched._bootstrapped = True
    sched._rank_by_address[addr.lower()] = 200
    expected = sched.fills_period_s_for(addr)
    assert expected == MAX_PERIOD_S  # 低速率＋非熱門名次 → 夾到上界，不是保底值

    # 推到「這一輪到期」：window_end_ms（=bootstrap 當下的 now_ms）之後至少
    # 一個 period，`plan_incremental` 才不會判 noop。
    clock.t += expected + 10
    store.enqueue(f"{addr}:fills", addr, "fills", 2, clock.now())

    r = sched.tick()
    assert r == "ran:fills"

    row = store._db.execute(
        "select next_attempt_at from refresh_job where key=?", (f"{addr}:fills",)).fetchone()
    # ⚠️ 比較的是「重排間隔」本身（`row[0] - clock.now()`），不是把它加進
    # `clock.now()`（此刻已是 4000 萬秒等級的 epoch）後再算相對誤差——後者的
    # 15% 容差會被巨大的 epoch 基底稀釋到遠大於一個週期，抓不到「重排用了
    # 錯的來源」這種錯（已用暫時改壞 `_run_increment` 的 done 分支重排驗證過：
    # 改成 `row[0] == pytest.approx(clock.now() + expected, rel=0.15)` 這種寫法
    # 抓不到 3600 vs 86400 的差；改成比較 delta 才會紅，見派工回報）。
    assert row[0] - clock.now() == pytest.approx(expected, rel=0.15)


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
                  fills_min_period_s=10**9, fills_max_period_s=10**9, rng=lambda: 0.0)
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
                  fills_min_period_s=10**9, fills_max_period_s=10**9, rng=lambda: 0.0)
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
    sched = _sched(store, hl, clock=clock, fills_min_period_s=1, fills_max_period_s=1)
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


# Task 3（2026-09-22 D-E／D-F）：`test_b7_ii_stale_probe_writeback_does_not_
# overwrite_new_scan`（探測回寫 CAS 落空不覆蓋新遍歷）已刪除——它測的是舊版
# `apply_probe_result` 的 scan_id 範圍雙 CAS，那個機制已被 `set_left_boundary`
# 取代（地址層級的冪等寫入，見 `explore_store.ExploreStore.set_left_boundary`
# docstring）：左界證據不屬於某一次特定的遍歷、只屬於地址本身，「舊探測回應
# 覆蓋新遍歷結論」這種以 scan_id 定義的競態在新設計裡不成立，沒有對應的行為
# 可測。


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
    # Task 3：`FakeHL` 沒有 portfolio `allTime` 資料，三個地址探測後都停在
    # `unknown`（D-F：不知道就是不知道，仍是候選、還會被重探）——這裡改驗證
    # 三個地址「各探測過一次」（輪詢排序，見 `next_probe_candidate` 的
    # `left_boundary_at` 鍵），不是候選集合歸零。
    assert sched.status()["probe"]["total"] == 3
    assert sched.status()["probe"]["candidates"] == 3
    for a in addrs:
        assert store.get_left_boundary(a, now_ms - 30 * 86_400_000).at is not None


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
    # Task 3：左界證據先解出——`SequencedFillsHL([[]])` 只有一筆排隊回應，
    # 留給真正的初次分頁用（短頁立刻收尾）；探測前置若也從隊列拿一筆會讓
    # 真正的分頁在下一個 tick 因為隊列已空而 IndexError。這個測試在測「新
    # 地址生命週期」三個階段的順序，與左界證據怎麼來無關。
    store.set_left_boundary(addr, "no_earlier_activity", scan.window_start_ms, clock.now())

    # 推進到 fills_scan job 到期並執行：initial scan 完成 → CAS 寫回 complete。
    store._db.execute("UPDATE refresh_job SET next_attempt_at=? WHERE key=?",
                      (clock.now(), f"{addr}:fills_scan"))
    r2 = sched.tick()
    assert r2 == "ran:fills_scan"
    sync2 = store.get_sync(addr)
    assert sync2.completeness == "complete"
    assert sync2.reason == "left_boundary_no_activity"
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
# Task 7.9a A1／Task 5（2026-09-22，D-B／D-H，主線程裁決改寫）：fills 週期單一
# 來源——scheduler 重排間隔與 `explore_fills_sync.plan_incremental` 的增量
# 寬限期必須讀同一個 `sched.fills_period_s_for(address)`，不是各自一份常數
# （Task 7.8 教訓）。⚠️ 這條測試守的就是 7.8 事故的不變式，只能改「期望值的
# 來源」（從建構子傳入的全域 `period` 改成 `fills_period_s_for(address)`），
# 不准刪。
# ============================================================

@pytest.mark.parametrize("rank,addr", [(1, "0xaaa1"), (200, "0xaaa2")])
def test_fills_period_single_source_ties_reschedule_and_plan_incremental(tmp_path, rank, addr):
    """完成一輪增量後：(i) 重排間隔 ≈ `fills_period_s_for(addr)`；
    (ii) `period − 1s` 仍是 noop（不打上游）；(iii) `period + 1s` 開新的增量輪
    （打上游）。三者都隨同一個 `fills_period_s_for(addr)` 值變化，不是各自一份
    常數。對 hot（`rank=1`，落在前 50 名，週期由新鮮度需求釘死＝`MIN_PERIOD_S`）
    與 cold（`rank=200`，無成交紀錄，週期由速率公式夾到上界＝`MAX_PERIOD_S`）
    各驗一次，確認不是巧合套中同一個數字。"""
    clock = Clock(t=40 * 86400.0)  # window 起點在 epoch 之後，避免負時間戳
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    store.upsert_candidates([(addr, None, rank, None)], as_of=clock.now())
    store.bootstrap_address_fills(addr, clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                  window_end_ms=now_ms, params_fp="")
    # Task 7.9d-S S1：`bootstrap_address_fills` 會建一個 running 的 initial scan，
    # 而對帳（首 tick／每個 candidates 輪）會為它補一個 `fills_scan` job 去續跑
    # ——本測試只量增量軌的週期，先把首次遍歷收尾掉，免得遍歷軌搶走 tick。
    _complete_scan(store, addr, result="complete", reason="retention_boundary_verified",
                   finished_at=clock.now())
    # 兩組共用同一份速率資料（2 筆/小時）——hot 靠名次覆寫這個速率
    # （`fills_period_s_for` 仍算出 MIN_PERIOD_S，不是因為速率沒資料才巧合
    # 套中同一個保底值），cold 則是這個速率被公式夾到上界。
    _set_fills_rate(store, addr, window_hours=100, fills_in_window=200)
    store.enqueue(f"{addr}:fills", addr, "fills", 2, clock.now())
    hl = SequencedFillsHL([[], []])
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True
    # Task 5：`_rank_by_address` 平常由 `_run_candidates` 寫入（見該方法）——
    # 本測試不跑 candidates 輪（直接餵手動 enqueue 的 `fills` job），比照
    # `upsert_candidates` 已寫進 DB 的名次直接灌快取。
    sched._rank_by_address[addr.lower()] = rank

    period = sched.fills_period_s_for(addr)
    expected_period = MIN_PERIOD_S if rank <= 50 else MAX_PERIOD_S
    assert period == expected_period  # 兩組真的踩中不同分支，非巧合套中同一數字

    r1 = sched.tick()
    assert r1 == "ran:fills"

    row = store._db.execute(
        "select next_attempt_at from refresh_job where key=?", (f"{addr}:fills",)).fetchone()
    # (i) 完成收尾後的重排間隔 ≈ `fills_period_s_for(addr)`（±15% jitter）。比較
    # delta（`row[0] - clock.now()`）而不是把 period 加進 `clock.now()`（4000
    # 萬秒等級的 epoch）再比——後者的 15% 容差會被巨大 epoch 基底稀釋到遠大於
    # 一個週期，抓不到「重排用了錯的來源」（見
    # `test_due_check_and_reschedule_share_one_period_source` 的派工回報）。
    assert row[0] - clock.now() == pytest.approx(period, rel=0.15)

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

    # (iii) period + 1s：開新的增量輪，打上游，真的完成一輪（走 `_run_increment`
    # 的 done 分支）——順便驗這一輪收尾後的重排間隔同樣對得上 `period`
    # （done 分支與 noop 分支的重排必須同源，7.8 教訓）。
    before_round3 = clock.now()
    clock.t = (window_end_ms + period * 1000 + 1000) / 1000
    store._db.execute("UPDATE refresh_job SET next_attempt_at=? WHERE key=?",
                      (clock.now(), f"{addr}:fills"))
    r3 = sched.tick()
    assert r3 == "ran:fills"
    assert len(hl.calls) == calls_after_round1 + 1
    row3 = store._db.execute(
        "select next_attempt_at from refresh_job where key=?", (f"{addr}:fills",)).fetchone()
    assert row3[0] - clock.now() == pytest.approx(period, rel=0.15)
    assert clock.now() > before_round3  # 確認這確實是新的一輪，不是誤判 noop


def test_run_api_passes_configured_fills_period_to_scheduler(tmp_path, monkeypatch):
    """Task 5（2026-09-22，D-B／D-H，主線程裁決）：`scripts.run_api` 把
    `cfg.explore_fills_period_s`／`explore_fills_max_period_s` 原樣傳給
    `ExploreScheduler(fills_min_period_s=, fills_max_period_s=)`（沿
    `test_run_api_wiring.py` 的 `__init__` 攔截慣例）——兩個參數都要有讀取者，
    不是傳進去沒人讀的死旋鈕。"""
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

    assert captured["fills_min_period_s"] == 7200
    assert captured["fills_max_period_s"] == MAX_PERIOD_S  # 未設 env，走預設上界


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
    window_start_ms = now_ms - 30 * 86_400_000
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store.bootstrap_address_fills("0xabc", clock.now(), window_start_ms=window_start_ms,
                                  window_end_ms=now_ms, params_fp="")
    # Task 3：左界證據先解出——這個測試在測分頁抓取失敗時的隔離行為，與左界
    # 證據怎麼來無關，先解出讓第一個 tick 直接打到真正的分頁請求。
    store.set_left_boundary("0xabc", "no_earlier_activity", window_start_ms, clock.now())
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
    """`_run_probe` 的 dirty 通知也吞例外——store 寫入（`set_left_boundary`）
    已經在通知之前完成，callback 失敗不影響探測結果落地。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    window_start_ms = now_ms - 30 * 86_400_000
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store.bootstrap_address_fills("0xabc", clock.now(), window_start_ms=window_start_ms,
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
    # FakeHL 回空頁且沒有 portfolio allTime 資料 → D-F：不知道就是不知道，
    # 維持 unknown；重點是這次探測「已經落地」（dirty callback 失敗不影響
    # store 寫入，callback 在寫入之後才被呼叫）。
    assert store.get_left_boundary("0xabc", window_start_ms).state == "unknown"
    assert store.get_left_boundary("0xabc", window_start_ms).at is not None


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
    # Task 3：不再用 monkeypatch 頂替 `get_left_boundary`（那條路徑繞過了本次
    # 要驗證的真實探測機制——DB 裡的 `left_boundary` 欄位永遠不會被真的寫入，
    # 讓這個地址變成永久的探測候選，3 天模擬下 `probe.total` 沖到 25 萬+，
    # 這正是拆掉這個掩體要抓的風險）。改用 `_ProbeResolvesThenEmptyHL`：
    # 探測窗回一筆成交 → 一次解出 `earlier_fills_seen`；真正的分頁窗仍回
    # 空頁，沿用原本 `FakeHL` 的「立刻空頁收尾」行為。
    sched = _sched(store, _ProbeResolvesThenEmptyHL(), leaderboard_source_fn=lambda: payload,
                  clock=clock, cfg=ExploreConfig(candidate_pool=1))

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
    # Task 3：探測前置多佔一次 `ran:fills_scan`（第一個 tick 探測、第二個
    # tick 才是真正的分頁）——一次遍歷仍只建一筆 `fills_scan` 列（上面已驗），
    # 只是這筆列的完成現在跨兩個 tick。
    assert results.count("ran:fills_scan") == 2
    assert store.get_sync(ADDR_A).completeness == "complete"
    assert store.get_sync(ADDR_A).reason == "left_boundary_verified"
    assert sched.status()["probe"]["total"] == 1  # 正面證據永久有效，只探一次


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
    # Task 3：不再用 monkeypatch 頂替 `get_left_boundary`（見
    # `test_s7a_complete_address_never_rescans_across_candidate_rounds` 的
    # 教訓）——改用 `_ProbeResolvesThenEmptyHL`，探測窗一次解出正面證據，
    # 真正的分頁窗仍空頁立刻收尾（沿用原本 `FakeHL` 的行為，這個測試在測
    # 「首次遍歷立刻完整 → 永不重掃」，與左界證據怎麼取得無關）。
    hl = _ProbeResolvesThenEmptyHL()
    payload = _payload([ADDR_A])
    sched = _sched(store, hl, leaderboard_source_fn=lambda: payload, clock=clock,
                  cfg=ExploreConfig(candidate_pool=1))

    results = _run_for(sched, clock, 3 * 86400.0)

    rows = _scan_rows(store, ADDR_A)
    kinds = [r[0] for r in rows]
    assert kinds.count("partial_rescan") == 0, rows
    assert kinds.count("initial") == 1, rows
    assert len(rows) == 1, rows
    # Task 3：探測前置多佔一次 `ran:fills_scan`（見 test_s7a 同型註記）。
    assert results.count("ran:fills_scan") == 2
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
    HL 回同毫秒滿頁（一頁收尾）。左界證據預先解出（Task 3：這批測試在測
    `ScanWriteback` 收尾分支，與左界證據怎麼來無關——先解出讓 `sched.tick()`
    第一次呼叫就直接跑到真正的分頁請求，不必多耗一個 tick 在探測前置上）。"""
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    window_start_ms = now_ms - 30 * 86_400_000
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store.bootstrap_address_fills("0xabc", clock.now(), window_start_ms=window_start_ms,
                                  window_end_ms=now_ms, params_fp="")
    store.set_left_boundary("0xabc", "no_earlier_activity", window_start_ms, clock.now())
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


# ============================================================
# Task 3（2026-09-22，D-E／D-F）：左界證據——探測前置、可快取、單調有效。
# plan docs/superpowers/plans/2026-09-22-explore-fills-coverage-verdict-fix.md
# Task 3 Step 1 的六條測試。
# ============================================================

def test_probe_runs_before_first_page_of_a_new_scan(tmp_path):
    """探測前置：新建的 scan 第一個動作是探測，不是抓頁。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "e.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    window_start_ms = now_ms - 30 * 86_400_000
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store.bootstrap_address_fills("0xabc", clock.now(), window_start_ms=window_start_ms,
                                  window_end_ms=now_ms, params_fp="")
    store.enqueue("0xabc:fills_scan", "0xabc", "fills_scan", 2, clock.now())
    hl = _ProbeResolvesThenEmptyHL()
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True
    job = store.claim_due(clock.now(), "o", 60, kinds=("fills_scan",))
    assert job is not None

    result = sched._run_scan(job, clock.now(), verify=False)

    assert result == "ran:fills_scan"
    assert sched.probes_total == 1
    assert sched.scan_pages_total == 0
    assert store.get_left_boundary("0xabc", window_start_ms).state == "earlier_fills_seen"


def test_positive_boundary_evidence_survives_window_roll_forward(tmp_path):
    """單調性：窗口往前滾之後，正面證據仍適用，不得重探。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "e.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    window_start_ms = now_ms - 30 * 86_400_000
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store.bootstrap_address_fills("0xabc", clock.now(), window_start_ms=window_start_ms,
                                  window_end_ms=now_ms, params_fp="")
    store.set_left_boundary("0xabc", "earlier_fills_seen", window_start_ms, clock.now())
    _complete_scan(store, "0xabc", result="complete", reason="left_boundary_verified",
                   window_end_ms=now_ms, finished_at=clock.now())

    later = window_start_ms + 7 * 86_400_000
    assert store.get_left_boundary("0xabc", later).state == "earlier_fills_seen"
    assert store.next_probe_candidate() is None


def _probe_with_portfolio(tmp_path, clock, *, first_activity_ms: int | None,
                          local_last_seen_at: float | None = None):
    """建一個左界證據待解的候選：`bootstrap_address_fills` 建好 `fills_sync`
    ＋一個 running 的 `initial` scan（`_run_probe` 用它的 `window_start_ms`
    算探測窗），`portfolio` 端點快取的 `allTime` 首點＝`first_activity_ms`
    （`None` 代表完全沒有快取，模擬「從未成功抓過 portfolio」）。回傳
    `(store, sched, scan, window_start_ms)`。"""
    store = ExploreStore(tmp_path / "e.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    window_start_ms = now_ms - 30 * 86_400_000
    as_of = clock.now() if local_last_seen_at is None else local_last_seen_at
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=as_of)
    store.bootstrap_address_fills("0xabc", clock.now(), window_start_ms=window_start_ms,
                                  window_end_ms=now_ms, params_fp="")
    scan = store.get_active_scan("0xabc")
    if first_activity_ms is not None:
        store.put_cache_ok(
            "0xabc", "portfolio",
            [["allTime", {"accountValueHistory": [[first_activity_ms, "100"]],
                          "pnlHistory": [[first_activity_ms, "0"]]}]],
            clock.now(), clock.now() + 3600)
    sched = _sched(store, FakeHL(), clock=clock)  # FakeHL：探測窗回空頁
    return store, sched, scan, window_start_ms


def test_probe_empty_with_clearly_older_account_is_truncation_suspected(tmp_path):
    clock = Clock(t=40 * 86400.0)
    store, sched, scan, window_start_ms = _probe_with_portfolio(
        tmp_path, clock, first_activity_ms=None)
    # 帳戶首次活動明顯早於窗口起點（差距遠超過 `_PROBE_WINDOW_MS` 1 天）。
    store.put_cache_ok(
        "0xabc", "portfolio",
        [["allTime", {"accountValueHistory": [[window_start_ms - 30 * 86_400_000, "100"]],
                      "pnlHistory": [[window_start_ms - 30 * 86_400_000, "0"]]}]],
        clock.now(), clock.now() + 3600)

    ok = sched._run_probe(("0xabc", scan.scan_id), clock.now())

    assert ok is True
    assert store.get_left_boundary("0xabc", window_start_ms).state == "truncation_suspected"


def test_probe_empty_with_clearly_newer_account_is_no_earlier_activity(tmp_path):
    clock = Clock(t=40 * 86400.0)
    store, sched, scan, window_start_ms = _probe_with_portfolio(
        tmp_path, clock, first_activity_ms=None)
    # 帳戶首次活動晚於（或等於）窗口起點——窗口起點前確無活動。
    store.put_cache_ok(
        "0xabc", "portfolio",
        [["allTime", {"accountValueHistory": [[window_start_ms + 3 * 86_400_000, "100"]],
                      "pnlHistory": [[window_start_ms + 3 * 86_400_000, "0"]]}]],
        clock.now(), clock.now() + 3600)

    ok = sched._run_probe(("0xabc", scan.scan_id), clock.now())

    assert ok is True
    assert store.get_left_boundary("0xabc", window_start_ms).state == "no_earlier_activity"


def test_probe_empty_in_ambiguous_band_stays_unknown(tmp_path):
    """帳戶首次活動落在探測窗內（理應探得到卻回空）＝資料互相矛盾 → 維持 unknown。"""
    clock = Clock(t=40 * 86400.0)
    store, sched, scan, window_start_ms = _probe_with_portfolio(
        tmp_path, clock, first_activity_ms=None)
    store.put_cache_ok(
        "0xabc", "portfolio",
        [["allTime", {"accountValueHistory": [[window_start_ms - 12 * 3600_000, "100"]],
                      "pnlHistory": [[window_start_ms - 12 * 3600_000, "0"]]}]],
        clock.now(), clock.now() + 3600)

    ok = sched._run_probe(("0xabc", scan.scan_id), clock.now())

    assert ok is True
    assert store.get_left_boundary("0xabc", window_start_ms).state == "unknown"


def test_probe_never_uses_local_first_seen_as_evidence(tmp_path):
    """D-F：沒有 portfolio 就是不知道；不得拿 `candidate.last_seen_at`／
    `source_as_of` 頂替——即使那個本機時間戳明顯早於窗口起點（若被誤用會被
    判成 `truncation_suspected`），沒有 portfolio 快取就必須維持 `unknown`。"""
    clock = Clock(t=40 * 86400.0)
    store, sched, scan, window_start_ms = _probe_with_portfolio(
        tmp_path, clock, first_activity_ms=None,
        local_last_seen_at=clock.now() - 400 * 86400.0)

    ok = sched._run_probe(("0xabc", scan.scan_id), clock.now())

    assert ok is True
    assert store.first_activity_ms("0xabc") is None
    assert store.get_left_boundary("0xabc", window_start_ms).state == "unknown"


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


def test_s7h_probe_positive_finds_earlier_fills(tmp_path):
    """(h1) 探測命中（窗內仍有更早的成交）→ 左界證據升級為
    `earlier_fills_seen`，探測窗＝`[scan.window_start - 1 天,
    scan.window_start - 1]`。"""
    clock = Clock(t=40 * 86400.0)
    window_start_ms = int(clock.t * 1000) - 30 * 86_400_000
    store, hl, sched, scan = _probe_ready(
        tmp_path, clock, [{"coin": "BTC", "tid": 1, "time": window_start_ms - 1}])

    assert sched.tick() == "ran:probe"

    assert store.get_left_boundary("0xabc", window_start_ms).state == "earlier_fills_seen"
    assert len(hl.probe_calls) == 1
    addr, probe_start, probe_end = hl.probe_calls[0]
    assert addr == "0xabc"
    assert probe_end == scan.window_start_ms - 1
    assert probe_start == scan.window_start_ms - 86_400_000
    assert sched.status()["probe"]["verified"] == 1


def test_s7h_probe_negative_no_portfolio_stays_unknown(tmp_path):
    """(h2) 探測回空頁、且沒有 portfolio 快取（`first_activity_ms` 回 `None`）
    → D-F：不知道就是不知道，左界證據維持 `unknown`——**仍是候選**（與舊版
    「探過就永久排除」不同：正面證據才是永久的，`unknown` 本來就該被重探，
    見 `ExploreStore.get_left_boundary` docstring 的單調性設計）。"""
    clock = Clock(t=40 * 86400.0)
    window_start_ms = int(clock.t * 1000) - 30 * 86_400_000
    store, hl, sched, _ = _probe_ready(tmp_path, clock, [])

    assert sched.tick() == "ran:probe"

    assert store.get_left_boundary("0xabc", window_start_ms).state == "unknown"
    assert len(hl.probe_calls) == 1
    probe = sched.status()["probe"]
    assert (probe["total"], probe["verified"], probe["empty"], probe["failed"],
            probe["stale"], probe["candidates"]) == (1, 0, 1, 0, 0, 1)


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


def test_recomputed_complete_drops_the_pending_partial_rescan_job(tmp_path):
    """Task 3b（D-G）：重算成 `complete` 之後，原本排著、已到期的整窗重掃
    job 要因狀態推導而被丟棄——不得真的再跑一次 18 頁以上的整窗重掃。

    構造與 `test_s7_scan_job_dropped_when_state_says_no_rescan` 同形（一個
    `partial` 地址、一個已到期的 `fills_scan` job），差別在於這裡的「狀態已經
    不需要重掃」不是一開始就是 `complete`，而是在 job 已經排定之後，探測
    （模擬 `next_probe_candidate` 獨立路徑，事後解出正面證據）才讓
    `set_left_boundary` 就地把 `partial` 翻成 `complete`——這正是 165 個
    位址（Task 4 遷移後）要靠這個機制翻身，而不必付一次整窗重掃的場景。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    window_start_ms = now_ms - 30 * 86_400_000
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store.bootstrap_address_fills("0xabc", clock.now(), window_start_ms=window_start_ms,
                                  window_end_ms=now_ms, params_fp="")
    _complete_scan(store, "0xabc", result="partial", reason="left_boundary_unknown",
                   window_end_ms=now_ms, finished_at=clock.now())
    # 24 小時重掃已到期：一個已到期的 `fills_scan` job 排在那裡（Task 4 遷移後
    # 的 165 個位址正是這個形狀——遍歷資料都在，只差一頁探測）。
    store.enqueue("0xabc:fills_scan", "0xabc", "fills_scan", 2, clock.now())
    hl = FakeHL()
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True

    # 探測事後解出正面證據（模擬 `next_probe_candidate` 獨立路徑成功探測；
    # 直接呼叫 `set_left_boundary`，`_run_probe` 本身的探測邏輯已由 Task 3
    # 的六條測試覆蓋，這裡只驗重算之後的 job 對帳）。
    ok = store.set_left_boundary("0xabc", "earlier_fills_seen", window_start_ms, clock.now())
    assert ok is True
    assert store.get_sync("0xabc").completeness == "complete"     # 重算先生效

    assert sched.tick() == "dropped"

    assert hl.calls == []                                        # 零上游請求，沒有整窗重掃
    assert store.job_kinds("0xabc") == set()
    assert store.get_sync("0xabc").completeness == "complete"     # 結論沒被重掃覆蓋


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
    # Task 3：續跑同一個 scan_id 是這個測試要驗的核心不變式；`cursor_ms` 改用
    # 單調不倒退＋確實不是從 window_start 重新開始（而非要求與churn 前逐位元
    # 相等）——探測前置會讓 `_ResumableFillsHL` 的「前 N 次滿頁」預算多吃掉
    # 一次探測呼叫（該假物件不分辨探測窗與真實窗，兩者共用同一個呼叫計數器），
    # 這會讓後續真實分頁的確切游標值往後移一格，但不影響「續跑不整窗重抓」
    # 這個不變式本身。
    assert resumed.scan_id == scan_id
    assert resumed.cursor_ms >= cursor_ms
    assert resumed.cursor_ms > scan.window_start_ms

    _drive_until(sched, clock,
                 lambda rs: store.get_sync(ADDR_A).completeness != "backfilling")

    sync = store.get_sync(ADDR_A)
    # Task 3：`_ResumableFillsHL` 對探測窗一樣回滿頁（假物件不分辨探測窗與
    # 真實窗），左界證據在完成回補前就已經解出——遍歷完成即為 complete。
    assert sync.completeness == "complete"
    assert sync.reason == "left_boundary_verified"
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

    assert sched._needs_scan_job(ADDR_A, clock.now()) == ("initial_missing", "fills_scan")
    assert sched.reconcile_scan_jobs(clock.now())["initial_missing"] == 1

    # Task 3：探測前置消耗第一個 tick（`_ResumableFillsHL(full_pages=0)`
    # 對探測窗也回非空的短頁，左界證據當場解出為 `earlier_fills_seen`），
    # 第二個 tick 才是真正的分頁請求並收尾——這個測試在測「孤兒 backfilling
    # 地址會被對帳建回一次遍歷」，兩個 tick 都算數，與完整性結論是哪個值無關。
    assert sched.tick() == "ran:fills_scan"
    assert sched.tick() == "ran:fills_scan"
    assert [r[0] for r in _scan_rows(store, ADDR_A)] == ["initial"]
    sync = store.get_sync(ADDR_A)
    assert sync.completeness == "complete"
    assert sync.reason == "left_boundary_verified"


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

    results = [sched.tick() for _ in range(3)]

    # 名額每個 tick 都被用掉（不必等 9 頁 fills）；核驗連續推進。第三次可能讓給
    # 剛出現的探測候選——verify 遍歷收尾時 reason 會變成「門檻推論」，那一刻該
    # 地址就成為探測候選，而 7.9e-S S5 規定兩邊都在等時一律輪流。
    assert "idle" not in results
    assert results.count("ran:fills_verify") >= 2


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


# --- 7.9d（主線程）：非法頁退避——不每 tick 免費重試、達上限隔離 ---

class _OutOfWindowHL(FakeHL):
    """所有 fills 請求都回一筆落在請求窗口之外的成交（`validate_page` →
    `time_out_of_range`）；模擬持續回壞頁的上游。"""

    def get_fills_page(self, address, start_ms, end_ms):
        self.calls.append(("fills", address, start_ms, end_ms))
        return [{"coin": "BTC", "tid": 1, "time": start_ms - 86_400_000, "px": "1", "sz": "1"}]


def test_invalid_page_backs_off_and_quarantines_after_max_attempts(tmp_path):
    """7.9d-D 之後非法頁不終止輪次（游標不動、`last_error=invalid_page:*`）；排程端
    不得每 tick 免費重試：視同一次實際嘗試的暫時性失敗——指數退避、計 attempts，
    第 `MAX_JOB_ATTEMPTS` 次隔離 24 小時；`status()["invalid_pages"]` 可觀測。"""
    from spark.publicapi.explore_scheduler import MAX_JOB_ATTEMPTS
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    window_start_ms = now_ms - 30 * 86_400_000
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store.bootstrap_address_fills("0xabc", clock.now(), window_start_ms=window_start_ms,
                                  window_end_ms=now_ms, params_fp="")
    # Task 3：左界證據先解出——這個測試數的是「分頁一直非法」的重試次數，
    # 與左界證據怎麼來無關，先解出避免第一個 tick 被探測前置占走。
    store.set_left_boundary("0xabc", "no_earlier_activity", window_start_ms, clock.now())
    store.enqueue("0xabc:fills_scan", "0xabc", "fills_scan", 2, clock.now())
    hl = _OutOfWindowHL()
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True
    sched._first_tick_done = True

    results = []
    for _ in range(MAX_JOB_ATTEMPTS):
        t_before = clock.now()
        results.append(sched.tick())
        job = store._db.execute(
            "SELECT next_attempt_at, attempts FROM refresh_job WHERE key='0xabc:fills_scan'").fetchone()
        assert job is not None
        assert job[0] > clock.now() + 29           # 退避到未來，不是 now
        clock.t = job[0] + 1                        # 推進到下次到期再試
    assert results[:-1] == ["retry"] * (MAX_JOB_ATTEMPTS - 1)
    assert results[-1] == "quarantined"
    assert job[0] >= t_before + 86400 - 2          # 最後一次：隔離 24 小時
    assert sched.status()["invalid_pages"] == MAX_JOB_ATTEMPTS
    assert len(hl.calls) == MAX_JOB_ATTEMPTS        # 每次到期各發一次，沒有 tick 級免費重試
    scan = store.running_scan("0xabc")
    assert scan is not None and "invalid_page:" in scan.last_error   # 隔離時前綴 max_attempts:
    assert store.get_fills("0xabc", 0, now_ms) == []


# ---- 2026-09-22 主線程整合模擬（正式機 287 列快照、3 天）抓到的兩個缺陷 ----

def _verify_ready_address(tmp_path, clock, addr: str = "0xver", *, due_ago: float = 0.0):
    """一個 active、`complete`、`evidence_unknown=1` 且帶到期 `fills_verify` job
    的地址（＝遷移產生的核驗工作的真實形狀）。"""
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    store.upsert_candidates([(addr, None, 1, None)], as_of=clock.now())
    store.bootstrap_address_fills(addr, clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                  window_end_ms=now_ms, params_fp="")
    _complete_scan(store, addr, result="complete", reason="retention_boundary_verified",
                   window_end_ms=now_ms, finished_at=clock.now())
    store._db.execute("UPDATE fills_sync SET evidence_unknown=1 WHERE address=?", (addr,))
    store.enqueue(f"{addr}:fills_verify", addr, "fills_verify", 4, clock.now() - due_ago)
    return store


def test_s1_reconcile_resumes_running_verify_scan_with_verify_job(tmp_path):
    """整合模擬 Critical：對帳補的 job kind 必須與進行中的遍歷**同類**。

    舊版一律補 `fills_scan` → `_run_scan(verify=False)` 接手核驗遍歷跑完、原本的
    `fills_verify` job 還留著 → 下次被服務時看不到 running scan 就再開一個
    verify 遍歷：3 天模擬跑出 654 次 verify 遍歷（工作只有 129 件），27 個地址
    各累積 13–31 個 done verify scan。"""
    clock = Clock(t=40 * 86400.0)
    store = _verify_ready_address(tmp_path, clock)
    sched = _sched(store, _ResumableFillsHL(full_pages=1), clock=clock)
    sched._bootstrapped = True
    sched._first_tick_done = True

    assert sched.tick() == "ran:fills_verify"                 # 第一頁：verify 遍歷進行中
    running = store.running_scan("0xver")
    assert running is not None and running.kind == "verify"

    out = sched.reconcile_scan_jobs(clock.now())

    assert "resume_running" not in out                        # 已經有同類 job，不重排
    assert store.job_kinds("0xver") == {"fills_verify"}       # **不得**出現 fills_scan

    # 續跑到收尾：verify job 被刪、verify 遍歷恰好一個（不是每輪新開一個）。
    _drive_until(sched, clock, lambda rs: store.running_scan("0xver") is None, limit=50)
    assert store.job_kinds("0xver") == set()
    assert [r[0] for r in _scan_rows(store, "0xver")] == ["initial", "verify"]
    assert store.get_sync("0xver").evidence_unknown is False


def test_s1_reconcile_resumes_orphan_verify_scan_with_verify_job(tmp_path):
    """同上的對帳面：verify 遍歷進行中、`fills_verify` job 卻不見了（退池再回池、
    重啟）→ 對帳補的是 `fills_verify`（不是 `fills_scan`），續跑同一個 scan。"""
    clock = Clock(t=40 * 86400.0)
    store = _verify_ready_address(tmp_path, clock)
    sched = _sched(store, _ResumableFillsHL(full_pages=1), clock=clock)
    sched._bootstrapped = True
    sched._first_tick_done = True
    assert sched.tick() == "ran:fills_verify"
    scan_id = store.running_scan("0xver").scan_id
    store.delete_jobs("0xver")

    out = sched.reconcile_scan_jobs(clock.now())

    assert out["resume_running"] == 1
    assert store.job_kinds("0xver") == {"fills_verify"}
    assert sched.tick() == "ran:fills_verify"
    assert [r[0] for r in _scan_rows(store, "0xver")] == ["initial", "verify"]
    assert store.get_sync("0xver").scan_id == scan_id


def test_s1_scan_job_does_not_take_over_a_running_verify_scan(tmp_path):
    """整合模擬 Critical 的下游閘門：兩條遍歷軌不得互相接手——`fills_scan` job
    遇到進行中的 `verify` 遍歷（反之亦然）→ 丟棄 job、不接手、不建新 scan。"""
    clock = Clock(t=40 * 86400.0)
    store = _verify_ready_address(tmp_path, clock)
    hl = _ResumableFillsHL(full_pages=1)
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True
    sched._first_tick_done = True
    assert sched.tick() == "ran:fills_verify"
    scan_id = store.running_scan("0xver").scan_id
    rows_before = _scan_rows(store, "0xver")
    calls_before = len(hl.calls)

    # 人工（或舊版對帳）塞一個 fills_scan job。
    store.enqueue("0xver:fills_scan", "0xver", "fills_scan", 3, clock.now())
    assert sched.tick() == "dropped"

    assert store.running_scan("0xver").scan_id == scan_id     # 遍歷仍在跑
    assert _scan_rows(store, "0xver") == rows_before          # 沒有新 scan
    assert len(hl.calls) == calls_before                      # 零上游呼叫
    # Task 7.9e-S S2：kind 不相容的丟棄與「狀態不需要遍歷」的良性丟棄分開計數。
    assert sched.status()["scan_job_dropped_kind_mismatch"] == 1
    assert sched.status()["scan_job_dropped"] == 0
    assert store.job_kinds("0xver") == {"fills_verify"}

    # 反向：`fills_verify` job 遇到進行中的 initial／partial_rescan 遍歷
    # ——一樣不接手，但改成**延後**（刪掉的話核驗工作就此消失，見 7.9e-S S2）。
    clock2 = Clock(t=40 * 86400.0)
    store2 = ExploreStore(tmp_path / "second.db", now_fn=clock2.now)
    now_ms = int(clock2.now() * 1000)
    store2.upsert_candidates([("0xabc", None, 1, None)], as_of=clock2.now())
    store2.bootstrap_address_fills("0xabc", clock2.now(),
                                   window_start_ms=now_ms - 30 * 86_400_000,
                                   window_end_ms=now_ms, params_fp="")   # initial 進行中
    store2.enqueue("0xabc:fills_verify", "0xabc", "fills_verify", 4, clock2.now())
    hl2 = _ResumableFillsHL(full_pages=1)
    sched2 = _sched(store2, hl2, clock=clock2)
    sched2._bootstrapped = True
    sched2._first_tick_done = True

    # 反向的 `fills_verify` job **不得被刪**（7.9e-S S2／複審 C1）：延後 10 分鐘。
    assert sched2.tick() == "deferred"
    assert hl2.calls == []
    assert store2.running_scan("0xabc").kind == "initial"
    assert sched2.status()["verify_job_deferred"] == 1
    assert store2.job_kinds("0xabc") == {"fills_verify"}
    assert store2._db.execute(
        "SELECT next_attempt_at FROM refresh_job WHERE key='0xabc:fills_verify'"
    ).fetchone()[0] == pytest.approx(clock2.now() + 600)


class _MixedFillsHL:
    """`verify_addr` 的頁前 `verify_full_pages` 次滿頁、之後短頁收尾；其他地址
    恆滿頁（增量積壓永不消退）。所有時間戳落在請求窗口內。`pages` 記錄每個
    地址實際發出的頁數，供份額斷言用。"""

    def __init__(self, verify_addr: str, verify_full_pages: int = 3):
        self._verify_addr = verify_addr
        self._verify_full_pages = verify_full_pages
        self.pages: dict[str, int] = {}

    def get_fills_page(self, address, start_ms, end_ms):
        n_seen = self.pages.get(address, 0) + 1
        self.pages[address] = n_seen
        span = max(0, end_ms - start_ms)
        want = PAGE_LIMIT
        if address == self._verify_addr and n_seen > self._verify_full_pages:
            want = 3
        n = min(want, max(span - 1, 0))
        step = max(1, span // (n + 1)) if n else 1
        return [{"coin": "BTC", "tid": start_ms + (i + 1) * step,
                 "time": start_ms + (i + 1) * step} for i in range(n)]


def test_s3_multi_page_verify_finishes_under_continuous_fills_pressure(tmp_path):
    """整合模擬 Warning：輔助名額**不以逾期為前提**——只要有到期的 verify job，
    每 `SPECIAL_SERVE_RATIO` 頁 fills-like 就給一次，進行中的多頁 verify 遍歷
    才跑得完。舊版只在「逾期 ≥2h 或有探測候選」時給名額，而多頁 verify 每頁
    `_reschedule(job, now)` 又把等待歸零 → 實測 8 小時只推進 2 頁，129 件核驗
    要拖數十天。"""
    import dataclasses as _dc

    clock = Clock(t=40 * 86400.0)
    store = _verify_ready_address(tmp_path, clock)
    now_ms = int(clock.now() * 1000)
    busy = [f"0xbusy{i}" for i in range(10)]
    store.upsert_candidates([("0xver", None, 1, None)]
                            + [(a, None, i + 2, None) for i, a in enumerate(busy)],
                            as_of=clock.now())
    for a in busy:                                   # 永不消退的增量積壓
        store.bootstrap_address_fills(a, clock.now(),
                                      window_start_ms=now_ms - 30 * 86_400_000,
                                      window_end_ms=now_ms + 10**9, params_fp="")
        sync = store.get_sync(a)
        store.insert_fills_page(a, [], _dc.replace(sync, cursor_ms=now_ms + 1,
                                                   window_end_ms=now_ms + 10**9))
        store.enqueue(f"{a}:fills", a, "fills", 2, clock.now())
    hl = _MixedFillsHL("0xver")
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True
    sched._first_tick_done = True

    # Task 3：探測前置會先對 `0xver` 打一次左界證據探測（`_MixedFillsHL` 對
    # 探測窗一樣回非空滿頁，一次就解出 `earlier_fills_seen`），多耗掉一次
    # `0xver` 自己的「前 N 頁滿頁」預算，讓核驗遍歷完成所需的輪數往後移；
    # 60 輪（原 40）足夠涵蓋這一次額外探測。
    results = []
    for _ in range(60):
        results.append(sched.tick())
        clock.t += 1.0
        if store.get_sync("0xver").evidence_unknown is False and "0xver" in hl.pages:
            break

    assert store.get_sync("0xver").evidence_unknown is False      # 核驗完成
    assert store.job_kinds("0xver") == set()
    total_pages = sum(hl.pages.values())
    assert hl.pages["0xver"] <= total_pages / 10 + 1              # 輔助份額 <= 1/10
    # Task 3：`hl.pages["0xver"]` 含 1 次左界證據探測——探測走 `_run_scan` 的
    # 探測前置（消耗 `explore_fills` 預算本身，不占 9:1 輔助份額），不受這條
    # 比例約束；只有真正走輔助份額的 verify 頁才要滿足 9:1。
    verify_ratio_pages = hl.pages["0xver"] - 1
    assert results.count("ran:fills") >= 9 * verify_ratio_pages


def test_s3_verify_and_probe_take_turns_on_the_auxiliary_slot(tmp_path):
    """整合模擬 Warning 的另一半：verify 與探測**輪流**用輔助名額——固定
    「探測優先」時，287 個地址的探測候選會把每一次名額都吃掉（核驗永遠排第二）。"""
    clock = Clock(t=40 * 86400.0)
    store = _verify_ready_address(tmp_path, clock)
    now_ms = int(clock.now() * 1000)
    probes = [f"0xprobe{i}" for i in range(3)]
    store.upsert_candidates([("0xver", None, 1, None)]
                            + [(a, None, i + 2, None) for i, a in enumerate(probes)],
                            as_of=clock.now())
    for a in probes:                                 # 探測候選
        store.bootstrap_address_fills(a, clock.now(),
                                      window_start_ms=now_ms - 30 * 86_400_000,
                                      window_end_ms=now_ms, params_fp="")
        _complete_scan(store, a, result="complete",
                       reason="count_below_retention_threshold", window_end_ms=now_ms,
                       finished_at=clock.now())
    sched = _sched(store, _ResumableFillsHL(full_pages=5), clock=clock)
    sched._bootstrapped = True
    sched._first_tick_done = True

    results = [sched.tick() for _ in range(6)]

    assert results.count("ran:probe") == 3
    assert results.count("ran:fills_verify") == 3
    # 交錯（不是先連三次探測再連三次 verify）。
    assert results[:2] in (["ran:fills_verify", "ran:probe"], ["ran:probe", "ran:fills_verify"])


# ============================================================
# Task 7.9e-S：`verify_needed` 第四原因碼（複審 C1／C2 同根）、verify job 延後
# 不刪、對帳例外不隔離 candidates、輔助名額不白燒、逾期仍輪流、發送前 active
# ============================================================

def _evidence_unknown_address(tmp_path, clock, addr: str = ADDR_A, *,
                              completeness: str = "complete"):
    """一個 active、`evidence_unknown=1` 的地址（遷移留下的形狀），首次遍歷已收尾。"""
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    now_ms = int(clock.now() * 1000)
    store.upsert_candidates([(addr, None, 1, None)], as_of=clock.now())
    store.bootstrap_address_fills(addr, clock.now(), window_start_ms=now_ms - 30 * 86_400_000,
                                  window_end_ms=now_ms, params_fp="")
    _complete_scan(store, addr, result=completeness,
                   reason="retention_limit" if completeness == "partial"
                   else "retention_boundary_verified",
                   window_end_ms=now_ms, finished_at=clock.now())
    store._db.execute("UPDATE fills_sync SET evidence_unknown=1 WHERE address=?",
                      (addr.lower(),))
    store._db.commit()
    return store


def test_s1e_verify_needed_enqueues_verify_job_from_state(tmp_path):
    """S1 第四原因碼：`evidence_unknown == 1` 而**沒有任何核驗工作**（無
    `fills_verify` job、無進行中的 verify 遍歷）→ 對帳補一個 `fills_verify`
    （priority 4、到期時刻依地址雜湊攤在 48 小時內）。

    複審 C1／C2 同根：verify job 原本只有「遷移」與「resume」兩個來源，被刪或被
    掃除之後沒有任何路徑會依狀態重建 → 該地址對外永遠是 `evidence_unknown`。"""
    from spark.publicapi.explore_scheduler import VERIFY_SPREAD_S

    clock = Clock(t=40 * 86400.0)
    store = _evidence_unknown_address(tmp_path, clock)
    sched = _sched(store, FakeHL(), clock=clock)
    sched._bootstrapped = True
    sched._first_tick_done = True

    assert sched._needs_scan_job(ADDR_A, clock.now()) == ("verify_needed", "fills_verify")
    out = sched.reconcile_scan_jobs(clock.now())

    assert out["verify_needed"] == 1
    assert store.job_kinds(ADDR_A) == {"fills_verify"}
    row = store._db.execute(
        "SELECT priority, next_attempt_at FROM refresh_job WHERE key=?",
        (f"{ADDR_A.lower()}:fills_verify",)).fetchone()
    assert row[0] == 4
    assert clock.now() <= row[1] <= clock.now() + VERIFY_SPREAD_S
    # 冪等：第二次連跑零變更。
    assert "verify_needed" not in sched.reconcile_scan_jobs(clock.now())

    # 補上的 job 真的會把 evidence_unknown 清掉。
    store._db.execute("UPDATE refresh_job SET next_attempt_at=? WHERE key=?",
                      (clock.now(), f"{ADDR_A.lower()}:fills_verify"))
    store._db.commit()
    _drive_until(sched, clock,
                 lambda rs: store.get_sync(ADDR_A).evidence_unknown is False, limit=50)


def test_s1e_churned_verify_work_is_rebuilt_after_returning_to_pool(tmp_path):
    """S1／複審 C2 反轉（複審腳本 `rv_verify_loss.py` 的 test_B 斷言「verify job
    不會回來」）：`evidence_unknown` 地址退池 → 殘留 job 被掃除 → 回池後對帳依
    **狀態**重建 `fills_verify`，核驗不會永久消失。"""
    clock = Clock(t=40 * 86400.0)
    store = _evidence_unknown_address(tmp_path, clock)
    store.enqueue(f"{ADDR_A.lower()}:fills_verify", ADDR_A, "fills_verify", 4,
                  clock.now() + 10**5)
    sched = _sched(store, FakeHL(), clock=clock)
    sched._bootstrapped = True
    sched._first_tick_done = True

    store.deactivate_missing(set())                          # 退池
    swept = sched.reconcile_scan_jobs(clock.now())
    assert swept["inactive_jobs_deleted"] >= 1
    assert store.job_kinds(ADDR_A) == set()

    store.upsert_candidates([(ADDR_A, None, 1, None)], as_of=clock.now())   # 回池
    clock.t += 60.0
    rebuilt = sched.reconcile_scan_jobs(clock.now())

    assert rebuilt["verify_needed"] == 1
    assert store.job_kinds(ADDR_A) == {"fills_verify"}
    assert sched.status()["verify_needed"] == {"rows": 1, "with_job": 1, "with_running": 0,
                                              "unserved": 0}


def test_s1e_verify_needed_not_duplicated_when_work_exists(tmp_path):
    """S1：已有 `fills_verify` job、或已有進行中的 verify 遍歷 → 不再補（去重，
    不把已排好的到期時刻提前）。"""
    clock = Clock(t=40 * 86400.0)
    store = _evidence_unknown_address(tmp_path, clock)
    due_at = clock.now() + 12 * 3600
    store.enqueue(f"{ADDR_A.lower()}:fills_verify", ADDR_A, "fills_verify", 4, due_at)
    sched = _sched(store, FakeHL(), clock=clock)
    sched._bootstrapped = True
    sched._first_tick_done = True

    assert sched._needs_scan_job(ADDR_A, clock.now()) is None
    assert "verify_needed" not in sched.reconcile_scan_jobs(clock.now())
    assert store._db.execute(
        "SELECT next_attempt_at FROM refresh_job WHERE key=?",
        (f"{ADDR_A.lower()}:fills_verify",)).fetchone()[0] == due_at

    # 進行中的 verify 遍歷、job 也在 → 同樣不補。
    store.create_scan(ADDR_A, kind="verify", window_start_ms=0, window_end_ms=1,
                      cursor_ms=0, started_at=clock.now())
    assert sched._needs_scan_job(ADDR_A, clock.now()) is None
    st = sched.status()["verify_needed"]
    assert (st["rows"], st["with_job"], st["with_running"], st["unserved"]) == (1, 1, 1, 0)


def test_s2e_verify_job_is_deferred_not_deleted_when_rescan_running(tmp_path):
    """S2／複審 C1 反轉（`rv_verify_loss.py` 的 test_A 斷言「verify job 被刪掉後
    不會回來」）：`fills_verify` job 遇到進行中的 `partial_rescan` → 延後 10 分鐘
    （`verify_job_deferred`）、零上游、**job 不刪**；重掃收尾後 `evidence_unknown`
    最終清 0。"""
    clock = Clock(t=40 * 86400.0)
    store = _evidence_unknown_address(tmp_path, clock, completeness="partial")
    store.enqueue(f"{ADDR_A.lower()}:fills_verify", ADDR_A, "fills_verify", 4, clock.now())
    scan = store.create_scan(ADDR_A, kind="partial_rescan", window_start_ms=0,
                             window_end_ms=int(clock.now() * 1000),
                             cursor_ms=int(clock.now() * 1000), started_at=clock.now())
    hl = FakeHL()
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True
    sched._first_tick_done = True

    job = store.claim_due(clock.now(), "o", 60, kinds=("fills_verify",))
    assert job is not None
    assert sched._run_scan(job, clock.now(), verify=True) == "deferred"

    assert hl.calls == []
    assert "fills_verify" in store.job_kinds(ADDR_A)          # 沒有被刪
    assert sched.status()["verify_job_deferred"] == 1
    assert store.running_scan(ADDR_A).scan_id == scan.scan_id

    # 重掃收尾（FakeHL 空頁 → 一頁結束）→ 任何一次完成的遍歷都清掉
    # evidence_unknown，核驗需求隨之歸零。
    store.enqueue(f"{ADDR_A.lower()}:fills_scan", ADDR_A, "fills_scan", 3, clock.now())
    _drive_until(sched, clock,
                 lambda rs: store.get_sync(ADDR_A).evidence_unknown is False, limit=50)
    assert sched.status()["verify_needed"]["rows"] == 0


def test_s3e_reconcile_failure_does_not_quarantine_candidates(tmp_path):
    """S3／複審 W2：candidates 輪內的對帳拋例外 → 記 `reconcile_errors`、
    candidates job 照常收尾續排（舊版會走 `_quarantine`，24 小時整池停更）。"""
    import sqlite3

    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    payload = _payload([ADDR_A])
    sched = _sched(store, FakeHL(), leaderboard_source_fn=lambda: payload, clock=clock,
                  cfg=ExploreConfig(candidate_pool=1))
    sched._bootstrapped = True
    sched._first_tick_done = True
    store.enqueue("candidates:candidates", None, "candidates", 0, clock.now())
    sched.reconcile_scan_jobs = lambda now: (_ for _ in ()).throw(
        sqlite3.OperationalError("database is locked"))

    assert sched.tick() == "ran:candidates"

    assert sched.status()["reconcile_errors"] == 1
    due = store._db.execute(
        "SELECT next_attempt_at, attempts FROM refresh_job "
        "WHERE key='candidates:candidates'").fetchone()
    assert due[0] == pytest.approx(clock.now() + sched._candidates_every_s)
    assert due[1] == 0                                        # 沒有被當成失敗


def test_s4e_probe_that_sends_nothing_does_not_burn_the_auxiliary_slot(tmp_path):
    """S4／複審 S2：探測候選在發送前被判非 active（查詢與發送之間剛退池）→
    一頁都沒打，輔助名額計數器**不歸零**，同一個 tick 改試 verify。"""
    clock = Clock(t=40 * 86400.0)
    store = _evidence_unknown_address(tmp_path, clock, addr="0xver")
    # Task 3：`0xver` 的左界證據先解出（讓它不是探測候選）——這個測試要驗的
    # 是「探測候選」路徑，候選必須確定是 `0xgone`；否則 `0xver`／`0xgone`
    # 左界證據都是預設 `unknown`、都是候選，`next_probe_candidate` 挑到誰
    # 不確定（新排序鍵 `left_boundary_at` 兩者皆為 `NULL`，等值）。
    store.set_left_boundary("0xver", "earlier_fills_seen",
                            int(clock.now() * 1000) - 30 * 86_400_000, clock.now())
    store.enqueue("0xver:fills_verify", "0xver", "fills_verify", 4, clock.now())
    now_ms = int(clock.now() * 1000)
    store.upsert_candidates([("0xver", None, 1, None), ("0xgone", None, 2, None)],
                            as_of=clock.now())
    store.bootstrap_address_fills("0xgone", clock.now(),
                                  window_start_ms=now_ms - 30 * 86_400_000,
                                  window_end_ms=now_ms, params_fp="")
    _complete_scan(store, "0xgone", result="complete",
                   reason="count_below_retention_threshold", window_end_ms=now_ms,
                   finished_at=clock.now())
    hl = _ResumableFillsHL(full_pages=1)
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True
    sched._first_tick_done = True
    sched._fills_pages_since_special = 5
    # 探測候選查得到（active=1），但在 `_run_probe` 發送前退池。
    real_next = store.next_probe_candidate

    def racing_next():
        cand = real_next()
        if cand is not None:
            store.deactivate_missing({"0xver"})
        return cand

    store.next_probe_candidate = racing_next
    sched._special_turn = True        # 翻轉後是 False → 這一次輪到 probe 先

    assert sched.tick() == "ran:fills_verify"                 # 名額改給 verify

    assert sched.status()["probe"]["total"] == 0               # 探測一頁都沒打
    assert sched.status()["inactive_job_dropped"] == 1
    assert sched._fills_pages_since_special == 0               # 被 verify 用掉（不是白燒）


def test_s5e_overdue_verify_and_probe_still_alternate(tmp_path):
    """S5／複審 S1：verify **逾期**時仍與探測輪流——verify 的續頁會保留舊的
    `next_attempt_at`，所以核驗積壓期間「逾期」恆真；若逾期就一直優先，探測會
    零服務。4 次輔助名額中探測至少拿到 2 次。"""
    clock = Clock(t=40 * 86400.0)
    store = _verify_burst_fixture(tmp_path, clock, n=4, due_ago=3 * 3600.0)
    store.delete_jobs("0xbusy")
    now_ms = int(clock.now() * 1000)
    probes = [f"0xprobe{i}" for i in range(4)]
    store.upsert_candidates([(a, None, 10 + i, None) for i, a in enumerate(probes)],
                            as_of=clock.now())
    for a in probes:
        store.bootstrap_address_fills(a, clock.now(),
                                      window_start_ms=now_ms - 30 * 86_400_000,
                                      window_end_ms=now_ms, params_fp="")
        _complete_scan(store, a, result="complete",
                       reason="count_below_retention_threshold", window_end_ms=now_ms,
                       finished_at=clock.now())
    sched = _sched(store, _ResumableFillsHL(full_pages=5), clock=clock)
    sched._bootstrapped = True
    sched._first_tick_done = True
    assert sched._verify_overdue(clock.now()) is True

    results = [sched.tick() for _ in range(4)]

    assert results.count("ran:probe") >= 2
    assert results.count("ran:fills_verify") >= 2


def test_s7e_cache_kind_sends_nothing_for_inactive_address(tmp_path):
    """S7（裁決點 3 字面）：`state`／`portfolio`／`ledger` 也在**發送前**查
    active——退池地址的殘留基礎 job 舊版是「先抓、抓完才決定不續排」，白打一次
    上游、白吃 `explore_base` 額度。"""
    clock = Clock(t=40 * 86400.0)
    store = ExploreStore(tmp_path / "explore.db", now_fn=clock.now)
    store.upsert_candidates([("0xabc", None, 1, None)], as_of=clock.now())
    store.deactivate_missing(set())                           # 退池
    for kind, prio in (("state", 0), ("portfolio", 1), ("ledger", 1)):
        store.enqueue(f"0xabc:{kind}", "0xabc", kind, prio, clock.now())
    hl = FakeHL()
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True
    sched._first_tick_done = True   # 對帳的主動掃除另有測試

    assert [sched.tick() for _ in range(3)] == ["dropped"] * 3

    assert hl.calls == []
    assert sched.status()["inactive_job_dropped"] == 3
    assert store.job_kinds("0xabc") == set()


def test_w2_deferred_verify_job_is_dropped_once_evidence_is_filled_in(tmp_path):
    """7.9e 複審 W2：核驗需求也**由狀態推導**——verify job 被延後期間，別的遍歷
    （partial_rescan）收尾清掉了 `evidence_unknown`，這次核驗就沒有必要：領到時
    不開 verify 遍歷、零上游、`_complete(job)`、計 `verify_job_obsolete`。

    少了這一關就是白跑一次 30 天整窗遍歷去確認一件已經確認的事。"""
    clock = Clock(t=40 * 86400.0)
    store = _evidence_unknown_address(tmp_path, clock, completeness="partial")
    store.enqueue(f"{ADDR_A.lower()}:fills_verify", ADDR_A, "fills_verify", 4, clock.now())
    store.create_scan(ADDR_A, kind="partial_rescan", window_start_ms=0,
                      window_end_ms=int(clock.now() * 1000),
                      cursor_ms=int(clock.now() * 1000), started_at=clock.now())
    # Task 3：不再用 monkeypatch 頂替 `get_left_boundary`（見 test_s7a 教訓）
    # ——改用 `_ProbeResolvesThenEmptyHL`。這個測試在測「核驗需求由狀態推導、
    # evidence 補齊後 verify job 自動變 obsolete」，與重掃本身判 complete 還是
    # partial 無關——但若重掃判 partial，既有（未改動的）`partial_rescan`
    # 復原邏輯會對這個地址另外排一個 `fills_scan` job，讓 (3) 之後
    # `job_kinds(ADDR_A)` 不再是空集合，測穿了另一個不相干的機制。讓左界證據
    # 探測前置真的解出正面結論，保持這個測試只測它原本要測的那件事。
    hl = _ProbeResolvesThenEmptyHL()
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True
    sched._first_tick_done = True

    # (1) 延後（kind 不相容）。
    job = store.claim_due(clock.now(), "o", 60, kinds=("fills_verify",))
    assert sched._run_scan(job, clock.now(), verify=True) == "deferred"

    # (2) 重掃收尾 → `complete_scan` 無條件清 evidence_unknown。
    store.enqueue(f"{ADDR_A.lower()}:fills_scan", ADDR_A, "fills_scan", 3, clock.now())
    _drive_until(sched, clock,
                 lambda rs: store.get_sync(ADDR_A).evidence_unknown is False, limit=50)
    scan_rows_before = _scan_rows(store, ADDR_A)
    calls_before = len(hl.calls)
    clock.t += 601.0

    # (3) 同一個 verify job 到期被領到 → 不開遍歷。
    job2 = store.claim_due(clock.now(), "o", 60, kinds=("fills_verify",))
    assert job2 is not None
    assert sched._run_scan(job2, clock.now(), verify=True) == "dropped"

    assert len(hl.calls) == calls_before                      # 零上游呼叫
    assert _scan_rows(store, ADDR_A) == scan_rows_before      # 沒有新的 verify 列
    assert store.job_kinds(ADDR_A) == set()                   # job 收尾刪除
    assert sched.status()["verify_job_obsolete"] == 1


def test_w3_deferred_verify_does_not_burn_the_auxiliary_slot(tmp_path):
    """7.9e 複審 W3：輔助名額只在 verify **真的抓了一頁**時才算用掉——延後
    （kind 不相容）時計數器不歸零、`verify_served_by_deadline` 也不加，否則核驗軌
    要再等 10 頁 fills 才有下一次機會（而它一頁都還沒打）。"""
    from spark.publicapi.explore_scheduler import SPECIAL_SERVE_RATIO

    clock = Clock(t=40 * 86400.0)
    store = _evidence_unknown_address(tmp_path, clock, completeness="partial")
    store.enqueue(f"{ADDR_A.lower()}:fills_verify", ADDR_A, "fills_verify", 4,
                  clock.now() - 3 * 3600.0)                   # 已逾期 3 小時
    store.create_scan(ADDR_A, kind="partial_rescan", window_start_ms=0,
                      window_end_ms=int(clock.now() * 1000),
                      cursor_ms=int(clock.now() * 1000), started_at=clock.now())
    hl = FakeHL()
    sched = _sched(store, hl, clock=clock)
    sched._bootstrapped = True
    sched._first_tick_done = True
    sched._fills_pages_since_special = SPECIAL_SERVE_RATIO     # 輔助名額到位

    assert sched.tick() == "deferred"

    assert hl.calls == []
    assert sched._fills_pages_since_special == SPECIAL_SERVE_RATIO   # 名額沒被燒掉
    assert sched.status()["verify_served_by_deadline"] == 0
    assert sched.status()["verify_job_deferred"] == 1


# ============================================================================
# Task 7a（plan docs/superpowers/plans/2026-09-22-explore-fills-coverage-
# verdict-fix.md，D-I）：300 地址競爭負載整合 harness。
#
# 與本檔既有 harness 的差異：既有的 `FakeHL`／`_ProbeResolvesThenEmptyHL`
# 之類的假物件完全繞過 `hl_budget.WeightLimiter`（見各自 docstring：「不經
# HLGateway／WeightLimiter」），這正是 D-I 明確要求補的缺口——單元測試證明
# 函式對，證明不了「300 個位址在真實限流下互相競爭時，排程真的把大戶推進到
# complete」。本節新增 `SchedulerHarness`：真實 `WeightLimiter` ＋ 真實
# `HLGateway`（`hl_budget.py`／`hl.py` 未改動，即正式機會用到的那份），唯一
# 假造的是最外層 HTTP `post_fn`（`_T7AUpstream`）。Task 6／7b 可直接沿用本
# harness（`total_fills_pages`／`scan_pages`／`probes_executed`／
# `overdue_p95_s`／`verify_completed`／`published_row`／`scan_cursor`／
# `stored_fill_count`／`pages_refetched_after_restart` 皆已提供）。
# ============================================================================


def _t7a_addr(tag: int) -> str:
    """短數字 tag → 合法的 40 hex 字元地址。

    `explore_scheduler._spread` 對 `address[-8:]` 做 `int(..., 16)`——plan
    Task 7 pseudocode 裡的字面地址（`"0xwhale"`／`"0xmystery"`／`"0xburst"`）
    在真實排程碼路徑上會直接 `ValueError`（"whale"/"mystery"/"burst" 不是合法
    十六進位）。這是本 harness 唯一偏離 plan 範例字面碼的地方——用有意義的
    模組常數（`T7A_WHALE` 等）取代，語意不變。"""
    return f"0x{tag:040x}"


T7A_WHALE = _t7a_addr(1)
T7A_MYSTERY = _t7a_addr(2)
T7A_BURST = _t7a_addr(3)


def _t7a_default_portfolio(first_activity_ms: int) -> list:
    """`month`／`allTime` 皆有效（`enrich_candidate` 的必要 gating）；
    `allTime.accountValueHistory` 首點 = `first_activity_ms`——
    `ExploreStore.first_activity_ms` 唯一讀取來源，見該方法 docstring。"""
    av = [[first_activity_ms, "1000"], [first_activity_ms + 86_400_000, "1010"]]
    pnl = [[first_activity_ms, "0"], [first_activity_ms + 86_400_000, "10"]]
    body = {"accountValueHistory": av, "pnlHistory": pnl}
    return [["month", body], ["allTime", body]]


def _t7a_missing_evidence_portfolio() -> list:
    """D-F 反向護欄用：`month` 正常（維持列可被 enrich），`allTime.
    accountValueHistory` 刻意留空——`first_activity_ms` 因此回 `None`（見
    `ExploreStore.first_activity_ms`：`if not av: return None`），逼探測在
    回空頁時只能落在 `unknown`（缺 portfolio 佐證）。"""
    month = {"accountValueHistory": [[0, "1000"], [1, "1010"]],
             "pnlHistory": [[0, "0"], [1, "10"]]}
    all_time = {"accountValueHistory": [], "pnlHistory": [[0, "0"], [1, "10"]]}
    return [["month", month], ["allTime", all_time]]


def _t7a_even_fills(window_start_ms: int, window_end_ms: int, count: int) -> list[dict]:
    """`count` 筆均勻分布在窗口內、時間互不相同的成交——一半 `Open Long`／
    一半 `Close Long`（`startPosition==sz` 且 `closedPnl>0`），讓
    `trader_stats.fills_stats` 算出非 null 的 `win_rate_pct`（回歸根因用：
    大戶必須真的能在 `/explore` 顯示勝率，不是只有覆蓋狀態變綠）。"""
    span = window_end_ms - window_start_ms
    step = max(1, (span - 1) // max(1, count))
    out = []
    for i in range(count):
        t = window_start_ms + i * step
        if i % 2 == 0:
            dir_, start_pos, pnl = "Open Long", "0", "0"
        else:
            dir_, start_pos, pnl = "Close Long", "1", "1.5"
        out.append({"coin": "BTC", "tid": i, "time": t, "dir": dir_, "px": "100",
                   "sz": "1", "startPosition": start_pos, "closedPnl": pnl, "oid": i})
    return out


def _t7a_fill_at(t_ms: int, tid: int) -> dict:
    return {"coin": "BTC", "tid": tid, "time": t_ms, "dir": "Open Long", "px": "100",
           "sz": "1", "startPosition": "0", "closedPnl": "0", "oid": tid}


def _t7a_same_ms_fills(window_start_ms: int, count: int, *, cluster_size: int = 400) -> list[dict]:
    """`count` 筆成交，其中一個 `cluster_size` 筆的叢集全部落在同一毫秒、且
    刻意排列成該叢集橫跨遍歷軌第一頁／第二頁的分頁邊界（`PAGE_LIMIT` 頁面
    上限）——其餘成交各自獨立、遞增的毫秒。用來驗證『游標 inclusive 重疊 ＋
    (address, coin, tid) 去重』：分頁邊界重疊會讓叢集裡的一部分成交同時出現
    在兩次上游回應裡，去重必須讓最終落地筆數精確等於 `count`。

    ⚠️ 預設（`cluster_size=400`）不是把全部 `count` 筆塞在單一毫秒——那個字面
    讀法在 `PAGE_LIMIT=2000` 下數學上不可能被現有（未改動的）`apply_scan_page`
    完整取回：只要有任何一頁的成交「全部」等於該頁查詢的 `start_ms`，就會觸發
    `same_ms_overflow`（真正的、刻意的資料缺口保護——見
    `explore_fills_sync.apply_scan_page` docstring），且 `cursor_ms` 在該頁之後
    停滯在那個毫秒，下一頁查詢的 `start_ms` 還是同一個值，只要叢集大小超過
    一頁就必然再次觸發、永久卡住。`count=4_500 > 2×PAGE_LIMIT` 時，一輪遍歷
    最多只能取回 `2×PAGE_LIMIT`（實測：4,000）——這是現有正確、未改動程式碼的
    真實架構限制，不是本 harness 的缺陷。把叢集縮小到能被單一頁面「跨過去」
    （`cluster_size` 明顯小於 `PAGE_LIMIT`）才是預設情境真正要驗證的：同一
    毫秒的資料**可以** survive 分頁邊界，只要它沒有大到把整頁塞滿。

    `cluster_size >= count`（Task 7b 新增）：反過來構造「叢集大到跨不過去」
    的情境——整批 `count` 筆全部落在同一毫秒（`window_start_ms + 1`）。第一頁
    查詢的 `start_ms` 是 `window_start_ms`（游標初值），該毫秒與叢集時間不同，
    所以第一頁會正常前進、游標推到 `window_start_ms + 1`；第二頁查詢的
    `start_ms` 變成 `window_start_ms + 1`，此時整頁全部等於查詢 `start_ms`，
    觸發 `same_ms_overflow` 且永久卡死在該游標——用來驗證這個情境必須誠實
    降級成 `partial`／`unresolved_gap`，不得少抓了卻宣稱 `complete`。"""
    if cluster_size >= count:
        return [_t7a_fill_at(window_start_ms + 1, tid) for tid in range(count)]
    before = PAGE_LIMIT - 200   # 讓叢集橫跨 page1/page2 邊界（200 落在 page1 尾端）
    after = count - before - cluster_size
    assert after > 0, "count 太小，叢集放不進去——調整 cluster_size/before"
    out: list[dict] = []
    tid = 0
    t = window_start_ms + 1
    for _ in range(before):
        out.append(_t7a_fill_at(t, tid))
        tid += 1
        t += 1000
    cluster_t = t
    for _ in range(cluster_size):
        out.append(_t7a_fill_at(cluster_t, tid))
        tid += 1
    t = cluster_t + 1000
    for _ in range(after):
        out.append(_t7a_fill_at(t, tid))
        tid += 1
        t += 1000
    return out


def _t7a_payload(addresses: list[str]) -> dict:
    rows = [{"ethAddress": addr, "displayName": f"t7a{i}",
            "windowPerformances": [["month", {"roi": str(1.0 - i * 0.0001)}]]}
           for i, addr in enumerate(addresses)]
    return {"leaderboardRows": rows}


class _T7AUpstream:
    """300 地址整合 harness 的假上游：`clearinghouseState`／`portfolio`／
    `userNonFundingLedgerUpdates`／`userFillsByTime` 的最小可控實作，作為
    `HLGateway(post_fn=...)` 的 `post_fn`——`WeightLimiter`／`HLGateway` 本身
    完全是正式機的那份程式碼，見本節模組檔頭。

    `get_fills_page` 的分頁模型：**純函式**，只依 `(address, start_ms,
    end_ms)` 從該地址依 (time, tid) 排序好的成交清單裡切出 `time` 落在
    `[start_ms, end_ms]`（inclusive）的前 `PAGE_LIMIT` 筆。不記「已經供應到
    哪」——每次都是對同一份靜態資料的獨立查詢，就像對一個真正持久、不會
    「記得你上次問到哪」的上游資料庫查詢一樣（`hl.py.get_user_fills_paged`
    docstring：upstream 逐頁分頁行為「僅單頁 fixture 驗證過，未對真實 API
    逐頁分頁驗證」——這是本 harness 在缺乏該證據下能做的最保守假設：不虛構
    upstream 有任何超出「時間範圍查詢」以外的狀態）。這個純函式設計是刻意
    的：`explore_fills_sync` 的 inclusive-overlap 游標會讓同一筆成交在連續
    兩次分頁請求裡都出現在回應中（下一頁 `startTime` = 上一頁最後一筆的
    `time`），真正需要落地的是「這個重複會被 `(address, coin, tid)` 去重，
    不會被重複計數、也不會被漏算」——純函式上游正是唯一能真實重現這個重疊
    的建模方式；帶「已供應位置」記憶的上游（本檔先前一版）會讓上游自己
    避開重疊，反而測不到去重路徑。"""

    def __init__(self):
        self._fills: dict[str, list[dict]] = {}
        self._times: dict[str, list[int]] = {}
        self._portfolio: dict[str, list] = {}
        self.calls: Counter = Counter()
        self._seen_tids: dict[str, set] = {}
        self._track_redundant = False
        self.refetched_pages = 0

    def seed_fills(self, address: str, items: list[dict]) -> None:
        addr = address.lower()
        items = sorted(items, key=lambda f: (f["time"], f["tid"]))
        self._fills[addr] = items
        self._times[addr] = [f["time"] for f in items]
        self._seen_tids.setdefault(addr, set())

    def set_portfolio(self, address: str, payload: list) -> None:
        self._portfolio[address.lower()] = payload

    def mark_restart(self) -> None:
        """`pages_refetched_after_restart` 起算點——之後每一次
        `userFillsByTime` 回應若**整頁**的 tid 全部是重啟前已經回應過的
        （零新 tid），計一次「白打的一頁」。上游本身是純函式（見類別
        docstring），`_seen_tids` 只是供觀測用的旁路帳本，不影響實際供應
        的內容。"""
        self._track_redundant = True
        self.refetched_pages = 0

    def post(self, url: str, body: dict) -> object:
        t = body["type"]
        self.calls[t] += 1
        if t == "clearinghouseState":
            return {"marginSummary": {"accountValue": "10000"}}
        if t == "portfolio":
            return self._portfolio.get(body["user"].lower(), _t7a_default_portfolio(0))
        if t == "userNonFundingLedgerUpdates":
            return []
        if t == "userFillsByTime":
            return self._get_fills_page(body["user"], int(body["startTime"]),
                                        int(body["endTime"]))
        raise AssertionError(f"_T7AUpstream: 未預期的請求類型 {t!r}")

    def _get_fills_page(self, address: str, start_ms: int, end_ms: int) -> list[dict]:
        addr = address.lower()
        items = self._fills.get(addr)
        if not items:
            return []
        times = self._times[addr]
        pos = bisect.bisect_left(times, start_ms)
        out: list[dict] = []
        while pos < len(items) and len(out) < PAGE_LIMIT:
            f = items[pos]
            if f["time"] > end_ms:
                break
            out.append(f)
            pos += 1
        seen = self._seen_tids.setdefault(addr, set())
        if self._track_redundant and out:
            if all(f["tid"] in seen for f in out):
                self.refetched_pages += 1
        seen.update(f["tid"] for f in out)
        return out


class SchedulerHarness:
    """Task 7a：300 地址競爭負載下的整合驗收 harness。`seed` 釘死（D-I／7.9
    教訓：harness 沒有預算模型／churn／多頁就抓不到問題，種子不釘死結果不可
    複現）——目前只餵給 `ExploreScheduler(rng=...)`（jitter 來源），成交資料
    本身是確定性生成（不吃 rng），整體結果因此完全可複現。"""

    def __init__(self, *, seed: int = 20260922, candidates: int = 300):
        self._clock = Clock()
        self._rng = random.Random(seed)
        self._upstream = _T7AUpstream()
        self._limiter = WeightLimiter(
            global_cap=900,
            scope_caps={"explore": 300, "explore_base": 180, "explore_fills": 120},
            scope_parents={"explore_base": "explore", "explore_fills": "explore"},
            now_fn=self._clock.now, sleep_fn=self._clock.sleep, rng=self._rng.random)
        self._gateway = HLGateway("https://t7a.invalid", post_fn=self._upstream.post,
                                  sleep_fn=self._clock.sleep, limiter=self._limiter)
        self._n = candidates
        self._addresses = [T7A_WHALE, T7A_MYSTERY, T7A_BURST] + \
            [_t7a_addr(100 + i) for i in range(candidates - 3)]
        self._store: ExploreStore | None = None
        self._sched: ExploreScheduler | None = None
        self._tick_counts: dict[str, int] = {}

    def build(self, db_path) -> "SchedulerHarness":
        self._store = ExploreStore(db_path, now_fn=self._clock.now)
        for addr in self._addresses:
            self._upstream.set_portfolio(addr, _t7a_default_portfolio(0))
        self._sched = self._build_scheduler()
        return self

    def _build_scheduler(self) -> ExploreScheduler:
        payload = _t7a_payload(self._addresses)
        return ExploreScheduler(
            store=self._store, hl=self._gateway.scoped("explore"),
            hl_base=self._gateway.scoped("explore_base"),
            hl_fills=self._gateway.scoped("explore_fills"),
            leaderboard_source_fn=lambda: payload, excluded_fn=lambda: set(),
            cfg=ExploreConfig(candidate_pool=self._n),
            now_fn=self._clock.now, sleep_fn=self._clock.sleep, on_dirty=lambda: None,
            # Task 7b（主線程 2026-09-22 裁決）：下界改成正式機真實預設
            # `FILET_EXPLORE_FILLS_MIN_PERIOD_S`（21600s＝6h）——舊值 3600（1h）
            # 比正式機密 6 倍，讓 300 個候選裡「前 50 名」（D-H 強制 min_period）
            # 每小時就要重新增量一次，量出來的任何吞吐比例都不能拿來推論正式機
            # 行為（保真度 bug，不是刻意的壓力測試設定）。
            fills_min_period_s=21600, fills_max_period_s=86400, rng=self._rng.random)

    # ---- 場景設定（`run_for` 之前呼叫；此時鐘面仍在 t=0，與 bootstrap 時
    # `fresh_scan_window` 算出的窗口一致） ----
    def set_fill_count(self, address: str, *, window_fills: int) -> None:
        ws, we = fresh_scan_window(int(self._clock.now() * 1000))
        self._upstream.seed_fills(address, _t7a_even_fills(ws, we, window_fills))
        self._upstream.set_portfolio(address, _t7a_default_portfolio(ws))

    def set_fills_all_same_ms(self, address: str, *, count: int, cluster_size: int = 400) -> None:
        ws, _we = fresh_scan_window(int(self._clock.now() * 1000))
        self._upstream.seed_fills(address, _t7a_same_ms_fills(ws, count, cluster_size=cluster_size))
        self._upstream.set_portfolio(address, _t7a_default_portfolio(ws))

    def seed_evidence_unknown_address(self, address: str) -> None:
        """Task 7b：構造一個「已有完成遍歷、`evidence_unknown=1`」的地址——
        這個形狀只有 v3→v4 遷移會留下（見 `explore_store._migrate_v3_to_v4`），
        全新建立的 store 不會自然產生。沿用本檔既有 `_evidence_unknown_address`
        手法（`upsert_candidates` → `bootstrap_address_fills` → 收尾一次遍歷 →
        直接 flip 旗標）在 harness 自己的 store 上構造好*初始狀態*；構造完成後
        `fills_verify` job 的建立、領工、完成全部交給真實 `ExploreScheduler`
        在 300 地址競爭下跑（`run_for` 是唯一推進時間與領工的地方）——只有
        「進入 evidence_unknown 狀態」這一步是構造的。"""
        import dataclasses
        now = self._clock.now()
        now_ms = int(now * 1000)
        self._store.upsert_candidates([(address, None, 1, None)], as_of=now)
        self._store.bootstrap_address_fills(
            address, now, window_start_ms=now_ms - 30 * 86_400_000,
            window_end_ms=now_ms, params_fp="")
        scan = self._store.get_active_scan(address)
        scan = dataclasses.replace(scan, cursor_ms=scan.window_end_ms, result="complete",
                                   reason="left_boundary_verified", finished_at=now)
        self._store.complete_scan(address, [], scan)
        self._store._db.execute(
            "UPDATE fills_sync SET evidence_unknown=1 WHERE address=?", (address.lower(),))
        self._store._db.commit()

    def set_probe_always_empty(self, address: str) -> None:
        """未配置任何成交的地址本來就是空探測——保留這個方法只為讓呼叫端
        對 D-F 反向護欄場景的意圖明確表態（即使目前是 no-op）。"""
        self._upstream.seed_fills(address, [])

    def set_portfolio_missing(self, address: str) -> None:
        self._upstream.set_portfolio(address, _t7a_missing_evidence_portfolio())

    # ---- 執行 ----
    def run_for(self, *, hours: float) -> None:
        target = self._clock.t + hours * 3600.0
        ticks = 0
        max_ticks = 3_000_000
        # Task 8（2026-09-22 實測抓到）：`idle` 且 `due_jobs_count>0` 不保證是
        # 「剛排了工作，下一 tick 才領」——也可能是「有到期工作（例如
        # `fills_verify`）但 `explore_fills` 保留額度這個 tick 不足
        # （`_fills_available() < FILLS_PAGE_WEIGHT`），`_serve_special`
        # 整條路徑因此連被呼叫都不會」。後者跟 `no_budget` 同形，需要真的
        # 睡過一段時間讓限流視窗清空；當成前者無限 `continue` 會是零時間推進
        # 的無限緊迴圈（40 個 `evidence_unknown` 位址＋300 候選競爭下實測
        # 命中，不是假設性風險）。分不出兩者就限制連續快轉次數，超過就退化成
        # 與 `no_budget` 相同的真實睡眠。
        idle_spins = 0
        while self._clock.t <= target:
            ticks += 1
            if ticks > max_ticks:
                raise AssertionError(
                    f"SchedulerHarness.run_for 超過 {max_ticks} tick 安全上限，可能卡住")
            r = self._sched.tick()
            self._tick_counts[r] = self._tick_counts.get(r, 0) + 1
            if r == "idle":
                due = self._store.due_jobs_count(self._clock.t) > 0
                if due and idle_spins < 3:
                    idle_spins += 1
                    continue    # 這一輪的 idle 只是「剛排了工作，下一 tick 才領」
                idle_spins = 0
                if due:
                    self._clock.sleep(1.0)
                    continue
                nxt = self._next_due_at(self._clock.t)
                if nxt is None or nxt > target:
                    break
                self._clock.t = max(nxt, self._clock.t)
            elif r in ("no_budget", "paused", "retry", "rate_limited", "deferred"):
                # 與正式 `run_forever` 同形：讓限流視窗／退避真的流逝，不是
                # 直接跳到「下一個到期時間」（那樣會讓 60 秒滑動視窗窗口失真）。
                # `deferred`：`_run_scan` 的探測前置在額度不足／限流暫停時也會
                # 回這個結果並把 job 重排到 `now`（立即可再領）——若當成零延遲
                # 結果處理，會在額度真的耗盡時對同一個 job 形成無時間推進的
                # 無限緊迴圈（budget 視窗永遠等不到清空），這是本 harness 實測
                # 抓到的問題，不是假設性風險。
                self._clock.sleep(1.0)
                idle_spins = 0
            else:
                # 其餘（ran:*／dropped／quarantined）：delay=0，立即續 tick。
                idle_spins = 0

    def restart(self) -> None:
        """重建 `ExploreScheduler`，只留 DB（模擬 process 重啟；假上游的分頁
        供應指標也標記重啟點，供 `pages_refetched_after_restart` 判斷）。"""
        self._upstream.mark_restart()
        self._sched = self._build_scheduler()

    def _next_due_at(self, now: float) -> float | None:
        row = self._store._db.execute(
            "SELECT MIN(next_attempt_at) FROM refresh_job WHERE next_attempt_at > ?",
            (now,)).fetchone()
        return row[0]

    # ---- 觀測 ----
    @property
    def scan_pages(self) -> int:
        return self._sched.scan_pages_total

    @property
    def total_fills_pages(self) -> int:
        return self._sched.status()["fills_pages_total"]

    @property
    def probes_executed(self) -> int:
        return self._sched.probes_total

    def limiter_snapshot(self) -> dict:
        return self._limiter.snapshot()

    def published_row(self, address: str) -> dict | None:
        rows, _meta = compose_rows(self._store, now=self._clock.t,
                                   cfg=ExploreConfig(candidate_pool=self._n))
        for row in rows:
            if row.address.lower() == address.lower():
                return {"fills_coverage": row.fills_coverage,
                       "win_rate": row.close_win_rate_pct,
                       "order_count_30d": row.order_count_30d,
                       "realized_pnl_30d_usd": row.realized_pnl_30d_usd}
        return None

    def stored_fill_count(self, address: str) -> int:
        row = self._store._db.execute(
            "SELECT COUNT(*) FROM fills WHERE address=?", (address.lower(),)).fetchone()
        return row[0]

    def scan_cursor(self, address: str) -> int | None:
        scan = self._store.get_active_scan(address) or self._store.latest_done_scan(address)
        return None if scan is None else scan.cursor_ms

    def pages_refetched_after_restart(self) -> int:
        return self._upstream.refetched_pages

    def verify_completed(self) -> int:
        row = self._store._db.execute(
            "SELECT COUNT(*) FROM fills_scan WHERE kind='verify' AND status='done'").fetchone()
        return row[0]

    # ---- Task 8：新判準端到端可達性 ----
    def addresses_with_left_boundary(self) -> int:
        """左界證據真的落地的位址數（`left_boundary != 'unknown'`）——證明
        `_run_probe`／`set_left_boundary` 真的被排程迴圈走到，不是只在單元
        測試裡對。"""
        row = self._store._db.execute(
            "SELECT COUNT(*) FROM fills_sync WHERE left_boundary != 'unknown'").fetchone()
        return row[0]

    @property
    def verdicts_from_scan_verdict(self) -> int:
        return self._sched.verdicts_total

    def seed_rows(self, *, evidence_unknown: int) -> list[str]:
        """種下 `evidence_unknown` 個「已有完成遍歷、`evidence_unknown=1`」的
        候選位址（沿用 `seed_evidence_unknown_address`）——這個形狀只有
        v3→v4 遷移會留下，全新建立的 store 不會自然產生。位址從候選池既有的
        `_t7a_addr(200..)` 區段取（避開 `T7A_WHALE/MYSTERY/BURST` 與既有
        `_t7a_addr(150)` 用法），確保它們本來就在 `leaderboard_source_fn`
        回傳的 active 候選集合內。"""
        addrs = [_t7a_addr(200 + i) for i in range(evidence_unknown)]
        for addr in addrs:
            self.seed_evidence_unknown_address(addr)
        return addrs

    def rows_with_evidence_unknown(self) -> int:
        row = self._store._db.execute(
            "SELECT COUNT(*) FROM fills_sync WHERE evidence_unknown=1").fetchone()
        return row[0]

    def overdue_p95_s(self, kind: str) -> float:
        """目前（呼叫當下）該 kind 已到期但尚未被領走的 job，逾期秒數分布的
        p95——快照式量測，不是整個 run 期間逐次領工延遲的歷史百分位（那需要
        逐次領工的時間戳，目前的 `ExploreScheduler`/`ExploreStore` 公開介面
        沒有暴露；這是留給 Task 6／7b 的已知近似，非精確值）。"""
        now = self._clock.t
        rows = self._store._db.execute(
            "SELECT next_attempt_at FROM refresh_job WHERE kind=? AND next_attempt_at<=?",
            (kind, now)).fetchall()
        ages = sorted(now - r[0] for r in rows)
        if not ages:
            return 0.0
        idx = min(len(ages) - 1, max(0, -(-95 * len(ages) // 100) - 1))
        return ages[idx]


def _t7a_harness(tmp_path, *, seed: int = 20260922, candidates: int = 300) -> SchedulerHarness:
    return SchedulerHarness(seed=seed, candidates=candidates).build(tmp_path / "t7a.db")


# --- Task 7 Step 1（plan Task 7a 範圍：六條裡的前四條） ---

def test_whale_reaches_complete_within_24h_under_full_contention(tmp_path):
    """回歸根因：30 天窗口 20,000 筆的地址，在 300 地址競爭＋真實限流下，
    必須在模擬 24 小時內成為對外 complete，且 win_rate 非 null。"""
    h = _t7a_harness(tmp_path)
    h.set_fill_count(T7A_WHALE, window_fills=20_000)
    h.run_for(hours=24)
    row = h.published_row(T7A_WHALE)
    assert row is not None
    assert row["fills_coverage"]["state"] == "complete"
    assert row["win_rate"] is not None


def test_missing_left_boundary_evidence_can_never_publish_complete(tmp_path):
    """反向護欄（比大戶那條更重要——防的是「把錯判不完整換成錯判完整」）：
    探測一直拿不到證據、portfolio 也缺席的位址，24 小時後仍不得是 complete。"""
    h = _t7a_harness(tmp_path)
    h.set_probe_always_empty(T7A_MYSTERY)
    h.set_portfolio_missing(T7A_MYSTERY)
    h.run_for(hours=24)
    row = h.published_row(T7A_MYSTERY)
    assert row is not None
    assert row["fills_coverage"]["state"] != "complete"


def test_same_millisecond_fills_survive_page_boundary(tmp_path):
    """同毫秒跨頁不漏單：游標 inclusive 重疊＋(time, tid) 去重。"""
    h = _t7a_harness(tmp_path)
    h.set_fills_all_same_ms(T7A_BURST, count=4_500)
    h.run_for(hours=6)
    assert h.stored_fill_count(T7A_BURST) == 4_500


def test_restart_resumes_scan_from_persisted_cursor(tmp_path):
    h = _t7a_harness(tmp_path)
    h.set_fill_count(T7A_WHALE, window_fills=20_000)
    h.run_for(hours=3)
    before = h.scan_cursor(T7A_WHALE)
    h.restart()
    h.run_for(hours=1)
    after = h.scan_cursor(T7A_WHALE)
    assert before is not None and after is not None and after >= before
    assert h.pages_refetched_after_restart() <= 1


# --- Task 7b（plan Task 7 Step 1 剩下三條，D-I）---


def test_period_policy_keeps_incremental_demand_under_budget():
    """D-B／D-H 迴歸測試（取代已廢棄的
    `test_traversal_track_gets_at_least_half_the_fills_pages`——主線程
    2026-09-22 複審裁決）。

    廢棄原因：「遍歷軌佔 fills 頁的比例」不是這個系統的穩定性質，是**工作負載
    組成**的函數，用 `SchedulerHarness`（300 個候選，其中 297 個是全空的假
    地址）量測必然失真——全空地址 bootstrap 完後遍歷需求趨近 0（實測 t=48h
    後 `scan_pages` 封頂不再增長），增量卻依 `fills_min_period_s` 永遠持續
    累加，長期比例必然單調趨近 0（實測 6h=0.177 → 24h=0.155 → 48h=0.117 →
    72h=0.078）；反過來把 harness 下界換成正式機真實值（6h）重跑同一個
    6 小時窗，增量還沒輪到第一次，比例又卡在另一個極端 1.0（`scan_pages ==
    total_fills_pages == 176`）。兩個極端都不是穩態，這個指標量不出真正要
    保護的東西。

    真正要保護的性質是**增量輪不可以再吃掉 fills 預算的大半**（事故當下
    50/56 ≈ 83%，把遍歷軌餓到只剩 12 頁/小時）。這個性質只由週期政策
    （`fills_period_s`）決定，跟「當下有多少遍歷工作可做」無關，因此是穩定
    的、不需要跑 harness——直接對一組**貼近正式機形狀**的合成位址算
    `Σ 3600/period_s` 即可，快、穩定、可重現。

    合成分佈依據：2026-09-22 在正式機複本上用正式程式碼（`fills_period_s_for`）
    實算出的真實週期分佈——6h=78（50 個 D-H 強制熱門＋28 個排名外但速率天生
    夠高，自然落在下界）、中段=4（速率介於下界與上界之間）、24h=218（排名外
    且速率低，自然落在上界），合計 300，對應增量需求 **22.34 頁/小時**
    （Task 5b 驗收記錄）。事故當下（門檻推論時代、無分層週期）的對照值是
    50 頁/小時（56 頁/小時實測上限的 83%）。"""
    demand = 0.0
    for rank in range(1, 51):          # 50：前 50 名，D-H 強制 6h（不論速率）
        demand += 3600 / fills_period_s(fills_per_hour=1.0, rank=rank)
    for rank in range(51, 79):         # 28：排名外，速率天生高（>266.7/h），自然 6h
        demand += 3600 / fills_period_s(fills_per_hour=500.0, rank=rank)
    for rank in range(79, 83):         # 4：中段，速率介於 66.7～266.7/h 之間
        demand += 3600 / fills_period_s(fills_per_hour=150.0, rank=rank)
    for rank in range(83, 301):        # 218：排名外，速率低（<66.7/h），自然 24h
        demand += 3600 / fills_period_s(fills_per_hour=1.0, rank=rank)

    demand_pages_per_hour = demand
    assert demand_pages_per_hour <= 30.0          # 正式機實算 22.34，留餘裕
    assert demand_pages_per_hour < 56 / 2         # 不得再吃掉 fills 上限的一半


def test_base_and_verify_are_not_starved_under_fills_pressure(tmp_path):
    """base（`state` 輪詢）與 verify（核驗遍歷）在 fills 壓力下不得被餓死。

    `evidence_unknown=1` 這個狀態全新建立的 store 不會自然產生（只有
    v3→v4 遷移會留下——見 `SchedulerHarness.seed_evidence_unknown_address`
    docstring），這裡用它構造出「遷移後留下一個待核驗地址」的形狀；構造完成
    之後，`fills_verify` job 的建立（`reconcile_scan_jobs` 在第一個 tick 對帳）、
    在 300 地址真實限流競爭下領工、完成（`kind='verify' AND status='done'`）
    全部交給真實排程跑，不是構造出來的。"""
    h = _t7a_harness(tmp_path)
    verify_addr = _t7a_addr(150)
    h.seed_evidence_unknown_address(verify_addr)
    h.run_for(hours=6)
    assert h.overdue_p95_s("state") < 1800
    assert h.verify_completed() > 0


def test_massive_same_ms_cluster_degrades_to_partial_not_silent_loss(tmp_path):
    """Task 7a 揭露的系統性質（主線程 2026-09-22 補）：單一毫秒超過
    2×PAGE_LIMIT 筆時，`same_ms_overflow` 這個刻意的保護會讓遍歷無法前進。
    此時**必須**誠實降級成 partial／unresolved_gap，絕不可以少抓了卻宣稱
    complete——這是「錯判完整」的最後一道防線，要有整合測試釘住，不能只靠
    Task 1 的單元測試。"""
    h = _t7a_harness(tmp_path)
    h.set_fills_all_same_ms(T7A_BURST, count=6_000, cluster_size=6_000)
    h.run_for(hours=6)
    row = h.published_row(T7A_BURST)
    assert row is not None
    assert row["fills_coverage"]["state"] == "partial"
    assert row["fills_coverage"]["reason"] == "unresolved_gap"
    assert row["win_rate"] is None          # D-14：非 complete 不得給成交衍生數字


# --- Task 8：新判準真的被正式流程執行——兩個端到端測試 ＋ 輔助份額臨時加速 ---
#
# 使用者指定：這兩個測試比「函式回傳正確」更能防止「程式改了但正式流程永遠走不到」
# ——本次已經現場抓到兩個這種缺口（探測候選查詢仍用被刪除的 reason、
# `evidence_unknown=0` 把待核驗列排除在探測母體外，見 plan Task 8 前言）。


def _scan_verdict_call_sites() -> set[str]:
    """`scan_verdict(` 在 `src/spark/publicapi/` 底下所有**指派形式**呼叫
    （`x, y = scan_verdict(...)`）的 `檔名:行號` 集合。用來斷言結論只有一個
    來源——比留一個恆為 0 的 `verdicts_from_legacy_path` 死計數器更誠實：
    Task 1–3 已經把覆蓋判準收斂成單一函式，本專案目前**沒有**第二條結論路徑
    可數，硬留一個計數器只會是「沒人加新路徑」的替代品，grep 直接測本體
    （主線程 2026-09-22 裁決，見 plan Task 8 派工 prompt）。"""
    root = Path(__file__).resolve().parents[1] / "src" / "spark" / "publicapi"
    pattern = re.compile(r"=\s*scan_verdict\(")
    hits: set[str] = set()
    for path in sorted(root.glob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if pattern.search(line):
                hits.add(f"{path.name}:{lineno}")
    return hits


def test_new_verdict_path_is_actually_reached_by_the_scheduler(tmp_path):
    """端到端：跑完排程迴圈後，探測候選查詢必須真的挑到人、左界證據必須真的
    被寫入、結論必須真的由 `scan_verdict` 產生（單元測試證明函式對，這條證明
    流程走得到）。

    `verdicts_from_legacy_path == 0`（plan 字面稿）在本實作沒有對應物可
    數——結構性斷言取代死計數器，見 `_scan_verdict_call_sites` docstring。"""
    call_sites = _scan_verdict_call_sites()
    files = {site.split(":")[0] for site in call_sites}
    assert len(call_sites) == 2, f"scan_verdict 呼叫點應恰好 2 個，實際：{call_sites}"
    assert files == {"explore_scheduler.py", "explore_store.py"}

    h = _t7a_harness(tmp_path)
    h.set_fill_count(T7A_WHALE, window_fills=500)
    h.run_for(hours=2)
    assert h.probes_executed > 0
    assert h.addresses_with_left_boundary() > 0
    assert h.verdicts_from_scan_verdict > 0


def test_evidence_unknown_rows_actually_leave_unknown_via_verify(tmp_path):
    """待核驗列必須真的能經 verify 軌離開 unknown——不是只在單元測試裡能。"""
    h = _t7a_harness(tmp_path)
    h.seed_rows(evidence_unknown=40)
    assert h.rows_with_evidence_unknown() == 40
    h.run_for(hours=24)
    assert h.rows_with_evidence_unknown() == 0


# --- Task 8 Step 4：輔助份額臨時加速，到期自動恢復（D-C／D-I）---


def test_special_serve_ratio_default_is_nine():
    """`SPECIAL_SERVE_RATIO` 的預設值 9 是使用者 2026-09-21 裁決，本次不得更動。"""
    assert SPECIAL_SERVE_RATIO == 9


def test_special_serve_ratio_reverts_after_deadline(tmp_path):
    """D-C／D-I：暫時加速只在期限內有效，逾期自動恢復預設 9——不依賴任何人
    記得移除 env。"""
    store = ExploreStore(tmp_path / "explore.db")
    sched = _sched(store, FakeHL(), special_serve_ratio=3,
                   special_serve_ratio_until="1970-01-01T00:00:05Z")
    assert sched._special_serve_ratio(0.0) == 3
    assert sched._special_serve_ratio(4.999) == 3
    assert sched._special_serve_ratio(5.0) == SPECIAL_SERVE_RATIO   # 到期當下即恢復
    assert sched._special_serve_ratio(100.0) == SPECIAL_SERVE_RATIO


@pytest.mark.parametrize("kw", [
    {},                                                                    # 完全未設
    {"special_serve_ratio": 3},                                           # 缺到期時間
    {"special_serve_ratio_until": "1970-01-01T00:00:05Z"},                # 缺比例
    {"special_serve_ratio": 0, "special_serve_ratio_until": "1970-01-01T00:00:05Z"},
    {"special_serve_ratio": -1, "special_serve_ratio_until": "1970-01-01T00:00:05Z"},
    {"special_serve_ratio": 3, "special_serve_ratio_until": "not-a-date"},  # 時間無法解析
    {"special_serve_ratio": 3, "special_serve_ratio_until": ""},
])
def test_special_serve_ratio_fails_safe_on_missing_or_invalid_config(tmp_path, kw):
    store = ExploreStore(tmp_path / "explore.db")
    sched = _sched(store, FakeHL(), **kw)
    assert sched._special_serve_ratio(0.0) == SPECIAL_SERVE_RATIO
    assert sched._special_serve_ratio(10_000.0) == SPECIAL_SERVE_RATIO
