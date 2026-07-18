import { describe, expect, it } from "vitest";

describe("offline guard", () => {
  it("未 mock 的 fetch 會直接炸（測試離線是結構保證）", () => {
    expect(() => fetch("https://example.com")).toThrow(/禁止真實網路呼叫/);
  });
});
