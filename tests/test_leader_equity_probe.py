"""scripts/leader_equity_probe 離線測試：假 info 物件餵固定回應，不連網。

數值錨例取自 recon §2／§4（2026-11-22 13:57:10Z leader 0xfb9c…）：
USDC total 209.48、UBTC 0.0081968513、perp accountValue 120.58、portfolio 707.37。
"""
import json
from decimal import Decimal

import pytest

from scripts import leader_equity_probe as probe

ADDR = "0x" + "ab" * 20
NOW_MS = 1_800_000_000_000
USDC, UBTC, HYPE = 0, 197, 150
UBTC_MARK = "60743.0"      # 0.0081968513 × 60743.0 ≈ 497.90（recon §4 的 $497.89）
HYPE_MARK = "40.0"

META = [
    {
        "tokens": [
            {"name": "USDC", "index": USDC},
            {"name": "HYPE", "index": HYPE},
            {"name": "UBTC", "index": UBTC},
            {"name": "NOQUOTE", "index": 999},   # 沒有任何 USDC pair
        ],
        "universe": [
            {"name": "@107", "tokens": [HYPE, USDC], "index": 107, "isCanonical": True},
            {"name": "@142", "tokens": [UBTC, USDC], "index": 142, "isCanonical": True},
        ],
    },
    [{"coin": "@107", "markPx": HYPE_MARK}, {"coin": "@142", "markPx": UBTC_MARK}],
]


class FakeInfo:
    """只實作探針會碰的面：post（原始端點）、portfolio、user_state、frontend_open_orders。"""

    def __init__(self, *, mode="unifiedAccount", portfolio_value="707.37",
                 portfolio_age_ms=1000, balances=None, delegated="0", vaults=None,
                 perp_av=None, perp_dexs=None, positions=None, orders=None):
        self.mode = mode
        self.portfolio_value = portfolio_value
        self.portfolio_age_ms = portfolio_age_ms
        self.balances = balances if balances is not None else [
            {"coin": "USDC", "token": USDC, "total": "209.48", "hold": "121.57"},
            {"coin": "UBTC", "token": UBTC, "total": "0.0081968513", "hold": "0.0"},
        ]
        self.delegated = delegated
        self.vaults = vaults or []
        self.perp_av = perp_av or {"": "120.58"}
        self.perp_dexs = perp_dexs if perp_dexs is not None else [None]
        self.positions = positions or []
        self.orders = orders or []
        self.post_types: list[str] = []

    def post(self, path, payload):
        assert path == "/info"
        t = payload["type"]
        self.post_types.append(t)
        if t == "userAbstraction":
            return self.mode
        if t == "spotClearinghouseState":
            return {"balances": self.balances}
        if t == "spotMetaAndAssetCtxs":
            return META
        if t == "delegatorSummary":
            return {"delegated": self.delegated, "undelegated": "0.0",
                    "totalPendingWithdrawal": "0.0", "nPendingWithdrawals": 0}
        if t == "userVaultEquities":
            return self.vaults
        if t == "perpDexs":
            return self.perp_dexs
        if t == "clearinghouseState":
            return {"marginSummary": {"accountValue": self.perp_av[payload["dex"]]}}
        raise AssertionError(f"探針不該打 {t}")

    # --- 正式路徑（HyperliquidAdapter 會呼叫的 SDK 方法）---
    def portfolio(self, address):
        ts = NOW_MS - self.portfolio_age_ms
        return [["day", {"accountValueHistory": [[ts - 60_000, "1.0"], [ts, self.portfolio_value]]}],
                ["perpDay", {"accountValueHistory": [[ts + 5_000, "120.58"]]}]]

    def user_state(self, address, dex=""):
        return {"assetPositions": self.positions, "marginSummary": {"accountValue": "0"}}

    def frontend_open_orders(self, address, dex=""):
        return self.orders


@pytest.fixture
def patch_env(monkeypatch):
    def _install(fake):
        built = []

        def _build():
            built.append(1)
            return fake

        monkeypatch.setattr(probe, "_build_info", _build)
        monkeypatch.setattr(probe, "_now_ms", lambda: NOW_MS)
        return built
    return _install


