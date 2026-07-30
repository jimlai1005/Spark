import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  type LeaderSelectMessageResp,
  type LeadersResp,
  type MyLeaderResp,
  type MyRiskResp,
  type RiskPrefs,
  type RiskSettingsMessageResp,
} from "@/lib/api";

const ME = { address: "0xAbC0000000000000000000000000000000000001", account_id: "fabc" };

const getMe = vi.fn();
const getLeaders = vi.fn();
const getLeaderSelectMessage = vi.fn();
const postLeaderSelect = vi.fn();
const getLeaderPreview = vi.fn();
const getMyLeader = vi.fn();
const getMyRisk = vi.fn();
const getRiskSettingsMessage = vi.fn();
const postMyRisk = vi.fn();
const getRiskUnlockMessage = vi.fn();
const postRiskUnlock = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getMe: (...a: unknown[]) => getMe(...a),
  getLeaders: (...a: unknown[]) => getLeaders(...a),
  getLeaderSelectMessage: (...a: unknown[]) => getLeaderSelectMessage(...a),
  postLeaderSelect: (...a: unknown[]) => postLeaderSelect(...a),
  getLeaderPreview: (...a: unknown[]) => getLeaderPreview(...a),
  getMyLeader: (...a: unknown[]) => getMyLeader(...a),
  getMyRisk: (...a: unknown[]) => getMyRisk(...a),
  getRiskSettingsMessage: (...a: unknown[]) => getRiskSettingsMessage(...a),
  postMyRisk: (...a: unknown[]) => postMyRisk(...a),
  getRiskUnlockMessage: (...a: unknown[]) => getRiskUnlockMessage(...a),
  postRiskUnlock: (...a: unknown[]) => postRiskUnlock(...a),
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

/**
 * 後端 /api/leaders 的形狀。⭐ 2026-07-30 起本頁不再把這份目錄畫成卡片格線——
 * 只用來判定貼上的位址是不是平台已知的 leader（見 page.tsx listedEntryOf）。
 */
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

/** 這是平台背景目錄裡已知的位址（Alpha）；用於「已在精選清單」的測試。 */
const LISTED_ADDR = "0x1111111111111111111111111111111111111111";

const CUSTOM_ADDR = "0x2222222222222222222222222222222222222222";
const CUSTOM_PREVIEW = {
  address: CUSTOM_ADDR, exists: true,
  account_value: "5123.45", position_count: 3, already_listed: false,
  accepting_new: true,
};
/**
 * ⭐ 原文必須含 `Leader: <位址>` 那一行——這不是裝飾，是伺服器版型的一部分
 * （filet/leader_change.py 的 `_message`，位址正規化為小寫），也是前端 leader 預驗
 * 的第二道比對對象。fixture 若省略它，測試就驗不到真實流程會走的那條路徑。
 */
const CUSTOM_MSG: LeaderSelectMessageResp = {
  message:
    "Filet: change copy-trading leader\n\nAccount: fabc\n"
    + `Leader: ${CUSTOM_ADDR}\nNonce: n-2`,
  nonce: "n-2",
  issued_at: "2026-07-27T00:00:00Z",
  leader_address: CUSTOM_ADDR,
  account_id: "fabc",
};
const SIG = `0x${"ab".repeat(65)}`;

/**
 * `/api/me/leader` 的線上形狀（app.py `me_leader`）。四種 `status` 語意不同，
 * 且 `leader_address` 為 null 時**只能靠 status** 分辨「還沒活化」與「用引擎預設」。
 */
function myLeader(over: Partial<MyLeaderResp> = {}): MyLeaderResp {
  return {
    account_id: "fabc",
    status: "following",
    leader_address: "0x1111111111111111111111111111111111111111",
    leader_name: "Alpha",
    pending_change: null,
    note: "這是引擎目前為你跟隨的 leader。",
    ...over,
  } as MyLeaderResp;
}

/** 把位址貼進地址 dock 的輸入框（paste 而非逐鍵，42 字元逐鍵太慢）。 */
async function pasteAddress(addr: string) {
  const input = await screen.findByLabelText(/leader 錢包位址/);
  await userEvent.click(input);
  await userEvent.paste(addr);
  return input;
}

/** 查詢 → 預覽卡出現。 */
async function previewCustom(addr = CUSTOM_ADDR) {
  await pasteAddress(addr);
  await userEvent.click(screen.getByRole("button", { name: "查詢" }));
  await screen.findByText("鏈上預覽");
}

/** 預覽 → 勾選聲明 → 開啟確認框（本頁唯一的跟單入口，取代舊版「選擇此 leader」）。 */
async function openConfirmViaDock(addr = CUSTOM_ADDR) {
  await previewCustom(addr);
  await userEvent.click(screen.getByRole("checkbox", { name: /未審核 leader/ }));
  await userEvent.click(screen.getByRole("button", { name: "跟單此地址" }));
  return screen.getByRole("dialog");
}

/**
 * 後端 /api/me/risk 的形狀。⭐ `specs` 由後端供給——本 fixture 刻意照抄
 * src/spark/filet/risk_prefs.py 的實際區間，好讓「前端不硬編數字」這件事在
 * 測試裡也成立（改了後端區間，這裡跟著改，前端 code 不必動）。
 */
const RISK_PREFS: RiskPrefs = {
  enabled: false, size_tolerance: "0.08", max_drawdown_pct: "0.2",
  max_total_drawdown_pct: "0.4", flatten_on_breach: true, cooldown_hours: "12",
};

function myRisk(over: Partial<MyRiskResp> = {}): MyRiskResp {
  return {
    prefs: { ...RISK_PREFS },
    defaults: { ...RISK_PREFS },
    specs: [
      // ⭐ group="tracking"：不受風控開關影響，UI 必須畫在 checkbox 之外。
      { name: "size_tolerance", env: "COPY_SIZE_TOLERANCE", type: "decimal",
        group: "tracking", default: "0.08", recommended: "0.08",
        min: "0.02", max: "0.25",
        label: "與 leader 的部位差異容忍度", help: "調小＝跟得更緊，成本上升。" },
      { name: "max_drawdown_pct", env: "COPY_MAX_DRAWDOWN_PCT", type: "decimal",
        group: "risk", default: "0.2", recommended: "0.2", min: "0.05", max: "0.5",
        label: "7 天滾動回撤上限", help: "跌幅超過此值即熔斷。" },
      { name: "max_total_drawdown_pct", env: "COPY_MAX_TOTAL_DRAWDOWN_PCT",
        type: "decimal", group: "risk", default: "0.4", recommended: "0.4",
        min: "0", max: "0.8",
        label: "累計回撤上限", help: "0 ＝ 停用這一道。" },
      { name: "flatten_on_breach", env: "COPY_FLATTEN_ON_BREACH", type: "bool",
        group: "risk", default: true, recommended: true, min: null, max: null,
        label: "熔斷時自動平倉", help: "關：只停止交易並告警。" },
      // ⭐ unit="hours"：不做百分比換算，刻度是整數小時。
      { name: "cooldown_hours", env: "COPY_RISK_COOLDOWN_HOURS", type: "decimal",
        group: "risk", unit: "hours", default: "12", recommended: "12",
        min: "0", max: "168",
        label: "熔斷後的冷靜期（小時）", help: "設 0 ＝ 不自動恢復。" },
    ],
    submitted: { issued_at: "2026-07-30T00:00:00Z" },
    applied: {
      controls_enabled: false, source: "signed_settings",
      changed_at: "2026-07-30T00:01:00Z",
      // 引擎實際在執法的門檻＝預設值（與 prefs 一致 ⇒ 「已生效」）
      prefs: {
        enabled: false, size_tolerance: "0.08", max_drawdown_pct: "0.2",
        max_total_drawdown_pct: "0.4", flatten_on_breach: true,
        cooldown_hours: "12",
      },
    },
    halted: {
      tripped: false, reason: null, tripped_at: null, resumable: null,
      residual_exposure: null, cooldown_hours: null, resume_at: null,
    },
    editable: true,
    ...over,
  };
}

