"""CRIT 告警可自行診斷測試（2026-07-28 00:33 事故的可觀測性修法）。

事故中 CRIT 訊息的缺陷：extra 側只印 oid 不印價量方向、缺部位資訊、
補單失敗原因被吞掉、無 dedup——使用者無法只靠告警自行診斷。

涵蓋：extra/missing 格式（價量方向 + [ro] + 原量）、部位行、補單失敗原因、
trim 形狀診斷行、fallback 排查提示、dedup_key（oid 穩定／內容敏感）、
ActionExecutor.place_with_reason 的拒因萃取。
"""
from decimal import Decimal

from spark.copytrade.config import CopySettings
from spark.copytrade.executor import ActionExecutor, ExecutorPort
from spark.copytrade.notifier import RecordingNotifier
from spark.copytrade.orders import (
    OrderSpec,
    ReconcileState,
    _reconcile_orders,
    sync_open_orders,
)
from spark.exchange.base import BuilderCode, OpenOrder, OrderResult, Position
from spark.exchange.fakes import FakeAdapter

# 顯式釘住容忍度（不依賴 CopySettings 預設值變動）
SETTINGS = CopySettings(px_rel_tol=Decimal("1e-4"), size_tolerance=Decimal("0.08"))


class FakeExecutor:
    """離線假 executor（含 place_with_reason）。

    - open_orders_seq：get_open_orders() 依序回傳的清單，耗盡後回 []。
    - place_reason_seq：place_with_reason() 依序回傳的 (ok, reason)，耗盡後 (True, "")。
    """

    def __init__(self, open_orders_seq=None, place_reason_seq=None):
        self.records: list = []
        self._open_orders_seq = [list(x) for x in (open_orders_seq or [])]
        self._place_reason_seq = list(place_reason_seq or [])

    def place(self, spec) -> bool:
        return self.place_with_reason(spec)[0]

    def place_with_reason(self, spec) -> tuple[bool, str]:
        self.records.append(("place", spec))
        if self._place_reason_seq:
            return self._place_reason_seq.pop(0)
        return True, ""

    def modify(self, oid, spec) -> bool:
        self.records.append(("modify", oid, spec))
        return True

    def cancel(self, coin, oid) -> bool:
        self.records.append(("cancel", coin, oid))
        return True

    def market_open(self, coin, is_buy, size) -> OrderResult:
        return OrderResult(ok=True, filled_size=size, avg_px=Decimal("0"), raw={})

    def close_reduce_only(self, coin, is_buy, size) -> OrderResult:
        return OrderResult(ok=True, filled_size=size, avg_px=Decimal("0"), raw={})

    def update_leverage(self, coin, leverage, is_cross) -> bool:
        return True

    def get_open_orders(self) -> list[OpenOrder]:
        return self._open_orders_seq.pop(0) if self._open_orders_seq else []

    def get_size_decimals(self, coin) -> int:
        return 4


def _spec(coin="ETH", is_buy=False, sz="0.0457", limit_px="2000",
          reduce_only=True) -> OrderSpec:
    return OrderSpec(coin=coin, is_buy=is_buy, sz=Decimal(sz),
                     limit_px=Decimal(limit_px), reduce_only=reduce_only)


def _open_order(coin="ETH", is_buy=False, sz="0.0289", limit_px="2000",
                reduce_only=True, oid=100, orig_sz=None) -> OpenOrder:
    return OpenOrder(
        oid=oid, coin=coin, is_buy=is_buy, limit_px=Decimal(limit_px),
        sz=Decimal(sz), reduce_only=reduce_only, is_trigger=False,
        trigger_px=None, tpsl=None,
        orig_sz=Decimal(orig_sz) if orig_sz is not None else None,
    )


def _position(coin="ETH", szi="0.0289") -> Position:
    return Position(coin=coin, szi=Decimal(szi), entry_px=Decimal("1900"),
                    leverage=5, is_cross=True, unrealized_pnl=Decimal("0"),
                    margin_used=Decimal("100"))


