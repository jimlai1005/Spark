import { describe, expect, it, vi } from "vitest";
import type {
  RiskPrefs,
  RiskSettingsMessageResp,
  RiskSettingsResp,
  RiskUnlockMessageResp,
  RiskUnlockResp,
} from "./api";
import {
  runRiskSettingsFlow,
  runRiskUnlockFlow,
  type RiskSettingsDeps,
  type RiskUnlockDeps,
} from "./riskSettingsFlow";

const ACCOUNT = "fabc";
const SIGNER = "0xAbC0000000000000000000000000000000000001";
const SIG = `0x${"ab".repeat(65)}`;

const PREFS: RiskPrefs = {
  enabled: true,
  size_tolerance: "0.08",
  max_drawdown_pct: "0.2",
  max_total_drawdown_pct: "0.4",
  flatten_on_breach: true,
  cooldown_hours: "12",
};

/**
 * 伺服器產生的 canonical 原文。⭐ 版型逐字照抄後端 `build_risk_settings_message`
 * （filet/risk_settings.py）：每一個參數各佔一行 `<name>: <值>`，帶單位的參數在值
 * 後面附單位詞，總開關那一行的標籤是 `Risk Controls`。fixture 若簡化掉這個形狀，
 * 測試就驗不到真實流程會走的那條路徑。
 */
function messageFor(p: RiskPrefs): string {
  return (
    "Filet: update copy-trading risk settings\n\n"
    + "Signing this authorises Filet to change the risk controls on your\n"
    + "copy-trading account.\n\n"
    + `Account: ${ACCOUNT}\n`
    + `Risk Controls: ${p.enabled ? "enabled" : "disabled"}\n`
    + `size_tolerance: ${p.size_tolerance}\n`
    + `max_drawdown_pct: ${p.max_drawdown_pct}\n`
    + `max_total_drawdown_pct: ${p.max_total_drawdown_pct}\n`
    + `flatten_on_breach: ${p.flatten_on_breach}\n`
    + `cooldown_hours: ${p.cooldown_hours} hours\n`
    + "Nonce: n-1\nIssued At: 2026-07-30T00:00:00Z"
  );
}

const PAYLOAD: RiskSettingsMessageResp = {
  message: messageFor(PREFS),
  nonce: "n-1",
  issued_at: "2026-07-30T00:00:00Z",
  account_id: ACCOUNT,
  prefs: PREFS,
};

const OK_RESP: RiskSettingsResp = {
  ok: true, prefs: PREFS, effective_note: "引擎會在下一輪套用。",
};

function deps(over: Partial<RiskSettingsDeps> = {}): RiskSettingsDeps & {
  fetchMessage: ReturnType<typeof vi.fn>;
  signMessage: ReturnType<typeof vi.fn>;
  recover: ReturnType<typeof vi.fn>;
  submit: ReturnType<typeof vi.fn>;
} {
  return {
    fetchMessage: vi.fn(async () => PAYLOAD),
    signMessage: vi.fn(async () => SIG),
    recover: vi.fn(async () => SIGNER.toLowerCase()),
    submit: vi.fn(async () => OK_RESP),
    ...over,
  } as never;
}

const OPTS = { expectedSigner: SIGNER, expectedAccountId: ACCOUNT, expectedPrefs: PREFS };

