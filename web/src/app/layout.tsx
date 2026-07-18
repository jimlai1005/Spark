import type { Metadata } from "next";
import type { ReactNode } from "react";
import { Header } from "@/components/Header";
import "@/styles/globals.css";

export const metadata: Metadata = {
  title: "Filet",
  description: "資金留在你自己的錢包。策略照樣執行。",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-Hant">
      <body>
        <Header />
        {children}
      </body>
    </html>
  );
}
