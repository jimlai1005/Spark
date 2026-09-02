"""部署產物的安全不變量（2026-07-20）。

## 為什麼有這個檔

測試計畫對 25 個不變量做變異測試：程式碼層 20 個全被既有測試咬住，**裸露的 5 個
全部在部署產物層**——而每一條都對應一次真實事故。最危險的那條已實地複驗：把
`deploy/follower.env.example` 改回 `SPARK_NETWORK=mainnet` ＋ `COPY_LIVE_TRADING=true`
（＝複製範本忘了改就拿真錢在主網跑），全套 1722 條測試**全過**。

我們每次事故都修好了**內容**，卻從沒留下「有人改回去就轉紅」的測試——**修了實例，
沒修類別**。這個檔補的就是類別。

## 「容忍格式、不容忍語意」的界線

斷言一律建立在**解析出來的值**上，不是檔案的字面文字：

- **容忍**：空白與縮排、空行、行序、rsync 參數順序與換行續行位置、引號風格
  （`'x'`／`"x"`／裸值）、`--exclude X` 與 `--exclude=X` 兩種寫法、目錄樣式的結尾斜線
  （`var` 與 `var/` 對「保護 var 這個目錄」而言同義）、註解內文任意改寫。
- **不容忍**：任何會改變**執行後果**的更動——值變了、鍵不見了、exclude 清單少一項、
  rsync 從兩段變成三段、佔位符被換成可用的具體值。

布林與網路值刻意**沿用生產環境的解析器**（`_env_bool`、`API_URLS`），而不是自己再寫一份
比較邏輯：被比較的兩個值必須同源同基準，否則測試綠燈只證明測試自己的假設成立。
所以 `COPY_LIVE_TRADING=0` 這種語意上安全、字面上不等於 `false` 的寫法不會誤紅。
"""
import re
from pathlib import Path

import pytest

from spark.config import API_URLS
from spark.copytrade.config import _env_bool

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def _read(name: str) -> str:
    return (DEPLOY / name).read_text()


def _env_file_map(name: str) -> dict[str, str]:
    """讀 .env 風格範本 → {KEY: VALUE}。跳過註解與空行（容忍行序與縮排）。"""
    out: dict[str, str] = {}
    for line in _read(name).splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        out[k.strip()] = v.strip()
    return out


def _unit_env(name: str) -> dict[str, str]:
    """讀 systemd unit → 生效的 `Environment=KEY=VALUE`。跳過被註解掉的行。"""
    out: dict[str, str] = {}
    for line in _read(name).splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        m = re.match(r"Environment=([A-Za-z_][A-Za-z0-9_]*)=(.*)$", s)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def _unit_directive(name: str, key: str) -> list[str]:
    """讀 systemd unit 的某個指令（如 ExecStart／ReadWritePaths）的所有生效值。"""
    vals = []
    for line in _read(name).splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        if s.startswith(f"{key}="):
            vals.append(s[len(key) + 1:].strip())
    return vals


# ── RUNBOOK 的 rsync：解析成結構，不做字面比對 ──────────────────────────────
def _rsync_commands() -> list[str]:
    """抽出 RUNBOOK ```bash 區塊裡**真正被執行**的 rsync 指令（散文提及不算）。

    續行（`\\` + 換行）先接成一條邏輯行，所以換行位置怎麼排都不影響。
    """
    text = _read("RUNBOOK.md")
    cmds = []
    for block in re.findall(r"```bash\n(.*?)```", text, re.DOTALL):
        joined = re.sub(r"\\\s*\n\s*", " ", block)
        for line in joined.splitlines():
            s = line.strip()
            if s.startswith("#"):
                continue
            toks = s.split()
            if toks and toks[0] == "sudo":
                toks = toks[1:]
            if toks and toks[0] == "rsync":
                cmds.append(" ".join(toks))
    return cmds


def _excludes(cmd: str) -> set[str]:
    """指令 → exclude 樣式集合。容忍引號風格、`=` 寫法、目錄結尾斜線。"""
    raw = re.findall(r"--exclude(?:=|\s+)('[^']*'|\"[^\"]*\"|\S+)", cmd)
    return {p.strip("'\"").rstrip("/") for p in raw}


