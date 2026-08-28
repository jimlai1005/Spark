import type { Metadata } from "next";
import type { ReactNode } from "react";
import { JetBrains_Mono, Noto_Sans_TC } from "next/font/google";
import { Footer } from "@/components/Footer";
import { Header } from "@/components/Header";
import { Providers } from "./providers";
import "@/styles/globals.css";

// 介面字體（Noto Sans TC）＋ 數字字體（JetBrains Mono，tabular-nums）。
// next/font/google 自架字體，注入 CSS 變數供 tokens.css 的 --font-body/--font-mono 使用。
const notoSansTC = Noto_Sans_TC({
  subsets: ["latin"],
  weight: ["400", "500", "700"],
  variable: "--font-noto-sans-tc",
  display: "swap",
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "700"],
  variable: "--font-jetbrains-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Filet",
  description: "資金留在你自己的錢包。策略照樣執行。",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-Hant" className={`${notoSansTC.variable} ${jetbrainsMono.variable}`}>
      <body>
        <Providers>
          <Header />
          {children}
          <Footer />
        </Providers>
      </body>
    </html>
  );
}
