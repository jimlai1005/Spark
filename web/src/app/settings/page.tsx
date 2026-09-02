"use client";
/**
 * `/settings` — 帳戶設定中樞（Task 16）。
 *
 * 登入後四段，各自獨立查詢、獨立失敗處理——任一段讀不到不擋其餘三段（工程原則 3：
 * 讀不到 ≠ 沒有；沿舊版 `leaders/page.tsx` 的 `CurrentLeaderPanel`／`RiskControlsSection`
 * 既定原則，那兩個元件在 Task 11 遷移 `/leaders` → `/advanced` 時被暫時落掉，本頁把它們
 * 以新視覺復活，另加資金配置與授權管理兩段）：
 *
 * 1. 風控設定（`RiskSection`）——沿舊版 `RiskControlsSection` 原樣邏輯：opt-in（裁決 1），
 *    數字全來自後端 `specs`，儲存＝簽章（`lib/riskSettingsFlow.ts`），熔斷時顯示恢復入口。
 * 2. 資金配置（`CapitalSection`）——Task 10b 同一套簽章防線（`lib/capitalSettingsFlow.ts`）：
 *    投入比例直接乘進部位大小，危害與換 leader 同級。
 * 3. 授權管理（`AuthorizationSection`）——agent 位址／builder fee／agent 授權狀態
 *    （`GET /api/onboard/status`，唯讀不簽章）；暫停跟單（`postPause`，無需簽章，Task 15）；
 *    平倉並撤銷（複用 `components/dashboard/CloseAllModal`，同一個簽章 modal，不重寫）。
 * 4. 目前跟隨的策略（`LeaderSection`）——沿舊版 `CurrentLeaderPanel`：讀不到就說讀不到，
 *    不退化成任何一種「你沒有 leader」；`engine_default`／`not_activated` 不得互相代用。
 *    另加「更換策略」「進階模式」兩個出口（本頁新增，設計稿無對應章節）。
 *
 * 未登入 → redirect `/strategies`（與 dashboard/onboarding 同慣例，guard 用 effect）。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useSignMessage } from "wagmi";
import {
  ApiError,
  getCapitalSettingsMessage,
  getDashboard,
  getMyCapital,
  getMyLeader,
  getMyRisk,
  getRiskSettingsMessage,
  getRiskUnlockMessage,
  getStatus,
  postCapitalSettings,
  postMyRisk,
  postPause,
  postRiskUnlock,
  type DashboardResp,
  type MyCapitalResp,
  type MyLeaderPendingChange,
  type MyLeaderResp,
  type MyRiskResp,
  type OnboardStatus,
  type RiskParamName,
  type RiskParamSpec,
  type RiskPrefs,
} from "@/lib/api";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { CloseAllModal } from "@/components/dashboard/CloseAllModal";
import { Toast } from "@/components/Toast";
import { runCapitalSettingsFlow, type CapitalFlowFailure } from "@/lib/capitalSettingsFlow";
import { fmtRatioPct, shortAddr } from "@/lib/format";
import { useMe } from "@/lib/hooks";
import { useCopy } from "@/lib/lang";
import { runRiskSettingsFlow, runRiskUnlockFlow, type RiskFlowFailure } from "@/lib/riskSettingsFlow";
import { capitalNoteOf, leaderNoteOf, paramCopyOf } from "@/lib/settingsCopy";
import { recoverPersonalSigner } from "@/lib/sign";

type Copy = ReturnType<typeof useCopy>;
type Me = { address: string; account_id: string };

// ---------- 共用比例 ↔ 百分比換算（0.1% 刻度，對齊後端 `risk_prefs._RATIO_STEP`）。
// 本檔刻意各自持有一份，理由同 `components/wizard/StepRiskLimits.tsx` 檔頭：
// 換一個檔案，換算就再寫一份純函式，兩份的測試各自抓分岔，不強行共用一個私有函式。 ----------
function ratioToPct(v: string): string {
  const n = Number(v);
  return Number.isFinite(n) ? String(Math.round(n * 1000) / 10) : "";
}
function pctToRatio(pct: string): string {
  const n = Number(pct);
  if (!Number.isFinite(n)) return pct;
  return String(Number((Math.round(n * 10) / 1000).toFixed(3)));
}

// ==================== 1. 風控設定 ====================

function riskErrorCopy(r: RiskFlowFailure, c: Copy["settings"]["risk"]): string {
  const e = c.errors;
  if (r.kind === "wallet-rejected") return e.walletRejected;
  if (r.kind === "signer-mismatch") return e.signerMismatch;
  if (r.kind === "content-mismatch") return e.contentMismatch;
  const base = r.kind === "message-failed" ? e.messageFailed : e.submitFailed;
  const detail = r.error instanceof ApiError ? r.error.detail : undefined;
  return detail ? `${base}（${detail}）` : base;
}

function scaleOf(spec: RiskParamSpec, c: Copy["settings"]["risk"]): {
  toDisplay: (raw: string) => string;
  fromDisplay: (shown: string) => string;
  step: string;
  suffix: string;
} {
  return spec.unit === "hours"
    ? { toDisplay: (v) => v, fromDisplay: (v) => v, step: "1", suffix: c.hoursSuffix }
    : { toDisplay: ratioToPct, fromDisplay: pctToRatio, step: "0.1", suffix: c.percentSuffix };
}

function prefValue(prefs: RiskPrefs, name: RiskParamName): string | boolean {
  return (prefs as unknown as Record<string, string | boolean>)[name];
}
function withPref(prefs: RiskPrefs, name: RiskParamName, v: string | boolean): RiskPrefs {
  return { ...prefs, [name]: v } as RiskPrefs;
}

// ⭐ M3 round3 Task 8（R2·P1）：建議值／目前生效值／待簽署值三種數字混在同一區，
// 用戶分不出哪個在作用——每個參數固定顯示「目前生效 / 你的設定」兩值，僅顯示層
// 重排，不動簽章流程與 API 呼叫。`sameRawValue`／`fmtParamValue` 只服務顯示，
// 精度容忍度沿用 `riskSettingsFlow.ts` 的 `samePref` 同一種數值同值即相同原則
// （各自持有一份，理由同檔頭「共用比例 ↔ 百分比換算」段：這裡是純顯示判斷，
// 不需要跟簽章預驗那份耦合）。
function sameRawValue(a: string | boolean, b: string | boolean): boolean {
  if (typeof a === "boolean" || typeof b === "boolean") return a === b;
  const na = Number(a);
  const nb = Number(b);
  if (Number.isFinite(na) && Number.isFinite(nb)) return na === nb;
  return a === b;
}

function fmtParamValue(
  v: string | boolean,
  type: "decimal" | "bool",
  unit: RiskParamSpec["unit"] | undefined,
  c: Copy["settings"]["risk"],
): string {
  if (type === "bool") return v === true ? c.boolOn : c.boolOff;
  const suffix = unit === "hours" ? c.hoursSuffix : c.percentSuffix;
  const toDisplay = unit === "hours" ? (x: string) => x : ratioToPct;
  return `${toDisplay(String(v))}${suffix}`;
}

/**
 * 每個參數固定顯示的「目前生效 / 你的設定」兩值。`submitted` 是已簽章提交的值
 * （`data.prefs`，不是還在編輯、尚未送出的 `draft`——待套用與否比的是「已提交
 * vs 引擎已採用」，草稿還沒簽就談不上套用進度）；`applied`/`appliedUnknown`
 * 由呼叫端從 `data.applied` 拆出（引擎心跳缺席或版本較舊讀不到逐項門檻時
 * `appliedUnknown=true`，不得只比對總開關就宣稱逐項都已生效，見 api.ts
 * `RiskAppliedInfo.prefs` 註解）。
 */
