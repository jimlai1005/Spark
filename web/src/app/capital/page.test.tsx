import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { BillingPlansResp, CapitalSettingsMessageResp } from "@/lib/api";

const ME = { address: "0xAbC0000000000000000000000000000000000001", account_id: "fabc" };

const getMe = vi.fn();
const getBillingPlans = vi.fn();
const getCapitalSettingsMessage = vi.fn();
const postCapitalSettings = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getMe: (...a: unknown[]) => getMe(...a),
  getBillingPlans: (...a: unknown[]) => getBillingPlans(...a),
  getCapitalSettingsMessage: (...a: unknown[]) => getCapitalSettingsMessage(...a),
  postCapitalSettings: (...a: unknown[]) => postCapitalSettings(...a),
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

  it("⭐ 明說後端沒有「目前生效值／上次變更時間」，不拿預設值冒充現況", async () => {
    render(wrap(<CapitalPage />));

    expect(await screen.findByText(/本頁無法顯示你目前生效的設定值與上次變更時間/))
      .toBeInTheDocument();
    expect(screen.getByText(/不是.*你目前生效的設定/)).toBeInTheDocument();
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
