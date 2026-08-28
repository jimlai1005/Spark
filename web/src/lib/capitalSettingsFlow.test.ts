import { describe, expect, it, vi } from "vitest";
import type { CapitalSettingsMessageResp, CapitalSettingsResp } from "./api";
import { runCapitalSettingsFlow, type CapitalSettingsDeps } from "./capitalSettingsFlow";

const ACCOUNT = "fabc";
const SIGNER = "0xAbC0000000000000000000000000000000000001";
const SIG = `0x${"ab".repeat(65)}`;

/** 逐字照抄後端 `build_capital_settings_message`（filet/capital_settings.py）版型。 */
function messageFor(util: string): string {
  return (
    "Filet: update copy-trading capital allocation\n\n"
    + "Signing this authorises Filet to change how much capital mirrors your leader.\n"
    + "These values scale your position sizes directly: a higher utilisation means\n"
    + "larger positions and a closer liquidation distance.\n"
    + "It takes effect on the engine's next cycle. No immediate forced rebalance is\n"
    + "performed; positions converge naturally as the leader trades.\n\n"
    + `Account: ${ACCOUNT}\n`
    + "Allocated Capital: full account equity\n"
    + `Capital Utilization: ${util}\n`
    + "Nonce: n-1\nIssued At: 2026-08-28T00:00:00Z"
  );
}

const PAYLOAD: CapitalSettingsMessageResp = {
  message: messageFor("0.2500"),
  nonce: "n-1",
  issued_at: "2026-08-28T00:00:00Z",
  account_id: ACCOUNT,
  allocated_capital: "0.00",
  capital_utilization: "0.2500",
  use_full_equity: true,
};

const OK_RESP: CapitalSettingsResp = {
  ok: true, account_id: ACCOUNT, allocated_capital: "0.00", capital_utilization: "0.2500",
  use_full_equity: true, effective: "next_engine_cycle", effective_note: "下一個 cycle 生效。",
  consequences: "不會立即強制再平衡。",
};

function deps(over: Partial<CapitalSettingsDeps> = {}): CapitalSettingsDeps & {
  fetchMessage: ReturnType<typeof vi.fn>;
  signMessage: ReturnType<typeof vi.fn>;
  recover: ReturnType<typeof vi.fn>;
  submit: ReturnType<typeof vi.fn>;
} {
  return {
    fetchMessage: vi.fn(async () => PAYLOAD),
    signMessage: vi.fn(async () => SIG),
    recover: vi.fn(async () => SIGNER.toLowerCase()),
    submit: vi.fn(async () => OK_RESP),
    ...over,
  } as never;
}

const OPTS = {
  expectedSigner: SIGNER,
  expectedAccountId: ACCOUNT,
  expectedAllocatedCapital: "0",
  expectedCapitalUtilization: "0.25",
  expectedUseFullEquity: true,
};

describe("runCapitalSettingsFlow", () => {
  it("happy path：原文原樣進錢包，成功送出", async () => {
    const d = deps();
    const r = await runCapitalSettingsFlow(d, OPTS);
    expect(r).toEqual({ ok: true, resp: OK_RESP });
    expect(d.signMessage).toHaveBeenCalledWith(PAYLOAD.message);
    expect(d.submit).toHaveBeenCalledWith(PAYLOAD, SIG);
  });

  it("fetchMessage 失敗 → message-failed，不喚起錢包", async () => {
    const d = deps({ fetchMessage: vi.fn().mockRejectedValue(new Error("x")) });
    const r = await runCapitalSettingsFlow(d, OPTS);
    expect(r).toEqual({ ok: false, kind: "message-failed", error: expect.any(Error) });
    expect(d.signMessage).not.toHaveBeenCalled();
  });

  it("伺服器回聲的 account_id 與期望不符 → content-mismatch，零網路請求（不喚起錢包）", async () => {
    const d = deps({
      fetchMessage: vi.fn(async () => ({ ...PAYLOAD, account_id: "other" })),
    });
    const r = await runCapitalSettingsFlow(d, OPTS);
    expect(r).toEqual({ ok: false, kind: "content-mismatch" });
    expect(d.signMessage).not.toHaveBeenCalled();
  });

  it("伺服器回聲的 capital_utilization 與期望不符（例如被打穿的 API 想拉滿曝險）→ content-mismatch", async () => {
    const d = deps({
      fetchMessage: vi.fn(async () => ({
        ...PAYLOAD, capital_utilization: "1.0000", message: messageFor("1.0000"),
      })),
    });
    const r = await runCapitalSettingsFlow(d, OPTS);
    expect(r).toEqual({ ok: false, kind: "content-mismatch" });
  });

  it("原文本體沒有寫著期望的 utilization（欄位對、原文不對）→ content-mismatch", async () => {
    const d = deps({
      fetchMessage: vi.fn(async () => ({ ...PAYLOAD, message: messageFor("0.9900") })),
    });
    const r = await runCapitalSettingsFlow(d, OPTS);
    expect(r).toEqual({ ok: false, kind: "content-mismatch" });
  });

  it("錢包拒簽 → wallet-rejected", async () => {
    const d = deps({ signMessage: vi.fn().mockRejectedValue(new Error("rejected")) });
    const r = await runCapitalSettingsFlow(d, OPTS);
    expect(r).toEqual({ ok: false, kind: "wallet-rejected" });
    expect(d.submit).not.toHaveBeenCalled();
  });

  it("recover 位址與登入位址不符 → signer-mismatch，不送出", async () => {
    const d = deps({ recover: vi.fn(async () => "0xdead000000000000000000000000000000dead") });
    const r = await runCapitalSettingsFlow(d, OPTS);
    expect(r).toEqual({ ok: false, kind: "signer-mismatch" });
    expect(d.submit).not.toHaveBeenCalled();
  });

  it("submit 失敗 → submit-failed，不自動重試", async () => {
    const d = deps({ submit: vi.fn().mockRejectedValue(new Error("500")) });
    const r = await runCapitalSettingsFlow(d, OPTS);
    expect(r).toEqual({ ok: false, kind: "submit-failed", error: expect.any(Error) });
  });
});
