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
    BuilderCode,
    EquityView,
    OpenOrder,
    Position,
)
from spark.exchange.fakes import FakeAdapter

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
    fa = FakeAdapter(account_value=Decimal("700"), account=_account("700"))
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
    fa = FakeAdapter(account_value=Decimal("700"), account=_account("700"))
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
        def get_ledger_flows(self, address, start_ms):
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
        def get_ledger_flows(self, address, start_ms):
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
    perp_equity_view 於**校正後**追加 current 樣本 → 出金不被誤判為回撤。"""
    from spark.exchange.base import LedgerFlow
    last_ms = _FLOW_NOW_MS - 60_000
    _seed_follower_flow_state(          # 先播種再釘時鐘：helper 的樣本戳要真實時間
        tmp_path, last_ms=last_ms,
        samples=[(600, "1000"), (300, "1200")],
        peak="1200")
    _pin_clock(monkeypatch)
    fa = _follower_flow_adapter(
        [LedgerFlow(time_ms=_FLOW_NOW_MS - 30_000, usdc=Decimal("-300"))])
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
    客戶哪天開啟風控，基準必須已經是對的。"""
    from spark.exchange.base import LedgerFlow
    _seed_follower_flow_state(          # 先播種再釘時鐘（同上）
        tmp_path, last_ms=_FLOW_NOW_MS - 60_000,
        samples=[(600, "1000"), (300, "1200")],
        peak="1200")
    _pin_clock(monkeypatch)
    fa = _follower_flow_adapter(
        [LedgerFlow(time_ms=_FLOW_NOW_MS - 30_000, usdc=Decimal("-300"))])
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
    時間戳＝start_ms（＝marker+1，必落在 (last, now_ms] 窗內，不依賴時鐘細節）。"""

    def __init__(self, *a, reveal_usdc, **kw):
        super().__init__(*a, **kw)
        self._reveal_usdc = reveal_usdc

    def get_ledger_flows(self, address, start_ms):
        from spark.exchange.base import LedgerFlow
        self.calls["get_ledger_flows"].append({"address": address, "start_ms": start_ms})
        if len(self.calls["get_ledger_flows"]) == 1:
            return [], []
        return [LedgerFlow(time_ms=start_ms, usdc=self._reveal_usdc)], []


class _RecheckBoomAdapter(FakeAdapter):
    """第一次回空、第二次（二次確認）起拋例外——模擬縫隙後 ledger API 掛掉。"""

    def get_ledger_flows(self, address, start_ms):
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
    """幻影攔截：步驟 1.5 查無流量、AV 已扣掉出金 300（peak 1000 → current 700，
    dd=0.3 假破線）→ 二次確認吐出該筆出金 → 基準校正 → 不 trip、無 dd_breach
    critical、收 averted warn、本輪照常續行。"""
    _seed_follower_flow_state(          # 先播種再上假時鐘（helper 樣本戳要真實時間）
        tmp_path, last_ms=_FLOW_NOW_MS - 60_000,
        samples=[(600, "1000"), (300, "1000")],
        peak="1000")
    _ticking_clock(monkeypatch)
    trips = _trip_recorder(monkeypatch)
    fa = _TwoPhaseLedgerAdapter(account_value="700", equity=_healthy_equity(),
                                account=_account("700"),
                                reveal_usdc=Decimal("-300"))
    report, notifier, _ = _run(fa, tmp_path=tmp_path)

    assert trips == [], "假破線經二次校正後不得 trip"
    assert report.tripped is False
    assert not any(r[0] == "critical" and r[3] == "dd_breach"
                   for r in notifier.records), "false alarm 連 critical 都要攔"
    warns = [r for r in notifier.records if r[0] == "warn"]
    assert any(r[3] == "dd_breach_averted" for r in warns)
    assert len(fa.calls["get_ledger_flows"]) == 2  # 步驟 1.5 ＋ 二次確認各一次
    # 樣本/peak 檔已校正：既有樣本與步驟 2 追加的 current 皆平移 -300，
    # 二次確認的 perp_equity_view 再追加校正後 current（已接受的副作用）。
    vals, peak = _follower_flow_files(tmp_path)
    assert vals == ["700", "700", "400", "700"]
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
    二次確認救不回（不在本任務範圍）；它不觸發任何動作且吃 TTL 去重，可接受。"""
    _seed_follower_flow_state(
        tmp_path, last_ms=_FLOW_NOW_MS - 60_000,
        samples=[(600, "860"), (300, "860")],
        peak="1300")
    _ticking_clock(monkeypatch)
    trips = _trip_recorder(monkeypatch)
    fa = _TwoPhaseLedgerAdapter(account_value="700", equity=_healthy_equity(),
                                account=_account("700"),
                                reveal_usdc=Decimal("-160"))
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
    assert vals == ["700", "700", "540", "700"]
    assert peak == "1140"


# ── 4. skip_trigger → warn（per-coin dedup）──────────────────────────
def test_leader_trigger_order_surfaces_as_skip_trigger_warn(tmp_path):
    trigger = OpenOrder(oid=1, coin="ETH", is_buy=False, limit_px=Decimal("1900"),
                        sz=Decimal("1"), reduce_only=False, is_trigger=True,
                        trigger_px=Decimal("1900"), tpsl="sl", is_market=False)
    fa = FakeAdapter(equity=_healthy_equity(), account=_account("1000"),
                     open_orders=[trigger])
    report, notifier, ex = _run(fa, tmp_path=tmp_path)

    assert any(r.kind == "skip_trigger" and r.coin == "ETH" for r in ex.records)
    assert report.reconcile.placed == 0  # skip 不算 placed
    warns = [r for r in notifier.records if r[0] == "warn"]
    assert any(r[3] == "skip_trigger:ETH" and "trigger" in r[2] for r in warns)
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
