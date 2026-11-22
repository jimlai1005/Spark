"""leader 觸發單（止盈／止損／條件進場）在開關關閉時的行為（plan 2026-11-21 T2）。

`mirror_triggers=False`（`_build_desired` 的函式參數預設；`CopySettings` 預設自 T7 起為 True，
需顯式關閉）：所有觸發單在 `_build_desired` 就略過（`trigger_disabled`），
不進 desired、不下單、不發 CRIT，只發一則以 oid 集合去重的 warn。
"""
from dataclasses import replace
from decimal import Decimal

from spark.copytrade.config import CopySettings
from spark.copytrade.executor import ActionExecutor
from spark.copytrade.instrument import _round_size
from spark.copytrade.notifier import RecordingNotifier
from spark.copytrade.orders import (
    OrderSpec,
    ReconcileState,
    SkippedOrder,
    _build_desired,
    _orders_match,
    sync_open_orders,
)
from spark.exchange.base import BuilderCode, OpenOrder, Position
from spark.exchange.fakes import FakeAdapter

from tests.test_copy_orders_crit_alert import FakeExecutor

# 本檔前半測「開關關閉時的行為」：CopySettings 預設自 T7（D15）起為 True，故顯式關閉。
SETTINGS = CopySettings(px_rel_tol=Decimal("1e-4"), size_tolerance=Decimal("0.08"),
                        mirror_triggers=False)


def _pos_tpsl(oid=1, coin="BTC", is_buy=True) -> OpenOrder:
    """倉位止損：sz=0、isPositionTpsl、Stop Market（2026-11-21 主網實測形狀）。"""
    return OpenOrder(
        oid=oid, coin=coin, is_buy=is_buy, limit_px=Decimal("67100"), sz=Decimal("0.0"),
        reduce_only=True, is_trigger=True, trigger_px=Decimal("66000"), tpsl="sl",
        is_market=True, is_position_tpsl=True,
    )


def _fixed_sl_limit(oid=2, coin="BTC", is_buy=True) -> OpenOrder:
    """固定數量止損限價：sz=0.015、Stop Limit。"""
    return OpenOrder(
        oid=oid, coin=coin, is_buy=is_buy, limit_px=Decimal("61000"), sz=Decimal("0.015"),
        reduce_only=True, is_trigger=True, trigger_px=Decimal("61000"), tpsl="sl",
        is_market=False,
    )


def _limit(oid=9, coin="BTC", is_buy=True, sz="0.015", px="57840", ro=True) -> OpenOrder:
    return OpenOrder(
        oid=oid, coin=coin, is_buy=is_buy, limit_px=Decimal(px), sz=Decimal(sz),
        reduce_only=ro, is_trigger=False, trigger_px=None, tpsl=None,
    )


def _short_btc() -> dict[str, Position]:
    return {"BTC": Position(coin="BTC", szi=Decimal("-0.5"), entry_px=Decimal("65000"),
                            leverage=5, is_cross=True, unrealized_pnl=Decimal("0"),
                            margin_used=Decimal("100"))}


def _build(leader, positions=None, **kw):
    return _build_desired(
        leader, Decimal("26.91"), min_notional=Decimal("10"),
        size_decimals=lambda coin: 5, my_positions=positions or {}, protected=set(), **kw,
    )


# ── 案例 1：倉位止損＋開關關 → desired 空、skipped 一筆 trigger_disabled 帶 oid ──
def test_position_tpsl_with_switch_off_is_skipped_with_oid():
    desired, skipped = _build([_pos_tpsl(oid=11)], _short_btc())
    assert desired == []
    assert skipped == [SkippedOrder("BTC", Decimal("0"), "trigger_disabled", oid=11)]


def test_switch_off_skips_even_without_position_or_under_breaker_and_protection():
    """觸發單略過在成本熔斷／抗單保護／無倉位判斷之前，原因一律是 trigger_disabled。"""
    desired, skipped = _build_desired(
        [_pos_tpsl(oid=1), _fixed_sl_limit(oid=2)], Decimal("26.91"),
        min_notional=Decimal("10"), size_decimals=lambda coin: 5, my_positions={},
        protected={"BTC"}, no_new_exposure=True,
    )
    assert desired == []
    assert [(s.reason, s.oid) for s in skipped] == [("trigger_disabled", 1),
                                                    ("trigger_disabled", 2)]


def test_non_trigger_orders_unaffected_by_switch_off():
    """限價單路徑不變：同一輪的 reduce-only 限價單照常進 desired。"""
    desired, skipped = _build([_limit(), _fixed_sl_limit()], _short_btc())
    assert len(desired) == 1
    assert desired[0].is_trigger is False and desired[0].reduce_only is True
    assert desired[0].sz == Decimal("0.40365")  # 0.015 × 26.91 = 0.40365（szDecimals 5）
    assert [s.reason for s in skipped] == ["trigger_disabled"]


# ── 案例 2：固定數量止損限價經 sync_open_orders：零下單、零 CRIT、恰一則 warn ──
def _sync(ex, leader, positions, state, notifier, *, settings=SETTINGS, my_orders=(),
          now=1000.0):
    return sync_open_orders(
        ex, leader, list(my_orders), positions, Decimal("26.91"),
        settings=settings, notifier=notifier, state=state, live=True,
        clock=lambda: now, sleep_fn=lambda s: None,
    )


def _warns(n):
    return [r for r in n.records if r[0] == "warn"]


def _crits(n):
    return [r for r in n.records if r[0] == "critical"]


def _places(ex):
    return [r for r in ex.records if r[0] == "place"]


