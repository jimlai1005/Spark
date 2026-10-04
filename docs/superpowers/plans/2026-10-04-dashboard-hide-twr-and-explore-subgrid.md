# 儀表板隱藏 TWR 兩數字 ＋ 探索表格 subgrid 對齊

日期：2026-10-04。使用者裁決（本 session）：

1. 儀表板「-81.5% 30D」與「你的跟單回撤 -86.42%」**不改算法、不改 API**，頁面上不顯示，
   但數值必須留在網頁 DOM 裡（debug 時從原始碼撈得到）。
2. 探索頁表格改 subgrid，讓表頭與所有列共用同一組欄軌。

## 背景（實作者只需知道這些）

### Task 1 背景
- 兩個數字出自 `/api/me/dashboard` 的 `equity.ret_30d_pct` 與 `pnl.max_drawdown_pct`，
  前端在 `web/src/components/dashboard/EquityCard.tsx:48-54` 與 `PnlCard.tsx:89-94` 渲染。
- 它們是 TWR（時間加權）指標，對 follower 自己的錢包會因「入金與虧損同取樣區間」被誇大
  （真實案例 0x438b…dce9：回撤顯示 86%，金額加權只有 −11%）。使用者決定先隱藏不改算法。
- 後端、`lib/api.ts` 型別、`copy.ts` 文案 key、既有測試 fixture **全部不動**。

### Task 2 背景
- `web/src/styles/globals.css:1609-1619`：`.explore-table-head` 與每個 `.explore-table-row`
  **各自**是 `display:grid` 容器，共用同一份 `grid-template-columns`，第 8 欄（目前曝險）是 `1fr`。
- `1fr` 的隱含最小值是內容 min-content，所以每列各自解出不同欄寬：表頭的 `1fr` 只剩 48px
  （「目前曝險」折行），有持倉的列 cell 內是 64px bar＋10px gap＋「100.0%」≈124px，
  該列被撐寬 ~76px，第 9、10 欄右移、「跟單 →」被切出容器；無持倉列只有「–」所以正常。
- DOM 結構（`web/src/app/explore/page.tsx:483-543`）：
  `.explore-table` > `.explore-table-head` + （`.explore-group` > `.explore-group-header` + `.explore-table-row`×n）×g
  或無分組時 `.explore-table` > head + `.explore-table-row`×n。
- 容器可用寬：`main.page` max-width 1080 − padding 20×2 = **1040px**。
- 既有測試 `web/src/app/explore/page.test.tsx:721-730` 要求 `.explore-group-header` 仍是
  `.explore-table` 的後代。

## Task 1 @inline — 儀表板隱藏兩個 TWR 數字，數值改放 data attribute

檔案：`web/src/components/dashboard/EquityCard.tsx`、`web/src/components/dashboard/PnlCard.tsx`。

1. `EquityCard.tsx`
   - 刪除 `<span className="mono dash-equity-ret">…{ret}{c.retSuffix}</span>` 整個元素
     （連同 `signedPct` 若因此無人使用則一併刪，避免 lint unused）。
   - 根元素 `<div className="card dash-card dash-card-equity">` 加
     `data-ret-30d-pct={equity?.ret_30d_pct ?? undefined}`（null 時不輸出屬性）。
   - 在根元素上方加一行註解：
     `{/* 2026-10-04 使用者裁決：30D TWR 報酬對 follower 會因入金同區間被誇大（見 plan 2026-10-04-dashboard-hide-twr-and-explore-subgrid.md），頁面不顯示、值留在 data-ret-30d-pct 供 debug。 */}`
2. `PnlCard.tsx`
   - 刪除 `.dash-pnl-metrics` 內第三個區塊（`c.maxDrawdown` 那一組 `<div>`）。
   - 根元素 `<div className="card dash-card dash-card-pnl">` 加
     `data-max-drawdown-pct={pnl?.max_drawdown_pct ?? undefined}`。
   - 同樣加一行同形註解。
3. `copy.ts` 的 `retSuffix`／`maxDrawdown` key **保留不刪**（COPY_ZH/COPY_EN 對稱測試不受影響，
   未來恢復顯示直接用）。
4. 新增測試 `web/src/components/dashboard/hiddenTwr.test.tsx`（用 `EquityCard.test.tsx` 的
   render 慣例與 fixture 形狀）：
   - render `EquityCard` 給 `ret_30d_pct: "-81.5"` → `container.textContent` 不含 `81.5`、
     不含 `30D`；`container.querySelector("[data-ret-30d-pct]")?.getAttribute(...)` 等於 `"-81.5"`。
   - render `PnlCard` 給 `max_drawdown_pct: "-86.42"` → textContent 不含 `86.42`、不含
     `COPY_ZH.dashboard.pnl.maxDrawdown` 文字；`[data-max-drawdown-pct]` 屬性等於 `"-86.42"`。
   - 兩者給 `null` 時屬性不存在（`querySelector` 回 null）。