SECRET_PATTERNS = {".env", ".env.*", "*.key"}


# ══ 不變量 1 & 2：出貨的 follower 範本必須是安全值 ═════════════════════════════
# 事故 2026-07-19：本範本原本出貨 mainnet + live_trading=true。

def test_follower_env_example_ships_testnet():
    """⭐⭐⭐ 範本的 SPARK_NETWORK 必須是 testnet。

    範本存在的唯一用途就是被 `cp` 走。出貨 mainnet ＝ 複製後忘記改的人，
    第一次啟動引擎就是拿**真錢**在主網跟單——而且沒有任何一步會警告他。
    """
    val = _env_file_map("follower.env.example")["SPARK_NETWORK"]
    assert val == "testnet", (
        f"follower.env.example 出貨 SPARK_NETWORK={val!r}，必須是 'testnet'。"
        " 這個範本是要被 cp 走的：出貨主網＝複製的人忘了改就拿真錢在主網下單，"
        " 全程零警告（2026-07-19 事故）。改主網是動真錢的人工決策，不是範本預設值。"
    )


def test_follower_env_example_ships_live_trading_off():
    """⭐⭐⭐ 範本的 COPY_LIVE_TRADING 必須解析為 False。

    用生產解析器 `_env_bool` 判定，不是字面比對——語意安全的寫法（`0`、`False`）
    不該誤紅，而 `true`／`True`／`TRUE` 任一種都必須轉紅。
    """
    env = _env_file_map("follower.env.example")
    assert _env_bool("COPY_LIVE_TRADING", "false", env) is False, (
        f"follower.env.example 出貨 COPY_LIVE_TRADING={env.get('COPY_LIVE_TRADING')!r}，"
        " 它會被解析成 True。這直接反轉專案紅線 5「live_trading 預設 False，任何主網"
        " 寫入是人工決策，不得自動開啟」——範本把紅線的預設值翻面了（2026-07-19 事故）。"
    )


def test_follower_env_example_keeps_no_safe_default_fields_as_placeholders():
    """沒有安全預設值的欄位必須維持佔位符，讓引擎 fail-fast 拒絕啟動。

    尤其 COPY_ALLOCATED_CAPITAL：出貨一個**看起來合理的數字**，複製的人不會察覺
    自己從沒填過本金上限——而那個數字就是實際投入的資金上限。
    """
    env = _env_file_map("follower.env.example")
    for key in ("SPARK_ACCOUNT_ID", "SPARK_USER_ADDR", "SPARK_BUILDER_ADDR",
                "COPY_ALLOCATED_CAPITAL"):
        assert env[key].startswith("REPLACE_WITH"), (
            f"{key}={env[key]!r} 不再是佔位符。這些欄位沒有安全預設值，"
            " 出貨具體值＝複製範本的人不會發現自己從沒填過（本金上限尤其致命）。"
        )


# ══ 不變量 3：filet-api 的 network 必須是佔位符 ════════════════════════════════

def test_api_unit_network_is_a_placeholder_not_a_usable_network():
    """⭐⭐⭐ filet-api.service 不得出貨具體 network。

    這個檔會覆蓋 /etc/systemd/system/ 的同名檔：硬編 mainnet 的話，一次照 RUNBOOK
    的重新部署就把測試機翻上主網。判準是「不是**可用**的 network」——直接拿生產的
    API_URLS 當來源，因為那正是 config 判定 fail-closed 與否的同一份字典。
    """
    val = _unit_env("filet-api.service")["FILET_API_NETWORK"]
    assert val not in API_URLS, (
        f"filet-api.service 出貨 FILET_API_NETWORK={val!r}（可用的 network）。"
        " 此檔會覆蓋 /etc/systemd/system/ 的同名檔：照 RUNBOOK 重新部署一次就會把"
        " 測試機翻上主網，服務照常 active、沒有任何錯誤。必須留佔位符讓 config"
        " 因 `network not in API_URLS` 拒絕啟動（fail-closed）。"
    )
    assert val.startswith("REPLACE_WITH"), (
        f"FILET_API_NETWORK={val!r} 既不是可用 network 也不是 REPLACE_WITH_ 佔位符；"
        " 部署者不會知道這裡要填東西。"
    )


