"""Conservative AST refinements for baseline line-coverage selection.

These helpers return baseline source intervals, never test identities. They do
not read patches or labels. A None result asks the caller to retain its broader
dependency boundary. Import effects and persistent state remain caller checks.
"""
from __future__ import annotations

import ast
import copy
import dis
from functools import lru_cache
from types import CodeType
from ast_equal import ast_equal
from dependency_safety import MUTATORS


def _bindings(function):
    """Compare bindings within each lexical scope, including compiler special names."""
    names = set()

    def visit(node, scope=()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            name = getattr(node, "name", "<lambda>")
            names.add((scope, type(node).__name__, name))
            scope = scope + ((type(node).__name__, name),)
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                names.add((scope, type(node.ctx).__name__, node.id))
            elif node.id in {"super", "__class__"}:
                names.add((scope, "implicit-class-cell", node.id))
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update((scope, type(node).__name__, name) for name in node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add((scope, "except", node.name))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((scope, "import", name.asname or name.name.split(".")[0]) for name in node.names)
        elif isinstance(node, ast.arg):
            names.add((scope, "parameter", node.arg))
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(function)
    return names


def function_lines(before, after):
    """Refine a function with an unchanged signature to changed old statements.

    Top-level insertions, deletions and statement-kind changes retain whole-
    function coverage. Inside an unchanged compound, those changes retain the
    whole compound: its baseline header is evaluated before the changed suite
    could run. Lexical binding changes still retain whole-function coverage.
    A changed condition keeps its compound
    statement's header and body, including tests that took a different branch.
    Nested definitions keep the definition event, not just their changed bodies.
    """
    comprehensions = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
    # Comprehensions have their own scope, with a special outer-scope rule for
    # assignment expressions. Avoid collapsing those bindings into the parent
    # when a comprehension itself changes.
    before_comprehensions = [node for node in ast.walk(before) if isinstance(node, comprehensions)]
    after_comprehensions = [node for node in ast.walk(after) if isinstance(node, comprehensions)]
    if not ast_equal(before_comprehensions, after_comprehensions) or _bindings(before) != _bindings(after):
        return None
    ranges = []

    def compare(left, right):
        if ast_equal(left, right):
            return True
        if type(left) is not type(right):
            return False
        if isinstance(left, list):
            return len(left) == len(right) and all(compare(a, b) for a, b in zip(left, right))
        if not isinstance(left, ast.stmt):
            return False
        if isinstance(left, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            ranges.append((left.lineno, left.end_lineno))
            return True
        bodies = []
        for name, value in ast.iter_fields(left):
            other = getattr(right, name)
            if isinstance(value, list) and (not value or isinstance(value[0], ast.stmt)):
                bodies.append((value, other))
            elif not ast_equal(value, other):
                ranges.append((left.lineno, left.end_lineno))
                return True
        if not bodies:
            ranges.append((left.lineno, left.end_lineno))
            return True
        # Suite structure changes do not invalidate the unchanged enclosing
        # control-flow boundary. Keep its header as well as its body, since a
        # newly populated else/finally suite may have no old executed line.
        # The caller handles top-level function-suite changes conservatively.
        if all(compare(a, b) for a, b in bodies):
            return True
        ranges.append((left.lineno, left.end_lineno))
        return True

    return ranges if compare(before.body, after.body) else None


class _OpaqueLambdas(ast.NodeTransformer):
    def __init__(self):
        self.lambdas = []

    def visit_Lambda(self, node):
        self.lambdas.append(node)
        replacement = copy.copy(node)
        replacement.body = ast.Constant(value=None)
        return replacement


def _changed_expression_lines(before, after, owner_line=None):
    owner_line = getattr(before, "lineno", owner_line)
    if ast_equal(before, after):
        return set()
    if type(before) is not type(after):
        return {owner_line}
    lines = set()
    for name, value in ast.iter_fields(before):
        other = getattr(after, name)
        if isinstance(value, ast.AST) and isinstance(other, ast.AST):
            lines.update(_changed_expression_lines(value, other, owner_line))
        elif isinstance(value, list) and isinstance(other, list):
            if len(value) != len(other):
                lines.add(owner_line)
            else:
                for left, right in zip(value, other):
                    if isinstance(left, ast.AST) and isinstance(right, ast.AST):
                        lines.update(_changed_expression_lines(left, right, owner_line))
                    elif left != right:
                        lines.add(owner_line)
        elif value != other:
            lines.add(owner_line)
    return lines


@lru_cache(maxsize=32)
def _lambda_codes(source):
    codes = {}

    def visit(code):
        for value in code.co_consts:
            if isinstance(value, CodeType):
                if value.co_name == "<lambda>":
                    codes.setdefault(value.co_firstlineno, []).append(value)
                visit(value)

    visit(compile(source.encode("utf-8"), "<baseline>", "exec"))
    return codes


def _mandatory_interior_line(code, definition_line):
    """Find a line event on the straight-line entry path before any branch.

    Python 3.13 changed Instruction.starts_line from a line number to bool;
    positions.lineno supplies the actual number on both 3.12 and 3.13.
    """
    for instruction in dis.get_instructions(code):
        opcode = instruction.opname
        if (instruction.opcode in dis.hasjabs or instruction.opcode in dis.hasjrel
                or "JUMP" in opcode or opcode.startswith(("RETURN", "YIELD"))
                or opcode in {"FOR_ITER", "SEND", "RAISE_VARARGS", "RERAISE"}):
            return None
        if instruction.starts_line:
            line = instruction.positions.lineno
            if line is not None and line > definition_line:
                return line
    return None


def _lambda_effects_unchanged(before, after, unsafe_call_names):
    """Reject new execution paths and calls that can outlive this invocation."""
    before_calls = [node for node in ast.walk(before) if isinstance(node, ast.Call)]
    after_calls = [node for node in ast.walk(after) if isinstance(node, ast.Call)]
    if len(before_calls) != len(after_calls):
        return False
    forbidden = MUTATORS | {"setattr", "delattr", "globals", "locals", "vars", "eval", "exec", "compile", "__import__"} | set(unsafe_call_names)
    for left, right in zip(before_calls, after_calls):
        if not ast_equal(left.func, right.func):
            return False
        name = left.func.id if isinstance(left.func, ast.Name) else left.func.attr if isinstance(left.func, ast.Attribute) else None
        if name is None or name in forbidden:
            return False
        if len(left.args) != len(right.args) or [kw.arg for kw in left.keywords] != [kw.arg for kw in right.keywords]:
            return False
        if [isinstance(arg, ast.Starred) for arg in left.args] != [isinstance(arg, ast.Starred) for arg in right.args]:
            return False
    return True


def lambda_intervals(before, after, file_source, collection_lines, collection_callable_lines=None, unsafe_call_names=()):
    """Refine assignment edits confined to existing direct dispatch lambda bodies.

    Arguments, defaults, dictionary keys, targets and surrounding expressions
    must stay identical. Exact collection call events, when available, prove
    the baseline lambda did not enter its body during imports. Without that
    evidence, require a mandatory interior line absent from collection coverage
    and disallow first-line edits. Generators, walrus bindings, class-cell
    changes and ambiguous code objects retain the broader boundary.
    """
    if not isinstance(before, (ast.Assign, ast.AnnAssign)) or type(before) is not type(after):
        return None
    # Only direct dispatch-table values are supported. A lambda passed through
    # cache(...), a descriptor or another wrapper can persist effects outside
    # the tests that execute its body.
    if not isinstance(before.value, ast.Dict) or not isinstance(after.value, ast.Dict):
        return None
    direct_values = {(node.lineno, node.col_offset) for node in before.value.values if isinstance(node, ast.Lambda)}
    old_transform, new_transform = _OpaqueLambdas(), _OpaqueLambdas()
    old = old_transform.visit(copy.deepcopy(before))
    new = new_transform.visit(copy.deepcopy(after))
    if not old_transform.lambdas or len(old_transform.lambdas) != len(new_transform.lambdas) or not ast_equal(old, new):
        return None
    collection = set(collection_lines)
    codes = _lambda_codes(file_source)
    intervals = []
    for left, right in zip(old_transform.lambdas, new_transform.lambdas):
        if ast_equal(left.body, right.body):
            continue
        if (left.lineno, left.col_offset) not in direct_values:
            return None
        if _bindings(left) != _bindings(right):
            return None
        if not _lambda_effects_unchanged(left.body, right.body, unsafe_call_names):
            return None
        changes = _changed_expression_lines(left.body, right.body)
        if not changes:
            return None
        if any(isinstance(node, (ast.Yield, ast.YieldFrom, ast.NamedExpr, ast.Lambda))
               for body in (left.body, right.body) for node in ast.walk(body)):
            return None
        candidates = codes.get(left.lineno, ())
        if len(candidates) != 1:
            return None
        if collection_callable_lines is None:
            if min(changes) <= left.lineno:
                return None
            mandatory = _mandatory_interior_line(candidates[0], left.lineno)
            if mandatory is None or mandatory in collection:
                return None
            if any(left.lineno < line <= left.end_lineno for line in collection):
                return None
        elif left.lineno in collection_callable_lines:
            return None
        intervals.append((left.lineno, left.end_lineno))
    return intervals
