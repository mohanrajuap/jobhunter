# jobhunter

Finds jobs that match your resume every morning, applies to the ones it can, and emails
you a list of the ones it couldn't so you can finish them by hand.

Runs as a desktop app or as a scheduled background task.

```
Discover ──▶ De-duplicate ──▶ Match ──▶ Apply ──▶ Notify
 8 sources    vs. history    per role   browser   email/Telegram
```

---

## What it actually does

**Discovery** pulls open roles from public ATS APIs — Greenhouse, Lever, Ashby,
SmartRecruiters, Workable, Recruitee — plus **Naukri**, **LinkedIn**, and any company
careers page you point it at. A single run routinely sees a few thousand postings. Give it
a careers URL and it sniffs which ATS is behind the page and uses that platform's API
instead of scraping. Pick which sources run from the dropdown on the Search tab.

**Matching** scores every job against your roles. Keywords come out of your resume
automatically; you can add more. Each role carries its own resume(s), and the winning role
decides which resume gets uploaded.

**Applying** drives a real browser, because every application is a real form. It fills what
it can from your saved details and submits. When it hits something it can't answer honestly
— a captcha, a login wall, a screening question you haven't given it an answer for — it
**stops and queues the job for you** instead of guessing.

**Notification** is the morning summary: what it applied to, and what needs you.

---

## Install

```bash
git clone https://github.com/mohanrajuap/jobhunter
cd jobhunter
pip install -r requirements.txt
python -m playwright install chromium
```

Python 3.10+. The Chromium download is ~150 MB and is required — applications are submitted
through a real browser.

---

## Quick start

```bash
cp config/config.example.yaml config/config.yaml
```

Then either edit `config/config.yaml` directly, or use the UI:

```bash
python run_gui.py
```

Fill in **My Details** and **Roles & Resumes**, hit Save on each, then check everything:

```bash
python -m jobhunter doctor
```

`doctor` verifies your dependencies, resumes, board slugs and notification settings. Fix
anything marked ✗ before going further.

See what it *would* apply to, without applying:

```bash
python -m jobhunter discover
```

Tune `search.min_score` until that list looks right. Then a full dry run — fills every form,
screenshots the result, submits nothing:

```bash
python -m jobhunter run --dry-run
```

Check `screenshots/` and confirm the forms are filled correctly. **Only then** go live:

```bash
python -m jobhunter run --live
```

---

## The desktop app

```bash
python run_gui.py
```

| Tab | What it's for |
|---|---|
| **Search** | Pick your sources and browser, hit Search. Results show score, role, which resume was picked, and whether you've **already applied**. Select rows and apply to just those, or apply to everything new. Double-click opens the posting; right-click for actions. |
| **My Details** | The saved form — name, contact, CTC, notice period, screening answers. These values are what get typed into company application forms. |
| **Roles & Resumes** | Multiple roles, each with its own titles, keywords and resumes. Add several resumes to one role and each job gets whichever one matches it best. |
| **Activity** | Live log of the current run. |

### Apply modes

- **Automatic** — fills the form and submits it. Respects the dry-run checkbox.
- **Manual review** — fills the form, leaves it open in the browser, and waits. You check
  it and click submit yourself. Nothing is sent without you.

### Which browser it drives

The Browser dropdown lists what's actually installed — **Chrome, Brave, Edge**, or
Playwright's bundled Chromium. Using one you already have skips the 150 MB download.

Tick **Use my logged-in profile** to run against your real browser profile, so sites
you're already signed into (Naukri, LinkedIn) stay signed in. That browser has to be
**completely closed** while a run happens — including any system-tray icon — because
Chromium won't share a profile between two programs.

### Filtering a search

**Posted within** (any time / 24h / 3 days / week / 2 weeks / month) and **Locations**
(comma-separated) sit above the results and apply to **every source**. Sources that can
filter server-side get it pushed into the query — LinkedIn's age band, Naukri's `jobAge`
and location list — which cuts how much is fetched. ATS boards and scraped career pages
have no such concept, so the same filter is enforced centrally when scoring. That's what
makes "last 3 days, Chennai only" behave identically whether a job came from Naukri or a
company's own careers page.

