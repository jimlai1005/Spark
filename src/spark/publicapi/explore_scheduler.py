"""src/spark/publicapi/explore_scheduler.py
Explore 排行榜背景排程（spec `docs/superpowers/specs/2026-09-20-hl-leaderboard-refactor-spec.md`
§6 頻率／jitter／前 50 優先、§9.1 lease／fencing／dedupe／admission；
plan `docs/superpowers/plans/2026-09-20-explore-rate-limit-refactor.md` Task 3.1）。

單一 thread、逐 job 執行；每個 job＝一次 HL 呼叫（或一頁 fills）。所有持久化走
`ExploreStore`；所有 HL 呼叫走建構子傳入的 `hl`（正式接線必須是
`gateway.scoped("explore")`，wait_s=0——本模組不自己等額度，額度不夠就讓位給
下一輪 tick，spec §3 條件「超過預算就 stale、不暴衝」）。

容量估算與 Task 7.4b 修正（2026-09-21 使用者裁決：正式機證實 fills 類別級飢餓——
嚴格優先級＋沒有為 fills 一頁 120 weight 的大請求保留額度，state/portfolio/ledger
的穩態流量長期貼著 300 上限，fills 一小時拿不到一次額度）：
- 週期放寬：state 900→1800s、portfolio／ledger 3600→7200s（各欄位 `as_of`／
  `fetched_at` 仍是抓取當下的真實時間，只有「多久抓一次」變慢）——約
  20＋50＋50＝120 weight/分鐘，留 ~180 給 fills。
- 限流器父子 scope（`hl_budget.WeightLimiter` Task 7.4a）：`explore`（父，300）／
  `explore_base`（子，180）／`explore_fills`（子，120）。有 fills 待處理
  （`store.due_count("fills", now) > 0`）時，基礎類別走 `hl_base`（scoped
  `explore_base`，上限 180）、fills 走 `hl_fills`（scoped `explore_fills`，
  保證每分鐘至少擠進一頁 120 weight 的 `userFillsByTime`）；無 fills 待處理時
  基礎類別改走父 scope `hl`（可用滿 300）。`hl_base`／`hl_fills` 皆可為
  `None`（退回 `hl`），維持舊呼叫端（單一 gateway、無父子 scope）的行為不變。
- 領工優先序＋等待加權：`store.claim_due` 的排序公式對等待越久的 job
  「升級」有效 priority（`priority - min(3, floor(wait_s/600))`，每等 10 分鐘
  降一級），避免同 priority 內先到期的 job 被持續插隊的新到期 job 永久排擠。
- 啟動時重排既有 overdue：第一個 tick 對 state／portfolio／ledger 各自呼叫
  `store.rebalance_overdue`，把逾期超過新週期的 job 打散到 `(now, now+period]`
  ——不重排的話，週期改長後這些舊積壓仍會先把新週期的額度占滿。

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
from spark.publicapi.hl_budget import BudgetExhausted, ScopePaused, is_rate_limited, weight_for
from spark.publicapi.hl_explore import ExploreConfig, _roi_sort_key, candidate_addresses

logger = logging.getLogger(__name__)

# Task 7.4b（2026-09-21 使用者裁決，fills 類別級飢餓修法）：一頁 `userFillsByTime`
# 預留的上限權重（與 `hl_budget.ENDPOINT_WEIGHTS["userFillsByTime"]` 同源，工程原則
# 1）——`_fills_available()` 用它判斷 `explore_fills` 保留額度是否夠讓一整頁擠進去。
FILLS_PAGE_WEIGHT = weight_for("userFillsByTime")

# Task 7.4b：非 fills 的四種 kind，領工時一起用 `kinds=` 限定（`claim_due` 的
# IN 子句）——candidates 本身也走這個集合，它不吃 explore_base／explore_fills
# 的區分（`_run_job` 直接用 `leaderboard_source_fn`，不經任何 scoped gateway）。
_BASE_KINDS = ("candidates", "state", "portfolio", "ledger")

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
    ExploreStore，所有 HL 呼叫走 `hl`（必須是 gateway.scoped("explore")，wait_s=0）。

    Task 7.4b：`hl_base`／`hl_fills` 是 `hl_base=gateway.scoped("explore_base")`／
    `hl_fills=gateway.scoped("explore_fills")` 的保留額度視圖，皆可省略（`None`）
    ——省略時基礎類別與 fills 一律退回 `hl`（父 scope），與改動前行為一致，供
    未接父子 scope 的舊呼叫端／測試沿用。"""

    def __init__(self, *, store: ExploreStore, hl, leaderboard_source_fn: Callable[[], dict | None],
                 excluded_fn: Callable[[], set[str]], cfg: ExploreConfig, now_fn, sleep_fn,
                 on_dirty: Callable[[], None], owner: str = "api", lease_s: float = 60.0,
                 hl_base=None, hl_fills=None,
                 candidates_every_s: float = 1800,
                 state_every_s: float = 1800,
                 portfolio_every_s: float = 7200,
                 ledger_every_s: float = 7200,
                 fills_every_s: float = 14400, hot_rank: int = 50, jitter_pct: float = 0.10,
                 rng: Callable[[], float] = random.random,
                 on_tick: Callable[[], None] | None = None):
        self._store = store
        self._hl = hl
        self._hl_base = hl_base
        self._hl_fills = hl_fills
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
        self._first_tick_done = False
        self._ticks = 0
        self._last_tick_at: float | None = None
        self._last_result: str | None = None
        self._results: dict[str, int] = {}
        self._last_candidates_ok_at: float | None = None
        self._candidates_empty_streak = 0
        # Task 7.4b：fills 保留額度的類別感知領工觀測值。
        self._fills_pages_total = 0
        self._last_fills_at: float | None = None
        self._base_scope_in_use = "explore"
        self._rebalanced: dict[str, int] = {}
        # 2026-09-21 主線程裁決：`_fills_available()` 無 limiter 時的交替旗標
        # ——初值 False，第一次呼叫翻成 True（回 120），第二次翻回 False（回 0）。
        self._fallback_turn = False

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
            "fills_pages_total": self._fills_pages_total,
            "last_fills_at": self._last_fills_at,
            "base_scope_in_use": self._base_scope_in_use,
            "rebalanced": dict(self._rebalanced),
        }

    # ---- 內部：一次 tick ----
    def _tick_once(self, now: float) -> str:
        if not self._first_tick_done:
            # <!-- 2026-09-21 複審 W2 -->：旗標在重排**成功之後**才設；重排拋例外時
            # 大聲記錄並在下一 tick 再試（工程原則 #3：關鍵一次性動作不得靜默失敗）。
            try:
                self._rebalanced = self._rebalance_overdue_base_jobs(now)
            except Exception:
                logger.exception("explore scheduler: 逾期 job 重排失敗，下一 tick 重試")
                raise
            self._first_tick_done = True
            logger.warning(
                "explore scheduler: 重排逾期 job（新週期 state=%.0fs portfolio=%.0fs "
                "ledger=%.0fs）：%s", self._state_every_s, self._portfolio_every_s,
                self._ledger_every_s, self._rebalanced)

        if not self._bootstrapped:
            self._bootstrapped = True
            self._store.enqueue("candidates:candidates", None, "candidates", 0, now)
            return "idle"

        # Task 7.4b（2026-09-21 主線程二次裁決）：類別感知的領工——有 fills
        # 待處理且 `explore_fills` 保留額度足夠擠進一整頁時，優先從 fills 領；
        # 否則優先從基礎類別領。但「優先」不是「只」：優先的那一類這一輪如果
        # 根本沒有到期 job（例如非候選地址只有 fills、沒有任何基礎 job），
        # 同一個 tick 改領另一類，不浪費這個 tick——否則會把「沒有競爭對象」
        # 的情境也誤判成節流，讓本來每 tick 都能跑的類別平白變慢一半
        # （builder 回報：這正是舊測試
        # `test_fills_non_candidate_continues_paging_until_done_then_dropped`
        # 被打斷的原因）。`fills_due` 同時決定基礎 job 執行時要不要走受限的
        # `explore_base`（見 `_run_job`）——這個判斷只看「fills 是否待處理」，
        # 不受這裡的 fallback 影響。
        fills_due = self._store.due_count("fills", now) > 0
        self._base_scope_in_use = "explore_base" if fills_due else "explore"
        prefer_fills = fills_due and self._fills_available() >= FILLS_PAGE_WEIGHT
        job = self._store.claim_due(
            now, self._owner, self._lease_s, kinds=("fills",) if prefer_fills else _BASE_KINDS)
        if job is None:
            # fallback：同一 tick 改領另一類。`fills_due` 已經用同一個
            # `next_attempt_at <= now` 條件查過一次——`fills_due is False`
            # 時 `claim_due(kinds=("fills",))` 保證也會是 `None`（單一
            # thread、沒有其他 writer 插隊），省下一次確定沒有意義的查詢，
            # 讓「兩類都沒有到期 job」的 idle tick 仍只查一次（不因新增的
            # fallback 機制而變成兩次——這正是舊測試
            # `test_run_forever_sleeps_after_unexpected_exception` 用
            # claim_due 呼叫次數對應 tick 次數時會被打亂的地方）。
            if prefer_fills:
                job = self._store.claim_due(now, self._owner, self._lease_s, kinds=_BASE_KINDS)
            elif fills_due:
                job = self._store.claim_due(now, self._owner, self._lease_s, kinds=("fills",))
        if job is None:
            return "idle"

        try:
            return self._run_job(job, now, fills_due=fills_due)
        except BudgetExhausted:
            # <!-- 2026-09-21 複審 W3 -->：額度不足不是這個 job 的錯——保留原本的
            # `next_attempt_at`（不推到未來），等待加權才能持續累積、老 job 不會
            # 因為額度緊繃反而被自己的退避重置成「剛到期」。lease 照常釋放。
            self._reschedule(job, min(job.next_attempt_at, now), bump_attempts=False)
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

    def _run_job(self, job: Job, now: float, *, fills_due: bool = False) -> str:
        if job.kind == "candidates":
            return self._run_candidates(job, now)
        # Task 7.4b：fills 待處理時基礎抓取走受限的 `explore_base`（保留額度
        # 給 fills）；沒有 `hl_base`（舊呼叫端／測試未接父子 scope）一律退回 `hl`。
        base_hl = self._hl_base if (fills_due and self._hl_base is not None) else self._hl
        if job.kind == "state":
            return self._run_cache_kind(
                job, now, endpoint="clearinghouseState",
                fetch=lambda addr: base_hl.clearinghouse_state(addr),
                period_s=self._state_every_s)
        if job.kind == "portfolio":
            return self._run_cache_kind(
                job, now, endpoint="portfolio",
                fetch=lambda addr: base_hl.portfolio(addr),
                period_s=self._portfolio_every_s)
        if job.kind == "ledger":
            return self._run_cache_kind(
                job, now, endpoint="ledger",
                fetch=lambda addr: base_hl.non_funding_ledger_updates(addr, 0),
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

        # Task 7.4b：`self._fills_available()` 已檢查（見 `_tick_once`）用
        # `self._hl_fills` 判斷保留額度；這裡沿同一個 gateway 實際發送——兩處
        # 用同一個屬性、同一個 scope，才是同源同基準的判斷（工程原則 1）。
        hl_fills = self._hl_fills if self._hl_fills is not None else self._hl
        page = hl_fills.get_fills_page(job.address, plan.start_ms, plan.end_ms)
        self._fills_pages_total += 1
        self._last_fills_at = now
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

    def _fills_available(self) -> int:
        """`explore_fills` 保留 scope 目前還能預留多少權重（Task 7.4b）——
        用它判斷「值不值得這一輪把工作分給 fills」。刻意只看 `self._hl_fills`
        （不像 `_run_job` 那樣在 `None` 時退回 `self._hl`）。

        2026-09-21 主線程二次裁決：沒有接父子 scope 的舊呼叫端／測試沒有真正
        的限流器可查，只在「基礎類別也有到期 job」時才交替回
        `FILLS_PAGE_WEIGHT`／`0`（`self._fallback_turn`）——沒有基礎 job 在
        排隊時，交替毫無意義，只會把本來每 tick 都能跑的 fills 平白拖慢一半
        （`_tick_once` 的 fallback-claim 已經處理了「優先類別這輪沒 job 就換
        另一類」，這裡的交替只用來處理『兩類都有 job 排隊、無 limiter 時如何
        公平分配』這個更窄的情境）。有真實 limiter（生產接線）時完全不受
        影響，一律照 `limiter.available("explore_fills")` 的真實保留額度判斷。"""
        limiter = getattr(self._hl_fills, "_limiter", None)
        if limiter is not None:
            # <!-- 2026-09-21 複審 W1 -->：用 gateway 自己的 scope 名，不硬編字串
            # ——判斷與實際發送同源（同一個 `_hl_fills`、同一個 scope）。
            return limiter.available(getattr(self._hl_fills, "_scope", "explore_fills"))
        now = self._now()
        base_due = any(self._store.due_count(kind, now) > 0 for kind in _BASE_KINDS)
        if not base_due:
            return FILLS_PAGE_WEIGHT
        self._fallback_turn = not self._fallback_turn
        return FILLS_PAGE_WEIGHT if self._fallback_turn else 0

    def _rebalance_overdue_base_jobs(self, now: float) -> dict[str, int]:
        """第一個 tick 對三個基礎 kind 各自呼叫 `store.rebalance_overdue`
        （Task 7.4b：週期改長之後，舊積壓若不重排會先把新週期的額度占滿）。
        用 default-arg 綁定當下迴圈的 `period`——lambda 直接閉包 `period`
        會在迴圈跑完後統一指到最後一次賦值（ledger 的週期），造成三個 kind
        全部用同一個週期打散。"""
        rebalanced: dict[str, int] = {}
        for kind, period in (("state", self._state_every_s),
                             ("portfolio", self._portfolio_every_s),
                             ("ledger", self._ledger_every_s)):
            rebalanced[kind] = self._store.rebalance_overdue(
                kind, now, period, lambda addr, period=period: _spread(addr, period))
        return rebalanced

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
