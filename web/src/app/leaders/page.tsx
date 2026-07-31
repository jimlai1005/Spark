"use client";
/**
 * /leaders — 跟單頁（本站的招牌互動：地址 dock）。
 *
 * ⭐⭐ 2026-07-30 大幅減法：付費閘門與精選 leader 目錄卡片（含完整績效揭露）全部
 * 下架，舊版負責績效判定的 lib 模組一併刪除。整個產品收斂成單一動作——貼上任一
 * Hyperliquid 錢包位址，通過准入檢查即可跟單。`getLeaders` 目錄仍在背景呼叫，
 * 但只用來判斷貼上的位址是不是平台已知的 leader（顯示徽章），不再渲染成卡片
 * 格線；規模／曝險／績效欄位隨之一併下架。
 *
 * 保留且**邏輯不變**的部分：
 * - CurrentLeaderPanel（`/api/me/leader`）：換 leader 這個決定的左半邊，安靜地
 *   擺在 dock 下方——本頁只有一個視覺重拳（地址輸入框），其餘一律安靜。
 * - 位址輸入區的准入預覽流程（`lib/customLeader.ts` 不動）：本地格式驗證 →
 *   後端准入預覽 → 專屬風險聲明 checkbox → 既有確認框與簽章流程。
 * - `runFlow` / `runLeaderSelectFlow` 簽章管線與 `ConfirmDialog`：授權安全性不因
 *   唯一入口而打折——伺服器原文原樣進錢包，本地 recover 比對登入地址，
 *   `expectedLeader` 錨定使用者實際選的那一位。
 *
 * 授權流程沿 approvalFlow 的謹慎度（見 lib/leaderSelectFlow.ts）：伺服器產生原文 →
 * 錢包 personal_sign → **本地 recover 比對登入地址**（不符零網路請求）→ 原文原樣回送。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useSignMessage } from "wagmi";
import {
  ApiError,
  getLeaderPreview,
  getLeaderSelectMessage,
  getLeaders,
  getMyLeader,
  getMyRisk,
  getRiskSettingsMessage,
  getRiskUnlockMessage,
  postLeaderSelect,
  postMyRisk,
  postRiskUnlock,
  type LeaderEntry,
  type LeaderPreviewResp,
  type LeaderSelectResp,
  type LeadersResp,
  type MyLeaderPendingChange,
  type MyLeaderResp,
  type MyRiskResp,
  type RiskParamName,
  type RiskParamSpec,
  type RiskPrefs,
} from "@/lib/api";
import { runCustomLeaderPreview, validateCustomLeaderInput } from "@/lib/customLeader";
import Link from "next/link";
import { ActivationNotice } from "@/components/ActivationNotice";
import { COPY } from "@/lib/copy";
import { fmtAmount, shortAddr } from "@/lib/format";
import { useMe } from "@/lib/hooks";
import { runLeaderSelectFlow, type LeaderSelectFlowResult } from "@/lib/leaderSelectFlow";
import {
  runRiskSettingsFlow,
  runRiskUnlockFlow,
  type RiskFlowFailure,
} from "@/lib/riskSettingsFlow";
import { recoverPersonalSigner } from "@/lib/sign";

const c = COPY.leaders;

type Phase =
  | { t: "idle" }
  | { t: "confirming"; leader: LeaderEntry }
  | { t: "running"; leader: LeaderEntry }
  | { t: "done"; resp: LeaderSelectResp }
  /** `dismissOnly`：此錯誤要求使用者停手回報，不得提供「重新操作」按鈕。 */
  | { t: "error"; message: string; detail?: string; dismissOnly?: boolean };

/** 未登入守門：與 /onboarding 的同款呈現（提示＋回登入頁按鈕），不是一行裸文字。 */
function NotLoggedInGuard() {
  return (
    <main className="page">
      <div className="narrow">
        <p>{COPY.common.notLoggedIn}</p>
        <Link className="btn btn-primary" href="/">{COPY.common.backToLogin}</Link>
      </div>
    </main>
  );
}