驗收指令（一行）：
`export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH" && cd web && npx vitest run src/components/dashboard src/app/dashboard && npx eslint src/components/dashboard`
全綠、lint 零錯。

## Task 2 @inline — 探索表格改 subgrid，欄軌只定義一次

檔案：`web/src/styles/globals.css`（主要）、`web/src/app/explore/page.tsx`（曝險 cell 結構）、
新增 `web/scripts/explore_align_check.mjs`（Playwright 量測腳本，留在 repo 供日後重跑）。

### 2a CSS

1. `.explore-table`：加 `display: grid;` 與
   `grid-template-columns: 36px 164px 88px minmax(7.5rem, auto) 80px 64px 72px minmax(64px, 1fr) 68px 140px;`
   `column-gap: 12px;`。保留 `overflow-x: auto`、border、radius。
   （帳戶 172→164、操作 148→140，各省 8px 補曝險欄最小 64px；固定欄＋gap＋padding 合計 ≤ 1040。）
2. `.explore-table-head`、`.explore-table-row`、`.explore-group`、`.explore-group-header`：
   全部 `grid-column: 1 / -1;`。
3. `.explore-table-head`、`.explore-table-row`、`.explore-group`：
   `display: grid; grid-template-columns: subgrid; column-gap: inherit;`（巢狀 subgrid 合法，
   `.explore-group` 內的 row 再 subgrid 一層即可對到最外層欄軌）。
4. 從 `.explore-table-head, .explore-table-row` 規則**刪除** `grid-template-columns` 與
   `min-width: 1028px`；`gap: 12px` 改為只留 `row-gap: 0`（column-gap 由父層繼承）。
   水平 padding 18px 改到 `.explore-table` 上（`padding: 0 18px`），列與表頭只留
   `padding: 14px 0`；`.explore-group-header` 的 `min-width: 1028px` 刪除、`padding: 10px 0`。
   ⚠️ 若實測 `.explore-table` 的 `overflow-x:auto` 在窄視窗下 padding 右側被吃掉
   （grid 容器常見），改回在 head/row/group-header 各自 `padding-inline: 18px`（subgrid 允許
   padding，會算進首尾軌的 gutter），兩種擇一並在 CSS 註解寫明選了哪種、為什麼。
5. `.explore-exposure` 改直排：`display: flex; flex-direction: column; align-items: flex-start; gap: 4px;`。
   `.explore-exposure-label` 加 `white-space: nowrap;`。
6. ~~表頭第 8 欄文字加 `white-space: nowrap`~~ **撤回（2026-10-04 主線程 EN 截圖實測）**：
   英文「Current exposure」在 68px 內 nowrap 會溢出壓到「To liquidation」；英文表頭折兩行
   是既有慣例（Max drawdown／Live days），表頭不加 nowrap，只有列內 `.explore-exposure-label` 加。
   量測腳本判準同步改為「表頭第 8 欄不溢出」（`head_overflow`），`head_wrap` 只回報。
   腳本支援 `LANG_UI=en` 量英文版。
   另：builder 實測 subgrid 的 padding-inline 會吃掉首尾軌內容寬，首欄 36→46、末欄 140→158
   （總寬仍在 1040 內、1280 視窗無橫向捲動）。
7. `grid-template-columns` 在整份 CSS 的 explore 區段**只能出現一次**（在 `.explore-table`）。
   更新 1610-1617 行的歷史註解：保留，末尾追加一段
   `/* 2026-10-04：head/row 各自是獨立 grid，1fr 依各列 min-content 解出不同寬度 → 有持倉列第 9/10 欄右移。改為 .explore-table 單一 grid ＋ 列用 subgrid，欄軌只定義一次。 */`

### 2b JSX（`page.tsx`）
- `ExploreRowView` 曝險 cell 結構不變（bar 在前、label 在後），直排由 CSS 處理；不需改 JSX。
  若 subgrid 需要，表頭的 10 個 `<div>` 與列的 10 個 `<div>` 數量與順序**必須完全相同**
  （現況已是），不得增減。

### 2c 量測腳本 `web/scripts/explore_align_check.mjs`
- 用 `@playwright/test` 的 chromium，viewport 1280×900，`page.route("**/api/explore**", …)`
  餵 mock（形狀照 `web/src/app/explore/page.test.tsx` 的 `buildResp()`／`ExploreRow` fixture，
  自行內嵌 JSON），至少 4 列：① 有持倉＋距強平 39.9%、② 無持倉（dir null、liq null）、
  ③ pending 組一列、④ 損益 `+16819029`（最寬數字）。
