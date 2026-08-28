/**
 * 對外主機名注入（Task 17，plan §0.3 不變量 8）。`NEXT_PUBLIC_SITE_ORIGIN`
 * **只**用於 canonical／OG／sitemap 等絕對連結；站內連結一律走相對路徑，
 * 不吃這個值——換網域時不需要動任何 `<Link>`。
 *
 * 預設 `https://app.filet.trade`：正式網域購買前的佔位值，正式環境由部署時的
 * env 覆蓋（見 deploy/RUNBOOK.md）。
 */
const DEFAULT_SITE_ORIGIN = "https://app.filet.trade";

/** 讀取一次即可——SSR 與瀏覽器都只在模組載入時看一次 `process.env`。 */
function resolveSiteOrigin(): string {
  const raw = process.env.NEXT_PUBLIC_SITE_ORIGIN?.trim();
  const origin = raw && raw.length > 0 ? raw : DEFAULT_SITE_ORIGIN;
  return origin.endsWith("/") ? origin.slice(0, -1) : origin;
}

export const SITE_ORIGIN = resolveSiteOrigin();

/**
 * 絕對 URL：`SITE_ORIGIN + path`。`path` 不論有沒有前導 `/` 都會被收斂成
 * 恰好一個——`canonicalUrl("strategies")` 與 `canonicalUrl("/strategies")`
 * 結果相同，呼叫端不必記這個細節。
 */
export function canonicalUrl(path: string): string {
  const normalized = path.startsWith("/") ? path : `/${path}`;
  return `${SITE_ORIGIN}${normalized}`;
}
