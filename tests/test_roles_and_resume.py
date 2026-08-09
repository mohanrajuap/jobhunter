"""Tests for resume parsing, role targeting and resume-per-job selection."""

from __future__ import annotations

import pytest

from jobhunter.config import Config
from jobhunter.matching.multi import MultiRoleScorer
from jobhunter.models import Job
from jobhunter.resume.keywords import build_profile, extract_titles, extract_years
from jobhunter.roles import ResumeVariant, RoleTarget, load_roles

SUPPORT_TEXT = """
Jane Doe — Senior Application Support Engineer
6 years of experience in production support and incident management.
Skills: Python, SQL, Oracle, ServiceNow, Splunk, ITIL, Linux
Led L3 production support for the payments platform.
"""

DEVOPS_TEXT = """
Jane Doe — Site Reliability Engineer
Skills: Kubernetes, Docker, Terraform, AWS, Prometheus, Grafana, Jenkins, Python
Built a Kubernetes platform on AWS with Terraform.
"""


class TestResumeParsing:
    def test_keywords_are_extracted(self):
        profile = build_profile(SUPPORT_TEXT)
        assert "python" in profile.keywords
        assert "servicenow" in profile.keywords
        assert "incident management" in profile.keywords

    def test_years_of_experience_detected(self):
        assert extract_years(SUPPORT_TEXT) == 6.0

    def test_prose_is_not_mistaken_for_a_job_title(self):
        """'led l3 production support' and 'of experience in production support' match the
        title shape but are not titles."""
        titles = extract_titles(SUPPORT_TEXT)
        assert all("led" not in t.split() for t in titles)
        assert all("experience" not in t.split() for t in titles)

    def test_config_keywords_outrank_inferred_ones(self):
        profile = build_profile(SUPPORT_TEXT, extra_keywords=["kafka"])
        assert profile.keywords["kafka"] == 1.0


class TestResumeVariantSelection:
    def _variants(self):
        return [
            ResumeVariant(path=__import__("pathlib").Path("support.txt"), label="support",
                          profile=build_profile(SUPPORT_TEXT)),
            ResumeVariant(path=__import__("pathlib").Path("devops.txt"), label="devops",
                          profile=build_profile(DEVOPS_TEXT)),
        ]

    def test_kubernetes_job_picks_the_devops_resume(self):
        role = RoleTarget(name="R", titles=["engineer"], resumes=self._variants())
        job = Job(source="t", company="A", title="SRE", url="u",
                  description="kubernetes terraform aws prometheus grafana docker")
        assert role.best_resume(job).label == "devops"

    def test_support_job_picks_the_support_resume(self):
        role = RoleTarget(name="R", titles=["engineer"], resumes=self._variants())
        job = Job(source="t", company="A", title="Support", url="u",
                  description="servicenow itil incident management oracle splunk production support")
        assert role.best_resume(job).label == "support"

    def test_single_resume_is_always_chosen(self):
        role = RoleTarget(name="R", titles=["engineer"], resumes=self._variants()[:1])
        job = Job(source="t", company="A", title="Anything", url="u", description="unrelated")
        assert role.best_resume(job).label == "support"

    def test_role_without_resumes_returns_none(self):
        role = RoleTarget(name="R", titles=["engineer"], resumes=[])
        assert role.best_resume(Job(source="t", company="A", title="X", url="u")) is None


