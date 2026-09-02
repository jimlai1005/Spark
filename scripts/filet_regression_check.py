#!/usr/bin/env python3
"""Filet 全站回歸健康檢查——一條指令跑完本機測試、部署站台契約與伺服器結構性拓撲。

取代原本零散的手工 curl／ssh。設計原則：

1. **每條檢查都要說得出「預期什麼／實際什麼」**——只回 exit code 的健康檢查在出事時
   幫不上忙。失敗行一律印 expected / actual。
2. **分段可獨立跑**：`--local`（測試＋lint）／`--http`（唯讀 curl）／`--ssh`（結構性）。
   沒有 SSH 金鑰時前兩段仍可跑，SSH 段標記 SKIP 而不是假裝通過。
3. **SKIP 不等於 PASS**——摘要分開計數。一個「因為連不上所以全綠」的健康檢查等於沒有。
4. **唯讀為主**：唯一的寫入是交換目錄的權限探針（RUNBOOK §5.5.1 驗收 1-3b 的同一套
   做法），檔名固定前綴且結束一律清除。不碰資金、不讀 `.env*`、不印任何密鑰內容。
5. **unit 環境變數一律讀「有效值」，不 grep 主檔**——正式機常用 systemd drop-in
   （`/etc/systemd/system/<unit>.service.d/*.conf`）疊加設定；直接 `grep` unit 主檔
   在 drop-in 補上該變數時會誤判成「缺少」（2026-09-02 實測：`filet-api` 的
   `FILET_LEADERS_PATH`／`FILET_ACCRUED_HISTORY_PATH` 都是靠 drop-in 生效，主檔本身
   沒有這兩行）。一般 unit 用 `systemctl show <unit> -p Environment --value`；
   `filet-follower@` 這種未具體化的 template unit `systemctl show` 無法解析，改用
   `systemctl cat`（合併輸出，取同一個鍵最後一次宣告＝有效值）。

2026-09-02 對齊現行產品（plan T4）：HOST 改回正式網域、前端路由改成改版後集合、
新增 `systemctl --failed`／timer 新鮮度／公開 API 契約欄位／未授權 POST 401 等檢查。

用法：
    uv run python -m scripts.filet_regression_check              # 全部
    uv run python -m scripts.filet_regression_check --local      # 只跑本機
    uv run python -m scripts.filet_regression_check --http --ssh # 只跑遠端
    uv run python -m scripts.filet_regression_check --fast       # 跳過單元測試（只 lint）

Exit code：0＝全數通過（SKIP 不算失敗）；1＝有 FAIL。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WEB_APP_DIR = REPO / "web" / "src" / "app"

HOST = os.environ.get("FILET_RC_HOST", "trade.filet.app")
BASE = f"https://{HOST}"
SSH_HOST = os.environ.get("FILET_RC_SSH", "ubuntu@52.197.137.3")
SSH_KEY = os.environ.get(
    "FILET_RC_SSH_KEY",
    str(Path.home() / "Downloads" / "LightsailDefaultKey-ap-northeast-1-spark.pem"),
)
# 「網路模式必須是刻意的人工決策」——預期值可宣告（正式機現行為 mainnet；未來若真的
# 要開一台 testnet 驗證機，用這個 env 覆蓋，不必改程式碼）。
EXPECT_NETWORK = os.environ.get("FILET_RC_EXPECT_NETWORK", "mainnet")

# 現行前端路由集合（2026-08-29 改版後，見專案 CLAUDE.md「慣例」節與
# `docs/superpowers/plans/2026-09-02-golive-regression.md` T4）。`/leaderboard`
# （redirect）與 `/strategies/<slug>`（動態）另外檢查，不在這裡列。
FRONTEND_ROUTES = ["/", "/strategies", "/explore", "/advanced", "/docs", "/terms",
                   "/privacy", "/risk", "/status", "/onboarding", "/dashboard",
                   "/settings"]

# 需要 session 的 GET 端點：**必須 401**。500 代表沒認證就先炸（可能已碰到資料層），
# 200 代表閘門根本不在——兩者都比 401 嚴重得多，所以這裡不接受「非 200 就算過」。
SESSION_GET_ENDPOINTS = ["/api/me", "/api/me/capital", "/api/me/leader",
                         "/api/me/risk", "/api/me/dashboard", "/api/me/fees",
                         "/api/me/fills", "/api/me/authorizations", "/api/leaders",
                         "/api/ops/health", "/api/ops/revenue", "/api/admin/pending"]

# 需要 session 的 POST 端點：未授權一樣必須 401（不能因為換了個 HTTP method 就漏了
# 認證閘門——這正是「送出動作」的入口，比 GET 更該守住）。
SESSION_POST_ENDPOINTS = ["/api/me/pause", "/api/leaders/select", "/api/me/close-all"]

# 前端路由的「已知全集」——一致性測試用（`discover_page_routes` 對照）：
#   - FRONTEND_ROUTES：現行 200 檢查集合
#   - DYNAMIC_ROUTE_TEMPLATES：帶真實參數另外驗（不在這裡列固定路徑）
#   - REDIRECT_ROUTES：`/leaderboard` 另外驗 redirect 語意
#   - LEGACY_UNLISTED_ROUTES：2026-08-29 改版前遺留、CLAUDE.md 現行路由清單未列的
#     舊頁（`/admin` `/leaders` `/ops`）——檔案還在但不是現行產品的一部分，本腳本
#     刻意不對它們斷言 200/404，只是不能讓它們被誤判成「新頁面漏了檢查」。
# 新增頁面若沒被歸進任一類，`discover_page_routes()` 產生的集合就不會是這個全集的
# 子集，`tests/test_regression_check.py` 會抓到（「新增頁面沒加進檢查」自動 fail）。
DYNAMIC_ROUTE_TEMPLATES = {"/strategies/[slug]", "/traders/[address]"}
REDIRECT_ROUTES = {"/leaderboard"}
LEGACY_UNLISTED_ROUTES = {"/admin", "/leaders", "/ops"}
KNOWN_FRONTEND_ROUTES = (set(FRONTEND_ROUTES) | DYNAMIC_ROUTE_TEMPLATES
                        | REDIRECT_ROUTES | LEGACY_UNLISTED_ROUTES)

# `/api/public/strategies` 每個條目必須齊的欄位（`build_strategy_view` +
# `follower_count`/`as_of`，見 `src/spark/filet/strategies.py`）。
REQUIRED_STRATEGY_FIELDS = ["slug", "name", "tagline", "tagline_en", "featured",
                           "leader_address", "status", "listable", "live_days",
                           "min_notional_usd", "max_leverage", "metrics",
                           "follower_count", "as_of"]

REQUIRED_STATUS_FIELDS = ["status", "components", "updated_at"]

# `FILET_LEADERS_PATH` 應宣告的 unit（實測結果，2026-09-02——不是 plan 草稿寫的
# 「含 daily-report」：`scripts/filet_daily_report.py` 從不讀這個 env，daily-report
# 從沒宣告過它；真正的 5 個 unit 是 api/follower@/leaderboard/perf-series/
# auto-activate，逐一 `sudo systemctl show <unit> -p Environment` 實測核對過）。
LEADERS_PATH_UNITS = ["filet-api", "filet-leaderboard", "filet-perf-series",
                      "filet-auto-activate"]
LEADERS_PATH_TEMPLATE_UNITS = ["filet-follower@"]  # template unit，走 systemctl cat

# `FILET_ACCRUED_HISTORY_PATH`：日報寫、API 讀，兩邊必須逐字元同值（T0，工程原則 1）。
ACCRUED_HISTORY_UNITS = ["filet-api", "filet-daily-report"]

TIMER_UNITS = ["filet-leaderboard.timer", "filet-perf-series.timer",
              "filet-daily-report.timer", "filet-auto-activate.timer"]

TLS_MIN_DAYS = 14
PROBE_PREFIX = ".rc-probe"


@dataclass
class Result:
    section: str
    name: str
    status: str            # PASS / FAIL / SKIP
    expected: str = ""
    actual: str = ""
    note: str = ""


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    def add(self, section: str, name: str, ok: bool | None,
            expected: str = "", actual: str = "", note: str = "") -> None:
        status = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
        self.results.append(Result(section, name, status, expected, actual, note))
        mark = {"PASS": "  ok  ", "FAIL": " FAIL ", "SKIP": " skip "}[status]
        line = f"[{mark}] {name}"
        if status == "FAIL":
            line += f"\n           預期：{expected}\n           實際：{actual}"
        elif status == "SKIP" and note:
            line += f"  ({note})"
        elif status == "PASS" and actual:
            line += f"  → {actual}"
        print(line, flush=True)

    @property
    def failed(self) -> list[Result]:
        return [r for r in self.results if r.status == "FAIL"]

    @property
    def skipped(self) -> list[Result]:
        return [r for r in self.results if r.status == "SKIP"]


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


# ───────────────────────────── 純函式（離線可測） ─────────────────────────────

def sitemap_locs(xml_text: str) -> list[str]:
    """`sitemap.xml` 的 `<loc>...</loc>` 內容列表（順序保留）。"""
    return re.findall(r"<loc>(.*?)</loc>", xml_text)


def sitemap_hosts(xml_text: str) -> set[str]:
    """`sitemap.xml` 每個 `<loc>` 的 host 集合——正常只會有一個值；出現第二個或跟
    預期不同，代表 build 時 `NEXT_PUBLIC_SITE_ORIGIN` 沒對上目前的對外網域
    （RUNBOOK §4.2 的地雷：改 env 沒重 build）。"""
    hosts = set()
    for loc in sitemap_locs(xml_text):
        m = re.match(r"https?://([^/]+)", loc)
        if m:
            hosts.add(m.group(1))
    return hosts


def discover_page_routes(app_dir: Path) -> set[str]:
    """`web/src/app/**/page.tsx` → 路由字串集合（例：`admin/page.tsx` → `/admin`，
    `strategies/[slug]/page.tsx` → `/strategies/[slug]`，根目錄 `page.tsx` → `/`）。
    純檔案系統推導，不解析檔案內容，用於「新頁面有沒有被本腳本歸類」的一致性測試。
    """
    routes: set[str] = set()
    if not app_dir.is_dir():
        return routes
    for p in app_dir.rglob("page.tsx"):
        rel = p.relative_to(app_dir).parent
        routes.add("/" if str(rel) == "." else "/" + rel.as_posix())
    return routes


def parse_show_env_value(show_output: str, key: str) -> str | None:
    """`systemctl show <unit> -p Environment --value` 的單行輸出
    （`KEY1=VAL1 KEY2=VAL2 …`，空白分隔）解析出指定 key 的值。沒有該 key → None。
    """
    for tok in show_output.split():
        k, sep, v = tok.partition("=")
        if sep and k == key:
            return v
    return None


def parse_cat_env(cat_output: str, key: str) -> str | None:
    """`systemctl cat <unit>`（含 drop-in 的合併輸出）逐行找 `Environment=KEY=VALUE`，
    多次宣告取**最後一個**（systemd 對重複 `Environment=` 是疊加語意，後面的宣告對
    同一個鍵生效）。沒有任何宣告 → None。
    """
    prefix = f"Environment={key}="
    value: str | None = None
    for line in cat_output.splitlines():
        s = line.strip()
        if s.startswith(prefix):
            value = s[len(prefix):]
    return value


def parse_disk_pct(df_output: str) -> int | None:
    """`df --output=pcent /` 的輸出（含表頭行 `Use%` 與資料行 ` 13%`）解析出整數
    百分比；格式不對 → None（呼叫端視為 SKIP，不是假裝 0%）。
    """
    lines = [ln.strip() for ln in df_output.splitlines() if ln.strip()]
    if not lines:
        return None
    last = lines[-1].rstrip("%")
    try:
        return int(last)
    except ValueError:
        return None


def parse_finished_timestamp(line: str) -> datetime | None:
    """`journalctl -o short-iso` 單行（例：
    `2026-09-02T00:10:31+0000 host systemd[1]: Finished ...`）取開頭的 ISO-8601
    時間戳，轉成 tz-aware `datetime`。解析失敗 → None。
    """
    s = line.strip()
    if not s:
        return None
    token = s.split(" ", 1)[0]
    try:
        return datetime.fromisoformat(token)
    except ValueError:
        return None


def parse_freshness_output(output: str) -> tuple[datetime | None, datetime | None]:
    """SSH 指令 `journalctl -u X --since -2d -o short-iso | grep Finished | tail -1;
    date -u +%s` 的組合輸出 → `(finished_at, server_now)`，皆為 tz-aware UTC
    `datetime`（或 None）。最後一行恆為伺服器當下 epoch 秒；若還有前一行，那是最新
    一筆 `Finished` 記錄。兩天內查無 `Finished` → `finished_at is None`。
    """
    lines = [ln for ln in output.splitlines() if ln.strip()]
    if not lines:
        return None, None
    try:
        server_now = datetime.fromtimestamp(int(lines[-1].strip()), tz=timezone.utc)
    except ValueError:
        return None, None
    finished_at = parse_finished_timestamp(lines[0]) if len(lines) > 1 else None
    return finished_at, server_now


def parse_routed_volume(payload: dict) -> Decimal | None:
    """`/api/public/stats` 的 `routed_volume_usd_total`（字串或 null）→ `Decimal`。
    `null`／缺鍵／無法解析 → None（呼叫端一律視為「取不到」，不是 0）。
    """
    raw = payload.get("routed_volume_usd_total")
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return None


def explore_is_empty(payload: dict) -> bool:
    """`/api/public/explore` 的 `rows` 是否為空榜（含 `rows` 缺鍵的防禦性判斷）。"""
    return not payload.get("rows")


def strategies_missing_fields(entry: dict) -> list[str]:
    """`/api/public/strategies` 單一策略條目缺少哪些必要欄位。"""
    return [k for k in REQUIRED_STRATEGY_FIELDS if k not in entry]


# ───────────────────────────── 本機段 ─────────────────────────────

def section_local(rep: Report, fast: bool) -> None:
    print("\n=== LOCAL：單元測試與 lint ===")

    if fast:
        rep.add("local", "後端 pytest", None, note="--fast 略過")
        rep.add("local", "前端 vitest", None, note="--fast 略過")
    else:
        p = run(["uv", "run", "pytest", "-q", "--no-header"], cwd=REPO)
        tail = next((ln for ln in reversed(p.stdout.splitlines())
                     if "passed" in ln or "failed" in ln or "error" in ln), "(無輸出)")
        rep.add("local", "後端 pytest", p.returncode == 0,
                "全數通過（exit 0）", tail.strip())

        node_bin = os.environ.get("FILET_RC_NODE_BIN",
                                  str(Path.home() / ".nvm/versions/node/v24.18.0/bin"))
        env = dict(os.environ, PATH=f"{node_bin}:{os.environ['PATH']}")
        web = REPO / "web"
        if not (web / "node_modules").is_dir():
            rep.add("local", "前端 vitest", None, note="web/node_modules 不存在，先跑 npm ci")
        else:
            p = subprocess.run(["npm", "test"], cwd=web, capture_output=True,
                               text=True, timeout=1800, env=env)
            out = p.stdout + p.stderr
            tail = next((ln.strip() for ln in reversed(out.splitlines())
                         if "Tests " in ln), "(無摘要)")
            rep.add("local", "前端 vitest", p.returncode == 0,
                    "全數通過（exit 0）", tail)

    p = run(["uv", "run", "ruff", "check", "src", "tests", "scripts"], cwd=REPO)
    rep.add("local", "ruff lint", p.returncode == 0, "All checks passed",
            (p.stdout.strip().splitlines() or ["(無輸出)"])[-1])


# ───────────────────────────── HTTP 段 ─────────────────────────────

def http_status(path: str, timeout: int = 20) -> tuple[int | None, str]:
    """回 (status_code, body_or_error)。4xx/5xx 不拋，照樣回 code。"""
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "filet-regression-check"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(4096).decode("utf-8", "replace")
    except Exception as e:                                  # 連不上／TLS 失敗
        return None, f"{type(e).__name__}: {e}"


def http_status_headers(path: str, timeout: int = 20) -> tuple[int | None, dict]:
    """回 (status_code, headers)。用於需要看 response header（HSTS）的檢查。"""
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "filet-regression-check"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers)
    except Exception:
        return None, {}


def http_status_no_redirect(path: str, timeout: int = 20) -> tuple[int | None, str]:
    """回 (status_code, location_or_error)——不跟隨 redirect，用於驗證某條路由
    「本身就是」30x（例如 `/leaderboard`）。"""
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "filet-regression-check"})
    opener = urllib.request.build_opener(NoRedirect())
    try:
        r = opener.open(req, timeout=timeout)
        return r.status, r.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location", "")
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def http_post_status(path: str, timeout: int = 20) -> tuple[int | None, str]:
    """未授權 POST（帶最小合法 JSON body，避免因為 body 缺欄位而先被 422 擋掉，
    掩蓋掉真正要驗的「有沒有先擋 401」）。"""
    req = urllib.request.Request(
        BASE + path, data=b"{}", method="POST",
        headers={"User-Agent": "filet-regression-check",
                "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(4096).decode("utf-8", "replace")
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def section_http(rep: Report) -> None:
    print(f"\n=== HTTP：部署站台 {BASE}（唯讀）===")

    # 1. 現行前端路由（皆需 200）
    for path in FRONTEND_ROUTES:
        code, body = http_status(path)
        rep.add("http", f"路由 {path}", code == 200, "HTTP 200",
                f"HTTP {code}" if code else body)

    # 1b. /leaderboard 必須導向 /explore。現行實作是 client-side `router.replace`
    #     （web/src/app/leaderboard/page.tsx），不是伺服器層 30x——2026-09-02 主線程
    #     裁決接受現況（該路由已無任何導覽入口，只服務舊書籤），所以兩種形態都算過：
    #     (a) 30x 且 Location 含 /explore；(b) 200 且 HTML 內含 "/explore"（client
    #     redirect 的目的地字串）。200 但 HTML 完全不提 /explore ＝ 轉址被拿掉，FAIL。
    code, loc = http_status_no_redirect("/leaderboard")
    if code in (301, 302, 307, 308):
        ok_lb, actual_lb = "/explore" in loc, f"HTTP {code} Location={loc or '(無)'}"
    else:
        _c, body_lb = http_status("/leaderboard")
        ok_lb = _c == 200 and "/explore" in body_lb
        actual_lb = f"HTTP {_c}（client-side redirect，HTML {'含' if '/explore' in body_lb else '不含'} /explore）"
    rep.add("http", "/leaderboard 導向 /explore（30x 或 client-side）", ok_lb,
            "30x Location 含 /explore，或 200 且 HTML 含 /explore", actual_lb)

    # 1c. /strategies/<slug>（slug 取自 /api/public/strategies 第一筆）
    code, body = http_status("/api/public/strategies")
    slug = None
    if code == 200:
        try:
            strategies = json.loads(body).get("strategies", [])
            if strategies:
                slug = strategies[0].get("slug")
        except json.JSONDecodeError:
            pass
    if slug is None:
        rep.add("http", "/strategies/<slug> 200", None,
                note="/api/public/strategies 沒有可用的第一筆 slug")
    else:
        code, _ = http_status(f"/strategies/{slug}")
        rep.add("http", f"/strategies/{slug} 200", code == 200, "HTTP 200",
                f"HTTP {code}")

    # 2. robots.txt / sitemap.xml
    code, body = http_status("/robots.txt")
    rep.add("http", "/robots.txt 200", code == 200, "HTTP 200", f"HTTP {code}")

    code, body = http_status("/sitemap.xml")
    if code != 200:
        rep.add("http", "/sitemap.xml 200", False, "HTTP 200", f"HTTP {code}")
    else:
        hosts = sitemap_hosts(body)
        rep.add("http", "sitemap.xml host 與 HOST 一致（NEXT_PUBLIC_SITE_ORIGIN 有重 build）",
                hosts == {HOST}, f"{{{HOST!r}}}", f"{hosts or '(無 <loc>)'}")

    # 3. TLS：有效、非自簽、未接近到期；HSTS header 存在
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(socket.create_connection((HOST, 443), timeout=20),
                             server_hostname=HOST) as s:
            cert = s.getpeercert()
        issuer = dict(x[0] for x in cert["issuer"])
        subject = dict(x[0] for x in cert["subject"])
        issuer_cn = issuer.get("organizationName") or issuer.get("commonName", "?")
        subject_cn = subject.get("commonName", "?")
        not_after = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(
            tzinfo=timezone.utc)
        days = (not_after - datetime.now(timezone.utc)).days
        # 鏈驗證已由 create_default_context 完成（自簽會在 wrap_socket 就拋）。
        # 這裡再明確檢查 issuer != subject，把「自簽但被信任」的情況也擋掉。
        self_signed = (issuer.get("commonName") == subject_cn)
        rep.add("http", "TLS 憑證有效且非自簽", not self_signed,
                "由外部 CA 簽發（issuer != subject）",
                f"issuer={issuer_cn} subject={subject_cn}")
        rep.add("http", f"TLS 到期日 > {TLS_MIN_DAYS} 天", days > TLS_MIN_DAYS,
                f"剩餘 > {TLS_MIN_DAYS} 天",
                f"{not_after:%Y-%m-%d}（剩 {days} 天）")
    except Exception as e:
        rep.add("http", "TLS 憑證", False, "可建立受信任的 TLS 連線",
                f"{type(e).__name__}: {e}")

    code, headers = http_status_headers("/")
    hsts = headers.get("Strict-Transport-Security")
    rep.add("http", "HSTS header 存在", bool(hsts), "Strict-Transport-Security 存在",
            hsts or "(缺失)")

    # 4. 公開 API 契約
    code, body = http_status("/api/public/stats")
    if code != 200:
        rep.add("http", "/api/public/stats 契約", False, "HTTP 200", f"HTTP {code}: {body[:200]}")
    else:
        try:
            data = json.loads(body)
            volume = parse_routed_volume(data)
            rep.add("http", "/api/public/stats routed_volume_usd_total > 0",
                    volume is not None and volume > 0,
                    "非 null 且 > 0",
                    f"{volume}" if volume is not None else "null（取不到／未累積）")
        except json.JSONDecodeError as e:
            rep.add("http", "/api/public/stats 契約", False, "合法 JSON", str(e))

    code, body = http_status("/api/public/status")
    if code != 200:
        rep.add("http", "/api/public/status 契約", False, "HTTP 200", f"HTTP {code}: {body[:200]}")
    else:
        try:
            data = json.loads(body)
            missing = [k for k in REQUIRED_STATUS_FIELDS if k not in data]
            rep.add("http", "/api/public/status 契約欄位", not missing,
                    "含 " + "/".join(REQUIRED_STATUS_FIELDS),
                    f"缺少：{missing}" if missing else "齊全")
        except json.JSONDecodeError as e:
            rep.add("http", "/api/public/status 契約", False, "合法 JSON", str(e))

    code, body = http_status("/api/public/strategies")
    if code != 200:
        rep.add("http", "/api/public/strategies 契約", False, "HTTP 200", f"HTTP {code}: {body[:200]}")
    else:
        try:
            data = json.loads(body)
            strategies = data.get("strategies", [])
            rep.add("http", "/api/public/strategies ≥ 1 條", len(strategies) >= 1,
                    "≥ 1 條", f"{len(strategies)} 條")
            missing_all: list[str] = []
            for entry in strategies:
                missing_all.extend(f"{entry.get('slug')}.{k}"
                                   for k in strategies_missing_fields(entry))
            rep.add("http", "/api/public/strategies 每筆欄位齊全", not missing_all,
                    "每筆皆含 " + "/".join(REQUIRED_STRATEGY_FIELDS),
                    f"缺少：{missing_all}" if missing_all else "齊全")
        except json.JSONDecodeError as e:
            rep.add("http", "/api/public/strategies 契約", False, "合法 JSON", str(e))

    code, body = http_status("/api/public/benchmarks")
    if code != 200:
        rep.add("http", "/api/public/benchmarks 契約", False, "HTTP 200", f"HTTP {code}: {body[:200]}")
    else:
        try:
            data = json.loads(body)
            missing = [k for k in ("series", "updated_at") if k not in data]
            rep.add("http", "/api/public/benchmarks 契約欄位", not missing,
                    "含 series/updated_at", f"缺少：{missing}" if missing else "齊全")
        except json.JSONDecodeError as e:
            rep.add("http", "/api/public/benchmarks 契約", False, "合法 JSON", str(e))

    code, body = http_status("/api/public/explore")
    if code != 200:
        rep.add("http", "/api/public/explore 契約", False, "HTTP 200", f"HTTP {code}: {body[:200]}")
    else:
        try:
            data = json.loads(body)
            empty = explore_is_empty(data)
            note = f"（building={data.get('building')}）" if empty else ""
            rep.add("http", "/api/public/explore 非空榜", not empty,
                    "rows ≥ 1", f"rows 為空{note}" if empty else f"{len(data['rows'])} rows")
        except json.JSONDecodeError as e:
            rep.add("http", "/api/public/explore 契約", False, "合法 JSON", str(e))

    # 5. /api/billing/plans（沿用既有契約，含機密欄位零外洩）
    code, body = http_status("/api/billing/plans")
    if code != 200:
        rep.add("http", "/api/billing/plans 契約", False, "HTTP 200 + JSON",
                f"HTTP {code}: {body[:200]}")
    else:
        try:
            data = json.loads(body)
            missing: list[str] = []
            if "billing_enabled" not in data:
                missing.append("billing_enabled")
            plans = data.get("plans", [])
            ids = {p.get("id") for p in plans}
            for want in ("free", "pro"):
                if want not in ids:
                    missing.append(f"plans[id={want}]")
            for p in plans:
                for k in ("id", "name_key", "price_display", "features", "purchasable"):
                    if k not in p:
                        missing.append(f"plans[{p.get('id')}].{k}")
                for feat in p.get("features", []):
                    for k in ("text_key", "included", "shipped"):
                        if k not in feat:
                            missing.append(f"plans[{p.get('id')}].features[].{k}")
            # ⭐ 誠信欄位：price_id／customer_id／subscription_id 絕不可外洩到公開端點
            leaked = [k for k in ("price_id", "customer_id", "subscription_id")
                      if k in body]
            rep.add("http", "/api/billing/plans 契約欄位", not missing,
                    "含 billing_enabled + free/pro 方案 + 每個 feature 的 "
                    "text_key/included/shipped",
                    "缺少：" + ", ".join(sorted(set(missing))) if missing else "齊全")
            rep.add("http", "/api/billing/plans 不外洩 Stripe 識別碼", not leaked,
                    "回應不含 price_id/customer_id/subscription_id",
                    f"外洩：{leaked}" if leaked else "無外洩")
        except json.JSONDecodeError as e:
            rep.add("http", "/api/billing/plans 契約", False, "合法 JSON", str(e))

    # 6. 需 session 的 GET 端點必須 401（不是 500、不是 200）
    for path in SESSION_GET_ENDPOINTS:
        code, body = http_status(path)
        rep.add("http", f"未授權 GET {path} → 401", code == 401,
                "HTTP 401（閘門在且乾淨拒絕）",
                f"HTTP {code}" + (" ⚠️ 200＝閘門不存在" if code == 200 else
                                  " ⚠️ 500＝認證前就炸" if code == 500 else ""))

    # 6b. 需 session 的 POST 端點也必須 401
    for path in SESSION_POST_ENDPOINTS:
        code, body = http_post_status(path)
        rep.add("http", f"未授權 POST {path} → 401", code == 401,
                "HTTP 401（閘門在且乾淨拒絕）",
                f"HTTP {code}" + (" ⚠️ 200＝閘門不存在" if code == 200 else
                                  " ⚠️ 500＝認證前就炸" if code == 500 else
                                  " ⚠️ 422＝body 驗證跑在認證前面" if code == 422 else ""))

    # 7. http → https 轉址
    try:
        req = urllib.request.Request(f"http://{HOST}/",
                                     headers={"User-Agent": "filet-regression-check"})
        opener = urllib.request.build_opener(NoRedirect())
        try:
            r = opener.open(req, timeout=20)
            code, loc = r.status, r.headers.get("Location", "")
        except urllib.error.HTTPError as e:
            code, loc = e.code, e.headers.get("Location", "")
        rep.add("http", "http → https 轉址", code in (301, 308) and loc.startswith("https://"),
                "301/308 且 Location 為 https://", f"HTTP {code} Location={loc or '(無)'}")
    except Exception as e:
        rep.add("http", "http → https 轉址", False, "301/308", f"{type(e).__name__}: {e}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):    # noqa: D102
        return None


# ───────────────────────────── SSH 段 ─────────────────────────────

def ssh(script: str, timeout: int = 120) -> tuple[int, str]:
    p = run(["ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
             "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
             SSH_HOST, script], timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()


def _effective_env(unit: str, key: str, *, template: bool = False) -> tuple[str | None, str]:
    """回 (有效值, 診斷字串)。`template=True` 用 `systemctl cat`（適用未具體化的
    template unit，例如 `filet-follower@`）；否則用 `systemctl show -p Environment
    --value`（一般 unit，涵蓋 drop-in 疊加後的有效值）。ssh 本身失敗 → (None, 原因)。
    """
    if template:
        rc, out = ssh(f"sudo systemctl cat {unit} 2>&1")
        if rc != 0:
            return None, f"systemctl cat 失敗：{out[:160]}"
        return parse_cat_env(out, key), out[:160]
    rc, out = ssh(f"systemctl show {unit} -p Environment --value 2>&1")
    if rc != 0:
        return None, f"systemctl show 失敗：{out[:160]}"
    return parse_show_env_value(out, key), out[:160]


def section_ssh(rep: Report) -> None:
    print(f"\n=== SSH：伺服器結構性檢查 {SSH_HOST} ===")

    if not Path(SSH_KEY).is_file():
        rep.add("ssh", "SSH 可達性", None, note=f"金鑰不存在：{SSH_KEY}")
        return
    rc, out = ssh("echo SSH_OK")
    if rc != 0 or "SSH_OK" not in out:
        rep.add("ssh", "SSH 可達性", None, note=f"連線失敗：{out[:160]}")
        return
    rep.add("ssh", "SSH 可達性", True, "可連線", "SSH_OK")

    # --- ⭐ 沒有任何 failed unit（filet-daily-report 兩天沒跑成功的樣板事故就是
    #     這條檢查該抓到、卻因為原本沒有這條檢查而漏掉兩天）---
    rc, out = ssh("systemctl --failed --no-legend")
    failed_lines = [ln for ln in out.splitlines() if ln.strip()]
    rep.add("ssh", "⭐ systemctl --failed 為空（無失敗的 unit）", not failed_lines,
            "無任何 failed unit",
            "; ".join(failed_lines) if failed_lines else "(無)")

    # --- 服務與 timer ---
    rc, out = ssh("systemctl is-active filet-api filet-dashboard filet-keysvc")
    states = out.split()
    rep.add("ssh", "三個常駐服務 active",
            states == ["active"] * 3, "filet-api/dashboard/keysvc 皆 active",
            " ".join(states) or "(無輸出)")

    # 僅列示：目前有幾個 filet-follower@ 實例在跑，供人工核對數量是否符合預期
    # （沒有固定閾值——follower 數量隨客戶增減，不是本腳本能判斷對錯的量）。
    rc, out = ssh("systemctl list-units 'filet-follower@*' --state=active "
                  "--no-legend --plain --no-pager | wc -l")
    count = out.strip().splitlines()[-1].strip() if out.strip() else "?"
    rep.add("ssh", "filet-follower@* 現有 active 實例數（僅列示）", True,
            "無判準，供人工核對", f"{count} 個")

    rc, out = ssh(f"systemctl is-enabled {' '.join(TIMER_UNITS)}")
    states = out.split()
    rep.add("ssh", f"四個 timer 皆 enabled（{', '.join(TIMER_UNITS)}）",
            states == ["enabled"] * len(TIMER_UNITS),
            "皆 enabled", " ".join(states) or "(無輸出)")

    # --- timer 新鮮度：daily-report/leaderboard 26h 內，perf-series 13h 內
    #     （filet-daily-report 兩天故障沒被抓到的第二道防線——即使 --failed 剛好
    #     被清過〔重啟過 unit〕，這條仍會抓到「最近一次成功執行是多久以前」）---
    def _check_freshness(unit: str, max_hours: float) -> None:
        rc, out = ssh(f"sudo journalctl -u {unit} --since -2d -o short-iso "
                      f"--no-pager | grep Finished | tail -1; date -u +%s")
        if rc != 0:
            rep.add("ssh", f"{unit} 最近一次 Finished 在 {max_hours:g}h 內", None,
                    note=f"journalctl 失敗：{out[:160]}")
            return
        finished_at, server_now = parse_freshness_output(out)
        if server_now is None:
            rep.add("ssh", f"{unit} 最近一次 Finished 在 {max_hours:g}h 內", None,
                    note="無法解析伺服器時間")
            return
        if finished_at is None:
            rep.add("ssh", f"{unit} 最近一次 Finished 在 {max_hours:g}h 內", False,
                    f"≤ {max_hours:g}h", "過去 2 天內查無 Finished 記錄")
            return
        hours = (server_now - finished_at).total_seconds() / 3600
        rep.add("ssh", f"{unit} 最近一次 Finished 在 {max_hours:g}h 內",
                hours <= max_hours, f"≤ {max_hours:g}h",
                f"{hours:.1f}h 前（{finished_at.isoformat()}）")

    _check_freshness("filet-daily-report", 26)
    _check_freshness("filet-leaderboard", 26)
    _check_freshness("filet-perf-series", 13)

    # --- 網路模式：測試機翻上主網是已發生過的地雷 ---
    net, diag = _effective_env("filet-api", "FILET_API_NETWORK")
    if net is None and "失敗" in diag:
        rep.add("ssh", f"FILET_API_NETWORK={EXPECT_NETWORK}", None, note=diag)
    else:
        rep.add("ssh", f"FILET_API_NETWORK={EXPECT_NETWORK}", net == EXPECT_NETWORK,
                f"{EXPECT_NETWORK}（網路模式必須是刻意的人工決策，"
                "可用 FILET_RC_EXPECT_NETWORK 覆蓋預期值）",
                net or "(未宣告)")

    # --- FILET_LEADERS_PATH：5 個 unit 皆須宣告且同值（同源，非各自推導）---
    values: dict[str, str | None] = {}
    diag_msgs: list[str] = []
    for u in LEADERS_PATH_UNITS:
        v, d = _effective_env(u, "FILET_LEADERS_PATH")
        values[u] = v
        if v is None:
            diag_msgs.append(f"{u}: {d}")
    for u in LEADERS_PATH_TEMPLATE_UNITS:
        v, d = _effective_env(u, "FILET_LEADERS_PATH", template=True)
        values[u] = v
        if v is None:
            diag_msgs.append(f"{u}: {d}")
    missing_units = [u for u, v in values.items() if v is None]
    distinct = {v for v in values.values() if v is not None}
    ok = not missing_units and len(distinct) == 1
    rep.add("ssh", "FILET_LEADERS_PATH 五個 unit 皆宣告且同值（有效值，含 drop-in）",
            ok, "五個 unit 皆有且逐字元相同",
            (f"缺少：{missing_units}；" if missing_units else "") +
            (f"值不一致：{values}" if len(distinct) > 1 else
             (next(iter(distinct)) if distinct else "(皆缺)")))

    # --- FILET_ACCRUED_HISTORY_PATH：api 與 daily-report 同值（T0）---
    acc_values: dict[str, str | None] = {}
    for u in ACCRUED_HISTORY_UNITS:
        v, d = _effective_env(u, "FILET_ACCRUED_HISTORY_PATH")
        acc_values[u] = v
    acc_missing = [u for u, v in acc_values.items() if v is None]
    acc_distinct = {v for v in acc_values.values() if v is not None}
    rep.add("ssh", "FILET_ACCRUED_HISTORY_PATH 於 api/daily-report 兩 unit 同值（T0）",
            not acc_missing and len(acc_distinct) == 1,
            "兩個 unit 皆有且逐字元相同",
            (f"缺少：{acc_missing}；" if acc_missing else "") +
            (f"值不一致：{acc_values}" if len(acc_distinct) > 1 else
             (next(iter(acc_distinct)) if acc_distinct else "(皆缺)")))

    # --- var/filet/reports 目錄必須 filet-engine:filet-api 2750（setgid，
    #     日報寫、API 讀共用同一組 group 權限）---
    rc, out = ssh("sudo stat -c '%U:%G %a %n' /opt/filet/spark/var/filet/reports 2>&1")
    want_reports_dir = "filet-engine:filet-api 2750 /opt/filet/spark/var/filet/reports"
    rep.add("ssh", "var/filet/reports 為 filet-engine:filet-api 2750（setgid）",
            out.strip() == want_reports_dir, want_reports_dir, out.strip() or "(無輸出)")

    # --- filet-api 讀得到 FILET_ACCRUED_HISTORY_PATH 指向的檔案（讀不到＝首頁
    #     路由量靜默變 null，2026-09-02 實際發生過）---
    api_accrued_path = acc_values.get("filet-api")
    if api_accrued_path is None:
        rep.add("ssh", "filet-api 讀得到 FILET_ACCRUED_HISTORY_PATH 指向的檔案", None,
                note="上一條取不到有效路徑，無法測讀取")
    else:
        rc, out = ssh(f"sudo -u filet-api test -r {shlex.quote(api_accrued_path)} "
                      f"&& echo READABLE || echo DENIED")
        rep.add("ssh", "filet-api 讀得到 FILET_ACCRUED_HISTORY_PATH 指向的檔案",
                "READABLE" in out, "READABLE", out.strip() or "(無輸出)")

    # --- FILET_EXPLORE_CACHE_PATH（I-17 磁碟快取）：必須宣告、落在 filet-api 的
    #     ReadWritePaths 底下、且父目錄可寫。2026-09-02 查明正式機從未宣告 → 預設相對
    #     路徑 var/copytrade/ 在 ProtectSystem=strict 下 Read-only → 快照落檔靜默失敗
    #     → 每次 API 重啟 /explore 空榜 ~12 分鐘（stale-while-revalidate 形同虛設）。---
    cache_path, d = _effective_env("filet-api", "FILET_EXPLORE_CACHE_PATH")
    rw_rc, rw_out = ssh("systemctl show filet-api -p ReadWritePaths --value")
    rw_paths = [p for p in rw_out.split() if p.startswith("/")]
    if cache_path is None:
        rep.add("ssh", "FILET_EXPLORE_CACHE_PATH 已宣告且落在 ReadWritePaths 底下", False,
                "有效環境含 FILET_EXPLORE_CACHE_PATH（絕對路徑）",
                f"未宣告（{d}）→ 預設相對路徑，重啟後 /explore 冷建空榜")
    else:
        under_rw = any(cache_path.startswith(p.rstrip("/") + "/") for p in rw_paths)
        rep.add("ssh", "FILET_EXPLORE_CACHE_PATH 已宣告且落在 ReadWritePaths 底下",
                cache_path.startswith("/") and under_rw,
                f"絕對路徑且以 {rw_paths} 之一為前綴", cache_path)
        parent = cache_path.rsplit("/", 1)[0]
        rc, out = ssh(f"sudo -u filet-api test -w {shlex.quote(parent)} "
                      f"&& echo WRITABLE || echo DENIED; "
                      f"sudo test -f {shlex.quote(cache_path)} && echo EXISTS || echo ABSENT")
        rep.add("ssh", "explore 快取父目錄 filet-api 可寫（快照檔存在與否另列）",
                "WRITABLE" in out, "WRITABLE", out.replace("\n", " ").strip() or "(無輸出)")

    # --- 交換目錄雙通道方向性（RUNBOOK §5.5.1）---
    rc, out = ssh(
        "sudo stat -c '%U:%G %a %n' /var/lib/filet-exchange "
        "/var/lib/filet-exchange/engine /var/lib/filet-exchange/engine/health 2>&1")
    want = ["filet-api:filet-engine 750 /var/lib/filet-exchange",
            "filet-engine:filet-api 750 /var/lib/filet-exchange/engine",
            "filet-engine:filet-api 750 /var/lib/filet-exchange/engine/health"]
    got = out.splitlines()
    rep.add("ssh", "交換目錄三層 owner/group/mode（第 2、3 層對調）",
            got == want, " | ".join(want), " | ".join(got) or "(無輸出)")

    # ⭐ 承重點：引擎**寫不進**交換目錄根目錄（單向性）。寫得進＝方向性失效，
    #    引擎可偽造客戶簽章記錄。這是整個換 leader 信任鏈最關鍵的一條。
    rc, out = ssh(
        f"sudo -u filet-engine touch /var/lib/filet-exchange/{PROBE_PREFIX}-engine "
        f"2>&1 && echo WROTE || echo DENIED; "
        f"sudo rm -f /var/lib/filet-exchange/{PROBE_PREFIX}-engine")
    rep.add("ssh", "⭐ 引擎寫不進交換目錄根目錄（單向性）", "DENIED" in out,
            "Permission denied", "可寫入（方向性失效）" if "WROTE" in out else "Permission denied")

    # api→engine 通道實際打通（不是同名的兩個檔）
    rc, out = ssh(
        f"sudo -u filet-api touch /var/lib/filet-exchange/{PROBE_PREFIX}-api 2>&1 "
        f"&& sudo -u filet-engine test -r /var/lib/filet-exchange/{PROBE_PREFIX}-api "
        f"&& echo CHANNEL_OK || echo CHANNEL_BROKEN; "
        f"sudo rm -f /var/lib/filet-exchange/{PROBE_PREFIX}-api")
    rep.add("ssh", "api→engine 通道：api 可寫且 engine 讀得到同一個檔",
            "CHANNEL_OK" in out, "CHANNEL_OK",
            "CHANNEL_BROKEN（客戶按了永遠不生效）" if "CHANNEL_BROKEN" in out else "CHANNEL_OK")

    # engine→api 通道（心跳，方向相反）
    rc, out = ssh(
        f"sudo -u filet-engine touch /var/lib/filet-exchange/engine/health/{PROBE_PREFIX}-hb "
        f"2>&1 && sudo -u filet-api test -r "
        f"/var/lib/filet-exchange/engine/health/{PROBE_PREFIX}-hb "
        f"&& echo CHANNEL_OK || echo CHANNEL_BROKEN; "
        f"sudo rm -f /var/lib/filet-exchange/engine/health/{PROBE_PREFIX}-hb")
    rep.add("ssh", "engine→api 通道：engine 可寫心跳且 api 讀得到",
            "CHANNEL_OK" in out, "CHANNEL_OK",
            "CHANNEL_BROKEN（面板永遠未知）" if "CHANNEL_BROKEN" in out else "CHANNEL_OK")

    # --- leader 白名單：承重點是「filet-api 寫不到」---
    rc, out = ssh("sudo stat -c '%U:%G %a' /opt/filet/spark/var/filet/leaders.json 2>&1")
    rep.add("ssh", "白名單 leaders.json 為 root:root 644",
            out.strip() == "root:root 644", "root:root 644", out.strip() or "(不存在)")

    rc, out = ssh("sudo -u filet-api test -w /opt/filet/spark/var/filet/leaders.json "
                  "&& echo WRITABLE || echo READONLY")
    rep.add("ssh", "⭐ filet-api 寫不到白名單（被打穿也改不了名單）",
            "READONLY" in out, "READONLY", out.strip())

    # --- 機密檔不在伺服器上（rsync exclude 清單同時是機密邊界）---
    # 排除 .venv：certifi 的 cacert.pem 是公開 CA bundle，不是機密。
    rc, out = ssh(r"sudo find /opt/filet/spark -path '*/.venv' -prune -o "
                  r"\( -name '.env' -o -name '.env.*' -o -name '*.key' -o -name '*.pem' \) "
                  r"-print 2>/dev/null | head -20")
    found = [ln for ln in out.splitlines() if ln.strip()]
    rep.add("ssh", "部署樹內無機密檔（.env / *.key / *.pem）", not found,
            "零命中", f"命中 {len(found)} 個：{found[:5]}" if found else "零命中")

    # --- 磁碟使用率 ---
    rc, out = ssh("df --output=pcent / | tail -1")
    pct = parse_disk_pct(out)
    if pct is None:
        rep.add("ssh", "磁碟使用率 < 85%", None, note=f"無法解析 df 輸出：{out[:80]}")
    else:
        rep.add("ssh", "磁碟使用率 < 85%", pct < 85, "< 85%", f"{pct}%")


# ───────────────────────────── 主流程 ─────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Filet 全站回歸健康檢查")
    ap.add_argument("--local", action="store_true", help="只跑本機測試與 lint")
    ap.add_argument("--http", action="store_true", help="只跑部署站台唯讀檢查")
    ap.add_argument("--ssh", action="store_true", help="只跑伺服器結構性檢查")
    ap.add_argument("--fast", action="store_true", help="本機段跳過單元測試（只 lint）")
    args = ap.parse_args()

    want_all = not (args.local or args.http or args.ssh)
    rep = Report()

    print(f"Filet 回歸檢查 — {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC")
    print(f"站台 {BASE}｜repo {REPO}")

    if want_all or args.local:
        section_local(rep, args.fast)
    if want_all or args.http:
        section_http(rep)
    if want_all or args.ssh:
        section_ssh(rep)

    total = len(rep.results)
    n_fail, n_skip = len(rep.failed), len(rep.skipped)
    n_pass = total - n_fail - n_skip

    print("\n" + "=" * 62)
    print(f"總計 {total} 條： {n_pass} 通過 / {n_fail} 失敗 / {n_skip} 略過")
    if n_skip:
        print("\n略過（**不等於通過**）：")
        for r in rep.skipped:
            print(f"  - [{r.section}] {r.name}：{r.note}")
    if n_fail:
        print("\n失敗明細：")
        for r in rep.failed:
            print(f"  ✗ [{r.section}] {r.name}")
            print(f"      預期：{r.expected}")
            print(f"      實際：{r.actual}")
        print("\n結論：FAIL")
        return 1
    print("\n結論：PASS" + ("（但有略過項，非完整證明）" if n_skip else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
