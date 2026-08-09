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

# Navigation and utility links. A careers page is full of these, and they all sit under
# a /careers/ path, so the URL hint alone lets every one of them through.
_NAV_RE = re.compile(
    r"^(apply|apply now|view|view all|view job|learn more|read more|see all|see more|details|"
    r"open|open roles|search|search jobs|explore|explore jobs|browse jobs|all jobs|jobs|"
    r"careers?|job search|login|log in|sign in|sign up|signup|register|get started|"
    r"skip to (main )?content|home|about|about us|contact|contact us|privacy|privacy policy|"
    r"terms|cookies|blog|news|press|support|help|faq|back|back to (jobs|search|results)|"
    r"next|previous|more|menu|close|share|save|life at .*|our (culture|values|team|people)|"
    r"why (join|work).*|benefits|diversity|students?|internships?|interns)$",
    re.I,
)

# Calls to action that only *start* with a nav phrase, so the anchored match above
# misses them — "Sign Up for Free", "Join us. Do good work.", "Grow with Chargebee".
_NAV_PREFIX_RE = re.compile(
    r"^(sign\s?up|sign\s?in|log\s?in|register|subscribe|get started|try (it )?free|"
    r"start (free|now)|book a demo|request a demo|contact|join us|grow with|work with|"
    r"how we|why we|meet the|read (the|our)|watch|download|learn about|discover)\b",
    re.I,
)

# A real posting URL points at one job, not a section: a numeric/hashed id, or a path
# segment below the listing page.
_JOB_URL_RE = re.compile(
    r"/(jobs?|careers?|positions?|openings?|vacanc\w*|opportunit\w*)/[^/?#]{4,}", re.I
)
_ID_RE = re.compile(r"(\d{4,}|[0-9a-f]{8}-[0-9a-f]{4})")
_TITLE_WORD_RE = re.compile(
    r"\b(engineer|developer|analyst|manager|architect|consultant|specialist|designer|"
    r"scientist|administrator|lead|director|executive|associate|officer|technician|"
    r"programmer|tester|intern|apprentice|advisor|representative|coordinator|"
    r"strategist|marketer|recruiter|accountant|counsel|partner|principal|head)\b",
    re.I,
)


def looks_like_a_job_link(title: str, url: str, base_url: str) -> bool:
    """Decide whether a scraped anchor is a job posting rather than site furniture.

    Heuristics, deliberately strict: a career page has far more nav links than jobs, and
    a false positive becomes a bogus row the user has to sift out. Anything genuinely
    missed can be recovered by setting an explicit `job_link_selector`.
    """
    words = title.split()
    if not 2 <= len(words) <= 14 or len(title) < 6 or len(title) > 120:
        return False
    stripped = title.strip()
    if _NAV_RE.match(stripped) or _NAV_PREFIX_RE.match(stripped):
        return False

    clean_url = url.split("?")[0].rstrip("/")
    if clean_url == base_url.split("?")[0].rstrip("/"):
        return False  # a link back to the page we're already on

    # It must look like an individual posting, or read like a job title.
    return bool(_JOB_URL_RE.search(clean_url) or _ID_RE.search(clean_url)) or bool(
        _TITLE_WORD_RE.search(title)
    )


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

    def __init__(self, config: dict[str, Any], session: Any = None, browser: Any = None, **_: Any):
        super().__init__(config, session)
        self.pages: list[dict] = self.config.get("pages", []) or []
        self.max_links = int(self.config.get("max_links_per_page", 60))
        self.fetch_descriptions = bool(self.config.get("fetch_descriptions", True))
        # Most modern career pages render their job list in JavaScript, so plain HTTP
        # sees an empty shell. When that happens we re-open the page in the browser and
        # scrape the rendered DOM instead.
        self.browser = browser
        self.use_browser_fallback = bool(self.config.get("use_browser_fallback", True))
        self.min_jobs_before_fallback = int(self.config.get("min_jobs_before_fallback", 3))

    def fetch(self, queries: list[str]) -> list[Job]:  # noqa: ARG002 - filtering happens in Scorer
        jobs: list[Job] = []
        for entry in self.pages:
            url = entry.get("url", "")
            name = entry.get("name") or urllib.parse.urlparse(url).netloc
            if not url:
                continue
            if self.cancelled:
                log.info("career_page: stopped before '%s'", name)
                break
            try:
                found = self._fetch_page(entry, name)
                log.info("career_page: %s -> %d jobs", name, len(found))
                jobs.extend(found)
                self._emit(found)
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

        # 2. Scrape links from the raw HTML.
        jobs = self._scrape(html, entry, company)

        # 3. If that found little or nothing, the page is probably rendered client-side.
        if (
            len(jobs) < self.min_jobs_before_fallback
            and self.use_browser_fallback
            and self.browser is not None
        ):
            log.info(
                "career_page: %s yielded %d jobs from raw HTML — re-reading it in the browser",
                company, len(jobs),
            )
            rendered = self._scrape_rendered(entry, company)
            if len(rendered) > len(jobs):
                jobs = rendered

        if not jobs:
            log.warning(
                "career_page: found no jobs at %s. If the list loads in JavaScript, add a "
                "job_link_selector for it, or point the URL straight at the underlying "
                "job board.", entry["url"],
            )
        return jobs

    def _scrape_rendered(self, entry: dict, company: str) -> list[Job]:
        """Open the page in the real browser and scrape the DOM after scripts have run."""
        page = None
        try:
            page = self.browser.new_page()
            page.goto(entry["url"], wait_until="domcontentloaded")
            page.wait_for_timeout(int(self.config.get("render_wait_ms", 3500)))

            selector = entry.get("job_link_selector")
            if selector:
                try:
                    page.wait_for_selector(selector, timeout=8000)
                except Exception:
                    pass

            html = page.content()
            # An ATS widget often only appears after render, so try sniffing again.
            if (detected := sniff_ats(html)) and not entry.get("ats"):
                ats, slug = detected
                log.info("career_page: %s renders a %s board ('%s')", company, ats, slug)
                delegated = self._delegate(ats, slug)
                if delegated:
                    for job in delegated:
                        job.company = company
                        job.source = f"{self.name}:{ats}"
                    return delegated

            return self._scrape(html, entry, company)
        except Exception as exc:
            log.warning("career_page: browser render failed for %s — %s", company, exc)
            return []
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

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
            if not href or not title or href.startswith(("#", "javascript:", "mailto:")):
                continue

            absolute = urllib.parse.urljoin(base_url, href)
            if absolute in seen:
                continue

            # An explicit selector is the user telling us exactly what the job links are,
            # so trust it. Without one, filter hard — a careers page is mostly navigation.
            if not link_selector:
                if not _JOB_LINK_HINTS.search(absolute):
                    continue
                if not looks_like_a_job_link(title, absolute, base_url):
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
