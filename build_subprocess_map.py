"""Supplement baseline dependencies for tests that launch Python children.

Only the unchanged pinned source is executed. Each supported child's imported,
executed or opened source file becomes a whole-file dependency of its parent
test. Unsupported commands, nested subprocesses, failed tests and missing exit
reports preserve the existing incomplete-test fallback.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"

CHILD_HOOK = r'''
import atexit, json, os, sys, threading
_root = os.environ.get("BLAST_TRACE_SOURCE", "")
_report = os.environ.get("BLAST_TRACE_REPORT", "")
_files, _nested = set(), []
def _record(path):
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if isinstance(path, str):
        if path.startswith("<"):
            return
        path = os.path.abspath(path)
        if path.startswith(_root + os.sep):
            _files.add(os.path.relpath(path, _root).replace(os.sep, "/"))
def _profile(frame, event, arg):
    if event == "call":
        _record(frame.f_code.co_filename)
def _audit(event, args):
    if event == "open":
        _record(args[0])
    elif event in {"subprocess.Popen", "os.fork", "os.forkpty", "os.posix_spawn"}:
        _nested.append(True)
def _finish():
    complete = sys.getprofile() is _profile and threading.getprofile() is _profile
    sys.setprofile(None)
    threading.setprofile(None)
    with open(_report, "w") as stream:
        json.dump({"files": sorted(_files), "nested": bool(_nested), "complete": complete}, stream)
if _root and _report:
    sys.addaudithook(_audit)
    sys.setprofile(_profile)
    threading.setprofile(_profile)
    atexit.register(_finish)
'''


def supports_command(command, kwargs):
    if (kwargs.get("shell") or kwargs.get("executable") or not isinstance(command, (list, tuple))
            or not command or not isinstance(command[0], (str, os.PathLike))):
        return False
    name = Path(command[0]).name
    if name not in {"python", "python3", Path(sys.executable).name}:
        return False
    # Isolated/startup-disabled modes do not load our sitecustomize module.
    for arg in command[1:]:
        if arg in {"-c", "-m"} or not str(arg).startswith("-"):
            break
        if any(flag in str(arg)[1:] for flag in "IES"):
            return False
    return True


def verify_source(source, manifest):
    if manifest.get("commit") != PIN:
        raise ValueError("source manifest is not the pinned baseline")
    for name, expected in manifest["files"].items():
        path = source / name
        if not path.resolve().is_relative_to(source):
            raise ValueError("source manifest escapes the baseline")
        value = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
        actual = hashlib.sha1(f"blob {len(value)}\0".encode() + value).hexdigest()
        if actual != expected:
            raise ValueError(f"source differs from pinned baseline: {name}")


def run_test(source, node, destination):
    import pytest

    original = subprocess.Popen
    reports, unsupported = [], []
    outcomes = []
    class Outcomes:
        def pytest_runtest_logreport(self, report):
            if report.nodeid == node and (report.when == "call" or report.failed or report.skipped):
                outcomes.append(report.outcome)
    active = True
    def audit(event, arguments):
        if active and event in {"os.fork", "os.forkpty", "os.posix_spawn"}:
            unsupported.append(True)
    sys.addaudithook(audit)
    with tempfile.TemporaryDirectory(prefix="baseline-child-") as scratch:
        hookdir = Path(scratch)
        (hookdir / "sitecustomize.py").write_text(CHILD_HOOK, encoding="utf-8")

        def traced(command, *args, **kwargs):
            if args or not supports_command(command, kwargs):
                unsupported.append(True)
                return original(command, *args, **kwargs)
            report = hookdir / f"child-{len(reports)}.json"
            reports.append(report)
            env = dict(os.environ if kwargs.get("env") is None else kwargs["env"])
            env.update(BLAST_TRACE_SOURCE=str(source), BLAST_TRACE_REPORT=str(report),
                       PYTHONPATH=str(hookdir) + os.pathsep + env.get("PYTHONPATH", ""))
            kwargs["env"] = env
            return original(command, **kwargs)

        subprocess.Popen = traced
        try:
            status = pytest.main(["-q", "--tb=short", "-p", "no:cacheprovider", node], plugins=[Outcomes()])
        finally:
            subprocess.Popen = original
            active = False
        rows = [json.loads(path.read_text()) for path in reports if path.exists()]
        complete = (status == 0 and outcomes and set(outcomes) == {"passed"}
                    and reports and not unsupported and len(rows) == len(reports)
                    and all(row.get("complete") and not row.get("nested") for row in rows))
        result = {"node": node, "complete": bool(complete), "pytest_exit": int(status), "outcomes": outcomes,
                  "children": len(reports), "files": sorted({p for row in rows for p in row["files"]})}
        destination.write_text(json.dumps(result, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--map", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--child-test")
    args = parser.parse_args()
    source = args.source.resolve()
    if args.child_test:
        sys.path.insert(0, str(source))
        os.chdir(source)
        run_test(source, args.child_test, args.output)
        return
    manifest = json.loads(args.source_manifest.read_text())
    verify_source(source, manifest)
    mapping = json.load(gzip.open(args.map, "rt", encoding="utf-8"))
    if mapping.get("commit") != PIN or mapping["metadata"].get("pytest_exit_code") != 0:
        raise ValueError("only a passing pinned baseline map may be supplemented")
    candidates = mapping["metadata"].get("untraced_subprocess_tests", [])
    results = []
    with tempfile.TemporaryDirectory(prefix="subprocess-evidence-") as scratch:
        for position, node in enumerate(candidates):
            destination = Path(scratch) / f"{position}.json"
            env = {**os.environ, "SKIP_INTEGRATION": "0", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                   "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(source)}
            subprocess.run([sys.executable, str(Path(__file__).resolve()), "--source", str(source),
                            "--output", str(destination), "--child-test", node],
                           env=env, check=True, timeout=90)
            result = json.loads(destination.read_text())
            results.append(result)
            if not result["complete"]:
                continue
            index = mapping["tests"].index(node)
            for name in result["files"]:
                # Ignore generated bytecode; executed Python files are also recorded.
                if name in mapping["files"]:
                    entry = mapping["files"][name]
                    entry["reads"] = sorted(set(entry.get("reads", [])) | {index})
            mapping["incomplete"] = [item for item in mapping["incomplete"] if item != node]
    mapping["metadata"]["subprocess_dependency_supplement"] = results
    mapping["metadata"]["untraced_subprocess_tests"] = [row["node"] for row in results if not row["complete"]]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("wb") as destination:
        with gzip.GzipFile(fileobj=destination, mode="wb", mtime=0) as stream:
            stream.write(json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode())
    print(json.dumps({"supplemented": sum(row["complete"] for row in results),
                      "remaining_incomplete": mapping["incomplete"]}))


if __name__ == "__main__":
    main()
