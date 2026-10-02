"""settle 重試不得重下已成交的鏡射單（2026-09-30 PUMP／ENA 重複成交事故）。

D1 可行動 missing 謂詞（2026-10-01 起只剩「本輪 place 失敗」，同 slot extra 條款已刪）才補下／CRIT；
其餘 missing 視為已消耗（成交或外部撤銷），不補、不通知。
"""
from decimal import Decimal

from spark.copytrade.config import CopySettings
from spark.copytrade.notifier import RecordingNotifier
from spark.copytrade.orders import OrderSpec, ReconcileState, _reconcile_orders, _verify_diff
from spark.exchange.base import OpenOrder

from tests.test_copy_orders_crit_alert import FakeExecutor

SETTINGS = CopySettings(px_rel_tol=Decimal("1e-4"), size_tolerance=Decimal("0.08"))


def _spec(coin="PUMP", is_buy=False, sz="168927", px="0.00576", ro=False,
          **kw) -> OrderSpec:
    return OrderSpec(coin=coin, is_buy=is_buy, sz=Decimal(sz),
                     limit_px=Decimal(px), reduce_only=ro, **kw)


def _open(spec: OrderSpec, oid=1, sz=None, orig="spec") -> OpenOrder:
    """orig="spec"（預設）＝原量取 spec.sz；傳 None＝orig_sz 缺失；傳字串＝指定原量。"""
    orig_sz = spec.sz if orig == "spec" else (None if orig is None else Decimal(orig))
    return OpenOrder(oid=oid, coin=spec.coin, is_buy=spec.is_buy,
                     limit_px=spec.limit_px,
                     sz=spec.sz if sz is None else Decimal(sz),
                     reduce_only=spec.reduce_only, is_trigger=False,
                     trigger_px=None, tpsl=None, orig_sz=orig_sz)


def _run(desired, my_orders, open_seq, place_seq=None):
    ex = FakeExecutor(open_orders_seq=open_seq, place_reason_seq=place_seq)
    n = RecordingNotifier()
    res = _reconcile_orders(
        ex, desired, my_orders, settings=SETTINGS, notifier=n,
        state=ReconcileState(), live=True, clock=lambda: 1000.0,
        sleep_fn=lambda s: None, my_positions={})
    return ex, n, res


def _places(ex):
    return [r for r in ex.records if r[0] == "place"]


def _crits(n):
    return [r for r in n.records if r[0] == "critical"]


def test_pump_filled_after_place_is_not_replaced():
    ex, n, res = _run([_spec()], [], [[], []])
    assert len(_places(ex)) == 1
    assert _crits(n) == []
    assert res.sync_failed is False


def test_ena_buy_filled_after_place_is_not_replaced():
    d = _spec(coin="ENA", is_buy=True, sz="2319", px="0.2652")
    ex, n, res = _run([d], [], [[], []])
    assert len(_places(ex)) == 1
    assert _crits(n) == []
    assert res.sync_failed is False


def test_matched_order_vanishing_is_not_replaced():
    d = _spec()
    ex, n, res = _run([d], [_open(d)], [[], []])
    assert _places(ex) == []
    assert _crits(n) == []
    assert res.sync_failed is False


def test_failed_place_is_retried_once_and_crit_on_second_failure():
    d = _spec()
    ex, n, res = _run([d], [], [[], []],
                      place_seq=[(False, "boom"), (False, "still boom")])
    assert len(_places(ex)) == 2
    assert res.sync_failed is True
    assert "still boom" in _crits(n)[0][2]


def test_failed_place_retry_success_then_consumed_no_crit():
    d = _spec()
    ex, n, res = _run([d], [], [[], []], place_seq=[(False, "boom"), (True, "")])
    assert len(_places(ex)) == 2
    assert _crits(n) == []
    assert res.sync_failed is False


def test_trigger_rejection_stays_loud():
    d = _spec(is_trigger=True, tpsl="sl", trigger_px=Decimal("0.006"))
    ex, n, res = _run([d], [], [[], []],
                      place_seq=[(False, "trigger skip"), (False, "trigger skip")])
    assert res.sync_failed is True
    assert len(_crits(n)) == 1


