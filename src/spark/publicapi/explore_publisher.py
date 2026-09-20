"""src/spark/publicapi/explore_publisher.py
Explore 榜單的漸進發布層（spec `docs/superpowers/specs/2026-09-20-hl-leaderboard-refactor-spec.md`
§9.2；plan `docs/superpowers/plans/2026-09-20-explore-rate-limit-refactor.md` Task 4.1）。

`ExploreScheduler`（Task 3.1）把每地址的 portfolio／state／ledger／fills 持續寫進
`ExploreStore`（SQLite），但 `ExploreIndex.query()`（讀路徑）只端出**上一次成功發布**
的一批 `ExploreRow`——本模組是兩者之間唯一的橋接：

- `compose_rows`：純函式，只讀 `ExploreStore`（零網路），把目前 store 裡的資料合成一批
  `ExploreRow`（`hl_explore.enrich_candidate` 的組裝與 gating 邏輯照舊沿用，唯一差別是
  這裡的 `portfolio_raw`／`ch_state` 可能是 `None`——某個候選可能還沒被 scheduler
  enrich 過，見 `enrich_candidate` Task 4.1 段）。
- `ExplorePublisher`：`mark_dirty`／`maybe_publish` 兩段式節流——scheduler 每次寫入後
  呼叫 `mark_dirty()`，scheduler thread 每 tick 末呼叫 `maybe_publish()`；每分鐘至多
  換版一次（`min_interval_s`，spec §9.2：有變更就發布、不等 300 人全部完成，但也不必
  每個 tick 都重算一次全池）。`compose_rows` 或落快照途中任何例外 → 記錄失敗、
  **保留 `ExploreIndex` 目前正在服務的版本**（fail-open，同 `build_sync` 既有語意），
  不讓一次瞬間的 store 讀取失敗把已經在服務的榜單打掉。
"""
from __future__ import annotations

import logging
from collections import Counter

from spark.publicapi.explore_store import ExploreStore
from spark.publicapi.hl_explore import (FILLS_WINDOW_DAYS, ExploreConfig, ExploreIndex,
                                        ExploreRow, _apply_tags, dump_snapshot,
                                        enrich_candidate)

logger = logging.getLogger(__name__)


def compose_rows(store: ExploreStore, *, now: float, cfg: ExploreConfig,
                 fills_window_days: int = FILLS_WINDOW_DAYS) -> tuple[list[ExploreRow], dict]:
    """純函式：`store` 目前的資料 → `(rows, meta)`。零網路——只讀
    `store.active_candidates()`／`get_cache`／`get_fills`／`get_sync`。

    每個 active 候選一律出列（`enrich_candidate` 的 `portfolio_raw`／`ch_state`
    可為 `None`——尚未被 scheduler enrich 過的候選也要出現在榜單裡，`windows`
    全 `None`／`live_days` 為 `None`，是「分析待完成」的誠實狀態，不是 0，見
    `enrich_candidate` docstring）；只有 `enrich_candidate` 既有的 gating（`portfolio_raw`
    非 `None` 但 `month`／`allTime` 視窗算不出）才會整列跳過。

    `meta`：`published_at`（＝`now`）、`candidates`（本輪掃描的候選數）、
    `with_portfolio`（其中有 portfolio 快取的數量）、`coverage_counts`
    （`Counter`→`dict`，各 `fills_coverage.state` 的列數）、`as_of_oldest`
    （所有列 `as_of` 三鍵中最舊的非 `None` 時間戳，皆缺 → `None`）。
    """
    now_ms = int(now * 1000)
    window_ms = fills_window_days * 86400_000
    candidates = store.active_candidates()

    rows: list[ExploreRow] = []
    coverage_counter: Counter = Counter()
    as_of_values: list[float] = []
    with_portfolio = 0

    for c in candidates:
        pf = store.get_cache(c.address, "portfolio")
        st = store.get_cache(c.address, "clearinghouseState")
        portfolio_raw = pf.payload if pf is not None else None
        ch_state = st.payload if st is not None else None
        fills = store.get_fills(c.address, now_ms - window_ms, now_ms)
        sync = store.get_sync(c.address)

        coverage = {
            "state": sync.completeness if sync is not None else "backfilling",
            "observed_from": sync.observed_from_ms if sync is not None else None,
            "observed_to": sync.observed_to_ms if sync is not None else None,
            "reason": sync.reason if sync is not None else None,
        }
        as_of = {
            "portfolio": pf.fetched_at if pf is not None else None,
            "state": st.fetched_at if st is not None else None,
            "fills": sync.updated_at if sync is not None else None,
        }

        row = enrich_candidate(
            c.address, c.display_name, portfolio_raw, fills, ch_state,
            fills_truncated=(coverage["state"] != "complete"),
            as_of=as_of, fills_coverage=coverage)
        if row is None:
            continue

        rows.append(row)
        coverage_counter[coverage["state"]] += 1
        if portfolio_raw is not None:
            with_portfolio += 1
        as_of_values.extend(v for v in as_of.values() if v is not None)

    rows = _apply_tags(rows, cfg)
    meta = {
        "published_at": now,
        "candidates": len(candidates),
        "with_portfolio": with_portfolio,
        "coverage_counts": dict(coverage_counter),
        "as_of_oldest": min(as_of_values) if as_of_values else None,
    }
    return rows, meta


