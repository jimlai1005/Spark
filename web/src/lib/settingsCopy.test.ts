/**
 * M3 round4 Task R4-4：`paramCopyOf`／`capitalNoteOf`／`leaderNoteOf` 三個顯示層
 * 對照函式的單元測試——新檔，不改既有 `page.test.tsx`。涵蓋：五個封閉列舉 name／
 * status 一律取 copy.ts 雙語表；查無對應 key 才 fallback 伺服器原文（不炸畫面）。
 */
import { describe, expect, it } from "vitest";
import type { MyCapitalResp, MyLeaderResp, RiskParamSpec } from "@/lib/api";
import { COPY_EN, COPY_ZH } from "@/lib/copy";
import { capitalNoteOf, leaderNoteOf, paramCopyOf } from "@/lib/settingsCopy";

function specOf(name: string, label: string, help: string): RiskParamSpec {
  return {
    name: name as RiskParamSpec["name"], env: "X", type: "decimal", group: "risk",
    default: "0.1", recommended: "0.1", min: "0", max: "1", label, help,
  };
}

describe("paramCopyOf — RiskParamSpec 雙語對照＋fallback", () => {
  it("已知 name（max_drawdown_pct）→ 一律用 copy.ts 的 label/help，忽略後端原文", () => {
    const spec = specOf("max_drawdown_pct", "後端占位 label", "後端占位 help");
    const zh = paramCopyOf(spec, COPY_ZH.settings.risk);
    expect(zh.label).toBe(COPY_ZH.settings.risk.paramLabels.max_drawdown_pct.label);
    expect(zh.label).not.toBe("後端占位 label");

    const en = paramCopyOf(spec, COPY_EN.settings.risk);
    expect(en.label).toBe(COPY_EN.settings.risk.paramLabels.max_drawdown_pct.label);
    expect(en.help).not.toBe("後端占位 help");
  });

  it("未知 name（未來新參數）→ fallback 後端 spec.label/spec.help，不炸", () => {
    const spec = specOf("future_param", "未來參數 label", "未來參數 help");
    const zh = paramCopyOf(spec, COPY_ZH.settings.risk);
    expect(zh).toEqual({ label: "未來參數 label", help: "未來參數 help" });
  });
});

describe("capitalNoteOf — MyCapitalResp.note 依 status 取雙語，查無才 fallback", () => {
  const base: MyCapitalResp = {
    account_id: "fabc", status: "effective",
    effective: null, pending: null, heartbeat: null,
    note: "後端占位 note",
  };

  it("已知 status（effective）→ 用 copy.ts，不用後端原文", () => {
    const got = capitalNoteOf(base, COPY_ZH.settings.capital);
    expect(got).toBe(COPY_ZH.settings.capital.notesByStatus.effective);
    expect(got).not.toBe("後端占位 note");
  });

  it("未知 status → fallback 後端 note", () => {
    const got = capitalNoteOf(
      { ...base, status: "future_status" as MyCapitalResp["status"] },
      COPY_ZH.settings.capital,
    );
    expect(got).toBe("後端占位 note");
  });
});

describe("leaderNoteOf — MyLeaderResp.note 依 status 取雙語，查無才 fallback", () => {
  const base: MyLeaderResp = {
    account_id: "fabc", status: "following",
    leader_address: "0x1", leader_name: "Alpha", pending_change: null,
    note: "後端占位 note",
  };

  it("已知 status（following）→ 用 copy.ts，不用後端原文", () => {
    const got = leaderNoteOf(base, COPY_EN.settings.leader);
    expect(got).toBe(COPY_EN.settings.leader.notesByStatus.following);
    expect(got).not.toBe("後端占位 note");
  });

  it("未知 status → fallback 後端 note", () => {
    const got = leaderNoteOf(
      { ...base, status: "future_status" as MyLeaderResp["status"] },
      COPY_ZH.settings.leader,
    );
    expect(got).toBe("後端占位 note");
  });
});
