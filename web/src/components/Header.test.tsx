import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Header } from "./Header";

vi.mock("next/navigation", () => ({ usePathname: () => "/onboarding" }));

describe("Header", () => {
  it("渲染 wordmark 與三個 tab，當前頁帶 aria-current", () => {
    render(<Header />);
    expect(screen.getByText("FILET")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "登入" })).not.toHaveAttribute("aria-current");
    expect(screen.getByRole("link", { name: "開通" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "績效" })).toBeInTheDocument();
    // admin 不在 tabs（設計定案 8）
    expect(screen.queryByRole("link", { name: /admin/i })).not.toBeInTheDocument();
  });
});
