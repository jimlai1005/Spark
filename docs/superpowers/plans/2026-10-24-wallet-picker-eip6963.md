# 多錢包選單（EIP-6963）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 桌面瀏覽器同時裝多個擴充套件錢包（MetaMask、Rabby、OKX…）時，Filet 的「連接錢包」讓使用者自己選要用哪一個；只裝一個時行為不變（直接連）。手機／WalletConnect **不在本 plan 範圍**（使用者 2026-10-24 裁決暫緩）。

**Architecture:** 開啟 wagmi 內建的 EIP-6963 發現（`multiInjectedProviderDiscovery: true`），connector 清單會自動多出每個宣告自己的擴充套件錢包（`id` = rdns，附 `name`/`icon`）；保留原本的 `injected()` 當「舊式只注入 `window.ethereum` 的錢包」後備。目前 Header、advanced、strategies/[slug]、traders/[address]、ReconnectGate 五處各自複製的 `connectors[0]` → `connectAsync` → `loginWithSiwe` 流程，收斂成一個 hook `useWalletLogin` ＋ 一個純函式模組 `walletChoice.ts` ＋ 一個展示元件 `WalletPicker`。選單只在使用者按下 CTA 之後才渲染，避免 SSR／hydration 不一致（server 端 connector 清單只有 `injected`，client 端才有發現到的錢包）。

**Tech Stack:** Next.js 15 (app router, "use client")、wagmi 2.19.5、viem 2.55.2、vitest 2 + @testing-library/react、既有 `.modal-overlay/.modal-card` 樣式。

**不動的東西：** `lib/siwe.ts`（SIWE 編排不變）、後端 `/api/auth/*`、`FollowPanel`／`NotLoggedInGateCard` 的按鈕 JSX（只換它們拿到的 `phase`/`error`/`onCta` 來源）、`wagmi.ts` 檔頭關於 barrel import 的禁令（**仍然只能 `import { injected } from "wagmi"`**，不得引入 `wagmi/connectors`）。

**派工標記說明：** `@sdd` → `impl-worker`（haiku）；`@inline` → `builder`（sonnet）。每個 task 完成後主線程親跑該 task 的驗收指令。

**共同前置（每個 task 的指令都要先做）：**
```bash
export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH"
cd /Users/jim/projects/spark/web
```
基線：`npm test` 全綠（開工前先跑一次確認）。

---

## 關鍵事實（實作者不需要再去查）

- wagmi `createConfig` 開 `multiInjectedProviderDiscovery: true` 後，`useConnect().connectors` ＝ `[...設定檔的 connectors, ...EIP-6963 發現到的]`，每個發現到的 connector 由 `injected({ target: { ...info, id: info.rdns, provider } })` 建立，所以 `connector.id` 是 rdns（如 `io.metamask`、`io.rabby`），`connector.name`／`connector.icon` 來自錢包自己宣告（`node_modules/@wagmi/core/dist/esm/createConfig.js:64-68`）。清單是 reactive 的（`useConnectors` 用 `useSyncExternalStore` 訂閱），錢包晚一點宣告也會補進來。
- 設定檔裡那個不帶 target 的 `injected()`，`id` 固定是 `"injected"`、`name` 是 `"Injected"`、provider 是 `window.ethereum`（`connectors/injected.js:25-31`）。沒有 `window.ethereum` 時 `connect()` 丟 `ProviderNotFoundError`，其 `name` 屬性為 `"ProviderNotFoundError"`（`errors/connector.js:2-10`）。
- 使用者拒簽／拒連：viem `UserRejectedRequestError`（`name === "UserRejectedRequestError"`，或 `code === 4001`）。
- 既有 vitest 的 wagmi mock 形狀全部是 `useConnect: () => ({ connectAsync, connectors: [{ id: "injected" }] })`（Header.test.tsx:23-27、advanced/page.test.tsx:33-39、strategies/[slug]/page.test.tsx:25-29、traders/[address]/page.test.tsx:33-37、strategies/[slug]/enNoCjk.test.tsx:19-23、pageStates.test.tsx:36-40）。jsdom 沒有 `window.ethereum`。**本 plan 的候選規則刻意讓「清單只有 injected」時直接走 injected**，所以這些測試不必改 mock 就能維持綠燈。
- `copy.test.ts` 有「zh/en 深層 key 完全對稱」與「en 值不含 CJK」兩個結構測試：任何新 key 必須 ZH／EN 同時加。
- Playwright e2e（`e2e/wallet-onboarding.spec.ts:129-180`）只注入 `window.ethereum`（不發 EIP-6963 announce）→ 開啟發現後清單仍只有 `injected`，走單一路徑，行為不變。本 plan 不要求跑 e2e（需瀏覽器），只改它的註解。

---

## 檔案結構

| 檔案 | 動作 | 職責 |
|---|---|---|
| `web/src/lib/wagmi.ts` | 修改 | 開 EIP-6963 發現，保留 `injected()` 後備 |
| `web/src/lib/walletChoice.ts` | 新建 | 純函式：候選錢包判定 `pickWalletCandidates`、錯誤分類 `classifyLoginError` |
| `web/src/lib/walletChoice.test.ts` | 新建 | 上述純函式測試 |
| `web/src/lib/useWalletLogin.ts` | 新建 | hook：連線（含選單狀態）→ SIWE → resetQueries → onSuccess |
| `web/src/lib/useWalletLogin.test.tsx` | 新建 | 用迷你 harness 元件測 hook 三條路徑 |
| `web/src/components/WalletPicker.tsx` | 新建 | 展示元件：錢包清單 modal |
| `web/src/components/WalletPicker.test.tsx` | 新建 | 渲染／點選／取消 |
| `web/src/lib/copy.ts` | 修改 | `login.noWallet` 文案去 MetaMask 專名；新增 `login.picker*` 三個 key（ZH＋EN） |
| `web/src/components/Header.tsx` | 修改 | 換用 hook＋WalletPicker |
| `web/src/components/Header.test.tsx` | 修改 | 新增「多錢包出選單」案例 |
| `web/src/app/advanced/page.tsx` | 修改 | 換用 hook＋WalletPicker |
| `web/src/app/strategies/[slug]/page.tsx` | 修改 | 換用 hook＋WalletPicker |
| `web/src/app/traders/[address]/page.tsx` | 修改 | 換用 hook＋WalletPicker |
| `web/src/components/wizard/ReconnectGate.tsx` | 修改 | 換用 hook（connectOnly）＋WalletPicker |
| `web/e2e/wallet-onboarding.spec.ts` | 修改 | 只改 35-36 行註解 |

---

### Task 1: `walletChoice.ts` 純函式模組 `@sdd`

**Files:**
- Create: `web/src/lib/walletChoice.ts`
- Create: `web/src/lib/walletChoice.test.ts`

- [ ] **Step 1: 寫失敗測試**

