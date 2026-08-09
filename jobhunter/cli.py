"""Command line interface."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, load_config
from .logging_setup import setup_logging

log = logging.getLogger("jobhunter.cli")

LOGIN_SITES = {
    "naukri": "https://www.naukri.com/nlogin/login",
    "linkedin": "https://www.linkedin.com/login",
    "indeed": "https://secure.indeed.com/account/login",
    "greenhouse": "https://my.greenhouse.io/",
}


def _force_utf8_stdout() -> None:
    """Windows consoles default to cp1252 and mangle the report's bullets and arrows."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _bootstrap(args: argparse.Namespace) -> Config:
    config = load_config(args.config)
    setup_logging(
        log_dir=config.get("paths.log_dir", "logs"),
        level="DEBUG" if getattr(args, "verbose", False) else config.get("log_level", "INFO"),
        quiet=getattr(args, "quiet", False),
    )
    return config


# --- commands ---


def cmd_run(args: argparse.Namespace) -> int:
    config = _bootstrap(args)

    problems = config.validate()
    if problems:
        log.error("Configuration problems:")
        for problem in problems:
            log.error("  • %s", problem)
        log.error("Fix these (or run `jobhunter doctor`) and try again.")
        return 2

    dry_run = None
    if args.live:
        dry_run = False
    elif args.dry_run:
        dry_run = True

    if dry_run is False:
        log.warning("LIVE MODE — applications will actually be submitted.")

    from .pipeline import Pipeline

    report = Pipeline(config).run(dry_run=dry_run, limit=args.limit, apply=not args.no_apply)

    print()
    print(f"  discovered : {report.discovered}")
    print(f"  matched    : {report.matched}")
    print(f"  applied    : {len(report.applied)}")
    print(f"  need you   : {len(report.manual)}")
    if report.manual:
        print("\n  Apply to these manually:")
        for outcome in report.manual:
            print(f"    • {outcome.job.title} — {outcome.job.company}")
            print(f"      {outcome.reason}")
            print(f"      {outcome.job.target_url}")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    """Open a real browser so you can sign in by hand. The session then persists."""
    config = _bootstrap(args)
    url = LOGIN_SITES.get(args.site, args.site)

    from .browser import browser_from_config

    print(f"\nOpening {url}")
    print("Sign in manually (including any OTP or captcha), then come back here.")
    print("The session is saved to the browser profile and reused by every future run.\n")

    with browser_from_config(config, headless=False) as browser:
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded")
        input("Press Enter once you are signed in and the page has fully loaded... ")
        try:
            print(f"Current page: {page.url}")
        except Exception:
            pass

    print("\nSession saved. Future runs will reuse it until the site logs you out.")
    return 0


