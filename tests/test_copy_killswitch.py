"""killswitch 純函式 + trip 流程 + panic 腳本測試（Task 13 / spec T3.1 + 雙審修正）。

門檻語意對照線上引擎 hl-copytrader/main.py:176：drawdown > max 才觸發（嚴格大於）。
trip 紅線：lock-first ARM 先落地；cancel 全部先於任何 close；單點失敗不擋其他動作；
ARM_FILE 部分失敗也要寫；冪等呼叫走 resilience 邊界（sleep_fn 注入，測試不真睡）。
"""
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from spark.copytrade.config import CopySettings
from spark.copytrade.killswitch import (
    ALERTS_LOG_RELPATH,
    ARM_FILE_RELPATH,
    CloseAction,
    DrawdownStatus,
    check_drawdown,
    evaluate,
    is_tripped,
    plan_close_actions,
    trip,
)
from spark.copytrade.notifier import RecordingNotifier
from spark.exchange.base import EquityView, OpenOrder, OrderResult, Position

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── 測試替身 ─────────────────────────────────────────────────────────

class FakeExecutor:
    """記錄呼叫序列的 ExecutorPort 替身（trip 所需方法＋T12 相容的 get_size_decimals）。

    records 是全域時序清單：("cancel", coin, oid) / ("close", coin, is_buy, size)，
    供「cancel 全部先於任何 close」的順序斷言。
    open_orders_exc：get_open_orders 拋出的例外（驗 resilience 重試與 critical 升級）。
    on_close：每次 close 呼叫開頭回呼（驗 lock-first ARM 在平倉期間已落地）。
    """

    def __init__(self, open_orders=(), cancel_fail_oids=frozenset(),
                 cancel_raise_oids=frozenset(), close_fail_coins=frozenset(),
                 close_raise_coins=frozenset(), open_orders_exc=None, on_close=None):
        self.records: list[tuple] = []
        self._open_orders = list(open_orders)
        self._cancel_fail_oids = frozenset(cancel_fail_oids)
        self._cancel_raise_oids = frozenset(cancel_raise_oids)
        self._close_fail_coins = frozenset(close_fail_coins)
        self._close_raise_coins = frozenset(close_raise_coins)
        self._open_orders_exc = open_orders_exc
        self._on_close = on_close

    def get_open_orders(self) -> list[OpenOrder]:
        self.records.append(("get_open_orders",))
        if self._open_orders_exc is not None:
            raise self._open_orders_exc
        return list(self._open_orders)

    def cancel(self, coin: str, oid: int) -> bool:
        self.records.append(("cancel", coin, oid))
        if oid in self._cancel_raise_oids:
            raise RuntimeError(f"boom cancelling oid={oid}")
        return oid not in self._cancel_fail_oids

    def close_reduce_only(self, coin: str, is_buy: bool, size: Decimal) -> OrderResult:
        self.records.append(("close", coin, is_buy, size))
        if self._on_close is not None:
            self._on_close(coin)
        if coin in self._close_raise_coins:
            raise RuntimeError(f"connection reset while closing {coin}")
        if coin in self._close_fail_coins:
            return OrderResult(ok=False, filled_size=Decimal("0"), avg_px=Decimal("0"),
                               raw={"error": "rejected"})
        return OrderResult(ok=True, filled_size=size, avg_px=Decimal("100"), raw={})

    def get_size_decimals(self, coin: str) -> int:
        return 4  # T12 ExecutorPort 新增方法的相容實作


def _open_order(coin="ETH", oid=1) -> OpenOrder:
    return OpenOrder(oid=oid, coin=coin, is_buy=True, limit_px=Decimal("2000"),
                     sz=Decimal("1"), reduce_only=False, is_trigger=False,
                     trigger_px=None, tpsl=None)


def _position(coin="ETH", szi="2") -> Position:
    return Position(coin=coin, szi=Decimal(szi), entry_px=Decimal("1900"), leverage=5,
                    is_cross=True, unrealized_pnl=Decimal("0"), margin_used=Decimal("100"))


