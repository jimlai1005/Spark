/**
 * `/leaders` 舊路由（Task 11）：功能已遷移至 `/advanced`，本頁只驗 redirect
 * 行為——完整的地址輸入／准入預覽／選定流程測試搬到 `../advanced/page.test.tsx`。
 */
import { render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const routerPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: routerPush }),
}));

import LeadersPage from "./page";

beforeEach(() => {
  routerPush.mockReset();
});

describe("LeadersPage — 舊路由 redirect（Task 11）", () => {
  it("⭐ 進頁即 redirect /advanced（不留白畫面）", async () => {
    const { container } = render(<LeadersPage />);
    await waitFor(() => expect(routerPush).toHaveBeenCalledWith("/advanced"));
    expect((container.textContent ?? "").trim().length).toBeGreaterThan(0);
  });
});