describe("runRiskSettingsFlow — 一般流程（沿 leaderSelectFlow 的謹慎度）", () => {
  it("happy path：簽伺服器原文 → recover 相符 → 整包 payload 原樣送出", async () => {
    const d = deps();
    const r = await runRiskSettingsFlow(d, OPTS);

    expect(r).toEqual({ ok: true, resp: OK_RESP });
    expect(d.signMessage).toHaveBeenCalledWith(PAYLOAD.message);
    // 送出的是同一個 payload 物件——前端沒有機會從別處拼欄位
    expect(d.submit).toHaveBeenCalledWith(PAYLOAD, SIG);
  });

  it("取原文失敗 → message-failed，不叫錢包（不浪費使用者一次簽名）", async () => {
    const err = new Error("boom");
    const d = deps({ fetchMessage: vi.fn(async () => { throw err; }) });
    const r = await runRiskSettingsFlow(d, OPTS);

    expect(r).toEqual({ ok: false, kind: "message-failed", error: err });
    expect(d.signMessage).not.toHaveBeenCalled();
  });

  it("錢包拒絕 → wallet-rejected，不 recover、不送出", async () => {
    const d = deps({ signMessage: vi.fn(async () => { throw new Error("User rejected"); }) });
    const r = await runRiskSettingsFlow(d, OPTS);

    expect(r).toEqual({ ok: false, kind: "wallet-rejected" });
    expect(d.recover).not.toHaveBeenCalled();
    expect(d.submit).not.toHaveBeenCalled();
  });

  it("⭐ recover 出的簽章者 ≠ 登入地址 → 中止，且完全不送出（零網路請求）", async () => {
    const d = deps({ recover: vi.fn(async () => "0x9999999999999999999999999999999999999999") });
    const r = await runRiskSettingsFlow(d, OPTS);

    expect(r).toEqual({ ok: false, kind: "signer-mismatch" });
    expect(d.submit).not.toHaveBeenCalled();
  });

  it("⭐ recover 本身失敗（簽名格式壞）→ fail closed 視為不符，同樣不送出", async () => {
    const d = deps({ recover: vi.fn(async () => { throw new Error("bad signature"); }) });
    const r = await runRiskSettingsFlow(d, OPTS);

    expect(r).toEqual({ ok: false, kind: "signer-mismatch" });
    expect(d.submit).not.toHaveBeenCalled();
  });

  it("地址大小寫不同視為相同（一律小寫比對，不得誤擋）", async () => {
    const d = deps({ recover: vi.fn(async () => SIGNER.toUpperCase()) });
    const r = await runRiskSettingsFlow(d, { ...OPTS, expectedSigner: SIGNER.toLowerCase() });

    expect(r.ok).toBe(true);
    expect(d.submit).toHaveBeenCalledTimes(1);
  });

  it("⭐ 送出失敗**不自動重試**（非冪等寫入 ＋ nonce 一次性）：submit 只被呼叫一次", async () => {
    const err = new Error("500");
    const d = deps({ submit: vi.fn(async () => { throw err; }) });
    const r = await runRiskSettingsFlow(d, OPTS);

    expect(r).toEqual({ ok: false, kind: "submit-failed", error: err });
    expect(d.submit).toHaveBeenCalledTimes(1);
  });
});

/**
 * ⭐ 被打穿的 filet-api 想無中生有一次「把保護關掉」，唯一的著力點就是這裡：回一份
 * 指向別組設定的待簽原文。使用者在錢包裡看到的是一段英文，簽下去之後每一關
 * （recover、後端重建驗簽、引擎二次驗章）都會放行——那份簽章確實是本人簽的，
 * 只是簽的不是他要的那件事。攔截點必須在**進錢包之前**。
 */
