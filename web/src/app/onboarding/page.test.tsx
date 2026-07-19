import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { OnboardStatus } from "@/lib/api";

let mockWagmiAccount: { isConnected: boolean; address?: string; chainId?: number };
const connect = vi.fn();
vi.mock("wagmi", () => ({
  useAccount: () => mockWagmiAccount,
  useConnect: () => ({ connect, connectors: [{ id: "injected" }], isPending: false }),
  useConnectorClient: () => ({ data: { request: vi.fn() } }),
}));
let mockMe: { data: { address: string; account_id: string } | null; isLoading: boolean };
let mockStatus: { data: OnboardStatus | null; refetch: () => void };
vi.mock("@/lib/hooks", () => ({
  useMe: () => mockMe,
  useOnboardingStatus: () => mockStatus,
}));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  createAgent: vi.fn(async () => ({ agent_address: "0xa" })),
}));

import OnboardingPage from "./page";

function status(over: Partial<OnboardStatus> = {}): OnboardStatus {
  return {
    address: "0xabc0000000000000000000000000000000000001", account_id: "fabc",
    agent_address: null, agent_generated: false, builder_fee_approved: false,
    agent_approved: false, funded: false, spot_stranded: null, state: "IN_PROGRESS",
    ...over,
  };
}

/** 後端 `_spot_stranded` 的實際形狀（Decimal → string）。 */
const STRANDED = {
  usdc: "250.5", threshold: "10",
  action_required: "manual_transfer_spot_to_perp",
  note: "你有 250.5 USDC 在 **spot** 錢包。",
};

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
  mockWagmiAccount = { isConnected: true, address: "0xAbC0000000000000000000000000000000000001", chainId: 42161 };
  mockMe = { data: { address: "0xabc0000000000000000000000000000000000001", account_id: "fabc" }, isLoading: false };
  mockStatus = { data: status(), refetch: () => undefined };
});

describe("OnboardingPage 斷點續走渲染", () => {
  it("未登入 → 導回登入的提示", () => {
    mockMe = { data: null, isLoading: false };
    render(<OnboardingPage />);
    expect(screen.getByText(/尚未登入/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "回登入頁" })).toHaveAttribute("href", "/");
  });

  it("已登入未勾風險 → step 2（風險確認）", () => {
    render(<OnboardingPage />);
    expect(screen.getByText("請確認以下事項")).toBeInTheDocument();
  });

  it("勾過風險 → step 3（簽署授權）", () => {
    localStorage.setItem("filet.risk-confirmed.0xabc0000000000000000000000000000000000001", "1");
    render(<OnboardingPage />);
    expect(screen.getByText("簽署兩筆授權")).toBeInTheDocument();
  });

  it("鏈上雙授權已生效 → step 4（入金），未勾風險也直達", () => {
    mockStatus = {
      data: status({ agent_generated: true, agent_address: "0xa", agent_approved: true, builder_fee_approved: true }),
      refetch: () => undefined,
    };
    render(<OnboardingPage />);
    expect(screen.getByText("入金檢查")).toBeInTheDocument();
  });

  it("⭐ session 有效但錢包未連（隔天回來錢包鎖住）→ step 3 顯示重連閘，非死路（Finding 1）", async () => {
    mockWagmiAccount = { isConnected: false };
    localStorage.setItem("filet.risk-confirmed.0xabc0000000000000000000000000000000000001", "1");
    render(<OnboardingPage />);
    // 仍在 step 3（不回退 step 1），但內容是重連閘而非 disabled 簽署鈕
    expect(screen.getByText("錢包未連接")).toBeInTheDocument();
    expect(screen.queryByText("簽署兩筆授權")).not.toBeInTheDocument();
    const btn = screen.getByRole("button", { name: "重新連接錢包" });
    const userEvent = (await import("@testing-library/user-event")).default;
    await userEvent.click(btn);
    expect(connect).toHaveBeenCalledWith({ connector: expect.objectContaining({ id: "injected" }) });
  });

  it("session 有效但錢包未連、鏈上雙授權已生效 → step 4 同樣顯示重連閘", () => {
    mockWagmiAccount = { isConnected: false };
    mockStatus = {
      data: status({ agent_generated: true, agent_address: "0xa", agent_approved: true, builder_fee_approved: true }),
      refetch: () => undefined,
    };
    render(<OnboardingPage />);
    expect(screen.getByText("錢包未連接")).toBeInTheDocument();
    expect(screen.queryByText("入金檢查")).not.toBeInTheDocument();
  });
});

/**
 * ⭐ 錢卡在 spot 錢包的提示。存在的理由：我方只鏡像 **perp**，客戶從 CEX 提幣或
 * 走橋入金時錢會落在 spot，畫面卻只寫「尚未偵測到足額資金」——這是入金漏斗上最貴
 * 的一種沉默。
 *
 * ⚠️⚠️ 這組測試最重要的一條是**最後一條**：畫面上永遠不得出現「幫我劃轉」按鈕。
 * 劃轉需要客戶的主鑰簽章，我方結構上不持有主鑰，那顆按鈕是一個我們兌現不了的承諾。
 */
describe("OnboardingPage — 資金卡在 spot 的提示", () => {
  it("⭐ spot_stranded 為 null → 完全不顯示（不是顯示「無卡住資金」）", () => {
    mockStatus = { data: status({ spot_stranded: null }), refetch: () => undefined };
    render(<OnboardingPage />);
    // null 同時代表「沒有卡住的錢」與「查詢失敗」，後者不該被畫成一句肯定的結論
    expect(screen.queryByText(/停在 spot 錢包/)).not.toBeInTheDocument();
    expect(document.querySelectorAll(".spot-stranded")).toHaveLength(0);
  });

  it("⭐ 有卡住的資金 → 說明「有多少錢、卡在哪、要做什麼」", () => {
    mockStatus = { data: status({ spot_stranded: STRANDED }), refetch: () => undefined };
    render(<OnboardingPage />);

    const box = document.querySelector(".spot-stranded")!;
    expect(box).not.toBeNull();
    expect(box.textContent).toContain("250.50 USDC");      // 金額取自後端
    expect(box.textContent).toMatch(/停在 spot 錢包/);
    // 為什麼要動它：跟單只用 perp
    expect(box.textContent).toMatch(/跟單只使用永續合約（perp）帳戶/);
    expect(box.textContent).toMatch(/劃轉到 perp/);
  });

  it("⭐ 明說「只能你自己動手」，並給外部連結（不是站內動作）", () => {
    mockStatus = { data: status({ spot_stranded: STRANDED }), refetch: () => undefined };
    render(<OnboardingPage />);

    const box = document.querySelector(".spot-stranded")!;
    expect(box.textContent).toMatch(/我們不持有你的主鑰/);
    const link = screen.getByRole("link", { name: "前往 Hyperliquid 進行劃轉" });
    expect(link).toHaveAttribute("href", "https://app.hyperliquid.xyz/balances");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));
  });

  it("⭐⭐ 絕不得出現任何「幫我劃轉」按鈕——劃轉需要主鑰，我方結構上做不到", () => {
    mockStatus = { data: status({ spot_stranded: STRANDED }), refetch: () => undefined };
    render(<OnboardingPage />);

    const box = document.querySelector(".spot-stranded")!;
    // 這一區裡不得有任何 button（只能有說明文字與外部連結）
    expect(box.querySelectorAll("button")).toHaveLength(0);
    // 也不得有任何看起來像代為操作的字眼
    for (const w of ["幫你轉", "幫我轉", "一鍵", "代為劃轉", "自動劃轉"]) {
      expect(box.textContent).not.toContain(w);
    }
  });
});
