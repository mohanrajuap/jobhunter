"""Tests for de-duplication and application history.

If these break, the tool re-applies to jobs it already applied to — the single most
embarrassing failure mode it has.
"""

from __future__ import annotations

import pytest

from jobhunter.models import Job, Outcome, Status
from jobhunter.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "test.sqlite3")


def make_job(**kwargs) -> Job:
    defaults = dict(
        source="greenhouse", ats="greenhouse", company="Acme",
        title="Support Engineer", url="https://example.com/1", location="Chennai",
    )
    defaults.update(kwargs)
    return Job(**defaults)


class TestFingerprint:
    def test_same_role_from_two_sources_shares_a_fingerprint(self):
        """The whole point: a job found on both Naukri and the company board is one job."""
        a = make_job(source="naukri", url="https://naukri.com/x")
        b = make_job(source="greenhouse", url="https://boards.greenhouse.io/y")
        assert a.fingerprint == b.fingerprint

    def test_different_titles_differ(self):
        assert make_job(title="Support Engineer").fingerprint != make_job(title="SRE").fingerprint

    def test_punctuation_and_case_are_ignored(self):
        a = make_job(company="Acme Corp.", title="Support Engineer")
        b = make_job(company="ACME  CORP", title="support engineer")
        assert a.fingerprint == b.fingerprint


class TestApplicationHistory:
    def test_new_job_is_new(self, store):
        assert store.status_for(make_job())[0] == "new"
        assert not store.has_applied(make_job())

    def test_applied_job_is_remembered(self, store):
        job = make_job()
        store.record_outcome(Outcome(job=job, status=Status.APPLIED, reason="confirmed"))
        assert store.has_applied(job)
        assert store.status_for(job)[0] == "applied"

    def test_manual_outcome_does_not_count_as_applied(self, store):
        job = make_job()
        store.record_outcome(Outcome(job=job, status=Status.MANUAL, reason="captcha"))
        assert not store.has_applied(job)
        assert store.status_for(job)[0] == "manual"

    def test_applied_wins_over_a_later_manual_row(self, store):
        """Order matters: an applied row must not be masked by a subsequent failure."""
        job = make_job()
        store.record_outcome(Outcome(job=job, status=Status.APPLIED))
        store.record_outcome(Outcome(job=job, status=Status.MANUAL, reason="retry noise"))
        assert store.status_for(job)[0] == "applied"
        assert store.has_applied(job)

    def test_seen_recently_covers_non_applied_attempts(self, store):
        job = make_job()
        store.record_outcome(Outcome(job=job, status=Status.MANUAL, reason="captcha"))
        assert store.seen_recently(job, within_days=30)
        assert not store.seen_recently(make_job(title="Other Role"), within_days=30)


class TestCaps:
    def test_daily_count_tracks_applied_only(self, store):
        store.record_outcome(Outcome(job=make_job(title="A"), status=Status.APPLIED))
        store.record_outcome(Outcome(job=make_job(title="B"), status=Status.APPLIED))
        store.record_outcome(Outcome(job=make_job(title="C"), status=Status.MANUAL))
        assert store.applications_today() == 2

    def test_per_company_count_is_case_insensitive(self, store):
        store.record_outcome(Outcome(job=make_job(company="Acme", title="A"), status=Status.APPLIED))
        store.record_outcome(Outcome(job=make_job(company="ACME", title="B"), status=Status.APPLIED))
        store.record_outcome(Outcome(job=make_job(company="Other", title="C"), status=Status.APPLIED))
        assert store.applications_today_for_company("acme") == 2


class TestManualQueue:
    def test_manual_items_are_listed(self, store):
        store.record_outcome(Outcome(job=make_job(title="A"), status=Status.MANUAL, reason="captcha"))
        store.record_outcome(Outcome(job=make_job(title="B"), status=Status.FAILED, reason="boom"))
        assert len(store.pending_manual()) == 2

    def test_manual_item_disappears_once_applied(self, store):
        job = make_job()
        store.record_outcome(Outcome(job=job, status=Status.MANUAL, reason="captcha"))
        assert len(store.pending_manual()) == 1
        store.record_outcome(Outcome(job=job, status=Status.APPLIED))
        assert len(store.pending_manual()) == 0


