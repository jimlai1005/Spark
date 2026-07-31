"""follower（我方錢包）出入金回撤基準校正（Wave 5）。

數值錨例為**預註冊**（docs/superpowers/plans/2026-07-31-custom-vault-follower-flow.md
Wave 5 節），測試逐條硬編，不得由實作反推。

語意提醒：與 leader 側 36h 線性衰減刻意不同——follower 回撤量的是交易損益，
出入金流量要**永久**排除、不衰減。
"""
import json
from decimal import Decimal

from spark.copytrade.config import CopySettings
from spark.copytrade.equity import LIFETIME_PEAK_RELPATH, SAMPLES_RELPATH
from spark.copytrade.follower_flow import STATE_RELPATH, apply_follower_flows
from spark.copytrade.killswitch import check_drawdown, evaluate
from spark.copytrade.notifier import RecordingNotifier
from spark.exchange.base import EquityView, LedgerFlow
from spark.exchange.fakes import FakeAdapter
from spark.exchange.ledger_flows import signed_flow

ADDR = "0xfollower"
NOW_MS = 1_753_920_000_000
LAST_MS = NOW_MS - 60_000          # 已初始化標記（上一輪）
FLOW_MS = NOW_MS - 30_000          # 流量落在 (LAST_MS, NOW_MS] 窗內
T1_S = NOW_MS / 1000 - 600.0
T2_S = NOW_MS / 1000 - 300.0


# ── seeding / read-back helpers ──────────────────────────────────────────
def _seed_samples(root, values) -> None:
    p = root / SAMPLES_RELPATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([[ts, v] for ts, v in values]))


def _seed_peak(root, value: str) -> None:
    p = root / LIFETIME_PEAK_RELPATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(value))


def _seed_marker(root, last_ms: int) -> None:
    p = root / STATE_RELPATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"last_processed_ms": last_ms}))


def _seed_baseline(root) -> None:
    """預註冊錨例的共同起點：樣本 [(t1,1000),(t2,1200)]、lifetime peak 1200。"""
    _seed_samples(root, [(T1_S, "1000"), (T2_S, "1200")])
    _seed_peak(root, "1200")
    _seed_marker(root, LAST_MS)


def _sample_values(root) -> list[Decimal]:
    return [Decimal(v) for _, v in json.loads((root / SAMPLES_RELPATH).read_text())]


def _sample_ts(root) -> list[float]:
    return [ts for ts, _ in json.loads((root / SAMPLES_RELPATH).read_text())]


def _peak(root) -> Decimal:
    return Decimal(str(json.loads((root / LIFETIME_PEAK_RELPATH).read_text())))


def _marker(root) -> int:
    return json.loads((root / STATE_RELPATH).read_text())["last_processed_ms"]


def _apply(root, adapter, *, now_ms=NOW_MS) -> RecordingNotifier:
    notifier = RecordingNotifier()
    apply_follower_flows(root, adapter, ADDR, notifier, now_ms=now_ms)
    return notifier


def _flows_adapter(*flows) -> FakeAdapter:
    return FakeAdapter(ledger_flows=(list(flows), []))


# ── 錨例 1：出金 300（net=-300）──────────────────────────────────────────
def test_withdrawal_shifts_samples_and_peak(tmp_path):
    """樣本 [1000,1200]、peak 1200，出金 300 → 樣本 [700,900]、peak 900；
    current=900 時 rolling dd=0（出金不再被視為虧損）。"""
    _seed_baseline(tmp_path)
    fa = _flows_adapter(LedgerFlow(time_ms=FLOW_MS, usdc=Decimal("-300")))
    notifier = _apply(tmp_path, fa)

    assert _sample_values(tmp_path) == [Decimal("700"), Decimal("900")]
    assert _sample_ts(tmp_path) == [T1_S, T2_S]  # 平移 value，不動時間戳
    assert _peak(tmp_path) == Decimal("900")
    assert _marker(tmp_path) == NOW_MS
    # 校正後基準：current=900、peak=900 → dd=0
    st = check_drawdown(EquityView(current=Decimal("900"), recent_peak=Decimal("900")),
                        Decimal("0.20"))
    assert st.drawdown_pct == Decimal("0")
    assert st.breached is False
    # 資訊性告警：net 與「已校正」都要在
    warns = [r for r in notifier.records if r[0] == "warn"]
    assert any("-300" in r[2] and "已校正" in r[2] for r in warns)