def test_fixed_size_trigger_with_switch_off_no_place_no_crit_one_warn():
    ex = FakeExecutor()
    n = RecordingNotifier()
    res = _sync(ex, [_fixed_sl_limit(oid=2)], _short_btc(), ReconcileState(), n)
    assert _places(ex) == []  # 即 ex.place_outcome 呼叫 0 次（FakeExecutor 經它記錄 place）
    assert [r for r in ex.records if r[0] in ("place", "modify", "cancel")] == []
    assert _crits(n) == []
    assert res.reconcile.sync_failed is False
    warns = _warns(n)
    assert len(warns) == 1
    assert warns[0][1] == "orders"
    assert "[未鏡射] leader 在 BTC 有 1 張止盈／止損觸發單，本帳戶未跟單（觸發單鏡射未啟用）" in warns[0][2]
    assert "最多延遲一輪" in warns[0][2]


def test_position_tpsl_with_switch_off_via_sync_also_one_warn_no_place():
    ex = FakeExecutor()
    n = RecordingNotifier()
    _sync(ex, [_pos_tpsl(oid=1), _fixed_sl_limit(oid=2)], _short_btc(), ReconcileState(), n)
    assert _places(ex) == []
    assert _crits(n) == []
    warns = _warns(n)
    assert len(warns) == 1  # 同幣兩張聚合成一則
    assert "有 2 張止盈／止損觸發單" in warns[0][2]


def test_limit_order_still_mirrored_alongside_skipped_trigger():
    ex = FakeExecutor()
    n = RecordingNotifier()
    _sync(ex, [_limit(oid=9), _fixed_sl_limit(oid=2)], _short_btc(), ReconcileState(), n)
    placed = _places(ex)
    assert len(placed) == 1 and placed[0][1].is_trigger is False
    assert len(_warns(n)) == 1


# ── 案例 3：去重（oid 集合）＋消失清空 ───────────────────────────────
def test_warn_dedup_by_oid_set_rewarn_on_change_and_clear_on_disappear():
    ex = FakeExecutor()
    n = RecordingNotifier()
    state = ReconcileState()
    pos = _short_btc()

    # 各輪相隔 400 秒（> 每幣最短嘗試間隔 300 秒）：本測試驗的是 oid 集合去重，
    # 間隔封頂另有 test_warn_rate_capped_when_leader_changes_trigger_every_cycle。
    _sync(ex, [_fixed_sl_limit(oid=2)], pos, state, n, now=1000.0)
    assert len(_warns(n)) == 1
    assert state.trigger_skip_seen == {"BTC": frozenset({2})}

    _sync(ex, [_fixed_sl_limit(oid=2)], pos, state, n, now=1400.0)  # 同一組 oid → 不再 warn
    assert len(_warns(n)) == 1

    _sync(ex, [_fixed_sl_limit(oid=3)], pos, state, n, now=1800.0)  # leader 改價換 oid → 再 warn
    assert len(_warns(n)) == 2
    assert state.trigger_skip_seen == {"BTC": frozenset({3})}

    _sync(ex, [_fixed_sl_limit(oid=3), _pos_tpsl(oid=4)], pos, state, n, now=2200.0)  # 新增一張
    assert len(_warns(n)) == 3
    assert "有 2 張" in _warns(n)[-1][2]

    _sync(ex, [_limit(oid=9)], pos, state, n, now=2600.0)  # 觸發單全消失 → 清空，不告警
    assert state.trigger_skip_seen == {}
    assert len(_warns(n)) == 3

    _sync(ex, [_fixed_sl_limit(oid=3)], pos, state, n, now=3000.0)  # 再出現 → 重新告警
    assert len(_warns(n)) == 4


def test_warn_dedup_is_per_coin():
    ex = FakeExecutor()
    n = RecordingNotifier()
    state = ReconcileState()
    pos = _short_btc()
    _sync(ex, [_fixed_sl_limit(oid=2, coin="BTC")], pos, state, n)
    _sync(ex, [_fixed_sl_limit(oid=2, coin="BTC"),
               _fixed_sl_limit(oid=5, coin="ETH")], pos, state, n)
    warns = _warns(n)
    assert len(warns) == 2  # 第二輪只為 ETH 新增告警
    assert "ETH" in warns[1][2] and "BTC" not in warns[1][2]
    assert set(state.trigger_skip_seen) == {"BTC", "ETH"}
    _sync(ex, [_fixed_sl_limit(oid=2, coin="BTC")], pos, state, n)  # ETH 消失
    assert set(state.trigger_skip_seen) == {"BTC"}
    assert len(_warns(n)) == 2


def test_switch_on_does_not_use_trigger_disabled_path():
    """開關開啟時不走 trigger_disabled（T2 範圍內僅驗證不被該分支攔截；T4 起真的鏡射）。"""
    _desired, skipped = _build([_pos_tpsl(oid=1)], _short_btc(), mirror_triggers=True)
    assert all(s.reason != "trigger_disabled" for s in skipped)


# ── 案例 5：COPY_MIRROR_TRIGGERS ────────────────────────────────────
def test_mirror_triggers_defaults_true_and_env_false_disables():
    assert CopySettings().mirror_triggers is True
    assert CopySettings.from_env({}).mirror_triggers is True
    assert CopySettings.from_env({"COPY_MIRROR_TRIGGERS": "true"}).mirror_triggers is True
    assert CopySettings.from_env({"COPY_MIRROR_TRIGGERS": "false"}).mirror_triggers is False


def test_default_settings_mirror_position_tpsl_through_sync_open_orders():
    """預設 `CopySettings()`（不顯式給 mirror_triggers）走 sync_open_orders：
    leader 一張倉位止損＋follower 有同向倉位 → 對 follower 下一張倉位型觸發單。
    證明預設值真的接到下單路徑，而不只是欄位值變了（plan T7）。"""
    o = _pos_tpsl(oid=1)
    ex = _Ex5(open_orders_seq=[[_mirror(o, 60)], [_mirror(o, 60)]])
    n = RecordingNotifier()
    settings = CopySettings(px_rel_tol=Decimal("1e-4"), size_tolerance=Decimal("0.08"))
    assert settings.mirror_triggers is True
    rep = sync_open_orders(
        ex, [o], [], _short_btc(), Decimal("26.91"),
        settings=settings, notifier=n, state=ReconcileState(), live=True,
        clock=lambda: 1000.0, sleep_fn=lambda s: None,
    )
    placed = _places(ex)
    assert len(placed) == 1
    assert rep.reconcile.sync_failed is False
    assert _crits(n) == [] and _warns(n) == []  # 未被「trigger_disabled」略過、無 [未鏡射] warn


