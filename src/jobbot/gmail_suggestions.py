"""Turn stored application emails into Inbox cards, and apply a card when the user accepts it.

Pure rules, no AI: the agent (gmail_agent.py) only stores what each email is and which job it's
about. Everything here is computed at display time from the job's *current* status, so a card never
proposes something stale (e.g. you already moved the job by hand).

One card per job (user decision 2026-11-06): all of a job's pending emails are grouped, and the
card proposes a single change based on the latest email that implies a status. Rules, per the
action table in CLAUDE.md ("Planned next: Gmail agent"):
- rejection -> close the application; if it's already in an interview process, just a note
- interview invite / home assignment -> move to process
- confirmation -> mark applied (only if the job isn't already applied or further)
- other_update -> a note only
Ambiguous emails (several of the user's applications fit) act on the oldest application - the one
applied to first - and the note records that it was a guess (decision 4 in CLAUDE.md).
"""

import json
import re
import sqlite3
from dataclasses import dataclass, field

from jobbot import db

# Types that say where an application stands; other_update / recruiter_outreach don't
_STATUS_TYPES = {"confirmation", "rejection", "interview_invite", "home_assignment"}
_NOTE_LABELS = {
    "rejection": "Rejected",
    "interview_invite": "Interview invite",
    "home_assignment": "Home assignment",
    "other_update": "Update",
}
_LATER_THAN_APPLIED = {"applied", "process", "closed"}


@dataclass
class Card:
    key: str  # stable id for the Accept/Dismiss forms
    emails: list[dict]  # oldest first
    job: dict | None = None  # None for an application not in the DB, or outreach
    action: str = "none"  # mark_applied | to_process | close | add_note | add_job | none
    new_status: str = ""  # for add_job: the status the created job starts in
    notes: list[str] = field(default_factory=list)
    guessed_between: list[dict] = field(default_factory=list)  # ambiguous: every candidate

    @property
    def latest(self) -> dict:
        return self.emails[-1]

    @property
    def visible(self) -> bool:
        """Worth showing: it changes something, or it's outreach the user may want to read."""
        return self.action != "none" or bool(self.notes) or self.latest["type"] == "recruiter_outreach"


def build_cards(conn) -> list[Card]:
    """Every pending Inbox email grouped into cards, newest activity first.

    Input: a DB connection.
    Output: Cards - including invisible no-op ones (confirmations of jobs already applied), which
    the agent auto-resolves so they don't sit in the table forever.
    """
    conn.row_factory = sqlite3.Row
    jobs = _jobs_by_id(conn)
    groups: dict[str, Card] = {}
    for row in db.get_pending_suggestions(conn):
        email = {**dict(row), "job_ids": json.loads(row["job_ids"])}
        key, job, candidates = _card_key(email, jobs)
        if key is None:  # an application made outside JobBot
            key = _outside_card_key(email, groups)
        card = groups.setdefault(key, Card(key=key, emails=[], job=job))
        card.emails.append(email)
        if candidates:
            card.guessed_between = candidates
    cards = list(groups.values())
    for card in cards:
        _decide(card)
    return sorted(cards, key=lambda c: c.latest["received"], reverse=True)


def apply_card(conn, card: Card) -> None:
    """Make the card's change in the DB and mark its emails accepted. Uses the same db.py
    functions as the dashboard and MCP server, so the applied counter stays exact: a job is counted
    once, on the way into 'applied', whichever path gets it there."""
    conn.row_factory = sqlite3.Row
    job_id = card.job["id"] if card.job else None
    status = card.job["status"] if card.job else "new"

    if card.action == "add_job":
        # Emails about one outside application name it differently ("Junior Data Scientist" vs
        # "... | TLV", or not at all) - take the first company and the most specific role
        company = next((e["company"] for e in card.emails if e["company"]), "(unknown company)")
        role = max((e["role"] or "" for e in card.emails), key=len) or "(role not stated)"
        job_id = db.insert_job_from_email(conn, card.emails[0]["email_id"], company, role, _gmail_link(card.latest))
        target = card.new_status
    else:
        target = {"mark_applied": "applied", "to_process": "process", "close": "closed"}.get(card.action)

    if target:
        if status not in _LATER_THAN_APPLIED:
            db.mark_applied(conn, job_id)  # count it - applied outside the dashboard
        if target != "applied":
            db.update_status(conn, job_id, target)
    if card.notes and job_id:
        _append_notes(conn, job_id, card.notes)
    db.resolve_suggestions(conn, [e["email_id"] for e in card.emails], "accepted")


