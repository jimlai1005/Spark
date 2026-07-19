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
    navPricing: "方案",
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
} as const;

/**
 * 後端 i18n 鍵（name_key / text_key）→ 使用者可見文案。
 * 未知鍵**原樣回傳**而不是空字串或丟棄該列：後端新增功能鍵而前端還沒補文案時，
 * 顯示鍵名雖然醜，但至少使用者看得到「有這一項」；靜默隱藏會讓方案表少一列而無人察覺。
 */
export function copyForKey(key: string): string {
  return (COPY.billing.keys as Record<string, string>)[key] ?? key;
}
