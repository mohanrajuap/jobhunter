"""Role targets: multiple job roles, each with its own resume(s).

A role bundles the titles you want, the resumes you'd send for them, and any filter
overrides. Every discovered job is scored against every role; the best-scoring role
wins and decides which resume gets uploaded.

Several resumes per role is the interesting case: give it a Kubernetes-heavy variant
and a database-heavy one and each job gets whichever variant its description actually
matches. `RoleTarget.best_resume()` does that pick.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .models import Job
from .resume import build_profile, extract_text
from .resume.keywords import ResumeProfile

log = logging.getLogger(__name__)


@dataclass
class ResumeVariant:
    """One resume file plus the keyword profile parsed out of it."""

    path: Path
    label: str = ""
    profile: ResumeProfile = field(default_factory=ResumeProfile)
    parse_error: str = ""

    @property
    def usable(self) -> bool:
        return self.path.exists() and not self.parse_error

    def match_strength(self, job: Job) -> float:
        """How well this specific resume fits one job, by weighted keyword overlap."""
        if not self.profile.keywords:
            return 0.0
        text = job.search_text.lower()
        earned = sum(w for k, w in self.profile.keywords.items() if k in text)
        ceiling = sum(sorted(self.profile.keywords.values(), reverse=True)[:20]) or 1.0
        return min(earned / ceiling, 1.0)


@dataclass
class RoleTarget:
    name: str
    titles: list[str] = field(default_factory=list)
    resumes: list[ResumeVariant] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    overrides: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    profile: ResumeProfile = field(default_factory=ResumeProfile)
    cover_letter_path: Path | None = None

    @property
    def usable_resumes(self) -> list[ResumeVariant]:
        return [r for r in self.resumes if r.usable]

    def best_resume(self, job: Job) -> ResumeVariant | None:
        """Pick the resume variant whose keywords best match this job."""
        candidates = self.usable_resumes or self.resumes
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        return max(candidates, key=lambda r: r.match_strength(job))

    def summary(self) -> str:
        resumes = ", ".join(r.label or r.path.name for r in self.resumes) or "none"
        return (
            f"{self.name}: {len(self.titles)} title(s), resumes [{resumes}], "
            f"{len(self.profile.keywords)} keywords"
        )


def _load_variant(spec: Any, config_keywords: list[str]) -> ResumeVariant:
    """A resume entry may be a bare path string or a {path, label} mapping."""
    if isinstance(spec, str):
        path, label = spec, ""
    else:
        path, label = spec.get("path", ""), spec.get("label", "")

    resolved = Path(path).expanduser()
    variant = ResumeVariant(path=resolved, label=label or resolved.stem)

    if not resolved.exists():
        variant.parse_error = f"file not found: {resolved}"
        log.error("Resume missing for '%s': %s", variant.label, resolved)
        return variant

    try:
        variant.profile = build_profile(extract_text(resolved), extra_keywords=config_keywords)
        log.info("Parsed resume '%s': %d keywords", variant.label, len(variant.profile.keywords))
    except Exception as exc:
        variant.parse_error = str(exc)
        log.error("Could not parse resume '%s' (%s): %s", variant.label, resolved, exc)

    return variant


def _merge_profiles(
    variants: list[ResumeVariant], titles: list[str], keywords: list[str], fallback_years: float | None
) -> ResumeProfile:
    """Union of every resume's keywords for the role, keeping the highest weight each.

    Matching a *role* should use everything the role's resumes collectively prove;
    choosing between resumes is a separate decision, made per job by best_resume().
    """
    merged: dict[str, float] = {}
    years: list[float] = []

    for variant in variants:
        for keyword, weight in variant.profile.keywords.items():
            merged[keyword] = max(merged.get(keyword, 0.0), weight)
        if variant.profile.years_experience:
            years.append(variant.profile.years_experience)

    for keyword in keywords:
        merged[keyword.lower().strip()] = 1.0

    return ResumeProfile(
        keywords=merged,
        titles=[t.lower().strip() for t in titles if t],
        years_experience=max(years) if years else fallback_years,
    )


def load_roles(config: Config, only_names: list[str] | None = None) -> list[RoleTarget]:
    """Build role targets from config.

    Supports both layouts: the multi-role `roles:` list, and the older flat
    `search.roles` + `profile.resume_path` pair, which becomes a single role.

    `only_names` narrows to specific roles *before* any resume is opened. Filtering
    afterwards would parse every CV on the machine and report errors for roles the user
    did not ask for — slow, and alarming for no reason.
    """
    global_keywords = list(config.get("search.keywords", []) or [])
    fallback_years = config.get("profile.total_experience_years")
    fallback_years = float(fallback_years) if fallback_years is not None else None

    raw_roles = config.get("roles", []) or []
    roles: list[RoleTarget] = []

    if raw_roles and only_names:
        wanted = {name.strip().lower() for name in only_names}
        narrowed = [r for r in raw_roles if str(r.get("name", "")).lower() in wanted]
        if narrowed:
            raw_roles = narrowed
        else:
            log.warning("No role matched %s — using all roles", sorted(wanted))

    if raw_roles:
        for entry in raw_roles:
            if not entry.get("enabled", True):
                log.info("Role '%s' is disabled — skipping", entry.get("name", "?"))
                continue

            titles = list(entry.get("titles", []) or [])
            if not titles and entry.get("name"):
                titles = [entry["name"]]

            role_keywords = list(entry.get("keywords", []) or [])
            variants = [
                _load_variant(spec, global_keywords + role_keywords)
                for spec in (entry.get("resumes", []) or [])
            ]

            if not variants:
                # Fall back to the global resume so a role without its own still works.
                if config.resume_path:
                    variants = [_load_variant(str(config.resume_path), global_keywords + role_keywords)]
                else:
                    log.warning("Role '%s' has no resume and profile.resume_path is unset", entry.get("name"))

            cover = entry.get("cover_letter_path") or config.get("profile.cover_letter_path", "")

            roles.append(
                RoleTarget(
                    name=entry.get("name") or (titles[0] if titles else "unnamed"),
                    titles=titles,
                    resumes=variants,
                    keywords=role_keywords,
                    overrides=dict(entry.get("overrides", {}) or {}),
                    enabled=True,
                    profile=_merge_profiles(
                        variants, titles, global_keywords + role_keywords, fallback_years
                    ),
                    cover_letter_path=Path(cover).expanduser() if cover else None,
                )
            )
    else:
        titles = list(config.get("search.roles", []) or [])
        variants = [_load_variant(str(config.resume_path), global_keywords)] if config.resume_path else []
        profile = _merge_profiles(variants, titles, global_keywords, fallback_years)

        # With no `roles:` block we also keep the titles the resume itself suggests.
        if config.get("search.use_resume_keywords", True):
            for variant in variants:
                for title in variant.profile.titles:
                    if title not in profile.titles:
                        profile.titles.append(title)

        cover = config.get("profile.cover_letter_path", "")
        roles.append(
            RoleTarget(
                name=titles[0] if titles else "default",
                titles=profile.titles,
                resumes=variants,
                keywords=global_keywords,
                profile=profile,
                cover_letter_path=Path(cover).expanduser() if cover else None,
            )
        )

    for role in roles:
        log.info("Role loaded — %s", role.summary())
    return roles
