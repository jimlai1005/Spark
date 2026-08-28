/**
 * `/docs` 頁測試（Task 12）：渲染五段（運作方式／授權邊界／費用／績效方法論／
 * 法務連結），且各自的文案值都是既有 copy key 的復用（不是本頁新寫的中文）。
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { COPY_ZH as COPY } from "@/lib/copy";
import DocsPage from "./page";

describe("DocsPage", () => {
  it("五段標題都渲染", () => {
    render(<DocsPage />);
    expect(screen.getByRole("heading", { level: 1, name: COPY.nav.docs })).toBeInTheDocument();
    // 1. 運作方式
    expect(screen.getByRole("heading", { level: 2, name: COPY.home.steps.heading })).toBeInTheDocument();
    for (const step of COPY.home.steps.items) {
      expect(screen.getByText(step.t)).toBeInTheDocument();
    }
    // 2. 授權邊界（CapabilityMatrix）
    expect(screen.getByRole("heading", { level: 2, name: COPY.auth.heading })).toBeInTheDocument();
    expect(screen.getByText(COPY.auth.can[0])).toBeInTheDocument();
    // 3. 費用（FeeCalculator）
    expect(screen.getByRole("heading", { level: 2, name: COPY.fee.heading })).toBeInTheDocument();
    // 4. 績效方法論
    expect(screen.getByRole("heading", { level: 2, name: COPY.strategyDetail.methodology.heading }))
      .toBeInTheDocument();
    expect(document.getElementById("methodology")).not.toBeNull();
    expect(screen.getByText(COPY.strategyDetail.methodology.basisNote)).toBeInTheDocument();
    expect(screen.getByText(COPY.strategyDetail.metrics.totalReturnNote)).toBeInTheDocument();
    // 5. 法務文件連結
    expect(screen.getByRole("heading", { level: 2, name: COPY.footer.legalTitle })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: COPY.footer.legalTerms })).toHaveAttribute("href", "/terms");
    expect(screen.getByRole("link", { name: COPY.footer.legalPrivacy })).toHaveAttribute("href", "/privacy");
    expect(screen.getByRole("link", { name: COPY.footer.legalRisk })).toHaveAttribute("href", "/risk");
  });
});
