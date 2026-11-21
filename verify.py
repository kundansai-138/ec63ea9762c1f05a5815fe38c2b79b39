"""Run candidate tests on patched sources in forked workers.

Each job is one commit: the files its patch replaces and the tests that the
static selector says the change can reach. A forked child builds the patched
tree under /tmp and runs those tests (see runner.py). The parent reads one line
per test from a pipe, enforces time limits and restarts a child that died.

Every test ends in one of two states. "pass" means it ran to completion on the
patched tree without a failure. Anything else, including a test that could not
be run or judged, is "fail" and therefore stays selected.
"""
from __future__ import annotations

import gc
import os
import select
import shutil
import signal
import sys
import time

import runner

PASS = runner.PASS
FAIL = runner.FAIL

# Standard-library modules that importing sqlglot and unittest pulls in on the
# pinned commit, and the extra packages particular test files import.
COMMON_MODULES = (
    "argparse", "ast", "asyncio", "base64", "bisect", "concurrent.futures",
    "concurrent.futures.process", "contextlib", "contextvars", "copy", "dataclasses",
    "datetime", "decimal", "difflib", "dis", "enum", "fnmatch", "fractions", "functools",
    "glob", "heapq", "importlib.metadata", "importlib.resources", "importlib.util", "inspect",
    "itertools", "linecache", "locale", "logging", "math", "numbers", "pathlib", "pickle",
    "pkgutil", "pprint", "random", "re", "shutil", "signal", "statistics", "string", "struct",
    "subprocess", "tempfile", "textwrap", "threading", "tokenize", "traceback", "typing",
    "unittest", "unittest.mock", "warnings", "weakref", "zipfile", "mypy_extensions",
)
EXTRA_MODULES = {
    "tests/test_executor.py": ("csv", "gzip", "multiprocessing", "numpy", "pandas",
                               "pandas.testing", "duckdb"),
    "tests/test_optimizer.py": ("multiprocessing", "numpy", "pandas", "pandas.testing", "duckdb"),
    "tests/dialects/test_bigquery.py": ("pytz",),
    "tests/dialects/test_sqlite.py": ("sqlite3",),
    "tests/test_generator.py": ("tracemalloc",),
    "tests/test_sqlglotc.py": ("runpy", "setuptools", "setuptools.command.build_ext"),
}


class Job:
    def __init__(self, key, replacements: dict[str, bytes], tests: list[tuple[str, float]],
                 estimate: float):
        self.key = key
        self.replacements = replacements
        self.tests = tests                # (node id, recorded seconds), in collection order
        self.estimate = estimate          # predicted CPU seconds, for ordering only
        self.outcomes: dict[str, str] = {}
        self.cpu: dict[str, float] = {}
        self.restarts = 0
        self.timeouts = 0


class _Child:
    def __init__(self, job: Job, pid: int, fd: int, directory: str, now: float, allowance: float):
        self.job = job
        self.pid = pid
        self.fd = fd
        self.directory = directory
        self.buffer = b""
        self.current: str | None = None
        self.deadline = now + allowance   # wall clock limit for the step in progress
        self.finished = False


