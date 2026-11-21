"""Derive static dependency indexes solely from an existing baseline map."""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

from attribute_dependencies import build_attribute_readers
from dependency_safety import unsafe_functions


def enrich(mapping):
    mapping["attribute_readers"] = build_attribute_readers(mapping["files"])
    for path, entry in mapping["files"].items():
        if path.startswith("sqlglot/") and path.endswith(".py") and entry.get("source"):
            entry["persistent_functions"] = sorted(unsafe_functions(entry["source"]))
    mapping["dependency_analysis_version"] = 1
    mapping.setdefault("metadata", {})["static_dependency_analysis"] = (
        "Version 1: persistent-state producer/callee ranges and attribute-read "
        "dependencies derived only from pinned source and baseline coverage")
    return mapping


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-map", type=Path, required=True)
    parser.add_argument("--output-map", type=Path, required=True)
    args = parser.parse_args()
    with gzip.open(args.input_map, "rt", encoding="utf-8") as stream:
        mapping = json.load(stream)
    if mapping.get("commit") != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa" or mapping.get("version") != 1:
        raise ValueError("Expected the pinned baseline coverage map")
    enrich(mapping)
    temporary = args.output_map.with_suffix(args.output_map.suffix + ".tmp")
    with temporary.open("wb") as destination:
        with gzip.GzipFile(filename="", fileobj=destination, mode="wb", mtime=0) as stream:
            stream.write(json.dumps(mapping, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    temporary.replace(args.output_map)


if __name__ == "__main__":
    main()
