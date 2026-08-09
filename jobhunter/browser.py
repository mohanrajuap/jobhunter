"""Playwright browser session shared by the Naukri source and every applier.

Uses a *persistent* context so logins survive between daily runs. You sign in once,
by hand (`jobhunter login naukri`), and the saved profile keeps the run autonomous
after that. Credentials are never typed by the tool — that keeps OTP, captcha and
2FA in your hands, which is also the only thing that reliably works.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

_CAPTCHA_MARKERS = (
    "recaptcha", "g-recaptcha", "hcaptcha", "cf-turnstile",
    "captcha", "are you a robot", "verify you are human",
)


@dataclass
class BrowserChoice:
    """An installed browser the tool can drive.

    Playwright speaks `channel` for Chrome and Edge, but everything else Chromium-based
    (Brave, Vivaldi, Opera) has to be launched by executable path instead.
    """

    key: str
    label: str
    channel: str = ""
    executable: str = ""
    profile_dir: str = ""

    @property
    def installed(self) -> bool:
        return self.key == "chromium" or bool(self.executable)


def _first_existing(*paths: str) -> str:
    return next((p for p in paths if p and Path(p).exists()), "")


def detect_browsers() -> list[BrowserChoice]:
    """Which browsers are actually on this machine, for the UI's picker."""
    local = os.environ.get("LOCALAPPDATA", "")
    files = os.environ.get("ProgramFiles", r"C:\Program Files")
    files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")

    candidates = [
        BrowserChoice("chromium", "Bundled Chromium (Playwright)"),
        BrowserChoice(
            "chrome", "Google Chrome", channel="chrome",
            executable=_first_existing(
                rf"{files}\Google\Chrome\Application\chrome.exe",
                rf"{files_x86}\Google\Chrome\Application\chrome.exe",
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            ),
            profile_dir=rf"{local}\Google\Chrome\User Data" if local else "",
        ),
        BrowserChoice(
            "brave", "Brave",
            executable=_first_existing(
                rf"{files}\BraveSoftware\Brave-Browser\Application\brave.exe",
                rf"{files_x86}\BraveSoftware\Brave-Browser\Application\brave.exe",
                rf"{local}\BraveSoftware\Brave-Browser\Application\brave.exe" if local else "",
                "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            ),
            profile_dir=rf"{local}\BraveSoftware\Brave-Browser\User Data" if local else "",
        ),
        BrowserChoice(
            "msedge", "Microsoft Edge", channel="msedge",
            executable=_first_existing(
                rf"{files_x86}\Microsoft\Edge\Application\msedge.exe",
                rf"{files}\Microsoft\Edge\Application\msedge.exe",
            ),
            profile_dir=rf"{local}\Microsoft\Edge\User Data" if local else "",
        ),
    ]
    return [c for c in candidates if c.installed]


def get_browser_choice(key: str) -> BrowserChoice | None:
    return next((c for c in detect_browsers() if c.key == key.lower().strip()), None)


def _profile_is_locked(profile_dir: Path) -> bool:
    """Chromium refuses to start on a profile another instance already owns."""
    return any((profile_dir / marker).exists() for marker in ("SingletonLock", "lockfile"))


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
        channel: str = "",
        executable_path: str = "",
    ):
        self.user_data_dir = Path(user_data_dir)
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.slow_mo_ms = slow_mo_ms
        self.timeout_ms = timeout_ms
        self.locale = locale
        # "" uses Playwright's bundled Chromium; "chrome"/"msedge" drive the browser
        # already installed on the machine, which skips the ~150 MB download.
        self.channel = (channel or "").strip()
        # Set for Chromium-based browsers Playwright has no channel for — Brave, Vivaldi.
        self.executable = (executable_path or "").strip()
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
        options: dict[str, Any] = dict(
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
        # executable_path and channel are mutually exclusive in Playwright.
        if self.executable:
            options["executable_path"] = self.executable
        elif self.channel:
            options["channel"] = self.channel

        if _profile_is_locked(self.user_data_dir):
            log.warning(
                "The browser profile at %s is locked — close that browser completely "
                "(check the system tray) or the launch will fail.", self.user_data_dir
            )

        try:
            self.context = self._playwright.chromium.launch_persistent_context(**options)
            log.info("Browser started (%s)", self.executable or self.channel or "bundled chromium")
        except Exception as exc:
            # Falling back beats failing the whole run: if the bundled Chromium was
            # never downloaded but a real browser is installed, use that instead.
            if not self.channel and not self.executable:
                for candidate in detect_browsers():
                    if candidate.key == "chromium":
                        continue
                    retry = dict(options)
                    if candidate.channel:
                        retry["channel"] = candidate.channel
                    else:
                        retry["executable_path"] = candidate.executable
                    try:
                        self.context = self._playwright.chromium.launch_persistent_context(**retry)
                        self.channel = candidate.channel
                        self.executable = candidate.executable
                        log.warning(
                            "Bundled Chromium unavailable — using your installed %s instead. "
                            "Set apply.browser: %s to make that the default.",
                            candidate.label, candidate.key,
                        )
                        break
                    except Exception:
                        continue

            if self.context is None:
                self._playwright.stop()
                installed = ", ".join(
                    c.key for c in detect_browsers() if c.key != "chromium"
                ) or "none detected"
                raise BrowserUnavailable(
                    f"Could not launch a browser ({str(exc)[:200]}).\n\n"
                    "Fix it either way:\n"
                    "  • download the bundled browser:  python -m playwright install chromium\n"
                    f"  • or use one you already have — installed: {installed}\n"
                    "    (set it on the Search tab, or apply.browser in the config)\n\n"
                    "If you picked 'use my logged-in profile', the browser must be fully "
                    "closed first — check the system tray."
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


def resolve_browser(config: Any) -> tuple[str, str, str]:
    """Work out (channel, executable_path, user_data_dir) from config.

    `apply.use_existing_profile` is the one that matters for sites you're already signed
    into: it points the automation at your real browser profile, cookies and all, so
    Naukri and LinkedIn are already logged in. The cost is that the browser must be
    completely closed while a run happens — Chromium will not share a profile.
    """
    key = str(config.get("apply.browser", "") or "").lower().strip()
    channel = str(config.get("apply.browser_channel", "") or "")
    executable = str(config.get("apply.browser_executable", "") or "")
    profile_dir = str(config.get("apply.browser_profile_dir", "") or "")

    choice = get_browser_choice(key) if key else None
    if choice:
        channel = channel or choice.channel
        executable = executable or choice.executable
        if choice.key == "chromium":
            channel, executable = "", ""

    if config.get("apply.use_existing_profile", False):
        if choice and choice.profile_dir and Path(choice.profile_dir).exists():
            profile_dir = choice.profile_dir
            log.info("Using your existing %s profile — sites you're signed into stay "
                     "signed in. That browser must be closed while this runs.", choice.label)
        else:
            log.warning(
                "apply.use_existing_profile is on but no profile directory was found for "
                "'%s' — falling back to the tool's own profile", key or "(unset)"
            )

    return channel, executable, profile_dir or str(config.state_dir() / "browser")


@contextmanager
def browser_from_config(config: Any, headless: bool | None = None) -> Iterator[BrowserSession]:
    """Build a BrowserSession from the app config."""
    channel, executable, profile_dir = resolve_browser(config)

    resolved_headless = config.get("apply.headless", False) if headless is None else headless
    if config.get("apply.use_existing_profile", False) and resolved_headless:
        # Headless plus a real profile is a reliable way to get a confusing failure.
        log.info("Running visibly: headless mode is not reliable with a real browser profile")
        resolved_headless = False

    session = BrowserSession(
        user_data_dir=profile_dir,
        headless=resolved_headless,
        slow_mo_ms=int(config.get("apply.slow_mo_ms", 0)),
        timeout_ms=int(config.get("apply.timeout_seconds", 30)) * 1000,
        screenshot_dir=config.get("paths.screenshot_dir", "screenshots"),
        locale=config.get("apply.locale", "en-IN"),
        channel=channel,
        executable_path=executable,
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
