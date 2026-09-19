"""Extract human-labeled ground truth for the prompt-eval project from real dashboard usage.

status='applied'/'process' -> label True (a real "I'd apply to this"). status='deleted' ->
label False (a real "I saw this and didn't want it"). 'new' (undecided) and 'rejected' (never
seen by a human - screened out by title/language/Gemini before reaching the dashboard) are
excluded: neither is a human judgment.

Only meaningful once qa/purge_filtered_rows.py has removed rows the location/title filters would
now reject (see the plan) - otherwise 'deleted' includes location/role-type noise unrelated to
years-of-experience.

Safe to re-run anytime as more labels accumulate via daily dashboard use. Merges into the existing
snapshot rather than overwriting it, so labels survive the DB rows being auto-cleaned.

Usage:
    python qa/collect_ground_truth.py
"""

import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from jobbot import db

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ground_truth.json")

LABEL_BY_STATUS = {
    "applied": True,
    "process": True,
    "deleted": False,
}


def main() -> None:
    conn = db.get_connection()
    rows = db.get_jobs(conn, list(LABEL_BY_STATUS))
    live_ids = {row[0] for row in conn.execute("SELECT id FROM jobs")}
    conn.close()

    examples = [
        {
            "id": row["id"],
            "title": row["title"],
            "description": row["description"],
            "label": LABEL_BY_STATUS[row["status"]],
            "status": row["status"],
        }
        for row in rows
    ]

    # Merge, don't overwrite: once COLLECTING_GROUND_TRUTH is off, db.cleanup_old_rows() resumes
    # deleting 'deleted' rows after 7 days, so the DB stops being a complete record of past labels.
    # Keep any earlier snapshot entry whose row has since been auto-cleaned from the DB entirely. An
    # entry whose row still exists but changed status (e.g. Restore -> 'new') is no longer a label
    # and is dropped.
    kept_from_snapshot = 0
    if os.path.exists(OUTPUT_PATH):
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            previous = json.load(f)
        current_ids = {e["id"] for e in examples}
        for entry in previous:
            if entry["id"] not in current_ids and entry["id"] not in live_ids:
                examples.append(entry)
                kept_from_snapshot += 1

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(examples, f, ensure_ascii=False, indent=2)

    counts = Counter(e["label"] for e in examples)
    print(f"Wrote {len(examples)} labeled jobs to {OUTPUT_PATH}")
    print(f"  positive (should show): {counts[True]}")
    print(f"  negative (shouldn't):   {counts[False]}")
    print(f"  ({kept_from_snapshot} kept from earlier snapshots after their DB rows were auto-cleaned)")


if __name__ == "__main__":
    main()
