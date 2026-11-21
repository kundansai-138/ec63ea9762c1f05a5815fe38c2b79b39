#!/usr/bin/env python3
"""Record dependencies by executing only the unmodified, pinned sqlglot tree.

This builder never reads proposed patches, labels, or sample test numbers.  Its
identities are pytest node IDs; the selector joins them to the supplied manifest.
Run in the locked Linux/Python environment for a submission-quality artifact.
"""
from __future__ import annotations

import argparse
import ast
from bisect import bisect_left
from collections import defaultdict
from concurrent.futures import Future
import functools
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import pickle
from multiprocessing.reduction import ForkingPickler
import subprocess
import sys
import time

import coverage
import pytest

from attribute_dependencies import build_attribute_readers
from enrich_map import enrich

PINNED_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def collection_attributes(files):
    """Attribute names potentially consumed by baseline import-time execution.

    Attribute expressions inherit the nearest statement's executed range, so
    continuation lines remain covered without treating an imported function's
    entire (unexecuted) body as import-time work. Some compound expressions can
    conservatively add extra names. Names are independent of proposed patches.
    """
    names = set()
    for filename, entry in files.items():
        source = entry.get("source")
        lines = sorted(entry.get("collection_lines", ()))
        if not filename.endswith(".py") or not source or not lines:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        def visit(node, is_collection=False):
            if isinstance(node, ast.stmt):
                index = bisect_left(lines, node.lineno)
                is_collection = index < len(lines) and lines[index] <= node.end_lineno
            if is_collection:
                if isinstance(node, ast.Attribute):
                    names.add(node.attr)
                elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                      and node.func.id in {"getattr", "hasattr"} and len(node.args) >= 2
                      and isinstance(node.args[1], ast.Constant)
                      and isinstance(node.args[1].value, str)):
                    names.add(node.args[1].value)
            for child in ast.iter_child_nodes(node):
                visit(child, is_collection)

        visit(tree)
    return sorted(names)


class InlineExecutor:
    """Run baseline worker functions in the traced process, retaining Future API.

    sqlglot's optimizer and executor suites submit independent computations via
    spawned processes.  Inlining those functions avoids silently losing their
    coverage and fixture reads. This is used only while building the base map.
    """

    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer, initargs = self._transport((initializer, initargs))
            initializer(*initargs)

    @staticmethod
    def _transport(value):
        # ProcessPoolExecutor transports arguments and returned values through
        # multiprocessing queues. Preserve those serialization dependencies,
        # notably Expr.__reduce__/serde, while executing the worker in-process.
        return pickle.loads(ForkingPickler.dumps(value))

    def submit(self, function, /, *args, **kwargs):
        future = Future()
        try:
            function, args, kwargs = self._transport((function, args, kwargs))
            future.set_result(self._transport(function(*args, **kwargs)))
        except BaseException as error:
            try:
                error = self._transport(error)
            except BaseException as transport_error:
                error = transport_error
            future.set_exception(error)
        return future

    def map(self, function, *iterables, timeout=None, chunksize=1, **kwargs):
        return (self.submit(function, *items).result() for items in zip(*iterables))

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.shutdown()


