"use client";
import { CapabilityMatrix } from "@/components/CapabilityMatrix";
import type { OnboardStatus } from "@/lib/api";
import { useCopy } from "@/lib/lang";
import { StepDeposit } from "./StepDeposit";
import { StepSign } from "./StepSign";

/**
 * StepConnect — onboarding step 2（設計稿 §05：「連接與授權」）。
 *
 * ⭐ 只換殼、不換簽署邏輯（Task 10 規格）：`StepSign`（agent＋builder fee 兩筆簽署）
 * 與 `StepDeposit`（入金檢查＋送出綁定）兩個既有元件原封不動巢狀在本步之下——
 * 兩者各自的 API 呼叫、狀態機、錯誤處理完全不變，本元件只提供合併後的單一
 * eyebrow/標題（設計稿每個步驟面板只有一個標題）與能力矩陣精簡版。
 *
 * 完成條件（父層 `deriveStep` 判定）＝ `status.state === "READY"`
 * （agent_approved && builder_fee_approved && funded 的伺服器端投影）。
 */
export function StepConnect({ status, loginAddress, refetchStatus }: {
  status: OnboardStatus;
  loginAddress: string;
  refetchStatus: () => void;
}) {
  const COPY = useCopy();
  const c = COPY.wizard;

  return (
    <div className="wizard-step-connect">
      <p className="eyebrow">02・{c.stepNames[1]}</p>
      <h2>{c.step2Title}</h2>
      <p className="hint">{c.step2Body}</p>
      <CapabilityMatrix />
      {/* ⭐ StepSign／StepDeposit 各自保留自己的 `.step-card` 外框（既有樣式），
          本層刻意不再包一層 `.step-card`，避免視覺上雙層邊框巢狀。 */}
      <StepSign status={status} loginAddress={loginAddress} refetchStatus={refetchStatus} />
      <StepDeposit status={status} refetchStatus={refetchStatus} />
    </div>
  );
}
