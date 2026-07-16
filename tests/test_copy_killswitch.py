"""killswitch 純函式 + trip 流程 + panic 腳本測試（Task 13 / spec T3.1）。

門檻語意對照線上引擎 hl-copytrader/main.py:176：drawdown > max 才觸發（嚴格大於）。
trip 紅線：cancel 全部先於任何 close；單點失敗不擋其他動作；ARM_FILE 部分失敗也要寫。
"""
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

from spark.copytrade.killswitch import (
    ARM_FILE_RELPATH,
    DrawdownStatus,
    check_drawdown,
    is_tripped,
    trip,
)
from spark.copytrade.notifier import RecordingNotifier
from spark.exchange.base import EquityView, OpenOrder, OrderResult, Position

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── 測試替身 ─────────────────────────────────────────────────────────

class FakeExecutor:
    """記錄呼叫序列的 ExecutorPort 替身（只實作 trip 用到的三個方法）。

    records 是全域時序清單：("cancel", coin, oid) / ("close", coin, is_buy, size)，
    供「cancel 全部先於任何 close」的順序斷言。
    """

    def __init__(self, open_orders=(), cancel_fail_oids=frozenset(),
                 close_fail_coins=frozenset(), close_raise_coins=frozenset()):
        self.records: list[tuple] = []
        self._open_orders = list(open_orders)
        self._cancel_fail_oids = frozenset(cancel_fail_oids)
        self._close_fail_coins = frozenset(close_fail_coins)
        self._close_raise_coins = frozenset(close_raise_coins)

    def get_open_orders(self) -> list[OpenOrder]:
        self.records.append(("get_open_orders",))
        return list(self._open_orders)

    def cancel(self, coin: str, oid: int) -> bool:
        self.records.append(("cancel", coin, oid))
        return oid not in self._cancel_fail_oids

    def close_reduce_only(self, coin: str, is_buy: bool, size: Decimal) -> OrderResult:
        self.records.append(("close", coin, is_buy, size))
        if coin in self._close_raise_coins:
            raise RuntimeError(f"connection reset while closing {coin}")
        if coin in self._close_fail_coins:
            return OrderResult(ok=False, filled_size=Decimal("0"), avg_px=Decimal("0"),
                               raw={"error": "rejected"})
        return OrderResult(ok=True, filled_size=size, avg_px=Decimal("100"), raw={})


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


def test_trip_all_success_sends_exactly_one_critical_summary(tmp_path):
    notifier = RecordingNotifier()
    trip(FakeExecutor(), {"ETH": _position()}, notifier, tmp_path, _status())
    criticals = [r for r in notifier.records if r[0] == "critical"]
    assert len(criticals) == 1  # 只有總結，沒有逐 coin 失敗告警


# ── 3. close 失敗路徑 ────────────────────────────────────────────────

def test_trip_close_failure_isolated_and_armed(tmp_path):
    """中間一個 coin close ok=False → failures 記錄、逐 coin critical + 總結 critical、
    其餘部位照平、ARM_FILE 仍寫入且內容含 failures。"""
    notifier = RecordingNotifier()
    ex = FakeExecutor(close_fail_coins={"SOL"})
    positions = {"ETH": _position("ETH", "2"), "SOL": _position("SOL", "10"),
                 "BTC": _position("BTC", "-0.5")}
    report = trip(ex, positions, notifier, tmp_path, _status())

    assert report.failures == ("SOL",)
    assert set(report.closed) == {"ETH", "BTC"}  # 一個失敗不擋其他部位
    closes = [r[1] for r in ex.records if r[0] == "close"]
    assert closes == ["ETH", "SOL", "BTC"]  # SOL 失敗後 BTC 照平

    criticals = [r for r in notifier.records if r[0] == "critical"]
    assert len(criticals) >= 2  # 逐 coin + 總結
    assert any("SOL" in r[2] for r in criticals)

    arm = tmp_path / ARM_FILE_RELPATH
    assert arm.exists(), "部分失敗也必須寫 ARM_FILE（鎖死交易優先於完美平倉）"
    payload = json.loads(arm.read_text())
    assert payload["failures"] == ["SOL"]


def test_trip_close_exception_treated_as_failure(tmp_path):
    """close 拋例外（transient 已由 resilience 層重試耗盡）→ 同 ok=False 處理，不再重試。"""
    notifier = RecordingNotifier()
    ex = FakeExecutor(close_raise_coins={"ETH"})
    positions = {"ETH": _position("ETH", "2"), "BTC": _position("BTC", "-0.5")}
    report = trip(ex, positions, notifier, tmp_path, _status())

    assert report.failures == ("ETH",)
    assert report.closed == ("BTC",)
    assert (tmp_path / ARM_FILE_RELPATH).exists()
    assert any(r[0] == "critical" and "ETH" in r[2] for r in notifier.records)


# ── 4. cancel 失敗路徑 ───────────────────────────────────────────────

def test_trip_cancel_failure_warns_continues_and_closes_proceed(tmp_path):
    notifier = RecordingNotifier()
    ex = FakeExecutor(
        open_orders=[_open_order("ETH", 1), _open_order("BTC", 2)],
        cancel_fail_oids={1},
    )
    report = trip(ex, {"ETH": _position()}, notifier, tmp_path, _status())

    cancel_records = [r for r in ex.records if r[0] == "cancel"]
    assert len(cancel_records) == 2, "單張撤單失敗不得中斷後續撤單"
    assert report.cancelled == 1  # 只計成功的
    assert any(r[0] == "warn" for r in notifier.records)
    assert any(r[0] == "close" for r in ex.records), "撤單失敗不得擋平倉"


# ── 5. ARM_FILE 內容與 is_tripped ────────────────────────────────────

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


def test_is_tripped_both_states(tmp_path):
    assert is_tripped(tmp_path) is False
    trip(FakeExecutor(), {}, RecordingNotifier(), tmp_path, _status())
    assert is_tripped(tmp_path) is True


# ── 6. panic 腳本 ────────────────────────────────────────────────────

def test_panic_without_env_prints_usage_and_exits_nonzero():
    """實跑（subprocess，import 階段不得打網路——若打了會因無 mock 而炸出非用法錯誤）。"""
    env = {k: v for k, v in os.environ.items()
           if k not in ("SPARK_ACCOUNT_ID", "SPARK_USER_ADDR", "SPARK_BUILDER_ADDR")}
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.panic"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode != 0
    assert "用法" in proc.stdout or "用法" in proc.stderr


def test_panic_plan_actions_is_pure_and_lists_all_actions():
    """dry 模式驗證做法：抽出純函式 _plan_actions（不碰 executor、零寫入呼叫），
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
