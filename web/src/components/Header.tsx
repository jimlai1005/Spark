"use client";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";
import { useAccount, useConnect, useSignMessage } from "wagmi";
import { getDashboard, logout, type DashboardResp } from "@/lib/api";
import { LANG_LABELS } from "@/lib/copy";
import { LOW_MARGIN_THRESHOLD } from "@/components/dashboard/EquityCard";
import { shortAddr } from "@/lib/format";
import { useIsAdmin, useMe } from "@/lib/hooks";
import { useCopy, useLang } from "@/lib/lang";
import { loginWithSiwe } from "@/lib/siwe";

/**
 * Header — 導覽狀態機（Task 7，顧問 P1：導覽本身是信任訊號的一部分）。
 *
 * ⭐⭐ 未登入與已登入不是同一份 tab 清單加減／disabled，是**兩組完全不同的頁籤**：
 * 未登入時完全不渲染任何需要登入才有意義的頁面（不是空白頁、不是灰階），移除
 * 任何連回首頁的「開始」自我連結，改為單一 CTA。
 * 已登入時才出現 Dashboard／設定／跟單狀態 pill／地址縮寫。
 *
 * Task 2（2026-08-29）：CTA 從「查看策略與風險」連結改為「登入」按鈕——
 * 連 injected 錢包 → SIWE 簽署（沿用 strategies/[slug] 的連線/簽署寫法）→
 * 成功後打一次 `getDashboard()` 讀 `status.state`：非 inactive（following/paused/
 * halted）代表這個地址已經在跟單，直接送去 /dashboard；inactive 或讀不到（404／
 * 例外）就是還沒選策略，送去 /strategies 選策略。這支呼叫是一次性讀取（不進
 * react-query 快取），只服務這次導向決策；下方 `dash` query 是既有的 pill 資料源，
 * 職責不同不合併。
 *
 * ADMIN 分組沿用舊機制：只有後端真的放行 /api/admin/pending 的人才顯示
 * （見 hooks.useIsAdmin 檔頭）——分組＝可見性，不是授權，/ops 與 /admin 各自
 * 掛後端 `_require_admin`，手打網址仍會 403。
 */
type LoginPhase = "idle" | "connecting" | "signing";

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

  const { address, chainId, isConnected } = useAccount();
  const { connectAsync, connectors } = useConnect();
  const { signMessageAsync } = useSignMessage();
  const [loginPhase, setLoginPhase] = useState<LoginPhase>("idle");
  const [loginError, setLoginError] = useState<string | null>(null);

  async function handleLogout() {
    await logout();
    // 成功後讓 ["me"] 快取失效——useMe 重抓回未登入態，各頁 guard 自然導回登入視圖。
    queryClient.invalidateQueries({ queryKey: ["me"] });
  }

  /**
   * 未登入 CTA：connect（injected）→ SIWE → 依 dashboard 狀態導向。
   * 錯誤處理沿用 strategies/[slug] 的拒簽判別（不得 console 洩漏簽章內容——
   * 這裡從頭到尾沒有把 message／signature 印出來，只把 error.name/code/message
   * 拿來分類成使用者看得懂的兩句文案）。
   */
  async function handleLoginCta() {
    setLoginError(null);
    try {
      let addr = address;
      let cid = chainId;
      if (!isConnected) {
        const injected = connectors[0];
        if (!injected) {
          setLoginError(COPY.login.noWallet);
          return;
        }
        setLoginPhase("connecting");
        const result = await connectAsync({ connector: injected });
        addr = result.accounts[0];
        cid = result.chainId;
      }
      if (!addr || !cid) {
        setLoginError(COPY.login.noWallet);
        return;
      }
      setLoginPhase("signing");
      await loginWithSiwe({
        address: addr,
        chainId: cid,
        signMessage: (message) => signMessageAsync({ message }),
      });
      queryClient.invalidateQueries({ queryKey: ["me"] });
      let dest = "/strategies";
      try {
        const dashboard = await getDashboard();
        if (dashboard.status && dashboard.status.state !== "inactive") dest = "/dashboard";
      } catch {
        // 404／例外＝還沒有可看的 dashboard 資料，保守送去選策略，不視為登入失敗。
        dest = "/strategies";
      }
      router.push(dest);
    } catch (err) {
      const e = err as { name?: string; code?: number; message?: string } | undefined;
      const isRejected =
        e?.name === "UserRejectedRequestError"
        || e?.code === 4001
        || /reject|denied|cancel/i.test(String(e?.message ?? ""));
      setLoginError(isRejected ? COPY.login.rejected : COPY.login.loginFailed);
    } finally {
      setLoginPhase("idle");
    }
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

  /**
   * ⭐ M3 round3 Task 6（R2 P2「Dashboard 保證金」）：低保證金 header 同步提示。
   * 沿用同一個 `dash` query（已在跟單狀態 pill 使用），不為這顆 pill 另外打
   * `/api/me/dashboard`——同一份回應的 `equity.available_pct` 是唯一來源，
   * 與 EquityCard 卡片內的告警判準同源同基準（工程原則 1）。
   */
  const availablePctNum = dash.data?.equity?.available_pct != null
    ? Number(dash.data.equity.available_pct) : null;
  const showMarginAlert = availablePctNum != null
    && Number.isFinite(availablePctNum) && availablePctNum < LOW_MARGIN_THRESHOLD;

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
              disabled={loginPhase !== "idle"}
              onClick={handleLoginCta}
            >
              {loginPhase === "connecting"
                ? COPY.nav.ctaConnecting
                : loginPhase === "signing"
                  ? COPY.nav.ctaSigning
                  : COPY.nav.cta}
            </button>
            {loginError && <p className="header-cta-error">{loginError}</p>}
          </div>
        )}
        {loggedIn && me.data && (
          <>
            {showMarginAlert && (
              <Link href="/dashboard" className="pill header-margin-alert">
                {COPY.nav.marginAlertPill}
              </Link>
            )}
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
