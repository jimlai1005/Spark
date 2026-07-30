"""src/spark/publicapi/ops.py
營運後台（/ops，管理端）的純資料層：每客戶損益 ＋ 收入對帳。純函式，無 FastAPI
裝飾器——所有外部依賴（adapter / store / 檔案路徑）由呼叫端注入，可離線測試。

⭐ 存取模式警告：本模組是全 repo 唯一的**跨客戶聚合**——其餘端點都是 session-scoped
（客戶只看自己）。呼叫端必須掛 admin 白名單閘（app.py 的 `_require_admin`，
與 /api/admin/pending 同一道），這裡刻意不做授權：授權是路由層的結構性職責，
在資料層再做一次只會製造「兩個地方各記得一半」的縫。

⭐ 紅線（北極星查一次不加總，沿 filet/aggregate.py）：
`revenue_reconciliation` 的 `accrued_delta` **只能**來自參數（builder 層級查一次
的今昨差），結構上不從 rows 推導或加總——rows 的 builder_fee 是**歸屬／應收**，
兩者的差額正是本函式要算的對帳訊號，互相取代等於把訊號歸零。
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import NamedTuple

from spark.copytrade.killswitch import count_alerts
from spark.copytrade.report import compute_trade_quality
from spark.filet.aggregate import collect_follower_summary
from spark.filet.engine_health import HEARTBEAT_STALE_S
from spark.publicapi.billing import map_stripe_status

logger = logging.getLogger(__name__)


def customer_pnl(followers, adapter, start: datetime, end: datetime,
                 *, store=None) -> list[dict]:
    """每客戶損益列（跨 follower 聚合）。

    每列 = `collect_follower_summary` 的 fills 衍生量（notional / builder_fee /
    taker_share）＋ `account_value`（perp accountValue）＋ `subscription`。

    ⭐ 跨 follower 隔離（工程原則 3/4）：任一 follower 的查詢失敗**不得**中止其他。
    `collect_follower_summary` 已有此語意（錯誤入 summary.error）；`account_value`
    與 billing 查詢在此各自 try/except，錯誤併入該列的 `error` 而非外拋——
    一個客戶的 API 錯誤讓整張營運報表變 500 是最糟的失敗模式。
    失敗不靜默：錯誤原文留在該列 `error`，前端可見（不 log 完就吞）。

    subscription：未給 store → "unknown"（無從得知，不假裝是 "none"）；
    給了 store 但查無記錄 → "none"；查詢本身失敗 → "unknown" ＋ 併入 error。
    """
    rows: list[dict] = []
    for ref in followers:
        summary = collect_follower_summary(ref, adapter, start, end)
        errors = [summary.error] if summary.error else []

        try:
            account_value = adapter.get_account_value(ref.user_address)
        except Exception as e:  # noqa: BLE001 — 跨 follower 隔離，錯誤入列不外拋
            account_value = None
            errors.append(f"account_value 查詢失敗: {e}")

        subscription = "unknown"
        if store is not None:
            try:
                rec = store.get_billing(ref.account_id)
                subscription = rec.status if rec is not None else "none"
            except Exception as e:  # noqa: BLE001 — 同上
                errors.append(f"subscription 查詢失敗: {e}")

        rows.append({
            "account_id": ref.account_id,
            "user_address": ref.user_address,
            "label": ref.label,
            "network": ref.network,
            "fills": summary.fills,
            "notional": summary.notional,
            "builder_fee": summary.builder_fee,
            "taker_share": summary.taker_share,
            "account_value": account_value,
            "subscription": subscription,
            "error": "; ".join(errors) if errors else None,
        })
    return rows


def revenue_reconciliation(rows, accrued_now: Decimal, accrued_prev: Decimal,
                           *, threshold_pct: Decimal) -> dict:
    """收入對帳：**應收（歸屬）** vs **實收（北極星）**，兩者不可互相取代。

    - `attributed`（應收／歸屬）＝ Σ rows 的 builder_fee。來自各 follower 的 fills
      明細，是「這筆收入該記在誰頭上」的歸屬分析。它**不是**權威收入數字：
      fills 可能漏抓、時間窗邊界可能切錯、builderFee 欄位可能缺。
    - `accrued_delta`（實收／北極星）＝ `accrued_now - accrued_prev`，builder 位址
      全域累積量的今昨差——**查一次，不跨 follower 加總**（紅線；沿 filet/aggregate.py
      的 `builder_fee_delta`）。這是權威值。本函式**只從參數取**這兩個數，
      結構上不從 rows 推導——若哪天有人「順手」用 Σrows 補上缺的 accrued，
      discrepancy 會恆為 0，對帳訊號被自己消滅掉。
    - `discrepancy` ＝ 實收 − 應收。非 0 代表歸屬邏輯與鏈上實收對不上，要查。

    ⚠️ 同基準要求（工程原則 1）：呼叫端必須保證 rows 的時間窗與 accrued 今昨差
    覆蓋**同一段期間**，否則 discrepancy 是窗口錯配的假訊號、不是真對不上。
    唯一正確的窗口是 `(AccruedPoint[前].captured_at, AccruedPoint[後].captured_at]`
    ——accrued 是查詢當下的累積量，不是日曆日的量（opus 對抗審查 Critical：日報
    cron 排在 00:10 時，用日曆日取 fills 會與 accrued 增量錯開整整一天）。
    快照時刻缺漏時呼叫端必須**放棄計算**，不得用日期回推（見 app.ops_revenue）。

    除零防護：`attributed` 為 0 時 `discrepancy_pct` 回 None（不得除零）；
    但若 `accrued_delta` 非 0（收到費用卻歸屬不到任何客戶）仍判 `over_threshold`
    為 True——異常不得因為算不出百分比而靜默放行。若 `accrued_delta` 也為 0
    則 `over_threshold` 為 False（無收費，無異常）。
    """
    attributed = sum((_as_decimal(r.get("builder_fee")) for r in rows), Decimal("0"))
    # ⭐ 只從參數取；此處刻意不出現任何 rows 的聚合（紅線：北極星不由 rows 推導）
    accrued_delta = Decimal(str(accrued_now)) - Decimal(str(accrued_prev))
    discrepancy = accrued_delta - attributed
    threshold = Decimal(str(threshold_pct))
    # attributed == 0 但 accrued_delta != 0：收到費用卻歸屬不到任何客戶——
    # 百分比無從計算（除零），但這是該告警的異常，不得因為算不出比例而靜默放行。
    if attributed == 0:
        pct = None
        over_threshold = accrued_delta != 0
    else:
        pct = abs(discrepancy) / attributed
        over_threshold = pct > threshold
    return {
        "attributed": attributed,
        "accrued_delta": accrued_delta,
        "accrued_now": Decimal(str(accrued_now)),
        "accrued_prev": Decimal(str(accrued_prev)),
        "discrepancy": discrepancy,
        "discrepancy_pct": pct,
        "over_threshold": over_threshold,
        "threshold_pct": threshold,
        "rows": len(rows),
    }


# ---------- 成交品質（跨 follower 聚合） ----------

# per-follower 的 skipped 小額落檔版面（引擎狀態根之下，一天一個檔）。
# ⭐ 由 scripts/copytrade_daily_report.load_skipped 的同一個版面推導，不另拼一次：
# 兩份路徑定義漂移的症狀是「營運面板永遠顯示沒有 skipped」——一個安靜的零。
SKIPPED_RELDIR = Path("var/copytrade/skipped")


def skipped_path_for(state_base, account_id: str, day_iso: str) -> Path:
    """某個 follower 某一天的 skipped 小額檔（**寫端與讀端的單一定義**）。"""
    return Path(state_base) / account_id / SKIPPED_RELDIR / f"{day_iso}.json"


def load_skipped_notional(path) -> Decimal | None:
    """讀一天的 skipped 小額名目總和；**無檔或壞檔 → None（不是 0）**。

    ⭐ `None` 與 `0` 在這裡是兩件完全不同的事：0 是「今天沒有跳過任何小額單」，
    None 是「不知道」。把讀不到當 0 會讓面板顯示一個看起來健康的零——
    而 skipped 比例正是用來發現「引擎其實一直在跳過客戶的單」的指標，
    它安靜地歸零等於把這個訊號關掉（健康面板謊報健康）。
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        return sum((Decimal(str(item["notional"])) for item in data), Decimal("0"))
    except (OSError, ValueError, KeyError, TypeError, InvalidOperation) as e:
        logger.warning("skipped 小額檔讀取失敗（%s）: %r", p, e)
        return None