def cmd_parse_resume(args: argparse.Namespace) -> int:
    config = _bootstrap(args)
    from .resume import build_profile, extract_text

    path = Path(args.path).expanduser() if args.path else config.resume_path
    if not path:
        log.error("No resume path given and profile.resume_path is not set")
        return 2

    text = extract_text(path)
    profile = build_profile(
        text,
        extra_keywords=list(config.get("search.keywords", []) or []),
        extra_roles=list(config.get("search.roles", []) or []),
    )

    print(f"\nResume: {path}")
    print(f"Characters extracted : {len(text)}")
    print(f"Experience detected  : {profile.years_experience or 'unknown'} years")
    print(f"Seniority level      : {profile.seniority}")
    print(f"\nTitles ({len(profile.titles)}):")
    for title in profile.titles:
        print(f"  • {title}")
    print(f"\nKeywords ({len(profile.keywords)}), strongest first:")
    for keyword in profile.top_keywords:
        print(f"  {profile.keywords[keyword]:.2f}  {keyword}")
    print("\nThese keywords drive matching. Add anything missing to search.keywords in your config.")
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    """Discovery + scoring with no applying — the safe way to tune your filters."""
    config = _bootstrap(args)
    from .matching import MultiRoleScorer
    from .pipeline import Pipeline

    pipeline = Pipeline(config)
    roles = pipeline.load_roles()
    queries = pipeline.build_queries(roles)
    scorer = MultiRoleScorer(config, roles)

    needs_browser = config.get("sources.naukri.enabled", False)
    if needs_browser:
        from .browser import browser_from_config

        with browser_from_config(config) as browser:
            jobs, errors = pipeline.discover(queries, browser)
    else:
        jobs, errors = pipeline.discover(queries, None)

    results = scorer.score_all(jobs)
    passed = sorted([r for r in results if r.passed], key=lambda r: -r.score)

    print(f"\n{len(jobs)} jobs discovered, {len(passed)} matched\n")
    for result in passed[: args.limit or 40]:
        job = result.job
        status, _, _ = pipeline.store.status_for(job)
        tag = "" if status == "new" else f"  [{status.upper()}]"
        print(f"  {result.score:.0%}  {job.title}{tag}")
        print(f"        {job.company} · {job.location or 'n/a'} · {job.source}")
        print(f"        role '{result.role_name}' · resume '{result.resume_label or 'default'}'")
        print(f"        {job.target_url}")
        if args.verbose and result.matched_keywords:
            print(f"        matched: {', '.join(result.matched_keywords[:12])}")

    if args.show_rejected:
        print("\nRejected:\n")
        for result in results:
            if not result.passed:
                print(f"  ✗ {result.job.title} — {result.job.company}: {result.rejected_because}")

    for name, err in errors.items():
        print(f"\n  ! source '{name}' failed: {err}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check everything before you trust this thing to run unattended."""
    config = _bootstrap(args)
    ok = True

    print(f"\njobhunter {__version__} — configuration check")
    print(f"Config file: {config.path}\n")

    print("Dependencies")
    for module, hint in [
        ("yaml", "pip install PyYAML"),
        ("requests", "pip install requests"),
        ("pdfplumber", "pip install pdfplumber (PDF resumes)"),
        ("docx", "pip install python-docx (DOCX resumes)"),
        ("rapidfuzz", "pip install rapidfuzz (better title matching)"),
        ("bs4", "pip install beautifulsoup4 (custom career pages)"),
        ("playwright", "pip install playwright && python -m playwright install chromium"),
    ]:
        try:
            __import__(module)
            print(f"  ✓ {module}")
        except ImportError:
            print(f"  ✗ {module} — {hint}")
            ok = False

    print("\nConfiguration")
    problems = config.validate()
    if problems:
        ok = False
        for problem in problems:
            print(f"  ✗ {problem}")
    else:
        print("  ✓ required fields present")
    for warning in config.warnings():
        print(f"  ! {warning}")

    print("\nRoles and resumes")
    try:
        from .roles import load_roles

        roles = load_roles(config)
        if not roles:
            print("  ✗ no roles configured")
            ok = False
        for role in roles:
            print(f"  • role '{role.name}' — {len(role.titles)} title(s), "
                  f"{len(role.profile.keywords)} keywords")
            if not role.resumes:
                print("    ✗ no resume attached")
                ok = False
            for variant in role.resumes:
                if variant.usable:
                    print(f"    ✓ {variant.label}: {len(variant.profile.keywords)} keywords "
                          f"({variant.path.name})")
                else:
                    print(f"    ✗ {variant.label}: {variant.parse_error}")
                    ok = False
    except Exception as exc:
        print(f"  ✗ {exc}")
        ok = False

    print("\nJob board slugs")
    ok = _check_boards(config) and ok

    print("\nNotifications")
    channels = [c for c in ("email", "telegram", "desktop") if config.get(f"notify.{c}.enabled", False)]
    if channels:
        print(f"  ✓ enabled: {', '.join(channels)}  (run `jobhunter notify-test` to verify delivery)")
    else:
        print("  ! no channel enabled — you will not get the daily summary")

    print("\nApply settings")
    print(f"  dry_run  : {config.dry_run}  {'(safe — nothing is submitted)' if config.dry_run else '(LIVE)'}")
    print(f"  daily cap: {config.get('apply.max_applications_per_day', 25)}")
    print(f"  headless : {config.get('apply.headless', False)}")

    print("\n" + ("All good — you can run `jobhunter run`." if ok else "Fix the ✗ items above first."))
    return 0 if ok else 1


def _check_boards(config: Config) -> bool:
    """Hit each configured board once. A dead slug silently returns zero jobs forever."""
    from .sources import BOARD_SOURCES
    from .sources.base import make_session

    session = make_session()
    all_ok = True
    checked = 0

    for name, source_cls in BOARD_SOURCES.items():
        section = config.section(f"sources.{name}")
        if not section.get("enabled", False):
            continue
        source = source_cls(dict(section), session=session)
        for company in source.companies():
            checked += 1
            try:
                jobs = source.fetch_company(company)
                marker = "✓" if jobs else "!"
                print(f"  {marker} {name}/{company}: {len(jobs)} open roles")
                if not jobs:
                    all_ok = False
            except Exception as exc:
                print(f"  ✗ {name}/{company}: {str(exc)[:90]}")
                all_ok = False

    if checked == 0:
        print("  ! no board slugs configured")
    return all_ok


def cmd_notify_test(args: argparse.Namespace) -> int:
    config = _bootstrap(args)
    from . import notify
    from .models import Job, Outcome, RunReport, Status

    sample = Job(
        source="greenhouse", ats="greenhouse", company="Example Corp",
        title="Senior Application Support Engineer", url="https://example.com/jobs/1",
        location="Chennai, India",
    )
    blocked = Job(
        source="naukri", ats="naukri", company="Blocked Inc",
        title="SRE Lead", url="https://example.com/jobs/2", location="Remote",
    )
    report = RunReport(
        started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc),
        discovered=137, matched=9,
        outcomes=[
            Outcome(job=sample, status=Status.APPLIED, score=0.82, reason="confirmation shown"),
            Outcome(job=blocked, status=Status.MANUAL, score=0.71,
                    reason="Naukri hands this one off to the company's own site"),
        ],
    )

    results = notify.send_all(config, report)
    ok = True
    for channel, result in results.items():
        succeeded = not str(result).startswith("error")
        ok = ok and succeeded
        print(f"  {'✓' if succeeded else '✗'} {channel}: {result}")
    return 0 if ok else 1


