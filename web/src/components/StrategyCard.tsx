"use client";
import Link from "next/link";
import { fmtAmount } from "@/lib/format";
import { useCopy } from "@/lib/lang";
import type { PublicStrategy } from "@/lib/publicApi";

interface CardMetric {
  key: string;
  label: string;
  value: string | null;
  insufficient: boolean;
  suffix?: string;
}

/**
 * StrategyCard — 策略卡（Task 8，設計稿 §03「策略區」＋ Task 9 策略列表頁復用）。
 *
 * `listable=false`（未達 60 天上架門檻或例行下架）→ disabled 態：不渲染任何可跟單
 * CTA 連結，改顯示「樣本累積中」badge＋說明文字（NOTE 04：上架門檻是程式邏輯，
 * 不是前端自己判斷——這裡完全信任後端算好的 `listable` 旗標）。
 *
 * **CAGR 不出現在這張卡上**（NOTE 03）：只呈現 API 提供的 total_return / max_drawdown /
 * sharpe(±se) / win_rate 四格，沒有任何年化外推欄位。
 *
 * `summary`（Task 19 修正）：onboarding step1（`StepSelectStrategy`）已選卡復用本
 * 元件顯示摘要，不該再出現「查看策略與風險」CTA——傳 `summary` 時整個底部 CTA
 * 區塊（含 listable=false 的樣本累積中按鈕）都不渲染。
 */
export function StrategyCard({ strategy, summary }: { strategy: PublicStrategy; summary?: boolean }) {
  const COPY = useCopy();
  const c = COPY.home.strategies;
  const m = strategy.metrics;

  const metrics: CardMetric[] = [
    {
      key: "total_return",
      label: c.metricTotalReturn,
      value: m.total_return_pct,
      insufficient: m.total_return_pct_insufficient,
      suffix: "%",
    },
    {
      key: "max_drawdown",
      label: c.metricMaxDrawdown,
      value: m.max_drawdown_pct,
      insufficient: m.max_drawdown_pct_insufficient,
      suffix: "%",
    },
    {
      key: "sharpe",
      label: c.metricSharpe,
      value: m.sharpe,
      insufficient: m.sharpe_insufficient,
    },
    {
      key: "win_rate",
      label: c.metricWinRate,
      value: m.win_rate_pct,
      insufficient: m.win_rate_pct_insufficient,
      suffix: "%",
    },
  ];

  return (
    <div className="card strategy-card" data-slug={strategy.slug} data-listable={strategy.listable}>
      {strategy.featured && <div className="strategy-card-accent" aria-hidden="true" />}
      <div className="strategy-card-head">
        <div>
          <div className="strategy-card-name">{strategy.name}</div>
          {strategy.tagline && <div className="strategy-card-tagline">{strategy.tagline}</div>}
        </div>
        {strategy.featured ? (
          <span className="pill chip-featured">{c.featuredBadge}</span>
        ) : !strategy.listable ? (
          <span className="pill chip-pending">{c.pendingBadge}</span>
        ) : null}
      </div>

      <div className="strategy-card-metrics">
        {metrics.map((metric) => (
          <div className="inset strategy-metric" key={metric.key}>
            <div className="strategy-metric-label">{metric.label}</div>
            <div className="strategy-metric-value mono">
              {metric.insufficient ? (
                c.insufficientLabel
              ) : (
                <>
                  {metric.value}
                  {metric.suffix ?? ""}
                  {metric.key === "sharpe" && !m.sharpe_se_insufficient && m.sharpe_se != null && (
                    <span className="strategy-metric-se"> ±{m.sharpe_se}</span>
                  )}
                </>
              )}
            </div>
          </div>
        ))}
      </div>

      <div className="strategy-card-chips">
        {strategy.max_leverage && (
          <span className="pill chip">
            {c.leveragePrefix}
            {strategy.max_leverage}x
          </span>
        )}
        {strategy.min_notional_usd && (
          <span className="pill chip">
            {c.minNotionalPrefix}
            {fmtAmount(strategy.min_notional_usd, 0)}
          </span>
        )}
      </div>

      {summary ? null : strategy.listable ? (
        <Link href={`/strategies/${strategy.slug}`} className="btn btn-primary btn-block">
          {c.cta}
        </Link>
      ) : (
        <>
          <p className="strategy-card-note">{c.pendingNote}</p>
          <button type="button" className="btn btn-block" disabled data-testid="strategy-card-disabled">
            {c.pendingBadge}
          </button>
        </>
      )}
    </div>
  );
}
