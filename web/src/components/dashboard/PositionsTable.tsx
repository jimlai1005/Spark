"use client";
import { useEffect, useState, type CSSProperties } from "react";
import {
  getMyAuthorizations,
  getMyFees,
  getMyFills,
  type DashboardFeesMonth,
  type DashboardPosition,
  type MyAuthorizationRow,
  type MyFeesDailyRow,
  type MyFeesPeriod,
  type MyFeesResp,
  type MyFillRow,
} from "@/lib/api";
import { fmtAmount, fmtUpdatedAtUtc, NO_VALUE } from "@/lib/format";
import { useCopy } from "@/lib/lang";

const HL_EXPLORER_TX_BASE = "https://app.hyperliquid.xyz/explorer/tx/";

type Tab = "positions" | "fees" | "history";

/** upnl 一律兩位小數（Task 19 修正）——`fmtAmount` 對 |v|<1 預設放到 4 位（給
 * 對帳欄位用），持倉表不是對帳欄位，-$0.1600 這種尾數只是雜訊，固定 `dp=2`。 */
function signedAmount(v: string): string {
  const n = Number(v);
  if (!Number.isFinite(n)) return NO_VALUE;
  const sign = n >= 0 ? "+" : "-";
  return `${sign}$${fmtAmount(String(Math.abs(n)), 2)}`;
}

/**
 * 次要區塊：跟單持倉表＋tab 列（Task 14）。「費用明細」自 M3 round3 Task 5（R2·B
 * 重構）起改用獨立客戶端點 `GET /api/me/fees?period=`（見 `FeesGrid`），支援
 * 本月/上月/全部三種期間切換＋前端補日曆列＋CSV 匯出，不再吃 dashboard 快照裡
 * 的 `fees_month.daily_bars`。「成交記錄・授權歷程」自 M3 round2 Task 7 起已實作
 * （見下方 `HistoryPanel`），非 backlog。
 */
export function PositionsTable({
  positions,
  // ⭐ M3 round3 Task 5：費用明細 tab 改為自帶期間切換的獨立 lazy fetch
  // （`FeesGrid` 內部直接呼叫 `getMyFees`），不再吃 `fees_month` 快照。`feesMonth`
  // prop 留著只是為了不動既有呼叫端／既有測試的簽名（`PositionsTable.test.tsx`／
  // `PositionsTable.history.test.tsx` 仍傳 `feesMonth={null}`），本元件內不使用它。
  feesMonth: _feesMonth,
}: {
  positions: DashboardPosition[] | null;
  feesMonth: DashboardFeesMonth | null;
}) {
  const COPY = useCopy();
  const c = COPY.dashboard;
  const [tab, setTab] = useState<Tab>("positions");

  return (
    <div className="dash-positions-block">
      <div className="dash-tabs">
        <button
          type="button"
          className="dash-tab"
          data-active={tab === "positions"}
          onClick={() => setTab("positions")}
        >
          {c.tabs.positions}{positions ? ` (${positions.length})` : ""}
        </button>
        <button
          type="button"
          className="dash-tab"
          data-active={tab === "fees"}
          onClick={() => setTab("fees")}
        >
          {c.tabs.fees}
        </button>
        <button
          type="button"
          className="dash-tab"
          data-active={tab === "history"}
          onClick={() => setTab("history")}
        >
          {c.tabs.history}
        </button>
      </div>

      {tab === "positions" && <PositionsGrid positions={positions} />}
      {tab === "fees" && <FeesGrid />}
      {tab === "history" && <HistoryPanel />}
    </div>
  );
}