def _status(current="840", peak="1000", dd="0.16", breached=True) -> DrawdownStatus:
    return DrawdownStatus(current=Decimal(current), peak=Decimal(peak),
                          drawdown_pct=Decimal(dd), breached=breached)


def _no_sleep(_delay):
    """resilience 退避的測試替身：不真睡。"""


# ── 1. check_drawdown 手算案例 ───────────────────────────────────────

def test_check_drawdown_below_threshold_not_breached():
    ev = EquityView(current=Decimal("900"), recent_peak=Decimal("1000"))
    st = check_drawdown(ev, Decimal("0.15"))
    assert st.drawdown_pct == Decimal("0.1")
    assert st.breached is False
    assert st.current == Decimal("900")
    assert st.peak == Decimal("1000")


def test_check_drawdown_above_threshold_breached():
    ev = EquityView(current=Decimal("840"), recent_peak=Decimal("1000"))
    st = check_drawdown(ev, Decimal("0.15"))
    assert st.drawdown_pct == Decimal("0.16")
    assert st.breached is True


def test_check_drawdown_exactly_at_threshold_not_breached():
    """嚴格大於（hl main.py:176 語意）：dd == max → 不觸發。"""
    ev = EquityView(current=Decimal("850"), recent_peak=Decimal("1000"))
    st = check_drawdown(ev, Decimal("0.15"))
    assert st.drawdown_pct == Decimal("0.15")
    assert st.breached is False


def test_check_drawdown_zero_peak_never_breaches():
    ev = EquityView(current=Decimal("0"), recent_peak=Decimal("0"))
    st = check_drawdown(ev, Decimal("0.15"))
    assert st.breached is False
    assert st.drawdown_pct == Decimal("0")


# ── 1b. evaluate：degenerate equity 的結構性告警出口 ─────────────────

def test_evaluate_degenerate_peak_warns_with_dedup_key():
    notifier = RecordingNotifier()
    st = evaluate(EquityView(current=Decimal("0"), recent_peak=Decimal("0")),
                  CopySettings(), notifier)
    assert st.breached is False
    warns = [r for r in notifier.records if r[0] == "warn"]
    assert len(warns) == 1
    assert warns[0][3] == "equity_degenerate"  # dedup_key


def test_evaluate_healthy_equity_no_warn_and_breach_passthrough():
    notifier = RecordingNotifier()
    st = evaluate(EquityView(current=Decimal("700"), recent_peak=Decimal("1000")),
                  CopySettings(), notifier)  # dd=0.3 > 預設 max 0.20
    assert st.breached is True
    assert notifier.records == []


# ── 2. trip 順序與方向 ───────────────────────────────────────────────

def test_trip_cancels_all_before_any_close(tmp_path):
    ex = FakeExecutor(
        open_orders=[_open_order("ETH", 1), _open_order("BTC", 2)],
    )
    positions = {"ETH": _position("ETH", "2"), "BTC": _position("BTC", "-0.5")}
    report = trip(ex, positions, RecordingNotifier(), tmp_path, _status())

    cancels = [i for i, r in enumerate(ex.records) if r[0] == "cancel"]
    closes = [i for i, r in enumerate(ex.records) if r[0] == "close"]
    assert len(cancels) == 2 and len(closes) == 2
    assert max(cancels) < min(closes), "全部 cancel 必須先於任何 close"
    assert report.cancelled == 2
    assert report.orders_not_cancelled is False


def test_trip_close_direction_and_full_size(tmp_path):
    """多倉 is_buy=False（賣出平多）、空倉 is_buy=True（買回平空），全量 |szi|。"""
    ex = FakeExecutor()
    positions = {"ETH": _position("ETH", "2"), "BTC": _position("BTC", "-0.5")}
    report = trip(ex, positions, RecordingNotifier(), tmp_path, _status())

    closes = {r[1]: r for r in ex.records if r[0] == "close"}
    assert closes["ETH"] == ("close", "ETH", False, Decimal("2"))
    assert closes["BTC"] == ("close", "BTC", True, Decimal("0.5"))
    assert set(report.closed) == {"ETH", "BTC"}
    assert report.failures == ()


