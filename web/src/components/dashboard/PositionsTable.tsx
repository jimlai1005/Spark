"use client";
import { useEffect, useState } from "react";
import {
  getMyAuthorizations,
  getMyFills,
  type DashboardFeesMonth,
  type DashboardPosition,
  type MyAuthorizationRow,
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
      {tab === "fees" && <FeesGrid feesMonth={feesMonth} />}
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
    Promise.allSettled([getMyFills(), getMyAuthorizations()]).then(([f, a]) => {
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

function FillsTable({ rows, failed }: { rows: MyFillRow[] | null; failed: boolean }) {
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
        <p className="dash-table-empty">{c.fillsEmpty}</p>
      </div>
    );
  }

  return (
    <div className="dash-table">
      <div className="dash-table-head">
        <div>{c.time}</div>
        <div>{c.coin}</div>
        <div>{c.side}</div>
        <div>{c.px}</div>
        <div>{c.sz}</div>
        <div>{c.fee}</div>
        <div>{c.closedPnl}</div>
        <div>{c.tx}</div>
      </div>
      {rows.map((r) => (
        <div className="dash-table-row" key={r.hash || `${r.time}-${r.coin}`}>
          <div>{fmtUpdatedAtUtc(r.time / 1000)}</div>
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