function ParamStatus({ submitted, applied, appliedUnknown, type, unit, c }: {
  submitted: string | boolean;
  applied: string | boolean | null;
  appliedUnknown: boolean;
  type: "decimal" | "bool";
  unit?: RiskParamSpec["unit"];
  c: Copy["settings"]["risk"];
}) {
  const pending = !appliedUnknown && applied !== null && !sameRawValue(applied, submitted);
  return (
    <p className="hint mono risk-param-status"
      style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center" }}
    >
      <span>
        {c.applied.effectiveLabel}:{" "}
        {appliedUnknown || applied === null ? c.applied.unknownShort : fmtParamValue(applied, type, unit, c)}
      </span>
      <span>{c.applied.yourSettingLabel}: {fmtParamValue(submitted, type, unit, c)}</span>
      {pending && (
        <span style={{ display: "inline-flex", alignItems: "center", gap: 6, color: "var(--warn)" }}>
          <span aria-hidden="true"
            style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--warn)", display: "inline-block" }}
          />
          {c.applied.pendingBadge}
        </span>
      )}
    </p>
  );
}

function RiskParamField({ spec, prefs, onChange, c, data }: {
  spec: RiskParamSpec;
  prefs: RiskPrefs;
  onChange: (next: RiskPrefs) => void;
  c: Copy["settings"]["risk"];
  data: MyRiskResp;
}) {
  const v = prefValue(prefs, spec.name);
  // ⚠️ `== null` 同時擋 null 與 undefined：舊版引擎心跳的 `applied` 塊**沒有**
  // `prefs` 鍵（undefined），`=== null` 放行後下一行就炸——2026-09-01 生產事故，
  // 引擎未升級期間所有已跟單用戶的設定頁白屏。
  const appliedUnknown = data.applied === null || data.applied.prefs == null;
  const appliedValue = appliedUnknown ? null : prefValue(data.applied!.prefs as RiskPrefs, spec.name);
  const statusProps = {
    submitted: prefValue(data.prefs, spec.name),
    applied: appliedValue,
    appliedUnknown,
    type: spec.type,
    unit: spec.unit,
    c,
  } as const;
  const paramCopy = paramCopyOf(spec, c);
  if (spec.type === "bool") {
    return (
      <div className="risk-field">
        <label className="risk-toggle">
          <input type="checkbox" checked={v === true}
            onChange={(e) => onChange(withPref(prefs, spec.name, e.target.checked))} />
          <span>{paramCopy.label}</span>
        </label>
        <p className="hint risk-recommended">
          {c.recommendedLabel}：{spec.recommended === true ? c.boolOn : c.boolOff}
        </p>
        <ParamStatus {...statusProps} />
        <p className="hint">{paramCopy.help}</p>
      </div>
    );
  }
  const s = scaleOf(spec, c);
  const shown = s.toDisplay(String(v));
  return (
    <div className="risk-field">
      <label htmlFor={`settings-risk-${spec.name}`}>{paramCopy.label}</label>
      <div className="risk-slider-row">
        <input
          id={`settings-risk-${spec.name}`}
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
        {c.recommendedLabel}：{s.toDisplay(String(spec.recommended))}{s.suffix}
      </p>
      <ParamStatus {...statusProps} />
      <p className="hint">{paramCopy.help}</p>
    </div>
  );
}

