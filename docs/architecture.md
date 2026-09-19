# Filet 系統結構說明

寫給第一次接觸這個 repo 的人：把「誰是誰、跑在哪、誰能碰什麼」講清楚。程式碼細節看各檔檔頭；這份只講形狀。最後更新 2026-09-19。

---

## 1. 角色（產品層）

| 角色 | 是誰 | 在系統裡的樣子 |
|---|---|---|
| **Leader** | 被跟單的交易員 | 一個 Hyperliquid 錢包地址，列在 `var/filet/leaders.json`（精選白名單）或 `user_leaders.json`（用戶自加）。我們不持有 leader 的任何鑰匙，只讀他在鏈上的公開部位。 |
| **Follower** | 跟單的人，也就是我們的用戶 | 一個錢包地址 → 推導出 `account_id`（`f` + 地址去 0x），→ 一顆專屬引擎進程 `filet-follower@<account_id>`。 |
| **Builder** | 我們自己（Filet） | 一個 builder 錢包地址。用戶授權我們在他每筆單上收 builder fee；引擎下的每一張單都帶這個 builder 參數。 |
| **Agent** | 用戶授權給我們的下單代理鑰 | 用戶用主錢包簽 `approveAgent` 產生。Agent 只能下單，**不能提領或轉帳**（Hyperliquid 結構限制，也是本專案的非託管不變量）。Agent 私鑰只存在引擎那一側。 |

一句話：用戶（follower）授權一把 agent 鑰給我們，我們替他跑一顆引擎去跟某位 leader，引擎每張單順手收 builder fee。

---

## 2. 進程（系統層，正式機全是 systemd unit）

```
              瀏覽器
                │
        ┌───────▼────────┐   Next.js 前端（唯讀 UI，簽章在瀏覽器錢包裡完成）
        │ filet-dashboard│
        └───────┬────────┘
                │ HTTP
        ┌───────▼────────┐   FastAPI。對外、權限最低。
        │   filet-api    │   能做的事：發待簽原文、驗章、寫「記錄檔」與 pending 清單。
        └───┬───────┬────┘   不能做的事：啟動引擎、碰任何私鑰、下單。
            │       │ unix socket
            │   ┌───▼──────────┐  金鑰服務。替 API 產生 agent key 並落到 /etc/filet/keys，
            │   │ filet-keysvc │  API 只拿得到 agent 的**地址**，拿不到私鑰。
            │   └──────────────┘
            │ 寫 pending.json
   ┌────────▼─────────────┐  「watcher」。systemd timer 每分鐘跑一次。
   │ filet-auto-activate  │  掃 pending → 驗章 → 用 template 產生該用戶的 env
   └────────┬─────────────┘  → systemctl start filet-follower@<id>。
            │
   ┌────────▼─────────────────┐  一位 follower 一顆。持有他的 agent 私鑰（從 /etc/filet/keys 讀）。
   │ filet-follower@<account> │  每輪：讀 leader 部位 → 讀用戶簽章的各種設定 → 算目標部位 → 下單。
   └──────────────────────────┘  互相獨立，一顆掛掉不影響其他人。
```

其他 timer：`filet-daily-report`（日報）、`filet-leaderboard`（榜單快照）、`filet-perf-series`（績效序列）。

**為什麼切成這麼多進程**：對外的 filet-api 是最可能被打穿的一層，所以它只能寫「清單」和「記錄」，不能直接做任何有真錢後果的事。真正動錢的引擎，只相信兩樣東西：鏈上狀態，以及**用戶自己錢包簽出來的記錄**。

---

## 3. 檔案（誰寫、誰讀）

| 檔案 | 寫的人 | 讀的人 | 內容 |
|---|---|---|---|
| `/var/lib/filet-api/pending.json` | filet-api | watcher | 完成 onboarding、等待啟用的用戶清單 |
| `var/filet/followers.json`（manifest） | watcher（啟用時追加） | 引擎、日報、API 唯讀 | 已啟用 follower 的登錄簿：account_id、地址、網路。**引擎驗章時的可信來源**：簽章者必須等於這裡登錄的地址 |
| `var/filet/leaders.json` | 人工／撤銷腳本 | API、引擎每輪 | 精選 leader 白名單 |
| `/var/lib/filet-exchange/*.json`（交換目錄） | filet-api | 引擎、watcher | 用戶簽章的記錄檔：`leader_changes.json`（換 leader）、`capital_settings.json`（資金）、`risk_settings.json`／`risk_unlock.json`（風控）、`user_leaders.json`。API 只能寫，引擎只能讀 |
| `/etc/filet/follower.env.template` | 部署者 | watcher | 每顆引擎 env 的底稿（共用參數） |
| `/etc/filet/followers/<id>.env` | watcher | 該顆引擎 | 從 template 複製並填入該用戶專屬值（網路、帳號、地址、風控參數） |
| `/etc/filet/keys/` | keysvc | 引擎 | agent 私鑰 |
| `/opt/filet/state/<id>/` | 該顆引擎 | 引擎、API 唯讀 | 引擎自己的狀態：kill switch ARM 檔、已套用設定的護欄、心跳 |

