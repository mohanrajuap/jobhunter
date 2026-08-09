"""Mirror every application into Oracle.

SQLite stays the source of truth for de-duplication — it's local, always available, and
a run must not depend on a database being up. Oracle is a *mirror*, for querying your
application history with real SQL, joining it to whatever else you track, and building
reports. Every write here is best-effort: if the database is down the run continues and
the failure is logged.

Uses python-oracledb in thin mode, so no Oracle Instant Client install is needed.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import MatchResult, Outcome

log = logging.getLogger(__name__)

_DEFAULT_TABLE = "JOBHUNTER_APPLICATIONS"

# Oracle has no "CREATE TABLE IF NOT EXISTS"; catching ORA-00955 is the idiom.
_TABLE_EXISTS_ERRORS = ("ORA-00955",)

_CREATE_TABLE = """
CREATE TABLE {table} (
    id            NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fingerprint   VARCHAR2(64)   NOT NULL,
    applied_at    TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    status        VARCHAR2(32)   NOT NULL,
    company       VARCHAR2(400),
    title         VARCHAR2(400),
    location      VARCHAR2(400),
    source        VARCHAR2(64),
    ats           VARCHAR2(64),
    job_url       VARCHAR2(2000),
    role_name     VARCHAR2(200),
    resume_label  VARCHAR2(200),
    match_score   NUMBER(5, 4),
    apply_mode    VARCHAR2(16),
    reason        VARCHAR2(1000),
    screenshot    VARCHAR2(1000)
)
"""

_CREATE_INDEX = "CREATE INDEX {table}_fp_idx ON {table} (fingerprint)"

_INSERT = """
INSERT INTO {table}
    (fingerprint, applied_at, status, company, title, location, source, ats,
     job_url, role_name, resume_label, match_score, apply_mode, reason, screenshot)
VALUES
    (:fingerprint, :applied_at, :status, :company, :title, :location, :source, :ats,
     :job_url, :role_name, :resume_label, :match_score, :apply_mode, :reason, :screenshot)
"""


def _clip(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:limit] if text else None


class OracleSink:
    def __init__(
        self,
        user: str,
        password: str,
        dsn: str,
        table: str = _DEFAULT_TABLE,
        create_table: bool = True,
    ):
        self.user = user
        self.password = password
        self.dsn = dsn
        # Interpolated into DDL, so it must not be attacker-controlled — restrict it to
        # an identifier-shaped string rather than trusting the config blindly.
        self.table = "".join(c for c in (table or _DEFAULT_TABLE) if c.isalnum() or c == "_").upper()
        self.create_table = create_table
        self._conn: Any = None
        self._ready = False

    # --- connection ---

    def connect(self) -> bool:
        try:
            import oracledb
        except ImportError:
            log.error("Oracle logging is enabled but python-oracledb is not installed "
                      "— run: pip install oracledb")
            return False

        if not self.user or not self.password or not self.dsn:
            log.error("Oracle logging is enabled but user/password/dsn are incomplete "
                      "(set ORACLE_PASSWORD in .env)")
            return False

        try:
            self._conn = oracledb.connect(user=self.user, password=self.password, dsn=self.dsn)
            log.info("Connected to Oracle as %s@%s", self.user, self.dsn)
        except Exception as exc:
            log.error("Could not connect to Oracle (%s@%s): %s", self.user, self.dsn, exc)
            return False

        self._ready = self._ensure_schema() if self.create_table else True
        return self._ready

    def _ensure_schema(self) -> bool:
        try:
            with self._conn.cursor() as cursor:
                for statement in (
                    _CREATE_TABLE.format(table=self.table),
                    _CREATE_INDEX.format(table=self.table),
                ):
                    try:
                        cursor.execute(statement)
                    except Exception as exc:
                        # Already there is the normal case on every run after the first.
                        if not any(code in str(exc) for code in _TABLE_EXISTS_ERRORS):
                            raise
            self._conn.commit()
            log.info("Oracle table %s is ready", self.table)
            return True
        except Exception as exc:
            log.error("Could not create the Oracle table %s: %s", self.table, exc)
            return False

    # --- writes ---

    def record(self, outcome: Outcome, match: MatchResult | None = None, mode: str = "") -> bool:
        """Write one application row. Returns False on failure without raising."""
        if not self._ready:
            return False

        job = outcome.job
        params = {
            "fingerprint": job.fingerprint,
            "applied_at": outcome.at,
            "status": outcome.status.value,
            "company": _clip(job.company, 400),
            "title": _clip(job.title, 400),
            "location": _clip(job.location, 400),
            "source": _clip(job.source, 64),
            "ats": _clip(job.ats, 64),
            "job_url": _clip(job.target_url, 2000),
            "role_name": _clip(match.role_name if match else "", 200),
            "resume_label": _clip(match.resume_label if match else "", 200),
            "match_score": round(float(outcome.score or 0.0), 4),
            "apply_mode": _clip(mode, 16),
            "reason": _clip(outcome.reason, 1000),
            "screenshot": _clip(outcome.screenshot, 1000),
        }

        try:
            with self._conn.cursor() as cursor:
                cursor.execute(_INSERT.format(table=self.table), params)
            self._conn.commit()
            return True
        except Exception as exc:
            log.error("Oracle insert failed for %s @ %s: %s", job.title, job.company, exc)
            try:
                self._conn.rollback()
            except Exception:
                pass
            return False

    def count(self) -> int:
        if not self._ready:
            return 0
        try:
            with self._conn.cursor() as cursor:
                cursor.execute(f"SELECT COUNT(*) FROM {self.table}")
                return int(cursor.fetchone()[0])
        except Exception as exc:
            log.debug("Oracle count failed: %s", exc)
            return 0

    def recent(self, limit: int = 20) -> list[dict]:
        """Most recent rows — used by `jobhunter stats` to prove the mirror is working."""
        if not self._ready:
            return []
        try:
            with self._conn.cursor() as cursor:
                cursor.execute(
                    f"""SELECT applied_at, status, title, company, role_name
                        FROM {self.table} ORDER BY applied_at DESC
                        FETCH FIRST :n ROWS ONLY""",
                    {"n": limit},
                )
                columns = [c[0].lower() for c in cursor.description]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except Exception as exc:
            log.debug("Oracle recent() failed: %s", exc)
            return []

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None
        self._ready = False


def build_sink(config: Any) -> OracleSink | None:
    """Create and connect the Oracle sink if it's enabled. Returns None otherwise."""
    if not config.get("database.oracle.enabled", False):
        return None

    sink = OracleSink(
        user=config.get("database.oracle.user", ""),
        password=config.get("database.oracle.password", ""),
        dsn=config.get("database.oracle.dsn", "localhost:1521/FREEPDB1"),
        table=config.get("database.oracle.table", _DEFAULT_TABLE),
        create_table=bool(config.get("database.oracle.create_table", True)),
    )
    if sink.connect():
        return sink

    log.warning("Oracle logging is enabled but unavailable — the run continues, "
                "and everything is still recorded in SQLite")
    return None
