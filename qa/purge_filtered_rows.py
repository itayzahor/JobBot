"""One-off cleanup: delete existing new/deleted/rejected rows in jobs.db that the new title
("field role") and location (Ashdod) filters would now reject - those rows are location/role-type
noise, not signal about years-of-experience, and would otherwise contaminate the ground-truth set
built by qa/collect_ground_truth.py (see the plan for the prompt-eval project).

Deliberately leaves 'applied'/'process' rows untouched even if they match - those are real
decisions, not noise. Reuses title_filter/location_filter directly (not a separately-maintained
pattern list) so it purges exactly what the current filters would catch, staying in sync
automatically with any future filter tweaks.

Run once, after the filter changes land and before ground-truth collection starts in earnest.

Usage:
    python qa/purge_filtered_rows.py
"""

import os
import sys

# Some scraped titles contain characters (e.g. emoji) outside the Windows console's default
# codepage (cp1255/cp1252/etc.) - printing them raw crashes with UnicodeEncodeError before the
# transaction below ever commits. Reconfigure stdout to UTF-8 with a safe fallback so a decorative
# character in a title never aborts a real DB cleanup.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from jobbot import db
from jobbot.filters.location_filter import get_exclusion_reason as get_location_exclusion_reason
from jobbot.filters.title_filter import get_exclusion_reason as get_title_exclusion_reason

PURGE_STATUSES = ("new", "deleted", "rejected")


def main() -> None:
    conn = db.get_connection()
    rows = conn.execute(
        f"SELECT id, title, location, status FROM jobs WHERE status IN ({', '.join('?' for _ in PURGE_STATUSES)})",
        PURGE_STATUSES,
    ).fetchall()

    to_delete = []
    for job_id, title, location, status in rows:
        reason = get_title_exclusion_reason(title) or get_location_exclusion_reason(location)
        if reason is not None:
            to_delete.append((job_id, title, status, reason))

    for job_id, title, status, reason in to_delete:
        print(f"Deleting [{status}] {title!r} - {reason}")
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()

    print(f"\nDeleted {len(to_delete)} of {len(rows)} checked rows (status in {PURGE_STATUSES}).")


if __name__ == "__main__":
    main()
