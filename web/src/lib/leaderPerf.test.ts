import { describe, expect, it } from "vitest";
import {
  MIN_DAYS_FOR_ANNUALIZATION,
  MIN_DAYS_FOR_RETURN,
  daysShortOf,
  perfView,
  resolvePerfNotes,
} from "./leaderPerf";

/**
 * fixture 依**後端實際線上形狀**：Decimal 欄位序列化後是 **string**
 * （filet/leader_perf.py `jsonable_performance`：`str(v) if isinstance(v, Decimal)`），
 * `sample_count`／`skipped_intervals`／`*_ts_ms` 才是 number。
 * ⭐ 分級揭露的載體是**鍵的不存在**（不是 null）：低層級的 fixture 一律**不寫**那些鍵。
 */
function win(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    period: "perpAllTime", basis: "perp", status: "ok", reason: null,
    disclosure_tier: "annualizable",
    sample_count: 745, covered_days: "412.5000",
    first_ts_ms: 1700000000000, last_ts_ms: 1735640000000, skipped_intervals: 0,
    cum_pnl: "18234.55", twr: "0.3421", max_drawdown: "0.1875",
    annualized_return: "0.2938",
    ...over,
  };
}

describe("perfView — 分級揭露：鍵存不存在決定能不能顯示", () => {
  it("annualizable（四個鍵齊全）→ 四個數字都可顯示", () => {
    const v = perfView(win());
    expect(v.level).toBe("annualized");
    expect(v.degraded).toBe(false);
    expect(v.shown).toEqual({
      cum_pnl: "18234.55", twr: "0.3421",
      max_drawdown: "0.1875", annualized_return: "0.2938",
    });
    // 型別實測：Decimal → string，樣本數才是 number
    expect(typeof v.shown.twr).toBe("string");
    expect(v.coveredDays).toBe(412.5);
    expect(v.sampleCount).toBe(745);
  });

  it("⭐ window_return（不足 90 天 → 沒有 annualized_return 鍵）→ 年化「鍵不存在」", () => {
    const { annualized_return: _drop, ...rest } = win({ disclosure_tier: "window_return", covered_days: "45.2000" });
    const v = perfView(rest);
    expect(v.level).toBe("window");
    expect(v.degraded).toBe(false);
    // ⭐ 不是 null、不是 "—"、不是 0——是這個鍵**根本不存在**
    expect("annualized_return" in v.shown).toBe(false);
    expect(v.shown.twr).toBe("0.3421");
  });

  it("⭐ pnl_only（不足 30 天 → 沒有 twr／max_drawdown 鍵）→ 兩個百分比都不存在", () => {
    const { twr: _t, max_drawdown: _m, annualized_return: _a, ...rest } =
      win({ disclosure_tier: "pnl_only", covered_days: "6.1000", sample_count: 12 });
    const v = perfView(rest);
    expect(v.level).toBe("pnl");
    expect(v.degraded).toBe(false);
    expect("twr" in v.shown).toBe(false);
    expect("max_drawdown" in v.shown).toBe(false);
    expect("annualized_return" in v.shown).toBe(false);
    expect(v.shown.cum_pnl).toBe("18234.55");
  });

  it("⭐ insufficient → 連 cum_pnl 都沒有；covered_days 為 null（後端 _insufficient 原形）", () => {
    const v = perfView({
      period: "perpMonth", basis: "perp", status: "insufficient",
      reason: "need_at_least_two_samples", disclosure_tier: "insufficient",
      sample_count: 1, covered_days: null, first_ts_ms: null, last_ts_ms: null,
      skipped_intervals: 0,
    });
    expect(v.level).toBe("none");
    expect(v.shown).toEqual({});
    expect(v.coveredDays).toBeNull();
    expect(v.reason).toBe("need_at_least_two_samples");
    // 1 個資料點的 cum_pnl 恆為 0，而畫面上的「0」是有意義且錯誤的訊息
    expect("cum_pnl" in v.shown).toBe(false);
  });
});

