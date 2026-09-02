/**
 * `/contact` 頁測試（2026-09-02，Task 6：雙欄深色設計稿重做）。無需登入、不掛
 * LangProvider（`useCopy` 預設 context 值就是 zh，同 `status/page.test.tsx` 的渲染
 * 慣例）；`useMe` 走 `@/lib/hooks` mock（同 `settings/page.test.tsx` 慣例）。涵蓋：
 * 渲染（眉標／h1／五顆主題 chip／欄位／確認清單／安全提示）；未登入錢包欄可編輯；
 * 已登入錢包欄自動帶入＋徽章＋「改填其他地址」；客端驗證（壞 email／壞錢包／短
 * 訊息）；訊息字數計數；成功送出顯示工單編號並可「再送一則」；錯誤映射
 * （client detail 原樣／upstream 帶 mailto／network）。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import ContactPage from "./page";
import { COPY_ZH } from "@/lib/copy";
import { ApiError } from "@/lib/api";

let mockMe: { data: { address: string; account_id: string } | null };
vi.mock("@/lib/hooks", () => ({
  useMe: () => mockMe,
}));

vi.mock("@/lib/api", async (orig) => {
  const mod = await orig<typeof import("@/lib/api")>();
  return { ...mod, postContact: vi.fn() };
});
import { postContact } from "@/lib/api";
const mocked = vi.mocked(postContact);

const c = COPY_ZH.contact;
const ADDR = "0x" + "ab".repeat(20);

function renderPage() {
  return render(<ContactPage />);
}

function fillEmail(value: string) {
  fireEvent.change(screen.getByLabelText(c.emailLabel, { exact: false }), { target: { value } });
}

function fillMessage(value: string) {
  fireEvent.change(screen.getByLabelText(c.messageLabel, { exact: false }), { target: { value } });
}

function walletInput() {
  return screen.getByLabelText(c.walletLabel) as HTMLInputElement;
}

function submit() {
  fireEvent.click(screen.getByRole("button", { name: c.send }));
}

describe("/contact", () => {
  beforeEach(() => {
    mocked.mockReset();
    mockMe = { data: null };
  });

  it("渲染：眉標、h1、五顆主題 chip（預設「跟單問題」選中）、Email/錢包/訊息欄位、送出鈕、確認清單、安全提示", () => {
    renderPage();
    expect(screen.getByText(c.eyebrow)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: c.heading })).toBeInTheDocument();

    const radios = screen.getAllByRole("radio");
    expect(radios).toHaveLength(5);
    const active = radios.find((r) => r.getAttribute("aria-checked") === "true");
    expect(active).toHaveTextContent(c.topics.copytrade);
    for (const t of Object.values(c.topics)) {
      expect(screen.getByRole("radio", { name: t })).toBeInTheDocument();
    }

    expect(screen.getByLabelText(c.emailLabel, { exact: false })).toBeInTheDocument();
    expect(walletInput()).toBeInTheDocument();
    expect(screen.getByLabelText(c.messageLabel, { exact: false })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: c.send })).toBeInTheDocument();

    expect(screen.getByText(c.checklistTitle)).toBeInTheDocument();
    expect(screen.getByText(c.check1)).toBeInTheDocument();
    expect(screen.getByText(c.check2)).toBeInTheDocument();
    expect(screen.getByText(c.checkWarnStrong)).toBeInTheDocument();
    expect(screen.getByText(c.securityNote)).toBeInTheDocument();
  });

  it("未登入：錢包欄可編輯、無「已連結錢包」徽章", () => {
    renderPage();
    const input = walletInput();
    expect(input).not.toHaveAttribute("readonly");
    expect(screen.queryByText(c.walletConnected)).not.toBeInTheDocument();
    fireEvent.change(input, { target: { value: "0xabc" } });
    expect(input).toHaveValue("0xabc");
  });

  it("已登入：錢包欄 readOnly＋顯示 shortAddr＋徽章；送出時帶完整地址；「改填其他地址」後可編輯且徽章消失", async () => {
    mockMe = { data: { address: ADDR, account_id: "acc1" } };
    mocked.mockResolvedValue({ ok: true, ticket: "FLT-AB12-CD34" });
    renderPage();

    const input = walletInput();
    expect(input).toHaveAttribute("readonly");
    expect(input).toHaveValue(`${ADDR.slice(0, 6)}…${ADDR.slice(-3)}`);
    expect(screen.getByText(c.walletConnected)).toBeInTheDocument();

    fillEmail("jim@example.com");
    fillMessage("Hello, a question about fees and copy trading.");
    submit();
    await waitFor(() => expect(mocked).toHaveBeenCalled());
    expect(mocked).toHaveBeenCalledWith(
      expect.objectContaining({ wallet: ADDR }),
    );

    // 送出中已在成功卡；重置回表單以測試「改填其他地址」互動。
    await waitFor(() => expect(screen.getByText(c.successTitle)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: c.sendAnother }));

    fireEvent.click(screen.getByRole("button", { name: c.walletUseOther }));
    const editable = walletInput();
    expect(editable).not.toHaveAttribute("readonly");
    expect(screen.queryByText(c.walletConnected)).not.toBeInTheDocument();
    expect(editable).toHaveValue("");
  });

  it("客端驗證：壞 email 不打 API", () => {
    renderPage();
    fillEmail("nope");
    fillMessage("Hello, a question about fees.");
    submit();
    expect(mocked).not.toHaveBeenCalled();
    expect(screen.getByText(c.errEmailInvalid)).toBeInTheDocument();
  });

  it("客端驗證：壞錢包地址不打 API", () => {
    renderPage();
    fillEmail("jim@example.com");
    fireEvent.change(walletInput(), { target: { value: "0x123" } });
    fillMessage("Hello, a question about fees.");
    submit();
    expect(mocked).not.toHaveBeenCalled();
    expect(screen.getByText(c.errWalletInvalid)).toBeInTheDocument();
  });

  it("客端驗證：短訊息不打 API", () => {
    renderPage();
    fillEmail("jim@example.com");
    fillMessage("short");
    submit();
    expect(mocked).not.toHaveBeenCalled();
    expect(screen.getByText(c.errMessageLength)).toBeInTheDocument();
  });

  it("字數計數：輸入 12 字後顯示 12 / 2000", () => {
    renderPage();
    fillMessage("123456789012");
    expect(screen.getByText("12 / 2000")).toBeInTheDocument();
  });

  it("成功：顯示已送出與工單編號、表單消失；「再送一則」回到表單且主題重置", async () => {
    mocked.mockResolvedValue({ ok: true, ticket: "FLT-AB12-CD34" });
    renderPage();

    fireEvent.click(screen.getByRole("radio", { name: c.topics.security }));
    fillEmail("jim@example.com");
    fillMessage("Hello, a question about fees.");
    submit();

    await waitFor(() => expect(screen.getByText(c.successTitle)).toBeInTheDocument());
    expect(screen.getByText("FLT-AB12-CD34")).toBeInTheDocument();
    expect(screen.queryByLabelText(c.emailLabel, { exact: false })).not.toBeInTheDocument();
    expect(mocked).toHaveBeenCalledWith(
      expect.objectContaining({ topic: "security", email: "jim@example.com" }),
    );

    fireEvent.click(screen.getByRole("button", { name: c.sendAnother }));
    expect(screen.getByLabelText(c.emailLabel, { exact: false })).toBeInTheDocument();
    const active = screen
      .getAllByRole("radio")
      .find((r) => r.getAttribute("aria-checked") === "true");
    expect(active).toHaveTextContent(c.topics.copytrade);
  });

  it("錯誤：client detail 原樣顯示；upstream detail 顯示並含 mailto 連結；network 顯示 errNetwork", async () => {
    mocked.mockRejectedValueOnce(
      new ApiError("client", "送出過於頻繁，請稍後再試", 429, "送出過於頻繁，請稍後再試"));
    renderPage();
    fillEmail("jim@example.com");
    fillMessage("Hello, a question about fees.");
    submit();
    await waitFor(() => expect(screen.getByText("送出過於頻繁，請稍後再試")).toBeInTheDocument());
    expect(screen.queryByRole("link", { name: c.fallbackEmail })).not.toBeInTheDocument();

    mocked.mockRejectedValueOnce(
      new ApiError("upstream", "上游服務暫時不可用", 503, "上游服務暫時不可用"));
    submit();
    await waitFor(() => expect(screen.getByText(/上游服務暫時不可用/)).toBeInTheDocument());
    expect(screen.getByRole("link", { name: c.fallbackEmail }))
      .toHaveAttribute("href", `mailto:${c.fallbackEmail}`);

    mocked.mockRejectedValueOnce(new ApiError("network", "x"));
    submit();
    await waitFor(() => expect(screen.getByText(c.errNetwork)).toBeInTheDocument());
  });
});
