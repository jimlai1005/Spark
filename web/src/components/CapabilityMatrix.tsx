"use client";
import { useCopy } from "@/lib/lang";

/**
 * CapabilityMatrix — 授權能力矩陣（Task 8，設計稿 §03「授權能力矩陣」段）。
 *
 * ⭐ 單一來源、三處共用：首頁、策略詳情頁的授權說明、onboarding 授權卡都會掛這個
 * 元件，文案（`COPY.auth.*`）刻意抽成一個獨立 key namespace，不掛在 `home.*`
 * 底下（見 copy.ts 檔頭註解）。`id` 由呼叫端決定要不要提供錨點——首頁把它掛在
 * `#security`（Header 的「安全性」nav 連到 `/#security`），其他頁面通常不需要。
 */
export function CapabilityMatrix({ id }: { id?: string }) {
  const COPY = useCopy();
  const c = COPY.auth;

  return (
    <section id={id} className="capability-matrix">
      <h2>{c.heading}</h2>
      <p className="section-sub">{c.sub}</p>
      <div className="capability-grid">
        <div className="card capability-col capability-can">
          <div className="capability-col-head">
            <span aria-hidden="true">✓</span>
            <span>{c.canTitle}</span>
          </div>
          <ul>
            {c.can.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
        <div className="card capability-col capability-cannot">
          <div className="capability-col-head">
            <span aria-hidden="true">✕</span>
            <span>{c.cannotTitle}</span>
          </div>
          <ul>
            {c.cannot.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      </div>
      <p className="inset capability-revocable">{c.revocable}</p>
    </section>
  );
}
