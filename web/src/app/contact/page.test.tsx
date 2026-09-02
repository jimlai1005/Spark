/**
 * `/contact` 頁測試（設計稿 R3-01～R3-08，Task 7）。無需登入、不掛 LangProvider；
 * `useMe` 走 `@/lib/hooks` mock。涵蓋：渲染；R3-01 無信箱字串；R3-02 送出 body 含 page_url／user_agent；
 * R3-06 錢包自動帶入＋徽章＋清除；R3-07 送出中／4xx detail／5xx 與網路統一文案／成功卡（工單、複製、回首頁）。
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
const MSG = "Hello, a question about fees.";

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

  it("渲染：眉標、h1、五顆 chip（預設跟單問題）、三欄位、送出鈕、確認清單、安全提示", () => {
    renderPage();
    expect(screen.getByText(c.eyebrow)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: c.heading })).toBeInTheDocument();
    const radios = screen.getAllByRole("radio");
    expect(radios).toHaveLength(5);
    expect(screen.getByRole("radio", { name: c.topics.copytrade })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByLabelText(c.emailLabel, { exact: false })).toBeInTheDocument();
    expect(walletInput()).toBeInTheDocument();
    expect(screen.getByLabelText(c.messageLabel, { exact: false })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: c.send })).toBeInTheDocument();
    expect(screen.getByText(c.check1)).toBeInTheDocument();
    expect(screen.getByText(c.securityNote)).toBeInTheDocument();
  });

  it("R3-01：頁面上沒有 mailto 與任何 @ 信箱字串", () => {
    const { container } = renderPage();
    expect(container.querySelector('a[href^="mailto:"]')).toBeNull();
    expect(container.textContent).not.toMatch(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/);
  });

  it("未登入：錢包欄可編輯、無徽章", () => {
    renderPage();
    expect(walletInput()).not.toHaveAttribute("readonly");
    expect(screen.queryByText(c.walletConnected)).toBeNull();
  });

  it("R3-06 已登入：readOnly＋短地址＋徽章；送出帶完整地址；「清除」後可編輯且徽章消失", async () => {
    mockMe = { data: { address: ADDR, account_id: "x" } };
    mocked.mockResolvedValue({ ok: true, ticket: "FLT-2609-0001" });
    renderPage();
    expect(walletInput()).toHaveAttribute("readonly");
    expect(walletInput().value).toContain("…");
    expect(screen.getByText(c.walletConnected)).toBeInTheDocument();
    fillEmail("jim@example.com");
    fillMessage(MSG);
    submit();
    await waitFor(() => expect(mocked).toHaveBeenCalledTimes(1));
    expect(mocked.mock.calls[0][0].wallet).toBe(ADDR);
  });

  it("R3-06 清除鈕", () => {
    mockMe = { data: { address: ADDR, account_id: "x" } };
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: c.walletClear }));
    expect(walletInput()).not.toHaveAttribute("readonly");
    expect(walletInput().value).toBe("");
    expect(screen.queryByText(c.walletConnected)).toBeNull();
  });

  it("R3-02 送出 body 含 topic/email/wallet/message/page_url/user_agent/website", async () => {
    mocked.mockResolvedValue({ ok: true, ticket: "FLT-2609-0001" });
    renderPage();
    fireEvent.click(screen.getByRole("radio", { name: c.topics.billing }));
    fillEmail(" jim@example.com ");
    fillMessage(MSG);
    submit();
    await waitFor(() => expect(mocked).toHaveBeenCalledTimes(1));
    const body = mocked.mock.calls[0][0];
    expect(body.topic).toBe("billing");
    expect(body.email).toBe("jim@example.com");
    expect(body.wallet).toBe("");
    expect(body.message).toBe(MSG);
    expect(typeof body.page_url).toBe("string");
    expect(typeof body.user_agent).toBe("string");
    expect(body.website).toBe("");
  });

  it("客端驗證：壞 email／壞錢包／短訊息各自顯示錯誤且不打 API", () => {
    renderPage();
    fillEmail("nope");
    fillMessage(MSG);
    submit();
    expect(screen.getByRole("alert")).toHaveTextContent(c.errEmailInvalid);
    fillEmail("jim@example.com");
    fireEvent.change(walletInput(), { target: { value: "0x123" } });
    submit();
    expect(screen.getByRole("alert")).toHaveTextContent(c.errWalletInvalid);
    fireEvent.change(walletInput(), { target: { value: "" } });
    fillMessage("short");
    submit();
    expect(screen.getByRole("alert")).toHaveTextContent(c.errMessageLength);
    expect(mocked).not.toHaveBeenCalled();
  });

  it("字數計數：輸入 12 字後顯示 12 / 2000", () => {
    renderPage();
    fillMessage("123456789012");
    expect(screen.getByText("12 / 2000")).toBeInTheDocument();
  });

  it("R3-07 成功：同位置成功卡（標題、工單、{email} 代入、複製、回首頁），表單消失", async () => {
    mocked.mockResolvedValue({ ok: true, ticket: "FLT-2609-0412" });
    renderPage();
    fillEmail("jim@example.com");
    fillMessage(MSG);
    submit();
    await waitFor(() => expect(screen.getByText(c.successTitle)).toBeInTheDocument());
    expect(screen.getByText("FLT-2609-0412")).toBeInTheDocument();
    expect(screen.getByText(c.successBody.replace("{email}", "jim@example.com"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: c.copy })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: c.backHome })).toHaveAttribute("href", "/");
    expect(screen.queryByLabelText(c.emailLabel, { exact: false })).toBeNull();
  });

  it("R3-07 錯誤：4xx detail 原樣；5xx 與網路皆顯示 errSendFailed，內容保留", async () => {
    mocked.mockRejectedValueOnce(new ApiError("client", "送出太頻繁，請稍後再試", 429, "送出太頻繁，請稍後再試"));
    renderPage();
    fillEmail("jim@example.com");
    fillMessage(MSG);
    submit();
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("送出太頻繁，請稍後再試"));
    expect((screen.getByLabelText(c.messageLabel, { exact: false }) as HTMLTextAreaElement).value).toBe(MSG);

    mocked.mockRejectedValueOnce(new ApiError("upstream", "x", 502, "x"));
    submit();
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(c.errSendFailed));

    mocked.mockRejectedValueOnce(new ApiError("network", "x"));
    submit();
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(c.errSendFailed));
  });
});
