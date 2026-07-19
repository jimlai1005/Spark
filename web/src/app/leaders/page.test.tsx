import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { NO_VALUE } from "@/lib/format";
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

/**
 * 績效窗 fixture。⭐ 依**後端實際線上形狀**：Decimal 欄位序列化後是 **string**
 * （filet/leader_perf.py `jsonable_performance`），`sample_count` 才是 number。
 * ⭐⭐ 分級揭露的載體是**鍵的不存在**——低層級的 fixture 一律**不寫**那些鍵，
 * 不是寫成 null。fixture 若補成 null，就驗不到本頁真正要防的那件事。
 */
const PERF_ANNUALIZABLE = {
  period: "perpAllTime", basis: "perp", status: "ok", reason: null,
  disclosure_tier: "annualizable", sample_count: 745, covered_days: "412.5000",
  first_ts_ms: 1700000000000, last_ts_ms: 1735640000000, skipped_intervals: 0,
  cum_pnl: "18234.55", twr: "0.3421", max_drawdown: "0.1875",
  annualized_return: "0.2938",
};
/** 不足 90 天 → **沒有** `annualized_return` 鍵。 */
const PERF_WINDOW = {
  period: "perpMonth", basis: "perp", status: "ok", reason: null,
  disclosure_tier: "window_return", sample_count: 128, covered_days: "45.2000",
  first_ts_ms: 1731000000000, last_ts_ms: 1734900000000, skipped_intervals: 2,
  cum_pnl: "820.10", twr: "0.0712", max_drawdown: "0.0304",
};
/** 不足 30 天 → **沒有** `twr`／`max_drawdown` 鍵。 */
const PERF_PNL_ONLY = {
  period: "perpMonth", basis: "perp", status: "ok", reason: null,
  disclosure_tier: "pnl_only", sample_count: 24, covered_days: "6.1000",
  first_ts_ms: 1734300000000, last_ts_ms: 1734827040000, skipped_intervals: 0,
  cum_pnl: "310.75",
};
/** 資料不足 → 連 `cum_pnl` 都沒有；covered_days 為 null（後端 `_insufficient` 原形）。 */
const PERF_INSUFFICIENT = {
  period: "perpMonth", basis: "perp", status: "insufficient",
  reason: "need_at_least_two_samples", disclosure_tier: "insufficient",
  sample_count: 1, covered_days: null, first_ts_ms: null, last_ts_ms: null,
  skipped_intervals: 0,
};

const PERF_NOTES = {
  basis: "後端基準原文",
  upper_bound: "後端上界原文：leader 的報酬是跟單者的上界，不是期望值。",
  max_drawdown: "後端回撤原文：MDD 由 15 分鐘取樣推得，系統性低估。",
  sufficiency: "後端充足度原文：涵蓋天數與樣本點數決定可信度。",
};

