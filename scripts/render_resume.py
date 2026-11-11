r"""Render a resume to a one-page PDF and run the checks (fit, links, font).

    venv\Scripts\python.exe scripts\render_resume.py                     (the main resume)
    venv\Scripts\python.exe scripts\render_resume.py data\resume\jobs\<id>.json
    venv\Scripts\python.exe scripts\render_resume.py src\jobbot\resume\example_resume.json -o out.pdf

Accepts either a selection (main.json / a per-job resume - assembled with data\resume\blocks.json)
or a full resume dict (like example_resume.json). Writes <name>.pdf and <name>.html next to the
JSON unless -o is given.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from jobbot import paths  # noqa: E402
from jobbot.resume import content  # noqa: E402
from jobbot.resume import render as resume_render  # noqa: E402

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("resume_json", type=Path, nargs="?", default=paths.RESUME_MAIN_PATH)
    parser.add_argument("-o", "--out", type=Path, help="output PDF path")
    args = parser.parse_args()

    data = content.load_json(args.resume_json)
    if "contact" not in data:  # a selection - check it against the approved blocks, then assemble
        blocks = content.load_blocks()
        check = content.check(data, blocks)
        for w in check.warnings:
            print(f"WARNING: {w}")
        if not check.ok:
            print("FAILED: " + "; ".join(check.errors))
            sys.exit(1)
        data = content.assemble(data, blocks)

    out = args.out or args.resume_json.with_suffix(".pdf")
    try:
        result = resume_render.render(data, out)
    except resume_render.ResumeCheckError as e:
        print(f"FAILED: {e}")
        sys.exit(1)
    print(f"PDF:  {result.pdf_path}")
    print(f"HTML: {result.html_path}")
    print(f"Free space at the bottom: main {result.main_free_mm:.1f} mm, sidebar {result.sidebar_free_mm:.1f} mm")
    print("Links in the PDF:")
    for url in result.links:
        print(f"  {url}")