`web/src/lib/walletChoice.test.ts`：
```ts
import { describe, expect, it } from "vitest";
import { classifyLoginError, pickWalletCandidates, type WalletConnectorLike } from "./walletChoice";

const injected: WalletConnectorLike = { id: "injected", name: "Injected" };
const metamask: WalletConnectorLike = { id: "io.metamask", name: "MetaMask", icon: "data:image/svg+xml;base64,AAA" };
const rabby: WalletConnectorLike = { id: "io.rabby", name: "Rabby Wallet", icon: "data:image/svg+xml;base64,BBB" };

describe("pickWalletCandidates", () => {
  it("清單為空 → none", () => {
    expect(pickWalletCandidates([])).toEqual({ kind: "none", candidates: [] });
  });
  it("只有舊式 injected（沒有 EIP-6963 宣告）→ single，直接用 injected", () => {
    expect(pickWalletCandidates([injected])).toEqual({ kind: "single", candidates: [injected] });
  });
  it("發現一個擴充套件錢包 → single，且排除 injected 後備（避免同一錢包出現兩次）", () => {
    expect(pickWalletCandidates([injected, metamask])).toEqual({ kind: "single", candidates: [metamask] });
  });
  it("發現兩個以上 → multiple，順序照清單", () => {
    expect(pickWalletCandidates([injected, metamask, rabby])).toEqual({
      kind: "multiple",
      candidates: [metamask, rabby],
    });
  });
});

describe("classifyLoginError", () => {
  it("viem UserRejectedRequestError → rejected", () => {
    expect(classifyLoginError({ name: "UserRejectedRequestError", message: "User rejected the request." })).toBe("rejected");
  });
  it("EIP-1193 code 4001 → rejected", () => {
    expect(classifyLoginError({ code: 4001, message: "x" })).toBe("rejected");
  });
  it("訊息含 denied/cancel → rejected", () => {
    expect(classifyLoginError(new Error("MetaMask Tx Signature: User denied message signature."))).toBe("rejected");
    expect(classifyLoginError(new Error("cancelled by user"))).toBe("rejected");
  });
  it("wagmi ProviderNotFoundError → noWallet", () => {
    expect(classifyLoginError({ name: "ProviderNotFoundError", message: "Provider not found." })).toBe("noWallet");
  });
  it("其他 → failed；undefined 也不炸", () => {
    expect(classifyLoginError(new Error("boom"))).toBe("failed");
    expect(classifyLoginError(undefined)).toBe("failed");
  });
});
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `npx vitest run src/lib/walletChoice.test.ts`
Expected: FAIL，`Failed to resolve import "./walletChoice"`。

- [ ] **Step 3: 實作**

`web/src/lib/walletChoice.ts`：
```ts
/**
 * lib/walletChoice.ts — 錢包候選判定與登入錯誤分類（純函式，離線可測）。
 *
 * 背景（2026-10-24）：wagmi 開啟 `multiInjectedProviderDiscovery` 後，connector
 * 清單＝ `[injected() 後備, ...EIP-6963 發現到的錢包]`。發現到的 connector `id`
 * 是 rdns（io.metamask／io.rabby…）；後備那個 `id === "injected"`，provider 是
 * `window.ethereum`。規則：
 *   - 有任何發現到的錢包 → 只列發現到的（排除 injected 後備，否則同一錢包會
 *     出現兩次：一次叫 MetaMask、一次叫 Injected）。
 *   - 沒有發現到任何錢包 → 退回 injected 後備（舊式只注入 window.ethereum 的
 *     錢包，以及 e2e 的 mock）。此時**不**在這裡檢查 window.ethereum 是否存在：
 *     交給 connect() 丟 ProviderNotFoundError，由 classifyLoginError 判成 noWallet。
 *     （這也讓既有 vitest 以 `connectors: [{ id: "injected" }]` mock 的測試維持不變。）
 */

export const INJECTED_FALLBACK_ID = "injected";

/** 只取用得到的三個欄位，讓測試不必建完整的 wagmi Connector。 */
export interface WalletConnectorLike {
  id: string;
  name: string;
  icon?: string;
}

export type WalletChoice<C extends WalletConnectorLike> =
  | { kind: "none"; candidates: [] }
  | { kind: "single"; candidates: [C] }
  | { kind: "multiple"; candidates: C[] };

export function pickWalletCandidates<C extends WalletConnectorLike>(connectors: readonly C[]): WalletChoice<C> {
  const discovered = connectors.filter((c) => c.id !== INJECTED_FALLBACK_ID);
  const fallback = connectors.find((c) => c.id === INJECTED_FALLBACK_ID);
  const candidates = discovered.length > 0 ? discovered : fallback ? [fallback] : [];
  if (candidates.length === 0) return { kind: "none", candidates: [] };
  if (candidates.length === 1) return { kind: "single", candidates: [candidates[0]] };
  return { kind: "multiple", candidates };
}

export type LoginErrorKind = "rejected" | "noWallet" | "failed";

/**
 * 錯誤分類。原本五個呼叫點各自複製的 catch 判別搬來這裡；新增 ProviderNotFoundError
 * → noWallet（以前會被歸成 loginFailed，使用者看不出是沒裝錢包）。
 * 不得把 err 內容印到 console（可能含簽章／訊息原文）。
 */
