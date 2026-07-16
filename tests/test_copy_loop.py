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
                        lambda ex, pos, notifier, root, status: calls.append(
                            (pos, root, status)))
    fa = FakeAdapter(
        equity=EquityView(current=Decimal("700"), recent_peak=Decimal("1000")),  # dd=0.3
        positions=[Position(coin="ETH", szi=Decimal("1"), entry_px=Decimal("2000"),
                            leverage=5, is_cross=True, unrealized_pnl=Decimal("0"),
                            margin_used=Decimal("0"))],
    )
    report, notifier, _ = _run(fa, tmp_path=tmp_path)  # 預設 max_dd=0.20、flatten 開

    assert report.tripped is True
    assert len(calls) == 1
    pos, root, status = calls[0]
    assert status.breached is True and status.drawdown_pct == Decimal("0.3")
    assert set(pos) == {"ETH"}
    assert root == tmp_path
    assert any(r[0] == "critical" and r[3] == "dd_breach" for r in notifier.records)


def test_breach_without_flatten_skips_trip_but_still_tripped_report(tmp_path, monkeypatch):
    monkeypatch.setattr(loop_mod, "trip",
                        lambda *a, **k: pytest.fail("flatten_on_breach=False 不得呼叫 trip"))
    fa = FakeAdapter(equity=EquityView(current=Decimal("700"), recent_peak=Decimal("1000")))
    report, notifier, _ = _run(fa, settings=_settings(flatten_on_breach=False),
                               tmp_path=tmp_path)
    assert report.tripped is True
    assert any(r[0] == "critical" and r[3] == "dd_breach" for r in notifier.records)


def test_peak_zero_warns_no_data_and_continues(tmp_path):
    fa = FakeAdapter(equity=EquityView(current=Decimal("0"), recent_peak=Decimal("0")),
                     account=_account())
    report, notifier, _ = _run(fa, tmp_path=tmp_path)
    assert report.tripped is False  # 無資料不是 breach，本輪照常對帳
    warns = [r for r in notifier.records if r[0] == "warn"]
    assert any(r[3] == "dd_no_data" for r in warns)


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
                                         margin_used=Decimal("0"))])
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
    # safety_net 已接線且回傳 dict（fake_sync 內已實際呼叫一次）
    assert isinstance(report.safety_net, dict)
    assert set(report.safety_net) >= {"opened", "flattened", "failed"}
    # 讀取地址正確：leader 與 my 各讀了 orders/positions/account
    addrs = [c["address"] for c in fa.calls["get_account_state"]]
    assert addrs == [LEADER, MY_ADDR]


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
