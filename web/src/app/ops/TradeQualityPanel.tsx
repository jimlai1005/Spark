"use client";
/**
 * /ops 成交品質面板（管理端）。
 *
 * ⭐ 本區塊的誠信要求高於一般表格，因為它的每一格都有「看起來正常的錯誤值」：
 * - 配對延遲填 0 讀起來是完美的跟單品質，實際上是「我們根本不知道要跟誰比」。
 * - 跳過小額填 0 讀起來是「引擎沒在跳過客戶的單」，正好是這個指標要抓的問題的反面。
 * - 跳過小額**佔比**在非整個 UTC 日的窗口下分子分母不同基準（分子按日曆日落檔、
 *   分母依窗口過濾），後端刻意回 null 而不硬算——那個商看起來完全像一個正常的比例。
 * 所以三種 null 在畫面上是**三種不同的字**（無法配對／讀不到／此窗口無法計算），
 * 而不是同一個「—」：把它們混成一格，等於把「不知道」與「算不出來」講成同一件事。
 */
import type {
  OpsTradeQualityResp,
  OpsTradeQualityRow,
  OpsTradeQualitySummary,
} from "@/lib/api";
import { COPY_ZH as COPY } from "@/lib/copy";
import { fmtAmount, fmtRatioPct, NO_VALUE } from "@/lib/format";

const t = COPY.ops.tradeQuality;

export function TradeQualityBlock({ data }: { data: OpsTradeQualityResp }) {
  // ⭐ 判別欄位先擋：窗口對不齊時整塊不出現任何數字（沿 RevenueBlock／CustomersBlock
  // 的 basis_unknown 分支，同樣的嚴格度）。後端在這個分支不給 followers 也不給
  // summary——顯示層在型別上就畫不出空表（空表會被讀成「今天成交品質完美」）。
  if (data.basis_unknown) {
    return (
      <div className="panel ops-notice">
        <p className="ops-notice-title">{t.basisUnknown}</p>
        <p className="hint">{data.note}</p>
        <p className="hint">{t.basisUnknownNote}</p>
      </div>
    );
  }

  const rows = data.followers ?? [];
  return (
    <>
      <div className="panel">
        <p className="ops-subtitle">{t.summaryTitle}</p>
        <p className="hint">{t.summaryNote}</p>
        <SummaryStats s={data.summary} />
        {/* ⭐ 比例的分母基準攤在畫面上：讀者要能自己判斷「這個窗口到底是不是整日」，
            而不是只讀到一句「無法計算」卻不知道為什麼。 */}
        <p className="hint mono ops-window">
          {t.skippedDaysLabel}: {data.skipped_days.join(" / ") || NO_VALUE}
        </p>
        <p className="hint mono ops-window">
          {t.window}: {data.window_start} → {data.window_end}
        </p>
      </div>
      {rows.length === 0 ? (
        <p>{t.empty}</p>
      ) : (
        <div className="panel">
          <table className="admin-table ops-table">
            <thead>
              <tr>
                <th scope="col">{t.cols.account}</th>
                <th scope="col">{t.cols.fills}</th>
                <th scope="col">{t.cols.takerShare}</th>
                <th scope="col">{t.cols.pairCount}</th>
                <th scope="col">{t.cols.medianDelay}</th>
                <th scope="col">{t.cols.slippage}</th>
                <th scope="col">{t.cols.skippedNotional}</th>
                <th scope="col">{t.cols.skippedRatio}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => <QualityRow key={row.account_id} row={row} />)}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function SummaryStats({ s }: { s: OpsTradeQualitySummary }) {
  return (
    <dl className="ops-stats">
      <QStat label={t.stats.followers} value={String(s.followers)} />
      <QStat label={t.stats.qualityAvailable} value={String(s.quality_available_count)} />
      <QStat label={t.stats.teAvailable} value={String(s.te_available_count)} />
      <QStat label={t.stats.skippedAvailable} value={String(s.skipped_available_count)} />
      {/* ⭐ 最差值與樣本數成對出現：拆開任一個，另一個就會被誤讀。 */}
      <QStat label={t.stats.worstDelay} value={s.worst_median_delay_s ?? NO_VALUE} />
      <QStat label={t.stats.delaySample} value={String(s.delay_sample)} />
      <QStat
        label={t.stats.worstSlippage}
        value={s.worst_taker_slippage_bp_median ?? NO_VALUE}
      />
      <QStat label={t.stats.slippageSample} value={String(s.slippage_sample)} />
    </dl>
  );
}

function QStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="ops-stat">
      <dt>{label}</dt>
      <dd className="mono">{value}</dd>
    </div>
  );
}