def test_extra_only_still_cancelled_and_crit():
    stray = _open(_spec(coin="ETH", px="2000", sz="1"), oid=7)
    ex, n, res = _run([], [], [[stray], [stray]])
    assert ("cancel", "ETH", 7) in ex.records
    assert res.sync_failed is True
    assert len(_crits(n)) == 1


# ── R2／R3／R4（審查補強）────────────────────────────────────────────
def _run_counted(desired, open_seq, place_seq=None):
    ex = FakeExecutor(open_orders_seq=open_seq, place_reason_seq=place_seq)
    gets: list = []
    orig_get = ex.get_open_orders

    def _get():
        gets.append(1)
        return orig_get()

    ex.get_open_orders = _get
    n = RecordingNotifier()
    sleeps: list = []
    res = _reconcile_orders(
        ex, desired, [], settings=SETTINGS, notifier=n, state=ReconcileState(),
        live=True, clock=lambda: 1000.0, sleep_fn=sleeps.append, my_positions={})
    return ex, n, res, gets, sleeps


def test_pump_ena_no_second_round_when_nothing_actionable():
    ex, n, res, gets, sleeps = _run_counted([_spec()], [[], []])
    assert len(gets) == 1 and len(sleeps) == 1
    ex, n, res, gets, sleeps = _run_counted(
        [_spec(coin="ENA", is_buy=True, sz="2319", px="0.2652")], [[], []])
    assert len(gets) == 1 and len(sleeps) == 1


def test_non_ro_partial_fill_not_replaced_residual_kept():
    d = _spec(sz="1000")
    residual = _open(_spec(sz="1000"), oid=5, sz="400")
    ex, n, res, _g, _s = _run_counted([d], [[residual], []])
    assert len(_places(ex)) == 1
    assert ("cancel", "PUMP", 5) not in ex.records  # 2026-10-01 C2：殘單保留不撤
    assert _crits(n) == []
    assert res.sync_failed is False


def test_same_slot_two_non_ro_one_full_one_partial_zero_replace():
    a = _spec(px="0.00576", sz="1000")
    b = _spec(px="0.00570", sz="1000")
    residual_b = _open(_spec(px="0.00570", sz="1000"), oid=7, sz="400")
    ex, n, res, _g, _s = _run_counted([a, b], [[residual_b], []])
    assert len(_places(ex)) == 2  # 只有步驟 3 的兩次初掛
    assert _crits(n) == []
    assert res.sync_failed is False


def test_duplicate_specs_fail_then_ok_retries_once():
    a = _spec()
    ex, n, res, _g, _s = _run_counted(
        [a, a], [[], []], place_seq=[(False, "margin"), (True, "")])
    assert len(_places(ex)) == 3  # 初掛 2 ＋ 重試 1
    assert _crits(n) == []
    assert res.sync_failed is False


def test_duplicate_specs_fail_then_ok_retry_fails_crits_with_reason():
    a = _spec()
    ex, n, res, _g, _s = _run_counted(
        [a, a], [[], []],
        place_seq=[(False, "margin"), (True, ""), (False, "boom")])
    assert len(_places(ex)) == 3
    assert res.sync_failed is True
    assert "boom" in _crits(n)[0][2]


def test_duplicate_specs_ok_then_fail_retries_once():
    a = _spec()
    ex, n, res, _g, _s = _run_counted(
        [a, a], [[], []], place_seq=[(True, ""), (False, "margin")])
    assert len(_places(ex)) == 3  # 只重試失敗的那 1 張
    assert _crits(n) == []


# ── F2（2026-10-01）：_verify_diff 1:1 配對 ───────────────────────────
def test_identical_specs_one_ok_one_fail_retries_and_crits_loudly():
    a = _spec()
    ex, n, res, _g, _s = _run_counted(
        [a, a], [[_open(a)], [_open(a)]],
        place_seq=[(True, ""), (False, "margin"), (False, "boom")])
    assert len(_places(ex)) == 3  # 初掛 2 ＋ 補下 1
    assert res.sync_failed is True
    assert "boom" in _crits(n)[0][2]


