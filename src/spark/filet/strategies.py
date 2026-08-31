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
from decimal import Decimal, InvalidOperation
from typing import Any

from spark.exchange.ledger_flows import FLOW_FIELDS
from spark.filet.leaders import LeaderRef

# ⚠️ 2026-08-29 使用者裁決移除「60 天上架閘門」（曾用 STRATEGY_MIN_LIVE_DAYS）：
# 免責已講清楚、平台本就不審查跟單來源（進階模式即明證）。listable 不再看
# covered_days。leader_perf 的 RATIO_MIN_DAYS（原 60 天，2026-08-30 D15 裁決降為
# 30 天，Sharpe/Sortino/年化波動樣本充足度）**不受影響**——那是誠實統計揭露，
# 不是准入門檻。見 plan §0.1 裁決 5。

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


def sample_days_from_perf(perf: dict[str, Any] | None) -> int:
    """perf 的 `covered_days`（涵蓋天數）→ 整數天數。`perf` 不可用或
    `status != "ok"` → 0（沿 `build_strategy_view` 既有算法抽出，供
    `build_strategy_view` 與 `/api/public/traders/{address}` 共用同一個算法，
    不重新發明，工程原則 1）。"""
    covered_days = Decimal("0")
    if isinstance(perf, dict) and perf.get("status") == "ok":
        cd = perf.get("covered_days")
        if isinstance(cd, Decimal):
            covered_days = cd
    return int(covered_days)


def build_strategy_view(entry: LeaderRef, perf: dict[str, Any] | None) -> dict[str, Any]:
    """`LeaderRef` ＋ 單一 perf window → 策略卡 dict（**不含** `follower_count`）。

    ⭐ `follower_count` 需要讀 followers manifest（跨客戶聚合、有 IO），本函式
    刻意保持純函式（零網路、零檔案 IO）——呼叫端（`publicapi/app.py`）在拿到
    這個 dict 之後，自己併入 `"follower_count"` 鍵。分開的理由：純函式才能在
    測試裡不落地任何檔案就直接餵假 perf 驗證計算邏輯（工程原則同型：職責邊界
    strutural 分開，而不是靠呼叫端記得「這裡不能傳跨客戶資料」）。

    `listable`＝`enabled` 且 `accepting_new`（2026-08-29 裁決移除 60 天涵蓋天數
    門檻，見模組檔頭）。`live_days` 仍由 perf 的 `covered_days` 算出，純展示用，
    不再影響 `listable`。
    `status`：`accepting_new` → `"running"`；否則（例行下架、仍在跟的不受影響）→
    `"paused"`（沿 `leaders.py` 檔頭的旗標語意，這裡只是把它投影成展示用字串）。
    """
    live_days = sample_days_from_perf(perf)
    listable = bool(entry.enabled and entry.accepting_new)
    status = "running" if entry.accepting_new else "paused"
    slug = entry.slug or entry.address
    return {
        "slug": slug,
        "name": entry.name,
        "tagline": entry.tagline,
        "tagline_en": entry.tagline_en,
        "featured": entry.featured,
        "leader_address": entry.address,
        "status": status,
        "listable": listable,
        "live_days": live_days,
        "min_notional_usd": entry.min_notional_usd,
        "max_leverage": entry.max_leverage,
        "metrics": build_metrics(perf),
    }


# ⭐ M3 round3 Task 3（D5 數字一致性）：CAGR 是策略詳情頁專屬的樣本閘（原 60 天，
# ⚠️ 2026-08-30 使用者裁決 D15 降為 30 天——目的是讓自營策略〔59 天實盤〕能在
# 站上完整呈現指標並可跟單，全鏈路（explore 30 天、本閘、leader_perf 的
# RATIO_MIN_DAYS、前端 TRADER_SAMPLE_THRESHOLD_DAYS）同步降為 30。
# **與 leader_perf 自己的 `annualized_return_insufficient_data`（90 天，
# `MIN_DAYS_FOR_ANNUALIZATION`）刻意不同一個門檻**——後者是「年化外推可信度」的
# 通用揭露分級，這裡是策略卡「要不要秀這張大字卡」的產品決策，兩者服務不同用途，
# 混用會讓 30–89 天之間的策略卡看到「有 annualized_return 但被前者的旗標關掉」
# 這種前端要另外判斷的岔路。呼叫端（`publicapi/app.py`）以 `live_days`（＝
# `int(covered_days)`，與 `build_strategy_view` 算 `live_days` 同一個值、同源）
# 對照本常數做結構性 gating：`sample_days < CAGR_SAMPLE_THRESHOLD_DAYS` 時
# 呼叫端整個不放 `cagr_pct` 鍵進回應（不是放 null），前端因此不必自己判斷門檻。
CAGR_SAMPLE_THRESHOLD_DAYS = 30


