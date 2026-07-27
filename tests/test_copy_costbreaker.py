"""成本熔斷器測試（計畫 docs/superpowers/plans/2026-07-19-cost-circuit-breaker.md）。

驗收條件 1-8 逐條對應（測試名稱標註 A1..A8）＋ D5 硬規則的兩條放行路徑。
全離線：FakeAdapter/FakeExecutor 注入，無網路、無真通知。

⭐ 本檔的四個「變異測試靶」（拿掉對應實作必須轉紅，見各測試 docstring）：
  - `test_a4_*`：權益與成交名目同源同輪（D2）
  - `test_denominator_*` / `test_equity_view_*`：分母**恰為** `ev.current`（D2）
  - `test_d5_*`：reduce-only 一律放行（D5）
  - `test_a3_*`：累犯升級（D6）
"""
import dataclasses
import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import spark.copytrade.loop as loop_mod
from spark.copytrade.config import CopySettings
from spark.copytrade.costbreaker import (
    STATE_RELPATH,
    WINDOW_S,
    BreachLog,
    check_cost,
    evaluate_cost,
    is_enabled,
    load_log,
    reset_log,
    save_log,
    window_fills,
)
from spark.copytrade.executor import ActionExecutor
from spark.copytrade.notifier import RecordingNotifier
from spark.copytrade.orders import _build_desired
from spark.copytrade.positions import sync_positions
from spark.exchange.base import (
    AccountSnapshot,
    BuilderCode,
    EquityView,
    FillsTruncatedError,
    OpenOrder,
    OrderResult,
    Position,
    UserFill,
)
from spark.exchange.fakes import FakeAdapter

MY_ADDR = "0xme"
BUILDER = BuilderCode(b="0xbuilder", f=20)
NOW = 1_800_000_000.0  # 固定時間戳，避免測試依賴真實時鐘


def _settings(**kw) -> CopySettings:
    kw.setdefault("volatility_weight_enabled", False)
    return CopySettings(**kw)


def _ev(current="500", peak=None) -> EquityView:
    return EquityView(current=Decimal(current),
                      recent_peak=Decimal(peak if peak is not None else current))


def _fill(*, sz="2", px="2000", age_s=60.0, coin="ETH", now=NOW) -> UserFill:
    """一筆成交，時間為 now - age_s。名目 = sz × px。"""
    return UserFill(
        time=datetime.fromtimestamp(now - age_s, timezone.utc),
        coin=coin, px=Decimal(px), sz=Decimal(sz), side="B", crossed=True,
        oid=1, fee=Decimal("0.9"), builder_fee=Decimal("0.8"),
    )


def _fills(n: int, *, sz="2", px="2000", age_s=60.0) -> list[UserFill]:
    return [_fill(sz=sz, px=px, age_s=age_s + i) for i in range(n)]


def _crits(notifier) -> list[tuple]:
    return [r for r in notifier.records if r[0] == "critical"]


def _warns(notifier) -> list[tuple]:
    return [r for r in notifier.records if r[0] == "warn"]


# ═══════════════════════════════════════════════════════════════════
# 純函式層：換手率、滾動窗
# ═══════════════════════════════════════════════════════════════════


def test_turnover_is_notional_over_equity():
    """換手率 = 窗內成交名目 ÷ perp 權益（D1）。3 筆 × 4000 = 12000 ÷ 500 = 24×。"""
    st = check_cost(_ev("500"), _fills(3), _settings(), now_s=NOW)
    assert st.notional == Decimal("12000")
    assert st.equity == Decimal("500")
    assert st.turnover == Decimal("24")
    assert st.fill_count == 3


def test_turnover_breaches_only_above_threshold_strictly():
    """門檻語意 `>` 嚴格大於（對齊 killswitch.check_drawdown，兩道熔斷器讀法一致）。"""
    s = _settings(cost_max_turnover_24h=Decimal("24"), cost_max_fills_24h=0)
    assert check_cost(_ev("500"), _fills(3), s, now_s=NOW).breached is False, \
        "恰好等於門檻不觸發"
    s2 = _settings(cost_max_turnover_24h=Decimal("23.99"), cost_max_fills_24h=0)
    st = check_cost(_ev("500"), _fills(3), s2, now_s=NOW)
    assert st.breached is True and st.reasons == ("turnover",)


def test_fill_count_gate_is_independent_of_turnover():
    """次要度量：成交筆數。抓「換手率看不到的高頻小額對敲」（大帳戶碎單洗量）。"""
    s = _settings(cost_max_turnover_24h=0, cost_max_fills_24h=2)
    st = check_cost(_ev("1000000"), _fills(3, sz="0.001"), s, now_s=NOW)
    assert st.turnover < Decimal("0.001"), "名目佔比極小——換手率閘完全看不到"
    assert st.breached is True and st.reasons == ("fill_count",)


def test_both_reasons_reported_when_both_breach():
    s = _settings(cost_max_turnover_24h=Decimal("1"), cost_max_fills_24h=2)
    st = check_cost(_ev("500"), _fills(3), s, now_s=NOW)
    assert st.reasons == ("turnover", "fill_count")


def test_degenerate_equity_disables_turnover_but_not_fill_count():
    """權益 <= 0：換手率無定義 → 該項停用；筆數項不需要分母，照常把關。"""
    s = _settings(cost_max_turnover_24h=Decimal("1"), cost_max_fills_24h=2)
    st = check_cost(_ev("0"), _fills(3), s, now_s=NOW)
    assert st.turnover == Decimal("0")
    assert st.reasons == ("fill_count",), "分母為 0 不得算出假的換手率觸發"


# ── A6 滾動窗（D4）───────────────────────────────────────────────────
def test_a6_rolling_window_excludes_older_than_24h():
    """A6：窗以 fill 時間戳為準，窗外的成交不計入。"""
    inside = _fill(age_s=WINDOW_S - 10)
    outside = _fill(age_s=WINDOW_S + 10)
    got = window_fills([inside, outside], now_s=NOW)
    assert got == [inside]


