"""Tests for manual application tracking, browser selection, LinkedIn parsing and the
Oracle mirror."""

from __future__ import annotations

import pytest

from jobhunter.config import Config
from jobhunter.db.oracle_sink import OracleSink, build_sink
from jobhunter.models import Job, Outcome, Status
from jobhunter.sources.linkedin import LinkedInSource
from jobhunter.store import Store


def make_job(**kwargs) -> Job:
    defaults = dict(source="linkedin", ats="linkedin", company="Acme",
                    title="Support Engineer", url="https://example.com/1", location="Chennai")
    defaults.update(kwargs)
    return Job(**defaults)


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "t.sqlite3")


class TestManualApplicationTracking:
    """The 'I applied to this myself' button — it has to survive restarts and stop the
    tool from applying again."""

    def test_marking_persists_and_blocks_reapplying(self, store):
        job = make_job()
        assert store.status_for(job)[0] == "new"

        store.mark_applied_manually(job, "applied on the company site")
        assert store.status_for(job)[0] == Status.APPLIED_MANUALLY.value
        assert store.has_applied(job)

    def test_marked_job_counts_as_seen(self, store):
        job = make_job()
        store.mark_applied_manually(job)
        assert store.seen_recently(job, within_days=30)

    def test_unmarking_restores_new(self, store):
        job = make_job()
        store.mark_applied_manually(job)
        assert store.clear_manual_mark(job) == 1
        assert store.status_for(job)[0] == "new"
        assert not store.has_applied(job)

    def test_unmarking_does_not_erase_a_real_application(self, store):
        """An application the tool actually submitted must survive an 'undo' click."""
        job = make_job()
        store.record_outcome(Outcome(job=job, status=Status.APPLIED, reason="confirmed"))
        store.mark_applied_manually(job)

        store.clear_manual_mark(job)
        assert store.has_applied(job)
        assert store.status_for(job)[0] == Status.APPLIED.value

    def test_marked_job_leaves_the_manual_queue(self, store):
        job = make_job()
        store.record_outcome(Outcome(job=job, status=Status.MANUAL, reason="captcha"))
        assert len(store.pending_manual()) == 1
        store.mark_applied_manually(job)
        assert len(store.pending_manual()) == 0


class TestFilledForReview:
    def test_filled_is_not_terminal(self, store):
        """Manual-review mode fills a form but you may never submit it — so it must not
        count as an application."""
        job = make_job()
        store.record_outcome(Outcome(job=job, status=Status.FILLED, reason="left open"))
        assert not store.has_applied(job)
        assert store.status_for(job)[0] == Status.FILLED.value

    def test_filled_needs_a_human(self):
        assert Outcome(job=make_job(), status=Status.FILLED).needs_human


