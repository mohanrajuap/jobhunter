"""Generic browser applier for the standard ATS platforms.

Greenhouse, Lever, Ashby, Workable, SmartRecruiters and most custom career pages all
follow the same shape: open posting → reveal the form → fill → submit → confirmation.
The per-ATS differences are just selectors, so they live in `_ATS_SPECS` rather than
in six near-identical classes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..browser import page_has_captcha
from ..models import Job, Outcome, Status
from .base import Applier, ApplyContext
from .form_filler import FormFiller

log = logging.getLogger(__name__)


@dataclass
class AtsSpec:
    apply_button: list[str] = field(default_factory=list)
    submit_button: list[str] = field(default_factory=list)
    success_text: list[str] = field(default_factory=list)
    success_url: list[str] = field(default_factory=list)


_GENERIC_SUBMIT = [
    "button#submit_app", "input[type='submit']", "button[type='submit']",
    "button:has-text('Submit application')", "button:has-text('Submit Application')",
    "button:has-text('Submit')", "button:has-text('Send application')",
    "button:has-text('Apply')", "a:has-text('Submit')",
]

_GENERIC_APPLY = [
    "a#apply_button", "a:has-text('Apply for this job')", "button:has-text('Apply for this job')",
    "button:has-text('Apply now')", "a:has-text('Apply now')",
    "button:has-text('Apply')", "a:has-text('Apply')",
]

_SUCCESS_TEXT = [
    "thank you for applying", "thanks for applying", "application received",
    "application submitted", "successfully submitted", "we have received your application",
    "your application has been", "thank you for your interest", "application complete",
]

_ATS_SPECS: dict[str, AtsSpec] = {
    "greenhouse": AtsSpec(
        apply_button=["a#apply_button", "button:has-text('Apply')"],
        submit_button=["input#submit_app", "button#submit_app", "button[type='submit']"],
        success_text=_SUCCESS_TEXT + ["your application has been submitted"],
        success_url=["confirmation", "thank"],
    ),
    "lever": AtsSpec(
        apply_button=["a:has-text('Apply for this job')", "a.postings-btn"],
        submit_button=["button[type='submit']", "input[type='submit']", "button:has-text('Submit application')"],
        success_text=_SUCCESS_TEXT,
        success_url=["thanks", "confirmation"],
    ),
    "ashby": AtsSpec(
        apply_button=["button:has-text('Apply for this Job')", "a:has-text('Apply')"],
        submit_button=["button:has-text('Submit Application')", "button[type='submit']"],
        success_text=_SUCCESS_TEXT + ["application submitted"],
        success_url=["confirmation", "thank"],
    ),
    "workable": AtsSpec(
        apply_button=["a:has-text('Apply for this job')", "button:has-text('Apply')"],
        submit_button=["button[data-ui='submit-application']", "button[type='submit']"],
        success_text=_SUCCESS_TEXT,
        success_url=["thank", "success"],
    ),
    "smartrecruiters": AtsSpec(
        # 'interested' matches SmartRecruiters' "I'm interested" button without the
        # apostrophe tripping up Playwright's selector parser.
        apply_button=["button:has-text('interested')", "a:has-text('Apply')", "button:has-text('Apply')"],
        submit_button=["button[type='submit']", "button:has-text('Submit')"],
        success_text=_SUCCESS_TEXT,
        success_url=["thank", "success", "confirmation"],
    ),
}

# Pages that hand off to a login wall or a third-party system we shouldn't drive.
_HANDOFF_RE = re.compile(
    r"(sign in to apply|log in to apply|continue with linkedin|apply on company (site|website)|"
    r"create an account to apply|register to apply)",
    re.I,
)


class BrowserApplier(Applier):
    name = "browser"
    handles = ("greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee", "custom")

    def can_handle(self, job: Job) -> bool:
        return job.ats != "naukri"  # everything non-Naukri gets the generic treatment

    def _spec(self, job: Job) -> AtsSpec:
        return _ATS_SPECS.get(
            job.ats,
            AtsSpec(apply_button=_GENERIC_APPLY, submit_button=_GENERIC_SUBMIT,
                    success_text=_SUCCESS_TEXT, success_url=["thank", "confirmation", "success"]),
        )

    def apply(self, job: Job, ctx: ApplyContext) -> Outcome:
        spec = self._spec(job)
        page = ctx.browser.new_page()
        try:
            return self._run(page, job, ctx, spec)
        except Exception as exc:
            shot = ctx.browser.screenshot(page, f"error-{job.company}") if ctx.screenshot_on_failure else ""
            log.warning("apply failed for %s @ %s — %s", job.title, job.company, exc)
            return self.failed(job, f"unexpected error: {exc}", shot)
        finally:
            try:
                page.close()
            except Exception:
                pass

    def _run(self, page: Any, job: Job, ctx: ApplyContext, spec: AtsSpec) -> Outcome:
        page.goto(job.target_url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

        if page_has_captcha(page):
            shot = ctx.browser.screenshot(page, f"captcha-{job.company}")
            return self.manual(job, "captcha / human verification on the page", shot)

        body = (page.inner_text("body") or "")[:6000]
        if (handoff := _HANDOFF_RE.search(body)):
            shot = ctx.browser.screenshot(page, f"handoff-{job.company}")
            return self.manual(job, f"requires sign-in or external site ({handoff.group(0)})", shot)

        self._reveal_form(page, spec)

        filler = FormFiller(page, ctx.profile, ctx.resume_path, ctx.cover_letter_path)
        fields = filler.collect()
        if not fields:
            shot = ctx.browser.screenshot(page, f"noform-{job.company}")
            return self.manual(job, "no application form found on the page", shot)

        result = filler.fill()
        log.debug("filled %d fields for %s (%d skipped)", len(result.filled), job.title, len(result.skipped))

        if not result.resume_uploaded and any(f.type == "file" for f in fields):
            log.warning("resume was not attached for %s @ %s", job.title, job.company)

        if not result.ok:
            shot = ctx.browser.screenshot(page, f"unanswered-{job.company}")
            missing = "; ".join(result.unresolved_required[:5])
            return self.manual(
                job,
                f"{len(result.unresolved_required)} required question(s) I could not answer: {missing}",
                shot,
            )

        if ctx.dry_run:
            shot = ctx.browser.screenshot(page, f"dryrun-{job.company}")
            return Outcome(
                job=job,
                status=Status.DRY_RUN,
                reason=f"form filled ({len(result.filled)} fields), submit skipped — dry run",
                screenshot=shot,
            )

        if not self._click_first(page, spec.submit_button):
            shot = ctx.browser.screenshot(page, f"nosubmit-{job.company}")
            return self.manual(job, "form filled but no submit button found", shot)

        page.wait_for_timeout(4000)
        try:
            page.wait_for_load_state("networkidle", timeout=ctx.submit_timeout_ms)
        except Exception:
            pass  # SPAs often never go idle; the success check below is what counts

        return self._verify(page, job, ctx, spec)

    def _reveal_form(self, page: Any, spec: AtsSpec) -> None:
        """Click 'Apply' only when no form is on screen — clicking it after the form
        is rendered can navigate away from a half-filled page."""
        try:
            if page.locator("input[type='file'], textarea, form input[type='email']").count() > 0:
                return
        except Exception:
            pass
        if self._click_first(page, spec.apply_button or _GENERIC_APPLY):
            page.wait_for_timeout(2500)

    def _click_first(self, page: Any, selectors: list[str]) -> bool:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0 and locator.is_visible():
                    locator.scroll_into_view_if_needed(timeout=3000)
                    locator.click(timeout=6000)
                    return True
            except Exception as exc:
                log.debug("click '%s' failed: %s", selector, exc)
        return False

    def _verify(self, page: Any, job: Job, ctx: ApplyContext, spec: AtsSpec) -> Outcome:
        try:
            body = (page.inner_text("body") or "").lower()
            url = (page.url or "").lower()
        except Exception:
            body, url = "", ""

        if any(marker in body for marker in spec.success_text):
            return self.applied(job, "confirmation message shown")
        if any(marker in url for marker in spec.success_url):
            return self.applied(job, f"redirected to confirmation page ({url[:80]})")

        # Validation errors mean the submit was rejected — a human should look.
        if re.search(r"(is required|required field|please (enter|complete|fill)|invalid)", body):
            shot = ctx.browser.screenshot(page, f"validation-{job.company}")
            return self.manual(job, "form rejected on submit (validation errors remain)", shot)

        if page_has_captcha(page):
            shot = ctx.browser.screenshot(page, f"captcha-submit-{job.company}")
            return self.manual(job, "captcha appeared at submit", shot)

        # Submitted, but we can't prove it landed. Flagged rather than silently counted.
        shot = ctx.browser.screenshot(page, f"unconfirmed-{job.company}")
        return self.manual(job, "submitted but no confirmation detected — please verify", shot)
