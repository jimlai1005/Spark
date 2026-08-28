import type { Metadata } from "next";
import type { ReactNode } from "react";
import { canonicalUrl } from "@/lib/siteOrigin";

// 只負責路由層級的 metadata（`/strategies` 的 page.tsx 是 client component，
// 不能自己 export metadata）。`/strategies/[slug]` 巢狀在這層底下也會繼承這份
// canonical/title 當預設值，動態頁再用 client 端 document.title 覆蓋
// （見 `[slug]/page.tsx`，Task 17 dispatch 允許 client 頁不強求 SSR metadata）。
export const metadata: Metadata = {
  title: "策略列表",
  description: "瀏覽 Filet 精選策略的實盤天數、回撤與方法論揭露，選定後才需要連接錢包。",
  alternates: { canonical: canonicalUrl("/strategies") },
};

export default function StrategiesLayout({ children }: { children: ReactNode }) {
  return <>{children}</>;
}
