import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

/**
 * 公開頁瀏覽器 smoke — plan `docs/superpowers/plans/2026-09-02-golive-regression.md` T5。
 *
 * 前置（主線程另外起，本檔不起任何 server，見 `playwright.config.ts`）：
 *   1. FastAPI（testnet env + tmp db/exchange-dir/state-base + `leaders.json`
 *      複製自 `deploy/leaders.json.example`）綁在 127.0.0.1:8700
 *      （`web/next.config.ts` 的 `/api/*` rewrite 固定打這個位址）。
 *   2. `NEXT_PUBLIC_SITE_ORIGIN=http://127.0.0.1:3100 npm run build && npx next start -p 3100`。
 *   3. `E2E_BASE_URL=http://127.0.0.1:3100 npx playwright test`（或直接跑
 *      `npm run test:e2e`，config 預設 baseURL 落在 3000，用 env 覆寫）。
 *
 * 路由清單不硬編：從 `src/app/**\/page.tsx` glob 出全部路由，依「已知類別」
 * 分流。新增一個沒有登入/管理員閘門、非動態段的公開頁會自動落進通用內容檢查，
 * 不需要改這份清單；分類是否遺漏由檔尾的守門測試把關。
 */

const APP_DIR = path.join(__dirname, "..", "src", "app");

interface PageEntry {
  route: string;
  file: string;
}

function collectPages(dir: string, base = ""): PageEntry[] {
  const out: PageEntry[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      out.push(...collectPages(path.join(dir, entry.name), `${base}/${entry.name}`));
    } else if (entry.name === "page.tsx") {
      out.push({ route: base || "/", file: path.join(dir, entry.name) });
    }
  }
  return out;
}

const ALL_PAGES = collectPages(APP_DIR).sort((a, b) => a.route.localeCompare(b.route));

/** 管理員專用頁：後端 `_require_admin` 結構性授權（`app/admin/page.tsx`、
 * `app/ops/page.tsx` 皆無 `<h1>`，未登入/非管理員只顯示一行文字）。不在
 * CLAUDE.md 的公開路由清單、Header 也不對訪客/一般會員渲染連結——需要管理員
 * 登入態才有意義，T5 範圍外（不覆蓋）。*/
const ADMIN_GATED = new Set(["/admin", "/ops"]);

/** 登入閘門頁：`useMe()` 為 null 時各自在 `useEffect` 導向 `/strategies`
 * （dashboard/page.tsx:105-110、settings/page.tsx:762-767、onboarding/page.tsx:97-101）。
 * plan 明講只驗 `/dashboard` 的未登入行為；這裡連同 settings/onboarding 一併驗
 * （同一種 guard 模式，零額外風險，超出 plan 明文但仍在「常規工程判斷權」內——
 * 已登入內容仍完全交給 T6／既有 vitest，這裡只驗未登入導向 + 零 console error）。*/
const LOGIN_REDIRECT_GATED = new Set(["/dashboard", "/settings", "/onboarding"]);

/** 動態段：`/strategies/[slug]` 需要向 `/api/public/strategies` 取真實 slug
 * （plan 明講），走專屬 test，不進通用迴圈。*/
const DYNAMIC_NEEDS_API = new Set(["/strategies/[slug]"]);

/** 其餘動態段：用合法格式的假位址即可驗證頁面骨架——`/traders/[address]`
 * 對查無資料的位址有獨立的 not-found `<h1>` 分支（`traders/[address]/page.tsx:116`），
 * 不需要真實鏈上資料也能驗證「頁面渲染成功、無 console error」。*/
const DYNAMIC_SYNTHETIC: Record<string, string> = {
  "/traders/[address]": "/traders/0x1111111111111111111111111111111111111111",
};

const CJK = /[一-鿿]/;

/** 語言切換鈕固定顯示原生語言名稱（`LANG_LABELS.zh === "繁中"`，見
 * `web/src/lib/copy.ts:15`），不隨 `useCopy()` 的當前語言改變——這是刻意設計
 * （切換鈕本身用兩種語言的原生名稱標示，不是翻譯殘留）。EN 模式的 CJK 檢查
 * 排除這個容器（沿用 `web/src/app/dashboard/enNoCjk.test.tsx` 等既有 vitest 慣例：
 * 同一份 `CJK = /[一-鿿]/`、同樣用 `textContent`；差別只在這裡多一個 Header
 * 一定會渲染、需要顯式排除語言切換鈕）。*/
const LANG_TOGGLE_SELECTOR = ".lang-toggle";

function findRedirectTarget(file: string): string | null {
  const src = readFileSync(file, "utf8");
  const m = src.match(/router\.replace\(\s*"([^"]+)"\s*\)/);
  return m ? m[1] : null;
}

type Category = "admin" | "login-redirect" | "dynamic-api" | "redirect" | "content";

const CATEGORY = new Map<string, Category>();
const REDIRECT_ROUTES = new Map<string, string>();

