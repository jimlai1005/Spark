# Filet M2 部署 Runbook — 裸 Ubuntu 22.04 到全服務上線

> 目標實例（已建議、尚未開）：AWS Lightsail，2GB RAM / 2vCPU / Tokyo / Ubuntu 22.04。
> 本文件假設從乾淨系統開始，逐步指令執行到位。**部署日零設計決策**——所有需要人工判斷
> 或人工填值的地方，本文件明列指令與佔位符，不留「到時候再想」的空白。
>
> 範圍：key-service + Public API + 引擎多實例（後端）＋ dashboard（前端，Next.js）＋
> nginx 反代 + TLS。**不含**：律師背書（M0 gate，非工程項）、Stripe/M3 範疇。
>
> 參考：
> - `docs/superpowers/specs/2026-07-17-m2-onboarding-dashboard-design.md`（三層隔離設計、不變量）
> - `docs/superpowers/plans/2026-07-17-m2-publicapi.md`（移交部署清單、實作細節、opus 審查結論）
> - `deploy/filet-keysvc.service`、`deploy/filet-api.service`、`deploy/filet-follower@.service`、
>   `deploy/filet-dashboard.service`、`deploy/nginx-filet.conf`（本次新增，與本文件配套）

## 0. 佔位符總表

執行前先決定以下值（部分只有拿到 Lightsail 實例與網域後才能定案——見附錄 A「部署日需使用者決策」）：

| 佔位符 | 說明 | 填入方式 |
|---|---|---|
| `FILET_DOMAIN_PLACEHOLDER` | 對外網域（例如 `filet.example.com`），DNS A 記錄需先指到 Lightsail 靜態 IP | 使用者決定，見附錄 A |
| `FILET_LIGHTSAIL_IP_PLACEHOLDER` | Lightsail 靜態 IP | 開實例後取得 |
| `FILET_BUILDER_ADDR_PLACEHOLDER` | 我方 builder 錢包地址（`FILET_BUILDER_ADDR`／`filet-api.service` 的 `FILET_BUILDER_ADDR`） | 使用者決定，見附錄 A |
| `FILET_ADMIN_ADDRESSES_PLACEHOLDER` | admin 白名單地址（逗號分隔） | 使用者決定，見附錄 A |
| `FILET_GIT_REMOTE_PLACEHOLDER` | ~~repo 取得方式，本 repo 目前無 git remote~~ **已於 2026-07-19 更新**：repo 已有私有 remote（`git@github.com:jimlai1005/Spark.git`），但部署改採 rsync 推碼、不用 git clone——見 §3.2 | 不適用（見 §3.2） |

---

## 1. 系統準備

### ⚠️ 前置：Lightsail 雲防火牆（本機 ufw 之外的另一層）（2026-07-19 實機部署修正）

Lightsail 實例有**獨立於 ufw 的雲端防火牆**，預設只放行 22。未開 80/443 時：
外網完全連不到、且 **Let's Encrypt 的 HTTP-01 challenge 會逾時失敗**（憑證簽不下來）。

操作：Lightsail Console → 實例 → Networking → IPv4 Firewall → 新增 HTTP(80) 與 HTTPS(443)。
（此步**無法用 CLI 完成**，除非 IAM user 有 lightsail:* 權限。）

驗證（從**外部**機器）：

```bash
nc -z -w5 <IP> 80 && echo "80 OPEN"; nc -z -w5 <IP> 443 && echo "443 OPEN"
```

```bash
# 以有 sudo 權限的一般帳號登入（Lightsail 預設 ubuntu 帳號）
sudo apt-get update && sudo apt-get -y upgrade
sudo apt-get -y install build-essential curl git ufw fail2ban unattended-upgrades

# 防火牆：只開 22（SSH）/80（ACME + redirect）/443（TLS）
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
sudo ufw status verbose   # 驗收：三條 ALLOW 規則，其餘 deny

# fail2ban：sshd 預設 jail 就足夠（Lightsail 已用金鑰登入，此為縱深防禦）
sudo systemctl enable --now fail2ban
sudo fail2ban-client status sshd   # 驗收：Status for the jail: sshd 正常輸出

# 自動安全更新（降低長期未 patch 風險；不影響本 runbook 其餘步驟）
# ⚠️ 非互動式寫法（2026-07-19 實機部署修正）——`dpkg-reconfigure -plow` 會跳互動視窗，
# 批次 SSH 部署（無 tty）跑不了，會卡住整個部署腳本。
echo "unattended-upgrades unattended-upgrades/enable_auto_updates boolean true" | sudo debconf-set-selections
sudo dpkg-reconfigure -f noninteractive unattended-upgrades
```

---

## 2. 三個 OS user 建立與目錄權限

三層隔離對應設計文件的元件拓撲：`filet-engine`（金鑰生成/持有＋引擎）、`filet-api`
（對外 web，只能經 socket 問 key-service，讀不到金鑰）、`filet-dashboard`（前端，只呼叫 API）。
全部 `--system --no-create-home --shell /usr/sbin/nologin`（不可互動登入）。

```bash
sudo groupadd --system filet-engine
sudo useradd --system --no-create-home --shell /usr/sbin/nologin --gid filet-engine filet-engine

sudo groupadd --system filet-api
sudo useradd --system --no-create-home --shell /usr/sbin/nologin --gid filet-api filet-api

sudo groupadd --system filet-dashboard
sudo useradd --system --no-create-home --shell /usr/sbin/nologin --gid filet-dashboard filet-dashboard

# 關鍵：filet-api 必須是 filet-engine 群組的「附加成員」，key-service 的 unix socket
# 檔案 owner:group = filet-engine:filet-engine、mode 660——filet-api 要能 connect()
# 就得在這個群組裡（spec：「socket 檔 owner filet-engine、group 含 filet-api」）。
# SO_PEERCRED（見 §8 驗收 2）是這層之上的第二道防線，不是取代它。
sudo usermod -aG filet-engine filet-api

id filet-api   # 驗收：groups 輸出含 filet-api 與 filet-engine 兩者
```

### 目錄與權限表