def test_verify_diff_is_one_to_one():
    a = _spec()
    kw = dict(px_rel_tol=Decimal("1e-4"), size_tol=Decimal("0.08"))
    missing, extra = _verify_diff([a, a], [_open(a)], **kw)
    assert len(missing) == 1 and extra == []
    missing, extra = _verify_diff([a], [_open(a, oid=1), _open(a, oid=2)], **kw)
    assert missing == [] and len(extra) == 1


# ── F1／F3（2026-10-01）：部分成交殘單配對（C2／C3／C4）─────────────────
def _cancels(ex):
    return [r for r in ex.records if r[0] == "cancel"]


def test_ro_partial_fill_residual_not_replaced_not_cancelled():
    """T-F1：ro 賣 1000 成交 600，殘單 400 → 不重下（否則實際減 1600）、不撤、不 CRIT。"""
    d = _spec(sz="1000", ro=True)
    residual = _open(_spec(sz="1000", ro=True), oid=3, sz="400")
    ex, n, res, gets, _s = _run_counted([d], [[residual], [residual]])
    assert len(_places(ex)) == 1
    assert _cancels(ex) == []
    assert _crits(n) == []
    assert res.sync_failed is False
    assert len(gets) == 1


def test_residual_in_second_round_not_cancelled_nor_listed_in_crit():
    """T-F3：第二次重抓時已掛的非 ro 單變殘單 → 不撤、CRIT 只含另一幣。"""
    pump = _spec(sz="1000")
    eth = _spec(coin="ETH", px="2000", sz="1")
    residual = _open(_spec(sz="1000"), oid=1, sz="300")
    # 不同 slot 的 to_place 順序取決於 set 迭代（hash seed）→ 以幣別決定成敗，保持確定性
    ex = FakeExecutor(open_orders_seq=[[_open(pump)], [residual]])
    orig = ex.place_with_reason
    ex.place_with_reason = (
        lambda spec: (orig(spec)[0] and False, "boom") if spec.coin == "ETH"
        else orig(spec))
    n = RecordingNotifier()
    res = _reconcile_orders(
        ex, [pump, eth], [], settings=SETTINGS, notifier=n, state=ReconcileState(),
        live=True, clock=lambda: 1000.0, sleep_fn=lambda s: None, my_positions={})
    assert _cancels(ex) == []
    assert res.sync_failed is True
    text = _crits(n)[0][2]
    assert "ETH" in text and "boom" in text
    assert "多餘 0" in text
    assert "oid=1" not in text


def test_first_round_non_ro_residual_kept_no_second_round():
    """T-F3b：第一輪就是殘單、無任何失敗 → 0 撤、0 重下、不跑第二輪、無 CRIT。"""
    d = _spec(sz="1000")
    residual = _open(_spec(sz="1000"), oid=1, sz="300")
    ex, n, res, gets, sleeps = _run_counted([d], [[residual], []])
    assert len(_places(ex)) == 1
    assert _cancels(ex) == []
    assert len(gets) == 1 and len(sleeps) == 1
    assert _crits(n) == []
    assert res.sync_failed is False


def test_trim_shape_0728_now_kept_silently():
    """T-trim-new：7/28 形狀（ro 1000、簿上同價 ro 600）→ 保留、無 CRIT、無撤單。"""
    d = _spec(sz="1000", ro=True)
    trimmed = _open(_spec(sz="1000", ro=True), oid=4, sz="600")
    ex, n, res, _g, _s = _run_counted([d], [[trimmed], [trimmed]])
    assert len(_places(ex)) == 1
    assert _cancels(ex) == []
    assert _crits(n) == []
    assert res.sync_failed is False


