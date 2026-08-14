"""SQLite persistence — de-duplication across runs and an audit trail of applications.

The de-dup guarantee is the important part: without it a daily run re-applies to
every job it found yesterday.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
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

-- What you told the app about jobs it showed you. Read back at scoring time, so
-- marking things irrelevant actually changes what surfaces tomorrow.
CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    label       TEXT NOT NULL,   -- 'irrelevant' | 'relevant'
    company     TEXT,
    title       TEXT,
    source      TEXT,
    reason      TEXT,
    at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_feedback_label ON feedback (label);
CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_unique ON feedback (fingerprint, label);

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
_TERMINAL = (Status.APPLIED.value, Status.ALREADY_APPLIED.value, Status.APPLIED_MANUALLY.value)

# Title words too common to carry any signal about what you rejected.
_GENERIC_TITLE_WORDS = {
    "the", "and", "for", "with", "senior", "junior", "lead", "staff", "principal",
    "associate", "engineer", "developer", "analyst", "manager", "specialist",
    "consultant", "architect", "officer", "executive", "new", "job", "role",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _chunks(items: list, size: int = 900):
    """Split a list into fixed-size slices.

    Keeps `IN (...)` bind counts under SQLite's per-query variable limit — 32766 in
    modern SQLite but only 999 before 3.32, which a 1000+ job run would blow through.
    """
    for i in range(0, len(items), size):
        yield items[i:i + size]


class Store:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The connection is shared between discovery's worker pool and the main
        # thread, so it must tolerate cross-thread use; the lock serialises access.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
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

    def record_jobs(self, jobs: list[Job]) -> None:
        """Batch upsert — one transaction for the whole list instead of one per job.

        Discovery feeds several hundred jobs through here per run; committing once per
        job was the dominant SQLite cost. Semantics are identical to `record_job`.
        """
        if not jobs:
            return
        now = _now()
        with self._tx() as conn:
            conn.executemany(
                """
                INSERT INTO jobs (fingerprint, source, ats, company, title, location,
                                  url, apply_url, posted_at, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    apply_url    = COALESCE(NULLIF(excluded.apply_url, ''), jobs.apply_url)
                """,
                [
                    (
                        job.fingerprint, job.source, job.ats, job.company, job.title,
                        job.location, job.url, job.apply_url,
                        job.posted_at.isoformat() if job.posted_at else None,
                        now, now,
                    )
                    for job in jobs
                ],
            )

    def has_applied(self, job: Job) -> bool:
        row = self._query_one(
            f"""SELECT 1 FROM applications
                WHERE fingerprint = ? AND status IN ({','.join('?' * len(_TERMINAL))})
                LIMIT 1""",
            (job.fingerprint, *_TERMINAL),
        )
        return row is not None

    def has_applied_bulk(self, fingerprints: list[str]) -> set[str]:
        """Which of these fingerprints have a terminal application — one query, not N.

        Chunked so the `IN (...)` list stays under SQLite's variable limit (999 before
        3.32, 32766 after) — a 1000+ job run would otherwise fail mid-discovery.
        """
        out: set[str] = set()
        for chunk in _chunks(fingerprints):
            rows = self._query_all(
                f"""SELECT DISTINCT fingerprint FROM applications
                    WHERE status IN ({','.join('?' * len(_TERMINAL))})
                      AND fingerprint IN ({','.join('?' * len(chunk))})""",
                (*_TERMINAL, *chunk),
            )
            out.update(row["fingerprint"] for row in rows)
        return out

    def seen_recently(self, job: Job, within_days: int = 30) -> bool:
        """True if we already logged *any* attempt recently — including manual/failed.

        Stops the tool from re-queuing the same un-appliable job every single morning.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=within_days)).isoformat()
        row = self._query_one(
            "SELECT 1 FROM applications WHERE fingerprint = ? AND at >= ? LIMIT 1",
            (job.fingerprint, cutoff),
        )
        return row is not None

    def seen_recently_bulk(self, fingerprints: list[str], within_days: int = 30) -> set[str]:
        """Which of these fingerprints have any attempt logged recently — one query.

        Chunked like `has_applied_bulk` to respect SQLite's bind-variable limit.
        """
        out: set[str] = set()
        if not fingerprints:
            return out
        cutoff = (datetime.now(timezone.utc) - timedelta(days=within_days)).isoformat()
        for chunk in _chunks(fingerprints):
            rows = self._query_all(
                f"""SELECT DISTINCT fingerprint FROM applications
                    WHERE at >= ? AND fingerprint IN ({','.join('?' * len(chunk))})""",
                (cutoff, *chunk),
            )
            out.update(row["fingerprint"] for row in rows)
        return out

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
        row = self._query_one(
            "SELECT COUNT(*) c FROM applications WHERE status = ? AND at >= ?",
            (Status.APPLIED.value, start.isoformat()),
        )
        return int(row["c"])

    def applications_today_for_company(self, company: str) -> int:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        row = self._query_one(
            """SELECT COUNT(*) c FROM applications a
               JOIN jobs j ON j.fingerprint = a.fingerprint
               WHERE a.status = ? AND a.at >= ? AND LOWER(j.company) = LOWER(?)""",
            (Status.APPLIED.value, start.isoformat(), company),
        )
        return int(row["c"])

    def applications_today_by_company(self) -> dict[str, int]:
        """Today's applied count grouped by lowercased company — one query for the run.

        Replaces calling `applications_today_for_company` once per match during the
        apply phase, which was a query per candidate application.
        """
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        rows = self._query_all(
            """SELECT LOWER(j.company) company, COUNT(*) c
               FROM applications a JOIN jobs j ON j.fingerprint = a.fingerprint
               WHERE a.status = ? AND a.at >= ?
               GROUP BY LOWER(j.company)""",
            (Status.APPLIED.value, start.isoformat()),
        )
        return {row["company"]: int(row["c"]) for row in rows}

    def mark_applied_manually(self, job: Job, note: str = "") -> Outcome:
        """Record that you applied to this job yourself.

        Persisted exactly like an automated application, so it counts toward the
        de-duplication check and the tool will never apply to it again.
        """
        outcome = Outcome(
            job=job,
            status=Status.APPLIED_MANUALLY,
            reason=note or "marked as applied by you",
        )
        self.record_outcome(outcome)
        return outcome

    def clear_manual_mark(self, job: Job) -> int:
        """Undo a manual mark. Only removes rows you added by hand — an application the
        tool actually submitted stays on the record."""
        with self._tx() as conn:
            cursor = conn.execute(
                "DELETE FROM applications WHERE fingerprint = ? AND status = ?",
                (job.fingerprint, Status.APPLIED_MANUALLY.value),
            )
            return cursor.rowcount

    # --- feedback / learning ---

    def mark_irrelevant(self, job: Job, reason: str = "") -> Outcome:
        """Record that a job isn't what you want.

        Two writes on purpose: an `applications` row so it's never applied to or shown
        as new again, and a `feedback` row whose company and title terms are fed back
        into scoring.
        """
        self.record_job(job)
        with self._tx() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO feedback
                       (fingerprint, label, company, title, source, reason, at)
                   VALUES (?, 'irrelevant', ?, ?, ?, ?, ?)""",
                (job.fingerprint, job.company, job.title, job.source, reason, _now()),
            )
        outcome = Outcome(
            job=job, status=Status.IRRELEVANT, reason=reason or "marked not relevant by you"
        )
        self.record_outcome(outcome)
        return outcome

    def clear_feedback(self, job: Job) -> int:
        with self._tx() as conn:
            removed = conn.execute(
                "DELETE FROM feedback WHERE fingerprint = ?", (job.fingerprint,)
            ).rowcount
            conn.execute(
                "DELETE FROM applications WHERE fingerprint = ? AND status = ?",
                (job.fingerprint, Status.IRRELEVANT.value),
            )
        return removed

    def is_irrelevant(self, job: Job) -> bool:
        row = self._query_one(
            "SELECT 1 FROM feedback WHERE fingerprint = ? AND label = 'irrelevant' LIMIT 1",
            (job.fingerprint,),
        )
        return row is not None

    def feedback_signals(self) -> dict[str, dict[str, int]]:
        """Aggregate what you've rejected into signals the Scorer can act on.

        Returns counts of companies and of distinctive title words. Counts matter: one
        rejection is noise, several of the same company or word is a preference.
        """
        rows = self._query_all(
            "SELECT company, title FROM feedback WHERE label = 'irrelevant'"
        )

        companies: dict[str, int] = {}
        terms: dict[str, int] = {}
        for row in rows:
            company = (row["company"] or "").strip().lower()
            if company:
                companies[company] = companies.get(company, 0) + 1
            for word in re.findall(r"[a-z]{3,}", (row["title"] or "").lower()):
                if word not in _GENERIC_TITLE_WORDS:
                    terms[word] = terms.get(word, 0) + 1

        return {"companies": companies, "title_terms": terms, "total": len(rows)}

    def feedback_rows(self, limit: int = 200) -> list[sqlite3.Row]:
        return self._query_all(
            """SELECT f.label, f.company, f.title, f.source, f.reason, f.at
               FROM feedback f ORDER BY f.at DESC LIMIT ?""",
            (limit,),
        )

    def status_for(self, job: Job) -> tuple[str, str, str]:
        """Latest known state of a job as (status, reason, iso_timestamp).

        Returns ("new", "", "") when we've never attempted it. This is what the GUI
        uses to tell you at a glance whether you've already applied.
        """
        row = self._query_one(
            f"""SELECT status, reason, at FROM applications
               WHERE fingerprint = ?
               ORDER BY CASE WHEN status IN ({','.join('?' * len(_TERMINAL))}) THEN 0 ELSE 1 END,
                        at DESC
               LIMIT 1""",
            (job.fingerprint, *_TERMINAL),
        )
        if row is None:
            return "new", "", ""
        return row["status"], row["reason"] or "", row["at"]

    def statuses_for(self, jobs: list[Job]) -> dict[str, tuple[str, str, str]]:
        """Bulk version of status_for — one query instead of N for a full result grid.

        Chunked like `has_applied_bulk` to respect SQLite's bind-variable limit.
        """
        out = {job.fingerprint: ("new", "", "") for job in jobs}
        terminal = ",".join("?" * len(_TERMINAL))
        for chunk in _chunks(jobs):
            fingerprints = [job.fingerprint for job in chunk]
            rows = self._query_all(
                f"""SELECT fingerprint, status, reason, at FROM (
                        SELECT fingerprint, status, reason, at,
                               ROW_NUMBER() OVER (
                                   PARTITION BY fingerprint
                                   ORDER BY CASE WHEN status IN ({terminal})
                                                THEN 0 ELSE 1 END,
                                            at DESC
                               ) AS rn
                        FROM applications
                        WHERE fingerprint IN ({','.join('?' * len(fingerprints))})
                    ) WHERE rn = 1""",
                (*_TERMINAL, *fingerprints),
            )
            for row in rows:
                out[row["fingerprint"]] = (row["status"], row["reason"] or "", row["at"])
        return out

    def pending_manual(self, limit: int = 100) -> list[sqlite3.Row]:
        placeholders = ",".join("?" * len(_TERMINAL))
        return self._query_all(
            f"""SELECT j.company, j.title, j.location, COALESCE(NULLIF(j.apply_url,''), j.url) url,
                      a.reason, a.at
               FROM applications a JOIN jobs j ON j.fingerprint = a.fingerprint
               WHERE a.status IN (?, ?)
                 AND a.fingerprint NOT IN (
                     SELECT fingerprint FROM applications WHERE status IN ({placeholders})
                 )
               ORDER BY a.at DESC LIMIT ?""",
            (Status.MANUAL.value, Status.FAILED.value, *_TERMINAL, limit),
        )

    def stats(self) -> dict[str, int]:
        rows = self._query_all(
            "SELECT status, COUNT(*) c FROM applications GROUP BY status"
        )
        out = {r["status"]: int(r["c"]) for r in rows}
        out["jobs_seen"] = int(self._query_one("SELECT COUNT(*) c FROM jobs")["c"])
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
