import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type LeadersResp } from "@/lib/api";

const ME = { address: "0xAbC0000000000000000000000000000000000001", account_id: "fabc" };

const routerPush = vi.fn();
// `mockLeaderQuery`：`?leader=` 預填測試用（M3 round3 Task 4，D9）；vitest 對
// `vi.mock` 工廠內引用的變數，只要前綴是 `mock` 就會與 `vi.mock` 一起被提升，
// 可在宣告位置之後才賦值（同檔既有 `mockConnected` 慣例）。
let mockLeaderQuery = "";
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: routerPush }),
  useSearchParams: () => new URLSearchParams(mockLeaderQuery),
}));

const getMe = vi.fn();
const getLeaders = vi.fn();
const getLeaderPreview = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getMe: (...a: unknown[]) => getMe(...a),
  getLeaders: (...a: unknown[]) => getLeaders(...a),
  getLeaderPreview: (...a: unknown[]) => getLeaderPreview(...a),
}));

const connectAsync = vi.fn();
const signMessageAsync = vi.fn();
let mockConnected = false;
vi.mock("wagmi", () => ({
  useAccount: () => (mockConnected
    ? { address: ME.address, chainId: 999, isConnected: true }
    : { address: undefined, chainId: undefined, isConnected: false }),
  useConnect: () => ({ connectAsync, connectors: [{ id: "injected" }] }),
  useSignMessage: () => ({ signMessageAsync }),
}));

const loginWithSiwe = vi.fn();
vi.mock("@/lib/siwe", () => ({ loginWithSiwe: (...a: unknown[]) => loginWithSiwe(...a) }));

import AdvancedPage from "./page";

function wrap(children: ReactNode, me: typeof ME | null = ME) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(["me"], me);
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function leaders(over: Partial<LeadersResp> = {}): LeadersResp {
  return {
    leaders: [
      {
        address: "0x1111111111111111111111111111111111111111",
        name: "Alpha",
        description: "多幣種網格",
        account_value: "125000.5",
        total_ntl_pos: "340000",
        unrealized_pnl: "-1200.25",
        position_count: 4,
      },
    ],
    stats_available: true,
    stats_day: "2026-07-18",
    stats_as_of: "2026-07-18T00:10:03+00:00",
    note: null,
    ...over,
  } as LeadersResp;
}

const CUSTOM_ADDR = "0x2222222222222222222222222222222222222222";
const CUSTOM_PREVIEW = {
  address: CUSTOM_ADDR, exists: true,
  account_value: "5123.45", position_count: 3, already_listed: false,
  accepting_new: true,
  kind: "standard" as const, vault_checks: null,
};

/** 勾選頁首無背書聲明 checkbox（NOTE 05 的閘門，輸入框在此之前一律 disabled）。 */
async function agreeGate() {
  const box = await screen.findByRole("checkbox", { name: /Filet 不對此地址的策略品質/ });
  await userEvent.click(box);
}

async function pasteAddress(addr: string) {
  const input = await screen.findByLabelText(/leader 錢包位址/);
  await userEvent.click(input);
  await userEvent.paste(addr);
  return input;
}

async function previewCustom(addr = CUSTOM_ADDR) {
  await agreeGate();
  await pasteAddress(addr);
  await userEvent.click(screen.getByRole("button", { name: "查詢" }));
  await screen.findByText("鏈上預覽");
}

beforeEach(() => {
  vi.clearAllMocks();
  mockConnected = false;
  mockLeaderQuery = "";
  getMe.mockResolvedValue(ME);
  getLeaders.mockResolvedValue(leaders());
  getLeaderPreview.mockResolvedValue(CUSTOM_PREVIEW);
});

describe("AdvancedPage — 無背書閘門（NOTE 05）", () => {
  it("⭐ 進頁即顯示無背書聲明，勾選前地址輸入框 disabled", async () => {
    render(wrap(<AdvancedPage />));
    expect(await screen.findByText(/Filet 不對此位址的策略品質、風控或存續做任何背書/))
      .toBeInTheDocument();
    const input = await screen.findByLabelText(/leader 錢包位址/);
    expect(input).toBeDisabled();
    const checkBtn = screen.getByRole("button", { name: "查詢" });
    expect(checkBtn).toBeDisabled();
  });

  it("⭐ 勾選聲明後 → 輸入框可輸入、可查詢", async () => {
    render(wrap(<AdvancedPage />));
    await agreeGate();
    const input = await screen.findByLabelText(/leader 錢包位址/);
    expect(input).not.toBeDisabled();
    await userEvent.click(input);
    await userEvent.paste(CUSTOM_ADDR);
    await waitFor(() => expect(screen.getByRole("button", { name: "查詢" })).not.toBeDisabled());
  });
});

