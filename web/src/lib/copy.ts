/**
 * lib/copy.ts — 全部使用者可見文案（單一來源）。
 * 慣例：元件不得內嵌中文字串；語言紅線測試掃本檔（copy.test.ts），
 * 最終任務再全 repo grep 雙保險。
 * 文案原則：非託管講清楚；不出現 固定收益/保證/存款/代操。
 */

/**
 * 語言切換鈕的原生語言名稱（「繁中」「EN」）——刻意**不**跟著 useCopy() 的當前語言
 * 翻譯：這是「切到哪個語言」的鈕本身，不管畫面目前是中文還是英文，鈕的字樣永遠是
 * 該語言的原生名稱。獨立於 COPY_ZH/COPY_EN 之外（不進 DeepString 鏡射結構、
 * 不受 en 值零 CJK 的測試約束），單一來源仍在本檔，元件只 import 這個常數，
 * 不在 Header.tsx 內嵌中文字面值。
 */
export const LANG_LABELS: Record<"zh" | "en", string> = { zh: "繁中", en: "EN" };

/**
 * M3 round3 Task 9（R2 P2）：首頁主推策略卡「目前跟單人數」顯示門檻——低於此值
 * 代表冷啟動人數太少，整欄不渲染，改顯示連續實盤天數（見 `app/page.tsx`）。
 * ⭐ 定義在這裡而非 `app/page.tsx`：Next.js app router 的 `page.tsx` 只允許固定的
 * 幾個具名匯出（`default`/`metadata`/...），額外具名匯出會讓 `next build` 的路由
 * 型別檢查報錯（`OmitWithTag<...>` 不滿足 `{ [x: string]: never }`）。
 */
export const FOLLOWER_COUNT_DISPLAY_MIN = 10;

