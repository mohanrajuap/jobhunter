"""Config loading: YAML + `env:VAR` indirection so secrets stay out of the file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_PATHS = [
    Path("config/config.yaml"),
    Path("config/config.example.yaml"),
]


class ConfigError(Exception):
    pass


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader — avoids a dependency for four lines of parsing."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _resolve(value: Any) -> Any:
    """Expand `env:NAME` strings recursively. Missing vars resolve to ''."""
    if isinstance(value, str) and value.startswith("env:"):
        return os.environ.get(value[4:], "")
    if isinstance(value, dict):
        return {k: _resolve(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v) for v in value]
    return value


@dataclass
class Config:
    """Thin typed wrapper over the YAML tree.

    Kept dict-backed on purpose: sources and appliers evolve faster than a schema
    would, and `cfg.get("sources.naukri.enabled", False)` reads fine at call sites.
    """

    data: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None
    # The file exactly as written, with `env:VAR` references still unresolved. Saving
    # writes this, never `data` — otherwise editing config from the GUI would bake the
    # resolved SMTP password straight into the YAML file.
    raw: dict[str, Any] = field(default_factory=dict)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node if node is not None else default

    def section(self, dotted: str) -> dict[str, Any]:
        value = self.get(dotted, {})
        return value if isinstance(value, dict) else {}

    # --- mutation & persistence (used by the GUI) ---

    def set(self, dotted: str, value: Any) -> None:
        """Set a value in both the resolved tree and the writable raw tree."""
        for tree in (self.data, self.raw):
            node = tree
            parts = dotted.split(".")
            for part in parts[:-1]:
                nxt = node.get(part)
                if not isinstance(nxt, dict):
                    nxt = {}
                    node[part] = nxt
                node = nxt
            node[parts[-1]] = value

    #: How many timestamped backups to keep beside the config.
    KEEP_BACKUPS = 5

    def save(self, path: str | Path | None = None, backup: bool = True) -> Path:
        """Write the config back to YAML.

        Comments are not preserved — PyYAML doesn't round-trip them — so the previous
        version is kept first. Backups are timestamped rather than a single `.bak`
        slot: the app rewrites this file on every save, and one shared slot means the
        second save destroys the only copy of what you had before the first.
        """
        target = Path(path) if path else self.path
        if target is None:
            raise ConfigError("No config path to save to")
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)

        if backup and target.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_path = target.with_suffix(f"{target.suffix}.{stamp}.bak")
            backup_path.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
            self._prune_backups(target)

        target.write_text(
            yaml.safe_dump(self.raw or self.data, sort_keys=False, allow_unicode=True,
                           default_flow_style=False, width=100),
            encoding="utf-8",
        )
        self.path = target
        return target

    def _prune_backups(self, target: Path) -> None:
        """Keep only the most recent KEEP_BACKUPS copies."""
        pattern = f"{target.name}.*.bak"
        backups = sorted(target.parent.glob(pattern), reverse=True)
        for stale in backups[self.KEEP_BACKUPS:]:
            try:
                stale.unlink()
            except OSError:
                pass

    # --- frequently used, typed accessors ---

    @property
    def profile(self) -> dict[str, Any]:
        return self.section("profile")

    @property
    def dry_run(self) -> bool:
        return bool(self.get("apply.dry_run", True))

    @property
    def resume_path(self) -> Path | None:
        raw = self.get("profile.resume_path", "")
        return Path(raw).expanduser() if raw else None

    def state_dir(self) -> Path:
        d = Path(self.get("paths.state_dir", ".state")).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        return d

    def data_dir(self) -> Path:
        d = Path(self.get("paths.data_dir", "data")).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _validate_resumes(self) -> tuple[list[str], list[str]]:
        """Returns (blocking problems, warnings).

        A role is fine as long as *one* of its resumes exists — `RoleTarget.best_resume`
        already skips unusable variants at runtime. A stale extra path is worth
        mentioning but must not stop a run that can otherwise proceed.
        """
        problems: list[str] = []
        warnings: list[str] = []
        roles = self.get("roles", []) or []
        global_resume = self.resume_path
        global_ok = bool(global_resume and global_resume.exists())

        if not roles:
            if not global_resume:
                problems.append(
                    "No resume configured — set profile.resume_path, or define `roles:` "
                    "with a resumes list"
                )
            elif not global_ok:
                problems.append(f"profile.resume_path does not exist: {global_resume}")
            return problems, warnings

        if global_resume and not global_ok:
            warnings.append(
                f"profile.resume_path does not exist and will be ignored: {global_resume}"
            )

        for role in roles:
            if not role.get("enabled", True):
                continue
            name = role.get("name", "unnamed")
            specs = role.get("resumes", []) or []

            if not specs:
                if not global_ok:
                    problems.append(
                        f"role '{name}' has no resume — add one on the Roles & Resumes tab"
                    )
                continue

            present, missing = [], []
            for spec in specs:
                path = spec if isinstance(spec, str) else spec.get("path", "")
                if not path:
                    missing.append("(entry with no path)")
                elif Path(path).expanduser().exists():
                    present.append(path)
                else:
                    missing.append(path)

            if not present and not global_ok:
                listed = ", ".join(missing[:3])
                problems.append(
                    f"role '{name}' has no usable resume — none of its files exist: {listed}"
                )
            elif missing:
                warnings.append(
                    f"role '{name}': ignoring {len(missing)} missing resume(s) — "
                    f"{', '.join(missing[:3])}"
                )

        return problems, warnings

    def warnings(self) -> list[str]:
        """Non-blocking configuration issues worth surfacing but not worth stopping for."""
        return self._validate_resumes()[1]

    def validate(self) -> list[str]:
        """Return human-readable problems. Empty list means good to go."""
        problems: list[str] = []
        p = self.profile
        for required in ("full_name", "email", "phone"):
            if not p.get(required):
                problems.append(f"profile.{required} is required for filling application forms")

        problems.extend(self._validate_resumes()[0])

        roles = self.get("roles", []) or self.get("search.roles", [])
        keywords = self.get("search.keywords", [])
        if not roles and not keywords and not self.get("search.use_resume_keywords", True):
            problems.append(
                "Nothing to search for: set `roles:`, search.roles, search.keywords, "
                "or enable search.use_resume_keywords"
            )

        if not any(
            self.get(f"sources.{name}.enabled", False)
            for name in ("naukri", "greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee")
        ) and not self.get("sources.custom_career_pages", []):
            problems.append("No sources enabled — enable at least one under `sources:`")

        if self.get("notify.email.enabled", False):
            if not self.get("notify.email.username"):
                problems.append("notify.email.username is empty (set SMTP_USER in .env)")
            if not self.get("notify.email.password"):
                problems.append("notify.email.password is empty (set SMTP_PASS in .env)")
            if not self.get("notify.email.to"):
                problems.append("notify.email.to has no recipients")

        return problems


def load_config(path: str | Path | None = None) -> Config:
    _load_dotenv()

    if path:
        candidate = Path(path).expanduser()
        if not candidate.exists():
            raise ConfigError(f"Config file not found: {candidate}")
    else:
        candidate = next((p for p in _DEFAULT_PATHS if p.exists()), None)
        if candidate is None:
            raise ConfigError(
                "No config found. Copy config/config.example.yaml to config/config.yaml "
                "and edit it, or pass --config PATH."
            )

    raw = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{candidate} must contain a YAML mapping at the top level")

    import copy

    return Config(data=_resolve(copy.deepcopy(raw)), path=candidate, raw=raw)
