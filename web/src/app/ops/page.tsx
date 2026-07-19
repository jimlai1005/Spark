"use client";
/**
 * /ops — 營運儀表板（管理端；Header 刻意不放連結，與 /admin 同慣例）。
 * 授權是後端結構性職責（app.py 的 _require_admin）；本頁只負責在 403 時把
 * 「僅限管理員」講清楚，不做任何前端授權判斷。
 *
 * ⭐ 顯示原則：所有金額由後端以字串（Decimal 無損）送來，前端只在最後一刻 parse
 * 成顯示字串，不做任何金額算術——門檻比較（over_threshold）一律沿用後端結論，
 * 前端重算等於製造第二個真相來源（工程原則 1）。
 */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import {
  ApiError,
  getOpsCustomers,
  getOpsRevenue,
  type OpsCustomerRow,
  type OpsCustomersResp,
  type OpsRevenueResp,
} from "@/lib/api";
import { COPY } from "@/lib/copy";
import { fmtAmount, fmtRatioPct, NO_VALUE, shortAddr } from "@/lib/format";

const DAY_OPTIONS = [1, 7, 30] as const;
/** 與後端 /api/ops/revenue 的 threshold_pct 預設同值（比例，非百分比）。 */
const THRESHOLD_PCT = 0.01;

const c = COPY.ops;

