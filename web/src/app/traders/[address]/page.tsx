"use client";
/**
 * `/traders/[address]` — 交易員詳情頁（M3 round2 Task 6 首建；M3 round4
 * Task R4-11 版型對齊 `/strategies/[slug]`；2026-09-05 explore/trader 指標統一
 * plan Task 6 改四窗切換＋與探索清單同源欄位）。
 *
 * leaderboard（`/leaderboard`）任意地址的鏈上績效展示頁，**不受精選白名單管轄**
 * ——資料源是公開端點 `GET /api/public/traders/{address}`。`windows`／`live_days`／
 * `fills_30d`／`exposure` 與 `/api/public/explore`（`ExploreRow`）共用同一組後端
 * 純函式（`spark.filet.trader_stats`，工程原則 1：兩頁的每一個數字只能從同一處
 * 出來）——四窗切換（day/week/month/allTime，預設 `month`，D10）不重打 API，
 * 四窗資料已一次回來，只切換本地顯示。
 *
 * ⭐ D6（2026-09-04 使用者否決移除版，改為保留）：`metrics`（Sharpe/Sortino/
 * 年化波動/日勝率/最佳最差日）改逐窗，網格只渲染**比率型指標**，**不重複渲染**
 * `total_return_pct`／`max_drawdown_pct`——損益與回撤由窗卡（`windows[w]`）顯示，
 * 避免同頁兩個回撤數字。CAGR 只算 allTime（`sample_days`／`sample_threshold`
 * 為頁面層欄位，與所選窗無關）。
 * ⭐⭐ 2026-09-05 Task 9 Step 2（reviewer W4）：`sample_days<sample_threshold`
 * （`sampleInsufficient`）**只**守 `CagrCard`（`cagr_pct` 鍵是否存在，後端
 * `build_cagr_fields` 結構性防呆）——不再拿它去摺疊比率型指標網格。網格一律
 * 渲染該窗全部比率型指標卡，每張各自依 `metrics[window_].<key>_insufficient`
 * 顯示「樣本不足」。原因：`sampleInsufficient` 只看 allTime 樣本量，但網格顯示
 * 的是**所選窗**（`windowSel`）的指標——一個 allTime 樣本不足的帳戶切到 30D
 * 窗，30D 底下的 Sharpe 等指標可能是充足的，被 allTime 門檻牽連隱藏就違反
 * D6「指標逐窗、跟著選窗切換」的精神。`equity_index` 已移除，由損益曲線
 * （`PnlCurve`，讀 `windows[w].spark`）取代——`EquityCurve` 與
 * `MethodologyCard`（其 prop 型別 `PublicStrategyMethodology` 含
 * `start_date`/`end_date`/`annualization_days`/`risk_free_rate`，與新版精簡的
 * `PublicTraderMethodology` 不相容，見 `lib/publicApi.ts` 該型別檔頭「不共用
 * 同一個介面」）本頁均不再使用。
 *
 * ⭐ M3 round4 Task R4-11（版型對齊）：右欄跟單面板（含投入比例／回撤 slider）
 * 本次改版**完全不動**——差異全部收在 `FollowPanel` 的 props：
 * 1. 面板頂部多一行進階模式無背書說明（`COPY.advanced.gate.body`）。
 * 2. 槓桿唯讀列：本頁沒有平台審核過的槓桿上限，顯示 `NO_VALUE`（「—」）。
 * 3. CTA 導向 `strategy=advanced:{address}`，查詢字串可帶 `scale`／`dd`。
 *
 * ⭐⭐ CTA「連接錢包並繼續」**原封不動**沿用 `/strategies/[slug]` 的
 * connect→SIWE→跳轉流程：`strategy=advanced:{address}`——後端
 * `_admit_custom_leader` 在送出簽章那一刻重新准入，本頁不重新實作。
 *
 * ⭐ [W3] 標題**一律**用 `shortAddr(trader.address)`，不信任 `?name=`。
 * ⭐ [W4] `follow_blocked=true`：隱藏 CTA、改顯示提示文案。
 */
