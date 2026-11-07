"""Build the Gmail agent's eval dataset: every email from the last N days that survives the
pre-filter, with its body, saved to qa/data/gmail_emails.json.

Resumable like qa/build_review.py - emails already saved are not re-downloaded. The output is
personal mail and is gitignored; only aggregate scores are ever committed.

    venv\\Scripts\\python.exe qa\\gmail_dataset.py --days 60
"""

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from jobbot.gmail_client import get_message, get_service, list_message_ids  # noqa: E402
from jobbot.gmail_prefilter import skip_reason  # noqa: E402

OUT_PATH = os.path.join(ROOT, "qa", "data", "gmail_emails.json")
_MAX_BODY_CHARS = 8000  # long HTML newsletters - the classifier never needs more than this


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    emails = {}
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH, encoding="utf-8") as f:
            emails = {e["id"]: e for e in json.load(f)}

    service = get_service()
    ids = list_message_ids(service, datetime.now(timezone.utc) - timedelta(days=args.days))
    skipped = Counter()

    for i, message_id in enumerate(ids, 1):
        if message_id in emails:
            continue
        msg = get_message(service, message_id)
        reason = skip_reason(msg)
        if reason:
            skipped[reason] += 1
            continue
        # Only now pay for the full body
        msg = get_message(service, message_id, with_body=True)
        msg["body"] = msg["body"][:_MAX_BODY_CHARS]
        msg["received"] = msg["received"].isoformat()
        emails[message_id] = msg
        # Save every 20 so a network drop never loses much
        if i % 20 == 0:
            _save(emails)
            print(f"{i}/{len(ids)}")

    _save(emails)
    print(f"\n{len(ids)} emails, {len(emails)} kept, skipped: {dict(skipped)}")


def _save(emails: dict) -> None:
    ordered = sorted(emails.values(), key=lambda e: e["received"], reverse=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
