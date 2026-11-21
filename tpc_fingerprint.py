"""Fingerprint what sqlglot's TPC executor tests compute, without executing queries.

test_execute_tpcds (21.5 s recorded) and test_execute_tpch (3.9 s) parse each
fixture query, optimize it, build a logical plan, compile Python code for every
plan step and run that code over CSV tables. The expected result comes from
DuckDB running the query transpiled by sqlglot. test_optimized_tpch runs both
the original and the fixture's optimized SQL through DuckDB.

Given unchanged data and unchanged executor code, those results are a function
of what sqlglot produces before execution starts:

- the parsed expression, also after a pickle round trip (the queries cross a
  process boundary in the test);
- the optimized expression and the plan built from it, step by step;
- the Python source generated for every expression the executor compiles,
  including the plans of subqueries it replaces;
- the DuckDB SQL used for the expected result;
- the executor's run-time constants from sqlglot (type groups, time units).

This module computes all of that on a sqlglot tree and reduces it to one hash
per test. The selector compares a patched tree's hashes with the pinned
commit's, computed once when the image is built. Equal hashes, together with a
patch that leaves the executor, sqlglot.helper, the tests and the fixtures
untouched, mean the test performs the same computation and gets the same result
as on the pinned commit, where it passes.

Usage (run from inside the tree, with the tree first on sys.path):
    python tpc_fingerprint.py <tree> <output json>
"""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import sys

TESTS = {
    "tpcds": "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "tpch": "tests/test_executor.py::TestExecutor::test_execute_tpch",
    "optimized_tpch": "tests/test_executor.py::TestExecutor::test_optimized_tpch",
    "max_nodes": "tests/test_parser.py::TestParser::test_max_nodes",
}

# A patch touching any of these is never judged by fingerprint.
PROTECTED_PREFIXES = (
    "sqlglot/executor/",
    "sqlglot/helper.py",
    "tests/",
)


def eligible(paths) -> bool:
    return bool(paths) and not any(path.startswith(PROTECTED_PREFIXES) for path in paths)


