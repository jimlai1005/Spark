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
| `REPLACE_WITH_NETWORK` | ⭐ `filet-api.service` 的 `FILET_API_NETWORK`：`testnet` 或 `mainnet`。**這台機器是測試機就填 `testnet`**——填錯＝整台機器對主網真實資金下單。2026-07-19 起改為佔位符（原硬編 `mainnet`），見 §5.3 | 依機器定位決定，見 §5.3 |
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
| `/opt/filet/state/<account_id>` | `filet-engine:filet-engine` | `700` | 由 `filet-follower@.service` 的 `ReadWritePaths` 隱含要求，首次啟動前手動 `mkdir -p` | 單一 follower 引擎狀態根（kill-switch 不連坐）。⭐ **維持 700，不要為了健康面板放寬**——面板改讀引擎發布的心跳，見 §5.6 |
| `/var/lib/filet-exchange` ＋ `…/engine` | 見 §5.5.1（**owner/group 對調的兩層**） | `0750` | 手動 mkdir（§5.5.1） | 兩個單向通道：根層 API 寫／引擎讀（客戶簽章記錄），`engine/` 引擎寫／API 讀（健康心跳） |
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

> ⚠️ **exclude 清單同時是機密邊界，不只是體積優化**（2026-07-19 審查發現）。
> 前四項擋的是 `.env`／`.env.*`／`*.key`／`.git`。原清單只排除「大／可再生」的目錄，
> **沒有任何一項是機密**——而 `.gitignore` 明列 `.env` 與 `.env.*`，代表這類檔案在
> 開發者的工作樹裡是預期會存在的。一旦存在，舊清單就會把它推上正式機（撞紅線 2：
> 私鑰不得離開本機）。這是**潛在風險而非已發生的外洩**：撰寫本註記時工作樹內
> 恰好沒有這些檔案。新增 exclude 時不要只想「這個大不大」，要想「這個外洩會怎樣」。

> ⚠️ **`uv.lock` 必須在兩段 rsync 都 exclude**（2026-07-20 審查發現，**行為與敘述不符**）。
> 本節下方寫著「`uv.lock` 是 gitignored，僅存在此機（＝伺服器）」，但它**沒有出現在
> 任何一段的 exclude 清單裡**——所以每次部署其實都是**本機的 lock 蓋掉伺服器的**。
> 敘述說 A、行為是 B，這個不一致本身就是問題：本次部署無害（依賴沒變），但只要本機
> 曾在不同依賴狀態下解過 lock，下一次部署就會**靜默改動正式機的依賴版本**，而
> `uv sync` 會忠實地照那份 lock 裝——沒有任何錯誤訊息。
>
> 這台機器的本機與伺服器**本來就不是同一個解析環境**：本機是 macOS ＋ Python 3.14，
> 伺服器是 Linux ＋ Python 3.11（§3.1 釘死）。lock 由伺服器自己解、留在伺服器，是這份
> 文件一貫的模型（見下方 `uv sync` 那行、重新部署前的 `chown` 修正、以及附錄 B 記錄
> 解出的版本以便偵測漂移）。修法就是把敘述缺的那一格補上，維持該模型。
>
> **兩段都要加**：第一段少了它 → 本機的 lock 進 `/tmp/spark-sync/`；第二段少了它 →
> `--delete` 會把伺服器上那份**刪掉**（`--exclude` 同時保護目的端的檔案不被 `--delete`
> 清除）。只加一段等於換一種方式弄丟它。

```bash
# 在本機執行 (/Users/jim/projects/spark)：
rsync -az --delete \
  --exclude .env --exclude '.env.*' --exclude '*.key' --exclude .git \
  --exclude node_modules --exclude .venv --exclude .next \
  --exclude __pycache__ --exclude var --exclude .pytest_cache \
  --exclude uv.lock \
  -e "ssh -i <金鑰路徑>" /Users/jim/projects/spark/ ubuntu@FILET_LIGHTSAIL_IP_PLACEHOLDER:/tmp/spark-sync/
```

```bash
# 回 Lightsail：把暫存目錄搬到正式路徑並修正 owner
sudo mkdir -p /opt/filet
sudo rsync -a --delete \
  --exclude .venv --exclude web/node_modules --exclude web/.next --exclude var \
  --exclude uv.lock \
  /tmp/spark-sync/ /opt/filet/spark/
rm -rf /tmp/spark-sync   # 清掉暫存，不留在 /tmp

# 驗收：伺服器上的 uv.lock 沒有被本次部署動到（mtime 應是上一次在**這台伺服器**跑
# uv sync 的時間，不是剛才那一秒）。首次部署時它還不存在，這行會 No such file——正常。
ls -l --time-style=long-iso /opt/filet/spark/uv.lock 2>/dev/null \
  || echo "（首次部署：uv.lock 尚未產生，下面的 uv sync 會建它）"
```

> ⭐ **伺服器上「沒有 git repo」是刻意的，不是缺陷**（2026-07-19 實機重新部署發現）。
> 第一段 rsync 排除了 `.git`，所以 `/tmp/spark-sync/` 裡沒有 `.git`；第二段的 `--delete`
> 因此會把伺服器上原有的 `/opt/filet/spark/.git` 刪掉——實機已確認它現在不存在。
>
> **不要用「第二段也加 `--exclude .git`」來修**：那會在伺服器留下一份**永遠不再更新**的
> 舊 `.git`，`git log` 會顯示錯誤的版本，比沒有更危險（讓人以為部署的是別的 commit）。
> 正式機不該有 git repo 本來就更好：攻擊面更小、不會有人在上面誤跑 git 指令改動線上程式碼。
>
> 代價是**版本可追溯性**與**回滾**都不能再依賴伺服器上的 git：
> 前者由下面的 `DEPLOYED_VERSION` 標記檔接手，後者見**重寫過的 §9.3**（回滾改由
> 操作者本機驅動）。

```bash
cd /opt/filet/spark

# ⚠️ 重新部署才需要這一段（2026-07-19 實機重新部署發現）：首次部署末尾的
# `chown -R root:root`（本節最後一行）會讓 .venv 與 uv.lock 變成 root 所有，
# 之後以 ubuntu 身分跑 uv sync 一律 Permission denied。首次部署時這兩個路徑
# 還不存在，`|| true` 讓這行在首次部署也能無害地跑過去。
sudo chown -R ubuntu:ubuntu /opt/filet/spark/.venv /opt/filet/spark/uv.lock 2>/dev/null || true

uv sync   # 從 pyproject.toml 解析、產生 .venv/ 與 uv.lock（uv.lock 是 gitignored，僅存在此機）
uv run python -c "import spark; print('spark import OK')"   # 驗收

# 驗證 venv 內零 /home 參照（確認 §3.1 的 UV_PYTHON_INSTALL_DIR 修正生效）：
readlink -f .venv/bin/python    # 應指向 /opt/filet/python/...，不得出現 /home

# 首次部署後，把解出的版本記進本文件（見文末「附錄 B」），避免下次部署解出不同版本造成漂移。

# ⭐ 還原成 root 所有——這一步不是收尾潔癖，是安全邊界：三個 service user 跑的就是
# 這棵樹底下的程式碼，留成 ubuntu 所有＝任何拿到 ubuntu 的人都能改服務執行的程式碼
# （不需要 sudo、不會留下 sudo 稽核紀錄）。重新部署跑完 uv sync 後務必回到這一行。
sudo chown -R root:root /opt/filet/spark
sudo chmod -R go-w /opt/filet/spark   # 確保 group/other 無寫入權（唯讀給三個 service user）

# 驗收：venv 與 repo 根都已回到 root 所有（重新部署時最容易漏的一步）
sudo ls -ld /opt/filet/spark /opt/filet/spark/.venv   # 預期：兩行都是 root root
```

#### ⭐ 最後一步：寫版本標記檔 `DEPLOYED_VERSION`（2026-07-19 實機重新部署發現）

伺服器上沒有 `.git`（見本節上方說明），所以「這台機器現在跑的是哪一版」**只有這個檔知道**。
§9.3 的回滾要靠它判斷「現在是哪一版」，漏寫這一步等於回滾時無從下手。

**寫入方式：本機算值、ssh 寫入（部署完成之後）**——不是先寫進本機工作樹再讓 rsync 帶上去。
理由有三：(1) 標記檔要描述「**實際落地**的東西」，寫在 rsync＋`chown` 都成功之後，才不會
出現「rsync 中途失敗、標記檔卻宣稱新版本」；(2) 不會在本機工作樹留一個未追蹤的檔案
（也就不必為它加 `.gitignore`，更不會讓 `git describe --dirty` 把自己算進去）；
(3) 上面第二段 rsync 的 `--delete` 每次都會先刪掉舊的標記檔（來源 `/tmp/spark-sync/` 裡沒有它），
所以失敗情境是「**標記檔不見了**」這種一眼看得出的大聲失敗，而不是留著一份騙人的舊版本號。

```bash
# 在本機執行 (/Users/jim/projects/spark)，<金鑰路徑> 與上面 rsync 用的同一把：
printf 'commit=%s\ndescribe=%s\ndeployed_at_utc=%s\ndeployed_by=%s@%s\n' \
  "$(git rev-parse HEAD)" \
  "$(git describe --always --dirty)" \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  "$(whoami)" "$(hostname -s)" \
| ssh -i <金鑰路徑> ubuntu@FILET_LIGHTSAIL_IP_PLACEHOLDER \
    'sudo tee /opt/filet/spark/DEPLOYED_VERSION >/dev/null \
     && sudo chown root:root /opt/filet/spark/DEPLOYED_VERSION \
     && sudo chmod 644 /opt/filet/spark/DEPLOYED_VERSION \
     && cat /opt/filet/spark/DEPLOYED_VERSION'
```