def utc_days_in_window(start: datetime, end: datetime) -> list[str]:
    """窗口涵蓋的 UTC 日期清單（含端點所在日）。"""
    d, last = start.date(), end.date()
    out = []
    while d <= last:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _window_is_whole_utc_days(start: datetime, end: datetime) -> bool:
    """窗口是否恰好由整個 UTC 日組成（兩端都在午夜）。

    ⭐ 這個判定決定 `skipped_small_ratio` 算不算得出來（工程原則 1）：
    skipped 檔**沒有逐筆時間戳**，只能以整天為單位讀取。窗口若不是整日
    （accrued 窗的兩端是快照時刻，例如 00:10），分子涵蓋整個日曆日、分母只涵蓋
    窗口內的成交——那是一個混源比較，而它的商看起來完全像一個正常的比例。
    """
    midnight = timezone.utc
    return (start.astimezone(midnight).timetz().replace(tzinfo=None) ==
            datetime.min.time() and
            end.astimezone(midnight).timetz().replace(tzinfo=None) ==
            datetime.min.time() and end > start)


def follower_trade_quality(ref, adapter, start: datetime, end: datetime, *,
                           leader_fills, skipped_notional,
                           skipped_ratio_comparable: bool) -> dict:
    """單一 follower 的成交品質列。

    `leader_fills`：該 follower 所跟 leader 在**同一窗口**的成交；`None` ＝ 不知道
    這個 follower 跟誰（manifest 未記 leader_address），此時 TE 與滑價無從計算
    ——回 `te_available=False` 而不是回 0（0 會被讀成「零延遲」）。
    `skipped_notional`：該窗口的 skipped 名目；`None` ＝ 讀不到（同上，不填 0）。

    ⭐ `skipped_ratio_comparable`：skipped 檔以**整個日曆日**為單位（檔內無逐筆
    時間戳），成交名目卻依窗口過濾。兩者只有在窗口恰好是整個 UTC 日時才同基準
    （`_window_is_whole_utc_days`）。不同基準時比例一律回 `None` ＋ note，
    **不硬算**——那個商看起來完全像一個正常的比例，沒有人會發現它是錯的。
    """
    errors: list[str] = []
    try:
        my_fills = adapter.get_user_fills(ref.user_address, start, end)
    except Exception as e:  # noqa: BLE001 — 跨 follower 隔離（工程原則 4）
        # 取不到自己的成交 ⇒ 這一列的每一個量都無從計算。回「未知」而不是零列。
        return {"account_id": ref.account_id, "label": ref.label,
                "network": ref.network, "te_available": False,
                "quality_available": False,
                "skipped_available": False,
                "error": f"fills 查詢失敗: {e}"}

    te_available = leader_fills is not None
    q = compute_trade_quality(leader_fills or [], my_fills,
                              [("", skipped_notional)]
                              if skipped_notional is not None else [])
    row = {
        "account_id": ref.account_id,
        "label": ref.label,
        "network": ref.network,
        "quality_available": True,
        "fills": len(my_fills),
        # taker 佔比只由我方成交算出，與 leader 無關 ⇒ 即使 te_available=False 也有效
        "taker_share": q.taker_share,
        # ⭐ TE 與滑價需要 leader 的成交才配得起來；不知道 leader 是誰時一律 null
        "te_available": te_available,
        "pair_count": q.pair_count if te_available else None,
        "median_delay_s": q.median_delay_s if te_available else None,
        "taker_slippage_bp_median": q.taker_slippage_bp_median if te_available else None,
        "skipped_available": skipped_notional is not None,
        "skipped_small_notional": (q.skipped_small_notional
                                   if skipped_notional is not None else None),
        "skipped_small_ratio": (q.skipped_small_ratio
                                if skipped_notional is not None
                                and skipped_ratio_comparable else None),
        "error": "; ".join(errors) if errors else None,
    }
    if not te_available:
        row["te_note"] = ("manifest 未記錄該 follower 的 leader_address，"
                          "無法配對成交 → 配對延遲與滑價無從計算")
    if skipped_notional is not None and not skipped_ratio_comparable:
        row["skipped_note"] = (
            "skipped 小額以日曆日落檔（檔內無逐筆時間戳），本窗口非整個 UTC 日 → "
            "比例的分子與分母不同基準，故不計算；名目為窗口涵蓋日的合計")
    return row


