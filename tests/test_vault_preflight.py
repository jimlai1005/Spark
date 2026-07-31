"""tests/test_vault_preflight.py
vault leader 上架前 preflight 六檢查——fixture 數字取自 Ultron vault 實測
（spec §1，2026-07-31）：恆等式殘差 ~$0.08 應 PASS。全離線（conftest socket-ban）。"""
import pytest

from scripts.vault_preflight import (
    PreflightData,
    main,
    run_checks,
)
from spark.exchange.ledger_flows import ACCOUNT_CLASS_TRANSFER, FLOW_FIELDS

# Ultron month 窗首末點時間戳（實測）
TS0 = 1782774120062
TS1 = 1785416679041
VAULT = "0x" + "ab" * 20

EXPECTED_NAMES = [
    "is-vault",
    "perp-resident-tvl",
    "flow-neutral-pnl",
    "no-spot-pollution",
    "flow-stats",
    "ledger-type-whitelist",
]


def _clearinghouse():
    return {
        "marginSummary": {"accountValue": "902154.206736"},
        "withdrawable": "645277.220236",
    }


def _vault_details(max_distributable=645277.220236):
    return {
        "name": "Ultron",
        "leaderFraction": 0.2405,
        "maxDistributable": max_distributable,
        "isClosed": False,
        "followers": [
            {"user": "0x" + "11" * 20, "vaultEquity": "1000.0"},
            {"user": "0x" + "22" * 20, "vaultEquity": "2000.0"},
        ],
    }


def _period(pnl_end):
    return {
        "accountValueHistory": [
            [TS0, "857543.602782"],
            [TS0 + 86_400_000, "860000.0"],
            [TS1, "902154.206736"],
        ],
        "pnlHistory": [[TS0, "0.0"], [TS1, pnl_end]],
        "vlm": "0.0",
    }


def _portfolio(month_pnl_end="81606.703954", perp_month_pnl_end="81606.703954"):
    # 混入其他 period 證明挑窗看名字、不靠位置
    return [
        ["day", _period("1.0")],
        ["month", _period(month_pnl_end)],
        ["perpMonth", _period(perp_month_pnl_end)],
    ]


def _ledger(extra=()):
    """deposits 合計 +1010（兩筆）、withdrawals netWithdrawnUsd 合計 38006.1768（三筆）。
    vaultWithdraw 帶著更大的 requestedUsd：實作若誤用 requestedUsd（把留在帳內的
    commission 當流出），殘差會爆容差、檢查 3 轉 FAIL——結構上防呆。"""
    entries = [
        {"time": TS0 + 1_000, "hash": "0x1",
         "delta": {"type": "deposit", "usdc": "500"}},
        {"time": TS0 + 2_000, "hash": "0x2",
         "delta": {"type": "vaultDeposit", "vault": VAULT, "usdc": "510"}},
        {"time": TS0 + 3_000, "hash": "0x3",
         "delta": {"type": "withdraw", "usdc": "1000", "nonce": 1, "fee": "1"}},
        {"time": TS0 + 4_000, "hash": "0x4",
         "delta": {"type": "vaultWithdraw", "vault": VAULT, "user": "0x" + "11" * 20,
                   "requestedUsd": "30303.03", "commission": "303.03",
                   "closingCost": "0.0", "basis": "29000.0",
                   "netWithdrawnUsd": "30000.0"}},
        {"time": TS0 + 5_000, "hash": "0x5",
         "delta": {"type": "vaultWithdraw", "vault": VAULT, "user": "0x" + "22" * 20,
                   "requestedUsd": "7077.0", "commission": "70.82",
                   "closingCost": "0.0", "basis": "7000.0",
                   "netWithdrawnUsd": "7006.1768"}},
    ]
    entries.extend(extra)
    return entries


def _data(**over):
    base = {
        "clearinghouse_state": _clearinghouse(),
        "vault_details": _vault_details(),
        "portfolio": _portfolio(),
        "ledger_updates": _ledger(),
    }
    base.update(over)
    return PreflightData(**base)


def _by_name(results):
    return {r.name: r for r in results}


# ── PASS 全套 ────────────────────────────────────────────────────────────