describe("perfView — 兩個方向都要擋（tier 授權 ∩ 資料具備）", () => {
  it("⭐ tier 宣稱 annualizable 但鍵缺漏（schema 漂移）→ 降級且 degraded=true，不無中生有", () => {
    const { annualized_return: _drop, ...rest } = win(); // tier 仍宣稱 annualizable
    const v = perfView(rest);
    expect(v.level).toBe("window");
    expect("annualized_return" in v.shown).toBe(false);
    // ⭐ 靜默降級會讓資料缺漏在畫面上完全看不出來（工程原則 3：失敗要出聲）
    expect(v.degraded).toBe(true);
  });

  it("⭐ 鍵在但 tier 不許（pnl_only 夾帶 twr）→ 不顯示：tier 才是揭露決策", () => {
    const v = perfView(win({ disclosure_tier: "pnl_only" }));
    expect(v.level).toBe("pnl");
    expect("twr" in v.shown).toBe(false);
    expect("max_drawdown" in v.shown).toBe(false);
    expect("annualized_return" in v.shown).toBe(false);
  });

  it("⭐ twr 與 max_drawdown 只有其中一個 → 兩個都不顯示", () => {
    // 「有報酬率、沒有回撤」是最容易被讀成「這個 leader 沒有回撤」的形狀
    const { max_drawdown: _m, ...rest } = win({ disclosure_tier: "window_return" });
    const v = perfView(rest);
    expect(v.level).toBe("pnl");
    expect("twr" in v.shown).toBe(false);
    expect(v.degraded).toBe(true);
  });

  it("未知 tier → fail closed（什麼都不顯示），不猜一個層級", () => {
    const v = perfView(win({ disclosure_tier: "some_future_tier" }));
    expect(v.tier).toBe("unknown");
    expect(v.level).toBe("none");
    expect(v.shown).toEqual({});
  });

  it.each([["空字串", ""], ["非數字", "abc"], ["NaN", "NaN"], ["null", null], ["number 型別", 0.34]])(
    "髒的 twr（%s）→ 不顯示，不硬轉成數字",
    (_label, bad) => {
      const v = perfView(win({ disclosure_tier: "window_return", twr: bad }));
      expect("twr" in v.shown).toBe(false);
      expect(v.degraded).toBe(true);
    },
  );

  it("undefined／非物件（窗缺席、回應形狀不符）→ level none，不讀 undefined 的屬性", () => {
    for (const bad of [undefined, null, "x", 42, []]) {
      const v = perfView(bad);
      expect(v.level).toBe("none");
      expect(v.shown).toEqual({});
    }
  });
});

describe("daysShortOf — 把「這格為什麼空著」變成有內容的狀態", () => {
  it("未達門檻 → 還差幾天（一位小數）", () => {
    expect(daysShortOf(45.2, MIN_DAYS_FOR_ANNUALIZATION)).toBe(44.8);
    expect(daysShortOf(6.1, MIN_DAYS_FOR_RETURN)).toBe(23.9);
  });

  it("已達門檻或天數未知 → null（不編一個天數）", () => {
    expect(daysShortOf(120, MIN_DAYS_FOR_ANNUALIZATION)).toBeNull();
    expect(daysShortOf(90, MIN_DAYS_FOR_ANNUALIZATION)).toBeNull();
    expect(daysShortOf(null, MIN_DAYS_FOR_ANNUALIZATION)).toBeNull();
  });

  it("門檻與後端 leader_perf.py 同源（改一邊要改兩邊）", () => {
    expect(MIN_DAYS_FOR_RETURN).toBe(30);
    expect(MIN_DAYS_FOR_ANNUALIZATION).toBe(90);
  });
});

describe("resolvePerfNotes — ⭐ 警語與數字的規則相反：數字缺了不顯示，警語缺了要補上", () => {
  const FB = {
    basis: "fb-basis", upperBound: "fb-upper",
    maxDrawdown: "fb-mdd", sufficiency: "fb-suff",
  };

  it("後端有原文 → 用後端的（單一來源：極限與警語出自同一處）", () => {
    const r = resolvePerfNotes(
      { basis: "B", upper_bound: "U", max_drawdown: "M", sufficiency: "S" }, FB);
    expect(r).toEqual({ basis: "B", upperBound: "U", maxDrawdown: "M", sufficiency: "S" });
  });

  it("後端整包缺席 → 全部用前端等義文案（不得讓數字旁邊沒有警語）", () => {
    expect(resolvePerfNotes(undefined, FB)).toEqual(FB);
  });

  it("⭐ 後端送空字串／純空白 → 一樣視為缺席（`??` 擋不掉這個，會留下一則空警語）", () => {
    const r = resolvePerfNotes(
      { basis: "", upper_bound: "   ", max_drawdown: "\n", sufficiency: "" }, FB);
    expect(r).toEqual(FB);
  });
});
