# 本地驗收 Issue Log（feat/m3-redesign）

> 範圍：2026-08-29 本地環境（localhost:3000＋真後端）交付使用者驗收後，**使用者提出**的
> 全部問題——each 含：發生了什麼（根因）、怎麼修、對應 commit、狀態。
> 設計審查階段（非本地驗收）的兩輪回饋見各自 plan：
> `2026-08-29-m3-ui-round2.md`（11 項）、`2026-08-30-m3-ui-round3.md`（D1–D14）。
> 維護慣例：新 issue 依序編號追加；狀態 ✅ 已修驗收／🔶 已修待使用者複驗／🟥 未修（待共識）。

## 第一批（2026-08-29，接真後端後首次驗收）

| # | 問題（使用者原話摘要） | 根因 | 修法 | Commit | 狀態 |
|---|---|---|---|---|---|
| I-01 | 3662 登入後「完成綁定」過不了，紅字看不出問題 | 該錢包**已是 active follower**，本不該再走綁定；且本機無 keysvc/agent 記錄使伺服器 re-check 失敗；錯誤訊息籠統 | 已跟單者選同策略 → 短路面板直達儀表板；失敗訊息逐條列出（agent/builder fee/入金） | `480e26e` | ✅ |
| I-02 | 60 天上架閘門把自家策略（58 天）擋住 | 設計稿 NOTE 04 忠實執行 vs 產品裁決（免責已足、不審來源） | `listable` 改只看 enabled＋accepting_new；「≥60 天」文案移除；Sharpe 統計閘保留 | `480e26e` | ✅ |

## 第二批（2026-08-30，六項回饋＝round-4 plan）

| # | 問題 | 根因 | 修法 | Commit | 狀態 |
|---|---|---|---|---|---|
| I-03 | EN 模式殘留繁中；繁中「Dashboard」要改中文 | 後端供給字串（風控參數 label/help、leader/capital note）只有 zh；策略 tagline 是營運設定欄位無英文 | 前端雙語對照表（封閉列舉 name→{zh,en}，fallback 後端）；zh 全站「儀表板」；leaders.json 新增可選 `tagline_en`；EN 九公開頁 CJK 掃描歸零＋enNoCjk vitest 釘住 | `557bc81` `eb4a813` | ✅ |
| I-04 | 同步誤差不得「存入主機再累積」，要從鏈上算 | 原設計含引擎本機對帳資料 | 請求時抓 leader/follower 近 24h 鏈上 fills 餵既有配對純函式；零落盤（僅 60s 記憶體快取）；配不到→「—」 | `d57b91d` | ✅ |
| I-05 | 費用明細空日不要顯示（後端側） | dashboard fees_month 逐日含空日 | 後端只回有成交/費用日期 | `d57b91d`（沿早前 `3e4d399`） | ✅ |
| I-06 | 暫停/平倉按鈕要確認彈窗防誤觸 | 暫停原為單擊直發 | 新 ConfirmDialog（取消不觸發，測試釘）；平倉本有 CloseAllModal | `557bc81` | ✅ |
| I-07 | 起訖淨值「樣本不足」不對，鏈上查得到 | 帳戶首快照為 $0（入金晚於序列起點），舊比值法誤判 | methodology 補 start/end（首非零→末值）；真實入金改查 `userNonFundingLedgerUpdates`（欄位名經主網 probe 驗證） | `94ae170` | ✅（後續演進見 I-11） |
| I-08 | explore filter 拆分＋參數自由填＋期間窗 | — | 初版做成數字輸入框＋四窗 | `5b99484` | 🔶→被 I-10 退版 |

（同批附帶：`error.tsx` 的 `<a>` 讓 production build 掛掉——上線阻斷，`72f4146` ✅）

## 第三批（2026-08-31 上午，四項）

| # | 問題 | 根因 | 修法 | Commit | 狀態 |
|---|---|---|---|---|---|
| I-09 | modal 內容貼外框 | `.modal-card` 無 padding | padding 24px＋內容 gap 18px | `e4803bb` | ✅ |
| I-10 | explore 新版「做壞了，之前的比較好」 | 自由輸入框 UX 不被接受 | 整包 revert 回布林 chip＋30D 版 | `061433a` | ✅ |
| I-11 | 起訖淨值 51→150 與圖表（~1000→1149）矛盾 | 卡片用鏈上原始帳戶值（含出入金）、圖表用 TWR×入金——同頁兩基準 | 卡片改與圖表同基準：入金×指數比值，註記「等效淨值（不含出入金）」 | `e4803bb` | ✅（基準本身另見 I-15） |
| I-12 | 費用明細空日仍出現（前端） | 前端有一段「補整月日曆」邏輯把後端已過濾的空日填回來 | 移除補日曆，直接渲染後端回應；CSV 同規則 | `e4803bb` | ✅ |

## 第四批（2026-08-31 下午）

| # | 問題 | 根因 | 修法 | Commit | 狀態 |
|---|---|---|---|---|---|
| I-13 | modal 按鈕之間也要空間 | 按鈕列無 gap | `.modal-card .step-actions` gap 12px | `bc1bc6a` | ✅ |
| I-14 | explore：期間 1D/7D/30D/全部要能用；「實盤30天＋200筆」拆兩顆 | 舊版只算 30D 窗；合併 chip | 後端還原四窗架構（portfolio 單次回應含四窗，零額外上游）；前端保留 chip 風格、qualified 拆兩顆、四鈕全開；另實測預設「回撤≤30%」閘把 top-ROI 候選 24→0＝空榜 → 回撤/集中度 chip 改預設關 | `b203d9e` `1917bc4` | 🔶 |
| I-14b | traders 頁要與 strategies 頁一致（含 CAGR、右欄）；CTA 與小字間距；雙值卡換行醜 | traders 頁缺區塊；共用樣式缺口 | 抽 FollowPanel/CagrCard/MethodologyCard 共用元件；後端 traders 補 cagr 組裝（共用函式）；雙值卡單行/斷點兩行 | `2960bb8` | 🔶 |

