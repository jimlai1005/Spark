import { describe, expect, it, vi } from "vitest";
import type { LeaderSelectMessageResp, LeaderSelectResp } from "./api";
import { runLeaderSelectFlow, type LeaderSelectDeps } from "./leaderSelectFlow";

/** 伺服器產生的 canonical 原文（版型見 filet/leader_change.py；此處只需原樣傳遞）。 */
const PAYLOAD: LeaderSelectMessageResp = {
  message:
    "Filet: change copy-trading leader\n\n" +
    "Account: fabc\nLeader: 0x1111111111111111111111111111111111111111\n" +
    "Nonce: n-1\nIssued At: 2026-07-19T00:00:00Z",
  nonce: "n-1",
  issued_at: "2026-07-19T00:00:00Z",
  leader_address: "0x1111111111111111111111111111111111111111",
  account_id: "fabc",
};
const SIGNER = "0xAbC0000000000000000000000000000000000001";
const SIG = `0x${"ab".repeat(65)}`;
const OK_RESP: LeaderSelectResp = {
  ok: true, account_id: "fabc", leader_address: PAYLOAD.leader_address,
  effective: "next_engine_cycle", effective_note: "下一個 cycle 生效", consequences: "會收斂部位",
};

function deps(over: Partial<LeaderSelectDeps> = {}) {
  return {
    fetchMessage: vi.fn(async () => PAYLOAD),
    signMessage: vi.fn(async () => SIG),
    recover: vi.fn(async () => SIGNER.toLowerCase()),
    submit: vi.fn(async () => OK_RESP),
    ...over,
  };
}

describe("runLeaderSelectFlow ⭐（沿 approvalFlow 的謹慎度）", () => {
  it("happy path：簽伺服器原文 → recover 相符 → 整包 payload 原樣送出", async () => {
    const d = deps();
    const r = await runLeaderSelectFlow(d, { expectedSigner: SIGNER });

    expect(r).toEqual({ ok: true, resp: OK_RESP });
    // ⭐ 簽的必須是伺服器原文本身（一個位元組差異就會被後端拒絕）
    expect(d.signMessage).toHaveBeenCalledWith(PAYLOAD.message);
    // ⭐ 送出的是同一個 payload 物件——前端沒有機會從別處拼欄位
    expect(d.submit).toHaveBeenCalledWith(PAYLOAD, SIG);
  });

  it("⭐ recover 出的簽章者 ≠ 登入地址 → 中止，且完全不送出（零網路請求）", async () => {
    const d = deps({ recover: vi.fn(async () => "0x9999999999999999999999999999999999999999") });
    const r = await runLeaderSelectFlow(d, { expectedSigner: SIGNER });

    expect(r).toEqual({ ok: false, kind: "signer-mismatch" });
    expect(d.submit).not.toHaveBeenCalled();
  });

  it("⭐ recover 本身失敗（簽名格式壞）→ fail closed 視為不符，同樣不送出", async () => {
    const d = deps({ recover: vi.fn(async () => { throw new Error("bad signature"); }) });
    const r = await runLeaderSelectFlow(d, { expectedSigner: SIGNER });

    expect(r).toEqual({ ok: false, kind: "signer-mismatch" });
    expect(d.submit).not.toHaveBeenCalled();
  });

  it("地址大小寫不同視為相同（recover 與登入地址一律小寫比對）", async () => {
    const d = deps({ recover: vi.fn(async () => SIGNER.toUpperCase()) });
    const r = await runLeaderSelectFlow(d, { expectedSigner: SIGNER.toLowerCase() });

    expect(r.ok).toBe(true);
    expect(d.submit).toHaveBeenCalledTimes(1);
  });

  it("錢包拒絕 → wallet-rejected，不 recover、不送出", async () => {
    const d = deps({ signMessage: vi.fn(async () => { throw new Error("User rejected"); }) });
    const r = await runLeaderSelectFlow(d, { expectedSigner: SIGNER });

    expect(r).toEqual({ ok: false, kind: "wallet-rejected" });
    expect(d.recover).not.toHaveBeenCalled();
    expect(d.submit).not.toHaveBeenCalled();
  });

  it("取原文失敗 → message-failed，不叫錢包（不浪費使用者一次簽名）", async () => {
    const err = new Error("boom");
    const d = deps({ fetchMessage: vi.fn(async () => { throw err; }) });
    const r = await runLeaderSelectFlow(d, { expectedSigner: SIGNER });

    expect(r).toEqual({ ok: false, kind: "message-failed", error: err });
    expect(d.signMessage).not.toHaveBeenCalled();
  });

  it("⭐ 送出失敗**不自動重試**（非冪等寫入 ＋ nonce 一次性）：submit 只被呼叫一次", async () => {
    const err = new Error("500");
    const d = deps({ submit: vi.fn(async () => { throw err; }) });
    const r = await runLeaderSelectFlow(d, { expectedSigner: SIGNER });

    expect(r).toEqual({ ok: false, kind: "submit-failed", error: err });
    expect(d.submit).toHaveBeenCalledTimes(1);
  });
});
