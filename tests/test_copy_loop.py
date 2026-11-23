"""run_cycle / main_loop / CLI 解析測試（Task 12）。

全離線：FakeAdapter（讀側注入）+ ActionExecutor（dry）+ RecordingNotifier。
main_loop 用假 clock/sleep/now_fn；CLI 只測 argparse 與 live 判定純函式，
外加 subprocess 實跑「無 env → 用法 + exit 2」（import 階段不觸網的證據）。
"""
import os
import subprocess
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import spark.copytrade.loop as loop_mod
from spark.copytrade.config import CopySettings
from spark.copytrade.executor import ActionExecutor
from spark.copytrade.killswitch import ARM_FILE_RELPATH
from spark.copytrade.loop import (
    _seconds_until_next_interval,
    main_loop,
    run_cycle,
)
from spark.copytrade.notifier import RecordingNotifier
from spark.copytrade.orders import CycleReport, ReconcileResult, ReconcileState
from spark.exchange.base import (
    AccountSnapshot,
    AccountTotal,
    BuilderCode,
    EquityView,
    OpenOrder,
    Position,
)
from spark.exchange.fakes import FakeAdapter
from tests.leader_total_helper import seed_leader_total as _seed_leader_total

REPO_ROOT = Path(__file__).resolve().parents[1]
LEADER = CopySettings().leader_address
MY_ADDR = "0xme"
BUILDER = BuilderCode(b="0xbuilder", f=20)

_WRITE_CALLS = ("place_order", "modify_order", "cancel_order", "market_open",
                "close_reduce_only", "update_leverage")


def _seed_equity_peak(root, peak: str, *, ts: float | None = None) -> None:
    """預先播種 perp 權益樣本檔，讓 perp_equity_view 算出指定的 peak。

    回撤判定 2026-07-19 起改用 perp accountValue + 本地滾動樣本（findings F1），
    不再讀 adapter 的 EquityView 注入槽——breach 情境改由此 helper 建立。
    """
    import json
    import time
    from spark.copytrade.equity import SAMPLES_RELPATH

    if ts is None:
        ts = time.time()
    p = root / SAMPLES_RELPATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([[ts, peak]]))


def _settings(**kw) -> CopySettings:
    kw.setdefault("volatility_weight_enabled", False)
    return CopySettings(**kw)


def _account(value="1000", ntl="0") -> AccountSnapshot:
    return AccountSnapshot(account_value=Decimal(value), total_margin_used=Decimal("0"),
                           withdrawable=Decimal(value), total_ntl_pos=Decimal(ntl))


def _healthy_equity() -> EquityView:
    return EquityView(current=Decimal("1000"), recent_peak=Decimal("1000"))


def _executor(adapter, *, live=False) -> ActionExecutor:
    return ActionExecutor(adapter, "SIGNER" if live else None, BUILDER, live=live,
                          my_address=MY_ADDR, settings=_settings())


def _run(adapter, *, settings=None, notifier=None, state=None, root=None,
         ex=None, tmp_path=None) -> tuple[CycleReport, RecordingNotifier, ActionExecutor]:
    notifier = notifier or RecordingNotifier()
    ex = ex or _executor(adapter)
    _seed_leader_total(adapter)
    report = run_cycle(adapter, ex, settings or _settings(), notifier,
                       state or ReconcileState(), root or tmp_path)
    return report, notifier, ex


# ── 1. tripped 短路：零交易呼叫、critical 帶 dedup ────────────────────
def test_tripped_short_circuits_with_zero_calls(tmp_path):
    arm = tmp_path / ARM_FILE_RELPATH
    arm.parent.mkdir(parents=True)
    arm.write_text("{}")
    fa = FakeAdapter()
    report, notifier, ex = _run(fa, tmp_path=tmp_path)

    assert report.tripped is True
    assert report.scale == Decimal("0")
    assert dict(fa.calls) == {}, "tripped 短路不得有任何 adapter 呼叫（含讀取）"
    assert ex.records == []
    crits = [r for r in notifier.records if r[0] == "critical"]
    assert len(crits) == 1
    assert crits[0][1] == "killswitch"
    assert crits[0][3] == "tripped"  # dedup_key（TelegramNotifier TTL 去重靠它）


# ── 1b. owner_close 終態：發完成通知後 halt_engine=True（裁決 D1/D5）─────
def _owner_close_arm(root, *, tripped_at=None,
                     phase="complete", failures=None,
                     orders_not_cancelled=False, cancelled=3, closed=None,
                     reason="owner_close") -> None:
    """本檔專用（不依賴 tests/test_copy_killswitch.py）——寫一份能通過
    `owner_close_terminal` 判定的 ARM payload，欄位對齊 killswitch.trip() 寫入形狀。"""
    import json
    from datetime import datetime, timezone
    # ⚠️ 不可寫死時間戳：reason=drawdown 的案例會先經過 auto_rearm_if_cooled_down，
    # 寫死的時間一過 12 小時冷靜期就被自動解鎖（2026-09-08 跨日後真的發生過）。
    if tripped_at is None:
        tripped_at = datetime.now(timezone.utc).isoformat()
    p = root / ARM_FILE_RELPATH
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "tripped_at": tripped_at,
        "current": "700",
        "peak": "1000",
        "drawdown_pct": "0.3",
        "breached": True,
        "reason": reason,
        "phase": phase,
        "cancelled": cancelled,
        "orders_not_cancelled": orders_not_cancelled,
        "closed": closed or [],
        "failures": failures or [],
    }
    p.write_text(json.dumps(payload))


def test_owner_close_terminal_halts_engine_with_completion_notice(tmp_path):
    """(a) 終態、無殘留 → halt_engine=True、恰一則 critical 含完成訊息、零交易。"""
    _owner_close_arm(tmp_path, tripped_at="2026-09-07T01:00:00+00:00")
    fa = FakeAdapter()
    report, notifier, ex = _run(fa, tmp_path=tmp_path)

    assert report.halt_engine is True
    assert report.tripped is True
    assert dict(fa.calls) == {}, "owner_close 終態不得有任何 adapter 呼叫（含讀取）"
    assert ex.records == []
    crits = [r for r in notifier.records if r[0] == "critical"]
    assert len(crits) == 1
    assert crits[0][3] == "owner_close_done"
    assert "平倉並撤銷已完成" in crits[0][2]
    assert "2026-09-07T01:00:00+00:00" in crits[0][2]
    assert "殘留暴險" not in crits[0][2]


def test_owner_close_terminal_with_residual_still_halts_and_names_failures(tmp_path):
    """(b) 終態、有殘留暴險（D1：仍只發一則、仍 halt_engine=True，訊息明列殘留）。"""
    _owner_close_arm(tmp_path, failures=["BTC"], orders_not_cancelled=True)
    fa = FakeAdapter()
    report, notifier, ex = _run(fa, tmp_path=tmp_path)

    assert report.halt_engine is True
    assert dict(fa.calls) == {}
    assert ex.records == []
    crits = [r for r in notifier.records if r[0] == "critical"]
    assert len(crits) == 1, "D1：有無殘留都只發一則"
    assert crits[0][3] == "owner_close_done"
    assert "殘留暴險" in crits[0][2]
    assert "BTC" in crits[0][2]


def test_owner_close_reason_mismatch_keeps_ordinary_tripped_message(tmp_path):
    """(b2) reason=drawdown（非 owner_close）→ 不算終態：halt_engine=False，
    訊息仍是既有那則「kill switch 已 tripped」。"""
    _owner_close_arm(tmp_path, reason="drawdown")
    fa = FakeAdapter()
    report, notifier, ex = _run(fa, tmp_path=tmp_path)

    assert report.halt_engine is False
    assert report.tripped is True
    assert dict(fa.calls) == {}
    assert ex.records == []
    crits = [r for r in notifier.records if r[0] == "critical"]
    assert len(crits) == 1
    assert crits[0][3] == "tripped"
    assert "kill switch 已 tripped" in crits[0][2]
    assert "平倉並撤銷已完成" not in crits[0][2]


