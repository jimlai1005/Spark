import { describe, expect, it } from "vitest";
import robots from "./robots";

describe("robots.ts（Task 17）", () => {
  it("全站放行且指向 sitemap", () => {
    const r = robots();
    expect(r.rules).toEqual({ userAgent: "*", allow: "/" });
    expect(r.sitemap).toBe("https://app.filet.trade/sitemap.xml");
  });
});
