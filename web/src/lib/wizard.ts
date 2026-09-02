/**
 * lib/wizard.ts — onboarding 四步 wizard 狀態機（純函式，斷點續走）＋ localStorage
 * 進度存取（Task 10 重寫；沿舊版「鏈上事實 > 本地狀態」原則，見 deriveStep）。
 *
 * 四步（設計稿 §05）：01 選擇策略（進頁即完成態，不參與本狀態機）→
 * 02 連接與授權（agent＋builder fee 簽署＋入金檢查，鏈上事實＝`OnboardStatus.state`）
 * → 03 風險限制（投入比例接真實 `/api/me/capital` 簽章流——Task 10b 主線程裁決；
 * 槓桿上限改唯讀資訊列，非使用者可調；回撤自動停止 opt-in 簽章。step3 完成與否
 * 是本地旗標，沒有對應的伺服器欄位——已簽章的事實一律以伺服器狀態為準）
 * → 04 確認（費用試算＋三條 checkbox＋送出）。
 *
 * ⭐ NOTE 11（斷點續作）：localStorage 只存 **UI 進度**（目前到第幾步、滑桿數值、
 * 是否已完成 step3），**不存簽章內容**——已簽章的事實一律以伺服器狀態
 * （`OnboardStatus`／`/api/me/risk`）為準，本地存的只是「畫面該停在哪」。
 */
import type { OnboardStatus } from "./api";

export type WizardStep = 2 | 3 | 4;

export interface WizardInputs {
  status: OnboardStatus | null;
  /** step3（風險限制）是否已按過「前往費用與風險確認」——純本地旗標。 */
  step3Confirmed: boolean;
}

/**
 * 鏈上事實（`status.state`）優先於本地旗標：agent／fee／入金任一還沒到位，
 * 一律停在 step 2，即使本地曾經標記 step3 已confirmed（例如切換錢包、或
 * 伺服器端資料被管理員動過）——不讓一個過期的本地旗標把使用者帶去一個
 * 前置條件還沒滿足的步驟。
 */
export function deriveStep(i: WizardInputs): WizardStep {
  const ready = i.status?.state === "READY";
  if (!ready) return 2;
  if (!i.step3Confirmed) return 3;
  return 4;
}

export interface WizardProgress {
  /** 登入地址（小寫），續作前必須與目前 session 一致，否則視為別人的殘留進度。 */
  address: string;
  /** 目前 onboarding 的策略參數（slug 或 `advanced:0x…`），續作前必須一致。 */
  strategy: string;
  scale: number;
  ddEnabled: boolean;
  ddPct: number;
  step3Confirmed: boolean;
  /**
   * step 2（連接與授權）是否已經成功呼叫過 `POST /api/onboard/verify` 一次
   * ——T10（2026-09-02）：精靈只靠 `status.state === "READY"` 判斷可以跳過
   * step 2，但客戶可能是「重新整理／換頁」跳過了唯一會寫 pending.json 的
   * verify 呼叫。`onboarding/page.tsx` 用這個旗標決定：載入時已 READY 但這裡
   * 仍是 false → 先自動補打一次 verify，成功才放行到 step 3。
   */
  step2Verified: boolean;
}

const STORAGE_KEY = "filet_onboarding";

/**
 * 讀回進度。位址或策略對不上目前 session／查詢參數 → 視為不相關的殘留資料，
 * 回傳 `null`（不續作，也不主動清除——留給呼叫端在真的要覆蓋時才清）。
 * 格式壞掉（手改過的 localStorage、舊版 schema）→ 同樣回傳 `null`（fail safe：
 * 讀不到就從頭來，不猜測殘缺欄位的值）。
 */
export function loadWizardProgress(address: string, strategy: string): WizardProgress | null {
  let raw: string | null;
  try {
    raw = localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (parsed == null || typeof parsed !== "object") return null;
  const p = parsed as Partial<WizardProgress>;
  if (
    typeof p.address !== "string" || typeof p.strategy !== "string"
    || p.address.toLowerCase() !== address.toLowerCase() || p.strategy !== strategy
    || typeof p.scale !== "number"
    || typeof p.ddEnabled !== "boolean" || typeof p.ddPct !== "number"
    || typeof p.step3Confirmed !== "boolean"
    // ⭐ step2Verified 缺鍵（舊格式，T10 之前存的進度）視為合法、預設 false
    // ——不讓一個新加的欄位把舊 localStorage 判成壞格式而整個從頭來；型別錯
    // （存在但不是 boolean）仍視為壞格式，同其餘欄位。
    || (p.step2Verified !== undefined && typeof p.step2Verified !== "boolean")
  ) return null;
  return {
    address: p.address.toLowerCase(), strategy: p.strategy, scale: p.scale,
    ddEnabled: p.ddEnabled, ddPct: p.ddPct, step3Confirmed: p.step3Confirmed,
    step2Verified: p.step2Verified === true,
  };
}

/** 寫入失敗（隱私模式等）→ 靜默忽略：進度續作是體驗優化，不是正確性條件。 */
export function saveWizardProgress(p: WizardProgress): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...p, address: p.address.toLowerCase() }));
  } catch {
    // ignore
  }
}

/** 完成或撤銷時清除（NOTE 11）。 */
export function clearWizardProgress(): void {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore
  }
}
