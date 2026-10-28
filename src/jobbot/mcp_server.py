"""MCP server exposing the jobs DB to an LLM client (Claude Desktop, Claude Code, ...).

The client talks to this process over stdin/stdout and decides on its own which tools to call -
"I have an interview Wednesday with Wix" becomes search_jobs -> move_to_process. Every tool is a
thin wrapper around db.py, so the dashboard and this server always apply the same status rules
(notably: the persistent applied counter is incremented exactly once per job, whichever path
marks it).

Tool docstrings are what the model reads to decide when and how to call each tool - they're
prompt text, not just developer docs.

The CLI entry point is the mcp_server.py shim at the project root (same pattern as app.py).
Nothing here may print to stdout: stdout is the protocol channel.
"""

import sqlite3
from datetime import date

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from jobbot import db, scrape_state
from jobbot.paths import PROFILE_PATH

mcp = MCPServer(
    "jobbot",
    instructions=(
        "Tracks the user's job search: jobs scraped from LinkedIn/Indeed and their status "
        "(new -> applied -> process). Find a job with search_jobs before acting on it, and if "
        "more than one job plausibly matches, ask the user which one instead of guessing. "
        "Whenever a question depends on the user's own background (fit for a job, interview "
        "prep, which projects to mention), call get_profile first instead of guessing."
    ),
)

_STATUSES = ("new", "applied", "process", "deleted", "rejected")

# Jobs the user has engaged with sort first in search results - when someone says "the Wix job",
# they almost always mean one they applied to, not one of a dozen unseen 'new'/'rejected' rows.
_STATUS_RANK = "CASE status WHEN 'process' THEN 0 WHEN 'applied' THEN 1 WHEN 'new' THEN 2 ELSE 3 END"


def _connect() -> sqlite3.Connection:
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    return conn


def _require_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise ToolError(f"No job with id {job_id!r} - use search_jobs to find the right id.")
    return row


def _append_note(conn: sqlite3.Connection, row: sqlite3.Row, note: str) -> None:
    """Append a dated line rather than overwrite - notes are the user's interview history."""
    line = f"[{date.today().isoformat()}] {note.strip()}"
    existing = (row["notes"] or "").rstrip()
    db.update_notes(conn, row["id"], f"{existing}\n{line}" if existing else line)


def _read_profile() -> str:
    if not PROFILE_PATH.exists():
        return "(No profile yet - the user's background is unknown; ask them.)"
    return PROFILE_PATH.read_text(encoding="utf-8")