def cmd_stats(args: argparse.Namespace) -> int:
    config = _bootstrap(args)
    from .store import Store

    store = Store(config.data_dir() / "jobhunter.sqlite3")
    stats = store.stats()
    print("\nAll-time")
    for key, value in sorted(stats.items()):
        print(f"  {key:16} {value}")
    print(f"\n  applied today    {store.applications_today()}")

    pending = store.pending_manual(limit=args.limit or 25)
    if pending:
        print(f"\nStill needing manual application ({len(pending)}):")
        for row in pending:
            print(f"  • {row['title']} — {row['company']} ({row['location'] or 'n/a'})")
            print(f"    {row['reason']}")
            print(f"    {row['url']}")
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    """Print the exact command to register a daily run on this machine."""
    config = _bootstrap(args)
    at = args.at or config.get("schedule.time", "08:00")
    python = sys.executable
    project = Path.cwd()
    script = project / "scripts" / "install_windows_task.ps1"

    print(f"\nTo run jobhunter every day at {at}:\n")
    if sys.platform == "win32":
        print("  Windows Task Scheduler (run PowerShell as your normal user):\n")
        print(f'    powershell -ExecutionPolicy Bypass -File "{script}" -Time "{at}"\n')
        print("  Or register it directly:\n")
        print(
            f'    schtasks /Create /TN "JobHunter Daily" /SC DAILY /ST {at} '
            f'/TR "\\"{python}\\" -m jobhunter run --live" /F\n'
        )
    else:
        print("  cron (crontab -e):\n")
        hour, _, minute = at.partition(":")
        print(f"    {minute or 0} {hour} * * 1-5  cd {project} && {python} -m jobhunter run --live\n")

    print("  Or keep a long-running scheduler in the foreground:\n")
    print("    jobhunter daemon\n")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """Work out where a career site keeps its jobs, and print the config to read them."""
    config = _bootstrap(args)
    from .browser import browser_from_config
    from .discovery import probe_career_site

    print(f"\nProbing {args.url} — loading it in a browser and watching what it fetches…\n")
    with browser_from_config(config, headless=not args.show) as browser:
        report = probe_career_site(args.url, browser, wait_ms=args.wait * 1000)

    print(report.to_text())
    return 0 if report.findings else 1