def _run(desired, open_orders_seq, *, my_positions=None, place_reason_seq=None):
    ex = FakeExecutor(open_orders_seq=open_orders_seq,
                      place_reason_seq=place_reason_seq)
    notifier = RecordingNotifier()
    res = _reconcile_orders(
        ex, desired, [], settings=SETTINGS, notifier=notifier,
        state=ReconcileState(), live=True, clock=lambda: 1000.0,
        sleep_fn=lambda s: None,
        my_positions=my_positions if my_positions is not None else {},
    )
    return ex, notifier, res


def _crit_text(notifier) -> str:
    crits = [r for r in notifier.records if r[0] == "critical"]
    assert len(crits) == 1
    return crits[0][2]


def _crit_dedup(notifier) -> str | None:
    return [r for r in notifier.records if r[0] == "critical"][0][3]


# 事故形狀：desired ro 0.0457；交易所上只剩被修剪到部位量的 0.0289（oid 每輪換）
def _incident_scenario(*, oid1=99, oid2=100, place_reason_seq=None, extra_px="2000"):
    d = _spec(sz="0.0457")
    trimmed1 = _open_order(sz="0.0289", oid=oid1, orig_sz="0.0457", limit_px=extra_px)
    trimmed2 = _open_order(sz="0.0289", oid=oid2, orig_sz="0.0457", limit_px=extra_px)
    return _run([d], [[trimmed1], [trimmed2]],
                my_positions={"ETH": _position()},
                place_reason_seq=place_reason_seq)


# ── 1. extra 側含 oid/幣/方向/剩餘量@價/[ro]/原量；missing 側含 [ro] ──
def test_crit_extra_has_price_size_side_ro_and_orig_sz():
    _ex, notifier, res = _incident_scenario()
    assert res.sync_failed is True
    text = _crit_text(notifier)
    assert "oid=100 ETH S 0.0289@2000 [ro] 原量0.0457" in text
    assert "ETH S 0.0457@2000 [ro]" in text  # missing 側含 [ro] 標記


def test_crit_extra_without_orig_sz_omits_orig_field():
    d = _spec(sz="0.0457")
    trimmed = _open_order(sz="0.0289", oid=100, orig_sz=None)
    _ex, notifier, _res = _run([d], [[trimmed], [trimmed]],
                               my_positions={"ETH": _position()})
    text = _crit_text(notifier)
    assert "原量" not in text
    assert "oid=100 ETH S 0.0289@2000 [ro]" in text


# ── 2. 部位行 ────────────────────────────────────────────────────────
def test_crit_includes_position_line_for_involved_coins():
    _ex, notifier, _res = _incident_scenario()
    assert "部位：ETH 0.0289" in _crit_text(notifier)


def test_crit_position_line_shows_no_position():
    d = _spec(coin="BTC", is_buy=True, sz="0.5", limit_px="50000",
              reduce_only=False)
    _ex, notifier, _res = _run([d], [[], []], my_positions={})
    assert "部位：BTC 無部位" in _crit_text(notifier)


# ── 3. 補單失敗原因入訊息（不得靜默）；組合驗收：四要素同訊息 ────────
def test_crit_lists_place_failure_reasons():
    """missing+extra+補單失敗的組合案例：單一 CRIT 訊息須同時包含
    (1) 逐單價量方向與 [ro]、(2) 部位行、(3) 補單失敗原因、(4) 診斷行。"""
    reason = "Order must have minimum value of $10."
    # 第一次 place（初掛）成功、重試段的補單失敗
    _ex, notifier, _res = _incident_scenario(
        place_reason_seq=[(True, ""), (False, reason)])
    text = _crit_text(notifier)
    # (1) 價量方向 + ro 標記（missing 與 extra 兩側）
    assert "ETH S 0.0457@2000 [ro]" in text
    assert "oid=100 ETH S 0.0289@2000 [ro] 原量0.0457" in text
    # (2) 部位行
    assert "部位：ETH 0.0289" in text
    # (3) 補單失敗原因
    assert "補單失敗：" in text
    assert reason in text
    # (4) trim 形狀診斷行
    assert "診斷：ETH 的 reduce-only 掛單已被交易所修剪至部位上限" in text


# ── 4. trim 形狀 → 診斷行 ────────────────────────────────────────────
def test_trim_shape_triggers_self_heal_diagnosis_line():
    _ex, notifier, _res = _incident_scenario()
    text = _crit_text(notifier)
    assert "診斷：ETH 的 reduce-only 掛單已被交易所修剪至部位上限" in text
    assert "下一輪仍告警才需人工介入" in text