export default function LeadersPage() {
  const me = useMe();
  const { signMessageAsync } = useSignMessage();
  const queryClient = useQueryClient();
  const [phase, setPhase] = useState<Phase>({ t: "idle" });

  const loggedIn = !!me.data;
  // ⭐ 背景抓目錄，只用於「這個位址是不是平台已知的 leader」的徽章判定（見
  // CustomLeaderSection 的 listedLeaders／alreadyListedBadge），不再渲染成卡片格線。
  const leaders = useQuery<LeadersResp>({
    queryKey: ["leaders"],
    queryFn: getLeaders,
    enabled: loggedIn,
  });

  async function runFlow(leader: LeaderEntry) {
    if (!me.data) return;
    setPhase({ t: "running", leader });
    const r = await runLeaderSelectFlow(
      {
        fetchMessage: () => getLeaderSelectMessage(leader.address),
        // 原文原樣進錢包：不加工、不重組（後端會用自己重建的版本驗簽）。
        signMessage: (message) => signMessageAsync({ message }),
        recover: recoverPersonalSigner,
        submit: postLeaderSelect,
      },
      // ⭐ expectedLeader＝使用者實際點的那一位。沒有這個，被打穿的 API 可以回一份
      // 指向別人的原文，而流程的其他每一關（recover、後端重建驗簽）都會照樣放行。
      { expectedSigner: me.data.address, expectedLeader: leader.address },
    );
    if (r.ok) {
      setPhase({ t: "done", resp: r.resp });
      // ⭐ 授權成功 → 重抓「目前跟隨」：後端此刻已有一筆 pending_change，而本頁上方
      // 若還顯示「沒有待生效的變更」，同一頁就同時說了兩件互相矛盾的事。
      void queryClient.invalidateQueries({ queryKey: ["my-leader"] });
    } else setPhase({ t: "error", ...errorCopy(r) });
  }

  if (me.isLoading) {
    return <main className="page"><p className="hint">{COPY.common.loading}</p></main>;
  }
  if (!loggedIn) {
    return <NotLoggedInGuard />;
  }
  if (leaders.error instanceof ApiError && leaders.error.kind === "auth") {
    return <NotLoggedInGuard />;
  }

  return (
    <main className="page">
      <p className="eyebrow">{c.eyebrow}</p>
      <h1>{c.title}</h1>
      <p className="hint lead">{c.subtitle}</p>

      {/* 未活化就先說——否則使用者會一路查到最後才撞上「查無 follower」。 */}
      <ActivationNotice />

      {phase.t === "done" && <DoneNotice resp={phase.resp} />}
      {phase.t === "error" && (
        <div className="ops-alert" role="alert">
          <p className="ops-alert-body">{phase.message}</p>
          {phase.detail && <p className="hint mono">{phase.detail}</p>}
          <button type="button" className="btn btn-ghost"
            onClick={() => setPhase({ t: "idle" })}>
            {phase.dismissOnly ? c.errors.dismiss : c.errors.retry}
          </button>
        </div>
      )}

      {/* ⭐ 招牌互動：地址 dock。除了這裡，其餘一律安靜、留白紀律。 */}
      <CustomLeaderSection
        busy={phase.t === "running"}
        listedLeaders={leaders.data?.leaders}
        onSelect={(leader) => setPhase({ t: "confirming", leader })}
      />

      {phase.t === "confirming" && (
        <ConfirmDialog
          leader={phase.leader}
          onCancel={() => setPhase({ t: "idle" })}
          onConfirm={() => runFlow(phase.leader)}
        />
      )}
      {phase.t === "running" && <p className="hint" role="status">{c.signing}</p>}

      {/* ⭐ 風控 opt-in：擺在 dock 下方（選 leader 的同一個決定裡的另一半）。
          預設不啟用；勾選後才展開細項。與換 leader 的簽章流程刻意**分開**送出——
          leader 授權的待簽原文是逐位元組驗證的，把風控欄位塞進那份訊息會動到
          canonical 格式，風險遠高於這個功能的價值。 */}
      <RiskControlsSection />


      {/* ⭐ 換 leader 這個決定的左半邊：我現在跟的是誰（spec User Story 12）。安靜地
          擺在 dock 下方。 */}
      <CurrentLeaderPanel />

      <p className="hint">{c.fundsWarning}</p>
      <p className="hint">{COPY.common.nonCustodial}</p>
    </main>
  );
}

/**
 * 失敗文案。⭐ 每一條都必須讓使用者知道**現在的狀態**——「沒有被變更」要講出來：
 * 換 leader 有真實成本，使用者最需要知道的是「我剛才那下到底有沒有發生」。
 * 後端 detail 原樣附在第二行（供回報問題用），不做字串比對推測原因。
 */
function errorCopy(r: Extract<LeaderSelectFlowResult, { ok: false }>):
  { message: string; detail?: string; dismissOnly?: boolean } {
  const e = c.errors;
  if (r.kind === "wallet-rejected") return { message: e.walletRejected };
  if (r.kind === "signer-mismatch") return { message: e.signerMismatch };
  // ⭐ 伺服器指定的授權對象 ≠ 使用者所選：唯一一種「請停手並回報」的失敗，
  // 因此連按鈕都不給「重新操作」（見 copy.ts 的 leaderMismatch）。
  if (r.kind === "leader-mismatch") return { message: e.leaderMismatch, dismissOnly: true };
  const err = r.error;
  const detail = err instanceof ApiError ? err.detail : undefined;
  if (err instanceof ApiError) {
    if (err.kind === "auth") return { message: COPY.common.notLoggedIn, detail };
    if (err.status === 403) return { message: e.forbidden, detail };
    // 429 有專屬文案：兩個端點共用同一個查詢額度，所以「查很多位址之後按送出」
    // 也會撞到。通用的 submitFailed 會說「上一筆簽名已作廢」——這裡根本還沒到
    // 驗簽，那句話是錯的，且會讓人以為出了更嚴重的事。
    if (err.status === 429) return { message: e.rateLimited, detail };
    // 待簽原文端點的 400 只有一種語意：這位 leader 不可選（見 app.py）。
    if (err.status === 400 && r.kind === "message-failed") {
      return { message: e.notSelectable, detail };
    }
  }
  return {
    message: r.kind === "message-failed" ? e.messageFailed : e.submitFailed,
    detail,
  };
}

