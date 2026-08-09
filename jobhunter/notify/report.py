"""Builds the daily summary in three formats: plain text, HTML email, and Markdown."""

from __future__ import annotations

from datetime import datetime

from ..models import RunReport, Status

# Kept out of the template below on purpose: str.format() reads every `{` in the
# template as a placeholder, and CSS is nothing but braces. Passing it in as a value
# sidesteps that entirely.
_CSS = """
  body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;line-height:1.5;
       color:#1a1a1a;background:#f6f7f9;margin:0;padding:24px}
  .wrap{max-width:680px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;
        box-shadow:0 1px 3px rgba(0,0,0,.08)}
  .head{background:#12253f;color:#fff;padding:22px 26px}
  .head h1{margin:0;font-size:19px}.head p{margin:6px 0 0;opacity:.75;font-size:13px}
  .stats{display:flex;flex-wrap:wrap;gap:10px;padding:20px 26px;border-bottom:1px solid #eceef1}
  .stat{flex:1;min-width:110px;background:#f6f7f9;border-radius:9px;padding:12px 14px}
  .stat b{display:block;font-size:24px;line-height:1.2}
  .stat span{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:#5a6472}
  .sec{padding:20px 26px;border-bottom:1px solid #eceef1}
  .sec h2{font-size:14px;text-transform:uppercase;letter-spacing:.6px;color:#5a6472;margin:0 0 12px}
  .job{padding:10px 0;border-bottom:1px solid #f2f3f5}.job:last-child{border-bottom:none}
  .job a{color:#0b5cd5;text-decoration:none;font-weight:600}
  .meta{font-size:12px;color:#6b7280;margin-top:3px}
  .why{font-size:12px;color:#a1470c;background:#fff7ed;border-left:3px solid #f59e0b;
       padding:5px 9px;margin-top:5px;border-radius:0 5px 5px 0}
  .foot{padding:16px 26px;font-size:11px;color:#8b93a1}
  .none{color:#6b7280;font-size:13px;font-style:italic}
  .banner{background:#fff7ed;color:#9a3412;padding:10px 26px;font-size:13px;font-weight:600}
"""

_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job hunt summary</title>
<style>{css}</style></head><body><div class="wrap">
<div class="head">
  <h1>Job hunt summary — {date}</h1>
  <p>{discovered} jobs seen · {matched} matched your profile · run took {duration}</p>
</div>
{banner}
<div class="stats">
  <div class="stat"><b>{applied_n}</b><span>Applied</span></div>
  <div class="stat"><b>{manual_n}</b><span>Need you</span></div>
  <div class="stat"><b>{skipped_n}</b><span>Skipped</span></div>
</div>
<div class="sec">
  <h2>Applied automatically ({applied_n})</h2>
  {applied_html}
</div>
<div class="sec">
  <h2>⚠ Could not apply — please do these manually ({manual_n})</h2>
  {manual_html}
