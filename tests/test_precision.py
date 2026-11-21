"""Synthetic dependency-boundary tests without sample patches or labels."""
import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selection_precision import function_lines, lambda_intervals


def function(source):
    return ast.parse(source).body[0]


def assignment(source):
    return next(node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Assign))


class StatementPrecisionTests(unittest.TestCase):
    def test_changed_leaf_retains_only_its_reachable_statement(self):
        before = "def f(x):\n    if x:\n        return 1\n    return 2\n"
        after = before.replace("return 1", "return 3")
        self.assertEqual(function_lines(function(before), function(after)), [(3, 3)])

    def test_changed_predicate_retains_compound_header_and_body(self):
        before = "def f(x):\n    if x > 0:\n        return 1\n    return 2\n"
        after = before.replace("x > 0", "x < 0")
        self.assertEqual(function_lines(function(before), function(after)), [(2, 3)])

    def test_multiline_expression_keeps_first_executed_line(self):
        before = "def f(x):\n    return (\n        x + 1\n    )\n"
        after = before.replace("x + 1", "x + 2")
        self.assertEqual(function_lines(function(before), function(after)), [(2, 4)])

    def test_insertions_retain_function_boundary(self):
        before = "def f(x):\n    return x\n"
        after = "def f(x):\n    x += 1\n    return x\n"
        self.assertIsNone(function_lines(function(before), function(after)))

    def test_insertion_inside_branch_retains_unchanged_compound_boundary(self):
        before = "def f(x):\n    if x:\n        if x > 1:\n            return 1\n    return 2\n"
        after = before.replace("            return 1", "            print(x)\n            return 1")
        self.assertEqual(function_lines(function(before), function(after)), [(3, 4)])

    def test_deletion_inside_branch_retains_unchanged_compound_boundary(self):
        before = "def f(x):\n    if x:\n        print(x)\n        return 1\n    return 2\n"
        after = before.replace("        print(x)\n", "")
        self.assertEqual(function_lines(function(before), function(after)), [(2, 4)])

    def test_new_else_suite_includes_control_header(self):
        before = "def f(x):\n    if x:\n        return 1\n    return 2\n"
        after = before.replace("    return 2", "    else:\n        raise ValueError(x)\n    return 2")
        self.assertEqual(function_lines(function(before), function(after)), [(2, 3)])

    def test_statement_kind_change_inside_branch_includes_header(self):
        before = "def f(x):\n    if x:\n        return x\n    return 2\n"
        after = before.replace("        return x", "        raise ValueError(x)")
        self.assertEqual(function_lines(function(before), function(after)), [(2, 3)])

    def test_inserted_binding_retains_function_boundary_even_inside_branch(self):
        before = "def f(x):\n    if x:\n        return 1\n    return 2\n"
        after = before.replace("        return 1", "        y = x\n        return 1")
        self.assertIsNone(function_lines(function(before), function(after)))

    def test_nested_insertion_covers_changed_executions_and_excludes_early_returns(self):
        before = "def f(x):\n    if x < 0:\n        return x\n    if x:\n        if x > 1:\n            return 1\n    return 2\n"
        after = before.replace("            return 1", "            raise ValueError(x)\n            return 1")
        intervals = function_lines(function(before), function(after))
        functions = []
        for source in (before, after):
            namespace = {}
            exec(compile(source, "<precision-execution>", "exec"), namespace)
            functions.append(namespace["f"])
        selected, changed = set(), set()
        for argument in (-1, 0, 1, 2):
            lines = set()
            def trace(frame, event, _argument):
                if frame.f_code is functions[0].__code__ and event == "line":
                    lines.add(frame.f_lineno)
                return trace
            previous = sys.gettrace()
            sys.settrace(trace)
            try:
                original = functions[0](argument)
            finally:
                sys.settrace(previous)
            try:
                modified = functions[1](argument)
            except ValueError:
                modified = object()
            if original != modified:
                changed.add(argument)
            if any(start <= line <= end for start, end in intervals for line in lines):
                selected.add(argument)
        self.assertTrue(changed <= selected)
        self.assertEqual(changed, {2})
        self.assertTrue({-1, 0}.isdisjoint(selected))

    def test_changed_binding_can_affect_earlier_unexecuted_statement(self):
        before = "def f(x):\n    print(y)\n    if x:\n        z = 1\n"
        after = before.replace("z = 1", "y = 1")
        self.assertIsNone(function_lines(function(before), function(after)))

    def test_bindings_do_not_merge_across_nested_scopes(self):
        before = "def f():\n    x = 1\n    def g():\n        y = 2\n    return g\n"
        after = "def f():\n    y = 1\n    def g():\n        x = 2\n    return g\n"
        self.assertIsNone(function_lines(function(before), function(after)))

    def test_comprehension_bindings_do_not_hide_new_outer_local(self):
        before = "def f(flag):\n    result = x\n    [x for x in []]\n    if flag:\n        y = 1\n    return result\n"
        after = "def f(flag):\n    result = x\n    [y for y in []]\n    if flag:\n        x = 1\n    return result\n"
        # Before: global x can be read with flag=False. After: the unreachable
        # assignment still makes x a local, so the earlier read fails.
        self.assertIsNone(function_lines(function(before), function(after)))

    def test_unchanged_comprehension_does_not_prevent_other_refinement(self):
        before = "def f(x):\n    values = [v for v in x]\n    return 1\n"
        after = before.replace("return 1", "return 2")
        self.assertEqual(function_lines(function(before), function(after)), [(3, 3)])

    def test_nested_generator_change_keeps_definition_event(self):
        before = "def f():\n    def g():\n        yield 1\n    return g\n"
        after = before.replace("yield 1", "return 1")
        self.assertEqual(function_lines(function(before), function(after)), [(2, 3)])

    def test_global_and_exception_bindings_retain_function_boundary(self):
        examples = [
            ("def f():\n    global x\n    x = 1\n", "def f():\n    global y\n    x = 1\n"),
            ("def f():\n    try:\n        pass\n    except Exception as x:\n        pass\n",
             "def f():\n    try:\n        pass\n    except Exception as y:\n        pass\n"),
        ]
        for before, after in examples:
            with self.subTest(before=before):
                self.assertIsNone(function_lines(function(before), function(after)))


