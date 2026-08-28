import type { Metadata } from "next";
import type { ReactNode } from "react";
import { canonicalUrl } from "@/lib/siteOrigin";

export const metadata: Metadata = {
  title: "服務條款",
  description: "Filet 服務條款全文。",
  alternates: { canonical: canonicalUrl("/terms") },
};

export default function TermsLayout({ children }: { children: ReactNode }) {
  return <>{children}</>;
}
