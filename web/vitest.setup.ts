import "@testing-library/jest-dom/vitest";
import { beforeEach, vi } from "vitest";

// 測試全離線：每個測試開始前，把全域 fetch 換成會炸的 stub。
// 需要 fetch 的測試必須自己 vi.stubGlobal("fetch", ...) 換成受控 mock。
beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => {
      throw new Error("測試禁止真實網路呼叫——請先 mock fetch");
    }),
  );
});
