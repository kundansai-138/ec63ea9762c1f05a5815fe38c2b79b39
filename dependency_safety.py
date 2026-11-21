"""Conservative baseline analysis for dependencies outliving an individual call.

Only unmodified source is examined. Persistent state writers and their callees
cannot be selected solely from first-call line coverage: a cached result may be
used by later tests without running its producer again.
"""
from __future__ import annotations

import ast
from collections import defaultdict


MUTATORS = {
    "append", "extend", "insert", "remove", "pop", "popitem", "clear",
    "update", "setdefault", "add", "discard", "difference_update",
    "intersection_update", "symmetric_difference_update", "sort", "reverse",
    "__setitem__", "__delitem__", "__setattr__", "__delattr__",
}
CACHE_DECORATORS = {"functools.cache", "functools.lru_cache", "functools.cached_property"}


def _names(target):
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return set().union(*(_names(x) for x in target.elts)) if target.elts else set()
    return set()


def _own_nodes(function):
    """Iterate a function's own scope, without attributing nested bodies to it."""
    pending = list(function.body)
    while pending:
        node = pending.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        pending.extend(ast.iter_child_nodes(node))


def unsafe_functions(source: str | ast.Module) -> set[tuple[int, int]]:
    tree = source if isinstance(source, ast.Module) else ast.parse(source)
    functions = []
    classes = set()
    module_names = set()
    imports = {}
    by_name = defaultdict(list)

    for node in _own_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module_names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                module_names.update(_names(target))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                module_names.add(name)
                imports[name] = alias.name if alias.asname else name
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.asname or alias.name
                module_names.add(name)
                imports[name] = (node.module + "." if node.module else "") + alias.name

    def collect(node, owner=None):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                classes.add(child.name)
                collect(child, child.name)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                index = len(functions)
                functions.append((child, owner))
                by_name[child.name].append(index)
                collect(child, None)
            else:
                collect(child, owner)
    collect(tree)
    module_names.update(classes)
    # Resolve simple module-level aliases such as factory = make_value.
    for _ in range(len(tree.body) + 1):
        changed = False
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Name):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for name in _names(target):
                        previous = set(by_name[name])
                        by_name[name] = sorted(previous | set(by_name.get(node.value.id, ())))
                        changed |= previous != set(by_name[name])
        if not changed:
            break

    def qualified(node):
        if isinstance(node, ast.Call):
            return qualified(node.func)
        if isinstance(node, ast.Name):
            return imports.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return qualified(node.value) + "." + node.attr
        return ""

    records = []
    for function, owner in functions:
        nodes = list(_own_nodes(function))
        positional = [a.arg for a in function.args.posonlyargs + function.args.args]
        parameters = positional + [a.arg for a in function.args.kwonlyargs]
        parameters += [a.arg for a in (function.args.vararg, function.args.kwarg) if a]
        explicit_global, nonlocal_names, locals_ = set(), set(), set(parameters)
        local_imports = set()
        assignments = []
        for node in nodes:
            if isinstance(node, ast.Global):
                explicit_global.update(node.names)
            elif isinstance(node, ast.Nonlocal):
                nonlocal_names.update(node.names)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                locals_.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    locals_.update(_names(target))
                    assignments.append((target, node.value))
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                locals_.update(_names(node.target))
                assignments.append((node.target, node.iter))
            elif isinstance(node, ast.comprehension):
                assignments.append((node.target, node.iter))
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars:
                        locals_.update(_names(item.optional_vars))
            elif isinstance(node, ast.ExceptHandler) and node.name:
                locals_.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[0]
                    locals_.add(name)
                    local_imports.add(name)
        locals_.difference_update(explicit_global | nonlocal_names)
        persistent = (module_names - locals_) | explicit_global | nonlocal_names | local_imports
        if owner and positional and (positional[0] == "cls" or any(qualified(d) == "classmethod" for d in function.decorator_list)):
            persistent.add(positional[0])

        # A name's origins are either persistent ('*') or mutable arguments.
        origins = {name: {"*"} for name in persistent}
        for name in parameters:
            origins.setdefault(name, set()).add(name)

        def provenance(node):
            if node is None:
                return set()
            if isinstance(node, ast.Name):
                return origins.get(node.id, set())
            if isinstance(node, (ast.Attribute, ast.Subscript)):
                return provenance(node.value)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in {"globals", "locals"}:
                    return {"*"}
                if node.func.id in {"getattr", "vars"} and node.args:
                    return provenance(node.args[0])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"values", "items", "keys"}:
                return provenance(node.func.value)
            if isinstance(node, ast.IfExp):
                return provenance(node.body) | provenance(node.orelse)
            return set()

        defaults = list(zip(positional[-len(function.args.defaults):], function.args.defaults)) if function.args.defaults else []
        defaults += list(zip([a.arg for a in function.args.kwonlyargs], function.args.kw_defaults))
        for name, default in defaults:
            if provenance(default) or isinstance(default, (ast.List, ast.Dict, ast.Set)):
                origins.setdefault(name, set()).add("*")
        for _ in range(len(assignments) + 1):
            changed = False
            for target, value in assignments:
                value_origins = provenance(value)
                for name in _names(target):
                    before = origins.setdefault(name, set()).copy()
                    origins[name].update(value_origins)
                    changed |= origins[name] != before
            if not changed:
                break

        callee_aliases = {name: set(indices) for name, indices in by_name.items()}
        for _ in range(len(assignments) + 1):
            changed = False
            for target, value in assignments:
                referred = value.id if isinstance(value, ast.Name) else value.attr if isinstance(value, ast.Attribute) else None
                for name in _names(target):
                    previous = callee_aliases.setdefault(name, set()).copy()
                    callee_aliases[name].update(callee_aliases.get(referred, ()))
                    changed |= callee_aliases[name] != previous
            if not changed:
                break

        mutated, calls = set(), []
        for node in nodes:
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                targets = [node.target]
            elif isinstance(node, ast.Delete):
                targets = node.targets
            for target in targets:
                if isinstance(target, ast.Name):
                    if target.id in explicit_global | nonlocal_names:
                        mutated.add("*")
                else:
                    mutated.update(provenance(target))
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr in MUTATORS:
                    mutated.update(provenance(node.func.value))
                if isinstance(node.func, ast.Name) and node.func.id in {"setattr", "delattr"} and node.args:
                    mutated.update(provenance(node.args[0]))
                name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else None
                for callee in callee_aliases.get(name, ()):
                    target_function, target_owner = functions[callee]
                    arguments = list(node.args)
                    if isinstance(node.func, ast.Attribute) and target_owner:
                        # Conservative binding for self/cls/class method calls.
                        arguments.insert(0, node.func.value)
                    target_params = [a.arg for a in target_function.args.posonlyargs + target_function.args.args]
                    bindings = {p: provenance(a) for p, a in zip(target_params, arguments)}
                    bindings.update({kw.arg: provenance(kw.value) for kw in node.keywords if kw.arg})
                    calls.append((callee, bindings))
        seeded = any(qualified(d) in CACHE_DECORATORS for d in function.decorator_list)
        records.append({"mutated": mutated, "calls": calls, "unsafe": seeded})

    # Propagate argument mutation effects through same-module calls. Passing a
    # global cache to a helper that mutates its parameter is a persistent write.
    changed = True
    while changed:
        changed = False
        for record in records:
            before = record["mutated"].copy()
            for callee, bindings in record["calls"]:
                for parameter in tuple(records[callee]["mutated"]):
                    if parameter == "*":
                        record["mutated"].add("*")
                    else:
                        record["mutated"].update(bindings.get(parameter, ()))
            changed |= record["mutated"] != before

    unsafe = {i for i, record in enumerate(records) if record["unsafe"] or "*" in record["mutated"]}
    pending = list(unsafe)
    while pending:
        caller = pending.pop()
        for callee, _ in records[caller]["calls"]:
            if callee not in unsafe:
                unsafe.add(callee)
                pending.append(callee)
    return {(functions[i][0].lineno, functions[i][0].end_lineno) for i in unsafe}
