"use client";
/**
 * `/risk` — 風險揭露（Task 12）。內容來自 `content/legal.ts`；未登入可直接開啟，
 * 不掛登入 guard（見 `/terms/page.tsx` 檔頭同一份說明）。
 */
import { useLang } from "@/lib/lang";
import { LEGAL_EN, LEGAL_ZH } from "@/content/legal";

export default function RiskPage() {
  const { lang } = useLang();
  const doc = lang === "en" ? LEGAL_EN.risk : LEGAL_ZH.risk;

  return (
    <main className="page legal-page">
      <header className="legal-page-head">
        <h1>{doc.title}</h1>
        <p className="section-sub mono">
          {doc.effectiveDateLabel}: {doc.effectiveDate}
        </p>
      </header>
      {doc.sections.map((section) => (
        <section key={section.heading} className="card legal-section">
          <h2>{section.heading}</h2>
          {section.paragraphs.map((p, i) => (
            <p key={`${section.heading}-${i}`}>{p}</p>
          ))}
        </section>
      ))}
    </main>
  );
}
