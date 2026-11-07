"""Filesystem locations, resolved relative to the project root regardless of the process's cwd -
matters because pipeline.py/app.py (root shims) and Task Scheduler both run with the project root
as the working directory today, but nothing here should silently break if that ever changes.
"""

import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent  # src/jobbot/paths.py -> src/jobbot -> src -> root

DATA_DIR = ROOT_DIR / "data"
LOG_DIR = ROOT_DIR / "logs"
OUTPUT_DIR = DATA_DIR / "output"

# JOBBOT_DB_PATH lets qa/test_mcp_server.py point a spawned server at a throwaway DB instead of
# the real one - nothing in production sets it.
DB_PATH = Path(os.environ.get("JOBBOT_DB_PATH", DATA_DIR / "jobs.db"))
# The user's candidate profile (resume master record) - personal, so it lives in gitignored data/.
PROFILE_PATH = Path(os.environ.get("JOBBOT_PROFILE_PATH", DATA_DIR / "profile.md"))
SCRAPE_STATE_PATH = DATA_DIR / "scrape_state.json"
# Gmail agent. The credentials file is the OAuth client downloaded from Google Cloud; the token is
# created on first login and is the real key to the mailbox - both stay in gitignored data/.
GMAIL_CREDENTIALS_PATH = DATA_DIR / "gmail_credentials.json"
GMAIL_TOKEN_PATH = DATA_DIR / "gmail_token.json"
GMAIL_STATE_PATH = DATA_DIR / "gmail_state.json"  # when the Gmail agent last finished a run

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
