"""ActionExecutor / VirtualBook / ActionRecord 測試（Task 12）。

全離線：adapter 用 spark.exchange.fakes.FakeAdapter（calls 記錄為「零寫入」的證據）。
重點：live gate（dry 零 adapter 寫入）、live 轉呼叫參數正確（builder/slippage 注入）、
leverage 同值去重、skip_trigger、虛擬簿演化、get_open_orders 兩態。
附 HyperliquidAdapter.get_daily_abs_pnl 的離線解析一案（欄位形狀照 hl weight.py:82-98
解析的 portfolio 回應）。
"""
from decimal import Decimal

import pytest

from spark.copytrade.config import CopySettings
from spark.copytrade.executor import ActionExecutor, ActionRecord, ExecutorPort, VirtualBook
from spark.copytrade.orders import OrderSpec
from spark.exchange.base import BuilderCode, EquityView, OpenOrder
from spark.exchange.fakes import FakeAdapter
from spark.exchange.hyperliquid import HyperliquidAdapter

SETTINGS = CopySettings(slippage=Decimal("0.05"))
BUILDER = BuilderCode(b="0xbuilder", f=20)
MY_ADDR = "0xme"

_WRITE_CALLS = ("place_order", "modify_order", "cancel_order", "market_open",
                "close_reduce_only", "update_leverage",
                "approve_builder_fee", "approve_agent")


def _spec(coin="ETH", is_buy=True, sz="1.0", limit_px="2000", reduce_only=False,
          is_trigger=False, tpsl=None, trigger_px=None, is_market=False,
          tif="Gtc") -> OrderSpec:
    return OrderSpec(
        coin=coin, is_buy=is_buy, sz=Decimal(sz), limit_px=Decimal(limit_px),
        reduce_only=reduce_only, is_trigger=is_trigger, tpsl=tpsl,
        trigger_px=Decimal(trigger_px) if trigger_px is not None else None,
        is_market=is_market, tif=tif,
    )


def _executor(adapter=None, *, live=False, signer=None, book=None, clock=None):
    return ActionExecutor(
        adapter if adapter is not None else FakeAdapter(),
        signer if signer is not None else ("SIGNER" if live else None),
        BUILDER, live=live, my_address=MY_ADDR, settings=SETTINGS,
        virtual_book=book, clock=clock or (lambda: 1234.5),
    )


def _adapter_write_calls(fa: FakeAdapter) -> list[str]:
    return [k for k in _WRITE_CALLS if fa.calls[k]]


# ── 結構：符合 ExecutorPort、live=True 無 signer 必炸 ─────────────────
def test_action_executor_satisfies_port():
    assert isinstance(_executor(), ExecutorPort)


def test_live_requires_agent_signer():
    with pytest.raises(ValueError):
        ActionExecutor(FakeAdapter(), None, BUILDER, live=True,
                       my_address=MY_ADDR, settings=SETTINGS)


def test_dry_run_allows_none_signer():
    ex = _executor(live=False, signer=None)  # shadow 零 Keychain
    assert ex.place(_spec()) is True


# ── 1. live=False：零 adapter 寫入 + records 齊全 ─────────────────────
def test_dry_run_all_actions_never_touch_adapter_writes():
    fa = FakeAdapter()
    ex = _executor(fa, live=False)
    ex.place(_spec(coin="ETH"))
    ex.modify(1, _spec(coin="ETH", limit_px="2100"))
    ex.cancel("ETH", 1)
    ex.market_open("BTC", True, Decimal("0.5"))
    ex.close_reduce_only("BTC", False, Decimal("0.5"))
    ex.update_leverage("SOL", 5, True)

    assert _adapter_write_calls(fa) == [], "dry-run 不得有任何 adapter 寫入呼叫"
    kinds = [r.kind for r in ex.records]
    assert kinds == ["place", "modify", "cancel", "market_open", "close",
                     "update_leverage"]
    assert all(isinstance(r, ActionRecord) for r in ex.records)
    assert all(r.ts == 1234.5 for r in ex.records)


def test_record_payload_numbers_are_str_and_no_key_material():
    ex = _executor(live=False)
    ex.place(_spec(sz="1.5", limit_px="2000.25"))
    ex.market_open("BTC", True, Decimal("0.5"))
    for r in ex.records:
        for k, v in r.payload.items():
            assert isinstance(v, (str, bool)) or v is None, (
                f"payload[{k}]={v!r} 應為 str/bool/None（數值存 str）")
            if isinstance(v, str):
                assert "SIGNER" not in v and "key" not in v.lower()
    place = ex.records[0]
    assert place.payload["sz"] == "1.5"
    assert place.payload["limit_px"] == "2000.25"


