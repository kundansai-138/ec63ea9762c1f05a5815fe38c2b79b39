"""Selection boundaries for a separately loaded tokenizer dependency profile."""
import copy
import contextlib
import gzip
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch as mock_patch

from test_selector import IDS, TESTS, file_data, mapping, patch
from selector import SelectionEngine, main
from tokenizer_dependencies import CONTRACT, PIN, literal_shape
import ast


class TokenizerIntegrationTests(unittest.TestCase):
    source = 'class Tokenizer:\n    NUMERIC_ESCAPES = {"x": (2, 16)}\n'
    path = "sqlglot/example.py"

    def evidence(self):
        value = ast.parse(self.source).body[0].body[0].value
        profile = {
            "version": 1, "commit": PIN, "tests": list(IDS), "incomplete": [],
            "metadata": {"complete": True, "background_thread": False},
            "attributes": {self.path + "::Tokenizer::NUMERIC_ESCAPES": {
                "contract": CONTRACT, "shape": literal_shape(value),
                "readers": [1], "unsafe_collection": False,
            }},
        }
        return mapping(
            {self.path: file_data(self.source)},
            classes={self.path + "::Tokenizer": [0]},
            collection_attributes=["NUMERIC_ESCAPES"],
            tokenizer_attribute_profile=profile,
        )

    def select(self, evidence, after=None, tests=TESTS):
        return SelectionEngine(evidence, tests).select(
            patch(self.path, self.source, after or self.source.replace("2, 16", "3, 16")))

    def test_keeps_constructor_and_cached_core_reader(self):
        self.assertEqual(self.select(self.evidence()), {0, 1})

    def test_keeps_source_reflection_and_incomplete_readers(self):
        for protection in ("source", "reflection", "incomplete"):
            with self.subTest(protection=protection):
                evidence = self.evidence()
                if protection == "source":
                    evidence["files"][self.path]["reads"] = [2]
                elif protection == "reflection":
                    evidence["attribute_readers"] = {"__dict__": [], "__code__": [2]}
                else:
                    evidence["tokenizer_attribute_profile"]["incomplete"] = [IDS[2]]
                self.assertEqual(self.select(evidence), {0, 1, 2})

    def test_unknown_manifest_test_is_kept(self):
        tests = [*TESTS, {"id": "tests/new.py::test_new", "number": 3,
                         "file": "tests/new.py", "seconds": 0.1}]
        self.assertEqual(self.select(self.evidence(), tests=tests), {0, 1, 3})

    def test_missing_stale_or_partial_profile_keeps_fallback(self):
        evidence = self.evidence()
        broken = [None, {"metadata": {"complete": False}},
                  {"commit": "wrong"}, {"tests": list(reversed(IDS))},
                  {"metadata": {"complete": True, "background_thread": True}}]
        for change in broken:
            with self.subTest(change=change):
                candidate = copy.deepcopy(evidence)
                if change is None:
                    candidate.pop("tokenizer_attribute_profile")
                else:
                    candidate["tokenizer_attribute_profile"].update(change)
                self.assertEqual(self.select(candidate), {0, 1, 2})

    def test_changed_keys_or_types_keep_fallback(self):
        for old, new in [('"x"', '"y"'), ("16", '"16"'), ("2, 16", "2, 16, 0")]:
            with self.subTest(new=new):
                self.assertEqual(self.select(self.evidence(), self.source.replace(old, new)), {0, 1, 2})

    def test_manifest_numbers_are_joined_through_ids(self):
        tests = [dict(t, number=2 - t["number"]) for t in TESTS]
        self.assertEqual(self.select(self.evidence(), tests=tests), {1, 2})

    def test_truncated_map_or_profile_keeps_cli_fallback(self):
        for broken in ("map", "profile"):
            with self.subTest(broken=broken), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "commits").mkdir()
                (root / "tests.json").write_text(json.dumps({"tests": TESTS}))
                (root / "commits/change.patch").write_text(
                    patch(self.path, self.source, self.source.replace("2, 16", "3, 16")))
                evidence = self.evidence()
                profile = evidence.pop("tokenizer_attribute_profile")
                for name, value in (("map", evidence), ("profile", profile)):
                    compressed = gzip.compress(json.dumps(value).encode())
                    (root / name).write_bytes(compressed[:-4] if name == broken else compressed)
                output = io.StringIO()
                with mock_patch("sys.argv", ["selector", "--data", str(root), "--map", str(root / "map"),
                                             "--tokenizer-profile", str(root / "profile")]), \
                        contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                    main()
                answer = json.loads(output.getvalue())
                self.assertEqual({n for worker in answer["workers"] for n in worker}, {0, 1, 2})


if __name__ == "__main__":
    unittest.main()
