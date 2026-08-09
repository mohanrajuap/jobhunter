"""Source registry: turns the `sources:` config block into live Source objects."""

from __future__ import annotations

import logging
from typing import Any

from ..config import Config
from .ashby import AshbySource
from .base import BoardSource, Source, make_session
from .career_page import CareerPageSource, sniff_ats
from .greenhouse import GreenhouseSource
from .kula import KulaSource
from .lever import LeverSource
from .linkedin import LinkedInSource
from .naukri import NaukriSource
from .recruitee import RecruiteeSource
from .smartrecruiters import SmartRecruitersSource
from .workday import WorkdaySource
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
    "Source", "BoardSource", "BOARD_SOURCES", "ALL_SOURCE_NAMES", "build_sources", "sniff_ats",
    "GreenhouseSource", "LeverSource", "AshbySource", "SmartRecruitersSource",
    "WorkableSource", "RecruiteeSource", "NaukriSource", "LinkedInSource", "CareerPageSource",
    "WorkdaySource", "KulaSource",
]

# Every source the UI can offer, in the order it shows them.
ALL_SOURCE_NAMES = [
    "naukri", "linkedin", "greenhouse", "lever", "ashby",
    "smartrecruiters", "workable", "recruitee", "workday", "kula", "career_pages",
]


def build_sources(
    config: Config,
    browser: Any = None,
    only: list[str] | None = None,
    should_cancel: Any = None,
    on_jobs: Any = None,
) -> list[Source]:
    """Instantiate every enabled source.

    `only` restricts the run to a subset by name — that's how the UI's source picker
    works, without having to rewrite the config file. `should_cancel` and `on_jobs` are
    attached to each instance so Stop interrupts a search and results can stream to the
    UI as they arrive.
    """
    session = make_session(timeout=int(config.get("http.timeout_seconds", 20)))
    selected = {s.lower() for s in only} if only else None
    sources: list[Source] = []

    def wanted(name: str) -> bool:
        return selected is None or name in selected

    for name, source_cls in BOARD_SOURCES.items():
        section = config.section(f"sources.{name}")
        if not section.get("enabled", False) or not wanted(name):
            continue
        if not (section.get("companies") or section.get("boards")):
            log.warning("sources.%s is enabled but lists no companies — skipping", name)
            continue
        sources.append(source_cls(section, session=session))

    naukri_cfg = config.section("sources.naukri")
    if naukri_cfg.get("enabled", False) and wanted("naukri"):
        if browser is None:
            log.warning("sources.naukri is enabled but no browser session was provided — skipping")
        else:
            sources.append(NaukriSource(naukri_cfg, browser=browser))

    workday_cfg = config.section("sources.workday")
    if workday_cfg.get("enabled", False) and wanted("workday"):
        if workday_cfg.get("companies"):
            sources.append(WorkdaySource(workday_cfg, session=session))
        else:
            log.warning("sources.workday is enabled but lists no companies — skipping")

    kula_cfg = config.section("sources.kula")
    if kula_cfg.get("enabled", False) and wanted("kula"):
        if kula_cfg.get("companies"):
            sources.append(KulaSource(kula_cfg, session=session))
        else:
            log.warning("sources.kula is enabled but lists no companies — skipping")

    linkedin_cfg = config.section("sources.linkedin")
    if linkedin_cfg.get("enabled", False) and wanted("linkedin"):
        sources.append(LinkedInSource(linkedin_cfg, session=session))

    pages = config.get("sources.custom_career_pages", []) or []
    if wanted("career_pages"):
        if pages:
            page_cfg = dict(config.section("sources.career_page_options"))
            page_cfg["pages"] = pages
            # The browser lets it read career pages that build their job list in JS.
            sources.append(CareerPageSource(page_cfg, session=session, browser=browser))
        elif only and "career_pages" in {s.lower() for s in only}:
            # Explicitly asked for, but nothing to search — say so instead of silently
            # returning nothing.
            log.warning(
                "Company career pages are selected but none are configured. "
                "Add them on the Career Pages tab (or sources.custom_career_pages)."
            )

    for source in sources:
        if should_cancel is not None:
            source.should_cancel = should_cancel
        if on_jobs is not None:
            source.on_jobs = on_jobs

    log.info("Enabled sources: %s", ", ".join(s.name for s in sources) or "none")
    return sources
