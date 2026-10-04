#!/usr/bin/env python3
"""filet 主機／服務門檻 Telegram 告警（純標準庫，正式機以系統 python3 執行）。

讀取 explore-obs 取樣器寫的 host.jsonl 與 systemctl 狀態，套門檻後去重送 TG。
token／chat id 只從 filet-api 的 unit Environment 解析到局部變數，不落檔、不印。
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.request

HOST_JSONL = "/home/ubuntu/explore-obs/host.jsonl"
STATE_PATH = "/home/ubuntu/explore-obs/alert_state.json"
FIXED_UNITS = ("filet-api", "filet-dashboard", "filet-keysvc", "nginx")
STALE_S = 1200
REMIND_S = 6 * 3600


def _num(d: object, *path: str) -> float | None:
    cur: object = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return float(cur) if isinstance(cur, (int, float)) else None


SAMPLE_KEYS = ("low_memory", "io_pressure", "client_timeouts", "db_size")


def _conditions(
    sample: dict | None, age: float, units: dict[str, str], failed: int
) -> tuple[dict[str, str], set[str]]:
    """回傳 (成立的條件, 無法判定的條件鍵)。無法判定（樣本缺、欄位缺）的鍵
    不算「已恢復」——reviewer W3（2026-10-04）：取樣器掛掉時原本成立的 IO 告警
    會被誤判 recovered。"""
    c: dict[str, str] = {}
    unknown: set[str] = set()
    if sample is None or age > STALE_S:
        c["sampler_stale"] = f"取樣器停擺：最新樣本 age={age:.0f}s（>{STALE_S}s）或無樣本"
    if sample is None:
        unknown.update(SAMPLE_KEYS)
    else:
        v = _num(sample, "mem_mb", "available")
        if v is None:
            unknown.add("low_memory")
        elif v < 400:
            c["low_memory"] = f"可用記憶體 {v:.0f} MB（<400）"
        v = _num(sample, "psi_io", "some_avg300")
        if v is None:
            unknown.add("io_pressure")
        elif v > 50:
            c["io_pressure"] = f"IO PSI some_avg300={v:.1f}（>50）"
        v = _num(sample, "nginx_499_15m")
        if v is None:
            unknown.add("client_timeouts")
        elif v > 10:
            c["client_timeouts"] = f"nginx 499 近 15 分 {v:.0f} 次（>10）"
        v = _num(sample, "explore_db_mb")
        if v is None:
            unknown.add("db_size")
        elif v > 2500:
            c["db_size"] = f"explore DB {v:.0f} MB（>2500）"
    for name, st in units.items():
        if st != "active":
            c[f"unit_down:{name}"] = f"unit {name} 狀態 {st}"
    if failed > 0:
        c["failed_units"] = f"systemd failed units 共 {failed} 個"
    return c, unknown


def evaluate(
    sample: dict | None,
    sample_age_s: float,
    units: dict[str, str],
    failed_units: int,
    state: dict,
    now: float,
) -> tuple[list[str], dict]:
    conds, unknown = _conditions(sample, sample_age_s, units, failed_units)
    new_state: dict = {k: dict(v) for k, v in state.items()}
    msgs: list[str] = []
    for key, text in conds.items():
        ent = new_state.get(key)
        if ent is None:
            new_state[key] = {"since": now, "last_sent": now}
            msgs.append(f"[filet 告警] {text}")
        elif now - ent["last_sent"] >= REMIND_S:
            ent["last_sent"] = now
            hrs = (now - ent["since"]) / 3600
            msgs.append(f"[filet 仍在] {text}（已持續 {hrs:.1f} 小時）")
    for key in list(new_state):
        if key not in conds and key not in unknown:
            del new_state[key]
            msgs.append(f"[filet recovered] {key} 已恢復")
    return msgs, new_state


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _read_sample(path: str, now: float) -> tuple[dict | None, float]:
    try:
        with open(path, "rb") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        if not lines:
            return None, float("inf")
        return json.loads(lines[-1]), now - os.path.getmtime(path)
    except (OSError, ValueError):
        return None, float("inf")


def _collect_units() -> tuple[dict[str, str], int]:
    names = list(FIXED_UNITS)
    # 不帶 `--all`：只看目前 loaded／active 的 follower 實例。owner_close 後引擎
    # 會 exit 0 停下（刻意 inactive），列進來會每 6 小時誤報 unit_down；真正的
    # 失敗（非 0 退出、Restart 耗盡）由下面的 `--failed` 計數抓。
    for ln in _run(
        ["systemctl", "list-units", "filet-follower@*", "--no-legend", "--plain"]
    ).splitlines():
        parts = ln.split()
        if parts and parts[0].startswith("filet-follower@"):
            names.append(parts[0].removesuffix(".service"))
    units = {}
    for n in names:
        out = _run(["systemctl", "is-active", n]).strip()
        units[n] = out or "unknown"
    failed = len([ln for ln in _run(["systemctl", "--failed", "--no-legend", "--plain"]).splitlines() if ln.strip()])
    return units, failed


def send_telegram(text: str) -> None:
    env_line = _run(["systemctl", "show", "filet-api", "-p", "Environment", "--value"])
    token = chat = ""
    for item in shlex.split(env_line):
        if item.startswith("FILET_API_TG_BOT_TOKEN="):
            token = item.split("=", 1)[1]
        elif item.startswith("FILET_API_TG_CHAT_ID="):
            chat = item.split("=", 1)[1]
    if not token or not chat:
        raise RuntimeError("telegram send failed: credentials missing")
    body = json.dumps({"chat_id": chat, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"telegram send failed: HTTP {e.code}") from None
    except Exception as e:  # noqa: BLE001 - 重新包成固定字串，避免 URL（含 token）外洩
        raise RuntimeError(f"telegram send failed: {type(e).__name__}") from None
    if status != 200:
        raise RuntimeError(f"telegram send failed: HTTP {status}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--state", default=STATE_PATH)
    ap.add_argument("--host-jsonl", default=HOST_JSONL)
    args = ap.parse_args()

    if args.test:
        try:
            send_telegram("filet 告警通道測試")
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 1
        return 0

    now = time.time()
    sample, age = _read_sample(args.host_jsonl, now)
    if sample is None:
        print("no sample")
    units, failed = _collect_units()
    try:
        with open(args.state) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    msgs, new_state = evaluate(sample, age, units, failed, state, now)
    if args.dry_run:
        for m in msgs:
            print(m)
        return 0
    rc = 0
    if msgs:
        try:
            send_telegram("\n".join(msgs))
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 1
    tmp = args.state + ".tmp"
    with open(tmp, "w") as f:
        json.dump(new_state, f)
    os.replace(tmp, args.state)
    return rc


if __name__ == "__main__":
    sys.exit(main())
