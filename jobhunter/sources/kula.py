"""Kula.ai job boards.

Kula is a recruiting platform used by a number of Indian startups (Cashfree among
them). Their careers pages render client-side and expose no board slug, so neither
sniffing nor scraping found anything — but the page calls a public JSON endpoint that
returns the full listing, descriptions included:

    https://careers.kula.ai/api/internal/ats_job_posts?accountName={account}&page=1&items=99

The account name is the slug in the company's Kula careers URL, and
`jobhunter probe <careers-url>` will surface it for you.

One caveat: Kula publishes no posting date, so these jobs have an unknown age. The
Scorer treats that as neutral rather than stale, but a "posted within N days" filter
cannot narrow them.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import Job
from .base import Source, looks_remote, strip_html

log = logging.getLogger(__name__)

API = "https://careers.kula.ai/api/internal/ats_job_posts?accountName={account}&page={page}&type=ats_job_post.index&items=99"
JOB_URL = "https://careers.kula.ai/{account}/{job_id}"


class KulaSource(Source):
    name = "kula"

    def __init__(self, config: dict[str, Any], session: Any = None, **_: Any):
        super().__init__(config, session)
        self.max_pages = int(self.config.get("max_pages", 5))

    def accounts(self) -> list[dict]:
        out = []
        for entry in self.config.get("companies", []) or []:
            if isinstance(entry, str):
                out.append({"account": entry, "name": entry.replace("-", " ").title()})
            elif entry.get("account"):
                out.append({"account": entry["account"],
                            "name": entry.get("name") or entry["account"].title()})
        return out

    def fetch(self, queries: list[str]) -> list[Job]:  # noqa: ARG002 - Scorer filters
        jobs: list[Job] = []
        for board in self.accounts():
            if self.cancelled:
                log.info("kula: stopped")
                break
            try:
                found = self.fetch_account(board["account"], board["name"])
                log.info("kula: %s -> %d jobs", board["name"], len(found))
                jobs.extend(found)
                self._emit(found)
            except Exception as exc:
                log.warning("kula: %s failed — %s", board["name"], exc)
            self._sleep()
        return jobs

    def fetch_account(self, account: str, company: str) -> list[Job]:
        jobs: list[Job] = []
        page = 1
        while page <= self.max_pages:
            if self.cancelled:
                break
            payload = self._get_json(API.format(account=account, page=page))
            items = payload.get("data") or []
            if not items:
                break

            for item in items:
                job = self._to_job(account, company, item)
                if job:
                    jobs.append(job)

            meta = payload.get("meta") or {}
            if page >= int(meta.get("pages") or 1):
                break
            page += 1
            self._sleep()
        return jobs

    def _to_job(self, account: str, company: str, item: dict) -> Job | None:
        if not item.get("listed", True) or item.get("is_confidential"):
            return None

        details = item.get("ats_job") or {}
        offices = details.get("offices") or []
        location = ", ".join(
            o.get("location", "") for o in offices if o.get("location")
        ) or ""
        workplace = str(details.get("workplace") or "")
        remote = workplace.lower() == "remote" or any(o.get("remote") for o in offices)

        return Job(
            source=self.name,
            ats="kula",
            company=company,
            title=(item.get("title") or "").strip(),
            url=JOB_URL.format(account=account, job_id=item.get("id")),
            apply_url=JOB_URL.format(account=account, job_id=item.get("id")),
            location=location,
            description=strip_html(details.get("job_description", "")),
            # Kula exposes no posting date; leaving it None is honest, and the Scorer
            # treats unknown age as neutral rather than stale.
            posted_at=None,
            remote=remote or looks_remote(location, item.get("title", "")),
            raw={"id": item.get("id"), "account": account,
                 "department": (details.get("ats_department") or {}).get("name", ""),
                 "employment_type": details.get("employment_type", "")},
        )
