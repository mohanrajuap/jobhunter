"""Work out where a career site keeps its job data.

The hard part of covering "all career pages" isn't parsing — it's *finding* the data.
A modern careers page is an empty shell that fetches its listings from somewhere, and
that somewhere is almost always a JSON endpoint. This module opens the page in a real
browser, watches what it fetches, and reports every candidate.

Four checks, cheapest first:

  1. **Known board** — Greenhouse/Lever/Ashby/Workable/SmartRecruiters/Recruitee links
     in the HTML. Best case: a supported adapter already handles it.
  2. **Workday** — its own detection, because the tenant/site pair lives in the URL and
     the JSON API is a fixed shape.
  3. **Network capture** — every XHR/fetch returning JSON that looks like job data.
     This is what finds the long tail: bespoke APIs and niche ATSs.
  4. **schema.org JobPosting** — JSON-LD markup, which sites add so Google Jobs can
     index them. Usually on individual job pages rather than the listing.

The output is a report you can act on, not a guess.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Words that suggest a JSON payload is a job listing rather than analytics or chat.
_JOB_HINTS = ("jobtitle", "job_title", "jobs", "positions", "openings", "requisition",
              "vacancies", "careers", "job_post", "jobposting", "departments")
# Endpoints that mention jobs but never contain any — trackers, chat widgets, CDNs.
_NOISE_HOSTS = ("google-analytics", "googleadservices", "googletagmanager", "doubleclick",
                "facebook", "hotjar", "segment.io", "sentry", "zoho.in/api/v1/webchat",
                "convokraft", "salesforce-scrt", "acsbapp", "intercom", "clarity.ms",
                "cdn.cookielaw", "onetrust", "lottie")


@dataclass
class Finding:
    kind: str          # "board" | "workday" | "api" | "jsonld"
    detail: str
    confidence: str    # "high" | "medium" | "low"
    config_hint: str = ""
    sample: str = ""


@dataclass
class ProbeReport:
    url: str
    findings: list[Finding] = field(default_factory=list)
    endpoints: list[tuple[str, int]] = field(default_factory=list)
    error: str = ""

    @property
    def best(self) -> Finding | None:
        for level in ("high", "medium", "low"):
            for finding in self.findings:
                if finding.confidence == level:
                    return finding
        return None

    def to_text(self) -> str:
        lines = [f"Career site probe — {self.url}", "=" * 68, ""]
        if self.error:
            lines += [f"Could not load the page: {self.error}", ""]
        if not self.findings:
            lines += [
                "No job data source found.",
                "",
                "That usually means one of:",
                "  • the listing lives on a separate site — look for a 'View openings'",
                "    link and probe that URL instead",
                "  • the page needs a login",
                "  • it renders jobs server-side, in which case add the page under",
                "    Career Pages with a CSS selector for its job links",
            ]
            return "\n".join(lines)

        for finding in self.findings:
            lines.append(f"[{finding.confidence.upper():6}] {finding.kind}: {finding.detail}")
            if finding.sample:
                lines.append(f"          e.g. {finding.sample}")
            if finding.config_hint:
                lines.append("")
                lines.append("  Add this to your config:")
                lines += [f"    {line}" for line in finding.config_hint.splitlines()]
            lines.append("")

        if self.endpoints:
            lines += ["Other JSON the page fetched (largest first):", ""]
            for url, size in self.endpoints[:8]:
                lines.append(f"  {size:>8,}b  {url}")
        return "\n".join(lines)


def _is_noise(url: str) -> bool:
    lowered = url.lower()
    return any(host in lowered for host in _NOISE_HOSTS)


def _looks_like_jobs(body: str) -> bool:
    lowered = body[:20000].lower()
    return sum(hint in lowered for hint in _JOB_HINTS) >= 2


def extract_jsonld_jobs(html: str) -> list[dict]:
    """Pull schema.org JobPosting nodes out of a page."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []

    soup = BeautifulSoup(html, "html.parser")
    found: list[dict] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "{}")
        except Exception:
            continue
        for node in (data if isinstance(data, list) else [data]):
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph") if isinstance(node.get("@graph"), list) else [node]
            for entry in graph:
                if isinstance(entry, dict) and "JobPosting" in str(entry.get("@type", "")):
                    found.append(entry)
    return found


def probe_career_site(url: str, browser: Any, wait_ms: int = 6000) -> ProbeReport:
    """Load `url` and report every way its job data could be read."""
    from .sources.career_page import sniff_ats
    from .sources.workday import discover_from_html

    report = ProbeReport(url=url)
    captured: list[tuple[str, int, str]] = []
    page = None

    try:
        page = browser.new_page()

        def on_response(response: Any) -> None:
            try:
                if response.request.resource_type not in ("xhr", "fetch"):
                    return
                if "json" not in (response.headers.get("content-type") or ""):
                    return
                if _is_noise(response.url):
                    return
                body = response.text()
                if len(body) < 300 or not _looks_like_jobs(body):
                    return
                captured.append((response.url, len(body), body[:400]))
            except Exception:
                pass

        page.on("response", on_response)
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(wait_ms)
        html = page.content()
    except Exception as exc:
        report.error = f"{type(exc).__name__}: {exc}"
        html = ""
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass

    # 1. A board we already support.
    if html and (detected := sniff_ats(html)):
        ats, slug = detected
        key = "boards" if ats == "greenhouse" else "companies"
        report.findings.append(Finding(
            kind="board", confidence="high",
            detail=f"backed by {ats} (slug '{slug}') — already supported",
            config_hint=f"sources:\n  {ats}:\n    enabled: true\n    {key}:\n      - {slug}",
        ))

    # 2. Workday.
    if html and (found := discover_from_html(html)):
        tenant, wd, site = found
        report.findings.append(Finding(
            kind="workday", confidence="high",
            detail=f"Workday tenant '{tenant}' ({wd}), site '{site}' — already supported",
            config_hint=(
                "sources:\n  workday:\n    enabled: true\n    companies:\n"
                f"      - name: {tenant.title()}\n        tenant: {tenant}\n"
                f"        wd: {wd}\n        site: {site}"
            ),
        ))

    # 3. Whatever the page fetched for itself.
    captured.sort(key=lambda item: -item[1])
    report.endpoints = [(url_, size) for url_, size, _ in captured]
    for endpoint, size, sample in captured[:3]:
        report.findings.append(Finding(
            kind="api", confidence="medium",
            detail=f"the page loads its jobs from a JSON endpoint ({size:,} bytes)",
            sample=endpoint[:150],
            config_hint=(
                "# No adapter for this one yet. The endpoint above returns the job data\n"
                "# directly — open it in a browser to confirm, then ask for an adapter."
            ),
        ))

    # 4. Structured markup.
    if html:
        postings = extract_jsonld_jobs(html)
        if postings:
            titles = ", ".join(str(p.get("title", "?"))[:40] for p in postings[:3])
            report.findings.append(Finding(
                kind="jsonld", confidence="high",
                detail=f"{len(postings)} schema.org JobPosting block(s) in the HTML",
                sample=titles,
                config_hint=(
                    "sources:\n  custom_career_pages:\n"
                    f"    - name: \"\"\n      url: \"{url}\"\n      parser: jsonld"
                ),
            ))

    return report