def _fingerprints(tree: str, expected: dict | None = None, only=None, budgets=None) -> dict:
    """expected: the pinned commit's per-query parts, to stop early on a difference.
    only: the test ids to fingerprint (all when None).
    budgets: per test id, how many executed queries may differ; those are then
    run for real and the test reported as "pass" when they all pass."""
    budgets = budgets or {}
    os.chdir(tree)
    sys.path.insert(0, tree)
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.dont_write_bytecode = True

    import tests.test_executor  # noqa: F401 - the test file must import
    from sqlglot import exp, parse_one, planner, transpile
    from sqlglot.executor.python import PythonExecutor
    from sqlglot.generator import Generator
    from sqlglot.optimizer import optimize
    from sqlglot.schema import ensure_schema
    from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs

    runtime = repr((
        sorted(member.name for member in exp.DType),
        sorted(str(value) for value in exp.DataType.TEXT_TYPES),
        sorted(str(value) for value in exp.DataType.NUMERIC_TYPES),
        sorted(Generator.TIME_PART_SINGULARS.items()),
    ))

    def describe_plan(plan, executor, seen):
        steps = []
        pending = [plan.root]
        visited = set()
        while pending:
            step = pending.pop()
            if id(step) in visited:
                continue
            visited.add(id(step))
            pending.extend(step.dependencies)
            steps.append(step)

        def value(item):
            if isinstance(item, exp.Expr):
                code = None
                if not isinstance(item, (exp.Table, exp.Subquery)) or True:
                    try:
                        recorded = []
                        original = executor.generator.generate

                        def capture(expression, *args, **kwargs):
                            text = original(expression, *args, **kwargs)
                            recorded.append(text)
                            return text

                        executor.generator.generate = capture
                        try:
                            executor.generate(item.copy())
                        finally:
                            executor.generator.generate = original
                        code = recorded
                    except Exception as error:  # noqa: BLE001 - recorded, compared as data
                        code = "error:" + type(error).__name__
                return ["E", repr(item), code]
            if isinstance(item, planner.Step):
                return ["S", type(item).__name__, item.name]
            if isinstance(item, dict):
                return ["D", sorted([repr(k), value(v)] for k, v in item.items())]
            if isinstance(item, (set, frozenset)):
                return ["T", sorted(json.dumps(value(v), sort_keys=True) for v in item)]
            if isinstance(item, (list, tuple)):
                return ["L", [value(v) for v in item]]
            if isinstance(item, type):
                return ["C", item.__name__]
            return ["R", repr(item)]

        described = []
        for step in steps:
            fields = {name: value(getattr(step, name)) for name in sorted(vars(step))
                      if name not in ("dependents",)}
            described.append(json.dumps([type(step).__name__, fields], sort_keys=True, default=repr))
        # Subqueries the executor replaced get their own plans.
        for name, (subplan, _) in sorted(executor._subquery_plans.items()):
            if name not in seen:
                seen.add(name)
                described.append(json.dumps(["subquery", name, describe_plan(subplan, executor, seen)]))
        return sorted(described)

    class _Differs(Exception):
        pass

    differing: dict[str, list[int]] = {}

    def checked(parts: list, key: str, executes: bool | None = None) -> None:
        # Compare each part with the pinned commit's as soon as it exists.
        # A query whose result is executed may differ, up to the budget: those
        # queries are then run for real. Anything else stops the comparison.
        if expected is None:
            return
        known = expected.get(TESTS[key])
        index = len(parts) - 1
        if isinstance(known, list) and index < len(known) and known[index] == parts[index]:
            return
        if not isinstance(known, list) or index >= len(known) or index == 0 or executes is None:
            raise _Differs()
        if not executes:
            # Parsed, pickled and sent to a worker that returns None for it:
            # a different but valid parse cannot change the outcome.
            return
        found = differing.setdefault(key, [])
        found.append(index - 1)
        if len(found) > budgets.get(TESTS[key], 0):
            raise _Differs()

    def execute_digest(key: str, fixture: str, schema: dict) -> list:
        parts = []
        resolved = ensure_schema(schema)
        parts.append(hashlib.sha256((runtime + repr(resolved.supported_table_args)).encode()).hexdigest())
        checked(parts, key)
        for meta, sql, _ in load_sql_fixture_pairs(fixture):
            digest = hashlib.sha256()
            parsed = parse_one(sql)
            moved = pickle.loads(pickle.dumps(parsed))
            digest.update(repr(parsed).encode())
            digest.update(repr(moved).encode())
            digest.update(transpile(sql, write="duckdb")[0].encode())
            digest.update(repr(sorted(meta.items())).encode())
            if meta.get("execute"):
                optimized = optimize(moved, ensure_schema(schema), leave_tables_isolated=True)
                digest.update(repr(optimized).encode())
                plan = planner.Plan(optimized)
                executor = PythonExecutor(tables={})
                for line in describe_plan(plan, executor, set()):
                    digest.update(line.encode())
            parts.append(digest.hexdigest())
            checked(parts, key, bool(meta.get("execute")))
        return parts

    def optimized_digest(key: str, fixture: str) -> list:
        parts = []
        for _, sql, optimized in load_sql_fixture_pairs(fixture):
            digest = hashlib.sha256()
            digest.update(transpile(sql, write="duckdb")[0].encode())
            digest.update(transpile(optimized, write="duckdb")[0].encode())
            parts.append(digest.hexdigest())
            checked(parts, key)
        return parts

    def run_queries(key: str, indices: list[int]) -> None:
        """Run fixture queries exactly as TestExecutor._mp_execute does.

        The pool hands each query, pickled, to a worker that executes it with
        the pickled schema and tables, and the result comes back pickled. The
        test's own subtestHelper compares it with DuckDB.
        """
        import tests.test_executor as module

        case = module.TestExecutor("test_execute_" + key)
        module.TestExecutor.setUpClass()
        try:
            tpch = key == "tpch"
            schema = TPCH_SCHEMA if tpch else TPCDS_SCHEMA
            tables = case.tpch_tables if tpch else case.tpcds_tables
            sqls = case.tpch_sqls if tpch else case.tpcds_sqls
            module.initializer(*pickle.loads(pickle.dumps((schema, tables))))
            for index in indices:
                meta, sql, _ = sqls[index]
                expression, meta = pickle.loads(pickle.dumps((parse_one(sql), meta)))
                table = pickle.loads(pickle.dumps(module.mp_execute(expression, meta)))
                if table is None:
                    continue
                case.subtestHelper(index, table, tpch=tpch)
        finally:
            module.TestExecutor.tearDownClass()

    def max_nodes_digest() -> tuple[str, float]:
        """test_max_nodes at 1/50 of its width, with the node limits scaled to match.

        The test parses 10,000 identical columns, each `x=x=...` 100 deep, and
        checks where the parser's per-node counter stops it. Nothing in that
        path depends on the input's size except the count itself, so the same
        assertions on 200 columns exercise the same code. Every outcome and
        message is hashed; the CPU time catches a patch that only makes parsing
        slower, which at full size would end in a timeout.
        """
        import time

        import tests.test_parser  # noqa: F401 - the test file must import
        from sqlglot import exp as expressions
        from sqlglot.errors import ParseError

        digest = hashlib.sha256()

        def outcome(call):
            try:
                value = call()
            except ParseError as error:
                return "ParseError:" + str(error)
            return "ok:" + type(value).__name__ + ":" + repr(value)

        started = time.process_time()
        digest.update(outcome(lambda: parse_one("SELECT 1", max_nodes=1_000_000)).encode())
        columns = ",".join(f"col{i}" for i in range(500))
        digest.update(outcome(lambda: parse_one(f"SELECT {columns} FROM t", max_nodes=1_000_000)).encode())
        width, depth = 200, 100
        wide = "SELECT " + ",".join("x" + "=x" * depth for _ in range(width))
        for limit in (20_000, 1_000):
            text = outcome(lambda: parse_one(wide, max_nodes=limit))
            if not (text.startswith("ParseError:") and f"({limit})" in text
                    and "Maximum number of AST nodes" in text):
                raise AssertionError("max_nodes limit not enforced")
            digest.update(text.encode())
        many = "SELECT " + ",".join(f"x={i}" for i in range(100))
        digest.update(outcome(lambda: parse_one(many, max_nodes=5)).encode())
        last = parse_one("SELECT 1, 2, 3")
        if not isinstance(last, expressions.Select):
            raise AssertionError("parse after a limit failed")
        digest.update(repr(last).encode())
        return digest.hexdigest(), time.process_time() - started

    result: dict = {"_parts": {}}
    work = (("tpcds", lambda: execute_digest("tpcds", "optimizer/tpc-ds/tpc-ds.sql", TPCDS_SCHEMA)),
            ("tpch", lambda: execute_digest("tpch", "optimizer/tpc-h/tpc-h.sql", TPCH_SCHEMA)),
            ("optimized_tpch", lambda: optimized_digest("optimized_tpch", "optimizer/tpc-h/tpc-h.sql")))
    if only is None or TESTS["max_nodes"] in only:
        name = TESTS["max_nodes"]
        try:
            value, seconds = max_nodes_digest()
            reference = (expected or {}).get(name)
            if isinstance(reference, list) and len(reference) == 2 and seconds > 3 * reference[1] + 0.3:
                result[name] = "error:slow"
            else:
                result[name] = value
                result["_parts"][name] = [value, round(seconds, 4)]
        except BaseException as error:  # noqa: BLE001 - differs from the baseline hash
            result[name] = "error:" + type(error).__name__
    for key, compute in work:
        if only is not None and TESTS[key] not in only:
            continue
        try:
            parts = compute()
        except _Differs:
            result[TESTS[key]] = "error:differs"
            continue
        except Exception as error:  # noqa: BLE001 - differs from the baseline hash
            result[TESTS[key]] = "error:" + type(error).__name__
            continue
        if expected is not None and len(parts) != len(expected.get(TESTS[key]) or ()):
            result[TESTS[key]] = "error:differs"
            continue
        if differing.get(key):
            # Only these queries compute something new: run them as the test does.
            try:
                run_queries(key, differing[key])
            except BaseException as error:  # noqa: BLE001 - the test would fail
                result[TESTS[key]] = "error:failed:" + type(error).__name__
                continue
            result[TESTS[key]] = "pass"
            continue
        result[TESTS[key]] = hashlib.sha256("".join(parts).encode()).hexdigest()
        result["_parts"][TESTS[key]] = parts
    return result