/**
 * 單列。⭐ 三種不可得各有各的字，且都附 title 說明「為什麼這一格不是數字」：
 * 一個沒有解釋的「—」會被當成「這個客戶沒事」，而它其實常常是「我們看不到」。
 */
function QualityRow({ row }: { row: OpsTradeQualityRow }) {
  // 自己的 fills 都查不到 ⇒ 這一列的每一個量都無從計算。後端在這個分支不給任何
  // 量測欄（型別上讀不到），此處整列標未知並附原文（跨客戶隔離，其餘列照常）。
  if (!row.quality_available) {
    return (
      <>
        <tr className="ops-row-failed">
          <td className="mono">{row.account_id}</td>
          <td className="mono" colSpan={7}>{NO_VALUE}</td>
        </tr>
        <tr className="ops-row-failed">
          <td colSpan={8} className="ops-row-error">
            <span className="ops-row-error-label">{t.rowError}</span>
            <span className="mono">{row.error}</span>
            <span className="hint"> {t.rowErrorHint}</span>
          </td>
        </tr>
      </>
    );
  }

  // TE（配對延遲）與滑價需要 leader 的成交才配得起來；不知道跟誰時一律「無法配對」。
  const te = (v: string | number | null) =>
    row.te_available && v != null
      ? <span className="mono">{String(v)}</span>
      : <span className="ops-unknown" title={row.te_note ?? t.teUnavailableHint}>
          {t.teUnavailable}
        </span>;

  return (
    <>
      <tr>
        <td className="mono">{row.account_id}</td>
        <td className="mono">{row.fills}</td>
        <td className="mono">{fmtRatioPct(row.taker_share)}</td>
        <td>{te(row.pair_count)}</td>
        <td>{te(row.median_delay_s)}</td>
        <td>{te(row.taker_slippage_bp_median)}</td>
        <td>
          {row.skipped_available ? (
            <span className="mono" title={row.skipped_small_notional ?? undefined}>
              {fmtAmount(row.skipped_small_notional)}
            </span>
          ) : (
            <span className="ops-unknown" title={t.skippedUnavailableHint}>
              {t.skippedUnavailable}
            </span>
          )}
        </td>
        <td><SkippedRatio row={row} /></td>
      </tr>
      {row.error && (
        <tr className="ops-row-failed">
          <td colSpan={8} className="ops-row-error">
            <span className="ops-row-error-label">{t.rowError}</span>
            <span className="mono">{row.error}</span>
          </td>
        </tr>
      )}
    </>
  );
}

/**
 * ⭐⭐ 跳過小額佔比。**兩種 null 意義完全不同，必須分開講**：
 * - `skipped_available=false`＝記錄檔讀不到（或多日窗只有部分天有檔）。
 * - `skipped_available=true` 且比例為 null＝窗口非整個 UTC 日，分子（日曆日落檔）
 *   與分母（依窗口過濾的成交名目）不同基準，後端刻意不硬算（工程原則 1）。
 * 兩者都**絕不**顯示成 0，也絕不留白——留白在一欄數字裡會被讀成 0。
 */
function SkippedRatio({ row }: { row: Extract<OpsTradeQualityRow, { quality_available: true }> }) {
  if (!row.skipped_available) {
    return (
      <span className="ops-unknown" title={t.skippedUnavailableHint}>
        {t.skippedUnavailable}
      </span>
    );
  }
  if (row.skipped_small_ratio == null) {
    return (
      <span className="ops-unknown" title={row.skipped_note ?? t.ratioIncomparableHint}>
        {t.ratioIncomparable}
      </span>
    );
  }
  return <span className="mono">{fmtRatioPct(row.skipped_small_ratio)}</span>;
}
