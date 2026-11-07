"""Score the Gmail classifier (jobbot.gmail_classifier) against the labels in
qa/data/gmail_labels.json.

Resumable like qa/prompt_eval.py: each email's result is saved to qa/data/gmail_classify_results.json
as soon as it's classified, and already-classified emails are skipped. --rerun throws the saved
results away (after a prompt change).

Metrics, reported for verified labels only and for all labels (unverified ones are Claude's drafts):
- application recall: of the application-related emails, how many were NOT called unrelated -
  the costly miss, since an unrelated-labeled email never reaches the Inbox
- type accuracy + per-type recall/precision, and the confusion pairs
- company accuracy on application-related emails (loose match: one name contains the other)

    venv\\Scripts\\python.exe qa\\gmail_eval.py [--limit N] [--rerun] [--fast]
"""

import argparse
import json
import os
import re
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from jobbot import gmail_classifier  # noqa: E402
from jobbot.filters import gemini_filter  # noqa: E402

EMAILS_PATH = os.path.join(ROOT, "qa", "data", "gmail_emails.json")
LABELS_PATH = os.path.join(ROOT, "qa", "data", "gmail_labels.json")
RESULTS_PATH = os.path.join(ROOT, "qa", "data", "gmail_classify_results.json")
REPORT_PATH = os.path.join(ROOT, "qa", "data", "gmail_eval_report.json")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, help="only the first N emails (smoke test)")
    parser.add_argument("--rerun", action="store_true", help="discard saved results first")
    parser.add_argument("--fast", action="store_true", help="0.5s between calls (billing is on) instead of 4.5s")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if args.fast:
        gemini_filter.MIN_SECONDS_BETWEEN_CALLS = 0.5

    emails = _load(EMAILS_PATH)[: args.limit]
    labels = _load(LABELS_PATH)
    results = {} if args.rerun or not os.path.exists(RESULTS_PATH) else _load(RESULTS_PATH)

    # Classify whatever isn't scored yet, saving after each one
    todo = [e for e in emails if e["id"] not in results]
    for n, e in enumerate(todo, 1):
        parsed, usage = gmail_classifier.classify_email(e["sender"], e["subject"], e["body"], return_usage=True)
        results[e["id"]] = {**parsed.model_dump(), "tokens": usage.total_token_count}
        with open(RESULTS_PATH, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=1)
        if n % 25 == 0:
            print(f"classified {n}/{len(todo)}")

    scored = [(e, labels[e["id"]], results[e["id"]]) for e in emails if e["id"] in labels]
    report = {
        "verified": _score([s for s in scored if s[1].get("verified")]),
        "all": _score(scored),
        "avg_tokens": round(sum(r["tokens"] for _, _, r in scored) / len(scored)),
    }
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1)

    for name in ("verified", "all"):
        _print(name, report[name])
    print(f"\navg tokens/email: {report['avg_tokens']}")
    _print_mistakes(scored)


def _score(scored: list) -> dict:
    """Metrics for a list of (email, label, result) triples."""
    if not scored:
        return {"n": 0}
    app = [(lab, res) for _, lab, res in scored if lab["type"] != "unrelated"]
    confusion = Counter((lab["type"], res["type"]) for _, lab, res in scored)
    per_type = {}
    for t in sorted({lab["type"] for _, lab, _ in scored} | {res["type"] for _, _, res in scored}):
        tp = confusion[(t, t)]
        actual = sum(c for (a, _), c in confusion.items() if a == t)
        predicted = sum(c for (_, p), c in confusion.items() if p == t)
        per_type[t] = {"n": actual, "recall": _ratio(tp, actual), "precision": _ratio(tp, predicted)}
    return {
        "n": len(scored),
        "application_emails": len(app),
        "application_recall": _ratio(sum(res["type"] != "unrelated" for _, res in app), len(app)),
        "type_accuracy": _ratio(sum(lab["type"] == res["type"] for _, lab, res in scored), len(scored)),
        "company_accuracy": _ratio(sum(_same_company(lab["company"], res["company"]) for lab, res in app), len(app)),
        "per_type": per_type,
        "confusions": {f"{a} -> {p}": c for (a, p), c in confusion.most_common() if a != p},
    }


def _same_company(expected: str, got: str) -> bool:
    """Loose company match: 'Acme' vs 'Acme Software Ltd', 'acme' vs 'ACME'. A label can list
    aliases with '|' ('Acme|אקמה' - Hebrew emails name companies in Hebrew). An expected
    empty company (e.g. an Indeed notice) only matches an empty answer."""
    b = _norm(got)
    for alias in expected.split("|"):
        a = _norm(alias)
        if (not a or not b) and a == b:
            return True
        if a and b and (a in b or b in a):
            return True
    return False


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9֐-׿]+", "", (s or "").lower())


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


def _print(name: str, r: dict) -> None:
    print(f"\n=== {name} labels (n={r['n']}) ===")
    if not r["n"]:
        return
    print(f"application recall {r['application_recall']}  type accuracy {r['type_accuracy']}  "
          f"company accuracy {r['company_accuracy']}  ({r['application_emails']} application emails)")
    for t, m in r["per_type"].items():
        print(f"  {t:<19} n={m['n']:<4} recall={m['recall']}  precision={m['precision']}")
    if r["confusions"]:
        print("  confusions:", ", ".join(f"{k} x{v}" for k, v in r["confusions"].items()))


def _print_mistakes(scored: list) -> None:
    """Every wrong type and every wrong company on an application email, to read by hand."""
    print("\n=== mistakes ===")
    for e, lab, res in scored:
        wrong_type = lab["type"] != res["type"]
        wrong_company = lab["type"] != "unrelated" and not _same_company(lab["company"], res["company"])
        if wrong_type or wrong_company:
            what = f"type {lab['type']} -> {res['type']}" if wrong_type else f"company '{lab['company']}' -> '{res['company']}'"
            print(f"{e['received'][:10]} | {e['subject'][:60]:<60} | {what} | evidence: {res['evidence'][:90]}")


def _load(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    main()
