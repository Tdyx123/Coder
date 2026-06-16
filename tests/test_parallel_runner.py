import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from executor_system.action_plan import Action, StagePlan, TaskPlan
from executor_system.parallel_runner import (
    main as parallel_runner_main,
    run_action_plan_tolerant,
)
import executor_system.parallel_runner as parallel_runner
from executor_system.runtime import PICKUP_OBJECT_CLIP_ERROR


class FakeEvent:
    def __init__(self):
        self.metadata = {
            "lastActionSuccess": True,
            "agent": {"rotation": {"y": 0.0}},
        }


class FakeRuntime:
    physical_agent_count = 2

    def __init__(self):
        self.robot_agent_map = {"robot1": 0, "robot2": 1}

    def physical_agent_id(self, robot_id):
        return self.robot_agent_map[str(robot_id)]

    def current_agent_position(self, agent_id):
        return {"x": float(agent_id), "y": 0.0, "z": 0.0}

    def agent_event(self, _agent_id):
        return FakeEvent()

    def agent_held_objects_for(self, _agent_id):
        return set()

    def step(self, _payload, **_kwargs):
        return FakeEvent()


def write_fake_generated_script(
    path: Path,
    *,
    sleep_seconds=0.0,
    returncode=0,
    gcr=1.0,
    robot_failures=None,
    stdout_text="",
):
    path.parent.mkdir(parents=True, exist_ok=True)
    status = "success" if returncode == 0 else "failed"
    robot_failures = [] if robot_failures is None else robot_failures
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import argparse",
                "import json",
                "import os",
                "import time",
                "from pathlib import Path",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--runner-mode', action='store_true')",
                "parser.add_argument('--metrics-output', required=True)",
                "parser.add_argument('--timeout-seconds', type=float, default=100.0)",
                "args = parser.parse_args()",
                f"time.sleep({sleep_seconds!r})",
                f"print({stdout_text!r})" if stdout_text else "pass",
                "result = {",
                f"    'status': {status!r},",
                "    'timed_out': False,",
                "    'run_time_seconds': 0.01,",
                f"    'gcr': {gcr!r},",
                "    'executed_actions': 2,",
                "    'failed_actions': 0,",
                "    'failure_action_ratio': 0.0,",
                f"    'robot_failures': {robot_failures!r},",
                "    'cuda_visible_devices': os.environ.get('CUDA_VISIBLE_DEVICES'),",
                "    'lammap_parallel_gpu_id': os.environ.get('LAMMAP_PARALLEL_GPU_ID'),",
                "}",
                "Path(args.metrics_output).write_text(json.dumps(result), encoding='utf-8')",
                f"raise SystemExit({returncode!r})",
                "",
            ]
        ),
        encoding="utf-8",
    )


