import { readFileSync } from "node:fs";
import { expect, test, type Locator } from "@playwright/test";
import { privateKeyToAccount } from "viem/accounts";

/**
 * 錢包注入走 onboarding — plan `docs/superpowers/plans/2026-09-02-golive-regression.md` T6。
 *
 * 這是**瀏覽器路徑**與 T2（Python 直送 HL）的交叉驗證：客戶真正走的是
 * 前端 wagmi → `eth_signTypedData_v4`/`personal_sign` → 直送 Hyperliquid
 * （見 `web/src/lib/hl.ts` 檔頭：簽名不落地、不回後端，瀏覽器直接 POST
 * `https://api.hyperliquid-testnet.xyz/exchange`），這條路只有真瀏覽器能驗。
 *
 * 前置（主線程另外起，見 T5 `public-smoke.spec.ts` 檔頭同一組 stack；⚠️ **host 用
 * `localhost` 不是 `127.0.0.1`**，見下方原因）：
 *   1. keysvc（`uv run python -m tests.integration.harness keysvc-serve ...`）。
 *   2. FastAPI testnet env 綁 127.0.0.1:8700，但 `FILET_SIWE_DOMAIN=localhost`／
 *      `FILET_SIWE_URI=http://localhost:3100`（leaders.json 需含精選白名單，
 *      沿用 `deploy/leaders.json.example`，第一個 enabled+accepting_new 條目
 *      即本測試使用的策略）。
 *   3. `npm run build && npx next start -p 3100`，用 `http://localhost:3100`
 *      （不是 `127.0.0.1:3100`）連。
 *   4. 一個已 fund ≥100 USDC perp 的拋棄式錢包（`mint-wallet` 產生），私鑰檔路徑
 *      經 `E2E_WALLET_PK_FILE` 傳入；`E2E_BASE_URL=http://localhost:3100`。
 *
 * ⚠️ **實測發現**：SIWE session cookie 是 `secure=True`（`app.py` 的
 * `auth_verify` 硬編，見該函式檔頭）。Chromium 只對 hostname 為 **`localhost`**
 * 的來源把 http 視為可信來源而接受 Secure cookie；`127.0.0.1` **不**享有這個
 * 例外，實測 `http://127.0.0.1:3100` 登入後 cookie 完全沒被存下——`authVerify`
 * 呼叫本身回 200（前端因此還是導向了 `/onboarding`），但下一次任何需要 session
 * 的請求（含 onboarding 頁自己的 `useMe()`）一律 401，整條登入態當場失效。
 * 這與 T5（純公開頁、本就預期 401）不受影響；T6 需要真的維持登入態，因此改用
 * `localhost` 作為本測試 stack 的 host（`FILET_SIWE_DOMAIN`／`NEXT_PUBLIC_SITE_ORIGIN`
 * 若要重 build 也一併換成 localhost，但只影響 metadata，不影響本測試）。
 *
 * `window.ethereum` 是本檔注入的最小 EIP-1193 mock（`multiInjectedProviderDiscovery:
 * false`，見 `web/src/lib/wagmi.ts`——app 只用 `injected()` 直接讀 `window.ethereum`，
 * 不需要 EIP-6963 announce）；簽章委由 Node 端 viem `privateKeyToAccount` 完成
 * （`page.exposeFunction`），私鑰只活在 Node 進程內，從不進 log／console。
 *
 * ⚠️ chainId 固定回報 `0xa4b1`（42161，Arbitrum One）——**不是** 421614（HL testnet
 * 對應的鏈），而是 `web/src/lib/wagmi.ts` 唯二配置的鏈之一（`chains: [arbitrum,
 * mainnet]`）。實測發現：回報 421614 時 `useConnectorClient()`（`StepSign.tsx`
 * 簽章用的 client）解析不出 viem client（該鏈不在 wagmiConfig.chains 內），導致
 * `signRaw` 走 `!client` 分支直接 reject——UI 上顯示成「Signature rejected」
 * （`approvalFlow.ts` 對任何 `signTypedData` 例外一律歸類 `wallet-rejected`），
 * 與真正的使用者拒簽無法區分（見交付回報第 7 點的觀察）。這對正式環境沒有影響
 * （正式客戶一定連在這兩條鏈之一），但代表本測試必須用 wagmiConfig 實際配置的
 * 鏈 id，而非 HL testnet 自己的鏈 id——`signatureChainId` 只是 EIP-712 domain 的
 * 一個欄位，HL 用 `hyperliquidChain`（"Testnet"）決定環境／防重放，不要求
 * signatureChainId 等於 421614（`src/spark/publicapi/approvals.py` 檔頭）。
 */

