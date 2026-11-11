"""Per-job resumes: one JSON + PDF per job under data/resume/jobs/.

    {"job": {"id", "title", "company", "location", "url", "description"},  # snapshot
     "selection": {...},          # the resume for this job (content.py selection format)
     "main_at_start": {...},      # the main resume when this one was created - what it's compared to
     "status": "draft" | "approved",
     "created_at", "updated_at", "approved_at"}

The job is snapshotted because job rows are auto-deleted after a while (db._CLEANUP_RULES), but
the resume - and what it was tailored to - should stay readable. `main_at_start` is kept for the
later feedback step: the difference between it and the final selection is what the user changed.
(When the tailoring agent exists, its draft will be stored here too.)
"""

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
    fields = ("id", "title", "company", "location", "url", "description")
    conn = db.get_connection()
    row = conn.execute(f"SELECT {', '.join(fields)} FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return dict(zip(fields, row)) if row else None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_record(job: dict) -> dict:
    """A not-yet-saved resume for `job`, starting from the current main resume."""
    main = content.load_main()
    return {"job": job, "selection": main, "main_at_start": main, "status": "draft",
            "created_at": None, "updated_at": None, "approved_at": None}
