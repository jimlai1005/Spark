/**
 * content/legal.ts — 法務長文 content module（/terms /privacy /risk 三頁，Task 12）。
 *
 * ⭐ 內容**逐字**轉自 `docs/superpowers/specs/2026-08-28-legal-copy-zh.md`（繁中權威
 * 版），不得改寫語義；唯一的結構性加工是：(1) 去除來源 markdown 的粗體標記
 * （`**...**`）與純排版換行——這些是格式語法，不是內容；(2) 把來源中以「- 」條列
 * 的項目拆成 `paragraphs` 陣列的個別元素，讓 `{heading, paragraphs[]}` 的結構與來源的
 * 條列語意對齊；(3) `/risk` 頁首的單句提示（來源中在「### 1.」之前、不屬於任何小節）
 * 併入第 1 節 paragraphs 的第一個元素——三份文件因此共用同一個 schema，不需要另開
 * 一個「頁首前言」欄位。內容本身逐字不變。
 *
 * `effectiveDate` 依使用者裁決（2026-08-28）直接填入上線日期，不加任何審閱前置標注
 * （見 plan §0.1 裁決 4）；英文版由本次實作對照翻譯（語義等值、法律語氣、不軟化免責與風險語句），
 * 專有名詞（Hyperliquid、builder fee、agent）保留英文。
 *
 * zh/en 結構對稱由型別強制（沿用 `lib/copy.ts` 的 `DeepString<T>` 模式）＋
 * `content/legal.test.ts` 的遞迴 key 比對雙重把關。
 */
import type { DeepString } from "@/lib/copy";

export interface LegalSection {
  readonly heading: string;
  readonly paragraphs: readonly string[];
}

export interface LegalDoc {
  readonly title: string;
  readonly effectiveDateLabel: string;
  readonly effectiveDate: string;
  readonly sections: readonly LegalSection[];
}

