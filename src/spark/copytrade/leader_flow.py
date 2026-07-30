"""leader 申贖流量中性化的純數學層（vault leader，Wave 4）。

vault 的 accountValue（sizing 分母）會被 depositor 申贖被動改變：入金瞬間分母
變大 → scale 縮小 → 全部位跟著縮（反之亦然），但 leader 的部位其實沒動——這是
churn，不是訊號。修法：流量發生當下**全額**從分母扣除，之後在 decay 窗內線性
衰減歸零——leader 事後會照新 TVL 調倉，分母必須收斂回真實 accountValue。

本檔只放純函式：無 IO、不 import loop/adapter。guard（adjusted ≤ 0 的幻影歸零
防護、取數失敗降級）屬呼叫端職責（loop.run_cycle）。
"""
from decimal import Decimal

from spark.exchange.base import LedgerFlow


def adjusted_leader_equity(raw: Decimal, flows: list[LedgerFlow],
                           now_ms: int, decay_ms: int) -> Decimal:
    """中性化後的 leader 分母：`raw − Σ flow.usdc × weight`。

    - `weight = max(0, 1 − age/decay_ms)`，`age = now_ms − flow.time_ms`（ms）。
    - `age < 0`（時鐘漂移：交易所時間戳在本機 now 之後）視為 0 → 全額中性化。
      漂移量級是秒、衰減窗是 36h，取 0 的誤差可忽略；反向外推（weight > 1）
      則會放大一筆已知金額的流量，沒有任何情境需要。
    - flow.usdc 有號：入金 +（分母扣除）、出金 −（分母加回）。
    - 全程 Decimal；age/decay 用 Decimal 除法（避免 float 二進位噪音進分母）。
    """
    if decay_ms <= 0:
        # 結構性防護：config 驗 flow_decay_hours > 0，但 int(hours × 3_600_000)
        # 對極小值會截斷成 0 → 下方 Decimal(age)/Decimal(decay_ms) 除零。
        # 截斷後的零衰減窗＝所有流量視同已完全衰減 → 回 raw。
        # 注意：「關閉中性化」走 enabled flag，不走這裡。
        return raw
    adjustment = Decimal("0")
    for f in flows:
        age = max(now_ms - f.time_ms, 0)
        weight = Decimal("1") - Decimal(age) / Decimal(decay_ms)
        if weight <= 0:
            continue  # 窗外（age >= decay）：已完全衰減
        adjustment += f.usdc * weight
    return raw - adjustment
