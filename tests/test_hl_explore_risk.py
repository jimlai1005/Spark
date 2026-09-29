"""tests/test_hl_explore_risk.py — `hl_explore.RiskSummary` / `risk_from_clearinghouse`
（plan `docs/superpowers/plans/2026-09-30-leader-truth-and-liq-risk.md` Task 4）。

單一定義點：探索列（Task 5）與交易員詳情頁（Task 6）都只呼叫
`risk_from_clearinghouse(ch_state)`，不各自重算（工程原則 1）。全部從**同一次**
`clearinghouseState` 回應推導，零新增 HL 額度、零新增上游呼叫。

fixture 用 2026-09-30 抓到的 0xedea 真實回應形狀（plan Task 4 錨例）：
`marginSummary.accountValue="1278231.985074"`、
`crossMaintenanceMarginUsed="378974.694601"`、PUMP `szi="961627241.0"`、
`liquidationPx="0.0041602315"`，`positionValue` 由 `szi × 0.005738`
（構造出的 mark）算出 —— 使 `liq_distance_pct ≈ 27.5`、`maint_ratio ≈ 0.296`
兩個數字可以手算覆核（見下方 `test_real_shape_0xedea_matches_hand_calc`）。
"""
from decimal import Decimal

from spark.publicapi.hl_explore import RiskSummary, risk_from_clearinghouse


def _position(coin, szi, liquidation_px, position_value,
              leverage="10", margin_used="1"):
    """`assetPositions[i]` 的最小真實形狀（欄位名見
    `tests/fixtures/hl_payload_keys/mainnet-clearinghouseState.position.json`）。
    `liquidation_px`／`position_value` 可傳 `None`，序列化為 JSON `null`
    （交易所對某些方向／帳戶型態確實會回 `null`，見 plan Task 4 情境 3）。"""
    return {"position": {
        "coin": coin, "szi": str(szi),
        "leverage": {"type": "cross", "value": leverage},
        "marginUsed": margin_used,
        "liquidationPx": None if liquidation_px is None else str(liquidation_px),
        "positionValue": None if position_value is None else str(position_value),
    }}


def _ch_state(account_value, maint_used, positions, cross_account_value=None):
    """C2（2026-09-30 reviewer）：`crossMarginSummary.accountValue` 是
    `_maint_ratio` 真正的分母（同源同基準，工程原則 1）。預設
    `cross_account_value=None` 時與 `account_value` 同值（純 cross 帳戶，
    plan Task 4 的 0xedea 錨例即此情境，數字不受本次修正影響）；傳入不同的
    `cross_account_value` 供 `test_maint_ratio_uses_cross_account_value_not_full_account`
    構造「有 isolated 部位、全帳戶權益 > cross 權益」的情境。"""
    return {
        "marginSummary": {"accountValue": str(account_value)},
        "crossMarginSummary": {"accountValue": str(
            account_value if cross_account_value is None else cross_account_value)},
        "crossMaintenanceMarginUsed": str(maint_used),
        "assetPositions": positions,
    }


def test_ch_state_none_returns_all_none():
    r = risk_from_clearinghouse(None)
    assert r == RiskSummary(None, None, None)


def test_empty_asset_positions_returns_all_none_liq_but_maint_ratio_still_computed():
    ch_state = _ch_state("1000", "100", [])
    r = risk_from_clearinghouse(ch_state)
    assert r.liq_distance_pct is None
    assert r.liq_coin is None
    assert r.maint_ratio == 0.1


def test_real_shape_0xedea_matches_hand_calc():
    """plan 錨例：mark = positionValue/|szi| = 0.005738（構造值）；
    多單 dist = (mark − liq)/mark ≈ 0.274968 → 27.5%；
    maint_ratio = 378974.694601/1278231.985074 ≈ 0.29648 → 0.296。"""
    szi = Decimal("961627241.0")
    mark = Decimal("0.005738")
    position_value = szi * mark
    ch_state = _ch_state(
        "1278231.985074", "378974.694601",
        [_position("PUMP", szi, "0.0041602315", position_value)],
    )
    r = risk_from_clearinghouse(ch_state)
    assert r.liq_distance_pct == 27.5
    assert r.liq_coin == "PUMP"
    assert r.maint_ratio == 0.296


