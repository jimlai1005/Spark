import { beforeEach, describe, expect, it } from "vitest";
import type { OnboardStatus } from "./api";
import {
  deriveStep,
  getRiskConfirmed,
  setRiskConfirmed,
  threadPercent,
} from "./wizard";

function status(over: Partial<OnboardStatus> = {}): OnboardStatus {
  return {
    address: "0xabc", account_id: "fabc", agent_address: null,
    agent_generated: false, builder_fee_approved: false,
    agent_approved: false, funded: false, state: "IN_PROGRESS",
    perp_account_value: "0", min_deposit: "100", deposit_shortfall: "100",
    ...over,
  };
}

describe("deriveStep（斷點續走矩陣）", () => {
  it("未登入 → 1（session 是身分權威；錢包連線狀態不參與推導——設計定案 17）", () => {
    expect(deriveStep({ loggedIn: false, riskConfirmed: false, status: null })).toBe(1);
    expect(deriveStep({ loggedIn: false, riskConfirmed: true, status: status() })).toBe(1);
  });
  it("已登入、未勾風險 → 2", () => {
    expect(deriveStep({ loggedIn: true, riskConfirmed: false, status: status() })).toBe(2);
    expect(deriveStep({ loggedIn: true, riskConfirmed: false, status: null })).toBe(2);
  });
  it("已勾風險、雙授權未齊 → 3（含只簽了一筆的殘局）", () => {
    expect(deriveStep({ loggedIn: true, riskConfirmed: true, status: status() })).toBe(3);
    expect(deriveStep({
      loggedIn: true, riskConfirmed: true,
      status: status({ agent_generated: true, agent_address: "0xa", agent_approved: true }),
    })).toBe(3);
  });
  it("鏈上雙授權已生效 → 4（覆蓋本地風險狀態——已簽即已越過風險閘）", () => {
    const s = status({ agent_generated: true, agent_address: "0xa", agent_approved: true, builder_fee_approved: true });
    expect(deriveStep({ loggedIn: true, riskConfirmed: false, status: s })).toBe(4);
    expect(deriveStep({ loggedIn: true, riskConfirmed: true, status: s })).toBe(4);
  });
});

describe("threadPercent（照 v1 原型：step<3→0；step3 每簽一筆 +50；step4→100）", () => {
  it("step 1/2 → 0", () => {
    expect(threadPercent(1, status())).toBe(0);
    expect(threadPercent(2, status())).toBe(0);
  });
  it("step 3 依鏈上簽署數 0/50/100", () => {
    expect(threadPercent(3, status())).toBe(0);
    expect(threadPercent(3, status({ agent_approved: true }))).toBe(50);
    expect(threadPercent(3, status({ builder_fee_approved: true }))).toBe(50);
    expect(threadPercent(3, status({ agent_approved: true, builder_fee_approved: true }))).toBe(100);
    expect(threadPercent(3, null)).toBe(0);
  });
  it("step 4 → 100", () => {
    expect(threadPercent(4, status())).toBe(100);
  });
});

describe("riskConfirmed 持久化（localStorage per-address）", () => {
  beforeEach(() => localStorage.clear());
  it("預設 false；set 後 true；地址間互不影響；大小寫同一 key", () => {
    expect(getRiskConfirmed("0xAAA0000000000000000000000000000000000001")).toBe(false);
    setRiskConfirmed("0xAAA0000000000000000000000000000000000001");
    expect(getRiskConfirmed("0xaaa0000000000000000000000000000000000001")).toBe(true);
    expect(getRiskConfirmed("0xBBB0000000000000000000000000000000000002")).toBe(false);
  });
});
