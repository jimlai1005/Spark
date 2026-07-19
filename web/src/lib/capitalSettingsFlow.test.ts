import { describe, expect, it, vi } from "vitest";
import type { CapitalSettingsMessageResp, CapitalSettingsResp } from "./api";
import { runCapitalSettingsFlow, type CapitalSettingsDeps } from "./capitalSettingsFlow";
import type { CapitalValues } from "./capitalValues";

/** 使用者實際調的那組值（已 canonical 化，與伺服器同基準）。 */
const EXPECTED: CapitalValues = {
  allocated_capital: "10000.00",
  capital_utilization: "0.2000",
};

/**
 * 伺服器產生的 canonical 原文。⭐ 版型逐行照 filet/capital_settings.py 的
 * `build_capital_settings_message`——`Allocated Capital: … USDC` 與
 * `Capital Utilization: …` 兩行不是裝飾，是前端設定值預驗的第二道比對對象。
 * fixture 若省略它們，測試就驗不到真實流程會走的那條路徑。
 */
const PAYLOAD: CapitalSettingsMessageResp = {
  message:
    "Filet: update copy-trading capital allocation\n\n" +
    "Signing this authorises Filet to change how much capital mirrors your leader.\n" +
    "\n" +
    "Account: fabc\n" +
    "Allocated Capital: 10000.00 USDC\n" +
    "Capital Utilization: 0.2000\n" +
    "Nonce: n-1\nIssued At: 2026-07-19T00:00:00Z",
  nonce: "n-1",
  issued_at: "2026-07-19T00:00:00Z",
  account_id: "fabc",
  allocated_capital: "10000.00",
  capital_utilization: "0.2000",
};
const SIGNER = "0xAbC0000000000000000000000000000000000001";
const SIG = `0x${"ab".repeat(65)}`;
const OK_RESP: CapitalSettingsResp = {
  ok: true, account_id: "fabc",
  allocated_capital: "10000.00", capital_utilization: "0.2000",
  effective: "next_engine_cycle",
  effective_note: "下一個 cycle 生效",
  consequences: "不會立即強制再平衡",
};

function deps(over: Partial<CapitalSettingsDeps> = {}) {
  return {
    fetchMessage: vi.fn(async () => PAYLOAD),
    signMessage: vi.fn(async () => SIG),
    recover: vi.fn(async () => SIGNER.toLowerCase()),
    submit: vi.fn(async () => OK_RESP),
    ...over,
  };
}

