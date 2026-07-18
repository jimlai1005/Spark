"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";

const TABS = [
  { href: "/", label: "登入" },
  { href: "/onboarding", label: "開通" },
  { href: "/performance", label: "績效" },
] as const;

export function Header() {
  const pathname = usePathname();
  return (
    <header className="app-header">
      <div className="wordmark-mini">FILET</div>
      <nav className="tabs" aria-label="頁面切換">
        {TABS.map((t) => (
          <Link
            key={t.href}
            href={t.href}
            className="tab"
            aria-current={pathname === t.href ? "page" : undefined}
          >
            {t.label}
          </Link>
        ))}
      </nav>
    </header>
  );
}
