"""Senate LDA lobbying disclosures (addendum §10.1) — lda.senate.gov REST API.

Free, keyless, JSON, page-paginated: /api/v1/filings/?filing_year=Y&page_size=25
&page=N. Each filing's registrant lands in the influence ledger (org_type
lobbying_firm, sector left 'uncategorized' — ensure_org never overwrites a
curated sector) and one lobbying_disclosures row records period, client, the
general_issue_code list, and the reported income-or-expenses amount. The page
cursor lives in app_meta ('lda_page'); a few budget-checked pages per tick walk
the year, then wrap. Daily ceiling: ingestion.budgets.lda.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from core import db
from domain import influence
from ingestion import budget
from ingestion.http import get_json
from ingestion.scheduler import register

LDA_URL = "https://lda.senate.gov/api/v1/filings/"
PAGE_SIZE = 25
PAGES_PER_TICK = 3


def ensure_source() -> None:
    """Idempotent sources row (this adapter is not in sources_seed.py — new
    adapters own their row). interval_key reuses 'census': daily-ish cadence."""
    db.execute(
        "INSERT OR IGNORE INTO sources(name,source_type,interval_key,url,api_key_env,"
        "reliability_tier,is_active,config_json) VALUES(?,?,?,?,?,?,?,?)",
        ("Senate LDA lobbying", "lda_lobbying", "census", LDA_URL, "", 1, 1, "{}"))


def map_filing(filing: dict) -> dict | None:
    """Pure mapping: one LDA filing dict (documented API shape: nested
    registrant/client objects, filing_year + filing_period, income/expenses as
    decimal strings, lobbying_activities[].general_issue_code) → the
    lobbying_disclosures row fields. None when there is no registrant name."""
    registrant = ((filing.get("registrant") or {}).get("name") or "").strip()
    if not registrant:
        return None
    period = f"{filing.get('filing_year')}-{filing.get('filing_period') or 'unknown'}"
    client = ((filing.get("client") or {}).get("name") or "").strip() or None
    issue_codes = [a.get("general_issue_code")
                   for a in (filing.get("lobbying_activities") or [])
                   if a.get("general_issue_code")]
    amount = filing.get("income")
    if amount in (None, ""):
        amount = filing.get("expenses")
    try:
        amount = float(amount) if amount not in (None, "") else None
    except (TypeError, ValueError):
        amount = None
    return {
        "registrant": registrant,
        "period": period,
        "client": client,
        "issue_codes": json.dumps(issue_codes),
        "amount": amount,
        "source_url": filing.get("filing_document_url") or filing.get("url"),
    }


def land_filing(filing: dict) -> int | None:
    """map_filing → influence ledger org + lobbying_disclosures row. Idempotent:
    the table's UNIQUE(org_id,period,client,amount) + INSERT OR IGNORE."""
    row = map_filing(filing)
    if row is None:
        return None
    org_id = influence.ensure_org(row["registrant"], "uncategorized", "lobbying_firm")
    return db.execute(
        "INSERT OR IGNORE INTO lobbying_disclosures(org_id,period,client,issue_codes,amount,"
        "source,source_url) VALUES(?,?,?,?,?,'senate_lda',?)",
        (org_id, row["period"], row["client"], row["issue_codes"], row["amount"],
         row["source_url"]))


@register("lda_lobbying")
def run(source: dict) -> None:
    base = source["url"] or LDA_URL
    conf = json.loads(source["config_json"] or "{}")
    year = int(conf.get("filing_year") or datetime.now(timezone.utc).year)
    page = int(db.meta_get("lda_page", "1"))
    for _ in range(PAGES_PER_TICK):
        budget.spend("lda")  # BudgetExhausted propagates — the scheduler treats it as ok
        data = get_json(base, {"filing_year": year, "page_size": PAGE_SIZE, "page": page})
        for filing in data.get("results") or []:
            land_filing(filing)
        page = page + 1 if data.get("next") else 1
        db.meta_set("lda_page", str(page))  # cursor persists per page, resumable mid-batch
        if page == 1:
            return  # year exhausted; next tick restarts from the front (new filings first)


try:  # at boot migrate() runs before adapters are imported, so this lands the row;
    ensure_source()  # a bare unit-test import without a migrated DB skips quietly
except Exception:
    pass
