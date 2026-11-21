"""Synthetic executable witnesses for baseline comparison replay."""
import ast
import enum
import itertools
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from baseline_comparisons import OPERATORS, changed_comparison, observation, operand, replay, unchanged_tests
try:
    from build_comparison_map import ComparisonRecorder, CompareTransformer, HOOK
except ImportError:
    ComparisonRecorder = None


class ComparisonReplayTests(unittest.TestCase):
    def test_supported_operands_match_python_for_all_operators(self):
        first, second = object(), object()
        values = [None, True, False, 0, 1, 1.0, 0.0, -0.0, float("nan"), float("inf"),
                  1j, complex(float("nan"), 0), "x", "", b"x"]
        pairs = itertools.chain(itertools.product(values, repeat=2),
                                itertools.product([None, first, second, first], repeat=2))
        for left, right in pairs:
            record = observation(left, right)
            self.assertIsNotNone(record)
            for name, operation in OPERATORS.items():
                with self.subTest(left=repr(left), right=repr(right), op=name):
                    self.assertEqual(replay(record, name), operation(left, right))

    def test_mixed_identity_and_value_comparisons_are_conservative(self):
        class IdentityString(str):
            __eq__ = object.__eq__
            __ne__ = object.__ne__
        class IdentityInt(int):
            __eq__ = object.__eq__
            __ne__ = object.__ne__
        class IdentityBuffer(bytearray):
            __eq__ = object.__eq__
            __ne__ = object.__ne__
        # The exact builtin on the other side can still supply a reflected
        # value comparison after object.__eq__ returns NotImplemented.
        for left, right in [(IdentityString("x"), "x"), (IdentityInt(10000), 10000)]:
            self.assertIsNot(left, right)
            self.assertTrue(left == right)
            for first, second in ((left, right), (right, left)):
                # Existing raw profiles may contain these records. Runtime
                # replay, rather than only a future recorder, must reject them.
                record = observation(first, second)
                self.assertIsNotNone(record)
                for operation in ("Eq", "NotEq"):
                    with self.assertRaises(ValueError):
                        replay(record, operation)
        # Conservatively reject even currently harmless mixed object/scalar
        # pairs; raw object slots alone do not establish reverse-slot purity.
        with self.assertRaises(ValueError):
            replay(observation(object(), "x"), "Eq")
        with self.assertRaises(ValueError):
            replay(observation(IdentityBuffer(b"x"), b"x"), "Eq")
        self.assertIsNotNone(observation(object(), None))

    def test_value_equality_does_not_replace_observed_identity(self):
        left, right = int("10000"), int("10000")
        record = observation(left, right)
        self.assertFalse(replay(record, "Is"))
        self.assertTrue(replay(record, "Eq"))

    def test_scalar_enums_use_builtin_slots_only(self):
        class StringEnum(str, enum.Enum):
            A = "a"
        class IntegerEnum(enum.IntEnum):
            A = 1
        for left, right in [(StringEnum.A, "a"), (StringEnum.A, StringEnum.A), (IntegerEnum.A, 1)]:
            for name, operation in OPERATORS.items():
                self.assertEqual(replay(observation(left, right), name), operation(left, right))

    def test_custom_comparisons_are_not_executed_while_describing_values(self):
        class Dangerous:
            def __eq__(self, other):
                raise AssertionError("must not call user equality")
        class AlmostBuiltin(str):
            def __eq__(self, other):
                raise AssertionError("must not call subclass equality")
        class CustomEnum(str, enum.Enum):
            A = "a"
            def __ne__(self, other):
                return False
        self.assertIsNone(operand(Dangerous()))
        self.assertIsNone(operand(AlmostBuiltin("a")))
        self.assertIsNone(operand(CustomEnum.A))
        self.assertIsNone(operand([1]))

    def test_long_values_remain_unknown(self):
        self.assertIsNone(operand("x" * 129))

    def test_descriptor_and_metaclass_hooks_are_not_called(self):
        class Descriptor:
            def __get__(self, instance, owner):
                raise AssertionError("comparison descriptor must not execute")
        class Meta(type):
            def __getattribute__(cls, name):
                raise AssertionError("metaclass attribute hook must not execute")
            @property
            def __dict__(cls):
                raise AssertionError("metaclass dictionary property must not execute")
        class Identity(metaclass=Meta):
            pass
        class Unknown(metaclass=Meta):
            __eq__ = Descriptor()
        self.assertEqual(operand(Identity()), ("identity",))
        self.assertIsNone(operand(Unknown()))

    def test_custom_value_class_property_is_not_used_for_enum_check(self):
        class PretendEnum:
            @property
            def __class__(self):
                raise AssertionError("instance class property must not execute")
        self.assertEqual(operand(PretendEnum()), ("identity",))

    def test_only_one_simple_operator_change_is_eligible(self):
        before = "def f(x):\n    return x is None\n"
        after = before.replace("is None", "== None")
        self.assertEqual(changed_comparison(ast.parse(before), ast.parse(after)), ("2:11", "Is", "Eq"))
        rejected = [after.replace("x == None", "y == None"),
                    after + "x = 1\n", after.replace("== None", "== False"),
                    before.replace("is None", "is None is x")]
        for changed in rejected:
            self.assertIsNone(changed_comparison(ast.parse(before), ast.parse(changed)))
        twice = before + "def g(y):\n    return y is None\n"
        self.assertIsNone(changed_comparison(ast.parse(twice), ast.parse(twice.replace("is None", "== None"))))

    def test_unknown_or_changed_observations_retain_tests(self):
        before = ast.parse("def f(x):\n    return x is None\n")
        after = ast.parse("def f(x):\n    return x == None\n")
        values = [observation(None, None), observation(object(), None)]
        profile = {"2:11": {"operator": "Is", "values": values,
                            "tests": {"0": [0], "1": [1], "2": None}}}
        self.assertEqual(unchanged_tests(before, after, profile), set())
        del profile["2:11"]["tests"]["2"]
        self.assertEqual(unchanged_tests(before, after, profile), {0, 1})
        self.assertEqual(unchanged_tests(before, after, profile, incomplete=[1]), set())
        profile["2:11"]["operator"] = "Eq"
        self.assertEqual(unchanged_tests(before, after, profile), set())

    def test_an_earlier_changed_observation_can_affect_a_later_equivalent_one(self):
        source = ("CACHE = {}\n"
                  "def record(key, left, right):\n"
                  "    CACHE[key] = left is right\n"
                  "    return CACHE.get('first')\n")
        changed = source.replace("left is right", "left == right")
        first = (int("10000"), int("10000"))
        second = (None, None)
        outcomes = []
        for code in (source, changed):
            namespace = {}
            exec(compile(code, "<shared-comparison-state>", "exec"), namespace)
            namespace["record"]("first", *first)
            outcomes.append(namespace["record"]("second", *second))
        # The second comparison itself has the same result, yet its test can
        # fail because the first invocation changed state that survives it.
        self.assertEqual(outcomes, [False, True])
        second_record = observation(*second)
        self.assertEqual(replay(second_record, "Is"), replay(second_record, "Eq"))
        before, after = ast.parse(source), ast.parse(changed)
        comparison = next(node for node in ast.walk(before) if isinstance(node, ast.Compare))
        site = f"{comparison.lineno}:{comparison.col_offset}"
        profile = {site: {"operator": "Is", "values": [observation(*first), second_record],
                          "tests": {"0": [0], "1": [1]}}}
        self.assertEqual(unchanged_tests(before, after, profile), set())