# ═══════════════════════════════════════════════════════════════════════
# T4：開關開啟（mirror_triggers=True）時引擎鏡射觸發單（plan 2026-11-21 T4）
# ═══════════════════════════════════════════════════════════════════════
import pytest  # noqa: E402

ON = CopySettings(px_rel_tol=Decimal("1e-4"), size_tolerance=Decimal("0.08"),
                  mirror_triggers=True)
SCALE = Decimal("26.91")
BUILDER = BuilderCode(b="0xbuilder", f=20)


def _entry_stop(oid=3, coin="BTC", is_buy=True, sz="0.015", limit_px="68000",
                trigger_px="67000", market=True) -> OpenOrder:
    """條件進場單：非 reduce-only 的止損（市價或限價）觸發單。"""
    return OpenOrder(
        oid=oid, coin=coin, is_buy=is_buy, limit_px=Decimal(limit_px), sz=Decimal(sz),
        reduce_only=False, is_trigger=True, trigger_px=Decimal(trigger_px), tpsl="sl",
        is_market=market,
    )


class _Ex5(FakeExecutor):
    """BTC szDecimals=5（與 _build 的 size_decimals 同源；FakeExecutor 預設 4）。"""

    def get_size_decimals(self, coin) -> int:
        return 5


def _on(leader, positions=None, **kw):
    return _build(leader, positions, mirror_triggers=True, **kw)


def _mirror(o: OpenOrder, oid: int, scale: Decimal = SCALE) -> OpenOrder:
    """follower 帳戶上、與 leader 單 `o` 對應的鏡射單（數量依引擎規則換算；倉位型恆 0）。"""
    sz = Decimal("0") if o.is_position_tpsl else _round_size(o.sz * scale, 5)
    return replace(o, oid=oid, sz=sz)


def _sync_on(ex, leader, positions, state, notifier, my_orders=(), **kw):
    return sync_open_orders(
        ex, leader, list(my_orders), positions, SCALE,
        settings=ON, notifier=notifier, state=state, live=True,
        clock=lambda: 1000.0, sleep_fn=lambda s: None, **kw,
    )


def _cancels(ex):
    return [r for r in ex.records if r[0] == "cancel"]


def _modifies(ex):
    return [r for r in ex.records if r[0] == "modify"]


# ── 測試 1：空倉（short）＋leader 倉位止損（買）→ desired 一筆倉位型 ──
def test_t4_01_position_tpsl_mirrored_as_position_type():
    desired, skipped = _on([_pos_tpsl(oid=11)], _short_btc())
    assert skipped == []
    assert len(desired) == 1
    d = desired[0]
    assert d.sz == Decimal("0") and d.is_position_tpsl is True and d.tpsl == "sl"
    assert d.is_trigger is True and d.reduce_only is True and d.is_buy is True
    assert d.trigger_px == Decimal("66000") and d.limit_px == Decimal("67100")  # 照抄
    assert d.is_market is True


def test_t4_01b_position_tpsl_with_zero_quantity_not_filtered_by_size_or_notional():
    """倉位型跳過數量與最小名目檢查：即使換算後名目 0 也進 desired。"""
    desired, skipped = _build_desired(
        [_pos_tpsl()], Decimal("0.0001"), min_notional=Decimal("10"),
        size_decimals=lambda c: 5, my_positions=_short_btc(), protected=set(),
        mirror_triggers=True,
    )
    assert len(desired) == 1 and skipped == []


# ── 測試 2：方向／無倉位／型態判讀 ───────────────────────────────────
def test_t4_02_reduce_only_trigger_without_position_is_skipped():
    for o in (_pos_tpsl(oid=1), _fixed_sl_limit(oid=2)):
        desired, skipped = _on([o], {})
        assert desired == []
        assert [s.reason for s in skipped] == ["reduce_only_no_pos"]


def test_t4_02_reduce_only_trigger_wrong_direction_is_skipped():
    long_btc = {"BTC": Position(coin="BTC", szi=Decimal("0.5"), entry_px=Decimal("65000"),
                                leverage=5, is_cross=True, unrealized_pnl=Decimal("0"),
                                margin_used=Decimal("100"))}
    # 多倉配買單（ro 買需空倉）→ 略過；多倉配賣單 → 放行
    for o in (_pos_tpsl(oid=1, is_buy=True), _fixed_sl_limit(oid=2, is_buy=True)):
        desired, skipped = _on([o], long_btc)
        assert desired == [] and [s.reason for s in skipped] == ["reduce_only_no_pos"]
    desired, skipped = _on([_pos_tpsl(oid=1, is_buy=False),
                            _fixed_sl_limit(oid=2, is_buy=False)], long_btc)
    assert len(desired) == 2 and skipped == []


def test_t4_02_unreadable_tpsl_is_trigger_unsupported_with_oid():
    bad = replace(_fixed_sl_limit(oid=7), tpsl=None)
    desired, skipped = _on([bad], _short_btc())
    assert desired == []
    assert skipped == [SkippedOrder("BTC", Decimal("0"), "trigger_unsupported", oid=7)]


def test_t4_02_position_tpsl_not_reduce_only_is_unsupported_not_sent():
    """倉位型非 reduce-only：寫入層必 raise；引擎端先略過並記錄（不讓例外冒到 cycle）。"""
    bad = replace(_pos_tpsl(oid=8), reduce_only=False)
    desired, skipped = _on([bad], _short_btc())
    assert desired == []
    assert [(s.reason, s.oid) for s in skipped] == [("trigger_unsupported", 8)]