# ══ 不變量 4 & 5：RUNBOOK 兩段 rsync ═══════════════════════════════════════════
# ⚠️ 兩段的 --exclude 語意**不同**，這是本節所有斷言的基礎：
#   第一段 來源＝本機工作樹  → exclude ＝「不要把這個**推上去**」
#   第二段 來源＝已消毒的 /tmp/spark-sync/ → exclude ＝「不要讓 --delete **刪掉**目的端的它」
# RUNBOOK 自己在 §3.2 寫了這件事（--exclude 同時保護目的端不被 --delete 清除），
# 也依這個道理拒絕在第二段加 --exclude .git（會在伺服器留下永不更新的舊 .git）。

def test_runbook_has_exactly_the_two_documented_rsync_segments():
    """部署路徑就是這兩段。多出第三段 ＝ 有一條沒被本檔其餘斷言涵蓋的推碼路徑。"""
    cmds = _rsync_commands()
    assert len(cmds) == 2, (
        f"RUNBOOK 的 bash 區塊有 {len(cmds)} 段 rsync，預期 2 段。"
        " 下面每一條 exclude 斷言都是按「第一段推碼、第二段落地」寫的；"
        " 段數一變，那些斷言就不再涵蓋完整的推碼路徑——請先確認新那段的 exclude 清單。"
    )


def test_first_rsync_excludes_secrets_before_they_leave_the_working_tree():
    """⭐⭐⭐ 第一段必須排除 .env／.env.*／*.key。

    第一段是**唯一**來源為本機工作樹的 rsync，也就是唯一可能把真實 `.env` 與私鑰
    推上正式機的一段。少一個樣式 ＝ 私鑰離開本機。
    """
    ex = _excludes(_rsync_commands()[0])
    missing = SECRET_PATTERNS - ex
    assert not missing, (
        f"第一段 rsync 少了機密 exclude：{sorted(missing)}。"
        " 這是唯一來源為本機工作樹的一段——少一個樣式，真實 .env／私鑰就會被推上"
        " 正式機。私鑰不得離開本機（專案紅線 2）。"
    )


def test_second_rsync_does_not_shield_secrets_from_delete():
    """⭐⭐ 第二段刻意**不**排除機密樣式——別「補齊」它。

    第二段來源是已被第一段消毒的 /tmp/spark-sync/，本來就帶不上機密；而在這一段，
    `--exclude X` 的實際效果是**保護目的端的 X 不被 `--delete` 清掉**。加上
    `--exclude .env` 等於讓伺服器上任何一份殘留／外洩的 `.env` **永遠留著**，
    而不是每次部署被 --delete 掃掉。這與 RUNBOOK 拒絕在第二段加 `--exclude .git`
    是同一個道理（留下永不更新的舊檔比沒有更危險）。
    """
    ex = _excludes(_rsync_commands()[1])
    leaked = SECRET_PATTERNS & ex
    assert not leaked, (
        f"第二段 rsync 排除了機密樣式 {sorted(leaked)}。第二段的來源已被第一段消毒，"
        " 這裡的 --exclude 不會擋任何東西上傳，只會**保護目的端不被 --delete 清除**"
        " ——等於讓伺服器上殘留的 .env／私鑰永久留存。要擋機密請看第一段。"
    )