def run(argv):
    return probe.main(argv)


# (1) 統一帳戶：重建值不含 perp accountValue
def test_unified_account_excludes_perp_account_value(patch_env, capsys):
    fake = FakeInfo(mode="unifiedAccount", perp_av={"": "120.58"})
    patch_env(fake)
    assert run([ADDR]) == 0
    out = capsys.readouterr().out
    assert "PASS" in out and "mode=unifiedAccount" in out and "perp_av=120.58" in out
    rb = probe.fetch_rebuild(fake, ADDR)
    expected = Decimal("209.48") + Decimal("0.0081968513") * Decimal(UBTC_MARK)
    assert rb.total == expected                       # 120.58 沒被加進去
    assert rb.perp_added == 0 and rb.perp_default == Decimal("120.58")
    assert abs(rb.total - Decimal("707.37")) < Decimal("0.02")   # recon §4 錨例


# (2) disabled：重建值含所有 dex 的 perp accountValue
def test_disabled_mode_adds_all_dex_perp_account_values(patch_env):
    fake = FakeInfo(
        mode="disabled", portfolio_value="360.0",
        balances=[{"coin": "USDC", "token": USDC, "total": "10", "hold": "0"}],
        perp_av={"": "100", "xyz": "50", "abc": "200"},
        perp_dexs=[None, {"name": "xyz"}, {"name": "abc"}])
    patch_env(fake)
    rb = probe.fetch_rebuild(fake, ADDR)
    assert rb.perp_added == Decimal("350")
    assert rb.total == Decimal("360")
    assert rb.perp_by_dex == {"": Decimal("100"), "xyz": Decimal("50"), "abc": Decimal("200")}
    assert run([ADDR]) == 0


# (2b) default：與 disabled 同一條恆等式（2026-11-23 主網 vault leader 實測）
def test_default_mode_adds_perp_account_values_like_disabled(patch_env):
    fake = FakeInfo(
        mode="default", portfolio_value="360.0",
        balances=[{"coin": "USDC", "token": USDC, "total": "10", "hold": "0"}],
        perp_av={"": "100", "xyz": "50", "abc": "200"},
        perp_dexs=[None, {"name": "xyz"}, {"name": "abc"}])
    patch_env(fake)
    rb = probe.fetch_rebuild(fake, ADDR)
    assert rb.perp_added == Decimal("350")
    assert rb.total == Decimal("360")
    assert rb.perp_by_dex == {"": Decimal("100"), "xyz": Decimal("50"), "abc": Decimal("200")}
    assert run([ADDR]) == 0


# (3) 質押與 vault 有被加總
def test_staking_and_vault_are_summed(patch_env):
    fake = FakeInfo(
        mode="unifiedAccount", portfolio_value="4260.0",
        balances=[{"coin": "USDC", "token": USDC, "total": "10", "hold": "0"}],
        delegated="100", vaults=[{"vaultAddress": "0x1", "equity": "200.5"},
                                 {"vaultAddress": "0x2", "equity": "49.5"}])
    patch_env(fake)
    rb = probe.fetch_rebuild(fake, ADDR)
    assert rb.staking == Decimal("4000.0")        # 100 HYPE × 40
    assert rb.vault == Decimal("250.0")
    assert rb.total == Decimal("4260.0")
    assert run([ADDR]) == 0


# (4) 差額超過容差 → FAIL、exit 1（且重試一次）
def test_difference_over_tolerance_fails_and_exit_1(patch_env, capsys, tmp_path):
    fake = FakeInfo(portfolio_value="800.00")        # 重建 ≈ 707.38，差約 -92.6
    patch_env(fake)
    out_path = tmp_path / "o.jsonl"
    assert run(["--out", str(out_path), ADDR]) == 1
    out = capsys.readouterr().out
    assert out.startswith("FAIL") and "retried" in out
    row = json.loads(out_path.read_text().splitlines()[0])
    assert row["verdict"] == "FAIL" and row["retried"] is True and row["attempts"] == 2
    assert row["formal"]["value"] == "800.00" and row["formal"]["age_s"] == "1"
    assert Decimal(row["diff"]) < Decimal("-90")


