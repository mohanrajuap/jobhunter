"""Logging: rotating file for the audit trail, console for interactive runs."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s %(name)-22s %(message)s"


def setup_logging(log_dir: str | Path = "logs", level: str = "INFO", quiet: bool = False) -> None:
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    root.handlers.clear()

    file_handler = RotatingFileHandler(
        directory / "jobhunter.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(file_handler)

    if not quiet:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
        root.addHandler(console)

    # These are chatty and rarely tell us anything we need.
    for noisy in ("urllib3", "requests", "pdfminer", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
