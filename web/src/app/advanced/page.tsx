"use client";
/**
 * `/advanced` — 進階模式（Task 11，設計稿 §03 進階模式卡＋NOTE 05）。
 *
 * ⭐⭐ 自舊版 `/leaders` 遷移重構。與舊版最大的行為差異：選定位址後**不在本頁
 * 簽章**——按下後 `router.push` 帶 `advanced:0x…` 進 `/onboarding`，真正的
 * 「選定 leader＋伺服器簽文」在 onboarding step 4（`StepConfirm`，Task 10）
 * 完成，沿用既有 `runLeaderSelectFlow`／`postLeaderSelect`，結構未改（見
 * `lib/copy.ts` 的 `advanced` 檔頭）。
 *
 * 保留的既有功能（`lib/customLeader.ts` 不動）：本地格式驗證 → 後端准入預覽
 * （格式／自跟／operator kill-switch）→ 專屬風險聲明 checkbox → 送出（本頁改為
 * 導向 onboarding，取代舊版的確認框與簽章）。
 *
 * 新增門檻（NOTE 05）：頁首顯著的無背書聲明＋checkbox，勾選前地址輸入框
 * disabled。checkbox 與聲明放在頁面層（不塞進 CustomLeaderSection），未登入
 * 時也看得到——這是頁面對「進階模式沒有平台背書」的宣示，不只是輸入框的
 * 前置條件。
 *
 * 未登入：顯示說明＋登入 CTA（重用 `/strategies/[slug]` 的 connect+SIWE 模式），
 * **不 redirect**——本頁是進階用戶的直達入口，不強迫先繞去 /strategies。
 * 登入成功後只 invalidate `["me"]`（不 `router.push`）：使用者應留在本頁繼續
 * 輸入位址，而不是被導去別處重新找路。
 *
 * `?leader=<address>` 預填（M3 round3 Task 4，D9）：`/explore` 表格「跟單 →」
 * 帶地址參數導來這裡，僅**預填**輸入框——本地格式驗證／後端准入預覽／專屬風險
 * 聲明勾選全部照舊（不因為來源是 explore 就跳過任何一道閘門）。未登入時看不到
 * 輸入框（沿既有 gate），登入後才會用這個值初始化 `CustomLeaderSection` 的
 * `input` state。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useRef, useState } from "react";
import { useAccount, useConnect, useSignMessage } from "wagmi";
import {
  ApiError,
  getLeaderPreview,
  getLeaders,
  type LeaderEntry,
  type LeaderPreviewResp,
  type LeadersResp,
} from "@/lib/api";
import { runCustomLeaderPreview, validateCustomLeaderInput } from "@/lib/customLeader";
import { fmtAmount, shortAddr } from "@/lib/format";
import { useMe } from "@/lib/hooks";
import { useCopy } from "@/lib/lang";
import { loginWithSiwe } from "@/lib/siwe";

type LoginPhase = "idle" | "connecting" | "signing";

/** `useSearchParams()` 在 build 期 prerender 需要 Suspense 邊界（Next.js
 * missing-suspense-with-csr-bailout，見 `onboarding/page.tsx` 同寫法，
 * R-C／C1：`npm run build` 沒包這層會直接失敗）。頁面本體在 AdvancedInner。
 * fallback 留空：本頁全 client 資料，無首繪內容可給。 */
export default function AdvancedPage() {
  return (
    <Suspense fallback={null}>
      <AdvancedInner />
    </Suspense>
  );
}

