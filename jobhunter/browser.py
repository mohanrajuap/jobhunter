"""Playwright browser session shared by the Naukri source and every applier.

Uses a *persistent* context so logins survive between daily runs. You sign in once,
by hand (`jobhunter login naukri`), and the saved profile keeps the run autonomous
after that. Credentials are never typed by the tool — that keeps OTP, captcha and
2FA in your hands, which is also the only thing that reliably works.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

_CAPTCHA_MARKERS = (
    "recaptcha", "g-recaptcha", "hcaptcha", "cf-turnstile",
    "captcha", "are you a robot", "verify you are human",
)


class BrowserUnavailable(RuntimeError):
    """Playwright isn't installed or the browser binary is missing."""


class BrowserSession:
    """Owns one persistent Chromium context for the lifetime of a run."""

    def __init__(
        self,
        user_data_dir: str | Path,
        headless: bool = False,
        slow_mo_ms: int = 0,
        timeout_ms: int = 30_000,
        screenshot_dir: str | Path = "screenshots",
        locale: str = "en-IN",
    ):
        self.user_data_dir = Path(user_data_dir)
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.slow_mo_ms = slow_mo_ms
        self.timeout_ms = timeout_ms
        self.locale = locale
        self.screenshot_dir = Path(screenshot_dir)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)

        self._playwright: Any = None
        self.context: Any = None

    def start(self) -> "BrowserSession":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserUnavailable(
                "Playwright is not installed. Run:\n"
                "  pip install playwright\n"
                "  python -m playwright install chromium"
            ) from exc

        self._playwright = sync_playwright().start()
        try:
            self.context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=self.headless,
                slow_mo=self.slow_mo_ms,
                locale=self.locale,
                viewport={"width": 1440, "height": 900},
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
        except Exception as exc:
            self._playwright.stop()
            raise BrowserUnavailable(
                f"Could not launch Chromium ({exc}). "
                "If the browser is missing run: python -m playwright install chromium"
            ) from exc

        self.context.set_default_timeout(self.timeout_ms)
        self.context.set_default_navigation_timeout(self.timeout_ms)
        return self

    def new_page(self) -> Any:
        if self.context is None:
            raise RuntimeError("BrowserSession.start() must be called first")
        return self.context.new_page()

    def screenshot(self, page: Any, label: str) -> str:
        """Best-effort screenshot for the manual-review queue. Never raises."""
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:60]
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.screenshot_dir / f"{stamp}_{safe}.png"
        try:
            page.screenshot(path=str(path), full_page=False)
            return str(path)
        except Exception as exc:
            log.debug("screenshot failed for %s — %s", label, exc)
            return ""

    def close(self) -> None:
        for closer in (getattr(self.context, "close", None), getattr(self._playwright, "stop", None)):
            if closer:
                try:
                    closer()
                except Exception as exc:
                    log.debug("browser teardown warning: %s", exc)
        self.context = None
        self._playwright = None

    def __enter__(self) -> "BrowserSession":
        return self.start()

    def __exit__(self, *_exc: Any) -> None:
        self.close()


@contextmanager
def browser_from_config(config: Any, headless: bool | None = None) -> Iterator[BrowserSession]:
    """Build a BrowserSession from the app config."""
    session = BrowserSession(
        user_data_dir=config.get("apply.browser_profile_dir", str(config.state_dir() / "browser")),
        headless=config.get("apply.headless", False) if headless is None else headless,
        slow_mo_ms=int(config.get("apply.slow_mo_ms", 0)),
        timeout_ms=int(config.get("apply.timeout_seconds", 30)) * 1000,
        screenshot_dir=config.get("paths.screenshot_dir", "screenshots"),
        locale=config.get("apply.locale", "en-IN"),
    )
    session.start()
    try:
        yield session
    finally:
        session.close()


def page_has_captcha(page: Any) -> bool:
    """Detect a human-verification wall. Such jobs are routed to manual review.

    Solving or bypassing captchas is out of scope by design — that check exists to
    keep automation out, and this tool respects it.
    """
    try:
        html = (page.content() or "").lower()
    except Exception:
        return False
    return any(marker in html for marker in _CAPTCHA_MARKERS)


def is_logged_out(page: Any, markers: tuple[str, ...] = ("login", "sign in", "register")) -> bool:
    """Rough heuristic: a login form on the page means the saved session expired."""
    try:
        if page.locator("input[type='password']").count() > 0:
            return True
        url = (page.url or "").lower()
        return any(m in url for m in ("login", "signin", "sign-in"))
    except Exception:
        return False
