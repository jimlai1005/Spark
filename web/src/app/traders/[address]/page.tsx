"use client";
/**
 * `/traders/[address]` — 交易員詳情頁（M3 round2 Task 6 首建；M3 round4
 * Task R4-11 版型對齊 `/strategies/[slug]`，使用者三項回饋之一）。
 *
 * leaderboard（`/leaderboard`）任意地址的鏈上績效展示頁，**不受精選白名單管轄**
 * ——資料源是公開端點 `GET /api/public/traders/{address}`，計算與
 * `/strategies/[slug]` 共用同一份後端純函式（`filet.strategies.build_metrics`／
 * `build_equity_index`／`build_methodology`／`build_cagr_fields`），前端亦共用
 * 同一批純算術（`lib/strategyMetrics.ts`）與元件（`EquityCurve`／`CagrCard`／
 * `MethodologyCard`／`FollowPanel`），兩頁的公式與圖表渲染只有一份。
 *
 * ⭐ M3 round4 Task R4-11（版型對齊）：本頁版型自此與 `/strategies/[slug]`
 * 完整對齊——淨值曲線、指標卡組（含最佳/最差日、起訖淨值）、CAGR 收合卡、
 * 方法論與樣本揭露、右欄跟單面板（含投入比例／回撤 slider）全部齊備，
 * 不再是「plan 明訂範圍」的精簡版（沿舊版註解，已過時移除）。唯三差異全部
 * 收在 `FollowPanel` 的 props，不寫死在頁面裡：
 * 1. 面板頂部多一行進階模式無背書說明（`COPY.advanced.gate.body`，與 `/advanced`
 *    頁同一句無背書語義，不另開一組重複 key）。
 * 2. 槓桿唯讀列：本頁沒有平台審核過的槓桿上限（任意鏈上地址，非策展），顯示
 *    `NO_VALUE`（「—」），不臆造數字（工程原則 1）。
 * 3. CTA 導向 `strategy=advanced:{address}`（沿 `/advanced` 頁同一個入口語義），
 *    不是 `strategy={slug}`；查詢字串同樣可帶 `scale`／`dd`。
 *
 * ⭐⭐ CTA「連接錢包並繼續」**原封不動**沿用 `/strategies/[slug]` 的
 * connect→SIWE→跳轉流程：`strategy=advanced:{address}`——與 `/advanced` 頁
 * （Task 11）產生的格式完全相同，onboarding（`page.tsx` `ADVANCED_PREFIX`）與
 * `StepConfirm` 的 `postLeaderSelect` 已經吃這個格式：非精選位址在**送出簽章
 * 那一刻**由後端 `_admit_custom_leader` 重新准入並寫入 `user_leaders`
 * registry（見 `publicapi/app.py` `leaders_select` 端點 4a 段），本頁不需要、
 * 也不重新實作那段准入或 registry 邏輯。
 *
 * ⭐ [W3] 2026-08-29 opus 審查修正：標題**一律**用 `shortAddr(trader.address)`
 * ——不再信任 `?name=` 查詢參數（那是 client 端可任意竄改的值，曾經被拿來當
 * 顯示名稱直接渲染）。displayName 現在只在 `/leaderboard` 表格內顯示。
 * ⭐ [W4] 已被平台安全撤銷（`enabled=false`）的 leader：`follow_blocked=true`
 * 時隱藏 CTA、改顯示提示文案，不讓新客戶點進一個已撤銷的地址
 * （`FollowPanel` 的 `disabledState={{kind:"blocked",...}}`，與策略頁
 * `{kind:"pending",...}` 是刻意不同形狀，見該元件檔頭）。
 *
 * ⭐ M3 round3 Task 7（R2-P0 指標收斂，比照 `/strategies/[slug]`）：本頁沿用
 * 同一組 headline／collapse 分組。⭐ R4-11 起改用後端直接供給的
 * `sample_days`／`sample_threshold`（`build_cagr_fields`，與策略頁同一套組裝
 * 規則）取代先前的 `metrics.sample_count` ＋前端鏡射常數 30——不再需要自己
 * 鏡射門檻值。
 */
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useAccount, useConnect, useSignMessage } from "wagmi";
import { CagrCard } from "@/components/CagrCard";
import { EquityCurve } from "@/components/EquityCurve";
import {
  DD_DEFAULT, FollowPanel, SCALE_DEFAULT,
} from "@/components/FollowPanel";
import { MethodologyCard } from "@/components/MethodologyCard";
import { fmtAmount, fmtUpdatedAtUtc, NO_VALUE, shortAddr } from "@/lib/format";
import { useMe } from "@/lib/hooks";
import { useCopy } from "@/lib/lang";
import { getPublicTraderDetail, type PublicTraderDetail } from "@/lib/publicApi";
import { loginWithSiwe } from "@/lib/siwe";
import { formatDepositEquivalentEquity, metricText, type MetricCardDef } from "@/lib/strategyMetrics";

