import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from executor_system.runtime import ThorRuntime


def runtime_without_init():
    return object.__new__(ThorRuntime)


class RuntimeMetadataTest(unittest.TestCase):
    def test_write_final_metadata_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_root = Path(tmp_dir)
            runtime = runtime_without_init()
            runtime.output_root = output_root

            with patch("executor_system.runtime.GENERATE_METADATA", False):
                metadata_path = runtime.write_final_metadata()

            self.assertIsNone(metadata_path)
            self.assertFalse((output_root / "metadata.txt").exists())

    def test_write_final_metadata_writes_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_root = Path(tmp_dir)
            runtime = runtime_without_init()
            runtime.output_root = output_root
            runtime.controller_lock = threading.RLock()
            runtime.controller = SimpleNamespace(
                last_event=SimpleNamespace(metadata={"b": 2, "a": 1})
            )

            with patch("executor_system.runtime.GENERATE_METADATA", True):
                metadata_path = runtime.write_final_metadata()

            expected_path = output_root / "metadata.txt"
            self.assertEqual(metadata_path, expected_path)
            self.assertEqual(json.loads(expected_path.read_text(encoding="utf-8")), {"a": 1, "b": 2})
            self.assertEqual(expected_path.read_text(encoding="utf-8"), '{\n  "a": 1,\n  "b": 2\n}\n')


if __name__ == "__main__":
    unittest.main()
