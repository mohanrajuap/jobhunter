"""Ashby job boards — https://api.ashbyhq.com/posting-api (public, no auth).

The richest of the public board APIs: one call returns descriptions, apply URLs and
remote flags, so no per-job detail fetch is needed.
"""

from __future__ import annotations

from ..models import Job
from .base import BoardSource, looks_remote, parse_iso, strip_html

API = "https://api.ashbyhq.com/posting-api/job-board/{company}?includeCompensation=true"


class AshbySource(BoardSource):
    name = "ashby"

    def fetch_company(self, company: str) -> list[Job]:
        payload = self._get_json(API.format(company=company))
        jobs: list[Job] = []

        for item in payload.get("jobs", []):
            if item.get("isListed") is False:
                continue

            location = item.get("location", "") or ""
            secondary = ", ".join(
                s.get("location", "") for s in item.get("secondaryLocations", []) if s.get("location")
            )
            full_location = ", ".join(p for p in (location, secondary) if p)
            description = item.get("descriptionPlain") or strip_html(item.get("descriptionHtml"))

            jobs.append(
                Job(
                    source=self.name,
                    ats="ashby",
                    company=company.replace("-", " ").title(),
                    title=(item.get("title") or "").strip(),
                    url=item.get("jobUrl", ""),
                    apply_url=item.get("applyUrl") or item.get("jobUrl", ""),
                    location=full_location,
                    description=description,
                    posted_at=parse_iso(item.get("publishedAt")),
                    remote=bool(item.get("isRemote")) or looks_remote(full_location),
                    raw={
                        "id": item.get("id"),
                        "team": item.get("team", ""),
                        "employment_type": item.get("employmentType", ""),
                    },
                )
            )
        return jobs
