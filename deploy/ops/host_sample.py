#!/usr/bin/env python3
"""Host resource sampler for the filet prod box (read-only, no sudo, stdlib only).

Installed at /home/ubuntu/explore-obs/host_sample.py and run from the `ubuntu`
crontab every 15 minutes (:02/:17/:32/:47). Appends one JSON line per run to
/home/ubuntu/explore-obs/host.jsonl. Independent of sample.py (the explore
sampler) and of every filet service.

v2 (2026-10-04, RUNBOOK §5.8h／§5.8k): added explore.db／WAL size, PSI io,
nginx 499／5xx counts over the last 15 minutes, and the reboot-required flag,
so that the IO-saturation regime that hit 2026-09-28..10-04 (DB > page cache →
filet-api re-reading the DB from disk every publisher tick, users seeing 499s)
leaves a curve instead of being discovered by hand.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections import deque
from datetime import datetime, timedelta, timezone

OBS = "/home/ubuntu/explore-obs"
OUT = f"{OBS}/host.jsonl"
STATE = f"{OBS}/host_state.json"
EXPLORE_DB = "/var/lib/filet-api/explore.db"
NGINX_ACCESS = "/var/log/nginx/access.log"
NGINX_WINDOW_S = 15 * 60
NGINX_TAIL_LINES = 5000

_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
# nginx combined log: ... [04/Oct/2026:08:27:22 +0000] "GET / HTTP/2.0" 499 0 ...
_NGINX_RE = re.compile(r"\[(\d{2})/(\w{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2}) ([+-]\d{4})\] \"[^\"]*\" (\d{3}) ")


def meminfo() -> dict[str, int]:
    d: dict[str, int] = {}
    with open("/proc/meminfo") as fh:
        for line in fh:
            k, v = line.split(":", 1)
            d[k] = int(v.split()[0]) // 1024  # MB
    return d


def psi(kind: str) -> dict[str, float]:
    out: dict[str, float] = {}
    try:
        with open(f"/proc/pressure/{kind}") as fh:
            for line in fh:
                parts = line.split()
                tag = parts[0]
                vals = dict(p.split("=") for p in parts[1:])
                out[f"{tag}_avg60"] = float(vals["avg60"])
                out[f"{tag}_avg300"] = float(vals["avg300"])
    except OSError:
        pass
    return out


def cpu_jiffies() -> tuple[int, int, int]:
    with open("/proc/stat") as fh:
        f = fh.readline().split()
    vals = list(map(int, f[1:]))
    idle = vals[3] + vals[4]  # idle + iowait
    steal = vals[7] if len(vals) > 7 else 0
    return sum(vals), idle, steal


def systemctl_units(pattern: str) -> list[str]:
    r = subprocess.run(
        ["systemctl", "list-units", "--type=service", "--state=active",
         "--no-legend", "--plain", pattern],
        capture_output=True, text=True, timeout=10,
    )
    return [ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()]


def unit_props(unit: str) -> dict[str, object]:
    r = subprocess.run(
        ["systemctl", "show", unit, "-p", "MemoryCurrent", "-p", "CPUUsageNSec",
         "-p", "ActiveState", "-p", "NRestarts"],
        capture_output=True, text=True, timeout=10,
    )
    props = dict(ln.split("=", 1) for ln in r.stdout.splitlines() if "=" in ln)
    mem = props.get("MemoryCurrent", "")
    cpu = props.get("CPUUsageNSec", "")
    return {
        "mem_mb": (int(mem) // (1024 * 1024)) if mem.isdigit() else None,
        "cpu_s": (int(cpu) // 1_000_000_000) if cpu.isdigit() else None,
        "state": props.get("ActiveState"),
        "restarts": int(props.get("NRestarts", "0") or 0),
    }


def top_rss(n: int = 5) -> list[dict[str, object]]:
    r = subprocess.run(
        ["ps", "-eo", "pid,rss,comm", "--sort=-rss"],
        capture_output=True, text=True, timeout=10,
    )
    rows = []
    for ln in r.stdout.splitlines()[1 : n + 1]:
        pid, rss, comm = ln.split(None, 2)
        rows.append({"pid": int(pid), "rss_mb": int(rss) // 1024, "comm": comm})
    return rows


def file_mb(path: str) -> float | None:
    try:
        return round(os.path.getsize(path) / 1048576, 1)
    except OSError:
        return None


def nginx_status_counts(now: float) -> dict[str, int | None]:
    """499 / 5xx / total request counts in the last NGINX_WINDOW_S seconds, from
    the tail of the access log. None when the log is unreadable (the sampler
    must still emit a line — a missing field is a visible gap, a crash is not)."""
    try:
        with open(NGINX_ACCESS, errors="replace") as fh:
            tail = deque(fh, maxlen=NGINX_TAIL_LINES)
    except OSError:
        return {"nginx_499_15m": None, "nginx_5xx_15m": None, "nginx_req_15m": None}
    cutoff = now - NGINX_WINDOW_S
    c499 = c5xx = total = 0
    for ln in tail:
        m = _NGINX_RE.search(ln)
        if not m:
            continue
        dd, mon, yyyy, hh, mi, ss, tz, status = m.groups()
        sign = 1 if tz[0] == "+" else -1
        off = timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5])) * sign
        try:
            ts = datetime(int(yyyy), _MONTHS[mon], int(dd), int(hh), int(mi), int(ss),
                          tzinfo=timezone(off)).timestamp()
        except (KeyError, ValueError):
            continue
        if ts < cutoff:
            continue
        total += 1
        if status == "499":
            c499 += 1
        elif status.startswith("5"):
            c5xx += 1
    return {"nginx_499_15m": c499, "nginx_5xx_15m": c5xx, "nginx_req_15m": total}


def main() -> None:
    now = time.time()
    total, idle, steal = cpu_jiffies()
    prev = None
    try:
        with open(STATE) as fh:
            prev = json.load(fh)
    except (OSError, ValueError):
        prev = None
    cpu_pct = steal_pct = None
    if prev and total > prev["total"]:
        dt = total - prev["total"]
        cpu_pct = round(100.0 * (1 - (idle - prev["idle"]) / dt), 1)
        steal_pct = round(100.0 * (steal - prev["steal"]) / dt, 2)
    with open(STATE, "w") as fh:
        json.dump({"total": total, "idle": idle, "steal": steal, "t": now}, fh)

    m = meminfo()
    load1, load5, load15 = os.getloadavg()
    followers = systemctl_units("filet-follower@*.service")
    services = {"filet-api": unit_props("filet-api.service")}
    for u in followers:
        services[u.replace(".service", "")] = unit_props(u)
    du = shutil.disk_usage("/")

    rec = {
        "t": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec="seconds"),
        "load": [round(load1, 2), round(load5, 2), round(load15, 2)],
        "cpu_pct_since_prev": cpu_pct,
        "steal_pct_since_prev": steal_pct,
        "mem_mb": {
            "total": m["MemTotal"],
            "available": m["MemAvailable"],
            "used": m["MemTotal"] - m["MemAvailable"],
            "swap_used": m["SwapTotal"] - m["SwapFree"],
        },
        "psi": {"cpu": psi("cpu"), "memory": psi("memory")},
        "psi_io": psi("io"),
        "explore_db_mb": file_mb(EXPLORE_DB),
        "explore_wal_mb": file_mb(EXPLORE_DB + "-wal"),
        **nginx_status_counts(now),
        "reboot_required": os.path.exists("/var/run/reboot-required"),
        "follower_count": len(followers),
        "services": services,
        "disk_used_pct": round(100.0 * du.used / du.total, 1),
        "top_rss": top_rss(),
    }
    with open(OUT, "a") as fh:
        fh.write(json.dumps(rec, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
