"""Workday job boards.

Workday powers a large share of enterprise hiring — including most Indian GCCs — and
until now those companies were invisible to this tool: their careers pages render in
JavaScript and expose no board slug, so both sniffing and scraping came back empty.

They do, however, serve their own listings from a documented-by-observation JSON
endpoint, the same one the careers page itself calls:

    POST https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
         {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}

Three values identify a board — tenant, the `wdN` shard, and the site name — and all
three are in the careers-page URL. `jobhunter probe <careers-url>` reads them off for
you and prints the config block to paste in.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..models import Job
from .base import Source, looks_remote, strip_html

log = logging.getLogger(__name__)

LIST_API = "https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
DETAIL_API = "https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}"
JOB_URL = "https://{tenant}.{wd}.myworkdayjobs.com/{site}{path}"

# Any Workday careers URL contains everything needed to call the API.
WORKDAY_URL_RE = re.compile(
    r"https://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)",
    re.I,
)

# Workday reports age as prose: "Posted 3 Days Ago", "Posted Today", "Posted 30+ Days Ago".
_POSTED_RE = re.compile(r"(\d+)\+?\s*(day|week|month)s?\s*ago", re.I)
_PAGE_SIZE = 20


def discover_from_html(html: str) -> tuple[str, str, str] | None:
    """Pull (tenant, wd, site) out of any page that links to a Workday board."""
    match = WORKDAY_URL_RE.search(html or "")
    if not match:
        return None
    tenant, wd, site = match.groups()
    # The CXS path segment is never a locale or a job path.
    if site.lower() in ("job", "jobs", "login", "wday"):
        return None
    return tenant, wd, site


def _parse_posted(text: str) -> datetime | None:
    if not text:
        return None
    lowered = text.lower()
    now = datetime.now(timezone.utc)
    if "today" in lowered or "just posted" in lowered:
        return now
    if "yesterday" in lowered:
        return now - timedelta(days=1)
    match = _POSTED_RE.search(lowered)
    if not match:
        return None
    amount, unit = int(match.group(1)), match.group(2)
    days = {"day": 1, "week": 7, "month": 30}[unit] * amount
    return now - timedelta(days=days)


class WorkdaySource(Source):
    name = "workday"

    def __init__(self, config: dict[str, Any], session: Any = None, **_: Any):
        super().__init__(config, session)
        self.max_per_company = int(self.config.get("max_postings_per_company", 200))
        self.detail_limit = int(self.config.get("detail_limit", 25))
        self.polite_delay = float(self.config.get("delay_seconds", 1.0))

    def boards(self) -> list[dict]:
        """Each entry needs tenant/wd/site, or a careers URL to read them from."""
        out: list[dict] = []
        for entry in self.config.get("companies", []) or []:
            if isinstance(entry, str):
                found = discover_from_html(entry)
                if found:
                    out.append({"name": found[0].title(), "tenant": found[0],
                                "wd": found[1], "site": found[2]})
                else:
                    log.warning("workday: could not read tenant/site from '%s'", entry)
                continue

            if entry.get("tenant") and entry.get("site"):
                out.append({**entry, "wd": entry.get("wd", "wd1")})
            elif entry.get("url") and (found := discover_from_html(entry["url"])):
                out.append({"name": entry.get("name") or found[0].title(),
                            "tenant": found[0], "wd": found[1], "site": found[2]})
            else:
                log.warning("workday: entry %s has no tenant/site and no usable url", entry)
        return out

    def fetch(self, queries: list[str]) -> list[Job]:  # noqa: ARG002 - Scorer filters
        jobs: list[Job] = []
        for board in self.boards():
            if self.cancelled:
                log.info("workday: stopped")
                break
            try:
                found = self.fetch_board(board)
                log.info("workday: %s -> %d jobs", board.get("name", board["tenant"]), len(found))
                jobs.extend(found)
                self._emit(found)
            except Exception as exc:
                log.warning("workday: %s failed — %s", board.get("name", board.get("tenant")), exc)
            self._sleep()
        return jobs

    def fetch_board(self, board: dict) -> list[Job]:
        tenant, wd, site = board["tenant"], board.get("wd", "wd1"), board["site"]
        company = board.get("name") or tenant.replace("-", " ").title()
        url = LIST_API.format(tenant=tenant, wd=wd, site=site)

        jobs: list[Job] = []
        offset = 0
        while offset < self.max_per_company:
            if self.cancelled:
                break
            payload = self._post_json(
                url, {"appliedFacets": {}, "limit": _PAGE_SIZE, "offset": offset, "searchText": ""}
            )
            postings = payload.get("jobPostings") or []
            if not postings:
                break

            for item in postings:
                jobs.append(self._to_job(board, company, item))

            offset += len(postings)
            if offset >= int(payload.get("total") or 0):
                break
            self._sleep()

        # Descriptions are a separate call, so only enrich the first N.
        for job in jobs[: self.detail_limit]:
            if self.cancelled:
                break
            try:
                self._enrich(board, job)
            except Exception as exc:
                log.debug("workday: detail failed for %s — %s", job.title, exc)
            self._sleep()

        return jobs

    def _post_json(self, url: str, body: dict) -> dict:
        timeout = getattr(self.session, "request_timeout", 20)
        response = self.session.post(
            url, json=body, timeout=timeout,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        response.raise_for_status()
        return response.json()

    def _to_job(self, board: dict, company: str, item: dict) -> Job:
        tenant, wd, site = board["tenant"], board.get("wd", "wd1"), board["site"]
        path = item.get("externalPath", "")
        location = item.get("locationsText", "") or ""

        return Job(
            source=self.name,
            ats="workday",
            company=company,
            title=(item.get("title") or "").strip(),
            url=JOB_URL.format(tenant=tenant, wd=wd, site=site, path=path),
            apply_url=JOB_URL.format(tenant=tenant, wd=wd, site=site, path=path),
            location=location,
            posted_at=_parse_posted(item.get("postedOn", "")),
            remote=looks_remote(location, item.get("title", "")),
            raw={"path": path, "req_id": (item.get("bulletFields") or [""])[0],
                 "tenant": tenant, "wd": wd, "site": site},
        )

    def _enrich(self, board: dict, job: Job) -> None:
        url = DETAIL_API.format(
            tenant=board["tenant"], wd=board.get("wd", "wd1"),
            site=board["site"], path=job.raw.get("path", ""),
        )
        timeout = getattr(self.session, "request_timeout", 20)
        response = self.session.get(url, timeout=timeout, headers={"Accept": "application/json"})
        response.raise_for_status()
        info = (response.json() or {}).get("jobPostingInfo") or {}

        if info.get("jobDescription"):
            job.description = strip_html(info["jobDescription"])
        if info.get("location"):
            job.location = info["location"]
        if info.get("timeType"):
            job.raw["time_type"] = info["timeType"]
        if info.get("remoteType"):
            job.remote = "remote" in str(info["remoteType"]).lower()