for (const p of ALL_PAGES) {
  if (ADMIN_GATED.has(p.route)) {
    CATEGORY.set(p.route, "admin");
    continue;
  }
  if (LOGIN_REDIRECT_GATED.has(p.route)) {
    CATEGORY.set(p.route, "login-redirect");
    continue;
  }
  if (DYNAMIC_NEEDS_API.has(p.route)) {
    CATEGORY.set(p.route, "dynamic-api");
    continue;
  }
  const target = findRedirectTarget(p.file);
  if (target) {
    REDIRECT_ROUTES.set(p.route, target);
    CATEGORY.set(p.route, "redirect");
    continue;
  }
  CATEGORY.set(p.route, "content");
}

const GENERIC_CONTENT_ROUTES = ALL_PAGES
  .filter((p) => CATEGORY.get(p.route) === "content")
  .map((p) => DYNAMIC_SYNTHETIC[p.route] ?? p.route);

// ---------- 分類完整性守門 ----------

test.describe("路由分類完整性（防新頁面悄悄漏檢）", () => {
  test("src/app 底下每個 page.tsx 都已落進某一類", () => {
    expect(CATEGORY.size, "每個 route 必須恰好被分類一次").toBe(ALL_PAGES.length);
  });

  test("已知的排除/特例路由仍然存在（防目錄改名讓排除清單靜默失效）", () => {
    const known = [...ADMIN_GATED, ...LOGIN_REDIRECT_GATED, ...DYNAMIC_NEEDS_API,
      ...Object.keys(DYNAMIC_SYNTHETIC)];
    const routeNames = ALL_PAGES.map((p) => p.route);
    for (const r of known) {
      expect(routeNames, `已知路由消失: ${r}`).toContain(r);
    }
  });
});

// ---------- 共用 helper ----------

interface Listeners {
  consoleErrors: string[];
  pageErrors: string[];
}

/**
 * 白名單只有一條，範圍鎖到「這個 URL＋這個狀態碼」：`Header` 在**每個**頁面都會
 * 呼叫 `useMe()` 探測登入態（`GET /api/me`），401＝正常的「未登入」訊號，程式碼
 * 已經把它當合法狀態處理（不是例外路徑，見 `web/src/lib/hooks.ts` 檔頭「401 →
 * null，不當錯誤」）。這則 console 訊息不是我們的 JS 丟出的 `console.error()`，
 * 是 Chromium 對任何非 2xx 網路回應的內建鏡射（`msg.location().url` 精準指到
 * `/api/me`，不是含糊的「看起來像第三方」）——每個訪客頁面首次載入都會出現，
 * 若不排除，等於整份 smoke 永遠是紅的，而紅的原因與頁面本身是否正常無關。
 * 這是唯一一條白名單，且鎖到 URL 尾碼＋狀態碼字串雙重比對，不放寬成「忽略所有
 * 401」或「忽略所有 console error」。
 */
function isWhitelistedConsoleError(msg: { text(): string; location(): { url: string } }): boolean {
  return msg.location().url.endsWith("/api/me")
    && msg.text().includes("responded with a status of 401");
}

function attachErrorListeners(page: Page): Listeners {
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error" && !isWhitelistedConsoleError(msg)) {
      consoleErrors.push(msg.text());
    }
  });
  page.on("pageerror", (err) => pageErrors.push(String(err)));
  return { consoleErrors, pageErrors };
}

function expectNoErrors(route: string, { consoleErrors, pageErrors }: Listeners) {
  expect(consoleErrors, `${route} console errors: ${consoleErrors.join(" | ")}`).toEqual([]);
  expect(pageErrors, `${route} page errors: ${pageErrors.join(" | ")}`).toEqual([]);
}

/** 通用公開頁檢查：200、有 h1、零 console error/pageerror、無「Application error」
 * 文字、EN 模式下（`localStorage.filet_lang=en`，沿用 `web/src/lib/lang.tsx` 的
 * 真實 storage key）body 文字（排除語言切換鈕）不含 CJK。*/