def _bulk_jobs(n: int) -> list[Job]:
    return [Job(source="greenhouse", ats="greenhouse", company=f"Co{i % 20}",
                title=f"Support Engineer {i}", url=f"https://x/{i}", location="Chennai")
            for i in range(n)]


class TestBulkQueries:
    """The batch store methods must agree exactly with the per-job ones, and must keep
    working when a run hands them more fingerprints than one SQLite query can bind."""

    def test_has_applied_bulk_matches_per_job(self, store):
        jobs = _bulk_jobs(60)
        store.record_jobs(jobs)
        for i in range(0, 60, 7):
            store.record_outcome(Outcome(job=jobs[i], status=Status.APPLIED))

        expect = {j.fingerprint for j in jobs if store.has_applied(j)}
        assert store.has_applied_bulk([j.fingerprint for j in jobs]) == expect

    def test_seen_recently_bulk_matches_per_job(self, store):
        jobs = _bulk_jobs(60)
        store.record_jobs(jobs)
        for i in range(1, 60, 5):
            store.record_outcome(Outcome(job=jobs[i], status=Status.MANUAL, reason="captcha"))

        expect = {j.fingerprint for j in jobs if store.seen_recently(j, 30)}
        assert store.seen_recently_bulk([j.fingerprint for j in jobs], 30) == expect

    def test_statuses_for_matches_per_job(self, store):
        jobs = _bulk_jobs(80)
        store.record_jobs(jobs)
        store.record_outcome(Outcome(job=jobs[0], status=Status.APPLIED, reason="confirmed"))
        store.record_outcome(Outcome(job=jobs[0], status=Status.MANUAL, reason="retry noise"))
        store.record_outcome(Outcome(job=jobs[1], status=Status.MANUAL, reason="captcha"))

        got = store.statuses_for(jobs)
        for job in jobs:
            assert got[job.fingerprint] == store.status_for(job)

    def test_bulk_spans_multiple_chunked_queries(self, store):
        """>900 fingerprints forces _chunks to issue more than one IN (...) query."""
        jobs = _bulk_jobs(950)
        store.record_jobs(jobs)
        for i in range(0, 950, 100):
            store.record_outcome(Outcome(job=jobs[i], status=Status.APPLIED))

        applied = store.has_applied_bulk([j.fingerprint for j in jobs])
        assert len(applied) == 10
        assert all(jobs[i].fingerprint in applied for i in range(0, 950, 100))

        statuses = store.statuses_for(jobs)
        assert statuses[jobs[0].fingerprint][0] == "applied"
        assert statuses[jobs[900].fingerprint][0] == "applied"
        assert statuses[jobs[940].fingerprint][0] == "new"
        assert len(statuses) == 950

    def test_applications_today_by_company_groups_case_insensitively(self, store):
        store.record_outcome(Outcome(job=make_job(company="Acme", title="A"), status=Status.APPLIED))
        store.record_outcome(Outcome(job=make_job(company="ACME", title="B"), status=Status.APPLIED))
        store.record_outcome(Outcome(job=make_job(company="Other", title="C"), status=Status.APPLIED))
        store.record_outcome(Outcome(job=make_job(company="Other", title="D"), status=Status.MANUAL))
        assert store.applications_today_by_company() == {"acme": 2, "other": 1}

    def test_chunks_splits_by_size(self):
        from jobhunter.store import _chunks

        assert [list(c) for c in _chunks(list(range(10)), 4)] == [
            [0, 1, 2, 3], [4, 5, 6, 7], [8, 9],
        ]
        assert list(_chunks([], 900)) == []
