import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { COPY_ZH } from "@/lib/copy";
import { LangProvider } from "@/lib/lang";
import { Footer } from "./Footer";

function jsonResponse(body: unknown, ok = true): Response {
  return { ok, json: async () => body } as Response;
}

function stubStatus(status: "ok" | "degraded" | "unknown", ok = true) {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => jsonResponse({ status, components: [], updated_at: 1 }, ok)),
  );
}

function renderFooter() {
  return render(<LangProvider><Footer /></LangProvider>);
}

describe("Footer", () => {
  it("四欄都渲染：品牌/產品/可驗證/法務與聯絡", () => {
    stubStatus("unknown");
    renderFooter();
    expect(screen.getByText("FILET")).toBeInTheDocument();
    expect(screen.getByText(COPY_ZH.footer.productTitle)).toBeInTheDocument();
    expect(screen.getByText(COPY_ZH.footer.verifiableTitle)).toBeInTheDocument();
    expect(screen.getByText(COPY_ZH.footer.legalTitle)).toBeInTheDocument();
  });

  it("免責文字存在", () => {
    stubStatus("unknown");
    renderFooter();
    expect(screen.getByText(COPY_ZH.footer.disclaimer)).toBeInTheDocument();
  });

  it("法務欄連向 /terms /privacy /risk 與 mailto:contact@filet.trade", () => {
    stubStatus("unknown");
    renderFooter();
    expect(screen.getByRole("link", { name: COPY_ZH.footer.legalTerms })).toHaveAttribute("href", "/terms");
    expect(screen.getByRole("link", { name: COPY_ZH.footer.legalPrivacy })).toHaveAttribute("href", "/privacy");
    expect(screen.getByRole("link", { name: COPY_ZH.footer.legalRisk })).toHaveAttribute("href", "/risk");
    expect(screen.getByRole("link", { name: COPY_ZH.footer.legalContact }))
      .toHaveAttribute("href", "mailto:contact@filet.trade");
  });

  it("狀態燈三態：ok → 綠字「系統運作正常」", async () => {
    stubStatus("ok");
    renderFooter();
    expect(await screen.findByText(COPY_ZH.footer.statusOk)).toBeInTheDocument();
  });

  it("狀態燈三態：degraded → 黃字對應文案", async () => {
    stubStatus("degraded");
    renderFooter();
    expect(await screen.findByText(COPY_ZH.footer.statusDegraded)).toBeInTheDocument();
  });

  it("狀態燈三態：unknown（含讀取失敗降級）→ 灰字「狀態未知」", async () => {
    vi.stubGlobal("fetch", vi.fn(() => { throw new Error("network down"); }));
    renderFooter();
    expect(await screen.findByText(COPY_ZH.footer.statusUnknown)).toBeInTheDocument();
  });

  it("載入完成後 data-status 反映後端回應（供樣式掛鉤）", async () => {
    stubStatus("degraded");
    const { container } = renderFooter();
    const el = await screen.findByText(COPY_ZH.footer.statusDegraded);
    const wrapper = el.closest(".footer-status");
    expect(wrapper).not.toBeNull();
    expect(wrapper).toHaveAttribute("data-status", "degraded");
    expect(container.querySelectorAll(".footer-status").length).toBe(1);
  });
});
