import type { MetadataRoute } from "next";
import { canonicalUrl } from "@/lib/siteOrigin";

/**
 * 全站放行（無需登入的公開頁與 API 都可被索引；Dashboard/Settings/Onboarding
 * 等登入態頁面沒有可索引內容，不特別擋——爬蟲進去也只會看到 redirect）。
 * 指向 `sitemap.ts` 的固定路由清單。
 */
export default function robots(): MetadataRoute.Robots {
  return {
    rules: { userAgent: "*", allow: "/" },
    sitemap: canonicalUrl("/sitemap.xml"),
  };
}
