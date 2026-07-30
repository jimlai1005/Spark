"""tests/test_risk_prefs.py
錢包主人自選風控（2026-07-30）：偏好的**驗證與正規化**，以及三條「壞資料不得靜默
變成無保護」的路徑。

⚠️ 本檔只測 `risk_prefs` 這個單一定義點。端點（簽章、落記錄、解除熔斷）在
`tests/test_api_risk_settings.py`；watcher 把已驗章的偏好變成 env 行在
`tests/test_filet_auto_activate.py`。偏好曾經走「pending 條目」那條無簽章路徑，
該路徑（連同 `set_pending_risk`）已於 2026-07-30 移除。
"""
import pytest

from spark.filet.risk_prefs import (RISK_ENV_KEYS, RiskPrefsError, canonical_prefs,
                                    default_prefs, prefs_summary, risk_env_lines,
                                    safe_fallback_prefs)


# ── 1. 預設與邊界 ────────────────────────────────────────────────────
def test_product_default_is_no_risk_controls():
    """2026-07-30 使用者裁決：新錢包預設不啟用任何風控。"""
    assert default_prefs()["enabled"] is False


def test_out_of_range_is_rejected_not_clamped():
    """夾取會讓客戶送了 A、系統套用 B，而且沒有人會知道（沿 capital_settings 裁決）。"""
    with pytest.raises(RiskPrefsError) as e:
        canonical_prefs({"enabled": True, "max_drawdown_pct": "0.01"})
    assert e.value.reason == "max_drawdown_pct_out_of_range"
    with pytest.raises(RiskPrefsError):
        canonical_prefs({"enabled": True, "max_drawdown_pct": "0.90"})


def test_details_are_validated_even_when_disabled():
    """⭐ enabled=False 也驗細項：現在收下超界值，等於埋一顆客戶啟用當下才炸的雷。"""
    with pytest.raises(RiskPrefsError):
        canonical_prefs({"enabled": False, "max_drawdown_pct": "0.99"})


def test_unknown_field_is_rejected():
    """打錯字不該被靜默忽略——客戶會以為自己設了某個東西。"""
    with pytest.raises(RiskPrefsError) as e:
        canonical_prefs({"enabled": True, "max_drawdwon_pct": "0.3"})
    assert e.value.reason == "unknown_field"


def test_non_bool_flag_rejected():
    with pytest.raises(RiskPrefsError):
        canonical_prefs({"enabled": "yes"})


def test_zero_total_drawdown_is_legal_meaning_that_gate_is_off():
    """累計回撤 0 ＝ 停用那一道（引擎既有語意，config.py:137）。"""
    assert canonical_prefs({"enabled": True,
                            "max_total_drawdown_pct": "0"})["max_total_drawdown_pct"] == "0"


# ── 1b. 缺鍵補值的來源（審查 F1）─────────────────────────────────────
def test_missing_enabled_is_rejected_when_required():
    """⭐⭐ 寫入路徑要求**明確**表達開或關：省略它會被讀成「關閉」，
    而那不該由一個缺漏的欄位決定（`{}` 的請求不得靜默關掉已開啟的風控）。"""
    with pytest.raises(RiskPrefsError) as e:
        canonical_prefs({}, require_enabled=True)
    assert e.value.reason == "enabled_missing"


def test_base_supplies_missing_keys_not_product_defaults():
    """F1 的另一半：缺鍵補值的來源是「這個帳號目前存的值」，不是產品預設——
    只送 enabled 的請求不該把客戶調過的 0.15 重設回 0.2。"""
    base = canonical_prefs({"enabled": True, "max_drawdown_pct": "0.15"})
    got = canonical_prefs({"enabled": False}, base=base, require_enabled=True)
    assert got["enabled"] is False
    assert got["max_drawdown_pct"] == "0.15"


# ── 2. env 行 ────────────────────────────────────────────────────────
def test_env_lines_cover_exactly_the_watcher_owned_keys():
    """env 行的鍵集必須與 GENERATED_KEYS 那一側用的常數一致，否則會漏寫一個鍵
    而讓它落回引擎預設（＝有保護），與客戶的選擇不符。"""
    keys = [ln.split("=", 1)[0] for ln in risk_env_lines(None)]
    assert tuple(keys) == RISK_ENV_KEYS


def test_env_lines_off_by_default_and_on_when_chosen():
    assert "COPY_RISK_CONTROLS_ENABLED=false" in risk_env_lines(None)
    on = risk_env_lines({"enabled": True, "max_drawdown_pct": "0.3",
                         "max_total_drawdown_pct": "0.5",
                         "flatten_on_breach": False})
    assert "COPY_RISK_CONTROLS_ENABLED=true" in on
    assert "COPY_MAX_DRAWDOWN_PCT=0.3" in on
    assert "COPY_MAX_TOTAL_DRAWDOWN_PCT=0.5" in on
    assert "COPY_FLATTEN_ON_BREACH=false" in on   # bool 必須是小寫 true/false


def test_safe_fallback_turns_protection_on():
    """⭐ 讀不懂客戶偏好時的方向是**開啟**保護：「資料壞了」與「客戶不要保護」
    必須產生不同結果，否則一次資料損壞就是一顆無保護的實盤引擎。"""
    assert safe_fallback_prefs()["enabled"] is True


def test_env_lines_revalidate_persisted_values():
    """落檔後被手改成超界值 → 上拋（呼叫端據此走安全側），不得原樣寫進 env。"""
    with pytest.raises(RiskPrefsError):
        risk_env_lines({"enabled": True, "max_drawdown_pct": "5"})


# ── 3. 呈現形式 ──────────────────────────────────────────────────────
def test_ratio_values_are_fixed_point_and_snapped_to_the_display_step():
    """F7＋觀察 1/2：落檔值對齊 0.1% 刻度且不用科學記號——
    否則畫面顯示 33.3% 卻套用 0.3333，或 env 出現 `=0E+9` 這種人讀不懂的行。"""
    p = canonical_prefs({"enabled": True, "max_drawdown_pct": "0.3333",
                         "max_total_drawdown_pct": "0E+1"})
    assert p["max_drawdown_pct"] == "0.333"
    assert p["max_total_drawdown_pct"] == "0"
    assert canonical_prefs({"enabled": True,
                            "max_drawdown_pct": "0." + "2" * 5000,
                            })["max_drawdown_pct"] == "0.222"


def test_summary_never_leaks_extra_keys():
    """回應的 prefs 只有宣告過的鍵（前端據此比對「已儲存 vs 表單現值」）。"""
    assert set(prefs_summary(None)["prefs"]) == {
        "enabled", "size_tolerance", "max_drawdown_pct", "max_total_drawdown_pct",
        "flatten_on_breach", "cooldown_hours"}
