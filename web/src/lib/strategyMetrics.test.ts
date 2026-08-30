import { describe, expect, it } from "vitest";
import { fmtAmount } from "@/lib/format";
import { computeStartEndEquity, metricText } from "./strategyMetrics";

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