| 路徑 | owner:group | mode | 建立方式 | 用途 |
|---|---|---|---|---|
| `/opt/filet/spark` | `root:root` | `755` | 手動 mkdir（§4） | repo checkout，三個 service user 都要能讀+執行（唯讀） |
| `/opt/filet/spark/web` | `root:root`（build 產物） | `755` | `npm run build` 產出後 | Next.js build 輸出，filet-dashboard 讀+執行 |
| `/etc/filet/keys` | `filet-engine:filet-engine` | `700` | 手動 mkdir | agent key 根目錄；子目錄/檔案由 `EnvFileKeyStore` 自己建（子目錄 700、`agent.key` 600） |
| `/etc/filet/followers` | `filet-engine:filet-engine` | `700` | 手動 mkdir | per-follower `<id>.env`（640，見 `deploy/follower.env.example`） |
| `/var/lib/filet-api` | `filet-api:filet-api` | `0750`（systemd `StateDirectory` 預設） | 由 `filet-api.service` 的 `StateDirectory=filet-api` 自動建立 | `api.db`、`pending.json` |
| `/run/filet` | `filet-engine:filet-engine` | `0750`（`RuntimeDirectoryMode` 見 `filet-keysvc.service`） | 由 `filet-keysvc.service` 的 `RuntimeDirectory=filet` 自動建立（服務啟動時） | `keysvc.sock`（socket 檔本身 660，見 `serve_forever` chmod） |
| `/opt/filet/state/<account_id>` | `filet-engine:filet-engine` | `700` | 由 `filet-follower@.service` 的 `ReadWritePaths` 隱含要求，首次啟動前手動 `mkdir -p` | 單一 follower 引擎狀態根（kill-switch 不連坐） |
| `/opt/filet/spark/var/filet` | `root:root` | `755` | 手動 mkdir（§5.5） | leader 白名單與 follower manifest 的所在目錄 |
| `/opt/filet/spark/var/filet/leaders.json` | **`root:root`** | **`644`** | 手動建立（§5.5，範本 `deploy/leaders.json.example`） | ⭐ 策劃 leader 白名單。**承重點：filet-api 必須寫不到**——它被打穿時攻擊者能改 pending/manifest，唯獨改不了這份檔，引擎每輪的二次驗證才擋得住 |

```bash
sudo mkdir -p /opt/filet/spark
sudo chown root:root /opt/filet/spark && sudo chmod 755 /opt/filet/spark

sudo mkdir -p /etc/filet/keys /etc/filet/followers
sudo chown filet-engine:filet-engine /etc/filet/keys /etc/filet/followers
sudo chmod 700 /etc/filet/keys /etc/filet/followers

# /var/lib/filet-api 與 /run/filet 不手動建——分別由 filet-api.service 的
# StateDirectory、filet-keysvc.service 的 RuntimeDirectory 在服務啟動時自動建立並設好權限
# （手動 mkdir 反而可能權限不一致，見 §9 回滾一節的常見誤區）。
```

---

## 3. Python 3.11 + uv 安裝、repo 部署

### 3.1 uv + Python 3.11

不依賴 Ubuntu 22.04 內建的 Python 3.10（`pyproject.toml` 要求 `>=3.11`）——用 uv 自帶的
Python 管理，不需要 deadsnakes PPA。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"   # 或重新登入 shell
uv --version                     # 驗收：印出版本號

# ⚠️ 必須指定安裝路徑（2026-07-19 實機部署修正）
# 預設會裝進 /home/ubuntu/.local/share/uv/python，但所有 systemd unit 都設了
# ProtectHome=true——venv 的 interpreter symlink 會指向服務讀不到的 /home，服務必定起不來。
sudo mkdir -p /opt/filet/python && sudo chown ubuntu:ubuntu /opt/filet/python
export UV_PYTHON_INSTALL_DIR=/opt/filet/python
uv python install 3.11
uv python list | grep 3.11       # 驗收：3.11.x 已安裝
```

> `export UV_PYTHON_INSTALL_DIR=...` 只在目前 shell session 生效。若中途登出重進再執行
> §3.2 的 `uv sync`，記得重新 `export` 一次，否則 `uv sync` 會退回預設路徑另外裝一份到
> `/home`，重蹈同一個 bug。venv 是否乾淨的實際驗證在 §3.2（`readlink -f .venv/bin/python`）。

### 3.2 repo 部署（rsync 推碼，2026-07-19 實機部署修正）

> repo 其實已有私有 remote（`git@github.com:jimlai1005/Spark.git`）——原文件寫「本 repo
> 目前無 git remote」已過時。但本次部署刻意不在伺服器上執行 `git clone`：這是私有 repo，
> 在伺服器放 GitHub 存取憑證（deploy key／PAT）多一份要管理的機密。改採 **rsync 推碼**：
> 直接從本機把工作樹同步過去，伺服器端全程不需要任何 GitHub 存取權限。

```bash
# 在本機執行 (/Users/jim/projects/spark)：
rsync -az --delete \
  --exclude node_modules --exclude .venv --exclude .next \
  --exclude __pycache__ --exclude var --exclude .pytest_cache \
  -e "ssh -i <金鑰路徑>" /Users/jim/projects/spark/ ubuntu@FILET_LIGHTSAIL_IP_PLACEHOLDER:/tmp/spark-sync/
```

```bash
# 回 Lightsail：把暫存目錄搬到正式路徑並修正 owner
sudo mkdir -p /opt/filet
sudo rsync -a --delete /tmp/spark-sync/ /opt/filet/spark/
rm -rf /tmp/spark-sync   # 清掉暫存，不留在 /tmp
```

```bash
cd /opt/filet/spark
uv sync   # 從 pyproject.toml 解析、產生 .venv/ 與 uv.lock（uv.lock 是 gitignored，僅存在此機）
uv run python -c "import spark; print('spark import OK')"   # 驗收

# 驗證 venv 內零 /home 參照（確認 §3.1 的 UV_PYTHON_INSTALL_DIR 修正生效）：
readlink -f .venv/bin/python    # 應指向 /opt/filet/python/...，不得出現 /home

# 首次部署後，把解出的版本記進本文件（見文末「附錄 B」），避免下次部署解出不同版本造成漂移。
sudo chown -R root:root /opt/filet/spark
sudo chmod -R go-w /opt/filet/spark   # 確保 group/other 無寫入權（唯讀給三個 service user）
```

> 之後每次重新部署（拉新版本）：重跑上面兩段 rsync（`--delete` 會清掉伺服器上已刪除的
> 檔案，保持與本機工作樹一致），再視情況重跑 `uv sync`（只在依賴有變動時需要）。

---

## 4. Node 20 LTS + 前端 build + dashboard 部署

### 4.0 Swap 與 follower 容量估算（2GB 機型，2026-07-19 實機部署修正）

2GB RAM 機型跑 `npm run build`（§4.2）偶爾會頂到記憶體上限；先建 2GB swapfile 當保險，
免得 build 中途被 OOM killer 打斷。實測本次部署 build 只用到約 3MB swap（代表平常不會
真的觸發），純粹是防禦性配置，不建才是賭運氣。

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
free -m   # 驗收：Swap 行顯示 2048 total

# 開機自動掛載（持久化，不然重開機後 swap 就沒了）
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
cat /etc/fstab | grep swapfile   # 驗收：有這一行
```

**follower 容量估算（實測值）**：四個常駐服務（keysvc + api + dashboard + nginx，不含
follower）合計約 203MB，`free -m` 的 available 約 1372MB；每個 `filet-follower@` 實例
實測約 55-60MB。以此估算，**約可容納 15-20 個 follower** 才會開始吃到 swap；建議接近
10 個 follower 時就開始盯 `free -m -h`，不要等到逼近上限才注意。

