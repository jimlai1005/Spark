"use client";
/**
 * `/explore` — 可跟單對象探索榜（M3 round3 Task 4，設計審查 R2·A；R4-10
 * 2026-08-31 使用者裁決：保留 chip UI，(a) 期間 chip 從固定 30D 改四窗全開
 * （1D/7D/30D/全部），(b) 原「樣本門檻」合併 chip 拆成兩顆獨立布林 chip
 * （實盤天數／成交筆數）。R4-3 那版自由填數字輸入框已被 revert（commit
 * 061433a，使用者不喜歡輸入框）——本輪**不**重加輸入框，四個門檻維持布林
 * chip 開關 + 後端固定數值（見下方 `*_THRESHOLD` 常數），只是從三顆 chip
 * 拆成四顆。
 *
 * 從舊版「鯨魚 PnL 榜」（`/leaderboard`，見 `hl_leaderboard.py`）重構而來：解決
 * 冷啟動——讓用戶找到「值得貼進進階模式的地址」。唯一資料源是
 * `GET /api/public/explore`（無需登入，`hl_explore.py`），排序（風險調整後報酬＝
 * 所選窗報酬率 ÷ 最大回撤）與資格過濾（樣本門檻／回撤上限／集中度上限）**全在
 * 後端完成**（R2-01）——本頁四顆 chip 只送布林開關對映的固定數值，絕不自己
 * 排序或過濾一次已經拿到的 rows（否則分頁的 `total_qualified` 會跟畫面對不上）。
 *
 * 三態明確區分（同 `/leaderboard` 舊頁與 `PositionsTable.history` 的既有慣例）：
 * - `state==="error"`：fetch 本身失敗（連線／非 200／格式異常，見
 *   `getPublicExplore` 檔頭「不吞錯」設計）→ R2·C 態三，時間戳＋重試鍵。
 * - `state==="ready" && resp.building`：後端從未成功建置過 index（200，不是
 *   故障，見 `ExploreIndex.query` 檔頭）→ R2·C 態二，「建置中」文案。
 * - `state==="ready" && !resp.building && rows.length===0`：成功但零筆合格 → 空態。
 *
 * 四窗（day/week/month/allTime，UI 標籤 1D/7D/30D/全部）全部可點、切換立即
 * 生效（不 debounce——離散選擇，不是連續輸入）；候選池仍固定用後端 stats-data
 * month 窗 roi 選出，切窗只改變顯示與排序（見 `hl_explore.py` 檔頭「R4-3」節）。
 * 表格數字讀 `row.windows[所選窗]`——該窗對這一列缺席（day/week best-effort）
 * → 誠實顯示「—」，不得回退借用其他窗的數字冒充（工程原則：不編數字）。
 */
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, type Dispatch, type SetStateAction, useEffect, useState } from "react";
import { NO_VALUE, fmtSignedUsd, fmtUpdatedAtUtc } from "@/lib/format";
import { useCopy } from "@/lib/lang";
import {
  EXPLORE_ORDERS, EXPLORE_SORT_FIELDS, EXPLORE_WINDOWS, getPublicExplore,
  type ExploreFilters, type ExploreOrder, type ExploreResp, type ExploreRow,
  type ExploreSort, type ExploreWindow,
} from "@/lib/publicApi";

type LoadState = "loading" | "error" | "ready";

const SPARK_W = 96;
const SPARK_H = 28;

// R4-10：四顆布林 chip → 後端固定數值門檻的映射常數（集中於此一處）。
// on → 套用門檻；off → 送邊界值（不過濾，見 `hl_explore.clamp_explore_params`
// 檔頭「伺服器夾取」設計：min 系送 0、max 系送 100 天然等於「這個維度永遠
// 通過」）。門檻數字固定，不提供自由填寫（使用者裁決：不喜歡輸入框）。
const LIVE_DAYS_THRESHOLD = 30;
const FILLS_THRESHOLD = 200;
const MAX_DD_PCT_THRESHOLD = 30;
const MAX_CONCENTRATION_PCT_THRESHOLD = 90;
const DEFAULT_WINDOW: ExploreWindow = "month";
// Task 12（2026-09-05，D11–D13）：與後端 `hl_explore.DEFAULT_SORT`／`DEFAULT_ORDER`
// 同值（`src/spark/publicapi/hl_explore.py:285-286`）。
const DEFAULT_SORT: ExploreSort = "pnl";
const DEFAULT_ORDER: ExploreOrder = "desc";

/** D11：query string → 各狀態的解析 helper，全部有 fallback（非法／缺席值不炸，
 * 退回既有預設，讓「使用者手改網址」與「舊書籤缺新鍵」都安全）。 */
