"""tests/test_explore_publisher.py — `explore_publisher.compose_rows`／`ExplorePublisher`
（plan `docs/superpowers/plans/2026-09-20-explore-rate-limit-refactor.md` Task 4.1）。

全離線（autouse socket-ban，見 conftest.py）：只用 `ExploreStore`（SQLite，`tmp_path`）
與 `hl_explore` 純函式，不打上游。
"""
import dataclasses
import json
from decimal import Decimal

from spark.publicapi import hl_explore
from spark.publicapi.explore_publisher import ExplorePublisher, compose_rows
from spark.publicapi.explore_store import ExploreStore, FillsSyncState
from spark.publicapi.hl_explore import (ExploreConfig, ExploreIndex, dump_snapshot,
                                        enrich_candidate)

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
          observed_from_ms=1, observed_to_ms=2, reason=None, synced_through_ms=2):
    return FillsSyncState(
        address=address, window_start_ms=1, window_end_ms=2, cursor_ms=2,
        synced_through_ms=synced_through_ms, observed_from_ms=observed_from_ms,
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


def test_compose_rows_fills_coverage_synced_through_and_last_success_at_from_store(tmp_path):
    """Task 7.1（2026-09-21）：`fills_coverage.synced_through`＝
    `fills_sync.synced_through_ms`、`last_success_at`＝`fills_sync.updated_at`
    ——刻意用互不相同的三個值（`synced_through_ms` 落後於 `observed_to_ms`，
    模擬回補中的真實情境）驗證兩鍵各自對應正確來源、不是互相抄襲或抄
    `observed_to`。"""
    store = ExploreStore(tmp_path / "explore.db")
    store.upsert_candidates([(_A, "Alice", 1, 0.1)], as_of=1000.0)
    portfolio_raw = _portfolio_raw([1000, 1000], [1000] * 60)
    store.put_cache_ok(_A, "portfolio", portfolio_raw, fetched_at=111.0, refresh_after=2000.0)
    store.insert_fills_page(_A, [], _sync(
        _A, completeness="partial", updated_at=333.0,
        observed_from_ms=10_000, observed_to_ms=99_000, synced_through_ms=50_000))

    rows, _meta = compose_rows(store, now=1000.0, cfg=_cfg())

    d = rows[0].to_dict()
    assert d["fills_coverage"]["synced_through"] == 50_000
    assert d["fills_coverage"]["last_success_at"] == 333.0
    assert d["as_of"]["fills"] == 333.0   # 相容：as_of.fills 仍＝ last_success_at


def test_compose_rows_fills_coverage_synced_through_null_when_no_sync(tmp_path):
    """無 sync（尚未回補過）→ 兩鍵皆 null（同 `observed_from`／`observed_to`）。"""
    store = ExploreStore(tmp_path / "explore.db")
    store.upsert_candidates([(_A, "Alice", 1, 0.1)], as_of=1000.0)
    portfolio_raw = _portfolio_raw([1000, 1000], [1000] * 60)
    store.put_cache_ok(_A, "portfolio", portfolio_raw, fetched_at=900.0, refresh_after=2000.0)

    rows, _meta = compose_rows(store, now=1000.0, cfg=_cfg())

    d = rows[0].to_dict()
    assert d["fills_coverage"]["synced_through"] is None
    assert d["fills_coverage"]["last_success_at"] is None
    # Task 7.5：無 sync → 三個新鍵也一律 null。
    assert d["fills_coverage"]["window_start"] is None
    assert d["fills_coverage"]["window_end"] is None
    assert d["fills_coverage"]["params_fp"] is None


def test_compose_rows_fills_coverage_window_and_params_fp_from_store(tmp_path):
    """Task 7.9b B6（語義變更）：`fills_coverage.window_start`／`window_end`
    改為**增量軌覆蓋區間** `[inc_from_ms, synced_through_ms]`（不再是「目前
    這一輪」的查詢區間，那個語意現在放進 `evidence.window_start`／
    `evidence.window_end`）；`params_fp` 對應不變，仍讀 `fills_sync.params_fp`
    （工程原則 1：各鍵各自讀各自的來源，不是互相抄）。"""
    store = ExploreStore(tmp_path / "explore.db")
    store.upsert_candidates([(_A, "Alice", 1, 0.1)], as_of=1000.0)
    portfolio_raw = _portfolio_raw([1000, 1000], [1000] * 60)
    store.put_cache_ok(_A, "portfolio", portfolio_raw, fetched_at=111.0, refresh_after=2000.0)
    checkpoint = dataclasses.replace(
        _sync(_A, completeness="complete", updated_at=333.0, synced_through_ms=777),
        window_start_ms=555, window_end_ms=777, params_fp="aggregateByTime=default(false)",
        inc_from_ms=100)
    store.insert_fills_page(_A, [], checkpoint)

    rows, _meta = compose_rows(store, now=1000.0, cfg=_cfg())

    d = rows[0].to_dict()
    assert d["fills_coverage"]["window_start"] == 100
    assert d["fills_coverage"]["window_end"] == 777
    assert d["fills_coverage"]["params_fp"] == "aggregateByTime=default(false)"


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
    """驗收 4：compose **整體**拋例外（`store.active_candidates()` 本身壞掉，
    在任何一個地址的 per-row try/except 之外——P6 契約 C：這才是「來源故障」
    的例外形狀）→ 回 False、`index.query()` 仍是上一版、`status().failures==1`。
    （單一地址 enrich 失敗改由 `test_compose_rows_...enrich_error` 系列覆蓋：
    P6 之後那種失敗不再讓整批 compose 失敗，見 `compose_rows` 檔頭。）"""
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
    monkeypatch.setattr(store, "active_candidates", boom)

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
    # Task 7.1（2026-09-21）：v3 快照沒有游標／最後成功時間可推導，兩鍵補 null
    # （沿用 `DEFAULT_FILLS_COVERAGE`，與 `observed_from`／`observed_to` 同語意）。
    # Task 7.5：同理再補 window_start／window_end／params_fp 三鍵。Task 7.9b：
    # 再補 `evidence`（scan 沒有可回溯的一次遍歷 → None）。
    assert row.fills_coverage == {"state": "backfilling", "observed_from": None,
                                  "observed_to": None, "reason": None,
                                  "synced_through": None, "last_success_at": None,
                                  "window_start": None, "window_end": None,
                                  "params_fp": None, "evidence": None}

    index = ExploreIndex(cfg=_cfg(), now_fn=lambda: 1000.0, snapshot_path=str(path))
    result = index.query()
    assert result["published_at"] == 555.0
    assert result["initializing"] is False


def test_load_snapshot_v3_migrates_masks_incomplete_fills_and_zeroes_order_count(tmp_path):
    """Task 6.5：主線程本機以正式機 v3 快照實測發現——舊快照列本身可能帶有
    非空的成交衍生欄位（v3 時代沒有「未知≠0」語意，`enrich_candidate` 尚未
    遮蔽），遷移到 v4 後必須被輸出層遮罩補上，且 `order_count_30d` 歸零
    （v3 的值不是「本輪已觀測筆數」，沿用會讓 `classify` 誤判 eligible）。"""
    row_dict = _v3_row_dict()
    row_dict.update(coins=["ZEC", "BTC"], order_count_30d=886, closed_positions_30d=12,
                    realized_pnl_30d_usd=345.6, close_win_rate_pct=13.33,
                    concentration_pct=28.6, tags=["concentrated", "low_drawdown"])
    path = tmp_path / "explore_snapshot.json"
    path.write_text(json.dumps({"version": 3, "built_at": 555.0, "total_scanned": 1,
                                "rows": [row_dict]}))

    loaded = hl_explore.load_snapshot(str(path))
    assert loaded is not None
    row = loaded["rows"][0]
    assert row.close_win_rate_pct is None
    assert row.concentration_pct is None
    assert row.closed_positions_30d is None
    assert row.realized_pnl_30d_usd is None
    assert row.coins == ()
    assert "concentrated" not in row.tags
    assert "low_drawdown" in row.tags   # 非成交衍生標籤不受本遮罩影響
    assert row.order_count_30d == 0

    index = ExploreIndex(cfg=_cfg(), now_fn=lambda: 1000.0, snapshot_path=str(path))
    result = index.query(min_live_days=30, min_fills=200, max_dd_pct=100.0,
                         max_concentration_pct=100.0)
    # 修法前：舊 order_count_30d=886 >= min_fills(200) 讓這列被誤判 eligible。
    assert result["total_qualified"] == 0
    assert result["rows"][0]["eligibility"] in ("pending", "ineligible")


# ============================================================
# Task 6.7（P6 reviewer W2）：快照列必須帶預設門檻下的真實分類，
# 不能讓 `ExploreRow.eligibility` 的類別預設值 "eligible" 冒充進快照檔。
# ============================================================

def test_compose_rows_and_dump_snapshot_row_eligibility_matches_query_default_thresholds(
        tmp_path):
    """驗收 4(a)：`compose_rows`（進而 `dump_snapshot` 落盤）產出的列，其
    `eligibility`／`eligibility_reason` 必須等於 `ExploreIndex.query()` 在
    預設門檻（`ExploreConfig()`）下對同一份 rows 算出的結果——不是類別預設值
    `"eligible"`（修法前 `dump_snapshot` 會原封不動寫出這個佔位值）。"""
    store = ExploreStore(tmp_path / "explore.db")
    store.upsert_candidates([(_A, "Alice", 1, 0.1)], as_of=1000.0)
    portfolio_raw = _portfolio_raw([1000, 1000], [1000] * 60)
    store.put_cache_ok(_A, "portfolio", portfolio_raw, fetched_at=900.0, refresh_after=2000.0)
    # 無 fills sync → coverage backfilling、order_count_30d==0，預設 min_fills=200 未滿足。
    cfg = ExploreConfig()

    rows, meta = compose_rows(store, now=1000.0, cfg=cfg)
    snap_path = tmp_path / "snap.json"
    dump_snapshot(str(snap_path), rows=rows, built_at=meta["published_at"],
                 total_scanned=meta["candidates"])
    dumped_row = json.loads(snap_path.read_text())["rows"][0]

    index = ExploreIndex(cfg=cfg, now_fn=lambda: 1000.0)
    index.set_published(rows, meta)
    queried_row = index.query()["rows"][0]

    assert dumped_row["eligibility"] == queried_row["eligibility"]
    assert dumped_row["eligibility_reason"] == queried_row["eligibility_reason"]
    # 明確錨定（不是修法前的類別預設值 "eligible"）：
    assert dumped_row["eligibility"] == "pending"
    assert dumped_row["eligibility_reason"] == "fills_unknown"


def test_load_snapshot_v3_migration_default_thresholds_yield_pending_and_ineligible(tmp_path):
    """驗收 4(b)：v3→v4 遷移列以預設門檻（`ExploreConfig()`）分類，不是全部
    `"eligible"`——`live_days` 足但成交未知的一列是 `pending`／`fills_unknown`，
    `live_days` 不足（5 < 預設門檻 30）的一列是 `ineligible`／`live_days`。"""
    row_sufficient = _v3_row_dict(_A)   # live_days=60
    row_thin = _v3_row_dict(_B)
    row_thin["live_days"] = 5
    path = tmp_path / "explore_snapshot.json"
    path.write_text(json.dumps({"version": 3, "built_at": 555.0, "total_scanned": 2,
                                "rows": [row_sufficient, row_thin]}))

    loaded = hl_explore.load_snapshot(str(path))
    assert loaded is not None
    by_addr = {r.address: r for r in loaded["rows"]}
    assert by_addr[_A].eligibility == "pending"
    assert by_addr[_A].eligibility_reason == "fills_unknown"
    assert by_addr[_B].eligibility == "ineligible"
    assert by_addr[_B].eligibility_reason == "live_days"


# ============================================================
# 原子寫（驗收 6）
# ============================================================

# ============================================================
# P6（D12，2026-09-20）：取消 portfolio 覆蓋率／新版列數兩種發布門檻——
# 合格人數真的下降就讓榜縮小甚至為空；發布只檢查來源是否有候選、組版是否
# 成功（見 `ExplorePublisher.maybe_publish` 檔頭）。以下測試取代已刪除的
# Task 3.5 C／3.6 A／3.7 A 整組「輸入/輸出端門檻」測試。
# ============================================================

def test_maybe_publish_publishes_normally_even_with_near_zero_portfolio_coverage(tmp_path):
    """P6：候選來源本身有候選（不是空），即使 portfolio 覆蓋率只有 10%（9 個
    候選完全沒 enrich 過），也**不擋**——沒有 portfolio 的候選改列
    `pending`／`portfolio_missing`，不是被整批擋下（見 `hl_explore.classify`）。"""
    store = ExploreStore(tmp_path / "explore.db")
    store.upsert_candidates([(_A, "Alice", 1, 0.1)], as_of=1000.0)
    portfolio_raw = _portfolio_raw([1000, 1000], [1000] * 60)
    store.put_cache_ok(_A, "portfolio", portfolio_raw, fetched_at=900.0, refresh_after=2000.0)
    index = _dummy_index()
    now = [1000.0]
    pub = ExplorePublisher(store=store, index=index, cfg=_cfg(), now_fn=lambda: now[0],
                           snapshot_path=None)
    pub.mark_dirty()
    assert pub.maybe_publish() is True   # 第一次發布

    # 新增 9 個沒有 portfolio 的候選，10 個裡只有 1 個（10%）有 portfolio。
    for i in range(9):
        addr = "0x" + f"{i:02d}" * 20
        store.upsert_candidates([(addr, f"trader{i}", i + 2, 0.0)], as_of=1000.0)

    now[0] += 60.0   # 越過 min_interval
    pub.mark_dirty()
    assert pub.maybe_publish() is True   # P6：無門檻，正常換版
    result = index.query()
    assert result["total_scanned"] == 10
    assert len(result["rows"]) == 10   # 全數出列（1 eligible/pending + 9 pending）
    assert pub.status()["source_failures"] == 0


def test_maybe_publish_blocked_when_active_candidates_empty_and_has_version(tmp_path):
    """P6 契約 C：來源故障的唯一形狀——`active_candidates()` 整批回空、且已有
    版本可保護 → 擋下、`source_failures==1`、`last_skip_reason==
    "no_active_candidates"`，index／快照都不變。"""
    store = ExploreStore(tmp_path / "explore.db")
    index = _dummy_index()
    index.set_published([], {"published_at": 1.0, "candidates": 0})
    snap_path = tmp_path / "snap.json"
    pub = ExplorePublisher(store=store, index=index, cfg=_cfg(), now_fn=lambda: 1000.0,
                           snapshot_path=str(snap_path))
    pub.mark_dirty()
    assert pub.maybe_publish() is False
    assert pub.status()["source_failures"] == 1
    assert pub.status()["last_skip_reason"] == "no_active_candidates"
    assert not snap_path.exists()


def test_maybe_publish_force_bypasses_active_candidates_empty_check(tmp_path):
    """`force=True` 連「來源故障」檢查也繞過（人工強制換版逃生門）。"""
    store = ExploreStore(tmp_path / "explore.db")
    index = _dummy_index()
    index.set_published([], {"published_at": 1.0, "candidates": 0})   # 已有版本
    pub = ExplorePublisher(store=store, index=index, cfg=_cfg(), now_fn=lambda: 1000.0,
                           snapshot_path=None)
    pub.mark_dirty()
    assert pub.maybe_publish(force=True) is True   # store 沒有任何候選，但 force 繞過檢查
    assert pub.status()["source_failures"] == 0
    assert index.query()["total_scanned"] == 0


def test_maybe_publish_first_ever_publish_succeeds_even_with_zero_candidates(tmp_path):
    """`index` 從未發布過版本（`rows is None`）時，即使候選整批為空，也不算
    「來源故障」（沒有舊版可保護）——照常 compose（結果是空列表）並成功換版，
    是「有效空結果」（P6 契約 C）。"""
    store = ExploreStore(tmp_path / "explore.db")
    index = _dummy_index()
    pub = ExplorePublisher(store=store, index=index, cfg=_cfg(), now_fn=lambda: 1000.0,
                           snapshot_path=None)
    pub.mark_dirty()
    assert pub.maybe_publish() is True
    result = index.query()
    assert result["initializing"] is False
    assert result["rows"] == []
    assert result["total_qualified"] == 0
    assert pub.status()["source_failures"] == 0


def test_maybe_publish_source_failure_updates_last_attempt_but_not_last_published(tmp_path):
    """節流看 `last_attempt_at`（成功與失敗都更新）；`last_published_at` 只在
    成功時更新——來源故障（失敗）也要更新 `last_attempt_at`，讓下一次
    `force=False` 呼叫仍受 `min_interval_s` 節流保護。"""
    store = ExploreStore(tmp_path / "explore.db")
    store.upsert_candidates([(_A, "Alice", 1, 0.1)], as_of=1000.0)
    portfolio_raw = _portfolio_raw([1000, 1000], [1000] * 60)
    store.put_cache_ok(_A, "portfolio", portfolio_raw, fetched_at=900.0, refresh_after=2000.0)
    index = _dummy_index()
    now = [1000.0]
    pub = ExplorePublisher(store=store, index=index, cfg=_cfg(), now_fn=lambda: now[0],
                           snapshot_path=None, min_interval_s=60.0)
    pub.mark_dirty()
    assert pub.maybe_publish() is True

    store.deactivate_missing(set())   # 全部停用 → active_candidates() 回空
    now[0] += 100.0
    pub.mark_dirty()
    assert pub.maybe_publish() is False   # 來源故障擋下
    last_published_before = pub.status()["last_published_at"]

    now[0] += 10.0   # 未滿 60s
    pub.mark_dirty()
    assert pub.maybe_publish() is False   # 仍被 min_interval 節流
    assert pub.status()["last_published_at"] == last_published_before


def test_v3_snapshot_backed_up_once_before_first_v4_overwrite(tmp_path):
    """C(3)：首次以 v4 覆寫既有 v3 快照前，備份一份 `.v3.bak`；已存在則不重複備份。"""
    snap_path = tmp_path / "explore_snapshot.json"
    snap_path.write_text(json.dumps({"version": 3, "built_at": 500.0, "total_scanned": 1,
                                     "rows": [_v3_row_dict()]}))
    store = ExploreStore(tmp_path / "explore.db")
    store.upsert_candidates([(_A, "Alice", 1, 0.1)], as_of=1000.0)
    portfolio_raw = _portfolio_raw([1000, 1000], [1000] * 60)
    store.put_cache_ok(_A, "portfolio", portfolio_raw, fetched_at=900.0, refresh_after=2000.0)
    index = ExploreIndex(cfg=_cfg(), now_fn=lambda: 1000.0, snapshot_path=str(snap_path))
    pub = ExplorePublisher(store=store, index=index, cfg=_cfg(), now_fn=lambda: 1000.0,
                           snapshot_path=str(snap_path))

    pub.mark_dirty()
    assert pub.maybe_publish() is True

    bak_path = snap_path.with_name(snap_path.name + ".v3.bak")
    assert bak_path.exists()
    backed_up = json.loads(bak_path.read_text())
    assert backed_up["version"] == 3

    # 再發布一次不應該再覆寫備份（已存在即跳過）。
    bak_mtime = bak_path.stat().st_mtime_ns
    pub.mark_dirty()
    assert pub.maybe_publish(force=True) is True
    assert bak_path.stat().st_mtime_ns == bak_mtime


# ============================================================
# P6（D12）：compose 輸出空列表＝「有效空結果」，正常發布（原 Task 3.7 A/E
# 的輸出側門檻已整段刪除，見 `ExplorePublisher.maybe_publish` 檔頭）。
# ============================================================

def test_maybe_publish_publishes_normally_when_compose_output_is_empty(tmp_path, monkeypatch):
    """P6 契約 C：候選來源有效（`active_candidates()` 非空）、`compose_rows`
    組版成功但輸出 0 列（例如全部候選都被 `classify` 判定不合格——這裡直接用
    monkeypatch 模擬 compose 輸出空列表）→ **正常發布**，`total_qualified==0`，
    快照照常落地，不是門檻擋下。"""
    from spark.publicapi import explore_publisher as ep

    store = ExploreStore(tmp_path / "explore.db")
    for i in range(10):
        addr = "0x" + f"{i:02d}" * 20
        store.upsert_candidates([(addr, f"trader{i}", i + 1, 0.0)], as_of=1000.0)
        portfolio_raw = _portfolio_raw([1000, 1000], [1000] * 60)
        store.put_cache_ok(addr, "portfolio", portfolio_raw, fetched_at=900.0,
                           refresh_after=2000.0)
    index = _dummy_index()
    index.set_published([], {"published_at": 1.0, "candidates": 1, "with_portfolio": 1})
    snap_path = tmp_path / "snap.json"
    pub = ep.ExplorePublisher(store=store, index=index, cfg=_cfg(), now_fn=lambda: 1000.0,
                              snapshot_path=str(snap_path))

    monkeypatch.setattr(ep, "compose_rows", lambda *a, **kw: (
        [], {"published_at": 1000.0, "candidates": 10, "with_portfolio": 0}))

    pub.mark_dirty()
    assert pub.maybe_publish() is True
    assert pub.status()["source_failures"] == 0
    assert snap_path.exists()
    result = index.query()
    assert result["rows"] == []
    assert result["total_qualified"] == 0


def test_maybe_publish_passes_when_compose_output_normal_and_prev_backed_up(tmp_path):
    """有版本且 compose 正常輸出 → 正常換版；每次覆寫快照前保留 `.prev`，
    內容＝前一版。"""
    store = ExploreStore(tmp_path / "explore.db")
    store.upsert_candidates([(_A, "Alice", 1, 0.1)], as_of=1000.0)
    portfolio_raw = _portfolio_raw([1000, 1000], [1000] * 60)
    store.put_cache_ok(_A, "portfolio", portfolio_raw, fetched_at=900.0, refresh_after=2000.0)
    snap_path = tmp_path / "snap.json"
    index = ExploreIndex(cfg=_cfg(), now_fn=lambda: 1000.0, snapshot_path=str(snap_path))
    pub = ExplorePublisher(store=store, index=index, cfg=_cfg(), now_fn=lambda: 1000.0,
                           snapshot_path=str(snap_path))

    pub.mark_dirty()
    assert pub.maybe_publish() is True
    first_content = snap_path.read_text()
    prev_path = tmp_path / "snap.json.prev"
    assert not prev_path.exists()   # 第一次發布，磁碟上尚無舊檔可備份

    store.upsert_candidates([(_B, "Bob", 2, 0.2)], as_of=1000.0)
    store.put_cache_ok(_B, "portfolio", portfolio_raw, fetched_at=900.0, refresh_after=2000.0)
    pub.mark_dirty()
    assert pub.maybe_publish(force=True) is True
    assert prev_path.exists()
    assert prev_path.read_text() == first_content


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


def test_source_failure_logs_warning_each_time(tmp_path, caplog):
    """P6：來源故障（`active_candidates()` 整批回空、已有版本）每次都要「會叫」
    （工程原則 #6——進度不推進得有人知道）；沒有 gate 之後不再需要「每 10 次」
    節流（來源故障預期是罕見事件，不像舊版「冷啟動期間每分鐘擋一次」那樣
    高頻，見 `ExplorePublisher.maybe_publish` 的來源故障分支）。"""
    import logging

    store = ExploreStore(tmp_path / "explore.db")
    index = _dummy_index()
    index.set_published([], {"published_at": 1.0, "candidates": 0})
    clock = [1000.0]
    pub = ExplorePublisher(store=store, index=index, cfg=_cfg(), now_fn=lambda: clock[0],
                           snapshot_path=None, min_interval_s=0.0)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            pub.mark_dirty()
            clock[0] += 1.0
            assert pub.maybe_publish() is False
    msgs = [r.getMessage() for r in caplog.records if "候選來源整批回空" in r.getMessage()]
    assert len(msgs) == 3
    assert pub.status()["source_failures"] == 3


def test_daily_snapshot_rotates_at_most_once_per_day(tmp_path):
    """2026-09-20 第四輪複審 W1：`.prev` 每分鐘輪替只有一分鐘回退窗口；另留 `.daily`
    每 24 小時至多輪替一次（以 mtime 判斷），提供至少一天前的回退點。"""
    import os
    import time

    store = ExploreStore(tmp_path / "explore.db")
    store.upsert_candidates([(_A, "Alice", 1, 0.1)], as_of=1000.0)
    pr = _portfolio_raw([1000, 1000], [1000] * 60)
    store.put_cache_ok(_A, "portfolio", pr, fetched_at=900.0, refresh_after=2000.0)
    snap = tmp_path / "snap.json"
    daily = tmp_path / "snap.json.daily"
    index = ExploreIndex(cfg=_cfg(), now_fn=time.time, snapshot_path=str(snap))
    pub = ExplorePublisher(store=store, index=index, cfg=_cfg(), now_fn=time.time,
                           snapshot_path=str(snap))

    pub.mark_dirty()
    assert pub.maybe_publish() is True          # 第一次：磁碟無舊檔，不備份
    assert not daily.exists()
    v1 = snap.read_text()

    store.upsert_candidates([(_B, "Bob", 2, 0.2)], as_of=1000.0)
    store.put_cache_ok(_B, "portfolio", pr, fetched_at=900.0, refresh_after=2000.0)
    pub.mark_dirty()
    assert pub.maybe_publish(force=True) is True
    assert daily.read_text() == v1              # 第一次覆寫：.daily＝v1

    pub.mark_dirty()
    assert pub.maybe_publish(force=True) is True
    assert daily.read_text() == v1              # 同一天再覆寫：.daily 不動
    v3 = snap.read_text()

    old = time.time() - 90_000
    os.utime(daily, (old, old))                 # 讓 .daily 看起來超過 24 小時
    pub.mark_dirty()
    assert pub.maybe_publish(force=True) is True
    assert daily.read_text() == v3              # 超過 24h：輪替成覆寫前那一版


# ============================================================
# P6 契約 C：單一地址 enrich 例外 → pending/enrich_error，不阻擋其他列
# ============================================================

def test_compose_rows_single_address_enrich_exception_becomes_pending_enrich_error(
        tmp_path, monkeypatch):
    """單一地址在 enrich 途中拋例外（模擬 payload 格式錯／timeout 留下的壞
    資料）→ 該列改列 `pending`／`enrich_error`（不是整批 compose 失敗、也不是
    被丟棄），`meta["row_errors"]` 累計，其他地址正常出列。"""
    from spark.publicapi import explore_publisher as ep

    store = ExploreStore(tmp_path / "explore.db")
    store.upsert_candidates([(_A, "Alice", 1, 0.1), (_B, "Bob", 2, 0.2)], as_of=1000.0)
    portfolio_raw = _portfolio_raw([1000, 1000], [1000] * 60)
    store.put_cache_ok(_A, "portfolio", portfolio_raw, fetched_at=900.0, refresh_after=2000.0)
    store.put_cache_ok(_B, "portfolio", portfolio_raw, fetched_at=900.0, refresh_after=2000.0)

    real_enrich = ep.enrich_candidate

    def flaky_enrich(address, *a, **kw):
        if address == _B:
            raise ValueError("malformed payload")
        return real_enrich(address, *a, **kw)

    monkeypatch.setattr(ep, "enrich_candidate", flaky_enrich)

    rows, meta = compose_rows(store, now=1000.0, cfg=_cfg())

    assert meta["row_errors"] == 1
    by_addr = {r.address: r for r in rows}
    assert set(by_addr) == {_A, _B}
    assert by_addr[_B].eligibility == "pending"
    assert by_addr[_B].eligibility_reason == "enrich_error"
    assert by_addr[_B].windows == {"day": None, "week": None, "month": None, "allTime": None}
    assert by_addr[_A].eligibility_reason != "enrich_error"


# ============================================================
# Task 6.4（D16）：三個重現驗收（放行前置）
# ============================================================

def _stats_fill(tid, oid, time_ms, *, coin="BTC", dir_="Open Long", start_position="0",
                sz="1", closed_pnl="0", px="100"):
    """同時滿足 `ExploreStore.insert_fills_page`（`coin`/`tid`/`time`）與
    `trader_stats.fills_stats`（`oid`/`dir`/`startPosition`/`sz`/`closedPnl`）
    兩邊要求的原始 HL 成交形狀。"""
    return {"coin": coin, "tid": tid, "time": time_ms, "oid": oid, "dir": dir_,
           "startPosition": start_position, "sz": sz, "px": px, "closedPnl": closed_pnl}


def _stats_fills(n, *, coins=("BTC", "ETH"), time_ms=1_700_000_000_000):
    """`n` 筆 distinct-oid 開倉單，兩幣輪流（避免單幣頂到 100% 集中度，同
    `tests/test_public_explore.py::_many_perp_fills`）。"""
    return [_stats_fill(i, i, time_ms, coin=coins[i % len(coins)]) for i in range(n)]


def test_recovery_a_300_candidates_11_complete_8_qualify_rest_pending_or_ineligible(tmp_path):
    """(a) 300 候選、全部有 portfolio（live_days 足、dd 合格）；只有 11 個地址
    fills sync 為 `complete`，其中 8 個 ≥200 筆（`eligible`）、3 個 <200 筆
    （`ineligible/min_fills`——資料已齊全、確定不足）；其餘 289 個從未同步過
    （`backfilling` → `pending/fills_unknown`）。發布後**無一被丟**：
    300 個候選全部出現在 store／compose 的處理範圍內，只是 3 個 ineligible
    不進 `rows`（契約 A：列表 API 不回傳 ineligible 列）。

    ⚠️ 數字修正（相對 plan 原文 Task 6.4(a)）：plan 原文寫「rows==300
    （8 eligible + 292 pending）」——這沒有把 3 個「complete 但 <200 筆」算成
    ineligible。按 `classify()` 的定義（coverage==complete 時 `order_count_30d
    < min_fills` 直接判 ineligible，不是「未定」），正確數字是
    `rows==297`／`total_pending==289`／`total_ineligible==3`
    （8+289+3==300，候選無一遺漏，只是 3 個不合格的沒有進 rows）。本測試以
    此為準（派工單已預先核可這個修正）。
    """
    store = ExploreStore(tmp_path / "explore.db")
    now = 1_800_000_000.0
    now_ms = int(now * 1000)
    portfolio_raw = _portfolio_raw([1000, 1050], [1000] * 60)   # live_days=59、dd 小
    for i in range(300):
        addr = f"0x{i:040x}"
        store.upsert_candidates([(addr, f"trader{i}", i, 0.0)], as_of=now)
        store.put_cache_ok(addr, "portfolio", portfolio_raw, fetched_at=now - 100,
                          refresh_after=now + 1000)
        if i < 8:
            store.insert_fills_page(addr, _stats_fills(200, time_ms=now_ms - 1000),
                                    _sync(addr, completeness="complete", updated_at=now))
        elif i < 11:
            store.insert_fills_page(addr, _stats_fills(50, time_ms=now_ms - 1000),
                                    _sync(addr, completeness="complete", updated_at=now))
        # 其餘 11..299（289 個）完全不同步——`store.get_sync` 回 None。

    snap_path = tmp_path / "snap.json"
    index = ExploreIndex(cfg=ExploreConfig(page_size=500), now_fn=lambda: now,
                         snapshot_path=str(snap_path))
    pub = ExplorePublisher(store=store, index=index, cfg=ExploreConfig(), now_fn=lambda: now,
                           snapshot_path=str(snap_path))
    pub.mark_dirty()
    assert pub.maybe_publish() is True

    result = index.query()
    assert len(result["rows"]) == 297
    assert result["total_qualified"] == 8
    assert result["total_pending"] == 289
    assert result["total_ineligible"] == 3


def test_recovery_b_swap_80_of_300_candidates_new_ones_pending_old_ones_gone(tmp_path):
    """(b) 已有版本 300 列（全部 eligible）→ 候選換掉其中 80 個（新地址從未
    被 enrich 過，任何資料都沒有）→ 發布成功；新 80 個以 `pending`／
    `portfolio_missing` 出列，舊 80 個（已停用、不在 `active_candidates()`）
    完全不在 `rows`。"""
    store = ExploreStore(tmp_path / "explore.db")
    now = 1_800_000_000.0
    now_ms = int(now * 1000)
    portfolio_raw = _portfolio_raw([1000, 1050], [1000] * 60)
    old_addrs = [f"0x{i:040x}" for i in range(300)]
    for addr in old_addrs:
        store.upsert_candidates([(addr, addr[:8], 1, 0.0)], as_of=now)
        store.put_cache_ok(addr, "portfolio", portfolio_raw, fetched_at=now - 100,
                          refresh_after=now + 1000)
        store.insert_fills_page(addr, _stats_fills(200, time_ms=now_ms - 1000),
                                _sync(addr, completeness="complete", updated_at=now))

    snap_path = tmp_path / "snap.json"
    clock = [now]
    index = ExploreIndex(cfg=ExploreConfig(page_size=500), now_fn=lambda: clock[0],
                         snapshot_path=str(snap_path))
    pub = ExplorePublisher(store=store, index=index, cfg=ExploreConfig(), now_fn=lambda: clock[0],
                           snapshot_path=str(snap_path))
    pub.mark_dirty()
    assert pub.maybe_publish() is True
    first = index.query()
    assert first["total_qualified"] == 300
    assert len(first["rows"]) == 300

    removed = set(old_addrs[:80])
    kept = old_addrs[80:]
    new_addrs = [f"0x{i:040x}" for i in range(1000, 1080)]
    for addr in new_addrs:
        store.upsert_candidates([(addr, addr[:8], 1, 0.0)], as_of=now)
    store.deactivate_missing(set(kept) | set(new_addrs))

    # Task 6.7（W3）：非 force——推進 fake clock 越過 `min_interval_s`（預設
    # 60s），證明門檻真的移除（不是靠 force 繞過所有節流/來源故障檢查）。
    clock[0] += 61.0
    pub.mark_dirty()
    assert pub.maybe_publish() is True
    second = index.query()
    addrs_in_rows = {r["address"] for r in second["rows"]}
    assert not (removed & addrs_in_rows)           # 舊 80 個不在 rows
    assert set(new_addrs) <= addrs_in_rows         # 新 80 個出列
    new_rows = [r for r in second["rows"] if r["address"] in new_addrs]
    assert all(r["eligibility"] == "pending" for r in new_rows)
    assert all(r["eligibility_reason"] == "portfolio_missing" for r in new_rows)
    assert second["total_qualified"] == 220        # 220 個舊候選仍 eligible
    assert second["total_pending"] == 80


def test_recovery_c_shrinking_from_20_eligible_to_12_not_blocked(tmp_path):
    """(c) 已有版本 20 列皆 `eligible` → 新資料（更新後的 portfolio 快取）
    證明其中 8 個 `live_days` 不足 30 天 → 發布成功、`total_qualified==12`
    （榜縮小到 60%，不被任何門檻擋下——P6 D12：合格人數真的下降就讓榜縮小，
    不用數量門檻掩蓋）。

    （用 `live_days` 而非 `order_count_30d` 示範「新資料證明不合格」：
    `fills` 在 store 裡是只增不減的累加表——同地址重新 `insert_fills_page`
    無法讓已收到的筆數變少，`portfolio` 快取則是 upsert 語意，改一次
    `put_cache_ok` 就能代表「重新抓到的最新資料」，兩者都是 `classify()`
    四個已知維度之一，示範的是同一條程式碼路徑：`ineligible` 不擋發布。）"""
    store = ExploreStore(tmp_path / "explore.db")
    now = 1_800_000_000.0
    now_ms = int(now * 1000)
    healthy_portfolio = _portfolio_raw([1000, 1050], [1000] * 60)   # live_days=59
    addrs = [f"0x{i:040x}" for i in range(20)]
    for addr in addrs:
        store.upsert_candidates([(addr, addr[:8], 1, 0.0)], as_of=now)
        store.put_cache_ok(addr, "portfolio", healthy_portfolio, fetched_at=now - 100,
                          refresh_after=now + 1000)
        store.insert_fills_page(addr, _stats_fills(200, time_ms=now_ms - 1000),
                                _sync(addr, completeness="complete", updated_at=now))

    snap_path = tmp_path / "snap.json"
    clock = [now]
    index = ExploreIndex(cfg=ExploreConfig(page_size=100), now_fn=lambda: clock[0],
                         snapshot_path=str(snap_path))
    pub = ExplorePublisher(store=store, index=index, cfg=ExploreConfig(), now_fn=lambda: clock[0],
                           snapshot_path=str(snap_path))
    pub.mark_dirty()
    assert pub.maybe_publish() is True
    assert index.query()["total_qualified"] == 20

    # 新資料揭露：前 8 個地址重新抓到的 portfolio 只有 5 天的 allTime 序列
    # （live_days=4 < min_trading_days=30）。
    degraded_portfolio = _portfolio_raw([1000, 1050], [1000] * 5)
    for addr in addrs[:8]:
        store.put_cache_ok(addr, "portfolio", degraded_portfolio, fetched_at=now + 10,
                          refresh_after=now + 2000)

    # Task 6.7（W3）：非 force——推進 fake clock 越過 `min_interval_s`。
    clock[0] += 61.0
    pub.mark_dirty()
    assert pub.maybe_publish() is True
    result = index.query()
    assert result["total_qualified"] == 12
    assert result["total_ineligible"] == 8


def test_load_snapshot_v3_migration_sets_fills_truncated_true(tmp_path):
    """2026-09-21 複審 W1：契約 A `fills_truncated == (coverage.state != "complete")`；
    v3 遷移列 coverage 為 backfilling，旗標必須同步為 True。"""
    snap = tmp_path / "snap.json"
    row = _v3_row_dict(_A)
    row["fills_truncated"] = False
    snap.write_text(json.dumps({"version": 3, "built_at": 1000.0, "total_scanned": 1,
                                "rows": [row]}))
    loaded = hl_explore.load_snapshot(str(snap))
    assert loaded is not None
    r = loaded["rows"][0]
    assert r.fills_coverage["state"] == "backfilling"
    assert r.fills_truncated is True
    assert r.to_dict()["fills_truncated"] is True
