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
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

HOST = os.environ.get("FILET_RC_HOST", "52-197-137-3.sslip.io")
BASE = f"https://{HOST}"
SSH_HOST = os.environ.get("FILET_RC_SSH", "ubuntu@52.197.137.3")
SSH_KEY = os.environ.get(
    "FILET_RC_SSH_KEY",
    str(Path.home() / "Downloads" / "LightsailDefaultKey-ap-northeast-1-spark.pem"),
)

# 前端九條路由（web/src/app/**/page.tsx 的完整集合）。少一條代表某頁沒部署上去。
FRONTEND_ROUTES = ["/", "/onboarding", "/leaders", "/capital", "/performance",
                   "/pricing", "/billing", "/ops", "/admin"]

# 需要 session 的端點：**必須 401**。500 代表沒認證就先炸（可能已碰到資料層），
# 200 代表閘門根本不在——兩者都比 401 嚴重得多，所以這裡不接受「非 200 就算過」。
SESSION_ENDPOINTS = ["/api/me", "/api/me/capital", "/api/me/leader",
                     "/api/ops/health", "/api/ops/revenue", "/api/admin/pending"]

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


def section_http(rep: Report) -> None:
    print(f"\n=== HTTP：部署站台 {BASE}（唯讀）===")

    # 1. 九條前端路由
    for path in FRONTEND_ROUTES:
        code, body = http_status(path)
        rep.add("http", f"路由 {path}", code == 200, "HTTP 200",
                f"HTTP {code}" if code else body)

    # 2. TLS：有效、非自簽、未接近到期
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

    # 3. 公開 API 契約：/api/billing/plans
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

    # 4. 需 session 的端點必須 401（不是 500、不是 200）
    for path in SESSION_ENDPOINTS:
        code, body = http_status(path)
        rep.add("http", f"未授權 {path} → 401", code == 401,
                "HTTP 401（閘門在且乾淨拒絕）",
                f"HTTP {code}" + (" ⚠️ 200＝閘門不存在" if code == 200 else
                                  " ⚠️ 500＝認證前就炸" if code == 500 else ""))

    # 5. http → https 轉址
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

    # --- 服務與 timer ---
    rc, out = ssh("systemctl is-active filet-api filet-dashboard filet-keysvc")
    states = out.split()
    rep.add("ssh", "三個常駐服務 active",
            states == ["active"] * 3, "filet-api/dashboard/keysvc 皆 active",
            " ".join(states) or "(無輸出)")

    rc, out = ssh("systemctl is-enabled filet-leaderboard.timer filet-perf-series.timer")
    states = out.split()
    rep.add("ssh", "兩個 timer enabled", states == ["enabled", "enabled"],
            "leaderboard.timer 與 perf-series.timer 皆 enabled",
            " ".join(states) or "(無輸出)")

    # --- 網路模式：測試機翻上主網是已發生過的地雷 ---
    rc, out = ssh("sudo grep -h '^Environment=FILET_API_NETWORK=' "
                  "/etc/systemd/system/filet-api.service || true")
    net = out.split("=")[-1].strip() if out else "(未宣告)"
    rep.add("ssh", "FILET_API_NETWORK=testnet", net == "testnet",
            "testnet（主網模式必須是刻意的人工決策）", net)

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

    # --- 四個 unit 都必須顯式宣告 FILET_LEADERS_PATH（否則各自推導＝漂移）---
    units = ["filet-api", "filet-follower@", "filet-leaderboard", "filet-perf-series"]
    missing = []
    for u in units:
        rc, out = ssh(f"sudo grep -c '^Environment=FILET_LEADERS_PATH=' "
                      f"/etc/systemd/system/{u}.service 2>/dev/null || echo 0")
        if out.strip().splitlines()[-1].strip() == "0":
            missing.append(u)
    rep.add("ssh", "四個 unit 皆宣告 FILET_LEADERS_PATH（同源，非各自推導）",
            not missing, "四個 unit 都有這一行",
            f"缺少：{', '.join(missing)}" if missing else "四個都有")

    # --- 機密檔不在伺服器上（rsync exclude 清單同時是機密邊界）---
    # 排除 .venv：certifi 的 cacert.pem 是公開 CA bundle，不是機密。
    rc, out = ssh(r"sudo find /opt/filet/spark -path '*/.venv' -prune -o "
                  r"\( -name '.env' -o -name '.env.*' -o -name '*.key' -o -name '*.pem' \) "
                  r"-print 2>/dev/null | head -20")
    found = [ln for ln in out.splitlines() if ln.strip()]
    rep.add("ssh", "部署樹內無機密檔（.env / *.key / *.pem）", not found,
            "零命中", f"命中 {len(found)} 個：{found[:5]}" if found else "零命中")


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