describe("runCapitalSettingsFlow（沿 leaderSelectFlow 的謹慎度）", () => {
  it("happy path：簽伺服器原文 → recover 相符 → 整包 payload 原樣送出", async () => {
    const d = deps();
    const r = await runCapitalSettingsFlow(d, { expectedSigner: SIGNER, expected: EXPECTED });

    expect(r).toEqual({ ok: true, resp: OK_RESP });
    // ⭐ 簽的必須是伺服器原文本身（一個位元組差異就會被後端拒絕）
    expect(d.signMessage).toHaveBeenCalledWith(PAYLOAD.message);
    // ⭐ 送出的是同一個 payload 物件——前端沒有機會從別處拼欄位
    expect(d.submit).toHaveBeenCalledWith(PAYLOAD, SIG);
  });

  it("⭐ recover 出的簽章者 ≠ 登入地址 → 中止，且完全不送出（零網路請求）", async () => {
    const d = deps({ recover: vi.fn(async () => "0x9999999999999999999999999999999999999999") });
    const r = await runCapitalSettingsFlow(d, { expectedSigner: SIGNER, expected: EXPECTED });

    expect(r).toEqual({ ok: false, kind: "signer-mismatch" });
    expect(d.submit).not.toHaveBeenCalled();
  });

  it("⭐ recover 本身失敗（簽名格式壞）→ fail closed 視為不符，同樣不送出", async () => {
    const d = deps({ recover: vi.fn(async () => { throw new Error("bad signature"); }) });
    const r = await runCapitalSettingsFlow(d, { expectedSigner: SIGNER, expected: EXPECTED });

    expect(r).toEqual({ ok: false, kind: "signer-mismatch" });
    expect(d.submit).not.toHaveBeenCalled();
  });

  it("地址大小寫不同視為相同（recover 與登入地址一律小寫比對）", async () => {
    const d = deps({ recover: vi.fn(async () => SIGNER.toUpperCase()) });
    const r = await runCapitalSettingsFlow(d, {
      expectedSigner: SIGNER.toLowerCase(),
      expected: EXPECTED,
    });

    expect(r.ok).toBe(true);
    expect(d.submit).toHaveBeenCalledTimes(1);
  });

  it("錢包拒絕 → wallet-rejected，不 recover、不送出", async () => {
    const d = deps({ signMessage: vi.fn(async () => { throw new Error("User rejected"); }) });
    const r = await runCapitalSettingsFlow(d, { expectedSigner: SIGNER, expected: EXPECTED });

    expect(r).toEqual({ ok: false, kind: "wallet-rejected" });
    expect(d.recover).not.toHaveBeenCalled();
    expect(d.submit).not.toHaveBeenCalled();
  });

  it("取原文失敗 → message-failed，不叫錢包（不浪費使用者一次簽名）", async () => {
    const err = new Error("boom");
    const d = deps({ fetchMessage: vi.fn(async () => { throw err; }) });
    const r = await runCapitalSettingsFlow(d, { expectedSigner: SIGNER, expected: EXPECTED });

    expect(r).toEqual({ ok: false, kind: "message-failed", error: err });
    expect(d.signMessage).not.toHaveBeenCalled();
  });

  it("⭐ 送出失敗**不自動重試**（非冪等寫入 ＋ nonce 一次性）：submit 只被呼叫一次", async () => {
    const err = new Error("500");
    const d = deps({ submit: vi.fn(async () => { throw err; }) });
    const r = await runCapitalSettingsFlow(d, { expectedSigner: SIGNER, expected: EXPECTED });

    expect(r).toEqual({ ok: false, kind: "submit-failed", error: err });
    expect(d.submit).toHaveBeenCalledTimes(1);
  });
});

/**
 * ⭐ 被打穿的 filet-api 想無中生有一次曝險放大，唯一的著力點就是這裡：回一份寫著
 * 別的數字的待簽原文。使用者在錢包裡看到的只有一段英文，簽下去之後每一關（recover、
 * 後端重建驗簽、引擎的邊界檢查）都會放行——因為那份簽章確實是本人簽的，數值也確實
 * 落在合法區間（1.0 本來就合法），只是不是他調的那組。攔截點必須在**進錢包之前**：
 * 簽章一旦產生就已經是一份有效的超額曝險授權，事後才發現不符已經來不及。
 */