def trade_quality_rows(followers, adapter, start: datetime, end: datetime, *,
                       leader_fills_for, skipped_for) -> list[dict]:
    """⭐ **跨客戶**成交品質列（登記在 tests/test_api_ops.py 的 CROSS_CUSTOMER_SOURCES）。

    授權不在這一層（同本模組其餘函式）：呼叫端必須掛 `_require_admin`。

    ⭐ leader 成交**每個相異 leader 只查一次**（`leader_fills_for` 由呼叫端做快取）：
    多個 follower 跟同一個 leader 是常態，逐 follower 各查一次會把一次面板載入
    變成 N 次外呼。

    ⭐ 跨 follower 隔離（工程原則 4）：任一 follower 的查詢失敗只影響該列，
    其 `error` 原樣上呈（不 log 完就吞），其餘客戶照常有資料。
    """
    comparable = _window_is_whole_utc_days(start, end)
    rows = []
    for ref in followers:
        try:
            leader_fills = leader_fills_for(ref)
        except Exception as e:  # noqa: BLE001 — leader 查詢失敗只讓該列的 TE 未知
            leader_fills = None
            logger.warning("leader 成交查詢失敗 account=%s: %r", ref.account_id, e)
        row = follower_trade_quality(ref, adapter, start, end,
                                     leader_fills=leader_fills,
                                     skipped_notional=skipped_for(ref),
                                     skipped_ratio_comparable=comparable)
        rows.append(row)
    return rows


def trade_quality_summary(rows) -> dict:
    """跨 follower 的彙總（**只彙總算得出來的那些列**，並回報樣本數）。

    ⭐ 分母是「有資料的列數」而不是「follower 總數」，且 `*_sample` 一併上呈：
    只回一個平均值會讓「10 個客戶裡只有 1 個有資料」與「10 個都有資料」看起來
    一模一樣。中位數的中位數不是中位數——本函式刻意只回**最差值**與樣本數，
    不回「中位數的平均」那種沒有統計意義卻很像數字的東西。
    """
    delays = [r["median_delay_s"] for r in rows
              if r.get("median_delay_s") is not None]
    slips = [r["taker_slippage_bp_median"] for r in rows
             if r.get("taker_slippage_bp_median") is not None]
    return {
        "followers": len(rows),
        "quality_available_count": sum(1 for r in rows if r.get("quality_available")),
        "te_available_count": sum(1 for r in rows if r.get("te_available")),
        "skipped_available_count": sum(1 for r in rows if r.get("skipped_available")),
        # 最差值（不是平均）：品質面板要回答的是「最慢的那個客戶多慢」
        "worst_median_delay_s": max(delays) if delays else None,
        "delay_sample": len(delays),
        "worst_taker_slippage_bp_median": max(slips) if slips else None,
        "slippage_sample": len(slips),
    }


# ---------- 系統健康（跨 follower 聚合） ----------

# 引擎每個 cycle 寫一筆 equity 樣本；超過這麼久沒有新樣本 ⇒ 判為 stale。
# ⭐ 取遠大於 `CopySettings.interval_s` 預設（60s）的值：門檻若貼近 cycle 間隔，
# 一次正常的 GC 停頓或一輪慢的 API 就會讓面板閃紅——而會誤報的面板等於沒有面板
# （操作者會學會忽略它）。10 分鐘＝連續 10 個 cycle 沒動，那不是抖動。
ENGINE_STALE_S = 600

# ⭐ 心跳的過期門檻由 `filet.engine_health` 定義（引擎是寫端，門檻該住在寫端旁邊），
# 這裡只引用不重寫。兩個常數目前相等且由 tests/test_engine_health.py 釘住——理由見
# HEARTBEAT_STALE_S 的 docstring：兩者回答同一個問題，各自漂移會讓面板上「引擎存活」
# 與「心跳新鮮」在同一時刻給出相反的答案。
_HEARTBEAT_STALE_S = HEARTBEAT_STALE_S


