"""CVAP — Citizen Voting Age Population, the Census special tabulation.

Per the manual (§05) CVAP is NOT an ACS B-table pull: it ships as its own
zip-of-CSVs product on www2.census.gov, so it gets its own ingestion path.
This adapter streams the zip (stdlib urllib), extracts County.csv with stdlib
zipfile, and lands one demographics row per county: tier county_equivalent,
category population_age, variable cvap_total, confidence measured.

The source is seeded INACTIVE by default (the zip is ~100 MB); flip it on and
point config_json.url at a newer vintage when one ships. The product is a
one-shot: after a successful import a meta flag skips re-downloading until the
flag (or the configured url's as_of tag) changes.
"""
from __future__ import annotations

import csv
import io
import json
import shutil
import tempfile
import urllib.request
import zipfile

from core import db
from core.util import now_iso
from ingestion.http import FetchError, SourceNotConfigured
from ingestion.scheduler import register

DEFAULT_CVAP_URL = ("https://www2.census.gov/programs-surveys/decennial/rdo/datasets/"
                    "2022/2022-cvap/CVAP_2018-2022_ACS_csv_files.zip")
AS_OF = "cvap_2018_2022"
SOURCE_TAG = "cvap_2018_2022"

_UA = "PollGrid/1.0 (+local research tool)"


def rows_to_demographics(rows) -> list[tuple]:
    """CVAP County.csv dict-rows → demographics tuples. Keeps only the Total
    line (lntitle 'Total' / lnnumber 1); entity_id is the 5-digit county geoid
    parsed off the tabulation geoid ('05000US01001' → '01001'). Header case and
    thousands separators vary across vintages — both are normalized here."""
    out: list[tuple] = []
    for raw in rows:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        if row.get("lntitle", "").lower() != "total" and row.get("lnnumber") not in ("1", "01"):
            continue
        geoid = row.get("geoid", "")
        if "US" in geoid.upper():
            geoid = geoid[geoid.upper().rindex("US") + 2:]
        if len(geoid) != 5 or not geoid.isdigit():
            continue  # not a county-tier geoid
        try:
            value = float(row.get("cvap_est", "").replace(",", ""))
        except ValueError:
            continue
        if value < 0:
            continue
        out.append(("county_equivalent", geoid, AS_OF, "population_age", "cvap_total",
                    value, "measured", SOURCE_TAG))
    return out


def _iter_county_rows(url: str):
    """Stream the zip to a spooled temp file (zipfile needs a seekable object),
    then yield County.csv rows as dicts."""
    tmp = tempfile.TemporaryFile()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                shutil.copyfileobj(resp, tmp, 1 << 20)
        except Exception as e:
            raise FetchError(f"{type(e).__name__}: {e}") from e
        tmp.seek(0)
        with zipfile.ZipFile(tmp) as zf:
            name = next((n for n in zf.namelist() if n.lower().endswith("county.csv")), None)
            if name is None:
                raise FetchError("County.csv not found inside the CVAP zip")
            with zf.open(name) as fh:
                text = io.TextIOWrapper(fh, encoding="latin-1", newline="")
                yield from csv.DictReader(text)
    finally:
        tmp.close()


@register("cvap")
def run(source: dict) -> None:
    conf = json.loads(source["config_json"] or "{}")
    url = conf.get("url") or source["url"] or ""
    if not url:
        raise SourceNotConfigured("no CVAP zip url configured (sources.config_json.url)")
    done_key = f"cvap_synced:{AS_OF}"
    if db.meta_get(done_key):
        return  # one-shot product already imported; clear the meta key to force a re-import
    rows = rows_to_demographics(_iter_county_rows(url))
    if rows:
        db.executemany(
            "INSERT OR IGNORE INTO demographics(tier,entity_id,as_of,category,variable,value,"
            "confidence,source) VALUES(?,?,?,?,?,?,?,?)", rows)
    db.meta_set(done_key, now_iso())
