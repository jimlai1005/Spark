"""tests/test_ledger_flows.py
ledger 流量型別映射單一定義點（ledger_flows.py）的測試。
三端（引擎 hyperliquid.py、preflight scripts/vault_preflight.py、follower 未來）共用同一映射。
"""
from decimal import Decimal

import pytest

from spark.exchange.ledger_flows import signed_flow, flow_anomaly


class TestSignedFlow:
    """四型別白名單 + 缺欄位情況的 signed_flow 測試。"""

    def test_vault_deposit_positive(self):
        """vaultDeposit 型別 usdc 欄位 → +有號值。"""
        delta = {"type": "vaultDeposit", "usdc": "100.50"}
        assert signed_flow(delta) == Decimal("100.50")
        assert isinstance(signed_flow(delta), Decimal)

    def test_deposit_positive(self):
        """deposit 型別 usdc 欄位 → +有號值。"""
        delta = {"type": "deposit", "usdc": "50.00"}
        assert signed_flow(delta) == Decimal("50.00")

    def test_withdraw_negative(self):
        """withdraw 型別 usdc 欄位 → -有號值。"""
        delta = {"type": "withdraw", "usdc": "30.25"}
        assert signed_flow(delta) == Decimal("-30.25")

    def test_vault_withdraw_negative(self):
        """vaultWithdraw 型別 netWithdrawnUsd 欄位 → -有號值。"""
        delta = {"type": "vaultWithdraw", "netWithdrawnUsd": "20.75"}
        assert signed_flow(delta) == Decimal("-20.75")

    def test_whitelisted_type_missing_amount_field_returns_none(self):
        """白名單型別但缺金額欄位 → None（flow_anomaly 負責記 "{type}:missing-amount"）。"""
        # vaultDeposit 缺 usdc
        assert signed_flow({"type": "vaultDeposit"}) is None
        # vaultWithdraw 缺 netWithdrawnUsd
        assert signed_flow({"type": "vaultWithdraw"}) is None
        # deposit 缺 usdc
        assert signed_flow({"type": "deposit"}) is None

    def test_unknown_type_returns_none(self):
        """白名單外型別（accountTransfer、feeRebate 等）→ None（flow_anomaly 負責記）。"""
        assert signed_flow({"type": "accountTransfer", "amount": "50"}) is None
        assert signed_flow({"type": "feeRebate", "amount": "5"}) is None

    def test_none_type_returns_none(self):
        """缺 type 欄位（None）→ None（flow_anomaly 負責記 "None"）。"""
        assert signed_flow({"usdc": "100"}) is None
        assert signed_flow({}) is None

    def test_string_amount_converted_to_decimal(self):
        """金額字串轉 Decimal（API 原文轉換）。"""
        delta = {"type": "deposit", "usdc": "123.456789"}
        result = signed_flow(delta)
        assert result == Decimal("123.456789")
        assert isinstance(result, Decimal)


class TestFlowAnomaly:
    """flow_anomaly 的三態分類測試。"""

    def test_unknown_type_returns_type_string(self):
        """白名單外型別 → 回傳 type 字串本身。"""
        assert flow_anomaly({"type": "accountTransfer", "amount": "50"}) == "accountTransfer"
        assert flow_anomaly({"type": "feeRebate", "amount": "5"}) == "feeRebate"

    def test_none_type_stringified(self):
        """缺 type（None）→ 字串化為 "None"（防止 set 混型 TypeError）。"""
        assert flow_anomaly({"usdc": "100"}) == "None"
        assert flow_anomaly({}) == "None"

    def test_whitelisted_type_missing_amount_returns_missing_marker(self):
        """白名單內但缺金額欄位 → "{type}:missing-amount"。"""
        assert flow_anomaly({"type": "vaultDeposit"}) == "vaultDeposit:missing-amount"
        assert flow_anomaly({"type": "deposit"}) == "deposit:missing-amount"
        assert flow_anomaly({"type": "withdraw"}) == "withdraw:missing-amount"
        assert flow_anomaly({"type": "vaultWithdraw"}) == "vaultWithdraw:missing-amount"

    def test_whitelisted_type_with_amount_returns_none(self):
        """白名單內且欄位齊全 → None（正常，無異常）。"""
        assert flow_anomaly({"type": "vaultDeposit", "usdc": "100"}) is None
        assert flow_anomaly({"type": "deposit", "usdc": "50"}) is None
        assert flow_anomaly({"type": "withdraw", "usdc": "30"}) is None
        assert flow_anomaly({"type": "vaultWithdraw", "netWithdrawnUsd": "20"}) is None