/** ⭐ `applied` 為 null ＝引擎心跳讀不到＝我們不知道（工程原則 3），排最前。 */
function appliedNoteOf(d: MyRiskResp, c: Copy["settings"]["risk"]): string {
  const a = d.applied;
  if (a === null || a.controls_enabled === null) return c.applied.unknown;
  if (d.submitted.issued_at === null) return c.applied.notSubmitted;
  if (a.prefs === null || a.prefs === undefined) return c.applied.unknown;
  const applied = a.prefs;
  const same = (Object.keys(d.prefs) as Array<keyof RiskPrefs>).every(
    (k) => String(applied[k]) === String(d.prefs[k]),
  );
  return same ? c.applied.inSync : c.applied.pending;
}

function HaltedNotice({ halted, busy, note, error, onResume, onDismissError, c }: {
  halted: MyRiskResp["halted"];
  busy: boolean;
  note: string | null;
  error: string | null;
  onResume: () => void;
  onDismissError: () => void;
  c: Copy["settings"]["risk"];
}) {
  const COPY = useCopy();
  const h = c.halted;
  if (halted === null) {
    return <p className="hint risk-halt-unknown" role="status">{h.unknown}</p>;
  }
  if (!halted.tripped) return null;
  const resumable = halted.resumable === true;
  return (
    <div className="ops-alert risk-halted" role="alert">
      <p className="ops-alert-body">{h.title}</p>
      <p className="hint">{h.body}</p>
      <p className="hint mono">{h.reasonLabel}: {halted.reason ?? h.unknownValue}</p>
      <p className="hint mono">{h.trippedAtLabel}: {halted.tripped_at ?? h.unknownValue}</p>
      <p className="hint mono">{h.cooldownLabel}: {halted.cooldown_hours ?? h.unknownValue}</p>
      {halted.resume_at !== null
        ? <p className="hint mono">{h.resumeAtLabel}: {halted.resume_at}</p>
        : <p className="hint">{h.noAutoResume}</p>}
      {halted.residual_exposure === true && (
        <p className="hint risk-halt-residual">{h.residualNote}</p>
      )}
      {resumable ? (
        <>
          <p className="hint">{h.resumeNote}</p>
          <button type="button" className="btn btn-secondary" disabled={busy} onClick={onResume}>
            {busy ? h.resuming : error ? COPY.settings.toast.retrySignButton : h.resumeButton}
          </button>
        </>
      ) : (
        <p className="hint risk-halt-governance">{h.leaderRevokedNote}</p>
      )}
      {note && <p className="hint risk-resumed" role="status">{note}</p>}
      {error && <Toast message={error} onDismiss={onDismissError} dismissLabel={COPY.settings.toast.dismiss} />}
    </div>
  );
}

