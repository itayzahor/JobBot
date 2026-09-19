"""One-off: run the production filter (Pass 0 + the promoted Gemini prompt) over every job currently
sitting in status='new', and move the ones it rejects to status='rejected'.

Needed once, when config.COLLECTING_GROUND_TRUTH was turned off: jobs scraped while it was on reached
the dashboard without ever being Gemini-screened, so they'd otherwise stay visible forever.

Non-destructive: a rejected job keeps its row (status='rejected', with Gemini's one-line reason stored
in the `reason` column - unlike the normal pipeline, which stores none - so each hidden job can be
spot-checked). Only rows still 'new' at update time are touched, so a job you Apply/Delete while this
runs is left alone. Undo for one job: UPDATE jobs SET status='new' WHERE id=...

Usage:
    python qa/rescreen_new_jobs.py
"""

import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from jobbot import config, db
from jobbot.filters import filter_job


def main() -> None:
    if config.COLLECTING_GROUND_TRUTH:
        print("config.COLLECTING_GROUND_TRUTH is still True - filter_job would skip Gemini. Aborting.")
        return

    conn = db.get_connection()
    rows = conn.execute("SELECT id, title, location, description FROM jobs WHERE status = 'new'").fetchall()
    print(f"Re-screening {len(rows)} 'new' jobs...")

    kept = rejected = 0
    for i, (job_id, title, location, description) in enumerate(rows, 1):
        min_years, decision, reason = filter_job(title or "", description or "", location=location or "", verbose=True)
        if decision == "no":
            cursor = conn.execute(
                "UPDATE jobs SET status = 'rejected', decision = 'no', min_years = ?, reason = ?, "
                "status_changed_at = datetime('now') WHERE id = ? AND status = 'new'",
                (min_years, reason, job_id),
            )
            rejected += cursor.rowcount
            print(f"[{i}/{len(rows)}] REJECTED  {title!r} (years={min_years}) - {reason}")
        else:
            conn.execute(
                "UPDATE jobs SET decision = 'yes', min_years = ? WHERE id = ? AND status = 'new'",
                (min_years, job_id),
            )
            kept += 1
            print(f"[{i}/{len(rows)}] kept      {title!r} (years={min_years})")
        conn.commit()

    conn.close()
    print(f"\nDone: {kept} kept as 'new', {rejected} moved to 'rejected'.")


if __name__ == "__main__":
    main()
