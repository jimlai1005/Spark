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
    await api.getOpsCustomers({ days: 7 });
    expect(captured[0].url).toBe("/api/ops/customers?days=7");

    // ⭐ 同基準模式：與 /api/ops/revenue 共用後端的窗口推導函式。days 與 window 互斥，
    // 型別（OpsCustomersQuery 的 optional-never）已擋掉同時給——這裡釘住送出的 query。
    mockFetchJson(200, { window: "accrued", basis_unknown: false, customers: [], manifest_errors: [] });
    await api.getOpsCustomers({ window: "accrued" });
    expect(captured[0].url).toBe("/api/ops/customers?window=accrued");

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

/**
 * 「我目前跟誰」（對照 app.py 的 /api/me/leader）。
 * ⭐ 端點**沒有任何 account 參數**——account_id 由 session 衍生，結構上不可能查到
 * 別人的（後端 docstring 明寫）。這裡把「不送參數」當成契約釘住：哪天前端加了一個
 * `?account_id=`，那條結構保證就從前端這側被打開了。
 */
describe("getMyLeader（我目前跟隨的 leader）", () => {
  it("GET /api/me/leader（零查詢參數），回傳四態 status 與 leader 位址", async () => {
    mockFetchJson(200, {
      account_id: "fabc",
      status: "following",
      leader_address: "0x1111111111111111111111111111111111111111",
      leader_name: "Alpha",
      pending_change: null,
      note: "這是引擎目前為你跟隨的 leader。",
    });
    const r = await api.getMyLeader();
    expect(captured[0].url).toBe("/api/me/leader");
    expect(captured[0].init.method).toBeUndefined(); // GET
    expect(captured[0].init.credentials).toBe("include");
    expect(r.status).toBe("following");
    expect(r.leader_address).toBe("0x1111111111111111111111111111111111111111");
    expect(r.leader_name).toBe("Alpha");
    expect(r.pending_change).toBeNull();
  });

  it("pending_change 原樣帶出（leader_address／issued_at／effective／note）", async () => {
    mockFetchJson(200, {
      account_id: "fabc",
      status: "following",
      leader_address: "0x1111111111111111111111111111111111111111",
      leader_name: "Alpha",
      pending_change: {
        leader_address: "0x2222222222222222222222222222222222222222",
        issued_at: "2026-07-27T00:00:00Z",
        effective: "next_engine_cycle",
        note: "你已簽署換 leader，尚未生效。",
      },
      note: "這是引擎目前為你跟隨的 leader。",
    });
    const r = await api.getMyLeader();
    expect(r.pending_change).toMatchObject({
      leader_address: "0x2222222222222222222222222222222222222222",
      effective: "next_engine_cycle",
    });
  });

  it("manifest 讀不到 → 503 歸 kind=upstream（不得被讀成「你沒在跟單」）", async () => {
    mockFetchJson(503, { detail: "跟隨狀態暫時不可用，請稍後重試" });
    await expect(api.getMyLeader()).rejects.toMatchObject({ kind: "upstream", status: 503 });
  });
});

/**
 * 自訂 leader 准入預覽（2026-07-27 spec；對照 app.py 的 /api/leaders/preview）。
 * ⭐ 這是全 app 第一個 detail 為**物件**（`{reason, message}`）的端點：reason 是
 * 機器可判的分類碼，message 是人話。分類在邊界完成（工程原則 5）——request()
 * 把兩者拆進 ApiError 的 `reason`／`detail`，呼叫端不用自己去戳 detail 的形狀。
 */
describe("getLeaderPreview（自訂 leader 准入預覽）", () => {
  it("GET /api/leaders/preview?leader_address=，回傳預覽欄位", async () => {
    mockFetchJson(200, {
      address: "0x2222222222222222222222222222222222222222",
      exists: true, account_value: "5123.45", position_count: 3, already_listed: false,
      accepting_new: true,
    });
    const r = await api.getLeaderPreview("0x2222222222222222222222222222222222222222");
    expect(captured[0].url).toBe(
      "/api/leaders/preview?leader_address=0x2222222222222222222222222222222222222222",
    );
    expect(captured[0].init.method).toBeUndefined(); // GET
    // account_value 是 Decimal → string（落地慣例），不是 number
    expect(r.account_value).toBe("5123.45");
    expect(r.position_count).toBe(3);
    expect(r.already_listed).toBe(false);
    expect(r.accepting_new).toBe(true);
  });

  it("⭐ 4xx 的 detail 是 {reason, message} 物件 → ApiError.reason 帶分類碼、detail 帶人話", async () => {
    mockFetchJson(400, {
      detail: { reason: "self_follow", message: "不能跟單自己的登入位址" },
    });
    await expect(api.getLeaderPreview("0xAbC0000000000000000000000000000000000001"))
      .rejects.toMatchObject({
        kind: "client", status: 400,
        reason: "self_follow",
        detail: "不能跟單自己的登入位址",
        // Error.message 用人話那一份，不是 "[object Object]"
        message: "不能跟單自己的登入位址",
      });
  });

  it("4xx（404）帶 reason 一律 kind=client、reason 原樣穿透（不歸 upstream）", async () => {
    // 通用傳輸不變式：任何 4xx＋reason 都歸 client、reason 原樣帶出——與具體業務
    // reason 碼無關（此處用一個佔位 reason；not_found 自 2026-07-27 裁決已不再回傳）。
    mockFetchJson(404, {
      detail: { reason: "some_reason", message: "任意 4xx 訊息" },
    });
    await expect(api.getLeaderPreview("0x3333333333333333333333333333333333333333"))
      .rejects.toMatchObject({ kind: "client", status: 404, reason: "some_reason" });
  });

  it("既有端點的字串 detail 行為不變：reason 為 undefined", async () => {
    mockFetchJson(400, { detail: "該 leader 目前不可選擇" });
    await expect(api.getLeaderSelectMessage("0x1111111111111111111111111111111111111111"))
      .rejects.toMatchObject({
        kind: "client", detail: "該 leader 目前不可選擇", reason: undefined,
      });
  });
});