### 4.1 安裝 Node 20 LTS

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
node --version   # 驗收：v20.x
npm --version
```

### 4.2 前端 build

> 前提：`web/` 目錄已由前端計畫（`docs/superpowers/plans/2026-07-17-m2-frontend.md`，
> 尚未執行）落地。本節假設 `web/` 已存在並含 `package.json`/`package-lock.json`。

```bash
cd /opt/filet/spark/web
npm ci    # 需要已 commit 的 package-lock.json；若尚無 lock 檔，改用 npm install 並在
          # 部署後把產生的 package-lock.json commit 回 repo（鎖版本，避免下次部署解出不同版本）
npm run build   # 驗收：`.next/` 產出，無 build error
```

```bash
sudo chown -R root:root /opt/filet/spark/web
sudo chmod -R go-w /opt/filet/spark/web
```

### 4.3 安裝 dashboard systemd unit

```bash
sudo cp /opt/filet/spark/deploy/filet-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
# 先不 enable/start——等 §6 依順序統一拉起
```

### 4.4 ⚠️ 安全前提：前端與 filet-api 必須是不同信任域

**這一節不是操作步驟，是一條部署約束。改動部署拓撲前必須回來讀。**

`/leaders` 換 leader 流程有一道**前端防線**——在喚起錢包前斷言「伺服器回傳的待簽
授權對象 == 使用者所選的 leader」。它擋的是：**filet-api 被打穿後，讓使用者簽下
對攻擊者所選 leader 的真實授權**。客戶的簽章是整套設計裡唯一能回答「這位客戶真的
要求換到他嗎」的東西（見 `src/spark/filet/leader_change.py` 檔頭的威脅模型），
而簽章一旦被騙取，後面引擎的二次驗章會**正常通過**——因為簽章本身是真的。

**這道防線有效的前提是：前端 bundle 由 `filet-dashboard` 服務、檔案 root:root 唯讀，
攻擊者打穿 filet-api 改不到它。** 前端與 API 分屬兩個信任域，斷言才有意義：
被打穿的一方無法竄改做斷言的那一方。

> **⚠️ 若日後改由 filet-api 服務前端 bundle、或兩者同源部署，此防護會靜默失效，
> 且不會有任何測試轉紅。** 攻擊者只要能改到 bundle，就能連同那句斷言一起改掉，
> 而所有測試仍然全綠、所有服務仍然正常運作——沒有任何訊號會告訴你防線沒了。

變更部署拓撲（合併服務、改用同一個進程服務靜態檔、把前端搬到 API 的 static 目錄、
引入會同時代理兩者的 SSR/BFF 層）**之前必須重新評估**，並在此記錄評估結論。

驗收（確認兩者確實是不同信任域）：

```bash
# 1. 前端 bundle 的 owner 必須是 root，且 filet-api 寫不到
ls -ld /opt/filet/spark/web/.next
sudo -u filet-api touch /opt/filet/spark/web/.next/probe 2>&1 | head -1
# 預期：touch: cannot touch ...: Permission denied

# 2. 兩個服務跑在不同 User 底下（同一個 User 就不是兩個信任域）
systemctl show filet-dashboard -p User; systemctl show filet-api -p User
# 預期：User=filet-dashboard / User=filet-api，兩者不同
```

---

## 5. systemd units 安裝順序

**順序理由**：keysvc 先起（socket 要存在，api 才連得到）→ api 次之（dashboard 依賴它做
onboarding）→ dashboard → follower（依帳號 activate 後才起，見 §5.4）。

### 5.1 安裝 unit 檔

```bash
sudo cp /opt/filet/spark/deploy/filet-keysvc.service   /etc/systemd/system/
sudo cp /opt/filet/spark/deploy/filet-api.service      /etc/systemd/system/
sudo cp /opt/filet/spark/deploy/filet-dashboard.service /etc/systemd/system/
sudo cp /opt/filet/spark/deploy/filet-follower@.service /etc/systemd/system/
sudo systemctl daemon-reload
```

> 兩個**定時任務**（`filet-leaderboard` 與 `filet-perf-series`）的 unit 不在這裡裝，
> 它們有自己的一節：**§5.7**。不做那一節不影響本節的服務起得來，但 leader 目錄頁
> 會永遠沒有統計、績效序列會永久缺資料點（見該節）。

### 5.2 填 `REPLACE_WITH_FILET_API_UID`

`filet-keysvc.service` 的 `FILET_KEYSVC_ALLOWED_UIDS` 必須是 `filet-api` 的**實際數字 uid**
（SO_PEERCRED 比對的是 uid，不是使用者名稱）。

```bash
id -u filet-api   # 記下這個數字，例如 998

sudo systemctl edit --full filet-keysvc.service
# 把這一行：
#   Environment=FILET_KEYSVC_ALLOWED_UIDS=REPLACE_WITH_FILET_API_UID
# 改成（用上面 id -u filet-api 印出的實際數字取代 998）：
#   Environment=FILET_KEYSVC_ALLOWED_UIDS=998
# 存檔離開編輯器（systemctl edit --full 會把改動寫到
# /etc/systemd/system/filet-keysvc.service，不動 /opt/filet/spark/deploy/ 底下的原檔）。

sudo systemctl daemon-reload
grep FILET_KEYSVC_ALLOWED_UIDS /etc/systemd/system/filet-keysvc.service   # 驗收：不含 REPLACE_WITH
```

### 5.3 其餘環境變數佔位符

`filet-api.service` 還有三個佔位符要填（同樣用 `systemctl edit --full filet-api.service`）：

| 變數 | 填入值 |
|---|---|
| `FILET_BUILDER_ADDR` | `FILET_BUILDER_ADDR_PLACEHOLDER`（附錄 A） |
| `FILET_SIWE_DOMAIN` | `FILET_DOMAIN_PLACEHOLDER` 實際網域 |
| `FILET_SIWE_URI` | `https://FILET_DOMAIN_PLACEHOLDER` |
| `FILET_ADMIN_ADDRESSES` | `FILET_ADMIN_ADDRESSES_PLACEHOLDER`（附錄 A，逗號分隔） |

```bash
sudo systemctl daemon-reload
grep -E 'REPLACE_WITH|PLACEHOLDER' /etc/systemd/system/filet-api.service
# 驗收：改完後這個 grep 應該零輸出（找不到任何殘留佔位符）
```

### 5.4 拉起服務（依序，逐一確認再往下）

