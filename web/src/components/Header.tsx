"use client";
import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { logout } from "@/lib/api";
import { COPY } from "@/lib/copy";
import { useBillingStatus, useMe } from "@/lib/hooks";

const TABS = [
  { href: "/", label: "登入" },
  { href: "/onboarding", label: "開通" },
  { href: "/performance", label: "績效" },
  // /pricing 是公開頁，適合曝光；/billing 與 /admin、/ops 一樣不佔導覽列（由 chip 進入）。
  { href: "/pricing", label: COPY.billing.navPricing },
] as const;

export function Header() {
  const pathname = usePathname();
  const me = useMe();
  // 訂閱 chip：僅在**已登入且 billing 已啟用**時顯示。billing 未啟用時後端回 501，
  // query 落在 error 而沒有 data → 整組不渲染（不是渲染一個空殼），Header 版面不變形。
  const billing = useBillingStatus({ enabled: !!me.data });
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
      {me.data && billing.data && (
        <Link
          href="/billing"
          className={`chip header-chip ${
            billing.data.status === "active" ? "chip-up" : "chip-neutral"
          }`}
        >
          {billing.data.status === "active"
            ? COPY.billing.planNameActive
            : COPY.billing.status[billing.data.status]}
        </Link>
      )}
      {me.data && (
        <button type="button" className="btn btn-ghost" onClick={handleLogout}>
          {COPY.common.logout}
        </button>
      )}
    </header>
  );
}
