import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "./api";
import { ApiError } from "./api";

type Captured = { url: string; init: RequestInit };
let captured: Captured[];

function mockFetchJson(status: number, body: unknown) {
  captured = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      captured.push({ url, init });
      return {
        ok: status >= 200 && status < 300,
        status,
        json: async () => body,
      } as Response;
    }),
  );
}

beforeEach(() => {
  captured = [];
});

describe("request 基礎行為", () => {
  it("一律 credentials include、同源相對路徑（紅線 5）", async () => {
    mockFetchJson(200, { address: "0xabc", account_id: "fabc" });
    await api.getMe();
    expect(captured[0].url).toBe("/api/me");
    expect(captured[0].init.credentials).toBe("include");
  });

  it("401 → ApiError kind=auth", async () => {
    mockFetchJson(401, { detail: "未登入或 session 已過期" });
    await expect(api.getMe()).rejects.toMatchObject({ kind: "auth", status: 401 });
  });

  it("4xx → kind=client 且帶後端 detail", async () => {
    mockFetchJson(409, { detail: "已有 agent，不重生（避免 rotate 作廢既有鏈上授權）" });
    await expect(api.createAgent()).rejects.toMatchObject({
      kind: "client", status: 409, detail: expect.stringContaining("已有 agent"),
    });
  });

  it("502/503 → kind=upstream", async () => {
    mockFetchJson(502, { detail: "金鑰服務暫時不可用" });
    await expect(api.createAgent()).rejects.toMatchObject({ kind: "upstream" });
    mockFetchJson(503, { detail: "builder 門檻" });
    await expect(api.getApproveBuilderFeePayload(42161)).rejects.toMatchObject({ kind: "upstream" });
  });

  it("fetch 拋錯 → kind=network", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
    await expect(api.getMe()).rejects.toMatchObject({ kind: "network" });
  });
});

describe("端點契約（對照 src/spark/publicapi/app.py）", () => {
  it("getNonce：GET /api/auth/nonce?address=&chain_id=", async () => {
    mockFetchJson(200, { nonce: "n1", message: "filet.example wants you to sign in…" });
    const r = await api.getNonce("0xAbC0000000000000000000000000000000000001", 42161);
    expect(captured[0].url).toBe(
      "/api/auth/nonce?address=0xAbC0000000000000000000000000000000000001&chain_id=42161",
    );
    expect(r.nonce).toBe("n1");
  });

  it("authVerify：POST /api/auth/verify {nonce, signature}——唯一帶簽名的後端呼叫", async () => {
    mockFetchJson(200, { address: "0xabc", account_id: "fabc" });
    await api.authVerify("n1", "0xsig");
    expect(captured[0].url).toBe("/api/auth/verify");
    expect(captured[0].init.method).toBe("POST");
    expect(JSON.parse(captured[0].init.body as string)).toEqual({ nonce: "n1", signature: "0xsig" });
  });

  it("logout / createAgent / getStatus / postVerify / payload×2 / adminPending 路徑與方法", async () => {
    mockFetchJson(200, { ok: true });
    await api.logout();
    expect(captured[0]).toMatchObject({ url: "/api/auth/logout", init: expect.objectContaining({ method: "POST" }) });

    mockFetchJson(200, { agent_address: "0xagent" });
    await api.createAgent();
    expect(captured[0]).toMatchObject({ url: "/api/onboard/agent", init: expect.objectContaining({ method: "POST" }) });

    mockFetchJson(200, {
      address: "0xabc", account_id: "fabc", agent_address: null,
      agent_generated: false, builder_fee_approved: false,
      agent_approved: false, funded: false, state: "IN_PROGRESS",
    });
    const s = await api.getStatus();
    expect(captured[0].url).toBe("/api/onboard/status");
    expect(s.state).toBe("IN_PROGRESS");

    mockFetchJson(200, { state: "READY" });
    await api.postVerify();
    expect(captured[0]).toMatchObject({ url: "/api/onboard/verify", init: expect.objectContaining({ method: "POST" }) });

    mockFetchJson(200, { typed_data: { message: {} } });
    await api.getApproveAgentPayload(42161);
    expect(captured[0].url).toBe("/api/onboard/payload/approve-agent");
    expect(JSON.parse(captured[0].init.body as string)).toEqual({ chain_id: 42161 });

    mockFetchJson(200, { typed_data: { message: {} } });
    await api.getApproveBuilderFeePayload(1);
    expect(captured[0].url).toBe("/api/onboard/payload/approve-builder-fee");
    expect(JSON.parse(captured[0].init.body as string)).toEqual({ chain_id: 1 });

    mockFetchJson(200, { pending: [] });
    await api.getAdminPending();
    expect(captured[0].url).toBe("/api/admin/pending");
  });
});

describe("⭐ 結構性紅線：EIP-712 授權簽名絕不進後端（紅線 3）", () => {
  it("除 authVerify 外，所有端點的請求 body 不含 signature/r/s/v 欄位", async () => {
    const calls: Array<() => Promise<unknown>> = [
      () => api.logout(),
      () => api.createAgent(),
      () => api.getStatus(),
      () => api.postVerify(),
      () => api.getApproveAgentPayload(42161),
      () => api.getApproveBuilderFeePayload(42161),
      () => api.getAdminPending(),
      () => api.getMe(),
      () => api.getNonce("0xAbC0000000000000000000000000000000000001", 1),
    ];
    for (const call of calls) {
      mockFetchJson(200, { pending: [], typed_data: {}, nonce: "n", message: "m" });
      await call().catch(() => undefined);
      const body = captured[0].init.body;
      if (body != null) {
        const keys = Object.keys(JSON.parse(body as string));
        for (const banned of ["signature", "r", "s", "v"]) {
          expect(keys).not.toContain(banned);
        }
      }
    }
  });
});