def test_a6_rolling_window_does_not_reset_at_midnight():
    """⭐ A6：日曆日會在午夜重置，攻擊者可跨午夜規避（23:50 衝一半、00:10 衝另一半，
    兩個日曆日都不超標）。滾動窗必須把兩邊都算進來。"""
    midnight = datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc).timestamp()
    now = midnight + 30 * 60          # 00:30
    before = _fill(age_s=40 * 60, now=now)   # 前一日 23:50
    after = _fill(age_s=20 * 60, now=now)    # 當日 00:10
    st = check_cost(_ev("500"), [before, after], _settings(), now_s=now)
    assert st.fill_count == 2, "跨午夜的兩筆必須都在窗內"
    assert st.notional == Decimal("8000")


def test_window_drops_future_timestamps():
    """時鐘前跳／NTP 校正的未來戳若不丟棄，會永不出窗、永久佔住換手率額度。"""
    assert window_fills([_fill(age_s=-3600)], now_s=NOW) == []


def test_window_treats_naive_datetime_as_utc():
    naive = UserFill(time=datetime.fromtimestamp(NOW - 60, timezone.utc).replace(tzinfo=None),
                     coin="ETH", px=Decimal("2000"), sz=Decimal("2"), side="B",
                     crossed=True, oid=1, fee=Decimal("0"))
    assert len(window_fills([naive], now_s=NOW)) == 1


# ═══════════════════════════════════════════════════════════════════
# 判定歷史（**不是帳本**）
# ═══════════════════════════════════════════════════════════════════


def test_log_roundtrip(tmp_path):
    save_log(tmp_path, BreachLog(breaches=(1.0, 2.0), active=True))
    got = load_log(tmp_path)
    assert got.breaches == (1.0, 2.0) and got.active is True and got.read_error is False


def test_missing_log_is_empty_not_error(tmp_path):
    """首次啟動：檔案不存在 ≠ 讀取失敗（read_error 要能分辨兩者）。"""
    got = load_log(tmp_path)
    assert got.breaches == () and got.active is False and got.read_error is False


def test_corrupt_log_flags_read_error(tmp_path):
    p = tmp_path / STATE_RELPATH
    p.parent.mkdir(parents=True)
    p.write_text("{ not json")
    assert load_log(tmp_path).read_error is True


def test_save_log_never_raises_on_oserror(tmp_path, monkeypatch):
    """留痕失敗不得反過來擋掉保護本身（閘門的依據是本輪的 fills，不是這個檔）。"""
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr("spark.copytrade.costbreaker.os.replace", boom)
    save_log(tmp_path, BreachLog(breaches=(1.0,), active=True))  # 不得拋出


def test_reset_log_removes_state(tmp_path):
    save_log(tmp_path, BreachLog(breaches=(NOW,), active=True))
    reset_log(tmp_path)
    assert not (tmp_path / STATE_RELPATH).exists()
    reset_log(tmp_path)  # 冪等：再呼叫一次不得拋


# ═══════════════════════════════════════════════════════════════════
# evaluate_cost：狀態轉換、告警、累犯升級
# ═══════════════════════════════════════════════════════════════════


def _eval(adapter, tmp_path, *, settings=None, notifier=None, ev=None, now=NOW):
    notifier = notifier or RecordingNotifier()
    st = evaluate_cost(adapter, MY_ADDR, ev or _ev("500"), settings or _settings(),
                       notifier, tmp_path, now_fn=lambda: now, sleep_fn=lambda _s: None)
    return st, notifier


# ── A1 觸發 ─────────────────────────────────────────────────────────
def test_a1_breach_sets_flag_and_sends_critical(tmp_path):
    """A1：換手率超過門檻 → breached（呼叫端據此停開新倉）＋ critical 告警。"""
    fa = FakeAdapter(account_value=Decimal("500"), fills=_fills(3))
    st, notifier = _eval(fa, tmp_path)

    assert st.breached is True and st.reasons == ("turnover",)
    assert st.turnover == Decimal("24")
    crits = _crits(notifier)
    assert len(crits) == 1 and crits[0][1] == "costbreaker"
    assert crits[0][3] == "cost_breach"


def test_a1_critical_message_carries_rate_threshold_window_and_reason(tmp_path):
    """D5 要求告警訊息含：目前換手率、門檻、窗口、觸發了哪一項。"""
    fa = FakeAdapter(account_value=Decimal("500"), fills=_fills(3))
    _st, notifier = _eval(fa, tmp_path)
    text = _crits(notifier)[0][2]
    assert "24.00" in text, "目前換手率"
    assert "20" in text, "門檻"
    assert "24h" in text, "窗口"
    assert "換手率" in text, "觸發項"
    assert "reduce-only" in text, "必須明講平倉仍放行——客戶最怕的是被困在部位裡"


def test_a1_breach_persists_episode_in_log(tmp_path):
    fa = FakeAdapter(account_value=Decimal("500"), fills=_fills(3))
    _eval(fa, tmp_path)
    log = load_log(tmp_path)
    assert log.active is True and len(log.breaches) == 1


def test_consecutive_breach_cycles_count_as_one_episode(tmp_path):
    """⭐ 邊緣觸發：連續超標算**一次**。否則 60s 一輪的引擎會在 3 分鐘內
    自動升級到 kill switch——那不是「累犯」，那是同一次事件被數了三遍。"""
    fa = FakeAdapter(account_value=Decimal("500"), fills=_fills(3))
    notifier = RecordingNotifier()
    for _ in range(5):
        st, _ = _eval(fa, tmp_path, notifier=notifier)
    assert len(load_log(tmp_path).breaches) == 1
    assert st.escalate is False, "同一次事件不得升級"


