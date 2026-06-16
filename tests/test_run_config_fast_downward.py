import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from run_config import RunConfig


class RunConfigFastDownwardTests(unittest.TestCase):
    def test_fast_downward_path_env_overrides_configured_planner(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared = root / "shared" / "fast-downward.py"
            config = RunConfig(
                root,
                values={"planner": {"executable": "downward/fast-downward.py"}},
            )

            with patch.dict("os.environ", {"FAST_DOWNWARD_PATH": str(shared)}):
                self.assertEqual(config.planner_executable, shared.resolve())


if __name__ == "__main__":
    unittest.main()
