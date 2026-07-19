import { describe, expect, it, vi } from "vitest";
import type { LeaderSelectMessageResp, LeaderSelectResp } from "./api";
import { runLeaderSelectFlow, type LeaderSelectDeps } from "./leaderSelectFlow";

/** 伺服器產生的 canonical 原文（版型見 filet/leader_change.py；此處只需原樣傳遞）。 */
const PAYLOAD: LeaderSelectMessageResp = {
  message:
    "Filet: change copy-trading leader\n\n" +
    "Account: fabc\nLeader: 0x1111111111111111111111111111111111111111\n" +
    "Nonce: n-1\nIssued At: 2026-07-19T00:00:00Z",
  nonce: "n-1",
  issued_at: "2026-07-19T00:00:00Z",
  leader_address: "0x1111111111111111111111111111111111111111",
  account_id: "fabc",
};
const SIGNER = "0xAbC0000000000000000000000000000000000001";
const SIG = `0x${"ab".repeat(65)}`;
const OK_RESP: LeaderSelectResp = {
  ok: true, account_id: "fabc", leader_address: PAYLOAD.leader_address,
  effective: "next_engine_cycle", effective_note: "下一個 cycle 生效", consequences: "會收斂部位",
};

function deps(over: Partial<LeaderSelectDeps> = {}) {
  return {
    fetchMessage: vi.fn(async () => PAYLOAD),
    signMessage: vi.fn(async () => SIG),
    recover: vi.fn(async () => SIGNER.toLowerCase()),
    submit: vi.fn(async () => OK_RESP),
    ...over,
  };
}

/** 使用者實際點的那位 leader（＝ PAYLOAD 正常情況下指向的人）。 */
const SELECTED = PAYLOAD.leader_address;

describe("runLeaderSelectFlow ⭐（沿 approvalFlow 的謹慎度）", () => {
  it("happy path：簽伺服器原文 → recover 相符 → 整包 payload 原樣送出", async () => {
    const d = deps();
    const r = await runLeaderSelectFlow(d, { expectedSigner: SIGNER, expectedLeader: SELECTED });

    expect(r).toEqual({ ok: true, resp: OK_RESP });
    // ⭐ 簽的必須是伺服器原文本身（一個位元組差異就會被後端拒絕）
    expect(d.signMessage).toHaveBeenCalledWith(PAYLOAD.message);
    // ⭐ 送出的是同一個 payload 物件——前端沒有機會從別處拼欄位
    expect(d.submit).toHaveBeenCalledWith(PAYLOAD, SIG);
  });

  it("⭐ recover 出的簽章者 ≠ 登入地址 → 中止，且完全不送出（零網路請求）", async () => {
    const d = deps({ recover: vi.fn(async () => "0x9999999999999999999999999999999999999999") });
    const r = await runLeaderSelectFlow(d, { expectedSigner: SIGNER, expectedLeader: SELECTED });

    expect(r).toEqual({ ok: false, kind: "signer-mismatch" });
    expect(d.submit).not.toHaveBeenCalled();
  });

  it("⭐ recover 本身失敗（簽名格式壞）→ fail closed 視為不符，同樣不送出", async () => {
    const d = deps({ recover: vi.fn(async () => { throw new Error("bad signature"); }) });
    const r = await runLeaderSelectFlow(d, { expectedSigner: SIGNER, expectedLeader: SELECTED });

    expect(r).toEqual({ ok: false, kind: "signer-mismatch" });
    expect(d.submit).not.toHaveBeenCalled();
  });

  it("地址大小寫不同視為相同（recover 與登入地址一律小寫比對）", async () => {
    const d = deps({ recover: vi.fn(async () => SIGNER.toUpperCase()) });
    const r = await runLeaderSelectFlow(d, {
      expectedSigner: SIGNER.toLowerCase(),
      expectedLeader: SELECTED,
    });

    expect(r.ok).toBe(true);
    expect(d.submit).toHaveBeenCalledTimes(1);
  });

  it("錢包拒絕 → wallet-rejected，不 recover、不送出", async () => {
    const d = deps({ signMessage: vi.fn(async () => { throw new Error("User rejected"); }) });
    const r = await runLeaderSelectFlow(d, { expectedSigner: SIGNER, expectedLeader: SELECTED });

    expect(r).toEqual({ ok: false, kind: "wallet-rejected" });
    expect(d.recover).not.toHaveBeenCalled();
    expect(d.submit).not.toHaveBeenCalled();
  });

  it("取原文失敗 → message-failed，不叫錢包（不浪費使用者一次簽名）", async () => {
    const err = new Error("boom");
    const d = deps({ fetchMessage: vi.fn(async () => { throw err; }) });
    const r = await runLeaderSelectFlow(d, { expectedSigner: SIGNER, expectedLeader: SELECTED });

    expect(r).toEqual({ ok: false, kind: "message-failed", error: err });
    expect(d.signMessage).not.toHaveBeenCalled();
  });

  it("⭐ 送出失敗**不自動重試**（非冪等寫入 ＋ nonce 一次性）：submit 只被呼叫一次", async () => {
    const err = new Error("500");
    const d = deps({ submit: vi.fn(async () => { throw err; }) });
    const r = await runLeaderSelectFlow(d, { expectedSigner: SIGNER, expectedLeader: SELECTED });

    expect(r).toEqual({ ok: false, kind: "submit-failed", error: err });
    expect(d.submit).toHaveBeenCalledTimes(1);
  });
});