# ── A2 自動恢復 ─────────────────────────────────────────────────────
def test_a2_recovery_clears_flag_and_alerts(tmp_path):
    """A2：換手率回落 → 自動恢復 ＋ 告警（留痕）。

    沒有自動恢復的話，一次尖峰會讓客戶停到有人發現為止。"""
    fa = FakeAdapter(account_value=Decimal("500"), fills=_fills(3))
    st1, _ = _eval(fa, tmp_path)
    assert st1.breached is True

    calm = FakeAdapter(account_value=Decimal("500"), fills=_fills(1))
    st2, notifier = _eval(calm, tmp_path)

    assert st2.breached is False
    assert load_log(tmp_path).active is False
    rec = [r for r in _warns(notifier) if r[3] == "cost_recovered"]
    assert len(rec) == 1 and "恢復" in rec[0][2]


def test_a2_recovery_keeps_breach_history_for_escalation(tmp_path):
    """恢復清掉 active，但**不**清掉窗內的觸發記錄——累犯計數要跨恢復累積，
    否則「衝上去→收手→再衝」可以無限重複而永遠升級不了。"""
    fa = FakeAdapter(account_value=Decimal("500"), fills=_fills(3))
    _eval(fa, tmp_path)
    calm = FakeAdapter(account_value=Decimal("500"), fills=_fills(1))
    _eval(calm, tmp_path)
    assert len(load_log(tmp_path).breaches) == 1


# ── A3 ⭐ 累犯升級（變異測試靶）──────────────────────────────────────
def test_a3_three_episodes_within_window_escalate(tmp_path):
    """⭐ A3 變異靶：滾動 24h 內觸發 3 次 → escalate（呼叫端 trip kill switch）。

    拿掉 evaluate_cost 的累犯升級計算 → 本測試轉紅。
    理由（D6）：純自動恢復會讓濫用**穩定在門檻上**持續進行——每次觸發後稍微收手、
    掉回門檻下自動恢復、再衝上去。升級路徑讓持續性問題必須有人看一眼。
    """
    hot = FakeAdapter(account_value=Decimal("500"), fills=_fills(3))
    calm = FakeAdapter(account_value=Decimal("500"), fills=_fills(1))

    st, _ = _eval(hot, tmp_path)
    assert (st.breached, st.escalate, st.breach_count) == (True, False, 1)
    _eval(calm, tmp_path)
    st, _ = _eval(hot, tmp_path)
    assert (st.escalate, st.breach_count) == (False, 2)
    _eval(calm, tmp_path)
    st, _ = _eval(hot, tmp_path)
    assert st.escalate is True and st.breach_count == 3


def test_a3_episodes_outside_window_do_not_escalate(tmp_path):
    """累犯計數同樣是滾動窗——25 小時前的兩次不該讓今天的第一次就升級。"""
    old = NOW - WINDOW_S - 3600
    save_log(tmp_path, BreachLog(breaches=(old, old + 60), active=False))
    hot = FakeAdapter(account_value=Decimal("500"), fills=_fills(3))
    st, _ = _eval(hot, tmp_path)
    assert st.breach_count == 1 and st.escalate is False


def test_a3_escalate_count_configurable(tmp_path):
    hot = FakeAdapter(account_value=Decimal("500"), fills=_fills(3))
    s = _settings(cost_breach_escalate_count=1)
    st, _ = _eval(hot, tmp_path, settings=s)
    assert st.escalate is True, "設為 1 ⇒ 一次即升級"


# ── A5 ⭐ 無帳本：重啟不重置 ─────────────────────────────────────────
def test_a5_turnover_survives_total_state_loss(tmp_path):
    """⭐ A5：換手率從 fills 完全重建，**不維護帳本**（D3）。

    本專案已因「帳本遺失」出過兩次事故（一次 Critical）。這裡刪掉全部持久狀態
    （＝重啟到一個沒有 volume 的新容器），換手率判定必須完全不受影響。
    """
    fa = FakeAdapter(account_value=Decimal("500"), fills=_fills(3))
    st1, _ = _eval(fa, tmp_path)
    assert st1.breached is True

    (tmp_path / STATE_RELPATH).unlink()  # 全部狀態遺失
    st2, _ = _eval(fa, tmp_path)
    assert st2.breached is True, "重啟後換手率必須從 fills 重算，不得因狀態遺失而放行"
    assert st2.turnover == st1.turnover


def test_a5_repeated_cycles_do_not_accumulate_turnover(tmp_path):
    """無帳本的另一面：同一批 fills 跑 N 輪，換手率恆等——不是累加計數器。"""
    fa = FakeAdapter(account_value=Decimal("500"), fills=_fills(3))
    seen = {_eval(fa, tmp_path)[0].turnover for _ in range(4)}
    assert seen == {Decimal("24")}


# ── D2 ⭐⭐ 分母**恰為** ev.current：EquityView 的其他欄位一律不得替代 ────
#
# 這一組測試存在的理由：D2 是本模組檔頭用 ⭐⭐ 標註、綁定兩次實盤事故的不變量，
# 但先前**只有註解在守它**——把 `check_cost` 的 `equity = ev.current` 改成
# `ev.recent_peak`，全套測試仍全綠（其餘 8 個同批變異都被咬到，只有這個活下來）。
# 原因是所有既有測試的 helper 都讓 `current == recent_peak`，分母換掉看不出來。
#
# 危害方向是單向且無聲的：`recent_peak >= current` 恆成立 ⇒ 分母只會被灌水 ⇒
# 換手率被**低估** ⇒ 熔斷器該觸發時不觸發，而且沒有任何徵兆。
#
# ⭐ 結構性（工程原則 5 的 meta-rule：不要用「記得檢查」去守不變量）：候選欄位由
# `dataclasses.fields(EquityView)` 反查，EquityView 一旦增減欄位，
# `test_equity_view_field_inventory_pins_denominator_candidates` 立刻轉紅，
# 強迫新欄位也被「誤用會轉紅」的參數化測試涵蓋——不靠下一個人記得回來補。

