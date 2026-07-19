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
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import NamedTuple

from spark.filet.aggregate import collect_follower_summary
from spark.publicapi.billing import map_stripe_status


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

    for sub in stripe_subs:
        stripe_count += 1
        sub_id = _field(sub, "id")
        raw_status = _field(sub, "status") or ""
        stripe_status = map_stripe_status(raw_status)  # ⭐ 正規化後才比（同基準）
        md = _field(sub, "metadata") or {}
        md_acct = md.get("account_id") if isinstance(md, dict) else None

        row = by_sub.get(sub_id) if sub_id else None
        matched_by = "subscription_id" if row is not None else None
        if row is None and md_acct:
            row = by_acct.get(md_acct)
            matched_by = "metadata" if row is not None else None

        if row is None:
            entry = {"account_id": md_acct, "local_status": None,
                     "stripe_status": stripe_status, "stripe_status_raw": raw_status,
                     "stripe_subscription_id": sub_id, "matched_by": None}
            # 危害優先：付了錢卻對不到帳號 → 進「客戶沒拿到權益」清單，不進 orphan
            (stripe_active_local_not if stripe_status == _ACTIVE
             else orphan_stripe).append(entry)
            continue

        acct = _field(row, "account_id")
        matched_accounts.add(acct)
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
