import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from executor_system.config import (
    DIRECTIONAL_VIEW_NAMES,
    THIRD_PARTY_VIEW_NAMES,
    TOP_VIEW_NAME,
)
from executor_system.runtime import ThorRuntime


def runtime_without_init():
    return object.__new__(ThorRuntime)


class RuntimeThirdPartyViewsTest(unittest.TestCase):
    def test_third_party_view_names_only_include_top_view(self):
        self.assertEqual(THIRD_PARTY_VIEW_NAMES, (TOP_VIEW_NAME,))
        for view_name in DIRECTIONAL_VIEW_NAMES:
            self.assertNotIn(view_name, THIRD_PARTY_VIEW_NAMES)

    def test_active_third_party_view_names_defaults_to_top_view(self):
        runtime = runtime_without_init()
        runtime.top_view_enabled = True
        runtime.third_party_view_names = []

        self.assertEqual(runtime.active_third_party_view_names(), [TOP_VIEW_NAME])

    def test_prepare_output_dirs_removes_legacy_directional_view_dirs(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_root = Path(tmp_dir)
            runtime = runtime_without_init()
            runtime.output_root = output_root
            runtime.physical_agent_count = 1

            for view_name in (TOP_VIEW_NAME,) + DIRECTIONAL_VIEW_NAMES:
                view_path = output_root / view_name
                view_path.mkdir()
                (view_path / "img_00000.png").write_bytes(b"old")

            runtime.prepare_output_dirs()

            self.assertTrue((output_root / TOP_VIEW_NAME).is_dir())
            for view_name in DIRECTIONAL_VIEW_NAMES:
                self.assertFalse((output_root / view_name).exists())
            self.assertTrue((output_root / "agent_1").is_dir())


if __name__ == "__main__":
    unittest.main()