# 各欄位的測試值：互異、且滿足 recent_peak >= current 的語意（新欄位請沿用
# 「比 current 大一個數量級」的慣例，才會落在「分母灌水」的危害方向上）。
_DENOM_CANDIDATES: dict[str, str] = {"current": "100", "recent_peak": "1000"}
_SKEW_NOTIONAL = Decimal("2500")   # ÷100 = 25× > 20 觸發；÷1000 = 2.5× 不觸發


def _skewed_ev() -> EquityView:
    """current 與其他欄位刻意差一個數量級——分母換掉就會被算式咬到。"""
    return EquityView(**{k: Decimal(v) for k, v in _DENOM_CANDIDATES.items()})


def _skew_fills() -> list[UserFill]:
    """單筆、名目固定 _SKEW_NOTIONAL 的窗內成交（1 筆遠低於 fill_count 門檻，
    確保觸發與否只由換手率決定）。"""
    return [_fill(sz="1", px=str(_SKEW_NOTIONAL))]


def test_equity_view_field_inventory_pins_denominator_candidates():
    """⭐ EquityView 增減欄位 → 本測試轉紅，強迫新欄位補進 `_DENOM_CANDIDATES`，
    連帶被下方「誤用即轉紅」的參數化測試涵蓋。這是本組測試的結構性那一半：
    沒有它，新增一個 `lifetime_peak` 之類的欄位又會回到「只有註解守著」。"""
    names = {f.name for f in dataclasses.fields(EquityView)}
    assert names == set(_DENOM_CANDIDATES), (
        f"EquityView 欄位變了（{sorted(names)}）——新欄位同樣可能被誤用為換手率分母。"
        f"請在 _DENOM_CANDIDATES 補上一個與 current 互異的值（慣例：大一個數量級）。"
    )


def test_denominator_is_exactly_ev_current_not_any_other_field():
    """⭐⭐ 變異測試靶：`check_cost` 的分母改成 ev 的任何其他欄位 → 本測試必轉紅。

    current=100、recent_peak=1000（刻意不等），成交名目固定 2500：
    正確分母 → 25×；換成 recent_peak → 2.5×（差 10 倍，且是低估方向）。
    """
    ev = _skewed_ev()
    st = check_cost(ev, _skew_fills(), _settings(), now_s=NOW)

    assert st.notional == _SKEW_NOTIONAL
    assert st.equity == ev.current, "CostStatus.equity 必須回報實際用的分母"
    assert st.equity == Decimal("100"), "分母被換成 EquityView 的其他欄位了"
    assert st.turnover == _SKEW_NOTIONAL / ev.current == Decimal("25")


@pytest.mark.parametrize("wrong_field",
                         [n for n in _DENOM_CANDIDATES if n != "current"])
def test_denominator_swap_to_other_equity_field_flips_the_verdict(wrong_field):
    """⭐⭐ 分母換成 EquityView 的其他欄位 ⇒ **判定翻面**（保護靜默失效）。

    上一條盯數值，這一條盯後果：同一組 fills，用 `ev.current` 觸發、用其他欄位
    不觸發。參數化涵蓋 current 以外的每一個欄位，不是只擋 recent_peak 一個。
    """
    ev = _skewed_ev()
    settings = _settings()
    st = check_cost(ev, _skew_fills(), settings, now_s=NOW)

    assert st.breached is True and st.reasons == ("turnover",), \
        "以 ev.current 為分母必須觸發；未觸發代表分母被灌水了"

    wrong_turnover = _SKEW_NOTIONAL / getattr(ev, wrong_field)
    assert wrong_turnover <= settings.cost_max_turnover_24h, (
        f"測試設計檢查：改用 ev.{wrong_field} 當分母確實不會觸發，"
        f"故本測試對分母欄位敏感（{wrong_turnover}× <= "
        f"{settings.cost_max_turnover_24h}×）"
    )


def test_evaluate_cost_denominator_is_ev_current_end_to_end(tmp_path):
    """引擎接入點同樣只能用 `ev.current`——純函式對了但 evaluate_cost 自己換一個
    欄位當分母，客戶端看到的仍是失效的熔斷器。"""
    fa = FakeAdapter(account_value=Decimal("999999"), fills=_skew_fills())
    st, notifier = _eval(fa, tmp_path, ev=_skewed_ev())

    assert st.equity == Decimal("100")
    assert st.turnover == Decimal("25")
    assert st.breached is True
    assert len(_crits(notifier)) == 1, "觸發必須發 critical 告警"


# ── D2 同基準：分子也只能是主 DEX perp（現貨／非主 DEX 一律不計）───────
def test_spot_and_foreign_dex_fills_excluded_from_both_metrics():
    """⭐ 變異測試靶：拿掉 `_in_perp_scope` 過濾，本測試必轉紅。

    分母是 perp accountValue，分子若含現貨（`PURR/USDC`、`@107`）或非主 DEX
    （`xyz:NVDA`）就是混源比較（工程原則 1）。`fill_count` 必須套同一道過濾——
    否則次要度量有一模一樣的誤報路徑（客戶的現貨碎單灌爆筆數 → 誤觸）。
    """
    mixed = [
        _fill(sz="1", px="1000", coin="ETH"),          # 唯一該計入的一筆
        _fill(sz="1", px="9000", coin="PURR/USDC"),    # 現貨（交易對名稱含 '/'）
        _fill(sz="1", px="9000", coin="@107"),         # 現貨（'@' 索引代號）
        _fill(sz="1", px="9000", coin="xyz:NVDA"),     # 非主 DEX
    ]
    st = check_cost(_ev("1000"), mixed, _settings(), now_s=NOW)
    assert st.notional == Decimal("1000"), "只有主 DEX perp 的名目可以進分子"
    assert st.fill_count == 1, "fill_count 必須套用同一道過濾，否則同型誤報"
    assert st.turnover == Decimal("1")
    assert st.breached is False


