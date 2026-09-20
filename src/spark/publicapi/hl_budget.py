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

父子 scope（Task 7.4a，2026-09-21 使用者裁決：fills 類別級飢餓修法）：`scope_parents`
把子 scope 對映到父 scope（例如 `explore_base`／`explore_fills` → `explore`）。子 scope
的每一筆預留**同時**計入自己、父與全域三個帳——這是同一本帳的三種切面，不是三份
獨立配額（工程原則 1：比較雙方須同源同基準）。用途：有 fills 待處理時，基礎抓取
（state／portfolio／ledger）走 `explore_base`（cap 180），fills 走 `explore_fills`
（cap 120，保證每分鐘至少擠進一頁 120 權重的 `userFillsByTime`）——嚴格優先級曾讓
fills 在一小時內拿不到一次額度（正式機觀測，2026-09-21）；子 cap 是父 cap（300）內的
**保留切片**，不是額外配額，兩個子 cap 之和（300）等於父 cap，父 cap 又受全域 900
限制。無 fills 待處理時基礎類別改直接走父 scope `explore`（可用滿 300）。父 scope 暫停
（429）時所有子 scope 一併暫停；子 scope 的 429 也會暫停父（`note_429` 解析出「要暫停
的 scope」＝該 scope 的父，沒有父才退回 `DEFERRABLE_SCOPE`），確保暫停語意不因走子
scope 而失效。

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

import math
import random
import re
import threading
import time
from collections import Counter, defaultdict, deque