export function classifyLoginError(err: unknown): LoginErrorKind {
  const e = err as { name?: string; code?: number; message?: string } | undefined;
  if (e?.name === "ProviderNotFoundError") return "noWallet";
  const isRejected =
    e?.name === "UserRejectedRequestError"
    || e?.code === 4001
    || /reject|denied|cancel/i.test(String(e?.message ?? ""));
  return isRejected ? "rejected" : "failed";
}
```

- [ ] **Step 4: 跑測試確認通過**

Run: `npx vitest run src/lib/walletChoice.test.ts`
Expected: `9 passed`。

- [ ] **Step 5: Commit**

```bash
git add web/src/lib/walletChoice.ts web/src/lib/walletChoice.test.ts
git commit -m "feat(web): walletChoice 純函式——EIP-6963 候選錢包判定與登入錯誤分類"
```

---

### Task 2: 開啟 EIP-6963 發現＋文案 `@sdd`

**Files:**
- Modify: `web/src/lib/wagmi.ts:1-6`（檔頭註解）、`:23`
- Modify: `web/src/lib/copy.ts:149`（ZH `login.noWallet`）、`:151` 之後新增三 key；`:2029`（EN `login.noWallet`）、`:2031` 之後新增三 key
- Modify: `web/e2e/wallet-onboarding.spec.ts:35-36`（註解）

- [ ] **Step 1: 寫失敗測試（文案）**

在 `web/src/lib/copy.test.ts` 的 `describe("i18n（Task 2：雙語 copy 基礎）"` 區塊內（第 73 行那個 `it` 之前）新增：
```ts
  it("多錢包選單文案存在且 noWallet 不再只點名 MetaMask（2026-10-24 EIP-6963）", () => {
    expect(COPY_ZH.login.pickerTitle).toBeTruthy();
    expect(COPY_ZH.login.pickerHint).toBeTruthy();
    expect(COPY_ZH.login.pickerCancel).toBeTruthy();
    expect(COPY_ZH.login.noWallet).toContain("Rabby");
    expect(COPY_EN.login.noWallet).toContain("Rabby");
  });
```
（該檔已 import `COPY_ZH`／`COPY_EN`；若只 import 其中一個，補上另一個。）

- [ ] **Step 2: 跑測試確認失敗**

Run: `npx vitest run src/lib/copy.test.ts`
Expected: FAIL（`pickerTitle` undefined）。

- [ ] **Step 3: 改 copy.ts**

ZH（`copy.ts:149-151`）把 `noWallet` 整行換成，並在 `loginFailed` 之後加三行：
```ts
    noWallet: "未偵測到瀏覽器錢包。請先安裝 MetaMask、Rabby 等瀏覽器擴充套件錢包後重新整理本頁。Filet 永遠不會請你輸入私鑰或助記詞。",
    rejected: "你在錢包中取消了簽署。準備好後再點一次「連接錢包」即可。",
    loginFailed: "登入失敗，請稍後再試。",
    pickerTitle: "選擇錢包",
    pickerHint: "偵測到多個瀏覽器錢包，請選擇要用來登入的那一個。",
    pickerCancel: "取消",
```
EN（`copy.ts:2029-2031`）同樣：
```ts
    noWallet: "No browser wallet detected. Please install a browser-extension wallet such as MetaMask or Rabby and refresh this page. Filet will never ask for your private key or seed phrase.",
    rejected: "You canceled the signature in your wallet. Click \"Connect wallet\" again when you're ready.",
    loginFailed: "Login failed, please try again later.",
    pickerTitle: "Choose a wallet",
    pickerHint: "Several browser wallets were detected. Pick the one you want to sign in with.",
    pickerCancel: "Cancel",
```

- [ ] **Step 4: 改 wagmi.ts**

檔頭第 2 行改為：
```ts
 * wagmi 設定：closed alpha 只接 injected 類 connector（瀏覽器擴充套件錢包）。
 * 2026-10-24 起開啟 EIP-6963 發現（multiInjectedProviderDiscovery）：裝了多個
 * 擴充套件錢包時由 lib/useWalletLogin + components/WalletPicker 讓使用者選；
 * 下方保留的 `injected()` 是「只注入 window.ethereum、不發 6963 宣告」的舊式
 * 錢包後備（候選規則見 lib/walletChoice.ts）。
```
第 23 行 `multiInjectedProviderDiscovery: false,` 改為 `multiInjectedProviderDiscovery: true,`。其餘（含 barrel import 禁令註解、`connectors: [injected()]`）**不動**。

- [ ] **Step 5: 改 e2e 註解**

`web/e2e/wallet-onboarding.spec.ts:35-36` 改成：
```ts
 * `window.ethereum` 是本檔注入的最小 EIP-1193 mock——**不**發 EIP-6963 announce，
 * 所以即使 `web/src/lib/wagmi.ts` 已開 `multiInjectedProviderDiscovery`，connector
 * 清單仍只有 `injected()` 後備一個、不會出現錢包選單（候選規則見 lib/walletChoice.ts）。
```

- [ ] **Step 6: 跑測試**

Run: `npx vitest run src/lib/copy.test.ts src/app/strategies/[slug]/enNoCjk.test.tsx src/app/settings/enNoCjk.test.tsx src/app/dashboard/enNoCjk.test.tsx src/app/onboarding/enNoCjk.test.tsx`
Expected: 全 PASS（含「zh/en 深層 key 完全對稱」）。

- [ ] **Step 7: Commit**

```bash
git add web/src/lib/wagmi.ts web/src/lib/copy.ts web/src/lib/copy.test.ts web/e2e/wallet-onboarding.spec.ts
git commit -m "feat(web): 開啟 EIP-6963 多錢包發現；noWallet 文案不再只點名 MetaMask，新增選單文案"
```

---

### Task 3: `WalletPicker` 展示元件 `@sdd`

**Files:**
- Create: `web/src/components/WalletPicker.tsx`
- Create: `web/src/components/WalletPicker.test.tsx`

- [ ] **Step 1: 寫失敗測試**

`web/src/components/WalletPicker.test.tsx`：
```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { COPY_ZH } from "@/lib/copy";
import { LangProvider } from "@/lib/lang";
import { WalletPicker } from "./WalletPicker";

const metamask = { id: "io.metamask", name: "MetaMask", icon: "data:image/svg+xml;base64,AAA" };
const rabby = { id: "io.rabby", name: "Rabby Wallet" };

describe("WalletPicker", () => {
  it("列出每個候選錢包的名稱；有 icon 才渲染圖片", () => {
    render(
      <LangProvider>
        <WalletPicker candidates={[metamask, rabby]} onPick={vi.fn()} onCancel={vi.fn()} />
      </LangProvider>,
    );
    expect(screen.getByRole("dialog", { name: COPY_ZH.login.pickerTitle })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /MetaMask/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Rabby Wallet/ })).toBeInTheDocument();
    expect(screen.getAllByRole("img")).toHaveLength(1);
  });

  it("點某個錢包 → onPick 收到該 connector；點取消 → onCancel", async () => {
    const onPick = vi.fn();
    const onCancel = vi.fn();
    render(
      <LangProvider>
        <WalletPicker candidates={[metamask, rabby]} onPick={onPick} onCancel={onCancel} />
      </LangProvider>,
    );
    await userEvent.click(screen.getByRole("button", { name: /Rabby Wallet/ }));
    expect(onPick).toHaveBeenCalledWith(rabby);
    await userEvent.click(screen.getByRole("button", { name: COPY_ZH.login.pickerCancel }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("busy 時全部按鈕 disabled", () => {
    render(
      <LangProvider>
        <WalletPicker candidates={[metamask, rabby]} busy onPick={vi.fn()} onCancel={vi.fn()} />
      </LangProvider>,
    );
    for (const b of screen.getAllByRole("button")) expect(b).toBeDisabled();
  });
});
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `npx vitest run src/components/WalletPicker.test.tsx`
Expected: FAIL（模組不存在）。

- [ ] **Step 3: 實作**

`web/src/components/WalletPicker.tsx`：
```tsx
"use client";
import { useCopy } from "@/lib/lang";
import type { WalletConnectorLike } from "@/lib/walletChoice";

/**
 * WalletPicker — 多個瀏覽器擴充套件錢包並存時的選擇彈窗（2026-10-24，EIP-6963）。
 *
 * 純展示元件：候選清單、選了哪個、取消，全部交回呼叫端（`useWalletLogin`）。
 * 沿站內 `.modal-overlay` / `.modal-card` 樣式（同 ConfirmDialog）。
 * ⚠️ 呼叫端只在使用者按下 CTA 之後才渲染本元件——server 端的 connector 清單
 * 只有 injected 後備，client 才有發現到的錢包，先渲染會 hydration mismatch。
 * icon 是錢包自己透過 EIP-6963 宣告的 data URI，直接用 <img>（不是 next/image：
 * data URI 不走最佳化，且來源不是我們的網域）。
 */
export function WalletPicker<C extends WalletConnectorLike>({ candidates, busy, onPick, onCancel }: {
  candidates: readonly C[];
  busy?: boolean;
  onPick: (connector: C) => void;
  onCancel: () => void;
}) {
  const COPY = useCopy();
  const c = COPY.login;
  return (
    <div className="modal-overlay" role="dialog" aria-modal="true" aria-label={c.pickerTitle}>
      <div className="modal-card card">
        <h3>{c.pickerTitle}</h3>
        <p className="hint">{c.pickerHint}</p>
        <ul className="wallet-picker-list">
          {candidates.map((connector) => (
            <li key={connector.id}>
              <button
                type="button"
                className="btn btn-secondary btn-block wallet-picker-item"
                disabled={busy}
                onClick={() => onPick(connector)}
              >
                {connector.icon && (
                  <img src={connector.icon} alt="" width={24} height={24} className="wallet-picker-icon" />
                )}
                <span>{connector.name}</span>
              </button>
            </li>
          ))}
        </ul>
        <div className="step-actions">
          <button type="button" className="btn btn-secondary" onClick={onCancel} disabled={busy}>
            {c.pickerCancel}
          </button>
        </div>
      </div>
    </div>
  );
}
```

在 `web/src/styles/globals.css` 的 `.modal-card h3 { margin-top: 0; }`（約 1488 行）之後加：
```css
.wallet-picker-list { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 8px; }
.wallet-picker-item { display: flex; align-items: center; justify-content: flex-start; gap: 10px; }
.wallet-picker-icon { width: 24px; height: 24px; border-radius: 6px; flex: none; }
```

- [ ] **Step 4: 跑測試確認通過**

Run: `npx vitest run src/components/WalletPicker.test.tsx`
Expected: `3 passed`。（若 `getAllByRole("img")` 因 `alt=""` 被視為 presentation 而找不到：改測 `container.querySelectorAll("img")` 長度為 1，不要把 alt 改成有文字。）

- [ ] **Step 5: Lint**

Run: `npm run lint`
Expected: 無 error。若 `@next/next/no-img-element` 報 warning，在該 `<img>` 上一行加 `{/* eslint-disable-next-line @next/next/no-img-element -- 錢包宣告的 data URI，不走 next/image */}`。

- [ ] **Step 6: Commit**

```bash
git add web/src/components/WalletPicker.tsx web/src/components/WalletPicker.test.tsx web/src/styles/globals.css
git commit -m "feat(web): WalletPicker 多錢包選擇彈窗（純展示元件）"
```

---

### Task 4: `useWalletLogin` hook `@inline`

**Files:**
- Create: `web/src/lib/useWalletLogin.ts`
- Create: `web/src/lib/useWalletLogin.test.tsx`

**行為規格（取代五處重複的流程）：**

```
start():
  清 error
  if isConnected 且有 address/chainId → finish(address, chainId)
  else:
    choice = pickWalletCandidates(connectors)
    none     → error = noWallet
    single   → connectThen(candidates[0])
    multiple → pickerOpen = true, picker 的 candidates = choice.candidates
choose(connector):   pickerOpen = false; connectThen(connector)
cancelPicker():      pickerOpen = false
connectThen(c):      phase = "connecting"; r = await connectAsync({ connector: c }); finish(r.accounts[0], r.chainId)
finish(addr, cid):
  if connectOnly → onSuccess?.(undefined); return          （ReconnectGate 用）
  phase = "signing"; me = await loginWithSiwe({ address, chainId, signMessage })
  await queryClient.resetQueries()                           （身份切換整包清，2026-09-01 事故）
  await onSuccess?.(me)
catch → error = COPY.login[rejected|noWallet|loginFailed]  依 classifyLoginError
finally → phase = "idle"
```

- [ ] **Step 1: 寫失敗測試**

`web/src/lib/useWalletLogin.test.tsx`：
```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const connectAsync = vi.fn();
const signMessageAsync = vi.fn();
let accountState: { address?: string; chainId?: number; isConnected: boolean } = { isConnected: false };
let connectorsState: Array<{ id: string; name: string; icon?: string }> = [{ id: "injected", name: "Injected" }];
vi.mock("wagmi", () => ({
  useAccount: () => accountState,
  useConnect: () => ({ connectAsync, connectors: connectorsState }),
  useSignMessage: () => ({ signMessageAsync }),
}));
const loginWithSiwe = vi.fn();
vi.mock("@/lib/siwe", () => ({ loginWithSiwe: (...a: unknown[]) => loginWithSiwe(...a) }));

import { COPY_ZH } from "@/lib/copy";
import { LangProvider } from "@/lib/lang";
import { WalletPicker } from "@/components/WalletPicker";
import { useWalletLogin } from "./useWalletLogin";

const metamask = { id: "io.metamask", name: "MetaMask" };
const rabby = { id: "io.rabby", name: "Rabby Wallet" };

function Harness({ onSuccess, connectOnly }: { onSuccess?: (me: unknown) => void; connectOnly?: boolean }) {
  const login = useWalletLogin({ onSuccess, connectOnly });
  return (
    <div>
      <button type="button" onClick={() => void login.start()}>go</button>
      <output data-testid="phase">{login.phase}</output>
      {login.error && <p role="alert">{login.error}</p>}
      {login.pickerOpen && (
        <WalletPicker candidates={login.candidates} onPick={(c) => void login.choose(c)} onCancel={login.cancelPicker} />
      )}
    </div>
  );
}

function renderHarness(props: Parameters<typeof Harness>[0] = {}) {
  const qc = new QueryClient();
  const resetSpy = vi.spyOn(qc, "resetQueries");
  render(
    <QueryClientProvider client={qc}>
      <LangProvider><Harness {...props} /></LangProvider>
    </QueryClientProvider>,
  );
  return { resetSpy };
}

beforeEach(() => {
  vi.clearAllMocks();
  accountState = { isConnected: false };
  connectorsState = [{ id: "injected", name: "Injected" }];
  connectAsync.mockResolvedValue({ accounts: ["0xabc"], chainId: 42161 });
  loginWithSiwe.mockResolvedValue({ address: "0xabc", account_id: "acc" });
});

describe("useWalletLogin", () => {
  it("只有 injected 後備 → 直接連、簽、resetQueries、onSuccess 收到 me", async () => {
    const onSuccess = vi.fn();
    const { resetSpy } = renderHarness({ onSuccess });
    await userEvent.click(screen.getByRole("button", { name: "go" }));
    await waitFor(() => expect(onSuccess).toHaveBeenCalledWith({ address: "0xabc", account_id: "acc" }));
    expect(connectAsync).toHaveBeenCalledWith({ connector: connectorsState[0] });
    expect(loginWithSiwe).toHaveBeenCalledWith(expect.objectContaining({ address: "0xabc", chainId: 42161 }));
    expect(resetSpy).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByTestId("phase")).toHaveTextContent("idle");
  });

  it("多個發現到的錢包 → 先出選單，選了才連；取消則不連", async () => {
    connectorsState = [{ id: "injected", name: "Injected" }, metamask, rabby];
    const onSuccess = vi.fn();
    renderHarness({ onSuccess });
    await userEvent.click(screen.getByRole("button", { name: "go" }));
    expect(connectAsync).not.toHaveBeenCalled();
    const dialog = screen.getByRole("dialog", { name: COPY_ZH.login.pickerTitle });
    expect(dialog).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Injected/ })).toBeNull(); // 後備不列
    await userEvent.click(screen.getByRole("button", { name: COPY_ZH.login.pickerCancel }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(connectAsync).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "go" }));
    await userEvent.click(screen.getByRole("button", { name: /Rabby Wallet/ }));
    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1));
    expect(connectAsync).toHaveBeenCalledWith({ connector: rabby });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("已連線 → 跳過 connect 直接簽", async () => {
    accountState = { address: "0xdef", chainId: 1, isConnected: true };
    renderHarness();
    await userEvent.click(screen.getByRole("button", { name: "go" }));
    await waitFor(() => expect(loginWithSiwe).toHaveBeenCalledWith(expect.objectContaining({ address: "0xdef", chainId: 1 })));
    expect(connectAsync).not.toHaveBeenCalled();
  });

  it("拒簽 → rejected 文案；ProviderNotFoundError → noWallet 文案；其他 → loginFailed", async () => {
    loginWithSiwe.mockRejectedValueOnce({ name: "UserRejectedRequestError", message: "User rejected" });
    renderHarness();
    await userEvent.click(screen.getByRole("button", { name: "go" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(COPY_ZH.login.rejected));

    connectAsync.mockRejectedValueOnce({ name: "ProviderNotFoundError", message: "Provider not found." });
    await userEvent.click(screen.getByRole("button", { name: "go" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(COPY_ZH.login.noWallet));

    loginWithSiwe.mockRejectedValueOnce(new Error("boom"));
    await userEvent.click(screen.getByRole("button", { name: "go" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(COPY_ZH.login.loginFailed));
  });

  it("connectOnly：連上就 onSuccess，不簽、不 resetQueries", async () => {
    const onSuccess = vi.fn();
    const { resetSpy } = renderHarness({ onSuccess, connectOnly: true });
    await userEvent.click(screen.getByRole("button", { name: "go" }));
    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1));
    expect(loginWithSiwe).not.toHaveBeenCalled();
    expect(resetSpy).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `npx vitest run src/lib/useWalletLogin.test.tsx`
Expected: FAIL（模組不存在）。

- [ ] **Step 3: 實作**

`web/src/lib/useWalletLogin.ts`：
```ts
"use client";
/**
 * lib/useWalletLogin.ts — 連接錢包 → SIWE 登入的單一入口（2026-10-24）。
 *
 * 取代 Header／advanced／strategies/[slug]／traders/[address]／ReconnectGate 五處
 * 各自複製的 `connectors[0]` → `connectAsync` → `loginWithSiwe` 流程，並加上
 * EIP-6963 多錢包選單：
 *   - 候選規則在 lib/walletChoice.ts（純函式）；
 *   - 選單 UI 由呼叫端用 `pickerOpen`／`candidates`／`choose`／`cancelPicker`
 *     渲染 <WalletPicker>（只在 pickerOpen 時渲染，避免 SSR hydration mismatch）；
 *   - 登入成功後 `await queryClient.resetQueries()`：身份切換整包清
 *     （2026-09-01 快取殘留事故，見 Header.handleLogout 註解），再呼叫 onSuccess。
 * `connectOnly`：只連錢包不簽（ReconnectGate：server session 仍有效、只是錢包斷了）。
 * 錯誤分類不得把 err 原文印出（可能含簽章／訊息原文）。
 */
import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import { useAccount, useConnect, useSignMessage, type Connector } from "wagmi";
import type { Me } from "./api";
import { useCopy } from "./lang";
import { loginWithSiwe } from "./siwe";
import { classifyLoginError, pickWalletCandidates } from "./walletChoice";

export type LoginPhase = "idle" | "connecting" | "signing";

export interface UseWalletLoginOptions {
  /** 登入（或 connectOnly 時連線）成功後呼叫；login 模式帶 Me，connectOnly 帶 undefined。 */
  onSuccess?: (me: Me | undefined) => void | Promise<void>;
  /** 只連錢包、不做 SIWE 也不 resetQueries。 */
  connectOnly?: boolean;
}

export interface WalletLogin {
  phase: LoginPhase;
  error: string | null;
  pickerOpen: boolean;
  candidates: readonly Connector[];
  start: () => Promise<void>;
  choose: (connector: Connector) => Promise<void>;
  cancelPicker: () => void;
}

export function useWalletLogin({ onSuccess, connectOnly = false }: UseWalletLoginOptions = {}): WalletLogin {
  const COPY = useCopy();
  const queryClient = useQueryClient();
  const { address, chainId, isConnected } = useAccount();
  const { connectAsync, connectors } = useConnect();
  const { signMessageAsync } = useSignMessage();
  const [phase, setPhase] = useState<LoginPhase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [candidates, setCandidates] = useState<readonly Connector[]>([]);

  const fail = useCallback((err: unknown) => {
    const kind = classifyLoginError(err);
    setError(kind === "rejected" ? COPY.login.rejected : kind === "noWallet" ? COPY.login.noWallet : COPY.login.loginFailed);
  }, [COPY]);

  const finish = useCallback(async (addr: string, cid: number) => {
    if (connectOnly) {
      await onSuccess?.(undefined);
      return;
    }
    setPhase("signing");
    const me = await loginWithSiwe({
      address: addr,
      chainId: cid,
      signMessage: (message) => signMessageAsync({ message }),
    });
    await queryClient.resetQueries();
    await onSuccess?.(me);
  }, [connectOnly, onSuccess, queryClient, signMessageAsync]);

  const connectThen = useCallback(async (connector: Connector) => {
    try {
      setPhase("connecting");
      const result = await connectAsync({ connector });
      const addr = result.accounts[0];
      if (!addr) {
        setError(COPY.login.noWallet);
        return;
      }
      await finish(addr, result.chainId);
    } catch (err) {
      fail(err);
    } finally {
      setPhase("idle");
    }
  }, [COPY, connectAsync, fail, finish]);

  const start = useCallback(async () => {
    setError(null);
    if (isConnected && address && chainId) {
      try {
        await finish(address, chainId);
      } catch (err) {
        fail(err);
      } finally {
        setPhase("idle");
      }
      return;
    }
    const choice = pickWalletCandidates(connectors);
    if (choice.kind === "none") {
      setError(COPY.login.noWallet);
      return;
    }
    if (choice.kind === "single") {
      await connectThen(choice.candidates[0]);
      return;
    }
    setCandidates(choice.candidates);
    setPickerOpen(true);
  }, [COPY, address, chainId, connectThen, connectors, fail, finish, isConnected]);

  const choose = useCallback(async (connector: Connector) => {
    setPickerOpen(false);
    await connectThen(connector);
  }, [connectThen]);

  const cancelPicker = useCallback(() => setPickerOpen(false), []);

  return { phase, error, pickerOpen, candidates, start, choose, cancelPicker };
}
```

- [ ] **Step 4: 跑測試確認通過**

Run: `npx vitest run src/lib/useWalletLogin.test.tsx src/lib/walletChoice.test.ts`
Expected: 全 PASS。

- [ ] **Step 5: 型別檢查**

Run: `npx tsc --noEmit -p tsconfig.json`
Expected: 無 error。（`pickWalletCandidates(connectors)` 的泛型以 wagmi `Connector` 實例化，`candidates` 型別為 `Connector[]`；若 tsc 抱怨 `Connector` 缺 `icon`／`name`，確認 `WalletConnectorLike` 三欄位皆為 Connector 既有欄位——是，`icon?: string | undefined`、`name: string`、`id: string`。）

- [ ] **Step 6: Commit**

```bash
git add web/src/lib/useWalletLogin.ts web/src/lib/useWalletLogin.test.tsx
git commit -m "feat(web): useWalletLogin hook——連線（含多錢包選單）→ SIWE → resetQueries 單一入口"
```

---

### Task 5: Header 換用 hook `@inline`

**Files:**
- Modify: `web/src/components/Header.tsx:5-6,12,34,47-51,66-119`＋CTA 渲染處（檔內搜 `handleLoginCta`／`loginPhase`／`loginError` 的 JSX 使用點）
- Modify: `web/src/components/Header.test.tsx:20-27`＋新增一個 `it`

- [ ] **Step 1: 寫失敗測試**

`Header.test.tsx:20-27` 的 mock 改成可變 connectors：
```ts
const connectAsync = vi.fn();
const signMessageAsync = vi.fn();
let accountState: { address?: string; chainId?: number; isConnected: boolean } = { isConnected: false };
let connectorsState: Array<{ id: string; name: string; icon?: string }> = [{ id: "injected", name: "Injected" }];
vi.mock("wagmi", () => ({
  useAccount: () => accountState,
  useConnect: () => ({ connectAsync, connectors: connectorsState }),
  useSignMessage: () => ({ signMessageAsync }),
}));
```
在既有 `beforeEach`（若有）加 `connectorsState = [{ id: "injected", name: "Injected" }];`；沒有 `beforeEach` 就新增一個。然後加一個案例（放在既有未登入 CTA 測試旁；`render`/`wrap` 用法照該檔既有案例）：
```tsx
  it("多個瀏覽器錢包並存：按 CTA 先出選單，選 Rabby 才連該 connector 並簽署", async () => {
    connectorsState = [
      { id: "injected", name: "Injected" },
      { id: "io.metamask", name: "MetaMask" },
      { id: "io.rabby", name: "Rabby Wallet" },
    ];
    connectAsync.mockResolvedValue({ accounts: ["0xabc"], chainId: 42161 });
    loginWithSiwe.mockResolvedValue({ address: "0xabc", account_id: "acc" });
    getDashboard.mockRejectedValue(new ApiError("client", "not found", 404, "not found")); // 同本檔 :255 的既有用法
    const qc = new QueryClient();
    render(wrap(<Header />, qc));
    await userEvent.click(screen.getByRole("button", { name: COPY_ZH.nav.cta }));
    expect(connectAsync).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: COPY_ZH.login.pickerTitle })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Rabby Wallet/ }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/strategies"));
    expect(connectAsync).toHaveBeenCalledWith({ connector: connectorsState[2] });
    expect(loginWithSiwe).toHaveBeenCalledTimes(1);
  });
