"""Tests for the filtering and scoring rules — the logic that decides what you apply to.

These cover the mistakes that actually cost something: applying to a job you can't
legally take, or silently discarding good jobs over a substring collision.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jobhunter.config import Config
from jobhunter.matching.scorer import Scorer
from jobhunter.models import Job
from jobhunter.resume.keywords import ResumeProfile

NOW = datetime.now(timezone.utc)


def make_config(**search) -> Config:
    base = {
        "roles": ["Application Support Engineer"],
        "exclude_keywords": ["intern"],
        "locations": ["Chennai", "Bangalore", "India"],
        "remote_ok": True,
        "min_score": 0.5,
        "posted_within_days": 30,
    }
    base.update(search)
    return Config(data={"search": base})


def make_profile() -> ResumeProfile:
    return ResumeProfile(
        keywords={"python": 1.0, "sql": 1.0, "linux": 1.0, "incident management": 1.0,
                  "oracle": 0.8, "splunk": 0.8},
        titles=["application support engineer"],
        years_experience=6,
    )


def make_job(**kwargs) -> Job:
    defaults = dict(
        source="test", company="Acme", title="Application Support Engineer",
        url="https://example.com/job", location="Chennai, India",
        description="python sql linux incident management", posted_at=NOW,
    )
    defaults.update(kwargs)
    return Job(**defaults)


@pytest.fixture
def scorer() -> Scorer:
    return Scorer(make_config(), make_profile())


class TestExclusions:
    def test_real_intern_role_is_rejected(self, scorer):
        result = scorer.score(make_job(title="Software Intern", description="intern programme"))
        assert not result.passed
        assert "intern" in result.rejected_because

    @pytest.mark.parametrize("word", ["internal", "international", "internship-free"])
    def test_words_containing_an_excluded_term_survive(self, scorer, word):
        """'intern' must not knock out 'internal tools' — this cost 819 jobs in testing."""
        result = scorer.score(make_job(title=f"Engineer, {word.title()} Tools"))
        assert result.rejected_because is None or "excluded keyword" not in result.rejected_because


class TestLocation:
    def test_preferred_location_passes(self, scorer):
        assert scorer.score(make_job(location="Chennai, India")).passed

    def test_fully_remote_passes(self, scorer):
        assert scorer.score(make_job(location="Remote")).passed

    @pytest.mark.parametrize(
        "location",
        ["Remote - United States", "Remote-Friendly | San Francisco, CA",
         "Remote (US)", "Remote - Europe", "San Francisco, CA"],
    )
    def test_region_locked_remote_is_rejected(self, scorer, location):
        """'Remote' inside the US is not remote from India."""
        result = scorer.score(make_job(location=location))
        assert not result.passed
        assert "location" in result.rejected_because

    def test_remote_in_a_preferred_country_passes(self, scorer):
        assert scorer.score(make_job(location="Remote - India")).passed

    def test_unknown_location_is_not_rejected_outright(self, scorer):
        assert scorer.score(make_job(location="")).passed


class TestTitleMatching:
    def test_exact_title_scores_high(self, scorer):
        assert scorer.score(make_job(title="Application Support Engineer")).score > 0.8

    def test_one_word_difference_is_penalised(self, scorer):
        """Support vs Security is a different job despite the token overlap."""
        support = scorer.score(make_job(title="Application Support Engineer")).score
        security = scorer.score(make_job(title="Application Security Engineer")).score
        assert security < support

    def test_strict_mode_rejects_missing_anchor(self):
        strict = Scorer(make_config(strict_title_match=True), make_profile())
        assert not strict.score(make_job(title="Application Security Engineer")).passed
        assert strict.score(make_job(title="Application Support Engineer")).passed


class TestExperience:
    def test_job_demanding_far_more_experience_is_rejected(self, scorer):
        result = scorer.score(make_job(title="Support Engineer", description="12+ years required"))
        assert not result.passed
        assert "years" in result.rejected_because

    def test_structured_range_from_source_is_used(self, scorer):
        result = scorer.score(make_job(min_experience_years=15, max_experience_years=20))
        assert not result.passed

    def test_range_inside_profile_is_accepted(self, scorer):
        assert scorer.score(make_job(min_experience_years=4, max_experience_years=8)).passed


class TestRoleTitlesDriveScoring:
    """A role's own titles must win over the global `search.roles` list.

    Preferring the global list meant every role in a multi-role config was scored
    against the wrong titles — picking a Java role still matched against "Application
    Support Engineer", so a perfect "Java Developer" hit scored 0.22 and was dropped.
    """

    def _java_scorer(self):
        config = make_config(roles=["Application Support Engineer", "Site Reliability Engineer"])
        java_profile = ResumeProfile(
            keywords={"java": 1.0, "spring boot": 1.0, "microservices": 0.8},
            titles=["java full stack developer", "java developer"],
            years_experience=5,
        )
        return Scorer(config, java_profile)

    def test_role_titles_are_used(self):
        assert self._java_scorer().target_titles == ["java full stack developer", "java developer"]

    def test_exact_role_title_scores_high(self):
        result = self._java_scorer().score(make_job(title="Java Developer",
                                                    description="java spring boot microservices"))
        assert result.passed
        assert result.score > 0.6

    def test_other_roles_title_is_rejected(self):
        result = self._java_scorer().score(make_job(title="Application Support Engineer",
                                                    description="itil servicenow"))
        assert not result.passed

    def test_global_roles_used_when_profile_has_no_titles(self):
        """Configs with no `roles:` block must keep working."""
        scorer = Scorer(make_config(roles=["Application Support Engineer"]),
                        ResumeProfile(keywords={"python": 1.0}, titles=[], years_experience=5))
        assert scorer.target_titles == ["application support engineer"]


class TestExperienceHandling:
    def _scorer(self, years, **search):
        profile = ResumeProfile(keywords={"java": 1.0}, titles=["java developer"],
                                years_experience=years)
        return Scorer(make_config(**search), profile)

    def test_stated_requirement_within_slack_is_accepted(self):
        """3.5 years against a "5+ years" posting is a normal stretch — posted
        requirements are routinely inflated."""
        scorer = self._scorer(3.5, experience_slack_years=2.0)
        job = make_job(title="Java Developer", min_experience_years=5, max_experience_years=10)
        assert scorer.score(job).passed

    def test_far_beyond_slack_is_still_rejected(self):
        scorer = self._scorer(3.5, experience_slack_years=2.0)
        job = make_job(title="Java Developer", min_experience_years=12, max_experience_years=18)
        assert not scorer.score(job).passed

    def test_ignore_experience_skips_the_check(self):
        scorer = self._scorer(3.5, ignore_experience=True)
        job = make_job(title="Java Developer", min_experience_years=15, max_experience_years=20)
        assert scorer.score(job).passed

    def test_unknown_experience_never_rejects(self):
        scorer = self._scorer(None)
        assert scorer.score(make_job(title="Java Developer", min_experience_years=10)).passed


class TestFreshness:
    def test_stale_posting_is_rejected(self, scorer):
        old = NOW - timedelta(days=90)
        result = scorer.score(make_job(posted_at=old))
        assert not result.passed
        assert "days ago" in result.rejected_because

    def test_fresh_beats_old_on_score(self, scorer):
        fresh = scorer.score(make_job(posted_at=NOW)).score
        older = scorer.score(make_job(posted_at=NOW - timedelta(days=20))).score
        assert fresh > older


class TestThreshold:
    def test_below_threshold_is_rejected(self):
        picky = Scorer(make_config(min_score=0.95), make_profile())
        result = picky.score(make_job(title="Warehouse Operative", description="lifting"))
        assert not result.passed

    def test_rank_returns_only_passing_sorted_desc(self, scorer):
        jobs = [
            make_job(title="Application Support Engineer", company="A"),
            make_job(title="Software Intern", company="B"),
            make_job(title="Production Support Engineer", company="C"),
        ]
        ranked = scorer.rank(jobs)
        assert all(r.passed for r in ranked)
        assert ranked == sorted(ranked, key=lambda r: -r.score)


class TestBlocklist:
    def test_blocked_company_is_rejected(self):
        scorer = Scorer(make_config(blocked_companies=["acme"]), make_profile())
        result = scorer.score(make_job(company="Acme"))
        assert not result.passed
        assert "blocklist" in result.rejected_because