class TestFeedbackLearning:
    """Marking jobs irrelevant has to change future results — and must never damage the
    roles the user actually wants."""

    def _scorer(self, signals):
        from jobhunter.matching.scorer import Scorer
        from jobhunter.resume.keywords import ResumeProfile

        cfg = Config(data={"search": {
            "roles": ["Application Support Engineer"],
            "locations": ["Chennai", "India"], "min_score": 0.4, "posted_within_days": None,
        }})
        profile = ResumeProfile(
            keywords={"python": 1.0, "sql": 1.0, "linux": 1.0},
            titles=["application support engineer"], years_experience=6,
        )
        return Scorer(cfg, profile, feedback=signals)

    def _job(self, title, company="Acme"):
        return Job(source="t", company=company, title=title, url="u",
                   location="Chennai, India", description="python sql linux")

    def test_marking_is_persisted(self, store):
        job = make_job()
        store.mark_irrelevant(job, "wrong domain")
        assert store.is_irrelevant(job)
        assert store.status_for(job)[0] == Status.IRRELEVANT.value

    def test_irrelevant_job_is_not_reoffered(self, store):
        job = make_job()
        store.mark_irrelevant(job)
        assert store.seen_recently(job, within_days=30)

    def test_undo_restores_the_job(self, store):
        job = make_job()
        store.mark_irrelevant(job)
        store.clear_feedback(job)
        assert not store.is_irrelevant(job)
        assert store.status_for(job)[0] == "new"

    def test_signals_count_companies_and_terms(self, store):
        for i in range(3):
            store.mark_irrelevant(make_job(title=f"Telecalling Executive {i}", company="BadCorp"))
        signals = store.feedback_signals()
        assert signals["total"] == 3
        assert signals["companies"]["badcorp"] == 3
        assert signals["title_terms"]["telecalling"] == 3

    def test_generic_words_are_not_learned(self, store):
        store.mark_irrelevant(make_job(title="Senior Engineer and Manager"))
        terms = store.feedback_signals()["title_terms"]
        assert "senior" not in terms and "engineer" not in terms and "and" not in terms

    def test_repeatedly_rejected_company_is_filtered(self, store):
        for i in range(3):
            store.mark_irrelevant(make_job(title=f"Role {i}", company="BadCorp"))
        scorer = self._scorer(store.feedback_signals())
        result = scorer.score(self._job("Application Support Engineer", company="BadCorp"))
        assert not result.passed
        assert "not relevant" in result.rejected_because

    def test_one_rejection_is_treated_as_noise(self, store):
        store.mark_irrelevant(make_job(title="Role", company="BadCorp"))
        scorer = self._scorer(store.feedback_signals())
        assert scorer.score(self._job("Application Support Engineer", company="BadCorp")).passed

    def test_lookalike_titles_are_penalised(self, store):
        for i, company in enumerate(["A", "B", "C", "D"]):
            store.mark_irrelevant(make_job(title=f"Voice Process Telecaller {i}", company=company))
        scorer = self._scorer(store.feedback_signals())

        clean = scorer.score(self._job("Application Support Engineer")).score
        lookalike = scorer.score(self._job("Voice Process Support Engineer")).score
        assert lookalike < clean

    def test_target_role_words_never_become_negative_signals(self, store):
        """Rejecting "Voice Process Support Engineer" must not teach it that 'support'
        is bad — that would bury the user's own target role."""
        for i, company in enumerate(["A", "B", "C", "D"]):
            store.mark_irrelevant(make_job(title=f"Voice Process Support {i}", company=company))
        scorer = self._scorer(store.feedback_signals())

        assert "support" in scorer.protected_terms
        result = scorer.score(self._job("Application Support Engineer"))
        assert result.passed
        assert result.score > 0.9

    def test_penalty_is_capped(self, store):
        for i, company in enumerate(["A", "B", "C", "D", "E"]):
            store.mark_irrelevant(
                make_job(title=f"Voice Process Telecalling Bpo Chat {i}", company=company))
        scorer = self._scorer(store.feedback_signals())
        penalty, _terms = scorer._feedback_penalty(
            self._job("Voice Process Telecalling Bpo Chat Engineer"))
        assert penalty <= scorer.max_feedback_penalty

    def test_no_feedback_means_no_penalty(self, store):
        scorer = self._scorer(store.feedback_signals())
        assert scorer._feedback_penalty(self._job("Anything At All")) == (0.0, [])


class TestBrowserDetection:
    def test_bundled_chromium_is_always_offered(self):
        from jobhunter.browser import detect_browsers

        assert any(b.key == "chromium" for b in detect_browsers())

    def test_detected_browsers_are_usable(self):
        from jobhunter.browser import detect_browsers

        for browser in detect_browsers():
            # Either Playwright knows it by channel, or we have a real path to run.
            assert browser.key == "chromium" or browser.channel or browser.executable

    def test_chromium_choice_clears_channel_and_executable(self):
        cfg = Config(data={"apply": {"browser": "chromium"}, "paths": {"state_dir": ".state"}})
        from jobhunter.browser import resolve_browser

        channel, executable, _profile = resolve_browser(cfg)
        assert channel == "" and executable == ""

    def test_unknown_browser_key_falls_back_safely(self):
        cfg = Config(data={"apply": {"browser": "netscape"}, "paths": {"state_dir": ".state"}})
        from jobhunter.browser import resolve_browser

        channel, executable, profile = resolve_browser(cfg)
        assert channel == "" and executable == "" and profile