預期輸出（`cat` 直接把寫進去的四行印回來，就是驗收）：

```
commit=696e3cfa84152a062eccbfa2a634316ea40c27c6
describe=696e3cf
deployed_at_utc=2026-07-19T13:51:36Z
deployed_by=jim@Jims-MacBook-Pro
```

> ⚠️ `describe` 出現 `-dirty` 字尾＝**推上去的是未 commit 的工作樹**。測試機可接受；
> 正式機看到 `-dirty` 代表這一版在 git 裡不存在，**無法回滾到它**（§9.3 的回滾以 commit
> 為單位）。正式機部署前先把工作樹 commit 乾淨。

> 之後每次重新部署（拉新版本）：重跑上面兩段 rsync（`--delete` 會清掉伺服器上已刪除的
> 檔案，保持與本機工作樹一致），再視情況重跑 `uv sync`（只在依賴有變動時需要）。
>
> 🛑 **但重新部署不是「把本文件從頭跑一遍」**（2026-07-19 實機重新部署發現）：§5.1 的
> `cp` 會靜默清掉 §5.2／§5.3 填進 `/etc/systemd/system/` 的實際值。動 systemd unit 之前
> **先讀 §5.1a**，那一節列出重新部署專屬的備份／還原步驟與其餘會踩到的點。
>
> ⚠️ 兩段 rsync 的 `--exclude` 清單也是重新部署的承重點：少了 `--exclude var`，
> `--delete` 會連 §5.5 的 leader 白名單（`var/filet/leaders.json`）一起刪掉。

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

> ⚠️ **build 前一定要先把 `web/` 交還給 ubuntu**（2026-07-19 實機重新部署發現）。
> §3.2 結尾的 `chown -R root:root /opt/filet/spark` 是**遞迴**的，`web/` 也被收成 root
> 所有；重新部署時 `web/node_modules`、`web/.next` 更是早就是 root 的了。以 ubuntu
> 身分跑 `npm ci`／`npm run build` 會直接 Permission denied（`npm ci` 要重建
> `node_modules`、build 要寫 `.next`，兩者都在這棵子樹底下）。

```bash
# build 前：把整個 web/ 交給 ubuntu（涵蓋 node_modules 與 .next 兩個實際寫入點）
sudo chown -R ubuntu:ubuntu /opt/filet/spark/web

cd /opt/filet/spark/web
npm ci    # 需要已 commit 的 package-lock.json；若尚無 lock 檔，改用 npm install 並在
          # 部署後把產生的 package-lock.json commit 回 repo（鎖版本，避免下次部署解出不同版本）
npm run build   # 驗收：`.next/` 產出，無 build error
```

```bash
# ⭐ build 後**必須**還原成 root——這是 §4.4「前端與 filet-api 是不同信任域」的
# 承重點之一：bundle 留成 ubuntu 所有，等於任何拿到 ubuntu 的人都能改前端執行的
# 程式碼（含 §4.4 那道「待簽授權對象 == 使用者所選 leader」的前端防線），
# 不需要 sudo、不留 sudo 稽核紀錄。§4.4 的驗收 1 正是在檢查這一格。
sudo chown -R root:root /opt/filet/spark/web
sudo chmod -R go-w /opt/filet/spark/web

# 驗收：owner 已回到 root（重新部署時最容易漏的一步）
sudo ls -ld /opt/filet/spark/web /opt/filet/spark/web/.next   # 預期：兩行都是 root root
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

> 🛑 **這台機器已經部署過的話：先做完 §5.1a 的步驟 1（備份），再回來跑下面這段。**
>（2026-07-19 實機重新部署發現）下面每一行都會**覆蓋**掉 §5.2／§5.3 曾經填進
> `/etc/systemd/system/` 的實際值，而且是**靜默**的——沒有任何錯誤輸出，服務照樣
> `active`，功能卻已經壞掉。跑完這段之後**不要 start 服務**，先回 §5.1a 步驟 3 還原。
> 首次安裝（`/etc/systemd/system/` 底下還沒有 `filet-*.service`）則直接往下跑。

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

### 5.1a ⭐⭐ 重新部署既有機器（**不是首次安裝就必讀這節**）

> 編號用 `5.1a` 是刻意的插入步驟——不重編後面的章節號（避免既有交叉引用失效）。
> **本節新增於 2026-07-19（實機重新部署發現）**：原文件只有首次安裝路徑，照著重跑
> 一次會把機器打回未設定狀態。

> 🛑🛑 **本次升級新增一個必填變數：`FILET_LEADERS_PATH`（2026-07-20）**
>
> 現行伺服器上的 `/etc/systemd/system/filet-api.service` **沒有這一行**（它以前靠程式
> 的隱含預設運作）。預設值已被移除，所以**升級後 filet-api 會拒絕啟動**，錯誤訊息是
> `缺少環境變數: FILET_LEADERS_PATH`。這是刻意的 fail-closed，不是故障——理由見 §5.5。
>
> **部署前**先把這一行加進 `/etc/systemd/system/filet-api.service`（repo 版的
> `deploy/filet-api.service` 已內建，跑完下面步驟 2 的 `cp` 就會帶進來；但若你依
> **步驟 0** 判定要跳過 `cp`，就必須手動加）：
>
> ```ini
> Environment=FILET_LEADERS_PATH=/opt/filet/spark/var/filet/leaders.json
> ```
>
> 值必須與 `filet-follower@`、`filet-leaderboard`、`filet-perf-series` 三個 unit 逐字元
> 相同（那三個本來就有這一行）。四邊一致的驗收指令見 **§5.7 驗收 4**。
> 加完 `daemon-reload` ＋ `restart filet-api`，再跑一次 §5.4 的狀態確認。

**問題**：§5.2／§5.3 用 `systemctl edit --full` 把實際值寫進
`/etc/systemd/system/filet-*.service`，而 §5.1 的 `cp` 覆蓋的**正是同一個路徑**。
所以在既有機器上重跑 §5.1，會靜默清掉全部 6 個已填入的值：

| unit | 被清掉的值 | 後果 |
|---|---|---|
| `filet-api.service` | `FILET_API_NETWORK` | ⭐ 變回 `REPLACE_WITH_NETWORK` → **API 拒絕啟動**（唯一會大聲失敗的一個） |
| `filet-api.service` | `FILET_BUILDER_ADDR` | builder 收益歸零／下單帶錯 builder |
| `filet-api.service` | `FILET_SIWE_DOMAIN`、`FILET_SIWE_URI` | SIWE 登入全數失敗（domain 對不上） |
| `filet-api.service` | `FILET_ADMIN_ADDRESSES` | 沒有人是 admin，後台進不去 |
| `filet-keysvc.service` | `FILET_KEYSVC_ALLOWED_UIDS`（§5.2） | SO_PEERCRED 白名單失效 → filet-api 呼不到 key-service |

`FILET_API_NETWORK` 起不來反而是**最幸運**的一個：其餘五個都是「服務照樣 active，
功能靜默壞掉」。所以下面的還原步驟一條都不能跳。

#### 步驟 0：先 diff repo 版與 `/etc` 現行版，決定要不要跑 §5.1 的 `cp`

<!-- 2026-07-20 新增：本次部署實際採用且證明安全的作法 -->

§5.1 的 `cp` ＋ §5.1a 步驟 3 的還原，合起來是「把 6 個實際值換成佔位符、再還原回去」。
**若 repo 版與現行版的差異只是那幾個已填值**，整段就是純風險零收益（中途中斷、還原
迴圈少還原一個、`sed` 撞到特殊字元——每一種都會讓服務靜默壞掉），此時**直接跳過
§5.1 與步驟 3**，只做 `daemon-reload`。

```bash
# 逐一 diff（unit 沒有真正的改動時，輸出應只有那幾行 REPLACE_WITH / 已填值的差異）
for U in filet-keysvc filet-api filet-dashboard filet-follower@; do
  echo "=== $U ==="
  sudo diff -u "/etc/systemd/system/${U}.service" "/opt/filet/spark/deploy/${U}.service"
done
```

判讀：
- **差異只有已填值 vs `REPLACE_WITH_*` 佔位符** → 跳過 §5.1 的 `cp` 與步驟 3，
  這次部署不動 unit 檔。
- **有其他差異**（多／少一行 `Environment=`、`ExecStart`／`ReadWritePaths`／沙箱選項變了）
  → 那些正是本次升級要帶上去的東西，照常跑 §5.1 ＋ 步驟 1／3。
  ⚠️ 本次（2026-07-20）就屬於這一類：`filet-api.service` 多了 `FILET_LEADERS_PATH` 一行。
- 拿不準 → 照常跑完整流程（步驟 1 的備份先做）。多做一次是可回復的，跳錯不是。

#### 步驟 1：覆蓋 unit 之前先備份

```bash
# 帶日期的備份目錄（同一天重跑多次也不互相覆蓋，故帶時分秒）
BACKUP_DIR=/etc/filet/unit-backups/$(date -u +%Y-%m-%d-%H%M%S)
sudo mkdir -p "$BACKUP_DIR"
sudo cp -a /etc/systemd/system/filet-*.service "$BACKUP_DIR"/
sudo chmod 700 /etc/filet/unit-backups "$BACKUP_DIR"   # 內含 admin／builder 位址，不給 other