These are per-run choices and are not written back to your config file.

The results grid shows **posted date** ("today", "5d ago", "3w ago") and **applicants**
alongside location. Applicant counts come from LinkedIn's posting pages — real numbers
like 25, 198, 200. ATS boards don't publish them, so those rows show a dash rather than a
fake zero.

Results **stream in as they're found** — each source fills the grid while the slower ones
are still working, rather than everything appearing at the end. **Stop** interrupts a
search at the next checkpoint (it finishes the HTTP request already in flight, so it's
"stopping", not an instant kill).

### Jobs you applied to yourself

Select any row and hit **"I applied to this myself"**. It's written to the database, the
row turns green, and the tool will never apply to that job automatically. **Undo**
reverses it — though an application the tool actually submitted stays on the record.

### Teaching it what you don't want

Hit **"✗ Not relevant"** on jobs that miss the mark. Each one is written to a `feedback`
table, and the signals are read back at scoring time:

- **Company** — reject 3+ jobs from the same company and it stops surfacing them.
- **Title words** — reject 3+ jobs sharing a distinctive word ("telecalling", "voice") and
  similar titles get pushed down the rankings.

Title words are a **penalty, not a filter**, capped at 0.35 — the same word can appear in a
job you'd want, so this reshuffles the ranking rather than hiding things.

One guard matters: **words from your own target roles can never become negative signals.**
Without it, rejecting a few "Voice Process *Support* Engineer" jobs would teach it that
"support" is bad and bury your entire target role. Verified in the tests: after rejecting
four such jobs, "Application Support Engineer" still scores 1.00 while "Voice Process
Support Engineer" drops to 0.46.

Thresholds are tunable under `search:` — `feedback_company_threshold`,
`feedback_term_threshold`, `feedback_max_penalty`.

### Building a standalone .exe

```bash
pip install pyinstaller
python scripts/build_exe.py
```

Produces `dist/jobhunter.exe`, which runs on a machine without Python. Your config, resumes
and history stay as normal files beside it. Chromium still needs installing once.

---

## Multiple roles, multiple resumes

```yaml
roles:
  - name: "Application Support"
    titles: ["Application Support Engineer", "Production Support Engineer"]
    resumes:
      - path: "C:/Users/you/Documents/resume-support.pdf"
        label: "support"
    keywords: ["incident management", "servicenow"]

  - name: "DevOps / SRE"
    titles: ["Site Reliability Engineer", "DevOps Engineer"]
    resumes:
      - path: "C:/Users/you/Documents/resume-devops.pdf"
        label: "devops-primary"
      - path: "C:/Users/you/Documents/resume-cloud.pdf"
        label: "cloud-variant"
    keywords: ["kubernetes", "terraform"]
    overrides:
      min_score: 0.45        # any `search:` setting can be overridden per role
```

Every job is scored against every role. The best-scoring role wins, and within that role the
resume whose keywords best fit the job description is the one uploaded. A Kubernetes-heavy
posting gets your Kubernetes CV; a database-heavy one gets the other.

---

## Naukri

Naukri's search API returns `406 recaptcha required` to anonymous callers, so it runs inside a
logged-in browser instead. Sign in once:

```bash
python -m jobhunter login naukri
```

A browser opens; you sign in by hand — including OTP and captcha — and press Enter. The session
is saved to a browser profile and reused by every run until Naukri logs you out.

**The tool never types your password.** That's deliberate: it keeps your credentials out of the
config, and manual sign-in is the only thing that reliably survives OTP and 2FA anyway.

Naukri jobs come out in three ways:
- **Easy apply** — automated.
- **Chatbot screening** — automated only if `profile.answers` covers the questions asked.
- **"Apply on company site"** — always queued for you, since it redirects somewhere unknown.

---

## Company career pages

Add any company's careers URL on the **Career Pages** tab. Two things happen, in order:

