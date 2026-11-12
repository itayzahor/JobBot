r"""Add a job the scraper didn't find, so it can be tailored, applied to and tracked like the rest.

    venv\Scripts\python.exe scripts\add_job.py https://www.linkedin.com/jobs/view/1234567890/ [more urls...]

LinkedIn links are read from LinkedIn's public (logged-out) job page - the same source the
scraper's LinkedIn descriptions come from. The job lands on the dashboard's New tab as a normal
row (`site='linkedin'`, the same normalized URL the scraper would store, so dedup still works if
it shows up in a later scrape). It skips the filter on purpose: you add a job because you've
already decided to apply.
"""

import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jobbot import db  # noqa: E402

_LINKEDIN_ID = re.compile(r"linkedin\.com/jobs/(?:view|collections/[^?]*\?.*currentJobId=)/?(?:[^/?]*-)?(\d{6,})")
_GUEST_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/126.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}


def fetch_linkedin(job_id: str) -> dict:
    """Title, company, location and description (as Markdown, like the scraper stores it)."""
    resp = requests.get(_GUEST_URL.format(job_id), headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    def text(selector):
        el = soup.select_one(selector)
        return el.get_text(" ", strip=True) if el else None

    desc = soup.select_one("div.show-more-less-html__markup")
    return {
        "url": f"https://www.linkedin.com/jobs/view/{job_id}",
        "title": text("h2.top-card-layout__title") or text("h1"),
        "company": text("a.topcard__org-name-link") or text("span.topcard__flavor"),
        "location": text("span.topcard__flavor--bullet"),
        "description": markdownify(str(desc)).strip() if desc else None,
        "site": "linkedin",
        "decision": "yes",
        "reason": db.MANUAL_REASON,
    }


def main(urls: list[str]) -> None:
    conn = db.get_connection()
    for url in urls:
        m = _LINKEDIN_ID.search(url)
        if not m:
            print(f"SKIPPED {url} - not a LinkedIn job link (only LinkedIn is supported for now)")
            continue
        job = fetch_linkedin(m.group(1))
        if not job["title"] or not job["description"]:
            print(f"FAILED  {url} - LinkedIn didn't return the posting (expired, or blocked the request)")
            continue
        inserted = db.insert_job(conn, job)
        state = "ADDED  " if inserted else "ALREADY IN DB"
        print(f"{state} {job['title']} @ {job['company']} ({job['location']}) - "
              f"{len(job['description'])} chars of description")
    conn.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])
