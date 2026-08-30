"use client";
/**
 * `/explore` — 可跟單對象探索榜（M3 round3 Task 4，設計審查 R2·A）。
 *
 * 從舊版「鯨魚 PnL 榜」（`/leaderboard`，見 `hl_leaderboard.py`）重構而來：解決
 * 冷啟動——讓用戶找到「值得貼進進階模式的地址」。唯一資料源是
 * `GET /api/public/explore`（無需登入，`hl_explore.py`），排序（風險調整後報酬＝
 * 30D 報酬率 ÷ 最大回撤）與資格過濾（樣本門檻／回撤上限／集中度上限）**全在
 * 後端完成**（R2-01）——本頁三顆 chip 只送布林開關，絕不自己排序或過濾一次
 * 已經拿到的 rows（否則分頁的 `total_qualified` 會跟畫面對不上）。
 *
 * 三態明確區分（同 `/leaderboard` 舊頁與 `PositionsTable.history` 的既有慣例）：
 * - `state==="error"`：fetch 本身失敗（連線／非 200／格式異常，見
 *   `getPublicExplore` 檔頭「不吞錯」設計）→ R2·C 態三，時間戳＋重試鍵。
 * - `state==="ready" && resp.building`：後端從未成功建置過 index（200，不是
 *   故障，見 `ExploreIndex.query` 檔頭）→ R2·C 態二，「建置中」文案。
 * - `state==="ready" && !resp.building && rows.length===0`：成功但零筆合格 → 空態。
 *
 * `window` 本輪固定 `30d`（7D/90D/全部 三個 chip disabled＋「即將推出」，D1：
 * enrich 成本是 ×4，本輪不做）。
 */
import Link from "next/link";
import { type Dispatch, type SetStateAction, useEffect, useState } from "react";
import { NO_VALUE, fmtUpdatedAtUtc } from "@/lib/format";
import { useCopy } from "@/lib/lang";
import { getPublicExplore, type ExploreResp, type ExploreRow } from "@/lib/publicApi";

type LoadState = "loading" | "error" | "ready";

const SPARK_W = 96;
const SPARK_H = 28;

// 後端 `hl_explore._apply_tags`／`_exposure`（`src/spark/publicapi/hl_explore.py`）
// 回傳的是 locale 中性代碼（D14，2026-08-30 主線程裁決）：`tags` ⊂
// {"low_drawdown","concentrated"}、`exposure.dir` ∈ {"long","short",null}。
// 顯示文案一律用下面兩個對映表換成 `COPY.explore.tags.*`／`COPY.explore.
// exposureDir.*`；未知代碼（後端日後新增、前端尚未跟上）防禦性地顯示原始
// 代碼字串，不吞掉、不當機。

/** tag 代碼 → `[顯示文案, CSS `data-tag` 值]`；未知代碼原樣顯示，且不掛
 * 顏色樣式（樸素灰底 chip，見 `globals.css` `.explore-tag` 預設樣式）。 */
function tagLabel(code: string, c: ReturnType<typeof useCopy>["explore"]): string {
  if (code === "low_drawdown") return c.tags.lowDrawdown;
  if (code === "concentrated") return c.tags.concentrated;
  return code;
}

/** exposure 方向代碼 → 顯示文案；未知代碼／`null` 原樣顯示（`null` 不會走到
 * 這裡——呼叫端已先判斷 `dir != null` 才呼叫）。 */
function exposureDirLabel(dir: string, c: ReturnType<typeof useCopy>["explore"]): string {
  if (dir === "long") return c.exposureDir.long;
  if (dir === "short") return c.exposureDir.short;
  return dir;
}

/** values → SVG polyline 的 `points` 字串（等距 x，y 依 min/max 正規化，同
 * `EquityCurve.toPoints` 的簡化版：sparkline 只需要形狀，不需要 y 軸刻度）。 */
