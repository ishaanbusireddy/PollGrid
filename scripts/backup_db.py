#!/usr/bin/env python3
"""Online SQLite backup (Phase Z ops hardening). Uses sqlite3's backup API, so
it is safe while the platform is running; keeps the last N timestamped copies.

Usage: python scripts/backup_db.py [--dest data/backups] [--keep 14]
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import DB_PATH  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default=os.path.join(os.path.dirname(DB_PATH), "backups"))
    ap.add_argument("--keep", type=int, default=14)
    args = ap.parse_args()
    if not os.path.exists(DB_PATH):
        sys.exit(f"no database at {DB_PATH}")
    os.makedirs(args.dest, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = os.path.join(args.dest, f"pollgrid-{stamp}.db")
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(out)
    with dst:
        src.backup(dst)
    src.close(); dst.close()
    print(f"backed up → {out} ({os.path.getsize(out) / 1e6:.1f} MB)")
    backups = sorted(f for f in os.listdir(args.dest) if f.startswith("pollgrid-") and f.endswith(".db"))
    for stale in backups[:-args.keep]:
        os.remove(os.path.join(args.dest, stale))
        print(f"pruned {stale}")


if __name__ == "__main__":
    main()