def test_trip_skips_zero_size_position(tmp_path):
    ex = FakeExecutor()
    positions = {"ETH": _position("ETH", "0"), "BTC": _position("BTC", "1")}
    report = trip(ex, positions, RecordingNotifier(), tmp_path, _status())
    assert [r[1] for r in ex.records if r[0] == "close"] == ["BTC"]
    assert report.closed == ("BTC",)


def test_trip_all_success_sends_exactly_one_critical_summary(tmp_path):
    notifier = RecordingNotifier()
    trip(FakeExecutor(), {"ETH": _position()}, notifier, tmp_path, _status())
    criticals = [r for r in notifier.records if r[0] == "critical"]
    assert len(criticals) == 1  # 只有總結，沒有逐 coin 失敗告警


# ── 2b. plan_close_actions 共用規劃（trip 實際動作 = panic 預覽，交叉比對）──

def test_plan_close_actions_pure_mapping():
    positions = [_position("ETH", "2"), _position("BTC", "-0.5"), _position("DOGE", "0")]
    assert plan_close_actions(positions) == (
        CloseAction(coin="ETH", is_buy=False, size=Decimal("2")),
        CloseAction(coin="BTC", is_buy=True, size=Decimal("0.5")),
    )


def test_trip_actions_match_plan_close_actions_same_input(tmp_path):
    """同輸入交叉比對：trip 實際送出的 close 序列 == plan_close_actions 的規劃
    ==（經 panic._plan_actions）dry-run 預覽——預覽與實際不得雙實作。"""
    from scripts.panic import _plan_actions

    positions = [_position("ETH", "2"), _position("BTC", "-0.5"), _position("DOGE", "0")]
    actions = plan_close_actions(positions)

    ex = FakeExecutor()
    trip(ex, {p.coin: p for p in positions}, RecordingNotifier(), tmp_path, _status())
    trip_closes = [(r[1], r[2], r[3]) for r in ex.records if r[0] == "close"]
    assert trip_closes == [(a.coin, a.is_buy, a.size) for a in actions]

    preview = "\n".join(_plan_actions([], positions))
    for a in actions:
        side = "buy" if a.is_buy else "sell"
        assert f"close {a.coin} {side} size={a.size}" in preview
    assert "DOGE" not in preview


# ── 3. close 失敗路徑 ────────────────────────────────────────────────

def test_trip_close_failure_isolated_and_armed(tmp_path):
    """中間一個 coin close ok=False（語意拒單，不重試）→ failures 記錄、逐 coin critical
    + 總結 critical、其餘部位照平、ARM_FILE 仍寫入且內容含 failures。"""
    notifier = RecordingNotifier()
    ex = FakeExecutor(close_fail_coins={"SOL"})
    positions = {"ETH": _position("ETH", "2"), "SOL": _position("SOL", "10"),
                 "BTC": _position("BTC", "-0.5")}
    report = trip(ex, positions, notifier, tmp_path, _status())

    assert report.failures == ("SOL",)
    assert set(report.closed) == {"ETH", "BTC"}  # 一個失敗不擋其他部位
    closes = [r[1] for r in ex.records if r[0] == "close"]
    assert closes == ["ETH", "SOL", "BTC"]  # 語意失敗單次嘗試、SOL 失敗後 BTC 照平

    criticals = [r for r in notifier.records if r[0] == "critical"]
    assert len(criticals) >= 2  # 逐 coin + 總結
    assert any("SOL" in r[2] for r in criticals)

    arm = tmp_path / ARM_FILE_RELPATH
    assert arm.exists(), "部分失敗也必須寫 ARM_FILE（鎖死交易優先於完美平倉）"
    payload = json.loads(arm.read_text())
    assert payload["failures"] == ["SOL"]


