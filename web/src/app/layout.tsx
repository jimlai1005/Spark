import type { Metadata } from "next";
import type { ReactNode } from "react";
import { JetBrains_Mono, Noto_Sans_TC } from "next/font/google";
import { Footer } from "@/components/Footer";
import { Header } from "@/components/Header";
import { canonicalUrl, SITE_ORIGIN } from "@/lib/siteOrigin";
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

// 全站 SEO 固定值（設計稿 §07 HEAD/SEO，L893-905）。屬 metadata 定義，非元件
// 內文，不進 copy.ts（plan Task 17 dispatch）；title 用 Next 的 template 機制，
// 子路由的 layout.tsx 只需給短標題，這裡統一補上「｜Filet」尾綴。
const SITE_TITLE = "Filet｜Hyperliquid 非保管策略跟單";
const SITE_DESCRIPTION =
  "資金留在你自己的錢包。Filet 依你設定的風險限制在 Hyperliquid 執行量化策略，無法提領或轉帳，可隨時撤銷授權。";

export const metadata: Metadata = {
  metadataBase: new URL(SITE_ORIGIN),
  title: { default: SITE_TITLE, template: "%s｜Filet" },
  description: SITE_DESCRIPTION,
  alternates: { canonical: canonicalUrl("/") },
  openGraph: {
    title: SITE_TITLE,
    description: SITE_DESCRIPTION,
    url: canonicalUrl("/"),
    siteName: "Filet",
    type: "website",
    locale: "zh_Hant",
    images: [{ url: "/og.png", width: 1200, height: 630 }],
  },
  twitter: {
    card: "summary_large_image",
    title: SITE_TITLE,
    description: SITE_DESCRIPTION,
    images: ["/og.png"],
  },
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
