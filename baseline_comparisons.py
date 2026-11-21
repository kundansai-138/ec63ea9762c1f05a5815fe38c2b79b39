"""Replay pure comparisons over raw observations from the unchanged baseline.

The artifact contains operands, identities and test contexts, never results for
proposed edits. Unsupported values or syntax keep the original dependency set.
"""
from __future__ import annotations

import ast
import enum
import operator

from ast_equal import ast_equal

OPERATORS = {"Is": operator.is_, "IsNot": operator.is_not,
             "Eq": operator.eq, "NotEq": operator.ne}
MAX_VALUE_LENGTH = 128
_MRO_DESCRIPTOR = type.__dict__["__mro__"]
_DICT_DESCRIPTOR = type.__dict__["__dict__"]


def _raw_slots(cls):
    # Use type's own descriptors, not a custom metaclass or a comparison
    # descriptor's __get__. Inspecting evidence must not execute user code.
    mro = _MRO_DESCRIPTOR.__get__(cls)
    slots = {}
    for base in mro:
        namespace = _DICT_DESCRIPTOR.__get__(base)
        for name in ("__eq__", "__ne__"):
            if name not in slots and name in namespace:
                slots[name] = namespace[name]
    return mro, slots.get("__eq__"), slots.get("__ne__")


def operand(value):
    """Describe exact builtin values, safe scalar enums, or identity equality."""
    cls = type(value)
    if value is None:
        return ("none",)
    if cls is bool:
        return ("bool", value)
    if cls is int:
        result = ("int", str(value))
    elif cls is str:
        result = ("str", value)
    elif cls is bytes:
        result = ("bytes", value.hex())
    elif cls is float:
        result = ("float", value.hex())
    elif cls is complex:
        result = ("complex", value.real.hex(), value.imag.hex())
    else:
        mro, eq, ne = _raw_slots(cls)
        is_enum = any(base is enum.Enum for base in mro)
        if is_enum and any(base is str for base in mro) and eq is str.__eq__ and ne is str.__ne__:
            result = ("str", str.__str__(value))
        elif is_enum and any(base is int for base in mro) and eq is int.__eq__ and ne is int.__ne__:
            result = ("int", str(int.__int__(value)))
        elif eq is object.__eq__ and ne is object.__ne__:
            return ("identity",)
        else:
            return None
    return result if all(len(value) <= MAX_VALUE_LENGTH for value in result[1:] if isinstance(value, str)) else None


def observation(left, right):
    before, after = operand(left), operand(right)
    return (before, after, left is right) if before is not None and after is not None else None


def _value(description):
    kind = description[0]
    if kind == "none":
        return None
    if kind == "bool":
        return description[1]
    if kind == "int":
        return int(description[1])
    if kind == "str":
        return description[1]
    if kind == "bytes":
        return bytes.fromhex(description[1])
    if kind == "float":
        return float.fromhex(description[1])
    if kind == "complex":
        return complex(float.fromhex(description[1]), float.fromhex(description[2]))
    raise ValueError("unsupported recorded operand")


def replay(record, operation):
    left, right, same = record
    if operation == "Is":
        return same
    if operation == "IsNot":
        return not same
    if operation not in {"Eq", "NotEq"}:
        raise ValueError("unsupported comparison")
    # A value-comparing builtin can accept an opaque object's underlying value
    # through its reflected comparison (including subclasses/buffer providers).
    # Only identity objects and None are safe partners for an opaque identity.
    if left[0] == "identity" or right[0] == "identity":
        if left[0] not in {"identity", "none"} or right[0] not in {"identity", "none"}:
            raise ValueError("unknown reflected comparison")
        equal = same
    else:
        equal = _value(left) == _value(right)
    return equal if operation == "Eq" else not equal


def changed_comparison(before, after):
    """Find exactly one simple operator substitution and no other AST change."""
    changes = []

    def visit(left, right):
        if ast_equal(left, right):
            return True
        if type(left) is not type(right):
            return False
        if isinstance(left, ast.Compare):
            if (len(left.ops) == len(right.ops) == 1
                    and type(left.ops[0]).__name__ in OPERATORS
                    and type(right.ops[0]).__name__ in OPERATORS
                    and type(left.ops[0]) is not type(right.ops[0])
                    and ast_equal(left.left, right.left)
                    and ast_equal(left.comparators, right.comparators)):
                changes.append((f"{left.lineno}:{left.col_offset}",
                                type(left.ops[0]).__name__, type(right.ops[0]).__name__))
                return True
        if isinstance(left, ast.AST):
            return all(visit(getattr(left, field, None), getattr(right, field, None)) for field in left._fields)
        if isinstance(left, list):
            return len(left) == len(right) and all(visit(a, b) for a, b in zip(left, right))
        return False

    return changes[0] if visit(before, after) and len(changes) == 1 else None


def unchanged_tests(before, after, file_profile, incomplete=()):
    """Return baseline-map indices with complete, unchanged pure observations.

    Call only for a patch with one semantically changed file. Source readers and
    uncertain tests must remain selected independently of this result.
    """
    change = changed_comparison(before, after)
    if not change:
        return set()
    site, old_op, new_op = change
    evidence = file_profile.get(site, {})
    if evidence.get("operator") != old_op:
        return set()
    excluded, result = set(incomplete), set()
    values = evidence.get("values", [])
    # Require equivalence at the whole site, including earlier callers. A
    # changed earlier result could otherwise modify state consumed by a later
    # test whose own baseline comparison happened to remain equivalent.
    for index, observations in evidence.get("tests", {}).items():
        number = int(index)
        if number in excluded or not observations:
            return set()
        try:
            if not all(replay(values[value], old_op) == replay(values[value], new_op) for value in observations):
                return set()
            result.add(number)
        except (TypeError, ValueError, IndexError, KeyError, OverflowError):
            return set()
    return result


def ordinary_function_site(tree, site):
    """Require the comparison to be in a function body, not a definition hook."""
    def visit(node, ordinary=False):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return any(visit(child, True) for child in node.body)
        if isinstance(node, ast.ClassDef):
            return any(visit(child, False) for child in node.body)
        if isinstance(node, ast.Lambda):
            return False
        if isinstance(node, ast.Compare) and f"{node.lineno}:{node.col_offset}" == site:
            return ordinary
        return any(visit(child, ordinary) for child in ast.iter_child_nodes(node))
    return visit(tree)
