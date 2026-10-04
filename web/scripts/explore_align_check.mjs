// 探索表格對齊量測（2026-10-04 plan dashboard-hide-twr-and-explore-subgrid Task 2c）。
// 目標：production server（`npm run build && npm run start`，預設 http://127.0.0.1:3000）。
// 用法（operator 自己管 server，腳本不起／殺）：先 `lsof -ti :3000` 必須為空，再 `npm run build && npm run start`，
// 另一個 shell 跑本腳本；結束後自行殺掉 server。連到殭屍 server 時 CSS 是舊的 → 腳本偵測到會 exit 2。
// 輸出 report.json 與截圖到 OUT_DIR（預設 web/test-results/explore-align/）；
// 對齊誤差 >0.5px、桌面橫向捲動、任一列任一 cell 文字溢出 → exit 1；server CSS 過舊 → exit 2。
// `LANG_UI=en` 環境變數會先寫 localStorage.filet_lang=en 量英文版（表頭較寬）。
import { chromium } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";

const BASE = process.env.E2E_BASE_URL ?? "http://127.0.0.1:3000";
const OUT = process.env.OUT_DIR ?? new URL("../test-results/explore-align/", import.meta.url).pathname; // web/.gitignore 已忽略 test-results
mkdirSync(OUT, { recursive: true });

const win = (pnl) => ({ pnl_usd: pnl, max_dd_pct: -5.0, max_dd_reason: null, spark: [1, 1.05, 1.1] });
const wins = (pnl) => ({ day: win(pnl), week: win(pnl), month: win(pnl), allTime: win(pnl) });
const mk = (addr, name, pnl, exposure, risk, extra = {}) => ({
  address: addr, display_name: name, label: name, coins: ["BTC", "ETH"],
  account_bucket: "$100K–$1M", windows: wins(pnl), live_days: 118, order_count_30d: 250,
  closed_positions_30d: 20, realized_pnl_30d_usd: 5000, close_win_rate_pct: 61.2,
  concentration_pct: 40, exposure, tags: ["low_drawdown"], risk, eligibility: "eligible", ...extra,
});
const none = { liq_distance_pct: null, liq_coin: null, maint_ratio: null };
const rows = [
  mk("0x" + "a".repeat(40), "Alice", 210, { dir: "long", pct: 100.0 }, { liq_distance_pct: 39.9, liq_coin: "PUMP", maint_ratio: 0.3 }),
  mk("0x" + "b".repeat(40), "0xbbbb…bbbb", -2181.94, { dir: null, pct: null }, none),
  mk("0x" + "d".repeat(40), "Whale", 16819029, { dir: "short", pct: 63.4 }, { liq_distance_pct: 12.3, liq_coin: "BTC", maint_ratio: 0.5 }),
  mk("0x" + "c".repeat(40), "Pending", 0, { dir: null, pct: null }, none, { eligibility: "pending", windows: { day: null, week: null, month: null, allTime: null } }),
];
const resp = {
  rows, page: 1, page_size: 25, total_qualified: 3, total_pending: 1, total_scanned: 100, pool: 100,
  updated_at: 1_700_000_000, building: false, sort: "pnl", order: "desc",
};

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
await page.route("**/api/**", (route) => {
  const url = route.request().url();
  const body = url.includes("/api/public/explore") ? resp : url.includes("strategies") ? [] : {};
  return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
});
if (process.env.LANG_UI === "en") {
  await page.addInitScript(() => localStorage.setItem("filet_lang", "en"));
}
await page.goto(`${BASE}/explore`, { waitUntil: "networkidle" });
await page.waitForSelector(".explore-table-row");

const tableDisplay = await page.evaluate(() => getComputedStyle(document.querySelector(".explore-table")).display);
if (tableDisplay !== "grid") {
  console.error(`:3000 served stale CSS (display=${tableDisplay})——是不是殭屍 server？`);
  await browser.close();
  process.exit(2);
}

const m = await page.evaluate(() => {
  const left = (el, i) => el.children[i].getBoundingClientRect().left;
  const cols = (el) => ({ c8: left(el, 7), c9: left(el, 8), c10: left(el, 9) });
  const head = document.querySelector(".explore-table-head");
  const tbl = document.querySelector(".explore-table");
  const h8 = head.children[7];
  const lh = parseFloat(getComputedStyle(h8).lineHeight) || parseFloat(getComputedStyle(h8).fontSize) * 1.2;
  const cell_overflow = [];
  [...document.querySelectorAll(".explore-table-head, .explore-table-row")].forEach((r, ri) => {
    [...r.children].forEach((c, ci) => {
      if (c.scrollWidth > c.clientWidth + 0.5) cell_overflow.push({ row: ri, col: ci + 1, scrollWidth: c.scrollWidth, clientWidth: c.clientWidth });
      // svg 等替換元素不貢獻 scrollWidth 以外的溢出：另比對子元素右緣是否超出 cell
      const cr = c.getBoundingClientRect();
      for (const k of c.querySelectorAll("*")) {
        if (k.getBoundingClientRect().right > cr.right + 0.5 && !cell_overflow.some((o) => o.row === ri && o.col === ci + 1))
          cell_overflow.push({ row: ri, col: ci + 1, child: k.tagName.toLowerCase(), over: k.getBoundingClientRect().right - cr.right });
      }
    });
  });
  return {
    head: cols(head),
    rows: [...document.querySelectorAll(".explore-table-row")].map((r) => ({
      ...cols(r), right10: r.children[9].getBoundingClientRect().right,
    })),
    overflow: tbl.scrollWidth > tbl.clientWidth,
    scrollWidth: tbl.scrollWidth, clientWidth: tbl.clientWidth,
    tableRight: tbl.getBoundingClientRect().right,
    // head_wrap 只回報不判定：英文表頭（Max drawdown／Current exposure）折兩行是既有慣例；
    // 真正的 bug 是文字溢出壓到鄰欄 → 用 head_overflow 判定。
    head_wrap: h8.getBoundingClientRect().height > lh * 1.6,
    cell_overflow,
    head_overflow: h8.scrollWidth > h8.clientWidth + 0.5,
    h8_height: h8.getBoundingClientRect().height, h8_lineHeight: lh,
  };
});
let max = 0;
for (const r of m.rows) for (const k of ["c8", "c9", "c10"]) max = Math.max(max, Math.abs(r[k] - m.head[k]));
const report = { ...m, max_abs_diff_px: max };
writeFileSync(`${OUT}/report.json`, JSON.stringify(report, null, 2));
await page.screenshot({ path: `${OUT}/explore_after${process.env.LANG_UI === "en" ? "_en" : ""}.png` });
await browser.close();
console.log(JSON.stringify({ max_abs_diff_px: max, overflow: m.overflow, head_overflow: m.head_overflow, cell_overflow: m.cell_overflow, head_wrap: m.head_wrap }));
process.exit(max > 0.5 || m.overflow || m.head_overflow || m.cell_overflow.length > 0 ? 1 : 0);
