"""src/spark/publicapi/public_stats.py
`/api/public/stats`、`/api/public/status` 的純邏輯層（策略平台改版 Task 6）。

⭐⭐ 公開狀態頁必須比被監控對象更可靠：任何子資料源（accrued 歷史檔、心跳目錄、
featured 策略的績效查詢）丟出的例外**一律降級為該欄 `null`／`"unknown"`，端點恆回
200**——一個「查看系統健不健康」的端點自己先 500，是這類頁面最不該犯的錯。
每個 compute 函式因此各自 try/except，並在呼叫端（`publicapi/app.py`）的路由層
再包一層防禦（見該檔），雙重保險不是重複——任一層漏接都不能讓另一層也漏接。

⭐ `routed_volume_usd_total` 的推導與 `/api/ops/revenue` 同源、只取總量
---------------------------------------------------------------------
`/api/ops/revenue`（`app.ops_revenue`）用 `ops.load_accrued_series` 讀
`accrued_history.jsonl`，取**相鄰兩點**算今昨差（實收北極星）。本模組讀的是
**同一份檔案、同一個 loader**，但只取**最新一點**的累積值當「歷史總額」——
`query_builder_accrued` 回的是 builder 位址自註冊以來的**累積**應計費用（見
`scripts/copytrade_daily_report.py` 檔頭），不是單日增量，所以最新一點本身就是
「至今為止」的總量，不需要相鄰兩點。

USD 路由量無法直接取得（accrued 歷史記的是 fee，不是 notional），故用固定費率
反推：`volume = accrued_total / (BUILDER_FEE_BPS / 10000)`。`BUILDER_FEE_BPS`
與 `/api/public/stats` 回應裡的 `builder_fee_bps` 欄位是**同一個常數**（見下方
定義），兩者不可能各自漂移；若未來費率調整，反推的歷史總量會連帶失真——這是
「用單一目前費率反推歷史總量」這個近似法本身的局限，不是這裡的 bug，記在此處
供未來讀者一眼看到。

⭐ engine 元件的新鮮度判定**只看檔案 mtime**，不解析內容
--------------------------------------------------------
plan 明訂：「存在且 mtime < 10 分鐘 → ok；否則 degraded；讀不到 → unknown」。
刻意不解析心跳 JSON 內容（`filet.engine_health.read_heartbeat` 那一份是給
admin 面板用的，會讀出 `account_id` 等欄位）——公開端點不需要，也結構上更不會
不小心把 follower 識別資訊帶出來（不變量 4）。多 follower 引擎時取目錄裡
**最新一個檔案的 mtime** 代表整體，回應裡完全不出現檔名／account_id／有幾個檔。
"""
from __future__ import annotations

import logging
import threading
from decimal import Decimal
from pathlib import Path
from typing import Callable

from spark.publicapi.ops import load_accrued_series

logger = logging.getLogger(__name__)

# 本站對外公告的固定 builder 費率（bps）。與 routed_volume 反推公式共用同一個
# 常數（見檔頭），故只能在此定義一次。
BUILDER_FEE_BPS = 2
_BPS_DENOMINATOR = Decimal(10000)

# engine 元件的心跳過期門檻。與 `filet.engine_health.HEARTBEAT_STALE_S` /
# `publicapi.ops.ENGINE_STALE_S` 同值（600s＝連續 10 個 cycle 沒有心跳）——三處
# 回答的是同一個問題（引擎最近有沒有動），刻意保持同值但不 import 共用同一個符號：
# 本模組只看檔案 mtime、不碰心跳內容，語意上是獨立的判定，值恰好相同是刻意對齊。
ENGINE_HEARTBEAT_STALE_S = 600.0

# 兩端點共用的 in-process cache TTL。
CACHE_TTL_S = 60.0

# 元件狀態的嚴重度排序：unknown 最嚴重（讀不到 ≠ 沒事，見工程原則「讀不到資料
# ≠ 進入危險態」的反向應用——這裡是「讀不到資訊 ≠ 系統健康」，不確定不能顯示成
# 比 degraded 更安心的狀態）。
_STATUS_SEVERITY = {"ok": 0, "degraded": 1, "unknown": 2}