function RiskSection({ me }: { me: Me }) {
  const COPY = useCopy();
  const c = COPY.settings.risk;
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
      <section className="risk-section" aria-label={c.title}>
        <h2 className="panel-title">{c.title}</h2>
        <p className="hint">{COPY.common.loading}</p>
      </section>
    );
  }
  if (risk.error || !risk.data || !draft) {
    return (
      <section className="risk-section" aria-label={c.title}>
        <h2 className="panel-title">{c.title}</h2>
        <p className="hint">{c.loadError}</p>
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
    if (!draft) return;
    setSaving(true);
    setError(null);
    setSavedNote(null);
    const target = draft;
    const r = await runRiskSettingsFlow(
      {
        fetchMessage: () => getRiskSettingsMessage(target),
        signMessage: (message) => signMessageAsync({ message }),
        recover: recoverPersonalSigner,
        submit: postMyRisk,
      },
      { expectedSigner: me.address, expectedAccountId: me.account_id, expectedPrefs: target },
    );
    setSaving(false);
    if (r.ok) {
      setSavedNote(r.resp.effective_note);
      void queryClient.invalidateQueries({ queryKey: ["me-risk"] });
    } else setError(riskErrorCopy(r, c));
  }

  async function resume() {
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
        expectedSigner: me.address,
        expectedAccountId: me.account_id,
        riskParamNames: data.specs.map((s) => s.name),
      },
    );
    setUnlocking(false);
    if (r.ok) {
      setUnlockNote(r.resp.effective_note);
      void queryClient.invalidateQueries({ queryKey: ["me-risk"] });
    } else setUnlockError(riskErrorCopy(r, c));
  }

  return (
    <section className="risk-section" aria-label={c.title}>
      <h2 className="panel-title">{c.title}</h2>
      <p className="hint">{c.subtitle}</p>
      <p className="hint">{c.applyNote}</p>

      <HaltedNotice halted={data.halted} busy={unlocking} note={unlockNote}
        error={unlockError} onResume={() => void resume()}
        onDismissError={() => setUnlockError(null)} c={c} />

      {tracking.length > 0 && (
        <div className="risk-tracking">
          <p className="eyebrow">{c.trackingTitle}</p>
          <p className="hint">{c.trackingSubtitle}</p>
          {tracking.map((spec) => (
            <RiskParamField key={spec.name} spec={spec} prefs={draft} onChange={edit} c={c} data={data} />
          ))}
        </div>
      )}

      <label className="risk-toggle">
        <input type="checkbox" checked={draft.enabled}
          onChange={(e) => edit({ ...draft, enabled: e.target.checked })} />
        <span>{c.enableLabel}</span>
      </label>
      <p className="hint risk-toggle-help">{c.enableHelp}</p>
      <ParamStatus
        submitted={data.prefs.enabled}
        applied={data.applied?.prefs != null ? data.applied.prefs.enabled : null}
        appliedUnknown={data.applied === null || data.applied.prefs == null}
        type="bool"
        c={c}
      />

      {draft.enabled && (
        <div className="risk-details">
          <p className="eyebrow">{c.detailsTitle}</p>
          {riskParams.map((spec) => (
            <RiskParamField key={spec.name} spec={spec} prefs={draft} onChange={edit} c={c} data={data} />
          ))}
        </div>
      )}

      <p className="hint risk-applied" role="status">{appliedNoteOf(data, c)}</p>
      {data.applied !== null && (
        <p className="hint mono">
          {c.applied.sourceLabel}: {data.applied.source}
          {data.applied.changed_at !== null && `　${c.applied.changedAtLabel}: ${data.applied.changed_at}`}
        </p>
      )}

      <div className="step-actions">
        <button type="button" className="btn btn-secondary" disabled={saving} onClick={() => void save()}>
          {saving ? c.saving : error ? COPY.settings.toast.retrySignButton : c.saveButton}
        </button>
      </div>
      <p className="hint">{c.signNote}</p>
      {savedNote && <p className="hint risk-saved" role="status">{c.saved} {savedNote}</p>}
      {error && <Toast message={error} onDismiss={() => setError(null)} dismissLabel={COPY.settings.toast.dismiss} />}
    </section>
  );
}

