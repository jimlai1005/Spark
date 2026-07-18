import { privateKeyToAccount } from "viem/accounts";
import { describe, expect, it, vi } from "vitest";
import {
  buildExchangePayload,
  hlBaseUrl,
  normalizeV,
  recoverSigner,
  splitSignature,
  submitToHl,
  type HlTypedData,
} from "./hl";

// ---- 測試用 typed data（形狀照 research §1 / 後端 approvals.py 輸出）----
function approveAgentTypedData(): HlTypedData {
  return {
    domain: {
      name: "HyperliquidSignTransaction",
      version: "1",
      chainId: 42161,
      verifyingContract: "0x0000000000000000000000000000000000000000",
    },
    types: {
      EIP712Domain: [
        { name: "name", type: "string" },
        { name: "version", type: "string" },
        { name: "chainId", type: "uint256" },
        { name: "verifyingContract", type: "address" },
      ],
      "HyperliquidTransaction:ApproveAgent": [
        { name: "hyperliquidChain", type: "string" },
        { name: "agentAddress", type: "address" },
        { name: "agentName", type: "string" },
        { name: "nonce", type: "uint64" },
      ],
    },
    primaryType: "HyperliquidTransaction:ApproveAgent",
    message: {
      type: "approveAgent",
      hyperliquidChain: "Testnet",
      signatureChainId: "0xa4b1",
      agentAddress: "0x1111111111111111111111111111111111111111",
      agentName: "filet",
      nonce: 1752700000000,
    },
  };
}

const SIG_V27 = `0x${"ab".repeat(32)}${"cd".repeat(32)}1b`; // v=0x1b=27
const SIG_V28 = `0x${"ab".repeat(32)}${"cd".repeat(32)}1c`; // v=0x1c=28
const SIG_V0 = `0x${"ab".repeat(32)}${"cd".repeat(32)}00`;  // v=0 → 27
const SIG_V1 = `0x${"ab".repeat(32)}${"cd".repeat(32)}01`;  // v=1 → 28

describe("normalizeV", () => {
  it("0→27、1→28、27/28 原樣", () => {
    expect(normalizeV(0)).toBe(27);
    expect(normalizeV(1)).toBe(28);
    expect(normalizeV(27)).toBe(27);
    expect(normalizeV(28)).toBe(28);
  });
  it("其他值一律拒絕（不猜）", () => {
    for (const bad of [2, 26, 29, -1, 255]) {
      expect(() => normalizeV(bad)).toThrow(/無法正規化/);
    }
  });
});

describe("splitSignature", () => {
  it("65-byte hex 拆成 r/s/v，v 正規化", () => {
    expect(splitSignature(SIG_V27)).toEqual({ r: `0x${"ab".repeat(32)}`, s: `0x${"cd".repeat(32)}`, v: 27 });
    expect(splitSignature(SIG_V0).v).toBe(27);
    expect(splitSignature(SIG_V1).v).toBe(28);
    expect(splitSignature(SIG_V28).v).toBe(28);
  });
  it("長度/格式不對一律拒絕", () => {
    expect(() => splitSignature("0x1234")).toThrow(/65 bytes/);
    expect(() => splitSignature(`0x${"ab".repeat(64)}`)).toThrow(/65 bytes/); // 64B（無 v）
    expect(() => splitSignature(`zz${"ab".repeat(32)}${"cd".repeat(32)}1b`)).toThrow(/65 bytes/);
  });
});

describe("hlBaseUrl", () => {
  it("由 hyperliquidChain 推導官方 URL（設計定案 2：單一來源）", () => {
    expect(hlBaseUrl("Mainnet")).toBe("https://api.hyperliquid.xyz");
    expect(hlBaseUrl("Testnet")).toBe("https://api.hyperliquid-testnet.xyz");
  });
  it("未知鏈值拒絕", () => {
    // @ts-expect-error 蓄意壞輸入
    expect(() => hlBaseUrl("Devnet")).toThrow(/hyperliquidChain/);
  });
});