1. **ATS sniffing.** Most "custom" career pages are a thin wrapper over Greenhouse, Lever,
   Ashby, Workable or SmartRecruiters. If the page links to one, the board slug is pulled
   out and that platform's API is used — clean structured data, no scraping. Razorpay, for
   example, resolves to a Greenhouse board and returns 23 jobs directly.
2. **Scraping.** Only if sniffing finds nothing. If the job list is drawn in JavaScript
   (most modern career sites), the page is re-opened in the browser and the rendered DOM
   is read instead.

Scraped links are filtered hard, because a careers page contains far more navigation than
jobs — "Log in", "Skip to main content" and "Explore jobs" all sit under a `/careers/` path
and would otherwise sail through. If a real posting gets filtered out, set an explicit
**Job link selector** (a CSS selector) for that page and it's trusted verbatim.

Use the **🔎 Test this page** button after adding one. It fetches immediately and tells you
what it found, which board it detected, or why it found nothing — detection failures are
otherwise silent until a real search comes back empty.

Sniffing is by far the more reliable path. If a page scrapes badly, look for the job board
behind it and point the URL straight there.

---

## LinkedIn

Discovery uses LinkedIn's logged-out job search, so finding jobs needs no account. In
testing it returned 83 matching Chennai/Bangalore roles in one pass.

Applying splits two ways:
- **"Apply on company website"** — the tool follows the link through to the real ATS and
  fills that form normally.
- **Easy Apply** — behind a login with its own screening modal, so it goes to your manual
  list with a direct link.

One caveat on scoring: LinkedIn's search cards carry no job description, only a title and
location. Keyword matching has nothing to bite on, so LinkedIn jobs top out around 65%
while a Greenhouse job with a full description can hit 90%+. That's expected — don't raise
`min_score` above ~0.6 if you want LinkedIn results.

LinkedIn's terms restrict automated access. This runs the same searches you'd run by hand,
at a human pace, but the account risk is yours.

---

## Oracle mirror (entirely optional)

**You do not need a database to use JobHunter.** Your whole application history lives in a
local SQLite file that requires no setup, and that file is the source of truth for
de-duplication. If you skip this section nothing is missing — the app behaves identically.

On first run the app creates your config and asks once whether you want Oracle. Say no and
it's off. You can change your mind any time under **Settings**, which has the connection
fields and a **🔌 Test connection** button. A database that's down, misconfigured, or whose
driver isn't installed never stops a run — it logs and carries on.

If you do have Oracle: every application — automated, or one you marked as "I applied to
this myself" — is also written to an Oracle table so you can query your job hunt with SQL.

```yaml
database:
  oracle:
    enabled: true
    user: "learn"
    password: env:ORACLE_PASSWORD    # set in .env
    dsn: "localhost:1521/FREEPDB1"
    table: "JOBHUNTER_APPLICATIONS"
    create_table: true
```

```bash
pip install oracledb
```

Thin mode — no Oracle Instant Client needed. The table and index are created on first run.

SQLite stays the source of truth for de-duplication: it's local and always available. Oracle
is a **mirror**, and if it's down the run carries on and logs the failure. Columns include
fingerprint, applied_at, status, company, title, location, source, ats, job_url, role_name,
resume_label, match_score, apply_mode and reason.

---

## Scheduling it

**Windows** (recommended — survives reboots and late wake-ups):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1 -Time "08:00"
```

Add `-Live` when you're ready for it to actually submit. `-Remove` uninstalls.

**Linux / macOS** — `python -m jobhunter schedule` prints the exact cron line.

**Any platform** — `python -m jobhunter daemon` stays in the foreground and fires on the
schedule in your config. Less reliable on a laptop that sleeps.

---

## Notifications

Email is the useful one, because it carries the full clickable manual list.

```yaml
notify:
  email:
    enabled: true
    smtp_host: "smtp.gmail.com"
    smtp_port: 587
    username: env:SMTP_USER
    password: env:SMTP_PASS
    to: ["you@example.com"]