```
（`beforeEach` 已存在於 `:71`，把 `connectorsState = [{ id: "injected", name: "Injected" }];` 加進去即可。）

- [ ] **Step 2: 跑測試確認新案例失敗、其餘仍綠**

Run: `npx vitest run src/components/Header.test.tsx`
Expected: 只有新案例 FAIL（找不到 dialog）。

- [ ] **Step 3: 改 Header.tsx**

1. 移除 import：`useAccount, useConnect, useSignMessage`（第 6 行整行刪）、`loginWithSiwe`（第 12 行刪）；`useState` 若其他地方仍用就保留，否則刪。`useQueryClient` 仍用於 handleLogout，保留。
2. 新增 import：
   ```ts
   import { WalletPicker } from "@/components/WalletPicker";
   import { useWalletLogin } from "@/lib/useWalletLogin";
   ```
3. 刪第 34 行 `type LoginPhase = ...`。
4. 刪第 47-51 行五個宣告（`useAccount`／`useConnect`／`useSignMessage`／`loginPhase`／`loginError`），改為：
   ```ts
   const login = useWalletLogin({
     onSuccess: async () => {
       // 依 dashboard 狀態導向（原 handleLoginCta 的後半；一次性讀取，不進 react-query 快取）
       let dest = "/strategies";
       try {
         const dashboard = await getDashboard();
         if (dashboard.status && dashboard.status.state !== "inactive") dest = "/dashboard";
       } catch {
         dest = "/strategies"; // 404／例外＝還沒有可看的 dashboard，保守送去選策略
       }
       router.push(dest);
     },
   });
   ```
5. 刪第 66-119 行整個 `handleLoginCta`（含 docstring）。檔頭 docstring 第 22-28 行改一句：「連線／簽署流程改由 `lib/useWalletLogin` 統一（2026-10-24，EIP-6963 多錢包選單）；本元件只提供成功後的導向決策」。
6. JSX（`Header.tsx:204-213`）：`disabled={loginPhase !== "idle"}` → `disabled={login.phase !== "idle"}`；`onClick={handleLoginCta}` → `onClick={() => void login.start()}`；207-209 行的 `loginPhase` → `login.phase`；213 行 `{loginError && <p className="header-cta-error">{loginError}</p>}` → `{login.error && <p className="header-cta-error">{login.error}</p>}`。緊接在 213 行之後加：
   ```tsx
   {login.pickerOpen && (
     <WalletPicker
       candidates={login.candidates}
       busy={login.phase !== "idle"}
       onPick={(c) => void login.choose(c)}
       onCancel={login.cancelPicker}
     />
   )}
   ```

- [ ] **Step 4: 跑測試**

Run: `npx vitest run src/components/Header.test.tsx src/app/pageStates.test.tsx`
Expected: 全 PASS（既有案例不改仍綠：單一 injected 走直連路徑）。

- [ ] **Step 5: Commit**

```bash
git add web/src/components/Header.tsx web/src/components/Header.test.tsx
git commit -m "refactor(web): Header 登入 CTA 改用 useWalletLogin，支援多錢包選單"
```

---

### Task 6: 三個頁面換用 hook `@inline`

**Files:**
- Modify: `web/src/app/advanced/page.tsx:35,48,50,77-81,94-133,154-160`
- Modify: `web/src/app/strategies/[slug]/page.tsx:30,42,47,80-84,132-174,336-351`
- Modify: `web/src/app/traders/[address]/page.tsx:49,63,76,142-146,187-229,526-543`
- 測試檔不改（既有 mock 形狀 `connectors: [{ id: "injected" }]` 走直連路徑）。

**三頁共同改法：**
1. 刪 `import { useAccount, useConnect, useSignMessage } from "wagmi"` 與 `import { loginWithSiwe } from "@/lib/siwe"`；若 `useQueryClient` 只被登入流程用到就一併刪（搜該檔其他 `queryClient.` 用法再決定）。
2. 加 `import { WalletPicker } from "@/components/WalletPicker";` 與 `import { useWalletLogin } from "@/lib/useWalletLogin";`。
3. 刪頁面本地的 `type LoginPhase`／`type ConnectPhase`。`FollowPanel.tsx:34` 自己 export 的 `ConnectPhase = "idle" | "connecting" | "signing"` **不動**（與 hook 的 `LoginPhase` 形狀相同，結構型別相容，直接傳 `login.phase` 即可）；advanced 的 `NotLoggedInGateCard` prop 型別改 `import type { LoginPhase } from "@/lib/useWalletLogin";`。
4. 刪五個宣告與整個 handler，換成 `const login = useWalletLogin({ onSuccess })`。
5. 在原 CTA 元件旁渲染 `{login.pickerOpen && <WalletPicker candidates={login.candidates} busy={login.phase !== "idle"} onPick={(c) => void login.choose(c)} onCancel={login.cancelPicker} />}`。

**各頁 onSuccess 與 CTA：**

- [ ] **Step 1: advanced/page.tsx**
  - `const login = useWalletLogin();`（登入成功後不跳轉，頁面靠 `useMe` 重抓翻成登入態，與現行一致）。
  - `:154-160` 改 `phase={login.phase} error={login.error} onLogin={() => void login.start()}`，並在 `<NotLoggedInGateCard …/>` 之後（同一個 fragment 內）加 WalletPicker 渲染。
  - Run: `npx vitest run src/app/advanced/page.test.tsx` → PASS。

- [ ] **Step 2: strategies/[slug]/page.tsx**
  - 保留 `buildQuery()`。handler 改為：
    ```ts
    const login = useWalletLogin({
      onSuccess: () => { router.push(`/onboarding?${buildQuery()}`); },
    });
    function handleCta() {
      if (loggedIn) {
        router.push(`/onboarding?${buildQuery()}`);
        return;
      }
      void login.start();
    }
    ```
    （`loggedIn` 時直接跳、不簽——與現行 `:134-137` 一致。）
  - `:336-351` `<FollowPanel … phase={login.phase} error={login.error} onCta={handleCta} …/>`，其後加 WalletPicker 渲染。
  - Run: `npx vitest run "src/app/strategies/[slug]/page.test.tsx" "src/app/strategies/[slug]/enNoCjk.test.tsx"` → PASS。

- [ ] **Step 3: traders/[address]/page.tsx**
  - 與 strategies 同形，`onSuccess` 的 push 目標照現行 `:218` 的 `buildQuery()`（內含 `advanced:${trader.address}`）。`walletAddress` 變數不再需要（hook 內部用 `useAccount`），刪除；若該檔其他地方用到 `walletAddress`，保留 `const { address: walletAddress } = useAccount();` 那一行並保留 wagmi import 的 `useAccount`。
  - `:526-543` `phase={login.phase} error={login.error} onCta={handleCta}`，其後加 WalletPicker 渲染。
  - Run: `npx vitest run "src/app/traders/[address]/page.test.tsx"` → PASS。

- [ ] **Step 4: 全量＋型別**

Run: `npm test && npx tsc --noEmit -p tsconfig.json && npm run lint`
Expected: 全綠、無 type error、lint 無 error。

- [ ] **Step 5: Commit**

```bash
git add web/src/app/advanced/page.tsx "web/src/app/strategies/[slug]/page.tsx" "web/src/app/traders/[address]/page.tsx"
git commit -m "refactor(web): advanced／strategies／traders 三頁登入 CTA 改用 useWalletLogin"
```

---

### Task 7: ReconnectGate 換用 hook（connectOnly） `@inline`

**Files:**
- Modify: `web/src/components/wizard/ReconnectGate.tsx`（整檔重寫，24 行）
- 既有 onboarding 測試（`onboarding/page.test.tsx:23-28` 等三檔）mock 的是 `useConnect: () => ({ connect, connectors, isPending })`，**沒有 `connectAsync`／`useSignMessage`**。hook 會呼叫 `useSignMessage()`，mock 缺它會 throw。因此這三檔 mock 要補 `connectAsync: vi.fn()` 與 `useSignMessage: () => ({ signMessageAsync: vi.fn() })`（只補、不改既有斷言）。

- [ ] **Step 1: 補三個 onboarding 測試的 mock**

`onboarding/page.test.tsx`、`onboarding/enNoCjk.test.tsx`、`onboarding/scaleClamp.test.tsx` 的 `vi.mock("wagmi", …)` 回傳物件各加：
```ts
  useConnect: () => ({ connect: vi.fn(), connectAsync: vi.fn(), connectors: [{ id: "injected", name: "Injected" }], isPending: false }),
  useSignMessage: () => ({ signMessageAsync: vi.fn() }),
