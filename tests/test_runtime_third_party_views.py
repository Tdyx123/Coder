import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


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
            runtime.render_image = True

            for view_name in (TOP_VIEW_NAME,) + DIRECTIONAL_VIEW_NAMES:
                view_path = output_root / view_name
                view_path.mkdir()
                (view_path / "img_00000.png").write_bytes(b"old")

            runtime.prepare_output_dirs()

            self.assertTrue((output_root / TOP_VIEW_NAME).is_dir())
            for view_name in DIRECTIONAL_VIEW_NAMES:
                self.assertFalse((output_root / view_name).exists())
            self.assertTrue((output_root / "agent_1").is_dir())

    def test_prepare_output_dirs_skips_media_dirs_when_render_disabled(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_root = Path(tmp_dir)
            runtime = runtime_without_init()
            runtime.output_root = output_root
            runtime.physical_agent_count = 1
            runtime.render_image = False

            runtime.prepare_output_dirs()

            self.assertFalse((output_root / TOP_VIEW_NAME).exists())
            self.assertFalse((output_root / "agent_1").exists())

    def test_save_frames_skips_frame_writes_when_render_disabled(self):
        runtime = runtime_without_init()
        runtime.render_image = False
        event = SimpleNamespace(events=[SimpleNamespace(frame=object())])

        with patch("executor_system.runtime.event_cv2_frame") as event_cv2_frame:
            runtime.save_frames(event)

        event_cv2_frame.assert_not_called()

    def test_generate_video_skips_directory_checks_when_render_disabled(self):
        runtime = runtime_without_init()
        runtime.render_image = False

        with patch("executor_system.runtime.shutil.which") as which:
            runtime.generate_video()

        which.assert_not_called()


if __name__ == "__main__":
    unittest.main()