def _ensure_applied(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """Count the job as applied if it isn't already (applied outside the dashboard). Returns
    True if it was newly counted. 'process' rows already passed through 'applied', so they're
    never counted twice."""
    if row["status"] in ("applied", "process"):
        return False
    db.mark_applied(conn, row["id"])
    return True


# --- Tools ---


@mcp.tool()
def search_jobs(
    company: str = "",
    title: str = "",
    keyword: str = "",
    status: str = "",
    limit: int = 20,
) -> list[dict]:
    """Search the job tracker. All filters are optional, case-insensitive substring matches,
    combined with AND.

    - company / title: match the company name or job title.
    - keyword: match anywhere in the job description (e.g. "RAG", "Kubernetes").
    - status: one of new (on the dashboard, not acted on), applied, process (active or finished
      interview process), deleted (user dismissed it), rejected (auto-filtered, never shown).

    Returns short summaries (no description) - call get_job for the full posting and notes.
    Results are ordered process, applied, new, then the rest; most recent first within each.
    """
    if status and status not in _STATUSES:
        raise ToolError(f"status must be one of {', '.join(_STATUSES)}")
    clauses, params = [], []
    for column, value in (("company", company), ("title", title), ("description", keyword)):
        if value:
            clauses.append(f"{column} LIKE ?")
            params.append(f"%{value}%")
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = _connect()
    try:
        rows = conn.execute(
            f"""
            SELECT id, title, company, location, site, status, date_posted,
                   COALESCE(status_changed_at, first_seen) AS last_changed,
                   notes IS NOT NULL AND notes != '' AS has_notes
            FROM jobs {where}
            ORDER BY {_STATUS_RANK}, last_changed DESC
            LIMIT ?
            """,
            (*params, max(1, min(limit, 100))),
        ).fetchall()
    finally:
        conn.close()
    return [{**dict(row), "has_notes": bool(row["has_notes"])} for row in rows]


@mcp.tool()
def get_job(job_id: str) -> dict:
    """Full details of one job: the complete description, URL, status and the user's notes."""
    conn = _connect()
    try:
        row = _require_job(conn, job_id)
    finally:
        conn.close()
    keys = ("id", "title", "company", "location", "site", "url", "status", "date_posted",
            "first_seen", "status_changed_at", "notes", "description")
    return {key: row[key] for key in keys}


@mcp.tool()
def mark_applied(job_id: str) -> str:
    """Record that the user applied to this job (e.g. they applied directly on LinkedIn or the
    company site). Safe to call on a job that's already applied/in process - it won't be
    double-counted."""
    conn = _connect()
    try:
        row = _require_job(conn, job_id)
        if not _ensure_applied(conn, row):
            return f"{row['title']} @ {row['company']} was already '{row['status']}' - nothing changed."
        return f"Marked {row['title']} @ {row['company']} as applied."
    finally:
        conn.close()


@mcp.tool()
def move_to_process(job_id: str, note: str = "") -> str:
    """Move a job to 'process' - use when the user has an interview, a home assignment, or any
    next step beyond applying. If the job wasn't marked applied yet, it's counted as applied
    first. An optional note (e.g. "Technical interview Wed 10/15") is appended to its notes."""
    conn = _connect()
    try:
        row = _require_job(conn, job_id)
        counted = _ensure_applied(conn, row)
        if row["status"] != "process":
            db.update_status(conn, job_id, "process")
        if note.strip():
            _append_note(conn, row, note)
    finally:
        conn.close()
    parts = [f"{row['title']} @ {row['company']} is now in process"]
    if counted:
        parts.append("(also counted as applied - it wasn't marked before)")
    if note.strip():
        parts.append("and the note was saved")
    return " ".join(parts) + "."


@mcp.tool()
def add_note(job_id: str, note: str) -> str:
    """Append a dated note to a job - what happened in an interview, why a process failed,
    who the contact is. Notes are the user's own record: save what they said (tidied up is fine),
    not generated advice or prep material."""
    if not note.strip():
        raise ToolError("note is empty")
    conn = _connect()
    try:
        row = _require_job(conn, job_id)
        _append_note(conn, row, note)
    finally:
        conn.close()
    return f"Note saved on {row['title']} @ {row['company']}."


@mcp.tool()
def get_stats() -> dict:
    """Overview of the job search: application counts, jobs per status, and when the scraper
    last ran (if that's more than a day ago, the scheduled scraping is probably broken)."""
    conn = _connect()
    try:
        by_status = dict(conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
        applied_all_time = db.get_applied_count(conn)
    finally:
        conn.close()
    last_scrape = scrape_state.get_last_scrape_time()
    hours_ago = scrape_state.hours_since_last_scrape()
    return {
        "applied_all_time": applied_all_time,
        "open_applications": by_status.get("applied", 0),
        "in_process": by_status.get("process", 0),
        "new_on_dashboard": by_status.get("new", 0),
        "jobs_per_status": by_status,
        "last_scraped_utc": last_scrape.isoformat(timespec="minutes") if last_scrape else None,
        "hours_since_last_scrape": round(hours_ago, 1) if hours_ago is not None else None,
    }


@mcp.tool()
def get_profile() -> str:
    """The user's candidate profile: education, GPA, skills with honest proficiency levels, all
    projects in detail, home assignments, courses, target roles, and interview stories. Use it to
    judge fit for a job, prepare interviews, or pick which projects to highlight. It is the source
    of truth: never attribute a skill or experience to the user that isn't in it. Lines marked
    TODO are unknown - ask rather than assume."""
    return _read_profile()


# --- Resources ---

# The same profile as a resource, for clients that let the user attach context explicitly (the
# "+" menu in Claude Desktop). Resources are user-attached, not model-fetched, which is why
# get_profile also exists as a tool.
@mcp.resource("profile://me", name="profile", title="My candidate profile", mime_type="text/markdown")
def profile_resource() -> str:
    return _read_profile()


# --- Prompts ---


@mcp.prompt()
def interview_prep(company: str, stage: str = "the next interview") -> str:
    """Prepare for an interview: pulls the job description, your notes on this process, and
    notes from past processes into a prep session."""
    conn = _connect()
    try:
        matches = conn.execute(
            f"""
            SELECT * FROM jobs WHERE company LIKE ? AND status IN ('applied', 'process')
            ORDER BY {_STATUS_RANK}, COALESCE(status_changed_at, first_seen) DESC
            """,
            (f"%{company}%",),
        ).fetchall()
        past = conn.execute(
            """
            SELECT title, company, notes FROM jobs
            WHERE status = 'process' AND notes IS NOT NULL AND notes != '' AND company NOT LIKE ?
            """,
            (f"%{company}%",),
        ).fetchall()
    finally:
        conn.close()

    if len(matches) != 1:
        # Zero or several candidates: hand the lookup to the model, which can ask the user and
        # then fetch the right job with the tools.
        found = "\n".join(f"- {m['title']} @ {m['company']} ({m['status']}, id {m['id']})" for m in matches)
        return (
            f"I want to prepare for {stage} at {company}. "
            + (f"These applied/in-process jobs match:\n{found}\nAsk me which one I mean, "
               if matches else "No applied/in-process job matched that name - use search_jobs to find it, ")
            + "then fetch it with get_job and help me prepare as described below.\n\n"
            + _PREP_INSTRUCTIONS
        )

    job = matches[0]
    past_notes = "\n\n".join(f"{p['title']} @ {p['company']}:\n{p['notes']}" for p in past) or "(none yet)"
    return (
        f"I want to prepare for {stage} for this job.\n\n"
        f"## {job['title']} @ {job['company']} ({job['location']})\n"
        f"Status: {job['status']}\n\n"
        f"### My notes on this process so far\n{job['notes'] or '(none)'}\n\n"
        f"### Job description\n{job['description'] or '(no description stored)'}\n\n"
        f"### My notes from other interview processes\n{past_notes}\n\n"
        f"### My profile\n{_read_profile()}\n\n"
        + _PREP_INSTRUCTIONS
    )


_PREP_INSTRUCTIONS = """\
Help me prepare:
1. The topics and skills this role will most likely probe, based on the description - ranked.
2. Likely questions for this stage, technical and behavioral.
3. Which of my projects and experience fit which requirements - only from my profile (call
   get_profile if it isn't included above); name the gaps honestly and how to address them.
4. If my notes from past processes show a recurring weak spot, call it out and plan for it.
5. Which of my interview stories from the profile fit this role's behavioral questions.
Keep the prep here in the chat - don't save it into the job's notes unless I ask."""