```
（既有 `useAccount` 等保留。）

- [ ] **Step 2: 重寫 ReconnectGate.tsx**

```tsx
"use client";
import { WalletPicker } from "@/components/WalletPicker";
import { useCopy } from "@/lib/lang";
import { useWalletLogin } from "@/lib/useWalletLogin";

/**
 * ReconnectGate — onboarding 中途錢包斷線（上鎖）時的重連卡。server session 仍有效，
 * 所以只連錢包不重簽（`connectOnly`）。多錢包並存時與登入 CTA 同一套選單
 * （2026-10-24，EIP-6963；流程見 lib/useWalletLogin）。
 */
export function ReconnectGate() {
  const COPY = useCopy();
  const c = COPY.wizard;
  const login = useWalletLogin({ connectOnly: true });
  return (
    <div className="step-card">
      <h2>{c.reconnectTitle}</h2>
      <p className="hint">{c.reconnectHint}</p>
      <div className="step-actions">
        <button type="button" className="btn btn-primary"
          disabled={login.phase !== "idle"}
          onClick={() => void login.start()}>
          {c.reconnectButton}
        </button>
      </div>
      {login.error && <p className="hint sign-error">{login.error}</p>}
      {login.pickerOpen && (
        <WalletPicker
          candidates={login.candidates}
          busy={login.phase !== "idle"}
          onPick={(conn) => void login.choose(conn)}
          onCancel={login.cancelPicker}
        />
      )}
    </div>
  );
}
```
行為差異（刻意）：原本 `!injected` 時顯示 noWallet 且按鈕 disabled；現在 `injected()` 永遠在清單裡，所以 noWallet 只會在 connect 丟 `ProviderNotFoundError` 之後顯示（按下去才知道）。這與原本實際行為相同（原本 `connectors[0]` 也永遠存在，`!injected` 分支是死碼）。

- [ ] **Step 3: 跑測試**

Run: `npx vitest run src/app/onboarding`
Expected: 全 PASS。

- [ ] **Step 4: Commit**

```bash
git add web/src/components/wizard/ReconnectGate.tsx web/src/app/onboarding/page.test.tsx web/src/app/onboarding/enNoCjk.test.tsx web/src/app/onboarding/scaleClamp.test.tsx
git commit -m "refactor(web): ReconnectGate 改用 useWalletLogin(connectOnly)，支援多錢包選單"
```

---

### Task 8: 收尾驗證（主線程親跑，不派工）

- [ ] **Step 1: 舊路徑歸零**

Run: `grep -rn "connectors\[0\]" src --include=*.tsx --include=*.ts | grep -v test`
Expected: 0 行。

Run: `grep -rln "loginWithSiwe" src --include=*.tsx --include=*.ts | grep -v test`
Expected: 只有 `src/lib/siwe.ts` 與 `src/lib/useWalletLogin.ts` 兩個檔。

Run: `grep -rn "from \"wagmi/connectors\"" src`
Expected: 0 行（barrel import 禁令）。

- [ ] **Step 2: 全量**

Run: `npm test && npx tsc --noEmit -p tsconfig.json && npm run lint && npm run build`
Expected: 測試全綠、無 type error、lint 無 error、build 成功。build 後：
```bash
grep -rl MetaMaskSDK .next/static/chunks/ | wc -l
```
Expected: `0`（wagmi.ts 檔頭的依賴衛生檢查，開 6963 不應把任何 SDK 拖進 bundle）。

- [ ] **Step 3: 手動煙霧（使用者或主線程用瀏覽器）**

`npm run dev` 後，在同時裝 MetaMask＋Rabby 的 Chrome 開首頁按「連接錢包」：應出現兩個錢包的選單（含圖示）；選一個 → 該錢包彈連線 → 簽署 → 導向。只裝一個錢包的瀏覽器：直接彈連線，無選單。這一步**不是**派工驗收條件（需真錢包），由主線程於全部 task 完成後向使用者回報「待實機確認」。

- [ ] **Step 4: 派 `reviewer`**

輸入：`git diff <開工前 commit>..HEAD -- web/`、本 plan 檔、`npm test` 輸出。重點：(1) 五處舊流程是否全部移除、行為是否等價（Header 導向邏輯、strategies/traders 的 loggedIn 直跳）；(2) 錯誤路徑有無 console 洩漏 message／signature；(3) WalletPicker 是否只在 pickerOpen 時渲染（SSR）；(4) copy ZH/EN 對稱。

---

### Task 9: 複審修正（reviewer 第一輪：1 Critical／1 Warning／2 Suggestion） `@inline`

<!-- 2026-10-24 主線程裁決，依 reviewer 回報寫回 -->

**裁決：**
- R1（Critical，`Header.tsx:164`＋`globals.css:79`）：`.app-header` 有 `backdrop-filter`，成為內部 `position: fixed` 的定位基準，選單 overlay 只蓋到 header 那一條。修法：`WalletPicker` 一律用 `createPortal` 掛到 `document.body`，並加 `wallet-picker-overlay` class 把 z-index 提到 100（高於 sticky header 的 90）。這同時解掉 reviewer 的 Suggestion 2（頁面上選單被 header 蓋住、CTA 仍可點）。不改全域 `.modal-overlay` 的 z-index（其他 modal 不在本次範圍）。
- R2（Warning，`useWalletLogin.ts` start 的已連線分支）：wagmi 自動重連讓回訪者 `isConnected` 為 true，原規格直接簽、永遠看不到選單。裁決：**偵測到多個錢包時，不論是否已連線都出選單**；`choose()` 選到的若就是目前已連的 connector（`useAccount().connector?.id` 相同）→ 跳過 connect 直接 finish（避免 wagmi `ConnectorAlreadyConnectedError`，`actions/connect.js:12-13` 只在 `uid === state.current` 時丟）；選別的 → 照常 `connectAsync`（wagmi 允許在已連線時連另一個 connector 並切換 current）。
- R3（Suggestion 1，`globals.css:1492`）：`.modal-card ul`（0,1,1）蓋掉 `.wallet-picker-list`（0,1,0）。修法：選擇器改成 `.modal-card ul.wallet-picker-list`。
- 觀察三條（候選快照、ReconnectGate 可選不同地址、chainId undefined）：不處理，reviewer 自己也說不出實際觸發情境；ReconnectGate 地址不一致由後端驗簽擋，與本次無關。

**Files:**
- Modify: `web/src/components/WalletPicker.tsx`
- Modify: `web/src/styles/globals.css:1489`（選擇器）＋新增 `.wallet-picker-overlay`
- Modify: `web/src/lib/useWalletLogin.ts`
- Modify: `web/src/lib/useWalletLogin.test.tsx`（新增 1 案例）
- Modify: `web/src/components/WalletPicker.test.tsx`（新增 1 斷言）

- [ ] **Step 1: 寫失敗測試**

`useWalletLogin.test.tsx` 新增（放在 describe 內最後）：
```tsx
  it("已連線但偵測到多個錢包 → 仍出選單；選目前已連的那個直接簽、不重連；選別的才 connectAsync", async () => {
    connectorsState = [{ id: "injected", name: "Injected" }, metamask, rabby];
    accountState = { address: "0xdef", chainId: 1, isConnected: true, connector: metamask } as typeof accountState;
    const onSuccess = vi.fn();
    renderHarness({ onSuccess });
    await userEvent.click(screen.getByRole("button", { name: "go" }));
    expect(screen.getByRole("dialog", { name: COPY_ZH.login.pickerTitle })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /MetaMask/ }));
    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1));
    expect(connectAsync).not.toHaveBeenCalled();
    expect(loginWithSiwe).toHaveBeenCalledWith(expect.objectContaining({ address: "0xdef", chainId: 1 }));

    await userEvent.click(screen.getByRole("button", { name: "go" }));
    await userEvent.click(screen.getByRole("button", { name: /Rabby Wallet/ }));
    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(2));
    expect(connectAsync).toHaveBeenCalledWith({ connector: rabby });
  });
