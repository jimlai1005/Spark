import { describe, expect, it, vi } from "vitest";
import type { CloseAllMessageResp, CloseAllResp } from "./api";
import { runCloseAllFlow, type CloseAllDeps } from "./closeAllFlow";

const ACCOUNT = "fabc";
const SIGNER = "0xAbC0000000000000000000000000000000000001";
const SIG = `0x${"ab".repeat(65)}`;

const PAYLOAD: CloseAllMessageResp = {
  message:
    "Filet: close all positions and revoke copy-trading\n\n"
    + "Signing this tells Filet to close all of your open copy-trading positions\n"
    + "at market and stop following your strategy. This action is irreversible.\n\n"
    + `Account: ${ACCOUNT}\nNonce: n-1\nIssued At: 2026-08-28T01:00:00Z`,
  nonce: "n-1",
  issued_at: "2026-08-28T01:00:00Z",
  account_id: ACCOUNT,
};
const OK_RESP: CloseAllResp = {
  ok: true, account_id: ACCOUNT, effective: "next_engine_cycle",
  effective_note: "已記錄，下一輪觸發受控收尾。",
};

function deps(over: Partial<CloseAllDeps> = {}) {
  return {
    fetchMessage: vi.fn(async () => PAYLOAD),
    signMessage: vi.fn(async () => SIG),
    recover: vi.fn(async () => SIGNER.toLowerCase()),
    submit: vi.fn(async () => OK_RESP),
    ...over,
  } as never as CloseAllDeps & {
    fetchMessage: ReturnType<typeof vi.fn>;
    signMessage: ReturnType<typeof vi.fn>;
    recover: ReturnType<typeof vi.fn>;
    submit: ReturnType<typeof vi.fn>;
  };
}

const OPTS = { expectedSigner: SIGNER, expectedAccountId: ACCOUNT };

describe("runCloseAllFlow — 平倉並撤銷（kill switch 第二級）", () => {
  it("happy path：簽伺服器原文 → recover 相符 → 整包 payload 原樣送出", async () => {
    const d = deps();
    const r = await runCloseAllFlow(d, OPTS);

    expect(r).toEqual({ ok: true, resp: OK_RESP });
    expect(d.signMessage).toHaveBeenCalledWith(PAYLOAD.message);
    expect(d.submit).toHaveBeenCalledWith(PAYLOAD, SIG);
  });

  it("⭐ account_id ≠ 我 → 中止，零網路請求（不喚起錢包）", async () => {
    const d = deps({ fetchMessage: vi.fn(async () => ({ ...PAYLOAD, account_id: "fevil" })) });
    const r = await runCloseAllFlow(d, OPTS);

    expect(r).toEqual({ ok: false, kind: "content-mismatch" });
    expect(d.signMessage).not.toHaveBeenCalled();
  });

  it("⭐ 原文沒有綁在我的帳號上 → 中止", async () => {
    const d = deps({
      fetchMessage: vi.fn(async () => ({
        ...PAYLOAD, message: PAYLOAD.message.replace(ACCOUNT, "fother"),
      })),
    });
    const r = await runCloseAllFlow(d, OPTS);

    expect(r).toEqual({ ok: false, kind: "content-mismatch" });
    expect(d.signMessage).not.toHaveBeenCalled();
  });

  it("⭐ 域分隔：回的其實是另一種動作的原文 → 中止，不喚起錢包", async () => {
    // 若放行，客戶會在以為自己只是平倉並撤銷的情況下簽掉別的授權。
    const d = deps({
      fetchMessage: vi.fn(async () => ({
        ...PAYLOAD,
        message: `Filet: resume copy-trading after a risk halt\n\nAccount: ${ACCOUNT}\nNonce: n-1\nIssued At: 2026-08-28T01:00:00Z`,
      })),
    });
    const r = await runCloseAllFlow(d, OPTS);

    expect(r).toEqual({ ok: false, kind: "content-mismatch" });
    expect(d.signMessage).not.toHaveBeenCalled();
  });

  it("recover 不符 → signer-mismatch，且完全不送出", async () => {
    const d = deps({
      recover: vi.fn(async () => "0x9999999999999999999999999999999999999999"),
    });
    const r = await runCloseAllFlow(d, OPTS);

    expect(r).toEqual({ ok: false, kind: "signer-mismatch" });
    expect(d.submit).not.toHaveBeenCalled();
  });

  it("錢包拒絕 → wallet-rejected；取原文失敗 → message-failed（不叫錢包）", async () => {
    const rejected = deps({
      signMessage: vi.fn(async () => { throw new Error("User rejected"); }),
    });
    expect(await runCloseAllFlow(rejected, OPTS))
      .toEqual({ ok: false, kind: "wallet-rejected" });

    const err = new Error("network down");
    const failed = deps({ fetchMessage: vi.fn(async () => { throw err; }) });
    expect(await runCloseAllFlow(failed, OPTS))
      .toEqual({ ok: false, kind: "message-failed", error: err });
    expect(failed.signMessage).not.toHaveBeenCalled();
  });

  it("⭐ 送出失敗不自動重試：submit 只被呼叫一次", async () => {
    const err = new Error("409");
    const d = deps({ submit: vi.fn(async () => { throw err; }) });
    const r = await runCloseAllFlow(d, OPTS);

    expect(r).toEqual({ ok: false, kind: "submit-failed", error: err });
    expect(d.submit).toHaveBeenCalledTimes(1);
  });
});
