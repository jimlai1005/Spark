"""src/spark/publicapi/hl_leaderboard.py
`/api/public/leaderboard`（M3 round2 Task 5）唯一的上游出口＋快取＋裁切。

上游 `GET https://stats-data.hyperliquid.xyz/Mainnet/leaderboard` 回應約 36MB JSON
（已驗證外部事實，見 plan 檔頭）——絕不可讓瀏覽器直連。本模組是唯一出口：
抓取（含 transient 重試，沿 `hl.py` 的 `_default_post`／`run()` 慣例）＋進程內
TTL 快取＋依 window 排序裁切成前端要的精簡列。公開端點（`app.py`）只再包一層 HTTP。

快取策略刻意 fail-open 到舊值（工程原則 3 的反向應用——這是展示資料非交易路徑，
上游一次抖動不該讓整頁掛掉，見 `LeaderboardCache`）；但**從未成功抓過**時沒有
舊值可回退，呼叫端須自行轉譯為 503（不得偽裝成健康的空清單）。

排序鍵一律 `Decimal(str(...))`：pnl 是字串（工程原則——金額比較不得用 float，
大數字下 float 會在有效位數邊界丟精度、造成排序錯亂）。
"""
from __future__ import annotations

import logging
import threading
import time
from decimal import Decimal, InvalidOperation
from typing import Callable

import httpx

from spark.resilience import run

logger = logging.getLogger(__name__)

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
_TIMEOUT_S = 30.0  # payload 36MB，遠高於 hl.py /info 查詢的 10s（那些回應小很多）。
CACHE_TTL_S = 600.0  # 10 分鐘
FAILURE_BACKOFF_S = 60.0  # [8b-5] 抓取失敗後的冷卻期，見 `LeaderboardCache` 檔頭
# ⭐ [8b-2] 2026-08-29 二輪複審 Warning：首抓（無舊值可回退）時，等待中的呼叫
# 原本無條件 `wait_event.wait()`——若進行中的那條 fetcher thread 卡住（上游
# 掛住不斷不連、或某個未預期的阻塞），所有等待者會永遠掛住。加一個上限：
# 逾時就放棄等待、回 `None`（呼叫端轉譯 503），比無限期掛住安全；fetcher
# thread 本身不受影響，繼續跑完，跑完後下一輪請求自然能拿到新值。
_FETCH_WAIT_TIMEOUT_S = 35.0

WINDOWS = ("day", "week", "month", "allTime")


def _default_get(url: str) -> dict:
    """httpx 的 ConnectError/ReadTimeout 等不繼承內建 ConnectionError/TimeoutError
    ——鏡像 `hl.py` `_default_post` 的同一條理由：在這個唯一的 IO 邊界把 httpx
    例外轉譯成 `spark.resilience` 分類器認得的內建型別。"""
    try:
        resp = httpx.get(url, timeout=_TIMEOUT_S)
    except httpx.TimeoutException as e:
        raise TimeoutError(str(e) or "leaderboard fetch timed out") from e
    except httpx.TransportError as e:
        raise ConnectionError(f"leaderboard transport error: {e}") from e
    resp.raise_for_status()
    return resp.json()


