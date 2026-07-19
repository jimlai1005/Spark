"""成本熔斷器 **獨立** 規格測試（獨立測試者撰寫）。

判準來源：`docs/superpowers/plans/2026-07-19-cost-circuit-breaker.md`
（驗收條件 1-8 ＋ 設計決定 D2/D4/D5/D6/D8）。

本檔刻意**未參考** `tests/test_copy_costbreaker.py`（實作者自己的測試），
以免複製實作者的盲點——這正是本檔存在的理由。判準一律以計畫書為準：
「程式碼照自己的邏輯跑」不算通過，必須是「計畫說的行為確實發生」。

全離線（tests/conftest.py 的 autouse socket ban）。
"""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import spark.copytrade.loop as loop_mod
from spark.copytrade.config import CopySettings
from spark.copytrade.costbreaker import (
    WINDOW_S,
    BreachLog,
    check_cost,
    evaluate_cost,
    is_enabled,
    save_log,
    window_fills,
)
from spark.copytrade.executor import ActionExecutor
from spark.copytrade.killswitch import ARM_FILE_RELPATH
from spark.copytrade.loop import run_cycle
from spark.copytrade.notifier import RecordingNotifier
from spark.copytrade.orders import CycleReport, ReconcileResult, ReconcileState, _build_desired
from spark.copytrade.positions import sync_positions
from spark.exchange.base import (
    AccountSnapshot,
    BuilderCode,
    EquityView,
    OpenOrder,
    OrderResult,
    Position,
    UserFill,
)
from spark.exchange.fakes import FakeAdapter

NOW = 1_700_000_000.0          # 固定時鐘，所有時間斷言以它為錨
MY_ADDR = "0xme"
BUILDER = BuilderCode(b="0xbuilder", f=20)
NO_SLEEP = lambda *_a, **_k: None  # noqa: E731 —— 重試退避不真睡


# ══════════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════════


def _settings(**kw) -> CopySettings:
    kw.setdefault("volatility_weight_enabled", False)
    return CopySettings(**kw)


def _ev(current="1000", peak=None) -> EquityView:
    return EquityView(current=Decimal(current), recent_peak=Decimal(peak or current))


def _fill(*, ts: float | datetime, px="100", sz="1", coin="ETH", oid=1,
          naive=False) -> UserFill:
    t = ts if isinstance(ts, datetime) else datetime.fromtimestamp(ts, timezone.utc)
    if naive:
        t = t.replace(tzinfo=None)
    return UserFill(time=t, coin=coin, px=Decimal(px), sz=Decimal(sz), side="B",
                    crossed=True, oid=oid, fee=Decimal("0"))


def _fills_notional(total: str, *, now_s: float = NOW, n: int = 1) -> list[UserFill]:
    """n 筆總名目為 total 的成交（皆落在窗內）。"""
    each = Decimal(total) / n
    return [_fill(ts=now_s - 60 - i, px=str(each), sz="1", oid=i) for i in range(n)]


def _account(value="1000", ntl="0") -> AccountSnapshot:
    return AccountSnapshot(account_value=Decimal(value), total_margin_used=Decimal("0"),
                           withdrawable=Decimal(value), total_ntl_pos=Decimal(ntl))


def _pos(coin="ETH", szi="1", entry_px="2000") -> Position:
    return Position(coin=coin, szi=Decimal(szi), entry_px=Decimal(entry_px), leverage=5,
                    is_cross=True, unrealized_pnl=Decimal("0"), margin_used=Decimal("0"))


def _order(coin="ETH", *, reduce_only=False, oid=1, sz="1", px="2000") -> OpenOrder:
    return OpenOrder(oid=oid, coin=coin, is_buy=True, limit_px=Decimal(px),
                     sz=Decimal(sz), reduce_only=reduce_only, is_trigger=False,
                     trigger_px=None, tpsl=None)