# ── 2. breach → flatten_on_breach=True 時呼叫 trip ────────────────────
def test_breach_with_flatten_calls_trip(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(loop_mod, "trip",
                        lambda ex, pos, notifier, root, status, reason="": calls.append(
                            (pos, root, status, reason)))
    fa = FakeAdapter(
        account_value=Decimal("700"),  # 對照播種的 peak=1000 → dd=0.3
        positions=[Position(coin="ETH", szi=Decimal("1"), entry_px=Decimal("2000"),
                            leverage=5, is_cross=True, unrealized_pnl=Decimal("0"),
                            margin_used=Decimal("0"))],
    )
    _seed_equity_peak(tmp_path, "1000")
    report, notifier, _ = _run(fa, tmp_path=tmp_path)  # 預設 max_dd=0.20、flatten 開

    assert report.tripped is True
    assert len(calls) == 1
    pos, root, status, reason = calls[0]
    assert status.breached is True and status.drawdown_pct == Decimal("0.3")
    # ⭐ reason 必須明講（審查 F1）：沒有 reason 的鎖檔不可自動恢復，而 7 天滾動窗
    # 的熔斷應該要能在冷靜期後恢復——兩者靠這個字串區分。
    assert reason == "drawdown"
    assert set(pos) == {"ETH"}
    assert root == tmp_path
    assert any(r[0] == "critical" and r[3] == "dd_breach" for r in notifier.records)


def test_breach_without_flatten_skips_trip_but_still_tripped_report(tmp_path, monkeypatch):
    monkeypatch.setattr(loop_mod, "trip",
                        lambda *a, **k: pytest.fail("flatten_on_breach=False 不得呼叫 trip"))
    fa = FakeAdapter(account_value=Decimal("700"))
    _seed_equity_peak(tmp_path, "1000")
    report, notifier, _ = _run(fa, settings=_settings(flatten_on_breach=False),
                               tmp_path=tmp_path)
    assert report.tripped is True
    assert any(r[0] == "critical" and r[3] == "dd_breach" for r in notifier.records)


# ── 2b. 風控總開關關閉（錢包主人自選，2026-07-30）─────────────────────
# 關的是**執法**，不是蒐證；而且關不掉鎖檔短路。


def test_risk_controls_off_does_not_trip_on_breach(tmp_path, monkeypatch):
    """同 test_breach_with_flatten_calls_trip 的 breach 情境（dd=0.3 > 0.20），
    但錢包主人關掉風控 ⇒ 不 trip、不發 dd_breach、本輪照常對帳。"""
    monkeypatch.setattr(loop_mod, "trip",
                        lambda *a, **k: pytest.fail("風控關閉時不得呼叫 trip"))
    fa = FakeAdapter(account_value=Decimal("700"), account=_account("700"))
    _seed_equity_peak(tmp_path, "1000")
    report, notifier, _ = _run(fa, settings=_settings(risk_controls_enabled=False),
                               tmp_path=tmp_path)
    assert report.tripped is False, "風控關閉時 breach 不得停止交易"
    assert not any(r[3] == "dd_breach" for r in notifier.records)


def test_risk_controls_off_says_so_loudly_once_per_process(tmp_path):
    """保護不存在必須大聲（工程原則 3），且訊息與「保護尚未生效」分得開——
    後者是故障，這裡是客戶的選擇；混成一句話會讓真故障被當成選擇而忽略。"""
    fa = FakeAdapter(account_value=Decimal("700"), account=_account("700"))
    _seed_equity_peak(tmp_path, "1000")
    _, notifier, _ = _run(fa, settings=_settings(risk_controls_enabled=False),
                          tmp_path=tmp_path)
    warns = [r for r in notifier.records if r[0] == "warn"]
    disabled = [r for r in warns if r[3] == "risk_controls_disabled"]
    assert len(disabled) == 1
    assert "風控已由錢包主人關閉" in disabled[0][2]
    # 不得沿用 coverage 不足那組 key／訊息（那是故障訊號，不是客戶選擇）
    assert not any(r[3] == "equity_coverage_insufficient" for r in notifier.records)


def test_risk_off_warning_does_not_flood_the_shared_alert_channel(tmp_path):
    """⭐⭐ 審查 F2：風控關閉是**穩定狀態**而非事件。每輪一則會變成每天數百則洗版
    同一個共用 TG 頻道，而 notifier 撞上 429 之後**所有**告警都送不出去——那正是
    這則提醒要保護的東西。觸發情境：任何風控關閉的 follower（＝預設每一顆）連續跑。
    """
    fa = _seed_leader_total(FakeAdapter(account_value=Decimal("700"), account=_account("700")))
    _seed_equity_peak(tmp_path, "1000")
    settings = _settings(risk_controls_enabled=False)
    notifier = RecordingNotifier()
    state = ReconcileState()
    ex = _executor(fa)
    for _ in range(60):     # 一小時（interval_s=60）
        run_cycle(fa, ex, settings, notifier, state, tmp_path)
    disabled = [r for r in notifier.records if r[3] == "risk_controls_disabled"]
    assert len(disabled) == 1, f"一小時內只該說一次，實際 {len(disabled)} 次"
    assert "只發一次" in disabled[0][2], "訊息要講明節奏，否則讀者會以為它會一直提醒"


def test_risk_off_warning_fires_once_per_process_only(tmp_path):
    """⭐ 2026-07-31 使用者裁決：**每個進程只發一次**，不定期重發。

    「這顆引擎沒有風控」是設定事實而非事件，適合看面板（心跳持續帶著
    `risk.controls_enabled`），不適合長期佔用共用告警通道。重啟會再發一次
    ——那是有意義的，因為重啟正是風控姿態可能改變的時刻（新 state 物件）。
    """
    fa = _seed_leader_total(FakeAdapter(account_value=Decimal("700"), account=_account("700")))
    _seed_equity_peak(tmp_path, "1000")
    settings = _settings(risk_controls_enabled=False)
    notifier = RecordingNotifier()
    state = ReconcileState()
    ex = _executor(fa)
    for _ in range(200):        # 遠超過任何合理的重發間隔
        run_cycle(fa, ex, settings, notifier, state, tmp_path)
    assert len([r for r in notifier.records if r[3] == "risk_controls_disabled"]) == 1

    # 重啟＝新的 ReconcileState ⇒ 再發一次（風控姿態可能在重啟時改變）
    run_cycle(fa, ex, settings, notifier, ReconcileState(), tmp_path)
    assert len([r for r in notifier.records if r[3] == "risk_controls_disabled"]) == 2


def test_risk_controls_off_keeps_sampling_equity(tmp_path):
    """⭐ 關執法不關蒐證：樣本與高水位必須續寫，否則客戶勾選啟用的那一刻，
    回撤保護會因為「樣本覆蓋不足」而實質不存在（開了等於沒開）。"""
    import json

    from spark.copytrade.equity import LIFETIME_PEAK_RELPATH, SAMPLES_RELPATH
    fa = FakeAdapter(account_value=Decimal("880"), account=_account("880"))
    _run(fa, settings=_settings(risk_controls_enabled=False), tmp_path=tmp_path)
    samples = json.loads((tmp_path / SAMPLES_RELPATH).read_text())
    assert [s[1] for s in samples] == ["880"], "本輪權益必須進樣本檔"
    peak = json.loads((tmp_path / LIFETIME_PEAK_RELPATH).read_text())
    assert Decimal(str(peak["peak"] if isinstance(peak, dict) else peak)) == Decimal("880")


def test_risk_controls_off_never_bypasses_the_arm_lock(tmp_path):
    """⭐ 鎖檔短路不歸這個開關管：已 tripped 的交易只能人工 re-arm。
    若關風控能繞過 ARM 檔，客戶就有了一個自助解除熔斷的按鈕。"""
    arm = tmp_path / ARM_FILE_RELPATH
    arm.parent.mkdir(parents=True)
    arm.write_text("{}")
    fa = FakeAdapter()
    report, notifier, ex = _run(fa, settings=_settings(risk_controls_enabled=False),
                               tmp_path=tmp_path)
    assert report.tripped is True
    assert dict(fa.calls) == {}
    assert ex.records == []
    assert any(r[0] == "critical" and r[3] == "tripped" for r in notifier.records)


def test_peak_zero_warns_no_data_and_continues(tmp_path):
    """degenerate equity 的 warn 由 killswitch.evaluate() 結構性內建
    （dedup_key="equity_degenerate"）——run_cycle 不得直呼 check_drawdown 繞過它。"""
    fa = FakeAdapter(equity=EquityView(current=Decimal("0"), recent_peak=Decimal("0")),
                     account=_account())
    report, notifier, _ = _run(fa, tmp_path=tmp_path)
    assert report.tripped is False  # 無資料不是 breach，本輪照常對帳
    warns = [r for r in notifier.records if r[0] == "warn"]
    assert any(r[3] == "equity_degenerate" for r in warns)


# ── 3. 正常路徑組裝：sync_open_orders 收到的參數（mock 驗）───────────
def test_normal_path_passes_scale_live_state_to_sync(tmp_path, monkeypatch):
    captured = {}

    def fake_sync(ex, leader_orders, my_orders, my_positions, scale, **kw):
        captured.update(kw, ex=ex, leader_orders=leader_orders, my_orders=my_orders,
                        my_positions=my_positions, scale=scale)
        return CycleReport(
            reconcile=ReconcileResult(0, 0, 0, 0, False, ()),
            safety_net=kw["safety_net"](), scale=scale)

    monkeypatch.setattr(loop_mod, "sync_open_orders", fake_sync)
    leader_order = OpenOrder(oid=1, coin="ETH", is_buy=True, limit_px=Decimal("2000"),
                             sz=Decimal("1"), reduce_only=False, is_trigger=False,
                             trigger_px=None, tpsl=None)
    fa = FakeAdapter(equity=_healthy_equity(), account=_account("1000"),
                     open_orders=[leader_order],
                     positions=[Position(coin="BTC", szi=Decimal("2"),
                                         entry_px=Decimal("50000"), leverage=7,
                                         is_cross=False, unrealized_pnl=Decimal("0"),
                                         margin_used=Decimal("0"))],
                     active_asset_leverage={"ETH": (25, True)})
    state = ReconcileState()
    report, notifier, ex = _run(fa, state=state, tmp_path=tmp_path)

    # scale = (my_equity × util × weight) / leader_equity = 1000/1000 = 1
    # （兩邊 equity 同源：都取 get_account_state().account_value——工程原則 1）
    assert captured["scale"] == Decimal("1")
    assert captured["live"] is False       # dry executor → live=False 一路傳到底
    assert captured["state"] is state
    assert captured["ex"] is ex
    assert captured["leader_orders"] == [leader_order]
    assert captured["my_orders"] == []     # dry：my orders 經 ex 讀虛擬簿
    assert captured["protected"] == set()  # holding protection 預設關 → 空集合
    # leader 有部位的 coin → leverage map（取 leader 部位欄位）
    assert captured["leverage_by_coin"] == {"BTC": (7, False)}
    # map 查無的幣（leader 空手）→ fallback 查 activeAssetData，地址用 leader
    #（2026-07-25 首航盲區修法接線）
    assert captured["leverage_fallback"]("ETH") == (25, True)
    assert fa.calls["get_active_asset_leverage"] == [
        {"address": LEADER, "coin": "ETH"}]
    # safety_net 已接線且回傳 dict（fake_sync 內已實際呼叫一次）
    assert isinstance(report.safety_net, dict)
    assert set(report.safety_net) >= {"opened", "flattened", "failed"}
    # 讀取地址正確：leader 與 my 各讀了 orders/positions/account
    addrs = [c["address"] for c in fa.calls["get_account_state"]]
    assert addrs == [LEADER, MY_ADDR]


def test_leverage_fallback_wired_end_to_end_through_run_cycle(tmp_path):
    """首航盲區 loop 整合（不 mock sync）：leader 持倉 BTC → BTC 用持倉欄位值；
    ETH 空手純掛單 → 用 activeAssetData fallback 值。兩張掛單照常送出。"""
    btc_pos = Position(coin="BTC", szi=Decimal("2"), entry_px=Decimal("50000"),
                       leverage=7, is_cross=False, unrealized_pnl=Decimal("0"),
                       margin_used=Decimal("0"))
    leader_orders = [
        OpenOrder(oid=1, coin="ETH", is_buy=True, limit_px=Decimal("2000"),
                  sz=Decimal("1"), reduce_only=False, is_trigger=False,
                  trigger_px=None, tpsl=None),
        OpenOrder(oid=2, coin="BTC", is_buy=True, limit_px=Decimal("50000"),
                  sz=Decimal("1"), reduce_only=False, is_trigger=False,
                  trigger_px=None, tpsl=None),
    ]
    fa = FakeAdapter(equity=_healthy_equity(), account=_account("1000"),
                     open_orders=leader_orders, positions=[btc_pos],
                     active_asset_leverage={"ETH": (25, True)})
    report, notifier, ex = _run(fa, tmp_path=tmp_path)

    lev = [(r.coin, r.payload["leverage"], r.payload["is_cross"])
           for r in ex.records if r.kind == "update_leverage"]
    assert sorted(lev) == [("BTC", "7", False), ("ETH", "25", True)]
    # fallback 只對 map 查無的 ETH 查一次，地址是 leader
    assert fa.calls["get_active_asset_leverage"] == [
        {"address": LEADER, "coin": "ETH"}]
    assert report.reconcile.placed == 2  # 兩張掛單照常送出


def test_volatility_weight_scales_down_via_leader_daily_pnl(tmp_path, monkeypatch):
    captured = {}

    def fake_sync(ex, leader_orders, my_orders, my_positions, scale, **kw):
        captured["scale"] = scale
        return CycleReport(reconcile=ReconcileResult(0, 0, 0, 0, False, ()),
                           safety_net={"skipped": True}, scale=scale)

    monkeypatch.setattr(loop_mod, "sync_open_orders", fake_sync)
    # baseline [10,20]：μ=15、pstdev=5；today=30 → z=3 → 扣 0.6 → weight=0.4
    fa = FakeAdapter(equity=_healthy_equity(), account=_account("1000"),
                     daily_abs_pnl=[Decimal("10"), Decimal("20"), Decimal("30")])
    _run(fa, settings=_settings(volatility_weight_enabled=True), tmp_path=tmp_path)

    assert fa.calls["get_daily_abs_pnl"] == [{"address": LEADER}]  # 取 leader 的波動
    assert captured["scale"] == Decimal("0.4")


def test_volatility_disabled_never_fetches_daily_pnl(tmp_path):
    fa = FakeAdapter(equity=_healthy_equity(), account=_account())
    _run(fa, settings=_settings(volatility_weight_enabled=False), tmp_path=tmp_path)
    assert fa.calls["get_daily_abs_pnl"] == []


# ── 3a. leader 權益單一產生點（plan 2026-11-22 T2）─────────────────────
# 分母＝交易所帳戶總值（`get_account_total`），不是 leader 的 perp accountValue。
# 錨例 E1／E2 取自事故對帳（recon §4）；E3／E4／E5 的純函式版在 test_leader_equity.py。
_NOW_S = 1_763_900_000
_NOW_MS = _NOW_S * 1000


class _SplitAdapter(FakeAdapter):
    """leader 與 follower 的持倉分開給（FakeAdapter 的 positions 是與位址無關的單一槽）。"""

    def __init__(self, *, leader_positions=(), my_positions=(), **kw):
        super().__init__(**kw)
        self._pos_by_addr = {LEADER.lower(): list(leader_positions),
                             MY_ADDR.lower(): list(my_positions)}

    def get_positions(self, address):
        self.calls["get_positions"].append({"address": address})
        return list(self._pos_by_addr.get(address.lower(), []))


def _btc_pos(szi: str) -> Position:
    return Position(coin="BTC", szi=Decimal(szi), entry_px=Decimal("100000"),
                    leverage=5, is_cross=True, unrealized_pnl=Decimal("0"),
                    margin_used=Decimal("0"))


def _split_run(tmp_path, monkeypatch, *, leader_av, leader_total, leader_szi,
               my_av, total_age_ms=0, util="0.5"):
    """事故形狀：leader 統一帳戶（perp av 小、帳戶總值大）、follower 傳統帳戶。"""
    monkeypatch.setattr(loop_mod.time, "time", lambda: float(_NOW_S))
    fa = _SplitAdapter(
        leader_positions=[_btc_pos(leader_szi)],
        account_value=my_av,
        account_states={LEADER: _account(leader_av), MY_ADDR: _account(my_av)},
        account_totals={LEADER: AccountTotal(Decimal(leader_total),
                                             _NOW_MS - total_age_ms)},
        mids={"BTC": Decimal("100000")}, sz_decimals={"BTC": 5})
    return fa, *_run(fa, settings=_settings(capital_utilization=Decimal(util)),
                     tmp_path=tmp_path)


def _leader_equity_crits(notifier) -> list[str]:
    """只看 leader_equity 這個類別的 critical（其他閘門如 equity_coverage_insufficient
    與本題無關，且不影響零動作斷言）。"""
    return [r[3] for r in notifier.records
            if r[0] == "critical" and r[1] == "leader_equity"]


def _opened_btc(ex) -> tuple[bool, Decimal]:
    opens = [r for r in ex.records if r.kind == "market_open" and r.coin == "BTC"]
    assert len(opens) == 1, [r.kind for r in ex.records]
    return opens[0].payload["is_buy"], Decimal(opens[0].payload["sz"])


def test_E1_scale_uses_exchange_total_not_perp_account_value(tmp_path, monkeypatch):
    """E1（recon §4，13:57:10Z）：total 707.37、perp av 120.58；follower 4704.22、
    使用率 0.5 → scale＝2352.11÷707.37＝3.3252；leader −0.02 BTC → 目標 −0.0665 BTC
    （舊算法 scale＝19.5066、目標 −0.39 BTC）。"""
    fa, report, notifier, ex = _split_run(
        tmp_path, monkeypatch, leader_av="120.58", leader_total="707.37",
        leader_szi="-0.02", my_av="4704.22")
    assert abs(report.scale - Decimal("3.3252")) < Decimal("0.0001")
    is_buy, sz = _opened_btc(ex)
    assert is_buy is False
    assert abs(sz - Decimal("0.0665")) < Decimal("0.0001")
    assert fa.calls["get_account_total"] == [{"address": LEADER}]
    assert _leader_equity_crits(notifier) == []


def test_E2_incident_first_cycle_scale_is_not_111(tmp_path, monkeypatch):
    """E2（事故第一輪，13:49:03Z）：total 708、perp av 21.2；follower 4744.73 →
    scale＝2372.365÷708＝3.3508（舊算法 111.9）；leader −0.015 → 目標 −0.0503 BTC
    （事故實際 −1.6788）。"""
    _fa, report, _n, ex = _split_run(
        tmp_path, monkeypatch, leader_av="21.2", leader_total="708",
        leader_szi="-0.015", my_av="4744.73")
    assert abs(report.scale - Decimal("3.3508")) < Decimal("0.0001")
    is_buy, sz = _opened_btc(ex)
    assert is_buy is False
    assert abs(sz - Decimal("0.0503")) < Decimal("0.0001")


def test_E3_stale_total_with_exposure_means_zero_actions_and_critical(
        tmp_path, monkeypatch):
    """E3：total 的時間戳比 now 早 181 秒、leader 有倉位與掛單 → 本輪零動作。
    perp av 刻意給很大（5000）：不得退回它來算倍率。"""
    leader_order = OpenOrder(oid=1, coin="ETH", is_buy=True, limit_px=Decimal("2000"),
                             sz=Decimal("1"), reduce_only=False, is_trigger=False,
                             trigger_px=None, tpsl=None)
    monkeypatch.setattr(loop_mod.time, "time", lambda: float(_NOW_S))
    fa = _SplitAdapter(
        leader_positions=[_btc_pos("-0.02")], open_orders=[leader_order],
        account_value="4704.22",
        account_states={LEADER: _account("5000"), MY_ADDR: _account("4704.22")},
        account_totals={LEADER: AccountTotal(Decimal("707.37"), _NOW_MS - 181_000)},
        mids={"BTC": Decimal("100000")}, sz_decimals={"BTC": 5})
    report, notifier, ex = _run(fa, settings=_settings(), tmp_path=tmp_path)

    assert ex.records == [], "無法判定時不得有任何下單／改單／撤單／調倉／改槓桿"
    crits = [r for r in notifier.records
             if r[0] == "critical" and r[1] == "leader_equity"]
    assert len(crits) == 1
    assert "leader 權益無法判定" in crits[0][2]
    assert crits[0][3] == "leader_equity_unavailable"
    assert report.skip_detail and "leader 權益無法判定" in report.skip_detail
    assert report.tripped is False
    assert report.scale == Decimal("0")
    assert report.reconcile.placed == 0 and report.safety_net == {"skipped": True}
    # 提前返回發生在讀 follower 狀態之前
    assert "get_account_state" in fa.calls
    assert [c["address"] for c in fa.calls["get_account_state"]] == [LEADER]


def test_unavailable_when_total_unreadable_never_falls_back_to_perp_av(
        tmp_path, monkeypatch):
    """讀不到帳戶總值（ValueError）→ 與過期同一條路徑：零動作＋critical。
    直接呼叫 run_cycle（不經 `_run` 的補 total helper）——這裡要的是「真的讀不到」。"""
    monkeypatch.setattr(loop_mod.time, "time", lambda: float(_NOW_S))
    fa = _SplitAdapter(
        leader_positions=[_btc_pos("-0.02")], account_value="4704.22",
        account_states={LEADER: _account("5000"), MY_ADDR: _account("4704.22")},
        mids={"BTC": Decimal("100000")}, sz_decimals={"BTC": 5})
    ex = _executor(fa)
    notifier = RecordingNotifier()
    report = run_cycle(fa, ex, _settings(), notifier, ReconcileState(), tmp_path)
    assert ex.records == []
    assert report.skip_detail and "讀不到帳戶總值" in report.skip_detail
    assert _leader_equity_crits(notifier) == ["leader_equity_unavailable"]


def test_E5_zero_total_with_exposure_is_unavailable(tmp_path, monkeypatch):
    _fa, report, notifier, ex = _split_run(
        tmp_path, monkeypatch, leader_av="300", leader_total="0",
        leader_szi="-0.02", my_av="4704.22")
    assert ex.records == []
    assert report.skip_detail
    assert _leader_equity_crits(notifier) == ["leader_equity_unavailable"]


def test_E4_empty_leader_with_zero_total_is_quiet_scale_zero(tmp_path, monkeypatch):
    """E4：total 0、leader 無倉位無掛單 → leader_equity 0、scale 0、無告警、無 skip_detail。"""
    monkeypatch.setattr(loop_mod.time, "time", lambda: float(_NOW_S))
    fa = FakeAdapter(
        account_value="4704.22",
        account_states={LEADER: _account("0"), MY_ADDR: _account("4704.22")},
        account_totals={LEADER: AccountTotal(Decimal("0"), _NOW_MS)})
    report, notifier, ex = _run(fa, settings=_settings(), tmp_path=tmp_path)
    assert report.scale == Decimal("0")
    assert report.skip_detail is None
    assert not any(r[3] == "leader_equity_unavailable" for r in notifier.records)
    assert not any(r[1] == "leader_equity" for r in notifier.records)


class _SplitOrdersAdapter(_SplitAdapter):
    """連掛單也依位址分開給（D3a 測試：leader 沒有掛單、follower 有殘留掛單）。"""

    def __init__(self, *, orders_by_addr=None, **kw):
        super().__init__(**kw)
        self._orders_by_addr = {a.lower(): list(v) for a, v in (orders_by_addr or {}).items()}

    def get_open_orders(self, address):
        self.calls["get_open_orders"].append({"address": address})
        return list(self._orders_by_addr.get(address.lower(), []))


def test_D3a_no_exposure_total_unreadable_still_lets_follower_clean_up(
        tmp_path, monkeypatch):
    """D3a：leader 無倉位無掛單、帳戶總值讀取丟 ValueError、follower 有殘留倉位與殘留掛單
    → 該輪**有**平倉與撤單、沒有 leader_equity 類 critical、skip_detail 為 None。
    直接呼叫 run_cycle（不經 `_run` 的補 total helper）——這裡要的是「真的讀不到」。"""
    monkeypatch.setattr(loop_mod.time, "time", lambda: float(_NOW_S))
    stale_order = OpenOrder(oid=77, coin="ETH", is_buy=True, limit_px=Decimal("2000"),
                            sz=Decimal("1"), reduce_only=False, is_trigger=False,
                            trigger_px=None, tpsl=None)
    fa = _SplitOrdersAdapter(
        orders_by_addr={MY_ADDR: [stale_order]},
        my_positions=[_btc_pos("0.01")], account_value="4704.22",
        account_states={LEADER: _account("0"), MY_ADDR: _account("4704.22")},
        mids={"BTC": Decimal("100000"), "ETH": Decimal("2000")},
        sz_decimals={"BTC": 5, "ETH": 4})
    ex = _executor(fa, live=True)
    notifier = RecordingNotifier()
    report = run_cycle(fa, ex, _settings(), notifier, ReconcileState(), tmp_path)

    done = {(r.kind, r.coin) for r in ex.records}
    assert ("cancel", "ETH") in done, [(r.kind, r.coin) for r in ex.records]
    assert ("close", "BTC") in done, [(r.kind, r.coin) for r in ex.records]
    assert report.skip_detail is None
    assert _leader_equity_crits(notifier) == []
    assert report.tripped is False


# ── 3a'. 倍率鎖定（plan 2026-11-22 T2b／D7）──────────────────────────
# 連跑多輪、共用同一個 ReconcileState／executor／adapter；每輪之間直接改 adapter 的
# 內部槽來模擬行情與帳戶漂移。錨例：leader −0.02 BTC、total 700、follower 4704.22、
# 使用率 0.5 → scale＝2352.11÷700＝3.36016、目標 −0.0672 BTC。
_LATCH_TARGET = "-0.06720"


class _LatchWorld:
    def __init__(self, tmp_path, monkeypatch, *, total="700", leader_szi="-0.02",
                 my_av="4704.22", util="0.5", my_szi=None, leader_orders=()):
        monkeypatch.setattr(loop_mod.time, "time", lambda: float(_NOW_S))
        self.tmp_path = tmp_path
        self.fa = _SplitOrdersAdapter(
            leader_positions=[_btc_pos(leader_szi)],
            my_positions=[_btc_pos(my_szi)] if my_szi else [],
            orders_by_addr={LEADER: list(leader_orders)},
            account_value=my_av,
            account_states={LEADER: _account("120"), MY_ADDR: _account(my_av)},
            account_totals={LEADER: AccountTotal(Decimal(total), _NOW_MS)},
            mids={"BTC": Decimal("100000")}, sz_decimals={"BTC": 5})
        self.ex = _executor(self.fa)
        self.state = ReconcileState()
        self.notifier = RecordingNotifier()
        self.settings = _settings(capital_utilization=Decimal(util))

    def set_total(self, total, *, age_ms=0):
        self.fa._account_totals[LEADER.lower()] = AccountTotal(
            Decimal(total), _NOW_MS - age_ms)

    def set_leader_szi(self, szi):
        self.fa._pos_by_addr[LEADER.lower()] = [_btc_pos(szi)]

    def set_my_szi(self, szi):
        self.fa._pos_by_addr[MY_ADDR.lower()] = [_btc_pos(szi)] if szi else []

    def set_my_av(self, av):
        self.fa._account_states[MY_ADDR.lower()] = _account(av)

    def cycle(self):
        n = len(self.ex.records)
        report = run_cycle(self.fa, self.ex, self.settings, self.notifier,
                           self.state, self.tmp_path)
        return report, self.ex.records[n:]


def _expected_scale(total, *, my_av="4704.22", util="0.5"):
    return Decimal(my_av) * Decimal(util) / Decimal(total)


def test_latch_denominator_drift_alone_changes_nothing(tmp_path, monkeypatch):
    """T2b #1：第 1 輪建倉；第 2 輪 leader 完全不變、total 700→560（−20%，遠超 8% 容差）、
    follower 倉位＝第 1 輪目標 → 0 筆動作、scale 與第 1 輪相同。"""
    w = _LatchWorld(tmp_path, monkeypatch)
    r1, recs1 = w.cycle()
    assert abs(r1.scale - _expected_scale("700")) < Decimal("0.00001")
    assert [r.kind for r in recs1 if r.kind == "market_open"] == ["market_open"]
    w.set_my_szi(_LATCH_TARGET)

    _r_ctl, ctl = w.cycle()  # 對照：沒漂移時本來就 0 筆（排除「基線本身就有動作」）
    assert ctl == []

    w.set_total("560")
    r2, recs2 = w.cycle()
    assert recs2 == [], [(r.kind, r.coin) for r in recs2]
    assert r2.scale == r1.scale


def test_latch_leader_action_recomputes_with_new_denominator(tmp_path, monkeypatch):
    """T2b #2：承上，第 3 輪 leader 倉位變 −0.03、total 560 → scale 以 560 重算，
    follower 目標依新倍率（−0.03×4.2002＝−0.1260；現有 −0.0672 → 加空約 0.0588）。"""
    w = _LatchWorld(tmp_path, monkeypatch)
    r1, _ = w.cycle()
    w.set_my_szi(_LATCH_TARGET)
    w.set_total("560")
    r2, recs2 = w.cycle()
    assert recs2 == [] and r2.scale == r1.scale

    w.set_leader_szi("-0.03")
    r3, recs3 = w.cycle()
    assert abs(r3.scale - _expected_scale("560")) < Decimal("0.00001")
    assert r3.scale != r1.scale
    opens = [r for r in recs3 if r.kind == "market_open" and r.coin == "BTC"]
    assert len(opens) == 1 and opens[0].payload["is_buy"] is False
    assert abs(Decimal(opens[0].payload["sz"]) - Decimal("0.0588")) < Decimal("0.0002")


def test_latch_follower_settings_change_recomputes(tmp_path, monkeypatch):
    """T2b #3：leader 不變、使用率 0.5→0.25 → 當輪重算（目標減半 → 有減倉動作）。"""
    w = _LatchWorld(tmp_path, monkeypatch)
    r1, _ = w.cycle()
    w.set_my_szi(_LATCH_TARGET)
    w.settings = _settings(capital_utilization=Decimal("0.25"))
    r2, recs2 = w.cycle()
    assert abs(r2.scale - _expected_scale("700", util="0.25")) < Decimal("0.00001")
    assert r2.scale != r1.scale
    assert recs2, "設定變了必須反映到倉位"


def test_latch_follower_without_exposure_recomputes(tmp_path, monkeypatch):
    """T2b #4：leader 不變、follower 無倉位無掛單 → 當輪用新值（total 700→560）。"""
    w = _LatchWorld(tmp_path, monkeypatch)
    r1, _ = w.cycle()
    w.set_total("560")  # follower 仍無倉位（dry run 沒有真的成交）
    r2, recs2 = w.cycle()
    assert abs(r2.scale - _expected_scale("560")) < Decimal("0.00001")
    assert r2.scale != r1.scale
    opens = [r for r in recs2 if r.kind == "market_open"]
    assert len(opens) == 1
    assert abs(Decimal(opens[0].payload["sz"]) - Decimal("0.0840")) < Decimal("0.0002")


def test_latch_numerator_drift_alone_changes_nothing(tmp_path, monkeypatch):
    """T2b #5：leader 不變、follower account_value +30% → 0 筆動作、scale 不變。"""
    w = _LatchWorld(tmp_path, monkeypatch)
    r1, _ = w.cycle()
    w.set_my_szi(_LATCH_TARGET)
    w.set_my_av("6115.49")  # 4704.22 × 1.3
    r2, recs2 = w.cycle()
    assert recs2 == [], [(r.kind, r.coin) for r in recs2]
    assert r2.scale == r1.scale


def test_latch_survives_a_leader_equity_unavailable_cycle(tmp_path, monkeypatch):
    """T2b #6：第 2 輪 total 過期（leader 有倉位 → unavailable、零動作）；鎖定值保留，
    第 3 輪恢復、leader 不變、total 已漂移 → 仍用舊倍率、0 筆動作。"""
    w = _LatchWorld(tmp_path, monkeypatch)
    r1, _ = w.cycle()
    w.set_my_szi(_LATCH_TARGET)
    latch_before = w.state.scale_latch
    assert latch_before is not None

    w.set_total("560", age_ms=181_000)
    r2, recs2 = w.cycle()
    assert r2.skip_detail and recs2 == []
    assert w.state.scale_latch is latch_before

    w.set_total("560")
    r3, recs3 = w.cycle()
    assert recs3 == [], [(r.kind, r.coin) for r in recs3]
    assert r3.scale == r1.scale


def test_latch_leader_order_change_recomputes(tmp_path, monkeypatch):
    """leader 只多一張掛單（倉位沒變）也算「有動作」→ 重算。"""
    w = _LatchWorld(tmp_path, monkeypatch)
    r1, _ = w.cycle()
    w.set_my_szi(_LATCH_TARGET)
    w.set_total("560")
    r2, _ = w.cycle()
    assert r2.scale == r1.scale
    w.fa._orders_by_addr[LEADER.lower()] = [OpenOrder(
        oid=5, coin="ETH", is_buy=True, limit_px=Decimal("2000"), sz=Decimal("1"),
        reduce_only=False, is_trigger=False, trigger_px=None, tpsl=None)]
    w.fa._mids["ETH"] = Decimal("2000")
    w.fa._sz_decimals["ETH"] = 4
    r3, _ = w.cycle()
    assert abs(r3.scale - _expected_scale("560")) < Decimal("0.00001")


def test_latch_not_kept_when_leader_goes_flat(tmp_path, monkeypatch):
    """leader 清倉清單（指紋為空）→ 不鎖；scale 照新值（無倉位時目標全 0，無事可鎖）。"""
    w = _LatchWorld(tmp_path, monkeypatch)
    w.cycle()
    assert w.state.scale_latch is not None
    w.fa._pos_by_addr[LEADER.lower()] = []
    w.set_my_szi(_LATCH_TARGET)
    w.set_total("560")
    w.cycle()
    assert w.state.scale_latch is None


def test_flow_neutralization_raw_is_exchange_total_not_perp_av(tmp_path, monkeypatch):
    """流量中性化的 raw 與倍率分母同源：leader perp av 400、total 2000、入金 +1000 →
    adjusted＝1000 → scale＝2（若 raw 誤用 perp av＝400 會得到 −600 → 退回 raw → scale 5）。"""
    from spark.exchange.base import LedgerFlow
    seen = {}
    real_adj = loop_mod.adjusted_leader_equity

    def spy(raw, flows, now_ms, decay_ms):
        seen["raw"] = raw
        return real_adj(raw, flows, now_ms, decay_ms)

    monkeypatch.setattr(loop_mod, "adjusted_leader_equity", spy)
    monkeypatch.setattr(loop_mod.time, "time", lambda: float(_NOW_S))
    fa = FakeAdapter(
        account_value="2000",
        account_states={LEADER: _account("400"), MY_ADDR: _account("2000")},
        account_totals={LEADER: AccountTotal(Decimal("2000"), _NOW_MS)},
        ledger_flows=([LedgerFlow(time_ms=_NOW_MS, usdc=Decimal("1000"))], []))
    report, _n, _ex = _run(fa, settings=_settings(leader_flow_neutralization_enabled=True),
                           tmp_path=tmp_path)
    assert seen["raw"] == Decimal("2000")
    assert report.scale == Decimal("2")


def test_volatility_weight_receives_exchange_total_as_equity(tmp_path, monkeypatch):
    """波動權重的 leader 權益參數同源（第 4 節三處之一）。"""
    seen = {}
    real = loop_mod.compute_volatility_stats

    def spy(daily, equity):
        seen["equity"] = equity
        return real(daily, equity)

    monkeypatch.setattr(loop_mod, "compute_volatility_stats", spy)
    monkeypatch.setattr(loop_mod.time, "time", lambda: float(_NOW_S))
    fa = FakeAdapter(
        account_value="1000", daily_abs_pnl=[Decimal("10")] * 5,
        account_states={LEADER: _account("120.58"), MY_ADDR: _account("1000")},
        account_totals={LEADER: AccountTotal(Decimal("707.37"), _NOW_MS)})
    _run(fa, settings=_settings(volatility_weight_enabled=True), tmp_path=tmp_path)
    assert seen["equity"] == Decimal("707.37")


# ── 3b. leader 申贖流量中性化接線（Wave 4 規格 C）─────────────────────
# 時間源與 run_cycle 一致（time.time）；測試釘死 time.time 讓 age 可控。
_FLOW_NOW_S = 1_753_920_000
_FLOW_NOW_MS = _FLOW_NOW_S * 1000
_FLOW_DECAY_MS = 129_600_000  # 36h


def _pin_clock(monkeypatch):
    monkeypatch.setattr(loop_mod.time, "time", lambda: float(_FLOW_NOW_S))


def test_flow_neutralization_scales_off_adjusted_denominator(tmp_path, monkeypatch):
    """兩組相同現場只差 flows：raw=2000、my=2000、入金 +1000 @ age 0
    → adjusted=1000 → scale=2；無 flows → scale=1。"""
    from spark.exchange.base import LedgerFlow
    _pin_clock(monkeypatch)
    settings = _settings(leader_flow_neutralization_enabled=True)

    fa = FakeAdapter(equity=_healthy_equity(), account=_account("2000"),
                     ledger_flows=([LedgerFlow(time_ms=_FLOW_NOW_MS,
                                               usdc=Decimal("1000"))], []))
    report, notifier, _ = _run(fa, settings=settings, tmp_path=tmp_path)
    assert report.scale == Decimal("2")
    # 取數窗 = now − decay（36h），地址 = leader
    assert fa.calls["get_ledger_flows"] == [
        {"address": LEADER, "start_ms": _FLOW_NOW_MS - _FLOW_DECAY_MS}]
    assert not any(r[1] == "leader_flow" for r in notifier.records)

    fa2 = FakeAdapter(equity=_healthy_equity(), account=_account("2000"),
                      ledger_flows=([], []))
    report2, _, _ = _run(fa2, settings=settings, tmp_path=tmp_path)
    assert report2.scale == Decimal("1")


def test_flow_neutralization_disabled_never_fetches_ledger(tmp_path):
    """enabled=False（預設）⇒ get_ledger_flows 完全不被呼叫（零行為變動證明）。"""
    fa = FakeAdapter(equity=_healthy_equity(), account=_account("1000"),
                     ledger_flows=([], ["sentinel_should_not_be_read"]))
    report, notifier, _ = _run(fa, tmp_path=tmp_path)
    assert fa.calls["get_ledger_flows"] == []
    assert report.scale == Decimal("1")
    assert not any(r[1] == "leader_flow" for r in notifier.records)


def test_flow_fetch_exception_falls_back_to_raw_with_warn(tmp_path, monkeypatch):
    """取數失敗 → 本輪退回未中性化分母（raw）＋ warn；該輪照常完成。"""
    _pin_clock(monkeypatch)

    class _BoomAdapter(FakeAdapter):
        def get_ledger_flows(self, address, start_ms, *, basis="perp"):
            raise RuntimeError("ledger api down")

    fa = _BoomAdapter(equity=_healthy_equity(), account=_account("1000"))
    report, notifier, _ = _run(
        fa, settings=_settings(leader_flow_neutralization_enabled=True),
        tmp_path=tmp_path)
    assert report.tripped is False
    assert report.scale == Decimal("1")  # raw 分母（my=leader=1000）
    warns = [r for r in notifier.records if r[0] == "warn"]
    assert any("RuntimeError" in r[2] and "本輪退回未中性化分母" in r[2]
               for r in warns)


def test_flow_adjusted_nonpositive_uses_raw_with_critical(tmp_path, monkeypatch):
    """adjusted ≤ 0 < raw：幻影歸零防護——計算產物不得觸發 scale=0 的全平語意
    （工程原則：讀不到錢 ≠ 錢虧光）→ critical ＋ 本輪用 raw。"""
    from spark.exchange.base import LedgerFlow
    _pin_clock(monkeypatch)
    fa = FakeAdapter(equity=_healthy_equity(), account=_account("1000"),
                     ledger_flows=([LedgerFlow(time_ms=_FLOW_NOW_MS,
                                               usdc=Decimal("5000"))], []))
    report, notifier, _ = _run(
        fa, settings=_settings(leader_flow_neutralization_enabled=True),
        tmp_path=tmp_path)
    assert report.tripped is False
    assert report.scale == Decimal("1")  # raw，而非 adjusted(-4000) 的 scale=0
    crits = [r for r in notifier.records
             if r[0] == "critical" and r[1] == "leader_flow"]
    assert len(crits) == 1
    assert crits[0][3] == "leader_flow_nonpositive"  # dedup_key（TG TTL 去重靠它）


def test_flow_unknown_ledger_types_warn_with_type_names(tmp_path, monkeypatch):
    """白名單外 ledger 型別 → warn 含型別名（恆等式基礎可能不成立）。"""
    _pin_clock(monkeypatch)
    fa = FakeAdapter(equity=_healthy_equity(), account=_account("1000"),
                     ledger_flows=([], ["accountClassTransfer", "spotTransfer"]))
    report, notifier, _ = _run(
        fa, settings=_settings(leader_flow_neutralization_enabled=True),
        tmp_path=tmp_path)
    assert report.scale == Decimal("1")  # flows 空 → adjusted == raw
    warns = [r for r in notifier.records if r[0] == "warn"]
    assert any("accountClassTransfer" in r[2] and "spotTransfer" in r[2]
               for r in warns)


def test_flow_interpret_failure_falls_back_to_raw_with_warn(tmp_path, monkeypatch):
    """回歸：解讀段（join／adjusted／guard）曾裸奔在 try 之外——一筆髒 ledger
    資料（unknown 含 None → join TypeError）會逃出 run_cycle，且同筆資料在 36h
    窗內每輪都在 → 連續錯誤 → main_loop SystemExit 停機。
    修法：解讀段自己的 try/except → warn ＋ 本輪退回 raw。"""
    _pin_clock(monkeypatch)

    class _DirtyLedgerAdapter(FakeAdapter):
        def get_ledger_flows(self, address, start_ms, *, basis="perp"):
            return [], [None]  # 髒資料：join(unknown) 會 TypeError

    fa = _DirtyLedgerAdapter(equity=_healthy_equity(), account=_account("1000"))
    report, notifier, _ = _run(
        fa, settings=_settings(leader_flow_neutralization_enabled=True),
        tmp_path=tmp_path)
    assert report.tripped is False               # run_cycle 正常完成，未逃出例外
    assert report.scale == Decimal("1")          # raw 分母（my=leader=1000）
    warns = [r for r in notifier.records if r[0] == "warn"]
    assert any("本輪退回未中性化分母" in r[2]
               and r[3] == "leader_flow_interpret_failed" for r in warns)


# ── 3c. follower 出入金校正接線（Wave 5）──────────────────────────────
# 校正必須發生在 perp_equity_view 取樣**之前**（樣本先平移、再追加本輪 current），
# 且 risk_controls_enabled=False 也要跑（樣本照常累積的同一理由）。
# ⚠️ 樣本時間戳用**真實** time.time：_pin_clock 只 patch loop_mod.time，
# perp_equity_view 用 equity 模組自己的 time.time——用釘死的假戳會讓樣本
# 因超齡出窗被丟棄（apply 的流量窗運算則走 loop 的釘死時鐘，兩者互不相干）。
def _seed_follower_flow_state(root, *, last_ms, samples, peak):
    import json
    import time
    from spark.copytrade.equity import LIFETIME_PEAK_RELPATH, SAMPLES_RELPATH
    from spark.copytrade.follower_flow import STATE_RELPATH
    now = time.time()
    sp = root / SAMPLES_RELPATH
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps([[now - age_s, v] for age_s, v in samples]))
    (root / LIFETIME_PEAK_RELPATH).write_text(json.dumps(peak))
    (root / STATE_RELPATH).write_text(json.dumps({"last_processed_ms": last_ms}))


