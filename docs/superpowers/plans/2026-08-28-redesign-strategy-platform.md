# Filet 改版：策略執行平台 Implementation Plan

> **For agentic workers:** 本 plan 依 `~/.claude/CLAUDE.md` 單 session 派工制執行：
> 主線程逐 task 派 `builder`（@inline）／`impl-worker`（@sdd），親跑驗收後才派下一個。
> 每個 task 是自足的：**只讀本檔與引用的 spec 檔**，不依賴任何對話脈絡。
> Steps 用 checkbox 追蹤。

**Goal:** 把現有「貼任意地址的工具」改版為「非保管策略執行平台」——首頁重構、策略詳情頁、統一 onboarding、使用者 Dashboard（六塊＋kill switch）、法務四頁、導覽狀態機、繁中/EN 雙語。

**Architecture:** 前端 Next.js 15 App Router（沿用；無 Tailwind，CSS 變數 tokens），文案維持 `copy.ts` 單一來源並升級為雙語＋`useCopy()` context。後端 FastAPI `publicapi/app.py` 新增無需登入的 `/api/public/*`（策略、統計、狀態）與登入後的 `/api/me/dashboard`、kill switch API。「策略」＝精選白名單 `leaders.json` 條目＋展示欄位＋由 `leader_perf` 擴充的統計指標。

**Tech Stack:** Next.js 15.5 / React 19 / wagmi 2.19 / viem 2.55 / vitest；Python 3.11 / FastAPI / pytest / Decimal。

---

## 0. 參考檔案（每個 task 開工前先讀）

| 檔案 | 內容 |
|---|---|
| `docs/superpowers/specs/2026-08-28-redesign-design.html` | **設計稿全文**（視覺與文案的權威來源）。段落行號：§01 顧問對應 L51-118 · §02 IA/導覽 L120-193 · §03 首頁 L195-426 · §04 策略詳情 L428-558 · §05 Onboarding L560-644 · §06 Dashboard L646-859 · §07 交付規格 L861-950 · **資料段 L956-1085**（NOTE 01-18、文案對照表、design tokens、路由表、實作順序） |
| `docs/superpowers/specs/2026-08-28-legal-copy-zh.md` | 法務三頁＋/docs 頁的繁中權威文本 |
| 專案 `CLAUDE.md` | 紅線（特別是 2、4、5、6、7）與慣例 |

## 0.1 使用者裁決記錄（2026-08-28，不得推翻）

1. **風控維持 opt-in**（紅線 5 不動）：Onboarding「風險限制」步驟照設計稿視覺呈現，但「最大回撤自動停止／成本熔斷」帶啟用開關、**預設關閉**，啟用才走既有 `/api/me/risk` 簽章流程。投入比例與槓桿上限照常必設（屬 capital 配置，非熔斷風控）。
2. **agent 授權 90 天到期：本次不做**。授權彈窗顯示真實授權內容，文案不得出現「90 天自動失效」。列 backlog。
3. **繁中＋英文雙語都做**：`copy.ts` 雙語字典＋header「繁中 / EN」切換。
4. **法務頁直接草擬全文上線**，不加「待法律審閱」標注（使用者自行安排審閱，屆時再替換）。

## 0.2 對設計稿的既定偏差（已裁決或工程取捨，實作照此為準）

- 授權彈窗不提 90 天到期（裁決 2）。
- 「Filet Neutral」「加入通知名單」不實作：策略卡由 API 驅動，有幾個真實精選 leader 就渲染幾張；`listable:false` 的策略卡呈現「樣本累積中／未開放跟單」disabled 態，無 email 收集。
- hreflang / `/en` 路由不做：單一路由＋client 端語言切換（`localStorage` 記憶，預設繁中）。SEO metadata 一律繁中。
- OG image v1 用靜態品牌圖；每策略動態 OG 圖列 backlog。
- Dashboard 次要 tab v1 只做「跟單持倉」與「費用明細」；「成交記錄」「授權歷程」tab 顯示為 disabled（copy: 即將推出），列 backlog。
- 「平倉並撤銷授權」的撤銷段 v1 為引導：引擎受控收尾完成後，前端指引用戶至 Hyperliquid 官方介面移除 API wallet（附連結與步驟），不在本站代發撤銷交易。
- 策略卡「日均筆數」chip：若成交數據取得成本低則顯示，否則省略該 chip（不擋驗收）。

## 0.3 全域不變量（每個 task 的隱含驗收條件）

1. **簽章原文一律伺服器產生**：前端永不自組簽署字串（既有四支簽署端點形狀不得破壞）。
2. **內部一律 Decimal**；float 只在 adapter↔SDK 邊界。金額序列化為字串。
3. **測試全離線**：autouse socket-ban；新測試不得連網。
4. `/api/public/*` **不得洩漏 follower 個資**：只回聚合數字與 leader 公開資訊；follower 地址、個別 email、env 內容一律不出現。
5. 元件不得內嵌中文字面值（語言紅線測試）；新文案一律進 `copy.ts`（或 legal content module），**zh/en key 結構對稱**。
6. 證據層／指標數字**全部來自 API**；取不到顯示「—」並保留欄位（NOTE 02）。不可寫死假數字——設計稿裡的 +20.35% 等全是佔位示意。
7. 讀不到資料 ≠ 進入危險態：pause 旗標讀取失敗 → 視為暫停＋告警（fail-safe 朝「少動作」）；權益讀值異常 → 顯示「—」，不觸發任何自動安全動作。
8. 對外主機名以 `NEXT_PUBLIC_SITE_ORIGIN`（預設 `https://app.filet.trade`）注入，只用於 canonical/OG/絕對連結；站內連結一律相對路徑。

## 0.4 驗收指令（主線程每個 task 完成後親跑）

```bash
uv run pytest                    # 後端（integration 預設跳過）
uv run ruff check src tests scripts
export PATH="/Users/jim/.nvm/versions/node/v24.18.0/bin:$PATH" && cd web && npm test
```

Branch：`feat/m3-redesign`（自 `feat/m2-frontend` 分出）。Commit 逐 task：`feat:`/`fix:` 一行。

---

## 檔案地圖（新建／主要修改）

