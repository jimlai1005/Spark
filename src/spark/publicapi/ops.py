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
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from spark.filet.aggregate import collect_follower_summary


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

    除零防護：`attributed` 為 0 時 `discrepancy_pct` 回 None（不得除零），
    此時 `over_threshold` 為 False——但 `discrepancy` 仍照實回報，
    「應收 0 而實收非 0」的異常不會因為算不出百分比而消失。
    """
    attributed = sum((_as_decimal(r.get("builder_fee")) for r in rows), Decimal("0"))
    # ⭐ 只從參數取；此處刻意不出現任何 rows 的聚合（紅線：北極星不由 rows 推導）
    accrued_delta = Decimal(str(accrued_now)) - Decimal(str(accrued_prev))
    discrepancy = accrued_delta - attributed
    pct = (abs(discrepancy) / attributed) if attributed != 0 else None
    threshold = Decimal(str(threshold_pct))
    return {
        "attributed": attributed,
        "accrued_delta": accrued_delta,
        "accrued_now": Decimal(str(accrued_now)),
        "accrued_prev": Decimal(str(accrued_prev)),
        "discrepancy": discrepancy,
        "discrepancy_pct": pct,
        "over_threshold": pct is not None and pct > threshold,
        "threshold_pct": threshold,
        "rows": len(rows),
    }


def load_accrued_series(path: str | Path) -> list[tuple[str, Decimal]]:
    """讀 accrued 歷史序列（jsonl，每行 {"date": ..., "accrued": ...}），
    依日期升冪回 [(day_iso, accrued), ...]；無檔 → 空 list。

    容錯（營運檢視不該被一行壞資料整份打掉）：壞行跳過。但**不**把壞行當 0——
    accrued 是累積量，把缺值當 0 會造出巨大的假 delta。
    """
    p = Path(path)
    if not p.exists():
        return []
    out: list[tuple[str, Decimal]] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            out.append((str(rec["date"]), Decimal(str(rec["accrued"]))))
        except (ValueError, KeyError, TypeError, InvalidOperation):
            continue
    out.sort(key=lambda t: t[0])
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
