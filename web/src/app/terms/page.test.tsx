/** `/terms` 頁測試（Task 12）：渲染標題與至少 3 個 section 標題，內容取自 content/legal.ts。 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LEGAL_ZH } from "@/content/legal";
import TermsPage from "./page";

describe("TermsPage", () => {
  it("渲染標題與生效日期", () => {
    render(<TermsPage />);
    expect(screen.getByRole("heading", { level: 1, name: LEGAL_ZH.terms.title })).toBeInTheDocument();
    expect(screen.getByText(new RegExp(LEGAL_ZH.terms.effectiveDate))).toBeInTheDocument();
  });

  it("渲染至少 3 個 section 標題", () => {
    render(<TermsPage />);
    const headings = LEGAL_ZH.terms.sections.slice(0, 3);
    for (const s of headings) {
      expect(screen.getByRole("heading", { level: 2, name: s.heading })).toBeInTheDocument();
    }
  });

  it("內容逐字命中（抽查第一節首段）", () => {
    render(<TermsPage />);
    expect(screen.getByText(LEGAL_ZH.terms.sections[0].paragraphs[0])).toBeInTheDocument();
  });
});
