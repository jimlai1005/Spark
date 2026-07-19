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
 * JSX 元素的區間 `[「<」的索引, 「>」的索引]`——配對 `<Tag>…</Tag>` 與自閉合
 * `<Tag … />` 都算。
 *
 * ⭐⭐ 2026-07-20 新增，補一個**實測會逃逸**的洞。舊 `scopeAround` 只認大括號區塊
 * （函式主體／含 JSX 標籤者），而 `<dd>{fmtRatioPct(s.twr)}</dd>` 這種寫法兩者都不是：
 * 表達式容器 `{fmtRatioPct(s.twr)}` 內部沒有標籤所以不合格，JSX 元素本身又不是大括號
 * 分隔的——於是 scope 一路往外逃到整個元件函式主體，而該主體別處已提過標記，
 * 違規就被吸收了。實測：在既有元件內插入一格無標記的數字，紅線 (b) 不會咬。
 * 那正是本檔 209 行原本宣稱「不直接取整個函式」所要避免的失效，實際上卻發生了。
 *
 * 把 JSX 元素本身納入邊界之後，`<dd>` 成為最內層 scope，違規無處可逃。
 *
 * ⚠️ 泛型不是 JSX：`Set<number>`／`Partial<LeaderPerfWindow>` 的 `<` 前面是 word
 * 字元，真正的 JSX 前面只會是空白／`(`／`{`／`>`／運算子。用這點把兩者分開，否則
 * .ts 檔的型別註記會被當成標籤，切出假的（過小的）scope 而誤報。
 */
function jsxSpans(code: string): Array<[number, number]> {
  const spans: Array<[number, number]> = [];
  const open: Array<{ name: string; start: number }> = [];
  let i = 0;
  while (i < code.length) {
    if (code[i] !== "<" || (i > 0 && /[\w$)\]]/.test(code[i - 1]))) { i++; continue; }
    const m = /^<(\/?)([A-Za-z][\w.]*)[\s/>]/.exec(code.slice(i, i + 200));
    if (m === null) { i++; continue; }
    // 標籤的結尾 `>`：屬性值裡的 `{…}` 可能含 `>`（`onClick={() => …}`），
    // 所以只認大括號深度 0 的那個。
    let depth = 0;
    let end = -1;
    for (let j = i + m[0].length - 1; j < code.length; j++) {
      const ch = code[j];
      if (ch === "{") depth++;
      else if (ch === "}") depth = Math.max(0, depth - 1);
      else if (ch === ">" && depth === 0) { end = j; break; }
    }
    if (end === -1) { i++; continue; }
    if (m[1] === "/") {
      for (let k = open.length - 1; k >= 0; k--) {
        if (open[k].name === m[2]) { spans.push([open[k].start, end]); open.length = k; break; }
      }
    } else if (code[end - 1] === "/") spans.push([i, end]);
    else open.push({ name: m[2], start: i });
    i = end + 1;
  }
  return spans;
}

/**
 * 某個位置所屬的 scope 文字＝所有包住它的邊界裡**最小**的那個。
 *
 * 邊界有兩類：**大括號區塊**（函式主體，或內含 JSX 標籤者）與 **JSX 元素**。
 * ⭐ 為什麼不直接取最內層大括號：`value={fmtRatioPct(s.twr)}` 的最內層是
 * `{fmtRatioPct(s.twr)}`，那裡面永遠塞不進標記，規則會嚴到無法遵守（而一條無法
 * 遵守的規則最後一定被刪掉）。這種「屬性值容器」的正確粒度是它所屬的**整個 JSX
 * 元素**——`<PerfStat value={…s.twr} marker={…s.twr_insufficient_data} />` 的屬性群組
 * 就是一次完整的渲染決策，數字與標記本來就該在這裡碰頭。
 * ⭐ 為什麼不直接取整個函式：那樣同一個大型元件裡只要某處提過標記，其他地方就能
 * 白渲染。⚠️ 舊版**取最內層合格大括號**看似避開了這點，實測卻仍會退化成整個函式
 * （見 `jsxSpans` 檔頭），所以現在改成「兩類邊界一起取最小」。
 */
function scopeAround(code: string, idx: number): string {
  const fnOpens = functionOpenBraces(code);
  const stack: number[] = [];
  const enclosing: Array<[number, number]> = [];
  for (let i = 0; i < code.length; i++) {
    if (code[i] === "{") stack.push(i);
    else if (code[i] === "}") {
      const start = stack.pop();
      if (start === undefined || start >= idx || i <= idx) continue;
      const body = code.slice(start + 1, i);
      if (fnOpens.has(start) || JSX_TAG.test(body)) enclosing.push([start + 1, i]);
    }
  }
  for (const [start, end] of jsxSpans(code)) {
    if (start < idx && end > idx) enclosing.push([start, end + 1]);
  }
  if (enclosing.length === 0) return code; // 不在任何區塊內（例如 interface 欄位宣告）
  enclosing.sort((a, b) => a[1] - a[0] - (b[1] - b[0]));
  return code.slice(enclosing[0][0], enclosing[0][1]);
}

function sourceFiles(): string[] {
  return ROOTS.flatMap((r) => walk(join(process.cwd(), r)));
}