def cmd_gui(args: argparse.Namespace) -> int:
    """Open the desktop UI. Config problems are reported inside the window, not here,
    so the UI still opens when there's nothing configured yet."""
    setup_logging(log_dir="logs", level="DEBUG" if args.verbose else "INFO", quiet=True)
    try:
        from .gui import launch
    except ImportError as exc:
        print(f"Could not start the UI: {exc}")
        print("Tkinter is required. It ships with Python on Windows; on Linux: apt install python3-tk")
        return 1
    return launch(args.config)


def cmd_daemon(args: argparse.Namespace) -> int:
    config = _bootstrap(args)
    from .scheduler import run_daemon

    return run_daemon(config)


# --- parser ---


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobhunter",
        description="Autonomously find and apply to jobs every morning.",
    )
    parser.add_argument("--version", action="version", version=f"jobhunter {__version__}")
    parser.add_argument("-c", "--config", help="path to config YAML (default: config/config.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="log to file only")

    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="do a full discover-match-apply-notify run")
    p_run.add_argument("--live", action="store_true", help="actually submit applications")
    p_run.add_argument("--dry-run", action="store_true", help="fill forms but never submit")
    p_run.add_argument("--limit", type=int, help="cap how many jobs to attempt this run")
    p_run.add_argument("--no-apply", action="store_true", help="discover and match only")
    p_run.set_defaults(func=cmd_run)

    p_login = sub.add_parser("login", help="sign in to a job site once; the session persists")
    p_login.add_argument("site", nargs="?", default="naukri",
                         help=f"one of {', '.join(LOGIN_SITES)} — or any URL")
    p_login.set_defaults(func=cmd_login)

    p_resume = sub.add_parser("parse-resume", help="show the keywords pulled from your resume")
    p_resume.add_argument("path", nargs="?", help="resume file (default: profile.resume_path)")
    p_resume.set_defaults(func=cmd_parse_resume)

    p_disc = sub.add_parser("discover", help="find and score jobs without applying")
    p_disc.add_argument("--limit", type=int, help="how many matches to print")
    p_disc.add_argument("--show-rejected", action="store_true", help="also print what was filtered out and why")
    p_disc.set_defaults(func=cmd_discover)

    p_doc = sub.add_parser("doctor", help="check config, dependencies, resume and board slugs")
    p_doc.set_defaults(func=cmd_doctor)

    p_notify = sub.add_parser("notify-test", help="send a sample summary through every enabled channel")
    p_notify.set_defaults(func=cmd_notify_test)

    p_stats = sub.add_parser("stats", help="application history and the pending manual queue")
    p_stats.add_argument("--limit", type=int, help="how many manual items to list")
    p_stats.set_defaults(func=cmd_stats)

    p_sched = sub.add_parser("schedule", help="print how to register the daily run")
    p_sched.add_argument("--at", help="time of day, HH:MM (default: schedule.time)")
    p_sched.set_defaults(func=cmd_schedule)

    p_probe = sub.add_parser(
        "probe", help="find where a career site keeps its jobs and print the config for it")
    p_probe.add_argument("url", help="the company's careers page URL")
    p_probe.add_argument("--wait", type=int, default=6, help="seconds to watch for requests")
    p_probe.add_argument("--show", action="store_true", help="show the browser while probing")
    p_probe.set_defaults(func=cmd_probe)

    p_gui = sub.add_parser("gui", help="open the desktop UI")
    p_gui.set_defaults(func=cmd_gui)

    p_daemon = sub.add_parser("daemon", help="stay running and fire on the configured schedule")
    p_daemon.set_defaults(func=cmd_daemon)

    return parser


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        logging.getLogger("jobhunter").exception("fatal error")
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
