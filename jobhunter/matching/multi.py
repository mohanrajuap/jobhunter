"""Scoring across several role targets at once.

Each job is scored against every enabled role. The winning role determines the match
score *and* which resume gets uploaded — so a job that reads as "SRE" is applied to
with your SRE resume even when the DevOps role also matched it.
"""

from __future__ import annotations

import logging

from ..config import Config
from ..models import Job, MatchResult
from ..roles import RoleTarget
from .scorer import Scorer

log = logging.getLogger(__name__)


class MultiRoleScorer:
    def __init__(self, config: Config, roles: list[RoleTarget], feedback: dict | None = None):
        self.cfg = config
        self.roles = roles
        self.feedback = feedback or {}
        self.scorers: list[tuple[RoleTarget, Scorer]] = [
            (role, Scorer(config, role.profile, overrides=role.overrides, feedback=self.feedback))
            for role in roles
        ]
        if not self.scorers:
            log.warning("No roles configured — nothing can match")
        if self.feedback.get("total"):
            log.info(
                "Applying your feedback from %d rejected job(s): %d companies, %d title terms",
                self.feedback.get("total", 0),
                len(self.feedback.get("companies", {})),
                len(self.feedback.get("title_terms", {})),
            )

    def score(self, job: Job) -> MatchResult:
        """Best result across all roles. If none pass, return the closest miss so the
        rejection reason shown to the user is the informative one."""
        best_pass: MatchResult | None = None
        best_fail: MatchResult | None = None

        for role, scorer in self.scorers:
            result = scorer.score(job)
            result.role_name = role.name

            if result.passed:
                if best_pass is None or result.score > best_pass.score:
                    self._attach_resume(result, role, job)
                    best_pass = result
            elif best_fail is None or result.score > best_fail.score:
                best_fail = result

        if best_pass:
            return best_pass
        return best_fail or MatchResult(job=job, score=0.0, rejected_because="no roles configured")

    def _attach_resume(self, result: MatchResult, role: RoleTarget, job: Job) -> None:
        variant = role.best_resume(job)
        if variant is None:
            result.rejected_because = f"role '{role.name}' has no usable resume"
            return
        result.resume_path = str(variant.path)
        result.resume_label = variant.label

    def rank(self, jobs: list[Job]) -> list[MatchResult]:
        results = [self.score(job) for job in jobs]
        for r in results:
            if not r.passed:
                log.debug("skip %s @ %s — %s", r.job.title, r.job.company, r.rejected_because)

        passed = [r for r in results if r.passed]
        passed.sort(key=lambda r: -r.score)

        by_role: dict[str, int] = {}
        for r in passed:
            by_role[r.role_name] = by_role.get(r.role_name, 0) + 1
        breakdown = ", ".join(f"{name}: {n}" for name, n in sorted(by_role.items())) or "none"
        log.info("Matched %d of %d discovered jobs (%s)", len(passed), len(results), breakdown)
        return passed

    def score_all(self, jobs: list[Job]) -> list[MatchResult]:
        """Every result, passing or not — the GUI shows rejections too."""
        return [self.score(job) for job in jobs]