@unittest.skipIf(ComparisonRecorder is None, "map builder dependencies are unavailable")
class ComparisonBuilderTests(unittest.TestCase):
    def recorder(self):
        recorder = ComparisonRecorder(Path("/tmp"), [])
        recorder.active = True
        recorder.nodes = ["tests/example.py::test_first", "tests/example.py::test_second"]
        recorder.context_tests = {node: [node] for node in recorder.nodes}
        recorder.context = recorder.nodes[0]
        return recorder

    def test_instrumentation_evaluates_operands_once_in_order(self):
        recorder = self.recorder()
        tree = ast.parse("def evaluate(left, right):\n    return left() is right()\n")
        tree = CompareTransformer(recorder, "sqlglot/example.py").visit(tree)
        ast.fix_missing_locations(tree)
        scope = {HOOK: recorder.compare}
        exec(compile(tree, "example.py", "exec"), scope)
        calls = []
        self.assertTrue(scope["evaluate"](lambda: calls.append("left"), lambda: calls.append("right")))
        self.assertEqual(calls, ["left", "right"])

    def test_original_exceptions_and_nonbool_results_are_preserved(self):
        recorder = self.recorder()
        class Raised:
            def __eq__(self, other):
                raise ValueError("original error")
        class NonBool:
            def __eq__(self, other):
                return ["original value"]
        with self.assertRaisesRegex(ValueError, "original error"):
            recorder.compare(0, "Eq", Raised(), None)
        self.assertEqual(recorder.compare(1, "Eq", NonBool(), None), ["original value"])
        self.assertIsNone(recorder.observations[0][recorder.context])
        self.assertIsNone(recorder.observations[1][recorder.context])

    def test_unsupported_replay_does_not_change_original_comparison(self):
        recorder = self.recorder()
        self.assertFalse(recorder.compare(0, "Eq", object(), "x"))
        self.assertIsNone(recorder.observations[0][recorder.context])

    def test_observation_limit_becomes_unknown(self):
        recorder = self.recorder()
        for number in range(40):
            recorder.compare(0, "Eq", number, 1)
        self.assertIsNone(recorder.observations[0][recorder.context])

    def test_shared_fixture_values_and_untraced_context_propagate(self):
        recorder = self.recorder()
        recorder.sites = [("sqlglot/example.py", "1:0", "Is")]
        recorder.context = recorder.scope_context("module", "tests/example.py")
        recorder.compare(0, "Is", None, None)
        recorder.untraced_subprocess_contexts.add(recorder.context)
        profile = recorder.profile(0, 1)
        self.assertEqual(set(profile["files"]["sqlglot/example.py"]["1:0"]["tests"]), {"0", "1"})
        self.assertEqual(profile["incomplete"], recorder.nodes)


if __name__ == "__main__":
    unittest.main()
