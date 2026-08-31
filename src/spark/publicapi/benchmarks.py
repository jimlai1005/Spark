"""src/spark/publicapi/benchmarks.py
`GET /api/public/benchmarks`（issue log I-19）：策略／交易員詳情頁 `EquityCurve`
疊加對照用的四個外部標的日線收盤序列——BTC、ETH、S&P 500、黃金。無需登入。

⭐ 代號與 K 線欄位皆為 2026-08-31 主網 curl 實測，非憑印象入碼（工程原則
「欄位名是假設，不是事實」）：
- `POST /info {"type":"meta","dex":"xyz"}` 的 universe 內確有 `xyz:SP500`／
  `xyz:GOLD`（`allMids` 對同一批代號回即時報價，證實非死盤/佔位符）；S&P 500
  與黃金在 Hyperliquid 主網上只以 xyz builder-dex 的合成永續市場形式存在，
  不是預設 dex 的原生幣種。
- `POST /info {"type":"candleSnapshot","req":{"coin":...,"interval":"1d",
  "startTime":...,"endTime":...}}` 回應是 `[{t,T,s,i,o,c,h,l,v,n}, ...]`——
  `t`＝K 棒開盤時間 epoch 毫秒、`c`＝收盤價字串（其餘欄位本模組不使用）。
  xyz 市場的 coin 欄位**已含 `xyz:` 前綴**，請求體不需另帶 `dex` 鍵；用不含
  前綴的裸代號（如 `"SP500"`）查詢一律回 `null`（HL 視為未知幣種）。

⭐ 任一標的查詢失敗（上游例外、形狀不符）只讓該鍵降級為 `null`，不拖累其餘
三個標的、端點恆回 200——與 `public_stats.py` 檔頭同一條公開端點可靠度原則。
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)

# key（回應鍵名）→ HLGateway.candle_snapshot 的 coin 參數。後兩者是 xyz
# builder-dex 合成市場，代號見檔頭實測依據。
BENCHMARK_COINS: dict[str, str] = {
    "btc": "BTC",
    "eth": "ETH",
    "sp500": "xyz:SP500",
    "gold": "xyz:GOLD",
}

DEFAULT_DAYS = 90
MIN_DAYS = 1
MAX_DAYS = 400

# in-process cache TTL（plan 明訂 600s）；依 `days` 分桶，見 `BenchmarksCache`。
CACHE_TTL_S = 600.0

_DAY_MS = 86_400_000


def clamp_days(days: int | None) -> int:
    """`days` 查詢參數夾取至 `[MIN_DAYS, MAX_DAYS]`；缺席／非整數 → `DEFAULT_DAYS`
    （只夾取範圍不 422——沿 `hl_explore.clamp_explore_params` 的既有慣例：這是
    防濫用的展示層參數，不是驗證錯誤）。"""
    if not isinstance(days, int) or isinstance(days, bool):
        return DEFAULT_DAYS
    return max(MIN_DAYS, min(MAX_DAYS, days))


def _fetch_series(hl, coin: str, *, days: int, now_ms: int) -> list[list] | None:
    """單一標的日線收盤序列 `[[epoch_ms, "close"], ...]`（升冪，來自 `t`／`c`
    兩個欄位）。任何例外或非預期回應形狀 → `None`（該標的降級，四個標的各自
    獨立查詢、獨立降級，不共用失敗狀態）。"""
    start_ms = now_ms - days * _DAY_MS
    try:
        raw = hl.candle_snapshot(coin, "1d", start_ms, now_ms)
    except Exception as e:  # noqa: BLE001 — 公開端點：任一標的失敗不得拖累其餘/整頁
        logger.error("benchmark 標的 %s K 線查詢失敗（降級為 null）: %r", coin, e)
        return None
    if not isinstance(raw, list):
        return None
    out: list[list] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        t = row.get("t")
        c = row.get("c")
        if not isinstance(t, int) or c is None:
            continue
        out.append([t, str(c)])
    return out


def build_benchmarks_payload(hl, *, days: int, now_fn: Callable[[], float]) -> dict:
    """`/api/public/benchmarks` 回應 dict：`{"series": {btc/eth/sp500/gold: [...]
    或 null}, "updated_at": ...}`。任一標的失敗 → 該鍵 `null`，本函式恆回可
    序列化結構（呼叫端仍需 200，本函式不拋——與 `public_stats.build_stats_payload`
    同一條公開端點可靠度原則）。"""
    now_ms = int(now_fn() * 1000)
    series = {key: _fetch_series(hl, coin, days=days, now_ms=now_ms)
              for key, coin in BENCHMARK_COINS.items()}
    return {"series": series, "updated_at": int(now_fn())}


class BenchmarksCache:
    """依 `days` 分桶的 in-process TTL 快取（600s）。

    與 `public_stats.TTLCache` 刻意不同：那裡是單一值快取，本端點的回應會
    隨查詢參數 `days` 變動，同一個 `days` 值才共用同一份快取——不同 `days`
    各自獨立一個 TTL 窗口，互不影響彼此的新鮮度。`now_fn` 必須注入（測試靠
    假時鐘釘死 TTL 邊界，沿既有慣例）；`compute()` 不持鎖（K 線查詢可能較慢，
    持鎖跑 IO 會讓並發請求互相卡住排隊，見 `TTLCache.get` 同款理由）。
    """

    def __init__(self, *, now_fn: Callable[[], float], ttl_s: float = CACHE_TTL_S):
        self._now_fn = now_fn
        self._ttl_s = ttl_s
        self._lock = threading.Lock()
        self._entries: dict[int, tuple[float, dict]] = {}

    def get(self, days: int, compute: Callable[[], dict]) -> dict:
        now = self._now_fn()
        with self._lock:
            cached = self._entries.get(days)
            if cached is not None and now - cached[0] < self._ttl_s:
                return cached[1]
        value = compute()
        with self._lock:
            self._entries[days] = (now, value)
        return value
