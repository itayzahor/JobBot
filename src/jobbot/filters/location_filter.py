"""Pass 0: reject jobs in cities outside easy commuting range that still slip through JobSpy's/
LinkedIn's radius parameter - LinkedIn only supports fixed radius steps (5/10/25/50/75/100 mi),
and the next step down from config.DISTANCE_MILES (25) would also cut out wanted cities
(Ra'anana/Holon), so excluding one specific city needs a text filter instead of a smaller radius.
"""

import re

EXCLUDED_LOCATION_PATTERNS = [
    ("ashdod", re.compile(r"\bashdod\b", re.IGNORECASE)),
    # Indeed returns place names in the site's local script regardless of locale headers (same
    # reason the cross-site dedup logic in scraper.py has to account for Hebrew location strings).
    ("ashdod (he)", re.compile(r"אשדוד")),
]


def get_exclusion_reason(location: str) -> str | None:
    """Return why a location should be excluded, or None if it's fine."""
    if not location:
        return None

    for reason, pattern in EXCLUDED_LOCATION_PATTERNS:
        if pattern.search(location):
            return reason

    return None