@pytest.mark.parametrize("seg", [0, 1])
def test_both_rsync_segments_exclude_var(seg):
    """⭐⭐⭐ 兩段都必須 `--exclude var`——這條**真的**需要兩段各自檢查。

    `var/` 裝的是 leader 白名單與 followers manifest（伺服器端狀態，repo 裡沒有）。
    第一段少了它：本機的 var 蓋掉伺服器的白名單。
    第二段少了它：來源 /tmp/spark-sync/ 沒有 var，`--delete` 直接**刪掉**伺服器上的
    白名單——而白名單消失的方向是 fail-open 的下游災難（撤銷紀錄一起沒了）。
    只補一段等於換一種方式弄丟它。
    """
    cmd = _rsync_commands()[seg]
    assert "var" in _excludes(cmd), (
        f"第 {seg + 1} 段 rsync 少了 `--exclude var`。var/ 是伺服器端狀態"
        "（leader 白名單、followers manifest），repo 裡沒有這份資料："
        " 第一段少了它是本機蓋掉伺服器，第二段少了它是 --delete 直接刪掉白名單。"
        " 兩段各自都要有（RUNBOOK §3.2）。"
    )


@pytest.mark.parametrize("seg", [0, 1])
def test_both_rsync_segments_keep_delete_and_exclude_uv_lock(seg):
    """兩段都要 `--delete`，且兩段都要 `--exclude uv.lock`（RUNBOOK 2026-07-20 發現）。

    `--delete` 是上面 var 那條斷言的前提：沒有它，「第二段會刪掉白名單」的風險模型
    就不成立，而清不掉的舊檔會無限累積。
    uv.lock 由伺服器自己解（本機 macOS/py3.14 vs 伺服器 Linux/py3.11），少任一段
    都會靜默改動正式機的依賴版本——`uv sync` 會忠實照做，零錯誤訊息。
    """
    cmd = _rsync_commands()[seg]
    assert "--delete" in cmd.split(), (
        f"第 {seg + 1} 段 rsync 少了 --delete。伺服器上已刪除的檔案不會被清掉，"
        " 且 `--exclude var` 保護白名單的風險模型（見同檔 var 測試）不再成立。"
    )
    assert "uv.lock" in _excludes(cmd), (
        f"第 {seg + 1} 段 rsync 少了 `--exclude uv.lock`。本機與伺服器不是同一個解析"
        " 環境（macOS/py3.14 vs Linux/py3.11）：第一段少了它是本機 lock 進暫存區，"
        " 第二段少了它是 --delete 刪掉伺服器那份。兩者都會讓下次 uv sync 靜默改動"
        " 正式機的依賴版本，沒有任何錯誤訊息。"
    )


def test_first_rsync_excludes_git_and_second_deliberately_does_not():
    """`.git` 只在第一段排除——第二段刻意不排，讓伺服器上不留 git repo。

    第二段若也加 `--exclude .git`，伺服器會留下一份**永不更新**的舊 .git，
    `git log` 顯示錯誤的版本，比沒有更危險（讓人以為部署的是別的 commit）。
    RUNBOOK §3.2 明文寫了「不要用這個方式修」，這裡把它釘住。
    """
    first, second = _rsync_commands()
    assert ".git" in _excludes(first), (
        "第一段 rsync 少了 `--exclude .git`：整個 git 歷史（含曾經誤commit的東西）"
        " 會被推上正式機。"
    )
    assert ".git" not in _excludes(second), (
        "第二段 rsync 加了 `--exclude .git`。伺服器上沒有 git repo 是刻意的："
        " 排除它只會讓一份永不更新的舊 .git 留在正式機，git log 顯示錯誤的 commit，"
        " 比沒有更危險（RUNBOOK §3.2）。"
    )


# ══ 自行掃出：同類但同樣裸露的不變量 ═══════════════════════════════════════════

# 只有這兩個唯讀快照服務可以出貨具體 mainnet：它們只讀 HL 公開統計、不下單，
# 而 testnet 的 leader 績效是垃圾資料。任何**其他**部署檔出現具體 mainnet 都是事故。
_MAINNET_EXEMPT = {"filet-leaderboard.service", "filet-perf-series.service"}


