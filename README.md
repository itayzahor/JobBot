# JobBot — Automated Job Filtering System

JobBot crawls LinkedIn and Indeed on a schedule, filters out jobs that don't match my
title/location/experience criteria (using both rule-based checks and an LLM), deduplicates
everything, and serves the survivors on a local dashboard — so I only ever look at jobs actually
worth applying to, without opening either site myself.

It runs unattended in the background on Windows (Task Scheduler), self-throttles so it's safe to
invoke constantly, and survives the computer sleeping, restarting, or being off for a few days.

## Why

Manually scrolling LinkedIn/Indeed and re-reading the same "5+ years required" postings over and
over is a waste of time. This automates the boring filtering step and leaves only a decision:
apply or don't.

## Pipeline

```
LinkedIn + Indeed  →  Scraper (JobSpy)  →  Filters (title → language → Gemini)  →  SQLite (dedup)  →  Flask dashboard
```

### 1. Scraping
- Built on [`python-jobspy`](https://pypi.org/project/python-jobspy/), queried once per
  (search term × site) pair across 9 broad search terms (software engineer, data scientist, ML
  engineer, product manager, etc.) covering a 25-mile radius around Tel Aviv.
- Full job descriptions are fetched (not just titles), since the filtering steps need them.
- Two scraper-level bugs were found and worked around directly:
  - Indeed's "hours old" filter only works in day-level buckets — asking for anything under 48h
    silently returns zero results even though jobs exist. The scraper detects this and floors
    Indeed's window at 48h regardless of what the rest of the pipeline requests.
  - JobSpy's LinkedIn date parser misses real posting dates whenever an `hours_old` filter is
    used (a JobSpy selector gap, not missing data — confirmed via raw HTML). Patched at import
    time so LinkedIn dates come through correctly instead of showing up as blank.
- The same job posted to both sites gets two different URLs, so it's cross-matched by comparing
  descriptions (fuzzy match, not exact — the "same" text differs slightly between the two sites)
  instead of relying on the URL alone.

### 2. Filtering
Every job runs through three passes before it's allowed to reach the dashboard, cheapest first:

1. **Title filter** — rejects seniority levels (Senior, Staff, Principal, Director, "Engineer
   III", etc.) and generic "Manager" titles outright, including Hebrew equivalents — no API call
   needed for the jobs that are obviously not a fit.
2. **Language filter** — drops postings that aren't Hebrew or English before spending an LLM call
   on them.
3. **Gemini call** — everything that survives the first two passes is read by Google's Gemini
   (`gemini-3.1-flash-lite`, free tier), which extracts the *explicit* years-of-experience
   requirement from the text and compares it against my actual experience — deliberately ignoring
   vague "extensive experience" language that has no real number behind it, so genuinely
   open-to-junior roles aren't excluded just because of confident-sounding wording.

Every decision is stored with a reason, so any result can be spot-checked later.

### 3. Storage & deduplication (SQLite)
- Jobs are hashed by URL so nothing gets classified (or shown) twice, even across separate runs
  hours apart.
- Each job has a status — `new`, `applied`, `process` (interviewing), `deleted`, or `rejected`
  (auto-filtered, never shown) — and a full history is kept: an "applied" counter survives even
  after old rows are cleaned up, so the all-time application count is never lost.
- Old, no-longer-relevant rows (rejected/deleted/expired) are cleaned up automatically on a
  rolling basis; active applications and interview notes are kept indefinitely.

### 4. Dashboard (Flask)
A local web app with four tabs:

| Tab | Shows | Actions |
|---|---|---|
| **New** | Jobs that passed every filter | Apply, Delete |
| **Deleted** | Jobs I looked at and rejected myself | Restore, Apply |
| **Applied** | Open applications | Move to Process, Undo |
| **Processes** | Active/past interview processes | Free-text notes per job |

- Each job shows title, company, location, source site, full description, and how long ago it
  was posted (using the real posting date when available, otherwise "added" time).
- A **"Scrape Now"** button triggers an on-demand scrape from the dashboard itself, running in the
  background so the page never blocks — with a guard against starting two scrapes at once.

## Running in the background

The whole point is that I never have to open a terminal for this. Two Windows Task Scheduler
jobs handle everything:

- **Pipeline task** — runs every 15 minutes, but the pipeline itself self-throttles and only
  actually scrapes roughly every 6 hours; every other invocation is a no-op that exits instantly.
  If the computer was asleep or off past a scheduled run, Windows fires the missed run as soon as
  it's back on, and the pipeline automatically widens its search window to cover the gap so
  nothing posted while it was off gets missed.
- **Dashboard task** — starts the Flask app automatically at login, so the dashboard is always
  reachable without manually starting anything (a desktop shortcut is also there for convenience).

Both run invisibly (no flashing console windows) and log to file instead of a terminal, since
nothing is attended.

**Why not a Telegram bot?** Push notifications were considered and dropped: the computer has to
be on regardless for any of this to run, so a dashboard I check when I want is simpler than
maintaining a bot for the same result.

## Tech stack

- **Python** — `python-jobspy` (scraping), `pandas` (data handling), `Flask` (dashboard),
  `google-genai` (Gemini API), `langdetect`
- **SQLite** — local storage, no external DB server
- **Windows Task Scheduler** — scheduling, no external cron/hosting needed
- Everything runs locally on my own machine — no server costs, no external service dependencies
  beyond the free-tier Gemini API

## QA tooling

A permanent, checked-in test harness (`qa/`) re-runs the full filter pipeline against a frozen,
already-scraped dataset and generates a standalone review page comparing results across three
independent lenses (no filtering, title filter only, full pipeline) — used to catch regressions
whenever the title filter or the Gemini prompt changes, without burning API calls or waiting on a
live scrape.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # add your Gemini API key
python pipeline.py     # run the scraper + filters once
python app.py           # start the dashboard at http://localhost:5000
```
