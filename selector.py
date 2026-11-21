"""Baseline coverage selection; no sample answers or patch fingerprints.

Unsupported changes and missing evidence fall back to the complete suite.
"""
from __future__ import annotations

import argparse
import ast
import gzip
import json
import os
import re
import sys
import time
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from scheduling import schedule
from ast_equal import ast_equal
from attribute_dependencies import build_attribute_readers
from dependency_safety import unsafe_functions
from selection_precision import function_lines, lambda_intervals
from factory_dependencies import factory_intervals
from class_dependencies import class_attribute_tests
from tokenizer_dependencies import tokenizer_attribute_tests
from baseline_comparisons import changed_comparison, ordinary_function_site, unchanged_tests
from verification_plan import Planner

PINNED_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class UncertainPatch(ValueError):
    pass


@dataclass
class Edit:
    path: str
    source: str
    changed: set[int]
    inserted: set[int]


def _path(header: str) -> str:
    value = header[4:].split("\t", 1)[0]
    if not value.startswith(("a/", "b/")):
        raise UncertainPatch("unsupported path")
    value = value[2:]
    if ".." in PurePosixPath(value).parts or value.startswith("/"):
        raise UncertainPatch("unsafe path")
    return value


def apply_patch(patch: str, files: dict) -> list[Edit]:
    """Apply unified diff in memory, checking all context and hunk lengths.

    Renames, binary hunks, creations/deletions and mode changes use fallback.
    """
    lines, edits, i = patch.splitlines(keepends=True), [], 0
    while i < len(lines):
        if not lines[i].startswith("diff --git "):
            raise UncertainPatch("expected file header")
        i += 1
        if i < len(lines) and lines[i].startswith("index "):
            i += 1
        if i+1 >= len(lines) or not lines[i].startswith("--- ") or not lines[i+1].startswith("+++ "):
            raise UncertainPatch("unsupported operation")
        old_path, new_path = (_path(lines[j].rstrip("\r\n")) for j in (i, i+1))
        if old_path != new_path or old_path not in files or not isinstance(files[old_path].get("source"), str):
            raise UncertainPatch("unknown baseline file")
        original = files[old_path]["source"].splitlines(keepends=True)
        output, cursor, hunks = [], 0, 0
        changed, inserted = set(), set()
        i += 2
        while i < len(lines) and not lines[i].startswith("diff --git "):
            match = HUNK.match(lines[i])
            if not match:
                raise UncertainPatch("unsupported hunk")
            start, count, new_start, new_count = match.groups()
            start, count = int(start), int(count or 1)
            new_start, new_count = int(new_start), int(new_count or 1)
            offset = start-1 if count else start
            if offset < cursor or offset > len(original):
                raise UncertainPatch("overlapping hunk")
            output.extend(original[cursor:offset])
            cursor = offset
            if len(output) != (new_start-1 if new_count else new_start):
                raise UncertainPatch("inconsistent new position")
            seen_old = seen_new = 0
            i += 1
            while i < len(lines) and not lines[i].startswith(("@@ ", "diff --git ")):
                line = lines[i]
                if line.startswith("\\ No newline at end of file"):
                    i += 1
                    continue
                if not line or line[0] not in " +-":
                    raise UncertainPatch("invalid hunk line")
                marker, payload = line[0], line[1:].replace("\r\n", "\n")
                if i+1 < len(lines) and lines[i+1].startswith("\\ No newline at end of file"):
                    payload = payload.rstrip("\n")
                if marker in " -":
                    if cursor >= len(original) or original[cursor].replace("\r\n", "\n") != payload:
                        raise UncertainPatch("baseline context mismatch")
                    if marker == "-":
                        changed.add(cursor+1)
                    cursor += 1
                    seen_old += 1
                if marker in " +":
                    output.append(payload)
                    seen_new += 1
                if marker == "+":
                    inserted.add(cursor)
                i += 1
            if (seen_old, seen_new) != (count, new_count):
                raise UncertainPatch("incorrect hunk length")
            hunks += 1
        if not hunks:
            raise UncertainPatch("missing hunk")
        output.extend(original[cursor:])
        edits.append(Edit(old_path, "".join(output), changed, inserted))
    if not edits or len({e.path for e in edits}) != len(edits):
        raise UncertainPatch("empty or repeated diff")
    return edits