```

Secrets live in `.env`, never in the config:

```
SMTP_USER=you@gmail.com
SMTP_PASS=your-16-char-app-password
```

Gmail needs an [App Password](https://myaccount.google.com/apppasswords), not your account
password. Telegram and desktop toasts are also supported. Test delivery:

```bash
python -m jobhunter notify-test
```

Every report is written to `reports/` regardless, so nothing is lost if a channel fails.

---

## What it won't do

These are design decisions, not gaps:

- **It won't guess at required questions.** If a form asks something your config doesn't
  answer, the application is abandoned and queued for you. A wrong answer to a screening
  question is worse than no application.
- **It won't solve captchas.** Those exist to keep automation out. Captcha → manual queue.
- **It won't type your passwords.** You log in yourself, once, per site.
- **It won't invent demographic data.** EEO questions default to "Decline to self-identify".
- **It won't claim success it can't verify.** No confirmation detected means the job is
  flagged for you to check, not counted as applied.

---

## Commands

| Command | Purpose |
|---|---|
| `run` | Full daily run. `--dry-run` / `--live` / `--limit N` / `--no-apply` |
| `gui` | Open the desktop app |
| `discover` | Find and score jobs, apply to nothing. `--show-rejected` explains filtering |
| `doctor` | Check config, dependencies, resumes, board slugs |
| `login <site>` | One-time manual sign-in; session persists |
| `parse-resume` | Show the keywords pulled from a resume |
| `stats` | History and the pending manual queue |
| `notify-test` | Send a sample summary through every channel |
| `schedule` | Print how to register the daily run |
| `daemon` | Stay running and fire on schedule |

---

## Tuning

Too few matches → lower `search.min_score`, widen `locations`, raise `posted_within_days`,
add board slugs under `sources:`.

Too many bad matches → raise `min_score`, set `strict_title_match: true`, add
`exclude_keywords`. Run `discover --show-rejected` to see exactly why things were filtered.

Exclusions match whole words, so `intern` won't knock out `internal` or `international`.

Remote roles are region-checked: `Remote - United States` is rejected for an India-based
profile, while a bare `Remote` passes. This is intentional — without it the tool applies to
roles you can't legally take.

---

## Safety rails

`apply.dry_run: true` is the default. Also worth keeping:

```yaml
apply:
  max_applications_per_day: 20
  max_per_company_per_day: 3
  delay_seconds: [25, 60]      # do not lower this
  headless: false              # watch it work until you trust it
```

The delay is not decoration — rapid-fire submissions get your IP blocked and look nothing
like a real applicant. Every job you've applied to is fingerprinted in SQLite
(`data/jobhunter.sqlite3`) by company + title + location, so the same role found on two
different sources counts once and is never applied to twice.

One thing to know before you switch on `--live`: bulk automated applications are against the
terms of service of some job sites, Naukri included. The caps and delays above keep the volume
human-scale, but the account risk is yours. Start with a small `max_applications_per_day`.

---

## Layout

```
jobhunter/
├── config.py, models.py, store.py     # config, types, SQLite history
├── roles.py                           # role targets + resume variants
├── browser.py                         # persistent Playwright session
├── pipeline.py                        # discover → match → apply → notify
├── resume/                            # PDF/DOCX parsing, keyword extraction
├── matching/                          # scoring, filters, multi-role selection
├── sources/                           # one adapter per ATS + careers-page sniffer
├── appliers/                          # form filler, generic ATS, Naukri
├── notify/                            # email, Telegram, desktop, report builder
└── gui/                               # Tkinter app
```

Run the tests with `python -m pytest tests/ -q`.

---

## Troubleshooting

**"Playwright is not installed"** — `pip install playwright && python -m playwright install chromium`

**Naukri finds nothing** — your session expired. Run `python -m jobhunter login naukri` again.

**A board returns 0 jobs** — the slug is wrong or the company moved ATS. `doctor` lists every
slug and what it returned.

**Resume keywords look thin** — `parse-resume` shows what was extracted. A scanned-image PDF
yields nothing; export a text-based PDF or use DOCX.

**Everything lands in the manual queue** — usually unanswered screening questions. The reason
is in the report; add matching entries to `profile.answers`.