def test_withdrawal_uncorrected_would_misread_25pct_drawdown(tmp_path):
    """對照組：**不校正**時同一情境會誤判 25% 回撤——這正是本模組要償還的債
    （(1200-900)/1200 = 0.25；未校正的 peak 停留在出金前的 1200）。"""
    st = check_drawdown(EquityView(current=Decimal("900"), recent_peak=Decimal("1200")),
                        Decimal("0.20"))
    assert st.drawdown_pct == Decimal("0.25")
    assert st.breached is True  # 25% > 20% ⇒ 修正前出金會直接觸發熔斷


# ── 錨例 2：入金 500（net=+500）——入金不得掩蓋虧損 ──────────────────────
def test_deposit_does_not_mask_losses(tmp_path):
    _seed_baseline(tmp_path)
    fa = _flows_adapter(LedgerFlow(time_ms=FLOW_MS, usdc=Decimal("500")))
    _apply(tmp_path, fa)

    assert _sample_values(tmp_path) == [Decimal("1500"), Decimal("1700")]
    assert _peak(tmp_path) == Decimal("1700")
    # current=1700（入金後、無交易虧損）→ dd=0
    st0 = check_drawdown(EquityView(current=Decimal("1700"), recent_peak=Decimal("1700")),
                         Decimal("0.20"))
    assert st0.drawdown_pct == Decimal("0")
    # current=1400（入金後又虧 300）→ rolling dd=(1700-1400)/1700 ≈ 17.6%
    st = check_drawdown(EquityView(current=Decimal("1400"), recent_peak=Decimal("1700")),
                        Decimal("0.20"))
    assert st.drawdown_pct == Decimal("300") / Decimal("1700")
    assert abs(st.drawdown_pct - Decimal("0.176")) < Decimal("0.001")


# ── 錨例 3：出金 1500（超過 peak）→ floor 0、killswitch 除零守衛 ──────────
def test_overwithdrawal_floors_peak_and_killswitch_skips_total_dd(tmp_path):
    _seed_baseline(tmp_path)
    fa = _flows_adapter(LedgerFlow(time_ms=FLOW_MS, usdc=Decimal("-1500")))
    _apply(tmp_path, fa)

    assert _sample_values(tmp_path) == [Decimal("-500"), Decimal("-300")]
    assert _peak(tmp_path) == Decimal("0")  # floor：max(0, 1200-1500)

    # killswitch：lifetime_peak <= 0 → 不判 total dd、不拋 ZeroDivisionError
    settings = CopySettings(volatility_weight_enabled=False)
    assert settings.max_total_drawdown_pct > 0  # 守衛真的被走到（不是靠閘門停用）
    notifier = RecordingNotifier()
    st = evaluate(EquityView(current=Decimal("-300"), recent_peak=Decimal("-300")),
                  settings, notifier, lifetime_peak=Decimal("0"))
    assert st.breached is False
    assert not any(r[3] == "equity_total_drawdown" for r in notifier.records)


# ── 標記生命週期 ─────────────────────────────────────────────────────────
def test_first_round_initializes_marker_without_touching_samples(tmp_path):
    """標記缺失＝首輪：只寫 now_ms、零調整、不打 ledger API——部署前的流量
    已烘進既有樣本，回溯會雙重計算，誠實不追。"""
    _seed_samples(tmp_path, [(T1_S, "1000"), (T2_S, "1200")])
    _seed_peak(tmp_path, "1200")
    fa = _flows_adapter(LedgerFlow(time_ms=FLOW_MS, usdc=Decimal("-999")))
    _apply(tmp_path, fa)

    assert _marker(tmp_path) == NOW_MS
    assert fa.calls["get_ledger_flows"] == []  # 初始化輪不取數
    assert _sample_values(tmp_path) == [Decimal("1000"), Decimal("1200")]
    assert _peak(tmp_path) == Decimal("1200")


def test_corrupt_marker_reinitializes_without_adjustment(tmp_path):
    """壞標記檔 → load 回 None → 視同首輪重新初始化（fail-safe：寧可漏校正，
    不可猜一個 last 出來冒雙重套用的險）。"""
    _seed_baseline(tmp_path)
    (tmp_path / STATE_RELPATH).write_text("{not json")
    fa = _flows_adapter(LedgerFlow(time_ms=FLOW_MS, usdc=Decimal("-300")))
    _apply(tmp_path, fa)

    assert _marker(tmp_path) == NOW_MS
    assert fa.calls["get_ledger_flows"] == []
    assert _sample_values(tmp_path) == [Decimal("1000"), Decimal("1200")]