describe("buildExchangePayload", () => {
  it("action ≡ typed_data.message 原文；nonce 三位一體；null 欄位齊備（research §3）", () => {
    const td = approveAgentTypedData();
    const p = buildExchangePayload(td, SIG_V27);
    expect(p.action).toEqual(td.message);       // 原文，不增刪欄位
    expect(p.nonce).toBe(td.message.nonce);     // 頂層 nonce == action.nonce
    expect(p.signature).toEqual({ r: `0x${"ab".repeat(32)}`, s: `0x${"cd".repeat(32)}`, v: 27 });
    expect(p.vaultAddress).toBeNull();
    expect(p.expiresAfter).toBeNull();
    // 結構性：payload 恰好 5 個 key，無多餘欄位
    expect(Object.keys(p).sort()).toEqual(
      ["action", "expiresAfter", "nonce", "signature", "vaultAddress"].sort(),
    );
    // JSON 序列化後 null 欄位仍在（不是 undefined 被剔除）
    const json = JSON.parse(JSON.stringify(p));
    expect(json.vaultAddress).toBeNull();
    expect(json.expiresAfter).toBeNull();
  });
  it("nonce 缺失或非安全整數拒絕", () => {
    const td = approveAgentTypedData();
    (td.message as Record<string, unknown>).nonce = "1752700000000"; // 字串不行
    expect(() => buildExchangePayload(td, SIG_V27)).toThrow(/nonce/);
  });
});

describe("submitToHl", () => {
  const td = approveAgentTypedData();

  function mockFetchOnce(response: Partial<Response> | Error) {
    const fn = vi.fn(async () => {
      if (response instanceof Error) throw response;
      return response as Response;
    });
    return fn;
  }

  it("POST 到 testnet /exchange、Content-Type json、不帶 credentials（紅線 5）", async () => {
    const fetchFn = mockFetchOnce({
      ok: true,
      status: 200,
      json: async () => ({ status: "ok" }),
    } as Partial<Response>);
    const res = await submitToHl(td, SIG_V27, fetchFn as unknown as typeof fetch);
    expect(res).toEqual({ ok: true });
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://api.hyperliquid-testnet.xyz/exchange");
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
    expect(init.credentials).toBeUndefined(); // 絕不把 cookie 帶去 HL
    const body = JSON.parse(init.body as string);
    expect(body.action).toEqual(td.message);
    expect(body.nonce).toBe(td.message.nonce);
  });

  it('HL 回 {"status":"err"} → semantic（不重試同簽名）', async () => {
    const fetchFn = mockFetchOnce({
      ok: true,
      status: 200,
      json: async () => ({ status: "err", response: "User or API Wallet does not exist." }),
    } as Partial<Response>);
    const res = await submitToHl(td, SIG_V27, fetchFn as unknown as typeof fetch);
    expect(res).toEqual({
      ok: false, kind: "semantic",
      message: "User or API Wallet does not exist.",
    });
  });

  it("HTTP 4xx → semantic；5xx → transient（工程原則 2）", async () => {
    const r400 = await submitToHl(td, SIG_V27,
      mockFetchOnce({ ok: false, status: 422, json: async () => ({}) } as Partial<Response>) as unknown as typeof fetch);
    expect(r400.ok).toBe(false);
    if (!r400.ok) expect(r400.kind).toBe("semantic");
    const r500 = await submitToHl(td, SIG_V27,
      mockFetchOnce({ ok: false, status: 503, json: async () => ({}) } as Partial<Response>) as unknown as typeof fetch);
    if (!r500.ok) expect(r500.kind).toBe("transient");
  });

  it("網路層拋錯 → transient", async () => {
    const res = await submitToHl(td, SIG_V27,
      mockFetchOnce(new TypeError("Failed to fetch")) as unknown as typeof fetch);
    expect(res.ok).toBe(false);
    if (!res.ok) expect(res.kind).toBe("transient");
  });
});

describe("recoverSigner（真密碼學、離線——鏡射後端 round-trip pin）", () => {
  const PK = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d" as const;

  it("viem 帳號簽 typed data → recover 回同一地址（小寫）", async () => {
    const account = privateKeyToAccount(PK);
    const td = approveAgentTypedData();
    const sig = await account.signTypedData({
      domain: td.domain,
      types: td.types,
      primaryType: td.primaryType,
      message: td.message,
    } as Parameters<typeof account.signTypedData>[0]);
    const recovered = await recoverSigner(td, sig);
    expect(recovered).toBe(account.address.toLowerCase());
  });

  it("message 被竄改 → recover 出不同地址（mismatch 可被偵測）", async () => {
    const account = privateKeyToAccount(PK);
    const td = approveAgentTypedData();
    const sig = await account.signTypedData({
      domain: td.domain,
      types: td.types,
      primaryType: td.primaryType,
      message: td.message,
    } as Parameters<typeof account.signTypedData>[0]);
    const tampered = approveAgentTypedData();
    (tampered.message as Record<string, unknown>).agentAddress =
      "0x2222222222222222222222222222222222222222";
    const recovered = await recoverSigner(tampered, sig);
    expect(recovered).not.toBe(account.address.toLowerCase());
  });
});
