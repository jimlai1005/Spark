import { cleanup } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, vi } from "vitest";

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

// vitest.config.ts 未啟用 test.globals，RTL 的自動 afterEach cleanup 偵測不到
// 全域 afterEach，需手動註冊，否則同一測試檔多次 render() 會殘留 DOM 節點。
afterEach(() => {
  cleanup();
});