</div>
{errors_html}
<div class="foot">jobhunter · de-duplicated against every previous run, so nothing here is a repeat.</div>
</div></body></html>"""


def _fmt_duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def _job_html(outcome, show_reason: bool) -> str:
    job = outcome.job
    reason = (
        f'<div class="why">{outcome.reason}</div>' if show_reason and outcome.reason else ""
    )
    return (
        f'<div class="job"><a href="{job.target_url}">{job.title}</a>'
        f'<div class="meta">{job.company} · {job.location or "location n/a"} '
        f'· via {job.source} · match {outcome.score:.0%}</div>{reason}</div>'
    )


def build_subject(report: RunReport) -> str:
    applied, manual = len(report.applied), len(report.manual)
    prefix = "[DRY RUN] " if report.dry_run else ""
    if applied == 0 and manual == 0:
        return f"{prefix}Job hunt: nothing new today"
    tail = f", {manual} need you" if manual else ""
    return f"{prefix}Job hunt: applied to {applied} job{'s' if applied != 1 else ''}{tail}"


def build_text(report: RunReport) -> str:
    """Plain-text body — also used verbatim for Telegram and desktop notifications."""
    applied, manual = report.applied, report.manual
    lines = [
        f"Job hunt summary — {report.started_at.astimezone().strftime('%A, %d %B %Y %H:%M')}",
        "=" * 60,
        "",
    ]
    if report.dry_run:
        lines += ["*** DRY RUN — forms were filled but nothing was submitted. ***", ""]

    lines += [
        f"Discovered : {report.discovered}",
        f"Matched    : {report.matched}",
        f"Applied    : {len(applied)}",
        f"Need you   : {len(manual)}",
        f"Duration   : {_fmt_duration(report.duration_seconds)}",
        "",
        f"APPLIED AUTOMATICALLY ({len(applied)})",
        "-" * 60,
    ]
    if applied:
        for o in applied:
            lines += [
                f"  • {o.job.title} — {o.job.company} ({o.job.location or 'n/a'})",
                f"    match {o.score:.0%} · {o.job.target_url}",
            ]
    else:
        lines.append("  (none)")

    lines += ["", f"COULD NOT APPLY — PLEASE DO THESE MANUALLY ({len(manual)})", "-" * 60]
    if manual:
        for o in manual:
            lines += [
                f"  • {o.job.title} — {o.job.company} ({o.job.location or 'n/a'})",
                f"    why: {o.reason}",
                f"    apply here: {o.job.target_url}",
            ]
    else:
        lines.append("  (none — everything that matched went through)")

    if report.source_errors:
        lines += ["", "SOURCE PROBLEMS", "-" * 60]
        lines += [f"  • {name}: {err}" for name, err in report.source_errors.items()]

    lines += ["", "Nothing above is a repeat — every job is checked against previous runs."]
    return "\n".join(lines)


def build_html(report: RunReport) -> str:
    applied, manual = report.applied, report.manual

    applied_html = (
        "".join(_job_html(o, show_reason=False) for o in applied)
        or '<p class="none">No applications submitted this run.</p>'
    )
    manual_html = (
        "".join(_job_html(o, show_reason=True) for o in manual)
        or '<p class="none">Nothing needs your attention — everything that matched went through.</p>'
    )

    errors_html = ""
    if report.source_errors:
        rows = "".join(f"<div class='job'>{name}: {err}</div>" for name, err in report.source_errors.items())
        errors_html = f'<div class="sec"><h2>Source problems</h2>{rows}</div>'

    banner = (
        '<div class="banner">DRY RUN — forms were filled but nothing was submitted.</div>'
        if report.dry_run else ""
    )

    return _HTML.format(
        css=_CSS,
        date=report.started_at.astimezone().strftime("%A, %d %B %Y"),
        discovered=report.discovered,
        matched=report.matched,
        duration=_fmt_duration(report.duration_seconds),
        applied_n=len(applied),
        manual_n=len(manual),
        skipped_n=len(report.by_status(Status.SKIPPED)),
        applied_html=applied_html,
        manual_html=manual_html,
        errors_html=errors_html,
        banner=banner,
    )


def build_short(report: RunReport, limit: int = 6) -> str:
    """Chat-sized summary for Telegram / desktop toasts."""
    applied, manual = report.applied, report.manual
    parts = [f"🤖 Applied to {len(applied)} job(s)."]
    for o in applied[:limit]:
        parts.append(f"  ✅ {o.job.title} — {o.job.company}")
    if len(applied) > limit:
        parts.append(f"  …and {len(applied) - limit} more")

    if manual:
        parts.append(f"\n⚠️ {len(manual)} could NOT be applied to automatically — please do them manually:")
        for o in manual[:limit]:
            parts.append(f"  • {o.job.title} — {o.job.company}\n    {o.reason}\n    {o.job.target_url}")
        if len(manual) > limit:
            parts.append(f"  …and {len(manual) - limit} more (see the email for the full list)")
    return "\n".join(parts)
