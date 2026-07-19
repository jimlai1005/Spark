import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  type BillingPlansResp,
  type LeaderSelectMessageResp,
  type LeadersResp,
} from "@/lib/api";

const ME = { address: "0xAbC0000000000000000000000000000000000001", account_id: "fabc" };

const getMe = vi.fn();
const getLeaders = vi.fn();
const getBillingPlans = vi.fn();
const getLeaderSelectMessage = vi.fn();
const postLeaderSelect = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getMe: (...a: unknown[]) => getMe(...a),
  getLeaders: (...a: unknown[]) => getLeaders(...a),
  getBillingPlans: (...a: unknown[]) => getBillingPlans(...a),
  getLeaderSelectMessage: (...a: unknown[]) => getLeaderSelectMessage(...a),
  postLeaderSelect: (...a: unknown[]) => postLeaderSelect(...a),
}));

const signMessageAsync = vi.fn();
vi.mock("wagmi", () => ({ useSignMessage: () => ({ signMessageAsync }) }));

const recoverPersonalSigner = vi.fn();
vi.mock("@/lib/sign", () => ({
  recoverPersonalSigner: (...a: unknown[]) => recoverPersonalSigner(...a),
}));

import LeadersPage from "./page";

function wrap(children: ReactNode, me: typeof ME | null = ME) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(["me"], me);
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

/** 後端 /api/leaders 的形狀：⭐ 只有規模與曝險欄位，沒有任何績效指標。 */
function leaders(over: Partial<LeadersResp> = {}): LeadersResp {
  return {
    leaders: [
      {
        address: "0x1111111111111111111111111111111111111111",
        name: "Alpha",
        description: "多幣種網格",
        account_value: "125000.5",
        total_ntl_pos: "340000",
        unrealized_pnl: "-1200.25",
        position_count: 4,
      },
    ],
    stats_available: true,
    stats_day: "2026-07-18",
    stats_as_of: "2026-07-18T00:10:03+00:00",
    note: null,
    ...over,
  } as LeadersResp;
}

/** 方案目錄。`shipped` 決定本頁能否送出——前端不硬編。 */
function catalog(multiLeaderShipped: boolean): BillingPlansResp {
  return {
    billing_enabled: true,
    plans: [
      {
        id: "pro",
        name_key: "plans.pro.name",
        price_display: "USD 29 / 月",
        purchasable: true,
        features: [
          { text_key: "plans.feature.copytrade", included: true, shipped: true },
          { text_key: "plans.feature.multiLeader", included: true, shipped: multiLeaderShipped },
        ],
      },
    ],
  };
}

/**
 * ⭐ 原文必須含 `Leader: <位址>` 那一行——這不是裝飾，是伺服器版型的一部分
 * （filet/leader_change.py 的 `_message`，位址正規化為小寫），也是前端 leader 預驗
 * 的第二道比對對象。fixture 若省略它，測試就驗不到真實流程會走的那條路徑。
 */
const MSG: LeaderSelectMessageResp = {
  message:
    "Filet: change copy-trading leader\n\nAccount: fabc\n" +
    "Leader: 0x1111111111111111111111111111111111111111\nNonce: n-1",
  nonce: "n-1",
  issued_at: "2026-07-19T00:00:00Z",
  leader_address: "0x1111111111111111111111111111111111111111",
  account_id: "fabc",
};
const SIG = `0x${"ab".repeat(65)}`;

beforeEach(() => {
  vi.clearAllMocks();
  getMe.mockResolvedValue(ME);
  getLeaders.mockResolvedValue(leaders());
  getBillingPlans.mockResolvedValue(catalog(false));
  getLeaderSelectMessage.mockResolvedValue(MSG);
  postLeaderSelect.mockResolvedValue({
    ok: true, account_id: "fabc", leader_address: MSG.leader_address,
    effective: "next_engine_cycle",
    effective_note: "已記錄，於引擎的下一個 cycle 生效——不是立即生效。",
    consequences: "生效時引擎會把你的部位收斂到新 leader：平掉目前的部位、依新 leader 開新部位。",
  });
  signMessageAsync.mockResolvedValue(SIG);
  recoverPersonalSigner.mockResolvedValue(ME.address.toLowerCase());
});