echo "$BACKUP_DIR"                     # 記下這個路徑，步驟 3 要用
sudo ls -1 "$BACKUP_DIR"               # 驗收：至少有 filet-api.service 與 filet-keysvc.service
```

> ⚠️ `$BACKUP_DIR` 只是目前 shell 的變數，登出就沒了。中途登出重進的話，這樣找回最新那個：
>
> ```bash
> BACKUP_DIR=/etc/filet/unit-backups/$(sudo ls -1 /etc/filet/unit-backups | sort | tail -1)
> sudo ls -1 "$BACKUP_DIR"   # 驗收：確認是你要的那份備份
> ```

#### 步驟 2：照 §5.1 覆蓋 unit 檔

回 §5.1 跑那段 `cp` ＋ `daemon-reload`。**跑完先不要 start 任何服務**，先做步驟 3。

#### 步驟 3：把 6 個值從備份還原回去

> ⭐ 下面的迴圈**刻意不印出任何值**——只印變數名。備份裡含 builder 錢包位址與
> admin 位址，不該出現在部署日誌／終端捲動紀錄／截圖裡。

```bash
for PAIR in \
  filet-api.service:FILET_API_NETWORK \
  filet-api.service:FILET_BUILDER_ADDR \
  filet-api.service:FILET_SIWE_DOMAIN \
  filet-api.service:FILET_SIWE_URI \
  filet-api.service:FILET_ADMIN_ADDRESSES \
  filet-keysvc.service:FILET_KEYSVC_ALLOWED_UIDS
do
  UNIT="${PAIR%%:*}"; VAR="${PAIR##*:}"
  LINE="$(sudo grep -m1 -E "^Environment=${VAR}=" "$BACKUP_DIR/$UNIT" || true)"
  if [ -z "$LINE" ]; then
    echo "★ $UNIT: $VAR 在備份中找不到——手動處理（舊版 unit 可能還沒有這個變數）"
    continue
  fi
  case "$LINE" in
    *REPLACE_WITH*)
      echo "★ $UNIT: $VAR 備份裡仍是佔位符——舊機器本來就沒填好，依 §5.2／§5.3 手動填"
      continue ;;
  esac
  # 用 | 當分隔符：值可能含 /（SIWE URI），但這 6 個值（網路名／hex 位址／網域／
  # https URI／逗號分隔位址／數字 uid）都不含 | 或 &（& 在 sed 取代字串裡有特殊意義）。
  # 未來若有值可能含這兩個字元，這一行要改成逐字元轉義的寫法。
  sudo sed -i "s|^Environment=${VAR}=.*|${LINE}|" "/etc/systemd/system/$UNIT"
  echo "restored: $UNIT $VAR"          # 只印變數名，不印值
done
```

預期輸出：6 行 `restored: ...`，沒有任何 `★`。出現 `★` 就照該行提示手動補，
**補完再往下**——這一節唯一的失敗模式就是「以為還原了，其實少了一個」。

#### 步驟 4：daemon-reload 與驗證

```bash
sudo systemctl daemon-reload

# 驗收 1：兩個 unit 都不再有殘留佔位符（只印檔名與計數，不印值）
grep -c 'REPLACE_WITH' /etc/systemd/system/filet-api.service \
                       /etc/systemd/system/filet-keysvc.service
# 預期：兩行都以 :0 結尾

# 驗收 2：network 確實不是佔位符，且與這台機器的定位相符
#（此值非機密，刻意印出來看——它是填錯代價最高的一個，見 §5.3）
grep -E '^Environment=FILET_API_NETWORK=' /etc/systemd/system/filet-api.service
# 預期：testnet（測試機）或 mainnet（正式機）；出現 REPLACE_WITH_NETWORK 代表步驟 3 沒生效

# 驗收 3：systemd 實際載入的值與檔案一致（daemon-reload 漏跑就會不一致）
#（只比對變數名是否齊全，不印值；--value 的理由見 §5.5.2 驗收 2 的方框）
systemctl show filet-api.service -p Environment --value | tr ' ' '\n' \
  | grep -oE 'FILET_(API_NETWORK|BUILDER_ADDR|SIWE_DOMAIN|SIWE_URI|ADMIN_ADDRESSES)' | sort
# 預期：五個變數名各一行，一個都不缺
```

#### 步驟 5：其餘重新部署會踩到的點（逐一確認）

- **§3.2 `uv sync` 與 §4.2 `npm ci`／`npm run build` 會 Permission denied**——首次部署
  末尾已把 `/opt/filet/spark` chown 成 `root:root`。兩節各自已補上「build 前 chown 給
  ubuntu、build 後還原 root」的步驟，照該節做。
- **`var/` 目錄（leaders.json、manifest）靠 rsync 的 `--exclude var` 保護**——§3.2 兩段
  rsync 的 exclude 清單少一個，`--delete` 就會連白名單一起刪掉。跑之前確認 exclude
  清單完整（`.venv`／`web/node_modules`／`web/.next`／`var`／`uv.lock`）。
  <!-- 2026-07-20：uv.lock 補進本清單，理由見 §3.2 上方的方框（原本漏列，導致每次
       部署都是本機的 lock 蓋掉伺服器的，與 §3.2 的敘述矛盾）。 -->
- **本次升級（2026-07-20）多一個必填變數 `FILET_LEADERS_PATH`**——見本節開頭的方框，
  不補會讓 filet-api 拒絕啟動。
- **`/var/lib/filet-exchange` 與 `/etc/filet/keys` 不受重新部署影響**（不在 repo 路徑下），
  但仍值得跑一次 §5.5.1 的驗收確認權限沒被別的操作動過。
- **§6 的 nginx 設定同樣不要重跑**——`cp nginx-filet.conf` 會蓋掉網域代換**與 certbot
  寫進去的 `ssl_certificate` 路徑／HTTP→HTTPS redirect**，症狀是 `nginx -t` 直接失敗
  （憑證路徑變回 `FILET_DOMAIN_PLACEHOLDER`）。反代設定沒改就整節跳過；真的改了就
  先 `sudo cp -a /etc/nginx/sites-available/filet "$BACKUP_DIR"/` 再動，重跑後補做
  `sed` 代換與 `sudo certbot --nginx -d <網域>`（certbot 會重新改寫，不需重新簽發）。
- **§5.7 的兩個 timer unit 可以安全重跑**（那四個檔沒有任何佔位符），但重跑後要記得
  `daemon-reload`。
- **重啟順序照 §9.2**，不要在這裡逐一 `restart`。

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

`filet-api.service` 還有五個佔位符要填（同樣用 `systemctl edit --full filet-api.service`）：

| 變數 | 填入值 |
|---|---|
| `FILET_API_NETWORK` | ⭐ `testnet` 或 `mainnet`。**測試機必須填 `testnet`**——填成 `mainnet` 會讓這台機器連上主網、對真實資金下單。此欄 2026-07-19 起才成為佔位符（原本硬編 `mainnet`），見 `deploy/filet-api.service` 該行註解 |
| `FILET_BUILDER_ADDR` | `FILET_BUILDER_ADDR_PLACEHOLDER`（附錄 A） |
| `FILET_SIWE_DOMAIN` | `FILET_DOMAIN_PLACEHOLDER` 實際網域 |
| `FILET_SIWE_URI` | `https://FILET_DOMAIN_PLACEHOLDER` |
| `FILET_ADMIN_ADDRESSES` | `FILET_ADMIN_ADDRESSES_PLACEHOLDER`（附錄 A，逗號分隔） |

> `FILET_API_NETWORK` 一列補於 2026-07-19（**實機重新部署發現**）：它已改為佔位符，
> 但本表漏列，照本表填完會留下 `REPLACE_WITH_NETWORK`，API 直接拒絕啟動
> （`config.from_env` 的 `network not in API_URLS`，fail-closed）。

> 🛑 **升級到 2026-07-20 之後的版本時，`filet-api.service` 多一個必填變數
> `FILET_LEADERS_PATH`**（不是佔位符，repo 版已內建實際值，故不在上表）。
> 舊機器的 `/etc/systemd/system/filet-api.service` 沒有這一行，**升級後 filet-api 會
> 拒絕啟動**（`缺少環境變數: FILET_LEADERS_PATH`）——刻意的 fail-closed，處理方式與
> 完整理由見 §5.1a 開頭的方框與 §5.5。四個 unit 值一致的驗收見 §5.7 驗收 4。

