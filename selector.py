"""Baseline coverage selection; no sample answers or patch fingerprints.

Unsupported changes and missing evidence fall back to the complete suite.
"""
from __future__ import annotations

import argparse
import ast
import gzip
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from scheduling import schedule
from attribute_dependencies import build_attribute_readers
from dependency_safety import unsafe_functions
from selection_precision import function_lines, lambda_intervals

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
    def __init__(self, mapping: dict, tests: list[dict]):
        if not isinstance(mapping, dict) or not isinstance(mapping.get("files", {}), dict) or not isinstance(mapping.get("tests", []), list):
            mapping = {}
        self.mapping, self.tests = mapping, tests
        self.files = mapping.get("files", {})
        self.all_tests = {t["number"] for t in tests}
        current = {t["id"]: t["number"] for t in tests}
        self.by_index = {i: current[n] for i, n in enumerate(mapping.get("tests", [])) if n in current}
        known = set(mapping.get("tests", []))
        self.uncertain = {t["number"] for t in tests if t["id"] not in known or t["id"] in mapping.get("incomplete", [])}
        self.trees = {}
        self.attribute_readers = mapping.get("attribute_readers")
        self.unsafe = {}

    def _numbers(self, indices) -> set[int]:
        return {self.by_index[i] for i in indices if i in self.by_index}

    def _lines(self, file: dict, start: int, end: int) -> set[int]:
        return self._numbers(i for line, ids in file.get("lines", {}).items()
                             if start <= int(line) <= end for i in ids)

    def _class_selection(self, path: str, qualname: str, file: dict) -> set[int]:
        key = path + "::" + qualname
        if key in self.mapping.get("classes", {}):
            return self._numbers(self.mapping["classes"][key])
        return self._numbers(file.get("consumers", [])) or self.all_tests

    def _python(self, edit: Edit) -> set[int]:
        file = self.files[edit.path]
        if edit.path not in self.trees:
            self.trees[edit.path] = _ast(file["source"])
        old, new = self.trees[edit.path], _ast(edit.source)
        selected = self._numbers(file.get("reads", []))
        if ast.dump(old, include_attributes=False) == ast.dump(new, include_attributes=False):
            return selected
        if edit.path.startswith("tests/"):
            return self.all_tests
        if edit.path not in self.unsafe:
            if self.mapping.get("dependency_analysis_version") == 1 and "persistent_functions" in file:
                self.unsafe[edit.path] = {tuple(bounds) for bounds in file["persistent_functions"]}
            else:
                self.unsafe[edit.path] = unsafe_functions(file["source"])
        # Patches can introduce shared-state effects even when the baseline was
        # pure; callers of the changed function are then not the only consumers.
        new_unsafe = unsafe_functions(edit.source)

        def ordinary_decorators(function):
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
            import copy
            node = copy.copy(node)
            node.body = []
            return ast.dump(node, include_attributes=False)

        def compare_body(before, after, prefix=""):
            if len(before) != len(after):
                return False
            for left, right in zip(before, after):
                if ast.dump(left, include_attributes=False) == ast.dump(right, include_attributes=False):
                    continue
                if type(left) is not type(right):
                    return False
                if isinstance(left, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if header(left) != header(right):
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
                    if any(left.body[0].lineno <= n <= left.end_lineno
                           for n in file.get("collection_lines", [])):
                        return False
                    intervals = function_lines(left, right)
                    if intervals is None:
                        intervals = [(left.body[0].lineno, left.end_lineno)]
                    for start, end in intervals:
                        selected.update(self._lines(file, start, end))
                elif isinstance(left, ast.ClassDef):
                    if header(left) != header(right):
                        return False
                    if not compare_body(left.body, right.body, prefix + left.name + "."):
                        return False
                elif isinstance(left, (ast.Assign, ast.AnnAssign)):
                    intervals = lambda_intervals(left, right, file["source"], file.get("collection_lines", []))
                    if intervals:
                        for start, end in intervals:
                            selected.update(self._lines(file, start, end))
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
                    if isinstance(left, ast.AnnAssign) and ast.dump(left.annotation) != ast.dump(right.annotation):
                        return False
                    if ([ast.dump(n) for n in left_target] != [ast.dump(n) for n in right_target]
                            or not _literal_tree(left.value) or not _literal_tree(right.value)):
                        return False
                    if _literal_shape(left.value) != _literal_shape(right.value):
                        return False
                    if any(n.id in self.mapping.get("collection_attributes", []) for n in left_target):
                        return False
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
            return self.all_tests
        return selected

    def select(self, patch: str) -> set[int]:
        try:
            if self.mapping.get("version") != 1 or self.mapping.get("commit") != PINNED_COMMIT:
                return set(self.all_tests)
            edits = apply_patch(patch, self.files)
            selected = set(self.uncertain)
            for edit in edits:
                if edit.path.endswith(".py"):
                    selected.update(self._python(edit))
                else:
                    readers = self._numbers(self.files[edit.path].get("reads", []))
                    selected.update(readers or self.all_tests)
            return selected
        except (UncertainPatch, SyntaxError, ValueError, TypeError, KeyError, AttributeError, IndexError, RuntimeError, RecursionError):
            return set(self.all_tests)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--map", type=Path, default=Path(__file__).with_name("coverage-map.json.gz"))
    args = parser.parse_args()
    tests = json.loads((args.data / "tests.json").read_text("utf-8"))["tests"]
    try:
        with gzip.open(args.map, "rt", encoding="utf-8") as handle:
            mapping = json.load(handle)
    except (OSError, ValueError):
        mapping = {}
        print("Coverage map unavailable; using the complete suite", file=sys.stderr)
    engine, cache, total = SelectionEngine(mapping, tests), {}, 0
    for patch in sorted((args.data / "commits").glob("*.patch")):
        try:
            chosen = engine.select(patch.read_text("utf-8"))
        except UnicodeError:
            chosen = engine.all_tests
        key = frozenset(chosen)
        if key not in cache:
            cache[key] = schedule([t for t in tests if t["number"] in chosen])
        print(json.dumps({"commit": patch.stem, "workers": cache[key]}, separators=(",", ":")))
        total += len(chosen)
    print(f"Selected {total} test executions; {len(cache)} distinct selections", file=sys.stderr)
    print(json.dumps({"selector_cpu_seconds": time.process_time()}), file=sys.stderr)


if __name__ == "__main__":
    main()