def test_trip_close_transient_exception_retried_then_failure(tmp_path):
    """close 拋 transient 例外 → 經 resilience 邊界重試 3 次（退避 0.6/1.2，不真睡）
    → 耗盡即終態 failure；其餘部位照平；preliminary ARM 在平倉期間已落地（lock-first）。"""
    notifier = RecordingNotifier()
    sleeps: list[float] = []
    arm_seen: list[bool] = []

    def on_close(_coin):
        arm = tmp_path / ARM_FILE_RELPATH
        arm_seen.append(arm.exists() and "flatten_in_progress" in arm.read_text())

    ex = FakeExecutor(close_raise_coins={"ETH"}, on_close=on_close)
    positions = {"ETH": _position("ETH", "2"), "BTC": _position("BTC", "-0.5")}
    report = trip(ex, positions, notifier, tmp_path, _status(), sleep_fn=sleeps.append)

    eth_attempts = [r for r in ex.records if r[0] == "close" and r[1] == "ETH"]
    assert len(eth_attempts) == 3, "transient 例外應由 resilience 邊界重試 3 次"
    assert sleeps == [0.6, 1.2]
    assert report.failures == ("ETH",)
    assert report.closed == ("BTC",)
    assert all(arm_seen), "lock-first：任何 close 期間 preliminary ARM 必須已存在"
    assert any(r[0] == "critical" and "ETH" in r[2] for r in notifier.records)


# ── 4. cancel / get_open_orders 失敗路徑 ─────────────────────────────

def test_trip_cancel_failure_criticals_continues_and_closes_proceed(tmp_path):
    notifier = RecordingNotifier()
    ex = FakeExecutor(
        open_orders=[_open_order("ETH", 1), _open_order("BTC", 2)],
        cancel_fail_oids={1},
    )
    report = trip(ex, {"ETH": _position()}, notifier, tmp_path, _status())

    cancel_records = [r for r in ex.records if r[0] == "cancel"]
    assert len(cancel_records) == 2, "單張撤單失敗不得中斷後續撤單"
    assert report.cancelled == 1  # 只計成功的
    assert any(r[0] == "critical" and "撤單失敗" in r[2] for r in notifier.records)
    assert any(r[0] == "close" for r in ex.records), "撤單失敗不得擋平倉"


def test_trip_cancel_exception_criticals_and_continues(tmp_path):
    notifier = RecordingNotifier()
    ex = FakeExecutor(
        open_orders=[_open_order("ETH", 1), _open_order("BTC", 2)],
        cancel_raise_oids={1},
    )
    report = trip(ex, {"ETH": _position()}, notifier, tmp_path, _status())

    assert len([r for r in ex.records if r[0] == "cancel"]) == 2
    assert report.cancelled == 1
    assert any(r[0] == "critical" and "撤單例外" in r[2] for r in notifier.records)
    assert report.closed == ("ETH",)


def test_trip_get_open_orders_transient_failure_criticals_and_still_flattens(tmp_path):
    """get_open_orders 拋 transient → resilience 重試 3 次耗盡 → critical（非 warn）
    ＋orders_not_cancelled=True（含 ARM payload）＋照樣續平倉。"""
    notifier = RecordingNotifier()
    sleeps: list[float] = []
    ex = FakeExecutor(open_orders_exc=RuntimeError("connection reset by peer"))
    report = trip(ex, {"ETH": _position()}, notifier, tmp_path, _status(),
                  sleep_fn=sleeps.append)

    assert len([r for r in ex.records if r[0] == "get_open_orders"]) == 3
    assert sleeps == [0.6, 1.2]
    assert report.orders_not_cancelled is True
    assert report.cancelled == 0
    assert report.closed == ("ETH",), "取掛單失敗不得擋平倉"
    assert any(r[0] == "critical" and "取得掛單失敗" in r[2] for r in notifier.records)
    payload = json.loads((tmp_path / ARM_FILE_RELPATH).read_text())
    assert payload["orders_not_cancelled"] is True


# ── 5. ARM_FILE / alerts.log / is_tripped ────────────────────────────

def test_arm_file_contains_timestamp_and_status_numbers(tmp_path):
    status = _status(current="840", peak="1000", dd="0.16")
    report = trip(FakeExecutor(), {}, RecordingNotifier(), tmp_path, status)

    arm = tmp_path / ARM_FILE_RELPATH
    assert report.arm_file == str(arm)
    payload = json.loads(arm.read_text())
    assert payload["tripped_at"]  # ISO 時間戳非空
    assert "T" in payload["tripped_at"]
    assert payload["current"] == "840"
    assert payload["peak"] == "1000"
    assert payload["drawdown_pct"] == "0.16"
    assert payload["failures"] == []
    assert payload["phase"] == "complete"
    assert payload["orders_not_cancelled"] is False