describe("AdvancedPage — `?leader=` 預填（M3 round3 Task 4，D9：/explore「跟單 →」帶入）", () => {
  it("⭐ 帶 ?leader= 進頁、勾選聲明後 → 輸入框已預填該地址（風險勾選仍照舊、不自動送出）", async () => {
    mockLeaderQuery = `leader=${CUSTOM_ADDR}`;
    render(wrap(<AdvancedPage />));
    await agreeGate();
    const input = await screen.findByLabelText(/leader 錢包位址/);
    expect(input).toHaveValue(CUSTOM_ADDR);
    // 預填不等於自動查詢／自動送出——查詢按鈕仍需使用者自己按。
    expect(getLeaderPreview).not.toHaveBeenCalled();
  });

  it("沒有 ?leader= → 輸入框維持空白（既有行為不變）", async () => {
    render(wrap(<AdvancedPage />));
    await agreeGate();
    const input = await screen.findByLabelText(/leader 錢包位址/);
    expect(input).toHaveValue("");
  });
});

describe("AdvancedPage — 地址預覽與導向 onboarding", () => {
  it("⭐ 查詢成功 → 預覽卡顯示帳戶權益、持倉數、位址縮寫", async () => {
    render(wrap(<AdvancedPage />));
    await previewCustom();

    expect(getLeaderPreview).toHaveBeenCalledWith(CUSTOM_ADDR);
    const card = screen.getByText("鏈上預覽").closest(".leader-custom-preview")!;
    expect(card.textContent).toContain("帳戶權益");
    expect(card.textContent).toContain("5,123.45");
    expect(card.textContent).toContain("持倉數");
    expect(card.textContent).toContain("3");
    expect(card.textContent).toContain("0x2222…222");
  });

  it("⭐ 風險聲明未勾選 → 「前往開通」停用；勾選後開放", async () => {
    render(wrap(<AdvancedPage />));
    await previewCustom();

    const proceed = screen.getByRole("button", { name: "前往開通" });
    expect(proceed).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox", { name: /未審核 leader/ }));
    expect(proceed).not.toBeDisabled();
  });

  it("⭐⭐ 選定後 → router.push 帶 advanced:{address} 參數進 /onboarding（不在本頁簽章）", async () => {
    render(wrap(<AdvancedPage />));
    await previewCustom();
    await userEvent.click(screen.getByRole("checkbox", { name: /未審核 leader/ }));
    await userEvent.click(screen.getByRole("button", { name: "前往開通" }));

    expect(routerPush).toHaveBeenCalledWith(`/onboarding?strategy=advanced:${CUSTOM_ADDR}`);
  });

  it("查詢失敗（rejected：自跟）→ 顯示對應文案，零導向", async () => {
    getLeaderPreview.mockRejectedValue(new ApiError("client", "自跟", 400, "自跟", "self_follow"));
    render(wrap(<AdvancedPage />));
    await agreeGate();
    await pasteAddress(CUSTOM_ADDR);
    await userEvent.click(screen.getByRole("button", { name: "查詢" }));

    expect(await screen.findByText(/不能跟單自己/)).toBeInTheDocument();
    expect(routerPush).not.toHaveBeenCalled();
  });
});

describe("AdvancedPage — 未登入", () => {
  it("⭐ 未登入 → 顯示說明＋登入 CTA，不 redirect（進階用戶的直達入口）", async () => {
    render(wrap(<AdvancedPage />, null));

    expect(await screen.findByText(/請先登入以繼續/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "連接錢包並登入" })).toBeInTheDocument();
    expect(routerPush).not.toHaveBeenCalled();
    // 無背書聲明本身在未登入時也顯示（頁首宣示，不等登入才出現）。
    expect(screen.getByText(/Filet 不對此位址的策略品質、風控或存續做任何背書/))
      .toBeInTheDocument();
  });

  it("⭐ M3 round3 Task 8（R2·P1）：未登入合併為單一卡——地址輸入框可見但 disabled，"
    + "並提供「或先看精選策略 →」出口", async () => {
    render(wrap(<AdvancedPage />, null));
    await screen.findByText(/請先登入以繼續/);

    // 輸入框不再整個藏起來：可見，但 disabled（不可送出）。
    const input = screen.getByLabelText(/leader 錢包位址/);
    expect(input).toBeDisabled();

    // 精選策略出口。
    const exit = screen.getByRole("link", { name: /精選策略/ });
    expect(exit).toHaveAttribute("href", "/strategies");
  });

  it("登入 CTA → connect+SIWE 成功後留在本頁（不 push），改顯示位址輸入區", async () => {
    connectAsync.mockImplementation(async () => {
      mockConnected = true;
      return { accounts: [ME.address], chainId: 999 };
    });
    signMessageAsync.mockResolvedValue(`0x${"ab".repeat(65)}`);
    loginWithSiwe.mockResolvedValue(ME);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    qc.setQueryData(["me"], null);
    getMe.mockResolvedValue(ME); // 登入後 useMe 重抓會拿到已登入身分

    render(<QueryClientProvider client={qc}><AdvancedPage /></QueryClientProvider>);
    await userEvent.click(await screen.findByRole("button", { name: "連接錢包並登入" }));

    expect(await screen.findByLabelText(/leader 錢包位址/)).toBeInTheDocument();
    expect(routerPush).not.toHaveBeenCalled();
  });
});
