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

    mockFetchJson(200, { days: 7, customers: [], manifest_errors: [] });
    await api.getOpsCustomers(7);
    expect(captured[0].url).toBe("/api/ops/customers?days=7");

    mockFetchJson(200, { insufficient_accrued_history: true, manifest_errors: [] });
    await api.getOpsRevenue(0.01);
    expect(captured[0].url).toBe("/api/ops/revenue?threshold_pct=0.01");
  });

  it("billing 四端點：路徑、方法、回傳欄位（checkout 回 checkout_url、portal 回 url）", async () => {
    mockFetchJson(200, { billing_enabled: false, plans: [] });
    const plans = await api.getBillingPlans();
    expect(captured[0].url).toBe("/api/billing/plans");
    expect(captured[0].init.method).toBeUndefined(); // GET
    expect(plans.billing_enabled).toBe(false);

    mockFetchJson(200, { account_id: "fabc", status: "active", active: true });
    const st = await api.getBillingStatus();
    expect(captured[0].url).toBe("/api/billing/status");
    expect(st.status).toBe("active");

    // ⭐ checkout 的欄位名是 checkout_url，不是 url——兩者不同，型別與測試一起釘住
    mockFetchJson(200, { checkout_url: "https://checkout.stripe.test/s1" });
    const co = await api.postBillingCheckout();
    expect(captured[0]).toMatchObject({
      url: "/api/billing/checkout", init: expect.objectContaining({ method: "POST" }),
    });
    expect(co.checkout_url).toBe("https://checkout.stripe.test/s1");

    mockFetchJson(200, { url: "https://portal.stripe.test/p1" });
    const po = await api.postBillingPortal();
    expect(captured[0]).toMatchObject({
      url: "/api/billing/portal", init: expect.objectContaining({ method: "POST" }),
    });
    expect(po.url).toBe("https://portal.stripe.test/p1");
  });

  it("billing 未啟用 → 501 歸 kind=client 且保留 status（前端據此顯示「即將開放」）", async () => {
    mockFetchJson(501, { detail: "計費未啟用" });
    await expect(api.getBillingStatus()).rejects.toMatchObject({ kind: "client", status: 501 });
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
      () => api.getOpsCustomers(1),
      () => api.getOpsRevenue(0.01),
      () => api.getBillingPlans(),
      () => api.getBillingStatus(),
      () => api.postBillingCheckout(),
      () => api.postBillingPortal(),
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

describe("⭐ 反射式結構掃描：api.ts 每個匯出函式都不外洩簽名（防新函式漏測）", () => {
  // 上一個 describe 的 calls 陣列是手寫的：新增一個 api.ts 匯出函式時，容易忘記把它加進去，
  // 讓紅線測試悄悄失去涵蓋。這裡改用 Object.entries 反射列舉「當下實際存在」的匯出函式，
  // 對每一個都自動呼叫並驗證 body——手寫列表漏了誰，這裡都補上。
  const EXCLUDED = new Set(["ApiError", "authVerify"]); // ApiError 非函式呼叫端點；authVerify 是唯一合法帶簽名的端點（紅線 3 已知例外，別處測試已覆蓋）
  const reflected = Object.entries(api).filter(
    ([name, value]) => typeof value === "function" && !EXCLUDED.has(name),
  ) as Array<[string, (...args: unknown[]) => Promise<unknown>]>;

  it("反射函式數量與手寫清單一致——手寫清單不會因新函式而過時（保底斷言）", () => {
    // 對照上一個 describe 的 calls 陣列長度：兩者必須同步增減。
    const HAND_WRITTEN_LIST_LENGTH = 15;
    expect(reflected.length).toBe(HAND_WRITTEN_LIST_LENGTH);
  });

  function fakeArg(index: number): unknown {
    // 位置式合理假值：第 2 個參數（index 1）目前都是 chainId，其餘視為地址／字串類參數。
    // 值本身是否符合語意不重要——這裡只驗證 body 的「欄位名稱」，不驗證欄位值。
    return index === 1 ? 42161 : "0xAbC0000000000000000000000000000000000001";
  }

  it.each(reflected)("%s：request body 不含 signature/r/s/v 欄位", async (name, fn) => {
    mockFetchJson(200, {
      pending: [], typed_data: {}, nonce: "n", message: "m",
      address: "0xabc", account_id: "f", agent_address: "0xagent",
      state: "READY", ok: true,
    });
    const args = Array.from({ length: fn.length }, (_, i) => fakeArg(i));
    await fn(...args).catch(() => undefined);
    const body = captured[0]?.init.body;
    if (body != null) {
      const keys = Object.keys(JSON.parse(body as string));
      for (const banned of ["signature", "r", "s", "v"]) {
        expect(keys, `${name} body 含禁止欄位 ${banned}`).not.toContain(banned);
      }
    }
  });
});
