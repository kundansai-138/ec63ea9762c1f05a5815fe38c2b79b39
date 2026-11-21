"""Independent synthetic dependency cases, with executable failure witnesses.

These do not contain sample commit IDs, sample labels, or selector lookup rules.
"""
import difflib
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from selector import SelectionEngine
from dependency_safety import unsafe_functions


PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
IDS = ["tests/test_independent.py::test_first", "tests/test_independent.py::test_later"]
TESTS = [{"id": node, "file": node.split("::")[0], "number": i, "seconds": .01}
         for i, node in enumerate(IDS)]


def patch(path, before, after):
    return f"diff --git a/{path} b/{path}\n" + "".join(difflib.unified_diff(
        before.splitlines(True), after.splitlines(True), fromfile="a/" + path, tofile="b/" + path))


def engine(source, lines, *, collection=(), classes=None, other_files=None):
    files = {"sqlglot/example.py": {"source": source,
        "lines": {str(k): v for k, v in lines.items()}, "consumers": [0, 1],
        "reads": [], "collection_lines": list(collection)}}
    files.update(other_files or {})
    return SelectionEngine({"version": 1, "commit": PIN, "tests": IDS,
                            "files": files, "classes": classes or {}, "incomplete": []}, TESTS)


class AdversarialDependencyTests(unittest.TestCase):
    def test_new_persistent_side_effect_affects_other_test_readers(self):
        before = "STATE = 7\ndef trigger():\n    return 1\ndef read():\n    return STATE\n"
        after = before.replace("    return 1", "    global STATE\n    STATE = 9\n    return 1")
        scope = {}
        exec(before, scope)
        scope["trigger"]()
        self.assertEqual(scope["read"](), 7)
        exec(after, scope)
        scope["trigger"]()
        self.assertEqual(scope["read"](), 9)
        selector = engine(before, {3: [0], 5: [1]})
        self.assertIn(1, selector.select(patch("sqlglot/example.py", before, after)))

    def test_slots_name_collision_must_cover_import_failures(self):
        before = "class Config:\n    __slots__ = ('x', 'y')\n    def __init__(self):\n        self.x = 1\n"
        after = before.replace("('x', 'y')", "('__init__', 'y')")
        exec(before, {})
        with self.assertRaises(ValueError):
            exec(after, {})
        selector = engine(before, {4: [0]}, classes={"sqlglot/example.py::Config": [0]})
        self.assertEqual(selector.select(patch("sqlglot/example.py", before, after)), {0, 1})

    def test_persistent_cached_result_affects_later_tests(self):
        before = "CACHE = {}\ndef build():\n    return 7\ndef read():\n    if 'value' not in CACHE:\n        CACHE['value'] = build()\n    return CACHE['value']\n"
        after = before.replace("return 7", "return 8")
        for source, expected in ((before, 7), (after, 8)):
            scope = {}
            exec(source, scope)
            self.assertEqual(scope["read"](), expected)  # first baseline caller
            self.assertEqual(scope["read"](), expected)  # later cached caller
        selector = engine(before, {3: [0], 5: [0, 1], 6: [0], 7: [0, 1]})
        self.assertIn(1, selector.select(patch("sqlglot/example.py", before, after)))

    def test_class_attribute_reader_needs_no_constructor(self):
        before = "class Config:\n    LIMIT = 4\ndef read():\n    return Config.LIMIT\n"
        after = before.replace("LIMIT = 4", "LIMIT = 5")
        scope = {}
        exec(before, scope)
        self.assertEqual(scope["read"](), 4)
        exec(after, scope)
        self.assertEqual(scope["read"](), 5)
        selector = engine(before, {4: [1]}, classes={"sqlglot/example.py::Config": [0]})
        self.assertIn(1, selector.select(patch("sqlglot/example.py", before, after)))

    def test_import_decorator_can_capture_unexecuted_function_code(self):
        before = "def capture(f):\n    constant = f.__code__.co_consts[1]\n    return lambda: constant\n@capture\ndef answer():\n    return 7\n"
        after = before.replace("return 7", "return 8")
        for source, expected in ((before, 7), (after, 8)):
            scope = {}
            exec(source, scope)
            self.assertEqual(scope["answer"](), expected)
        # Only the decorator's returned lambda executes during the tests. The
        # original answer body is never called, even though its code matters.
        selector = engine(before, {3: [0, 1]}, collection=[1, 2, 3, 4, 5])
        self.assertIn(1, selector.select(patch("sqlglot/example.py", before, after)))

    def test_function_default_change_remains_conservative(self):
        before = "def answer(x=7):\n    return x\n"
        after = before.replace("x=7", "x=8")
        selector = engine(before, {2: [0]})
        self.assertEqual(selector.select(patch("sqlglot/example.py", before, after)), {0, 1})

    def test_changed_nested_function_covers_saved_closure_calls(self):
        before = "def factory():\n    def inner():\n        return 7\n    return inner\n"
        after = before.replace("return 7", "return 8")
        selector = engine(before, {2: [0], 3: [1], 4: [0]})
        self.assertIn(1, selector.select(patch("sqlglot/example.py", before, after)))

    def test_new_file_read_inside_function_preserves_its_callers(self):
        before = "def value():\n    return 7\n"
        after = "def value():\n    return open('previously_unused.txt').read()\n"
        selector = engine(before, {2: [0, 1]})
        self.assertEqual(selector.select(patch("sqlglot/example.py", before, after)), {0, 1})


