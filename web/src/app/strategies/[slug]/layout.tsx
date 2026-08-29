import type { Metadata } from "next";
import type { ReactNode } from "react";
import { canonicalUrl } from "@/lib/siteOrigin";

/**
 * `/strategies/[slug]` 的路由層級 metadata（opus 審查 Suggestion 3）。
 *
 * `page.tsx` 是 client component（見該檔 NOTE：連錢包流程需要 hooks），不能
 * `export generateMetadata`——Task 17 當時的取捨是留給父層 `strategies/layout.tsx`
 * 的通用 title/canonical 當預設值，載入後在 client 端補上 `document.title`。
 * 這裡補上動態頁**自己的** SSR metadata：title 用 `params.slug` 做一個最陽春的
 * 展示轉換（首字大寫，不打 API），canonical 指向這個 slug 自己的頁面而不是
 * 父層籠統的 `/strategies`——crawler／未執行 JS 前拿到的標題與正規連結都該是
 * 這個策略頁自己的，不是列表頁的。client 端 `document.title`（真實策略名）
 * 載入後仍會覆蓋這裡的陽春版本，兩者互不取代、分工不同時機（SSR 先於 hydration）。
 */
export async function generateMetadata(
  { params }: { params: Promise<{ slug: string }> },
): Promise<Metadata> {
  const { slug } = await params;
  const title = slug ? slug.charAt(0).toUpperCase() + slug.slice(1) : slug;
  return {
    title,
    alternates: { canonical: canonicalUrl(`/strategies/${slug}`) },
  };
}

export default function StrategyDetailLayout({ children }: { children: ReactNode }) {
  return <>{children}</>;
}
