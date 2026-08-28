import { describe, expect, it, afterEach, vi } from "vitest";

describe("siteOrigin（Task 17）", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("預設 origin 為 https://app.filet.trade（NEXT_PUBLIC_SITE_ORIGIN 未設定時）", async () => {
    vi.stubEnv("NEXT_PUBLIC_SITE_ORIGIN", "");
    vi.resetModules();
    const { SITE_ORIGIN } = await import("./siteOrigin");
    expect(SITE_ORIGIN).toBe("https://app.filet.trade");
  });

  it("canonicalUrl 收斂前導斜線：有無 / 開頭結果相同", async () => {
    vi.stubEnv("NEXT_PUBLIC_SITE_ORIGIN", "");
    vi.resetModules();
    const { canonicalUrl } = await import("./siteOrigin");
    expect(canonicalUrl("/strategies")).toBe("https://app.filet.trade/strategies");
    expect(canonicalUrl("strategies")).toBe("https://app.filet.trade/strategies");
    expect(canonicalUrl("/")).toBe("https://app.filet.trade/");
  });

  it("NEXT_PUBLIC_SITE_ORIGIN 有值時覆蓋預設，且吃掉結尾斜線", async () => {
    vi.stubEnv("NEXT_PUBLIC_SITE_ORIGIN", "https://staging.filet.trade/");
    vi.resetModules();
    const { SITE_ORIGIN, canonicalUrl } = await import("./siteOrigin");
    expect(SITE_ORIGIN).toBe("https://staging.filet.trade");
    expect(canonicalUrl("/status")).toBe("https://staging.filet.trade/status");
  });
});