describe("runCapitalSettingsFlow — 設定值預驗 ⭐（伺服器要簽的數字必須是使用者調的）", () => {
  /** 這些測試會因為「拿掉設定值比對」而全部轉紅——它們是那道檢查的唯一防線。 */
  const cases: Array<[string, Partial<CapitalSettingsMessageResp>]> = [
    [
      "使用比例被拉滿（欄位與原文同時被改）",
      {
        capital_utilization: "1.0000",
        allocated_capital: "10000.00",
        message: PAYLOAD.message.replace("Capital Utilization: 0.2000", "Capital Utilization: 1.0000"),
      },
    ],
    [
      "本金被放大十倍（欄位與原文同時被改）",
      {
        allocated_capital: "100000.00",
        message: PAYLOAD.message.replace(
          "Allocated Capital: 10000.00 USDC",
          "Allocated Capital: 100000.00 USDC",
        ),
      },
    ],
    [
      "欄位對得上，但原文本體寫著別的比例（使用者在錢包看到的是後者）",
      {
        message: PAYLOAD.message.replace(
          "Capital Utilization: 0.2000",
          "Capital Utilization: 1.0000",
        ),
      },
    ],
    [
      "欄位對得上，但原文本體寫著別的本金",
      {
        message: PAYLOAD.message.replace(
          "Allocated Capital: 10000.00 USDC",
          "Allocated Capital: 100000.00 USDC",
        ),
      },
    ],
    [
      "原文完全沒有這兩行（缺一行就等於使用者沒看到他在授權什麼）",
      { message: "Filet: update copy-trading capital allocation\n\nAccount: fabc\nNonce: n-1" },
    ],
  ];

  for (const [name, over] of cases) {
    it(`⭐ ${name} → 中止：不喚起錢包、零網路請求`, async () => {
      const d = deps({ fetchMessage: vi.fn(async () => ({ ...PAYLOAD, ...over })) });
      const r = await runCapitalSettingsFlow(d, { expectedSigner: SIGNER, expected: EXPECTED });

      expect(r).toEqual({ ok: false, kind: "values-mismatch" });
      // 錢包完全沒有被喚起——使用者連一次「看起來很正常」的簽名請求都不該看到
      expect(d.signMessage).not.toHaveBeenCalled();
      expect(d.recover).not.toHaveBeenCalled();
      expect(d.submit).not.toHaveBeenCalled();
    });
  }

  /**
   * ⭐ 子字串比對擋不住的那一種：`"110000.00".includes("10000.00")` 為真。
   * 本金被放大 11 倍，只要預驗是 includes 就會照樣放行——所以比對必須是整行相等。
   */
  it("⭐ 放大後的數字**包含**原數字（1 萬 → 11 萬）→ 仍然中止", async () => {
    const d = deps({
      fetchMessage: vi.fn(async () => ({
        ...PAYLOAD,
        allocated_capital: "110000.00",
        message: PAYLOAD.message.replace(
          "Allocated Capital: 10000.00 USDC",
          "Allocated Capital: 110000.00 USDC",
        ),
      })),
    });
    const r = await runCapitalSettingsFlow(d, { expectedSigner: SIGNER, expected: EXPECTED });

    expect(r).toEqual({ ok: false, kind: "values-mismatch" });
    expect(d.signMessage).not.toHaveBeenCalled();
  });

  /**
   * ⭐ 欄位側的盲點。上面的 cases 只涵蓋「兩邊同時被改」與「只有原文被改」——都還有
   * 訊息本體那一半在擋。真正沒有人擋的是對偶情況：原文本體保持**完全正常**（使用者
   * 在錢包裡讀到的就是他自己調的那組數字），只有欄位被改。而後端正是拿這兩個欄位
   * **重建**原文驗簽、引擎也是拿它們乘進部位大小——欄位側才是真正生效的那一側。
   * 少了這半邊的比對，這道預驗對「使用者看得懂的攻擊」完全無效。
   */
  it("⭐ 原文本體對，但 allocated_capital 欄位被放大十倍 → 中止", async () => {
    const d = deps({
      fetchMessage: vi.fn(async () => ({ ...PAYLOAD, allocated_capital: "100000.00" })),
    });
    const r = await runCapitalSettingsFlow(d, { expectedSigner: SIGNER, expected: EXPECTED });

    expect(r).toEqual({ ok: false, kind: "values-mismatch" });
    expect(d.signMessage).not.toHaveBeenCalled();
    expect(d.submit).not.toHaveBeenCalled();
  });

  it("⭐ 原文本體對，但 capital_utilization 欄位被拉滿 → 中止", async () => {
    const d = deps({
      fetchMessage: vi.fn(async () => ({ ...PAYLOAD, capital_utilization: "1.0000" })),
    });
    const r = await runCapitalSettingsFlow(d, { expectedSigner: SIGNER, expected: EXPECTED });

    expect(r).toEqual({ ok: false, kind: "values-mismatch" });
    expect(d.signMessage).not.toHaveBeenCalled();
    expect(d.submit).not.toHaveBeenCalled();
  });

  /**
   * ⭐ 整行比對 vs 子字串比對的分水嶺。上面「1 萬 → 11 萬」那條其實**兩種寫法都擋得住**
   * （行首標籤 `Allocated Capital: ` 跟著數字一起比對，`110000.00` 破壞了整段的連續性），
   * 所以它證明不了整行比對的必要性——`hasEveryLine` 退化成 `message.includes(line)` 時
   * 它照樣是綠的。真正只有整行比對擋得住的是這一種：canonical 那兩行原封不動地**出現在
   * 別的行裡面**（此處被引述成「原值」），而使用者在錢包裡真正讀到的授權是下一行的新值。
   * 子字串比對會在 `Previous:` 那行同時找到兩個必要字串，然後放行一份 5 倍本金、
   * 使用比例拉滿的授權。
   */
  it("⭐ 必要行只以子字串形式藏在別行裡（引述舊值、實際授權新值）→ 中止", async () => {
    const d = deps({
      fetchMessage: vi.fn(async () => ({
        ...PAYLOAD,
        message:
          "Filet: update copy-trading capital allocation\n\n" +
          "Account: fabc\n" +
          "Previous: Allocated Capital: 10000.00 USDC / Capital Utilization: 0.2000\n" +
          "New: Allocated Capital: 100000.00 USDC / Capital Utilization: 1.0000\n" +
          "Nonce: n-1\nIssued At: 2026-07-19T00:00:00Z",
      })),
    });
    const r = await runCapitalSettingsFlow(d, { expectedSigner: SIGNER, expected: EXPECTED });

    expect(r).toEqual({ ok: false, kind: "values-mismatch" });
    expect(d.signMessage).not.toHaveBeenCalled();
    expect(d.submit).not.toHaveBeenCalled();
  });

  /**
   * 裁決（刻意不改）：**多餘／重複的行不擋**——`hasEveryLine` 只要求必要兩行都在，
   * 不要求原文恰好等於這些行。這不是疏漏，理由兩條：
   * 1. 多餘行拿不到授權，只拿得到失敗。後端不看客戶送來的 message，它拿
   *    `allocated_capital`／`capital_utilization` 兩個欄位**重建** canonical 原文再
   *    recover；而那兩個欄位已被上面的欄位側預驗釘在使用者調的值上，重建出來的必定是
   *    canonical 版。夾帶多餘行的原文簽出來的簽章，在後端 recover 時得到的是**另一個
   *    位址**，必然被拒（fail closed）。攻擊者能造成的最壞結果是一次失敗的交易。
   * 2. 改成「整份原文相等」要付的代價是真的。那等於把伺服器版型逐位元組寫死在前端：
   *    後端日後加一行揭露文字，前端就會把**每一次**正常的資金調整判成攻擊——而且症狀
   *    長得跟遭到攻擊一模一樣（工程原則 1 註解裡警告的那種誤擋）。拿一個真實的可用性
   *    風險，換一個攻擊者本來就拿不到的東西。
   * 本測試把這個裁決釘住：行為若被改成嚴格相等，它會轉紅，逼下一個人先讀完上面兩條。
   */
  it("多餘／重複的行不擋（必要兩行齊全即通過）——見上方裁決理由", async () => {
    const d = deps({
      fetchMessage: vi.fn(async () => ({
        ...PAYLOAD,
        message: `${PAYLOAD.message}\nNote: scheduled by support\nAllocated Capital: 10000.00 USDC`,
      })),
    });
    const r = await runCapitalSettingsFlow(d, { expectedSigner: SIGNER, expected: EXPECTED });

    expect(r.ok).toBe(true);
  });

  /**
   * 反向的保護：canonical 形式一致時**不得**誤擋。兩側都走 lib/capitalValues.ts 的
   * 同一套規則，所以正常情況必須通過——誤擋在這裡會讓每一次調整都失敗，
   * 而且看起來像遭到攻擊（工程原則 1：同源同基準才有得比）。
   */
  it("canonical 形式一致 → 正常通過（同基準比對，不得誤擋）", async () => {
    const d = deps();
    const r = await runCapitalSettingsFlow(d, {
      expectedSigner: SIGNER,
      expected: { allocated_capital: "10000.00", capital_utilization: "0.2000" },
    });

    expect(r.ok).toBe(true);
    expect(d.signMessage).toHaveBeenCalledTimes(1);
    expect(d.submit).toHaveBeenCalledTimes(1);
  });
});
