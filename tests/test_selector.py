"""Synthetic regression tests; no sample patches or answer labels are used."""

import copy
import difflib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from selector import SelectionEngine


IDS = [
    "tests/test_example.py::TestExample::test_first",
    "tests/test_example.py::TestExample::test_second",
    "tests/test_other.py::TestOther::test_other",
]
TESTS = [
    {"id": nodeid, "file": nodeid.split("::")[0], "number": i, "seconds": 0.01}
    for i, nodeid in enumerate(IDS)
]


def patch(path, before, after, context=3):
    """Produce ordinary Git-style patches without hand-maintained hunk counts."""
    return f"diff --git a/{path} b/{path}\n" + "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=context,
        )
    )


def file_data(source, lines=None, consumers=(), reads=()):
    return {
        "source": source,
        "lines": {str(line): list(tests) for line, tests in (lines or {}).items()},
        "consumers": list(consumers),
        "reads": list(reads),
    }


def mapping(files, **extra):
    return {
        "version": 1,
        "commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
        "tests": list(IDS),
        "files": files,
        "incomplete": [],
        **extra,
    }


class TestSelectionEngine(unittest.TestCase):
    def comparison_engine(self, *, reads=(), reflection=(), incomplete=(), complete=True):
        source = "def value(x):\n    return x is None\n"
        profile = {"version": 1, "commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
                   "tests": IDS, "incomplete": list(incomplete), "metadata": {"complete": complete},
                   "files": {"sqlglot/example.py": {"2:11": {"operator": "Is",
                       "values": [[["none"], ["none"], True]], "tests": {"0": [0], "1": [0]}}}}}
        return source, self.engine({"sqlglot/example.py": file_data(source, {2: [0, 1]}, reads=reads)},
                                  comparison_profile=profile, attribute_readers={"__code__": list(reflection)})

    def test_comparison_equivalence_preserves_file_and_metadata_readers(self):
        source, engine = self.comparison_engine(reads=[0], reflection=[1])
        self.assertEqual(engine.select(patch("sqlglot/example.py", source, source.replace("is None", "== None"))), {0, 1})

    def test_comparison_equivalence_removes_only_proven_complete_tests(self):
        source, engine = self.comparison_engine(incomplete=[IDS[1]])
        self.assertEqual(engine.select(patch("sqlglot/example.py", source, source.replace("is None", "== None"))), {0, 1})

    def test_incomplete_comparison_map_never_prunes(self):
        source, engine = self.comparison_engine(complete=False)
        self.assertEqual(engine.select(patch("sqlglot/example.py", source, source.replace("is None", "== None"))), {0, 1})

    def test_comparison_proof_does_not_cross_another_edited_file(self):
        source, engine = self.comparison_engine()
        other = "def other():\n    return 1\n"
        engine.files["sqlglot/other.py"] = file_data(other, {2: [0]})
        change = patch("sqlglot/example.py", source, source.replace("is None", "== None"))
        change += patch("sqlglot/other.py", other, other.replace("return 1", "return 2"))
        self.assertEqual(engine.select(change), {0, 1})

    def test_function_metadata_readers_do_not_need_to_call_changed_body(self):
        before = "def value():\n    return 7\n"
        engine = self.engine({"sqlglot/example.py": file_data(before, {2: [0]})},
                             attribute_readers={"__code__": [1]})
        self.assertEqual(engine.select(patch("sqlglot/example.py", before, before.replace("7", "8"))), {0, 1})

    def test_invalid_map_root_falls_back_to_all_tests(self):
        self.assertEqual(SelectionEngine([], TESTS).select("anything"), {0, 1, 2})

    def engine(self, files, tests=None, **extra):
        return SelectionEngine(mapping(files, **extra), tests or copy.deepcopy(TESTS))

    def test_python_comments_and_whitespace_leave_ast_unchanged(self):
        before = "def value():\n    return 7\n"
        after = "# Explain why this value is needed.\ndef value():\n    # Used by the caller.\n    return 7  # Seven\n"
        engine = self.engine({"sqlglot/example.py": file_data(before, {2: [0, 1]}, [0, 1])})
        self.assertEqual(engine.select(patch("sqlglot/example.py", before, after)), set())

    def test_changed_docstring_preserves_readers_without_function_calls(self):
        before = 'def value():\n    """old documentation"""\n    return 7\n'
        after = before.replace("old documentation", "new documentation")
        engine = self.engine({"sqlglot/example.py": file_data(before, {3: [0]}, [0, 1])})
        self.assertEqual(engine.select(patch("sqlglot/example.py", before, after)), {0, 1, 2})

    def test_comment_in_one_file_does_not_hide_fixture_change(self):
        python = "def value():\n    return 7\n"
        fixture = "SELECT 1;\nSELECT 1;\n"
        engine = self.engine(
            {
                "sqlglot/example.py": file_data(python, {2: [0]}, [0]),
                "tests/fixtures/example.sql": file_data(fixture, reads=[1]),
            }
        )
        mixed_patch = patch("sqlglot/example.py", python, "# Comment\n" + python)
        mixed_patch += patch("tests/fixtures/example.sql", fixture, "SELECT 1;\nSELECT 2;\n")
        self.assertEqual(engine.select(mixed_patch), {1})

    def test_fixture_metadata_comments_are_not_discarded(self):
        before = "# dialect: duckdb\nSELECT 1;\nSELECT 1;\n"
        after = before.replace("dialect: duckdb", "dialect: another")
        engine = self.engine({"tests/fixtures/example.sql": file_data(before, reads=[0, 1])})
        self.assertEqual(engine.select(patch("tests/fixtures/example.sql", before, after)), {0, 1})

    def test_inserted_branch_selects_enclosing_function_consumers(self):
        before = "def calculate(x):\n    if x < 0:\n        return -1\n    return x\n\ndef other():\n    return 8\n"
        after = before.replace("    if x < 0:\n", "    if x == 10:\n        raise ValueError('new path')\n    if x < 0:\n")
        engine = self.engine(
            {"sqlglot/example.py": file_data(before, {2: [0, 1], 3: [0], 4: [1], 7: [2]}, [0, 1, 2])}
        )
        self.assertTrue({0, 1}.issubset(engine.select(patch("sqlglot/example.py", before, after))))

    def test_multiline_statement_continuation_uses_statement_coverage(self):
        before = "def calculate(x):\n    return (\n        x\n        + 1\n    )\n"
        after = before.replace("+ 1", "+ 2")
        # Tracers may attribute the whole return expression to its first line.
        engine = self.engine({"sqlglot/example.py": file_data(before, {2: [1]}, [1])})
        self.assertIn(1, engine.select(patch("sqlglot/example.py", before, after)))

    def test_class_field_uses_class_provenance(self):
        before = "class Options:\n    VALUES = {\n        'width': 8,\n        'height': 9,\n    }\n"
        after = before.replace("'height': 9", "'height': 0")
        engine = self.engine(
            {"sqlglot/example.py": file_data(before, consumers=[0, 1])},
            classes={"sqlglot/example.py::Options": [1]},
        )
        self.assertIn(1, engine.select(patch("sqlglot/example.py", before, after)))

    def test_module_constant_has_conservative_consumer_fallback(self):
        before = "OPTIONS = {\n    'width': 8,\n    'height': 9,\n}\n"
        engine = self.engine({"sqlglot/example.py": file_data(before, consumers=[0, 1])})
        selected = engine.select(patch("sqlglot/example.py", before, before.replace("9", "0")))
        self.assertTrue({0, 1}.issubset(selected))

    def test_syntax_error_selects_every_test(self):
        before = "def value():\n    return 7\n"
        engine = self.engine({"sqlglot/example.py": file_data(before, {2: [0]}, [0])})
        self.assertEqual(engine.select(patch("sqlglot/example.py", before, "def value(:\n    return 7\n")), {0, 1, 2})

    def test_unknown_file_selects_every_test(self):
        engine = self.engine({})
        self.assertEqual(engine.select(patch("unknown.conf", "setting = 1\n", "setting = 2\n")), {0, 1, 2})

    def test_unknown_python_file_even_comment_change_is_conservative(self):
        engine = self.engine({})
        self.assertEqual(engine.select(patch("sqlglot/unknown.py", "x = 1\n", "# New comment\nx = 1\n")), {0, 1, 2})

    def test_rename_patch_is_conservative(self):
        engine = self.engine({"sqlglot/example.py": file_data("VALUE = 1\n", consumers=[0])})
        renamed = "diff --git a/sqlglot/example.py b/sqlglot/renamed.py\nsimilarity index 100%\nrename from sqlglot/example.py\nrename to sqlglot/renamed.py\n"
        self.assertEqual(engine.select(renamed), {0, 1, 2})

    def test_binary_patch_is_conservative(self):
        engine = self.engine({})
        binary = "diff --git a/tests/fixtures/data.bin b/tests/fixtures/data.bin\nBinary files a/tests/fixtures/data.bin and b/tests/fixtures/data.bin differ\n"
        self.assertEqual(engine.select(binary), {0, 1, 2})

    def test_malformed_hunk_is_conservative(self):
        before = "def value():\n    return 7\n"
        engine = self.engine({"sqlglot/example.py": file_data(before, {2: [0]}, [0])})
        malformed = "diff --git a/sqlglot/example.py b/sqlglot/example.py\n--- a/sqlglot/example.py\n+++ b/sqlglot/example.py\n@@ -1,2 +1,2 @@\n def not_the_original():\n-    return 7\n+    return 9\n"
        self.assertEqual(engine.select(malformed), {0, 1, 2})

    def test_current_test_numbers_are_remapped_by_node_id(self):
        before = "SELECT 1;\nSELECT 1;\n"
        reordered = [
            {**TESTS[2], "number": 0},
            {**TESTS[0], "number": 1},
            {**TESTS[1], "number": 2},
        ]
        engine = self.engine({"tests/fixtures/example.sql": file_data(before, reads=[0])}, tests=reordered)
        self.assertEqual(engine.select(patch("tests/fixtures/example.sql", before, before.replace("1", "2"))), {1})

    def test_multiple_hunks_apply_against_original_line_numbers(self):
        before = "def first():\n    return 1\n\n\n\n\n\ndef second():\n    return 2\n"
        after = "# New documentation\n" + before.replace("    return 2", "    return 3")
        engine = self.engine({"sqlglot/example.py": file_data(before, {2: [0], 9: [1]}, [0, 1])})
        selected = engine.select(patch("sqlglot/example.py", before, after, context=1))
        self.assertIn(1, selected)
        self.assertNotIn(2, selected)

    def test_deletion_before_later_hunk_preserves_its_source_location(self):
        before = "# Old documentation\ndef first():\n    return 1\n\n\n\n\n\ndef second():\n    return 2\n"
        after = before.removeprefix("# Old documentation\n").replace("    return 2", "    return 3")
        engine = self.engine({"sqlglot/example.py": file_data(before, {3: [0], 10: [1]}, [0, 1])})
        selected = engine.select(patch("sqlglot/example.py", before, after, context=1))
        self.assertIn(1, selected)
        self.assertNotIn(2, selected)

    def test_incomplete_test_is_not_silently_excluded(self):
        before = "def value():\n    return 7\n"
        engine = self.engine(
            {"sqlglot/example.py": file_data(before, {2: [0]}, [0])},
            incomplete=[IDS[2]],
        )
        self.assertIn(2, engine.select(patch("sqlglot/example.py", before, before.replace("7", "9"))))

    def test_same_engine_does_not_accumulate_previous_patch_edits(self):
        before = "def value():\n    return 7\n"
        engine = self.engine({"sqlglot/example.py": file_data(before, {2: [0]}, [0])})
        changed = patch("sqlglot/example.py", before, before.replace("7", "9"))
        harmless = patch("sqlglot/example.py", before, "# Comment\n" + before)
        first = engine.select(changed)
        self.assertIn(0, first)
        self.assertEqual(engine.select(harmless), set())
        self.assertEqual(engine.select(changed), first)

    def test_test_missing_from_map_is_preserved_for_a_code_edit(self):
        before = "def value():\n    return 7\n"
        current = copy.deepcopy(TESTS)
        current.append({"id": "tests/test_new.py::test_new", "file": "tests/test_new.py", "number": 3, "seconds": 0.01})
        engine = self.engine({"sqlglot/example.py": file_data(before, {2: [0]}, [0])}, tests=current)
        self.assertIn(3, engine.select(patch("sqlglot/example.py", before, before.replace("7", "9"))))

    def test_no_final_newline_marker_preserves_comment_only_edit(self):
        before = "VALUE = 1"
        engine = self.engine({"sqlglot/example.py": file_data(before, consumers=[0])})
        change = (
            "diff --git a/sqlglot/example.py b/sqlglot/example.py\n"
            "--- a/sqlglot/example.py\n+++ b/sqlglot/example.py\n"
            "@@ -1 +1,2 @@\n+# A comment\n VALUE = 1\n"
            "\\ No newline at end of file\n"
        )
        self.assertEqual(engine.select(change), set())

    def test_import_time_function_coverage_requires_conservative_selection(self):
        before = "def configuration():\n    return {'width': 8}\n\nOPTIONS = configuration()\n"
        data = file_data(before, consumers=[0])
        data["collection_lines"] = [1, 2, 4]
        engine = self.engine({"sqlglot/example.py": data})
        self.assertEqual(engine.select(patch("sqlglot/example.py", before, before.replace("8", "9"))), {0, 1, 2})

    def test_changed_class_annotation_can_fail_import(self):
        before = "class Options:\n    WIDTH: int = 8\n"
        engine = self.engine(
            {"sqlglot/example.py": file_data(before, consumers=[0])},
            classes={"sqlglot/example.py::Options": [0]},
        )
        after = before.replace("WIDTH: int", "WIDTH: UndefinedType")
        self.assertEqual(engine.select(patch("sqlglot/example.py", before, after)), {0, 1, 2})

    def test_invalid_class_literal_can_fail_import(self):
        for before_value, after_value in (("{1: 2}", "{[]: 2}"), ("-1", "-'x'"), ("{'x': 1}", "{**[]}")):
            with self.subTest(after_value=after_value):
                before = f"class Options:\n    VALUE = {before_value}\n"
                engine = self.engine(
                    {"sqlglot/example.py": file_data(before, consumers=[0])},
                    classes={"sqlglot/example.py::Options": [0]},
                )
                after = before.replace(before_value, after_value)
                self.assertEqual(engine.select(patch("sqlglot/example.py", before, after)), {0, 1, 2})

    def test_changed_class_unpacking_can_fail_import(self):
        before = "class Options:\n    WIDTH, HEIGHT = (8, 9)\n"
        engine = self.engine(
            {"sqlglot/example.py": file_data(before, consumers=[0])},
            classes={"sqlglot/example.py::Options": [0]},
        )
        self.assertEqual(engine.select(patch("sqlglot/example.py", before, before.replace("(8, 9)", "(8,)"))), {0, 1, 2})

    def test_generator_kind_change_affects_callers_without_iteration(self):
        for declaration in ("def", "async def"):
            with self.subTest(declaration=declaration):
                before = f"{declaration} values():\n    yield 1\n"
                engine = self.engine({"sqlglot/example.py": file_data(before, consumers=[0])})
                after = before.replace("yield 1", "return 1")
                self.assertEqual(engine.select(patch("sqlglot/example.py", before, after)), {0, 1, 2})

    def test_class_container_shape_change_can_fail_inherited_initialization(self):
        before = (
            "class Base:\n"
            "    def __init_subclass__(cls):\n"
            "        cls.first = cls.ITEMS[0]\n\n"
            "class Options(Base):\n"
            "    ITEMS = [8]\n"
        )
        data = file_data(before, consumers=[0])
        data["collection_lines"] = [1, 2, 3, 5, 6]
        engine = self.engine(
            {"sqlglot/example.py": data},
            classes={"sqlglot/example.py::Options": [0]},
        )
        self.assertEqual(engine.select(patch("sqlglot/example.py", before, before.replace("[8]", "[]"))), {0, 1, 2})


if __name__ == "__main__":
    unittest.main()
