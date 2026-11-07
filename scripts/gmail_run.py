"""Run the Gmail agent once by hand (the scheduled path runs it after each pipeline scrape).
Opens a browser to log in if the Gmail token is missing or revoked.

    venv\Scripts\python.exe scripts\gmail_run.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from jobbot import gmail_agent  # noqa: E402

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    gmail_agent.run(interactive=True)