/** 帶績效的目錄回應（`performance_available: true` ＋ 逐 leader 的 `performance`）。 */
function leadersWithPerf(
  performance: Record<string, unknown> | null,
  over: Partial<LeadersResp> = {},
): LeadersResp {
  const base = leaders();
  return {
    ...base,
    leaders: [{ ...base.leaders[0], performance }],
    performance_available: true,
    performance_basis: "perp",
    performance_windows: ["perpMonth", "perpAllTime"],
    performance_notes: PERF_NOTES,
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

/**
 * ⭐⭐ 績效揭露。本區每一條都對應一條誠信規則——這一頁的數字會直接決定客戶把錢
 * 投給誰，所以「少顯示」永遠優於「顯示一個看起來正常但是前端造出來的數字」。
 */
describe("LeadersPage — 績效分級揭露 ⭐⭐（缺鍵＝不該顯示，不是顯示為空）", () => {
  it("⭐ 誠信 1：annualizable → 四個數字都畫；型別是後端的 string（Decimal 序列化）", async () => {
    getLeaders.mockResolvedValue(leadersWithPerf({ perpAllTime: PERF_ANNUALIZABLE }));
    render(wrap(<LeadersPage />));

    const block = (await screen.findByLabelText("績效（perp）"));
    expect(block.textContent).toContain("18,234.55");   // cum_pnl
    expect(block.textContent).toContain("34.2%");       // twr 0.3421
    expect(block.textContent).toContain("18.8%");       // max_drawdown 0.1875
    expect(block.textContent).toContain("29.4%");       // annualized_return 0.2938
    expect(screen.getByText("年化報酬率")).toBeInTheDocument();
  });

  it("⭐⭐ 誠信 1：不足 90 天（無 annualized_return 鍵）→ 年化整個欄位不渲染，不畫「—」", async () => {
    getLeaders.mockResolvedValue(leadersWithPerf({ perpMonth: PERF_WINDOW }));
    render(wrap(<LeadersPage />));

    const block = await screen.findByLabelText("績效（perp）");
    // 欄位本身不存在——不是存在但顯示為空
    expect(document.querySelectorAll(".leader-perf-stat-annualized-return")).toHaveLength(0);
    expect(screen.queryByText("年化報酬率")).not.toBeInTheDocument();
    // ⭐ 釘死實際畫出來的格子：只有三格，且沒有任何一格是佔位符。
    // （刻意驗 dd 而不是整段 textContent——文案裡的破折號是標點，不是佔位符。）
    const values = Array.from(block.querySelectorAll(".leader-perf-stat dd"))
      .map((n) => n.textContent);
    expect(values).toEqual(["820.10", "7.1%", "3.0%"]);
    expect(values).not.toContain(NO_VALUE);
  });

  it("⭐⭐ 誠信 1：不足 30 天（無 twr／max_drawdown 鍵）→ 兩個百分比欄位都不渲染", async () => {
    getLeaders.mockResolvedValue(leadersWithPerf({ perpMonth: PERF_PNL_ONLY }));
    render(wrap(<LeadersPage />));

    const block = await screen.findByLabelText("績效（perp）");
    expect(document.querySelectorAll(".leader-perf-stat-twr")).toHaveLength(0);
    expect(document.querySelectorAll(".leader-perf-stat-max-drawdown")).toHaveLength(0);
    // ⭐ 釘死欄位清單：只有「累積損益」一欄，沒有被補成空欄位。
    // （驗 dt 而不是全頁文字——說明「為什麼沒有報酬率」的那段本來就會提到這些詞。）
    const labels = Array.from(block.querySelectorAll(".leader-perf-stat dt"))
      .map((n) => n.textContent);
    expect(labels).toEqual(["累積損益"]);
    const values = Array.from(block.querySelectorAll(".leader-perf-stat dd"))
      .map((n) => n.textContent);
    expect(values).toEqual(["310.75"]);
    expect(values).not.toContain(NO_VALUE);
    // 不足 30 天 → 整區不得出現任何百分比
    expect(block.textContent).not.toContain("%");
  });

  it("⭐ 誠信 1／5：缺一級要說明「缺什麼、為什麼、還差幾天」——不是留白", async () => {
    getLeaders.mockResolvedValue(leadersWithPerf({ perpMonth: PERF_WINDOW }));
    render(wrap(<LeadersPage />));

    const gap = (await screen.findByText("尚未提供年化報酬率")).closest(".leader-perf-state")!;
    expect(gap.textContent).toMatch(/年化需要至少 90 天/);
    // ⭐ 不是「年化為零」——明說這是資料還不夠的狀態
    expect(gap.textContent).toMatch(/不是「年化為零」/);
    // 還差多少天：涵蓋 45.2 天 → 距 90 天還差 44.8 天
    expect(gap.textContent).toContain("距門檻還差 44.8 天");
  });

  it("⭐ 誠信 1／5：pnl_only 也要說明還差幾天才有報酬率與回撤", async () => {
    getLeaders.mockResolvedValue(leadersWithPerf({ perpMonth: PERF_PNL_ONLY }));
    render(wrap(<LeadersPage />));

    const gap = (await screen.findByText("尚未提供報酬率與回撤")).closest(".leader-perf-state")!;
    expect(gap.textContent).toMatch(/需要至少 30 天/);
    expect(gap.textContent).toMatch(/不是「報酬為零」或「沒有回撤」/);
    expect(gap.textContent).toContain("距門檻還差 23.9 天");   // 30 - 6.1
  });

  it("⭐ 誠信 2：上界警語在績效區塊內（與績效數字同一個容器，不是頁尾小字）", async () => {
    getLeaders.mockResolvedValue(leadersWithPerf({ perpAllTime: PERF_ANNUALIZABLE }));
    render(wrap(<LeadersPage />));

    const block = await screen.findByLabelText("績效（perp）");
    const warn = block.querySelector(".leader-perf-upper-bound")!;
    expect(warn).toBeTruthy();
    // 後端原文優先（單一來源）
    expect(warn.textContent).toContain("上界，不是期望值");
    // 警語與數字在同一個容器裡
    expect(block.textContent).toContain("34.2%");
  });

  it("⭐ 誠信 2：後端沒給上界原文 → 用前端等義文案遞補（數字旁不得沒有警語）", async () => {
    getLeaders.mockResolvedValue(
      leadersWithPerf({ perpAllTime: PERF_ANNUALIZABLE }, { performance_notes: {} }),
    );
    render(wrap(<LeadersPage />));

    const block = await screen.findByLabelText("績效（perp）");
    const warn = block.querySelector(".leader-perf-upper-bound")!;
    expect(warn.textContent).toMatch(/是跟單者的上界，不是你的期望值/);
    expect(warn.textContent).toMatch(/你的實際結果會低於這裡的數字/);
  });

  it("⭐ 誠信 3：MDD 取樣低估的警語與 MDD 數字在**同一格**（不得被拆開）", async () => {
    getLeaders.mockResolvedValue(leadersWithPerf({ perpMonth: PERF_WINDOW }));
    render(wrap(<LeadersPage />));

    await screen.findByLabelText("績效（perp）");
    const cell = document.querySelector(".leader-perf-stat-max-drawdown")!;
    expect(cell.textContent).toContain("3.0%");             // 數字
    expect(cell.textContent).toContain("15 分鐘取樣");       // 警語，同一格
    expect(cell.textContent).toContain("系統性低估");
  });

  it("⭐ 誠信 3：後端沒給回撤原文 → 前端文案遞補，且點名「回撤看起來小」要存疑", async () => {
    getLeaders.mockResolvedValue(
      leadersWithPerf({ perpMonth: PERF_WINDOW }, { performance_notes: {} }),
    );
    render(wrap(<LeadersPage />));

    await screen.findByLabelText("績效（perp）");
    const cell = document.querySelector(".leader-perf-stat-max-drawdown")!;
    expect(cell.textContent).toMatch(/系統性低估/);
    expect(cell.textContent).toMatch(/回撤的下界/);
    expect(cell.textContent).toMatch(/回撤看起來特別小的 leader 尤其要存疑/);
  });

  it("⭐ 誠信 4：資料充足度（涵蓋天數＋樣本點數）與數字同時顯示", async () => {
    getLeaders.mockResolvedValue(leadersWithPerf({ perpMonth: PERF_WINDOW }));
    render(wrap(<LeadersPage />));

    await screen.findByLabelText("績效（perp）");
    const suff = document.querySelector(".leader-perf-sufficiency")!;
    expect(suff.textContent).toContain("涵蓋天數: 45.2 天");
    expect(suff.textContent).toContain("樣本點數: 128 點");
    // 為什麼要看它：涵蓋 31 天與涵蓋兩年的報酬率長得一樣
    const note = document.querySelector(".leader-perf-suff-note")!;
    expect(note.textContent).toBeTruthy();
  });

  it("⭐ 誠信 4：涵蓋天數不同、數字相同的兩個窗，充足度必須看得出差別", async () => {
    getLeaders.mockResolvedValue(
      leadersWithPerf({ perpMonth: PERF_WINDOW, perpAllTime: PERF_ANNUALIZABLE }),
    );
    render(wrap(<LeadersPage />));

    await screen.findByLabelText("績效（perp）");
    const suffs = Array.from(document.querySelectorAll(".leader-perf-sufficiency"))
      .map((n) => n.textContent);
    expect(suffs).toHaveLength(2);
    expect(suffs[0]).toContain("45.2 天");
    expect(suffs[1]).toContain("412.5 天");
  });

  it("⭐ 誠信 5：每個窗都有揭露層級標籤——「資料不足」是一個狀態，不是幾格空白", async () => {
    getLeaders.mockResolvedValue(
      leadersWithPerf({ perpMonth: PERF_PNL_ONLY, perpAllTime: PERF_ANNUALIZABLE }),
    );
    render(wrap(<LeadersPage />));

    await screen.findByLabelText("績效（perp）");
    const tiers = Array.from(document.querySelectorAll(".leader-perf-tier"))
      .map((n) => n.textContent);
    // 兩個窗的層級**不同**，且各自說出資料到什麼程度
    expect(tiers[0]).toBe("僅累積損益：涵蓋不足 30 天，不提供任何百分比");
    expect(tiers[1]).toBe("可年化：涵蓋 90 天以上");
    // data-tier 讓兩種狀態在樣式上也分得開（不是同一個樣子少幾個數字）
    const windows = Array.from(document.querySelectorAll(".leader-perf-window"));
    expect(windows.map((n) => n.getAttribute("data-tier")))
      .toEqual(["pnl_only", "annualizable"]);
  });

  it("⭐ 誠信 5／6：tier=insufficient → 該窗零數字指標，畫成「資料不足」狀態並附原因碼", async () => {
    getLeaders.mockResolvedValue(leadersWithPerf({ perpMonth: PERF_INSUFFICIENT }));
    render(wrap(<LeadersPage />));

    await screen.findByLabelText("績效（perp）");
    // 一格指標都不畫
    expect(document.querySelectorAll(".leader-perf-stat")).toHaveLength(0);
    const state = (await screen.findByText("資料不足以計算此窗的指標"))
      .closest(".leader-perf-state")!;
    expect(state.textContent).toMatch(/連累積損益都不顯示/);
    // 明說「0」會是資料不足的假象，不是結論
    expect(state.textContent).toMatch(/資料不足的假象/);
    expect(state.textContent).toContain("原因碼: need_at_least_two_samples");
  });

  it("⭐ 誠信 6：performance_available=false → 顯示原因，且該區塊零數字、零績效欄位", async () => {
    getLeaders.mockResolvedValue(
      leadersWithPerf(null, { performance_available: false, performance_notes: undefined }),
    );
    render(wrap(<LeadersPage />));

    const notice = await screen.findByText(/績效資料暫時不可用/);
    const block = notice.closest(".leader-perf-notice")!;
    expect(block.textContent).not.toMatch(/\d/);
    // 一個績效欄位都不畫（沿 stats_available=false 的嚴格度）
    expect(document.querySelectorAll(".leader-perf-stat")).toHaveLength(0);
    expect(document.querySelectorAll(".leader-perf-window")).toHaveLength(0);
    // 規模欄位不受影響（兩個旗標獨立，後端明寫不可合併）
    expect(document.querySelectorAll(".leader-stat")).toHaveLength(4);
  });

  it("⭐ 誠信 6：leader 缺 performance → 說明沒有資料，不畫任何績效數字", async () => {
    getLeaders.mockResolvedValue(leadersWithPerf(null));
    render(wrap(<LeadersPage />));

    const state = (await screen.findByText("這位 leader 沒有績效資料"))
      .closest(".leader-perf-state")!;
    expect(state.textContent).toMatch(/不代表「績效為零」/);
    expect(document.querySelectorAll(".leader-perf-stat")).toHaveLength(0);
    // 規模照畫
    expect(document.querySelectorAll(".leader-stat")).toHaveLength(4);
  });

  it("⭐ 後端宣稱的層級高於實際資料（schema 漂移）→ 出聲，不靜默降級", async () => {
    // tier 說 annualizable，但 annualized_return 鍵不存在
    const { annualized_return: _drop, ...drifted } = PERF_ANNUALIZABLE;
    getLeaders.mockResolvedValue(leadersWithPerf({ perpAllTime: drifted }));
    render(wrap(<LeadersPage />));

    await screen.findByLabelText("績效（perp）");
    expect(screen.queryByText("年化報酬率")).not.toBeInTheDocument();
    expect(screen.getByText(/後端宣告的揭露層級高於實際附上的資料/)).toBeInTheDocument();
  });

  it("⭐ 規模／曝險與績效是兩個分開的容器（混在一起會被讀成同一類數字）", async () => {
    getLeaders.mockResolvedValue(leadersWithPerf({ perpAllTime: PERF_ANNUALIZABLE }));
    render(wrap(<LeadersPage />));

    const perf = await screen.findByLabelText("績效（perp）");
    const stats = document.querySelector(".leader-stats")!;
    // 兩個容器互不包含
    expect(perf.contains(stats)).toBe(false);
    expect(stats.contains(perf)).toBe(false);
    // 規模那張表仍然只有原本四欄（沒有被塞進績效欄位）
    const labels = Array.from(stats.querySelectorAll("dt")).map((n) => n.textContent);
    expect(labels).toEqual(["帳戶淨值", "名目部位總額", "未實現損益", "持倉數"]);
    // 揭露文案改成描述「畫面上實際有什麼」（顯示績效時不得再宣稱本頁沒有報酬率）
    expect(screen.getByText(/規模／曝險與績效是兩類不同的數字/)).toBeInTheDocument();
    expect(screen.queryByText(/沒有報酬率、沒有回撤、沒有勝率/)).not.toBeInTheDocument();
  });

  it("⭐ 沒有時點 → 連績效也不畫（同一份快照，沒有時點的報酬率一樣會被當成即時值）", async () => {
    getLeaders.mockResolvedValue(
      leadersWithPerf({ perpAllTime: PERF_ANNUALIZABLE }, { stats_day: null, stats_as_of: null }),
    );
    render(wrap(<LeadersPage />));

    expect(await screen.findByText(/統計缺少時點，本頁不顯示任何數字/)).toBeInTheDocument();
    expect(document.querySelectorAll(".leader-perf-stat")).toHaveLength(0);
    expect(document.querySelectorAll(".leader-stat")).toHaveLength(0);
  });

  it("⭐ 不依績效排序：清單順序沿用後端順序（排序＝本頁在替使用者推薦）", async () => {
    const base = leaders();
    getLeaders.mockResolvedValue({
      ...base,
      performance_available: true,
      performance_windows: ["perpAllTime"],
      performance_notes: PERF_NOTES,
      leaders: [
        // 後端給的第一位績效較差，第二位較好——畫面**不得**因此把第二位排到前面
        { ...base.leaders[0], name: "Alpha", performance: { perpAllTime: PERF_WINDOW } },
        {
          ...base.leaders[0], address: "0x3333333333333333333333333333333333333333",
          name: "Zeta", performance: { perpAllTime: PERF_ANNUALIZABLE },
        },
      ],
    } as LeadersResp);
    render(wrap(<LeadersPage />));

    await screen.findByText("Zeta");
    const names = Array.from(document.querySelectorAll(".leader-name")).map((n) => n.textContent);
    expect(names).toEqual(["Alpha", "Zeta"]);
    // 也不得出現任何績效排序控制項
    expect(screen.queryByRole("button", { name: /排序/ })).not.toBeInTheDocument();
  });

  it("後端沒有 performance_available 欄位（舊版）→ fail closed：整區不出現，零績效數字", async () => {
    render(wrap(<LeadersPage />));   // 預設 fixture 沒有績效欄位

    await screen.findByText("Alpha");
    expect(document.querySelectorAll(".leader-perf")).toHaveLength(0);
    expect(document.querySelectorAll(".leader-perf-stat")).toHaveLength(0);
    // 舊版文案（本頁確實沒有報酬率）仍為真
    expect(screen.getByText(/沒有報酬率、沒有回撤、沒有勝率/)).toBeInTheDocument();
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