export const COPY_ZH = {
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
   * 導覽列文案（單一來源）。⭐ 這裡刻意收齊**全部** tab 的字面值——元件不得內嵌
   * 中文字面值，改文案只改這一處。
   *
   * ⭐⭐ 2026-08-28 導覽狀態機重寫（Task 7，顧問 P1）：未登入與已登入不再是同一份
   * tab 清單加減，而是**兩組完全不同的頁籤**——未登入時「綁定錢包」「跟單」不
   * 渲染，`nav.onboarding`／`nav.leaders`／`nav.login`（原「開始」自我連結）三個
   * 舊 key 隨之刪除，改採：
   *   - 未登入：策略／運作方式／安全性／文件 ＋ 單一 CTA「查看策略與風險」。
   *   - 已登入：Dashboard／策略／設定／文件 ＋ 跟單狀態 pill ＋ 地址縮寫。
   * `ADMIN` 分組沿用（後端真的放行 /api/admin/pending 才顯示，見 hooks.useIsAdmin）。
   * 分組的意義是 **UX 可見性，不是授權**（紅線 3）：藏起連結**不構成任何保護**——
   * /ops 與 /admin 的每一支端點都掛著後端 `_require_admin`，手打網址進來照樣吃
   * 403 並顯示「此頁僅限管理員」。前端只負責不把死路擺在人眼前。
   */
  nav: {
    ariaLabel: "頁面切換",
    strategies: "策略",
    explore: "探索",
    how: "運作方式",
    security: "安全性",
    docs: "文件",
    dashboard: "Dashboard",
    settings: "設定",
    ops: "營運",
    admin: "待核准",
    // Task 2（2026-08-29）：CTA 從「查看策略與風險」改為登入入口——按錢包連接／簽署
    // 進度顯示簡短狀態字；成功後依 dashboard 狀態導向 dashboard 或 strategies。
    cta: "登入",
    ctaConnecting: "連接中…",
    ctaSigning: "簽署中…",
    langToggleLabel: "語言切換",
    /**
     * 跟單狀態 pill（三態）。⭐ 2026-08-28：資料源尚未接上（Task 13 的
     * `/api/me/dashboard` 摘要才有真實旗標）——`/api/me` 目前只有
     * `{address, account_id}`，沒有跟單狀態欄位可推。Header 目前恆顯示
     * `pillNotFollowing`，不得偽造成 `pillFollowing`（工程原則：讀不到 ≠ 安全態，
     * 寧可顯示保守值也不要顯示一個沒有根據的綠燈）。
     */
    pillFollowing: "跟單中",
    pillPaused: "已暫停",
    pillNotFollowing: "未跟單",
    /**
     * 保證金告警 pill（Task 6，R2 P2「Dashboard 保證金」）：登入態且可用保證金
     * 低於 `EquityCard.LOW_MARGIN_THRESHOLD`（5%）時，header 同步顯示這顆 pill，
     * 點擊導向 /dashboard——與 EquityCard 卡片內的告警文案同一資料源
     * （`/api/me/dashboard` 的 `equity.available_pct`），不是另外算一份。
     */
    marginAlertPill: "保證金偏低",
  },
  /**
   * Footer 文案（Task 7）。四欄＋系統狀態燈——資料來自 `/api/public/status`
   * （見 lib/publicApi.ts），只讀一次不輪詢。法務欄連向 /terms /privacy /risk
   * 三頁（Task 12 建立）與 contact@filet.trade；免責文字對照設計稿 §03 L412。
   */
  footer: {
    brandTagline: "Hyperliquid 上的非保管策略執行。資金留在你的錢包，授權可隨時撤銷。",
    statusOk: "系統運作正常",
    statusDegraded: "系統部分功能異常",
    statusUnknown: "狀態未知",
    productTitle: "產品",
    productStrategies: "策略",
    productHow: "運作方式",
    productFees: "費用",
    productDocs: "文件",
    verifiableTitle: "可驗證",
    verifiableLeaderAccounts: "Leader 帳戶（鏈上）",
    verifiableBuilderFee: "Builder code 費率",
    verifiableMethodology: "績效方法論",
    verifiableStatus: "系統狀態",
    legalTitle: "法務與聯絡",
    legalTerms: "服務條款",
    legalPrivacy: "隱私政策",
    legalRisk: "風險揭露",
    legalContact: "contact@filet.trade",
    disclaimer:
      "跟單交易具有虧損風險，過往績效不代表未來結果，你可能損失全部投入資金。"
      + "Filet 不提供投資建議，不保管用戶資產。所有簽署僅在你自己的錢包中完成；"
      + "Filet 永遠不會索取私鑰或助記詞。",
    copyright: "© 2026 Filet",
  },
  login: {
    // ⭐ 2026-07-30：單一入口產品敘事。eyebrow／heroTitle 是登入頁的論點；
    // journey 是「綁定錢包」的權威定義——兩項授權的名稱（跟單授權／builder code
    // 收款授權）只在這裡寫一次，其餘地方（wizard 的簽署卡）是同一件事的技術實作。
    eyebrow: "FILET",
    heroTitle: "在 Hyperliquid，貼上地址就能跟單",
    subtitle: "資金留在你自己的錢包。策略照樣執行。",
    journey: [
      {
        title: "連結錢包並登入",
        body: "用你自己的瀏覽器錢包連接，並簽署一筆免費的登入訊息——不上鏈、不花費 gas。",
      },
      {
        title: "完成兩項授權",
        body: "在錢包中簽署兩筆訊息，同樣不上鏈、不花費 gas：跟單授權（Filet 僅能代你下單，"
          + "無法動用或提領資金）與 builder code 收款授權（每筆成交依你簽署的費率上限收取，鏈上可驗）。",
      },
      {
        title: "貼上 leader 地址，開始跟單",
        body: "到跟單頁貼上任一 Hyperliquid 錢包地址，通過准入檢查後即可開始跟單。",
      },
    ],
    connect: "連接錢包",
    connecting: "連接中…",
    signingIn: "請在錢包中簽署登入訊息…",
    signInNote: "登入需要一筆免費的訊息簽名（不上鏈、不花費 gas）。",
    noWallet: "未偵測到瀏覽器錢包。請先安裝 MetaMask 後重新整理本頁。Filet 永遠不會請你輸入私鑰或助記詞。",
    rejected: "你在錢包中取消了簽署。準備好後再點一次「連接錢包」即可。",
    loginFailed: "登入失敗，請稍後再試。",
    // walletPanelTitle／enginePanelTitle／addrLabel／strategyLabel／strategyValue／
    // pillUnauthorized／pillAuthorized 與 /onboarding 的 Boundary 顯示共用（單一來源），
    // 本頁不再渲染 Boundary，但這些鍵仍是 onboarding 頁的實際依賴，不得刪除。
    walletPanelTitle: "你的錢包",
    enginePanelTitle: "Filet 引擎",
    addrLabel: "地址",
    strategyLabel: "策略",
    strategyValue: "網格・多幣",
    pillUnauthorized: "尚未授權",
    pillAuthorized: "trade-only・無提款權",
    footnote: "每筆成交收取 0.02%（2bp）builder fee，鏈上可驗。跟單有虧損風險，過往績效不代表未來結果。",
  },
  wizard: {
    // Task 10：統一 onboarding 四步（取代舊版「連接錢包／風險確認／簽署授權／入金啟用」）。
    stepNames: ["選擇策略", "連接與授權", "風險限制", "確認"],
    backButton: "上一步",
    // ---- step 1：選擇策略（進頁即完成態，見 onboarding/page.tsx） ----
    step1Eyebrow: "已選擇策略",
    step1Back: "返回改選",
    step1AdvancedLabel: "進階模式（無背書）",
    step1AdvancedBody: "此位址不在 Filet 精選清單內，未經任何盡職審查或背書——請自行評估風險後再繼續。",
    step1NotFound: "找不到這個策略——它可能已下架或網址有誤，請回到策略列表重新選擇。",
    // ---- step 2：連接與授權（StepSign 簽署＋StepDeposit 入金檢查合併於此步） ----
    step2Title: "連接與授權",
    step2Body: "兩筆錢包簽名都不上鏈、不花 gas；資金入帳後即可進入下一步。",
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
    // ⭐ Task 10：不再是獨立步驟的標題（原「04・入金檢查」），改成 step 2 內部
    // 一個子區塊的小標——StepDeposit 現在巢狀顯示於 StepSign 之下（見 StepConnect）。
    step2DepositSubheading: "入金檢查",
    // ⭐ 門檻金額**不再寫進文案**（2026-07-30）。原本這兩句各自硬編「100 USDC」，
    // 而真正生效的門檻在後端（src/spark/config.py 的 MIN_BUILDER_BALANCE 經
    // ApiConfig.min_user_deposit）——兩份常數改一邊忘另一邊，症狀是畫面寫 100、
    // 系統擋在別的數字。現在數字一律取自 `/api/onboard/status` 的 `min_deposit`
    // 與 `perp_account_value`（見 StepDeposit 的餘額區塊），文案只講「該做什麼」。
    depositPerpLabel: "perps 帳戶餘額",
    depositThresholdLabel: "開通門檻",
    depositShortfallLabel: "還差",
    depositDetected: "已偵測到足額資金，你的 perps 帳戶餘額已達開通門檻。",
    // ⭐ 明講「perps 錢包」而不只是「你的 Hyperliquid 帳戶」（2026-07-30）：
    // 後端的 funded 判定查的是 perp 帳戶淨值，錢放在 spot 錢包一樣被擋。原文案
    // 只說「轉入你自己的 Hyperliquid 帳戶」，客戶照做（轉到 spot）卻依然過不了，
    // 而畫面沒有任何線索指向真正的原因。
    depositPending:
      "尚未偵測到足額資金。請把 USDC 存入你自己 Hyperliquid 帳戶的「perps（永續合約）」"
      + "錢包（與登入錢包同一地址）——跟單只使用 perps 錢包，放在 spot 錢包的資金"
      + "不計入開通門檻，也不會被跟單。若你的資金已經在 spot 錢包，請在 Hyperliquid "
      + "介面把它劃轉到 perps。資金全程留在你的帳戶，Filet 無法動用或提領。",
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
    // 2026-07-30 移除人工審核（auto-activate watcher）：完成綁定＝進啟用佇列，
    // 選定 leader 後自動開始跟單，文案不再出現「審核」。
    submitReview: "完成綁定",
    // ⭐ Task 10：不再顯示「前往選擇 leader」的站外導出連結——leader（即所選策略）
    // 已在 step 1 選定，本頁 step 4 送出時會直接完成選定，不需要使用者中途跳出精靈
    // 另外操作（舊版 goFollow 連結因此移除；見 StepDeposit.tsx 檔頭）。
    submitted: "已偵測到入金，正在為你進入下一步…",
    // ⚠️ 2026-07-31 更新：引擎已上線出入金自動校正（follower flow correction）——
    // 出入金不再被回撤保護視為虧損，「視為虧損」的舊句子刪除。但轉出的提醒本身
    // 仍然成立：perp 淨值是跟單規模的分母，轉出會即時等比縮小所有跟單倉位。
    fundsWarning:
      "跟單期間出入金會自動校正回撤基準，不會被視為虧損；但系統以 perp 帳戶淨值" +
      "計算跟單規模，轉出資金會即時等比縮小你的跟單倉位（產生實際交易成本）。" +
      "大額調整資金前建議先在此頁停止跟單。",
    // ---- step 3：風險限制（投入比例／槓桿上限本地調整＋回撤自動停止 opt-in 簽章） ----
    step3Title: "設定你的風險限制",
    step3Body: "這些數值會寫進 agent 授權範圍。Filet 無法超出這些限制下單；你之後可以隨時調低。",
    step3NextButton: "前往費用與風險確認",
    ddSaving: "簽署送出中…",
    // ⭐ Task 10b（主線程裁決 2026-08-28）：投入比例改真實簽章流，槓桿改唯讀。
    capitalEffectiveLabel: "目前生效",
    capitalPendingLabel: "已提交，待引擎套用",
    leverageInfoPrefix: "本策略槓桿上限 ",
    leverageInfoSuffix: "（平台層強制，非使用者可調）",
    // ---- step 4：費用與風險確認（NOTE 12：三條 checkbox 逐字） ----
    step4Title: "確認",
    step4Body: "最後一步：確認費用與風險揭露，全部勾選後即可完成開通。",
    step4CheckLoss: "我理解可能虧損",
    step4CheckFee: "我理解費用為 0.02% 每筆",
    step4CheckRevoke: "我理解可隨時撤銷",
    step4SubmitButton: "確認並開始跟單",
    step4Submitting: "送出中…",
    errors: {
      walletRejected: "簽署被拒絕——請在錢包中重試。Filet 永遠不會請你輸入私鑰或助記詞；簽署只會在你自己的錢包中完成。",
      signerMismatch: "簽名帳號與登入帳號不符——請在錢包中切回登入時使用的帳號後重試。這筆簽名不會被送出。",
      // 2026-08-29 M3 round2 Task 3：舊文案只說「暫時不可用」，看不出既有授權
      // 沒事——場景是錢包先前已完成鏈上授權，keysvc 掛掉時整卡只剩這句話，
      // 客戶會誤以為要重新授權。改講清楚「按重試」而非「自動重試」
      // （進場 effect 只跑一次，是否自動重試要與實作一致，不能空講）。
      agentUnavailable: "金鑰服務暫時不可用，請點「重試」再試一次；你已完成的簽署與授權仍然有效，不需要重做。",
      // agent_conflict：keystore 與 DB 狀態不一致、自癒失敗——與上面的
      // agent_exists（後端視為成功）不同義，需要人工介入，不會靠重試自己好。
      agentConflict: "你的 agent 金鑰狀態不一致，請聯絡我們處理；已完成的鏈上授權不受影響。",
      payloadFailed: "取得待簽內容失敗，請稍後重試。",
      hlTransient: "送出授權時網路不穩——可以放心重試，重複送出同一筆簽名不會造成重複授權。",
      hlSemantic: "Hyperliquid 拒絕了這筆授權。請點「重試」重新取得待簽內容再簽一次。",
      builderPaused: "系統暫停開通中，請聯絡我們再試。",
      verifyIncomplete: "尚有條件未完成（授權或資金未確認），請依畫面提示補齊後再送出。",
      // ⭐ 2026-08-29 裁決 6：完成綁定失敗改逐條列出未滿足條件，不再用單句籠統紅字。
      verifyAgentPending: "agent 授權尚未生效",
      verifyBuilderFeePending: "builder fee 尚未核准",
      verifyNotFunded: "入金未達門檻",
      contentMismatch: "伺服器回傳的內容與你送出的設定不符，為安全起見已中止——請重新整理頁面後再試一次。",
      submitFailed: "送出失敗，請稍後重試；尚未成功的簽署不會被重複計費或重複套用。",
    },
    // ⭐ 2026-08-29 裁決 6：已跟單同策略的短路面板（bodyPrefix/bodySuffix 圍住
    // 動態策略名，沿 leverageInfoPrefix/Suffix 既有慣例，不用字串樣板替換）。
    alreadyFollowingTitle: "已在跟單此策略",
    alreadyFollowingBodyPrefix: "你目前已在跟單「",
    alreadyFollowingBodySuffix: "」，不需要重新開通。",
    alreadyFollowingDashboardCta: "前往 Dashboard",
    alreadyFollowingOtherCta: "查看其他策略",
  },
  admin: {
    title: "待核准清單",
    empty: "目前沒有待核准的項目。",
    forbidden: "此頁僅限管理員。",
    note: "啟用由 auto-activate watcher 自動處理（選定 leader 後約一分鐘生效）；"
      + "本頁唯讀，用於觀察佇列——長期滯留的條目代表該用戶尚未選 leader 或啟用失敗"
      + "（查 journalctl -u filet-auto-activate）。",
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
   * leader 選擇頁（跟單頁）文案。⭐⭐ 2026-07-30 大幅減法：付費閘門與精選 leader
   * 目錄卡片（含全部績效揭露）全部下架，本頁收斂成單一互動——貼上任一 Hyperliquid
   * 錢包位址，通過准入檢查即可跟單。`getLeaders` 目錄仍在背景呼叫，但只用來判斷
   * 貼上的位址是不是平台已知的 leader（`custom.alreadyListedBadge`），不再渲染成
   * 卡片格線，因此規模／曝險／績效欄位一併下架。
   *
   * 誠實揭露原則沒有因為產品變小而放寬：
   * 1. 換 leader 有真實成本（收斂部位＝平舊開新，付實際交易成本）且下一個 cycle 才
   *    生效，確認對話框必須寫明這兩件事。
   * 2. 跟單者的實際結果**低於**貼上的這個地址的表現（延遲、滑價、資金規模差異）——
   *    這句話必須貼著送出按鈕，不是頁尾小字。
   */
  /**
   * `/advanced` — 進階模式（Task 11，設計稿 §03 進階模式卡＋NOTE 05；自舊版
   * `/leaders` 遷移，該路由現改為單純 redirect）。
   *
   * ⭐⭐ 與舊版 `/leaders` 最大的行為差異：選定位址後**不在本頁簽章**，而是
   * `router.push` 帶 `advanced:0x…` 進 `/onboarding`——真正的「選定 leader＋
   * 伺服器簽文」流程已在 Task 10 統一到 onboarding step 4（`StepConfirm`，
   * 原樣沿用 `runLeaderSelectFlow`／`postLeaderSelect`，結構未改）。因此舊版
   * 「目前跟隨的 leader」面板與風控設定區塊**不在本頁**——前者本 plan 未指定
   * 新去處（既有落地缺口，未列入 Task 11 範圍）；後者已明確搬到 Task 16
   * `/settings`（沿用 onboarding step3 元件）。本頁保留的四項既有功能：
   * 地址輸入、准入檢查、鏈上預覽（含績效欄位）、以及選定動作本身
   * （只是動作內容從「簽章」變成「導向開通流程」）。
   */
  advanced: {
    eyebrow: "進階模式",
    title: "進階模式：貼上任一位址跟單",
    subtitle: "跳過精選清單，直接對任一 Hyperliquid 錢包位址開始跟單設定。",
    // ⭐ 安全揭露：與 wizard 開通頁的同義句（COPY.wizard.fundsWarning）各自成立、
    // 互不取代（沿舊版 /leaders 原樣保留，copy.test.ts 語言紅線測試釘住這條）。
    fundsWarning:
      "提醒：跟單期間出入金會自動校正回撤基準，不會被視為虧損；但轉出資金會即時"
      + "等比縮小跟單倉位。大額調整前建議先停止跟單。",
    /** ⭐ NOTE 05：頁首顯著聲明，勾選前地址輸入框 disabled（見 page.tsx gate）。 */
    gate: {
      title: "在你開始之前",
      body:
        "進階模式下，Filet 不對此位址的策略品質、風控或存續做任何背書——"
        + "你貼上的位址未經平台額外審核，是否值得跟隨由你自行判斷。",
      checkboxLabel: "我理解 Filet 不對此地址的策略品質、風控或存續做任何背書。",
    },
    /**
     * 未登入：顯示說明＋登入 CTA，不 redirect（進階用戶的直達入口）。
     * ⭐ M3 round3 Task 8（R2·P1）：舊版兩個藍框堆疊、下半頁全空——合併為單一卡
     * （風險確認 + disabled 地址輸入框 + 登入按鈕），`exploreExit` 是旁置的
     * 「或先看精選策略 →」出口（連 `/strategies`），讓用戶不是只有登入一條路。
     */
    notLoggedIn: {
      title: "請先登入以繼續",
      body: "進階模式需要先連接錢包並登入，才能查詢位址與繼續開通流程。",
      cta: "連接錢包並登入",
      connecting: "連接中…",
      signing: "請在錢包中簽署登入訊息…",
      exploreExit: "或先看精選策略 →",
    },
    // ⭐ 誠信要求：必須貼著送出按鈕出現。
    upperBound:
      "跟單者的實際結果會低於 leader 的數字：進出場延遲、滑價與資金規模差異會持續侵蝕"
      + "跟單結果。leader 的數字是上界，不是你的期望值。跟單有虧損風險。",
    /**
     * 位址輸入區。⭐ 這是本頁**唯一**的入口（原「自訂 leader」；2026-07-27 spec
     * 的准入預覽流程原樣適用）。三件事是硬性的：
     * 1. 「平台不背書」講在查詢之前——上方 gate checkbox 是輸入框的前置閘門。
     * 2. 預覽卡明說那是「查詢當下的鏈上切面」，用途是確認沒貼錯位址——不是推薦。
     * 3. 准入被拒的文案按後端 reason 分類碼對應（機器碼分流，不對人話字串比對），
     *    每句都講清楚使用者現在能做什麼，不洩內部治理細節。
     */
    custom: {
      title: "跟單對象",
      subtitle:
        "貼上任意錢包位址即可跟單。貼上的位址未經平台額外審核，"
        + "是否值得跟隨由你自行判斷。",
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
      alreadyListedBadge: "此位址已在精選清單",
      // ⭐ vault 位址（2026-07-31 契約）：**資訊性標示，不是警告**——中性說明引擎
      // 會做什麼，不勸退、不新增步驟。只有 advisory 檢查有 FAIL 才另外顯示警語。
      vaultBadge: "Vault",
      vaultNote:
        "此位址是 Hyperliquid vault。跟單時引擎會自動套用 20x 槓桿上限，"
        + "並對 vault 的申購／贖回資金流做中性化處理（不把申贖誤判為交易盈虧）。",
      // advisory 檢查 FAIL 的收尾句：講清楚風險，但這是資訊、不是閘門——不擋送出。
      vaultCheckWarning:
        "此 vault 的帳本形態引擎可能無法精確計算，繼續前請理解風險。",
      /**
       * ⭐⭐ already_listed 的兩個子情形（2026-07-27，2026-07-30 拿掉精選卡片後仍
       * 適用）。後端的 `already_listed` 對 **paused**（accepting_new=false）的精選
       * leader 也回 true，但本頁已不再渲染任何精選清單格線——「在不在目前這份目錄
       * 清單裡」一律以**背景抓到的那份清單**為準（同源比較，工程原則 1），不由旗標
       * 單獨推論；抓不到清單時一律當作無法核對，不宣稱已確認過。
       */
      alreadyListedNote:
        "這個位址已在平台的精選 leader 名單中，後續流程與其他位址相同。"
        + "由於你是透過位址輸入框操作，以下聲明仍需勾選。",
      alreadyListedNoteNotShown:
        "這個位址已在平台的 leader 名單中，但目前無法核對其是否仍開放跟單"
        + "（例如已暫停接受新跟單者，或名單暫時讀取失敗）。後續流程與其他位址相同；"
        + "由於你是透過位址輸入框操作，以下聲明仍需勾選。",
      // ⭐ 專屬風險聲明：未勾選不得送出（純前端閘門，仿 wizard AML attestation）。
      attestation: "我了解此為未審核 leader，風險自負。",
      // ⭐ Task 11：本頁不再現場簽章——按下後導向 /onboarding 帶 advanced:0x… 參數，
      // 真正的選定簽章在 onboarding step 4 完成（見本檔 advanced 檔頭）。
      select: "前往開通",
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
      entryName: "未審核 leader",
    },
  },
  /**
   * 首頁（Task 8，設計稿 §03「首頁（策略優先 + 證據層）」）。⭐ 第一屏零錢包按鈕
   * （NOTE 01）：hero CTA 與 header CTA 一律指向 /strategies，本頁不觸發任何
   * wagmi 連線。`hero.title`／`sub`／`ctaPrimary` 三個值照文案對照表 L999-1006
   * 的 new 欄逐字採用；`strategies.*`／`evidence.*`／`steps.*` 照 §03 內文。
   */
  home: {
    hero: {
      badge: "非保管 · 資金不離開你的錢包",
      title: "讓你的 Hyperliquid 帳戶，自動跟隨量化策略",
      sub: "資金始終留在你的錢包。Filet 只能依你設定的風險限制下單，不能提領或轉帳，你可以隨時暫停或撤銷授權。",
      ctaPrimary: "查看策略與風險",
      ctaSecondary: "授權能做什麼？",
      microNote: "不需要註冊、不需要 email。連接錢包只發生在你選好策略之後。",
      featuredCard: {
        statusRunning: "運行中",
        statusPaused: "已暫停",
        leaderPrefix: "leader ",
        leaderLinkSuffix: " · 鏈上可驗",
        returnLabelPrefix: "樣本 ",
        returnLabelSuffix: " 天全期報酬",
        // ⭐ M3 round3 Task 7（D5）：回撤 label 不再自己定義一份，改在
        // page.tsx 直接引用 `strategyDetail.metrics.maxDrawdownLabel`
        // （「策略期間回撤」），與策略詳情頁／traders 頁同一個 key。
        liveDaysLabel: "實盤天數",
        followerCountLabel: "目前跟單人數",
        sampleNotePrefix: "樣本 ",
        sampleNoteSuffix: " 交易日，樣本數偏小、指標帶寬較寬。",
        methodologyLink: "完整方法論揭露 →",
        noDataNote: "目前尚無足夠資料可顯示主推策略卡，請直接查看策略清單。",
      },
    },
    /** 證據列四格（NOTE 02）：全部接 `/api/public/stats`，取不到 → 顯示 `—` 並
     * 保留欄位；`custody` 為託管資產永遠是 0，屬靜態陳述，不接任何 API 欄位。 */
    evidence: {
      routedVolumeLabel: "累計路由交易量",
      routedVolumeLink: "hyperliquid explorer ↗",
      liveDaysLabel: "自營策略連續實盤",
      liveDaysSuffix: " 天",
      liveDaysLink: "leader 帳戶 ↗",
      builderFeeLabel: "Builder fee（每筆成交）",
      builderFeeLink: "builder code ↗",
      custodyLabel: "託管資產（永遠）",
      custodyValue: "0",
      custodyLink: "授權範圍說明 ↗",
    },
    strategies: {
      heading: "可跟單策略",
      sub: "每個策略的績效都來自 Hyperliquid 鏈上帳戶，任何人都能自行核對。",
      viewAll: "全部策略 →",
      featuredBadge: "主推",
      pendingBadge: "暫不開放新跟單",
      metricTotalReturn: "總報酬",
      metricMaxDrawdown: "最大回撤",
      metricSharpe: "Sharpe",
      metricWinRate: "日勝率",
      insufficientLabel: "樣本不足",
      leveragePrefix: "槓桿 ≤ ",
      minNotionalPrefix: "最低跟單 $",
      cta: "查看策略與風險",
      pendingNote: "此策略目前暫不開放新跟單，既有跟單者不受影響。",
      empty: "目前沒有可跟單的策略，請稍後再回來查看。",
      advancedTitle: "進階模式",
      advancedBody:
        "已經有指定的 leader 地址？可跟單任一 Hyperliquid 錢包。此模式下 Filet 不對該地址的策略品質、"
        + "風控或存續做任何背書，需另外勾選風險確認。",
      advancedCta: "進入進階模式",
    },
    steps: {
      heading: "開始跟單的四個步驟",
      items: [
        { n: "01", t: "選擇策略", d: "看完績效、回撤與方法論揭露後再決定。" },
        { n: "02", t: "連接錢包並授權", d: "兩筆免費簽名：agent 下單權與 builder fee 費率上限，均不上鏈。" },
        { n: "03", t: "設定風險限制", d: "投入比例、槓桿上限、最大回撤自動停止。" },
        { n: "04", t: "進入 Dashboard", d: "同步狀態、曝險、PnL、費用一頁看完，隨時可暫停。" },
      ],
    },
  },
  /**
   * 授權能力矩陣（單一來源，首頁／策略頁授權說明／onboarding 授權卡三處共用，
   * 見 plan Task 8）。`can`／`cannot` 各四條，`revocable` 為單方可撤銷段。
   */
  auth: {
    heading: "授權之後，Filet 能做什麼、不能做什麼",
    sub: "兩筆錢包簽名都不上鏈、不花 gas。以下是 agent 權限的精確邊界。",
    canTitle: "可以",
    cannotTitle: "不能",
    // ⭐ Task 10b（主線程裁決 2026-08-28）：三處共用同一份 key（首頁／策略詳情頁／
    // onboarding 授權卡皆掛 CapabilityMatrix），改一次全站同步。
    can: [
      "依策略訊號在你的帳戶下單、加倉、平倉",
      "使用你設定範圍內的可動用資金（投入比例上限）",
      "在策略標示的槓桿上限內執行（平台層強制）",
      "依已簽署的費率上限（0.02%）收取 builder fee",
    ],
    cannot: [
      "提領你的資金到任何地址",
      "轉帳或在錢包之間移動資產",
      "取得你的私鑰或助記詞（永遠不會索取）",
      "超出你簽署的投入比例；超出策略標示的槓桿上限；觸發你啟用的最大回撤而不停止",
    ],
    revocable:
      "授權為你單方可撤銷：Dashboard 一鍵暫停跟單（停止新開倉），或撤銷 agent（Filet 立即失去下單能力）。"
      + "撤銷不需要 Filet 同意，也不需要我們在線。",
  },
  /** 費用試算區（NOTE 06）。slider 邏輯在 `FeeCalculator.tsx`，本區只放靜態文案。 */
  fee: {
    heading: "費用：每筆成交 0.02%",
    body: "沒有月費、沒有分潤、沒有提領費。builder fee 直接記在鏈上，每一筆都能核對。",
    note: "此費率為你在授權時簽署的上限，我們無法單方調高。Hyperliquid 本身的交易手續費與資金費率另計。",
    calcLabel: "試算：倉位規模",
    openLabel: "建倉 builder fee",
    closeLabel: "平倉 builder fee",
    totalLabel: "一次完整交易合計",
  },
  /**
   * 策略詳情頁（決策頁，Task 9，設計稿 §04）。跟單面板的三個 slider 未連錢包即可
   * 調整，只有最後一顆 CTA 需要錢包（NOTE 07）。`auth.*`（授權能力矩陣）在本頁
   * 不重複定義——若後續要在本頁附一份授權說明，直接復用 `CapabilityMatrix`。
   */
  strategyDetail: {
    breadcrumb: "策略",
    runningPill: "運行中",
    pausedPill: "已暫停",
    leaderPrefix: "leader ",
    leaderLinkSuffix: " ↗",
    asOfPrefix: "資料截至 ",
    sourceSuffix: " · 來源：Hyperliquid API",
    notFoundTitle: "找不到這個策略",
    notFoundBody: "這個策略不存在，或已下架。請回到策略列表重新選擇。",
    backToList: "回策略列表 →",
    loadingNote: "讀取策略資料中…",
    equity: {
      heading: "帳戶淨值曲線（USD）",
      periodAll: "全部",
      period30d: "30D",
      period7d: "7D",
      overlayLabel: "疊加對照：",
      overlayNote: "目前無對照資料來源，暫不可用",
      overlays: ["BTC", "ETH", "S&P 500", "黃金"],
      empty: "尚無足夠資料繪製淨值曲線。",
    },
    metrics: {
      totalReturnLabel: "總報酬",
      totalReturnNote: "真實入金起算",
      // ⭐ M3 round3 Task 7（D5 裁決）：策略頁「策略期間回撤」與 Dashboard
      // 「你的跟單回撤」（dashboard.pnl.maxDrawdown）是兩個不同標的，不可同名
      // ——本 key 同時被 home.tsx 與 traders/[address]/page.tsx 引用，三處一致。
      maxDrawdownLabel: "策略期間回撤",
      maxDrawdownNote: "期間內單次最深",
      sharpeLabel: "Sharpe（年化）",
      sharpeNoteSuffix: " (1 s.e.)",
      winRateLabel: "日勝率",
      winRateNotePrefix: "N=",
      winRateNoteSuffix: " 日報酬樣本",
      annualizedVolLabel: "年化波動",
      annualizedVolNote: "365 日慣例",
      sortinoLabel: "Sortino",
      sortinoNote: "下行風險調整",
      bestWorstLabel: "最佳 / 最差日",
      bestWorstNote: "單日 %",
      startEndEquityLabel: "起訖淨值",
      startEndEquityNote: "真實入金 → 目前淨值",
      insufficientLabel: "樣本不足",
      /** ⭐ Task 7（主線程驗收修正）：大字只留總報酬／策略期間回撤／日勝率三張
       * （plan Task 7 第 1 條＋設計稿 R2 P0 原文）。Sharpe／Sortino／年化波動／
       * 起訖淨值／最佳最差日在 `sample_days` 未達 `sample_threshold` 時摺成
       * 一行小字，取代個別「樣本不足」卡片（8 格中 5 格是樣本不足、佔兩屏
       * 高度的 R2-P0 問題）。五段拼接：
       * `{insufficientGroupLabel}{insufficientGroupPrefix}{sample_days}
       * {insufficientGroupMid}{sample_threshold}{insufficientGroupSuffix}`。 */
      insufficientGroupLabel: "Sharpe／Sortino／年化波動／起訖淨值／最佳最差日",
      insufficientGroupPrefix: "：樣本不足（",
      insufficientGroupMid: "/",
      insufficientGroupSuffix: " 天），達門檻後顯示",
    },
    cagr: {
      heading: "CAGR（年化）",
      toggleShow: "展開",
      toggleHide: "收合",
      notePrefix: "樣本僅 ",
      noteSuffix: " 天，年化外推無統計意義，因此刻意灰階、不出現在首頁與策略卡。",
    },
    methodology: {
      heading: "方法論與樣本揭露",
      unavailable: "方法論資料暫不可用。",
      depositPrefix: "以真實入金本金 $",
      depositSuffix: " 起算（鏈上 deposit 可驗證），涵蓋 ",
      /** 首快照為 0 時省略入金句，改由此開頭（誠實顯示，2026-08-29）。 */
      rangePrefix: "涵蓋 ",
      daysSuffix: " 個交易日（",
      rangeSuffix: "）。",
      sharpePrefix: "Sharpe ",
      sharpeSeInfix: "，標準誤 ±",
      sharpeSeSuffix: "（N=",
      sampleSuffix: " 個日報酬樣本）。",
      conventionPrefix: "指標以 ",
      conventionMid: " 日/年、無風險利率 ",
      conventionSuffix: " 之加密慣例年化。",
      /**
       * ⭐ Task 12（/docs 頁「績效方法論」段）新增：spec 要求揭露「perp 基準」
       * 這條慣例，但既有欄位裡沒有一句現成的話講到它（`methodology.basis` 這個
       * API 欄位本身也從未在任何頁面被渲染過，見 publicApi.ts）。這是本任務唯一
       * 新增的一句話；其餘 /docs 段落全部複用既有 key，不重複定義語義。
       */
      basisNote: "指標以 perp（永續合約）帳戶淨值為計算基準。",
    },
    panel: {
      heading: "跟隨此策略",
      scaleLabel: "投入比例（帳戶淨值）",
      leverageLabel: "槓桿上限",
      ddLabel: "最大回撤自動停止",
      ddEnableLabel: "啟用最大回撤自動停止",
      ddDisabledNote: "預設關閉。啟用後，實際門檻將在下一步透過既有簽章流程確認。",
      estDepositLabel: "預估投入",
      estDepositValue: "連接錢包後計算",
      builderFeeLabel: "Builder fee",
      builderFeeValue: "0.02% / 成交",
      estMonthlyLabel: "預估月費用",
      estMonthlyValue: "依成交量",
      cta: "連接錢包並繼續",
      ctaConnecting: "連接中…",
      ctaSigning: "請在錢包中簽署登入訊息…",
      footnote: "下一步僅為免費簽名（不上鏈、不花 gas），你會在授權前看到完整權限說明與費用確認。",
      pendingCta: "暫不開放新跟單",
      pendingNote: "此策略目前暫不開放新跟單，既有跟單者不受影響。",
    },
  },
  /**
   * `/dashboard`（Task 14，設計稿 §06＋NOTE 13-18）：六塊＋持倉。⭐ 全部數字來自
   * `/api/me/dashboard`（Task 13），每塊獨立 nullable、塊內欄位個別 null →「—」
   * （不變量 6，`format.NO_VALUE`）。kill switch 兩顆按鈕本 task 只做 UI／handler
   * 接口，實際生效在 Task 15（`DASHBOARD_KILL_SWITCH_ENABLED` 常數關閉時不渲染）。
   */
  dashboard: {
    heading: "Dashboard",
    lastSyncPrefix: "最後同步 ",
    lastSyncSuffix: " 前",
    lastSyncJustNow: "剛剛",
    liveBadge: "即時",
    loadingNote: "讀取 Dashboard 資料中…",
    status: {
      label: "策略狀態",
      strategyFallback: "此帳號",
      stateFollowing: "跟單中",
      statePaused: "已暫停",
      stateHalted: "已停止",
      stateInactive: "尚未開通",
      followingDaysPrefix: "已跟單 ",
      followingDaysSuffix: " 天",
      signalOk: "訊號來源正常",
      signalUnknown: "訊號來源狀態未知",
      pauseBtn: "暫停跟單",
      resumeBtn: "恢復跟單",
      closeAllBtn: "平倉並撤銷授權",
      pauseErrorNote: "操作失敗，請稍後重試。",
      closeAllModal: {
        title: "確認：平倉並撤銷授權",
        warning: "此操作將以市價平倉你目前所有跟單部位並停止跟單，且不可逆。",
        positionsHeading: "將平倉部位",
        noPositions: "目前無持倉。",
        ackLabel: "我理解此操作不可逆，且完成後不會自動恢復跟單。",
        confirmBtn: "確認平倉並撤銷",
        cancelBtn: "取消",
        signingNote: "請在錢包中簽署…",
      },
      closeAllProgress: {
        title: "收尾進行中",
        note: "引擎已收到請求，正在撤單並平倉，請稍候（約一分鐘內完成，此區塊會自動更新）。",
      },
      closeAllDone: {
        title: "已完成平倉並撤銷",
        note: "跟單已停止，且不會自動恢復。此動作未撤銷 API wallet 的鏈上權限，"
             + "請至 Hyperliquid 官方介面自行移除。",
        linkLabel: "前往 Hyperliquid 官方介面移除 API wallet",
        steps: [
          "登入 app.hyperliquid.xyz",
          "進入「API」設定頁面",
          "找到本站建立的 API wallet 並移除其權限",
        ],
      },
      closeAllFailed: {
        title: "處理逾時",
        note: "引擎未在時限內處理平倉請求，可能暫時離線。你的授權與部位狀態未變，"
             + "請稍後重試，或直接至 Hyperliquid 官方介面移除 API wallet 並自行平倉。",
        linkLabel: "前往 Hyperliquid 官方介面移除 API wallet",
      },
      guardsHeading: "風險護欄（設定值 vs 目前）",
      guardScale: "投入比例",
      guardLeverage: "槓桿",
      guardDrawdown: "回撤（自高點）",
      drawdownDisabled: "未啟用 · 前往設定 →",
    },
    equity: {
      label: "帳戶淨值與可用保證金",
      custodyNote: "錢包資產由你自己保管；Filet 無提領權限",
      retSuffix: " 30D",
      usedMargin: "已用保證金",
      availableMargin: "可用保證金",
      lowMarginWarning:
        "可用保證金偏低。若策略需加倉可能被跳過，建議入金或調低投入比例。",
      // ⭐ Task 6（R2 保證金分級）：<2% 的紅框文案——比 lowMarginWarning 更急迫，
      // 明講「極可能被跳過」而非「可能」，並把「儘速」放在句首引導動作。
      criticalMarginWarning:
        "可用保證金嚴重不足，策略加倉極可能被跳過。請儘速入金或調低投入比例。",
    },
    exposure: {
      label: "目前曝險",
      notionalSuffix: " 名目",
      long: "多方",
      short: "空方",
      biasLabel: "方向偏誤",
      biasLong: "偏多",
      biasShort: "偏空",
      biasNeutral: "中性",
      positionCount: "持倉檔數",
      maxPosition: "最大單一部位",
    },
    pnl: {
      label: "淨 PnL（已扣 builder fee）",
      realizedPrefix: "已實現 ",
      unrealizedPrefix: " · 未實現 ",
      chartEmpty: "尚無足夠資料繪製走勢圖。",
      winRate: "勝率",
      closedPositions: "已結束倉位",
      // ⭐ D5（2026-08-30 主線程裁決）：策略頁「策略期間回撤」與 Dashboard 這裡的
      // 「你的跟單回撤」是兩個不同標的（策略自身 vs 你實際跟單的帳戶），不可同名——
      // 同名曾造成首頁/詳情頁/Dashboard 三處數字互相矛盾卻被誤讀成同一件事。
      maxDrawdown: "你的跟單回撤",
      feeShare: "費用佔 PnL",
    },
    sync: {
      label: "master / follower 同步誤差",
      latencyMedian: "訊號延遲 中位",
      latencyP95Prefix: "p95 ",
      priceDiff: "成交價差",
      priceDiffNote: "加權平均",
      unsyncedPositions: "未同步倉位",
      scaleDeviation: "部位比例偏差（vs master）",
      missedSignals: "近 24h 遺漏訊號",
      lastRecon: "上次完整對帳",
      // ⭐ R2·C 空值三態（2026-08-30）：`data_state` 三態的收斂文案。「ok」沿用
      // 上面既有的逐欄渲染（個別欄位仍可能為 null →「—」，但絕不顯示 0ms）；
      // 「warming」與「error」時整卡摺為一行，不留一整塊空白卡片。
      warmingLine: "同步誤差：跟單啟動後 24h 內開始累積",
      errorLine: "引擎狀態讀取失敗",
    },
    fees: {
      label: "本月交易量與 builder fee",
      routedVolume: "路由交易量",
      builderFees: "Builder fee 累計",
      fillCount: "成交筆數",
      avgFee: "平均每筆費用",
      effectiveRate: "實際費率",
    },
    tabs: {
      positions: "跟單持倉",
      fees: "費用明細",
      history: "成交記錄・授權歷程",
    },
    positionsTable: {
      symbol: "標的",
      value: "部位價值",
      upnl: "未實現",
      entry: "進場均價",
      mark: "標記價",
      deviation: "vs master",
      long: "多",
      short: "空",
      marginModeCross: "全倉",
      marginModeIsolated: "逐倉",
      empty: "目前沒有跟單持倉。",
    },
    /**
     * 費用明細 tab（R2·B 重構，2026-08-30）：頂部合計四格＋期間切換（本月/上月/全部）＋
     * 匯出 CSV＋前端補日曆列（無成交日渲染整列「—」，與 $0.00 有成交但費用為零區分）。
     * `summaryPnlShare` 語意＝佔**已實現**淨 PnL（Task 2b 裁決 D12），不是總 PnL——
     * 文案必須如實寫「已實現」，不可省略（`pnl_share_pct` 為 null 時前端顯示「—」）。
     */
    feesTable: {
      periodThisMonth: "本月",
      periodLastMonth: "上月",
      periodAll: "全部",
      summaryBuilderFee: "Builder Fee 合計",
      summaryRoutedVolume: "路由交易量",
      summaryFillCount: "成交筆數",
      summaryPnlShare: "佔已實現淨 PnL",
      exportCsv: "匯出 CSV",
      colDate: "日期 ↓",
      colFillCount: "成交筆數",
      colRoutedVolume: "路由交易量",
      colBuilderFee: "Builder fee",
      colEffectiveRate: "實際費率",
      loadMore: "載入更早的 20 天",
      footerNote:
        "日期為 UTC 日界；「—」表示當日無成交，與費用為 0 的情況區分。"
        + "實際費率＝當日 fee ÷ 路由交易量，用來核對 0.02% 上限沒有被超收。",
      loading: "讀取中…",
      loadError: "費用明細暫時讀不到，請稍後重試。",
      empty: "此期間尚無成交紀錄。",
    },
    /**
     * 「成交記錄・授權歷程」tab（M3 round2 Task 7）——資料**直取 Hyperliquid**
     * （userFillsByTime／explorer userDetails），結構上不讀自家 DB。lazy fetch，
     * load/error/empty 三態各自獨立（成交與授權是兩個獨立上游查詢）。
     */
    history: {
      fillsTitle: "成交記錄",
      authorizationsTitle: "授權歷程",
      loading: "讀取中…",
      loadError: "資料暫時讀不到（直接查詢 Hyperliquid 失敗），請稍後重試。",
      fillsEmpty: "近期沒有成交紀錄。",
      authorizationsEmpty: "沒有查到授權紀錄。",
      time: "時間",
      coin: "幣別",
      side: "方向",
      buy: "買",
      sell: "賣",
      px: "價格",
      sz: "數量",
      fee: "手續費",
      closedPnl: "已實現盈虧",
      action: "動作",
      summary: "說明",
      // ⭐ [W2] 2026-08-29 opus 審查修正：後端 `/api/me/authorizations` 改回
      // 結構化欄位（agent_address／builder／max_fee_rate），中文組字移到這裡
      // （`PositionsTable.tsx` 依 `action_type` 挑對應標籤＋結構化欄位組句）。
      actionApproveAgent: "授權 API wallet",
      actionApproveBuilderFeeLabel: "授權 builder fee",
      actionApproveBuilderFeeTo: "給",
      actionUnknown: "授權動作",
      tx: "交易",
      viewTx: "查看",
      // ⭐ M3 round3 Task 8（R2·P1）：分頁 50/頁＋期間／幣種篩選＋UTC/本地切換。
      periods: { "7d": "7 天", "30d": "30 天", all: "全部" },
      coinFilterLabel: "篩選幣種",
      coinFilterAll: "全部幣種",
      tzLocal: "本地時間",
      tzUtc: "UTC",
      pagination: {
        showing: "顯示 ",
        rangeSep: "–",
        ofTotal: " / ",
        prev: "上一頁",
        next: "下一頁",
      },
    },
  },
  /**
   * `/settings`（Task 16）：登入後的設定中樞。把 Task 11 遷移舊 `/leaders` 時
   * 暫時落掉的「目前跟隨的 leader」面板與風控設定區塊（`leaders.risk`／
   * `leaders.current`，均已隨舊頁重寫移除）以新視覺復活，另加資金配置與
   * 授權管理（agent 位址／builder fee／暫停跟單／平倉並撤銷）。四段各自獨立
   * 查詢、獨立失敗處理——任一段讀不到不擋其餘三段（沿舊版 CurrentLeaderPanel
   * 的既定原則：讀不到≠沒有，工程原則 3）。
   */
  settings: {
    eyebrow: "設定",
    title: "帳戶設定",
    subtitle: "調整風控門檻、資金配置與授權；查看目前跟隨的策略。",
    loadingNote: "讀取設定中…",
    /**
     * ⭐ M3 round3 Task 8（R2·P0）：簽署失敗原本是永久停留在頁上的紅框——改成
     * 右下角 toast（可手動關閉、8 秒自動消失），區塊內只留一顆重新簽署的按鈕。
     * 風控／資金配置／熔斷解除三處簽署動作共用同一份文案。
     */
    toast: {
      dismiss: "關閉",
      retrySignButton: "重新簽署",
    },
    /** ⭐ 風控 opt-in（裁決 1）：預設不啟用，數字全來自後端 specs，不寫死任何門檻。 */
    risk: {
      title: "風控設定",
      subtitle:
        "預設不啟用任何風控——系統只按 leader 的動作跟單，不會替你停損或熔斷。"
        + "每個門檻都由你自己決定：我們把建議值標在旁邊，最終要設多少是你的選擇。",
      applyNote:
        "每一次調整都會請你在錢包簽署一則訊息，送出後引擎會在下一輪（約一分鐘內）套用。",
      trackingTitle: "跟單精度",
      trackingSubtitle: "這一項與風控開關無關，無論你有沒有啟用風控都會生效。",
      enableLabel: "啟用 Filet 風控系統",
      enableHelp:
        "開啟後：權益回撤達到你設定的門檻時，系統會停止跟單（並依你的選擇平倉）。"
        + "這限制的是「繼續跟下去」的曝險——觸發時該筆虧損通常已經發生，"
        + "並不會讓本金免於虧損。關閉時系統完全不介入，僅純粹跟單。",
      detailsTitle: "風控細項",
      percentSuffix: "%",
      hoursSuffix: "小時",
      recommendedLabel: "建議",
      boolOn: "開啟",
      boolOff: "關閉",
      saveButton: "簽署並儲存風控設定",
      saving: "等待錢包簽署…",
      saved: "風控設定已送出。",
      loadError: "風控設定暫時讀不到，請稍後重新整理本頁。",
      signNote:
        "接下來會請你在錢包簽署一則訊息（不上鏈、不花費 gas）。"
        + "Filet 永遠不會請你輸入私鑰或助記詞；簽署只會在你自己的錢包中完成。",
      errors: {
        walletRejected: "你在錢包取消了簽署，風控設定沒有被變更。",
        signerMismatch:
          "簽署的錢包與你登入的錢包不是同一個，設定沒有被送出。"
          + "請切換回登入的錢包後再試一次。",
        contentMismatch:
          "伺服器回傳的待簽內容與你在畫面上設定的不一致，已為你中止，"
          + "沒有送出任何簽署。請不要重試，並回報這個狀況給我們。",
        messageFailed: "無法取得待簽內容，風控設定沒有被變更。請稍後再試一次。",
        submitFailed:
          "送出失敗，風控設定沒有被變更。上一筆簽署已作廢，請重新操作一次。",
      },
      applied: {
        pending: "已提交，尚未生效（引擎約一分鐘內套用）。",
        inSync: "目前生效中的設定與你提交的一致。",
        unknown: "目前生效的設定暫時無法確認（引擎狀態讀不到）。",
        notSubmitted: "你尚未提交過風控設定；畫面上顯示的是系統預設值。",
        sourceLabel: "來源",
        changedAtLabel: "生效時間",
        // ⭐ M3 round3 Task 8（R2·P1）：建議值／目前生效值／待簽署值三種數字混在
        // 同一區，用戶分不出哪個在作用——改成每個參數固定顯示「目前生效 / 你的
        // 設定」兩值；尚未套用時加黃點與這句提示。
        effectiveLabel: "目前生效",
        yourSettingLabel: "你的設定",
        unknownShort: "無法確認",
        pendingBadge: "待下輪套用（約 1 分鐘）",
      },
      halted: {
        title: "你的跟單已被風控停止",
        body:
          "風控門檻被觸發，引擎已停止繼續跟單。觸發時那筆虧損通常已經發生——"
          + "停止的是「繼續跟下去」的曝險。",
        reasonLabel: "觸發原因",
        trippedAtLabel: "觸發時間",
        cooldownLabel: "冷靜期",
        resumeAtLabel: "預計自動恢復",
        noAutoResume:
          "沒有預計的自動恢復時間（冷靜期設為 0，或目前算不出來）——"
          + "要恢復跟單請按下方按鈕。",
        unknownValue: "（讀不到）",
        resumeButton: "立即恢復跟單",
        resuming: "等待錢包簽署…",
        resumed: "已送出恢復請求，引擎會在下一輪恢復跟單。",
        residualNote:
          "熔斷當下有部位未能平倉或掛單未撤，那些部位仍留在市場上。"
          + "恢復跟單後，引擎會在下一輪把它們往 leader 的目標收斂。",
        resumeNote:
          "恢復後引擎會在下一輪重新依 leader 建立部位。權益基準已在熔斷當下重置，"
          + "所以不會因為熔斷前的那段跌幅立刻再停一次。",
        leaderRevokedNote:
          "這次停止是因為你跟隨的 leader 已被我們下架，不是你的風控門檻被觸發。"
          + "這一種無法自助恢復——請改為選擇另一位 leader。",
        unknown: "目前無法確認你的風控是否被觸發（引擎狀態讀不到）。",
      },
    },
    /** ⭐ Task 10b：投入比例直接乘進部位大小，與換 leader 同級的簽章防線。 */
    capital: {
      title: "資金配置",
      subtitle: "投入比例直接乘進你的跟單部位大小，調整前請先確認你理解影響。",
      scaleLabel: "投入比例（佔帳戶淨值）",
      effectiveLabel: "目前生效",
      // ⭐ M3 round3 Task 8（R2·P1）：與風控設定同一套「目前生效 / 你的設定」
      // 兩值＋待套用黃點慣例。
      yourSettingLabel: "你的設定",
      pendingLabel: "已提交，待下輪套用（約 1 分鐘）",
      saveButton: "簽署並儲存資金配置",
      saving: "等待錢包簽署…",
      saved: "資金配置已送出。",
      loadError: "資金配置暫時讀不到，請稍後重新整理本頁。",
      signNote:
        "接下來會請你在錢包簽署一則訊息（不上鏈、不花費 gas）。"
        + "Filet 永遠不會請你輸入私鑰或助記詞；簽署只會在你自己的錢包中完成。",
      errors: {
        walletRejected: "你在錢包取消了簽署，資金配置沒有被變更。",
        signerMismatch:
          "簽署的錢包與你登入的錢包不是同一個，設定沒有被送出。"
          + "請切換回登入的錢包後再試一次。",
        contentMismatch:
          "伺服器回傳的待簽內容與你在畫面上設定的不一致，已為你中止，"
          + "沒有送出任何簽署。請不要重試，並回報這個狀況給我們。",
        messageFailed: "無法取得待簽內容，資金配置沒有被變更。請稍後再試一次。",
        submitFailed:
          "送出失敗，資金配置沒有被變更。上一筆簽署已作廢，請重新操作一次。",
      },
    },
    /** agent 位址／builder fee／暫停跟單／平倉並撤銷（複用 Task 15 kill switch）。 */
    auth: {
      title: "授權管理",
      subtitle: "你目前授權給 Filet 的 agent 資訊，以及暫停跟單／平倉並撤銷入口。",
      agentAddressLabel: "Agent 位址",
      agentAddressMissing: "尚未產生 Agent",
      builderFeeLabel: "Builder fee 上限已核准",
      agentApprovedLabel: "Agent 授權狀態",
      approvedYes: "已核准",
      approvedNo: "尚未核准",
      loadError: "授權資訊暫時讀不到，請稍後重新整理本頁。",
      pauseHeading: "跟單開關",
      pauseBtn: "暫停跟單",
      resumeBtn: "恢復跟單",
      closeAllBtn: "平倉並撤銷授權",
      pauseErrorNote: "操作失敗，請稍後重試。",
      noEngineNote: "此帳號目前沒有在運作的引擎，沒有可操作的動作。",
      closeAllPendingNote: "已送出，引擎正在收尾，請至 Dashboard 查看進度。",
    },
    /** ⭐ 「目前跟隨的 leader」——沿舊版 `leaders.current` 的既定誠實揭露原則。 */
    leader: {
      title: "目前跟隨的策略",
      loading: "讀取你目前的跟隨狀態…",
      leaderLabel: "目前跟隨",
      failedTitle: "目前無法讀取你目前的跟隨狀態",
      failedNote:
        "這次查詢失敗只影響本區塊的顯示：它不會改變你的跟單設定，也不代表你沒有在跟單。"
        + "本頁其餘功能不受影響；請重新整理本頁再試一次。",
      noneTitles: {
        engine_default: "你已啟用跟單，但尚未指定 leader",
        not_activated: "你的帳號尚未啟用跟單",
        indeterminate: "目前無法確認你的跟隨狀態",
      },
      noneTitleFallback: "目前沒有可顯示的跟隨對象",
      statusLabel: "狀態碼",
      pendingTitle: "有一筆已簽署、尚未生效的換 leader",
      pendingLabel: "將換到",
      pendingIssuedAtLabel: "簽署時間",
      changeStrategyBtn: "更換策略",
      advancedModeBtn: "進階模式",
    },
  },
  /**
   * `/status` 頁（Task 12）：讀 `/api/public/status` 渲染整體狀態＋components＋
   * updated_at。法務內容 spec（legal-copy-zh.md）只涵蓋 /docs，不涵蓋 /status，
   * 這幾個 key 因此是本頁專屬的新文案；三態字樣復用 `footer.status*`（同一套狀態
   * 語彙同時用在頁尾狀態燈與本頁，避免同一個系統狀態有兩種說法）。
   */
  status: {
    heading: "系統狀態",
    sub: "各項服務元件目前的運作狀態，資料來自 /api/public/status（不需登入）。",
    componentsHeading: "元件",
    empty: "目前沒有可顯示的元件狀態。",
    loadFailedNote: "狀態讀取失敗或逾時，以下顯示為保守值（未知），不代表系統健康。",
  },
  /**
   * `/leaderboard` 頁（M3 round2 Task 5）：Hyperliquid 主網公開排行榜的展示頁，
   * 資料來自 `/api/public/leaderboard`（無需登入）。與本站策略／客戶績效無關，
   * 純供研究參考，故文案刻意強調資料來源與「非本站背書」。
   */
  leaderboard: {
    heading: "交易排行榜",
    sub: "資料來自 Hyperliquid 官方公開排行榜，依所選視窗的損益排序，僅供研究參考，不代表本站背書或跟單建議。",
    windows: { day: "日", week: "週", month: "月", allTime: "全期" },
    table: {
      rank: "排名",
      trader: "交易員",
      accountValue: "帳戶價值",
      pnl: "損益",
      roi: "報酬率",
      volume: "成交量",
    },
    loading: "讀取排行榜中…",
    error: "排行榜讀取失敗，請稍後重試。",
    empty: "目前沒有可顯示的排行榜資料。",
  },
  /**
   * `/explore`（M3 round3 Task 4，設計審查 R2·A）：把「鯨魚 PnL 榜」重構成
   * 「可跟單對象探索」，資料來自 `/api/public/explore`（無需登入，`hl_explore.py`）。
   * 排序／資格過濾全在後端（R2-01），本頁只送布林 chip 開關；building 態與 fetch
   * 失敗態分開處理（見 `explore/page.tsx` 檔頭）。
   */
  explore: {
    heading: "探索跟單對象",
    disclaimerBadge: "Filet 不對清單上任何地址背書",
    sub: "資料全部來自 Hyperliquid 公開鏈上紀錄，每 10 分鐘更新。排序預設為風險調整後報酬（報酬率 ÷ 最大回撤），而非絕對獲利金額——大額帳戶的 PnL 無法被小額帳戶複製。",
    updatedAtPrefix: "資料更新於 ",
    windows: { d7: "7D", d30: "30D", d90: "90D", all: "全部" },
    windowComingSoon: "即將推出",
    filters: {
      sample: "僅顯示達樣本門檻（≥ 60 交易日 · ≥ 200 筆成交）",
      maxDd: "最大回撤 < 30%",
      concentrated: "排除單一幣種 > 90%",
    },
    countPrefix: "",
    countMid: " 個帳戶 → 符合 ",
    countSuffix: "",
    table: {
      rank: "#",
      account: "帳戶",
      sparkline: "30D 淨值",
      ret: "30D 報酬率",
      dd: "最大回撤",
      days: "實盤天數",
      winRate: "結倉勝率",
      exposure: "目前曝險",
      actions: "",
    },
    tags: { lowDrawdown: "低回撤", concentrated: "集中度高" },
    // D14（2026-08-30 主線程裁決）：後端 `exposure.dir` 改回傳 locale 中性代碼
    // `"long"`/`"short"`（見 `hl_explore.py`），顯示文案改由這裡對映。
    exposureDir: { long: "多", short: "空" },
    subSep: " · 帳戶 ",
    copyAddress: "複製地址",
    copied: "已複製",
    view: "查看",
    follow: "跟單 →",
    building: "探索榜建置中，約數分鐘後就緒",
    errorPrefix: "探索榜讀取失敗 · ",
    empty: "目前沒有符合條件的地址。",
    pagination: {
      showing: "顯示 ",
      rangeSep: "–",
      ofTotal: " / ",
      perPagePrefix: " · 每頁 ",
      perPageSuffix: " 列",
      prev: "上一頁",
      next: "下一頁",
    },
  },
  /**
   * `/traders/[address]`（M3 round2 Task 6）：leaderboard 任意地址的詳情頁。
   * 指標卡／CAGR／方法論文案沿用 `strategyDetail.metrics`／`.cagr`／`.methodology`
   * （通用績效用語，非策略專屬），本區塊只放這頁自己的殼與「非精選、不背書」的
   * 揭露（沿 `advanced` 頁「進階模式（無背書）」的既有揭露精神，見
   * `wizard.step1AdvancedLabel`／`step1AdvancedBody`）。
   */
  traders: {
    breadcrumb: "交易員",
    loadingNote: "讀取交易員資料中…",
    notFoundTitle: "找不到這個交易員",
    notFoundBody: "這個地址查無鏈上績效資料，或位址格式不正確。請回到排行榜重新選擇。",
    backToList: "回排行榜 →",
    asOfPrefix: "資料截至 ",
    sourceSuffix: " · 來源：Hyperliquid API",
    accountValueLabel: "目前帳戶價值",
    disclaimerNote: "此地址來自 Hyperliquid 公開排行榜，非本平台精選策略，本平台不對其表現背書或負責。",
    panel: {
      heading: "跟隨這個地址",
      cta: "連接錢包並繼續",
      ctaConnecting: "連接中…",
      ctaSigning: "請在錢包中簽署登入訊息…",
      footnote: "下一步僅為免費簽名（不上鏈、不花 gas），你會在授權前看到完整權限說明與費用確認。",
      // ⭐ [W4] 2026-08-29 opus 審查修正：已被平台安全撤銷（enabled=false）的
      // leader 不該再讓新客戶點進來就能跟——後端回傳 `follow_blocked` 時前端
      // 隱藏 CTA、改顯示這句提示。
      followBlocked: "此地址目前不可跟單。",
    },
  },
} as const;

/**
 * DeepString<T> — 把 typeof COPY_ZH 的每個字面字串型別展開回 `string`（其餘結構原樣保留，
 * 包含陣列與巢狀物件）。COPY_ZH 用 `as const` 讓每個文案值成為字面型別，若 COPY_EN 直接標
 * `typeof COPY_ZH` 會因為值不同而型別不符；用這個 mapped type 才能「結構對稱由型別強制」，
 * 同時允許兩邊文案內容不同。
 */
export type DeepString<T> = T extends string
  ? string
  : T extends readonly (infer U)[]
    ? readonly DeepString<U>[]
    : T extends object
      ? { [K in keyof T]: DeepString<T[K]> }
      : T;

/**
 * COPY_EN — 英文文案，key 結構與 COPY_ZH 完全對稱（由 DeepString<typeof COPY_ZH> 型別強制＋
 * copy.test.ts 的遞迴 key 比對雙重把關）。專有名詞（Hyperliquid、builder fee、agent、leader）
 * 保留英文；語氣對齊 zh 版的直接與精確，不用行銷腔。
 */
export const COPY_EN: DeepString<typeof COPY_ZH> = {
  common: {
    appName: "FILET",
    next: "Next",
    retry: "Retry",
    loading: "Loading…",
    notLoggedIn: "You're not signed in — please go back to the login page and connect your wallet.",
    backToLogin: "Back to login",
    logout: "Log out",
    nonCustodial: "Filet will never ask for your private key or seed phrase; all signing happens only in your own wallet.",
    notActivated: "This account hasn't finished activation yet — please go back to the onboarding page to complete authorization and deposit.",
    goOnboarding: "Go to onboarding",
  },
  nav: {
    ariaLabel: "Page navigation",
    strategies: "Strategies",
    explore: "Explore",
    how: "How it works",
    security: "Security",
    docs: "Docs",
    dashboard: "Dashboard",
    settings: "Settings",
    ops: "Ops",
    admin: "Pending",
    cta: "Log in",
    ctaConnecting: "Connecting…",
    ctaSigning: "Signing…",
    langToggleLabel: "Language",
    pillFollowing: "Copying",
    pillPaused: "Paused",
    pillNotFollowing: "Not copying",
    marginAlertPill: "Margin low",
  },
  footer: {
    brandTagline: "Non-custodial strategy execution on Hyperliquid. Your funds stay in your wallet; authorization can be revoked anytime.",
    statusOk: "All systems operational",
    statusDegraded: "Some systems degraded",
    statusUnknown: "Status unknown",
    productTitle: "Product",
    productStrategies: "Strategies",
    productHow: "How it works",
    productFees: "Fees",
    productDocs: "Docs",
    verifiableTitle: "Verifiable",
    verifiableLeaderAccounts: "Leader accounts (on-chain)",
    verifiableBuilderFee: "Builder code fee rate",
    verifiableMethodology: "Performance methodology",
    verifiableStatus: "System status",
    legalTitle: "Legal & contact",
    legalTerms: "Terms of service",
    legalPrivacy: "Privacy policy",
    legalRisk: "Risk disclosure",
    legalContact: "contact@filet.trade",
    disclaimer:
      "Copy trading carries risk of loss; past performance does not guarantee future results and you may lose your "
      + "entire investment. Filet does not provide investment advice and does not custody user assets. All signing "
      + "happens only in your own wallet; Filet will never ask for your private key or seed phrase.",
    copyright: "© 2026 Filet",
  },
  login: {
    eyebrow: "FILET",
    heroTitle: "Paste an address, start copy trading on Hyperliquid",
    subtitle: "Your funds stay in your own wallet. The strategy still executes.",
    journey: [
      {
        title: "Connect and sign in with your wallet",
        body: "Connect using your own browser wallet and sign one free login message — no on-chain transaction, no gas.",
      },
      {
        title: "Complete two authorizations",
        body: "Sign two messages in your wallet, also free and gas-less: copy-trade authorization (Filet can only place orders "
          + "on your behalf and can never move or withdraw funds) and builder code fee authorization (a fee is charged per fill "
          + "up to the rate cap you sign, verifiable on-chain).",
      },
      {
        title: "Paste a leader's address to start copy trading",
        body: "Go to the copy trade page, paste any Hyperliquid wallet address, and start copy trading once it passes the admission check.",
      },
    ],
    connect: "Connect wallet",
    connecting: "Connecting…",
    signingIn: "Please sign the login message in your wallet…",
    signInNote: "Signing in requires one free message signature (no on-chain transaction, no gas).",
    noWallet: "No browser wallet detected. Please install MetaMask and refresh this page. Filet will never ask for your private key or seed phrase.",
    rejected: "You canceled the signature in your wallet. Click \"Connect wallet\" again when you're ready.",
    loginFailed: "Login failed, please try again later.",
    walletPanelTitle: "Your wallet",
    enginePanelTitle: "Filet engine",
    addrLabel: "Address",
    strategyLabel: "Strategy",
    strategyValue: "Grid · Multi-asset",
    pillUnauthorized: "Not authorized",
    pillAuthorized: "Trade-only · no withdrawal rights",
    footnote: "A builder fee of 0.02% (2bp) is charged per fill, verifiable on-chain. Copy trading carries risk of loss; past performance does not guarantee future results.",
  },
  wizard: {
    stepNames: ["Select strategy", "Connect & authorize", "Risk limits", "Confirm"],
    backButton: "Back",
    step1Eyebrow: "Strategy selected",
    step1Back: "Change selection",
    step1AdvancedLabel: "Advanced mode (unendorsed)",
    step1AdvancedBody: "This address is not on Filet's curated list and has not undergone any due diligence or endorsement "
      + "— please assess the risk yourself before continuing.",
    step1NotFound: "This strategy could not be found — it may have been delisted, or the link is incorrect. Please go "
      + "back to the strategy list and pick again.",
    step2Title: "Connect & authorize",
    step2Body: "Neither wallet signature goes on-chain or costs gas; once funds arrive you can proceed to the next step.",
    agentCardName: "ApproveAgent",
    agentCardDesc: "Authorizes Filet's trade-only agent key to place orders on your behalf, with no withdrawal rights.",
    feeCardName: "ApproveBuilderFee",
    feeCardDesc: "Authorizes a builder fee cap of 0.1%; the actual rate charged per fill is 0.02%.",
    signWithWallet: "Sign with wallet",
    stateUnsigned: "Not signed",
    stateAwaitingWallet: "Confirm in your wallet…",
    stateSubmitting: "Submitting…",
    stateSubmitted: "Submitted, awaiting on-chain confirmation…",
    stateConfirmed: "Active",
    stateRejected: "Rejected",
    agentPreparing: "Preparing agent key…",
    agentLabel: "Agent address",
    reconnectTitle: "Wallet not connected",
    reconnectHint: "Your session is still valid, but your browser wallet is currently disconnected (it may be locked). "
      + "Please unlock your wallet and reconnect to continue signing; your progress won't be lost.",
    reconnectButton: "Reconnect wallet",
    step2DepositSubheading: "Deposit check",
    depositPerpLabel: "Perps account balance",
    depositThresholdLabel: "Activation threshold",
    depositShortfallLabel: "Remaining",
    depositDetected: "Sufficient funds detected — your perps account balance has reached the activation threshold.",
    depositPending:
      "No sufficient funds detected yet. Please deposit USDC into the \"perps\" (perpetuals) wallet of your own Hyperliquid "
      + "account (same address as your login wallet) — copy trading only uses the perps wallet; funds held in the spot wallet "
      + "don't count toward the activation threshold and won't be copy-traded. If your funds are already in the spot wallet, "
      + "please transfer them to perps in the Hyperliquid interface. Your funds always remain in your own account; Filet "
      + "cannot move or withdraw them.",
    spotStrandedTitle: "You have funds stranded in your spot wallet",
    spotStrandedBody:
      "Copy trading only uses the perpetuals (perp) account; funds stranded in the spot wallet won't be copy-traded and "
      + "won't count toward the amount required for activation. Please transfer these funds to perp in the Hyperliquid interface.",
    spotStrandedAmountLabel: "Spot balance",
    spotStrandedThresholdLabel: "Notice threshold",
    spotStrandedManualNote:
      "This step can only be completed by you: the transfer requires your main wallet's signature, and we don't hold your "
      + "main key, so we can't do it for you — this page will never show a button that does it for you.",
    spotStrandedLink: "Go to Hyperliquid to transfer",
    spotStrandedLinkHref: "https://app.hyperliquid.xyz/balances",
    submitReview: "Complete setup",
    submitted: "Funds detected — taking you to the next step…",
    fundsWarning:
      "During copy trading, deposits and withdrawals automatically recalibrate the drawdown baseline and are not treated " +
      "as losses; but the system sizes your copy-trade positions from your perp account equity, so withdrawing funds will " +
      "immediately scale down all your copy-trade positions proportionally (incurring real trading costs). Before making " +
      "a large withdrawal, we recommend stopping copy trading on this page first.",
    step3Title: "Set your risk limits",
    step3Body: "These values are written into the agent's authorization scope. Filet cannot trade beyond these limits; "
      + "you can always lower them later.",
    step3NextButton: "Continue to fees & risk confirmation",
    ddSaving: "Submitting signature…",
    capitalEffectiveLabel: "Currently in effect",
    capitalPendingLabel: "Submitted, pending engine application",
    leverageInfoPrefix: "This strategy's leverage cap is ",
    leverageInfoSuffix: " (platform-enforced, not user-adjustable)",
    step4Title: "Confirm",
    step4Body: "Last step: confirm fees and risk disclosures. Check all boxes to complete setup.",
    step4CheckLoss: "I understand I may lose money",
    step4CheckFee: "I understand the fee is 0.02% per fill",
    step4CheckRevoke: "I understand I can revoke at any time",
    step4SubmitButton: "Confirm & start copy trading",
    step4Submitting: "Submitting…",
    errors: {
      walletRejected: "Signature rejected — please try again in your wallet. Filet will never ask for your private key or "
        + "seed phrase; signing only happens in your own wallet.",
      signerMismatch: "The signing account doesn't match your login account — please switch your wallet back to the "
        + "account you logged in with and try again. This signature was not submitted.",
      agentUnavailable: "The key service is temporarily unavailable — click \"Retry\" to try "
        + "again; any signature or authorization you've already completed remains valid and "
        + "doesn't need to be redone.",
      agentConflict: "Your agent key state is inconsistent — please contact us for help; any "
        + "on-chain authorization you've already completed is not affected.",
      payloadFailed: "Failed to fetch the content to sign, please try again later.",
      hlTransient: "The network was unstable while submitting the authorization — it's safe to retry; resubmitting the "
        + "same signature won't create a duplicate authorization.",
      hlSemantic: "Hyperliquid rejected this authorization. Click \"Retry\" to fetch new content to sign and sign again.",
      builderPaused: "The system is currently pausing activations, please contact us and try again.",
      verifyIncomplete: "Some conditions are still unmet (authorization or funds not yet confirmed) — please follow the "
        + "on-screen prompts to complete them before submitting.",
      // ⭐ 2026-08-29 decision 6: itemized "complete setup" failure instead of one blanket message.
      verifyAgentPending: "Agent authorization not yet in effect",
      verifyBuilderFeePending: "Builder fee not yet approved",
      verifyNotFunded: "Deposit below threshold",
      contentMismatch: "The content returned by the server doesn't match what you submitted, so this was stopped for "
        + "safety — please refresh the page and try again.",
      submitFailed: "Submission failed, please try again later; a signature that didn't succeed won't be charged or "
        + "applied twice.",
    },
    // ⭐ 2026-08-29 decision 6: short-circuit panel for already-active followers.
    alreadyFollowingTitle: "Already following this strategy",
    alreadyFollowingBodyPrefix: "You're currently following \"",
    alreadyFollowingBodySuffix: "\" — no need to set up again.",
    alreadyFollowingDashboardCta: "Go to Dashboard",
    alreadyFollowingOtherCta: "View other strategies",
  },
  admin: {
    title: "Pending approvals",
    empty: "No items pending approval.",
    forbidden: "This page is admin-only.",
    note: "Activation is handled automatically by the auto-activate watcher (effective about a minute after a leader is "
      + "selected); this page is read-only, for observing the queue — entries stuck here for a long time usually mean the "
      + "user hasn't picked a leader yet, or activation failed (check journalctl -u filet-auto-activate).",
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
    title: "Ops dashboard",
    eyebrow: "OPS",
    forbidden: "This page is admin-only.",
    window: {
      sharedTitle: "The two tables below use the same comparison window",
      sharedNote:
        "Revenue reconciliation and per-customer P&L are taken from the same set of snapshot moments (the backend derives "
        + "both windows from a shared function), so builder fees in the two tables can be directly compared.",
      label: "Comparison window",
      mismatchTitle: "The two tables' comparison windows don't match — the numbers cannot be subtracted",
      mismatchBody:
        "Revenue reconciliation and per-customer P&L should share the same window, but the two windows received are "
        + "actually different — subtracting builder fees between the two tables in this state produces a false "
        + "discrepancy (a one-day window offset once made a healthy account look like a massive shortfall). Please check "
        + "whether the backend window derivation has regressed; do not draw reconciliation conclusions from this page "
        + "until it's fixed.",
      revenueLabel: "Revenue reconciliation window",
      customersLabel: "Per-customer P&L window",
      tradeQualityLabel: "Trade quality window",
      tradeQualityShared: "The trade quality panel is also taken from the same set of snapshot moments, so all three "
        + "tables can be compared side by side.",
      tqMismatchTitle: "Trade quality's comparison window doesn't match the reconciliation tables — the numbers cannot "
        + "be compared side by side",
      tqMismatchBody:
        "Trade quality should share the same window derivation function as revenue reconciliation, but the window "
        + "received is actually different — reading delay and slippage alongside the other two tables in this state "
        + "would treat two different time periods as the same batch. Please check whether the backend window derivation "
        + "has regressed; do not compare these tables side by side until it's fixed.",
    },
    revenue: {
      title: "Revenue reconciliation",
      note: "Attributed = sum of builder fees attributed to each customer's fills; actual = today-minus-yesterday delta "
        + "of the builder address's on-chain accrual (queried once, not derived from the customer rows). The gap between "
        + "the two is the reconciliation signal.",
      attributed: "Attributed (owed)",
      accruedDelta: "Actual (on-chain delta)",
      discrepancy: "Discrepancy (actual − attributed)",
      discrepancyPct: "Discrepancy %",
      threshold: "Alert threshold",
      window: "Reconciliation window",
      rowsCounted: "Customer rows included in attribution",
      ok: "Discrepancy within threshold.",
      alertTitle: "Revenue reconciliation exceeds threshold",
      alertBody:
        "The attribution analysis doesn't match the on-chain actual — please check the day's fill details before "
        + "adjusting attribution. Common causes: "
        + "(1) fills routed via the modify path carry no builder field and earn no fee; "
        + "(2) fills not routed through us (self-directed customer orders) count toward equity but generate no builder fee; "
        + "(3) on-chain accrual posts with a delay, causing temporary misalignment across UTC day boundaries.",
      insufficient: "Historical data is still accumulating; at least two daily snapshots are needed to reconcile.",
      insufficientNote:
        "Actual delta = the difference between today's and yesterday's snapshot; with only one snapshot there's no way "
        + "to know the single-day delta. We deliberately don't show 0 here — treating the whole accumulated total as a "
        + "single day's delta would produce an astronomically false discrepancy. The daily report script keeps "
        + "accumulating snapshots.",
      pctUnavailable: "Attributed is 0, so the percentage can't be calculated (see the amount column directly).",
      basisUnknown: "Snapshot moments could not be aligned; reconciliation skipped for this day.",
      basisUnknownNote:
        "Both ends of the comparison window can only come from snapshot moments; when one is missing or out of order, "
        + "any window would be a guess. We deliberately show no amount or range here — a reconciliation number offset by "
        + "a day would send someone chasing a problem that doesn't exist.",
    },
    customers: {
      title: "Per-customer P&L",
      note: "This table uses the same comparison window as revenue reconciliation above (the same set of snapshot "
        + "moments), so builder fees in both tables can be directly compared.",
      daysNote:
        "Free-range window: this table uses \"now minus N days,\" a different basis from the reconciliation window "
        + "above (snapshot moments) — the numbers in the two tables cannot be directly subtracted. Switch back to "
        + "\"reconciliation window\" to compare side by side.",
      basisUnknown: "Snapshot moments could not be aligned; calculation skipped for this table.",
      basisUnknownNote:
        "Both ends of the reconciliation window can only come from snapshot moments; when one is missing or out of "
        + "order, any window would be a guess. We deliberately show no customer numbers here — P&L on a different basis "
        + "than revenue reconciliation would be misread as subtractable. To view customer details first, use the "
        + "free-range window above (which uses a different basis than the reconciliation table).",
      empty: "No customer data available.",
      rangeLabel: "Reporting period",
      ranges: { accrued: "Reconciliation window (matched basis)", d1: "1 day", d7: "7 days", d30: "30 days" },
      manifestErrors: "The manifest has invalid entries that were skipped; the following items are not included in "
        + "this table (other customers display normally):",
      rowError: "This row failed to query:",
      rowErrorHint: "A single customer query failure only affects this row; the numbers for other customers remain valid.",
      cols: {
        account: "Account",
        address: "Address",
        fills: "Fill count",
        notional: "Routed notional",
        builderFee: "Attributed builder fee",
        takerShare: "Taker share",
        accountValue: "Account equity",
        subscription: "Subscription status",
      },
    },
    tradeQuality: {
      title: "Trade quality",
      note:
        "Pairing delay = the time difference between our fill and the leader's corresponding fill; slippage is in bp, "
        + "with positive values meaning the fill price was unfavorable to the follower. These figures call the same "
        + "formula as the daily reconciliation report, not a separately computed copy (if the two formulas drifted, the "
        + "two tables side by side wouldn't reveal they'd diverged).",
      sameWindowNote: "This table is taken from the same set of snapshot moments as revenue reconciliation and "
        + "per-customer P&L above, so all three tables can be compared side by side.",
      daysWindowNote:
        "Free-range window: this table uses \"now minus N days,\" a different basis from revenue reconciliation's "
        + "window above (snapshot moments) — the two cannot be compared side by side. Switch back to \"reconciliation "
        + "window\" to compare all three tables side by side.",
      basisUnknown: "Snapshot moments could not be aligned; trade quality calculation skipped this time.",
      basisUnknownNote:
        "Both ends of the comparison window can only come from snapshot moments; when one is missing or out of order, "
        + "any window would be a guess. We deliberately show no quality numbers here — delay and slippage on a "
        + "different basis than other tables would be misread as directly comparable.",
      loadFailed: "Trade quality failed to load; this section's numbers are unavailable (other sections are unaffected):",
      empty: "No customers with computable trade quality.",
      summaryTitle: "Cross-customer summary (worst value, not average)",
      summaryNote:
        "The summary only covers customers for whom a value could be computed, and the sample size is listed alongside "
        + "— showing only a single worst value would make \"1 of 10 customers has data\" look identical to \"all 10 "
        + "have data\" on screen. A median of medians is not a median, so we deliberately don't provide an average.",
      stats: {
        followers: "Follower count",
        qualityAvailable: "Customers with quality data",
        teAvailable: "Pairable customers (known who they follow)",
        skippedAvailable: "Customers with skipped-small-order data",
        worstDelay: "Worst median pairing delay (seconds)",
        delaySample: "Delay sample size (customers)",
        worstSlippage: "Worst median taker slippage (bp)",
        slippageSample: "Slippage sample size (customers)",
      },
      cols: {
        account: "Account",
        fills: "Our fill count",
        takerShare: "Taker share",
        pairCount: "Paired fill count",
        medianDelay: "Median pairing delay (seconds)",
        slippage: "Median taker slippage (bp)",
        skippedNotional: "Skipped small-order notional",
        skippedRatio: "Skipped-small-order ratio",
      },
      teUnavailable: "Cannot pair",
      teUnavailableHint: "It's unknown who this customer is following (manifest doesn't record leader_address), so "
        + "delay and slippage cannot be computed — we deliberately don't fill in 0, since \"0 second delay\" reads as "
        + "perfect copy-trade quality.",
      skippedUnavailable: "Unreadable",
      skippedUnavailableHint: "The skipped-small-order log couldn't be read (or the window covers days for which only "
        + "some have log files). The total for partial days will read low, and a low number happens to look exactly "
        + "like \"the engine isn't skipping any orders.\"",
      ratioIncomparable: "Cannot be computed for this window",
      ratioIncomparableHint: "Skipped-small-order data is logged per full calendar day (no per-fill timestamps in the "
        + "file), while routed notional is filtered by window. When the window isn't a full UTC day, the numerator and "
        + "denominator of this ratio are on different bases — the resulting quotient would look exactly like a normal "
        + "ratio, so we deliberately don't compute it. The notional figure is still meaningful; see the column on the left.",
      skippedDaysLabel: "Days logged for skipped-small-orders (the ratio's denominator basis)",
      rowError: "This row failed to query:",
      rowErrorHint: "A single customer query failure only affects this row; the quality numbers for other customers "
        + "remain valid.",
      window: "This table's window",
    },
    health: {
      title: "System health",
      note: "A cross-customer sweep of engine status. Every cell has an \"unknown\" state — anything that can't be "
        + "read is marked unknown and never falls back to a value that looks safe. It's intentional that \"unknown\" "
        + "looks jarring.",
      loadFailed: "System health failed to load; this section's status is unavailable (other sections are unaffected):",
      empty: "No customers to check.",
      checkedAtLabel: "Checked at",
      staleAfterLabel: "Heartbeat considered stale after this many seconds",
      basisTitle: "\"Equity sample\" is a proxy metric, not a process liveness check",
      basisBody: "That column looks at whether the engine has recently written an equity sample (one per cycle). A "
        + "process that's still writing files but no longer placing orders would show green in that column. True "
        + "process liveness is managed by systemd (see \"systemctl status\" in the RUNBOOK) — don't use this column "
        + "as a substitute for it.",
      basisLabel: "Sampling basis",
      basisEquitySample: "Equity sample (engine writes one per cycle)",
      basisUnknownPrefix: "Basis code reported by the backend (no matching explanation on this page yet):",
      sourceTitle: "Where this table's data comes from",
      sourceBody: "The engine's state directory is only readable by the engine itself (0700); the panel runs under a "
        + "different account. So the panel's data comes from a narrow summary (heartbeat) that the engine actively "
        + "publishes every cycle; for a few cells the panel can read directly. Each row's \"source\" column notes "
        + "which side its kill switch and coverage figures came from — direct reads are fresher.",
      sourceLabel: "Source",
      sources: {
        state_root: "Direct state-root read",
        heartbeat: "Engine heartbeat",
        absent: "State root doesn't exist",
        unreadable: "State root unreadable",
      },
      sourceUnknownPrefix: "Source code reported by the backend (no matching explanation on this page yet):",
      heartbeat: {
        ok: "Heartbeat fresh",
        stale: "Heartbeat stale",
        missing: "Never received a heartbeat",
        unreadable: "Heartbeat unreadable",
        unknownPrefix: "Heartbeat status code reported by the backend (no matching explanation on this page yet):",
        okHint: "The engine wrote a heartbeat within the threshold window, so this row's leader and capital settings have values.",
        staleHint: "The engine hasn't written a heartbeat past the threshold — it may not be running, or it may be "
          + "running but unable to write to the exchange directory. None of the values in the heartbeat are shown as "
          + "current (the backend structurally omits them once stale).",
        missingHint: "This customer has never had a heartbeat file. Most likely just activated and the engine hasn't "
          + "run its first cycle yet, or the exchange directory's subchannel hasn't been set up yet (deployment TODO).",
        unreadableHint: "The heartbeat file exists but couldn't be read (malformed, or timestamped in the future). "
          + "This row's status is entirely unconfirmed.",
      },
      state: {
        alive: "All good",
        stale: "Sample stale",
        engineUnknown: "Sample unknown",
        tripped: "Tripped",
        armed: "Not tripped",
        killswitchUnknown: "Status unknown",
        covered: "In effect",
        insufficient: "Not yet in effect",
        coverageUnknown: "Coverage unknown",
        unknown: "Unknown",
      },
      engineStateTitle: "Engine state (unknown whenever heartbeat isn't fresh)",
      engineStateNote: "These are the values actually used by the engine in its last cycle. When the heartbeat is "
        + "stale or unreachable, these cells are always \"unknown\" — the backend structurally omits these values "
        + "once the heartbeat is stale, so this page can never present a setting from tens of minutes ago as if it "
        + "were currently in effect.",
      engineStateCols: {
        account: "Account",
        leader: "Current leader",
        leaderSource: "Leader source",
        allocated: "Allocated capital",
        utilization: "Utilization",
        fullEquity: "Uses full equity",
        capitalSource: "Capital source",
        lastCycle: "Last cycle",
      },
      yes: "Yes",
      no: "No",
      staleTitle: "Some customers' engine heartbeats are stale",
      staleBody: "These customers' engines have gone past the threshold without writing a heartbeat. A stopped "
        + "heartbeat doesn't mean positions were closed, but it does mean drawdown protection and copy trading may "
        + "both have stopped working — the last-heartbeat time in the table below is \"last seen,\" not its current status.",
      trippedTitle: "Some customers' kill switch has tripped, and these customers have stopped copy trading",
      trippedBody: "Drawdown protection has tripped and closed positions for these customers' accounts; the engine "
        + "will not copy trade for them again until manually re-armed. Please confirm the customer has been notified "
        + "before following the RUNBOOK to re-enable.",
      unknownTitle: "Some cells are unreadable; the status of these items is unconfirmed",
      unknownBody: "These counts don't mean \"no problem,\" they mean \"can't see it\" — the most common cause is "
        + "filet-api lacking read permission on the engine's state directory (the state root is 0700, crossing a "
        + "permission boundary). While unreadable, whether the kill switch has tripped or drawdown protection is "
        + "active is unknown; fix the permission issue first before reading this table.",
      cols: {
        account: "Account",
        heartbeat: "Engine heartbeat",
        lastBeat: "Last heartbeat",
        engine: "Equity sample",
        coverage: "Drawdown protection coverage",
        killswitch: "Kill switch",
        alerts: "Alert count",
        source: "Source",
      },
      stats: {
        followers: "Follower count",
        heartbeatOk: "Heartbeat OK",
        heartbeatStale: "Heartbeat stale",
        heartbeatMissing: "Heartbeat never written",
        engineAlive: "Equity sample alive",
        engineStale: "Equity sample stale",
        engineUnknown: "Equity sample unreadable",
        killswitchTripped: "Kill switch tripped",
        killswitchUnknown: "Kill switch unreadable",
        coverageInsufficient: "Drawdown protection sample insufficient",
        coverageUnknown: "Drawdown protection unreadable",
        alertsTotal: "Total alerts",
        alertsUnknown: "Alert count unreadable",
        backlog: "Pending leader-switch backlog",
      },
      units: { sec: "s", min: "min", hour: "hr", day: "d" },
      ageSuffix: "ago",
      lastBeatNever: "Never received a heartbeat",
      lastBeatNeverHint: "This customer's state directory has no equity samples at all. May have just activated and "
        + "the engine hasn't run its first cycle yet, or the engine may have never come up — this page can't tell "
        + "the two apart, so it doesn't guess.",
      coverageInsufficientHint: "Sample is under one hour or fewer than two points: drawdown protection's peak "
        + "doesn't yet have enough history, so protection isn't truly active yet. This is a definite answer, not \"unreadable.\"",
      rowError: "This row failed to load:",
      backlogTitle: "Leader switch: written but never applied by the engine",
      backlogNote: "The customer signed, the API accepted it and replied \"effective next cycle,\" but the engine "
        + "never applied it — this chain spans two processes and two permission sets, and its failures are silent "
        + "(both sides' logs look fine, and the customer thinks the switch went through). This check calls the same "
        + "function as the daily reconciliation report, not a separate one.",
      backlogEmpty: "No pending leader-switch backlog.",
      backlogUnknownTitle: "Leader-switch backlog is unknown",
      backlogUnknownBody: "This chain can't be checked (the exchange directory isn't set up, or the log file is "
        + "unreadable), so this page shows no backlog count. We deliberately don't show 0 here — 0 would be read as "
        + "\"no backlog,\" when it actually means \"unknown whether there's a backlog,\" and those are opposites. "
        + "Reasons below:",
      backlogCols: { account: "Account", nonce: "nonce", age: "Record age", reason: "Reason" },
      reasons: {
        not_redeemed: "The record has expired with no redemption in the engine's ledger — the customer signed, but "
          + "the engine never applied it",
        bad_issued_at: "The record's issued_at is invalid; the engine will reject this change",
        ledger_unreadable: "The engine ledger is unreadable, so it can't be confirmed whether this was applied (not "
          + "the same as \"not applied\")",
      },
      reasonUnknownPrefix: "Reason code reported by the backend (no matching explanation on this page yet):",
      manifestErrors: "The manifest has invalid entries that were skipped; the following items are not included in "
        + "this section (other customers are checked normally):",
    },
    subscriptions: {
      title: "Subscription reconciliation",
      note: "Local billing table vs. Stripe's real status. The webhook is the sole writer of the local table; missing "
        + "one message causes permanent drift, and this table is the only way to notice it.",
      detectOnly: "This section only detects, it does not fix: overwriting locally with Stripe as the source of truth "
        + "would directly change billing and entitlements, which is a human decision. If you see drift, first confirm "
        + "the truth in the Stripe dashboard before deciding how to handle it.",
      clean: "All four drift categories are zero; local and Stripe match.",
      counts: {
        inSync: "In sync",
        drift: "Total drift",
        stripe: "Stripe subscriptions",
        local: "Local records",
        superseded: "Superseded (returning customers)",
      },
      supersededNote: "\"Superseded\" is a historical subscription for the same customer, not drift, and isn't "
        + "counted in total drift.",
      truncatedTitle: "Sample incomplete, this section's conclusions are unreliable",
      truncatedBody: "The Stripe subscription list hit its 1000-record limit, so a complete sample wasn't retrieved. "
        + "In this state, \"missing from Stripe\"-type findings may all be false drift (the subscription actually "
        + "exists, it just wasn't fetched). Please don't disable any customer based on this section — narrow the "
        + "query range first, or confirm directly in the Stripe dashboard row by row.",
      lists: {
        stripeActiveLocalNot: {
          title: "Customer paid but didn't get entitlements (highest harm)",
          desc: "Stripe shows the subscription active, but locally there's no record or it's non-active — the "
            + "customer is paying but not receiving the paid features. Handle first.",
        },
        localActiveStripeNot: {
          title: "Still providing service but not getting paid (revenue leak)",
          desc: "Local is active, but on Stripe it's non-active or the subscription doesn't exist — service "
            + "continues to be delivered, but the payment never arrived.",
        },
        statusMismatch: {
          title: "Status mismatch between the two sides (needs manual review)",
          desc: "Both sides have a record but the status doesn't match (e.g., local is past_due while Stripe shows "
            + "canceled); entitlement decisions follow the local value, which may not be the current truth.",
        },
        orphanStripe: {
          title: "Stripe subscriptions with no matching local account (non-active)",
          desc: "Mostly manually created externally or leftover test data. Non-active, so no immediate entitlement "
            + "impact, but it will make billing reports not reconcile.",
        },
      },
      empty: "None",
      cols: {
        account: "account_id",
        local: "Local status",
        stripe: "Stripe status (normalized)",
        raw: "Stripe raw value",
        subId: "stripe_subscription_id",
        matchedBy: "Matched by",
      },
    },
  },
  advanced: {
    eyebrow: "Advanced mode",
    title: "Advanced mode: follow any address",
    subtitle: "Skip the curated list and set up copy trading directly for any Hyperliquid wallet address.",
    fundsWarning:
      "Reminder: during copy trading, deposits and withdrawals automatically recalibrate the drawdown baseline and "
      + "are not treated as losses; but withdrawing funds will immediately scale down your copy-trade positions "
      + "proportionally. We recommend stopping copy trading before making a large adjustment.",
    gate: {
      title: "Before you continue",
      body: "In advanced mode, Filet makes no endorsement of this address's strategy quality, risk controls, or "
        + "continuity — the address you paste receives no additional platform vetting, and whether it's worth "
        + "following is your own judgment.",
      checkboxLabel: "I understand Filet makes no endorsement of this address's strategy quality, risk controls, "
        + "or continuity.",
    },
    notLoggedIn: {
      title: "Please log in to continue",
      body: "Advanced mode requires connecting your wallet and logging in before you can look up an address and "
        + "continue onboarding.",
      cta: "Connect wallet and log in",
      connecting: "Connecting…",
      signing: "Please sign the login message in your wallet…",
      exploreExit: "Or browse curated strategies first →",
    },
    upperBound: "A follower's actual results will fall below the leader's numbers: entry/exit delay, slippage, and "
      + "differences in capital size will continuously erode copy-trade results. The leader's numbers are an upper "
      + "bound, not your expected return. Copy trading carries risk of loss.",
    custom: {
      title: "Who to follow",
      subtitle: "Paste any wallet address to start copy trading. Pasted addresses receive no additional platform "
        + "vetting — whether it's worth following is your own judgment.",
      leaderboardLabel: "For research, you can refer to Hyperliquid's official leaderboard (external link, opens "
        + "in a new tab):",
      leaderboardLinkText: "app.hyperliquid.xyz/leaderboard",
      inputLabel: "Leader wallet address",
      inputPlaceholder: "0x…",
      inputHint: "0x followed by 40 hex characters. The query reads this account's on-chain Hyperliquid perp "
        + "activity; if the address has no activity yet, you can still complete setup (copy trading starts "
        + "automatically once it becomes active).",
      formatError: "Invalid address format: must be 0x followed by 40 hex characters.",
      check: "Check",
      checking: "Checking…",
      previewTitle: "On-chain preview",
      previewNote: "This is an on-chain snapshot at the time of the query, meant to help you confirm the address "
        + "wasn't mistyped — it is not performance, and it is not a recommendation.",
      previewAccountValue: "Account equity",
      previewAccountValueHint: "This address's account equity at the time of the query (a live on-chain read, not "
        + "a daily snapshot).",
      previewPositionCount: "Position count",
      previewPositionCountHint: "The number of positions this address held at the time of the query.",
      alreadyListedBadge: "This address is on the curated list",
      vaultBadge: "Vault",
      vaultNote: "This address is a Hyperliquid vault. When copy trading it, the engine automatically applies a 20x "
        + "leverage cap and neutralizes the vault's deposit/redemption flows (so subscriptions/redemptions aren't "
        + "mistaken for trading P&L).",
      vaultCheckWarning: "The engine may not be able to precisely account for this vault's ledger shape — please "
        + "understand the risk before continuing.",
      alreadyListedNote: "This address is already on the platform's curated leader list; the rest of the flow is "
        + "the same as for any other address. Since you're using the address-input box, the declaration below is "
        + "still required.",
      alreadyListedNoteNotShown: "This address is already on the platform's leader list, but whether it's currently "
        + "open to copy trading can't be confirmed right now (for example, it may have paused new followers, or the "
        + "list is temporarily unreadable). The rest of the flow is the same as for any other address; since you're "
        + "using the address-input box, the declaration below is still required.",
      attestation: "I understand this is an unvetted leader and I bear the risk myself.",
      select: "Continue to onboarding",
      reasons: {
        invalid_format: "Invalid address format: must be 0x followed by 40 hex characters.",
        self_follow: "This is your own login address — you can't follow yourself.",
        leader_disabled: "This address has been safety-revoked by the platform (leader incident) and can't be followed.",
      },
      notAcceptingNewWarning: "⚠️ This leader is currently not accepting new followers (routine delisting or slot "
        + "adjustment). You can still complete setup and follow — this is just a status the platform has flagged.",
      noActivityWarning: "This address currently has no perp trading activity on Hyperliquid (zero equity and no "
        + "positions). If this leader hasn't entered the market yet, you can complete copy-trade setup now — the "
        + "engine will start copy trading automatically once they enter. Please also double-check the address "
        + "wasn't mistyped.",
      previewFailed: "Query failed; this query didn't change your copy-trade settings. Please click \"Check\" again later.",
      echoMismatch: "Aborted: the preview the server returned doesn't match the address you entered, so this "
        + "preview is not displayed. Please stop and report this to support.",
      entryName: "Unvetted leader",
    },
  },
  home: {
    hero: {
      badge: "Non-custodial · funds never leave your wallet",
      title: "Let your Hyperliquid account automatically follow quant strategies",
      sub: "Your funds always stay in your own wallet. Filet can only place orders within the risk limits you set — "
        + "it can never withdraw or transfer — and you can pause or revoke authorization at any time.",
      ctaPrimary: "View strategies & risks",
      ctaSecondary: "What can authorization do?",
      microNote: "No sign-up, no email required. Connecting a wallet only happens after you pick a strategy.",
      featuredCard: {
        statusRunning: "Running",
        statusPaused: "Paused",
        leaderPrefix: "leader ",
        leaderLinkSuffix: " · verifiable on-chain",
        returnLabelPrefix: "Sample ",
        returnLabelSuffix: "-day full-period return",
        // Task 7 (D5): drawdown label now reuses `strategyDetail.metrics.maxDrawdownLabel`
        // ("Strategy drawdown") directly in page.tsx — same key across detail/home/traders.
        liveDaysLabel: "Days live",
        followerCountLabel: "Current followers",
        sampleNotePrefix: "Sample ",
        sampleNoteSuffix: " trading days; small sample, wider metric bands.",
        methodologyLink: "Full methodology disclosure →",
        noDataNote: "No featured strategy card available yet — please browse the strategy list directly.",
      },
    },
    evidence: {
      routedVolumeLabel: "Total routed volume",
      routedVolumeLink: "hyperliquid explorer ↗",
      liveDaysLabel: "Consecutive days live (in-house strategy)",
      liveDaysSuffix: " days",
      liveDaysLink: "leader account ↗",
      builderFeeLabel: "Builder fee (per fill)",
      builderFeeLink: "builder code ↗",
      custodyLabel: "Assets in custody (always)",
      custodyValue: "0",
      custodyLink: "authorization scope ↗",
    },
    strategies: {
      heading: "Strategies you can follow",
      sub: "Every strategy's performance comes from a Hyperliquid on-chain account that anyone can independently "
        + "verify.",
      viewAll: "All strategies →",
      featuredBadge: "Featured",
      pendingBadge: "Not open to new followers",
      metricTotalReturn: "Total return",
      metricMaxDrawdown: "Max drawdown",
      metricSharpe: "Sharpe",
      metricWinRate: "Daily win rate",
      insufficientLabel: "Insufficient sample",
      leveragePrefix: "Leverage ≤ ",
      minNotionalPrefix: "Min. follow $",
      cta: "View strategy & risks",
      pendingNote: "This strategy is not currently open to new followers. Existing followers are unaffected.",
      empty: "No strategies are open to follow right now — please check back later.",
      advancedTitle: "Advanced mode",
      advancedBody: "Already have a leader address in mind? You can follow any Hyperliquid wallet. In this mode "
        + "Filet makes no endorsement of that address's strategy quality, risk controls, or continuity — an extra "
        + "risk acknowledgement is required.",
      advancedCta: "Enter advanced mode",
    },
    steps: {
      heading: "Four steps to start following",
      items: [
        { n: "01", t: "Choose a strategy", d: "Decide only after reviewing performance, drawdown, and methodology disclosure." },
        { n: "02", t: "Connect wallet & authorize", d: "Two free signatures: agent order authority and the builder fee rate cap — neither goes on-chain." },
        { n: "03", t: "Set risk limits", d: "Allocation share, leverage cap, and max-drawdown auto-stop." },
        { n: "04", t: "Enter Dashboard", d: "Sync status, exposure, PnL, and fees on one page — pause anytime." },
      ],
    },
  },
  auth: {
    heading: "What Filet can — and can't — do after authorization",
    sub: "Neither wallet signature goes on-chain or costs gas. Here is the exact boundary of agent permissions.",
    canTitle: "Can",
    cannotTitle: "Can't",
    can: [
      "Place, add to, and close orders in your account based on strategy signals",
      "Use margin within the range you set (allocation share cap)",
      "Execute within the strategy's stated leverage cap (platform-enforced)",
      "Collect builder fee up to the rate cap you signed (0.02%)",
    ],
    cannot: [
      "Withdraw your funds to any address",
      "Transfer or move assets between wallets",
      "Obtain your private key or seed phrase (never requested)",
      "Exceed the allocation share you signed; exceed the strategy's stated leverage cap; "
        + "breach the max drawdown you enabled without stopping",
    ],
    revocable: "Authorization is unilaterally revocable by you: pause following from the Dashboard with one click "
      + "(stops new order placement), or revoke the agent (Filet immediately loses order authority). Revocation "
      + "needs neither Filet's consent nor Filet to be online.",
  },
  fee: {
    heading: "Fee: 0.02% per fill",
    body: "No monthly fee, no profit share, no withdrawal fee. The builder fee is recorded on-chain, and every "
      + "fill can be verified.",
    note: "This rate is the cap you signed at authorization — we cannot raise it unilaterally. Hyperliquid's own "
      + "trading fees and funding rates are separate.",
    calcLabel: "Estimate: position size",
    openLabel: "Open builder fee",
    closeLabel: "Close builder fee",
    totalLabel: "Total for one round trip",
  },
  strategyDetail: {
    breadcrumb: "Strategies",
    runningPill: "Running",
    pausedPill: "Paused",
    leaderPrefix: "leader ",
    leaderLinkSuffix: " ↗",
    asOfPrefix: "As of ",
    sourceSuffix: " · Source: Hyperliquid API",
    notFoundTitle: "Strategy not found",
    notFoundBody: "This strategy doesn't exist, or has been delisted. Please go back to the strategy list.",
    backToList: "Back to strategies →",
    loadingNote: "Loading strategy data…",
    equity: {
      heading: "Account equity curve (USD)",
      periodAll: "All",
      period30d: "30D",
      period7d: "7D",
      overlayLabel: "Overlay:",
      overlayNote: "No comparison data source available yet",
      overlays: ["BTC", "ETH", "S&P 500", "Gold"],
      empty: "Not enough data to draw an equity curve yet.",
    },
    metrics: {
      totalReturnLabel: "Total return",
      totalReturnNote: "From real deposit",
      maxDrawdownLabel: "Strategy drawdown",
      maxDrawdownNote: "Deepest single instance in period",
      sharpeLabel: "Sharpe (annualized)",
      sharpeNoteSuffix: " (1 s.e.)",
      winRateLabel: "Daily win rate",
      winRateNotePrefix: "N=",
      winRateNoteSuffix: " daily return samples",
      annualizedVolLabel: "Annualized volatility",
      annualizedVolNote: "365-day convention",
      sortinoLabel: "Sortino",
      sortinoNote: "Downside-risk adjusted",
      bestWorstLabel: "Best / worst day",
      bestWorstNote: "Single-day %",
      startEndEquityLabel: "Start → end equity",
      startEndEquityNote: "Real deposit → current equity",
      insufficientLabel: "Insufficient sample",
      insufficientGroupLabel: "Sharpe／Sortino／Annualized vol／Start–end equity／Best-worst day",
      insufficientGroupPrefix: ": insufficient sample (",
      insufficientGroupMid: "/",
      insufficientGroupSuffix: " days), shown once the threshold is met",
    },
    cagr: {
      heading: "CAGR (annualized)",
      toggleShow: "Expand",
      toggleHide: "Collapse",
      notePrefix: "Only ",
      noteSuffix: " days of sample — annualized extrapolation has no statistical meaning, so it's deliberately "
        + "grayed out and never shown on the homepage or strategy cards.",
    },
    methodology: {
      heading: "Methodology & sample disclosure",
      unavailable: "Methodology data is not available right now.",
      depositPrefix: "Computed from a real deposit of $",
      depositSuffix: " (verifiable on-chain), covering ",
      /** Mirrors zh rangePrefix: opening used when the deposit clause is omitted. */
      rangePrefix: "Covering ",
      daysSuffix: " trading days (",
      rangeSuffix: ").",
      sharpePrefix: "Sharpe ",
      sharpeSeInfix: ", standard error ±",
      sharpeSeSuffix: " (N=",
      sampleSuffix: " daily return samples).",
      conventionPrefix: "Metrics are annualized using the crypto convention of ",
      conventionMid: " days/year, risk-free rate ",
      conventionSuffix: ".",
      basisNote: "Metrics are computed on a perp (perpetual futures) account equity basis.",
    },
    panel: {
      heading: "Follow this strategy",
      scaleLabel: "Allocation (% of account equity)",
      leverageLabel: "Leverage cap",
      ddLabel: "Max-drawdown auto-stop",
      ddEnableLabel: "Enable max-drawdown auto-stop",
      ddDisabledNote: "Off by default. If enabled, the actual threshold is confirmed in the next step via the "
        + "existing signature flow.",
      estDepositLabel: "Estimated deposit",
      estDepositValue: "Calculated after wallet connect",
      builderFeeLabel: "Builder fee",
      builderFeeValue: "0.02% / fill",
      estMonthlyLabel: "Estimated monthly fee",
      estMonthlyValue: "Depends on volume",
      cta: "Connect wallet & continue",
      ctaConnecting: "Connecting…",
      ctaSigning: "Please sign the login message in your wallet…",
      footnote: "The next step is just a free signature (no on-chain transaction, no gas). You'll see the full "
        + "permission scope and fee confirmation before authorizing.",
      pendingCta: "Not open to new followers",
      pendingNote: "This strategy is not currently open to new followers. Existing followers are unaffected.",
    },
  },
  dashboard: {
    heading: "Dashboard",
    lastSyncPrefix: "Last synced ",
    lastSyncSuffix: " ago",
    lastSyncJustNow: "just now",
    liveBadge: "Live",
    loadingNote: "Loading dashboard data…",
    status: {
      label: "Strategy status",
      strategyFallback: "This account",
      stateFollowing: "Following",
      statePaused: "Paused",
      stateHalted: "Halted",
      stateInactive: "Not activated",
      followingDaysPrefix: "Following for ",
      followingDaysSuffix: " days",
      signalOk: "Signal source healthy",
      signalUnknown: "Signal source status unknown",
      pauseBtn: "Pause following",
      resumeBtn: "Resume following",
      closeAllBtn: "Close all & revoke authorization",
      pauseErrorNote: "Action failed, please try again later.",
      closeAllModal: {
        title: "Confirm: close all & revoke authorization",
        warning: "This will close all of your current copy-trading positions at market "
                + "and stop following. This action is irreversible.",
        positionsHeading: "Positions to be closed",
        noPositions: "No open positions.",
        ackLabel: "I understand this is irreversible and following will not resume "
                 + "automatically after this completes.",
        confirmBtn: "Confirm close all & revoke",
        cancelBtn: "Cancel",
        signingNote: "Please sign in your wallet…",
      },
      closeAllProgress: {
        title: "Winding down",
        note: "The engine has received your request and is cancelling orders and closing "
             + "positions — this usually finishes within a minute; this panel updates "
             + "automatically.",
      },
      closeAllDone: {
        title: "Close all & revoke complete",
        note: "Following has stopped and will not resume automatically. This action did "
             + "not revoke the API wallet's on-chain permissions — please remove it "
             + "yourself on the official Hyperliquid interface.",
        linkLabel: "Go to Hyperliquid to remove the API wallet",
        steps: [
          "Sign in at app.hyperliquid.xyz",
          "Open the \"API\" settings page",
          "Find the API wallet created by this site and remove its permissions",
        ],
      },
      closeAllFailed: {
        title: "Processing timed out",
        note: "The engine did not process your close-all request in time — it may be "
             + "temporarily offline. Your authorization and position status are unchanged. "
             + "Please try again later, or go to the official Hyperliquid interface to "
             + "remove the API wallet and close your positions yourself.",
        linkLabel: "Go to Hyperliquid to remove the API wallet",
      },
      guardsHeading: "Risk guardrails (set vs current)",
      guardScale: "Allocation",
      guardLeverage: "Leverage",
      guardDrawdown: "Drawdown (from peak)",
      drawdownDisabled: "Not enabled · Go to settings →",
    },
    equity: {
      label: "Account equity & available margin",
      custodyNote: "Assets stay in your own wallet; Filet has no withdrawal permission",
      retSuffix: " 30D",
      usedMargin: "Margin used",
      availableMargin: "Available margin",
      lowMarginWarning:
        "Available margin is low. New entries may be skipped — consider depositing more or lowering your "
        + "allocation.",
      criticalMarginWarning:
        "Available margin is critically low — new entries will very likely be skipped. Please deposit more "
        + "or lower your allocation as soon as possible.",
    },
    exposure: {
      label: "Current exposure",
      notionalSuffix: " notional",
      long: "Long",
      short: "Short",
      biasLabel: "Directional bias",
      biasLong: "Net long",
      biasShort: "Net short",
      biasNeutral: "Neutral",
      positionCount: "Open positions",
      maxPosition: "Largest single position",
    },
    pnl: {
      label: "Net PnL (after builder fee)",
      realizedPrefix: "Realized ",
      unrealizedPrefix: " · Unrealized ",
      chartEmpty: "Not enough data to draw the chart yet.",
      winRate: "Win rate",
      closedPositions: "Closed positions",
      maxDrawdown: "Your following drawdown",
      feeShare: "Fees / PnL",
    },
    sync: {
      label: "Master / follower sync deviation",
      latencyMedian: "Signal latency (median)",
      latencyP95Prefix: "p95 ",
      priceDiff: "Fill price diff",
      priceDiffNote: "Weighted average",
      unsyncedPositions: "Unsynced positions",
      scaleDeviation: "Position ratio deviation (vs master)",
      missedSignals: "Missed signals (24h)",
      lastRecon: "Last full reconciliation",
      warmingLine: "Sync deviation: starts accumulating within 24h of activation",
      errorLine: "Engine status unavailable",
    },
    fees: {
      label: "This month's volume & builder fee",
      routedVolume: "Routed volume",
      builderFees: "Builder fee accrued",
      fillCount: "Fill count",
      avgFee: "Average fee / fill",
      effectiveRate: "Effective rate",
    },
    tabs: {
      positions: "Followed positions",
      fees: "Fee detail",
      history: "Fill history & authorization log",
    },
    positionsTable: {
      symbol: "Symbol",
      value: "Position value",
      upnl: "Unrealized",
      entry: "Entry price",
      mark: "Mark price",
      deviation: "vs master",
      long: "Long",
      short: "Short",
      marginModeCross: "Cross",
      marginModeIsolated: "Isolated",
      empty: "No followed positions right now.",
    },
    feesTable: {
      periodThisMonth: "This month",
      periodLastMonth: "Last month",
      periodAll: "All time",
      summaryBuilderFee: "Builder fee total",
      summaryRoutedVolume: "Routed volume",
      summaryFillCount: "Fill count",
      summaryPnlShare: "Share of realized PnL",
      exportCsv: "Export CSV",
      colDate: "Date ↓",
      colFillCount: "Fills",
      colRoutedVolume: "Routed volume",
      colBuilderFee: "Builder fee",
      colEffectiveRate: "Effective rate",
      loadMore: "Load 20 more days",
      footerNote:
        "Dates use UTC day boundaries. “—” means no fills that day, "
        + "distinct from a $0.00 fee. Effective rate = that day's fee ÷ routed volume, "
        + "so you can verify the 0.02% cap was never exceeded.",
      loading: "Loading…",
      loadError: "Fee detail temporarily unavailable, please try again later.",
      empty: "No fills in this period yet.",
    },
    history: {
      fillsTitle: "Fill history",
      authorizationsTitle: "Authorization log",
      loading: "Loading…",
      loadError: "Data temporarily unavailable (direct query to Hyperliquid failed); please try again later.",
      fillsEmpty: "No recent fills.",
      authorizationsEmpty: "No authorization records found.",
      time: "Time",
      coin: "Coin",
      side: "Side",
      buy: "Buy",
      sell: "Sell",
      px: "Price",
      sz: "Size",
      fee: "Fee",
      closedPnl: "Realized PnL",
      action: "Action",
      summary: "Summary",
      actionApproveAgent: "Authorized API wallet",
      actionApproveBuilderFeeLabel: "Authorized builder fee",
      actionApproveBuilderFeeTo: "to",
      actionUnknown: "Authorization action",
      tx: "Tx",
      viewTx: "View",
      periods: { "7d": "7d", "30d": "30d", all: "All" },
      coinFilterLabel: "Filter by coin",
      coinFilterAll: "All coins",
      tzLocal: "Local time",
      tzUtc: "UTC",
      pagination: {
        showing: "Showing ",
        rangeSep: "–",
        ofTotal: " / ",
        prev: "Previous",
        next: "Next",
      },
    },
  },
  settings: {
    eyebrow: "Settings",
    title: "Account settings",
    subtitle: "Adjust risk limits, capital allocation and authorization; check which strategy you're following.",
    loadingNote: "Loading settings…",
    toast: {
      dismiss: "Dismiss",
      retrySignButton: "Sign again",
    },
    risk: {
      title: "Risk controls",
      subtitle:
        "No risk controls are enabled by default — the system only follows the leader's actions and won't "
        + "stop-loss or trip a breaker for you. Every threshold is yours to set: we mark our recommended "
        + "value next to it, but the final call is yours.",
      applyNote:
        "Every change asks you to sign a message in your wallet; once submitted, the engine applies it on "
        + "its next cycle (within about a minute).",
      trackingTitle: "Tracking precision",
      trackingSubtitle: "This one is independent of the risk-control switch — it applies whether or not risk controls are enabled.",
      enableLabel: "Enable Filet risk controls",
      enableHelp:
        "When on: once equity drawdown reaches your threshold, the system stops following (and closes "
        + "positions if you chose that). This limits the exposure of \"continuing to follow\" — by the time "
        + "it trips, that loss has usually already happened, so it does not shield your principal from loss. "
        + "When off, the system never intervenes and simply mirrors trades.",
      detailsTitle: "Risk control details",
      percentSuffix: "%",
      hoursSuffix: " hours",
      recommendedLabel: "Recommended",
      boolOn: "On",
      boolOff: "Off",
      saveButton: "Sign & save risk controls",
      saving: "Waiting for wallet signature…",
      saved: "Risk controls submitted.",
      loadError: "Risk controls could not be loaded right now. Please reload this page later.",
      signNote:
        "Next you'll be asked to sign a message in your wallet (no on-chain transaction, no gas). Filet will "
        + "never ask for your private key or seed phrase; signing happens only in your own wallet.",
      errors: {
        walletRejected: "You cancelled the signature in your wallet — risk controls were not changed.",
        signerMismatch:
          "The wallet that signed isn't the one you're logged in with, so nothing was submitted. Please "
          + "switch back to your logged-in wallet and try again.",
        contentMismatch:
          "The content returned by the server didn't match what you set on screen, so this was aborted — "
          + "nothing was signed. Please don't retry, and report this to us.",
        messageFailed: "Couldn't fetch the content to sign — risk controls were not changed. Please try again shortly.",
        submitFailed: "Submission failed — risk controls were not changed. The previous signature is now void; please try again.",
      },
      applied: {
        pending: "Submitted, not yet in effect (the engine applies it within about a minute).",
        inSync: "The currently effective settings match what you submitted.",
        unknown: "The currently effective settings can't be confirmed right now (engine status unavailable).",
        notSubmitted: "You haven't submitted risk controls yet; the values shown are system defaults.",
        sourceLabel: "Source",
        changedAtLabel: "Effective since",
        effectiveLabel: "Currently in effect",
        yourSettingLabel: "Your setting",
        unknownShort: "Unavailable",
        pendingBadge: "Pending next cycle (about a minute)",
      },
      halted: {
        title: "Your following has been stopped by risk controls",
        body:
          "A risk threshold was tripped and the engine has stopped following further. By the time it trips, "
          + "that loss has usually already happened — what's stopped is the exposure of continuing to follow.",
        reasonLabel: "Trigger reason",
        trippedAtLabel: "Tripped at",
        cooldownLabel: "Cooldown",
        resumeAtLabel: "Expected auto-resume",
        noAutoResume:
          "No scheduled auto-resume time (cooldown is set to 0, or it can't be computed right now) — use the "
          + "button below to resume.",
        unknownValue: "(unavailable)",
        resumeButton: "Resume following now",
        resuming: "Waiting for wallet signature…",
        resumed: "Resume request submitted; the engine will resume following on its next cycle.",
        residualNote:
          "Some positions or open orders were not closed/cancelled when this tripped and remain in the "
          + "market. After you resume, the engine will converge them toward the leader's targets on its next cycle.",
        resumeNote:
          "After resuming, the engine rebuilds positions from the leader on its next cycle. The equity "
          + "baseline was reset at the moment this tripped, so it won't immediately trip again from the drop "
          + "that preceded it.",
        leaderRevokedNote:
          "This stopped because the leader you were following was delisted by us, not because your risk "
          + "threshold was tripped. This kind can't be resumed by yourself — please choose another leader instead.",
        unknown: "Whether your risk controls have tripped can't be confirmed right now (engine status unavailable).",
      },
    },
    capital: {
      title: "Capital allocation",
      subtitle: "Your allocation ratio multiplies directly into your position sizes — please confirm you understand the impact before changing it.",
      scaleLabel: "Allocation ratio (of account equity)",
      effectiveLabel: "Currently in effect",
      yourSettingLabel: "Your setting",
      pendingLabel: "Submitted, pending next cycle (about a minute)",
      saveButton: "Sign & save capital allocation",
      saving: "Waiting for wallet signature…",
      saved: "Capital allocation submitted.",
      loadError: "Capital allocation could not be loaded right now. Please reload this page later.",
      signNote:
        "Next you'll be asked to sign a message in your wallet (no on-chain transaction, no gas). Filet will "
        + "never ask for your private key or seed phrase; signing happens only in your own wallet.",
      errors: {
        walletRejected: "You cancelled the signature in your wallet — capital allocation was not changed.",
        signerMismatch:
          "The wallet that signed isn't the one you're logged in with, so nothing was submitted. Please "
          + "switch back to your logged-in wallet and try again.",
        contentMismatch:
          "The content returned by the server didn't match what you set on screen, so this was aborted — "
          + "nothing was signed. Please don't retry, and report this to us.",
        messageFailed: "Couldn't fetch the content to sign — capital allocation was not changed. Please try again shortly.",
        submitFailed: "Submission failed — capital allocation was not changed. The previous signature is now void; please try again.",
      },
    },
    auth: {
      title: "Authorization",
      subtitle: "The agent information you've authorized Filet to use, plus the pause-following and close-all entry points.",
      agentAddressLabel: "Agent address",
      agentAddressMissing: "No agent generated yet",
      builderFeeLabel: "Builder fee cap approved",
      agentApprovedLabel: "Agent authorization status",
      approvedYes: "Approved",
      approvedNo: "Not yet approved",
      loadError: "Authorization info could not be loaded right now. Please reload this page later.",
      pauseHeading: "Following switch",
      pauseBtn: "Pause following",
      resumeBtn: "Resume following",
      closeAllBtn: "Close all & revoke authorization",
      pauseErrorNote: "Action failed, please try again later.",
      noEngineNote: "There is no engine currently running for this account, so there's nothing to act on.",
      closeAllPendingNote: "Submitted — the engine is winding down; check the Dashboard for progress.",
    },
    leader: {
      title: "Strategy you're currently following",
      loading: "Loading your current following status…",
      leaderLabel: "Currently following",
      failedTitle: "Your current following status can't be loaded right now",
      failedNote:
        "This failed query only affects what's shown in this section: it doesn't change your copy-trading "
        + "settings, and it doesn't mean you're not following. The rest of this page is unaffected — please "
        + "reload this page and try again.",
      noneTitles: {
        engine_default: "Following is enabled, but no leader is specified yet",
        not_activated: "This account hasn't enabled following yet",
        indeterminate: "Your following status can't be confirmed right now",
      },
      noneTitleFallback: "There's no following target to show right now",
      statusLabel: "Status code",
      pendingTitle: "There's a signed, not-yet-effective leader change",
      pendingLabel: "Switching to",
      pendingIssuedAtLabel: "Signed at",
      changeStrategyBtn: "Change strategy",
      advancedModeBtn: "Advanced mode",
    },
  },
  status: {
    heading: "System status",
    sub: "Current operating status of each service component, sourced from /api/public/status (no login required).",
    componentsHeading: "Components",
    empty: "No component status to display right now.",
    loadFailedNote: "Status could not be loaded or timed out; the values below are conservative (unknown) and do not imply system health.",
  },
  leaderboard: {
    heading: "Trader leaderboard",
    sub: "Sourced from Hyperliquid's official public leaderboard, ranked by PnL for the selected window. For research only — not an endorsement or copy-trading recommendation from this site.",
    windows: { day: "Day", week: "Week", month: "Month", allTime: "All time" },
    table: {
      rank: "Rank",
      trader: "Trader",
      accountValue: "Account value",
      pnl: "PnL",
      roi: "ROI",
      volume: "Volume",
    },
    loading: "Loading leaderboard…",
    error: "Failed to load the leaderboard. Please try again later.",
    empty: "No leaderboard data to display right now.",
  },
  explore: {
    heading: "Explore traders to copy",
    disclaimerBadge: "Filet does not endorse any address on this list",
    sub: "Data is sourced entirely from Hyperliquid's public on-chain records, updated every 10 minutes. Default sort is risk-adjusted return (return ÷ max drawdown), not absolute PnL — a large account's PnL can't be replicated by a small account.",
    updatedAtPrefix: "Data updated at ",
    windows: { d7: "7D", d30: "30D", d90: "90D", all: "All" },
    windowComingSoon: "Coming soon",
    filters: {
      sample: "Only show accounts meeting the sample threshold (≥ 60 trading days · ≥ 200 fills)",
      maxDd: "Max drawdown < 30%",
      concentrated: "Exclude single-coin concentration > 90%",
    },
    countPrefix: "",
    countMid: " accounts → ",
    countSuffix: " qualify",
    table: {
      rank: "#",
      account: "Account",
      sparkline: "30D equity",
      ret: "30D return",
      dd: "Max drawdown",
      days: "Live days",
      winRate: "Close win rate",
      exposure: "Current exposure",
      actions: "",
    },
    tags: { lowDrawdown: "Low drawdown", concentrated: "High concentration" },
    exposureDir: { long: "Long", short: "Short" },
    subSep: " · Account ",
    copyAddress: "Copy address",
    copied: "Copied",
    view: "View",
    follow: "Copy →",
    building: "Building the explore index, ready in a few minutes",
    errorPrefix: "Failed to load the explore index · ",
    empty: "No addresses match the current filters.",
    pagination: {
      showing: "Showing ",
      rangeSep: "–",
      ofTotal: " / ",
      perPagePrefix: " · ",
      perPageSuffix: " per page",
      prev: "Previous",
      next: "Next",
    },
  },
  traders: {
    breadcrumb: "Trader",
    loadingNote: "Loading trader data…",
    notFoundTitle: "Trader not found",
    notFoundBody: "No on-chain performance data for this address, or the address "
      + "format is invalid. Please go back to the leaderboard and pick another one.",
    backToList: "Back to leaderboard →",
    asOfPrefix: "Data as of ",
    sourceSuffix: " · Source: Hyperliquid API",
    accountValueLabel: "Current account value",
    disclaimerNote: "This address is sourced from Hyperliquid's public leaderboard, "
      + "not a curated Filet strategy — this platform does not endorse or take "
      + "responsibility for its performance.",
    panel: {
      heading: "Copy this address",
      cta: "Connect wallet and continue",
      ctaConnecting: "Connecting…",
      ctaSigning: "Please sign the login message in your wallet…",
      footnote: "The next step is a free signature only (no on-chain transaction, "
        + "no gas) — you'll see the full permissions and fee summary before "
        + "authorizing anything.",
      followBlocked: "This address is not available for copy-trading right now.",
    },
  },
};
