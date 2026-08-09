"""Naukri.com applier.

Naukri has three outcomes and only one of them is automatable:

  * **Easy apply** — one click, resume already on file. Automated.
  * **Chatbot screening** — a drawer opens asking free-form recruiter questions
    ("why are you leaving?", "current CTC?"). Answered only when your config supplies
    a matching answer; otherwise routed to manual, because a wrong answer to a
    screening question is worse than no application.
  * **"Apply on company site"** — redirects to an external ATS. Routed to manual with
    the link, since the destination form is unknown and often behind a login.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..browser import is_logged_out, page_has_captcha
from ..models import Job, Outcome, Status
from .base import Applier, ApplyContext

log = logging.getLogger(__name__)

_APPLY_SELECTORS = [
    "button#apply-button",
    "button.apply-button",
    "button:has-text('Apply')",
    "a:has-text('Apply')",
    "[class*='apply-button']",
]

_EXTERNAL_SELECTORS = [
    "button#company-site-button",
    "button:has-text('Apply on company site')",
    "a:has-text('Apply on company site')",
]

_CHATBOT_SELECTORS = [
    "div[class*='chatbot_Drawer']",
    "div[class*='chatbot']",
    "div._chatBotContainer",
]

_ALREADY_RE = re.compile(r"\b(already applied|you have applied|applied on)\b", re.I)
_SUCCESS_RE = re.compile(
    r"(successfully applied|application sent|you have successfully|applied successfully)", re.I
)


class NaukriApplier(Applier):
    name = "naukri"
    handles = ("naukri",)

    def apply(self, job: Job, ctx: ApplyContext) -> Outcome:
        page = ctx.browser.new_page()
        try:
            return self._run(page, job, ctx)
        except Exception as exc:
            shot = ctx.browser.screenshot(page, f"naukri-error-{job.company}") if ctx.screenshot_on_failure else ""
            log.warning("naukri apply failed for %s — %s", job.title, exc)
            return self.failed(job, f"unexpected error: {exc}", shot)
        finally:
            try:
                page.close()
            except Exception:
                pass

    def _run(self, page: Any, job: Job, ctx: ApplyContext) -> Outcome:
        page.goto(job.target_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)

        if is_logged_out(page):
            return self.manual(job, "Naukri session expired — run `jobhunter login naukri` to sign in again")

        if page_has_captcha(page):
            shot = ctx.browser.screenshot(page, f"naukri-captcha-{job.company}")
            return self.manual(job, "Naukri showed a captcha", shot)

        body = (page.inner_text("body") or "")
        if _ALREADY_RE.search(body):
            return Outcome(job=job, status=Status.ALREADY_APPLIED, reason="Naukri says you already applied")

        # External redirect: check before the generic Apply button, since both exist
        # on the page and the generic selector would match the wrong one.
        if self._visible(page, _EXTERNAL_SELECTORS):
            return self.manual(
                job,
                "Naukri hands this one off to the company's own site — apply there manually",
            )

        if ctx.dry_run:
            return self.dry_run(job, "easy-apply button present, submit skipped — dry run")

        if not self._click(page, _APPLY_SELECTORS):
            shot = ctx.browser.screenshot(page, f"naukri-noapply-{job.company}")
            return self.manual(job, "no apply button found on the posting", shot)

        page.wait_for_timeout(3500)

        if self._visible(page, _CHATBOT_SELECTORS):
            handled = self._answer_chatbot(page, ctx)
            if not handled:
                shot = ctx.browser.screenshot(page, f"naukri-chatbot-{job.company}")
                return self.manual(
                    job,
                    "recruiter screening questions I have no configured answer for "
                    "(add them under profile.answers to automate next time)",
                    shot,
                )
            page.wait_for_timeout(2500)

        body = (page.inner_text("body") or "")
        if _SUCCESS_RE.search(body) or _ALREADY_RE.search(body):
            return self.applied(job, "Naukri confirmed the application")

        shot = ctx.browser.screenshot(page, f"naukri-unconfirmed-{job.company}")
        return self.manual(job, "clicked apply but Naukri showed no confirmation — please verify", shot)

    def _answer_chatbot(self, page: Any, ctx: ApplyContext) -> bool:
        """Answer screening questions from configured answers only. All-or-nothing:
        a partially completed chatbot leaves the application in limbo."""
        for _ in range(8):  # the drawer asks one question at a time
            try:
                question = page.locator("div[class*='botMsg'], div[class*='chatbot'] li").last
                text = question.inner_text() if question.count() else ""
            except Exception:
                return False
            if not text:
                return False

            answer = ctx.profile.custom_answer(text)
            if not answer:
                log.info("naukri chatbot asked something unconfigured: %s", text[:120])
                return False

            try:
                box = page.locator("div[class*='textArea'], textarea, input[type='text']").last
                box.fill(answer, timeout=5000)
                page.locator("div[class*='sendMsg'], button:has-text('Save')").last.click(timeout=5000)
                page.wait_for_timeout(1500)
            except Exception as exc:
                log.debug("chatbot interaction failed: %s", exc)
                return False

            if not self._visible(page, _CHATBOT_SELECTORS):
                return True
        return False

    def _visible(self, page: Any, selectors: list[str]) -> bool:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0 and locator.is_visible():
                    return True
            except Exception:
                continue
        return False

    def _click(self, page: Any, selectors: list[str]) -> bool:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0 and locator.is_visible():
                    locator.scroll_into_view_if_needed(timeout=3000)
                    locator.click(timeout=6000)
                    return True
            except Exception as exc:
                log.debug("naukri click '%s' failed: %s", selector, exc)
        return False