- 對每個 `.explore-table-row` 與 `.explore-table-head`，取第 9、10 個子元素的
  `getBoundingClientRect().left`，輸出 `report.json`：`{head:{c9,c10}, rows:[{c9,c10,…}]}`
  與 `max_abs_diff_px`；另量 `.explore-table` 的 `scrollWidth > clientWidth`（桌面不得橫向捲動）
  與表頭第 8 欄 `height <= lineHeight*1.6`（不折行）。
- 以 `npm run build && npm run start`（production，port 3000）為目標；開跑前 `lsof -ti :3000`
  必須為空（見 memory nextjs-screenshot-testing-traps），結束後殺掉自己起的 server。
- 腳本退出碼：`max_abs_diff_px > 0.5` 或橫向捲動或折行 → exit 1。

驗收指令（兩行）：
1. `export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH" && cd web && npx vitest run src/app/explore && npx eslint src/app/explore`
2. `export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH" && cd web && node scripts/explore_align_check.mjs`
   → exit 0，並貼 report.json 的 `max_abs_diff_px`、`overflow`、`head_wrap` 三個欄位。

另：`grep -c "grid-template-columns" web/src/styles/globals.css` 在 explore 區段（1600-1690 行）
應恰為 1 次（非 subgrid 字樣）＋ subgrid 宣告。

## Task 3 @inline — 審查修正（2026-10-04 reviewer 三個 Warning ＋ 一個 Suggestion，主線程裁決全部修）

檔案：`web/src/styles/globals.css`、`web/scripts/explore_align_check.mjs`。不碰 JSX、不碰 dashboard。

1. **subgrid fallback**（W1）：在 explore 區段末尾加
   `@supports not (grid-template-columns: subgrid) { … }`，裡面讓 `.explore-table` 回到 `display: block`，
   `.explore-table-head, .explore-table-row` 各自 `display: grid; grid-template-columns: <與 .explore-table 同一串>; column-gap: 12px; min-width: 1040px;`，
   `.explore-group { display: block; }`、`.explore-group-header { min-width: 1040px; }`。
   退化目標＝修改前的行為（第 9/10 欄可能偏移，但不直向堆疊）。欄軌字串重複一次是 fallback
   的必要之惡，註解寫明「兩處必須同步改」。
2. **腳本驗 server 新鮮度**（W2）：連上後先 `getComputedStyle(.explore-table).display === "grid"`，
   否則印「:3000 served stale CSS (display=block)——是不是殭屍 server？」並 exit 2。
   不在腳本裡起／殺 server（operator 自己管，README 註解寫明步驟與 `lsof -ti :3000` 檢查）。
3. **輸出路徑**（W3）：`OUT_DIR` 預設改成相對 `web/` 的 `test-results/explore-align/`
   （`web/.gitignore` 已忽略 `test-results`；若未忽略則加一行）。
4. **列內溢出判準**（S2）：腳本對每一列的 10 個 cell 都量 `scrollWidth > clientWidth + 0.5`，
   任一溢出 → exit 1，report 列出是哪列哪欄。繁中與 `LANG_UI=en` 都要跑。
   若英文「Short 100.0%」在第 8 欄溢出：把最大回撤 80→72、實盤天數 64→56、結倉勝率 72→64
   （三欄內容最寬分別是 `-98.1%`、`1000`、`61.2%`，13px mono 皆 <50px），省下 24px 讓第 8 欄
   `minmax(64px,1fr)` 解到約 94px；fallback 區塊同步改。改完重量，兩種語言 10 欄皆不溢出。
5. 移除腳本裡未使用的 `btn` 變數（S3）。
6. `.dash-pnl-metrics` 保持 `repeat(4, 1fr)` 不動（S1 裁決：原本就是 4 欄放 3 項，留白是既有設計）。
7. 已知既有溢出：走勢欄 88px 但 `page.tsx:45` 的 `SPARK_W = 96`，svg 本來就超出 8px。第 4 項的
   逐 cell 判準會抓到它；修法只用 CSS：`.explore-spark svg { width: 100%; height: auto; }`
   （viewBox 不變，等比縮到 88px），不改 JSX。

驗收指令：
1. `export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH" && cd web && npx eslint scripts/explore_align_check.mjs src/app/explore && npx vitest run src/app/explore`
2. `npm run build && npm run start`（:3000 事先為空）後 `node scripts/explore_align_check.mjs` 與 `LANG_UI=en node scripts/explore_align_check.mjs` 皆 exit 0，貼兩次輸出 JSON；收尾殺掉自己起的 server。
3. `grep -c "grid-template-columns:" web/src/styles/globals.css` explore 區段 = 3（主定義、subgrid、fallback）。

## 不在範圍
- 不改 `leader_perf.py`、`app.py`、`trader_stats.py`、任何後端。
- 不改探索表格欄位順序、文案、排序行為。
- 不加響應式斷點。