def test_non_trim_shape_without_reasons_falls_back_to_investigation_hint():
    d = _spec(coin="BTC", is_buy=True, sz="0.5", limit_px="50000",
              reduce_only=False)
    _ex, notifier, _res = _run([d], [[], []], my_positions={})
    text = _crit_text(notifier)
    assert "診斷：" not in text
    assert "原因未能自動判定" in text
    assert "historicalOrders" in text


def test_trim_diagnosis_requires_extra_total_near_position():
    """extra 剩餘量與 |部位| 差超出 size_tolerance → 不下修剪診斷（避免誤導）。"""
    d = _spec(sz="0.0457")
    stray = _open_order(sz="0.01", oid=100)  # 0.01 vs 部位 0.0289：差 65% >> 8%
    _ex, notifier, _res = _run([d], [[stray], [stray]],
                               my_positions={"ETH": _position()})
    text = _crit_text(notifier)
    assert "診斷：" not in text
    assert "原因未能自動判定" in text


# ── 5. dedup_key：oid 變化穩定、內容變化敏感 ─────────────────────────
def test_dedup_key_stable_when_only_oid_changes():
    _ex1, n1, _ = _incident_scenario(oid1=99, oid2=100)
    _ex2, n2, _ = _incident_scenario(oid1=555, oid2=556)
    k1, k2 = _crit_dedup(n1), _crit_dedup(n2)
    assert k1 is not None
    assert k1 == k2  # oid 每輪重試都會換，不得讓 dedup 失效


def test_dedup_key_changes_when_content_changes():
    _ex1, n1, _ = _incident_scenario()
    _ex2, n2, _ = _incident_scenario(extra_px="2100")  # extra 價變 → 新告警
    assert _crit_dedup(n1) != _crit_dedup(n2)


# ── Finding 2（2026-07-28 審查）：CRIT 訊息長度必須有上限 ────────────
def test_crit_message_bounded_with_many_mismatches():
    """Telegram sendMessage >4096 字元回 400、notifier 吞掉 False → 整則 CRIT
    靜默消失。每類最多列 5 張、其餘「另 M 張」摘要。"""
    desired = [_spec(coin=f"C{i}", is_buy=True, sz="1", limit_px="100",
                     reduce_only=False) for i in range(30)]
    reason = "Order must have minimum value of $10."
    seq = [(True, "")] * 30 + [(False, reason)] * 30  # 初掛 30 成功、重試 30 全拒
    ex = FakeExecutor(open_orders_seq=[[], []], place_reason_seq=seq)
    notifier = RecordingNotifier()
    _reconcile_orders(ex, desired, [], settings=SETTINGS, notifier=notifier,
                      state=ReconcileState(), live=True, clock=lambda: 1000.0,
                      sleep_fn=lambda s: None, my_positions={})
    text = _crit_text(notifier)
    assert "缺少 30" in text          # 總數不失真
    assert "另 25 張" in text          # 逐單清單被截到 5 張
    assert text.count("oid=") == 0     # extra 為空，不影響
    assert len(text) <= 3600           # 遠低於 Telegram 4096 上限


def test_crit_message_hard_truncated_at_safe_length():
    """逐類截斷後仍過長（例如拒因字串本身巨大）→ 保險截斷到安全長度。"""
    desired = [_spec(coin=f"C{i}", is_buy=True, sz="1", limit_px="100",
                     reduce_only=False) for i in range(5)]
    huge = "x" * 1000
    seq = [(True, "")] * 5 + [(False, huge)] * 5
    ex = FakeExecutor(open_orders_seq=[[], []], place_reason_seq=seq)
    notifier = RecordingNotifier()
    _reconcile_orders(ex, desired, [], settings=SETTINGS, notifier=notifier,
                      state=ReconcileState(), live=True, clock=lambda: 1000.0,
                      sleep_fn=lambda s: None, my_positions={})
    text = _crit_text(notifier)
    assert len(text) <= 3600
    assert "（已截斷）" in text


