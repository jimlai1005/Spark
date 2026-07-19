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
 * `sample_count`／`skipped_intervals`／`*_ts_ms` 是 number，
 * 三個 `*_insufficient_data` 是**原生 bool**（後端刻意不轉字串）。
 *
 * ⚠️ 2026-07-19 揭露模型改版：資料不足**不再**由缺鍵承載，而是由標記承載，
 * 所以 fixture 一律**同時**帶數字與標記——這才是線上真正的形狀。
 */
function win(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    period: "perpAllTime", basis: "perp", status: "ok", reason: null,
    disclosure_tier: "annualizable",
    sample_count: 745, covered_days: "412.5000",
    first_ts_ms: 1700000000000, last_ts_ms: 1735640000000, skipped_intervals: 0,
    cum_pnl: "18234.55",
    twr: "0.3421", twr_insufficient_data: false,
    max_drawdown: "0.1875", max_drawdown_insufficient_data: false,
    annualized_return: "0.2938", annualized_return_insufficient_data: false,
    annualized_return_extrapolated_from_days: "412.5000",
    ...over,
  };
}

/** 完整的 shown（供 `toEqual` 用）：數字與標記成對，缺一不可。 */
const SHOWN_FULL = {
  cum_pnl: "18234.55",
  twr: "0.3421", twr_insufficient_data: false,
  max_drawdown: "0.1875", max_drawdown_insufficient_data: false,
  annualized_return: "0.2938", annualized_return_insufficient_data: false,
  annualized_return_extrapolated_from_days: "412.5000",
};

/** 整組年化欄位（三鍵同生共死）→ 一起拿掉，模擬「年化在數學上無定義」。 */
function dropAnnualGroup(w: Record<string, unknown>): Record<string, unknown> {
  const {
    annualized_return: _a,
    annualized_return_insufficient_data: _b,
    annualized_return_extrapolated_from_days: _c,
    ...rest
  } = w;
  return rest;
}

/** 整組窗口報酬欄位 → 一起拿掉（舊快照／窗太短算不出來）。 */
function dropWindowGroup(w: Record<string, unknown>): Record<string, unknown> {
  const {
    twr: _t, twr_insufficient_data: _ti,
    max_drawdown: _m, max_drawdown_insufficient_data: _mi,
    ...rest
  } = w;
  return rest;
}