```bash
sudo systemctl daemon-reload
grep -E 'REPLACE_WITH|PLACEHOLDER' /etc/systemd/system/filet-api.service
# 驗收：改完後這個 grep 應該零輸出（找不到任何殘留佔位符）

# ⭐ network 額外單獨確認一次（它是唯一「填錯不會報錯、但會連上主網」的欄位）
grep -E '^Environment=FILET_API_NETWORK=' /etc/systemd/system/filet-api.service
# 預期：testnet（測試機）或 mainnet（正式機）——與這台機器的定位相符才往下走
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

> ⭐⭐⭐ **路徑本身是必填、無預設、必須絕對路徑**（2026-07-20，同一失敗模式的第三次）
>
> `FILET_LEADERS_PATH` 有**五個**消費端，各自推導一次路徑：
>
> | 消費端 | 讀它決定什麼 | 宣告在哪 |
> |---|---|---|
> | `filet-api` | 客戶**能選誰**（目錄頁／選 leader） | `filet-api.service` |
> | `filet-follower@` | 已在跟的人**還能不能繼續**（每輪二次驗證） | `filet-follower@.service` |
> | `filet-leaderboard` | 每日快照**抓誰** | `filet-leaderboard.service` |
> | `filet-perf-series` | 12 小時序列**抓誰**（無法回填） | `filet-perf-series.service` |
> | `filet_activate` CLI | 管理端**核可誰**（硬閘） | `--leaders` 或執行時的 env |
>
> 前兩個實例是 `FILET_EXCHANGE_DIR`（§5.5.1）與 `FILET_STATE_BASE`（§5.5.2），修法相同：
> **拿掉隱含預設，漏設的那一邊直接起不來**。2026-07-20 之前 `filet-api` 沒有宣告這個
> 變數卻能正確運作——因為程式的預設值錨在 repo 根，而它的 `WorkingDirectory` 恰好也是
> repo 根。**那是巧合不是強制**：白名單一搬家（或改 `WorkingDirectory`），API 與引擎就
>讀不同的白名單，而危險方向是 **fail-open**——管理端在引擎那份撤銷了一個 leader，
> 目錄頁仍列著他、客戶仍選得到、activate 仍放行。現在漏設一律拒絕啟動。
>
> 相對路徑同樣被拒：引擎的 CWD 由 systemd 釘死，管理端跑 CLI 的 CWD 是他當下所在——
> 同一個相對路徑在兩邊指向不同的檔，症狀一樣是下架無聲失效。
>
> 四個 unit 值一致的驗收指令見 **§5.7 驗收 4**；activate CLI 的用法見 **§5.6**。

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

#### 成本熔斷器的狀態檔 `cost_breaker.json`（在哪、什麼時候清）

換手率熔斷器（成交名目 ÷ perp 權益，滾動 24h）唯一的持久狀態，**刻意不是帳本**：
只存「觸發事件的時間戳」與「上一輪是否處於觸發狀態」。換手率本身每輪從交易所的
fills 完全重算，所以這個檔遺失**不影響主閘**（停開新倉），只會讓累犯計數歸零。

| 項目 | 內容 |
|---|---|
| 路徑 | `/opt/filet/state/<account_id>/var/copytrade/cost_breaker.json` |
| 自動清除 | **只有 `killswitch.trip()` 會清**（`trip` 內呼叫 `reset_log`，與 `reset_samples` 同一個「已由人接手」的重置點）。人工 re-arm 刪 ARM 檔**不會**清它——清除發生在 trip 當下，不是 re-arm 當下 |
| 自然出窗 | 觸發記錄逾 24h 會在下一輪 `_prune` 時自動丟棄（不需要人工介入） |

```bash
# 看某個 follower 目前的觸發歷史（breaches = 各次觸發的 epoch 秒、active = 上輪是否觸發中）
sudo cat /opt/filet/state/<account_id>/var/copytrade/cost_breaker.json
```

> ⚠️ **`flatten_on_breach=False` 時會停滿 24h，而且沒有 ARM 檔可刪**——這是這一節
> 存在的理由。成本熔斷累犯升級（滾動 24h 內觸發達 `cost_breach_escalate_count` 次）
> 會**比照回撤路徑尊重 `flatten_on_breach`**：關掉時引擎**不 trip**，於是
> **不強制平倉、不鎖死交易、也不清 `cost_breaker.json`**，但每一輪仍然 `return`
> 在升級判定處（停止所有交易動作）。結果是引擎會**持續停到最舊的那筆觸發記錄
> 自然出窗為止（最長 24h）**，而操作者手上**沒有 ARM 檔可以刪**——`flatten_on_breach=True`
> 時那條「刪 ARM 檔 re-arm」的復原路徑在這裡不存在。
>
> 此情境下唯一的留痕是 `cost_escalate_no_flatten` 這則 critical 告警（明載
> 「既有部位仍在市場上，請人工處置」）。若確認已人工處置、要讓引擎**立刻**恢復：
>
> ```bash
> # 停服務 → 刪狀態檔 → 起服務（刪檔等同人工 re-arm；引擎不會在執行中重讀被刪的檔）
> sudo systemctl stop filet-follower@<account_id>
> sudo rm -f /opt/filet/state/<account_id>/var/copytrade/cost_breaker.json
> sudo systemctl start filet-follower@<account_id>
> ```
>
> ⚠️ 刪檔前先確認**造成觸發的原因已經處理掉**（leader 是不是還在高頻對敲？）。
> 清掉的是「我方的判定歷史」，不是磨損本身——原因還在的話下一輪就會再度觸發，
> 而累犯計數已被你歸零，等於把保護往後推遲了一輪。

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
| `/var/lib/filet-exchange` | **`filet-api:filet-engine`** | **`0750`** | 手動 mkdir（本節） | ⭐ **api→engine** 通道：客戶簽章的共享記錄 `leader_changes.json`（換 leader）＋ `capital_settings.json`（資金設定）。owner 寫、group 讀、other 無 |
| `/var/lib/filet-exchange/engine/` | **`filet-engine:filet-api`** | **`0750`** | 手動 mkdir（本節） | ⭐ **engine→api** 通道（**owner/group 剛好對調**）：引擎發布的健康心跳 `engine/health/<account_id>.json`。引擎寫、API 讀 |
| `/var/lib/filet-exchange/engine/health/` | **`filet-engine:filet-api`** | **`0750`** | 手動 mkdir（本節，2026-07-19 補） | 心跳檔實際落點。⭐ **不要倚賴引擎自建**——它建得出來，但會是 `filet-engine:filet-engine` ＋ umask 決定的 mode（通常 0755，比設計意圖寬），理由見本節下方 |

> 兩份客戶簽章記錄**刻意分開兩個檔**（不是同一個檔多一個欄位）：讀者是兩個獨立的
> 套用器，共用一個檔會讓其中一方的格式問題連坐另一方——而這兩件事各自都能造成
> 資金損失，不該共命運（`src/spark/filet/capital_settings.py` 檔頭）。兩者同目錄、
> 同權限拓撲，所以本節的建立與驗收步驟**一次涵蓋兩者**。

**⭐⭐ 兩個單向通道，沒有任何雙向可寫的路徑**（`src/spark/filet/engine_health.py` 檔頭）：

```
/var/lib/filet-exchange/              filet-api:filet-engine  0750   ← api→engine
├── leader_changes.json                                              （API 寫、引擎讀）
├── capital_settings.json
└── engine/                           filet-engine:filet-api  0750   ← engine→api
    └── health/                                                      （引擎寫、API 讀）
        └── <account_id>.json
```

- 根目錄的 owner 仍是 `filet-api`：引擎對它**沒有寫權**，被打穿的引擎改寫不了客戶
  簽章記錄。
- 子目錄 `engine/` 的 owner/group **對調**：引擎能寫這一格、API 只能讀；API 對它
  沒有寫權。所以「引擎可寫」的範圍恰好是它要發布的那一格，不是整個交換目錄。
- `deploy/filet-follower@.service` 對稱地**只**把 `engine/` 列入 `ReadWritePaths`
  （根目錄不列入）。
- ⚠️ **子目錄必須人工建立**：引擎對根目錄無寫權，`mkdir` 會失敗。這是刻意的——
  「引擎能寫什麼」是部署決定，不是引擎自己決定的事。漏建的症狀是**心跳寫不出去、
  但跟單照常**（可觀測性不得中斷被觀測的系統），面板上會看到 `heartbeat_status`
  由 `missing` 轉成 `stale`。

```bash
sudo mkdir -p /var/lib/filet-exchange
sudo chown filet-api:filet-engine /var/lib/filet-exchange
sudo chmod 0750 /var/lib/filet-exchange

# ⭐ engine→api 子通道（健康心跳）。owner/group 與上面**剛好對調**——這一行寫反
# 的話，引擎寫不進去（心跳永遠 missing）或 API 讀不到（面板永遠未知）。
sudo mkdir -p /var/lib/filet-exchange/engine
sudo chown filet-engine:filet-api /var/lib/filet-exchange/engine
sudo chmod 0750 /var/lib/filet-exchange/engine

# ⭐ health/ 這一層也要人工建（2026-07-19 實機重新部署發現——原文件漏了這三行）
sudo mkdir -p /var/lib/filet-exchange/engine/health
sudo chown filet-engine:filet-api /var/lib/filet-exchange/engine/health
sudo chmod 0750 /var/lib/filet-exchange/engine/health
```

**為什麼 `health/` 不能倚賴引擎自建**：引擎的 `write_heartbeat` 確實會
`p.parent.mkdir(parents=True, exist_ok=True)`（`src/spark/filet/engine_health.py:217`），
所以漏建**不會**壞掉功能——這正是它危險的地方：**它會安靜地成功，但建出來的權限比
設計意圖寬**。引擎自建的結果是 `filet-engine:filet-engine`、mode 取決於當下的 umask
（預設 022 → `0755`）：

- group 錯成 `filet-engine`：filet-api 讀得到心跳**不是**靠群組，而是靠 `other` 的
  `r-x` 位元——等於這一格對系統上**所有**帳號開放，而不是只對 API。
- 哪天有人把服務的 umask 收緊（或加 `UMask=0077`），`other` 位元消失，**面板會在
  沒有任何人改過部署的情況下突然全部變成 `missing`**——而根因藏在一次 umask 變更裡。

人工建立讓這一格的權限是**部署決定**，跟上面 `engine/` 的理由一致：「引擎能寫什麼、
誰讀得到」不該是引擎自己（或 umask）決定的事。

**兩個 unit 都必須宣告 `FILET_EXCHANGE_DIR`**（`deploy/filet-api.service` 與
`deploy/filet-follower@.service` 已內建，值必須逐字元相同）：

```ini
Environment=FILET_EXCHANGE_DIR=/var/lib/filet-exchange
```

⭐ **半邊漏設會拒絕啟動，不會靜默失效**：這個變數**沒有預設值**。API 漏設 →
`ApiConfig.from_env` 拒絕啟動；引擎漏設 → `require_exchange_dir` 拒絕啟動
（unit 進 `failed`，是可監控的狀態）。「起不來」刻意優先於「起來了但功能靜默失效」
——後者可能好幾天沒人發現，而期間客戶每一次換 leader 都石沉大海。

#### 驗收（八條都要跑，缺一條就沒證明打通）

```bash
# ── 驗收 0：三層目錄的 owner/group/mode 逐層正確 ──
# ⚠️ 必須 sudo（2026-07-19 實機重新部署發現）：根目錄是 0750 且 other 無權，
# ubuntu 既不是 owner（filet-api）也不在 group（filet-engine），連 traverse 都不行——
# 不加 sudo 的 `ls -ld` 會在後兩層直接 Permission denied，看起來像「目錄沒建」。
sudo ls -ld /var/lib/filet-exchange \
            /var/lib/filet-exchange/engine \
            /var/lib/filet-exchange/engine/health
