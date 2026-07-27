import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import path from "node:path";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { BillingPlansResp, CapitalSettingsMessageResp, MyCapitalResp } from "@/lib/api";

const ME = { address: "0xAbC0000000000000000000000000000000000001", account_id: "fabc" };

const getMe = vi.fn();
const getBillingPlans = vi.fn();
const getCapitalSettingsMessage = vi.fn();
const postCapitalSettings = vi.fn();
const getMyCapital = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getMe: (...a: unknown[]) => getMe(...a),
  getBillingPlans: (...a: unknown[]) => getBillingPlans(...a),
  getCapitalSettingsMessage: (...a: unknown[]) => getCapitalSettingsMessage(...a),
  postCapitalSettings: (...a: unknown[]) => postCapitalSettings(...a),
  getMyCapital: (...a: unknown[]) => getMyCapital(...a),
}));

const signMessageAsync = vi.fn();
vi.mock("wagmi", () => ({ useSignMessage: () => ({ signMessageAsync }) }));

const recoverPersonalSigner = vi.fn();
vi.mock("@/lib/sign", () => ({
  recoverPersonalSigner: (...a: unknown[]) => recoverPersonalSigner(...a),
}));

import CapitalPage from "./page";

function wrap(children: ReactNode, me: typeof ME | null = ME) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(["me"], me);
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

/** 方案目錄。`shipped` 決定本頁能否送出——前端不硬編（後端 _F_RATIO_SLIDER）。 */
function catalog(ratioSliderShipped: boolean): BillingPlansResp {
  return {
    billing_enabled: true,
    plans: [
      {
        id: "pro",
        name_key: "plans.pro.name",
        price_display: "USD 29 / 月",
        purchasable: true,
        features: [
          { text_key: "plans.feature.copytrade", included: true, shipped: true },
          { text_key: "plans.feature.ratioSlider", included: true, shipped: ratioSliderShipped },
        ],
      },
    ],
  };
}

/**
 * ⭐ 原文必須含 `Allocated Capital: … USDC` 與 `Capital Utilization: …` 兩行——
 * 這是伺服器版型的一部分（filet/capital_settings.py 的 build_capital_settings_message），
 * 也是前端設定值預驗的第二道比對對象。fixture 省略它們就驗不到真實路徑。
 */
function message(over: Partial<CapitalSettingsMessageResp> = {}): CapitalSettingsMessageResp {
  return {
    message:
      "Filet: update copy-trading capital allocation\n\nAccount: fabc\n" +
      "Allocated Capital: 10000.00 USDC\nCapital Utilization: 0.2000\nNonce: n-1",
    nonce: "n-1",
    issued_at: "2026-07-19T00:00:00Z",
    account_id: "fabc",
    allocated_capital: "10000.00",
    capital_utilization: "0.2000",
    ...over,
  };
}

const SIG = `0x${"ab".repeat(65)}`;

beforeEach(() => {
  vi.clearAllMocks();
  getMe.mockResolvedValue(ME);
  getBillingPlans.mockResolvedValue(catalog(true));
  getMyCapital.mockResolvedValue({
    account_id: "fabc",
    status: "effective",
    effective: {
      allocated_capital: "5000.00",
      capital_utilization: "0.1500",
      use_full_equity: false,
      source: "customer_signed",
      changed_at: "2026-07-18T12:30:00Z",
      as_of: "2026-07-19T00:00:00Z",
    },
    pending: null,
    heartbeat: { status: "ok", at: "2026-07-19T00:00:00Z", age_s: 0, stale_after_s: 300 },
    note: "資金設定已生效，於最新 cycle 應用。",
  });
  getCapitalSettingsMessage.mockResolvedValue(message());
  postCapitalSettings.mockResolvedValue({
    ok: true, account_id: "fabc",
    allocated_capital: "10000.00", capital_utilization: "0.2000",
    effective: "next_engine_cycle",
    effective_note: "已記錄，於引擎的下一個 cycle 生效——不是立即生效。",
    consequences: "新的部位大小會在下一個 cycle 起套用，但不會立即強制再平衡現有部位。",
  });
  signMessageAsync.mockResolvedValue(SIG);
  recoverPersonalSigner.mockResolvedValue(ME.address.toLowerCase());
});

/** 填好一組合法的值（本金 10000、比例維持預設 20%）。 */
async function fillValidForm(user: ReturnType<typeof userEvent.setup>) {
  const input = await screen.findByLabelText(/投入本金/);
  await user.clear(input);
  await user.type(input, "10000");
  return input;
}

async function openConfirm(user: ReturnType<typeof userEvent.setup>) {
  await fillValidForm(user);
  const btn = await screen.findByRole("button", { name: "套用這組設定" });
  expect(btn).toBeEnabled();
  await user.click(btn);
  return screen.findByRole("dialog");
}