function parseBoolFlag(v: string | null, fallback: boolean): boolean {
  if (v === "1") return true;
  if (v === "0") return false;
  return fallback;
}

function parsePositiveIntParam(v: string | null, fallback: number): number {
  const n = v == null ? Number.NaN : Number(v);
  return Number.isInteger(n) && n >= 1 ? n : fallback;
}

function parseWindowParam(v: string | null): ExploreWindow {
  return v != null && (EXPLORE_WINDOWS as readonly string[]).includes(v)
    ? (v as ExploreWindow)
    : DEFAULT_WINDOW;
}

function parseSortParam(v: string | null): ExploreSort {
  return v != null && (EXPLORE_SORT_FIELDS as readonly string[]).includes(v)
    ? (v as ExploreSort)
    : DEFAULT_SORT;
}

function parseOrderParam(v: string | null): ExploreOrder {
  return v != null && (EXPLORE_ORDERS as readonly string[]).includes(v)
    ? (v as ExploreOrder)
    : DEFAULT_ORDER;
}

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

function fmtPct1(n: number | null): string {
  return n == null ? NO_VALUE : `${n.toFixed(1)}%`;
}

/** D13（2026-09-05，Task 12）：表頭排序按鈕。作用中的欄在文字後加箭頭
 * （`▼` desc／`▲` asc，`aria-hidden`——不進 accessible name，避免
 * `getByRole("button", {name})` 因箭頭字元而配不上純文案）。 */
function SortableTh({
  field, label, sort, order, hint, onSort,
}: {
  field: ExploreSort;
  label: string;
  sort: ExploreSort;
  order: ExploreOrder;
  hint: string;
  onSort: (field: ExploreSort) => void;
}) {
  const active = sort === field;
  return (
    <button
      type="button"
      className="explore-sort-btn"
      data-active={active}
      aria-sort={active ? (order === "desc" ? "descending" : "ascending") : "none"}
      title={hint}
      onClick={() => onSort(field)}
    >
      {label}
      {active && <span aria-hidden="true">{order === "desc" ? "▼" : "▲"}</span>}
    </button>
  );
}

/** 分頁按鈕：目前頁 ± `span`，並夾在 `[1, total]` 範圍內（不做省略號，`total`
 * 一般不大——`page_size` 預設 25，`ExploreConfig.candidate_pool` 預設 300
 * 時最多約 12 頁，實際頁數以合格列數 `total_qualified` 為準）。 */
function pageWindow(current: number, total: number, span = 2): number[] {
  const start = Math.max(1, current - span);
  const end = Math.min(total, current + span);
  return Array.from({ length: Math.max(0, end - start + 1) }, (_, i) => start + i);
}

/** `useSearchParams()` 在 build 期 prerender 需要 Suspense 邊界（沿
 * `traders/[address]/page.tsx:73-83` 既有寫法）。頁面本體在 `ExploreInner`。 */
export default function ExplorePage() {
  return (
    <Suspense fallback={null}>
      <ExploreInner />
    </Suspense>
  );
}