# ── 測試 3：固定數量止損限價 ─────────────────────────────────────────
def test_t4_03_fixed_size_trigger_scaled_and_not_capped_by_position():
    small_short = {"BTC": Position(coin="BTC", szi=Decimal("-0.1"), entry_px=Decimal("65000"),
                                   leverage=5, is_cross=True, unrealized_pnl=Decimal("0"),
                                   margin_used=Decimal("100"))}
    desired, skipped = _on([_fixed_sl_limit()], small_short)
    assert skipped == []
    assert len(desired) == 1
    d = desired[0]
    expected = _round_size(Decimal("0.015") * SCALE, 5)
    assert d.sz == expected == Decimal("0.40365")
    assert d.sz > Decimal("0.1")  # 倉位 0.1 < 數量：不封頂（D7）
    assert d.limit_px == d.trigger_px == Decimal("61000")
    assert d.is_market is False and d.is_position_tpsl is False and d.tpsl == "sl"
    assert d.reduce_only is True and d.is_trigger is True


def test_t4_03_fixed_size_trigger_does_not_consume_ro_limit_capacity():
    desired, _ = _on([_fixed_sl_limit(oid=2), _limit(oid=9)], _short_btc())
    by = {d.is_trigger: d for d in desired}
    assert by[True].sz == Decimal("0.40365") and by[False].sz == Decimal("0.40365")


# ── 測試 4：_orders_match ────────────────────────────────────────────
def _pos_spec(**kw) -> OrderSpec:
    base = dict(coin="BTC", is_buy=True, sz=Decimal("0"), limit_px=Decimal("67100"),
                reduce_only=True, is_trigger=True, tpsl="sl", trigger_px=Decimal("66000"),
                is_market=True, is_position_tpsl=True)
    base.update(kw)
    return OrderSpec(**base)


def _fixed_spec(**kw) -> OrderSpec:
    base = dict(coin="BTC", is_buy=True, sz=Decimal("0.40365"), limit_px=Decimal("66000"),
                reduce_only=True, is_trigger=True, tpsl="sl", trigger_px=Decimal("66000"),
                is_market=True, is_position_tpsl=False)
    base.update(kw)
    return OrderSpec(**base)


TOLS = dict(px_rel_tol=Decimal("1e-4"), size_tol=Decimal("0.08"))


def test_t4_04_orders_match_position_vs_position_true():
    assert _orders_match(_pos_spec(), _pos_spec(), **TOLS) is True


def test_t4_04_orders_match_position_vs_fixed_false_both_ways():
    assert _orders_match(_pos_spec(), _fixed_spec(), **TOLS) is False
    assert _orders_match(_fixed_spec(), _pos_spec(), **TOLS) is False


def test_t4_04_orders_match_fixed_size_within_tolerance():
    assert _orders_match(_fixed_spec(), _fixed_spec(sz=Decimal("0.41")), **TOLS) is True
    assert _orders_match(_fixed_spec(), _fixed_spec(sz=Decimal("0.5")), **TOLS) is False


def test_t4_04_position_vs_position_still_compares_price_and_tpsl():
    assert _orders_match(_pos_spec(), _pos_spec(trigger_px=Decimal("65000")), **TOLS) is False
    assert _orders_match(_pos_spec(), _pos_spec(tpsl="tp"), **TOLS) is False


# ── 測試 5：兩輪收斂（dry、VirtualBook）─────────────────────────────
def _dry_executor():
    fa = FakeAdapter(sz_decimals={"BTC": 5})
    return ActionExecutor(fa, None, BUILDER, live=False, my_address="0xme",
                          settings=ON, clock=lambda: 1.0), fa


def _dry_round(ex, leader, positions, notifier, state):
    before = len(ex.records)
    rep = sync_open_orders(
        ex, leader, ex.get_open_orders(), positions, SCALE,
        settings=ON, notifier=notifier, state=state, live=False,
    )
    new = ex.records[before:]
    return rep, new


@pytest.mark.parametrize("trigger", ["position", "fixed"])
def test_t4_05_two_rounds_converge_dry_zero_actions_second_round(trigger):
    ex, fa = _dry_executor()
    n = RecordingNotifier()
    state = ReconcileState()
    leader = [_pos_tpsl(oid=1) if trigger == "position" else _fixed_sl_limit(oid=2),
              _limit(oid=9)]
    rep1, new1 = _dry_round(ex, leader, _short_btc(), n, state)
    assert [r.kind for r in new1 if r.kind == "place"] == ["place", "place"]
    assert rep1.reconcile.placed == 2
    trig_places = [r for r in new1 if r.kind == "place" and "trigger_px" in r.payload]
    assert len(trig_places) == 1

    rep2, new2 = _dry_round(ex, leader, _short_btc(), n, state)
    assert [r for r in new2 if r.kind in ("place", "cancel", "modify")] == []
    r2 = rep2.reconcile
    assert (r2.placed, r2.cancelled, r2.modified, r2.matched) == (0, 0, 0, 2)
    assert len(ex.get_open_orders()) == 2
    assert [r for r in n.records if r[0] in ("warn", "critical")] == []
    assert fa.calls["place_order"] == []  # dry：零 adapter 寫入


# ── 測試 6：leader 改觸發價 → 撤單＋重掛，絕不 modify ─────────────────
@pytest.mark.parametrize("kind", ["position", "fixed"])
def test_t4_06_changed_trigger_px_is_cancel_plus_place_never_modify(kind):
    old = _pos_tpsl(oid=1) if kind == "position" else _fixed_sl_limit(oid=2)
    new_leader = replace(old, trigger_px=old.trigger_px - Decimal("500"),
                         limit_px=(old.limit_px if kind == "position"
                                   else old.limit_px - Decimal("500")))
    mine = _mirror(old, oid=50)
    fresh = _mirror(new_leader, oid=51)
    ex = _Ex5(open_orders_seq=[[fresh], [fresh]])
    n = RecordingNotifier()
    state = ReconcileState()
    rep = _sync_on(ex, [new_leader], _short_btc(), state, n, my_orders=[mine])
    assert len(_modifies(ex)) == 0                                   # ex.modify 呼叫數 0
    assert _cancels(ex) == [("cancel", "BTC", 50)]                   # 舊單撤 1 次
    placed = _places(ex)
    assert len(placed) == 1 and placed[0][1].trigger_px == new_leader.trigger_px  # 新單掛 1 次
    assert state.modify_fail_until == {}
    assert rep.reconcile.sync_failed is False and _crits(n) == []