import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { useAccount, useConnect, useSignMessage } from "wagmi";
import { CagrCard } from "@/components/CagrCard";
import {
  DD_DEFAULT, FollowPanel, SCALE_DEFAULT,
} from "@/components/FollowPanel";
import { PnlCurve } from "@/components/PnlCurve";
import { fmtAmount, fmtSignedUsd, fmtUpdatedAtUtc, NO_VALUE, shortAddr } from "@/lib/format";
import { useQueryClient } from "@tanstack/react-query";
import { useMe } from "@/lib/hooks";
import { useCopy } from "@/lib/lang";
import {
  EXPLORE_WINDOWS, getPublicTraderDetail, type ExploreWindow, type PublicTraderDetail,
} from "@/lib/publicApi";
import { loginWithSiwe } from "@/lib/siwe";
import { metricText, type MetricCardDef } from "@/lib/strategyMetrics";

type ConnectPhase = "idle" | "connecting" | "signing";

const DEFAULT_WINDOW: ExploreWindow = "month"; // D10（2026-09-05）：與探索清單預設一致，最穩的窗。

function isExploreWindow(v: string | null): v is ExploreWindow {
  return v != null && (EXPLORE_WINDOWS as readonly string[]).includes(v);
}

/** `useSearchParams()` 在 build 期 prerender 需要 Suspense 邊界（Next.js
 * missing-suspense-with-csr-bailout，見 `advanced/page.tsx`／`onboarding/page.tsx`
 * 同寫法）。頁面本體在 `TraderDetailInner`。fallback 留空：本頁全 client 資料，
 * 無首繪內容可給。 */
export default function TraderDetailPage() {
  return (
    <Suspense fallback={null}>
      <TraderDetailInner />
    </Suspense>
  );
}

