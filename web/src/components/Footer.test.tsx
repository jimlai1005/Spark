import { readFileSync } from "node:fs";
import path from "node:path";
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

  // ⭐ M3 round3 Task 9（R2 P2）：unknown／讀取失敗／載入中三態一律不渲染狀態燈
  // （DOM 不存在，不是 visibility hidden）——不再有「狀態未知」灰字常駐。
  it("狀態燈：unknown（後端明確回傳 unknown）→ 整顆燈不渲染（DOM 不存在）", async () => {
    stubStatus("unknown");
    renderFooter();
    await screen.findByText(COPY_ZH.footer.productTitle); // 等 effect 跑完
    expect(screen.queryByText(COPY_ZH.footer.statusUnknown)).not.toBeInTheDocument();
    expect(document.querySelector(".footer-status")).not.toBeInTheDocument();
  });

  it("狀態燈：fetch 失敗（reject）→ 整顆燈不渲染（DOM 不存在）", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("network down"))));
    renderFooter();
    await screen.findByText(COPY_ZH.footer.productTitle); // 等 effect 跑完
    expect(screen.queryByText(COPY_ZH.footer.statusUnknown)).not.toBeInTheDocument();
    expect(document.querySelector(".footer-status")).not.toBeInTheDocument();
  });

  it("狀態燈：載入中（fetch 尚未 resolve）→ 整顆燈不渲染（DOM 不存在）", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {}))); // 永不 resolve，模擬載入中
    renderFooter();
    expect(screen.queryByText(COPY_ZH.footer.statusOk)).not.toBeInTheDocument();
    expect(screen.queryByText(COPY_ZH.footer.statusDegraded)).not.toBeInTheDocument();
    expect(screen.queryByText(COPY_ZH.footer.statusUnknown)).not.toBeInTheDocument();
    expect(document.querySelector(".footer-status")).not.toBeInTheDocument();
  });

  it("Task 12：系統狀態／績效方法論已接上真實連結", () => {
    stubStatus("unknown");
    renderFooter();
    expect(screen.getByRole("link", { name: COPY_ZH.footer.verifiableMethodology }))
      .toHaveAttribute("href", "/docs#methodology");
    expect(screen.getByRole("link", { name: COPY_ZH.footer.verifiableStatus })).toHaveAttribute("href", "/status");
  });

  it("Task 19 round2：「文件」連結已隱藏（產品欄不再有連向 /docs 的入口）", () => {
    stubStatus("unknown");
    renderFooter();
    expect(screen.queryByRole("link", { name: COPY_ZH.footer.productDocs })).not.toBeInTheDocument();
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

  it("390 斷點（Task 19 修正）：.footer-cols 在 ≤480px 收斂為單欄，欄間距 24px", () => {
    const css = readFileSync(
      path.resolve(__dirname, "../styles/globals.css"),
      "utf-8",
    );
    const mediaBlock = css.match(/@media \(max-width: 480px\) \{[\s\S]*?\n\}/);
    expect(mediaBlock).not.toBeNull();
    expect(mediaBlock?.[0]).toMatch(/\.footer-cols\s*\{[^}]*grid-template-columns:\s*1fr[^}]*gap:\s*24px/);
  });
});
