"""Dependencies for literal captures in provably inert closure factories.

Only baseline source and baseline coverage are used. Successful proofs return
the bodies of the existing closures, not predictions of failing tests. Callers
must also retain class consumers, attribute readers and source-file readers.
"""
from __future__ import annotations

import ast
import copy
from functools import lru_cache
from pathlib import PurePosixPath


def _dump(node):
    return ast.dump(node, include_attributes=False)


@lru_cache(maxsize=32)
def _tree(source):
    return ast.parse(source)


def _shape(node):
    allowed = (ast.Constant, ast.Dict, ast.List, ast.Tuple, ast.Set, ast.Load,
               ast.UnaryOp, ast.UAdd, ast.USub)
    if any(not isinstance(n, allowed) for n in ast.walk(node)):
        raise ValueError("nonliteral argument")
    def shape(value):
        if isinstance(value, dict):
            return (dict, tuple((shape(k), shape(v)) for k, v in value.items()))
        if isinstance(value, (tuple, list, set)):
            return (type(value), tuple(shape(v) for v in value))
        return type(value)
    return shape(ast.literal_eval(node))


def _bindings(body, name):
    """Find all bindings in this namespace, excluding nested namespaces."""
    found = []
    pending = list(body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                found.append(node)
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if any((alias.asname or alias.name.split('.')[0]) == name or alias.name == '*'
                   for alias in node.names):
                found.append(node)
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)) and node.id == name:
            found.append(node)
        # A dynamic namespace mutation prevents static binding resolution.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {
            'exec', 'eval', 'globals', 'locals', 'vars',
        }:
            found.append(node)
        pending.extend(ast.iter_child_nodes(node))
    return found


def _owner_classes(tree, assignment):
    owners = []
    def walk(node, classes, inside_function=False):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            inside_function = True
        if isinstance(node, ast.ClassDef):
            classes = classes + [node]
        if (isinstance(node, type(assignment)) and getattr(node, 'lineno', None) == assignment.lineno
                and _dump(node) == _dump(assignment)):
            if not inside_function:
                owners.extend(classes)
            return
        for child in ast.iter_child_nodes(node):
            walk(child, classes, inside_function)
    walk(tree, [])
    return owners


def _module_path(module, path, level, files):
    if level:
        package = list(PurePosixPath(path).parent.parts)
        if level > len(package):
            return None
        parts = package[:len(package) - level + 1] + (module.split('.') if module else [])
    else:
        parts = module.split('.') if module else []
    base = '/'.join(parts)
    candidates = [p for p in (base + '.py', base + '/__init__.py') if p in files]
    return candidates[0] if len(candidates) == 1 else None


def _resolve(name, path, files, seen=()):
    if (path, name) in seen or len(seen) >= 8:
        return None
    source = files.get(path, {}).get('source')
    if not isinstance(source, str):
        return None
    tree = _tree(source)
    bindings = _bindings(tree.body, name)
    if len(bindings) != 1:
        return None
    node = bindings[0]
    if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node in tree.body:
        return path, node, tree
    if not isinstance(node, ast.ImportFrom) or node not in tree.body:
        return None
    alias = next((a for a in node.names if (a.asname or a.name) == name), None)
    target = _module_path(node.module, path, node.level, files)
    if alias is None or target is None:
        return None
    return _resolve(alias.name, target, files, seen + ((path, name),))


