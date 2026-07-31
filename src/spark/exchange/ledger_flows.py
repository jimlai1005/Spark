"""ledger 流量型別映射的唯一定義點。

本模組是流量映射的唯一定義點——引擎（hyperliquid.py）、preflight（vault_preflight.py）、
follower 校正（copytrade/follower_flow.py）三端共用；改這裡要三端測試都跑。
"""
from decimal import Decimal


# 白名單 + 欄位對應
FLOW_FIELDS = {
    "vaultDeposit": ("usdc", +1),
    "deposit": ("usdc", +1),
    "withdraw": ("usdc", -1),
    "vaultWithdraw": ("netWithdrawnUsd", -1),
}


def signed_flow(delta: dict) -> Decimal | None:
    """白名單內型別 + 金額欄位齊全 → 有號 USD 流量（入 +、出 −）；否則 None。

    ⚠️ vaultWithdraw 用 `netWithdrawnUsd` 而**不是** `requestedUsd`：requested 含
    commission，而 commission 是 vault 內部再分配（付給 leader），**不離開帳戶**——
    誤用 requested 會把留在帳內的錢當成流出，恆等式殘差立刻爆容差。
    """
    delta_type = delta.get("type")
    if delta_type not in FLOW_FIELDS:
        return None

    amount_field, sign = FLOW_FIELDS[delta_type]
    amount = delta.get(amount_field)
    if amount is None:
        return None

    return sign * Decimal(str(amount))


def flow_anomaly(delta: dict) -> str | None:
    """流量異常分類。

    回傳值：
    - None：正常（白名單內且欄位齊全）
    - str（型別字串）：白名單外
    - str（"{type}:missing-amount"）：白名單內但缺金額欄位
    """
    delta_type = delta.get("type")

    # 案例 1：白名單外 → 返回 type（None 型別也字串化為 "None"）
    if delta_type not in FLOW_FIELDS:
        return str(delta_type)

    # 案例 2：白名單內但缺金額欄位 → 返回 "{type}:missing-amount"
    amount_field, _ = FLOW_FIELDS[delta_type]
    if delta.get(amount_field) is None:
        return f"{delta_type}:missing-amount"

    # 案例 3：白名單內且欄位齊全 → 正常，無異常
    return None
