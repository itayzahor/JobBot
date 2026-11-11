"""Per-job resumes: one JSON + PDF per job under data/resume/jobs/.

    {"job": {"id", "title", "company", "location", "url", "description"},  # snapshot
     "selection": {...},          # the resume for this job (content.py selection format)
     "main_at_start": {...},      # the main resume when this one was created - what it's compared to
     "status": "draft" | "approved" | "sent",
     "created_at", "updated_at", "approved_at",
     "sent_at", "sent_source", "status_before_sent"}   # set by freeze()

The job is snapshotted because job rows are auto-deleted after a while (db._CLEANUP_RULES), but
the resume - and what it was tailored to - should stay readable. `main_at_start` is kept for the
later feedback step: the difference between it and the final selection is what the user changed.

Once the user applies, the resume is **frozen as "sent"** (freeze(), called from db.mark_applied so
every path - dashboard, MCP, Inbox - is covered): the version they sent must stay exactly as sent.
The saved/approved version is frozen if there is one; otherwise the main resume is what was sent,
and the record just points to a shared copy of that *version* of the main resume
(main_versions/<fingerprint>.json/.pdf) - 170 applications sent with one main resume share one copy.

Retention follows the job row: cleanup() (after every pipeline run, right after
db.cleanup_old_rows) deletes a job's resume files once its row is gone - new/deleted/rejected after 7
days, applied/closed after 90, 'process' never - plus main versions no record points to anymore. A sent resume is read-only; undoing an accidental Apply
(db.undo_applied -> unfreeze()) unlocks it again. Applications found in Gmail (site='gmail' - made
outside JobBot) are never frozen: what was sent is unknown.
"""

import hashlib
import json

from datetime import datetime, timezone
from pathlib import Path

from jobbot import db, paths
from jobbot.resume import content


def json_path(job_id: str) -> Path:
    return paths.RESUME_JOBS_DIR / f"{job_id}.json"


def pdf_path(job_id: str) -> Path:
    return paths.RESUME_JOBS_DIR / f"{job_id}.pdf"


def draft_path(job_id: str) -> Path:
    """The tailoring agent's latest draft for this job (tailor.draft_for_job output + job + time).
    Kept apart from the user's own saved version so the two can be compared later."""
    return paths.RESUME_JOBS_DIR / f"{job_id}.draft.json"


def load_draft(job_id: str) -> dict | None:
    p = draft_path(job_id)
    return content.load_json(p) if p.exists() else None


def save_draft(job: dict, draft: dict) -> dict:
    record = {**draft, "job": job, "drafted_at": now()}
    content.save_json(draft_path(job["id"]), record)
    return record


def load(job_id: str) -> dict | None:
    p = json_path(job_id)
    return content.load_json(p) if p.exists() else None


def save(record: dict) -> None:
    content.save_json(json_path(record["job"]["id"]), record)


def statuses() -> dict[str, str]:
    """{job_id: status} for every job that has a resume - shown as badges on the job cards."""
    if not paths.RESUME_JOBS_DIR.exists():
        return {}
    out = {}
    for p in paths.RESUME_JOBS_DIR.glob("*.json"):
        if p.name.endswith(".draft.json"):
            continue
        try:
            out[p.stem] = content.load_json(p).get("status", "draft")
        except (OSError, ValueError):
            continue
    return out


