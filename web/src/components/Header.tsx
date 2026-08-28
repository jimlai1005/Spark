"use client";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { getDashboard, logout, type DashboardResp } from "@/lib/api";
import { LANG_LABELS } from "@/lib/copy";
import { shortAddr } from "@/lib/format";
import { useIsAdmin, useMe } from "@/lib/hooks";
import { useCopy, useLang } from "@/lib/lang";

/**
 * Header — 導覽狀態機（Task 7，顧問 P1：導覽本身是信任訊號的一部分）。
 *
 * ⭐⭐ 未登入與已登入不是同一份 tab 清單加減／disabled，是**兩組完全不同的頁籤**：
 * 未登入時完全不渲染任何需要登入才有意義的頁面（不是空白頁、不是灰階），移除
 * 任何連回首頁的「開始」自我連結，改為單一 CTA「查看策略與風險」→ /strategies。
 * 已登入時才出現 Dashboard／設定／跟單狀態 pill／地址縮寫。
 *
 * ADMIN 分組沿用舊機制：只有後端真的放行 /api/admin/pending 的人才顯示
 * （見 hooks.useIsAdmin 檔頭）——分組＝可見性，不是授權，/ops 與 /admin 各自
 * 掛後端 `_require_admin`，手打網址仍會 403。
 */
export function Header() {
  const pathname = usePathname();
  const me = useMe();
  const loggedIn = !!me.data;
  // 管理員與否由後端探測回答（見 hooks.useIsAdmin 註解）；未登入不打這支。
  const isAdmin = useIsAdmin({ enabled: loggedIn });
  const queryClient = useQueryClient();
  const COPY = useCopy();
  const { lang, setLang } = useLang();

  async function handleLogout() {
    await logout();
    // 成功後讓 ["me"] 快取失效——useMe 重抓回未登入態，各頁 guard 自然導回登入視圖。
    queryClient.invalidateQueries({ queryKey: ["me"] });
  }

  const guestTabs = [
    { href: "/strategies", label: COPY.nav.strategies },
    { href: "/#how", label: COPY.nav.how },
    { href: "/#security", label: COPY.nav.security },
    { href: "/docs", label: COPY.nav.docs },
  ];
  const memberTabs = [
    { href: "/dashboard", label: COPY.nav.dashboard },
    { href: "/strategies", label: COPY.nav.strategies },
    { href: "/settings", label: COPY.nav.settings },
    { href: "/docs", label: COPY.nav.docs },
    ...(isAdmin
      ? [
          { href: "/ops", label: COPY.nav.ops },
          { href: "/admin", label: COPY.nav.admin },
        ]
      : []),
  ];
  const tabs = loggedIn ? memberTabs : guestTabs;

  /**
   * 跟單狀態三態，接上 `/api/me/dashboard`（Task 14；資料源 Task 13）。
   * `dashboard.status.state` 四態 → 三態 pill：`following`→跟單中；`paused`→已暫停；
   * 其餘（`halted`／`inactive`／載入中／讀取失敗）一律 `not_following`——讀不到 ≠
   * 安全態，寧可顯示保守值也不偽造一個沒有根據的「跟單中」綠燈。
   * `staleTime` 給一點餘裕：Header 在多數頁面掛載，不必每次切頁都重新打一次。
   */
  const dash = useQuery<DashboardResp>({
    queryKey: ["me-dashboard"],
    queryFn: getDashboard,
    enabled: loggedIn,
    staleTime: 30_000,
  });
  const state = dash.data?.status?.state;
  const followStatus: "following" | "paused" | "not_following" =
    state === "following" ? "following" : state === "paused" ? "paused" : "not_following";
  const followLabel = {
    following: COPY.nav.pillFollowing,
    paused: COPY.nav.pillPaused,
    not_following: COPY.nav.pillNotFollowing,
  }[followStatus];

  return (
    <header className="app-header">
      <div className="wordmark-mini">{COPY.common.appName}</div>
      <nav className="tabs" aria-label={COPY.nav.ariaLabel}>
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
      <div className="header-auth">
        <div className="lang-toggle" role="group" aria-label={COPY.nav.langToggleLabel}>
          <button
            type="button"
            className="lang-btn"
            aria-pressed={lang === "zh"}
            onClick={() => setLang("zh")}
          >
            {LANG_LABELS.zh}
          </button>
          <button
            type="button"
            className="lang-btn"
            aria-pressed={lang === "en"}
            onClick={() => setLang("en")}
          >
            {LANG_LABELS.en}
          </button>
        </div>
        {!loggedIn && (
          <Link href="/strategies" className="btn btn-primary header-cta">
            {COPY.nav.cta}
          </Link>
        )}
        {loggedIn && me.data && (
          <>
            <Link href="/dashboard" className="pill follow-pill" data-state={followStatus}>
              <span className="follow-pill-dot" aria-hidden="true" />
              {followLabel}
            </Link>
            <span className="mono header-addr">{shortAddr(me.data.address)}</span>
            <button type="button" className="btn btn-ghost header-logout" onClick={handleLogout}>
              {COPY.common.logout}
            </button>
          </>
        )}
      </div>
    </header>
  );
}