def test_arm_write_failure_criticals_raises_and_no_trade_actions(tmp_path):
    """ARM 寫入 OSError（root 是檔案 → mkdir 失敗）→ critical＋上拋；lock-first 語意：
    鎖不住就不動手——一筆撤單/平倉都不得送出。"""
    notifier = RecordingNotifier()
    bad_root = tmp_path / "rootfile"
    bad_root.write_text("")  # 佔住路徑，mkdir parents 必失敗
    ex = FakeExecutor(open_orders=[_open_order("ETH", 1)])

    with pytest.raises(OSError):
        trip(ex, {"ETH": _position()}, notifier, bad_root, _status())

    assert any(r[0] == "critical" and "ARM_FILE" in r[2] for r in notifier.records)
    assert ex.records == [], "鎖不住（ARM 落不了地）就不得執行任何交易動作"


def test_criticals_are_persisted_to_alerts_log(tmp_path):
    """持久證據層：trip 的每則 critical 同步落 alerts.log（時間戳＋訊息）。"""
    ex = FakeExecutor(close_fail_coins={"ETH"})
    trip(ex, {"ETH": _position()}, RecordingNotifier(), tmp_path, _status())

    log = (tmp_path / ALERTS_LOG_RELPATH).read_text()
    lines = [ln for ln in log.splitlines() if ln]
    assert len(lines) == 2  # 逐 coin 失敗 + 總結
    assert all("T" in ln[:30] for ln in lines)  # 每行開頭是 ISO 時間戳
    assert "平倉失敗 ETH" in log
    assert "KILL SWITCH TRIPPED" in log


def test_is_tripped_both_states(tmp_path):
    assert is_tripped(tmp_path) is False
    trip(FakeExecutor(), {}, RecordingNotifier(), tmp_path, _status())
    assert is_tripped(tmp_path) is True


# ── 6. panic 腳本 ────────────────────────────────────────────────────

def test_panic_without_env_prints_usage_and_exits_2():
    """實跑（subprocess，import 階段不得打網路——若打了會因無 mock 而炸出非用法錯誤）。"""
    env = {k: v for k, v in os.environ.items()
           if k not in ("SPARK_ACCOUNT_ID", "SPARK_USER_ADDR", "SPARK_BUILDER_ADDR")}
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.panic"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 2
    assert "用法" in proc.stdout or "用法" in proc.stderr


def test_panic_plan_actions_is_pure_and_lists_all_actions():
    """dry 模式驗證做法：純函式 _plan_actions（內部共用 plan_close_actions，零寫入呼叫），
    直接以輸入輸出驗證 dry 輸出內容——撤單張數、每個部位的平倉方向與全量。"""
    from scripts.panic import _plan_actions

    orders = [_open_order("ETH", 11), _open_order("BTC", 22)]
    positions = [_position("ETH", "2"), _position("BTC", "-0.5"),
                 _position("DOGE", "0")]  # szi=0 不產生平倉動作
    lines = _plan_actions(orders, positions)
    text = "\n".join(lines)

    assert "2" in lines[0] and "撤" in lines[0]        # 撤單張數
    assert "oid=11" in text and "oid=22" in text
    assert any("ETH" in ln and "sell" in ln and "2" in ln for ln in lines)   # 平多 → 賣
    assert any("BTC" in ln and "buy" in ln and "0.5" in ln for ln in lines)  # 平空 → 買
    assert "DOGE" not in text


def test_panic_resolve_state_dir_env_override_direct(monkeypatch, tmp_path):
    """M2 Task 10（T8 sonnet minor）：panic.resolve_state_dir() 讀 FILET_STATE_DIR
    env 覆寫路徑的直接測試——既有測試（panic_env fixture）都是 monkeypatch
    panic._REPO_ROOT 繞過 env 分支，這裡直測 env 分支本身。絕對路徑經 .resolve()
    正規化（冪等），故對已存在的 tmp_path 斷言 == tmp_path.resolve()。"""
    import scripts.panic as panic

    monkeypatch.setenv("FILET_STATE_DIR", str(tmp_path))
    assert panic.resolve_state_dir() == tmp_path.resolve()


