#!/usr/bin/env python3
"""Offline maintenance for explore.db: unbounded purge + VACUUM.

RUNBOOK §5.8k (2026-10-04). Run **only while `filet-api` is stopped** — the
store takes a single-connection lock and VACUUM needs exclusive access; the
script refuses to run when the unit is active unless `--force` is given.

    sudo -u filet-api /opt/filet/spark/.venv/bin/python \
        /opt/filet/spark/deploy/ops/explore_offline_maintenance.py \
        --db /var/lib/filet-api/explore.db --purge --vacuum

Steps (each printed with timing so the operator can follow the maintenance
window in RUNBOOK):
  --purge   `ExploreStore.purge(now)` with the module defaults (retired
            candidates kept 7 d, fills 35 d, done scans 30 d) and **no
            per-round bound** — nothing else is competing for the lock, so one
            pass is cheaper than 14 bounded rounds under load.
  --vacuum  `wal_checkpoint(TRUNCATE)` → `auto_vacuum=INCREMENTAL` → `VACUUM`
            → `integrity_check`; prints page_count before/after. Needs free
            disk ≥ current DB size (VACUUM rewrites into a temp file).
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, "/opt/filet/spark/src")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))


def _mb(path: str) -> float:
    try:
        return os.path.getsize(path) / 1048576
    except OSError:
        return 0.0


def _unit_active(unit: str) -> bool:
    try:
        r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.stdout.strip() == "active"


def _counts(db: sqlite3.Connection, now: float) -> dict[str, int]:
    cutoff = now - 7 * 86400
    return {
        "candidate_active": db.execute("SELECT COUNT(*) FROM candidate WHERE active=1").fetchone()[0],
        "candidate_inactive": db.execute("SELECT COUNT(*) FROM candidate WHERE active=0").fetchone()[0],
        "candidate_stale_no_job": db.execute(
            "SELECT COUNT(*) FROM candidate c WHERE c.active=0 AND c.last_seen_at < ? "
            "AND NOT EXISTS (SELECT 1 FROM refresh_job j WHERE j.address=c.address)", (cutoff,)).fetchone()[0],
        "fills_max_rowid": db.execute("SELECT COALESCE(MAX(rowid),0) FROM fills").fetchone()[0],
        "fills_sync": db.execute("SELECT COUNT(*) FROM fills_sync").fetchone()[0],
        "fills_scan": db.execute("SELECT COUNT(*) FROM fills_scan").fetchone()[0],
        "page_count": db.execute("PRAGMA page_count").fetchone()[0],
        "freelist_count": db.execute("PRAGMA freelist_count").fetchone()[0],
    }


def run_purge(db_path: str, now: float) -> dict[str, int]:
    from spark.publicapi.explore_store import ExploreStore  # deployed venv / repo src

    store = ExploreStore(db_path, now_fn=lambda: now)
    try:
        return store.purge(now)
    finally:
        store._db.close()  # noqa: SLF001 — one-off maintenance, store has no public close()


def run_vacuum(db_path: str) -> dict[str, object]:
    db = sqlite3.connect(db_path)
    try:
        before = db.execute("PRAGMA page_count").fetchone()[0]
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        db.isolation_level = None  # VACUUM cannot run inside a transaction
        db.execute("VACUUM")
        after = db.execute("PRAGMA page_count").fetchone()[0]
        auto = db.execute("PRAGMA auto_vacuum").fetchone()[0]
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        return {"page_count_before": before, "page_count_after": after,
                "auto_vacuum": auto, "integrity": integrity}
    finally:
        db.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--purge", action="store_true")
    ap.add_argument("--vacuum", action="store_true")
    ap.add_argument("--force", action="store_true", help="run even if filet-api is active (NOT for prod)")
    ap.add_argument("--unit", default="filet-api")
    args = ap.parse_args()

    if not (args.purge or args.vacuum):
        ap.error("nothing to do: pass --purge and/or --vacuum")
    if not os.path.exists(args.db):
        ap.error(f"no such db: {args.db}")
    if _unit_active(args.unit) and not args.force:
        print(f"refusing: {args.unit} is active — stop it first (or --force for a non-prod copy)",
              file=sys.stderr)
        return 2

    now = time.time()
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        print("before:", _counts(db, now))
    finally:
        db.close()
    print(f"file: {_mb(args.db):.0f} MB  wal: {_mb(args.db + '-wal'):.0f} MB  free disk: "
          f"{os.statvfs(os.path.dirname(args.db) or '.').f_bavail * os.statvfs(os.path.dirname(args.db) or '.').f_frsize / 1048576:.0f} MB")

    if args.purge:
        t0 = time.time()
        counts = run_purge(args.db, now)
        print(f"purge: {counts}  ({time.time() - t0:.0f}s)")

    if args.vacuum:
        t0 = time.time()
        res = run_vacuum(args.db)
        print(f"vacuum: {res}  ({time.time() - t0:.0f}s)")
        if res["integrity"] != "ok":
            print("integrity_check FAILED — do not start the service; restore from backup", file=sys.stderr)
            return 3

    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        print("after:", _counts(db, now))
    finally:
        db.close()
    print(f"file: {_mb(args.db):.0f} MB  wal: {_mb(args.db + '-wal'):.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
