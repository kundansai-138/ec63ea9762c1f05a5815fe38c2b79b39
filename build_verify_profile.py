"""Measure what it costs to run each baseline test in-process.

Run inside an image that holds the pinned sqlglot checkout and the locked test
dependencies (the submission image, or Dockerfile.map's):

    python build_verify_profile.py --source /opt/sqlglot --tests sample/tests.json \
        --output verify-profile.json

Every test runs once on the unmodified checkout through runner.py, exactly as
the selector runs tests on a patched tree. The profile records, per test:

- CPU seconds, divided by this machine's speed relative to the recorded
  durations, so the numbers are in the same units as tests.json;
- whether the test passes in-process at all. A test that fails here on
  unmodified code cannot be judged by running it and is never dropped.

The profile describes the pinned commit only. It holds no patch, no failure
label and no outcome of any proposed change.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

import runner

PINNED_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def measure(source: str, tests: list[dict], work: str) -> dict:
    by_file: dict[str, list[dict]] = {}
    for test in tests:
        by_file.setdefault(test["file"], []).append(test)
    cpu: dict[str, float] = {}
    failed: list[str] = []
    for index, (path, group) in enumerate(by_file.items()):
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                os.close(read_fd)
                out = os.fdopen(write_fd, "w", buffering=1)
                sink = os.open(os.devnull, os.O_WRONLY)
                os.dup2(sink, 1)
                os.dup2(sink, 2)
                tree = os.path.join(work, "p%d" % index)
                runner.build_tree(source, tree, {})

                def report(kind, name, seconds):
                    out.write("%s\t%s\t%.5f\n" % (kind, name, seconds))

                runner.run_tests(tree, [(t["id"], t["seconds"]) for t in group], report,
                                 cpu_factor=1000.0, cpu_floor=3600.0)
            finally:
                os._exit(0)
        os.close(write_fd)
        outcome: dict[str, str] = {}
        with os.fdopen(read_fd) as stream:
            for line in stream:
                kind, name, seconds = line.rstrip("\n").split("\t")
                if kind in ("P", "F", "T"):
                    outcome[name] = kind
                    if float(seconds) >= 0:
                        cpu[name] = float(seconds)
        os.waitpid(pid, 0)
        for test in group:
            if outcome.get(test["id"]) != "P":
                failed.append(test["id"])
        print("%-45s %4d tests" % (path, len(group)), file=sys.stderr)
    return {"cpu": cpu, "failed": failed}


def build(raw: dict, tests: list[dict]) -> dict:
    """CPU seconds as measured. Build on an idle machine of about the grader's
    speed: busy-machine timings come out several times too high."""
    recorded = {test["id"]: test["seconds"] for test in tests}
    ratios = [raw["cpu"][name] / seconds for name, seconds in recorded.items()
              if name in raw["cpu"] and 0.02 <= seconds <= 1.0 and name not in raw["failed"]]
    return {
        "version": 1,
        "commit": PINNED_COMMIT,
        "machine_ratio": round(statistics.median(ratios), 4),
        "cpu": {name: round(seconds, 4) for name, seconds in sorted(raw["cpu"].items())},
        "unverifiable": sorted(raw["failed"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="/opt/sqlglot")
    parser.add_argument("--tests", type=Path, required=True)
    parser.add_argument("--work", default="/tmp/profile")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    tests = json.loads(args.tests.read_text("utf-8"))["tests"]
    os.makedirs(args.work, exist_ok=True)
    started = time.time()
    profile = build(measure(args.source, tests, args.work), tests)
    args.output.write_text(json.dumps(profile, separators=(",", ":")) + "\n", "utf-8")
    print("profiled %d tests in %.0fs; ratio %.2f; %d unverifiable" % (
        len(profile["cpu"]), time.time() - started, profile["machine_ratio"],
        len(profile["unverifiable"])), file=sys.stderr)


if __name__ == "__main__":
    main()
