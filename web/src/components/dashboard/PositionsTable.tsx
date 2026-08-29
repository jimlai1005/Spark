"use client";
import { useState } from "react";
import type { DashboardFeesMonth, DashboardPosition } from "@/lib/api";
import { fmtAmount, NO_VALUE } from "@/lib/format";
import { useCopy } from "@/lib/lang";

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
 * 次要區塊：跟單持倉表＋tab 列（Task 14）。「費用明細」v1 無獨立客戶自助帳單端點
 * （`lib/api.ts` 只有 admin only 的 `/api/ops/*`），以 `fees_month.daily_bars` 簡表
 * 呈現（plan 0.2「查有無現成，沒有就以 fees_month 資料簡表呈現」）。「成交記錄・
 * 授權歷程」列 backlog，tab 本身 disabled（0.2）。
 */
export function PositionsTable({
  positions, feesMonth,
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
        <button type="button" className="dash-tab" disabled title={c.tabs.comingSoon}>
          {c.tabs.history}
        </button>
      </div>

      {tab === "positions" && <PositionsGrid positions={positions} />}
      {tab === "fees" && <FeesGrid feesMonth={feesMonth} />}
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

function FeesGrid({ feesMonth }: { feesMonth: DashboardFeesMonth | null }) {
  const COPY = useCopy();
  const c = COPY.dashboard.feesTable;
  const bars = feesMonth?.daily_bars ?? [];

  if (bars.length === 0) {
    return (
      <div className="dash-table">
        <p className="dash-table-empty">{c.empty}</p>
      </div>
    );
  }

  return (
    <div className="dash-table dash-fees-list">
      {bars.map(([date, fee]) => (
        <div className="dash-fees-row" key={date}>
          <span>{date}</span>
          <span className="mono">${fmtAmount(fee)}</span>
        </div>
      ))}
    </div>
  );
}
