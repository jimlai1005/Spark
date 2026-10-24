"use client";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { WalletPicker } from "@/components/WalletPicker";
import { getDashboard, logout, type DashboardResp } from "@/lib/api";
import { LANG_LABELS } from "@/lib/copy";
import { shortAddr } from "@/lib/format";
import { useIsAdmin, useMe } from "@/lib/hooks";
import { useCopy, useLang } from "@/lib/lang";
import { useWalletLogin } from "@/lib/useWalletLogin";

/**
 * Header — 導覽狀態機（Task 7，顧問 P1：導覽本身是信任訊號的一部分）。
 *
 * ⭐⭐ 未登入與已登入不是同一份 tab 清單加減／disabled，是**兩組完全不同的頁籤**：
 * 未登入時完全不渲染任何需要登入才有意義的頁面（不是空白頁、不是灰階），移除
 * 任何連回首頁的「開始」自我連結，改為單一 CTA。
 * 已登入時才出現 Dashboard／設定／跟單狀態 pill／地址縮寫。
 *
 * Task 2（2026-08-29）：CTA 從「查看策略與風險」連結改為「登入」按鈕。
 * 連線／簽署流程改由 `lib/useWalletLogin` 統一（2026-10-24，EIP-6963 多錢包選單）；
 * 本元件只提供成功後的導向決策：成功後打一次 `getDashboard()` 讀 `status.state`：
 * 非 inactive（following/paused/halted）代表這個地址已經在跟單，直接送去 /dashboard；
 * inactive 或讀不到（404／例外）就是還沒選策略，送去 /strategies 選策略。這支呼叫是
 * 一次性讀取（不進 react-query 快取），只服務這次導向決策；下方 `dash` query 是既有的
 * pill 資料源，職責不同不合併。
 *
 * ADMIN 分組沿用舊機制：只有後端真的放行 /api/admin/pending 的人才顯示
 * （見 hooks.useIsAdmin 檔頭）——分組＝可見性，不是授權，/ops 與 /admin 各自
 * 掛後端 `_require_admin`，手打網址仍會 403。
 */
export function Header() {
  const pathname = usePathname();
  const router = useRouter();
  const me = useMe();
  const loggedIn = !!me.data;
  // 管理員與否由後端探測回答（見 hooks.useIsAdmin 註解）；未登入不打這支。
  const isAdmin = useIsAdmin({ enabled: loggedIn });
  const queryClient = useQueryClient();
  const COPY = useCopy();
  const { lang, setLang } = useLang();

  const login = useWalletLogin({
    onSuccess: async () => {
      // 依 dashboard 狀態導向（原登入 CTA 處理函式的後半；一次性讀取，不進 react-query 快取）
      let dest = "/strategies";
      try {
        const dashboard = await getDashboard();
        if (dashboard.status && dashboard.status.state !== "inactive") dest = "/dashboard";
      } catch {
        dest = "/strategies"; // 404／例外＝還沒有可看的 dashboard，保守送去選策略
      }
      router.push(dest);
    },
  });

  async function handleLogout() {
    await logout();
    // ⭐ resetQueries 而非只 invalidate ["me"]：換錢包時上一個 session 的快取
    // （admin 探測、儀表板、風控/資金）會殘留（2026-09-01 生產事故）。
    // ⚠️ 也不能用 queryClient.clear()：clear 只清資料**不通知活著的 observer**，
    // Header 的 useMe 拿不到變更 → 畫面停在登入態、換頁才更新（同日第二波事故）。
    // resetQueries = 清資料＋通知＋活躍查詢重抓，useMe 立即翻回未登入態。
    await queryClient.resetQueries();
    // 使用者裁決（2026-09-01）：登出一律回首頁，不留在原頁（也免得各頁 guard
    // 各自導去不同地方——dashboard 導 /strategies、settings 又是另一套）。
    router.push("/");
  }

  const guestTabs = [
    { href: "/#strategies", label: COPY.nav.strategies },
    { href: "/explore", label: COPY.nav.explore },
    { href: "/#how", label: COPY.nav.how },
    { href: "/#security", label: COPY.nav.security },
  ];
  const memberTabs = [
    { href: "/dashboard", label: COPY.nav.dashboard },
    { href: "/#strategies", label: COPY.nav.strategies },
    { href: "/explore", label: COPY.nav.explore },
    { href: "/settings", label: COPY.nav.settings },
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
      <Link href="/" className="wordmark-mini">
        {COPY.common.appName}
      </Link>
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
          <div className="header-cta-group">
            <button
              type="button"
              className="btn btn-primary header-cta"
              disabled={login.phase !== "idle"}
              onClick={() => void login.start()}
            >
              {login.phase === "connecting"
                ? COPY.nav.ctaConnecting
                : login.phase === "signing"
                  ? COPY.nav.ctaSigning
                  : COPY.nav.cta}
            </button>
            {login.error && <p className="header-cta-error">{login.error}</p>}
            {login.pickerOpen && (
              <WalletPicker
                candidates={login.candidates}
                busy={login.phase !== "idle"}
                onPick={(c) => void login.choose(c)}
                onCancel={login.cancelPicker}
              />
            )}
          </div>
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