function AdvancedInner() {
  const COPY = useCopy();
  const c = COPY.advanced;
  const me = useMe();
  const loggedIn = !!me.data;
  const queryClient = useQueryClient();
  const router = useRouter();
  const searchParams = useSearchParams();
  // ⭐ D9：只讀一次當初始值——`useState` 的初始 initializer 只在首次 render 求值，
  // 之後即使網址列的 query 變了也不會覆寫使用者正在輸入的內容，符合「預填」語意
  // （不是「持續同步」）。
  const leaderParam = searchParams.get("leader") ?? undefined;

  const { address, chainId, isConnected } = useAccount();
  const { connectAsync, connectors } = useConnect();
  const { signMessageAsync } = useSignMessage();
  const [loginPhase, setLoginPhase] = useState<LoginPhase>("idle");
  const [loginError, setLoginError] = useState<string | null>(null);

  // ⭐ NOTE 05 的閘門：勾選前，下方（登入後才會渲染的）位址輸入框維持 disabled。
  const [gateAgreed, setGateAgreed] = useState(false);

  // 背景抓目錄，只用於「這個位址是不是平台已知的 leader」的徽章判定（同源比較，
  // 見 CustomLeaderSection 的 listedEntryOf）。
  const leaders = useQuery<LeadersResp>({
    queryKey: ["leaders"],
    queryFn: getLeaders,
    enabled: loggedIn,
  });

  async function handleLogin() {
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
      // 留在本頁：只讓 useMe 重抓，不 router.push（不同於 strategy 詳情頁的登入
      // 即帶參數跳轉——這裡登入完就是為了繼續填本頁的位址輸入框）。
      void queryClient.invalidateQueries({ queryKey: ["me"] });
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

  function handleProceed(selectedAddress: string) {
    router.push(`/onboarding?strategy=advanced:${selectedAddress}`);
  }

  if (me.isLoading) {
    return <main className="page"><p className="hint">{COPY.common.loading}</p></main>;
  }

  return (
    <main className="page">
      <p className="eyebrow">{c.eyebrow}</p>
      <h1>{c.title}</h1>
      <p className="hint lead">{c.subtitle}</p>

      {!loggedIn ? (
        // ⭐ M3 round3 Task 8（R2·P1）：未登入原本是「風險確認」與「登入」兩個
        // 藍框堆疊、下半頁全空——合併為單一卡：風險確認 + 可見但 disabled 的
        // 地址輸入框（讓用戶看得到接下來要填什麼）+ 登入按鈕 + 「或先看精選
        // 策略 →」出口。
        <NotLoggedInGateCard
          gateAgreed={gateAgreed}
          onGateChange={setGateAgreed}
          phase={loginPhase}
          error={loginError}
          onLogin={() => void handleLogin()}
        />
      ) : (
        <>
          <div className="panel ops-notice advanced-gate" role="note">
            <p className="ops-notice-title">{c.gate.title}</p>
            <p className="hint">{c.gate.body}</p>
            <label className="check-row">
              <input
                type="checkbox"
                checked={gateAgreed}
                onChange={(e) => setGateAgreed(e.target.checked)}
              />
              <span>{c.gate.checkboxLabel}</span>
            </label>
          </div>
          <CustomLeaderSection
            gateAgreed={gateAgreed}
            listedLeaders={leaders.data?.leaders}
            onProceed={handleProceed}
            initialAddress={leaderParam}
          />
          {/* ⭐ 沿舊版 /leaders 原樣保留：與 wizard 開通頁的同義句各自成立、互不
              取代（copy.test.ts 語言紅線測試釘住這條）。 */}
          <p className="hint">{c.fundsWarning}</p>
        </>
      )}

      <p className="hint">{COPY.common.nonCustodial}</p>
    </main>
  );
}

/**
 * 未登入時的合併卡（Task 8，取代舊版兩個堆疊的藍框：`advanced-gate` 通知框 +
 * `advanced-login` 登入框）。地址輸入框在此**永遠 disabled**（未登入無論如何
 * 都無法送出）——只是「可見」，讓用戶知道登入後接下來要填什麼；真正可輸入的
 * 那個輸入框在登入後由 `CustomLeaderSection` 接手（不同 DOM 元素，id 不同）。
 */
function NotLoggedInGateCard({ gateAgreed, onGateChange, phase, error, onLogin }: {
  gateAgreed: boolean;
  onGateChange: (v: boolean) => void;
  phase: LoginPhase;
  error: string | null;
  onLogin: () => void;
}) {
  const COPY = useCopy();
  const c = COPY.advanced;
  const nc = c.notLoggedIn;
  return (
    <div className="panel ops-notice advanced-gate" role="note">
      <p className="ops-notice-title">{c.gate.title}</p>
      <p className="hint">{c.gate.body}</p>
      <label className="check-row">
        <input
          type="checkbox"
          checked={gateAgreed}
          onChange={(e) => onGateChange(e.target.checked)}
        />
        <span>{c.gate.checkboxLabel}</span>
      </label>

      <div className="dash-divider" />

      <label className="addr-field" htmlFor="advanced-preview-address">
        <span className="addr-field-label">{c.custom.inputLabel}</span>
        <input
          id="advanced-preview-address"
          className="addr-input mono"
          type="text"
          value=""
          disabled
          readOnly
          placeholder={c.custom.inputPlaceholder}
        />
      </label>
      <p className="hint addr-field-hint">{c.custom.inputHint}</p>

      <div className="dash-divider" />

      <p className="panel-title">{nc.title}</p>
      <p className="hint">{nc.body}</p>
      <button type="button" className="btn btn-primary" disabled={phase !== "idle"}
        onClick={onLogin}>
        {phase === "connecting" ? nc.connecting : phase === "signing" ? nc.signing : nc.cta}
      </button>
      {error && <div className="sign-error" role="alert"><p>{error}</p></div>}

      <p className="hint">
        <Link href="/strategies">{nc.exploreExit}</Link>
      </p>
    </div>
  );
}

/** HL 官方 leaderboard（外部研究入口；沿舊版 hybrid UI 決策）。 */
const HL_LEADERBOARD_URL = "https://app.hyperliquid.xyz/leaderboard";

/**
 * 位址 dock 的預覽狀態機（沿舊版 `/leaders` 的 CustomPhase 慣例）。
 * `rejected`＝准入分類碼對應的拒絕（semantic，使用者改輸入）；
 * `failed`＝transport／未知錯誤（可重按查詢；查詢唯讀，重按零成本）。
 */
type CustomPhase =
  | { t: "idle" }
  | { t: "checking" }
  | { t: "previewed"; preview: LeaderPreviewResp }
  | { t: "rejected"; message: string; detail?: string }
  | { t: "failed"; message: string; detail?: string };

/**
 * ⭐ 這個預覽位址在**背景抓到的那份目錄**裡的條目（找不到 → undefined）。
 * 同源比較（工程原則 1）：抓不到目錄（載入中／失敗）時一律當作「無法核對」。
 */
function listedEntryOf(
  address: string, leaders: LeaderEntry[] | undefined,
): LeaderEntry | undefined {
  const target = address.toLowerCase();
  return leaders?.find((l) => l.address.toLowerCase() === target);
}

/**
 * ⭐ 地址 dock：本頁唯一的入口。閘門順序（每一道未過就零後續動作）：
 * 0. 頁面層的無背書聲明 checkbox（`gateAgreed`，NOTE 05）→ 未勾選整個輸入框
 *    disabled，連查詢都按不下去；
 * 1. 本地格式驗證（viem isAddress，strict:false）→ 不合法連「查詢」都按不下去；
 * 2. 後端准入預覽（格式／自跟／operator kill-switch）→ 被拒按 reason 分類碼
 *    顯示對應文案，**不對人話字串比對**；
 * 3. 專屬風險聲明 checkbox（純前端閘門，仿 wizard AML attestation）→ 未勾不得送出；
 * 4. 送出＝`onProceed(address)` → 頁面層 `router.push` 到
 *    `/onboarding?strategy=advanced:{address}`（Task 11 與舊版最大差異：不在
 *    本頁簽章）。
 */
function CustomLeaderSection({ gateAgreed, listedLeaders, onProceed, initialAddress }: {
  gateAgreed: boolean;
  listedLeaders: LeaderEntry[] | undefined;
  onProceed: (address: string) => void;
  /** `?leader=` 預填（D9），見本檔檔頭。 */
  initialAddress?: string;
}) {
  const COPY = useCopy();
  const c = COPY.advanced;
  const cc = c.custom;
  const [input, setInput] = useState(initialAddress ?? "");
  const [agreed, setAgreed] = useState(false);
  const [cPhase, setCPhase] = useState<CustomPhase>({ t: "idle" });
  // in-flight 防護：查詢途中輸入變了，回來的結果屬於舊位址，一律丟棄。
  const seq = useRef(0);

  const check = validateCustomLeaderInput(input);
  const showFormatError = !check.ok && !check.empty;
  const listed = cPhase.t === "previewed"
    ? listedEntryOf(cPhase.preview.address, listedLeaders)
    : undefined;

  function onInputChange(v: string) {
    setInput(v);
    seq.current += 1;
    setAgreed(false);
    setCPhase({ t: "idle" });
  }

  async function runPreview() {
    if (!gateAgreed || !check.ok) return; // 按鈕已 disabled，這裡是第二道防禦
    const mySeq = ++seq.current;
    setCPhase({ t: "checking" });
    const r = await runCustomLeaderPreview({ fetchPreview: getLeaderPreview }, input);
    if (seq.current !== mySeq) return; // 結果屬於舊輸入
    if (r.ok) {
      setAgreed(false); // 每一份新預覽都要重新勾選（聲明綁的是「這個位址」）
      setCPhase({ t: "previewed", preview: r.preview });
    } else if (r.kind === "rejected") {
      setCPhase({ t: "rejected", message: cc.reasons[r.reason], detail: r.detail });
    } else if (r.kind === "address-mismatch") {
      setCPhase({ t: "failed", message: cc.echoMismatch });
    } else {
      const detail = r.error instanceof ApiError ? r.error.detail : undefined;
      setCPhase({ t: "failed", message: cc.previewFailed, detail });
    }
  }

  return (
    <section className="panel leader-dock">
      <p className="leader-custom-title">{cc.title}</p>
      <p className="hint">{cc.subtitle}</p>
      <p className="hint">
        {cc.leaderboardLabel}
        {/* noopener：外部站不得拿到 window.opener（noreferrer 順帶擋 referrer） */}
        <a href={HL_LEADERBOARD_URL} target="_blank" rel="noopener noreferrer">
          {cc.leaderboardLinkText}
        </a>
      </p>

      <div className="leader-dock-row">
        <label className="addr-field" htmlFor="custom-leader-address">
          <span className="addr-field-label">{cc.inputLabel}</span>
          <input
            id="custom-leader-address"
            className="addr-input mono"
            type="text"
            autoComplete="off"
            spellCheck={false}
            placeholder={cc.inputPlaceholder}
            value={input}
            disabled={!gateAgreed}
            // 原樣存 state；小寫正規化只發生在送後端前（lib/customLeader.ts）。
            onChange={(e) => onInputChange(e.target.value)}
          />
        </label>
        <button
          type="button"
          className="btn btn-primary leader-dock-check"
          disabled={!gateAgreed || !check.ok || cPhase.t === "checking"}
          onClick={() => void runPreview()}
        >
          {cPhase.t === "checking" ? cc.checking : cc.check}
        </button>
      </div>
      <p className="hint addr-field-hint">{cc.inputHint}</p>
      {showFormatError && (
        <p className="addr-input-error" role="alert">{cc.formatError}</p>
      )}
      {cPhase.t === "checking" && <p className="hint" role="status">{cc.checking}</p>}

      {(cPhase.t === "rejected" || cPhase.t === "failed") && (
        <div className="ops-alert" role="alert">
          <p className="ops-alert-body">{cPhase.message}</p>
          {cPhase.detail && <p className="hint mono">{cPhase.detail}</p>}
        </div>
      )}

      {cPhase.t === "previewed" && (
        <div className="leader-custom-preview">
          <p className="leader-custom-title">{cc.previewTitle}</p>
          {/* ⭐ 背景目錄查得到就沿用它的策展名：同一個位址在同一頁只能有一個名字。 */}
          {listed !== undefined && <p className="leader-name">{listed.name}</p>}
          <p className="hint mono leader-addr" title={cPhase.preview.address}>
            {shortAddr(cPhase.preview.address)}
          </p>
          {/* ⭐ 鏈上無活動（exists=false）→ 警示但**不擋**：leader 可能尚未進場，
              客戶可先完成配置，進場後引擎自動開始跟。 */}
          {!cPhase.preview.exists && (
            <div className="ops-alert" role="alert">
              <p className="ops-alert-body">{cc.noActivityWarning}</p>
            </div>
          )}
          {/* ⭐ accepting_new=false（例行下架）→ 警示但**不擋**。 */}
          {!cPhase.preview.accepting_new && (
            <div className="ops-alert" role="alert">
              <p className="ops-alert-body">{cc.notAcceptingNewWarning}</p>
            </div>
          )}
          {cPhase.preview.already_listed && (
            <p className="leader-custom-listed">
              <span className="leader-badge">{cc.alreadyListedBadge}</span>
            </p>
          )}
          {/* ⭐ vault 位址：資訊性標示，不是警告。 */}
          {cPhase.preview.kind === "vault" && (
            <>
              <p className="leader-custom-listed">
                <span className="leader-badge">{cc.vaultBadge}</span>
              </p>
              <p className="hint">{cc.vaultNote}</p>
            </>
          )}
          {cPhase.preview.vault_checks && !cPhase.preview.vault_checks.passed && (
            <div className="ops-alert" role="alert">
              {cPhase.preview.vault_checks.failures.map((f) => (
                <p key={f.name} className="ops-alert-body">
                  <span className="mono">{f.name}</span>：{f.detail}
                </p>
              ))}
              <p className="ops-alert-body">{cc.vaultCheckWarning}</p>
            </div>
          )}
          <p className="hint">{cc.previewNote}</p>
          <dl className="leader-stats">
            <LeaderStat label={cc.previewAccountValue} hint={cc.previewAccountValueHint}
              value={fmtAmount(cPhase.preview.account_value)}
              raw={cPhase.preview.account_value} />
            <LeaderStat label={cc.previewPositionCount} hint={cc.previewPositionCountHint}
              value={String(cPhase.preview.position_count)} />
          </dl>

          {cPhase.preview.already_listed && (
            <p className="hint">
              {listed !== undefined ? cc.alreadyListedNote : cc.alreadyListedNoteNotShown}
            </p>
          )}

          {/* ⭐ 專屬風險聲明：純前端閘門（仿 StepRisk），未勾選送出按鈕不開。 */}
          <label className="check-row">
            <input
              type="checkbox"
              checked={agreed}
              onChange={(e) => setAgreed(e.target.checked)}
            />
            <span>{cc.attestation}</span>
          </label>

          <div className="leader-action">
            <p className="leader-upper-bound">{c.upperBound}</p>
            <button
              type="button"
              className="btn btn-primary btn-block"
              disabled={!agreed}
              onClick={() => onProceed(cPhase.preview.address)}
            >
              {cc.select}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

function LeaderStat({ label, hint, value, raw }: {
  label: string; hint: string; value: string; raw?: string | null;
}) {
  return (
    <div className="leader-stat">
      <dt title={hint}>{label}</dt>
      <dd className="mono" title={raw ?? undefined}>{value}</dd>
    </div>
  );
}