# 預期（逐行）：
#   drwxr-x--- ... filet-api    filet-engine  /var/lib/filet-exchange
#   drwxr-x--- ... filet-engine filet-api     /var/lib/filet-exchange/engine
#   drwxr-x--- ... filet-engine filet-api     /var/lib/filet-exchange/engine/health
# ⭐ 第 2、3 行的 owner/group 相對第 1 行是**對調**的；三行的 mode 都必須是 drwxr-x---。
# health 那行若是 filet-engine filet-engine 或 drwxr-xr-x，代表它是引擎自建的，
# 照上面三行指令重設一次（引擎下個 cycle 不會再改它）。

# ── api→engine 通道（根目錄）──
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

# ── engine→api 通道（engine/ 子目錄，方向相反）──
# 驗收 3a：filet-engine 寫得進子目錄（心跳落點）
sudo -u filet-engine touch /var/lib/filet-exchange/engine/.probe \
  && echo "engine 可寫子通道 OK" || echo "★ 失敗：心跳寫不出去，面板永遠未知"

# 驗收 3b：filet-api 讀得到（不讀就沒有面板）
sudo -u filet-api test -r /var/lib/filet-exchange/engine/.probe \
  && echo "api 可讀子通道 OK" || echo "★ 失敗：面板讀不到心跳"

# 驗收 3c：filet-api 寫**不**進子通道（反向單向性；寫得進代表 owner/group 給反了）
sudo -u filet-api touch /var/lib/filet-exchange/engine/.api-probe \
  && echo "★ 危險：api 可寫子通道，單向性失效！" || echo "api 不可寫子通道 OK"

sudo rm -f /var/lib/filet-exchange/engine/.probe

# 驗收 4：兩個 unit 宣告的值真的相同（打錯字是本節唯一擋不住的殘餘風險）
# ⚠️ 兩行都要 --value（2026-07-19 實機重新部署發現）：沒有它，`systemctl show -p Environment`
#    輸出的**第一個** token 會帶著 `Environment=` 屬性前綴。這裡是逐字元比對兩行輸出，
#    只要其中一邊的 FILET_EXCHANGE_DIR 剛好排在該 unit 的第一個，兩行就會長得不一樣，
#    在**設定完全正確**的機器上誤報成不一致。理由詳見 §5.5.2 驗收 2 的方框。
systemctl show filet-api.service -p Environment --value | tr ' ' '\n' | grep FILET_EXCHANGE_DIR
systemctl show 'filet-follower@probe.service' -p Environment --value \
  | tr ' ' '\n' | grep FILET_EXCHANGE_DIR
```

> ⚠️ **實例名不可省略，但也不必是真實帳號**（2026-07-20 修正）。上面第二行原本寫
> `filet-follower@<account_id>.service`，在**還沒有任何 follower 的機器上跑不出東西**
> ——而那正是首次部署時的狀態，也就是這條驗收最該被跑的時候。模板 unit 本身
> （`filet-follower@.service`，`@` 後面空的）同樣問不出值：systemd 要有具體實例才能
> 展開 `%i`。
>
> 解法是給一個**合成實例名**（這裡用 `probe`）：`%i` 對任意名字都會展開，unit 不必
> 啟動、帳號不必存在、不會建立任何東西。`FILET_EXCHANGE_DIR` 的值本來就與實例名無關
> （不含 `%i`），所以拿 `probe` 問到的就是真實 follower 會拿到的值。
> 同一個技巧在 §5.5.2 驗收 2（那裡用 `synthetic-check-0001`）與 §5.7 驗收 4 都用得上。

> 「兩邊都設了但**值不同**」是唯一擋不住的一格（兩個進程各看各的 env，沒有共同的
> 仲裁者）。驗收 4 是人工比對；長期由 `scripts/filet_daily_report.py` 的
> 「已寫入但未被套用」對帳告警接住——記錄落地超過 30 分鐘而引擎帳本沒有對應的
> redeemed nonce 就告警，那涵蓋路徑不通、權限錯、引擎沒跑整類失敗。

---

### 5.5.2 ⭐⭐ 宣告 `FILET_STATE_BASE`（不做這步，filet-api 會拒絕啟動）

換 leader 的交換目錄（§5.5.1）是「同一條路徑、兩個 unit 各推導一次」的**第一個**
實例。狀態根是**第二個**，而且它壞掉的方式更安靜：

| | 引擎（`filet-follower@.service`） | API（`filet-api.service`） |
|---|---|---|
| 變數 | `FILET_STATE_DIR=/opt/filet/state/%i` | `FILET_STATE_BASE=/opt/filet/state` |
| 用途 | ARM 檔／equity 樣本／`alerts.log`／兩份帳本的落點 | 健康面板與 skipped 小額的**讀**取根 |

`%i` ＝ systemd 實例名 ＝ `account_id`，所以兩者的關係是
**`<FILET_STATE_BASE>/<account_id>` 必須逐字元等於 `FILET_STATE_DIR`**。

```ini
# deploy/filet-api.service（已內建）
Environment=FILET_STATE_BASE=/opt/filet/state
```

⭐ **這個變數沒有預設值，漏設會拒絕啟動**（`ApiConfig.from_env` 的 `required` 清單，
與 `FILET_EXCHANGE_DIR` 同一個處理）。2026-07-19 之前它有一個隱含預設
`/opt/filet/state`，於是**設錯或漏設的症狀是靜默的**：API 去讀一個引擎根本沒在寫的
目錄 → 每個 follower 的狀態根都是 `absent` → 而面板當時把 `absent` 讀成
「沒有 ARM 檔＝kill switch 未觸發」，並以「已知」上呈。也就是說：**引擎已經熔斷、
部位已經被平掉的當下，面板會告訴管理員一切正常。**

修法有兩層，兩層都要（缺一層就只是換一種方式謊報）：
1. **擋源頭**：本變數升為必填，漏設直接起不來（本節）。
2. **面板不再自作主張**：`absent` 與 `unreadable` 一視同仁——kill switch 與告警數
   一律交給引擎發布的心跳（§5.6），只有「心跳新鮮且明說未觸發」才敢顯示未觸發。
   心跳來自引擎**自己的**狀態根，那一份路徑不可能弄錯。

#### 驗收（三條都要跑）

```bash
# ── 驗收 1：API 端宣告了這個變數（沒宣告的話服務根本起不來，見驗收 3）──
systemctl show filet-api.service -p Environment --value | tr ' ' '\n' | grep FILET_STATE_BASE
# 預期：FILET_STATE_BASE=/opt/filet/state

# ── 驗收 2：⭐ 兩個 unit 拼出來的是**同一個目錄**（本節唯一真正重要的一條）──
# 取兩邊的值自己算一次，不要用眼睛比對——差一個尾斜線或大小寫都看不出來。
# ⚠️ 兩個 systemctl show 的 --value 不可省略（2026-07-19 實機重新部署發現）——見下方方框。
# ACCT 用真實 account id；這台機器還沒有 follower 的話見下方「零 follower 的機器」。
ACCT=<account_id>
BASE="$(systemctl show filet-api.service -p Environment --value | tr ' ' '\n' \
        | sed -n 's/^FILET_STATE_BASE=//p')"
DIR="$(systemctl show "filet-follower@${ACCT}.service" -p Environment --value | tr ' ' '\n' \
       | sed -n 's/^FILET_STATE_DIR=//p')"
# 先確認兩個值都抓到了——空值代表指令本身有問題，不是設定有問題
[ -n "$BASE" ] && [ -n "$DIR" ] \
  || echo "★ BASE 或 DIR 抓不到值（BASE='$BASE' DIR='$DIR'）——先查指令，別急著改 unit"
[ "$(realpath -m "$BASE/$ACCT")" = "$(realpath -m "$DIR")" ] \
  && echo "狀態根一致 OK: $DIR" \
  || echo "★ 失敗：API 讀 $BASE/$ACCT，引擎寫 $DIR —— 面板會把每一列讀成 absent"

# ── 驗收 3：漏設就起不來（fail-closed，非破壞性：只在子 shell 裡試跑）──
sudo -u filet-api env -u FILET_STATE_BASE \
  /opt/filet/spark/.venv/bin/python -c \
  'from spark.publicapi.config import ApiConfig; ApiConfig.from_env()' 2>&1 \
  | grep -q FILET_STATE_BASE \
  && echo "漏設拒絕啟動 OK" || echo "★ 失敗：漏設沒有拒絕啟動，隱含預設值又回來了"
