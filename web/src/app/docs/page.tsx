"use client";
/**
 * `/docs` — 運作方式與文件（Task 12，法務內容頁但非法務長文）。
 *
 * 五段（見 `docs/superpowers/specs/2026-08-28-legal-copy-zh.md` 的 /docs 節）：
 * 1. 運作方式：直接復用首頁「開始跟單的四個步驟」（`home.steps`），同一份字串。
 * 2. 授權邊界：`<CapabilityMatrix>` 元件本身（首頁／策略頁授權說明共用同一顆）。
 * 3. 費用：`<FeeCalculator>` 元件本身（首頁費用區同一顆，含試算 slider）。
 * 4. 績效方法論 `id="methodology"`：復用 `strategyDetail.methodology` 的慣例句
 *    （365 日年化、無風險利率 0%——這兩個是全平台統一常數，不是單一策略的實例
 *    數字，見 `PublicStrategyMethodology` 預設值）＋`totalReturnNote`（真實入金
 *    起算）＋`sharpeLabel`/`sharpeNoteSuffix`（標準誤揭露的既有標記法）；
 *    唯一新增的一句是 `methodology.basisNote`（perp 基準），既有 key 裡沒有任何
 *    一句話涵蓋這個慣例（`methodology.basis` 這個 API 欄位本身從未被任何頁面
 *    渲染過，見 `lib/publicApi.ts`）。
 * 5. 法務文件連結：復用 `footer.legal*`，連向 /terms /privacy /risk（Task 12
 *    建立的三頁）。
 *
 * 未登入可直接開啟，不掛登入 guard（同 /terms /privacy /risk）。
 */
import Link from "next/link";
import { CapabilityMatrix } from "@/components/CapabilityMatrix";
import { FeeCalculator } from "@/components/FeeCalculator";
import { useCopy } from "@/lib/lang";

export default function DocsPage() {
  const COPY = useCopy();
  const home = COPY.home;
  const m = COPY.strategyDetail.methodology;
  const metrics = COPY.strategyDetail.metrics;
  const footer = COPY.footer;

  return (
    <main className="page docs-page">
      <header className="docs-page-head">
        <h1>{COPY.nav.docs}</h1>
      </header>

      {/* ---------- 1. 運作方式 ---------- */}
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

      {/* ---------- 2. 授權邊界 ---------- */}
      <CapabilityMatrix id="boundary" />

      {/* ---------- 3. 費用 ---------- */}
      <FeeCalculator />

      {/* ---------- 4. 績效方法論 ---------- */}
      <section id="methodology" className="card docs-methodology">
        <h2>{m.heading}</h2>
        <p>
          {m.conventionPrefix}365{m.conventionMid}0%{m.conventionSuffix}
        </p>
        <ul>
          <li>{m.basisNote}</li>
          <li>{metrics.totalReturnNote}</li>
          <li>
            {metrics.sharpeLabel}
            {metrics.sharpeNoteSuffix}
          </li>
        </ul>
      </section>

      {/* ---------- 5. 法務文件連結 ---------- */}
      <section className="card docs-legal-links">
        <h2>{footer.legalTitle}</h2>
        <div className="footer-col-list">
          <Link href="/terms">{footer.legalTerms}</Link>
          <Link href="/privacy">{footer.legalPrivacy}</Link>
          <Link href="/risk">{footer.legalRisk}</Link>
        </div>
      </section>
    </main>
  );
}
