import json
import sys
import tempfile
import unittest
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


def write_fake_generated_script(path: Path, *, sleep_seconds=0.0, returncode=0, gcr=1.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    status = "success" if returncode == 0 else "failed"
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import argparse",
                "import json",
                "import time",
                "from pathlib import Path",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--runner-mode', action='store_true')",
                "parser.add_argument('--metrics-output', required=True)",
                "parser.add_argument('--timeout-seconds', type=float, default=100.0)",
                "args = parser.parse_args()",
                f"time.sleep({sleep_seconds!r})",
                "result = {",
                f"    'status': {status!r},",
                "    'timed_out': False,",
                "    'run_time_seconds': 0.01,",
                f"    'gcr': {gcr!r},",
                "    'executed_actions': 2,",
                "    'failed_actions': 0,",
                "    'failure_action_ratio': 0.0,",
                "    'robot_failures': [],",
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
            write_fake_generated_script(first, gcr=0.5)
            write_fake_generated_script(second, gcr=1.0)

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


if __name__ == "__main__":
    unittest.main()
