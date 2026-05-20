"""NL → IDRE API query parameters. EST-aware date math."""
from __future__ import annotations
import re
from datetime import datetime, timedelta, timezone

ENTITY_PATTERNS = [
    r"capitol bridge", r"halo", r"veratru", r"halomd",
    r"unitedhealthcare", r"uhc", r"pacifichealth",
]


def resolve_date_phrase(phrase: str, now: datetime) -> dict:
    """Return {startDate, endDate} ISO strings for a NL date phrase."""
    p = phrase.lower().strip()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if p in ("today",):
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif p in ("yesterday",):
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif p in ("this week",):
        start = now - timedelta(days=now.weekday())
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
    elif p in ("last 7 days", "past 7 days", "previous 7 days"):
        start = now - timedelta(days=7)
        end = now
    elif p in ("month-to-date", "mtd", "this month"):
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = now
    elif p in ("last month",):
        first_of_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_month_end = first_of_this - timedelta(seconds=1)
        start = last_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = first_of_this
    else:
        return {}
    return {
        "startDate": start.isoformat().replace("+00:00", "Z"),
        "endDate": end.isoformat().replace("+00:00", "Z"),
    }


def extract_search_term(query: str) -> str | None:
    q = query.lower()
    for pat in ENTITY_PATTERNS:
        m = re.search(pat, q, re.IGNORECASE)
        if m:
            return m.group(0)
    return None
