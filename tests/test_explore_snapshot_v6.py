"""tests/test_explore_snapshot_v6.py — EXPLORE_INDEX_VERSION 5 → 6
（plan 2026-11-16-strategy-return-hl-roi D2：`max_dd_pct` 數值語意改變）。

舊版（v5）快照的回撤是舊公式算的、沒有可回填的新值：載入端必須回 None
（走冷建語意），不得把舊語意的回撤當成新版服務。"""
import json

from spark.publicapi import hl_explore


def test_v5_snapshot_is_rejected_not_served_as_v6(tmp_path):
    path = tmp_path / "explore_snapshot.json"
    path.write_text(json.dumps({"version": 5, "built_at": 1.0,
                                "total_scanned": 0, "rows": []}))
    assert hl_explore.load_snapshot(str(path)) is None


def test_v6_snapshot_round_trips(tmp_path):
    path = tmp_path / "explore_snapshot.json"
    hl_explore.dump_snapshot(str(path), rows=[], built_at=2.0, total_scanned=0)
    assert json.loads(path.read_text())["version"] == 6
    loaded = hl_explore.load_snapshot(str(path))
    assert loaded is not None and loaded["built_at"] == 2.0