```bash
sudo systemctl enable --now filet-keysvc.service
sudo systemctl status filet-keysvc.service --no-pager   # 驗收：active (running)
ls -l /run/filet/keysvc.sock                             # 驗收：srwxrwx--- filet-engine filet-engine

sudo systemctl enable --now filet-api.service
sudo systemctl status filet-api.service --no-pager       # 驗收：active (running)

sudo systemctl enable --now filet-dashboard.service
sudo systemctl status filet-dashboard.service --no-pager # 驗收：active (running)

# follower@<account_id> 不在這裡起——依 spec §7，activate 是人工 CLI 動作
# （見 §5.5 的 activate 指令），只在有帳號完成 onboarding+verify 後才拉起。
# 本 runbook 只負責讓「拉起 follower 的能力」就緒，不代表部署當下就有帳號要拉。
```

### 5.5 ⭐ 建立 leader 白名單（**不做這步，第一個選 leader 的客戶就會卡死**）

白名單是「客戶可以跟哪些 leader」的唯一合法來源，也是 filet-api 被打穿時**唯一**
還擋得住「把 follower 指向惡意 leader」的東西（威脅模型見
`src/spark/filet/leaders.py` 檔頭）。repo 內**刻意不附**真實的 `leaders.json`
（它是營運資料，不是程式碼），所以這步不做的話：

- 任何帶 `--leader` 的 activate 會直接 `SystemExit`（白名單檔不存在 → 空清單 → 全拒）
- env 回退路徑會走「白名單檔不存在」的向後相容豁免——**這道防線等於沒啟用**

```bash
# 目錄與檔案：owner 必須是 root，且 filet-api 寫不到（承重點，見 §2 權限表）
sudo mkdir -p /opt/filet/spark/var/filet
sudo chown root:root /opt/filet/spark/var/filet
sudo chmod 755 /opt/filet/spark/var/filet

# 以範本起手，改成真實 leader（範本內含兩個旗標的用法說明）
sudo cp /opt/filet/spark/deploy/leaders.json.example \
        /opt/filet/spark/var/filet/leaders.json
sudo vi /opt/filet/spark/var/filet/leaders.json   # 換掉三筆範例位址與名稱

sudo chown root:root /opt/filet/spark/var/filet/leaders.json
sudo chmod 644 /opt/filet/spark/var/filet/leaders.json

# 驗收 1：JSON 合法（格式壞掉會讓引擎與 activate 一起 fail-fast）
python3 -m json.tool /opt/filet/spark/var/filet/leaders.json > /dev/null && echo "JSON OK"

# 驗收 2：權限正確——filet-api 讀得到、寫不到
sudo -u filet-api test -r /opt/filet/spark/var/filet/leaders.json && echo "api 可讀 OK"
sudo -u filet-api test -w /opt/filet/spark/var/filet/leaders.json \
  && echo "★ 危險：filet-api 可寫，白名單防線失效！" || echo "api 不可寫 OK"
```

#### 下架一個 leader 時：`enabled` 還是 `accepting_new`？

**選錯的代價是雙向的**（該止血的沒止血／不該平倉的付了平倉成本），所以先問一句：
**「正在跟他的客戶，現在該不該立刻出場？」**

| 情境 | 用哪個 | 對**正在跟**的 follower 的後果 |
|---|---|---|
| 帳號被盜、開始對敲、策略失控、任何需要**立刻止血** | `"enabled": false` | ⭐ **受控收尾**：停止開新倉 → 撤單 → reduce-only 全平 → 鎖死 kill switch。有真實 taker 成本，**re-arm 需人工刪 ARM 檔** |
| 名額滿、策略調整中、準備退場等**例行下架** | `"accepting_new": false` | **完全不受影響**，繼續正常跟單 |

```bash
# 改完一律重驗 JSON——白名單壞掉會讓引擎 fail-fast（這是刻意的，但要知道）
python3 -m json.tool /opt/filet/spark/var/filet/leaders.json > /dev/null && echo "JSON OK"
```

- ⚠️ **把條目整筆刪掉 ＝ `enabled: false`**（引擎驗不到就當作撤銷，會觸發所有跟隨者
  收尾）。例行下架請改 `accepting_new`，**不要刪條目**——條目保留也才有歷史可查。
- 改動**不需要重啟 follower**：引擎每個 cycle 重讀白名單，最遲下一輪生效。
- `enabled: false` 之後請確認收尾真的發生了（別停在「我已下架＝已止血」的假設上）：

```bash
sudo journalctl -u 'filet-follower@*' --since '10 min ago' | grep -i '撤銷\|leader_revoked'
ls -l /opt/filet/state/*/var/copytrade/killswitch.tripped   # 收尾完成的 ARM 檔
```

### 5.5.1 ⭐⭐ 建立換 leader 交換目錄（不做這步，客戶按「換 leader」永遠不會生效）

> 編號用 `5.5.1` 是刻意的插入步驟——它必須排在 §5.4 拉起服務**之前**做完，
> 但不重編後面的章節號（避免程式碼裡的 §5.6 引用失效）。

客戶簽章的換 leader 記錄是 **filet-api 寫、引擎讀** 的**共享**產物，它有自己的目錄。

**為什麼不能跟 `pending.json` 同住 `/var/lib/filet-api`**（2026-07-19 opus 審查 C3，
上線前抓到）：`pending.json` 是 **API 私有**產物（含活化前的客戶資料，只有 filet-api
該讀得到），而變更記錄必須讓 **filet-engine** 讀得到。一個目錄的權限**滿足不了這兩個
相反的要求**，於是舊設計實際跑出來的結果是：API 寫 `/var/lib/filet-api/leader_changes.json`、
引擎讀 repo 內的 `var/filet/leader_changes.json`——**兩個進程根本在讀寫不同的檔案**，
換 leader 功能完全不通，而 API 仍回客戶「於引擎的下一個 cycle 生效」。

| 路徑 | owner:group | mode | 建立方式 | 用途 |
|---|---|---|---|---|
| `/var/lib/filet-exchange` | **`filet-api:filet-engine`** | **`0750`** | 手動 mkdir（本節） | ⭐ 客戶簽章的共享記錄：`leader_changes.json`（換 leader）＋ `capital_settings.json`（資金設定）。owner 寫、group 讀、other 無 |

> 兩份記錄**刻意分開兩個檔**（不是同一個檔多一個欄位）：讀者是兩個獨立的套用器，
> 共用一個檔會讓其中一方的格式問題連坐另一方——而這兩件事各自都能造成資金損失，
> 不該共命運（`src/spark/filet/capital_settings.py` 檔頭）。兩者同目錄、同權限拓撲，
> 所以本節的建立與驗收步驟**一次涵蓋兩者**，不需要另外再建一個目錄。

**方向是單向的**：filet-api 寫、filet-follower@ 讀。引擎那邊的 unit **刻意不把這個目錄
列入 `ReadWritePaths`**——被打穿的引擎因此污染不了 API 的狀態。

```bash
sudo mkdir -p /var/lib/filet-exchange
sudo chown filet-api:filet-engine /var/lib/filet-exchange
sudo chmod 0750 /var/lib/filet-exchange
```