function ExploreInner() {
  const COPY = useCopy();
  const c = COPY.explore;
  const searchParams = useSearchParams();

  // D11（2026-09-05，Task 12）：filter／window／page／sort／order 全部放 URL
  // query，初始值從 `useSearchParams` 讀（返回頁面時 Next 重新掛載，讀到 query
  // 即還原——不用 sessionStorage，URL 可分享／可書籤）。
  const [window_, setWindow] = useState<ExploreWindow>(() => parseWindowParam(searchParams.get("window")));
  const [liveDaysChip, setLiveDaysChip] = useState(() => parseBoolFlag(searchParams.get("ld"), true));
  const [fillsChip, setFillsChip] = useState(() => parseBoolFlag(searchParams.get("fills"), true));
  // ⭐ 回撤/集中度 chip 預設關閉（2026-08-31 主線程裁決）：候選池本就是 top-ROI
  // 帳戶，30D 回撤 ≤30% 的閘門會把預設榜刷成空的（實測 month 窗 24→0）——落地頁
  // 空榜比寬鬆預設更糟。品質基線（實盤天數/成交筆數）維持預設開。
  const [maxDdChip, setMaxDdChip] = useState(() => parseBoolFlag(searchParams.get("dd"), false));
  const [concentratedChip, setConcentratedChip] = useState(() => parseBoolFlag(searchParams.get("conc"), false));
  const [page, setPage] = useState(() => parsePositiveIntParam(searchParams.get("page"), 1));
  const [sort, setSort] = useState<ExploreSort>(() => parseSortParam(searchParams.get("sort")));
  const [order, setOrder] = useState<ExploreOrder>(() => parseOrderParam(searchParams.get("order")));
  const [reloadKey, setReloadKey] = useState(0);

  const [state, setState] = useState<LoadState>("loading");
  const [resp, setResp] = useState<ExploreResp | null>(null);
  const [errorAt, setErrorAt] = useState<number | null>(null);

  const filters: ExploreFilters = {
    window: window_,
    minLiveDays: liveDaysChip ? LIVE_DAYS_THRESHOLD : 0,
    minFills: fillsChip ? FILLS_THRESHOLD : 0,
    maxDdPct: maxDdChip ? MAX_DD_PCT_THRESHOLD : 100,
    maxConcentrationPct: concentratedChip ? MAX_CONCENTRATION_PCT_THRESHOLD : 100,
    sort,
    order,
  };

  useEffect(() => {
    let cancelled = false;
    setState("loading");
    getPublicExplore(page, filters)
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

    // D11：每次任一狀態變動都把全部狀態寫回 URL（含預設值，讓 URL 自描述），
    // 用 `replaceState` 不進歷史堆疊、不用 Next router（純顯示狀態同步，同
    // `traders/[address]/page.tsx:110-126` 既有寫法與理由）。
    if (typeof window !== "undefined") {
      const url = new URL(window.location.href);
      url.searchParams.set("window", window_);
      url.searchParams.set("ld", liveDaysChip ? "1" : "0");
      url.searchParams.set("fills", fillsChip ? "1" : "0");
      url.searchParams.set("dd", maxDdChip ? "1" : "0");
      url.searchParams.set("conc", concentratedChip ? "1" : "0");
      url.searchParams.set("page", String(page));
      url.searchParams.set("sort", sort);
      url.searchParams.set("order", order);
      window.history.replaceState(null, "", url.toString());
    }

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, window_, liveDaysChip, fillsChip, maxDdChip, concentratedChip, sort, order, reloadKey]);

  function toggleChip(setter: Dispatch<SetStateAction<boolean>>) {
    setter((v) => !v);
    setPage(1);
  }

  function handleWindowChange(w: ExploreWindow) {
    setWindow(w);
    setPage(1);
  }

  // D13：第一次點某欄＝desc；再點同一欄＝翻轉；切換排序回第一頁（分頁在後端切，
  // 換排序後舊頁碼對不上新順序）。
  function handleSortClick(field: ExploreSort) {
    if (sort === field) {
      setOrder((o) => (o === "desc" ? "asc" : "desc"));
    } else {
      setSort(field);
      setOrder("desc");
    }
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
          {/* I-17（2026-08-31 使用者裁決）：榜首常駐一行，說明候選池與上榜數的
              落差來自鏈上資料缺席（帳戶太新／抓取失敗／期間資料無效），不是
              篩選條件太嚴——數字（`pool`／`total_qualified`）一律來自後端回應，
              不寫死候選池上限常數。與「資料更新於」同一個顯示條件（成功且非
              building 態才有意義的數字）。 */}
          {showUpdatedAt && resp && (
            <p className="hint explore-pool-note">
              {c.poolNotePrefix}
              {resp.pool}
              {c.poolNoteMid}
              {resp.total_qualified}
              {c.poolNoteSuffix}
            </p>
          )}
          {/* 2026-09-05（explore/trader 指標統一 plan Task 5）：回撤定義揭露——
              權益指數 MDD 與交易所／第三方工具的原始淨值 MDD 不同，靜態文案，
              不依賴資料載入狀態。 */}
          <p className="hint explore-dd-definition">{c.ddDefinition}</p>
        </div>
        <div className="explore-disclaimer-badge">{c.disclaimerBadge}</div>
      </header>

      <div className="explore-chips">
        <div className="explore-window-group" role="group" aria-label={c.windows.month}>
          {EXPLORE_WINDOWS.map((w) => (
            <button
              key={w}
              type="button"
              className="explore-window-btn"
              data-active={window_ === w}
              aria-pressed={window_ === w}
              onClick={() => handleWindowChange(w)}
            >
              {c.windows[w]}
            </button>
          ))}
        </div>
        <button
          type="button"
          className="explore-chip"
          data-active={liveDaysChip}
          aria-pressed={liveDaysChip}
          onClick={() => toggleChip(setLiveDaysChip)}
        >
          {c.filters.liveDays}
        </button>
        <button
          type="button"
          className="explore-chip"
          data-active={fillsChip}
          aria-pressed={fillsChip}
          onClick={() => toggleChip(setFillsChip)}
        >
          {c.filters.fills}
        </button>
        <button
          type="button"
          className="explore-chip"
          data-active={maxDdChip}
          aria-pressed={maxDdChip}
          onClick={() => toggleChip(setMaxDdChip)}
        >
          {c.filters.maxDd}
        </button>
        <button
          type="button"
          className="explore-chip"
          data-active={concentratedChip}
          aria-pressed={concentratedChip}
          onClick={() => toggleChip(setConcentratedChip)}
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

      {/* ⭐ Task 9 Step 5（reviewer W5）：後端回撤過濾對「算不出回撤」的帳戶
          不過濾（`hl_explore.qualify`「沒有證據代表回撤超標」慣例），chip 開啟
          時提示這件事，否則使用者以為榜單裡不會再出現回撤欄「—」的列。 */}
      {maxDdChip && <p className="hint explore-dd-filter-note">{c.ddFilterNoEvidenceNote}</p>}

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
              <div>
                <SortableTh
                  field="pnl" label={c.table.pnl} sort={sort} order={order}
                  hint={c.table.sortHint} onSort={handleSortClick}
                />
              </div>
              <div>
                <SortableTh
                  field="max_dd" label={c.table.dd} sort={sort} order={order}
                  hint={c.table.sortHint} onSort={handleSortClick}
                />
              </div>
              <div>
                <SortableTh
                  field="live_days" label={c.table.days} sort={sort} order={order}
                  hint={c.table.sortHint} onSort={handleSortClick}
                />
              </div>
              <div>
                <SortableTh
                  field="win_rate" label={c.table.winRate} sort={sort} order={order}
                  hint={c.table.sortHint} onSort={handleSortClick}
                />
              </div>
              <div>{c.table.exposure}</div>
              <div>{c.table.actions}</div>
            </div>
            {resp.rows.map((row, i) => (
              <ExploreRowView
                key={row.address}
                row={row}
                window={window_}
                rank={(resp.page - 1) * resp.page_size + i + 1}
              />
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

function ExploreRowView(
  { row, window: windowKey, rank }: { row: ExploreRow; window: ExploreWindow; rank: number },
) {
  const COPY = useCopy();
  const c = COPY.explore;
  const [copied, setCopied] = useState(false);
  // R4-10：所選窗對這一列缺席（day/week best-effort，見 hl_explore.py 檔頭
  // 「R4-3」節）→ `stats` 為 `null`，下方各儲存格誠實顯示「—」，不回退借用
  // 其他窗的數字冒充。
  const stats = row.windows[windowKey];

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
          {/* D14（2026-09-05，Task 12）：地址 label 點進詳情頁，與「查看」同目標
              （帶所選窗過去，詳情頁預設就落在同一窗，D10）；複製按鈕維持獨立
              元素，不包在 Link 內。 */}
          <Link href={`/traders/${row.address}?window=${windowKey}`} className="mono explore-address-link"
                target="_blank" rel="noopener noreferrer">
            {row.label}
          </Link>
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
            points={sparkPoints(stats?.spark ?? [])}
            fill="none"
            stroke={(stats?.pnl_usd ?? 0) >= 0 ? "var(--pos)" : "var(--neg)"}
            strokeWidth="1.4"
          />
        </svg>
      </div>
      <div className={`mono explore-ret ${stats == null ? "" : stats.pnl_usd >= 0 ? "pos" : "neg"}`}>
        {stats == null ? NO_VALUE : fmtSignedUsd(stats.pnl_usd)}
      </div>
      {/* max_dd_pct 可能為 null（算不出，見 max_dd_reason）——與「該窗整列缺席」
          （stats == null）是不同語意，兩者都顯示 c.table.ddUnavailable，但算不出時
          額外帶 title 說明原因；不得用 ?? / || 把 null 換成 0（那是偽造回撤為零）。 */}
      <div className="mono neg explore-dd">
        {stats == null || stats.max_dd_pct == null
          ? <span title={c.table.ddUnavailableTitle}>{c.table.ddUnavailable}</span>
          : `${stats.max_dd_pct.toFixed(1)}%`}
      </div>
      <div className="mono explore-days">{row.live_days}</div>
      <div className="mono explore-wr">{fmtPct1(row.close_win_rate_pct)}</div>
      <div className="explore-exposure">
        {/* 無持倉（dir null）不畫 bar：bar 底色是空方紅，畫出來會像 100% 空單 */}
        {row.exposure.dir != null && (
          <div className="explore-exposure-bar">
            <div className="explore-exposure-fill" style={{ width: `${longPct}%` }} />
          </div>
        )}
        <span className="mono explore-exposure-label">{exposureLabel}</span>
      </div>
      <div className="explore-actions">
        {/* 2026-09-05（Task 6）：帶所選窗過去，詳情頁預設就是同一窗（D10）。 */}
        {/* 2026-09-05 使用者要求：詳情頁一律新分頁開啟（清單的 filter／排序狀態留在原分頁） */}
        <Link href={`/traders/${row.address}?window=${windowKey}`} className="btn btn-ghost explore-view-btn"
              target="_blank" rel="noopener noreferrer">
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
