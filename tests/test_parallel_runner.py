import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from executor_system.action_plan import (
    ACTION_FAILED,
    FAILURE_RETRY,
    Action,
    ExecutionLogger,
    StagePlan,
    TaskPlan,
    TaskRunner,
)
from executor_system.parallel_runner import (
    DEFAULT_TIMEOUT_SECONDS,
    cleanup_gpu_processes,
    main as parallel_runner_main,
    parse_gpu_cleanup_pids,
    run_action_plan_tolerant,
)
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
                "# generated_plan_runtime compatibility marker",
                "# BUNDLE_DATA compatibility marker",
                "import argparse",
                "import json",
                "import os",
                "import time",
                "from pathlib import Path",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--runner-mode', action='store_true')",
                "parser.add_argument('--metrics-output', required=True)",
                "parser.add_argument('--timeout-seconds', type=float, default=30.0)",
                "args = parser.parse_args()",
                f"time.sleep({sleep_seconds!r})",
                f"print({stdout_text!r})" if stdout_text else "pass",
                "result = {",
                f"    'status': {status!r},",
                "    'timed_out': False,",
                "    'run_time_seconds': 0.01,",
                f"    'gcr': {gcr!r},",
                "    'exec_rate': 0.25,",
                "    'executed_actions': 2,",
                "    'failed_actions': 0,",
                "    'failure_action_ratio': 0.0,",
                f"    'robot_failures': {robot_failures!r},",
                "    'observed_env': {",
                "        'CUDA_VISIBLE_DEVICES': os.environ.get('CUDA_VISIBLE_DEVICES'),",
                "        'LAMMAP_PARALLEL_GPU_ID': os.environ.get('LAMMAP_PARALLEL_GPU_ID'),",
                "    },",
                "}",
                "Path(args.metrics_output).write_text(json.dumps(result), encoding='utf-8')",
                f"raise SystemExit({returncode!r})",
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_attempt_sequence_script(path: Path, attempts):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "# generated_plan_runtime compatibility marker",
                "# BUNDLE_DATA compatibility marker",
                "import argparse",
                "import json",
                "import time",
                "from pathlib import Path",
                f"ATTEMPTS = {attempts!r}",
                "counter_path = Path(__file__).with_suffix('.attempt')",
                "try:",
                "    attempt_index = int(counter_path.read_text(encoding='utf-8'))",
                "except Exception:",
                "    attempt_index = 0",
                "counter_path.write_text(str(attempt_index + 1), encoding='utf-8')",
                "config = ATTEMPTS[min(attempt_index, len(ATTEMPTS) - 1)]",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--runner-mode', action='store_true')",
                "parser.add_argument('--metrics-output', required=True)",
                "parser.add_argument('--timeout-seconds', type=float, default=30.0)",
                "args = parser.parse_args()",
                "time.sleep(float(config.get('sleep_seconds', 0.0)))",
                "returncode = int(config.get('returncode', 0))",
                "status = 'success' if returncode == 0 else 'failed'",
                "result = {",
                "    'status': status,",
                "    'timed_out': False,",
                "    'run_time_seconds': float(config.get('run_time_seconds', 0.01)),",
                "    'gcr': config.get('gcr', 1.0 if returncode == 0 else 0.0),",
                "    'exec_rate': 0.25,",
                "    'executed_actions': int(config.get('executed_actions', 2)),",
                "    'failed_actions': int(config.get('failed_actions', 0)),",
                "    'failure_action_ratio': 0.0,",
                "    'robot_failures': [],",
                "}",
                "Path(args.metrics_output).write_text(json.dumps(result), encoding='utf-8')",
                "raise SystemExit(returncode)",
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_incompatible_generated_script(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "print('legacy generated plan')",
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def load_only_summary(output_dir: Path):
    summary_paths = sorted(output_dir.glob("*.json"))
    if len(summary_paths) != 1:
        raise AssertionError(f"Expected one summary JSON, found: {summary_paths}")
    summary_path = summary_paths[0]
    return summary_path, json.loads(summary_path.read_text(encoding="utf-8"))


class ParallelRunnerCliTest(unittest.TestCase):
    def test_default_timeout_seconds_is_30(self):
        self.assertEqual(DEFAULT_TIMEOUT_SECONDS, 30.0)

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
            summary_path, summary = load_only_summary(output_dir)
            self.assertRegex(summary_path.name, r"^\d{4}_01\.json$")
            self.assertEqual(summary["total_results"], 2)
            self.assertEqual(summary["success_count"], 2)
            self.assertEqual(summary["failure_count"], 0)
            self.assertEqual(summary["timeout_count"], 0)
            self.assertEqual(sorted(result["gcr"] for result in summary["results"]), [0.5, 1.0])
            self.assertEqual(
                sorted(result["action_sr"] for result in summary["results"]),
                [1.0, 1.0],
            )
            self.assertTrue(
                all("exec_rate" not in result for result in summary["results"])
            )
            self.assertTrue(
                all("stdout" not in result for result in summary["results"])
            )

    def test_runner_does_not_assign_gpu_environment(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output_dir = root / "runner_results"
            script = root / "run" / "plan_to_code" / "executable_plan.py"
            write_fake_generated_script(script)

            with patch.dict(os.environ, {}, clear=True):
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
            _summary_path, summary = load_only_summary(output_dir)
            result = summary["results"][0]
            self.assertNotIn("gpu_id", result)
            self.assertNotIn("cuda_visible_devices", result)
            self.assertEqual(
                result["observed_env"],
                {
                    "CUDA_VISIBLE_DEVICES": None,
                    "LAMMAP_PARALLEL_GPU_ID": None,
                },
            )

    def test_write_individual_results_argument_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            script = root / "a" / "plan_to_code" / "executable_plan.py"
            output_dir = root / "runner_results"
            write_fake_generated_script(script)

            with self.assertRaises(SystemExit) as exc:
                parallel_runner_main(
                    [
                        str(script),
                        "--output-dir",
                        str(output_dir),
                        "--write-individual-results",
                        "--timeout-seconds",
                        "5",
                    ]
                )

            self.assertEqual(exc.exception.code, 2)
            self.assertEqual(sorted(output_dir.glob("*.json")), [])

    def test_summary_filename_increments_for_existing_daily_output(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            script = root / "run" / "plan_to_code" / "executable_plan.py"
            output_dir = root / "runner_results"
            write_fake_generated_script(script)
            output_dir.mkdir(parents=True)
            existing = output_dir / "1231_01.json"
            write_json(existing, {"previous": True})

            with patch("executor_system.parallel_runner.time.strftime", return_value="1231"):
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
            summary_path = output_dir / "1231_02.json"
            self.assertTrue(summary_path.is_file())
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["total_results"], 1)

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
            _summary_path, summary = load_only_summary(output_dir)
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
            _summary_path, summary = load_only_summary(output_dir)
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
            _summary_path, summary = load_only_summary(output_dir)
            self.assertEqual(summary["total_results"], 1)
            self.assertEqual(summary["results"][0]["executable_path"], str(script.resolve()))

    def test_parallel_run_discovers_only_summary_task_dirs(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            parallel_run = root / "parallel_runs" / "pddlrun_fixture"
            selected_task = root / "logs" / "intermediate_runs" / "selected"
            historical_task = root / "logs" / "intermediate_runs" / "historical"
            selected_script = selected_task / "plan_to_code" / "executable_plan.py"
            historical_script = historical_task / "plan_to_code" / "executable_plan.py"
            output_dir = root / "runner_results"
            write_fake_generated_script(selected_script, gcr=0.5)
            write_fake_generated_script(historical_script, gcr=0.0)

            with patch(
                "executor_system.parallel_runner.pddlrun.discover_parallel_run_task_runs",
                return_value=[selected_task],
            ) as discover:
                result_code = parallel_runner_main(
                    [
                        "--parallel-run",
                        str(parallel_run),
                        "--output-dir",
                        str(output_dir),
                        "--timeout-seconds",
                        "5",
                    ]
                )

            self.assertEqual(result_code, 0)
            discover.assert_called_once_with(str(parallel_run))
            _summary_path, summary = load_only_summary(output_dir)
            self.assertEqual(summary["total_results"], 1)
            self.assertEqual(
                summary["results"][0]["executable_path"],
                str(selected_script.resolve()),
            )

    def test_parallel_run_skips_missing_and_incompatible_generated_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            parallel_run = root / "parallel_runs" / "pddlrun_fixture"
            good_task = root / "logs" / "intermediate_runs" / "good"
            missing_task = root / "logs" / "intermediate_runs" / "missing"
            incompatible_task = root / "logs" / "intermediate_runs" / "legacy"
            good_script = good_task / "plan_to_code" / "executable_plan.py"
            incompatible_script = incompatible_task / "plan_to_code" / "executable_plan.py"
            output_dir = root / "runner_results"
            write_fake_generated_script(good_script, gcr=0.5)
            write_incompatible_generated_script(incompatible_script)
            output = io.StringIO()

            with patch(
                "executor_system.parallel_runner.pddlrun.discover_parallel_run_task_runs",
                return_value=[good_task, missing_task, incompatible_task],
            ):
                with redirect_stdout(output):
                    result_code = parallel_runner_main(
                        [
                            "--parallel-run",
                            str(parallel_run),
                            "--output-dir",
                            str(output_dir),
                            "--timeout-seconds",
                            "5",
                        ]
                    )

            self.assertEqual(result_code, 0)
            self.assertIn(
                "Skipped 1 missing, 1 incompatible generated executable(s)",
                output.getvalue(),
            )
            _summary_path, summary = load_only_summary(output_dir)
            self.assertEqual(summary["total_results"], 1)
            self.assertEqual(
                summary["results"][0]["executable_path"],
                str(good_script.resolve()),
            )

    def test_parallel_run_without_compatible_files_returns_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            parallel_run = root / "parallel_runs" / "pddlrun_fixture"
            missing_task = root / "logs" / "intermediate_runs" / "missing"
            output = io.StringIO()

            with patch(
                "executor_system.parallel_runner.pddlrun.discover_parallel_run_task_runs",
                return_value=[missing_task],
            ):
                with redirect_stdout(output):
                    result_code = parallel_runner_main(
                        ["--parallel-run", str(parallel_run), "--timeout-seconds", "5"]
                    )

            self.assertEqual(result_code, 1)
            self.assertIn(
                "No runner-compatible plan_to_code/executable_plan.py files found",
                output.getvalue(),
            )
            self.assertIn(
                "python scripts/plantocode.py --parallel-run",
                output.getvalue(),
            )

    def test_parallel_run_cannot_be_combined_with_other_discovery_modes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            parallel_run = root / "parallel_runs" / "pddlrun_fixture"
            conflicts = [
                [str(root / "plan_to_code" / "executable_plan.py")],
                ["--root", str(root)],
                ["--py-dir", str(root)],
                ["--base-line", "LaMMA-P"],
            ]

            for conflict in conflicts:
                output = io.StringIO()
                with self.subTest(conflict=conflict):
                    with redirect_stdout(output):
                        result_code = parallel_runner_main(
                            ["--parallel-run", str(parallel_run), *conflict]
                        )

                    self.assertEqual(result_code, 1)
                    self.assertIn(
                        "--parallel-run cannot be combined",
                        output.getvalue(),
                    )

    def test_lammap_baseline_discovers_successful_summary_entries(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "baselines" / "LaMMA-P"
            script = (
                root
                / "logs"
                / "intermediate_runs"
                / "test_set"
                / "task"
                / "run"
                / "plan_to_code"
                / "executable_plan.py"
            )
            fallback_only = (
                root
                / "logs"
                / "intermediate_runs"
                / "test_set"
                / "other"
                / "run"
                / "plan_to_code"
                / "executable_plan.py"
            )
            skipped = (
                root
                / "logs"
                / "intermediate_runs"
                / "test_set"
                / "skipped"
                / "run"
                / "plan_to_code"
                / "executable_plan.py"
            )
            legacy = (
                root
                / "logs"
                / "intermediate_runs"
                / "test_set"
                / "legacy"
                / "run"
                / "plan_to_code"
                / "executable_plan.py"
            )
            output_dir = Path(tmp_dir) / "runner_results"
            write_fake_generated_script(script, gcr=0.5)
            write_fake_generated_script(fallback_only, gcr=0.75)
            write_fake_generated_script(skipped, gcr=0.0)
            write_incompatible_generated_script(legacy)
            write_json(
                root / "plan_to_code_results" / "plan_to_code_results.json",
                [
                    {
                        "status": "success",
                        "success": True,
                        "generated": {"executable_plan": str(script)},
                    },
                    {
                        "status": "skipped",
                        "success": False,
                        "generated": {"executable_plan": str(skipped)},
                    },
                    {
                        "status": "success",
                        "success": True,
                        "generated": {"executable_plan": str(legacy)},
                    },
                    {
                        "status": "success",
                        "success": True,
                        "generated": {"executable_plan": str(root / "missing.py")},
                    },
                ],
            )

            result_code = parallel_runner_main(
                [
                    "--base-line",
                    "LaMMA-P",
                    "--root",
                    str(root),
                    "--output-dir",
                    str(output_dir),
                    "--timeout-seconds",
                    "5",
                ]
            )

            self.assertEqual(result_code, 0)
            summary_path, summary = load_only_summary(output_dir)
            self.assertRegex(summary_path.name, r"^LaMMA-P_\d{4}_01\.json$")
            self.assertEqual(summary["base_line"], "LaMMA-P")
            self.assertEqual(summary["discovery_root"], str(root.resolve()))
            self.assertEqual(summary["total_results"], 1)
            self.assertEqual(summary["results"][0]["executable_path"], str(script.resolve()))

    def test_smart_llm_baseline_discovers_successful_summary_entries(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "baselines" / "SMART-LLM"
            script = root / "logs" / "2" / "task" / "plan_to_code" / "executable_plan.py"
            skipped = root / "logs" / "2" / "skipped" / "plan_to_code" / "executable_plan.py"
            output_dir = Path(tmp_dir) / "runner_results"
            write_fake_generated_script(script, gcr=0.25)
            write_fake_generated_script(skipped, gcr=0.0)
            write_json(
                root / "plan_to_code_results.json",
                [
                    {
                        "status": "success",
                        "success": True,
                        "generated": {"executable_plan": str(script)},
                    },
                    {
                        "status": "failed",
                        "success": False,
                        "generated": {"executable_plan": str(skipped)},
                    },
                ],
            )

            result_code = parallel_runner_main(
                [
                    "--base-line",
                    "SMART-LLM",
                    "--root",
                    str(root),
                    "--output-dir",
                    str(output_dir),
                    "--timeout-seconds",
                    "5",
                ]
            )

            self.assertEqual(result_code, 0)
            summary_path, summary = load_only_summary(output_dir)
            self.assertRegex(summary_path.name, r"^SMART-LLM_\d{4}_01\.json$")
            self.assertEqual(summary["base_line"], "SMART-LLM")
            self.assertEqual(summary["discovery_root"], str(root.resolve()))
            self.assertEqual(summary["total_results"], 1)
            self.assertEqual(summary["results"][0]["executable_path"], str(script.resolve()))

    def test_baseline_fallback_discovers_compatible_log_executables(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "baselines" / "SMART-LLM"
            first = root / "logs" / "2" / "first" / "plan_to_code" / "executable_plan.py"
            second = root / "logs" / "2" / "second" / "plan_to_code" / "executable_plan.py"
            legacy = root / "logs" / "2" / "legacy" / "plan_to_code" / "executable_plan.py"
            output_dir = Path(tmp_dir) / "runner_results"
            write_fake_generated_script(first, gcr=0.25)
            write_fake_generated_script(second, gcr=0.75)
            write_incompatible_generated_script(legacy)

            result_code = parallel_runner_main(
                [
                    "--base-line",
                    "SMART-LLM",
                    "--root",
                    str(root),
                    "--output-dir",
                    str(output_dir),
                    "--max-workers",
                    "2",
                    "--timeout-seconds",
                    "5",
                ]
            )

            self.assertEqual(result_code, 0)
            _summary_path, summary = load_only_summary(output_dir)
            self.assertEqual(summary["total_results"], 2)
            self.assertEqual(
                [result["executable_path"] for result in summary["results"]],
                [str(first.resolve()), str(second.resolve())],
            )

    def test_baseline_without_compatible_files_returns_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "baselines" / "LaMMA-P"
            legacy = (
                root
                / "logs"
                / "intermediate_runs"
                / "test_set"
                / "task"
                / "run"
                / "plan_to_code"
                / "executable_plan.py"
            )
            write_incompatible_generated_script(legacy)
            output = io.StringIO()

            with redirect_stdout(output):
                result_code = parallel_runner_main(
                    [
                        "--base-line",
                        "LaMMA-P",
                        "--root",
                        str(root),
                        "--timeout-seconds",
                        "5",
                    ]
                )

            self.assertEqual(result_code, 1)
            self.assertIn(
                "No runner-compatible LaMMA-P executable_plan.py files discovered",
                output.getvalue(),
            )

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
            _summary_path, summary = load_only_summary(output_dir)
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
            _summary_path, summary = load_only_summary(output_dir)
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
            _summary_path, summary = load_only_summary(output_dir)
            self.assertEqual(summary["total_results"], 2)
            self.assertEqual(summary["success_count"], 1)
            self.assertEqual(summary["timeout_count"], 1)
            self.assertTrue(any(result["timed_out"] for result in summary["results"]))
            timeout_result = next(result for result in summary["results"] if result["timed_out"])
            self.assertEqual(timeout_result["action_sr"], 1.0)
            self.assertNotIn("exec_rate", timeout_result)
            self.assertEqual(timeout_result["attempt_count"], 3)
            self.assertEqual(timeout_result["timed_out_attempt_count"], 3)

    def test_timeout_task_is_retried_after_initial_round_and_can_succeed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            slow = root / "slow" / "plan_to_code" / "executable_plan.py"
            fast = root / "fast" / "plan_to_code" / "executable_plan.py"
            output_dir = root / "runner_results"
            write_attempt_sequence_script(
                slow,
                [
                    {"sleep_seconds": 0.3},
                    {"sleep_seconds": 0.0, "gcr": 0.9},
                ],
            )
            write_fake_generated_script(fast)

            def fake_cleanup(round_index):
                return {
                    "round": round_index,
                    "matched_pids": [],
                    "killed_pids": [],
                    "error": "",
                }

            with patch(
                "executor_system.parallel_runner.cleanup_gpu_processes",
                side_effect=fake_cleanup,
            ):
                result_code = parallel_runner_main(
                    [
                        str(slow),
                        str(fast),
                        "--output-dir",
                        str(output_dir),
                        "--max-workers",
                        "2",
                        "--timeout-seconds",
                        "0.1",
                    ]
                )

            self.assertEqual(result_code, 0)
            _summary_path, summary = load_only_summary(output_dir)
            self.assertEqual(summary["total_results"], 2)
            self.assertEqual(summary["success_count"], 2)
            self.assertEqual(summary["timeout_count"], 0)
            self.assertEqual(summary["timeout_retry_tasks"], [str(slow)])
            self.assertEqual(len(summary["gpu_cleanup_events"]), 2)
            slow_result = next(
                result
                for result in summary["results"]
                if result["executable_path"] == str(slow)
            )
            self.assertFalse(slow_result["timed_out"])
            self.assertEqual(slow_result["attempt_count"], 2)
            self.assertEqual(slow_result["timed_out_attempt_count"], 1)
            self.assertTrue(slow_result["attempts"][0]["timed_out"])
            self.assertFalse(slow_result["attempts"][1]["timed_out"])

    def test_timeout_task_gets_only_two_retry_rounds(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            slow = root / "slow" / "plan_to_code" / "executable_plan.py"
            output_dir = root / "runner_results"
            write_attempt_sequence_script(
                slow,
                [
                    {"sleep_seconds": 0.2},
                    {"sleep_seconds": 0.2},
                    {"sleep_seconds": 0.2},
                    {"sleep_seconds": 0.0, "gcr": 1.0},
                ],
            )

            def fake_cleanup(round_index):
                return {
                    "round": round_index,
                    "matched_pids": [],
                    "killed_pids": [],
                    "error": "",
                }

            with patch(
                "executor_system.parallel_runner.cleanup_gpu_processes",
                side_effect=fake_cleanup,
            ):
                result_code = parallel_runner_main(
                    [
                        str(slow),
                        "--output-dir",
                        str(output_dir),
                        "--timeout-seconds",
                        "0.05",
                    ]
                )

            self.assertEqual(result_code, 1)
            _summary_path, summary = load_only_summary(output_dir)
            self.assertEqual(summary["success_count"], 0)
            self.assertEqual(summary["timeout_count"], 1)
            self.assertEqual(summary["timeout_retry_tasks"], [str(slow)])
            self.assertEqual(len(summary["gpu_cleanup_events"]), 3)
            result = summary["results"][0]
            self.assertTrue(result["timed_out"])
            self.assertEqual(result["attempt_count"], 3)
            self.assertEqual(result["timed_out_attempt_count"], 3)
            self.assertEqual(
                [attempt["attempt"] for attempt in result["attempts"]],
                [1, 2, 3],
            )

    def test_gpu_cleanup_runs_after_each_full_round(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            slow = root / "slow" / "plan_to_code" / "executable_plan.py"
            fast = root / "fast" / "plan_to_code" / "executable_plan.py"
            output_dir = root / "runner_results"
            write_fake_generated_script(slow)
            write_fake_generated_script(fast)
            events = []
            call_counts = {}

            def fake_run_generated_executable(executable_path, **_kwargs):
                path = Path(executable_path)
                call_counts[path] = call_counts.get(path, 0) + 1
                events.append(("run", path.parent.parent.name, call_counts[path]))
                timed_out = path == slow and call_counts[path] == 1
                return {
                    "status": "timeout" if timed_out else "success",
                    "timed_out": timed_out,
                    "timeout_message": "synthetic timeout" if timed_out else "",
                    "run_time_seconds": 0.01,
                    "gcr": None,
                    "executed_actions": 0,
                    "failed_actions": 0,
                    "action_sr": 1.0,
                    "failure_action_ratio": 0.0,
                    "robot_failures": [],
                    "returncode": 124 if timed_out else 0,
                    "executable_path": str(path),
                }

            def fake_cleanup(round_index):
                events.append(("cleanup", round_index))
                return {
                    "round": round_index,
                    "matched_pids": [],
                    "killed_pids": [],
                    "error": "",
                }

            with patch(
                "executor_system.parallel_runner.run_generated_executable",
                side_effect=fake_run_generated_executable,
            ), patch(
                "executor_system.parallel_runner.cleanup_gpu_processes",
                side_effect=fake_cleanup,
            ):
                result_code = parallel_runner_main(
                    [
                        str(slow),
                        str(fast),
                        "--output-dir",
                        str(output_dir),
                        "--max-workers",
                        "1",
                        "--timeout-seconds",
                        "5",
                    ]
                )

            self.assertEqual(result_code, 0)
            self.assertEqual(
                events,
                [
                    ("run", "slow", 1),
                    ("run", "fast", 1),
                    ("cleanup", 0),
                    ("run", "slow", 2),
                    ("cleanup", 1),
                ],
            )

    def test_gpu_cleanup_handles_unavailable_nvidia_smi(self):
        completed = SimpleNamespace(
            returncode=9,
            stdout="",
            stderr="NVIDIA-SMI has failed",
        )

        with patch(
            "executor_system.parallel_runner.subprocess.run",
            return_value=completed,
        ):
            event = cleanup_gpu_processes(0)

        self.assertEqual(event["round"], 0)
        self.assertEqual(event["matched_pids"], [])
        self.assertEqual(event["killed_pids"], [])
        self.assertIn("NVIDIA-SMI has failed", event["error"])

    def test_gpu_cleanup_exception_is_recorded_without_failing_tasks(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            script = root / "run" / "plan_to_code" / "executable_plan.py"
            output_dir = root / "runner_results"
            write_fake_generated_script(script)

            with patch(
                "executor_system.parallel_runner.cleanup_gpu_processes",
                side_effect=RuntimeError("cleanup exploded"),
            ):
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
            _summary_path, summary = load_only_summary(output_dir)
            self.assertEqual(summary["success_count"], 1)
            self.assertEqual(len(summary["gpu_cleanup_events"]), 1)
            self.assertIn("cleanup exploded", summary["gpu_cleanup_events"][0]["error"])

    def test_gpu_cleanup_only_kills_matching_process_suffix(self):
        query_output = "\n".join(
            [
                "1234, /tmp/not_this_process",
                "5678, /tmp/worker_0d69f666c7f282e54abfe58f1e917",
            ]
        )
        completed_query = SimpleNamespace(returncode=0, stdout=query_output, stderr="")
        completed_kill = SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch(
            "executor_system.parallel_runner.subprocess.run",
            side_effect=[completed_query, completed_kill],
        ) as run_mock:
            event = cleanup_gpu_processes(2)

        self.assertEqual(
            parse_gpu_cleanup_pids(query_output),
            [5678],
        )
        self.assertEqual(event["round"], 2)
        self.assertEqual(event["matched_pids"], [5678])
        self.assertEqual(event["killed_pids"], [5678])
        self.assertEqual(run_mock.call_args_list[1].args[0], ["kill", "-9", "5678"])

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
            _summary_path, summary = load_only_summary(output_dir)
            self.assertEqual(summary["total_results"], 2)
            self.assertEqual(summary["success_count"], 1)
            self.assertEqual(summary["failure_count"], 1)
            self.assertEqual(sorted(result["returncode"] for result in summary["results"]), [0, 7])
            self.assertTrue(
                all("exec_rate" not in result for result in summary["results"])
            )

    def test_future_exception_result_has_action_sr(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            script = root / "failed" / "plan_to_code" / "executable_plan.py"
            output_dir = root / "runner_results"
            write_fake_generated_script(script)

            with patch(
                "executor_system.parallel_runner.run_generated_executable",
                side_effect=RuntimeError("runner exploded"),
            ):
                result_code = parallel_runner_main(
                    [
                        str(script),
                        "--output-dir",
                        str(output_dir),
                        "--timeout-seconds",
                        "5",
                    ]
                )

            self.assertEqual(result_code, 1)
            _summary_path, summary = load_only_summary(output_dir)
            result = summary["results"][0]
            self.assertEqual(result["executed_actions"], 0)
            self.assertEqual(result["failed_actions"], 0)
            self.assertEqual(result["action_sr"], 1.0)
            self.assertNotIn("exec_rate", result)


class TolerantExecutorTest(unittest.TestCase):
    def test_robot_failure_continues_with_next_action(self):
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
        self.assertEqual(result["executed_actions"], 4)
        self.assertEqual(result["failed_actions"], 1)
        self.assertEqual(result["action_sr"], 3 / 4)
        self.assertEqual(result["failure_action_ratio"], 1 / 4)
        self.assertIn(("robot1", "OpenObject"), calls)
        self.assertIn(("robot1", "CloseObject"), calls)
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

        self.assertEqual(result["executed_actions"], 2)
        self.assertEqual(result["failed_actions"], 0)
        self.assertEqual(result["action_sr"], 1.0)
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

        self.assertEqual(result["executed_actions"], 2)
        self.assertEqual(result["failed_actions"], 0)
        self.assertEqual(result["action_sr"], 1.0)
        self.assertEqual(result["failure_action_ratio"], 0.0)
        self.assertEqual(len(result["robot_failures"]), 1)
        self.assertTrue(result["robot_failures"][0]["ignored_for_failure_ratio"])

    def test_retryable_action_retries_then_continues_in_runner_mode(self):
        runtime = FakeRuntime()
        calls = []

        def fake_execute(_adapter, robot_id, action, **_kwargs):
            calls.append((robot_id, action.action_type))
            if action.action_type == "Teleport":
                raise RuntimeError("teleport failed")
            return FakeEvent()

        plan = TaskPlan(
            "task",
            [
                StagePlan(
                    "Phase 1",
                    {
                        "robot1": [
                            Action(
                                "Teleport",
                                {},
                                on_failure=FAILURE_RETRY,
                                max_retries=1,
                            ),
                            Action("CloseObject", {"args": ("Cabinet",)}),
                        ],
                    },
                )
            ],
        )

        with patch("executor_system.action_plan.AI2ThorAdapter.execute", fake_execute):
            result = run_action_plan_tolerant(runtime, plan, timeout_seconds=5)

        self.assertEqual(calls.count(("robot1", "Teleport")), 2)
        self.assertIn(("robot1", "CloseObject"), calls)
        self.assertEqual(result["executed_actions"], 3)
        self.assertEqual(result["failed_actions"], 0)
        self.assertEqual(result["action_sr"], 1.0)
        self.assertEqual(len(result["robot_failures"]), 1)
        self.assertTrue(result["robot_failures"][0]["ignored_for_failure_ratio"])


class OrdinaryExecutorFailureContinuationTest(unittest.TestCase):
    def test_task_runner_continues_after_robot_action_failure(self):
        runtime = FakeRuntime()
        calls = []
        logger = ExecutionLogger()

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
            TaskRunner(runtime, logger=logger).execute(plan)

        self.assertIn(("robot1", "OpenObject"), calls)
        self.assertIn(("robot1", "CloseObject"), calls)
        self.assertIn(("robot2", "PickupObject"), calls)
        self.assertIn(("robot2", "PutObject"), calls)
        failed_results = [
            record for record in logger.records if record.status == ACTION_FAILED
        ]
        self.assertEqual(len(failed_results), 1)
        self.assertEqual(failed_results[0].action.action_type, "OpenObject")

    def test_wait_condition_timeout_is_skipped_and_next_action_runs(self):
        runtime = FakeRuntime()
        calls = []
        logger = ExecutionLogger()

        def fake_execute(_adapter, robot_id, action, **_kwargs):
            calls.append((robot_id, action.action_type))
            return FakeEvent()

        plan = TaskPlan(
            "task",
            [
                StagePlan(
                    "Phase 1",
                    {
                        "robot1": [
                            Action(
                                "Wait",
                                {},
                                wait_until=lambda _world_state: False,
                                timeout_ticks=0,
                            ),
                            Action("CloseObject", {"args": ("Cabinet",)}),
                        ],
                    },
                )
            ],
        )

        with patch("executor_system.action_plan.AI2ThorAdapter.execute", fake_execute):
            TaskRunner(runtime, logger=logger).execute(plan)

        self.assertNotIn(("robot1", "Wait"), calls)
        self.assertIn(("robot1", "CloseObject"), calls)
        failed_results = [
            record for record in logger.records if record.status == ACTION_FAILED
        ]
        self.assertEqual(len(failed_results), 1)
        self.assertEqual(failed_results[0].action.action_type, "Wait")

    def test_retryable_action_retries_then_skips_and_continues(self):
        runtime = FakeRuntime()
        calls = []
        logger = ExecutionLogger()

        def fake_execute(_adapter, robot_id, action, **_kwargs):
            calls.append((robot_id, action.action_type))
            if action.action_type == "Teleport":
                raise RuntimeError("teleport failed")
            return FakeEvent()

        plan = TaskPlan(
            "task",
            [
                StagePlan(
                    "Phase 1",
                    {
                        "robot1": [
                            Action(
                                "Teleport",
                                {},
                                on_failure=FAILURE_RETRY,
                                max_retries=1,
                            ),
                            Action("CloseObject", {"args": ("Cabinet",)}),
                        ],
                    },
                )
            ],
        )

        with patch("executor_system.action_plan.AI2ThorAdapter.execute", fake_execute):
            TaskRunner(runtime, logger=logger).execute(plan)

        self.assertEqual(calls.count(("robot1", "Teleport")), 2)
        self.assertIn(("robot1", "CloseObject"), calls)
        failed_results = [
            record for record in logger.records if record.status == ACTION_FAILED
        ]
        self.assertEqual(
            [record.action.action_type for record in failed_results],
            ["Teleport", "Teleport"],
        )


if __name__ == "__main__":
    unittest.main()
