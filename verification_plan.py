"""Decide which candidate tests are worth running before answering.

Running a test costs its CPU seconds once. Scheduling a test costs its recorded
seconds on a worker, plus two seconds for each worker used. Running pays off
when the tests are cheap and likely to pass: a commit whose candidates all pass
then costs nothing to schedule.

The decision uses only the candidate set, recorded durations and the baseline
cost profile. It does not look at what a patch changes beyond the coarse kind
of edit reported by the selector.
"""
from __future__ import annotations

from scheduling import WORKER_MILLISECONDS, schedule

# CPU seconds to fork a worker, build the patched tree and import sqlglot and
# one test module, in the units of the recorded durations.
START_COST = 0.2
# Extra CPU for the first import of the numerical packages some test modules need.
HEAVY_IMPORT_COST = 0.3
HEAVY_IMPORT_FILES = ("tests/test_executor.py", "tests/test_optimizer.py")

# Chance that every verified candidate passes, by kind of edit. These are rough
# priors about edits in general, not about any particular patch.
PASS_CHANCE_CODE = 0.6
PASS_CHANCE_TEXT_ONLY = 0.9

# A candidate predicted to need more CPU than this is scheduled, never run.
RUN_LIMITS = (0.25, 0.75, 2.0, 5.0)


def cost_milliseconds(tests: list[dict]) -> int:
    """Simulated cost of the best schedule found for these tests."""
    if not tests:
        return 0
    duration = {test["number"]: int(test["seconds"] * 1000 + 0.5) for test in tests}
    workers = schedule(tests)
    return max(sum(duration[n] for n in worker) for worker in workers) + WORKER_MILLISECONDS * len(workers)


class Planner:
    def __init__(self, tests: list[dict], profile: dict | None):
        self.by_number = {test["number"]: test for test in tests}
        profile = profile if isinstance(profile, dict) else {}
        self.cpu = profile.get("cpu", {}) if isinstance(profile.get("cpu", {}), dict) else {}
        self.unverifiable = set(profile.get("unverifiable", []))
        self.usable = bool(self.cpu)

    def estimate(self, number: int) -> float:
        test = self.by_number[number]
        known = self.cpu.get(test["id"])
        # An unprofiled test gets a pessimistic guess.
        return float(known) if known is not None else 2.0 * test["seconds"] + 0.05

    def plan(self, chosen: set[int], assumed_failing: set[int], text_only: bool) -> list[int]:
        """Return the test numbers to run; an empty list means run nothing.

        assumed_failing holds candidates that are expected to fail whatever the
        run shows (readers of an edited fixture), so running them saves nothing.
        """
        if not self.usable or not chosen:
            return []
        chance = PASS_CHANCE_TEXT_ONLY if text_only else PASS_CHANCE_CODE
        everything = [self.by_number[n] for n in sorted(chosen)]
        base = cost_milliseconds(everything) / 1000.0
        runnable = [n for n in sorted(chosen)
                    if n not in assumed_failing and self.by_number[n]["id"] not in self.unverifiable]
        best_value, best = 0.0, []
        for limit in RUN_LIMITS:
            run = [n for n in runnable if self.estimate(n) <= limit]
            if not run or len(run) == len(best):
                continue
            keep = chosen.difference(run)
            floor = cost_milliseconds([self.by_number[n] for n in sorted(keep)]) / 1000.0
            cpu = START_COST + sum(self.estimate(n) for n in run)
            if any(self.by_number[n]["file"] in HEAVY_IMPORT_FILES for n in run):
                cpu += HEAVY_IMPORT_COST
            value = chance * (base - floor) - cpu
            if value > best_value:
                best_value, best = value, run
        return best
