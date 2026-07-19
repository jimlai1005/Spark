import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const BANNED = ["固定收益", "保證", "存款", "代操"];
const ROOTS = ["src/app", "src/components", "src/lib"];

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else if (/\.(ts|tsx)$/.test(name) && !/\.test\.(ts|tsx)$/.test(name) && !p.endsWith(join("lib", "copy.ts"))) out.push(p);
  }
  return out;
}

describe("language red-line (file-level)", () => {
  it("使用者可見原始碼不得出現禁詞", () => {
    const files = ROOTS.flatMap((r) => walk(join(process.cwd(), r)));
    const hits: string[] = [];
    for (const f of files) {
      const src = readFileSync(f, "utf8");
      for (const w of BANNED) if (src.includes(w)) hits.push(`${f}: ${w}`);
    }
    expect(hits).toEqual([]);
  });
});

/**
 * ⭐ `/api/leaders` 的 `performance` 子物件用**鍵的不存在**承載後端的分級揭露保證：
 * 樣本不足 30 天就沒有 `twr`／`max_drawdown` 鍵，不足 90 天就沒有 `annualized_return`
 * 鍵（見 filet/leader_perf.py；publicapi/app.py 的 `_leader_perf_public` 刻意用
 * `if k in row` 投影而不是 `.get()`，才不會把缺席的鍵補成 null）。
 *
 * 缺鍵的意思是「不該顯示」，不是「顯示為空」。只要有人寫下 `annualized_return ?? "—"`
 * 或 `twr || 0`，後端用「鍵不存在」換來的那道防線就退化成「前端記得檢查」，而畫面上
 * 會多出一個看起來完全正常、實際上是前端造出來的欄位——那正是本專案不做的事。
 *
 * 這條斷言擋的是**寫法**，不是再加一句提醒（工程原則的 meta-rule：流程漏洞不要用
 * 更多流程補，要用一個繞不過去的結構性強制點）。要顯示績效請走 `disclosure_tier`
 * 分支或 `"annualized_return" in w` 這種二元判斷。
 */
const PERF_FIELDS = ["cum_pnl", "twr", "max_drawdown", "annualized_return"];

describe("leader performance 缺鍵防線（結構性）", () => {
  it("績效欄位不得接預設值（?? 或 ||）——缺鍵是「不顯示」，不是「顯示為空」", () => {
    const files = ROOTS.flatMap((r) => walk(join(process.cwd(), r)));
    const pattern = new RegExp(`\\b(${PERF_FIELDS.join("|")})\\b\\s*(\\?\\?|\\|\\|)`);
    const hits: string[] = [];
    for (const f of files) {
      readFileSync(f, "utf8").split("\n").forEach((line, i) => {
        if (pattern.test(line)) hits.push(`${f}:${i + 1}: ${line.trim()}`);
      });
    }
    expect(hits).toEqual([]);
  });
});
