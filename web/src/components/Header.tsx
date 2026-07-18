"use client";
import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { logout } from "@/lib/api";
import { COPY } from "@/lib/copy";
import { useMe } from "@/lib/hooks";

const TABS = [
  { href: "/", label: "登入" },
  { href: "/onboarding", label: "開通" },
  { href: "/performance", label: "績效" },
] as const;

export function Header() {
  const pathname = usePathname();
  const me = useMe();
  const queryClient = useQueryClient();

  async function handleLogout() {
    await logout();
    // 成功後讓 ["me"] 快取失效——useMe 重抓回未登入態，各頁 guard 自然導回登入視圖。
    queryClient.invalidateQueries({ queryKey: ["me"] });
  }

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
      {me.data && (
        <button type="button" className="btn btn-ghost" onClick={handleLogout}>
          {COPY.common.logout}
        </button>
      )}
    </header>
  );
}
