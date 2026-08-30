"use client";
/**
 * `/traders/[address]` — 交易員詳情頁（M3 round2 Task 6）。
 *
 * leaderboard（`/leaderboard`）任意地址的鏈上績效展示頁，**不受精選白名單管轄**
 * ——資料源是新公開端點 `GET /api/public/traders/{address}`（後端 §「已驗證的
 * 外部事實」＋ Task 6），計算與 `/strategies/[slug]` 共用同一份後端純函式
 * （`filet.strategies.build_metrics`／`build_equity_index`／`build_methodology`），
 * 前端亦共用同一批純算術（`lib/strategyMetrics.ts`，本 task 從 `strategies/[slug]`
 * 抽出）與 `EquityCurve` 元件，兩頁的公式與圖表渲染只有一份。
 *
 * 版型刻意只取 plan 明訂的範圍（equity 圖 ＋ 指標卡）：不含策略頁的槓桿/回撤
 * 滑桿與 CAGR／方法論卡——那些是「跟隨此**策展**策略」才有意義的設定，任意
 * leaderboard 地址沒有平台審查過的槓桿上限可顯示，硬套會讓這頁看起來像一張
 * 精選策略卡（`disclaimerNote` 與 `advanced` 頁「進階模式（無背書）」同一個
 * 揭露精神，見 copy.ts `traders` 檔頭）。
 *
 * ⭐⭐ CTA「連接錢包並繼續」**原封不動**沿用 `/strategies/[slug]` 的
 * connect→SIWE→跳轉流程（僅 query string 的 `strategy` 值不同）：
 * `strategy=advanced:{address}`——與 `/advanced` 頁（Task 11）產生的格式完全
 * 相同，onboarding（`page.tsx` `ADVANCED_PREFIX`）與 `StepConfirm` 的
 * `postLeaderSelect` 已經吃這個格式：非精選位址在**送出簽章那一刻**由後端
 * `_admit_custom_leader` 重新准入並寫入 `user_leaders` registry（見
 * `publicapi/app.py` `leaders_select` 端點 4a 段），本頁不需要、也不重新
 * 實作那段准入或 registry 邏輯。
 *
 * ⭐ [W3] 2026-08-29 opus 審查修正：標題**一律**用 `shortAddr(trader.address)`
 * ——不再信任 `?name=` 查詢參數（那是 client 端可任意竄改的值，曾經被拿來當
 * 顯示名稱直接渲染）。displayName 現在只在 `/leaderboard` 表格內顯示。
 * ⭐ [W4] 已被平台安全撤銷（`enabled=false`）的 leader：`follow_blocked=true`
 * 時隱藏 CTA、改顯示提示文案，不讓新客戶點進一個已撤銷的地址。
 *
 * ⭐ M3 round3 Task 7（R2-P0 指標收斂，比照 `/strategies/[slug]`）：本頁沿用
 * 同一組 headline／collapse 分組，但 `/api/public/traders/{address}` 沒有
 * `sample_days`／`sample_threshold`（Task 3 只加到 `/api/public/strategies*`，
 * 本 task 檔案範圍不含後端，不新增端點欄位）——改用同一份 `metrics.
 * sample_count`（已有欄位，`build_metrics` 對兩個端點同源同義：N 個日報酬
 * 樣本，見 copy.ts `winRateNotePrefix`「N=」的既有用法）當門檻判斷依據，
 * 門檻值沿用與後端 `CAGR_SAMPLE_THRESHOLD_DAYS` 相同的 30（⚠️ 2026-08-30 使用者
 * 裁決 D15：原 60 降為 30，全鏈路同步——沒有可讀的後端欄位可用，故在此鏡射一份
 * 常數，而非重新發明門檻）。
 */
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useAccount, useConnect, useSignMessage } from "wagmi";
import { EquityCurve } from "@/components/EquityCurve";
import { fmtAmount, fmtUpdatedAtUtc, shortAddr } from "@/lib/format";
import { useMe } from "@/lib/hooks";
import { useCopy } from "@/lib/lang";
import { getPublicTraderDetail, type PublicTraderDetail } from "@/lib/publicApi";
import { loginWithSiwe } from "@/lib/siwe";
import { formatStartEndEquity, metricText } from "@/lib/strategyMetrics";

type ConnectPhase = "idle" | "connecting" | "signing";

const TRADER_SAMPLE_THRESHOLD_DAYS = 30;