def test_liquidation_px_null_position_skipped_other_positions_still_counted():
    """kPEPE follower 那種情境：某部位 `liquidationPx` 為 `null`（交易所對該
    方向/帳戶型態未回報強平價）——該部位不得讓整體解析失敗，也不得被當成
    「已越線」納入計算，直接跳過，改用其他部位的最近距離。"""
    no_liq = _position("kPEPE", "1000", None, "50")
    has_liq = _position("BTC", "1", "50000", "60000")  # mark=60000, dist=(60000-50000)/60000
    ch_state = _ch_state("10000", "500", [no_liq, has_liq])
    r = risk_from_clearinghouse(ch_state)
    assert r.liq_coin == "BTC"
    assert r.liq_distance_pct == float(round((Decimal(10000) / Decimal(60000) * 100), 1))
    assert r.maint_ratio == 0.05


def test_short_position_distance_formula():
    """空單：dist = (liq − mark)/mark。szi 負值。mark = positionValue/|szi|
    = 100/1 = 100；liq=120 → dist=(120-100)/100=0.2 → 20.0%。"""
    pos = _position("ETH", "-1", "120", "100")
    ch_state = _ch_state("5000", "0", [pos])
    r = risk_from_clearinghouse(ch_state)
    assert r.liq_coin == "ETH"
    assert r.liq_distance_pct == 20.0
    assert r.maint_ratio == 0.0


def test_already_past_liquidation_price_returns_negative_distance():
    """已越線（多單 mark < liq）：交易所尚未執行強平，`dist` 照實回負值，
    不得被 clamp 成 0 或 None（plan Task 4 情境 5，供前端顏色分級判斷）。"""
    pos = _position("SOL", "2", "150", "200")  # mark=100, liq=150 > mark
    ch_state = _ch_state("1000", "50", [pos])
    r = risk_from_clearinghouse(ch_state)
    assert r.liq_coin == "SOL"
    assert r.liq_distance_pct == -50.0
    assert r.maint_ratio == 0.05


def test_multiple_positions_takes_minimum_distance():
    near = _position("BTC", "1", "95000", "100000")   # mark=100000 dist=5%
    far = _position("ETH", "1", "50", "3000")          # mark=3000 dist~98.3%
    ch_state = _ch_state("10000", "0", [near, far])
    r = risk_from_clearinghouse(ch_state)
    assert r.liq_coin == "BTC"
    assert r.liq_distance_pct == 5.0


def test_maint_ratio_none_when_account_value_non_positive():
    ch_state = _ch_state("0", "100", [])
    r = risk_from_clearinghouse(ch_state)
    assert r.maint_ratio is None


def test_maint_ratio_none_when_maintenance_field_missing():
    ch_state = {"marginSummary": {"accountValue": "1000"}, "assetPositions": []}
    r = risk_from_clearinghouse(ch_state)
    assert r.maint_ratio is None


def test_zero_szi_position_ignored():
    pos = _position("DOGE", "0", "1", "1")
    ch_state = _ch_state("1000", "10", [pos])
    r = risk_from_clearinghouse(ch_state)
    assert r.liq_distance_pct is None
    assert r.liq_coin is None


# --- C2（2026-09-30 reviewer，工程原則 1）：分母同源同基準 ------------------
def test_maint_ratio_uses_cross_account_value_not_full_account():
    """有 isolated 部位時，`marginSummary.accountValue`（全帳戶）>
    `crossMarginSummary.accountValue`（cross 池自己的權益）——分母若誤用全帳戶
    權益，比值會被稀釋、低估。`crossMaintenanceMarginUsed=300`：
    用 cross 值（1000）→ 0.3；若誤用全帳戶值（2000）會得到 0.15。"""
    ch_state = _ch_state("2000", "300", [], cross_account_value="1000")
    r = risk_from_clearinghouse(ch_state)
    assert r.maint_ratio == 0.3
    assert r.maint_ratio != 0.15


def test_maint_ratio_none_when_cross_margin_summary_missing():
    """`crossMarginSummary` 整個鍵缺席（極舊或非預期形狀）→ 缺鍵降級為
    None，不得退回去讀 `marginSummary`（那正是 C2 要修掉的行為）。"""
    ch_state = {"marginSummary": {"accountValue": "1000"},
               "crossMaintenanceMarginUsed": "100", "assetPositions": []}
    r = risk_from_clearinghouse(ch_state)
    assert r.maint_ratio is None


