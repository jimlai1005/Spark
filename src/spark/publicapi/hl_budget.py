"""同出口 IP 的 Hyperliquid REST 權重限流器（spec §5，2026-09-20）。

事故背景（2026-09-19）：Explore 300 池重建以「每請求睡 0.7 秒」節流，但 HL 是
每 IP 每分鐘 1200 **權重**——fills 一頁 120、portfolio 20，等於拿 429 當節流器，
同 IP 的 dashboard／onboard 一起被打掉。本模組是所有 `/info` 呼叫的權重帳本
（`user_details` 走 explorer host，不計）：每次 HTTP 嘗試（含重試、分頁）發送前
都要先 `reserve`。

演算法：滑動視窗帳本（deque 存 [ts, weight, scope] 可變項目，供 `settle` 事後
下修），每次預留時剪掉 60 秒前的項目、檢查全域與 scope 上限。預留在發送前登記、
不因完成而提前釋放（保守：inflight 持續占額，完成後仍留在視窗內，spec §5.1）。
任意 60 秒切片的總和 ≤ cap——證明：任一切片 (t-60, t] 內的項目都包含於「最後一筆
預留 t_last 時檢查過的視窗 (t_last-60, t_last]」，該次檢查已保證 ≤ cap。

scope：`interactive`（dashboard／onboard／traders 詳情等使用者請求）與 `explore`
（背景更新）。`explore` 有子預算；任何 scope 觀察到 429 都會暫停 `explore`
（spec §5.2「其他服務觀察到 429 也應通知 Explore 暫停」），`interactive` 自己不暫停
（必要查詢不停，由呼叫端的既有失敗路徑處理）。

覆蓋範圍（誠實標註）：只有 filet-api 這個進程。follower 引擎與三個每日 timer 各自是
獨立進程、不經本模組；全域上限取 900 而非 1200 就是給它們留的餘量。

<!-- 2026-09-20 opus 審查（P1 Task 1.5）修正：
C1 fills 固定預留 120（一頁上限）比 HL 實際計費（20 + ceil(筆數/20)）高
3-6 倍，會造成「HL 沒擋、我們自己先擋」的假陽性——新增 `settle()`，回應到手後
依實際筆數下修（只降不升）。
W1 `ScopePaused` 訊息原本含十進位的 `until`（例如 250238 內含子字串 "502"），
會被 `spark.resilience._TRANSIENT_MARKERS`（含 "502"/"503"/"504"）誤判成
transient 而被重試 3 次——訊息改為不含任何數字。
W2 舊版用「訊息含 429 子字串就判定」的鬆散判準，會把 `JSONDecodeError ... column 429` 這種訊息誤判成
429——`is_rate_limited` 改用錨定在字串開頭或含完整片語的正則。
`note_429` 也修正：暫停已在生效中不再重複升級（`n` 只在「已解除又再犯」時累加），
且 Retry-After 不再被乘上指數（它是「至少等這麼久」的下限，不是退避基期）。 -->
"""
from __future__ import annotations

import random
import re
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
# 20 + 100，預留時先用上限 120（`weight_for`），回應到手後由呼叫端呼叫
# `settle()` 依實際筆數下修（reviewer C1；spec §5.1「第一版不退款」是簡化
# 而非禁令，同源同基準優先）。
ENDPOINT_WEIGHTS: dict[str, int] = {
    "clearinghouseState": 2, "spotClearinghouseState": 2, "l2Book": 2,
    "allMids": 2, "orderStatus": 2, "exchangeStatus": 2,
    "userFillsByTime": 120, "userFills": 120,
    "userRole": 60,
}
DEFAULT_WEIGHT = 20

# reviewer W2：429 偵測不能只看訊息是否含 429 子字串——`JSONDecodeError`
# 之類的訊息可能含 "column 429" 這種與速率限制無關的數字子字串。錨定在字串
# 開頭（HTTP 狀態行常見的 "429 ..." 開頭）或完整片語 "429 Too Many Requests"。
_RATE_LIMIT_RE = re.compile(r"^429\b|429 Too Many Requests")


def weight_for(info_type: str) -> int:
    return ENDPOINT_WEIGHTS.get(info_type, DEFAULT_WEIGHT)


def is_rate_limited(exc: Exception) -> bool:
    """429 偵測的唯一判準（`hl.py`／`hl_explore.py` 都委派這裡，reviewer W2）：
    優先看 `httpx.HTTPStatusError.response.status_code`；沒有 response 物件的
    情況（例如測試用字串例外）才退回錨定正則，避免把 `JSONDecodeError:
    ... column 429` 這類訊息誤判成速率限制。"""
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 429:
        return True
    return bool(_RATE_LIMIT_RE.search(str(exc)))