def test_failed_spec_replaced_even_if_smaller_same_price_old_order_exists():
    """T-priority：place 失敗的 spec 優先補下；同價較小舊單照 extra 撤掉。"""
    d = _spec(sz="1000")
    # 原量等於 d.sz 的同價殘單形狀（C12）：若先配殘單再算可行動，d 會被吞掉而不補下
    old = _open(_spec(sz="1000"), oid=9, sz="400")
    ex, n, res, _g, _s = _run_counted(
        [d], [[old], [_open(d, oid=10)]], place_seq=[(False, "x"), (True, "")])
    assert len(_places(ex)) == 2
    assert ("cancel", "PUMP", 9) in ex.records
    assert _crits(n) == []


def test_smaller_order_at_different_price_is_extra_not_residual():
    """T-price-diff：同 slot 但價格不同的較小單 → 不是殘單，撤並入 CRIT。"""
    d = _spec(sz="1000", px="0.00576")
    other = _open(_spec(sz="400", px="0.00500"), oid=6)
    ex, n, res, _g, _s = _run_counted([d], [[other], [other]])
    assert ("cancel", "PUMP", 6) in ex.records
    assert res.sync_failed is True
    assert "oid=6" in _crits(n)[0][2]


# ── C5／C6（審查後）：殘單必須核對原始下單量 orig_sz ────────────────────
class _NoopModify(FakeExecutor):
    """modify 回 True 但簿上沒變（交易所「謊報成功」）。"""

    def modify(self, oid, spec):
        self.records.append(("modify", oid, spec))
        return True


class _CancelFalse(FakeExecutor):
    """cancel 回 False 且單仍在簿上。"""

    def cancel(self, coin, oid):
        self.records.append(("cancel", coin, oid))
        return False


def _run_with(cls, desired, mine, open_seq, settings=SETTINGS):
    ex = cls(open_orders_seq=open_seq)
    n = RecordingNotifier()
    res = _reconcile_orders(
        ex, desired, mine, settings=settings, notifier=n, state=ReconcileState(),
        live=True, clock=lambda: 1000.0, sleep_fn=lambda s: None, my_positions={})
    return ex, n, res


def test_larger_same_price_extra_is_not_residual():
    """T-larger：同價較大 1500 vs desired 1000 → 非殘單，撤＋第二輪仍在則 CRIT。"""
    d = _spec(sz="1000")
    big = _open(_spec(sz="1500"), oid=7)
    ex, n, res, _g, _s = _run_counted([d], [[big], [big]])
    assert ("cancel", "PUMP", 7) in ex.records
    assert res.sync_failed is True and len(_crits(n)) == 1


def test_extra_larger_than_desired_even_with_matching_orig_is_not_residual():
    """T-larger（釘 `E.sz < d.sz` 子句）：orig_sz 與 desired 相符、但剩餘量 1500 > desired 1000
    （物理上不該發生）→ 不算殘單，撤＋CRIT。拿掉該子句此測試會紅。"""
    d = _spec(sz="1000")
    odd = _open(_spec(sz="1000"), oid=7, sz="1500")
    ex, n, res, _g, _s = _run_counted([d], [[odd], [odd]])
    assert ("cancel", "PUMP", 7) in ex.records
    assert res.sync_failed is True and len(_crits(n)) == 1


def test_modify_lies_success_size_unchanged_is_loud_not_residual():
    """T-Q1：leader 同價加量 600→1000，modify 回 True 但簿上仍 600（orig 600，modify 謊報成功）
    → orig 不符、不是殘單，仍 CRIT。前提是交易所謊報；真實 modify 生效時簿上會是 1000。"""
    d = _spec(sz="1000")
    old = _open(_spec(sz="600"), oid=1)
    ex, n, res = _run_with(_NoopModify, [d], [old], [[old], [old]])
    assert res.sync_failed is True
    assert len(_crits(n)) == 1