```
web/src/lib/copy.ts                 改：雙語（COPY_ZH 沿用現有＋新 key；COPY_EN 鏡像）
web/src/lib/lang.tsx                新：LangProvider + useLang + useCopy
web/src/lib/publicApi.ts            新：/api/public/* 的 fetch helpers（無需登入）
web/src/content/legal.ts            新：法務長文 content module（zh/en 結構對稱）
web/src/styles/tokens.css           改：設計稿 palette/字級/圓角
web/src/styles/globals.css          改：深色底、字體、tabular-nums
web/src/components/Header.tsx       改：導覽狀態機＋狀態 pill＋語言切換
web/src/components/Footer.tsx       新：四欄 footer＋狀態燈
web/src/components/FeeCalculator.tsx 新：費用試算 slider
web/src/components/CapabilityMatrix.tsx 新：能力矩陣（三處共用）
web/src/components/StrategyCard.tsx 新：策略卡
web/src/components/EquityCurve.tsx  新：淨值曲線 SVG（含疊加對照骨架）
web/src/app/page.tsx                改：首頁重構（§03）
web/src/app/strategies/page.tsx     新：策略列表
web/src/app/strategies/[slug]/page.tsx 新：策略詳情（§04）
web/src/app/onboarding/page.tsx     改：四步 wizard（§05）
web/src/app/advanced/page.tsx       新：進階模式（自 leaders/ 遷移重構）
web/src/app/leaders/page.tsx        改：redirect → /advanced
web/src/app/dashboard/page.tsx      新：六塊 Dashboard（§06）
web/src/app/settings/page.tsx       新：設定（風控/資金/授權管理）
web/src/app/{terms,privacy,risk,docs,status}/page.tsx 新：內容頁
src/spark/filet/leader_perf.py      改：新增比率指標（sharpe/sortino/vol/勝率/最佳最差日）
src/spark/filet/strategies.py       新：策略視圖（leaders.json 條目 → strategy 物件＋60 天閘門）
src/spark/publicapi/app.py          改：/api/public/*、/api/me/dashboard、pause/close-all
src/spark/publicapi/public_stats.py 新：聚合統計（路由量/費用/狀態）＋60s cache
src/spark/copytrade/…               改：pause 旗標與 owner close 請求的引擎側（task 15 詳述）
```

---

# Phase 0 — 基礎

### Task 1 @inline：Design tokens 與全站深色主題

**Files:** 改 `web/src/styles/tokens.css`、`web/src/styles/globals.css`、`web/src/app/layout.tsx`（字體載入）。

**規格**（設計稿 §07 L861-905、資料段 tokens L986-998）：
- palette：canvas `#07080a`、section `#0e1013`、card `#101317`、inset `#0a0c0e`、border `#232830`、主文字 `#e9ecef`、次文字 `#868f99`、accent `#46d6b3`、positive `#3ecf8e`、negative `#f2666b`、warning `#e9b872`。全部落成 CSS 變數（沿用現有 `--bg`/`--text`/`--primary`… 命名，值換掉；缺的變數新增）。
- 字體：Noto Sans TC（介面）＋ JetBrains Mono（數字，`font-variant-numeric: tabular-nums`）。用 `next/font/google` 載入，勿用 `<link>`。
- 字級階：52/34/30/22/17/15/13/11；圓角：卡 14 / 內層 10 / 按鈕 9 / 標籤 5；卡間距 12px、頁邊 44px；**不用陰影**，層級靠 border＋底色。
- 提供 utility class：`.mono`（等寬數字）、`.card`、`.inset`、`.pill`。
- 既有頁面在新 tokens 下仍可讀（顏色對比夠、不追求舊版視覺不變——反正後續 task 會重寫各頁）。

- [ ] 寫 tokens.css/globals.css 變更＋layout 字體
- [ ] `npm test` 全綠（既有測試不因 class 改名而壞；若壞，修測試中的樣式斷言）
- [ ] Commit `feat: redesign design tokens + dark theme foundation`

**驗收：** `npm test` 全綠；`grep -c "46d6b3" web/src/styles/tokens.css` ≥ 1。

### Task 2 @inline：i18n 基礎（雙語 copy＋useCopy）

**Files:** 改 `web/src/lib/copy.ts`；新 `web/src/lib/lang.tsx`；改 `web/src/app/providers.tsx`；改 `web/src/lib/copy.test.ts`。

**規格：**
- `copy.ts`：現有 `COPY` 更名 `COPY_ZH`（內容不動，後續 task 增 key）；新增 `COPY_EN`，型別 `typeof COPY_ZH`（TS 結構對稱由型別強制）。翻譯語義等值、專有名詞（Hyperliquid、builder fee、agent）保留英文。保留 `export const COPY = COPY_ZH`（讓未遷移元件過渡期不炸）。
- `lang.tsx`：`LangProvider`（React context；`lang: "zh" | "en"`，初值讀 `localStorage.filet_lang`，預設 `"zh"`；`setLang` 寫回）＋ `useLang()` ＋ `useCopy(): typeof COPY_ZH`（依 lang 回傳字典）。SSR 安全：`localStorage` 讀取包在 effect，首繪一律 zh，避免 hydration mismatch。
- `providers.tsx` 掛 `LangProvider`。
- 測試：(a) zh/en 深層 key 完全對稱（遞迴比對 key set）；(b) en 值不含 CJK 字元（regex `[一-鿿]`）；(c) `useCopy` 切換後回傳 en 字典。

- [ ] 寫失敗測試（key 對稱＋en 無 CJK）→ 實作 → 綠
- [ ] Commit `feat: bilingual copy source + LangProvider/useCopy`

**驗收：** `npm test` 全綠；`grep -c "COPY_EN" web/src/lib/copy.ts` ≥ 1。

### Task 3 @sdd：元件遷移到 useCopy

**前置：** Task 2 已完成並在任一元件示範過 pattern。

**規格：** 對 `web/src` 全部 import `COPY` 的元件／頁面：`import { COPY } from ...` → 於元件內 `const c = useCopy()` 並把 `COPY.` 引用改 `c.`。非 React 模組（若有）維持 `COPY_ZH` 顯式 import。完成後移除 `export const COPY` 過渡別名。

**驗收（一行）：** `cd web && grep -rn "import { COPY }" src --include="*.tsx" | wc -l` 輸出 `0`，且 `npm test` 全綠。

- [ ] 逐檔套用 → 驗收 grep 為 0 → `npm test` 綠 → Commit `refactor: migrate components to useCopy`

---

# Phase A — 後端 API

### Task 4 @inline：leader_perf 比率指標擴充

**Files:** 改 `src/spark/filet/leader_perf.py`（現有 `compute_window_performance` L219 起）；測試加在既有 `tests/` 對應檔或新 `tests/test_leader_perf_ratios.py`。