def test_t4_06_trigger_replace_does_not_degrade_limit_order_modify():
    """同幣限價單照常走 modify（觸發單不登記 modify_fail_until，D4）。"""
    old_t = _fixed_sl_limit(oid=2)
    new_t = replace(old_t, trigger_px=Decimal("60000"), limit_px=Decimal("60000"))
    old_l = _limit(oid=9, px="57840")
    new_l = _limit(oid=9, px="57000")
    mine = [_mirror(old_t, 50), _mirror(old_l, 51)]
    ex = _Ex5(open_orders_seq=[[_mirror(new_t, 52), _mirror(new_l, 51)]] * 2)
    n = RecordingNotifier()
    state = ReconcileState()
    _sync_on(ex, [new_t, new_l], _short_btc(), state, n, my_orders=mine)
    assert [m[1] for m in _modifies(ex)] == [51]  # 限價單仍 modify
    assert _cancels(ex) == [("cancel", "BTC", 50)]
    assert state.modify_fail_until == {}


# ── 測試 7：settle 與拒因 ───────────────────────────────────────────
@pytest.mark.parametrize("kind", ["position", "fixed"])
def test_t4_07_settle_sees_new_trigger_no_crit(kind):
    o = _pos_tpsl(oid=1) if kind == "position" else _fixed_sl_limit(oid=2)
    mirror = _mirror(o, 60)
    ex = _Ex5(open_orders_seq=[[mirror], [mirror]])
    n = RecordingNotifier()
    rep = _sync_on(ex, [o], _short_btc(), ReconcileState(), n)
    assert len(_places(ex)) == 1  # 只下一次，settle 沒有重下
    assert rep.reconcile.sync_failed is False and _crits(n) == []


def test_t4_07_rejected_trigger_is_one_loud_crit_with_reason():
    o = _fixed_sl_limit(oid=2)
    reason = "Trigger price too far from market"
    ex = _Ex5(open_orders_seq=[[], []],
                      place_reason_seq=[(False, reason), (False, reason)])
    n = RecordingNotifier()
    rep = _sync_on(ex, [o], _short_btc(), ReconcileState(), n)
    assert rep.reconcile.sync_failed is True
    crits = _crits(n)
    assert len(crits) == 1
    assert reason in crits[0][2]
    assert "BTC B 0.40365@61000" in crits[0][2]


def test_t4_07_rejected_position_tpsl_crit_is_readable():
    o = _pos_tpsl(oid=1)
    ex = _Ex5(open_orders_seq=[[], []],
                      place_reason_seq=[(False, "nope"), (False, "nope")])
    n = RecordingNotifier()
    _sync_on(ex, [o], _short_btc(), ReconcileState(), n)
    text = _crits(n)[0][2]
    assert "倉位TP/SL@66000" in text and "B 0@" not in text and "nope" in text


# ── 測試 8：leader 撤單 → follower 跟著撤 ───────────────────────────
@pytest.mark.parametrize("kind", ["position", "fixed", "entry"])
def test_t4_08_leader_cancels_trigger_follower_cancels(kind):
    o = {"position": _pos_tpsl(oid=1), "fixed": _fixed_sl_limit(oid=2),
         "entry": _entry_stop(oid=3)}[kind]
    ex = _Ex5(open_orders_seq=[[], []])
    n = RecordingNotifier()
    rep = _sync_on(ex, [], _short_btc(), ReconcileState(), n, my_orders=[_mirror(o, 50)])
    assert _cancels(ex) == [("cancel", "BTC", 50)]
    assert _places(ex) == [] and _modifies(ex) == []
    assert rep.reconcile.sync_failed is False and _crits(n) == []


# ── 測試 9：倉位止損 → 固定數量止損（本次事故的實際操作順序）──────────
def test_t4_09_position_sl_replaced_by_fixed_size_sl():
    pos_order = _pos_tpsl(oid=1)
    fixed = _fixed_sl_limit(oid=2)
    mine = _mirror(pos_order, 50)
    new_mine = _mirror(fixed, 52)
    ex = _Ex5(open_orders_seq=[[new_mine], [new_mine]])
    n = RecordingNotifier()
    state = ReconcileState()
    rep = _sync_on(ex, [fixed], _short_btc(), state, n, my_orders=[mine])
    assert _cancels(ex) == [("cancel", "BTC", 50)]
    placed = _places(ex)
    assert len(placed) == 1
    assert placed[0][1].is_position_tpsl is False and placed[0][1].sz == Decimal("0.40365")
    assert _modifies(ex) == [] and state.modify_fail_until == {}
    assert rep.reconcile.sync_failed is False and _crits(n) == []


def test_t4_09_reverse_fixed_replaced_by_position():
    fixed = _fixed_sl_limit(oid=2)
    pos_order = _pos_tpsl(oid=1)
    new_mine = _mirror(pos_order, 52)
    ex = _Ex5(open_orders_seq=[[new_mine], [new_mine]])
    n = RecordingNotifier()
    _sync_on(ex, [pos_order], _short_btc(), ReconcileState(), n, my_orders=[_mirror(fixed, 50)])
    assert _cancels(ex) == [("cancel", "BTC", 50)]
    assert len(_places(ex)) == 1 and _places(ex)[0][1].is_position_tpsl is True
    assert _crits(n) == []


