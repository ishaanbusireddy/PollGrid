"""Social signal: volume/sentiment only, never a source of fact. No free API
exists; without a configured provider this source is honestly degraded forever."""
from __future__ import annotations

import os

from ingestion.http import SourceNotConfigured
from ingestion.scheduler import register


@register("social")
def run(source: dict) -> None:
    if not os.environ.get(source["api_key_env"] or ""):
        raise SourceNotConfigured("no social-signal provider configured (SOCIAL_API_KEY)")
    raise SourceNotConfigured("social provider integration not yet implemented; source stays degraded")