def _follower_flow_files(root):
    import json
    from spark.copytrade.equity import LIFETIME_PEAK_RELPATH, SAMPLES_RELPATH
    vals = [v for _, v in json.loads((root / SAMPLES_RELPATH).read_text())]
    peak = json.loads((root / LIFETIME_PEAK_RELPATH).read_text())
    return vals, peak


def _follower_flow_adapter(flows, *, value="900"):
    return FakeAdapter(account_value=value, equity=_healthy_equity(),
                       account=_account(value), ledger_flows=(flows, []))


def test_follower_flow_correction_corrects_baseline_in_cycle(tmp_path, monkeypatch):
    """run_cycle 接線：出金 300 → 樣本與 lifetime peak 皆平移 -300，
    perp_equity_view 於**校正後**追加 current 樣本 → 出金不被誤判為回撤。

    2026-08-01 起 _shift_samples 是 ts-aware 的（第三批審查 F2）：流量時間戳
    必須**晚於**既有樣本才會平移它們（真實時序——樣本是前幾輪寫的、流量是本輪
    縫隙才發生的）。故本組改釘**真實 now**，不用 _FLOW_NOW_S（那會讓流量比
    真實時間戳的樣本早一年、一筆都平移不到）。"""
    import time as _time

    from spark.exchange.base import LedgerFlow
    now_s = int(_time.time())
    last_ms = now_s * 1000 - 60_000
    _seed_follower_flow_state(          # 先播種再釘時鐘：helper 的樣本戳要真實時間
        tmp_path, last_ms=last_ms,
        samples=[(600, "1000"), (300, "1200")],
        peak="1200")
    monkeypatch.setattr(loop_mod.time, "time", lambda: float(now_s))
    fa = _follower_flow_adapter(
        [LedgerFlow(time_ms=now_s * 1000 - 30_000, usdc=Decimal("-300"))])
    report, notifier, _ = _run(fa, tmp_path=tmp_path)

    assert report.tripped is False
    assert fa.calls["get_ledger_flows"] == [
        {"address": MY_ADDR, "start_ms": last_ms + 1}]
    vals, peak = _follower_flow_files(tmp_path)
    assert vals[:2] == ["700", "900"]  # 既有樣本平移 -300
    assert vals[2] == "900"            # 校正後追加的本輪 current
    assert peak == "900"               # 1200-300；update_lifetime_peak(900) 不再推高


