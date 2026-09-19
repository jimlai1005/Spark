"""同出口 IP 的 Hyperliquid REST 權重限流器（spec §5，2026-09-20）。

事故背景（2026-09-19）：Explore 300 池重建以「每請求睡 0.7 秒」節流，但 HL 是
每 IP 每分鐘 1200 **權重**——fills 一頁 120、portfolio 20，等於拿 429 當節流器，
同 IP 的 dashboard／onboard 一起被打掉。本模組是**唯一**的權重帳本：
每次 HTTP 嘗試（含重試、分頁）發送前都要先 `reserve`。

演算法：滑動視窗帳本（deque 存 (ts, weight, scope)），每次預留時剪掉 60 秒前的
項目、檢查全域與 scope 上限。預留在發送前登記、不因完成而提前釋放（保守：
inflight 持續占額，完成後仍留在視窗內，spec §5.1）。任意 60 秒切片的總和 ≤ cap
——證明：任一切片 (t-60, t] 內的項目都包含於「最後一筆預留 t_last 時檢查過的
視窗 (t_last-60, t_last]」，該次檢查已保證 ≤ cap。

scope：`interactive`（dashboard／onboard／traders 詳情等使用者請求）與 `explore`
（背景更新）。`explore` 有子預算；任何 scope 觀察到 429 都會暫停 `explore`
（spec §5.2「其他服務觀察到 429 也應通知 Explore 暫停」），`interactive` 自己不暫停
（必要查詢不停，由呼叫端的既有失敗路徑處理）。

覆蓋範圍（誠實標註）：只有 filet-api 這個進程。follower 引擎與三個每日 timer 各自是
獨立進程、不經本模組；全域上限取 900 而非 1200 就是給它們留的餘量。
"""
from __future__ import annotations

import random
import threading
import time
from collections import Counter, deque

WINDOW_S = 60.0
DEFAULT_GLOBAL_CAP = 900
DEFAULT_SCOPE_CAPS: dict[str, int] = {"explore": 300}
PAUSE_MIN_S = 60.0
PAUSE_MAX_S = 900.0
PAUSE_JITTER_MAX_S = 5.0
DEFERRABLE_SCOPE = "explore"
INTERACTIVE_SCOPE = "interactive"
INTERACTIVE_WAIT_S = 2.0   # D9：使用者請求最多等 2 秒額度，等不到就走既有失敗路徑

# HL 官方權重（rate-limits 文件，2026-09-19 查閱）：clearinghouseState 等 2；
# 其餘 documented info 20；userRole 60；fills 類另按每 20 筆 +1——一頁 2000 筆＝
# 20 + 100，第一版每頁固定預留 120、不依實際筆數退款（spec §5.1）。
ENDPOINT_WEIGHTS: dict[str, int] = {
    "clearinghouseState": 2, "spotClearinghouseState": 2, "l2Book": 2,
    "allMids": 2, "orderStatus": 2, "exchangeStatus": 2,
    "userFillsByTime": 120, "userFills": 120,
    "userRole": 60,
}
DEFAULT_WEIGHT = 20


def weight_for(info_type: str) -> int:
    return ENDPOINT_WEIGHTS.get(info_type, DEFAULT_WEIGHT)


class BudgetExhausted(RuntimeError):
    """等候額度逾時。刻意**不是** ConnectionError/TimeoutError 子類：resilience 邊界
    會把它當語意錯誤直接上拋，不再重試（重試只會再等一次）。"""


class ScopePaused(RuntimeError):
    """該 scope 因 429 暫停中；`until` 為 now_fn 時基的解除時刻。"""

    def __init__(self, scope: str, until: float):
        super().__init__(f"scope {scope} paused until {until:.0f}")
        self.scope, self.until = scope, until