def state_root_status(root) -> str:
    """狀態根的可讀性：`absent` | `readable` | `unreadable`。

    ⭐⭐ 為什麼這個探測必須存在（否則面板會謊報健康）：引擎的狀態根是
    `filet-engine:filet-engine 0700`，而健康面板跑在 **filet-api** 進程裡——
    跨了一道權限邊界。`Path.exists()` 在**讀不到的目錄**底下一律回 `False`
    （它把 PermissionError 吞成 False），於是 `is_tripped()` 會對一個**確實已經
    觸發**的 kill switch 回答「沒有觸發」。那是這個面板最不能犯的錯：
    它會在客戶的引擎已經熔斷、部位已經被平掉的當下，告訴管理員一切正常。

    `absent`（狀態根根本不存在）與 `unreadable` 仍然分開回報，因為兩者的**處置**
    不同（前者多半是「剛 activate、引擎還沒跑過」或路徑設錯，後者是權限問題），
    這個區別以 `basis` 欄位原樣上呈給操作者。

    ⚠️ 但「分得開」**不等於**「absent 可以直讀」（2026-07-19 修正）：本函式的舊
    docstring 曾主張 absent 時「沒有 ARM 檔是真的沒觸發」，所以 `follower_health`
    讓 absent 走直讀。那個論證**只在路徑正確的前提下成立**，而面板恰恰分不出
    「引擎沒跑過」與「我看錯地方」——兩者在這個探測底下長得一模一樣。
    API 的狀態根是 `Path(cfg.state_base)/account_id`、引擎的是 systemd 注入的
    `FILET_STATE_DIR`，**兩份獨立推導**；一旦漂移，absent 會在引擎已經熔斷、部位
    已被平掉的當下被讀成「沒有 ARM 檔＝未觸發」。故 `follower_health` 現在對
    absent 與 unreadable 一視同仁，把 kill switch 與告警數交給心跳補
    （心跳來自引擎自己的狀態根，那一份路徑不可能弄錯）。
    `FILET_STATE_BASE` 也因此升為必填（見 `ApiConfig.from_env`）——擋掉漂移的
    源頭，比事後在面板上補救可靠。
    """
    p = Path(root)
    try:
        if not p.is_dir():
            # 不存在（或不是目錄）。⚠️ 這裡也可能是「父目錄讀不到」——
            # 用 os.access 對父目錄再確認一次，避免把權限問題誤判成「尚未啟動」。
            parent = p.parent
            if parent.is_dir() and not os.access(parent, os.R_OK | os.X_OK):
                return "unreadable"
            return "absent"
        os.listdir(p)          # 真正的讀取探測（exists() 不會拋，listdir 會）
    except OSError:
        return "unreadable"
    return "readable"


def _apply_heartbeat(row: dict, hb) -> dict:
    """把引擎發布的健康心跳疊到健康列上（**過期的心跳只留時刻與年齡**）。

    ⭐⭐ 為什麼面板需要心跳（完整論證見 `filet.engine_health` 檔頭）：引擎的狀態根是
    `0700 filet-engine`，面板跑在 filet-api——預設每一格都是「未知」。修法**不是**把
    狀態根放寬到 0750（那是拿廣泛的讀取權換窄的資訊需求），而是讓引擎主動發布一份
    窄的摘要到交換目錄的 engine→api 子通道。

    ⭐⭐ **過期的心跳不是目前狀態**。`hb.data` 只在 `status == "ok"` 時非 None
    （`read_heartbeat` 的結構性保證），所以過期時本函式**拿不到**那些值，即使想填
    也填不進去；面板只會多出「上次心跳時刻 ＋ 年齡」兩格。一份 40 分鐘前的
    「kill switch 未觸發」在客戶的引擎已經熔斷的當下顯示成現況，正是這個面板最不能
    犯的錯（謊報健康比沒有面板更危險）。

    ⚠️ 疊加規則（優先序刻意寫死）：**狀態根直讀優先，心跳只補未知的格子**。
    兩者都是同一批底層事實（同一個 ARM 檔、同一份 equity 樣本），直讀較新鮮，
    所以有直讀就用直讀，並以 `basis` 欄位明講這一列的值出自哪一邊——兩個來源
    並存卻不標示，讀者無從判斷他看到的數字有多舊（工程原則 1 的變形）。
    `leader` 與 `capital` 例外：狀態根**沒有**這兩者的可讀投影，它們只可能來自心跳。
    `alerts` 是第三種情形——直讀**有**這個投影，但它可能是未知（狀態根 absent／
    unreadable，或告警檔本身讀不到）。規則仍是同一條：有直讀就用直讀，
    只有 `None`（未知）那一格才由心跳補。
    """
    row["heartbeat_status"] = hb.status
    row["heartbeat_at"] = hb.at
    row["heartbeat_age_s"] = hb.age_s
    row["heartbeat_stale_after_s"] = _HEARTBEAT_STALE_S
    data = hb.data
    if data is None:
        # missing／stale／unreadable：三者在面板上是三件不同的事（引擎從未寫過／
        # 引擎沒跑或寫不進去／檔案壞了），所以 status 原樣上呈，不折疊成一個「未知」。
        return row

    leader = data.get("leader") or {}
    row["leader_address"] = leader.get("address")
    row["leader_source"] = leader.get("source")
    row["capital"] = data.get("capital")
    # 風控總開關同 `capital`：狀態根沒有可讀投影，心跳是唯一來源。
    row["risk"] = data.get("risk")
    row["last_cycle"] = data.get("last_cycle")

    # ⭐ 告警數：**只補未知的格子**（直讀已經給出數字時不覆蓋——它較新鮮）。
    # 這一格非補不可：狀態根 absent／unreadable 時 `alerts` 恆為 None，而
    # 「有幾則告警」正是操作者判斷「要不要現在去看」的依據之一。心跳的計數與直讀
    # 出自**同一個** `killswitch.count_alerts`，兩側同基準（工程原則 1）。
    if row.get("alerts") is None:
        alerts = data.get("alerts") or {}
        count = alerts.get("count")
        if alerts.get("known") is True and isinstance(count, int) \
                and not isinstance(count, bool):
            row["alerts"] = count

    if row.get("basis") == "state_root":
        return row      # 直讀已經給了 kill switch 與覆蓋度，心跳不覆蓋較新鮮的值
    row["basis"] = "heartbeat"
    tripped = data.get("killswitch_tripped")
    if isinstance(tripped, bool):
        row["killswitch_tripped"] = tripped
        row["killswitch_known"] = True
    cov = data.get("coverage") or {}
    if cov.get("known") is True:
        row["coverage_known"] = True
        row["sample_count"] = cov.get("count")
        row["sample_coverage_sufficient"] = cov.get("sufficient")
    newest = cov.get("newest_age_s")
    if isinstance(newest, (int, float)) and not isinstance(newest, bool):
        # ⚠️ 樣本年齡是**心跳寫入當下**量到的，不是現在量到的。面板要顯示「引擎活著」
        # 就必須把心跳自己的年齡也加回去，否則一份兩小時前的心跳裡那句「樣本 5 秒前
        # 才寫過」會被當成現在的事實——正是「過期心跳不得當成現況」的細粒度版本。
        # 兩個加數同單位、同一次讀取（工程原則 1）。
        row["last_sample_age_s"] = float(newest) + float(hb.age_s or 0.0)
        row["engine_alive"] = row["last_sample_age_s"] <= ENGINE_STALE_S
    if row.get("error") and row["killswitch_known"]:
        row["error"] = None
    return row


