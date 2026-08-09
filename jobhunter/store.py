"""SQLite persistence — de-duplication across runs and an audit trail of applications.

The de-dup guarantee is the important part: without it a daily run re-applies to
every job it found yesterday.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .models import Job, Outcome, RunReport, Status

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    fingerprint   TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    ats           TEXT,
    company       TEXT NOT NULL,
    title         TEXT NOT NULL,
    location      TEXT,
    url           TEXT,
    apply_url     TEXT,
    posted_at     TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    status      TEXT NOT NULL,
    reason      TEXT,
    score       REAL,
    screenshot  TEXT,
    at          TEXT NOT NULL,
    FOREIGN KEY (fingerprint) REFERENCES jobs (fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_app_fingerprint ON applications (fingerprint);
CREATE INDEX IF NOT EXISTS idx_app_at ON applications (at);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    discovered   INTEGER DEFAULT 0,
    matched      INTEGER DEFAULT 0,
    applied      INTEGER DEFAULT 0,
    manual       INTEGER DEFAULT 0,
    dry_run      INTEGER DEFAULT 0,
    errors       TEXT
);
"""

# Statuses that mean "do not try this job again".
_TERMINAL = (Status.APPLIED.value, Status.ALREADY_APPLIED.value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()

    # --- jobs ---

    def record_job(self, job: Job) -> None:
        """Upsert; keeps first_seen_at intact so we can reason about staleness."""
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO jobs (fingerprint, source, ats, company, title, location,
                                  url, apply_url, posted_at, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    apply_url    = COALESCE(NULLIF(excluded.apply_url, ''), jobs.apply_url)
                """,
                (
                    job.fingerprint, job.source, job.ats, job.company, job.title,
                    job.location, job.url, job.apply_url,
                    job.posted_at.isoformat() if job.posted_at else None,
                    _now(), _now(),
                ),
            )

    def has_applied(self, job: Job) -> bool:
        row = self._conn.execute(
            f"""SELECT 1 FROM applications
                WHERE fingerprint = ? AND status IN ({','.join('?' * len(_TERMINAL))})
                LIMIT 1""",
            (job.fingerprint, *_TERMINAL),
        ).fetchone()
        return row is not None

    def seen_recently(self, job: Job, within_days: int = 30) -> bool:
        """True if we already logged *any* attempt recently — including manual/failed.

        Stops the tool from re-queuing the same un-appliable job every single morning.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=within_days)).isoformat()
        row = self._conn.execute(
            "SELECT 1 FROM applications WHERE fingerprint = ? AND at >= ? LIMIT 1",
            (job.fingerprint, cutoff),
        ).fetchone()
        return row is not None

    # --- applications ---

    def record_outcome(self, outcome: Outcome) -> None:
        self.record_job(outcome.job)
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO applications (fingerprint, status, reason, score, screenshot, at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    outcome.job.fingerprint, outcome.status.value, outcome.reason,
                    outcome.score, outcome.screenshot, outcome.at.isoformat(),
                ),
            )

    def applications_today(self) -> int:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        row = self._conn.execute(
            "SELECT COUNT(*) c FROM applications WHERE status = ? AND at >= ?",
            (Status.APPLIED.value, start.isoformat()),
        ).fetchone()
        return int(row["c"])

    def applications_today_for_company(self, company: str) -> int:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        row = self._conn.execute(
            """SELECT COUNT(*) c FROM applications a
               JOIN jobs j ON j.fingerprint = a.fingerprint
               WHERE a.status = ? AND a.at >= ? AND LOWER(j.company) = LOWER(?)""",
            (Status.APPLIED.value, start.isoformat(), company),
        ).fetchone()
        return int(row["c"])

    def status_for(self, job: Job) -> tuple[str, str, str]:
        """Latest known state of a job as (status, reason, iso_timestamp).

        Returns ("new", "", "") when we've never attempted it. This is what the GUI
        uses to tell you at a glance whether you've already applied.
        """
        row = self._conn.execute(
            """SELECT status, reason, at FROM applications
               WHERE fingerprint = ?
               ORDER BY CASE status WHEN 'applied' THEN 0 WHEN 'already_applied' THEN 0 ELSE 1 END,
                        at DESC
               LIMIT 1""",
            (job.fingerprint,),
        ).fetchone()
        if row is None:
            return "new", "", ""
        return row["status"], row["reason"] or "", row["at"]

    def statuses_for(self, jobs: list[Job]) -> dict[str, tuple[str, str, str]]:
        """Bulk version of status_for — one query instead of N for a full result grid."""
        return {job.fingerprint: self.status_for(job) for job in jobs}

    def pending_manual(self, limit: int = 100) -> list[sqlite3.Row]:
        return self._conn.execute(
            """SELECT j.company, j.title, j.location, COALESCE(NULLIF(j.apply_url,''), j.url) url,
                      a.reason, a.at
               FROM applications a JOIN jobs j ON j.fingerprint = a.fingerprint
               WHERE a.status IN (?, ?)
                 AND a.fingerprint NOT IN (SELECT fingerprint FROM applications WHERE status = ?)
               ORDER BY a.at DESC LIMIT ?""",
            (Status.MANUAL.value, Status.FAILED.value, Status.APPLIED.value, limit),
        ).fetchall()

    def stats(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) c FROM applications GROUP BY status"
        ).fetchall()
        out = {r["status"]: int(r["c"]) for r in rows}
        out["jobs_seen"] = int(
            self._conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]
        )
        return out

    # --- runs ---

    def record_run(self, report: RunReport) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO runs (started_at, finished_at, discovered, matched,
                                     applied, manual, dry_run, errors)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    report.started_at.isoformat(),
                    (report.finished_at or datetime.now(timezone.utc)).isoformat(),
                    report.discovered, report.matched,
                    len(report.applied), len(report.manual),
                    int(report.dry_run), json.dumps(report.source_errors),
                ),
            )