# ── 2. 虛擬簿演化：place → modify → cancel ────────────────────────────
def test_virtual_book_evolves_place_modify_cancel():
    book = VirtualBook()
    ex = _executor(live=False, book=book)

    assert ex.place(_spec(coin="ETH", limit_px="2000")) is True
    orders = ex.get_open_orders()
    assert len(orders) == 1
    oid = orders[0].oid
    assert orders[0].limit_px == Decimal("2000")

    assert ex.modify(oid, _spec(coin="ETH", limit_px="2100", sz="0.7")) is True
    orders = ex.get_open_orders()
    assert len(orders) == 1
    assert orders[0].oid == oid  # 就地改，oid 保留
    assert orders[0].limit_px == Decimal("2100")
    assert orders[0].sz == Decimal("0.7")

    assert ex.cancel("ETH", oid) is True
    assert ex.get_open_orders() == []


def test_virtual_book_oid_auto_increments_and_rejects_unknown():
    book = VirtualBook()
    ex = _executor(live=False, book=book)
    ex.place(_spec(coin="ETH"))
    ex.place(_spec(coin="BTC", limit_px="50000"))
    oids = [o.oid for o in ex.get_open_orders()]
    assert oids == [1, 2]
    assert ex.modify(99, _spec(coin="ETH")) is False   # 不存在的 oid
    assert ex.cancel("ETH", 99) is False
    assert ex.cancel("BTC", 1) is False  # coin 不符不撤
    assert len(ex.get_open_orders()) == 2


def test_dry_run_default_book_created_when_not_injected():
    ex = _executor(live=False, book=None)
    ex.place(_spec())
    assert len(ex.get_open_orders()) == 1


# ── 3. live=True：轉呼叫 adapter、參數正確 ────────────────────────────
def test_live_place_calls_adapter_with_order_and_builder():
    fa = FakeAdapter()
    ex = _executor(fa, live=True)
    ok = ex.place(_spec(coin="ETH", is_buy=False, sz="0.3", limit_px="2000",
                        reduce_only=True, tif="Alo"))
    assert ok is True
    calls = fa.calls["place_order"]
    assert len(calls) == 1
    order = calls[0]["order"]
    assert order.coin == "ETH" and order.is_buy is False
    assert order.size == Decimal("0.3") and order.limit_px == Decimal("2000")
    assert order.reduce_only is True and order.tif == "Alo"
    assert calls[0]["builder"] == BUILDER
    assert calls[0]["agent_signer"] == "SIGNER"


def test_live_modify_cancel_call_adapter():
    fa = FakeAdapter()
    ex = _executor(fa, live=True)
    assert ex.modify(7, _spec(coin="ETH", limit_px="2100")) is True
    assert fa.calls["modify_order"][0]["oid"] == 7
    assert fa.calls["modify_order"][0]["order"].limit_px == Decimal("2100")
    assert ex.cancel("ETH", 7) is True
    assert fa.calls["cancel_order"][0] == {
        "agent_signer": "SIGNER", "coin": "ETH", "oid": 7}


def test_live_market_open_and_close_carry_slippage_and_builder():
    fa = FakeAdapter(mids={"ETH": Decimal("2000")})
    ex = _executor(fa, live=True)
    res = ex.market_open("ETH", True, Decimal("0.5"))
    assert res.ok is True
    mo = fa.calls["market_open"][0]
    assert mo["slippage"] == SETTINGS.slippage
    assert mo["builder"] == BUILDER

    res2 = ex.close_reduce_only("ETH", False, Decimal("0.5"))
    assert res2.ok is True
    cl = fa.calls["close_reduce_only"][0]
    assert cl["slippage"] == SETTINGS.slippage
    assert cl["builder"] == BUILDER
    assert cl["is_buy"] is False


def test_live_failure_propagates_ok_false():
    fa = FakeAdapter(modify_ok=False, cancel_ok=False)
    ex = _executor(fa, live=True)
    assert ex.modify(1, _spec()) is False
    assert ex.cancel("ETH", 1) is False
    assert [r.payload["ok"] for r in ex.records] == [False, False]


# ── 4. update_leverage 同值去重（port hl trader.py:118-130）───────────
def test_update_leverage_dedups_same_value_per_coin():
    fa = FakeAdapter()
    ex = _executor(fa, live=True)
    assert ex.update_leverage("ETH", 5, True) is True
    assert ex.update_leverage("ETH", 5, True) is True   # 同值 → 去重
    assert len(fa.calls["update_leverage"]) == 1
    assert ex.update_leverage("ETH", 10, True) is True  # 變值 → 重送
    assert ex.update_leverage("BTC", 5, True) is True   # 不同 coin 各自快取
    assert len(fa.calls["update_leverage"]) == 3
    # 去重命中的那次不再記 record（沒有實際動作）
    assert len([r for r in ex.records if r.kind == "update_leverage"]) == 3


def test_update_leverage_failure_not_cached_retries_next_call():
    fa = FakeAdapter(update_leverage_ok=False)
    ex = _executor(fa, live=True)
    assert ex.update_leverage("ETH", 5, True) is False
    assert ex.update_leverage("ETH", 5, True) is False  # 失敗不入快取 → 重試
    assert len(fa.calls["update_leverage"]) == 2