class TestMultiRoleScoring:
    def _scorer(self, tmp_path):
        support = tmp_path / "support.txt"
        support.write_text(SUPPORT_TEXT, encoding="utf-8")
        devops = tmp_path / "devops.txt"
        devops.write_text(DEVOPS_TEXT, encoding="utf-8")

        cfg = Config(data={
            "search": {"locations": ["Chennai", "India"], "remote_ok": True, "min_score": 0.4,
                       "exclude_keywords": [], "posted_within_days": None},
            "roles": [
                {"name": "Support", "titles": ["Application Support Engineer"],
                 "resumes": [{"path": str(support), "label": "support"}]},
                {"name": "SRE", "titles": ["Site Reliability Engineer"],
                 "resumes": [{"path": str(devops), "label": "devops"}]},
            ],
        })
        return MultiRoleScorer(cfg, load_roles(cfg))

    def test_winning_role_selects_its_own_resume(self, tmp_path):
        scorer = self._scorer(tmp_path)
        job = Job(source="t", company="A", title="Site Reliability Engineer",
                  url="u", location="Chennai, India",
                  description="kubernetes terraform aws prometheus grafana")
        result = scorer.score(job)
        assert result.role_name == "SRE"
        assert result.resume_label == "devops"

    def test_each_role_can_win(self, tmp_path):
        scorer = self._scorer(tmp_path)
        job = Job(source="t", company="A", title="Application Support Engineer",
                  url="u", location="Chennai, India",
                  description="servicenow itil incident management oracle splunk")
        result = scorer.score(job)
        assert result.role_name == "Support"
        assert result.resume_label == "support"

    def test_disabled_role_is_skipped(self, tmp_path):
        support = tmp_path / "s.txt"
        support.write_text(SUPPORT_TEXT, encoding="utf-8")
        cfg = Config(data={
            "search": {"min_score": 0.0},
            "roles": [{"name": "Off", "titles": ["X"], "enabled": False,
                       "resumes": [{"path": str(support)}]}],
        })
        assert load_roles(cfg) == []


class TestRoleSelection:
    """The Search tab's Role picker. Choosing one role must narrow both what is
    searched for and which resume is sent."""

    def _pipeline(self, tmp_path):
        from jobhunter.pipeline import Pipeline
        from jobhunter.store import Store

        support = tmp_path / "support.txt"
        support.write_text(SUPPORT_TEXT, encoding="utf-8")
        devops = tmp_path / "devops.txt"
        devops.write_text(DEVOPS_TEXT, encoding="utf-8")

        cfg = Config(data={
            "paths": {"data_dir": str(tmp_path)},
            "search": {"max_queries_per_role": 4},
            "roles": [
                {"name": "Support", "titles": ["Application Support Engineer"],
                 "resumes": [{"path": str(support), "label": "support"}]},
                {"name": "Java", "titles": ["Java Developer", "Java Backend Developer"],
                 "resumes": [{"path": str(devops), "label": "java"}]},
            ],
        })
        return Pipeline(cfg, store=Store(tmp_path / "t.sqlite3"))

    def test_no_selection_loads_every_role(self, tmp_path):
        pipeline = self._pipeline(tmp_path)
        assert [r.name for r in pipeline.load_roles(None)] == ["Support", "Java"]

    def test_selecting_one_role_narrows_the_queries(self, tmp_path):
        pipeline = self._pipeline(tmp_path)
        roles = pipeline.load_roles(["Java"])
        assert [r.name for r in roles] == ["Java"]
        assert pipeline.build_queries(roles) == ["Java Developer", "Java Backend Developer"]

    def test_selecting_one_role_narrows_the_resumes(self, tmp_path):
        pipeline = self._pipeline(tmp_path)
        roles = pipeline.load_roles(["Java"])
        assert [v.label for r in roles for v in r.resumes] == ["java"]

    def test_role_matching_is_case_insensitive(self, tmp_path):
        pipeline = self._pipeline(tmp_path)
        assert [r.name for r in pipeline.load_roles(["java"])] == ["Java"]

    def test_unknown_role_falls_back_to_all(self, tmp_path):
        """Better to search everything than to silently return nothing."""
        pipeline = self._pipeline(tmp_path)
        assert len(pipeline.load_roles(["Nonexistent"])) == 2


class TestBackwardCompatibility:
    def test_flat_search_roles_becomes_one_role(self, tmp_path):
        resume = tmp_path / "r.txt"
        resume.write_text(SUPPORT_TEXT, encoding="utf-8")
        cfg = Config(data={
            "profile": {"resume_path": str(resume)},
            "search": {"roles": ["Application Support Engineer"], "use_resume_keywords": True},
        })
        roles = load_roles(cfg)
        assert len(roles) == 1
        assert roles[0].resumes[0].usable
        assert "application support engineer" in roles[0].titles
