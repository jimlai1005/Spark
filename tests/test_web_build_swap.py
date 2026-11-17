"""deploy/ops/web_build_swap.sh 的離線行為測試（plan 2026-11-17 §2 Task B）。

腳本是 bash；測試以子行程執行它，並用環境變數把所有外部依賴換成假的：
NPM_BIN（假 build）、RESTART_CMD（記次數）、CURL_BIN（假健康檢查端點，依線上 .next/BUILD_ID
決定回 200 或 500——所以「回退後線上是舊版」可以被觀察）、SYSTEMCTL_BIN（假 is-active）、
SUDO=""（不用 sudo）、LOCK_FILE（不碰 /run/lock）。不連網、不需要 node。
"""

from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "ops" / "web_build_swap.sh"

OLD_ID = "OLDBUILD"
NEW_ID = "NEWBUILD"

FAKE_NPM = """#!/usr/bin/env bash
# 假 npm：只接受 `run build`；cwd 必須是 BUILD_DIR（含 node_modules 符號連結）。
set -e
[ "$1" = run ] && [ "$2" = build ]
echo "fake build cwd=$PWD origin=$NEXT_PUBLIC_SITE_ORIGIN"
echo "$PWD" >> "$FAKE_STATE/npm_cwd"
{ echo "ls: $(ls -A . | tr '\n' ' ')"; echo "nm: $(readlink node_modules 2>/dev/null)"; echo "src: $(cat src/page.tsx 2>/dev/null)"; } >> "$FAKE_STATE/build_view"
if [ -n "${FAKE_NPM_STALE_WAIT:-}" ]; then
  # 孫行程：等測試寫出旗標檔 stale_go 才寫到「它啟動時的工作目錄」的 .next/server/stale.js（模擬被 TERM 的舊 build 殘留行程）。
  # 沒有固定延遲：什麼時候寫檔完全由測試決定（上限 30 秒只是防孤兒行程永遠不結束）。
  d="$PWD"
  ( for _ in $(seq 1 600); do [ -e "$FAKE_STATE/stale_go" ] && break; sleep 0.05; done
    mkdir -p "$d/.next/server"; echo stale > "$d/.next/server/stale.js"; touch "$FAKE_STATE/stale_done" ) &
  echo $! > "$FAKE_STATE/stale_pid"
fi
if [ -n "${HS_NPM_WAIT:-}" ]; then
  touch "$FAKE_STATE/at_$HS_NPM_WAIT"
  for _ in $(seq 1 500); do [ -e "$FAKE_STATE/delivered_$HS_NPM_WAIT" ] && break; sleep 0.02; done
fi
[ -e /dev/fd/9 ] && echo open >> "$FAKE_STATE/fd9_npm"
if [ -n "${HS_NPM_NAME:-}" ]; then
  touch "$FAKE_STATE/at_$HS_NPM_NAME"
  exec sleep 30  # 等測試送 TERM；被終止＝腳本確實在 build 期間被打斷
fi
[ -n "${FAKE_NPM_SLEEP:-}" ] && sleep "$FAKE_NPM_SLEEP"
if [ -n "${FAKE_NPM_FAIL:-}" ]; then echo "next/font: fetch failed (simulated)" >&2; exit 1; fi
[ -L node_modules ] || { echo "node_modules is not a symlink" >&2; exit 1; }
mkdir -p .next/static/chunks
[ -z "${FAKE_NPM_NO_BUILD_ID:-}" ] && printf '%s' "$FAKE_NEW_ID" > .next/BUILD_ID
if [ -n "${FAKE_NPM_EMPTY_STATIC:-}" ]; then rm -rf .next/static; mkdir -p .next/static; else
echo "console.log(1)" > .next/static/chunks/app.js
echo "a{}" > .next/static/chunks/app.css
fi
printf '%s' "$NEXT_PUBLIC_SITE_ORIGIN" > .next/origin.txt
ls .next/cache >> "$FAKE_STATE/cache_seen" 2>/dev/null || true
exit 0
"""

# 握手（handshake）：假指令在「要被打斷的時機」寫旗標檔 at_<name>，然後等測試寫 delivered_<name> 才繼續。
# 測試在看到旗標檔之後才送訊號——「訊號確實在該時機送達」由旗標檔存在來證明，而不是靠 sleep 賭時間。
HANDSHAKE = """
handshake() {
  touch "$FAKE_STATE/at_$1"
  for _ in $(seq 1 500); do [ -e "$FAKE_STATE/delivered_$1" ] && return 0; sleep 0.02; done
  echo "handshake $1 timed out" >> "$FAKE_STATE/handshake_timeouts"
}
"""

FAKE_RESTART = (
    """#!/usr/bin/env bash
"""
    + HANDSHAKE
    + """
echo restart >> "$FAKE_STATE/restarts"
n=$(wc -l < "$FAKE_STATE/restarts" | tr -d ' ')
[ "${HS_RESTART_AT:-0}" = "$n" ] && handshake "${HS_RESTART_NAME:-restart}"
[ -z "${FAKE_RESTART_FAIL_ONCE:-}" ] || [ -e "$FAKE_STATE/restart_failed" ] || { touch "$FAKE_STATE/restart_failed"; exit 1; }
[ "${FAKE_RESTART_FAIL_AT:-0}" = "$n" ] && exit 1
[ -n "${FAKE_RESTART_FAIL_FROM:-}" ] && [ "$n" -ge "$FAKE_RESTART_FAIL_FROM" ] && exit 1
exit 0
"""
)

# 假 curl：解析 -o FILE 與最後一個 URL 參數，印狀態碼到 stdout（對應腳本的 -w '%{http_code}'）。
# 線上 BUILD_ID == FAKE_BAD_BUILD_ID → 首頁 500；FAKE_ASSET_STATUS 設定時，資源回該碼。
FAKE_CURL = (
    """#!/usr/bin/env bash
"""
    + HANDSHAKE
    + """
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out="$2"; shift 2 ;; -w|--max-time) shift 2 ;; -*) shift ;; *) url="$1"; shift ;; esac
done
echo "$url" >> "$FAKE_STATE/curl_urls"
if [ -n "${HS_CURL_NAME:-}" ] && [ ! -e "$FAKE_STATE/at_${HS_CURL_NAME}" ]; then handshake "$HS_CURL_NAME"; fi
live_id=$(cat "$WEB_DIR/.next/BUILD_ID" 2>/dev/null || echo none)
case "$url" in
  */_next/static/*)
    [ -n "${FAKE_ASSET_SLEEP:-}" ] && sleep "$FAKE_ASSET_SLEEP"
    [ -n "$out" ] && [ "$out" != /dev/null ] && echo x > "$out"
    printf '%s' "${FAKE_ASSET_STATUS:-200}"
    ;;
  *)
    if [ "$live_id" = "${FAKE_BAD_BUILD_ID:-__none__}" ]; then
      [ "$out" != /dev/null ] && echo oops > "$out"
      printf '500'
    else
      [ "$out" != /dev/null ] && cat > "$out" <<HTML
<html><head><link rel="stylesheet" href="/_next/static/chunks/app.css"/></head>
<body><script src="/_next/static/chunks/app.js" async></script> live=$live_id</body></html>
HTML
      printf '200'
    fi
    ;;
esac
"""
)

FAKE_SYSTEMCTL = (
    """#!/usr/bin/env bash
# 只支援 `is-active --quiet <unit>`；FAKE_API_ACTIVE=0 → inactive；HS_SYSTEMCTL_NAME：在前置檢查中握手
"""
    + HANDSHAKE
    + """
[ -n "${HS_SYSTEMCTL_NAME:-}" ] && handshake "$HS_SYSTEMCTL_NAME"
[ "${FAKE_API_ACTIVE:-1}" = 1 ]
"""
)