class TTLCache:
    """單一值的 in-process 快取（預設 60s TTL）。

    ⭐ 與 `publicapi.app` 既有的 `_cached_strategy_portfolio` 同一個模式
    （dict/值 + Lock + `now_fn()` 比較），抽成小型可重用類別供 `/api/public/stats`
    與 `/api/public/status` 共用同一份快取邏輯（plan Task 6：「兩端點共用
    in-process cache」）——每個端點各自持有一個獨立實例（各自的資料源例外互不
    影響），共用的是機制，不是同一份資料。

    `now_fn` 必須注入（不得偷用 `time.time`）：測試靠假時鐘釘死 TTL 邊界，
    沿本檔其餘函式與 `publicapi.app` 全域的 `now_fn` 注入慣例。
    """

    def __init__(self, *, now_fn: Callable[[], float], ttl_s: float = CACHE_TTL_S):
        self._now_fn = now_fn
        self._ttl_s = ttl_s
        self._lock = threading.Lock()
        self._value = None
        self._cached_at: float | None = None

    def get(self, compute: Callable[[], dict]) -> dict:
        """TTL 內回快取值；否則呼叫 `compute()` 並更新快取。

        ⚠️ `compute()` 本身不持鎖：資料源查詢（檔案 IO）可能較慢，持鎖跑 IO 會讓
        並發請求互相卡住排隊，而快取的意義正是要避免這種放大。鎖只保護「讀/寫
        快取狀態」這幾行，不保護計算過程——多個請求在 TTL 剛過期的瞬間並發，
        最壞情況是重複算個一兩次，比整批請求排隊等一次 IO 划算。
        """
        now = self._now_fn()
        with self._lock:
            if self._cached_at is not None and now - self._cached_at < self._ttl_s:
                return self._value
        value = compute()
        with self._lock:
            self._value = value
            self._cached_at = now
        return value


def compute_routed_volume_usd_total(accrued_history_path) -> Decimal | None:
    """歷史總路由成交名目（USD）。無歷史資料／讀取失敗 → `None`（不是 0——0 會被
    讀成「至今沒有任何路由量」，那是另一個陳述）。"""
    try:
        series = load_accrued_series(accrued_history_path)
    except Exception as e:  # noqa: BLE001 — 公開端點：任何子源例外都不得讓端點 500
        logger.error("accrued 歷史讀取失敗（routed_volume 降級為 null）: %r", e)
        return None
    if not series:
        return None
    total_fee = series[-1].accrued
    if not isinstance(total_fee, Decimal):
        return None
    # ⭐ 先乘後除（`total_fee * 10000 / BUILDER_FEE_BPS`），不要 `total_fee /
    # (BUILDER_FEE_BPS/10000)`：Decimal 除以一個帶負指數的商（0.0002）在 Python
    # 的 Decimal context 下會正規化成科學記號（`4.28E+6`），`str()` 後就不是
    # 一般人讀的十進位字串——這裡要的是可以直接顯示的金額，不是精確度考量。
    return total_fee * _BPS_DENOMINATOR / Decimal(BUILDER_FEE_BPS)


def compute_live_days(entries, perf_for: Callable[[str], dict | None]) -> int | None:
    """featured 策略的 `covered_days`（無 featured 條目、perf 缺席或查詢失敗 →
    `None`）。

    `entries`：`strategies._public_strategy_entries()` 的輸出（`enabled=True`
    條目）。`perf_for`：address → `leader_perf.compute_window_performance` 的
    結果或 `None`；呼叫端注入以複用既有的 60s portfolio 快取
    （`app._strategy_perf_for`），本函式不重新觸網。
    """
    featured = next((e for e in entries if getattr(e, "featured", False)), None)
    if featured is None:
        return None
    try:
        perf = perf_for(featured.address)
    except Exception as e:  # noqa: BLE001 — 同上
        logger.error("featured 策略績效查詢失敗（live_days 降級為 null）: %r", e)
        return None
    if not isinstance(perf, dict) or perf.get("status") != "ok":
        return None
    covered_days = perf.get("covered_days")
    if isinstance(covered_days, Decimal):
        return int(covered_days)
    return None