/** `檔案:行號` — idx 是 stripNonCode 後的索引，與原文同長度，行號可直接反推。 */
function lineOf(code: string, idx: number): number {
  return code.slice(0, idx).split("\n").length;
}

/**
 * 這個檔案裡**確定是 `PerfShown`** 的識別字（`const s = view.shown` 的 `s`、
 * 任何 `x: PerfShown` 的 `x`）。
 *
 * ⭐ 為什麼需要它：`?:` 與 `&&` 對標記**不是無條件有害**。`perfView()` 是唯一的邊界，
 * 它強制「數字與標記成對到齊」——`PerfShown` 上標記缺席時，那個數字也一定不在，
 * 整格根本不會渲染。所以讀 `PerfShown` 欄位時 `s.marker && <警示/>` 是安全的。
 * 但**直接讀原始 window**（`w.marker`，還沒過邊界）就沒有這個保證：`undefined`
 * 會靜靜地折進 falsy 分支＝「資料充足」。同一個算子，安全與否取決於讀的是誰。
 */
function perfShownRoots(code: string): Set<string> {
  const roots = new Set<string>();
  for (const m of code.matchAll(
    /\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=;\n]*)?=\s*[^;\n]*\.shown\b/g,
  )) roots.add(m[1]);
  for (const m of code.matchAll(/\b([A-Za-z_$][\w$]*)\s*:\s*PerfShown\b/g)) roots.add(m[1]);
  return roots;
}

describe("紅線 (a)：不得對資料不足標記消音", () => {
  /**
   * `marker ?? false`／`marker || false`／`!marker`／`!!marker`／`Boolean(marker)`／
   * `marker ? a : b`／`marker && x` 都會把**標記缺席**（`undefined`）壓成 falsy，
   * 而 falsy 在這裡的語意是「資料充足」——等於替後端說了一句它沒說過的保證。
   * 標記缺席的正確語意是「我們不知道」，不知道的時候必須 fail closed
   * （見 lib/leaderPerf.ts 的成對到齊規則）。
   *
   * ⚠️ 2026-07-20：算子清單原本只有 `??`／`||`／`!`，實測 `Boolean(marker)`、
   * `marker ? a : b`、`marker && x` **三種等價寫法全數被放行**。三者折疊 `undefined`
   * 的方式與 `?? false` 完全相同，卻走了不同的語法路徑——一條只擋得住其中一種寫法的
   * 紅線，擋住的是拼字而不是語意。
   *
   * 分兩級是刻意的：
   *   - **無條件禁止** `??`／`||`／`!`／`!!`／`Boolean()`：這些對標記沒有安全用法，
   *     要判斷充足與否一律用顯式比較（`=== true`／`typeof === "boolean"`），
   *     讓「缺席」這個第三種狀態無法被悄悄折疊掉。
   *   - **僅限 `PerfShown`** `?:`／`&&`：它們是 React 最慣用的條件渲染寫法，過了
   *     `perfView()` 邊界之後確實安全（見 `perfShownRoots`）。全面禁止會逼所有 JSX
   *     改寫成不自然的形狀，而一條無法遵守的規則最後一定被刪掉。
   */
  it("標記欄位不得接 ??／||／!／!!／Boolean()；?: 與 && 僅限 PerfShown", () => {
    const M = `(?:${ALL_MARKERS.join("|")})`;
    // 無條件禁止。`!` 與標記之間只允許屬性存取（`!isBoolean(x.marker)` 這種
    // 先做型別檢查的寫法不在此列）；`!!marker` 由同一式的第二個 `!` 命中。
    const always: RegExp[] = [
      new RegExp(`\\b${M}\\b\\s*(?:\\?\\?|\\|\\|)`, "g"),
      new RegExp(`![\\s]*[A-Za-z0-9_$.]*\\b${M}\\b`, "g"),
      new RegExp(`\\bBoolean\\s*\\(\\s*[A-Za-z0-9_$.]*\\b${M}\\b`, "g"),
    ];
    // 僅限 PerfShown：捕捉接收者前綴（`s.`／`w.`／空）以便判定。
    // ⚠️ `\?(?!\s*[?.:])` 把 `??`（上面已擋）、`?.`（選擇性串接）與 `marker?: boolean`
    //（介面的選擇性欄位宣告，api.ts／leaderPerf.ts 都有）排除在三元之外。
    const guarded: RegExp[] = [
      new RegExp(`([A-Za-z0-9_$.]*)\\b${M}\\b\\s*\\?(?!\\s*[?.:])`, "g"),
      new RegExp(`([A-Za-z0-9_$.]*)\\b${M}\\b\\s*&&`, "g"),
    ];
    const hits: string[] = [];
    for (const f of sourceFiles()) {
      const code = stripNonCode(readFileSync(f, "utf8"));
      for (const re of always) {
        for (const m of code.matchAll(re)) {
          hits.push(`${f}:${lineOf(code, m.index!)}: ${m[0].trim()}`);
        }
      }
      const shownRoots = perfShownRoots(code);
      for (const re of guarded) {
        for (const m of code.matchAll(re)) {
          const root = m[1].replace(/\.$/, "").split(".")[0];
          if (root !== "" && shownRoots.has(root)) continue;
          hits.push(`${f}:${lineOf(code, m.index!)}: ${m[0].trim()}（非 PerfShown 來源）`);
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
