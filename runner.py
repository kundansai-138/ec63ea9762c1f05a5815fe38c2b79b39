"""Execute selected sqlglot tests against one patched source tree.

The static selector names the tests a change can reach. This module runs those
tests on the changed sources and reports which of them fail, so the answer can
keep only real failures. Nothing here knows about particular patches: it builds
a tree, imports the test modules from it and runs unittest.

Outcomes follow the challenge's definition of a failing test: a failure, an
error, a failing subtest, an unexpected success, a timeout, or a test file that
cannot be imported. A test this module cannot judge is reported as failing so
that it stays selected.
"""
from __future__ import annotations

import os
import pickle
import signal
import sys
import time
import unittest

FAIL = "fail"
PASS = "pass"


class TestTimeout(BaseException):
    """Raised inside a test whose CPU budget ran out."""


def build_tree(source_root: str, target_root: str, replacements: dict[str, bytes]) -> None:
    """Create target_root as a view of source_root with some files replaced.

    Unchanged entries are symbolic links, so only directories on the way to a
    replaced file are real. A replaced module's cached bytecode is ignored by
    the import system because its recorded source time no longer matches.
    """
    nested: dict = {}
    for path, content in replacements.items():
        node = nested
        parts = path.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ValueError("file used as directory: " + path)
        node[parts[-1]] = content

    def build(source: str, target: str, subtree: dict) -> None:
        os.mkdir(target)
        present = set()
        if os.path.isdir(source):
            for name in os.listdir(source):
                present.add(name)
                node = subtree.get(name)
                if node is None:
                    os.symlink(os.path.join(source, name), os.path.join(target, name))
                elif isinstance(node, dict):
                    build(os.path.join(source, name), os.path.join(target, name), node)
                else:
                    with open(os.path.join(target, name), "wb") as handle:
                        handle.write(node)
        for name, node in subtree.items():
            if name in present:
                continue
            if isinstance(node, dict):
                build(os.path.join(source, name), os.path.join(target, name), node)
            else:
                with open(os.path.join(target, name), "wb") as handle:
                    handle.write(node)

    build(source_root, target_root, nested)


class InlineProcessPool:
    """Stand-in for ProcessPoolExecutor that runs each job in this process.

    The executor tests hand their queries to a spawned pool. Running the jobs
    here keeps the work on one CPU and inside the patched tree. Arguments and
    results still make a pickle round trip, as they would between processes.
    """

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=()):
        self._initializer = initializer
        self._initargs = initargs
        self._started = False

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def shutdown(self, wait=True, cancel_futures=False):
        return None

    def _start(self):
        if self._started:
            return
        self._started = True
        if self._initializer is not None:
            # The spawned worker receives pickled initializer arguments. Check a
            # shallow copy of each container for picklability, then pass the
            # originals: the large fixture tables need not be duplicated.
            pickle.loads(pickle.dumps(_sample_for_pickle(self._initargs)))
            self._initializer(*self._initargs)

    def submit(self, fn, /, *args, **kwargs):
        # A real, already finished Future: the tests pass these to wait() and
        # as_completed(), which need the standard library's own type.
        from concurrent.futures import Future

        future = Future()
        try:
            self._start()
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            value = pickle.loads(pickle.dumps(fn(*args, **kwargs)))
        except (TestTimeout, KeyboardInterrupt):
            raise
        except BaseException as error:  # noqa: BLE001 - delivered through result()
            future.set_exception(error)
        else:
            future.set_result(value)
        return future

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        futures = [self.submit(fn, *items) for items in zip(*iterables)]
        return (future.result() for future in futures)


def _sample_for_pickle(value, depth=0):
    """A small stand-in with the same types, for a cheap picklability check."""
    if depth > 3:
        return None
    if isinstance(value, dict):
        return {k: _sample_for_pickle(v, depth + 1) for k, v in list(value.items())[:64]}
    if isinstance(value, (list, tuple)) and len(value) > 8:
        return type(value)(value[:8]) if type(value) in (list, tuple) else value
    rows = getattr(value, "rows", None)
    if isinstance(rows, list) and len(rows) > 8:
        import copy

        clone = copy.copy(value)
        try:
            clone.rows = rows[:8]
        except Exception:  # noqa: BLE001
            return value
        return clone
    return value


def node_id(test: unittest.TestCase) -> str | None:
    """unittest id 'tests.a.test_b.Class.method' -> 'tests/a/test_b.py::Class::method'."""
    parts = test.id().split(".")
    if len(parts) < 3:
        return None
    return "/".join(parts[:-2]) + ".py::" + parts[-2] + "::" + parts[-1]


