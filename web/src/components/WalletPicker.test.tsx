import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { COPY_ZH } from "@/lib/copy";
import { LangProvider } from "@/lib/lang";
import { WalletPicker } from "./WalletPicker";

const metamask = { id: "io.metamask", name: "MetaMask", icon: "data:image/svg+xml;base64,AAA" };
const rabby = { id: "io.rabby", name: "Rabby Wallet" };

describe("WalletPicker", () => {
  it("列出每個候選錢包的名稱；有 icon 才渲染圖片", () => {
    render(
      <LangProvider>
        <WalletPicker candidates={[metamask, rabby]} onPick={vi.fn()} onCancel={vi.fn()} />
      </LangProvider>,
    );
    expect(screen.getByRole("dialog", { name: COPY_ZH.login.pickerTitle })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /MetaMask/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Rabby Wallet/ })).toBeInTheDocument();
    expect(document.querySelectorAll("img")).toHaveLength(1);
  });

  it("點某個錢包 → onPick 收到該 connector；點取消 → onCancel", async () => {
    const onPick = vi.fn();
    const onCancel = vi.fn();
    render(
      <LangProvider>
        <WalletPicker candidates={[metamask, rabby]} onPick={onPick} onCancel={onCancel} />
      </LangProvider>,
    );
    await userEvent.click(screen.getByRole("button", { name: /Rabby Wallet/ }));
    expect(onPick).toHaveBeenCalledWith(rabby);
    await userEvent.click(screen.getByRole("button", { name: COPY_ZH.login.pickerCancel }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("busy 時全部按鈕 disabled", () => {
    render(
      <LangProvider>
        <WalletPicker candidates={[metamask, rabby]} busy onPick={vi.fn()} onCancel={vi.fn()} />
      </LangProvider>,
    );
    for (const b of screen.getAllByRole("button")) expect(b).toBeDisabled();
  });
});
