"""Step 4: local Flask dashboard for browsing new jobs and marking them deleted/applied.

The CLI entry point (--log-to-file, app.run(...)) lives in the thin app.py shim at the project
root, not here - see that file for why it stays at the root.
"""

import threading
from datetime import date, datetime, timezone

from flask import Flask, redirect, render_template, request, url_for

from jobbot import db, pipeline, scrape_state

app = Flask(__name__)

# In-memory guard against a double-click (or an overlapping scheduled run) starting two scrapes
# at once - SQLite tolerates concurrent writers with brief lock waits, but there's no reason to
# let it happen when a simple flag avoids it. Fine as a plain module global: this app runs as a
# single process for one user, not multiple workers.
_scraping_in_progress = False


def _format_time_ago(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        minutes = int(seconds // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(seconds // 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"


def _format_posted(job: dict) -> str:
    """The real posting date when JobSpy could extract one (LinkedIn/Indeed both usually provide
    it now), falling back to when we first saw it - per user request: best data available, and a
    graceful fallback rather than leaving it blank when the source doesn't have it."""
    date_posted = job["date_posted"]
    if date_posted:
        try:
            posted = datetime.strptime(date_posted, "%Y-%m-%d").date()
            days = (date.today() - posted).days
            if days <= 0:
                return "Posted today"
            if days == 1:
                return "Posted yesterday"
            return f"Posted {days} days ago"
        except ValueError:
            pass
    first_seen = datetime.strptime(job["first_seen"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return f"Added {_format_time_ago(first_seen)}"


def _run_scrape_in_background() -> None:
    global _scraping_in_progress
    try:
        pipeline.run(force=True)
    finally:
        _scraping_in_progress = False


def _enrich(rows: list) -> list[dict]:
    return [{**dict(row), "posted_display": _format_posted(row)} for row in rows]


def _redirect_back(default: str = "dashboard"):
    """Routes shared across pages (e.g. /apply from both '/' and '/deleted') redirect back to
    wherever the action was submitted from, via a hidden 'next' field on each form, rather than
    always landing on the main dashboard."""
    return redirect(url_for(request.form.get("next", default)))


@app.route("/")
def dashboard():
    conn = db.get_connection()
    jobs = _enrich(db.get_new_jobs(conn))
    conn.close()
    last_scraped = _format_time_ago(scrape_state.get_last_scrape_time())
    return render_template("dashboard.html", jobs=jobs, last_scraped=last_scraped, scraping=_scraping_in_progress)


@app.route("/deleted")
def deleted():
    # 'deleted' = user clicked Delete on a job shown to them - distinct from 'rejected', the
    # filter pipeline's auto-screen-out status, which never appears on this page.
    conn = db.get_connection()
    jobs = _enrich(db.get_jobs(conn, ["deleted"]))
    conn.close()
    return render_template("deleted.html", jobs=jobs)


@app.route("/applied")
def applied():
    conn = db.get_connection()
    jobs = _enrich(db.get_jobs(conn, ["applied"]))
    applied_count = db.get_applied_count(conn)
    conn.close()
    return render_template("applied.html", jobs=jobs, applied_count=applied_count)


@app.route("/processes")
def processes():
    # 'process' is the one status for an active or finished interview process - there's no
    # separate terminal status; how it went is recorded via the notes field on the same row.
    conn = db.get_connection()
    jobs = _enrich(db.get_jobs(conn, ["process"]))
    conn.close()
    return render_template("processes.html", jobs=jobs)


@app.route("/scrape", methods=["POST"])
def scrape_now():
    global _scraping_in_progress
    if not _scraping_in_progress:
        _scraping_in_progress = True
        threading.Thread(target=_run_scrape_in_background, daemon=True).start()
    return redirect(url_for("dashboard"))


@app.route("/delete/<job_id>", methods=["POST"])
def delete(job_id):
    conn = db.get_connection()
    db.update_status(conn, job_id, "deleted")
    conn.close()
    return _redirect_back()


@app.route("/apply/<job_id>", methods=["POST"])
def apply(job_id):
    conn = db.get_connection()
    db.mark_applied(conn, job_id)
    conn.close()
    return _redirect_back()


@app.route("/undo_apply/<job_id>", methods=["POST"])
def undo_apply(job_id):
    """Undo an accidental Apply click - back to 'new', and the applied counter drops back down
    to match (see db.undo_applied)."""
    conn = db.get_connection()
    db.undo_applied(conn, job_id)
    conn.close()
    return _redirect_back(default="applied")


@app.route("/restore/<job_id>", methods=["POST"])
def restore(job_id):
    conn = db.get_connection()
    db.update_status(conn, job_id, "new")
    conn.close()
    return _redirect_back(default="deleted")


@app.route("/interview/<job_id>", methods=["POST"])
def interview(job_id):
    # applied -> process. Deliberately does NOT touch the applied counter - the job was already
    # counted when it first became 'applied', and starting an interview process doesn't create a
    # second application.
    conn = db.get_connection()
    db.update_status(conn, job_id, "process")
    conn.close()
    return _redirect_back(default="applied")


@app.route("/undo_process/<job_id>", methods=["POST"])
def undo_process(job_id):
    """Undo an accidental 'Move to Process' click - back to 'applied'. Same non-counting logic as
    /interview: this job was already counted on the way into 'applied', so reversing the move
    doesn't touch the counter either."""
    conn = db.get_connection()
    db.update_status(conn, job_id, "applied")
    conn.close()
    return _redirect_back(default="processes")


@app.route("/notes/<job_id>", methods=["POST"])
def notes(job_id):
    conn = db.get_connection()
    db.update_notes(conn, job_id, request.form.get("notes", ""))
    conn.close()
    return _redirect_back(default="processes")


def main() -> None:
    # use_reloader=False: the debug reloader spawns a second process, which would double-fire
    # any background thread started from a request (like /scrape) - not just wasteful, it would
    # race two concurrent pipeline runs against the same DB.
    app.run(debug=True, use_reloader=False)
