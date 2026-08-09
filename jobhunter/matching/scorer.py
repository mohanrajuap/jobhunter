"""Decide which discovered jobs are worth applying to.

Two stages, deliberately separate:
  1. Hard filters — a mismatch here is disqualifying no matter how good the rest is
     (wrong seniority, excluded keyword, blocked company, stale posting).
  2. A weighted score in [0, 1] over title fit, keyword overlap, location and freshness.

Only jobs that clear the filters *and* beat `search.min_score` get applied to.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ..config import Config
from ..models import Job, MatchResult
from ..resume.keywords import ResumeProfile

log = logging.getLogger(__name__)

try:
    from rapidfuzz import fuzz

    def _ratio(a: str, b: str) -> float:
        # token_set alone rates "application security engineer" ~= "application support
        # engineer" very highly, because the shared tokens dominate. Averaging in
        # token_sort makes the one differing word actually cost something.
        return (fuzz.token_set_ratio(a, b) + fuzz.token_sort_ratio(a, b)) / 200.0

except ImportError:  # pragma: no cover - fallback when rapidfuzz is unavailable
    from difflib import SequenceMatcher

    def _ratio(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio()


_REMOTE_RE = re.compile(r"\b(remote|work from home|wfh|anywhere|distributed)\b", re.IGNORECASE)

# "Remote" is almost never global. A posting that says "Remote-Friendly | San Francisco,
# CA" is remote *within the US* — useless to someone who needs to work from India. When a
# remote posting names a geography, that geography is authoritative.
_GEO_MARKERS = (
    "united states", "usa", "u.s.", "us-based", "canada", "united kingdom", "england",
    "ireland", "germany", "france", "netherlands", "spain", "portugal", "poland",
    "sweden", "norway", "denmark", "switzerland", "italy", "greece", "israel",
    "australia", "new zealand", "singapore", "japan", "korea", "china", "hong kong",
    "brazil", "mexico", "argentina", "colombia", "chile", "philippines", "vietnam",
    "indonesia", "malaysia", "thailand", "uae", "dubai", "saudi", "egypt",
    "south africa", "nigeria", "kenya", "europe", "emea", "apac", "latam", "americas",
    "north america", "south america", "eu only", "us only",
)
# Two-letter uppercase state/province codes, e.g. "Remote - NY", "Austin, TX".
# Matched against the original casing so the word "in" never reads as Indiana.
_STATE_CODE_RE = re.compile(r"\b(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC|ON|BC|QC|AB)\b")
# Bare region abbreviations, as in "Remote (US)" or "Remote — EMEA".
_REGION_CODE_RE = re.compile(r"\b(?:US|USA|UK|EU|EMEA|APAC|LATAM|ANZ|NA)\b")

# Words shared by nearly every job title, so they say nothing about role fit.
_GENERIC_TITLE_WORDS = {
    "engineer", "senior", "sr", "junior", "jr", "staff", "principal", "lead", "associate",
    "specialist", "analyst", "manager", "consultant", "developer", "architect", "i", "ii",
    "iii", "iv", "and", "of", "the", "a", "an",
}
# "3-5 years", "5+ years", "minimum 4 years"
_EXP_RANGE_RE = re.compile(r"(\d{1,2})\s*(?:-|to|–)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.IGNORECASE)
_EXP_MIN_RE = re.compile(r"(\d{1,2})\s*\+\s*(?:years?|yrs?)", re.IGNORECASE)


@dataclass
class Weights:
    title: float = 0.45
    keywords: float = 0.35
    location: float = 0.10
    freshness: float = 0.10


class Scorer:
    def __init__(
        self,
        config: Config,
        profile: ResumeProfile,
        overrides: dict | None = None,
        feedback: dict | None = None,
    ):
        self.cfg = config
        self.profile = profile

        # What you've marked "not relevant". A single rejection is noise, so signals
        # only take effect once the same company or word has been rejected repeatedly.
        feedback = feedback or {}
        self.rejected_companies: dict[str, int] = feedback.get("companies", {}) or {}
        self.rejected_terms: dict[str, int] = feedback.get("title_terms", {}) or {}
        search_cfg = config.section("search")
        self.company_reject_at = int(search_cfg.get("feedback_company_threshold", 3))
        self.term_penalty_at = int(search_cfg.get("feedback_term_threshold", 3))
        self.max_feedback_penalty = float(search_cfg.get("feedback_max_penalty", 0.35))

        # Per-role overrides shadow the global `search:` block, so one role can use a
        # different location list or score threshold without duplicating the rest.
        search = {**config.section("search"), **(overrides or {})}
        self.roles = [r.lower().strip() for r in search.get("roles", []) if r]
        self.exclude = [k.lower().strip() for k in search.get("exclude_keywords", []) if k]
        self.must_have_any = [k.lower().strip() for k in search.get("must_have_any", []) if k]
        self.locations = [l.lower().strip() for l in search.get("locations", []) if l]
        self.remote_ok = bool(search.get("remote_ok", True))
        self.min_exp = search.get("min_experience_years")
        self.max_exp = search.get("max_experience_years")
        # Skip the experience check entirely — useful when your resume's stated years
        # don't reflect what you can actually do, or you're changing track.
        self.ignore_experience = bool(search.get("ignore_experience", False))
        # How far below a stated requirement you're still willing to apply.
        self.experience_slack = float(search.get("experience_slack_years", 2.0))
        self.posted_within_days = search.get("posted_within_days")
        self.min_score = float(search.get("min_score", 0.45))
        self.blocked_companies = [c.lower().strip() for c in search.get("blocked_companies", []) if c]
        # When true, a title missing any distinctive word of every target role is rejected
        # outright instead of merely penalised. Higher precision, lower recall.
        self.strict_title_match = bool(search.get("strict_title_match", False))

        # Exclusions are matched on word boundaries. Plain substring matching means
        # "intern" silently kills every job mentioning "internal" or "international".
        self._exclude_res = [
            (word, re.compile(rf"(?<![\w-]){re.escape(word)}(?![\w-])", re.IGNORECASE))
            for word in self.exclude
        ]

        w = search.get("weights", {}) or {}
        self.weights = Weights(
            title=float(w.get("title", 0.45)),
            keywords=float(w.get("keywords", 0.35)),
            location=float(w.get("location", 0.10)),
            freshness=float(w.get("freshness", 0.10)),
        )

        # Titles to match against.
        #
        # The role's own titles win. `search.roles` is only a fallback for configs with
        # no `roles:` block — using it first meant every role in a multi-role config was
        # scored against the *global* title list, so picking "Java Developer" still
        # matched against "Application Support Engineer" and scored 0.22 on a perfect hit.
        self.target_titles = self.profile.titles or self.roles

        # Words from the roles you actually want are never allowed to become negative
        # signals. Without this, rejecting a few "Voice Process Support Engineer" jobs
        # teaches the matcher that "support" is bad — and buries the whole target role.
        self.protected_terms: set[str] = set()
        for title in self.target_titles:
            self.protected_terms.update(re.findall(r"[a-z]{3,}", title.lower()))
        for keyword in self.profile.keywords:
            self.protected_terms.update(re.findall(r"[a-z]{3,}", keyword.lower()))
        if not self.target_titles:
            log.warning("No target titles from config or resume — title scoring will be neutral")

    # --- stage 1: hard filters ---

    def _reject(self, job: Job) -> str | None:
        text = job.search_text.lower()
        title = job.title.lower()

        if job.company.lower().strip() in self.blocked_companies:
            return f"company '{job.company}' is on the blocklist"

        rejections = self.rejected_companies.get(job.company.lower().strip(), 0)
        if rejections >= self.company_reject_at:
            return (
                f"you marked {rejections} jobs from '{job.company}' as not relevant"
            )

        for word, pattern in self._exclude_res:
            # Excludes are checked against the title and the first part of the body;
            # matching the whole description throws away too many good jobs over a
            # stray word in a boilerplate benefits section.
            if pattern.search(title) or pattern.search(text[:1500]):
                return f"excluded keyword '{word}'"

        if self.must_have_any and not any(k in text for k in self.must_have_any):
            return f"none of the must-have keywords present ({', '.join(self.must_have_any[:5])})"

        exp_problem = self._experience_mismatch(job)
        if exp_problem:
            return exp_problem

        if not self._location_ok(job):
            return f"location '{job.location}' not in preferred list"

        age = job.age_days()
        if self.posted_within_days and age is not None and age > float(self.posted_within_days):
            return f"posted {age:.0f} days ago (limit {self.posted_within_days})"

        return None

    def _experience_range(self, job: Job) -> tuple[float | None, float | None]:
        """Prefer structured fields from the source; fall back to parsing the text."""
        lo, hi = job.min_experience_years, job.max_experience_years
        if lo is not None or hi is not None:
            return lo, hi

        blob = f"{job.title} {job.description[:3000]}"
        if (m := _EXP_RANGE_RE.search(blob)):
            return float(m.group(1)), float(m.group(2))
        if (m := _EXP_MIN_RE.search(blob)):
            return float(m.group(1)), None
        return None, None

    def _experience_mismatch(self, job: Job) -> str | None:
        if self.ignore_experience:
            return None

        lo, hi = self._experience_range(job)
        mine = self.profile.years_experience

        # Job asks for more than the user has, beyond the allowed stretch.
        #
        # The slack matters: posted requirements are routinely inflated, and applying to
        # a "5+ years" role with 3.5 is normal. A tight tolerance here rejected 88 of 129
        # real jobs in testing.
        if lo is not None and mine is not None and mine + self.experience_slack < lo:
            return f"needs {lo:.0f}+ years, you have ~{mine:.1f}"
        # User's floor: don't apply to roles below the configured minimum.
        if self.min_exp is not None and hi is not None and hi < float(self.min_exp):
            return f"tops out at {hi:.0f} years, below your minimum of {self.min_exp}"
        if self.max_exp is not None and lo is not None and lo > float(self.max_exp):
            return f"needs {lo:.0f}+ years, above your maximum of {self.max_exp}"
        return None

    def _matches_preferred(self, job_location: str) -> bool:
        job_loc = job_location.lower()
        return any(pref in job_loc for pref in self.locations)

    def _remote_scope_conflict(self, job: Job) -> bool:
        """True when a job is remote but only within a region you're not in.

        Without this, every "Remote - US" posting looks like a match to a candidate in
        India, and the tool cheerfully applies to roles they cannot legally accept.
        """
        location = job.location or ""
        if not location:
            return False  # nothing claimed, nothing to conflict with
        if self._matches_preferred(location):
            return False

        lowered = location.lower()
        if any(marker in lowered for marker in _GEO_MARKERS):
            return True
        # Only trust these in the original casing — "in" the word vs "IN" the state.
        return bool(_STATE_CODE_RE.search(location) or _REGION_CODE_RE.search(location))

    def _location_ok(self, job: Job) -> bool:
        if not self.locations:
            return True

        job_loc = job.location.lower()
        if self._matches_preferred(job_loc):
            return True

        is_remote = job.remote or bool(_REMOTE_RE.search(f"{job.location} {job.title}"))
        if is_remote and self.remote_ok:
            return not self._remote_scope_conflict(job)

        if not job_loc:
            return True  # unknown location — let the score decide rather than dropping it
        return any(job_loc in pref for pref in self.locations)

    # --- stage 2: scoring ---

    def _title_score(self, job: Job) -> float:
        if not self.target_titles:
            return 0.6
        title = job.title.lower()
        return max(
            _ratio(title, target) * self._anchor_factor(title, target)
            for target in self.target_titles
        )

    def _anchor_factor(self, job_title: str, target: str) -> float:
        """Penalise titles that miss the *distinctive* words of the target role.

        "Application Security Engineer" and "Application Support Engineer" are one word
        apart and fuzzy-match well, but they are different jobs. Generic words carry no
        signal, so the anchors are what's left: {application, support}. Missing an anchor
        costs the match a third of its title score.
        """
        anchors = {w for w in re.findall(r"[a-z]+", target) if w not in _GENERIC_TITLE_WORDS}
        if not anchors:
            return 1.0
        present = {a for a in anchors if a in job_title}
        if present == anchors:
            return 1.0
        if self.strict_title_match:
            return 0.0
        return 0.65 if present else 0.45

    def _keyword_score(self, job: Job) -> tuple[float, list[str]]:
        if not self.profile.keywords:
            return 0.5, []

        text = job.search_text.lower()
        matched: list[str] = []
        earned = 0.0
        for keyword, weight in self.profile.keywords.items():
            if keyword in text:
                matched.append(keyword)
                earned += weight

        # Normalise against the strongest ~20 keywords rather than all of them: no job
        # description mentions every skill on a resume, and dividing by the full set
        # would push every score toward zero.
        top = sorted(self.profile.keywords.values(), reverse=True)[:20]
        ceiling = sum(top) or 1.0
        score = min(earned / ceiling, 1.0)
        matched.sort(key=lambda k: -self.profile.keywords[k])
        return score, matched

    def _location_score(self, job: Job) -> float:
        if not self.locations:
            return 1.0
        if self._matches_preferred(job.location or ""):
            return 1.0
        if job.remote or _REMOTE_RE.search(job.location or ""):
            if not self.remote_ok:
                return 0.3
            # Region-locked remote scores low rather than perfect.
            return 0.4 if self._remote_scope_conflict(job) else 1.0
        job_loc = (job.location or "").lower()
        if not job_loc:
            return 0.5
        return max((_ratio(job_loc, pref) for pref in self.locations), default=0.5)

    def _freshness_score(self, job: Job) -> float:
        age = job.age_days()
        if age is None:
            return 0.6  # unknown date — mildly penalised, not disqualifying
        if age <= 1:
            return 1.0
        if age <= 3:
            return 0.9
        if age <= 7:
            return 0.75
        if age <= 14:
            return 0.55
        if age <= 30:
            return 0.35
        return 0.15

    def _feedback_penalty(self, job: Job) -> tuple[float, list[str]]:
        """Push down jobs resembling ones you've rejected before.

        A penalty rather than a filter: the same word can appear in a job you'd want,
        so this changes the ranking instead of hiding things outright. Capped so no
        amount of feedback can bury an otherwise excellent match entirely.
        """
        if not self.rejected_terms:
            return 0.0, []

        title_words = set(re.findall(r"[a-z]{3,}", job.title.lower()))
        hits = [
            (word, count) for word, count in self.rejected_terms.items()
            if count >= self.term_penalty_at
            and word in title_words
            and word not in self.protected_terms
        ]
        if not hits:
            return 0.0, []

        # Each qualifying term costs a little more, with diminishing effect.
        penalty = min(sum(0.06 + 0.02 * min(count, 6) for _, count in hits),
                      self.max_feedback_penalty)
        labels = [f"{word}×{count}" for word, count in sorted(hits, key=lambda h: -h[1])[:3]]
        return round(penalty, 3), labels

    def score(self, job: Job) -> MatchResult:
        rejection = self._reject(job)
        if rejection:
            return MatchResult(job=job, score=0.0, rejected_because=rejection)

        title_s = self._title_score(job)
        keyword_s, matched = self._keyword_score(job)
        loc_s = self._location_score(job)
        fresh_s = self._freshness_score(job)

        w = self.weights
        total_weight = w.title + w.keywords + w.location + w.freshness
        total = (
            title_s * w.title
            + keyword_s * w.keywords
            + loc_s * w.location
            + fresh_s * w.freshness
        ) / (total_weight or 1.0)

        penalty, penalty_terms = self._feedback_penalty(job)
        total = max(0.0, total - penalty)

        reasons = [
            f"title {title_s:.2f}",
            f"keywords {keyword_s:.2f} ({len(matched)} matched)",
            f"location {loc_s:.2f}",
            f"freshness {fresh_s:.2f}",
        ]
        if penalty:
            reasons.append(f"-{penalty:.2f} from your feedback ({', '.join(penalty_terms)})")

        result = MatchResult(
            job=job, score=round(total, 3), matched_keywords=matched[:25], reasons=reasons
        )
        if total < self.min_score:
            result.rejected_because = f"score {total:.2f} below threshold {self.min_score:.2f}"
        return result

    def rank(self, jobs: list[Job]) -> list[MatchResult]:
        """Score everything, return only the passing matches, best first."""
        results = [self.score(job) for job in jobs]
        for r in results:
            if not r.passed:
                log.debug("skip %s @ %s — %s", r.job.title, r.job.company, r.rejected_because)
        passed = [r for r in results if r.passed]
        passed.sort(key=lambda r: -r.score)
        log.info("Matched %d of %d discovered jobs", len(passed), len(results))
        return passed
