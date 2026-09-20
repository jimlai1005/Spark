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
  **保留 `ExploreIndex` 目前正在服務的版本**（fail-open，同既有的展示端點語意），
  不讓一次瞬間的 store 讀取失敗把已經在服務的榜單打掉。
"""
from __future__ import annotations

import json
import logging
import shutil
from collections import Counter
from pathlib import Path

from spark.publicapi.explore_store import ExploreStore
from spark.publicapi.hl_explore import (FILLS_WINDOW_DAYS, ExploreConfig, ExploreIndex,
                                        ExploreRow, _apply_tags, dump_snapshot,
                                        enrich_candidate)

logger = logging.getLogger(__name__)


def _gate_reason(with_pf: int, n: int, rows: list | None, *, ratio: float) -> str | None:
    """發布門檻共用判斷式（Task 3.7 A，C 修法：門檻量的是輸入 `with_pf`／`n`，
    換版看的是 `compose_rows` 輸出 `rows`，兩者不同源會漏掉「輸入端看似齊全、
    compose 卻整批丟列」的情況——HL portfolio 結構一變、`enrich_candidate`
    整列丟棄就是這個形狀）。`with_pf == 0` 或 `with_pf < ratio * max(n, 1)`
    （`n == 0` 時 `with_pf` 必為 0，已被前一條擋下，`max(n, 1)` 只是避免除以
    零）視為門檻不通過；`rows` 非 `None` 時空列表也視為不通過——這一項只在
    compose 輸出側才有意義，輸入端預檢當下 compose 還沒跑，傳 `rows=None`
    跳過。回傳 `None` 代表通過門檻。"""
    if with_pf == 0 or with_pf < ratio * max(n, 1):
        return f"{with_pf}/{n}"
    if rows is not None and not rows:
        return f"{with_pf}/{n}"
    return None


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
                now_fn, snapshot_path: str | None, min_interval_s: float = 60.0,
                min_portfolio_ratio: float = 0.8):
        self._store = store
        self._index = index
        self._cfg = cfg
        self._now_fn = now_fn
        self._snapshot_path = snapshot_path
        self._min_interval_s = min_interval_s
        self._min_portfolio_ratio = min_portfolio_ratio

        self._dirty = False
        self._last_published_at: float | None = None
        self._last_attempt_at: float | None = None
        self._publishes = 0
        self._failures = 0
        self._last_error: str | None = None
        self._gate_skips = 0
        self._last_gate: str | None = None

    def mark_dirty(self) -> None:
        self._dirty = True

    def maybe_publish(self, *, force: bool = False) -> bool:
        """未 dirty 且非強制 → `False`（沒有東西可發布）。距上次嘗試（`_last_attempt_at`，
        成功或失敗都算）不足 `min_interval_s` 且非強制 → `False`（節流，spec §9.2：
        每分鐘至多一次）。

        Task 3.7 A（opus 第三輪 Critical 修法）：門檻**改判 compose 輸出**，不再只信
        輸入端的 payload 計數——`with_pf`／`n` 量的是「store 裡有多少候選有 payload」，
        但換版真正端出去的是 `compose_rows` 的 `rows`／`meta`；HL portfolio 結構一變，
        `enrich_candidate` 可能整列丟棄，導致輸入端看起來齊全（`with_pf/n` 過門檻）
        但 compose 出來是空列表——這種形狀舊版會誤放行、把 0 列覆寫上 index 與快照。
        兩道門檻（`_gate_reason`）：
        1. **輸入端預檢**（`rows=None`，只當省算 compose 的捷徑，不是正確性依據）：
           `ExploreIndex` **已經有版本**時，`with_pf = store.count_with_payload("portfolio")`
           與 `n = admission_counts()[1]` 未過門檻 → 直接跳過 compose，記
           `_gate_skips`／`last_gate=f"in:{reason}"`，回 `False`。
        2. **輸出端門檻**（compose 之後、換版之前）：用 `meta["with_portfolio"]`／
           `meta["candidates"]`／`rows` 本身（空列表視為不過）再判一次；未過 →
           `last_gate=f"out:{reason}"`，不寫快照、不呼叫 `set_published`，回 `False`。
        `force=True` 完全繞過兩道門檻（仍會更新 `_last_attempt_at`），供人工強制換版
        使用。首次上線（`index.status()["rows"] is None`）兩道門檻都不套用。

        通過門檻才 → （若快照是 v3 來源且尚未備份）備份一份 `.v3.bak`；每次即將覆寫
        快照前，若磁碟上已有一份 → 備份 `.prev`（Task 3.7 A：`.v3.bak` 只保護第一次
        v3→v4 轉換，`.prev` 保護「每一次」換版——2026-09-19 事故的教訓是換版當下若
        出問題無法回退）→ `ExploreIndex.set_published` → （有設 `snapshot_path` 才）
        `dump_snapshot`；任一步例外 → 記錄失敗次數與訊息、**不清 `_dirty`**（下次
        tick 會重試）、保留 `ExploreIndex` 目前版本（不呼叫 `set_published`），回
        `False`。成功 → 清 `_dirty`、更新 `_last_published_at`、`_publishes+=1`、回
        `True`。"""
        if not self._dirty and not force:
            return False
        now = self._now_fn()
        if (not force and self._last_attempt_at is not None
                and now - self._last_attempt_at < self._min_interval_s):
            return False
        try:
            has_version = self._index.status()["rows"] is not None
            if has_version and not force:
                with_pf = self._store.count_with_payload("portfolio")
                _, n = self._store.admission_counts()
                reason = _gate_reason(with_pf, n, None, ratio=self._min_portfolio_ratio)
                if reason is not None:
                    self._gate_skips += 1
                    self._last_gate = f"in:{reason}"
                    self._last_attempt_at = now
                    return False

            rows, meta = compose_rows(self._store, now=now, cfg=self._cfg)

            if has_version and not force:
                reason = _gate_reason(meta["with_portfolio"], meta["candidates"], rows,
                                      ratio=self._min_portfolio_ratio)
                if reason is not None:
                    self._gate_skips += 1
                    self._last_gate = f"out:{reason}"
                    self._last_attempt_at = now
                    return False

            if self._snapshot_path is not None:
                self._backup_v3_snapshot_once()
                self._backup_prev_snapshot()
                dump_snapshot(self._snapshot_path, rows=rows, built_at=meta["published_at"],
                             total_scanned=meta["candidates"])
            self._index.set_published(rows, meta)
        except Exception as e:  # noqa: BLE001 — 展示端點：一次發布失敗不得中斷 scheduler
            logger.error("explore publisher：發布失敗，保留舊版: %r", e)
            self._failures += 1
            self._last_error = repr(e)
            self._last_attempt_at = now
            return False
        self._dirty = False
        self._last_attempt_at = now
        self._last_published_at = now
        self._publishes += 1
        return True

    def _backup_v3_snapshot_once(self) -> None:
        """C(3)：第一次以 v4 覆寫既有快照前，若磁碟上那份是 v3 來源且尚未備份
        過，複製一份 `<path>.v3.bak`（C2 修法的一部分——2026-09-19 事故的教訓是
        換版當下若出問題無法回退，先留一份舊版本在旁邊）。讀檔／解析失敗一律
        跳過備份、不阻擋本次發布（快照備份是額外保險，不是發布正確性的一部分）。"""
        p = Path(self._snapshot_path)
        if not p.exists():
            return
        bak = p.with_name(p.name + ".v3.bak")
        if bak.exists():
            return
        try:
            payload = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError, ValueError) as e:
            logger.warning("explore publisher：v3 快照備份失敗（不影響發布）: %r", e)
            return
        if isinstance(payload, dict) and payload.get("version") == 3:
            shutil.copyfile(p, bak)

    def _backup_prev_snapshot(self) -> None:
        """Task 3.7 A：每一次即將以新版覆寫快照前（不限第一次），若磁碟上已有
        一份 → `shutil.copyfile` 成 `<path>.prev`（best effort，讀寫失敗只記
        warning，不阻擋本次發布——快照備份是額外保險，不是發布正確性的一部分，
        與 `_backup_v3_snapshot_once` 同一取捨）。"""
        p = Path(self._snapshot_path)
        if not p.exists():
            return
        try:
            shutil.copyfile(p, str(p) + ".prev")
        except OSError as e:
            logger.warning("explore publisher：快照 .prev 備份失敗（不影響發布）: %r", e)

    def status(self) -> dict:
        """`/api/ops/health` 揭露用（Task 3.4／5.1；`gate_skips`／`last_gate`／
        `last_attempt_at` 為 Task 3.5 C(4) 新增；`min_portfolio_ratio` 為 Task 3.6 A
        新增）：`last_published_at`、`dirty`（尚有未發布的變更）、`publishes`／
        `failures` 累計次數、`last_error`（最近一次失敗的訊息，從未失敗過 →
        `None`）、`gate_skips`（因發布門檻擋下的累計次數）、`last_gate`（最近一次
        擋下的 `"with_pf/n"` 字串，從未擋過 → `None`）、`last_attempt_at`（成功或
        失敗都更新）、`min_portfolio_ratio`（目前生效的門檻比例，供人工判讀
        `last_gate` 用）。"""
        return {"last_published_at": self._last_published_at, "dirty": self._dirty,
               "publishes": self._publishes, "failures": self._failures,
               "last_error": self._last_error, "gate_skips": self._gate_skips,
               "last_gate": self._last_gate, "last_attempt_at": self._last_attempt_at,
               "min_portfolio_ratio": self._min_portfolio_ratio}