/** 開啟確認對話框（閘門必須先是開的）。 */
async function openConfirm() {
  const btn = await screen.findByRole("button", { name: "選擇此 leader" });
  await userEvent.click(btn);
  return screen.getByRole("dialog");
}

describe("LeadersPage — 誠信揭露 ⭐（本頁最重要的部分）", () => {
  it("誠信 1／2：明講數字是規模與曝險而非績效；統計欄位只有後端快照原名的四項", async () => {
    render(wrap(<LeadersPage />));

    expect(await screen.findByText(/以下數字是規模與當下曝險的快照，不是績效/)).toBeInTheDocument();
    expect(screen.getByText(/沒有報酬率、沒有回撤、沒有勝率/)).toBeInTheDocument();

    // ⭐ 釘死欄位清單：任何被改名成像績效的欄位、或前端自行算出的比率都會讓這條失敗。
    const labels = Array.from(document.querySelectorAll(".leader-stat dt")).map((n) => n.textContent);
    expect(labels).toEqual(["帳戶淨值", "名目部位總額", "未實現損益", "持倉數"]);
  });

  it("⭐ 誠信 3：stats_available=false → 顯示後端 note，該區塊零數字，全頁不畫任何統計", async () => {
    getLeaders.mockResolvedValue(
      leaders({
        stats_available: false, stats_day: null, stats_as_of: null,
        note: "績效統計暫時不可用（每日快照尚未產生或讀取失敗）；leader 清單不受影響。",
        leaders: [{
          address: "0x1111111111111111111111111111111111111111",
          name: "Alpha", description: "多幣種網格",
          account_value: null, total_ntl_pos: null, unrealized_pnl: null, position_count: null,
        }],
      }),
    );
    render(wrap(<LeadersPage />));

    const notice = await screen.findByText(/每日快照尚未產生或讀取失敗/);
    const block = notice.closest(".leader-stats-notice")!;
    // 沿 /ops basis_unknown 的嚴格度：整段不得出現任何數字
    expect(block.textContent).not.toMatch(/\d/);
    // 統計欄位一個都不畫（畫成「—」會被讀成「有查到且為零／無部位」）
    expect(document.querySelectorAll(".leader-stat")).toHaveLength(0);
    expect(screen.queryByText("帳戶淨值")).not.toBeInTheDocument();
    // 清單本身照樣可用
    expect(screen.getByText("Alpha")).toBeInTheDocument();
  });

  it("誠信 4：stats_day 與 stats_as_of 必須顯示（沒有時點的數字會被當成即時數字讀）", async () => {
    render(wrap(<LeadersPage />));
    const meta = await screen.findByText(/2026-07-18T00:10:03/);
    expect(meta.textContent).toContain("2026-07-18");
    expect(meta.textContent).toContain("快照日期");
    expect(meta.textContent).toContain("統計時點");
    expect(screen.getByText(/不是即時值/)).toBeInTheDocument();
  });

  it("⭐ 誠信 4 強化：快照可用但兩個時間戳皆缺 → 一樣不畫任何數字", async () => {
    getLeaders.mockResolvedValue(leaders({ stats_day: null, stats_as_of: null }));
    render(wrap(<LeadersPage />));

    expect(await screen.findByText(/統計缺少時點，本頁不顯示任何數字/)).toBeInTheDocument();
    expect(document.querySelectorAll(".leader-stat")).toHaveLength(0);
  });

  it("⭐ 誠信 5：上界警語與選擇按鈕在同一個容器（不得被推去頁尾當小字）", async () => {
    getBillingPlans.mockResolvedValue(catalog(true));
    render(wrap(<LeadersPage />));

    const btn = await screen.findByRole("button", { name: "選擇此 leader" });
    const action = btn.closest(".leader-action")!;
    expect(action.textContent).toMatch(/實際結果會低於 leader 的數字/);
    expect(action.textContent).toMatch(/上界，不是你的期望值/);
  });

  it("⭐ 誠信 6：確認對話框寫明真實成本與「下一個 cycle 生效」，且開啟前零 API 呼叫", async () => {
    getBillingPlans.mockResolvedValue(catalog(true));
    render(wrap(<LeadersPage />));
    const dialog = await openConfirm();

    // 成本：平舊開新 ＋ 真實交易成本
    expect(dialog.textContent).toMatch(/平掉目前的部位、依新 leader 開新部位/);
    expect(dialog.textContent).toMatch(/實際的交易成本/);
    // 生效時機：下一個 cycle，不是立即
    expect(dialog.textContent).toMatch(/不是立即生效/);
    expect(dialog.textContent).toMatch(/下一個 cycle 生效/);
    // 按下確認之前不得有任何請求（也不得叫錢包）
    expect(getLeaderSelectMessage).not.toHaveBeenCalled();
    expect(signMessageAsync).not.toHaveBeenCalled();
    expect(postLeaderSelect).not.toHaveBeenCalled();
  });

  it("取消確認 → 對話框關閉，零請求、零簽章", async () => {
    getBillingPlans.mockResolvedValue(catalog(true));
    render(wrap(<LeadersPage />));
    await openConfirm();

    await userEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(getLeaderSelectMessage).not.toHaveBeenCalled();
    expect(signMessageAsync).not.toHaveBeenCalled();
  });

  it("「目前跟隨中」沒有資料來源 → 明說缺口，不猜、不標示", async () => {
    render(wrap(<LeadersPage />));
    expect(await screen.findByText(/本頁無法標示你目前跟隨中的 leader/)).toBeInTheDocument();
  });
});