def test_no_new_flows_advances_marker_only(tmp_path):
    _seed_baseline(tmp_path)
    fa = FakeAdapter(ledger_flows=([], []))
    _apply(tmp_path, fa)

    assert fa.calls["get_ledger_flows"] == [{"address": ADDR, "start_ms": LAST_MS + 1}]
    assert _marker(tmp_path) == NOW_MS
    assert _sample_values(tmp_path) == [Decimal("1000"), Decimal("1200")]
    assert _peak(tmp_path) == Decimal("1200")


# ── 失敗與 exactly-once ──────────────────────────────────────────────────
class _FlakyAdapter(FakeAdapter):
    """第一次取數丟 ConnectionError，之後回注入的 flows。"""

    def __init__(self, flows, **kw):
        super().__init__(**kw)
        self._flaky_flows = flows
        self._fail_next = True

    def get_ledger_flows(self, address, start_ms):
        self.calls["get_ledger_flows"].append({"address": address, "start_ms": start_ms})
        if self._fail_next:
            self._fail_next = False
            raise ConnectionError("ledger api down")
        return list(self._flaky_flows), []


def test_fetch_failure_keeps_marker_then_applies_exactly_once(tmp_path):
    """取數失敗輪：標記不動、零調整＋warn；下一輪同批流量**恰好套用一次**；
    再下一輪不重套（exactly-once）。"""
    _seed_baseline(tmp_path)
    fa = _FlakyAdapter([LedgerFlow(time_ms=FLOW_MS, usdc=Decimal("-300"))])

    # 輪 1：例外 → 標記不動、樣本不動、warn（dedup）
    n1 = _apply(tmp_path, fa, now_ms=NOW_MS)
    assert _marker(tmp_path) == LAST_MS
    assert _sample_values(tmp_path) == [Decimal("1000"), Decimal("1200")]
    assert any(r[0] == "warn" and r[3] == "follower_flow_fetch_failed"
               for r in n1.records)

    # 輪 2：自 last 重試 → 套用一次
    _apply(tmp_path, fa, now_ms=NOW_MS + 60_000)
    assert fa.calls["get_ledger_flows"][1] == {"address": ADDR, "start_ms": LAST_MS + 1}
    assert _sample_values(tmp_path) == [Decimal("700"), Decimal("900")]
    assert _peak(tmp_path) == Decimal("900")
    assert _marker(tmp_path) == NOW_MS + 60_000

    # 輪 3：fake 仍回同一批 flows（time_ms 已 ≤ 標記）→ 不得重套
    _apply(tmp_path, fa, now_ms=NOW_MS + 120_000)
    assert _sample_values(tmp_path) == [Decimal("700"), Decimal("900")]
    assert _peak(tmp_path) == Decimal("900")


def test_future_timestamped_flow_deferred_not_double_applied(tmp_path):
    """time_ms > now_ms 的流量（時鐘偏差）本輪不套——標記（now_ms）蓋不住它，
    套了下輪還會再撈到＝雙重套用（fail-open）。留給下輪恰好一次。"""
    _seed_baseline(tmp_path)
    future_ms = NOW_MS + 30_000
    fa = _flows_adapter(LedgerFlow(time_ms=future_ms, usdc=Decimal("-300")))

    _apply(tmp_path, fa, now_ms=NOW_MS)
    assert _sample_values(tmp_path) == [Decimal("1000"), Decimal("1200")]  # 本輪不套
    assert _marker(tmp_path) == NOW_MS

    _apply(tmp_path, fa, now_ms=NOW_MS + 60_000)  # 下輪窗涵蓋 → 套用一次
    assert _sample_values(tmp_path) == [Decimal("700"), Decimal("900")]

    _apply(tmp_path, fa, now_ms=NOW_MS + 120_000)  # 再下輪不重套
    assert _sample_values(tmp_path) == [Decimal("700"), Decimal("900")]