---

## 4. 簽章管線（所有「用戶改設定」都長這樣）

換 leader、資金設定、風控設定、解除熔斷、平倉並撤銷、以及本次的推薦碼 opt-in，全部同一個形狀：

1. 前端向 filet-api 要一份**待簽原文**（伺服器組字串，附一次性 nonce）。
2. 前端在進錢包**之前**先比對原文內容是不是用戶要的那件事（防被打穿的 API 換內容）。
3. 用戶錢包 `personal_sign` 原文。
4. 前端本地 recover 簽章者，不是本人就不送。
5. filet-api 重建原文、驗章、消耗 nonce，通過才把記錄寫進交換目錄。
6. 引擎（或 watcher）在**套用前自己再驗一次章**，簽章者必須等於 manifest 登錄的地址。

每種記錄的原文第一行是一個固定字面量（例如 `Filet: update copy-trading risk settings`），彼此不同，所以一種授權不可能被拿去當另一種用。

原則：能改一顆引擎行為的條件，是「握有那顆錢包的私鑰」，不是「寫得到某個檔」。

---

## 5. 一位用戶的一生

1. **Onboarding（網站四步）**：選策略 → 連錢包、簽 `approveAgent` 與 `approveBuilderFee`（EIP-712，主錢包直送 Hyperliquid）→ 風控設定簽章 → 費用確認。完成後 API 檢查全過，寫 pending。
2. **啟用（watcher，一分鐘內）**：驗 leader 選擇簽章、讀風控簽章記錄 → 產生 env → 啟動引擎。
3. **跟單（引擎，每輪約一分鐘）**：解析 leader → 疊上資金／風控簽章設定 → 查暫停旗標 → 算目標部位 → 下單（帶 builder 參數）。
4. **調整**：用戶在設定頁簽新記錄，引擎下一輪自己驗章套用，不重啟、不經人工。
5. **結束**：用戶簽「平倉並撤銷」→ 引擎平倉、發告警、退出；或管理端跑 `scripts/revoke_leader.py` 撤銷 leader → 所有跟他的引擎受控收尾。

---

## 6. 推薦碼 opt-in 放在哪（2026-09-19 設計）

- 用戶簽的是第五種記錄 `referral_optin.json`（原文第一行 `Filet: set Hyperliquid referral code`）。
- 送 `setReferrer` 的是**引擎**，因為只有引擎持有 agent 私鑰；每輪在下單前做一次冪等檢查，鏈上還沒設才送。
- 推薦碼單一來源 `/etc/filet/referral.env`（`FILET_REFERRAL_CODE`），filet-api 與 watcher 兩個 unit 都以 `EnvironmentFile` 載入；watcher 啟用新用戶時把它寫成該引擎的 `COPY_REFERRAL_CODE`。引擎要求「用戶簽的碼」等於「自己 env 的碼」才動作。
- 既有已啟用的引擎 env 沒有這個變數，行為不變。

---

## 7. 常用查詢

```bash
# 正式機上
systemctl --failed
systemctl list-units 'filet-*'
sudo journalctl -u filet-auto-activate -n 50
sudo journalctl -u 'filet-follower@*' -f        # 注意：logger.info 不進 journal，只有 WARNING 以上

# 某地址的鏈上推薦狀態（公開端點，本機可跑）
curl -s https://api.hyperliquid.xyz/info -H 'Content-Type: application/json' \
  -d '{"type":"referral","user":"0x..."}' | jq '.referredBy, .cumVlm'
```

相關文件：`deploy/RUNBOOK.md`（部署與操作）、`CLAUDE.md`（紅線）、各簽章模組檔頭（`src/spark/filet/*_settings.py`）。
