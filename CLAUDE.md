# Project: Automated Job Filtering System

## Goal
A script that crawls job boards, filters listings by title / location / required years of experience, and displays only the relevant ones with no duplicates. Saves time otherwise spent manually filtering out jobs that require more experience than the user has.

## Architecture (flow)
Job boards (LinkedIn, Indeed) -> Scraper (JobSpy) -> Filtering (title/language/Gemini) -> SQLite DB with dedup -> Flask dashboard. Self-throttling (`pipeline.run()`) plus Windows Task Scheduler stands in for a background service - no Telegram bot (considered, then dropped: the computer needs to be on regardless, so a pull-based dashboard is simpler than push notifications).

## Project layout
```
JobBot/
├── pipeline.py, app.py       # thin entry-point shims - see "Why pipeline.py/app.py stay at the root" below
├── start_dashboard.bat       # manual launcher (desktop shortcut target - see Scheduling)
├── scripts/run_pipeline_task.bat   # manual convenience only, NOT used by Task Scheduler (see Scheduling)
├── src/jobbot/                # the actual package - everything importable lives here
│   ├── paths.py               # single source of truth for data/log directory locations
│   ├── config.py, db.py, scraper.py, scrape_state.py, pipeline.py, webapp.py
│   ├── filters/                # title/language/regex/gemini filters
│   └── templates/              # Flask templates (Flask resolves this relative to webapp.py)
├── qa/                        # permanent test harness - unchanged location, imports from jobbot.*
├── data/                      # gitignored runtime state: jobs.db, scrape_state.json, output/
└── logs/                      # gitignored: pipeline.log, app.log
```
**Why `pipeline.py`/`app.py` stay at the root instead of moving into `src/jobbot/`:** the
`JobBot-Pipeline` and `JobBot-Dashboard` Windows Scheduled Tasks, `start_dashboard.bat`, and the
desktop shortcut all hard-code these two paths (`pythonw.exe pipeline.py` / `pythonw.exe app.py`,
project root as working directory). Keeping thin shims at those exact paths means the real package
restructure (below) needed zero changes to any of that live, external automation. Each shim just
adds `src/` to `sys.path`, imports the real logic from `jobbot.pipeline` / `jobbot.webapp`, and
handles the `--log-to-file` redirect (now writing to `logs/`) before delegating.

## Step 1 - Scraping
- Library: `python-jobspy` (`pip install python-jobspy`)
- Scrapes each (search term, site) pair as a *separate* `scrape_jobs` call, not one combined
  `site_name=["linkedin", "indeed"]` call - required because of the `hours_old` quirk below.
- `country_indeed="Israel"` (required for Indeed)
- `location="Tel Aviv, Israel"` (or another city - LinkedIn is global, doesn't need a country param)
- `linkedin_fetch_description=True` - critical, fetches the full description needed for step 2
- ZipRecruiter / Bayt / Glassdoor - not relevant / weak coverage for Israel, not used for now
- Indeed's `hours_old` filter works in day-level buckets, not real hours: confirmed any value from
  6-24h returns 0 results even when jobs clearly exist (LinkedIn works fine at any window) - only
  jumps to real counts at 48h. `scraper.SITE_HOURS_OLD` gives Indeed a `max(base, 48)` floor
  regardless of the base window, so it's never silently broken by a short base window.
- `scrape_all(hours_old_override=None)` - the base window is normally `config.HOURS_OLD`, but
  `pipeline.py` overrides it with the actual elapsed time since the last successful scrape (see
  `scrape_state.py` under Scheduling) so a gap - computer asleep/off - widens the window to cover
  what was posted during it, instead of the fixed default silently missing it. JobSpy's `hours_old`
  is int-only, so a fractional override is rounded UP, never down.
- **Real posting dates, patched at import time**: JobSpy's LinkedIn parser only looks for
  `<time class="job-search-card__listdate">`, but confirmed via raw HTML comparison that setting
  `hours_old` (which we always do) makes LinkedIn render the exact same date under
  `job-search-card__listdate--new` instead (a genuine JobSpy selector gap, not missing data -
  the date is right there, just under an extra `--new` suffix). Without setting `hours_old`,
  dates come through fine; with it, LinkedIn dates were 100% null before this fix. `scraper.py`
  monkeypatches `jobspy.linkedin.LinkedIn._process_job` once at import: run the original, and if
  `date_posted` is still `None`, check the `--new` class on the same `job_card` and fill it in
  (`.date()`, matching the model's field type - a bare `datetime.strptime()` result triggers a
  Pydantic serialization warning since the field wants `date`, not `datetime`). Indeed's
  `date_posted` was already reliable (0 nulls observed) and needed no patch. Isolated and
  low-maintenance (a handful of lines wrapping one method), but tied to JobSpy's current internals
  - if it ever silently stops helping (JobSpy changes its own parsing), the dashboard's fallback to
  `first_seen` still holds, so this degrades gracefully rather than breaking anything.
- Output: DataFrame with title, company, city/state, description, job_url, date_posted, plus a
  `search_terms` column (which of `config.SEARCH_TERMS` surfaced this job - a job can match more
  than one; comma-joined) computed at dedup time, since `drop_duplicates` would otherwise silently
  discard that provenance
- The same real job gets a different `job_url` on each site, so URL-based dedup alone lets it
  through twice. `scraper._dedup_cross_site` catches this with a fuzzy match on description
  content (`SequenceMatcher` ratio >= 0.97 after stripping punctuation), pre-filtered to pairs
  sharing the same (company, title) - company alone isn't tight enough (a recruiting agency like
  "Gotfriends" posts dozens of genuinely distinct roles under one company). Location is NOT part
  of the pre-filter: Indeed returns city/region names in the site's local script (e.g. Hebrew for
  Israeli listings) regardless of the request's locale headers - JobSpy already sends
  `indeed-locale: en-US`, Indeed just doesn't honor it for place names - so a LinkedIn
  "Tel Aviv-Yafo" would never string-match Indeed's "תל אביב -יפו" for the same job. Exact-match
  on description was confirmed insufficient even after normalization: the same NVIDIA posting
  rendered "doing**" on LinkedIn vs "doing:**" on Indeed, and LinkedIn's copy had an extra trailing
  req id ("jr2025271") Indeed's didn't. When merged, LinkedIn's row is kept over Indeed's (it's the
  platform actually browsed day to day) and their
  `search_terms` are unioned.