def test_spot_fills_alone_cannot_trip_the_breaker(tmp_path):
    """端到端：整批都是現貨成交 ⇒ 換手率 0、不觸發、不發任何告警。

    誤報在這條路徑上不只是「偏保守」——24h 內誤觸達次數上限會累犯升級到
    killswitch.trip，強制平掉客戶的 perp 部位。
    """
    fa = FakeAdapter(account_value=Decimal("500"),
                     fills=[_fill(sz="10", px="9000", coin="PURR/USDC"),
                            _fill(sz="10", px="9000", coin="@107")])
    st, notifier = _eval(fa, tmp_path)
    assert (st.notional, st.fill_count, st.breached) == (Decimal("0"), 0, False)
    assert notifier.records == []


# ── fills 被 API 上限截斷 → fail closed，絕不靜默低估 ──────────────────
class _TruncatedAdapter(FakeAdapter):
    """get_user_fills 拋 FillsTruncatedError（HL 單次 2000 筆上限被打到）。"""

    def get_user_fills(self, address, start, end):
        raise FillsTruncatedError("成交查詢回傳筆數達單次上限（2000 筆）")


def test_truncated_fills_fail_closed_and_alert(tmp_path):
    """⭐ 截斷 ≠ 查不到：分子被**確定方向地**低估，所以 fail closed 而非沿用上一輪。

    沿用上一輪在這裡是最糟的選擇——上一輪沒觸發就照常放行，而「24h 內成交達
    2000 筆」正是這道閘門存在的理由，熔斷器會在最該作用時失效。
    """
    st, notifier = _eval(_TruncatedAdapter(account_value=Decimal("500")), tmp_path)

    assert st.truncated is True, "結果必須被明確標記為不可信"
    assert st.breached is True, "已知低估 ⇒ 停開新倉（最輕的閘），不得靜默放行"
    assert st.reasons == ("fills_truncated",)
    crits = _crits(notifier)
    assert len(crits) == 1 and crits[0][3] == "cost_fills_truncated", \
        f"截斷必須大聲，且與 cost_fills_unavailable 分開，實得 {notifier.records}"
    assert "reduce-only" in crits[0][2], "必須說明平倉仍放行（D5 硬規則不變）"


def test_truncated_fills_do_not_count_as_a_breach_episode(tmp_path):
    """資料層問題不得把客戶一路推到 kill switch 強制平倉（比照 stale 的理由）。"""
    bad = _TruncatedAdapter(account_value=Decimal("500"))
    for _ in range(5):
        st, _ = _eval(bad, tmp_path)
    assert st.escalate is False
    assert load_log(tmp_path).breaches == (), "截斷輪不得寫入觸發記錄"


def test_truncated_fills_is_not_retried(tmp_path):
    """語意錯誤：重試同一個窗口只會再拿回同樣被截斷的 2000 筆（工程原則 2）。"""
    calls = []

    class _Counting(FakeAdapter):
        def get_user_fills(self, address, start, end):
            calls.append(1)
            raise FillsTruncatedError("成交查詢回傳筆數達單次上限（2000 筆）")

    _eval(_Counting(account_value=Decimal("500")), tmp_path)
    assert len(calls) == 1, "截斷不得走 transient 重試路徑"


# ── A7 fills 查詢失敗 → transient，沿用上一輪判定 ────────────────────
class _BoomAdapter(FakeAdapter):
    """get_user_fills 一律拋 transient 錯誤（resilience 邊界會重試後放棄）。"""

    def get_user_fills(self, address, start, end):
        raise ConnectionResetError("connection reset by peer")


def test_a7_fills_failure_holds_previous_breached_verdict(tmp_path):
    """⭐ A7：查不到 fills **不得放行**。上一輪是觸發狀態 → 繼續擋。

    查不到 fills 的當下，我們對「客戶正在被磨損多快」一無所知；放行的代價是繼續
    開新倉，擋下的代價只是暫停開新倉（平倉仍放行）。兩者不對稱，故沿用上一輪判定。
    """
    save_log(tmp_path, BreachLog(breaches=(NOW - 100,), active=True))
    st, notifier = _eval(_BoomAdapter(account_value=Decimal("500")), tmp_path)

    assert st.breached is True, "上一輪觸發＋查不到 ⇒ 必須繼續擋，絕不放行"
    assert st.stale is True
    crits = _crits(notifier)
    assert len(crits) == 1 and crits[0][3] == "cost_fills_unavailable"


def test_a7_fills_failure_holds_previous_clear_verdict(tmp_path):
    """對偶：上一輪沒觸發 → 沿用「照常」，不因查不到就無故停止跟單。"""
    st, notifier = _eval(_BoomAdapter(account_value=Decimal("500")), tmp_path)
    assert st.breached is False and st.stale is True
    assert _crits(notifier)[0][3] == "cost_fills_unavailable"


def test_a7_fills_failure_does_not_corrupt_history(tmp_path):
    """stale 輪不得寫入新的觸發記錄（那會讓網路抖動累積成累犯升級）。"""
    save_log(tmp_path, BreachLog(breaches=(NOW - 100,), active=True))
    for _ in range(5):
        _eval(_BoomAdapter(account_value=Decimal("500")), tmp_path)
    assert len(load_log(tmp_path).breaches) == 1


def test_state_read_error_alerts_but_keeps_main_gate(tmp_path):
    """狀態檔壞掉：累犯計數歸零要大聲（否則保護靜默消失），但主閘照常從 fills 算。"""
    p = tmp_path / STATE_RELPATH
    p.parent.mkdir(parents=True)
    p.write_text("{ not json")
    fa = FakeAdapter(account_value=Decimal("500"), fills=_fills(3))
    st, notifier = _eval(fa, tmp_path)
    assert st.breached is True
    assert any(r[3] == "cost_state_read_error" for r in _crits(notifier))


def test_degenerate_equity_warns(tmp_path):
    fa = FakeAdapter(account_value=Decimal("0"), fills=_fills(3))
    _st, notifier = _eval(fa, tmp_path, ev=_ev("0"))
    assert any(r[3] == "cost_equity_degenerate" for r in _warns(notifier))


