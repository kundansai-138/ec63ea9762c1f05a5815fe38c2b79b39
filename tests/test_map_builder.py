"""Verify that in-process baseline workers retain process-pool transport effects.

The selector itself has no external dependencies. These builder tests skip when
coverage/pytest are unavailable; run them in the map-builder environment to
exercise serialization, including sqlglot's custom expression reducer.
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from build_map import DependencyRecorder, InlineExecutor
except ModuleNotFoundError as error:
    if error.name not in {"coverage", "pytest"}:
        raise
    InlineExecutor = None
    BUILDER_SKIP = f"map-builder dependency unavailable: {error.name}"
else:
    BUILDER_SKIP = ""


EVENTS = []
INITIALIZER_VALUE = None
SHARED_RESULT = {"values": []}


def identity(value):
    return value


def append_in_worker(value):
    value.append("worker")
    return value


def aliases_are_shared(first, *, second):
    return first is second


def global_result():
    return SHARED_RESULT


def store_initializer(value):
    global INITIALIZER_VALUE
    value.append("initialized")
    INITIALIZER_VALUE = value


def rebuilt_value(value):
    EVENTS.append(("rebuild", value))
    return ReducibleValue(value)


class ReducibleValue:
    def __init__(self, value):
        self.value = value

    def __reduce__(self):
        EVENTS.append(("reduce", self.value))
        return rebuilt_value, (self.value,)


class TransportedError(Exception):
    def __reduce__(self):
        EVENTS.append(("exception-reduce", str(self)))
        return TransportedError, self.args


WORKER_ERROR = TransportedError("worker failed")


def raise_worker_error():
    raise WORKER_ERROR


def unpicklable_result():
    return lambda: None


@unittest.skipIf(InlineExecutor is None, BUILDER_SKIP)
class InlineExecutorTests(unittest.TestCase):
    def setUp(self):
        EVENTS.clear()
        SHARED_RESULT["values"].clear()

    def test_submitted_arguments_are_copied_before_worker_mutation(self):
        original = ["client"]
        with InlineExecutor() as executor:
            result = executor.submit(append_in_worker, original).result()
        self.assertEqual(original, ["client"])
        self.assertEqual(result, ["client", "worker"])
        self.assertIsNot(result, original)

    def test_returned_values_are_copied_before_client_mutation(self):
        with InlineExecutor() as executor:
            result = executor.submit(global_result).result()
        self.assertIsNot(result, SHARED_RESULT)
        self.assertIsNot(result["values"], SHARED_RESULT["values"])
        result["values"].append("client")
        self.assertEqual(SHARED_RESULT, {"values": []})

    def test_initializer_arguments_are_copied(self):
        original = ["client"]
        with InlineExecutor(initializer=store_initializer, initargs=(original,)):
            self.assertEqual(INITIALIZER_VALUE, ["client", "initialized"])
        self.assertEqual(original, ["client"])
        self.assertIsNot(INITIALIZER_VALUE, original)

    def test_one_transport_preserves_aliases_between_args_and_kwargs(self):
        shared = []
        with InlineExecutor() as executor:
            self.assertTrue(executor.submit(aliases_are_shared, shared, second=shared).result())

    def test_custom_reducer_runs_for_argument_and_result_transport(self):
        original = ReducibleValue("payload")
        with InlineExecutor() as executor:
            result = executor.submit(identity, original).result()
        self.assertEqual(result.value, "payload")
        self.assertIsNot(result, original)
        self.assertEqual(EVENTS, [
            ("reduce", "payload"), ("rebuild", "payload"),
            ("reduce", "payload"), ("rebuild", "payload"),
        ])

    def test_worker_exception_is_transported_through_future(self):
        with InlineExecutor() as executor:
            future = executor.submit(raise_worker_error)
        self.assertTrue(future.done())
        self.assertIsInstance(future.exception(), TransportedError)
        self.assertIsNot(future.exception(), WORKER_ERROR)
        self.assertEqual(EVENTS, [("exception-reduce", "worker failed")])
        with self.assertRaisesRegex(TransportedError, "worker failed"):
            future.result()

    def test_argument_serialization_failure_is_reported_by_future(self):
        with InlineExecutor() as executor:
            future = executor.submit(identity, lambda: None)
        self.assertTrue(future.done())
        self.assertIsNotNone(future.exception())
        with self.assertRaises(Exception):
            future.result()

    def test_result_serialization_failure_is_reported_by_future(self):
        with InlineExecutor() as executor:
            future = executor.submit(unpicklable_result)
        self.assertTrue(future.done())
        self.assertIsNotNone(future.exception())
        with self.assertRaises(Exception):
            future.result()

    def test_map_preserves_transport_for_every_item(self):
        inputs = [["first"], ["second"]]
        with InlineExecutor() as executor:
            results = list(executor.map(append_in_worker, inputs, chunksize=2))
        self.assertEqual(inputs, [["first"], ["second"]])
        self.assertEqual(results, [["first", "worker"], ["second", "worker"]])

    def test_expression_transport_executes_dump_and_load_in_traced_process(self):
        source = Path(os.environ.get("SQLGLOT_SOURCE", ROOT.parent.parent / "work" / "sqlglot"))
        if not (source / "sqlglot" / "expressions" / "core.py").is_file():
            self.skipTest("set SQLGLOT_SOURCE to the pinned sqlglot checkout")
        sys.path.insert(0, str(source))
        try:
            from sqlglot import exp
        finally:
            sys.path.remove(str(source))

        expression = exp.Add(this=exp.Literal.number(1), expression=exp.Literal.number(2))
        calls = []

        def profile(frame, event, arg):
            if event == "call" and frame.f_globals.get("__name__") == "sqlglot.serde":
                calls.append(frame.f_code.co_name)

        previous_profile = sys.getprofile()
        try:
            sys.setprofile(profile)
            with InlineExecutor() as executor:
                result = executor.submit(identity, expression).result()
        finally:
            sys.setprofile(previous_profile)

        self.assertEqual(result, expression)
        self.assertIsNot(result, expression)
        self.assertIsNot(result.this, expression.this)
        self.assertGreaterEqual(calls.count("dump"), 2)
        self.assertGreaterEqual(calls.count("load"), 2)

    def test_class_instrumentation_allows_instance_named_expression_argument(self):
        source = Path(os.environ.get("SQLGLOT_SOURCE", ROOT.parent.parent / "work" / "sqlglot"))
        if not (source / "sqlglot" / "expressions" / "core.py").is_file():
            self.skipTest("set SQLGLOT_SOURCE to the pinned sqlglot checkout")
        sys.path.insert(0, str(source))
        try:
            from sqlglot import expressions, generator, parser, tokens
            from sqlglot.dialects.dialect import Dialect
        finally:
            sys.path.remove(str(source))
        bases = (expressions.Expression, generator.Generator, parser.Parser, tokens.Tokenizer, Dialect)
        originals = [(base, base.__init__) for base in bases]
        try:
            recorder = DependencyRecorder(source.resolve())
            recorder.instrument_classes()
            expression = expressions.Var(instance="field named instance")
            self.assertEqual(expression.args["instance"], "field named instance")
        finally:
            for base, initializer in originals:
                base.__init__ = initializer


if __name__ == "__main__":
    unittest.main()