class TestSourceUnification:
    """同源釘死測試：映射字面只能存在於 ledger_flows.py。"""

    def test_ledger_flows_module_contains_netwithdrawusd(self):
        """ledger_flows.py 應含 netWithdrawnUsd 字面（定義點）。"""
        import inspect
        from spark.exchange import ledger_flows
        source = inspect.getsource(ledger_flows)
        assert "netWithdrawnUsd" in source, "映射字面應在 ledger_flows.py 定義"

    def test_hyperliquid_does_not_contain_netwithdrawusd(self):
        """hyperliquid.py get_ledger_flows 不再含 netWithdrawnUsd（改用 import）。"""
        import inspect
        from spark.exchange import hyperliquid
        source = inspect.getsource(hyperliquid.HyperliquidAdapter.get_ledger_flows)
        assert "netWithdrawnUsd" not in source, "hyperliquid.py 應 import，不再硬編"

    def test_vault_preflight_does_not_contain_netwithdrawusd(self):
        """vault_preflight.py 不再含 netWithdrawnUsd（改用 import）。"""
        # 讀原始碼檔案（避免 import 路徑問題）
        with open("/Users/jim/projects/spark/scripts/vault_preflight.py") as f:
            source = f.read()
        # _signed_flow 函式內應無 netWithdrawnUsd（已改 import）
        lines = source.split("\n")
        in_signed_flow = False
        for i, line in enumerate(lines):
            if "def _signed_flow" in line:
                in_signed_flow = True
            elif in_signed_flow and line and not line[0].isspace():
                # 出函式
                break
            if in_signed_flow and "netWithdrawnUsd" in line:
                pytest.fail(f"vault_preflight._signed_flow 不應含 netWithdrawnUsd（行 {i+1}）")


class TestRegressionCasesFromExistingTests:
    """從既有測試搬移的邊界情況。"""

    def test_missing_type_stringified_in_anomaly(self):
        """迴歸 test_hyperliquid_reads.py:test_get_ledger_flows_missing_type_is_stringified_not_typeerror
        delta 缺 type（None）應被 flow_anomaly 字串化為 "None"，不得炸。"""
        delta = {"usdc": "100"}  # 缺 type
        anomaly = flow_anomaly(delta)
        assert anomaly == "None"
        assert isinstance(anomaly, str)
        # 下游 ", ".join([anomaly, ...]) 操作必須可行
        ", ".join([anomaly, "other"])

    def test_missing_amount_flagged_with_type_label(self):
        """迴歸 test_hyperliquid_reads.py:test_get_ledger_flows_whitelisted_type_missing_amount_is_flagged_and_skipped
        白名單型別缺金額 → "{type}:missing-amount" 進異常清單。"""
        vault_deposit_no_usdc = {"type": "vaultDeposit"}
        vault_withdraw_no_netwithdrawusd = {"type": "vaultWithdraw"}

        assert flow_anomaly(vault_deposit_no_usdc) == "vaultDeposit:missing-amount"
        assert flow_anomaly(vault_withdraw_no_netwithdrawusd) == "vaultWithdraw:missing-amount"
        # signed_flow 應一律回 None（不進 flows）
        assert signed_flow(vault_deposit_no_usdc) is None
        assert signed_flow(vault_withdraw_no_netwithdrawusd) is None