def _decide(card: Card) -> None:
    """Fill in the card's action, notes and (for add_job) starting status."""
    status_emails = [e for e in card.emails if e["type"] in _STATUS_TYPES]
    decisive = status_emails[-1]["type"] if status_emails else None
    card.notes = [
        f"[{e['received'][:10]}] {_NOTE_LABELS[e['type']]}: {e['evidence']}".strip()
        for e in card.emails if e["type"] in _NOTE_LABELS
    ]
    if card.latest["type"] == "recruiter_outreach":
        card.action, card.notes = "none", []
        return
    if card.job is None:  # applied outside JobBot
        card.action = "add_job"
        # An interview or home task means it became a process - a rejection after that ends the
        # process (kept as a note), it doesn't close an untouched application
        if any(e["type"] in ("interview_invite", "home_assignment") for e in card.emails):
            card.new_status = "process"
        else:
            card.new_status = "closed" if decisive == "rejection" else "applied"
        return

    status = card.job["status"]
    if decisive == "rejection":
        card.action = "add_note" if status == "process" else ("none" if status == "closed" else "close")
    elif decisive in ("interview_invite", "home_assignment"):
        card.action = "add_note" if status == "process" else "to_process"
    elif decisive == "confirmation":
        card.action = "none" if status in _LATER_THAN_APPLIED else "mark_applied"
    # Record a guess only when it changes something - a guessed confirmation of a job that's
    # already applied would otherwise surface as a card whose only content is the guess
    if card.guessed_between and card.action in ("close", "to_process", "mark_applied"):
        names = ", ".join(j["title"] for j in card.guessed_between)
        card.notes.append(f"[{card.latest['received'][:10]}] Matched by guess (oldest application) - the email could be about any of: {names}")
    if card.action == "none" and card.notes:
        card.action = "add_note"


def _card_key(email: dict, jobs: dict) -> tuple[str, dict | None, list[dict]]:
    """Which card an email belongs to: (key, job or None, ambiguous candidates or [])."""
    if email["type"] == "recruiter_outreach":
        return f"outreach:{email['email_id']}", None, []
    candidates = [jobs[i] for i in email["job_ids"] if i in jobs]
    if email["match"] == "job" and candidates:
        # Duplicate scrapes of one job: act on the copy the user actually engaged with
        job = max(candidates, key=lambda j: j["status"] in _LATER_THAN_APPLIED)
        return f"job:{job['id']}", job, []
    if email["match"] == "ambiguous" and candidates:
        if len({(_norm(j["company"]), _norm(j["title"])) for j in candidates}) == 1:
            # "Ambiguous" between duplicate scrapes of one posting isn't a real guess
            job = max(candidates, key=lambda j: j["status"] in _LATER_THAN_APPLIED)
            return f"job:{job['id']}", job, []
        job = _oldest_open_application(candidates)
        return f"job:{job['id']}", job, candidates
    # not_in_db, or every matched id has since been cleaned up
    return None, None, []


def _outside_card_key(email: dict, groups: dict) -> str:
    """Card for an application made outside JobBot. Emails join an existing card when they're in
    the same Gmail thread, or from the same company with a compatible role - one names it, the
    other doesn't, or one is a longer form of the other ("Junior Data Scientist | TLV"). Without
    this, one outside application split into several cards and accepting each made duplicates."""
    company, role = _norm(email["company"]), _norm(email["role"])
    for key, card in groups.items():
        if not key.startswith("new:"):
            continue
        if any(e["thread_id"] == email["thread_id"] for e in card.emails):
            return key
        card_company = next((_norm(e["company"]) for e in card.emails if e["company"]), "")
        card_role = max((_norm(e["role"]) for e in card.emails), key=len)
        if company and company == card_company and (not role or not card_role or role in card_role or card_role in role):
            return key
    return f"new:{email['email_id']}"


def _oldest_open_application(candidates: list[dict]) -> dict:
    """The application made first, skipping ones already closed - a second rejection from the
    same company is about the next application, not the one already closed."""
    open_ones = [j for j in candidates if j["status"] != "closed"] or candidates
    return min(open_ones, key=lambda j: j["last_changed"] or "")


def _jobs_by_id(conn) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT id, title, company, status, url, COALESCE(status_changed_at, first_seen) AS last_changed FROM jobs"
    ).fetchall()
    return {r["id"]: dict(r) for r in rows}


def _append_notes(conn, job_id: str, lines: list[str]) -> None:
    row = conn.execute("SELECT notes FROM jobs WHERE id = ?", (job_id,)).fetchone()
    existing = (row["notes"] or "").rstrip()
    new = [line for line in lines if line not in existing]  # accepting twice never duplicates
    if new:
        db.update_notes(conn, job_id, "\n".join(filter(None, [existing, *new])))


def _gmail_link(email: dict) -> str:
    return f"https://mail.google.com/mail/u/0/#all/{email['thread_id']}"


def _norm(s: str | None) -> str:
    return re.sub(r"\W+", " ", (s or "").lower()).strip()