function PositionsGrid({ positions }: { positions: DashboardPosition[] | null }) {
  const COPY = useCopy();
  const c = COPY.dashboard.positionsTable;

  if (!positions || positions.length === 0) {
    return (
      <div className="dash-table">
        <p className="dash-table-empty">{c.empty}</p>
      </div>
    );
  }

  return (
    <div className="dash-table">
      <div className="dash-table-head">
        <div>{c.symbol}</div>
        <div>{c.value}</div>
        <div>{c.upnl}</div>
        <div>{c.entry}</div>
        <div>{c.mark}</div>
        <div>{c.deviation}</div>
      </div>
      {positions.map((p) => (
        <div className="dash-table-row" key={`${p.symbol}-${p.side}`}>
          <div className="dash-table-symbol">
            <span className="dash-side-chip" data-side={p.side}>
              {p.side === "long" ? c.long : c.short}
            </span>
            <div>
              <div>{p.symbol}</div>
              <div className="mono" style={{ fontSize: 11, color: "var(--text-dim)" }}>
                {p.margin_mode === "cross" ? c.marginModeCross : c.marginModeIsolated}{" "}
                {p.leverage}x
              </div>
            </div>
          </div>
          <div>${fmtAmount(p.value)}</div>
          <div style={{ color: Number(p.upnl) >= 0 ? "var(--pos)" : "var(--neg)" }}>
            {signedAmount(p.upnl)}
          </div>
          <div>{fmtAmount(p.entry)}</div>
          <div>{fmtAmount(p.mark)}</div>
          <div>{p.deviation_pct != null ? `${p.deviation_pct}%` : NO_VALUE}</div>
        </div>
      ))}
    </div>
  );
}

// ── 費用明細 tab（R2·B 重構，M3 round3 Task 5；M3 round4 Task R4-9 移除補日曆）──
//
// `/api/me/fees?period=` 只回「有成交的日子」（`daily`，見 api.ts `MyFeesDailyRow`
// 註解，後端 `_fee_daily_bars` 從一開始就只吐 `fill_count > 0` 的日子）；
// `builder_fee === "0"`（有成交、費用恰為零）與「當日無成交」在後端回應裡
// 本來就明確分開（前者有列、後者無列）。
//
// ⭐ R4-9（2026-08-31 使用者裁決）：R2·B 當時額外加了 `buildFeesCalendarRows`
// 把後端已過濾掉的空日**重新補**成整月連續日曆（顯示一整排「—」），本意是
// 「讓月份看起來完整」，但使用者截圖回饋這排「—」反而是雜訊——真正的空日
// 源頭不是後端（後端從未回傳過），是這段前端補日曆邏輯，故整段移除，改為
// 直接渲染後端回應（新→舊排序），CSV 匯出同規則。
//
// ⚠️ 這裡刻意不掛 globals.css 的既有 `.dash-table-head`/`.dash-table-row`
// class（那組 grid-template-columns 是為 6 欄的持倉表寫的，本表是 5 欄），
// 改用 inline grid style——globals.css 不在本 task 的改動範圍，避免動到共用
// 樣式表影響其他表格。

const FEES_PERIODS: MyFeesPeriod[] = ["this_month", "last_month", "all"];
const FEES_DEFAULT_VISIBLE = 10;
const FEES_LOAD_MORE_STEP = 20;
const FEES_ROW_GRID_COLUMNS = "140px 1fr 1fr 1fr 110px";

type FeesTableCopy = ReturnType<typeof useCopy>["dashboard"]["feesTable"];

/** 後端 `daily`（升冪，`_fee_daily_bars` 依日期由舊到新 append）→ 新→舊排序，
 * 純格式化、不補日期空隙（見上方檔頭 R4-9）。 */
export function sortFeesRowsDesc(daily: MyFeesDailyRow[]): MyFeesDailyRow[] {
  return [...daily].sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0));
}

