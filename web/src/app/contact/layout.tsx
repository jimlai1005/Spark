import type { Metadata } from "next";
import type { ReactNode } from "react";
import { canonicalUrl } from "@/lib/siteOrigin";

export const metadata: Metadata = {
  title: "聯絡我們",
  description: "透過表單聯絡 Filet 團隊，我們會盡快回覆。",
  alternates: { canonical: canonicalUrl("/contact") },
};

export default function ContactLayout({ children }: { children: ReactNode }) {
  return <>{children}</>;
}