```
`accountState` 的型別宣告改成 `{ address?: string; chainId?: number; isConnected: boolean; connector?: { id: string; name: string } }`。

`WalletPicker.test.tsx` 第一個案例加兩個斷言（portal＋z-index class）：
```tsx
    const dialog = screen.getByRole("dialog", { name: COPY_ZH.login.pickerTitle });
    expect(dialog.parentElement).toBe(document.body); // createPortal 到 body
    expect(dialog).toHaveClass("wallet-picker-overlay");
```

- [ ] **Step 2: 跑測試確認新案例失敗**

Run: `npx vitest run src/lib/useWalletLogin.test.tsx src/components/WalletPicker.test.tsx`
Expected: 兩個新斷言／案例 FAIL，其餘綠。

- [ ] **Step 3: 改 WalletPicker.tsx**

```tsx
import { createPortal } from "react-dom";
```
回傳改為 `createPortal(<div className="modal-overlay wallet-picker-overlay" role="dialog" …>…</div>, document.body)`。docstring 補一段：「用 createPortal 掛到 body：Header 有 `backdrop-filter`，會成為內部 fixed 元素的定位基準，直接渲染在 Header 內 overlay 只蓋到 header 那一條（reviewer 2026-10-24 Critical）；`wallet-picker-overlay` 把 z-index 提到 100，高於 sticky header 的 90。本元件只在使用者按下 CTA 後才渲染，所以 `document` 一定存在。」

- [ ] **Step 4: 改 globals.css**

第 1489 行選擇器改成 `.modal-card ul.wallet-picker-list { … }`（內容不變），並在其後加一行：
```css
.wallet-picker-overlay { z-index: 100; } /* 高於 .app-header 的 90 */
```

- [ ] **Step 5: 改 useWalletLogin.ts**

`useAccount()` 多取 `connector: currentConnector`。`start()` 改為：
```ts
  const start = useCallback(async () => {
    setError(null);
    const choice = pickWalletCandidates(connectors);
    // 多錢包一律先問（即使 wagmi 已自動重連其中一個）：回訪者才有機會換錢包登入。
    if (choice.kind === "multiple") {
      setCandidates(choice.candidates);
      setPickerOpen(true);
      return;
    }
    if (isConnected && address && chainId) {
      await finishGuarded(address, chainId);
      return;
    }
    if (choice.kind === "none") {
      setError(COPY.login.noWallet);
      return;
    }
    await connectThen(choice.candidates[0]);
  }, [...]);