# ── 測試 11：條件進場單 ─────────────────────────────────────────────
class _LevExecutor(_Ex5):
    """記錄 update_leverage 呼叫（與 place 同一條 records 時間線，可驗順序）。"""

    def update_leverage(self, coin, leverage, is_cross) -> bool:
        self.records.append(("lev", coin, leverage, is_cross))
        return True


def test_t4_11_conditional_entry_desired_shape():
    desired, skipped = _on([_entry_stop()], {})  # 無倉位也可掛（D-P13）
    assert skipped == []
    assert len(desired) == 1
    d = desired[0]
    assert d.reduce_only is False and d.is_trigger is True and d.is_position_tpsl is False
    assert d.sz == _round_size(Decimal("0.015") * SCALE, 5) == Decimal("0.40365")
    assert d.trigger_px == Decimal("67000") and d.limit_px == Decimal("68000")
    assert d.is_market is True and d.tpsl == "sl"


def test_t4_11_conditional_entry_sets_leverage_once_before_place():
    o = _entry_stop()
    mirror = _mirror(o, 60)
    ex = _LevExecutor(open_orders_seq=[[mirror], [mirror]])
    n = RecordingNotifier()
    _sync_on(ex, [o], {}, ReconcileState(), n, leverage_by_coin={"BTC": (5, True)})
    kinds = [r[0] for r in ex.records]
    assert kinds.count("lev") == 1 and kinds.count("place") == 1
    assert kinds.index("lev") < kinds.index("place")
    assert [r for r in ex.records if r[0] == "lev"][0] == ("lev", "BTC", 5, True)
    assert _crits(n) == []


def test_t4_11_reduce_only_trigger_does_not_set_leverage():
    o = _fixed_sl_limit(oid=2)
    ex = _LevExecutor(open_orders_seq=[[_mirror(o, 60)], [_mirror(o, 60)]])
    _sync_on(ex, [o], _short_btc(), ReconcileState(), RecordingNotifier(),
             leverage_by_coin={"BTC": (5, True)})
    assert [r for r in ex.records if r[0] == "lev"] == []


# ── 測試 12：條件進場單的熔斷／保護／最小名目；reduce-only 觸發單不受影響 ──
def test_t4_12_entry_blocked_by_cost_breaker_ro_trigger_unaffected():
    desired, skipped = _on([_entry_stop(oid=3), _fixed_sl_limit(oid=2), _pos_tpsl(oid=1)],
                           _short_btc(), no_new_exposure=True)
    assert [s.reason for s in skipped] == ["cost_breaker"]
    assert sorted(d.is_position_tpsl for d in desired) == [False, True]
    assert all(d.reduce_only for d in desired)


def test_t4_12_entry_blocked_by_protected_ro_trigger_unaffected():
    desired, skipped = _build_desired(
        [_entry_stop(oid=3), _fixed_sl_limit(oid=2), _pos_tpsl(oid=1)], SCALE,
        min_notional=Decimal("10"), size_decimals=lambda c: 5, my_positions=_short_btc(),
        protected={"BTC"}, mirror_triggers=True,
    )
    assert [s.reason for s in skipped] == ["protected"]
    assert len(desired) == 2 and all(d.reduce_only for d in desired)


def test_t4_12_entry_small_notional_ro_trigger_exempt():
    tiny_entry = _entry_stop(oid=3, sz="0.001", limit_px="100", trigger_px="100")
    tiny_ro = replace(_fixed_sl_limit(oid=2), sz=Decimal("0.001"),
                      limit_px=Decimal("100"), trigger_px=Decimal("100"))
    desired, skipped = _on([tiny_entry, tiny_ro], _short_btc())
    assert [s.reason for s in skipped] == ["small"]
    assert skipped[0].notional == Decimal("0.02691") * Decimal("100")
    assert len(desired) == 1 and desired[0].reduce_only is True


# ── 測試 13：已掛的條件進場單，no_new_exposure 轉 True → 被撤 ─────────
def test_t4_13_resting_entry_cancelled_when_cost_breaker_trips():
    o = _entry_stop(oid=3)
    ex = _Ex5(open_orders_seq=[[], []])
    n = RecordingNotifier()
    rep = _sync_on(ex, [o], {}, ReconcileState(), n, my_orders=[_mirror(o, 60)],
                   no_new_exposure=True)
    assert _cancels(ex) == [("cancel", "BTC", 60)]
    assert _places(ex) == []
    assert rep.reconcile.sync_failed is False and _crits(n) == []


def test_t4_13_resting_entry_kept_while_breaker_off():
    o = _entry_stop(oid=3)
    mirror = _mirror(o, 60)
    ex = _Ex5(open_orders_seq=[[mirror], [mirror]])
    rep = _sync_on(ex, [o], {}, ReconcileState(), RecordingNotifier(), my_orders=[mirror])
    assert [r for r in ex.records if r[0] in ("place", "cancel", "modify")] == []
    assert rep.reconcile.matched == 1


# ── 測試 14：母單未完成的附帶子單（D-P12）────────────────────────────
def test_t4_14_pending_child_not_mirrored_and_no_warn():
    child = replace(_fixed_sl_limit(oid=2), is_pending_child=True)
    desired, skipped = _on([child], _short_btc())
    assert desired == []
    assert skipped == [SkippedOrder("BTC", Decimal("0"), "trigger_pending_child", oid=2)]
    # 同一張單不是 pending child → 進 desired
    desired2, skipped2 = _on([replace(child, is_pending_child=False)], _short_btc())
    assert len(desired2) == 1 and skipped2 == []


@pytest.mark.parametrize("settings", [ON, SETTINGS])
def test_t4_14_pending_child_no_warn_no_place_via_sync(settings):
    """不論開關：不下單、不發 warn、不記入 trigger_skip_seen。"""
    child = replace(_fixed_sl_limit(oid=2), is_pending_child=True)
    ex = _Ex5()
    n = RecordingNotifier()
    state = ReconcileState()
    sync_open_orders(ex, [child], [], _short_btc(), SCALE, settings=settings, notifier=n,
                     state=state, live=True, clock=lambda: 1000.0, sleep_fn=lambda s: None)
    assert _places(ex) == [] and _warns(n) == [] and _crits(n) == []
    assert state.trigger_skip_seen == {}