def test_follower_flow_correction_runs_even_with_risk_controls_disabled(
        tmp_path, monkeypatch):
    """risk_controls_enabled=False 仍校正——與「樣本照常累積」同一理由：
    客戶哪天開啟風控，基準必須已經是對的。
    （時鐘釘真實 now：ts-aware 平移的時序要求，同上一個測試的 docstring。）"""
    import time as _time

    from spark.exchange.base import LedgerFlow
    now_s = int(_time.time())
    _seed_follower_flow_state(          # 先播種再釘時鐘（同上）
        tmp_path, last_ms=now_s * 1000 - 60_000,
        samples=[(600, "1000"), (300, "1200")],
        peak="1200")
    monkeypatch.setattr(loop_mod.time, "time", lambda: float(now_s))
    fa = _follower_flow_adapter(
        [LedgerFlow(time_ms=now_s * 1000 - 30_000, usdc=Decimal("-300"))])
    report, _, _ = _run(fa, settings=_settings(risk_controls_enabled=False),
                        tmp_path=tmp_path)

    assert report.tripped is False
    vals, peak = _follower_flow_files(tmp_path)
    assert vals[:2] == ["700", "900"]
    assert peak == "900"


def test_follower_flow_escape_valve_skips_fetch_and_adjustment(tmp_path, monkeypatch):
    """逃生閥 follower_flow_correction_enabled=False ⇒ get_ledger_flows 零呼叫、
    檔案零平移（回到今天「出金視為虧損」的 fail-safe 行為）。"""
    from spark.exchange.base import LedgerFlow
    _seed_follower_flow_state(          # 先播種再釘時鐘（同上）
        tmp_path, last_ms=_FLOW_NOW_MS - 60_000,
        samples=[(600, "1000"), (300, "1200")],
        peak="1200")
    _pin_clock(monkeypatch)
    fa = _follower_flow_adapter(
        [LedgerFlow(time_ms=_FLOW_NOW_MS - 30_000, usdc=Decimal("-300"))])
    report, _, _ = _run(
        fa, settings=_settings(follower_flow_correction_enabled=False),
        tmp_path=tmp_path)

    assert report.tripped is False
    assert fa.calls["get_ledger_flows"] == []
    vals, peak = _follower_flow_files(tmp_path)
    assert vals[:2] == ["1000", "1200"]  # 零平移；vals[2] 為本輪追加的 current
    assert peak == "1200"


