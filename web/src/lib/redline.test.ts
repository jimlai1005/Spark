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
