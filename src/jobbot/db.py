"""Step 3: SQLite storage with dedup by normalized job_url hash."""

import hashlib
import sqlite3

from jobbot import config
from jobbot.paths import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    title TEXT,
    company TEXT,
    location TEXT,
    url TEXT,
    description TEXT,
    min_years INTEGER,
    decision TEXT,
    reason TEXT,
    site TEXT,
    date_posted TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    status_changed_at TEXT,
    notes TEXT
);
"""
# status_changed_at is nullable (not NOT NULL DEFAULT ...): SQLite disallows a non-constant
# default (a function call like datetime('now')) on a column added via ALTER TABLE to an existing
# table, which the migration below needs to do. NULL means "never explicitly changed" - every
# query treats that as equivalent to first_seen via COALESCE, which is also the semantically
# correct fallback (if status was never touched, its own creation time is when it "last changed").

# One consolidated lifecycle: 'new' -> 'deleted' (user rejects it) or 'applied' (user applies) or
# it's screened out before ever reaching the user ('rejected'); 'applied' -> 'process' once an
# interview process actually starts. There's no separate terminal "closed" status anymore - a
# process that fails is recorded via the `notes` field on the same 'process' row (see below)
# rather than a status transition, per explicit user request to track "how did it go" as text, not
# as another state.
_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Auto-cleanup, permanently DELETE-ing the row, per status - a status absent from this dict (just
# 'process') is never auto-cleaned, kept indefinitely so its notes ("why did it fail and how did
# it go") stay available. 'applied' gets a much longer window than new/deleted/rejected because
# losing an in-flight application from view after only a week would be actively annoying, but the
# historical *count* of applications (see applied_count below) is separate from the row and
# survives this cleanup regardless of the window chosen here.
_CLEANUP_RULES = {
    "new": 7,
    "deleted": 7,
    "rejected": 7,
    "applied": 90,
}


def hash_url(url: str) -> str:
    normalized = url.strip().lower().rstrip("/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def get_connection() -> sqlite3.Connection:
    # timeout=30: how long a writer waits for another writer's lock before giving up. Matters now
    # that a scheduled run (Task Scheduler) and a manual "Scrape Now" click are two independent
    # processes with no lock between them - the 5s default could raise "database is locked" on a
    # real collision; 30s comfortably outlasts our fast, single-row transactions.
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute(SCHEMA)
    conn.execute(_META_SCHEMA)
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    for column in ("reason", "site", "date_posted", "status_changed_at", "notes"):
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
    # One-off, idempotent migration: the old 'interviewing'/'closed' statuses collapsed into a
    # single 'process' status. Runs on every connection but is a no-op once nothing matches.
    conn.execute("UPDATE jobs SET status = 'process' WHERE status IN ('interviewing', 'closed')")
    conn.commit()
    return conn


def job_exists(conn: sqlite3.Connection, job_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return row is not None


def insert_job(conn: sqlite3.Connection, job: dict, status: str = "new") -> bool:
    """Insert a job if its URL hash isn't already present. Returns True if inserted.

    Every processed job should be inserted (even auto-rejected ones, with status='rejected') so
    dedup catches it on future scrapes instead of re-running regex/Gemini on it every run.
    """
    job_id = hash_url(job["url"])
    if job_exists(conn, job_id):
        return False
    try:
        conn.execute(
            """
            INSERT INTO jobs (id, title, company, location, url, description, min_years, decision, reason, site, date_posted, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                job.get("title"),
                job.get("company"),
                job.get("location"),
                job["url"],
                job.get("description"),
                job.get("min_years"),
                job.get("decision"),
                job.get("reason"),
                job.get("site"),
                job.get("date_posted"),
                status,
            ),
        )
    except sqlite3.IntegrityError:
        # A concurrent process (e.g. a manual "Scrape Now" overlapping the scheduled run) won the
        # race and inserted this exact job between our job_exists() check and this INSERT - that's
        # a normal "already there", not a real error.
        return False
    conn.commit()
    return True


def get_recent_for_dedup(conn: sqlite3.Connection, hours: int = 24) -> list[sqlite3.Row]:
    """Rows first seen within the last `hours` hours - candidates for pipeline.py's cross-run
    fuzzy dedup, which catches a duplicate whose sibling site was scraped in an earlier run (see
    scraper.is_same_posting). Bounded to a short lookback since the sibling copy you'd actually
    hit is almost certainly recent - not worth fuzzy-comparing against a whole week of history.
    """
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT id, company, title, description FROM jobs WHERE first_seen >= datetime('now', ?)",
        (f"-{hours} hours",),
    ).fetchall()


