"""Add collection-time callable evidence from the unmodified pinned baseline.

Only pytest collection runs here. Existing per-test line coverage already
overapproximates lambda calls; the missing distinction is whether an import
line constructed a lambda or actually entered its body. No patches, answers,
or test numbers are inputs to this builder.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import threading
import time

import pytest

PINNED_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


class CollectionCalls:
    def __init__(self, source):
        self.prefix = str(source.resolve()) + os.sep
        self.calls = defaultdict(set)
        self.nodes = []
        self.errors = []
        self._paths = {}

    def profile(self, frame, event, argument):
        if event != "call":
            return
        code = frame.f_code
        filename = code.co_filename
        if filename not in self._paths:
            absolute = os.path.abspath(filename)
            relative = absolute[len(self.prefix):].replace(os.sep, "/") if absolute.startswith(self.prefix) else None
            self._paths[filename] = relative if relative and relative.startswith("sqlglot/") else None
        relative = self._paths[filename]
        if relative:
            self.calls[relative].add(code.co_firstlineno)

    def pytest_collection_modifyitems(self, session, config, items):
        self.nodes = sorted(item.nodeid for item in items)

    def pytest_collectreport(self, report):
        if report.failed:
            self.errors.append(report.nodeid)

    def record(self):
        previous, previous_thread = sys.getprofile(), threading.getprofile()
        # A bound method is freshly allocated at every attribute access; keep
        # the installed callback identity so replacement cannot go unnoticed.
        callback = self.profile
        sys.setprofile(callback)
        threading.setprofile(callback)
        try:
            result = int(pytest.main(["--collect-only", "-q", "--tb=short", "-p", "no:cacheprovider", "tests"], plugins=[self]))
            if sys.getprofile() is not callback or threading.getprofile() is not callback:
                raise RuntimeError("collection replaced the callable profiler; evidence is incomplete")
            return result
        finally:
            sys.setprofile(previous)
            threading.setprofile(previous_thread)


def verify_source(source, manifest):
    if manifest.get("commit") != PINNED_COMMIT:
        raise ValueError("manifest is not the pinned baseline")
    for name, expected in manifest["files"].items():
        raw_path = source / name
        path = raw_path.resolve()
        if not path.is_relative_to(source) or not path.is_file():
            raise ValueError("invalid baseline manifest path: " + name)
        content = os.readlink(raw_path).encode("utf-8") if raw_path.is_symlink() else path.read_bytes()
        digest = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
        if digest != expected:
            raise ValueError("source does not match pinned Git blob: " + name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--source-manifest", required=True, type=Path)
    parser.add_argument("--map", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    mapping = json.load(gzip.open(args.map, "rt", encoding="utf-8"))
    if mapping.get("commit") != PINNED_COMMIT or mapping.get("metadata", {}).get("test_subset"):
        parser.error("a complete pinned-baseline coverage map is required")
    verify_source(source, json.loads(args.source_manifest.read_text("utf-8")))
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path.insert(0, str(source))
    os.chdir(source)
    recorder = CollectionCalls(source)
    started = time.monotonic()
    code = recorder.record()
    if code or recorder.errors or recorder.nodes != mapping["tests"]:
        raise RuntimeError(f"baseline collection differs: exit={code}, errors={recorder.errors}, tests={len(recorder.nodes)}")
    for path, entry in mapping["files"].items():
        if path.endswith(".py"):
            entry["collection_callable_lines"] = sorted(recorder.calls.get(path, ()))
    mapping["callable_collection_version"] = 1
    mapping["metadata"]["callable_collection"] = {
        "method": "Python call events during complete pinned-baseline pytest collection, including new threads",
        "python": platform.python_version(), "pytest": pytest.__version__,
        "pytest_exit_code": code, "tests": len(recorder.nodes),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "callable_entries": sum(len(lines) for lines in recorder.calls.values()),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as destination:
        with gzip.GzipFile(fileobj=destination, mode="wb", mtime=0) as compressed:
            compressed.write(json.dumps(mapping, separators=(",", ":"), sort_keys=True).encode())
    print(json.dumps(mapping["metadata"]["callable_collection"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