def test_panic_resolve_state_dir_relative_env_becomes_absolute(monkeypatch, tmp_path):
    """Task 10 fast-follow（T4 reviewer Important）：panic.py 的孿生
    resolve_state_dir() 也須把相對路徑 FILET_STATE_DIR .resolve() 成絕對——否則
    同一相對路徑在不同 CWD 下靜默指向不同狀態根，緊急平倉 ARM 檔可能因此失聯。"""
    import scripts.panic as panic

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FILET_STATE_DIR", "relative/sub")
    result = panic.resolve_state_dir()
    assert result.is_absolute()
    assert result == (tmp_path / "relative" / "sub").resolve()


def test_panic_resolve_state_dir_defaults_to_repo_root(monkeypatch):
    import scripts.panic as panic

    monkeypatch.delenv("FILET_STATE_DIR", raising=False)
    assert panic.resolve_state_dir() == panic._REPO_ROOT


# ── 6b. panic main()：monkeypatch 注入 fake，dry / --yes 兩分支 ──────

class _FakePanicAdapter:
    """panic main() 用的 HyperliquidAdapter 替身：一個 ETH 多倉 + 一張掛單。"""

    def __init__(self, equity: EquityView, close_ok: bool):
        self.writes: list[tuple] = []
        self.network = None
        self.exchange = None
        self._equity = equity
        self._close_ok = close_ok

    def get_open_orders(self, address):
        return [_open_order("ETH", 11)]

    def get_positions(self, address):
        return [_position("ETH", "2")]

    def get_account_value(self, address):
        return self._equity.current

    def get_equity_view(self, address):
        return self._equity

    def cancel_order(self, signer, coin, oid):
        self.writes.append(("cancel", coin, oid))
        return True

    def close_reduce_only(self, signer, coin, is_buy, size, slippage, builder):
        self.writes.append(("close", coin, is_buy, size))
        if self._close_ok:
            return OrderResult(ok=True, filled_size=size, avg_px=Decimal("100"), raw={})
        return OrderResult(ok=False, filled_size=Decimal("0"), avg_px=Decimal("0"),
                           raw={"error": "rejected"})


@pytest.fixture
def panic_env(monkeypatch, tmp_path):
    """把 panic main() 的外部依賴全部換成離線替身，ARM/alerts 落 tmp_path。"""
    import scripts.panic as panic

    cfg = {"equity": EquityView(current=Decimal("900"), recent_peak=Decimal("1000")),
           "close_ok": True}
    adapters: list[_FakePanicAdapter] = []

    class _FakeKeychain:
        def get_agent_signer(self, account_id):
            return object()

    def adapter_factory(network, info=None, exchange=None):
        a = _FakePanicAdapter(equity=cfg["equity"], close_ok=cfg["close_ok"])
        a.network = network
        a.exchange = exchange
        adapters.append(a)
        return a

    monkeypatch.setenv("SPARK_ACCOUNT_ID", "acct")
    monkeypatch.setenv("SPARK_USER_ADDR", "0x" + "1" * 40)
    monkeypatch.setenv("SPARK_BUILDER_ADDR", "0x" + "2" * 40)
    monkeypatch.setenv("SPARK_NETWORK", "testnet")
    monkeypatch.setattr("hyperliquid.info.Info", lambda *a, **k: object())
    monkeypatch.setattr("hyperliquid.exchange.Exchange", lambda *a, **k: object())
    monkeypatch.setattr("spark.keystore.keychain.MacKeychainBackend", _FakeKeychain)
    monkeypatch.setattr("spark.exchange.hyperliquid.HyperliquidAdapter", adapter_factory)
    monkeypatch.setattr(panic, "_REPO_ROOT", tmp_path)
    return SimpleNamespace(panic=panic, adapters=adapters, root=tmp_path, cfg=cfg)


