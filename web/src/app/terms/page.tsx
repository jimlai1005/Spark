"use client";
/**
 * `/terms` — 服務條款（Task 12）。內容來自 `content/legal.ts`（逐字自法務權威文本，
 * 見該檔檔頭）。未登入可直接開啟，不掛登入 guard（設計稿 §07 L904：五頁法務/內容頁
 * 皆可被索引）。
 */
import { useLang } from "@/lib/lang";
import { LEGAL_EN, LEGAL_ZH } from "@/content/legal";

export default function TermsPage() {
  const { lang } = useLang();
  const doc = lang === "en" ? LEGAL_EN.terms : LEGAL_ZH.terms;

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
            <p key={`${section.heading}-${i}`}>
              {p.startsWith("http")
                ? <a href={p} target="_blank" rel="noopener noreferrer">{p}</a>
                : p}
            </p>
          ))}
        </section>
      ))}
    </main>
  );
}