def test_cancel_returns_false_old_order_survives_is_loud_not_residual():
    """T-Q2：cancel-place 撤舊 600 回 False 且仍在簿上（撤單失敗／謊報）、新 1000 全成交
    → 舊 600（orig 600）orig 不符、非殘單，CRIT。前提是撤單確實沒生效。"""
    cp = CopySettings(px_rel_tol=Decimal("1e-4"), size_tolerance=Decimal("0.08"),
                      modify_policy="cancel-place")
    d = _spec(sz="1000")
    old = _open(_spec(sz="600"), oid=1)
    ex, n, res = _run_with(_CancelFalse, [d], [old], [[old], [old]], settings=cp)
    assert res.sync_failed is True
    assert len(_crits(n)) == 1


def _q3(order):
    d1 = _spec(sz="1000")
    d2 = _spec(sz="500")
    r1 = _open(d1, oid=1, sz="900")  # 1000 成交 100
    r2 = _open(d2, oid=2, sz="100")  # 500 成交 400
    book = {"a": [r2, r1], "b": [r1, r2]}[order]
    ex, n, res, _g, _s = _run_counted([d1, d2], [book, book])
    return ex, n, res


def test_two_same_price_partials_book_order_100_900_nothing_cancelled():
    """T-Q3：同價兩張 1000/500 都部分成交，簿順序 [100,900] → 0 撤單、無 CRIT。"""
    ex, n, res = _q3("a")
    assert _cancels(ex) == []
    assert _crits(n) == [] and res.sync_failed is False
    assert len(_places(ex)) == 2


def test_two_same_price_partials_book_order_900_100_nothing_cancelled():
    """T-Q3：簿順序 [900,100]。"""
    ex, n, res = _q3("b")
    assert _cancels(ex) == []
    assert _crits(n) == [] and res.sync_failed is False
    assert len(_places(ex)) == 2


def test_residual_without_orig_sz_is_not_residual_cancelled_and_crit():
    """T-orig-none：同價較小但 orig_sz=None → 保守退路：撤單（FakeExecutor 撤單回 True 但簿上仍在，
    即撤單謊報，故第二輪仍見到而 CRIT；實盤撤單生效則不會 CRIT）。"""
    d = _spec(sz="1000")
    nl = _open(_spec(sz="1000"), oid=8, sz="400", orig=None)
    ex, n, res, _g, _s = _run_counted([d], [[nl], [nl]])
    assert ("cancel", "PUMP", 8) in ex.records
    assert res.sync_failed is True and len(_crits(n)) == 1


def test_mismatched_order_cancel_effective_not_replaced_no_crit():
    """C10：撤單真的生效版本——簿上不符的舊單（600，orig 600）被撤、desired 不補（非可行動）、
    無 CRIT。接受的行為：下一輪（約 60 秒）由 `_plan` 重算補回。"""
    cp = CopySettings(px_rel_tol=Decimal("1e-4"), size_tolerance=Decimal("0.08"),
                      modify_policy="cancel-place")
    d = _spec(sz="1000")
    old = _open(_spec(sz="600"), oid=1)
    ex, n, res = _run_with(FakeExecutor, [d], [old], [[old], []], settings=cp)
    assert ("cancel", "PUMP", 1) in ex.records
    assert len(_places(ex)) == 1  # 只有步驟 3 的初掛，不補
    assert _crits(n) == [] and res.sync_failed is False


# ── C8：本輪動過的 oid 不得當殘單 ──────────────────────────────────────
def test_stale_residual_touched_by_failed_cancel_is_not_swallowed():
    """C8：上一輪殘單（oid=1，剩 600、orig 1000），本輪 cancel-place 撤它回 False 仍在，
    新 1000 全成交。orig 與 desired 相符，但該 oid 本輪被動過 → 不得當殘單吞掉，CRIT。"""
    cp = CopySettings(px_rel_tol=Decimal("1e-4"), size_tolerance=Decimal("0.08"),
                      modify_policy="cancel-place")
    d = _spec(sz="1000")
    stale = _open(_spec(sz="1000"), oid=1, sz="600")
    ex, n, res = _run_with(_CancelFalse, [d], [stale], [[stale], [stale]], settings=cp)
    assert res.sync_failed is True and len(_crits(n)) == 1