class Recorder(unittest.TestResult):
    def __init__(self, wanted: dict[str, float], report, cpu_factor: float, cpu_floor: float):
        super().__init__()
        self.wanted = wanted
        self.report = report
        self.cpu_factor = cpu_factor
        self.cpu_floor = cpu_floor
        self.outcomes: dict[str, str] = {}
        self.current: str | None = None
        self.started = 0.0
        self.timed_out = False

    # --- timing -----------------------------------------------------------
    def _alarm(self, signum, frame):
        self.timed_out = True
        if self.current is not None:
            self.outcomes[self.current] = FAIL
            self.report("T", self.current, 0.0)
        # State after an interrupted test is not trustworthy: stop the run. The
        # caller restarts a fresh process for whatever was not reached.
        self.report("X", "", 0.0)
        os._exit(3)

    def startTest(self, test):
        super().startTest(test)
        self.current = node_id(test)
        self.started = time.process_time()
        if self.current is not None:
            self.report("S", self.current, 0.0)
            budget = max(self.cpu_floor, self.cpu_factor * self.wanted.get(self.current, 0.0))
            signal.setitimer(signal.ITIMER_PROF, budget)

    def stopTest(self, test):
        signal.setitimer(signal.ITIMER_PROF, 0)
        if self.current is not None:
            outcome = self.outcomes.setdefault(self.current, PASS)
            self.report("P" if outcome == PASS else "F", self.current, time.process_time() - self.started)
        self.current = None
        super().stopTest(test)

    # --- outcomes ---------------------------------------------------------
    def _mark(self, test):
        name = node_id(test) if isinstance(test, unittest.TestCase) else None
        if name is not None and name in self.wanted:
            self.outcomes[name] = FAIL
            return
        # A class or module fixture failed: unittest hands over a placeholder
        # whose description names the scope, e.g. "setUpClass (tests.x.Class)".
        text = str(test)
        scope = text[text.find("(") + 1:text.rfind(")")] if "(" in text else ""
        prefix = scope.replace(".", "/")
        hit = False
        for wanted in self.wanted:
            module, _, rest = wanted.partition(".py::")
            dotted = module.replace("/", ".") + "." + rest.split("::")[0]
            if dotted == scope or module.replace("/", ".") == scope or (prefix and module == prefix):
                self.outcomes[wanted] = FAIL
                hit = True
        if not hit:
            for wanted in self.wanted:
                self.outcomes[wanted] = FAIL

    def addError(self, test, err):
        super().addError(test, err)
        self._mark(test)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._mark(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._mark(test)
        super().addSubTest(test, subtest, err)

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._mark(test)


def run_tests(root: str, tests: list[tuple[str, float]], report, cpu_factor: float = 6.0,
              cpu_floor: float = 3.0) -> dict[str, str]:
    """Run node ids (in the given order) from the tree at root.

    tests holds (node id, recorded seconds). report(kind, node id, cpu seconds)
    is called as each test starts ("S") and ends ("P" passed, "F" failed,
    "T" timed out), so a caller that loses this process knows how far it got.
    """
    os.chdir(root)
    sys.path.insert(0, root)
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTHONPATH"] = root
    sys.dont_write_bytecode = True

    import concurrent.futures

    concurrent.futures.ProcessPoolExecutor = InlineProcessPool
    try:
        import concurrent.futures.process as process_module

        process_module.ProcessPoolExecutor = InlineProcessPool
    except Exception:  # noqa: BLE001
        pass

    wanted = dict(tests)
    recorder = Recorder(wanted, report, cpu_factor, cpu_floor)
    signal.signal(signal.SIGPROF, recorder._alarm)

    suite = unittest.TestSuite()
    modules: dict[str, object] = {}
    for name, _ in tests:
        path, _, rest = name.partition("::")
        class_name, _, method = rest.partition("::")
        module_name = path[:-3].replace("/", ".")
        if module_name not in modules:
            try:
                __import__(module_name)
                modules[module_name] = sys.modules[module_name]
            except BaseException as error:  # noqa: BLE001 - the file cannot be imported
                if isinstance(error, (KeyboardInterrupt, TestTimeout)):
                    raise
                modules[module_name] = None
        module = modules[module_name]
        if module is None:
            recorder.outcomes[name] = FAIL
            report("F", name, 0.0)
            continue
        try:
            case = getattr(module, class_name)(method)
        except BaseException as error:  # noqa: BLE001 - class or method is gone
            if isinstance(error, (KeyboardInterrupt, TestTimeout)):
                raise
            recorder.outcomes[name] = FAIL
            report("F", name, 0.0)
            continue
        suite.addTest(case)

    suite.run(recorder)
    signal.setitimer(signal.ITIMER_PROF, 0)
    if os.environ.get("BR_DEBUG"):
        for case, text in recorder.errors + recorder.failures:
            print("FAILED", case, "\n", text[-1500:], file=sys.stderr)
    for name in wanted:
        if name not in recorder.outcomes:
            # Never started, for example because a class fixture failed.
            recorder.outcomes[name] = FAIL
    # Fixture failures are attributed after the tests they affect have ended,
    # so send the final word on every test.
    for name, outcome in recorder.outcomes.items():
        report("P" if outcome == PASS else "F", name, -1.0)
    return recorder.outcomes