def _write_exe(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


def _tree_digest(root: Path) -> dict[str, str]:
    """目錄樹 -> {相對路徑: 內容 sha256 或 'dir'/'link'}，用來斷言 .next 完全沒被動到。"""
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_symlink():
            out[rel] = "link"
        elif p.is_dir():
            out[rel] = "dir"
        else:
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.web = tmp_path / "web"
        self.build = tmp_path / "web-build"
        self.state = tmp_path / "state"
        self.bin = tmp_path / "bin"
        self.state.mkdir()
        self.bin.mkdir()
        # 線上目錄：原始碼 + node_modules + 舊 .next（含 cache）
        (self.web / "src").mkdir(parents=True)
        (self.web / "src" / "page.tsx").write_text("export default 1\n")
        (self.web / "package.json").write_text("{}\n")
        (self.web / "node_modules" / "next").mkdir(parents=True)
        (self.web / "node_modules" / "next" / "index.js").write_text("//\n")
        (self.web / ".next" / "static" / "chunks").mkdir(parents=True)
        (self.web / ".next" / "cache").mkdir()
        (self.web / ".next" / "cache" / "webpack.pack").write_text("cache\n")
        (self.web / ".next" / "BUILD_ID").write_text(OLD_ID)
        (self.web / ".next" / "static" / "chunks" / "old.js").write_text("old\n")
        _write_exe(self.bin / "npm", FAKE_NPM)
        _write_exe(self.bin / "restart.sh", FAKE_RESTART)
        _write_exe(self.bin / "curl", FAKE_CURL)
        _write_exe(self.bin / "systemctl", FAKE_SYSTEMCTL)
        self.flag = tmp_path / "maintenance" / "ON"
        self.extra: dict[str, str] = {}
        # LOG_FILE 放在測試樹（self.root）之外：腳本從第一行輸出起就會寫 log，
        # 放在樹內會讓「拒絕情境整棵樹 digest 不變」的斷言誤報。
        self.logdir = tmp_path.parent / f"{tmp_path.name}-logs"
        self.logdir.mkdir(exist_ok=True)
        self.log = self.logdir / "build.log"

    def build_env(self, drop: tuple[str, ...] = (), **overrides: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in drop}
        env.pop("NEXT_PUBLIC_SITE_ORIGIN", None)
        env.update(
            WEB_DIR=str(self.web),
            BUILD_DIR=str(self.build),
            SUDO="",
            NPM_BIN=str(self.bin / "npm"),
            RESTART_CMD=str(self.bin / "restart.sh"),
            CURL_BIN=str(self.bin / "curl"),
            SYSTEMCTL_BIN=str(self.bin / "systemctl"),
            HEALTH_URL="http://127.0.0.1:3000/",
            HEALTH_TIMEOUT_S="2",
            MAINTENANCE_FLAG=str(self.flag),
            LOCK_FILE=str(self.root / "lock"),
            LOG_FILE=str(self.log),
            NEXT_PUBLIC_SITE_ORIGIN="https://trade.filet.app",
            FAKE_STATE=str(self.state),
            FAKE_NEW_ID=NEW_ID,
            TMPDIR=str(self.root),
        )
        env.update(self.extra)
        env.update(overrides)
        for k in drop:
            env.pop(k, None)
        return env

    def run(
        self, *args: str, drop: tuple[str, ...] = (), **overrides: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=self.build_env(drop, **overrides),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def popen(self, *args: str, **overrides: str) -> subprocess.Popen[str]:
        """在新 session 啟動腳本（行程群組＝腳本 pid），供「在指定時機送訊號」的測試用。"""
        return subprocess.Popen(
            ["bash", str(SCRIPT), *args],
            env=self.build_env(**overrides),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    def log_text(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    # 觀測
    def live_id(self) -> str:
        return (self.web / ".next" / "BUILD_ID").read_text()

    def restarts(self) -> int:
        f = self.state / "restarts"
        return len(f.read_text().split()) if f.exists() else 0


@pytest.fixture()
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _line(text: str, prefix: str) -> str:
    """報告中以 prefix 起頭的那一行（找不到就讓測試明確失敗）。"""
    return next(ln for ln in text.splitlines() if ln.startswith(prefix))


def _out(r: subprocess.CompletedProcess[str]) -> str:
    return f"rc={r.returncode}\n--stdout--\n{r.stdout}\n--stderr--\n{r.stderr}"


# ------------------------------------------------------------------ plan §2 測試 1
def test_build_failure_leaves_live_untouched(env: Env) -> None:
    """plan 測試 1：build 失敗 → 非零、BUILD_ID 不變、沒有 .next-prev（且 .next 整棵不變、不重啟）。"""
    before = _tree_digest(env.web / ".next")
    r = env.run(FAKE_NPM_FAIL="1")
    assert r.returncode != 0, _out(r)
    assert env.live_id() == OLD_ID
    assert not (env.web / ".next-prev").exists()
    assert _tree_digest(env.web / ".next") == before
    assert env.restarts() == 0
    assert "build 失敗" in r.stderr
    assert "next/font: fetch failed" in r.stderr  # log 末行有被印出


def test_build_without_build_id_is_rejected_before_swap(env: Env) -> None:
    """plan 步驟 4：產物缺 BUILD_ID → 非零、線上不動。"""
    before = _tree_digest(env.web / ".next")
    r = env.run(FAKE_NPM_NO_BUILD_ID="1")
    assert r.returncode != 0, _out(r)
    assert "BUILD_ID" in r.stderr
    assert _tree_digest(env.web / ".next") == before
    assert not (env.web / ".next-prev").exists()
    assert env.restarts() == 0


@pytest.mark.parametrize("overrides", [{"FAKE_NEW_ID": ""}, {"FAKE_NPM_EMPTY_STATIC": "1"}])
def test_empty_build_artifacts_are_rejected_before_swap(env: Env, overrides: dict[str, str]) -> None:
    """plan 步驟 4：BUILD_ID 為空、或 static 為空 → 非零、線上不動。"""
    before = _tree_digest(env.web / ".next")
    r = env.run(**overrides)
    assert r.returncode != 0, _out(r)
    assert "線上版未被改動" in r.stderr
    assert _tree_digest(env.web / ".next") == before
    assert not (env.web / ".next-prev").exists()
    assert env.restarts() == 0


# ------------------------------------------------------------------ plan §2 測試 2
def test_build_success_swaps_and_restarts_once(env: Env) -> None:
    """plan 測試 2：成功 → 新 BUILD_ID 在線上、.next-prev 為舊值、重啟恰一次。"""
    r = env.run()
    assert r.returncode == 0, _out(r)
    assert env.live_id() == NEW_ID
    assert (env.web / ".next-prev" / "BUILD_ID").read_text() == OLD_ID
    assert env.restarts() == 1
    assert OLD_ID in r.stdout and NEW_ID in r.stdout  # 摘要含舊／新 BUILD_ID
    assert ".next-prev" in r.stdout and "手動回退" in r.stdout
    # 假 build 收到 NEXT_PUBLIC_SITE_ORIGIN，且在 BUILD_DIR 內、不是線上目錄內執行
    assert (env.web / ".next" / "origin.txt").read_text() == "https://trade.filet.app"
    cwd = Path((env.state / "npm_cwd").read_text().strip())
    assert cwd.parent == env.build and cwd.name.startswith("run-")  # 在本次專屬的 run-* 目錄內，不是線上目錄、也不是 BUILD_DIR 根
    # 成功後本次的 run-* 已刪除（.next 已被 mv 走），BUILD_DIR 只剩標記檔
    assert not (env.build / ".next").exists()
    assert list(env.build.glob("run-*")) == []


def test_build_dir_is_source_copy_with_symlinked_node_modules_and_cache(env: Env) -> None:
    """plan 步驟 2：node_modules 是指回 WEB_DIR 的符號連結（不複製）、.next/cache 被帶進 BUILD_DIR。"""
    r = env.run()
    assert r.returncode == 0, _out(r)
    # run-* 在成功後即刪除，所以由假 build 在 build 當下記錄它看到的樣子（build_view）
    view = (env.state / "build_view").read_text()
    assert f"nm: {env.web / 'node_modules'}" in view  # node_modules 是指回 WEB_DIR 的符號連結（不複製）
    assert "src: export default 1" in view  # 原始碼副本
    assert ".next-prev" not in view.split("ls:")[1].split("\n")[0]  # 同步時排除 WEB_DIR 內的 .next* 目錄
    assert (env.state / "cache_seen").read_text().strip() == "webpack.pack"
    # 線上目錄自己的 node_modules 沒被動到
    assert (env.web / "node_modules" / "next" / "index.js").is_file()
    assert not (env.web / "node_modules").is_symlink()


def test_second_run_replaces_prev_with_previous_live(env: Env) -> None:
    """.next-prev 的保留語意：永遠只留「再上一版」，第二次執行時第一次的結果成為 .next-prev。"""
    assert env.run().returncode == 0
    r = env.run(FAKE_NEW_ID="THIRD")
    assert r.returncode == 0, _out(r)
    assert env.live_id() == "THIRD"
    assert (env.web / ".next-prev" / "BUILD_ID").read_text() == NEW_ID
    assert env.restarts() == 2


def test_swapped_bundle_is_not_group_or_other_writable(env: Env) -> None:
    """RUNBOOK §4.4：換入的 .next 不得 go-w（SUDO 為空時跳過 chown，但 chmod 仍做）。"""
    (env.bin / "npm").write_text(
        FAKE_NPM.replace(
            "exit 0\n", "chmod -R 777 .next\nexit 0\n"
        )
    )
    r = env.run()
    assert r.returncode == 0, _out(r)
    bad = [
        str(p)
        for p in [env.web / ".next", *(env.web / ".next").rglob("*")]
        if p.stat().st_mode & 0o022
    ]
    assert bad == []


# ------------------------------------------------------------------ plan §2 測試 3
def test_health_failure_rolls_back_automatically(env: Env) -> None:
    """plan 測試 3：換入後新版健康檢查失敗 → BUILD_ID 回舊值、重啟兩次、非零退出。"""
    r = env.run(FAKE_BAD_BUILD_ID=NEW_ID)
    assert r.returncode == 4, _out(r)
    assert env.live_id() == OLD_ID
    assert env.restarts() == 2
    assert not (env.web / ".next-prev").exists()  # 已換回去
    assert (env.web / ".next-failed" / "BUILD_ID").read_text() == NEW_ID  # 壞版留作排查
    assert "自動回退" in r.stderr
    assert "健康檢查失敗" in r.stderr
    assert "回退後健康檢查通過" in r.stderr  # 回退後線上確實是好的（假 curl 依線上 BUILD_ID 回應）


def test_static_asset_400_after_swap_triggers_rollback(env: Env) -> None:
    """plan 步驟 7：首頁 200 但引用的 /_next/static 資源 400（2026-11-15 事故形狀）→ 回退。"""
    r = env.run(FAKE_ASSET_STATUS="400", HEALTH_TIMEOUT_S="1")
    assert r.returncode != 0, _out(r)
    assert env.live_id() == OLD_ID
    assert env.restarts() == 2
    assert "/_next/static/" in r.stderr and "400" in r.stderr


def test_restart_command_failure_rolls_back(env: Env) -> None:
    """步驟 6 失敗（重啟指令非零）→ 也走回退；第二次重啟（回退）成功。"""
    r = env.run(FAKE_RESTART_FAIL_ONCE="1")
    assert r.returncode != 0, _out(r)
    assert env.live_id() == OLD_ID
    assert env.restarts() == 2


def test_rollback_failure_exits_5_and_shouts(env: Env) -> None:
    """回退本身失敗（.next-prev 被人手刪掉）→ 結束碼 5、醒目訊息，且新版 .next 仍放回原位不缺席。"""
    # 健康檢查期間，假 curl 順手刪掉 .next-prev，模擬回退前提被破壞
    curl = FAKE_CURL.replace(
        'echo "$url" >> "$FAKE_STATE/curl_urls"',
        'echo "$url" >> "$FAKE_STATE/curl_urls"; rm -rf "$WEB_DIR/.next-prev"',
    )
    (env.bin / "curl").write_text(curl)
    r = env.run(FAKE_BAD_BUILD_ID=NEW_ID)
    assert r.returncode == 5, _out(r)
    assert "回退失敗" in r.stderr
    assert env.live_id() == NEW_ID  # 退而求其次：.next 沒有缺席


# ------------------------------------------------------------------ plan §2 測試 4：拒絕執行
def _assert_rejected_untouched(env: Env, r: subprocess.CompletedProcess[str], needle: str) -> None:
    assert r.returncode != 0, _out(r)
    assert needle in r.stderr, _out(r)
    assert env.live_id() == OLD_ID
    assert not (env.web / ".next-prev").exists()
    assert env.restarts() == 0
    assert not (env.state / "npm_cwd").exists()  # 連 build 都沒開始


def test_refuses_without_site_origin(env: Env) -> None:
    """plan 測試 4a：未設 NEXT_PUBLIC_SITE_ORIGIN。"""
    # Env.run 預設會補上 origin，這裡以空字串覆寫＝未設
    r = env.run(NEXT_PUBLIC_SITE_ORIGIN="")
    _assert_rejected_untouched(env, r, "NEXT_PUBLIC_SITE_ORIGIN")


def test_refuses_when_maintenance_flag_present_and_force_overrides(env: Env) -> None:
    """plan 測試 4b：維護旗標存在 → 拒絕；--force 則放行。"""
    env.flag.parent.mkdir()
    env.flag.write_text("")
    r = env.run()
    _assert_rejected_untouched(env, r, "維護旗標")
    r2 = env.run("--force")
    assert r2.returncode == 0, _out(r2)
    assert env.live_id() == NEW_ID


def test_refuses_when_api_unit_inactive_and_flag_overrides(env: Env) -> None:
    """plan 測試 4c：API_UNIT 非 active → 拒絕；--allow-api-start 則放行。"""
    r = env.run(FAKE_API_ACTIVE="0")
    _assert_rejected_untouched(env, r, "filet-api")
    r2 = env.run("--allow-api-start", FAKE_API_ACTIVE="0")
    assert r2.returncode == 0, _out(r2)
    assert env.live_id() == NEW_ID


def test_refuses_when_no_live_build_id(env: Env) -> None:
    """plan 步驟 1：WEB_DIR/.next/BUILD_ID 不存在（沒有可回退版本）→ 拒絕。"""
    (env.web / ".next" / "BUILD_ID").unlink()
    r = env.run()
    assert r.returncode != 0, _out(r)
    assert "BUILD_ID" in r.stderr
    assert not (env.web / ".next-prev").exists()
    assert env.restarts() == 0


def test_unknown_argument_is_usage_error(env: Env) -> None:
    r = env.run("--nope")
    assert r.returncode == 2, _out(r)
    assert env.live_id() == OLD_ID


def test_concurrent_run_is_refused_by_lock(env: Env) -> None:
    """plan 步驟 1：單一執行鎖——第一個還在 build 時，第二個必須被拒且不動線上。
    握手式：第一個執行的 build 停在握手點 hold（旗標檔證明它確實持有鎖、正在 build），測試在第二個被拒之後才放行它。"""
    p1 = env.popen(HS_NPM_WAIT="hold")
    _wait_flag(env, p1, "hold")
    r2 = env.run()
    assert r2.returncode != 0, _out(r2)
    assert "另一個 web_build_swap" in r2.stderr
    assert env.live_id() == OLD_ID  # 第二個被拒時線上沒被動
    (env.state / "delivered_hold").touch()  # 放行第一個
    out1, err1 = p1.communicate(timeout=60)
    assert p1.returncode == 0, f"{out1}\n{err1}"
    assert env.live_id() == NEW_ID
    assert env.restarts() == 1  # 只有第一個重啟過


# ------------------------------------------------------------------ Task E / C1：BUILD_DIR 護欄
def _make_precious(root: Path) -> Path:
    d = root / "precious"
    d.mkdir()
    (d / "ledger.json").write_text("do not delete\n")
    (root / "unrelated.txt").write_text("unrelated\n")
    return d


def _assert_c1_rejected(env: Env, r: subprocess.CompletedProcess[str], before_tree: dict[str, str]) -> None:
    """拒絕情境共同斷言：非零、整棵測試樹（含 precious/、無關檔案、web/.next）內容完全不變、沒開始 build。"""
    assert r.returncode != 0, _out(r)
    assert (env.root / "precious" / "ledger.json").read_text() == "do not delete\n"
    assert (env.root / "unrelated.txt").read_text() == "unrelated\n"
    assert env.live_id() == OLD_ID
    assert _tree_digest(env.root) == before_tree
    assert not (env.state / "npm_cwd").exists()
    assert env.restarts() == 0


def test_c1_build_dir_equal_to_web_dir_is_refused(env: Env) -> None:
    """C1-a：BUILD_DIR == WEB_DIR → 拒絕，一個檔案都不動。"""
    _make_precious(env.root)
    before = _tree_digest(env.root)
    r = env.run(BUILD_DIR=str(env.web))
    _assert_c1_rejected(env, r, before)
    assert "不得等於 WEB_DIR" in r.stderr, _out(r)


def test_c1_build_dir_is_parent_of_web_dir_is_refused(env: Env) -> None:
    """C1-b：BUILD_DIR 是 WEB_DIR 的上層（上層內有 precious/）→ 拒絕；precious/ 與整棵樹不變。"""
    _make_precious(env.root)
    before = _tree_digest(env.root)
    r = env.run(BUILD_DIR=str(env.root))
    _assert_c1_rejected(env, r, before)
    assert "上層目錄" in r.stderr, _out(r)


def test_c1_build_dir_inside_web_dir_is_refused(env: Env) -> None:
    """C1-c：BUILD_DIR 在 WEB_DIR 之內 → 拒絕（也不會在 web/ 內建立目錄）。"""
    _make_precious(env.root)
    before = _tree_digest(env.root)
    r = env.run(BUILD_DIR=str(env.web / "nested-build"))
    _assert_c1_rejected(env, r, before)
    assert "之內" in r.stderr, _out(r)
    assert not (env.web / "nested-build").exists()


def test_c1_nonempty_dir_without_marker_is_refused(env: Env) -> None:
    """C1-d：已有內容、沒有標記檔的目錄（不是本腳本建的）→ 拒絕；不寫標記檔、內容不變。"""
    _make_precious(env.root)
    victim = env.root / "someone-elses-dir"
    victim.mkdir()
    (victim / "important.txt").write_text("keep me\n")
    before = _tree_digest(env.root)
    r = env.run(BUILD_DIR=str(victim))
    _assert_c1_rejected(env, r, before)
    assert "沒有標記檔" in r.stderr, _out(r)
    assert not (victim / ".web_build_swap_dir").exists()
    assert (victim / "important.txt").read_text() == "keep me\n"


def test_c1_existing_empty_dir_is_claimed_with_marker(env: Env) -> None:
    """C1-e：已存在的空目錄 → 通過，並寫入標記檔；build 在其中進行。"""
    _make_precious(env.root)
    empty = env.root / "empty-build"
    empty.mkdir()
    r = env.run(BUILD_DIR=str(empty))
    assert r.returncode == 0, _out(r)
    assert (env.root / "precious" / "ledger.json").read_text() == "do not delete\n"  # 旁邊的無關目錄與檔案仍在
    assert (env.root / "unrelated.txt").read_text() == "unrelated\n"
    assert (empty / ".web_build_swap_dir").is_file()
    cwd = Path((env.state / "npm_cwd").read_text().strip())
    assert cwd.parent == empty and cwd.name.startswith("run-")
    assert env.live_id() == NEW_ID


def test_c1_second_run_with_marker_passes(env: Env) -> None:
    """C1-f：第二次執行（目錄已有內容＋標記檔；rsync --delete 不得刪掉標記檔）→ 通過。"""
    _make_precious(env.root)
    r1 = env.run()
    assert r1.returncode == 0, _out(r1)
    assert (env.build / ".web_build_swap_dir").is_file()
    assert [p.name for p in env.build.iterdir()] == [".web_build_swap_dir"]  # 目錄非空（有標記檔）；run-* 已刪除
    r2 = env.run(FAKE_NEW_ID="SECOND")
    assert r2.returncode == 0, _out(r2)
    assert env.live_id() == "SECOND"
    assert (env.build / ".web_build_swap_dir").is_file()
    assert (env.root / "precious" / "ledger.json").read_text() == "do not delete\n"  # 旁邊的無關目錄與檔案仍在
    assert (env.root / "unrelated.txt").read_text() == "unrelated\n"


def test_c1_symlink_to_web_dir_or_parent_is_resolved_and_refused(env: Env) -> None:
    """C1：護欄以實體路徑判斷——指向 WEB_DIR、或指向其上層的符號連結同樣被拒。"""
    _make_precious(env.root)
    to_web = env.root / "link-to-web"
    to_web.symlink_to(env.web)
    to_root = env.root / "link-to-root"
    to_root.symlink_to(env.root)
    before = _tree_digest(env.root)
    r1 = env.run(BUILD_DIR=str(to_web))
    _assert_c1_rejected(env, r1, before)
    r2 = env.run(BUILD_DIR=str(to_root))
    _assert_c1_rejected(env, r2, before)
    # 以符號連結＋子路徑形式指到 web 之內也要擋
    r3 = env.run(BUILD_DIR=str(to_web / "sub"))
    _assert_c1_rejected(env, r3, before)


def test_c1_dotdot_component_is_refused(env: Env) -> None:
    """C1：BUILD_DIR 含 .. 成分（可繞過字串比對）→ 拒絕。"""
    _make_precious(env.root)
    before = _tree_digest(env.root)
    r = env.run(BUILD_DIR=f"{env.web}/../web-build")
    _assert_c1_rejected(env, r, before)
    assert ".." in r.stderr, _out(r)


# ------------------------------------------------------------------ Task E / C2：--rollback
def test_c2_rollback_without_prev_is_refused_and_touches_nothing(env: Env) -> None:
    """C2：沒有 .next-prev → 拒絕、.next digest 不變、不重啟、不產生 .next-failed。"""
    before = _tree_digest(env.web)
    r = env.run("--rollback")
    assert r.returncode != 0, _out(r)
    assert "沒有可回退的版本" in r.stderr and "整包重部署" in r.stderr
    assert _tree_digest(env.web) == before
    assert env.restarts() == 0
    assert not (env.web / ".next-failed").exists()


def test_c2_rollback_with_empty_prev_build_id_is_refused(env: Env) -> None:
    """C2：.next-prev 存在但 BUILD_ID 為空 → 同樣拒絕且不動。"""
    (env.web / ".next-prev").mkdir()
    (env.web / ".next-prev" / "BUILD_ID").write_text("")
    before = _tree_digest(env.web)
    r = env.run("--rollback")
    assert r.returncode != 0, _out(r)
    assert _tree_digest(env.web) == before
    assert env.restarts() == 0


def test_c2_rollback_with_prev_restores_old_version(env: Env) -> None:
    """C2：先正常換入一次，再 --rollback：.next 回舊值、重啟恰 1 次（本次）、新版在 .next-failed。"""
    assert env.run().returncode == 0
    assert env.restarts() == 1
    r = env.run("--rollback", FAKE_BAD_BUILD_ID=NEW_ID)
    assert r.returncode == 0, _out(r)
    assert env.live_id() == OLD_ID
    assert env.restarts() == 2  # 換入 1 次 ＋ 回退 1 次
    assert (env.web / ".next-failed" / "BUILD_ID").read_text() == NEW_ID
    assert not (env.web / ".next-prev").exists()
    assert (env.state / "npm_cwd").read_text().count("\n") == 1  # 只有第一次換入 build 過；--rollback 沒有再 build
    # .next-prev 已被消耗：再 --rollback 必須被拒絕，且線上不動
    before = _tree_digest(env.web / ".next")
    r2 = env.run("--rollback")
    assert r2.returncode != 0, _out(r2)
    assert _tree_digest(env.web / ".next") == before
    assert env.restarts() == 2


def test_c2_rollback_health_failure_exits_5(env: Env) -> None:
    """C2：--rollback 後健康檢查失敗 → 結束碼 5。"""
    assert env.run().returncode == 0
    r = env.run("--rollback", FAKE_BAD_BUILD_ID=OLD_ID)
    assert r.returncode == 5, _out(r)
    assert "回退後健康檢查失敗" in r.stderr


def test_c2_rollback_obeys_maintenance_flag_and_api_check(env: Env) -> None:
    """C2：--rollback 同樣受維護旗標與 API_UNIT 檢查約束（被拒時 .next-prev 與 .next 都不動）。"""
    assert env.run().returncode == 0
    before = _tree_digest(env.web)
    env.flag.parent.mkdir()
    env.flag.write_text("")
    r = env.run("--rollback")
    assert r.returncode != 0 and "維護旗標" in r.stderr, _out(r)
    assert _tree_digest(env.web) == before
    env.flag.unlink()
    r2 = env.run("--rollback", FAKE_API_ACTIVE="0")
    assert r2.returncode != 0 and "filet-api" in r2.stderr, _out(r2)
    assert _tree_digest(env.web) == before
    assert env.restarts() == 1


def test_c2_no_bare_mv_rollback_recipe_in_output_or_help(env: Env) -> None:
    """C2：成功摘要與 --help 只指向 --rollback，不再列裸的 mv 回退指令。"""
    r = env.run()
    assert r.returncode == 0, _out(r)
    assert "--rollback" in r.stdout
    bare_mv = re.compile(r"\bmv\s+\S*\.next-prev\s+\S*\.next\b")
    assert not bare_mv.search(r.stdout)
    h = env.run("--help")
    assert h.returncode == 0 and "--rollback" in h.stdout
    assert not bare_mv.search(h.stdout)


# ------------------------------------------------------------------ Task G / Suggestion：護欄形狀測試
def test_g_marker_is_a_symlink_is_refused(env: Env) -> None:
    """標記檔是符號連結（不算標記）＋目錄非空 → 拒絕，不動任何東西。"""
    _make_precious(env.root)
    victim = env.root / "victim"
    victim.mkdir()
    (victim / "real.txt").write_text("x\n")
    (victim / ".web_build_swap_dir").symlink_to(victim / "real.txt")
    before = _tree_digest(env.root)
    r = env.run(BUILD_DIR=str(victim))
    _assert_c1_rejected(env, r, before)
    assert "沒有標記檔" in r.stderr, _out(r)


def test_g_marker_is_a_directory_is_refused(env: Env) -> None:
    """標記檔名稱其實是目錄 → 不算標記 → 拒絕。"""
    _make_precious(env.root)
    victim = env.root / "victim"
    (victim / ".web_build_swap_dir").mkdir(parents=True)
    before = _tree_digest(env.root)
    r = env.run(BUILD_DIR=str(victim))
    _assert_c1_rejected(env, r, before)
    assert "沒有標記檔" in r.stderr, _out(r)


def test_g_dir_with_only_hidden_files_is_refused(env: Env) -> None:
    """目錄只含隱藏檔（ls 不加 -A 會看成空）→ 視為非空、拒絕。"""
    _make_precious(env.root)
    victim = env.root / "victim"
    victim.mkdir()
    (victim / ".hidden").write_text("secret\n")
    before = _tree_digest(env.root)
    r = env.run(BUILD_DIR=str(victim))
    _assert_c1_rejected(env, r, before)
    assert (victim / ".hidden").read_text() == "secret\n"
    assert not (victim / ".web_build_swap_dir").exists()


def test_g_build_dir_is_a_regular_file_is_refused(env: Env) -> None:
    """BUILD_DIR 是一般檔案 → 拒絕，檔案內容不變。"""
    _make_precious(env.root)
    f = env.root / "a-file"
    f.write_text("i am a file\n")
    before = _tree_digest(env.root)
    r = env.run(BUILD_DIR=str(f))
    _assert_c1_rejected(env, r, before)
    assert f.read_text() == "i am a file\n"


def test_g_build_dir_is_a_dangling_symlink_is_refused(env: Env) -> None:
    """BUILD_DIR 是懸空符號連結 → 拒絕，連結本身與其「目標位置」都不被建立。"""
    _make_precious(env.root)
    link = env.root / "dangling"
    link.symlink_to(env.root / "does-not-exist")
    before = _tree_digest(env.root)
    r = env.run(BUILD_DIR=str(link))
    _assert_c1_rejected(env, r, before)
    assert link.is_symlink() and not (env.root / "does-not-exist").exists()


# ------------------------------------------------------------------ Task G / W2：缺 .next 的復原模式
def _make_prev(env: Env, build_id: str = "PREVBUILD") -> Path:
    prev = env.web / ".next-prev"
    (prev / "static").mkdir(parents=True)
    (prev / "BUILD_ID").write_text(build_id)
    (prev / "static" / "p.js").write_text("prev\n")
    return prev


def test_g_w2_rollback_recovers_when_next_is_missing(env: Env) -> None:
    """W2：.next 缺席＋.next-prev 有效＋既有 .next-failed → 復原模式：0、.next 為 .next-prev 的值、
    既有 .next-failed 內容不變、重啟恰 1 次。"""
    import shutil

    shutil.rmtree(env.web / ".next")
    _make_prev(env, "PREVBUILD")
    failed = env.web / ".next-failed"
    failed.mkdir()
    (failed / "evidence.txt").write_text("keep this evidence\n")
    before_failed = _tree_digest(failed)
    r = env.run("--rollback")
    assert r.returncode == 0, _out(r)
    assert env.live_id() == "PREVBUILD"
    assert _tree_digest(failed) == before_failed  # 排查證據原封不動
    assert not (env.web / ".next-prev").exists()
    assert env.restarts() == 1
    assert "線上原本沒有 .next，已由 .next-prev 復原" in r.stdout
    assert not (env.state / "npm_cwd").exists()  # 沒有 build


def test_g_w2_failed_evidence_not_deleted_before_prev_is_verified(env: Env) -> None:
    """W2：確認 .next-prev 有效之前不得刪 .next-failed。自動回退時 .next-prev 已失效（被人手刪）→
    回退失敗（5），.next 仍為新版、既有 .next-failed 證據仍在。"""
    failed = env.web / ".next-failed"
    failed.mkdir()
    (failed / "evidence.txt").write_text("keep this evidence\n")
    curl = FAKE_CURL.replace(
        'echo "$url" >> "$FAKE_STATE/curl_urls"',
        'echo "$url" >> "$FAKE_STATE/curl_urls"; rm -rf "$WEB_DIR/.next-prev"',
    )
    (env.bin / "curl").write_text(curl)
    r = env.run(FAKE_BAD_BUILD_ID=NEW_ID)
    assert r.returncode == 5, _out(r)
    assert (failed / "evidence.txt").read_text() == "keep this evidence\n"
    assert env.live_id() == NEW_ID  # .next 沒被搬走


def test_g_w4_failed_rollback_with_missing_next_tells_operator_to_run_rollback(env: Env) -> None:
    """W4：回退後 .next 缺席、舊版完好在 .next-prev 時，訊息必須明確指示執行 --rollback（而不是籠統的人工處理）。"""
    assert env.run().returncode == 0
    # 假 sudo：所有「目的地是 .next」的 mv 一律失敗（含補救用的搬回）→ .next 缺席、.next-prev 仍完好
    sudo = _write_exe(
        env.bin / "fake_sudo",
        '#!/usr/bin/env bash\n[ "$1" = chown ] && exit 0\n'
        '[ "$1" = mv ] && [ "${3##*/}" = .next ] && exit 1\n"$@"\n',
    )
    r = env.run("--rollback", SUDO=str(sudo))
    assert r.returncode == 5, _out(r)
    assert not (env.web / ".next").exists() and (env.web / ".next-prev" / "BUILD_ID").exists()
    assert "--rollback" in r.stderr and "復原模式" in r.stderr, _out(r)


# ------------------------------------------------------------------ Task H / H4：訊號模型與結束碼＝最終實況
# 假 sudo：chown 略過；其餘照常執行；可在指定指令的「前／後」握手，或讓指定的 mv 失敗／先做完再回報失敗。
#   HS_SUDO_NAME     握手名（旗標檔 at_<name>；測試送完訊號後寫 delivered_<name> 才放行）
#   HS_SUDO_CMD/SRC/DST  以指令名與 basename 比對（mv：來源／目的；rm -rf：SRC=-rf、DST=目標）
#   HS_SUDO_WHEN     before｜after（相對於該指令）
#   HS_FAIL_MV_DST   目的地 basename 等於它的 mv 一律失敗且不執行（含補救用的搬回）
#   HS_FAIL_MV_DST_GLOB / HS_FAIL_MV_SRCBASE  目的地 basename 符合 glob／來源 basename 等於它的 mv 失敗且不執行
#   HS_PARTIAL_RM_GLOB  目標 basename 符合 glob 的 rm：只刪掉其中的 BUILD_ID（刪一半）並回報失敗
#   HS_FAIL_MV_FROM  來源路徑含此字串的 mv：照常執行，但回報失敗（exit 1）
HS_SUDO = (
    """#!/usr/bin/env bash
"""
    + HANDSHAKE
    + """
[ "$1" = chown ] && exit 0
hit=0
if [ -n "${HS_SUDO_NAME:-}" ] && [ "$1" = "$HS_SUDO_CMD" ] && [ "${2##*/}" = "$HS_SUDO_SRC" ] \\
   && [[ "${3##*/}" == $HS_SUDO_DST ]] && [ ! -e "$FAKE_STATE/at_$HS_SUDO_NAME" ]; then hit=1; fi
[ "$hit" = 1 ] && [ "${HS_SUDO_WHEN:-after}" = before ] && handshake "$HS_SUDO_NAME"
if [ "$1" = mv ] && [ -n "${HS_FAIL_MV_DST:-}" ] && [ "${3##*/}" = "$HS_FAIL_MV_DST" ]; then exit 1; fi
if [ "$1" = mv ] && [ -n "${HS_FAIL_MV_DST_GLOB:-}" ] && [[ "${3##*/}" == $HS_FAIL_MV_DST_GLOB ]]; then exit 1; fi
if [ "$1" = mv ] && [ -n "${HS_FAIL_MV_SRCBASE:-}" ] && [ "${2##*/}" = "$HS_FAIL_MV_SRCBASE" ]; then exit 1; fi
if [ "$1" = rm ] && [ -n "${HS_PARTIAL_RM_GLOB:-}" ] && [[ "${3##*/}" == $HS_PARTIAL_RM_GLOB ]]; then rm -f "$3/BUILD_ID"; exit 1; fi
"$@"; rc=$?
[ "$hit" = 1 ] && [ "${HS_SUDO_WHEN:-after}" = after ] && handshake "$HS_SUDO_NAME"
if [ "$1" = mv ] && [ -n "${HS_FAIL_MV_FROM:-}" ] && [[ "$2" == *"$HS_FAIL_MV_FROM"* ]]; then exit 1; fi
exit $rc
"""
)


def _hs_sudo(env: Env) -> str:
    return str(_write_exe(env.bin / "hs_sudo", HS_SUDO))


def _wait_flag(env: Env, p: subprocess.Popen[str], name: str, timeout: float = 30.0) -> None:
    import time

    flag = env.state / f"at_{name}"
    deadline = time.time() + timeout
    while not flag.exists():
        assert time.time() < deadline, f"handshake {name} 沒有出現（腳本沒有走到該時機）"
        assert p.poll() is None or flag.exists(), f"腳本在到達時機 {name} 之前就結束了：rc={p.returncode}"
        time.sleep(0.02)


def _signalled_run(
    env: Env,
    args: tuple[str, ...],
    name: str,
    sig: int | None,
    *,
    group: bool = False,
    close_pipes: bool = False,
    **overrides: str,
) -> tuple[int, str, str]:
    """啟動腳本；等假指令在指定時機寫出旗標檔（＝腳本確實走到那裡）；才送訊號；再放行假指令。
    回傳 (結束碼, stdout, stderr)。close_pipes：先關掉 stdout／stderr 讀端（模擬 tee 被殺／ssh 斷線）。"""
    p = env.popen(*args, **overrides)
    _wait_flag(env, p, name)
    assert (env.state / f"at_{name}").exists()
    if close_pipes:
        assert p.stdout is not None and p.stderr is not None
        p.stdout.close()
        p.stderr.close()
    if sig is not None:
        if group:
            os.killpg(p.pid, sig)
        else:
            os.kill(p.pid, sig)
    (env.state / f"delivered_{name}").touch()
    if close_pipes:
        rc = p.wait(timeout=60)
        return rc, "", ""
    out, err = p.communicate(timeout=60)
    assert not (env.state / "handshake_timeouts").exists()
    return p.returncode, out, err


def _swap_once(env: Env) -> None:
    """先正常換入一次：.next=NEW、.next-prev=OLD、重啟 1 次（--rollback 情境的前置）。"""
    assert env.run().returncode == 0


def _assert_deploy_ok(env: Env, rc: int, out: str, err: str) -> None:
    """與「沒有收到訊號」相同的結果：結束碼 0、最終狀態符合 H2 表。"""
    msg = f"rc={rc}\n{out}\n{err}"
    assert rc == 0, msg
    assert env.live_id() == NEW_ID, msg
    assert (env.web / ".next-prev" / "BUILD_ID").read_text() == OLD_ID, msg
    assert env.restarts() == 1, msg
    assert "結束碼      : 0" in out, msg


def _assert_deploy_rolled_back(env: Env, rc: int, out: str, err: str) -> None:
    """與「沒有收到訊號」相同的結果：結束碼 4、.next 是舊版、失敗的新版在 .next-failed、重啟 2 次。"""
    msg = f"rc={rc}\n{out}\n{err}"
    assert rc == 4, msg
    assert env.live_id() == OLD_ID, msg
    assert (env.web / ".next-failed" / "BUILD_ID").read_text() == NEW_ID, msg
    assert not (env.web / ".next-prev").exists(), msg
    assert env.restarts() == 2, msg
    assert "結束碼      : 4" in err, msg


def _assert_rollback_ok(env: Env, rc: int, out: str, err: str) -> None:
    msg = f"rc={rc}\n{out}\n{err}"
    assert rc == 0, msg
    assert env.live_id() == OLD_ID, msg
    assert (env.web / ".next-failed" / "BUILD_ID").read_text() == NEW_ID, msg
    assert not (env.web / ".next-prev").exists(), msg
    assert env.restarts() == 2, msg  # 換入 1 次 ＋ --rollback 1 次
    assert "結束碼      : 0" in out, msg


# ---- 不可中斷階段 × 八個時機（TERM／INT／HUP 各至少一次；旗標檔證明訊號在該時機送達）
def test_h4_swap_signal_between_two_renames_term(env: Env) -> None:
    """時機 1：換入的兩個 rename 之間（mv .next .next-prev 剛完成）送 TERM → 照常跑完，結束碼 0。"""
    rc, out, err = _signalled_run(
        env, (), "pair", signal.SIGTERM, SUDO=_hs_sudo(env),
        HS_SUDO_NAME="pair", HS_SUDO_CMD="mv", HS_SUDO_SRC=".next", HS_SUDO_DST=".next-prev",
    )  # fmt: skip
    _assert_deploy_ok(env, rc, out, err)


def test_h4_swap_signal_during_restart_hup(env: Env) -> None:
    """時機 2：重啟指令執行期間送 HUP → 照常跑完（重啟與健康檢查不被打斷），結束碼 0。"""
    rc, out, err = _signalled_run(
        env, (), "restart", signal.SIGHUP, HS_RESTART_AT="1", HS_RESTART_NAME="restart"
    )
    _assert_deploy_ok(env, rc, out, err)


def test_h4_swap_signal_during_health_check_int(env: Env) -> None:
    """時機 3：健康檢查期間（第一次首頁請求中）送 INT → 照常跑完，結束碼 0。"""
    rc, out, err = _signalled_run(env, (), "health", signal.SIGINT, HS_CURL_NAME="health")
    _assert_deploy_ok(env, rc, out, err)


def test_h4_swap_signal_after_second_rename_before_restart_int(env: Env) -> None:
    """（原 S1 改寫）第二個 rename 剛完成、尚未重啟時送 INT → 不再需要「視同已換入」特判，照常跑完，結束碼 0。"""
    rc, out, err = _signalled_run(
        env, (), "after2", signal.SIGINT, SUDO=_hs_sudo(env),
        HS_SUDO_NAME="after2", HS_SUDO_CMD="mv", HS_SUDO_SRC=".next", HS_SUDO_DST=".next",
    )  # fmt: skip
    _assert_deploy_ok(env, rc, out, err)


def test_h4_auto_rollback_signal_during_rm_next_failed_term(env: Env) -> None:
    """時機 4：自動回退刪除既有 .next-failed 期間（已 rename 成 .next-failed.deleting-<PID>、rm -rf 之前）送 TERM
    → 照常回退完成，結束碼 4。"""
    (env.web / ".next-failed").mkdir()
    (env.web / ".next-failed" / "old.txt").write_text("old failure\n")
    rc, out, err = _signalled_run(
        env, (), "rmfailed", signal.SIGTERM, SUDO=_hs_sudo(env), FAKE_BAD_BUILD_ID=NEW_ID,
        HS_SUDO_NAME="rmfailed", HS_SUDO_CMD="rm", HS_SUDO_SRC="-rf", HS_SUDO_DST=".next-failed.deleting-*",
        HS_SUDO_WHEN="before",
    )  # fmt: skip
    _assert_deploy_rolled_back(env, rc, out, err)


def test_h4_auto_rollback_signal_between_two_renames_hup(env: Env) -> None:
    """時機 5：自動回退的兩個 rename 之間（.next -> .next-failed 剛完成）送 HUP → 照常回退完成，結束碼 4。"""
    rc, out, err = _signalled_run(
        env, (), "rbpair", signal.SIGHUP, SUDO=_hs_sudo(env), FAKE_BAD_BUILD_ID=NEW_ID,
        HS_SUDO_NAME="rbpair", HS_SUDO_CMD="mv", HS_SUDO_SRC=".next", HS_SUDO_DST=".next-failed",
    )  # fmt: skip
    _assert_deploy_rolled_back(env, rc, out, err)


def test_h4_auto_rollback_signal_during_restart_int(env: Env) -> None:
    """時機 6：自動回退的重啟（第 2 次重啟）期間送 INT → 照常跑完，結束碼 4。"""
    rc, out, err = _signalled_run(
        env, (), "rbrestart", signal.SIGINT, FAKE_BAD_BUILD_ID=NEW_ID,
        HS_RESTART_AT="2", HS_RESTART_NAME="rbrestart",
    )  # fmt: skip
    _assert_deploy_rolled_back(env, rc, out, err)


def test_h4_rollback_mode_signal_between_two_renames_term(env: Env) -> None:
    """時機 7：--rollback 的兩個 rename 之間送 TERM → 照常回退完成，結束碼 0（不是 143）。"""
    _swap_once(env)
    rc, out, err = _signalled_run(
        env, ("--rollback",), "rb2pair", signal.SIGTERM, SUDO=_hs_sudo(env),
        HS_SUDO_NAME="rb2pair", HS_SUDO_CMD="mv", HS_SUDO_SRC=".next", HS_SUDO_DST=".next-failed",
    )  # fmt: skip
    _assert_rollback_ok(env, rc, out, err)


def test_h4_rollback_mode_signal_during_restart_hup(env: Env) -> None:
    """時機 8：--rollback 的重啟（整體第 2 次重啟）期間送 HUP → 照常跑完，結束碼 0。"""
    _swap_once(env)
    rc, out, err = _signalled_run(
        env, ("--rollback",), "rb2restart", signal.SIGHUP, HS_RESTART_AT="2", HS_RESTART_NAME="rb2restart"
    )
    _assert_rollback_ok(env, rc, out, err)


# ---- 行程群組訊號（Ctrl-C／ssh 斷線會打到整個群組，不只腳本）
@pytest.mark.parametrize("where", ["pair", "restart"])
def test_h4_process_group_signal_does_not_interrupt_mv_or_restart(env: Env, where: str) -> None:
    """對整個行程群組送訊號（腳本在新 session，群組＝腳本 pid）：mv 與重啟子行程繼承被忽略的訊號，不被打斷。"""
    if where == "pair":
        rc, out, err = _signalled_run(
            env, (), "gpair", signal.SIGTERM, group=True, SUDO=_hs_sudo(env),
            HS_SUDO_NAME="gpair", HS_SUDO_CMD="mv", HS_SUDO_SRC=".next", HS_SUDO_DST=".next-prev",
        )  # fmt: skip
    else:
        rc, out, err = _signalled_run(
            env, (), "grestart", signal.SIGINT, group=True, HS_RESTART_AT="1", HS_RESTART_NAME="grestart"
        )
    _assert_deploy_ok(env, rc, out, err)


# ---- SIGPIPE：終端機／管線在換入後消失
def test_h4_closed_output_pipes_after_swap_still_complete_auto_rollback(env: Env) -> None:
    """stdout／stderr 讀端在換入後關閉（tee 被殺、無 tty 的 ssh 斷線），健康檢查失敗 → 仍完成自動回退
    （.next 為舊版、結束碼 4），LOG_FILE 內有最終狀態報告。"""
    rc, _, _ = _signalled_run(
        env, (), "pipe", None, close_pipes=True, FAKE_BAD_BUILD_ID=NEW_ID,
        HS_RESTART_AT="1", HS_RESTART_NAME="pipe",
    )  # fmt: skip
    assert rc == 4
    assert env.live_id() == OLD_ID
    assert (env.web / ".next-failed" / "BUILD_ID").read_text() == NEW_ID
    assert env.restarts() == 2
    log = env.log_text()
    assert "最終狀態報告" in log and "結束碼      : 4" in log


# ---- 可中斷階段
def test_h4_interruptible_phase_term_during_build_leaves_live_untouched(env: Env) -> None:
    """build 期間送 TERM → 結束碼 143、WEB_DIR digest 不變、沒有重啟；build 子行程被終止。"""
    before = _tree_digest(env.web)
    rc, out, err = _signalled_run(env, (), "build", signal.SIGTERM, HS_NPM_NAME="build")
    assert rc == 143, f"{out}\n{err}"
    assert _tree_digest(env.web) == before
    assert env.restarts() == 0
    assert not (env.web / ".next-prev").exists()
    assert "結束碼      : 143" in err


# ------------------------------------------------------------------ Task I / I1：每次執行獨立的 run-* 工作目錄
def _shim_setsid(env: Env, *, new_session: bool) -> dict[str, str]:
    """在 PATH 前面放一個假 setsid。new_session=False：只 exec（行程群組不變，等同沒有 setsid 的行為，
    但走「有 setsid」的程式分支；group kill 會失敗並退回只殺直接子行程）；True：真的 os.setsid() 再 exec。
    這讓測試在 macOS（沒有 setsid）與 Linux（有 util-linux setsid）上行為一致。"""
    import sys

    body = (
        f"""#!/usr/bin/env bash
exec {sys.executable} -c 'import os,sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' "$@"
"""
        if new_session
        else """#!/usr/bin/env bash
exec "$@"
"""
    )
    _write_exe(env.bin / "setsid", body)
    return {"PATH": f"{env.bin}{os.pathsep}{os.environ['PATH']}"}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_i1_interrupted_build_leftover_process_cannot_contaminate_next_run(env: Env) -> None:
    """W1：第一次執行在 build 期間被 TERM，它的孫行程殘留（等旗標檔 stale_go 才寫 .next/server/stale.js 到它啟動時的工作目錄）。
    第二次執行確實進入 build（握手 build2：它的 build 停在那裡等測試放行）之後，測試才放行殘留行程，等它寫完，
    再放行第二次 build → 第二次結束碼 0，換上線的 .next 內沒有 stale.js；兩次執行的 run-* 目錄名不同；
    殘留行程確實寫了——只是寫進它自己的舊目錄。沒有任何固定延遲。"""
    import time

    path_env = _shim_setsid(env, new_session=False)  # 孫行程不被 group kill 殺掉（確保殘留行程真的存在）
    rc1, _, _ = _signalled_run(
        env, (), "build", signal.SIGTERM, HS_NPM_NAME="build", FAKE_NPM_STALE_WAIT="1", **path_env
    )
    assert rc1 == 143
    p2 = env.popen(HS_NPM_WAIT="build2", **path_env)
    _wait_flag(env, p2, "build2")  # 第二次執行確實進入 build 了
    (env.state / "stale_go").touch()  # 現在才放行殘留行程：它在第二次 build 進行中寫檔
    deadline = time.time() + 10
    while not (env.state / "stale_done").exists() and time.time() < deadline:
        time.sleep(0.02)
    assert (env.state / "stale_done").exists(), "殘留行程沒有寫檔——測試沒有鑑別力"
    (env.state / "delivered_build2").touch()  # 放行第二次 build
    out2, err2 = p2.communicate(timeout=60)
    assert p2.returncode == 0, f"{out2}\n{err2}"
    cwds = (env.state / "npm_cwd").read_text().split()
    assert len(cwds) == 2
    assert not (env.web / ".next" / "server" / "stale.js").exists()  # 沒有混進換上線的產物
    assert (Path(cwds[0]) / ".next" / "server" / "stale.js").exists()  # 殘留行程寫進了它自己的舊目錄
    assert env.live_id() == NEW_ID
    assert cwds[0] != cwds[1]  # 兩次執行的 run-* 目錄名不同
    assert all(Path(c).parent == env.build and Path(c).name.startswith("run-") for c in cwds)


def test_i1_setsid_branch_terminates_whole_process_group(env: Env) -> None:
    """有 setsid 時 build 自成 session：TERM 腳本 → 對整個行程群組送 TERM，連孫行程一起終止（盡力而為）。
    用假 setsid（真的 os.setsid()）執行這個分支；真正的 util-linux setsid 只能在正式機確認。
    以輪詢「孫行程是否還存在」取代固定等待。"""
    import time

    path_env = _shim_setsid(env, new_session=True)
    rc, _, _ = _signalled_run(
        env, (), "build", signal.SIGTERM, HS_NPM_NAME="build", FAKE_NPM_STALE_WAIT="1", **path_env
    )
    assert rc == 143
    pid = int((env.state / "stale_pid").read_text())
    deadline = time.time() + 5
    while _alive(pid) and time.time() < deadline:
        time.sleep(0.02)
    assert not _alive(pid), "孫行程應該隨行程群組一起被終止"
    assert list(env.build.rglob("stale.js")) == []
    assert env.live_id() == OLD_ID


def test_i1_failed_build_keeps_run_dir_and_next_run_cleans_it(env: Env) -> None:
    """失敗時保留本次 run-* 供排查並在報告寫出路徑；下一次執行開頭清掉舊的 run-*，成功後刪除本次的。"""
    r = env.run(FAKE_NPM_FAIL="1")
    assert r.returncode == 1, _out(r)
    runs = list(env.build.glob("run-*"))
    assert len(runs) == 1
    assert str(runs[0]) in r.stderr and "保留供排查" in r.stderr
    r2 = env.run()
    assert r2.returncode == 0, _out(r2)
    assert list(env.build.glob("run-*")) == []
    assert "已刪除" in r2.stdout


# ------------------------------------------------------------------ Task I / I2：刪除前先失效
def test_i2_partial_delete_of_retired_prev_does_not_leave_a_rollback_source(env: Env) -> None:
    """舊 .next-prev 先 rename 成 .next-prev.deleting-<PID> 再刪；刪到一半失敗 → 只留垃圾目錄，換入照常完成（0）。
    新的 .next-prev 是剛換下來的完整舊版（可供 --rollback），不存在名為 .next-prev 的殘缺目錄。"""
    prev = env.web / ".next-prev"
    (prev / "static").mkdir(parents=True)
    (prev / "BUILD_ID").write_text("ANCIENT")
    (prev / "static" / "a.js").write_text("a\n")
    r = env.run(SUDO=_hs_sudo(env), HS_PARTIAL_RM_GLOB=".next-prev.deleting-*")
    assert r.returncode == 0, _out(r)
    assert env.live_id() == NEW_ID
    assert (env.web / ".next-prev" / "BUILD_ID").read_text() == OLD_ID  # 剛換下來的完整舊版
    assert (env.web / ".next-prev" / "static" / "chunks" / "old.js").is_file()
    garbage = list(env.web.glob(".next-prev.deleting-*"))
    assert len(garbage) == 1 and not (garbage[0] / "BUILD_ID").exists()  # 殘骸只在垃圾目錄裡，且不是回退來源
    assert ".deleting-" in r.stderr


def test_i2_rename_failure_of_prev_aborts_with_code_1_and_does_not_recommend_rollback(env: Env) -> None:
    """.next-prev -> .next-prev.deleting-<PID> 的 rename 失敗 → 結束碼 1、.next 與 .next-prev 的 digest 都不變；
    報告的「下一步」明講不要執行 --rollback。"""
    prev = env.web / ".next-prev"
    (prev / "static").mkdir(parents=True)
    (prev / "BUILD_ID").write_text("ANCIENT")
    (prev / "static" / "a.js").write_text("a\n")
    before_next = _tree_digest(env.web / ".next")
    before_prev = _tree_digest(prev)
    r = env.run(SUDO=_hs_sudo(env), HS_FAIL_MV_DST_GLOB=".next-prev.deleting-*")
    assert r.returncode == 1, _out(r)
    assert _tree_digest(env.web / ".next") == before_next
    assert _tree_digest(prev) == before_prev
    assert env.restarts() == 0
    next_line = next(line for line in r.stderr.splitlines() if line.startswith("下一步"))
    assert "不要執行 --rollback" in next_line
    assert f"bash {SCRIPT} --rollback" not in next_line  # 不是建議執行它
    assert "結束碼      : 1" in r.stderr


def test_i2_step2_cleans_stale_deleting_dirs_and_sync_excludes_next_dirs(env: Env) -> None:
    """步驟 2 清掉 WEB_DIR 內殘留的 .next-prev.deleting-*／.next-failed.deleting-*；原始碼同步排除所有 .next* 目錄。"""
    for name in (".next-prev.deleting-111", ".next-failed.deleting-222"):
        (env.web / name).mkdir()
        (env.web / name / "junk.txt").write_text("junk\n")
    r = env.run()
    assert r.returncode == 0, _out(r)
    assert not list(env.web.glob(".next-*.deleting-*")) and not (env.web / ".next-failed").exists()
    first_ls = (env.state / "build_view").read_text().split("ls:")[1].split("\n")[0]
    assert "deleting" not in first_ls and ".next-prev" not in first_ls


# ------------------------------------------------------------------ Task I / I3：報告如實
def test_i3_report_states_last_successful_restart_and_consistency_when_rollback_cannot_move_failed(env: Env) -> None:
    """健康檢查失敗、自動回退時既有 .next-failed 無法移走 → 結束碼 5；.next 仍是新版（檔案與正在服務的一致）；
    報告寫明「最後一次成功重啟」是新版的 BUILD_ID，而不是「重啟指令失敗」。"""
    failed = env.web / ".next-failed"
    failed.mkdir()
    (failed / "evidence.txt").write_text("keep\n")
    r = env.run(SUDO=_hs_sudo(env), FAKE_BAD_BUILD_ID=NEW_ID, HS_FAIL_MV_DST_GLOB=".next-failed.deleting-*")
    assert r.returncode == 5, _out(r)
    assert env.live_id() == NEW_ID
    assert (failed / "evidence.txt").read_text() == "keep\n"
    assert f"最後一次成功重啟：{NEW_ID}" in r.stderr, _out(r)
    assert f"目前 .next 與正在服務的版本：一致（檔案與正在服務的都是 {NEW_ID}）" in r.stderr
    assert "重啟指令失敗" not in r.stderr


def test_i3_report_says_never_restarted_when_no_restart_succeeded(env: Env) -> None:
    """沒有成功重啟過：「最後一次成功重啟」寫明本次執行未成功重啟過；正在服務的是執行前的版本。"""
    r = env.run(FAKE_NPM_FAIL="1")
    assert "最後一次成功重啟：本次執行未成功重啟過" in r.stderr
    # J2：本次執行未嘗試重啟 → 第 3 種文字（不再寫「一致」——腳本不知道執行前的服務是否健康）
    assert "目前 .next 與正在服務的版本：服務未被本次執行重啟（仍是執行前啟動的行程）" in r.stderr
    assert "一致" not in _line(r.stderr, "目前 .next 與正在服務的版本")


# ------------------------------------------------------------------ Task I / I4
def test_i4_files_rolled_requires_prev_to_be_gone_even_if_build_ids_equal(env: Env) -> None:
    """I4.1：新舊 BUILD_ID 相同、回退的第二個 rename 失敗（.next-prev 仍在）→ 不得判定「回退的檔案層完成」：
    報告不能說「只差重啟」（那會叫人只重啟一次）。"""
    r = env.run(
        SUDO=_hs_sudo(env), FAKE_NEW_ID=OLD_ID, FAKE_RESTART_FAIL_FROM="1", HS_FAIL_MV_SRCBASE=".next-prev"
    )
    assert r.returncode == 5, _out(r)
    assert (env.web / ".next-prev").exists()  # 回退沒有完成
    assert f"下一步      : {env.bin / 'restart.sh'}" not in r.stderr, _out(r)
    assert "目前線上 .next 的 BUILD_ID" in r.stderr


def test_i4_unwritable_log_file_fails_in_preflight_without_touching_anything(env: Env) -> None:
    """I4.2：LOG_FILE 無法建立或寫入 → 前置檢查階段直接失敗：結束碼 1、未動任何東西、訊息說明；不降級成 /dev/null。"""
    before = _tree_digest(env.root)
    r = env.run(LOG_FILE=str(env.root / "no-such-dir" / "x.log"))
    assert r.returncode == 1, _out(r)
    assert "LOG_FILE" in r.stderr and "未動任何東西" in r.stderr
    assert _tree_digest(env.root) == before
    assert env.restarts() == 0 and not (env.state / "npm_cwd").exists()


def test_i4_default_log_file_is_created_by_mktemp_with_mode_0600(env: Env) -> None:
    """I4.2：沒有指定 LOG_FILE 時用 mktemp 產生（權限 0600）。"""
    r = env.run(LOG_FILE="")
    assert r.returncode == 0, _out(r)
    logs = list(env.root.glob("web_build_swap.log.*"))
    assert len(logs) == 1
    assert (logs[0].stat().st_mode & 0o777) == 0o600
    assert "最終狀態報告" in logs[0].read_text()


@pytest.mark.skipif(os.geteuid() == 0, reason="root 讀得到 chmod 000 的檔案")
def test_i4_unexpected_set_e_exit_in_interruptible_phase_leaves_an_error_line(env: Env) -> None:
    """I4.3：可中斷階段因 set -e 靜默結束的路徑（步驟 4 讀不到舊 BUILD_ID）→ ERR trap 印出
    「ERROR：未預期的指令失敗（第 N 行…）」，使報告中「排除上面的 ERROR 原因」恆有對應訊息。"""
    bid_file = env.web / ".next" / "BUILD_ID"
    bid_file.chmod(0)
    try:
        r = env.run()
    finally:
        bid_file.chmod(0o644)
    assert r.returncode == 1, _out(r)
    assert re.search(r"ERROR：未預期的指令失敗（第 \d+ 行", r.stderr), _out(r)
    assert "結束碼      : 1" in r.stderr and env.restarts() == 0


def test_i4_health_check_asset_loop_shares_the_total_deadline(env: Env) -> None:
    """I4.4：逐一檢查靜態資源也受同一個總時限約束。資源各回 200 但每個要 2 秒、總時限 1 秒 →
    健康檢查以「超過總時限」失敗（否則 4 秒後會判定通過）。"""
    r = env.run(FAKE_ASSET_SLEEP="2", HEALTH_TIMEOUT_S="1")
    assert r.returncode != 0, _out(r)
    assert "超過總時限" in r.stderr, _out(r)


def test_i4_script_comments_describe_exit_141_as_reserved() -> None:
    """I4.5：檔頭與 emit 的註解不再宣稱「可中斷階段寫入已關閉的管線會終止腳本＝141」。"""
    text = SCRIPT.read_text()
    assert "141          保留碼" in text
    assert "寫入已關閉的管線會終止腳本" not in text
    assert "129/130/143/141  只可能發生在可中斷階段" not in text


# ------------------------------------------------------------------ Task J：報告文字只陳述已知事實
def test_j1_rollback_refused_without_prev_next_step_points_to_full_redeploy(env: Env) -> None:
    """J1：--rollback 因 .next-prev 無效而回 1 → 「下一步」不是「不要執行 --rollback」，而是回退沒有執行＋整包重部署（§9.3）。"""
    r = env.run("--rollback")
    assert r.returncode == 1, _out(r)
    nxt = _line(r.stderr, "下一步")
    assert "不要執行 --rollback" not in nxt and "沒有任何東西需要回退" not in nxt, nxt
    assert "回退沒有執行" in nxt and "§9.3" in nxt, nxt


def test_j1_rollback_refused_by_maintenance_flag_next_step_mentions_force(env: Env) -> None:
    """J1：--rollback 因維護旗標存在而回 1 → 「下一步」含「--force」，且不叫人不要回退。"""
    _swap_once(env)
    env.flag.parent.mkdir()
    env.flag.write_text("")
    r = env.run("--rollback")
    assert r.returncode == 1, _out(r)
    nxt = _line(r.stderr, "下一步")
    assert "不要執行 --rollback" not in nxt and "沒有任何東西需要回退" not in nxt, nxt
    assert "--force" in nxt and "回退沒有執行" in nxt, nxt


def test_j2_deploy_both_restarts_fail_reports_unknown_not_consistent(env: Env) -> None:
    """J2：一般模式兩次重啟都失敗 → 結束碼 5；「目前 .next 與正在服務的版本」寫「未知」，不得寫「一致」。"""
    r = env.run(FAKE_RESTART_FAIL_FROM="1")
    assert r.returncode == 5, _out(r)
    line = _line(r.stderr, "目前 .next 與正在服務的版本")
    assert "未知" in line and "systemctl is-active filet-dashboard" in line, line
    assert "一致" not in line, line


def test_j2_rollback_restart_failure_does_not_assert_which_version_is_serving(env: Env) -> None:
    """J2：--rollback 重啟失敗 → 該行含「未知」，不得斷言「正在服務的是」哪一版。"""
    _swap_once(env)
    r = env.run("--rollback", FAKE_RESTART_FAIL_FROM="2")
    assert r.returncode == 5, _out(r)
    line = _line(r.stderr, "目前 .next 與正在服務的版本")
    assert "未知" in line and "正在服務的是" not in line and "一致" not in line, line


def test_j2_no_restart_attempted_reports_third_text_and_notes_changed_files(env: Env) -> None:
    """J2：換入中途失敗已還原、未嘗試重啟 → 第 3 種文字（服務未被本次執行重啟）；檔案與執行前相同就不加註。
    檔案已變更（.next 缺席）而未嘗試重啟時加註「檔案已變更但尚未重啟」。"""
    sudo = _hs_sudo(env)
    (env.bin / "hs_sudo").write_text(
        HS_SUDO.replace(
            'if [ "$1" = mv ] && [ -n "${HS_FAIL_MV_DST:-}" ] && [ "${3##*/}" = "$HS_FAIL_MV_DST" ]; then exit 1; fi',
            'if [ "$1" = mv ] && [[ "$2" == *web-build* ]]; then exit 1; fi',
        )
    )
    r = env.run(SUDO=sudo)
    assert r.returncode == 1, _out(r)
    line = _line(r.stderr, "目前 .next 與正在服務的版本")
    assert line == "目前 .next 與正在服務的版本：服務未被本次執行重啟（仍是執行前啟動的行程）", line
    # 變更後未重啟：.next 缺席（還原也失敗）
    env2_sudo = _hs_sudo(env)  # 還原成原版 hs_sudo 才能用 HS_FAIL_MV_DST
    _write_exe(env.bin / "hs_sudo", HS_SUDO)
    r2 = env.run(SUDO=env2_sudo, HS_FAIL_MV_DST=".next")
    assert r2.returncode == 5, _out(r2)
    line2 = _line(r2.stderr, "目前 .next 與正在服務的版本")
    assert "服務未被本次執行重啟" in line2 and "檔案已變更但尚未重啟" in line2, line2


def test_j3_recovery_mode_never_moves_failed_into_next(env: Env) -> None:
    """J3-1：復原模式（.next 缺席）下 .next-prev 的 rename 持續失敗，既有 .next-failed 絕不被搬成 .next：
    結束碼 5、.next 仍缺席、.next-failed 內容不變。（非復原模式的補救仍在——見 test_i4_files_rolled_requires_prev_to_be_gone…）"""
    import shutil

    shutil.rmtree(env.web / ".next")
    _make_prev(env, "PREVBUILD")
    failed = env.web / ".next-failed"
    failed.mkdir()
    (failed / "evidence.txt").write_text("keep this evidence\n")
    before = _tree_digest(failed)
    r = env.run("--rollback", SUDO=_hs_sudo(env), HS_FAIL_MV_SRCBASE=".next-prev")
    assert r.returncode == 5, _out(r)
    assert not (env.web / ".next").exists()
    assert _tree_digest(failed) == before
    assert env.restarts() == 0


def test_j3_unexpected_failure_inside_command_substitution_prints_exactly_one_error_line(env: Env) -> None:
    """J3-2：BUILD_DIR 的上層是一般檔案 → phys_path 在 $(...) 內失敗（正常失敗），之後由 die 處理：
    輸出中以 ERROR 開頭的行恰一行（die 的訊息），不再多一行誤導的「未預期」ERROR。"""
    afile = env.root / "a-regular-file"
    afile.write_text("x\n")
    r = env.run(BUILD_DIR=str(afile / "sub"))
    assert r.returncode == 1, _out(r)
    error_lines = [ln for ln in r.stderr.splitlines() if ln.startswith("[web_build_swap] ERROR")]
    assert len(error_lines) == 1, error_lines
    assert "無法解析 BUILD_DIR" in error_lines[0]


def test_j3_old_run_symlink_is_unlinked_not_followed(env: Env) -> None:
    """J3-3：BUILD_DIR 內舊的 run-x 是指向別處的符號連結 → 只移除連結本身，目標目錄內容完好。"""
    scratch = env.root / "scratch-target"
    scratch.mkdir()
    (scratch / "keep.txt").write_text("keep\n")
    env.build.mkdir()
    (env.build / ".web_build_swap_dir").write_text("")
    (env.build / "run-x").symlink_to(scratch)
    r = env.run()
    assert r.returncode == 0, _out(r)
    assert not (env.build / "run-x").exists() and not (env.build / "run-x").is_symlink()
    assert (scratch / "keep.txt").read_text() == "keep\n"


# ------------------------------------------------------------------ Task K：用法錯誤與被訊號中斷時的報告
def _assert_usage_error_report(env: Env, r: subprocess.CompletedProcess[str], before_web: dict[str, str]) -> None:
    """K1：結束碼 2 的報告對線上狀態與是否需要回退不做任何斷言。"""
    assert r.returncode == 2, _out(r)
    assert "模式        : 未定（參數錯誤）" in r.stderr, _out(r)
    nxt = _line(r.stderr, "下一步")
    assert "參數錯誤" in nxt and "--help" in nxt, nxt
    for text in (r.stderr, r.stdout, env.log_text()):
        for forbidden in ("不要執行 --rollback", "沒有任何東西需要回退", "回退沒有執行"):
            assert forbidden not in text, (forbidden, text)
    assert _tree_digest(env.web) == before_web
    assert env.restarts() == 0 and not (env.state / "npm_cwd").exists()


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(("--rollbak",), id="typo"),
        pytest.param(("--force", "--bogus", "--rollback"), id="unknown-before-rollback"),
        pytest.param(("--rollback", "--bogus"), id="unknown-after-rollback"),
    ],
)
def test_k1_usage_error_report_makes_no_claim_about_rollback(env: Env, args: tuple[str, ...]) -> None:
    """K1：參數打錯（打錯字／未知參數在 --rollback 之前／之後）→ 結束碼 2；報告「模式：未定（參數錯誤）」，
    「下一步」只說參數錯誤與 --help；整份報告不含「不要執行 --rollback」等斷言；web/ 不變。"""
    before = _tree_digest(env.web)
    r = env.run(*args)
    _assert_usage_error_report(env, r, before)


def _assert_signal_next_step(r_err: str) -> None:
    nxt = _line(r_err, "下一步")
    assert "被訊號中斷" in nxt and "直接重跑" in nxt, nxt
    assert "ERROR" not in nxt, nxt


def test_k2_signal_during_build_next_step_says_just_rerun(env: Env) -> None:
    """K2：一般模式 build 期間 TERM → 143；「下一步」是「被訊號中斷，線上未受影響；直接重跑即可」，不提 ERROR。"""
    rc, out, err = _signalled_run(env, (), "build", signal.SIGTERM, HS_NPM_NAME="build")
    assert rc == 143, f"{out}\n{err}"
    _assert_signal_next_step(err)


def test_k2_signal_during_rollback_preflight_next_step_says_just_rerun(env: Env) -> None:
    """K2：--rollback 前置階段也能穩定送達訊號（假 systemctl 在 is-active 檢查中握手）→ 143；兩種模式共用同一句話。"""
    _swap_once(env)
    rc, out, err = _signalled_run(
        env, ("--rollback",), "preflight", signal.SIGTERM, HS_SYSTEMCTL_NAME="preflight"
    )
    assert rc == 143, f"{out}\n{err}"
    _assert_signal_next_step(err)
    assert env.live_id() == NEW_ID and env.restarts() == 1  # 回退沒有發生、線上不變


def test_k3_health_timeout_with_leading_zero_is_normalized(env: Env) -> None:
    """K3：HEALTH_TIMEOUT_S=08 → 當成 8（十進位）正常運作：健康檢查確實有執行（stdout 提示「最多 8s」、報告「通過」）。"""
    r = env.run(HEALTH_TIMEOUT_S="08")
    assert r.returncode == 0, _out(r)
    assert "最多 8s" in r.stdout
    assert "健康檢查    : 通過" in r.stdout
    assert "value too great" not in r.stderr and "arithmetic" not in r.stderr.lower()


@pytest.mark.parametrize("value", ["0", "00", "abc", "1x"])
def test_k3_invalid_health_timeout_is_a_usage_error(env: Env, value: str) -> None:
    """K3：HEALTH_TIMEOUT_S 為 0／非數字 → 結束碼 2、未動任何東西、報告同 K1。"""
    before = _tree_digest(env.web)
    r = env.run(HEALTH_TIMEOUT_S=value)
    _assert_usage_error_report(env, r, before)
    assert "HEALTH_TIMEOUT_S" in r.stderr


# ------------------------------------------------------------------ Task L：前置檢查加強與用法錯誤報告
def test_l1_incomplete_live_next_is_refused_before_anything(env: Env) -> None:
    """L1：線上 .next 有 BUILD_ID 但沒有 static/ → 不是完整版本（回退來源判準）：結束碼 1、WEB_DIR digest 不變、build 沒有被呼叫。"""
    import shutil

    shutil.rmtree(env.web / ".next" / "static")
    before = _tree_digest(env.web)
    r = env.run()
    assert r.returncode == 1, _out(r)
    assert "不是完整版本" in r.stderr and "§9.3" in r.stderr
    assert _tree_digest(env.web) == before
    assert not (env.state / "npm_cwd").exists() and env.restarts() == 0


@pytest.mark.parametrize("value", ["601", "3000", "99999999999999999999", "9223372036854775808"])
def test_l2_health_timeout_out_of_range_is_a_usage_error(env: Env, value: str) -> None:
    """L2：HEALTH_TIMEOUT_S 超出 1–600（含會溢位的巨大值）→ 結束碼 2、訊息含允許範圍、WEB_DIR digest 不變。"""
    before = _tree_digest(env.web)
    r = env.run(HEALTH_TIMEOUT_S=value)
    _assert_usage_error_report(env, r, before)
    assert "1–600" in r.stderr, _out(r)


def test_l2_health_timeout_upper_bound_600_is_accepted(env: Env) -> None:
    """L2：600 是允許的上限 → 接受，流程成功（含前導零的 0600 也接受）。"""
    r = env.run(HEALTH_TIMEOUT_S="600")
    assert r.returncode == 0, _out(r)
    assert "最多 600s" in r.stdout
    r2 = env.run(HEALTH_TIMEOUT_S="0000600", FAKE_NEW_ID="SECOND")
    assert r2.returncode == 0, _out(r2)


@pytest.mark.parametrize("kw", [{"args": ("--bogus",)}, {"env": {"HEALTH_TIMEOUT_S": "abc"}}])
def test_l3_usage_error_report_makes_no_claim_about_the_service_process(env: Env, kw: dict) -> None:
    """L3：用法錯誤的報告不印「仍是執行前啟動的行程」；服務行程的兩行都是「（參數錯誤，未檢查）」。"""
    r = env.run(*kw.get("args", ()), **kw.get("env", {}))
    assert r.returncode == 2, _out(r)
    assert _line(r.stderr, "最後一次成功重啟") == "最後一次成功重啟：（參數錯誤，未檢查）"
    assert _line(r.stderr, "目前 .next 與正在服務的版本") == "目前 .next 與正在服務的版本：（參數錯誤，未檢查）"
    for text in (r.stderr, r.stdout, env.log_text()):
        assert "仍是執行前啟動的行程" not in text


# ---- flock 的 fd 9 不得被 build 子行程繼承（build 指令的 9>&-）
def test_h1_build_child_does_not_inherit_lock_fd9(env: Env) -> None:
    """以「先開 fd 9 再 exec 腳本」模擬腳本持有的 flock fd：build（假 npm）看不到 fd 9，
    對照組（沒有 9>&- 的假 curl）看得到——證明這個斷言有鑑別力。"""
    (env.bin / "curl").write_text(
        FAKE_CURL.replace(
            'echo "$url" >> "$FAKE_STATE/curl_urls"',
            'echo "$url" >> "$FAKE_STATE/curl_urls"; [ -e /dev/fd/9 ] && echo open >> "$FAKE_STATE/fd9_curl"',
        )
    )
    lockfile = env.root.parent / f"{env.root.name}-fd9.lock"
    r = subprocess.run(
        ["bash", "-c", 'exec 9>"$1"; shift; exec bash "$@"', "wrapper", str(lockfile), str(SCRIPT)],
        env=env.build_env(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert r.returncode == 0, _out(r)
    assert (env.state / "fd9_curl").exists()  # 對照：沒關 fd 9 的子行程確實繼承得到
    assert not (env.state / "fd9_npm").exists()  # build 子行程沒有繼承


# ---- 輸出：不可中斷階段開始時的提示、報告同時寫 LOG
def test_h4_uninterruptible_banner_and_log_mirror(env: Env) -> None:
    r = env.run()
    assert r.returncode == 0, _out(r)
    assert "從這裡開始不可中斷" in r.stdout and "最長約" in r.stdout
    log = env.log_text()
    assert "從這裡開始不可中斷" in log and "最終狀態報告" in log


# ---- H2 結束碼表：每一列直接斷言「結束碼＋最終狀態」
def test_h2_code0_deploy_final_state(env: Env) -> None:
    r = env.run()
    assert r.returncode == 0, _out(r)
    assert env.live_id() == NEW_ID and (env.web / ".next-prev" / "BUILD_ID").read_text() == OLD_ID
    assert env.restarts() == 1
    assert "結束碼      : 0" in r.stdout and f".next       : {NEW_ID}" in r.stdout
    assert f".next-prev  : {OLD_ID}" in r.stdout and "健康檢查    : 通過" in r.stdout


def test_h2_code0_rollback_final_state(env: Env) -> None:
    _swap_once(env)
    r = env.run("--rollback")
    assert r.returncode == 0, _out(r)
    assert f".next       : {OLD_ID}" in r.stdout and f".next-failed: {NEW_ID}" in r.stdout
    assert ".next-prev  : 不存在" in r.stdout and "結束碼      : 0" in r.stdout


def test_h2_code1_midway_swap_failure_restored_without_restart(env: Env) -> None:
    """結束碼 1：換入中途失敗（第二個 rename 失敗）且已還原成舊版、服務未重啟、.next 內容與執行前相同。"""
    before = _tree_digest(env.web / ".next")
    sudo = _hs_sudo(env)
    # 只讓「目的地是 web/.next」的搬入失敗；還原用的 .next-prev -> .next 也是目的地 .next，所以用來源路徑區分
    (env.bin / "hs_sudo").write_text(
        HS_SUDO.replace(
            'if [ "$1" = mv ] && [ -n "${HS_FAIL_MV_DST:-}" ] && [ "${3##*/}" = "$HS_FAIL_MV_DST" ]; then exit 1; fi',
            'if [ "$1" = mv ] && [[ "$2" == *web-build* ]]; then exit 1; fi',
        )
    )
    r = env.run(SUDO=sudo)
    assert r.returncode == 1, _out(r)
    assert _tree_digest(env.web / ".next") == before
    assert env.restarts() == 0
    assert "結束碼      : 1" in r.stderr
    assert not (env.web / ".next-failed").exists()


def test_h2_code2_usage_error(env: Env) -> None:
    r = env.run("--nope")
    assert r.returncode == 2, _out(r)
    assert env.live_id() == OLD_ID and env.restarts() == 0


def test_h2_code4_when_swap_command_reports_failure_after_rename(env: Env) -> None:
    """結束碼 4：rename 成功但指令回報失敗（假 sudo 先 rename 再 exit 1）→ 以實況判定新版已換入，走自動回退。"""
    sudo = _hs_sudo(env)
    r = env.run(SUDO=sudo, HS_FAIL_MV_FROM="web-build")
    assert r.returncode == 4, _out(r)
    assert env.live_id() == OLD_ID
    assert (env.web / ".next-failed" / "BUILD_ID").read_text() == NEW_ID
    assert env.restarts() == 1  # 只有回退那次重啟；從未把新版重啟起來
    assert "結束碼      : 4" in r.stderr and "實況" in r.stderr


def test_h2_code5_when_next_is_missing_not_1(env: Env) -> None:
    """結束碼 5（不是 1）：.next 缺席。報告指示 --rollback。"""
    sudo = _hs_sudo(env)
    r = env.run(SUDO=sudo, HS_FAIL_MV_DST=".next")  # 所有目的地是 .next 的 mv 都失敗，含還原
    assert r.returncode == 5, _out(r)
    assert not (env.web / ".next").exists() and (env.web / ".next-prev" / "BUILD_ID").read_text() == OLD_ID
    assert "結束碼      : 5" in r.stderr
    assert "--rollback" in r.stderr and "復原模式" in r.stderr
    assert env.restarts() == 0


def test_h2_code5_rollback_restart_failure_reports_restart_cmd_not_redeploy(env: Env) -> None:
    """結束碼 5：回退的兩個 rename 已完成、只差重啟失敗 → 報告印出 RESTART_CMD 本身，且不叫人重部署。"""
    r = env.run(FAKE_BAD_BUILD_ID=NEW_ID, FAKE_RESTART_FAIL_AT="2")
    assert r.returncode == 5, _out(r)
    assert env.live_id() == OLD_ID  # 檔案層已回到舊版
    restart_cmd = str(env.bin / "restart.sh")
    assert f"下一步      : {restart_cmd}" in r.stderr, _out(r)
    for text in (r.stdout, r.stderr, env.log_text()):
        assert "重部署" not in text


def test_h2_code5_rollback_mode_restart_failure_reports_restart_cmd(env: Env) -> None:
    _swap_once(env)
    r = env.run("--rollback", FAKE_RESTART_FAIL_AT="2")
    assert r.returncode == 5, _out(r)
    assert f"下一步      : {env.bin / 'restart.sh'}" in r.stderr, _out(r)
    assert "重部署" not in r.stderr and "重部署" not in r.stdout


def test_h2_next_step_full_redeploy_when_prev_invalid_and_next_is_new(env: Env) -> None:
    """下一步分支「整包重部署」：回退無法進行（.next-prev 無效）且 .next 是新版。"""
    curl = FAKE_CURL.replace(
        'echo "$url" >> "$FAKE_STATE/curl_urls"',
        'echo "$url" >> "$FAKE_STATE/curl_urls"; rm -rf "$WEB_DIR/.next-prev"',
    )
    (env.bin / "curl").write_text(curl)
    r = env.run(FAKE_BAD_BUILD_ID=NEW_ID)
    assert r.returncode == 5, _out(r)
    assert env.live_id() == NEW_ID
    assert "整包重部署" in r.stderr and "§9.3" in r.stderr


def test_h2_next_step_generic_when_health_fails_after_rollback(env: Env) -> None:
    """下一步分支「其餘」：檔案已回舊版、重啟成功，但回退後健康檢查仍失敗 → 明講目前 BUILD_ID 與檢查項目。"""
    r = env.run(FAKE_ASSET_STATUS="400", HEALTH_TIMEOUT_S="1")
    assert r.returncode == 5, _out(r)
    assert f"目前線上 .next 的 BUILD_ID={OLD_ID}" in r.stderr and "journalctl" in r.stderr


def test_h2_rollback_requires_prev_static_non_empty(env: Env) -> None:
    """--rollback 前置檢查：.next-prev/static 必須存在且非空（與步驟 4 同一判準）。"""
    prev = env.web / ".next-prev"
    (prev / "static").mkdir(parents=True)  # 空的 static
    (prev / "BUILD_ID").write_text("PREVBUILD")
    before = _tree_digest(env.web)
    r = env.run("--rollback")
    assert r.returncode == 1, _out(r)
    assert _tree_digest(env.web) == before
    assert env.restarts() == 0
    assert "沒有可回退的版本" in r.stderr

def _default_of(var: str) -> str:
    """從腳本文字解析 `VAR="${VAR:-預設}"` 的預設值。"""
    m = re.search(rf'^{var}="\$\{{{var}:-([^}}]*)\}}"', SCRIPT.read_text(), re.MULTILINE)
    assert m, f"找不到 {var} 的預設值宣告"
    return m.group(1)


def test_default_build_dir_is_outside_deploy_tree() -> None:
    """RUNBOOK §3.2 不變量：/opt/filet/spark 樹內（var/ 除外）無非 root 檔案。

    BUILD_DIR 是 ubuntu 所有的工作副本，預設值不得落在預設 WEB_DIR 的上層部署樹
    /opt/filet/spark/ 之內，否則每次前端部署都會打破該不變量。
    """
    web_dir = _default_of("WEB_DIR")
    build_dir = _default_of("BUILD_DIR")
    assert web_dir == "/opt/filet/spark/web"
    assert build_dir == "/opt/filet/web-build"
    deploy_tree = "/opt/filet/spark"
    assert not (build_dir + "/").startswith(deploy_tree + "/")
    assert (web_dir + "/").startswith(deploy_tree + "/")  # 對照：WEB_DIR 才在樹內
    # 同一個 /opt/filet 底下（同磁碟前提：正式機 /opt/filet 與 web 同 device，2026-11-17 唯讀確認）
    assert os.path.dirname(build_dir) == os.path.dirname(deploy_tree)


def test_script_has_no_secret_access() -> None:
    """紅線 2：腳本不讀、不印任何 .env*。"""
    text = SCRIPT.read_text()
    assert ".env" not in text