function TraderDetailInner() {
  const params = useParams<{ address: string }>();
  const routeAddress = params?.address ?? "";
  const router = useRouter();
  const searchParams = useSearchParams();
  const COPY = useCopy();
  const c = COPY.traders;
  const sc = COPY.strategyDetail; // 指標卡／CAGR 文案沿用（通用績效用語）

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

  // 2026-09-05 Task 6：四窗切換只改本地 state＋URL query，四窗資料已一次回來，
  // 不重打 API。初始值讀 `?window=`（explore 頁「查看」連結帶所選窗過來，D10：
  // 非法／缺席一律退回預設 `month`）。用 `window.history.replaceState` 而非
  // Next router 更新網址——這是純顯示狀態同步，不是導航，不需要進歷史堆疊。
  const [windowSel, setWindowSel] = useState<ExploreWindow>(() => {
    const w = searchParams.get("window");
    return isExploreWindow(w) ? w : DEFAULT_WINDOW;
  });

  function handleWindowChange(w: ExploreWindow) {
    setWindowSel(w);
    if (typeof window !== "undefined") {
      const url = new URL(window.location.href);
      url.searchParams.set("window", w);
      window.history.replaceState(null, "", url.toString());
    }
  }

  const queryClient = useQueryClient();
  const me = useMe();
  const loggedIn = !!me.data;
  const { address: walletAddress, chainId, isConnected } = useAccount();
  const { connectAsync, connectors } = useConnect();
  const { signMessageAsync } = useSignMessage();
  const [phase, setPhase] = useState<ConnectPhase>("idle");
  const [error, setError] = useState<string | null>(null);

  // 投入比例／回撤 slider（R4-11：版型對齊策略頁，見檔頭）。
  const [scalePct, setScalePct] = useState(SCALE_DEFAULT);
  const [ddEnabled, setDdEnabled] = useState(false);
  const [ddPct, setDdPct] = useState(DD_DEFAULT);

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

  const stats = trader.windows[windowSel];
  const wm = trader.metrics[windowSel];
  const explorerHref = `https://app.hyperliquid.xyz/explorer/address/${trader.address}`;
  const asOf = fmtUpdatedAtUtc(trader.methodology.updated_at);

  function buildQuery(): string {
    const p = new URLSearchParams();
    p.set("strategy", `advanced:${trader!.address}`);
    p.set("scale", String(scalePct));
    if (ddEnabled) p.set("dd", String(ddPct));
    return p.toString();
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
      void queryClient.resetQueries(); // 身份切換整包清（同 Header，2026-09-01 快取殘留事故）
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

  // ⭐ R4-11：改用後端直接供給的 sample_days／sample_threshold（與策略頁同一套
  // `build_cagr_fields` 組裝規則）——allTime 專屬的整體樣本量門檻，只守
  // `CagrCard`（`trader.cagr_pct != null` 已是結構性防呆，見 `CagrCard.tsx`
  // 檔頭），2026-09-05 Task 9 Step 2 起不再用來摺疊下方指標網格（見檔頭 D6 說明）。

  // D6：指標網格只渲染比率型指標（sharpe／sharpe_se／annualized_vol_pct／
  // sortino／win_rate_pct／best_day_pct／worst_day_pct），不再渲染
  // total_return_pct／max_drawdown_pct（窗卡已顯示 pnl_usd／max_dd_pct，同頁不要
  // 兩個回撤數字）。Task 9 Step 2 起網格全卡一律渲染，每張各自依對應
  // `*_insufficient` 顯示「樣本不足」，不再整組隨 allTime 樣本量摺疊。
  const headlineCards: MetricCardDef[] = [
    {
      key: "win_rate",
      label: sc.metrics.winRateLabel,
      value: metricText(wm.win_rate_pct, wm.win_rate_pct_insufficient, "%"),
      insufficient: wm.win_rate_pct_insufficient,
      note: `${sc.metrics.winRateNotePrefix}${wm.sample_count}${sc.metrics.winRateNoteSuffix}`,
    },
  ];

  const collapsibleCards: MetricCardDef[] = [
    {
      key: "sharpe",
      label: sc.metrics.sharpeLabel,
      value: metricText(wm.sharpe, wm.sharpe_insufficient),
      insufficient: wm.sharpe_insufficient,
      note: wm.sharpe_se_insufficient || wm.sharpe_se == null
        ? "" : `±${wm.sharpe_se}${sc.metrics.sharpeNoteSuffix}`,
    },
    {
      key: "annualized_vol",
      label: sc.metrics.annualizedVolLabel,
      value: metricText(wm.annualized_vol_pct, wm.annualized_vol_pct_insufficient, "%"),
      insufficient: wm.annualized_vol_pct_insufficient,
      note: sc.metrics.annualizedVolNote,
    },
    {
      key: "sortino",
      label: sc.metrics.sortinoLabel,
      value: metricText(wm.sortino, wm.sortino_insufficient),
      insufficient: wm.sortino_insufficient,
      note: sc.metrics.sortinoNote,
    },
    {
      key: "best_worst",
      label: sc.metrics.bestWorstLabel,
      insufficient: wm.best_day_pct_insufficient || wm.worst_day_pct_insufficient,
      note: sc.metrics.bestWorstNote,
      pair: {
        a: metricText(wm.best_day_pct, wm.best_day_pct_insufficient),
        sep: "/",
        b: metricText(wm.worst_day_pct, wm.worst_day_pct_insufficient),
      },
    },
  ];

  const metricCards = [...headlineCards, ...collapsibleCards];

  const ddText = stats == null || stats.max_dd_pct == null ? null : `${stats.max_dd_pct.toFixed(1)}%`;
  // ⭐ Task 9 Step 3（reviewer S4）：`neg`（紅字）class 只在確定是負值時加；
  // `stats` 缺席或 `max_dd_pct` 算不出（null，顯示「—」）用中性樣式，不得讓
  // 一個算不出的回撤看起來像已知的負數。
  const ddNeg = stats != null && stats.max_dd_pct != null && stats.max_dd_pct < 0;
  const exposureText = trader.exposure == null
    ? NO_VALUE
    : trader.exposure.pct != null
      ? `${COPY.explore.exposureDir[trader.exposure.dir]} ${trader.exposure.pct.toFixed(1)}%`
      : COPY.explore.exposureDir[trader.exposure.dir];
  // ⭐ Task 9 Step 1（reviewer W2）：`fills_30d` 可能為 `null`（上游成交抓取
  // 失敗，見 `publicApi.ts` 型別檔頭）——顯示 `c.fillsUnavailable`，不得渲染
  // 四個偽造的 0。
  const fills = trader.fills_30d;

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
      {/* ⭐ R4-11：這個交易員頁專屬欄位移到頁面層單獨一行，不塞進共用
          `FollowPanel`。帳戶價值讀 `methodology.end_equity_usd`（allTime
          `accountValueHistory` 末值），與損益曲線（`windows[w].spark`）不同源
          （工程原則 1：`account_value` 來自另一個端點 clearinghouseState），
          label 不變、來源不變（沿舊版 issue log I-19 修法）。 */}
      <p className="hint trader-account-value">
        <span>{c.accountValueLabel}</span>
        <span className="mono">{fmtAmount(trader.methodology.end_equity_usd)}</span>
      </p>
      {/* 2026-09-05（Task 7 補回）：Task 6 移除了 MethodologyCard，但後端
          methodology.start/end_equity_usd／initial_deposit_usd 仍回傳，使用者
          要求「其餘資料全部保留」——沿用起訖淨值文案 key，加一行不建整張卡。 */}
      <div className="trader-account-row">
        <span>{c.startEndEquityLabel}</span>
        <span className="mono">
          {fmtAmount(trader.methodology.start_equity_usd)} → {fmtAmount(trader.methodology.end_equity_usd)}
        </span>
      </div>
      {trader.methodology.initial_deposit_usd != null && (
        <div className="trader-account-row">
          <span>{c.initialDepositLabel}</span>
          <span className="mono">{fmtAmount(trader.methodology.initial_deposit_usd)}</span>
        </div>
      )}

      <div className="explore-window-group trader-window-group" role="group" aria-label={c.windowsLabel}>
        {EXPLORE_WINDOWS.map((w) => (
          <button
            key={w}
            type="button"
            className="explore-window-btn"
            data-active={windowSel === w}
            aria-pressed={windowSel === w}
            onClick={() => handleWindowChange(w)}
          >
            {COPY.explore.windows[w]}
          </button>
        ))}
      </div>

      <div className="strategy-detail-grid">
        <div className="strategy-detail-left">
          <div className="metric-grid">
            <div className="card metric-card">
              <div className="metric-card-label">{c.pnlLabel}</div>
              <div className={`mono metric-card-value${stats == null ? "" : stats.pnl_usd >= 0 ? " pos" : " neg"}`}>
                {stats == null ? NO_VALUE : fmtSignedUsd(stats.pnl_usd)}
              </div>
            </div>
            <div className="card metric-card">
              <div className="metric-card-label">{c.ddLabel}</div>
              <div className={`mono metric-card-value${ddNeg ? " neg" : ""}`}>
                {ddText == null
                  ? <span title={c.ddUnavailableTitle}>{c.ddUnavailable}</span>
                  : ddText}
              </div>
            </div>
            <div className="card metric-card">
              <div className="metric-card-label">{c.liveDaysLabel}</div>
              <div className="mono metric-card-value">{trader.live_days}</div>
            </div>
            <div className="card metric-card">
              <div className="metric-card-label">{c.exposureLabel}</div>
              <div className="mono metric-card-value">{exposureText}</div>
            </div>
          </div>

          <PnlCurve values={stats?.spark ?? []} ariaLabel={c.pnlCurveLabel} />
          <p className="hint">{c.pnlSourceNote}</p>
          <p className="hint">{c.ddDefinition}</p>

          <div className="metric-grid">
            {metricCards.map((card) => (
              <div className={`card metric-card${card.pair ? " metric-card-pair" : ""}`} key={card.key}>
                <div className="metric-card-label">{card.label}</div>
                <div className="mono metric-card-value">
                  {card.insufficient ? (
                    sc.metrics.insufficientLabel
                  ) : card.pair ? (
                    <>
                      <span className="metric-card-value-a">{card.pair.a}</span>
                      <span className="metric-card-value-b">{card.pair.sep} {card.pair.b}</span>
                    </>
                  ) : card.value}
                </div>
                <div className="metric-card-note">{card.insufficient ? "" : card.note}</div>
              </div>
            ))}
          </div>

          {/* ⭐ R4-11：與策略頁同一套結構性防呆——`sample_days<sample_threshold`
              時 `cagr_pct` 鍵整個不存在，本頁只依「鍵是否存在」決定是否渲染。 */}
          {trader.cagr_pct != null && (
            <CagrCard cagr={trader.cagr_pct} sampleDays={trader.sample_days} copy={sc.cagr} />
          )}

          <div className="trader-fills-card card">
            <div className="methodology-heading">{c.fillsHeading}</div>
            {fills == null ? (
              <p className="hint">{c.fillsUnavailable}</p>
            ) : (
              <>
                <div className="metric-grid">
                  <div className="card metric-card">
                    <div className="metric-card-label">{c.orders}</div>
                    <div className="mono metric-card-value">{fills.order_count}</div>
                  </div>
                  <div className="card metric-card">
                    <div className="metric-card-label">{c.closedPositions}</div>
                    <div className="mono metric-card-value">{fills.closed_positions}</div>
                  </div>
                  <div className="card metric-card">
                    <div className="metric-card-label">{c.winRate}</div>
                    <div className="mono metric-card-value">
                      {fills.win_rate_pct == null ? NO_VALUE : `${fills.win_rate_pct}%`}
                    </div>
                  </div>
                  <div className="card metric-card">
                    <div className="metric-card-label">{c.realizedPnl}</div>
                    <div className={`mono metric-card-value${fills.realized_pnl_usd >= 0 ? " pos" : " neg"}`}>
                      {fmtSignedUsd(fills.realized_pnl_usd)}
                    </div>
                  </div>
                </div>
                {fills.truncated && <p className="hint">{c.fillsTruncatedNote}</p>}
              </>
            )}
          </div>
        </div>

        <FollowPanel
          heading={c.panel.heading}
          copy={sc.panel}
          // ⭐ R4-11 差異 2：本頁沒有平台審核過的槓桿上限，顯示 `NO_VALUE`
          // （「—」），不臆造數字（工程原則 1）。
          leverageDisplay={NO_VALUE}
          leverageInfoPrefix={COPY.wizard.leverageInfoPrefix}
          leverageInfoSuffix={COPY.wizard.leverageInfoSuffix}
          scalePct={scalePct}
          onScalePctChange={setScalePct}
          ddEnabled={ddEnabled}
          onDdEnabledChange={setDdEnabled}
          ddPct={ddPct}
          onDdPctChange={setDdPct}
          phase={phase}
          error={error}
          onCta={() => void handleCta()}
          // ⭐ R4-11 差異 1：進階模式無背書說明，沿用 `/advanced` 頁同一句
          // （`COPY.advanced.gate.body`），不另開一組重複 key。
          advancedNote={COPY.advanced.gate.body}
          disabledState={trader.follow_blocked ? { kind: "blocked", note: c.panel.followBlocked } : undefined}
        />
      </div>
    </main>
  );
}