def follower_health(ref, state_root, *, now_fn, coverage_fn, killswitch_fn,
                    alerts_path_fn, heartbeat_fn=None) -> dict:
    """單一 follower 的健康列。**每一格讀不到都回明確的「未知」**。

    ⭐⭐ 本函式最重要的性質是它**不會**在讀不到時填一個看起來健康的值：
    - kill switch 狀態讀不到 → `killswitch_tripped: null` ＋ `killswitch_known: false`
      （不是 `false`＝「沒觸發」）。
    - 無 equity 樣本 → `engine_alive: null`（不是 `false`，更不是 `true`）。
    - 告警檔讀不到 → `alerts: null`（不是 0）。

    理由：健康面板的讀者會用它決定「要不要現在去看」。一個謊報健康的格子會讓他
    **不去看**，而那正是他最該去看的時刻。未知看起來刺眼是對的——它就是要刺眼。

    ⚠️ `engine_alive` 的基準（誠實標註）：這**不是**真正的 process 檢查，而是
    「引擎最近有沒有寫過 equity 樣本」的代理（引擎每 cycle 寫一筆）。回應以
    `liveness_basis` 明講這件事——把代理指標當成真的存活檢查上呈，是另一種謊報。
    一個掛在 D 狀態、還在寫檔卻不下單的進程，這一格看起來會是健康的。
    """
    row: dict = {"account_id": ref.account_id, "label": ref.label,
                 "network": ref.network,
                 "liveness_basis": "equity_sample",
                 "state_root": str(state_root),
                 # 心跳專屬的格子先給明確的 None（不是缺鍵）：缺鍵在 JSON 上與
                 # 「值為 null」對前端是兩種形狀，而這一列每一欄都該恆定存在。
                 "leader_address": None, "leader_source": None,
                 "capital": None, "last_cycle": None}

    # ⭐⭐ 先探測可讀性（見 state_root_status）：狀態根讀不到時，底下每一個
    # `Path.exists()` 都會安靜地回 False——kill switch 會被回報成「沒有觸發」。
    # 讀不到就整列標未知，不讓任何一格落到「看起來健康」的預設值上。
    status = state_root_status(state_root)
    # `basis` ＝這一列的 kill switch／覆蓋度出自哪裡。只有真的讀得到才算 state_root。
    row["basis"] = "state_root" if status == "readable" else status
    if status != "readable":
        # ⭐⭐ `absent` 與 `unreadable` **一視同仁**（2026-07-19 修正的 Critical）。
        # 舊版只有 unreadable 走這一支，absent 落到底下的直讀，於是「沒有 ARM 檔」
        # 被斷言成「kill switch 未觸發」並以 killswitch_known=True 上呈。那個推論
        # 需要一個面板無法驗證的前提：**我看的是引擎真正在寫的那個目錄**。
        # API 由 `cfg.state_base`／引擎由 systemd 的 `FILET_STATE_DIR` 各自推導，
        # 兩者相等靠的是兩個字面量剛好一樣；漂移的症狀就是整批 follower 同時
        # 「absent 且一切正常」——在引擎已經熔斷的當下。
        # 未知看起來刺眼是對的：能斷言未觸發的只有「心跳新鮮且明說未觸發」。
        why = ("讀不到" if status == "unreadable" else "不存在")
        hint = ("——請確認目錄權限" if status == "unreadable" else
                "——可能是剛 activate、引擎尚未跑過，**也可能是 FILET_STATE_BASE "
                "與引擎的 FILET_STATE_DIR 不一致（面板分不出這兩者）**")
        row.update({
            "coverage_known": False, "sample_count": None,
            "sample_coverage_sufficient": None, "last_sample_age_s": None,
            "engine_alive": None,
            "killswitch_tripped": None, "killswitch_known": False,
            "alerts": None,
            "error": (f"狀態根{why}（{state_root}）{hint}——kill switch、覆蓋度、"
                      f"告警數改由引擎發布的健康心跳提供（見 RUNBOOK §5.6）；"
                      f"心跳也讀不到時整列維持未知"),
        })
        return _apply_heartbeat(row, heartbeat_fn(ref.account_id)) \
            if heartbeat_fn is not None else row

    try:
        cov = coverage_fn(state_root)
    except Exception as e:  # noqa: BLE001 — 跨 follower 隔離（工程原則 4）
        row.update({"coverage_known": False, "sample_count": None,
                    "sample_coverage_sufficient": None,
                    "last_sample_age_s": None, "engine_alive": None,
                    "error": f"樣本覆蓋度讀取失敗: {e}"})
    else:
        # read_error＝檔案在但讀不出來，與「首次啟動無檔」是兩件事（見 SampleCoverage）
        row.update({
            "coverage_known": not cov.read_error,
            "sample_count": None if cov.read_error else cov.count,
            "sample_coverage_sufficient": None if cov.read_error else cov.sufficient,
            "last_sample_age_s": cov.newest_age_s,
            # 無樣本 → None（不知道），不是 False（已死）也不是 True（活著）
            "engine_alive": (None if cov.newest_age_s is None
                             else cov.newest_age_s <= ENGINE_STALE_S),
            "error": None,
        })
        if cov.read_error:
            row["error"] = "equity 樣本檔存在但讀不出來——回撤保護是否生效無從確認"

    try:
        row["killswitch_tripped"] = bool(killswitch_fn(state_root))
        row["killswitch_known"] = True
    except Exception as e:  # noqa: BLE001 — 同上；讀不到絕不當成「沒觸發」
        row["killswitch_tripped"] = None
        row["killswitch_known"] = False
        row["error"] = "; ".join(filter(None, [row.get("error"),
                                               f"kill switch 狀態讀取失敗: {e}"]))

    row["alerts"] = count_alerts(alerts_path_fn(state_root))
    if heartbeat_fn is None:
        return row
    return _apply_heartbeat(row, heartbeat_fn(ref.account_id))


