"""Lever job boards — https://api.lever.co/v0/postings (public, no auth)."""

from __future__ import annotations

from ..models import Job
from .base import BoardSource, looks_remote, parse_epoch_ms

API = "https://api.lever.co/v0/postings/{company}?mode=json"


class LeverSource(BoardSource):
    name = "lever"

    def fetch_company(self, company: str) -> list[Job]:
        payload = self._get_json(API.format(company=company))
        jobs: list[Job] = []

        for item in payload:
            categories = item.get("categories") or {}
            location = categories.get("location", "") or ""
            title = item.get("text", "")
            workplace = item.get("workplaceType", "") or ""

            jobs.append(
                Job(
                    source=self.name,
                    ats="lever",
                    company=company.replace("-", " ").title(),
                    title=title,
                    url=item.get("hostedUrl", ""),
                    # Lever's applyUrl is the form; hostedUrl is the description page.
                    apply_url=item.get("applyUrl") or f"{item.get('hostedUrl', '')}/apply",
                    location=location,
                    description=item.get("descriptionPlain") or item.get("description", ""),
                    posted_at=parse_epoch_ms(item.get("createdAt")),
                    remote=workplace.lower() == "remote" or looks_remote(location, title),
                    raw={"id": item.get("id"), "team": categories.get("team", "")},
                )
            )
        return jobs
