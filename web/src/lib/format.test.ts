import { describe, expect, it } from "vitest";
import { fmtAmount, fmtRatioPct, NO_VALUE, shortAddr } from "./format";

describe("shortAddr", () => {
  it("縮寫成 0x5579…B5d 形式（前 6 + 後 3，照 v1 原型）", () => {
    expect(shortAddr("0x5579C7B45D6a4c30F1B87c2E3d9A8b7c6D5E4B5d")).toBe("0x5579…B5d");
  });
  it("非地址原樣回傳（防禦性，不炸 UI）", () => {
    expect(shortAddr("")).toBe("");
    expect(shortAddr("not-an-address")).toBe("not-an-address");
  });
});

describe("fmtAmount", () => {
  it("千分位 + 2 位小數；小額改 4 位（builder fee 不被捨成 0.00）", () => {
    expect(fmtAmount("12345.6789")).toBe("12,345.68");
    expect(fmtAmount("0.02345")).toBe("0.0235");
    expect(fmtAmount("0")).toBe("0.00");
  });
  it("可指定小數位（對帳欄位固定 4 位，避免小額差額被四捨五入抹掉）", () => {
    expect(fmtAmount("25.0101", 4)).toBe("25.0101");
    expect(fmtAmount("1234.5", 4)).toBe("1,234.5000");
  });
  it("缺值顯示佔位符而非 0（0 與「查不到」意義相反）", () => {
    expect(fmtAmount(null)).toBe(NO_VALUE);
    expect(fmtAmount(undefined)).toBe(NO_VALUE);
    expect(fmtAmount("")).toBe(NO_VALUE);
    expect(fmtAmount("not-a-number")).toBe(NO_VALUE);
  });
});

describe("fmtRatioPct", () => {
  it("比例轉百分比", () => {
    expect(fmtRatioPct("0.25")).toBe("25.0%");
    expect(fmtRatioPct("0.0123", 2)).toBe("1.23%");
  });
  it("null → 佔位符（不顯示 0%）", () => {
    expect(fmtRatioPct(null)).toBe(NO_VALUE);
  });
});
