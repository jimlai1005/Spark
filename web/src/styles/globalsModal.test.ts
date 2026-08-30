/**
 * R4-7 — modal 間距回歸測試：ConfirmDialog／CloseAllModal 共用 `.modal-card`／
 * `.modal-overlay`，之前內容貼邊（`.card` 本身不帶 padding）。直接檢查 CSS
 * 規則本身（而非 jsdom computed style，jsdom 不套用外部 stylesheet），
 * 確保 padding／間距不會被之後的改動悄悄移除。
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

describe("globals.css — modal 間距 (R4-7)", () => {
  it(".modal-card 有 24px padding 與 direct-children gap", () => {
    const body = ruleBody(".modal-card {");
    expect(body).toMatch(/padding:\s*24px/);
    expect(body).toMatch(/gap:\s*1[6-9]px|gap:\s*20px/);
  });

  it(".modal-card > * 歸零 margin，避免與 gap 疊加造成雙重間距", () => {
    const body = ruleBody(".modal-card > * {");
    expect(body).toMatch(/margin:\s*0/);
  });

  it(".modal-card .step-actions 覆寫 margin-top 為 0（不與 gap 疊加）", () => {
    const body = ruleBody(".modal-card .step-actions {");
    expect(body).toMatch(/margin-top:\s*0/);
  });
});