function sparkPoints(values: number[]): string {
  if (values.length === 0) return "";
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min;
  const n = values.length;
  return values
    .map((v, i) => {
      const x = n === 1 ? 0 : (i / (n - 1)) * SPARK_W;
      const y = span === 0 ? SPARK_H / 2 : SPARK_H - ((v - min) / span) * SPARK_H;
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
}

function fmtSignedPct(n: number): string {
  return `${n > 0 ? "+" : ""}${n.toFixed(1)}%`;
}

function fmtPct1(n: number | null): string {
  return n == null ? NO_VALUE : `${n.toFixed(1)}%`;
}

/** 分頁按鈕：目前頁 ± `span`，並夾在 `[1, total]` 範圍內（不做省略號，`total`
 * 一般不大——`page_size` 預設 25，符合 `ExploreConfig.candidate_pool` 上限 100
 * 時最多 4 頁）。 */
function pageWindow(current: number, total: number, span = 2): number[] {
  const start = Math.max(1, current - span);
  const end = Math.min(total, current + span);
  return Array.from({ length: Math.max(0, end - start + 1) }, (_, i) => start + i);
}

export default function ExplorePage() {
  const COPY = useCopy();
  const c = COPY.explore;

  const [qualified, setQualified] = useState(true);
  const [maxDd, setMaxDd] = useState(true);
  const [excludeConcentrated, setExcludeConcentrated] = useState(true);
  const [page, setPage] = useState(1);
  const [reloadKey, setReloadKey] = useState(0);

  const [state, setState] = useState<LoadState>("loading");
  const [resp, setResp] = useState<ExploreResp | null>(null);
  const [errorAt, setErrorAt] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    setState("loading");
    getPublicExplore(page, { qualified, maxDd, excludeConcentrated })
      .then((r) => {
        if (cancelled) return;
        setResp(r);
        setState("ready");
      })
      .catch(() => {
        if (cancelled) return;
        setResp(null);
        setErrorAt(Math.floor(Date.now() / 1000));
        setState("error");
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, qualified, maxDd, excludeConcentrated, reloadKey]);

  function toggleFilter(setter: Dispatch<SetStateAction<boolean>>) {
    setter((v) => !v);
    setPage(1);
  }

  const showBuilding = state === "ready" && !!resp?.building;
  const showEmpty = state === "ready" && resp != null && !resp.building && resp.rows.length === 0;
  const showTable = state === "ready" && resp != null && !resp.building && resp.rows.length > 0;
  // W3：index 成功建置過（非 building）就有意義的 `updated_at`，不論這頁是否為空。
  const showUpdatedAt = (showTable || showEmpty) && resp?.updated_at != null;

  const totalPages = resp ? Math.max(1, Math.ceil(resp.total_qualified / Math.max(1, resp.page_size))) : 1;
  const rangeStart = resp && resp.rows.length > 0 ? (resp.page - 1) * resp.page_size + 1 : 0;
  const rangeEnd = resp && resp.rows.length > 0 ? rangeStart + resp.rows.length - 1 : 0;

  return (
    <main className="page explore-page">
      <header className="strategies-page-head explore-head">
        <div>
          <h1>{c.heading}</h1>
          <p className="section-sub">{c.sub}</p>
          {/* W3（R-C，2026-08-30 審查修正）：後端 index 有 TTL（10min），文案已同步
              改「每 10 分鐘更新」——渲染 `updated_at` 讓用戶自己核對這份榜單多新，
              不只是相信文案描述的頻率。只在成功且非 building 態顯示（loading/error/
              building 都還沒有一個有意義的 `updated_at`）。 */}
          {showUpdatedAt && resp?.updated_at != null && (
            <p className="hint explore-updated-at">
              {c.updatedAtPrefix}
              {fmtUpdatedAtUtc(resp.updated_at)}
            </p>
          )}
        </div>
        <div className="explore-disclaimer-badge">{c.disclaimerBadge}</div>
      </header>

      <div className="explore-chips">
        <div className="explore-window-group" role="group" aria-label={c.windows.d30}>
          <span className="explore-window-btn" data-active="true">{c.windows.d30}</span>
          <button type="button" className="explore-window-btn" disabled title={c.windowComingSoon}>
            {c.windows.d7}
          </button>
          <button type="button" className="explore-window-btn" disabled title={c.windowComingSoon}>
            {c.windows.d90}
          </button>
          <button type="button" className="explore-window-btn" disabled title={c.windowComingSoon}>
            {c.windows.all}
          </button>
        </div>
        <button
          type="button"
          className="explore-chip"
          data-active={qualified}
          aria-pressed={qualified}
          onClick={() => toggleFilter(setQualified)}
        >
          {c.filters.sample}
        </button>
        <button
          type="button"
          className="explore-chip"
          data-active={maxDd}
          aria-pressed={maxDd}
          onClick={() => toggleFilter(setMaxDd)}
        >
          {c.filters.maxDd}
        </button>
        <button
          type="button"
          className="explore-chip"
          data-active={excludeConcentrated}
          aria-pressed={excludeConcentrated}
          onClick={() => toggleFilter(setExcludeConcentrated)}
        >
          {c.filters.concentrated}
        </button>
        <span className="explore-count mono">
          {c.countPrefix}
          {(resp?.total_scanned ?? 0).toLocaleString()}
          {c.countMid}
          {resp?.total_qualified ?? 0}
          {c.countSuffix}
        </span>
      </div>

      {state === "loading" && <p className="hint">{COPY.common.loading}</p>}

      {state === "error" && (
        <div className="hint explore-error" role="alert">
          <p>
            {c.errorPrefix}
            {errorAt != null ? fmtUpdatedAtUtc(errorAt) : NO_VALUE}
          </p>
          <button type="button" className="btn btn-ghost" onClick={() => setReloadKey((k) => k + 1)}>
            {COPY.common.retry}
          </button>
        </div>
      )}

      {showBuilding && <p className="hint">{c.building}</p>}
      {showEmpty && <p className="hint">{c.empty}</p>}

      {showTable && resp && (
        <>
          <div className="explore-table">
            <div className="explore-table-head">
              <div>{c.table.rank}</div>
              <div>{c.table.account}</div>
              <div>{c.table.sparkline}</div>
              <div>{c.table.ret}</div>
              <div>{c.table.dd}</div>
              <div>{c.table.days}</div>
              <div>{c.table.winRate}</div>
              <div>{c.table.exposure}</div>
              <div>{c.table.actions}</div>
            </div>
            {resp.rows.map((row, i) => (
              <ExploreRowView key={row.address} row={row} rank={(resp.page - 1) * resp.page_size + i + 1} />
            ))}
          </div>

          <div className="explore-pagination">
            <span className="explore-pagination-summary">
              {c.pagination.showing}
              {rangeStart}
              {c.pagination.rangeSep}
              {rangeEnd}
              {c.pagination.ofTotal}
              {resp.total_qualified}
              {c.pagination.perPagePrefix}
              {resp.page_size}
              {c.pagination.perPageSuffix}
            </span>
            <div className="explore-pagination-controls">
              <button
                type="button"
                className="explore-page-btn"
                disabled={resp.page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
              >
                {c.pagination.prev}
              </button>
              {pageWindow(resp.page, totalPages).map((n) => (
                <button
                  key={n}
                  type="button"
                  className="explore-page-btn"
                  data-active={n === resp.page}
                  onClick={() => setPage(n)}
                >
                  {n}
                </button>
              ))}
              <button
                type="button"
                className="explore-page-btn"
                disabled={resp.page >= totalPages}
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              >
                {c.pagination.next}
              </button>
            </div>
          </div>
        </>
      )}
    </main>
  );
}

function ExploreRowView({ row, rank }: { row: ExploreRow; rank: number }) {
  const COPY = useCopy();
  const c = COPY.explore;
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), 1500);
    return () => clearTimeout(t);
  }, [copied]);

  function handleCopy() {
    if (typeof navigator !== "undefined" && navigator.clipboard) {
      void navigator.clipboard.writeText(row.address);
    }
    setCopied(true);
  }

  const sub = row.coins.length > 0
    ? `${row.coins.join(" ")}${c.subSep}${row.account_bucket}`
    : row.account_bucket;
  const exposureLabel = row.exposure.dir != null && row.exposure.pct != null
    ? `${exposureDirLabel(row.exposure.dir, c)} ${row.exposure.pct.toFixed(1)}%`
    : NO_VALUE;
  const longPct = row.exposure.dir === "long"
    ? (row.exposure.pct ?? 0)
    : row.exposure.dir === "short"
      ? 100 - (row.exposure.pct ?? 0)
      : 0;

  return (
    <div className="explore-table-row">
      <div className="mono explore-rank">{rank}</div>
      <div className="explore-account">
        <div className="explore-account-top">
          <span className="mono">{row.label}</span>
          <button
            type="button"
            className="explore-copy-btn"
            onClick={handleCopy}
            aria-label={c.copyAddress}
            title={row.address}
          >
            {copied ? c.copied : "⧉"}
          </button>
          {row.tags.map((tag) => (
            <span key={tag} className="explore-tag" data-tag={tag}>{tagLabel(tag, c)}</span>
          ))}
        </div>
        <div className="explore-sub">{sub}</div>
      </div>
      <div className="explore-spark">
        <svg
          width={SPARK_W}
          height={SPARK_H}
          viewBox={`0 0 ${SPARK_W} ${SPARK_H}`}
          role="img"
          aria-label={c.table.sparkline}
        >
          <polyline
            points={sparkPoints(row.spark)}
            fill="none"
            stroke={row.ret_30d_pct >= 0 ? "var(--pos)" : "var(--neg)"}
            strokeWidth="1.4"
          />
        </svg>
      </div>
      <div className={`mono explore-ret ${row.ret_30d_pct >= 0 ? "pos" : "neg"}`}>
        {fmtSignedPct(row.ret_30d_pct)}
      </div>
      <div className="mono neg explore-dd">{row.max_dd_30d_pct.toFixed(1)}%</div>
      <div className="mono explore-days">{row.live_days}</div>
      <div className="mono explore-wr">{fmtPct1(row.close_win_rate_pct)}</div>
      <div className="explore-exposure">
        <div className="explore-exposure-bar">
          <div className="explore-exposure-fill" style={{ width: `${longPct}%` }} />
        </div>
        <span className="mono explore-exposure-label">{exposureLabel}</span>
      </div>
      <div className="explore-actions">
        <Link href={`/traders/${row.address}`} className="btn btn-ghost explore-view-btn">
          {c.view}
        </Link>
        <Link
          href={`/advanced?leader=${encodeURIComponent(row.address)}`}
          className="btn btn-primary explore-follow-btn"
        >
          {c.follow}
        </Link>
      </div>
    </div>
  );
}
