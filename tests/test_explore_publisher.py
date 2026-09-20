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

# ============================================================
# Task 3.5 C：發布門檻（min_portfolio_ratio）＋ v3 快照備份
# ============================================================

def test_maybe_publish_blocked_when_below_portfolio_ratio_threshold(tmp_path):
    """既有版本＋只有 10% 候選有 portfolio、非 force → 擋下發布、`gate_skips==1`、
    index／快照都不變。（Task 3.6 A：`force` 改為連這個比例門檻也一併繞過，
    這裡改用可變 clock 越過 `min_interval_s` 節流，不再借用 `force=True` 來
    測試節流——`force=True` 的比例門檻繞過另見
    `test_maybe_publish_force_bypasses_ratio_gate`。Task 3.7 A：輸入端預檢命中
    時 `last_gate` 前綴為 `in:`——compose 根本沒被呼叫到。）"""
    store = ExploreStore(tmp_path / "explore.db")
    store.upsert_candidates([(_A, "Alice", 1, 0.1)], as_of=1000.0)
    portfolio_raw = _portfolio_raw([1000, 1000], [1000] * 60)
    store.put_cache_ok(_A, "portfolio", portfolio_raw, fetched_at=900.0, refresh_after=2000.0)
    index = _dummy_index()
    now = [1000.0]
    pub = ExplorePublisher(store=store, index=index, cfg=_cfg(), now_fn=lambda: now[0],
                           snapshot_path=None)
    pub.mark_dirty()
    assert pub.maybe_publish() is True   # 第一次發布：index 從未有版本，不套門檻
    first = index.query()

    # 新增 9 個沒有 portfolio 的候選，10 個裡只有 1 個（10%）有 portfolio。
    for i in range(9):
        addr = "0x" + f"{i:02d}" * 20
        store.upsert_candidates([(addr, f"trader{i}", i + 2, 0.0)], as_of=1000.0)

    now[0] += 60.0   # 越過 min_interval，非 force 也能重新嘗試
    pub.mark_dirty()
    assert pub.maybe_publish() is False
    assert pub.status()["gate_skips"] == 1
    assert pub.status()["last_gate"] == "in:1/10"
    assert index.query() == first


def test_maybe_publish_force_bypasses_ratio_gate(tmp_path):
    """Task 3.6 A：`force=True` 繞過門檻檢查——10% 覆蓋率下仍發布，
    `gate_skips` 不遞增（Task 3.6 D 驗收點）。"""
    store = ExploreStore(tmp_path / "explore.db")
    store.upsert_candidates([(_A, "Alice", 1, 0.1)], as_of=1000.0)
    portfolio_raw = _portfolio_raw([1000, 1000], [1000] * 60)
    store.put_cache_ok(_A, "portfolio", portfolio_raw, fetched_at=900.0, refresh_after=2000.0)
    index = _dummy_index()
    pub = ExplorePublisher(store=store, index=index, cfg=_cfg(), now_fn=lambda: 1000.0,
                           snapshot_path=None)
    pub.mark_dirty()
    assert pub.maybe_publish() is True

    for i in range(9):
        addr = "0x" + f"{i:02d}" * 20
        store.upsert_candidates([(addr, f"trader{i}", i + 2, 0.0)], as_of=1000.0)

    pub.mark_dirty()
    assert pub.maybe_publish(force=True) is True
    assert pub.status()["gate_skips"] == 0
    assert index.query()["total_scanned"] == 10


def test_maybe_publish_blocked_when_n_is_zero(tmp_path):
    """Critical 修法：候選來源整批回空（`n == 0`，例如 300 候選全被停用）時，
    舊公式 `with_pf < ratio * n` 在 `n == 0` 恆為 False，會誤放行、把 0 列
    發布上 index 並覆寫快照——新公式 `n == 0` 直接判定 `gate_blocked`，
    快照不落地（`status()["min_portfolio_ratio"]` 一併驗證）。"""
    store = ExploreStore(tmp_path / "explore.db")
    index = _dummy_index()
    index.set_published([], {"published_at": 1.0, "candidates": 0})
    snap_path = tmp_path / "snap.json"
    pub = ExplorePublisher(store=store, index=index, cfg=_cfg(), now_fn=lambda: 1000.0,
                           snapshot_path=str(snap_path))
    pub.mark_dirty()
    assert pub.maybe_publish() is False
    assert pub.status()["gate_skips"] == 1
    assert pub.status()["min_portfolio_ratio"] == 0.8
    assert not snap_path.exists()


def test_maybe_publish_blocked_when_with_pf_is_zero(tmp_path):
    """Critical 修法的另一形狀：`n > 0` 但沒有任何候選有 portfolio
    （`with_pf == 0`）——同樣要擋下，不因 `ratio * n` 較小的邊界情況誤放行。"""
    store = ExploreStore(tmp_path / "explore.db")
    store.upsert_candidates([(_A, "Alice", 1, 0.1)], as_of=1000.0)
    index = _dummy_index()
    index.set_published([], {"published_at": 1.0, "candidates": 0})
    pub = ExplorePublisher(store=store, index=index, cfg=_cfg(), now_fn=lambda: 1000.0,
                           snapshot_path=None)
    pub.mark_dirty()
    assert pub.maybe_publish() is False
    assert pub.status()["gate_skips"] == 1
    assert pub.status()["last_gate"] == "in:0/1"