// ==================== 2. 資金配置 ====================

function capitalErrorCopy(r: CapitalFlowFailure, c: Copy["settings"]["capital"]): string {
  const e = c.errors;
  if (r.kind === "wallet-rejected") return e.walletRejected;
  if (r.kind === "signer-mismatch") return e.signerMismatch;
  if (r.kind === "content-mismatch") return e.contentMismatch;
  const base = r.kind === "message-failed" ? e.messageFailed : e.submitFailed;
  const detail = r.error instanceof ApiError ? r.error.detail : undefined;
  return detail ? `${base}（${detail}）` : base;
}

const CAPITAL_SCALE_MIN = 5;
const CAPITAL_SCALE_MAX = 100;

function CapitalSection({ me }: { me: Me }) {
  const COPY = useCopy();
  const c = COPY.settings.capital;
  const { signMessageAsync } = useSignMessage();
  const queryClient = useQueryClient();
  const capital = useQuery<MyCapitalResp>({ queryKey: ["me-capital"], queryFn: getMyCapital });
  const [scale, setScale] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedNote, setSavedNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!capital.data || scale !== null) return;
    const current = capital.data.effective?.capital_utilization ?? capital.data.pending?.capital_utilization;
    setScale(current != null ? Number(ratioToPct(current)) : CAPITAL_SCALE_MIN);
  }, [capital.data, scale]);

  if (capital.isLoading) {
    return (
      <section className="risk-section" aria-label={c.title}>
        <h2 className="panel-title">{c.title}</h2>
        <p className="hint">{COPY.common.loading}</p>
      </section>
    );
  }
  if (capital.error || !capital.data || scale === null) {
    return (
      <section className="risk-section" aria-label={c.title}>
        <h2 className="panel-title">{c.title}</h2>
        <p className="hint">{c.loadError}</p>
      </section>
    );
  }

  const data = capital.data;

  async function save() {
    if (scale === null) return;
    setSaving(true);
    setError(null);
    setSavedNote(null);
    const capitalUtilization = pctToRatio(String(scale));
    const r = await runCapitalSettingsFlow(
      {
        fetchMessage: () => getCapitalSettingsMessage("0", capitalUtilization, true),
        signMessage: (message) => signMessageAsync({ message }),
        recover: recoverPersonalSigner,
        submit: postCapitalSettings,
      },
      {
        expectedSigner: me.address, expectedAccountId: me.account_id,
        expectedAllocatedCapital: "0", expectedCapitalUtilization: capitalUtilization,
        expectedUseFullEquity: true,
      },
    );
    setSaving(false);
    if (r.ok) {
      setSavedNote(r.resp.effective_note);
      void queryClient.invalidateQueries({ queryKey: ["me-capital"] });
    } else setError(capitalErrorCopy(r, c));
  }

  return (
    <section className="risk-section" aria-label={c.title}>
      <h2 className="panel-title">{c.title}</h2>
      <p className="hint">{c.subtitle}</p>

      <div className="risk-field">
        <div className="risk-slider-row">
          <label htmlFor="settings-scale-slider">{c.scaleLabel}</label>
          <span className="mono risk-value">{scale}%</span>
        </div>
        <input id="settings-scale-slider" type="range" className="risk-slider"
          min={CAPITAL_SCALE_MIN} max={CAPITAL_SCALE_MAX} step={1} value={scale}
          onChange={(e) => { setSavedNote(null); setError(null); setScale(Number(e.target.value)); }} />
        <div className="inset">
          {/* ⭐ M3 round3 Task 8（R2·P1）：同風控設定「目前生效 / 你的設定」兩值
              慣例——「你的設定」有已提交未生效的值（`pending`）就顯示那個，沒有
              就等於目前生效值（沒有分岔）；黃點只在真的有 `pending` 時出現。 */}
          <p className="hint mono">
            {c.effectiveLabel}：{fmtRatioPct(data.effective?.capital_utilization ?? null)}
          </p>
          <p className="hint mono" style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center" }}>
            <span>
              {c.yourSettingLabel}：
              {fmtRatioPct(data.pending?.capital_utilization ?? data.effective?.capital_utilization ?? null)}
            </span>
            {data.pending && (
              <span
                role="status"
                style={{ display: "inline-flex", alignItems: "center", gap: 6, color: "var(--warn)" }}
              >
                <span aria-hidden="true" style={{
                  width: 6, height: 6, borderRadius: "50%", background: "var(--warn)", display: "inline-block",
                }}
                />
                {c.pendingLabel}
              </span>
            )}
          </p>
          <p className="hint">{capitalNoteOf(data, c)}</p>
        </div>
      </div>

      <div className="step-actions">
        <button type="button" className="btn btn-secondary" disabled={saving} onClick={() => void save()}>
          {saving ? c.saving : error ? COPY.settings.toast.retrySignButton : c.saveButton}
        </button>
      </div>
      <p className="hint">{c.signNote}</p>
      {savedNote && <p className="hint risk-saved" role="status">{c.saved} {savedNote}</p>}
      {error && <Toast message={error} onDismiss={() => setError(null)} dismissLabel={COPY.settings.toast.dismiss} />}
    </section>
  );
}

