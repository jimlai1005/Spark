"""tests/test_explore_publisher.py — `explore_publisher.compose_rows`／`ExplorePublisher`
（plan `docs/superpowers/plans/2026-09-20-explore-rate-limit-refactor.md` Task 4.1）。

全離線（autouse socket-ban，見 conftest.py）：只用 `ExploreStore`（SQLite，`tmp_path`）
與 `hl_explore` 純函式，不打上游。
"""
import json
from decimal import Decimal

from spark.publicapi import hl_explore
from spark.publicapi.explore_publisher import ExplorePublisher, compose_rows
from spark.publicapi.explore_store import ExploreStore, FillsSyncState
from spark.publicapi.hl_explore import ExploreConfig, ExploreIndex, enrich_candidate

_A = "0x" + "a1" * 20
_B = "0x" + "b2" * 20


def _av_series(start_ms, values, step_ms=86_400_000):
    return [[start_ms + i * step_ms, str(v)] for i, v in enumerate(values)]


def _pnl_from_av(av_series):
    base = Decimal(av_series[0][1])
    return [[t, str(Decimal(v) - base)] for t, v in av_series]


def _portfolio_raw(month_values, alltime_values, start_ms=1_700_000_000_000):
    month_series = _av_series(start_ms, month_values)
    alltime_series = _av_series(start_ms, alltime_values)
    return [
        ["month", {"accountValueHistory": month_series,
                   "pnlHistory": _pnl_from_av(month_series), "vlm": "0"}],
        ["allTime", {"accountValueHistory": alltime_series,
                     "pnlHistory": _pnl_from_av(alltime_series), "vlm": "0"}],
    ]


def _ch_state(account_value="50000", positions=None):
    return {"marginSummary": {"accountValue": account_value},
           "assetPositions": positions or []}


def _sync(address, *, completeness="complete", updated_at=1000.0,
          observed_from_ms=1, observed_to_ms=2, reason=None):
    return FillsSyncState(
        address=address, window_start_ms=1, window_end_ms=2, cursor_ms=2,
        synced_through_ms=2, observed_from_ms=observed_from_ms,
        observed_to_ms=observed_to_ms, completeness=completeness, reason=reason,
        pages_done=1, fills_in_window=0, updated_at=updated_at, last_error=None)


def _cfg(**over):
    base = dict(min_trading_days=0, min_fills=0)
    base.update(over)
    return ExploreConfig(**base)


def _dummy_index(now=1000.0):
    # Task 3.4（D6）：`ExploreIndex` 不再接受 `leaderboard_source_fn`／`hl`／
    # `excluded_fn`／`sleep_fn`——它不再打上游、不再自己建置（見
    # `hl_explore.ExploreIndex` 類別檔頭）。
    return ExploreIndex(cfg=_cfg(), now_fn=lambda: now)


# ============================================================
# hl_explore.enrich_candidate：portfolio_raw / ch_state 可為 None（D 卡片改動）
# ============================================================

def test_enrich_candidate_portfolio_none_still_emits_row_with_none_windows_and_live_days():
    """`portfolio_raw is None` → 不再整列跳過：`windows` 全 None、`live_days` None
    （「分析待完成」，不是 0）。"""
    row = enrich_candidate(_A, None, None, [], None)
    assert row is not None
    assert row.windows == {"day": None, "week": None, "month": None, "allTime": None}
    assert row.live_days is None
    assert row.account_bucket is None
    assert row.exposure_dir is None
    assert row.exposure_pct is None
    assert row.order_count_30d == 0


def test_enrich_candidate_ch_state_none_degrades_exposure_and_bucket_only():
    """`ch_state is None`（portfolio 仍在）→ `account_bucket`／`exposure_*` 降級為
    None，其餘欄位（windows／live_days）不受影響。"""
    portfolio_raw = _portfolio_raw([1000, 1000], [1000] * 60)
    row = enrich_candidate(_A, None, portfolio_raw, [], None)
    assert row is not None
    assert row.windows["month"] is not None
    assert row.live_days == 59
    assert row.account_bucket is None
    assert row.exposure_dir is None
    assert row.exposure_pct is None


