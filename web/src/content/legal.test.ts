/**
 * content/legal.test.ts — 語言紅線＋結構對稱（Task 12），比照 `lib/copy.test.ts` 對
 * `COPY_ZH`/`COPY_EN` 的驗法，把 legal.ts 當成跟 copy.ts 同等待遇的豁免來源：
 *
 * ⭐ 為什麼不能直接套用 `redline.test.ts` 的禁詞清單（固定收益/保證/存款/代操）：
 * 法務文本本來就需要合法使用「保證」做否定句（「不保證未來結果」「不是損失上限的
 * 保證」）與「保證金」（margin，交易辭彙）——這些是權威文本 spec 的逐字內容，
 * 不是行銷承諾。對 legal.ts 的字串值套用同一顆「保證」關鍵字會產生大量假陽性，
 * 弱化這條規則沒有意義。因此比照 `lib/copy.ts` 被 `redline.test.ts` 的 `walk()`
 * 排除在檔案級掃描之外（copy.ts 的檔頭註解同樣會合法提到禁詞），legal.ts 也放在
 * `web/src/content/`（`redline.test.ts` 的 `ROOTS` 未涵蓋此目錄，自然豁免原始檔掃描）
 * ——但仍對「固定收益／存款／代操」三個不含合法否定用法的詞做值級掃描（不弱化）。
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { LEGAL_EN, LEGAL_ZH } from "./legal";

function allStrings(node: unknown, acc: string[] = []): string[] {
  if (typeof node === "string") acc.push(node);
  else if (node && typeof node === "object") {
    for (const v of Object.values(node)) allStrings(v, acc);
  }
  return acc;
}

function deepKeySet(node: unknown, prefix = "", acc: Set<string> = new Set()): Set<string> {
  if (Array.isArray(node)) {
    node.forEach((v, i) => deepKeySet(v, `${prefix}[${i}]`, acc));
  } else if (node && typeof node === "object") {
    for (const [k, v] of Object.entries(node)) {
      const path = prefix ? `${prefix}.${k}` : k;
      acc.add(path);
      deepKeySet(v, path, acc);
    }
  }
  return acc;
}

const CJK_RE = /[一-鿿]/;

describe("legal.ts 語言紅線（比照 copy.test.ts）", () => {
  it("zh/en 深層 key 完全對稱", () => {
    const zhKeys = deepKeySet(LEGAL_ZH);
    const enKeys = deepKeySet(LEGAL_EN);
    const onlyInZh = [...zhKeys].filter((k) => !enKeys.has(k));
    const onlyInEn = [...enKeys].filter((k) => !zhKeys.has(k));
    expect(onlyInZh, `僅存在於 zh 的 key: ${onlyInZh.join(", ")}`).toEqual([]);
    expect(onlyInEn, `僅存在於 en 的 key: ${onlyInEn.join(", ")}`).toEqual([]);
  });

  it("en 值不含 CJK 字元", () => {
    const offenders = allStrings(LEGAL_EN).filter((s) => CJK_RE.test(s));
    expect(offenders, `含 CJK 字元的 en 文案: ${offenders.join(" | ")}`).toEqual([]);
  });

  it("zh 禁詞零命中（固定收益/存款/代操——「保證」在法務文本中有合法否定用法，見檔頭）", () => {
    const banned = ["固定收益", "存款", "代操"];
    for (const s of allStrings(LEGAL_ZH)) {
      for (const b of banned) {
        expect(s, `文案含禁詞「${b}」: ${s}`).not.toContain(b);
      }
    }
  });

  it("不加「待法律審閱」標注（使用者裁決 4）", () => {
    const joined = [...allStrings(LEGAL_ZH), ...allStrings(LEGAL_EN)].join("\n");
    expect(joined).not.toMatch(/待法律審閱|pending legal review/i);
  });

  it("生效日期已填為 2026-08-28（不是 {{ effectiveDate }} 佔位符）", () => {
    for (const doc of Object.values(LEGAL_ZH)) expect(doc.effectiveDate).toBe("2026-08-28");
    for (const doc of Object.values(LEGAL_EN)) expect(doc.effectiveDate).toBe("2026-08-28");
    const joined = [...allStrings(LEGAL_ZH), ...allStrings(LEGAL_EN)].join("\n");
    expect(joined).not.toContain("{{ effectiveDate }}");
  });

  it("每份文件至少 3 個 section 標題（terms/privacy/risk）", () => {
    for (const [key, doc] of Object.entries(LEGAL_ZH)) {
      expect(doc.sections.length, `${key} 的 section 數`).toBeGreaterThanOrEqual(3);
    }
  });
});

/**
 * ⭐ 與 `redline.test.ts` 的 `walk()` 對照：`content/` 不在其 `ROOTS`
 * （`["src/app", "src/components", "src/lib"]`）內，legal.ts 自然不會被檔案級
 * 禁詞掃描命中——這裡用一個實測斷言釘住這個豁免前提，避免未來有人把 `content/`
 * 加進 ROOTS 卻沒意識到會對合法的「保證」用法產生假陽性。
 */
describe("legal.ts 的檔案級豁免前提（對照 redline.test.ts 的 ROOTS）", () => {
  it("content/ 目錄不在 redline.test.ts 的掃描 ROOTS 內", () => {
    const redlineSrc = readFileSync(
      join(process.cwd(), "src/lib/redline.test.ts"),
      "utf8",
    );
    const rootsMatch = redlineSrc.match(/const ROOTS = (\[[^\]]*\]);/);
    expect(rootsMatch, "redline.test.ts 應含 ROOTS 常數宣告").not.toBeNull();
    const roots = JSON.parse(rootsMatch![1].replace(/'/g, '"')) as string[];
    expect(roots).not.toContain("src/content");
    // 額外確認 legal.ts 檔案本身確實存在於未被涵蓋的路徑（防止路徑重構後失準）。
    const legalPath = join(process.cwd(), "src/content/legal.ts");
    expect(() => statSync(legalPath)).not.toThrow();
    const inScannedRoot = roots.some((r) => legalPath.startsWith(join(process.cwd(), r) + "/"));
    expect(inScannedRoot).toBe(false);
  });

  it("（自我檢查）content/ 目錄下確實只有 legal.ts／legal.test.ts，沒有其他檔案漏檢", () => {
    const dir = join(process.cwd(), "src/content");
    const files = readdirSync(dir).filter((f) => statSync(join(dir, f)).isFile());
    expect(files.sort()).toEqual(["legal.test.ts", "legal.ts"]);
  });
});
