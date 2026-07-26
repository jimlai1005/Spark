import { describe, expect, it, vi } from "vitest";
import { ApiError, type LeaderPreviewResp } from "./api";
import {
  runCustomLeaderPreview,
  validateCustomLeaderInput,
  type CustomLeaderPreviewDeps,
} from "./customLeader";

const ADDR = "0x2222222222222222222222222222222222222222";
const PREVIEW: LeaderPreviewResp = {
  address: ADDR,
  exists: true,
  account_value: "5123.45",
  position_count: 3,
  already_listed: false,
};

function deps(over: Partial<CustomLeaderPreviewDeps> = {}): CustomLeaderPreviewDeps {
  return { fetchPreview: vi.fn(async () => PREVIEW), ...over };
}

describe("validateCustomLeaderInput — 即時格式驗證（與後端准入同一基準：0x＋40 hex）", () => {
  it("合法位址 → ok，且小寫正規化（後端以小寫為單一位址基準）", () => {
    const r = validateCustomLeaderInput("0xAbC0000000000000000000000000000000000001");
    expect(r).toEqual({ ok: true, address: "0xabc0000000000000000000000000000000000001" });
  });

  it("前後空白剪掉（貼上位址常帶空白，不因此誤判格式錯）", () => {
    const r = validateCustomLeaderInput(`  ${ADDR}\n`);
    expect(r).toEqual({ ok: true, address: ADDR });
  });

  it("格式錯（長度不足／非 hex／缺 0x）→ 不 ok，且非空", () => {
    for (const bad of ["0x1234", "2222222222222222222222222222222222222222",
      "0xZZ22222222222222222222222222222222222222", "leader.eth"]) {
      expect(validateCustomLeaderInput(bad)).toEqual({ ok: false, empty: false });
    }
  });

  it("空字串／純空白 → 不 ok 且標記為空（顯示層據此不畫錯誤訊息）", () => {
    expect(validateCustomLeaderInput("")).toEqual({ ok: false, empty: true });
    expect(validateCustomLeaderInput("   ")).toEqual({ ok: false, empty: true });
  });
});

describe("runCustomLeaderPreview — 准入預覽編排（依賴注入、離線）", () => {
  it("happy path：以小寫正規化位址查詢，回傳預覽", async () => {
    const d = deps();
    const r = await runCustomLeaderPreview(d, `  ${ADDR.toUpperCase().replace("0X", "0x")} `);
    expect(r).toEqual({ ok: true, preview: PREVIEW });
    // ⭐ 送後端的是正規化後的小寫位址（與後端單一位址基準同源）
    expect(d.fetchPreview).toHaveBeenCalledWith(ADDR);
  });

  it("⭐ 格式不合法 → 本地擋下（reason=invalid_format），零網路請求", async () => {
    const d = deps();
    const r = await runCustomLeaderPreview(d, "0x1234");
    expect(r).toEqual({ ok: false, kind: "rejected", reason: "invalid_format" });
    expect(d.fetchPreview).not.toHaveBeenCalled();
  });

  it.each(["self_follow", "not_found", "leader_disabled"] as const)(
    "後端 4xx reason=%s → rejected 帶同一分類碼與後端人話 detail",
    async (reason) => {
      const d = deps({
        fetchPreview: vi.fn(async () => {
          throw new ApiError("client", "人話說明", 400, "人話說明", reason);
        }),
      });
      const r = await runCustomLeaderPreview(d, ADDR);
      expect(r).toEqual({ ok: false, kind: "rejected", reason, detail: "人話說明" });
    },
  );

  it("後端也可能回 invalid_format（前端驗證被繞過時）→ 同樣歸 rejected", async () => {
    const d = deps({
      fetchPreview: vi.fn(async () => {
        throw new ApiError("client", "格式不正確", 400, "格式不正確", "invalid_format");
      }),
    });
    const r = await runCustomLeaderPreview(d, ADDR);
    expect(r).toEqual({
      ok: false, kind: "rejected", reason: "invalid_format", detail: "格式不正確",
    });
  });

  it("⭐ 未知的 reason 分類碼 → 歸 failed，不硬塞進四種已知碼（fail closed）", async () => {
    const err = new ApiError("client", "新分類", 400, "新分類", "brand_new_reason");
    const d = deps({ fetchPreview: vi.fn(async () => { throw err; }) });
    const r = await runCustomLeaderPreview(d, ADDR);
    expect(r).toEqual({ ok: false, kind: "failed", error: err });
  });

  it("無 reason 的 ApiError（503 upstream／網路錯）→ failed，錯誤原樣上呈", async () => {
    const err = new ApiError("upstream", "上游服務暫時不可用", 503);
    const d = deps({ fetchPreview: vi.fn(async () => { throw err; }) });
    const r = await runCustomLeaderPreview(d, ADDR);
    expect(r).toEqual({ ok: false, kind: "failed", error: err });
  });

  it("非 ApiError 的例外 → failed（不吞掉、不重試——唯讀查詢由使用者按鈕重跑）", async () => {
    const err = new Error("boom");
    const d = deps({ fetchPreview: vi.fn(async () => { throw err; }) });
    const r = await runCustomLeaderPreview(d, ADDR);
    expect(r).toEqual({ ok: false, kind: "failed", error: err });
    expect(d.fetchPreview).toHaveBeenCalledTimes(1);
  });

  it("⭐ 伺服器回聲的位址 ≠ 送出的位址 → address-mismatch（預覽卡不得畫別人的資料）", async () => {
    const d = deps({
      fetchPreview: vi.fn(async () => ({
        ...PREVIEW, address: "0x9999999999999999999999999999999999999999",
      })),
    });
    const r = await runCustomLeaderPreview(d, ADDR);
    expect(r).toEqual({ ok: false, kind: "address-mismatch" });
  });

  it("回聲位址大小寫不同但實為同一位址 → 正常通過（大小寫不敏感，不得誤擋）", async () => {
    const echoed = { ...PREVIEW, address: ADDR.toUpperCase().replace("0X", "0x") };
    const d = deps({ fetchPreview: vi.fn(async () => echoed) });
    const r = await runCustomLeaderPreview(d, ADDR);
    expect(r).toEqual({ ok: true, preview: echoed });
  });
});