def build_stats_payload(*, accrued_history_path, entries,
                        perf_for: Callable[[str], dict | None],
                        now_fn: Callable[[], float]) -> dict:
    """`/api/public/stats` 回應 dict。任一子項取不到 → 該欄 `null`，恆回可序列化
    的結構（呼叫端仍需 200，本函式不拋）。"""
    routed_volume = compute_routed_volume_usd_total(accrued_history_path)
    live_days = compute_live_days(entries, perf_for)
    return {
        "routed_volume_usd_total": (str(routed_volume)
                                    if routed_volume is not None else None),
        "builder_fee_bps": BUILDER_FEE_BPS,
        "live_days": live_days,
        "updated_at": int(now_fn()),
    }


def _newest_heartbeat_mtime(heartbeat_dir) -> float | None:
    """heartbeat 目錄中最新檔案的 mtime（epoch 秒）。目錄不存在／不可讀／無任何
    `.json` 檔 → `None`（**未知**，不是「過期」：分不清「引擎從未寫過」與
    「我讀錯地方／權限問題」，但兩者都不該被顯示成「新鮮」）。

    ⭐ 只讀檔名與 mtime，不開檔、不解析內容——不變量 4（`/api/public/*` 不得洩漏
    follower 個資）在這裡是結構性的：連讀取路徑上都拿不到任何 account_id 以外
    的資訊（而 account_id 本身也從不進回應）。
    """
    p = Path(heartbeat_dir)
    try:
        entries = list(p.iterdir())
    except OSError:
        return None
    mtimes: list[float] = []
    for entry in entries:
        if entry.suffix != ".json":
            continue
        try:
            mtimes.append(entry.stat().st_mtime)
        except OSError:
            continue
    return max(mtimes) if mtimes else None


def engine_component_status(heartbeat_dir, *, now_fn: Callable[[], float],
                            stale_s: float = ENGINE_HEARTBEAT_STALE_S) -> str:
    """`engine` 元件狀態：`"ok"` / `"degraded"` / `"unknown"`。

    未來時刻的 mtime（時鐘跳動或手工放的檔）視為 `"unknown"`：同 `engine_health
    .read_heartbeat` 對負年齡的處理——一個算不出正確年齡的心跳沒辦法證明自己新鮮。
    任何未預期例外一律降級為 `"unknown"`，不外拋（公開狀態頁本身要比被監控對象
    可靠）。
    """
    try:
        mtime = _newest_heartbeat_mtime(heartbeat_dir)
        if mtime is None:
            return "unknown"
        age = now_fn() - mtime
        if age < 0:
            return "unknown"
        return "ok" if age < stale_s else "degraded"
    except Exception as e:  # noqa: BLE001 — 公開端點：任何子源例外都不得讓端點 500
        logger.error("engine 心跳新鮮度判定失敗（降級為 unknown）: %r", e)
        return "unknown"


def overall_status(components: list[dict]) -> str:
    """整體狀態＝最差的 component（嚴重度排序見 `_STATUS_SEVERITY`）。空清單視為
    `"unknown"`（沒有任何元件可回答＝不知道系統狀態，不是「什麼都沒壞」）。"""
    if not components:
        return "unknown"
    worst = max(components, key=lambda c: _STATUS_SEVERITY.get(c.get("status"), 2))
    return worst["status"]


def build_status_payload(*, heartbeat_dir, now_fn: Callable[[], float],
                         stale_s: float = ENGINE_HEARTBEAT_STALE_S) -> dict:
    """`/api/public/status` 回應 dict。

    `api` 元件恆為 `"ok"`：這個端點自己能被呼叫並回應，本身就是 API 進程存活的
    證明——不需要另一個資料源去驗證「我正在回應這個請求」這件事。
    """
    engine_status = engine_component_status(heartbeat_dir, now_fn=now_fn,
                                            stale_s=stale_s)
    components = [
        {"name": "api", "status": "ok"},
        {"name": "engine", "status": engine_status},
    ]
    return {
        "status": overall_status(components),
        "components": components,
        "updated_at": int(now_fn()),
    }
