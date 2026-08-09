"""Foreground scheduler.

An alternative to Task Scheduler / cron for when you'd rather keep a process running
(a always-on box, a container). The OS scheduler is more reliable for a laptop that
sleeps — see `jobhunter schedule`.
"""

from __future__ import annotations

import logging

from .config import Config

log = logging.getLogger(__name__)

_DAY_MAP = {
    "mon": "mon", "tue": "tue", "wed": "wed", "thu": "thu",
    "fri": "fri", "sat": "sat", "sun": "sun",
}


def run_daemon(config: Config) -> int:
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        log.error("APScheduler is not installed. Run: pip install APScheduler")
        return 2

    at = str(config.get("schedule.time", "08:00"))
    hour, _, minute = at.partition(":")
    days = [_DAY_MAP.get(str(d).lower()[:3], "mon") for d in config.get("schedule.days", []) or []]
    day_of_week = ",".join(days) if days else "*"
    timezone = config.get("schedule.timezone", "Asia/Kolkata")

    from .pipeline import Pipeline

    pipeline = Pipeline(config)

    def job() -> None:
        log.info("Scheduled run starting")
        try:
            pipeline.run()
        except Exception:
            # Never let one bad morning kill the daemon.
            log.exception("scheduled run failed")

    scheduler = BlockingScheduler(timezone=timezone)
    scheduler.add_job(
        job,
        CronTrigger(hour=int(hour), minute=int(minute or 0), day_of_week=day_of_week, timezone=timezone),
        id="daily_job_hunt",
        misfire_grace_time=3600,  # a laptop that woke up late should still run
        coalesce=True,
    )

    log.info("Daemon started — running at %s (%s) on %s. Ctrl-C to stop.", at, timezone, day_of_week)
    if config.get("schedule.run_on_start", False):
        job()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Daemon stopped")
    return 0
