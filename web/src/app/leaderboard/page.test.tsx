/**
 * `/leaderboard` 舊路由（M3 round3 Task 4）：功能已遷移至 `/explore`（重構為
 * 「可跟單對象探索」），本頁只驗 redirect 行為——完整的表格／過濾／分頁測試
 * 搬到 `../explore/page.test.tsx`。沿用 `../leaders/page.test.tsx` 的既有驗法。
 */
import { render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const routerPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: routerPush }),
}));

import LeaderboardPage from "./page";

beforeEach(() => {
  routerPush.mockReset();
});

describe("LeaderboardPage — 舊路由 redirect（M3 round3 Task 4）", () => {
  it("進頁即 redirect /explore（不留白畫面）", async () => {
    const { container } = render(<LeaderboardPage />);
    await waitFor(() => expect(routerPush).toHaveBeenCalledWith("/explore"));
    expect((container.textContent ?? "").trim().length).toBeGreaterThan(0);
  });
});
