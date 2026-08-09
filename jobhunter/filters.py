"""Runtime search filters that apply across every source.

Sources disagree about what they can filter server-side:

  * LinkedIn takes a posted-age band and a location string in the query.
  * Naukri takes `jobAge` in days and a location list.
  * ATS boards (Greenhouse, Lever, Ashby, …) and scraped career pages take nothing —
    they hand back the whole board.

So a filter is applied twice: pushed down to whichever sources understand it, to cut
the amount fetched, and then enforced centrally in the Scorer, which is what makes it
consistent no matter where a job came from. That second step is why picking "last 7
days" also works for a career page that has no such concept.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Labels shown in the UI, mapped to a day count. None means "no age filter".
POSTED_WITHIN_CHOICES: dict[str, int | None] = {
    "Any time": None,
    "Last 24 hours": 1,
    "Last 3 days": 3,
    "Last week": 7,
    "Last 2 weeks": 14,
    "Last month": 30,
}

# Sources that accept a location list, and the config key each one uses.
_LOCATION_KEYS = {
    "linkedin": "locations",
    "naukri": "locations",
}

# Sources that accept a posted-age filter, and the key each one uses.
_AGE_KEYS = {
    "linkedin": "posted_within_days",
    "naukri": "job_age_days",
}


def parse_locations(text: str) -> list[str]:
    """Split the UI's comma-separated location box into a clean list."""
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def apply_runtime_filters(
    config: Any,
    posted_within_days: int | None = None,
    locations: list[str] | None = None,
    set_age: bool = True,
    set_locations: bool = True,
) -> dict[str, Any]:
    """Write the chosen filters into the in-memory config.

    Returns a summary of what was applied, for logging. Nothing is written to disk —
    these are per-run choices.
    """
    applied: dict[str, Any] = {}

    if set_age:
        # None is a real value here: it means "clear the age filter", which is different
        # from "leave whatever the config had".
        config.set("search.posted_within_days", posted_within_days)
        applied["posted_within_days"] = posted_within_days
        for source, key in _AGE_KEYS.items():
            config.set(f"sources.{source}.{key}", posted_within_days)

    if set_locations and locations is not None:
        config.set("search.locations", locations)
        applied["locations"] = locations
        for source, key in _LOCATION_KEYS.items():
            # Only override sources that are actually configured, so we don't
            # accidentally create a half-built section for a disabled source.
            if config.section(f"sources.{source}"):
                config.set(f"sources.{source}.{key}", locations)

    if applied:
        log.info(
            "Filters — posted within: %s, locations: %s",
            f"{posted_within_days} days" if posted_within_days else "any time",
            ", ".join(locations) if locations else "any",
        )
    return applied


def describe(posted_within_days: int | None, locations: list[str] | None) -> str:
    """One-line summary for the status bar."""
    age = f"posted within {posted_within_days}d" if posted_within_days else "any date"
    where = ", ".join(locations) if locations else "any location"
    return f"{age} · {where}"
