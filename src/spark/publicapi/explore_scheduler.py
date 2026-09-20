"""src/spark/publicapi/explore_scheduler.py
Explore 排行榜背景排程（spec `docs/superpowers/specs/2026-09-20-hl-leaderboard-refactor-spec.md`
§6 頻率／jitter／前 50 優先、§9.1 lease／fencing／dedupe／admission；
plan `docs/superpowers/plans/2026-09-20-explore-rate-limit-refactor.md` Task 3.1）。

單一 thread、逐 job 執行；每個 job＝一次 HL 呼叫（或一頁 fills）。所有持久化走
`ExploreStore`；所有 HL 呼叫走建構子傳入的 `hl`（正式接線必須是
`gateway.scoped("explore")`，wait_s=0——本模組不自己等額度，額度不夠就讓位給
下一輪 tick，spec §3 條件「超過預算就 stale、不暴衝」）。

容量估算（300 候選池，spec §6，2026-09-20 主線程裁決追加 ledger）：
- state：`clearinghouseState` 2 weight／900s ≈ 40 weight/分鐘。
- portfolio：`portfolio` 20 weight／3600s ≈ 100 weight/分鐘。
- ledger：`userNonFundingLedgerUpdates` 20 weight／3600s ≈ 100 weight/分鐘。
合計 240 weight/分鐘，explore 子預算 300/分鐘尚餘 60 weight/分鐘給 fills
（14400s 週期、每頁上限 120 weight，實際頁數視回補進度而定）。

四種 kind：`state`（priority 0）、`portfolio`／`ledger`（priority 1，同級）、
`fills`（前 `hot_rank` 名 priority 2，其餘 3）；`candidates`（priority 0）
是每輪重新選池的 bootstrap／週期 job，本身不佔上述 4 個 per-address job 之列。
`candidates_every_s` 預設 1800（Task 3.5 B(5)）：candidates job 每次都要整批下載
stats-data leaderboard payload（約 36MB），拉太頻繁本身就是一筆不小的頻寬／延遲
成本，30 分鐘一次至多已經足夠偵測候選進出。

單一 job 失敗不影響其他 job：例外分類（`BudgetExhausted`／`ScopePaused`／429／
transient／其他）各自決定下一次 `next_attempt_at`，thread 本身只在
`run_forever` 層被保護——不因單一 tick 的未預期例外死掉（spec §3 條件五）。
"""
from __future__ import annotations

import logging
import random
import threading
from decimal import Decimal
from typing import Callable

from spark.publicapi.explore_fills_sync import apply_page, plan_page
from spark.publicapi.explore_store import ExploreStore, Job
from spark.publicapi.hl_budget import BudgetExhausted, ScopePaused, is_rate_limited
from spark.publicapi.hl_explore import ExploreConfig, _roi_sort_key, candidate_addresses

logger = logging.getLogger(__name__)

# 準入上限：`refresh_job` 總數 ≤ ADMISSION_MULTIPLIER × active 候選數（2026-09-20
# 主線程裁決：原 4× 因新增 ledger job 而放寬為 5×——四個 per-address job 種類
# （state/portfolio/ledger/fills）＋候選變動期間的緩衝）。
ADMISSION_MULTIPLIER = 5

# kind → endpoint_cache 的 endpoint 名稱（quarantine 時 `put_cache_error` 用；
# `fills` 不在這裡——它的錯誤落地在 `fills_sync.last_error`，見 `set_sync_error`）。
_ENDPOINT_BY_KIND = {
    "state": "clearinghouseState",
    "portfolio": "portfolio",
    "ledger": "ledger",
}


def _spread(address: str, period_s: float) -> float:
    """首次到期分散（spec §6）：用地址尾碼打散，避免 300 個地址在同一秒到期。
    `period_s <= 0` 時不分散（避免 `% 0`，測試可能傳極端值關掉某個 kind）。"""
    if period_s <= 0:
        return 0.0
    return float(int(address[-8:], 16) % int(period_s))


def _is_5xx(exc: Exception) -> bool:
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    return status is not None and 500 <= status < 600


