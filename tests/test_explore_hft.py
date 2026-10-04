"""HFT 過濾器（plan docs/superpowers/plans/2026-10-05-explore-hft-filter.md Task 2）：
H1 純函式與 ExploreConfig 參數。"""
from decimal import Decimal

from spark.publicapi.hl_explore import (DEFAULT_HFT_COOLDOWN_S, DEFAULT_HFT_MAX_FILLS_30D,
                                        DEFAULT_HFT_MAX_VLM_MONTH_USD, ExploreConfig,
                                        hft_by_volume)


def _row(addr: str, vlm):
    perf = {"roi": "0.5"}
    if vlm is not None:
        perf["vlm"] = vlm
    return {"ethAddress": addr, "windowPerformances": [["month", perf]]}


def test_defaults_match_spec():
    assert DEFAULT_HFT_MAX_VLM_MONTH_USD == Decimal("100000000")
    assert DEFAULT_HFT_MAX_FILLS_30D == 6000
    assert DEFAULT_HFT_COOLDOWN_S == 30 * 86400
    cfg = ExploreConfig()
    assert cfg.hft_max_vlm_month_usd == DEFAULT_HFT_MAX_VLM_MONTH_USD
    assert cfg.hft_max_fills_30d == DEFAULT_HFT_MAX_FILLS_30D
    assert cfg.hft_cooldown_s == DEFAULT_HFT_COOLDOWN_S


def test_from_env_reads_three_hft_vars():
    cfg = ExploreConfig.from_env({"EXPLORE_HFT_MAX_VLM_MONTH_USD": "50000000",
                                  "EXPLORE_HFT_MAX_FILLS_30D": "3000",
                                  "EXPLORE_HFT_COOLDOWN_S": "86400"})
    assert cfg.hft_max_vlm_month_usd == Decimal("50000000")
    assert cfg.hft_max_fills_30d == 3000
    assert cfg.hft_cooldown_s == 86400


def test_hft_by_volume_flags_at_or_above_threshold_lowercased():
    payload = {"leaderboardRows": [
        _row("0xAAA", "100000000"),      # 等於門檻 → 命中
        _row("0xBBB", "99999999.99"),    # 低於 → 不命中
        _row("0xCCC", "1.5e9"),          # 科學記號字串 → 命中
        _row("0xDDD", None),             # 缺 vlm → 不命中
        _row("0xEEE", "NaN"),            # NaN → 不命中
        {"ethAddress": "", "windowPerformances": []},  # 壞列 → 跳過
        "garbage",
    ]}
    assert hft_by_volume(payload, Decimal("100000000")) == {"0xaaa", "0xccc"}


def test_hft_by_volume_disabled_when_threshold_non_positive():
    payload = {"leaderboardRows": [_row("0xAAA", "1e12")]}
    assert hft_by_volume(payload, Decimal("0")) == set()
    assert hft_by_volume(None, Decimal("1")) == set()
