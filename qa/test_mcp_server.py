"""End-to-end check of the MCP server, the same way Claude Desktop talks to it: spawn
mcp_server.py as a subprocess, speak MCP over stdin/stdout, call every tool, then inspect the DB.

Runs against a throwaway DB of fake jobs (via JOBBOT_DB_PATH), never the real data/jobs.db.

    venv\\Scripts\\python.exe qa\\test_mcp_server.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parent.parent
TEST_DIR = Path(tempfile.mkdtemp())
TEST_DB = TEST_DIR / "test_jobs.db"
TEST_PROFILE = TEST_DIR / "profile.md"
TEST_PROFILE.write_text("# Test Candidate\n- Built a chess-vision app with YOLO.\n", encoding="utf-8")
# Before importing jobbot, so paths.py picks them up - never touch the real DB/profile.
os.environ["JOBBOT_DB_PATH"] = str(TEST_DB)
os.environ["JOBBOT_PROFILE_PATH"] = str(TEST_PROFILE)
sys.path.insert(0, str(ROOT / "src"))

from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

from jobbot import db  # noqa: E402

FAKE_JOBS = [
    ("AI Engineer", "Wix", "applied", "Build LLM agents with RAG and evaluation pipelines."),
    ("Backend Developer", "Wix", "new", "Python, Kubernetes, microservices."),
    ("Data Scientist", "Monday.com", "new", "Statistics, SQL, experimentation."),
    ("ML Engineer", "Lightricks", "process", "Train and deploy vision models."),
]

failures = []


def check(condition: bool, label: str) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        failures.append(label)


def seed_db() -> dict[str, str]:
    conn = db.get_connection()
    ids = {}
    for title, company, status, description in FAKE_JOBS:
        url = f"https://example.com/{company}/{title}".replace(" ", "-")
        db.insert_job(conn, {"url": url, "title": title, "company": company, "location": "Tel Aviv",
                             "description": description, "site": "linkedin"}, status=status)
        ids[f"{company}/{title}"] = db.hash_url(url)
    conn.execute("INSERT INTO meta (key, value) VALUES ('applied_count', '2')")  # Wix AI + Lightricks
    conn.commit()
    conn.close()
    return ids


def db_row(job_id: str) -> dict:
    conn = db.get_connection()
    conn.row_factory = __import__("sqlite3").Row
    row = dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
    row["_applied_count"] = db.get_applied_count(conn)
    conn.close()
    return row


def payload(result):
    """A tool's return value: structured output when the server sent it, else the text."""
    if result.structured_content is not None:
        data = result.structured_content
        return data.get("result", data) if isinstance(data, dict) and set(data) == {"result"} else data
    return json.loads(result.content[0].text) if result.content[0].text[:1] in "[{" else result.content[0].text


async def main() -> None:
    ids = seed_db()
    wix_ai, wix_backend = ids["Wix/AI Engineer"], ids["Wix/Backend Developer"]
    server = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp_server.py")],
                                   env={"JOBBOT_DB_PATH": str(TEST_DB), "JOBBOT_PROFILE_PATH": str(TEST_PROFILE)})

    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        print("Discovery (what Claude sees when it connects):")
        tools = {t.name for t in (await session.list_tools()).tools}
        expected = {"search_jobs", "get_job", "mark_applied", "move_to_process", "add_note", "get_stats",
                    "get_profile", "suggest_profile_update"}
        check(tools == expected, f"tools listed: {sorted(tools)}")
        prompts = {p.name for p in (await session.list_prompts()).prompts}
        check(prompts == {"interview_prep"}, f"prompts listed: {sorted(prompts)}")
        resources = {str(r.uri) for r in (await session.list_resources()).resources}
        check(resources == {"profile://me"}, f"resources listed: {sorted(resources)}")

        print("profile:")
        profile = payload(await session.call_tool("get_profile", {}))
        check("chess-vision" in profile, "get_profile returns the profile file")
        resource = await session.read_resource("profile://me")
        check("chess-vision" in resource.contents[0].text, "profile://me resource returns it too")

        print("suggest_profile_update:")
        await session.call_tool("suggest_profile_update", {"fact": "Used LangChain in a course", "source": "test"})
        await session.call_tool("suggest_profile_update", {"fact": "Interviewed at Wix"})
        text = TEST_PROFILE.read_text(encoding="utf-8")
        check(text.startswith("# Test Candidate\n- Built a chess-vision app"), "existing profile untouched")
        check(text.count("## Pending review") == 1, "one Pending review section")
        check("Used LangChain in a course (source: test)" in text and "Interviewed at Wix" in text,
              "both suggestions saved")
        empty = await session.call_tool("suggest_profile_update", {"fact": "  "})
        check(empty.is_error, "empty fact rejected")

        print("search_jobs:")
        wix = payload(await session.call_tool("search_jobs", {"company": "wix"}))
        check(len(wix) == 2 and wix[0]["id"] == wix_ai, "finds both Wix jobs, applied one first")
        rag = payload(await session.call_tool("search_jobs", {"keyword": "RAG"}))
        check([j["id"] for j in rag] == [wix_ai], "keyword searches the description")
        bad = await session.call_tool("search_jobs", {"status": "bogus"})
        check(bad.is_error, "invalid status is reported as an error, not a crash")

        print("get_job:")
        job = payload(await session.call_tool("get_job", {"job_id": wix_ai}))
        check("RAG" in job["description"], "returns the full description")
        missing = await session.call_tool("get_job", {"job_id": "nope"})
        check(missing.is_error and "search_jobs" in missing.content[0].text,
              "unknown id is an error whose message tells the model what to do next")

        print("move_to_process on an already-applied job:")
        await session.call_tool("move_to_process", {"job_id": wix_ai, "note": "Technical interview Wed"})
        row = db_row(wix_ai)
        check(row["status"] == "process", "status -> process")
        check("Technical interview Wed" in row["notes"], "note saved")
        check(row["_applied_count"] == 2, "applied counter unchanged (already counted)")

        print("move_to_process on a 'new' job (applied outside the dashboard):")
        await session.call_tool("move_to_process", {"job_id": wix_backend})
        check(db_row(wix_backend)["status"] == "process", "status -> process")
        check(db_row(wix_backend)["_applied_count"] == 3, "counted as applied exactly once")

        print("mark_applied is idempotent:")
        await session.call_tool("mark_applied", {"job_id": wix_backend})
        check(db_row(wix_backend)["_applied_count"] == 3, "no double count on a process job")

        print("add_note appends instead of overwriting:")
        await session.call_tool("add_note", {"job_id": wix_ai, "note": "Failed: froze on caching question"})
        notes = db_row(wix_ai)["notes"]
        check("Technical interview Wed" in notes and "froze on caching" in notes, "both notes kept")

        print("get_stats:")
        stats = payload(await session.call_tool("get_stats", {}))
        check(stats["applied_all_time"] == 3 and stats["in_process"] == 3, f"counts: {stats}")

        print("interview_prep prompt:")
        prep = await session.get_prompt("interview_prep", {"company": "Lightricks"})
        text = prep.messages[0].content.text
        check("vision models" in text, "embeds the job description")
        check("froze on caching" in text, "includes notes from other processes")
        check("chess-vision" in text, "includes the profile")
        ambiguous = (await session.get_prompt("interview_prep", {"company": "Wix"})).messages[0].content.text
        check("Ask me which one" in ambiguous, "asks which job when the company matches several")

    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED: {failures}'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    anyio.run(main)
