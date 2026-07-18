import { describe, expect, it } from "vitest";
import { COPY } from "./copy";

function allStrings(node: unknown, acc: string[] = []): string[] {
  if (typeof node === "string") acc.push(node);
  else if (node && typeof node === "object") {
    for (const v of Object.values(node)) allStrings(v, acc);
  }
  return acc;
}

describe("語言紅線（spec 不變量 4：2026-06-18 沿用）", () => {
  it("全部文案禁詞零命中：固定收益/保證/存款/代操", () => {
    const banned = ["固定收益", "保證", "存款", "代操"];
    for (const s of allStrings(COPY)) {
      for (const b of banned) {
        expect(s, `文案含禁詞「${b}」: ${s}`).not.toContain(b);
      }
    }
  });

  it("反釣魚聲明存在（紅線 1）", () => {
    const joined = allStrings(COPY).join("\n");
    expect(joined).toContain("永遠不會請你輸入私鑰或助記詞");
  });

  it("非託管核心句存在（無法動用或提領）", () => {
    const joined = allStrings(COPY).join("\n");
    expect(joined).toContain("無法動用或提領");
  });
});
