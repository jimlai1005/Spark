/**
 * `/leaderboard` 舊路由（M3 round3 Task 4）：功能已遷移至 `/explore`（重構為
 * 「可跟單對象探索」），本頁只驗 redirect 行為——完整的表格／過濾／分頁測試
 * 搬到 `../explore/page.test.tsx`。沿用 `../leaders/page.test.tsx` 的既有驗法。
 */
import { render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const routerReplace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: routerReplace }),
}));

import LeaderboardPage from "./page";

beforeEach(() => {
  routerReplace.mockReset();
});

// R-C／S1（2026-08-30 審查修正）：純轉發頁改 `router.replace`（非 `push`），
// 避免使用者在 `/explore` 按上一頁回到這裡又立刻被轉走的死循環。
describe("LeaderboardPage — 舊路由 redirect（M3 round3 Task 4，R-C/S1）", () => {
  it("進頁即 replace /explore（不留白畫面、不留在瀏覽紀錄）", async () => {
    const { container } = render(<LeaderboardPage />);
    await waitFor(() => expect(routerReplace).toHaveBeenCalledWith("/explore"));
    expect((container.textContent ?? "").trim().length).toBeGreaterThan(0);
  });
});
