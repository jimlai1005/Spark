# 拿掉 Dashboard 保證金警示文案（保留數字變色）

> **狀態：已部署** commit `c1a8a37`，2026-09-29 11:41 UTC，只重啟 `filet-dashboard`；回歸 67/67。記錄見 `deploy/RUNBOOK.md` 附錄 B。

日期：2026-09-29。使用者裁決（選項 C）：跟單用戶的可用保證金由 leader 槓桿與投入比例決定，
用戶自己幾乎無法操作；「可用保證金嚴重不足…請儘速入金」這類文案嚇人多於有用。
拿掉**所有叫用戶入金的文案與紅／黃框**，只保留可用保證金**數字**在低水位時的黃／紅色。

## 目標狀態

| 元素 | 現況 | 目標 |
|---|---|---|
| EquityCard 黃框文案 `lowMarginWarning`（<5%） | 顯示 | **移除** |
| EquityCard 紅框文案 `criticalMarginWarning`（<2%） | 顯示 | **移除** |
| EquityCard 整張卡外框變色 `data-margin` | 黃／紅 | **移除**（屬性與 CSS 一併刪） |
| 可用保證金數字顏色（`var(--warn)` / `var(--neg)`） | 黃／紅 | **保留** |
| Header 「保證金偏低」pill `marginAlertPill` | 顯示 | **移除**（含 CSS `.header-margin-alert`） |
| 門檻常數 `LOW_MARGIN_THRESHOLD`=0.05 / `CRITICAL_MARGIN_THRESHOLD`=0.02 | export | 保留（數字變色仍用） |

不動：進階頁的 leader 揭露文案、出入金 hint、全平倉確認框、任何後端。

## Task 1 @inline — 前端移除（一次完成，含測試）

檔案：
- `web/src/lib/copy.ts`
  - 刪 `nav.marginAlertPill`（ZH 約 L91-97 含 docblock；EN 約 L1968）。
  - 刪 `dashboard.equity.lowMarginWarning` 與 `criticalMarginWarning`（ZH 約 L1206-1211 含 ⭐ 註解；EN 約 L2909-2914）。
  - COPY_ZH / COPY_EN 結構必須維持對稱（copy.test.ts 有結構測試）。
- `web/src/components/dashboard/EquityCard.tsx`
  - 刪 `marginLevel`、卡片的 `data-margin` 屬性、`{lowMargin && (<div className="dash-low-margin-card" …>)}` 整段。
  - 保留 `lowMargin`／`criticalMargin` 判斷與 L72-80 數字變色。
  - 檔頭 docblock 改寫：說明只剩數字變色、門檻常數保留給 Header 已不再需要 → 若 Header 不再 import，註解改為「僅本檔使用」。
- `web/src/components/Header.tsx`
  - 刪 `LOW_MARGIN_THRESHOLD` import（L9）、L164-173 的 `availablePctNum`／`showMarginAlert` 與 docblock、L230-234 的 pill。
  - `dash` query 仍被跟單狀態 pill 使用，**不要**刪 query。
- `web/src/styles/globals.css`
  - 刪 `.header-margin-alert`（L195 起的規則）、`.dash-low-margin-card`、`.dash-low-margin-card[data-level="critical"]`、`.dash-card-equity[data-margin=…]` 兩條（L1512-1516）。
- 測試：
  - `web/src/components/dashboard/EquityCard.test.tsx`：改寫「保證金分級樣式」describe → 三個 case 各斷言：無 `data-margin` 屬性、無 `.dash-low-margin-card`、可用保證金數字的 inline `color` 為 `var(--warn)`（0.049）／`var(--neg)`（0.019）／無 color（0.051）。邊界 case（0.02 → warn 色非 neg 色）保留。門檻常數測試保留。
  - `web/src/components/Header.test.tsx`：刪整個「保證金告警 pill」describe（L229-249）；若 `dashboardWithMargin` helper 只被它用，一併刪。
  - `web/src/app/dashboard/page.test.tsx`：刪「available_pct 0.05 低保證金告警閾值翻轉」describe（L191-208）。

驗收（逐條要證據）：
1. `export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH" && cd web && npm test` 全綠，貼輸出末尾。
2. `grep -rn -E 'marginAlertPill|lowMarginWarning|criticalMarginWarning|dash-low-margin-card|header-margin-alert|data-margin' web/src | grep -v '\.test\.'` 為 0 命中。
   測試檔裡對這些 class／屬性的**否定式斷言是刻意保留的回歸守門**，不得為了讓 grep 歸零而刪。
   <!-- 2026-09-29 reviewer：原條件掃到測試檔會永遠命中，改為排除測試檔 -->
3. `cd web && npx tsc --noEmit` 的錯誤與基線相同：基線（改動前）已有 30 個錯誤，全在
   `src/app/explore/page.test.tsx` 與 `src/components/EquityCurve.test.tsx`；本次 7 個檔 0 錯誤即通過。
   <!-- 2026-09-29 reviewer：原條件寫「無錯」但沒先建基線 -->
4. 測試守門（reviewer 變異實測後補）：`Header.test.tsx` 保留「未登入不打 /api/me/dashboard」斷言；
   `EquityCard.test.tsx` 的數字 span 用「可用保證金」標籤反查、不用位置定位。