class LeaderboardCache:
    """單一值 TTL 快取（預設 10 分鐘）。

    過期後嘗試重新抓取；抓取失敗（transient 重試耗盡或非預期例外）→ **保留舊值
    續用**並記 log，不對外拋（fail-open，見檔頭）。從未成功抓過又失敗 → 回
    `None`，呼叫端（`app.py`）須轉譯為 503。

    `get_fn`/`sleep_fn` 可注入（測試給假 GET 與不真睡的 sleep，沿 `HLGateway`
    的 post_fn/sleep_fn 慣例）。`now_fn` 必須注入（測試靠假時鐘釘死 TTL 邊界，
    沿 `public_stats.TTLCache` 的既有慣例）。

    ⭐ [C2] 2026-08-29 opus 審查：single-flight。原始版本沒有防護——TTL 過期後
    若同時有 N 個請求打進來，會**各自**觸發一次 36MB 下載＋解析，是可用性風險
    （一次流量尖峰放大成 N 倍上游流量與 N 倍 CPU）。修法按「有沒有舊值可回退」
    分流：
    - 有舊值：TTL 過期的當下，只有**先到的那一條** thread 真的去下載；其餘同時
      抵達的呼叫**立刻拿到舊值**返回，不排隊等下載完成（見檔頭「下載中不阻塞
      TTL 內讀取」的要求——這裡延伸成「有舊值時也不阻塞」，因為舊值仍然可用）。
    - 沒有舊值（首次抓取）：沒有東西可回退，只能等進行中的那一條 thread 抓完，
      所有並發呼叫共用同一個結果（成功或失敗）。
    `_cached_at` 改記**抓取完成後**的時間戳（原本記的是「決定要抓」那一刻），
    避免 TTL 視窗因為下載耗時被悄悄縮短。

    ⭐ [8b-5] 2026-08-29 二輪複審 Warning：上游故障期間的負面快取。原本 TTL
    過期後若抓取失敗，`_cached_at` 不動，代表**下一個**進來的請求會立刻視為
    「過期」再打一次上游（36MB＋`run()` 的 transient 重試，成本不低）——上游
    真的掛掉時，這會變成「每個請求都各自完整重試一輪」而不是「認賠一次，
    冷卻一陣子」。修法：失敗後記 `_failed_at`，`FAILURE_BACKOFF_S`（預設 60s）
    內的請求不再接手新的 fetch，直接回舊值（或 `None`，若從未成功過）。
    """

    def __init__(self, *, now_fn: Callable[[], float], get_fn=None,
                sleep_fn=time.sleep, ttl_s: float = CACHE_TTL_S,
                failure_backoff_s: float = FAILURE_BACKOFF_S):
        self._now_fn = now_fn
        self._get_fn = get_fn or _default_get
        self._sleep_fn = sleep_fn
        self._ttl_s = ttl_s
        self._failure_backoff_s = failure_backoff_s
        self._lock = threading.Lock()
        self._value: dict | None = None
        self._cached_at: float | None = None
        self._failed_at: float | None = None  # [8b-5]：最近一次抓取失敗的時間戳
        self._fetching = False
        self._fetch_done = threading.Event()
        # window 排序結果的記憶化（[C2] Warning：避免每個請求對 36MB 全量重排）。
        # key 是 `(id(payload), window)`——`id()` 在同一份 payload 物件存活期間
        # （＝同一世代）穩定，且本快取只會有一份 payload 活著（`_value`），換代
        # 時舊 id 不會被誤用；抓取成功時整個 dict 一併清空（見 `get()`）。
        self._sorted_cache: dict[tuple[int, str], list[dict]] = {}

    def _fetch(self) -> dict:
        return run(lambda: self._get_fn(LEADERBOARD_URL),
                   what="HL leaderboard 查詢", idempotent=True, sleep_fn=self._sleep_fn)

    @property
    def cached_at(self) -> float | None:
        """[8b-4] 目前快取值**實際抓取完成**的時間戳（見 `get()` 內 `_cached_at`
        的賦值點）——供 `app.py` 的 `updated_at` 欄位使用，不得用請求當下的
        `now_fn()` 冒充：那會讓客戶端以為資料剛更新，實際上可能是十分鐘前
        （甚至因為 fail-open 續用舊值）抓到的。`None` 代表從未成功抓過。"""
        with self._lock:
            return self._cached_at

    def get(self) -> dict | None:
        now = self._now_fn()
        with self._lock:
            fresh = (self._cached_at is not None and now - self._cached_at < self._ttl_s)
            if fresh:
                return self._value
            stale = self._value
            if self._fetching:
                if stale is not None:
                    # 已有舊值：不必等進行中的下載，直接回舊值（single-flight
                    # 的重點是不重複觸發下載，不是把所有請求都串成一列）。
                    return stale
                # 沒有舊值可回退（首抓）：只能等那一條進行中的 thread 抓完。
                wait_event = self._fetch_done
                am_fetcher = False
            elif (self._failed_at is not None
                  and now - self._failed_at < self._failure_backoff_s):
                # ⭐ [8b-5] 上游故障冷卻期內：不接手新的 fetch，直接回舊值
                # （或 `None`，若從未成功過）——見類別檔頭。
                return stale
            else:
                self._fetching = True
                self._fetch_done = threading.Event()
                wait_event = self._fetch_done
                am_fetcher = True

        if not am_fetcher:
            # ⭐ [8b-2] 有上限地等待，不無限期掛住（見模組層 `_FETCH_WAIT_TIMEOUT_S`
            # 檔頭註解）——這個分支只在「沒有舊值可回退」時才會走到，逾時代表
            # 也沒有更好的答案，回 `None` 讓呼叫端轉譯 503 是唯一誠實的選項。
            if not wait_event.wait(timeout=_FETCH_WAIT_TIMEOUT_S):
                logger.error(
                    "leaderboard single-flight 等待逾時（%.0fs）：放棄等待，"
                    "回退 503 路徑（fetcher thread 本身不受影響，繼續跑）",
                    _FETCH_WAIT_TIMEOUT_S)
                return None
            with self._lock:
                return self._value

        # ⭐ [8b-2] try/finally：無論 `self._fetch()` 拋出的是 `Exception`
        # （上游失敗，已知路徑）還是任何其他 `BaseException`（未預期的錯誤、
        # 中斷訊號……），`_fetching`／`_fetch_done` 都必須被復位——否則這條
        # thread 一旦以非 `Exception` 的方式死亡，`_fetching` 會永遠卡在
        # `True`，後續所有「沒有舊值可回退」的呼叫都會在上面的 `wait_event.wait()`
        # 卡到逾時為止（有 [8b-2] 的 timeout 兜底，但根源仍應該是「快速釋放
        # single-flight 名額」，不是靠等待端的逾時苦撐）。
        success = False
        fetched: dict | None = None
        try:
            fetched = self._fetch()
            success = True
        except Exception as e:  # noqa: BLE001 — fail-open：上游失敗保留舊值，見檔頭
            logger.error("leaderboard 上游抓取失敗（%s）: %r",
                         "沿用舊值續用" if stale is not None else "無舊值可用，需上拋 503", e)
        finally:
            with self._lock:
                if success:
                    self._value = fetched
                    self._cached_at = self._now_fn()  # ⭐ 抓取完成後的時間戳（見檔頭）
                    self._failed_at = None  # [8b-5] 上游已恢復，清掉故障冷卻標記
                    self._sorted_cache.clear()  # 新世代到來，舊排序結果作廢
                else:
                    self._failed_at = self._now_fn()  # [8b-5] 進入故障冷卻期
                self._fetching = False
                self._fetch_done.set()
        return fetched if success else stale

    # ⭐ [8b-6] 2026-08-29 二輪複審 Suggestion：記憶化原本存**全量**排序結果
    # （可能上萬筆 leaderboardRows），但公開端點（`app.py`）的 `limit` 恆
    # `<= 100`（422 擋掉更大的值）——存全量等於為每個 `(世代, window)` 多留一份
    # 幾乎用不到的完整排序副本。改存截斷後的前 `_MEMOIZED_ROWS_CAP` 筆。
    _MEMOIZED_ROWS_CAP = 100

    def top_rows(self, window: str, limit: int) -> list[dict] | None:
        """依 `window` 排序＋裁切；同一世代（同一份 payload）＋window 的排序結果
        做記憶化（見類別檔頭 [C2]、[8b-6]）。`payload` 從未成功抓過 → `None`
        （呼叫端轉譯 503，與模組層 `top_rows` 呼叫慣例相同）；`window` 不合法
        → `ValueError`；`limit` 超過 `_MEMOIZED_ROWS_CAP`（100，公開端點的
        上限，見 [8b-6]）→ `ValueError`（fail-fast：悄悄截斷會讓呼叫端拿到
        「看起來正確、實際被砍短」的清單而不自知）。
        """
        if window not in WINDOWS:
            raise ValueError(f"不支援的 window: {window!r}")
        if limit > self._MEMOIZED_ROWS_CAP:
            raise ValueError(
                f"limit={limit} 超過記憶化上限 {self._MEMOIZED_ROWS_CAP}"
                "（公開端點本就不允許超過 100，見 [8b-6]）")
        payload = self.get()
        if payload is None:
            return None
        cache_key = (id(payload), window)
        with self._lock:
            sorted_rows = self._sorted_cache.get(cache_key)
        if sorted_rows is None:
            # 排序在鎖外算（可能是全量 36MB 的資料），只用鎖保護 dict 存取——
            # 極端情況下兩個並發 miss 各自算一次一樣的結果，是可接受的重工，
            # 不是正確性問題（見檔頭：single-flight 只保證下載只做一次，排序
            # 記憶化只是效能優化，不需要同等強度的互斥）。
            sorted_rows = _sorted_rows(payload, window)[:self._MEMOIZED_ROWS_CAP]
            with self._lock:
                self._sorted_cache[cache_key] = sorted_rows
        return sorted_rows[:limit]