describe("/capital 基本呈現與誠實揭露", () => {
  it("未登入 → 顯示未登入訊息，不出現表單，也不打任何設定端點", async () => {
    getMe.mockResolvedValue(null);
    render(wrap(<CapitalPage />, null));

    expect(await screen.findByText(/尚未登入|請先登入|未登入/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "套用這組設定" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/投入本金/)).not.toBeInTheDocument();
    expect(getCapitalSettingsMessage).not.toHaveBeenCalled();
    expect(postCapitalSettings).not.toHaveBeenCalled();
  });

  it("⭐ status=effective 時顯示生效的本金與使用比例、來源與時刻", async () => {
    render(wrap(<CapitalPage />));

    expect(await screen.findByText("你目前生效的資金設定")).toBeInTheDocument();
    // 生效值區塊
    expect(screen.getByText("目前生效值")).toBeInTheDocument();
    expect(screen.getByText("5,000.00 USDC")).toBeInTheDocument();
    expect(screen.getByText("15.00%")).toBeInTheDocument();
    expect(screen.getByText("你簽署的設定")).toBeInTheDocument();
    expect(screen.getByText("2026-07-18T12:30:00Z")).toBeInTheDocument();
  });

  it("⭐ 風險揭露（提高曝險）與滑桿在同一個容器裡，不是頁尾小字", async () => {
    render(wrap(<CapitalPage />));

    const slider = await screen.findByLabelText(/使用比例/);
    const form = slider.closest("section");
    expect(form).not.toBeNull();
    expect(within(form as HTMLElement).getByText(/提高曝險與清算風險/)).toBeInTheDocument();
  });

  it("兩個欄位用客戶看得懂的話解釋，並講明兩者相乘才是部位規模的基準", async () => {
    render(wrap(<CapitalPage />));

    // 名詞解釋區塊：兩個詞條各有一段白話說明（不是欄位名直譯）
    const terms = (await screen.findByText("這兩個數字各是什麼")).parentElement as HTMLElement;
    expect(within(terms).getByText("投入本金")).toBeInTheDocument();
    expect(within(terms).getByText(/你打算讓這套系統拿去跟單的金額/)).toBeInTheDocument();
    expect(within(terms).getByText("使用比例")).toBeInTheDocument();
    expect(within(terms).getByText(/實際被拿去建立部位的比例/)).toBeInTheDocument();
    // ⭐ 兩者相乘的意義
    expect(screen.getByText(/兩者相乘才是部位規模的基準/)).toBeInTheDocument();
    expect(screen.getByText(/投入本金 × 使用比例 = 引擎為你建立部位時的資金基準/))
      .toBeInTheDocument();
  });

  it("⭐ 拖動滑桿本身不觸發任何請求（調好按套用才簽章）", async () => {
    const user = userEvent.setup();
    render(wrap(<CapitalPage />));
    await fillValidForm(user);

    const slider = await screen.findByLabelText(/使用比例/);
    await user.click(slider);
    await user.keyboard("{ArrowRight}{ArrowRight}{ArrowRight}");

    expect(getCapitalSettingsMessage).not.toHaveBeenCalled();
    expect(signMessageAsync).not.toHaveBeenCalled();
    expect(postCapitalSettings).not.toHaveBeenCalled();
  });
});

describe("/capital 邊界值：阻擋並明說，不靜默截斷", () => {
  it("⭐ 本金 0（超界）→ 明確錯誤、按鈕不可送出、零請求，且輸入框內容不被程式改掉", async () => {
    const user = userEvent.setup();
    render(wrap(<CapitalPage />));

    const input = await screen.findByLabelText(/投入本金/);
    await user.type(input, "0");

    expect(screen.getByText(/投入本金必須大於 0/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "套用這組設定" })).toBeDisabled();
    // ⭐ 不靜默修正：使用者打的字原樣留著，程式不替他改成合法值
    expect(input).toHaveValue("0");
    expect(getCapitalSettingsMessage).not.toHaveBeenCalled();
  });

  it("⭐ 小數位超過 2 位 → 明說不會四捨五入、不可送出，且輸入框不被截斷", async () => {
    const user = userEvent.setup();
    render(wrap(<CapitalPage />));

    const input = await screen.findByLabelText(/投入本金/);
    await user.type(input, "1000.005");

    expect(screen.getByText(/不會自動幫你四捨五入/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "套用這組設定" })).toBeDisabled();
    // ⭐ 沒有被截成 1000.00 —— 截斷等於改掉他要簽署的數字
    expect(input).toHaveValue("1000.005");
    expect(getCapitalSettingsMessage).not.toHaveBeenCalled();
  });

  it("使用比例滑桿的值域鎖在 1%～100%（結構上到不了 0 或 >1）", async () => {
    render(wrap(<CapitalPage />));

    const slider = await screen.findByLabelText(/使用比例/);
    expect(slider).toHaveAttribute("min", "1");
    expect(slider).toHaveAttribute("max", "100");
  });

  it("後端回 400（超界）→ 顯示明確錯誤與後端 detail，不假裝成功", async () => {
    const user = userEvent.setup();
    const { ApiError } = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
    getCapitalSettingsMessage.mockRejectedValue(
      new ApiError("client", "boom", 400, "數值超出允許範圍"),
    );
    render(wrap(<CapitalPage />));

    const dialog = await openConfirm(user);
    await user.click(within(dialog).getByRole("button", { name: "確認並簽署" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/數值超出允許範圍/);
    expect(signMessageAsync).not.toHaveBeenCalled();
  });
});

