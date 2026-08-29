import { describe, expect, it } from "vitest";
import { generateMetadata } from "./layout";

describe("strategies/[slug]/layout.tsx — generateMetadata（opus 審查 Suggestion 3）", () => {
  it("title 用 slug 首字大寫", async () => {
    const meta = await generateMetadata({ params: Promise.resolve({ slug: "core" }) });
    expect(meta.title).toBe("Core");
  });

  it("canonical 指向這個 slug 自己的頁面，不是父層 /strategies", async () => {
    const meta = await generateMetadata({ params: Promise.resolve({ slug: "core" }) });
    expect(meta.alternates?.canonical).toBe("https://app.filet.trade/strategies/core");
  });

  it("不同 slug → 不同 title 與 canonical（不是寫死同一個值）", async () => {
    const meta = await generateMetadata({ params: Promise.resolve({ slug: "momentum" }) });
    expect(meta.title).toBe("Momentum");
    expect(meta.alternates?.canonical).toBe("https://app.filet.trade/strategies/momentum");
  });
});
