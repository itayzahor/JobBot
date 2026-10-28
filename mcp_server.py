"""Thin entry point for the MCP server, kept at the project root like app.py/pipeline.py so the
Claude Desktop config can point at a stable path. The real server lives in
src/jobbot/mcp_server.py. Claude Desktop starts this itself (stdio transport) - you never run it
by hand except to debug.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from jobbot.mcp_server import mcp  # noqa: E402

if __name__ == "__main__":
    mcp.run()
