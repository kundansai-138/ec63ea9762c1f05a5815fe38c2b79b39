"""Failure-safe support boundaries for supplemental child tracing."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_subprocess_map import CHILD_HOOK, supports_command


class SubprocessEvidenceTests(unittest.TestCase):
    def test_supported_python_child(self):
        self.assertTrue(supports_command([sys.executable, "-c", "print('ok')"], {}))
        self.assertTrue(supports_command(["python", "-B", "-m", "example"], {}))

    def test_unobservable_startup_modes_preserve_fallback(self):
        for flag in ("-I", "-E", "-S", "-IS", "-BE"):
            self.assertFalse(supports_command(["python", flag, "-c", "pass"], {}))
        self.assertFalse(supports_command("python -c pass", {}))
        self.assertFalse(supports_command(["python", "-c", "pass"], {"shell": True}))
        self.assertFalse(supports_command(["python", "-c", "pass"], {"executable": "different"}))
        self.assertFalse(supports_command(["sh", "-c", "true"], {}))

    def test_child_records_whole_module_and_data_reads(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            hooks = root / "hooks"
            source = root / "source"
            hooks.mkdir()
            source.mkdir()
            report = root / "report.json"
            (hooks / "sitecustomize.py").write_text(CHILD_HOOK)
            (source / "module.py").write_text("VALUE = open('fixture.txt').read()\n")
            (source / "fixture.txt").write_text("value")
            env = {**os.environ, "BLAST_TRACE_SOURCE": str(source), "BLAST_TRACE_REPORT": str(report),
                   "PYTHONPATH": os.pathsep.join((str(hooks), str(source))), "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run([sys.executable, "-c", "import module; print(module.VALUE)"],
                                    cwd=source, env=env, capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), "value")
            evidence = json.loads(report.read_text())
            self.assertTrue(evidence["complete"])
            self.assertFalse(evidence["nested"])
            self.assertIn("module.py", evidence["files"])
            self.assertIn("fixture.txt", evidence["files"])


if __name__ == "__main__":
    unittest.main()
