"use client";
/**
 * `/explore` — 可跟單對象探索榜（M3 round3 Task 4，設計審查 R2·A；R4-3
 * 2026-08-30 改版：合併 chip 拆成四個自由數值門檻＋四期間窗全開）。
 *
 * 從舊版「鯨魚 PnL 榜」（`/leaderboard`，見 `hl_leaderboard.py`）重構而來：解決
 * 冷啟動——讓用戶找到「值得貼進進階模式的地址」。唯一資料源是
 * `GET /api/public/explore`（無需登入，`hl_explore.py`），排序（風險調整後報酬＝
 * 所選窗報酬率 ÷ 最大回撤）與資格過濾（樣本門檻／回撤上限／集中度上限）**全在
 * 後端完成**（R2-01）——本頁只送四個自由數值門檻＋所選期間窗，絕不自己排序或
 * 過濾一次已經拿到的 rows（否則分頁的 `total_qualified` 會跟畫面對不上）。
 *
 * 三態明確區分（同 `/leaderboard` 舊頁與 `PositionsTable.history` 的既有慣例）：
 * - `state==="error"`：fetch 本身失敗（連線／非 200／格式異常，見
 *   `getPublicExplore` 檔頭「不吞錯」設計）→ R2·C 態三，時間戳＋重試鍵。
 * - `state==="ready" && resp.building`：後端從未成功建置過 index（200，不是
 *   故障，見 `ExploreIndex.query` 檔頭）→ R2·C 態二，「建置中」文案。
 * - `state==="ready" && !resp.building && rows.length===0`：成功但零筆合格 → 空態。
 *
 * R4-3：四個門檻輸入框各自 debounce 500ms 再打 API（避免每個按鍵都發請求）；
 * 清空欄位＝不套用該條件——前端送邊界值（min 系欄位送 0、max 系欄位送 100，
 * 見 `hl_explore.clamp_explore_params` 檔頭「伺服器夾取」設計，這兩個邊界值
 * 本身天然等於「這個維度永遠通過」，不需要額外的 sentinel/None 概念）。四期間
 * 窗（`EXPLORE_WINDOWS`：day/week/month/allTime）全部可點，切換立即生效（不
 * debounce——這是離散選擇不是連續輸入）；候選池仍固定用 30D 表現選出，切窗
 * 只改變顯示與排序（`c.poolNote` 誠實揭露，見 `hl_explore.py` 檔頭「R4-3」節）。
 */
import Link from "next/link";
import { useEffect, useState } from "react";
import { NO_VALUE, fmtUpdatedAtUtc } from "@/lib/format";
import { useCopy } from "@/lib/lang";
import {
  EXPLORE_WINDOWS, getPublicExplore, type ExploreFilters, type ExploreResp,
  type ExploreRow, type ExploreWindow,
} from "@/lib/publicApi";

type LoadState = "loading" | "error" | "ready";

const SPARK_W = 96;
const SPARK_H = 28;
const FILTER_DEBOUNCE_MS = 500;

const DEFAULT_MIN_LIVE_DAYS = 30;
const DEFAULT_MIN_FILLS = 200;
const DEFAULT_MAX_DD_PCT = 30;
const DEFAULT_MAX_CONCENTRATION_PCT = 90;

const DEFAULT_FILTERS: ExploreFilters = {
  window: "month",
  minLiveDays: DEFAULT_MIN_LIVE_DAYS,
  minFills: DEFAULT_MIN_FILLS,
  maxDdPct: DEFAULT_MAX_DD_PCT,
  maxConcentrationPct: DEFAULT_MAX_CONCENTRATION_PCT,
};

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

/** 門檻輸入框文字 → 送給後端的數值：清空＝送邊界值（"不過濾"，見檔頭說明）；
 * 非數字（理論上 `type="number"` 擋得住，防禦性保留）→ 落回預設值，不送
 * `NaN`。 */
function parseThresholdInput(text: string, emptyValue: number, fallback: number): number {
  const trimmed = text.trim();
  if (trimmed === "") return emptyValue;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : fallback;
}

