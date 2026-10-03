import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class LogTestCase(unittest.TestCase):
    """Temporary directory with helpers to write synthetic log files."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def write(self, name, rows):
        path = self.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        return path

    def run_cli(self, *args, **kwargs):
        return subprocess.run([sys.executable, str(ROOT / "ai-costs"), *map(str, args)], text=True,
                              stdin=subprocess.DEVNULL, capture_output=True, **kwargs)
