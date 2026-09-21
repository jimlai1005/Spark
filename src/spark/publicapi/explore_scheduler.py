"""src/spark/publicapi/explore_scheduler.py
Explore 排行榜背景排程（spec `docs/superpowers/specs/2026-09-20-hl-leaderboard-refactor-spec.md`
§6 頻率／jitter／前 50 優先、§9.1 lease／fencing／dedupe／admission；
plan `docs/superpowers/plans/2026-09-20-explore-rate-limit-refactor.md` Task 3.1／7.9b）。

單一 thread、逐 job 執行；每個 job＝一次 HL 呼叫（或一頁 fills）。所有持久化走
`ExploreStore`；所有 HL 呼叫走建構子傳入的 `hl`（正式接線必須是
`gateway.scoped("explore")`，wait_s=0——本模組不自己等額度，額度不夠就讓位給
下一輪 tick，spec §3 條件「超過預算就 stale、不暴衝」）。

Task 7.9b（2026-09-21 使用者第二輪裁決）：遍歷軌／增量軌分離之後，job kind 從
5 種變成 7 種：`candidates`（priority 0）、`state`（priority 0）、`portfolio`／
`ledger`（priority 1）、`fills`（**增量軌**，前 `hot_rank` 名 priority 2，其餘 3）、
`fills_scan`（**遍歷軌**：`initial` 建立時 priority 2、`partial_rescan` 排程時
priority 3）、`fills_verify`（**核驗遍歷**，遷移產生，只在同一 tick 沒有任何
`candidates`／`state`／`portfolio`／`ledger`／`fills`／`fills_scan` 到期時才領——
嚴格讓位，見 `_tick_once`）。`fills`／`fills_scan` 共用 `explore_fills` 保留額度
（`_fills_available()`），`fills_verify` 領工前也要求同樣的額度充足（避免用
「反正閒著」的錯覺擠掉真正該優先的核驗以外預算）。

留存邊界探測（B4）從舊版「deferred 佇列＋每輪 complete 收尾嘗試」改為**純 DB
推導＋9:1 節流**：候選由 `ExploreStore.next_probe_candidate()` 查詢（`fills_sync
.scan_id` 指向的那次遍歷 `result==complete` 且 `reason` 仍是門檻推論），
`_tick_once` 每個 tick 開頭先問「這次機會給 fills-like（`fills`／`fills_scan`）
還是探測」：雙方都有積壓時，探測每 9 次 fills-like 服務才輪到 1 次
（`_fills_served_since_probe` 計數器）；fills-like 這一側沒有到期工作時，探測
可以直接借用這次機會（不必空等）。探測與工作領取互斥（同一個 tick 只做其中
一件），這樣才能用「tick 次數」直接控制服務比例（見 `test_explore_scheduler.py`
的 9:1 行為測試）。回寫走 CAS（`ExploreStore.apply_probe_result`）：探測發出後、
回寫前，若該地址已完成新的一次遍歷（`fills_sync.scan_id` 已指向別的
`scan_id`）→ CAS 落空、計 `probe.stale`，不覆蓋新遍歷的結論。

容量估算與 Task 7.4b 修正（2026-09-21 使用者裁決：正式機證實 fills 類別級飢餓——
嚴格優先級＋沒有為 fills 一頁 120 weight 的大請求保留額度，state/portfolio/ledger
的穩態流量長期貼著 300 上限，fills 一小時拿不到一次額度）：
- 限流器父子 scope（`hl_budget.WeightLimiter` Task 7.4a）：`explore`（父，300）／
  `explore_base`（子，180）／`explore_fills`（子，120）。有 fills-like 待處理時，
  基礎類別走 `hl_base`（scoped `explore_base`，上限 180）、fills-like 走
  `hl_fills`（scoped `explore_fills`，保證每分鐘至少擠進一頁 120 weight 的
  `userFillsByTime`）；無 fills-like 待處理時基礎類別改走父 scope `hl`
  （可用滿 300）。
- 領工優先序＋等待加權：`store.claim_due` 的排序公式對等待越久的 job
  「升級」有效 priority，避免同 priority 內先到期的 job 被持續插隊的新到期
  job 永久排擠。
- 啟動時重排既有 overdue：第一個 tick 對 state／portfolio／ledger 各自呼叫
  `store.rebalance_overdue`。

單一 job 失敗不影響其他 job：例外分類（`BudgetExhausted`／`ScopePaused`／429／
transient／其他）各自決定下一次 `next_attempt_at`，thread 本身只在
`run_forever` 層被保護——不因單一 tick 的未預期例外死掉（spec §3 條件五）。
"""
from __future__ import annotations