def health_summary(rows, unapplied_leader_changes: int | None,
                   leader_change_errors) -> dict:
    """跨 follower 的健康彙總。

    ⭐ 每一個計數都附一個 `*_unknown` 兄弟欄：「0 個引擎沒回應」與「10 個引擎的
    狀態都讀不到」在單一計數上長得一模一樣，而後者才是該立刻處理的那個。
    """
    return {
        "followers": len(rows),
        "engine_alive_count": sum(1 for r in rows if r.get("engine_alive") is True),
        "engine_stale_count": sum(1 for r in rows if r.get("engine_alive") is False),
        "engine_unknown_count": sum(1 for r in rows if r.get("engine_alive") is None),
        "killswitch_tripped_count": sum(1 for r in rows
                                        if r.get("killswitch_tripped") is True),
        "killswitch_unknown_count": sum(1 for r in rows
                                        if not r.get("killswitch_known")),
        "coverage_insufficient_count": sum(
            1 for r in rows if r.get("sample_coverage_sufficient") is False),
        "coverage_unknown_count": sum(
            1 for r in rows if r.get("sample_coverage_sufficient") is None),
        "alerts_total": sum(r["alerts"] for r in rows if r.get("alerts") is not None),
        "alerts_unknown_count": sum(1 for r in rows if r.get("alerts") is None),
        # ⭐ 心跳三態各自計數，**不合併成一個「心跳有問題」**：`stale` 是「引擎沒跑
        # 或寫不進交換目錄」（要立刻查），`missing` 多半是「剛 activate／子目錄沒建」
        # （部署待辦）。合成一個數字會讓前者被後者的常態雜訊蓋掉。
        "heartbeat_ok_count": sum(1 for r in rows
                                  if r.get("heartbeat_status") == "ok"),
        "heartbeat_stale_count": sum(1 for r in rows
                                     if r.get("heartbeat_status") == "stale"),
        "heartbeat_missing_count": sum(1 for r in rows
                                       if r.get("heartbeat_status") == "missing"),
        # ⭐ 「已寫入但未套用」的積壓：None ＝ 這條鏈路查不下去（見 leader_change_errors），
        # **不是** 0。0 會被讀成「沒有積壓」，而實際上是「無從得知有沒有積壓」。
        "unapplied_leader_changes": unapplied_leader_changes,
        "leader_change_errors": list(leader_change_errors),
    }


_ACTIVE = "active"