# ── 測試 15：D-P11 reduce-only 固定數量觸發單豁免最小名目 ──────────────
def test_t4_15_ro_fixed_trigger_exempt_from_min_notional_entry_is_not():
    # 名目：sz 0.0006 × 26.91 = 0.016146 → 5 位 0.01614；× 297.5 ≈ 4.80
    ro = replace(_fixed_sl_limit(oid=2), sz=Decimal("0.0006"),
                 limit_px=Decimal("297.5"), trigger_px=Decimal("297.5"))
    entry = _entry_stop(oid=3, sz="0.0006", limit_px="297.5", trigger_px="297.5")
    desired, skipped = _on([ro], _short_btc())
    assert len(desired) == 1 and skipped == []
    assert desired[0].sz * desired[0].limit_px < Decimal("10")
    assert Decimal("4.7") < desired[0].sz * desired[0].limit_px < Decimal("4.9")
    desired, skipped = _on([entry], {})
    assert desired == []
    assert [s.reason for s in skipped] == ["small"]


def test_t4_15_ro_fixed_trigger_rounding_to_zero_is_small_not_silent():
    tiny = replace(_fixed_sl_limit(oid=2), sz=Decimal("0.00001"))
    desired, _ = _on([tiny], _short_btc())  # 0.00001 × 26.91 → 0.00026（szDecimals 5）> 0：對照組
    assert len(desired) == 1
    desired, skipped = _build_desired(
        [tiny], Decimal("0.1"), min_notional=Decimal("10"), size_decimals=lambda c: 5,
        my_positions=_short_btc(), protected=set(), mirror_triggers=True)
    assert desired == [] and [s.reason for s in skipped] == ["small"]  # 0.000001 → 0（D11）


# ── 測試 16：同價兩張倉位止損、leader 一張 → 一張 matched、一張撤（D-P5）─
def test_t4_16_duplicate_position_sl_deduped_by_cancel():
    leader = _pos_tpsl(oid=1)
    a, b = _mirror(leader, 70), _mirror(leader, 71)
    ex = _Ex5(open_orders_seq=[[a], [a]])
    n = RecordingNotifier()
    rep = _sync_on(ex, [leader], _short_btc(), ReconcileState(), n, my_orders=[a, b])
    assert rep.reconcile.matched == 1
    assert _cancels(ex) == [("cancel", "BTC", 71)]
    assert _places(ex) == [] and _modifies(ex) == []
    assert rep.reconcile.sync_failed is False and _crits(n) == []


# ── 開關關閉：條件進場單同樣略過（不論熔斷／保護）──────────────────────
def test_t4_switch_off_entry_trigger_still_skipped():
    desired, skipped = _build([_entry_stop(oid=3)], {})
    assert desired == [] and [s.reason for s in skipped] == ["trigger_disabled"]


# ── 2026-11-22 兩輪審查：送出成功才記入、每幣最短嘗試間隔、同幣合併成一則 ──────────
class _ScriptedNotifier(RecordingNotifier):
    """warn 的回傳值依序取自 results（用完後恆 True）；每次呼叫都照樣記錄。"""

    def __init__(self, results=()):
        super().__init__()
        self._results = list(results)

    def warn(self, category, text, dedup_key=None):
        super().warn(category, text, dedup_key)
        return self._results.pop(0) if self._results else True


def test_warn_send_failure_is_retried_after_interval_not_swallowed():
    """送出失敗不得記成「已告警」；重試受每幣最短間隔約束（不是每輪都打）。"""
    n = _ScriptedNotifier([False])
    ex, state, pos = FakeExecutor(), ReconcileState(), _short_btc()

    _sync(ex, [_fixed_sl_limit(oid=7)], pos, state, n, now=1000.0)  # 嘗試一次、失敗
    assert len(_warns(n)) == 1
    assert state.trigger_skip_seen == {}  # 沒送出去＝沒告警過

    _sync(ex, [_fixed_sl_limit(oid=7)], pos, state, n, now=1060.0)  # 間隔內：不重打
    assert len(_warns(n)) == 1
    assert state.trigger_skip_seen == {}

    _sync(ex, [_fixed_sl_limit(oid=7)], pos, state, n, now=1300.0)  # 間隔過：重試並成功
    assert len(_warns(n)) == 2
    assert state.trigger_skip_seen == {"BTC": frozenset({7})}

    _sync(ex, [_fixed_sl_limit(oid=7)], pos, state, n, now=1700.0)  # 已送達 → 不再送
    assert len(_warns(n)) == 2


def test_warn_attempts_bounded_when_notifier_never_succeeds():
    """notifier 一直送不出去（Telegram 掛掉／分類靜音／停用）：10 輪只嘗試 2 次，
    不會每輪每幣都卡一次同步發送；狀態始終不記成已告警。"""
    n = _ScriptedNotifier([False] * 50)
    ex, state, pos = FakeExecutor(), ReconcileState(), _short_btc()
    for i in range(10):  # t = 0, 60, …, 540 秒
        _sync(ex, [_fixed_sl_limit(oid=7)], pos, state, n, now=1000.0 + 60 * i)
    assert len(_warns(n)) == 2  # t=0 與 t=300
    assert state.trigger_skip_seen == {}


def test_warn_not_recorded_with_null_notifier():
    from spark.copytrade.notifier import NullNotifier

    state = ReconcileState()
    _sync(FakeExecutor(), [_fixed_sl_limit(oid=7)], _short_btc(), state, NullNotifier())
    assert state.trigger_skip_seen == {}
    assert state.trigger_warn_attempt_at == {"BTC": 1000.0}


