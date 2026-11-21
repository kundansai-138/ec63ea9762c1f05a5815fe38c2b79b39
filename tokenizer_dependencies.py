"""Conservative contracts for baseline tokenizer configuration provenance.

The artifact contains readers of class fields and the core instances to which
those fields were copied. It never contains patched programs or failure labels.
Dictionary key changes, structural changes and unproven collection consumers
deliberately retain the existing selector fallback.
"""
from __future__ import annotations
import ast
from ast_equal import ast_equal

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
CONTRACT = "tokenizer-literal-v1"


def literal_shape(node):
    """JSON-stable shape; dictionary keys and scalar types remain invariant."""
    if isinstance(node, ast.Constant) and type(node.value) in (str, int, bool):
        return [type(node.value).__name__]
    if (isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub))
            and isinstance(node.operand, ast.Constant) and type(node.operand.value) is int):
        return ["int"]
    if isinstance(node, (ast.Tuple, ast.List)):
        children = [literal_shape(n) for n in node.elts]
        return [type(node).__name__, children] if all(s is not None for s in children) else None
    if isinstance(node, ast.Dict):
        if not all(isinstance(k, ast.Constant) and type(k.value) is str for k in node.keys):
            return None
        keys = [k.value for k in node.keys]
        values = [literal_shape(v) for v in node.values]
        if len(set(keys)) != len(keys) or any(s is None for s in values):
            return None
        return ["Dict", [[key, value] for key, value in zip(keys, values)]]
    return None


def tokenizer_attribute_tests(before, after, path, qualname, mapping):
    """Baseline test indices, or None when any proof obligation is missing."""
    profile = mapping.get("tokenizer_attribute_profile", {})
    if (profile.get("version") != 1 or profile.get("commit") != PIN
            or profile.get("tests") != mapping.get("tests")
            or profile.get("metadata", {}).get("complete") is not True
            or profile.get("metadata", {}).get("background_thread")):
        return None
    if type(before) is not type(after) or not isinstance(before, (ast.Assign, ast.AnnAssign)):
        return None
    old = before.targets if isinstance(before, ast.Assign) else [before.target]
    new = after.targets if isinstance(after, ast.Assign) else [after.target]
    if len(old) != 1 or not isinstance(old[0], ast.Name) or not ast_equal(old, new):
        return None
    if isinstance(before, ast.AnnAssign) and not ast_equal(before.annotation, after.annotation):
        return None
    shape = literal_shape(before.value)
    if shape is None or shape != literal_shape(after.value):
        return None
    record = profile.get("attributes", {}).get(path + "::" + qualname + "::" + old[0].id)
    # JSON converts tuple pairs into lists; shapes are represented only by lists.
    if (not record or record.get("contract") != CONTRACT or record.get("shape") != shape
            or record.get("unsafe_collection")):
        return None
    incomplete = set(profile.get("incomplete", []))
    return set(record["readers"]) | {i for i, node in enumerate(mapping["tests"]) if node in incomplete}


def membership_only_alias(function, statement):
    """Prove an assigned dictionary is subsequently observed only by its keys.

    Used for import-time derivation such as choosing an escape format. Every
    use of the local alias must be an exact string-key membership comparison;
    aliases, calls, rebinding, nested scopes and value reads reject the proof.
    """
    if (not isinstance(statement, ast.Assign) or len(statement.targets) != 1
            or not isinstance(statement.targets[0], ast.Name)
            or not isinstance(statement.value, ast.Attribute)):
        return False
    alias = statement.targets[0].id
    if any(isinstance(n, (ast.Global, ast.Nonlocal)) and alias in n.names for n in ast.walk(function)):
        return False
    # Dynamic namespace/frame access can observe an alias without an AST Name
    # load, or rebind it without a Store. Such functions have no key-only proof.
    dynamic_names = {"locals", "vars", "globals", "eval", "exec", "__builtins__"}
    frame_attributes = {"f_locals", "f_globals", "_getframe", "currentframe", "stack", "getouterframes"}
    if any(isinstance(n, ast.Name) and n.id in dynamic_names
           or isinstance(n, ast.Attribute) and n.attr in frame_attributes for n in ast.walk(function)):
        return False
    parents = {id(child): node for node in ast.walk(function) for child in ast.iter_child_nodes(node)}
    stores = []
    loads = []
    for node in ast.walk(function):
        if isinstance(node, ast.Name) and node.id == alias:
            if isinstance(node.ctx, ast.Store):
                stores.append(node)
            elif isinstance(node.ctx, ast.Load):
                loads.append(node)
            else:
                return False
    if stores != statement.targets or not loads:
        return False
    for node in loads:
        parent = parents.get(id(node))
        if (not isinstance(parent, ast.Compare) or parent.comparators != [node]
                or len(parent.ops) != 1 or not isinstance(parent.ops[0], (ast.In, ast.NotIn))
                or not isinstance(parent.left, ast.Constant) or type(parent.left.value) is not str):
            return False
        scope = parent
        while id(scope) in parents:
            scope = parents[id(scope)]
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                if scope is not function:
                    return False
                break
    return True