def build_cagr_pct(perf: dict[str, Any] | None) -> str | None:
    """CAGR（年化報酬）＝直接取 `leader_perf.compute_window_performance` 算好的
    `annualized_return`，**不重算**（同一支計算，見 D5 主線程裁決：前端刪除
    `strategyMetrics.ts` 的自算函式，一律由後端供給）。

    `perf` 缺席／`status != "ok"`／`annualized_return` 數學上無定義（帳戶歸零，
    `1+twr<=0`，leader_perf 對此整組 `annualized_return*` 鍵一起缺席）→ `None`。
    是否要把這個值**放進**回應（樣本天數門檻）由呼叫端決定，本函式只管「算不算
    得出來」。
    """
    if not (isinstance(perf, dict) and perf.get("status") == "ok"):
        return None
    v = perf.get("annualized_return")
    if v is None:
        return None
    return _quantize_pct_or_ratio(v * Decimal("100"))


# ⭐ M3 round4 Task R4-11（trader/strategy 詳情頁欄位對齊）：`sample_days`／
# `sample_threshold`／(可能的) `cagr_pct` 一次組裝，供 `/api/public/strategies/
# {slug}` 與 `/api/public/traders/{address}` 共用同一套組裝規則，不各自重複
# 「sample_days<CAGR_SAMPLE_THRESHOLD_DAYS 時整個不放 cagr_pct 鍵」這條判斷
# （工程原則 1：同一個值只能有一個計算來源）。
def build_cagr_fields(perf: dict[str, Any] | None, *, sample_days: int) -> dict[str, Any]:
    """回傳 `{"sample_days", "sample_threshold", 可能的 "cagr_pct"}`，呼叫端
    `dict.update()` 併入自己的回應。`sample_days` 由呼叫端傳入（strategies 端點
    傳 `view["live_days"]`；traders 端點傳 `sample_days_from_perf(perf)`）——
    兩端點的「涵蓋天數」計算方式本就不同源（前者來自 `LeaderRef` 白名單條目的
    perf，後者是任意鏈上地址），但門檻判斷與 `cagr_pct` 是否放進回應的規則
    完全相同，故只抽出這條共用規則，不強迫兩端點共用 `sample_days` 的計算。
    """
    out: dict[str, Any] = {
        "sample_days": sample_days,
        "sample_threshold": CAGR_SAMPLE_THRESHOLD_DAYS,
    }
    if sample_days >= CAGR_SAMPLE_THRESHOLD_DAYS:
        cagr_pct = build_cagr_pct(perf)
        if cagr_pct is not None:
            out["cagr_pct"] = cagr_pct
    return out


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


def build_equity_range(
    account_value_history: Any,
) -> tuple[Decimal | None, Decimal | None]:
    """`hl.portfolio()` 的原始 `accountValueHistory`（`leader_perf.extract_window`
    回傳的 `av`，形狀 `[[ts_ms, value_str], ...]`）→ `(start_equity_usd,
    end_equity_usd)`。

    起點＝**首個非零值**（前導 0 點跳過——錢包晚於序列起點入金時，首點常是
    0，「以 $0 起算」是誤導不是揭露，2026-08-30 使用者裁決）；終點＝序列**最後
    一點**（不過濾，即使剛好是 0 也照實顯示——那是真實的期末餘額，不是「無
    資料」）。

    整條序列從頭到尾都是 0（或序列為空／形狀不符）→ `(None, None)`：沒有任何
    一刻是「真的有本金」的快照，null 比硬印 $0 起算更誠實。"""
    if not isinstance(account_value_history, (list, tuple)) or not account_value_history:
        return None, None
    start: Decimal | None = None
    for row in account_value_history:
        try:
            v = Decimal(str(row[1]))
        except (InvalidOperation, ValueError, IndexError, TypeError):
            continue
        if v != 0:
            start = v
            break
    if start is None:
        return None, None
    try:
        end = Decimal(str(account_value_history[-1][1]))
    except (InvalidOperation, ValueError, IndexError, TypeError):
        return None, None
    return start, end


