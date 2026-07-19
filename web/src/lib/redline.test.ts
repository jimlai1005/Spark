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

/* ────────────────────────────────────────────────────────────────────────────
 * 績效揭露的結構性防線（2026-07-19 改版後重寫）
 *
 * ⚠️ **舊防線已作廢，不是被刪掉，是前提消失了。** 舊規則是「績效欄位不得接
 * `??`／`||`」，它成立的前提是後端用**鍵的不存在**承載分級揭露（不足 30 天就沒有
 * `twr` 鍵），因此把缺鍵接上預設值＝憑空造出後端刻意不給的數字。
 *
 * 改版後（filet/leader_perf.py 檔頭）資料不足時**照樣回傳數字**，只是附上一組
 * `*_insufficient_data` 標記。於是 `twr`／`max_drawdown`／`annualized_return`
 * 幾乎恆存在——舊規則要擋的那個寫法已經幾乎不可能出現，留著它就是一條**保護不到
 * 任何東西的儀式**：每次跑都綠燈，卻不再對應任何真實風險。
 *
 * 最危險的失敗方向也跟著換邊了：
 *   舊：把後端不給的數字造出來。
 *   新：**把數字送出去而沒有它的警示**。一個 7 天 +3% 的 leader，年化算出來是
 *       365%；數字本身完全「正常」，少掉標記則畫面上一點也看不出它是把 7 天的
 *       雜訊複利放大 52 倍的結果。這種失敗**看起來一切正常**，所以人工審查抓不到，
 *       必須由結構擋。
 *
 * 因此換上兩條**更強**的規則，正對新的失敗方向：
 *   (a) 禁止對標記消音——`??`／`||`／`!` 把 `undefined` 壓成 falsy，等於靜默宣告
 *       「資料充足」，是一句我們沒有根據的保證。
 *   (b) 渲染數字的 scope 必須讀到對應的標記——數字與它的警示不得分家。
 *
 * 兩條都經過**變異測試**驗證：故意寫一段違規的 code 進去確認會轉紅。
 * 一條無法被變異測試證明會咬的紅線，等於不存在。
 * ──────────────────────────────────────────────────────────────────────────── */

/** 指標 → 它**必須**同時出現的標記（缺一不可）。 */
const METRIC_MARKERS: Record<string, string[]> = {
  twr: ["twr_insufficient_data"],
  max_drawdown: ["max_drawdown_insufficient_data"],
  // 年化要兩個標記：不足與否（是否加重警語）＋ 由幾天外推（警語的內容本身）。
  // 少了 extrapolated_from_days 就寫不出「由 N 天外推」，而沒有標明外推基礎的
  // 年化數字正是本次改版最想避免的東西。
  annualized_return: [
    "annualized_return_insufficient_data",
    "annualized_return_extrapolated_from_days",
  ],
};

const ALL_MARKERS = [
  "twr_insufficient_data",
  "max_drawdown_insufficient_data",
  "annualized_return_insufficient_data",
  "annualized_return_extrapolated_from_days",
];

/**
 * 註解與字串字面值 → 等長空白（保留索引，供大括號配對用）。
 *
 * ⭐ 一定要先做這一步，否則兩條規則都會被**文件**騙過去：本專案的檔頭註解大量出現
 * `twr`／`annualized_return ?? "—"` 這類示例文字。掃原文會讓註解裡的一句說明就
 * 「滿足」了標記出現的要求（規則 b 變成寫註解就能繞過），也會讓註解裡的反例把
 * 規則 a 誤判成違規。掃描器只能看**真的會執行的 code**。
 */
function stripNonCode(src: string): string {
  const out = src.split("");
  let i = 0;
  const blank = (from: number, to: number) => {
    for (let k = from; k < to && k < out.length; k++) if (out[k] !== "\n") out[k] = " ";
  };
  while (i < src.length) {
    const two = src.slice(i, i + 2);
    if (two === "//") {
      const end = src.indexOf("\n", i);
      blank(i, end === -1 ? src.length : end);
      i = end === -1 ? src.length : end;
    } else if (two === "/*") {
      const end = src.indexOf("*/", i + 2);
      const stop = end === -1 ? src.length : end + 2;
      blank(i, stop);
      i = stop;
    } else if (src[i] === '"' || src[i] === "'" || src[i] === "`") {
      const quote = src[i];
      let j = i + 1;
      while (j < src.length) {
        if (src[j] === "\\") { j += 2; continue; }
        if (src[j] === quote) break;
        j++;
      }
      blank(i + 1, j);
      i = j + 1;
    } else {
      i++;
    }
  }
  return out.join("");
}