def _resolve_base(expression, path, files):
    if isinstance(expression, ast.Name):
        return _resolve(expression.id, path, files)
    attributes = []
    while isinstance(expression, ast.Attribute):
        attributes.insert(0, expression.attr)
        expression = expression.value
    if not isinstance(expression, ast.Name) or not attributes:
        return None
    tree = _tree(files[path]['source'])
    bindings = _bindings(tree.body, expression.id)
    if len(bindings) != 1 or bindings[0] not in tree.body:
        return None
    binding = bindings[0]
    if isinstance(binding, ast.Import):
        alias = next((a for a in binding.names if (a.asname or a.name.split('.')[0]) == expression.id), None)
        if alias is None:
            return None
        module = alias.name if alias.asname else alias.name.split('.')[0]
        target = _module_path('.'.join([module] + attributes[:-1]), path, 0, files)
    elif isinstance(binding, ast.ImportFrom):
        alias = next((a for a in binding.names if (a.asname or a.name) == expression.id), None)
        if alias is None:
            return None
        package = _module_path(binding.module, path, binding.level, files)
        if package is None or not package.endswith('/__init__.py'):
            return None
        # An explicitly rebound package attribute may not be the submodule.
        if _bindings(_tree(files[package]['source']).body, alias.name):
            return None
        base = str(PurePosixPath(package).parent).replace('\\', '/')
        module = '/'.join([base, alias.name] + attributes[:-1])
        candidates = [p for p in (module + '.py', module + '/__init__.py') if p in files]
        target = candidates[0] if len(candidates) == 1 else None
    else:
        return None
    return _resolve(attributes[-1], target, files) if target else None


def _plain_class(node, path, files, seen=()):
    identity = (path, node.lineno, node.name)
    if identity in seen or len(seen) >= 32 or node.decorator_list or node.keywords:
        return False
    if any(_bindings(node.body, hook) for hook in ('__init_subclass__', '__new__')):
        return False
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id == 'object' and not _bindings(_tree(files[path]['source']).body, 'object'):
            continue
        resolved = _resolve_base(base, path, files)
        if resolved is None or not isinstance(resolved[1], ast.ClassDef):
            return False
        if not _plain_class(resolved[1], resolved[0], files, seen + (identity,)):
            return False
    return True