# ── 3d. breach 二次確認（Wave 3：幻影回撤窗口）────────────────────────
# 出金若恰落在步驟 1.5 校正之後、取樣之前的縫隙，該輪以未校正基準判回撤。
# 判定破線後、critical/flatten 之前重呼校正＋重判：假破線攔下、真虧損放行。
# ⚠️ 這組不用 _pin_clock：二次確認需要 now_ms 在兩次 apply 之間嚴格前進
# （釘死時鐘下 marker 已推進到 now_ms，第二窗 (last, now] 恆空，流量永遠看不到），
# 改用每次呼叫 +1s 的遞增時鐘。樣本戳仍走 equity 模組的真實 time.time（同 3c 註記）。


def _ticking_clock(monkeypatch, start_s=_FLOW_NOW_S, step_s=1.0):
    state = {"now": float(start_s)}

    def _tick():
        state["now"] += step_s
        return state["now"]

    monkeypatch.setattr(loop_mod.time, "time", _tick)


class _TwoPhaseLedgerAdapter(FakeAdapter):
    """get_ledger_flows 序列回覆（fakes.py 不支援 side-effect 序列，依計畫在測試
    subclass）：第一次回空（步驟 1.5 查無流量），之後回吐指定 net 的單筆流量，
    時間戳＝reveal_time_ms。2026-08-01 起 _shift_samples 是 ts-aware 的，流量
    時間戳必須落在「歷史樣本之後、步驟 2 追加的樣本之前」（縫隙的真實時序）——
    測試用 `int((time.time() - 100) * 1000)`（於上假時鐘**之前**取真實時間，
    介於 helper 播種的 -600s/-300s 歷史樣本與 step-2 append 的 now 之間），
    同時仍 > 步驟 1.5 寫下的標記（釘死時鐘的 _FLOW_NOW_MS 遠小於真實時間）。"""

    def __init__(self, *a, reveal_usdc, reveal_time_ms, **kw):
        super().__init__(*a, **kw)
        self._reveal_usdc = reveal_usdc
        self._reveal_time_ms = reveal_time_ms

    def get_ledger_flows(self, address, start_ms, *, basis="perp"):
        from spark.exchange.base import LedgerFlow
        self.calls["get_ledger_flows"].append({"address": address, "start_ms": start_ms})
        if len(self.calls["get_ledger_flows"]) == 1:
            return [], []
        return [LedgerFlow(time_ms=self._reveal_time_ms, usdc=self._reveal_usdc)], []