import dataclasses
import logging
import random
import threading
from decimal import Decimal
from typing import Callable

from spark.publicapi.explore_fills_sync import (DEFAULT_FILLS_PERIOD_S, PARAMS_FP,
                                                PARTIAL_RESCAN_AFTER_MS, apply_incremental_page,
                                                apply_scan_page, fresh_scan_window,
                                                plan_incremental, plan_scan, validate_page)
from spark.publicapi.explore_store import (REASON_COUNT_BELOW_RETENTION_THRESHOLD,
                                           REASON_PROBE_NO_EARLIER_FILLS,
                                           REASON_RETENTION_BOUNDARY_VERIFIED, ExploreStore,
                                           Job)
from spark.publicapi.hl_budget import BudgetExhausted, ScopePaused, is_rate_limited, weight_for
from spark.publicapi.hl_explore import ExploreConfig, _roi_sort_key, candidate_addresses

logger = logging.getLogger(__name__)

# Task 7.4b（2026-09-21 使用者裁決，fills 類別級飢餓修法）：一頁 `userFillsByTime`
# 預留的上限權重（與 `hl_budget.ENDPOINT_WEIGHTS["userFillsByTime"]` 同源，工程原則
# 1）——`_fills_available()` 用它判斷 `explore_fills` 保留額度是否夠讓一整頁擠進去。
FILLS_PAGE_WEIGHT = weight_for("userFillsByTime")

# Task 7.5 點 3（留存邊界探測）：查詢窗口起點之前一天——與
# `explore_fills_sync._DAY_MS` 同數值，不 import 該私有名稱（避免跨模組耦合
# 私有常數），供 `_run_probe` 算探測窗口用。
_PROBE_WINDOW_MS = 86_400_000

# Task 7.9b B4：探測與 fills-like（`fills`／`fills_scan`）不可各半——雙方都有
# 積壓時每服務這麼多次 fills-like 才輪到一次探測（使用者裁決：9:1）。
PROBE_SERVE_RATIO = 9

# Task 7.4b：非 fills 的四種 kind，領工時一起用 `kinds=` 限定（`claim_due` 的
# IN 子句）——candidates 本身也走這個集合，它不吃 explore_base／explore_fills
# 的區分（`_run_job` 直接用 `leaderboard_source_fn`，不經任何 scoped gateway）。
_BASE_KINDS = ("candidates", "state", "portfolio", "ledger")

# Task 7.9b：增量軌／遍歷軌共用同一份 `explore_fills` 保留額度（B3：「`fills`
# （增量）與 `fills_scan` 都走 `explore_fills` 保留額度」）。
_FILLS_LIKE_KINDS = ("fills", "fills_scan")

# Task 7.9a A2（2026-09-21 使用者裁決）：暫時性錯誤（連線／逾時／5xx）連續
# 達到這麼多次「實際嘗試」→ 改隔離 24 小時，不再無限期每次退避封頂 900 秒
# 後仍持續重試同一個地址。只計「真的發送過一次請求且失敗」的暫時性錯誤——
# `BudgetExhausted`／`ScopePaused`（既有 `bump_attempts=False`）與 429 都不算
# 一次嘗試，見 `_tick_once` 的例外分類。
MAX_JOB_ATTEMPTS = 8

# 準入上限：`refresh_job` 總數 ≤ ADMISSION_MULTIPLIER × active 候選數（2026-09-20
# 主線程裁決：原 4× 因新增 ledger job 而放寬為 5×；Task 7.9b 新增
# `fills_scan`／`fills_verify` 兩種 kind，但 `fills_verify` 只在遷移／partial
# 復原時短暫出現，不常駐——維持 5× 不變，觀察正式機準入命中率再決定要不要
# 進一步放寬，見本次派工報告）。
ADMISSION_MULTIPLIER = 5