describe("runRiskSettingsFlow — 內容預驗 ⭐（進錢包之前，零網路請求）", () => {
  function expectBlocked(d: ReturnType<typeof deps>, r: { ok: boolean }) {
    expect(r).toEqual({ ok: false, kind: "content-mismatch" });
    expect(d.signMessage).not.toHaveBeenCalled();
    expect(d.recover).not.toHaveBeenCalled();
    expect(d.submit).not.toHaveBeenCalled();
  }

  it("⭐ account_id ≠ 我 → 中止：別人的帳號不該由我的簽章授權", async () => {
    const d = deps({ fetchMessage: vi.fn(async () => ({ ...PAYLOAD, account_id: "fevil" })) });
    expectBlocked(d, await runRiskSettingsFlow(d, OPTS));
  });

  it("⭐ 伺服器回聲的 prefs 把風控關掉（我送的是開）→ 中止", async () => {
    const evil = { ...PREFS, enabled: false };
    const d = deps({
      fetchMessage: vi.fn(async () => ({ ...PAYLOAD, prefs: evil, message: messageFor(evil) })),
    });
    expectBlocked(d, await runRiskSettingsFlow(d, OPTS));
  });

  it("⭐ 逐欄位：門檻被偷偷放寬（0.2 → 0.5）→ 中止", async () => {
    const evil = { ...PREFS, max_drawdown_pct: "0.5" };
    const d = deps({
      fetchMessage: vi.fn(async () => ({ ...PAYLOAD, prefs: evil, message: messageFor(evil) })),
    });
    expectBlocked(d, await runRiskSettingsFlow(d, OPTS));
  });

  it("⭐ 欄位對得上、但 message 本體寫著另一組門檻 → 同樣中止", async () => {
    // 使用者在錢包裡實際看到、實際同意的是 message 本體。欄位與原文指向不同設定時，
    // 兩者之間就有一道縫——只驗欄位的預驗會整條放行這一種。
    const d = deps({
      fetchMessage: vi.fn(async () => ({
        ...PAYLOAD, message: messageFor({ ...PREFS, max_drawdown_pct: "0.5" }),
      })),
    });
    expectBlocked(d, await runRiskSettingsFlow(d, OPTS));
  });

  it("⭐ message 本體的總開關被改成 disabled（欄位仍是開）→ 中止", async () => {
    const d = deps({
      fetchMessage: vi.fn(async () => ({
        ...PAYLOAD, message: PAYLOAD.message.replace("Risk Controls: enabled",
                                                     "Risk Controls: disabled"),
      })),
    });
    expectBlocked(d, await runRiskSettingsFlow(d, OPTS));
  });

  it("⭐ 原文裡完全沒有這個參數那一行（被刪掉）→ 中止（缺席不等於同意）", async () => {
    const d = deps({
      fetchMessage: vi.fn(async () => ({
        ...PAYLOAD,
        message: PAYLOAD.message.split("\n")
          .filter((l) => !l.startsWith("cooldown_hours:")).join("\n"),
      })),
    });
    expectBlocked(d, await runRiskSettingsFlow(d, OPTS));
  });

  it("伺服器正規化（0.20 → 0.2）**不算**不符：同一個數值不得誤擋", async () => {
    // 後端 risk_prefs._as_bounded_decimal 會把 `0.20` 正規化成 `0.2`。把這視為
    // 竄改，會讓每一次儲存都失敗，而且看起來像遭到攻擊。
    const mine = { ...PREFS, max_drawdown_pct: "0.20" };
    const d = deps();
    const r = await runRiskSettingsFlow(d, { ...OPTS, expectedPrefs: mine });

    expect(r.ok).toBe(true);
    expect(d.signMessage).toHaveBeenCalledTimes(1);
  });

  it("帶單位的參數：原文的 `12 hours` 讀得出值 12（單位詞不得造成誤擋）", async () => {
    const d = deps();
    const r = await runRiskSettingsFlow(d, OPTS);
    expect(r.ok).toBe(true);
    expect(PAYLOAD.message).toContain("cooldown_hours: 12 hours");
  });
});

// ── 解除熔斷（一次性動作，與設定調整是兩個域）──────────────────────────
const UNLOCK_PAYLOAD: RiskUnlockMessageResp = {
  message:
    "Filet: resume copy-trading after a risk halt\n\n"
    + "Signing this tells Filet to lift that halt immediately.\n\n"
    + `Account: ${ACCOUNT}\nNonce: n-2\nIssued At: 2026-07-30T01:00:00Z`,
  nonce: "n-2",
  issued_at: "2026-07-30T01:00:00Z",
  account_id: ACCOUNT,
};
const UNLOCK_OK: RiskUnlockResp = { ok: true, effective_note: "下一輪恢復跟單。" };
const PARAM_NAMES = [
  "size_tolerance", "max_drawdown_pct", "max_total_drawdown_pct",
  "flatten_on_breach", "cooldown_hours",
];

function unlockDeps(over: Partial<RiskUnlockDeps> = {}) {
  return {
    fetchMessage: vi.fn(async () => UNLOCK_PAYLOAD),
    signMessage: vi.fn(async () => SIG),
    recover: vi.fn(async () => SIGNER.toLowerCase()),
    submit: vi.fn(async () => UNLOCK_OK),
    ...over,
  } as never as RiskUnlockDeps & {
    fetchMessage: ReturnType<typeof vi.fn>;
    signMessage: ReturnType<typeof vi.fn>;
    recover: ReturnType<typeof vi.fn>;
    submit: ReturnType<typeof vi.fn>;
  };
}

