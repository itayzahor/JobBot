"""Score the Gmail matching agent (jobbot.gmail_matcher) against the job matches in
qa/data/gmail_labels.json.

Matching is scored on its own: every application email gets the classifier's saved output from
qa/data/gmail_classify_results.json (run qa/gmail_eval.py first), so a classifier mistake can't
show up here as a matching mistake. The agent talks to the real mcp_server.py over stdio with
read-only tools, against the real DB - nothing is written.

Resumable: results go to qa/data/gmail_match_results.json after each email; --rerun starts over.

Scoring per label:
- job: correct if the agent answered job/ambiguous with ids that are all among the labeled ids
  (any of several duplicate rows counts)
- ambiguous: strict = answered ambiguous; acceptable = also a single job among the candidates
- not_in_db: correct if answered not_in_db

    venv\\Scripts\\python.exe qa\\gmail_match_eval.py [--limit N] [--rerun] [--only-verified]
"""

import argparse
import json
import os
import sys
from collections import Counter

import anyio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

from jobbot.gmail_classifier import EmailClassification  # noqa: E402
from jobbot.gmail_matcher import build_agent, match_email, mcp_tools  # noqa: E402

DATA = os.path.join(ROOT, "qa", "data")
RESULTS_PATH = os.path.join(DATA, "gmail_match_results.json")


async def run(args) -> None:
    emails = {e["id"]: e for e in _load("gmail_emails.json")}
    labels = _load("gmail_labels.json")
    classified = _load("gmail_classify_results.json")
    results = {} if args.rerun or not os.path.exists(RESULTS_PATH) else _load_path(RESULTS_PATH)

    # Application emails only, newest first (same order as the mailbox)
    todo = [eid for eid in emails if labels[eid]["type"] != "unrelated" and eid in classified
            and (labels[eid].get("verified") or not args.only_verified)][: args.limit]

    server = StdioServerParameters(command=sys.executable, args=[os.path.join(ROOT, "mcp_server.py")])
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        agent = build_agent(mcp_tools(session, (await session.list_tools()).tools))
        for n, eid in enumerate([e for e in todo if e not in results], 1):
            c = classified[eid]
            classification = EmailClassification(**{k: c[k] for k in EmailClassification.model_fields})
            result, stats = await match_email(agent, emails[eid], classification)
            results[eid] = {**result.model_dump(), **stats}
            with open(RESULTS_PATH, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=1)
            if n % 10 == 0:
                print(f"matched {n}")

    _report([(emails[eid], labels[eid], results[eid]) for eid in todo if eid in results])


def _verdict(label: dict, res: dict) -> str:
    """'ok', 'acceptable' (ambiguous label, agent picked one candidate) or 'wrong'."""
    expected, got, ids = label["match"], res["match"], set(res["job_ids"])
    if expected == "not_in_db":
        return "ok" if got == "not_in_db" else "wrong"
    if expected == "job":
        return "ok" if got != "not_in_db" and ids and ids <= set(label["job_ids"]) else "wrong"
    # ambiguous label
    if got == "ambiguous" and ids & set(label["job_ids"]):
        return "ok"
    return "acceptable" if got == "job" and ids and ids <= set(label["job_ids"]) else "wrong"


def _report(scored: list) -> None:
    verdicts = Counter()
    by_kind = Counter()
    print("\n=== mistakes ===")
    for e, lab, res in scored:
        v = _verdict(lab, res)
        verdicts[v] += 1
        by_kind[(lab["match"], v)] += 1
        if v == "wrong":
            print(f"{e['received'][:10]} | {e['subject'][:55]:<55} | expected {lab['match']} ({lab['company']} / {lab['role'][:30]})"
                  f" -> {res['match']} {len(res['job_ids'])} ids | {res['reason'][:110]}")
            print(f"           calls: {' ; '.join(res['calls'][:6])[:230]}")

    n = len(scored)
    print(f"\n=== {n} application emails ===")
    print(f"correct {verdicts['ok']}  acceptable {verdicts['acceptable']}  wrong {verdicts['wrong']}  "
          f"-> accuracy {(verdicts['ok'] + verdicts['acceptable']) / n:.3f}")
    for kind in ("job", "ambiguous", "not_in_db"):
        total = sum(c for (k, _), c in by_kind.items() if k == kind)
        if total:
            print(f"  {kind:<10} n={total:<4} ok={by_kind[(kind, 'ok')]} acceptable={by_kind[(kind, 'acceptable')]} wrong={by_kind[(kind, 'wrong')]}")
    calls = [len(r["calls"]) for _, _, r in scored]
    tokens = [r["tokens"] for _, _, r in scored]
    print(f"tool calls/email avg {sum(calls) / n:.1f} (max {max(calls)}), tokens/email avg {sum(tokens) // n}")


def _load(name: str):
    return _load_path(os.path.join(DATA, name))


def _load_path(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int)
    parser.add_argument("--rerun", action="store_true")
    parser.add_argument("--only-verified", action="store_true")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    anyio.run(run, args)


if __name__ == "__main__":
    main()