def _gap_flow_ms() -> int:
    """縫隙流量的時間戳：真實 now − 100s（呼叫時機必須在上假時鐘之前）。"""
    import time
    return int((time.time() - 100) * 1000)


class _RecheckBoomAdapter(FakeAdapter):
    """第一次回空、第二次（二次確認）起拋例外——模擬縫隙後 ledger API 掛掉。"""

    def get_ledger_flows(self, address, start_ms, *, basis="perp"):
        self.calls["get_ledger_flows"].append({"address": address, "start_ms": start_ms})
        if len(self.calls["get_ledger_flows"]) == 1:
            return [], []
        raise RuntimeError("ledger api down at recheck")


def _trip_recorder(monkeypatch):
    calls = []
    monkeypatch.setattr(loop_mod, "trip",
                        lambda ex, pos, notifier, root, status, reason="": calls.append(
                            (status, reason)))
    return calls


def test_breach_recheck_averts_phantom_drawdown_from_withdrawal(tmp_path, monkeypatch):
    """幻影攔截（既有錨例，攔截語意不變）：步驟 1.5 查無流量、AV 已扣掉出金 300
    （peak 1000 → current 700，dd=0.3 假破線）→ 二次確認吐出該筆出金 → 基準校正
    → 不 trip、無 dd_breach critical、收 averted warn、本輪照常續行。

    2026-08-01 F1/F2 修法後的檔面差異（語意不變、機制修正）：
    - step-2 追加的樣本（700）已含出金，**不再**被平移——舊斷言裡的 "400"
      正是 F2 雙重平移值；
    - 重判改走 recompute_view，不再 append 第四筆樣本（F1 修法）。"""
    _seed_follower_flow_state(          # 先播種再上假時鐘（helper 樣本戳要真實時間）
        tmp_path, last_ms=_FLOW_NOW_MS - 60_000,
        samples=[(600, "1000"), (300, "1000")],
        peak="1000")
    reveal_ms = _gap_flow_ms()          # 真實時間，必須在上假時鐘之前取
    _ticking_clock(monkeypatch)
    trips = _trip_recorder(monkeypatch)
    fa = _TwoPhaseLedgerAdapter(account_value="700", equity=_healthy_equity(),
                                account=_account("700"),
                                reveal_usdc=Decimal("-300"),
                                reveal_time_ms=reveal_ms)
    report, notifier, _ = _run(fa, tmp_path=tmp_path)

    assert trips == [], "假破線經二次校正後不得 trip"
    assert report.tripped is False
    assert not any(r[0] == "critical" and r[3] == "dd_breach"
                   for r in notifier.records), "false alarm 的 dd_breach critical 要攔"
    warns = [r for r in notifier.records if r[0] == "warn"]
    assert any(r[3] == "dd_breach_averted" for r in warns)
    assert len(fa.calls["get_ledger_flows"]) == 2  # 步驟 1.5 ＋ 二次確認各一次
    # 樣本/peak 檔已校正：歷史樣本（早於流量）平移 -300；step-2 樣本（晚於流量、
    # 已含出金）不動；重判不 append。
    vals, peak = _follower_flow_files(tmp_path)
    assert vals == ["700", "700", "700"]
    assert peak == "700"
    assert fa.calls["get_open_orders"], "averted 後本輪必須照常續行（掛單同步照跑）"


def test_breach_recheck_real_loss_still_trips(tmp_path, monkeypatch):
    """真虧損放行：二次確認回空流量 → 基準不變 → 原判照走（critical＋trip），
    語意與 test_breach_with_flatten_calls_trip 一致、不得弱化。"""
    _seed_follower_flow_state(
        tmp_path, last_ms=_FLOW_NOW_MS - 60_000,
        samples=[(600, "1000"), (300, "1000")],
        peak="1000")
    _ticking_clock(monkeypatch)
    trips = _trip_recorder(monkeypatch)
    fa = _follower_flow_adapter([], value="700")  # ledger 恆空：兩次呼叫皆無流量
    report, notifier, _ = _run(fa, tmp_path=tmp_path)

    assert report.tripped is True
    assert len(trips) == 1
    status, reason = trips[0]
    assert status.breached is True and status.drawdown_pct == Decimal("0.3")
    assert reason == "drawdown"
    assert any(r[0] == "critical" and r[3] == "dd_breach" for r in notifier.records)
    assert not any(r[3] == "dd_breach_averted" for r in notifier.records)
    assert len(fa.calls["get_ledger_flows"]) == 2  # 二次確認真的查了、查無流量


def test_breach_recheck_fetch_failure_fails_closed_to_trip(tmp_path, monkeypatch):
    """取數失敗 fail-closed：二次確認 get_ledger_flows 拋例外 → apply 內部吞成
    warn、基準未變 → trip 照常。二次確認的任何失敗都不得阻止熔斷。"""
    _seed_follower_flow_state(
        tmp_path, last_ms=_FLOW_NOW_MS - 60_000,
        samples=[(600, "1000"), (300, "1000")],
        peak="1000")
    _ticking_clock(monkeypatch)
    trips = _trip_recorder(monkeypatch)
    fa = _RecheckBoomAdapter(account_value="700", equity=_healthy_equity(),
                             account=_account("700"))
    report, notifier, _ = _run(fa, tmp_path=tmp_path)

    assert report.tripped is True
    assert len(trips) == 1
    assert trips[0][0].drawdown_pct == Decimal("0.3")
    assert any(r[0] == "critical" and r[3] == "dd_breach" for r in notifier.records)
    assert any(r[0] == "warn" and r[3] == "follower_flow_fetch_failed"
               for r in notifier.records)
    assert len(fa.calls["get_ledger_flows"]) == 2


def test_breach_recheck_escape_valve_skips_second_fetch(tmp_path, monkeypatch):
    """逃生閥：follower_flow_correction_enabled=False ⇒ 無二次確認
    （get_ledger_flows 零呼叫），breach 走既有路徑（critical＋trip）。"""
    _seed_follower_flow_state(
        tmp_path, last_ms=_FLOW_NOW_MS - 60_000,
        samples=[(600, "1000"), (300, "1000")],
        peak="1000")
    trips = _trip_recorder(monkeypatch)
    fa = _follower_flow_adapter([], value="700")
    report, notifier, _ = _run(
        fa, settings=_settings(follower_flow_correction_enabled=False),
        tmp_path=tmp_path)

    assert report.tripped is True
    assert len(trips) == 1
    assert fa.calls["get_ledger_flows"] == []  # 步驟 1.5 與二次確認都沒跑
    assert any(r[0] == "critical" and r[3] == "dd_breach" for r in notifier.records)


def test_breach_recheck_averts_phantom_lifetime_breach(tmp_path, monkeypatch):
    """lifetime basis 同樣被二次確認覆蓋：慢跌後（窗內 860、lifetime peak 1300）
    縫隙出金 160 → AV 700 → rolling dd=160/860≈0.186 未破、total dd=600/1300
    ≈0.4615 破 0.40 假破線 → 二次確認校正（peak→1140、total dd≈0.386）→ 攔截。
    告警面殘留：evaluate 內建的 equity_total_drawdown critical 在一次判定時已發出，
    二次確認救不回（不在本任務範圍）；它不觸發任何動作且吃 TTL 去重，可接受。
    檔面斷言同 phantom 錨例的 2026-08-01 修正：step-2 樣本不平移、重判不 append。"""
    _seed_follower_flow_state(
        tmp_path, last_ms=_FLOW_NOW_MS - 60_000,
        samples=[(600, "860"), (300, "860")],
        peak="1300")
    reveal_ms = _gap_flow_ms()
    _ticking_clock(monkeypatch)
    trips = _trip_recorder(monkeypatch)
    fa = _TwoPhaseLedgerAdapter(account_value="700", equity=_healthy_equity(),
                                account=_account("700"),
                                reveal_usdc=Decimal("-160"),
                                reveal_time_ms=reveal_ms)
    report, notifier, _ = _run(fa, tmp_path=tmp_path)

    assert trips == [], "lifetime 假破線經二次校正後不得 trip"
    assert report.tripped is False
    # 一次判定確實是 lifetime 閘在破（證明本測試測的是 lifetime basis）
    assert any(r[0] == "critical" and r[3] == "equity_total_drawdown"
               for r in notifier.records)
    assert not any(r[0] == "critical" and r[3] == "dd_breach"
                   for r in notifier.records)
    assert any(r[0] == "warn" and r[3] == "dd_breach_averted"
               for r in notifier.records)
    vals, peak = _follower_flow_files(tmp_path)
    assert vals == ["700", "700", "700"]
    assert peak == "1140"


def test_breach_recheck_deposit_in_gap_must_not_wash_out_real_breach(
        tmp_path, monkeypatch):
    """⭐ 第三批審查 F1（fail-open）迴歸：樣本檔只有 **1 筆歷史**（trip 後 reset
    重建的頭幾輪——正是二次回撤最可能的時候）＋縫隙**入金** 100、真虧損
    current=700 → 校正後 peak=1100、dd=400/1100≈36.4% 仍破線 → **trip 必須
    被呼叫**。

    修復前的 fail-open 機轉：重判走 perp_equity_view 再 append 一筆 current，
    樣本數 2→3 跨過 _WICK_GUARD_MIN_SAMPLES，peak 從最高值降級成次高值 →
    真破線被 averted 洗掉。"""
    _seed_follower_flow_state(
        tmp_path, last_ms=_FLOW_NOW_MS - 60_000,
        samples=[(600, "1000")], peak="1000")
    reveal_ms = _gap_flow_ms()
    _ticking_clock(monkeypatch)
    trips = _trip_recorder(monkeypatch)
    fa = _TwoPhaseLedgerAdapter(account_value="700", equity=_healthy_equity(),
                                account=_account("700"),
                                reveal_usdc=Decimal("100"),
                                reveal_time_ms=reveal_ms)
    report, notifier, _ = _run(fa, tmp_path=tmp_path)

    assert len(trips) == 1, "真破線不得被重判的 +1 樣本洗掉（F1 fail-open）"
    status, reason = trips[0]
    assert status.breached is True and reason == "drawdown"
    assert status.peak == Decimal("1100")  # 歷史樣本 1000 平移 +100
    assert status.drawdown_pct == Decimal("400") / Decimal("1100")  # ≈36.4%
    assert report.tripped is True
    assert any(r[0] == "critical" and r[3] == "dd_breach" for r in notifier.records)
    assert not any(r[3] == "dd_breach_averted" for r in notifier.records)
    vals, _pk = _follower_flow_files(tmp_path)
    assert vals == ["1100", "700"]  # hist 平移 +100；step-2 樣本不動；重判不 append


