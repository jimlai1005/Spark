/**
 * lib/copy.ts — 全部使用者可見文案（單一來源）。
 * 慣例：元件不得內嵌中文字串；語言紅線測試掃本檔（copy.test.ts），
 * 最終任務再全 repo grep 雙保險。
 * 文案原則：非託管講清楚；不出現 固定收益/保證/存款/代操。
 */
export const COPY = {
  common: {
    appName: "FILET",
    next: "下一步",
    retry: "重試",
    loading: "載入中…",
    notLoggedIn: "尚未登入——請先回到登入頁連接錢包。",
    backToLogin: "回登入頁",
    logout: "登出",
    nonCustodial: "Filet 永遠不會請你輸入私鑰或助記詞；所有簽署只會在你自己的錢包中完成。",
    notActivated: "這個帳號還沒開通完成——請先回到開通頁完成授權與入金。",
    goOnboarding: "前往開通",
  },
  /**
   * 導覽列文案（單一來源）。⭐ 這裡刻意收齊**全部** tab 的字面值：先前 登入／開通／
   * 績效 硬編在 Header.tsx、只有「方案」走 COPY，同一列文字有兩個來源——改文案時
   * 很容易只改到一半，而畫面上看不出來哪個字沒被改到。
   *
   * ⭐⭐ 分組的意義是 **UX 可見性，不是授權**（紅線 3）：
   *   - `PUBLIC`：任何人都看得到，頁面自己各有登入 guard。
   *   - `MEMBER`：登入後才顯示（未登入看到「訂閱管理」只會點進去吃一個「尚未登入」）。
   *   - `ADMIN`：只有後端真的放行 /api/admin/pending 的人才顯示（見 hooks.useIsAdmin）。
   * 藏起連結**不構成任何保護**——/ops 與 /admin 的每一支端點都掛著後端 `_require_admin`，
   * 手打網址進來照樣吃 403 並顯示「此頁僅限管理員」。前端只負責不把死路擺在人眼前。
   */
  nav: {
    login: "登入",
    onboarding: "開通",
    leaders: "跟單對象",
    capital: "資金配置",
    performance: "績效",
    pricing: "方案",
    billing: "訂閱管理",
    ops: "營運",
    admin: "待核准",
  },
  login: {
    subtitle: "資金留在你自己的錢包。策略照樣執行。",
    connect: "連接錢包",
    connecting: "連接中…",
    signingIn: "請在錢包中簽署登入訊息…",
    signInNote: "登入需要一筆免費的訊息簽名（不上鏈、不花費 gas）。",
    noWallet: "未偵測到瀏覽器錢包。請先安裝 MetaMask 後重新整理本頁。Filet 永遠不會請你輸入私鑰或助記詞。",
    rejected: "你在錢包中取消了簽署。準備好後再點一次「連接錢包」即可。",
    loginFailed: "登入失敗，請稍後再試。",
    walletPanelTitle: "你的錢包",
    enginePanelTitle: "Filet 引擎",
    addrLabel: "地址",
    balanceLabel: "餘額",
    strategyLabel: "策略",
    strategyValue: "網格・多幣",
    engineStateLabel: "狀態",
    engineStateIdle: "待命",
    notConnected: "尚未連接",
    pillUnauthorized: "尚未授權",
    pillAuthorized: "trade-only・無提款權",
    footnote: "每筆成交收取 0.02%（2bp）builder fee，鏈上可驗。跟單有虧損風險，過往績效不代表未來結果。",
  },
  wizard: {
    stepNames: ["連接錢包", "風險確認", "簽署授權", "入金啟用"],
    step1Title: "錢包已連接",
    step2Title: "請確認以下事項",
    risk1: "我了解跟單有虧損風險",
    risk2: "我了解 Filet 僅持有下單權限，無法動用或提領我的資金",
    risk3: "我已閱讀費用說明（每筆成交 0.02%）",
    risk4: "我聲明本錢包由我本人合法持有，資金來源合法且符合適用的反洗錢（AML）規範；我並非於禁止或限制本類服務的司法管轄區內使用本服務。",
    step3Title: "簽署兩筆授權",
    agentCardName: "ApproveAgent",
    agentCardDesc: "授權 Filet trade-only agent key 代下單，無提款權限。",
    feeCardName: "ApproveBuilderFee",
    feeCardDesc: "授權 builder fee 上限 0.1%，每筆成交依實際費率（0.02%）扣收。",
    signWithWallet: "以錢包簽署",
    stateUnsigned: "待簽署",
    stateAwaitingWallet: "請在錢包中確認…",
    stateSubmitting: "送出中…",
    stateSubmitted: "已送出，等待鏈上確認…",
    stateConfirmed: "已生效",
    stateRejected: "被拒絕",
    agentPreparing: "正在準備 agent 金鑰…",
    agentLabel: "agent 地址",
    reconnectTitle: "錢包未連接",
    reconnectHint: "你的登入仍有效，但瀏覽器錢包目前未連接（可能已上鎖）。請解鎖錢包並重新連接後繼續簽署；你的進度不會遺失。",
    reconnectButton: "重新連接錢包",
    step4Title: "入金檢查",
    // 「100 USDC」須與後端門檻同源：src/spark/config.py 的 MIN_BUILDER_BALANCE
    // （經 ApiConfig.min_user_deposit 生效於 /api/onboard/status 的 funded）。
    // 後端常數若改，這兩句文案要同步改——Task 14 review 檢查點。
    depositDetected: "已偵測到足額資金（≥ 100 USDC）",
    depositPending: "尚未偵測到足額資金（需 ≥ 100 USDC）。請將 USDC 轉入你自己的 Hyperliquid 帳戶（與登入錢包同一地址）；資金全程留在你的帳戶，Filet 無法動用或提領。",
    /**
     * ⭐ 錢卡在 spot 錢包的提示（後端 `/api/onboard/status` 的 `spot_stranded`）。
     * 存在的理由：我方只鏡像 **perp**。客戶從 CEX 提幣或走橋入金時錢會落在 spot，
     * perp 仍是 0 → 畫面只寫「尚未偵測到足額資金」，而客戶在交易所頁面上明明看得到
     * 那筆錢。這是入金漏斗上最貴的一種沉默，本區塊就是把缺的那句話補上。
     *
     * ⚠️⚠️ 這裡**永遠不會**有「幫我劃轉」按鈕：spot → perp 需要客戶的主鑰簽章，
     * 我方結構上不持有主鑰（非託管不變量）。能做的只有說明 ＋ 外部連結。
     * 任何人想在這裡加一顆代為劃轉的按鈕，都是在承諾一件做不到的事。
     */
    spotStrandedTitle: "你有資金停在 spot 錢包",
    spotStrandedBody:
      "跟單只使用永續合約（perp）帳戶，停在 spot 錢包的資金不會被跟單，也不會被"
      + "計入開通所需的金額。請在 Hyperliquid 介面把這筆資金劃轉到 perp。",
    spotStrandedAmountLabel: "spot 餘額",
    spotStrandedThresholdLabel: "提示門檻",
    spotStrandedManualNote:
      "這一步只能由你自己完成：劃轉需要你的主錢包簽章，而我們不持有你的主鑰，"
      + "因此無法替你操作，本頁也不會出現替你操作的按鈕。",
    spotStrandedLink: "前往 Hyperliquid 進行劃轉",
    spotStrandedLinkHref: "https://app.hyperliquid.xyz/balances",
    submitReview: "送出審核",
    submitted: "已送出審核。管理員核准後開始跟單；你隨時可回到本頁或績效頁查看狀態。",
    fundsWarning:
      "跟單期間請勿將資金從永續合約（perp）帳戶轉出。系統以 perp 帳戶淨值計算回撤保護，" +
      "轉出資金會被視為虧損，可能觸發保護性平倉。若要調整資金請先在此頁停止跟單。",
    errors: {
      walletRejected: "簽署被拒絕——請在錢包中重試。Filet 永遠不會請你輸入私鑰或助記詞；簽署只會在你自己的錢包中完成。",
      signerMismatch: "簽名帳號與登入帳號不符——請在錢包中切回登入時使用的帳號後重試。這筆簽名不會被送出。",
      agentUnavailable: "金鑰服務暫時不可用，請稍後重試。",
      payloadFailed: "取得待簽內容失敗，請稍後重試。",
      hlTransient: "送出授權時網路不穩——可以放心重試，重複送出同一筆簽名不會造成重複授權。",
      hlSemantic: "Hyperliquid 拒絕了這筆授權。請點「重試」重新取得待簽內容再簽一次。",
      builderPaused: "系統暫停開通中，請聯絡我們再試。",
      verifyIncomplete: "尚有條件未完成（授權或資金未確認），請依畫面提示補齊後再送出。",
    },
  },
  perf: {
    title: "帳戶狀態",
    heroLabel: "跟單狀態",
    stateReady: "已就緒",
    stateInProgress: "開通進行中",
    walletPanelTitle: "你的錢包",
    enginePanelTitle: "Filet 引擎",
    addrLabel: "地址",
    fundedLabel: "資金門檻",
    fundedOk: "已達（≥ 100 USDC）",
    fundedNo: "未達（需 ≥ 100 USDC）",
    agentLabel: "agent",
    agentNone: "尚未生成",
    approvalsLabel: "授權",
    approvalsBoth: "兩筆皆已生效",
    approvalsPartial: "尚未完成",
    goOnboarding: "前往開通",
    feePanelTitle: "費用透明",
    feeRateNote: "費率 0.02%，逐筆鏈上可驗。",
    feeUpperNote: "你簽署的授權上限為 0.1%；實際僅收 0.02%。",
    refreshNote: "本頁每 30 秒自動更新。",
    fundsWarning:
      "提醒：跟單期間請勿將資金從永續合約（perp）帳戶轉出——系統以 perp 淨值計算回撤保護，" +
      "轉出會被視為虧損並可能觸發保護性平倉。",
  },
  admin: {
    title: "待核准清單",
    empty: "目前沒有待核准的項目。",
    forbidden: "此頁僅限管理員。",
    note: "核准動作走人工 CLI（scripts/filet_activate.py），本頁唯讀。逐筆核對 builder_address。",
    cols: {
      account: "account_id",
      user: "user_address",
      agent: "agent_address",
      builder: "builder_address",
      network: "network",
      label: "label",
    },
  },
  ops: {
    title: "營運儀表板",
    eyebrow: "OPS",
    forbidden: "此頁僅限管理員。",
    /**
     * ⭐ 共用比較窗口標頭。收入對帳與每客戶損益錯開一整天曾是 Critical bug（健康帳戶
     * 被誤判 199 倍差異並告警）。後端現在讓兩個端點共用 ops.accrued_window()，
     * 但前端不盲信後端保證：窗口值不同時必須大聲說「不可相減」（工程原則 1＋3）。
     */
    window: {
      sharedTitle: "以下兩張表使用同一比較窗口",
      sharedNote:
        "收入對帳與每客戶損益取自同一組快照時刻（後端共用同一個窗口推導函式），"
        + "兩張表的 builder fee 可直接對照相減。",
      label: "比較窗口",
      mismatchTitle: "兩張表的比較窗口不一致，數字不可相減",
      mismatchBody:
        "收入對帳與每客戶損益應共用同一個窗口，實際收到的兩組窗口卻不同——"
        + "在這個狀態下把兩張表的 builder fee 相減會得到假差額（窗口錯開一天曾把健康帳戶"
        + "判成巨額漏財）。請先確認後端窗口推導是否退化，修好之前不要依本頁下對帳結論。",
      revenueLabel: "收入對帳窗口",
      customersLabel: "每客戶損益窗口",
      // 成交品質併入同一個標頭（它與另外兩張表共用同一個窗口推導函式時）。
      tradeQualityLabel: "成交品質窗口",
      tradeQualityShared: "成交品質面板也取自同一組快照時刻，三張表可並排對照。",
      tqMismatchTitle: "成交品質的比較窗口與對帳表不一致，數字不可並排相減",
      tqMismatchBody:
        "成交品質應與收入對帳共用同一個窗口推導函式，實際收到的窗口卻不同——"
        + "在這個狀態下把延遲、滑價與上面兩張表的數字並排解讀，會把兩段不同時間的"
        + "成交當成同一批。請先確認後端窗口推導是否退化，修好之前不要並排看這幾張表。",
    },
    revenue: {
      title: "收入對帳",
      note: "應收＝各客戶成交歸屬的 builder fee 加總；實收＝builder 位址鏈上累積量的今昨差（查一次，不由客戶列推導）。兩者的差額就是對帳訊號。",
      attributed: "應收（歸屬）",
      accruedDelta: "實收（鏈上增量）",
      discrepancy: "差額（實收 − 應收）",
      discrepancyPct: "差額百分比",
      threshold: "告警門檻",
      window: "對帳期間",
      rowsCounted: "納入歸屬的客戶列數",
      ok: "差額在門檻內。",
      alertTitle: "收入對帳超出門檻",
      alertBody:
        "歸屬分析與鏈上實收對不上，請先查當日 fills 明細再決定是否調整歸屬。常見原因："
        + "(1) modify 路徑的改單無 builder 欄位，該筆成交收不到費；"
        + "(2) 有非經我方路由的成交（客戶自行下單）被計入淨值卻不產生 builder fee；"
        + "(3) 鏈上累積量入帳有延遲，跨 UTC 日邊界時兩邊暫時錯位。",
      insufficient: "歷史資料累積中，需至少兩日快照才能對帳。",
      insufficientNote:
        "實收增量＝今昨兩點的差；只有一點時無從得知單日增量。此處刻意不顯示 0——"
        + "把整段累積量當成單日增量會造出天文數字的假差額。每日報表腳本會持續累積快照。",
      pctUnavailable: "應收為 0，百分比無從計算（差額請直接看金額欄）。",
      // basis_unknown：窗口對不齊時整段不顯示任何數字（連日期都不顯示）。理由與
      // insufficient 同源——算不出來就不要給數字，看得到的數字會被當成已對帳的結論。
      basisUnknown: "快照時刻無法對齊，本日對帳跳過。",
      basisUnknownNote:
        "比較窗口的兩端只能取自快照時刻；缺了或順序顛倒時，任何窗口都是猜的。"
        + "此處刻意不顯示金額與區間——錯開一天的對帳數字會叫人去查根本不存在的問題。",
    },
    customers: {
      title: "每客戶損益",
      // 2026-07-19：舊文案寫「兩張表各自獨立、不可直接相減」。後端已讓 window=accrued
      // 與收入對帳共用同一個窗口推導函式，那句話現在是錯的——過期的警語比沒有警語更糟，
      // 它會讓人以為系統還沒修好，於是繼續不敢對照兩張表。
      note: "本表與上方收入對帳使用同一比較窗口（同一組快照時刻），兩張表的 builder fee 可直接對照相減。",
      // 自由檢視窗（days）：這個模式下兩張表確實不同基準，警語在此仍然成立且必須顯示。
      daysNote:
        "自由檢視窗：本表取「現在往回 N 天」，與上方對帳窗口（快照時刻）不同基準，"
        + "兩張表的數字不可直接相減。要並排對照請切回「對帳窗口」。",
      basisUnknown: "快照時刻無法對齊，本表跳過計算。",
      basisUnknownNote:
        "對帳窗口的兩端只能取自快照時刻；缺了或順序顛倒時，任何窗口都是猜的。"
        + "此處刻意不顯示任何客戶數字——與收入對帳不同基準的損益會被讀成可相減的數字。"
        + "要先看客戶明細請改用上方的自由檢視窗（該模式與對帳表不同基準）。",
      empty: "目前沒有客戶資料。",
      rangeLabel: "統計期間",
      ranges: { accrued: "對帳窗口（同基準）", d1: "1 天", d7: "7 天", d30: "30 天" },
      manifestErrors: "manifest 有壞條目被跳過，以下項目未納入本表（其餘客戶照常顯示）：",
      rowError: "此列查詢失敗：",
      rowErrorHint: "單一客戶查詢失敗只影響該列，其餘客戶的數字仍然有效。",
      cols: {
        account: "帳號",
        address: "地址",
        fills: "成交筆數",
        notional: "路由名目",
        builderFee: "歸屬 builder fee",
        takerShare: "taker 佔比",
        accountValue: "帳戶淨值",
        subscription: "訂閱狀態",
      },
    },
    /**
     * 成交品質面板。⭐ 指標名稱用營運看得懂的話，但**不得改變其意義**：
     * `median_delay_s` 是「配對延遲中位數」——我方成交與 leader 對應成交的時間差。
     * 把它寫成「速度」或「反應時間」會讓人拿這個數字去回答另一個問題
     * （「系統快不快」），而那個問題這個數字答不了。
     */
    tradeQuality: {
      title: "成交品質",
      note:
        "配對延遲＝我方成交與 leader 對應成交的時間差；滑價以 bp 計，正值代表成交價"
        + "對跟單者不利。這幾個量與每日對帳報告呼叫同一個算式，不是另外算一份"
        + "（兩份算式漂移時，兩張表並排看不出已經不同基準）。",
      // 窗口說明隨模式切換：同一句話不能同時對兩種模式成立。
      sameWindowNote: "本表與上方收入對帳、每客戶損益取自同一組快照時刻，三張表可並排對照。",
      // ⚠️ 措辭刻意與客戶表的 daysNote 不同字（「並排」vs「直接」）：兩段文案同時
      // 出現在同一畫面上，用字一模一樣會讓「這句話在講哪張表」變成要靠位置猜。
      daysWindowNote:
        "自由檢視窗：本表取「現在往回 N 天」，與上方收入對帳的窗口（快照時刻）不同基準，"
        + "兩者的數字不可並排相減。要三張表並排對照請切回「對帳窗口」。",
      basisUnknown: "快照時刻無法對齊，本次成交品質跳過計算。",
      basisUnknownNote:
        "比較窗口的兩端只能取自快照時刻；缺了或順序顛倒時，任何窗口都是猜的。"
        + "此處刻意不顯示任何品質數字——與其他表不同基準的延遲與滑價，會被讀成可並排"
        + "對照的數字。",
      loadFailed: "成交品質載入失敗，本區塊的數字無從得知（其他區塊不受影響）：",
      empty: "目前沒有可計算成交品質的客戶。",
      summaryTitle: "跨客戶彙總（最差值，不是平均）",
      summaryNote:
        "彙總只涵蓋算得出來的那些客戶，樣本數一併列出：只給一個最差值，會讓"
        + "「10 個客戶裡只有 1 個有資料」與「10 個都有資料」在畫面上長得一模一樣。"
        + "中位數的中位數不是中位數，故此處刻意不提供平均。",
      stats: {
        followers: "客戶數",
        qualityAvailable: "有品質資料的客戶",
        teAvailable: "可配對的客戶（知道跟誰）",
        skippedAvailable: "有跳過小額資料的客戶",
        worstDelay: "最慢的配對延遲中位數（秒）",
        delaySample: "延遲樣本數（客戶）",
        worstSlippage: "最差的 taker 滑價中位數（bp）",
        slippageSample: "滑價樣本數（客戶）",
      },
      cols: {
        account: "帳號",
        fills: "我方成交筆數",
        takerShare: "taker 佔比",
        pairCount: "配對成功筆數",
        medianDelay: "配對延遲中位數（秒）",
        slippage: "taker 滑價中位數（bp）",
        skippedNotional: "跳過的小額名目",
        skippedRatio: "跳過小額佔比",
      },
      // ⭐ 三種 null 的意義完全不同，文案必須分得開——全部寫「—」等於把
      // 「不知道」「算不出來」「查詢失敗」混成同一格。
      teUnavailable: "無法配對",
      teUnavailableHint: "不知道這位客戶跟的是誰（manifest 未記 leader_address），延遲與滑價無從計算——刻意不填 0，「延遲 0 秒」讀起來是完美的跟單品質。",
      skippedUnavailable: "讀不到",
      skippedUnavailableHint: "跳過小額的記錄檔讀不到（或窗口涵蓋日只有部分天有檔）。部分天的合計會偏低，而偏低的數字讀起來正好像「引擎沒在跳過單」。",
      // ⚠️ 本任務的核心之一：比例在非整個 UTC 日的窗口下必為 null，
      // 必須顯示「此窗口無法計算」，不得顯示成 0，也不得留白（留白會被讀成 0）。
      ratioIncomparable: "此窗口無法計算",
      ratioIncomparableHint: "跳過小額以整個日曆日落檔（檔內無逐筆時間戳），成交名目卻依窗口過濾。窗口不是整個 UTC 日時，這個比例的分子與分母不同基準——那個商看起來完全像一個正常的比例，所以刻意不算。名目仍然有意義，見左欄。",
      skippedDaysLabel: "跳過小額的落檔日（比例分母的基準）",
      rowError: "此列查詢失敗：",
      rowErrorHint: "單一客戶查詢失敗只影響該列，其餘客戶的品質數字仍然有效。",
      window: "本表窗口",
    },
    /**
     * 系統健康面板。⭐⭐ 本區塊只有一條設計原則：**讀不到就說讀不到**。
     * 每一格都有「未知」狀態，而未知**絕不**被折疊成看起來健康的值——
     * 面板的讀者用它決定「要不要現在去看」，一個謊報健康的格子會讓他**不去看**，
     * 而那正是他最該去看的時刻。謊報健康比沒有面板更危險。
     *
     * 文案上的具體要求：狀態字樣「一切良好」「未觸發」「已生效」只能出現在
     * 對應的健康分支，不得出現在任何說明句裡——否則「未知時畫面不得出現健康字樣」
     * 這條就無法用測試釘住（測試會被說明文字裡的同一個詞誤傷）。
     */
    health: {
      title: "系統健康",
      note:
        "跨客戶的引擎狀態橫切。每一格都有「未知」——讀不到的項目一律標未知，"
        + "不會退化成看起來安全的值。未知看起來刺眼是刻意的。",
      loadFailed: "系統健康載入失敗，本區塊的狀態無從得知（其他區塊不受影響）：",
      empty: "目前沒有可檢查的客戶。",
      checkedAtLabel: "本次檢查時刻",
      staleAfterLabel: "心跳超過此秒數判為過期",
      // ⭐ 誠實標註：權益樣本欄是代理指標，不是 process 檢查。
      basisTitle: "「權益樣本」是代理指標，不是 process 存活檢查",
      basisBody:
        "那一欄看的是引擎最近有沒有寫過 equity 樣本（每個 cycle 一筆）。一個還在寫檔"
        + "卻已經不下單的進程，那一欄看起來會是綠燈。真正的 process 存活由 systemd 管"
        + "（RUNBOOK 的 systemctl status），不要拿那一欄取代它。",
      basisLabel: "取樣判定基準",
      basisEquitySample: "equity 樣本（引擎每 cycle 寫一筆）",
      basisUnknownPrefix: "後端回報的基準代碼（本頁尚無對應說明）：",
      /**
       * ⭐⭐ 資料來源揭露。引擎的狀態根是 0700 filet-engine，面板跑在 filet-api——
       * 面板看得到什麼，取決於它是直讀狀態根還是讀引擎發布的心跳。同一格數字，
       * 兩個來源的新鮮度差很多，不標示等於讓人拿一個不知道多舊的值下判斷。
       */
      sourceTitle: "這張表的資料從哪來",
      sourceBody:
        "引擎的狀態目錄只有引擎自己讀得到（0700），面板跑在另一個帳號下。所以面板的"
        + "資料來自引擎每個 cycle 主動發布的一份窄摘要（心跳）；少數格子面板讀得到就"
        + "直讀。每一列的「來源」欄說明它的 kill switch 與覆蓋度出自哪一邊——直讀較新鮮。",
      sourceLabel: "來源",
      sources: {
        state_root: "直讀狀態根",
        heartbeat: "引擎心跳",
        absent: "狀態根不存在",
        unreadable: "狀態根讀不到",
      },
      sourceUnknownPrefix: "後端回報的來源代碼（本頁尚無對應說明）：",
      // ⭐⭐ 心跳新鮮度。四態刻意不折疊成一個「未知」：處置完全不同。
      heartbeat: {
        ok: "心跳新鮮",
        stale: "心跳過期",
        missing: "從未收到心跳",
        unreadable: "心跳讀不到",
        unknownPrefix: "後端回報的心跳狀態代碼（本頁尚無對應說明）：",
        okHint: "引擎在門檻時間內寫過心跳，本列的 leader 與資金設定才有值。",
        staleHint: "引擎已超過門檻沒有寫心跳——可能是引擎沒在跑，也可能是它在跑但寫不進交換目錄。心跳裡的值一個都不會被顯示成現況（後端在過期時結構上就不回傳那些值）。",
        missingHint: "這個客戶從來沒有心跳檔。多半是剛啟用、引擎還沒跑過第一個 cycle，或交換目錄的子通道還沒建立（部署待辦）。",
        unreadableHint: "心跳檔存在但讀不出來（格式壞了或時刻在未來）。這一列的狀態全部無從確認。",
      },
      // 狀態字樣。⭐ 健康字樣（一切良好／未觸發／已生效／心跳新鮮）彼此不互為子字串，
      // 也不得出現在任何說明或標籤裡：測試要能用 not.toMatch 斷言「未知時不出現健康字樣」。
      state: {
        alive: "一切良好",
        stale: "樣本過期",
        engineUnknown: "樣本未知",
        tripped: "已觸發",
        armed: "未觸發",
        killswitchUnknown: "狀態未知",
        covered: "已生效",
        insufficient: "尚未生效",
        coverageUnknown: "覆蓋未知",
        /** 通用未知（告警數、積壓筆數、心跳非 ok 時的引擎現況）。 */
        unknown: "未知",
      },
      // ---------- 引擎現況（只可能來自心跳；心跳非 ok 時整區為未知） ----------
      // ⚠️ 標題刻意不含「心跳新鮮」四字：那是心跳欄的健康值字樣，出現在標題裡會讓
      // 「未知時畫面不得出現健康字樣」這條測不起來（測試會被標題誤傷）。
      engineStateTitle: "引擎現況（心跳非新鮮時一律未知）",
      engineStateNote:
        "以下是引擎上一個 cycle 實際採用的值。心跳過期或收不到時，這些格子一律「未知」"
        + "——後端在心跳過期時結構上就不回傳這些值，所以本頁不可能拿一份幾十分鐘前的"
        + "設定當成現在生效的設定。",
      engineStateCols: {
        account: "帳號",
        leader: "目前 leader",
        leaderSource: "leader 來源",
        allocated: "投入本金",
        utilization: "使用比例",
        fullEquity: "使用全額權益",
        capitalSource: "資金設定來源",
        lastCycle: "上次 cycle",
      },
      yes: "是",
      no: "否",
      // ⭐ 過期的綠燈比沒有燈更危險：過期時必須同時標「過期」與最後心跳時刻，
      // 不得把一個過期的心跳當成目前狀態呈現。
      staleTitle: "有客戶的引擎心跳已過期",
      staleBody:
        "這些客戶的引擎已經超過門檻沒有寫入心跳。心跳停了不代表部位被平掉，但代表"
        + "回撤保護與跟單都可能已經停止運作——最後心跳時刻見下表，那是「上次看到它」"
        + "的時間，不是它現在的狀態。",
      // kill switch 已觸發＝該客戶已停止跟單，視覺上必須明顯（不只是文字）。
      trippedTitle: "有客戶的 kill switch 已觸發，這些客戶已停止跟單",
      trippedBody:
        "回撤保護已在這些客戶的帳戶上熔斷並平倉，引擎不會再為他們跟單，直到人工"
        + "重新 arm。請先確認客戶已被告知，再依 RUNBOOK 處理重新啟用。",
      unknownTitle: "有格子讀不到，這些項目的狀態無從確認",
      unknownBody:
        "以下計數不是「沒有問題」，是「看不到」——最常見的原因是 filet-api 對引擎的"
        + "狀態目錄沒有讀取權限（狀態根為 0700，跨了一道權限邊界）。在讀不到的期間，"
        + "熔斷與否、回撤保護是否生效都無從得知，請優先修掉權限再看這張表。",
      cols: {
        account: "帳號",
        heartbeat: "引擎心跳",
        lastBeat: "最後心跳時刻",
        engine: "權益樣本",
        coverage: "回撤保護覆蓋",
        killswitch: "kill switch",
        alerts: "告警筆數",
        source: "來源",
      },
      stats: {
        followers: "客戶數",
        heartbeatOk: "心跳可用",
        heartbeatStale: "心跳已過期",
        heartbeatMissing: "心跳從未寫過",
        engineAlive: "權益樣本存活",
        engineStale: "權益樣本已過期",
        engineUnknown: "權益樣本讀不到",
        killswitchTripped: "kill switch 已熔斷",
        killswitchUnknown: "kill switch 讀不到",
        coverageInsufficient: "回撤保護樣本不足",
        coverageUnknown: "回撤保護讀不到",
        alertsTotal: "告警合計",
        alertsUnknown: "告警數讀不到",
        backlog: "換 leader 未套用積壓",
      },
      // 年齡的單位（元件不內嵌中文字串，沿本檔既有慣例）。
      units: { sec: "秒", min: "分", hour: "小時", day: "天" },
      ageSuffix: "前",
      lastBeatNever: "從未收到心跳",
      lastBeatNeverHint: "這個客戶的狀態目錄裡沒有任何 equity 樣本。可能是剛啟用、引擎還沒跑過第一個 cycle，也可能是引擎從來沒起來過——本頁分不出這兩者，故不猜。",
      coverageInsufficientHint: "樣本不足一小時或少於兩筆：回撤保護的 peak 還沒有足夠歷史，此時保護尚未真正發揮作用。這是一個確定的答案，不是「讀不到」。",
      rowError: "此列讀取失敗：",
      // ---------- 換 leader 積壓 ----------
      backlogTitle: "換 leader：已寫入但引擎從沒套用",
      backlogNote:
        "客戶簽了、API 收了並回覆「下一個 cycle 生效」，引擎卻從沒套用——這條鏈路"
        + "橫跨兩個進程與兩套權限，它的失敗是靜默的（兩邊的 log 都正常，客戶以為換好了）。"
        + "判定與每日對帳報告呼叫同一個函式，不是另寫一份。",
      backlogEmpty: "沒有積壓的換 leader 記錄。",
      backlogUnknownTitle: "換 leader 積壓無從得知",
      backlogUnknownBody:
        "這條鏈路查不下去（交換目錄沒設好或記錄檔讀不到），所以本頁不顯示積壓筆數。"
        + "此處刻意不顯示 0——0 會被讀成「沒有積壓」，而實際上是「不知道有沒有積壓」，"
        + "兩者正好相反。原因如下：",
      backlogCols: {
        account: "帳號",
        nonce: "nonce",
        age: "記錄年齡",
        reason: "判定原因",
      },
      reasons: {
        not_redeemed: "記錄已逾時，引擎帳本裡沒有兌現記錄——客戶簽了，引擎從沒套用",
        bad_issued_at: "記錄的 issued_at 不合法，引擎會拒收這筆變更",
        ledger_unreadable: "引擎帳本讀不到，無法確認是否已套用（不等於未套用）",
      },
      reasonUnknownPrefix: "後端回報的原因代碼（本頁尚無對應說明）：",
      manifestErrors: "manifest 有壞條目被跳過，以下項目未納入本區塊（其餘客戶照常檢查）：",
    },
    /**
     * 訂閱對帳文案。⭐ 清單標題一律寫「危害是什麼」而不是欄位名：admin 看到
     * 「local_active_stripe_not」不會知道要做什麼，看到「還在服務卻收不到錢」會。
     */
    subscriptions: {
      title: "訂閱對帳",
      note:
        "本地 billing 表 vs Stripe 真實狀態。webhook 是本地表的唯一寫入者，掉一包就永久漂移；"
        + "這張表是唯一的察覺途徑。",
      detectOnly:
        "本區塊只偵測、不修正：以 Stripe 為準覆寫本地會直接改動計費與權益，屬人工決策。"
        + "看到漂移請先到 Stripe 後台確認真相，再決定怎麼處理。",
      clean: "四類漂移皆為零，本地與 Stripe 一致。",
      counts: {
        inSync: "一致",
        drift: "漂移合計",
        stripe: "Stripe 訂閱數",
        local: "本地記錄數",
        superseded: "已被取代（回鍋客戶）",
      },
      supersededNote: "「已被取代」是同一客戶的歷史訂閱，不是漂移，不計入漂移合計。",
      truncatedTitle: "樣本不完整，本區塊結論不可信",
      truncatedBody:
        "Stripe 訂閱清單已達 1000 筆上限，未取得完整樣本。此時「Stripe 查無」類的判定"
        + "可能全是假漂移（訂閱其實存在，只是沒被取回）。請勿依本區塊停用任何客戶——"
        + "先縮小查詢範圍或直接到 Stripe 後台逐筆確認。",
      lists: {
        stripeActiveLocalNot: {
          title: "客戶付了錢卻沒拿到權益（危害最高）",
          desc: "Stripe 顯示訂閱生效，本地卻沒有記錄或非 active——客戶正在付費但拿不到付費功能。優先處理。",
        },
        localActiveStripeNot: {
          title: "還在提供服務卻收不到錢（漏財）",
          desc: "本地 active，Stripe 上非 active 或查無此訂閱——服務照給，費用卻沒進來。",
        },
        statusMismatch: {
          title: "兩邊狀態不一致（需人工判讀）",
          desc: "兩邊都有記錄但狀態對不上（例如本地 past_due、Stripe 已取消），權益判定會依本地值，可能不是當前真相。",
        },
        orphanStripe: {
          title: "對不到本地帳號的 Stripe 訂閱（非 active）",
          desc: "多為外部手建或測試殘留。非 active 故無立即權益影響，但會讓計費報表對不起來。",
        },
      },
      empty: "無",
      cols: {
        account: "account_id",
        local: "本地狀態",
        stripe: "Stripe 狀態（正規化）",
        raw: "Stripe 原始值",
        subId: "stripe_subscription_id",
        matchedBy: "命中方式",
      },
    },
  },
  /**
   * 計費／方案文案。⭐ 誠實揭露原則（產品要求，非 UI 細節）：
   * 1. 免費層**不受任何限制**——不限本金、不限跟單；付費解鎖的是多 leader 與比例調整。
   *    文案不得暗示免費層有隱藏限制。
   * 2. 尚未推出的功能（後端 shipped=false）一律標「開發中」，不得讓使用者以為
   *    付款即可使用（pricing 頁測試釘住此條）。
   * 3. 價格未拍板時後端 price_display 為 null → 顯示「價格待定」，絕不顯示 0。
   */
  billing: {
    // navPricing 已移入 COPY.nav.pricing（導覽文案單一來源，2026-07-19）。
    pricingEyebrow: "PRICING",
    pricingTitle: "方案與定價",
    pricingSubtitle: "免費層不限本金、不限跟單。付費解鎖的是多 leader 與跟單比例調整。",
    // 後端只送 i18n 鍵（billing.py 的 name_key / text_key），文案一律在這裡。
    // 鍵名與 src/spark/publicapi/billing.py 的 _F_* 常數同源，改一邊要改兩邊。
    keys: {
      "plans.free.name": "免費",
      "plans.pro.name": "專業",
      "plans.feature.copytrade": "跟單執行（不限本金、不限跟單筆數）",
      "plans.feature.killswitch": "回撤保護（kill-switch 自動平倉）",
      "plans.feature.multiLeader": "同時跟隨多位 leader",
      "plans.feature.ratioSlider": "跟單比例自訂",
    },
    priceFree: "免費",
    priceTbd: "價格待定",
    priceTbdNote: "價格尚未定案，確定後會在本頁公告。",
    perMonth: "／月",
    // ⭐ 未推出功能的標示：視覺與文字都必須讓人一眼看出「現在還不能用」
    unshippedBadge: "開發中，敬請期待",
    unshippedNote: "標示為「開發中」的功能尚未推出，訂閱後也還不能使用；推出前請以免費層的功能為準。",
    includedAria: "已包含",
    excludedAria: "未包含",
    subscribe: "訂閱",
    subscribing: "前往付款頁…",
    comingSoon: "即將開放",
    disabledNote: "訂閱功能即將開放",
    freePlanAction: "免費使用中，無需訂閱",
    loginFirst: "請先登入",
    // 訂閱管理頁
    manageEyebrow: "BILLING",
    manageTitle: "訂閱管理",
    currentPlanLabel: "目前方案",
    statusLabel: "訂閱狀態",
    // 狀態標籤（chip 用）。none 刻意用「未訂閱」而非「尚無訂閱」：後者是空狀態
    // 區塊的標題，同一畫面出現兩個一樣的字串會讓人分不出哪個是狀態、哪個是提示。
    status: {
      active: "訂閱中",
      past_due: "付款逾期",
      canceled: "已取消",
      none: "未訂閱",
    },
    planNameActive: "專業",
    planNameFree: "免費",
    pastDueNote: "最近一次扣款未成功，請更新付款方式，否則訂閱會被取消。",
    manage: "管理訂閱",
    managing: "前往付款入口…",
    manageNote: "更新付款方式與取消訂閱都在 Stripe 的付款入口完成——我們不自建，也不經手你的卡片資料。",
    noSubscription: "尚無訂閱",
    noSubscriptionNote: "你目前使用免費層：不限本金、不限跟單。",
    goPricing: "查看方案",
    errors: {
      alreadyActive: "你已有生效中的訂閱，不需重複訂閱。請到本頁的「管理訂閱」調整。",
      noRecord: "尚無訂閱記錄，請先從方案頁訂閱。",
      notEnabled: "訂閱功能即將開放，請稍後再試。",
      checkoutFailed: "無法開啟付款頁，請稍後再點一次。重複點擊不會產生重複訂閱。",
      portalFailed: "無法開啟付款入口，請稍後再點一次。",
      loadFailed: "載入方案資料失敗，請重新整理本頁。",
    },
  },
  /**
   * leader 選擇頁文案。⭐ 本頁的核心需求是**誠實揭露**，不是 UI 細節：
   * 1. 目前可取得的統計**只有規模與當下曝險**（帳戶淨值／名目部位／未實現損益／持倉數），
   *    **沒有任何績效指標**——沒有報酬率、沒有回撤、沒有勝率。文案不得把這些欄位講成
   *    像是報酬，也不得暗示「淨值大 ＝ 賺得多」。欄位名一律沿用後端原名的直譯。
   * 2. 數字取自每日快照，時點必須顯示；沒有時點就不顯示數字（見頁面 statsShown）。
   * 3. 跟單者的實際結果**低於** leader 的數字（延遲、滑價、資金規模差異），
   *    leader 數字是上界不是期望值——這句話必須出現在選擇按鈕旁邊，不是頁尾小字。
   * 4. 換 leader 有真實成本（收斂部位＝平舊開新，付實際交易成本）且下一個 cycle 才生效，
   *    確認對話框必須寫明這兩件事。
   */
  leaders: {
    eyebrow: "LEADERS",
    title: "選擇 leader",
    subtitle: "選定後，引擎會依這位 leader 的部位變化在你自己的帳戶執行跟單。",
    // ⭐ 誠信要求 1／2：先講清楚「這些數字是什麼、不是什麼」，再讓人看數字。
    statsScopeTitle: "以下數字是規模與當下曝險的快照，不是績效",
    statsScopeBody:
      "本頁顯示的統計只有帳戶淨值、名目部位總額、未實現損益與持倉數——那是某個時點的"
      + "資產負債切面。本頁沒有報酬率、沒有回撤、沒有勝率：這些指標本頁尚未提供，"
      + "請不要在這裡尋找它們，也不要把「淨值大」讀成「賺得多」或把未實現損益讀成"
      + "已經到手的結果。",
    /**
     * ⭐ 接上績效後的版本。原本那句「本頁沒有報酬率、沒有回撤」在顯示績效時就是
     * 假話——揭露文案必須描述**畫面上實際有什麼**，否則它自己就成了第一個不誠實的
     * 元素。顯示層依「這次到底有沒有畫績效」二選一，兩種狀態各自為真。
     */
    statsScopeTitleWithPerf: "規模／曝險與績效是兩類不同的數字，請分開讀",
    statsScopeBodyWithPerf:
      "每張卡片分成兩區。上半部是規模與當下曝險——帳戶淨值、名目部位總額、未實現損益、"
      + "持倉數，那是某個時點的資產負債切面，不是績效：請不要把「淨值大」讀成「賺得多」，"
      + "也不要把未實現損益讀成已經到手的結果。下半部才是績效（報酬率與回撤），"
      + "它有自己的資料充足度與極限，逐項標在數字旁邊。本頁沒有勝率，"
      + "也刻意不提供依績效排序的清單——依績效排序等同於本頁在替你推薦，"
      + "而這些數字的限制（見下）還不足以支撐推薦。",
    statsDayLabel: "快照日期",
    statsAsOfLabel: "統計時點",
    statsStaleNote: "數字取自每日快照，不是即時值——請以上方時點為準。",
    statsUnavailableTitle: "統計暫時不可用，本頁不顯示任何數字",
    statsUnavailableNote:
      "此時刻意不顯示金額與筆數：沒有時點的數字會被當成即時數字讀，比沒有數字更危險。"
      + "leader 清單本身不受影響，仍可正常選擇。",
    statsNoTimestampTitle: "統計缺少時點，本頁不顯示任何數字",
    statsNoTimestampNote:
      "快照沒有附上日期與產生時間，無從得知這批數字是今天還是三天前的。此處刻意不顯示"
      + "金額與筆數（沿營運頁「窗口對不齊就不給數字」的既有嚴格度）。",
    leaderStatsMissing: "這位 leader 不在最新快照中，沒有可顯示的數字。",
    cols: {
      accountValue: "帳戶淨值",
      totalNtlPos: "名目部位總額",
      unrealizedPnl: "未實現損益",
      positionCount: "持倉數",
    },
    colHints: {
      accountValue: "leader 帳戶在快照時點的權益規模。",
      totalNtlPos: "快照時點所有部位的名目總額，代表曝險大小，不是損益。",
      unrealizedPnl: "快照時點未平倉部位的浮動損益，隨行情變動，不是已實現的結果。",
      positionCount: "快照時點持有的部位數量。",
    },
    /**
     * 績效區文案。⭐ 三件事在這裡是硬性的，不是版面偏好：
     * 1. 上界警語與回撤取樣警語**貼著各自的數字**（不是頁尾小字）；
     * 2. 資料充足度（涵蓋天數／樣本點數）與數字同時出現——涵蓋 31 天與涵蓋 2 年的
     *    報酬率長得一模一樣，可信度卻天差地遠；
     * 3. 「資料不足」要被畫成一個**有內容的狀態**（說明＋還差幾天），不是幾格空白。
     * 後端 `performance_notes` 有給的段落一律以後端原文優先（單一來源：計算的極限與
     * 呈現的警語必須出自同一處）；這裡的 *Fallback 只在後端沒給時遞補——
     * ⭐ 數字缺了就不顯示，警語缺了要補上：少一句警語等於把數字送出去而沒有它的極限。
     */
    perf: {
      sectionTitle: "績效（perp）",
      sectionNote:
        "以下是報酬率與回撤，與上方的規模／曝險是兩類不同的東西——規模大不等於賺得多。",
      basisFallback:
        "基準為 perp only，與跟單實際鏡像的範圍一致；不含 spot 與 vault 餘額。",
      // ⭐ 誠信要求：leader 報酬是上界不是期望值，必須貼著績效數字。
      upperBoundFallback:
        "這些是 leader 本人的數字，是跟單者的上界，不是你的期望值：滑價、進出場延遲、"
        + "資金規模與槓桿上限都不同，leader 在流動性差的幣種大額進出時你拿不到同樣價格。"
        + "你的實際結果會低於這裡的數字。",
      // ⭐ 誠信要求：MDD 為 15 分鐘取樣，系統性低估——必須與 MDD 數字在一起。
      mddFallback:
        "回撤由 15 分鐘取樣的權益指數推得，取樣間隔之內的來回完全看不見，因此系統性低估，"
        + "應讀作回撤的下界而不是精確值。回撤看起來特別小的 leader 尤其要存疑。",
      // ⭐ 誠信要求：資料充足度必須可見。
      sufficiencyTitle: "資料充足度",
      sufficiencyFallback:
        "涵蓋天數與樣本點數決定這些數字有多可信：涵蓋 31 天與涵蓋 2 年的報酬率在畫面上"
        + "長得一模一樣，可信度卻天差地遠。窗越短、樣本點越少，數字越接近雜訊。",
      coveredDaysLabel: "涵蓋天數",
      sampleCountLabel: "樣本點數",
      skippedLabel: "略過的間隔",
      daysUnit: "天",
      pointsUnit: "點",
      /**
       * 窗名。⭐ 刻意在標籤裡保留後端原名（perpMonth／perpAllTime）：中文名是為了
       * 好讀，原名才是對得上後端的那個東西——只留中文名，日後改窗定義時沒人對得起來。
       * 窗的**實際**長度一律以旁邊的涵蓋天數為準（月窗未必真有 30 天）。
       */
      periods: {
        perpMonth: "月窗（perpMonth）",
        perpAllTime: "全期（perpAllTime）",
      } as Record<string, string>,
      // 揭露層級：每一級是一個**狀態**，名字要讓人看得出資料還缺什麼。
      tierLabel: "揭露層級",
      tiers: {
        annualizable: "可年化：涵蓋 90 天以上",
        window_return: "僅窗口報酬：涵蓋 30 天以上，尚未達年化門檻",
        pnl_only: "僅累積損益：涵蓋不足 30 天，不提供任何百分比",
        insufficient: "資料不足：本窗不顯示任何數字",
        unknown: "無法判讀的揭露層級：本窗不顯示任何數字",
      },
      /**
       * ⭐⭐ 資料不足的**標記文案**（2026-07-19 揭露模型改版）。
       * 改版後薄資料的數字**照樣顯示**，警示全靠這幾句話——它們不是補充說明，
       * 而是這些數字唯一的警示載體，因此一律緊貼數字、不折疊、不放頁尾。
       * `{days}` 由顯示層以實際涵蓋天數替換（單一來源：後端送來的天數）。
       */
      markerThinTitle: "資料不足",
      markerThinBody:
        "僅涵蓋 {days} 天，低於 30 天門檻。這個百分比的雜訊遠大於訊號，"
        + "不應讀作這位 leader 的實力。",
      /** 後端標了不足卻沒給天數（不該發生）：警示照樣要出現，只是說不出天數。 */
      markerThinBodyNoDays:
        "涵蓋期間低於 30 天門檻（後端未提供確切天數）。這個百分比的雜訊遠大於訊號，"
        + "不應讀作這位 leader 的實力。",
      /**
       * 年化的外推說明。⭐ **無條件**顯示（不是只在不足時才給）：年化本質上就是
       * 外推，一個 365 天以外的窗算出來的年化都是把一段期間的結果放大或縮小。
       */
      markerExtrapolatedBody: "由 {days} 天資料外推為年化值。",
      /** 不足 90 天的年化：外推倍率大到必須額外說出它的數學後果。 */
      markerExtrapolatedThinBody:
        "由 {days} 天資料外推為年化值，遠低於 90 天門檻。外推是把這段期間的結果"
        + "複利放大到一年，短期的運氣與雜訊會被同倍放大——這個數字不是對未來一年的"
        + "預期，把它當成預期是這一頁最容易犯的錯。",
      /** 年化欄位整體的降階說明（與視覺上的弱化同時存在，兩者互為備份）。 */
      annualizedSecondaryNote:
        "年化是外推出來的推算值，因此排在期間報酬之後、字級也刻意較小："
        + "上方的期間報酬是實際發生過的事，年化不是。",
      // 「為什麼這裡沒有數字」——把空格變成有內容的狀態。
      gapAnnualizedTitle: "尚未提供年化報酬率",
      gapAnnualizedBody:
        "年化需要至少 90 天的涵蓋期間。這不是「年化為零」，而是這段資料還不足以年化——"
        + "把一段短資料強行年化，會把雜訊放大成一個看起來像結論的數字。"
        // ⭐ 2026-07-19 改版後，年化「整組缺席」的成因換了：後端現在連 7 天也照樣
        // 年化（附標記），所以缺席只剩一種原因——權益曾歸零，年化在數學上無解。
        // 不寫出這一點，這段話會停在一個已經不成立的舊理由上。
        + "另一種可能是這段期間的權益曾歸零或更差，年化在數學上沒有實數解，"
        + "此時不存在任何可標記的數字，因此整個欄位不畫。",
      gapWindowTitle: "尚未提供報酬率與回撤",
      gapWindowBody:
        "窗口報酬率與回撤需要至少 30 天的涵蓋期間。這不是「報酬為零」或「沒有回撤」，"
        + "而是這段期間太短，百分比的雜訊遠大於訊號，因此刻意不給。",
      gapShortfallLabel: "距門檻還差",
      insufficientTitle: "資料不足以計算此窗的指標",
      insufficientBody:
        "這個窗連累積損益都不顯示。樣本太少時算出來的數字（例如只有一個資料點時恆為零的"
        + "損益）在畫面上是一個有意義的結論——「這段期間沒賺沒賠」——但那只是資料不足的假象。",
      reasonLabel: "原因碼",
      // 績效整體不可得（沿 stats_available=false 的嚴格度：不畫任何數字）。
      unavailableTitle: "績效資料暫時不可用，本頁不顯示任何報酬率或回撤",
      unavailableBody:
        // ⭐ 這段刻意連一個數字字元都不出現（沿 stats_available=false 那塊的既有嚴格度）：
        // 說明「為什麼沒有數字」的文字裡冒出數字，本身就是最容易被誤讀成資料的東西。
        "每日快照尚未包含績效來源（或尚未產生）。規模與曝險不受影響、照常顯示；"
        + "此處刻意不以佔位符號或零值填充——沒有數字，比一個看起來正常的假數字安全。",
      leaderMissingTitle: "這位 leader 沒有績效資料",
      leaderMissingBody:
        "最新快照沒有這位 leader 的績效窗，因此本卡片不顯示任何報酬率或回撤。"
        + "這代表「沒有資料」，不代表「績效為零」。",
      windowMissingTitle: "這個窗沒有資料",
      // schema 漂移：後端宣告的層級高於實際附上的資料 → 出聲，不靜默降級。
      degradedNote:
        "後端宣告的揭露層級高於實際附上的資料，缺漏的欄位已略去不顯示。"
        + "這是資料來源的異常，請回報這個狀況。",
      cols: {
        cumPnl: "累積損益",
        twr: "窗口報酬率（TWR）",
        maxDrawdown: "最大回撤（MDD）",
        annualizedReturn: "年化報酬率",
      },
      colHints: {
        cumPnl: "本窗期間的累積損益金額，不是百分比，也不是你跟單會拿到的金額。",
        twr: "時間加權報酬率：本窗期間的權益成長，已排除出入金的影響。",
        maxDrawdown: "本窗期間權益指數自高點的最大跌幅；由 15 分鐘取樣推得，系統性低估。",
        annualizedReturn:
          "把本窗報酬換算成年度速率的結果，不是對未來一年的預測，也不是承諾。",
      },
    },
    // ⭐ 誠信要求 5：必須貼著選擇按鈕出現。
    upperBound:
      "跟單者的實際結果會低於 leader 的數字：進出場延遲、滑價與資金規模差異會持續侵蝕"
      + "跟單結果。leader 的數字是上界，不是你的期望值。跟單有虧損風險。",
    /**
     * ⭐ 「我目前跟誰」（/api/me/leader）。本頁要客戶簽署一份**換掉**現有 leader 的
     * 授權，所以「現在跟的是誰」是這個決定的左半邊——缺了它，客戶是在不知道現況的
     * 情況下被要求簽署變更。
     *
     * ⚠️ 這一區的錯誤方向不對稱，文案必須照著寫：
     * - 讀不到（503、網路）＝「暫時讀不到」，**不是**「你沒在跟單」。後者是一句我們
     *   沒有根據的斷言，會讓一個正在跟單的客戶以為資金沒在動而不去看它。
     * - `engine_default`＝**已經在跟單**，只是對象由部署決定；與 `not_activated`
     *   （還沒啟用）是完全不同的處境，兩者的文案不得互相代用。
     * 狀態的**內文**一律用後端 note 原文（單一來源）；本檔只提供標題與欄位標籤。
     */
    current: {
      title: "你目前跟隨的 leader",
      loading: "讀取你目前的跟隨狀態…",
      leaderLabel: "目前跟隨",
      failedTitle: "目前無法讀取你目前的跟隨狀態",
      failedNote:
        "這次查詢失敗只影響本區塊的顯示：它不會改變你的跟單設定，也不代表你沒有在跟單。"
        + "本頁其餘功能不受影響；請重新整理本頁再試一次。",
      /** 沒有 leader 位址可顯示的三種狀態各有一個標題；內文用後端 note 原文。 */
      noneTitles: {
        engine_default: "你已啟用跟單，但尚未指定 leader",
        not_activated: "你的帳號尚未啟用跟單",
        indeterminate: "目前無法確認你的跟隨狀態",
      },
      /** 後端回了預期外的狀態碼時的標題——原樣顯示狀態碼，不猜它是哪一種。 */
      noneTitleFallback: "目前沒有可顯示的跟隨對象",
      statusLabel: "狀態碼",
      pendingTitle: "有一筆已簽署、尚未生效的換 leader",
      pendingLabel: "將換到",
      pendingIssuedAtLabel: "簽署時間",
    },
    select: "選擇此 leader",
    // ⭐ 誠信要求 6：確認對話框必須寫明成本與生效時機。
    confirmTitle: "確認要換到這位 leader？",
    confirmLeaderLabel: "要換到的 leader",
    confirmCost:
      "換 leader 有真實成本：生效時引擎會把你的部位收斂到新 leader——平掉目前的部位、"
      + "依新 leader 開新部位。這是真實成交，會產生實際的交易成本（taker 費用與滑價）。",
    confirmTiming:
      "不是立即生效：授權記錄下來後，於引擎的下一個 cycle 生效。引擎在套用前會自己"
      + "重新驗證你的簽章與白名單，驗證不過就不會套用。",
    confirmSignNote:
      "接下來會請你在錢包簽署一則訊息（不上鏈、不花費 gas）。"
      + "Filet 永遠不會請你輸入私鑰或助記詞；簽署只會在你自己的錢包中完成。",
    confirmOk: "確認並簽署",
    confirmCancel: "取消",
    signing: "請在錢包中簽署…",
    submitting: "送出中…",
    doneTitle: "已授權，於引擎的下一個 cycle 生效",
    // 付費閘門：可用性一律由後端 shipped 旗標驅動，前端不硬編、也不自行放行。
    gateBadge: "開發中，敬請期待",
    gateTitle: "換 leader 尚未推出，本頁目前唯讀",
    gateNote:
      "「同時跟隨多位 leader」在方案目錄中標示為開發中，因此本頁只能瀏覽，不能送出選擇。"
      + "推出與否由後端旗標決定，前端不自行放行；功能推出後本頁會自動開放。",
    gateUnknown:
      "無法確認這個功能是否已推出（方案目錄載入失敗），本頁暫不開放送出。請重新整理再試。",
    empty: "目前沒有可選擇的 leader。",
    loadFailed: "載入 leader 清單失敗，請重新整理本頁。",
    /**
     * 自訂 leader 區塊（2026-07-27 spec：用戶輸入任意 wallet address）。
     * ⭐ 誠實揭露的門檻比精選卡片**更高**：這裡的位址未經平台審核、沒有績效快照。
     * 三件事是硬性的：
     * 1. 「平台不背書」講在送出之前——專屬聲明 checkbox 是送出的前置閘門
     *    （仿 wizard AML attestation 的純前端閘門），未勾不得進確認框。
     * 2. 預覽卡明說那是「查詢當下的鏈上切面」，用途是確認沒貼錯位址——不是績效、
     *    不是推薦；無績效快照要說「沒有資料」，不畫 0、不畫錯誤。
     * 3. 准入被拒的文案按後端 reason 分類碼對應（機器碼分流，不對人話字串比對），
     *    每句都講清楚使用者現在能做什麼，不洩內部治理細節。
     */
    custom: {
      title: "自訂 leader",
      subtitle:
        "除了上方精選清單，你也可以輸入任意錢包位址，跟隨你自己研究過的交易者。"
        + "此路徑的 leader 未經平台審核、亦無績效快照，是否值得跟隨由你自行判斷。",
      leaderboardLabel: "研究對象可參考 Hyperliquid 官方排行榜（外部連結，另開新分頁）：",
      leaderboardLinkText: "app.hyperliquid.xyz/leaderboard",
      inputLabel: "leader 錢包位址",
      inputPlaceholder: "0x…",
      inputHint:
        "0x 開頭＋40 個十六進位字元。查詢會到鏈上讀取此帳戶在 Hyperliquid 的 perp "
        + "活動；若該位址尚無活動，仍可先完成配置（進場後自動開始跟單）。",
      formatError: "位址格式不正確：須為 0x 開頭＋40 個十六進位字元。",
      check: "查詢",
      checking: "查詢中…",
      previewTitle: "鏈上預覽",
      previewNote:
        "以下是查詢當下的鏈上切面，用途是讓你確認沒有貼錯位址——它不是績效，也不是推薦。",
      previewAccountValue: "帳戶權益",
      previewAccountValueHint: "該位址在查詢當下的帳戶權益（鏈上即時讀值，非每日快照）。",
      previewPositionCount: "持倉數",
      previewPositionCountHint: "該位址在查詢當下持有的部位數量。",
      noPerfTitle: "無績效資料",
      noPerfBody:
        "自訂 leader 不在平台的績效快照中，本區無法顯示報酬率與回撤。這代表「沒有資料」，"
        + "不代表「績效為零」——請自行研究這個位址的過往表現後再決定。",
      alreadyListedBadge: "此位址已在精選清單",
      /**
       * ⭐⭐ already_listed 的兩個子情形（2026-07-27）。後端的 `already_listed` 對
       * **paused**（accepting_new=false）的精選 leader 也回 true，但 `/api/leaders`
       * 目錄用 `is_selectable` 過濾、**不含** paused leader——對 paused 說「這個位址
       * 就是上方精選清單中的 leader」是一句假話（上方根本沒有它）。所以「在不在上方」
       * 一律以**上方實際渲染的那份清單**為準（同源比較，工程原則 1），不由旗標推論。
       */
      alreadyListedNote:
        "這個位址就是上方精選清單中的 leader，後續流程與選擇精選 leader 相同。"
        + "由於你走的是自行輸入位址的路徑，以下聲明仍需勾選。",
      alreadyListedNoteNotShown:
        "這個位址已在平台的 leader 名單中，但目前未列於上方的可選清單（例如已暫停接受"
        + "新跟單者），因此本頁沒有它的卡片可以對照。後續流程與選擇精選 leader 相同；"
        + "由於你走的是自行輸入位址的路徑，以下聲明仍需勾選。",
      /**
       * ⭐ 精選位址的績效**在上方那張卡片上**——這一區就不能再說「沒有資料」。
       * 同一頁對同一個位址講兩套話，比少講一次更傷：使用者會相信離他比較近的那一句。
       */
      perfAboveTitle: "績效資料在上方的卡片",
      perfAboveBody:
        "這個位址在上方的精選清單中，報酬率與回撤（含資料充足度與各項警語）顯示在那張"
        + "卡片上，本區不重複顯示，也不另外算一份。",
      perfNotShownTitle: "本頁沒有這個位址的績效卡片",
      perfNotShownBody:
        "這個位址目前未列於上方的可選清單，因此本頁沒有它的績效卡片可以對照；本區同樣"
        + "不顯示報酬率與回撤。這代表「本頁沒有資料」，不代表「績效為零」——"
        + "請自行研究這個位址的過往表現後再決定。",
      // ⭐ 專屬風險聲明：未勾選不得送出（純前端閘門，仿 wizard AML attestation）。
      attestation: "我了解此為未審核 leader，非平台精選，績效與風險自負。",
      select: "選擇此自訂 leader",
      /** 准入拒絕文案，按後端 reason 分類碼對應（app.py `_admission_reject`）。 */
      reasons: {
        invalid_format: "位址格式不正確：須為 0x 開頭＋40 個十六進位字元。",
        self_follow: "這是你自己的登入位址——不能跟單自己。",
        // ⭐ 2026-07-27 拆旗標後 leader_disabled **只**代表安全撤銷（enabled=false）：
        // 文案收窄為撤銷專屬，不再提「停止接受新客戶」（後者改為 accepting_new=false
        // 放行帶警示，見 notAcceptingNewWarning）。
        leader_disabled:
          "該位址已被平台安全撤銷（leader 出事），無法跟隨。",
      },
      // ⭐ accepting_new=false（例行下架，2026-07-27）：**放行但警示**——不是拒絕、
      // 不擋 checkbox／送出。leader 仍在跟（引擎照跟正在跟的人），只是平台已標記
      // 為暫不收新客。客戶堅持要跟就讓他跟（使用者裁決）。
      notAcceptingNewWarning:
        "⚠️ 此 leader 目前未開放接受新跟單者（例行下架或名額調整）。"
        + "你仍可完成配置並跟隨，但這是平台已標記的狀態。",
      // ⚠️ 鏈上無活動（exists=false）不再是拒絕（2026-07-27 裁決），改為**警示但放行**：
      // leader 可能尚未進場，客戶可先完成配置，進場後引擎自動開始跟。文案講清楚「現在
      // 沒動靜、但仍可先配置」，不是錯誤、不擋送出。
      noActivityWarning:
        "此位址目前在 Hyperliquid 上無 perp 交易活動（權益為 0 且無持倉）。"
        + "若該 leader 尚未進場，你可以先完成跟單配置——待其進場後引擎會自動開始跟單。"
        + "也請再確認一次位址沒有貼錯。",
      // 查詢是唯讀動作，失敗要明說「什麼都沒變」——與換 leader 的失敗文案同一原則。
      previewFailed: "查詢失敗，這次查詢沒有改變你的跟單設定。請稍後再按一次「查詢」。",
      // 伺服器回聲位址 ≠ 輸入位址：預覽資料不可信，要求停手（沿 leaderMismatch 的嚴格度）。
      echoMismatch:
        "已中止：伺服器回傳的預覽對象與你輸入的位址不符，畫面上不顯示這份預覽。"
        + "請不要繼續操作，並回報客服。",
      /**
       * 確認框與成功通知裡顯示的名稱。⭐ 只用於**真正的**自訂位址（不在平台名單中）：
       * already_listed 的位址一律沿用它在精選清單裡的名稱，查不到才退回位址縮寫——
       * 把一個有名字的精選 leader 顯示成「自訂 leader」，等於在確認框那一步把使用者
       * 正在授權的對象講得比實際更陌生。
       */
      entryName: "自訂 leader",
    },
    errors: {
      messageFailed: "取得待簽原文失敗，你的 leader 沒有被變更。請稍後再試一次。",
      walletRejected: "你在錢包中取消了簽署。這筆授權沒有送出，你的跟單設定沒有任何變動。",
      signerMismatch:
        "簽名帳號與登入帳號不符——請在錢包中切回登入時使用的帳號後重試。這筆簽名不會被送出。",
      // ⭐ 這不是一般的失敗，是「伺服器要你授權的對象，跟你點的那個不是同一個」。
      // 文案的工作是讓使用者**停手並回報**，不是讓他再按一次：能發生這件事就代表
      // 後端或連線可能已遭竄改，重試只會讓他再看到同一份被動過的授權請求。
      // 因此刻意不寫「請稍後再試」「請重新整理」這類會誘導重試的句子。
      leaderMismatch:
        "已中止：伺服器回傳的授權對象與你選擇的 leader 不符。這筆授權沒有進到你的錢包、"
        + "沒有被簽署、沒有送出，你的跟單設定完全沒有變動。"
        + "請不要重試——重試會再次拿到同一份不符的授權請求。"
        + "請截圖本畫面並回報客服，在問題釐清之前，不要簽署任何換 leader 的請求。",
      notSelectable: "這位 leader 目前不可選擇（可能剛下架）。請重新整理本頁後再試。",
      forbidden: "只能變更自己帳號的 leader。請重新登入後再試。",
      // 429（查詢額度用完）：後端對 preview／待簽原文／送出共用同一個 per-session
      // 額度，所以連續查很多位址之後按送出也可能撞到。要明講「沒有被變更」與
      // 「可以直接重試」——通用的 submitFailed 會說「上一筆簽名已作廢」，
      // 但這個情況根本還沒送到驗簽，講作廢是錯的。
      rateLimited:
        "查詢與送出的次數在短時間內太多，這次沒有送出，你的 leader 沒有被變更。"
        + "請等約一分鐘後重新操作一次。",
      // 送出失敗一律明講「沒有被變更」：這是安全關鍵動作，使用者必須知道現在的狀態。
      submitFailed:
        "送出授權失敗，你的 leader 沒有被變更。請重新操作一次——會重新取得待簽原文並"
        + "請你重簽一次（上一筆簽名已作廢，不會被重複套用）。",
      retry: "重新操作",
      // leader 不符時按鈕改成純關閉：把「重新操作」擺在一個要求使用者停手的訊息
      // 底下，等於用按鈕把文案的結論收回去。
      dismiss: "關閉",
    },
  },
  /**
   * 資金配置頁文案。⭐ 本頁調的兩個數字**直接乘進部位大小**，所以誠實揭露的門檻
   * 比其他頁更高：
   * 1. 「調高使用比例＝提高曝險與清算風險」必須貼著滑桿出現，不是頁尾小字。
   * 2. 兩個欄位要用客戶看得懂的話解釋，並講明**兩者相乘**才是部位規模的基準。
   * 3. 後端目前**沒有**查詢「目前生效值」與「上次變更時間」的端點——本頁明說沒有，
   *    不拿滑桿的預設位置假裝是現況（沿 /leaders「本頁無法標示目前跟隨中」的既有作法）。
   * 4. 確認步驟必須寫明「下一個 cycle 生效」「不會立即再平衡」「提高曝險」三件事。
   * 5. 數值超界或小數位過多一律**阻擋並明說**，前端不四捨五入、不夾取：靜默修正
   *    等於改掉使用者要簽署的數字。
   */
  capital: {
    eyebrow: "CAPITAL",
    title: "資金配置",
    subtitle:
      "設定你要讓引擎拿去跟單的本金，以及其中實際動用的比例。資金全程留在你自己的錢包，"
      + "Filet 無法動用或提領——這裡調的是「用多少去跟」，不是把錢交給誰。",

    // ⭐ 誠信要求 2：用客戶看得懂的話解釋兩個欄位，並講明兩者相乘的意義。
    glossaryTitle: "這兩個數字各是什麼",
    glossaryAllocatedTerm: "投入本金",
    glossaryAllocatedBody:
      "你打算讓這套系統拿去跟單的金額（以 USDC 計）。它是一個你自己設定的基準值，"
      + "不是你的帳戶餘額，設定它不會把任何一塊錢移出你的錢包。",
    glossaryUtilizationTerm: "使用比例",
    glossaryUtilizationBody:
      "上面那筆本金當中，實際被拿去建立部位的比例。20% 代表引擎只用其中五分之一去跟，"
      + "其餘不參與。",
    glossaryProductTitle: "兩者相乘才是部位規模的基準",
    glossaryProductBody:
      "投入本金 × 使用比例 = 引擎為你建立部位時的資金基準。例如 10,000 USDC × 25% ＝ "
      + "2,500 USDC。實際的名目部位還會隨 leader 的持倉結構與槓桿而異，可能大於這個基準——"
      + "它是計算的起點，不是部位金額的上限。",
    basisLabel: "目前這組設定的資金基準",
    basisNote: "＝ 投入本金 × 使用比例（顯示用的估算值，實際下單量由引擎計算）。",

    // ⭐ 誠信要求 1：必須與滑桿同一個容器。
    riskTitle: "調高使用比例＝提高曝險與清算風險",
    riskBody:
      "使用比例直接乘進部位大小：從 20% 調到 100%，部位規模變成五倍，帳面波動與清算距離"
      + "同步縮成五分之一。調高之前請先確認你承受得起對應的虧損；跟單有虧損風險，"
      + "調高比例會把這個風險等比放大。",

    // ⭐ 誠信要求 3：後端沒有的東西就說沒有，不拿預設值冒充現況。
    currentUnknownTitle: "本頁無法顯示你目前生效的設定值與上次變更時間",
    currentUnknownNote:
      "後端目前沒有提供查詢「目前生效的投入本金與使用比例」的端點，也沒有提供上次變更"
      + "時間（已簽署的設定記錄只有引擎讀得到）。與其顯示一個可能是錯的數字，本頁選擇"
      + "不顯示。下方欄位與滑桿的起始位置是本頁的預設值，**不是**你目前生效的設定。",
    currentSessionLabel: "本次登入期間你在本頁送出過的最後一組值",
    currentSessionNote:
      "這只是本頁記得的送出記錄，不等於引擎目前生效的值（送出後要到下一個 cycle 才套用，"
      + "且引擎會自己重新驗證）。重新整理本頁後這筆記錄就會消失。",

    // 表單
    formTitle: "調整設定",
    allocatedLabel: "投入本金（USDC）",
    allocatedHint: "最多 2 位小數；請填半形數字，不要加逗號或單位。",
    allocatedPlaceholder: "例如 10000",
    utilizationLabel: "使用比例",
    utilizationHint: "可調範圍 1%～100%，以 1% 為一格。",
    utilizationDefaultNote:
      "滑桿的起始位置是本頁預設，不代表你目前生效的設定（見上方說明）。",
    // ⭐ 拖動不觸發簽章：調好之後按一次「套用」才走流程，避免無謂的錢包彈窗。
    applyNote: "拖動滑桿不會送出任何東西。調到你要的位置後，按下方按鈕才會進入確認與簽署。",
    apply: "套用這組設定",

    // 前端輸入驗證（阻擋並明說，不靜默修正）
    invalid: {
      empty: "請先輸入投入本金。",
      format: "投入本金請填半形數字（可含小數點），不要加逗號、空白或單位。",
      // ⭐ 明講「不會自動幫你四捨五入」：靜默修正是這一段刻意不做的事。
      decimals:
        "投入本金最多 2 位小數。本頁不會自動幫你四捨五入——那等於改掉你要簽署的數字，"
        + "請自行改成你真正要的值。",
      not_positive: "投入本金必須大於 0（後端不接受 0 或空的本金，也不會替你補一個預設值）。",
      too_large: "投入本金超出可表示的範圍，請確認是不是多打了幾位數。",
      utilization: "使用比例必須落在 1%～100% 之間。",
    },

    // ⭐ 誠信要求 4：確認步驟。三件事都必須在按下確認之前看得到。
    confirmTitle: "確認要送出這組資金設定？",
    confirmDiffTitle: "前後對照",
    confirmFromLabel: "目前生效值",
    confirmFromUnknown: "無法顯示（後端未提供查詢端點）",
    confirmToLabel: "你要授權的新值",
    confirmExactLabel: "實際送出並簽署的字串",
    confirmExactNote:
      "請在錢包的簽署畫面上核對這兩個數字——錢包顯示的原文由伺服器產生，上面這串就是"
      + "它應該要有的內容。",
    confirmTiming:
      "不是立即生效：授權記錄下來後，於引擎的下一個 cycle 生效。引擎在套用前會自己"
      + "重新驗證你的簽章與數值範圍，驗證不過就不會套用。",
    confirmNoRebalance:
      "不會立即再平衡：調整後現有部位不會被立刻平掉或補上。引擎讓部位隨 leader 的後續"
      + "動作自然收斂，避免一次無謂的 taker 成本——所以調低比例之後，你不會馬上看到部位縮小。",
    confirmSignNote:
      "接下來會請你在錢包簽署一則訊息（不上鏈、不花費 gas）。"
      + "Filet 永遠不會請你輸入私鑰或助記詞；簽署只會在你自己的錢包中完成。",
    confirmOk: "確認並簽署",
    confirmCancel: "取消",
    signing: "請在錢包中簽署…",
    doneTitle: "已授權，於引擎的下一個 cycle 生效",

    // 付費閘門：可用性一律由後端 shipped 旗標驅動，前端不硬編、也不自行放行。
    gateBadge: "開發中，敬請期待",
    gateTitle: "跟單比例自訂尚未推出，本頁目前唯讀",
    gateNote:
      "「跟單比例自訂」在方案目錄中標示為開發中，因此本頁只能瀏覽與試算，不能送出設定。"
      + "推出與否由後端旗標決定，前端不自行放行；功能推出後本頁會自動開放。",
    gateUnknown:
      "無法確認這個功能是否已推出（方案目錄載入失敗），本頁暫不開放送出。請重新整理再試。",

    errors: {
      messageFailed: "取得待簽原文失敗，你的資金設定沒有被變更。請稍後再試一次。",
      walletRejected: "你在錢包中取消了簽署。這筆授權沒有送出，你的資金設定沒有任何變動。",
      signerMismatch:
        "簽名帳號與登入帳號不符——請在錢包中切回登入時使用的帳號後重試。這筆簽名不會被送出。",
      // ⭐ 這不是一般的失敗，是「伺服器要你簽的數字，跟你調的不是同一組」。
      // 文案的工作是讓使用者**停手並回報**，不是讓他再按一次：能發生這件事就代表
      // 後端或連線可能已遭竄改，而這兩個數字直接決定他的曝險倍數。
      // 因此刻意不寫「請稍後再試」「請重新整理」這類會誘導重試的句子。
      valuesMismatch:
        "已中止：伺服器要你簽署的設定值，與你在本頁調整的數值不符。這筆授權沒有進到你的"
        + "錢包、沒有被簽署、沒有送出，你的資金設定完全沒有變動。"
        + "請不要重試——重試會再次拿到同一份不符的授權請求。"
        + "請截圖本畫面並回報客服，在問題釐清之前，不要簽署任何資金設定的請求。",
      outOfRange:
        "數值超出允許範圍：投入本金必須大於 0，使用比例必須落在 0（不含）到 1（含）之間。"
        + "系統不會替你把值調到範圍內——那會變成你簽了一個數字、系統執行另一個。",
      forbidden: "只能變更自己帳號的資金設定。請重新登入後再試。",
      submitFailed:
        "送出授權失敗，你的資金設定沒有被變更。請重新操作一次——會重新取得待簽原文並"
        + "請你重簽一次（上一筆簽名已作廢，不會被重複套用）。",
      retry: "重新操作",
      // 設定值不符時按鈕改成純關閉：把「重新操作」擺在一個要求使用者停手的訊息
      // 底下，等於用按鈕把文案的結論收回去。
      dismiss: "關閉",
    },
  },
} as const;

/**
 * 後端 i18n 鍵（name_key / text_key）→ 使用者可見文案。
 * 未知鍵**原樣回傳**而不是空字串或丟棄該列：後端新增功能鍵而前端還沒補文案時，
 * 顯示鍵名雖然醜，但至少使用者看得到「有這一項」；靜默隱藏會讓方案表少一列而無人察覺。
 */
export function copyForKey(key: string): string {
  return (COPY.billing.keys as Record<string, string>)[key] ?? key;
}
