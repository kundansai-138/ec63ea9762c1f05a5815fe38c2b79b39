"""Call evidence distinguishes function construction from actual execution."""
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_callable_map import CollectionCalls


class CollectionCallTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.source = Path(self.directory.name).resolve()
        self.path = str(self.source / "sqlglot" / "example.py")
        self.recorder = CollectionCalls(self.source)

    def tearDown(self):
        self.directory.cleanup()

    def record(self, source):
        before = sys.getprofile()
        sys.setprofile(self.recorder.profile)
        try:
            exec(compile(source, self.path, "exec"), {})
        finally:
            sys.setprofile(before)
        return self.recorder.calls["sqlglot/example.py"]

    def test_definition_does_not_enter_single_line_lambda(self):
        self.assertNotIn(2, self.record("x = 1\nTABLE = {'x': lambda x: x + 1}\n"))

    def test_import_time_invocation_records_lambda(self):
        self.assertIn(2, self.record("x = 1\nTABLE = {'x': lambda x: x + 1}\nTABLE['x'](2)\n"))

    def test_closure_construction_and_invocation_are_distinct(self):
        source = "def factory():\n    def value():\n        return 2\n    return value\nf = factory()\n"
        calls = self.record(source)
        self.assertIn(1, calls)
        self.assertNotIn(2, calls)
        self.assertIn(2, self.record(source + "f()\n"))

    def test_threaded_collection_calls_are_recorded(self):
        before = threading.getprofile()
        threading.setprofile(self.recorder.profile)
        try:
            self.record("import threading\nf = lambda: 1\nt = threading.Thread(target=f)\nt.start()\nt.join()\n")
        finally:
            threading.setprofile(before)
        self.assertIn(2, self.recorder.calls["sqlglot/example.py"])


if __name__ == "__main__":
    unittest.main()