def test_breach_recheck_withdrawal_plus_real_loss_no_double_shift(
        tmp_path, monkeypatch):
    """⭐ F1/F2 迴歸：1 筆歷史 [1000]、縫隙**出金** 100、current=700（出金之外
    另有真虧損）→ hist 平移至 900、step-2 樣本（已含出金）**不動**（F2：全檔
    平移會把它再平移一次、永久偏離真值）→ dd=200/900≈22.2% 破線 → trip 照常。

    修復前：全檔平移成 [900, 600]＋重判 append 700 → 3 樣本 wick-guard 取
    次高 700 → dd=0 → averted（fail-open）。"""
    _seed_follower_flow_state(
        tmp_path, last_ms=_FLOW_NOW_MS - 60_000,
        samples=[(600, "1000")], peak="1000")
    reveal_ms = _gap_flow_ms()
    _ticking_clock(monkeypatch)
    trips = _trip_recorder(monkeypatch)
    fa = _TwoPhaseLedgerAdapter(account_value="700", equity=_healthy_equity(),
                                account=_account("700"),
                                reveal_usdc=Decimal("-100"),
                                reveal_time_ms=reveal_ms)
    report, notifier, _ = _run(fa, tmp_path=tmp_path)

    assert len(trips) == 1, "出金校正後仍破線的真虧損必須 trip"
    status, reason = trips[0]
    assert status.breached is True and reason == "drawdown"
    assert status.peak == Decimal("900")
    assert status.drawdown_pct == Decimal("200") / Decimal("900")  # ≈22.2%
    assert report.tripped is True
    vals, peak = _follower_flow_files(tmp_path)
    assert vals == ["900", "700"], "出現 600/800 之類的值＝雙重平移（F2）"
    assert peak == "900"