export const LEGAL_ZH = {
  terms: {
    title: "服務條款",
    effectiveDateLabel: "生效日期",
    effectiveDate: "2026-08-28",
    sections: [
      {
        heading: "1. 服務性質",
        paragraphs: [
          "Filet（下稱「本服務」）是一套非保管（non-custodial）的策略跟單軟體服務。"
            + "本服務依你的授權，在你自己的 Hyperliquid 帳戶內執行跟單下單指令。"
            + "本服務不是交易所、不是經紀商、不是基金、不是投資顧問；我們不接受你的資產、"
            + "不代你保管任何資金或私鑰。",
        ],
      },
      {
        heading: "2. 非保管與授權邊界",
        paragraphs: [
          "你透過錢包簽署建立一組僅能下單的 agent 授權。在此授權下，本服務：",
          "可以：依策略訊號在你的帳戶下單、加倉、平倉；使用你設定範圍內的保證金；"
            + "在你簽署的槓桿上限內調整倉位；依你簽署的費率上限收取 builder fee。",
          "不能：提領你的資金到任何地址；轉帳或在錢包之間移動資產；"
            + "取得你的私鑰或助記詞（我們永遠不會索取）；超出你設定的投入比例與槓桿上限。",
          "授權為你單方可撤銷：你可以隨時暫停跟單或撤銷 agent 授權，撤銷不需要本服務同意，"
            + "也不需要本服務在線。",
        ],
      },
      {
        heading: "3. 費用",
        paragraphs: [
          "本服務就每筆經由本服務路由的成交，依 Hyperliquid builder code 機制收取 "
            + "builder fee，費率以你在授權時簽署的上限為準（現行為每筆成交 0.02%）。"
            + "我們無法單方調高此上限；調整上限需要你重新簽署。Hyperliquid 本身的交易"
            + "手續費與資金費率由 Hyperliquid 收取，與本服務無關。除 builder fee 外，"
            + "本服務不收取月費、分潤或提領費。",
        ],
      },
      {
        heading: "4. 你的責任",
        paragraphs: [
          "妥善保管你的錢包、私鑰與助記詞。任何以你的錢包完成的簽署視為你本人的行為。",
          "自行評估並承擔交易風險。本服務提供的一切資訊（含策略績效）不構成投資建議。",
          "自行確認使用本服務在你所在司法管轄區的合法性，並自行處理相關稅務申報義務。",
          "不得以本服務從事市場操縱、對敲或其他違法行為。",
        ],
      },
      {
        heading: "5. 服務可用性與執行品質",
        paragraphs: [
          "本服務按「現狀」提供。我們不保證服務不中斷、無錯誤，亦不保證跟單訊號的即時性"
            + "與完整性。網路延遲、交易所端故障、保證金不足、市場劇烈波動等因素都可能導致"
            + "訊號延遲、遺漏或成交價格偏離，相關偏差以 Dashboard 揭露為準。風控工具"
            + "（如最大回撤自動停止）的觸發與執行同樣受上述因素影響，不構成損失上限的保證。",
        ],
      },
      {
        heading: "6. 服務變更與終止",
        paragraphs: [
          "我們可能隨時調整、暫停或終止本服務之全部或一部。因本服務為非保管架構，"
            + "服務終止不影響你對自己資產的控制權；你可隨時自行撤銷授權並管理你的部位。"
            + "我們對條款的修改將於本頁公告，修改後繼續使用本服務視為同意修改後之條款。",
        ],
      },
      {
        heading: "7. 責任限制",
        paragraphs: [
          "在法律允許的最大範圍內，本服務及其運營者對於任何交易損失、利潤損失、"
            + "資料滅失或其他間接、衍生性損害不負賠償責任。無論如何，本服務就任何請求所負"
            + "之全部責任，以你於請求發生前三個月內支付予本服務之 builder fee 總額為上限。",
        ],
      },
      {
        heading: "8. 準據法",
        paragraphs: [
          "本條款以中華民國（臺灣）法律為準據法。因本條款所生之爭議，雙方同意以臺灣"
            + "臺北地方法院為第一審管轄法院。",
        ],
      },
      {
        heading: "9. 聯絡方式",
        paragraphs: ["contact@filet.trade"],
      },
    ],
  },
  privacy: {
    title: "隱私政策",
    effectiveDateLabel: "生效日期",
    effectiveDate: "2026-08-28",
    sections: [
      {
        heading: "1. 我們收集什麼",
        paragraphs: [
          "錢包地址與簽章記錄：你連接錢包並簽署授權、風控設定等訊息時，我們記錄你的"
            + "錢包地址與相應簽章，作為授權與設定的依據。",
          "鏈上公開資料：你的 Hyperliquid 帳戶淨值、持倉、成交記錄等。這些資料本即"
            + "公開於鏈上，我們讀取它們以執行跟單與顯示 Dashboard。",
          "技術資料：登入 session（cookie）、瀏覽器 localStorage 中的流程狀態與偏好設定"
            + "（語言、onboarding 進度）。",
          "你主動提供的資料：例如透過 email 與我們聯絡時的內容。",
          "我們不收集：私鑰、助記詞（永遠不會索取）；姓名、身分證件等身分資料"
            + "（本服務無 KYC）；與服務無關的瀏覽行為追蹤。",
        ],
      },
      {
        heading: "2. 我們如何使用",
        paragraphs: [
          "僅用於：執行與維護跟單服務、計算與揭露費用、風控設定之驗證與稽核、服務安全與"
            + "除錯、依法配合主管機關要求。我們不出售你的資料，不用於廣告。",
        ],
      },
      {
        heading: "3. 第三方",
        paragraphs: [
          "服務運作涉及以下第三方，其各自的條款與隱私政策適用：Hyperliquid（交易執行與"
            + "鏈上資料）、雲端主機服務商（伺服器）。除此之外我們不與第三方分享你的資料，"
            + "法律要求者除外。",
        ],
      },
      {
        heading: "4. 保存與刪除",
        paragraphs: [
          "簽章記錄與授權歷程為服務稽核所需，於你終止使用後保存至多五年。技術性 log "
            + "定期輪替刪除。你可來信要求刪除非稽核必要之資料。",
        ],
      },
      {
        heading: "5. 你的權利",
        paragraphs: [
          "你可隨時：撤銷授權（於 Dashboard 或錢包端操作）、清除瀏覽器端資料（登出並清除 "
            + "localStorage）、來信查詢或要求刪除我們持有的你的資料。",
        ],
      },
      {
        heading: "6. 聯絡方式",
        paragraphs: ["contact@filet.trade"],
      },
    ],
  },
  risk: {
    title: "風險揭露",
    effectiveDateLabel: "生效日期",
    effectiveDate: "2026-08-28",
    sections: [
      {
        heading: "1. 你可能損失全部投入資金",
        paragraphs: [
          "在使用 Filet 之前，請完整閱讀並確認你理解以下風險。",
          "永續合約為高風險槓桿商品。跟單交易會在你的帳戶執行真實交易，虧損由你全額"
            + "承擔。請只投入你能承受完全損失的資金。",
        ],
      },
      {
        heading: "2. 過往績效不代表未來結果",
        paragraphs: [
          "本站顯示的策略績效為歷史實盤記錄。歷史報酬、回撤與勝率不保證未來重現。"
            + "尤其請注意樣本期間：實盤天數較短的策略，其統計指標（如 Sharpe）帶寬較寬、"
            + "參考價值較低——我們在策略頁揭露各指標的樣本數與標準誤，請一併閱讀。",
        ],
      },
      {
        heading: "3. 槓桿與清算風險",
        paragraphs: [
          "槓桿放大損益。市場劇烈波動時，你的部位可能被交易所強制平倉（清算），損失"
            + "可能超過預期。你設定的槓桿上限只約束本服務的下單行為，不改變交易所的"
            + "清算規則。",
        ],
      },
      {
        heading: "4. 跟單執行風險",
        paragraphs: [
          "你的成交與策略帳戶（leader）的成交之間必然存在差異：訊號延遲、成交價差、"
            + "因保證金不足而遺漏的訊號、部位比例偏差。這些差異在多數時候很小，但在市場"
            + "劇烈波動時可能放大，導致你的績效顯著偏離 leader 的績效。實際偏差數據於 "
            + "Dashboard 持續揭露。",
        ],
      },
      {
        heading: "5. 風控工具的極限",
        paragraphs: [
          "最大回撤自動停止等風控功能依賴系統正常運作與市場流動性。在極端行情、"
            + "交易所故障或網路中斷時，自動平倉的實際成交價可能大幅劣於觸發價，實際損失"
            + "可能超過你設定的回撤比例。風控工具是降低風險的工具，不是損失上限的保證。",
        ],
      },
      {
        heading: "6. 平台與技術風險",
        paragraphs: [
          "本服務依賴 Hyperliquid 的正常運作。交易所端的故障、規則變更、預言機異常等均"
            + "可能影響你的部位與資產。此外，智能合約、錢包軟體與本服務自身的軟體都可能"
            + "存在缺陷。",
        ],
      },
      {
        heading: "7. 進階模式的額外風險",
        paragraphs: [
          "進階模式允許你跟單任意 Hyperliquid 地址。Filet 對該地址的策略品質、風控與"
            + "存續不做任何背書：該地址可能隨時改變策略、承受巨額回撤或停止交易。使用"
            + "進階模式前需另行確認你理解此風險。",
        ],
      },
      {
        heading: "8. 非投資建議",
        paragraphs: [
          "本站一切內容（含績效數據、策略說明、費用試算）僅為資訊揭露，不構成投資建議、"
            + "要約或招攬。是否跟單、投入多少、設定何種風險限制，均為你的獨立決策。",
        ],
      },
    ],
  },
} as const;