# ── A8 停用值 → 行為與現況完全一致 ──────────────────────────────────
def test_a8_disabled_thresholds_skip_everything(tmp_path):
    """A8：兩項門檻皆 0 ⇒ 不查 fills、不寫狀態檔、不發告警（向後相容）。"""
    s = _settings(cost_max_turnover_24h=0, cost_max_fills_24h=0)
    assert is_enabled(s) is False
    fa = FakeAdapter(account_value=Decimal("500"), fills=_fills(50))
    st, notifier = _eval(fa, tmp_path, settings=s)

    assert st.breached is False and st.enabled is False
    assert "get_user_fills" not in fa.calls, "停用時不得多打一次 API"
    assert notifier.records == []
    assert not (tmp_path / STATE_RELPATH).exists()


def test_enabled_when_either_threshold_set():
    assert is_enabled(_settings(cost_max_turnover_24h=Decimal("1"), cost_max_fills_24h=0))
    assert is_enabled(_settings(cost_max_turnover_24h=0, cost_max_fills_24h=1))


def test_negative_thresholds_rejected():
    """負門檻＝任何成交都超標＝靜默停止跟單。拒絕啟動，不靜默挑一邊。"""
    with pytest.raises(ValueError, match="cost_max_turnover_24h"):
        _settings(cost_max_turnover_24h=Decimal("-1"))
    with pytest.raises(ValueError, match="cost_max_fills_24h"):
        _settings(cost_max_fills_24h=-1)
    with pytest.raises(ValueError, match="cost_breach_escalate_count"):
        _settings(cost_breach_escalate_count=-1)


def test_env_overrides():
    s = CopySettings.from_env({
        "COPY_COST_MAX_TURNOVER_24H": "7.5",
        "COPY_COST_MAX_FILLS_24H": "42",
        "COPY_COST_BREACH_ESCALATE_COUNT": "9",
    })
    assert s.cost_max_turnover_24h == Decimal("7.5")
    assert s.cost_max_fills_24h == 42
    assert s.cost_breach_escalate_count == 9


def test_fills_query_uses_rolling_window_bounds(tmp_path):
    """查詢區間就是滾動窗（D4）——不是日曆日邊界。"""
    fa = FakeAdapter(account_value=Decimal("500"), fills=[])
    _eval(fa, tmp_path)
    call = fa.calls["get_user_fills"][0]
    assert call["address"] == MY_ADDR, "磨損發生在客戶帳上，不是 leader 帳上"
    assert (call["end"] - call["start"]) == timedelta(seconds=WINDOW_S)


# ═══════════════════════════════════════════════════════════════════
# D5 ⭐ reduce-only 一律放行（變異測試靶）
# ═══════════════════════════════════════════════════════════════════


def _order(coin="ETH", *, reduce_only=False, oid=1, is_buy=True) -> OpenOrder:
    # ro 單請配 is_buy=False（方向正確：賣單減多倉 _pos szi=1）。2026-07-28 起
    # 方向不符的 ro 比照無部位跳過（交易所會修剪到 0），見 test_copy_orders_ro_cap.py。
    return OpenOrder(oid=oid, coin=coin, is_buy=is_buy, limit_px=Decimal("2000"),
                     sz=Decimal("1"), reduce_only=reduce_only, is_trigger=False,
                     trigger_px=None, tpsl=None)


def _pos(coin="ETH", szi="1") -> Position:
    return Position(coin=coin, szi=Decimal(szi), entry_px=Decimal("2000"), leverage=5,
                    is_cross=True, unrealized_pnl=Decimal("0"), margin_used=Decimal("0"))


def _desired(orders, *, no_new_exposure, my_positions=None):
    return _build_desired(
        orders, Decimal("1"), min_notional=Decimal("10"), size_decimals=lambda c: 4,
        my_positions=my_positions if my_positions is not None else {"ETH": _pos()},
        protected=set(), no_new_exposure=no_new_exposure)


def test_d5_gated_orders_block_entries_but_pass_reduce_only():
    """⭐ D5 變異靶（掛單路徑）：拿掉 `_build_desired` 的 reduce-only 例外 → 轉紅。

    **絕不把客戶困在部位裡**——熔斷期間 leader 的平倉/止損掛單必須照常複製。
    """
    desired, skipped = _desired(
        [_order(reduce_only=False, oid=1),
         _order(reduce_only=True, oid=2, is_buy=False)],
        no_new_exposure=True)

    assert [d.reduce_only for d in desired] == [True], "只有 reduce-only 應通過"
    assert [(s.coin, s.reason) for s in skipped] == [("ETH", "cost_breaker")]


def test_d5_ungated_orders_pass_both():
    desired, skipped = _desired(
        [_order(reduce_only=False, oid=1),
         _order(reduce_only=True, oid=2, is_buy=False)],
        no_new_exposure=False)
    assert len(desired) == 2 and skipped == []


class _Ex:
    """記錄呼叫的 ExecutorPort 替身。"""

    def __init__(self):
        self.records: list[tuple] = []

    def place(self, spec):
        self.records.append(("place", spec))
        return True

    def modify(self, oid, spec):
        self.records.append(("modify", oid, spec))
        return True

    def cancel(self, coin, oid):
        self.records.append(("cancel", coin, oid))
        return True

    def market_open(self, coin, is_buy, size):
        self.records.append(("market_open", coin, is_buy, size))
        return OrderResult(ok=True, filled_size=size, avg_px=Decimal("0"), raw={})

    def close_reduce_only(self, coin, is_buy, size):
        self.records.append(("close_reduce_only", coin, is_buy, size))
        return OrderResult(ok=True, filled_size=size, avg_px=Decimal("0"), raw={})

    def update_leverage(self, coin, leverage, is_cross):
        self.records.append(("update_leverage", coin, leverage, is_cross))
        return True

    def get_open_orders(self): return []
    def get_size_decimals(self, coin): return 4


