import json
import shutil
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

from executor_system.execution_control import close_runtime
from executor_system.generated_plan_runtime import runtime_output_root
from executor_system.runtime import ThorRuntime


def runtime_without_init():
    return object.__new__(ThorRuntime)


def output_runtime(output_root: Path, *, render_image: bool = True):
    runtime = runtime_without_init()
    runtime.output_root = output_root
    runtime.physical_agent_count = 1
    runtime.render_image = render_image
    return runtime


class RuntimeOutputIsolationTest(unittest.TestCase):
    def test_prepare_output_dirs_only_cleans_its_owned_root(self):
        """Cleaning one run must not alter another run's media or metadata."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            parent = Path(tmp_dir)
            first = parent / "first-run"
            second = parent / "second-run"
            for root, label in ((first, b"first"), (second, b"second")):
                (root / "agent_1").mkdir(parents=True)
                (root / "agent_1" / "img_00000.png").write_bytes(label)
                (root / "top_view").mkdir()
                (root / "top_view" / "img_00000.png").write_bytes(label)
                (root / "video_agent_1.mp4").write_bytes(label)
                (root / "metadata.txt").write_text(label.decode("ascii"), encoding="utf-8")

            untouched = {
                path.relative_to(second): path.read_bytes()
                for path in second.rglob("*")
                if path.is_file()
            }
            output_runtime(first).prepare_output_dirs()

            self.assertEqual(
                {
                    path.relative_to(second): path.read_bytes()
                    for path in second.rglob("*")
                    if path.is_file()
                },
                untouched,
            )
            self.assertTrue((first / "agent_1").is_dir())
            self.assertTrue((first / "top_view").is_dir())
            self.assertFalse((first / "video_agent_1.mp4").exists())

    def test_default_output_roots_are_unique_and_source_roots_are_rejected(self):
        """Default runs are isolated, and cleanup can never target source trees."""
        controller = SimpleNamespace(last_event=SimpleNamespace(metadata={}))
        with patch("executor_system.runtime.require_dependencies"), patch.object(
            ThorRuntime, "resolve_physical_agent_count", return_value=1
        ), patch.object(ThorRuntime, "resolve_agent_mode", return_value="default"), patch.object(
            ThorRuntime, "resolve_show_windows", return_value=False
        ), patch.object(ThorRuntime, "build_robot_agent_map", return_value={}), patch.object(
            ThorRuntime, "configure_movement"
        ), patch.object(ThorRuntime, "create_controller", return_value=controller), patch.object(
            ThorRuntime, "print_agent_metadata"
        ), patch.object(ThorRuntime, "initialize_scene"):
            first = ThorRuntime([object()], "1", False, False)
            second = ThorRuntime([object()], "1", False, False)

            self.assertTrue(first.output_root.is_absolute())
            self.assertTrue(first.output_root.is_dir())
            self.assertNotEqual(first.output_root, second.output_root)
            module_source = Path(sys.modules[ThorRuntime.__module__].__file__).resolve().parent
            self.assertNotEqual(first.output_root, module_source)

            for unsafe_root in (module_source, ROOT):
                with self.assertRaises(ValueError):
                    ThorRuntime([object()], "1", False, False, output_root=unsafe_root)

        shutil.rmtree(first.output_root)
        shutil.rmtree(second.output_root)

    def test_generated_attempt_uses_the_parent_attempt_directory(self):
        """Retries have distinct parent-owned directories, never shared media."""
        identity = {
            "run_id": "run_123",
            "task_key": "a" * 64,
            "attempt": 2,
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            metrics_path = Path(tmp_dir) / "attempt_2" / "child_metrics.json"
            self.assertEqual(
                runtime_output_root(metrics_path, identity),
                metrics_path.resolve().parent,
            )
            standalone_root = runtime_output_root(None, identity)

        self.assertTrue(standalone_root.is_absolute())
        self.assertEqual(standalone_root.name, "attempt_2")
        self.assertEqual(standalone_root.parent.name, "a" * 64)

    def test_failed_close_is_idempotent_and_metadata_still_writes(self):
        """A failed controller close is recorded once and cannot block persistence."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            calls = []

            class FailingController:
                def stop(self):
                    calls.append("stop")
                    raise RuntimeError("stop failed")

            runtime = output_runtime(Path(tmp_dir), render_image=False)
            runtime.show_windows = False
            runtime.execution_quiescent = True
            runtime.controller_lock = threading.RLock()
            runtime.controller = FailingController()
            cleanup_errors = []
            close_runtime(runtime, cleanup_errors)
            close_runtime(runtime, cleanup_errors)

            with patch("executor_system.runtime.GENERATE_METADATA", True):
                metadata_path = runtime.write_final_metadata()

            self.assertEqual(calls, ["stop"])
            self.assertEqual(len(cleanup_errors), 1)
            self.assertEqual(metadata_path, Path(tmp_dir) / "metadata.txt")
            self.assertEqual(json.loads(metadata_path.read_text(encoding="utf-8")), {})


if __name__ == "__main__":
    unittest.main()
