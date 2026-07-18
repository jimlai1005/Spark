import { describe, expect, it } from "vitest";
import { shortAddr } from "./format";

describe("shortAddr", () => {
  it("縮寫成 0x5579…B5d 形式（前 6 + 後 3，照 v1 原型）", () => {
    expect(shortAddr("0x5579C7B45D6a4c30F1B87c2E3d9A8b7c6D5E4B5d")).toBe("0x5579…B5d");
  });
  it("非地址原樣回傳（防禦性，不炸 UI）", () => {
    expect(shortAddr("")).toBe("");
    expect(shortAddr("not-an-address")).toBe("not-an-address");
  });
});