describe("perfView — 揭露：數字與標記必須成對到齊", () => {
  it("四個數字 ＋ 標記齊全 → 全部可顯示", () => {
    const v = perfView(win());
    expect(v.level).toBe("annualized");
    expect(v.degraded).toBe(false);
    expect(v.shown).toEqual(SHOWN_FULL);
    // 型別實測：Decimal → string，樣本數是 number，標記是原生 bool
    expect(typeof v.shown.twr).toBe("string");
    expect(typeof v.shown.twr_insufficient_data).toBe("boolean");
    expect(v.coveredDays).toBe(412.5);
    expect(v.sampleCount).toBe(745);
  });

  it("⭐ 年化整組缺席（數學上無定義）→ 年化「鍵不存在」，且不算降級", () => {
    const rest = dropAnnualGroup(
      win({ disclosure_tier: "window_return", covered_days: "45.2000" }));
    const v = perfView(rest);
    expect(v.level).toBe("window");
    // 三鍵一起缺席是後端的合法狀態（1+TWR <= 0），不是 schema 漂移
    expect(v.degraded).toBe(false);
    // ⭐ 不是 null、不是 "—"、不是 0——是這個鍵**根本不存在**
    expect("annualized_return" in v.shown).toBe(false);
    // ⭐ 標記也絕不單獨存在：不得畫出「由 N 天外推」卻沒有被外推的數字
    expect("annualized_return_insufficient_data" in v.shown).toBe(false);
    expect("annualized_return_extrapolated_from_days" in v.shown).toBe(false);
    expect(v.shown.twr).toBe("0.3421");
  });

  it("⭐ 窗口報酬與年化都整組缺席（舊快照）→ 只剩累積損益，不算降級", () => {
    const rest = dropAnnualGroup(dropWindowGroup(
      win({ disclosure_tier: "pnl_only", covered_days: "6.1000", sample_count: 12 })));
    const v = perfView(rest);
    expect(v.level).toBe("pnl");
    expect(v.degraded).toBe(false);
    expect("twr" in v.shown).toBe(false);
    expect("max_drawdown" in v.shown).toBe(false);
    expect("annualized_return" in v.shown).toBe(false);
    expect(v.shown.cum_pnl).toBe("18234.55");
  });

  /**
   * ⭐⭐ 改版後的核心行為：資料很薄**照樣顯示數字**，標記負責說出它有多薄。
   * 舊行為是「不足 30 天就把 twr／MDD 藏起來」，那條規則已隨後端一起作廢。
   */
  it("⭐⭐ 薄資料（6 天）→ 數字照樣顯示，且標記為 true", () => {
    const v = perfView(win({
      disclosure_tier: "pnl_only", covered_days: "6.0000", sample_count: 12,
      twr_insufficient_data: true, max_drawdown_insufficient_data: true,
      annualized_return_insufficient_data: true,
      annualized_return_extrapolated_from_days: "6.0000",
      annualized_return: "3.6500",
    }));
    expect(v.level).toBe("annualized");
    expect(v.degraded).toBe(false);
    expect(v.shown.twr).toBe("0.3421");
    expect(v.shown.twr_insufficient_data).toBe(true);
    expect(v.shown.max_drawdown_insufficient_data).toBe(true);
    expect(v.shown.annualized_return).toBe("3.6500");
    expect(v.shown.annualized_return_insufficient_data).toBe(true);
    expect(v.shown.annualized_return_extrapolated_from_days).toBe("6.0000");
  });

  /**
   * ⭐⭐ fail closed 的頭號情境：數字在、標記缺席。
   * 標記缺席的語意是「我們不知道這段資料夠不夠」，不是「資料充足」——
   * 把它當成充足並照畫，等於替後端說了一句它沒說過的話。
   */
  it.each([
    ["twr", "twr_insufficient_data"],
    ["max_drawdown", "max_drawdown_insufficient_data"],
  ])("⭐⭐ %s 有值但標記缺席 → 不顯示（fail closed）＋ degraded", (metric, marker) => {
    const w = win();
    delete w[marker];
    const v = perfView(w);
    expect(metric in v.shown).toBe(false);
    // 一起給或一起不給：另一個也跟著不顯示
    expect("twr" in v.shown).toBe(false);
    expect("max_drawdown" in v.shown).toBe(false);
    expect(v.degraded).toBe(true);
  });

  it("⭐⭐ 標記是字串 \"false\" 而非 bool → 不顯示（字串 \"false\" 的真值是 true）", () => {
    const v = perfView(win({ twr_insufficient_data: "false" }));
    expect("twr" in v.shown).toBe(false);
    expect(v.degraded).toBe(true);
  });

  it("⭐⭐ 年化少了外推天數 → 整格不畫：說不出「由 N 天外推」就不該給那個數字", () => {
    const w = win();
    delete w.annualized_return_extrapolated_from_days;
    const v = perfView(w);
    expect(v.level).toBe("window");
    expect("annualized_return" in v.shown).toBe(false);
    expect(v.degraded).toBe(true);
  });

  it("⭐ 不變式：shown 裡有數字，就一定有它的標記（JSX 可以安心直接讀）", () => {
    for (const fixture of [win(), win({ twr_insufficient_data: true }),
      dropAnnualGroup(win()), dropWindowGroup(win())]) {
      const s = perfView(fixture).shown;
      if ("twr" in s) expect(typeof s.twr_insufficient_data).toBe("boolean");
      if ("max_drawdown" in s) {
        expect(typeof s.max_drawdown_insufficient_data).toBe("boolean");
      }
      if ("annualized_return" in s) {
        expect(typeof s.annualized_return_insufficient_data).toBe("boolean");
        expect(typeof s.annualized_return_extrapolated_from_days).toBe("string");
      }
    }
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

describe("perfView — 不成對就不放行（數字 ∩ 標記）", () => {
  it("⭐ 數字缺漏但標記還在（schema 漂移）→ 不顯示且 degraded=true，不無中生有", () => {
    const { annualized_return: _drop, ...rest } = win(); // 兩個年化標記仍在
    const v = perfView(rest);
    expect(v.level).toBe("window");
    expect("annualized_return" in v.shown).toBe(false);
    // ⭐ 靜默降級會讓資料缺漏在畫面上完全看不出來（工程原則 3：失敗要出聲）
    expect(v.degraded).toBe(true);
  });

  /**
   * ⚠️ 本測試 2026-07-19 **反轉**：原規則是「鍵在但 tier 不許 → 不顯示，
   * tier 才是揭露決策」。改版後 tier 只是 `covered_days` 的純函式，**不再**決定
   * 哪些鍵存在，因此拿 tier 去擋數字只會把薄資料的數字藏起來——而「藏起來」正是
   * 這次改版明確要停止的行為（使用者裁決：照樣顯示，但帶標記）。
   * tier 唯一保留的擋門作用是 rank 0（見下一個 describe 的 unknown tier 測試）。
   */
  it("⭐⭐ tier=pnl_only 但數字與標記齊全 → 照樣顯示（tier 不再是顯示授權）", () => {
    const v = perfView(win({
      disclosure_tier: "pnl_only", covered_days: "6.1000",
      twr_insufficient_data: true, max_drawdown_insufficient_data: true,
      annualized_return_insufficient_data: true,
      annualized_return_extrapolated_from_days: "6.1000",
    }));
    expect(v.level).toBe("annualized");
    expect(v.shown.twr).toBe("0.3421");
    expect(v.shown.max_drawdown).toBe("0.1875");
    expect(v.shown.annualized_return).toBe("0.2938");
    // 顯示了，但每一個都帶著「資料不足」的標記——警示沒有跟著消失
    expect(v.shown.twr_insufficient_data).toBe(true);
    expect(v.shown.max_drawdown_insufficient_data).toBe(true);
    expect(v.shown.annualized_return_insufficient_data).toBe(true);
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