def test_breach_recheck_internal_error_fails_closed_with_warn(tmp_path, monkeypatch):
    """O4：二次確認內部炸掉（recompute_view 拋例外）→ warn（dedup）＋**沿用原
    status 照常進 trip 路徑**（fail-closed）——IO 例外不得在 critical/trip 之前
    炸掉本輪（炸掉＝本輪不 trip、破線期間多跑一輪交易）。"""
    _seed_follower_flow_state(
        tmp_path, last_ms=_FLOW_NOW_MS - 60_000,
        samples=[(600, "1000"), (300, "1000")], peak="1000")
    reveal_ms = _gap_flow_ms()
    _ticking_clock(monkeypatch)
    trips = _trip_recorder(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("recompute boom")

    monkeypatch.setattr(loop_mod, "recompute_view", _boom)
    fa = _TwoPhaseLedgerAdapter(account_value="700", equity=_healthy_equity(),
                                account=_account("700"),
                                reveal_usdc=Decimal("-300"),
                                reveal_time_ms=reveal_ms)
    report, notifier, _ = _run(fa, tmp_path=tmp_path)

    assert report.tripped is True
    assert len(trips) == 1
    assert trips[0][0].drawdown_pct == Decimal("0.3")  # 沿用一次判定的原 status
    assert any(r[0] == "warn" and r[3] == "dd_recheck_failed"
               for r in notifier.records)


# ── 4. leader 觸發單 → 開關關閉時由 _build_desired 略過＋warn（oid 集合去重）──
# 2026-11-21 T2：原本由 executor 的 skip_trigger 記錄經 loop 發 warn（M1 限制）；
# 現在觸發單在 _build_desired 就被略過，executor 不再被呼叫，改由 sync_open_orders 告警。
def test_leader_trigger_order_surfaces_as_skipped_trigger_warn(tmp_path):
    trigger = OpenOrder(oid=1, coin="ETH", is_buy=False, limit_px=Decimal("1900"),
                        sz=Decimal("1"), reduce_only=False, is_trigger=True,
                        trigger_px=Decimal("1900"), tpsl="sl", is_market=False)
    fa = FakeAdapter(equity=_healthy_equity(), account=_account("1000"),
                     open_orders=[trigger])
    state = ReconcileState()
    # 此測試驗「開關關閉」行為；CopySettings 預設自 T7（D15）起為開，故顯式關閉。
    report, notifier, ex = _run(fa, tmp_path=tmp_path, state=state,
                                settings=_settings(mirror_triggers=False))

    assert not any(r.kind == "skip_trigger" for r in ex.records)  # executor 不再被呼叫
    assert [r for r in ex.records if r.kind in ("place", "modify", "cancel")] == []
    assert report.reconcile.placed == 0
    warns = [r for r in notifier.records if r[0] == "warn"]
    unmirrored = [r for r in warns if "[未鏡射]" in r[2]]
    assert len(unmirrored) == 1
    assert "ETH" in unmirrored[0][2] and "觸發單鏡射未啟用" in unmirrored[0][2]
    assert state.trigger_skip_seen == {"ETH": frozenset({1})}
    # orders 類別零 CRIT（killswitch 的覆蓋不足 CRIT 是此 harness 的既有背景，與觸發單無關）
    assert [r for r in notifier.records if r[0] == "critical" and r[1] == "orders"] == []
    assert [k for k in _WRITE_CALLS if fa.calls[k]] == []  # dry 全程零 adapter 寫入


# ── 5. main_loop：minute-key 防重跑 ──────────────────────────────────
def _dt(minute: int, second: int) -> datetime:
    return datetime(2026, 7, 16, 10, minute, second)


def test_main_loop_minute_key_prevents_rerun_within_same_minute():
    cycle_calls = []
    times = iter([_dt(0, 5), _dt(0, 35), _dt(1, 5)])
    sleeps = []

    def sleep_fn(s):
        sleeps.append(s)
        if len(sleeps) >= 3:
            raise KeyboardInterrupt

    notifier = RecordingNotifier()
    main_loop(lambda: cycle_calls.append(1), _settings(), notifier,
              clock=lambda: 0.0, sleep_fn=sleep_fn, now_fn=lambda: next(times))

    assert len(cycle_calls) == 2  # 10:00 兩次醒來只跑一次；10:01 再跑一次
    assert any(r[0] == "info" for r in notifier.records)  # KeyboardInterrupt → info 後返回


def test_main_loop_sleeps_aligned_to_interval():
    # clock=90（interval 60 秒）→ 到下一個邊界剩 30 秒；邊界上（120）→ 睡滿一輪 60。
    assert _seconds_until_next_interval(90.0, 60) == 30.0
    assert _seconds_until_next_interval(120.0, 60) == 60.0
    assert _seconds_until_next_interval(119.5, 60) == 1.0  # 貼近邊界至少睡 1 秒


# ── 5b. main_loop：report.halt_engine=True → 提前正常返回（裁決 D1/D5）────
def test_main_loop_returns_when_report_has_halt_engine():
    """(c) owner_close 終態的一輪之後，main_loop 不得再排程下一輪
    （sleep_fn 計數器證明沒有第二輪）；用 info 留痕，且正常返回（不 raise）。"""
    calls = []

    def mk_cycle():
        calls.append(1)
        return CycleReport(
            reconcile=ReconcileResult(placed=0, cancelled=0, modified=0, matched=0,
                                      sync_failed=False, skipped_small=()),
            safety_net={"skipped": True}, scale=Decimal("0"), tripped=True,
            halt_engine=True,
        )

    notifier = RecordingNotifier()
    sleeps = []
    main_loop(mk_cycle, _settings(), notifier, clock=lambda: 0.0,
             sleep_fn=lambda s: sleeps.append(s), now_fn=_advancing_now())

    assert len(calls) == 1, "halt_engine=True 之後不得再跑第二輪"
    assert sleeps == [], "halt_engine 應在排程下一次 sleep 之前就返回"
    infos = [r for r in notifier.records if r[0] == "info"]
    assert any("owner_close" in r[2] for r in infos)
    assert [r for r in notifier.records if r[0] == "critical"] == []


# ── 6. main_loop：連續錯誤熔斷與成功歸零 ─────────────────────────────
def _advancing_now():
    state = {"i": 0}

    def now_fn():
        state["i"] += 1
        return datetime(2026, 7, 16, 10, 0) + timedelta(minutes=state["i"])
    return now_fn


def test_main_loop_consecutive_errors_critical_and_systemexit():
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("api down")

    notifier = RecordingNotifier()
    with pytest.raises(SystemExit) as exc:
        main_loop(boom, _settings(max_consecutive_errors=5), notifier,
                  clock=lambda: 0.0, sleep_fn=lambda s: None, now_fn=_advancing_now())
    assert exc.value.code == 1
    assert len(calls) == 5  # 第 5 次失敗即熔斷，不再跑第 6 次
    crits = [r for r in notifier.records if r[0] == "critical"]
    assert len(crits) == 1 and crits[0][1] == "loop" and "5" in crits[0][2]


def test_main_loop_success_resets_error_counter():
    outcomes = iter([Exception, Exception, None, Exception, Exception, None])
    calls = []

    def flaky():
        calls.append(1)
        kind = next(outcomes, KeyboardInterrupt)
        if kind is KeyboardInterrupt:
            raise KeyboardInterrupt
        if kind is Exception:
            raise RuntimeError("transient")

    notifier = RecordingNotifier()
    main_loop(flaky, _settings(max_consecutive_errors=3), notifier,
              clock=lambda: 0.0, sleep_fn=lambda s: None, now_fn=_advancing_now())

    assert len(calls) == 7  # 6 輪 + 第 7 輪觸發 KeyboardInterrupt 收尾
    assert [r for r in notifier.records if r[0] == "critical"] == []  # 歸零 → 從未熔斷


# ── 7. CLI：argparse 與 live 判定（單元級，不觸網）────────────────────
def test_cli_parser_flags():
    from scripts.run_copytrade import build_parser
    args = build_parser().parse_args(["--once", "--dry-run", "--shadow", "--status"])
    assert args.once and args.dry_run and args.shadow and args.status
    empty = build_parser().parse_args([])
    assert not (empty.once or empty.dry_run or empty.shadow or empty.status)


def test_cli_dry_run_forces_live_off_even_when_env_true():
    from scripts.run_copytrade import _resolve_live, build_parser
    live_settings = CopySettings(live_trading=True)
    for flags in (["--dry-run"], ["--shadow"], ["--status"], ["--dry-run", "--once"]):
        args = build_parser().parse_args(flags)
        assert _resolve_live(args, live_settings) is False, flags
    assert _resolve_live(build_parser().parse_args([]), live_settings) is True
    assert _resolve_live(build_parser().parse_args([]), CopySettings()) is False  # 預設關


def test_cli_shadow_append_jsonl_accumulates(tmp_path):
    import json

    from spark.copytrade.executor import ActionRecord

    from scripts.run_copytrade import _append_shadow
    recs = [ActionRecord(ts=1.0, kind="place", coin="ETH",
                         payload={"sz": "1.5", "ok": True})]
    day = datetime(2026, 7, 16).date()
    p1 = _append_shadow(recs, tmp_path / "shadow", day=day)
    p2 = _append_shadow(recs, tmp_path / "shadow", day=day)
    assert p1 == p2 == tmp_path / "shadow" / "20260716.jsonl"
    lines = p1.read_text().strip().splitlines()
    assert len(lines) == 2  # append 累積
    row = json.loads(lines[0])
    assert row == {"ts": 1.0, "kind": "place", "coin": "ETH",
                   "payload": {"sz": "1.5", "ok": True}}


def test_cli_without_env_prints_usage_and_exits_2():
    """subprocess 實跑：無必要 env → 用法 + exit 2；import 階段不觸網
    （若觸網會在無 mock 下炸出非用法錯誤）。"""
    env = {k: v for k, v in os.environ.items()
           if k not in ("SPARK_ACCOUNT_ID", "SPARK_USER_ADDR", "SPARK_BUILDER_ADDR")}
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.run_copytrade", "--once"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 2
    assert "用法" in proc.stdout or "用法" in proc.stderr
    assert "SPARK_USER_ADDR" in proc.stdout + proc.stderr


# ── 3d. T4-fix（plan 2026-11-22 D8／D9＋審核測試缺口）──────────────────
def _set_leader_ntl(w, ntl):
    """leader 的名目總值（`total_ntl_pos`，D8 有效槓桿的分子）直接給值。"""
    w.fa._account_states[LEADER.lower()] = _account("120", ntl)


def _cap_world(tmp_path, monkeypatch):
    """D8 錨例（審核實跑）：follower 權益 10,000、使用率 0.5、`max_target_leverage=10`；
    leader 空 1 BTC、mark 100,000（名目 100,000）、帳戶總值 8,000 → 有效槓桿 12.5、上限生效。
    第 1 輪倍率＝5,000÷8,000×(10÷12.5)＝0.5。"""
    w = _LatchWorld(tmp_path, monkeypatch, total="8000", leader_szi="-1",
                    my_av="10000", util="0.5")
    w.settings = _settings(capital_utilization=Decimal("0.5"),
                           max_target_leverage=Decimal("10"))
    _set_leader_ntl(w, "100000")
    return w


def test_D8_cap_binding_shrink_is_not_frozen_by_latch_and_never_grows_back(
        tmp_path, monkeypatch):
    """D8：leader 抱單不動（倉位掛單不變）期間，權益崩跌使有效槓桿衝破上限 → follower 仍被縮
    （當輪重算並重新鎖定）；之後權益回升、上限解除，倍率不自動加回（等 leader 動作）。"""
    w = _cap_world(tmp_path, monkeypatch)
    r1, recs1 = w.cycle()
    assert abs(r1.scale - Decimal("0.5")) < Decimal("0.00001")
    assert [r.kind for r in recs1 if r.kind == "market_open"] == ["market_open"]
    w.set_my_szi("-0.50000")

    # 第 2 輪：mark 105,000、leader 帳戶總值 3,000（有效槓桿 35）、follower 權益 7,500
    w.fa._mids["BTC"] = Decimal("105000")
    _set_leader_ntl(w, "105000")
    w.set_total("3000")
    w.set_my_av("7500")
    r2, recs2 = w.cycle()
    expected2 = Decimal("3750") / Decimal("3000") * (Decimal("10") / Decimal("35"))
    assert abs(r2.scale - expected2) < Decimal("0.0005")       # ≈ 0.3571，不是 0.5
    assert abs(r2.scale - Decimal("0.3571")) < Decimal("0.0005")
    reductions = [r for r in recs2 if r.coin == "BTC"]
    assert reductions, "縮倉方向不得被鎖定凍結：必須有一筆 BTC 減倉"
    assert w.state.scale_latch.scale == r2.scale                # 已重新鎖定
    w.set_my_szi("-0.35714")

    # 第 3 輪：帳戶總值回到 8,000、mark 100,000、其餘不變 → 倍率仍是第 2 輪的值、0 筆動作
    w.fa._mids["BTC"] = Decimal("100000")
    _set_leader_ntl(w, "100000")
    w.set_total("8000")
    r3, recs3 = w.cycle()
    assert r3.scale == r2.scale                                  # 不加回（fresh 是 0.375）
    assert recs3 == [], [(r.kind, r.coin) for r in recs3]


class _BasisAwareLedgerAdapter(FakeAdapter):
    """依 `basis` 解讀原始 ledger delta（與 HyperliquidAdapter.get_ledger_flows 同一組
    ledger_flows 判定函式），讓迴圈層測試能分辨呼叫端傳了哪個 basis。"""

    def __init__(self, *, deltas, **kw):
        super().__init__(**kw)
        self._deltas = deltas

    def get_ledger_flows(self, address, start_ms, *, basis="perp"):
        from spark.exchange.base import LedgerFlow
        from spark.exchange.ledger_flows import flow_anomaly, signed_flow
        self.calls["get_ledger_flows"].append({"address": address, "start_ms": start_ms})
        self.calls["get_ledger_flows_basis"].append(basis)
        flows, unknown = [], set()
        for time_ms, delta in self._deltas:
            anomaly = flow_anomaly(delta, address=address, basis=basis)
            if anomaly is not None:
                unknown.add(anomaly)
                continue
            v = signed_flow(delta, address=address, basis=basis)
            if v is not None and not (basis == "total" and v == 0):
                flows.append(LedgerFlow(time_ms=time_ms, usdc=v))
        return flows, sorted(unknown)


def test_D9_leader_neutralization_ignores_account_class_transfer(tmp_path, monkeypatch):
    """D9：分母是帳戶總值，spot→perp 內部劃轉（accountClassTransfer toPerp 2,000）不改變
    總值，不得被當流量從分母扣除。帳戶總值 10,000、follower 4,000、使用率 0.5 →
    scale＝2,000÷10,000＝0.2（若劃轉被扣：分母 8,000 → 0.25）。"""
    _pin_clock(monkeypatch)
    fa = _BasisAwareLedgerAdapter(
        deltas=[(_FLOW_NOW_MS, {"type": "accountClassTransfer", "usdc": "2000",
                                "toPerp": True})],
        account_value="4000",
        account_states={LEADER: _account("500"), MY_ADDR: _account("4000")},
        account_totals={LEADER: AccountTotal(Decimal("10000"), _FLOW_NOW_MS)})
    seen = {}
    real_adj = loop_mod.adjusted_leader_equity

    def spy(raw, flows, now_ms, decay_ms):
        seen["flows"] = list(flows)
        return real_adj(raw, flows, now_ms, decay_ms)

    monkeypatch.setattr(loop_mod, "adjusted_leader_equity", spy)
    report, notifier, _ex = _run(
        fa, settings=_settings(capital_utilization=Decimal("0.5"),
                               leader_flow_neutralization_enabled=True),
        tmp_path=tmp_path)
    assert fa.calls["get_ledger_flows_basis"] == ["total"]
    assert seen["flows"] == []                       # 劃轉不在流量清單
    assert report.scale == Decimal("0.2")            # 分母 10,000，不是 8,000
    assert not any(r[1] == "leader_flow" for r in notifier.records)  # 也不列 unknown


def test_W3_leader_with_only_open_order_and_stale_total_is_unavailable_not_cleanup(
        tmp_path, monkeypatch):
    """審核 Warning 3：leader **只有掛單、沒有倉位**，帳戶總值過期 → 走 unavailable
    （零動作＋critical），不是 D3a 的無曝險收尾路徑；follower 既有的掛單與倉位都沒被動。"""
    monkeypatch.setattr(loop_mod.time, "time", lambda: float(_NOW_S))
    leader_order = OpenOrder(oid=1, coin="ETH", is_buy=True, limit_px=Decimal("2000"),
                             sz=Decimal("1"), reduce_only=False, is_trigger=False,
                             trigger_px=None, tpsl=None)
    mirror_order = OpenOrder(oid=77, coin="ETH", is_buy=True, limit_px=Decimal("1900"),
                             sz=Decimal("2"), reduce_only=False, is_trigger=False,
                             trigger_px=None, tpsl=None)
    fa = _SplitOrdersAdapter(
        orders_by_addr={LEADER: [leader_order], MY_ADDR: [mirror_order]},
        my_positions=[_btc_pos("0.01")], account_value="4704.22",
        account_states={LEADER: _account("5000"), MY_ADDR: _account("4704.22")},
        account_totals={LEADER: AccountTotal(Decimal("707.37"), _NOW_MS - 181_000)},
        mids={"BTC": Decimal("100000"), "ETH": Decimal("2000")},
        sz_decimals={"BTC": 5, "ETH": 4})
    ex = _executor(fa, live=True)
    notifier = RecordingNotifier()
    report = run_cycle(fa, ex, _settings(), notifier, ReconcileState(), tmp_path)

    assert ex.records == []
    assert all(fa.calls[k] == [] for k in _WRITE_CALLS), "follower 的掛單與倉位不得被動"
    assert _leader_equity_crits(notifier) == ["leader_equity_unavailable"]
    assert report.skip_detail and "過期" in report.skip_detail
    assert report.scale == Decimal("0")


def _spot_order(oid=9, sz="1", coin="UBTC/USDC"):
    return OpenOrder(oid=oid, coin=coin, is_buy=True, limit_px=Decimal("90000"),
                     sz=Decimal(sz), reduce_only=False, is_trigger=False,
                     trigger_px=None, tpsl=None)


def test_D10_latch_ignores_leader_spot_order_added_with_total_drift(tmp_path, monkeypatch):
    """D10：leader 永續倉位掛單不變，只多了一張現貨掛單、帳戶總值 −20% → 0 筆動作、
    scale 不變（現貨單不是「leader 有動作」）。"""
    w = _LatchWorld(tmp_path, monkeypatch)
    r1, _ = w.cycle()
    w.set_my_szi(_LATCH_TARGET)
    w.fa._orders_by_addr[LEADER.lower()] = [_spot_order()]
    w.set_total("560")
    r2, recs2 = w.cycle()
    assert recs2 == [], [(r.kind, r.coin) for r in recs2]
    assert r2.scale == r1.scale
    # 現貨單的剩餘量再變、再撤掉，仍然沒有動作
    w.fa._orders_by_addr[LEADER.lower()] = [_spot_order(sz="0.3")]
    r3, recs3 = w.cycle()
    assert recs3 == [] and r3.scale == r1.scale
    w.fa._orders_by_addr[LEADER.lower()] = []
    r4, recs4 = w.cycle()
    assert recs4 == [] and r4.scale == r1.scale


def test_D10_leader_with_only_a_spot_order_and_stale_total_takes_D3a_path(
        tmp_path, monkeypatch):
    """D10：leader **只有**一張現貨掛單（無永續倉位掛單）、帳戶總值過期 → 不算有曝險：
    不發 leader_equity critical、skip_detail 為 None，follower 殘留倉位照 D3a 收尾。"""
    monkeypatch.setattr(loop_mod.time, "time", lambda: float(_NOW_S))
    fa = _SplitOrdersAdapter(
        orders_by_addr={LEADER: [_spot_order()]},
        my_positions=[_btc_pos("0.01")], account_value="4704.22",
        account_states={LEADER: _account("0"), MY_ADDR: _account("4704.22")},
        account_totals={LEADER: AccountTotal(Decimal("707.37"), _NOW_MS - 181_000)},
        mids={"BTC": Decimal("100000")}, sz_decimals={"BTC": 5})
    ex = _executor(fa, live=True)
    notifier = RecordingNotifier()
    report = run_cycle(fa, ex, _settings(), notifier, ReconcileState(), tmp_path)

    assert report.skip_detail is None
    assert _leader_equity_crits(notifier) == []
    assert ("close", "BTC") in {(r.kind, r.coin) for r in ex.records}
