import type { Metadata } from "next";
import type { ReactNode } from "react";
import { canonicalUrl } from "@/lib/siteOrigin";

export const metadata: Metadata = {
  title: "風險揭露",
  description: "使用 Filet 前應理解的市場與技術風險。",
  alternates: { canonical: canonicalUrl("/risk") },
};

export default function RiskLayout({ children }: { children: ReactNode }) {
  return <>{children}</>;
}
