"""Minimize the scorer's worker overhead plus the longest worker load.

Selection happens elsewhere: this module schedules every supplied test exactly
once. All arithmetic matches score.py's integer milliseconds and half-up rule.
"""

from __future__ import annotations

from bisect import bisect_left
from math import floor

WORKER_MILLISECONDS = 2_000
MAX_WORKERS = 8


def _polish(workers: list[list[tuple[int, int]]], loads: list[int], lower: int) -> None:
    """Use bounded moves/swaps to improve LPT when its bound is not tight.

    A move must improve the complete sorted load vector. This also permits
    reducing one of several tied busiest workers without cycling. The common
    case (one long, indivisible test determines the makespan) exits immediately.
    """
    for _ in range(16):
        if max(loads) <= lower:
            return
        source = max(range(len(loads)), key=loads.__getitem__)
        old_key = tuple(sorted(loads, reverse=True))
        best_key = old_key
        best = None
        for target in range(len(loads)):
            if target == source or loads[target] == loads[source]:
                continue
            # The artificial zero represents moving a test with no exchange.
            choices = sorted([(0, -1)] + [(duration, i) for i, (duration, _) in enumerate(workers[target])])
            durations = [duration for duration, _ in choices]
            gap = loads[source] - loads[target]
            for source_index, (duration, _) in enumerate(workers[source]):
                ideal = duration - gap / 2
                insertion = bisect_left(durations, ideal)
                for candidate in (insertion - 1, insertion):
                    if candidate < 0 or candidate >= len(choices):
                        continue
                    exchange, target_index = choices[candidate]
                    improvement = duration - exchange
                    if not 0 < improvement < gap:
                        continue
                    trial = loads.copy()
                    trial[source] -= improvement
                    trial[target] += improvement
                    trial_key = tuple(sorted(trial, reverse=True))
                    if trial_key < best_key:
                        best_key = trial_key
                        best = source_index, target, target_index, improvement
        if best is None:
            return
        source_index, target, target_index, improvement = best
        moved = workers[source][source_index]
        if target_index < 0:
            workers[source].pop(source_index)
            workers[target].append(moved)
        else:
            workers[source][source_index] = workers[target][target_index]
            workers[target][target_index] = moved
        loads[source] -= improvement
        loads[target] += improvement


def schedule(tests: list[dict]) -> list[list[int]]:
    """Return deterministic workers minimizing a cheap, improved LPT estimate.

    All 1..8 worker counts are considered. A rigorous makespan lower bound
    skips counts that cannot beat the best known cost, saving decision CPU.
    """
    jobs = sorted(
        ((floor(test["seconds"] * 1000 + 0.5), test["number"]) for test in tests),
        key=lambda job: (-job[0], job[1]),
    )
    if not jobs:
        return []

    total = sum(duration for duration, _ in jobs)
    longest = jobs[0][0]
    best_workers = [jobs.copy()]
    best_cost = total + WORKER_MILLISECONDS
    for count in range(2, min(MAX_WORKERS, len(jobs)) + 1):
        lower = max(longest, (total + count - 1) // count)
        overhead = WORKER_MILLISECONDS * count
        if lower + overhead >= best_cost:
            continue
        workers: list[list[tuple[int, int]]] = [[] for _ in range(count)]
        loads = [0] * count
        for job in jobs:
            lightest = min(range(count), key=loads.__getitem__)
            workers[lightest].append(job)
            loads[lightest] += job[0]
        _polish(workers, loads, lower)
        cost = max(loads) + overhead
        if cost < best_cost:
            best_cost = cost
            best_workers = workers
    return [[number for _, number in worker] for worker in best_workers if worker]
