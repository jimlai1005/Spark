/** `/privacy` 頁測試（Task 12）：渲染標題與至少 3 個 section 標題，內容取自 content/legal.ts。 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LEGAL_ZH } from "@/content/legal";
import PrivacyPage from "./page";

describe("PrivacyPage", () => {
  it("渲染標題與生效日期", () => {
    render(<PrivacyPage />);
    expect(screen.getByRole("heading", { level: 1, name: LEGAL_ZH.privacy.title })).toBeInTheDocument();
    expect(screen.getByText(new RegExp(LEGAL_ZH.privacy.effectiveDate))).toBeInTheDocument();
  });

  it("渲染至少 3 個 section 標題", () => {
    render(<PrivacyPage />);
    const headings = LEGAL_ZH.privacy.sections.slice(0, 3);
    for (const s of headings) {
      expect(screen.getByRole("heading", { level: 2, name: s.heading })).toBeInTheDocument();
    }
  });

  it("內容逐字命中（抽查第一節第一個要點）", () => {
    render(<PrivacyPage />);
    expect(screen.getByText(LEGAL_ZH.privacy.sections[0].paragraphs[0])).toBeInTheDocument();
  });
});
