"""Render a resume (JSON data) to a one-page PDF and verify it.

Content and layout are kept apart: the resume is plain data (see `example_resume.json` for the
shape), `template.html` is the design - a copy of the Google Slides resume. Rendering goes through
headless Chromium (Playwright), which writes the PDF directly - no print dialog - so links stay
real, clickable link annotations (the Slides print path had lost the LinkedIn link).

Every render is checked, and a failed check raises instead of handing back a broken resume:
- fit:   the main column and the sidebar each end above the bottom of the page;
- links: every URL in the data is a clickable link in the PDF;
- fonts: Poppins actually loaded (a silent fallback font would change the layout).
"""

import base64
import json
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright
from pypdf import PdfReader

HERE = Path(__file__).resolve().parent
FONTS_DIR = HERE / "fonts"
PX_PER_MM = 96 / 25.4  # CSS pixels per millimetre


class ResumeCheckError(Exception):
    """The rendered resume failed a check (overflow, missing link, font)."""


@dataclass
class RenderResult:
    html_path: Path
    pdf_path: Path
    main_free_mm: float     # empty space left at the bottom of the main column
    sidebar_free_mm: float  # same for the sidebar
    links: list[str]        # link URLs found in the PDF


def _font_b64(name: str) -> str:
    return base64.b64encode((FONTS_DIR / name).read_bytes()).decode("ascii")


def build_html(resume: dict) -> str:
    """Fill the template with the resume data (autoescaped)."""
    env = Environment(loader=FileSystemLoader(HERE), autoescape=select_autoescape(["html"]))
    fonts = {"regular": _font_b64("Poppins-Regular.ttf"), "bold": _font_b64("Poppins-Bold.ttf")}
    return env.get_template("template.html").render(r=resume, fonts=fonts)


def expected_urls(resume: dict) -> set[str]:
    """Every URL the resume links to - each one must come out clickable in the PDF."""
    urls = {f"mailto:{resume['contact']['email']}"}
    urls |= {link["url"] for link in resume["contact"]["links"]}
    urls |= {p["link"]["url"] for p in resume["projects"] if p.get("link")}
    return urls


def pdf_links(pdf_path: Path) -> tuple[int, list[str]]:
    """(page count, link URLs) read back from the PDF file itself."""
    reader = PdfReader(str(pdf_path))
    urls = []
    for page in reader.pages:
        for annot in page.get("/Annots") or []:
            a = annot.get_object()
            if a.get("/Subtype") == "/Link" and "/A" in a and "/URI" in a["/A"]:
                urls.append(str(a["/A"]["/URI"]))
    return len(reader.pages), urls


def render(resume: dict, out_pdf: Path) -> RenderResult:
    """Render `resume` to `out_pdf` (plus an .html next to it) and run every check."""
    out_pdf = Path(out_pdf).resolve()
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    html_path = out_pdf.with_suffix(".html")
    html_path.write_text(build_html(resume), encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(html_path.as_uri())
        page.evaluate("document.fonts.ready")
        fonts_ok = page.evaluate("document.fonts.check('11pt Poppins') && document.fonts.check('bold 13pt Poppins')")
        # How far each column's content ends from the bottom of the page.
        free = page.evaluate("""() => {
            const pageBottom = document.querySelector('.page').getBoundingClientRect().bottom;
            const lastBottom = (col) => {
                const kids = [...document.getElementById(col).children];
                return Math.max(...kids.map(k => k.getBoundingClientRect().bottom));
            };
            return {main: pageBottom - lastBottom('main'), sidebar: pageBottom - lastBottom('sidebar')};
        }""")
        page.pdf(path=str(out_pdf), width="210mm", height="300mm", print_background=True)
        browser.close()

    pages, links = pdf_links(out_pdf)
    result = RenderResult(html_path, out_pdf, free["main"] / PX_PER_MM, free["sidebar"] / PX_PER_MM, links)

    problems = []
    if not fonts_ok:
        problems.append("Poppins font did not load")
    if pages != 1:
        problems.append(f"PDF has {pages} pages, expected 1")
    if result.main_free_mm < 0:
        problems.append(f"main column overflows the page by {-result.main_free_mm:.1f} mm")
    if result.sidebar_free_mm < 0:
        problems.append(f"sidebar overflows the page by {-result.sidebar_free_mm:.1f} mm")
    missing = expected_urls(resume) - set(links)
    if missing:
        problems.append(f"links missing from the PDF: {sorted(missing)}")
    if problems:
        raise ResumeCheckError("; ".join(problems))
    return result


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