export default function ExplorePage() {
  const COPY = useCopy();
  const c = COPY.explore;

  // 四個門檻輸入框各自獨立的「使用者正在打字」文字狀態（允許暫時清空／打到一半
  // 的非終態），debounce 後才轉成數值寫進 `filters`（實際打 API 的來源）。
  const [minLiveDaysText, setMinLiveDaysText] = useState(String(DEFAULT_MIN_LIVE_DAYS));
  const [minFillsText, setMinFillsText] = useState(String(DEFAULT_MIN_FILLS));
  const [maxDdPctText, setMaxDdPctText] = useState(String(DEFAULT_MAX_DD_PCT));
  const [maxConcentrationPctText, setMaxConcentrationPctText] =
    useState(String(DEFAULT_MAX_CONCENTRATION_PCT));

  const [filters, setFilters] = useState<ExploreFilters>(DEFAULT_FILTERS);
  const [page, setPage] = useState(1);
  const [reloadKey, setReloadKey] = useState(0);

  const [state, setState] = useState<LoadState>("loading");
  const [resp, setResp] = useState<ExploreResp | null>(null);
  const [errorAt, setErrorAt] = useState<number | null>(null);

  // R4-3：四個門檻文字框 debounce 500ms 後才提交成數值門檻並回第一頁——切換
  // 期間窗（見 `handleWindowChange`）不經過這條路徑，是離散選擇立即生效。
  useEffect(() => {
    const t = setTimeout(() => {
      setFilters((prev) => ({
        ...prev,
        minLiveDays: parseThresholdInput(minLiveDaysText, 0, DEFAULT_MIN_LIVE_DAYS),
        minFills: parseThresholdInput(minFillsText, 0, DEFAULT_MIN_FILLS),
        maxDdPct: parseThresholdInput(maxDdPctText, 100, DEFAULT_MAX_DD_PCT),
        maxConcentrationPct: parseThresholdInput(
          maxConcentrationPctText, 100, DEFAULT_MAX_CONCENTRATION_PCT,
        ),
      }));
      setPage(1);
    }, FILTER_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [minLiveDaysText, minFillsText, maxDdPctText, maxConcentrationPctText]);

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
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, filters, reloadKey]);

  function handleWindowChange(window: ExploreWindow) {
    setFilters((prev) => ({ ...prev, window }));
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
        <div className="explore-window-group" role="group" aria-label={c.windows.month}>
          {EXPLORE_WINDOWS.map((w) => (
            <button
              key={w}
              type="button"
              className="explore-window-btn"
              data-active={filters.window === w}
              aria-pressed={filters.window === w}
              onClick={() => handleWindowChange(w)}
            >
              {c.windows[w]}
            </button>
          ))}
        </div>
        <span className="explore-count mono">
          {c.countPrefix}
          {(resp?.total_scanned ?? 0).toLocaleString()}
          {c.countMid}
          {resp?.total_qualified ?? 0}
          {c.countSuffix}
        </span>
      </div>

      <div className="explore-filters">
        <label className="explore-filter-item">
          <span className="explore-filter-label">{c.filters.minLiveDaysLabel}</span>
          <input
            type="number"
            inputMode="numeric"
            min={0}
            max={365}
            className="explore-filter-input"
            value={minLiveDaysText}
            onChange={(e) => setMinLiveDaysText(e.target.value)}
            title={c.filters.clearToDisable}
          />
        </label>
        <label className="explore-filter-item">
          <span className="explore-filter-label">{c.filters.minFillsLabel}</span>
          <input
            type="number"
            inputMode="numeric"
            min={0}
            max={100000}
            className="explore-filter-input"
            value={minFillsText}
            onChange={(e) => setMinFillsText(e.target.value)}
            title={c.filters.clearToDisable}
          />
        </label>
        <label className="explore-filter-item">
          <span className="explore-filter-label">{c.filters.maxDdPctLabel}</span>
          <input
            type="number"
            inputMode="numeric"
            min={1}
            max={100}
            className="explore-filter-input"
            value={maxDdPctText}
            onChange={(e) => setMaxDdPctText(e.target.value)}
            title={c.filters.clearToDisable}
          />
        </label>
        <label className="explore-filter-item">
          <span className="explore-filter-label">{c.filters.maxConcentrationPctLabel}</span>
          <input
            type="number"
            inputMode="numeric"
            min={1}
            max={100}
            className="explore-filter-input"
            value={maxConcentrationPctText}
            onChange={(e) => setMaxConcentrationPctText(e.target.value)}
            title={c.filters.clearToDisable}
          />
        </label>
      </div>
      <p className="hint explore-pool-note">{c.poolNote}</p>

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
              <ExploreRowView
                key={row.address}
                row={row}
                rank={(resp.page - 1) * resp.page_size + i + 1}
                window={filters.window}
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
  { row, rank, window }: { row: ExploreRow; rank: number; window: ExploreWindow },
) {
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
  // R4-3：`windows[window]` 對這一列可能是 `null`（day/week best-effort 缺席，
  // 見 `hl_explore.py` 檔頭「R4-3」節）——誠實顯示「—」，不回退借用其他窗的
  // 數字冒充。
  const winStats = row.windows[window];

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
            points={sparkPoints(winStats?.spark ?? [])}
            fill="none"
            stroke={winStats != null && winStats.ret_pct >= 0 ? "var(--pos)" : "var(--neg)"}
            strokeWidth="1.4"
          />
        </svg>
      </div>
      <div className={`mono explore-ret ${winStats != null && winStats.ret_pct >= 0 ? "pos" : "neg"}`}>
        {winStats != null ? fmtSignedPct(winStats.ret_pct) : NO_VALUE}
      </div>
      <div className="mono neg explore-dd">
        {winStats != null ? `${winStats.max_dd_pct.toFixed(1)}%` : NO_VALUE}
      </div>
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
