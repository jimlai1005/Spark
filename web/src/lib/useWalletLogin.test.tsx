import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const connectAsync = vi.fn();
const signMessageAsync = vi.fn();
let accountState: { address?: string; chainId?: number; isConnected: boolean; connector?: { id: string; name: string } } = { isConnected: false };
let connectorsState: Array<{ id: string; name: string; icon?: string }> = [{ id: "injected", name: "Injected" }];
vi.mock("wagmi", () => ({
  useAccount: () => accountState,
  useConnect: () => ({ connectAsync, connectors: connectorsState }),
  useSignMessage: () => ({ signMessageAsync }),
}));
const loginWithSiwe = vi.fn();
vi.mock("@/lib/siwe", () => ({ loginWithSiwe: (...a: unknown[]) => loginWithSiwe(...a) }));

import { COPY_ZH } from "@/lib/copy";
import { LangProvider } from "@/lib/lang";
import { WalletPicker } from "@/components/WalletPicker";
import { useWalletLogin } from "./useWalletLogin";

const metamask = { id: "io.metamask", name: "MetaMask" };
const rabby = { id: "io.rabby", name: "Rabby Wallet" };

function Harness({ onSuccess, connectOnly }: { onSuccess?: (me: unknown) => void; connectOnly?: boolean }) {
  const login = useWalletLogin({ onSuccess, connectOnly });
  return (
    <div>
      <button type="button" onClick={() => void login.start()}>go</button>
      <output data-testid="phase">{login.phase}</output>
      {login.error && <p role="alert">{login.error}</p>}
      {login.pickerOpen && (
        <WalletPicker candidates={login.candidates} onPick={(c) => void login.choose(c)} onCancel={login.cancelPicker} />
      )}
    </div>
  );
}

function renderHarness(props: Parameters<typeof Harness>[0] = {}) {
  const qc = new QueryClient();
  const resetSpy = vi.spyOn(qc, "resetQueries");
  render(
    <QueryClientProvider client={qc}>
      <LangProvider><Harness {...props} /></LangProvider>
    </QueryClientProvider>,
  );
  return { resetSpy };
}

beforeEach(() => {
  vi.clearAllMocks();
  accountState = { isConnected: false };
  connectorsState = [{ id: "injected", name: "Injected" }];
  connectAsync.mockResolvedValue({ accounts: ["0xabc"], chainId: 42161 });
  loginWithSiwe.mockResolvedValue({ address: "0xabc", account_id: "acc" });
});

describe("useWalletLogin", () => {
  it("只有 injected 後備 → 直接連、簽、resetQueries、onSuccess 收到 me", async () => {
    const onSuccess = vi.fn();
    const { resetSpy } = renderHarness({ onSuccess });
    await userEvent.click(screen.getByRole("button", { name: "go" }));
    await waitFor(() => expect(onSuccess).toHaveBeenCalledWith({ address: "0xabc", account_id: "acc" }));
    expect(connectAsync).toHaveBeenCalledWith({ connector: connectorsState[0] });
    expect(loginWithSiwe).toHaveBeenCalledWith(expect.objectContaining({ address: "0xabc", chainId: 42161 }));
    expect(resetSpy).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByTestId("phase")).toHaveTextContent("idle");
  });

  it("多個發現到的錢包 → 先出選單，選了才連；取消則不連", async () => {
    connectorsState = [{ id: "injected", name: "Injected" }, metamask, rabby];
    const onSuccess = vi.fn();
    renderHarness({ onSuccess });
    await userEvent.click(screen.getByRole("button", { name: "go" }));
    expect(connectAsync).not.toHaveBeenCalled();
    const dialog = screen.getByRole("dialog", { name: COPY_ZH.login.pickerTitle });
    expect(dialog).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Injected/ })).toBeNull(); // 後備不列
    await userEvent.click(screen.getByRole("button", { name: COPY_ZH.login.pickerCancel }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(connectAsync).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "go" }));
    await userEvent.click(screen.getByRole("button", { name: /Rabby Wallet/ }));
    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1));
    expect(connectAsync).toHaveBeenCalledWith({ connector: rabby });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("已連線 → 跳過 connect 直接簽", async () => {
    accountState = { address: "0xdef", chainId: 1, isConnected: true };
    renderHarness();
    await userEvent.click(screen.getByRole("button", { name: "go" }));
    await waitFor(() => expect(loginWithSiwe).toHaveBeenCalledWith(expect.objectContaining({ address: "0xdef", chainId: 1 })));
    expect(connectAsync).not.toHaveBeenCalled();
  });

  it("拒簽 → rejected 文案；ProviderNotFoundError → noWallet 文案；其他 → loginFailed", async () => {
    loginWithSiwe.mockRejectedValueOnce({ name: "UserRejectedRequestError", message: "User rejected" });
    renderHarness();
    await userEvent.click(screen.getByRole("button", { name: "go" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(COPY_ZH.login.rejected));

    connectAsync.mockRejectedValueOnce({ name: "ProviderNotFoundError", message: "Provider not found." });
    await userEvent.click(screen.getByRole("button", { name: "go" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(COPY_ZH.login.noWallet));

    loginWithSiwe.mockRejectedValueOnce(new Error("boom"));
    await userEvent.click(screen.getByRole("button", { name: "go" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(COPY_ZH.login.loginFailed));
  });

  it("connectOnly：連上就 onSuccess，不簽、不 resetQueries", async () => {
    const onSuccess = vi.fn();
    const { resetSpy } = renderHarness({ onSuccess, connectOnly: true });
    await userEvent.click(screen.getByRole("button", { name: "go" }));
    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1));
    expect(loginWithSiwe).not.toHaveBeenCalled();
    expect(resetSpy).not.toHaveBeenCalled();
  });

  it("已連線但偵測到多個錢包 → 仍出選單；選目前已連的那個直接簽、不重連；選別的才 connectAsync", async () => {
    connectorsState = [{ id: "injected", name: "Injected" }, metamask, rabby];
    accountState = { address: "0xdef", chainId: 1, isConnected: true, connector: metamask };
    const onSuccess = vi.fn();
    renderHarness({ onSuccess });
    await userEvent.click(screen.getByRole("button", { name: "go" }));
    expect(screen.getByRole("dialog", { name: COPY_ZH.login.pickerTitle })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /MetaMask/ }));
    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1));
    expect(connectAsync).not.toHaveBeenCalled();
    expect(loginWithSiwe).toHaveBeenCalledWith(expect.objectContaining({ address: "0xdef", chainId: 1 }));

    await userEvent.click(screen.getByRole("button", { name: "go" }));
    await userEvent.click(screen.getByRole("button", { name: /Rabby Wallet/ }));
    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(2));
    expect(connectAsync).toHaveBeenCalledWith({ connector: rabby });
  });
});