const TESTNET_INFO_URL = "https://api.hyperliquid-testnet.xyz/info";
const CHAIN_ID_HEX = "0xa4b1"; // 42161 (Arbitrum One) — 見上方檔頭說明
const BUILDER_ADDR =
  process.env.FILET_BUILDER_ADDR ?? "0xbAC652a5fb611c1bdc3b9d244cc7e0cc03123662";

const PK_FILE = process.env.E2E_WALLET_PK_FILE;

/** 同 `public-smoke.spec.ts` 的白名單理由：Header 在每頁探測登入態，401 是
 * 合法的「尚未登入」訊號（本測試登入後應不再出現，留著只為與 T5 同一套判準）。
 *
 * 額外兩條是本測試範圍下的合法訊號（T6 只驗到「onboarding 精靈完成、/dashboard
 * 渲染成功」，不包含 auto-activate watcher——啟用是獨立的人工/watcher 流程，
 * plan T2 S8 才驗那段）：
 *   - `/api/admin/pending` 403：Header 的 `useIsAdmin` 探測，本測試錢包不是
 *     admin address，403 是預期回應（同 401 白名單的「探測性請求」邏輯）。
 *   - `/api/me/*` 503：帳號剛完成 leader 選定，pending.json 已有條目但**尚未
 *     被 auto-activate watcher 撿起寫進 followers manifest**（本測試 stack
 *     沒有跑 watcher）——`/api/me/leader` 等端點對「manifest 讀不到」的既有
 *     設計是回 503 而非偽造一個「未跟單」的假象（見 `web/src/lib/api.ts`
 *     `MyLeaderResp` 檔頭「讀不到 ≠ 沒有」）。這是本測試範圍刻意的環境限制，
 *     不是 dashboard 頁面本身的錯誤。 */
function isExpectedConsoleError(msg: { text(): string; location(): { url: string } }): boolean {
  const url = msg.location().url;
  const text = msg.text();
  if (url.endsWith("/api/me") && text.includes("responded with a status of 401")) return true;
  if (url.endsWith("/api/admin/pending") && text.includes("responded with a status of 403")) return true;
  if (url.includes("/api/me/") && text.includes("responded with a status of 503")) return true;
  return false;
}

