/**
 * R4-11 — 面板 CTA/小字間距＋雙值卡排版回歸測試：直接檢查 CSS 規則本身
 * （而非 jsdom computed style，jsdom 不套用外部 stylesheet），確保這兩項
 * 使用者回饋不會在之後的改動中悄悄被移除（沿 `globalsModal.test.ts` 同款
 * 檢查手法）。
 */
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const css = fs.readFileSync(
  path.resolve(__dirname, "globals.css"),
  "utf-8",
);

function ruleBody(selector: string): string {
  const bare = selector.replace(/\s*\{$/, "");
  const escaped = bare.replace(/[.[\]*]/g, (m) => `\\${m}`);
  const re = new RegExp(`${escaped}\\s*\\{([^}]*)\\}`);
  const match = css.match(re);
  if (!match) throw new Error(`CSS rule not found: ${selector}`);
  return match[1];
}

describe("globals.css — 項目 2：CTA 與下方小字間距 (R4-11)", () => {
  it(".strategy-follow-footnote 有正的 margin-top（不再貼著 CTA）", () => {
    const body = ruleBody(".strategy-follow-footnote {");
    const match = body.match(/margin-top:\s*(\d+)px/);
    expect(match).not.toBeNull();
    expect(Number(match?.[1])).toBeGreaterThanOrEqual(12);
    expect(Number(match?.[1])).toBeLessThanOrEqual(16);
  });
});

describe("globals.css — 項目 3：雙值卡排版 (R4-11)", () => {
  it(".metric-card-pair .metric-card-value 預設單行不換行、字級降一階", () => {
    const body = ruleBody(".metric-card-pair .metric-card-value {");
    expect(body).toMatch(/white-space:\s*nowrap/);
    expect(body).toMatch(/font-size:\s*var\(--fs-body-lg\)/);
  });

  it("390 斷點（max-width: 480px）內有 .metric-card-pair 改上下兩行的規則", () => {
    // globals.css 有多個 `@media (max-width: 480px)` 區塊——直接找「規則本身在
    // 某個 480px 區塊內」比切割字串更穩：抓規則本體，確認緊鄰其前的最近一個
    // `@media (max-width: 480px) {` 早於規則、且規則早於該區塊的收尾 `}`。
    const ruleMatch = css.match(/\.metric-card-pair \.metric-card-value\s*\{\s*flex-direction:\s*column[^}]*\}/);
    expect(ruleMatch).not.toBeNull();
    const ruleIndex = ruleMatch?.index ?? -1;
    const precedingMediaIndex = css.lastIndexOf("@media (max-width: 480px)", ruleIndex);
    expect(precedingMediaIndex).toBeGreaterThan(-1);
    // 確認兩者之間沒有夾著另一個頂層 `@media`（代表規則真的在這個 480px 區塊內，
    // 不是巧合落在更早的區塊之後、更晚的另一個 @media 之前）。
    const between = css.slice(precedingMediaIndex + 1, ruleIndex);
    expect(between).not.toMatch(/\n@media/);
  });
});