// ==================== 3. 授權管理 ====================

function AuthorizationSection({ me }: { me: Me }) {
  const COPY = useCopy();
  const c = COPY.settings.auth;
  const status = useQuery<OnboardStatus>({ queryKey: ["onboard-status"], queryFn: getStatus });
  const dash = useQuery<DashboardResp>({ queryKey: ["me-dashboard"], queryFn: getDashboard });
  const [pauseBusy, setPauseBusy] = useState(false);
  const [pauseError, setPauseError] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [closeAllSubmitted, setCloseAllSubmitted] = useState(false);
  // ⭐ M3 round4 Task R4-4（使用者裁決 4）：同 dashboard StatusCard，暫停/恢復
  // 前先確認——取消只關彈窗，不呼叫 postPause。
  const [pauseConfirmOpen, setPauseConfirmOpen] = useState(false);

  const state = dash.data?.status?.state ?? "inactive";
  const showActions = state === "following" || state === "paused";

  async function togglePause() {
    setPauseConfirmOpen(false);
    setPauseError(null);
    setPauseBusy(true);
    try {
      await postPause(state === "paused" ? "resume" : "pause");
      void dash.refetch();
    } catch {
      setPauseError(c.pauseErrorNote);
    } finally {
      setPauseBusy(false);
    }
  }

  return (
    <section className="risk-section" aria-label={c.title}>
      <h2 className="panel-title">{c.title}</h2>
      <p className="hint">{c.subtitle}</p>

      {status.isLoading && <p className="hint">{COPY.common.loading}</p>}
      {(status.error || (!status.isLoading && !status.data)) && (
        <p className="hint">{c.loadError}</p>
      )}
      {status.data && (
        <div className="inset">
          <p className="hint mono">{c.agentAddressLabel}: {status.data.agent_address ?? c.agentAddressMissing}</p>
          <p className="hint mono">
            {c.builderFeeLabel}: {status.data.builder_fee_approved ? c.approvedYes : c.approvedNo}
          </p>
          <p className="hint mono">
            {c.agentApprovedLabel}: {status.data.agent_approved ? c.approvedYes : c.approvedNo}
          </p>
        </div>
      )}

      <div className="dash-divider" />
      <p className="eyebrow">{c.pauseHeading}</p>
      {!showActions && <p className="hint">{c.noEngineNote}</p>}
      {showActions && (
        <div className="dash-status-actions">
          <button type="button" className="dash-btn-pause" onClick={() => setPauseConfirmOpen(true)} disabled={pauseBusy}>
            {state === "paused" ? c.resumeBtn : c.pauseBtn}
          </button>
          <button type="button" className="dash-btn-close" onClick={() => setModalOpen(true)} disabled={pauseBusy}>
            {c.closeAllBtn}
          </button>
        </div>
      )}
      {pauseConfirmOpen && (
        <ConfirmDialog
          title={state === "paused" ? c.resumeConfirm.title : c.pauseConfirm.title}
          body={state === "paused" ? c.resumeConfirm.body : c.pauseConfirm.body}
          confirmLabel={state === "paused" ? c.resumeConfirm.confirmBtn : c.pauseConfirm.confirmBtn}
          cancelLabel={state === "paused" ? c.resumeConfirm.cancelBtn : c.pauseConfirm.cancelBtn}
          busy={pauseBusy}
          onConfirm={() => void togglePause()}
          onCancel={() => setPauseConfirmOpen(false)}
        />
      )}
      {pauseError && <p className="dash-status-error">{pauseError}</p>}
      {closeAllSubmitted && <p className="hint" role="status">{c.closeAllPendingNote}</p>}

      {modalOpen && (
        <CloseAllModal
          me={me}
          positions={dash.data?.positions ?? null}
          onClose={() => setModalOpen(false)}
          onSubmitted={() => {
            setModalOpen(false);
            setCloseAllSubmitted(true);
            void dash.refetch();
          }}
        />
      )}
    </section>
  );
}

