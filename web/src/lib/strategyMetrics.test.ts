import { describe, expect, it } from "vitest";
import { fmtAmount } from "@/lib/format";
import { computeCagrPct, computeStartEndEquity, metricText } from "./strategyMetrics";

describe("metricText", () => {
  it("insufficient → NO_VALUE", () => {
    expect(metricText("12.34", true, "%")).toBe("—");
  });
  it("value 存在 → 附尾綴", () => {
    expect(metricText("12.34", false, "%")).toBe("12.34%");
  });
  it("value 為 null → NO_VALUE（即使 insufficient=false）", () => {
    expect(metricText(null, false)).toBe("—");
  });
});

describe("computeCagrPct", () => {
  it("72 天、20.35% 總報酬 → 年化外推", () => {
    const r = computeCagrPct("20.35", false, 72);
    expect(r).not.toBeNull();
    expect(Number(r)).toBeGreaterThan(0);
  });
  it("insufficient → null", () => {
    expect(computeCagrPct("20.35", true, 72)).toBeNull();
  });
  it("liveDays <= 0 → null", () => {
    expect(computeCagrPct("20.35", false, 0)).toBeNull();
  });
  it("帳戶歸零（1+r<=0）→ null，不強行印出無意義數字", () => {
    expect(computeCagrPct("-150", false, 30)).toBeNull();
  });
});

describe("computeStartEndEquity", () => {
  const meth = { initial_deposit_usd: "1000" } as Parameters<typeof computeStartEndEquity>[0];

  it("正常換算起訖淨值", () => {
    const r = computeStartEndEquity(meth, ["1", "1.2"], fmtAmount);
    expect(r).toEqual({ start: fmtAmount("1000", 0), end: fmtAmount("1200", 0) });
  });

  it("缺 initial_deposit_usd → null", () => {
    const r = computeStartEndEquity(
      { initial_deposit_usd: null } as Parameters<typeof computeStartEndEquity>[0],
      ["1", "1.2"], fmtAmount,
    );
    expect(r).toBeNull();
  });

  it("equity_index 空 → null", () => {
    expect(computeStartEndEquity(meth, [], fmtAmount)).toBeNull();
  });

  it("首點為 0（無法取比值）→ null", () => {
    expect(computeStartEndEquity(meth, ["0", "1.2"], fmtAmount)).toBeNull();
  });
});
