"""Workable job boards — https://apply.workable.com/api/v1/widget (public, no auth)."""

from __future__ import annotations

from ..models import Job
from .base import BoardSource, looks_remote, parse_iso, strip_html

API = "https://apply.workable.com/api/v1/widget/accounts/{company}?details=true"


class WorkableSource(BoardSource):
    name = "workable"

    def fetch_company(self, company: str) -> list[Job]:
        payload = self._get_json(API.format(company=company))
        account_name = payload.get("name") or company.replace("-", " ").title()
        jobs: list[Job] = []

        for item in payload.get("jobs", []):
            location = ", ".join(
                p for p in (item.get("city"), item.get("state"), item.get("country")) if p
            )
            description = strip_html(
                "\n".join(
                    filter(None, [item.get("description", ""), item.get("requirements", "")])
                )
            )

            jobs.append(
                Job(
                    source=self.name,
                    ats="workable",
                    company=account_name,
                    title=item.get("title", ""),
                    url=item.get("url") or item.get("shortlink", ""),
                    apply_url=item.get("application_url")
                    or f"{item.get('url', '')}/apply".replace("//apply", "/apply"),
                    location=location,
                    description=description,
                    posted_at=parse_iso(item.get("published_on") or item.get("created_at")),
                    remote=bool(item.get("telecommuting")) or looks_remote(location),
                    raw={"shortcode": item.get("shortcode"), "department": item.get("department", "")},
                )
            )
        return jobs