// ==================== 4. 目前跟隨的策略 ====================

function noneTitleOf(status: string, c: Copy["settings"]["leader"]): { title: string; unknown: boolean } {
  const titles: Record<string, string | undefined> = c.noneTitles;
  const title = titles[status];
  return title === undefined ? { title: c.noneTitleFallback, unknown: true } : { title, unknown: false };
}

function PendingLeaderChange({ change, c }: {
  change: MyLeaderPendingChange;
  c: Copy["settings"]["leader"];
}) {
  return (
    <div className="leader-perf-state leader-current-pending">
      <p className="leader-perf-state-title">{c.pendingTitle}</p>
      <p className="hint mono" title={change.leader_address}>
        {c.pendingLabel}: {shortAddr(change.leader_address)}
      </p>
      {change.issued_at !== null && (
        <p className="hint mono">{c.pendingIssuedAtLabel}: {change.issued_at}</p>
      )}
      {/* ⭐ 後端這則 note 恆為單一文案（無狀態分岔），直接用 copy.ts 固定字串，
          不再判斷伺服器散文；`change.note` 保留當 debug 欄位不顯示。 */}
      <p className="hint">{c.pendingChangeNote}</p>
    </div>
  );
}

function LeaderSection() {
  const COPY = useCopy();
  const c = COPY.settings.leader;
  const q = useQuery<MyLeaderResp>({ queryKey: ["my-leader"], queryFn: getMyLeader });

  return (
    <section className="risk-section leader-current" aria-label={c.title}>
      <h2 className="panel-title">{c.title}</h2>
      {q.isLoading && <p className="hint" role="status">{c.loading}</p>}
      {(q.error || (!q.isLoading && !q.data)) && (
        <>
          <p className="hint">{c.failedTitle}</p>
          <p className="hint">{c.failedNote}</p>
        </>
      )}
      {q.data && (
        <LeaderBody data={q.data} c={c} />
      )}
      <div className="step-actions">
        <Link href="/strategies" className="btn btn-secondary">{c.changeStrategyBtn}</Link>
        <Link href="/advanced" className="btn btn-secondary">{c.advancedModeBtn}</Link>
      </div>
      <p className="hint settings-help">
        {c.helpPrompt} <Link href="/contact">{c.helpLink}</Link>
      </p>
    </section>
  );
}

