"""Read the isolation map: what each baseline test depends on when run alone.

build_isolation_map.py ran every test by itself in a fresh process on the
pinned commit and recorded the files whose module-level code it executed. A
change confined to one module cannot affect a test whose own process never
imports that module, so the importers of a file bound the tests it can break.

The map is evidence about the unmodified commit only.
"""
from __future__ import annotations

import base64
import gzip
import json
import zlib

PINNED_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


class IsolationMap:
    def __init__(self, path, tests: list[dict]):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("version") != 1 or data.get("commit") != PINNED_COMMIT:
            raise ValueError("isolation map does not describe the pinned commit")
        if data.get("metadata", {}).get("subset"):
            raise ValueError("isolation map was built from a subset of the tests")
        current = {test["id"]: test["number"] for test in tests}
        self.numbers = [current.get(name) for name in data["tests"]]
        self.width = data["set_width"]
        self.raw = zlib.decompress(base64.b64decode(data["sets"]))
        self.files = data["files"]
        known = set(data["tests"])
        failed_alone = set(data.get("failed_alone", []))
        # Tests without usable evidence can never be excluded.
        self.always = {test["number"] for test in tests
                       if test["id"] not in known or test["id"] in failed_alone}
        self._cache: dict[tuple, set[int]] = {}

    def _bits(self, set_ids) -> set[int]:
        merged = 0
        width, raw = self.width, self.raw
        for set_id in set(set_ids):
            merged |= int.from_bytes(raw[set_id * width:(set_id + 1) * width], "little")
        members = set()
        numbers = self.numbers
        index = 0
        while merged:
            low = merged & 0xFFFFFFFFFFFFFFFF
            while low:
                bit = low & -low
                number = numbers[index + bit.bit_length() - 1]
                if number is not None:
                    members.add(number)
                low ^= bit
            merged >>= 64
            index += 64
        return members

    def importers(self, path: str) -> set[int] | None:
        """Tests whose own process executes this file's module-level code.

        None means the map has no evidence for the file.
        """
        key = ("importers", path)
        if key not in self._cache:
            entry = self.files.get(path)
            phase = entry.get("import") if entry else None
            self._cache[key] = self._bits(phase[1]) if phase else None
        found = self._cache[key]
        return None if found is None else set(found)

    def line_tests(self, path: str, start: int, end: int, phase: str) -> set[int]:
        """Tests that executed any line in [start, end] of the file in a phase."""
        entry = self.files.get(path)
        record = entry.get(phase) if entry else None
        if not record:
            return set()
        lines, set_ids = record
        return self._bits(set_id for line, set_id in zip(lines, set_ids) if start <= line <= end)
