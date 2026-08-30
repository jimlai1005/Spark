/**
 * lib/strategyMetrics.ts — 策略／交易員詳情頁共用的展示層 helper（M3 round2
 * Task 6 從 `strategies/[slug]/page.tsx` 抽出；M3 round3 Task 7 移除 CAGR 自算；
 * M3 round4 Task R4-2 移除 `computeStartEndEquity` client 端推算；R4-8 又改回
 * client 端推算，但改用 `equity_index` 而非 `accountValueHistory`——見下）。
 *
 * ⭐ M3 round3 Task 7（D5 數字一致性）：`computeCagrPct` 已刪除——CAGR 現在
 * 一律由後端 `PublicStrategyDetail.cagr_pct` 直接供給（`sample_days<30`——
 * 2026-08-30 D15 裁決原 60 降為 30——時該鍵整個不存在，結構性防呆），前端不再用
 * `total_return_pct`／`live_days` 外推年化，避免與後端 `annualized_return` 各自
 * 維護一份「365 日慣例」公式、遲早漂移（工程原則 1：同一個值只能有一個計算來源）。
 *
 * ⭐ M3 round4 Task R4-8（2026-08-31 使用者裁決）：起訖淨值卡改回與淨值曲線
 * （`EquityCurve`）**同一個基準**——`methodology.start_equity_usd`／
 * `end_equity_usd`（`accountValueHistory` 首個非零值／末值）與曲線畫的
 * `equity_index` 是兩個不同源的數字，卡片與圖表各說各話。`equity_index` 由
 * `filet.leader_perf` 建構、首點依函式內定義**恆為 `Decimal("1")`**（出入金
 * 中性化的 TWR 指數，見 `leader_perf.py:329`），不是 R4-2 之前那個「真實帳戶
 * 首點常態性是 0」的 `accountValueHistory`——用它當比值分母不會重蹈 R4-2 移除
 * 前的樣本不足誤判，故本次改動不衝突。`formatDepositEquivalentEquity` 算的是
 * 「若入金 `deposit` 全程按 `equity_index` 報酬率複利，現在值多少」（TWR 等效
 * 淨值，不含實際出入金時點的影響）。`start_equity_usd`／`end_equity_usd` 欄位
 * 後端保留（其他地方可能還用得到），本卡不再讀取。
 */
import { NO_VALUE } from "@/lib/format";

/** insufficient → 佔位符；否則附尾綴（例如 %）。與 StrategyCard 的 metricText 同形狀。 */
export function metricText(value: string | null, insufficient: boolean, suffix = ""): string {
  if (insufficient || value == null) return NO_VALUE;
  return `${value}${suffix}`;
}

/**
 * ⭐ M3 round4 Task R4-11 項目 3：指標卡的共用形狀——`strategies/[slug]` 與
 * `traders/[address]` 兩頁的 `metricCards` 陣列字面量結構完全相同（沿既有
 * 重複慣例，見兩檔各自的 `headlineCards`/`collapsibleCards`），這裡只抽出
 * 型別，不抽出陣列本身（陣列內容依賴各頁各自的 `strategy`/`trader` state）。
 *
 * 雙值卡（最佳/最差日「A / B」、起訖淨值「A → B」）用 `pair` 而非單一
 * `value` 字串——讓 CSS 能在窄寬把 A／B 拆成刻意的兩行對齊，不靠瀏覽器隨機
 * 折行（見 `globals.css` `.metric-card-pair`）。單值卡維持 `value` 字串。
 */
export interface MetricCardDef {
  key: string;
  label: string;
  insufficient: boolean;
  note: string;
  value?: string;
  pair?: { a: string; sep: string; b: string };
}

/**
 * 起訖淨值（USD，TWR 等效淨值）：`start = deposit`、
 * `end = deposit × (equity_index 末值 / 首值)`——與 `EquityCurve` 的美元換算
 * 同一條公式（`depositNum * (v / values[0])`，見該元件檔頭）。
 *
 * `equity_index` 首值依後端建構恆為 `"1"`，仍防禦性除以首值而非硬編 1（首值若
 * 因未來格式變動不是 1，比值仍要對，見上方檔頭）。
 *
 * 缺席條件（→ `null`，卡片顯示「—」）：`deposit` 缺席／非有限／`<=0`；
 * `equity_index` 少於 2 點；首值非有限或 `<=0`。
 */
export function formatDepositEquivalentEquity(
  initialDepositUsd: string | null,
  equityIndex: string[],
  fmtAmount: (v: string, decimals?: number) => string,
): { start: string; end: string } | null {
  const deposit = initialDepositUsd == null ? null : Number(initialDepositUsd);
  if (deposit == null || !Number.isFinite(deposit) || deposit <= 0) return null;
  if (equityIndex.length < 2) return null;
  const first = Number(equityIndex[0]);
  const last = Number(equityIndex[equityIndex.length - 1]);
  if (!Number.isFinite(first) || first <= 0 || !Number.isFinite(last)) return null;
  const end = deposit * (last / first);
  return {
    start: fmtAmount(String(deposit), 0),
    end: fmtAmount(String(end), 0),
  };
}