# ============================================================
# compose_rows（驗收 1／2）
# ============================================================

def test_compose_rows_address_with_only_portfolio_cache_still_listed(tmp_path):
    """驗收 1：單地址只有 portfolio 快取、無 state／fills → 出列、
    `exposure_pct is None`、`fills_coverage.state=="backfilling"`、
    `order_count_30d==0`。"""
    store = ExploreStore(tmp_path / "explore.db")
    store.upsert_candidates([(_A, "Alice", 1, 0.1)], as_of=1000.0)
    portfolio_raw = _portfolio_raw([1000, 1000], [1000] * 60)
    store.put_cache_ok(_A, "portfolio", portfolio_raw, fetched_at=900.0, refresh_after=2000.0)

    rows, meta = compose_rows(store, now=1000.0, cfg=_cfg())

    assert len(rows) == 1
    row = rows[0]
    assert row.address == _A
    assert row.exposure_pct is None
    assert row.account_bucket is None
    assert row.fills_coverage["state"] == "backfilling"
    assert row.order_count_30d == 0
    assert row.as_of["portfolio"] == 900.0
    assert row.as_of["state"] is None
    assert row.as_of["fills"] is None
    assert meta["candidates"] == 1
    assert meta["with_portfolio"] == 1
    assert meta["coverage_counts"] == {"backfilling": 1}
    assert meta["published_at"] == 1000.0


def test_compose_rows_full_address_reports_as_of_and_complete_coverage(tmp_path):
    """驗收 2：完整地址（三種快取＋complete sync）→ `to_dict()` 含 `as_of`
    （三鍵等於塞入的 `fetched_at`）與 `fills_coverage.state=="complete"`、
    `fills_truncated is False`。"""
    store = ExploreStore(tmp_path / "explore.db")
    store.upsert_candidates([(_A, "Alice", 1, 0.1)], as_of=1000.0)
    portfolio_raw = _portfolio_raw([1000, 1000], [1000] * 60)
    store.put_cache_ok(_A, "portfolio", portfolio_raw, fetched_at=111.0, refresh_after=2000.0)
    store.put_cache_ok(_A, "clearinghouseState", _ch_state(), fetched_at=222.0,
                      refresh_after=2000.0)
    store.insert_fills_page(_A, [], _sync(_A, completeness="complete", updated_at=333.0))

    rows, meta = compose_rows(store, now=1000.0, cfg=_cfg())

    assert len(rows) == 1
    d = rows[0].to_dict()
    assert d["as_of"] == {"portfolio": 111.0, "state": 222.0, "fills": 333.0}
    assert d["fills_coverage"]["state"] == "complete"
    assert d["fills_truncated"] is False
    assert meta["coverage_counts"] == {"complete": 1}


# ============================================================
# ExplorePublisher（驗收 3／4）
# ============================================================

def test_maybe_publish_gated_by_dirty_and_min_interval_force_bypasses():
    """驗收 3：未 dirty → False；dirty 但 60s 內第二次 → False；`force=True` → True。"""
    store = ExploreStore(":memory:")
    index = _dummy_index()
    now = [1000.0]
    pub = ExplorePublisher(store=store, index=index, cfg=_cfg(), now_fn=lambda: now[0],
                           snapshot_path=None)

    assert pub.maybe_publish() is False   # 未 dirty

    pub.mark_dirty()
    assert pub.maybe_publish() is True    # 第一次成功發布

    pub.mark_dirty()
    now[0] += 10.0                        # 60s 內
    assert pub.maybe_publish() is False

    assert pub.maybe_publish(force=True) is True


