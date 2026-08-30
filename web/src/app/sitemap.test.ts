import { describe, expect, it } from "vitest";
import sitemap, { SITEMAP_ROUTES } from "./sitemap";

describe("sitemap.ts（Task 17）", () => {
  it("固定路由清單＝9 條，不含動態 slug", () => {
    expect(SITEMAP_ROUTES).toEqual([
      "/",
      "/strategies",
      "/explore",
      "/advanced",
      "/docs",
      "/terms",
      "/privacy",
      "/risk",
      "/status",
    ]);
  });

  it("每條路由組成 https://app.filet.trade 開頭的絕對 URL", () => {
    const entries = sitemap();
    expect(entries).toHaveLength(SITEMAP_ROUTES.length);
    for (const [i, path] of SITEMAP_ROUTES.entries()) {
      expect(entries[i].url).toBe(`https://app.filet.trade${path === "/" ? "/" : path}`);
    }
  });
});