describe("/capital 付費閘門（後端 shipped 旗標驅動）", () => {
  it("⭐ ratioSlider 未 shipped → 無法送出：按鈕 disabled、點了也零請求", async () => {
    const user = userEvent.setup();
    getBillingPlans.mockResolvedValue(catalog(false));
    render(wrap(<CapitalPage />));

    await fillValidForm(user);
    const btn = await screen.findByRole("button", { name: "開發中，敬請期待" });
    expect(btn).toBeDisabled();
    await user.click(btn);

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(getCapitalSettingsMessage).not.toHaveBeenCalled();
    expect(signMessageAsync).not.toHaveBeenCalled();
    expect(postCapitalSettings).not.toHaveBeenCalled();
    expect(screen.getByText(/跟單比例自訂尚未推出/)).toBeInTheDocument();
  });

  it("方案目錄載入失敗 → fail closed（不確定就不放行）", async () => {
    const user = userEvent.setup();
    getBillingPlans.mockRejectedValue(new Error("down"));
    render(wrap(<CapitalPage />));

    await fillValidForm(user);
    expect(await screen.findByText(/無法確認這個功能是否已推出/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "開發中，敬請期待" })).toBeDisabled();
    expect(getCapitalSettingsMessage).not.toHaveBeenCalled();
  });
});

describe("/capital 確認步驟", () => {
  it("⭐ 確認步驟寫明「下一個 cycle 生效」「不會立即再平衡」「提高曝險」三件事", async () => {
    const user = userEvent.setup();
    render(wrap(<CapitalPage />));

    const dialog = await openConfirm(user);
    expect(within(dialog).getByText(/下一個 cycle 生效/)).toBeInTheDocument();
    expect(within(dialog).getByText(/不會立即再平衡/)).toBeInTheDocument();
    expect(within(dialog).getByText(/提高曝險/)).toBeInTheDocument();
  });

  it("⭐ 送出前顯示前後對照：目前值（未知，明說原因）→ 新值", async () => {
    const user = userEvent.setup();
    render(wrap(<CapitalPage />));

    const dialog = await openConfirm(user);
    expect(within(dialog).getByText("目前生效值")).toBeInTheDocument();
    expect(within(dialog).getByText(/無法顯示（後端未提供查詢端點）/)).toBeInTheDocument();
    expect(within(dialog).getByText("你要授權的新值")).toBeInTheDocument();
    expect(within(dialog).getByText(/10,000.00 USDC × 20%/)).toBeInTheDocument();
    // 實際會被簽署的 canonical 字串也攤在畫面上，供使用者與錢包畫面核對
    expect(within(dialog).getByText(/allocated_capital=10000\.00/)).toBeInTheDocument();
    expect(within(dialog).getByText(/capital_utilization=0\.2000/)).toBeInTheDocument();
  });

  it("按確認之前不打任何端點；取消後回到閒置且零請求", async () => {
    const user = userEvent.setup();
    render(wrap(<CapitalPage />));

    const dialog = await openConfirm(user);
    expect(getCapitalSettingsMessage).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "取消" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(getCapitalSettingsMessage).not.toHaveBeenCalled();
    expect(signMessageAsync).not.toHaveBeenCalled();
  });
});