export default function TraderDetailPage() {
  const params = useParams<{ address: string }>();
  const routeAddress = params?.address ?? "";
  const router = useRouter();
  const COPY = useCopy();
  const c = COPY.traders;
  const sc = COPY.strategyDetail; // 指標卡／CAGR／方法論文案沿用（通用績效用語）

  const [trader, setTrader] = useState<PublicTraderDetail | null | undefined>(undefined);
  useEffect(() => {
    let cancelled = false;
    setTrader(undefined);
    getPublicTraderDetail(routeAddress).then((r) => {
      if (!cancelled) setTrader(r);
    });
    return () => {
      cancelled = true;
    };
  }, [routeAddress]);

  useEffect(() => {
    if (trader) document.title = `${shortAddr(trader.address)}｜Filet`;
  }, [trader]);

  const me = useMe();
  const loggedIn = !!me.data;
  const { address: walletAddress, chainId, isConnected } = useAccount();
  const { connectAsync, connectors } = useConnect();
  const { signMessageAsync } = useSignMessage();
  const [phase, setPhase] = useState<ConnectPhase>("idle");
  const [error, setError] = useState<string | null>(null);

  if (trader === undefined) {
    return (
      <main className="page">
        <p className="hint">{c.loadingNote}</p>
      </main>
    );
  }
  if (trader === null) {
    return (
      <main className="page">
        <div className="narrow">
          <h1>{c.notFoundTitle}</h1>
          <p>{c.notFoundBody}</p>
          <Link className="btn btn-primary" href="/leaderboard">
            {c.backToList}
          </Link>
        </div>
      </main>
    );
  }

  const m = trader.metrics;
  const explorerHref = `https://app.hyperliquid.xyz/explorer/address/${trader.address}`;
  const asOf = fmtUpdatedAtUtc(trader.methodology.updated_at);
  const startEnd = formatStartEndEquity(trader.methodology, fmtAmount);

  function buildQuery(): string {
    return `strategy=advanced:${trader!.address}`;
  }

  async function handleCta() {
    setError(null);
    if (loggedIn) {
      router.push(`/onboarding?${buildQuery()}`);
      return;
    }
    try {
      let addr = walletAddress;
      let cid = chainId;
      if (!isConnected) {
        const injected = connectors[0];
        if (!injected) {
          setError(COPY.login.noWallet);
          return;
        }
        setPhase("connecting");
        const result = await connectAsync({ connector: injected });
        addr = result.accounts[0];
        cid = result.chainId;
      }
      if (!addr || !cid) {
        setError(COPY.login.noWallet);
        return;
      }
      setPhase("signing");
      await loginWithSiwe({
        address: addr,
        chainId: cid,
        signMessage: (message) => signMessageAsync({ message }),
      });
      router.push(`/onboarding?${buildQuery()}`);
    } catch (err) {
      const e = err as { name?: string; code?: number; message?: string } | undefined;
      const isRejected =
        e?.name === "UserRejectedRequestError"
        || e?.code === 4001
        || /reject|denied|cancel/i.test(String(e?.message ?? ""));
      setError(isRejected ? COPY.login.rejected : COPY.login.loginFailed);
    } finally {
      setPhase("idle");
    }
  }

  // ⭐ Task 7（主線程驗收修正）：大字只留總報酬／策略期間回撤／日勝率三張
  // （比照 `/strategies/[slug]`，plan Task 7 第 1 條）。Sharpe／Sortino／
  // 年化波動／起訖淨值／最佳最差日視樣本門檻整組摺成一行。本頁沒有
  // `sample_days`／`sample_threshold` 欄位，改用 `metrics.sample_count`
  // ＋本地鏡射常數 `TRADER_SAMPLE_THRESHOLD_DAYS`（見檔頭）。
  const sampleInsufficient = m.sample_count < TRADER_SAMPLE_THRESHOLD_DAYS;

  const headlineCards = [
    {
      key: "total_return",
      label: sc.metrics.totalReturnLabel,
      value: metricText(m.total_return_pct, m.total_return_pct_insufficient, "%"),
      insufficient: m.total_return_pct_insufficient,
      note: sc.metrics.totalReturnNote,
    },
    {
      key: "max_drawdown",
      label: sc.metrics.maxDrawdownLabel,
      value: metricText(m.max_drawdown_pct, m.max_drawdown_pct_insufficient, "%"),
      insufficient: m.max_drawdown_pct_insufficient,
      note: sc.metrics.maxDrawdownNote,
    },
    {
      key: "win_rate",
      label: sc.metrics.winRateLabel,
      value: metricText(m.win_rate_pct, m.win_rate_pct_insufficient, "%"),
      insufficient: m.win_rate_pct_insufficient,
      note: `${sc.metrics.winRateNotePrefix}${m.sample_count}${sc.metrics.winRateNoteSuffix}`,
    },
  ];

  const collapsibleCards = [
    {
      key: "sharpe",
      label: sc.metrics.sharpeLabel,
      value: metricText(m.sharpe, m.sharpe_insufficient),
      insufficient: m.sharpe_insufficient,
      note: m.sharpe_se_insufficient || m.sharpe_se == null
        ? "" : `±${m.sharpe_se}${sc.metrics.sharpeNoteSuffix}`,
    },
    {
      key: "annualized_vol",
      label: sc.metrics.annualizedVolLabel,
      value: metricText(m.annualized_vol_pct, m.annualized_vol_pct_insufficient, "%"),
      insufficient: m.annualized_vol_pct_insufficient,
      note: sc.metrics.annualizedVolNote,
    },
    {
      key: "sortino",
      label: sc.metrics.sortinoLabel,
      value: metricText(m.sortino, m.sortino_insufficient),
      insufficient: m.sortino_insufficient,
      note: sc.metrics.sortinoNote,
    },
    {
      key: "best_worst",
      label: sc.metrics.bestWorstLabel,
      value: `${metricText(m.best_day_pct, m.best_day_pct_insufficient)} / `
        + `${metricText(m.worst_day_pct, m.worst_day_pct_insufficient)}`,
      insufficient: m.best_day_pct_insufficient || m.worst_day_pct_insufficient,
      note: sc.metrics.bestWorstNote,
    },
    {
      key: "start_end_equity",
      label: sc.metrics.startEndEquityLabel,
      value: startEnd ? `${startEnd.start} → ${startEnd.end}` : sc.metrics.insufficientLabel,
      insufficient: startEnd === null,
      note: sc.metrics.startEndEquityNote,
    },
  ];

  const metricCards = sampleInsufficient ? headlineCards : [...headlineCards, ...collapsibleCards];

  return (
    <main className="page strategy-detail-page">
      <div className="mono strategy-detail-breadcrumb">
        {c.breadcrumb} / <span>{shortAddr(trader.address)}</span>
      </div>

      <div className="strategy-detail-headrow">
        <div>
          <div className="strategy-detail-title-row">
            <h1>{shortAddr(trader.address)}</h1>
          </div>
          <div className="strategy-detail-sub">
            <a className="mono" href={explorerHref} target="_blank" rel="noreferrer">
              {trader.address}
            </a>
          </div>
        </div>
        <div className="mono strategy-detail-asof">
          {c.asOfPrefix}
          {asOf}
          {c.sourceSuffix}
        </div>
      </div>

      <p className="hint">{c.disclaimerNote}</p>

      <div className="strategy-detail-grid">
        <div className="strategy-detail-left">
          <EquityCurve
            equityIndex={trader.equity_index}
            initialDepositUsd={trader.methodology.initial_deposit_usd}
            startDate={trader.methodology.start_date}
            endDate={trader.methodology.end_date}
          />

          <div className="metric-grid">
            {metricCards.map((card) => (
              <div className="card metric-card" key={card.key}>
                <div className="metric-card-label">{card.label}</div>
                <div className="mono metric-card-value">
                  {card.insufficient ? sc.metrics.insufficientLabel : card.value}
                </div>
                <div className="metric-card-note">{card.insufficient ? "" : card.note}</div>
              </div>
            ))}
          </div>

          {sampleInsufficient && (
            <p className="hint metric-collapsed-note">
              {sc.metrics.insufficientGroupLabel}
              {sc.metrics.insufficientGroupPrefix}
              {m.sample_count}
              {sc.metrics.insufficientGroupMid}
              {TRADER_SAMPLE_THRESHOLD_DAYS}
              {sc.metrics.insufficientGroupSuffix}
            </p>
          )}
        </div>

        <div className="card strategy-follow-panel">
          <div className="strategy-follow-panel-heading">{c.panel.heading}</div>

          <div className="inset strategy-follow-estimate">
            <div className="strategy-follow-estimate-row">
              <span>{c.accountValueLabel}</span>
              <span className="mono">{fmtAmount(trader.account_value)}</span>
            </div>
          </div>

          {trader.follow_blocked ? (
            <p className="hint">{c.panel.followBlocked}</p>
          ) : (
            <>
              <button
                type="button"
                className="btn btn-primary btn-block"
                disabled={phase !== "idle"}
                onClick={() => void handleCta()}
              >
                {phase === "connecting"
                  ? c.panel.ctaConnecting
                  : phase === "signing"
                    ? c.panel.ctaSigning
                    : c.panel.cta}
              </button>
              {error && (
                <div className="sign-error">
                  <p>{error}</p>
                </div>
              )}
              <p className="hint strategy-follow-footnote">{c.panel.footnote}</p>
            </>
          )}
        </div>
      </div>
    </main>
  );
}
