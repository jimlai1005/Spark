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

  it("資金轉出警示存在於 wizard 與跟單頁文案", () => {
    expect(COPY.wizard.fundsWarning).toMatch(/perp/);
    expect(COPY.wizard.fundsWarning).toMatch(/轉出/);
    // ⭐ 2026-07-30：/performance 頁下架，此警語搬到 /leaders（客戶查看與管理
    // 跟單狀態的地方），與 wizard 開通頁的同義句各自成立、互不取代。
    expect(COPY.leaders.fundsWarning).toMatch(/轉出/);
  });
});
