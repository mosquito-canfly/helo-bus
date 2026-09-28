"""Measure find_stop/plan_trip matching accuracy against data/eval_queries.json.

Read-only with respect to the matcher: this only calls the existing
gtfs.find_stop/plan_trip and reports what comes back. Ground truth in
eval_queries.json was picked from real stop names before this script ever
ran — the numbers below are not tuned to make this script pass.

Run: python scripts/evaluate.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import gtfs  # noqa: E402

QUERIES_FILE = ROOT / "data" / "eval_queries.json"
RESULTS_FILE = ROOT / "data" / "eval_results.json"


def _candidates(result: dict) -> list[str]:
    """Every stop name this find_stop result could reasonably mean, in the
    order find_stop itself ranked them."""
    names = []
    if result.get("stop"):
        names.append(result["stop"])
    names.extend(result.get("candidates", []))
    return names


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, round(pct / 100 * (len(values) - 1)))
    return values[idx]


def run_find_stop(cases: list[dict]) -> dict:
    latencies_ms = []
    first_try_hits = first_try_total = 0
    top3_hits = top3_total = 0
    none_hits = none_total = 0
    misses = []

    for case in cases:
        start = time.perf_counter()
        result = gtfs.find_stop(case["query"])
        latencies_ms.append((time.perf_counter() - start) * 1000)

        expected = case["expected"]
        names = _candidates(result)

        if expected == "none":
            none_total += 1
            correct = not result.get("ok")
            none_hits += correct
            if not correct:
                misses.append({"query": case["query"], "category": case["category"], "expected": "none", "got": names or [result.get("message", "")]})
            continue

        first_try_total += 1
        top3_total += 1
        first_try_ok = bool(names) and names[0] in expected
        top3_ok = any(n in expected for n in names[:3])
        first_try_hits += first_try_ok
        top3_hits += top3_ok
        if not top3_ok:
            misses.append({"query": case["query"], "category": case["category"], "expected": expected, "got": names or [result.get("message", "")]})

    return {
        "first_try_accuracy": round(100 * first_try_hits / first_try_total, 1) if first_try_total else None,
        "top3_accuracy": round(100 * top3_hits / top3_total, 1) if top3_total else None,
        "correct_no_match_rate": round(100 * none_hits / none_total, 1) if none_total else None,
        "latency_p50_ms": round(_percentile(latencies_ms, 50), 1),
        "latency_p95_ms": round(_percentile(latencies_ms, 95), 1),
        "total_queries": len(cases),
        "misses": misses,
    }


def run_plan_trip(cases: list[dict]) -> dict:
    latencies_ms = []
    hits = 0
    misses = []

    for case in cases:
        start = time.perf_counter()
        result = gtfs.plan_trip(case["from_stop"], case["to_stop"])
        latencies_ms.append((time.perf_counter() - start) * 1000)

        expected = case["expected"]
        # "found" only claims a route STRUCTURALLY exists — not that a live
        # bus ETA was available this instant, which is what the reasons
        # no_buses_nearby/feed_unavailable actually mean (same reasoning as
        # tests/test_plan_trip.py's own live-feed-independent assertions).
        if expected == "found":
            actual_ok = result.get("ok") or result.get("reason") in ("no_buses_nearby", "feed_unavailable", "no_service_night")
        else:
            actual_ok = result.get("reason") == expected
        hits += actual_ok
        if not actual_ok:
            misses.append({"from": case["from_stop"], "to": case["to_stop"], "expected": expected, "got": result.get("reason") or "ok"})

    return {
        "accuracy": round(100 * hits / len(cases), 1) if cases else None,
        "latency_p50_ms": round(_percentile(latencies_ms, 50), 1),
        "latency_p95_ms": round(_percentile(latencies_ms, 95), 1),
        "total_pairs": len(cases),
        "misses": misses,
    }


def main() -> None:
    gtfs._load_static()
    queries = json.loads(QUERIES_FILE.read_text(encoding="utf-8"))

    find_stop_results = run_find_stop(queries["find_stop"])
    plan_trip_results = run_plan_trip(queries["plan_trip"])

    report = {
        "first_try_accuracy": find_stop_results["first_try_accuracy"],
        "top3_accuracy": find_stop_results["top3_accuracy"],
        "correct_no_match_rate": find_stop_results["correct_no_match_rate"],
        "latency_p50_ms": find_stop_results["latency_p50_ms"],
        "latency_p95_ms": find_stop_results["latency_p95_ms"],
        "total_queries": find_stop_results["total_queries"],
        "plan_trip_pairs": plan_trip_results["total_pairs"],
        "plan_trip_accuracy": plan_trip_results["accuracy"],
        "plan_trip_latency_p50_ms": plan_trip_results["latency_p50_ms"],
        "plan_trip_latency_p95_ms": plan_trip_results["latency_p95_ms"],
        "find_stop_misses": find_stop_results["misses"],
        "plan_trip_misses": plan_trip_results["misses"],
    }

    RESULTS_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"find_stop: {find_stop_results['total_queries']} queries")
    print(f"  first-try accuracy:    {find_stop_results['first_try_accuracy']}%")
    print(f"  top-3 accuracy:        {find_stop_results['top3_accuracy']}%")
    print(f"  correct no-match rate: {find_stop_results['correct_no_match_rate']}%")
    print(f"  latency p50/p95:       {find_stop_results['latency_p50_ms']}ms / {find_stop_results['latency_p95_ms']}ms")
    print(f"plan_trip: {plan_trip_results['total_pairs']} pairs")
    print(f"  accuracy:              {plan_trip_results['accuracy']}%")
    print(f"  latency p50/p95:       {plan_trip_results['latency_p50_ms']}ms / {plan_trip_results['latency_p95_ms']}ms")
    if find_stop_results["misses"]:
        print(f"\nfind_stop misses ({len(find_stop_results['misses'])}):")
        for m in find_stop_results["misses"]:
            print(f"  [{m['category']}] {m['query']!r} expected {m['expected']} got {m['got']}")
    if plan_trip_results["misses"]:
        print(f"\nplan_trip misses ({len(plan_trip_results['misses'])}):")
        for m in plan_trip_results["misses"]:
            print(f"  {m['from']!r} -> {m['to']!r} expected {m['expected']} got {m['got']}")
    print(f"\nwrote {RESULTS_FILE}")


if __name__ == "__main__":
    main()
