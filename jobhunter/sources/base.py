"""Shared plumbing for job sources: HTTP session, HTML cleanup, base class."""

from __future__ import annotations

import html
import logging
import random
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..models import Job

log = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]*\n[ \t]*")


def make_session(timeout: int = 20) -> requests.Session:
    """Session with sane retries. Public job-board APIs rate-limit; backing off is required."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": _UA, "Accept": "application/json, text/html, */*"})
    session.request_timeout = timeout  # type: ignore[attr-defined]
    return session


def strip_html(raw: str | None) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return _WS_RE.sub("\n", text).strip()


def parse_epoch_ms(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def parse_iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        from dateutil import parser as date_parser

        parsed = date_parser.parse(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def looks_remote(*fields: str) -> bool:
    blob = " ".join(f for f in fields if f).lower()
    return bool(re.search(r"\b(remote|work from home|wfh|anywhere)\b", blob))


class Source(ABC):
    """A place jobs come from. Implementations must never raise — return what they got."""

    name: str = "source"

    def __init__(self, config: dict[str, Any], session: requests.Session | None = None):
        self.config = config or {}
        self.session = session or make_session()
        self.polite_delay = float(self.config.get("delay_seconds", 0.6))

    @abstractmethod
    def fetch(self, queries: list[str]) -> list[Job]:
        """Return jobs for the given search queries (role/keyword strings)."""

    # --- helpers for subclasses ---

    def _get_json(self, url: str, **kwargs: Any) -> Any:
        timeout = kwargs.pop("timeout", getattr(self.session, "request_timeout", 20))
        response = self.session.get(url, timeout=timeout, **kwargs)
        response.raise_for_status()
        return response.json()

    def _get_text(self, url: str, **kwargs: Any) -> str:
        timeout = kwargs.pop("timeout", getattr(self.session, "request_timeout", 20))
        response = self.session.get(url, timeout=timeout, **kwargs)
        response.raise_for_status()
        return response.text

    def _sleep(self) -> None:
        if self.polite_delay > 0:
            time.sleep(self.polite_delay * random.uniform(0.7, 1.4))


class BoardSource(Source):
    """Base for ATS board APIs, which list every open role for a company at one URL.

    These sources ignore the query strings during fetch — there is nothing to search,
    you get the whole board — and rely on the Scorer to filter. That is intentional:
    it finds roles a keyword search would miss because of an unusual title.
    """

    def companies(self) -> list[str]:
        raw = self.config.get("companies") or self.config.get("boards") or []
        return [str(c).strip() for c in raw if str(c).strip()]

    @abstractmethod
    def fetch_company(self, company: str) -> list[Job]:
        ...

    def fetch(self, queries: list[str]) -> list[Job]:  # noqa: ARG002 - see class docstring
        jobs: list[Job] = []
        for company in self.companies():
            try:
                found = self.fetch_company(company)
                log.info("%s: %s -> %d jobs", self.name, company, len(found))
                jobs.extend(found)
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "?"
                if status == 404:
                    log.warning("%s: board '%s' not found (404) — check the slug in your config",
                                self.name, company)
                else:
                    log.warning("%s: %s failed with HTTP %s", self.name, company, status)
            except Exception as exc:  # a broken board must not kill the run
                log.warning("%s: %s failed — %s", self.name, company, exc)
            self._sleep()
        return jobs