def job_from_db(job_id: str) -> dict | None:
    fields = ("id", "title", "company", "location", "url", "description", "site", "status")
    conn = db.get_connection()
    row = conn.execute(f"SELECT {', '.join(fields)} FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return dict(zip(fields, row)) if row else None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_SNAPSHOT_FIELDS = ("id", "title", "company", "location", "url", "description")


def _main_version() -> str:
    """Fingerprint of the main resume as it renders now (its full assembled content), and make
    sure that version has its one stored copy (JSON + PDF)."""
    blocks, main = content.load_blocks(), content.load_main()
    assembled = content.assemble(main, blocks)
    version = hashlib.sha256(json.dumps(assembled, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    d = paths.RESUME_MAIN_VERSIONS_DIR
    if not (d / f"{version}.pdf").exists():
        from jobbot.resume import render  # lazy: Playwright only when a new version needs a PDF

        d.mkdir(parents=True, exist_ok=True)
        render.render(assembled, d / f"{version}.pdf")
        content.save_json(d / f"{version}.json", main)
    return version


def sent_pdf_path(job_id: str) -> Path:
    """The PDF this job's resume page shows: the shared main-version copy when the main resume was
    sent, otherwise the job's own PDF."""
    record = load(job_id)
    if record and record.get("main_version"):
        return paths.RESUME_MAIN_VERSIONS_DIR / f"{record['main_version']}.pdf"
    return pdf_path(job_id)


def freeze(job_id: str) -> bool:
    """Lock this job's resume as the version sent with the application. Returns True if a resume
    was frozen. Idempotent: an already-sent resume is left exactly as it is."""
    job = job_from_db(job_id)
    if job is None or job.get("site") == "gmail":
        return False
    record = load(job_id)
    if record and record.get("status") == "sent":
        return True
    paths.RESUME_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    if record and record.get("created_at"):
        source = "approved" if record.get("status") == "approved" else "saved draft"
        if not pdf_path(job_id).exists():  # shouldn't happen - every save renders - but be safe
            _render_pdf(record["selection"], job_id)
        record["status_before_sent"] = record.get("status", "draft")
    else:
        # Nothing saved for this job: the main resume is what went out - point to its version.
        record = {"job": {k: job.get(k) for k in _SNAPSHOT_FIELDS}, "main_version": _main_version(),
                  "created_at": now()}
        source = "main resume"
    record["status"], record["sent_at"], record["sent_source"] = "sent", now(), source
    save(record)
    return True


def unfreeze(job_id: str) -> None:
    """Undo of an accidental Apply: unlock the resume. A record that only pointed to a main-resume
    version (the user never saved anything for this job) is removed; the shared version copy is
    left for cleanup() to collect if nothing else uses it."""
    record = load(job_id)
    if not record or record.get("status") != "sent":
        return
    if record.get("main_version"):
        json_path(job_id).unlink(missing_ok=True)
        return
    record["status"] = record.pop("status_before_sent", "draft")
    for k in ("sent_at", "sent_source"):
        record.pop(k, None)
    save(record)


def cleanup(conn) -> dict:
    """Delete resume files that outlived their job, per the job table's own retention (see the
    module docstring). Returns {"job_files": n, "main_versions": n, "html": n} deleted."""
    alive = {row[0] for row in conn.execute("SELECT id FROM jobs")}
    counts = {"job_files": 0, "main_versions": 0, "html": 0}
    for f in paths.RESUME_DIR.rglob("*.html"):  # left over from before the HTML stopped being kept
        f.unlink(missing_ok=True)
        counts["html"] += 1
    referenced = set()
    if paths.RESUME_JOBS_DIR.exists():
        for f in list(paths.RESUME_JOBS_DIR.iterdir()):
            job_id = f.name.split(".")[0]
            if job_id not in alive:
                f.unlink(missing_ok=True)
                counts["job_files"] += 1
            elif f.name == f"{job_id}.json":
                try:
                    if (v := content.load_json(f).get("main_version")):
                        referenced.add(v)
                except (OSError, ValueError):
                    pass
    if paths.RESUME_MAIN_VERSIONS_DIR.exists():
        for f in list(paths.RESUME_MAIN_VERSIONS_DIR.iterdir()):
            if f.stem not in referenced:
                f.unlink(missing_ok=True)
                counts["main_versions"] += 1
    return counts


def _render_pdf(selection: dict, job_id: str) -> None:
    from jobbot.resume import render  # lazy: Playwright only when a PDF is actually needed

    render.render(content.assemble(selection, content.load_blocks()), pdf_path(job_id))


def new_record(job: dict) -> dict:
    """A not-yet-saved resume for `job`, starting from the current main resume."""
    main = content.load_main()
    return {"job": job, "selection": main, "main_at_start": main, "status": "draft",
            "created_at": None, "updated_at": None, "approved_at": None}