# kind → endpoint_cache 的 endpoint 名稱（quarantine 時 `put_cache_error` 用；
# `fills`／`fills_scan`／`fills_verify` 不在這裡——它們的錯誤落地在
# `fills_sync.last_error`／`fills_scan.last_error`，見 `set_sync_error`／
# `set_scan_error`）。
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
    ——省略時基礎類別與 fills-like 一律退回 `hl`（父 scope），與改動前行為一致，
    供未接父子 scope 的舊呼叫端／測試沿用。"""

    def __init__(self, *, store: ExploreStore, hl, leaderboard_source_fn: Callable[[], dict | None],
                 excluded_fn: Callable[[], set[str]], cfg: ExploreConfig, now_fn, sleep_fn,
                 on_dirty: Callable[[], None], owner: str = "api", lease_s: float = 60.0,
                 hl_base=None, hl_fills=None,
                 candidates_every_s: float = 1800,
                 state_every_s: float = 1800,
                 portfolio_every_s: float = 7200,
                 ledger_every_s: float = 7200,
                 # Task 7.9a 補（2026-09-21 主線程裁決）：預設值 import
                 # `explore_fills_sync.DEFAULT_FILLS_PERIOD_S`（單一來源），
                 # 不得在這裡另寫一份秒數字面值——生產路徑一律由 `run_api.py`
                 # 顯式傳入 `cfg.explore_fills_period_s`，這個預設值只給沒有
                 # 接 config 的呼叫端／測試用。
                 fills_every_s: float = DEFAULT_FILLS_PERIOD_S, hot_rank: int = 50,
                 jitter_pct: float = 0.10,
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
        # Task 7.9b B4：留存邊界探測的觀測計數器與 9:1 節流計數器。
        self._probe_total = 0
        self._probe_verified = 0
        self._probe_empty = 0
        self._probe_failed = 0
        self._probe_stale = 0
        self._fills_served_since_probe = 0
        # Task 7.9a A2：連續暫時性失敗達到 `MAX_JOB_ATTEMPTS` 而被隔離的次數
        # （與語意錯誤的立即隔離分開計，見 `_quarantine` 的 `max_attempts` 參數）。
        self._quarantined_max_attempts = 0
        # Task 7.9a A3：`on_dirty` callback 拋例外的次數（`_notify_dirty` 吞例外
        # 後計數）——job 本身不因此遺失，這個計數器讓 callback 本身壞掉這件事
        # 變成可觀測（health 可見）。
        self._dirty_errors = 0

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
            # Task 7.9a A2／A3：與 `probe` 一樣經 `**scheduler.status()` 自然
            # 出現在 `/api/ops/health` 的 `explore_refresh`。
            "quarantined_max_attempts": self._quarantined_max_attempts,
            "dirty_errors": self._dirty_errors,
            # Task 7.9b B4：留存邊界探測觀測值，經 app.py `**scheduler.status()`
            # 自然出現在 `/api/ops/health` 的 `explore_refresh`。
            "probe": {
                "total": self._probe_total,
                "verified": self._probe_verified,
                "empty": self._probe_empty,
                "failed": self._probe_failed,
                "stale": self._probe_stale,
                "candidates": self._store.count_probe_candidates(),
            },
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

        fills_like_due = any(self._store.due_count(k, now) > 0 for k in _FILLS_LIKE_KINDS)

        # Task 7.9b B4：9:1 節流——每個 tick 先決定這次機會是探測還是job 領取，
        # 兩者互斥（同一 tick 只做其中一件），這樣「tick 次數」才能直接對應
        # 服務比例。額度不足（`_fills_available() < FILLS_PAGE_WEIGHT`）→
        # 不查候選，直接跳過（省一次無意義的 DB 查詢）。
        if self._fills_available() >= FILLS_PAGE_WEIGHT:
            candidate = self._store.next_probe_candidate()
            if candidate is not None:
                do_probe = (not fills_like_due) or (
                    self._fills_served_since_probe >= PROBE_SERVE_RATIO)
                if do_probe:
                    self._fills_served_since_probe = 0
                    self._run_probe(candidate, now)
                    return "ran:probe"

        # Task 7.4b（2026-09-21 主線程二次裁決）：類別感知的領工——有
        # fills-like（`fills`／`fills_scan`）待處理且 `explore_fills` 保留額度
        # 足夠擠進一整頁時，優先從 fills-like 領；否則優先從基礎類別領。但
        # 「優先」不是「只」：優先的那一類這一輪如果根本沒有到期 job，同一個
        # tick 改領另一類，不浪費這個 tick。
        self._base_scope_in_use = "explore_base" if fills_like_due else "explore"
        prefer_fills = fills_like_due and self._fills_available() >= FILLS_PAGE_WEIGHT
        job = self._store.claim_due(
            now, self._owner, self._lease_s,
            kinds=_FILLS_LIKE_KINDS if prefer_fills else _BASE_KINDS)
        if job is None:
            if prefer_fills:
                job = self._store.claim_due(now, self._owner, self._lease_s, kinds=_BASE_KINDS)
            elif fills_like_due:
                job = self._store.claim_due(
                    now, self._owner, self._lease_s, kinds=_FILLS_LIKE_KINDS)
        if job is None:
            # Task 7.9b B3：`fills_verify`（核驗遍歷）嚴格讓位——只在本 tick
            # base／fills-like 都沒有到期 job 時才嘗試領（等待加權對它一樣
            # 適用，但因為只在「沒有其他事可做」時才查，不會搶到本該給其他
            # 類別的 tick）。同樣要求額度足夠一整頁；先用 `due_count` 快速判斷
            # 有沒有 verify job 到期，沒有就不必多打一次 `claim_due`（idle tick
            # 不該平白多一次 DB 呼叫）。
            if (self._store.due_count("fills_verify", now) > 0
                    and self._fills_available() >= FILLS_PAGE_WEIGHT):
                job = self._store.claim_due(
                    now, self._owner, self._lease_s, kinds=("fills_verify",))
        if job is None:
            return "idle"

        try:
            result = self._run_job(job, now, fills_due=fills_like_due)
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
                # Task 7.9a A2（使用者修正）：429 走共享 cooldown（`note_429`／
                # `ScopePaused` 那一路），不算一次「實際嘗試」——不推進 attempts，
                # 與 `BudgetExhausted`／`ScopePaused` 既有的 `bump_attempts=False`
                # 一致。
                self._reschedule(job, now + 60, err=repr(e), bump_attempts=False)
                return "rate_limited"
            if isinstance(e, (ConnectionError, TimeoutError)) or _is_5xx(e):
                attempts = job.attempts + 1
                if attempts >= MAX_JOB_ATTEMPTS:
                    # Task 7.9a A2：第 `MAX_JOB_ATTEMPTS` 次實際嘗試仍失敗 → 隔離
                    # 24 小時，不再重試計畫中的退避。`job.attempts`（隔離前一刻
                    # 讀到的值）維持不變（`_quarantine(max_attempts=True)` 不
                    # 推進），隔離期滿只給一次恢復嘗試的額度。
                    self._quarantine(job, now, e, max_attempts=True)
                    return "quarantined"
                next_at = now + min(30 * (2 ** attempts), 900) + self._rng() * 10
                self._reschedule(job, next_at, err=repr(e))
                return "retry"
            self._quarantine(job, now, e)
            return "quarantined"

        if job.kind in _FILLS_LIKE_KINDS or job.kind == "fills_verify":
            self._fills_served_since_probe += 1
        return result

    def _run_job(self, job: Job, now: float, *, fills_due: bool = False) -> str:
        if job.kind == "candidates":
            return self._run_candidates(job, now)
        # Task 7.4b：fills-like 待處理時基礎抓取走受限的 `explore_base`（保留
        # 額度給 fills-like）；沒有 `hl_base`（舊呼叫端／測試未接父子 scope）
        # 一律退回 `hl`。
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
            return self._run_increment(job, now)
        if job.kind == "fills_scan":
            return self._run_scan(job, now, verify=False)
        if job.kind == "fills_verify":
            return self._run_scan(job, now, verify=True)
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
        # Task 7.9b B1：新地址同時建立增量軌＋初始遍歷（`bootstrap_address_fills`
        # 只在該地址第一次出現時真的建列，回 `True`——候選抖動重新入池不會
        # 重建，見該方法 docstring）；`fills_scan` job 一律入列（`enqueue` 本身
        # 冪等，已存在只會 MIN 優先級／時間，不會重複）。
        now_ms = int(now * 1000)
        window_start_ms, window_end_ms = fresh_scan_window(now_ms)
        self._store.bootstrap_address_fills(
            address, now, window_start_ms=window_start_ms, window_end_ms=window_end_ms,
            params_fp=PARAMS_FP)
        self._store.enqueue(self._key(address, "fills_scan"), address, "fills_scan",
                            fills_priority, now + _spread(address, self._fills_every_s))

    def _run_cache_kind(self, job: Job, now: float, *, endpoint: str, fetch, period_s: float) -> str:
        payload = fetch(job.address)
        refresh_after = now + self._jit(period_s)
        self._store.put_cache_ok(job.address, endpoint, payload, now, refresh_after)
        self._complete(job)
        # Task 7.9a A3（callback 不丟工作）：store 寫入＋續排都先做完，
        # `_notify_dirty()`（吞例外＋計數，見該方法）放在最後一步——`on_dirty`
        # 拋例外不得讓已經 `_complete` 的 job 漏排（工程原則 3 的反向：關鍵動作
        # 不因錦上添花的通知失敗而跟著失敗）。
        active = self._store.is_active(job.address)
        if active:
            # Task 3.5 B(2)（C1 修法）：候選已退池，續排前先檢查——不然這個
            # job 會無條件永遠自我續排，預算持續漏給非候選、`refresh_job` 只增
            # 不減，最終觸發準入上限讓新候選拿不到 job、也讓 `purge` 的
            # `NOT EXISTS(refresh_job)` 條件永久卡住。
            self._store.enqueue(job.key, job.address, job.kind, job.priority, refresh_after)
        self._notify_dirty()
        return f"ran:{job.kind}" if active else "dropped"

    def _run_increment(self, job: Job, now: float) -> str:
        """增量軌 job（kind='fills'）——Task 7.9b B2：只延伸
        `fills_sync.synced_through_ms`，完全不判定 `completeness`／`reason`
        （那是遍歷軌的事，見 `_run_scan`）。重掃期間增量照常前進（B7 (i)）
        ——本方法完全不查詢 `fills_scan`，兩軌互不依賴。"""
        st = self._store.get_sync(job.address)
        if st is None:
            # 防禦：`bootstrap_address_fills` 理論上保證這裡恆非 None（見
            # `_enqueue_address_jobs`）；地址仍存在 job 但 fills_sync 列缺席
            # 代表 store 資料被外部竄改，跳過不重試。
            self._complete(job)
            return "dropped"
        plan = plan_incremental(st, now_ms=int(now * 1000), period_s=self._fills_every_s)
        if plan.is_noop:
            self._complete(job)
            if not self._store.is_active(job.address):
                return "dropped"
            next_at = max(plan.next_due_ms / 1000, now + self._jit(60.0))
            self._store.enqueue(job.key, job.address, "fills", job.priority, next_at)
            return "ran:fills"

        hl_fills = self._hl_fills if self._hl_fills is not None else self._hl
        page = hl_fills.get_fills_page(job.address, plan.start_ms, plan.end_ms)
        self._fills_pages_total += 1
        self._last_fills_at = now
        res = apply_incremental_page(plan, page, now_ms=int(now * 1000))
        self._store.insert_fills_page(job.address, res.accepted, res.state)
        if not res.done:
            self._reschedule(job, now, bump_attempts=False)
            self._notify_dirty()
            return "ran:fills"
        self._complete(job)
        active = self._store.is_active(job.address)
        if active:
            next_at = now + self._jit(self._fills_every_s)
            self._store.enqueue(job.key, job.address, "fills", job.priority, next_at)
        self._notify_dirty()
        return "ran:fills" if active else "dropped"

    def _run_scan(self, job: Job, now: float, *, verify: bool) -> str:
        """遍歷軌 job（kind='fills_scan'／'fills_verify'）——Task 7.9b B2／B3：
        沒有進行中的 scan 時，依 `verify` 與目前增量軌 `completeness` 決定新
        scan 的 `kind`（`verify` 一律建 `verify`；`backfilling`／缺列 → 建
        `initial`；否則（`partial` 復原到期後排程進來的後續 job） → 建
        `partial_rescan`）——三種 `kind` 的頁面套用邏輯相同，只有窗口與收尾
        後的排程動作不同（`partial` 收尾要自動排下一次 `partial_rescan`，見
        下方）。"""
        result_kind = "fills_verify" if verify else "fills_scan"
        scan = self._store.get_active_scan(job.address)
        if scan is None:
            now_ms = int(now * 1000)
            window_start_ms, window_end_ms = fresh_scan_window(now_ms)
            if verify:
                kind = "verify"
            else:
                st = self._store.get_sync(job.address)
                kind = "initial" if (st is None or st.completeness == "backfilling") \
                    else "partial_rescan"
            scan = self._store.create_scan(
                job.address, kind=kind, window_start_ms=window_start_ms,
                window_end_ms=window_end_ms, cursor_ms=window_start_ms, started_at=now,
                params_fp=PARAMS_FP)

        plan = plan_scan(scan)
        hl_fills = self._hl_fills if self._hl_fills is not None else self._hl
        page = hl_fills.get_fills_page(job.address, plan.start_ms, plan.end_ms)
        self._fills_pages_total += 1
        self._last_fills_at = now
        res = apply_scan_page(plan, page, now_ms=int(now * 1000))
        if not res.done:
            self._store.insert_scan_page(job.address, res.accepted, res.scan)
            self._reschedule(job, now, bump_attempts=False)
            self._notify_dirty()
            return f"ran:{result_kind}"

        finished_scan = dataclasses.replace(res.scan, finished_at=now)
        self._store.complete_scan(job.address, res.accepted, finished_scan)
        self._complete(job)
        active = self._store.is_active(job.address)
        if active and not verify and finished_scan.result == "partial":
            # Task 7.9b B3：`partial` 的唯一復原路徑——排一次
            # `partial_rescan`，24 小時後再整窗重掃一次（同一個 `fills_scan`
            # job key，`_run_scan` 屆時因為沒有進行中的 scan 且增量軌
            # completeness=='partial' 而建出 `partial_rescan`）。
            self._store.enqueue(self._key(job.address, "fills_scan"), job.address,
                                "fills_scan", 3, now + PARTIAL_RESCAN_AFTER_MS)
        self._notify_dirty()
        return f"ran:{result_kind}" if active else "dropped"

    # ---- 內部：例外收尾 ----
    def _quarantine(self, job: Job, now: float, exc: Exception, *,
                    max_attempts: bool = False) -> None:
        err = repr(exc)
        if max_attempts:
            # Task 7.9a A2：字首標記讓 ops／測試分得出「連續暫時性失敗達到
            # MAX_JOB_ATTEMPTS」與既有的語意錯誤立即隔離（下面 `bump_attempts`
            # 分支不同：語意錯誤仍照舊遞增 attempts，這裡刻意不再推進——
            # `job.attempts` 在被判定達到上限之前已經是 `MAX_JOB_ATTEMPTS - 1`
            # ，隔離期滿只釋放一次恢復嘗試，失敗一次就立即再達到上限重新隔離）。
            err = f"max_attempts:{err}"
            self._quarantined_max_attempts += 1
        endpoint = _ENDPOINT_BY_KIND.get(job.kind)
        if endpoint is not None:
            self._store.put_cache_error(job.address, endpoint, err, now, now + 86400)
        elif job.kind == "fills":
            self._store.set_sync_error(job.address, err, now)
        elif job.kind in ("fills_scan", "fills_verify"):
            scan = self._store.get_active_scan(job.address)
            if scan is not None:
                self._store.set_scan_error(scan.scan_id, err)
        else:
            logger.error("explore scheduler: %s job 隔離、無對應快取欄位可寫（%s）",
                         job.kind, err)
        self._reschedule(job, now + 86400, err=err, bump_attempts=not max_attempts)

    def _run_probe(self, candidate: tuple[str, str], now: float) -> None:
        """留存邊界探測（Task 7.9b B4）：`candidate=(address, scan_id)` 來自
        `ExploreStore.next_probe_candidate()`（純 DB 推導，重啟後照常運作，
        B7 (vii)）。查詢 `[scan.window_start_ms - 1 天, scan.window_start_ms - 1]`
        是否仍有可查成交——回 >=1 筆合法成交代表 HL 的實際留存邊界早於這次
        遍歷的窗口起點，升級 reason 為 `REASON_RETENTION_BOUNDARY_VERIFIED`；
        回空頁代表已探過、無法升級，記為 `REASON_PROBE_NO_EARLIER_FILLS`（該
        reason 碼天然被 `next_probe_candidate` 的查詢條件排除，每次遍歷至多
        探到有結論為止）。

        回寫用 `ExploreStore.apply_probe_result` 的雙 CAS（B7 (ii)）：探測發出
        後、回寫前，若該地址已完成新的一次遍歷（`scan_id` 改變）→ CAS 落空、
        計 `probe.stale`，不覆蓋新遍歷的結論、也不重試（下一輪 tick 若新遍歷
        仍以「門檻推論」收尾，會自然成為新的探測候選）。"""
        address, scan_id = candidate
        scan = self._store.get_scan(scan_id)
        if scan is None:
            self._probe_failed += 1
            return
        probe_start = scan.window_start_ms - _PROBE_WINDOW_MS
        probe_end = scan.window_start_ms - 1
        hl_fills = self._hl_fills if self._hl_fills is not None else self._hl
        try:
            page = hl_fills.get_fills_page(address, probe_start, probe_end)
        except (BudgetExhausted, ScopePaused):
            return
        except Exception as e:  # noqa: BLE001 — 唯一的分類點，見上方 docstring
            if is_rate_limited(e):
                return
            logger.warning(
                "explore scheduler：留存邊界探測失敗 address=%s window=[%d,%d]: %r",
                address, probe_start, probe_end, e)
            self._probe_total += 1
            self._probe_failed += 1
            return
        self._probe_total += 1
        invalid_reason = validate_page(page, probe_start, probe_end)
        if invalid_reason is not None:
            logger.warning(
                "explore scheduler：留存邊界探測回應不合法 address=%s window=[%d,%d] "
                "reason=%s", address, probe_start, probe_end, invalid_reason)
            self._probe_failed += 1
            return
        new_reason = REASON_RETENTION_BOUNDARY_VERIFIED if page else REASON_PROBE_NO_EARLIER_FILLS
        ok = self._store.apply_probe_result(
            scan_id, address, old_reason=REASON_COUNT_BELOW_RETENTION_THRESHOLD,
            new_reason=new_reason)
        if not ok:
            self._probe_stale += 1
            return
        self._notify_dirty()
        if page:
            self._probe_verified += 1
        else:
            self._probe_empty += 1

    def _notify_dirty(self) -> None:
        """Task 7.9a A3：`on_dirty`（publisher 用來決定要不要提前組版）是錦上
        添花的通知，不是關鍵寫入路徑——所有呼叫點都已經把 store 寫入與
        `_complete`／續排／enqueue 做完才呼叫這裡，例外只吞不逸出（工程原則 3
        的反向：關鍵動作不因這個失敗而跟著失敗），計 `dirty_errors`
        （`status()`／health 可見）供營運端發現 callback 本身壞掉。"""
        try:
            self._on_dirty()
        except Exception:
            logger.error("explore scheduler: on_dirty callback 拋出例外", exc_info=True)
            self._dirty_errors += 1

    def _paused_remaining_s(self) -> float:
        limiter = getattr(self._hl, "_limiter", None)
        if limiter is None:
            return 60.0
        return limiter.snapshot()["paused_remaining_s"].get("explore", 60.0)

    def _fills_available(self) -> int:
        """`explore_fills` 保留 scope 目前還能預留多少權重（Task 7.4b）——
        用它判斷「值不值得這一輪把工作分給 fills-like」。刻意只看
        `self._hl_fills`（不像 `_run_job` 那樣在 `None` 時退回 `self._hl`）。

        2026-09-21 主線程二次裁決：沒有接父子 scope 的舊呼叫端／測試沒有真正
        的限流器可查，只在「基礎類別也有到期 job」時才交替回
        `FILLS_PAGE_WEIGHT`／`0`（`self._fallback_turn`）——沒有基礎 job 在
        排隊時，交替毫無意義，只會把本來每 tick 都能跑的 fills-like 平白拖慢
        一半。有真實 limiter（生產接線）時完全不受影響，一律照
        `limiter.available("explore_fills")` 的真實保留額度判斷。"""
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
