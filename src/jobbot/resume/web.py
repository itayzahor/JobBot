"""Dashboard pages for the resume: the Main Resume editor (more per-job pages to follow).

The editor works on a *selection* (see content.py) in the browser and asks the server for a live
preview on every change. The preview is the real template filled by the real `assemble()`, so what
the user sees is exactly what the PDF will be. Save re-checks on the server, renders the PDF (fit,
links and font are checked there), and only then overwrites main.json - a resume that fails a
check is never saved.

Editing a bullet on the Main Resume page edits the approved wording itself: the browser sends the
change as a {project, old, new} replacement, and Save applies it to blocks.json. Main is the
master copy, so its wording is by definition approved.
"""

import os
from pathlib import Path

from flask import Blueprint, abort, jsonify, render_template, request, send_file

from jobbot import paths
from jobbot.resume import content, render

bp = Blueprint("resume", __name__)


def _main_pdf_path() -> Path:
    return paths.RESUME_DIR / "main.pdf"


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


@bp.route("/resume")
def main_page():
    if not paths.RESUME_BLOCKS_PATH.exists() or not paths.RESUME_MAIN_PATH.exists():
        abort(404, "No resume content yet - data/resume/blocks.json and main.json are missing.")
    return render_template(
        "resume_main.html",
        blocks=content.load_blocks(),
        selection=content.load_main(),
        has_pdf=_main_pdf_path().exists(),
    )


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


@bp.route("/resume/save", methods=["POST"])
def save():
    payload = request.get_json(force=True)
    selection = payload["selection"]
    blocks = content.load_blocks()
    problems = _apply_replacements(blocks, payload.get("replacements", []))
    check = content.check(selection, blocks)
    if problems or not check.ok:
        return jsonify(ok=False, errors=problems + check.errors), 400

    # Render to a temporary file first: if the PDF fails a check, nothing on disk changes.
    tmp_pdf = paths.RESUME_DIR / "main.tmp.pdf"
    try:
        result = render.render(content.assemble(selection, blocks), tmp_pdf)
    except render.ResumeCheckError as e:
        for p in (tmp_pdf, tmp_pdf.with_suffix(".html")):
            p.unlink(missing_ok=True)
        return jsonify(ok=False, errors=[str(e)]), 400
    os.replace(tmp_pdf, _main_pdf_path())
    os.replace(tmp_pdf.with_suffix(".html"), _main_pdf_path().with_suffix(".html"))
    if payload.get("replacements"):
        content.save_json(paths.RESUME_BLOCKS_PATH, blocks)
    content.save_json(paths.RESUME_MAIN_PATH, selection)
    return jsonify(ok=True, warnings=check.warnings, main_free_mm=round(result.main_free_mm, 1),
                   sidebar_free_mm=round(result.sidebar_free_mm, 1))


@bp.route("/resume/main.pdf")
def main_pdf():
    pdf = _main_pdf_path()
    if not pdf.exists():
        abort(404, "No PDF yet - press Save on the Resume page first.")
    name = content.load_blocks()["fixed"]["name"]
    return send_file(pdf, mimetype="application/pdf", as_attachment=request.args.get("download") == "1",
                     download_name=f"{name}.pdf")