def _window_perf(row: dict, window: str) -> dict:
    """`row["windowPerformances"]` 是 `[[period, {...}], ...]` 配對清單（已驗證
    外部事實），非 dict——線性掃描找對應 window。缺窗／形狀不符 → 空 dict。"""
    for pair in row.get("windowPerformances") or []:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        period, perf = pair
        if period == window and isinstance(perf, dict):
            return perf
    return {}


def _pnl_sort_key(row: dict, window: str) -> Decimal:
    """window 對應的 pnl，轉 Decimal 供排序（不得用 float——見檔頭）。
    缺窗／解析失敗／`NaN`（[S3] 2026-08-29 opus 審查：`Decimal("NaN")` 本身不會
    在建構時炸掉，但拿 NaN 跟其他 Decimal 做排序比較是未定義行為——一律排到最後，
    視為最小,不得讓一筆壞資料把整份排序炸掉或搞亂順序）。"""
    perf = _window_perf(row, window)
    try:
        value = Decimal(str(perf.get("pnl", "")))
    except (InvalidOperation, TypeError):
        return Decimal("-Infinity")
    if value.is_nan():
        return Decimal("-Infinity")
    return value


def _sorted_rows(payload: dict, window: str) -> list[dict]:
    """`top_rows` 的核心：`window` 的 pnl 降冪排序＋裁切成輸出結構
    `{address, display_name, account_value, pnl, roi, vlm}`（字串保留原精度，
    前端格式化），**不做 `limit` 截斷**——是 `LeaderboardCache.top_rows` 記憶化
    的對象（見該方法檔頭 [C2]），呼叫端自行對回傳值切片。"""
    rows = (payload or {}).get("leaderboardRows") or []
    sortable = [r for r in rows if isinstance(r, dict) and r.get("ethAddress")]
    sortable.sort(key=lambda r: _pnl_sort_key(r, window), reverse=True)
    out = []
    for r in sortable:
        perf = _window_perf(r, window)
        out.append({
            "address": r["ethAddress"],
            "display_name": r.get("displayName"),
            "account_value": r.get("accountValue"),
            "pnl": perf.get("pnl"),
            "roi": perf.get("roi"),
            "vlm": perf.get("vlm"),
        })
    return out


def top_rows(payload: dict, window: str, limit: int) -> list[dict]:
    """依 `window` 的 pnl 降冪排序，回傳前 `limit` 筆裁切結構（見 `_sorted_rows`）。

    `window` 不在 `WINDOWS` 白名單 → `ValueError`（呼叫端轉譯 422）。
    """
    if window not in WINDOWS:
        raise ValueError(f"不支援的 window: {window!r}")
    return _sorted_rows(payload, window)[:limit]
