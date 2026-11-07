"""The Gmail agent's run: read new mail, keep application emails, store what each is about.

fetch (gmail_client) -> pre-filter (gmail_prefilter, no AI) -> classify (gmail_classifier, one
Gemini call) -> match to a job (gmail_matcher, the LangGraph agent over the MCP server) -> store in
email_suggestions. It never changes a job: the Inbox tab shows the result and the user accepts.

Runs after each real pipeline scrape (pipeline.run), and by hand:
    venv\\Scripts\\python.exe scripts\\gmail_run.py
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from jobbot import db, gmail_suggestions
from jobbot.gmail_classifier import classify_email
from jobbot.gmail_client import get_message, get_service, list_message_ids
from jobbot.gmail_matcher import build_agent, match_email, mcp_tools
from jobbot.gmail_prefilter import skip_reason
from jobbot.paths import GMAIL_STATE_PATH, ROOT_DIR

# First run reads this far back (user decision: 60 days - current open applications)
BACKFILL_DAYS = 60
# Re-read a little before the last check, so mail that arrived while a run was finishing isn't
# missed; already-stored emails are skipped by id
_OVERLAP = timedelta(hours=1)


def run(interactive: bool = False) -> None:
    """One agent run. interactive=False (scheduled) raises instead of opening a browser if the
    Gmail login has expired. The checkpoint only moves forward after a complete run, so a crash
    means the next run retries the same window."""
    started = datetime.now(timezone.utc)
    since = _last_check() or started - timedelta(days=BACKFILL_DAYS)
    service = get_service(interactive)
    conn = db.get_connection()
    known = {r[0] for r in conn.execute("SELECT email_id FROM email_suggestions")}

    found, counts = [], {"read": 0, "skipped": 0, "unrelated": 0}
    for message_id in list_message_ids(service, since - _OVERLAP):
        if message_id in known:
            continue
        counts["read"] += 1
        msg = get_message(service, message_id)
        if skip_reason(msg):
            counts["skipped"] += 1
            continue
        msg = get_message(service, message_id, with_body=True)
        classification = classify_email(msg["sender"], msg["subject"], msg["body"])
        if classification.type == "unrelated":
            counts["unrelated"] += 1
            continue
        found.append((msg, classification))

    stored, failed = anyio.run(_match_and_store, conn, found) if found else (0, 0)
    # Confirmations of jobs that are already applied change nothing - resolve them now so the
    # Inbox only ever holds cards that need a decision
    noops = [e["email_id"] for card in gmail_suggestions.build_cards(conn) if not card.visible for e in card.emails]
    db.resolve_suggestions(conn, noops, "noop")
    conn.close()

    # Only move the checkpoint when every email made it - otherwise the next run re-reads the
    # same window (already-stored emails are skipped by id, so nothing is processed twice)
    if not failed:
        _save_last_check(started)
    print(f"Gmail agent: {counts['read']} emails read, {counts['skipped']} pre-filtered, "
          f"{counts['unrelated']} unrelated, {stored} application emails stored, {len(noops)} needed no action"
          + (f", {failed} failed (will retry next run)" if failed else ""))


async def _match_and_store(conn, found: list) -> tuple[int, int]:
    """Match every application email through one MCP server session, storing as it goes so a
    failure partway keeps what was already matched. Returns (stored, failed).

    A network blip mid-run (seen on the first backfill: httpx.ReadError inside a Gemini call) gets
    one retry; an email that still fails is skipped and reported, not allowed to sink the run."""
    server = StdioServerParameters(command=sys.executable, args=[os.path.join(ROOT_DIR, "mcp_server.py")])
    stored = failed = 0
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        agent = build_agent(mcp_tools(session, (await session.list_tools()).tools))
        for msg, c in found:
            email = {**msg, "received": msg["received"].isoformat()}
            for attempt in (1, 2):
                try:
                    result, _stats = await match_email(agent, email, c)
                    break
                except Exception as e:
                    print(f"  matching failed for '{msg['subject'][:60]}' (attempt {attempt}): {type(e).__name__}")
                    if attempt == 2:
                        result = None
                    else:
                        await anyio.sleep(10)
            if result is None:
                failed += 1
                continue
            stored += db.insert_email_suggestion(conn, {
                "email_id": msg["id"], "thread_id": msg["thread_id"], "received": msg["received"].isoformat(),
                "sender": msg["sender"], "subject": msg["subject"], "evidence": c.evidence, "type": c.type,
                "company": c.company, "role": c.role, "job_reference": c.job_reference,
                "match": result.match, "job_ids": json.dumps(result.job_ids), "match_reason": result.reason,
            })
    return stored, failed


def _last_check() -> datetime | None:
    if not GMAIL_STATE_PATH.exists():
        return None
    return datetime.fromisoformat(json.loads(GMAIL_STATE_PATH.read_text())["last_check"])


def _save_last_check(when: datetime) -> None:
    GMAIL_STATE_PATH.write_text(json.dumps({"last_check": when.isoformat()}))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    run(interactive=True)
