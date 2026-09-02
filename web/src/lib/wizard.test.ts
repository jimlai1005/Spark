import { beforeEach, describe, expect, it } from "vitest";
import type { OnboardStatus } from "./api";
import {
  clearWizardProgress,
  deriveStep,
  loadWizardProgress,
  saveWizardProgress,
  type WizardProgress,
} from "./wizard";

function status(over: Partial<OnboardStatus> = {}): OnboardStatus {
  return {
    address: "0xabc", account_id: "fabc", agent_address: null,
    agent_generated: false, builder_fee_approved: false,
    agent_approved: false, funded: false, spot_stranded: null, state: "IN_PROGRESS",
    perp_account_value: "0", min_deposit: "100", deposit_shortfall: "100",
    ...over,
  };
}

function progress(over: Partial<WizardProgress> = {}): WizardProgress {
  return {
    address: "0xabc0000000000000000000000000000000000001",
    strategy: "core", scale: 25, ddEnabled: false, ddPct: 20,
    step3Confirmed: false, step2Verified: false,
    ...over,
  };
}

describe("deriveStep（斷點續走矩陣，Task 10：2→3→4）", () => {
  it("狀態未 READY → 2（不論本地 step3Confirmed 為何）", () => {
    expect(deriveStep({ status: status(), step3Confirmed: false })).toBe(2);
    expect(deriveStep({ status: status(), step3Confirmed: true })).toBe(2);
    expect(deriveStep({ status: null, step3Confirmed: true })).toBe(2);
  });
  it("READY 且未 confirm step3 → 3", () => {
    expect(deriveStep({ status: status({ state: "READY" }), step3Confirmed: false })).toBe(3);
  });
  it("READY 且已 confirm step3 → 4", () => {
    expect(deriveStep({ status: status({ state: "READY" }), step3Confirmed: true })).toBe(4);
  });
});

describe("wizard 進度 localStorage（NOTE 11：只存 UI 進度，不存簽章內容）", () => {
  beforeEach(() => localStorage.clear());

  it("寫入後可讀回，且不含任何簽章欄位（不變量 1 的前端鏡射）", () => {
    saveWizardProgress(progress());
    const raw = localStorage.getItem("filet_onboarding")!;
    expect(raw).not.toMatch(/signature|message/i);
    const back = loadWizardProgress(
      "0xabc0000000000000000000000000000000000001", "core",
    );
    expect(back).toEqual(progress());
  });

  it("位址不符（另一個錢包登入）→ 視為不相關，回傳 null", () => {
    saveWizardProgress(progress());
    expect(loadWizardProgress("0xdead000000000000000000000000000000dead", "core")).toBeNull();
  });

  it("策略不符（換了另一個策略）→ 回傳 null", () => {
    saveWizardProgress(progress());
    expect(loadWizardProgress(
      "0xabc0000000000000000000000000000000000001", "other",
    )).toBeNull();
  });

  it("位址比對不分大小寫", () => {
    saveWizardProgress(progress({ address: "0xABC0000000000000000000000000000000000001" }));
    expect(loadWizardProgress(
      "0xabc0000000000000000000000000000000000001", "core",
    )).not.toBeNull();
  });

  it("格式壞掉（手改過的殘缺 JSON）→ 回傳 null，不猜測欄位", () => {
    localStorage.setItem("filet_onboarding", JSON.stringify({ address: "0xabc", strategy: "core" }));
    expect(loadWizardProgress("0xabc", "core")).toBeNull();
  });

  it("clearWizardProgress 後讀回 null", () => {
    saveWizardProgress(progress());
    clearWizardProgress();
    expect(loadWizardProgress(
      "0xabc0000000000000000000000000000000000001", "core",
    )).toBeNull();
  });

  // ⭐ T10（2026-09-02）：step2Verified 是後補的欄位，舊格式（T10 之前存的
  // localStorage）缺這個鍵，缺鍵視為 false，不得整個判成壞格式而從頭來。
  it("舊格式缺 step2Verified → 視為 false，仍可續作（向後相容）", () => {
    const legacy = {
      address: "0xabc0000000000000000000000000000000000001",
      strategy: "core", scale: 25, ddEnabled: false, ddPct: 20,
      step3Confirmed: true,
      // 刻意不含 step2Verified 鍵
    };
    localStorage.setItem("filet_onboarding", JSON.stringify(legacy));
    const back = loadWizardProgress("0xabc0000000000000000000000000000000000001", "core");
    expect(back).not.toBeNull();
    expect(back?.step2Verified).toBe(false);
    expect(back?.step3Confirmed).toBe(true);
  });

  it("step2Verified 存在但型別錯誤（非 boolean）→ 回傳 null", () => {
    const bad = { ...progress(), step2Verified: "yes" };
    localStorage.setItem("filet_onboarding", JSON.stringify(bad));
    expect(loadWizardProgress(
      "0xabc0000000000000000000000000000000000001", "core",
    )).toBeNull();
  });
});
