/** `/status` 頁測試（Task 12）：三態（ok/degraded/unknown＋載入失敗）皆可讀，不炸。 */
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { COPY_ZH as COPY } from "@/lib/copy";
import StatusPage from "./page";

function jsonResponse(body: unknown, ok = true): Response {
  return { ok, json: async () => body } as Response;
}

function stubFetch(impl: () => Response) {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(impl())));
}

function stubFetchReject(err: unknown) {
  vi.stubGlobal("fetch", vi.fn(() => Promise.reject(err)));
}

describe("StatusPage", () => {
  it("ok 態：整體狀態＋components＋updated_at", async () => {
    stubFetch(() => jsonResponse({
      status: "ok",
      components: [{ name: "engine", status: "ok" }, { name: "billing", status: "ok" }],
      updated_at: 1724805063,
    }));
    render(<StatusPage />);
    await screen.findAllByText(COPY.footer.statusOk);
    expect(screen.getByText("engine")).toBeInTheDocument();
    expect(screen.getByText("billing")).toBeInTheDocument();
    expect(screen.getByText(/2024-08-28/)).toBeInTheDocument();
  });

  it("degraded 態：整體與個別元件的 degraded 標籤都顯示", async () => {
    stubFetch(() => jsonResponse({
      status: "degraded",
      components: [{ name: "engine", status: "degraded" }],
      updated_at: 1724805063,
    }));
    render(<StatusPage />);
    const degradedLabels = await screen.findAllByText(COPY.footer.statusDegraded);
    expect(degradedLabels.length).toBeGreaterThanOrEqual(2); // 整體 + 該元件各一次
  });

  it("非 200 回應 → 降級為 unknown 態，畫面不炸", async () => {
    stubFetch(() => jsonResponse({}, false));
    render(<StatusPage />);
    await screen.findByText(COPY.footer.statusUnknown);
    expect(screen.getByText(COPY.status.empty)).toBeInTheDocument();
    expect(screen.getByText(COPY.status.loadFailedNote)).toBeInTheDocument();
  });

  it("fetch 拋出例外 → 降級為 unknown 態，畫面不炸", async () => {
    stubFetchReject(new Error("network down"));
    render(<StatusPage />);
    await screen.findByText(COPY.footer.statusUnknown);
    await waitFor(() => {
      expect(screen.getByText(COPY.status.empty)).toBeInTheDocument();
    });
  });

  it("初始渲染（fetch 尚未回應）即為 unknown 態，不留白畫面", () => {
    stubFetch(() => new Promise(() => {}) as unknown as Response); // 永不 resolve
    render(<StatusPage />);
    expect(screen.getByRole("heading", { level: 1, name: COPY.status.heading })).toBeInTheDocument();
    expect(screen.getByText(COPY.footer.statusUnknown)).toBeInTheDocument();
  });

  it("卡片列表化（Task 19 修正）：每個元件列有燈點＋mono 名稱＋pill 狀態", async () => {
    stubFetch(() => jsonResponse({
      status: "ok",
      components: [{ name: "engine", status: "ok" }],
      updated_at: 1724805063,
    }));
    const { container } = render(<StatusPage />);
    await screen.findAllByText(COPY.footer.statusOk);
    const row = container.querySelector(".status-component-row");
    expect(row).not.toBeNull();
    expect(row?.querySelector(".status-dot")).not.toBeNull();
    expect(row?.querySelector(".mono")?.textContent).toBe("engine");
    const pill = row?.querySelector(".pill.status-component-pill");
    expect(pill).not.toBeNull();
    expect(pill?.textContent).toBe(COPY.footer.statusOk);
    expect(container.querySelector(".status-overall .status-dot-lg")).not.toBeNull();
  });

  it("updated_at 格式為 YYYY-MM-DD HH:mm UTC", async () => {
    stubFetch(() => jsonResponse({
      status: "ok",
      components: [],
      updated_at: 1724805063,
    }));
    const { container } = render(<StatusPage />);
    await screen.findAllByText(COPY.footer.statusOk);
    const el = container.querySelector(".status-updated-at");
    expect(el?.textContent).toMatch(/2024-08-28 \d{2}:\d{2} UTC$/);
  });
});
