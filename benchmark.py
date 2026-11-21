"""Validate and score fresh selector processes; labels stay outside the selector."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

from scoring_audit import audit_run, cpu_microseconds, median_microseconds, rounded_score


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=Path(__file__).parent / "sample")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--image", help="Run a built Docker image under the competition limits")
    args = parser.parse_args()
    if args.runs < 1 or args.runs % 2 == 0:
        parser.error("--runs must be a positive odd number; official runs use 3 or 5")
    data = args.data.resolve()
    tests = json.loads((data / "tests.json").read_text("utf-8"))["tests"]
    answers = json.loads((data / "answers.json").read_text("utf-8"))["commits"]
    if {patch.stem for patch in (data / "commits").glob("*.patch")} != set(answers):
        raise ValueError("answer IDs must exactly match the patch files")
    rows = []
    for run in range(args.runs):
        container_name = None
        if args.image:
            container_name = "blast-radius-benchmark-" + uuid.uuid4().hex
            # A tiny parent waits for selector shutdown, so measured CPU includes
            # garbage collection and interpreter teardown, not just its main().
            measure = ("import subprocess,resource,json,sys; "
                       "p=subprocess.run([sys.executable,'/app/selector.py']); "
                       "c=resource.getrusage(resource.RUSAGE_CHILDREN); "
                       "s=resource.getrusage(resource.RUSAGE_SELF); "
                       "print(json.dumps({'measured_cpu_seconds':c.ru_utime+c.ru_stime+s.ru_utime+s.ru_stime,'peak_rss_kib':c.ru_maxrss}),file=sys.stderr); "
                       "sys.exit(p.returncode)")
            command = ["docker", "run", "--rm", "--name", container_name, "--platform", "linux/amd64", "--network", "none",
                       "--cpus", "2", "--memory", "4096m", "--memory-swap", "4096m",
                       "--pids-limit", "256", "--read-only", "--tmpfs", "/tmp:rw,size=64m,mode=1777",
                       "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "65534:65534",
                       "--env", "HOME=/tmp", "--mount", f"type=bind,source={data / 'commits'},target=/data/commits,readonly",
                       "--mount", f"type=bind,source={data / 'tests.json'},target=/data/tests.json,readonly", args.image,
                       "python", "-c", measure]
        else:
            command = [sys.executable, str(Path(__file__).with_name("selector.py")), "--data", str(data)]
        started = time.monotonic()
        try:
            result = subprocess.run(command, capture_output=True, timeout=180, check=True)
        except subprocess.TimeoutExpired:
            # Killing the Docker client alone does not stop the container. Only
            # remove the uniquely named container created for this measurement.
            if container_name is not None:
                try:
                    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, timeout=30, check=False)
                except (OSError, subprocess.SubprocessError):
                    pass  # Preserve the original benchmark timeout failure.
            raise
        elapsed = time.monotonic() - started
        evaluation = audit_run(result.stdout, tests, answers)
        cpu_key = "measured_cpu_seconds" if args.image else "selector_cpu_seconds"
        stats = [json.loads(line) for line in result.stderr.decode().splitlines() if line.startswith('{"'+cpu_key+'":')]
        cpu = stats[-1][cpu_key]
        cpu_us = cpu_microseconds(cpu)
        row = {"run": run+1, "simulated_seconds": evaluation["simulated_ms"] / 1000,
               "cpu_seconds": cpu_us / 1_000_000, "cpu_microseconds": cpu_us,
               "wall_seconds": elapsed, "misses": evaluation["misses"],
               "missed_commits": evaluation["missed_commits"], "stdout_bytes": len(result.stdout),
               "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
               "scoring_breakdown": evaluation}
        row["total_microseconds"] = evaluation["simulated_ms"] * 1000 + cpu_us
        row["total_seconds"] = row["total_microseconds"] / 1_000_000
        if args.image:
            row["peak_rss_mib"] = stats[-1]["peak_rss_kib"] / 1024
        rows.append(row)
        print(json.dumps(row), flush=True)
    report = {"environment": "competition-limited Docker" if args.image else "local Python (not container-verified)",
              "run_count": args.runs,
              "competition_run_count": args.runs in (3, 5),
              "score_scope": "local sample batch; not an official hidden-set score",
              "cpu_measurement": "selector process plus measurement parent" if args.image else "selector main before interpreter teardown",
              "runs": rows,
              "deterministic_output": len({row["stdout_sha256"] for row in rows}) == 1}
    report["median_total_microseconds"] = median_microseconds([row["total_microseconds"] for row in rows])
    report["median_total_seconds"] = report["median_total_microseconds"] / 1_000_000
    report["rounded_sample_score_seconds"] = rounded_score(report["median_total_microseconds"])
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
