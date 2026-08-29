import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createElement } from "react";
import { describe, expect, it } from "vitest";
import { COPY_ZH as COPY, COPY_EN, COPY_ZH } from "./copy";
import { LEGAL_EN } from "@/content/legal";
import { LangProvider, useCopy, useLang } from "./lang";

function allStrings(node: unknown, acc: string[] = []): string[] {
  if (typeof node === "string") acc.push(node);
  else if (node && typeof node === "object") {
    for (const v of Object.values(node)) allStrings(v, acc);
  }
  return acc;
}

/** 遞迴取出物件（含陣列元素）的 key set，路徑用 "." 串接，陣列索引用 "[n]" 表示。 */
function deepKeySet(node: unknown, prefix = "", acc: Set<string> = new Set()): Set<string> {
  if (Array.isArray(node)) {
    node.forEach((v, i) => deepKeySet(v, `${prefix}[${i}]`, acc));
  } else if (node && typeof node === "object") {
    for (const [k, v] of Object.entries(node)) {
      const path = prefix ? `${prefix}.${k}` : k;
      acc.add(path);
      deepKeySet(v, path, acc);
    }
  }
  return acc;
}

const CJK_RE = /[一-鿿]/;

describe("語言紅線（spec 不變量 4：2026-06-18 沿用）", () => {
  it("全部文案禁詞零命中：固定收益/保證/存款/代操", () => {
    // ⭐ 2026-08-28（主線程裁決，Task 14 收尾）：「保證」原本是純 substring 禁詞，
    // 意圖是擋**承諾語**（保證收益／保證獲利／保證回本這類對報酬的保證），但
    // substring 比對連帶擋下「保證金」——那是 HL/期貨語境的標準中文詞（margin），
    // 不是承諾語，Dashboard §06（設計稿權威文本）明文要求這個詞（帳戶淨值與可用
    // 保證金等）。改成 `保證(?!金)`：「保證」後面接「金」豁免，其餘「保證 X」
    // （保證收益、保證獲利、保證回本…）維持零容忍。
    // 殘餘風險（已知、接受）：「保證金額」這類「保證」與「金」之間隔著其他字的
    // 片語不會被這條 lookahead 攔到——目前 repo 內沒有這種寫法，日後新增此類文案
    // 靠 review 把關，不靠這條規則機械擋下。
    const banned: RegExp[] = [/固定收益/, /保證(?!金)/, /存款/, /代操/];
    for (const s of allStrings(COPY)) {
      for (const b of banned) {
        expect(s, `文案含禁詞「${b}」: ${s}`).not.toMatch(b);
      }
    }
  });

  it("反釣魚聲明存在（紅線 1）", () => {
    const joined = allStrings(COPY).join("\n");
    expect(joined).toContain("永遠不會請你輸入私鑰或助記詞");
  });

  it("非託管核心句存在（無法動用或提領）", () => {
    const joined = allStrings(COPY).join("\n");
    expect(joined).toContain("無法動用或提領");
  });

  it("資金轉出警示存在於 wizard 與跟單頁文案", () => {
    expect(COPY.wizard.fundsWarning).toMatch(/perp/);
    expect(COPY.wizard.fundsWarning).toMatch(/轉出/);
    // ⭐ 2026-07-30：/performance 頁下架，此警語搬到 /leaders（客戶查看與管理
    // 跟單狀態的地方），與 wizard 開通頁的同義句各自成立、互不取代。
    // ⭐ Task 11（2026-08-28）：/leaders 遷移為 /advanced，警語隨遷移。
    expect(COPY.advanced.fundsWarning).toMatch(/轉出/);
  });
});

describe("i18n（Task 2：雙語 copy 基礎）", () => {
  it("zh/en 深層 key 完全對稱", () => {
    const zhKeys = deepKeySet(COPY_ZH);
    const enKeys = deepKeySet(COPY_EN);
    const onlyInZh = [...zhKeys].filter((k) => !enKeys.has(k));
    const onlyInEn = [...enKeys].filter((k) => !zhKeys.has(k));
    expect(onlyInZh, `僅存在於 zh 的 key: ${onlyInZh.join(", ")}`).toEqual([]);
    expect(onlyInEn, `僅存在於 en 的 key: ${onlyInEn.join(", ")}`).toEqual([]);
  });

  it("en 值不含 CJK 字元", () => {
    const offenders = allStrings(COPY_EN).filter((s) => CJK_RE.test(s));
    expect(offenders, `含 CJK 字元的 en 文案: ${offenders.join(" | ")}`).toEqual([]);
  });

  it("useCopy 在 setLang(\"en\") 後回傳英文字典", async () => {
    // 檔案為 .ts（非 .tsx），改用 React.createElement 避免 JSX 需要 .tsx 副檔名。
    function Probe() {
      const c = useCopy();
      const { setLang } = useLang();
      return createElement(
        "div",
        null,
        createElement("span", { "data-testid": "appName" }, c.common.appName),
        createElement("span", { "data-testid": "next" }, c.common.next),
        createElement("button", { onClick: () => setLang("en") }, "switch"),
      );
    }
    const user = userEvent.setup();
    render(createElement(LangProvider, null, createElement(Probe)));
    // 首繪一律 zh（SSR 安全）
    expect(screen.getByTestId("next").textContent).toBe(COPY_ZH.common.next);
    await user.click(screen.getByText("switch"));
    expect(screen.getByTestId("next").textContent).toBe(COPY_EN.common.next);
    expect(screen.getByTestId("appName").textContent).toBe(COPY_EN.common.appName);
  });
});

describe("語言紅線（S4：opus 審查——英文承諾語禁詞掃描）", () => {
  // 中文禁詞掃描（上面第一個 describe）只掃 `COPY`（=COPY_ZH）；en 字典與法務
  // en 全文從未被同一道閘檢查過。清單刻意窄（只挑「對報酬做保證」這一類最容易
  // 出現在英文行銷語氣裡的承諾語），不是要重建中文那份禁詞表的英文對照——
  // 中文禁詞表對應的是中文語境特有的用詞（存款/代操等），英文語境的對應風險
  // 主要是「guaranteed return/profit」「risk-free」「fixed income」這幾類。
  // `risk-?free(?! rate)`：豁免 "risk-free rate"（Sharpe ratio 計算慣例用語的
  // 標準金融詞彙，不是對本產品的承諾語），沿 COPY_ZH 那組「保證(?!金)」同一個
  // 「詞彙有正當技術語境例外」的既有模式。
  const englishBanned: RegExp[] = [
    /guaranteed (return|profit|yield)/i,
    /risk-?free(?! rate)/i,
    /fixed income/i,
  ];

  it("COPY_EN 全部文案禁詞零命中", () => {
    for (const s of allStrings(COPY_EN)) {
      for (const b of englishBanned) {
        expect(s, `en 文案含禁詞「${b}」: ${s}`).not.toMatch(b);
      }
    }
  });

  it("法務 en 全文禁詞零命中", () => {
    for (const s of allStrings(LEGAL_EN)) {
      for (const b of englishBanned) {
        expect(s, `法務 en 文案含禁詞「${b}」: ${s}`).not.toMatch(b);
      }
    }
  });
});
