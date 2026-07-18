/**
 * lib/wizard.ts — onboarding wizard 狀態機（純函式，斷點續走）。
 * 原則：鏈上事實（後端 status，唯一真相）> 本地狀態（localStorage 風險勾選）。
 * 4 階段沿 spec：01 連接錢包 → 02 風險確認 → 03 簽署授權 → 04 入金送審。
 */
import type { OnboardStatus } from "./api";

export type WizardStep = 1 | 2 | 3 | 4;

export interface WizardInputs {
  loggedIn: boolean;
  riskConfirmed: boolean;
  status: OnboardStatus | null;
}

export function deriveStep(i: WizardInputs): WizardStep {
  // 設計定案 17：session 是身分權威——錢包鎖住/斷連不把使用者退回 step 1；
  // 簽署所需的錢包連線由 UI 層在 step 3/4 以「重連閘」處理（onboarding/page.tsx）。
  if (!i.loggedIn) return 1;
  // 鏈上雙授權已生效 → 風險閘已被「實際簽署」越過，直接入金階段（設計定案 4）
  if (i.status && i.status.agent_approved && i.status.builder_fee_approved) return 4;
  if (!i.riskConfirmed) return 2;
  return 3;
}

/** 授權絲線填滿比例（照 v1 原型語意；簽署數出自鏈上 status，非本地猜測）。 */
export function threadPercent(step: WizardStep, status: OnboardStatus | null): number {
  if (step < 3) return 0;
  if (step === 3) {
    const n = (status?.agent_approved ? 1 : 0) + (status?.builder_fee_approved ? 1 : 0);
    return n * 50;
  }
  return 100;
}

function riskKey(address: string): string {
  return `filet.risk-confirmed.${address.toLowerCase()}`;
}

export function getRiskConfirmed(address: string): boolean {
  try {
    return localStorage.getItem(riskKey(address)) === "1";
  } catch {
    return false; // SSR / storage 不可用時保守回 false（多勾一次無害）
  }
}

export function setRiskConfirmed(address: string): void {
  try {
    localStorage.setItem(riskKey(address), "1");
  } catch {
    // storage 不可用：忽略——使用者下次會再被要求勾選，安全方向的失敗
  }
}