/**
 * 伺服器產生的風控待簽原文。⭐ 版型照抄後端 `build_risk_settings_message`
 * （filet/risk_settings.py）：每個參數各佔一行、帶單位的附單位詞、總開關那一行的
 * 標籤是 `Risk Controls`。前端的內容預驗會逐行比對它——fixture 若簡化掉這個形狀，
 * 測試驗到的就不是真實流程會走的那條路徑。
 */
function riskMessageFor(prefs: RiskPrefs): string {
  return [
    "Filet: update copy-trading risk settings", "",
    `Account: ${ME.account_id}`,
    `Risk Controls: ${prefs.enabled ? "enabled" : "disabled"}`,
    `size_tolerance: ${prefs.size_tolerance}`,
    `max_drawdown_pct: ${prefs.max_drawdown_pct}`,
    `max_total_drawdown_pct: ${prefs.max_total_drawdown_pct}`,
    `flatten_on_breach: ${prefs.flatten_on_breach}`,
    `cooldown_hours: ${prefs.cooldown_hours} hours`,
    "Nonce: n-risk", "Issued At: 2026-07-30T02:00:00Z",
  ].join("\n");
}

const UNLOCK_MSG = [
  "Filet: resume copy-trading after a risk halt", "",
  `Account: ${ME.account_id}`, "Nonce: n-unlock", "Issued At: 2026-07-30T03:00:00Z",
].join("\n");