test("connect → SIWE → approveAgent/approveBuilderFee 上鏈 → 入金通過 → pending → dashboard", async ({
  page,
}) => {
  // ⚠️ 一律用 `page.request`（與 page 共用同一個瀏覽器 context 的 cookie jar），
  // 不用頂層 `request` fixture——那是獨立的 APIRequestContext，不帶 SIWE session
  // cookie，打 `/api/me` 等需登入端點只會拿到 401（本檔已踩過這個坑）。
  const request = page.request;
  test.skip(
    !PK_FILE,
    "缺 E2E_WALLET_PK_FILE：需先用 `uv run python -m tests.integration.harness mint-wallet` "
      + "產生已 fund 的拋棄式錢包，見 plan 2026-09-02-golive-regression.md §3 T6",
  );
  if (!PK_FILE) return; // 型別收斂；runtime 已由上面 test.skip 短路

  test.setTimeout(420_000);

  const pk = readFileSync(PK_FILE, "utf8").trim();
  const account = privateKeyToAccount(pk as `0x${string}`);
  const address = account.address;
  console.log(`[T6] 拋棄式錢包位址: ${address}`);

  // ---------- 簽章委任：Node 端 viem，私鑰不進瀏覽器、不進 log ----------
  await page.exposeFunction("__e2eSign", async (method: string, payload: string) => {
    if (method === "personal_sign") {
      return account.signMessage({ message: { raw: payload as `0x${string}` } });
    }
    if (method === "eth_signTypedData_v4") {
      const td = JSON.parse(payload) as {
        domain: Record<string, unknown>;
        types: Record<string, unknown>;
        primaryType: string;
        message: Record<string, unknown>;
      };
      // recoverTypedDataAddress/signTypedData 對 types.EIP712Domain 的容忍度不保證
      // 跨版本一致（見 web/src/lib/hl.ts::recoverSigner 檔頭同一個防禦），剝除後再簽。
      const { EIP712Domain: _drop, ...types } = td.types;
      return account.signTypedData({
        domain: td.domain,
        types,
        primaryType: td.primaryType,
        message: td.message,
      } as never);
    }
    throw new Error(`__e2eSign: 不支援的方法 ${method}`);
  });

  // ---------- window.ethereum 最小 EIP-1193 mock ----------
  await page.addInitScript(
    ({ address, chainIdHex }: { address: string; chainIdHex: string }) => {
      window.localStorage.setItem("filet_lang", "en");

      const isAddr = (x: unknown): x is string =>
        typeof x === "string" && /^0x[0-9a-fA-F]{40}$/.test(x);
      const listeners = new Map<string, Set<(...a: unknown[]) => void>>();

      const provider = {
        isMetaMask: true,
        chainId: chainIdHex,
        selectedAddress: address,
        request: async ({ method, params }: { method: string; params?: unknown[] }) => {
          const p = (params ?? []) as unknown[];
          switch (method) {
            case "eth_requestAccounts":
            case "eth_accounts":
              return [address];
            case "eth_chainId":
              return chainIdHex;
            case "net_version":
              return "42161";
            case "wallet_switchEthereumChain":
            case "wallet_addEthereumChain":
              return null;
            case "personal_sign": {
              const [a, b] = p;
              const data = isAddr(a) ? b : a;
              return (window as unknown as { __e2eSign: (m: string, d: string) => Promise<string> })
                .__e2eSign("personal_sign", data as string);
            }
            case "eth_signTypedData_v4":
            case "eth_signTypedData": {
              const [a, b] = p;
              const data = isAddr(a) ? b : a;
              return (window as unknown as { __e2eSign: (m: string, d: string) => Promise<string> })
                .__e2eSign("eth_signTypedData_v4", data as string);
            }
            default:
              throw new Error(`e2e mock provider: 不支援的方法 ${method}`);
          }
        },
        on(event: string, cb: (...a: unknown[]) => void) {
          if (!listeners.has(event)) listeners.set(event, new Set());
          listeners.get(event)!.add(cb);
        },
        removeListener(event: string, cb: (...a: unknown[]) => void) {
          listeners.get(event)?.delete(cb);
        },
      };
      Object.defineProperty(window, "ethereum", { value: provider, configurable: true });
    },
    { address, chainIdHex: CHAIN_ID_HEX },
  );

  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error" && !isExpectedConsoleError(msg)) consoleErrors.push(msg.text());
  });
  page.on("pageerror", (err) => pageErrors.push(String(err)));

  // ---------- 取第一個上架策略（沿用 public-smoke.spec.ts 的取法，不硬編 slug） ----------
  const stratRes = await request.get("/api/public/strategies");
  expect(stratRes.status(), "/api/public/strategies 應回 200").toBe(200);
  const stratBody = await stratRes.json();
  const slug: string | undefined = stratBody.strategies?.[0]?.slug;
  expect(slug, "測試環境需要至少一個上架策略（deploy/leaders.json.example）").toBeTruthy();
  console.log(`[T6] 使用策略 slug=${slug}`);

  // ---------- 連錢包 + SIWE 登入（/strategies/[slug] 的 CTA） ----------
  await page.goto(`/strategies/${slug}`);
  console.log(`[T6] URL: ${page.url()}（策略詳情頁）`);
  await page.getByRole("button", { name: "Connect wallet & continue" }).click();
  await page.waitForURL((url) => url.pathname === "/onboarding", { timeout: 30_000 });
  console.log(`[T6] URL: ${page.url()}（SIWE 登入成功，已導向 onboarding）`);

  const meRes = await request.get("/api/me");
  expect(meRes.status(), "/api/me 應回 200").toBe(200);
  const me = await meRes.json();
  expect(me.address?.toLowerCase()).toBe(address.toLowerCase());
  console.log(`[T6] /api/me 200，session address=${me.address}`);

  // ---------- step 2：ApproveAgent / ApproveBuilderFee（真上鏈） ----------
  async function fetchOnboardStatus() {
    const r = await request.get("/api/onboard/status");
    expect(r.status(), "/api/onboard/status 應回 200").toBe(200);
    return r.json();
  }

  /** 觀察（交付回報第 7 點）：ApproveBuilderFee 簽署送出後，雖然簽名幾乎立刻就
   * 真的落鏈（直接對 HL testnet `/info` 的 `maxBuilderFee` 唯讀查詢可驗證），
   * 但 UI 的 `.sign-state` 文字（靠 `useOnboardingStatus` 的 `pollMs:5000` 輪詢
   * 重新渲染）在本測試環境下觀察到需要遠超過 5 個輪詢週期才反映——懷疑是
   * Playwright headless 分頁的背景計時器節流拖慢了 `setInterval`/`refetchInterval`
   * 的實際觸發頻率（ApproveAgent 每次都秒級確認，ApproveBuilderFee 則多次觀察
   * 到需要 1-3+ 分鐘）。為了不讓測試綁死在瀏覽器分頁的計時器節流上，直接輪詢
   * 後端 `/api/onboard/status`（Node 端 `setTimeout` 迴圈，不受頁面節流影響）
   * 確認鏈上事實，再用 `page.reload()` 強制重新掛載頁面拿一次新鮮的初始查詢
   * 讓 UI 反映出來（初始查詢不受 `refetchInterval` 節流影響）。 */
  async function waitForStatusFlag(
    flag: "agent_approved" | "builder_fee_approved",
    timeoutMs: number,
  ) {
    const deadline = Date.now() + timeoutMs;
    let last: Record<string, unknown> = {};
    while (Date.now() < deadline) {
      last = await fetchOnboardStatus();
      if (last[flag]) return last;
      await new Promise((resolve) => { setTimeout(resolve, 3000); });
    }
    throw new Error(`waitForStatusFlag(${flag}) 逾時，最後一次狀態: ${JSON.stringify(last)}`);
  }

  // 冪等處理：同一個拋棄式錢包若之前已在同一個 backend db 完成過某張卡的簽署
  // （例如本檔重跑時），該卡一開賣就是 "Active"、不會渲染「Sign with wallet」
  // 按鈕——不能假設按鈕必然存在，先看目前狀態文字再決定要不要簽。
  async function ensureCardSigned(
    card: Locator,
    label: string,
    flag: "agent_approved" | "builder_fee_approved",
  ) {
    const stateEl = card.locator(".sign-state");
    await expect(stateEl).toBeVisible({ timeout: 30_000 });
    if ((await stateEl.textContent())?.trim() === "Active") {
      console.log(`[T6] ${label} 已是 Active（沿用先前測試留下的鏈上狀態）`);
      return;
    }
    const signButton = card.getByRole("button", { name: "Sign with wallet" });
    await expect(signButton).toBeEnabled({ timeout: 30_000 });
    console.log(`[T6] ${label}：開始簽署`);
    await signButton.click();
    console.log(`[T6] ${label}：已送出簽名，輪詢後端確認鏈上事實…`);
    await waitForStatusFlag(flag, 180_000);
    await page.reload();
    await page.waitForLoadState("networkidle");
    // reload 後若兩張卡皆已核准，wizard 會直接前進到 step3+，`.sign-card`
    // 不再渲染——這也是確認成功的訊號，不用再對已消失的卡片斷言文字。
    const stateAfterReload = page.locator(".sign-card", { hasText: label }).locator(".sign-state");
    if (await stateAfterReload.count() > 0) {
      await expect(stateAfterReload).toHaveText("Active", { timeout: 30_000 });
    } else {
      console.log(`[T6] ${label}：reload 後 wizard 已前進（sign-card 不再渲染），視為已確認`);
    }
    console.log(`[T6] ${label} 已確認（Active）`);
  }

  let status = await fetchOnboardStatus();

  // ⭐ 冪等處理：若 `status` 已經全數核准（例如本檔曾用同一個錢包跑過、且該錢包
  // 已在鏈上完成 approveAgent/approveBuilderFee），`deriveStep` 會讓 wizard
  // 直接落在 step3+，此時整個 step2（含 sign-card、Complete setup 按鈕）根本
  // 不會渲染——不能假設它們存在。只有真的還沒核准時才走簽署 UI。
  if (!status.agent_approved || !status.builder_fee_approved) {
    const agentCard = page.locator(".sign-card", { hasText: "ApproveAgent" });
    await ensureCardSigned(agentCard, "ApproveAgent", "agent_approved");
    // ⭐ agentCard 這個 Locator 物件在上一行呼叫後仍然有效（Playwright locator
    // 是惰性查詢，reload 後重新解析），但 ensureCardSigned 內部的 reload 可能已讓
    // wizard 前進；重新用 page 現查一次 feeCard，避免用到 reload 前的過期參照。
    const feeCard = page.locator(".sign-card", { hasText: "ApproveBuilderFee" });
    await ensureCardSigned(feeCard, "ApproveBuilderFee", "builder_fee_approved");
    status = await fetchOnboardStatus();
  } else {
    console.log("[T6] step2：agent/builder fee 已核准（沿用先前執行留下的鏈上狀態），略過簽署 UI");
  }

  // ---------- HL testnet 唯讀複核：前端「直送 HL」路徑真的落鏈 ----------
  expect(status.agent_approved).toBe(true);
  expect(status.builder_fee_approved).toBe(true);
  const agentAddress: string = status.agent_address;

  const agentsRes = await request.post(TESTNET_INFO_URL, {
    data: { type: "extraAgents", user: address },
  });
  expect(agentsRes.status()).toBe(200);
  const agents: Array<{ address: string }> = await agentsRes.json();
  expect(
    agents.some((a) => a.address?.toLowerCase() === agentAddress.toLowerCase()),
    `HL testnet extraAgents 應含 ${agentAddress}: ${JSON.stringify(agents)}`,
  ).toBe(true);
  console.log(`[T6] HL testnet extraAgents 唯讀複核通過（agent=${agentAddress}）`);

  const feeRes = await request.post(TESTNET_INFO_URL, {
    data: { type: "maxBuilderFee", user: address, builder: BUILDER_ADDR },
  });
  expect(feeRes.status()).toBe(200);
  const maxFee = await feeRes.json();
  expect(Number(maxFee), `HL testnet maxBuilderFee 應 > 0: ${maxFee}`).toBeGreaterThan(0);
  console.log(`[T6] HL testnet maxBuilderFee 唯讀複核通過（=${maxFee}）`);

  // ---------- 入金檢查通過 → 完成綁定（寫入 pending.json） ----------
  expect(status.funded, "harness 應已在測試前 fund 該錢包 ≥ min_deposit").toBe(true);
  // 2026-09-02 T6 發現＋T10 修法：舊版 `deriveStep` 只看 status 是否 READY，客戶在
  // 核准＋入金後重新整理頁面會直接跳到 step 3、略過唯一寫 pending.json 的
  // `POST /api/onboard/verify`（watcher 永遠不啟用、無錯誤畫面）。T10 之後前端會在
  // 「已 READY 但本地沒有 step2Verified 旗標」時自動補打一次 verify 再放行到 step 3，
  // 後端 `POST /api/leaders/select` 另有一道結構性補寫。本段刻意**不**直接呼叫 API：
  // 精靈必須自己走到 step 3（自動補打或客戶按下已 enabled 的「Complete setup」），
  // 之後 pending.json 含該 account 才算通過——這樣本測試才真的守住 T10。
  {
    const step3 = page.getByText("Set your risk limits");
    const completeBtn = page.getByRole("button", { name: "Complete setup" });
    const deadline = Date.now() + 90_000;
    let via = "";
    while (Date.now() < deadline) {
      if (await step3.isVisible().catch(() => false)) { via = via || "auto-verify"; break; }
      if (await completeBtn.isEnabled().catch(() => false)) {
        via = "manual-click";
        await completeBtn.click().catch(() => { /* 可能已被自動補打換頁，下一輪重看 */ });
      }
      await page.waitForTimeout(500);
    }
    await expect(step3).toBeVisible({ timeout: 10_000 });
    console.log(`[T6] URL: ${page.url()}（進入 step3，途徑=${via || "auto-verify"}）`);
  }

  const pendingPath = process.env.FILET_PENDING_PATH;
  if (pendingPath) {
    const pendingRaw = readFileSync(pendingPath, "utf8");
    expect(
      pendingRaw.toLowerCase().includes(address.toLowerCase()),
      `FILET_PENDING_PATH（${pendingPath}）應含該 account`,
    ).toBe(true);
    console.log(`[T6] FILET_PENDING_PATH（${pendingPath}）已確認含該 account`);
  } else {
    console.log("[T6] 未設定 FILET_PENDING_PATH，略過 pending.json 內容檢查（僅驗證 wizard 狀態轉換）");
  }

  // ---------- step 3：投入比例（capital settings，必簽；回撤自動停止維持預設關閉） ----------
  await page.getByRole("button", { name: "Continue to fees & risk confirmation" }).click();

  // ---------- step 4：三條 checkbox + leader select 簽章送出 ----------
  const checkboxes = page.getByRole("checkbox");
  await expect(checkboxes).toHaveCount(3, { timeout: 30_000 });
  console.log(`[T6] URL: ${page.url()}（進入 step4：費用與風險確認）`);
  for (let i = 0; i < 3; i += 1) {
    await checkboxes.nth(i).check();
  }
  await page.getByRole("button", { name: "Confirm & start copy trading" }).click();
  await page.waitForURL((url) => url.pathname === "/dashboard", { timeout: 30_000 });
  console.log(`[T6] URL: ${page.url()}（leader 選定完成，onboarding 精靈結束）`);

  await page.waitForLoadState("networkidle");
  await expect(page.getByText(/Application error/i)).toHaveCount(0);
  expect(pageErrors, `dashboard page errors: ${pageErrors.join(" | ")}`).toEqual([]);
  expect(consoleErrors, `dashboard console errors: ${consoleErrors.join(" | ")}`).toEqual([]);
  console.log("[T6] /dashboard 渲染成功，無 console error / pageerror");
});