def _sync(leader, mine, *, no_new_exposure, scale="1"):
    ex, notifier = _Ex(), RecordingNotifier()
    res = sync_positions(ex, leader, mine, Decimal(scale), settings=_settings(),
                         notifier=notifier, protected=set(), size_decimals=lambda c: 4,
                         mids={"ETH": Decimal("2000"), "BTC": Decimal("60000")},
                         no_new_exposure=no_new_exposure)
    return ex, res


def _ops(ex) -> list[str]:
    return [r[0] for r in ex.records]


def test_d5_gated_positions_block_new_open():
    ex, res = _sync({"ETH": _pos()}, {}, no_new_exposure=True)
    assert "market_open" not in _ops(ex)
    assert {"coin": "ETH", "reason": "cost_breaker_open"} in res["skipped"]


def test_d5_gated_positions_block_increase():
    ex, res = _sync({"ETH": _pos(szi="2")}, {"ETH": _pos(szi="1")}, no_new_exposure=True)
    assert "market_open" not in _ops(ex)
    assert {"coin": "ETH", "reason": "cost_breaker_increase"} in res["skipped"]


def test_d5_gated_positions_still_decrease():
    """⭐ D5 變異靶（部位路徑）：減倉是 reduce-only，熔斷期間必須照常執行。"""
    ex, res = _sync({"ETH": _pos(szi="1")}, {"ETH": _pos(szi="2")}, no_new_exposure=True)
    assert ("close_reduce_only", "ETH", False, Decimal("1")) in ex.records
    assert res["adjusted"][0]["kind"] == "decrease"


def test_d5_gated_positions_still_flatten():
    """⭐ D5 變異靶：leader 已平倉 → 跟著平。這條若被擋住＝客戶被困在部位裡。"""
    ex, res = _sync({}, {"ETH": _pos(szi="1")}, no_new_exposure=True)
    assert ("close_reduce_only", "ETH", False, Decimal("1")) in ex.records
    assert res["flattened"] == [{"coin": "ETH", "side": "long", "size": Decimal("1")}]


def test_d5_gated_flip_closes_but_does_not_reopen():
    """反轉：平舊部位那一腿放行（reduce-only），反向重開被擋。
    淨效果＝曝險單向下降，正是熔斷期間想要的方向。"""
    ex, res = _sync({"ETH": _pos(szi="-1")}, {"ETH": _pos(szi="1")}, no_new_exposure=True)
    assert ("close_reduce_only", "ETH", False, Decimal("1")) in ex.records
    assert "market_open" not in _ops(ex)
    assert {"coin": "ETH", "reason": "cost_breaker_flip_open"} in res["skipped"]


def test_ungated_positions_behave_as_before():
    ex, res = _sync({"ETH": _pos()}, {}, no_new_exposure=False)
    assert "market_open" in _ops(ex) and res["opened"]


# ═══════════════════════════════════════════════════════════════════
# 引擎接線（loop.run_cycle）
# ═══════════════════════════════════════════════════════════════════


def _account(value="500", ntl="0") -> AccountSnapshot:
    return AccountSnapshot(account_value=Decimal(value), total_margin_used=Decimal("0"),
                           withdrawable=Decimal(value), total_ntl_pos=Decimal(ntl))


def _now_fills(n: int, *, sz="2", px="2000") -> list[UserFill]:
    """引擎層專用：run_cycle 內的 evaluate_cost 走**真實時鐘**（不注入 now_fn），
    所以 fills 必須相對 time.time() 產生——用純函式層的固定 NOW 會落在窗外被濾掉。"""
    now = time.time()
    return [_fill(sz=sz, px=px, age_s=60 + i, now=now) for i in range(n)]


def _run(fa, tmp_path, *, settings=None, notifier=None):
    from spark.copytrade.orders import ReconcileState
    notifier = notifier or RecordingNotifier()
    settings = settings or _settings()
    ex = ActionExecutor(fa, None, BUILDER, live=False, my_address=MY_ADDR,
                        settings=settings)
    report = loop_mod.run_cycle(fa, ex, settings, notifier, ReconcileState(), tmp_path)
    return report, notifier, ex


# ── A4 ⭐⭐ 同基準（變異測試靶）───────────────────────────────────────
def test_a4_turnover_denominator_is_perp_account_value(tmp_path):
    """⭐⭐ A4 變異靶（D2 同基準，工程原則 1）：分母必須是 `get_account_value`
    （perp accountValue，與 sizing 同一個數），且與分子同一輪同一次讀取。

    本 fake 刻意讓兩個端點分歧，複製 findings F1 的真實形態：
      perp `get_account_value` = 500（交易實際動得到的錢）
      `get_equity_view`/`get_account_state` = 5000（含 spot 的總值）
    成交名目 12000 ⇒ perp 基準 24×（觸發）／總值基準 2.4×（不觸發）。

    把 loop.py 的 `ev`（perp_equity_view 產出）換成任何其他來源 → 本測試轉紅。
    分母灌水 = 換手率被低估 = 保護靜默失效，而畫面上一切正常。
    """
    fa = FakeAdapter(
        account_value=Decimal("500"),                 # perp（正確基準）
        account=_account("5000"),                     # 含 spot 的總值（錯誤基準）
        equity=EquityView(current=Decimal("5000"), recent_peak=Decimal("5000")),
        fills=_now_fills(3),                          # 名目 12000
        positions=[_pos()], mids={"ETH": Decimal("2000")},
    )
    report, notifier, _ = _run(fa, tmp_path)

    assert any(r[3] == "cost_breach" for r in _crits(notifier)), \
        "perp 基準 24× 必須觸發；若改用含 spot 的 5000 基準只有 2.4×，保護會靜默失效"
    assert report.tripped is False, "成本熔斷只停開新倉，不是 kill switch"