type ConnectPhase = "idle" | "connecting" | "signing";

export default function TraderDetailPage() {
  const params = useParams<{ address: string }>();
  const routeAddress = params?.address ?? "";
  const router = useRouter();
  const COPY = useCopy();
  const c = COPY.traders;
  const sc = COPY.strategyDetail; // 指標卡／CAGR／方法論／面板 slider 文案沿用（通用績效用語）

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

  const m = trader.metrics;
  const explorerHref = `https://app.hyperliquid.xyz/explorer/address/${trader.address}`;
  const asOf = fmtUpdatedAtUtc(trader.methodology.updated_at);
  const startEnd = formatDepositEquivalentEquity(
    trader.methodology.initial_deposit_usd, trader.equity_index, fmtAmount,
  );

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
  // `build_cagr_fields` 組裝規則），取代先前 `metrics.sample_count` ＋前端鏡射
  // 常數 30 的 workaround（見檔頭）。
  const sampleInsufficient = trader.sample_days < trader.sample_threshold;

  const headlineCards: MetricCardDef[] = [
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

  const collapsibleCards: MetricCardDef[] = [
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
      insufficient: m.best_day_pct_insufficient || m.worst_day_pct_insufficient,
      note: sc.metrics.bestWorstNote,
      pair: {
        a: metricText(m.best_day_pct, m.best_day_pct_insufficient),
        sep: "/",
        b: metricText(m.worst_day_pct, m.worst_day_pct_insufficient),
      },
    },
    {
      key: "start_end_equity",
      label: sc.metrics.startEndEquityLabel,
      insufficient: startEnd === null,
      note: sc.metrics.startEndEquityNote,
      pair: startEnd ? { a: startEnd.start, sep: "→", b: startEnd.end } : { a: NO_VALUE, sep: "", b: "" },
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
      {/* ⭐ R4-11：`account_value`（clearinghouseState，與 equity_index 不同源，
          工程原則 1）原本顯示在右欄面板的估算區——右欄改用共用 `FollowPanel`
          （與策略頁同一組估算文案）後，這個交易員頁專屬欄位移到頁面層單獨
          一行，不塞進共用元件（避免共用元件多開一條「traders 專屬 prop」）。 */}
      <p className="hint trader-account-value">
        <span>{c.accountValueLabel}</span>
        <span className="mono">{fmtAmount(trader.account_value)}</span>
      </p>

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

          {sampleInsufficient && (
            <p className="hint metric-collapsed-note">
              {sc.metrics.insufficientGroupLabel}
              {sc.metrics.insufficientGroupPrefix}
              {trader.sample_days}
              {sc.metrics.insufficientGroupMid}
              {trader.sample_threshold}
              {sc.metrics.insufficientGroupSuffix}
            </p>
          )}

          {/* ⭐ R4-11：與策略頁同一套結構性防呆——`sample_days<sample_threshold`
              時 `cagr_pct` 鍵整個不存在，本頁只依「鍵是否存在」決定是否渲染。 */}
          {trader.cagr_pct != null && (
            <CagrCard cagr={trader.cagr_pct} sampleDays={trader.sample_days} copy={sc.cagr} />
          )}

          <MethodologyCard methodology={trader.methodology} metrics={m} copy={sc.methodology} />
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
