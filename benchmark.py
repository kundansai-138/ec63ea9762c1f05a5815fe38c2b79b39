"""Validate and score fresh selector processes; labels stay outside the selector."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import subprocess
import sys
from pathlib import Path

from score import evaluate_run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=Path(__file__).parent / "sample")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--image", help="Run a built Docker image under the competition limits")
    args = parser.parse_args()
    data = args.data.resolve()
    tests = json.loads((data / "tests.json").read_text("utf-8"))["tests"]
    answers = json.loads((data / "answers.json").read_text("utf-8"))["commits"]
    rows = []
    for run in range(args.runs):
        if args.image:
            # A tiny parent waits for selector shutdown, so measured CPU includes
            # garbage collection and interpreter teardown, not just its main().
            measure = ("import subprocess,resource,json,sys; "
                       "p=subprocess.run([sys.executable,'/app/selector.py']); "
                       "c=resource.getrusage(resource.RUSAGE_CHILDREN); "
                       "s=resource.getrusage(resource.RUSAGE_SELF); "
                       "print(json.dumps({'measured_cpu_seconds':c.ru_utime+c.ru_stime+s.ru_utime+s.ru_stime,'peak_rss_kib':c.ru_maxrss}),file=sys.stderr); "
                       "sys.exit(p.returncode)")
            command = ["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
                       "--cpus", "2", "--memory", "4096m", "--memory-swap", "4096m",
                       "--pids-limit", "256", "--read-only", "--tmpfs", "/tmp:rw,size=64m,mode=1777",
                       "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "65534:65534",
                       "--env", "HOME=/tmp", "--mount", f"type=bind,source={data / 'commits'},target=/data/commits,readonly",
                       "--mount", f"type=bind,source={data / 'tests.json'},target=/data/tests.json,readonly", args.image,
                       "python", "-c", measure]
        else:
            command = [sys.executable, str(Path(__file__).with_name("selector.py")), "--data", str(data)]
        result = subprocess.run(command, capture_output=True, timeout=180, check=True)
        if len(result.stdout) > 2_000_000:
            raise RuntimeError("stdout exceeded 2 MB")
        evaluation = evaluate_run(result.stdout, tests, answers)
        if not evaluation["ok"]:
            raise RuntimeError(evaluation)
        cpu_key = "measured_cpu_seconds" if args.image else "selector_cpu_seconds"
        stats = [json.loads(line) for line in result.stderr.decode().splitlines() if line.startswith('{"'+cpu_key+'":')]
        cpu = stats[-1][cpu_key]
        row = {"run": run+1, "simulated_seconds": evaluation["simulated_milliseconds"] / 1000,
               "cpu_seconds": cpu, "misses": len(evaluation["missed_commits"]),
               "missed_commits": evaluation["missed_commits"], "stdout_bytes": len(result.stdout),
               "stdout_sha256": hashlib.sha256(result.stdout).hexdigest()}
        row["total_seconds"] = row["simulated_seconds"] + cpu
        if args.image:
            row["peak_rss_mib"] = stats[-1]["peak_rss_kib"] / 1024
        rows.append(row)
        print(json.dumps(row), flush=True)
    report = {"environment": "competition-limited Docker" if args.image else "local Python (not container-verified)",
              "cpu_measurement": "selector process plus measurement parent" if args.image else "selector main before interpreter teardown",
              "runs": rows, "median_total_seconds": statistics.median(row["total_seconds"] for row in rows),
              "deterministic_output": len({row["stdout_sha256"] for row in rows}) == 1}
    report["rounded_sample_score_seconds"] = math.floor(report["median_total_seconds"] * 10 + 0.5) / 10
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