# --- W3（2026-09-30 reviewer）：低於權益 1% 的殘量部位不塗色 -----------------
def test_residual_position_below_one_percent_of_account_value_is_filtered():
    """$1M 帳戶：一條 $5、距強平 3% 的殘量（$5 < 1% × $1,000,000 = $10,000，
    被過濾）與一條 $500k、距強平 40% 的主部位（不被過濾）→ 回主部位的 40.0，
    不被殘量的 3% 蓋過。"""
    residual = _position("DUST", "100", liquidation_px="0.0485",
                         position_value="5")             # mark=0.05, dist=3%（低於主部位的 40%）
    main = _position("BTC", "10", liquidation_px="30000",
                     position_value="500000")             # mark=50000, dist=(50000-30000)/50000=40%
    ch_state = _ch_state("1000000", "0", [residual, main])
    r = risk_from_clearinghouse(ch_state)
    assert r.liq_coin == "BTC"
    assert r.liq_distance_pct == 40.0


def test_residual_position_filter_skipped_when_account_value_non_positive():
    """`accountValue` 缺或 ≤ 0 → 不過濾（沒有基準就不猜）：即使部位名目值
    很小，`accountValue<=0` 時仍被納入計算。"""
    tiny = _position("DUST", "100", liquidation_px="0.0485", position_value="5")
    ch_state = _ch_state("0", "0", [tiny])
    r = risk_from_clearinghouse(ch_state)
    assert r.liq_coin == "DUST"
    assert r.liq_distance_pct == 3.0


# --- S1（2026-09-30 reviewer）：非有限 Decimal 降級為 None -------------------
def test_nan_liquidation_px_position_skipped():
    """交易所對某些部位回 `liquidationPx: "NaN"`（字串，非 `null`）——
    `Decimal("NaN")` 轉換本身不拋例外，若不做 `is_finite()` 檢查會混進距離
    比較，產生一個看似合法卻無意義的排序值。該部位須被當成「無強平價」跳過，
    其他部位照算。

    ⚠️ W2（2026-09-30 第二輪 reviewer）：`position_value` 必須提到 W3 殘量門檻
    （`RISK_MIN_POSITION_FRACTION × accountValue`）以上，否則這條部位不管
    NaN 檢查有沒有做都會被 W3 的殘量過濾器擋掉——測試會在 S1 的 `is_finite()`
    檢查被拔掉時仍然綠燈（vacuous）。這裡 accountValue=10000、門檻=100，
    `position_value=5000` 遠高於門檻，只有 S1 的檢查在運作才會被跳過。"""
    nan_liq = _position("kPEPE", "1000", liquidation_px="NaN", position_value="5000")
    has_liq = _position("BTC", "1", liquidation_px="50000", position_value="60000")
    ch_state = _ch_state("10000", "500", [nan_liq, has_liq])
    r = risk_from_clearinghouse(ch_state)
    assert r.liq_coin == "BTC"
    assert r.liq_distance_pct == float(round((Decimal(10000) / Decimal(60000) * 100), 1))

    # 變異防護：把同一條部位的 `liquidationPx` 換成合法值（且刻意設得比 BTC
    # 更接近強平），驗證它確實會被算進去——證明上面之所以被跳過是因為 NaN
    # 被 `_safe_decimal` 擋下，而不是因為 W3 殘量過濾器先把它濾掉了。
    # mark = 5000/1000 = 5；liq=4.75 → dist=(5-4.75)/5=0.05=5.0%（< BTC 的
    # ~16.7%，故會成為新的最小值）。
    valid_liq = _position("kPEPE", "1000", liquidation_px="4.75", position_value="5000")
    ch_state_valid = _ch_state("10000", "500", [valid_liq, has_liq])
    r_valid = risk_from_clearinghouse(ch_state_valid)
    assert r_valid.liq_coin == "kPEPE"
    assert r_valid.liq_distance_pct == 5.0


# --- C1（2026-09-30 第二輪 reviewer）：maint_ratio 分母非有限值降級為 None ---
def test_maint_ratio_none_when_cross_account_value_is_nan_string():
    """`crossMarginSummary.accountValue: "NaN"`——舊版 `Decimal(str(...))` 建構
    不拋，但下面 `<= 0` 的比較對 NaN 會拋 `InvalidOperation`，讓端點 500。
    改走 `_safe_decimal` 後必須是 `None`，不拋。"""
    ch_state = _ch_state("10000", "100", [], cross_account_value="NaN")
    r = risk_from_clearinghouse(ch_state)
    assert r.maint_ratio is None


def test_maint_ratio_none_when_cross_account_value_is_infinity_string():
    """`crossMarginSummary.accountValue: "Infinity"`——舊版 `Infinity <= 0` 為
    False（不拋），除法卻靜默得 0.0（看似合法、實則無意義）。改走
    `_safe_decimal` 後必須是 `None`，不是 0.0。"""
    ch_state = _ch_state("10000", "100", [], cross_account_value="Infinity")
    r = risk_from_clearinghouse(ch_state)
    assert r.maint_ratio is None