class BudgetExhausted(RuntimeError):
    """等候額度逾時。刻意**不是** ConnectionError/TimeoutError 子類：resilience 邊界
    會把它當語意錯誤直接上拋，不再重試（重試只會再等一次）。"""


class ScopePaused(RuntimeError):
    """該 scope 因 429 暫停中；`until` 為 now_fn 時基的解除時刻，只存屬性、
    **不放進訊息字串**（reviewer W1）——`until` 的十進位表示法可能偶然含
    "502"/"503"/"504" 這類子字串，被 `spark.resilience._TRANSIENT_MARKERS`
    誤判成 transient 網路錯誤而重試 3 次，讓「暫停中」的訊號被吃掉。"""

    def __init__(self, scope: str, until: float):
        super().__init__(f"scope {scope} paused: upstream rate limited")
        self.scope, self.until = scope, until


class WeightLimiter:
    # snapshot()["counters"] 固定回報這些鍵：`dict(Counter)` 對從未被 `+=` 過的鍵
    # 不會回傳 0，而是索引時直接 KeyError（Counter 的 __missing__→0 行為只在
    # 對 Counter 物件本身取值時生效，轉成一般 dict 後就消失了）；用固定鍵集合
    # 逐一 `.get(k, 0)` 才能保證「未觸發＝0」而非缺鍵。
    _COUNTER_KEYS = ("reservations", "reserved_weight", "denied_global",
                     "denied_scope", "rate_limited", "exhausted", "refunded_weight")

    def __init__(self, *, global_cap: int = DEFAULT_GLOBAL_CAP,
                 scope_caps: dict[str, int] | None = None,
                 now_fn=time.monotonic, sleep_fn=time.sleep, rng=random.random):
        self._global_cap = global_cap
        self._scope_caps = dict(DEFAULT_SCOPE_CAPS if scope_caps is None else scope_caps)
        self._now, self._sleep, self._rng = now_fn, sleep_fn, rng
        self._lock = threading.Lock()
        # 項目改為可變 `[ts, weight, scope]`（原為 tuple）：`settle()` 需要在
        # 回應到手後就地下修某一筆已預留的權重（reviewer C1），tuple 不可變
        # 做不到這件事，改用 list 並把該 list 本身當作 token 回傳給呼叫端。
        self._log: deque[list] = deque()
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
    def try_reserve(self, weight: int, scope: str) -> list | None:
        """成功回傳該筆帳目的 token（`[ts, weight, scope]`，可傳給 `settle()`
        事後下修）；額度不足回 `None`（原版回 `bool`，list 的真值語意相同，
        既有 `if lim.try_reserve(...)`／`assert not lim.try_reserve(...)` 寫法
        不受影響）。"""
        with self._lock:
            now = self._now()
            self._prune(now)
            until = self._paused_until.get(scope, 0.0)
            if until > now:
                raise ScopePaused(scope, until)
            if self._used() + weight > self._global_cap:
                self._counters["denied_global"] += 1
                return None
            cap = self._scope_caps.get(scope)
            if cap is not None and self._used(scope) + weight > cap:
                self._counters["denied_scope"] += 1
                return None
            token = [now, weight, scope]
            self._log.append(token)
            self._counters["reservations"] += 1
            self._counters["reserved_weight"] += weight
            return token

    def reserve(self, weight: int, scope: str, *, wait_s: float = 0.0) -> list:
        """取得額度（回傳 token，見 `try_reserve`）或拋 BudgetExhausted；最多等
        wait_s 秒（0.25 秒輪詢）。取得後呼叫端必須**立即**發送（spec §5.1：
        不先扣額再排長隊）。"""
        deadline = self._now() + wait_s
        while True:
            token = self.try_reserve(weight, scope)
            if token is not None:
                return token
            remaining = deadline - self._now()
            if remaining <= 0:
                self._counters["exhausted"] += 1
                raise BudgetExhausted(f"hl budget exhausted for scope={scope} weight={weight}")
            self._sleep(min(0.25, remaining))

    def settle(self, token: list | None, actual_weight: int) -> None:
        """回應到手後依實際計費**下修**預留（只降不升，reviewer C1）。fills
        類預留 120 是上限，實際 = 20 + ceil(筆數/20)；不結算會讓自訂帳本比 HL
        真實計費高 3–6 倍，造成 HL 沒擋、我們自己先擋的假陽性。`token` 可能
        已因超過 60 秒視窗被 `_prune` 移出帳本——此時就地修改一個已經不在
        deque 裡的 list 沒有任何效果，等同 no-op（該筆本就已經不計入 `_used`）。

        2026-09-20 opus 複審 S1：token 已滑出視窗（`token[0] <= now - WINDOW_S`）
        時不只是「改它沒效果」，還必須連 `refunded_weight` 計數器都不計——那筆
        weight 早已不在 `_used()` 裡，計進 `refunded_weight` 會虛報一筆從未真正
        佔額度的退款，讓 `snapshot()["counters"]` 對不上實際發生過的預留。"""
        with self._lock:
            if token is None or actual_weight >= token[1]:
                return
            if token[0] <= self._now() - WINDOW_S:
                return
            self._counters["refunded_weight"] += token[1] - actual_weight
            token[1] = actual_weight

    def note_429(self, scope: str, retry_after_s: float | None = None) -> None:
        """任一 scope 的 429 → 暫停 explore。

        2026-09-20 reviewer 修正：
        - 暫停已在生效中（`now < paused_until[explore]`）視為同一次違規的
          延續，不升級 `_consecutive_429`，只把 `paused_until` 延到
          `max(既有, now + max(PAUSE_MIN_S, retry_after_s or 0) + jitter)`——
          重複收到同一輪的 429 不該讓退避指數暴衝。
        - 只有「暫停已解除、又再犯」才真正升級：`n` 遞增，
          `pause = max(retry_after_s or 0, min(PAUSE_MIN_S * 2**(n-1),
          PAUSE_MAX_S)) + jitter`——Retry-After 是「至少等這麼久」的下限，
          只有指數部分被 `PAUSE_MAX_S` 夾住，Retry-After 本身不夾（2026-09-20
          opus 複審 W1：舊版把 `max(retry_after, 指數)` 整體夾在 `PAUSE_MAX_S`，
          等於用我們自己的退避上限去縮短伺服器明講的「至少等這麼久」，方向反了）。
        """
        with self._lock:
            now = self._now()
            self._counters["rate_limited"] += 1
            jitter = self._rng() * PAUSE_JITTER_MAX_S
            current_until = self._paused_until.get(DEFERRABLE_SCOPE, 0.0)
            retry_after = float(retry_after_s or 0.0)
            if now < current_until:
                base = max(PAUSE_MIN_S, retry_after)
                self._paused_until[DEFERRABLE_SCOPE] = max(current_until, now + base + jitter)
                return
            self._consecutive_429[DEFERRABLE_SCOPE] += 1
            n = self._consecutive_429[DEFERRABLE_SCOPE]
            pause = max(retry_after, min(PAUSE_MIN_S * (2 ** (n - 1)), PAUSE_MAX_S)) + jitter
            self._paused_until[DEFERRABLE_SCOPE] = max(current_until, now + pause)

    def note_ok(self, scope: str) -> None:
        """2026-09-20 opus 複審 W2（撤回 1.5-A4）：只有 `explore` 自己成功才歸零
        `explore` 的連續 429 計數。`interactive` 的低權重（通常 2/20）成功不證明
        `explore` 的 120 權重請求也能過——若任何 scope 成功都歸零，升級階梯永遠
        到不了第二級（`interactive` 幾乎每次都成功），撤銷／重試節奏形同虛設。
        升級要靠 `explore` 自己的 429 累積、也要靠 `explore` 自己的成功歸零。"""
        if scope != DEFERRABLE_SCOPE:
            return
        with self._lock:
            self._consecutive_429[DEFERRABLE_SCOPE] = 0

    def snapshot(self) -> dict:
        """`paused_until` 是 `now_fn` 時基——正式路徑注入 `time.monotonic`，
        與 ops/health 讀取當下用的 wall clock（`checked_at`）不可比較（2026-09-20
        opus 複審 W3）。要看「還要暫停多久」一律讀 `paused_remaining_s`
        （`max(0.0, until - now)`，在同一次快照裡用同一個 `now_fn` 算出，
        不受時基混用影響）。"""
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
                "paused_remaining_s": {scope: max(0.0, until - now)
                                      for scope, until in self._paused_until.items()},
                "consecutive_429": dict(self._consecutive_429),
                "counters": {k: self._counters.get(k, 0) for k in self._COUNTER_KEYS},
            }