**兩個 unit 都必須宣告 `FILET_EXCHANGE_DIR`**（`deploy/filet-api.service` 與
`deploy/filet-follower@.service` 已內建，值必須逐字元相同）：

```ini
Environment=FILET_EXCHANGE_DIR=/var/lib/filet-exchange
```

⭐ **半邊漏設會拒絕啟動，不會靜默失效**：這個變數**沒有預設值**。API 漏設 →
`ApiConfig.from_env` 拒絕啟動；引擎漏設 → `require_exchange_dir` 拒絕啟動
（unit 進 `failed`，是可監控的狀態）。「起不來」刻意優先於「起來了但功能靜默失效」
——後者可能好幾天沒人發現，而期間客戶每一次換 leader 都石沉大海。

#### 驗收（三條都要跑，缺一條就沒證明打通）

```bash
# 驗收 1：filet-api 寫得進去
sudo -u filet-api touch /var/lib/filet-exchange/.probe \
  && echo "api 可寫 OK" || echo "★ 失敗：api 寫不進，換 leader 記錄永遠落不了地"

# 驗收 2：filet-engine 讀得到（同一個檔，不是同名的另一個檔）
sudo -u filet-engine test -r /var/lib/filet-exchange/.probe \
  && echo "engine 可讀 OK" || echo "★ 失敗：引擎讀不到，客戶按了永遠不生效"

# 驗收 3：filet-engine 寫**不**進去（方向單向；寫得進代表 mode/group 給錯了）
sudo -u filet-engine touch /var/lib/filet-exchange/.engine-probe \
  && echo "★ 危險：引擎可寫，單向性失效！" || echo "engine 不可寫 OK"

sudo rm -f /var/lib/filet-exchange/.probe

# 驗收 4：兩個 unit 宣告的值真的相同（打錯字是本節唯一擋不住的殘餘風險）
systemctl show filet-api.service -p Environment | tr ' ' '\n' | grep FILET_EXCHANGE_DIR
systemctl show 'filet-follower@<account_id>.service' -p Environment \
  | tr ' ' '\n' | grep FILET_EXCHANGE_DIR
```

> 「兩邊都設了但**值不同**」是唯一擋不住的一格（兩個進程各看各的 env，沒有共同的
> 仲裁者）。驗收 4 是人工比對；長期由 `scripts/filet_daily_report.py` 的
> 「已寫入但未被套用」對帳告警接住——記錄落地超過 30 分鐘而引擎帳本沒有對應的
> redeemed nonce 就告警，那涵蓋路徑不通、權限錯、引擎沒跑整類失敗。

---

### 5.6 activate 一個 follower（人工 CLI）

⚠️ **必須指定絕對路徑或先 `cd`**：`--pending`／`--manifest` 的預設值是 CWD 相對的，
在錯的目錄跑會寫出一份引擎讀不到的 manifest（引擎讀的是
`/opt/filet/spark/var/filet/followers.json`），症狀是「activate 說成功了，
follower 起來卻找不到自己」。

```bash
cd /opt/filet/spark
sudo FILET_BUILDER_ADDR=<builder 位址> \
  ./.venv/bin/python -m scripts.filet_activate <account_id> \
  --pending  /var/lib/filet-api/pending.json \
  --manifest /opt/filet/spark/var/filet/followers.json \
  --leaders  /opt/filet/spark/var/filet/leaders.json \
  --leader   <leader 位址>

# 驗收：manifest 裡有這筆、leader 是預期的那個
sudo python3 -m json.tool /opt/filet/spark/var/filet/followers.json | grep -A5 <account_id>

sudo mkdir -p /opt/filet/state/<account_id>
sudo chown filet-engine:filet-engine /opt/filet/state/<account_id>
sudo chmod 700 /opt/filet/state/<account_id>
sudo systemctl start filet-follower@<account_id>
```

#### ⚠️ 健康面板與狀態根權限（`/api/ops/health` 預設看不到，這是刻意的預設）

`0700` 表示只有 `filet-engine` 讀得到狀態根。營運健康面板跑在 **filet-api** 進程裡，
所以**預設情況下它讀不到**任何 follower 的 kill switch 狀態、equity 樣本覆蓋度與
告警數——面板會把這些格子顯示成 **「未知」**（`null`），這是正確且刻意的行為，
不是 bug。

> ⭐ 面板**不會**因為讀不到就顯示「未觸發／健康」。這一格曾經是個真的 bug：
> Python 的 `Path.exists()` 會把 PermissionError 吞成 `False`，於是一個**確實已經
> 熔斷**的 follower 會被回報成「kill switch 未觸發」。現在由
> `ops.state_root_status()` 先探測可讀性，讀不到一律整列標未知
> （回歸測試：`tests/test_api_ops.py::test_unreadable_state_root_never_reports_killswitch_as_untripped`）。

**要讓面板真的看得到，需要放寬到 `0750`**（`filet-api` 已是 `filet-engine` 群組的
附加成員，見 §2）：

```bash
# ⚠️ 這是一個**權限放寬**的決定，請先確認你要的是哪一邊的取捨：
#   維持 0700 → 面板顯示「未知」，但狀態根（含 ARM 檔、equity 樣本、已兌現帳本）
#               只有引擎讀得到。
#   放寬 0750 → 面板可用，代價是 filet-api 被打穿時可以**讀**（仍不能寫）這些檔案。
#               這些檔案不含私鑰（私鑰在 keysvc，filet-api 本來就讀不到，見 §8 驗收 1）。
sudo chmod 0750 /opt/filet/state/<account_id>

# 驗收：filet-api 讀得到，且**寫不進去**（唯讀方向必須維持）
sudo -u filet-api test -r /opt/filet/state/<account_id> \
  && echo "api 可讀 OK（面板會有資料）" || echo "api 仍讀不到（面板顯示未知）"
sudo -u filet-api touch /opt/filet/state/<account_id>/.probe \
  && echo "★ 危險：api 可寫，唯讀方向失效！" || echo "api 不可寫 OK"
```

---

### 5.7 ⭐ 兩個定時任務（leaderboard 快照 ＋ 績效序列取樣）

> 編號 `5.7` 是**附加**在 §5 尾端的新章節（不重編任何既有編號，程式碼與文件裡對
> §5.5.1／§5.6 的引用不受影響——沿 §5.5.1 的同一個插入慣例）。

兩個 timer 都跑在 `filet-api` 帳號下、都只讀上游、都把結果寫進 `/var/lib/filet-api`。
兩者的抓取對象都來自**同一個 leader 白名單**（§5.5 的 `leaders.json`），所以白名單
新增 leader 之後不需要再同步任何 env。