describe("LeadersPage — 付費功能閘門（由後端 shipped 驅動）", () => {
  it("⭐ multiLeader shipped=false → 標「開發中」且**無法送出**（按鈕停用、零請求）", async () => {
    render(wrap(<LeadersPage />));

    expect(await screen.findByText(/換 leader 尚未推出/)).toBeInTheDocument();
    const btn = screen.getByRole("button", { name: "開發中，敬請期待" });
    expect(btn).toBeDisabled();
    expect(screen.queryByRole("button", { name: "選擇此 leader" })).not.toBeInTheDocument();

    await userEvent.click(btn);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(getLeaderSelectMessage).not.toHaveBeenCalled();
    expect(postLeaderSelect).not.toHaveBeenCalled();
  });

  it("⭐ 方案目錄載入失敗 → fail closed：不確定就不開放送出", async () => {
    getBillingPlans.mockRejectedValue(new ApiError("network", "無法連線到伺服器"));
    render(wrap(<LeadersPage />));

    expect(await screen.findByText(/無法確認這個功能是否已推出/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "開發中，敬請期待" })).toBeDisabled();
    expect(getLeaderSelectMessage).not.toHaveBeenCalled();
  });

  it("shipped=true → 開放送出（前端不硬編，只跟著後端旗標走）", async () => {
    getBillingPlans.mockResolvedValue(catalog(true));
    render(wrap(<LeadersPage />));

    expect(await screen.findByRole("button", { name: "選擇此 leader" })).toBeEnabled();
    expect(screen.queryByText(/換 leader 尚未推出/)).not.toBeInTheDocument();
  });
});

