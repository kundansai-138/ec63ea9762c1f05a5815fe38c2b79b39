"""Class-qualified dependencies derived only from the pinned baseline.

The first supported contract is a plain boolean schema dictionary, with
unchanged ordered string keys. Import-time derived sets and exact class
attribute reads are supplied by the baseline recorder, never by mutant labels.
"""
from __future__ import annotations
import ast
from ast_equal import ast_equal

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def boolean_schema(node):
    if not isinstance(node, ast.Dict) or not node.keys:
        return None
    keys = []
    for key, value in zip(node.keys, node.values):
        if (not isinstance(key, ast.Constant) or type(key.value) is not str
                or not isinstance(value, ast.Constant) or type(value.value) is not bool):
            return None
        keys.append(key.value)
    return keys if len(set(keys)) == len(keys) else None


def class_attribute_tests(before, after, path, qualname, mapping):
    """Return baseline test indices, or None when the contract cannot apply."""
    profile = mapping.get("class_attribute_profile", {})
    if (profile.get("version") != 1 or profile.get("commit") != PIN
            or profile.get("tests") != mapping.get("tests")
            or profile.get("metadata", {}).get("complete") is not True
            or profile.get("metadata", {}).get("background_thread")):
        return None
    if (type(before) is not type(after) or not isinstance(before, (ast.Assign, ast.AnnAssign))):
        return None
    old_targets = before.targets if isinstance(before, ast.Assign) else [before.target]
    new_targets = after.targets if isinstance(after, ast.Assign) else [after.target]
    if len(old_targets) != 1 or not isinstance(old_targets[0], ast.Name) or not ast_equal(old_targets,new_targets):
        return None
    name = old_targets[0].id
    if name != "arg_types":
        return None
    if isinstance(before, ast.AnnAssign) and not ast_equal(before.annotation, after.annotation):
        return None
    keys = boolean_schema(before.value)
    if keys is None or keys != boolean_schema(after.value):
        return None
    record = profile.get("attributes", {}).get(path + "::" + qualname + "::" + name)
    if not record or record.get("contract") != "boolean-schema-v1" or record.get("unsafe_collection"):
        return None
    # A full successful map is required. Untraced or unusual baseline tests
    # remain selected even when their class-qualified edges are unavailable.
    incomplete = set(profile.get("incomplete", []))
    return set(record["readers"]) | {i for i, node in enumerate(mapping["tests"]) if node in incomplete}


def derived_schema_read(node):
    """Recognize cls.dest={k for k,v in cls.arg_types.items() if v}."""
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        return None
    target, value = node.targets[0], node.value
    if (not isinstance(target, ast.Attribute) or not isinstance(target.value, ast.Name)
            or not isinstance(value, ast.SetComp) or len(value.generators) != 1):
        return None
    gen = value.generators[0]
    if (gen.is_async or not isinstance(gen.target, ast.Tuple) or len(gen.target.elts) != 2
            or not all(isinstance(n, ast.Name) for n in gen.target.elts) or len(gen.ifs) != 1
            or not isinstance(value.elt, ast.Name) or value.elt.id != gen.target.elts[0].id
            or not isinstance(gen.ifs[0], ast.Name) or gen.ifs[0].id != gen.target.elts[1].id):
        return None
    call = gen.iter
    if (not isinstance(call, ast.Call) or call.args or call.keywords or not isinstance(call.func, ast.Attribute)
            or call.func.attr != "items" or not isinstance(call.func.value, ast.Attribute)):
        return None
    read = call.func.value
    if (read.attr != "arg_types" or not isinstance(read.value, ast.Name)
            or read.value.id != target.value.id or target.attr != "required_args"):
        return None
    return read, target.attr, target.value.id