/** 成功。⭐ 生效時機與後果一律顯示**後端原文**（單一來源，不在前端另寫一份）。 */
function DoneNotice({ resp }: { resp: LeaderSelectResp }) {
  return (
    <div className="panel leader-done" role="status">
      <p className="leader-done-title">{c.doneTitle}</p>
      <p className="hint">{resp.effective_note}</p>
      <p className="hint">{resp.consequences}</p>
      <p className="hint mono" title={resp.leader_address}>
        {c.confirmLeaderLabel}: {shortAddr(resp.leader_address)}
      </p>
    </div>
  );
}

/**
 * ⭐ 「你目前跟隨的 leader」（`/api/me/leader`；spec User Story 12）。
 *
 * 為什麼這一區存在：本頁要客戶簽署一份**換掉**現有 leader 的授權，而「現在跟的是誰」
 * 是這個決定的左半邊。沒有它，客戶是在不知道現況的情況下被要求簽署變更——尤其自訂
 * 位址的路徑，選完之後畫面上沒有任何地方看得到自己現在跟誰。
 *
 * ⚠️ 這一區的失敗方向**不對稱**，三種狀態因此各有各的畫法：
 * - 查詢失敗（503／網路）→ 說「暫時讀不到」，**絕不**畫成「你沒在跟單」：後者是一句
 *   我們沒有根據的斷言，會讓一個正在跟單的客戶以為資金沒在動而不去看它（工程原則 3：
 *   讀不到 ≠ 沒有）。且本區失敗**不得影響頁面其餘部分**——所以它是獨立的元件與
 *   獨立的 query，dock 與簽章流程都不經過它。
 * - `engine_default`（已活化、未指定 leader）與 `not_activated`（尚未活化）刻意不合併：
 *   前者**正在跟單**，只是對象由部署決定，兩者的處置完全不同（後端也是分兩態回傳）。
 * - 狀態的**內文**一律用後端 `note` 原文（單一來源）；本頁只提供標題與欄位標籤，
 *   不在前端另寫一份會過期的說明——這一區的前一版就是這樣過期的。
 */
function CurrentLeaderPanel() {
  // 本元件只在登入分支被渲染（頁面上方的 guard 已擋掉未登入），故不另掛 enabled。
  const q = useQuery<MyLeaderResp>({ queryKey: ["my-leader"], queryFn: getMyLeader });
  const cur = c.current;

  if (q.isLoading) {
    return (
      <div className="panel ops-notice leader-current">
        <p className="ops-notice-title">{cur.title}</p>
        <p className="hint" role="status">{cur.loading}</p>
      </div>
    );
  }
  // ⭐ 讀不到就說讀不到（含 data 缺席的保底）——不退化成任何一種「你沒有 leader」。
  if (q.error || !q.data) {
    return (
      <div className="panel ops-notice leader-current">
        <p className="ops-notice-title">{cur.failedTitle}</p>
        <p className="hint">{cur.failedNote}</p>
      </div>
    );
  }

  const d = q.data;
  const none = d.leader_address === null ? noneTitleOf(d.status) : null;
  return (
    <div className="panel ops-notice leader-current">
      <p className="ops-notice-title">{cur.title}</p>
      {d.leader_address !== null ? (
        // 名稱只是輔助：查無名稱（自訂 leader、或已下架的精選 leader）時位址照樣顯示
        // ——「沒有名稱」不等於「沒有 leader」。
        <p className="mono leader-current-addr" title={d.leader_address}>
          {cur.leaderLabel}: {d.leader_name === null
            ? shortAddr(d.leader_address)
            : `${d.leader_name}（${shortAddr(d.leader_address)}）`}
        </p>
      ) : (
        <p className="leader-current-none">{none?.title}</p>
      )}
      {/* 後端沒見過的狀態碼原樣顯示（看得懂但陌生），不靜默落進某個既有分支。 */}
      {none?.unknown === true && (
        <p className="hint mono">{cur.statusLabel}: {d.status}</p>
      )}
      <p className="hint">{d.note}</p>
      {d.pending_change !== null && <PendingLeaderChange change={d.pending_change} />}
    </div>
  );
}

/** 沒有 leader 位址時的標題；`unknown`＝後端回了預期外的狀態碼（要把它顯示出來）。 */
function noneTitleOf(status: string): { title: string; unknown: boolean } {
  const titles: Record<string, string | undefined> = c.current.noneTitles;
  const title = titles[status];
  return title === undefined
    ? { title: c.current.noneTitleFallback, unknown: true }
    : { title, unknown: false };
}

/**
 * 已簽署但尚未生效的換 leader。⭐ 與「目前跟隨」畫在同一區但**分開的區塊**：
 * 兩者是不同時點的事實（現在跟的 vs 下一個 cycle 會換到的），混在一起會讓客戶以為
 * 換已經發生了——而換 leader 是有真實成交成本的動作。生效說明用後端原文。
 */
function PendingLeaderChange({ change }: { change: MyLeaderPendingChange }) {
  const cur = c.current;
  return (
    <div className="leader-perf-state leader-current-pending">
      <p className="leader-perf-state-title">{cur.pendingTitle}</p>
      <p className="hint mono" title={change.leader_address}>
        {cur.pendingLabel}: {shortAddr(change.leader_address)}
      </p>
      {change.issued_at !== null && (
        <p className="hint mono">{cur.pendingIssuedAtLabel}: {change.issued_at}</p>
      )}
      <p className="hint">{change.note}</p>
    </div>
  );
}

