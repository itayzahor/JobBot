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
    hours_old_override = hours_old if hours_old is not None else scrape_state.hours_since_last_scrape()
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

    inserted = 0
    skipped_no = 0
    skipped_dupe = 0

    for _, row in df.iterrows():
        url = row.get("job_url")

        # Check dedup BEFORE classifying, not just before inserting - otherwise a job already
        # in the DB gets re-run through Gemini every time it reappears in a scrape (Indeed's
        # window overlaps runs a lot more than LinkedIn's, so this matters more than it used to).
        if db.job_exists(conn, db.hash_url(url)):
            skipped_dupe += 1
            continue

        title = row.get("title") or ""
        description = row.get("description") or ""
        location = row.get("location") or ""

        # Cross-run cross-site dedup: same posting, different site, scraped in a different run
        # (and so under a different job_url than the check above catches).
        candidate_key = ((row.get("company") or "").lower(), dedup_key(title))
        if any(
            is_same_posting(description, candidate_description)
            for _, candidate_description in recent_candidates.get(candidate_key, [])
        ):
            skipped_dupe += 1
            continue
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
            "company": row.get("company"),
            "location": row.get("location"),
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
        if db.insert_job(conn, job, status=status):
            if decision == "no":
                skipped_no += 1
            else:
                inserted += 1
        else:
            skipped_dupe += 1

    cleaned_up = db.cleanup_old_rows(conn)
    conn.close()
    scrape_state.set_last_scrape_time(datetime.now(timezone.utc))
    print(
        f"Pipeline done: {inserted} new jobs inserted, "
        f"{skipped_no} skipped (too much experience required), "
        f"{skipped_dupe} skipped (already in DB), "
        f"{cleaned_up} stale rows cleaned up (new/deleted/rejected @7d, applied @90d)"
    )
