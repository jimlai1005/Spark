# 推薦碼區塊移進費用確認主卡片（onboarding step 4）

日期：2026-09-19。狀態：**Task 1＋2 完成、reviewer PASS、已部署正式機（commit `b9e5649`，14:41 UTC，記錄見 `deploy/RUNBOOK.md` 附錄 B）**。
驗收證據（主線程親跑）：`npm test` 72 files / 714 tests 全綠；`npm run build` 型別檢查通過；
`ReferralOptinCard me` 在第 199 行、`btn-primary` 在第 204 行。reviewer 另提一條既有行為觀察：
推薦碼簽署進行中主按鈕仍可按（非本次引入，未處理，見下方「未處理項」）。

### Task 2 `@inline`：兩鈕互鎖（使用者 2026-09-19 裁決追加，主線程自行實作）

reviewer 指出：`signing=true` 時只 disable 推薦碼自己的按鈕，主按鈕仍可按；兩鈕相鄰後同時
排入兩個錢包簽章請求的機率變高（MetaMask／Rabby 會排隊依序彈窗、部分 WalletConnect 錢包
直接拒掉第二個；兩者都是離線 personal_sign、原文各自預驗，不會交叉污染，最壞是體驗混亂）。

- `ReferralOptinCard` 新增 `disabled`（主按鈕簽署中→本卡按鈕停用）與 `onSigningChange`
  （本卡進出簽署→父層 `referralSigning`），`sign()` 改 try/finally 保證解鎖。
- 主按鈕 `disabled` 加 `referralSigning`；卡片收 `disabled={submitting}`。
- 測試追加 2 例（推薦碼簽署中主按鈕停用、失敗後解鎖；主按鈕簽署中「簽署啟用」停用）。
- 狀態：**完成**，`npm test` 716 全綠、`npm run build` 通過。

## 問題與根因

正式站 `/onboarding` step 4 的「推薦碼（選填）」是一張獨立 `step-card`，掛在含
「確認並開始跟單」主按鈕的主卡片**之後**（`web/src/components/wizard/StepConfirm.tsx`
第 179-212 行，Fragment 的第二個子元素）。主按鈕成功後 `handleSubmit` 直接
`onDone()` 導去 `/dashboard`（同檔第 175 行），使用者由上往下讀、按下主按鈕就離開頁面，
推薦碼在視覺上被排在「流程已結束」之後，實際上幾乎沒人會在按主按鈕前先簽。

上游根因：`docs/superpowers/plans/2026-09-19-referral-optin.md` Task 5 第 256 行只寫
「顯示區塊」，未指定位置，builder 自行放在按鈕下方。顯示條件（`enabled && !signed`）
本身正確，**不動**。

## 裁決（使用者 2026-09-19 選 A）

推薦碼區塊搬進主卡片，放在三個勾選框**之後**、`step-actions`（主按鈕）**之前**，
改成卡片內的子區塊樣式。文案、API、`ReferralOptinCard` 的資料流與閘門、設定頁一律不動。

---

### Task 1 `@inline`：搬移 JSX ＋ 子區塊樣式 ＋ 補 StepConfirm 測試

**Files:**
- Modify: `web/src/components/wizard/StepConfirm.tsx`
- Modify: `web/src/styles/globals.css`（`.step-actions` 定義第 659 行附近，新增一小段）
- Create: `web/src/components/wizard/StepConfirm.test.tsx`（形狀照同目錄 `StepSign.test.tsx`）

**Steps:**

- [ ] **Step 1（JSX）**：`StepConfirm` 的 return 不再用 Fragment；`<ReferralOptinCard me={me} />`
  移到 `{labels.map(...)}` 勾選框區塊之後、`{error && ...}` 之前（即主按鈕之前）。
  `ReferralOptinCard` 內部三個 `return null` 閘門與 `sign()` 邏輯**一字不改**；
  外層 `div` 的 class 由 `step-card referral-optin-card` 改為 `referral-optin`
  （它不再是獨立卡片，不得再套 `step-card`，否則會吃到 `.step-card + .step-card` 的卡間距與雙層邊框）。
  按鈕維持 `btn btn-secondary`，避免與主按鈕 `btn-primary` 混淆。
  同步更新檔頭／函式上方 docstring 裡描述位置的句子（若有）。