class PersistentStateAnalysisTests(unittest.TestCase):
    def names(self, source):
        import ast
        unsafe = unsafe_functions(source)
        return {n.name for n in ast.walk(ast.parse(source))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and (n.lineno, n.end_lineno) in unsafe}

    def test_cache_writer_and_its_producer_are_unsafe(self):
        source = "CACHE={}\ndef produce():\n    return 7\ndef read():\n    if not CACHE:\n        CACHE['v']=produce()\n    return CACHE['v']\n"
        self.assertEqual(self.names(source), {"produce", "read"})

    def test_conditional_module_state_is_still_persistent(self):
        source = "if True:\n    CACHE={}\ndef produce():\n    return 7\ndef read():\n    CACHE['v']=produce()\n"
        self.assertEqual(self.names(source), {"produce", "read"})

    def test_recursive_parameter_effects_reach_fixed_point(self):
        source = "CACHE={}\ndef recurse(first, second, depth=1):\n    second['v']=1\n    if depth:\n        recurse(second, first, 0)\ndef read():\n    recurse(CACHE, {})\n"
        self.assertEqual(self.names(source), {"recurse", "read"})

    def test_locally_imported_module_state_is_shared(self):
        source = "def trigger():\n    import os\n    os.environ['EXAMPLE']='1'\n"
        self.assertEqual(self.names(source), {"trigger"})

    def test_mutation_through_local_alias_is_persistent(self):
        source = "CACHE={}\ndef produce():\n    return 7\ndef read():\n    alias=CACHE\n    alias.update(v=produce())\n"
        self.assertEqual(self.names(source), {"produce", "read"})

    def test_helper_mutating_global_argument_propagates(self):
        source = "CACHE={}\ndef produce():\n    return 7\ndef fill(mapping):\n    mapping['v']=produce()\ndef read():\n    fill(CACHE)\n"
        self.assertEqual(self.names(source), {"produce", "fill", "read"})

    def test_function_alias_still_reaches_cached_producer(self):
        source = "CACHE={}\ndef produce():\n    return 7\nfactory=produce\ndef read():\n    build=factory\n    CACHE['v']=build()\n"
        self.assertEqual(self.names(source), {"produce", "read"})

    def test_parameter_mutation_without_shared_origin_is_not_global(self):
        source = "def fill(mapping):\n    mapping['v']=7\ndef fresh():\n    mapping={}\n    fill(mapping)\n    return mapping\n"
        self.assertEqual(self.names(source), set())

    def test_class_state_writer_and_callee_are_unsafe(self):
        source = "def produce():\n    return 7\nclass Config:\n    cache={}\n    @classmethod\n    def read(cls):\n        cls.cache['v']=produce()\n"
        self.assertEqual(self.names(source), {"produce", "read"})

    def test_mutable_default_survives_between_calls(self):
        source = "def produce():\n    return 7\ndef read(cache={}):\n    cache.setdefault('v', produce())\n    return cache['v']\n"
        self.assertEqual(self.names(source), {"produce", "read"})

    def test_imported_cache_decorator_alias_is_recognized(self):
        source = "from functools import lru_cache as remember\n@remember(None)\ndef produce():\n    return 7\n"
        self.assertEqual(self.names(source), {"produce"})

    def test_cache_factory_in_pinned_source_is_unsafe(self):
        source_root = Path(os.environ.get("SQLGLOT_SOURCE", Path(__file__).resolve().parents[1].parent.parent / "work" / "sqlglot"))
        source_path = source_root / "sqlglot" / "generator.py"
        if not source_path.is_file():
            self.skipTest("pinned source checkout is unavailable")
        self.assertIn("_build_dispatch", self.names(source_path.read_text("utf-8")))


if __name__ == "__main__":
    unittest.main()
