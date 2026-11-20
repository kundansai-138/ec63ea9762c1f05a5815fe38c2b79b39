"""The scheduler is checked against the supplied scorer and small exact cases."""

import json
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scheduling import schedule
from score import evaluate_run


def cost(tests, workers):
    output = json.dumps({"commit": "test", "workers": workers}).encode()
    result = evaluate_run(output, tests, {"test": {"failing": [test["number"] for test in tests]}})
    assert result["ok"], result
    assert not result["missed_commits"], result
    return result["simulated_milliseconds"]


def make_tests(milliseconds):
    return [{"number": i, "seconds": duration / 1000} for i, duration in enumerate(milliseconds)]


def exact_cost(milliseconds):
    # Enumerate canonical partitions, not all permutations of worker labels.
    best = float("inf")

    def visit(index, loads):
        nonlocal best
        if index == len(milliseconds):
            best = min(best, max(loads, default=0) + 2000 * len(loads))
            return
        for worker in range(min(8, len(loads) + 1)):
            if worker == len(loads):
                visit(index + 1, loads + [milliseconds[index]])
            else:
                updated = loads.copy()
                updated[worker] += milliseconds[index]
                visit(index + 1, updated)

    visit(0, [])
    return best


class SchedulingTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(schedule([]), [])

    def test_small_and_zero_tests_share_one_worker(self):
        tests = make_tests([0, 1, 2, 5, 100])
        self.assertEqual(len(schedule(tests)), 1)
        self.assertEqual(cost(tests, schedule(tests)), 2108)

    def test_indivisible_long_test_does_not_buy_extra_workers(self):
        tests = make_tests([30_000, 1000, 1000, 1000])
        self.assertEqual(cost(tests, schedule(tests)), 34_000)
        self.assertEqual(len(schedule(tests)), 2)

    def test_worker_limit(self):
        tests = make_tests([100_000] * 40)
        workers = schedule(tests)
        self.assertEqual(len(workers), 8)
        self.assertEqual(cost(tests, workers), 516_000)

    def test_swaps_improve_a_known_lpt_failure(self):
        tests = make_tests([2400, 2100, 1800, 1500, 1200])
        self.assertEqual(cost(tests, schedule(tests)), 8500)

    def test_half_millisecond_rounding_matches_scorer(self):
        tests = [{"number": 10, "seconds": 0.0005}, {"number": 2, "seconds": 0.0015}]
        self.assertEqual(cost(tests, schedule(tests)), 2003)

    def test_deterministic_even_when_input_order_changes(self):
        tests = make_tests([1200, 1200, 2500, 600, 5000, 2500])
        self.assertEqual(schedule(tests), schedule(list(reversed(tests))))

    def test_small_examples_against_exhaustive_optimum(self):
        rng = random.Random(1729)
        for _ in range(100):
            durations = [rng.randint(0, 10) * 500 for _ in range(rng.randint(1, 7))]
            tests = make_tests(durations)
            with self.subTest(durations=durations):
                self.assertEqual(cost(tests, schedule(tests)), exact_cost(durations))

    def test_sample_full_suite_reaches_global_lower_bound(self):
        tests = json.loads((ROOT / "sample" / "tests.json").read_text())["tests"]
        durations = [round(test["seconds"] * 1000) for test in tests]
        lower = min(max(max(durations), (sum(durations) + k - 1) // k) + 2000 * k for k in range(1, 9))
        self.assertEqual(cost(tests, schedule(tests)), lower)


if __name__ == "__main__":
    unittest.main()
