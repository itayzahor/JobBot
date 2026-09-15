"""Tracks when the pipeline last actually scraped, so it can self-throttle to roughly every
config.SCRAPE_INTERVAL_HOURS and widen its search window to cover any gap after a long pause
(computer asleep/off) instead of missing everything posted during it.
"""

import json
from datetime import datetime, timezone

from jobbot.paths import SCRAPE_STATE_PATH


def get_last_scrape_time() -> datetime | None:
    if not SCRAPE_STATE_PATH.exists():
        return None
    with open(SCRAPE_STATE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return datetime.fromisoformat(data["last_scrape"])


def set_last_scrape_time(dt: datetime) -> None:
    with open(SCRAPE_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"last_scrape": dt.isoformat()}, f)


def hours_since_last_scrape() -> float | None:
    """None means no recorded scrape yet (fresh checkout) - callers should treat that as
    "always due" rather than crashing or assuming zero elapsed time."""
    last = get_last_scrape_time()
    if last is None:
        return None
    return (datetime.now(timezone.utc) - last).total_seconds() / 3600
