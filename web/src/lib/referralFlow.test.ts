import { describe, expect, it, vi } from "vitest";
import type { ReferralMessageResp, ReferralOptinResp } from "./api";
import {
  REFERRAL_OPTIN_FIRST_LINE,
  runReferralOptinFlow,
  type ReferralOptinDeps,
} from "./referralFlow";

const ACCOUNT = "fabc";
const SIGNER = "0xAbC0000000000000000000000000000000000001";
const SIG = `0x${"ab".repeat(65)}`;
const CODE = "FILET1000";

// 沿 riskSettingsFlow.test.ts 域分隔測試使用的風控參數名清單。
const RISK_PARAM_NAMES = [
  "size_tolerance", "max_drawdown_pct", "max_total_drawdown_pct",
  "flatten_on_breach", "cooldown_hours",
];

/**
 * 伺服器產生的 canonical 原文。⭐ 版型逐字照抄後端 `build_referral_optin_message`
 * （filet/referral_optin.py，見 plan 第 2 節）。
 */
function messageFor(code: string): string {
  return (
    `${REFERRAL_OPTIN_FIRST_LINE}\n\n`
    + "Signing this authorises Filet to set the referral code below on your\n"
    + "Hyperliquid account, using the trading agent you already approved.\n"
    + "Hyperliquid gives referred accounts a 4% fee discount on their first $25M\n"
    + "of trading volume, and pays Filet a share of the trading fees you pay.\n"
    + "A referral code can be set only once per account and cannot be changed\n"
    + "later. If your account already has a referral code, nothing changes.\n"
    + "This is optional: copy-trading works the same whether or not you sign.\n"
    + "No positions are opened or closed by this action.\n\n"
    + `Account: ${ACCOUNT}\n`
    + `Referral Code: ${code}\n`
    + "Nonce: n-1\nIssued At: 2026-09-19T00:00:00Z"
  );
}

const PAYLOAD: ReferralMessageResp = {
  message: messageFor(CODE),
  nonce: "n-1",
  issued_at: "2026-09-19T00:00:00Z",
  account_id: ACCOUNT,
  code: CODE,
};

const OK_RESP: ReferralOptinResp = {
  ok: true, account_id: ACCOUNT, code: CODE, effective: "next_engine_cycle",
};

function deps(over: Partial<ReferralOptinDeps> = {}): ReferralOptinDeps & {
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

const OPTS = {
  expectedSigner: SIGNER, expectedAccountId: ACCOUNT, expectedCode: CODE,
  riskParamNames: RISK_PARAM_NAMES,
};

describe("runReferralOptinFlow — 一般流程（沿 riskSettingsFlow 的謹慎度）", () => {
  it("happy path：簽伺服器原文 → recover 相符 → 整包 payload 原樣送出", async () => {
    const d = deps();
    const r = await runReferralOptinFlow(d, OPTS);

    expect(r).toEqual({ ok: true, resp: OK_RESP });
    expect(d.signMessage).toHaveBeenCalledWith(PAYLOAD.message);
    expect(d.submit).toHaveBeenCalledWith(PAYLOAD, SIG);
  });

  it("取原文失敗 → message-failed，不叫錢包", async () => {
    const err = new Error("boom");
    const d = deps({ fetchMessage: vi.fn(async () => { throw err; }) });
    const r = await runReferralOptinFlow(d, OPTS);

    expect(r).toEqual({ ok: false, kind: "message-failed", error: err });
    expect(d.signMessage).not.toHaveBeenCalled();
  });

  it("錢包拒絕 → wallet-rejected，不 recover、不送出", async () => {
    const d = deps({ signMessage: vi.fn(async () => { throw new Error("User rejected"); }) });
    const r = await runReferralOptinFlow(d, OPTS);

    expect(r).toEqual({ ok: false, kind: "wallet-rejected" });
    expect(d.recover).not.toHaveBeenCalled();
    expect(d.submit).not.toHaveBeenCalled();
  });

  it("⭐ recover 出的簽章者 ≠ 登入地址 → signer-mismatch，完全不送出", async () => {
    const d = deps({ recover: vi.fn(async () => "0x9999999999999999999999999999999999999999") });
    const r = await runReferralOptinFlow(d, OPTS);

    expect(r).toEqual({ ok: false, kind: "signer-mismatch" });
    expect(d.submit).not.toHaveBeenCalled();
  });

  it("⭐ 送出失敗**不自動重試**：submit 只被呼叫一次", async () => {
    const err = new Error("500");
    const d = deps({ submit: vi.fn(async () => { throw err; }) });
    const r = await runReferralOptinFlow(d, OPTS);

    expect(r).toEqual({ ok: false, kind: "submit-failed", error: err });
    expect(d.submit).toHaveBeenCalledTimes(1);
  });
});

/**
 * ⭐ 被打穿的 filet-api 想把一次「設定推薦碼」的授權換成別的東西（別人的帳號、
 * 別的碼、或其實是另一個域的動作），唯一的著力點就是這裡：回一份被動過的待簽
 * 原文。攔截點必須在**進錢包之前**。
 */
describe("runReferralOptinFlow — 內容預驗 ⭐（進錢包之前，零網路請求）", () => {
  function expectBlocked(d: ReturnType<typeof deps>, r: { ok: boolean }) {
    expect(r).toEqual({ ok: false, kind: "content-mismatch" });
    expect(d.signMessage).not.toHaveBeenCalled();
    expect(d.recover).not.toHaveBeenCalled();
    expect(d.submit).not.toHaveBeenCalled();
  }

  it("⭐ account_id ≠ 我 → 中止：別人的帳號不該由我的簽章授權", async () => {
    const d = deps({ fetchMessage: vi.fn(async () => ({ ...PAYLOAD, account_id: "fevil" })) });
    expectBlocked(d, await runReferralOptinFlow(d, OPTS));
  });

  it("⭐ payload.code 與我方預期的推薦碼不符 → 中止", async () => {
    const d = deps({
      fetchMessage: vi.fn(async () => ({ ...PAYLOAD, code: "EVILCODE", message: messageFor("EVILCODE") })),
    });
    expectBlocked(d, await runReferralOptinFlow(d, OPTS));
  });

  it("⭐ 原文缺少 `Referral Code:` 那一行（被刪掉）→ 中止（缺席不等於同意）", async () => {
    const d = deps({
      fetchMessage: vi.fn(async () => ({
        ...PAYLOAD,
        message: PAYLOAD.message.split("\n").filter((l) => !l.startsWith("Referral Code:")).join("\n"),
      })),
    });
    expectBlocked(d, await runReferralOptinFlow(d, OPTS));
  });

  it("⭐ 域分隔第一行被換成別的字面量 → 中止（不是這個動作專屬的原文）", async () => {
    const d = deps({
      fetchMessage: vi.fn(async () => ({
        ...PAYLOAD,
        message: PAYLOAD.message.replace(
          REFERRAL_OPTIN_FIRST_LINE, "Filet: update copy-trading risk settings",
        ),
      })),
    });
    expectBlocked(d, await runReferralOptinFlow(d, OPTS));
  });

  it("⭐ 域分隔：原文其實混了一份風控設定（含風控參數行）→ 中止，不喚起錢包", async () => {
    const d = deps({
      fetchMessage: vi.fn(async () => ({
        ...PAYLOAD,
        message: `${PAYLOAD.message}\nmax_drawdown_pct: 0.5`,
      })),
    });
    expectBlocked(d, await runReferralOptinFlow(d, OPTS));
  });
});
