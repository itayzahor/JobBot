"""Thin entry point kept at the project root on purpose: the `JobBot-Dashboard` Task Scheduler
task and start_dashboard.bat (and the desktop shortcut pointing at that .bat) both invoke this
exact path (`pythonw.exe app.py` / `python.exe app.py`) with the project root as the working
directory - moving it into src/jobbot/ would require updating that live scheduled task and
shortcut. The real Flask app lives in src/jobbot/webapp.py; this just wires up sys.path and the
--log-to-file redirect (pythonw has no real stdout).
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from jobbot import webapp  # noqa: E402
from jobbot.paths import LOG_DIR  # noqa: E402

if __name__ == "__main__":
    if "--log-to-file" in sys.argv:
        # Used by the Task Scheduler "at log on" auto-start task, which runs pythonw.exe (no
        # console, so no visible window at every login) - same reasoning and pattern as
        # pipeline.py's --log-to-file. Manual runs (start_dashboard.bat) don't pass this and keep
        # printing to the visible console, per explicit user preference for that one.
        log_file = open(LOG_DIR / "app.log", "a", encoding="utf-8")
        sys.stdout = log_file
        sys.stderr = log_file
        print(f"\n=== {datetime.now(timezone.utc).isoformat()} ===")
    webapp.main()