describe("LeadersPage — 簽章授權流程（沿 approvalFlow 的謹慎度）", () => {
  beforeEach(() => getBillingPlans.mockResolvedValue(catalog(true)));

  it("確認 → 取原文 → 簽原文 → recover 相符 → 原文原樣回送；成功顯示後端生效說明", async () => {
    render(wrap(<LeadersPage />));
    await openConfirm();
    await userEvent.click(screen.getByRole("button", { name: "確認並簽署" }));

    expect(await screen.findByText(/已授權，於引擎的下一個 cycle 生效/)).toBeInTheDocument();
    expect(getLeaderSelectMessage).toHaveBeenCalledWith("0x1111111111111111111111111111111111111111");
    // ⭐ 簽的是伺服器原文；送出的是同一包 payload（前端不重組字串）
    expect(signMessageAsync).toHaveBeenCalledWith({ message: MSG.message });
    expect(postLeaderSelect).toHaveBeenCalledWith(MSG, SIG);
    // 生效時機與後果顯示後端原文（單一來源）
    expect(screen.getByText(/不是立即生效/)).toBeInTheDocument();
  });

  it("⭐ recover 出的簽章者 ≠ 登入地址 → 完全不送出（postLeaderSelect 零呼叫）", async () => {
    recoverPersonalSigner.mockResolvedValue("0x9999999999999999999999999999999999999999");
    render(wrap(<LeadersPage />));
    await openConfirm();
    await userEvent.click(screen.getByRole("button", { name: "確認並簽署" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("簽名帳號與登入帳號不符");
    expect(postLeaderSelect).not.toHaveBeenCalled();
  });

  it("錢包取消 → 明說沒有送出、跟單設定沒有變動", async () => {
    signMessageAsync.mockRejectedValue(new Error("User rejected the request"));
    render(wrap(<LeadersPage />));
    await openConfirm();
    await userEvent.click(screen.getByRole("button", { name: "確認並簽署" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("沒有送出");
    expect(postLeaderSelect).not.toHaveBeenCalled();
  });

  it("取原文 400（leader 不可選）→ 顯示不可選文案，不叫錢包", async () => {
    getLeaderSelectMessage.mockRejectedValue(
      new ApiError("client", "該 leader 目前不可選擇", 400, "該 leader 目前不可選擇"),
    );
    render(wrap(<LeadersPage />));
    await openConfirm();
    await userEvent.click(screen.getByRole("button", { name: "確認並簽署" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("目前不可選擇");
    expect(signMessageAsync).not.toHaveBeenCalled();
  });

  it("送出 500（寫檔失敗）→ 明說 leader 沒有被變更，且不自動重試", async () => {
    postLeaderSelect.mockRejectedValue(
      new ApiError("client", "變更記錄寫入失敗", 500, "變更記錄寫入失敗"),
    );
    render(wrap(<LeadersPage />));
    await openConfirm();
    await userEvent.click(screen.getByRole("button", { name: "確認並簽署" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("你的 leader 沒有被變更");
    expect(postLeaderSelect).toHaveBeenCalledTimes(1);
  });

  /**
   * ⭐ 被打穿的 filet-api 唯一能無中生有一次換手的路徑：使用者點 Alpha，API 回一份
   * 指向別人的待簽原文。錢包只顯示一串 hex，使用者按下簽署後，那份簽章對後端而言
   * 完全合法——所以攔截必須發生在**喚起錢包之前**。
   */
  it("⭐ Critical：API 回傳的 leader_address ≠ 使用者所選 → 不喚起錢包、不送出", async () => {
    const EVIL = "0xEEEE000000000000000000000000000000000EEE";
    getLeaderSelectMessage.mockResolvedValue({
      ...MSG,
      leader_address: EVIL,
      message: MSG.message.replace(MSG.leader_address, EVIL),
    });
    render(wrap(<LeadersPage />));
    await openConfirm();
    await userEvent.click(screen.getByRole("button", { name: "確認並簽署" }));

    // 使用者連一次簽名請求都不該看到（簽了就已經是一份有效授權）
    expect(await screen.findByRole("alert")).toHaveTextContent("授權對象與你選擇的 leader 不符");
    expect(signMessageAsync).not.toHaveBeenCalled();
    expect(postLeaderSelect).not.toHaveBeenCalled();
  });

  it("⭐ 第二道：leader_address 相符但 message 內含的位址不同 → 同樣中止", async () => {
    const EVIL = "0xEEEE000000000000000000000000000000000EEE";
    getLeaderSelectMessage.mockResolvedValue({
      ...MSG,
      message: MSG.message.replace(MSG.leader_address, EVIL),
    });
    render(wrap(<LeadersPage />));
    await openConfirm();
    await userEvent.click(screen.getByRole("button", { name: "確認並簽署" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("授權對象與你選擇的 leader 不符");
    expect(signMessageAsync).not.toHaveBeenCalled();
    expect(postLeaderSelect).not.toHaveBeenCalled();
  });

  it("⭐ leader 不符的文案要求使用者停手回報，且不提供「重新操作」按鈕", async () => {
    getLeaderSelectMessage.mockResolvedValue({
      ...MSG,
      leader_address: "0xEEEE000000000000000000000000000000000EEE",
    });
    render(wrap(<LeadersPage />));
    await openConfirm();
    await userEvent.click(screen.getByRole("button", { name: "確認並簽署" }));

    const alert = await screen.findByRole("alert");
    // 明說現況：沒簽、沒送出、設定沒變
    expect(alert).toHaveTextContent("沒有被簽署");
    expect(alert).toHaveTextContent("沒有變動");
    // ⭐ 明確叫使用者不要重試並回報——不得出現誘導重試的字眼
    expect(alert).toHaveTextContent("請不要重試");
    expect(alert).toHaveTextContent("回報客服");
    expect(alert.textContent).not.toMatch(/請稍後再試|重新整理/);
    // 按鈕是純關閉，不是「重新操作」（否則等於用按鈕收回文案的結論）
    expect(screen.getByRole("button", { name: "關閉" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重新操作" })).not.toBeInTheDocument();
  });

  it("大小寫不同但實為同一位址 → 不得誤擋，流程正常完成", async () => {
    getLeaderSelectMessage.mockResolvedValue({
      ...MSG,
      leader_address: MSG.leader_address.toUpperCase(),
    });
    render(wrap(<LeadersPage />));
    await openConfirm();
    await userEvent.click(screen.getByRole("button", { name: "確認並簽署" }));

    expect(await screen.findByText(/已授權，於引擎的下一個 cycle 生效/)).toBeInTheDocument();
    expect(signMessageAsync).toHaveBeenCalledTimes(1);
    expect(postLeaderSelect).toHaveBeenCalledTimes(1);
  });

  it("送出 403（改別人的帳號）→ 顯示對應文案", async () => {
    postLeaderSelect.mockRejectedValue(
      new ApiError("client", "只能變更自己帳號的 leader", 403, "只能變更自己帳號的 leader"),
    );
    render(wrap(<LeadersPage />));
    await openConfirm();
    await userEvent.click(screen.getByRole("button", { name: "確認並簽署" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("只能變更自己帳號的 leader");
  });
});

describe("LeadersPage — 其他狀態", () => {
  it("未登入 → 顯示未登入文案，不打清單端點", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    render(wrap(<LeadersPage />, null));
    expect(await screen.findByText(/尚未登入/)).toBeInTheDocument();
    expect(getLeaders).not.toHaveBeenCalled();
  });

  it("清單載入失敗 → 顯示載入失敗文案而非空白頁", async () => {
    getLeaders.mockRejectedValue(new ApiError("upstream", "leader 名單暫時不可用", 503));
    render(wrap(<LeadersPage />));
    expect(await screen.findByText("載入 leader 清單失敗，請重新整理本頁。")).toBeInTheDocument();
  });

  it("清單為空 → 顯示空狀態（不是壞掉的畫面）", async () => {
    getLeaders.mockResolvedValue(leaders({ leaders: [] }));
    render(wrap(<LeadersPage />));
    expect(await screen.findByText("目前沒有可選擇的 leader。")).toBeInTheDocument();
  });

  it("leader 不在快照中（各欄 null）→ 說明沒有數字，不畫一排「—」", async () => {
    getLeaders.mockResolvedValue(
      leaders({
        leaders: [{
          address: "0x2222222222222222222222222222222222222222",
          name: "Beta", description: "測試",
          account_value: null, total_ntl_pos: null, unrealized_pnl: null, position_count: null,
        }],
      }),
    );
    render(wrap(<LeadersPage />));

    expect(await screen.findByText(/這位 leader 不在最新快照中/)).toBeInTheDocument();
    expect(document.querySelectorAll(".leader-stat")).toHaveLength(0);
  });
});
