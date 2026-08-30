"use client";
/**
 * `/leaderboard` — Hyperliquid 主網公開排行榜展示頁（M3 round2 Task 5）。
 * 唯一資料源是 `GET /api/public/leaderboard?window=…`（無需登入；後端已把上游
 * 36MB 全量 JSON 快取＋裁切，本頁絕不直連 stats-data，見
 * `src/spark/publicapi/hl_leaderboard.py` 檔頭）。
 *
 * 三態明確區分（plan 明訂 load/error/empty）：`getPublicLeaderboard` 刻意不吞錯
 * （見 lib/publicApi.ts 檔頭），本頁用 try/catch 自行分流 loading／error／ready
 * （ready 且 rows 為空 → empty 文案）。
 *
 * 視覺沿用 dashboard 既有 `.dash-tabs`／`.dash-table` 表格 class（不引新 UI 庫）；
 * 每列是整列可點的 `<Link>`，指向 Task 6 的 `/traders/{address}` 詳情頁。
 */
import Link from "next/link";
import { useEffect, useState } from "react";
import { fmtRatioPct, fmtUsdCompact, shortAddr } from "@/lib/format";
import { useCopy } from "@/lib/lang";
import {
  getPublicLeaderboard,
  LEADERBOARD_WINDOWS,
  type LeaderboardWindow,
  type PublicLeaderboardRow,
} from "@/lib/publicApi";

type LoadState = "loading" | "error" | "ready";

export default function LeaderboardPage() {
  const COPY = useCopy();
  const c = COPY.leaderboard;
  const [window_, setWindow] = useState<LeaderboardWindow>("month");
  const [rows, setRows] = useState<PublicLeaderboardRow[]>([]);
  const [state, setState] = useState<LoadState>("loading");

  useEffect(() => {
    let cancelled = false;
    setState("loading");
    getPublicLeaderboard(window_)
      .then((resp) => {
        if (cancelled) return;
        setRows(resp.rows);
        setState("ready");
      })
      .catch(() => {
        if (cancelled) return;
        setRows([]);
        setState("error");
      });
    return () => {
      cancelled = true;
    };
  }, [window_]);

  return (
    <main className="page leaderboard-page">
      <header className="strategies-page-head">
        <h1>{c.heading}</h1>
        <p className="section-sub">{c.sub}</p>
      </header>

      <div className="dash-tabs" role="tablist">
        {LEADERBOARD_WINDOWS.map((w) => (
          <button
            key={w}
            type="button"
            role="tab"
            className="dash-tab"
            data-active={window_ === w}
            aria-selected={window_ === w}
            onClick={() => setWindow(w)}
          >
            {c.windows[w]}
          </button>
        ))}
      </div>

      {state === "loading" && <p className="hint">{c.loading}</p>}
      {state === "error" && <p className="hint">{c.error}</p>}
      {state === "ready" && rows.length === 0 && <p className="hint">{c.empty}</p>}

      {state === "ready" && rows.length > 0 && (
        <div className="dash-table">
          <div className="dash-table-head">
            <div>{c.table.rank}</div>
            <div>{c.table.trader}</div>
            <div>{c.table.accountValue}</div>
            <div>{c.table.pnl}</div>
            <div>{c.table.roi}</div>
            <div>{c.table.volume}</div>
          </div>
          {rows.map((r, i) => (
            <Link
              key={r.address}
              // ⭐ [W3] 2026-08-29 opus 審查修正：不再帶 `?name=` 查詢參數——
              // 交易員詳情頁一律自己用 shortAddr 顯示標題，不信任 client 端可
              // 竄改的 query param（displayName 只在本頁表格內顯示）。
              href={`/traders/${r.address}`}
              className="dash-table-row"
            >
              <div>{i + 1}</div>
              <div className="mono">{r.display_name || shortAddr(r.address)}</div>
              <div>{fmtUsdCompact(r.account_value)}</div>
              <div>{fmtUsdCompact(r.pnl)}</div>
              <div>{fmtRatioPct(r.roi)}</div>
              <div>{fmtUsdCompact(r.vlm)}</div>
            </Link>
          ))}
        </div>
      )}
    </main>
  );
}