- [ ] **Step 2（CSS）**：`globals.css` 在 `.step-actions { margin-top: 24px; }` 之後追加：
  ```css
  /* 2026-09-19：推薦碼（選填）是 step 4 主卡片內的子區塊，放在勾選框與主按鈕之間；
     用 --surface 素色內嵌（與 .sign-card 同語彙），不套 .step-card。 */
  .referral-optin {
    margin-top: 20px;
    padding: 16px 18px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
  }
  .referral-optin .step-actions { margin-top: 12px; }
  ```
  若 `--surface` token 不存在（用 `grep -n "\-\-surface" web/src/styles/tokens.css` 確認），改用 `.sign-card` 實際使用的那個背景 token。
- [ ] **Step 3（測試）**：新建 `StepConfirm.test.tsx`。mock 方式照 `StepSign.test.tsx`：
  `vi.mock("wagmi")` 提供 `useSignMessage: () => ({ signMessageAsync: vi.fn() })`；
  `vi.mock("@/lib/api")` 覆寫 `getReferralStatus`（可控回傳）、`getMyRisk`、`getReferralMessage`、
  `submitReferralOptin`、`getLeaderSelectMessage`、`postLeaderSelect`；
  `vi.mock("@/lib/referralFlow")` 覆寫 `runReferralOptinFlow`；`vi.mock("@/lib/leaderSelectFlow")`（實際模組路徑以 StepConfirm.tsx 的 import 為準）覆寫 `runLeaderSelectFlow`。
  元件用 `useQuery`，render 時要包 `QueryClientProvider`（`new QueryClient({ defaultOptions: { queries: { retry: false } } })`）。
  至少四例：
  1. `enabled:true, signed:false, code:"JIMLAI1005"` → 找得到「推薦碼（選填）」與「簽署啟用」鈕，且推薦碼區塊在 DOM 順序上**位於**「確認並開始跟單」鈕之前
     （`referralNode.compareDocumentPosition(primaryBtn) & Node.DOCUMENT_POSITION_FOLLOWING` 為真），且兩者在同一個 `.step-card` 內（`primaryBtn.closest(".step-card") === referralNode.closest(".step-card")`）。
  2. `enabled:false` → 查無「推薦碼（選填）」；主按鈕仍在。
  3. `enabled:true, signed:true` → 查無「推薦碼（選填）」；主按鈕仍在。
  4. 例 1 狀態下點「簽署啟用」、`runReferralOptinFlow` 回 `{ ok:true }` → 出現「已簽署，引擎會在下一輪套用。」；`runLeaderSelectFlow` **未**被呼叫；主按鈕仍在頁面上。
- [ ] **Step 4**：`export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH" && cd web && npm test` 全綠；`npm run build`（或專案既有 typecheck 指令）通過。
- [ ] **Step 5**：commit `fix(web): 推薦碼區塊移進費用確認主卡片、置於主按鈕之前`。

**驗收指令**（主線程親跑）：
- `cd web && npm test` 全綠，且輸出含 `StepConfirm.test.tsx` 4 passed。
- `grep -n "ReferralOptinCard me" web/src/components/wizard/StepConfirm.tsx` 命中行號 **小於** `grep -n "btn-primary" web/src/components/wizard/StepConfirm.tsx` 的行號。
- `grep -c "referral-optin-card" web/src/components/wizard/StepConfirm.tsx` 為 0；`grep -c "^\.referral-optin {" web/src/styles/globals.css` 為 1。

## 部署

純前端改動，不影響探索快照；照 `deploy/RUNBOOK.md` 前端一般部署步驟即可，不需 §5.8c 預熱。