def _field(row, name):
    """支援 BillingRecord（dataclass）與 dict 兩種來源——純函式不綁定 store 型別。"""
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def subscription_drift(local_rows, stripe_subs) -> dict:
    """比對本地 billing 表與 Stripe 訂閱，找出漂移（**唯讀偵測，不做任何修正**）。

    存在理由：webhook 是本地 billing 表的唯一寫入者，掉一包就永久漂移，且兩個方向的
    危害不同——
    - `local_active_stripe_not`：本地 active 但 Stripe 非 active（含 Stripe 上根本
      查無此訂閱）→ **漏財**：還在提供服務卻收不到錢。
    - `stripe_active_local_not`：Stripe active 但本地非 active／無記錄 → **客戶付了錢
      沒拿到權益**：信任問題，比漏財更嚴重。
    - `status_mismatch`：兩邊都有、狀態不同，但不屬上述兩類（例如 past_due vs canceled）。
    - `orphan_stripe`：對不到任何本地 account 的 Stripe 訂閱，且**非 active**
      （多為外部手建或測試殘留）。

    ⚠️ 分類優先序（刻意的：危害優先於分類整齊）：Stripe active 卻對不到本地 account，
    同時符合 `stripe_active_local_not`（無記錄）與 `orphan_stripe`（對不到）的字面
    定義。一律歸入 `stripe_active_local_not`——那是「有人付了錢」的清單，漏掉一筆的
    代價遠高於 orphan 清單多一筆。故 `orphan_stripe` 只收非 active 的孤兒，四類互斥
    不重複計數。

    ⚠️ 同基準比較（工程原則 1）：Stripe 的**原始** status 與本地 status 不同一套值域
    （Stripe 有 trialing/unpaid/incomplete…，本地只有 none/active/past_due/canceled）。
    比較前一律先過 `map_stripe_status` 正規化到本地值域，兩側同基準——否則 `trialing`
    會被誤判成 mismatch，而它在本地映射就是 `active`（webhook 寫入時走的是同一個
    映射函式，這裡刻意重用同一個，不另寫一份對照表）。

    ⚠️ 呼叫端注意：`stripe_subs` 若是被截斷的清單（見 `StripeGateway
    .list_subscriptions` 的 `truncated`），「本地有、Stripe 沒有」的判定會產生假漂移
    ——本函式無從得知截斷與否，截斷旗標由呼叫端一併上呈。

    對應鍵：優先 `stripe_subscription_id`，其次 metadata 的 account_id
    （沿 `apply_webhook_event` 的雙保險慣例）。`matched_by` 欄位記錄實際命中的是哪一個。

    ⚠️ **兩輪比對的順序是正確性要求，不是效能考量**（opus 對抗審查 Important）：
    第一輪跑完**全部**的精確比對（`stripe_subscription_id`），第二輪才做 metadata
    fallback，且只對第一輪未命中的 account 生效。顛倒順序（逐筆各自 fallback）會誤判
    回鍋客戶：本地表每個 account 只存**一個** subscription id，而 `status="all"` 會
    永久回傳歷史上已取消的訂閱、其 metadata 同樣帶 account_id——同一個 account 於是
    被算兩次（新訂閱精確命中 in_sync、舊訂閱走 fallback 落入 `local_active_stripe_not`），
    docstring 宣稱的「四類互斥不重複計數」在此破功。危害是實質的：
    `local_active_stripe_not` 的語意是「還在服務卻收不到錢」，admin 照著它去停用的
    會是一位**正常付費**的客戶。已被精確命中的 account，其歷史訂閱直接跳過
    （計入 `superseded_count`，不進任何漂移清單）。

    同一 account 有多張 Stripe 訂閱但本地沒存 id（webhook 掉了首包）時，第二輪只認
    **一張**：優先取 active 者（那張才代表當前權益），其餘同樣計入 `superseded_count`。
    """
    local_rows = list(local_rows)
    by_sub: dict[str, object] = {}
    by_acct: dict[str, object] = {}
    for row in local_rows:
        acct = _field(row, "account_id")
        sub_id = _field(row, "stripe_subscription_id")
        if acct:
            by_acct[acct] = row
        if sub_id:
            by_sub[sub_id] = row

    local_active_stripe_not: list[dict] = []
    stripe_active_local_not: list[dict] = []
    status_mismatch: list[dict] = []
    orphan_stripe: list[dict] = []
    in_sync = 0
    matched_accounts: set[str] = set()
    stripe_count = 0

    # ---- 第一輪：精確比對（stripe_subscription_id）。⭐ 必須先跑**完**，
    # 因為第二輪要知道哪些 account 已經被精確命中（見 docstring 的回鍋客戶說明）。
    pairs: list[tuple[object, object, str | None]] = []   # (sub, row|None, matched_by)
    deferred: list[object] = []                           # 第一輪沒命中，留給第二輪
    for sub in stripe_subs:
        stripe_count += 1
        row = by_sub.get(_field(sub, "id")) if _field(sub, "id") else None
        if row is None:
            deferred.append(sub)
            continue
        matched_accounts.add(_field(row, "account_id"))
        pairs.append((sub, row, "subscription_id"))

    # ---- 第二輪：metadata fallback，只對第一輪未命中的 account 生效
    superseded_count = 0
    claimed_at: dict[str, int] = {}   # 本輪已認領的 account → 其在 pairs 中的索引
    for sub in deferred:
        md = _field(sub, "metadata") or {}
        md_acct = md.get("account_id") if isinstance(md, dict) else None
        row = by_acct.get(md_acct) if md_acct else None
        if row is None:
            pairs.append((sub, None, None))     # 對不到本地 account，照原邏輯分類
            continue
        acct = _field(row, "account_id")
        if acct in matched_accounts:
            # ⭐ 該 account 已被精確命中：這是回鍋客戶的歷史訂閱，跳過（不進任何清單）
            superseded_count += 1
            continue
        idx = claimed_at.get(acct)
        if idx is None:
            claimed_at[acct] = len(pairs)
            pairs.append((sub, row, "metadata"))
            continue
        # 同一 account 的第二張：只有 active 取代非 active（active 才代表當前權益）
        superseded_count += 1
        held = pairs[idx][0]
        if (map_stripe_status(_field(sub, "status") or "") == _ACTIVE
                and map_stripe_status(_field(held, "status") or "") != _ACTIVE):
            pairs[idx] = (sub, row, "metadata")
    matched_accounts |= set(claimed_at)

    # ---- 分類（兩輪的配對結果共用同一套判準）
    for sub, row, matched_by in pairs:
        sub_id = _field(sub, "id")
        raw_status = _field(sub, "status") or ""
        stripe_status = map_stripe_status(raw_status)  # ⭐ 正規化後才比（同基準）
        md = _field(sub, "metadata") or {}
        md_acct = md.get("account_id") if isinstance(md, dict) else None

        if row is None:
            entry = {"account_id": md_acct, "local_status": None,
                     "stripe_status": stripe_status, "stripe_status_raw": raw_status,
                     "stripe_subscription_id": sub_id, "matched_by": None}
            # 危害優先：付了錢卻對不到帳號 → 進「客戶沒拿到權益」清單，不進 orphan
            (stripe_active_local_not if stripe_status == _ACTIVE
             else orphan_stripe).append(entry)
            continue

        acct = _field(row, "account_id")   # matched_accounts 已在兩輪比對時登記
        local_status = _field(row, "status") or "none"
        entry = {"account_id": acct, "local_status": local_status,
                 "stripe_status": stripe_status, "stripe_status_raw": raw_status,
                 "stripe_subscription_id": sub_id, "matched_by": matched_by}
        if local_status == _ACTIVE and stripe_status != _ACTIVE:
            local_active_stripe_not.append(entry)
        elif stripe_status == _ACTIVE and local_status != _ACTIVE:
            stripe_active_local_not.append(entry)
        elif stripe_status != local_status:
            status_mismatch.append(entry)
        else:
            in_sync += 1

    # 本地有、Stripe 完全查無此訂閱（status="all" 已涵蓋已取消者，故「查無」＝真的沒有）
    for row in local_rows:
        acct = _field(row, "account_id")
        if acct in matched_accounts:
            continue
        local_status = _field(row, "status") or "none"
        entry = {"account_id": acct, "local_status": local_status,
                 "stripe_status": None, "stripe_status_raw": None,
                 "stripe_subscription_id": _field(row, "stripe_subscription_id"),
                 "matched_by": None}
        if local_status == _ACTIVE:
            local_active_stripe_not.append(entry)   # 漏財：給了權益，Stripe 上沒這筆
        elif local_status in ("none", "canceled"):
            in_sync += 1                            # 兩邊都無權益＝一致
        else:
            status_mismatch.append(entry)           # past_due 卻查無訂閱：要查

    drift_count = (len(local_active_stripe_not) + len(stripe_active_local_not)
                   + len(status_mismatch) + len(orphan_stripe))
    return {
        "local_active_stripe_not": local_active_stripe_not,
        "stripe_active_local_not": stripe_active_local_not,
        "status_mismatch": status_mismatch,
        "orphan_stripe": orphan_stripe,
        "in_sync_count": in_sync,
        "drift_count": drift_count,
        "local_count": len(local_rows),
        "stripe_count": stripe_count,
        # 被更新訂閱取代的歷史訂閱筆數（回鍋客戶）。**不是漂移**，不計入 drift_count；
        # 上呈是為了讓 stripe_count 對得起來——靜默丟棄會讓人以為對帳漏看了幾筆。
        "superseded_count": superseded_count,
    }


