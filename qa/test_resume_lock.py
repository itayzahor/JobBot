"""Resume lock: once a job is applied to, its resume is frozen as "sent" and can't be changed.

Runs every apply path's shared function (db.mark_applied / db.undo_applied) and the dashboard routes
against a throwaway DB and a throwaway resume folder with made-up content - nothing real is
touched, and nothing personal is in this file (the repo is public).

    venv\\Scripts\\python.exe qa\\test_resume_lock.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="jobbot_lock_test_"))
os.environ["JOBBOT_DB_PATH"] = str(TMP / "jobs.db")
os.environ["JOBBOT_RESUME_DIR"] = str(TMP / "resume")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jobbot import db  # noqa: E402
from jobbot.resume import job_resumes  # noqa: E402

FAILURES = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


def setup_content():
    """A made-up candidate: just enough blocks + a main resume to render."""
    resume_dir = TMP / "resume"
    resume_dir.mkdir()
    example = json.loads((Path(__file__).resolve().parent.parent / "src/jobbot/resume/example_resume.json")
                         .read_text(encoding="utf-8"))
    p = example["projects"][0]
    blocks = {
        "fixed": {k: example[k] for k in ("name", "education", "military", "contact", "languages")},
        "about_versions": [{"id": "default", "text": example["about"]}],
        "projects": [{"id": "trail", "title": p["title"], "link": p["link"], "bullets": p["bullets"],
                      "skills": ["Python", "FastAPI"]}],
        "skill_pool": [{"group": "Programming", "items": ["Python", "SQL"]},
                       {"group": "Backend", "items": ["FastAPI"]}],
    }
    main = {"about": example["about"], "projects": [{"id": "trail", "bullets": p["bullets"]}],
            "skills": [{"group": "Programming", "items": ["Python", "SQL"]}]}
    (resume_dir / "blocks.json").write_text(json.dumps(blocks), encoding="utf-8")
    (resume_dir / "main.json").write_text(json.dumps(main), encoding="utf-8")
    return main


def add_job(conn, n, site="linkedin"):
    job = {"url": f"https://example.com/job/{n}", "title": f"Engineer {n}", "company": f"Acme {n}",
           "description": "Build backend services in Python.", "site": site}
    db.insert_job(conn, job)
    return db.hash_url(job["url"])


def main():
    main_sel = setup_content()
    conn = db.get_connection()
    no_resume, approved, outside, old = (add_job(conn, 1), add_job(conn, 2),
                                         add_job(conn, 3, site="gmail"), add_job(conn, 4))

    # A saved + approved resume for job 2 (a different About Me than main, to tell them apart)
    rec = job_resumes.new_record({k: v for k, v in job_resumes.job_from_db(approved).items()
                                  if k in job_resumes._SNAPSHOT_FIELDS})
    rec["selection"] = dict(main_sel, about="Software Engineer who builds backend systems.")
    rec.update(status="approved", created_at=job_resumes.now())
    job_resumes.save(rec)
    job_resumes._render_pdf(rec["selection"], approved)

    # --- apply: freeze ---
    db.mark_applied(conn, no_resume)
    r = job_resumes.load(no_resume)
    check("no saved resume -> the main resume is frozen as sent",
          r and r["status"] == "sent" and r["sent_source"] == "main resume" and r["selection"] == main_sel)
    check("  ...and its PDF exists", job_resumes.pdf_path(no_resume).exists())

    db.mark_applied(conn, approved)
    r = job_resumes.load(approved)
    check("approved resume -> frozen as sent, its own content kept",
          r["status"] == "sent" and r["sent_source"] == "approved"
          and r["selection"]["about"] == "Software Engineer who builds backend systems.")

    sent_at = r["sent_at"]
    db.mark_applied(conn, approved)  # e.g. applied again via an Inbox card - must not re-freeze
    check("freezing twice leaves the sent version untouched", job_resumes.load(approved)["sent_at"] == sent_at)

    db.mark_applied(conn, outside)
    check("application found in Gmail (made outside JobBot) is not frozen", job_resumes.load(outside) is None)

    # --- dashboard: locked ---
    from jobbot.webapp import app
    c = app.test_client()
    sel = job_resumes.load(approved)["selection"]
    resp = c.post(f"/resume/job/{approved}/save", json={"selection": sel})
    check("saving a sent resume is refused (409)", resp.status_code == 409)
    resp = c.post(f"/resume/job/{approved}/draft", json={})
    check("drafting with AI for a sent resume is refused (409)", resp.status_code == 409)
    page = c.get(f"/resume/job/{approved}").get_data(as_text=True)
    check("the job's resume page is the read-only 'sent' page", "locked so it stays exactly as sent" in page)
    check("Applied cards show 'Resume sent'", "Resume sent" in c.get("/applied").get_data(as_text=True))

    db.update_status(conn, old, "applied")  # applied before resumes were kept: nothing recorded
    conn.commit()
    page = c.get(f"/resume/job/{old}").get_data(as_text=True)
    check("an old application shows 'No resume was recorded' (not an editor)", "No resume was recorded" in page)
    resp = c.post(f"/resume/job/{old}/save", json={"selection": main_sel})
    check("  ...and saving one is refused too (409)", resp.status_code == 409)

    # --- undo apply: unlock ---
    db.undo_applied(conn, approved)
    check("undo apply -> the approved resume is unlocked again", job_resumes.load(approved)["status"] == "approved")
    db.undo_applied(conn, no_resume)
    check("undo apply -> the frozen main-resume copy is removed",
          job_resumes.load(no_resume) is None and not job_resumes.pdf_path(no_resume).exists())
    conn.close()

    print(f"\n{'ALL PASSED' if not FAILURES else f'{len(FAILURES)} FAILED'} (throwaway data in {TMP})")
    sys.exit(1 if FAILURES else 0)


main()
