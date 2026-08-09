"""SmartRecruiters job boards — https://api.smartrecruiters.com (public, no auth).

The listing endpoint omits descriptions, so matching quality depends on a per-posting
detail call. That is capped by `detail_limit` to keep the daily run fast and polite.
"""

from __future__ import annotations

import logging

from ..models import Job
from .base import BoardSource, looks_remote, parse_iso, strip_html

LIST_API = "https://api.smartrecruiters.com/v1/companies/{company}/postings?limit={limit}&offset={offset}"
DETAIL_API = "https://api.smartrecruiters.com/v1/companies/{company}/postings/{posting_id}"

log = logging.getLogger(__name__)


class SmartRecruitersSource(BoardSource):
    name = "smartrecruiters"

    def fetch_company(self, company: str) -> list[Job]:
        page_size = int(self.config.get("page_size", 100))
        max_postings = int(self.config.get("max_postings_per_company", 200))
        detail_limit = int(self.config.get("detail_limit", 40))

        raw_postings: list[dict] = []
        offset = 0
        while len(raw_postings) < max_postings:
            payload = self._get_json(
                LIST_API.format(company=company, limit=page_size, offset=offset)
            )
            batch = payload.get("content", [])
            if not batch:
                break
            raw_postings.extend(batch)
            offset += len(batch)
            if offset >= int(payload.get("totalFound", 0)):
                break
            self._sleep()

        jobs = [self._to_job(company, p) for p in raw_postings[:max_postings]]

        # Enrich only the ones most likely to matter — detail calls are the slow part.
        for job in jobs[:detail_limit]:
            try:
                self._enrich(company, job)
            except Exception as exc:
                log.debug("smartrecruiters: detail fetch failed for %s — %s", job.title, exc)
            self._sleep()

        return jobs

    def _to_job(self, company: str, posting: dict) -> Job:
        loc = posting.get("location") or {}
        location = loc.get("fullLocation") or ", ".join(
            p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p
        )
        posting_id = posting.get("id", "")
        company_name = (posting.get("company") or {}).get("name") or company

        return Job(
            source=self.name,
            ats="smartrecruiters",
            company=company_name,
            title=posting.get("name", ""),
            url=f"https://jobs.smartrecruiters.com/{company}/{posting_id}",
            apply_url="",  # filled in by _enrich
            location=location,
            posted_at=parse_iso(posting.get("releasedDate")),
            remote=bool(loc.get("remote")) or looks_remote(location),
            raw={"id": posting_id, "company_slug": company, "ref": posting.get("refNumber", "")},
        )

    def _enrich(self, company: str, job: Job) -> None:
        detail = self._get_json(
            DETAIL_API.format(company=company, posting_id=job.raw.get("id", ""))
        )
        sections = (detail.get("jobAd") or {}).get("sections") or {}
        parts = [
            (sections.get(key) or {}).get("text", "")
            for key in ("jobDescription", "qualifications", "additionalInformation")
        ]
        job.description = strip_html("\n".join(p for p in parts if p))
        job.apply_url = detail.get("applyUrl") or detail.get("postingUrl") or job.url
        if detail.get("experienceLevel", {}).get("id"):
            job.raw["experience_level"] = detail["experienceLevel"]["id"]