class AccruedPoint(NamedTuple):
    """accrued 歷史序列的一個點。

    ⭐ `captured_at`（快照時刻）是**對帳窗口的唯一合法基準**（工程原則 1）：
    accrued 是「查詢當下」的鏈上累積量，故 `accrued[D] - accrued[D-1]` 涵蓋的是
    `(captured_at[D-1], captured_at[D]]`，**不是**日曆日 D。舊格式的行沒有這個欄位
    → None，呼叫端**必須拒絕硬算**（用日期猜窗口＝混源比較，見 app.ops_revenue
    的 basis_unknown 分支）。
    """
    date: str
    accrued: Decimal
    captured_at: datetime | None


def _parse_captured_at(raw) -> datetime | None:
    """ISO8601 → tz-aware UTC datetime。缺值／壞值一律 None（**不猜**）：
    下游看到 None 會拒絕對帳，那比拿猜來的時刻算出假 discrepancy 安全。
    無時區的字串視為 UTC——唯一寫入者（scripts/copytrade_daily_report.py）一律寫 UTC。"""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def load_accrued_series(path: str | Path) -> list[AccruedPoint]:
    """讀 accrued 歷史序列（jsonl，每行
    `{"date": ..., "captured_at": ..., "accrued": ...}`），依日期升冪回
    [AccruedPoint, ...]；無檔 → 空 list。

    新舊格式並存：`captured_at` 是後加的欄位（opus 對抗審查 Critical 的修法），
    舊行沒有 → 該點的 `captured_at` 為 None。**不回填猜測值**。

    容錯（營運檢視不該被一行壞資料整份打掉）：壞行跳過。但**不**把壞行當 0——
    accrued 是累積量，把缺值當 0 會造出巨大的假 delta。
    """
    p = Path(path)
    if not p.exists():
        return []
    out: list[AccruedPoint] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            out.append(AccruedPoint(str(rec["date"]),
                                    Decimal(str(rec["accrued"])),
                                    _parse_captured_at(rec.get("captured_at"))))
        except (ValueError, KeyError, TypeError, InvalidOperation):
            continue
    out.sort(key=lambda p: p.date)
    return out


def accrued_window(series) -> tuple[datetime, datetime] | None:
    """從 accrued 歷史序列推導對帳窗口 `(start, end]`。

    ⭐ **這是收入對帳與客戶損益共用的唯一窗口來源**（工程原則 1：被比較的兩個值
    必須同源同基準）。兩個端點都必須呼叫本函式，不得各自推導出「看起來一樣」的窗口——
    這正是先前那個 Critical（健康帳戶被誤判 199 倍差異）的成因。抽成函式而非各自
    複製一份日期算式，是刻意的結構性修法：兩份「長得一樣」的推導會各自漂移，
    而漂移後兩張表的 builder fee 仍然並排顯示、仍然看起來可以相減。

    窗口界一律取相鄰兩筆快照的 `captured_at`——accrued 是查詢當下的鏈上累積量，
    不是日曆日的量，故 `accrued[-1] - accrued[-2]` 涵蓋的是
    `(captured_at[-2], captured_at[-1]]`。

    回 None 代表無法對齊（資料不足或缺 `captured_at`），呼叫端必須據此標示 basis_unknown，
    不得自行退化成日曆日或 now 往回 N 天。
    """
    points = list(series)
    if len(points) < 2:
        return None
    prev_pt, now_pt = points[-2], points[-1]
    start, end = prev_pt.captured_at, now_pt.captured_at
    if start is None or end is None or end <= start:
        return None
    return start, end


def accrued_window_note(series) -> str:
    """`accrued_window` 回 None 時，說明「為什麼對不齊」的人話（供 basis_unknown 的
    `note` 用）。刻意與窗口推導分家：這裡只解釋，不產生任何時間界——
    否則就成了第二個窗口來源。"""
    points = list(series)
    if len(points) < 2:
        return "accrued 歷史不足兩點，無法推導對帳窗口"
    prev_pt, now_pt = points[-2], points[-1]
    if prev_pt.captured_at is None or now_pt.captured_at is None:
        return "歷史資料缺快照時刻（captured_at），無法對齊窗口"
    return "相鄰兩筆快照時刻非嚴格遞增（回填或時鐘倒退），無法對齊窗口"


def _as_decimal(v) -> Decimal:
    if v is None:
        return Decimal("0")
    return v if isinstance(v, Decimal) else Decimal(str(v))


def jsonable(value):
    """Decimal → str 的遞迴序列化（無損）。FastAPI 預設把 Decimal 轉 float，
    金額欄位走 float 會有精度損失——營運/對帳數字一律以字串上線。"""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value
