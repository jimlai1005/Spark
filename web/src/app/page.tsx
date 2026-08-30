"use client";
/**
 * `/` — 首頁（策略優先 + 證據層，Task 8，設計稿 §03）。
 *
 * ⭐⭐ 2026-08-28 大改版：舊版首頁是 SIWE 登入頁（連結錢包→簽署→導向 /onboarding）。
 * 顧問 P1/P2 核心是「第一屏不再要求任何簽名」（NOTE 01）——本頁**完全不 import
 * wagmi**，沒有任何錢包連線按鈕。順序：hero（零錢包 CTA）→ 證據列（接
 * `/api/public/stats`）→ 可跟單策略（接 `/api/public/strategies`）→ 授權能力矩陣
 * （`#security` 錨點）→ 費用試算 → 開始跟單四步驟（`#how` 錨點）→ Footer（Task 7，
 * 掛在 layout.tsx，不在本頁渲染）。
 *
 * 舊版的 SIWE 連線流程（`useConnect`/`useSignMessage`/`loginWithSiwe`）將在
 * Task 9 搬到策略詳情頁的跟單面板 CTA；本頁不再擁有登入職責。
 */
import Link from "next/link";
import { useEffect, useState } from "react";
import { CapabilityMatrix } from "@/components/CapabilityMatrix";
import { FeeCalculator } from "@/components/FeeCalculator";
import { StrategyCard } from "@/components/StrategyCard";
import { FOLLOWER_COUNT_DISPLAY_MIN } from "@/lib/copy";
import { fmtUsdCompact, NO_VALUE, resolveTagline, shortAddr } from "@/lib/format";
import { useCopy, useLang } from "@/lib/lang";
import {
  getPublicStats,
  getPublicStrategies,
  type PublicStats,
  type PublicStrategy,
} from "@/lib/publicApi";

const EMPTY_STATS: PublicStats = {
  routed_volume_usd_total: null,
  builder_fee_bps: null,
  live_days: null,
  updated_at: 0,
};

function pickFeatured(strategies: PublicStrategy[]): PublicStrategy | null {
  if (strategies.length === 0) return null;
  return strategies.find((s) => s.featured) ?? strategies[0];
}

/** metrics 顯示：insufficient → NO_VALUE；否則附上尾綴（例如 %）。 */
function metricText(value: string | null, insufficient: boolean, suffix = ""): string {
  if (insufficient || value == null) return NO_VALUE;
  return `${value}${suffix}`;
}

