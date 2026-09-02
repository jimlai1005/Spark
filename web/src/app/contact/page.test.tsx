/**
 * `/contact` 頁測試（2026-09-02，Task 3）。無需登入、不掛 LangProvider（`useCopy` 預設
 * context 值就是 zh，同 `status/page.test.tsx` 的渲染慣例）。涵蓋：渲染三欄位＋保底
 * mailto；客端驗證擋下不合法輸入；成功送出後表單被成功卡取代；後端 detail（422/429/502）
 * 原樣顯示、network 顯示 errNetwork。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import ContactPage from "./page";
import { COPY_ZH } from "@/lib/copy";
import { ApiError } from "@/lib/api";

vi.mock("@/lib/api", async (orig) => {
  const mod = await orig<typeof import("@/lib/api")>();
  return { ...mod, postContact: vi.fn() };
});
import { postContact } from "@/lib/api";
const mocked = vi.mocked(postContact);

function renderPage() {
  return render(<ContactPage />);
}

function fill(name: string, email: string, message: string) {
  fireEvent.change(screen.getByLabelText(COPY_ZH.contact.nameLabel), { target: { value: name } });
  fireEvent.change(screen.getByLabelText(COPY_ZH.contact.emailLabel), { target: { value: email } });
  fireEvent.change(screen.getByLabelText(COPY_ZH.contact.messageLabel), { target: { value: message } });
}

describe("/contact", () => {
  beforeEach(() => mocked.mockReset());

  it("渲染標題、三欄位、送出鈕與保底 mailto", () => {
    renderPage();
    expect(screen.getByRole("heading", { name: COPY_ZH.contact.heading })).toBeInTheDocument();
    expect(screen.getByLabelText(COPY_ZH.contact.nameLabel)).toBeInTheDocument();
    expect(screen.getByLabelText(COPY_ZH.contact.emailLabel)).toBeInTheDocument();
    expect(screen.getByLabelText(COPY_ZH.contact.messageLabel)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: COPY_ZH.contact.fallbackEmail }))
      .toHaveAttribute("href", "mailto:goldwisetw@gmail.com");
  });

  it("客端驗證：空姓名／壞 email／短訊息不打 API", () => {
    renderPage();
    fill("", "nope", "short");
    fireEvent.click(screen.getByRole("button", { name: COPY_ZH.contact.send }));
    expect(mocked).not.toHaveBeenCalled();
    expect(screen.getByText(COPY_ZH.contact.errNameRequired)).toBeInTheDocument();
  });

  it("成功送出 → 顯示成功卡、表單消失", async () => {
    mocked.mockResolvedValue({ ok: true });
    renderPage();
    fill("Jim", "jim@example.com", "Hello, a question about fees.");
    fireEvent.click(screen.getByRole("button", { name: COPY_ZH.contact.send }));
    await waitFor(() => expect(screen.getByText(COPY_ZH.contact.successTitle)).toBeInTheDocument());
    expect(mocked).toHaveBeenCalledWith({
      name: "Jim", email: "jim@example.com", message: "Hello, a question about fees.", website: "",
    });
    expect(screen.queryByLabelText(COPY_ZH.contact.nameLabel)).not.toBeInTheDocument();
  });

  it("後端 detail（422/429/502）原樣顯示；network 顯示 errNetwork", async () => {
    // ⭐ ApiError 的第 2 個參數（message）與第 4 個參數（detail）在真實 request()
    // 建構時恆為同一份人話字串（api.ts:156-166）；page.tsx 讀的是 `e.detail`，
    // 這裡照實際建構慣例把兩者都填，而不是只填 message（plan 骨架省略了 detail）。
    mocked.mockRejectedValueOnce(
      new ApiError("client", "送出過於頻繁，請稍後再試", 429, "送出過於頻繁，請稍後再試"));
    renderPage();
    fill("Jim", "jim@example.com", "Hello, a question about fees.");
    fireEvent.click(screen.getByRole("button", { name: COPY_ZH.contact.send }));
    await waitFor(() => expect(screen.getByText("送出過於頻繁，請稍後再試")).toBeInTheDocument());
    // 表單仍在，可重試
    expect(screen.getByLabelText(COPY_ZH.contact.nameLabel)).toBeInTheDocument();

    mocked.mockRejectedValueOnce(new ApiError("network", "x"));
    fireEvent.click(screen.getByRole("button", { name: COPY_ZH.contact.send }));
    await waitFor(() => expect(screen.getByText(COPY_ZH.contact.errNetwork)).toBeInTheDocument());
  });
});