```

> ⚠️⚠️ **`systemctl show -p Environment` 一定要加 `--value`**（2026-07-19 實機重新部署發現。
> 本節驗收 2 原本沒加，是一個**會在設定正確的機器上誤報失敗**的驗收指令——那比沒有驗收更糟，
> 它會訓練操作者忽略告警。）
>
> 不加 `--value` 時，輸出是 `Environment=VAR1=val1 VAR2=val2 ...`：**只有第一個 token 帶著
> `Environment=` 屬性前綴**。經 `tr ' ' '\n'` 拆行後，第一行變成
> `Environment=FILET_STATE_DIR=/opt/filet/state/%i`，於是錨定在行首的
> `sed -n 's/^FILET_STATE_DIR=//p'` **抓不到**，變數為空，判定失敗。
>
> 而 `FILET_STATE_DIR` 正是 `filet-follower@.service` 的**第一個** `Environment=`——
> 也就是說這條驗收在真實 unit 上必定誤報。舊寫法看起來能用，只是因為對照組
> `FILET_STATE_BASE` 在 `filet-api.service` 裡排在後面（第 9 個），碰巧沒有前綴。
> **這是「靠排序碰巧成立」的脆弱性：任何一次 unit 檔調序都會讓它翻臉**，所以本文件
> 所有解析 `-p Environment` 的地方都統一加了 `--value`，即使目前碰巧不受影響的那幾處
> 也一併加（§5.1a 驗收 3、§5.5.1 驗收 4、§5.7 驗收 4）。
>
> 加上 `--value` 後實測：三組合成 acct 全部一致。

> **零 follower 的機器怎麼跑驗收 2**：`ACCT` 不需要是真實存在的帳號。systemd 對**任意**
> 實例名都會展開 `%i`，`systemctl show 'filet-follower@<任意字串>.service'` 照樣印得出
> 推導後的 `FILET_STATE_DIR`（unit 不必啟動、帳號不必存在）。所以剛部署完、還沒有任何
> follower 的機器，用合成 id 驗證路徑推導即可：
>
> ```bash
> ACCT=synthetic-check-0001    # 合成 id，只為驗證 %i 展開；不會建立任何東西
> ```
>
> 驗的是「BASE 拼 ACCT」與「引擎展開的 DIR」是否逐字元相等，這個關係與 ACCT 取什麼值無關。
> 多跑幾個不同的合成 id（含含連字號、含數字的）更能確認沒有奇怪的展開行為。

> 驗收 2 的 `★` 是**部署當下唯一能抓到路徑漂移的時機**。錯過它之後，這條錯誤不會
> 有任何 log、不會有任何告警，面板上只會看到每個 follower 的 `basis` 都是 `absent`
> ——而那與「客戶剛 activate、引擎還沒跑過」長得一模一樣。心跳（§5.6）是唯一的
> 補救：`basis: heartbeat` 且 `heartbeat_status: ok` 代表面板拿到的是引擎自報的
> 真相，路徑漂移不影響它。**面板上一整排 `absent` ＋ 心跳 `missing` ＝先查這一節。**
>
> ⚠️ **症狀不一定是 `absent`，也可能是 `unreadable`**（2026-07-19 實機重新部署發現）：
> 兩者取決於**權限佈局**，都指向同一個根因，都要查這一節。
> - 路徑指到一個**不存在**的目錄 → `absent`。
> - 路徑存在但 filet-api **讀不到** → `unreadable`。實機的 `/opt/filet/state` 是
>   `0700 filet-engine`（§5.6 刻意不放寬），filet-api 連 traverse 都不行，所以在這台機器上
>   路徑漂移實際看到的是 `unreadable` 而不是 `absent`。
>
> 面板把兩者一視同仁（見上方修法第 2 點），所以**看到哪一個都一樣要查**——
> 不要因為文件寫 `absent` 而在看到 `unreadable` 時以為是別的問題。

---

### 5.6 activate 一個 follower（人工 CLI）

⚠️ **必須指定絕對路徑或先 `cd`**：`--pending`／`--manifest` 的預設值是 CWD 相對的，
在錯的目錄跑會寫出一份引擎讀不到的 manifest（引擎讀的是
`/opt/filet/spark/var/filet/followers.json`），症狀是「activate 說成功了，
follower 起來卻找不到自己」。

⭐ `--leaders` **沒有預設值**（2026-07-20）：不給它、且 env 也沒有 `FILET_LEADERS_PATH`
→ CLI 直接 exit 2（與缺 `FILET_BUILDER_ADDR` 同一處理）。這是刻意的——本 CLI 由人工在
某台機器的某個目錄執行，「用哪一份白名單把關」不該由一個看不見的預設值決定。下面的
指令已經明給了它，照抄即可；`--leaders` 給了就以它為準（管理端當下的明確指示優先於 env）。

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

#### ⭐⭐ 健康面板的資料來自**引擎發布的心跳**，狀態根維持 `0700`（不要放寬）

`0700` 表示只有 `filet-engine` 讀得到狀態根，而營運健康面板跑在 **filet-api** 進程裡
——直讀一律讀不到。**這個權限維持不變，不要改成 0750。**

> ⚠️ 本節在 2026-07-19 之前建議 `chmod 0750 /opt/filet/state/<account_id>` 來讓面板
> 看得到資料。**那個建議已被撤回，不要照做。** 理由：面板需要的是幾個摘要值
> （kill switch 有沒有跳、樣本夠不夠、跟誰、押多大），而狀態根裡裝的是引擎的**全部**
> 狀態——equity 樣本序列、kill switch ARM 檔、兩份已兌現 nonce 帳本、告警流水。
> 為了讀五個數字而把這一整包交給另一個進程，是拿「廣泛的讀取權」換「窄的資訊需求」。

**取而代之**：引擎每個 cycle 主動往 §5.5.1 的 **engine→api 子通道**發布一份窄的健康
摘要（`/var/lib/filet-exchange/engine/health/<account_id>.json`），面板從那裡讀。
設計與換 leader 同構——**發布一份窄的產物，優於開放廣泛的讀取權**——只是通道反向。
實作與威脅模型見 `src/spark/filet/engine_health.py` 檔頭。

心跳只含摘要，**結構性地不含**簽章／nonce／密鑰材料：寫入邊界會掃描整份 payload，
命中就拒寫（`tests/test_engine_health.py` 釘住）。

- **面板每列的 `basis`** 說明那一列的 kill switch 與覆蓋度出自直讀（`state_root`，
  只有在有人放寬過權限時才會出現）還是心跳（`heartbeat`）。
- **`heartbeat_status` 三態不折疊**，處置各不相同：
  | 值 | 意思 | 先做什麼 |
  |---|---|---|
  | `ok` | 心跳新鮮（≤ 600s） | 無 |
  | `missing` | 從未寫過 | 檢查 §5.5.1 的 `engine/` 子目錄建了沒、權限對不對 |
  | `stale` | 有心跳但過期 | `systemctl status filet-follower@<account_id>`；引擎在跑卻過期＝寫不進子通道 |
  | `unreadable` | 檔案在但讀不出來／格式壞 | 看檔案本身 |
- ⭐ **過期的心跳不會被當成目前狀態顯示**：`stale` 時面板只多出「最後心跳時刻」與
  「心跳年齡」兩格，心跳裡的值一個都不會被填進現況欄位
  （回歸測試：`tests/test_api_ops.py::test_stale_heartbeat_is_never_shown_as_current_state`）。
  一份 40 分鐘前的「kill switch 未觸發」在客戶的引擎已經熔斷的當下顯示成現況，
  是本面板最不能犯的錯。

> ⭐ 面板**不會**因為讀不到就顯示「未觸發／健康」。這一格曾經是個真的 bug：
> Python 的 `Path.exists()` 會把 PermissionError 吞成 `False`，於是一個**確實已經
> 熔斷**的 follower 會被回報成「kill switch 未觸發」。現在由
> `ops.state_root_status()` 先探測可讀性，讀不到一律整列標未知
> （回歸測試：`tests/test_api_ops.py::test_unreadable_state_root_never_reports_killswitch_as_untripped`）。

```bash
# 驗收：activate 並啟動 follower 之後，等一個 cycle（預設 60s），心跳應該落地
sudo -u filet-api test -r /var/lib/filet-exchange/engine/health/<account_id>.json \
  && echo "心跳 OK（面板會有資料）" || echo "★ 心跳缺席：見 §5.5.1 的子目錄與權限"

# 狀態根**維持** 0700：這一條應該印「api 不可讀 OK」
sudo -u filet-api test -r /opt/filet/state/<account_id> \
  && echo "★ 注意：狀態根已被放寬，面板會改用直讀（basis=state_root）" \
  || echo "api 不可讀 OK（面板改用心跳，這是預期的部署形態）"