# ── C9：_verify_diff 兩段配對（先 sz 完全相等、再容忍度）─────────────────
def test_verify_diff_exact_size_pairs_before_tolerance():
    a = _spec(sz="1000")
    b = _spec(sz="1050")  # 與 a 在 size_tol 內
    kw = dict(px_rel_tol=Decimal("1e-4"), size_tol=Decimal("0.08"))
    missing, extra = _verify_diff([b, a], [_open(a, oid=5)], **kw)
    assert missing == [b] and extra == []


def test_near_equal_specs_b_before_a_b_fails_is_retried():
    """C9（W2）：desired 順序 [b(1050), a(1000)]；a 掛成功、b 失敗 → b 必須被重試／CRIT。"""
    a, b = _spec(sz="1000"), _spec(sz="1050")
    ex = FakeExecutor(open_orders_seq=[[_open(a, oid=5)], [_open(a, oid=5)]])
    ex.place_with_reason = lambda s: (ex.records.append(("place", s)) or
                                      ((False, "margin") if s.sz == Decimal("1050")
                                       else (True, "")))
    n = RecordingNotifier()
    _reconcile_orders(
        ex, [b, a], [], settings=SETTINGS, notifier=n, state=ReconcileState(),
        live=True, clock=lambda: 1000.0, sleep_fn=lambda s: None, my_positions={})
    # 初掛 2 次（b 失敗）＋ 重試 b；b 持續失敗 → CRIT 不得靜默
    assert len([r for r in _places(ex) if r[1].sz == Decimal("1050")]) >= 2
    assert len(_crits(n)) == 1


# ── C11：failed 預算以「同 slot 同價」群組計（近似量單配對不唯一）──────────
def _fail_by_size(ex, sizes):
    """sizes 內的 sz 一律失敗（次數不限），其餘成功；確定性，不依賴呼叫順序。"""
    orig = ex.place_with_reason

    def _pw(spec):
        if spec.sz in sizes:
            ex.records.append(("place", spec))
            return False, "margin"
        return orig(spec)

    ex.place_with_reason = _pw


def _run_ex(ex, desired, mine):
    n = RecordingNotifier()
    res = _reconcile_orders(
        ex, desired, mine, settings=SETTINGS, notifier=n, state=ReconcileState(),
        live=True, clock=lambda: 1000.0, sleep_fn=lambda s: None, my_positions={})
    return n, res


def test_failed_budget_group_a_filled_down_b_fails_retried_and_crit():
    """C11(a)：desired [b=1050, a=1000]；a 成功後部分成交到 990、b 失敗。
    簿上 990 與 b 在容忍度內被配走，missing 變成 a——預算不能綁單一 spec 值。"""
    a, b = _spec(sz="1000"), _spec(sz="1050")
    book = [_open(a, oid=5, sz="990")]
    # 初掛 b 失敗、a 成功；重試補下的是 missing 那張（a），仍敗 → CRIT
    ex = FakeExecutor(open_orders_seq=[book, book],
                      place_reason_seq=[(False, "margin"), (True, ""), (False, "boom")])
    n, res = _run_ex(ex, [b, a], [])
    assert len(_places(ex)) == 3  # 初掛 2 ＋ 重試 1
    assert res.sync_failed is True and len(_crits(n)) == 1
    assert "boom" in _crits(n)[0][2]


def test_failed_budget_group_book_has_a_replace_a_fails_retried_and_crit():
    """C11(b)：簿上已有 A=1000、desired [b=1050, a=1000]；補下失敗 → 重試 1 次、仍敗則 CRIT。"""
    a, b = _spec(sz="1000"), _spec(sz="1050")
    book = [_open(a, oid=5)]
    ex = FakeExecutor(open_orders_seq=[book, book])
    _fail_by_size(ex, {Decimal("1050"), Decimal("1000")})
    n, res = _run_ex(ex, [b, a], [book[0]])
    assert len(_places(ex)) >= 2
    assert res.sync_failed is True and len(_crits(n)) == 1