| Timer | 節奏 | 產物 | 漏跑的後果 |
|---|---|---|---|
| `filet-leaderboard` | 每日 00:10 UTC | 全站 top-N ＋ watchlist 快照 | **可補**：明天再抓一次最新值即可 |
| `filet-perf-series` | 每 12 小時（00:05／12:05 UTC） | leader 績效時間序列 | ⭐⭐ **不可補**：見下方警告 |

#### ⚠️⚠️ `filet-perf-series` 是 append-only 且**無法回填**——它的存活監控比其他 timer 重要

這支腳本抓的是 Hyperliquid 的 `perpDay` 窗，**該窗只保留 24 小時**。漏跑一次就是
**那個 12 小時窗的資料點永久消失**，明天、下週、任何時候都補不回來——上游根本
不再持有那段資料。

而且後果不只是「少一個點」：序列的拼接靠**相鄰兩次取樣的重疊**把窗內累積 PnL 重定
基準（12 小時抓一次 → 約 12 小時重疊）。漏一次 ⇒ 重疊消失 ⇒ 拼接時**產生一個新的
分段**，那個斷點會一路留在往後所有的績效圖上。

所以：

- unit 檔**刻意沒有** `ExecStart` 的 `-` 前綴（與 `filet-leaderboard` 的全站快照那條
  相反）——失敗必須讓 unit 進 `failed`，被監控看見。
- **這一支的失敗必須有人處理**，不能像日常告警那樣攢著。它與其他 timer 不同的地方
  在於「晚點再看」這個選項不存在：等你看到的時候，資料已經沒了。
- `Persistent=true` 只救「關機期間錯過、開機後仍在同一個 bucket 內」的情形。補跑若
  已跨進下一個 bucket，漏掉的那個窗**永久缺席**（會顯示為新分段，是可見的證據而不是
  靜默的洞）。

#### 安裝與啟用

```bash
sudo cp /opt/filet/spark/deploy/filet-leaderboard.service /etc/systemd/system/
sudo cp /opt/filet/spark/deploy/filet-leaderboard.timer   /etc/systemd/system/
sudo cp /opt/filet/spark/deploy/filet-perf-series.service /etc/systemd/system/
sudo cp /opt/filet/spark/deploy/filet-perf-series.timer   /etc/systemd/system/
sudo systemctl daemon-reload

# ⚠️ enable --now 的是 **.timer**，不是 .service。
# enable 錯對象（enable 了 .service）會讓這支 oneshot 在**每次開機**跑一次、
# 而且從此不再定時跑——症狀是「資料每隔幾天才有一筆」，很久沒有人會發現。
sudo systemctl enable --now filet-leaderboard.timer
sudo systemctl enable --now filet-perf-series.timer
```

#### 驗收（四條都要跑）

```bash
# 驗收 1：兩個 timer 都已排程，且下次觸發時間合理（00:10／00:05 或 12:05 UTC）
systemctl list-timers 'filet-*' --all --no-pager
# 預期：兩行都在，NEXT 欄有時間、不是 n/a（n/a = timer 沒 enable 或 OnCalendar 寫錯）

# 驗收 2：手動各跑一次，確認**現在就能成功**（不要等到明天才發現 env 或權限錯）
sudo systemctl start filet-leaderboard.service
systemctl status filet-leaderboard.service --no-pager -l   # 預期：SUCCESS（oneshot 跑完即 inactive）
sudo systemctl start filet-perf-series.service
systemctl status filet-perf-series.service --no-pager -l   # 預期：SUCCESS

# 驗收 3：產物真的落地了（unit 回 SUCCESS 但沒有檔＝白名單是空的／路徑錯）
# 版面出自 scripts/watchlist_snapshot.py 與 filet/perf_series.py 的 series_dir_for：
#   <FILET_DATA_DIR>/leaderboard/watchlist/<YYYY-MM-DD>.json   （一天一個檔）
#   <FILET_DATA_DIR>/leaderboard/perf_series/<address>.json    （一個 leader 一個檔）
sudo ls -l /var/lib/filet-api/leaderboard/watchlist/   | tail -5
sudo ls -l /var/lib/filet-api/leaderboard/perf_series/ | tail -5
# 預期：watchlist 有今天日期的檔；perf_series 每個白名單 leader 各一個檔，
# 且 mtime 是剛才那次手動執行。⭐ 沒有檔就是**現在**要查，不是明天
# （perf_series 尤其：等到明天，今天漏掉的那兩個窗已經永遠補不回來了）

# 驗收 4：兩個 unit 看到的是同一份白名單（抓取對象的單一來源）
systemctl show filet-leaderboard.service -p Environment | tr ' ' '\n' | grep FILET_LEADERS_PATH
systemctl show filet-perf-series.service  -p Environment | tr ' ' '\n' | grep FILET_LEADERS_PATH
# 預期：兩行值逐字元相同，且等於 §5.5 建立的 leaders.json 路徑
```

#### 例行監控（`filet-perf-series` 需要比其他 timer 更積極的檢查）

```bash
# 有沒有跑失敗（⭐ perf-series 出現在這裡就是資料已經開始缺了）
systemctl list-units 'filet-*' --state=failed --no-pager

# 最近幾次執行的結果
sudo journalctl -u filet-perf-series --since '3 days ago' --no-pager | tail -30

# ⭐ 最直接的檢查：序列檔最後更新是多久以前？超過 ~12.5 小時就代表漏了一個窗
sudo ls -lt --time-style=long-iso /var/lib/filet-api/leaderboard/perf_series/ | head -5
```

> 營運後台的健康面板（`/api/ops/health`）看的是 **follower 引擎**的存活，**不涵蓋
> 這兩個 timer**。timer 的存活由 `systemctl list-timers` 與上面的 `--state=failed`
> 檢查負責——兩者是不同的東西，不要以為面板是綠的就代表序列還在收。

---

## 6. nginx + certbot

```bash
sudo apt-get install -y nginx certbot python3-certbot-nginx

sudo mkdir -p /var/www/certbot   # ACME challenge webroot（nginx-filet.conf 已配置此路徑）

# 部署本文件配套的反代設定，把 FILET_DOMAIN_PLACEHOLDER 換成實際網域
sudo cp /opt/filet/spark/deploy/nginx-filet.conf /etc/nginx/sites-available/filet
sudo sed -i 's/FILET_DOMAIN_PLACEHOLDER/'"實際網域"'/g' /etc/nginx/sites-available/filet
sudo ln -sf /etc/nginx/sites-available/filet /etc/nginx/sites-enabled/filet
sudo rm -f /etc/nginx/sites-enabled/default   # 移除預設站台，避免落到錯的 server block

# 先跑 certbot 取憑證（用 --nginx 外掛，會自動改寫 ssl_certificate 路徑並設 HTTP→HTTPS）
sudo certbot --nginx -d 實際網域 --redirect --agree-tos -m 你的email

sudo nginx -t          # 驗收：syntax is ok / test is successful
sudo systemctl reload nginx
sudo systemctl status nginx --no-pager   # 驗收：active (running)

# certbot 自動續期（Ubuntu 22.04 的 certbot 套件已含 systemd timer，確認它存在即可）
systemctl list-timers | grep certbot   # 驗收：有一條 certbot.timer
```