class Verifier:
    def __init__(self, source_root: str, work_root: str = "/tmp/br", concurrency: int = 2,
                 wall_deadline: float | None = None, cpu_factor: float = 6.0,
                 cpu_floor: float = 3.0, debug: bool = False):
        self.source_root = source_root
        self.work_root = work_root
        self.concurrency = max(1, concurrency)
        self.wall_deadline = wall_deadline
        self.cpu_factor = cpu_factor
        self.cpu_floor = cpu_floor
        self.debug = debug
        self.serial = 0
        self.recorded: dict[str, float] = {}

    # --- shared imports ---------------------------------------------------
    def preload(self, test_files: set[str]) -> None:
        """Import, once, the unchanging modules every child would import.

        Children are forked from this process and inherit them. Only the
        standard library and third-party packages are loaded here; sqlglot and
        its tests are always imported by the child from its patched tree.
        """
        names = list(COMMON_MODULES)
        for path in sorted(test_files):
            names.extend(EXTRA_MODULES.get(path, ()))
        for name in names:
            if name.startswith(("sqlglot", "tests")):
                continue
            try:
                __import__(name)
            except Exception:  # noqa: BLE001 - a child will meet the same error itself
                pass

    # --- child side -------------------------------------------------------
    def _spawn(self, job: Job, remaining: list[tuple[str, float]], now: float) -> _Child:
        self.serial += 1
        directory = os.path.join(self.work_root, "t%d" % self.serial)
        read_fd, write_fd = os.pipe()
        sys.stdout.flush()
        sys.stderr.flush()
        pid = os.fork()
        if pid == 0:
            code = 1
            try:
                os.close(read_fd)
                os.setpgid(0, 0)
                sink = os.open(os.devnull, os.O_WRONLY)
                source = os.open(os.devnull, os.O_RDONLY)
                os.dup2(source, 0)
                # Nothing a test prints may reach the answer stream.
                os.dup2(sink, 1)
                if not self.debug:
                    os.dup2(sink, 2)
                sys.stdout = open(os.devnull, "w")
                if not self.debug:
                    sys.stderr = open(os.devnull, "w")
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                out = os.fdopen(write_fd, "w", buffering=1, encoding="utf-8")

                def report(kind: str, name: str, cpu: float) -> None:
                    out.write("%s\t%s\t%.4f\n" % (kind, name, cpu))

                os.makedirs(self.work_root, exist_ok=True)
                runner.build_tree(self.source_root, directory, job.replacements)
                report("B", "", 0.0)
                runner.run_tests(directory, remaining, report, self.cpu_factor, self.cpu_floor)
                out.write("END\n")
                out.flush()
                code = 0
            except BaseException as error:  # noqa: BLE001
                if self.debug:
                    try:
                        import traceback

                        traceback.print_exc()
                    except Exception:  # noqa: BLE001
                        pass
                code = 2
            finally:
                os._exit(code)
        os.close(write_fd)
        os.set_blocking(read_fd, False)
        # Building the tree and importing the test modules precede the first test.
        return _Child(job, pid, read_fd, directory, now, 60.0)

    # --- parent side ------------------------------------------------------
    def _wall_allowance(self, name: str) -> float:
        # CPU limits are enforced inside the child; this only catches a child
        # that stopped making progress without using CPU.
        return max(self.cpu_floor, self.cpu_factor * self.recorded.get(name, 0.0)) * 3.0 + 20.0

    def _line(self, child: _Child, line: str, now: float) -> None:
        if line == "END":
            child.finished = True
            return
        kind, _, rest = line.partition("\t")
        name, _, cpu = rest.partition("\t")
        job = child.job
        if kind == "S":
            child.current = name
            child.deadline = now + self._wall_allowance(name)
        elif kind in ("P", "F", "T"):
            job.outcomes[name] = PASS if kind == "P" else FAIL
            try:
                value = float(cpu)
            except ValueError:
                value = -1.0
            if value >= 0:
                job.cpu[name] = value
            if kind == "T":
                job.timeouts += 1
            if child.current == name:
                child.current = None
                child.deadline = now + 60.0
        elif kind == "B":
            child.deadline = now + 60.0

    def _drain(self, child: _Child, now: float) -> bool:
        """Read what is available. Returns False once the pipe is closed."""
        while True:
            try:
                chunk = os.read(child.fd, 65536)
            except BlockingIOError:
                return True
            except OSError:
                return False
            if not chunk:
                return False
            child.buffer += chunk
            while b"\n" in child.buffer:
                raw, child.buffer = child.buffer.split(b"\n", 1)
                self._line(child, raw.decode("utf-8", "replace"), now)

    def _reap(self, child: _Child, kill: bool) -> None:
        if kill:
            for target in (child.pid,):
                try:
                    os.killpg(target, signal.SIGKILL)
                except OSError:
                    try:
                        os.kill(target, signal.SIGKILL)
                    except OSError:
                        pass
        try:
            os.waitpid(child.pid, 0)
        except OSError:
            pass
        try:
            # Processes a test left behind must not keep using CPU.
            os.killpg(child.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.close(child.fd)
        except OSError:
            pass
        shutil.rmtree(child.directory, ignore_errors=True)

    def run(self, jobs: list[Job]) -> None:
        """Fill job.outcomes for every job. Missing entries mean 'not verified'."""
        for job in jobs:
            for name, seconds in job.tests:
                self.recorded[name] = seconds
        gc.collect()
        gc.freeze()  # keep the parent's heap out of every child's collector
        pending = sorted(jobs, key=lambda job: -job.estimate)
        active: list[_Child] = []
        try:
            while pending or active:
                now = time.monotonic()
                if self.wall_deadline is not None and now >= self.wall_deadline:
                    break
                while pending and len(active) < self.concurrency:
                    job = pending.pop(0)
                    remaining = [item for item in job.tests if item[0] not in job.outcomes]
                    if not remaining:
                        continue
                    active.append(self._spawn(job, remaining, now))
                if not active:
                    continue
                limit = min(child.deadline for child in active)
                if self.wall_deadline is not None:
                    limit = min(limit, self.wall_deadline)
                wait = max(0.0, min(1.0, limit - now))
                ready, _, _ = select.select([child.fd for child in active], [], [], wait)
                now = time.monotonic()
                for child in list(active):
                    open_pipe = True
                    if child.fd in ready:
                        open_pipe = self._drain(child, now)
                    expired = now >= child.deadline
                    if open_pipe and not expired and not child.finished:
                        continue
                    active.remove(child)
                    self._reap(child, kill=open_pipe and not child.finished)
                    job = child.job
                    if child.finished:
                        continue
                    # The child stopped early: a timeout it reported, a crash,
                    # or a hang this loop ended. Whatever was running failed.
                    if child.current is not None and child.current not in job.outcomes:
                        job.outcomes[child.current] = FAIL
                        job.timeouts += 1
                    job.restarts += 1
                    left = [item for item in job.tests if item[0] not in job.outcomes]
                    if left and job.restarts <= 2 and job.timeouts <= 2:
                        pending.insert(0, job)
        finally:
            for child in active:
                self._reap(child, kill=True)
            gc.unfreeze()