```

---


### ⚠️ 5.7a 改動 `leader_perf` 投影欄位後：必須手動重跑一次快照

<!-- 2026-07-19 實機部署發現 -->
`_leader_perf_public` 刻意用 `if k in row`（不是 `.get()`）投影——**後端刻意不給的鍵不得被憑空造出來**。
代價是：**磁碟上的快照若由舊碼產生，就沒有新欄位**，而部署本身不會重生快照。

**實際發生過的失效**：改版目的是「績效數字必須帶資料不足警示」，但部署後 API 照樣吐出年化數字、
**一個警示都沒有**——因為快照是舊碼的。若沒發現，這個狀態會持續到隔天 00:10 UTC timer 觸發。
**失效方向剛好是這次改版要根除的那一個。**

```bash
sudo systemctl start filet-leaderboard.service
# 驗收：確認新欄位真的在快照裡（改成你這次新增的欄位名）
sudo -u filet-api /opt/filet/spark/.venv/bin/python - <<'EOF'
import json, glob
f = sorted(glob.glob("/var/lib/filet-api/leaderboard/watchlist/*.json"))[-1]
w = json.load(open(f))["rows"][0]["perf"]["windows"]["perpMonth"]
print([k for k in sorted(w) if "insufficient" in k or "extrapolated" in k])
EOF
```

**通則**：任何改動「資料生產者 → 投影 → API」這條鏈的欄位契約時，部署完都要問一句
**「磁碟上的資料是舊碼產生的嗎？」**——程式碼對、部署對、資料舊，是這條鏈特有的失效。

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
#   <FILET_DATA_DIR>/leaderboard/watchlist/<YYYY-MM-DD>.json    （一天一個檔）
#   <FILET_DATA_DIR>/leaderboard/perf_series/<address>.jsonl    （一個 leader 一個檔）
# ⚠️ 副檔名是 .jsonl 不是 .json（2026-07-19 實機重新部署發現，原文件寫錯）——
# 實際出自 `series_path_for`（src/spark/filet/perf_series.py:105，append-only 每行一筆）。
# 照舊文找 .json 會一個檔都找不到，誤判成「產物沒落地」而去查根本沒壞的 timer。
sudo ls -l /var/lib/filet-api/leaderboard/watchlist/   | tail -5
sudo ls -l /var/lib/filet-api/leaderboard/perf_series/ | tail -5
# 預期：watchlist 有今天日期的檔；perf_series 每個白名單 leader 各一個檔，
# 且 mtime 是剛才那次手動執行。⭐ 沒有檔就是**現在**要查，不是明天
# （perf_series 尤其：等到明天，今天漏掉的那兩個窗已經永遠補不回來了）

> ⚠️ **同一個 12h 窗內重跑會被冪等跳過**（`appended=0 idempotent_skips=1`），這是**正確行為**不是故障。
> 此時 mtime 仍是上一次取樣的時間——不要據此判定「產物沒落地」。<!-- 2026-07-19 實機發現：文件原文會把正確行為誤判成失敗 -->

# 驗收 4：⭐⭐ **四個** unit 看到的是同一份白名單（抓取對象與「誰能被選」的單一來源）
# --value 不可省略（同 §5.5.2 驗收 2 的方框）：這裡是逐字元比對四行輸出。
#
# ⚠️ 2026-07-20 之前這條只比對兩個 timer，而**在 filet-api 這端根本不成立**：
# 那時 `filet-api.service` 沒有宣告 FILET_LEADERS_PATH（宣告數 0），API 走的是程式
# 的隱含預設，只因為它的 WorkingDirectory 恰好是 repo 根、預設值又錨定 repo 根，
# 兩邊才「恰好」是同一個檔。也就是說：**當時最需要被驗的那一格，驗收指令量不到。**
# 現在四個 unit 全部顯式宣告（漏設即拒絕啟動），這條才真的證明了單一來源。
for U in filet-api filet-follower@probe filet-leaderboard filet-perf-series; do
  printf '%-24s ' "$U"
  systemctl show "${U}.service" -p Environment --value | tr ' ' '\n' \
    | sed -n 's/^FILET_LEADERS_PATH=//p'
done
# 預期：四行的值逐字元相同，且等於 §5.5 建立的 leaders.json 路徑。
# ⭐ 任何一行是**空的**就是本節最重要的失敗：那個 unit 沒宣告 → 該服務會拒絕啟動
#   （filet-api／follower）或該次取樣會 failed（兩個 timer）。
#
# `filet-follower@probe` 的 `probe` 是**合成實例名**（同 §5.5.2 驗收 2 的說明）：
# systemd 對任意實例名都會展開 `%i`，unit 不必啟動、帳號不必存在。模板 unit 本身
# （`filet-follower@.service`，沒有實例名）則問不出展開後的值，所以這裡一定要帶一個名字。

# 一行式的自動判定（不想用眼睛比四行時用這個）
UNIQ=$(for U in filet-api filet-follower@probe filet-leaderboard filet-perf-series; do
         systemctl show "${U}.service" -p Environment --value | tr ' ' '\n' \
           | sed -n 's/^FILET_LEADERS_PATH=//p'; done | sort -u | wc -l)
[ "$UNIQ" = "1" ] && echo "四個 unit 同一份白名單 OK" \
  || echo "★ 失敗：四個 unit 的白名單不一致或有人沒宣告（不同值個數=$UNIQ）"
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

### 5.8 ⭐ 每日跨 follower 日報（`filet-daily-report`，含營收關鍵告警推播）

> 編號 `5.8` **附加**在 §5 尾端（不重編既有編號，沿 §5.5.1／§5.7 的插入慣例）。

跨 follower 日報跑在 `filet-engine` 帳號下（**不是** `filet-api`：它要讀 per-follower
狀態根 `/opt/filet/state/<id>` 做換 leader 對帳，那一層是 `filet-engine:filet-engine 0700`，
只有引擎帳號讀得到）。它每日算一次北極星（builder fee 增量），並輸出兩類**營收關鍵**
告警——都會同時進報表檔／`journalctl`，**並推到 Telegram**：

| 告警 | 觸發 | 不推播的後果 |
|---|---|---|
| **builder 資格異常** | builder 的 perp 淨值跌破 100 USDC，或 account abstraction mode 非 standard | builder fee **無聲**停止累積：成交照常、log 正常，只有收入曲線悄悄變平 |
| **換 leader 未生效** | 客戶簽了、API 收了，引擎卻從沒套用（逾時未兌現） | 客戶以為換好了，實際仍跟舊 leader；三種成因（路徑不通／權限錯／引擎沒跑）症狀相同、兩邊 log 都正常 |

> ⭐⭐ 這兩類失效的共同點是「一切看起來正常但錢已經受影響」。推播是唯一能在**當下**
> 觸達的通道（報表檔與 journal 埋著，沒人會即時去翻）。**部署後務必實測 Telegram 收得到**
> （下方驗收 3），否則等於沒接。

#### 前置 1：Telegram 憑證檔 `/etc/filet/telegram.env`（**絕不進 repo**）

憑證只放伺服器這個檔，unit 用 `EnvironmentFile=-/etc/filet/telegram.env` 注入
（`-` 前綴＝檔案不存在也不擋啟動，此時日報降級成不推播、其餘照跑）。**格式如下，值請填
自己的 bot token 與 chat id——不要把真值寫進任何 repo 檔案或 commit**：

```bash
# 內容格式（token/chat 為佔位，換成真值）：
#   COPY_TG_BOT_TOKEN=123456:REPLACE_WITH_BOT_TOKEN
#   COPY_TG_CHAT_ID=REPLACE_WITH_CHAT_ID
# （選配）COPY_TG_MUTED=  # critical 永不受靜音影響，日報告警一律送達
sudo install -m 640 -o root -g filet-engine /dev/null /etc/filet/telegram.env
sudo -e /etc/filet/telegram.env    # 用編輯器填入上面兩行的真值

# 權限驗證：root:filet-engine 0640——filet-engine 讀得到、其他帳號讀不到
sudo ls -l /etc/filet/telegram.env
# 預期：-rw-r----- 1 root filet-engine ... /etc/filet/telegram.env
```

#### 前置 2：報表與快照的落點（`filet-engine` 可寫，`var/filet` 本身維持 root:root）

日報唯二要**寫**的是報表目錄與 builder accrued 快照。它們在 `/opt/filet/spark/var/filet`
底下，而該目錄刻意是 `root:root 755`（承載 `leaders.json` 承重點，見 §2）——所以只把這
**兩個子項**建立成 `filet-engine` 擁有，並在 unit 的 `ReadWritePaths` 精準授權這兩個，
`var/filet` 目錄本身不放寬（`leaders.json` 644 root:root 仍寫不到）：

```bash
sudo mkdir -p /opt/filet/spark/var/filet/reports
sudo chown filet-engine:filet-engine /opt/filet/spark/var/filet/reports
sudo chmod 700 /opt/filet/spark/var/filet/reports

# 快照檔預先建成合法 JSON（空 builders）並 chown——ReadWritePaths 指向檔案，
# 檔案必須在 unit 啟動前存在。內容 {"builders": {}} 語意上等同「無檔＝0」，首跑安全。
echo '{"builders": {}}' | sudo tee /opt/filet/spark/var/filet/builder_accrued_snapshot.json >/dev/null
sudo chown filet-engine:filet-engine /opt/filet/spark/var/filet/builder_accrued_snapshot.json
sudo chmod 600 /opt/filet/spark/var/filet/builder_accrued_snapshot.json
```

#### 安裝與啟用

```bash
sudo cp /opt/filet/spark/deploy/filet-daily-report.service /etc/systemd/system/
sudo cp /opt/filet/spark/deploy/filet-daily-report.timer   /etc/systemd/system/
sudo systemctl daemon-reload

# ⚠️ enable --now 的是 **.timer**，不是 .service（enable 錯對象會讓 oneshot 每次
# 開機跑一次、從此不再定時——症狀是「報表每隔幾天才有一筆」，很久沒人會發現）。
sudo systemctl enable --now filet-daily-report.timer
```

#### 驗收（三條都要跑）

```bash
# 驗收 1：timer 已排程，NEXT 欄有時間（不是 n/a）
systemctl list-timers 'filet-daily-report*' --all --no-pager
# 預期：一行，NEXT 落在明天 00:20 UTC 附近（+ 最多 5 分隨機延遲）

# 驗收 2：手動跑一次確認**現在就能成功**（不要等明天才發現 env 或權限錯）
sudo systemctl start filet-daily-report.service
systemctl status filet-daily-report.service --no-pager -l   # 預期：SUCCESS（oneshot 跑完 inactive）
# 產物落地：今天日期的報表檔（版面出自 scripts/filet_daily_report）
sudo ls -l /opt/filet/spark/var/filet/reports/ | tail -3
# 預期：有 <YYYY-MM-DD>.md，mtime 是剛才這次執行

# 驗收 3：⭐⭐ **Telegram 真的收得到**（營收關鍵——沒實測過等於沒接）
# 最可靠的實測：暫時把某個 mainnet builder 的門檻查詢引導到不合規，或直接送一則測試：
sudo -u filet-engine bash -c 'set -a; . /etc/filet/telegram.env; set +a; \
  /opt/filet/spark/.venv/bin/python -c "