class WeightLimiter:
    # snapshot()["counters"] 固定回報這些鍵：`dict(Counter)` 對從未被 `+=` 過的鍵
    # 不會回傳 0，而是索引時直接 KeyError（Counter 的 __missing__→0 行為只在
    # 對 Counter 物件本身取值時生效，轉成一般 dict 後就消失了）；用固定鍵集合
    # 逐一 `.get(k, 0)` 才能保證「未觸發＝0」而非缺鍵。
    _COUNTER_KEYS = ("reservations", "reserved_weight", "denied_global",
                     "denied_scope", "rate_limited", "exhausted")

    def __init__(self, *, global_cap: int = DEFAULT_GLOBAL_CAP,
                 scope_caps: dict[str, int] | None = None,
                 now_fn=time.monotonic, sleep_fn=time.sleep, rng=random.random):
        self._global_cap = global_cap
        self._scope_caps = dict(DEFAULT_SCOPE_CAPS if scope_caps is None else scope_caps)
        self._now, self._sleep, self._rng = now_fn, sleep_fn, rng
        self._lock = threading.Lock()
        self._log: deque[tuple[float, int, str]] = deque()
        self._paused_until: dict[str, float] = {}
        self._consecutive_429: Counter[str] = Counter()
        self._counters: Counter[str] = Counter()

    # ---- 內部（呼叫端須持鎖） ----
    def _prune(self, now: float) -> None:
        cutoff = now - WINDOW_S
        while self._log and self._log[0][0] <= cutoff:
            self._log.popleft()

    def _used(self, scope: str | None = None) -> int:
        return sum(w for _, w, s in self._log if scope is None or s == scope)

    # ---- 公開 ----
    def try_reserve(self, weight: int, scope: str) -> bool:
        with self._lock:
            now = self._now()
            self._prune(now)
            until = self._paused_until.get(scope, 0.0)
            if until > now:
                raise ScopePaused(scope, until)
            if self._used() + weight > self._global_cap:
                self._counters["denied_global"] += 1
                return False
            cap = self._scope_caps.get(scope)
            if cap is not None and self._used(scope) + weight > cap:
                self._counters["denied_scope"] += 1
                return False
            self._log.append((now, weight, scope))
            self._counters["reservations"] += 1
            self._counters["reserved_weight"] += weight
            return True

    def reserve(self, weight: int, scope: str, *, wait_s: float = 0.0) -> None:
        """取得額度或拋 BudgetExhausted；最多等 wait_s 秒（0.25 秒輪詢）。
        取得後呼叫端必須**立即**發送（spec §5.1：不先扣額再排長隊）。"""
        deadline = self._now() + wait_s
        while True:
            if self.try_reserve(weight, scope):
                return
            remaining = deadline - self._now()
            if remaining <= 0:
                self._counters["exhausted"] += 1
                raise BudgetExhausted(f"hl budget exhausted for scope={scope} weight={weight}")
            self._sleep(min(0.25, remaining))

    def note_429(self, scope: str, retry_after_s: float | None = None) -> None:
        """任一 scope 的 429 → 暫停 explore；連續 429 指數延長至 PAUSE_MAX_S，
        加 0–5 秒正向 jitter；Retry-After 存在時取較長者。"""
        with self._lock:
            now = self._now()
            self._counters["rate_limited"] += 1
            self._consecutive_429[DEFERRABLE_SCOPE] += 1
            n = self._consecutive_429[DEFERRABLE_SCOPE]
            base = max(PAUSE_MIN_S, float(retry_after_s or 0.0))
            pause = min(base * (2 ** (n - 1)), PAUSE_MAX_S) + self._rng() * PAUSE_JITTER_MAX_S
            self._paused_until[DEFERRABLE_SCOPE] = max(
                self._paused_until.get(DEFERRABLE_SCOPE, 0.0), now + pause)

    def note_ok(self, scope: str) -> None:
        with self._lock:
            if scope == DEFERRABLE_SCOPE:
                self._consecutive_429[DEFERRABLE_SCOPE] = 0

    def snapshot(self) -> dict:
        with self._lock:
            now = self._now()
            self._prune(now)
            used: Counter[str] = Counter()
            for _, w, s in self._log:
                used[s] += w
            return {
                "window_s": int(WINDOW_S), "global_cap": self._global_cap,
                "scope_caps": dict(self._scope_caps), "used": dict(used),
                "paused_until": dict(self._paused_until),
                "consecutive_429": dict(self._consecutive_429),
                "counters": {k: self._counters.get(k, 0) for k in self._COUNTER_KEYS},
            }
