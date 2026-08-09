"""Turn resume text into a weighted keyword profile used for job matching."""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

_LEXICON_PATH = Path(__file__).resolve().parent.parent / "data" / "skills.txt"

# Job-title shaped lines, e.g. "Senior Application Support Engineer".
_TITLE_WORDS = (
    r"engineer|developer|analyst|architect|consultant|manager|administrator|specialist|"
    r"lead|scientist|designer|programmer|tester|sre|devops|support"
)
_TITLE_RE = re.compile(
    rf"\b((?:senior|sr\.?|junior|jr\.?|lead|principal|staff|associate|chief)?\s*"
    rf"[A-Za-z][\w/.+#-]*(?:\s+[A-Za-z][\w/.+#-]*){{0,3}}\s+(?:{_TITLE_WORDS}))\b",
    re.IGNORECASE,
)
_YEARS_RE = re.compile(
    r"(\d{1,2}(?:\.\d)?)\s*\+?\s*(?:years?|yrs?)\s*(?:of\s*)?(?:experience|exp)?", re.IGNORECASE
)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE_RE = re.compile(r"(?:\+\d{1,3}[\s-]?)?(?:\d[\s-]?){9,12}\d")

# Words that prove a phrase is prose, not a job title. "led l3 production support"
# and "of experience in production support" both match the title shape otherwise.
_TITLE_STOPWORDS = {
    "of", "in", "the", "and", "with", "for", "at", "to", "a", "an", "my", "our", "as",
    "led", "built", "managed", "using", "via", "including", "across", "from", "on",
    "experience", "summary", "years", "year", "skills", "profile", "objective",
}

_SENIORITY = {
    "intern": 0, "trainee": 0, "graduate": 0,
    "junior": 1, "associate": 1,
    "senior": 3, "sr": 3,
    "lead": 4, "principal": 5, "staff": 5,
    "manager": 4, "head": 5, "director": 6, "vp": 7,
}


@lru_cache(maxsize=1)
def _load_lexicon() -> tuple[str, ...]:
    if not _LEXICON_PATH.exists():
        log.warning("Skill lexicon missing at %s — falling back to config keywords only", _LEXICON_PATH)
        return ()
    skills = []
    for line in _LEXICON_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            skills.append(line.lower())
    return tuple(skills)


def _phrase_pattern(skill: str) -> re.Pattern[str]:
    """Word-boundary match that tolerates the punctuation in things like `node.js` or `c++`."""
    escaped = re.escape(skill).replace(r"\ ", r"[\s\-_]+")
    return re.compile(rf"(?<![\w+#.]){escaped}(?![\w+#])", re.IGNORECASE)


@lru_cache(maxsize=2048)
def _cached_pattern(skill: str) -> re.Pattern[str]:
    return _phrase_pattern(skill)


@dataclass
class ResumeProfile:
    """What we learned from the resume, plus anything the user added in config."""

    keywords: dict[str, float] = field(default_factory=dict)  # keyword -> weight
    titles: list[str] = field(default_factory=list)
    years_experience: float | None = None
    seniority: int = 2
    email: str = ""
    phone: str = ""
    raw_text: str = ""

    @property
    def top_keywords(self) -> list[str]:
        return sorted(self.keywords, key=lambda k: -self.keywords[k])

    def summary(self) -> str:
        top = ", ".join(self.top_keywords[:15]) or "none"
        return (
            f"{len(self.keywords)} keywords (top: {top}); "
            f"titles: {', '.join(self.titles[:4]) or 'none'}; "
            f"experience: {self.years_experience if self.years_experience is not None else 'unknown'}y"
        )


def extract_skills(text: str, extra_skills: list[str] | None = None) -> dict[str, float]:
    """Match the lexicon against the text; weight by how often each skill appears.

    Frequency is a decent proxy for how central a skill is to someone's actual work —
    a language used across four projects beats one listed once under "familiar with".
    """
    lowered = text.lower()
    counts: Counter[str] = Counter()

    for skill in list(_load_lexicon()) + [s.lower() for s in (extra_skills or [])]:
        hits = len(_cached_pattern(skill).findall(lowered))
        if hits:
            counts[skill] = hits

    if not counts:
        return {}

    peak = max(counts.values())
    # Compress the range: 1 hit still counts for a lot, 10 hits is not 10x as important.
    return {skill: round(0.5 + 0.5 * (n / peak), 3) for skill, n in counts.items()}


def _is_title(words: list[str]) -> bool:
    """Reject prose that happens to end in a title-ish noun.

    Three rules do almost all the work: real titles are short, contain no filler
    words, and don't start with a fragment like "l3" or a person's initial.
    """
    if not 2 <= len(words) <= 4:
        return False
    if any(w in _TITLE_STOPWORDS for w in words):
        return False
    first = words[0]
    return first.isalpha() and len(first) >= 3


def extract_titles(text: str, limit: int = 8) -> list[str]:
    seen: dict[str, int] = {}
    for match in _TITLE_RE.finditer(text):
        title = re.sub(r"\s+", " ", match.group(1)).strip().lower()
        if _is_title(title.split()):
            seen[title] = seen.get(title, 0) + 1
    return [t for t, _ in sorted(seen.items(), key=lambda kv: -kv[1])[:limit]]


def extract_years(text: str) -> float | None:
    """Largest plausible '<n> years of experience' figure in the document."""
    values = [float(m) for m in _YEARS_RE.findall(text) if 0 < float(m) <= 45]
    return max(values) if values else None


def infer_seniority(titles: list[str], years: float | None) -> int:
    for title in titles:
        for word, level in _SENIORITY.items():
            if re.search(rf"\b{word}\b", title):
                return level
    if years is None:
        return 2
    if years < 1:
        return 0
    if years < 3:
        return 1
    if years < 6:
        return 2
    if years < 10:
        return 3
    return 4


def build_profile(
    text: str,
    extra_keywords: list[str] | None = None,
    extra_roles: list[str] | None = None,
) -> ResumeProfile:
    """Combine resume-derived signals with explicit config keywords/roles.

    Config entries are weighted 1.0 — above anything inferred — because a stated
    preference should always outrank a guess made from the document.
    """
    keywords = extract_skills(text, extra_keywords)
    for keyword in extra_keywords or []:
        keywords[keyword.lower().strip()] = 1.0

    titles = extract_titles(text)
    for role in extra_roles or []:
        role = role.lower().strip()
        if role and role not in titles:
            titles.insert(0, role)

    years = extract_years(text)
    email = (_EMAIL_RE.search(text) or [""])[0] if _EMAIL_RE.search(text) else ""
    phone_match = _PHONE_RE.search(text)

    return ResumeProfile(
        keywords=keywords,
        titles=titles,
        years_experience=years,
        seniority=infer_seniority(titles, years),
        email=email,
        phone=phone_match.group(0).strip() if phone_match else "",
        raw_text=text,
    )
