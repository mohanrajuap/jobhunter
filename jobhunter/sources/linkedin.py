"""LinkedIn job search via the public guest endpoint.

LinkedIn's logged-out job search is served by `/jobs-guest/jobs/api/seeMoreJobPostings`,
which returns plain HTML cards and needs no account. That is what this source reads.

Applying is a different matter: LinkedIn "Easy Apply" sits behind a login and a
multi-step modal, so applications are routed to your manual queue with a direct link
rather than automated. Discovery here is about *finding* the job — many postings link
straight through to the company's own ATS, which the browser applier can handle.

Note: LinkedIn's terms restrict automated access. This performs the same searches you
would run by hand, at a human pace, but the account risk is yours.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any

from ..models import Job
from .base import Source, looks_remote, parse_iso

log = logging.getLogger(__name__)

SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?{query}"
DETAIL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"

_JOB_ID_RE = re.compile(r"-(\d{6,})(?:\?|$)")
# "Be among the first 25 applicants" / "137 applicants" / "Over 200 applicants"
_APPLICANTS_RE = re.compile(r"(?:first\s+)?([\d,]+)\s*\+?\s*applicants", re.I)

# LinkedIn's f_TPR filter, in seconds.
_AGE_FILTERS = {1: "r86400", 7: "r604800", 30: "r2592000"}
_PAGE_SIZE = 10


class LinkedInSource(Source):
    name = "linkedin"

    def __init__(self, config: dict[str, Any], session: Any = None, **_: Any):
        super().__init__(config, session)
        self.locations: list[str] = self.config.get("locations") or [""]
        self.max_pages = int(self.config.get("max_pages", 3))
        self.posted_within_days = self.config.get("posted_within_days")
        self.remote_only = bool(self.config.get("remote_only", False))
        self.polite_delay = float(self.config.get("delay_seconds", 1.5))
        # Search cards carry no description, which caps LinkedIn matches around 65%.
        # One extra request per job fixes that and yields the applicant count, so it's
        # on by default but capped — it is the slowest thing this source does.
        self.fetch_details = bool(self.config.get("fetch_details", True))
        self.detail_limit = int(self.config.get("detail_limit", 25))

    def _params(self, query: str, location: str, start: int) -> str:
        params: dict[str, Any] = {"keywords": query, "start": start}
        if location:
            params["location"] = location
        if self.posted_within_days:
            # Round up to the nearest filter LinkedIn actually supports.
            for days in sorted(_AGE_FILTERS):
                if int(self.posted_within_days) <= days:
                    params["f_TPR"] = _AGE_FILTERS[days]
                    break
        if self.remote_only:
            params["f_WT"] = 2
        return urllib.parse.urlencode(params)

    def fetch(self, queries: list[str]) -> list[Job]:
        try:
            from bs4 import BeautifulSoup  # noqa: F401
        except ImportError:
            log.error("linkedin: beautifulsoup4 is required — pip install beautifulsoup4")
            return []

        jobs: list[Job] = []
        enriched = 0
        for query in queries:
            for location in self.locations:
                if self.cancelled:
                    log.info("linkedin: stopped")
                    return jobs
                found = self._search(query, location)
                if self.fetch_details:
                    self._enrich(found, budget=max(0, self.detail_limit - enriched))
                    enriched += min(len(found), max(0, self.detail_limit - enriched))
                log.info("linkedin: '%s' in '%s' -> %d jobs", query, location or "anywhere", len(found))
                jobs.extend(found)
                self._emit(found)
        return jobs

    def _enrich(self, jobs: list[Job], budget: int) -> None:
        """Pull description and applicant count from each posting page.

        Best-effort per job: a failure here loses detail, not the job itself.
        """
        for job in jobs[:budget]:
            if self.cancelled:
                return
            job_id = job.raw.get("job_id") or ""
            if not job_id:
                continue
            try:
                html = self._get_text(DETAIL.format(job_id=job_id))
            except Exception as exc:
                log.debug("linkedin: detail fetch failed for %s — %s", job_id, exc)
                continue

            description, applicants_text = self._parse_detail(html)
            if description:
                job.description = description
            if applicants_text:
                job.applicants_text = applicants_text
                if (match := _APPLICANTS_RE.search(applicants_text)):
                    try:
                        job.applicants = int(match.group(1).replace(",", ""))
                    except ValueError:
                        pass
            self._sleep()

    def _parse_detail(self, html: str) -> tuple[str, str]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        body = soup.select_one(".description__text, .show-more-less-html__markup")
        caption = soup.select_one(".num-applicants__caption")
        return (
            body.get_text(" ", strip=True)[:8000] if body else "",
            caption.get_text(strip=True) if caption else "",
        )

    def _search(self, query: str, location: str) -> list[Job]:
        jobs: list[Job] = []
        for page in range(self.max_pages):
            if self.cancelled:
                break
            url = SEARCH.format(query=self._params(query, location, page * _PAGE_SIZE))
            try:
                html = self._get_text(url)
            except Exception as exc:
                log.warning("linkedin: page %d for '%s' failed — %s", page, query, exc)
                break

            page_jobs = self._parse(html)
            jobs.extend(page_jobs)
            if len(page_jobs) < _PAGE_SIZE:
                break  # last page
            self._sleep()
        return jobs

    def _parse(self, html: str) -> list[Job]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        jobs: list[Job] = []

        for card in soup.select("li"):
            title_el = card.select_one("h3")
            link_el = card.select_one("a.base-card__full-link") or card.select_one("a[href*='/jobs/view/']")
            if not title_el or not link_el:
                continue

            company_el = card.select_one("h4")
            location_el = card.select_one(".job-search-card__location")
            time_el = card.select_one("time")

            title = title_el.get_text(strip=True)
            company = company_el.get_text(strip=True) if company_el else ""
            location = location_el.get_text(strip=True) if location_el else ""
            # Strip LinkedIn's tracking query string; the bare URL is stable.
            url = (link_el.get("href") or "").split("?")[0]

            if not title or not url:
                continue

            jobs.append(
                Job(
                    source=self.name,
                    ats="linkedin",
                    company=company,
                    title=title,
                    url=url,
                    apply_url=url,
                    location=location,
                    # The guest card has no description; the Scorer leans on the title
                    # and location for these, which is why min_score matters here.
                    description="",
                    posted_at=parse_iso(time_el.get("datetime")) if time_el else None,
                    remote=looks_remote(location, title),
                    applicants_text=self._early_hint(card),
                    raw={"guest_card": True, "job_id": self._job_id(url)},
                )
            )
        return jobs

    @staticmethod
    def _job_id(url: str) -> str:
        match = _JOB_ID_RE.search(url)
        return match.group(1) if match else ""

    @staticmethod
    def _early_hint(card: Any) -> str:
        """"Be an early applicant" is all the search card offers; the detail fetch
        replaces it with a real number when it runs."""
        el = card.select_one(".job-posting-benefits__text")
        return el.get_text(strip=True) if el else ""
