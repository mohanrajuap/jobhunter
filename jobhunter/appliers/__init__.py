"""Applier registry — routes each job to the applier that understands its ATS."""

from __future__ import annotations

import logging

from ..models import Job
from .base import Applier, ApplyContext
from .browser_apply import BrowserApplier
from .form_filler import FormFiller, Profile
from .linkedin_apply import LinkedInApplier
from .naukri_apply import NaukriApplier

log = logging.getLogger(__name__)

__all__ = [
    "Applier", "ApplyContext", "BrowserApplier", "NaukriApplier", "LinkedInApplier",
    "FormFiller", "Profile", "get_applier", "ALL_APPLIERS",
]

# Order matters: the specific appliers get first refusal, BrowserApplier is the catch-all.
ALL_APPLIERS: list[Applier] = [NaukriApplier(), LinkedInApplier(), BrowserApplier()]


def get_applier(job: Job) -> Applier | None:
    for applier in ALL_APPLIERS:
        if applier.can_handle(job):
            return applier
    log.warning("no applier can handle ATS '%s' for %s @ %s", job.ats, job.title, job.company)
    return None