/** 函式主體的 `{` 索引集合（`function f(...) {`、`(...) => {`、含回傳型別註記）。 */
function functionOpenBraces(code: string): Set<number> {
  const set = new Set<number>();
  const patterns = [
    /function\s*[\w$]*\s*\([^()]*\)\s*(?::[^{;=]*)?\{/g,
    /=>\s*\{/g,
  ];
  for (const re of patterns) {
    for (const m of code.matchAll(re)) {
      set.add(m.index! + m[0].length - 1);
    }
  }
  return set;
}

const JSX_TAG = /<\/?[A-Za-z][\w.]*[\s/>]/;

/**
 * 某個位置所屬的 scope 文字。
 *
 * 由內往外找第一個「有意義的」區塊：**函式主體**或**含 JSX 標籤的區塊**。
 * ⭐ 為什麼不直接取最內層大括號：`value={fmtRatioPct(s.twr)}` 的最內層是
 * `{fmtRatioPct(s.twr)}`，那裡面永遠塞不進標記，規則會嚴到無法遵守（而一條無法
 * 遵守的規則最後一定被刪掉）。⭐ 為什麼不直接取整個函式：那樣同一個大型元件裡
 * 只要某處提過標記，其他地方就能白渲染，規則會鬆到擋不住東西。
 * 取「最內層的函式或 JSX 區塊」是兩者之間**真正對應到一次渲染決策**的粒度。
 */
function scopeAround(code: string, idx: number): string {
  const fnOpens = functionOpenBraces(code);
  const stack: number[] = [];
  const enclosing: Array<[number, number]> = [];
  for (let i = 0; i < code.length; i++) {
    if (code[i] === "{") stack.push(i);
    else if (code[i] === "}") {
      const open = stack.pop();
      if (open !== undefined && open < idx && i > idx) enclosing.push([open, i]);
    }
  }
  // enclosing 依關閉順序（由內而外）累積，直接取第一個符合條件者。
  for (const [open, close] of enclosing) {
    const body = code.slice(open + 1, close);
    if (fnOpens.has(open) || JSX_TAG.test(body)) return body;
  }
  return code; // 不在任何區塊內（例如 interface 欄位宣告）→ 整檔為 scope
}

function sourceFiles(): string[] {
  return ROOTS.flatMap((r) => walk(join(process.cwd(), r)));
}

/** `檔案:行號` — idx 是 stripNonCode 後的索引，與原文同長度，行號可直接反推。 */
function lineOf(code: string, idx: number): number {
  return code.slice(0, idx).split("\n").length;
}

describe("紅線 (a)：不得對資料不足標記消音", () => {
  /**
   * `marker ?? false`／`marker || false`／`!marker` 都會把**標記缺席**
   * （`undefined`）壓成一個 falsy 值，而 falsy 在這裡的語意是「資料充足」——
   * 等於替後端說了一句它沒說過的保證。標記缺席的正確語意是「我們不知道」，
   * 而不知道的時候必須 fail closed（見 lib/leaderPerf.ts 的成對到齊規則）。
   *
   * 要判斷充足與否請用顯式比較（`marker === true` ／ `typeof marker === "boolean"`），
   * 讓「缺席」這個第三種狀態無法被悄悄折疊掉。
   */
  it("標記欄位不得接 ??／||／! —— 那等於靜默宣告「資料充足」", () => {
    const markers = ALL_MARKERS.join("|");
    // 1. `marker ?? x` / `marker || x`：用預設值頂掉缺席。
    const coalesce = new RegExp(`\\b(?:${markers})\\b\\s*(?:\\?\\?|\\|\\|)`, "g");
    // 2. `!marker` / `!s.marker`：直接把缺席折疊成 true。`!` 與標記之間只允許
    //    屬性存取（`!isBoolean(x.marker)` 這種先做型別檢查的寫法不在此列）。
    const negate = new RegExp(`![\\s]*[A-Za-z0-9_$.]*\\b(?:${markers})\\b`, "g");
    const hits: string[] = [];
    for (const f of sourceFiles()) {
      const code = stripNonCode(readFileSync(f, "utf8"));
      for (const re of [coalesce, negate]) {
        for (const m of code.matchAll(re)) {
          hits.push(`${f}:${lineOf(code, m.index!)}: ${m[0].trim()}`);
        }
      }
    }
    expect(hits).toEqual([]);
  });
});

describe("紅線 (b)：渲染績效數字的 scope 必須讀到對應的標記", () => {
  /**
   * 數字與它的警示不得分家。改版後 `twr`／`max_drawdown`／`annualized_return`
   * 即使資料極薄也照樣有值，**標記是畫面上唯一看得出這件事的東西**——
   * 一段讀了數字卻沒讀標記的 code，畫出來的必然是一個沒有任何警示的數字，
   * 而那正是這次改版把風險移過去的地方。
   *
   * ⭐ 這條擋的是**結構**而不是文案：它不管你把警語寫成什麼樣子，只要求標記與
   * 數字出現在同一次渲染決策裡。文案可以改、可以翻譯，這條依然有效。
   */
  it.each(Object.entries(METRIC_MARKERS))(
    "%s 出現的每個 scope 都必須讀到它的標記",
    (metric, required) => {
      // \b 在這裡是關鍵：`\btwr\b` **不會**命中 `twr_insufficient_data`
      //（`r` 與 `_` 都是 word 字元，中間沒有邊界），所以標記自身不會被誤判成
      // 「一次未標記的渲染」。
      const re = new RegExp(`\\b${metric}\\b`, "g");
      const hits: string[] = [];
      for (const f of sourceFiles()) {
        const code = stripNonCode(readFileSync(f, "utf8"));
        for (const m of code.matchAll(re)) {
          // ⚠️ 名稱碰撞（唯一的排除項）：`LeaderPerfNotes.max_drawdown` 是 MDD 的
          // **警語原文**，不是 MDD 的數字——後端把警語與指標取了同一個名字。
          // 渲染一句警語不需要不足標記（它本來就是警語），把它算成違規會逼出一個
          // 假的修法。排除條件寫得很窄：只放行 `notes.` ／ `notes?.` 前綴。
          if (/\bnotes\s*\??\.\s*$/.test(code.slice(Math.max(0, m.index! - 40), m.index!))) {
            continue;
          }
          const scope = scopeAround(code, m.index!);
          const missing = required.filter((k) => !scope.includes(k));
          if (missing.length > 0) {
            hits.push(`${f}:${lineOf(code, m.index!)}: 缺少 ${missing.join("／")}`);
          }
        }
      }
      expect(hits).toEqual([]);
    },
  );
});