describe("⭐ 結構性紅線：EIP-712 授權簽名絕不進後端（紅線 3）", () => {
  // 已知合法例外（三支，皆為 EIP-191 personal_sign、原文皆由伺服器產生）：
  //   authVerify（SIWE 登入）、postLeaderSelect（換 leader 授權）、
  //   postCapitalSettings（資金設定授權）——後兩支的後端契約都要求帶簽名。
  // ⭐ 例外不是豁免：後兩支各有專屬測試釘住「body 欄位集合恰好是契約欄位」
  //   （見下方兩個 describe），比這裡的黑名單掃描更嚴——r/s/v 與任何多餘欄位都會被抓到。
  //   HL 的 EIP-712 授權簽名仍然結構上沒有進後端的路（那條走 lib/hl.ts 直送 HL）。
  it("除三支帶簽名的例外外，所有端點的請求 body 不含 signature/r/s/v 欄位", async () => {
    const calls: Array<() => Promise<unknown>> = [
      () => api.logout(),
      () => api.createAgent(),
      () => api.getStatus(),
      () => api.postVerify(),
      () => api.getApproveAgentPayload(42161),
      () => api.getApproveBuilderFeePayload(42161),
      () => api.getAdminPending(),
      () => api.getMe(),
      () => api.getMyLeader(),
      () => api.getNonce("0xAbC0000000000000000000000000000000000001", 1),
      () => api.getOpsCustomers({ days: 1 }),
      () => api.getOpsRevenue(0.01),
      () => api.getOpsSubscriptions(),
      () => api.getOpsTradeQuality({ days: 1 }),
      () => api.getOpsHealth(),
      () => api.getBillingPlans(),
      () => api.getBillingStatus(),
      () => api.postBillingCheckout(),
      () => api.postBillingPortal(),
      () => api.getLeaders(),
      () => api.getLeaderSelectMessage("0x1111111111111111111111111111111111111111"),
      () => api.getLeaderPreview("0x2222222222222222222222222222222222222222"),
      () => api.getCapitalSettingsMessage("10000.00", "0.2000"),
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
  // ApiError 非函式呼叫端點；authVerify、postLeaderSelect 與 postCapitalSettings 是
  // **僅有的三個**合法帶簽名的端點（皆 EIP-191、原文皆由伺服器產生），各自有專屬測試
  // 覆蓋——後兩者的專屬測試是「欄位集合恰好等於契約欄位」的白名單，比本掃描的黑名單
  // 更嚴（見檔案末段兩個 describe）。
  const EXCLUDED = new Set([
    "ApiError", "authVerify", "postLeaderSelect", "postCapitalSettings",
  ]);
  const reflected = Object.entries(api).filter(
    ([name, value]) => typeof value === "function" && !EXCLUDED.has(name),
  ) as Array<[string, (...args: unknown[]) => Promise<unknown>]>;

  it("反射函式數量與手寫清單一致——手寫清單不會因新函式而過時（保底斷言）", () => {
    // 對照上一個 describe 的 calls 陣列長度：兩者必須同步增減。
    // 2026-07-19：+2（getOpsTradeQuality、getOpsHealth，/ops 兩個新面板）
    // 2026-07-27：+1（getLeaderPreview，自訂 leader 准入預覽）
    // 2026-07-27：+1（getMyLeader，「我目前跟誰」——/leaders 頁的現況揭露）
    const HAND_WRITTEN_LIST_LENGTH = 23;
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

/**
 * postLeaderSelect 是紅線 3 的第二個已知例外（後端契約要求帶簽名）。例外必須比通則
 * **更嚴**：這裡不是黑名單（缺什麼就漏什麼），而是白名單——body 欄位集合必須恰好等於
 * 契約六欄，多一個欄位就失敗（HL 的 r/s/v、私鑰材料、任何前端自作主張塞進去的東西）。
 */
describe("⭐ postLeaderSelect：帶簽名的例外，body 欄位集合精確釘死", () => {
  const PAYLOAD = {
    message: "Filet: change copy-trading leader\n\nAccount: fzzz\nNonce: n-1",
    nonce: "n-1",
    issued_at: "2026-07-19T00:00:00Z",
    leader_address: "0x1111111111111111111111111111111111111111",
    account_id: "fzzz",
  };
  const SIG = `0x${"ab".repeat(65)}`;

  it("欄位集合恰為契約六欄（白名單），且不含 r/s/v 或任何多餘欄位", async () => {
    mockFetchJson(200, { ok: true });
    await api.postLeaderSelect(PAYLOAD, SIG);

    const body = JSON.parse(captured[0].init.body as string);
    expect(Object.keys(body).sort()).toEqual(
      ["account_id", "issued_at", "leader_address", "message", "nonce", "signature"],
    );
    expect(captured[0]).toMatchObject({
      url: "/api/leaders/select", init: expect.objectContaining({ method: "POST" }),
    });
  });

  it("⭐ 原文原樣回送，且各欄位一律取自同一包 payload（不從別處拼）", async () => {
    mockFetchJson(200, { ok: true });
    await api.postLeaderSelect(PAYLOAD, SIG);

    const body = JSON.parse(captured[0].init.body as string);
    // 逐位元組相同：換行、大小寫、欄位順序都不得被前端重組（後端會重建訊息驗簽）
    expect(body.message).toBe(PAYLOAD.message);
    expect(body.account_id).toBe(PAYLOAD.account_id);
    expect(body.leader_address).toBe(PAYLOAD.leader_address);
    expect(body.nonce).toBe(PAYLOAD.nonce);
    expect(body.issued_at).toBe(PAYLOAD.issued_at);
    expect(body.signature).toBe(SIG);
  });
});

/**
 * postCapitalSettings 是紅線 3 的第三個已知例外（後端契約要求帶簽名）。同 postLeaderSelect：
 * 例外必須比通則**更嚴**——白名單釘死欄位集合，多一個欄位就失敗。
 *
 * ⭐ 欄位是**七欄**而不是記錄格式的八欄：`action` 刻意不由前端送出（後端
 * build_capital_settings_record 寫死）。讓客戶端指定動作類型，等於把「換 leader 授權
 * 不能被當成資金設定授權」的域分隔交還給請求內容，而請求內容整份都在攻擊者的控制
 * 範圍內（見 filet/capital_settings.py 檔頭的域分隔論證）。多送 action 這件事必須失敗。
 */
describe("⭐ postCapitalSettings：帶簽名的例外，body 欄位集合精確釘死", () => {
  const PAYLOAD = {
    message:
      "Filet: update copy-trading capital allocation\n\nAccount: fzzz\n" +
      "Allocated Capital: 10000.00 USDC\nCapital Utilization: 0.2000\nNonce: n-1",
    nonce: "n-1",
    issued_at: "2026-07-19T00:00:00Z",
    account_id: "fzzz",
    allocated_capital: "10000.00",
    capital_utilization: "0.2000",
  };
  const SIG = `0x${"ab".repeat(65)}`;

  it("欄位集合恰為契約七欄（白名單），且不含 action、r/s/v 或任何多餘欄位", async () => {
    mockFetchJson(200, { ok: true });
    await api.postCapitalSettings(PAYLOAD, SIG);

    const body = JSON.parse(captured[0].init.body as string);
    expect(Object.keys(body).sort()).toEqual([
      "account_id", "allocated_capital", "capital_utilization",
      "issued_at", "message", "nonce", "signature",
    ]);
    expect(captured[0]).toMatchObject({
      url: "/api/me/capital", init: expect.objectContaining({ method: "POST" }),
    });
  });

  it("⭐ 原文與兩個數值原樣回送，一律取自同一包 payload（不從別處拼）", async () => {
    mockFetchJson(200, { ok: true });
    await api.postCapitalSettings(PAYLOAD, SIG);

    const body = JSON.parse(captured[0].init.body as string);
    // 逐位元組相同：後端會用這些欄位**重建**訊息再 recover，前端重組就會出現
    // 「簽的是 A、送的是 B」的縫（症狀是本人簽的卻一直被拒，而兩邊 log 都正常）。
    expect(body.message).toBe(PAYLOAD.message);
    expect(body.account_id).toBe(PAYLOAD.account_id);
    expect(body.allocated_capital).toBe(PAYLOAD.allocated_capital);
    expect(body.capital_utilization).toBe(PAYLOAD.capital_utilization);
    expect(body.nonce).toBe(PAYLOAD.nonce);
    expect(body.issued_at).toBe(PAYLOAD.issued_at);
    expect(body.signature).toBe(SIG);
  });

  it("getCapitalSettingsMessage：GET /api/me/capital/message?allocated_capital=&capital_utilization=", async () => {
    mockFetchJson(200, { message: "m", nonce: "n" });
    await api.getCapitalSettingsMessage("10000.00", "0.2000");

    expect(captured[0].url).toBe(
      "/api/me/capital/message?allocated_capital=10000.00&capital_utilization=0.2000",
    );
    expect(captured[0].init.method).toBeUndefined(); // GET
  });
});
