"""Regression tests for the discovery pipeline — chiefly the emit/sweep interaction.

Every real source streams its jobs through `on_jobs` as it finds them *and* returns
the full list. A previous version of `_run_serial`/`_run_parallel` fed that returned
list through the scorer on top of the streamed chunks, doubling the reported count
in `discover` and the scoring work alongside it.
"""

from __future__ import annotations

from jobhunter.config import Config
from jobhunter.models import Job
from jobhunter.pipeline import Pipeline
from jobhunter.store import Store


class EmittingSource:
    """Streams chunks via on_jobs and returns the union — like every real source."""

    name = "emitting"
    browser = None

    def __init__(self, n: int, chunk: int = 3):
        self.n = n
        self.chunk = chunk
        self.on_jobs = None

    def fetch(self, queries: list[str]) -> list[Job]:
        jobs = [
            Job(source=self.name, company=f"Co{i % 5}", title=f"Support Engineer {i}",
                url=f"https://x/{i}", location="Chennai")
            for i in range(self.n)
        ]
        for i in range(0, self.n, self.chunk):
            if self.on_jobs:
                self.on_jobs(jobs[i:i + self.chunk])
        return jobs


class SilentSource:
    """Returns jobs without ever emitting — the case the sweep exists for."""

    name = "silent"
    browser = None

    def __init__(self, n: int):
        self.n = n
        self.on_jobs = None

    def fetch(self, queries: list[str]) -> list[Job]:
        return [
            Job(source=self.name, company="S", title=f"Role {i}", url=f"https://s/{i}",
                location="Pune")
            for i in range(self.n)
        ]


def _pipeline(tmp_path) -> Pipeline:
    cfg = Config(data={"paths": {"data_dir": str(tmp_path)}})
    return Pipeline(cfg, store=Store(tmp_path / "p.sqlite3"))


class TestEmitSweep:
    def test_emitting_source_is_not_double_counted_serial(self, tmp_path):
        seen: list[list[Job]] = []
        _pipeline(tmp_path)._run_serial([EmittingSource(5)], [], seen.append, {})
        # 5 emitted + the 5-return sweep = 10 before the fix.
        assert sum(len(b) for b in seen) == 5

    def test_emitting_source_is_not_double_counted_parallel(self, tmp_path):
        seen: list[list[Job]] = []
        _pipeline(tmp_path)._run_parallel([EmittingSource(5)], [], seen.append, {})
        assert sum(len(b) for b in seen) == 5

    def test_silent_source_is_still_swept(self, tmp_path):
        seen: list[list[Job]] = []
        _pipeline(tmp_path)._run_serial([SilentSource(4)], [], seen.append, {})
        assert sum(len(b) for b in seen) == 4

    def test_discover_reports_the_real_count(self, tmp_path, monkeypatch):
        import jobhunter.pipeline as pl

        p = _pipeline(tmp_path)
        monkeypatch.setattr(pl, "build_sources", lambda *a, **k: [EmittingSource(5)])
        jobs, errors = p.discover([], None)
        assert errors == {}
        assert len(jobs) == 5  # was 5 returned / 10 reported before the fix