/** RFC4180 最小轉義：欄位含逗號／引號／換行才加引號，內部引號雙寫。 */
function csvEscapeField(field: string): string {
  if (/[",\n]/.test(field)) return `"${field.replace(/"/g, '""')}"`;
  return field;
}

/**
 * CSV 內容（不含 BOM／下載副作用，純函式方便單測）。欄位與畫面表格一致，
 * 一律「有成交」（後端已過濾空日，見上方檔頭）。`c` 只取用 5 個 `col*`
 * 欄位——用 `Pick` 而非整個 `FeesTableCopy`，讓呼叫端（含測試）不需要餵一份
 * 完整的費用明細文案物件就能單測本函式。
 */
export function buildFeesCsv(
  rows: MyFeesDailyRow[],
  c: Pick<FeesTableCopy, "colDate" | "colFillCount" | "colRoutedVolume" | "colBuilderFee" | "colEffectiveRate">,
): string {
  const header = [c.colDate, c.colFillCount, c.colRoutedVolume, c.colBuilderFee, c.colEffectiveRate];
  const lines = [
    header,
    ...rows.map((r) => [
      r.date,
      String(r.fill_count),
      `$${fmtAmount(r.routed_volume)}`,
      `$${fmtAmount(r.builder_fee)}`,
      r.effective_rate_bps != null ? `${fmtAmount(r.effective_rate_bps, 2)} bps` : NO_VALUE,
    ]),
  ];
  return lines.map((line) => line.map(csvEscapeField).join(",")).join("\n");
}

function downloadCsv(filename: string, csv: string): void {
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function FeesGrid() {
  const COPY = useCopy();
  const c = COPY.dashboard.feesTable;
  const [period, setPeriod] = useState<MyFeesPeriod>("this_month");
  const [loading, setLoading] = useState(true);
  const [data, setData] = useState<MyFeesResp | null>(null);
  const [failed, setFailed] = useState(false);
  const [visibleCount, setVisibleCount] = useState(FEES_DEFAULT_VISIBLE);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setFailed(false);
    setVisibleCount(FEES_DEFAULT_VISIBLE);
    getMyFees(period).then(
      (resp) => {
        if (cancelled) return;
        setData(resp);
        setLoading(false);
      },
      () => {
        if (cancelled) return;
        setFailed(true);
        setLoading(false);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [period]);

  const periodLabel: Record<MyFeesPeriod, string> = {
    this_month: c.periodThisMonth, last_month: c.periodLastMonth, all: c.periodAll,
  };

  const rows = data ? sortFeesRowsDesc(data.daily) : [];
  const visibleRows = rows.slice(0, visibleCount);
  const hasMore = rows.length > visibleCount;

  return (
    <div className="dash-table">
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        flexWrap: "wrap", gap: 12, padding: "16px 20px 0",
      }}
      >
        <div style={{ display: "flex", gap: 4 }}>
          {FEES_PERIODS.map((p) => (
            <button
              key={p}
              type="button"
              onClick={() => setPeriod(p)}
              disabled={period === p}
              style={{
                fontSize: 13, padding: "6px 13px", borderRadius: 6,
                border: "1px solid var(--border)", cursor: period === p ? "default" : "pointer",
                background: period === p ? "var(--inset)" : "transparent",
                color: period === p ? "var(--text)" : "var(--text-dim)",
                fontWeight: period === p ? 600 : 400,
              }}
            >
              {periodLabel[p]}
            </button>
          ))}
        </div>
        <button
          type="button"
          disabled={!data || rows.length === 0}
          onClick={() => data && downloadCsv(`filet-fees-${period}.csv`, buildFeesCsv(rows, c))}
          style={{
            fontSize: 12.5, color: "var(--text-dim)", border: "1px solid var(--border)",
            borderRadius: 7, padding: "8px 13px", background: "none",
            cursor: !data || rows.length === 0 ? "not-allowed" : "pointer",
          }}
        >
          {c.exportCsv}
        </button>
      </div>

      {loading && <p className="dash-table-empty">{c.loading}</p>}
      {!loading && failed && <p className="dash-table-empty">{c.loadError}</p>}
      {!loading && !failed && data && (
        <>
          <div style={{
            display: "flex", gap: 34, flexWrap: "wrap", padding: "16px 20px",
          }}
          >
            {([
              [c.summaryBuilderFee, `$${fmtAmount(data.summary.builder_fees)}`],
              [c.summaryRoutedVolume, `$${fmtAmount(data.summary.routed_volume)}`],
              [c.summaryFillCount, data.summary.fill_count.toLocaleString("en-US")],
              [
                c.summaryPnlShare,
                data.summary.pnl_share_pct != null
                  ? `${fmtAmount(data.summary.pnl_share_pct, 2)}%`
                  : NO_VALUE,
              ],
            ] as const).map(([label, value]) => (
              <div key={label}>
                <div style={{
                  fontFamily: "var(--font-mono)", fontSize: 10.5, letterSpacing: "0.14em",
                  color: "var(--text-dim)", marginBottom: 6, textTransform: "uppercase",
                }}
                >
                  {label}
                </div>
                <div className="mono" style={{ fontSize: 17, fontWeight: 700 }}>{value}</div>
              </div>
            ))}
          </div>

          {rows.length === 0 ? (
            <p className="dash-table-empty">{c.empty}</p>
          ) : (
            <>
              <div style={{
                display: "grid", gridTemplateColumns: FEES_ROW_GRID_COLUMNS, gap: 16,
                padding: "0 20px 10px", fontFamily: "var(--font-mono)", fontSize: 11,
                letterSpacing: "0.1em", color: "var(--text-dim)",
              }}
              >
                <div>{c.colDate}</div>
                <div style={{ textAlign: "right" }}>{c.colFillCount}</div>
                <div style={{ textAlign: "right" }}>{c.colRoutedVolume}</div>
                <div style={{ textAlign: "right" }}>{c.colBuilderFee}</div>
                <div style={{ textAlign: "right" }}>{c.colEffectiveRate}</div>
              </div>
              {visibleRows.map((r) => (
                <div
                  key={r.date}
                  className="mono"
                  style={{
                    display: "grid", gridTemplateColumns: FEES_ROW_GRID_COLUMNS, gap: 16,
                    padding: "13px 20px", borderTop: "1px solid var(--border)", fontSize: 13.5,
                  }}
                >
                  <div>{r.date}</div>
                  <div style={{ textAlign: "right" }}>{r.fill_count}</div>
                  <div style={{ textAlign: "right" }}>{`$${fmtAmount(r.routed_volume)}`}</div>
                  <div style={{ textAlign: "right" }}>{`$${fmtAmount(r.builder_fee)}`}</div>
                  <div style={{ textAlign: "right" }}>
                    {r.effective_rate_bps != null
                      ? `${fmtAmount(r.effective_rate_bps, 2)} bps`
                      : NO_VALUE}
                  </div>
                </div>
              ))}
              {hasMore && (
                <button
                  type="button"
                  onClick={() => setVisibleCount((v) => v + FEES_LOAD_MORE_STEP)}
                  style={{
                    display: "block", width: "100%", padding: 14, fontSize: 13,
                    color: "var(--text-dim)", background: "none", border: "none",
                    borderTop: "1px solid var(--border)", cursor: "pointer",
                  }}
                >
                  {c.loadMore}
                </button>
              )}
            </>
          )}

          <p style={{ margin: "12px 20px 16px", fontSize: 12.5, color: "var(--text-dim)" }}>
            {c.footerNote}
          </p>
        </>
      )}
    </div>
  );
}

function TxLink({ hash, label }: { hash: string; label: string }) {
  if (!hash) return <span>{NO_VALUE}</span>;
  return (
    <a href={`${HL_EXPLORER_TX_BASE}${hash}`} target="_blank" rel="noopener noreferrer">
      {label}
    </a>
  );
}

/**
 * 「成交記錄・授權歷程」tab 內容（M3 round2 Task 7）——資料**直取 Hyperliquid**
 * （`GET /api/me/fills`／`GET /api/me/authorizations`），結構上不讀自家 DB。
 * lazy fetch：本元件只在 `tab === "history"` 時被掛載才會打 API（見上方
 * `PositionsTable` 的條件渲染）；兩個上游各自獨立成功/失敗，互不拖累
 * （工程原則 3 的展示資料版本——一邊讀不到不該讓另一邊也顯示不出來）。
 */
function HistoryPanel() {
  const COPY = useCopy();
  const c = COPY.dashboard.history;
  const [loading, setLoading] = useState(true);
  const [fills, setFills] = useState<MyFillRow[] | null>(null);
  const [fillsFailed, setFillsFailed] = useState(false);
  const [authorizations, setAuthorizations] = useState<MyAuthorizationRow[] | null>(null);
  const [authorizationsFailed, setAuthorizationsFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setFillsFailed(false);
    setAuthorizationsFailed(false);
    // ⭐ M3 round3 Task 8：抓滿後端上限（90 天），讓前端「7D/30D/全部」期間 chip
    // 有真實資料可切換——「全部」語意是「已抓到的視窗內全部」，不是無上限歷史
    // （後端 `/api/me/fills` 的 `days` 硬性上限 90，見 api.ts 註解）。
    Promise.allSettled([getMyFills(90), getMyAuthorizations()]).then(([f, a]) => {
      if (cancelled) return;
      if (f.status === "fulfilled") setFills(f.value.fills);
      else setFillsFailed(true);
      if (a.status === "fulfilled") setAuthorizations(a.value.authorizations);
      else setAuthorizationsFailed(true);
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
    // ApiError 未被讀取，只用 allSettled 的 status 分流——上游 kind 一律視為
    // 「暫時讀不到」（不 fallback 自家 DB，見 plan 檔尾裁決）。
  }, []);

  if (loading) {
    return (
      <div className="dash-table">
        <p className="dash-table-empty">{c.loading}</p>
      </div>
    );
  }

  return (
    <div className="dash-history-panel">
      <div className="dash-history-section">
        <h3>{c.fillsTitle}</h3>
        <FillsTable rows={fills} failed={fillsFailed} />
      </div>
      <div className="dash-history-section">
        <h3>{c.authorizationsTitle}</h3>
        <AuthorizationsList rows={authorizations} failed={authorizationsFailed} />
      </div>
    </div>
  );
}

// ── 成交記錄表（R2·P1 重構，M3 round3 Task 8）──────────────────────────────
// 現況問題：全量渲染上千列、無分頁、字級約 10px、時間全為 UTC。修法：
// client-side 分頁 50/頁＋期間（7D/30D/全部）與幣種篩選＋字級 ≥13px＋UTC/本地
// 切換。同 FeesGrid 慣例：inline style（globals.css 不在本 task 改動範圍，
// 只處理「資訊框」藍色樣式），沿用既有 `.dash-table`/`.dash-table-head`/
// `.dash-table-row` class 只借版面，字級用 inline style 覆蓋。

const FILLS_PAGE_SIZE = 50;
type FillsPeriodFilter = "7d" | "30d" | "all";
const FILLS_PERIODS: FillsPeriodFilter[] = ["7d", "30d", "all"];
const FILLS_PERIOD_DAYS: Record<FillsPeriodFilter, number | null> = { "7d": 7, "30d": 30, all: null };
const FILLS_ALL_COIN = "__all__";
const FILLS_ROW_FONT_SIZE = 13.5;

/**
 * epoch ms → `YYYY-MM-DD HH:mm UTC±N`（瀏覽器本地時區，標注偏移量）。
 * 與 `fmtUpdatedAtUtc` 的差別只在時區來源：這裡用 `Date` 的本地 getter
 * （由執行環境的時區決定），UTC 模式繼續沿用既有 `fmtUpdatedAtUtc`。
 */
/**
 * ⭐ R-C／S5（2026-08-30 審查修正）：時區偏移舊實作只取整小時（`Math.floor(.../60)`），
 * 半小時／45 分偏移（如 UTC+5:30、UTC+5:45）被無聲截斷成 `UTC+5`——時間戳因此
 * 錯了半小時以上。改法：分鐘為 0 時維持既有兩位數整點形式（`UTC+08`，不動既有
 * 顯示與既有測試），否則補上 `:分鐘`（`UTC+05:30`）。
 */
function fmtFillTimeLocal(epochMs: number): string {
  if (!epochMs) return NO_VALUE;
  const d = new Date(epochMs);
  if (Number.isNaN(d.getTime())) return NO_VALUE;
  const pad = (n: number) => String(n).padStart(2, "0");
  const offsetMin = -d.getTimezoneOffset();
  const sign = offsetMin >= 0 ? "+" : "-";
  const absMin = Math.abs(offsetMin);
  const oh = Math.floor(absMin / 60);
  const om = absMin % 60;
  const offsetLabel = om === 0 ? pad(oh) : `${pad(oh)}:${pad(om)}`;
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
    + `${pad(d.getHours())}:${pad(d.getMinutes())} UTC${sign}${offsetLabel}`;
}

function fillsFilterBtnStyle(active: boolean): CSSProperties {
  return {
    fontSize: 12.5, padding: "6px 12px", borderRadius: 6,
    border: "1px solid var(--border)", cursor: active ? "default" : "pointer",
    background: active ? "var(--inset)" : "transparent",
    color: active ? "var(--text)" : "var(--text-dim)",
    fontWeight: active ? 600 : 400,
  };
}

function FillsTable({ rows, failed }: { rows: MyFillRow[] | null; failed: boolean }) {
  const COPY = useCopy();
  const c = COPY.dashboard.history;
  const [period, setPeriod] = useState<FillsPeriodFilter>("30d");
  const [coin, setCoin] = useState<string>(FILLS_ALL_COIN);
  const [page, setPage] = useState(1);
  const [tz, setTz] = useState<"local" | "utc">("local");

  if (failed) {
    return (
      <div className="dash-table">
        <p className="dash-table-empty">{c.loadError}</p>
      </div>
    );
  }
  if (!rows || rows.length === 0) {
    return (
      <div className="dash-table">
        <p className="dash-table-empty">{c.fillsEmpty}</p>
      </div>
    );
  }

  function changePeriod(p: FillsPeriodFilter) {
    setPeriod(p);
    setPage(1);
  }
  function changeCoin(v: string) {
    setCoin(v);
    setPage(1);
  }

  const coins = Array.from(new Set(rows.map((r) => r.coin))).sort();
  const periodDays = FILLS_PERIOD_DAYS[period];
  const cutoff = periodDays != null ? Date.now() - periodDays * 86_400_000 : null;
  const filtered = rows.filter((r) => {
    if (cutoff != null && r.time < cutoff) return false;
    if (coin !== FILLS_ALL_COIN && r.coin !== coin) return false;
    return true;
  });

  const totalPages = Math.max(1, Math.ceil(filtered.length / FILLS_PAGE_SIZE));
  const clampedPage = Math.min(page, totalPages);
  const start = (clampedPage - 1) * FILLS_PAGE_SIZE;
  const pageRows = filtered.slice(start, start + FILLS_PAGE_SIZE);

  return (
    <div className="dash-table">
      <div style={{
        display: "flex", flexWrap: "wrap", gap: 12, alignItems: "center",
        justifyContent: "space-between", padding: "16px 20px",
      }}
      >
        <div style={{ display: "flex", gap: 4 }}>
          {FILLS_PERIODS.map((p) => (
            <button
              key={p}
              type="button"
              disabled={period === p}
              onClick={() => changePeriod(p)}
              style={fillsFilterBtnStyle(period === p)}
            >
              {c.periods[p]}
            </button>
          ))}
        </div>
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <select
            aria-label={c.coinFilterLabel}
            value={coin}
            onChange={(e) => changeCoin(e.target.value)}
            style={{
              fontSize: 12.5, padding: "6px 10px", borderRadius: 6,
              border: "1px solid var(--border)", background: "var(--card)", color: "var(--text)",
            }}
          >
            <option value={FILLS_ALL_COIN}>{c.coinFilterAll}</option>
            {coins.map((coinName) => (
              <option key={coinName} value={coinName}>{coinName}</option>
            ))}
          </select>
          <div style={{ display: "flex", gap: 4 }}>
            <button type="button" disabled={tz === "local"} onClick={() => setTz("local")}
              style={fillsFilterBtnStyle(tz === "local")}
            >
              {c.tzLocal}
            </button>
            <button type="button" disabled={tz === "utc"} onClick={() => setTz("utc")}
              style={fillsFilterBtnStyle(tz === "utc")}
            >
              {c.tzUtc}
            </button>
          </div>
        </div>
      </div>

      {filtered.length === 0 ? (
        <p className="dash-table-empty">{c.fillsEmpty}</p>
      ) : (
        <>
          <div className="dash-table-head" style={{ fontSize: FILLS_ROW_FONT_SIZE }}>
            <div>{c.time}</div>
            <div>{c.coin}</div>
            <div>{c.side}</div>
            <div>{c.px}</div>
            <div>{c.sz}</div>
            <div>{c.fee}</div>
            <div>{c.closedPnl}</div>
            <div>{c.tx}</div>
          </div>
          {pageRows.map((r) => (
            <div className="dash-table-row" key={r.hash || `${r.time}-${r.coin}`}
              style={{ fontSize: FILLS_ROW_FONT_SIZE }}
            >
              <div>{tz === "local" ? fmtFillTimeLocal(r.time) : fmtUpdatedAtUtc(r.time / 1000)}</div>
              <div>{r.coin}</div>
              <div>{r.side === "B" ? c.buy : c.sell}</div>
              <div>{fmtAmount(r.px)}</div>
              <div>{fmtAmount(r.sz)}</div>
              <div>{fmtAmount(r.fee)}</div>
              <div>{fmtAmount(r.closed_pnl)}</div>
              <div>
                <TxLink hash={r.hash} label={c.viewTx} />
              </div>
            </div>
          ))}
          <div style={{
            display: "flex", justifyContent: "space-between", alignItems: "center",
            flexWrap: "wrap", gap: 10, padding: "14px 20px", borderTop: "1px solid var(--border)",
            fontSize: 12.5, color: "var(--text-dim)",
          }}
          >
            <span>
              {c.pagination.showing}
              {start + 1}
              {c.pagination.rangeSep}
              {Math.min(start + FILLS_PAGE_SIZE, filtered.length)}
              {c.pagination.ofTotal}
              {filtered.length}
            </span>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <button
                type="button"
                disabled={clampedPage <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                style={{
                  fontSize: 12.5, padding: "6px 12px", borderRadius: 6,
                  border: "1px solid var(--border)", background: "none", color: "var(--text-dim)",
                  cursor: clampedPage <= 1 ? "not-allowed" : "pointer",
                }}
              >
                {c.pagination.prev}
              </button>
              <span className="mono">{clampedPage} / {totalPages}</span>
              <button
                type="button"
                disabled={clampedPage >= totalPages}
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                style={{
                  fontSize: 12.5, padding: "6px 12px", borderRadius: 6,
                  border: "1px solid var(--border)", background: "none", color: "var(--text-dim)",
                  cursor: clampedPage >= totalPages ? "not-allowed" : "pointer",
                }}
              >
                {c.pagination.next}
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

type HistoryCopy = ReturnType<typeof useCopy>["dashboard"]["history"];

/**
 * 授權動作 → 人類可讀摘要（[W2] 2026-08-29 opus 審查修正：後端只給結構化欄位
 * `agent_address`／`builder`／`max_fee_rate`，組字＋雙語一律在這裡做，禁止
 * 內嵌中文——標籤全部來自 `copy.ts`，這裡只負責組句順序）。
 */
function authorizationSummary(c: HistoryCopy, r: MyAuthorizationRow): string {
  if (r.action_type === "approveAgent" && r.agent_address) {
    return `${c.actionApproveAgent} ${r.agent_address}`;
  }
  if (r.action_type === "approveBuilderFee" && r.builder) {
    const rate = r.max_fee_rate ?? "?";
    return `${c.actionApproveBuilderFeeLabel} ${rate} ${c.actionApproveBuilderFeeTo} ${r.builder}`;
  }
  return c.actionUnknown;
}

function AuthorizationsList({
  rows, failed,
}: {
  rows: MyAuthorizationRow[] | null;
  failed: boolean;
}) {
  const COPY = useCopy();
  const c = COPY.dashboard.history;

  if (failed) {
    return (
      <div className="dash-table">
        <p className="dash-table-empty">{c.loadError}</p>
      </div>
    );
  }
  if (!rows || rows.length === 0) {
    return (
      <div className="dash-table">
        <p className="dash-table-empty">{c.authorizationsEmpty}</p>
      </div>
    );
  }

  return (
    <div className="dash-table">
      <div className="dash-table-head">
        <div>{c.time}</div>
        <div>{c.action}</div>
        <div>{c.summary}</div>
        <div>{c.tx}</div>
      </div>
      {rows.map((r) => (
        <div className="dash-table-row" key={r.hash || `${r.time}-${r.action_type}`}>
          <div>{fmtUpdatedAtUtc(r.time / 1000)}</div>
          <div>{r.action_type}</div>
          <div>{authorizationSummary(c, r)}</div>
          <div>
            <TxLink hash={r.hash} label={c.viewTx} />
          </div>
        </div>
      ))}
    </div>
  );
}