class DependencyRecorder:
    def __init__(self, source: Path, tracked=None):
        self.source = source
        self.tracked = tracked
        self.prefix = str(source) + os.sep
        self.context = "collection"
        self.active = False
        self.items = []
        self.nodes = []
        self.context_tests = {}
        self.read_contexts = defaultdict(set)
        self.class_contexts = defaultdict(set)
        self.class_seen = defaultdict(set)
        self.incomplete = set()
        self.outcomes = defaultdict(int)
        self.collection_errors = []
        self.untraced_subprocess_contexts = set()
        self.cov = coverage.Coverage(
            data_file=None,
            source=[str(source / "sqlglot"), str(source / "tests")],
            config_file=False,
        )

    def relative(self, filename):
        try:
            filename = os.path.abspath(os.fsdecode(filename))
        except (TypeError, ValueError):
            return None
        if filename.startswith(self.prefix):
            return filename[len(self.prefix):].replace(os.sep, "/")
        return None

    def audit(self, event, args):
        if self.active and event in {"subprocess.Popen", "os.fork", "os.posix_spawn"}:
            self.untraced_subprocess_contexts.add(self.context)
        if self.active and event == "open":
            relative = self.relative(args[0])
            if relative and not relative.startswith(".git/"):
                # Import machinery's file reads are not source introspection.
                # A normal open("module.py") inside a test is still recorded.
                if self.context != "collection" or not relative.endswith((".py", ".pyc")):
                    self.read_contexts[relative].add(self.context)

    def switch(self, context):
        previous = self.context
        self.context = context
        self.cov.switch_context(context)
        return previous

    def scope_context(self, scope, nodeid):
        context = f"scope:{scope}:{nodeid}"
        if context not in self.context_tests:
            if scope == "session":
                members = self.nodes
            else:
                members = [n for n in self.nodes if n == nodeid or n.startswith(nodeid + "::")
                           or (scope == "package" and n.startswith(nodeid.rstrip("/") + "/"))]
            self.context_tests[context] = members
        return context

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection_modifyitems(self, session, config, items):
        self.items = list(items)
        self.nodes = sorted(item.nodeid for item in items)
        self.context_tests["collection"] = self.nodes
        for node in self.nodes:
            self.context_tests[node] = [node]
        for module_name in ("tests.test_optimizer", "tests.test_executor"):
            module = sys.modules.get(module_name)
            if module and hasattr(module, "ProcessPoolExecutor"):
                module.ProcessPoolExecutor = InlineExecutor
        self.instrument_classes()

    def instrument_classes(self):
        from sqlglot import expressions, generator, parser, tokens
        from sqlglot.dialects.dialect import Dialect

        for base in (expressions.Expr, generator.Generator, parser.Parser, tokens.Tokenizer, Dialect):
            original = base.__init__
            cache = {}

            @functools.wraps(original)
            def record(instance, /, *args, _original=original, _cache=cache, **kwargs):
                cls = type(instance)
                if cls not in self.class_seen[self.context]:
                    self.class_seen[self.context].add(cls)
                    if cls not in _cache:
                        keys = []
                        for parent in cls.__mro__:
                            module = sys.modules.get(parent.__module__)
                            path = self.relative(getattr(module, "__file__", ""))
                            if path and path.startswith("sqlglot/"):
                                keys.append(path + "::" + parent.__qualname__)
                        _cache[cls] = keys
                    for key in _cache[cls]:
                        self.class_contexts[key].add(self.context)
                return _original(instance, *args, **kwargs)

            base.__init__ = record

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_setup(self, item):
        previous = self.switch(item.nodeid)
        yield
        self.switch(previous)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_fixture_setup(self, fixturedef, request):
        previous = self.context
        if request.scope != "function":
            self.switch(self.scope_context(request.scope, request.node.nodeid))
        yield
        self.switch(previous)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_call(self, item):
        previous = self.switch(item.nodeid)
        yield
        self.switch(previous)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_teardown(self, item, nextitem):
        # Includes shared fixture finalizers; union over the module prevents
        # attributing a class/module cleanup only to its final test.
        previous = self.switch(self.scope_context("module", item.nodeid.split("::")[0]))
        yield
        self.switch(previous)

    def pytest_runtest_logreport(self, report):
        if report.failed or report.skipped:
            self.incomplete.add(report.nodeid)
        if report.when == "call" or report.failed or report.skipped:
            self.outcomes[report.outcome] += 1

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append(report.nodeid)

    def record(self, pytest_args):
        sys.addaudithook(self.audit)
        self.cov.start()
        self.cov.switch_context(self.context)
        self.active = True
        try:
            result = pytest.main(pytest_args, plugins=[self])
        finally:
            self.active = False
            self.cov.stop()
        return int(result)

    def artifact(self, exit_code, elapsed):
        number = {node: i for i, node in enumerate(self.nodes)}
        context_numbers = {
            context: {number[node] for node in nodes if node in number}
            for context, nodes in self.context_tests.items()
        }

        def expand(contexts, include_collection=False):
            found = set()
            for context in contexts:
                if include_collection or context != "collection":
                    found.update(context_numbers.get(context, ()))
            return found

        tracked = self.tracked
        if tracked is None:
            tracked = subprocess.check_output(
                ["git", "ls-files", "-z"], cwd=self.source
            ).decode().split("\0")
        files = {}
        for name in tracked:
            if not name:
                continue
            path = self.source / name
            # Fixtures may be binary (CSV gzip); still retain their read edges.
            source = None
            if path.is_file() and path.stat().st_size <= 5_000_000:
                try:
                    source = path.read_text(encoding="utf-8")
                except UnicodeError:
                    pass
            files[name] = {
                "source": source, "lines": {}, "collection_lines": [],
                "consumers": [], "reads": [],
            }

        data = self.cov.get_data()
        for absolute in data.measured_files():
            name = self.relative(absolute)
            if name not in files:
                continue
            line_map = {}
            collection_lines = []
            consumers = set()
            for line, contexts in data.contexts_by_lineno(absolute).items():
                if "collection" in contexts:
                    collection_lines.append(line)
                indices = expand(contexts)
                if indices:
                    line_map[str(line)] = sorted(indices)
                    consumers.update(indices)
            files[name]["lines"] = line_map
            files[name]["collection_lines"] = sorted(collection_lines)
            files[name]["consumers"] = sorted(consumers)

        for name, contexts in self.read_contexts.items():
            if name in files:
                files[name]["reads"] = sorted(expand(contexts, include_collection=True))

        classes = {key: sorted(expand(contexts)) for key, contexts in self.class_contexts.items()}
        # Instantiating inherited classes consumes the defining module even if
        # it has no methods of its own and therefore emits no runtime line event.
        for key, indices in classes.items():
            filename = key.split("::", 1)[0]
            if filename in files:
                files[filename]["consumers"] = sorted(set(files[filename]["consumers"]) | set(indices))

        untraced = sorted(self.nodes[i] for i in expand(
            self.untraced_subprocess_contexts, include_collection=True
        ))
        self.incomplete.update(untraced)

        return {
            "version": 1,
            "commit": PINNED_COMMIT,
            "tests": self.nodes,
            "files": files,
            "classes": classes,
            "collection_attributes": collection_attributes(files),
            "attribute_readers": build_attribute_readers(files),
            "incomplete": sorted(self.incomplete),
            "metadata": {
                "python": platform.python_version(),
                "platform": platform.system(),
                "coverage": coverage.__version__,
                "pytest": pytest.__version__,
                "pytest_exit_code": exit_code,
                "collection_errors": self.collection_errors,
                "untraced_subprocess_tests": untraced,
                "outcomes": dict(self.outcomes),
                "elapsed_seconds": round(elapsed, 3),
                "integration_enabled": True,
                "worker_execution": "baseline ProcessPoolExecutor computations executed inline for tracing, with multiprocessing pickle roundtrips for initializer, jobs, results and exceptions",
                "scope_dependencies": "class, module, package and session fixture setup propagated to all scope members; module teardown union",
                "collection_lines": "lines executed during collection, retained separately because their effects may outlive import and affect any test",
                "collection_attribute_policy": "attribute/getattr/hasattr names in statements executed during collection are retained for import-time side-effect fallback",
            },
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-manifest", type=Path,
                        help="Optional pinned Git blob manifest, for environments without the git executable")
    parser.add_argument("--test", action="append", help="Development-only subset; default: the whole tests directory")
    options = parser.parse_args()
    source = options.source.resolve()
    output = options.output.resolve()
    tracked = None
    if options.source_manifest:
        manifest = json.loads(options.source_manifest.read_text("utf-8"))
        if manifest.get("commit") != PINNED_COMMIT or not isinstance(manifest.get("files"), dict):
            parser.error("source manifest must describe the pinned commit and Git blob hashes")
        tracked = sorted(manifest["files"])
        for name, expected in manifest["files"].items():
            raw_path = source / name
            path = raw_path.resolve()
            if not path.is_relative_to(source) or not path.is_file():
                parser.error(f"invalid source manifest path: {name}")
            content = os.readlink(raw_path).encode("utf-8") if raw_path.is_symlink() else path.read_bytes()
            actual = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
            if actual != expected:
                parser.error(f"source does not match pinned Git blob: {name}")
    else:
        actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
        if actual != PINNED_COMMIT:
            parser.error(f"expected pinned commit {PINNED_COMMIT}, got {actual}")
        # A Windows-created checkout may retain CRLF when read from WSL. Compare
        # normalized Git content so line endings alone do not reject the base tree.
        dirty = subprocess.check_output(
            ["git", "-c", "core.autocrlf=true", "diff", "HEAD", "--name-only"],
            cwd=source, text=True,
        )
        if dirty.strip():
            parser.error("the pinned source must have no tracked modifications")
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    sys.path.insert(0, str(source))
    os.chdir(source)
    recorder = DependencyRecorder(source, tracked=tracked)
    started = time.monotonic()
    # No xdist: dynamic contexts must share one process. Worker jobs are inlined.
    exit_code = recorder.record(["-q", "--tb=short", "-p", "no:cacheprovider", *(options.test or ["tests"])])
    artifact = recorder.artifact(exit_code, time.monotonic() - started)
    enrich(artifact)
    artifact["metadata"]["test_subset"] = options.test
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as destination:
        with gzip.GzipFile(fileobj=destination, mode="wb", mtime=0) as compressed:
            compressed.write(json.dumps(artifact, separators=(",", ":"), sort_keys=True).encode())
    print(f"wrote {output}: {len(artifact['tests'])} tests, {len(artifact['files'])} files, "
          f"{len(artifact['incomplete'])} incomplete tests, pytest exit {exit_code}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