def _closure_body(path, factory, tree, files):
    if not isinstance(factory, ast.FunctionDef) or factory.decorator_list or getattr(factory, 'type_params', []):
        return None
    body = list(factory.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body.pop(0)
    if (len(body) != 2 or not isinstance(body[0], ast.FunctionDef)
            or not isinstance(body[1], ast.Return) or not isinstance(body[1].value, ast.Name)
            or body[1].value.id != body[0].name):
        return None
    closure = body[0]
    if (closure.decorator_list or closure.args.defaults or any(d is not None for d in closure.args.kw_defaults)
            or getattr(closure, 'type_params', [])):
        return None
    annotations = [a.annotation for a in closure.args.posonlyargs + closure.args.args + closure.args.kwonlyargs]
    annotations += [a.annotation for a in (closure.args.vararg, closure.args.kwarg) if a]
    annotations.append(closure.returns)
    postponed = any(isinstance(n, ast.ImportFrom) and n.module == '__future__'
                    and any(a.name == 'annotations' for a in n.names) for n in tree.body)
    # Without postponed annotations, an annotation can execute against captures.
    if not postponed and any(a is not None for a in annotations):
        return None
    # A fresh closure may nevertheless keep mutable captured state between
    # calls. Support the forwarding idiom only: *args/**kwargs are expanded
    # into fresh call containers, and changed captures must be immutable.
    positional = closure.args.posonlyargs + closure.args.args
    closure_body = list(closure.body)
    if (closure_body and isinstance(closure_body[0], ast.Expr)
            and isinstance(closure_body[0].value, ast.Constant)
            and isinstance(closure_body[0].value.value, str)):
        closure_body.pop(0)
    if (len(positional) != 1 or closure.args.vararg or closure.args.kwarg or closure.args.kwonlyargs
            or factory.args.posonlyargs or factory.args.args or factory.args.kwonlyargs
            or not factory.args.vararg or not factory.args.kwarg
            or len(closure_body) != 1 or not isinstance(closure_body[0], ast.Return)
            or not isinstance(closure_body[0].value, ast.Call)):
        return None
    call = closure_body[0].value
    if (not isinstance(call.func, ast.Attribute) or not isinstance(call.func.value, ast.Name)
            or call.func.value.id != positional[0].arg or len(call.args) != 1
            or not isinstance(call.args[0], ast.Starred) or not isinstance(call.args[0].value, ast.Name)
            or call.args[0].value.id != factory.args.vararg.arg or len(call.keywords) != 1
            or call.keywords[0].arg is not None or not isinstance(call.keywords[0].value, ast.Name)
            or call.keywords[0].value.id != factory.args.kwarg.arg):
        return None
    if any(isinstance(n, (ast.Yield, ast.YieldFrom, ast.Await)) for n in ast.walk(closure)):
        return None
    entry = files[path]
    if 'collection_lines' not in entry or 'persistent_functions' not in entry or 'lines' not in entry:
        return None
    start, end = closure.body[0].lineno, closure.end_lineno
    # Same-line definitions cannot distinguish creation from invocation.
    if start <= closure.lineno:
        return None
    if any(start <= line <= end for line in entry['collection_lines']):
        return None
    if any(a <= end and b >= factory.lineno for a, b in entry['persistent_functions']):
        return None
    return path, start, end


def factory_intervals(before, after, path, files):
    """Prove a class dispatch-table edit changes only literal closure captures.

    The factory must only define and return an undecorated forwarding closure. Its closure
    is not called during baseline imports and has no known persistent effects.
    Arguments retain their shape and call binding; signatures, dictionary keys,
    surrounding expressions and all other assignment syntax remain unchanged.
    Returned intervals cover callers of *every* instance of each factory.

    This complements the selector's baseline dependency heuristic; reflective
    consumers still require its conservative class/attribute/source evidence.
    """
    if (not isinstance(before, (ast.Assign, ast.AnnAssign)) or type(before) is not type(after)
            or not isinstance(before.value, ast.Dict) or not isinstance(after.value, ast.Dict)
            or len(before.value.values) != len(after.value.values)):
        return None
    targets = before.targets if isinstance(before, ast.Assign) else [before.target]
    if not all(isinstance(t, ast.Name) and not t.id.startswith('__') for t in targets):
        return None
    try:
        tree = _tree(files[path]['source'])
        owners = _owner_classes(tree, before)
        if not owners or any(not _plain_class(owner, path, files) for owner in owners):
            return None
        old, new = copy.deepcopy(before), copy.deepcopy(after)
        intervals = []
        for index, (left, right) in enumerate(zip(before.value.values, after.value.values)):
            if _dump(left) == _dump(right):
                continue
            if (not isinstance(left, ast.Call) or not isinstance(right, ast.Call)
                    or not isinstance(left.func, ast.Name) or _dump(left.func) != _dump(right.func)
                    or len(left.args) != len(right.args)
                    or [k.arg for k in left.keywords] != [k.arg for k in right.keywords]
                    or any(k.arg is None for k in left.keywords)):
                return None
            pairs = list(zip(left.args, right.args)) + [(a.value, b.value) for a, b in zip(left.keywords, right.keywords)]
            if any(_dump(a) != _dump(b) and _shape(a) != _shape(b) for a, b in pairs):
                return None
            # Unchanged dynamic args could inspect or depend on earlier captured
            # values; support literal-only calls to keep construction inert.
            for a, b in pairs:
                _shape(a)
                _shape(b)
                if any(isinstance(node, (ast.Dict, ast.List, ast.Set))
                       for value in (a, b) for node in ast.walk(value)):
                    return None
            if any(_bindings(owner.body, left.func.id) for owner in owners):
                return None
            resolved = _resolve(left.func.id, path, files)
            if resolved is None:
                return None
            interval = _closure_body(*resolved, files)
            if interval is None:
                return None
            intervals.append(interval)
            old.value.values[index] = ast.Constant(value=None)
            new.value.values[index] = ast.Constant(value=None)
        if not intervals or _dump(old) != _dump(new):
            return None
        return sorted(set(intervals))
    except (KeyError, SyntaxError, ValueError, TypeError, RecursionError):
        return None
