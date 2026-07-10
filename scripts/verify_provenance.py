#!/usr/bin/env python3
"""Walk every hash chain; exit non-zero on any break. Run it in CI."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db, provenance  # noqa: E402

if __name__ == "__main__":
    db.migrate()
    failed = False
    for table, ok, detail in provenance.verify_all():
        print(("OK " if ok else "FAIL ") + detail)
        if not ok:
            failed = True
    sys.exit(1 if failed else 0)