/** HL 官方 leaderboard（外部研究入口；spec 的 hybrid UI 決策）。 */
const HL_LEADERBOARD_URL = "https://app.hyperliquid.xyz/leaderboard";

/**
 * 位址 dock 的預覽狀態機（仿本頁 Phase 的 useState 慣例）。
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
 *
 * 為什麼要判定「是不是平台已知的 leader」：後端對 **paused**（`accepting_new=false`）
 * 的精選 leader 也回 `already_listed=true`，而目錄端點用 `is_selectable` 過濾、
 * **不含** paused leader——本頁已不渲染任何目錄格線，因此一律以**背景抓到的那份
 * 清單**（同源比較，工程原則 1）判定能不能沿用策展名稱，抓不到（載入中／失敗）時
 * 一律當作「無法核對」，不宣稱已經確認過。兩側都 lowercase：後端兩支端點都回小寫，
 * 這行是防未來某一側改了大小寫慣例。
 */
function listedEntryOf(
  address: string, leaders: LeaderEntry[] | undefined,
): LeaderEntry | undefined {
  const target = address.toLowerCase();
  return leaders?.find((l) => l.address.toLowerCase() === target);
}

/**
 * 預覽 → 既有選擇流程用的 LeaderEntry。⭐ 位址取伺服器回聲（已通過回聲比對）。
 *
 * 名稱三段式：背景目錄查得到 → 用它的策展名（同一個位址在同一頁只有一個名字）；
 * 已在平台名單但目錄查不到（paused 或載入失敗）→ 位址縮寫；真正的自訂位址 →
 * 「自訂 leader」。⚠️ 把一個有名字的已知 leader 在確認框顯示成「自訂 leader」，
 * 等於在使用者授權的最後一步把對象講得比實際更陌生。
 */
function customEntryOf(preview: LeaderPreviewResp, listed: LeaderEntry | undefined): LeaderEntry {
  const name = listed !== undefined
    ? listed.name
    : preview.already_listed ? shortAddr(preview.address) : c.custom.entryName;
  return {
    address: preview.address,
    name,
    description: "",
    account_value: preview.account_value,
    total_ntl_pos: null,
    unrealized_pnl: null,
    position_count: preview.position_count,
  };
}

/**
 * ⭐ 地址 dock：本頁唯一的跟單入口（2026-07-27 spec 的准入預覽流程，2026-07-30
 * 拿掉精選卡片後仍原樣適用）。
 *
 * 閘門順序（每一道未過就零後續動作）：
 * 1. 本地格式驗證（viem isAddress，strict:false——與後端准入同一把尺，見
 *    lib/customLeader.ts 檔頭）→ 不合法連「查詢」都按不下去；
 * 2. 後端准入預覽（格式／自跟／operator kill-switch）→ 被拒按 reason 分類碼
 *    顯示對應文案，**不對人話字串比對**；
 * 3. 專屬風險聲明 checkbox（純前端閘門，仿 wizard AML attestation）→ 未勾不得送出；
 * 4. 之後進**既有**確認框與簽章流程（ConfirmDialog → runLeaderSelectFlow）。
 *
 * 誠實揭露：預覽卡明說是「查詢當下的鏈上切面」（confirm 沒貼錯位址用）。
 *
 * 輸入一變即整組重置（預覽、錯誤、勾選）＋ in-flight 序號防護：舊位址的預覽卡
 * 留在新位址旁邊，是本區塊最會騙過本人的顯示錯誤（工程原則 1：畫面上的資料
 * 必須與輸入框裡的位址同源）。
 */
