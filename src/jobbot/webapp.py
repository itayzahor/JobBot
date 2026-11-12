"""Step 4: local Flask dashboard for browsing new jobs and marking them deleted/applied.

The CLI entry point (--log-to-file, app.run(...)) lives in the thin app.py shim at the project
root, not here - see that file for why it stays at the root.
"""

import re
import threading
from datetime import date, datetime, timezone

from flask import Flask, redirect, render_template, request, url_for
from markupsafe import Markup, escape

from jobbot import config, db, gmail_suggestions, pipeline, scrape_state
from jobbot.resume import web as resume_web

app = Flask(__name__)
app.register_blueprint(resume_web.bp)

# Job descriptions come back from JobSpy as literal Markdown (LinkedIn/Indeed both write postings
# in it), but templates used to dump them as plain escaped text - so "**Requirements**" rendered
# with literal asterisks around it instead of as a bolded heading. This isn't a full Markdown
# renderer: escape first (the text is scraped from external sites, so it's untrusted), then
# re-introduce just enough HTML for bold text, headers, and bullets to read like the source intended.
_DESC_HEADER_RE = re.compile(r"^#{1,6}\s*(.+)$", re.MULTILINE)
_DESC_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_DESC_BULLET_RE = re.compile(r"^[*\-•]\s+", re.MULTILINE)


@app.template_filter("format_description")
def format_description(text: str | None) -> Markup:
    formatted = str(escape(text or ""))
    formatted = _DESC_HEADER_RE.sub(r'<span class="desc-heading">\1</span>', formatted)
    formatted = _DESC_BOLD_RE.sub(r"<strong>\1</strong>", formatted)
    formatted = _DESC_BULLET_RE.sub("• ", formatted)
    return Markup(formatted)

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
    return render_template(
        "dashboard.html",
        jobs=jobs,
        last_scraped=last_scraped,
        scraping=_scraping_in_progress,
        default_max_age_days=config.MAX_JOB_AGE_DAYS,
        added=request.args.get("added"),
        status_tab=_STATUS_TAB,
    )


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


# What an Inbox card's Accept button will do, in the user's words
_ACTION_LABELS = {
    "mark_applied": "Mark as applied",
    "to_process": "Move to Process",
    "close": "Close - rejected",
    "add_note": "Add note",
    "add_job": "Add to JobBot",
    "none": "",
}


@app.context_processor
def _inbox_count():
    """Number of Inbox cards waiting, shown on the nav bar of every page."""
    conn = db.get_connection()
    count = sum(card.visible for card in gmail_suggestions.build_cards(conn))
    conn.close()
    return {"inbox_count": count}


@app.route("/inbox")
def inbox():
    # Cards are built fresh on every view from each job's current status - see gmail_suggestions
    conn = db.get_connection()
    cards = [c for c in gmail_suggestions.build_cards(conn) if c.visible]
    conn.close()
    return render_template("inbox.html", cards=cards, action_labels=_ACTION_LABELS)


@app.route("/inbox/<action>", methods=["POST"])
def inbox_resolve(action):
    """Accept or dismiss one card. The card is looked up again by key and its action recomputed
    here - the form only says which card, never what to do."""
    conn = db.get_connection()
    card = next((c for c in gmail_suggestions.build_cards(conn) if c.key == request.form.get("key")), None)
    if card is not None:
        if action == "accept":
            gmail_suggestions.apply_card(conn, card)
        elif action == "dismiss":
            db.resolve_suggestions(conn, [e["email_id"] for e in card.emails], "dismissed")
    conn.close()
    return redirect(url_for("inbox"))


@app.route("/closed")
def closed():
    # 'closed' = rejected by the company before any interview (usually accepted from the Inbox)
    conn = db.get_connection()
    jobs = _enrich(db.get_jobs(conn, ["closed"]))
    conn.close()
    return render_template("closed.html", jobs=jobs)


@app.route("/reopen/<job_id>", methods=["POST"])
def reopen(job_id):
    """Undo a close (e.g. a wrong guess on an ambiguous email) - back to 'applied'. The job was
    already counted when it became applied, so the counter isn't touched."""
    conn = db.get_connection()
    db.update_status(conn, job_id, "applied")
    conn.close()
    return _redirect_back(default="closed")


@app.route("/scrape", methods=["POST"])
def scrape_now():
    global _scraping_in_progress
    if not _scraping_in_progress:
        _scraping_in_progress = True
        threading.Thread(target=_run_scrape_in_background, daemon=True).start()
    return redirect(url_for("dashboard"))


@app.route("/clear_old", methods=["POST"])
def clear_old():
    """Manual, any-N version of the automatic config.MAX_JOB_AGE_DAYS expiry pipeline.run() does -
    e.g. after a vacation, clearing the backlog down to just the last few days."""
    try:
        days = max(0, int(request.form.get("days", "")))
    except ValueError:
        return redirect(url_for("dashboard"))
    conn = db.get_connection()
    db.expire_old_new_jobs(conn, days)
    conn.close()
    return redirect(url_for("dashboard"))


_LINKEDIN_JOB_ID = re.compile(r"linkedin\.com/.*?(?:jobs/view/(?:[^/?]*-)?|currentJobId=)(\d{6,})")
_STATUS_TAB = {"new": "New", "applied": "Applied", "process": "Processes", "closed": "Closed"}


def _normalize_job_url(url: str) -> tuple[str, str]:
    """(url, site) for a job added by hand. A LinkedIn link is rewritten to the exact form the
    scraper stores (https://www.linkedin.com/jobs/view/<id>), whatever page it was copied from
    (search results, a tracking-laden share link), so the same job found by a later scrape is
    recognized as a duplicate instead of showing up twice."""
    m = _LINKEDIN_JOB_ID.search(url)
    if m:
        return f"https://www.linkedin.com/jobs/view/{m.group(1)}", "linkedin"
    return url, "indeed" if "indeed." in url.lower() else "manual"


@app.route("/add_job", methods=["POST"])
def add_job():
    """A job the scraper didn't find, filled in by hand. Lands on New like any scraped job (so it
    can be tailored, applied to and tracked), skipping the filter - adding it means you already
    want it. If it's already in the DB but was hidden (rejected by the filter, deleted, expired),
    it's brought back to New."""
    form = {k: (request.form.get(k) or "").strip() for k in ("url", "title", "company", "location", "description")}
    if not (form["url"] and form["title"] and form["company"]):
        return redirect(url_for("dashboard", added="missing"))
    url, site = _normalize_job_url(form["url"])
    job = {**form, "url": url, "site": site, "decision": "yes", "reason": db.MANUAL_REASON,
           "location": form["location"] or None, "description": form["description"] or None}
    conn = db.get_connection()
    job_id = db.hash_url(url)
    if db.insert_job(conn, job):
        result = "ok"
    else:
        status = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
        if status in ("rejected", "deleted"):
            db.update_status(conn, job_id, "new")
            conn.execute("UPDATE jobs SET reason = ? WHERE id = ?", (db.MANUAL_REASON, job_id))
            conn.commit()
            result = "restored"
        else:
            result = status  # already new / applied / process / closed - say where it is
    conn.close()
    return redirect(url_for("dashboard", added=result) + f"#job-{job_id}")


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
