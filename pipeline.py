"""Thin entry point kept at the project root on purpose: Windows Task Scheduler's `JobBot-Pipeline`
task and run_pipeline_task.bat both invoke this exact path (`pythonw.exe pipeline.py`) with the
project root as the working directory - moving it into src/jobbot/ would require updating that
live scheduled task. The real logic lives in src/jobbot/pipeline.py; this just wires up sys.path,
the --log-to-file redirect (pythonw has no real stdout), and CLI flags.
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from jobbot import pipeline  # noqa: E402
from jobbot.paths import LOG_DIR  # noqa: E402

if __name__ == "__main__":
    if "--log-to-file" in sys.argv:
        # Used by the Task Scheduler action, which runs pythonw.exe directly (no console window
        # at all, unlike python.exe/cmd.exe) - pythonw has no real stdout to print to, so redirect
        # our own prints into a persistent log file instead of losing them silently. Manual runs
        # (`python pipeline.py` from a terminal) don't pass this flag and keep printing normally.
        log_file = open(LOG_DIR / "pipeline.log", "a", encoding="utf-8")
        sys.stdout = log_file
        sys.stderr = log_file
        print(f"\n=== {datetime.now(timezone.utc).isoformat()} ===")
    pipeline.run(force="--force" in sys.argv)
