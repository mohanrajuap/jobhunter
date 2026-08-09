"""Source registry: turns the `sources:` config block into live Source objects."""

from __future__ import annotations

import logging
from typing import Any

from ..config import Config
from .ashby import AshbySource
from .base import BoardSource, Source, make_session
from .career_page import CareerPageSource, sniff_ats
from .greenhouse import GreenhouseSource
from .lever import LeverSource
from .naukri import NaukriSource
from .recruitee import RecruiteeSource
from .smartrecruiters import SmartRecruitersSource
from .workable import WorkableSource

log = logging.getLogger(__name__)

BOARD_SOURCES: dict[str, type[BoardSource]] = {
    "greenhouse": GreenhouseSource,
    "lever": LeverSource,
    "ashby": AshbySource,
    "smartrecruiters": SmartRecruitersSource,
    "workable": WorkableSource,
    "recruitee": RecruiteeSource,
}

__all__ = [
    "Source", "BoardSource", "BOARD_SOURCES", "build_sources", "sniff_ats",
    "GreenhouseSource", "LeverSource", "AshbySource", "SmartRecruitersSource",
    "WorkableSource", "RecruiteeSource", "NaukriSource", "CareerPageSource",
]


def build_sources(config: Config, browser: Any = None) -> list[Source]:
    """Instantiate every enabled source. Naukri needs a browser; the rest use HTTP."""
    session = make_session(timeout=int(config.get("http.timeout_seconds", 20)))
    sources: list[Source] = []

    for name, source_cls in BOARD_SOURCES.items():
        section = config.section(f"sources.{name}")
        if not section.get("enabled", False):
            continue
        if not (section.get("companies") or section.get("boards")):
            log.warning("sources.%s is enabled but lists no companies — skipping", name)
            continue
        sources.append(source_cls(section, session=session))

    naukri_cfg = config.section("sources.naukri")
    if naukri_cfg.get("enabled", False):
        if browser is None:
            log.warning("sources.naukri is enabled but no browser session was provided — skipping")
        else:
            sources.append(NaukriSource(naukri_cfg, browser=browser))

    pages = config.get("sources.custom_career_pages", []) or []
    if pages:
        page_cfg = dict(config.section("sources.career_page_options"))
        page_cfg["pages"] = pages
        sources.append(CareerPageSource(page_cfg, session=session))

    log.info("Enabled sources: %s", ", ".join(s.name for s in sources) or "none")
    return sources
