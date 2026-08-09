"""Greenhouse job boards — https://boards-api.greenhouse.io (public, no auth)."""

from __future__ import annotations

from ..models import Job
from .base import BoardSource, looks_remote, parse_iso, strip_html

API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"


class GreenhouseSource(BoardSource):
    name = "greenhouse"

    def fetch_company(self, company: str) -> list[Job]:
        payload = self._get_json(API.format(board=company))
        jobs: list[Job] = []

        for item in payload.get("jobs", []):
            location = (item.get("location") or {}).get("name", "")
            title = item.get("title", "")
            url = item.get("absolute_url", "")
            company_name = (
                (item.get("company_name") or "").strip() or company.replace("-", " ").title()
            )
            jobs.append(
                Job(
                    source=self.name,
                    ats="greenhouse",
                    company=company_name,
                    title=title,
                    url=url,
                    apply_url=url,  # Greenhouse serves the form on the posting page itself
                    location=location,
                    description=strip_html(item.get("content")),
                    posted_at=parse_iso(item.get("updated_at") or item.get("first_published")),
                    remote=looks_remote(location, title),
                    raw={"id": item.get("id"), "board": company},
                )
            )
        return jobs