def test_maybe_publish_keeps_old_version_on_compose_failure(tmp_path, monkeypatch):
    """驗收 4：compose 途中 store 拋例外 → 回 False、`index.query()` 仍是上一版、
    `status().failures==1`。"""
    store = ExploreStore(tmp_path / "explore.db")
    store.upsert_candidates([(_A, "Alice", 1, 0.1)], as_of=1000.0)
    portfolio_raw = _portfolio_raw([1000, 1000], [1000] * 60)
    store.put_cache_ok(_A, "portfolio", portfolio_raw, fetched_at=900.0, refresh_after=2000.0)
    index = _dummy_index()
    pub = ExplorePublisher(store=store, index=index, cfg=_cfg(), now_fn=lambda: 1000.0,
                           snapshot_path=None)

    pub.mark_dirty()
    assert pub.maybe_publish() is True
    first = index.query()
    assert len(first["rows"]) == 1

    def boom(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(store, "get_fills", boom)

    pub.mark_dirty()
    assert pub.maybe_publish(force=True) is False
    assert index.query() == first
    status = pub.status()
    assert status["failures"] == 1
    assert status["last_error"] is not None


# ============================================================
# v3 → v4 快照相容（驗收 5）
# ============================================================

def _v3_row_dict(address=_A):
    """真實 v3 快照列形狀：`enrich_candidate` 在 v3 時代保證 `month`／`allTime`
    恆非 `None`（否則整列不會被寫進快照），這裡如實反映該不變量——只有
    `as_of`／`fills_coverage` 兩個 v4 新欄位缺席。"""
    stats = {"pnl_usd": 100.0, "max_dd_pct": -5.0, "max_dd_reason": None, "spark": []}
    return {"address": address, "display_name": None, "label": "0xaaaa…aaaa",
           "coins": [], "account_bucket": "<$10K",
           "windows": {"day": None, "week": None, "month": stats, "allTime": stats},
           "live_days": 60, "order_count_30d": 0, "closed_positions_30d": 0,
           "realized_pnl_30d_usd": 0.0, "close_win_rate_pct": None,
           "concentration_pct": None, "exposure": {"dir": None, "pct": None},
           "tags": [], "fills_truncated": False}


def test_load_snapshot_v3_migrates_rows_with_backfilling_coverage(tmp_path):
    """驗收 5：v3 快照檔（無 `as_of`／`fills_coverage`）→ `load_snapshot` 回 v4
    結構，每列 `as_of.portfolio == built_at`、`fills_coverage.state ==
    "backfilling"`；`query()["published_at"]` 等於 `built_at`、`initializing`
    為 False。"""
    path = tmp_path / "explore_snapshot.json"
    path.write_text(json.dumps({"version": 3, "built_at": 555.0, "total_scanned": 1,
                                "rows": [_v3_row_dict()]}))

    loaded = hl_explore.load_snapshot(str(path))
    assert loaded is not None
    row = loaded["rows"][0]
    assert row.as_of == {"portfolio": 555.0, "state": 555.0, "fills": 555.0}
    assert row.fills_coverage == {"state": "backfilling", "observed_from": None,
                                  "observed_to": None, "reason": None}

    index = ExploreIndex(cfg=_cfg(), now_fn=lambda: 1000.0, snapshot_path=str(path))
    result = index.query()
    assert result["published_at"] == 555.0
    assert result["initializing"] is False


# ============================================================
# 原子寫（驗收 6）
# ============================================================

def test_publish_snapshot_write_leaves_no_leftover_tmp_file(tmp_path):
    store = ExploreStore(tmp_path / "explore.db")
    store.upsert_candidates([(_A, "Alice", 1, 0.1)], as_of=1000.0)
    portfolio_raw = _portfolio_raw([1000, 1000], [1000] * 60)
    store.put_cache_ok(_A, "portfolio", portfolio_raw, fetched_at=900.0, refresh_after=2000.0)
    index = _dummy_index()
    snap_dir = tmp_path / "snap"
    snap_path = snap_dir / "explore_snapshot.json"
    pub = ExplorePublisher(store=store, index=index, cfg=_cfg(), now_fn=lambda: 1000.0,
                           snapshot_path=str(snap_path))

    pub.mark_dirty()
    assert pub.maybe_publish() is True

    assert snap_path.exists()
    assert [p.name for p in snap_dir.iterdir()] == [snap_path.name]