def test_only_the_readonly_snapshot_units_may_ship_concrete_mainnet():
    """⭐⭐ 把不變量 1／3 從「兩個檔」升級成「整個 deploy/ 的掃描」。

    不變量 1 與 3 各自只釘一個檔——但下一個出事的會是**下一個新增的 unit**。
    這條改掃所有 unit／範本：出貨具體 mainnet 的檔必須在豁免名單裡，而豁免名單
    只裝「唯讀、不下單」的快照服務。新 unit 想進名單，得先解釋它為什麼不會下單。
    """
    offenders = []
    for path in sorted(DEPLOY.iterdir()):
        if path.suffix not in {".service", ".timer"} and not path.name.endswith(".example"):
            continue
        env = _unit_env(path.name) if path.suffix in {".service", ".timer"} \
            else _env_file_map(path.name)
        for key, val in env.items():
            if key.endswith("NETWORK") and val == "mainnet" \
                    and path.name not in _MAINNET_EXEMPT:
                offenders.append(f"{path.name}:{key}")
    assert not offenders, (
        f"這些部署檔出貨了具體 mainnet：{offenders}。只有唯讀快照服務"
        f"（{sorted(_MAINNET_EXEMPT)}，只讀 HL 公開統計、不下單）可以這樣做。"
        " 其餘任何 unit 硬編 mainnet ＝ 照 RUNBOOK 部署就把機器翻上主網對真錢下單。"
    )


def test_keysvc_allowed_uids_stays_a_placeholder():
    """⭐⭐ keysvc 的 ALLOWED_UIDS 必須是佔位符——具體 uid 是機器專屬的。

    這個值決定「**哪個 uid 可以叫 keysvc 產生 agent key**」。uid 在不同機器上會落到
    不同帳號：出貨一個具體數字，等於在別台機器上把 agent key 的產生權授給一個
    碰巧撞號的 process，而服務照常 active。
    """
    val = _unit_env("filet-keysvc.service")["FILET_KEYSVC_ALLOWED_UIDS"]
    assert not val.replace(",", "").strip().isdigit(), (
        f"filet-keysvc.service 出貨 FILET_KEYSVC_ALLOWED_UIDS={val!r}（具體 uid）。"
        " uid 是機器專屬的：在另一台機器上這個數字會落到別的帳號，等於把 agent key"
        " 的產生權授給碰巧撞號的 process，而服務照常 active、零告警。"
    )


_HARDENED_UNITS = [
    "filet-api.service", "filet-dashboard.service", "filet-follower@.service",
    "filet-keysvc.service", "filet-leaderboard.service", "filet-perf-series.service",
    "filet-daily-report.service",
]


@pytest.mark.parametrize("unit", _HARDENED_UNITS)
def test_every_unit_keeps_the_systemd_sandbox(unit):
    """⭐ 每個 unit 的 sandbox 三件套都不得消失。

    這三行是「被打穿的 process 能碰到什麼」的唯一邊界，而它們消失不會有任何症狀
    ——服務照常啟動、功能照常運作，只有事故發生時才會發現沙箱早就沒了。
    ProtectSystem 尤其要是 `strict`（`full` 只保護 /usr 與 /boot，/etc 仍可寫）。
    """
    text = _read(unit)
    for directive in ("NoNewPrivileges=true", "ProtectHome=true"):
        assert directive in text, (
            f"{unit} 少了 {directive}。sandbox 消失沒有任何症狀：服務照常 active、"
            " 功能照常，只有被打穿的那天才會發現邊界早就不在了。"
        )
    assert "ProtectSystem=strict" in text, (
        f"{unit} 的 ProtectSystem 不是 strict。`full` 只保護 /usr 與 /boot，"
        " /etc 仍可寫——而 /etc/filet/keys 就在那裡（agent 私鑰）。"
    )