> `deploy/nginx-filet.conf` 的 `/api/` upstream 寫死 `127.0.0.1:8700`——這是
> `filet-api.service` 裡 `FILET_API_PORT` 的**預設值**（`scripts/run_api.py` 未設定時也是
> 8700）。若部署時特意覆寫 `FILET_API_PORT`，這裡的 nginx upstream port 要同步改，
> 否則會出現「systemctl status 正常但外部一律 502」的假故障——兩邊必須讀同一個埠號
> （工程原則 1：同源同單位比較，此處是「同一個埠號常數」）。

---

## 7. 環境變數檔位置與權限（部署日調整）

- `filet-keysvc.service`、`filet-api.service` 目前用 unit 檔內的 `Environment=` 行內宣告
  （非 secrets 部分可接受）。`filet-follower@.service` 已經用 `EnvironmentFile=/etc/filet/followers/%i.env`
  （見該檔第 12 行）——這是唯一含真正敏感值（Telegram token 等）的 unit，`.env` 檔本身
  640 filet-engine:filet-engine（見 `deploy/follower.env.example` 檔頭註解）。
- **本 runbook 不改動任何既有 `deploy/*.service`**（紅線）。若部署當下想把 `filet-api.service`
  的 `FILET_BUILDER_ADDR` 等改成 `EnvironmentFile=/etc/filet/api.env`（600 filet-api）而非行內
  `Environment=`——這是合理的加固方向（行內值會出現在 `systemctl cat`／`ps` 環境可見範圍，
  `EnvironmentFile` 較不會意外印在指令列輸出），但**留給部署日視情況決定**，不在本次
  runbook 產出範圍內動手改 unit 檔。若採用，記得：
  1. `sudo install -m 600 -o filet-api -g filet-api /dev/null /etc/filet/api.env`
  2. 把 `Environment=FILET_...=` 行內值搬進 `/etc/filet/api.env`（`KEY=value` 格式，不含 `Environment=` 前綴）
  3. unit 檔加一行 `EnvironmentFile=/etc/filet/api.env`
  4. `systemctl daemon-reload && systemctl restart filet-api`

---

## 8. 實機驗收清單（移交自 `docs/superpowers/plans/2026-07-17-m2-publicapi.md` 的部署清單）

以下每項附確切指令與預期輸出；全部通過才算部署完成，缺一項就是未完工。

### 驗收 1：filet-api 讀不到 agent key（非託管不變量的實機證明）

```bash
# 先用 key-service 生一把測試用 agent key（不影響任何真實帳號——account_id 自訂測試值）
# 走正常路徑：透過已啟動的 filet-api，呼一次 onboarding agent 生成端點會更貼近真實情境；
# 若還沒接前端，可直接用 python 對 socket 送一筆 generate 測試。
# ⚠️ 必須以 filet-api 身分執行（2026-07-19 實機部署修正）——SO_PEERCRED 白名單
# （FILET_KEYSVC_ALLOWED_UIDS，見 §5.2）只認 filet-api 的 uid，用 filet-engine 呼會被拒絕：
sudo -u filet-api /opt/filet/spark/.venv/bin/python - <<'PY'
import socket, json
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect("/run/filet/keysvc.sock")
s.sendall((json.dumps({"op": "generate", "account_id": "deploytest0000000000000000000000000000"}) + "\n").encode())
print(s.recv(4096))
PY
# 預期：{"ok": true, "agent_address": "0x...", ...} 之類的 JSON（確認 keysvc 正常運作，
# 且走的是生產真實路徑——這是唯一被白名單允許呼 generate 的身分）

sudo -u filet-api ls /etc/filet/keys
# 預期：ls: cannot open directory '/etc/filet/keys': Permission denied

sudo -u filet-api cat /etc/filet/keys/deploytest0000000000000000000000000000/agent.key
# 預期：cat: /etc/filet/keys/deploytest0000000000000000000000000000/agent.key: Permission denied
# 驗收意義：filet-api 生得出 key（上面 generate 成功），卻讀不到 key 檔本身——
# 這正是非託管不變量要證明的事（生成與持有分離），不是同一件事的兩個弱驗證。

sudo -u filet-dashboard cat /etc/filet/keys/deploytest0000000000000000000000000000/agent.key
# 預期：同上 Permission denied（filet-dashboard 連 filet-engine 群組都不在，權限更嚴）
```

### 驗收 2：SO_PEERCRED 實測（非白名單 uid 連 socket 被拒）

```bash
# file-mode 層：filet-dashboard 不在 filet-engine 群組，connect() 本身就該被拒——
# 這一步先確認「連都連不上」（第一道防線）：
sudo -u filet-dashboard /opt/filet/spark/.venv/bin/python - <<'PY'
import socket
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    s.connect("/run/filet/keysvc.sock")
    print("UNEXPECTED: connected")
except PermissionError as e:
    print("OK denied at file-mode layer:", e)
PY
# 預期：OK denied at file-mode layer: ...

# SO_PEERCRED 層：建一個「有 group 權限但不在允許 uid 清單」的測試帳號，證明第二道防線
# 獨立生效（不是只靠檔案權限）：
sudo useradd --system --no-create-home --shell /usr/sbin/nologin filet-test-peer
sudo usermod -aG filet-engine filet-test-peer

sudo -u filet-test-peer /opt/filet/spark/.venv/bin/python - <<'PY'
import socket, json
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect("/run/filet/keysvc.sock")   # file-mode 這關會過（在 group 裡）
s.settimeout(3)
s.sendall((json.dumps({"op": "address", "account_id": "x"}) + "\n").encode())
try:
    data = s.recv(4096)
    print("recv:", data)   # 預期空 bytes（server 直接關連線，不回應）
except socket.timeout:
    print("timeout (server never responded)")
PY
# 預期：recv: b''（server 授權失敗時直接關閉連線，不處理請求）

sudo journalctl -u filet-keysvc --since "2 min ago" | grep "拒絕未授權連線"
# 預期：至少一行「keysvc 拒絕未授權連線」——證明 SO_PEERCRED 檢查確實擋下了 filet-test-peer

# 清理測試帳號
sudo userdel filet-test-peer
```

### 驗收 3：8700/3000 只在本機通、外網只走 nginx