def test_maybe_publish_no_gate_when_index_never_published(tmp_path):
    """index 從未有版本（`rows is None`）→ 不套門檻，即使 portfolio 覆蓋率 0%
    也照樣發布（首次上線必經狀態）。"""
    store = ExploreStore(tmp_path / "explore.db")
    store.upsert_candidates([(_A, "Alice", 1, 0.1), (_B, "Bob", 2, 0.2)], as_of=1000.0)
    # 兩個候選都沒有 portfolio 快取（0% 覆蓋率）。
    index = _dummy_index()
    pub = ExplorePublisher(store=store, index=index, cfg=_cfg(), now_fn=lambda: 1000.0,
                           snapshot_path=None)
    pub.mark_dirty()
    assert pub.maybe_publish() is True
    assert index.query()["initializing"] is False


def test_maybe_publish_passes_gate_at_or_above_threshold(tmp_path):
    """80% 以上有 portfolio → 通過門檻，正常換版。"""
    store = ExploreStore(tmp_path / "explore.db")
    store.upsert_candidates([(_A, "Alice", 1, 0.1)], as_of=1000.0)
    portfolio_raw = _portfolio_raw([1000, 1000], [1000] * 60)
    store.put_cache_ok(_A, "portfolio", portfolio_raw, fetched_at=900.0, refresh_after=2000.0)
    index = _dummy_index()
    pub = ExplorePublisher(store=store, index=index, cfg=_cfg(), now_fn=lambda: 1000.0,
                           snapshot_path=None)
    pub.mark_dirty()
    assert pub.maybe_publish() is True
    for i in range(1):  # 補一個有 portfolio 的候選，仍是 100% 覆蓋率。
        addr = _B
        store.upsert_candidates([(addr, "Bob", 2, 0.2)], as_of=1000.0)
        store.put_cache_ok(addr, "portfolio", portfolio_raw, fetched_at=900.0,
                          refresh_after=2000.0)
    pub.mark_dirty()
    assert pub.maybe_publish(force=True) is True
    assert pub.status()["gate_skips"] == 0


def test_maybe_publish_gate_skip_still_updates_last_attempt_but_not_last_published(tmp_path):
    """節流改看 `last_attempt_at`（成功與失敗都更新）；`last_published_at`
    只在成功時更新。門檻擋下（失敗）也要更新 `last_attempt_at`，讓下一次
    `force=False` 的呼叫仍受 `min_interval_s` 節流保護（不會被擋下之後立刻
    重新一直嘗試 compose）。"""
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
    for i in range(9):
        addr = "0x" + f"{i:02d}" * 20
        store.upsert_candidates([(addr, f"trader{i}", i + 2, 0.0)], as_of=1000.0)

    now[0] += 100.0
    pub.mark_dirty()
    assert pub.maybe_publish() is False   # 門檻擋下
    last_published_before = pub.status()["last_published_at"]

    now[0] += 10.0   # 未滿 60s
    pub.mark_dirty()
    assert pub.maybe_publish() is False   # 應仍被 min_interval 節流，不會又跑一次 compose
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
# Task 3.7 A/E：門檻改判 compose 輸出（不再只看輸入端 payload 數）
# ============================================================

def test_maybe_publish_blocked_when_compose_output_is_empty(tmp_path, monkeypatch):
    """Critical 修法：輸入端 `with_pf/n = 10/10` 全數通過預檢，但
    `compose_rows` 實際輸出 0 列（HL portfolio 結構一變、`enrich_candidate`
    整列丟棄）→ 換版前的輸出側門檻要擋下，不寫快照、不換版，`last_gate` 以
    `out:` 開頭。"""
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
    assert pub.maybe_publish() is False
    assert pub.status()["gate_skips"] == 1
    assert pub.status()["last_gate"].startswith("out:")
    assert pub.status()["last_gate"] == "out:0/10"
    assert not snap_path.exists()


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


def test_maybe_publish_blocked_output_gate_still_bypassed_by_force(tmp_path, monkeypatch):
    """`force=True` 仍繞過輸出側門檻。"""
    from spark.publicapi import explore_publisher as ep

    store = ExploreStore(tmp_path / "explore.db")
    store.upsert_candidates([(_A, "Alice", 1, 0.1)], as_of=1000.0)
    portfolio_raw = _portfolio_raw([1000, 1000], [1000] * 60)
    store.put_cache_ok(_A, "portfolio", portfolio_raw, fetched_at=900.0, refresh_after=2000.0)
    index = _dummy_index()
    pub = ep.ExplorePublisher(store=store, index=index, cfg=_cfg(), now_fn=lambda: 1000.0,
                              snapshot_path=None)
    pub.mark_dirty()
    assert pub.maybe_publish() is True   # 首次發布建立版本

    monkeypatch.setattr(ep, "compose_rows", lambda *a, **kw: (
        [], {"published_at": 1000.0, "candidates": 1, "with_portfolio": 0}))
    pub.mark_dirty()
    assert pub.maybe_publish(force=True) is True
    assert index.query()["rows"] == []


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
