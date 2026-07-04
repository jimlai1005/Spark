"""CLI 入口 smoke test：可 import、main 存在（邏輯已在單元測試覆蓋）。"""
import scripts.reconcile_day
import scripts.run_testnet_flow


def test_entrypoints_importable():
    assert callable(scripts.run_testnet_flow.main)
    assert callable(scripts.reconcile_day.main)