## 第五批（2026-08-31 晚，🟥 全部未修——依使用者指示先取得共識）

| # | 問題 | 診斷（詳見當日對話說明） | 提議修法 | 狀態 |
|---|---|---|---|---|
| I-15 | Filet Alpha 9760 數據與鏈上/參考工具不符 | 我方用 `perpAllTime`（僅 perp）序列，spot↔perp 內部轉帳被當損益（鏈上 ledger 實證）；參考工具用合併 `allTime` 序列（1002.24→1197.9 逐位吻合）＝參考工具正確 | 使用者裁決「改！」→ 策略/交易員/探索管線改合併窗（`769a328`；獨立 COMBINED_PERIODS 白名單、perp 白名單未動；follower 儀表板維持 perp）。上線驗證：19.73%／−3.09%／64.29%／1002.24→1199.99 對齊參考 | ✅ |
| I-16 | Agent key 一直過不了（金鑰服務暫時不可用） | 本機刻意未跑 keysvc（獨立金鑰服務，prod 由 systemd 跑在另一 OS user）；onboarding 的 agent 生成必經它 | 可在本機起 keysvc（新 keys 目錄）讓流程走通；⚠️ 但**不要用 3662 完成 ApproveAgent**——對已在 prod 跟單的錢包簽新 agent 授權可能頂掉正式引擎的 agent 槽位（風險：實盤斷跟）。建議用拋棄式測試錢包驗流程 | 🟥 |
| I-17 | explore 全關 filter 仍 100→27；池想擴到 300（早期 /leaderboard 可見更多） | 27＝enrich 成功且窗資料有效者；其餘鏈上資料缺席，與 filter 無關 | 裁決：池 300＋榜首常駐註記（數字後端回報）＋磁碟快照 stale-while-revalidate（`FILET_EXPLORE_CACHE_PATH`，RUNBOOK 已記）（`8cec8b3`） | 🔶 |
| I-18 | 成交記錄 7天顯示「近期沒有成交」（實際 574 筆）；第一列時間怪 | 真根因：HL fills 單次 2000 筆上限＋舊→新——90 天 2820 筆截掉最新 8 天；列表未排序放大混亂（初判「舊快取」對此二症狀不成立，收回） | 裁決：固定 30 天＋游標抓滿＋新→舊＋空態帶最近成交時間＋HL 外連（`5c5756b`） | 🔶 |

（I-17 備註：使用者當時句尾「另外，我怎麼記得」未說完，待補充。）

## 第六批（2026-08-31 深夜）

| # | 問題 | 根因/性質 | 修法 | Commit | 狀態 |
|---|---|---|---|---|---|
| I-19 | 淨值曲線「疊加對照」（BTC/ETH/S&P500/黃金）沒有實作 | v1 骨架（checkbox disabled，無資料源） | 新公開端點 `/api/public/benchmarks`（HL candleSnapshot 日線；xyz:SP500/xyz:GOLD 代號與 K 線欄位名經主網 probe 驗證；600s 快取；單標的失敗降級 null）；前端勾選 lazy 載入、同起點 rebase、細線低飽和、y 軸納入 overlay；附帶修 traders 頁「目前帳戶價值」改與曲線同源（原 perp-only 會出現 0.00 vs 曲線 15 萬的同頁矛盾）。上線驗證：四序列各 91 點、勾 BTC＋S&P500 疊加渲染正常 | `a65f387` | ✅ |


## 部署記錄（2026-09-01）

main `26d5780`（feat/m3-redesign ff-merge，200 檔）已上版正式機（sslip origin）：
rsync 照 RUNBOOK §3.2、前端 rebuild、leaders.json 補展示欄位（loader 實機驗證）、
filet-api 新增 `FILET_EXPLORE_CACHE_PATH`（unit 備份 `.bak-m3-20260901`）、
accrued 播種一筆真值。外部驗證：/ /strategies /explore /terms 皆 200、
alpha 合併基準 +20.58%、起訖 $1,002→$1,208、engine 心跳 ok。
**未重啟**：filet-follower@*、filet-keysvc（實盤紅線待使用者放行；引擎重啟前
pause/close-all 於 prod 無實效）。後續：日報每日附加 accrued（追蹤中）。

## 第七批（2026-09-01，生產環境登入後）

| # | 問題 | 根因 | 修法 | Commit | 狀態 |
|---|---|---|---|---|---|
| I-20 | 登入後出現營運/待核准 tabs，點入卻「此頁僅限管理員」 | 換錢包登入時 React Query 快取未清——前一個 admin 錢包的探測結果殘留，非 admin 錢包（builder 0x81E9…）看到 tabs、點入吃真 403。日誌實證：16:11 builder 錢包 403 全正確、16:13 換 9760 全 200 | 登入/登出全部改 `queryClient.clear()`（Header 兩處＋advanced/traders/strategies 登入流），Header 測試改斷言整包清 | `6d228e7` | ✅（prod 已驗） |
| I-21 | 設定頁壞掉（錯誤頁） | 舊版引擎心跳的 `applied` 塊**沒有 `prefs` 鍵**（undefined），settings 的 `=== null` 守門放行 → `undefined.size_tolerance` 崩潰。本機從未測過「心跳存在但形狀舊」象限；以正式機 3662 真 payload 重現後定位 | `== null` 補防＋以真 payload 形狀加回歸測試；prod 端到端驗證通過（除錯 session 用畢即刪） | `6d228e7` | ✅（prod 已驗） |
