"""Recruitee job boards — https://{company}.recruitee.com/api/offers/ (public, no auth).

Note: Recruitee subdomains disappear when a company migrates ATS, and the endpoint
404s rather than returning an empty list. `jobhunter doctor` flags dead slugs.
"""

from __future__ import annotations

from ..models import Job
from .base import BoardSource, looks_remote, parse_iso, strip_html

API = "https://{company}.recruitee.com/api/offers/"


class RecruiteeSource(BoardSource):
    name = "recruitee"

    def fetch_company(self, company: str) -> list[Job]:
        payload = self._get_json(API.format(company=company))
        jobs: list[Job] = []

        for item in payload.get("offers", []):
            if item.get("status") not in (None, "published"):
                continue

            location = item.get("location") or ", ".join(
                p for p in (item.get("city"), item.get("country")) if p
            )
            careers_url = item.get("careers_url") or item.get("careers_apply_url", "")

            jobs.append(
                Job(
                    source=self.name,
                    ats="recruitee",
                    company=company.replace("-", " ").title(),
                    title=item.get("title", ""),
                    url=careers_url,
                    apply_url=item.get("careers_apply_url") or careers_url,
                    location=location,
                    description=strip_html(
                        "\n".join(filter(None, [item.get("description", ""), item.get("requirements", "")]))
                    ),
                    posted_at=parse_iso(item.get("published_at") or item.get("created_at")),
                    remote=bool(item.get("remote")) or looks_remote(location),
                    raw={"id": item.get("id"), "department": item.get("department", "")},
                )
            )
        return jobs