class _EquityProbe(FakeAdapter):
    """記錄任何「再讀一次權益」的嘗試。同輪同源（D2）下 evaluate_cost 一次都不該讀。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.equity_reads = 0

    def get_account_value(self, address: str) -> Decimal:
        self.equity_reads += 1
        return Decimal("999999")  # 刻意與傳入的 ev.current 天差地遠


class _RaisingFills(FakeAdapter):
    """get_user_fills 一律拋指定例外，並計呼叫次數（驗重試語意）。"""

    def __init__(self, exc: Exception, **kw):
        super().__init__(**kw)
        self._exc = exc
        self.fill_calls = 0

    def get_user_fills(self, address, start, end):
        self.fill_calls += 1
        raise self._exc


class _FakeExec:
    """ExecutorPort 替身，記錄所有動作（positions gate 測試用）。"""

    def __init__(self):
        self.records: list[tuple] = []

    def place(self, spec) -> bool:
        self.records.append(("place", spec.coin))
        return True

    def modify(self, oid, spec) -> bool:
        self.records.append(("modify", oid))
        return True

    def cancel(self, coin, oid) -> bool:
        self.records.append(("cancel", coin, oid))
        return True

    def market_open(self, coin, is_buy, size) -> OrderResult:
        self.records.append(("market_open", coin, is_buy, size))
        return OrderResult(ok=True, filled_size=size, avg_px=Decimal("0"), raw={})

    def close_reduce_only(self, coin, is_buy, size, *, emergency=False) -> OrderResult:
        self.records.append(("close_reduce_only", coin, is_buy, size))
        return OrderResult(ok=True, filled_size=size, avg_px=Decimal("0"), raw={})

    def update_leverage(self, coin, leverage, is_cross) -> bool:
        self.records.append(("update_leverage", coin))
        return True

    def get_open_orders(self) -> list:
        return []

    def get_size_decimals(self, coin) -> int:
        return 4

    def kinds(self) -> list[str]:
        return [r[0] for r in self.records]


def _eval(adapter, root: Path, *, ev=None, settings=None, notifier=None, now_s=NOW):
    """evaluate_cost 的短呼叫包裝（固定時鐘、不真睡）。"""
    n = notifier or RecordingNotifier()
    st = evaluate_cost(adapter, MY_ADDR, ev or _ev(), settings or _settings(), n, root,
                       now_fn=lambda: now_s, sleep_fn=NO_SLEEP)
    return st, n


def _executor(adapter) -> ActionExecutor:
    return ActionExecutor(adapter, None, BUILDER, live=False, my_address=MY_ADDR,
                          settings=_settings())


def _run_cycle(adapter, root: Path, *, settings=None, notifier=None, ex=None):
    n = notifier or RecordingNotifier()
    e = ex or _executor(adapter)
    report = run_cycle(adapter, e, settings or _settings(), n, ReconcileState(), root)
    return report, n, e


def _texts(notifier: RecordingNotifier, level: str | None = None) -> list[str]:
    return [r[2] for r in notifier.records if level is None or r[0] == level]


# ══════════════════════════════════════════════════════════════════════
# AC6 / D4：滾動窗以 fill 時間戳為準，跨午夜不重置
# ══════════════════════════════════════════════════════════════════════


def test_window_includes_fill_at_exact_lower_edge():
    """窗邊界值：age 恰好 == 24h 的 fill 仍算在窗內（邊界含入）。"""
    f = _fill(ts=NOW - WINDOW_S)
    assert window_fills([f], now_s=NOW) == [f]


def test_window_excludes_fill_one_second_beyond_edge():
    assert window_fills([_fill(ts=NOW - WINDOW_S - 1)], now_s=NOW) == []


def test_window_includes_fill_at_exactly_now():
    f = _fill(ts=NOW)
    assert window_fills([f], now_s=NOW) == [f]


def test_window_discards_future_timestamps():
    """未來戳（時鐘前跳／可被影響的時間戳）不得永久佔住換手率額度。"""
    assert window_fills([_fill(ts=NOW + 1)], now_s=NOW) == []
    assert window_fills([_fill(ts=NOW + 86400 * 5)], now_s=NOW) == []


def test_naive_fill_timestamp_treated_as_utc():
    f = _fill(ts=NOW - 3600, naive=True)
    assert window_fills([f], now_s=NOW) == [f]


def test_rolling_window_does_not_reset_at_midnight():
    """AC6：23:50 與次日 00:10 的成交，在 00:20 這一刻必須**同時**計入。

    日曆日實作會在午夜丟掉前一天那筆——攻擊者即可跨午夜規避（D4）。
    """
    before_midnight = datetime(2026, 7, 18, 23, 50, tzinfo=timezone.utc)
    after_midnight = datetime(2026, 7, 19, 0, 10, tzinfo=timezone.utc)
    now = datetime(2026, 7, 19, 0, 20, tzinfo=timezone.utc).timestamp()
    fills = [_fill(ts=before_midnight, px="6000", sz="1", oid=1),
             _fill(ts=after_midnight, px="6000", sz="1", oid=2)]

    st = check_cost(_ev("1000"), fills, _settings(), now_s=now)
    assert st.fill_count == 2, "跨午夜的兩筆都必須在滾動窗內"
    assert st.notional == Decimal("12000"), "日曆日實作只會看到其中一筆（6000）"
    assert st.turnover == Decimal("12")


def test_rolling_window_midnight_span_actually_breaches():
    """把跨午夜的兩筆加大到超門檻：日曆日實作只會看到一半 → 不觸發。"""
    before_midnight = datetime(2026, 7, 18, 23, 50, tzinfo=timezone.utc)
    after_midnight = datetime(2026, 7, 19, 0, 10, tzinfo=timezone.utc)
    now = datetime(2026, 7, 19, 0, 20, tzinfo=timezone.utc).timestamp()
    fills = [_fill(ts=before_midnight, px="15000", sz="1", oid=1),
             _fill(ts=after_midnight, px="15000", sz="1", oid=2)]

    st = check_cost(_ev("1000"), fills, _settings(), now_s=now)
    assert st.notional == Decimal("30000")
    assert st.turnover == Decimal("30")
    assert st.breached is True


def test_fills_outside_window_are_not_counted():
    old = _fill(ts=NOW - WINDOW_S - 10, px="99999", sz="99")
    st = check_cost(_ev("1000"), [old], _settings(), now_s=NOW)
    assert st.notional == Decimal("0")
    assert st.fill_count == 0
    assert st.breached is False


# ══════════════════════════════════════════════════════════════════════
# AC1：門檻語意與邊界值
# ══════════════════════════════════════════════════════════════════════


def test_turnover_exactly_at_threshold_does_not_breach():
    """「超過門檻」＝嚴格大於。恰好等於上限不觸發。"""
    st = check_cost(_ev("1000"), _fills_notional("20000"), _settings(), now_s=NOW)
    assert st.turnover == Decimal("20")
    assert st.breached is False


def test_turnover_just_above_threshold_breaches():
    st = check_cost(_ev("1000"), _fills_notional("20001"), _settings(), now_s=NOW)
    assert st.breached is True
    assert "turnover" in st.reasons


def test_fill_count_exactly_at_threshold_does_not_breach():
    fills = _fills_notional("1", now_s=NOW, n=400)
    st = check_cost(_ev("1000000"), fills, _settings(), now_s=NOW)
    assert st.fill_count == 400
    assert st.breached is False


def test_fill_count_above_threshold_breaches():
    fills = _fills_notional("1", now_s=NOW, n=401)
    st = check_cost(_ev("1000000"), fills, _settings(), now_s=NOW)
    assert st.fill_count == 401
    assert st.breached is True
    assert "fill_count" in st.reasons


def test_empty_window_never_breaches():
    st = check_cost(_ev("1000"), [], _settings(), now_s=NOW)
    assert (st.breached, st.turnover, st.notional, st.fill_count) == (
        False, Decimal("0"), Decimal("0"), 0)


def test_zero_equity_disables_turnover_but_keeps_fill_count():
    """權益 0：換手率無定義 → 該項停用；成交筆數項不需分母，照常把關。"""
    big = _fills_notional("999999", now_s=NOW, n=1)
    st = check_cost(_ev("0"), big, _settings(), now_s=NOW)
    assert st.turnover == Decimal("0")
    assert st.breached is False, "權益 0 時換手率項必須停用（不得除以 0 或誤觸）"

    many = _fills_notional("1", now_s=NOW, n=401)
    st2 = check_cost(_ev("0"), many, _settings(), now_s=NOW)
    assert st2.breached is True and "fill_count" in st2.reasons


def test_negative_equity_disables_turnover_metric():
    st = check_cost(_ev("-500"), _fills_notional("999999"), _settings(), now_s=NOW)
    assert st.turnover == Decimal("0")
    assert "turnover" not in st.reasons


def test_zero_equity_emits_warning_not_silence(tmp_path):
    """「無資料」不是「安全」——權益 degenerate 必須留痕。"""
    fa = FakeAdapter(fills=_fills_notional("5000"))
    st, n = _eval(fa, tmp_path, ev=_ev("0"))
    assert st.breached is False
    assert any("degenerate" in t or "權益" in t for t in _texts(n, "warn")), \
        "權益 0 必須發 warn，不得靜默"


# ══════════════════════════════════════════════════════════════════════
# AC4 ⭐ 同源同輪：分母只能是 get_account_value（perp），且必須本輪本次讀取
# ══════════════════════════════════════════════════════════════════════


def test_evaluate_cost_never_rereads_equity(tmp_path):
    """⭐ 分子分母必須同一輪同一次讀取：evaluate_cost 不得自己再讀權益。

    變異測試性質：若實作改成在此重讀 get_account_value，equity_reads > 0 → 紅。
    """
    probe = _EquityProbe(fills=_fills_notional("25000"))
    st, _ = _eval(probe, tmp_path, ev=_ev("1000"))

    assert probe.equity_reads == 0, "evaluate_cost 重讀了權益 → 分子分母不同輪（混基準）"
    assert st.equity == Decimal("1000"), "分母必須是呼叫端傳入的本輪 EquityView.current"
    assert st.turnover == Decimal("25")


def test_evaluate_cost_never_touches_other_equity_endpoints(tmp_path):
    """⭐ 不得改用 portfolio / account_state 當分母（D2 明列的禁區）。"""
    fa = FakeAdapter(fills=_fills_notional("25000"),
                     equity=EquityView(current=Decimal("10000"),
                                       recent_peak=Decimal("10000")),
                     account=_account("9999"))
    st, _ = _eval(fa, tmp_path, ev=_ev("1000"))

    assert "get_equity_view" not in fa.calls, "碰了 portfolio 端點（分母灌水）"
    assert "get_account_state" not in fa.calls, "碰了 account_state（另一個端點）"
    assert st.equity == Decimal("1000")


def test_denominator_swap_would_flip_the_verdict(tmp_path):
    """⭐ 這一條就是「變異測試證明」：同一組 fills，perp 分母觸發、portfolio 分母不觸發。

    perp 權益 1000 → 25000/1000 = 25× > 20 → 觸發。
    若分母被換成 portfolio(10000) → 2.5× → 不觸發（保護靜默失效）。
    """
    fa = FakeAdapter(fills=_fills_notional("25000"),
                     equity=EquityView(current=Decimal("10000"),
                                       recent_peak=Decimal("10000")),
                     account=_account("10000"))
    st, _ = _eval(fa, tmp_path, ev=_ev("1000"))
    assert st.breached is True, "用 perp 分母必須觸發；未觸發＝分母被灌水了"

    portfolio_turnover = Decimal("25000") / Decimal("10000")
    assert portfolio_turnover < _settings().cost_max_turnover_24h, \
        "測試設計檢查：portfolio 分母確實不會觸發，故本測試對分母來源敏感"


def test_spot_fills_must_not_inflate_perp_turnover():
    """⭐ 同基準的另一半：**分子**也必須是 perp。

    D2 只明文規定分母（perp accountValue），但同基準是雙向的：分子若含現貨成交，
    就成了「(perp＋spot 名目) ÷ perp 權益」——正是工程原則 1 的混源比較，
    與 findings F1（分母含 spot）是同一個錯的鏡像。

    本專案對「這不是我們的 perp 範圍」已有既定慣例 `instrument._is_spot_coin`
    （orders.py:_build_desired 與 positions.py:sync_positions 兩處都用它排除現貨），
    成本熔斷器的分子未套用該慣例。

    實害不是「偏保守」而已：客戶自己在同一錢包做一筆大額現貨，換手率被灌爆 →
    誤觸 → 停開新倉；24h 內誤觸 3 次 → 累犯升級 → kill switch **強制平掉
    客戶的 perp 部位**。誤報在此路徑上是會動到客戶部位的。
    """
    spot = [_fill(ts=NOW - 60, px="25000", sz="1", coin="PURR/USDC", oid=1),
            _fill(ts=NOW - 30, px="25000", sz="1", coin="@107", oid=2)]
    st = check_cost(_ev("1000"), spot, _settings(), now_s=NOW)

    assert st.notional == Decimal("0"), \
        "現貨成交不得計入 perp 換手率的分子（分母是 perp 權益）"
    assert st.breached is False


def test_non_primary_dex_fills_must_not_inflate_perp_turnover():
    """同上，另一個面向：非主 DEX 標的（`xyz:NVDA`）在跟單兩處閘門都被排除，
    但其成交仍被計入成本熔斷器的分子。"""
    dex = [_fill(ts=NOW - 60, px="25000", sz="1", coin="xyz:NVDA", oid=1)]
    st = check_cost(_ev("1000"), dex, _settings(), now_s=NOW)

    assert st.notional == Decimal("0"), "非主 DEX 成交不得計入主 DEX perp 的換手率"
    assert st.breached is False


def test_fills_are_queried_for_our_own_address_not_leader(tmp_path):
    """磨損發生在客戶帳上——分子必須查我方地址。"""
    fa = FakeAdapter(fills=[])
    _eval(fa, tmp_path)
    assert fa.calls["get_user_fills"][0]["address"] == MY_ADDR


def test_fills_query_window_matches_rolling_window(tmp_path):
    fa = FakeAdapter(fills=[])
    _eval(fa, tmp_path)
    call = fa.calls["get_user_fills"][0]
    span = call["end"].timestamp() - call["start"].timestamp()
    assert span == pytest.approx(WINDOW_S)


def test_run_cycle_denominator_is_perp_account_value(tmp_path, monkeypatch):
    """⭐ 端到端：引擎層的分母也必須是 perp accountValue。

    account_value(perp)=1000、portfolio=10000、account_state=9999 三者刻意不同；
    fills 名目 25000。只有用 1000 當分母才會觸發 → no_new_exposure=True。
    """
    captured = {}

    def fake_sync(ex, leader_orders, my_orders, my_positions, scale, **kw):
        captured.update(kw)
        return CycleReport(reconcile=ReconcileResult(0, 0, 0, 0, False, ()),
                           safety_net={}, scale=scale)

    monkeypatch.setattr(loop_mod, "sync_open_orders", fake_sync)
    fa = FakeAdapter(account_value=Decimal("1000"),
                     equity=EquityView(current=Decimal("10000"),
                                       recent_peak=Decimal("10000")),
                     account=_account("9999"),
                     fills=_fills_notional("25000", now_s=NOW))
    # fills 用固定 NOW，但引擎用真實時鐘 → 改用「相對現在」的 fills
    import time as _t
    fa._fills = _fills_notional("25000", now_s=_t.time())

    _run_cycle(fa, tmp_path)
    assert captured["no_new_exposure"] is True, \
        "以 perp 1000 為分母應觸發；未觸發代表分母來自別的端點"


# ══════════════════════════════════════════════════════════════════════
# AC1 告警內容（D5 要求四項齊全）
# ══════════════════════════════════════════════════════════════════════


def test_breach_emits_critical_with_required_fields(tmp_path):
    fa = FakeAdapter(fills=_fills_notional("25000"))
    st, n = _eval(fa, tmp_path)
    assert st.breached is True

    crits = [r for r in n.records if r[0] == "critical" and r[1] == "costbreaker"]
    assert len(crits) == 1, f"觸發必須發且只發一則 critical，實得 {n.records}"
    text = crits[0][2]
    assert "25" in text, "訊息須含目前換手率"
    assert "20" in text, "訊息須含門檻"
    assert "24h" in text or "24 h" in text, "訊息須含窗口"
    assert "換手率" in text, "訊息須含觸發了哪一項"
    assert "reduce-only" in text, "訊息須說明平倉仍放行（不會把客戶困在部位裡）"


def test_breach_alert_is_critical_level_not_warn(tmp_path):
    fa = FakeAdapter(fills=_fills_notional("25000"))
    _, n = _eval(fa, tmp_path)
    assert [r[0] for r in n.records] == ["critical"]


# ══════════════════════════════════════════════════════════════════════
# AC2：自動恢復 ＋ 告警
# ══════════════════════════════════════════════════════════════════════


def test_recovery_clears_gate_and_alerts(tmp_path):
    fa = FakeAdapter(fills=_fills_notional("25000"))
    st1, _ = _eval(fa, tmp_path)
    assert st1.breached is True

    fa._fills = _fills_notional("100")          # 換手率掉回門檻下
    st2, n2 = _eval(fa, tmp_path)
    assert st2.breached is False, "換手率回落必須自動恢復（不能停到有人發現為止）"
    assert n2.records, "恢復必須留痕（發告警）"
    assert any("恢復" in t for t in _texts(n2)), f"恢復告警缺失：{n2.records}"


def test_recovery_alert_only_on_transition_not_every_round(tmp_path):
    fa = FakeAdapter(fills=_fills_notional("25000"))
    _eval(fa, tmp_path)
    fa._fills = []
    _, n2 = _eval(fa, tmp_path)
    _, n3 = _eval(fa, tmp_path)
    assert len(n2.records) == 1
    assert n3.records == [], "已恢復狀態不應每輪重發恢復告警"


def test_breach_alert_only_on_edge_not_every_breached_round(tmp_path):
    fa = FakeAdapter(fills=_fills_notional("25000"))
    _, n1 = _eval(fa, tmp_path)
    _, n2 = _eval(fa, tmp_path)
    assert len(n1.records) == 1
    assert n2.records == [], "持續超標不應每輪重發（60s 一輪會洗版）"


# ══════════════════════════════════════════════════════════════════════
# AC3 / D6：滾動 24h 內觸發 3 次 → escalate（trip kill switch，需人工 re-arm）
# ══════════════════════════════════════════════════════════════════════


def _episode(fa, root, *, now_s, notifier=None):
    """製造一次完整的 触发→恢復 episode，回傳觸發那一輪的 status。"""
    fa._fills = _fills_notional("25000", now_s=now_s)
    st, _ = _eval(fa, root, notifier=notifier, now_s=now_s)
    fa._fills = []
    _eval(fa, root, notifier=notifier, now_s=now_s + 1)
    return st


def test_consecutive_breached_rounds_count_as_one_episode(tmp_path):
    """連續超標算 1 次——否則 60s 一輪的引擎 3 分鐘就自動升級到 kill switch。"""
    fa = FakeAdapter(fills=_fills_notional("25000"))
    last = None
    for i in range(10):
        last, _ = _eval(fa, tmp_path, now_s=NOW + i)
    assert last.breach_count == 1
    assert last.escalate is False


def test_two_episodes_do_not_escalate(tmp_path):
    fa = FakeAdapter()
    _episode(fa, tmp_path, now_s=NOW)
    st2 = _episode(fa, tmp_path, now_s=NOW + 100)
    assert st2.breach_count == 2
    assert st2.escalate is False


def test_three_episodes_escalate(tmp_path):
    """AC3：滾動 24h 內第 3 次觸發 → escalate=True。"""
    fa = FakeAdapter()
    _episode(fa, tmp_path, now_s=NOW)
    _episode(fa, tmp_path, now_s=NOW + 100)
    st3 = _episode(fa, tmp_path, now_s=NOW + 200)
    assert st3.breach_count == 3
    assert st3.escalate is True


def test_escalate_count_one_escalates_on_first_breach(tmp_path):
    fa = FakeAdapter(fills=_fills_notional("25000"))
    st, _ = _eval(fa, tmp_path, settings=_settings(cost_breach_escalate_count=1))
    assert st.escalate is True


def test_escalate_count_zero_treated_as_one(tmp_path):
    """config 註記：0/1 皆視為「一次即升級」。"""
    fa = FakeAdapter(fills=_fills_notional("25000"))
    st, _ = _eval(fa, tmp_path, settings=_settings(cost_breach_escalate_count=0))
    assert st.escalate is True


def test_breaches_older_than_window_do_not_count_toward_escalation(tmp_path):
    """滾動窗：25 小時前的兩次觸發已出窗，本次是窗內第 1 次 → 不升級。"""
    save_log(tmp_path, BreachLog(breaches=(NOW - 25 * 3600, NOW - 26 * 3600),
                                 active=False))
    fa = FakeAdapter(fills=_fills_notional("25000"))
    st, _ = _eval(fa, tmp_path)
    assert st.breach_count == 1
    assert st.escalate is False


def test_breaches_inside_window_do_count_toward_escalation(tmp_path):
    save_log(tmp_path, BreachLog(breaches=(NOW - 23 * 3600, NOW - 22 * 3600),
                                 active=False))
    fa = FakeAdapter(fills=_fills_notional("25000"))
    st, _ = _eval(fa, tmp_path)
    assert st.breach_count == 3
    assert st.escalate is True


def test_clock_rollback_drops_future_breach_records(tmp_path):
    """時鐘倒退：記錄在「未來」的觸發戳被丟棄（同 window_fills 的未來戳防護）。

    副作用：時鐘往回跳會清掉累犯計數。此處釘住現況並在回報中標為觀察項。
    """
    save_log(tmp_path, BreachLog(breaches=(NOW + 3600, NOW + 7200), active=False))
    fa = FakeAdapter(fills=_fills_notional("25000"))
    st, _ = _eval(fa, tmp_path)
    assert st.breach_count == 1
    assert st.escalate is False


# ══════════════════════════════════════════════════════════════════════
# AC5：重啟後不重置（無帳本，每輪從 fills 重算）
# ══════════════════════════════════════════════════════════════════════


def test_first_ever_run_breaches_immediately_without_warmup(tmp_path):
    """全新啟動、無任何狀態檔 → 換手率從 fills 直接算出並觸發（無暖機期）。"""
    assert not (tmp_path / "var").exists()
    fa = FakeAdapter(fills=_fills_notional("25000"))
    st, _ = _eval(fa, tmp_path)
    assert st.breached is True
    assert st.notional == Decimal("25000")


def test_restart_does_not_reset_turnover_budget(tmp_path):
    """「重啟」＝下一次呼叫從磁碟重讀狀態。換手率仍是超標的（來自交易所 fills）。"""
    fa = FakeAdapter(fills=_fills_notional("25000"))
    st1, _ = _eval(fa, tmp_path)
    st2, _ = _eval(fa, tmp_path)          # 模擬重啟後的第一輪
    assert st1.breached and st2.breached
    assert st2.notional == st1.notional, "重啟不得讓已發生的成交名目歸零"


def test_state_file_loss_does_not_open_the_main_gate(tmp_path):
    """判定歷史遺失 ⇒ 累犯計數歸零是可接受代價，但**主閘不得因此放行**。"""
    fa = FakeAdapter(fills=_fills_notional("25000"))
    _eval(fa, tmp_path)
    (tmp_path / "var/copytrade/cost_breaker.json").unlink()

    st, _ = _eval(fa, tmp_path)
    assert st.breached is True, "狀態檔遺失後主閘必須仍然關著（換手率由 fills 重算）"


def test_corrupt_state_file_alerts_and_keeps_gate_working(tmp_path):
    p = tmp_path / "var/copytrade/cost_breaker.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    fa = FakeAdapter(fills=_fills_notional("25000"))
    st, n = _eval(fa, tmp_path)

    assert st.breached is True
    assert any(r[0] == "critical" and "狀態" in r[2] for r in n.records), \
        "狀態檔壞掉必須告警（讀不到 ≠ 沒有歷史）"


def test_state_write_failure_does_not_break_the_gate(tmp_path):
    """留痕失敗不得換掉保護本身：狀態檔寫不進去，主閘仍必須關著。"""
    bad_root = tmp_path / "not_a_dir"
    bad_root.write_text("i am a file")          # mkdir 會 NotADirectoryError
    fa = FakeAdapter(fills=_fills_notional("25000"))
    st, _ = _eval(fa, bad_root)
    assert st.breached is True


def test_both_reasons_reported_together(tmp_path):
    """換手率與成交筆數同時超標 → 兩項都要出現在 reasons 與告警文字。"""
    s = _settings(cost_max_fills_24h=10)
    fa = FakeAdapter(fills=_fills_notional("25000", n=11))
    st, n = _eval(fa, tmp_path, settings=s)
    assert set(st.reasons) == {"turnover", "fill_count"}
    text = [r[2] for r in n.records if r[0] == "critical"][0]
    assert "換手率" in text and "成交筆數" in text


def test_escalate_stays_true_on_a_recovered_round(tmp_path):
    """特徵化：escalate 只看窗內觸發**次數**，不要求本輪仍在觸發。

    引擎路徑上無害（run_cycle 一升級就 trip，而 trip 會 reset 判定歷史），
    但代表 escalate 不是「本輪要升級」而是「窗內已達累犯門檻」。
    任何未來的第二個呼叫端若只看 escalate 而不 trip，會持續看到 True。
    """
    fa = FakeAdapter()
    _episode(fa, tmp_path, now_s=NOW)
    _episode(fa, tmp_path, now_s=NOW + 100)
    _episode(fa, tmp_path, now_s=NOW + 200)      # 第 3 次 → escalate

    fa._fills = []
    st, _ = _eval(fa, tmp_path, now_s=NOW + 300)  # 本輪完全沒成交
    assert st.breached is False
    assert st.escalate is True


def test_escalation_history_survives_restart(tmp_path):
    """累犯計數跨「重啟」保留（狀態檔是判定歷史，不是帳本）。"""
    fa = FakeAdapter()
    _episode(fa, tmp_path, now_s=NOW)
    _episode(fa, tmp_path, now_s=NOW + 100)
    # 此處每次 _eval 都重新 load_log／save_log，等同重啟
    st3 = _episode(fa, tmp_path, now_s=NOW + 200)
    assert st3.escalate is True


# ══════════════════════════════════════════════════════════════════════
# AC7：fills 查詢失敗 → transient，沿用上一輪判定，不得放行
# ══════════════════════════════════════════════════════════════════════


def test_fills_failure_after_breach_keeps_gate_closed(tmp_path):
    """⭐ 上一輪已觸發 + 本輪查不到 fills → **必須繼續擋**，絕不因查不到而放行。"""
    fa = FakeAdapter(fills=_fills_notional("25000"))
    _eval(fa, tmp_path)                       # 建立 active=True

    bad = _RaisingFills(ConnectionError("connection reset"))
    st, n = _eval(bad, tmp_path)
    assert st.stale is True
    assert st.breached is True, "查不到 fills 就放行＝保護失效"
    assert any(r[0] == "critical" for r in n.records), "stale 必須大聲告警"


def test_fills_failure_without_prior_breach_carries_previous_verdict(tmp_path):
    """上一輪沒觸發 → 沿用「照常」。仍必須 stale=True ＋ critical 留痕。"""
    bad = _RaisingFills(ConnectionError("timed out"))
    st, n = _eval(bad, tmp_path)
    assert st.stale is True
    assert st.breached is False
    assert any(r[0] == "critical" for r in n.records)


def test_transient_fills_failure_is_retried(tmp_path):
    bad = _RaisingFills(ConnectionError("connection reset"))
    _eval(bad, tmp_path)
    assert bad.fill_calls == 3, "transient 讀取（冪等）必須重試"


def test_semantic_fills_failure_is_not_retried(tmp_path):
    """語意錯誤不重試（工程原則 2）——但仍不得放行。"""
    bad = _RaisingFills(ValueError("invalid address"))
    st, n = _eval(bad, tmp_path)
    assert bad.fill_calls == 1
    assert st.stale is True
    assert any(r[0] == "critical" for r in n.records)


def test_fills_failure_never_propagates_exception(tmp_path):
    """引擎不得因查不到 fills 而整輪崩掉（那會被主迴圈計為連續錯誤）。"""
    bad = _RaisingFills(RuntimeError("boom"))
    st, _ = _eval(bad, tmp_path)
    assert st.stale is True


def test_stale_status_reports_equity_from_the_passed_view(tmp_path):
    """即使 stale，分母仍必須是本輪傳入的 perp 權益（不得憑空造數或重讀）。"""
    bad = _RaisingFills(ConnectionError("timed out"))
    st, _ = _eval(bad, tmp_path, ev=_ev("777"))
    assert st.equity == Decimal("777")


def test_stale_round_does_not_wipe_breach_history(tmp_path):
    save_log(tmp_path, BreachLog(breaches=(NOW - 100, NOW - 50), active=False))
    bad = _RaisingFills(ConnectionError("timed out"))
    st, _ = _eval(bad, tmp_path)
    assert st.breach_count == 2, "stale 輪不得清掉累犯計數"


def test_stale_round_does_not_count_as_new_breach_episode(tmp_path):
    """stale 不是新的觸發事件——否則反覆斷線會自己升級到 kill switch。"""
    fa = FakeAdapter(fills=_fills_notional("25000"))
    _eval(fa, tmp_path)                       # episode 1
    bad = _RaisingFills(ConnectionError("timed out"))
    for _ in range(5):
        st, _ = _eval(bad, tmp_path)
    assert st.breach_count == 1
    assert st.escalate is False


# ══════════════════════════════════════════════════════════════════════
# AC8：門檻設為停用值 → 行為與現況完全一致（向後相容）
# ══════════════════════════════════════════════════════════════════════


def test_both_thresholds_zero_is_fully_disabled(tmp_path):
    s = _settings(cost_max_turnover_24h=Decimal("0"), cost_max_fills_24h=0)
    assert is_enabled(s) is False

    fa = FakeAdapter(fills=_fills_notional("9999999", n=500))
    st, n = _eval(fa, tmp_path, settings=s)

    assert st.enabled is False
    assert st.breached is False
    assert st.escalate is False
    assert dict(fa.calls) == {}, "停用時不得查 fills（不得有任何額外呼叫）"
    assert n.records == [], "停用時不得發任何通知"
    assert not (tmp_path / "var").exists(), "停用時不得讀寫狀態檔"


def test_disabled_ignores_existing_breach_history(tmp_path):
    save_log(tmp_path, BreachLog(breaches=(NOW - 10, NOW - 20, NOW - 30), active=True))
    s = _settings(cost_max_turnover_24h=Decimal("0"), cost_max_fills_24h=0)
    fa = FakeAdapter(fills=_fills_notional("9999999"))
    st, _ = _eval(fa, tmp_path, settings=s)
    assert st.escalate is False and st.breached is False


def test_only_turnover_disabled_still_enforces_fill_count(tmp_path):
    s = _settings(cost_max_turnover_24h=Decimal("0"), cost_max_fills_24h=10)
    fa = FakeAdapter(fills=_fills_notional("1", n=11))
    st, _ = _eval(fa, tmp_path, settings=s)
    assert st.breached is True and st.reasons == ("fill_count",)


def test_only_fill_count_disabled_still_enforces_turnover(tmp_path):
    s = _settings(cost_max_fills_24h=0)
    fa = FakeAdapter(fills=_fills_notional("25000"))
    st, _ = _eval(fa, tmp_path, settings=s)
    assert st.breached is True and st.reasons == ("turnover",)


def test_run_cycle_with_disabled_breaker_leaves_gates_open(tmp_path, monkeypatch):
    """向後相容：停用時 run_cycle 的下游閘門與加入本功能前一致。"""
    captured = {}

    def fake_sync(ex, leader_orders, my_orders, my_positions, scale, **kw):
        captured.update(kw)
        return CycleReport(reconcile=ReconcileResult(0, 0, 0, 0, False, ()),
                           safety_net={}, scale=scale)

    monkeypatch.setattr(loop_mod, "sync_open_orders", fake_sync)
    fa = FakeAdapter(account_value=Decimal("1000"), account=_account("1000"),
                     fills=_fills_notional("9999999"))
    s = _settings(cost_max_turnover_24h=Decimal("0"), cost_max_fills_24h=0)
    _run_cycle(fa, tmp_path, settings=s)

    assert captured["no_new_exposure"] is False
    assert "get_user_fills" not in fa.calls


# ══════════════════════════════════════════════════════════════════════
# D5 ⭐ 硬規則：reduce-only 一律放行，絕不把客戶困在部位裡
# ══════════════════════════════════════════════════════════════════════


def test_reduce_only_order_passes_the_gate():
    desired, skipped = _build_desired(
        [_order("ETH", reduce_only=True)], Decimal("1"),
        min_notional=Decimal("10"), size_decimals=lambda c: 4,
        my_positions={"ETH": _pos("ETH")}, protected=set(), no_new_exposure=True)
    assert [o.coin for o in desired] == ["ETH"], "reduce-only 掛單必須照常掛（D5 硬規則）"
    assert skipped == []


def test_non_reduce_only_order_is_blocked_by_the_gate():
    desired, skipped = _build_desired(
        [_order("ETH", reduce_only=False)], Decimal("1"),
        min_notional=Decimal("10"), size_decimals=lambda c: 4,
        my_positions={}, protected=set(), no_new_exposure=True)
    assert desired == []
    assert [s.reason for s in skipped] == ["cost_breaker"]


def test_gate_off_lets_normal_orders_through():
    desired, _ = _build_desired(
        [_order("ETH")], Decimal("1"), min_notional=Decimal("10"),
        size_decimals=lambda c: 4, my_positions={}, protected=set(),
        no_new_exposure=False)
    assert [o.coin for o in desired] == ["ETH"]


def _sync(leader, mine, *, no_new_exposure=True, scale="1"):
    ex = _FakeExec()
    res = sync_positions(ex, leader, mine, Decimal(scale), settings=_settings(),
                         notifier=RecordingNotifier(), size_decimals=lambda c: 4,
                         mids={"ETH": Decimal("2000")},
                         no_new_exposure=no_new_exposure)
    return ex, res


def test_follow_flatten_runs_during_breach():
    """leader 已平 → 跟平必須照常執行（純減倉）。"""
    ex, res = _sync({}, {"ETH": _pos("ETH", "1")})
    assert "close_reduce_only" in ex.kinds(), "熔斷期間仍必須跟著平倉"
    assert [f["coin"] for f in res["flattened"]] == ["ETH"]


def test_decrease_runs_during_breach():
    ex, res = _sync({"ETH": _pos("ETH", "1")}, {"ETH": _pos("ETH", "5")})
    assert "close_reduce_only" in ex.kinds(), "減倉是 reduce-only，熔斷期間必須放行"
    assert any(a["kind"] == "decrease" for a in res["adjusted"])


def test_flip_closes_old_leg_but_blocks_the_reopen():
    """反轉：平掉舊部位那一腿放行，反向重開被擋 → 曝險單向下降。"""
    ex, res = _sync({"ETH": _pos("ETH", "-2")}, {"ETH": _pos("ETH", "3")})
    kinds = ex.kinds()
    assert "close_reduce_only" in kinds, "反轉的平倉腿必須照常（不得把客戶困住）"
    assert "market_open" not in kinds, "反轉的重開腿必須被擋"
    assert any(s["reason"] == "cost_breaker_flip_open" for s in res["skipped"])


def test_new_open_blocked_during_breach():
    ex, res = _sync({"ETH": _pos("ETH", "1")}, {})
    assert ex.kinds() == []
    assert any(s["reason"] == "cost_breaker_open" for s in res["skipped"])


def test_increase_blocked_during_breach():
    ex, res = _sync({"ETH": _pos("ETH", "5")}, {"ETH": _pos("ETH", "1")})
    assert "market_open" not in ex.kinds()
    assert any(s["reason"] == "cost_breaker_increase" for s in res["skipped"])


def test_gate_off_allows_open_again():
    ex, _ = _sync({"ETH": _pos("ETH", "1")}, {}, no_new_exposure=False)
    assert "market_open" in ex.kinds()


# ══════════════════════════════════════════════════════════════════════
# D8：leader 撤銷 > kill switch（回撤）> 成本熔斷器
# ══════════════════════════════════════════════════════════════════════


def test_tripped_kill_switch_short_circuits_before_cost_breaker(tmp_path):
    """kill switch 已 tripped → 成本熔斷器完全不執行（連 fills 都不查）。"""
    arm = tmp_path / ARM_FILE_RELPATH
    arm.parent.mkdir(parents=True)
    arm.write_text("{}")
    fa = FakeAdapter(fills=_fills_notional("25000"))
    report, _, _ = _run_cycle(fa, tmp_path)

    assert report.tripped is True
    assert dict(fa.calls) == {}, "tripped 短路不得有任何 adapter 呼叫（含成本熔斷器）"


def test_drawdown_breach_returns_before_cost_breaker(tmp_path):
    """回撤熔斷成立 → 直接 return，成本熔斷器不得被諮詢、更不得覆寫其行為。"""
    import json
    import time as _t
    from spark.copytrade.equity import SAMPLES_RELPATH
    p = tmp_path / SAMPLES_RELPATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([[_t.time(), "1000"]]))

    fa = FakeAdapter(account_value=Decimal("700"), account=_account("700"),
                     positions=[], fills=_fills_notional("25000"))
    report, n, _ = _run_cycle(fa, tmp_path)

    assert report.tripped is True
    assert "get_user_fills" not in fa.calls, "回撤成立後不得再走成本熔斷器"
    assert any(r[3] == "dd_breach" for r in n.records)


def test_cost_breach_sets_no_new_exposure_on_both_gates(tmp_path, monkeypatch):
    """觸發 ⇒ 掛單對帳與部位安全網兩處都必須收到 no_new_exposure=True。"""
    import time as _t
    captured = {}
    net_kw = {}

    def fake_sync(ex, leader_orders, my_orders, my_positions, scale, **kw):
        captured.update(kw)
        return CycleReport(reconcile=ReconcileResult(0, 0, 0, 0, False, ()),
                           safety_net=kw["safety_net"](), scale=scale)

    def fake_positions(ex, leader_positions, my_positions, scale, **kw):
        net_kw.update(kw)
        return {}

    monkeypatch.setattr(loop_mod, "sync_open_orders", fake_sync)
    monkeypatch.setattr(loop_mod, "sync_positions", fake_positions)
    fa = FakeAdapter(account_value=Decimal("1000"), account=_account("1000"),
                     fills=_fills_notional("25000", now_s=_t.time()))
    _run_cycle(fa, tmp_path)

    assert captured["no_new_exposure"] is True
    assert net_kw["no_new_exposure"] is True


def test_no_breach_leaves_both_gates_open(tmp_path, monkeypatch):
    import time as _t
    captured = {}

    def fake_sync(ex, leader_orders, my_orders, my_positions, scale, **kw):
        captured.update(kw)
        return CycleReport(reconcile=ReconcileResult(0, 0, 0, 0, False, ()),
                           safety_net={}, scale=scale)

    monkeypatch.setattr(loop_mod, "sync_open_orders", fake_sync)
    fa = FakeAdapter(account_value=Decimal("1000"), account=_account("1000"),
                     fills=_fills_notional("100", now_s=_t.time()))
    _run_cycle(fa, tmp_path)
    assert captured["no_new_exposure"] is False


def test_escalation_trips_kill_switch_and_needs_manual_rearm(tmp_path):
    """AC3 端到端：第 3 次觸發 → ARM_FILE 落地、後續輪次短路，需人工刪檔才恢復。"""
    import time as _t
    now = _t.time()
    save_log(tmp_path, BreachLog(breaches=(now - 3600, now - 1800), active=False))
    fa = FakeAdapter(account_value=Decimal("1000"), account=_account("1000"),
                     positions=[], fills=_fills_notional("25000", now_s=now))

    report, _, _ = _run_cycle(fa, tmp_path)
    arm = tmp_path / ARM_FILE_RELPATH
    assert report.tripped is True
    assert arm.exists(), "累犯升級必須 trip kill switch（落 ARM_FILE）"

    fa.calls.clear()
    report2, _, _ = _run_cycle(fa, tmp_path)
    assert report2.tripped is True
    assert dict(fa.calls) == {}, "ARM_FILE 未人工刪除前必須持續短路"
    assert arm.exists(), "本模組不得自行 re-arm"


def test_escalation_ignores_flatten_on_breach_setting(tmp_path, monkeypatch):
    """特徵化（characterization）：flatten_on_breach=False 時，回撤路徑不 trip，
    但成本熔斷的累犯升級**仍**呼叫 trip（＝仍會強制平倉並鎖死）。

    計畫 D5 說成本熔斷器「不做強制平倉」、D8 說它「不得覆蓋前兩者的行為」，
    但 D6 又說累犯要 trip kill switch——三者對本情境的讀法不一致。
    此測試只釘住現況，歧義在回報中提出，不預設哪一邊是對的。
    """
    import time as _t
    calls = []
    monkeypatch.setattr(loop_mod, "trip", lambda *a, **k: calls.append((a, k)))
    now = _t.time()
    save_log(tmp_path, BreachLog(breaches=(now - 3600, now - 1800), active=False))
    fa = FakeAdapter(account_value=Decimal("1000"), account=_account("1000"),
                     positions=[_pos("ETH", "1")],
                     fills=_fills_notional("25000", now_s=now))

    report, _, _ = _run_cycle(fa, tmp_path, settings=_settings(flatten_on_breach=False))
    assert report.tripped is True
    assert len(calls) == 1
    assert calls[0][1].get("reason") == "cost_breach"


def test_escalation_trip_carries_cost_breach_reason(tmp_path, monkeypatch):
    """鎖死交易的理由必須寫在鎖上——ARM payload 要能區分回撤與成本。"""
    import time as _t
    now = _t.time()
    save_log(tmp_path, BreachLog(breaches=(now - 3600, now - 1800), active=False))
    fa = FakeAdapter(account_value=Decimal("1000"), account=_account("1000"),
                     positions=[], fills=_fills_notional("25000", now_s=now))
    _run_cycle(fa, tmp_path)

    import json
    payload = json.loads((tmp_path / ARM_FILE_RELPATH).read_text())
    assert payload.get("reason") == "cost_breach"
