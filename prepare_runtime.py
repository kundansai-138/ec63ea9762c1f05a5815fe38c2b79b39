"""Install the pinned sqlglot checkout into the image at build time.

Downloads sqlglot at commit eb11aec4be8cb2122afe713a4816662c3f0fb2fa from its
own GitHub repository, checks every text file against the baseline sources in
the committed map, and compiles bytecode so that importing the unchanged
modules at run time costs no compilation.

The check matters: the selector applies each patch to the map's copy of a file
and writes the result into a copy of this checkout. If the two baselines ever
differed, tests would run against the wrong sources, so the build stops.
"""
from __future__ import annotations

import compileall
import gzip
import io
import json
import os
import sys
import tarfile
import urllib.request

PINNED_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = f"https://github.com/tobymao/sqlglot/archive/{PINNED_COMMIT}.tar.gz"


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else "/opt/sqlglot"
    map_path = sys.argv[2] if len(sys.argv) > 2 else "/app/coverage-map.json.gz"
    last_error = None
    for _ in range(4):
        try:
            with urllib.request.urlopen(URL, timeout=120) as response:
                payload = response.read()
            break
        except OSError as error:
            last_error = error
    else:
        raise SystemExit(f"could not download sqlglot: {last_error!r}")

    parent = os.path.dirname(target.rstrip("/")) or "/"
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        archive.extractall(parent, filter="data")
    os.rename(os.path.join(parent, f"sqlglot-{PINNED_COMMIT}"), target)

    with gzip.open(map_path, "rt", encoding="utf-8") as handle:
        mapping = json.load(handle)
    if mapping.get("commit") != PINNED_COMMIT:
        raise SystemExit("the committed map is for a different commit")
    checked, different = 0, []
    for path, record in mapping["files"].items():
        source = record.get("source")
        if not isinstance(source, str):
            continue
        try:
            with open(os.path.join(target, path), "rb") as handle:
                same = handle.read() == source.encode("utf-8")
        except OSError:
            same = False
        if same:
            checked += 1
        else:
            different.append(path)
    # A file whose bytes differ (the map stores text with LF line endings) can
    # still be selected from baseline evidence, but a patch to it is never
    # verified by running tests. Source code must match exactly.
    if any(path.endswith(".py") for path in different) or len(different) > 8 or checked < 300:
        raise SystemExit(f"baseline mismatch in {different[:10]}; {checked} files matched")
    with open(os.path.join(target, ".unverifiable-files.json"), "w", encoding="utf-8") as handle:
        json.dump(sorted(different), handle)

    # sqlglot derives its version from Git metadata that the archive lacks; the
    # labeled environment imported it from a plain checkout the same way.
    ok = compileall.compile_dir(os.path.join(target, "sqlglot"), quiet=2, workers=1)
    ok = compileall.compile_dir(os.path.join(target, "tests"), quiet=2, workers=1) and ok
    if not ok:
        raise SystemExit("bytecode compilation failed")
    for directory, _, names in os.walk(target):
        os.chmod(directory, 0o755)
        for name in names:
            full = os.path.join(directory, name)
            if not os.path.islink(full):
                os.chmod(full, 0o644)
    print(f"sqlglot {PINNED_COMMIT[:7]} ready at {target}; {checked} files match the map; "
          f"not verifiable: {different}")


if __name__ == "__main__":
    main()
