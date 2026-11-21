"""Compare syntax trees without allocating their full formatted dump strings."""
from __future__ import annotations

import ast


_MISSING = object()


def ast_equal(left, right) -> bool:
    """Compare AST fields, ignoring source positions but preserving literal types.

    Python considers ``True == 1 == 1.0``; those are different syntax trees.
    Float/complex representations also distinguish signed zero. Only AST fields
    participate, matching ``ast.dump(..., include_attributes=False)`` on parsed
    trees. Missing optional fields remain distinct from explicit values.
    """
    pending = [(left, right)]
    while pending:
        before, after = pending.pop()
        if before is after:
            continue
        kind = type(before)
        if kind is not type(after):
            return False
        if isinstance(before, ast.AST):
            pending.extend((getattr(before, name, _MISSING), getattr(after, name, _MISSING))
                           for name in before._fields)
        elif kind is list or kind is tuple:
            if len(before) != len(after):
                return False
            pending.extend(zip(before, after))
        elif kind is float or kind is complex:
            if repr(before) != repr(after):
                return False
        elif before != after:
            return False
    return True