def test_size_gap_same_price_filled_spec_never_replaced_only_failed_retried():
    """T-size-gap（C13）：同價 a=1000、b=50（量差遠超 size_tol）；b 失敗、a 成功後全額成交。
    只重試 50、絕不重下已成交的 1000；重試仍敗 → CRIT 含拒因。"""
    a, b = _spec(sz="1000"), _spec(sz="50")
    ex = FakeExecutor(open_orders_seq=[[], []],
                      place_reason_seq=[(True, ""), (False, "m1"), (False, "boom")])
    n, res = _run_ex(ex, [a, b], [])
    assert [r[1].sz for r in _places(ex)] == [Decimal("1000"), Decimal("50"), Decimal("50")]
    assert res.sync_failed is True and len(_crits(n)) == 1
    assert "boom" in _crits(n)[0][2]


def test_actionable_missing_second_stage_requires_size_within_tol():
    """C13 單元：第二段只在同 `_failed_key` 且量差在 size_tol 內才配對；
    量差大（1000 vs 50）絕不放行（否則重下已成交的 1000）。"""
    from collections import Counter

    from spark.copytrade.orders import _actionable_missing

    a, b, near = _spec(sz="1000"), _spec(sz="50"), _spec(sz="1050")
    tol = Decimal("0.08")
    assert _actionable_missing([a], Counter({b: 1}), size_tol=tol) == []
    assert _actionable_missing([a], Counter({near: 1}), size_tol=tol) == [(a, near)]
    # 第一段（值相等）優先於第二段
    assert _actionable_missing([a, near], Counter({near: 1}), size_tol=tol) == [(near, near)]


def test_touched_oid_never_satisfies_desired_filled_spec_not_replaced():
    """C14（Z 情境）：撤單回 False 仍在簿上的舊單 Z（oid=9），settle 中部分成交到恰等於失敗的 b；
    Z 不得滿足 b（否則已全額成交的 a 會吃到 b 的預算而被重下）。a 不得重下，Z 照 extra 撤。"""
    cp = CopySettings(px_rel_tol=Decimal("1e-4"), size_tolerance=Decimal("0.08"),
                      modify_policy="cancel-place")
    a, b = _spec(sz="1000"), _spec(sz="1040")
    z0 = _open(_spec(sz="1500"), oid=9)
    z1 = _open(_spec(sz="1500"), oid=9, sz="1040")
    ex = _CancelFalse(open_orders_seq=[[z1], [z1]],
                      place_reason_seq=[(False, "m"), (True, "")])
    n, res = _run_ex_settings(ex, [a, b], [z0], cp)
    sizes = [r[1].sz for r in _places(ex)]
    assert sizes.count(Decimal("1000")) == 1  # a 只初掛一次，不重下
    assert [r[2] for r in _cancels(ex)].count(9) >= 2  # 步驟 2 的撤單＋Z 照 extra 再撤


def _run_ex_settings(ex, desired, mine, settings):
    n = RecordingNotifier()
    res = _reconcile_orders(
        ex, desired, mine, settings=settings, notifier=n, state=ReconcileState(),
        live=True, clock=lambda: 1000.0, sleep_fn=lambda s: None, my_positions={})
    return n, res


def test_modify_success_same_oid_in_place_resize_is_not_extra():
    """C15（H1）：modify 回 True 且 HL 原 oid 就地改量（簿上同 oid 已是 1000）
    → modify 成功的 oid 不在 touched，正常配對：0 撤單、0 補單、0 CRIT。"""
    d = _spec(sz="1000")
    old = _open(_spec(sz="600"), oid=1)
    resized = _open(_spec(sz="1000"), oid=1)
    ex, n, res = _run_with(FakeExecutor, [d], [old], [[resized], [resized]])
    assert _cancels(ex) == []
    assert _places(ex) == []
    assert _crits(n) == []
    assert res.sync_failed is False
