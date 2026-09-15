"""Build the QA review page: freeze a scrape (or reuse the existing one), classify every job
through title filter -> language filter -> Gemini (verbose), and write a standalone HTML page
ready to publish.

Usage:
    python qa/build_review.py              # reuse qa/data/frozen_test_scrape.csv, reclassify
    python qa/build_review.py --rescrape   # take a fresh scrape first, then classify

Never touches jobs.db or anything under pipeline.py/app.py - this is a read-only, offline view
built from a separate frozen dataset in qa/data/, so it can't slow down or interfere with the
production path no matter how large the dataset or how often it's rerun.
"""

import argparse
import json
import os
import sys
from collections import Counter

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from jobbot.filters.gemini_filter import classify
from jobbot.filters.language_filter import is_allowed_language
from jobbot.filters.title_filter import get_exclusion_reason
from jobbot.scraper import scrape_all

QA_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(QA_DIR, "data")
CSV_PATH = os.path.join(DATA_DIR, "frozen_test_scrape.csv")
STAGE2_PATH = os.path.join(DATA_DIR, "stage2.json")
MERGED_JSON_PATH = os.path.join(DATA_DIR, "frozen_test_scrape.json")
TEMPLATE_PATH = os.path.join(QA_DIR, "review_template.html")
OUTPUT_HTML_PATH = os.path.join(DATA_DIR, "review.html")

DISPLAY_COLUMNS = ["title", "company", "location", "description", "job_url", "site", "date_posted", "search_terms"]


def rescrape() -> None:
    print("Taking a fresh scrape...")
    df = scrape_all()
    os.makedirs(DATA_DIR, exist_ok=True)
    df.to_csv(CSV_PATH, index=False)
    print(f"Saved {len(df)} jobs to {CSV_PATH}")


def classify_all() -> None:
    """Run every job through title -> language -> Gemini(verbose), saving after each one so a
    quota wall mid-run never loses completed work (learned the hard way earlier this project)."""
    df = pd.read_csv(CSV_PATH).fillna("")
    stage2: list[dict | None] = [None] * len(df)

    # Resume from a previous partial run if one exists and matches this dataset's length. An
    # "error" status (e.g. a quota wall) is retried, not treated as final - everything else isn't.
    if os.path.exists(STAGE2_PATH):
        with open(STAGE2_PATH, encoding="utf-8") as f:
            existing = json.load(f)
        if len(existing) == len(df):
            stage2 = existing
            done = sum(1 for s in stage2 if s is not None and s["status"] != "error")
            print(f"Resuming: {done}/{len(df)} already classified")

    for i, row in df.iterrows():
        if stage2[i] is not None and stage2[i]["status"] != "error":
            continue

        title = row["title"]
        description = row["description"]

        title_reason = get_exclusion_reason(title)
        if title_reason is not None:
            stage2[i] = {"status": "excluded_title", "reason": f"Title filtered: {title_reason}", "min_years": None}
        elif not is_allowed_language(description):
            stage2[i] = {"status": "excluded_language", "reason": "Description is not in Hebrew or English", "min_years": None}
        else:
            try:
                result = classify(title, description, verbose=True)
                status = "gemini_yes" if result.fit else "gemini_no"
                stage2[i] = {"status": status, "reason": result.reason, "min_years": result.years_estimate}
                print(f"{i + 1}/{len(df)} [{status}] {title}")
            except Exception as e:
                stage2[i] = {"status": "error", "reason": f"Classification failed: {e}", "min_years": None}
                print(f"{i + 1}/{len(df)} [ERROR] {title}: {e}")

        # Unconditional - every branch above must reach this, or a crash/interrupt on the last
        # iteration silently leaves that entry as None in the saved file.
        with open(STAGE2_PATH, "w", encoding="utf-8") as f:
            json.dump(stage2, f, ensure_ascii=False)

    print(Counter(s["status"] for s in stage2))


def merge_and_build_html() -> None:
    df = pd.read_csv(CSV_PATH).fillna("")
    records = df[DISPLAY_COLUMNS].to_dict("records")

    with open(STAGE2_PATH, encoding="utf-8") as f:
        stage2 = json.load(f)
    assert len(records) == len(stage2), f"{len(records)} rows vs {len(stage2)} classifications - rerun classify"
    assert all(s is not None for s in stage2), "some rows still unclassified - rerun classify"

    for record, s2 in zip(records, stage2):
        record["stage2"] = s2

    with open(MERGED_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False)

    with open(MERGED_JSON_PATH, encoding="utf-8") as f:
        data = f.read()
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read()
    final = template.replace("__JOBS_DATA__", data)
    with open(OUTPUT_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(final)

    print(f"Wrote {OUTPUT_HTML_PATH} ({len(final)} bytes) - publish this via the Artifact tool")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rescrape", action="store_true", help="Take a fresh scrape before classifying")
    args = parser.parse_args()

    if args.rescrape or not os.path.exists(CSV_PATH):
        rescrape()
        if os.path.exists(STAGE2_PATH):
            os.remove(STAGE2_PATH)  # a fresh scrape invalidates any partial classification

    classify_all()
    merge_and_build_html()
