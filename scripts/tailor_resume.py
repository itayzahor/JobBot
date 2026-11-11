r"""Run the resume tailoring agent on jobs and print what it decided.

    venv\Scripts\python.exe scripts\tailor_resume.py                 (the jobs in data\resume\test_jobs.json)
    venv\Scripts\python.exe scripts\tailor_resume.py <job_id> ...    (jobs from the DB)

Each draft is saved as data\resume\jobs\<job_id>.draft.json - open the job's Resume page on the
dashboard to see it in the editor.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from jobbot import paths  # noqa: E402
from jobbot.resume import content, job_resumes, tailor  # noqa: E402

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) > 1:
        jobs = [job_resumes.job_from_db(j) or sys.exit(f"Job not found: {j}") for j in sys.argv[1:]]
    else:
        jobs = content.load_json(paths.RESUME_DIR / "test_jobs.json")

    for job in jobs:
        print(f"\n=== {job['title']} @ {job['company']}")
        draft = tailor.draft_for_job(job)
        job_resumes.save_draft({k: job.get(k) for k in ("id", "title", "company", "location", "url", "description")}, draft)
        print(f"About Me: {draft['selection']['about']}")
        for p in draft["selection"]["projects"]:
            print(f"Project: {draft['project_reasons'].get(p['id'], p['id'])}  ({len(p['bullets'])} bullets)")
        print("Skills: " + " | ".join(f"{g['group']}: {', '.join(g['items'])}" for g in draft["selection"]["skills"]))
        print("Requirements:")
        for r in draft["requirements"]:
            print(f"  [{r['match']:>7}] {'MUST' if r['must_have'] else 'nice'}  {r['quote']}" + (f"  <- {r['evidence']}" if r['evidence'] else ""))
        for n in draft["notes"]:
            print(f"Note: {n}")
        print(f"({draft['model']}, {draft['tokens']} tokens: {draft.get('token_breakdown')})")
