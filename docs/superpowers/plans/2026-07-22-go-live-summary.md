# Filet 主網 dogfood 上線 — 晨間準備狀態報告

日期：2026-07-22｜三路並行巡查彙整（指揮官親自覆核關鍵事實）
詳細來源：`2026-07-22-mainnet-readiness.md`（opus 就緒度）、`2026-07-22-open-items.md`（遺留項盤點）、健康稽核（內聯）

**一句話：明天還不能上線。核心引擎與防護都就緒、leader 適配、測試全綠;但有 4 個上線阻擋要先解,其中第 1 個最嚴重、是巡查才抓到的。**

---

## ⛔ 上線阻擋（mainnet dogfood 前必須解）

### 1. Follower 錢包 3662 正在跑你自己的 Momentum 實盤 —— 不能共用
**指揮官親自查證屬實**：`0xbAC652…3662` 主網 `extraAgents` 有一個 agent `name='Momentum'`（`0xfa48…d2f5`，有效到 2026-12-29），近 44 筆成交、最新 2026-07-21 14:26Z，交易 DOGE/HYPE/XRP。

把 Filet copytrade 指向同一顆錢包 → 兩引擎共用一個 perp 帳戶打架：
- Momentum 開的倉,在 Filet 眼裡是「leader 沒有的部位」→ Filet 會 **reduce-only 把 Momentum 的倉平掉**（`positions.py:268-279`）
- Momentum 的損益/換手率污染 Filet 的 kill switch 與成本熔斷器的權益基準

**修法**：跟 builder 一樣,follower 也要一顆**全新專屬錢包**,把 1000 dogfood 本金放進去（別用 Momentum 那顆）。

### 2. 伺服器是 testnet 設定 —— mainnet onboarding 要重配
後端依 `FILET_API_NETWORK` 決定 typed data 寫 Testnet/Mainnet。現在是 testnet,那頁簽出來的授權主網不認。
- 主網要:`FILET_API_NETWORK=mainnet` 的 onboarding（本機臨時實例即可,不動 testnet 伺服器）產 Mainnet typed data
- 實查:follower 主網**尚無 Filet agent、對新 builder 的 maxBuilderFee=0** → ApproveAgent + ApproveBuilderFee 兩筆都要在主網簽
- ApproveAgent 可在 HL 官方 app 做;ApproveBuilderFee 需我方工具（明天先架好驗過再讓你簽）

### 3. Follower env 必設 Telegram + 清狀態根殘留 + mainnet 白名單
**指揮官親自查證**：kill switch `trip()` 走 `notifier.critical`（killswitch.py:243）,**但** `run_copytrade.py:441` 是「有 `COPY_TG_BOT_TOKEN` 才建 Telegram,否則 NullNotifier」。
→ **follower env 不設 `COPY_TG_BOT_TOKEN`/`COPY_TG_CHAT_ID`,kill switch 觸發就靜默、沒人收到。主網必設。**
- 另:清 `/opt/filet/state/<新 account_id>` 確保無 testnet 殘留（避免回撤基準被污染或殘留 `killswitch.tripped` 卡死）——用新錢包就是新 account_id,天然乾淨
- 建 mainnet `leaders.json`（含此 leader）,否則引擎拒啟動

### 4. Builder 餘額太薄
新 builder `0x81E9…1183` 主網 perp 只有 110,只比 100 門檻高 10。**提領或波動跌破 100 即靜默停止累計 fee**。建議加厚到 200+（雖然有日報 Telegram 監控接著,但緩衝越大越安全）。

---

## 🟡 上線後短期要補（營運重要,非阻擋）

- **BotFather 重產 telegram token**（今晚的已入對話歷史）→ 換進伺服器 `/etc/filet/telegram.env`
- **builder fee 門檻「主 dex vs 跨 dex」向 HL 確認** —— 監控目前只看主 dex（保守正確,但官方語意未 100% 確認）
- **成本熔斷器門檻真實校準** —— 目前 `cost_max_turnover_24h=20` 是 n=1 樣本、未校準。leader 實測換手率 0.32×/日,離 20× 很遠,短期不會誤觸,但主網跑一陣子後應以真實資料重訂
- **perf-series 時間序列採集** —— 無法回填,越早在主網開始越好（但需 mainnet follower 存在）
- **builder fee 入帳對帳** —— builderRewards 要手動 claim（不自動進 spot),定期查一下有沒有正常累計

## 🔵 收客戶前才需要（dogfood 不受限）

- **律師/法遵 gate**（`docs/legal/2026-07-19-法遵諮詢文件.md` 46 題已備妥,未取得意見）—— 自有錢包 dogfood 不受此限;**收客戶前必過**
- billing I2/I3 缺口、多 leader/slider 的訂閱 gate、`day=` 對齊 —— 都是客戶功能,dogfood 用不到

## ✅ 已就緒（好消息,皆有實據）

- **測試全綠**（指揮官親跑）：1755 Python / 344 前端,ruff clean;抽查 4 條碰錢/碰安全不變量變異全被咬,**無裸露不變量**
- **部署健康**：伺服器 f4a2905 與本地 HEAD 一致、四服務 active、三 timer 在跑、TLS 87 天、**follower 停著（合規）**、無 failed unit
- **leader 適配**：`0xf97ad6…ddd1` 主網權益 17.7 萬、換手率約 0.32×/日（遠低於熔斷 20×）、crypto-only 鏡像正確（xyz 美股自動 skip）。⚠️ 上線後會先空手,直到 leader 開下一個 crypto 倉——**正常,不是壞掉**
- **非託管不變量主網一樣成立**：agent trade-only、adapter 無 withdraw/transfer
- **今晚 testnet 端到端全鏈路驗證**：activate 三閘、心跳實寫、開/加/平 0.40× 鏡像、reduce-only、builder fee 0.02%、有部位劃轉不污染損益序列、引擎重啟接續、簽章換 leader 鏈路

## 更正一則過期資訊
遺留項 agent 列「TLS/防火牆未解決」——**那是過期的**。今晚已開雲防火牆 80/443 並跑 certbot,Let's Encrypt 真憑證生效（指揮官外部查證 HTTPS 200、憑證鏈有效,到期 10/17）。已不是阻擋項。

---

## 明天的順序建議

1. **你**：開一顆全新 follower 錢包 → 放 1000 → 確認在主 dex perp（別進 xyz）
2. **你**：builder `0x81E9` 加厚到 200+
3. **我**：架 mainnet onboarding 工具、驗過 typed data 真的是 Mainnet
4. **你簽**：ApproveAgent（HL app 可做）+ ApproveBuilderFee（我方工具）
5. **我**：產 mainnet agent key、建 follower env（含 COPY_TG!）、mainnet 白名單、啟動、一起盯第一輪（可能先空手）

**最重要的收穫：在你把 Filet 指向 Momentum 錢包之前攔下來了。** 那會讓 Filet 去平掉你 Momentum 的實盤倉位。
