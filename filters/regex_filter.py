"""Pass 1: regex-based years-of-experience extraction (Hebrew + English phrasings)."""

import re

# Each pattern must capture the number(s) of years. For ranges, group 1 is the lower bound.
PATTERNS = [
    # English: "3+ years", "3 + years of experience"
    re.compile(r"(\d+)\s*\+\s*years?", re.IGNORECASE),
    # English: "3-5 years", "3 to 5 years"
    re.compile(r"(\d+)\s*(?:-|to)\s*\d+\s*years?", re.IGNORECASE),
    # English: "at least 3 years", "minimum of 3 years", "minimum 3 years"
    re.compile(r"(?:at least|minimum(?: of)?)\s*(\d+)\s*years?", re.IGNORECASE),
    # English: "3 years of experience", "3 years' experience"
    re.compile(r"(\d+)\s*years?[\s']*(?:of\s*)?experience", re.IGNORECASE),
    # Hebrew: "נדרש ניסיון של 3 שנים", "ניסיון של 3+ שנים"
    re.compile(r"ניסיון\s*(?:של)?\s*(\d+)\s*\+?\s*שנ(?:ה|ים|ות)"),
    # Hebrew: "3 שנות ניסיון"
    re.compile(r"(\d+)\s*שנ(?:ה|ים|ות)\s*(?:של\s*)?ניסיון"),
    # Hebrew: "לפחות 3 שנים"
    re.compile(r"לפחות\s*(\d+)\s*שנ(?:ה|ים|ות)"),
]

NO_EXPERIENCE_PATTERNS = [
    re.compile(r"\bno experience\b", re.IGNORECASE),
    re.compile(r"\bentry[\s-]level\b", re.IGNORECASE),
    re.compile(r"ללא\s*ניסיון"),
]


def extract_min_years(text: str) -> int | None:
    """Return the minimum required years of experience found via regex, or None if not found."""
    if not text:
        return None

    for pattern in NO_EXPERIENCE_PATTERNS:
        if pattern.search(text):
            return 0

    found = []
    for pattern in PATTERNS:
        for match in pattern.finditer(text):
            found.append(int(match.group(1)))

    if not found:
        return None
    return min(found)
