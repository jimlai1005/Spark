import type { Metadata } from "next";
import type { ReactNode } from "react";
import { canonicalUrl } from "@/lib/siteOrigin";

export const metadata: Metadata = {
  title: "文件與運作方式",
  description: "Filet 如何運作、非保管授權範圍與費用揭露。",
  alternates: { canonical: canonicalUrl("/docs") },
};

export default function DocsLayout({ children }: { children: ReactNode }) {
  return <>{children}</>;
}
