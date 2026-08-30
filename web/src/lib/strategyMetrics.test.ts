import { describe, expect, it } from "vitest";
import { fmtAmount } from "@/lib/format";
import { formatStartEndEquity, metricText } from "./strategyMetrics";

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

// M3 round4 Task R4-2：`computeStartEndEquity`（`initial_deposit_usd` ×
// `equity_index` 比值換算）已移除——後端直接供給 `start_equity_usd`／
// `end_equity_usd`（同一份 accountValueHistory 首個非零值與末值），前端只做
// 格式化，見 strategyMetrics.ts 檔頭。
describe("formatStartEndEquity", () => {
  const meth = {
    start_equity_usd: "1000", end_equity_usd: "1200",
  } as Parameters<typeof formatStartEndEquity>[0];

  it("正常格式化起訖淨值", () => {
    const r = formatStartEndEquity(meth, fmtAmount);
    expect(r).toEqual({ start: fmtAmount("1000", 0), end: fmtAmount("1200", 0) });
  });

  it("缺 start_equity_usd → null", () => {
    const r = formatStartEndEquity(
      { start_equity_usd: null, end_equity_usd: "1200" } as Parameters<
        typeof formatStartEndEquity
      >[0],
      fmtAmount,
    );
    expect(r).toBeNull();
  });

  it("缺 end_equity_usd → null", () => {
    const r = formatStartEndEquity(
      { start_equity_usd: "1000", end_equity_usd: null } as Parameters<
        typeof formatStartEndEquity
      >[0],
      fmtAmount,
    );
    expect(r).toBeNull();
  });
});
