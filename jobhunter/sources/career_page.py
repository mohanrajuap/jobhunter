"""Generic company career pages — the long tail beyond the big ATS boards.

Give it a careers URL and it does two things, in order:

  1. **ATS sniffing.** Most "custom" career pages are a thin wrapper over Greenhouse,
     Lever, Ashby, Workable or SmartRecruiters. If the HTML links to one, we pull the
     board slug out and hand off to that adapter — clean structured data, no scraping.
  2. **Selector scraping.** Only if sniffing finds nothing. Config supplies the CSS
     selectors; without them we fall back to link-text heuristics.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any

from ..models import Job
from .base import Source, looks_remote, strip_html

log = logging.getLogger(__name__)

# board slug -> which adapter can read it
_ATS_PATTERNS: dict[str, re.Pattern[str]] = {
    "greenhouse": re.compile(
        r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I
    ),
    "lever": re.compile(r"jobs\.(?:eu\.)?lever\.co/([a-z0-9_-]+)", re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_.-]+)", re.I),
    "workable": re.compile(r"([a-z0-9_-]+)\.workable\.com|apply\.workable\.com/([a-z0-9_-]+)", re.I),
    "smartrecruiters": re.compile(r"jobs\.smartrecruiters\.com/([a-z0-9_-]+)", re.I),
    "recruitee": re.compile(r"([a-z0-9_-]+)\.recruitee\.com", re.I),
}

_JOB_LINK_HINTS = re.compile(r"/(job|career|position|opening|vacanc|opportunit)", re.I)
_NOISE = re.compile(r"^(apply|view|learn more|read more|see all|details|open)$", re.I)


def sniff_ats(html: str) -> tuple[str, str] | None:
    """Return (ats_name, board_slug) if the page is backed by a known ATS."""
    for ats, pattern in _ATS_PATTERNS.items():
        match = pattern.search(html)
        if match:
            slug = next((g for g in match.groups() if g), "")
            if slug and slug.lower() not in ("www", "apply", "jobs", "boards"):
                return ats, slug
    return None


class CareerPageSource(Source):
    """Handles the `sources.custom_career_pages` list."""

    name = "career_page"

    def __init__(self, config: dict[str, Any], session: Any = None, **_: Any):
        super().__init__(config, session)
        self.pages: list[dict] = self.config.get("pages", []) or []
        self.max_links = int(self.config.get("max_links_per_page", 60))
        self.fetch_descriptions = bool(self.config.get("fetch_descriptions", True))

    def fetch(self, queries: list[str]) -> list[Job]:  # noqa: ARG002 - filtering happens in Scorer
        jobs: list[Job] = []
        for entry in self.pages:
            url = entry.get("url", "")
            name = entry.get("name") or urllib.parse.urlparse(url).netloc
            if not url:
                continue
            try:
                found = self._fetch_page(entry, name)
                log.info("career_page: %s -> %d jobs", name, len(found))
                jobs.extend(found)
            except Exception as exc:
                log.warning("career_page: %s failed — %s", name, exc)
            self._sleep()
        return jobs

    def _fetch_page(self, entry: dict, company: str) -> list[Job]:
        url = entry["url"]
        html = self._get_text(url)

        # 1. Declared or sniffed ATS — always better than scraping.
        declared_ats = entry.get("ats")
        declared_slug = entry.get("board")
        detected = (declared_ats, declared_slug) if declared_ats and declared_slug else sniff_ats(html)

        if detected:
            ats, slug = detected
            log.info("career_page: %s is backed by %s (board '%s') — using its API", company, ats, slug)
            jobs = self._delegate(ats, slug)
            if jobs:
                for job in jobs:
                    job.company = company  # prefer the human name from config
                    job.source = f"{self.name}:{ats}"
                return jobs
            log.info("career_page: %s board returned nothing, falling back to scraping", company)

        # 2. Scrape links.
        return self._scrape(html, entry, company)

    def _delegate(self, ats: str, slug: str) -> list[Job]:
        from .ashby import AshbySource
        from .greenhouse import GreenhouseSource
        from .lever import LeverSource
        from .recruitee import RecruiteeSource
        from .smartrecruiters import SmartRecruitersSource
        from .workable import WorkableSource

        adapters = {
            "greenhouse": GreenhouseSource, "lever": LeverSource, "ashby": AshbySource,
            "workable": WorkableSource, "smartrecruiters": SmartRecruitersSource,
            "recruitee": RecruiteeSource,
        }
        adapter_cls = adapters.get(ats)
        if not adapter_cls:
            return []
        adapter = adapter_cls({"companies": [slug]}, session=self.session)
        return adapter.fetch_company(slug)

    def _scrape(self, html: str, entry: dict, company: str) -> list[Job]:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            log.warning(
                "career_page: beautifulsoup4 not installed, cannot scrape %s "
                "(pip install beautifulsoup4)", company
            )
            return []

        soup = BeautifulSoup(html, "html.parser")
        base_url = entry["url"]
        link_selector = entry.get("job_link_selector")
        anchors = soup.select(link_selector) if link_selector else soup.find_all("a", href=True)

        jobs: list[Job] = []
        seen: set[str] = set()

        for anchor in anchors:
            href = anchor.get("href") or ""
            title = " ".join(anchor.get_text(" ", strip=True).split())
            if not href or not title or _NOISE.match(title) or len(title) < 6:
                continue

            absolute = urllib.parse.urljoin(base_url, href)
            if absolute in seen:
                continue
            # Without an explicit selector, only follow links that look like postings.
            if not link_selector and not _JOB_LINK_HINTS.search(absolute):
                continue
            seen.add(absolute)

            jobs.append(
                Job(
                    source=self.name,
                    ats=entry.get("ats", "custom"),
                    company=company,
                    title=title,
                    url=absolute,
                    apply_url=absolute,
                    location=entry.get("default_location", ""),
                    remote=looks_remote(title, entry.get("default_location", "")),
                    raw={"scraped_from": base_url},
                )
            )
            if len(jobs) >= self.max_links:
                break

        if self.fetch_descriptions:
            for job in jobs:
                try:
                    job.description = strip_html(self._get_text(job.url))[:8000]
                except Exception as exc:
                    log.debug("career_page: could not read description for %s — %s", job.url, exc)
                self._sleep()

        return jobs
