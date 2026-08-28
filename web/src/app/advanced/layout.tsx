import type { Metadata } from "next";
import type { ReactNode } from "react";
import { canonicalUrl } from "@/lib/siteOrigin";

export const metadata: Metadata = {
  title: "進階模式",
  description: "在你自行完成盡職調查後，對任意 Hyperliquid 地址設定跟單，需額外風險確認。",
  alternates: { canonical: canonicalUrl("/advanced") },
};

export default function AdvancedLayout({ children }: { children: ReactNode }) {
  return <>{children}</>;
}