# ── Finding 3：dedup_key 對 missing sz 微動穩定（scale 每輪漂移）─────
def test_dedup_key_stable_when_missing_size_drifts():
    """desired sz 隨 scale（leader 權益/weight）每輪微動；含 sz 的鍵會讓同一
    持續狀態每輪產生新 key → dedup 失效、告警轟炸。鍵只含 幣/方向/ro/價。"""
    def _run_with_sz(sz):
        d = _spec(sz=sz)
        return _run([d], [[], []], my_positions={"ETH": _position()})

    _ex1, n1, _ = _run_with_sz("0.0457")
    _ex2, n2, _ = _run_with_sz("0.0460")  # 同幣/同向/同價，只有量微動
    assert _crit_dedup(n1) == _crit_dedup(n2)


# ── 改善 c：補單拒因 HTML escape（notifier 用 parse_mode=HTML）────────
def test_place_failure_reason_is_html_escaped():
    """拒因含 '<' 會讓 Telegram HTML parse 失敗、整則 400 消失。"""
    reason = "Order size < minimum & invalid"
    _ex, notifier, _res = _incident_scenario(
        place_reason_seq=[(True, ""), (False, reason)])
    text = _crit_text(notifier)
    assert "Order size &lt; minimum &amp; invalid" in text
    assert "size < minimum" not in text


# ── sync_open_orders 有把部位 map 接進 CRIT（呼叫點 plumbing）────────
def test_sync_open_orders_passes_positions_into_crit():
    leader = [OpenOrder(oid=1, coin="BTC", is_buy=True, limit_px=Decimal("50000"),
                        sz=Decimal("0.5"), reduce_only=False, is_trigger=False,
                        trigger_px=None, tpsl=None)]
    ex = FakeExecutor(open_orders_seq=[[], []])  # 兩驗皆空 → 缺 1 → critical
    notifier = RecordingNotifier()
    sync_open_orders(
        ex, leader, [], {"BTC": _position("BTC", szi="0.3")}, Decimal("1"),
        settings=SETTINGS, notifier=notifier, state=ReconcileState(), live=True,
        clock=lambda: 1000.0, sleep_fn=lambda s: None,
    )
    assert "部位：BTC 0.3" in _crit_text(notifier)


# ── ActionExecutor.place_with_reason ─────────────────────────────────
_BUILDER = BuilderCode(b="0xbuilder", f=20)


def _order_spec(**kw) -> OrderSpec:
    base = dict(coin="ETH", is_buy=True, sz=Decimal("1"),
                limit_px=Decimal("2000"), reduce_only=False)
    base.update(kw)
    return OrderSpec(**base)


class _RejectingAdapter(FakeAdapter):
    """place_order 回 HL 形狀的單筆委託錯誤。"""

    REASON = "Order must have minimum value of $10."

    def place_order(self, agent_signer, order, builder) -> OrderResult:
        self.calls["place_order"].append({"order": order})
        return OrderResult(ok=False, filled_size=Decimal("0"), avg_px=Decimal("0"),
                           raw={"status": "ok", "response": {
                               "type": "order",
                               "data": {"statuses": [{"error": self.REASON}]}}})


def _executor(adapter=None, *, live=False):
    return ActionExecutor(
        adapter if adapter is not None else FakeAdapter(),
        "SIGNER" if live else None, _BUILDER, live=live, my_address="0xme",
        settings=CopySettings(), clock=lambda: 1234.5,
    )


def test_place_with_reason_dry_returns_ok_and_empty_reason():
    ok, reason = _executor(live=False).place_with_reason(_order_spec())
    assert ok is True
    assert reason == ""


def test_place_with_reason_live_extracts_exchange_reject_reason():
    ad = _RejectingAdapter()
    ok, reason = _executor(ad, live=True).place_with_reason(_order_spec())
    assert ok is False
    assert reason == _RejectingAdapter.REASON


def test_place_with_reason_trigger_skip_gives_reason():
    ok, reason = _executor(live=False).place_with_reason(
        _order_spec(is_trigger=True, tpsl="sl", trigger_px=Decimal("1900")))
    assert ok is False
    assert "trigger" in reason


def test_place_still_returns_bool_and_matches_place_with_reason():
    ad = _RejectingAdapter()
    assert _executor(ad, live=True).place(_order_spec()) is False
    assert _executor(live=False).place(_order_spec()) is True


def test_fake_executor_with_reason_satisfies_port():
    assert isinstance(FakeExecutor(), ExecutorPort)