def test_follower_cannot_write_the_exchange_directory_root():
    """⭐⭐ 引擎只能寫交換目錄底下的 engine/ 子通道，不能寫**根**。

    交換目錄根是 api→engine 單向的（API 寫客戶簽章的換 leader／資金設定記錄，
    引擎唯讀）。把根列進 ReadWritePaths（例如「修」一個權限錯誤時順手放寬），
    被打穿的引擎就能改寫客戶簽章紀錄——而功能面完全看不出差別。
    """
    rw = " ".join(_unit_directive("filet-follower@.service", "ReadWritePaths")).split()
    exchange_dir = _unit_env("filet-follower@.service")["FILET_EXCHANGE_DIR"]
    assert exchange_dir not in rw, (
        f"filet-follower@.service 把交換目錄根 {exchange_dir} 列入 ReadWritePaths。"
        " 那一層是 api→engine 單向（API 寫客戶簽章的換 leader／資金設定記錄、引擎唯讀）："
        " 放寬後被打穿的引擎可以改寫客戶簽章紀錄，而功能面完全看不出差別。"
        f" 引擎該寫的只有 {exchange_dir}/engine 子通道。"
    )
    assert f"{exchange_dir}/engine" in rw, (
        f"filet-follower@.service 的 ReadWritePaths 少了 {exchange_dir}/engine，"
        " 引擎寫不了健康心跳（面板會讀不到引擎狀態）。"
    )


def test_dashboard_binds_localhost_only():
    """⭐ dashboard 必須只綁 127.0.0.1，繞過反向代理就沒有 TLS 與安全 headers。

    綁 0.0.0.0 不會有任何錯誤：服務正常、nginx 也正常，只是 Next.js 同時多開了一個
    **無 TLS、無 HSTS／X-Frame-Options** 的門，而且從外面看不出來。
    """
    env = _unit_env("filet-dashboard.service")
    assert env["HOSTNAME"] == "127.0.0.1", (
        f"filet-dashboard.service 的 HOSTNAME={env['HOSTNAME']!r}，必須是 127.0.0.1。"
        " 綁對外位址等於在反向代理旁邊多開一個無 TLS、無安全 headers 的門，零症狀。"
    )
    exec_start = _unit_directive("filet-dashboard.service", "ExecStart")[0]
    assert "-H 127.0.0.1" in exec_start, (
        "filet-dashboard.service 的 ExecStart 少了顯式 `-H 127.0.0.1`。"
        " HOSTNAME 環境變數與 -H 旗標必須一致，否則哪一個生效取決於 next 的版本。"
    )


def test_nginx_upstream_port_matches_the_api_unit():
    """反向代理的 upstream port 必須等於 filet-api.service 宣告的 FILET_API_PORT。

    兩個檔各寫一次同一個數字，沒有共同的仲裁者。改了一邊 → 服務全部 active、
    journal 全部正常，只有外部連不到（nginx-filet.conf 自己註記過這個假故障）。
    """
    port = _unit_env("filet-api.service")["FILET_API_PORT"]
    nginx = _read("nginx-filet.conf")
    assert f"proxy_pass http://127.0.0.1:{port};" in nginx, (
        f"nginx-filet.conf 的 API upstream 不是 127.0.0.1:{port}"
        f"（filet-api.service 宣告 FILET_API_PORT={port}）。兩個檔各寫一次同一個數字、"
        " 沒有仲裁者：對不上的症狀是「服務全部正常但外部連不到」的假故障。"
    )


# ══ I-27（2026-09-02）：daily-report / api 的 accrued 歷史序列路徑必須同值＋落在放行內 ═
# 事故：daily-report 舊版硬編相對路徑 var/copytrade/accrued_history.jsonl，不在任何
# ReadWritePaths 底下 → ProtectSystem=strict 下每日 Read-only file system 失敗，
# 北極星／builder 合規／換 leader 對帳／營收告警全停兩天。

def test_accrued_history_path_matches_between_daily_report_and_api_units():
    """⭐⭐⭐ 兩個 unit 宣告的 FILET_ACCRUED_HISTORY_PATH 必須逐字元相同。

    日報寫、API 讀：不同源就是「日報寫到 A、API 讀 B」——API 讀到的檔案永遠是
    人工播種的舊值，零錯誤、零告警（工程原則 1：比較雙方必須同源）。
    """
    daily_val = _unit_env("filet-daily-report.service")["FILET_ACCRUED_HISTORY_PATH"]
    api_val = _unit_env("filet-api.service")["FILET_ACCRUED_HISTORY_PATH"]
    assert daily_val == api_val, (
        f"filet-daily-report.service 的 FILET_ACCRUED_HISTORY_PATH={daily_val!r} 與"
        f" filet-api.service 的 {api_val!r} 不同值。兩者必須逐字元相同，否則 API"
        " 讀到的歷史序列永遠不是日報實際寫入的那份（首頁路由量卡在舊值，零錯誤訊息）。"
    )