def main() -> None:
    tree, output = sys.argv[1], sys.argv[2]
    try:
        result = _fingerprints(tree)
    except BaseException as error:  # noqa: BLE001 - the test file cannot be imported
        result = {name: "error:" + type(error).__name__ for name in TESTS.values()}
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, sort_keys=True)


def run_jobs(source: str, jobs: list, work_root: str,
             deadline: float | None, concurrency: int = 2, expected: dict | None = None) -> dict[str, dict]:
    """Fingerprint patched trees in forked children: {key: {test id: hash}}.

    jobs holds (key, replaced files, test ids to fingerprint). A child that fails, or is still running
    at the deadline, yields no entry, which keeps every test selected.
    """
    import shutil
    import signal
    import time

    import runner

    results: dict[str, dict[str, str]] = {}
    pending = list(jobs)
    active: dict[int, tuple[str, str, str]] = {}
    serial = 0
    os.makedirs(work_root, exist_ok=True)

    def finish(pid: int) -> None:
        key, tree, output = active.pop(pid)
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            with open(output, encoding="utf-8") as handle:
                results[key] = json.load(handle)
        except (OSError, ValueError):
            pass
        shutil.rmtree(tree, ignore_errors=True)
        for leftover in (output, output + ".tmp"):
            try:
                os.unlink(leftover)
            except OSError:
                pass

    try:
        while pending or active:
            while (pending and len(active) < concurrency
                   and (deadline is None or time.monotonic() < deadline)):
                key, replacements, only, budgets = pending.pop(0)
                serial += 1
                tree = os.path.join(work_root, "fp%d" % serial)
                output = tree + ".json"
                sys.stdout.flush()
                sys.stderr.flush()
                pid = os.fork()
                if pid == 0:
                    try:
                        os.setpgid(0, 0)
                        sink = os.open(os.devnull, os.O_WRONLY)
                        os.dup2(sink, 1)
                        os.dup2(sink, 2)
                        runner.build_tree(source, tree, replacements)
                        hashes = _fingerprints(tree, expected, only, budgets)
                        hashes.pop("_parts", None)
                        with open(output + ".tmp", "w", encoding="utf-8") as handle:
                            json.dump(hashes, handle, sort_keys=True)
                        os.rename(output + ".tmp", output)
                    finally:
                        os._exit(0)
                active[pid] = (key, tree, output)
            if not active:
                break
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid in active:
                finish(pid)
                continue
            if deadline is not None and time.monotonic() >= deadline:
                for pid in list(active):
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except OSError:
                        pass
                    try:
                        os.waitpid(pid, 0)
                    except OSError:
                        pass
                    finish(pid)
                pending.clear()
                break
            time.sleep(0.02)
    finally:
        for pid in list(active):
            try:
                os.killpg(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except OSError:
                pass
    return results


if __name__ == "__main__":
    main()
