"""Dashboard pages for the resume: the Main Resume editor and the per-job resume editor.

Both pages are the same editor (templates/resume_editor.html) working on a *selection* (see
content.py) in the browser, asking the server for a live preview on every change. The preview is
the real template filled by the real `assemble()`, so what the user sees is what the PDF will be.
Save re-checks on the server, renders the PDF to a temp file (fit, links and font are checked
there), and only then replaces the saved JSON and PDF - a resume that fails a check is never saved.

The two modes differ in what a bullet edit means:
- Main Resume: main is the master copy, so editing a bullet edits its *approved* wording - the
  browser sends {project, old, new} replacements and Save applies them to blocks.json.
- Per-job: an edit is for that job only. It stays in the job's selection as unapproved wording
  (a warning, not an error) and blocks.json is untouched.
"""

import os
from pathlib import Path

from flask import Blueprint, abort, jsonify, render_template, request, send_file

from jobbot import paths
from jobbot.resume import content, job_resumes, render

bp = Blueprint("resume", __name__)


def _main_pdf_path() -> Path:
    return paths.RESUME_DIR / "main.pdf"


@bp.app_context_processor
def _resume_statuses():
    """{job_id: 'draft'|'approved'} for the Resume buttons on job cards - only on job-list pages."""
    if request.endpoint in ("dashboard", "applied", "processes", "closed", "deleted"):
        # values: draft | approved | sent
        return {"resume_status": job_resumes.statuses()}
    return {"resume_status": {}}


def _apply_replacements(blocks: dict, replacements: list[dict]) -> list[str]:
    """Replace approved bullet wording in place. Returns problems (unknown project/bullet)."""
    problems = []
    projects = content.projects_by_id(blocks)
    for r in replacements:
        project = projects.get(r.get("project"))
        if project is None or r.get("old") not in project["bullets"]:
            problems.append(f"can't find the original bullet to replace: {r.get('old')!r}")
            continue
        if not str(r.get("new", "")).strip():
            problems.append("a bullet can't be replaced with empty text")
            continue
        project["bullets"][project["bullets"].index(r["old"])] = r["new"].strip()
    return problems


def _render_checked(selection: dict, blocks: dict, pdf: Path):
    """Render to a temp file and move it into place only if every check passes.
    Returns (RenderResult, None) or (None, error message)."""
    tmp = pdf.with_name(pdf.stem + ".tmp.pdf")
    try:
        result = render.render(content.assemble(selection, blocks), tmp)
    except render.ResumeCheckError as e:
        for p in (tmp, tmp.with_suffix(".html")):
            p.unlink(missing_ok=True)
        return None, str(e)
    os.replace(tmp, pdf)
    os.replace(tmp.with_suffix(".html"), pdf.with_suffix(".html"))
    return result, None


def _pdf_response(pdf: Path):
    if not pdf.exists():
        abort(404, "No PDF yet - press Save first.")
    name = content.load_blocks()["fixed"]["name"]
    return send_file(pdf, mimetype="application/pdf", as_attachment=request.args.get("download") == "1",
                     download_name=f"{name}.pdf")


# ---------- shared ----------

@bp.route("/resume/preview", methods=["POST"])
def preview():
    """Live preview: the filled template + check results for the selection being edited."""
    payload = request.get_json(force=True)
    blocks = content.load_blocks()
    problems = _apply_replacements(blocks, payload.get("replacements", []))  # preview as if saved
    check = content.check(payload["selection"], blocks)
    errors = problems + check.errors
    html = None if errors else render.build_html(content.assemble(payload["selection"], blocks))
    return jsonify(html=html, errors=errors, warnings=check.warnings)


# ---------- main resume ----------

@bp.route("/resume")
def main_page():
    if not paths.RESUME_BLOCKS_PATH.exists() or not paths.RESUME_MAIN_PATH.exists():
        abort(404, "No resume content yet - data/resume/blocks.json and main.json are missing.")
    main = content.load_main()
    return render_template("resume_editor.html", mode="main", blocks=content.load_blocks(),
                           selection=main, main=main, has_pdf=_main_pdf_path().exists(), record=None,
                           draft=None)


@bp.route("/resume/save", methods=["POST"])
def save():
    payload = request.get_json(force=True)
    selection = payload["selection"]
    blocks = content.load_blocks()
    problems = _apply_replacements(blocks, payload.get("replacements", []))
    check = content.check(selection, blocks)
    if problems or not check.ok:
        return jsonify(ok=False, errors=problems + check.errors), 400
    result, error = _render_checked(selection, blocks, _main_pdf_path())
    if error:
        return jsonify(ok=False, errors=[error]), 400
    if payload.get("replacements"):
        content.save_json(paths.RESUME_BLOCKS_PATH, blocks)
    content.save_json(paths.RESUME_MAIN_PATH, selection)
    return jsonify(ok=True, warnings=check.warnings, main_free_mm=round(result.main_free_mm, 1),
                   free_mm=round(min(result.main_free_mm, result.sidebar_free_mm), 1))


@bp.route("/resume/main.pdf")
def main_pdf():
    return _pdf_response(_main_pdf_path())


# ---------- per-job resumes ----------

