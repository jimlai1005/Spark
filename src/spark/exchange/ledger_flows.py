"""ledger 流量型別映射的唯一定義點。

本模組是流量映射的唯一定義點——引擎（hyperliquid.py）、preflight
（vault_preflight.py，public API 准入 advisory 檢查亦取道其 run_checks）、
follower 校正（copytrade/follower_flow.py）三端共用；改這裡要三端測試都跑。
型別字面（含 accountClassTransfer）只准出現在本檔與測試——呼叫端一律取道
signed_flow／flow_anomaly，不得自寫型別分支。
"""
from decimal import Decimal


# 白名單 + 欄位對應（金額欄位, 固定號）
FLOW_FIELDS = {
    "vaultDeposit": ("usdc", +1),
    "deposit": ("usdc", +1),
    "withdraw": ("usdc", -1),
    "vaultWithdraw": ("netWithdrawnUsd", -1),
}

# perp↔spot 內部劃轉（owner 出金的必經路徑，2026-07-21 實測情境）：金額欄位
# usdc、號**不固定**——方向由 toPerp 決定（truthy → +usdc 進 perp；falsy →
# −usdc 出 perp），塞不進 FLOW_FIELDS 的 (欄位, 固定號) 結構，故以特例分支處理。
ACCOUNT_CLASS_TRANSFER = "accountClassTransfer"


def signed_flow(delta: dict) -> Decimal | None:
    """白名單內型別 + 欄位齊全 → 有號 USD 流量（入 perp +、出 perp −）；否則 None。

    ⚠️ vaultWithdraw 用 `netWithdrawnUsd` 而**不是** `requestedUsd`：requested 含
    commission，而 commission 是 vault 內部再分配（付給 leader），**不離開帳戶**——
    誤用 requested 會把留在帳內的錢當成流出，恆等式殘差立刻爆容差。

    accountClassTransfer 缺 `usdc` 或缺 `toPerp` **鍵** → None（方向不明不得
    猜號；由 flow_anomaly 記 missing-amount／missing-direction）。
    """
    delta_type = delta.get("type")

    if delta_type == ACCOUNT_CLASS_TRANSFER:
        amount = delta.get("usdc")
        if amount is None or "toPerp" not in delta:
            return None
        sign = 1 if delta["toPerp"] else -1
        return sign * Decimal(str(amount))

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
    - str（"accountClassTransfer:missing-direction"）：缺 toPerp 鍵（方向不明）
    """
    delta_type = delta.get("type")

    # 特例：accountClassTransfer（號不固定，見 ACCOUNT_CLASS_TRANSFER 註解）
    if delta_type == ACCOUNT_CLASS_TRANSFER:
        if delta.get("usdc") is None:
            return f"{delta_type}:missing-amount"
        if "toPerp" not in delta:
            return f"{delta_type}:missing-direction"
        return None

    # 案例 1：白名單外 → 返回 type（None 型別也字串化為 "None"）
    if delta_type not in FLOW_FIELDS:
        return str(delta_type)

    # 案例 2：白名單內但缺金額欄位 → 返回 "{type}:missing-amount"
    amount_field, _ = FLOW_FIELDS[delta_type]
    if delta.get(amount_field) is None:
        return f"{delta_type}:missing-amount"

    # 案例 3：白名單內且欄位齊全 → 正常，無異常
    return None