from spark.copytrade.notifier import TelegramNotifier
n = TelegramNotifier.from_env()
print(\"sent:\", n.critical(\"builder合規\", \"[部署驗收] filet-daily-report 推播測試，收到請忽略\", dedup_key=\"deploy_probe\"))
"'
# 預期：印出 sent: True，且該 Telegram 頻道收到一則 [CRIT] builder合規 | ... 訊息。
# sent: False → 憑證錯／頻道錯，回頭查前置 1（此時日報會靜默不推，最危險的失效方向）。
```

#### 監控

```bash
# 有沒有跑失敗（⭐ unit 刻意無 `-` 前綴：失敗會進 failed，這裡就看得到）
systemctl list-units 'filet-daily-report*' --state=failed --no-pager
# 最近幾次執行
sudo journalctl -u filet-daily-report --since '3 days ago' --no-pager | tail -30
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

> ⚠️ **`reload_follower.sh` 的執行位元**（2026-07-19 實機重新部署發現）：repo 內原本是
> `644`，直接呼叫會得到 `Permission denied`／`command not found`。已在 repo 裡 `chmod +x`
> 成 `755`（git 記錄了模式變更：`old mode 100644` → `new mode 100755`），§3.2 的
> `rsync -a` 會保留權限位元，所以新部署的機器拿到的就是可執行的版本。
>
> 選 `chmod +x` 而不是把這行改成 `sudo bash <path>`，理由是**後者會改變 sudo 的語意**：
> 這支腳本內部是**逐個 unit** 跑 `sudo systemctl restart`（它的檔頭寫明「需求：執行者對
> `systemctl restart filet-follower@*` 有 sudo NOPASSWD」）。整支用 `sudo bash` 跑等於
> 讓整個迴圈以 root 執行，套用的是 root 的權限而非操作者的 NOPASSWD 規則，違背腳本
> 原本「最小權限、只放行這一種 restart」的設計。
>
> **舊機器上驗證**（重新部署前這台機器的檔案可能還是 644）：
>
> ```bash
> ls -l /opt/filet/spark/deploy/reload_follower.sh   # 預期：-rwxr-xr-x
> # 若仍是 -rw-r--r--：重跑 §3.2 的 rsync 就會帶上新模式；急用時的一次性補救：
> sudo chmod +x /opt/filet/spark/deploy/reload_follower.sh
> ```

### 9.3 回滾（2026-07-19 實機重新部署發現：**全節重寫**）

> 🛑 **舊版本節的 `cd /opt/filet/spark && git checkout ...` 已經失效，不要照做。**
> 伺服器上**沒有 git repo**（`/opt/filet/spark/.git` 不存在，實機已確認；成因與「為什麼
> 這是刻意的」見 §3.2）。在伺服器上跑 `git log`／`git checkout` 只會得到
> `fatal: not a git repository`。
>
> **新模型：回滾由操作者本機驅動，伺服器只是被同步的目標。**
> 回滾 ＝ 本機 `git checkout <舊 commit>` → 重跑 §3.2 的 rsync → 重跑 §5.1a 的 unit 還原
> → 依 §9.2 重啟。與一次正常部署走的是**同一條路徑**，只是本機停在舊 commit 上——
> 這正是它可靠的原因：回滾不是一條平時沒人走、真的要用時才發現壞掉的獨立程序。

#### 步驟 0：先確認「現在部署的是哪一版」與「要回滾到哪一版」

```bash
# ── 現在是哪一版：讀伺服器上的版本標記檔（§3.2 最後一步寫的）──
ssh -i <金鑰路徑> ubuntu@FILET_LIGHTSAIL_IP_PLACEHOLDER 'cat /opt/filet/spark/DEPLOYED_VERSION'
# 預期：commit= / describe= / deployed_at_utc= / deployed_by= 四行
# ⚠️ 檔案不存在＝上一次部署沒跑 §3.2 最後一步（或跑到一半失敗）。此時無法從機器上
#    確定版本，只能靠部署紀錄回推——不要用猜的 commit 回滾。
```

```bash
# ── 要回滾到哪一版：在**本機**看 git 歷史（伺服器沒有 git，只有本機有）──
cd /Users/jim/projects/spark
git log --oneline -10
# 用上面 DEPLOYED_VERSION 的 commit= 對照，確認目前線上版本在歷史上的位置，
# 再挑「上一個已知良好」的 commit。
git log --oneline -1 <目前線上的 commit>   # 驗收：這個 commit 在本機確實存在
```

#### 步驟 1：本機切到要回滾的 commit

```bash
cd /Users/jim/projects/spark
git status --porcelain          # ⚠️ 必須是空的——有未 commit 的改動會被一起推上正式機
git checkout <要回滾到的 commit>
git describe --always --dirty   # 驗收：印出的短 hash 就是等一下會部署上去的版本
```

> 回滾結束、線上恢復之後，本機記得 `git checkout feat/m2-frontend`（或原本的分支）切回來，
> 不然下一次部署會從 detached HEAD 推出去。

#### 步驟 2：重跑 §3.2 的 rsync（兩段都要）

回 **§3.2 從頭到尾整節跑一遍**（本機→`/tmp/spark-sync/` 的 rsync、伺服器→`/opt/filet/spark/`
的 rsync、`chown ubuntu` → `uv sync` → `chown -R root:root` 收尾）。回滾是重新部署，
**不要只挑 rsync 那兩段跑**——尤其別漏掉重新部署專屬的
`sudo chown -R ubuntu:ubuntu .venv uv.lock` 那一行，漏了 `uv sync` 會 Permission denied。

- `--delete` 會把新版本多出來的檔案清掉，這正是回滾要的效果。
- `--exclude var` 保護 leader 白名單（§5.5）不被回滾波及——白名單是營運資料不是程式碼。
- **依賴可能降版**：`uv sync` 會依回滾後的 `pyproject.toml` 重新解析。
- 前端要一起回滾就照 §4.2 重跑 `npm ci && npm run build`（含 build 前後的 chown 兩步）。

#### 步驟 3：重跑 §5.1a 的 unit 還原（**最容易漏、漏了會靜默壞掉**）

回滾的 rsync 一樣會覆蓋 `/opt/filet/spark/deploy/` 下的 unit 檔。只要這次回滾**有重跑
§5.1 的 `cp`**（unit 檔在兩版之間有差異時就必須重跑），就會清掉 `/etc/systemd/system/`
裡那 6 個實際值——照 **§5.1a 步驟 1→4** 走一遍（備份、覆蓋、還原 6 個值、`daemon-reload` 與驗證）。

> 兩版之間 unit 檔沒有任何差異時，可以整個跳過 §5.1／§5.1a——但**要先確認**：
> 本機 `git diff <舊commit> <目前線上commit> -- deploy/` 沒有輸出才算確認。

#### 步驟 4：重寫版本標記檔

回滾也是一次部署，**標記檔必須跟著回滾**，否則下一個人讀到的是錯的版本號。
照 §3.2 最後一步那段 `printf ... | ssh ... sudo tee` 再跑一次（此時本機 HEAD 已在舊 commit 上，
算出來的就是回滾後的版本）。

#### 步驟 5：依 §9.2 順序重啟

```bash
sudo systemctl restart filet-keysvc.service
sudo systemctl status filet-keysvc.service --no-pager   # 確認 active 再往下
sudo systemctl restart filet-api.service
sudo systemctl status filet-api.service --no-pager
sudo systemctl restart filet-dashboard.service
sudo systemctl status filet-dashboard.service --no-pager
/opt/filet/spark/deploy/reload_follower.sh
```

#### 步驟 6（僅在本次部署也改過 nginx 設定時）

```bash
# 先備份現行設定，再換回舊版設定檔
sudo cp /etc/nginx/sites-available/filet /etc/nginx/sites-available/filet.bak-$(date +%F)
# 換回舊版設定檔後：
sudo nginx -t && sudo systemctl reload nginx
```

> ⚠️ 別直接從 repo `cp nginx-filet.conf` 蓋回去——那會清掉網域代換與 certbot 寫進去的
> 憑證路徑（同 §5.1a 步驟 5 的警告）。

#### ⚠️⚠️ 回滾**不會**回滾資料——這是本節最重要的一段

回滾只換程式碼。以下全部**維持在新版本留下的狀態**，不會跟著倒退：

| 資料 | 為什麼不動 |
|---|---|
| `/etc/filet/keys`（agent key） | 非託管信任鏈的一部分；回滾程式碼不等於回滾金鑰狀態 |
| `/var/lib/filet-api`（`api.db`、`pending.json`） | 使用者 onboarding 進度，不在 rsync 範圍內 |
| `/opt/filet/spark/var/`（leaders.json、manifest） | 被兩段 rsync 的 `--exclude var` 保護 |
| `/opt/filet/state/<account_id>`（ARM 檔、equity 樣本、帳本、`alerts.log`） | 狀態根不在 repo 路徑下，rsync 完全碰不到 |
| `/var/lib/filet-exchange`（換 leader 交換目錄、心跳） | 同上，不在同步範圍 |

**因此：如果被回滾掉的那一版曾經升級過任何資料格式（DB schema、manifest 欄位、帳本行格式、
心跳 JSON 欄位），舊程式碼會讀到「比它新」的資料。** 這類不相容可能是大聲的（起不來、
fail-fast），也可能是安靜的（欄位讀不到→當成預設值→面板顯示錯誤狀態）。

回滾**之前**必須先回答：**這兩版之間有沒有動過資料格式？**

```bash
# 在本機比對兩版之間有沒有碰到會寫資料的模組（回滾前必看）
cd /Users/jim/projects/spark
git diff --stat <要回滾到的 commit> <目前線上的 commit> -- \
  src/spark/publicapi/ src/spark/filet/ src/spark/copytrade/
```

有輸出就逐一看 diff 判斷是否含格式變更；**判不準就不要自行回滾，先問使用者**——
資料格式不相容造成的損壞，往往比原本要回滾的那個 bug 更難救。

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
