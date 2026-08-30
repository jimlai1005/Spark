/**
 * `/leaders` 舊路由（Task 11）：功能已遷移至 `/advanced`，本頁只驗 redirect
 * 行為——完整的地址輸入／准入預覽／選定流程測試搬到 `../advanced/page.test.tsx`。
 */
import { render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const routerReplace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: routerReplace }),
}));

import LeadersPage from "./page";

beforeEach(() => {
  routerReplace.mockReset();
});

// R-C／S1（2026-08-30 審查修正）：純轉發頁改 `router.replace`（非 `push`），
// 避免使用者在 `/advanced` 按上一頁回到這裡又立刻被轉走的死循環。
describe("LeadersPage — 舊路由 redirect（Task 11，R-C/S1）", () => {
  it("⭐ 進頁即 replace /advanced（不留白畫面、不留在瀏覽紀錄）", async () => {
    const { container } = render(<LeadersPage />);
    await waitFor(() => expect(routerReplace).toHaveBeenCalledWith("/advanced"));
    expect((container.textContent ?? "").trim().length).toBeGreaterThan(0);
  });
});
