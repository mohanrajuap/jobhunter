"""Core data types passed between discovery, matching, applying and notification."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


@dataclass
class Job:
    """A single job posting, normalised across every source."""

    source: str  # "naukri", "greenhouse", "lever", ...
    company: str
    title: str
    url: str
    ats: str = "unknown"  # which ATS the apply form lives on
    location: str = ""
    description: str = ""
    apply_url: str = ""
    posted_at: datetime | None = None
    remote: bool = False
    min_experience_years: float | None = None
    max_experience_years: float | None = None
    salary_text: str = ""
    # Applicant count, where the source publishes one. Board APIs never do, so this
    # stays None for most jobs — the UI shows a dash rather than a fake zero.
    applicants: int | None = None
    applicants_text: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        """Stable id used for de-duplication across sources and runs.

        Deliberately built from company+title+location rather than the URL, so the
        same role surfaced by both Naukri and the company's own board counts once.
        """
        key = f"{_norm(self.company)}|{_norm(self.title)}|{_norm(self.location)}"
        return hashlib.sha256(key.encode()).hexdigest()[:20]

    @property
    def target_url(self) -> str:
        return self.apply_url or self.url

    @property
    def search_text(self) -> str:
        return " ".join([self.title, self.company, self.location, self.description])

    @property
    def posted_display(self) -> str:
        """Human-readable age for the results grid."""
        age = self.age_days()
        if age is None:
            return "—"
        if age < 1:
            return "today"
        if age < 2:
            return "yesterday"
        if age < 14:
            return f"{int(age)}d ago"
        if age < 60:
            return f"{int(age / 7)}w ago"
        return f"{int(age / 30)}mo ago"

    @property
    def applicants_display(self) -> str:
        if self.applicants is not None:
            return str(self.applicants)
        return self.applicants_text or "—"

    def age_days(self) -> float | None:
        if not self.posted_at:
            return None
        posted = self.posted_at
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - posted).total_seconds() / 86400.0


@dataclass
class MatchResult:
    """Why a job did or didn't make the cut, and which role/resume won it."""

    job: Job
    score: float
    matched_keywords: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    rejected_because: str | None = None
    role_name: str = ""
    resume_path: str = ""
    resume_label: str = ""

    @property
    def passed(self) -> bool:
        return self.rejected_because is None


class Status(str, Enum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    # You applied by hand and told the app so. Terminal, like APPLIED — the tool must
    # never re-apply to something you've already sent yourself.
    APPLIED_MANUALLY = "applied_manually"
    MANUAL = "manual"  # needs a human — captcha, SSO, unanswerable question
    FAILED = "failed"  # unexpected error, retryable
    SKIPPED = "skipped"  # filtered out or over a cap
    DRY_RUN = "dry_run"  # matched and would have applied
    # Manual-review mode: the form was filled and left open in the browser for you to
    # check and submit. Not terminal — you decide whether it becomes an application.
    FILLED = "filled_for_review"
    # You told the app this job is not what you want. Never applied to, and the reasons
    # feed back into scoring so similar jobs rank lower next time.
    IRRELEVANT = "irrelevant"


@dataclass
class Outcome:
    """Result of attempting one application."""

    job: Job
    status: Status
    reason: str = ""
    score: float = 0.0
    screenshot: str = ""
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def needs_human(self) -> bool:
        return self.status in (Status.MANUAL, Status.FAILED, Status.FILLED)


@dataclass
class RunReport:
    """Everything one daily run did — the payload the notification is built from."""

    started_at: datetime
    finished_at: datetime | None = None
    discovered: int = 0
    matched: int = 0
    outcomes: list[Outcome] = field(default_factory=list)
    source_errors: dict[str, str] = field(default_factory=dict)
    dry_run: bool = False

    def by_status(self, status: Status) -> list[Outcome]:
        return [o for o in self.outcomes if o.status == status]

    @property
    def applied(self) -> list[Outcome]:
        return self.by_status(Status.APPLIED)

    @property
    def manual(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.needs_human]

    @property
    def duration_seconds(self) -> float:
        end = self.finished_at or datetime.now(timezone.utc)
        return (end - self.started_at).total_seconds()