/**
 * ⭐ 被打穿的 filet-api 想無中生有一次換手，唯一的著力點就是這裡：回一份指向別人的
 * 待簽原文。使用者在錢包裡看到的只有一串 hex，簽下去之後每一關（recover、後端重建
 * 驗簽、manifest 白名單）都會放行——因為那份簽章確實是本人簽的、確實指向一個合法的
 * leader，只是不是他要的那一個。攔截點必須在**進錢包之前**：簽章一旦產生就已經是
 * 一份有效授權，事後才發現不符已經來不及。
 */
describe("runLeaderSelectFlow — leader 預驗 ⭐（伺服器指定的對象必須是使用者選的）", () => {
  const EVIL = "0xEEEE000000000000000000000000000000000EEE";

  it("⭐ leader_address ≠ 使用者所選 → 中止：不喚起錢包、零網路請求", async () => {
    const d = deps({
      fetchMessage: vi.fn(async () => ({
        ...PAYLOAD,
        leader_address: EVIL,
        message: PAYLOAD.message.replace(PAYLOAD.leader_address, EVIL),
      })),
    });
    const r = await runLeaderSelectFlow(d, { expectedSigner: SIGNER, expectedLeader: SELECTED });

    expect(r).toEqual({ ok: false, kind: "leader-mismatch" });
    // 錢包完全沒有被喚起——使用者連一次「看起來很正常」的簽名請求都不該看到
    expect(d.signMessage).not.toHaveBeenCalled();
    expect(d.recover).not.toHaveBeenCalled();
    expect(d.submit).not.toHaveBeenCalled();
  });

  it("⭐ 第二道：leader_address 對，但 message 本體指向別人 → 同樣中止", async () => {
    // 欄位對得上、使用者在錢包裡看到的卻是另一個位址。後端若改成信任客戶端 message，
    // 或使用者據以判斷「我簽的是誰」，這個縫就會被利用。
    const d = deps({
      fetchMessage: vi.fn(async () => ({
        ...PAYLOAD,
        message: PAYLOAD.message.replace(PAYLOAD.leader_address, EVIL),
      })),
    });
    const r = await runLeaderSelectFlow(d, { expectedSigner: SIGNER, expectedLeader: SELECTED });

    expect(r).toEqual({ ok: false, kind: "leader-mismatch" });
    expect(d.signMessage).not.toHaveBeenCalled();
    expect(d.submit).not.toHaveBeenCalled();
  });

  it("⭐ 第一道：message 本體對，但 leader_address 欄位指向別人 → 同樣中止", async () => {
    // 上一條的對偶。這一側才是**真正生效**的那一側：後端是拿 `leader_address`
    // 重建訊息來驗簽的，訊息本體它根本不看。所以這種竄改對使用者是隱形的——
    // 錢包裡顯示的原文從頭到尾指向他選的人，欄位卻指向 0xEVIL。
    // 只比對 message 本體的預驗會整條放行這一種（兩邊都比對的理由就在這裡）。
    const d = deps({
      fetchMessage: vi.fn(async () => ({ ...PAYLOAD, leader_address: EVIL })),
    });
    const r = await runLeaderSelectFlow(d, { expectedSigner: SIGNER, expectedLeader: SELECTED });

    expect(r).toEqual({ ok: false, kind: "leader-mismatch" });
    expect(d.signMessage).not.toHaveBeenCalled();
    expect(d.recover).not.toHaveBeenCalled();
    expect(d.submit).not.toHaveBeenCalled();
  });

  it("大小寫不同但實為同一位址 → 正常通過（位址大小寫不敏感，不得誤擋）", async () => {
    // 使用者端拿到 checksum 形式、伺服器版型正規化成小寫：這是**正常**情況，
    // 誤擋在這裡會讓每一次換 leader 都失敗（且看起來像遭到攻擊）。
    const d = deps({
      fetchMessage: vi.fn(async () => ({
        ...PAYLOAD,
        leader_address: PAYLOAD.leader_address.toLowerCase(),
      })),
    });
    const r = await runLeaderSelectFlow(d, {
      expectedSigner: SIGNER,
      expectedLeader: SELECTED.toUpperCase(),
    });

    expect(r.ok).toBe(true);
    expect(d.signMessage).toHaveBeenCalledTimes(1);
    expect(d.submit).toHaveBeenCalledTimes(1);
  });
});