class _CountingAdapter(FakeAdapter):
    """數 get_account_value 的呼叫次數（FakeAdapter 本身不記錄這個方法）。"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.equity_reads = 0

    def get_account_value(self, address):
        self.equity_reads += 1
        return super().get_account_value(address)


def test_a4_denominator_read_once_per_cycle(tmp_path):
    """⭐ D2 的「同一輪**同一次讀取**」：成本熔斷不得自己再讀一次權益。

    同一個端點讀兩次仍是混基準——兩次讀取之間權益會變（成交、mark price 跳動），
    分子用的是本輪 fills、分母若用另一次讀取的值，比較的兩個數就不同時點了。
    在 evaluate_cost 裡加一行 `adapter.get_account_value(...)` → 本測試轉紅。
    """
    fa = _CountingAdapter(account_value=Decimal("500"), account=_account("500"),
                          fills=_now_fills(1), mids={"ETH": Decimal("2000")})
    _run(fa, tmp_path)
    assert fa.equity_reads == 1, "perp_equity_view 讀一次，成本熔斷沿用同一個值"


# ── 引擎層 D5：熔斷中 reduce-only 掛單仍下得出去 ────────────────────
def _gated_adapter(orders):
    return FakeAdapter(account_value=Decimal("500"), account=_account("500"),
                       fills=_now_fills(3), open_orders=orders, positions=[_pos()],
                       mids={"ETH": Decimal("2000")}, sz_decimals={"ETH": 4})


def test_engine_gated_blocks_entry_orders(tmp_path):
    report, _n, _ex = _run(_gated_adapter([_order(reduce_only=False)]), tmp_path)
    assert report.reconcile.placed == 0


def test_engine_gated_still_places_reduce_only_orders(tmp_path):
    """⭐ 引擎層 D5：熔斷中，leader 的 reduce-only（平倉/止損）掛單照常複製。"""
    report, _n, _ex = _run(_gated_adapter([_order(reduce_only=True, is_buy=False)]),
                           tmp_path)
    assert report.reconcile.placed == 1, "reduce-only 一律放行——絕不把客戶困在部位裡"


# ── A3 引擎層：累犯升級 → trip kill switch ───────────────────────────
def test_a3_engine_escalation_trips_killswitch(tmp_path, monkeypatch):
    """A3 引擎層：累犯達標 → 呼叫 killswitch.trip（需人工 re-arm），本輪零交易。"""
    calls = []
    monkeypatch.setattr(loop_mod, "trip",
                        lambda ex, pos, n, root, status, reason="": calls.append(reason))
    save_log(tmp_path, BreachLog(breaches=(time.time() - 60, time.time() - 30),
                                 active=False))
    fa = FakeAdapter(account_value=Decimal("500"), account=_account("500"),
                     fills=_now_fills(3), positions=[_pos()],
                     mids={"ETH": Decimal("2000")})
    report, _n, _ex = _run(fa, tmp_path)

    assert calls == ["cost_breach"], "第 3 次觸發必須交給 kill switch"
    assert report.tripped is True


def test_manual_rearm_path_clears_cost_history(tmp_path):
    """trip() 一併清空成本熔斷的觸發歷史——否則人工 re-arm 後，窗內的舊記錄仍在，
    下一次觸發就立刻再度升級再鎖死，人工複查等於沒有效果（同 equity.reset_samples）。"""
    from spark.copytrade.killswitch import DrawdownStatus, trip
    save_log(tmp_path, BreachLog(breaches=(time.time(),), active=True))
    trip(_Ex(), {}, RecordingNotifier(), tmp_path,
         DrawdownStatus(current=Decimal("1"), peak=Decimal("1"),
                        drawdown_pct=Decimal("0"), breached=False),
         reason="cost_breach")
    assert load_log(tmp_path).breaches == ()


# ── A8 引擎層：停用 → 行為與現況完全一致 ────────────────────────────
def test_a8_engine_disabled_opens_normally(tmp_path):
    """A8：門檻停用 ⇒ 即使換手率爆表也照常開倉，且完全不查 fills。"""
    s = _settings(cost_max_turnover_24h=0, cost_max_fills_24h=0)
    fa = FakeAdapter(account_value=Decimal("500"), account=_account("500"),
                     fills=_fills(50), open_orders=[_order(reduce_only=False)],
                     positions=[_pos()], mids={"ETH": Decimal("2000")},
                     sz_decimals={"ETH": 4})
    report, _n, _ex = _run(fa, tmp_path, settings=s)
    assert report.reconcile.placed == 1
    assert "get_user_fills" not in fa.calls


# ── D8 優先序 ───────────────────────────────────────────────────────
def test_d8_killswitch_tripped_short_circuits_before_cost_breaker(tmp_path):
    """D8：`kill switch（回撤）` > `成本熔斷器`。已 tripped ⇒ 連 fills 都不查。

    優先序由 run_cycle 的**位置**保證（前者 return 在前），不是靠旗標檢查。
    """
    from spark.copytrade.killswitch import ARM_FILE_RELPATH
    arm = tmp_path / ARM_FILE_RELPATH
    arm.parent.mkdir(parents=True)
    arm.write_text("{}")
    fa = FakeAdapter(account_value=Decimal("500"), fills=_fills(3))
    report, _n, _ex = _run(fa, tmp_path)
    assert report.tripped is True
    assert dict(fa.calls) == {}, "tripped 短路不得有任何 adapter 呼叫"


def test_d8_drawdown_breach_takes_precedence_over_cost_breaker(tmp_path):
    """D8：回撤 breach 的那一輪走 trip 並 return，成本熔斷不得改變其行為。"""
    from spark.copytrade.equity import SAMPLES_RELPATH
    p = tmp_path / SAMPLES_RELPATH
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps([[time.time(), "1000"]]))   # peak 1000 vs current 500 → dd 0.5

    fa = FakeAdapter(account_value=Decimal("500"), account=_account("500"),
                     fills=_fills(3), positions=[_pos()], mids={"ETH": Decimal("2000")})
    report, notifier, _ex = _run(fa, tmp_path)

    assert report.tripped is True
    assert any(r[3] == "dd_breach" for r in _crits(notifier))
    assert "get_user_fills" not in fa.calls, "回撤已 return，走不到成本熔斷"