def test_all_six_checks_pass_on_ultron_fixture():
    results = run_checks(_data())
    assert [r.name for r in results] == EXPECTED_NAMES
    assert all(r.passed for r in results), [(r.name, r.detail) for r in results]


def test_is_vault_detail_has_name_fraction_followers_closed():
    r = _by_name(run_checks(_data()))["is-vault"]
    assert "Ultron" in r.detail
    assert "0.2405" in r.detail
    assert "followers=2" in r.detail
    assert "isClosed=False" in r.detail


def test_flow_neutral_residual_is_about_8_cents():
    """實測錨例：ΔAV 44610.603954 − pnl 81606.703954 − 淨流量 (−36996.1768) ≈ $0.0768。"""
    r = _by_name(run_checks(_data()))["flow-neutral-pnl"]
    assert r.passed
    assert "0.0768" in r.detail


# ── FAIL 案例（各一）─────────────────────────────────────────────────────


def test_check2_fails_when_max_distributable_off_by_5_dollars():
    by = _by_name(run_checks(_data(vault_details=_vault_details(645282.220236))))
    assert not by["perp-resident-tvl"].passed
    assert by["is-vault"].passed  # 其他檢查不受牽連


def test_check3_fails_when_pnl_off_by_100_dollars():
    # month 與 perpMonth 一起改差 $100：檢查 4（兩窗一致）仍 PASS，只有恆等式 FAIL
    p = _portfolio(month_pnl_end="81706.703954", perp_month_pnl_end="81706.703954")
    by = _by_name(run_checks(_data(portfolio=p)))
    assert not by["flow-neutral-pnl"].passed
    assert by["no-spot-pollution"].passed


def test_check4_fails_when_perp_month_pnl_off_by_1_dollar():
    p = _portfolio(perp_month_pnl_end="81607.703954")
    by = _by_name(run_checks(_data(portfolio=p)))
    assert not by["no-spot-pollution"].passed
    assert by["flow-neutral-pnl"].passed  # 恆等式看 month 窗，未被動到


def test_empty_month_histories_fail_checks_3_and_4_without_traceback():
    """回歸：month 窗存在但 history 為空（新 vault 歷史不足）曾讓 avh[-1]
    IndexError——閘門腳本吐 traceback。修法：檢查 3/4 對空 history 回 FAIL。"""
    empty = {"accountValueHistory": [], "pnlHistory": [], "vlm": "0.0"}
    p = [["day", _period("1.0")], ["month", empty], ["perpMonth", empty]]
    results = run_checks(_data(portfolio=p))  # 不得拋例外
    by = _by_name(results)
    assert not by["flow-neutral-pnl"].passed
    assert "月窗樣本為空" in by["flow-neutral-pnl"].detail
    assert not by["no-spot-pollution"].passed
    assert "月窗樣本為空" in by["no-spot-pollution"].detail
    # 其餘檢查照常執行、不受牽連
    assert by["is-vault"].passed
    assert by["perp-resident-tvl"].passed
    assert by["ledger-type-whitelist"].passed
    assert [r.name for r in results] == EXPECTED_NAMES


def test_check6_fails_on_unknown_ledger_type():
    extra = [{"time": TS0 + 6_000, "hash": "0x6",
              "delta": {"type": "spotTransfer", "usdc": "5.0"}}]
    by = _by_name(run_checks(_data(ledger_updates=_ledger(extra))))
    assert not by["ledger-type-whitelist"].passed
    assert "spotTransfer" in by["ledger-type-whitelist"].detail
    # 未知型別不計入淨流量：恆等式不因此翻船（檢查 6 已負責把人叫來）
    assert by["flow-neutral-pnl"].passed


def test_check6_passes_on_complete_account_class_transfer():
    """F2 同步：accountClassTransfer（perp↔spot 劃轉）已入白名單——欄位齊全時
    檢查 6 PASS，且流量**計入**恆等式（+5 遠小於容差 ~$90，檢查 3 仍 PASS）。"""
    extra = [{"time": TS0 + 6_000, "hash": "0x6",
              "delta": {"type": "accountClassTransfer", "usdc": "5.0", "toPerp": True}}]
    by = _by_name(run_checks(_data(ledger_updates=_ledger(extra))))
    assert by["ledger-type-whitelist"].passed
    assert by["flow-neutral-pnl"].passed