def _ast(source: str) -> ast.Module:
    # Bytes respect comment-shaped encoding declarations; compile checks scope.
    tree = ast.parse(source.encode("utf-8"))
    compile(tree, "<patch>", "exec")
    return tree


def _nested_body(left: ast.AST, right: ast.AST, edit: "Edit") -> tuple[int, int] | None:
    """Lines of a nested function's body, if the change lies entirely inside it.

    Defining a nested function runs its `def` line but not its body. When the
    only difference between two versions of a function is the body of one
    function nested inside it, only executions of that body can observe the
    change. Returns None when the change is not confined that way.
    """
    lines = sorted(n for n in edit.changed if left.lineno <= n <= left.end_lineno)
    points = sorted(n for n in edit.inserted if left.lineno <= n < left.end_lineno)
    if not lines and not points:
        return None
    low = min(lines + [p + 1 for p in points])
    high = max(lines + points)
    functions = (ast.FunctionDef, ast.AsyncFunctionDef)
    old_nodes = [n for n in ast.walk(left) if isinstance(n, functions) and n is not left]
    new_nodes = [n for n in ast.walk(right) if isinstance(n, functions) and n is not right]
    if len(old_nodes) != len(new_nodes):
        return None
    best = None
    for index, node in enumerate(old_nodes):
        if node.body[0].lineno <= low and high <= node.end_lineno:
            if best is None or node.end_lineno - node.lineno < old_nodes[best].end_lineno - old_nodes[best].lineno:
                best = index
    if best is None:
        return None
    old_node, new_node = old_nodes[best], new_nodes[best]
    old_body, new_body = old_node.body, new_node.body
    try:
        # With that one body blanked on both sides, nothing else may differ.
        old_node.body = new_node.body = [ast.Pass()]
        same = ast_equal(left, right)
    finally:
        old_node.body, new_node.body = old_body, new_body
    return (old_body[0].lineno, old_node.end_lineno) if same else None


def _literal_tree(node: ast.AST) -> bool:
    allowed = all(isinstance(n, (ast.Constant, ast.Dict, ast.List, ast.Tuple,
                              ast.Set, ast.Load, ast.UnaryOp, ast.UAdd, ast.USub))
               for n in ast.walk(node))
    if not allowed:
        return False
    try:
        ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, RecursionError):
        return False
    return True


def _literal_shape(node: ast.AST):
    """Structural changes may violate import-time metaclass assumptions."""
    def shape(value):
        if isinstance(value, dict):
            return (dict, tuple((shape(k), shape(v)) for k, v in value.items()))
        if isinstance(value, (tuple, list, set)):
            return (type(value), tuple(shape(v) for v in value))
        return type(value)
    return shape(ast.literal_eval(node))