function LeaderBody({ data: d, c }: { data: MyLeaderResp; c: Copy["settings"]["leader"] }) {
  const none = d.leader_address === null ? noneTitleOf(d.status, c) : null;
  return (
    <>
      {d.leader_address !== null ? (
        <p className="mono leader-current-addr" title={d.leader_address}>
          {c.leaderLabel}: {d.leader_name === null
            ? shortAddr(d.leader_address)
            : `${d.leader_name}（${shortAddr(d.leader_address)}）`}
        </p>
      ) : (
        <p className="leader-current-none">{none?.title}</p>
      )}
      {none?.unknown === true && (
        <p className="hint mono">{c.statusLabel}: {d.status}</p>
      )}
      <p className="hint">{leaderNoteOf(d, c)}</p>
      {d.pending_change !== null && <PendingLeaderChange change={d.pending_change} c={c} />}
    </>
  );
}

// ==================== 頁面外殼 ====================

export default function SettingsPage() {
  const router = useRouter();
  const me = useMe();
  const COPY = useCopy();
  const c = COPY.settings;

  // 未登入一律 redirect /strategies（不在 render 期間呼叫 router.push，guard 用 effect，
  // 與 dashboard/onboarding 同慣例）。
  useEffect(() => {
    if (me.isLoading) return;
    if (!me.data) router.push("/strategies");
  }, [me.isLoading, me.data, router]);

  if (me.isLoading || !me.data) {
    return (
      <main className="page">
        <p className="hint">{c.loadingNote}</p>
      </main>
    );
  }

  return (
    <main className="page">
      <p className="eyebrow">{c.eyebrow}</p>
      <h1>{c.title}</h1>
      <p className="hint">{c.subtitle}</p>

      <RiskSection me={me.data} />
      <CapitalSection me={me.data} />
      <AuthorizationSection me={me.data} />
      <LeaderSection />
    </main>
  );
}
