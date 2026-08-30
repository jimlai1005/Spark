import { describe, expect, it } from "vitest";
import { fmtAmount } from "@/lib/format";
import { formatDepositEquivalentEquity, metricText } from "./strategyMetrics";

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

// M3 round4 Task R4-8（2026-08-31 使用者裁決）：起訖淨值卡改回與淨值曲線
// （equity_index）同一基準——deposit × (equity_index 末值 / 首值)，TWR 等效
// 淨值。見 strategyMetrics.ts 檔頭。
describe("formatDepositEquivalentEquity", () => {
  it("錨例：deposit 1000、equity_index 首 1 末 1.1484 → 1,000 → 1,148", () => {
    const r = formatDepositEquivalentEquity("1000", ["1", "1.1484"], fmtAmount);
    expect(r).toEqual({ start: "1,000", end: "1,148" });
  });

  it("正常換算：deposit 500、index 首 2 末 2.5（比值 1.25）→ 500 → 625", () => {
    const r = formatDepositEquivalentEquity("500", ["2", "2.2", "2.5"], fmtAmount);
    expect(r).toEqual({ start: "500", end: "625" });
  });

  it("deposit 缺席 → null", () => {
    expect(formatDepositEquivalentEquity(null, ["1", "1.1"], fmtAmount)).toBeNull();
  });

  it("deposit <= 0 → null", () => {
    expect(formatDepositEquivalentEquity("0", ["1", "1.1"], fmtAmount)).toBeNull();
    expect(formatDepositEquivalentEquity("-5", ["1", "1.1"], fmtAmount)).toBeNull();
  });

  it("equity_index 少於 2 點 → null", () => {
    expect(formatDepositEquivalentEquity("1000", [], fmtAmount)).toBeNull();
    expect(formatDepositEquivalentEquity("1000", ["1"], fmtAmount)).toBeNull();
  });

  it("equity_index 首值非有限或 <=0 → null（防禦性，不假設恆為 1）", () => {
    expect(formatDepositEquivalentEquity("1000", ["0", "1.1"], fmtAmount)).toBeNull();
    expect(formatDepositEquivalentEquity("1000", ["abc", "1.1"], fmtAmount)).toBeNull();
  });
});
