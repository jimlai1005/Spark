import { describe, expect, it } from "vitest";
import { liqDistanceDisplay } from "@/lib/liqDistance";

// Task 11（S1，2026-09-30）：`liqDistanceDisplay` 單一定義點的行為錨例——
// 兩頁（explore／traders）共用同一份格式化規則，錨值取自 plan Task 11。
describe("liqDistanceDisplay", () => {
  it("null → 「—」、無 class", () => {
    expect(liqDistanceDisplay(null)).toEqual({ text: "—", className: "" });
  });

  it("負值（已越線）→ 顯示 0.0%、danger", () => {
    expect(liqDistanceDisplay(-3)).toEqual({ text: "0.0%", className: "risk-num--danger" });
  });

  it("14.9（< 15）→ danger", () => {
    expect(liqDistanceDisplay(14.9)).toEqual({ text: "14.9%", className: "risk-num--danger" });
  });

  it("15（門檻本身）→ warn（不是 danger）", () => {
    expect(liqDistanceDisplay(15)).toEqual({ text: "15.0%", className: "risk-num--warn" });
  });

  it("29.9（< 30）→ warn", () => {
    expect(liqDistanceDisplay(29.9)).toEqual({ text: "29.9%", className: "risk-num--warn" });
  });

  it("30（門檻本身）→ 無 class（不變色）", () => {
    expect(liqDistanceDisplay(30)).toEqual({ text: "30.0%", className: "" });
  });

  it("27.5 → warn", () => {
    expect(liqDistanceDisplay(27.5)).toEqual({ text: "27.5%", className: "risk-num--warn" });
  });
});
