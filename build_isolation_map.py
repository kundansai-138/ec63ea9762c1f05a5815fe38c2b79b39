"""Record what each baseline test depends on when it runs alone.

Run inside an image holding the pinned sqlglot checkout and locked test
dependencies:

    python build_isolation_map.py --source /opt/sqlglot --tests sample/tests.json \
        --output isolation-map.json.gz --jobs 12

Every test runs once, by itself, in a fresh process on the unmodified checkout.
Because nothing was imported or cached by an earlier test, the record holds
everything the test needs, including work that a shared process would have done
once and reused:

- the sqlglot modules the process imported;
- the source lines executed while a module was being imported ("import phase");
- the source lines executed otherwise ("test phase").

The map describes the pinned commit only. It contains no patch, no failure
label and no outcome of any proposed change.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import sys
import time
import zlib
from pathlib import Path

import runner

PINNED_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def trace_one(source: str, tree: str, test: dict, out_path: str) -> None:
    """Child process: run one test with line monitoring and write its record."""
    runner.build_tree(source, tree, {})
    prefix = tree + "/"
    monitoring = sys.monitoring
    tool = monitoring.COVERAGE_ID
    events = monitoring.events
    import_lines: set = set()
    test_lines: set = set()
    state = {"depth": 0}

    def on_line(code, line):
        name = code.co_filename
        if name.startswith(prefix):
            (import_lines if state["depth"] else test_lines).add((name, line))
        return monitoring.DISABLE

    def on_start(code, offset):
        if code.co_name == "<module>" and code.co_filename.startswith(prefix):
            state["depth"] += 1
            monitoring.restart_events()
            return None
        return monitoring.DISABLE

    def on_exit(code, offset, value):
        if code.co_name == "<module>" and code.co_filename.startswith(prefix):
            state["depth"] -= 1
            monitoring.restart_events()
            return None
        return monitoring.DISABLE

    def on_unwind(code, offset, exception):
        if code.co_name == "<module>" and code.co_filename.startswith(prefix):
            state["depth"] -= 1
            monitoring.restart_events()

    monitoring.use_tool_id(tool, "isolation-map")
    monitoring.register_callback(tool, events.LINE, on_line)
    monitoring.register_callback(tool, events.PY_START, on_start)
    monitoring.register_callback(tool, events.PY_RETURN, on_exit)
    monitoring.register_callback(tool, events.PY_UNWIND, on_unwind)
    monitoring.set_events(tool, events.LINE | events.PY_START | events.PY_RETURN | events.PY_UNWIND)

    result = {}

    def report(kind, name, cpu):
        if kind in ("P", "F", "T"):
            result["outcome"] = kind
            if cpu >= 0:
                result["cpu"] = cpu

    started = time.process_time()
    runner.run_tests(tree, [(test["id"], test["seconds"])], report, cpu_factor=1000.0, cpu_floor=3600.0)
    monitoring.set_events(tool, 0)
    total = time.process_time() - started

    def relative(pairs):
        grouped: dict[str, list[int]] = {}
        for name, line in pairs:
            grouped.setdefault(name[len(prefix):], []).append(line)
        return {name: sorted(lines) for name, lines in grouped.items()}

    modules = sorted(
        os.path.relpath(module.__file__, tree).replace(os.sep, "/")
        for name, module in list(sys.modules.items())
        if (name == "sqlglot" or name.startswith("sqlglot."))
        and getattr(module, "__file__", None) and module.__file__.startswith(prefix)
    )
    record = {"id": test["id"], "outcome": result.get("outcome", "F"), "cpu": result.get("cpu", -1.0),
              "process_cpu": total, "modules": modules,
              "import_lines": relative(import_lines), "test_lines": relative(test_lines)}
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(record, handle)


def run_all(source: str, tests: list[dict], work: str, jobs: int) -> list[dict]:
    os.makedirs(work, exist_ok=True)
    pending = list(enumerate(tests))
    # Longest first, so the slow tests do not end up running alone at the end.
    pending.sort(key=lambda item: -item[1]["seconds"])
    active: dict[int, tuple[int, str]] = {}
    records: list[dict | None] = [None] * len(tests)
    done = 0
    while pending or active:
        while pending and len(active) < jobs:
            index, test = pending.pop(0)
            out_path = os.path.join(work, "r%d.json" % index)
            pid = os.fork()
            if pid == 0:
                code = 1
                try:
                    sink = os.open(os.devnull, os.O_WRONLY)
                    os.dup2(sink, 1)
                    os.dup2(sink, 2)
                    trace_one(source, os.path.join(work, "t%d" % index), test, out_path)
                    code = 0
                finally:
                    os._exit(code)
            active[pid] = (index, out_path)
        pid, _ = os.wait()
        index, out_path = active.pop(pid)
        try:
            with open(out_path, encoding="utf-8") as handle:
                records[index] = json.load(handle)
            os.unlink(out_path)
        except (OSError, ValueError):
            records[index] = {"id": tests[index]["id"], "outcome": "X", "cpu": -1.0, "process_cpu": -1.0,
                              "modules": [], "import_lines": {}, "test_lines": {}}
        import shutil

        shutil.rmtree(os.path.join(work, "t%d" % index), ignore_errors=True)
        done += 1
        if done % 100 == 0:
            print("%d/%d tests traced" % (done, len(tests)), file=sys.stderr)
    return records  # type: ignore[return-value]


class SetTable:
    """Stores each distinct set of test indices once, as a bit string."""

    def __init__(self, size: int):
        self.width = (size + 7) // 8
        self.index: dict[bytes, int] = {}
        self.rows: list[bytes] = []

    def add(self, members) -> int:
        bits = bytearray(self.width)
        for member in members:
            bits[member >> 3] |= 1 << (member & 7)
        key = bytes(bits)
        found = self.index.get(key)
        if found is None:
            found = self.index[key] = len(self.rows)
            self.rows.append(key)
        return found

    def dump(self) -> str:
        return base64.b64encode(zlib.compress(b"".join(self.rows), 9)).decode("ascii")


def assemble(records: list[dict]) -> dict:
    table = SetTable(len(records))
    importers: dict[str, list[int]] = {}
    by_line: dict[str, dict[str, dict[int, list[int]]]] = {"import_lines": {}, "test_lines": {}}
    for index, record in enumerate(records):
        for module in record["modules"]:
            importers.setdefault(module, []).append(index)
        for phase in ("import_lines", "test_lines"):
            for path, lines in record[phase].items():
                target = by_line[phase].setdefault(path, {})
                for line in lines:
                    target.setdefault(line, []).append(index)
    files: dict[str, dict] = {}
    for phase, short in (("import_lines", "import"), ("test_lines", "test")):
        for path, lines in by_line[phase].items():
            ordered = sorted(lines)
            files.setdefault(path, {})[short] = [ordered, [table.add(lines[line]) for line in ordered]]
    return {
        "version": 1,
        "commit": PINNED_COMMIT,
        "tests": [record["id"] for record in records],
        "failed_alone": [record["id"] for record in records if record["outcome"] != "P"],
        "cpu": [round(record["cpu"], 4) for record in records],
        "process_cpu": [round(record["process_cpu"], 4) for record in records],
        "importers": {module: table.add(indices) for module, indices in sorted(importers.items())},
        "files": files,
        "set_width": table.width,
        "sets": table.dump(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="/opt/sqlglot")
    parser.add_argument("--tests", type=Path, required=True)
    parser.add_argument("--work", default="/tmp/isolation")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--only", help="substring of test ids, for development")
    args = parser.parse_args()
    tests = json.loads(args.tests.read_text("utf-8"))["tests"]
    if args.only:
        tests = [test for test in tests if args.only in test["id"]]
    started = time.time()
    records = run_all(args.source, tests, args.work, args.jobs)
    result = assemble(records)
    result["metadata"] = {"elapsed_seconds": round(time.time() - started, 1), "python": sys.version.split()[0],
                          "subset": args.only}
    with gzip.open(args.output, "wt", encoding="utf-8", compresslevel=9) as handle:
        json.dump(result, handle, separators=(",", ":"))
    print("traced %d tests in %.0fs; %d failed alone; %d distinct test sets" % (
        len(tests), time.time() - started, len(result["failed_alone"]), len(result["sets"]) // 1), file=sys.stderr)


if __name__ == "__main__":
    main()
