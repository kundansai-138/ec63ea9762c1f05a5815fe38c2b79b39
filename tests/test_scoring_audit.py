"""Independent checks of the challenge's costs, miss penalties and rounding."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scoring_audit import audit_run, cpu_microseconds, median_microseconds, rounded_score


class ScoringAuditTests(unittest.TestCase):
    def run_case(self, workers, failing):
        tests = [{"number": 1, "seconds": 2.345}, {"number": 0, "seconds": 1.234}]
        return audit_run((json.dumps({"commit": "c", "workers": workers}) + "\n").encode(), tests, {"c": {"failing": failing}})

    def test_any_number_of_missing_failures_is_one_penalty_per_commit(self):
        result = self.run_case([], [0, 1])
        self.assertEqual(result["penalty_ms"], 300_000)
        self.assertEqual(result["misses"], 1)
        self.assertEqual(result["missing_failing_tests"], 2)
        self.assertEqual(result["simulated_ms"], 300_000)

    def test_miss_adds_penalty_to_selected_worker_cost(self):
        result = self.run_case([[0]], [0, 1])
        self.assertEqual((result["load_ms"], result["overhead_ms"], result["penalty_ms"]), (1234, 2000, 300000))
        self.assertEqual(result["simulated_ms"], 303234)

    def test_empty_workers_are_free_and_numbers_are_not_list_indices(self):
        result = self.run_case([[], [0, 1], []], [0, 1])
        self.assertEqual(result["simulated_ms"], 5579)
        self.assertEqual(result["workers"], 1)
        self.assertEqual(result["selected_failing_tests"], 2)

    def test_busiest_worker_not_sum_of_workers(self):
        result = self.run_case([[0], [1]], [0, 1])
        self.assertEqual((result["load_ms"], result["overhead_ms"]), (2345, 4000))
        self.assertEqual(result["simulated_ms"], 6345)

    def test_multiple_commits_have_independent_penalties(self):
        tests = [{"number": 0, "seconds": 0.001}, {"number": 1, "seconds": 0.002}]
        answers = {"first": {"failing": [0, 1]}, "second": {"failing": [1]}}
        output = b'{"commit":"first","workers":[]}\n{"commit":"second","workers":[[0]]}\n'
        result = audit_run(output, tests, answers)
        self.assertEqual((result["commit_count"], result["misses"], result["penalty_ms"]), (2, 2, 600000))

    def test_rounding_uses_integer_microseconds_at_float_counterexample(self):
        total_us = 328955 * 1000 + cpu_microseconds(0.395)
        self.assertEqual(rounded_score(total_us), 329.4)
        self.assertEqual(rounded_score(1614100 * 1000 + 50000), 1614.2)

    def test_exact_half_rounds_up_and_neighbors_do_not(self):
        self.assertEqual(rounded_score(49999), 0.0)
        self.assertEqual(rounded_score(50000), 0.1)
        self.assertEqual(rounded_score(50001), 0.1)
        self.assertEqual(rounded_score(149999), 0.1)
        self.assertEqual(rounded_score(150000), 0.2)

    def test_median_not_mean_and_invalid_run_counts_rejected(self):
        self.assertEqual(median_microseconds([900000, 200000, 300000]), 300000)
        for values in ([], [1, 2]):
            with self.assertRaises(ValueError):
                median_microseconds(values)

    def test_duplicate_output_still_rejected_by_authoritative_parser(self):
        with self.assertRaisesRegex(ValueError, "repeated_test"):
            self.run_case([[0], [0]], [])


if __name__ == "__main__":
    unittest.main()
