"""Derived congressional-district demographics via areal apportionment of REAL
county Census counts onto the current district lines. For an EXTENSIVE (count)
variable — total population, bachelor's-degree holders, owner-occupied units,
etc. — a district's count is the sum over the counties it overlaps of
(county count x share of the county's area inside the district). This is the
standard areal-apportionment method (uniform-density assumption within a county,
the honest default without sub-county microdata).

RATE variables (median age, median household income) are deliberately SKIPPED:
a median cannot be honestly recombined from county medians without the underlying
distribution, and inventing one would violate the no-fabrication doctrine. The
frontend's share modes (bachelor's share, homeownership share, etc.) are
recomputed from the apportioned COUNTS, so those still work at district tier.

Every output row is confidence='derived', source='derived:areal_apportionment',
entity_id=str(district_version_id). Reuses modeling/areal.py (shared, cached)."""
from __future__ import annotations

from core import db
from core.util import today
from modeling.areal import AreaShareResolver, district_version_id

SOURCE = "derived:areal_apportionment"
# rates / non-extensive variables that cannot be areally summed — skipped honestly
RATE_VARIABLES = {"median_age", "median_household_income"}


def derive_all(as_of: str | None = None) -> int:
    """Apportion every real, extensive county demographic count to the current
    district lines. Returns the number of district demographic rows written.
    Honest no-op where no real county demographics exist."""
    _ = as_of or today()
    # latest REAL value per (county, category, variable) — real beats synthetic,
    # newest vintage wins (the ORDER BY mirrors every other demographics read)
    rows = db.query(
        "SELECT entity_id geoid, category, variable, value, as_of, is_synthetic "
        "FROM demographics WHERE tier='county_equivalent' AND value IS NOT NULL "
        "ORDER BY is_synthetic, as_of DESC")
    latest: dict[tuple[str, str, str], dict] = {}
    for r in rows:
        if r["variable"] in RATE_VARIABLES:
            continue
        key = (r["geoid"], r["category"], r["variable"])
        latest.setdefault(key, r)   # first seen wins = real, newest
    if not latest:
        return 0

    resolver = AreaShareResolver()
    # (dvid, category, variable) -> [summed_count, max_as_of]
    acc: dict[tuple[int, str, str], list] = {}
    for (geoid, category, variable), r in latest.items():
        if r["is_synthetic"]:
            continue   # derive only from real county data
        shares = resolver.shares_for(geoid)
        if not shares:
            continue
        for dgeoid, share in shares.items():
            dvid = district_version_id(dgeoid)
            if dvid is None:
                continue
            a = acc.setdefault((dvid, category, variable), [0.0, r["as_of"]])
            a[0] += (r["value"] or 0.0) * share
            if r["as_of"] > a[1]:
                a[1] = r["as_of"]

    written = 0
    for (dvid, category, variable), (total, vintage) in acc.items():
        db.execute(
            "INSERT INTO demographics(tier,entity_id,as_of,category,variable,value,confidence,source,is_synthetic) "
            "VALUES('congressional_district',?,?,?,?,?,'derived',?,0) "
            "ON CONFLICT(tier,entity_id,as_of,category,variable) DO UPDATE SET "
            "value=excluded.value, confidence='derived', source=excluded.source, is_synthetic=0 "
            "WHERE demographics.confidence!='measured'",
            (str(dvid), vintage, category, variable, round(total, 2), SOURCE))
        written += 1
    return written
