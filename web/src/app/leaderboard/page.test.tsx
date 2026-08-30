/**
 * `/leaderboard` — 排行榜頁測試（M3 round2 Task 5）。
 * 只驗證頁面職責：抓資料、渲染表格、視窗切換、載入/錯誤/空態、列連結。
 */
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { COPY_ZH as COPY } from "@/lib/copy";
import LeaderboardPage from "./page";

function jsonResponse(body: unknown, ok = true): Response {
  return { ok, json: async () => body } as Response;
}

function stubFetch(impl: (url: string) => Response) {
  vi.stubGlobal("fetch", vi.fn((url: string) => Promise.resolve(impl(url))));
}

const ROW_A = {
  address: "0xaaaa00000000000000000000000000000000aaaa",
  display_name: "Alice", account_value: "1000000.00",
  pnl: "50000.00", roi: "0.05", vlm: "2000000.0",
};
const ROW_B = {
  address: "0xbbbb00000000000000000000000000000000bbbb",
  display_name: null, account_value: "500000.00",
  pnl: "10000.00", roi: "0.02", vlm: "900000.0",
};

describe("LeaderboardPage", () => {
  it("載入中顯示 loading 文案", () => {
    stubFetch(() => new Promise(() => {}) as unknown as Response); // 永不 resolve
    render(<LeaderboardPage />);
    expect(screen.getByText(COPY.leaderboard.loading)).toBeInTheDocument();
  });

  it("成功回傳 → 渲染表格列，每列連到 /traders/{address}", async () => {
    stubFetch(() => jsonResponse({ window: "month", updated_at: 1, rows: [ROW_A, ROW_B] }));
    render(<LeaderboardPage />);
    await screen.findByText("Alice");
    // [W3] 2026-08-29：連結一律不帶 ?name=（不信任 query param 顯示名稱）——
    // display_name 只在本頁表格內顯示，交易員詳情頁自己用 shortAddr 當標題。
    const rowA = screen.getByText("Alice").closest("a");
    expect(rowA).toHaveAttribute("href", `/traders/${ROW_A.address}`);
    // 沒有 display_name 的列 → 顯示縮寫地址，連結同樣不帶查詢參數
    expect(screen.getByText("0xbbbb…bbb")).toBeInTheDocument();
    const rowB = screen.getByText("0xbbbb…bbb").closest("a");
    expect(rowB).toHaveAttribute("href", `/traders/${ROW_B.address}`);
  });

  it("讀取失敗 → 顯示 error 文案，不顯示空態或表格", async () => {
    stubFetch(() => jsonResponse({ detail: "boom" }, false));
    render(<LeaderboardPage />);
    await waitFor(() => {
      expect(screen.getByText(COPY.leaderboard.error)).toBeInTheDocument();
    });
    expect(screen.queryByText(COPY.leaderboard.empty)).not.toBeInTheDocument();
  });

  it("成功但零筆 → 顯示 empty 文案", async () => {
    stubFetch(() => jsonResponse({ window: "month", updated_at: 1, rows: [] }));
    render(<LeaderboardPage />);
    await waitFor(() => {
      expect(screen.getByText(COPY.leaderboard.empty)).toBeInTheDocument();
    });
  });

  it("切換視窗會用新的 window 重新打 API", async () => {
    const fetchMock = vi.fn((url: string) => {
      const window_ = new URL(url, "https://x.test").searchParams.get("window");
      return Promise.resolve(jsonResponse({
        window: window_, updated_at: 1,
        rows: window_ === "day" ? [ROW_B] : [ROW_A],
      }));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<LeaderboardPage />);
    await screen.findByText("Alice"); // 預設 month

    screen.getByRole("tab", { name: COPY.leaderboard.windows.day }).click();
    await waitFor(() => {
      expect(fetchMock).toHaveBeenLastCalledWith("/api/public/leaderboard?window=day");
    });
    await screen.findByText("0xbbbb…bbb");
    expect(screen.queryByText("Alice")).not.toBeInTheDocument();
  });

  it("標題與說明文字存在", () => {
    stubFetch(() => jsonResponse({ window: "month", updated_at: 1, rows: [] }));
    render(<LeaderboardPage />);
    expect(screen.getByRole("heading", { level: 1, name: COPY.leaderboard.heading })).toBeInTheDocument();
    expect(screen.getByText(COPY.leaderboard.sub)).toBeInTheDocument();
  });
});
