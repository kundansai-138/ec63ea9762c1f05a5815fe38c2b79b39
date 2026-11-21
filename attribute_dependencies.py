"""Baseline field-read dependencies, including reads without construction."""
from __future__ import annotations

import ast
from bisect import bisect_left, bisect_right


def build_attribute_readers(files: dict) -> dict[str, list[int]]:
    readers: dict[str, set[int]] = {}
    for filename, entry in files.items():
        source = entry.get("source")
        if not filename.endswith(".py") or not source:
            continue
        line_map = entry.get("lines", {})
        lines = sorted(int(line) for line in line_map)
        if not lines:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        def visit(node, statement=None):
            if isinstance(node, ast.stmt):
                statement = node
            name = None
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                name = node.attr
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                  and node.func.id in {"getattr", "hasattr"} and len(node.args) >= 2
                  and isinstance(node.args[1], ast.Constant)
                  and isinstance(node.args[1].value, str)):
                name = node.args[1].value
            if name is not None and statement is not None:
                covered = readers.setdefault(name, set())
                # Continuation lines may not emit their own trace event. The
                # complete statement also covers short-circuited field reads.
                for line in lines[bisect_left(lines, statement.lineno):
                                  bisect_right(lines, statement.end_lineno)]:
                    covered.update(line_map[str(line)])
            for child in ast.iter_child_nodes(node):
                visit(child, statement)

        visit(tree)
    return {name: sorted(indices) for name, indices in sorted(readers.items())}
