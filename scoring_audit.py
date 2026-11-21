"""Independently account for scoring components, then cross-check the kit scorer.

This validation-only module never participates in runtime test selection.
All scoring arithmetic is integral milliseconds or microseconds.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import json

from score import evaluate_run


def cpu_microseconds(seconds: float) -> int:
    """Convert a measured user+system CPU duration to whole microseconds."""
    return int((Decimal(str(seconds)) * 1_000_000).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def rounded_score(total_microseconds: int) -> float:
    """The published half-up rule, without a floating-point boundary error."""
    return ((total_microseconds + 50_000) // 100_000) / 10


def median_microseconds(totals: list[int]) -> int:
    if not totals or len(totals) % 2 == 0:
        raise ValueError("the scoring median requires a positive odd run count")
    return sorted(totals)[len(totals) // 2]


def audit_run(stdout: bytes, tests: list[dict], answers: dict) -> dict:
    """Verify output and compute loads, overhead and one penalty per miss.

    Inputs use the provided test numbers, never list positions. The scorer is
    used for output validation and the final assertion, not for component sums.
    """
    if len(stdout) > 2_000_000:
        raise ValueError("stdout exceeded 2 MB")
    reference = evaluate_run(stdout, tests, answers)
    if not reference["ok"]:
        raise ValueError(f"Rejected: {reference['code']} on line {reference['line']}")
    durations = {
        test["number"]: int((Decimal(str(test["seconds"])) * 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        for test in tests
    }
    entries = []
    lines = stdout.decode("utf-8").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for line in lines:
        row = json.loads(line)
        workers = [worker for worker in row["workers"] if worker]
        selected = {int(number) for worker in workers for number in worker}
        failing = set(answers[row["commit"]]["failing"])
        missing = failing - selected
        entries.append({
            "commit": row["commit"],
            "load_ms": max((sum(durations[number] for number in worker) for worker in workers), default=0),
            "overhead_ms": 2_000 * len(workers),
            "penalty_ms": 300_000 if missing else 0,
            "workers": len(workers),
            "selected_tests": len(selected),
            "failing_tests": len(failing),
            "selected_failing_tests": len(failing & selected),
            "missing_failing_tests": len(missing),
        })
    totals = {key: sum(entry[key] for entry in entries) for key in (
        "load_ms", "overhead_ms", "penalty_ms", "workers", "selected_tests",
        "failing_tests", "selected_failing_tests", "missing_failing_tests",
    )}
    totals["commit_count"] = len(entries)
    totals["misses"] = sum(entry["penalty_ms"] != 0 for entry in entries)
    totals["simulated_ms"] = totals["load_ms"] + totals["overhead_ms"] + totals["penalty_ms"]
    totals["missed_commits"] = sorted(entry["commit"] for entry in entries if entry["penalty_ms"])
    if totals["simulated_ms"] != reference["simulated_milliseconds"] or totals["missed_commits"] != reference["missed_commits"]:
        raise AssertionError("independent score differs from supplied scorer")
    totals["supplied_scorer_agrees"] = True
    return totals
