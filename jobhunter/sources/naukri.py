"""Naukri.com discovery.

Naukri's public search API returns HTTP 406 `recaptcha required` to anonymous
callers, so discovery runs inside the logged-in browser context instead. Two paths
are tried, best first:

  1. The internal search API via the browser's own request context — it inherits the
     session cookies, so the recaptcha gate does not apply and we get clean JSON.
  2. DOM scraping of the rendered results page, if the API shape changes.

Run `jobhunter login naukri` once to seed the session.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

from ..browser import BrowserSession, is_logged_out
from ..models import Job
from .base import Source, looks_remote, parse_epoch_ms, strip_html

log = logging.getLogger(__name__)

SEARCH_API = (
    "https://www.naukri.com/jobapi/v3/search"
    "?noOfResults={size}&urlType=search_by_keyword&searchType=adv"
    "&keyword={keyword}&pageNo={page}{extra}"
)
SEARCH_PAGE = "https://www.naukri.com/jobs-in-india?k={keyword}&pageNo={page}{extra}"

_API_HEADERS = {
    "appid": "109",
    "systemid": "Naukri",
    "Accept": "application/json",
    "Referer": "https://www.naukri.com/",
}

# Search results carry a one-line snippet only. Without the full text the keyword half
# of the match score has nothing to work with, so every Naukri job scores low on
# relevance regardless of how well it actually fits.
DETAIL_API = "https://www.naukri.com/jobapi/v4/job/{job_id}?microsite=y&src=jobsearchDesk"
_DETAIL_HEADERS = {
    "appid": "121",
    "systemid": "121",
    "Accept": "application/json",
    "Referer": "https://www.naukri.com/",
}

# Naukri has renamed its JD container repeatedly; try the stable-ish ones in order.
_JD_SELECTORS = (
    "[class*='JDC__dang-inner-html']",
    "div.dang-inner-html",
    "section.job-desc",
    "div.job-desc",
    "[class*='job-description']",
)
_JOB_ID_RE = re.compile(r"-(\d{6,})(?:\?|$)")

# "3 Days Ago", "Just Now", "Few Hours Ago"
_AGE_RE = re.compile(r"(\d+)\s*(day|hour|week|month)", re.IGNORECASE)

_SCRAPE_JS = """
() => {
  const pick = (root, sels) => {
    for (const s of sels) { const el = root.querySelector(s); if (el && el.innerText.trim()) return el.innerText.trim(); }
    return "";
  };
  const cards = document.querySelectorAll(
    'div.srp-jobtuple-wrapper, article.jobTuple, div.cust-job-tuple, div[data-job-id]'
  );
  return Array.from(cards).map(c => {
    const link = c.querySelector('a.title, a.jobTitle, a[href*="/job-listings-"]');
    return {
      title: link ? link.innerText.trim() : pick(c, ['.title', '.jobTitle']),
      url: link ? link.href : "",
      company: pick(c, ['a.comp-name', 'a.subTitle', '.companyInfo span', '.comp-name']),
      location: pick(c, ['span.locWdth', 'li.location', '.loc span', '[title*="location" i]']),
      experience: pick(c, ['span.expwdth', 'li.experience', '.exp span']),
      salary: pick(c, ['span.sal', 'li.salary', '.sal-wrap span']),
      description: pick(c, ['span.job-desc', '.job-description', '.jobDescription']),
      posted: pick(c, ['span.job-post-day', '.jobPostDay', '.type br + span'])
    };
  }).filter(j => j.title && j.url);
}
"""


def _parse_relative_age(text: str) -> datetime | None:
    if not text:
        return None
    lowered = text.lower()
    if "just now" in lowered or "few hours" in lowered or "today" in lowered:
        return datetime.now(timezone.utc)
    match = _AGE_RE.search(lowered)
    if not match:
        return None
    amount, unit = int(match.group(1)), match.group(2).lower()
    days = {"hour": amount / 24, "day": amount, "week": amount * 7, "month": amount * 30}[unit]
    return datetime.now(timezone.utc) - timedelta(days=days)


def _parse_experience(text: str) -> tuple[float | None, float | None]:
    """'3-5 Yrs' -> (3, 5);  '5+ Yrs' -> (5, None)."""
    if not text:
        return None, None
    if (m := re.search(r"(\d+)\s*-\s*(\d+)", text)):
        return float(m.group(1)), float(m.group(2))
    if (m := re.search(r"(\d+)\s*\+", text)):
        return float(m.group(1)), None
    if (m := re.search(r"(\d+)", text)):
        return float(m.group(1)), float(m.group(1))
    return None, None


class NaukriSource(Source):
    name = "naukri"

    def __init__(self, config: dict[str, Any], browser: BrowserSession | None = None, **_: Any):
        super().__init__(config)
        self.browser = browser
        self.max_pages = int(self.config.get("max_pages", 3))
        self.results_per_page = int(self.config.get("results_per_page", 20))
        self.fetch_details = bool(self.config.get("fetch_details", True))
        self.detail_limit = int(self.config.get("detail_limit", 30))

    def _extra_params(self) -> str:
        """Experience / location / freshness filters, as Naukri's query string wants them."""
        parts = []
        if (exp := self.config.get("experience_years")) is not None:
            parts.append(f"&experience={int(exp)}")
        if locations := self.config.get("locations"):
            parts.append("&location=" + urllib.parse.quote(",".join(locations)))
        if (age := self.config.get("job_age_days")) is not None:
            parts.append(f"&jobAge={int(age)}")
        return "".join(parts)

    def fetch(self, queries: list[str]) -> list[Job]:
        if self.browser is None:
            log.warning("naukri: no browser session available — skipping (run `jobhunter login naukri`)")
            return []

        page = self.browser.new_page()
        jobs: list[Job] = []
        enriched = 0
        try:
            page.goto("https://www.naukri.com/", wait_until="domcontentloaded")
            if is_logged_out(page):
                log.warning(
                    "naukri: not logged in. Run `jobhunter login naukri` and sign in once — "
                    "the session is then reused every morning."
                )

            for query in queries:
                if self.cancelled:
                    log.info("naukri: stopped")
                    break
                found = self._search(page, query)
                if self.fetch_details:
                    budget = max(0, self.detail_limit - enriched)
                    enriched += self._enrich(page, found, budget)
                log.info("naukri: '%s' -> %d jobs", query, len(found))
                jobs.extend(found)
                self._emit(found)
                self._sleep()
        finally:
            try:
                page.close()
            except Exception:
                pass
        return jobs

    # --- detail enrichment ---

    @staticmethod
    def job_id_from(job: Job) -> str:
        """Naukri's numeric job id, from the search payload or the posting URL."""
        existing = str(job.raw.get("job_id") or "")
        if existing:
            return existing
        match = _JOB_ID_RE.search(job.url or "")
        return match.group(1) if match else ""

    @staticmethod
    def parse_detail_payload(payload: dict) -> tuple[str, int | None]:
        """Pull (description, applicant count) out of the job-detail JSON."""
        details = (payload or {}).get("jobDetails") or {}
        description = strip_html(
            details.get("description") or details.get("jobDescription") or ""
        )

        applicants = details.get("applyCount")
        if applicants is None:
            applicants = (details.get("applicationDetail") or {}).get("applyCount")
        try:
            applicants = int(applicants) if applicants is not None else None
        except (TypeError, ValueError):
            applicants = None

        return description, applicants

    def _enrich(self, page: Any, jobs: list[Job], budget: int) -> int:
        """Fetch full descriptions for up to `budget` jobs. Returns how many were done.

        Tries the detail API through the browser's session first — it returns clean
        JSON — and falls back to reading the posting page when that shape changes.
        """
        done = 0
        for job in jobs:
            if done >= budget or self.cancelled:
                break
            job_id = self.job_id_from(job)
            if not job_id or len(job.description) > 600:
                continue  # nothing to look up, or already substantial

            if self._enrich_via_api(page, job, job_id) or self._enrich_via_page(page, job):
                done += 1
            self._sleep()

        if done:
            log.info("naukri: fetched full descriptions for %d job(s)", done)
        return done

    def _enrich_via_api(self, page: Any, job: Job, job_id: str) -> bool:
        try:
            response = page.request.get(
                DETAIL_API.format(job_id=job_id), headers=_DETAIL_HEADERS
            )
            if not response.ok:
                return False
            description, applicants = self.parse_detail_payload(response.json())
        except Exception as exc:
            log.debug("naukri: detail api failed for %s — %s", job_id, exc)
            return False

        if not description:
            return False
        job.description = description
        if applicants is not None:
            job.applicants = applicants
        return True

    def _enrich_via_page(self, page: Any, job: Job) -> bool:
        try:
            page.goto(job.url, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            text = page.evaluate(
                """(sels) => {
                    for (const s of sels) {
                      const el = document.querySelector(s);
                      if (el && el.innerText.trim().length > 200) return el.innerText.trim();
                    }
                    return "";
                }""",
                list(_JD_SELECTORS),
            )
        except Exception as exc:
            log.debug("naukri: detail page failed for %s — %s", job.url, exc)
            return False

        if not text:
            return False
        job.description = text[:12000]
        return True

    def _search(self, page: Any, query: str) -> list[Job]:
        jobs: list[Job] = []
        extra = self._extra_params()

        for page_no in range(1, self.max_pages + 1):
            api_jobs = self._try_api(page, query, page_no, extra)
            if api_jobs is not None:
                jobs.extend(api_jobs)
                if len(api_jobs) < self.results_per_page:
                    break  # last page
            else:
                dom_jobs = self._try_dom(page, query, page_no, extra)
                jobs.extend(dom_jobs)
                if not dom_jobs:
                    break
            self._sleep()
        return jobs

    def _try_api(self, page: Any, query: str, page_no: int, extra: str) -> list[Job] | None:
        """Returns None (not []) when the API path is unusable, so the caller can fall back."""
        url = SEARCH_API.format(
            size=self.results_per_page, keyword=urllib.parse.quote(query), page=page_no, extra=extra
        )
        try:
            response = page.request.get(url, headers=_API_HEADERS)
            if not response.ok:
                log.debug("naukri api HTTP %s — falling back to DOM scraping", response.status)
                return None
            payload = response.json()
        except Exception as exc:
            log.debug("naukri api call failed (%s) — falling back to DOM scraping", exc)
            return None

        details = payload.get("jobDetails")
        if details is None:
            return None
        return [self._job_from_api(item) for item in details if item.get("title")]

    def _job_from_api(self, item: dict) -> Job:
        placeholders = {p.get("type"): p.get("label", "") for p in item.get("placeholders", [])}
        location = placeholders.get("location", "")
        experience = placeholders.get("experience", "")
        min_exp, max_exp = _parse_experience(experience)

        url = item.get("jdURL", "")
        if url and not url.startswith("http"):
            url = "https://www.naukri.com" + url

        posted = parse_epoch_ms(item.get("createdDate")) or _parse_relative_age(
            item.get("footerPlaceholderLabel", "")
        )

        return Job(
            source=self.name,
            ats="naukri",
            company=item.get("companyName", ""),
            title=item.get("title", ""),
            url=url,
            apply_url=url,
            location=location,
            description=strip_html(item.get("jobDescription", "")),
            posted_at=posted,
            remote=looks_remote(location, item.get("title", "")),
            min_experience_years=min_exp,
            max_experience_years=max_exp,
            salary_text=placeholders.get("salary", ""),
            raw={"job_id": item.get("jobId", ""), "experience_text": experience},
        )

    def _try_dom(self, page: Any, query: str, page_no: int, extra: str) -> list[Job]:
        url = SEARCH_PAGE.format(keyword=urllib.parse.quote(query), page=page_no, extra=extra)
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)  # results hydrate client-side
            cards = page.evaluate(_SCRAPE_JS)
        except Exception as exc:
            log.warning("naukri: DOM scrape failed for '%s' page %d — %s", query, page_no, exc)
            return []

        jobs = []
        for card in cards or []:
            min_exp, max_exp = _parse_experience(card.get("experience", ""))
            jobs.append(
                Job(
                    source=self.name,
                    ats="naukri",
                    company=card.get("company", ""),
                    title=card.get("title", ""),
                    url=card.get("url", ""),
                    apply_url=card.get("url", ""),
                    location=card.get("location", ""),
                    description=card.get("description", ""),
                    posted_at=_parse_relative_age(card.get("posted", "")),
                    remote=looks_remote(card.get("location", ""), card.get("title", "")),
                    min_experience_years=min_exp,
                    max_experience_years=max_exp,
                    salary_text=card.get("salary", ""),
                    raw={"scraped": True},
                )
            )
        return jobs