# P6 Task 6.3（D15，2026-09-20）：等待額度秒數／HTTP 結果計數的樣本上限——
# 兩者都是「近期健康狀態」的觀測窗，不是全歷史帳本，maxlen 界定記憶體與視窗大小。
WAIT_SAMPLES_MAXLEN = 500

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


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank 百分位數，不引入 numpy（P6 Task 6.3，D15）。

    `rank = ceil(pct/100 * n)`（1-based，夾在 `[1, n]`）——例如 n=2 時
    `pct=50 → rank=1`（排序後較小值）、`pct=95 → rank=2`（排序後較大值，
    此例等於 max）。空清單回 `None`（沒有樣本，不是 0——工程原則 1：未知≠0）。
    """
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    rank = max(1, min(n, math.ceil(pct / 100.0 * n)))
    return ordered[rank - 1]


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
                 scope_parents: dict[str, str] | None = None,
                 now_fn=time.monotonic, sleep_fn=time.sleep, rng=random.random):
        self._global_cap = global_cap
        self._scope_caps = dict(DEFAULT_SCOPE_CAPS if scope_caps is None else scope_caps)
        # Task 7.4a：子 scope → 父 scope 對映（例如 explore_base/explore_fills → explore）。
        # 空 dict＝沒有父子關係，行為與改動前完全一致。
        self._scope_parents: dict[str, str] = dict(scope_parents or {})
        self._now, self._sleep, self._rng = now_fn, sleep_fn, rng
        self._lock = threading.Lock()
        # 項目改為可變 `[ts, weight, scope]`（原為 tuple）：`settle()` 需要在
        # 回應到手後就地下修某一筆已預留的權重（reviewer C1），tuple 不可變
        # 做不到這件事，改用 list 並把該 list 本身當作 token 回傳給呼叫端。
        self._log: deque[list] = deque()
        self._paused_until: dict[str, float] = {}
        self._consecutive_429: Counter[str] = Counter()
        self._counters: Counter[str] = Counter()
        # P6 Task 6.3（D15）：per-scope 等待秒數樣本（`reserve` 從第一次
        # `try_reserve` 失敗到成功／逾時的耗時；立即成功記 0）與 HTTP 結果計數
        # （成功／各類例外，含每次重試各記一次）。只在 defaultdict 被存取的
        # scope 才會出現在 snapshot——未觸發的 scope 不虛報任何鍵。
        self._wait_samples: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=WAIT_SAMPLES_MAXLEN))
        self._http_counts: dict[str, Counter[str]] = defaultdict(Counter)

    # ---- 內部（呼叫端須持鎖） ----
    def _prune(self, now: float) -> None:
        cutoff = now - WINDOW_S
        while self._log and self._log[0][0] <= cutoff:
            self._log.popleft()

    def _used(self, scope: str | None = None) -> int:
        """`scope is None` → 全部帳目（全域用量）。否則含**自己＋子 scope**的帳目
        （Task 7.4a）：一筆記在 `explore_base` 的預留，查 `_used("explore")` 時
        要算進去——父 cap 與全域 cap 檢查都靠這個「自己或父」判準同源同基準。"""
        if scope is None:
            return sum(w for _, w, _ in self._log)
        return sum(w for _, w, s in self._log
                   if s == scope or self._scope_parents.get(s) == scope)

    # ---- 公開 ----
    def try_reserve(self, weight: int, scope: str) -> list | None:
        """成功回傳該筆帳目的 token（`[ts, weight, scope]`，可傳給 `settle()`
        事後下修）；額度不足回 `None`（原版回 `bool`，list 的真值語意相同，
        既有 `if lim.try_reserve(...)`／`assert not lim.try_reserve(...)` 寫法
        不受影響）。"""
        with self._lock:
            now = self._now()
            self._prune(now)
            parent = self._scope_parents.get(scope)
            # (1) 暫停：自身或父暫停中都拒（Task 7.4a：子 scope 不得繞過父的 429 暫停）。
            until = self._paused_until.get(scope, 0.0)
            if until > now:
                raise ScopePaused(scope, until)
            if parent is not None:
                parent_until = self._paused_until.get(parent, 0.0)
                if parent_until > now:
                    raise ScopePaused(scope, parent_until)
            # (2) 全域。
            if self._used() + weight > self._global_cap:
                self._counters["denied_global"] += 1
                return None
            # (3) 父 cap（若有父且父有設 cap）：子 cap 是父 cap 內的保留切片，
            # 父帳滿了子照樣被拒——即使子自己的 cap 還有餘量。
            if parent is not None and parent in self._scope_caps:
                if self._used(parent) + weight > self._scope_caps[parent]:
                    self._counters["denied_scope"] += 1
                    return None
            # (4) 自身 cap。
            cap = self._scope_caps.get(scope)
            if cap is not None and self._used(scope) + weight > cap:
                self._counters["denied_scope"] += 1
                return None
            token = [now, weight, scope]
            self._log.append(token)
            self._counters["reservations"] += 1
            self._counters["reserved_weight"] += weight
            return token

    def available(self, scope: str) -> int:
        """目前這個 scope 還能預留多少權重（Task 7.4a，供 scheduler 判斷「fills
        有沒有一頁 120 的額度」用，不消耗額度）。三個上限（自身／父／全域）取
        同源同基準的最小值：`min(global_cap - _used(None), cap_scope - _used(scope)
        [若有自身 cap], cap_parent - _used(parent) [若有父且父有 cap])`，夾在
        `[0, +inf)`（永不回負數）。暫停中（自身或父）視為 0——暫停期間 `try_reserve`
        會直接拋 `ScopePaused`，`available` 若回報非 0 會誤導呼叫端以為可以領工。"""
        with self._lock:
            now = self._now()
            self._prune(now)
            if self._paused_until.get(scope, 0.0) > now:
                return 0
            parent = self._scope_parents.get(scope)
            if parent is not None and self._paused_until.get(parent, 0.0) > now:
                return 0
            avail = self._global_cap - self._used()
            cap = self._scope_caps.get(scope)
            if cap is not None:
                avail = min(avail, cap - self._used(scope))
            if parent is not None and parent in self._scope_caps:
                avail = min(avail, self._scope_caps[parent] - self._used(parent))
            return max(0, avail)

    def reserve(self, weight: int, scope: str, *, wait_s: float = 0.0) -> list:
        """取得額度（回傳 token，見 `try_reserve`）或拋 BudgetExhausted；最多等
        wait_s 秒（0.25 秒輪詢）。取得後呼叫端必須**立即**發送（spec §5.1：
        不先扣額再排長隊）。

        P6 Task 6.3（D15）：無論成功或逾時都記一筆等待秒數樣本（`_record_wait`）
        ——立即成功記 0，逾時記實際等到的秒數；`snapshot()["wait_ms"]` 靠這份
        樣本算 p50/p95/max，讓「這個 scope 平常要等多久」看得見。"""
        start = self._now()
        deadline = start + wait_s
        while True:
            token = self.try_reserve(weight, scope)
            if token is not None:
                self._record_wait(scope, self._now() - start)
                return token
            remaining = deadline - self._now()
            if remaining <= 0:
                self._counters["exhausted"] += 1
                self._record_wait(scope, self._now() - start)
                raise BudgetExhausted(f"hl budget exhausted for scope={scope} weight={weight}")
            self._sleep(min(0.25, remaining))

    def _record_wait(self, scope: str, wait_s: float) -> None:
        with self._lock:
            self._wait_samples[scope].append(max(0.0, wait_s))

    def note_http(self, scope: str, cls: str) -> None:
        """記一次 HTTP 嘗試的結果分類（`hl.py` 的 `attempt()` 每次嘗試結束——
        成功或例外——都呼叫一次；重試每次各計，P6 Task 6.3 D15）。分類詞彙見
        `hl.py._classify_attempt_exc`：`2xx|4xx|429|5xx|timeout|conn_error|
        budget_exhausted|scope_paused|other`。"""
        with self._lock:
            self._http_counts[scope][cls] += 1

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
        """任一 scope 的 429 → 暫停「該 scope 要暫停的目標」：有父就暫停父
        （Task 7.4a：`explore_base`／`explore_fills` 的 429 都要暫停 `explore`，
        子 scope 不能自己扛一個獨立的暫停時鐘，否則另一個子 scope 會誤以為安全
        而繼續打，繞過父的退避），沒有父就退回舊行為 `DEFERRABLE_SCOPE`
        （`interactive`／`explore` 本身皆無父對映，維持改動前語意不變）。

        2026-09-20 reviewer 修正：
        - 暫停已在生效中（`now < paused_until[target]`）視為同一次違規的
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
            target = self._scope_parents.get(scope, DEFERRABLE_SCOPE)
            self._counters["rate_limited"] += 1
            jitter = self._rng() * PAUSE_JITTER_MAX_S
            current_until = self._paused_until.get(target, 0.0)
            retry_after = float(retry_after_s or 0.0)
            if now < current_until:
                base = max(PAUSE_MIN_S, retry_after)
                self._paused_until[target] = max(current_until, now + base + jitter)
                return
            self._consecutive_429[target] += 1
            n = self._consecutive_429[target]
            pause = max(retry_after, min(PAUSE_MIN_S * (2 ** (n - 1)), PAUSE_MAX_S)) + jitter
            self._paused_until[target] = max(current_until, now + pause)

    def note_ok(self, scope: str) -> None:
        """2026-09-20 opus 複審 W2（撤回 1.5-A4）：只有 `explore` 自己成功才歸零
        `explore` 的連續 429 計數。`interactive` 的低權重（通常 2/20）成功不證明
        `explore` 的 120 權重請求也能過——若任何 scope 成功都歸零，升級階梯永遠
        到不了第二級（`interactive` 幾乎每次都成功），撤銷／重試節奏形同虛設。
        升級要靠 `explore` 自己的 429 累積、也要靠 `explore` 自己的成功歸零。

        Task 7.4a：子 scope（`explore_base`／`explore_fills`）成功一樣歸零父
        `explore` 的計數——子的成功就是父的成功，同一本帳沒有理由分兩套。"""
        resolved = self._scope_parents.get(scope, scope)
        if resolved != DEFERRABLE_SCOPE:
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
            # Task 7.4a：父 scope 的 `used` 鍵是**合計**（自己＋所有子），用 `_used`
            # 覆寫而非疊加——第一輪迴圈已把父的直接帳目算進去，`_used(parent)`
            # 本身就是「直接＋子」的同一份總和，覆寫等於補上子的部分、不會重複計。
            for parent in set(self._scope_parents.values()):
                used[parent] = self._used(parent)
            return {
                "window_s": int(WINDOW_S), "global_cap": self._global_cap,
                "scope_caps": dict(self._scope_caps),
                "scope_parents": dict(self._scope_parents),
                "used": dict(used),
                "paused_until": dict(self._paused_until),
                "paused_remaining_s": {scope: max(0.0, until - now)
                                      for scope, until in self._paused_until.items()},
                "consecutive_429": dict(self._consecutive_429),
                "counters": {k: self._counters.get(k, 0) for k in self._COUNTER_KEYS},
                # P6 Task 6.3（D15）：只有實際發生過等待/HTTP 嘗試的 scope 才出現
                # 鍵——未觸發的 scope 不虛報一筆全 0 的統計（工程原則 1：未知≠0）。
                "wait_ms": {
                    scope: {
                        "n": len(samples),
                        "p50": round(percentile(list(samples), 50) * 1000),
                        "p95": round(percentile(list(samples), 95) * 1000),
                        "max": round(max(samples) * 1000),
                    }
                    for scope, samples in self._wait_samples.items() if samples
                },
                "http": {scope: dict(counts) for scope, counts in self._http_counts.items()},
            }
