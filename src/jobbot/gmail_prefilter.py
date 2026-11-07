"""Cheap pre-filter for the Gmail agent: drop mail that is never about an application before any
Gemini call - the same role the title filter plays for scraped jobs.

Rules come from the user's real mailbox (60 days, 629 emails, 2026-11-01): Promotions and Social
held zero application mail, so whole categories are safe to skip. Sender/subject rules are kept
narrow on purpose - lean permissive, a missed rejection is worse than one extra Gemini call.
"""

import re

_SKIP_CATEGORIES = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL"}

# Senders that only ever send job alerts / newsletters, never anything about an application
_SKIP_SENDERS = re.compile(
    r"jobalert\.indeed\.com|match\.indeed\.com|secrethunter\.|@log-on\.com",
    re.IGNORECASE,
)

# Candidate-account housekeeping from hiring platforms: login codes, verification, password resets
_SKIP_SUBJECTS = re.compile(
    r"one-time password|security code|verification code|verify your (candidate )?account"
    r"|confirm your identity|reset your password|קוד אימות|אמת את חשבון",
    re.IGNORECASE,
)


def skip_reason(msg: dict) -> str | None:
    """Why this email can be skipped without classifying it, or None if it should be classified.

    Input: a message dict from gmail_client.get_message (needs labels, sender, subject).
    Output: a short reason string, or None.
    """
    labels = set(msg["labels"])
    if "SENT" in labels:
        return "sent by me"
    if labels & _SKIP_CATEGORIES:
        return "promotions/social"
    if _SKIP_SENDERS.search(msg["sender"]):
        return "job-alert sender"
    if _SKIP_SUBJECTS.search(msg["subject"]):
        return "account housekeeping"
    return None