const UNLOCK_OPTS = {
  expectedSigner: SIGNER, expectedAccountId: ACCOUNT, riskParamNames: PARAM_NAMES,
};

describe("runRiskUnlockFlow — 立即恢復跟單", () => {
  it("happy path：簽伺服器原文 → recover 相符 → 整包 payload 原樣送出", async () => {
    const d = unlockDeps();
    const r = await runRiskUnlockFlow(d, UNLOCK_OPTS);

    expect(r).toEqual({ ok: true, resp: UNLOCK_OK });
    expect(d.signMessage).toHaveBeenCalledWith(UNLOCK_PAYLOAD.message);
    expect(d.submit).toHaveBeenCalledWith(UNLOCK_PAYLOAD, SIG);
  });

  it("⭐ 域分隔：解鎖端點回的其實是一份「設定」原文 → 中止，不喚起錢包", async () => {
    // 若放行，客戶會在以為自己只是恢復跟單的情況下簽掉一組（可能被放寬的）門檻。
    const d = unlockDeps({
      fetchMessage: vi.fn(async () => ({
        ...UNLOCK_PAYLOAD, message: messageFor({ ...PREFS, enabled: false }),
      })),
    });
    const r = await runRiskUnlockFlow(d, UNLOCK_OPTS);

    expect(r).toEqual({ ok: false, kind: "content-mismatch" });
    expect(d.signMessage).not.toHaveBeenCalled();
    expect(d.submit).not.toHaveBeenCalled();
  });

  it("⭐ account_id ≠ 我 → 中止，零網路請求", async () => {
    const d = unlockDeps({
      fetchMessage: vi.fn(async () => ({ ...UNLOCK_PAYLOAD, account_id: "fevil" })),
    });
    const r = await runRiskUnlockFlow(d, UNLOCK_OPTS);

    expect(r).toEqual({ ok: false, kind: "content-mismatch" });
    expect(d.signMessage).not.toHaveBeenCalled();
  });

  it("⭐ 原文沒有綁在我的帳號上 → 中止", async () => {
    const d = unlockDeps({
      fetchMessage: vi.fn(async () => ({
        ...UNLOCK_PAYLOAD, message: UNLOCK_PAYLOAD.message.replace(ACCOUNT, "fother"),
      })),
    });
    const r = await runRiskUnlockFlow(d, UNLOCK_OPTS);

    expect(r).toEqual({ ok: false, kind: "content-mismatch" });
    expect(d.signMessage).not.toHaveBeenCalled();
  });

  it("recover 不符 → signer-mismatch，且完全不送出", async () => {
    const d = unlockDeps({
      recover: vi.fn(async () => "0x9999999999999999999999999999999999999999"),
    });
    const r = await runRiskUnlockFlow(d, UNLOCK_OPTS);

    expect(r).toEqual({ ok: false, kind: "signer-mismatch" });
    expect(d.submit).not.toHaveBeenCalled();
  });

  it("錢包拒絕 → wallet-rejected；取原文失敗 → message-failed（不叫錢包）", async () => {
    const rejected = unlockDeps({
      signMessage: vi.fn(async () => { throw new Error("User rejected"); }),
    });
    expect(await runRiskUnlockFlow(rejected, UNLOCK_OPTS))
      .toEqual({ ok: false, kind: "wallet-rejected" });

    const err = new Error("no halt");
    const failed = unlockDeps({ fetchMessage: vi.fn(async () => { throw err; }) });
    expect(await runRiskUnlockFlow(failed, UNLOCK_OPTS))
      .toEqual({ ok: false, kind: "message-failed", error: err });
    expect(failed.signMessage).not.toHaveBeenCalled();
  });

  it("⭐ 送出失敗不自動重試：submit 只被呼叫一次", async () => {
    const err = new Error("409");
    const d = unlockDeps({ submit: vi.fn(async () => { throw err; }) });
    const r = await runRiskUnlockFlow(d, UNLOCK_OPTS);

    expect(r).toEqual({ ok: false, kind: "submit-failed", error: err });
    expect(d.submit).toHaveBeenCalledTimes(1);
  });
});