export type LegalDocKey = keyof typeof LEGAL_ZH;

export const LEGAL_EN: DeepString<typeof LEGAL_ZH> = {
  terms: {
    title: "Terms of Service",
    effectiveDateLabel: "Effective date",
    effectiveDate: "2026-08-28",
    sections: [
      {
        heading: "1. Nature of the Service",
        paragraphs: [
          "Filet (\"the Service\") is a non-custodial strategy copy-trading software "
            + "service. Acting on your authorization, the Service places copy-trade orders "
            + "within your own Hyperliquid account. The Service is not an exchange, not a "
            + "broker, not a fund, and not an investment adviser; we do not accept your "
            + "assets and do not custody any funds or private keys on your behalf.",
        ],
      },
      {
        heading: "2. Non-Custody and Authorization Scope",
        paragraphs: [
          "You create a trade-only agent authorization by signing with your wallet. "
            + "Under this authorization, the Service:",
          "Can: place orders, increase, and close positions in your account according to "
            + "strategy signals; use margin within the range you have configured; adjust "
            + "positions within the leverage cap you have signed; and charge builder fees "
            + "up to the rate cap you have signed.",
          "Cannot: withdraw your funds to any address; transfer or move assets between "
            + "wallets; obtain your private key or seed phrase (we will never ask for "
            + "them); or exceed the capital allocation or leverage cap you have set.",
          "The authorization is unilaterally revocable by you: you may pause copy trading "
            + "or revoke the agent authorization at any time, without needing the "
            + "Service's consent or the Service being online.",
        ],
      },
      {
        heading: "3. Fees",
        paragraphs: [
          "For each fill routed through the Service, we charge a builder fee under the "
            + "Hyperliquid builder code mechanism, at a rate up to the cap you signed at "
            + "authorization (currently 0.02% per fill). We cannot unilaterally raise this "
            + "cap; raising it requires you to sign again. Hyperliquid's own trading fees "
            + "and funding rates are charged by Hyperliquid and are unrelated to the "
            + "Service. Other than the builder fee, the Service charges no monthly fee, "
            + "profit share, or withdrawal fee.",
        ],
      },
      {
        heading: "4. Your Responsibilities",
        paragraphs: [
          "Safeguard your wallet, private key, and seed phrase. Any signature completed "
            + "with your wallet is deemed your own act.",
          "Independently assess and bear trading risk. All information provided by the "
            + "Service (including strategy performance) does not constitute investment "
            + "advice.",
          "Confirm the legality of using the Service in your own jurisdiction, and handle "
            + "any related tax filing obligations yourself.",
          "Do not use the Service to engage in market manipulation, wash trading, or "
            + "other unlawful conduct.",
        ],
      },
      {
        heading: "5. Availability and Execution Quality",
        paragraphs: [
          "The Service is provided \"as is.\" We do not guarantee that the Service will "
            + "be uninterrupted or error-free, nor do we guarantee the timeliness or "
            + "completeness of copy-trade signals. Network latency, exchange-side outages, "
            + "insufficient margin, and extreme market volatility may all cause delayed, "
            + "missed, or price-deviated signals; any such deviations are disclosed on the "
            + "Dashboard. Risk-control tools (such as maximum-drawdown auto-stop) are "
            + "likewise affected by the factors above, and their triggering and execution "
            + "do not constitute a guaranteed cap on losses.",
        ],
      },
      {
        heading: "6. Changes and Termination of Service",
        paragraphs: [
          "We may adjust, suspend, or terminate all or part of the Service at any time. "
            + "Because the Service is non-custodial, termination of the Service does not "
            + "affect your control over your own assets; you may revoke your authorization "
            + "and manage your positions at any time. We will announce any amendments to "
            + "these Terms on this page; continued use of the Service after such "
            + "amendments constitutes acceptance of the amended Terms.",
        ],
      },
      {
        heading: "7. Limitation of Liability",
        paragraphs: [
          "To the fullest extent permitted by law, the Service and its operators are not "
            + "liable for any trading losses, lost profits, data loss, or other indirect "
            + "or consequential damages. In any event, the Service's total liability for "
            + "any claim is capped at the total builder fees you paid to the Service in "
            + "the three months preceding the claim.",
        ],
      },
      {
        heading: "8. Governing Law",
        paragraphs: [
          "These Terms are governed by the laws of the Republic of China (Taiwan). Any "
            + "dispute arising from these Terms shall be submitted to the Taiwan Taipei "
            + "District Court as the court of first instance, as agreed by both parties.",
        ],
      },
      {
        heading: "9. Contact",
        paragraphs: ["contact@filet.trade"],
      },
    ],
  },
  privacy: {
    title: "Privacy Policy",
    effectiveDateLabel: "Effective date",
    effectiveDate: "2026-08-28",
    sections: [
      {
        heading: "1. What We Collect",
        paragraphs: [
          "Wallet address and signature records: when you connect your wallet and sign "
            + "authorization or risk-control settings messages, we record your wallet "
            + "address and the corresponding signature as the basis for the authorization "
            + "and settings.",
          "On-chain public data: your Hyperliquid account equity, positions, and trade "
            + "history. This data is already public on-chain; we read it to execute copy "
            + "trading and display the Dashboard.",
          "Technical data: your login session (cookie), and flow state and preferences "
            + "(language, onboarding progress) stored in your browser's localStorage.",
          "Data you voluntarily provide: for example, the content of messages when you "
            + "contact us by email.",
          "We do not collect: private keys or seed phrases (we will never ask for them); "
            + "identity documents such as your name or ID (the Service has no KYC); or "
            + "browsing-behavior tracking unrelated to the Service.",
        ],
      },
      {
        heading: "2. How We Use It",
        paragraphs: [
          "Used solely for: operating and maintaining the copy-trading service, "
            + "calculating and disclosing fees, verifying and auditing risk-control "
            + "settings, service security and debugging, and complying with lawful "
            + "requests from regulators. We do not sell your data and do not use it for "
            + "advertising.",
        ],
      },
      {
        heading: "3. Third Parties",
        paragraphs: [
          "Operation of the Service involves the following third parties, whose "
            + "respective terms and privacy policies apply: Hyperliquid (trade execution "
            + "and on-chain data) and cloud hosting providers (servers). Beyond this, we "
            + "do not share your data with third parties, except as required by law.",
        ],
      },
      {
        heading: "4. Retention and Deletion",
        paragraphs: [
          "Signature records and authorization history are retained for up to five years "
            + "after you stop using the Service, as required for service auditing. "
            + "Technical logs are periodically rotated and deleted. You may email us to "
            + "request deletion of data not required for auditing.",
        ],
      },
      {
        heading: "5. Your Rights",
        paragraphs: [
          "You may at any time: revoke your authorization (via the Dashboard or your "
            + "wallet), clear your browser-side data (log out and clear localStorage), or "
            + "email us to inquire about or request deletion of data we hold about you.",
        ],
      },
      {
        heading: "6. Contact",
        paragraphs: ["contact@filet.trade"],
      },
    ],
  },
  risk: {
    title: "Risk Disclosure",
    effectiveDateLabel: "Effective date",
    effectiveDate: "2026-08-28",
    sections: [
      {
        heading: "1. You May Lose Your Entire Investment",
        paragraphs: [
          "Please read and confirm you understand the following risks in full before "
            + "using Filet.",
          "Perpetual futures are high-risk, leveraged instruments. Copy trading executes "
            + "real trades in your account, and any losses are borne entirely by you. "
            + "Only allocate funds you can afford to lose completely.",
        ],
      },
      {
        heading: "2. Past Performance Does Not Represent Future Results",
        paragraphs: [
          "The strategy performance shown on this site is a historical live-trading "
            + "record. Historical returns, drawdowns, and win rates do not guarantee "
            + "future recurrence. Pay particular attention to the sample period: "
            + "strategies with a shorter live track record have wider confidence bands on "
            + "their statistics (e.g. Sharpe) and lower reference value — we disclose the "
            + "sample size and standard error for each metric on the strategy page; "
            + "please review them together.",
        ],
      },
      {
        heading: "3. Leverage and Liquidation Risk",
        paragraphs: [
          "Leverage amplifies gains and losses. During extreme market volatility, your "
            + "position may be force-liquidated by the exchange, and losses may exceed "
            + "expectations. The leverage cap you set only constrains the Service's own "
            + "order-placement behavior; it does not change the exchange's liquidation "
            + "rules.",
        ],
      },
      {
        heading: "4. Copy-Trade Execution Risk",
        paragraphs: [
          "There will always be differences between your fills and the strategy "
            + "account's (leader's) fills: signal latency, execution price slippage, "
            + "signals missed due to insufficient margin, and position-size deviation. "
            + "These differences are usually small, but may widen during extreme market "
            + "volatility, causing your performance to diverge materially from the "
            + "leader's performance. Actual deviation data is continuously disclosed on "
            + "the Dashboard.",
        ],
      },
      {
        heading: "5. Limits of Risk-Control Tools",
        paragraphs: [
          "Risk-control features such as maximum-drawdown auto-stop depend on the system "
            + "operating normally and on market liquidity. During extreme market "
            + "conditions, exchange outages, or network interruptions, the actual "
            + "execution price of an automatic close-out may be substantially worse than "
            + "the trigger price, and actual losses may exceed the drawdown percentage you "
            + "configured. Risk-control tools reduce risk; they are not a guaranteed cap "
            + "on losses.",
        ],
      },
      {
        heading: "6. Platform and Technical Risk",
        paragraphs: [
          "The Service depends on Hyperliquid operating normally. Outages, rule changes, "
            + "oracle anomalies, or other issues on the exchange side may all affect your "
            + "positions and assets. In addition, smart contracts, wallet software, and "
            + "the Service's own software may all contain defects.",
        ],
      },
      {
        heading: "7. Additional Risk of Advanced Mode",
        paragraphs: [
          "Advanced mode lets you copy-trade any Hyperliquid address. Filet makes no "
            + "endorsement whatsoever of that address's strategy quality, risk control, "
            + "or continued operation: the address may change its strategy, suffer a "
            + "large drawdown, or stop trading at any time. Before using Advanced mode "
            + "you must separately confirm that you understand this risk.",
        ],
      },
      {
        heading: "8. Not Investment Advice",
        paragraphs: [
          "All content on this site (including performance data, strategy descriptions, "
            + "and fee calculations) is for informational disclosure only and does not "
            + "constitute investment advice, an offer, or a solicitation. Whether to copy "
            + "trade, how much to allocate, and what risk limits to set are entirely your "
            + "own independent decisions.",
        ],
      },
    ],
  },
};
