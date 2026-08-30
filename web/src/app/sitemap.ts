import type { MetadataRoute } from "next";
import { canonicalUrl } from "@/lib/siteOrigin";

/**
 * 固定路由清單（Task 17 dispatch）。動態的 `/strategies/{slug}` 不列——
 * 精選白名單會變動，靜態列舉容易與 API 資料脫節；策略卡片本身在 `/strategies`
 * 已可被爬到（連結可達即可索引，不需要在 sitemap 裡重複列舉）。
 */
export const SITEMAP_ROUTES: readonly string[] = [
  "/",
  "/strategies",
  "/leaderboard",
  "/advanced",
  "/docs",
  "/terms",
  "/privacy",
  "/risk",
  "/status",
];

export default function sitemap(): MetadataRoute.Sitemap {
  return SITEMAP_ROUTES.map((path) => ({ url: canonicalUrl(path) }));
}
