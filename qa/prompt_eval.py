"""Score every prompt variant in qa/prompt_variants.py against the human-labeled ground truth in
qa/data/ground_truth.json (built by qa/collect_ground_truth.py), so prompt changes can be judged
against real outcomes instead of against Gemini's own prior opinion.

Saves every (variant, job) result to qa/data/prompt_eval_results.json as soon as it's made -
same crash-safety pattern as qa/build_review.py's stage2.json - so an interrupted run loses
nothing and a re-run only fills in what's missing.

Primary metric is recall on the "should show" class (minimize false negatives - a hidden good
job is worse than a shown bad one, per this project's lean-permissive filtering stance); token
counts are the secondary/tiebreaker axis, since that's the "can we say the same thing for less"
question. A Wilson interval on recall is reported alongside so a small gap between variants can
be read as noise rather than a real difference, given the sample size actually available.

Usage:
    python qa/prompt_eval.py               # score every variant against the full ground truth
    python qa/prompt_eval.py --limit 20    # score only the first 20 labeled jobs (quick smoke test)
    python qa/prompt_eval.py --variant quote_first   # score just one variant (repeatable flag)
"""

import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from jobbot.filters.gemini_filter import GeminiQuotaExceededError, classify

from prompt_variants import VARIANTS

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
GROUND_TRUTH_PATH = os.path.join(DATA_DIR, "ground_truth.json")
RESULTS_PATH = os.path.join(DATA_DIR, "prompt_eval_results.json")
REPORT_PATH = os.path.join(DATA_DIR, "prompt_eval_report.json")


def load_ground_truth() -> list[dict]:
    with open(GROUND_TRUTH_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_results() -> dict:
    if os.path.exists(RESULTS_PATH):
        with open(RESULTS_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_results(results: dict) -> None:
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


def run_scoring(jobs: list[dict], limit: int | None, variant_names: list[str]) -> dict:
    results = load_results()
    scored = 0

    for variant_name in variant_names:
        variant = VARIANTS[variant_name]
        variant_results = results.setdefault(variant_name, {})
        for job in jobs[:limit] if limit else jobs:
            job_id = job["id"]
            existing = variant_results.get(job_id)
            if existing is not None and "error" not in existing:
                continue

            try:
                result, usage = classify(
                    job["title"],
                    job["description"],
                    verbose=True,
                    system_instruction=variant["instruction"],
                    return_usage=True,
                    judge_background=variant["judge_background"],
                    judge_flexibility=variant.get("judge_flexibility", False),
                )
                variant_results[job_id] = {
                    "fit": result.fit,
                    "years_estimate": result.years_estimate,
                    "reason": result.reason,
                    "prompt_tokens": usage.prompt_token_count,
                    "output_tokens": usage.candidates_token_count,
                    "total_tokens": usage.total_token_count,
                }
                scored += 1
                print(f"[{variant_name}] {scored} scored - {job['title']!r} -> fit={result.fit}")
            except GeminiQuotaExceededError as e:
                print(f"[warning] {e} - stopping this run, progress saved so far")
                save_results(results)
                return results
            except Exception as e:
                variant_results[job_id] = {"error": str(e)}
                print(f"[error] [{variant_name}] {job['title']!r}: {e}")

            # Unconditional, same reasoning as build_review.py: a crash on the last iteration
            # must not silently lose completed work.
            save_results(results)

    return results


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z**2 / n
    center = p + z**2 / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))
    return ((center - spread) / denom, (center + spread) / denom)


def compute_metrics(variant_results: dict, jobs: list[dict]) -> dict:
    tp = tn = fp = fn = 0
    tokens = []

    for job in jobs:
        entry = variant_results.get(job["id"])
        if entry is None or "error" in entry:
            continue
        predicted_positive = entry["fit"]
        actual_positive = job["label"]
        if predicted_positive and actual_positive:
            tp += 1
        elif predicted_positive and not actual_positive:
            fp += 1
        elif not predicted_positive and actual_positive:
            fn += 1
        else:
            tn += 1
        tokens.append(entry["total_tokens"])

    n = tp + tn + fp + fn
    recall = tp / (tp + fn) if (tp + fn) else None
    precision = tp / (tp + fp) if (tp + fp) else None
    fpr = fp / (fp + tn) if (fp + tn) else None
    accuracy = (tp + tn) / n if n else None
    recall_ci = wilson_interval(tp, tp + fn) if (tp + fn) else None

    return {
        "n": n,
        "confusion_matrix": {"tp": tp, "tn": tn, "fp": fp, "fn": fn},
        "accuracy": accuracy,
        "recall": recall,
        "recall_95pct_ci": recall_ci,
        "precision": precision,
        "false_positive_rate": fpr,
        "mean_total_tokens": sum(tokens) / len(tokens) if tokens else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="Only score the first N labeled jobs (smoke test)")
    parser.add_argument(
        "--variant",
        choices=list(VARIANTS),
        action="append",
        help="Only score this variant (repeatable); default is every variant. The report still covers "
        "every variant that already has cached results.",
    )
    args = parser.parse_args()

    jobs = load_ground_truth()
    if not jobs:
        print(f"No labeled jobs in {GROUND_TRUTH_PATH} - run qa/collect_ground_truth.py first.")
        return
    if args.limit:
        jobs = jobs[: args.limit]

    results = run_scoring(jobs, args.limit, args.variant or list(VARIANTS))

    report = {}
    for variant_name in VARIANTS:
        report[variant_name] = compute_metrics(results.get(variant_name, {}), jobs)

    ranked = sorted(
        report.items(),
        key=lambda kv: (-(kv[1]["recall"] or 0), kv[1]["mean_total_tokens"] or float("inf")),
    )

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(dict(ranked), f, ensure_ascii=False, indent=2)

    print(f"\n{'variant':<27} {'n':>4} {'recall':>8} {'95% CI':>16} {'precision':>10} {'accuracy':>9} {'avg tokens':>11}")
    for name, m in ranked:
        recall = f"{m['recall']:.2f}" if m["recall"] is not None else "n/a"
        ci = f"[{m['recall_95pct_ci'][0]:.2f},{m['recall_95pct_ci'][1]:.2f}]" if m["recall_95pct_ci"] else "n/a"
        precision = f"{m['precision']:.2f}" if m["precision"] is not None else "n/a"
        accuracy = f"{m['accuracy']:.2f}" if m["accuracy"] is not None else "n/a"
        tokens = f"{m['mean_total_tokens']:.0f}" if m["mean_total_tokens"] is not None else "n/a"
        print(f"{name:<27} {m['n']:>4} {recall:>8} {ci:>16} {precision:>10} {accuracy:>9} {tokens:>11}")

    print(f"\nFull report written to {REPORT_PATH}")


if __name__ == "__main__":
    main()