class ParallelRunnerCliTest(unittest.TestCase):
    def test_runs_multiple_generated_files_and_writes_summary(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            first = root / "a" / "plan_to_code" / "executable_plan.py"
            second = root / "b" / "plan_to_code" / "executable_plan.py"
            output_dir = root / "runner_results"
            write_fake_generated_script(first, gcr=0.5, stdout_text="first stdout")
            write_fake_generated_script(second, gcr=1.0, stdout_text="second stdout")

            result_code = parallel_runner_main(
                [
                    str(first),
                    str(second),
                    "--output-dir",
                    str(output_dir),
                    "--max-workers",
                    "2",
                    "--timeout-seconds",
                    "5",
                ]
            )

            self.assertEqual(result_code, 0)
            summary = json.loads(
                (output_dir / "parallel_runner_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["total_results"], 2)
            self.assertEqual(summary["success_count"], 2)
            self.assertEqual(summary["failure_count"], 0)
            self.assertEqual(summary["timeout_count"], 0)
            self.assertEqual(sorted(result["gcr"] for result in summary["results"]), [0.5, 1.0])
            self.assertEqual(sorted(output_dir.glob("result_*.json")), [])
            self.assertTrue(
                all("stdout" not in result for result in summary["results"])
            )

    def test_default_gpu_ids_cycle_after_eight_executables(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output_dir = root / "runner_results"
            scripts = []
            for index in range(10):
                script = root / f"run_{index:02d}" / "plan_to_code" / "executable_plan.py"
                write_fake_generated_script(script)
                scripts.append(script)

            result_code = parallel_runner_main(
                [
                    *(str(script) for script in scripts),
                    "--output-dir",
                    str(output_dir),
                    "--max-workers",
                    "10",
                    "--timeout-seconds",
                    "5",
                ]
            )

            self.assertEqual(result_code, 0)
            summary = json.loads(
                (output_dir / "parallel_runner_summary.json").read_text(encoding="utf-8")
            )
            expected_gpu_ids = [0, 1, 2, 3, 4, 5, 6, 7, 0, 1]
            self.assertEqual(
                [result["gpu_id"] for result in summary["results"]],
                expected_gpu_ids,
            )
            self.assertEqual(
                [result["cuda_visible_devices"] for result in summary["results"]],
                [str(gpu_id) for gpu_id in expected_gpu_ids],
            )
            self.assertEqual(
                [result["lammap_parallel_gpu_id"] for result in summary["results"]],
                [str(gpu_id) for gpu_id in expected_gpu_ids],
            )

    def test_custom_global_gpu_ids_are_used_in_order(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output_dir = root / "runner_results"
            scripts = []
            for index in range(5):
                script = root / f"custom_{index:02d}" / "plan_to_code" / "executable_plan.py"
                write_fake_generated_script(script)
                scripts.append(script)

            with patch.object(parallel_runner, "GPU_IDS", [2, 4]):
                result_code = parallel_runner_main(
                    [
                        *(str(script) for script in scripts),
                        "--output-dir",
                        str(output_dir),
                        "--max-workers",
                        "5",
                        "--timeout-seconds",
                        "5",
                    ]
                )

            self.assertEqual(result_code, 0)
            summary = json.loads(
                (output_dir / "parallel_runner_summary.json").read_text(encoding="utf-8")
            )
            expected_gpu_ids = [2, 4, 2, 4, 2]
            self.assertEqual(
                [result["gpu_id"] for result in summary["results"]],
                expected_gpu_ids,
            )
            self.assertEqual(
                [result["cuda_visible_devices"] for result in summary["results"]],
                [str(gpu_id) for gpu_id in expected_gpu_ids],
            )

    def test_empty_gpu_ids_returns_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            script = root / "run" / "plan_to_code" / "executable_plan.py"
            write_fake_generated_script(script)
            output = io.StringIO()

            with patch.object(parallel_runner, "GPU_IDS", []), redirect_stdout(output):
                result_code = parallel_runner_main(
                    [
                        str(script),
                        "--output-dir",
                        str(root / "runner_results"),
                        "--timeout-seconds",
                        "5",
                    ]
                )

            self.assertEqual(result_code, 1)
            self.assertIn(
                "GPU_IDS must contain at least one GPU id",
                output.getvalue(),
            )

    def test_write_individual_results_writes_per_task_json_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            first = root / "a" / "plan_to_code" / "executable_plan.py"
            second = root / "b" / "plan_to_code" / "executable_plan.py"
            output_dir = root / "runner_results"
            write_fake_generated_script(first, gcr=0.5, stdout_text="first stdout")
            write_fake_generated_script(second, gcr=1.0, stdout_text="second stdout")

            result_code = parallel_runner_main(
                [
                    str(first),
                    str(second),
                    "--output-dir",
                    str(output_dir),
                    "--write-individual-results",
                    "--max-workers",
                    "2",
                    "--timeout-seconds",
                    "5",
                ]
            )

            self.assertEqual(result_code, 0)
            result_files = sorted(output_dir.glob("result_*.json"))
            self.assertEqual(len(result_files), 2)
            result_jsons = [
                json.loads(path.read_text(encoding="utf-8")) for path in result_files
            ]
            self.assertEqual(sorted(result["gcr"] for result in result_jsons), [0.5, 1.0])
            self.assertTrue(all("stdout" not in result for result in result_jsons))

    def test_stdout_is_kept_when_robot_failures_are_present(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            script = root / "run" / "plan_to_code" / "executable_plan.py"
            output_dir = root / "runner_results"
            write_fake_generated_script(
                script,
                stdout_text="failure stdout",
                robot_failures=[{"robot_id": "robot1", "error": "failed"}],
            )

            result_code = parallel_runner_main(
                [
                    str(script),
                    "--output-dir",
                    str(output_dir),
                    "--timeout-seconds",
                    "5",
                ]
            )

            self.assertEqual(result_code, 0)
            summary = json.loads(
                (output_dir / "parallel_runner_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["results"][0]["stdout"], "failure stdout\n")

    def test_save_all_stdout_keeps_stdout_without_robot_failures(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            script = root / "run" / "plan_to_code" / "executable_plan.py"
            output_dir = root / "runner_results"
            write_fake_generated_script(script, stdout_text="plain stdout")

            result_code = parallel_runner_main(
                [
                    str(script),
                    "--output-dir",
                    str(output_dir),
                    "--save-all-stdout",
                    "--timeout-seconds",
                    "5",
                ]
            )

            self.assertEqual(result_code, 0)
            summary = json.loads(
                (output_dir / "parallel_runner_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["results"][0]["stdout"], "plain stdout\n")

    def test_root_discovery_runs_generated_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            script = root / "run" / "plan_to_code" / "executable_plan.py"
            output_dir = root / "runner_results"
            write_fake_generated_script(script)

            result_code = parallel_runner_main(
                [
                    "--root",
                    str(root),
                    "--output-dir",
                    str(output_dir),
                    "--timeout-seconds",
                    "5",
                ]
            )

            self.assertEqual(result_code, 0)
            summary = json.loads(
                (output_dir / "parallel_runner_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["total_results"], 1)
            self.assertEqual(summary["results"][0]["executable_path"], str(script.resolve()))

    def test_py_dir_runs_direct_child_python_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            py_dir = root / "generated"
            first = py_dir / "first.py"
            second = py_dir / "second.py"
            output_dir = root / "runner_results"
            write_fake_generated_script(first, gcr=0.25)
            write_fake_generated_script(second, gcr=0.75)

            result_code = parallel_runner_main(
                [
                    "--py-dir",
                    str(py_dir),
                    "--output-dir",
                    str(output_dir),
                    "--max-workers",
                    "2",
                    "--timeout-seconds",
                    "5",
                ]
            )

            self.assertEqual(result_code, 0)
            summary = json.loads(
                (output_dir / "parallel_runner_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["total_results"], 2)
            self.assertEqual(summary["success_count"], 2)
            self.assertEqual(sorted(result["gcr"] for result in summary["results"]), [0.25, 0.75])
            self.assertEqual(
                [result["executable_path"] for result in summary["results"]],
                [str(first.resolve()), str(second.resolve())],
            )

    def test_py_dir_does_not_recurse_into_subdirectories(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            py_dir = root / "generated"
            direct = py_dir / "direct.py"
            nested = py_dir / "nested" / "ignored.py"
            output_dir = root / "runner_results"
            write_fake_generated_script(direct)
            write_fake_generated_script(nested, returncode=9, gcr=0.0)

            result_code = parallel_runner_main(
                [
                    "--py-dir",
                    str(py_dir),
                    "--output-dir",
                    str(output_dir),
                    "--timeout-seconds",
                    "5",
                ]
            )

            self.assertEqual(result_code, 0)
            summary = json.loads(
                (output_dir / "parallel_runner_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["total_results"], 1)
            self.assertEqual(summary["results"][0]["executable_path"], str(direct.resolve()))

    def test_py_dir_missing_directory_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            missing = root / "missing"
            output = io.StringIO()

            with redirect_stdout(output):
                result_code = parallel_runner_main(["--py-dir", str(missing)])

            self.assertEqual(result_code, 1)
            self.assertIn("Python script directory not found", output.getvalue())

    def test_py_dir_empty_directory_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            py_dir = root / "empty"
            py_dir.mkdir()
            output = io.StringIO()

            with redirect_stdout(output):
                result_code = parallel_runner_main(["--py-dir", str(py_dir)])

            self.assertEqual(result_code, 1)
            self.assertIn("No Python scripts found in directory", output.getvalue())

    def test_timeout_is_recorded_without_blocking_other_tasks(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            slow = root / "slow" / "plan_to_code" / "executable_plan.py"
            fast = root / "fast" / "plan_to_code" / "executable_plan.py"
            output_dir = root / "runner_results"
            write_fake_generated_script(slow, sleep_seconds=2.0)
            write_fake_generated_script(fast)

            result_code = parallel_runner_main(
                [
                    str(slow),
                    str(fast),
                    "--output-dir",
                    str(output_dir),
                    "--max-workers",
                    "2",
                    "--timeout-seconds",
                    "0.2",
                ]
            )

            self.assertEqual(result_code, 1)
            summary = json.loads(
                (output_dir / "parallel_runner_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["total_results"], 2)
            self.assertEqual(summary["success_count"], 1)
            self.assertEqual(summary["timeout_count"], 1)
            self.assertTrue(any(result["timed_out"] for result in summary["results"]))

    def test_failed_task_is_recorded_without_blocking_other_tasks(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            failed = root / "failed" / "plan_to_code" / "executable_plan.py"
            success = root / "success" / "plan_to_code" / "executable_plan.py"
            output_dir = root / "runner_results"
            write_fake_generated_script(failed, returncode=7, gcr=0.0)
            write_fake_generated_script(success, returncode=0, gcr=1.0)

            result_code = parallel_runner_main(
                [
                    str(failed),
                    str(success),
                    "--output-dir",
                    str(output_dir),
                    "--max-workers",
                    "2",
                    "--timeout-seconds",
                    "5",
                ]
            )

            self.assertEqual(result_code, 1)
            summary = json.loads(
                (output_dir / "parallel_runner_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["total_results"], 2)
            self.assertEqual(summary["success_count"], 1)
            self.assertEqual(summary["failure_count"], 1)
            self.assertEqual(sorted(result["returncode"] for result in summary["results"]), [0, 7])


class TolerantExecutorTest(unittest.TestCase):
    def test_robot_failure_stops_only_that_robot_in_current_phase(self):
        runtime = FakeRuntime()
        calls = []

        def fake_execute(_adapter, robot_id, action, **_kwargs):
            calls.append((robot_id, action.action_type))
            if robot_id == "robot1" and action.action_type == "OpenObject":
                raise RuntimeError("open failed")
            return FakeEvent()

        plan = TaskPlan(
            "task",
            [
                StagePlan(
                    "Phase 1",
                    {
                        "robot1": [
                            Action("OpenObject", {"args": ("Cabinet",)}),
                            Action("CloseObject", {"args": ("Cabinet",)}),
                        ],
                        "robot2": [
                            Action("PickupObject", {"args": ("Apple",)}),
                            Action("PutObject", {"args": ("Apple", "Table")}),
                        ],
                    },
                )
            ],
        )

        with patch("executor_system.action_plan.AI2ThorAdapter.execute", fake_execute):
            result = run_action_plan_tolerant(runtime, plan, timeout_seconds=5)

        self.assertFalse(result["timed_out"])
        self.assertEqual(result["executed_actions"], 3)
        self.assertEqual(result["failed_actions"], 1)
        self.assertEqual(result["failure_action_ratio"], 1 / 3)
        self.assertIn(("robot1", "OpenObject"), calls)
        self.assertNotIn(("robot1", "CloseObject"), calls)
        self.assertIn(("robot2", "PickupObject"), calls)
        self.assertIn(("robot2", "PutObject"), calls)

    def test_teleport_failure_is_not_counted_as_failed_action(self):
        runtime = FakeRuntime()

        def fake_execute(_adapter, _robot_id, action, **_kwargs):
            if action.action_type == "TeleportObjectToHand":
                raise RuntimeError("teleport failed")
            return FakeEvent()

        plan = TaskPlan(
            "task",
            [
                StagePlan(
                    "Phase 1",
                    {
                        "robot1": [
                            Action("TeleportObjectToHand", {"args": ("Apple",)}),
                            Action("PickupObject", {"args": ("Apple",)}),
                        ],
                    },
                )
            ],
        )

        with patch("executor_system.action_plan.AI2ThorAdapter.execute", fake_execute):
            result = run_action_plan_tolerant(runtime, plan, timeout_seconds=5)

        self.assertEqual(result["executed_actions"], 1)
        self.assertEqual(result["failed_actions"], 0)
        self.assertEqual(result["failure_action_ratio"], 0.0)
        self.assertEqual(len(result["robot_failures"]), 1)
        self.assertTrue(result["robot_failures"][0]["ignored_for_failure_ratio"])

    def test_pickup_clip_failure_is_not_counted_as_failed_action(self):
        runtime = FakeRuntime()

        def fake_execute(_adapter, _robot_id, action, **_kwargs):
            if action.action_type == "PickupObject":
                raise RuntimeError(
                    f"InvalidOperationException: {PICKUP_OBJECT_CLIP_ERROR}"
                )
            return FakeEvent()

        plan = TaskPlan(
            "task",
            [
                StagePlan(
                    "Phase 1",
                    {
                        "robot1": [
                            Action("PickupObject", {"args": ("Apple",)}),
                            Action("PutObject", {"args": ("Apple", "Table")}),
                        ],
                    },
                )
            ],
        )

        with patch("executor_system.action_plan.AI2ThorAdapter.execute", fake_execute):
            result = run_action_plan_tolerant(runtime, plan, timeout_seconds=5)

        self.assertEqual(result["executed_actions"], 1)
        self.assertEqual(result["failed_actions"], 0)
        self.assertEqual(result["failure_action_ratio"], 0.0)
        self.assertEqual(len(result["robot_failures"]), 1)
        self.assertTrue(result["robot_failures"][0]["ignored_for_failure_ratio"])


if __name__ == "__main__":
    unittest.main()