def sum_ledger_deposits(ledger_updates: Any) -> Decimal | None:
    """`hl.non_funding_ledger_updates()` 的原始清單 → 真實入金本金（USD）。

    只加總 `delta.type == "deposit"` 的金額（`spark.exchange.ledger_flows.
    FLOW_FIELDS["deposit"]` 是欄位名的唯一定義點——不在這裡另猜一次，工程原則
    5）。**不含** `send`／`accountClassTransfer`／`vaultDeposit` 等：那些是同一
    帳戶內部（spot↔perp、跨 dex）的資金搬動，不是新增本金（2026-08-30 對
    `0xfB9C…9760` 主網 probe 實測：這顆帳戶的 ledger 有 1 筆 `deposit`（真實外部
    入金）＋ 3 筆 `send`（同位址 spot→perp 內部轉帳），只有前者代表真金白銀
    進來過）。

    有多筆 `deposit` → 全部加總（真實入金本金 = 這個帳戶史上總共存入過多少）。
    形狀不符／查無任何 `deposit` 紀錄 → `None`（不猜；起訖淨值仍由
    `build_equity_range` 單獨供給，見 `publicapi/app.py` 呼叫端）。"""
    if not isinstance(ledger_updates, list):
        return None
    amount_field, sign = FLOW_FIELDS["deposit"]
    total = Decimal("0")
    found = False
    for item in ledger_updates:
        if not isinstance(item, dict):
            continue
        delta = item.get("delta")
        if not isinstance(delta, dict) or delta.get("type") != "deposit":
            continue
        amount = delta.get(amount_field)
        if amount is None:
            continue
        try:
            total += sign * Decimal(str(amount))
        except (InvalidOperation, ValueError):
            continue
        found = True
    return total if found else None


def build_methodology(perf: dict[str, Any] | None, *,
                      initial_deposit_usd: Decimal | None,
                      start_equity_usd: Decimal | None = None,
                      end_equity_usd: Decimal | None = None,
                      updated_at: int) -> dict[str, Any]:
    """策略詳情頁的方法論與樣本揭露段。`initial_deposit_usd` 由呼叫端傳入
    （2026-08-30 起改取自 `hl.non_funding_ledger_updates()` 的真實入金加總
    ——`sum_ledger_deposits`，不再是 `accountValueHistory` 首點；那個首點常態
    性是 0，見 `build_equity_range` 檔頭）。`start_equity_usd`／
    `end_equity_usd` 同樣由呼叫端傳入（`build_equity_range` 算好），與
    `initial_deposit_usd` 同源自呼叫端同一次 `hl.portfolio()` 回應，本模組不
    重新觸網。

    `risk_free_rate`／`annualization_days`／`basis` 是 leader_perf 全模組通用的
    計算慣例（365 日/年、無風險利率 0%），寫死在這裡是把「這份文案講的假設」與
    「compute_window_performance 實際用的假設」釘在同一批常數——leader_perf 若
    哪天改了年化天數，這裡也要跟著改，用寫死的字面值提醒維護者。

    ⚠️ 2026-08-31 issue log I-15 使用者裁決：`basis` 由 `"perp"` 改為
    `"combined"`——呼叫端（`publicapi/app.py`）已改用 `leader_perf.
    compute_window_performance(rows, "allTime")`（spot+perp 合併窗，原
    `"perpAllTime"`），本欄位釘的是「呼叫端實際用的假設」，必須跟著換，
    否則這份文案會對合併窗的數字謊稱是 perp-only 算出來的。理由見
    `leader_perf.py` 檔頭「I-15」段。
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
        "start_equity_usd": (str(start_equity_usd)
                             if start_equity_usd is not None else None),
        "end_equity_usd": (str(end_equity_usd)
                           if end_equity_usd is not None else None),
        "sample_count": sample_count if isinstance(sample_count, int) else None,
        "annualization_days": 365,
        "risk_free_rate": "0",
        "basis": "combined",
        "updated_at": updated_at,
    }