def test_warn_rate_capped_when_leader_changes_trigger_every_cycle():
    """leader 每輪改一次觸發單（oid 每輪都變）：每幣每個間隔至多一則；
    間隔過後送出的是當下最新的集合；被擋下的那幾輪不得記成已告警。"""
    n = RecordingNotifier()
    ex, state, pos = FakeExecutor(), ReconcileState(), _short_btc()

    _sync(ex, [_fixed_sl_limit(oid=1)], pos, state, n, now=1000.0)
    assert len(_warns(n)) == 1 and state.trigger_skip_seen == {"BTC": frozenset({1})}

    for i, oid in enumerate((2, 3, 4), start=1):  # 間隔內每 60 秒換一次 oid
        _sync(ex, [_fixed_sl_limit(oid=oid)], pos, state, n, now=1000.0 + 60 * i)
    assert len(_warns(n)) == 1
    assert state.trigger_skip_seen == {"BTC": frozenset({1})}

    _sync(ex, [_fixed_sl_limit(oid=5), _pos_tpsl(oid=6)], pos, state, n, now=1300.0)
    assert len(_warns(n)) == 2
    assert "有 2 張" in _warns(n)[-1][2]  # 送出的是當下最新的集合
    assert state.trigger_skip_seen == {"BTC": frozenset({5, 6})}


def test_warn_interval_not_bypassed_by_disappear_and_reappear():
    n = RecordingNotifier()
    ex, state, pos = FakeExecutor(), ReconcileState(), _short_btc()
    _sync(ex, [_fixed_sl_limit(oid=1)], pos, state, n, now=1000.0)
    _sync(ex, [_limit(oid=9)], pos, state, n, now=1060.0)          # 消失
    assert state.trigger_skip_seen == {}
    assert state.trigger_warn_attempt_at == {"BTC": 1000.0}        # 間隔未過：時間戳保留
    _sync(ex, [_fixed_sl_limit(oid=2)], pos, state, n, now=1120.0)  # 立刻又出現
    assert len(_warns(n)) == 1                                      # 仍在間隔內，不發
    _sync(ex, [_fixed_sl_limit(oid=2)], pos, state, n, now=1300.0)
    assert len(_warns(n)) == 2


def test_warn_attempt_timestamp_pruned_after_interval_once_triggers_gone():
    n = RecordingNotifier()
    ex, state, pos = FakeExecutor(), ReconcileState(), _short_btc()
    _sync(ex, [_fixed_sl_limit(oid=1)], pos, state, n, now=1000.0)
    _sync(ex, [_limit(oid=9)], pos, state, n, now=1300.0)  # 已無觸發單且間隔已過 → 清掉
    assert state.trigger_warn_attempt_at == {}
    assert state.trigger_skip_seen == {}


def test_warn_is_one_message_per_coin_merging_disabled_and_unsupported():
    """同一幣的 disabled 與 unsupported 合成一則：分成兩則時一成一敗會永遠輪流重送。"""
    from spark.copytrade.orders import _warn_unmirrored_triggers

    zero = Decimal("0")
    skipped = [
        SkippedOrder("BTC", zero, "trigger_disabled", oid=1),
        SkippedOrder("BTC", zero, "trigger_disabled", oid=2),
        SkippedOrder("BTC", zero, "trigger_unsupported", oid=3),
        SkippedOrder("ETH", zero, "trigger_unsupported", oid=4),
        SkippedOrder("SOL", zero, "small"),  # 非觸發單 reason：不得算進來
    ]
    n = _ScriptedNotifier([False, True])  # BTC 那則失敗、ETH 那則成功
    state = ReconcileState()
    _warn_unmirrored_triggers(skipped, state, n, now=1000.0)
    warns = _warns(n)
    assert len(warns) == 2  # 每幣恰一則
    assert "BTC" in warns[0][2] and "有 2 張" in warns[0][2]
    assert "另有 1 張型態無法判讀" in warns[0][2]
    assert "ETH" in warns[1][2] and "有 1 張型態無法判讀" in warns[1][2]
    assert all(w[3] is None for w in warns)  # 不借用 notifier 的 dedup_key
    assert state.trigger_skip_seen == {"ETH": frozenset({4})}  # BTC 失敗 → 不記

    _warn_unmirrored_triggers(skipped, state, n, now=1300.0)  # 間隔過：只重送 BTC 一則
    assert len(_warns(n)) == 3
    assert state.trigger_skip_seen == {"BTC": frozenset({1, 2, 3}), "ETH": frozenset({4})}

    _warn_unmirrored_triggers(skipped, state, n, now=1700.0)  # 收斂：不再送
    assert len(_warns(n)) == 3


# ── 2026-11-22 T6 審核建議：觸發價／限價為 0 的引擎端防線要有測試守住 ──────────────
# adapter 把缺鍵的 triggerPx 補成 "0"（hyperliquid.py get_open_orders），所以真實資料下
# `trigger_px is None` 不會成立，`<= 0` 才是唯一擋得住的地方；拿掉它，這張單會以觸發價 0
# 送出、每輪被拒、每輪 CRIT。
def test_t6_trigger_with_zero_trigger_px_or_zero_limit_px_is_unsupported():
    import dataclasses

    pos = _short_btc()
    for bad in (
        dataclasses.replace(_fixed_sl_limit(oid=11), trigger_px=Decimal("0")),
        dataclasses.replace(_fixed_sl_limit(oid=12), limit_px=Decimal("0")),
        dataclasses.replace(_pos_tpsl(oid=13), trigger_px=Decimal("0")),
        dataclasses.replace(_pos_tpsl(oid=14), limit_px=Decimal("0")),
    ):
        desired, skipped = _build([bad], pos, mirror_triggers=True)
        assert desired == []
        assert [(s.reason, s.oid) for s in skipped] == [("trigger_unsupported", bad.oid)]
    # 對照：同一張單價格正常時會進 desired（確認上面不是被別的過濾擋掉）
    desired, skipped = _build([_fixed_sl_limit(oid=15)], pos, mirror_triggers=True)
    assert len(desired) == 1 and skipped == []
