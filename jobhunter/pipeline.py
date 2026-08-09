"""The daily run: discover → de-duplicate → match → apply → notify.

One `Pipeline.run()` call is a complete morning's work. Everything it does is
recorded in SQLite, so tomorrow's run knows what today's already handled.

The pipeline is also what the GUI drives — `discover_and_match()` and `apply_one()`
are deliberately callable on their own so the window can search, show results, and
apply to a user-picked subset.
"""

from __future__ import annotations

import dataclasses
import logging
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import notify
from .appliers import ApplyContext, Profile, get_applier
from .browser import BrowserSession, BrowserUnavailable, browser_from_config
from .config import Config
from .matching import MultiRoleScorer
from .models import Job, MatchResult, Outcome, RunReport, Status
from .roles import RoleTarget, load_roles
from .sources import build_sources
from .store import Store

log = logging.getLogger(__name__)

ProgressFn = Callable[[str], None]


class Pipeline:
    def __init__(self, config: Config, store: Store | None = None, progress: ProgressFn | None = None):
        self.cfg = config
        self.store = store or Store(config.data_dir() / "jobhunter.sqlite3")
        self._progress = progress or (lambda _msg: None)

    def _say(self, message: str) -> None:
        log.info(message)
        self._progress(message)

    # --- phase 1: roles & queries ---

    def load_roles(self) -> list[RoleTarget]:
        roles = load_roles(self.cfg)
        for role in roles:
            self._say(f"Role '{role.name}' — {len(role.resumes)} resume(s), "
                      f"{len(role.profile.keywords)} keywords")
        return roles

    def build_queries(self, roles: list[RoleTarget]) -> list[str]:
        """Keyword-search sources (Naukri) need query strings; board APIs ignore these."""
        queries: list[str] = []
        per_role = int(self.cfg.get("search.max_queries_per_role", 4))

        for role in roles:
            for title in role.titles[:per_role]:
                if title and title not in queries:
                    queries.append(title)

        if not queries:
            for role in roles:
                queries.extend(role.profile.top_keywords[:3])

        log.info("Search queries: %s", "; ".join(queries) or "(none)")
        return queries

    # --- phase 2: discovery ---

    def discover(self, queries: list[str], browser: BrowserSession | None) -> tuple[list[Job], dict[str, str]]:
        jobs: list[Job] = []
        errors: dict[str, str] = {}

        for source in build_sources(self.cfg, browser=browser):
            try:
                self._say(f"Searching {source.name}…")
                found = source.fetch(queries)
                jobs.extend(found)
                self._say(f"{source.name}: {len(found)} jobs")
            except Exception as exc:
                log.error("source '%s' failed entirely: %s", source.name, exc)
                errors[source.name] = str(exc)

        unique = self._dedupe(jobs)
        self._say(f"Discovered {len(jobs)} jobs ({len(unique)} after de-duplication)")
        return unique, errors

    def _dedupe(self, jobs: list[Job]) -> list[Job]:
        """Collapse duplicates by fingerprint, keeping the entry with the most detail."""
        best: dict[str, Job] = {}
        for job in jobs:
            if not job.title or not job.company:
                continue
            existing = best.get(job.fingerprint)
            if existing is None or len(job.description) > len(existing.description):
                best[job.fingerprint] = job
        return list(best.values())

    def _filter_seen(self, jobs: list[Job]) -> list[Job]:
        recheck_days = int(self.cfg.get("apply.recheck_after_days", 30))
        fresh = []
        for job in jobs:
            self.store.record_job(job)
            if self.store.has_applied(job):
                continue
            if recheck_days > 0 and self.store.seen_recently(job, recheck_days):
                continue
            fresh.append(job)
        self._say(f"{len(fresh)} jobs are new (rest already seen or applied to)")
        return fresh

    def discover_and_match(
        self, browser: BrowserSession | None = None, include_seen: bool = False
    ) -> tuple[list[MatchResult], dict[str, str]]:
        """Search and score without applying. This is what the GUI's Search button runs.

        `include_seen` keeps already-applied jobs in the list so the GUI can label them
        rather than hiding them.
        """
        roles = self.load_roles()
        queries = self.build_queries(roles)
        scorer = MultiRoleScorer(self.cfg, roles)

        jobs, errors = self.discover(queries, browser)
        candidates = jobs if include_seen else self._filter_seen(jobs)
        for job in jobs:
            self.store.record_job(job)

        matches = scorer.rank(candidates)
        self._say(f"{len(matches)} jobs matched your roles")
        return matches, errors

    # --- phase 3: apply ---

    def _base_context(self, browser: BrowserSession, dry_run: bool) -> ApplyContext:
        answers = dict(self.cfg.get("profile.answers", {}) or {})
        cover = self.cfg.get("profile.cover_letter_path", "")

        return ApplyContext(
            profile=Profile(dict(self.cfg.profile), answers),
            resume_path=Path(self.cfg.get("profile.resume_path", "")).expanduser(),
            cover_letter_path=Path(cover).expanduser() if cover else None,
            browser=browser,
            dry_run=dry_run,
            submit_timeout_ms=int(self.cfg.get("apply.timeout_seconds", 30)) * 1000,
            screenshot_on_failure=bool(self.cfg.get("apply.screenshot_on_failure", True)),
        )

    def apply_one(self, match: MatchResult, ctx: ApplyContext) -> Outcome:
        """Apply to a single job with the resume its winning role selected."""
        applier = get_applier(match.job)
        if applier is None:
            outcome = Outcome(
                job=match.job, status=Status.MANUAL, score=match.score,
                reason=f"no applier supports ATS '{match.job.ats}'",
            )
            self.store.record_outcome(outcome)
            return outcome

        # Swap in the resume chosen for this job's role.
        job_ctx = ctx
        if match.resume_path:
            job_ctx = dataclasses.replace(ctx, resume_path=Path(match.resume_path))

        self._say(f"Applying: {match.job.title} @ {match.job.company} "
                  f"({match.score:.0%}, role '{match.role_name}', "
                  f"resume '{match.resume_label or 'default'}')")

        outcome = applier.apply(match.job, job_ctx)
        outcome.score = match.score
        self.store.record_outcome(outcome)
        self._say(f"  → {outcome.status.value}: {outcome.reason or 'ok'}")
        return outcome

    def apply_to(
        self, matches: list[MatchResult], browser: BrowserSession, dry_run: bool
    ) -> list[Outcome]:
        daily_cap = int(self.cfg.get("apply.max_applications_per_day", 25))
        company_cap = int(self.cfg.get("apply.max_per_company_per_day", 3))
        delay_range = self.cfg.get("apply.delay_seconds", [20, 45]) or [20, 45]
        ctx = self._base_context(browser, dry_run)

        already_today = self.store.applications_today()
        budget = max(daily_cap - already_today, 0)
        if budget == 0:
            self._say(f"Daily cap of {daily_cap} already reached ({already_today} applied today)")
            return []

        self._say(f"Applying to up to {min(budget, len(matches))} jobs (dry_run={dry_run})")

        outcomes: list[Outcome] = []
        per_company: dict[str, int] = {}
        submitted = 0

        for match in matches:
            if submitted >= budget:
                outcomes.append(Outcome(
                    job=match.job, status=Status.SKIPPED, score=match.score,
                    reason=f"daily cap of {daily_cap} applications reached",
                ))
                continue

            company_key = match.job.company.lower()
            used = per_company.get(company_key, 0) + self.store.applications_today_for_company(match.job.company)
            if used >= company_cap:
                outcomes.append(Outcome(
                    job=match.job, status=Status.SKIPPED, score=match.score,
                    reason=f"already at the per-company limit of {company_cap} today",
                ))
                continue

            outcome = self.apply_one(match, ctx)
            outcomes.append(outcome)

            if outcome.status in (Status.APPLIED, Status.DRY_RUN):
                submitted += 1
                per_company[company_key] = per_company.get(company_key, 0) + 1

            # Pace the run. Hammering an ATS gets the IP blocked and helps nobody.
            pause = random.uniform(float(delay_range[0]), float(delay_range[-1]))
            log.debug("waiting %.1fs before the next application", pause)
            time.sleep(pause)

        return outcomes

    # --- the whole thing ---

    def run(self, dry_run: bool | None = None, limit: int | None = None, apply: bool = True) -> RunReport:
        started = datetime.now(timezone.utc)
        is_dry = self.cfg.dry_run if dry_run is None else dry_run
        report = RunReport(started_at=started, dry_run=is_dry)

        if is_dry:
            log.warning("DRY RUN — forms will be filled but nothing will be submitted")

        needs_browser = apply or self.cfg.get("sources.naukri.enabled", False)
        browser_cm = browser_from_config(self.cfg) if needs_browser else _NullBrowser()

        try:
            with browser_cm as browser:
                matches, errors = self.discover_and_match(browser if needs_browser else None)
                report.source_errors = errors
                report.matched = len(matches)
                report.discovered = self.store.stats().get("jobs_seen", len(matches))

                if limit:
                    matches = matches[:limit]

                if not apply:
                    log.info("--no-apply set: stopping after matching")
                    report.outcomes = [
                        Outcome(job=m.job, status=Status.SKIPPED, score=m.score,
                                reason="discovery only (--no-apply)")
                        for m in matches
                    ]
                elif matches:
                    report.outcomes = self.apply_to(matches, browser, is_dry)
                else:
                    self._say("Nothing matched today.")
        except BrowserUnavailable as exc:
            log.error("%s", exc)
            report.source_errors["browser"] = str(exc)

        report.finished_at = datetime.now(timezone.utc)
        self.store.record_run(report)

        self._say(
            f"Run complete in {report.duration_seconds:.0f}s — {report.matched} matched, "
            f"{len(report.applied)} applied, {len(report.manual)} need manual work"
        )

        delivery = notify.send_all(self.cfg, report)
        log.info("Notifications: %s", ", ".join(f"{k}={v}" for k, v in delivery.items()))
        return report


class _NullBrowser:
    """Stands in for a browser session when discovery alone is requested."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> None:
        return None