async function assertPublicPage(page: Page, route: string) {
  await page.addInitScript(() => {
    window.localStorage.setItem("filet_lang", "en");
  });
  const listeners = attachErrorListeners(page);

  const response = await page.goto(route, { waitUntil: "networkidle" });
  expect(response?.status(), `${route} 應回 200`).toBe(200);

  await expect(page.locator("h1").first()).toBeVisible();
  await expect(page.getByText(/Application error/i)).toHaveCount(0);

  // 等語言切換真的生效（`useEffect` 讀 localStorage 之後才會把 aria-pressed 翻成 true），
  // 避免 hydration 尚未完成就抓文字造成偶發性假陰性。
  await expect(page.locator(`${LANG_TOGGLE_SELECTOR} button`, { hasText: "EN" }))
    .toHaveAttribute("aria-pressed", "true");

  const bodyTextExLangToggle = await page.evaluate((sel) => {
    const clone = document.body.cloneNode(true) as HTMLElement;
    clone.querySelectorAll(sel).forEach((el) => el.remove());
    // Next.js RSC streaming payload（`<script>self.__next_f.push(...)`）與 SSR 用的
    // `<style>` 標籤內容也會被 `textContent` 遞迴撈進來，但那不是「畫面上看得到的
    // 文字」——RSC payload 裡帶著頁面固定（未在地化）的 SEO metadata（title/
    // description，layout.tsx 硬編中文，本來就不受 `filet_lang` 影響），會製造假陽性。
    // 動態路由（`ƒ`，非靜態預渲染，例如 `/traders/[address]`）用 React 19 的
    // streaming metadata：`<title>`/`<meta>` 初期會暫放進 `<body>` 裡一個
    // `hidden` 容器（`<div hidden><template>...` outlet），稍後才由 client
    // 端搬進 `<head>`——搬移完成前讀到的話會把固定中文 SEO metadata 誤判成
    // 「畫面上的殘留中文」，實際上使用者從未看到它（`hidden` 屬性）。
    // 動態路由（`/traders/[address]`）觀察到 Next 15 的串流 metadata 有時把
    // `<title>` 留在 `<body>` 內（不在 `hidden` 容器裡、也未被搬進 `<head>`）——
    // `<title>` 本身無論放在文件哪裡都不會被瀏覽器畫出來（user-agent stylesheet
    // `display: none`），排除它與排除 `<script>` 是同一個理由：不是「畫面上的
    // 殘留中文」，是 Next.js 內部 metadata 管線的既有行為（詳見本次 T5 報告的
    // 「觀察」一節，非本檔職責修）。
    clone.querySelectorAll("script, style, template, title, [hidden]").forEach((el) => el.remove());
    return clone.textContent ?? "";
  }, LANG_TOGGLE_SELECTOR);
  expect(bodyTextExLangToggle, `${route} EN 模式殘留 CJK: ${bodyTextExLangToggle}`).not.toMatch(CJK);

  expectNoErrors(route, listeners);
}

// ---------- 通用公開頁內容檢查 ----------

for (const route of GENERIC_CONTENT_ROUTES) {
  test(`public page: ${route}`, async ({ page }) => {
    await assertPublicPage(page, route);
  });
}

// ---------- 動態段：/strategies/[slug] 用 API 真實第一個 slug ----------

test("/strategies/[slug] — 用 /api/public/strategies 第一個真實 slug 渲染", async ({ page, request }) => {
  const res = await request.get("/api/public/strategies");
  expect(res.status()).toBe(200);
  const body = await res.json();
  const first = body.strategies?.[0];
  expect(first, "測試資料需要至少一個 enabled 策略（見 deploy/leaders.json.example）")
    .toBeTruthy();
  await assertPublicPage(page, `/strategies/${first.slug}`);
});

// ---------- 舊路由 client-side redirect（自動偵測 router.replace 目標） ----------

for (const [route, target] of REDIRECT_ROUTES) {
  test(`${route} — client-side redirect 落到 ${target}`, async ({ page }) => {
    const listeners = attachErrorListeners(page);
    const response = await page.goto(route);
    expect(response?.status(), `${route} 首次載入應回 200（redirect 是 client-side）`).toBe(200);
    await page.waitForURL((url) => url.pathname === target, { timeout: 10_000 });
    expect(page.url()).toContain(target);
    expectNoErrors(route, listeners);
  });
}

// ---------- 登入閘門頁：未登入應導向 /strategies ----------

for (const route of LOGIN_REDIRECT_GATED) {
  test(`${route} — 未登入導向 /strategies`, async ({ page }) => {
    const listeners = attachErrorListeners(page);
    const response = await page.goto(route);
    expect(response?.status(), `${route} 首次載入應回 200（redirect 是 client-side）`).toBe(200);
    await page.waitForURL((url) => url.pathname === "/strategies", { timeout: 10_000 });
    await expect(page.locator("h1").first()).toBeVisible();
    expectNoErrors(route, listeners);
  });
}

// ---------- guest 導覽連結全部可達 ----------

test("guest 導覽連結（Header nav.tabs）全部可達 200", async ({ page }) => {
  const homeResponse = await page.goto("/");
  expect(homeResponse?.status()).toBe(200);

  const hrefs = await page.locator("nav.tabs a").evaluateAll((els) =>
    els.map((el) => el.getAttribute("href")).filter((h): h is string => !!h));
  expect(hrefs.length, "未登入時 nav.tabs 應該至少有一個連結").toBeGreaterThan(0);

  for (const href of hrefs) {
    const res = await page.goto(href, { waitUntil: "domcontentloaded" });
    // 同文件內的錨點連結（例如目前在 `/` 點 `/#strategies`）是 same-document
    // navigation，Playwright 不會發出新的 HTTP request，回傳 `null` 是正常行為
    // （已經在同一輪 goto 打過一次 200 的那份文件上原地捲動）——只有真的換文件
    // 才需要斷言狀態碼。
    if (res !== null) {
      expect(res.status(), `導覽連結 ${href} 應回 200`).toBe(200);
    }
  }
});