```bash
# 本機直連後端服務——應該通（這是 nginx upstream 的目標）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8700/api/auth/nonce?address=0x0000000000000000000000000000000000000000
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3000/
# 預期：印出 HTTP 狀態碼（4xx/2xx 皆代表有連上；重點是「連得上」不是特定碼）

# 確認監聽位址只在 127.0.0.1，沒有 0.0.0.0（這是「外網連不到」的根本保證，不是只靠 ufw）
ss -tlnp | grep -E ':(8700|3000)\b'
# 預期：兩行都是 127.0.0.1:8700 / 127.0.0.1:3000，看不到 0.0.0.0:8700 或 0.0.0.0:3000

# 從外部機器（非 Lightsail 本機，例如你的筆電）測試直連應該連不上：
curl -m 3 http://FILET_LIGHTSAIL_IP_PLACEHOLDER:8700/
curl -m 3 http://FILET_LIGHTSAIL_IP_PLACEHOLDER:3000/
# 預期：connection timed out / connection refused（ufw 未開放這兩個埠，且服務本身只聽 127.0.0.1）

# 從外部機器經 nginx（443）測試應該連得上：
curl -sk -o /dev/null -w '%{http_code}\n' https://FILET_DOMAIN_PLACEHOLDER/
curl -sk -o /dev/null -w '%{http_code}\n' https://FILET_DOMAIN_PLACEHOLDER/api/auth/nonce?address=0x0000000000000000000000000000000000000000
# 預期：印出 HTTP 狀態碼（連得上、有回應）
```

---

## 9. 回滾與日誌

### 9.1 日誌指令表

| 目的 | 指令 |
|---|---|
| key-service 即時日誌 | `sudo journalctl -u filet-keysvc -f` |
| Public API 即時日誌 | `sudo journalctl -u filet-api -f` |
| dashboard 即時日誌 | `sudo journalctl -u filet-dashboard -f` |
| 單一 follower 日誌 | `sudo journalctl -u filet-follower@<account_id> -f` |
| 全部 follower 最近錯誤 | `sudo journalctl -u 'filet-follower@*' -p err --since today` |
| nginx access/error | `sudo tail -f /var/log/nginx/{access,error}.log` |
| 服務啟動失敗的完整原因 | `sudo systemctl status <unit> --no-pager -l` |
| 兩個 timer 的排程狀態 | `systemctl list-timers 'filet-*' --all --no-pager` |
| ⭐ 績效序列近日執行結果（漏跑＝資料永久缺，見 §5.7） | `sudo journalctl -u filet-perf-series --since '3 days ago' --no-pager` |
| leaderboard 快照近日執行結果 | `sudo journalctl -u filet-leaderboard --since '3 days ago' --no-pager` |
| 任何進入 failed 的 unit（含 timer） | `systemctl list-units 'filet-*' --state=failed --no-pager` |

### 9.2 服務重啟順序

正常重啟（例如拉新版本後）依賴順序由上到下：

```bash
sudo systemctl restart filet-keysvc.service
sudo systemctl status filet-keysvc.service --no-pager   # 確認 active 再往下
sudo systemctl restart filet-api.service
sudo systemctl status filet-api.service --no-pager
sudo systemctl restart filet-dashboard.service
sudo systemctl status filet-dashboard.service --no-pager
# follower 一律用既有腳本滾動重啟（單一失敗不中止其餘）：
/opt/filet/spark/deploy/reload_follower.sh
```

### 9.3 回滾

```bash
cd /opt/filet/spark
git log --oneline -5              # 確認目前 commit 與要回滾到的目標
git checkout <上一個已知良好的 commit/tag>
uv sync                            # 依回滾後的 pyproject.toml 重新解析（可能觸發依賴降版）
# 若前端也要回滾：
cd web && npm ci && npm run build && cd ..

# 依 §9.2 順序重啟
sudo systemctl restart filet-keysvc.service filet-api.service filet-dashboard.service
/opt/filet/spark/deploy/reload_follower.sh

# nginx 設定回滾（若本次部署也改了 nginx-filet.conf）：
sudo cp /etc/nginx/sites-available/filet /etc/nginx/sites-available/filet.bak-$(date +%F)
# 換回舊版設定檔後：
sudo nginx -t && sudo systemctl reload nginx
```

**回滾不動的東西**：`/etc/filet/keys`（agent key，任何回滾都不該刪/改金鑰檔——那是
非託管信任鏈的一部分，回滾程式碼不等於回滾金鑰狀態）；`/var/lib/filet-api` 的
`pending.json`/`api.db`（onboarding 進度，回滾程式碼版本不代表要清使用者進度）。

---

## 附錄 A：部署日需使用者決策的點（本 runbook 無法代為決定）

1. **`FILET_DOMAIN_PLACEHOLDER` 實際網域**：需先有網域並把 DNS A 記錄指到 Lightsail 靜態 IP，
   本文件才能跑 certbot 那一步。
2. **`FILET_BUILDER_ADDR_PLACEHOLDER`**：我方 builder 錢包地址——這是收 builder fee 的地址，
   不是隨便填的測試值，需與 onboarding 流程要核准的鏈上帳戶一致。
3. **`FILET_ADMIN_ADDRESSES_PLACEHOLDER`**：admin 白名單地址——誰能看 `/admin/pending`，需你
   指定至少一個地址。
4. **repo 取得方式**：**已於 2026-07-19 更新**——repo 已有私有 remote
   （`git@github.com:jimlai1005/Spark.git`），但本次部署刻意不在伺服器上用 `git clone`
   （避免把 GitHub 存取憑證放上伺服器），改採 **rsync 推碼**（見 §3.2），伺服器端全程
   不需要任何 GitHub 存取權限。若之後想改回 `git clone` 流程，需額外在伺服器建 deploy key
   並鎖唯讀權限——不是目前採用的方式，此點不再是待決策項。
5. **§7 環境變數檔改用 `EnvironmentFile`**：屬於「值得做但非本次紅線允許動手」的加固——
   是否要在部署日順手做，由你決定（步驟已寫在 §7）。
6. **certbot 續期失敗的告警管道**：本文件只確認 `certbot.timer` 存在，沒有另外接告警
   （例如憑證到期前 N 天發 Telegram）。若要接，需決定告警管道與門檻，超出本次三檔案範圍。

## 附錄 B：首次部署後記錄

> `uv.lock` 是 gitignored（repo 慣例，見 `.gitignore`），所以「這次部署實際解出哪些版本」
> 只存在於部署機器上，不會自動留痕。**首次部署完成後，把 `uv sync` 實際解出的版本貼在這裡**，
> 之後任何一次重新 `uv sync`（例如換機器、換磁碟）都能對照，避免依賴漂移是排查困難的第一步。

**2026-07-19 首次實機部署（Lightsail，Ubuntu 22.04.5）實測版本：**

```
fastapi                  0.139.2
uvicorn                  0.51.0
eth-account               0.13.7
hyperliquid-python-sdk    0.24.0
pydantic                  2.13.4
Node.js                   20（LTS，見 §4.1）
nginx                     1.18（Ubuntu 22.04 內建，非 1.25+——見 deploy/nginx-filet.conf 的
                          http2 語法註解，這是 Bug 2 的根因）
```