def test_clock_step_back_does_not_regress_marker_or_double_apply(tmp_path):
    """F1 回歸：時鐘回跳（NTP step）時標記絕不倒退——倒退＝已處理流量重新落窗＝
    下輪重套同批（閘變鈍，fail-open）。三輪：正常套用 → 回跳 120s（無新流量）→
    回正；斷言樣本與 peak 只被調整一次、標記單調不減。"""
    _seed_baseline(tmp_path)
    fa = _flows_adapter(LedgerFlow(time_ms=FLOW_MS, usdc=Decimal("-300")))

    # 輪 1：正常套用一次
    _apply(tmp_path, fa, now_ms=NOW_MS)
    assert _sample_values(tmp_path) == [Decimal("700"), Decimal("900")]
    assert _marker(tmp_path) == NOW_MS

    # 輪 2：時鐘回跳 120s（無新流量）——標記必須單調不減
    _apply(tmp_path, fa, now_ms=NOW_MS - 120_000)
    assert _marker(tmp_path) == NOW_MS
    assert _sample_values(tmp_path) == [Decimal("700"), Decimal("900")]

    # 輪 3：時鐘回正——FLOW_MS 仍在標記之下，不得重套（雙套＝樣本 [400,600]）
    _apply(tmp_path, fa, now_ms=NOW_MS + 60_000)
    assert _sample_values(tmp_path) == [Decimal("700"), Decimal("900")]
    assert _peak(tmp_path) == Decimal("900")
    assert _marker(tmp_path) == NOW_MS + 60_000


def test_account_class_transfer_out_shifts_baseline(tmp_path):
    """F2 錨例（2026-07-21 實測情境：owner perp→spot 劃轉 100）：
    accountClassTransfer usdc=100 toPerp=false → net=-100 →
    樣本 [1000,1200]→[900,1100]、peak 1200→1100。
    流量號取自 ledger_flows.signed_flow（型別映射唯一定義點，同源）。"""
    _seed_baseline(tmp_path)
    usdc = signed_flow({"type": "accountClassTransfer", "usdc": "100",
                        "toPerp": False})
    assert usdc == Decimal("-100")
    fa = _flows_adapter(LedgerFlow(time_ms=FLOW_MS, usdc=usdc))
    _apply(tmp_path, fa)

    assert _sample_values(tmp_path) == [Decimal("900"), Decimal("1100")]
    assert _peak(tmp_path) == Decimal("1100")


def test_samples_write_failure_warns_and_skips_batch_without_raising(tmp_path,
                                                                     monkeypatch):
    """觀察 (b)：樣本檔寫失敗（OSError）→ warn＋本批跳過（標記已推進＝漏校正，
    fail-safe），例外**不得**穿出——run_cycle 直呼本函式，穿出＝整輪熔斷路徑中斷。
    語意與 _shift_lifetime_peak 的 OSError 處理對稱。"""
    _seed_baseline(tmp_path)

    def _boom(path, samples):
        raise OSError("disk full")

    monkeypatch.setattr("spark.copytrade.follower_flow._save", _boom)
    fa = _flows_adapter(LedgerFlow(time_ms=FLOW_MS, usdc=Decimal("-300")))
    notifier = _apply(tmp_path, fa)          # 不得拋

    assert _marker(tmp_path) == NOW_MS       # 標記已推進（順序鐵則不變）
    assert _sample_values(tmp_path) == [Decimal("1000"), Decimal("1200")]  # 本批跳過
    assert _peak(tmp_path) == Decimal("1200")  # 整批一致跳過，不留半套狀態
    assert any(r[0] == "warn" and r[3] == "follower_flow_samples_write_failed"
               for r in notifier.records)


def test_anomalies_warn_with_type_names(tmp_path):
    _seed_baseline(tmp_path)
    fa = FakeAdapter(ledger_flows=([], ["accountClassTransfer", "spotTransfer"]))
    notifier = _apply(tmp_path, fa)

    warns = [r for r in notifier.records if r[0] == "warn"]
    assert any("accountClassTransfer" in r[2] and "spotTransfer" in r[2]
               and r[3] == "follower_flow_unknown_types" for r in warns)
    assert _sample_values(tmp_path) == [Decimal("1000"), Decimal("1200")]  # 零調整
    assert _marker(tmp_path) == NOW_MS


# ── 設定 ─────────────────────────────────────────────────────────────────
def test_config_default_on_with_escape_valve_env():
    assert CopySettings().follower_flow_correction_enabled is True
    off = CopySettings.from_env(env={"COPY_FOLLOWER_FLOW_CORRECTION": "false"})
    assert off.follower_flow_correction_enabled is False