export default function HomePage() {
  const COPY = useCopy();
  const { lang } = useLang();
  const home = COPY.home;

  const [stats, setStats] = useState<PublicStats>(EMPTY_STATS);
  const [strategies, setStrategies] = useState<PublicStrategy[]>([]);

  useEffect(() => {
    let cancelled = false;
    getPublicStats().then((s) => {
      if (!cancelled) setStats(s);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    getPublicStrategies().then((r) => {
      if (!cancelled) setStrategies(r.strategies);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const featured = pickFeatured(strategies);
  const featuredTagline = featured ? resolveTagline(featured, lang) : "";
  const explorerBase = "https://app.hyperliquid.xyz/explorer/address";
  const leaderExplorerHref = featured ? `${explorerBase}/${featured.leader_address}` : "https://app.hyperliquid.xyz";

  const builderFeeDisplay =
    stats.builder_fee_bps == null ? NO_VALUE : `${(stats.builder_fee_bps / 100).toFixed(2)}%`;

  const evidence = [
    {
      key: "routed",
      value: fmtUsdCompact(stats.routed_volume_usd_total),
      label: home.evidence.routedVolumeLabel,
      link: home.evidence.routedVolumeLink,
      href: leaderExplorerHref,
    },
    {
      key: "live_days",
      value: stats.live_days == null ? NO_VALUE : `${stats.live_days}${home.evidence.liveDaysSuffix}`,
      label: home.evidence.liveDaysLabel,
      link: home.evidence.liveDaysLink,
      href: leaderExplorerHref,
    },
    {
      key: "builder_fee",
      value: builderFeeDisplay,
      label: home.evidence.builderFeeLabel,
      link: home.evidence.builderFeeLink,
      href: "/docs#fees",
    },
    {
      key: "custody",
      value: home.evidence.custodyValue,
      label: home.evidence.custodyLabel,
      link: home.evidence.custodyLink,
      href: "/docs#custody",
    },
  ];

  return (
    <main className="page home-page">
      {/* ---------- hero ---------- */}
      <section className="home-hero">
        <div className="home-hero-copy">
          <div className="pill home-badge">
            <span className="home-badge-dot" aria-hidden="true" />
            <span>{home.hero.badge}</span>
          </div>
          <h1 className="home-hero-title">{home.hero.title}</h1>
          <p className="home-hero-sub">{home.hero.sub}</p>
          <div className="home-hero-cta-row">
            <Link href="/strategies" className="btn btn-primary">
              {home.hero.ctaPrimary}
            </Link>
            <a href="#security" className="btn btn-secondary">
              {home.hero.ctaSecondary}
            </a>
          </div>
          <p className="home-hero-note">{home.hero.microNote}</p>
        </div>

        <div className="card home-hero-featured">
          {featured ? (
            <>
              <div className="home-hero-featured-head">
                <div>
                  <div className="home-hero-featured-name">
                    {featured.name}
                    {featuredTagline ? ` · ${featuredTagline}` : ""}
                  </div>
                  <div className="mono home-hero-featured-addr">
                    {home.hero.featuredCard.leaderPrefix}
                    {shortAddr(featured.leader_address)}
                    {home.hero.featuredCard.leaderLinkSuffix}
                  </div>
                </div>
                <span className="pill follow-pill" data-state={featured.status === "running" ? "following" : "paused"}>
                  <span className="follow-pill-dot" aria-hidden="true" />
                  {featured.status === "running"
                    ? home.hero.featuredCard.statusRunning
                    : home.hero.featuredCard.statusPaused}
                </span>
              </div>
              <div className="home-hero-featured-metrics">
                <div>
                  <div className="strategy-metric-label">
                    {home.hero.featuredCard.returnLabelPrefix}
                    {featured.live_days}
                    {home.hero.featuredCard.returnLabelSuffix}
                  </div>
                  <div className="mono home-hero-featured-value pos">
                    {metricText(featured.metrics.total_return_pct, featured.metrics.total_return_pct_insufficient, "%")}
                  </div>
                </div>
                <div>
                  {/* ⭐ D5：與策略詳情頁／traders 頁同一個 copy key（「策略期間回撤」），
                      不再自己定義一份「期間最大回撤」——見 copy.ts strategyDetail.metrics。 */}
                  <div className="strategy-metric-label">{COPY.strategyDetail.metrics.maxDrawdownLabel}</div>
                  <div className="mono home-hero-featured-value neg">
                    {metricText(featured.metrics.max_drawdown_pct, featured.metrics.max_drawdown_pct_insufficient, "%")}
                  </div>
                </div>
                <div>
                  <div className="strategy-metric-label">{home.hero.featuredCard.liveDaysLabel}</div>
                  <div className="mono home-hero-featured-value">{featured.live_days}</div>
                </div>
                {/* ⭐ M3 round3 Task 9 修正（主線程實機走查退回，2026-08-30）：<10 或 null
                    時原本改渲染「連續實盤天數」，但值與第三格「實盤天數」完全相同
                    （同一個 live_days），並排顯示同一個數字兩次看起來像 bug——裁決改為
                    直接不渲染替代欄，面板收斂為三格。 */}
                {featured.follower_count != null && featured.follower_count >= FOLLOWER_COUNT_DISPLAY_MIN && (
                  <div>
                    <div className="strategy-metric-label">{home.hero.featuredCard.followerCountLabel}</div>
                    <div className="mono home-hero-featured-value">{featured.follower_count}</div>
                  </div>
                )}
              </div>
              <div className="home-hero-featured-footnote">
                {home.hero.featuredCard.sampleNotePrefix}
                {featured.live_days}
                {home.hero.featuredCard.sampleNoteSuffix}{" "}
                <Link href={`/strategies/${featured.slug}`}>{home.hero.featuredCard.methodologyLink}</Link>
              </div>
            </>
          ) : (
            <p className="hint">{home.hero.featuredCard.noDataNote}</p>
          )}
        </div>
      </section>

      {/* ---------- 證據列（NOTE 02） ---------- */}
      <section className="evidence-row">
        {evidence.map((item) => (
          <div key={item.key} className="evidence-item">
            <div className="mono evidence-value">{item.value}</div>
            <div className="evidence-label">{item.label}</div>
            <a href={item.href} className="mono evidence-link">
              {item.link}
            </a>
          </div>
        ))}
      </section>

      {/* ---------- 策略區 ---------- */}
      <section className="home-strategies" id="strategies">
        <div className="home-strategies-head">
          <div>
            <h2>{home.strategies.heading}</h2>
            <p className="section-sub">{home.strategies.sub}</p>
          </div>
          <Link href="/leaderboard" className="home-strategies-viewall">
            {home.strategies.viewAll}
          </Link>
        </div>
        <div className="strategy-grid">
          {strategies.map((s) => (
            <StrategyCard key={s.slug} strategy={s} />
          ))}
          {strategies.length === 0 && <p className="hint">{home.strategies.empty}</p>}
          <div className="card strategy-card strategy-card-advanced">
            <div className="strategy-card-name">{home.strategies.advancedTitle}</div>
            <p className="strategy-card-advanced-body">{home.strategies.advancedBody}</p>
            <Link href="/advanced" className="btn btn-secondary btn-block">
              {home.strategies.advancedCta}
            </Link>
          </div>
        </div>
      </section>

      {/* ---------- 授權能力矩陣 ---------- */}
      <CapabilityMatrix id="security" />

      {/* ---------- 費用試算 ---------- */}
      <FeeCalculator />

      {/* ---------- 開始跟單的四個步驟 ---------- */}
      <section id="how" className="home-steps">
        <h2>{home.steps.heading}</h2>
        <div className="home-steps-grid">
          {home.steps.items.map((step) => (
            <div key={step.n} className="card home-step-card">
              <div className="mono home-step-num">STEP {step.n}</div>
              <div className="home-step-title">{step.t}</div>
              <div className="home-step-desc">{step.d}</div>
            </div>
          ))}
        </div>
      </section>
    </main>
  );
}
