"""src/spark/publicapi/explore_publisher.py
Explore 榜單的漸進發布層（spec `docs/superpowers/specs/2026-09-20-hl-leaderboard-refactor-spec.md`
§9.2；plan `docs/superpowers/plans/2026-09-20-explore-rate-limit-refactor.md` Task 4.1／P6）。

`ExploreScheduler`（Task 3.1）把每地址的 portfolio／state／ledger／fills 持續寫進
`ExploreStore`（SQLite），但 `ExploreIndex.query()`（讀路徑）只端出**上一次成功發布**
的一批 `ExploreRow`——本模組是兩者之間唯一的橋接：

- `compose_rows`：純函式，只讀 `ExploreStore`（零網路），把目前 store 裡的資料合成一批
  `ExploreRow`（`hl_explore.enrich_candidate` 的組裝邏輯照舊沿用，唯一差別是這裡的
  `portfolio_raw`／`ch_state` 可能是 `None`——某個候選可能還沒被 scheduler enrich
  過，見 `enrich_candidate` Task 4.1 段）。單一地址 enrich 途中拋例外（payload 格式
  錯、timeout 留下的壞資料）→ **不丟棄該列**，改列一筆 `pending`／`enrich_error`
  （P6 契約 C），`meta["row_errors"]` 累計、不阻擋其他列。
- `ExplorePublisher`：`mark_dirty`／`maybe_publish` 兩段式節流——scheduler 每次寫入後
  呼叫 `mark_dirty()`，scheduler thread 每 tick 末呼叫 `maybe_publish()`；每分鐘至多
  換版一次（`min_interval_s`，spec §9.2：有變更就發布、不等 300 人全部完成，但也不必
  每個 tick 都重算一次全池）。P6（D12，2026-09-20 使用者裁決）：**取消**
  portfolio 覆蓋率／新版列數兩種發布門檻——合格人數真的下降就讓榜縮小甚至為空。
  發布只檢查兩件事：`store.active_candidates()` 非空（候選來源本身沒故障）、
  compose／落快照沒有拋例外（組版成功）；來源故障（候選整批回空）或 compose 整體
  例外 → 記錄 `source_failures`／`last_skip_reason`、**保留 `ExploreIndex` 目前正在
  服務的版本**（fail-open，同既有的展示端點語意），不讓一次瞬間的故障把已經在服務
  的榜單打掉；候選有效但 compose 出來是空列表則視為正常結果、照常換版（P6 契約 C）。
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import shutil
from collections import Counter
from pathlib import Path

from spark.publicapi.explore_store import ExploreStore
from spark.publicapi.hl_explore import (DEFAULT_FILLS_COVERAGE, DEFAULT_WINDOW,
                                        FILLS_WINDOW_DAYS, WINDOW_KEYS, ExploreConfig,
                                        ExploreIndex, ExploreRow, _abbreviate_address,
                                        _apply_tags, _effective_thresholds, classify,
                                        dump_snapshot, enrich_candidate)

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
    （所有列 `as_of` 三鍵中最舊的非 `None` 時間戳，皆缺 → `None`）、`row_errors`
    （P6 契約 C：單一地址 enrich 途中拋例外的次數，該列改列 `pending`／
    `enrich_error`，不阻擋其他列——store 本身讀取失敗導致的整體例外不算在這裡，
    那種情況會往外拋、由 `ExplorePublisher.maybe_publish` 接住當來源故障）。
    """
    now_ms = int(now * 1000)
    window_ms = fills_window_days * 86400_000
    candidates = store.active_candidates()

    rows: list[ExploreRow] = []
    coverage_counter: Counter = Counter()
    as_of_values: list[float] = []
    with_portfolio = 0
    row_errors = 0

    for c in candidates:
        try:
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
        except Exception as e:  # noqa: BLE001 — P6 契約 C：單一地址壞資料不得拖垮整批
            logger.error("compose_rows：地址 %s enrich 失敗，改列 pending/enrich_error: %r",
                        c.address, e)
            row_errors += 1
            row = ExploreRow(
                address=c.address, display_name=c.display_name,
                label=c.display_name if c.display_name else _abbreviate_address(c.address),
                coins=(), account_bucket=None,
                windows={k: None for k in WINDOW_KEYS}, live_days=None,
                order_count_30d=0, closed_positions_30d=None, realized_pnl_30d_usd=None,
                close_win_rate_pct=None, concentration_pct=None,
                exposure_dir=None, exposure_pct=None, tags=(), fills_truncated=True,
                as_of={"portfolio": None, "state": None, "fills": None},
                fills_coverage=dict(DEFAULT_FILLS_COVERAGE),
                eligibility="pending", eligibility_reason="enrich_error")
            coverage = dict(row.fills_coverage)
            as_of = dict(row.as_of)
            portfolio_raw = None

        if row is None:
            continue

        rows.append(row)
        coverage_counter[coverage["state"]] += 1
        if portfolio_raw is not None:
            with_portfolio += 1
        as_of_values.extend(v for v in as_of.values() if v is not None)

    rows = _apply_tags(rows, cfg)

    # Task 6.7（P6 reviewer W2）：快照必須反映「這一輪資料在預設門檻下的分類」
    # ——不能讓 `ExploreRow.eligibility` 的類別預設值（"eligible"，見該類別
    # 檔頭）原封不動寫進 `dump_snapshot`，那個預設值只是「還沒被 `classify()`
    # 算過」的佔位；直接讀快照檔的人（RUNBOOK 回退流程、部署預建流程）會被
    # 誤導成全部合格。這裡的「預設門檻」＝`cfg` 本身四個門檻（不像
    # `ExploreIndex.query()` 那樣可被單次請求的 `min_live_days` 等參數覆寫），
    # 與 `qualify()` 未傳入任何逃生門旗標時的預設行為等價。`enrich_error` 列
    # （見上方 try/except 分支）已經是 `pending`／`enrich_error`——沒有真實
    # 資料可供 `classify()` 判斷，保留原樣不覆寫（與 `ExploreIndex.query()`
    # 對這類列的既有處理一致，見該函式）。
    # ⚠️（2026-09-21 複審 W2）這裡的門檻＝**本行程的 `cfg`**（正式機來自
    # `ExploreConfig.from_env()`），不是端點層的模組預設常數；兩者只在 env 未覆寫
    # `EXPLORE_MIN_*`／`EXPLORE_MAX_*` 時相等。快照檔的 `eligibility` 反映的是
    # 本行程 cfg 下的分類；`query()` 仍依每次請求的門檻重算，API 永遠正確。
    min_live_days, min_fills, max_dd_pct, max_concentration_pct = _effective_thresholds(
        cfg, require_sample=True, max_dd_filter=True, exclude_concentrated=True)
    classified_rows: list[ExploreRow] = []
    for r in rows:
        if r.eligibility_reason == "enrich_error":
            classified_rows.append(r)
            continue
        elig, reason = classify(r, cfg, window=DEFAULT_WINDOW, min_live_days=min_live_days,
                                min_fills=min_fills, max_dd_pct=max_dd_pct,
                                max_concentration_pct=max_concentration_pct)
        classified_rows.append(dataclasses.replace(r, eligibility=elig, eligibility_reason=reason))
    rows = classified_rows

    meta = {
        "published_at": now,
        "candidates": len(candidates),
        "with_portfolio": with_portfolio,
        "coverage_counts": dict(coverage_counter),
        "as_of_oldest": min(as_of_values) if as_of_values else None,
        "row_errors": row_errors,
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
        self._last_attempt_at: float | None = None
        self._publishes = 0
        self._failures = 0
        self._last_error: str | None = None
        # P6（D12，2026-09-20）：取代已刪除的舊版門檻計數器／原因欄位
        # （portfolio 覆蓋率／新版列數門檻整組拿掉）——「來源故障」現在只有
        # 一種形狀：`store.active_candidates()` 回空且已有版本可保護。
        self._source_failures = 0
        self._last_skip_reason: str | None = None

    def mark_dirty(self) -> None:
        self._dirty = True

    def maybe_publish(self, *, force: bool = False) -> bool:
        """未 dirty 且非強制 → `False`（沒有東西可發布）。距上次嘗試（`_last_attempt_at`，
        成功或失敗都算）不足 `min_interval_s` 且非強制 → `False`（節流，spec §9.2：
        每分鐘至多一次）。

        P6（D12，2026-09-20 使用者裁決）：**取消** portfolio 覆蓋率／新版列數
        兩種發布門檻（原 Task 3.5 C／3.7 A 的輸入／輸出兩道判斷式已整段刪除）
        ——合格人數真的下降就讓榜縮小甚至為空，不用數量門檻掩蓋資料語義問題
        （三態資格見 `hl_explore.classify`）。發布只檢查兩件事（P6 契約 C）：

        1. **來源故障**：`store.active_candidates()` 為空，且 `ExploreIndex`
           已經有版本可保護 → 記 `source_failures+=1`、
           `last_skip_reason="no_active_candidates"`，**保留舊版**、回 `False`
           （不呼叫 `compose_rows`，省一次無意義的空迴圈）。**首次上線**
           （`index.status()["rows"] is None`，沒有版本可保護）即使候選為空
           也照常往下 compose（結果會是空列表，視為「有效空結果」正常發布，
           見下）。
        2. **組版成功**：`compose_rows` 正常跑完（即使輸出 0 列，`compose_rows`
           本身對單一地址 enrich 失敗已有 `pending`／`enrich_error` 兜底，見該
           函式）→ 落快照＋`ExploreIndex.set_published`，視為成功換版；
           `compose_rows` 或落快照途中拋整體例外（例如 store 本身讀取失敗）
           → 記 `failures`／`last_error`，**保留舊版**、回 `False`。

        `force=True` 完全繞過第 1 項來源故障檢查（仍會更新 `_last_attempt_at`），
        供人工強制換版使用。

        通過檢查才 → （若快照是 v3 來源且尚未備份）備份一份 `.v3.bak`；每次即將
        覆寫快照前，若磁碟上已有一份 → 備份 `.prev`／`.daily`（不變，見
        `_backup_prev_snapshot`）→ `ExploreIndex.set_published` → （有設
        `snapshot_path` 才）`dump_snapshot`。成功 → 清 `_dirty`、更新
        `_last_published_at`、`_publishes+=1`、回 `True`。"""
        if not self._dirty and not force:
            return False
        now = self._now_fn()
        if (not force and self._last_attempt_at is not None
                and now - self._last_attempt_at < self._min_interval_s):
            return False
        try:
            has_version = self._index.status()["rows"] is not None
            if not force and has_version and not self._store.active_candidates():
                self._source_failures += 1
                self._last_skip_reason = "no_active_candidates"
                self._last_attempt_at = now
                logger.warning("explore publisher：候選來源整批回空（第 %d 次），榜單維持舊版",
                               self._source_failures)
                return False

            rows, meta = compose_rows(self._store, now=now, cfg=self._cfg)

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
        # <!-- 2026-09-20 第四輪複審 W1 -->：`.prev` 每分鐘輪替，回退窗口只有一分鐘；
        # 「非空但劣化」的版本連續兩分鐘就會把 .prev 也蓋掉。另留一份 `.daily`：
        # 每 24 小時至多輪替一次（以 .daily 的 mtime 對 now_fn 判斷），提供至少
        # 一天前的 last-good 回退點。同樣 best effort。
        # 兩個時間都取檔案系統 mtime（快照本身 vs .daily），同一基底（工程原則 #1）；
        # 不拿注入的 now_fn 跟 mtime 比——fake clock 會讓 24h 閘門靜默失效。
        # 複製先寫 .tmp 再 os.replace：ENOSPC／中途被砍不會留下截斷的 .daily。
        daily = Path(str(p) + ".daily")
        try:
            stale = (not daily.exists()) or (p.stat().st_mtime - daily.stat().st_mtime >= 86400)
            if stale:
                tmp = Path(str(daily) + ".tmp")
                shutil.copyfile(p, tmp)
                os.replace(tmp, daily)
        except OSError as e:
            logger.warning("explore publisher：快照 .daily 備份失敗（不影響發布）: %r", e)

    def status(self) -> dict:
        """`/api/ops/health` 揭露用（Task 3.4／5.1；P6（D12，2026-09-20）：
        舊版門檻計數器／原因欄位已隨門檻整組刪除，改為
        `source_failures`／`last_skip_reason`）：`last_published_at`、`dirty`
        （尚有未發布的變更）、`publishes`／`failures` 累計次數、`last_error`
        （最近一次組版/落快照失敗的訊息，從未失敗過 → `None`）、
        `source_failures`（候選來源整批回空、保留舊版的累計次數）、
        `last_skip_reason`（最近一次來源故障的原因字串，例如
        `"no_active_candidates"`，從未發生過 → `None`）、`last_attempt_at`
        （成功或失敗都更新）。"""
        return {"last_published_at": self._last_published_at, "dirty": self._dirty,
               "publishes": self._publishes, "failures": self._failures,
               "last_error": self._last_error, "source_failures": self._source_failures,
               "last_skip_reason": self._last_skip_reason,
               "last_attempt_at": self._last_attempt_at}