/** 熔斷中（非治理性）的 fixture——`halted.tripped=true` 才會出現恢復按鈕。 */
function halted(over: Partial<NonNullable<MyRiskResp["halted"]>> = {}) {
  return myRisk({
    halted: {
      tripped: true, reason: "max_drawdown_pct", resumable: true,
      residual_exposure: false,
      tripped_at: "2026-07-30T04:00:00Z", cooldown_hours: "12",
      resume_at: "2026-07-30T16:00:00Z", ...over,
    },
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  getMe.mockResolvedValue(ME);
  getLeaders.mockResolvedValue(leaders());
  getMyLeader.mockResolvedValue(myLeader());
  getMyRisk.mockResolvedValue(myRisk());
  // 伺服器就客戶送來的偏好產生 canonical 原文並回聲——mock 必須是動態的，否則
  // 前端的內容預驗（回聲要等於我送出的那一組）會在每一個測試裡把流程擋下來。
  getRiskSettingsMessage.mockImplementation(async (prefs: RiskPrefs) => ({
    message: riskMessageFor(prefs), nonce: "n-risk",
    issued_at: "2026-07-30T02:00:00Z", account_id: ME.account_id, prefs,
  }));
  postMyRisk.mockImplementation(async (payload: RiskSettingsMessageResp) => ({
    ok: true, prefs: payload.prefs,
    effective_note: "引擎會在下一輪（約一分鐘內）套用這份設定。",
  }));
  getRiskUnlockMessage.mockResolvedValue({
    message: UNLOCK_MSG, nonce: "n-unlock",
    issued_at: "2026-07-30T03:00:00Z", account_id: ME.account_id,
  });
  postRiskUnlock.mockResolvedValue({
    ok: true, effective_note: "已解除，引擎會在下一輪恢復跟單。",
  });
  getLeaderPreview.mockResolvedValue(CUSTOM_PREVIEW);
  getLeaderSelectMessage.mockResolvedValue(CUSTOM_MSG);
  postLeaderSelect.mockResolvedValue({
    ok: true, account_id: "fabc", leader_address: CUSTOM_MSG.leader_address,
    effective: "next_engine_cycle",
    effective_note: "已記錄，於引擎的下一個 cycle 生效——不是立即生效。",
    consequences: "生效時引擎會把你的部位收斂到新 leader：平掉目前的部位、依新 leader 開新部位。",
  });
  signMessageAsync.mockResolvedValue(SIG);
  recoverPersonalSigner.mockResolvedValue(ME.address.toLowerCase());
});

/**
 * ⭐ 地址 dock：本頁唯一的跟單入口（2026-07-27 spec 的准入預覽流程；2026-07-30
 * 拿掉付費閘門與精選卡片格線後，這是全站唯一能觸發 runFlow／簽章管線的路徑，
 * 因此原本掛在「簽章授權流程」describe 下的安全性測試一併搬進來，改用本頁
 * 實際存在的觸發方式（previewCustom → 勾選聲明 → 跟單此地址）。
 */
describe("LeadersPage — 地址 dock（本頁唯一的跟單入口）⭐", () => {
  it("區塊存在：輸入框、查詢按鈕、HL 官方 leaderboard 外部連結（新分頁＋noopener）", async () => {
    render(wrap(<LeadersPage />));

    expect(await screen.findByText("跟單對象")).toBeInTheDocument();
    expect(screen.getByLabelText(/leader 錢包位址/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查詢" })).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /leaderboard/ });
    expect(link).toHaveAttribute("href", "https://app.hyperliquid.xyz/leaderboard");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
  });

  it("⭐ 格式錯誤即時回饋：查詢按鈕停用、零 API 呼叫", async () => {
    render(wrap(<LeadersPage />));
    await pasteAddress("0x1234");

    expect(await screen.findByText(/位址格式不正確/)).toBeInTheDocument();
    const btn = screen.getByRole("button", { name: "查詢" });
    expect(btn).toBeDisabled();
    await userEvent.click(btn);
    expect(getLeaderPreview).not.toHaveBeenCalled();
  });

  it("輸入為空 → 不顯示格式錯誤（「還沒輸入」與「輸入錯了」是兩種狀態）", async () => {
    render(wrap(<LeadersPage />));
    await screen.findByLabelText(/leader 錢包位址/);

    expect(screen.queryByText(/位址格式不正確/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查詢" })).toBeDisabled();
  });

  it("⭐ 查詢成功 → 預覽卡：帳戶權益、持倉數、位址縮寫", async () => {
    render(wrap(<LeadersPage />));
    await previewCustom();

    expect(getLeaderPreview).toHaveBeenCalledWith(CUSTOM_ADDR);
    const card = screen.getByText("鏈上預覽").closest(".leader-custom-preview")!;
    expect(card.textContent).toContain("帳戶權益");
    expect(card.textContent).toContain("5,123.45");
    expect(card.textContent).toContain("持倉數");
    expect(card.textContent).toContain("3");
    expect(card.textContent).toContain("0x2222…222"); // shortAddr
  });

  it("⭐ checkbox 未勾 → 不能送出（按鈕停用、零對話框、零請求）；勾選後開放", async () => {
    render(wrap(<LeadersPage />));
    await previewCustom();

    const select = screen.getByRole("button", { name: "跟單此地址" });
    expect(select).toBeDisabled();
    await userEvent.click(select);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(getLeaderSelectMessage).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("checkbox", { name: /未審核 leader/ }));
    expect(screen.getByRole("button", { name: "跟單此地址" })).toBeEnabled();
  });

  it("⭐ 勾選聲明 → 確認對話框 → 簽章流程走完（原文含位址、整包 payload 回送）", async () => {
    render(wrap(<LeadersPage />));
    const dialog = await openConfirmViaDock();

    expect(dialog.textContent).toContain("0x2222…222");
    expect(dialog.textContent).toMatch(/平掉目前的部位、依新 leader 開新部位/);
    await userEvent.click(screen.getByRole("button", { name: "確認並簽署" }));

    expect(await screen.findByText(/已授權，於引擎的下一個 cycle 生效/)).toBeInTheDocument();
    expect(getLeaderSelectMessage).toHaveBeenCalledWith(CUSTOM_ADDR);
    expect(signMessageAsync).toHaveBeenCalledWith({ message: CUSTOM_MSG.message });
    expect(postLeaderSelect).toHaveBeenCalledWith(CUSTOM_MSG, SIG);
  });

  it("取消確認 → 對話框關閉，零請求、零簽章", async () => {
    render(wrap(<LeadersPage />));
    await openConfirmViaDock();

    await userEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(getLeaderSelectMessage).not.toHaveBeenCalled();
    expect(signMessageAsync).not.toHaveBeenCalled();
  });

  it("⭐ Critical：API 回傳的 leader_address ≠ 使用者所選 → 不喚起錢包、不送出", async () => {
    const EVIL = "0xEEEE000000000000000000000000000000000EEE";
    getLeaderSelectMessage.mockResolvedValue({
      ...CUSTOM_MSG,
      leader_address: EVIL,
      message: CUSTOM_MSG.message.replace(CUSTOM_ADDR, EVIL),
    });
    render(wrap(<LeadersPage />));
    await openConfirmViaDock();
    await userEvent.click(screen.getByRole("button", { name: "確認並簽署" }));

    // 使用者連一次簽名請求都不該看到（簽了就已經是一份有效授權）
    expect(await screen.findByRole("alert")).toHaveTextContent("授權對象與你選擇的 leader 不符");
    expect(signMessageAsync).not.toHaveBeenCalled();
    expect(postLeaderSelect).not.toHaveBeenCalled();
  });

  it("⭐ 第二道：leader_address 相符但 message 內含的位址不同 → 同樣中止", async () => {
    const EVIL = "0xEEEE000000000000000000000000000000000EEE";
    getLeaderSelectMessage.mockResolvedValue({
      ...CUSTOM_MSG,
      message: CUSTOM_MSG.message.replace(CUSTOM_ADDR, EVIL),
    });
    render(wrap(<LeadersPage />));
    await openConfirmViaDock();
    await userEvent.click(screen.getByRole("button", { name: "確認並簽署" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("授權對象與你選擇的 leader 不符");
    expect(signMessageAsync).not.toHaveBeenCalled();
    expect(postLeaderSelect).not.toHaveBeenCalled();
  });

  it("⭐ leader 不符的文案要求使用者停手回報，且不提供「重新操作」按鈕", async () => {
    getLeaderSelectMessage.mockResolvedValue({
      ...CUSTOM_MSG,
      leader_address: "0xEEEE000000000000000000000000000000000EEE",
    });
    render(wrap(<LeadersPage />));
    await openConfirmViaDock();
    await userEvent.click(screen.getByRole("button", { name: "確認並簽署" }));

    const alert = await screen.findByRole("alert");
    // 明說現況：沒簽、沒送出、設定沒變
    expect(alert).toHaveTextContent("沒有被簽署");
    expect(alert).toHaveTextContent("沒有變動");
    expect(alert).toHaveTextContent("請不要重試");
    expect(alert).toHaveTextContent("回報客服");
    expect(alert.textContent).not.toMatch(/請稍後再試|重新整理/);
    expect(screen.getByRole("button", { name: "關閉" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重新操作" })).not.toBeInTheDocument();
  });

  it("⭐ recover 出的簽章者 ≠ 登入地址 → 完全不送出（postLeaderSelect 零呼叫）", async () => {
    recoverPersonalSigner.mockResolvedValue("0x9999999999999999999999999999999999999999");
    render(wrap(<LeadersPage />));
    await openConfirmViaDock();
    await userEvent.click(screen.getByRole("button", { name: "確認並簽署" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("簽名帳號與登入帳號不符");
    expect(postLeaderSelect).not.toHaveBeenCalled();
  });

  it("錢包取消 → 明說沒有送出、跟單設定沒有變動", async () => {
    signMessageAsync.mockRejectedValue(new Error("User rejected the request"));
    render(wrap(<LeadersPage />));
    await openConfirmViaDock();
    await userEvent.click(screen.getByRole("button", { name: "確認並簽署" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("沒有送出");
    expect(postLeaderSelect).not.toHaveBeenCalled();
  });

  it("取原文 400（leader 不可選）→ 顯示不可選文案，不叫錢包", async () => {
    getLeaderSelectMessage.mockRejectedValue(
      new ApiError("client", "該 leader 目前不可選擇", 400, "該 leader 目前不可選擇"),
    );
    render(wrap(<LeadersPage />));
    await openConfirmViaDock();
    await userEvent.click(screen.getByRole("button", { name: "確認並簽署" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("目前不可選擇");
    expect(signMessageAsync).not.toHaveBeenCalled();
  });

  it("送出 500（寫檔失敗）→ 明說 leader 沒有被變更，且不自動重試", async () => {
    postLeaderSelect.mockRejectedValue(
      new ApiError("client", "變更記錄寫入失敗", 500, "變更記錄寫入失敗"),
    );
    render(wrap(<LeadersPage />));
    await openConfirmViaDock();
    await userEvent.click(screen.getByRole("button", { name: "確認並簽署" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("你的 leader 沒有被變更");
    expect(postLeaderSelect).toHaveBeenCalledTimes(1);
  });

  it("大小寫不同但實為同一位址 → 不得誤擋，流程正常完成", async () => {
    getLeaderSelectMessage.mockResolvedValue({
      ...CUSTOM_MSG,
      leader_address: CUSTOM_MSG.leader_address.toUpperCase(),
    });
    render(wrap(<LeadersPage />));
    await openConfirmViaDock();
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
    await openConfirmViaDock();
    await userEvent.click(screen.getByRole("button", { name: "確認並簽署" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("只能變更自己帳號的 leader");
  });

  it("送出 429（查詢額度用完）→ 專屬文案，不得說『上一筆簽名已作廢』", async () => {
    postLeaderSelect.mockRejectedValue(
      new ApiError("client", "查詢過於頻繁，請稍後再試", 429, "查詢過於頻繁，請稍後再試"),
    );
    render(wrap(<LeadersPage />));
    await openConfirmViaDock();
    await userEvent.click(screen.getByRole("button", { name: "確認並簽署" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("你的 leader 沒有被變更");
    expect(alert).toHaveTextContent("等約一分鐘");
    expect(alert).not.toHaveTextContent("已作廢");
  });

  it("⭐ already_listed=true → 標示已在精選清單，聲明勾選要求維持", async () => {
    getLeaderPreview.mockResolvedValue({ ...CUSTOM_PREVIEW, already_listed: true });
    render(wrap(<LeadersPage />));
    await previewCustom();

    expect(screen.getByText("此位址已在精選清單")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "跟單此地址" })).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox", { name: /未審核 leader/ }));
    expect(screen.getByRole("button", { name: "跟單此地址" })).toBeEnabled();
  });

  it("⭐ already_listed 且在背景目錄找得到 → 沿用策展名稱，不誤稱無法核對", async () => {
    getLeaderPreview.mockResolvedValue({
      ...CUSTOM_PREVIEW, address: LISTED_ADDR, already_listed: true,
    });
    render(wrap(<LeadersPage />));
    await previewCustom(LISTED_ADDR);

    const card = screen.getByText("鏈上預覽").closest(".leader-custom-preview")!;
    expect(card.textContent).toContain("Alpha"); // 沿用背景目錄的策展名，不寫死
    expect(card.textContent).toContain("此位址已在精選清單");
    expect(card.textContent).not.toMatch(/無法核對/);
  });

  it("⭐ already_listed 且在背景目錄找得到 → 確認框用策展名稱，不是寫死的「未審核 leader」", async () => {
    getLeaderPreview.mockResolvedValue({
      ...CUSTOM_PREVIEW, address: LISTED_ADDR, already_listed: true,
    });
    render(wrap(<LeadersPage />));
    await previewCustom(LISTED_ADDR);
    await userEvent.click(screen.getByRole("checkbox", { name: /未審核 leader/ }));
    await userEvent.click(screen.getByRole("button", { name: "跟單此地址" }));

    const dialog = screen.getByRole("dialog");
    expect(dialog.textContent).toContain("Alpha");
    expect(dialog.textContent).toContain("0x1111…111");
    expect(dialog.textContent).not.toContain("未審核 leader");
  });

  it("⭐ already_listed 但背景目錄查不到（paused 或未列示）→ 明說無法核對，不誤稱已確認", async () => {
    getLeaderPreview.mockResolvedValue({
      ...CUSTOM_PREVIEW, already_listed: true, accepting_new: false,
    });
    render(wrap(<LeadersPage />));
    await previewCustom();

    const card = screen.getByText("鏈上預覽").closest(".leader-custom-preview")!;
    expect(card.textContent).toMatch(/無法核對/);
    // 例行下架的既有警示不受影響（放行帶警示，不是拒絕）
    expect(card.textContent).toMatch(/未開放接受新跟單者/);
  });

  it("背景目錄載入失敗 → already_listed 的預覽仍可用，且不誤稱已核對名單", async () => {
    getLeaders.mockRejectedValue(new ApiError("upstream", "leader 名單暫時不可用", 503));
    getLeaderPreview.mockResolvedValue({
      ...CUSTOM_PREVIEW, address: LISTED_ADDR, already_listed: true,
    });
    render(wrap(<LeadersPage />));
    await previewCustom(LISTED_ADDR);

    const card = screen.getByText("鏈上預覽").closest(".leader-custom-preview")!;
    expect(card.textContent).toMatch(/無法核對/);
  });

  it("⭐ exists=false（鏈上無活動）→ 顯示警示但不擋，勾選後可送出走簽章（2026-07-27 裁決）", async () => {
    getLeaderPreview.mockResolvedValue({
      ...CUSTOM_PREVIEW, exists: false, account_value: "0", position_count: 0,
    });
    render(wrap(<LeadersPage />));
    await previewCustom();

    const card = screen.getByText("鏈上預覽").closest(".leader-custom-preview")!;
    expect(card.textContent).toMatch(/無 perp 交易活動/);
    expect(card.textContent).toMatch(/進場後.*自動開始跟單/);

    await userEvent.click(screen.getByRole("checkbox", { name: /未審核 leader/ }));
    const select = screen.getByRole("button", { name: "跟單此地址" });
    expect(select).toBeEnabled();
    await userEvent.click(select);
    await userEvent.click(screen.getByRole("button", { name: "確認並簽署" }));
    expect(await screen.findByText(/已授權，於引擎的下一個 cycle 生效/)).toBeInTheDocument();
    expect(getLeaderSelectMessage).toHaveBeenCalledWith(CUSTOM_ADDR);
  });

  it("⭐ accepting_new=false（例行下架）→ 顯示警示但不擋，勾選後可送出走簽章（2026-07-27 拆旗標）", async () => {
    getLeaderPreview.mockResolvedValue({ ...CUSTOM_PREVIEW, accepting_new: false });
    render(wrap(<LeadersPage />));
    await previewCustom();

    const card = screen.getByText("鏈上預覽").closest(".leader-custom-preview")!;
    expect(card.textContent).toMatch(/未開放接受新跟單者/);
    expect(card.textContent).toMatch(/仍可完成配置並跟隨/);

    await userEvent.click(screen.getByRole("checkbox", { name: /未審核 leader/ }));
    const select = screen.getByRole("button", { name: "跟單此地址" });
    expect(select).toBeEnabled();
    await userEvent.click(select);
    await userEvent.click(screen.getByRole("button", { name: "確認並簽署" }));
    expect(await screen.findByText(/已授權，於引擎的下一個 cycle 生效/)).toBeInTheDocument();
    expect(getLeaderSelectMessage).toHaveBeenCalledWith(CUSTOM_ADDR);
  });

  it.each([
    ["invalid_format", /位址格式不正確/],
    ["self_follow", /不能跟單自己/],
    ["leader_disabled", /已被平台安全撤銷/],
  ] as const)("⭐ 准入被拒 reason=%s → 對應文案，零預覽卡", async (reason, copyRe) => {
    getLeaderPreview.mockRejectedValue(
      new ApiError("client", "後端人話", 400, "後端人話", reason),
    );
    render(wrap(<LeadersPage />));
    await pasteAddress(CUSTOM_ADDR);
    await userEvent.click(screen.getByRole("button", { name: "查詢" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(copyRe);
    expect(screen.queryByText("鏈上預覽")).not.toBeInTheDocument();
  });

  it("查詢失敗（上游不可用）→ 明說這次查詢沒有改變跟單設定，且附後端 detail", async () => {
    getLeaderPreview.mockRejectedValue(
      new ApiError("upstream", "上游服務暫時不可用", 503, "上游服務暫時不可用"),
    );
    render(wrap(<LeadersPage />));
    await pasteAddress(CUSTOM_ADDR);
    await userEvent.click(screen.getByRole("button", { name: "查詢" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/查詢失敗/);
    expect(alert.textContent).toMatch(/沒有改變你的跟單設定/);
    expect(alert.textContent).toContain("上游服務暫時不可用");
    expect(screen.queryByText("鏈上預覽")).not.toBeInTheDocument();
  });

  it("⭐ 預覽後修改輸入 → 預覽卡與勾選立即重置（舊位址的預覽不得留在畫面上）", async () => {
    render(wrap(<LeadersPage />));
    await previewCustom();
    await userEvent.click(screen.getByRole("checkbox", { name: /未審核 leader/ }));

    // 在輸入框尾端多打一個字元：位址已不是預覽的那一個
    const input = screen.getByLabelText(/leader 錢包位址/);
    await userEvent.type(input, "f");

    expect(screen.queryByText("鏈上預覽")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "跟單此地址" })).not.toBeInTheDocument();
  });
});

/**
 * ⭐ 「我目前跟誰」（spec User Story 12；後端 /api/me/leader）。
 *
 * 為什麼這一區的失敗方向特別重要：本頁要客戶簽署一份**換掉**現有 leader 的授權，
 * 卻沒有左半邊的對照（我現在跟的是誰）。所以這一區的錯誤方向不對稱——
 * 顯示不出來只是少一個資訊，而把「讀不到」畫成「你沒在跟單」則是一句我們沒有根據
 * 的斷言（後端因此把 not_activated 與 indeterminate 分成兩態，前端不得合併）。
 */
describe("LeadersPage — 目前跟隨的 leader（/api/me/leader）⭐", () => {
  /** 這一區的 DOM 範圍。 */
  function currentPanel(): HTMLElement {
    return document.querySelector(".leader-current")!;
  }

  it("⭐ status=following → 顯示目前跟隨的位址縮寫與名稱，並附後端原文說明", async () => {
    render(wrap(<LeadersPage />));
    await screen.findByText("你目前跟隨的 leader");

    const panel = currentPanel();
    expect(panel.textContent).toContain("0x1111…111"); // shortAddr
    expect(panel.textContent).toContain("Alpha");
    expect(panel.textContent).toContain("這是引擎目前為你跟隨的 leader。");
  });

  it("leader_name 為 null（不在目前的可選清單）→ 照樣顯示位址，不顯示空名稱", async () => {
    getMyLeader.mockResolvedValue(myLeader({ leader_name: null }));
    render(wrap(<LeadersPage />));
    await screen.findByText("你目前跟隨的 leader");

    expect(currentPanel().textContent).toContain("0x1111…111");
  });

  it("⭐ 尚未設定 leader（status=engine_default）→ 合理提示＋後端原文，不畫任何位址", async () => {
    getMyLeader.mockResolvedValue(myLeader({
      status: "engine_default", leader_address: null, leader_name: null,
      note: "你已啟用跟單，但尚未指定 leader，引擎沿用部署的預設設定。",
    }));
    render(wrap(<LeadersPage />));
    await screen.findByText("你目前跟隨的 leader");

    const panel = currentPanel();
    expect(panel.textContent).toMatch(/尚未指定 leader/);
    expect(panel.textContent).toContain("引擎沿用部署的預設設定");
    expect(panel.textContent).not.toMatch(/0x/); // 沒有 leader 就不畫位址
  });

  it("⭐ 帳號尚未啟用（status=not_activated）→ 說明尚未啟用，不宣稱「沒有 leader」", async () => {
    getMyLeader.mockResolvedValue(myLeader({
      status: "not_activated", leader_address: null, leader_name: null,
      note: "你的帳號尚未啟用跟單（啟用是人工作業）。",
    }));
    render(wrap(<LeadersPage />));
    await screen.findByText("你目前跟隨的 leader");

    expect(currentPanel().textContent).toMatch(/尚未啟用跟單/);
  });

  it("⭐ status=indeterminate → 明說無法確認，不得被讀成「未在跟單」", async () => {
    getMyLeader.mockResolvedValue(myLeader({
      status: "indeterminate", leader_address: null, leader_name: null,
      note: "目前無法確認你的跟隨狀態（帳號清單有無法解析的條目）；請聯絡管理員，"
        + "不要當作「未在跟單」處理。",
    }));
    render(wrap(<LeadersPage />));
    await screen.findByText("你目前跟隨的 leader");

    const panel = currentPanel();
    expect(panel.textContent).toMatch(/無法確認你的跟隨狀態/);
    expect(panel.textContent).toMatch(/不要當作「未在跟單」處理/);
  });

  it("⭐ pending_change → 一併顯示待生效的目標位址與後端說明", async () => {
    getMyLeader.mockResolvedValue(myLeader({
      pending_change: {
        leader_address: "0x2222222222222222222222222222222222222222",
        issued_at: "2026-07-27T00:00:00Z",
        effective: "next_engine_cycle",
        note: "你已簽署換 leader，尚未生效：引擎會在下一個 cycle 重新驗證後套用。",
      },
    }));
    render(wrap(<LeadersPage />));
    await screen.findByText("你目前跟隨的 leader");

    const panel = currentPanel();
    expect(panel.textContent).toContain("0x2222…222");
    expect(panel.textContent).toMatch(/尚未生效/);
    expect(panel.textContent).toContain("2026-07-27T00:00:00Z");
  });

  it("⭐ 簽章成功後重抓本區塊：剛簽的那筆待生效變更要立刻看得到，不必重新整理", async () => {
    render(wrap(<LeadersPage />));
    await openConfirmViaDock();
    await userEvent.click(screen.getByRole("button", { name: "確認並簽署" }));
    await screen.findByText(/已授權，於引擎的下一個 cycle 生效/);

    // 同一頁不得同時說「已授權」與「沒有待生效的變更」——授權成功後本區塊重抓一次
    await waitFor(() => expect(getMyLeader.mock.calls.length).toBeGreaterThan(1));
  });

  it("⭐ 查詢失敗 → 本區塊說明讀不到，頁面其餘部分（地址 dock）仍可用", async () => {
    getMyLeader.mockRejectedValue(
      new ApiError("upstream", "跟隨狀態暫時不可用", 503, "跟隨狀態暫時不可用"),
    );
    render(wrap(<LeadersPage />));

    // 本區塊：說明讀不到，且**不得**宣稱「你沒在跟單」
    expect(await screen.findByText(/無法讀取你目前的跟隨狀態/)).toBeInTheDocument();

    // 頁面其餘部分照常運作
    expect(await screen.findByLabelText(/leader 錢包位址/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查詢" })).toBeInTheDocument();
  });

  it("未登入 → 不打 /api/me/leader（避免必然的 401 噪音）", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    render(wrap(<LeadersPage />, null));
    await screen.findByText(/尚未登入/);
    expect(getMyLeader).not.toHaveBeenCalled();
  });
});

describe("LeadersPage — 其他狀態", () => {
  it("未登入 → 顯示未登入文案，不打清單端點", async () => {
    getMe.mockRejectedValue(new ApiError("auth", "未登入", 401));
    render(wrap(<LeadersPage />, null));
    expect(await screen.findByText(/尚未登入/)).toBeInTheDocument();
    expect(getLeaders).not.toHaveBeenCalled();
  });
});

// ── 風控設定（2026-07-30 改版：slider ＋ 簽章送出 ＋ 自助解除熔斷）────────
//
// ⭐ 本區的設計前提（使用者裁決）：風控門檻涉及利益衝突，所以不由我們替客戶決定
// ——每個參數都給 slider、把我方建議值標在旁邊，最終由客戶自己決定。因此這裡的
// 測試全部針對「畫面上的數字是不是真的來自後端 specs」與「送出的東西是不是客戶
// 自己簽的那一份」，而不是針對某個特定的門檻值。
const RISK_TOGGLE = /啟用 Filet 風控系統/;
const SAVE_BUTTON = "簽署並儲存風控設定";
const RESUME_BUTTON = "立即恢復跟單";

async function enableRisk() {
  await userEvent.click(await screen.findByRole("checkbox", { name: RISK_TOGGLE }));
}

describe("LeadersPage — 風控設定：分組與顯示", () => {
  it("⭐ group=tracking 的參數在**未勾選**風控時仍然顯示（它不受風控開關影響）", async () => {
    // 藏在 checkbox 底下會讓關掉風控的客戶以為自己也關掉了它——而它照樣生效。
    render(wrap(<LeadersPage />));
    const toggle = await screen.findByRole("checkbox", { name: RISK_TOGGLE });
    expect(toggle).not.toBeChecked();
    expect(screen.getByLabelText("與 leader 的部位差異容忍度")).toBeInTheDocument();
  });

  it("group=risk 的參數只有勾選後才出現（沒開風控時這些門檻不會被執法）", async () => {
    render(wrap(<LeadersPage />));
    await screen.findByRole("checkbox", { name: RISK_TOGGLE });
    expect(screen.queryByLabelText("7 天滾動回撤上限")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("熔斷後的冷靜期（小時）")).not.toBeInTheDocument();

    await enableRisk();
    expect(screen.getByLabelText("7 天滾動回撤上限")).toBeInTheDocument();
    expect(screen.getByLabelText("熔斷後的冷靜期（小時）")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /熔斷時自動平倉/ })).toBeInTheDocument();
  });

  it("⭐ 比例參數＝slider，上下界與建議值取自後端 specs（前端不硬編）", async () => {
    render(wrap(<LeadersPage />));
    await enableRisk();

    const dd = screen.getByLabelText("7 天滾動回撤上限") as HTMLInputElement;
    expect(dd).toHaveAttribute("type", "range");
    expect(dd).toHaveAttribute("min", "5");    // spec.min 0.05 → 5%
    expect(dd).toHaveAttribute("max", "50");   // spec.max 0.50 → 50%
    expect(dd.value).toBe("20");               // prefs 0.2 → 20%
    expect(screen.getByText("20%")).toBeInTheDocument();
    expect(screen.getByText("建議：20%")).toBeInTheDocument();  // spec.recommended

    const total = screen.getByLabelText("累計回撤上限") as HTMLInputElement;
    expect(total).toHaveAttribute("min", "0");
    expect(total).toHaveAttribute("max", "80");
    expect(screen.getByText("建議：40%")).toBeInTheDocument();

    // tracking 組同樣是 slider ＋ 建議值
    const tol = screen.getByLabelText("與 leader 的部位差異容忍度") as HTMLInputElement;
    expect(tol).toHaveAttribute("type", "range");
    expect(tol).toHaveAttribute("min", "2");
    expect(tol).toHaveAttribute("max", "25");
    expect(screen.getByText("建議：8%")).toBeInTheDocument();
  });

  it("⭐ unit=hours 的參數以小時呈現、整數刻度（不做百分比換算）", async () => {
    render(wrap(<LeadersPage />));
    await enableRisk();

    const cd = screen.getByLabelText("熔斷後的冷靜期（小時）") as HTMLInputElement;
    expect(cd).toHaveAttribute("min", "0");
    expect(cd).toHaveAttribute("max", "168");
    expect(cd).toHaveAttribute("step", "1");
    expect(cd.value).toBe("12");
    expect(screen.getByText("12小時")).toBeInTheDocument();
    expect(screen.getByText("建議：12小時")).toBeInTheDocument();
  });

  it("⭐⭐ 門檻數字全部跟著 specs 走：換一份 specs，畫面就換一組界線與建議值", async () => {
    // 這條是「不得硬編」的直接證明：前端沒有任何一個數字是自己寫的，改後端區間
    // 前端不必動——反過來說，硬編了就會在這裡轉紅。
    const base = myRisk();
    getMyRisk.mockResolvedValue({
      ...base,
      specs: base.specs.map((s) =>
        s.name === "max_drawdown_pct"
          ? { ...s, min: "0.1", max: "0.3", recommended: "0.25" }
          : s),
    });
    render(wrap(<LeadersPage />));
    await enableRisk();

    const dd = screen.getByLabelText("7 天滾動回撤上限");
    expect(dd).toHaveAttribute("min", "10");
    expect(dd).toHaveAttribute("max", "30");
    expect(screen.getByText("建議：25%")).toBeInTheDocument();
  });

  it("⭐ 讀不到設定 → 只說讀不到，不畫一個預設值的表單當成客戶的設定", async () => {
    getMyRisk.mockRejectedValue(new ApiError("upstream", "500", 500));
    render(wrap(<LeadersPage />));
    expect(await screen.findByText(/風控設定暫時讀不到/)).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: RISK_TOGGLE })).not.toBeInTheDocument();
  });

  it("文案不得把啟用風控講成不會虧（誠信紅線）", async () => {
    render(wrap(<LeadersPage />));
    const help = await screen.findByText(/開啟後：權益回撤達到你設定的門檻/);
    expect(help.textContent).toMatch(/並不會讓本金免於虧損/);
  });
});

describe("LeadersPage — 風控設定：簽章送出", () => {
  it("⭐ 儲存＝簽章流程：伺服器原文原樣進錢包，整包 payload 原樣回送", async () => {
    render(wrap(<LeadersPage />));
    await enableRisk();
    await userEvent.click(screen.getByRole("button", { name: SAVE_BUTTON }));

    await waitFor(() => expect(postMyRisk).toHaveBeenCalledTimes(1));
    // 送去換原文的是畫面上的那一份草稿（含 enabled=true 與 tracking 參數）
    expect(getRiskSettingsMessage).toHaveBeenCalledWith(
      expect.objectContaining({ enabled: true, size_tolerance: "0.08" }));
    const payload = await getRiskSettingsMessage.mock.results[0].value;
    expect(signMessageAsync).toHaveBeenCalledWith({ message: payload.message });
    expect(postMyRisk).toHaveBeenCalledWith(payload, SIG);
    // 生效說明用後端原文（單一來源）
    expect(await screen.findByText(/套用這份設定/)).toBeInTheDocument();
  });

  it("⭐ 未啟用風控時照樣能送出：body 一律帶 enabled（後端據此區分「關」與「沒表達」）", async () => {
    render(wrap(<LeadersPage />));
    await screen.findByRole("checkbox", { name: RISK_TOGGLE });
    await userEvent.click(screen.getByRole("button", { name: SAVE_BUTTON }));

    await waitFor(() => expect(getRiskSettingsMessage).toHaveBeenCalledTimes(1));
    expect(getRiskSettingsMessage.mock.calls[0][0]).toHaveProperty("enabled", false);
  });

  it("拉動 slider：畫面顯示與送出的值是同一個數（33.3% ⇔ 0.333）", async () => {
    render(wrap(<LeadersPage />));
    await enableRisk();
    fireEvent.change(screen.getByLabelText("7 天滾動回撤上限"), {
      target: { value: "33.3" },
    });
    expect(screen.getByText("33.3%")).toBeInTheDocument();          // 顯示

    await userEvent.click(screen.getByRole("button", { name: SAVE_BUTTON }));
    await waitFor(() => expect(getRiskSettingsMessage).toHaveBeenCalledTimes(1));
    expect(getRiskSettingsMessage.mock.calls[0][0])
      .toMatchObject({ max_drawdown_pct: "0.333" });                // 送出（同一個數）
  });

  it("冷靜期 slider 以小時送出（不換算成比例）", async () => {
    render(wrap(<LeadersPage />));
    await enableRisk();
    fireEvent.change(screen.getByLabelText("熔斷後的冷靜期（小時）"), {
      target: { value: "24" },
    });
    expect(screen.getByText("24小時")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: SAVE_BUTTON }));
    await waitFor(() => expect(getRiskSettingsMessage).toHaveBeenCalledTimes(1));
    expect(getRiskSettingsMessage.mock.calls[0][0]).toMatchObject({ cooldown_hours: "24" });
  });

  it("熔斷自動平倉可以關掉（軟暫停語意）", async () => {
    render(wrap(<LeadersPage />));
    await enableRisk();
    await userEvent.click(screen.getByRole("checkbox", { name: /熔斷時自動平倉/ }));
    await userEvent.click(screen.getByRole("button", { name: SAVE_BUTTON }));

    await waitFor(() => expect(getRiskSettingsMessage).toHaveBeenCalledTimes(1));
    expect(getRiskSettingsMessage.mock.calls[0][0])
      .toMatchObject({ flatten_on_breach: false });
  });

  it("⭐ recover 出的簽章者 ≠ 登入地址 → 中止，且**零網路請求**（不送出）", async () => {
    recoverPersonalSigner.mockResolvedValue("0x9999999999999999999999999999999999999999");
    render(wrap(<LeadersPage />));
    await enableRisk();
    await userEvent.click(screen.getByRole("button", { name: SAVE_BUTTON }));

    expect(await screen.findByText(/簽署的錢包與你登入的錢包不是同一個/)).toBeInTheDocument();
    expect(postMyRisk).not.toHaveBeenCalled();
  });

  it("⭐ 伺服器回一份指向別組設定的原文 → 進錢包之前擋下（不喚起錢包、不送出）", async () => {
    // 被打穿的 filet-api 想無中生有一次「把保護關掉」，唯一的著力點就是這裡。
    getRiskSettingsMessage.mockImplementation(async (prefs: RiskPrefs) => {
      const evil = { ...prefs, enabled: false };
      return {
        message: riskMessageFor(evil), nonce: "n-risk",
        issued_at: "2026-07-30T02:00:00Z", account_id: ME.account_id, prefs: evil,
      };
    });
    render(wrap(<LeadersPage />));
    await enableRisk();
    await userEvent.click(screen.getByRole("button", { name: SAVE_BUTTON }));

    expect(await screen.findByText(/伺服器回傳的待簽內容與你在畫面上設定的不一致/))
      .toBeInTheDocument();
    expect(signMessageAsync).not.toHaveBeenCalled();
    expect(postMyRisk).not.toHaveBeenCalled();
  });

  it("錢包取消 → 明說設定沒有被變更", async () => {
    signMessageAsync.mockRejectedValue(new Error("User rejected"));
    render(wrap(<LeadersPage />));
    await enableRisk();
    await userEvent.click(screen.getByRole("button", { name: SAVE_BUTTON }));

    expect(await screen.findByText(/你在錢包取消了簽署/)).toBeInTheDocument();
    expect(postMyRisk).not.toHaveBeenCalled();
  });

  it("送出失敗 → 顯示後端訊息，不假裝成功", async () => {
    postMyRisk.mockRejectedValue(
      new ApiError("client", "nonce 已被使用", 409, "nonce 已被使用"));
    render(wrap(<LeadersPage />));
    await enableRisk();
    await userEvent.click(screen.getByRole("button", { name: SAVE_BUTTON }));

    expect(await screen.findByText(/nonce 已被使用/)).toBeInTheDocument();
    expect(screen.queryByText(/風控設定已送出/)).not.toBeInTheDocument();
  });
});

describe("LeadersPage — 已提交 vs 已生效", () => {
  it("已生效值與已提交值一致 → 說一致", async () => {
    render(wrap(<LeadersPage />));
    expect(await screen.findByText(/目前生效中的設定與你提交的一致/)).toBeInTheDocument();
  });

  it("⭐ 已生效值與已提交值不一致 → 「已提交，尚未生效」", async () => {
    getMyRisk.mockResolvedValue(myRisk({
      prefs: { ...RISK_PREFS, enabled: true },
      // 引擎仍在用舊門檻（風控未開）⇒ 逐項比對不一致
      applied: {
        controls_enabled: false, source: "signed_settings", changed_at: null,
        prefs: { ...RISK_PREFS, enabled: false },
      },
    }));
    render(wrap(<LeadersPage />));
    expect(await screen.findByText(/已提交，尚未生效/)).toBeInTheDocument();
  });

  it("⭐⭐ applied 為 null（引擎心跳讀不到）→ 說「無法確認」，**不得**畫成「尚未生效」", async () => {
    getMyRisk.mockResolvedValue(myRisk({ applied: null }));
    render(wrap(<LeadersPage />));
    expect(await screen.findByText(/目前生效的設定暫時無法確認/)).toBeInTheDocument();
    expect(screen.queryByText(/尚未生效/)).not.toBeInTheDocument();
    expect(screen.queryByText(/目前生效中的設定與你提交的一致/)).not.toBeInTheDocument();
  });

  it("applied 在但 controls_enabled 為 null → 同樣是「無法確認」", async () => {
    getMyRisk.mockResolvedValue(myRisk({
      applied: {
        controls_enabled: null, source: "unknown", changed_at: null, prefs: null,
      },
    }));
    render(wrap(<LeadersPage />));
    expect(await screen.findByText(/目前生效的設定暫時無法確認/)).toBeInTheDocument();
  });

  it("從未提交過 → 明說畫面上是預設值（不謊稱「與你提交的一致」）", async () => {
    getMyRisk.mockResolvedValue(myRisk({ submitted: { issued_at: null } }));
    render(wrap(<LeadersPage />));
    expect(await screen.findByText(/你尚未提交過風控設定/)).toBeInTheDocument();
  });
});

describe("LeadersPage — 熔斷與立即恢復跟單", () => {
  it("⭐ 熔斷中 → 醒目區塊：原因、觸發時間、預計自動恢復時間，並提供恢復按鈕", async () => {
    getMyRisk.mockResolvedValue(halted());
    render(wrap(<LeadersPage />));

    expect(await screen.findByText("你的跟單已被風控停止")).toBeInTheDocument();
    const box = screen.getByText("你的跟單已被風控停止").closest(".risk-halted")!;
    expect(box.textContent).toContain("max_drawdown_pct");
    expect(box.textContent).toContain("2026-07-30T04:00:00Z");
    expect(box.textContent).toContain("2026-07-30T16:00:00Z");
    expect(screen.getByRole("button", { name: RESUME_BUTTON })).toBeInTheDocument();
  });

  it("⭐ 按「立即恢復跟單」→ 走 unlock 簽章流程（伺服器原文原樣進錢包）", async () => {
    getMyRisk.mockResolvedValue(halted());
    render(wrap(<LeadersPage />));
    await userEvent.click(await screen.findByRole("button", { name: RESUME_BUTTON }));

    await waitFor(() => expect(postRiskUnlock).toHaveBeenCalledTimes(1));
    expect(getRiskUnlockMessage).toHaveBeenCalledTimes(1);
    expect(signMessageAsync).toHaveBeenCalledWith({ message: UNLOCK_MSG });
    const payload = await getRiskUnlockMessage.mock.results[0].value;
    expect(postRiskUnlock).toHaveBeenCalledWith(payload, SIG);
    expect(await screen.findByText(/引擎會在下一輪恢復跟單/)).toBeInTheDocument();
    // ⭐ 解鎖是一次性動作，且與「調整設定」是兩個域：不得順手送出一份設定
    expect(postMyRisk).not.toHaveBeenCalled();
  });

  it("⭐ 恢復流程 recover 不符 → 不送出（零網路請求）", async () => {
    getMyRisk.mockResolvedValue(halted());
    recoverPersonalSigner.mockResolvedValue("0x9999999999999999999999999999999999999999");
    render(wrap(<LeadersPage />));
    await userEvent.click(await screen.findByRole("button", { name: RESUME_BUTTON }));

    expect(await screen.findByText(/簽署的錢包與你登入的錢包不是同一個/)).toBeInTheDocument();
    expect(postRiskUnlock).not.toHaveBeenCalled();
  });

  it("⭐⭐ 不可自助恢復（治理動作）→ **不顯示**恢復按鈕，改顯示說明", async () => {
    // ⭐ 判定依據是後端的 `resumable`（引擎 rearm_allowed_for 導出），不是前端比對
    // reason 字串——引擎新增一種不可恢復的原因時，這裡不必跟著改就已經是對的。
    getMyRisk.mockResolvedValue(
      halted({ reason: "leader_revoked", resumable: false }));
    render(wrap(<LeadersPage />));

    expect(await screen.findByText(/已被我們下架/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: RESUME_BUTTON })).not.toBeInTheDocument();
  });

  it("⭐ resumable 為 null（引擎版本較舊、判不出來）→ 同樣不給按鈕", async () => {
    getMyRisk.mockResolvedValue(halted({ reason: null, resumable: null }));
    render(wrap(<LeadersPage />));

    expect(await screen.findByText(/已被我們下架/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: RESUME_BUTTON })).not.toBeInTheDocument();
  });

  it("冷靜期 0（沒有自動恢復時刻）→ 說明只能自己按，不留白", async () => {
    getMyRisk.mockResolvedValue(halted({ cooldown_hours: "0", resume_at: null }));
    render(wrap(<LeadersPage />));

    expect(await screen.findByText(/沒有預計的自動恢復時間/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: RESUME_BUTTON })).toBeInTheDocument();
  });

  it("⭐⭐ halted 為 null（引擎狀態讀不到）→ 說「無法確認」，**不得**畫成「沒熔斷」", async () => {
    getMyRisk.mockResolvedValue(myRisk({ halted: null }));
    render(wrap(<LeadersPage />));

    expect(await screen.findByText(/目前無法確認你的風控是否被觸發/)).toBeInTheDocument();
    // 讀不到不等於沒事：既不畫熔斷區塊，也不給一顆按了沒意義的恢復按鈕
    expect(screen.queryByText("你的跟單已被風控停止")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: RESUME_BUTTON })).not.toBeInTheDocument();
  });

  it("確定沒有熔斷（tripped=false）→ 整區不畫，也不顯示「無法確認」", async () => {
    render(wrap(<LeadersPage />));
    await screen.findByRole("checkbox", { name: RISK_TOGGLE });
    expect(screen.queryByText("你的跟單已被風控停止")).not.toBeInTheDocument();
    expect(screen.queryByText(/目前無法確認你的風控是否被觸發/)).not.toBeInTheDocument();
  });
});

describe("LeadersPage — 熔斷時的殘留部位（2026-07-31 使用者裁決）", () => {
  it("⭐⭐ 有殘留部位：**照樣**顯示恢復按鈕，但同時把殘留部位講出來", async () => {
    // 使用者裁決：殘留部位不擋自助解鎖——恢復本身就是收拾殘局的手段（引擎下一輪
    // 會把它往 leader 的目標收斂），維持鎖定只會讓那個部位無人管理地留在市場上。
    // 但客戶按下去之前有權知道市場上還留著什麼。
    getMyRisk.mockResolvedValue(halted({ residual_exposure: true }));
    render(wrap(<LeadersPage />));

    expect(await screen.findByText(/有部位未能平倉或掛單未撤/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: RESUME_BUTTON })).toBeInTheDocument();
  });

  it("沒有殘留部位 → 不顯示那句提示（不製造不存在的疑慮）", async () => {
    getMyRisk.mockResolvedValue(halted({ residual_exposure: false }));
    render(wrap(<LeadersPage />));

    await screen.findByRole("button", { name: RESUME_BUTTON });
    expect(screen.queryByText(/有部位未能平倉或掛單未撤/)).not.toBeInTheDocument();
  });
});
