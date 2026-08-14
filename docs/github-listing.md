# GitHub listing — what to paste where

Everything below is copy-paste ready. None of it changes the code; it changes whether
anyone finds the code.

Why it matters: the auto-apply space is saturated at the top — `Jobs_Applier_AI_Agent_AIHawk`
has ~30k stars and owns every generic "AI job applier" query. What is *not* saturated is
Naukri. When this was checked, the leading Naukri auto-apply repo had 44 stars and the
exact-name space had 2. That is the niche this project can actually be found in.

---

## 1. Repository name

```
naukri-linkedin-auto-apply
```

**Settings → General → Repository name.**

GitHub keeps redirects permanently, so existing clones and links keep working. Afterwards,
optionally point your local checkout at the new URL:

```bash
git remote set-url origin https://github.com/mohanrajuap/naukri-linkedin-auto-apply
```

Alternatives, if you would rather stay broad than own the niche:

| Name | Competing repos | Top competitor |
|---|---|---|
| `naukri-linkedin-auto-apply` | ~47 | 2 stars |
| `auto-apply-jobs` | ~84 | 5 stars |
| `job-auto-apply-bot` | ~135 | 804 stars |
| `jobhunter` (current) | ~1,531 | 221 stars |

---

## 2. Description

**Settings → General → Description**, or the ⚙ next to "About" on the repo home page.

```
Autonomously finds and applies to jobs on Naukri, LinkedIn, Workday and 8 ATS platforms. Resume-based matching, multiple roles per resume, desktop app, daily scheduling. Python + Playwright.
```

(349 characters — GitHub's limit is 350.)

---

## 3. Topics

**The single highest-leverage change here.** Topics drive GitHub's own browse and search,
and they are indexed by Google. Limit is 20; this is 20.

Repo home page → ⚙ next to "About" → Topics:

```
naukri
linkedin
job-search
job-application
auto-apply
job-bot
job-scraper
job-hunting
greenhouse
lever
workday
ats
applicant-tracking-system
automation
playwright
python
resume-parser
india
recruitment
jobs
```

---

## 4. Realistic expectations

A rename will not put this on the first page of Google. Repos rank there mainly on stars
and inbound links, and the top of this category is entrenched.

What it *will* do is win the long tail. Someone searching "naukri auto apply python" or
"workday job scraper" has very few good options today. Topics plus a first README line that
plainly states what the tool does is what surfaces you for those.

If you want more reach beyond that, the things that actually move it:

- Answer the relevant questions on r/developersIndia and r/india job threads with a link,
  when it genuinely helps someone
- Add a `LICENSE` file — many people will not touch an unlicensed repo
- A screenshot or short GIF of the Search tab in the README, near the top
- Submit to `awesome-job-search` style lists
