import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

const connectAsync = vi.fn(async () => ({ accounts: ["0xAbC0000000000000000000000000000000000001"], chainId: 42161 }));
const signMessageAsync = vi.fn(async () => "0xsiwe-sig");
let mockAccount: { address?: string; chainId?: number; isConnected: boolean } = { isConnected: false };
vi.mock("wagmi", () => ({
  useAccount: () => mockAccount,
  useConnect: () => ({ connectAsync, connectors: [{ id: "injected" }] }),
  useSignMessage: () => ({ signMessageAsync }),
}));

const loginWithSiwe = vi.fn(async () => ({ address: "0xabc", account_id: "fabc" }));
vi.mock("@/lib/siwe", () => ({ loginWithSiwe: (...a: unknown[]) => loginWithSiwe(...a) }));
vi.mock("@/lib/hooks", () => ({ useMe: () => ({ data: null }) }));

import LoginPage from "./page";

beforeEach(() => {
  vi.clearAllMocks();
  mockAccount = { isConnected: false };
});

describe("LoginPage", () => {
  it("connect → SIWE 簽署 → 導向 /onboarding", async () => {
    render(<LoginPage />);
    await userEvent.click(screen.getByRole("button", { name: "連接錢包" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/onboarding"));
    expect(connectAsync).toHaveBeenCalled();
    expect(loginWithSiwe).toHaveBeenCalledWith(
      expect.objectContaining({ address: "0xAbC0000000000000000000000000000000000001", chainId: 42161 }),
    );
  });

  it("錢包拒簽 → 顯示拒簽文案，不導頁", async () => {
    loginWithSiwe.mockRejectedValueOnce(new Error("User rejected the request."));
    render(<LoginPage />);
    await userEvent.click(screen.getByRole("button", { name: "連接錢包" }));
    expect(await screen.findByText(/取消了簽署/)).toBeInTheDocument();
    expect(push).not.toHaveBeenCalled();
  });

  it("非拒簽錯誤（例如後端驗簽失敗）→ 顯示登入失敗文案，不顯示拒簽文案", async () => {
    loginWithSiwe.mockRejectedValueOnce(new Error("401 signature verification failed"));
    render(<LoginPage />);
    await userEvent.click(screen.getByRole("button", { name: "連接錢包" }));
    expect(await screen.findByText(/登入失敗，請稍後再試/)).toBeInTheDocument();
    expect(screen.queryByText(/取消了簽署/)).not.toBeInTheDocument();
    expect(push).not.toHaveBeenCalled();
  });

  it("頁面不存在任何文字輸入框（紅線 1：無處可輸入私鑰/助記詞）", () => {
    render(<LoginPage />);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByText(/永遠不會請你輸入私鑰或助記詞/)).toBeInTheDocument();
  });
});