## Step 2 - Filtering
`jobbot.filters.filter_job(title, description, location=None) -> (min_years, decision, reason)`.
Every path returns a `reason` string, stored and shown on the dashboard so the user can spot-check
the call at a glance.

- Pass 0 - title filter (`src/jobbot/filters/title_filter.py`), runs before any other check:
  - Rejects seniority/leadership titles outright: senior, sr./sr, principal, staff (sits above
    Senior on most IC ladders - Engineer -> Senior -> Staff -> Principal), mid-level, head of,
    director, vp/vice president, chief, team lead/leader, and company leveling suffixes (e.g. "Software
    Engineer III" - Roman numeral III+ as its own token; II and below is fine) - applies
    universally, including to Product Manager titles.
  - Rejects generic "manager" titles, except "product manager" and "program manager" (individual-
    contributor roles despite the title). Substring match, so "Technical Program Manager" is
    covered by the plain "program manager" check - no need to separately spell out qualifiers.
  - Hebrew equivalents included: מנהל (manager, exempts מנהל מוצר), בכיר (senior),
    ראש צוות (team lead), and כלכל* (economist - not a seniority signal, just an irrelevant
    profession that slips through broad search-term matches).
  - Rejects "Field Engineer" / "Field Applications Engineer" / "Field Service Engineer"-style
    titles (`\bfield\s+(?:\w+\s+)?engineer\b`) - customer-site/travel-heavy roles the user noticed
    slipping through, same "wrong field entirely" category as economist/student.
- Pass 0 - location filter (`src/jobbot/filters/location_filter.py`): rejects a job whose location
  contains Ashdod (English or אשדוד) - outside the commute range `config.DISTANCE_MILES` is meant
  to enforce, but the user kept seeing Ashdod postings anyway. Not fixed by shrinking the radius:
  LinkedIn only supports fixed radius steps (5/10/25/50/75/100 mi), and the next step down (10mi)
  would also cut out Ra'anana/Holon, which are wanted - so this is a location-text filter instead,
  same shape as the title filter. Checked in `filter_job` alongside the title check, before
  language/Gemini.
- Pass 0.5 - language filter (`src/jobbot/filters/language_filter.py`): rejects descriptions that aren't
  Hebrew or English (via `langdetect`) before spending a Gemini call - a non-Hebrew/English
  posting is assumed not relevant to the user regardless of what it requires.
- Years-of-experience regex on the description (`src/jobbot/filters/regex_filter.py`) was tried and then
  dropped from the live pipeline - "3-5 years" phrasing has too many forms to chase reliably
  (en dashes vs hyphens, Markdown-escaped punctuation, degree-conditional alternates, a number
  landing in a "nice to have" section rather than requirements), and Gemini reads all of it
  correctly without a growing pile of special-case patterns. The file is kept as a fallback: if
  Gemini's free-tier quota ever becomes the binding constraint, reintroduce it as a cheap first
  pass ahead of Gemini rather than deleting it outright.
- Gemini call (`src/jobbot/filters/gemini_filter.py`) now runs for every job that clears title +
  location + language filtering, not just ones a regex pass couldn't classify:
  - **Live production prompt (promoted 2026-09-19): `quote_first_background_v3`**, the winner of the
    `qa/prompt_eval.py` comparison on real human labels (recall 1.000, precision 0.309 - vs. 0.971 /
    0.237 for the previous years-only prompt; full table in the `# SCORES` comments in
    `qa/prompt_variants.py`). It makes two independent judgments per job: `years_estimate` (explicit
    number/range only; quote-the-sentence-first; a capped carve-out when the posting itself invites
    less-experienced candidates) and `relevant_background` (is the role's core discipline software /
    data / ML-AI / product-or-technical-program management, vs. a clearly different profession -
    sales, business intelligence, support, hardware technician, admin, ...; permissive when
    ambiguous). **`fit` is never asked of the model** - `classify()` derives
    `fit = years_estimate <= MAX_YEARS_EXPERIENCE and relevant_background` in Python (the model's own
    `fit` field was measured disagreeing with its own `years_estimate` at the boundary). The prompt
    text is the plain string `gemini_filter.SYSTEM_INSTRUCTION`; `qa/prompt_variants.py` imports it
    so the eval and production can't drift, and keeps the old prompt frozen as `LEGACY_CURRENT`.
    Trade-off accepted: ~1255 tokens/call vs ~1120 for the old prompt.
  - Model: `gemini-3.1-flash-lite` (`gemini-3-flash-lite` does not exist as a model name). Billing is
    enabled on the API key, so the free-tier daily cap described below no longer binds.
  - The bullets below through "Case battery" describe the **previous years-only prompt**. They stay as
    the rationale for behavior the v3 prompt preserves (explicit-number-only, "or" branches).
  - The previous prompt: `system_instruction` was an f-string parameterized by
    `config.MAX_YEARS_EXPERIENCE`. Two-step: Gemini first estimates
    `years_estimate` from an EXPLICIT number/range only (0 if none is given; lower bound of a
    range; ignores a lower number that only applies with an advanced degree the candidate doesn't
    have), then `fit = years_estimate <= MAX_YEARS_EXPERIENCE`.
  - Deliberately does NOT let qualitative scope/expertise language ("proven experience,"
    "substantial experience," "extensive experience," "mandatory," a disclaimer that project
    experience "isn't a substitute" for professional experience) push years_estimate up on its
    own, however strongly worded, when there's no explicit number - per explicit, repeated user
    direction ("I would not mind some false positives"; confirmed again on a specific job with
    "recent and substantial hands-on experience... Mandatory" language and no number, which the
    user said they'd apply to anyway). A clearly senior-titled job is already filtered out before
    Gemini ever sees it (title filter, Pass 0), so this isn't relied on as a second seniority check.
  - Also tells it that an "or" between two qualification branches (e.g. "B.Sc. OR alumnus of an
    elite IDF unit") means satisfying either is enough - added after a real miss where it read the
    more demanding branch as required regardless, on a posting most Israeli tech job seekers will
    encounter in some form (degree-or-equivalent-military-service framing is common here).
  - Both of the above confirmed fixed: verified against the real flagged posting for the "or" case,
    and against a full real run of `qa/build_review.py` (112 jobs, 0 errors after quota recovered)
    for the permissive-by-default change.
  - Case battery to regression-test after any future prompt change: explicit "N+ years", explicit
    "N-M years" range, qualitative-only "substantial/extensive experience" with no number (should
    be fit=true), vague/no requirement, an "N years but projects don't substitute" disclaimer, an
    "X or Y" alternative-qualification requirement, the German ADMS "advanced experience" case.
  - `contents` = job title + description (any language - not a problem)
  - `classify(title, description, verbose=False)` - the `verbose` flag is the production/test split
    (see "Test tooling" below): `verbose=False` (production default) requests the minimal
    `BackgroundJudgment(years_estimate, relevant_background)` schema - no `reason` field, since
    generating a full sentence is the one part of this call with real added latency/output-token
    cost. `verbose=True` (`qa/` only) requests `VerboseBackgroundJudgment`, adding `reason`. Both
    paths share one client, one throttle/retry engine, one `SYSTEM_INSTRUCTION` - only the requested
    schema differs, so a prompt fix always applies to both automatically. The return is always a
    `JobFitResult(fit, years_estimate, reason)` with `fit` derived as described above.
  - `decision` stored in the DB is `"yes"` if `fit` else `"no"` (no three-way enum - ambiguous cases
    already lean `fit=true` per the prompt).
  - Rate-limited on the free tier per-minute AND per-day (500 requests/day for this model - a hard
    cap, distinct from the per-minute limit) - calls are throttled and retried with backoff on
    429/5xx before giving up for the rest of a run. If quota is exhausted, remaining jobs default
    to `decision="yes"` (not auto-screened, flagged for manual review) rather than being hidden.
  - `classify()` also takes optional `system_instruction` (override the module `SYSTEM_INSTRUCTION`)
    and `return_usage` (return `(JobFitResult, usage_metadata)` instead of just `JobFitResult`) -
    both `qa/prompt_eval.py`-only, unused by production, added so the eval harness can drive
    arbitrary prompt text through the exact same client/throttle/retry path instead of duplicating it.
    `judge_background` defaults to `True` (the production schema above); `False` selects the legacy
    years-only schema where the model outputs `fit` itself, kept solely so the eval can still score the
    older variants.

## Prompt-engineering evaluation (`qa/prompt_eval.py`) and the promoted prompt
Goal: score candidate `SYSTEM_INSTRUCTION` rewrites against real human labels instead of against
Gemini's own past output (the frozen `qa/data/` dataset has zero human labels - every field in it
is Gemini's own verdict, useless as ground truth).

- **`pipeline.py --force --hours-old N`**: forces an immediate scrape of the last N hours,
  overriding the normal auto-computed window - a manual one-off for pulling in a bigger batch to
  label right away instead of waiting for the next scheduled run (real scrapes are self-throttled
  to `config.SCRAPE_INTERVAL_HOURS`), used to jump-start ground-truth collection after flipping
  `COLLECTING_GROUND_TRUTH`. Not used by any scheduled task.
- **Ground truth comes from real dashboard use, not a separate labeling tool**: `status='applied'`/
  `'process'` -> label `True` ("I'd apply to this"), `status='deleted'` -> label `False` ("I saw
  this and didn't want it"). Noisier than a dedicated per-job judgment (a deletion could be about
  company/role fit, not years-of-experience) but costs no extra manual effort - accepted trade-off,
  per explicit user direction.
- **`config.COLLECTING_GROUND_TRUTH` (now `False` - turned off 2026-09-19 when `v3` was promoted, so
  Gemini gates automatically again and 7-day retention has resumed; see the merge note under
  `qa/collect_ground_truth.py` below for why labels survive that)**: while it was set,
  `filter_job()` skips the Gemini call entirely (title/location/language filters still run) so
  every job that clears them reaches the dashboard as `'new'` - this closes the biggest blind spot
  in the applied/deleted signal, since a job Gemini would score `fit=false` normally never reaches
  the dashboard at all and so could never be applied/deleted on. Also makes `db.cleanup_old_rows()`
  skip the `new`/`deleted`/`rejected` retention rules (7 days), so labels aren't auto-deleted
  before `qa/collect_ground_truth.py` can use them. Flip back to `False` once enough labels have
  accumulated (see that script's printed count).
- **`qa/purge_filtered_rows.py`** (one-off, already run once): deletes existing `new`/`deleted`/
  `rejected` rows that today's title/location filters would reject - run once when the location
  filter and the "field role" title pattern were added, since older `deleted` rows predating those
  (and predating some now-standard seniority/manager patterns too) would otherwise contaminate the
  ground truth with "Gemini got this wrong" labels for jobs actually rejected for an unrelated
  reason. Reuses `title_filter`/`location_filter` directly rather than a separately-maintained
  pattern list. Leaves `applied`/`process` rows alone even if they match (real decisions, not
  noise). 379 of 696 checked rows were purged the first time it ran (most were old `'rejected'`
  rows already outside the ground-truth set - only `'deleted'` rows actually move the accuracy
  numbers, and 36 of 90 of those matched).
- **`qa/collect_ground_truth.py`**: extracts `qa/data/ground_truth.json` from the DB per the labels
  above; safe to re-run anytime as more accumulate. **Merges into the existing snapshot instead of
  overwriting it**: with retention back on, `deleted` rows are auto-cleaned after 7 days, so the DB
  is no longer a complete record of past labels - entries whose row has been cleaned are kept, and
  entries whose row still exists but changed status (e.g. Restore -> `new`) are dropped. Re-run it
  periodically to keep banking new applied/deleted labels for future prompt experiments. **The
  snapshot (`qa/data/ground_truth.json`) and the per-job scores keyed to it
  (`qa/data/prompt_eval_results.json`) are gitignored on purpose**: they record which jobs were applied
  to and deleted, and the GitHub repo is public. Only the aggregate `prompt_eval_report.json` is
  committed. A fresh clone rebuilds them locally with `qa/collect_ground_truth.py` + `qa/prompt_eval.py`.
- **`qa/rescreen_new_jobs.py`** (one-off, run at promotion time): jobs scraped while
  `COLLECTING_GROUND_TRUTH` was on reached the dashboard un-screened, so this ran the production filter
  over every `status='new'` row and moved rejects to `'rejected'`. Non-destructive - rows are kept, with
  Gemini's one-line reason stored in `reason` (the normal pipeline stores none) for spot-checking - and
  it only touches rows still `'new'` at update time.
- **`qa/prompt_variants.py`**: candidate `SYSTEM_INSTRUCTION` rewrites to score. Each entry is
  `{"instruction": ..., "judge_background": bool}` - `judge_background` tells `classify()` which
  request schema to use (see below).
  - `current` (production baseline), `shortened` (same rules, trimmed repetition), `fewshot`
    (short rule + 3 labeled examples instead of prose), `quote_first` (asks the model to quote the
    relevant sentence before estimating years).
  - `quote_first_background`: adds a second independent judgment, `relevant_background` (does the
    role's core discipline actually match what the candidate wants - SWE/data/ML/product-PM
    tracks - vs. a different profession entirely). Added after real usage data showed most false
    positives weren't years-of-experience mistakes at all: jobs like Business Intelligence
    Analyst, Application Engineer, Solution Architect, Pre-Sales Engineer, Industrial Engineer
    routinely have a fine (low/no) years requirement but are the wrong profession, and the
    years-only prompt was never asked to judge that dimension. `relevant_background` is
    deliberately permissive (defaults true when ambiguous or technical-adjacent), matching the
    same lean-permissive stance as the years judgment.
  - `quote_first_background_v2` folds an "employer explicitly says less-experienced candidates are
    welcome" carve-out into `years_estimate` (recovers the recall `quote_first_background` lost);
    `quote_first_background_v3` caps that carve-out to modest requirements (~2-4 years) after v2
    was seen zeroing out a 15-year requirement on generic "exceptional candidates considered"
    boilerplate, and calls Business Intelligence out as not-data-engineering.
  - Every variant's measured scores live in a `# SCORES [...]` comment block above its prompt in
    `qa/prompt_variants.py` (355 jobs, scored 2026-09-19): `quote_first_background_v3` is best -
    recall 1.000, precision 0.309, accuracy 0.786, ~1255 tokens/call - vs `quote_first` (1.000 /
    0.241 / 0.699, ~948) and `current` (0.971 / 0.237 / 0.699, ~1120). Precision 0.31 is still far
    from 0.50: what remains are mostly legitimate SWE/AI roles deleted for reasons a job
    description can't reveal (company, tech-stack interest, gut feel).
  - `qa/prompt_eval.py --variant NAME` scores a single variant on its own (repeatable flag).
  - **Follow-up round after reviewing what v3 hid in production** (user feedback: data analyst is CS
    work while BI is industrial-engineering work; and two hidden jobs carrying "requirement is flexible"
    wording should have been shown). `v4` fixed the analyst reasoning but not the exception - prose can't
    force the model to lower its own number. `v5` moved the exception into code
    (`classify(judge_flexibility=True)`: the model reports `experience_flexible`, Python applies it up to
    `config.MAX_YEARS_WITH_FLEXIBLE_REQUIREMENT = 4`), and it works on the two target jobs. **But v5 is
    NOT promoted**: on 357 jobs it scores recall 0.971 vs v3's 1.000 (loses one applied CRM-flavoured
    AI role to a "CRM/billing" example added to the business-side list), precision a wash
    (0.306 vs 0.309), +12% tokens. **v3 remains the production prompt.** Re-running the disagreeing jobs
    showed ~3 of 10 flip between identical runs, i.e. differences of a few jobs on a 357-job set are
    within noise - judge a candidate on stable flips and on recall, not on a headline precision delta.
    Note also that the years-only data-analyst worry was overstated: every hidden Data Analyst job also
    required 2-3 years and would have been hidden by the years rule regardless.
- **`gemini_filter.classify(..., judge_background=False)`**: when `True`, requests
  `BackgroundJudgment`/`VerboseBackgroundJudgment` (`years_estimate` + `relevant_background`, no
  `fit` field at all) instead of the normal schema, and derives
  `fit = years_estimate <= MAX_YEARS_EXPERIENCE and relevant_background` **in Python**, never
  asking the model for the combined boolean. This is a direct fix for a real bug `qa/prompt_eval.py`
  found: `current`/`shortened`/`fewshot` all sometimes output `years_estimate=1` (at the exact
  `MAX_YEARS_EXPERIENCE` cutoff) yet `fit=False` anyway - the model's own `fit` field can silently
  disagree with its own extracted number. `quote_first`'s explicit step-by-step derivation reduced
  this (recall 1.00 vs `current`'s 0.97 on real data) but still asks the model to state `fit`
  itself; `judge_background=True` removes that risk entirely for the dimensions it covers, by
  never asking for `fit` at all.
- **`qa/prompt_eval.py`**: scores every variant against `ground_truth.json`, saving each
  `(variant, job)` result incrementally to `qa/data/prompt_eval_results.json` (same crash-safety
  pattern as `qa/build_review.py`'s `stage2.json` - resumable, skips already-scored pairs).
  Primary metric is **recall on the "should show" class** (minimize false negatives, per the
  lean-permissive filtering stance), not raw accuracy; reports a Wilson 95% CI on recall so a small
  gap between variants can be read as noise given the sample size actually available; token count
  is the secondary/tiebreaker axis. Writes a ranked comparison to `qa/data/prompt_eval_report.json`.
  `--limit N` scores only the first N labeled jobs, for a quick smoke test.
  - **Why precision/accuracy are unreliable here, concretely**: on a 348-job real run, `quote_first`
    scored recall=1.00 but precision=0.23 (confusion matrix tp=34, fn=0, fp=112, tn=202) - the 112
    false positives are mostly `deleted` jobs the model was still right to call `fit=true` on
    (years-wise), deleted for an unrelated reason (company/location/interest, or - before
    `quote_first_background` - wrong profession entirely). Precision/accuracy conflate "wrong
    about years" with "right about years, wrong about everything else," which is why they're
    secondary metrics, not the ones prompt changes are judged on.
  - First real result (348 jobs, 34 positive, after `COLLECTING_GROUND_TRUTH` had gathered enough
    post-bypass data to actually exercise the blind spot): `quote_first` recall=1.00 (0 misses),
    `current` recall=0.97 (missed one job at the exact `years_estimate=1` boundary - the bug
    above), `shortened`/`fewshot` recall=0.91 each (both let the "don't infer seniority from
    qualitative language alone" guardrail lapse when they trimmed it - confirmed on a real miss:
    `shortened` output `years_estimate=0` but `fit=False` anyway, reasoning about a "Senior AI
    Engineer" title that shouldn't have influenced the years-only decision at all).

## Step 3 - Storage and dedup (SQLite)
`jobs` table:
```
id (normalized hash of job_url) PK
title, company, location, url, description
min_years, decision, reason, site, date_posted
status: 'new' | 'deleted' | 'rejected' | 'applied' | 'process'
first_seen, status_changed_at, notes
```
`notes` is free text, meaningful only for `status='process'` rows - what happened in the interview
process, why it failed, per explicit user request to record that qualitatively rather than as a
separate terminal status (see the `'interviewing'`/`'closed'` -> `'process'` merge below).
`db.update_notes()` writes it without touching `status_changed_at` (editing a note isn't a status
change and shouldn't bump the row's sort order).
`date_posted` (ISO `"YYYY-MM-DD"` string, or `NULL`) is the real posting date from the source when
JobSpy could extract one (see the scraping-patch note in Step 1) - `pipeline.py` converts the
scraped `datetime.date` via `str()`, and uses `pd.isna()` (not `is None`) to detect a missing
value, since pandas represents it as `NaN`, not `None`.
`reason` stays in the schema (dropping a SQLite column is more churn than it's worth for a
personal local DB) but `pipeline.py` no longer populates it - new rows get `NULL` there, since
`filter_job` is called with the default `verbose=False` (see Step 2) and Gemini's reason text is
`qa/`-only. `site` (`'linkedin' | 'indeed'`) is populated and shown on the dashboard.

`status_changed_at` is nullable with no table-level default (unlike `first_seen`) - SQLite
disallows a non-constant default, like a `datetime('now')` function call, on a column added via
`ALTER TABLE` to an already-existing table, which the migration path needs to do. `NULL` means
"never explicitly changed since creation," and every query treats that as equivalent to
`first_seen` via `COALESCE(status_changed_at, first_seen)` - also the semantically correct
fallback, since a status that was never touched "last changed" at creation time. `update_status`
sets it on every call.

Dedup: `pipeline.py` checks the URL hash BEFORE calling `filter_job` (not just before inserting) -
a job already in the DB is skipped without spending a Gemini call on it again. This matters more
now that Indeed's window (48h) overlaps runs a lot more than LinkedIn's does. Every job that is
classified gets inserted regardless of decision (rejected ones with `status='rejected'` immediately)
so a still-posted rejected job doesn't get re-classified on some future run either.

**`'deleted'` vs `'rejected'`**: two distinct statuses for two different things, split out after the
user noticed `/deleted` was dominated by jobs they'd never actually seen. `'rejected'` = the filter
pipeline screened the job out (`pipeline.py`'s `decision == "no"` path) before it ever reached the
dashboard. `'deleted'` = the job *was* shown as `'new'` and the user clicked Delete on it themselves.
Only `'deleted'` rows appear on `/deleted` - `'rejected'` rows are invisible by design (that's the
whole point of the filter) but still occupy a DB row so dedup remembers them. One-time migration
when this was introduced: existing `status='deleted'` rows were reclassified to `'rejected'` when
their `decision` was `'no'` (never shown to the user) and left as `'deleted'` when `decision` was
`'yes'`/`'unsure'` (shown as `'new'`, then manually deleted) - `decision` is a reliable signal here
because pipeline.py never sets `status='deleted'` directly, only `/delete/<id>` does.

**`'interviewing'`/`'closed'` merged into one `'process'` status.** Originally two statuses
(`applied` -> `interviewing` -> `closed` as a terminal "didn't work out" state), collapsed into a
single `process` status per explicit user request: an interview process that fails is recorded via
the `notes` field on the same row ("why did it fail and how did it go"), not by moving to another
status - there's no longer a dedicated terminal state to move to. `db.get_connection()` runs an
idempotent `UPDATE jobs SET status = 'process' WHERE status IN ('interviewing', 'closed')` on every
connection (same pattern as the `ALTER TABLE ADD COLUMN` migrations above it) so this is self-
healing with no separate one-off script to run.

**Persistent applied counter, independent of row counts.** `db.get_applied_count()` /
`db.mark_applied()` / `db.undo_applied()` maintain a `meta` table (`key`/`value`, currently just
`applied_count`) tracking the total number of jobs ever applied to - deliberately NOT derived by
counting rows with `status='applied'`, because:
- `applied` rows are auto-deleted after 90 days (see Retention below), which would silently shrink
  a naive `COUNT(*)` over time even though the user did apply to those jobs.
- a job moving `applied` -> `process` no longer has `status='applied'` at all, so a naive count
  would also drop it despite it still being a real, counted application.

`db.mark_applied()` (used by `/apply/<id>`) sets `status='applied'` AND increments the counter in
one call, so the two can never drift apart. `db.undo_applied()` (used by the new
`/undo_apply/<id>`, "I clicked Apply by accident") reverses both: back to `status='new'` and
decrements the counter. Moving `applied` -> `process` (`/interview/<id>`) deliberately does NOT
touch the counter - explicit user requirement, since that's the same application continuing, not a
new one. One-time seed when this was introduced: `applied_count` was initialized to
`COUNT(status IN ('applied', 'process'))` at migration time (the best available reconstruction from
existing data - older rows that had already been through the pre-existing weekly-equivalent cleanup
by that point, if any, aren't recoverable, but none had at that point in practice).

**Retention**: `db.cleanup_old_rows()`, called once at the end of every `pipeline.run()`,
permanently `DELETE`s rows per `db._CLEANUP_RULES` - a per-status day threshold, not one blanket
number: `new`/`deleted`/`rejected` at 7 days, `applied` at 90 days (long enough that an open
application doesn't vanish from view after a week, per explicit user request). `process` has no
entry in `_CLEANUP_RULES` at all and is never auto-deleted - kept indefinitely so its `notes` stay
available. Losing an `applied` row after 90 days never loses the *count* of it, since
`applied_count` (above) is independent of the row's existence.
Accepted trade-off: deleting a `new`/`deleted`/`rejected` row also erases its dedup memory, so if
that exact posting is still live and gets scraped again later, it could reappear as "new" -
considered rare/minor enough to accept rather than keeping a permanent "expired-but-remembered"
record.

## Step 4 - Display
- **Current phase:** local HTML on top of Flask - deliberately chosen first because it's easy to develop and debug against (just refresh the page) before adding the complexity of a bot.
  - Four pages, each with a `New | Deleted | Applied | Processes` nav bar (plain links, no
    client-side routing) to jump between them - these are the "windows" the user asked for,
    implemented as pages within one Flask app/browser tabs rather than separate server processes:
    - `/` (`dashboard.html`) - `status='new'`, unchanged behavior.
    - `/deleted` (`deleted.html`) - `status='deleted'` only (jobs the user actually saw and chose
      to delete - see the `'deleted'` vs `'rejected'` split above; auto-rejected jobs never appear
      here). Two actions: "Restore" (`POST /restore/<id>` -> `new`, for an accidental delete) and
      "Apply" (reuses the existing `POST /apply/<id>` as-is - it already sets `status='applied'`
      unconditionally regardless of prior status, so applying straight from a deleted job needed no
      new route).
    - `/applied` (`applied.html`) - `status='applied'` only. Header shows both the count of
      currently-open applications and `db.get_applied_count()` ("3 open · 4 applied all-time") -
      the second number survives the 90-day cleanup, the first doesn't. Two actions: "Move to
      Process" (`POST /interview/<id>` -> `process`, doesn't touch the counter) and "Undo"
      (`POST /undo_apply/<id>` -> `new`, decrements the counter - for an accidental Apply click).
    - `/processes` (`processes.html`) - `status='process'` only, i.e. active or finished interview
      processes (see the `'interviewing'`/`'closed'` merge above). No status-changing action at
      all - instead, an inline textarea + "Save notes" button (`POST /notes/<id>`) for recording
      how it went or why it failed, per explicit user request. Split out from `/applied` per
      explicit user request to keep "jobs I applied to" and "jobs I'm actively in process with" as
      separate tabs rather than one combined list with conditional buttons.
    - Every form that a route shared across pages (`/apply`, `/delete`, `/interview`) can be reached
      from carries a hidden `next` field, so `app._redirect_back()` returns the user to whichever
      page they actually clicked from instead of always landing on `/`.
  - endpoints: `/delete/<id>`, `/restore/<id>`, `/interview/<id>` - thin wrappers around
    `db.update_status`; `/apply/<id>` and `/undo_apply/<id>` wrap `db.mark_applied`/
    `db.undo_applied` instead (status change + counter adjustment together, see Step 3); `/notes/<id>`
    wraps `db.update_notes` (no status change).
  - the dashboard only shows rows with `status='new'`
  - Shows title, company, location, a site badge (LinkedIn/Indeed), the expandable job
    description, and Applied/Delete actions. Deliberately no decision/years badges - a job
    appearing on the dashboard already means Gemini approved it, so per user direction ("I'd
    ideally only want to press on the links and that's it") those badges were never adding
    information worth the visual clutter. `min_years`/`reason` are still in the DB row if ever
    needed later, just not surfaced in this template.
  - Each job shows "Posted N days ago" (from `date_posted`) when the source gave a real date, or
    "Added N ago" (from `first_seen`) when it didn't - `app._format_posted` picks whichever is
    available, so the display always has something rather than a blank when a source lacks the
    data. Computed in the `/` route (`dict(row)` + a `posted_display` key) rather than in the
    template, since Jinja doesn't parse dates.
- A "Last scraped: X ago" line and a "Scrape Now" button (`POST /scrape`) let the user trigger a
  scrape on demand instead of waiting for the next scheduled one - see Scheduling below for how
  this interacts with the automatic schedule. Runs in a background thread (`threading.Thread`) so
  the request returns immediately rather than blocking on the several minutes a full scrape takes;
  a module-level `_scraping_in_progress` flag stops a double-click (or an overlapping scheduled
  run) from starting two concurrent scrapes against the same DB, and the page shows a disabled
  "Scraping…" button plus auto-refreshes every 10s while one is running (plain `<meta refresh>`,
  no JS needed). `app.run(..., use_reloader=False)`: the debug reloader spawns a second process,
  which would double-fire that background thread.
- Telegram was considered (inline Applied/Delete buttons pushed to chat) and explicitly dropped:
  since the computer needs to be on regardless for any of this to run, a pull-based dashboard the
  user opens when they want is simpler than maintaining a push-notification bot for no real gain.

## Test tooling (`qa/`)
A permanent, checked-in QA tool for manually auditing the filter pipeline - separate from and
never imported by the production path (`pipeline.py`/`db.py`/`app.py`), but built entirely from
the same shared modules (`scraper.py`, `filters/*`) via normal imports, so a fix to the title
filter or the Gemini prompt applies to both automatically with nothing to keep in sync by hand.
(`qa/collect_ground_truth.py`, `qa/prompt_variants.py`, `qa/prompt_eval.py`, and
`qa/purge_filtered_rows.py` are the prompt-eval project's tooling - see "Prompt-engineering
evaluation" above.)

- `qa/build_review.py` - the one entry point:
  - `python qa/build_review.py` - reuses the frozen dataset in `qa/data/frozen_test_scrape.csv`
    and (re-)classifies it. Only touches jobs that aren't already fully classified - an `"error"`
    status (e.g. a quota wall) is retried, everything else is left alone - so re-running after
    fixing a quota issue or a prompt bug doesn't burn calls re-doing settled work.
  - `python qa/build_review.py --rescrape` - takes a fresh scrape first (overwrites the frozen
    CSV and invalidates any partial classification), for when you deliberately want current data
    rather than testing a code change against a fixed dataset.
  - Classifies with `verbose=True` (wants the `reason` text) and writes progress to
    `qa/data/stage2.json` after every single job, not just at the end - a crash or quota wall
    partway through must never lose completed work (this was a real incident earlier: an early
    version only saved at the very end, and the very last job hitting a `continue` path skipped
    the save entirely, silently leaving a `null` in the output that crashed the review page's JS).
  - Ends by merging classifications into `qa/data/frozen_test_scrape.json` and writing the final
    standalone page to `qa/data/review.html`, ready to hand to the Artifact tool to publish -
    publishing itself is a manual/Claude-side step, the script has no way to call it.
- `qa/review_template.html` - the review page itself: three independent lenses (All / Regex on
  title / Gemini), each with its own stats row and status-filter dropdown, so each stage's effect
  can be judged in isolation rather than only as a combined pipeline result. Shows the job's
  `search_terms` provenance (which `config.SEARCH_TERMS` found it) as chips - production doesn't
  carry this field into the DB at all, it's purely a test-time debugging aid.
- `qa/data/` - the frozen dataset and build artifacts, checked into the repo (exempted from the
  blanket `*.csv` gitignore rule) so "once in a while" means deliberately building on or replacing
  a known dataset, not starting from nothing each session.

## Scheduling
Everything runs locally on the user's machine - no external server or always-on service needed.

**One self-throttling entry point, not a separate scheduler process.** `pipeline.run(force=False)`
checks `scrape_state.hours_since_last_scrape()` (a UTC timestamp persisted in `data/scrape_state.json`)
before doing any work: if less than `config.SCRAPE_INTERVAL_HOURS` (6) has passed, it prints one
line and returns immediately. This makes it cheap enough to invoke very frequently regardless of
whether that invocation actually ends up scraping.

- **Windows Task Scheduler**: one task, `JobBot-Pipeline`, triggered every 15 minutes all day,
  running `venv\Scripts\pythonw.exe pipeline.py --log-to-file` directly (no `.bat`/`cmd.exe`
  wrapper) with the project root as working directory. Almost every firing is a fast no-op; real
  scraping happens roughly every 6 hours - and immediately after a gap, via two settings on that
  task (Settings tab):
  - **`pythonw.exe`, not `python.exe`, and no `.bat` in between**: a console-subsystem process
    (`python.exe`, or `cmd.exe` when Task Scheduler runs a `.bat` file) always briefly flashes a
    window when launched, even with the task's own "Hidden" setting off - that "Hidden" flag only
    controls Task Scheduler UI visibility, not the launched process's own window. `pythonw.exe` is
    a GUI-subsystem build of the same interpreter with no console at all, baked into the exe
    itself, so nothing ever flashes regardless of how it's launched. Since `pythonw` has no real
    stdout to print to, `pipeline.py --log-to-file` (only used by this task, not manual runs)
    redirects `sys.stdout`/`sys.stderr` to `logs/pipeline.log` (gitignored) itself before running, so
    scheduled-run output isn't silently lost.
  - **"Run task as soon as possible after a scheduled start is missed": ON.** The standard Windows
    mechanism for "computer was asleep/off when a trigger was due" - the moment the machine is
    next on, Windows fires the missed run itself. Covers both sleep and full shutdown with one
    setting; no custom wake/unlock trigger logic needed.
  - **"Wake the computer to run this task": OFF** (explicit user choice - screen/sound behavior on
    forced wake varies by machine and wasn't worth the uncertainty). A run due while asleep is
    simply skipped, then caught up by the setting above once the computer is naturally on again.
- **Dashboard "Scrape Now" button** (`POST /scrape` in `app.py`) calls `pipeline.run(force=True)`,
  bypassing the throttle - and since it updates the same `data/scrape_state.json` the scheduled path
  reads, this is exactly "resets the timer" for the next automatic check, with no separate logic
  needed to keep the two in sync.
- **A manual click and a scheduled run are two independent OS processes with no lock between
  them** - Task Scheduler's "ignore new instance" setting and the dashboard's in-memory
  `_scraping_in_progress` flag each only guard against double-firing *within* their own path, not
  across the two. `db.get_connection()` uses `timeout=30` (vs. Python's 5s default) so a genuine
  write collision waits instead of erroring, and `db.insert_job` catches `sqlite3.IntegrityError`
  as a normal "already inserted by the other process" rather than crashing - confirmed with an
  actual two-thread test that races both through the exists-check before either writes (the real
  TOCTOU window): one insert succeeds, the other returns `False` cleanly. Worst case on a genuine
  overlap is a few duplicate Gemini calls on whatever job both happened to pick up, not a crash.
- **Covering a long gap**: `hours_since_last_scrape()` is passed into `scraper.scrape_all` as
  `hours_old_override`, so a run after a 2-day gap asks for a ~48h window instead of the normal
  ~6h one, catching what was posted during the gap. Known limit: `RESULTS_WANTED_PER_TERM` (200) is
  a ceiling, not unbounded - an extremely long gap could in principle have more than 200 new
  postings for one search term, in which case only the most recent 200 are captured. Not solved
  (would need real backfill/pagination beyond the ceiling); acceptable given how rare a multi-day
  gap should be in practice.
- **The dashboard used to not survive a reboot at all.** Unlike the pipeline, which has always
  been a real Task Scheduler task, `app.py` was only ever started by hand (a tool call, or a
  double-click on `start_dashboard.bat`) - nothing registered it to come back after a restart, so
  a reboot (or just closing whatever window/session had launched it) always silently killed it.
  Fixed with a second Task Scheduler task, mirroring the pipeline one:
  - **`JobBot-Dashboard`**: trigger "At log on" for this Windows user (not "At startup"/SYSTEM -
    the app reads/writes files under the user's own profile, so it needs the user's own logon
    session, not a SYSTEM one), action `venv\Scripts\pythonw.exe app.py --log-to-file` directly (no
    `.bat`/`cmd.exe`, same reasoning as `JobBot-Pipeline` - avoids a console flash at every login).
    `app.py --log-to-file` (mirrors `pipeline.py`'s flag) redirects `sys.stdout`/`sys.stderr` to
    `logs/app.log` (gitignored) since `pythonw` has no real stdout to print to.
- **`start_dashboard.bat`** at the project root - the manual path: checks whether something is
  already listening on port 5000 (`Get-NetTCPConnection -LocalPort 5000 -State Listen`) before
  starting a new `venv\Scripts\python.exe app.py` (visible console - the user explicitly wants to
  be able to see this one, unlike the scheduled tasks), then always opens the dashboard in the
  default browser. The port check matters because `JobBot-Dashboard` (above) is very likely
  already running by the time anyone clicks this - without it, a click would try to bind port 5000
  a second time, fail immediately, and print a spurious "address already in use" error. A desktop
  shortcut ("JobBot Dashboard.lnk", created via `WScript.Shell`'s `CreateShortcut`, target =
  `start_dashboard.bat`) gives one-click access to open the dashboard regardless of whether the
  auto-start task already has it running - the "even a desktop button" part of the request.
- **`run_pipeline_task.bat`** - no longer used by Task Scheduler (see above - the scheduled task
  now calls `pythonw.exe` directly to avoid the console flash a `.bat` file causes). Kept as a
  convenience for manually triggering a real, visible-console pipeline run outside the dashboard.

## Roadmap (step order)
1. Scrape LinkedIn + Indeed with JobSpy -> DataFrame/CSV
2. Add years-of-experience filtering (title/language/Gemini)
3. Build an HTML view for comfortable browsing + delete/applied buttons
4. Run in the background without manual terminal commands (self-throttling pipeline + Task
   Scheduler + a dashboard "Scrape Now" button - see Scheduling)
5. Monitoring and improvement - tune the title filter/prompt based on real-world mistakes; the
   permanent `qa/` tool exists for exactly this

## Rejected: Secret Hunter (secrethunter.io)
Investigated as an extra source; not pursued. Its `/jobs/search` endpoint has no public API - the
frontend calls it via POST with an `x-recaptcha-verified-token` header, and the server 403s
("Verification required") on requests missing a valid one. That token can only come from a real
browser completing Google's reCAPTCHA flow, so the only way in is full browser automation
(Playwright), not a lightweight `requests` scraper like `scraper.py`. Decided against it: heavier
to build and maintain (real Chromium dependency, fragile to their frontend/anti-bot changes) than
it's worth given the site doesn't carry that many additional listings anyway.