**規格（顯式方程式＋數值錨例；全 Decimal）：**

由 equity index 導出日報酬樣本 \(r_i = E_i/E_{i-1} - 1\)（沿用既有日對齊邏輯），N = 樣本數。慣例：**365 日/年、無風險利率 0%**（與站上方法論揭露一致）。

- 年化 Sharpe：\( SR = \dfrac{\bar r}{s} \sqrt{365} \)，\(s\) 為樣本標準差（ddof=1）。
- Sharpe 標準誤：\( SE(SR) = \sqrt{\dfrac{1 + SR_d^2/2}{N}} \cdot \sqrt{365} \)，其中 \(SR_d = \bar r / s\)（日頻）。
- 年化波動：\( \sigma_{ann} = s\sqrt{365} \)。
- Sortino：\( \dfrac{\bar r}{DD}\sqrt{365} \)，\( DD = \sqrt{\frac{1}{N}\sum_i \min(r_i, 0)^2} \)（全樣本平均、對 0 門檻）。DD = 0 → 標 insufficient。
- 日勝率：\( \#\{r_i > 0\} / N \)。
- 最佳／最差日：\(\max r_i\)、\(\min r_i\)。
- **樣本閘門：`RATIO_MIN_DAYS = 60`**——covered_days < 60 時 sharpe/sortino/年化波動回 `*_insufficient_data` 旗標（沿用檔內既有 insufficient 模式）；勝率與最佳最差日不設閘（樣本數照回）。

**數值錨例（測試必含，精度 1e-4）：** r = [0.01, -0.005, 0.02]（N=3，僅為公式錨，繞過 60 天閘門測純函式）：
mean=0.0083333、s=0.0125831、SR=**12.6526**、SE=**12.1798**、σ_ann=**0.2404**（24.04%）、Sortino=**55.1513**（DD=0.0028868）、勝率=2/3、best=0.02、worst=-0.005。<!-- 2026-08-28: SR/SE 錨值由 Decimal 50 位重算修正（原 12.6535/12.1800 為手算滑差），Task 4 實作與測試已用修正值 -->

- [ ] 寫錨例失敗測試 → 純函式實作（獨立小函式，`compute_window_performance` 組裝）→ 綠
- [ ] 閘門測試：covered_days 59 → insufficient；60 → 有值
- [ ] `jsonable_performance` 帶出新欄位（Decimal→str）
- [ ] Commit `feat: leader_perf ratio metrics (sharpe/sortino/vol/win-rate)`

**驗收：** `uv run pytest tests/ -k "leader_perf or ratio" -q` 全綠＋錨例斷言存在。

### Task 5 @inline：策略層與 /api/public/strategies

**Files:** 新 `src/spark/filet/strategies.py`；改 `src/spark/publicapi/app.py`；改 leaders 載入器所在模組（優雅接受新欄位）；改 `deploy/leaders.json.example`；新 `tests/test_public_strategies.py`。

**規格：**
- `leaders.json` 條目新增**可選**展示欄位：`slug`（URL id，如 `"core"`；缺→用完整地址小寫）、`tagline`（如「多資產動能 · 永續合約」）、`featured`（bool，主推 badge）、`min_notional_usd`（str）、`max_leverage`（str）。loader fail-fast 語義不變：未知欄位仍拒載？——**否**：改為「白名單內可選欄位」，清單外欄位照舊拒載（保持 fail-fast 防 typo）。
- `strategies.py`：`build_strategy_view(entry, perf) -> dict` 純函式＋`STRATEGY_MIN_LIVE_DAYS = 60` 閘門：`listable = enabled and accepting_new and covered_days >= 60`。
- `GET /api/public/strategies`：回 `{"strategies": [...], "updated_at": ...}`，每筆：

```json
{"slug": "core", "name": "Filet Core", "tagline": "…", "featured": true,
 "leader_address": "0xfB9C…（完整位址）", "status": "running",
 "listable": true, "live_days": 72, "follower_count": 14,
 "min_notional_usd": "500", "max_leverage": "3",
 "metrics": {"total_return_pct": "20.35", "max_drawdown_pct": "-0.80",
   "sharpe": "10.24", "sharpe_se": "3.36", "win_rate_pct": "64.86",
   "annualized_vol_pct": "18.05", "sortino": "43.42",
   "best_day_pct": "3.01", "worst_day_pct": "-0.80",
   "sample_count": 38}}
```

  （數字全為示意形狀；insufficient 時該欄回 `null` 並附 `"<key>_insufficient": true`。）績效走既有 preview/perf 路徑（Hyperliquid API 上游），**伺服器端 cache 60s**（同 task 6 的 cache helper 可共用）。`enabled:false` 條目不出現在列表。
- `GET /api/public/strategies/{slug}`：上述＋`equity_index`（jsonable series）＋`methodology`：`{"start_date","end_date","initial_deposit_usd","sample_count","annualization_days": 365, "risk_free_rate": "0", "basis": "perp", "updated_at"}`。404：slug 不存在或 enabled:false。
- `follower_count`：以現有 ops/customers 資料來源（server 端）統計「目前 active 跟該 leader 的 follower 數」，只回整數；資料源不可用 → `null`。**不得回任何 follower 識別資訊**（不變量 4）。
- 測試（離線、假資料）：列表形狀；60 天閘門翻轉 `listable`；enabled:false 隱藏；404；follower_count 聚合；不含任何 follower 位址欄位（結構斷言）。

- [ ] 失敗測試 → 實作 → 綠 → Commit `feat: public strategies API + 60-day listing gate`

**驗收：** `uv run pytest tests/test_public_strategies.py -q` 全綠；`uv run ruff check src` 乾淨。

### Task 6 @inline：/api/public/stats 與 /api/public/status

**Files:** 新 `src/spark/publicapi/public_stats.py`；改 `src/spark/publicapi/app.py`；新 `tests/test_public_stats.py`。

**規格：**
- `GET /api/public/stats` →

```json
{"routed_volume_usd_total": "4280000.00", "builder_fee_bps": 2,
 "live_days": 72, "updated_at": 1724800000}
```

  `routed_volume_usd_total`：由既有 billing/revenue 資料來源聚合的歷史總路由成交名目（與 `/api/ops/revenue` 同源，只取總量；實作時先讀 `src/spark/publicapi/ops.py` 確認資料形狀）。`live_days`：featured 策略的 covered_days。任一子項取不到 → 該欄 `null`，端點仍 200。
- `GET /api/public/status` →

```json
{"status": "ok", "components": [
  {"name": "api", "status": "ok"},
  {"name": "engine", "status": "ok"}], "updated_at": 1724800000}
```

  `engine`：由 heartbeat 檔新鮮度判定（存在且 mtime < 10 分鐘 → ok；否則 degraded；讀不到 → unknown）。多 follower 引擎取「最新一個 heartbeat」代表整體，**不揭露 follower 數與身分**。
- 兩端點共用 in-process cache（60s TTL，`now_fn` 可注入以便測試）。無需登入、無 cookie。
- 測試：形狀；cache 命中（同 60s 內資料源只被呼叫一次）；heartbeat 過期 → degraded；資料源丟例外 → 對應欄 null / status unknown（**不得 500**——公開狀態頁本身要比被監控對象可靠）。

- [ ] 失敗測試 → 實作 → 綠 → Commit `feat: public stats + status endpoints`

**驗收：** `uv run pytest tests/test_public_stats.py -q` 全綠。

---

# Phase B — 前端頁面

> 通用規格：視覺以設計稿對應段落為權威（inline style 轉成 CSS class；沿用 Task 1 tokens）。
> 所有新文案 key 同步進 `COPY_ZH`＋`COPY_EN`。數字用 `.mono`。
> 每頁配基本 vitest（渲染、關鍵互動、guard/redirect）。

### Task 7 @inline：Header 導覽狀態機＋Footer

**Files:** 改 `web/src/components/Header.tsx`；新 `web/src/components/Footer.tsx`；改 `web/src/app/layout.tsx`（Footer 掛全站）；改 `web/src/lib/copy.ts`（nav/footer key）。

**規格**（§02 L163-192、footer §03 L390-415）：
- **未登入**：logo＋`策略(/strategies)｜運作方式(/#how)｜安全性(/#security)｜文件(/docs)`＋語言切換（`繁中 / EN`，接 useLang）＋單一 CTA「查看策略與風險」→ `/strategies`。「綁定錢包」「跟單」tab **不渲染**；移除任何連回首頁的「開始」。
- **已登入**（`useMe()` 判定）：`Dashboard(/dashboard)｜策略｜設定(/settings)｜文件`＋跟單狀態 pill（`跟單中/已暫停/未跟單`，資料來自 `/api/me`＋dashboard 摘要；點擊 → `/dashboard`）＋地址縮寫。ADMIN 組照舊附加。
- Footer 四欄（產品/可驗證/法務與聯絡/品牌），含系統狀態燈：讀 `/api/public/status`（新 `web/src/lib/publicApi.ts` helper；polling 不需要，載入一次），ok→綠「系統運作正常」、degraded→黃、unknown→灰「狀態未知」。法務欄連 `/terms /privacy /risk`、`contact@filet.trade`。免責一段（文案對照設計稿 L412）。
- 測試：未登入不渲染 dashboard tab；登入渲染；語言切換改變 nav 字串；footer 狀態燈三態。

- [ ] 測試 → 實作 → 綠 → Commit `feat: nav state machine + footer with status light`

**驗收：** `npm test` 全綠；`grep -n "開始" web/src/lib/copy.ts` 中 nav.login 已移除或改義。

### Task 8 @inline：首頁重構

**Files:** 改 `web/src/app/page.tsx`；新 `web/src/components/{StrategyCard,CapabilityMatrix,FeeCalculator}.tsx`；改 `copy.ts`。

**規格**（§03 全段＋NOTE 01-06＋文案對照表 L999-1006）：
- 區塊順序：hero（badge「非保管 · 資金不離開你的錢包」＋H1「讓你的 Hyperliquid 帳戶，自動跟隨量化策略」＋sub＋雙 CTA＋「不需要註冊…」小字＋右側主推策略卡）→ 證據列 → 可跟單策略（StrategyCard×N＋進階模式卡）→ 能力矩陣 → 費用試算 → 四步驟 → footer（Task 7）。
- **第一屏零錢包按鈕**（NOTE 01）：hero CTA→`/strategies`；「授權能做什麼？」→ 錨點至能力矩陣。
- 證據列四數字接 `/api/public/stats`：累計路由量（USD 縮寫格式 $4.28M）、實盤天數、builder fee 0.02%、託管資產 0（靜態字串）。null → 「—」保留欄位（NOTE 02）。各附外連（hyperliquid explorer leader 帳戶頁、/docs 費用段）。
- 策略卡接 `/api/public/strategies`：指標四格（總報酬/最大回撤/Sharpe±s.e./日勝率）、chips（槓桿≤{max_leverage}x、最低跟單 ${min_notional_usd}、日均筆數若有）、`listable:false` → disabled 態＋「樣本累積中」。CTA →`/strategies/{slug}`。**CAGR 不出現在首頁**（NOTE 03）。
- 進階模式卡：說明＋CTA→`/advanced`（**不含**地址輸入框——輸入在 /advanced 頁內，NOTE 05）。
- `CapabilityMatrix`：可以/不能 各四條＋單方可撤銷段（文案 key `auth.can[]` / `auth.cannot[]` / `auth.revocable`，**單一來源三處共用**：首頁、策略頁授權說明、onboarding 授權卡）。
- `FeeCalculator`：slider $1,000–$100,000（step 1,000，預設 10,000）；`side = notional × 0.0002`，顯示建倉/平倉/合計（$10,000 → $2/$2/**$4**）。純 client 元件＋單元測試（錨例 10000→4.00、100000→40.00）。
- hero 主推卡數字接 featured 策略 API；30D 報酬用 API 的 window 數據（若只有全期，顯示全期並標註天數——不得虛構 30D）。
- 測試：FeeCalculator 錨例；證據列 null→「—」；策略卡 disabled 態；hero 無 wallet connect 按鈕。

- [ ] 測試 → 實作 → 綠 → Commit `feat: homepage — strategy-first + evidence layer`

**驗收：** `npm test` 全綠。

### Task 9 @inline：策略列表與詳情頁

**Files:** 新 `web/src/app/strategies/page.tsx`、`web/src/app/strategies/[slug]/page.tsx`、`web/src/components/EquityCurve.tsx`；改 `copy.ts`。

**規格**（§04 全段＋NOTE 07-09、metrics 形狀 L1049-1058）：
- 列表頁：StrategyCard 網格（復用 Task 8 元件）＋進階模式卡。
- 詳情頁 `/strategies/[slug]`：麵包屑、標題列（名稱＋運行中 pill＋leader 位址縮寫外連 explorer）、右上資料時戳「資料截至 {updated_at} · 來源：Hyperliquid API」。左欄：EquityCurve（API equity_index 繪 SVG polyline，період切換 全部/30D/7D client 端裁切；疊加對照 BTC/ETH/S&P/黃金 v1 只做 UI 骨架、預設關閉、無資料源時 checkbox disabled——NOTE 09 說保留現有實作，實作時先查有無現成對照資料，無則 disabled）、指標卡×8（API metrics；insufficient → 「樣本不足」灰階）、CAGR 灰階卡（帶「樣本 N 天，年化外推無統計意義」說明，可折疊）、方法論與樣本揭露段（文字模板接 API methodology 數字）。右欄跟單面板：三 slider（投入比例/槓桿上限/最大回撤——**未連錢包即可調**，NOTE 07；回撤 slider 帶「啟用」開關預設關，見裁決 1）＋預估區＋CTA「連接錢包並繼續」→ 觸發 SIWE 登入後帶參數進 `/onboarding?strategy={slug}&scale=…&lev=…&dd=…`；已登入者直接跳轉。面板下方小字「下一步僅為免費簽名…」。
- 每個績效數字旁有來源與時戳（NOTE 08）。
- 1024 斷點：右欄變底部 sticky bar（§07 L934）。
- 測試：slug 404 顯示空態；insufficient 指標渲染「樣本不足」；slider 未登入可互動；CTA 帶參數導向。

- [ ] 測試 → 實作 → 綠 → Commit `feat: strategy list + detail (decision page)`

**驗收：** `npm test` 全綠。

### Task 10 @inline：Onboarding 四步重構

**Files:** 改 `web/src/app/onboarding/page.tsx`＋`web/src/components/wizard/*`；改 `copy.ts`。

**規格**（§05 全段＋NOTE 10-12；裁決 1、2）：
- 路線 `/onboarding?strategy={slug}`。**未登入（無 SIWE session）→ redirect `/strategies`**（NOTE 10；`/leaders` 舊路由同置 Task 11）。無 strategy 參數 → 亦 redirect `/strategies`（進階模式走 `/advanced` 自帶參數）。
- 四步（視覺步驟條照設計稿；既有簽署／入金元件重用，只換殼與順序）：
  1. **選擇策略**：進頁即完成態（顯示所選策略摘要，可返回改選）。
  2. **連接與授權**：既有 agent 授權＋builder fee 授權簽署（StepSign 邏輯）＋**入金檢查**（StepDeposit 邏輯併入此步的完成條件）。授權卡顯示能力矩陣精簡版（共用 key）＋伺服器回的真實授權參數（agentAddress、maxBuilderFee）。**不出現「90 天」字樣**。
  3. **風險限制**：投入比例（必設，接 `/api/me/capital` 既有簽章流）＋槓桿上限（必設）＋「最大回撤自動停止」帶啟用開關**預設關**——開啟才顯示 slider 並於送出時走 `/api/me/risk/message`→`/api/me/risk` 既有簽章流；關閉則不建立風控記錄（紅線 5 語義）。從策略頁帶來的參數預填。
  4. **費用與風險確認**：費用試算（復用 FeeCalculator，預填投入額）＋三條 checkbox（NOTE 12：我理解可能虧損／我理解費用為 0.02% 每筆／我理解可隨時撤銷）**全勾才可送出**；送出後進入既有 pending/activate 流程，完成 → `/dashboard`。
- **斷點續作**（NOTE 11）：wizard 狀態（目前步、已簽項、參數）存 `localStorage.filet_onboarding`；重新進入從未完成步續作；完成或撤銷時清除。已簽章的事實一律以 `/api/onboard/status` 伺服器狀態為準，localStorage 只存 UI 進度（**不存簽章內容**）。
- 簽章原文伺服器產生的既有結構不得動（不變量 1）。
- 測試：未登入 redirect；步驟條狀態；step3 風控開關預設關且關閉時不呼叫 risk API（mock 斷言）；step4 未全勾不可送出；localStorage 續作（模擬 reload 從 step3 續）。

- [ ] 測試 → 實作 → 綠 → Commit `feat: unified 4-step onboarding (opt-in risk controls)`

**驗收：** `npm test` 全綠；`grep -rn "90 天\|90 days" web/src` 輸出 0。

### Task 10b @inline：投入比例接真實簽章流＋槓桿改誠實呈現（主線程裁決 2026-08-28）

> 背景：Task 10 實作時把投入比例做成純 UI 狀態、未接 `/api/me/capital`。主線程查證
> 後裁決如下（事實：`capital_settings.py` 檔頭明言 allocated_capital/capital_utilization
> **直接乘進部位大小**（sizing.compute_scale_factor），且引擎套用前自行驗章——這是
> 真實綁定機制，UI 不接等於騙用戶「已設限」；槓桿帽 `COPY_MAX_TARGET_LEVERAGE`
> 存在於引擎 config，但目前是 env 靜態值、無用戶簽章通道）。

**Files:** 改 `web/src/components/wizard/StepRiskLimits.tsx`、`web/src/app/onboarding/page.tsx`、`web/src/app/strategies/[slug]/page.tsx`、`web/src/lib/api.ts`（新增 capital 簽章流包裝——api.ts 檔頭「四支簽章端點」註解更新為五支並說明）、`copy.ts`。

**規格：**
1. **投入比例＝真實簽章流**：step 3 的投入比例送出走 `GET /api/me/capital/message?allocated_capital=0&capital_utilization={x}&use_full_equity=true` → 錢包簽名 → `POST /api/me/capital`（伺服器簽文原樣簽，不變量 1）。實作前先讀 `src/spark/copytrade/sizing.py` 的 compute_scale_factor 確認 use_full_equity 語義（若「淨值 X%」的正確組合不是 use_full_equity=true + utilization=x，以 sizing 實際語義為準並回報）。UI 顯示 `GET /api/me/capital` 的 effective/pending 兩態（該端點自帶此語義），pending 時標「已提交，待引擎套用」。
2. **槓桿改資訊呈現**：step 3 與策略頁面板的「槓桿上限」slider 移除，改為唯讀資訊列「本策略槓桿上限 {max_leverage}x（平台層強制）＋策略歷史平均槓桿（若 API 有）」；`lev` query 參數移除。per-user 可簽槓桿上限列 backlog。
3. **能力矩陣文案校正**（三處共用 key，一次改）：「使用你設定範圍內的保證金（投入比例上限）」保留（現在為真）；「在你簽署的槓桿上限內調整倉位」改為「在策略標示的槓桿上限內執行（平台層強制）」；不能側「超出你設定的投入比例、槓桿與最大回撤」改為「超出你簽署的投入比例；超出策略標示的槓桿上限；觸發你啟用的最大回撤而不停止」（zh/en 同步）。
4. 測試：step3 送出呼叫 capital message+POST（mock 斷言簽文原樣傳遞）；pending/effective 兩態渲染；槓桿 slider 不存在（斷言）；策略頁 CTA 參數只剩 scale/dd。

**驗收：** `npm test` 全綠；`grep -n "lev=" web/src/app/strategies` 無 CTA 參數殘留；commit `fix: wire allocation to signed capital flow + honest leverage presentation`。

### Task 11 @inline：進階模式 /advanced

**Files:** 新 `web/src/app/advanced/page.tsx`（自 `web/src/app/leaders/page.tsx` 遷移重構）；`leaders/page.tsx` 改為 redirect `/advanced`；改 `copy.ts`。

**規格**（§03 進階模式卡 L318-323＋NOTE 05；沿用既有 leaders 頁全部功能）：
- 既有功能不減：地址輸入、准入檢查、leader 預覽（含績效）、選定簽章（伺服器簽文）。
- 新增門檻：進頁顯著的無背書聲明＋checkbox「我理解 Filet 不對此地址的策略品質、風控或存續做任何背書」，**勾選前輸入框 disabled**。
- 未登入 → 先觸發 SIWE（此頁本就需要登入操作；未登入時顯示說明＋登入 CTA，不 redirect——它是進階用戶的直達入口）。
- 選定後 → `/onboarding?strategy=advanced:{address}`（onboarding 接受 `advanced:0x…` 形式，策略摘要顯示地址與「進階模式（無背書）」標示）。
- 測試：未勾選 disabled；勾選後可輸入；redirect 舊路由。

- [ ] 測試 → 實作 → 綠 → Commit `feat: advanced mode with explicit no-endorsement gate`

**驗收：** `npm test` 全綠；訪問 `/leaders` 的測試斷言 redirect。

### Task 12 @inline：法務與內容頁

**Files:** 新 `web/src/content/legal.ts`；新 `web/src/app/{terms,privacy,risk,docs,status}/page.tsx`；改 `copy.ts`（docs 頁複用 key）。

**規格：**
- 內容**逐字**取自 `docs/superpowers/specs/2026-08-28-legal-copy-zh.md`（繁中權威版，不得改寫語義；`{{ effectiveDate }}` 填 `2026-08-28`）。英文版對照翻譯（語義等值；zh/en 結構對稱，型別強制同 copy.ts 模式）。**不加「待法律審閱」標注**（使用者裁決 4）。
- 五頁未登入可直接開啟、可被索引（不掛登入 guard；§07 L904）。
- `/docs`：依 legal-copy spec 第 /docs 節，段落文字**複用** copy.ts 既有/新 key（四步驟、能力矩陣、費用、方法論），不新寫語義；附法務三頁連結。
- `/status`：讀 `/api/public/status` 渲染整體狀態＋components 清單＋updated_at；load 失敗顯示 unknown 態（頁面本身不炸）。
- 測試：各頁渲染標題；status 頁三態；legal zh/en key 對稱。

- [ ] 實作 → 測試綠 → Commit `feat: legal pages + docs + status`

**驗收：** `npm test` 全綠；主線程 read-back 抽查 terms/risk 內文與 spec 一致（抽 3 段 diff）。

### Task 13 @inline：Dashboard 後端 /api/me/dashboard

**Files:** 改 `src/spark/publicapi/app.py`（或新 `src/spark/publicapi/dashboard.py` 供 app 掛載）；新 `tests/test_me_dashboard.py`。

**規格：** 登入後 `GET /api/me/dashboard` 一次回六塊＋持倉，**每塊獨立 nullable**（子資料源失敗回 null，端點不 500）：

```json
{"status": {"strategy_name": "…", "state": "following|paused|halted|inactive",
   "following_days": 41, "signal_source_ok": true,
   "guards": {"scale": {"now": "0.241", "max": "0.25"},
              "leverage": {"now": "1.25", "max": "3.0"},
              "drawdown": {"now": "-0.0064", "max": "-0.10", "enabled": true}}},
 "equity": {"account_value": "1206.67", "margin_used": "418.05",
   "withdrawable": "2.69", "available_pct": "0.0064", "ret_30d_pct": "2.4"},
 "exposure": {"notional": "521.20", "leverage": "1.25", "long_pct": "100.0",
   "short_pct": "0.0", "position_count": 6,
   "max_position": {"symbol": "INTC", "pct": "29.1"}},
 "pnl": {"net": "39.57", "realized": "31.48", "unrealized": "8.09",
   "fees_paid": "1.66", "fee_share_of_pnl_pct": "4.2", "win_rate_pct": "75.61",
   "closed_positions": 41, "max_drawdown_pct": "-0.64", "series": [[ts, "v"], …]},
 "sync": {"latency_median_ms": 480, "latency_p95_ms": 1200,
   "price_diff_bp": "1.4", "unsynced_positions": 0,
   "scale_deviation_pct": "0.8", "missed_signals_24h": 1,
   "missed_reason": "insufficient_margin", "last_recon_ts": 1724805060},
 "fees_month": {"routed_volume": "312480", "builder_fees": "62.50",
   "fill_count": 96, "avg_fee": "0.65", "effective_rate_bps": "2.00",
   "daily_bars": [["2026-08-01","10.2"], …]},
 "positions": [{"symbol": "ETH", "side": "long", "leverage": "25", "margin_mode": "cross",
   "value": "99.70", "upnl": "1.59", "entry": "2452.76", "mark": "2492.54",
   "deviation_pct": "0.4"}],
 "updated_at": 1724805063}
```

- 資料來源（實作時逐一確認形狀，全部既有）：帳戶淨值/保證金/持倉 ← follower 的 clearinghouseState（**equity basis = accountValue**，恆等式 `accountValue == totalMarginUsed + withdrawable`（僅在無掛單時成立），engineering-principles #1）；PnL/費用 ← billing 資料與 fills；sync ← `/api/ops/trade-quality` 的同源資料**過濾到本 user**；guards ← `/api/me/risk` 設定＋當前帳戶推算；30D 報酬與 PnL series ← follower 自己的 portfolio series（同 leader_perf 管線餵 follower 位址）。找不到既有來源的欄位 → 回 null，**不造數字**。
- `fee_share_of_pnl_pct = fees_paid / |net + fees_paid| × 100`（分母為含費 PnL 絕對值；分母 0 → null）。
- 授權範圍：只回當前登入 user 自己的資料（沿用既有 `/api/me/*` 的 session 檢查）。
- 測試：假 store/假 HL 注入下的完整形狀；子源丟例外 → 對應塊 null 其餘正常；未登入 401；`available_pct` 錨例（2.69/418.05+... 用固定假資料算一組寫死斷言）。

- [ ] 失敗測試 → 實作 → 綠 → Commit `feat: /api/me/dashboard aggregate endpoint`

**驗收：** `uv run pytest tests/test_me_dashboard.py -q` 全綠。

### Task 14 @inline：Dashboard 前端六塊

**Files:** 新 `web/src/app/dashboard/page.tsx`＋`web/src/components/dashboard/*`（六塊各一元件＋持倉表）；改 `copy.ts`。

**規格**（§06 全段＋NOTE 13-18）：
- 佈局：Row1 ①狀態+kill switch（1.55fr）｜②淨值保證金；Row2 ③曝險｜④淨PnL(1.55fr)；Row3 ⑤同步誤差｜⑥本月量與費用。下方 tab：跟單持倉（表格）｜費用明細（接既有 billing 資料）｜成交記錄・授權歷程（disabled「即將推出」）。
- 未授權/未登入 → redirect `/strategies`。
- ①：狀態燈＋策略名＋跟單天數＋「暫停跟單」「平倉並撤銷授權」按鈕（行為接 Task 15 API；Task 15 未完成前按鈕以 feature flag 隱藏——本 task 只做 UI 與 handler 接口）＋風險護欄三條（設定 vs 目前；接近上限 → warning 色，規則：now/max ≥ 0.8 → `#e9b872`）。回撤護欄 `enabled:false` 時顯示「未啟用 · 前往設定」。
- ②：淨值＋30D＋已用/可用保證金；`available_pct < 0.05` → 黃色告警卡（NOTE 14，文案照設計稿 L715）。
- ④：淨 PnL（已扣費標注）＋已實現/未實現＋series 圖＋勝率/已結倉位/回撤/費用佔比（NOTE 16）。
- ⑤：三格（延遲中位/p95、成交價差、未同步倉位）＋三行（比例偏差、24h 遺漏＋原因、上次對帳）（NOTE 15：誠實顯示，遺漏 1 筆就寫 1 筆＋原因）。
- ⑥：月路由量＋費用累計＋日 bar chart＋成交筆數/平均費/實際費率。
- 持倉表：欄位照設計稿 L830-846；390 斷點改卡片式（Task 18 統一收）。所有 null → 「—」。
- 測試：六塊渲染假資料；低保證金告警閾值 0.05 翻轉；未登入 redirect；null 塊顯示「—」不炸。

- [ ] 測試 → 實作 → 綠 → Commit `feat: user dashboard (six blocks + positions)`

**驗收：** `npm test` 全綠。

> **主線程裁決（2026-08-28，Task 14 期間）**：語言紅線禁詞「保證」改為「保證(?!金)」式比對
> （copy.test.ts 與 redline.test.ts 兩處同步）——「保證金」為 margin 的標準中文，設計稿 §06
> 權威文案必用；禁詞意圖是擋承諾語（保證收益／保證獲利），此修正屬移除假陽性而非放寬紅線，
> 承諾語仍全數被擋。測試註解需寫明此例外的理由與殘餘風險（「保證金額」類片語會漏接，靠 review 把關）。

### Task 15 @inline：Kill switch（暫停／平倉並撤銷）⚠️ 引擎敏感

**Files:** 改 `src/spark/publicapi/app.py`；改 copytrade 引擎主循環（實作時先讀 `src/spark/copytrade/` 找每輪入口與既有受控收尾/風控 halt 機制）；新 `tests/test_kill_switch.py`；改 `web/src/app/dashboard/page.tsx` 接上按鈕；改 `deploy/RUNBOOK.md`（新增運維說明一節）。

**規格：**
- **暫停**：`POST /api/me/pause` body `{"action": "pause"|"resume"}`（登入即可、無需簽章——兩方向都只在既有授權範圍內收窄/恢復活動）。寫 per-follower 旗標檔 `FILET_EXCHANGE_DIR/<addr>/pause.json`（`{"paused": true, "ts": …, "by": "owner"}`）。引擎每輪讀旗標：paused → **跳過新開倉與加倉；允許跟隨 leader 的減倉/平倉與風控動作**。旗標**讀取失敗（IO error）→ 視為 paused ＋記 alert**（fail-safe 朝少動作；不變量 7）；旗標檔不存在 → 正常跟單。GET 狀態併入 `/api/me/dashboard.status.state`。
- **平倉並撤銷**：`POST /api/me/close-all` 需簽章（沿用 `/api/me/risk` 同形狀：`GET /api/me/close-all/message` 取伺服器簽文 → POST 附簽章）。寫請求檔 `owner_close.json`；引擎每輪偵測 → 走**既有**受控收尾機制（撤單、reduce-only 全平、halt——與 `enabled:false` 同一條路徑，不新造平倉邏輯）；完成後狀態標 `halted`。前端：二次確認 modal（列出將平倉位與「此操作不可逆」）→ 送出 → 輪詢 dashboard 顯示收尾進度 → 完成後顯示「至 Hyperliquid 官方介面移除 API wallet」指引卡（附步驟與外連；v1 不代發撤銷交易，見 0.2）。
- 紅線對照：這兩個動作都是**錢包主人主動觸發**且方向為收窄/退出，不屬「自動開啟主網寫入」；平倉走既有 reduce-only 收尾路徑（紅線 5 精神）。引擎測試全離線（假 adapter）。
- 測試：pause 旗標寫入/讀取；引擎 paused 跳過開倉、放行減倉（假訊號注入斷言下單集合）；旗標讀取 IO error → 當 paused＋alert；close-all 簽章驗證（壞簽章 401）；請求檔觸發收尾路徑（mock 既有收尾函式被呼叫）；resume 後恢復。
- RUNBOOK 增補：旗標檔位置、owner 觸發收尾後的人工 re-arm 程序（沿用既有 halt re-arm 慣例）。
- **15b（主線程裁決 2026-08-28 追加）**：watcher 建 env 時，若該 follower 的 leader 在 leaders.json 有 `max_leverage` 欄位，注入 `COPY_MAX_TARGET_LEVERAGE={max_leverage}`（沿 vault kind 20x 帽的既有注入模式）；讓策略卡「槓桿 ≤ Nx」chip 成為引擎層事實。測試：有欄位注入、無欄位不注入、vault kind 既有 20x 帽語義不被覆蓋（取兩者較嚴者）。

- [ ] 失敗測試 → 實作 → 綠 → Commit `feat: owner kill switch — pause + signed close-all`

**驗收：** `uv run pytest tests/test_kill_switch.py -q` 全綠；`uv run pytest` 全綠（既有引擎測試不壞）。

### Task 16 @inline：設定頁 /settings

**Files:** 新 `web/src/app/settings/page.tsx`；改 `copy.ts`。

**規格：** 登入後三段（未登入 redirect `/strategies`）：
1. **風控設定**：現況顯示＋修改（沿用 onboarding step3 的元件與 `/api/me/risk` 簽章流；含啟用/停用開關；熔斷中顯示 unlock 流程入口——接既有 `/api/me/risk/unlock`）。
2. **資金配置**：投入比例／槓桿上限檢視與修改（既有 `/api/me/capital` 簽章流）。
3. **授權管理**：目前 agent 位址、builder fee 上限、狀態；「暫停跟單」開關（接 Task 15 pause API）；「平倉並撤銷授權」入口（同 dashboard 的 modal 元件複用）。
- 測試：未登入 redirect；三段渲染；風控開關狀態對應 API。

- [ ] 測試 → 實作 → 綠 → Commit `feat: settings page (risk / capital / authorization)`

**驗收：** `npm test` 全綠。

---

# Phase C — 收尾

### Task 17 @inline：SEO / meta / 網域注入

**Files:** 改 `web/src/app/layout.tsx`＋各頁 `metadata`；新 `web/src/app/robots.ts`、`web/src/app/sitemap.ts`；新 `web/public/og.png`（簡潔品牌深色圖，可用 script 生成一次）；env `NEXT_PUBLIC_SITE_ORIGIN`。

**規格**（§07 L893-905）：
- 全站 title 模板：`Filet｜Hyperliquid 非保管策略跟單`；策略頁 `Filet Core｜Filet`；description 照設計稿 L896。
- canonical = `${NEXT_PUBLIC_SITE_ORIGIN}${path}`（預設 origin `https://app.filet.trade`）；OG title/description/image、`og:type website`、`twitter:card summary_large_image`；`html lang="zh-Hant"`。hreflang 不做（0.2）。
- sitemap：`/ /strategies /strategies/{各 slug 靜態列不出→只列固定路由} /docs /terms /privacy /risk /status /advanced`；robots 全放行、指向 sitemap。
- 測試：metadata 匯出存在（vitest 淺驗 title/canonical 字串）。

- [ ] 實作 → 測試綠 → Commit `feat: SEO metadata + canonical + sitemap/robots`

**驗收：** `npm test` 全綠；`grep -rn "sslip\|43\." web/src` 無 IP 殘留。

### Task 18 @inline：響應式 1024 / 390 全站 pass

**Files:** 改各頁 CSS。

**規格**（§07 L933-934）：1024——策略頁右欄→底部 sticky bar；Dashboard 六塊→兩欄。390——六塊單欄（順序：狀態/kill switch → 淨值 → PnL → 曝險 → 同步 → 費用）；持倉表→卡片式；kill switch 常駐底部；首頁/策略頁單欄；全站可點區域 ≥ 44px；表格容器 `overflow-x:auto`，body 不得橫向捲動。

- [ ] 逐頁媒體查詢 → `npm test` 綠 → Commit `feat: responsive pass (1024/390)`

**驗收：** `npm test` 全綠（斷點細節由 Task 19 截圖驗證把關）。

### Task 19（主線程）：截圖驗證

用 `webapp-testing` skill（Playwright）：起 dev server（注意 memory「Next.js 截圖驗證三坑」：殺乾淨殭屍 server、勿與 .next 互踩、登入態用 route-mock），對 `/ /strategies /strategies/core /onboarding /advanced /dashboard /settings /terms /risk /status` 各在 1440/1024/390 截圖，對照設計稿逐頁檢查；問題開清單派 builder 修，重截確認。API 以 mock 資料餵（公開端點可實跑本地後端假資料）。

### Task 20：審核（reviewer, opus）

派 `reviewer`：輸入 = `git diff feat/m2-frontend...feat/m3-redesign`＋本 plan＋測試輸出。重點：(a) 紅線 1-7 與不變量 0.3 逐條；(b) 簽章流程未被破壞；(c) 引擎 pause/close-all 的失敗路徑（fail-safe 方向）；(d) 法務頁 en 譯文語義等值抽查；(e) 假數字掃描（設計稿佔位數字不得出現在程式碼；`grep -rn "20.35\|4.28M\|10.24" web/src src` 應為 0——測試檔中的假資料除外且不得與佔位數字雷同）。Critical 全修，Warning 主線程裁決。

### Task 21（主線程）：終驗與收尾

- [ ] `uv run pytest`＋`npm test`＋`ruff` 三綠（親跑貼輸出）
- [ ] 全 repo 語言紅線 grep（copy.test.ts 既有機制＋`grep -rn "[一-鿿]" web/src/app web/src/components` 抽查非 copy 模組）
- [ ] 專案 CLAUDE.md「慣例」節補一行：前端路由結構與 copy 雙語慣例
- [ ] 更新 `deploy/RUNBOOK.md`：新 env（`NEXT_PUBLIC_SITE_ORIGIN`）、leaders.json 新展示欄位、pause/close-all 運維
- [ ] 最終 commit＋回報使用者（含：網域購買後的 DNS/Cloudflare 接線步驟清單）

---

## Backlog（本次明確不做）

agent 授權 90 天到期與續簽；`/en` 路由＋hreflang；每策略動態 OG 圖；Dashboard 成交記錄/授權歷程 tab；策略通知名單（email）；疊加對照資料源（BTC/ETH/S&P/黃金 series）；站內代發 agent 撤銷交易；資產聚合圓餅（NOTE 17 明列不做）。