def update_status(conn: sqlite3.Connection, job_id: str, status: str) -> None:
    conn.execute(
        "UPDATE jobs SET status = ?, status_changed_at = datetime('now') WHERE id = ?",
        (status, job_id),
    )
    conn.commit()


def update_notes(conn: sqlite3.Connection, job_id: str, notes: str) -> None:
    """Free-text notes, meaningful only for 'process' rows - what happened in the interview
    process, why it failed, etc. Doesn't touch status_changed_at (editing a note isn't a status
    change and shouldn't bump the job's ordering)."""
    conn.execute("UPDATE jobs SET notes = ? WHERE id = ?", (notes, job_id))
    conn.commit()


def _adjust_applied_count(conn: sqlite3.Connection, delta: int) -> None:
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('applied_count', '0')")
    conn.execute(
        "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + ? AS TEXT) WHERE key = 'applied_count'",
        (delta,),
    )


def get_applied_count(conn: sqlite3.Connection) -> int:
    """Total jobs ever applied to - a persistent counter, independent of the `jobs` table, so it
    keeps counting correctly even after the 90-day 'applied' cleanup deletes the row, and doesn't
    need to be re-derived by also counting 'process' rows (which all passed through 'applied'
    first, but no longer carry that status once they move on)."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'applied_count'").fetchone()
    return int(row[0]) if row else 0


def mark_applied(conn: sqlite3.Connection, job_id: str) -> None:
    """Move a job to 'applied' and increment the persistent applied counter together, so the two
    can never drift apart."""
    update_status(conn, job_id, "applied")
    _adjust_applied_count(conn, 1)
    conn.commit()


def undo_applied(conn: sqlite3.Connection, job_id: str) -> None:
    """Undo an accidental Apply click: back to 'new', and decrement the counter to match."""
    update_status(conn, job_id, "new")
    _adjust_applied_count(conn, -1)
    conn.commit()


def get_jobs(conn: sqlite3.Connection, statuses: list[str]) -> list[sqlite3.Row]:
    """Jobs in any of the given statuses, most recently status-changed first (falls back to
    first_seen for a row whose status was never explicitly changed - see the schema note above).
    """
    conn.row_factory = sqlite3.Row
    placeholders = ", ".join("?" for _ in statuses)
    return conn.execute(
        f"""
        SELECT * FROM jobs
        WHERE status IN ({placeholders})
        ORDER BY COALESCE(status_changed_at, first_seen) DESC
        """,
        statuses,
    ).fetchall()


def get_new_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return get_jobs(conn, ["new"])


def cleanup_old_rows(conn: sqlite3.Connection) -> int:
    """Permanently delete stale rows per `_CLEANUP_RULES` (each status has its own retention
    window; 'process' isn't in the dict at all and is never auto-deleted, so its notes stay
    available indefinitely). Returns the total number of rows deleted.

    Trade-off accepted by the user: deleting a 'new'/'deleted'/'rejected' row also erases its
    dedup memory, so if that exact posting is still live and gets scraped again later, it could
    reappear as "new". Considered rare/minor enough to accept rather than keeping a permanent
    "expired" record. 'applied' rows are exempt from this concern in the sense that matters most:
    even though the row itself is deleted after 90 days, `applied_count` already recorded it
    permanently, so the historical count is never affected by this cleanup.
    """
    total_deleted = 0
    for status, days in _CLEANUP_RULES.items():
        # While gathering ground truth for prompt-engineering experiments (see
        # qa/prompt_eval.py), skip cleanup for statuses that hold the labels being collected -
        # applied's rule is left alone since it's irrelevant to this and far off (90d) anyway.
        if config.COLLECTING_GROUND_TRUTH and status in ("new", "deleted", "rejected"):
            continue
        cursor = conn.execute(
            """
            DELETE FROM jobs
            WHERE status = ?
            AND COALESCE(status_changed_at, first_seen) < datetime('now', ?)
            """,
            (status, f"-{days} days"),
        )
        total_deleted += cursor.rowcount
    conn.commit()
    return total_deleted
