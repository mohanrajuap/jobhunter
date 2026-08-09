"""Applier contract and the shared context every applier needs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..browser import BrowserSession
from ..models import Job, Outcome, Status
from .form_filler import Profile


@dataclass
class ApplyContext:
    """Everything an applier needs that isn't the job itself."""

    profile: Profile
    resume_path: Path
    browser: BrowserSession
    cover_letter_path: Path | None = None
    dry_run: bool = True
    submit_timeout_ms: int = 20_000
    screenshot_on_failure: bool = True
    # "auto"   — fill the form and submit it
    # "manual" — fill the form, leave it open in the browser, you review and submit
    mode: str = "auto"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def manual_review(self) -> bool:
        return self.mode == "manual"


class Applier(ABC):
    """Applies to one job. Must return an Outcome rather than raising."""

    name: str = "applier"
    handles: tuple[str, ...] = ()

    def can_handle(self, job: Job) -> bool:
        return job.ats in self.handles

    @abstractmethod
    def apply(self, job: Job, ctx: ApplyContext) -> Outcome:
        ...

    # --- shared helpers ---

    @staticmethod
    def manual(job: Job, reason: str, screenshot: str = "") -> Outcome:
        return Outcome(job=job, status=Status.MANUAL, reason=reason, screenshot=screenshot)

    @staticmethod
    def failed(job: Job, reason: str, screenshot: str = "") -> Outcome:
        return Outcome(job=job, status=Status.FAILED, reason=reason, screenshot=screenshot)

    @staticmethod
    def applied(job: Job, reason: str = "") -> Outcome:
        return Outcome(job=job, status=Status.APPLIED, reason=reason)

    @staticmethod
    def dry_run(job: Job, reason: str) -> Outcome:
        return Outcome(job=job, status=Status.DRY_RUN, reason=reason)