class ExplorePublisher:
    """`ExploreIndex.set_published` 的節流呼叫端。`mark_dirty()` 由 scheduler
    在任何一次寫入（portfolio/state/fills/candidates）後呼叫；`maybe_publish()`
    由 scheduler thread 每 tick 末呼叫（見 Task 3.4 接線，本 task 只負責本類別
    本身，不接線）。"""

    def __init__(self, *, store: ExploreStore, index: ExploreIndex, cfg: ExploreConfig,
                now_fn, snapshot_path: str | None, min_interval_s: float = 60.0):
        self._store = store
        self._index = index
        self._cfg = cfg
        self._now_fn = now_fn
        self._snapshot_path = snapshot_path
        self._min_interval_s = min_interval_s

        self._dirty = False
        self._last_published_at: float | None = None
        self._publishes = 0
        self._failures = 0
        self._last_error: str | None = None

    def mark_dirty(self) -> None:
        self._dirty = True

    def maybe_publish(self, *, force: bool = False) -> bool:
        """未 dirty 且非強制 → `False`（沒有東西可發布）。距上次發布不足
        `min_interval_s` 且非強制 → `False`（節流，spec §9.2：每分鐘至多一次）。
        否則 `compose_rows` → `ExploreIndex.set_published` → （有設
        `snapshot_path` 才）`dump_snapshot`；任一步例外 → 記錄失敗次數與訊息、
        **不清 `_dirty`**（下次 tick 會重試）、保留 `ExploreIndex` 目前版本
        （不呼叫 `set_published`），回 `False`。成功 → 清 `_dirty`、更新
        `_last_published_at`、`_publishes+=1`、回 `True`。"""
        if not self._dirty and not force:
            return False
        now = self._now_fn()
        if (not force and self._last_published_at is not None
                and now - self._last_published_at < self._min_interval_s):
            return False
        try:
            rows, meta = compose_rows(self._store, now=now, cfg=self._cfg)
            self._index.set_published(rows, meta)
            if self._snapshot_path is not None:
                dump_snapshot(self._snapshot_path, rows=rows, built_at=meta["published_at"],
                             total_scanned=meta["candidates"])
        except Exception as e:  # noqa: BLE001 — 展示端點：一次發布失敗不得中斷 scheduler
            logger.error("explore publisher：發布失敗，保留舊版: %r", e)
            self._failures += 1
            self._last_error = repr(e)
            return False
        self._dirty = False
        self._last_published_at = now
        self._publishes += 1
        return True

    def status(self) -> dict:
        """`/api/ops/health` 揭露用（Task 3.4／5.1）：`last_published_at`、
        `dirty`（尚有未發布的變更）、`publishes`／`failures` 累計次數、
        `last_error`（最近一次失敗的訊息，從未失敗過 → `None`）。"""
        return {"last_published_at": self._last_published_at, "dirty": self._dirty,
               "publishes": self._publishes, "failures": self._failures,
               "last_error": self._last_error}