class SelectionEngine:
    def __init__(self, mapping: dict, tests: list[dict], isolation_loader=None):
        if not isinstance(mapping, dict) or not isinstance(mapping.get("files", {}), dict) or not isinstance(mapping.get("tests", []), list):
            mapping = {}
        self.mapping, self.tests = mapping, tests
        # Loaded on first use: most changes never need the isolation map.
        self._isolation_loader = isolation_loader
        self.name_index_path = None
        self._names = None
        self._isolation = None
        self.files = mapping.get("files", {})
        self.all_tests = {t["number"] for t in tests}
        current = {t["id"]: t["number"] for t in tests}
        self.by_index = {i: current[n] for i, n in enumerate(mapping.get("tests", [])) if n in current}
        known = set(mapping.get("tests", []))
        self.uncertain = {t["number"] for t in tests if t["id"] not in known or t["id"] in mapping.get("incomplete", [])}
        self.trees = {}
        self.attribute_readers = mapping.get("attribute_readers")
        self.unsafe = {}
        profile = mapping.get("comparison_profile", {})
        self.comparisons = (profile if isinstance(profile, dict)
                            and profile.get("version") == 1
                            and profile.get("commit") == PINNED_COMMIT
                            and profile.get("tests") == mapping.get("tests")
                            and profile.get("metadata", {}).get("complete") is True else {})
        self.comparison_incomplete = {i for i, node in enumerate(mapping.get("tests", []))
                                     if node in self.comparisons.get("incomplete", [])}
        self.reflection_readers = None
        # What the last select() call learned, for the verification stage.
        self.info = {"edits": None, "assumed": set(), "text_only": False}
        self.same_ast = False

    def _numbers(self, indices) -> set[int]:
        return {self.by_index[i] for i in indices if i in self.by_index}

    def _module_scope(self, path: str, selected: set[int]) -> set[int]:
        """The widest set a change confined to one file can reach.

        A test is affected only if its own process executes the file's
        module-level code, or reads the file as data. Without that evidence the
        complete suite is returned.
        """
        isolation = self._isolation_map()
        if isolation is None:
            return self.all_tests
        importers = isolation.importers(path)
        if importers is None:
            return self.all_tests
        return selected | importers | isolation.always | self.uncertain

    def _attribute_scope(self, name: str, path: str) -> set[int] | None:
        """Tests that can observe a new literal value of a class attribute.

        A class attribute that nothing reads while classes are created can
        only matter where code reads it by name: `x.NAME`, or getattr/hasattr
        with the constant "NAME". Those statements are found in every file,
        and the isolation map gives the tests whose own process ran them,
        while importing or later. A test must also import the edited file.

        Returns None (keep the wider rule) when the map is missing, or when
        "NAME" appears as a string anywhere else, since it could then be read
        through `__dict__`, `vars()` or a computed getattr.
        """
        isolation = self._isolation_map()
        if isolation is None:
            return None
        index = self._name_index()
        if name in index["strings"]:
            return None
        importers = isolation.importers(path)
        if importers is None:
            return None
        readers = set()
        for file_path, start, end in index["reads"].get(name, ()):
            readers |= isolation.line_tests(file_path, start, end, "import")
            readers |= isolation.line_tests(file_path, start, end, "test")
        return (readers & importers) | isolation.always | self.uncertain

    def _name_index(self) -> dict:
        """Where each attribute name is read, and every string constant."""
        if getattr(self, "_names", None) is None and self.name_index_path is not None:
            # Built from the pinned sources when the image was built.
            try:
                with open(self.name_index_path, encoding="utf-8") as handle:
                    stored = json.load(handle)
                if stored.get("commit") == PINNED_COMMIT:
                    self._names = {"reads": stored["reads"], "strings": set(stored["strings"])}
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if getattr(self, "_names", None) is None:
            reads: dict[str, list] = {}
            strings: set[str] = set()
            for file_path, entry in self.files.items():
                source = entry.get("source")
                if not file_path.endswith(".py") or not isinstance(source, str):
                    continue
                if not file_path.startswith(("sqlglot/", "tests/")):
                    continue
                try:
                    tree = ast.parse(source)
                except SyntaxError:
                    continue
                for statement in ast.walk(tree):
                    if not isinstance(statement, ast.stmt):
                        continue
                    # This statement's own expressions, not nested statements.
                    pending = [child for child in ast.iter_child_nodes(statement)
                               if not isinstance(child, ast.stmt)]
                    while pending:
                        inner = pending.pop()
                        if isinstance(inner, ast.Attribute) and isinstance(inner.ctx, ast.Load):
                            reads.setdefault(inner.attr, []).append(
                                (file_path, statement.lineno, statement.end_lineno))
                        elif isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                            strings.add(inner.value)
                        pending.extend(child for child in ast.iter_child_nodes(inner)
                                       if not isinstance(child, ast.stmt))
            self._names = {"reads": reads, "strings": strings}
        return self._names

    def _isolation_map(self):
        if self._isolation is None and self._isolation_loader is not None:
            try:
                self._isolation = self._isolation_loader()
            except Exception as error:  # noqa: BLE001 - callers fall back to wider selections
                print(f"isolation map unavailable: {error!r}", file=sys.stderr)
            self._isolation_loader = None
        return self._isolation

    def _lines(self, file: dict, start: int, end: int) -> set[int]:
        return self._numbers(i for line, ids in file.get("lines", {}).items()
                             if start <= int(line) <= end for i in ids)

    def _class_selection(self, path: str, qualname: str, file: dict) -> set[int]:
        key = path + "::" + qualname
        if key in self.mapping.get("classes", {}):
            return self._numbers(self.mapping["classes"][key])
        return self._numbers(file.get("consumers", [])) or self.all_tests

    def _reflection(self) -> set[int]:
        if self.reflection_readers is None:
            if self.attribute_readers is None:
                self.attribute_readers = build_attribute_readers(self.files)
            self.reflection_readers = self._numbers(
                index for name in ("__code__", "co_consts", "__closure__", "__defaults__", "__kwdefaults__", "__doc__")
                for index in self.attribute_readers.get(name, []))
        return self.reflection_readers

    def _python(self, edit: Edit) -> set[int]:
        self.comparison_eligible = False
        self.same_ast = False
        file = self.files[edit.path]
        if edit.path not in self.trees:
            self.trees[edit.path] = _ast(file["source"])
        selected = self._numbers(file.get("reads", []))
        try:
            old, new = self.trees[edit.path], _ast(edit.source)
        except (SyntaxError, ValueError):
            # The file no longer compiles: every test that imports it breaks.
            return self._module_scope(edit.path, selected | self._reflection())
        # Reuse these already validated trees for the single-file comparison
        # refinement; compiling and parsing the same patch again adds CPU cost.
        self.comparison_trees = old, new
        if ast_equal(old, new):
            self.same_ast = True
            return selected
        # Function metadata can be inspected without entering a function body.
        # Retain every baseline reader of those reflective fields on semantic
        # Python changes, including readers reached through aliases.
        selected.update(self._reflection())
        if edit.path.startswith("tests/"):
            return self._module_scope(edit.path, selected)
        if edit.path not in self.unsafe:
            if self.mapping.get("dependency_analysis_version") == 1 and "persistent_functions" in file:
                self.unsafe[edit.path] = {tuple(bounds) for bounds in file["persistent_functions"]}
            else:
                self.unsafe[edit.path] = unsafe_functions(file["source"])
        # Patches can introduce shared-state effects even when the baseline was
        # pure; callers of the changed function are then not the only consumers.
        new_unsafe = unsafe_functions(new)

        def ordinary_decorators(function):
            if not function.decorator_list:
                return True
            names = {d.id for d in function.decorator_list if isinstance(d, ast.Name)}
            if len(names) != len(function.decorator_list) or not names <= {"property", "staticmethod", "classmethod"}:
                return False
            # These builtin descriptors do not inspect/execute the wrapped body.
            # An alias or rebinding would invalidate that assumption.
            for node in ast.walk(old):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id in names:
                    return False
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
                    return False
                if isinstance(node, (ast.Import, ast.ImportFrom)) and any((a.asname or a.name.split('.')[0]) in names for a in node.names):
                    return False
            return True
        def header(node):
            return [getattr(node, name, None) for name in node._fields if name != "body"]

        def compare_body(before, after, prefix=""):
            if len(before) != len(after):
                return False
            for left, right in zip(before, after):
                if ast_equal(left, right):
                    continue
                if type(left) is not type(right):
                    return False
                if isinstance(left, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not ast_equal(header(left), header(right)):
                        return False
                    # A docstring is stored on the function, not executed as a
                    # statement. Its readers need not call the function at all.
                    if ast.get_docstring(left, clean=False) != ast.get_docstring(right, clean=False):
                        return False
                    if not ordinary_decorators(left):
                        return False
                    if any(start <= left.end_lineno and end >= left.lineno for start, end in self.unsafe[edit.path]):
                        return False
                    if any(start <= right.end_lineno and end >= right.lineno for start, end in new_unsafe):
                        return False
                    def is_generator(function):
                        pending = list(function.body)
                        while pending:
                            node = pending.pop()
                            if isinstance(node, (ast.Yield, ast.YieldFrom)):
                                return True
                            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                                pending.extend(ast.iter_child_nodes(node))
                        return False
                    if is_generator(left) != is_generator(right):
                        return False
                    # The entire old function is the dependency boundary. This
                    # protects new control flow and multiline expressions, while
                    # AST matching ignores independent documentation hunks.
                    if "lines" not in file:
                        return False
                    isolation = self._isolation_map()
                    if isolation is None and any(left.body[0].lineno <= n <= left.end_lineno
                                                 for n in file.get("collection_lines", [])):
                        # The function ran while modules were imported and its
                        # result may live on in any test; without per-process
                        # evidence the dependency cannot be bounded.
                        return False
                    intervals = function_lines(left, right)
                    if intervals is None:
                        intervals = [(left.body[0].lineno, left.end_lineno)]
                    for start, end in intervals:
                        selected.update(self._lines(file, start, end))
                    if isolation is not None:
                        # A shared process imports a module once, during
                        # whichever test needs it first. Run alone, every test
                        # that depends on the import executes it, so these are
                        # all tests whose own process ran the changed
                        # statements, while importing or later.
                        nested = _nested_body(left, right, edit)
                        for start, end in intervals:
                            if nested is not None:
                                start, end = max(start, nested[0]), min(end, nested[1])
                            if start <= end:
                                selected.update(isolation.line_tests(edit.path, start, end, "import"))
                                selected.update(isolation.line_tests(edit.path, start, end, "test"))
                        selected.update(isolation.always)
                elif isinstance(left, ast.ClassDef):
                    if not ast_equal(header(left), header(right)):
                        return False
                    if not compare_body(left.body, right.body, prefix + left.name + "."):
                        return False
                elif isinstance(left, (ast.Assign, ast.AnnAssign)):
                    tokenizer_tests = tokenizer_attribute_tests(left, right, edit.path, prefix.rstrip("."), self.mapping)
                    if tokenizer_tests is not None:
                        selected.update(self._numbers(tokenizer_tests))
                        selected.update(self._class_selection(edit.path, prefix.rstrip("."), file))
                        continue
                    class_tests = class_attribute_tests(left, right, edit.path, prefix.rstrip("."), self.mapping)
                    if class_tests is not None:
                        selected.update(self._numbers(class_tests))
                        selected.update(self._class_selection(edit.path, prefix.rstrip("."), file))
                        continue
                    collection_calls = (file.get("collection_callable_lines")
                                        if self.mapping.get("callable_collection_version") == 1 else None)
                    unsafe_names = {node.name for node in ast.walk(old)
                                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                                    and any(start <= node.end_lineno and end >= node.lineno
                                            for start, end in self.unsafe[edit.path])}
                    intervals = lambda_intervals(left, right, file["source"], file.get("collection_lines", []),
                                                 collection_calls, unsafe_names)
                    if intervals:
                        for start, end in intervals:
                            selected.update(self._lines(file, start, end))
                        continue
                    factory_dependencies = factory_intervals(left, right, edit.path, self.files)
                    if factory_dependencies:
                        for dependency_path, start, end in factory_dependencies:
                            selected.update(self._lines(self.files[dependency_path], start, end))
                        selected.update(self._class_selection(edit.path, prefix.rstrip("."), file))
                        if self.attribute_readers is None:
                            self.attribute_readers = build_attribute_readers(self.files)
                        targets = left.targets if isinstance(left, ast.Assign) else [left.target]
                        for target in targets:
                            selected.update(self._numbers(self.attribute_readers.get(target.id, [])))
                        continue
                    if not prefix:
                        return False
                    if left.value is None or right.value is None:
                        return False
                    left_target = left.targets if isinstance(left, ast.Assign) else [left.target]
                    right_target = right.targets if isinstance(right, ast.Assign) else [right.target]
                    if not all(isinstance(n, ast.Name) for n in left_target + right_target):
                        return False
                    # type.__new__ reads these in C, outside Python coverage.
                    # Same-shaped __slots__ edits can make the module unimportable.
                    if any(n.id.startswith("__") and n.id.endswith("__") for n in left_target):
                        return False
                    if isinstance(left, ast.AnnAssign) and not ast_equal(left.annotation, right.annotation):
                        return False
                    if (not ast_equal(left_target, right_target)
                            or not _literal_tree(left.value) or not _literal_tree(right.value)):
                        return False
                    if _literal_shape(left.value) != _literal_shape(right.value):
                        return False
                    if any(n.id in self.mapping.get("collection_attributes", []) for n in left_target):
                        return False
                    narrowed = [self._attribute_scope(n.id, edit.path) for n in left_target]
                    if all(scope is not None for scope in narrowed):
                        for scope in narrowed:
                            selected.update(scope)
                        continue
                    selected.update(self._class_selection(edit.path, prefix.rstrip("."), file))
                    if self.attribute_readers is None:
                        self.attribute_readers = build_attribute_readers(self.files)
                    for target in left_target:
                        selected.update(self._numbers(self.attribute_readers.get(target.id, [])))
                else:
                    # Top-level execution and class construction can break
                    # imports. Unsupported semantic changes use the full suite.
                    return False
            return True

        if not compare_body(old.body, new.body):
            return self._module_scope(edit.path, selected)
        self.comparison_eligible = True
        return selected

    def select(self, patch: str) -> set[int]:
        self.info = {"edits": None, "assumed": set(), "text_only": False}
        try:
            if self.mapping.get("version") != 1 or self.mapping.get("commit") != PINNED_COMMIT:
                return set(self.all_tests)
            edits = apply_patch(patch, self.files)
            selected = set(self.uncertain)
            assumed, text_only = set(), True
            for edit in edits:
                if edit.path.endswith(".py"):
                    selected.update(self._python(edit))
                    text_only = text_only and self.same_ast
                else:
                    readers = self._numbers(self.files[edit.path].get("reads", []))
                    selected.update(readers or self.all_tests)
                    # A test that reads an edited data file is expected to fail.
                    assumed.update(readers)
                    text_only = False
            self.info = {"edits": edits, "assumed": assumed, "text_only": text_only}
            # Raw baseline operands can establish equivalence for a single
            # pure comparison operator substitution. Never combine that proof
            # with other edited files, unknown observations, or source readers.
            if (len(edits) == 1 and self.comparisons and edits[0].path.endswith(".py")
                    and self.comparison_eligible and not self.uncertain):
                edit = edits[0]
                file = self.files[edit.path]
                old, new = self.comparison_trees
                change = changed_comparison(old, new)
                if change and ordinary_function_site(old, change[0]):
                    proven = unchanged_tests(old, new, self.comparisons.get("files", {}).get(edit.path, {}),
                                             self.comparison_incomplete, change=change)
                    protected = self.uncertain | self._numbers(file.get("reads", [])) | self._reflection()
                    selected.difference_update(self._numbers(proven) - protected)
            return selected
        except (UncertainPatch, SyntaxError, ValueError, TypeError, KeyError, AttributeError, IndexError, RuntimeError, RecursionError):
            return set(self.all_tests)


# Fingerprinting the TPC tests costs a few CPU seconds; it is only worth it
# when dropping them saves more scheduled time than that.
FINGERPRINT_MIN_SAVING = 6.0


def _load_json(path: Path, compressed: bool):
    try:
        if compressed:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                return json.load(handle)
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError, EOFError, zlib.error):
        return None