def _roi_lookup(payload: dict, excluded: set[str]) -> dict[str, float | None]:
    """位址（小寫）→ roi（`hl_explore._roi_sort_key` 讀 stats-data month 窗；
    缺窗／解析失敗回傳的哨兵 `-Infinity` 一律轉成 `None`，不落一個假的極端值
    進 `candidate.source_roi`）。"""
    rows = (payload or {}).get("leaderboardRows") or []
    out: dict[str, float | None] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        addr = row.get("ethAddress")
        if not addr or addr.lower() in excluded:
            continue
        roi = _roi_sort_key(row)
        out[addr.lower()] = None if roi == Decimal("-Infinity") else float(roi)
    return out


class ExploreScheduler:
    """單 thread、逐 job 執行；每個 job＝一次 HL 呼叫（或一頁 fills）。所有持久化走
    ExploreStore，所有 HL 呼叫走 `hl`（必須是 gateway.scoped("explore")，wait_s=0）。"""

    def __init__(self, *, store: ExploreStore, hl, leaderboard_source_fn: Callable[[], dict | None],
                 excluded_fn: Callable[[], set[str]], cfg: ExploreConfig, now_fn, sleep_fn,
                 on_dirty: Callable[[], None], owner: str = "api", lease_s: float = 60.0,
                 candidates_every_s: float = 1800, state_every_s: float = 900,
                 portfolio_every_s: float = 3600, ledger_every_s: float = 3600,
                 fills_every_s: float = 14400, hot_rank: int = 50, jitter_pct: float = 0.10,
                 rng: Callable[[], float] = random.random,
                 on_tick: Callable[[], None] | None = None):
        self._store = store
        self._hl = hl
        self._leaderboard_source_fn = leaderboard_source_fn
        self._excluded_fn = excluded_fn
        self._cfg = cfg
        self._now = now_fn
        self._sleep = sleep_fn
        self._on_dirty = on_dirty
        self._owner = owner
        self._lease_s = lease_s
        self._candidates_every_s = candidates_every_s
        self._state_every_s = state_every_s
        self._portfolio_every_s = portfolio_every_s
        self._ledger_every_s = ledger_every_s
        self._fills_every_s = fills_every_s
        self._hot_rank = hot_rank
        self._jitter_pct = jitter_pct
        self._rng = rng
        self._on_tick = on_tick

        self._bootstrapped = False
        self._ticks = 0
        self._last_tick_at: float | None = None
        self._last_result: str | None = None
        self._results: dict[str, int] = {}
        self._last_candidates_ok_at: float | None = None
        self._candidates_empty_streak = 0

    # ---- jitter ----
    def _jit(self, period_s: float) -> float:
        return period_s * (1 + (self._rng() * 2 - 1) * self._jitter_pct)

    @staticmethod
    def _key(address: str, kind: str) -> str:
        return f"{address.lower()}:{kind}"

    # ---- 對外 ----
    def tick(self) -> str:
        now = self._now()
        result = self._tick_once(now)
        self._ticks += 1
        self._last_tick_at = now
        self._last_result = result
        self._results[result] = self._results.get(result, 0) + 1
        return result

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                r = self.tick()
                if self._on_tick is not None:
                    self._on_tick()
                if r in ("idle", "no_budget", "retry"):
                    delay = 1.0
                elif r == "paused":
                    delay = 5.0
                else:
                    delay = 0.0
                self._sleep(delay)
            except Exception:
                logger.exception("explore scheduler: tick 拋出未預期例外，繼續下一輪")
                # W1/W2 修法：例外路徑也要節流，否則是緊迴圈（連續拋例外時
                # 會不斷佔用 CPU 狂打 claim_due）。
                self._sleep(1.0)

    def status(self) -> dict:
        stats = self._store.stats()
        now = self._now()
        oldest = self._store.oldest_due_at(now)
        return {
            "last_tick_at": self._last_tick_at,
            "last_result": self._last_result,
            "ticks": self._ticks,
            "results": dict(self._results),
            "queue_depth": stats["refresh_job"],
            "oldest_due_age_s": None if oldest is None else max(0.0, now - oldest),
            "last_candidates_ok_at": self._last_candidates_ok_at,
            "candidates_empty_streak": self._candidates_empty_streak,
        }

    # ---- 內部：一次 tick ----
    def _tick_once(self, now: float) -> str:
        if not self._bootstrapped:
            self._bootstrapped = True
            self._store.enqueue("candidates:candidates", None, "candidates", 0, now)
            return "idle"

        job = self._store.claim_due(now, self._owner, self._lease_s)
        if job is None:
            return "idle"

        try:
            return self._run_job(job, now)
        except BudgetExhausted:
            self._reschedule(job, now + 5, bump_attempts=False)
            return "no_budget"
        except ScopePaused:
            remaining = self._paused_remaining_s()
            # S1 裁決：限流暫停不是這個 job 自己的失敗，不計 attempts。
            self._reschedule(job, max(now + 5, now + remaining), bump_attempts=False)
            return "paused"
        except Exception as e:  # noqa: BLE001 — 唯一的分類點，見檔頭
            if is_rate_limited(e):
                self._reschedule(job, now + 60, err=repr(e))
                return "rate_limited"
            if isinstance(e, (ConnectionError, TimeoutError)) or _is_5xx(e):
                attempts = job.attempts + 1
                next_at = now + min(30 * (2 ** attempts), 900) + self._rng() * 10
                self._reschedule(job, next_at, err=repr(e))
                return "retry"
            self._quarantine(job, now, e)
            return "quarantined"

    def _run_job(self, job: Job, now: float) -> str:
        if job.kind == "candidates":
            return self._run_candidates(job, now)
        if job.kind == "state":
            return self._run_cache_kind(
                job, now, endpoint="clearinghouseState",
                fetch=lambda addr: self._hl.clearinghouse_state(addr),
                period_s=self._state_every_s)
        if job.kind == "portfolio":
            return self._run_cache_kind(
                job, now, endpoint="portfolio",
                fetch=lambda addr: self._hl.portfolio(addr),
                period_s=self._portfolio_every_s)
        if job.kind == "ledger":
            return self._run_cache_kind(
                job, now, endpoint="ledger",
                fetch=lambda addr: self._hl.non_funding_ledger_updates(addr, 0),
                period_s=self._ledger_every_s)
        if job.kind == "fills":
            return self._run_fills(job, now)
        raise ValueError(f"explore scheduler: unknown job kind {job.kind!r}")

    def _run_candidates(self, job: Job, now: float) -> str:
        payload = self._leaderboard_source_fn()
        if payload is None:
            self._reschedule(job, now + 60, err="no payload", bump_attempts=False)
            return "retry"

        excluded = self._excluded_fn()
        rows = candidate_addresses(payload, self._cfg.candidate_pool, excluded)
        if not rows:
            # Task 3.6 B(1)（Critical C1 修法）：候選來源整批回空（上游壞掉、
            # payload 格式跑掉、被排除清單濾光……）不能當成「所有人退池」——
            # 不呼叫 upsert_candidates／deactivate_missing，保留既有候選池，
            # 60 秒後重試。Task 3.7 B（W1 修法）：長期回空之前無外部證據——
            # 記警告與 streak，供 `status()` 揭露。
            self._candidates_empty_streak += 1
            logger.warning(
                "explore scheduler: 候選來源回空 rows（streak=%d），60s 後重試；"
                "候選池維持不動", self._candidates_empty_streak)
            self._reschedule(job, now + 60, err="empty candidate rows", bump_attempts=False)
            return "retry"
        roi_by_addr = _roi_lookup(payload, excluded)

        seen: set[str] = set()
        upsert_rows: list[tuple[str, str | None, int | None, float | None]] = []
        for rank, (address, display_name) in enumerate(rows, start=1):
            seen.add(address.lower())
            upsert_rows.append((address, display_name, rank, roi_by_addr.get(address.lower())))
        self._store.upsert_candidates(upsert_rows, now)
        self._candidates_empty_streak = 0
        self._last_candidates_ok_at = now
        dropped = self._store.deactivate_missing(seen)
        for addr in dropped:
            self._store.delete_jobs(addr)

        jobs, active_n = self._store.admission_counts()
        if jobs > ADMISSION_MULTIPLIER * active_n:
            logger.warning(
                "explore scheduler: admission cap reached (%d jobs, %d active) — "
                "本輪不再新增 per-address job", jobs, active_n)
        else:
            for rank, (address, _display_name) in enumerate(rows, start=1):
                self._enqueue_address_jobs(address, rank, now)

        self._complete(job)
        self._store.enqueue(job.key, None, "candidates", job.priority,
                            now + self._candidates_every_s)
        return "ran:candidates"

    def _enqueue_address_jobs(self, address: str, rank: int, now: float) -> None:
        self._store.enqueue(self._key(address, "state"), address, "state", 0,
                            now + _spread(address, self._state_every_s))
        self._store.enqueue(self._key(address, "portfolio"), address, "portfolio", 1,
                            now + _spread(address, self._portfolio_every_s))
        self._store.enqueue(self._key(address, "ledger"), address, "ledger", 1,
                            now + _spread(address, self._ledger_every_s))
        fills_priority = 2 if rank <= self._hot_rank else 3
        self._store.enqueue(self._key(address, "fills"), address, "fills", fills_priority,
                            now + _spread(address, self._fills_every_s))

    def _run_cache_kind(self, job: Job, now: float, *, endpoint: str, fetch, period_s: float) -> str:
        payload = fetch(job.address)
        refresh_after = now + self._jit(period_s)
        self._store.put_cache_ok(job.address, endpoint, payload, now, refresh_after)
        self._complete(job)
        self._on_dirty()
        if not self._store.is_active(job.address):
            # Task 3.5 B(2)（C1 修法）：候選已退池，續排前先檢查——不然這個
            # job 會無條件永遠自我續排，預算持續漏給非候選、`refresh_job` 只增
            # 不減，最終觸發準入上限讓新候選拿不到 job、也讓 `purge` 的
            # `NOT EXISTS(refresh_job)` 條件永久卡住。
            return "dropped"
        self._store.enqueue(job.key, job.address, job.kind, job.priority, refresh_after)
        return f"ran:{job.kind}"

    def _run_fills(self, job: Job, now: float) -> str:
        st = self._store.get_sync(job.address)
        plan = plan_page(st, address=job.address, now_ms=int(now * 1000))
        if plan.is_noop:
            self._complete(job)
            if not self._store.is_active(job.address):
                return "dropped"
            next_at = plan.state.window_end_ms / 1000 + self._fills_every_s
            self._store.enqueue(job.key, job.address, "fills", job.priority, next_at)
            return "ran:fills"

        page = self._hl.get_fills_page(job.address, plan.start_ms, plan.end_ms)
        res = apply_page(plan, page, now_ms=int(now * 1000))
        self._store.insert_fills_page(job.address, res.accepted, res.state)
        self._on_dirty()
        if not res.done:
            # Task 3.6 B(2)（W1 修法）：續頁不看 is_active——非候選地址（例如
            # 詳情頁按需入列）多頁回補若中途被判 dropped，會永遠停在
            # backfilling，整份成交補不完。是否退池只在整輪 done 之後才判斷。
            self._reschedule(job, now, bump_attempts=False)
            return "ran:fills"
        self._complete(job)
        if not self._store.is_active(job.address):
            return "dropped"
        next_at = now + self._jit(self._fills_every_s)
        self._store.enqueue(job.key, job.address, "fills", job.priority, next_at)
        return "ran:fills"

    # ---- 內部：例外收尾 ----
    def _quarantine(self, job: Job, now: float, exc: Exception) -> None:
        err = repr(exc)
        endpoint = _ENDPOINT_BY_KIND.get(job.kind)
        if endpoint is not None:
            self._store.put_cache_error(job.address, endpoint, err, now, now + 86400)
        elif job.kind == "fills":
            self._store.set_sync_error(job.address, err, now)
        else:
            logger.error("explore scheduler: %s job 隔離、無對應快取欄位可寫（%s）",
                         job.kind, err)
        self._reschedule(job, now + 86400, err=err)

    def _paused_remaining_s(self) -> float:
        limiter = getattr(self._hl, "_limiter", None)
        if limiter is None:
            return 60.0
        return limiter.snapshot()["paused_remaining_s"].get("explore", 60.0)

    # ---- 內部：lease/fencing 收尾（輸家 log warning、不重試） ----
    def _complete(self, job: Job) -> bool:
        ok = self._store.complete(job, job.fencing)
        if not ok:
            logger.warning("explore scheduler: complete 競態落敗 key=%s fencing=%s",
                           job.key, job.fencing)
        return ok

    def _reschedule(self, job: Job, next_attempt_at: float, *, err: str | None = None,
                    bump_attempts: bool = True) -> bool:
        ok = self._store.reschedule(job, job.fencing, next_attempt_at, err=err,
                                    bump_attempts=bump_attempts)
        if not ok:
            logger.warning("explore scheduler: reschedule 競態落敗 key=%s fencing=%s",
                           job.key, job.fencing)
        return ok
