"""Notification dispatch. Every channel is best-effort: a broken notifier must never
lose the run's results, so failures are logged and the others still fire."""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import Config
from ..models import RunReport
from . import report as report_builder

log = logging.getLogger(__name__)


def send_all(config: Config, run: RunReport) -> dict[str, str]:
    """Fire every enabled channel. Returns {channel: 'ok' | error message}."""
    results: dict[str, str] = {}

    # Always write the report to disk first — it's the fallback if every channel fails.
    try:
        results["file"] = _write_file(config, run)
    except Exception as exc:
        log.warning("could not write report file: %s", exc)
        results["file"] = f"error: {exc}"

    if config.get("notify.email.enabled", False):
        results["email"] = _try(_send_email, config, run, channel="email")

    if config.get("notify.telegram.enabled", False):
        results["telegram"] = _try(_send_telegram, config, run, channel="telegram")

    if config.get("notify.desktop.enabled", False):
        results["desktop"] = _try(_send_desktop, config, run, channel="desktop")

    if not any(k in results for k in ("email", "telegram", "desktop")):
        log.warning(
            "No notification channel is enabled — the report was only written to disk. "
            "Enable notify.email or notify.telegram to get the daily summary."
        )
    return results


def _try(fn, config: Config, run: RunReport, channel: str) -> str:
    try:
        fn(config, run)
        log.info("notification sent via %s", channel)
        return "ok"
    except Exception as exc:
        log.error("%s notification failed: %s", channel, exc)
        return f"error: {exc}"


def _write_file(config: Config, run: RunReport) -> str:
    directory = Path(config.get("paths.report_dir", "reports"))
    directory.mkdir(parents=True, exist_ok=True)
    stamp = run.started_at.astimezone().strftime("%Y-%m-%d_%H%M")
    html_path = directory / f"report_{stamp}.html"
    html_path.write_text(report_builder.build_html(run), encoding="utf-8")
    (directory / f"report_{stamp}.txt").write_text(report_builder.build_text(run), encoding="utf-8")
    log.info("report written to %s", html_path)
    return str(html_path)


def _send_email(config: Config, run: RunReport) -> None:
    from .email_smtp import send_email

    send_email(
        host=config.get("notify.email.smtp_host", "smtp.gmail.com"),
        port=int(config.get("notify.email.smtp_port", 587)),
        username=config.get("notify.email.username", ""),
        password=config.get("notify.email.password", ""),
        sender=config.get("notify.email.from") or config.get("notify.email.username", ""),
        recipients=config.get("notify.email.to", []) or [],
        subject=report_builder.build_subject(run),
        text_body=report_builder.build_text(run),
        html_body=report_builder.build_html(run),
        use_tls=bool(config.get("notify.email.use_tls", True)),
    )


def _send_telegram(config: Config, run: RunReport) -> None:
    from .telegram import send_telegram

    send_telegram(
        bot_token=config.get("notify.telegram.bot_token", ""),
        chat_id=config.get("notify.telegram.chat_id", ""),
        text=report_builder.build_short(run),
    )


def _send_desktop(config: Config, run: RunReport) -> None:
    from .desktop import send_desktop

    send_desktop(
        title=report_builder.build_subject(run),
        message=f"{len(run.applied)} applied, {len(run.manual)} need you. Check your email for links.",
    )