describe("/capital 簽章流程", () => {
  it("happy path：canonical 值送去取原文 → 簽伺服器原文 → 整包原樣送出", async () => {
    const user = userEvent.setup();
    render(wrap(<CapitalPage />));

    const dialog = await openConfirm(user);
    await user.click(within(dialog).getByRole("button", { name: "確認並簽署" }));

    expect(await screen.findByText(/已授權，於引擎的下一個 cycle 生效/)).toBeInTheDocument();
    // ⭐ 送去後端的是 canonical 字串（不是使用者打的 "10000"）
    expect(getCapitalSettingsMessage).toHaveBeenCalledWith("10000.00", "0.2000");
    // ⭐ 簽的是伺服器原文本身
    expect(signMessageAsync).toHaveBeenCalledWith({ message: message().message });
    // ⭐ 送出的是同一包 payload（前端沒有機會從別處拼欄位）
    expect(postCapitalSettings).toHaveBeenCalledWith(message(), SIG);
  });

  it("⭐ recover 出的簽章者 ≠ 登入地址 → 零送出", async () => {
    const user = userEvent.setup();
    recoverPersonalSigner.mockResolvedValue("0x9999999999999999999999999999999999999999");
    render(wrap(<CapitalPage />));

    const dialog = await openConfirm(user);
    await user.click(within(dialog).getByRole("button", { name: "確認並簽署" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/簽名帳號與登入帳號不符/);
    expect(postCapitalSettings).not.toHaveBeenCalled();
  });

  it("錢包取消 → 明說沒有任何變動，零送出", async () => {
    const user = userEvent.setup();
    signMessageAsync.mockRejectedValue(new Error("User rejected"));
    render(wrap(<CapitalPage />));

    const dialog = await openConfirm(user);
    await user.click(within(dialog).getByRole("button", { name: "確認並簽署" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/沒有任何變動/);
    expect(postCapitalSettings).not.toHaveBeenCalled();
  });
});

/**
 * ⭐ 本檔最重要的一組：被打穿的 API 回一份「把使用比例拉滿」的待簽原文。
 * 使用者在錢包裡只看到一段英文，簽下去就是一份**真實**的超額曝險授權，而後端每一關
 * （重建驗簽、邊界檢查）都會放行——1.0 本來就在合法區間內。攔截必須在進錢包之前。
 */
describe("/capital 設定值預驗 ⭐（伺服器要簽的數字必須是使用者調的）", () => {
  it("⭐ 伺服器回傳的使用比例 ≠ 使用者所調 → 不喚起錢包、零送出，且要求停手回報", async () => {
    const user = userEvent.setup();
    getCapitalSettingsMessage.mockResolvedValue(
      message({
        capital_utilization: "1.0000",
        message:
          "Filet: update copy-trading capital allocation\n\nAccount: fabc\n" +
          "Allocated Capital: 10000.00 USDC\nCapital Utilization: 1.0000\nNonce: n-1",
      }),
    );
    render(wrap(<CapitalPage />));

    const dialog = await openConfirm(user);
    await user.click(within(dialog).getByRole("button", { name: "確認並簽署" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/與你在本頁調整的數值不符/);
    expect(alert).toHaveTextContent(/請不要重試/);
    // ⭐ 錢包完全沒有被喚起，也沒有任何送出
    expect(signMessageAsync).not.toHaveBeenCalled();
    expect(recoverPersonalSigner).not.toHaveBeenCalled();
    expect(postCapitalSettings).not.toHaveBeenCalled();
    // 這是「請停手回報」的失敗，不給「重新操作」按鈕
    expect(within(alert).getByRole("button", { name: "關閉" })).toBeInTheDocument();
    expect(within(alert).queryByRole("button", { name: "重新操作" })).not.toBeInTheDocument();
  });

  it("⭐ 伺服器回傳的本金 ≠ 使用者所調 → 不喚起錢包、零送出", async () => {
    const user = userEvent.setup();
    getCapitalSettingsMessage.mockResolvedValue(
      message({
        allocated_capital: "100000.00",
        message:
          "Filet: update copy-trading capital allocation\n\nAccount: fabc\n" +
          "Allocated Capital: 100000.00 USDC\nCapital Utilization: 0.2000\nNonce: n-1",
      }),
    );
    render(wrap(<CapitalPage />));

    const dialog = await openConfirm(user);
    await user.click(within(dialog).getByRole("button", { name: "確認並簽署" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/與你在本頁調整的數值不符/);
    expect(signMessageAsync).not.toHaveBeenCalled();
    expect(postCapitalSettings).not.toHaveBeenCalled();
  });
});

/**
 * ⭐ CurrentCapitalPanel：「你目前生效的資金設定」區塊。
 * 核心設計：pending 永遠不能被畫成「已生效」的樣子；讀不到就說讀不到，不歸零；
 * 獨立元件、獨立失敗邊界，本區失敗不影響頁面其餘部分。
 */
describe("CurrentCapitalPanel：目前生效的資金設定", () => {
  it("status=effective 且 effective 存在 → 顯示本金、使用比例、來源與時刻", async () => {
    render(wrap(<CapitalPage />));

    expect(await screen.findByText("目前生效值")).toBeInTheDocument();
    expect(screen.getByText("5,000.00 USDC")).toBeInTheDocument();
    expect(screen.getByText("15.00%")).toBeInTheDocument();
    expect(screen.getByText("你簽署的設定")).toBeInTheDocument();
    expect(screen.getByText("2026-07-18T12:30:00Z")).toBeInTheDocument();
  });

  it("⭐ 顯示 pending 時用獨立區塊標示「已提交、尚未生效」", async () => {
    getMyCapital.mockResolvedValue({
      account_id: "fabc",
      status: "effective",
      effective: {
        allocated_capital: "5000.00",
        capital_utilization: "0.1500",
        use_full_equity: false,
        source: "customer_signed",
        changed_at: "2026-07-18T12:30:00Z",
        as_of: "2026-07-19T00:00:00Z",
      },
      pending: {
        allocated_capital: "8000.00",
        capital_utilization: "0.2500",
        use_full_equity: false,
        submitted_at: "2026-07-19T10:00:00Z",
        state: "not_yet_applied",
        effective_when: "next_engine_cycle",
        note: "待下一個 cycle 生效。",
      },
      heartbeat: { status: "ok", at: "2026-07-19T00:00:00Z", age_s: 0, stale_after_s: 300 },
      note: "資金設定已生效。",
    } as MyCapitalResp);
    render(wrap(<CapitalPage />));

    // 兩個標題都應該出現
    expect(await screen.findByText("目前生效值")).toBeInTheDocument();
    expect(screen.getByText("已提交、尚未生效的變更")).toBeInTheDocument();

    // 生效值區塊（舊值）
    const effectiveBlock = screen.getByText("目前生效值").closest(".capital-effective-values") as HTMLElement;
    expect(effectiveBlock).toBeInTheDocument();
    expect(within(effectiveBlock).getByText("5,000.00 USDC")).toBeInTheDocument();
    expect(within(effectiveBlock).getByText("15.00%")).toBeInTheDocument();

    // pending 區塊（新值）——獨立的區塊，用不同的標題、不同的 class
    const pendingBlock = screen.getByText("已提交、尚未生效的變更").closest(".capital-pending") as HTMLElement;
    expect(pendingBlock).toBeInTheDocument();
    expect(within(pendingBlock).getByText("8,000.00 USDC")).toBeInTheDocument();
    expect(within(pendingBlock).getByText("25.00%")).toBeInTheDocument();
    expect(within(pendingBlock).getByText("等待引擎套用")).toBeInTheDocument();

    // ⭐ 確保兩個區塊是分離的：pending 的高值不會出現在生效區塊
    expect(within(effectiveBlock).queryByText("8,000.00 USDC")).not.toBeInTheDocument();
    expect(within(effectiveBlock).queryByText("25.00%")).not.toBeInTheDocument();
  });

  it("查詢失敗 → 顯示「暫時讀不到」，且頁面其餘部分（表單）仍可用", async () => {
    getMyCapital.mockRejectedValue(new Error("503 Service Unavailable"));
    render(wrap(<CapitalPage />));

    expect(await screen.findByText("暫時讀不到你目前的設定")).toBeInTheDocument();
    expect(screen.getByText(/無法查詢後端/)).toBeInTheDocument();
    // 表單仍可用
    expect(screen.getByLabelText(/投入本金/)).toBeInTheDocument();
    expect(screen.getByLabelText(/使用比例/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "套用這組設定" })).toBeInTheDocument();
  });

  it("status=not_activated → 顯示帳號尚未活化", async () => {
    getMyCapital.mockResolvedValue({
      account_id: "fabc",
      status: "not_activated",
      effective: null,
      pending: null,
      heartbeat: null,
      note: "此帳號尚未在系統中註冊。",
    } as MyCapitalResp);
    render(wrap(<CapitalPage />));

    expect(await screen.findByText("此帳號尚未活化")).toBeInTheDocument();
  });

  it("status=indeterminate → 顯示帳號狀態無法確認", async () => {
    getMyCapital.mockResolvedValue({
      account_id: "fabc",
      status: "indeterminate",
      effective: null,
      pending: null,
      heartbeat: null,
      note: "無法判定帳號狀態。",
    } as MyCapitalResp);
    render(wrap(<CapitalPage />));

    expect(await screen.findByText("帳號狀態無法確認")).toBeInTheDocument();
  });

  it("⭐ 預期外的 status 值 → 原樣顯示該值（看得懂但陌生）", async () => {
    getMyCapital.mockResolvedValue({
      account_id: "fabc",
      status: "alien_status",
      effective: null,
      pending: null,
      heartbeat: null,
      note: "未知狀態。",
    } as unknown as MyCapitalResp);
    render(wrap(<CapitalPage />));

    expect(await screen.findByText(/狀態: alien_status/)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// 以下為 2026-07-27 審查（F1–F7）的錨定測試。每一條對應一個「形似而神不似」的
// 缺陷：class 名稱抄到了但沒有樣式、狀態碼抄到了但語意被合併、型別抄到了但
// nullable 被抹掉。共同的失效模式是**畫面對客戶說了一句我們沒有根據的話**。
// ---------------------------------------------------------------------------

/** `/api/me/capital` 回覆的組裝器：只寫這條測試在意的欄位，其餘走安全預設。 */
function capitalResp(over: Partial<MyCapitalResp> = {}): MyCapitalResp {
  return {
    account_id: "fabc",
    status: "effective",
    effective: {
      allocated_capital: "5000.00",
      capital_utilization: "0.1500",
      use_full_equity: false,
      source: "customer_signed",
      changed_at: "2026-07-18T12:30:00Z",
      as_of: "2026-07-19T00:00:00Z",
    },
    pending: null,
    heartbeat: { status: "ok", at: "2026-07-19T00:00:00Z", age_s: 0, stale_after_s: 300 },
    note: "資金設定已生效。",
    ...over,
  } as MyCapitalResp;
}

/**
 * F1 ⭐ 最嚴重的一條：`use_full_equity=true` 時後端的**硬不變量**是
 * `allocated_capital == 0`（copytrade/config.py 的 `__post_init__`），而且它是
 * `COPY_ALLOCATED_CAPITAL` 未設時的預設 ⇒ 每個 `source="env_default"` 帳號都是這個狀態。
 * 把 0 原樣畫成「投入本金 0.00 USDC」，再配上正上方術語表教的「本金 × 比例 = 部位基準」，
 * 客戶會算出**曝險為零**——而引擎其實是拿整個帳戶權益在下單。
 */
describe("F1 use_full_equity：不得把「用全部權益」畫成「本金 0」", () => {
  it("⭐ effective.use_full_equity=true → 明說使用全部帳戶權益，且不出現「0.00 USDC」", async () => {
    getMyCapital.mockResolvedValue(capitalResp({
      effective: {
        allocated_capital: "0",
        capital_utilization: "1.0000",
        use_full_equity: true,
        source: "env_default",
        changed_at: null,
        as_of: "2026-07-19T00:00:00Z",
      },
    }));
    render(wrap(<CapitalPage />));

    const block = (await screen.findByText("目前生效值"))
      .closest(".capital-effective-values") as HTMLElement;
    expect(within(block).getByText(/使用全部帳戶權益/)).toBeInTheDocument();
    // ⭐ 「0.00 USDC」在這個狀態下是一句錯誤的斷言，不得出現在生效值區塊裡
    expect(within(block).queryByText(/^0(\.00)? USDC$/)).not.toBeInTheDocument();
  });

  it("⭐ pending.use_full_equity=true → pending 區塊同樣不得畫成「本金 0」", async () => {
    getMyCapital.mockResolvedValue(capitalResp({
      pending: {
        allocated_capital: "0",
        capital_utilization: "1.0000",
        use_full_equity: true,
        submitted_at: "2026-07-19T10:00:00Z",
        state: "not_yet_applied",
        effective_when: "next_engine_cycle",
        note: "待下一個 cycle 生效。",
      },
    }));
    render(wrap(<CapitalPage />));

    const block = (await screen.findByText(/已提交/)).closest(".capital-pending") as HTMLElement;
    expect(within(block).getByText(/使用全部帳戶權益/)).toBeInTheDocument();
    expect(within(block).queryByText(/^0(\.00)? USDC$/)).not.toBeInTheDocument();
  });
});

/**
 * F2 ⭐ pending 與 effective 的分離原本只存在於 class **屬性**上——五個 class 在
 * globals.css 裡零規則，於是同一個面板裡兩個沒有樣式的 `<dl>`、標籤字樣完全相同，
 * 而 pending 的數字排在**下面**：掃視的人會把下面那組讀成最新的。
 *
 * ⚠️ 本測試的極限（誠實標註）：jsdom **不載入** globals.css，所以沒有任何辦法在這裡
 * 斷言「渲染後看起來不一樣」。折衷是把「class 有掛上」（DOM 斷言）與「class 有規則」
 * （直接讀 globals.css 原文）兩件事各釘一半——兩者都成立才代表分離真的落到樣式上。
 * 它抓不到的是：規則存在但視覺上仍然無從分辨（那需要真瀏覽器的視覺回歸測試）。
 */
describe("F2 pending／effective 的分離必須落到樣式，不只是 class 名稱", () => {
  const css = readFileSync(
    path.resolve(__dirname, "../../styles/globals.css"),
    "utf8",
  );

  it.each([
    ".capital-current",
    ".capital-effective-values",
    ".capital-current-dl",
    ".capital-current-none",
    ".capital-pending",
  ])("globals.css 對 %s 有實際規則（不是只有 class 名稱）", (cls) => {
    // ⚠️ 刻意要求「class 自己就是一條規則的完整選擇器」（`.x {`），不接受 `.x-y {`
    // 或 `.x .child {` 充數——用 \b 的話 `.capital-current` 會被 `.capital-current-dl`
    // 的規則滿足，測試就又退回「只檢查字串出現過」的等級。規則體必須含至少一個宣告。
    const re = new RegExp(`\\${cls}\\s*\\{[^}]*[a-z-]+\\s*:`, "m");
    expect(css, `${cls} 在 globals.css 沒有自己的規則`).toMatch(re);
  });

  it("⭐ pending 有自己的標題與獨立容器，且生效值區塊不含 pending 的數字", async () => {
    getMyCapital.mockResolvedValue(capitalResp({
      pending: {
        allocated_capital: "8000.00",
        capital_utilization: "0.2500",
        use_full_equity: false,
        submitted_at: "2026-07-19T10:00:00Z",
        state: "not_yet_applied",
        effective_when: "next_engine_cycle",
        note: "待下一個 cycle 生效。",
      },
    }));
    render(wrap(<CapitalPage />));

    const effective = (await screen.findByText("目前生效值"))
      .closest(".capital-effective-values") as HTMLElement;
    const pending = screen.getByText("已提交、尚未生效的變更")
      .closest(".capital-pending") as HTMLElement;
    // 兩個容器互不包含（不是同一塊 DOM 被兩個 class 標記）
    expect(effective.contains(pending)).toBe(false);
    expect(pending.contains(effective)).toBe(false);
    expect(within(effective).queryByText(/8,?000\.00 USDC/)).not.toBeInTheDocument();
    expect(within(pending).queryByText(/5,?000\.00 USDC/)).not.toBeInTheDocument();
  });
});

/**
 * F3 ⭐ 後端的 `unknown` 有明確語義：「已活化但心跳缺席／過期」（app.py 的 status 四態），
 * 不是「前端不認識的狀態碼」。把兩者合併之後，畫面變成「你目前生效的資金設定 →
 * 未知的狀態碼」，而整個面板裡唯一的數字是 pending 那組。
 */
describe("F3 status=unknown 有專屬語義，不得與「不認識的狀態碼」合併", () => {
  it("⭐ status=unknown → 說的是心跳缺席／過期，不是「未知的狀態碼」", async () => {
    getMyCapital.mockResolvedValue(capitalResp({
      status: "unknown",
      effective: null,
      heartbeat: { status: "stale", at: null, age_s: 9999, stale_after_s: 300 },
      note: "引擎心跳過期，無法確認目前生效值。",
    }));
    render(wrap(<CapitalPage />));

    // 面板自己的標題（不是後端 note）必須講出「心跳」這個真正的原因
    const none = await screen.findByText(/心跳/, { selector: ".capital-current-none" });
    expect(none).toBeInTheDocument();
    // ⭐ 這是後端明訂的狀態，不是「我們看不懂的字串」
    expect(screen.queryByText(/未知的狀態碼/)).not.toBeInTheDocument();
    // 也不該把它當陌生狀態碼攤出來
    expect(screen.queryByText(/狀態: unknown/)).not.toBeInTheDocument();
  });

  it("不認識的狀態碼仍走 fallback（兩者分開，各自成立）", async () => {
    getMyCapital.mockResolvedValue(capitalResp({
      status: "alien_status",
      effective: null,
      heartbeat: null,
      note: "未知狀態。",
    } as unknown as Partial<MyCapitalResp>));
    render(wrap(<CapitalPage />));

    expect(await screen.findByText(/狀態: alien_status/)).toBeInTheDocument();
  });
});

/**
 * F4 ⭐ 後端刻意把 pending 的兩態分開（app.py）：`unconfirmed`＝**無從得知**套用了沒；
 * `not_yet_applied`＝**確定**還沒套用，並註明「處置完全不同」。全部標成「尚未生效」，
 * 等於在心跳過期＋一筆往上調的變更時，用畫面上最大的那句話告訴客戶「還沒生效」——
 * 而它可能已經生效了。
 */
describe("F4 pending 標題必須依 state 分兩種", () => {
  it("⭐ state=unconfirmed → 不得斷言「尚未生效」，要說無從確認", async () => {
    getMyCapital.mockResolvedValue(capitalResp({
      status: "unknown",
      effective: null,
      pending: {
        allocated_capital: "10000.00",
        capital_utilization: "1.0000",
        use_full_equity: false,
        submitted_at: "2026-07-19T10:00:00Z",
        state: "unconfirmed",
        effective_when: "next_engine_cycle",
        note: "心跳缺席，無從得知是否已套用。",
      },
      heartbeat: { status: "stale", at: null, age_s: 9999, stale_after_s: 300 },
    }));
    render(wrap(<CapitalPage />));

    // pending 區塊自己的標題（selector 把它與面板的 none 標題分開）
    const title = await screen.findByText(/無從確認/, { selector: ".capital-section-title" });
    const pending = title.closest(".capital-pending") as HTMLElement;
    expect(pending).not.toBeNull();
    // ⭐ 「尚未生效」是一句斷言，在 unconfirmed 下我們沒有根據講它
    expect(within(pending).queryByText("已提交、尚未生效的變更")).not.toBeInTheDocument();
    expect(screen.queryByText("已提交、尚未生效的變更")).not.toBeInTheDocument();
  });

  it("state=not_yet_applied → 維持「已提交、尚未生效的變更」", async () => {
    getMyCapital.mockResolvedValue(capitalResp({
      pending: {
        allocated_capital: "8000.00",
        capital_utilization: "0.2500",
        use_full_equity: false,
        submitted_at: "2026-07-19T10:00:00Z",
        state: "not_yet_applied",
        effective_when: "next_engine_cycle",
        note: "待下一個 cycle 生效。",
      },
    }));
    render(wrap(<CapitalPage />));

    expect(await screen.findByText("已提交、尚未生效的變更")).toBeInTheDocument();
  });
});

/**
 * F5 ⭐ `as_of` 取自 `hb.at`，而 `HeartbeatRead.at` 是 `str | None`。型別宣告成
 * 非 nullable 之後，null 會被原樣渲染成空字串 →「此值查詢於 (空白)」，正好復活這個
 * 欄位存在就是要防的那件事：一份接近過期的心跳被當成即時查詢讀。
 */
describe("F5 as_of 可為 null：缺值要有明確標記，不得渲染成空白", () => {
  it("⭐ as_of=null → 該列顯示缺值標記（—），不是空白", async () => {
    getMyCapital.mockResolvedValue(capitalResp({
      effective: {
        allocated_capital: "5000.00",
        capital_utilization: "0.1500",
        use_full_equity: false,
        source: "customer_signed",
        changed_at: "2026-07-18T12:30:00Z",
        as_of: null,
      },
    } as unknown as Partial<MyCapitalResp>));
    render(wrap(<CapitalPage />));

    const block = (await screen.findByText("目前生效值"))
      .closest(".capital-effective-values") as HTMLElement;
    const dt = within(block).getByText("此值查詢於");
    const dd = dt.nextElementSibling as HTMLElement;
    expect(dd.textContent?.trim()).toBe("—");
  });
});

/**
 * F6 ⭐ 手寫的 `(Number(x) * 100).toFixed(2)` 對非數字回傳 "NaN"，畫面上就是 "NaN%"；
 * `format.ts` 已有 `fmtRatioPct`／`fmtAmount`，且明訂**缺值一律顯示 NO_VALUE 而非 0**。
 */
describe("F6 數值格式化一律走 format.ts 的 helper", () => {
  it("⭐ 壞掉的比例字串 → 顯示 —，不是 NaN%", async () => {
    getMyCapital.mockResolvedValue(capitalResp({
      effective: {
        allocated_capital: "abc",
        capital_utilization: "abc",
        use_full_equity: false,
        source: "customer_signed",
        changed_at: null,
        as_of: "2026-07-19T00:00:00Z",
      },
    }));
    render(wrap(<CapitalPage />));

    const block = (await screen.findByText("目前生效值"))
      .closest(".capital-effective-values") as HTMLElement;
    expect(within(block).queryByText(/NaN/)).not.toBeInTheDocument();
    expect(within(block).getAllByText("—").length).toBeGreaterThanOrEqual(2);
  });

  it("金額走 fmtAmount（千分位，與確認框的 describeValues 同一種寫法）", async () => {
    render(wrap(<CapitalPage />));
    expect(await screen.findByText("5,000.00 USDC")).toBeInTheDocument();
  });
});

/**
 * F7 ⭐ 最高風險的狀態組合：`effective === null` 而 `pending != null`。此時整個面板裡
 * 唯一的一組數字是 pending 的——它必須明確被標成「還沒算數」，否則它就是客戶眼中的現況。
 */
describe("F7 高風險狀態組合", () => {
  it("⭐ effective=null 但 pending 存在 → 唯一的數字組必須在 pending 區塊裡", async () => {
    getMyCapital.mockResolvedValue(capitalResp({
      status: "unknown",
      effective: null,
      pending: {
        allocated_capital: "9000.00",
        capital_utilization: "0.8000",
        use_full_equity: false,
        submitted_at: "2026-07-19T10:00:00Z",
        state: "unconfirmed",
        effective_when: "next_engine_cycle",
        note: "心跳缺席。",
      },
      heartbeat: { status: "stale", at: null, age_s: 9999, stale_after_s: 300 },
    }));
    render(wrap(<CapitalPage />));

    expect(await screen.findByText(/心跳/, { selector: ".capital-current-none" }))
      .toBeInTheDocument();
    // 生效值區塊整個不存在（沒有可宣稱的生效值）
    expect(document.querySelector(".capital-effective-values.capital-pending")).not.toBeNull();
    expect(screen.queryByText("目前生效值")).not.toBeInTheDocument();
    const pending = document.querySelector(".capital-pending") as HTMLElement;
    expect(within(pending).getByText("9,000.00 USDC")).toBeInTheDocument();
    expect(within(pending).getByText("80.00%")).toBeInTheDocument();
  });

  it("⭐ 未登入時完全不查 /api/me/capital（訪客不該收到 401）", async () => {
    getMe.mockResolvedValue(null);
    render(wrap(<CapitalPage />, null));

    expect(await screen.findByText(/尚未登入|請先登入|未登入/)).toBeInTheDocument();
    expect(getMyCapital).not.toHaveBeenCalled();
  });

  it("changed_at=null → 明說「尚未變更」，不是整列消失", async () => {
    getMyCapital.mockResolvedValue(capitalResp({
      effective: {
        allocated_capital: "5000.00",
        capital_utilization: "0.1500",
        use_full_equity: false,
        source: "env_default",
        changed_at: null,
        as_of: "2026-07-19T00:00:00Z",
      },
    }));
    render(wrap(<CapitalPage />));

    const block = (await screen.findByText("目前生效值"))
      .closest(".capital-effective-values") as HTMLElement;
    expect(within(block).getByText("上次變更時刻")).toBeInTheDocument();
    expect(within(block).getByText("（尚未變更）")).toBeInTheDocument();
  });

  it("⭐ source 出現第三個值 → 原樣顯示，不靜默落進「系統預設」", async () => {
    getMyCapital.mockResolvedValue(capitalResp({
      effective: {
        allocated_capital: "5000.00",
        capital_utilization: "0.1500",
        use_full_equity: false,
        source: "alien_source",
        changed_at: null,
        as_of: "2026-07-19T00:00:00Z",
      },
    } as unknown as Partial<MyCapitalResp>));
    render(wrap(<CapitalPage />));

    const block = (await screen.findByText("目前生效值"))
      .closest(".capital-effective-values") as HTMLElement;
    expect(within(block).getByText(/alien_source/)).toBeInTheDocument();
    expect(within(block).queryByText(/系統預設/)).not.toBeInTheDocument();
  });
});