```
其中 `finishGuarded` 是把原本已連線分支的 `try { await finish } catch { fail } finally { setPhase("idle") }` 抽成一個 `useCallback`（避免在 start 與 choose 重複）。`choose()` 改為：
```ts
  const choose = useCallback(async (connector: Connector) => {
    setPickerOpen(false);
    // 選到的就是目前已連的 connector → 不重連（wagmi 會丟 ConnectorAlreadyConnectedError），直接簽。
    if (isConnected && address && chainId && currentConnector && connector.id === currentConnector.id) {
      await finishGuarded(address, chainId);
      return;
    }
    await connectThen(connector);
  }, [...]);
```
依賴陣列補齊（lint 的 exhaustive-deps 會告訴你）。檔頭 docstring 補一句本裁決。

- [ ] **Step 6: 跑測試＋型別＋lint**

Run: `npx vitest run src/lib src/components/WalletPicker.test.tsx src/components/Header.test.tsx && npx tsc --noEmit -p tsconfig.json 2>&1 | grep "error TS" | cut -d'(' -f1 | sort | uniq -c && npm run lint`
Expected: 全綠；tsc 只剩基線兩檔；lint 無新 warning。

- [ ] **Step 7: Commit**

```bash
git add web/src/components/WalletPicker.tsx web/src/components/WalletPicker.test.tsx web/src/styles/globals.css web/src/lib/useWalletLogin.ts web/src/lib/useWalletLogin.test.tsx
git commit -m "fix(web): WalletPicker 改 portal＋z-index 100（Header backdrop-filter 定位基準）；已連線多錢包仍出選單；CSS 權重修正"
```

---

## 自我審查

- **Spec 覆蓋**：開發現 → Task 2；選單 UI → Task 3；五處收斂 → Task 4-7；文案去 MetaMask 專名 → Task 2；舊式錢包／e2e 後備 → Task 1 候選規則＋Task 2 註解；SSR 風險 → hook 設計（只在 pickerOpen 渲染）＋Task 3 註解。手機／WalletConnect 明確排除。
- **型別一致**：`LoginPhase` 定義在 `useWalletLogin.ts` 並 export；`WalletConnectorLike` 定義在 `walletChoice.ts`，`WalletPicker` 泛型以它為 bound，hook 傳入 wagmi `Connector`（符合 bound）。`useWalletLogin` 回傳欄位 `phase/error/pickerOpen/candidates/start/choose/cancelPicker` 在 Task 4-7 用法一致。`onSuccess` 參數型別 `Me | undefined`。
- **既有測試不改 mock 也能過的理由**：候選規則「只有 injected → single」＋ hook 用 `useConnect().connectors`（不用 `useConnectors`，既有 mock 沒提供）。唯一要補 mock 的是 onboarding 三檔（缺 `useSignMessage`／`connectAsync`），已列在 Task 7。
