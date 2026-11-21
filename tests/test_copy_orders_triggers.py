"""leader 觸發單（止盈／止損／條件進場）在開關關閉時的行為（plan 2026-11-21 T2）。

預設 `mirror_triggers=False`：所有觸發單在 `_build_desired` 就略過（`trigger_disabled`），
不進 desired、不下單、不發 CRIT，只發一則以 oid 集合去重的 warn。
"""
from decimal import Decimal

from spark.copytrade.config import CopySettings
from spark.copytrade.notifier import RecordingNotifier
from spark.copytrade.orders import (
    ReconcileState,
    SkippedOrder,
    _build_desired,
    sync_open_orders,
)
from spark.exchange.base import OpenOrder, Position

from tests.test_copy_orders_crit_alert import FakeExecutor

SETTINGS = CopySettings(px_rel_tol=Decimal("1e-4"), size_tolerance=Decimal("0.08"))


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
    """開關開啟時不走 trigger_disabled（T4 才實作鏡射；T2 範圍內僅驗證不被該分支攔截）。"""
    _desired, skipped = _build([_pos_tpsl(oid=1)], _short_btc(), mirror_triggers=True)
    assert all(s.reason != "trigger_disabled" for s in skipped)


# ── 案例 5：COPY_MIRROR_TRIGGERS ────────────────────────────────────
def test_mirror_triggers_defaults_false_and_env_true_enables():
    assert CopySettings().mirror_triggers is False
    assert CopySettings.from_env({}).mirror_triggers is False
    assert CopySettings.from_env({"COPY_MIRROR_TRIGGERS": "true"}).mirror_triggers is True
    assert CopySettings.from_env({"COPY_MIRROR_TRIGGERS": "false"}).mirror_triggers is False


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
