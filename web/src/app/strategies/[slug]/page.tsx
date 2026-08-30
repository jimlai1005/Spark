"use client";
/**
 * `/strategies/[slug]` — 策略詳情頁（決策頁／白手套驗證門面，Task 9，設計稿 §04）。
 *
 * ⭐⭐ 錢包連接**第一次**在本頁出現（NOTE 07）。舊首頁的 SIWE 登入流程
 * （`useConnect`/`useSignMessage`/`loginWithSiwe`，見 git history `0eb12df~1:
 * web/src/app/page.tsx`）原封不動搬到這裡的跟單面板 CTA：右欄三個 slider
 * 未連錢包即可調整，只有最後一顆「連接錢包並繼續」需要錢包——這是刻意的
 * 產品順序（先設定完、再簽名，降低跳出）。已登入者點同一顆按鈕直接帶參數
 * 跳轉，不重複走一次連線/簽名。
 *
 * ⭐ M3 round3 Task 7（D5 數字一致性）：CAGR 不再是前端自算——後端
 * `/api/public/strategies/{slug}` 直接供給 `cagr_pct`（`sample_days<30`——
 * 2026-08-30 D15 裁決原 60 降為 30——時該鍵整個不回傳，結構性防呆），
 * 本頁只依「鍵是否存在」決定是否渲染 `CagrCard`，
 * 不再重算年化外推（見 `lib/strategyMetrics.ts` 檔頭，工程原則 1：同一個值
 * 只能有一個計算來源）。⭐ M3 round4 Task R4-8（2026-08-31 使用者裁決）：
 * 「起訖淨值」改與淨值曲線同一基準——`initial_deposit_usd` × `equity_index`
 * 首尾比值（TWR 等效淨值，見 `strategyMetrics.ts` 檔頭：`equity_index` 首點
 * 恆為 1，與曾造成樣本不足誤判的 `accountValueHistory` 不同源）。
 *
 * ⭐ Task 7（R2-P0）指標收斂：8 張指標卡中只有總報酬／策略期間回撤／日勝率／
 * 最佳最差日維持個別小卡；Sharpe／Sortino／年化波動／起訖淨值在
 * `sample_days < sample_threshold` 時摺成一行文字，不逐格判斷（版面以整體
 * 門檻為準）——避免「8 格中 5 格是樣本不足、佔兩屏高度」（design R2 issue）。
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
import { fmtAmount, fmtUpdatedAtUtc, NO_VALUE, resolveTagline, shortAddr } from "@/lib/format";
import { useMe } from "@/lib/hooks";
import { useCopy, useLang } from "@/lib/lang";
import { getPublicStrategy, type PublicStrategyDetail } from "@/lib/publicApi";
import { loginWithSiwe } from "@/lib/siwe";
import {
  formatDepositEquivalentEquity, metricText, type MetricCardDef,
} from "@/lib/strategyMetrics";

type ConnectPhase = "idle" | "connecting" | "signing";

export default function StrategyDetailPage() {
  const params = useParams<{ slug: string }>();
  const slug = params?.slug ?? "";
  const router = useRouter();
  const COPY = useCopy();
  const { lang } = useLang();
  const c = COPY.strategyDetail;

  // undefined＝載入中；null＝404／讀不到（兩者對使用者都是「這裡沒有可看的東西」）。
  const [strategy, setStrategy] = useState<PublicStrategyDetail | null | undefined>(undefined);
  useEffect(() => {
    let cancelled = false;
    setStrategy(undefined);
    getPublicStrategy(slug).then((r) => {
      if (!cancelled) setStrategy(r);
    });
    return () => {
      cancelled = true;
    };
  }, [slug]);

  // Task 17（SEO）：動態 slug 頁沒有 SSR metadata（client component 不能
  // export generateMetadata），策略載入後在 client 端補上 `%s｜Filet` 標題；
  // 載入中／404 維持 `strategies/layout.tsx` 給的預設標題，不覆蓋成空字串。
  useEffect(() => {
    if (strategy) document.title = `${strategy.name}｜Filet`;
  }, [strategy]);

  const me = useMe();
  const loggedIn = !!me.data;
  const { address, chainId, isConnected } = useAccount();
  const { connectAsync, connectors } = useConnect();
  const { signMessageAsync } = useSignMessage();
  const [phase, setPhase] = useState<ConnectPhase>("idle");
  const [error, setError] = useState<string | null>(null);

  // 兩個 slider（NOTE 07：未連錢包即可調）。槓桿上限 Task 10b 起改唯讀資訊列
  // （無 per-user 簽章通道，見 StepRiskLimits.tsx 檔頭），不再是使用者可調的值。
  const [scalePct, setScalePct] = useState(SCALE_DEFAULT);
  const [ddEnabled, setDdEnabled] = useState(false); // 裁決 1：預設關閉
  const [ddPct, setDdPct] = useState(DD_DEFAULT);

  if (strategy === undefined) {
    return (
      <main className="page">
        <p className="hint">{c.loadingNote}</p>
      </main>
    );
  }
  if (strategy === null) {
    return (
      <main className="page">
        <div className="narrow">
          <h1>{c.notFoundTitle}</h1>
          <p>{c.notFoundBody}</p>
          <Link className="btn btn-primary" href="/strategies">
            {c.backToList}
          </Link>
        </div>
      </main>
    );
  }

  const m = strategy.metrics;
  const explorerHref = `https://app.hyperliquid.xyz/explorer/address/${strategy.leader_address}`;
  // ⭐ D5：`as_of`（perf 快照時間戳，列表／詳情同一份）取代 `methodology.updated_at`
  // （那是每次請求各自的 `now_fn()`，即使算同一份快照也會逐請求前進——正是
  // 「數字不一致」的根因之一）。
  const asOf = fmtUpdatedAtUtc(strategy.as_of ?? 0);
  const startEnd = formatDepositEquivalentEquity(
    strategy.methodology.initial_deposit_usd, strategy.equity_index, fmtAmount,
  );
  const sampleInsufficient = strategy.sample_days < strategy.sample_threshold;

  function buildQuery(): string {
    const p = new URLSearchParams();
    p.set("strategy", strategy!.slug);
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
      let addr = address;
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
  // （plan Task 7 第 1 條＋設計稿 R2 P0 原文「大字只留總報酬、最大回撤、日勝率」）。
  // Sharpe／Sortino／年化波動／起訖淨值／最佳最差日只在 `sampleInsufficient`
  // 為 false 時才併入同一個 metric-grid；為 true 時整組摺成下方一行文字
  // （不逐格判斷，見檔頭）。
  const headlineCards: MetricCardDef[] = [
    {
      key: "total_return",
      label: c.metrics.totalReturnLabel,
      value: metricText(m.total_return_pct, m.total_return_pct_insufficient, "%"),
      insufficient: m.total_return_pct_insufficient,
      note: c.metrics.totalReturnNote,
    },
    {
      key: "max_drawdown",
      label: c.metrics.maxDrawdownLabel,
      value: metricText(m.max_drawdown_pct, m.max_drawdown_pct_insufficient, "%"),
      insufficient: m.max_drawdown_pct_insufficient,
      note: c.metrics.maxDrawdownNote,
    },
    {
      key: "win_rate",
      label: c.metrics.winRateLabel,
      value: metricText(m.win_rate_pct, m.win_rate_pct_insufficient, "%"),
      insufficient: m.win_rate_pct_insufficient,
      note: `${c.metrics.winRateNotePrefix}${m.sample_count}${c.metrics.winRateNoteSuffix}`,
    },
  ];

  const collapsibleCards: MetricCardDef[] = [
    {
      key: "sharpe",
      label: c.metrics.sharpeLabel,
      value: metricText(m.sharpe, m.sharpe_insufficient),
      insufficient: m.sharpe_insufficient,
      note: m.sharpe_se_insufficient || m.sharpe_se == null
        ? "" : `±${m.sharpe_se}${c.metrics.sharpeNoteSuffix}`,
    },
    {
      key: "annualized_vol",
      label: c.metrics.annualizedVolLabel,
      value: metricText(m.annualized_vol_pct, m.annualized_vol_pct_insufficient, "%"),
      insufficient: m.annualized_vol_pct_insufficient,
      note: c.metrics.annualizedVolNote,
    },
    {
      key: "sortino",
      label: c.metrics.sortinoLabel,
      value: metricText(m.sortino, m.sortino_insufficient),
      insufficient: m.sortino_insufficient,
      note: c.metrics.sortinoNote,
    },
    {
      key: "best_worst",
      label: c.metrics.bestWorstLabel,
      insufficient: m.best_day_pct_insufficient || m.worst_day_pct_insufficient,
      note: c.metrics.bestWorstNote,
      // ⭐ R4-11 項目 3：雙值卡改渲染成 { a, sep, b } 三段而非單一字串——
      // 讓 CSS 能在窄寬把 A／B 拆成兩行對齊，不靠瀏覽器隨機折行（見 globals.css
      // `.metric-card-pair`）。
      pair: {
        a: metricText(m.best_day_pct, m.best_day_pct_insufficient),
        sep: "/",
        b: metricText(m.worst_day_pct, m.worst_day_pct_insufficient),
      },
    },
    {
      key: "start_end_equity",
      label: c.metrics.startEndEquityLabel,
      insufficient: startEnd === null,
      note: c.metrics.startEndEquityNote,
      pair: startEnd ? { a: startEnd.start, sep: "→", b: startEnd.end } : { a: NO_VALUE, sep: "", b: "" },
    },
  ];

  const metricCards = sampleInsufficient ? headlineCards : [...headlineCards, ...collapsibleCards];

  const listable = strategy.listable;
  const tagline = resolveTagline(strategy, lang);

  return (
    <main className="page strategy-detail-page">
      <div className="mono strategy-detail-breadcrumb">
        {c.breadcrumb} / <span>{strategy.name}</span>
      </div>

      <div className="strategy-detail-headrow">
        <div>
          <div className="strategy-detail-title-row">
            <h1>{strategy.name}</h1>
            <span className="pill follow-pill" data-state={strategy.status === "running" ? "following" : "paused"}>
              <span className="follow-pill-dot" aria-hidden="true" />
              {strategy.status === "running" ? c.runningPill : c.pausedPill}
            </span>
          </div>
          <div className="strategy-detail-sub">
            {tagline ? `${tagline} · ` : ""}
            {c.leaderPrefix}
            <a className="mono" href={explorerHref} target="_blank" rel="noreferrer">
              {shortAddr(strategy.leader_address)}
              {c.leaderLinkSuffix}
            </a>
          </div>
        </div>
        <div className="mono strategy-detail-asof">
          {c.asOfPrefix}
          {asOf}
          {c.sourceSuffix}
        </div>
      </div>

      <div className="strategy-detail-grid">
        <div className="strategy-detail-left">
          <EquityCurve
            equityIndex={strategy.equity_index}
            initialDepositUsd={strategy.methodology.initial_deposit_usd}
            startDate={strategy.methodology.start_date}
            endDate={strategy.methodology.end_date}
          />

          <div className="metric-grid">
            {metricCards.map((card) => (
              <div className={`card metric-card${card.pair ? " metric-card-pair" : ""}`} key={card.key}>
                <div className="metric-card-label">{card.label}</div>
                <div className="mono metric-card-value">
                  {card.insufficient ? (
                    c.metrics.insufficientLabel
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
              {c.metrics.insufficientGroupLabel}
              {c.metrics.insufficientGroupPrefix}
              {strategy.sample_days}
              {c.metrics.insufficientGroupMid}
              {strategy.sample_threshold}
              {c.metrics.insufficientGroupSuffix}
            </p>
          )}

          {/* ⭐ D5：後端 `sample_days<sample_threshold`（30，2026-08-30 D15 裁決
              原 60 降為 30）時 `cagr_pct` 鍵整個不存在——結構性防呆，前端只需
              判斷「有沒有這個值」，不必自己重算門檻。 */}
          {strategy.cagr_pct != null && (
            <CagrCard cagr={strategy.cagr_pct} sampleDays={strategy.sample_days} copy={c.cagr} />
          )}

          <MethodologyCard methodology={strategy.methodology} metrics={m} copy={c.methodology} />
        </div>

        <FollowPanel
          heading={c.panel.heading}
          copy={c.panel}
          leverageDisplay={strategy.max_leverage ? `${strategy.max_leverage}x` : NO_VALUE}
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
          onCta={handleCta}
          disabledState={listable ? undefined : { kind: "pending", cta: c.panel.pendingCta, note: c.panel.pendingNote }}
        />
      </div>
    </main>
  );
}