def test_accrued_history_path_is_writable_under_daily_report_read_write_paths():
    """⭐⭐⭐ 該路徑必須落在 daily-report unit 已放行的某條 ReadWritePaths 底下。

    這正是 I-27 事故的根因：`ProtectSystem=strict` 下，寫入任何**不在**
    `ReadWritePaths` 底下的路徑都會是 `OSError: Read-only file system`——服務進
    failed，但直到隔天報表沒生成才會被人發現。
    """
    path = _unit_env("filet-daily-report.service")["FILET_ACCRUED_HISTORY_PATH"]
    rw_paths = _unit_directive("filet-daily-report.service", "ReadWritePaths")
    assert any(path == rw or path.startswith(rw.rstrip("/") + "/") for rw in rw_paths), (
        f"filet-daily-report.service 的 FILET_ACCRUED_HISTORY_PATH={path!r} 不在任何"
        f" 已放行的 ReadWritePaths（{rw_paths}）底下。ProtectSystem=strict 下對它的寫入"
        " 會是 Read-only file system（I-27 事故：舊路徑 var/copytrade/accrued_history.jsonl"
        " 就是這樣掛掉兩天，北極星／builder 合規／換 leader 對帳／營收告警全停）。"
    )


def test_explore_cache_path_declared_and_writable_under_api_read_write_paths():
    """⭐ I-17 explore 磁碟快取路徑必須宣告、且落在 filet-api 的 ReadWritePaths 底下。

    2026-09-02 查明正式機從未宣告 FILET_EXPLORE_CACHE_PATH → 預設相對路徑
    var/copytrade/explore_index.json 在 ProtectSystem=strict 下落檔靜默失敗（只有一行
    warning）→ 每次 API 重啟 /explore 冷建空榜 ~12 分鐘，stale-while-revalidate 形同虛設。
    """
    path = _unit_env("filet-api.service").get("FILET_EXPLORE_CACHE_PATH")
    assert path and path.startswith("/"), (
        "filet-api.service 未宣告 FILET_EXPLORE_CACHE_PATH（或不是絕對路徑）——預設相對路徑"
        " 在 ProtectSystem=strict 下寫不進去，重啟後 /explore 空榜。")
    rw_paths = [p for line in _unit_directive("filet-api.service", "ReadWritePaths")
                for p in line.split()]
    assert any(path.startswith(rw.rstrip("/") + "/") for rw in rw_paths), (
        f"FILET_EXPLORE_CACHE_PATH={path!r} 不在 filet-api.service 的 ReadWritePaths"
        f"（{rw_paths}）底下——落檔會是 Read-only file system。")


def test_perf_series_failure_must_surface_as_a_failed_unit():
    """⭐ perf-series 的 ExecStart 不得加 `-` 前綴（與 leaderboard 的 best-effort 相反）。

    加了 `-`，systemd 會把失敗當成功：而這支腳本失敗代表**這個 12 小時窗的資料點
    永久消失**（perpDay 窗只保留 24 小時，補不回來）。靜默吞掉的是不可回填的資料。
    """
    execs = _unit_directive("filet-perf-series.service", "ExecStart")
    assert execs, "filet-perf-series.service 沒有 ExecStart"
    for cmd in execs:
        assert not cmd.startswith("-"), (
            f"filet-perf-series.service 的 ExecStart 帶了 `-` 前綴：{cmd!r}。"
            " systemd 會把失敗當成功，而這支腳本失敗代表這個 12 小時窗的資料點"
            " **永久消失**（perpDay 窗只留 24 小時，無法回填）。必須讓 unit 進 failed"
            " 被監控看見。"
        )