# ── 5. trigger 單 M1 限制：不下單、記 skip_trigger ────────────────────
def test_place_trigger_skips_and_records_in_both_modes():
    for live in (False, True):
        fa = FakeAdapter()
        ex = _executor(fa, live=live)
        spec = _spec(coin="ETH", is_trigger=True, trigger_px="1900", tpsl="sl",
                     is_market=True)
        assert ex.place(spec) is False
        assert _adapter_write_calls(fa) == []   # 兩態皆零寫入
        assert ex.get_open_orders() == []       # dry 虛擬簿也不入（live 讀 adapter 亦空）
        assert len(ex.records) == 1
        r = ex.records[0]
        assert r.kind == "skip_trigger" and r.coin == "ETH"
        assert r.payload["op"] == "place"
        assert r.payload["trigger_px"] == "1900"
        assert r.payload["tpsl"] == "sl"


def test_modify_trigger_skips_and_records():
    fa = FakeAdapter()
    ex = _executor(fa, live=True)
    assert ex.modify(3, _spec(is_trigger=True, trigger_px="1900", tpsl="tp")) is False
    assert fa.calls["modify_order"] == []
    assert ex.records[0].kind == "skip_trigger"
    assert ex.records[0].payload["op"] == "modify"


# ── 6. get_open_orders 兩態 ──────────────────────────────────────────
def test_get_open_orders_live_reads_adapter_with_my_address():
    injected = OpenOrder(oid=9, coin="ETH", is_buy=True, limit_px=Decimal("2000"),
                         sz=Decimal("1"), reduce_only=False, is_trigger=False,
                         trigger_px=None, tpsl=None)
    fa = FakeAdapter(open_orders=[injected])
    ex = _executor(fa, live=True)
    assert ex.get_open_orders() == [injected]
    assert fa.calls["get_open_orders"] == [{"address": MY_ADDR}]


def test_get_open_orders_dry_reads_virtual_book_not_adapter():
    fa = FakeAdapter(open_orders=[OpenOrder(
        oid=9, coin="ETH", is_buy=True, limit_px=Decimal("2000"), sz=Decimal("1"),
        reduce_only=False, is_trigger=False, trigger_px=None, tpsl=None)])
    ex = _executor(fa, live=False)
    assert ex.get_open_orders() == []       # 虛擬簿為空，不read adapter
    assert fa.calls["get_open_orders"] == []


# ── 7. get_size_decimals 轉 adapter（讀不需 live gate）────────────────
def test_get_size_decimals_passthrough_in_both_modes():
    fa = FakeAdapter(sz_decimals={"ETH": 3})
    assert _executor(fa, live=False).get_size_decimals("ETH") == 3
    assert _executor(fa, live=True).get_size_decimals("ETH") == 3


# ── 8. HyperliquidAdapter.get_daily_abs_pnl 解析（hl weight.py:82-98）─
class _PortfolioInfo:
    """欄位形狀照 hl 解析的 portfolio 回應：[period, {pnlHistory: [[ts_ms, cum], ...]}]。"""

    def __init__(self, resp):
        self._resp = resp

    def portfolio(self, address):
        return self._resp


def test_get_daily_abs_pnl_diffs_cumulative_month_history():
    day_ms = 86_400_000
    resp = [
        ["day", {"pnlHistory": [[0, "999"]]}],  # 非 month 時間窗應被忽略
        ["month", {"pnlHistory": [
            [0 * day_ms, "0"],        # day0 累積 0
            [1 * day_ms, "10"],       # day1 累積 10 → 日 |PnL| 10
            [2 * day_ms, "-5"],       # day2 累積 -5 → |−15| = 15
            [2 * day_ms + 3600_000, "-3"],  # 同日第二筆 → 覆蓋為 -3 → |−13| = 13
        ]}],
    ]
    ad = HyperliquidAdapter(network="testnet", info=_PortfolioInfo(resp))
    assert ad.get_daily_abs_pnl("0xleader") == [Decimal("10"), Decimal("13")]


def test_get_daily_abs_pnl_no_month_row_returns_empty():
    ad = HyperliquidAdapter(network="testnet", info=_PortfolioInfo(
        [["week", {"pnlHistory": [[0, "1"]]}]]))
    assert ad.get_daily_abs_pnl("0xleader") == []


def test_fake_adapter_returns_injected_daily_abs_pnl():
    fa = FakeAdapter(daily_abs_pnl=[Decimal("10"), Decimal("20")])
    assert fa.get_daily_abs_pnl("0xleader") == [Decimal("10"), Decimal("20")]
    assert fa.calls["get_daily_abs_pnl"] == [{"address": "0xleader"}]


def test_equity_view_not_used_for_scale_reminder():
    """工程原則 1 防呆：scale 的兩邊 equity 來源是 get_account_state().account_value
    （loop.py 接線責任，test_copy_loop.py 驗證）；EquityView 只給回撤判定用。
    此處驗證 FakeAdapter 兩讀彼此獨立，不會互相污染。"""
    fa = FakeAdapter(equity=EquityView(current=Decimal("1"), recent_peak=Decimal("2")))
    assert fa.get_account_state("0x").account_value == Decimal("0")
    assert fa.get_equity_view("0x").current == Decimal("1")
