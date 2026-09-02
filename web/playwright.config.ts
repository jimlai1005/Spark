import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright 設定 — 公開頁瀏覽器 smoke（plan `docs/superpowers/plans/
 * 2026-09-02-golive-regression.md` T5）。
 *
 * 刻意不設 `webServer`：本機 stack（FastAPI on :8700、`next start` on :3100）
 * 由主線程另外起、另外收尾（見 spec 檔頭與 plan T5 派工紀錄），這裡只負責
 * 打 `baseURL`。`E2E_BASE_URL` 可覆寫（例如未來 CI 用不同埠）。
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://127.0.0.1:3000",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
