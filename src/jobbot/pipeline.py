"""Runs the full pipeline: scrape -> filter by years of experience -> store new jobs.

Self-throttles to roughly every config.SCRAPE_INTERVAL_HOURS when not forced, so it's cheap to
invoke frequently (e.g. every 15 min via Task Scheduler) without actually scraping every time.

The CLI entry point (--log-to-file, --force) lives in the thin pipeline.py shim at the project
root, not here - see that file for why it stays at the root.
"""

from datetime import datetime, timezone

import pandas as pd

from jobbot import config, db, scrape_state
from jobbot.filters import filter_job
from jobbot.scraper import dedup_key, is_same_posting, scrape_all


def _text(value) -> str:
    """A scraped text field as a plain string. Missing values come back from pandas as NaN (a
    float), not None - and NaN is truthy, so `row.get(...) or ""` doesn't catch it. A job with no
    company name crashed every run on `.lower()` for 18 days before this existed."""
    return value if isinstance(value, str) else ""


def run(force: bool = False, hours_old: int | None = None) -> None:
    if not force:
        elapsed = scrape_state.hours_since_last_scrape()
        if elapsed is not None and elapsed < config.SCRAPE_INTERVAL_HOURS:
            print(f"Only {elapsed:.1f}h since last scrape (need {config.SCRAPE_INTERVAL_HOURS}h) - skipping")
            return

    # hours_old is an explicit override for a one-off wider scrape (e.g. manually pulling in more
    # ground-truth examples to label) - takes precedence over the normal auto-computed window. None
    # (the normal case) falls back to elapsed time since the last scrape, which itself falls back to
    # config.HOURS_OLD inside scrape_all if no prior scrape was recorded. Widening to actual elapsed
    # time (rather than always using the fixed default) means a gap - computer asleep/off - doesn't
    # silently miss everything posted during it.
    # The gap-widening is capped at config.MAX_JOB_AGE_DAYS, though: after a long absence, postings
    # older than that aren't wanted anyway, and an uncapped window just means a slower scrape
    # full of stale jobs.
    if hours_old is not None:
        hours_old_override = hours_old
    else:
        elapsed = scrape_state.hours_since_last_scrape()
        hours_old_override = None if elapsed is None else min(elapsed, config.MAX_JOB_AGE_DAYS * 24)
    df = scrape_all(hours_old_override=hours_old_override)
    conn = db.get_connection()

    # scraper._dedup_cross_site only fuzzy-matches descriptions within this run's own DataFrame,
    # so it can't catch a duplicate whose sibling site was scraped in an earlier run (e.g. a job
    # LinkedIn indexed a day before Indeed did). Build a lookup of recent DB rows by (company,
    # title) once up front so each new row can be fuzzy-checked against them too, below.
    recent_candidates: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for candidate in db.get_recent_for_dedup(conn, hours=24):
        key = ((candidate["company"] or "").lower(), dedup_key(candidate["title"] or ""))
        recent_candidates.setdefault(key, []).append((candidate["id"], candidate["description"] or ""))

    counts = {"inserted": 0, "rejected": 0, "dupe": 0, "error": 0}
    for _, row in df.iterrows():
        # One malformed row must never sink the whole run: a crash here skips the
        # set_last_scrape_time() below, so every later run silently retries (and re-crashes on)
        # an ever-widening window - exactly what happened with the missing-company bug.
        try:
            counts[_process_row(conn, row, recent_candidates)] += 1
        except Exception as e:
            counts["error"] += 1
            print(f"  skipped {row.get('job_url')}: {type(e).__name__}: {e}")

    expired = db.expire_old_new_jobs(conn, config.MAX_JOB_AGE_DAYS)
    cleaned_up = db.cleanup_old_rows(conn)
    conn.close()
    scrape_state.set_last_scrape_time(datetime.now(timezone.utc))
    print(
        f"Pipeline done: {counts['inserted']} new jobs inserted, "
        f"{counts['rejected']} skipped (too much experience required), "
        f"{counts['dupe']} skipped (already in DB), "
        f"{counts['error']} skipped (error), "
        f"{expired} new jobs expired (older than {config.MAX_JOB_AGE_DAYS}d), "
        f"{cleaned_up} stale rows cleaned up (new/deleted/rejected @7d, applied @90d)"
    )


def _process_row(conn, row, recent_candidates: dict[tuple[str, str], list[tuple[str, str]]]) -> str:
    """Dedup, classify and store one scraped row. Returns which counter it lands in:
    "inserted", "rejected" or "dupe"."""
    url = row.get("job_url")

    # Check dedup BEFORE classifying, not just before inserting - otherwise a job already
    # in the DB gets re-run through Gemini every time it reappears in a scrape (Indeed's
    # window overlaps runs a lot more than LinkedIn's, so this matters more than it used to).
    if db.job_exists(conn, db.hash_url(url)):
        return "dupe"

    title = _text(row.get("title"))
    description = _text(row.get("description"))
    location = _text(row.get("location"))
    company = _text(row.get("company"))

    # Cross-run cross-site dedup: same posting, different site, scraped in a different run
    # (and so under a different job_url than the check above catches).
    candidate_key = (company.lower(), dedup_key(title))
    if any(
        is_same_posting(description, candidate_description)
        for _, candidate_description in recent_candidates.get(candidate_key, [])
    ):
        return "dupe"
    # verbose=False (default): skips Gemini's reason text, the one part of the call with
    # real added latency/output-token cost - reason is qa/ tooling only, not shown in
    # production, so `reason` is intentionally left out of the job dict below (stored NULL).
    min_years, decision, _reason = filter_job(title, description, location=location)

    # date_posted: the real posting date from LinkedIn/Indeed when JobSpy could extract one
    # (a plain datetime.date - stored as its ISO string, "YYYY-MM-DD"). Missing shows up as
    # NaN (pandas), not None, so pd.isna() is required to detect it. The dashboard falls back
    # to first_seen (when we found it) when this is absent.
    date_posted = row.get("date_posted")
    date_posted = None if pd.isna(date_posted) else str(date_posted)

    job = {
        "title": title,
        "company": company or None,
        "location": location or None,
        "url": url,
        "description": description,
        "min_years": min_years,
        "decision": decision,
        "site": row.get("site"),
        "date_posted": date_posted,
    }
    # Insert regardless of decision so dedup covers rejected jobs too - otherwise the same
    # still-posted "no" job gets re-classified (and re-billed against Gemini) every run.
    # 'rejected' (auto-screened) is distinct from 'deleted' (user clicked Delete on the
    # dashboard) so /deleted only ever shows jobs the user actually chose to reject.
    status = "rejected" if decision == "no" else "new"
    if not db.insert_job(conn, job, status=status):
        return "dupe"
    return "rejected" if decision == "no" else "inserted"
