"""Filesystem locations, resolved relative to the project root regardless of the process's cwd -
matters because pipeline.py/app.py (root shims) and Task Scheduler both run with the project root
as the working directory today, but nothing here should silently break if that ever changes.
"""

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent  # src/jobbot/paths.py -> src/jobbot -> src -> root

DATA_DIR = ROOT_DIR / "data"
LOG_DIR = ROOT_DIR / "logs"
OUTPUT_DIR = DATA_DIR / "output"

DB_PATH = DATA_DIR / "jobs.db"
SCRAPE_STATE_PATH = DATA_DIR / "scrape_state.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
