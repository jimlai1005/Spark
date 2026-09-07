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
  it(".metric-card-pair .metric-card-value 預設單行不換行、字級降一階（上限 --fs-body-lg，隨卡寬縮放）", () => {
    const body = ruleBody(".metric-card-pair .metric-card-value {");
    expect(body).toMatch(/white-space:\s*nowrap/);
    // 2026-09-05 防折行改版：字級改為 `min(var(--fs-body-lg), Ncqw)`——上限仍是
    // 降一階的 17px，卡片內容寬不夠時隨寬度縮，不允許溢出。
    expect(body).toMatch(/font-size:\s*min\(var\(--fs-body-lg\),\s*[\d.]+cqw\)/);
  });

  it("窄卡（@container max-width ≤ 170px）內有 .metric-card-pair 改上下兩行的規則", () => {
    // 2026-09-05 防折行改版：上下兩行規則從 `@media (max-width: 480px)` 搬到
    // `.metric-card` 的 container query（量卡片實際寬度，不猜 viewport）。
    // 抓規則本體，確認緊鄰其前的最近一個 `@container (max-width: …px)` 早於規則、
    // 且兩者之間沒夾另一個頂層 at-rule。
    const ruleMatch = css.match(/\.metric-card-pair \.metric-card-value\s*\{\s*flex-direction:\s*column[^}]*\}/);
    expect(ruleMatch).not.toBeNull();
    const ruleIndex = ruleMatch?.index ?? -1;
    const precedingContainerIndex = css.lastIndexOf("@container (max-width:", ruleIndex);
    expect(precedingContainerIndex).toBeGreaterThan(-1);
    const header = css.slice(precedingContainerIndex, ruleIndex);
    const px = header.match(/@container \(max-width:\s*(\d+)px\)/);
    expect(Number(px?.[1])).toBeLessThanOrEqual(170);
    const between = css.slice(precedingContainerIndex + 1, ruleIndex);
    expect(between).not.toMatch(/\n@(media|container)/);
  });

  it(".metric-card 是 inline-size 容器、值 nowrap（2026-09-05 防折行）", () => {
    expect(ruleBody(".metric-card {")).toMatch(/container-type:\s*inline-size/);
    const value = ruleBody(".metric-card-value {");
    expect(value).toMatch(/white-space:\s*nowrap/);
    expect(value).toMatch(/font-size:\s*min\(var\(--fs-h3\),\s*[\d.]+cqw\)/);
  });
});
