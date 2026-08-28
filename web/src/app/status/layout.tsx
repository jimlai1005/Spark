import type { Metadata } from "next";
import type { ReactNode } from "react";
import { canonicalUrl } from "@/lib/siteOrigin";

export const metadata: Metadata = {
  title: "系統狀態",
  description: "Filet 各元件目前狀態與最後更新時間。",
  alternates: { canonical: canonicalUrl("/status") },
};

export default function StatusLayout({ children }: { children: ReactNode }) {
  return <>{children}</>;
}
