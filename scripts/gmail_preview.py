"""Gmail agent step 1: log in and list what the fetch step sees - no AI, nothing stored.

First run opens a browser to log in (creates data/gmail_token.json). Prints one line per email plus
counts per Gmail category, to decide what the cheap pre-filter should skip.

    venv\\Scripts\\python.exe scripts\\gmail_preview.py --days 60
"""

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from jobbot.gmail_client import get_message, get_service, list_message_ids  # noqa: E402

# Gmail's own tabs - the main input to the pre-filter decision
_CATEGORIES = ["CATEGORY_PERSONAL", "CATEGORY_UPDATES", "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # Hebrew subjects on a Windows console

    service = get_service()
    after = datetime.now(timezone.utc) - timedelta(days=args.days)
    ids = list_message_ids(service, after)
    print(f"{len(ids)} emails in the last {args.days} days\n")

    categories = Counter()
    for message_id in ids:
        msg = get_message(service, message_id)
        category = next((c for c in _CATEGORIES if c in msg["labels"]), "NONE").removeprefix("CATEGORY_")
        categories[category] += 1
        print(f"{msg['received']:%Y-%m-%d} | {category:<10} | {msg['sender'][:40]:<40} | {msg['subject'][:90]}")

    print("\nBy Gmail category:")
    for category, count in categories.most_common():
        print(f"  {category:<12} {count}")


if __name__ == "__main__":
    main()
