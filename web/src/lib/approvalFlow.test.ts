import { describe, expect, it, vi } from "vitest";
import { runApprovalFlow, type ApprovalDeps } from "./approvalFlow";
import type { HlTypedData } from "./hl";

const TD: HlTypedData = {
  domain: { name: "HyperliquidSignTransaction", version: "1", chainId: 42161,
    verifyingContract: "0x0000000000000000000000000000000000000000" },
  types: {},
  primaryType: "HyperliquidTransaction:ApproveAgent",
  message: { type: "approveAgent", hyperliquidChain: "Testnet",
    signatureChainId: "0xa4b1", nonce: 1752700000000 },
};

const SIG = `0x${"ab".repeat(32)}${"cd".repeat(32)}1b`;
const USER = "0xabc0000000000000000000000000000000000001";

function deps(over: Partial<ApprovalDeps> = {}): ApprovalDeps {
  return {
    fetchPayload: vi.fn(async () => TD),
    signTypedData: vi.fn(async () => SIG),
    recover: vi.fn(async () => USER),
    submit: vi.fn(async () => ({ ok: true }) as const),
    ...over,
  };
}

describe("runApprovalFlow ⭐", () => {
  it("happy path：payload → 簽（typed data 原文 stringify）→ recover 預驗 → 直送", async () => {
    const d = deps();
    const r = await runApprovalFlow(d, { expectedSigner: USER });
    expect(r).toEqual({ ok: true });
    // 順序斷言
    const order = [d.fetchPayload, d.signTypedData, d.recover, d.submit]
      .map((f) => (f as ReturnType<typeof vi.fn>).mock.invocationCallOrder[0]);
    expect([...order]).toEqual([...order].sort((a, b) => a - b));
    // ⭐ 紅線 4：交給錢包的是 typed data 原文的 JSON.stringify
    expect(d.signTypedData).toHaveBeenCalledWith(JSON.stringify(TD));
    // recover 收到同一 typed data 與簽名
    expect(d.recover).toHaveBeenCalledWith(TD, SIG);
    expect(d.submit).toHaveBeenCalledWith(TD, SIG);
  });

  it("⭐ recover 不符 → 不送 HL、回 signer-mismatch", async () => {
    const d = deps({ recover: vi.fn(async () => "0xdead000000000000000000000000000000000001") });
    const r = await runApprovalFlow(d, { expectedSigner: USER });
    expect(r).toEqual({ ok: false, kind: "signer-mismatch" });
    expect(d.submit).not.toHaveBeenCalled();
  });

  it("expectedSigner 大小寫不敏感（同基準比較）", async () => {
    const d = deps();
    const r = await runApprovalFlow(d, {
      expectedSigner: "0xABC0000000000000000000000000000000000001",
    });
    expect(r).toEqual({ ok: true });
  });

  it("錢包拒簽 → wallet-rejected，不送 HL", async () => {
    const d = deps({ signTypedData: vi.fn(async () => { throw new Error("User rejected"); }) });
    const r = await runApprovalFlow(d, { expectedSigner: USER });
    expect(r).toEqual({ ok: false, kind: "wallet-rejected" });
    expect(d.submit).not.toHaveBeenCalled();
  });

  it("payload 取得失敗 → payload-failed", async () => {
    const d = deps({ fetchPayload: vi.fn(async () => { throw new Error("502"); }) });
    const r = await runApprovalFlow(d, { expectedSigner: USER });
    expect(r).toEqual({ ok: false, kind: "payload-failed" });
  });

  it("HL semantic / transient 失敗原樣轉出（UI 依 kind 選文案與重試策略）", async () => {
    const dSem = deps({ submit: vi.fn(async () => ({ ok: false, kind: "semantic", message: "rejected" }) as const) });
    expect(await runApprovalFlow(dSem, { expectedSigner: USER }))
      .toEqual({ ok: false, kind: "hl-semantic", message: "rejected" });
    const dTr = deps({ submit: vi.fn(async () => ({ ok: false, kind: "transient", message: "net" }) as const) });
    const r = await runApprovalFlow(dTr, { expectedSigner: USER });
    expect(r).toMatchObject({ ok: false, kind: "hl-transient" });
    // transient 回帶 retrySubmit：重送同一簽名（同 nonce，HL 去重——設計定案 11）
    if (!r.ok && r.kind === "hl-transient") {
      const d2submit = dTr.submit as ReturnType<typeof vi.fn>;
      d2submit.mockResolvedValueOnce({ ok: true });
      expect(await r.retrySubmit()).toEqual({ ok: true });
      expect(d2submit).toHaveBeenCalledTimes(2);
      // 兩次都是同一 typed data + 同一簽名
      expect(d2submit.mock.calls[0]).toEqual(d2submit.mock.calls[1]);
    }
  });
});
