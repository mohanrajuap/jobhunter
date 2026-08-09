"""LinkedIn applier.

LinkedIn postings come in two shapes:

  * **Apply on company website** — the posting is really an ATS job with a LinkedIn
    front page. We follow the outbound link and hand the real form to the generic
    browser applier, which knows how to fill Greenhouse/Lever/Workday-style pages.
  * **Easy Apply** — a multi-step modal behind a login, with per-employer screening
    questions. Routed to your manual queue with a direct link.

That split is the whole value here: the first case is a normal application the tool can
complete, and it would otherwise be thrown away as "a LinkedIn job".
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..models import Job, Outcome
from .base import Applier, ApplyContext
from .browser_apply import BrowserApplier

log = logging.getLogger(__name__)

_EXTERNAL_LINK_SELECTORS = [
    "a[data-tracking-control-name*='apply'][href^='http']",
    "a.topcard__link[href*='guest_apply']",
    "code#applyUrl",
    "a:has-text('Apply on company website')",
]

_EASY_APPLY_RE = re.compile(r"easy apply", re.I)
# LinkedIn wraps outbound links; the real destination is in the url= parameter.
_WRAPPED_RE = re.compile(r"[?&]url=([^&]+)")


class LinkedInApplier(Applier):
    name = "linkedin"
    handles = ("linkedin",)

    def __init__(self) -> None:
        self._generic = BrowserApplier()

    def apply(self, job: Job, ctx: ApplyContext) -> Outcome:
        page = ctx.browser.new_page()
        try:
            page.goto(job.target_url, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

            external = self._external_url(page)
            body = (page.inner_text("body") or "")[:4000]

            if external:
                log.info("linkedin: '%s' applies on %s — handing to the browser applier",
                         job.title, external[:60])
                # Re-point the job at the real ATS and let the generic applier work.
                resolved = Job(**{**job.__dict__, "apply_url": external, "ats": "custom"})
                page.close()
                return self._generic.apply(resolved, ctx)

            if _EASY_APPLY_RE.search(body):
                shot = ctx.browser.screenshot(page, f"linkedin-easyapply-{job.company}")
                return self.manual(
                    job,
                    "LinkedIn Easy Apply — needs a login and its own screening questions, "
                    "so it has to be done by hand",
                    shot,
                )

            return self.manual(job, "no application link found on the LinkedIn posting")
        except Exception as exc:
            log.warning("linkedin apply failed for %s — %s", job.title, exc)
            return self.failed(job, f"unexpected error: {exc}")
        finally:
            try:
                if not page.is_closed():
                    page.close()
            except Exception:
                pass

    def _external_url(self, page: Any) -> str:
        """Find the outbound 'apply on company website' destination, if there is one."""
        for selector in _EXTERNAL_LINK_SELECTORS:
            try:
                locator = page.locator(selector).first
                if locator.count() == 0:
                    continue
                # LinkedIn sometimes stashes the destination in a <code> block.
                raw = locator.get_attribute("href") or (locator.inner_text() or "").strip().strip('"')
            except Exception:
                continue

            if not raw or not raw.startswith("http"):
                continue
            if "linkedin.com" in raw and (match := _WRAPPED_RE.search(raw)):
                from urllib.parse import unquote

                raw = unquote(match.group(1))
            if raw.startswith("http") and "linkedin.com/uas/login" not in raw:
                return raw
        return ""
