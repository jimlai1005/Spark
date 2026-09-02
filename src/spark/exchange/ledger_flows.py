"""ledger 流量型別映射的唯一定義點。

本模組是流量映射的唯一定義點——引擎（hyperliquid.py）、preflight
（vault_preflight.py，public API 准入 advisory 檢查亦取道其 run_checks）、
follower 校正（copytrade/follower_flow.py）三端共用；改這裡要三端測試都跑。
型別字面（含 accountClassTransfer）只准出現在本檔與測試——呼叫端一律取道
signed_flow／flow_anomaly，不得自寫型別分支。
"""
from decimal import Decimal, InvalidOperation


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

# 錢包對錢包轉帳（HL UI 的「Send」／SDK 的 usdSend，2026-09-02 testnet 實測，見
# T9）：雙方 ledger 都收到同一筆 `{"type": "internalTransfer", "usdc": "...",
# "user": "<sender>", "destination": "<receiver>", "fee": "..."}`——與
# accountClassTransfer 同樣塞不進 (欄位, 固定號) 結構，但號**不是**由某個布林欄位
# 決定，而是由**呼叫端查詢的帳戶位址**是 `user`（送方，−usdc）還是 `destination`
# （收方，+(usdc−fee)）決定。因此只有這個型別的 signed_flow／flow_anomaly 需要
# `address` 參數——其餘型別的白名單判定與帳戶位址無關，維持不變。
# 2026-09-02 主線程 testnet 實測補兩點（回應審查殘留疑慮）：(1) **spot** 錢包對外送
# USDC 的 ledger 型別是 `send`（帶 sourceDex/destinationDex/token/usdcValue），不是
# internalTransfer——所以本型別只代表 perp 桶的 usdSend，直接平移 perp basis 是對的；
# `send` 不在白名單，維持 unknown-type → 只 warn（fail-safe 方向不變）。(2) HL 對
# 自轉自回 `Cannot self-transfer.`，`_internal_transfer_direction` 的 "self" 分支是純防禦。
INTERNAL_TRANSFER = "internalTransfer"


def _internal_transfer_direction(delta: dict, address: str | None) -> str | None:
    """純字串比對（不牽涉數字解析）：查詢位址是 destination／user／兩者皆是
    （自轉自）／都不是。供 `_internal_transfer_flow` 與 `flow_anomaly` 共用，
    讓「方向不明」與「方向已知但金額解析失敗」能被分開分類。
    """
    if address is None:
        return None
    addr = address.lower()
    destination = delta.get("destination")
    user = delta.get("user")
    is_destination = isinstance(destination, str) and destination.lower() == addr
    is_user = isinstance(user, str) and user.lower() == addr
    if is_destination and is_user:
        return "self"
    if is_destination:
        return "destination"
    if is_user:
        return "user"
    return None


def _internal_transfer_flow(delta: dict, address: str | None) -> Decimal | None:
    """internalTransfer 的方向判定，供 signed_flow 呼叫。

    fee 缺鍵時視為 0（不是 None／anomaly）：實測兩種真實 payload（新地址
    fee="1.0"、既有地址 fee="0.0"）都**明確帶著** fee 鍵，缺鍵不是本型別的正常
    形狀，較可能是資料源異常而非新變體。既然 usdc（主金額）已存在、方向也已由
    user/destination 判定出來，選擇「當作沒收手續費」而非「打回 anomaly」：
    這一分支只影響收方（inbound）的入帳金額，且 fee 實測上限僅 ~$1，寧可讓
    這筆真實入金被記到（免得動用它的地方，如 follower_flow 的回撤基準校正，
    把一筆真實資金移動誤判成「型別不明」而完全不校正——工程原則 1）。

    ⚠️ 自轉自（`user == destination == address`，T9b W2）**必須先判**：若沿用
    「先看 destination 是否匹配」的順序，自轉自會先撞進 destination 分支回
    `+(usdc − fee)`（reviewer 實測修復前回 +149.0）——把一筆對 perp 淨額零
    影響的自轉自誤記成一筆入金，方向錯在幻影回撤側（工程原則 1 事故 #4 同型：
    基準被錯誤抬高）。自轉自的真實效果只有 `fee` 這筆流出（`−fee`）。

    髒 `usdc`／`fee`（非數字字串）→ `None`（不拋；由 `flow_anomaly` 回
    `"internal-transfer-bad-amount"`，與「方向不明」分開分類）。
    """
    usdc = delta.get("usdc")
    if usdc is None:
        return None
    direction = _internal_transfer_direction(delta, address)
    if direction is None:
        return None
    try:
        usdc_amount = Decimal(str(usdc))
        fee = delta.get("fee")
        fee_amount = Decimal(str(fee)) if fee is not None else Decimal("0")
    except (InvalidOperation, ValueError):
        return None
    if direction == "self":
        return -fee_amount
    if direction == "destination":
        return usdc_amount - fee_amount
    return -usdc_amount


def signed_flow(delta: dict, *, address: str | None = None) -> Decimal | None:
    """白名單內型別 + 欄位齊全 → 有號 USD 流量（入 perp +、出 perp −）；否則 None。

    ⚠️ vaultWithdraw 用 `netWithdrawnUsd` 而**不是** `requestedUsd`：requested 含
    commission，而 commission 是 vault 內部再分配（付給 leader），**不離開帳戶**——
    誤用 requested 會把留在帳內的錢當成流出，恆等式殘差立刻爆容差。

    accountClassTransfer 缺 `usdc` 或缺 `toPerp` **鍵** → None（方向不明不得
    猜號；由 flow_anomaly 記 missing-amount／missing-direction）。

    `address` 只有 internalTransfer 分支會用到（方向判定需要知道呼叫端在問
    哪個帳戶，見 INTERNAL_TRANSFER 註解）；不傳（None）或兩邊都不是 address
    → None（不得猜號；由 flow_anomaly 記 "internal-transfer-direction-unknown"）。
    """
    delta_type = delta.get("type")

    if delta_type == INTERNAL_TRANSFER:
        return _internal_transfer_flow(delta, address)

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


def flow_anomaly(delta: dict, *, address: str | None = None) -> str | None:
    """流量異常分類。

    回傳值：
    - None：正常（白名單內且欄位齊全）
    - str（型別字串）：白名單外
    - str（"{type}:missing-amount"）：白名單內但缺金額欄位
    - str（"accountClassTransfer:missing-direction"）：缺 toPerp 鍵（方向不明）
    - str（"internalTransfer:missing-amount"）：缺 usdc 鍵
    - str（"internal-transfer-direction-unknown"）：address 未提供，或兩邊
      （user／destination）都不是 address——方向不明，不得猜號。
    - str（"internal-transfer-bad-amount"）：方向已判定，但 usdc／fee 是非
      數字字串——一筆髒紀錄不得讓呼叫端拋例外（檔頭「不得炸掉呼叫端」承諾）。
    """
    delta_type = delta.get("type")

    # 特例：internalTransfer（方向由 address 對比 user/destination 決定）
    if delta_type == INTERNAL_TRANSFER:
        if delta.get("usdc") is None:
            return f"{delta_type}:missing-amount"
        if _internal_transfer_direction(delta, address) is None:
            return "internal-transfer-direction-unknown"
        if _internal_transfer_flow(delta, address) is None:
            return "internal-transfer-bad-amount"
        return None

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
