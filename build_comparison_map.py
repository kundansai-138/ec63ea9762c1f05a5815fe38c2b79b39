"""Observe comparison operands while running only the verified pinned baseline.

All eligible baseline comparison sites are instrumented; no proposed patch or
answer file is read. Raw operand observations are replayed only at selection.
"""
from __future__ import annotations

import argparse
import ast
import builtins
from collections import defaultdict
import gzip
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import platform
import sys
import threading
import time

import pytest

from baseline_comparisons import OPERATORS, observation, replay
from build_map import DependencyRecorder, PINNED_COMMIT
from build_callable_map import verify_source

HOOK = "_blast_baseline_compare_observer_7f1a"
MAX_OBSERVATIONS = 32


class _NoCoverage:
    def switch_context(self, context):
        pass


class ComparisonRecorder(DependencyRecorder):
    def __init__(self, source, tracked):
        super().__init__(source, tracked)
        self.cov = _NoCoverage()
        self.sites = []
        self.site_ids = {}
        self.observations = defaultdict(dict)
        self.background_thread = False

    def instrument_classes(self):
        pass

    def site(self, path, node):
        key = (path, node.lineno, node.col_offset)
        if key not in self.site_ids:
            self.site_ids[key] = len(self.sites)
            self.sites.append((path, f"{node.lineno}:{node.col_offset}", type(node.ops[0]).__name__))
        return self.site_ids[key]

    def compare(self, site, operation, left, right):
        try:
            result = OPERATORS[operation](left, right)
        except BaseException:
            self.observations[site][self.context] = None
            raise
        if self.active:
            if threading.current_thread() is not threading.main_thread():
                self.background_thread = True
            contexts = self.observations[site]
            values = contexts.setdefault(self.context, set())
            if values is not None:
                try:
                    value = observation(left, right)
                    understood = value is not None and type(result) is bool and replay(value, operation) == result
                except (TypeError, ValueError, OverflowError):
                    understood = False
                if not understood:
                    contexts[self.context] = None
                else:
                    values.add(value)
                    if len(values) > MAX_OBSERVATIONS:
                        contexts[self.context] = None
        return result

    def record(self, pytest_args):
        finder = ComparisonFinder(self)
        if hasattr(builtins, HOOK):
            raise RuntimeError("comparison observer name collision")
        setattr(builtins, HOOK, self.compare)
        sys.meta_path.insert(0, finder)
        sys.addaudithook(self.audit)
        self.active = True
        try:
            return int(pytest.main(pytest_args, plugins=[self]))
        finally:
            self.active = False
            sys.meta_path.remove(finder)
            delattr(builtins, HOOK)

    def profile(self, exit_code, elapsed):
        numbers = {node: i for i, node in enumerate(self.nodes)}
        context_numbers = {context: {numbers[node] for node in nodes if node in numbers}
                           for context, nodes in self.context_tests.items()}
        incomplete = set(self.incomplete)
        for context in self.untraced_subprocess_contexts:
            incomplete.update(self.context_tests.get(context, self.nodes))
        if self.background_thread:
            incomplete.update(self.nodes)
        files = defaultdict(dict)
        for site, contexts in self.observations.items():
            path, location, operation = self.sites[site]
            per_test = {}
            for context, values in contexts.items():
                for index in context_numbers.get(context, set(numbers.values())):
                    existing = per_test.setdefault(index, set())
                    if existing is None:
                        continue
                    if values is None:
                        per_test[index] = None
                    else:
                        existing.update(values)
                        if len(existing) > MAX_OBSERVATIONS:
                            per_test[index] = None
            unique = sorted(set().union(*(values for values in per_test.values() if values)), key=repr)
            value_ids = {value: index for index, value in enumerate(unique)}
            files[path][location] = {
                "operator": operation, "values": unique,
                "tests": {str(index): sorted(value_ids[value] for value in values) if values else None
                          for index, values in sorted(per_test.items())},
            }
        return {
            "version": 1, "commit": PINNED_COMMIT, "tests": self.nodes,
            "files": files, "incomplete": sorted(incomplete),
            "metadata": {"builder_version": 1, "complete": exit_code == 0 and not self.collection_errors,
                         "python": platform.python_version(), "pytest": pytest.__version__,
                         "pytest_exit_code": exit_code, "collection_errors": self.collection_errors,
                         "outcomes": dict(self.outcomes), "elapsed_seconds": round(elapsed, 3),
                         "instrumented_sites": len(self.sites), "observed_sites": len(self.observations),
                         "background_thread": self.background_thread,
                         "max_observations_per_test_site": MAX_OBSERVATIONS,
                         "scope_dependencies": "shared fixture setup and module teardown propagated to consumers",
                         "worker_execution": "baseline pool jobs inlined with ForkingPickler transport"},
        }


class CompareTransformer(ast.NodeTransformer):
    def __init__(self, recorder, path):
        self.recorder, self.path = recorder, path

    def visit_Compare(self, node):
        operation = type(node.ops[0]).__name__ if len(node.ops) == 1 else None
        site = self.recorder.site(self.path, node) if operation in OPERATORS else None
        self.generic_visit(node)
        if site is None:
            return node
        call = ast.Call(func=ast.Name(id=HOOK, ctx=ast.Load()),
                        args=[ast.Constant(site), ast.Constant(operation), node.left, node.comparators[0]],
                        keywords=[])
        return ast.copy_location(call, node)


class ComparisonLoader(importlib.machinery.SourceFileLoader):
    def __init__(self, name, path, recorder):
        super().__init__(name, path)
        self.recorder = recorder

    def get_code(self, fullname):
        path = self.get_filename(fullname)
        relative = self.recorder.relative(path)
        tree = ast.parse(self.get_data(path), filename=path)
        tree = CompareTransformer(self.recorder, relative).visit(tree)
        ast.fix_missing_locations(tree)
        return compile(tree, path, "exec", dont_inherit=True)


class ComparisonFinder(importlib.abc.MetaPathFinder):
    def __init__(self, recorder):
        self.recorder = recorder

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            relative = self.recorder.relative(spec.origin)
            if relative and relative.startswith("sqlglot/"):
                spec.loader = ComparisonLoader(fullname, spec.origin, self.recorder)
                return spec
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--map", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    manifest = json.loads(args.source_manifest.read_text())
    verify_source(source, manifest)
    mapping = json.load(gzip.open(args.map, "rt", encoding="utf-8"))
    if mapping.get("commit") != PINNED_COMMIT:
        raise ValueError("map is not the pinned baseline")
    os.environ.update(SKIP_INTEGRATION="0", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1")
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    sys.path.insert(0, str(source))
    os.chdir(source)
    if any(name == "sqlglot" or name.startswith("sqlglot.") for name in sys.modules):
        raise RuntimeError("sqlglot was imported before comparison instrumentation")
    recorder = ComparisonRecorder(source, sorted(manifest["files"]))
    started = time.monotonic()
    code = recorder.record(["-q", "--tb=short", "-p", "no:cacheprovider", "tests"])
    profile = recorder.profile(code, time.monotonic() - started)
    if code or recorder.collection_errors or recorder.nodes != mapping["tests"]:
        raise RuntimeError("instrumented baseline did not reproduce the full passing suite")
    verify_source(source, manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as stream:
        with gzip.GzipFile(fileobj=stream, mode="wb", mtime=0) as compressed:
            compressed.write(json.dumps(profile, sort_keys=True, separators=(",", ":")).encode())
    print(json.dumps(profile["metadata"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
