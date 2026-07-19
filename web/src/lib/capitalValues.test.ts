import { describe, expect, it } from "vitest";
import {
  canonicalAllocatedCapital,
  canonicalUtilizationPct,
  capitalMessageLines,
} from "./capitalValues";

describe("canonicalAllocatedCapital（鏡射後端 CAPITAL_DECIMALS = 2）", () => {
  it("補足小數位到 2 位——同一個數的三種寫法收斂成同一個字串", () => {
    // ⭐ 這就是前端也要 canonical 化的理由：不做的話 `1000` 與伺服器的 `1000.00`
    // 會被判成不符，把安全檢查變成一個每次都擋人的 bug（工程原則 1）。
    for (const raw of ["1000", "1000.0", "1000.00", " 1000 ", "0001000"]) {
      expect(canonicalAllocatedCapital(raw)).toEqual({ ok: true, value: "1000.00" });
    }
  });

  it("小數 1 位與 2 位都接受", () => {
    expect(canonicalAllocatedCapital("12.5")).toEqual({ ok: true, value: "12.50" });
    expect(canonicalAllocatedCapital("12.34")).toEqual({ ok: true, value: "12.34" });
  });

  it("⭐ 小數位超過 2 位 → 拒絕，**不四捨五入**（靜默截斷會改掉使用者要簽的數字）", () => {
    expect(canonicalAllocatedCapital("1000.005")).toEqual({ ok: false, reason: "decimals" });
    expect(canonicalAllocatedCapital("0.129")).toEqual({ ok: false, reason: "decimals" });
  });

  it("⭐ 超界：0 與各種零形式一律拒絕（後端 allocated_capital > 0，兩邊都不夾取）", () => {
    for (const raw of ["0", "0.0", "0.00", "000"]) {
      expect(canonicalAllocatedCapital(raw)).toEqual({ ok: false, reason: "not_positive" });
    }
  });

  it("格式不合法一律拒絕（負號、科學記號、千分位、單位、文字）", () => {
    for (const raw of ["-1", "1e5", "1,000", "1000 USDC", "abc", "1.2.3", "+5"]) {
      expect(canonicalAllocatedCapital(raw)).toEqual({ ok: false, reason: "format" });
    }
  });

  it("空字串回 empty（尚未輸入 ≠ 輸入錯誤，UI 據此不在一進頁面就報錯）", () => {
    expect(canonicalAllocatedCapital("")).toEqual({ ok: false, reason: "empty" });
    expect(canonicalAllocatedCapital("   ")).toEqual({ ok: false, reason: "empty" });
  });

  it("整數位數超出可表示範圍 → too_large（鏡射後端 _MAX_ABS）", () => {
    expect(canonicalAllocatedCapital("1".repeat(16))).toEqual({ ok: false, reason: "too_large" });
    expect(canonicalAllocatedCapital("1".repeat(15)).ok).toBe(true);
  });
});

describe("canonicalUtilizationPct（鏡射後端 UTILIZATION_DECIMALS = 4）", () => {
  it("整數百分比 → 固定 4 位小數的比例字串（不經過浮點）", () => {
    expect(canonicalUtilizationPct(1)).toBe("0.0100");
    expect(canonicalUtilizationPct(7)).toBe("0.0700");
    expect(canonicalUtilizationPct(20)).toBe("0.2000");
    expect(canonicalUtilizationPct(25)).toBe("0.2500");
    expect(canonicalUtilizationPct(100)).toBe("1.0000");
  });

  it("⭐ 超出 (0, 1] 對應的百分比範圍 → null（呼叫端阻擋，不夾取）", () => {
    expect(canonicalUtilizationPct(0)).toBeNull();
    expect(canonicalUtilizationPct(101)).toBeNull();
    expect(canonicalUtilizationPct(-5)).toBeNull();
  });

  it("非整數百分比 → null（滑桿以 1% 為一格；值域不靠控制項屬性成立）", () => {
    expect(canonicalUtilizationPct(20.5)).toBeNull();
    expect(canonicalUtilizationPct(Number.NaN)).toBeNull();
  });
});

describe("capitalMessageLines（版型鏡射 build_capital_settings_message）", () => {
  it("產生原文中載有兩個設定值的那兩行", () => {
    expect(capitalMessageLines({ allocated_capital: "10000.00", capital_utilization: "0.2000" }))
      .toEqual(["Allocated Capital: 10000.00 USDC", "Capital Utilization: 0.2000"]);
  });
});
