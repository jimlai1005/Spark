"use client";
import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { logout } from "@/lib/api";
import { COPY } from "@/lib/copy";
import { useBillingStatus, useIsAdmin, useMe } from "@/lib/hooks";

/**
 * 導覽分組。⭐ 全部文案走 COPY.nav（單一來源；先前三個 tab 硬編中文字面值）。
 *
 * ⭐⭐ 分組＝可見性，**不是授權**。藏起連結不保護任何東西：/ops、/admin 的端點
 * 各自掛後端 `_require_admin`，手打網址照樣 403。這裡只是不把「點了必然吃閉門羹」
 * 的入口擺在使用者眼前。
 */
/** 公開：任何人都看得到，各頁自己有登入 guard。 */
const PUBLIC_TABS = [
  { href: "/", label: COPY.nav.login },
  { href: "/onboarding", label: COPY.nav.onboarding },
  { href: "/leaders", label: COPY.nav.leaders },
  { href: "/capital", label: COPY.nav.capital },
  { href: "/performance", label: COPY.nav.performance },
  { href: "/pricing", label: COPY.nav.pricing },
] as const;
/** 登入後才顯示：未登入點進 /billing 只會看到「尚未登入」，擺出來是死路。 */
const MEMBER_TABS = [{ href: "/billing", label: COPY.nav.billing }] as const;
/** 後端放行 admin 端點者才顯示（useIsAdmin 是探測後端，不是前端推論）。 */
const ADMIN_TABS = [
  { href: "/ops", label: COPY.nav.ops },
  { href: "/admin", label: COPY.nav.admin },
] as const;

export function Header() {
  const pathname = usePathname();
  const me = useMe();
  const loggedIn = !!me.data;
  // 管理員與否由後端探測回答（見 hooks.useIsAdmin 註解）；未登入不打這支。
  const isAdmin = useIsAdmin({ enabled: loggedIn });
  const tabs = [
    ...PUBLIC_TABS,
    ...(loggedIn ? MEMBER_TABS : []),
    ...(isAdmin ? ADMIN_TABS : []),
  ];
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
        {tabs.map((t) => (
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