function CustomLeaderSection({ busy, listedLeaders, onSelect }: {
  busy: boolean;
  /**
   * 背景抓到的目錄（載入中／載入失敗 → undefined）。只用於「這個位址是不是平台
   * 已知的 leader」的判定與名稱沿用；本區塊的其餘行為完全不依賴它。undefined 時
   * 一律當作「無法核對」。
   */
  listedLeaders: LeaderEntry[] | undefined;
  onSelect: (leader: LeaderEntry) => void;
}) {
  const cc = c.custom;
  const [input, setInput] = useState("");
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
    if (!check.ok) return; // 按鈕已 disabled，這裡是第二道（沿 runFlow 的防禦慣例）
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
            // 原樣存 state（輸入框裡的字被程式改掉，最容易騙過本人）；
            // 小寫正規化只發生在送後端前（lib/customLeader.ts）。
            onChange={(e) => onInputChange(e.target.value)}
          />
        </label>
        <button
          type="button"
          className="btn btn-primary leader-dock-check"
          disabled={!check.ok || cPhase.t === "checking" || busy}
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
          {/* ⭐ 鏈上無活動（exists=false）→ 警示但**不擋**（2026-07-27 裁決）：leader
              尚未進場時客戶可先完成配置，進場後引擎自動開始跟。不是錯誤、不停送出。 */}
          {!cPhase.preview.exists && (
            <div className="ops-alert" role="alert">
              <p className="ops-alert-body">{cc.noActivityWarning}</p>
            </div>
          )}
          {/* ⭐ accepting_new=false（例行下架）→ 警示但**不擋**（2026-07-27 拆旗標）：
              只擋新客的行銷狀態，引擎仍照跟；客戶堅持要跟就放行（使用者裁決）。
              enabled=false 的安全撤銷早在准入層硬擋（leader_disabled），到不了這裡。 */}
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
          {/* ⭐ vault 位址（2026-07-31 契約）：**資訊性標示，不是警告**——badge＋中性
              說明，不新增任何步驟（checkbox／送出照舊）。舊後端不回 kind → 當 standard。 */}
          {cPhase.preview.kind === "vault" && (
            <>
              <p className="leader-custom-listed">
                <span className="leader-badge">{cc.vaultBadge}</span>
              </p>
              <p className="hint">{cc.vaultNote}</p>
            </>
          )}
          {/* advisory 檢查有 FAIL → 警語列出每項 name＋detail（advisory：只告知，
              不擋送出——與 exists／accepting_new 兩個警示同一「放行帶警示」原則）。 */}
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

          {/* 「已在平台名單」的兩個子情形分開講：能不能核對取決於背景目錄有沒有它。 */}
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

          {/* 上界警語與送出按鈕同容器（誠信要求 5）。 */}
          <div className="leader-action">
            <p className="leader-upper-bound">{c.upperBound}</p>
            <button
              type="button"
              className="btn btn-primary btn-block"
              disabled={!agreed || busy}
              onClick={() => onSelect(customEntryOf(cPhase.preview, listed))}
            >
              {cc.select}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

/**
 * 風控區塊（2026-07-30 改版：slider ＋ 簽章送出 ＋ 自助解除熔斷）。
 *
 * ⭐ 五個刻意的設計決定：
 * 1. **上下界、預設值、建議值全部來自後端 `specs`**——前端一個門檻數字都不寫死。
 *    引擎的合法區間在 `filet/risk_prefs.py`；前端另存一份的下場是畫面允許、
 *    引擎拒絕啟動，而症狀出現在啟用那一刻。
 * 2. **每個參數都是 slider ＋ 我方建議值**（使用者裁決）：風控門檻涉及利益衝突
 *    （守的是「客戶資金被交易磨損的速度」，而磨損的受益方是我方），所以設計不是
 *    替客戶決定，而是把建議標出來、讓客戶自己決定。
 * 3. **比例以百分比呈現、以比例送出**：客戶想的是「跌 20% 就停」，而引擎收的是 0.20。
 *    換算集中在 `pctToRatio`／`ratioToPct` 兩個函式，不散落在每個 input 的 onChange。
 *    `unit="hours"` 的參數不換算（小時就是小時），由 spec 的 `unit` 欄決定。
 * 4. **`group="tracking"` 的參數畫在啟用 checkbox 之外**：它不管風控開關都生效，
 *    藏在 checkbox 底下會讓關掉風控的客戶以為自己也關掉了它。
 * 5. **儲存＝簽章授權**（見 lib/riskSettingsFlow.ts）：能改門檻的人就能把一道保護
 *    整個拿掉，所以與換 leader 同一個等級的防線——伺服器原文原樣進錢包、進錢包
 *    之前先驗內容、本地 recover 比對登入地址。
 */
function ratioToPct(v: string): string {
  const n = Number(v);
  return Number.isFinite(n) ? String(Math.round(n * 1000) / 10) : "";
}

/**
 * 百分比 → 比例。⭐ **對齊 0.1% 刻度**（與 `ratioToPct` 的顯示精度、以及後端
 * `risk_prefs._RATIO_STEP` 同一個刻度）：不對齊的話，輸入 33.33 會顯示成 33.3
 * 卻送出 0.3333——畫面與實際套用值不一致，正是本 codebase 其他地方明文拒絕的
 * 「客戶送了 A、系統套用了 B」（審查 F7）。
 */
function pctToRatio(pct: string): string {
  const n = Number(pct);
  if (!Number.isFinite(n)) return pct;
  return String(Number((Math.round(n * 10) / 1000).toFixed(3)));
}

/**
 * 一個 decimal 參數的**顯示基準**：比例（以百分比呈現）或小時。
 * ⭐ 由 spec 的 `unit` 決定，不是由參數名稱猜——後端新增一個帶單位的參數時，
 * 前端不必改任何一行就會用對基準（工程原則 1：換算只有一處）。
 * `step` 是**顯示刻度**（0.1% ／ 1 小時），不是門檻：門檻（min／max）一律取自 spec。
 */
function scaleOf(spec: RiskParamSpec): {
  toDisplay: (raw: string) => string;
  fromDisplay: (shown: string) => string;
  step: string;
  suffix: string;
} {
  return spec.unit === "hours"
    ? { toDisplay: (v) => v, fromDisplay: (v) => v, step: "1", suffix: c.risk.hoursSuffix }
    : {
        toDisplay: ratioToPct, fromDisplay: pctToRatio,
        // 0.1% ＝後端 `risk_prefs._RATIO_STEP` 的同一個刻度（見 pctToRatio 註解）。
        step: "0.1", suffix: c.risk.percentSuffix,
      };
}

/** `RiskPrefs` 的動態存取（spec 驅動的渲染需要用名字取值）。 */
function prefValue(prefs: RiskPrefs, name: RiskParamName): string | boolean {
  return (prefs as unknown as Record<string, string | boolean>)[name];
}

function withPref(prefs: RiskPrefs, name: RiskParamName, v: string | boolean): RiskPrefs {
  return { ...prefs, [name]: v } as RiskPrefs;
}

/**
 * 一個參數的控制項。⭐ decimal ⇒ slider（使用者裁決：門檻由客戶自己決定，我們只
 * 標建議值）；bool ⇒ checkbox。上下界、建議值、標籤、說明**全部**取自 spec。
 */
function RiskParamField({ spec, prefs, onChange }: {
  spec: RiskParamSpec;
  prefs: RiskPrefs;
  onChange: (next: RiskPrefs) => void;
}) {
  const v = prefValue(prefs, spec.name);
  if (spec.type === "bool") {
    return (
      <div className="risk-field">
        <label className="risk-toggle">
          <input type="checkbox" checked={v === true}
            onChange={(e) => onChange(withPref(prefs, spec.name, e.target.checked))} />
          <span>{spec.label}</span>
        </label>
        <p className="hint risk-recommended">
          {c.risk.recommendedLabel}：
          {spec.recommended === true ? c.risk.boolOn : c.risk.boolOff}
        </p>
        <p className="hint">{spec.help}</p>
      </div>
    );
  }
  const s = scaleOf(spec);
  const shown = s.toDisplay(String(v));
  return (
    <div className="risk-field">
      <label htmlFor={`risk-${spec.name}`}>{spec.label}</label>
      <div className="risk-slider-row">
        <input
          id={`risk-${spec.name}`}
          type="range"
          className="risk-slider"
          min={spec.min == null ? undefined : s.toDisplay(spec.min)}
          max={spec.max == null ? undefined : s.toDisplay(spec.max)}
          step={s.step}
          value={shown}
          onChange={(e) => onChange(withPref(prefs, spec.name, s.fromDisplay(e.target.value)))}
        />
        <span className="risk-value mono">{shown}{s.suffix}</span>
      </div>
      <p className="hint risk-recommended">
        {c.risk.recommendedLabel}：{s.toDisplay(String(spec.recommended))}{s.suffix}
      </p>
      <p className="hint">{spec.help}</p>
    </div>
  );
}

/**
 * 「已提交 vs 已生效」。⚠️ 順序刻意把「讀不到」排在最前：`applied` 為 null ＝引擎
 * 心跳過期或缺席＝**我們不知道**，任何一句更具體的話（「尚未生效」「已生效」）
 * 都是我們沒有根據的斷言（工程原則 3：讀不到 ≠ 沒有）。
 * 比對只就 `controls_enabled` 一欄——心跳裡沒有逐項門檻，不得宣稱逐項都已生效。
 */
function appliedNoteOf(d: MyRiskResp): string {
  const a = d.applied;
  if (a === null || a.controls_enabled === null) return c.risk.applied.unknown;
  if (d.submitted.issued_at === null) return c.risk.applied.notSubmitted;
  // ⭐ 逐項比對「引擎實際在執法的門檻」與「我提交的門檻」（2026-07-30 心跳補上
  // `prefs` 之後才做得到）。只比 `controls_enabled` 的話，把回撤上限從 20% 調到
  // 10%（開關兩邊都是「開」）會顯示「已生效」，而引擎其實還在用舊門檻——那正是
  // 最常見的調整情境。心跳沒帶 `prefs`（舊版引擎）⇒ 說「無法確認」，不猜。
  if (a.prefs === null || a.prefs === undefined) return c.risk.applied.unknown;
  const applied = a.prefs;
  const same = (Object.keys(d.prefs) as Array<keyof RiskPrefs>).every(
    (k) => String(applied[k]) === String(d.prefs[k]),
  );
  return same ? c.risk.applied.inSync : c.risk.applied.pending;
}

/**
 * 熔斷區塊。⚠️ 三種畫法對應三種事實，不得互相代用：
 * - `halted === null` ⇒ 讀不到，說「無法確認」。**不得**什麼都不畫：不畫等於畫成
 *   「沒有熔斷」，那會在客戶已經停止跟單的當下告訴他一切照常。
 * - `tripped === false` ⇒ 確定沒有熔斷，整區不畫（這才是「沒事」的合法畫法）。
 * - `tripped === true` ⇒ 醒目區塊 ＋ 自助恢復按鈕，但 `reason === "leader_revoked"`
 *   是我方的治理動作，客戶不得自助解除 → **不給按鈕**，改給說明。
 */
function HaltedNotice({ halted, busy, note, error, onResume }: {
  halted: MyRiskResp["halted"];
  busy: boolean;
  note: string | null;
  error: string | null;
  onResume: () => void;
}) {
  const h = c.risk.halted;
  if (halted === null) {
    return <p className="hint risk-halt-unknown" role="status">{h.unknown}</p>;
  }
  if (!halted.tripped) return null;
  // ⭐ 用後端的 `resumable`（引擎以 `killswitch.rearm_allowed_for` 導出）而不是在
  // 這裡比對 reason 字串：「哪些原因可以恢復」只能有一個定義點，前端各自比對的話，
  // 引擎哪天新增一種不可恢復的原因，這裡會照樣給出一顆按了必然失敗的按鈕。
  // `resumable === null`（舊版引擎的心跳沒有這一格）＝未知 ⇒ 同樣不給按鈕。
  const resumable = halted.resumable === true;
  return (
    <div className="ops-alert risk-halted" role="alert">
      <p className="ops-alert-body">{h.title}</p>
      <p className="hint">{h.body}</p>
      <p className="hint mono">{h.reasonLabel}: {halted.reason ?? h.unknownValue}</p>
      <p className="hint mono">{h.trippedAtLabel}: {halted.tripped_at ?? h.unknownValue}</p>
      <p className="hint mono">
        {h.cooldownLabel}: {halted.cooldown_hours ?? h.unknownValue}
      </p>
      {halted.resume_at !== null
        ? <p className="hint mono">{h.resumeAtLabel}: {halted.resume_at}</p>
        : <p className="hint">{h.noAutoResume}</p>}
      {/* ⭐ 殘留部位：不擋恢復，但要讓客戶在按下去之前看到（知情的決定）。 */}
      {halted.residual_exposure === true && (
        <p className="hint risk-halt-residual">{h.residualNote}</p>
      )}
      {resumable ? (
        <>
          <p className="hint">{h.resumeNote}</p>
          <button type="button" className="btn btn-secondary" disabled={busy}
            onClick={onResume}>
            {busy ? h.resuming : h.resumeButton}
          </button>
        </>
      ) : (
        <p className="hint risk-halt-governance">{h.leaderRevokedNote}</p>
      )}
      {note && <p className="hint risk-resumed" role="status">{note}</p>}
      {error && <div className="sign-error" role="alert"><p>{error}</p></div>}
    </div>
  );
}

/** 簽章流程的失敗文案（沿本頁 errorCopy 的原則：每一條都說出「現在的狀態」）。 */
function riskErrorCopy(r: RiskFlowFailure): string {
  const e = c.risk.errors;
  if (r.kind === "wallet-rejected") return e.walletRejected;
  if (r.kind === "signer-mismatch") return e.signerMismatch;
  if (r.kind === "content-mismatch") return e.contentMismatch;
  const base = r.kind === "message-failed" ? e.messageFailed : e.submitFailed;
  // 後端 detail 原樣附上（供回報問題用），不對字串比對推測原因。
  const detail = r.error instanceof ApiError ? r.error.detail : undefined;
  return detail ? `${base}（${detail}）` : base;
}

function RiskControlsSection() {
  const me = useMe();
  const { signMessageAsync } = useSignMessage();
  const queryClient = useQueryClient();
  const risk = useQuery<MyRiskResp>({ queryKey: ["me-risk"], queryFn: getMyRisk });
  const [draft, setDraft] = useState<RiskPrefs | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedNote, setSavedNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [unlocking, setUnlocking] = useState(false);
  const [unlockNote, setUnlockNote] = useState<string | null>(null);
  const [unlockError, setUnlockError] = useState<string | null>(null);

  // 伺服器值是唯一的初始來源；載入完成後灌進草稿一次（之後由使用者主導）。
  useEffect(() => {
    if (risk.data && draft === null) setDraft(risk.data.prefs);
  }, [risk.data, draft]);

  if (risk.isLoading) {
    return (
      <section className="risk-section">
        <p className="hint">{COPY.common.loading}</p>
      </section>
    );
  }
  // 讀不到 ⇒ 只說「讀不到」，不畫一個預設值的表單：那會讓客戶以為他看到的是自己的
  // 設定，並據此以為風控是關的（或開的）。
  if (risk.error || !risk.data || !draft) {
    return (
      <section className="risk-section">
        <h2 className="panel-title">{c.risk.title}</h2>
        <p className="hint">{c.risk.loadError}</p>
      </section>
    );
  }

  const data = risk.data;
  const tracking = data.specs.filter((s) => s.group === "tracking");
  const riskParams = data.specs.filter((s) => s.group === "risk");

  function edit(next: RiskPrefs) {
    setSavedNote(null);
    setError(null);
    setDraft(next);
  }

  async function save() {
    if (!draft || !me.data) return;
    setSaving(true);
    setError(null);
    setSavedNote(null);
    const target = draft;
    const r = await runRiskSettingsFlow(
      {
        fetchMessage: () => getRiskSettingsMessage(target),
        // 原文原樣進錢包：不加工、不重組（後端會用自己重建的版本驗簽）。
        signMessage: (message) => signMessageAsync({ message }),
        recover: recoverPersonalSigner,
        submit: postMyRisk,
      },
      // ⭐ expectedPrefs＝畫面上這一份草稿。沒有它，被打穿的 API 可以回一份把保護
      // 關掉的原文，而流程的其他每一關（recover、後端重建驗簽）都會照樣放行。
      {
        expectedSigner: me.data.address,
        expectedAccountId: me.data.account_id,
        expectedPrefs: target,
      },
    );
    setSaving(false);
    if (r.ok) {
      // 生效說明一律用後端原文（單一來源，不在前端另寫一份會過期的說明）。
      setSavedNote(r.resp.effective_note);
      void queryClient.invalidateQueries({ queryKey: ["me-risk"] });
    } else setError(riskErrorCopy(r));
  }

  async function resume() {
    if (!me.data) return;
    setUnlocking(true);
    setUnlockError(null);
    setUnlockNote(null);
    const r = await runRiskUnlockFlow(
      {
        fetchMessage: getRiskUnlockMessage,
        signMessage: (message) => signMessageAsync({ message }),
        recover: recoverPersonalSigner,
        submit: postRiskUnlock,
      },
      {
        expectedSigner: me.data.address,
        expectedAccountId: me.data.account_id,
        // 域分隔預驗的比對清單取自後端 specs（不是前端硬編）——後端新增參數時
        // 這道防線自動跟上（見 lib/riskSettingsFlow.ts 檔頭）。
        riskParamNames: data.specs.map((s) => s.name),
      },
    );
    setUnlocking(false);
    if (r.ok) {
      setUnlockNote(r.resp.effective_note);
      void queryClient.invalidateQueries({ queryKey: ["me-risk"] });
    } else setUnlockError(riskErrorCopy(r));
  }

  return (
    <section className="risk-section" aria-label={c.risk.title}>
      <h2 className="panel-title">{c.risk.title}</h2>
      <p className="hint">{c.risk.subtitle}</p>
      <p className="hint">{c.risk.applyNote}</p>

      <HaltedNotice halted={data.halted} busy={unlocking} note={unlockNote}
        error={unlockError} onResume={() => void resume()} />

      {/* ⭐ group="tracking"：**不受風控開關影響**，所以畫在 checkbox 之外、永遠顯示。
          藏進下面的展開區，會讓關掉風控的客戶以為自己也關掉了它。 */}
      {tracking.length > 0 && (
        <div className="risk-tracking">
          <p className="eyebrow">{c.risk.trackingTitle}</p>
          <p className="hint">{c.risk.trackingSubtitle}</p>
          {tracking.map((spec) => (
            <RiskParamField key={spec.name} spec={spec} prefs={draft!} onChange={edit} />
          ))}
        </div>
      )}

      <label className="risk-toggle">
        <input type="checkbox" checked={draft.enabled}
          onChange={(e) => edit({ ...draft!, enabled: e.target.checked })} />
        <span>{c.risk.enableLabel}</span>
      </label>
      <p className="hint risk-toggle-help">{c.risk.enableHelp}</p>

      {/* 勾選後才展開細項——沒開風控時這些門檻不會被執法，擺出來只會讓人以為有。 */}
      {draft.enabled && (
        <div className="risk-details">
          <p className="eyebrow">{c.risk.detailsTitle}</p>
          {riskParams.map((spec) => (
            <RiskParamField key={spec.name} spec={spec} prefs={draft!} onChange={edit} />
          ))}
        </div>
      )}

      <p className="hint risk-applied" role="status">{appliedNoteOf(data)}</p>
      {data.applied !== null && (
        <p className="hint mono">
          {c.risk.applied.sourceLabel}: {data.applied.source}
          {data.applied.changed_at !== null
            && `　${c.risk.applied.changedAtLabel}: ${data.applied.changed_at}`}
        </p>
      )}

      <div className="step-actions">
        <button type="button" className="btn btn-secondary" disabled={saving}
          onClick={() => void save()}>
          {saving ? c.risk.saving : c.risk.saveButton}
        </button>
      </div>
      <p className="hint">{c.risk.signNote}</p>
      {savedNote && (
        <p className="hint risk-saved" role="status">{c.risk.saved} {savedNote}</p>
      )}
      {error && <div className="sign-error" role="alert"><p>{error}</p></div>}
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

/**
 * ⭐ 誠信要求 6：確認對話框。成本（收斂部位＝平舊開新、付真實交易成本）與生效時機
 * （下一個 cycle，不是立即）都必須在**按下確認之前**看得到——這是使用者唯一一次
 * 能反悔的地方，之後就是簽名與真實成交。
 * 刻意不用 window.confirm：它塞不下這些內容，也無法被測試釘住。
 */
/**
 * 對話框裡的 leader 標籤。名稱本身就是位址縮寫時（自訂位址在背景目錄查不到名稱）
 * 只顯示一次——`0x2222…222（0x2222…222）` 讀起來像兩個不同的東西。
 */
function leaderLabel(leader: LeaderEntry): string {
  const short = shortAddr(leader.address);
  return leader.name === short ? short : `${leader.name}（${short}）`;
}

function ConfirmDialog({ leader, onCancel, onConfirm }: {
  leader: LeaderEntry;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div className="leader-dialog-backdrop">
      <div className="panel leader-dialog" role="dialog" aria-modal="true"
           aria-label={c.confirmTitle}>
        <p className="leader-dialog-title">{c.confirmTitle}</p>
        <p className="hint mono" title={leader.address}>
          {c.confirmLeaderLabel}: {leaderLabel(leader)}
        </p>
        <p className="leader-dialog-body">{c.confirmCost}</p>
        <p className="leader-dialog-body">{c.confirmTiming}</p>
        <p className="leader-upper-bound">{c.upperBound}</p>
        <p className="hint">{c.confirmSignNote}</p>
        <div className="leader-dialog-actions">
          <button type="button" className="btn btn-ghost" onClick={onCancel}>
            {c.confirmCancel}
          </button>
          <button type="button" className="btn btn-primary" onClick={onConfirm}>
            {c.confirmOk}
          </button>
        </div>
      </div>
    </div>
  );
}
