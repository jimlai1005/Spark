"""tests/test_public_stats_fee_source.py
Opus 審查 Suggestion 2：`public_stats.BUILDER_FEE_BPS` 改由
`spark.config.Settings.f`（單一事實來源）導出，不再自己另立一個常數——本檔鎖住
這個導出關係本身，不只是鎖住數值 2（`test_public_stats.py` 已經釘住數值）。
"""
from spark.config import Settings
from spark.publicapi.public_stats import BUILDER_FEE_BPS


def test_builder_fee_bps_derives_from_settings_f():
    """f 是「十分之一 bp」單位（`Settings.__post_init__` 的 charged_pct = f/1000）；
    bps＝f 再除以 10。用同一條算式反推，不寫死兩邊都是 2 巧合相等。"""
    assert BUILDER_FEE_BPS == Settings.f // 10