def _job_record(job_id: str) -> dict:
    """The saved resume for this job, or a fresh one from the main resume. 404 if neither the
    resume nor the job exists."""
    record = job_resumes.load(job_id)
    if record:
        return record
    job = job_resumes.job_from_db(job_id)
    if job is None:
        abort(404, "Job not found.")
    return job_resumes.new_record(job)


_APPLIED_STATUSES = ("applied", "process", "closed")


def _locked(job_id: str, record: dict) -> bool:
    """A resume is read-only once sent - or when the job was applied to before resumes were kept
    (nothing was recorded, and editing now would invent a version that was never sent)."""
    if record.get("status") == "sent":
        return True
    job = job_resumes.job_from_db(job_id)
    return bool(job and job.get("status") in _APPLIED_STATUSES)


def _refuse_if_locked(job_id: str, record: dict):
    if _locked(job_id, record):
        return jsonify(ok=False, errors=["This resume was sent with your application - it can't be changed."],
                       error="This resume was sent with your application - it can't be changed."), 409
    return None


@bp.route("/resume/job/<job_id>")
def job_page(job_id):
    record = _job_record(job_id)
    if _locked(job_id, record):
        sent = record.get("status") == "sent"
        return render_template("resume_sent.html", record=record, sent=sent,
                               has_pdf=sent and job_resumes.pdf_path(job_id).exists())
    draft = job_resumes.load_draft(job_id)
    # Not saved yet but the agent drafted one: open with the draft in the editor (unsaved).
    selection = draft["selection"] if draft and not record["created_at"] else record["selection"]
    return render_template("resume_editor.html", mode="job", blocks=content.load_blocks(),
                           selection=selection, main=content.load_main(), record=record,
                           draft=draft, has_pdf=job_resumes.pdf_path(job_id).exists())


@bp.route("/resume/job/<job_id>/draft", methods=["POST"])
def job_draft(job_id):
    """Run the tailoring agent for this job (one model call + rule checks + fit, ~20-40s)."""
    from jobbot.resume import tailor  # lazy: pulls in the Gemini client only when used

    record = _job_record(job_id)
    if (refused := _refuse_if_locked(job_id, record)):
        return refused
    try:
        draft = tailor.draft_for_job(record["job"])
    except Exception as e:  # shown to the user - the editor keeps working without a draft
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 500
    return jsonify(ok=True, draft=job_resumes.save_draft(record["job"], draft))


@bp.route("/resume/job/<job_id>/save", methods=["POST"])
def job_save(job_id):
    """Save this job's resume. approve=true also marks it as the version to send."""
    record = _job_record(job_id)  # 404 before anything is written if the job doesn't exist
    if (refused := _refuse_if_locked(job_id, record)):
        return refused
    payload = request.get_json(force=True)
    selection = payload["selection"]
    blocks = content.load_blocks()
    check = content.check(selection, blocks)
    if not check.ok:
        return jsonify(ok=False, errors=check.errors), 400
    paths.RESUME_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    result, error = _render_checked(selection, blocks, job_resumes.pdf_path(job_id))
    if error:
        return jsonify(ok=False, errors=[error]), 400

    now = job_resumes.now()
    record["selection"] = selection
    # The three About Me cards (strengths / personal / main) and which one is on the resume -
    # kept so the user's edits to the versions not in use survive a reload.
    if payload.get("about_style") in ("strengths", "personal", "main"):
        record["about_style"] = payload["about_style"]
    if isinstance(payload.get("about_texts"), dict):
        record["about_texts"] = {k: v for k, v in payload["about_texts"].items()
                                 if k in ("strengths", "personal", "main") and isinstance(v, str)}
    record["created_at"] = record["created_at"] or now
    record["updated_at"] = now
    if payload.get("approve"):
        record["status"], record["approved_at"] = "approved", now
    else:
        record["status"], record["approved_at"] = "draft", None  # a change after approval needs re-approval
    job_resumes.save(record)
    return jsonify(ok=True, status=record["status"], warnings=check.warnings,
                   main_free_mm=round(result.main_free_mm, 1),
                   free_mm=round(min(result.main_free_mm, result.sidebar_free_mm), 1))


@bp.route("/resume/job/<job_id>/make_main", methods=["POST"])
def job_make_main(job_id):
    """Use this job's resume as the new main resume. Refused while it has unapproved wording -
    main may only hold approved bullets (wording is changed on the Resume tab itself)."""
    selection = request.get_json(force=True)["selection"]
    blocks = content.load_blocks()
    check = content.check(selection, blocks)
    if not check.ok or check.warnings:
        return jsonify(ok=False, errors=check.errors + [
            "Main can only use approved bullet wording - undo the edited bullets here, or edit them on the Resume tab."
        ] * bool(check.warnings)), 400
    result, error = _render_checked(selection, blocks, _main_pdf_path())
    if error:
        return jsonify(ok=False, errors=[error]), 400
    content.save_json(paths.RESUME_MAIN_PATH, selection)
    return jsonify(ok=True)


@bp.route("/resume/job/<job_id>/pdf")
def job_pdf(job_id):
    return _pdf_response(job_resumes.pdf_path(job_id))
