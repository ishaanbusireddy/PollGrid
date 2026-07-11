"""The gazetteer (§03/§06): dateline-first geocoding, a curated notable-places
override for newsworthy-but-small locales, state/county name resolution, and
the backend point-in-polygon twin over the vendored boundary files — zero GIS
dependency at runtime, ever. Geometry loads lazily and is cached in-process."""
from __future__ import annotations

import json
import os
import re

from core.config import ROOT

_DATA = os.path.join(ROOT, "frontend", "static", "data")

# Curated notable places: newsworthy locales whose names alone identify a
# county — checked before any generic matching. (city_lower -> (state_fips, county_geoid))
NOTABLE_PLACES: dict[str, tuple[str, str]] = {
    "atlanta": ("13", "13121"), "phoenix": ("04", "04013"), "philadelphia": ("42", "42101"),
    "pittsburgh": ("42", "42003"), "detroit": ("26", "26163"), "milwaukee": ("55", "55079"),
    "las vegas": ("32", "32003"), "reno": ("32", "32031"), "charlotte": ("37", "37119"),
    "raleigh": ("37", "37183"), "madison": ("55", "55025"), "tucson": ("04", "04019"),
    "savannah": ("13", "13051"), "grand rapids": ("26", "26081"), "erie": ("42", "42049"),
    "portland, maine": ("23", "23005"), "manchester": ("33", "33011"), "columbus": ("39", "39049"),
    "cleveland": ("39", "39035"), "cincinnati": ("39", "39061"), "miami": ("12", "12086"),
    "tampa": ("12", "12057"), "houston": ("48", "48201"), "dallas": ("48", "48113"),
    "austin": ("48", "48453"), "chicago": ("17", "17031"), "new york": ("36", "36061"),
    "los angeles": ("06", "06037"), "san francisco": ("06", "06075"), "seattle": ("53", "53033"),
    "denver": ("08", "08031"), "boston": ("25", "25025"), "baltimore": ("24", "24510"),
    "washington": ("11", "11001"), "minneapolis": ("27", "27053"), "omaha": ("31", "31055"),
    "kenosha": ("55", "55059"), "flint": ("26", "26049"), "scranton": ("42", "42069"),
}

# "ATLANTA — ..." / "PHOENIX, Ariz. — ..." wire-style datelines
_DATELINE = re.compile(r"^([A-Z][A-Z .'\-]{2,28}?)(?:,\s*[A-Za-z.]{2,14})?\s*[—–-]{1,2}\s")

_geometry_cache: dict[str, list] = {}


def _load_features(name: str) -> list:
    if name not in _geometry_cache:
        path = os.path.join(_DATA, name)
        try:
            with open(path, encoding="utf-8") as fh:
                _geometry_cache[name] = json.load(fh)["features"]
        except (OSError, ValueError, KeyError):
            _geometry_cache[name] = []
    return _geometry_cache[name]


def _point_in_ring(lon: float, lat: float, ring: list) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _point_in_geometry(lon: float, lat: float, geom: dict) -> bool:
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    for poly in polys:
        if _point_in_ring(lon, lat, poly[0]) and not any(_point_in_ring(lon, lat, h) for h in poly[1:]):
            return True
    return False


def reverse_geocode(lat: float, lon: float) -> dict:
    """lat/lon → {state_fips, county_geoid, district_geoid} via the vendored
    boundary files — the self-contained backend twin of the frontend map."""
    out: dict = {"state_fips": None, "county_geoid": None, "district_geoid": None}
    for f in _load_features("us_counties.json"):
        if _point_in_geometry(lon, lat, f["geometry"]):
            out["county_geoid"] = f.get("id")
            out["state_fips"] = (f.get("id") or "")[:2] or None
            break
    if out["state_fips"] is None:
        for f in _load_features("us_states.json"):
            if _point_in_geometry(lon, lat, f["geometry"]):
                out["state_fips"] = f.get("id")
                break
    for f in _load_features("us_districts.json"):
        if f["properties"].get("state_fips") == out["state_fips"] \
                and _point_in_geometry(lon, lat, f["geometry"]):
            out["district_geoid"] = f.get("id")
            break
    return out


def centroid(kind: str, key: str) -> tuple[float, float] | None:
    """(lat, lon) centroid of a state fips / county geoid — for map pins."""
    fname = {"state": "us_states.json", "county": "us_counties.json"}.get(kind)
    if not fname:
        return None
    for f in _load_features(fname):
        if f.get("id") == key:
            xs, ys, n = 0.0, 0.0, 0
            geom = f["geometry"]
            polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
            ring = max((p[0] for p in polys), key=len)
            for x, y in ring:
                xs += x; ys += y; n += 1
            return (ys / n, xs / n) if n else None
    return None


def geocode_text(text: str) -> tuple[str | None, str | None]:
    """Dateline FIRST, then the notable-places override, then state names/codes,
    then 'X County'-style mentions scoped to the found state.
    → (state_fips, county_geoid)."""
    from core import db
    m = _DATELINE.match(text.strip())
    if m:
        place = m.group(1).strip().lower()
        if place in NOTABLE_PLACES:
            return NOTABLE_PLACES[place]
        row = db.query_one("SELECT geoid, state_fips FROM county_equivalents "
                           "WHERE effective_to IS NULL AND lower(name) LIKE ?", (place + "%",))
        if row:
            return row["state_fips"], row["geoid"]
    low = text.lower()
    for place, (sf, geoid) in NOTABLE_PLACES.items():
        if re.search(rf"\b{re.escape(place)}\b", low):
            return sf, geoid
    from domain.geography import STATES
    state_fips = None
    for fips, (usps, name, _) in STATES.items():
        if re.search(rf"\b{re.escape(name)}\b", text) or re.search(rf"\b{usps}-\d", text):
            state_fips = fips
            break
    county_geoid = None
    m = re.search(r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\s+(County|Parish|Borough)\b", text)
    if m and state_fips:
        row = db.query_one("SELECT geoid FROM county_equivalents WHERE state_fips=? AND name LIKE ?",
                           (state_fips, m.group(1) + "%"))
        county_geoid = row["geoid"] if row else None
    return state_fips, county_geoid