def test_tolerance_flags_are_honoured(patch_env):
    patch_env(FakeInfo(portfolio_value="707.00"))    # 差約 0.38
    assert run(["--tolerance-abs", "0.1", "--tolerance-pct", "0", ADDR]) == 1
    assert run(["--tolerance-abs", "0.5", "--tolerance-pct", "0", ADDR]) == 0


# (5) 有未報價代幣 → UNPRICED
def test_unpriced_token_marks_unpriced(patch_env, capsys):
    fake = FakeInfo(
        portfolio_value="1000.0",
        balances=[{"coin": "USDC", "token": USDC, "total": "10", "hold": "0"},
                  {"coin": "NOQUOTE", "token": 999, "total": "5", "hold": "0"}])
    patch_env(fake)
    assert run([ADDR]) == 1
    out = capsys.readouterr().out
    assert out.startswith("UNPRICED") and "unpriced=NOQUOTE" in out


# (6) 位址格式錯 → exit 2、沒有任何網路物件被建立
@pytest.mark.parametrize("bad", ["0x123", "ab" * 21, "0x" + "zz" * 20, "0x" + "ab" * 21])
def test_bad_address_exit_2_without_network(patch_env, bad):
    built = patch_env(FakeInfo())
    assert run([bad]) == 2
    assert run([ADDR, bad]) == 2
    assert built == []


def test_no_address_is_argparse_error(patch_env):
    built = patch_env(FakeInfo())
    with pytest.raises(SystemExit) as ei:
        run([])
    assert ei.value.code == 2 and built == []


# (7) --help 不建立網路物件
def test_help_builds_no_network_objects(patch_env, capsys):
    built = patch_env(FakeInfo())
    with pytest.raises(SystemExit) as ei:
        run(["--help"])
    assert ei.value.code == 0 and built == []
    assert "leader_equity_probe" in capsys.readouterr().out


# --- 補充：正式路徑語意 ---
def test_stale_total_with_exposure_is_fail_with_reason(patch_env, capsys):
    pos = [{"position": {"coin": "BTC", "szi": "-0.02", "entryPx": "63000",
                         "leverage": {"type": "cross", "value": 20},
                         "unrealizedPnl": "0", "marginUsed": "10"}}]
    fake = FakeInfo(portfolio_age_ms=181_000, positions=pos)
    patch_env(fake)
    assert run([ADDR]) == 1
    out = capsys.readouterr().out
    assert out.startswith("FAIL") and "UNAVAILABLE" in out and "已過期 181 秒" in out


def test_open_orders_count_as_exposure(patch_env):
    fake = FakeInfo(portfolio_age_ms=181_000, orders=[{
        "oid": 1, "coin": "BTC", "side": "A", "limitPx": "63000", "sz": "0.01"}])
    patch_env(fake)
    res = probe.probe_once(probe.HyperliquidAdapter("mainnet", info=fake), fake, ADDR,
                           now_ms_fn=lambda: NOW_MS, tol_abs=Decimal("1"), tol_pct=Decimal("0.001"))
    assert res.has_exposure is True and res.verdict == "FAIL"


def test_stale_total_without_exposure_passes_through(patch_env):
    patch_env(FakeInfo(portfolio_age_ms=181_000))     # D3a：無曝險永不 raise
    assert run([ADDR]) == 0


def test_read_exception_is_reported_with_type_and_exit_1(patch_env, capsys):
    class Boom(FakeInfo):
        def post(self, path, payload):
            raise ConnectionError("boom")

    patch_env(Boom())
    assert run([ADDR]) == 1
    out = capsys.readouterr().out
    assert out.startswith("ERROR") and "ConnectionError: boom" in out


def test_rebuild_never_touches_portfolio_endpoint_and_multiple_addresses(patch_env):
    fake = FakeInfo(mode="disabled", portfolio_value="120.58",
                    balances=[{"coin": "USDC", "token": USDC, "total": "0", "hold": "0"}])
    patch_env(fake)
    assert run([ADDR, "0x" + "cd" * 20]) == 0
    assert "portfolio" not in fake.post_types
    assert fake.post_types.count("userAbstraction") == 2