def test_panic_main_dry_lists_plan_with_zero_writes(panic_env, capsys):
    panic_env.panic.main([])  # 無 --yes → dry，正常 return（不 SystemExit）
    out = capsys.readouterr().out
    assert "[DRY-RUN]" in out
    assert "cancel ETH oid=11" in out
    assert "close ETH sell size=2" in out
    assert all(a.writes == [] for a in panic_env.adapters), "dry 模式零寫入呼叫"
    assert panic_env.adapters[0].exchange is None, "dry 用唯讀 adapter（exchange=None）"
    assert not (panic_env.root / ARM_FILE_RELPATH).exists()


def test_panic_main_yes_runs_trip_and_exits_0(panic_env, capsys):
    with pytest.raises(SystemExit) as ei:
        panic_env.panic.main(["--yes"])
    assert ei.value.code == 0
    a = panic_env.adapters[0]
    assert ("cancel", "ETH", 11) in a.writes
    assert ("close", "ETH", False, Decimal("2")) in a.writes
    assert (panic_env.root / ARM_FILE_RELPATH).exists()
    assert "KILL SWITCH TRIPPED" in capsys.readouterr().out


def test_panic_main_yes_close_failure_exits_1_still_armed(panic_env):
    panic_env.cfg["close_ok"] = False
    with pytest.raises(SystemExit) as ei:
        panic_env.panic.main(["--yes"])
    assert ei.value.code == 1
    assert (panic_env.root / ARM_FILE_RELPATH).exists(), "失敗也要鎖死"


def test_panic_exit_code_reflects_orders_not_cancelled():
    """orders_not_cancelled=True（掛單清單未知、一張未撤）即使 failures 為空也是
    未乾淨收場——殘留掛單可能繼續成交，退出碼必須 1 讓外層腳本/人看到。"""
    from spark.copytrade.killswitch import FlattenReport

    from scripts.panic import _exit_code

    assert _exit_code(FlattenReport(cancelled=0, closed=("ETH",), failures=(),
                                    arm_file="x", orders_not_cancelled=True)) == 1
    assert _exit_code(FlattenReport(cancelled=1, closed=("ETH",), failures=("BTC",),
                                    arm_file="x", orders_not_cancelled=False)) == 1
    assert _exit_code(FlattenReport(cancelled=1, closed=("ETH",), failures=(),
                                    arm_file="x", orders_not_cancelled=False)) == 0


def test_panic_main_already_tripped_prints_notice(panic_env, capsys):
    arm = panic_env.root / ARM_FILE_RELPATH
    arm.parent.mkdir(parents=True)
    arm.write_text("{}")
    panic_env.panic.main([])
    assert "已 tripped" in capsys.readouterr().out


def test_panic_main_yes_degenerate_equity_warns(panic_env, capsys):
    panic_env.cfg["equity"] = EquityView(current=Decimal("0"), recent_peak=Decimal("0"))
    with pytest.raises(SystemExit):
        panic_env.panic.main(["--yes"])
    out = capsys.readouterr().out
    assert "[WARN]" in out and "degenerate" in out


def test_evaluate_alerts_when_coverage_insufficient():
    """C1：覆蓋不足必須發 critical（無資料不得偽裝成無回撤）。"""
    from spark.copytrade.equity import SampleCoverage

    notifier = RecordingNotifier()
    ev = EquityView(current=Decimal("500"), recent_peak=Decimal("500"))
    cov = SampleCoverage(count=0, oldest_age_s=0.0, read_error=False)
    evaluate(ev, CopySettings(), notifier, coverage=cov)
    messages = [r[2] for r in notifier.records if r[0] == "critical"]
    assert any("回撤保護尚未生效" in m for m in messages)


def test_evaluate_slow_gate_trips_on_lifetime_drawdown():
    """C2：7 天窗內無回撤，但自開始以來跌超過絕對底線 → 必須 breached。"""
    notifier = RecordingNotifier()
    # 窗內 peak==current（慢跌後窗已滾動）→ 快速閘算出 dd=0
    ev = EquityView(current=Decimal("400"), recent_peak=Decimal("400"))
    st = evaluate(ev, CopySettings(), notifier, lifetime_peak=Decimal("1000"))
    assert st.breached is True, "自 1000 跌到 400（60%）應觸發 40% 絕對底線"
    assert st.drawdown_pct == Decimal("0.6")
