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
import queue
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import notify
from .appliers import ApplyContext, Profile, get_applier
from .browser import BrowserSession, BrowserUnavailable, browser_from_config
from .config import Config
from .matching import MultiRoleScorer
from .models import Job, MatchResult, Outcome, RunReport, Status
from .roles import RoleTarget, load_roles
from .sources import Source, build_sources
from .store import Store

log = logging.getLogger(__name__)

ProgressFn = Callable[[str], None]


class Pipeline:
    def __init__(self, config: Config, store: Store | None = None, progress: ProgressFn | None = None):
        self.cfg = config
        self.store = store or Store(config.data_dir() / "jobhunter.sqlite3")
        self._progress = progress or (lambda _msg: None)
        self._oracle: Any = None
        self._oracle_tried = False

    @property
    def oracle(self) -> Any:
        """Lazily connected Oracle mirror, or None. Connecting once per pipeline keeps
        a dead database from stalling every single write."""
        if not self._oracle_tried:
            self._oracle_tried = True
            from .db import build_sink

            self._oracle = build_sink(self.cfg)
            if self._oracle:
                self._say(f"Oracle mirror connected ({self._oracle.count()} rows on record)")
        return self._oracle

    def _mirror(self, outcome: Outcome, match: MatchResult | None = None, mode: str = "") -> None:
        sink = self.oracle
        if sink is not None:
            sink.record(outcome, match, mode)

    def record_manual_application(self, job: Job, note: str = "") -> Outcome:
        """Mark a job you applied to yourself — from the UI's 'Mark applied' button."""
        outcome = self.store.mark_applied_manually(job, note)
        self._mirror(outcome, None, "manual-by-user")
        return outcome

    def record_irrelevant(self, job: Job, reason: str = "") -> Outcome:
        """Mark a job as not what you want. Feeds back into future scoring."""
        outcome = self.store.mark_irrelevant(job, reason)
        self._mirror(outcome, None, "feedback")
        return outcome

    def _say(self, message: str) -> None:
        log.info(message)
        self._progress(message)

    # --- phase 1: roles & queries ---

    def load_roles(self, only_roles: list[str] | None = None) -> list[RoleTarget]:
        """Load role targets, optionally narrowed to specific ones by name.

        Narrowing is what the UI's Role picker does: searching one role uses only its
        titles as queries and only its resumes, so results aren't diluted by the others.
        """
        roles = load_roles(self.cfg, only_names=only_roles)
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

    def discover(
        self,
        queries: list[str],
        browser: BrowserSession | None,
        only: list[str] | None = None,
    ) -> tuple[list[Job], dict[str, str]]:
        jobs: list[Job] = []
        errors: dict[str, str] = {}

        self._run_sources(
            build_sources(self.cfg, browser=browser, only=only),
            queries,
            handle=lambda found: jobs.extend(found),
            errors=errors,
        )

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
        if not jobs:
            return []
        # One batch upsert plus two bulk reads instead of three queries per job.
        self.store.record_jobs(jobs)
        fingerprints = [job.fingerprint for job in jobs]
        applied = self.store.has_applied_bulk(fingerprints)
        seen = (
            self.store.seen_recently_bulk(fingerprints, recheck_days)
            if recheck_days > 0
            else set()
        )
        fresh = [j for j in jobs if j.fingerprint not in applied and j.fingerprint not in seen]
        self._say(f"{len(fresh)} jobs are new (rest already seen or applied to)")
        return fresh

    def discover_and_match(
        self,
        browser: BrowserSession | None = None,
        include_seen: bool = False,
        only: list[str] | None = None,
        on_batch: Callable[[list[MatchResult]], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        only_roles: list[str] | None = None,
    ) -> tuple[list[MatchResult], dict[str, str]]:
        """Search and score without applying. This is what the GUI's Search button runs.

        Scoring happens incrementally, one chunk at a time, and `on_batch` is called with
        each set of new matches — so the UI fills in while a slow source is still going
        rather than staying blank until everything finishes.

        `include_seen` keeps already-applied jobs so the GUI can label them rather than
        hide them. `only` restricts which sources run, `only_roles` which roles.
        """
        roles = self.load_roles(only_roles)
        queries = self.build_queries(roles)
        scorer = MultiRoleScorer(self.cfg, roles, feedback=self.store.feedback_signals())
        cancel = should_cancel or (lambda: False)

        recheck_days = int(self.cfg.get("apply.recheck_after_days", 30))
        processed: set[str] = set()
        matches: list[MatchResult] = []
        seen_count = 0
        rejections: dict[str, int] = {}
        near_miss: list = [0.0, None]  # [best score, its MatchResult]

        def handle(found: list[Job]) -> list[MatchResult]:
            """Score one chunk. Streaming means we keep the first version of a duplicate
            rather than the most detailed one — a fair trade for live results."""
            nonlocal seen_count
            candidates: list[Job] = []
            for job in found:
                if not job.title or not job.company:
                    continue
                if job.fingerprint in processed:
                    continue
                processed.add(job.fingerprint)
                candidates.append(job)

            if not candidates:
                return []

            # One batch upsert plus two bulk reads instead of three queries per job.
            self.store.record_jobs(candidates)
            if include_seen:
                fresh = candidates
            else:
                fingerprints = [job.fingerprint for job in candidates]
                applied = self.store.has_applied_bulk(fingerprints)
                seen = (
                    self.store.seen_recently_bulk(fingerprints, recheck_days)
                    if recheck_days > 0
                    else set()
                )
                fresh = [
                    j for j in candidates
                    if j.fingerprint not in applied and j.fingerprint not in seen
                ]
                seen_count += len(candidates) - len(fresh)

            if not fresh:
                return []

            scored = scorer.score_all(fresh)
            batch = sorted([r for r in scored if r.passed], key=lambda r: -r.score)

            # Remember why the rest were dropped — "0 matched" with no explanation is
            # the single most useless thing this tool can say.
            for result in scored:
                if not result.passed and result.rejected_because:
                    reason = re.sub(r"\d+(\.\d+)?", "N", result.rejected_because)
                    rejections[reason[:70]] = rejections.get(reason[:70], 0) + 1
                    if result.score > near_miss[0]:
                        near_miss[0] = result.score
                        near_miss[1] = result

            if batch:
                matches.extend(batch)
                if on_batch:
                    on_batch(batch)
            return batch

        errors: dict[str, str] = {}
        sources = build_sources(
            self.cfg, browser=browser, only=only, should_cancel=cancel
        )
        self._run_sources(sources, queries, handle, errors)

        self._say(
            f"{len(processed)} jobs seen, {len(matches)} matched"
            + (f" ({seen_count} already handled)" if seen_count else "")
        )
        self._explain_rejections(rejections, near_miss, found_any=bool(matches))
        return matches, errors

    # --- running sources: HTTP boards in parallel, browser-backed ones serially ---

    def _run_sources(
        self,
        sources: list[Source],
        queries: list[str],
        handle: Callable[[list[Job]], None],
        errors: dict[str, str],
    ) -> None:
        """Run every source, session-backed boards in parallel, browser-backed serially.

        The board APIs, Workday and Kula are independent HTTP calls, so running them
        concurrently turns discovery wall time from the *sum* of all sources into
        roughly the slowest one. Browser-backed sources (Naukri, career pages) share a
        single Playwright session, which is not thread-safe, so they run afterwards,
        one at a time.

        `handle` is only ever invoked from a single consumer thread, so the pipeline's
        shared state — dedupe set, match list, counters — needs no locks of its own.
        """
        serial = [s for s in sources if getattr(s, "browser", None) is not None]
        parallel = [s for s in sources if getattr(s, "browser", None) is None]

        if len(parallel) > 1:
            self._say(f"Searching {len(parallel)} sources in parallel…")
            self._run_parallel(parallel, queries, handle, errors)
        else:
            self._run_serial(parallel, queries, handle, errors)
        self._run_serial(serial, queries, handle, errors)

    def _run_serial(
        self,
        sources: list[Source],
        queries: list[str],
        handle: Callable[[list[Job]], None],
        errors: dict[str, str],
    ) -> None:
        for source in sources:
            emitted = [False]

            def _on_jobs(found: list[Job]) -> None:
                emitted[0] = True
                handle(found)

            source.on_jobs = _on_jobs  # stream mid-fetch chunks straight into the pipeline
            try:
                self._say(f"Searching {source.name}…")
                found = source.fetch(queries)
                # A source that emitted has already fed every returned job through
                # handle as chunks; sweeping the return value too would double-count.
                if not emitted[0]:
                    handle(found)
                self._say(f"{source.name}: {len(found)} jobs")
            except Exception as exc:
                log.error("source '%s' failed entirely: %s", source.name, exc)
                errors[source.name] = str(exc)

    def _run_parallel(
        self,
        sources: list[Source],
        queries: list[str],
        handle: Callable[[list[Job]], None],
        errors: dict[str, str],
    ) -> None:
        """Fetch HTTP sources in a worker pool; one consumer thread scores their output.

        Sources emit batches as they go, so the queue streams results to the consumer
        continuously instead of waiting for the slowest source to finish. Only sources
        that never emitted get a final sweep of their return value — emitting sources
        have already queued every returned job as chunks.
        """
        batches: queue.Queue = queue.Queue()
        emitted: dict[Source, bool] = {source: False for source in sources}

        def _hook(source: Source) -> Callable[[list[Job]], None]:
            def _on_jobs(found: list[Job]) -> None:
                emitted[source] = True
                batches.put(found)

            return _on_jobs

        for source in sources:
            source.on_jobs = _hook(source)

        max_workers = min(len(sources), max(1, int(self.cfg.get("http.max_workers", 4))))
        stop = threading.Event()

        def consume() -> None:
            while not stop.is_set() or not batches.empty():
                try:
                    found = batches.get(timeout=0.2)
                except queue.Empty:
                    continue
                try:
                    handle(found)
                except Exception as exc:
                    log.error("failed to score a batch from a parallel source: %s", exc)

        consumer = threading.Thread(target=consume, daemon=True)
        consumer.start()
        try:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(source.fetch, queries): source for source in sources}
                for future in as_completed(futures):
                    source = futures[future]
                    try:
                        found = future.result()
                        if not emitted[source]:
                            batches.put(found)
                        self._say(f"{source.name}: {len(found)} jobs")
                    except Exception as exc:
                        log.error("source '%s' failed entirely: %s", source.name, exc)
                        errors[source.name] = str(exc)
        finally:
            stop.set()
            consumer.join(timeout=10)
            # The pool has exited so nothing is still producing; whatever the consumer
            # didn't get to before exiting is processed here rather than dropped.
            while True:
                try:
                    handle(batches.get_nowait())
                except queue.Empty:
                    break

    def _explain_rejections(
        self, rejections: dict[str, int], near_miss: list, found_any: bool
    ) -> None:
        """Say why jobs were filtered out, and what to change to loosen it."""
        if not rejections:
            return

        top = sorted(rejections.items(), key=lambda kv: -kv[1])
        headline = "Why the rest were filtered out:" if found_any else "Nothing matched. Why:"
        self._say(headline)
        for reason, count in top[:6]:
            self._say(f"   {count:4} x  {reason}")

        best_score, best = near_miss[0], near_miss[1]
        if best is not None:
            self._say(
                f"   closest miss: {best.score:.0%} — {best.job.title[:50]} "
                f"({best.job.company[:24]})"
            )

        # Point at the setting most likely responsible.
        biggest = top[0][0].lower()
        if "below threshold" in biggest:
            threshold = self.cfg.get("search.min_score", 0.5)
            hint = (
                f"lower search.min_score (currently {threshold}) — the closest was "
                f"{best_score:.0%}" if best_score else f"lower search.min_score ({threshold})"
            )
        elif "location" in biggest:
            hint = "widen Locations on the Search tab, or clear it to allow anywhere"
        elif "days ago" in biggest or "posted" in biggest:
            hint = "set 'Posted within' to a longer window"
        elif "years" in biggest:
            hint = "adjust search.min_experience_years / max_experience_years"
        elif "excluded keyword" in biggest:
            hint = "remove that word from search.exclude_keywords"
        elif "title" in biggest:
            hint = "add more job-title variants to the role"
        else:
            hint = "run `jobhunter discover --show-rejected` for the full breakdown"
        self._say(f"   → try: {hint}")

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
            mode=str(self.cfg.get("apply.mode", "auto")),
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
            self._mirror(outcome, match, ctx.mode)
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
        self._mirror(outcome, match, ctx.mode)
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

        # One grouped query instead of a lookup per candidate application.
        today_by_company = self.store.applications_today_by_company()

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
            used = per_company.get(company_key, 0) + today_by_company.get(company_key, 0)
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