class LambdaPrecisionTests(unittest.TestCase):
    SOURCE = 'class C:\n    TABLE = {\n        "x": lambda args: dict(\n            this=args[0],\n            other=args[1],\n        ),\n    }\n'

    def intervals(self, before, after, collection=(1, 2, 3)):
        return lambda_intervals(assignment(before), assignment(after), before, collection)

    def test_existing_multiline_body_can_be_selected_after_proven_entry(self):
        self.assertEqual(self.intervals(self.SOURCE, self.SOURCE.replace("args[0]", "args[1]")), [(3, 6)])

    def test_dictionary_key_change_is_not_a_lambda_body_change(self):
        self.assertIsNone(self.intervals(self.SOURCE, self.SOURCE.replace('"x"', '"y"')))

    def test_changed_default_argument_is_definition_time(self):
        before = self.SOURCE.replace("lambda args:", "lambda args, extra=1:")
        self.assertIsNone(self.intervals(before, before.replace("extra=1", "extra=2")))

    def test_single_line_lambda_is_ambiguous_with_collection(self):
        before = "TABLE = {'x': lambda x: x + 1}\n"
        self.assertIsNone(self.intervals(before, before.replace("+ 1", "+ 2"), (1,)))

    def test_single_line_lambda_can_use_exact_collection_call_evidence(self):
        before = "TABLE = {'x': lambda x: x + 1}\n"
        after = before.replace("+ 1", "+ 2")
        self.assertEqual(lambda_intervals(assignment(before), assignment(after), before, (1,), []), [(1, 1)])

    def test_exact_collection_lambda_call_prevents_refinement(self):
        before = "TABLE = {'x': lambda x: x + 1}\n"
        after = before.replace("+ 1", "+ 2")
        self.assertIsNone(lambda_intervals(assignment(before), assignment(after), before, (1,), [1]))

    def test_two_lambdas_on_same_line_remain_ambiguous(self):
        before = "TABLE = {'x': lambda x: x + 1, 'y': lambda x: x + 2}\n"
        after = before.replace("+ 1", "+ 3")
        self.assertIsNone(lambda_intervals(assignment(before), assignment(after), before, (1,), []))

    def test_new_shared_state_calls_are_not_refined(self):
        before = "TABLE = {'x': lambda x: x + 1}\n"
        after = before.replace("x + 1", "globals().update({'changed': x})")
        self.assertIsNone(lambda_intervals(assignment(before), assignment(after), before, (1,), []))

    def test_existing_mutator_calls_are_not_refined(self):
        before = "TABLE = {'x': lambda x: CACHE.update({'key': 1})}\n"
        after = before.replace("'key': 1", "'key': 2")
        self.assertIsNone(lambda_intervals(assignment(before), assignment(after), before, (1,), []))

    def test_changed_call_target_is_not_refined(self):
        before = "TABLE = {'x': lambda x: parse(x)}\n"
        after = before.replace("parse(x)", "cache(x)")
        self.assertIsNone(lambda_intervals(assignment(before), assignment(after), before, (1,), []))

    def test_known_persistent_callee_is_not_refined(self):
        before = "TABLE = {'x': lambda x: build_cache(x + 1)}\n"
        after = before.replace("x + 1", "x + 2")
        self.assertIsNone(lambda_intervals(assignment(before), assignment(after), before, (1,), [], {"build_cache"}))

    def test_nested_callable_factory_is_not_refined(self):
        before = "TABLE = {'x': lambda x: (lambda y: x + y + 1)}\n"
        after = before.replace("y + 1", "y + 2")
        self.assertIsNone(lambda_intervals(assignment(before), assignment(after), before, (1,), []))

    def test_observed_import_time_body_execution_is_conservative(self):
        self.assertIsNone(self.intervals(self.SOURCE, self.SOURCE.replace("args[0]", "args[1]"), (1, 2, 3, 4)))

    def test_short_circuit_does_not_prove_lambda_body_unexecuted(self):
        before = 'TABLE = {"x": lambda x: x or (\n    x + 1\n)}\n'
        # Baseline import can call lambda with true x without visiting line 2.
        self.assertIsNone(self.intervals(before, before.replace("x + 1", "x + 2"), (1,)))

    def test_first_line_expression_change_is_conservative(self):
        self.assertIsNone(self.intervals(self.SOURCE, self.SOURCE.replace("dict(", "list(")))

    def test_new_implicit_class_cell_is_conservative(self):
        self.assertIsNone(self.intervals(self.SOURCE, self.SOURCE.replace("args[0]", "__class__")))

    def test_wrapped_lambda_is_not_treated_as_uncached_dispatch(self):
        before = 'TABLE = {"x": cache(lambda args: dict(\n    this=args[0],\n))}\n'
        self.assertIsNone(self.intervals(before, before.replace("args[0]", "args[1]"), (1,)))

    def test_walrus_or_generator_change_is_conservative(self):
        for value in ("(new_name := args[0])", "(yield args[0])"):
            with self.subTest(value=value):
                self.assertIsNone(self.intervals(self.SOURCE, self.SOURCE.replace("args[0]", value)))

    def test_operator_nodes_without_line_numbers_are_supported(self):
        before = self.SOURCE.replace("args[0]", "args[0] + 1")
        self.assertEqual(self.intervals(before, before.replace("+ 1", "- 1")), [(3, 6)])


if __name__ == "__main__":
    unittest.main()