def test_check6_fails_on_whitelisted_type_with_missing_fields():
    """觀察 (a)：白名單內但缺金額／方向欄位＝該筆流量被靜默漏計（signed_flow 回
    None）——檢查 6 必須 FAIL，不得 PASS（改用 flow_anomaly 判定後涵蓋）。"""
    extra = [{"time": TS0 + 6_000, "hash": "0x6",
              "delta": {"type": "accountClassTransfer", "usdc": "5.0"}},  # 缺 toPerp
             {"time": TS0 + 6_500, "hash": "0x6b",
              "delta": {"type": "deposit"}}]                              # 缺 usdc
    by = _by_name(run_checks(_data(ledger_updates=_ledger(extra))))
    assert not by["ledger-type-whitelist"].passed
    assert "accountClassTransfer:missing-direction" in by["ledger-type-whitelist"].detail
    assert "deposit:missing-amount" in by["ledger-type-whitelist"].detail


# ── 檢查 5：資訊性，恆為 PASS，超標印 WARN ───────────────────────────────


def test_check5_is_informational_and_warns_on_large_single_flow():
    # 加一筆 80000 deposit（8.87% TVL > 8%），pnl 同步 −80000 保持恆等式成立
    extra = [{"time": TS0 + 7_000, "hash": "0x7",
              "delta": {"type": "deposit", "usdc": "80000"}}]
    p = _portfolio(month_pnl_end="1606.703954", perp_month_pnl_end="1606.703954")
    by = _by_name(run_checks(_data(ledger_updates=_ledger(extra), portfolio=p)))
    r = by["flow-stats"]
    assert r.passed  # 資訊性檢查不 FAIL
    assert "WARN" in r.detail
    assert by["flow-neutral-pnl"].passed


def test_check5_no_warn_on_baseline():
    r = _by_name(run_checks(_data()))["flow-stats"]
    assert r.passed
    assert "WARN" not in r.detail
    assert "筆數=5" in r.detail  # 兩筆入金＋三筆出金


def test_allowed_ledger_types_frozen():
    """白名單釘死（唯一定義點在 ledger_flows；F2 起含 accountClassTransfer）。"""
    assert set(FLOW_FIELDS) | {ACCOUNT_CLASS_TRANSFER} == {
        "deposit", "vaultDeposit", "withdraw", "vaultWithdraw",
        "accountClassTransfer"}


# ── main：exit code 與 ledger 查詢窗 ─────────────────────────────────────


class _FakeGateway:
    def __init__(self, data: PreflightData):
        self._d = data
        self.calls = []

    def clearinghouse_state(self, address):
        self.calls.append(("clearinghouseState", address))
        return self._d.clearinghouse_state

    def vault_details(self, vault_address):
        self.calls.append(("vaultDetails", vault_address))
        return self._d.vault_details

    def portfolio(self, address):
        self.calls.append(("portfolio", address))
        return self._d.portfolio

    def non_funding_ledger_updates(self, user, start_ms):
        self.calls.append(("ledger", user, start_ms))
        return self._d.ledger_updates


def test_main_exit_zero_and_prints_six_pass_lines(capsys):
    gw = _FakeGateway(_data())
    with pytest.raises(SystemExit) as ei:
        main([VAULT], gateway=gw)
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert out.count("PASS ") == 6
    assert "FAIL" not in out


def test_main_exit_one_when_any_check_fails(capsys):
    gw = _FakeGateway(_data(vault_details=_vault_details(645282.220236)))
    with pytest.raises(SystemExit) as ei:
        main([VAULT], gateway=gw)
    assert ei.value.code == 1
    assert "FAIL perp-resident-tvl" in capsys.readouterr().out


def test_main_ledger_window_starts_at_month_first_timestamp(capsys):
    """ledger 查詢窗用 portfolio month 窗首點時間戳（spec 檢查 3 的取窗規則）。"""
    gw = _FakeGateway(_data())
    with pytest.raises(SystemExit):
        main([VAULT], gateway=gw)
    assert ("ledger", VAULT, TS0) in gw.calls