def main():
    started = time.monotonic()
    here = Path(__file__).parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--map", type=Path, default=here / "coverage-map.json.gz")
    parser.add_argument("--tokenizer-profile", type=Path, default=here / "tokenizer-profile.json.gz")
    parser.add_argument("--profile", type=Path, default=here / "verify-profile.json")
    parser.add_argument("--isolation", type=Path, default=here / "isolation-map.json.gz")
    parser.add_argument("--source", default=os.environ.get("BLAST_SQLGLOT", "/opt/sqlglot"),
                        help="pinned sqlglot checkout used to run candidate tests")
    parser.add_argument("--no-verify", action="store_true",
                        help="answer from baseline dependencies alone")
    parser.add_argument("--wall-limit", type=float, default=150.0,
                        help="seconds after start at which verification stops")
    args = parser.parse_args()
    tests = json.loads((args.data / "tests.json").read_text("utf-8"))["tests"]
    by_number = {test["number"]: test for test in tests}
    mapping = _load_json(args.map, True)
    if mapping is None:
        mapping = {}
        print("Coverage map unavailable; using the complete suite", file=sys.stderr)
    # Supplemental evidence is stored separately to avoid duplicating the large
    # original map in Git history. The contract checks commit, test IDs and
    # completeness before any dependency refinement can use this artifact.
    if isinstance(mapping, dict):
        tokenizer_profile = _load_json(args.tokenizer_profile, True)
        if tokenizer_profile is None:
            print("Tokenizer profile unavailable; retaining existing fallbacks", file=sys.stderr)
        else:
            mapping["tokenizer_attribute_profile"] = tokenizer_profile

    def load_isolation():
        from isolation import IsolationMap

        return IsolationMap(args.isolation, tests)

    engine = SelectionEngine(mapping, tests, load_isolation)
    engine.name_index_path = here / "name-index.json"
    planner = Planner(tests, _load_json(args.profile, False))
    can_verify = (not args.no_verify and hasattr(os, "fork")
                  and os.path.isdir(os.path.join(args.source, "sqlglot"))
                  and os.path.isdir(os.path.join(args.source, "tests")))
    # Files whose checkout bytes differ from the map's baseline text: a patch
    # to one of them cannot be reproduced exactly, so it is never run.
    unverifiable_files = _load_json(Path(args.source) / ".unverifiable-files.json", False)
    if not isinstance(unverifiable_files, list):
        can_verify = False
        unverifiable_files = []

    # Stage 1: baseline dependencies name the tests each change can reach.
    decisions, jobs, infos = [], [], {}
    for patch in sorted((args.data / "commits").glob("*.patch")):
        try:
            chosen = engine.select(patch.read_text("utf-8"))
            info = engine.info
        except UnicodeError:
            chosen, info = set(engine.all_tests), {"edits": None}
        infos[patch.stem] = info
        job = None
        if (can_verify and info.get("edits") is not None
                and not any(edit.path in unverifiable_files for edit in info["edits"])):
            try:
                run = planner.plan(chosen, info["assumed"], info["text_only"])
            except Exception as error:  # noqa: BLE001 - planning must never lose an answer
                print(f"planning failed for {patch.stem}: {error!r}", file=sys.stderr)
                run = []
            if run:
                from verify import Job

                job = Job(
                    patch.stem,
                    {edit.path: edit.source.encode("utf-8") for edit in info["edits"]},
                    [(by_number[n]["id"], by_number[n]["seconds"]) for n in run],
                    sum(planner.estimate(n) for n in run),
                )
                job.numbers = run
                job.value = getattr(planner, "last_value", 0.0)
                jobs.append(job)
        decisions.append((patch.stem, chosen, job))

    # Stage 2: run the cheap candidates on the patched sources. A candidate is
    # dropped only when it ran to completion and passed.
    if jobs:
        try:
            from verify import Verifier

            verifier = Verifier(args.source, wall_deadline=started + args.wall_limit,
                                debug=bool(os.environ.get("BR_DEBUG")))
            verifier.preload({by_number[n]["file"] for job in jobs for n in job.numbers})
            verifier.run(jobs)
        except Exception as error:  # noqa: BLE001 - keep the unverified selections
            print(f"verification stopped: {error!r}", file=sys.stderr)

    explain = bool(os.environ.get("BR_EXPLAIN"))
    candidates_before = {}
    for commit, chosen, job in decisions:
        candidates_before[commit] = len(chosen)
        if job is not None:
            for number in job.numbers:
                if job.outcomes.get(by_number[number]["id"]) == "pass":
                    chosen.discard(number)

    # Stage 3: the TPC executor tests cost up to 21.5 s each. When a patch
    # leaves the executor, helper, tests and fixtures alone, fingerprint what
    # those tests compute (tpc_fingerprint.py). Equal to the pinned commit's
    # fingerprint means the same computation as there, where they pass.
    fingerprinted = {}
    if can_verify:
        try:
            import tpc_fingerprint
            from verification_plan import cost_milliseconds

            baseline = _load_json(here / "tpc-fingerprint.json", False)
            ids = {test["id"]: test["number"] for test in tests}
            tpc = {ids[name] for name in tpc_fingerprint.TESTS.values() if name in ids}
            wanted = []
            if isinstance(baseline, dict) and tpc:
                for commit, chosen, _ in decisions:
                    info = infos.get(commit) or {}
                    hit = chosen & tpc
                    if not hit or info.get("edits") is None:
                        continue
                    paths = [edit.path for edit in info["edits"]]
                    if (not tpc_fingerprint.eligible(paths)
                            or any(path in unverifiable_files for path in paths)):
                        continue
                    saving = (cost_milliseconds([by_number[n] for n in sorted(chosen)])
                              - cost_milliseconds([by_number[n] for n in sorted(chosen - hit)])) / 1000.0
                    if saving < FINGERPRINT_MIN_SAVING:
                        continue
                    # Each executed query that differs costs about 0.6 CPU s
                    # to run for real, after about 10 s of fingerprinting and
                    # loading data; stay well inside the saving.
                    budget = int(max(0.0, saving - 10.0) / 0.6)
                    wanted.append((commit, {edit.path: edit.source.encode("utf-8")
                                            for edit in info["edits"]},
                                   {by_number[n]["id"] for n in hit},
                                   {by_number[n]["id"]: min(budget, 4) for n in hit}))
            if wanted:
                # The children import these through the test module; loading
                # them once here is shared by every fork.
                for module in ("numpy", "pandas", "pandas.testing", "duckdb", "csv", "gzip",
                               "multiprocessing", "concurrent.futures.process"):
                    try:
                        __import__(module)
                    except Exception:  # noqa: BLE001 - the child meets the same error
                        pass
                found = tpc_fingerprint.run_jobs(args.source, wanted, "/tmp/br-fp",
                                                 started + args.wall_limit,
                                                 expected=baseline.get("_parts"))
                for commit, chosen, _ in decisions:
                    hashes = found.get(commit)
                    if not hashes:
                        continue
                    same = {ids[name] for name, value in hashes.items()
                            if name in ids and isinstance(value, str)
                            and (value == "pass" or (not value.startswith("error")
                                                     and value == baseline.get(name)))}
                    fingerprinted[commit] = len(chosen & same)
                    chosen.difference_update(same)
        except Exception as error:  # noqa: BLE001 - keep the selections
            print(f"fingerprinting stopped: {error!r}", file=sys.stderr)

    cache, total, ran, dropped = {}, 0, 0, 0
    for commit, chosen, job in decisions:
        candidates = candidates_before[commit]
        if job is not None:
            ran += len(job.numbers)
            dropped += sum(1 for n in job.numbers if job.outcomes.get(by_number[n]["id"]) == "pass")
        if explain:
            print(json.dumps({
                "explain": commit, "candidates": candidates, "final": len(chosen),
                "run": [by_number[n]["id"] for n in job.numbers] if job else [],
                "outcomes": job.outcomes if job else {},
                "cpu": round(sum(job.cpu.values()), 3) if job else 0.0,
                "fingerprint_dropped": fingerprinted.get(commit),
            }, separators=(",", ":")), file=sys.stderr)
        key = frozenset(chosen)
        if key not in cache:
            cache[key] = schedule([t for t in tests if t["number"] in chosen])
        print(json.dumps({"commit": commit, "workers": cache[key]}, separators=(",", ":")))
        total += len(chosen)
    sys.stdout.flush()
    print(f"Selected {total} test executions; {len(cache)} distinct selections; "
          f"ran {ran} candidates in {len(jobs)} commits and dropped {dropped}", file=sys.stderr)
    usage = os.times()
    print(json.dumps({"selector_cpu_seconds": time.process_time(),
                      "children_cpu_seconds": usage.children_user + usage.children_system,
                      "wall_seconds": time.monotonic() - started}), file=sys.stderr)


if __name__ == "__main__":
    main()