class TestLinkedInParsing:
    CARD = """
    <li>
      <div class="base-card">
        <a class="base-card__full-link" href="https://in.linkedin.com/jobs/view/support-eng-123?trk=abc">
          <h3 class="base-search-card__title">Application Support Engineer</h3>
        </a>
        <h4 class="base-search-card__subtitle"><a>BNP Paribas</a></h4>
        <span class="job-search-card__location">Chennai, Tamil Nadu, India</span>
        <time datetime="2026-08-03">5 days ago</time>
      </div>
    </li>
    """

    def test_card_is_parsed(self):
        jobs = LinkedInSource({})._parse(self.CARD)
        assert len(jobs) == 1
        job = jobs[0]
        assert job.title == "Application Support Engineer"
        assert job.company == "BNP Paribas"
        assert job.location == "Chennai, Tamil Nadu, India"
        assert job.posted_at is not None

    def test_tracking_parameters_are_stripped(self):
        """The bare URL is what makes de-duplication across runs work."""
        assert "?" not in LinkedInSource({})._parse(self.CARD)[0].url

    def test_cards_without_a_link_are_ignored(self):
        assert LinkedInSource({})._parse("<li><h3>Ghost job</h3></li>") == []

    def test_age_filter_maps_to_linkedin_values(self):
        source = LinkedInSource({"posted_within_days": 7})
        assert "f_TPR=r604800" in source._params("x", "Chennai", 0)

    def test_remote_only_sets_the_filter(self):
        assert "f_WT=2" in LinkedInSource({"remote_only": True})._params("x", "", 0)


class TestOracleSink:
    def test_disabled_returns_none(self):
        assert build_sink(Config(data={"database": {"oracle": {"enabled": False}}})) is None

    def test_missing_credentials_do_not_raise(self):
        """A misconfigured mirror must never take the run down with it."""
        cfg = Config(data={"database": {"oracle": {"enabled": True, "user": "", "password": ""}}})
        assert build_sink(cfg) is None

    def test_table_name_is_sanitised(self):
        """The table name is interpolated into DDL, so it must not carry punctuation."""
        assert OracleSink("u", "p", "d", table="bad;DROP TABLE x--").table == "BADDROPTABLEX"

    def test_default_table_when_blank(self):
        assert OracleSink("u", "p", "d", table="").table == "JOBHUNTER_APPLICATIONS"

    def test_recording_without_a_connection_is_a_no_op(self):
        sink = OracleSink("u", "p", "d")
        assert sink.record(Outcome(job=make_job(), status=Status.APPLIED)) is False


class TestSourceSelection:
    def test_only_filters_sources(self):
        from jobhunter.sources import build_sources

        cfg = Config(data={"sources": {
            "greenhouse": {"enabled": True, "boards": ["stripe"]},
            "lever": {"enabled": True, "companies": ["ro"]},
            "linkedin": {"enabled": True},
        }})
        names = {s.name for s in build_sources(cfg, only=["greenhouse"])}
        assert names == {"greenhouse"}

    def test_no_filter_builds_everything_enabled(self):
        from jobhunter.sources import build_sources

        cfg = Config(data={"sources": {
            "greenhouse": {"enabled": True, "boards": ["stripe"]},
            "linkedin": {"enabled": True},
        }})
        assert {s.name for s in build_sources(cfg)} == {"greenhouse", "linkedin"}

    def test_disabled_source_stays_off_even_if_requested(self):
        from jobhunter.sources import build_sources

        cfg = Config(data={"sources": {"linkedin": {"enabled": False}}})
        assert build_sources(cfg, only=["linkedin"]) == []
