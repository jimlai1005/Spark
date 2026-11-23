"""leader 權益（跟單倍率分母）的單一產生點。

背景（plan 2026-11-22、recon §1／§2）：倍率分母原本是 leader 的 perp `accountValue`。
統一帳戶／portfolio margin 的 leader，錢在現貨帳本，perp `accountValue` 空倉時為 0、
開倉後只是保證金——分母偏小數倍到數十倍，follower 倉位被放大、實際虧錢。
現在分母改取交易所自己顯示的帳戶總值（`portfolio` 最後一點，`adapter.get_account_total`）。

三條不變量：
- **唯一產生點**：倍率分母、`max_target_leverage` 的有效槓桿、流量中性化的 raw、波動
  權重的參數，全部從這裡的結果取值（工程原則 1、5）。
- **讀不到、過期、非正值而 leader 有曝險 → `LeaderEquityUnavailable`**：呼叫端本輪
  零交易動作＋告警（讀不到錢 ≠ 錢虧光，事故 #4 同型）。
- **絕不退回 perp `accountValue`、絕不以 0 繼續算**：那正是事故來源。

純函式層 `resolve_leader_equity` 無 IO；`read_leader_equity` 是薄讀取層。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from spark.exchange.base import AccountTotal

# `portfolio` 最後一點是否即時是實測、非文件保證（plan R4）——以時間戳新鮮度防守。
MAX_AGE_MS = 180_000


class LeaderEquityUnavailable(Exception):
    """leader 權益無法判定——呼叫端必須讓本輪零交易動作。reason 是給人看的一句話
    （不含 secrets；位址以外不放交易所原始回應）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class LeaderEquity:
    value: Decimal          # 倍率分母
    as_of_ms: int
    source: str             # 恆為 "exchange_total"（保留欄位供日後擴充）


def resolve_leader_equity(total: AccountTotal, *, now_ms: int,
                          leader_has_exposure: bool) -> LeaderEquity:
    """純函式。

    - `now_ms - total.as_of_ms > MAX_AGE_MS` → raise（過期；剛好 180 秒不算）。
      as_of 在未來（時鐘偏差）不算過期。
    - `total.value <= 0` 且 leader 有曝險 → raise（讀不到錢 ≠ 錢虧光）。
    - `total.value <= 0` 且無曝險 → `LeaderEquity(value=0)`（空帳戶，沿用既有 scale=0）。
    """
    age_ms = now_ms - total.as_of_ms
    if age_ms > MAX_AGE_MS:
        raise LeaderEquityUnavailable(
            f"帳戶總值資料已過期 {age_ms // 1000} 秒（上限 {MAX_AGE_MS // 1000} 秒）")
    if total.value <= 0:
        if leader_has_exposure:
            raise LeaderEquityUnavailable(
                f"帳戶總值 {total.value} 非正值，但 leader 仍有倉位或掛單")
        return LeaderEquity(value=Decimal("0"), as_of_ms=total.as_of_ms,
                            source="exchange_total")
    return LeaderEquity(value=total.value, as_of_ms=total.as_of_ms,
                        source="exchange_total")


def read_leader_equity(adapter, leader: str, *, now_ms: int,
                       leader_has_exposure: bool) -> LeaderEquity:
    """`adapter.get_account_total` → `resolve_leader_equity`。

    `ValueError`（回應形狀不對、查無資料點）轉成 `LeaderEquityUnavailable`；
    其餘例外（網路類）原樣往外拋，走 main_loop 的連續錯誤計數（plan D4）。"""
    try:
        total = adapter.get_account_total(leader)
    except ValueError as e:
        raise LeaderEquityUnavailable(f"讀不到帳戶總值（{e}）") from e
    return resolve_leader_equity(total, now_ms=now_ms,
                                 leader_has_exposure=leader_has_exposure)
