"""src/spark/copytrade/vault_policy.py
vault leader 的保護設定——**雙層執行的單一常數來源**（2026-07-31 Wave 2）。

vault 的 accountValue（scale 分母）會被 depositor 申贖被動改變：贖回潮讓分母
瞬縮，follower 的 scale／有效槓桿被動放大。owner 裁決（2026-07-31）：跟 vault
leader 一律套兩道保護——槓桿上限 20（平時不干涉、只防極端）＋申贖流量中性化。

兩層執行、一份常數（工程原則 1：不並存兩個 20）：
1. auto-activate watcher 在 follower env 注入兩鍵（operator 可稽核的顯式紀錄，
   見 scripts/filet_auto_activate._compose_env——值 import 自本檔）。
2. 引擎每輪按**本輪解析出的 kind** 呼叫 apply_vault_policy 自衛（接線見
   scripts/run_copytrade.py）。這層不可繞過：運行中的 follower 可經簽章換
   leader 而 watcher 不重寫 env，env 那層對這條路徑是盲的。

⚠️ 分層：本模組屬 copytrade（引擎核心），**不得 import spark.filet.***——
filet（受管部署層）在 copytrade 之上；kind 字串由呼叫端傳入，不在此解析。
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from spark.copytrade.config import CopySettings

KIND_STANDARD = "standard"
KIND_VAULT = "vault"

# owner 裁決 2026-07-31：平時不干涉正常跟單、只防極端（贖回潮使 vault TVL 分母
# 瞬縮時的被動槓桿放大）。20 對 Hyperliquid 主流 perp 已是交易所上限量級。
VAULT_MAX_TARGET_LEVERAGE = Decimal("20")


def apply_vault_policy(settings: CopySettings, kind: str) -> CopySettings:
    """按 leader kind 套 vault 保護；standard 原物件原樣回傳（is 同一）。

    vault：
    - max_target_leverage：0（停用）→ 補上 20；已設值 → min(現值, 20)——
      客戶選了更保守的上限就尊重它，絕不放寬。
    - leader_flow_neutralization_enabled：強制 True。

    idempotent：已符合保護值的 settings 再套一次回傳同一物件——引擎每輪都套
    （每輪自衛），這個捷徑讓 vault follower 不必每輪重建一份 settings。
    """
    if kind != KIND_VAULT:
        return settings
    cap = (VAULT_MAX_TARGET_LEVERAGE if settings.max_target_leverage == 0
           else min(settings.max_target_leverage, VAULT_MAX_TARGET_LEVERAGE))
    if (settings.max_target_leverage == cap
            and settings.leader_flow_neutralization_enabled):
        return settings
    return replace(settings, max_target_leverage=cap,
                   leader_flow_neutralization_enabled=True)