export default function OpsPage() {
  const [days, setDays] = useState<number>(1);
  const revenue = useQuery<OpsRevenueResp>({
    queryKey: ["ops-revenue"],
    queryFn: () => getOpsRevenue(THRESHOLD_PCT),
  });
  const customers = useQuery<OpsCustomersResp>({
    queryKey: ["ops-customers", days],
    queryFn: () => getOpsCustomers(days),
  });

  const errors = [revenue.error, customers.error];
  if (errors.some((e) => e instanceof ApiError && e.status === 403)) {
    return <main className="page"><p>{c.forbidden}</p></main>;
  }
  if (errors.some((e) => e instanceof ApiError && e.kind === "auth")) {
    return <main className="page"><p>{COPY.common.notLoggedIn}</p></main>;
  }
  if (revenue.isLoading && customers.isLoading) {
    return <main className="page"><p className="hint">{COPY.common.loading}</p></main>;
  }

  return (
    <main className="page">
      <p className="eyebrow">{c.eyebrow}</p>
      <h1>{c.title}</h1>

      <section aria-label="收入對帳">
        <h2 className="ops-section-title">{c.revenue.title}</h2>
        {revenue.error ? (
          <p className="ops-query-error">{errText(revenue.error)}</p>
        ) : revenue.data ? (
          <RevenueBlock data={revenue.data} />
        ) : (
          <p className="hint">{COPY.common.loading}</p>
        )}
      </section>

      <section aria-label="每客戶損益">
        <h2 className="ops-section-title">{c.customers.title}</h2>
        <p className="hint">{c.customers.note}</p>
        <RangeTabs days={days} onSelect={setDays} />
        {customers.error ? (
          <p className="ops-query-error">{errText(customers.error)}</p>
        ) : customers.data ? (
          <CustomersBlock data={customers.data} />
        ) : (
          <p className="hint">{COPY.common.loading}</p>
        )}
      </section>
    </main>
  );
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

// ---------- 收入對帳 ----------
function RevenueBlock({ data }: { data: OpsRevenueResp }) {
  const r = c.revenue;
  // ⭐ 判別欄位先擋：歷史不足時後端不給數值欄，這裡也絕不退化成顯示 0
  //（把整段累積量當單日增量會造出假差額——顯示 0 同樣是假訊號的一種）。
  if (data.insufficient_accrued_history) {
    return (
      <div className="panel ops-notice">
        <p className="ops-notice-title">{r.insufficient}</p>
        <p className="hint">{r.insufficientNote}</p>
        <p className="hint mono">history_points: {data.history_points}</p>
      </div>
    );
  }

  const pctUnknown = data.discrepancy_pct == null;
  return (
    <>
      {data.over_threshold && (
        <div className="ops-alert" role="alert">
          <p className="ops-alert-title">{r.alertTitle}</p>
          <p className="ops-alert-body">{r.alertBody}</p>
        </div>
      )}
      <div className="panel">
        <p className="hint">{r.note}</p>
        {/* ⭐ 對帳三欄固定 4 位小數且附完整原值（title）：應收與實收若各自捨到 2 位，
            小於 0.01 的真實差額會在畫面上消失——那正是本頁要抓的訊號。 */}
        <dl className="ops-stats">
          <Stat label={r.attributed} value={fmtAmount(data.attributed, 4)} raw={data.attributed} />
          <Stat label={r.accruedDelta} value={fmtAmount(data.accrued_delta, 4)} raw={data.accrued_delta} />
          <Stat
            label={r.discrepancy}
            value={fmtAmount(data.discrepancy, 4)}
            raw={data.discrepancy}
            tone={data.over_threshold ? "bad" : "neutral"}
          />
          <Stat
            label={r.discrepancyPct}
            value={fmtRatioPct(data.discrepancy_pct, 2)}
            tone={data.over_threshold ? "bad" : "neutral"}
          />
          <Stat label={r.threshold} value={fmtRatioPct(data.threshold_pct, 2)} />
          <Stat label={r.rowsCounted} value={String(data.rows)} />
        </dl>
        {pctUnknown && <p className="hint">{r.pctUnavailable}</p>}
        {!data.over_threshold && <p className="hint">{r.ok}</p>}
        <p className="hint mono ops-window">
          {r.window}: {data.window_start} → {data.window_end}（{data.prev_day} → {data.day}）
        </p>
      </div>
    </>
  );
}

function Stat({ label, value, raw, tone = "neutral" }: {
  label: string; value: string; raw?: string | null; tone?: "neutral" | "bad";
}) {
  return (
    <div className="ops-stat">
      <dt>{label}</dt>
      <dd className={`mono${tone === "bad" ? " is-bad" : ""}`} title={raw ?? undefined}>{value}</dd>
    </div>
  );
}

// ---------- 每客戶損益 ----------
function RangeTabs({ days, onSelect }: { days: number; onSelect: (d: number) => void }) {
  const labels: Record<number, string> = {
    1: c.customers.ranges.d1, 7: c.customers.ranges.d7, 30: c.customers.ranges.d30,
  };
  return (
    <div className="ops-range" role="group" aria-label="統計期間">
      <span className="hint">{c.customers.rangeLabel}</span>
      {DAY_OPTIONS.map((d) => (
        <button
          key={d}
          type="button"
          className={`btn btn-secondary ops-range-btn${d === days ? " is-active" : ""}`}
          aria-pressed={d === days}
          onClick={() => onSelect(d)}
        >
          {labels[d]}
        </button>
      ))}
    </div>
  );
}

function CustomersBlock({ data }: { data: OpsCustomersResp }) {
  const cols = c.customers.cols;
  const rows = data.customers ?? [];
  const manifestErrors = data.manifest_errors ?? [];
  return (
    <>
      {manifestErrors.length > 0 && (
        <div className="ops-alert ops-alert-warn" role="alert">
          <p className="ops-alert-title">{c.customers.manifestErrors}</p>
          <ul className="ops-error-list">
            {manifestErrors.map((m, i) => <li key={i} className="mono">{m}</li>)}
          </ul>
        </div>
      )}
      {rows.length === 0 ? (
        <p>{c.customers.empty}</p>
      ) : (
        <div className="panel">
          <table className="admin-table ops-table">
            <thead>
              <tr>
                <th scope="col">{cols.account}</th>
                <th scope="col">{cols.address}</th>
                <th scope="col">{cols.fills}</th>
                <th scope="col">{cols.notional}</th>
                <th scope="col">{cols.builderFee}</th>
                <th scope="col">{cols.takerShare}</th>
                <th scope="col">{cols.accountValue}</th>
                <th scope="col">{cols.subscription}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => <CustomerRow key={row.account_id} row={row} />)}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

/**
 * 跨客戶隔離的前端鏡射：某列 error 非空時標示該列，但**照樣渲染**它已取得的欄位，
 * 其他列完全不受影響（後端 ops.customer_pnl 已做隔離，前端不得把它退化成整頁失敗）。
 */
function CustomerRow({ row }: { row: OpsCustomerRow }) {
  const failed = !!row.error;
  return (
    <>
      <tr className={failed ? "ops-row-failed" : undefined}>
        <td className="mono">{row.account_id}</td>
        <td className="mono" title={row.user_address}>{shortAddr(row.user_address)}</td>
        <td className="mono">{row.fills ?? NO_VALUE}</td>
        <td className="mono" title={row.notional}>{fmtAmount(row.notional)}</td>
        <td className="mono" title={row.builder_fee}>{fmtAmount(row.builder_fee)}</td>
        <td className="mono">{fmtRatioPct(row.taker_share)}</td>
        <td className="mono" title={row.account_value ?? undefined}>{fmtAmount(row.account_value)}</td>
        <td className="mono">{row.subscription}</td>
      </tr>
      {failed && (
        <tr className="ops-row-failed">
          <td colSpan={8} className="ops-row-error">
            <span className="ops-row-error-label">{c.customers.rowError}</span>
            <span className="mono">{row.error}</span>
            <span className="hint"> {c.customers.rowErrorHint}</span>
          </td>
        </tr>
      )}
    </>
  );
}
