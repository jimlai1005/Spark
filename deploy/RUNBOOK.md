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
| `FILET_GIT_REMOTE_PLACEHOLDER` | repo 取得方式——見 §4.2（**本 repo 目前無 git remote**，需先決定推送目標或改用 rsync） | 使用者決定，見附錄 A |

---

## 1. 系統準備

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
sudo dpkg-reconfigure -plow unattended-upgrades
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

uv python install 3.11
uv python list | grep 3.11       # 驗收：3.11.x 已安裝
```

### 3.2 repo 部署

> ⚠️ **本 repo 目前沒有設定 git remote**（`git remote -v` 空）。以下二選一，`FILET_GIT_REMOTE_PLACEHOLDER`
> 依你的選擇填入——這是附錄 A 的決策點之一，先確定才能執行本節。

**選項 A（有私有 git host，例如 GitHub private repo）：**

```bash
sudo mkdir -p /opt/filet && sudo chown "$USER":"$USER" /opt/filet   # 暫時放寬給部署帳號寫
git clone FILET_GIT_REMOTE_PLACEHOLDER /opt/filet/spark
```

**選項 B（無 remote，直接從本機 push 到伺服器的 bare repo）：**

```bash
# 在 Lightsail 上：
sudo mkdir -p /opt/filet && sudo chown "$USER":"$USER" /opt/filet
git init --bare /opt/filet/spark.git

# 在本機 (/Users/jim/projects/spark)：
git remote add lightsail ssh://ubuntu@FILET_LIGHTSAIL_IP_PLACEHOLDER/opt/filet/spark.git
git push lightsail feat/m2-publicapi:main   # 或部署當下決定的正式分支

# 回 Lightsail：
git clone /opt/filet/spark.git /opt/filet/spark
```

```bash
cd /opt/filet/spark
uv sync   # 從 pyproject.toml 解析、產生 .venv/ 與 uv.lock（uv.lock 是 gitignored，僅存在此機）
uv run python -c "import spark; print('spark import OK')"   # 驗收

# 首次部署後，把解出的版本記進本文件（見文末「附錄 B」），避免下次部署解出不同版本造成漂移。
sudo chown -R root:root /opt/filet/spark
sudo chmod -R go-w /opt/filet/spark   # 確保 group/other 無寫入權（唯讀給三個 service user）
```

---

## 4. Node 20 LTS + 前端 build + dashboard 部署

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
# （`scripts/filet_activate.py <account_id>`），只在有帳號完成 onboarding+verify 後才拉起。
# 本 runbook 只負責讓「拉起 follower 的能力」就緒，不代表部署當下就有帳號要拉。
```

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
# 若還沒接前端，可直接用 python 對 socket 送一筆 generate 測試（以 filet-engine 身分執行）：
sudo -u filet-engine /opt/filet/spark/.venv/bin/python - <<'PY'
import socket, json
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect("/run/filet/keysvc.sock")
s.sendall((json.dumps({"op": "generate", "account_id": "deploytest0000000000000000000000000000"}) + "\n").encode())
print(s.recv(4096))
PY
# 預期：{"ok": true, "agent_address": "0x...", ...} 之類的 JSON（確認 keysvc 正常運作）

sudo -u filet-api ls /etc/filet/keys
# 預期：ls: cannot open directory '/etc/filet/keys': Permission denied

sudo -u filet-api cat /etc/filet/keys/deploytest0000000000000000000000000000/agent.key
# 預期：cat: /etc/filet/keys/deploytest0000000000000000000000000000/agent.key: Permission denied

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
4. **repo 取得方式（§3.2 選項 A vs B）**：本 repo 目前沒有設定任何 git remote
   （`git remote -v` 空輸出）。部署前要嘛先把 repo push 到一個私有 git host（選項 A，較常規、
   之後拉版方便），要嘛用裸 repo + push 到伺服器（選項 B，不依賴第三方 host）。這是架構層
   的選擇，不是本次任務範圍內能替你定案的事。
5. **§7 環境變數檔改用 `EnvironmentFile`**：屬於「值得做但非本次紅線允許動手」的加固——
   是否要在部署日順手做，由你決定（步驟已寫在 §7）。
6. **certbot 續期失敗的告警管道**：本文件只確認 `certbot.timer` 存在，沒有另外接告警
   （例如憑證到期前 N 天發 Telegram）。若要接，需決定告警管道與門檻，超出本次三檔案範圍。

## 附錄 B：首次部署後記錄（部署日填寫）

> `uv.lock` 是 gitignored（repo 慣例，見 `.gitignore`），所以「這次部署實際解出哪些版本」
> 只存在於部署機器上，不會自動留痕。**首次部署完成後，把 `uv sync` 實際解出的版本貼在這裡**，
> 之後任何一次重新 `uv sync`（例如換機器、換磁碟）都能對照，避免依賴漂移是排查困難的第一步。

```
# TODO（部署日填）：uv run python -c "import importlib.metadata as m; [print(d.name, d.version) for d in m.distributions()]" 的輸出，
# 或至少貼 hyperliquid-python-sdk / fastapi / uvicorn / eth-account 幾個關鍵套件版本。
```
