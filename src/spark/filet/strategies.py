"""src/spark/filet/strategies.py
策略視圖：精選白名單條目（`LeaderRef`）＋單一 perf window（`leader_perf.
compute_window_performance` 的輸出）→ `/api/public/strategies*` 的策略物件。

**純函式、零網路、零跨客戶 IO**：`follower_count`（需要讀 followers manifest，
跨客戶聚合）刻意**不**在這裡計算，由呼叫端（`publicapi/app.py`）算好後併進本模組
回傳的 dict——見 `build_strategy_view` docstring。

⭐ 策略平台自己的「insufficient → null」揭露契約（比 leader_perf 更嚴格）
--------------------------------------------------------------------
`leader_perf.compute_window_performance` 的既有契約是「資料薄也照樣給數字，
只是帶一個 `*_insufficient_data` 標記」（見該模組檔頭「揭露模型改版」）——那是為了
排行榜／目錄頁那種可以逐欄檢查標記的畫面設計的。策略卡是給決策用的**單一數字**
（例如首頁的「Sharpe 10.24」），不是排行榜列，讓資料薄的外推數字直接印在卡片上
比多一格「樣本不足」更容易誤導。所以本模組在 leader_perf 的基礎上**再收緊一層**：
數字不足 → 該欄位回 `null`，並附 `"<key>_insufficient": true`（前端據此顯示
「樣本不足」，不渲染任何數字）。這是 Task 5 plan 明訂的契約，刻意與 leader_perf
的既有契約不同層級、不同用途。
"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from spark.filet.leaders import LeaderRef

# 策略要能「上架可跟單」（listable）所需的最低涵蓋天數。刻意與 leader_perf 的
# RATIO_MIN_DAYS（60 天）同值：一個 listable 的策略，比率型指標（Sharpe/Sortino/
# 年化波動）必然已經跨過各自的充足度門檻——這個對齊不是巧合，是「能上架＝
# 卡片上每個數字都經得起看」的設計意圖。
STRATEGY_MIN_LIVE_DAYS = Decimal("60")

_TWO_DP = Decimal("0.01")


def _quantize_pct_or_ratio(v: Decimal) -> str:
    """統一兩位小數（策略卡示意形狀的精度慣例，見 plan JSON 錨例）。"""
    return str(v.quantize(_TWO_DP))


# 公開 key → (perf 值鍵, perf 不足旗標鍵或 None, 是否 ×100 轉成百分比, 是否取負號)。
# 取負號只用在 max_drawdown：`leader_perf._max_drawdown` 回傳非負的「跌幅大小」，
# 策略卡按 plan 錨例（"max_drawdown_pct": "-0.80"）以負值呈現回撤方向。
_METRIC_SPEC: tuple[tuple[str, str, str | None, bool, bool], ...] = (
    ("total_return_pct", "twr", "twr_insufficient_data", True, False),
    ("max_drawdown_pct", "max_drawdown", "max_drawdown_insufficient_data", True, True),
    ("sharpe", "sharpe", "sharpe_insufficient_data", False, False),
    ("sharpe_se", "sharpe_se", "sharpe_insufficient_data", False, False),
    ("win_rate_pct", "win_rate", None, True, False),
    ("annualized_vol_pct", "annualized_vol", "annualized_vol_insufficient_data",
     True, False),
    ("sortino", "sortino", "sortino_insufficient_data", False, False),
    ("best_day_pct", "best_day_return", None, True, False),
    ("worst_day_pct", "worst_day_return", None, True, False),
)


def build_metrics(perf: dict[str, Any] | None) -> dict[str, Any]:
    """單一 perf window（呼叫端固定傳 `perpAllTime`）→ 策略卡的 `metrics` 子物件。

    `perf` 非 `status == "ok"`（缺席、`insufficient`）→ 全部欄位 null＋
    `*_insufficient=True`、`sample_count=0`（沿 leader_perf 的 `_insufficient()`：
    這一類是「算不出來」，不是「算得出來但薄」，沒有任何數字可言）。

    `status == "ok"` 但個別指標在數學上算不出來（N<2、標準差為 0、DD 為 0）
    → 該指標的值鍵**不在** perf 字典裡（leader_perf 的既有契約），一樣視為
    insufficient——與「算得出來但天數不足」是同一種對外呈現（都是「這裡沒有
    看起來夠格的數字」），差別只在成因，前端不需要區分兩種「沒有」。
    """
    ok = isinstance(perf, dict) and perf.get("status") == "ok"
    out: dict[str, Any] = {}
    for pub_key, val_key, insuff_key, is_pct, negate in _METRIC_SPEC:
        value = None
        insufficient = True
        if ok and val_key in perf:
            flagged = bool(perf.get(insuff_key)) if insuff_key else False
            if not flagged:
                v = perf[val_key]
                if is_pct:
                    v = v * Decimal("100")
                if negate:
                    v = -v
                value = _quantize_pct_or_ratio(v)
                insufficient = False
        out[pub_key] = value
        out[f"{pub_key}_insufficient"] = insufficient
    sample_count = perf.get("sample_count") if isinstance(perf, dict) else None
    out["sample_count"] = sample_count if isinstance(sample_count, int) else 0
    return out


def build_strategy_view(entry: LeaderRef, perf: dict[str, Any] | None) -> dict[str, Any]:
    """`LeaderRef` ＋ 單一 perf window → 策略卡 dict（**不含** `follower_count`）。

    ⭐ `follower_count` 需要讀 followers manifest（跨客戶聚合、有 IO），本函式
    刻意保持純函式（零網路、零檔案 IO）——呼叫端（`publicapi/app.py`）在拿到
    這個 dict 之後，自己併入 `"follower_count"` 鍵。分開的理由：純函式才能在
    測試裡不落地任何檔案就直接餵假 perf 驗證計算邏輯（工程原則同型：職責邊界
    strutural 分開，而不是靠呼叫端記得「這裡不能傳跨客戶資料」）。

    `listable`＝`enabled` 且 `accepting_new` 且 `covered_days >= STRATEGY_MIN_LIVE_DAYS`。
    `status`：`accepting_new` → `"running"`；否則（例行下架、仍在跟的不受影響）→
    `"paused"`（沿 `leaders.py` 檔頭的旗標語意，這裡只是把它投影成展示用字串）。
    """
    covered_days = Decimal("0")
    if isinstance(perf, dict) and perf.get("status") == "ok":
        cd = perf.get("covered_days")
        if isinstance(cd, Decimal):
            covered_days = cd
    live_days = int(covered_days)
    listable = bool(entry.enabled and entry.accepting_new
                    and covered_days >= STRATEGY_MIN_LIVE_DAYS)
    status = "running" if entry.accepting_new else "paused"
    slug = entry.slug or entry.address
    return {
        "slug": slug,
        "name": entry.name,
        "tagline": entry.tagline,
        "featured": entry.featured,
        "leader_address": entry.address,
        "status": status,
        "listable": listable,
        "live_days": live_days,
        "min_notional_usd": entry.min_notional_usd,
        "max_leverage": entry.max_leverage,
        "metrics": build_metrics(perf),
    }


def build_equity_index(perf: dict[str, Any] | None) -> list[str]:
    """perf 的 `equity_index`（Decimal 序列，出入金中性化）→ jsonable 字串陣列。

    `perf` 不可用或 `status != "ok"` → `[]`（沒有序列可畫，不是錯誤）。
    """
    if not isinstance(perf, dict) or perf.get("status") != "ok":
        return []
    idx = perf.get("equity_index")
    if not isinstance(idx, (list, tuple)):
        return []
    return [str(x) for x in idx]


def _ms_to_date(ms: Any) -> str | None:
    if not isinstance(ms, int) or isinstance(ms, bool):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


def build_methodology(perf: dict[str, Any] | None, *,
                      initial_deposit_usd: Decimal | None,
                      updated_at: int) -> dict[str, Any]:
    """策略詳情頁的方法論與樣本揭露段。`initial_deposit_usd` 由呼叫端傳入
    （取自同一次 `hl.portfolio()` 回應的 `accountValueHistory` 首點——本模組
    不重新觸網，見 `publicapi/app.py` 的 detail 端點）。

    `risk_free_rate`／`annualization_days`／`basis` 是 leader_perf 全模組通用的
    計算慣例（365 日/年、無風險利率 0%、perp only），寫死在這裡是把「這份文案
    講的假設」與「compute_window_performance 實際用的假設」釘在同一批常數——
    leader_perf 若哪天改了年化天數，這裡也要跟著改，用寫死的字面值提醒維護者。
    """
    ok = isinstance(perf, dict) and perf.get("status") == "ok"
    first_ms = perf.get("first_ts_ms") if ok else None
    last_ms = perf.get("last_ts_ms") if ok else None
    sample_count = perf.get("sample_count") if ok else None
    return {
        "start_date": _ms_to_date(first_ms),
        "end_date": _ms_to_date(last_ms),
        "initial_deposit_usd": (str(initial_deposit_usd)
                                if initial_deposit_usd is not None else None),
        "sample_count": sample_count if isinstance(sample_count, int) else None,
        "annualization_days": 365,
        "risk_free_rate": "0",
        "basis": "perp",
        "updated_at": updated_at,
    }
