"""Check the Gmail Inbox rules (jobbot.gmail_suggestions) against a throwaway DB - no Gmail, no
Gemini. These rules write to the user's real job data on Accept, so every rule in the action table
gets a case here.

    venv\\Scripts\\python.exe qa\\test_gmail_suggestions.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_DB = Path(tempfile.mkdtemp()) / "jobs.db"
os.environ["JOBBOT_DB_PATH"] = str(TEST_DB)  # before importing jobbot - never touch the real DB
sys.path.insert(0, str(ROOT / "src"))

from jobbot import db, gmail_suggestions  # noqa: E402

failures = 0


def check(ok: bool, label: str) -> None:
    global failures
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    failures += not ok


def add_job(conn, company, title, status, changed):
    job_id = db.hash_url(f"https://example.com/{company}/{title}")
    conn.execute(
        "INSERT INTO jobs (id, title, company, url, status, status_changed_at) VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, title, company, f"https://example.com/{company}/{title}", status, changed),
    )
    conn.commit()
    return job_id


def add_email(conn, email_id, received, type_, match="job", job_ids=(), company="", role="", evidence="...", thread=None):
    db.insert_email_suggestion(conn, {
        "email_id": email_id, "thread_id": thread or f"t-{email_id}", "received": received, "sender": "x",
        "subject": f"subject {email_id}", "evidence": evidence, "type": type_, "company": company,
        "role": role, "job_reference": "", "match": match, "job_ids": json.dumps(list(job_ids)),
        "match_reason": "",
    })


def card_for(conn, key):
    return next(c for c in gmail_suggestions.build_cards(conn) if c.key == key)


def status_of(conn, job_id):
    return conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]


def main() -> None:
    conn = db.get_connection()

    print("Statuses:")
    closed = add_job(conn, "Old", "Closed job", "closed", "2026-09-01")
    conn.close()
    conn = db.get_connection()  # the old migration used to rewrite 'closed' on every connection
    check(status_of(conn, closed) == "closed", "'closed' survives a new connection (old migration narrowed)")

    print("Rejections:")
    applied = add_job(conn, "Acme", "Backend", "applied", "2026-10-01")
    add_email(conn, "r1", "2026-10-02", "confirmation", job_ids=[applied])
    add_email(conn, "r2", "2026-10-20", "rejection", job_ids=[applied], evidence="not moving forward")
    card = card_for(conn, f"job:{applied}")
    check(card.action == "close" and len(card.emails) == 2, "applied + confirmation + rejection -> one card: close")
    count = db.get_applied_count(conn)
    gmail_suggestions.apply_card(conn, card)
    check(status_of(conn, applied) == "closed", "accept closes the job")
    check(db.get_applied_count(conn) == count, "closing an applied job doesn't touch the counter")
    check("Rejected: not moving forward" in conn.execute("SELECT notes FROM jobs WHERE id=?", (applied,)).fetchone()[0],
          "the rejection is recorded in notes")

    in_process = add_job(conn, "Globex", "Automation", "process", "2026-09-17")
    add_email(conn, "p1", "2026-10-05", "rejection", job_ids=[in_process])
    card = card_for(conn, f"job:{in_process}")
    check(card.action == "add_note", "rejection of a job in process -> note only")
    gmail_suggestions.apply_card(conn, card)
    check(status_of(conn, in_process) == "process", "...and it stays in process")

    print("Moving forward:")
    fresh = add_job(conn, "Initech", "Full-Stack", "new", "2026-10-29")
    add_email(conn, "i1", "2026-10-30", "interview_invite", job_ids=[fresh])
    card = card_for(conn, f"job:{fresh}")
    check(card.action == "to_process", "interview invite for a 'new' job -> move to process")
    count = db.get_applied_count(conn)
    gmail_suggestions.apply_card(conn, card)
    check(status_of(conn, fresh) == "process" and db.get_applied_count(conn) == count + 1,
          "a job applied outside the dashboard is counted exactly once on the way to process")

    print("No-ops:")
    noop = add_job(conn, "Umbrella", "Junior", "applied", "2026-10-29")
    add_email(conn, "n1", "2026-10-29", "confirmation", job_ids=[noop])
    check(not card_for(conn, f"job:{noop}").visible, "confirmation of an already-applied job is hidden")
    deleted = add_job(conn, "Hooli", "SA", "deleted", "2026-10-31")
    add_email(conn, "n2", "2026-09-19", "confirmation", job_ids=[deleted])
    check(card_for(conn, f"job:{deleted}").action == "mark_applied", "confirmation of a deleted job -> mark applied")

    print("Ambiguous (oldest application first):")
    stark_old = add_job(conn, "Stark", "Software Engineer", "applied", "2026-10-20")
    stark_new = add_job(conn, "Stark", "Prompt Engineer", "applied", "2026-10-25")
    add_email(conn, "a1", "2026-10-28", "rejection", match="ambiguous", job_ids=[stark_new, stark_old])
    card = card_for(conn, f"job:{stark_old}")
    check(card.action == "close" and card.guessed_between, "ambiguous rejection targets the oldest application")
    check(any("guess" in n for n in card.notes), "the note records that it was a guess")
    gmail_suggestions.apply_card(conn, card)
    add_email(conn, "a2", "2026-10-30", "rejection", match="ambiguous", job_ids=[stark_new, stark_old])
    check(card_for(conn, f"job:{stark_new}").action == "close", "a second ambiguous rejection targets the next open one")

    print("Not in the DB:")
    # One outside application, named three ways - must stay one card (it was split into 3 before)
    add_email(conn, "x1", "2026-09-17", "confirmation", match="not_in_db", company="Wayne", role="")
    add_email(conn, "x2", "2026-09-18", "home_assignment", match="not_in_db", company="Wayne", role="Junior Data Scientist")
    add_email(conn, "x3", "2026-09-28", "rejection", match="not_in_db", company="Wayne", role="Junior Data Scientist | Remote")
    outside = [c for c in gmail_suggestions.build_cards(conn) if any(e["email_id"] == "x1" for e in c.emails)][0]
    check(len(outside.emails) == 3, "role-less and longer-role emails from one company join one card")
    check(outside.action == "add_job" and outside.new_status == "process",
          "home task then rejection -> added as a process (rejection kept as a note), not closed")
    count = db.get_applied_count(conn)
    gmail_suggestions.apply_card(conn, outside)
    row = conn.execute("SELECT status, site, notes, title FROM jobs WHERE company = 'Wayne'").fetchone()
    check(row[0] == "process" and row[1] == "gmail" and db.get_applied_count(conn) == count + 1,
          "creates the job in process, site=gmail, counted once")
    check("Home assignment" in row[2] and "Rejected" in row[2], "its history is in the notes")
    check(row[3] == "Junior Data Scientist | Remote", "the most specific role becomes the title")
    check(not any(any(e["email_id"] == "x1" for e in c.emails) for c in gmail_suggestions.build_cards(conn)),
          "accepted emails leave the Inbox")
    # Same Gmail thread joins even when the classifier named the company differently
    add_email(conn, "j1", "2026-09-17", "confirmation", match="not_in_db", company="Globex", role="Junior Automation Engineer", thread="globex")
    add_email(conn, "j2", "2026-09-29", "interview_invite", match="not_in_db", company="גלובקס", role="", thread="globex")
    check(sum(any(e["email_id"] in ("j1", "j2") for e in c.emails) for c in gmail_suggestions.build_cards(conn)) == 1,
          "emails in one Gmail thread share a card")
    add_email(conn, "c1", "2026-09-10", "rejection", match="not_in_db", company="Cyberdyne", role="C Software Developer")
    add_email(conn, "c2", "2026-09-20", "rejection", match="not_in_db", company="Cyberdyne", role="Low Level Software developer")
    check(sum(any(e["email_id"] in ("c1", "c2") for e in c.emails) for c in gmail_suggestions.build_cards(conn)) == 2,
          "two different roles at one company stay two cards")

    print("Guesses that change nothing:")
    soy_a = add_job(conn, "Soylent", "Junior Software Engineer", "applied", "2026-09-20")
    soy_b = add_job(conn, "Soylent", "CV Integration Engineer", "applied", "2026-10-19")
    add_email(conn, "y1", "2026-10-20", "confirmation", match="ambiguous", job_ids=[soy_a, soy_b])
    check(not card_for(conn, f"job:{soy_a}").visible, "an ambiguous confirmation of applied jobs is hidden (no guess-only note)")
    dup_a = add_job(conn, "Vandelay", "Junior QA", "applied", "2026-10-26")
    dup_b = add_job(conn, "Vandelay ", "Junior QA ", "new", "2026-10-26")
    add_email(conn, "d1", "2026-10-27", "rejection", match="ambiguous", job_ids=[dup_a, dup_b])
    card = card_for(conn, f"job:{dup_a}")
    check(card.action == "close" and not card.guessed_between, "'ambiguous' between duplicate scrapes is a plain match, not a guess")

    print("Outreach:")
    add_email(conn, "o1", "2026-10-11", "recruiter_outreach", match="not_in_db", company="Pied Piper")
    card = card_for(conn, "outreach:o1")
    check(card.visible and card.action == "none", "outreach is shown, with nothing to accept")

    conn.close()
    print("\nALL PASSED" if not failures else f"\n{failures} FAILED")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
