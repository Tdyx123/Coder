import ast
import json
import py_compile
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from executor_system.parallel_runner import is_runner_compatible_executable
from plantocode import main as plantocode_main


def write_json(path: Path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")


def write_parallel_run_fixture_task(
    root: Path,
    floor_plan: str,
    task_index: int,
    log_parts=("logs", "intermediate_runs"),
) -> Path:
    task_run_dir = root
    for part in log_parts:
        task_run_dir = task_run_dir / part
    task_run_dir = (
        task_run_dir
        / f"sample___{floor_plan}"
        / "task"
        / f"20260526_{task_index:03d}"
    )
    write_json(
        task_run_dir / "inputs" / "task_context.json",
        {
            "task": f"open the cabinet on floor {floor_plan}.",
            "robots": [{"name": "robot1", "skills": ["GoToObject", "OpenObject"]}],
            "objects_ai": "objects = [{'name': 'Cabinet'}]",
        },
    )
    (task_run_dir / "02_allocate").mkdir(parents=True)
    (task_run_dir / "02_allocate" / "02_allocate_output.txt").write_text(
        "# Sequence of Operations:\nSubtask 1: Robot 1;\n",
        encoding="utf-8",
    )

    outputs_dir = task_run_dir / "08_planner" / "outputs"
    outputs_dir.mkdir(parents=True)
    plan_path = outputs_dir / "subtask_01_problem_validated_plan.txt"
    plan_path.write_text(
        "(gotoobject robot1 cabinet)\n(openobject robot1 cabinet)\n",
        encoding="utf-8",
    )
    write_json(
        task_run_dir / "08_planner" / "planner_manifest.json",
        [
            {
                "problem_file": "subtask_01_problem_validated.pddl",
                "return_code": 0,
                "compatibility_output": str(plan_path),
            }
        ],
    )
    write_json(
        task_run_dir / "run_manifest.json",
        {
            "repo_root": str(root),
            "task": f"open the cabinet on floor {floor_plan}.",
            "test_set": "sample",
            "floor_plan": floor_plan,
            "task_index": task_index,
            "task_run_dir": str(task_run_dir),
        },
    )

    dataset_dir = root / "data" / "sample"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    lines = ["" for _ in range(task_index + 1)]
    lines[task_index] = json.dumps(
        {
            "task": f"open the cabinet on floor {floor_plan}.",
            "robot list": [1],
            "object_states": [{"name": "Cabinet", "contains": [], "states": ["OPENED"]}],
            "trans": 1,
            "min_trans": 2,
        },
        ensure_ascii=False,
    )
    (dataset_dir / f"FloorPlan{floor_plan}.jsonl").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    return task_run_dir


def write_parallel_run_summary(root: Path, parallel_run: Path, task_run_dirs):
    summaries = []
    for task_run_dir in task_run_dirs:
        manifest = json.loads((task_run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        summaries.append(
            {
                "floor_plan": manifest["floor_plan"],
                "results": [
                    {
                        "floor_plan": manifest["floor_plan"],
                        "task_index": manifest["task_index"],
                        "task": manifest["task"],
                        "status": "success",
                        "task_run_dir": str(task_run_dir),
                    }
                ],
            }
        )
    write_json(
        parallel_run / "summary.json",
        {
            "repo_root": str(root),
            "test_set": "sample",
            "summaries": summaries,
        },
    )


class PlanToCodeDemoBundleTest(unittest.TestCase):
    def test_pddlrun_fixture_generates_demo_style_script_with_hardcoded_bundle(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            task_run_dir = root / "logs" / "intermediate_runs" / "sample___6" / "task" / "20260526_001"

            write_json(
                task_run_dir / "inputs" / "task_context.json",
                {
                    "task": "break the window, then open the cabinet and the drawer.",
                    "robots": [
                        {"name": "robot1", "skills": ["GoToObject", "OpenObject"]},
                        {"name": "robot2", "skills": ["GoToObject", "BreakObject"]},
                    ],
                    "objects_ai": "\n\nobjects = [{'name': 'Window'}, {'name': 'Cabinet'}, {'name': 'Drawer'}]",
                },
            )
            (task_run_dir / "02_allocate").mkdir(parents=True)
            (task_run_dir / "02_allocate" / "02_allocate_output.txt").write_text(
                "# Sequence of Operations:\n"
                "Subtask 1: Robot 2;\n"
                "Subtask 2: Robot 1;Subtask 3: Robot 1;\n",
                encoding="utf-8",
            )

            outputs_dir = task_run_dir / "08_planner" / "outputs"
            outputs_dir.mkdir(parents=True)
            (outputs_dir / "subtask_01_problem_validated_plan.txt").write_text(
                "(gotoobject robot1 window1)\n(breakobject robot1 window1)\n",
                encoding="utf-8",
            )
            (outputs_dir / "subtask_02_problem_validated_plan.txt").write_text(
                "(gotoobject robot1 cabinet)\n(openobject robot1 cabinet)\n",
                encoding="utf-8",
            )
            (outputs_dir / "subtask_03_problem_validated_plan.txt").write_text(
                "(gotoobject robot1 drawer)\n(openobject robot1 drawer)\n",
                encoding="utf-8",
            )
            write_json(
                task_run_dir / "08_planner" / "planner_manifest.json",
                [
                    {
                        "problem_file": "subtask_01_problem_validated.pddl",
                        "return_code": 0,
                        "compatibility_output": str(outputs_dir / "subtask_01_problem_validated_plan.txt"),
                    },
                    {
                        "problem_file": "subtask_02_problem_validated.pddl",
                        "return_code": 0,
                        "compatibility_output": str(outputs_dir / "subtask_02_problem_validated_plan.txt"),
                    },
                    {
                        "problem_file": "subtask_03_problem_validated.pddl",
                        "return_code": 0,
                        "compatibility_output": str(outputs_dir / "subtask_03_problem_validated_plan.txt"),
                    },
                ],
            )
            write_json(
                task_run_dir / "run_manifest.json",
                {
                    "repo_root": str(root),
                    "task": "break the window, then open the cabinet and the drawer.",
                    "test_set": "sample",
                    "floor_plan": "6",
                    "task_index": 0,
                    "task_run_dir": str(task_run_dir),
                    "gpu_device": 0,
                },
            )

            dataset_dir = root / "data" / "sample"
            dataset_dir.mkdir(parents=True)
            (dataset_dir / "FloorPlan6.jsonl").write_text(
                json.dumps(
                    {
                        "task": "break the window, then open the cabinet and the drawer.",
                        "robot list": [1, 2],
                        "object_states": [
                            {"name": "Window", "contains": [], "states": ["BROKEN"]},
                            {"name": "Cabinet", "contains": [], "states": ["OPENED"]},
                            {"name": "Drawer", "contains": [], "states": ["OPENED"]},
                        ],
                        "trans": 3,
                        "min_trans": 6,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            result_code = plantocode_main(
                [
                    "--logs-dir",
                    str(root / "logs"),
                    "--output-dir",
                    str(root / "summary"),
                ]
            )

            self.assertEqual(result_code, 0)
            executable_plan = task_run_dir / "plan_to_code" / "executable_plan.py"
            parallel_executable_plan = task_run_dir / "plan_to_code" / "parallel_executable_plan.py"
            summary = json.loads((root / "summary" / "plan_to_code_summary.json").read_text(encoding="utf-8"))
            details = json.loads((root / "summary" / "plan_to_code_results.json").read_text(encoding="utf-8"))

            self.assertTrue(executable_plan.exists())
            self.assertFalse(parallel_executable_plan.exists())
            self.assertEqual(summary["successful_generations"], 1)
            self.assertEqual(details[0]["status"], "success")
            py_compile.compile(str(executable_plan), doraise=True)

            executable_text = executable_plan.read_text(encoding="utf-8")
            self.assertIn("BUNDLE_DATA =", executable_text)
            self.assertNotIn("TaskPlan.from_dict(BUNDLE_DATA[\"task_plan\"])", executable_text)
            self.assertNotIn("build_task_plan_from_pddlrun_paths(", executable_text)
            self.assertIn("--runner-mode", executable_text)
            self.assertIn("os.environ[\"renderImage\"] = \"0\"", executable_text)
            self.assertIn("run_generated_plan(BUNDLE_DATA, TASK_FILE, TASK_INDEX, __file__)", executable_text)
            self.assertNotIn("DEFAULT_RUNNER_TIMEOUT_SECONDS = 100.0", executable_text)
            self.assertNotIn("run_action_plan_tolerant(", executable_text)
            self.assertNotIn("_Demo2Facade", executable_text)

            parsed = ast.parse(executable_text)
            bundle_data = None
            for node in parsed.body:
                if not isinstance(node, ast.Assign):
                    continue
                if any(isinstance(target, ast.Name) and target.id == "BUNDLE_DATA" for target in node.targets):
                    bundle_data = ast.literal_eval(node.value)
                    break

            self.assertIsNotNone(bundle_data)
            self.assertEqual(bundle_data["no_trans"], 6)
            self.assertNotIn("gpu_device", bundle_data)
            self.assertNotIn("gpu_device", details[0])
            self.assertEqual(len(bundle_data["task_plan"]["stages"]), 2)
            first_stage = bundle_data["task_plan"]["stages"][0]
            second_stage = bundle_data["task_plan"]["stages"][1]
            self.assertEqual(list(first_stage["robot_action_queues"]), ["robot2"])
            self.assertEqual(
                [action["action_type"] for action in first_stage["robot_action_queues"]["robot2"]],
                ["GoToObject", "BreakObject"],
            )
            self.assertEqual(list(second_stage["robot_action_queues"]), ["robot1"])
            self.assertEqual(
                [action["action_type"] for action in second_stage["robot_action_queues"]["robot1"]],
                ["GoToObject", "OpenObject", "GoToObject", "OpenObject"],
            )
            self.assertEqual(bundle_data["object_mapping_warnings"], [])

    def test_lammap_baseline_mode_writes_parallel_runner_summary_path(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            baseline_root = Path(tmp_dir) / "baselines" / "LaMMA-P"
            task_run_dir = write_parallel_run_fixture_task(baseline_root, "6", 0)

            result_code = plantocode_main(
                [
                    "--base-line",
                    "LaMMA-P",
                    "--root",
                    str(baseline_root),
                ]
            )

            self.assertEqual(result_code, 0)
            executable_plan = task_run_dir / "plan_to_code" / "executable_plan.py"
            output_dir = baseline_root / "plan_to_code_results"
            details = json.loads((output_dir / "plan_to_code_results.json").read_text(encoding="utf-8"))
            summary = json.loads((output_dir / "plan_to_code_summary.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["successful_generations"], 1)
            self.assertEqual(details[0]["status"], "success")
            self.assertTrue(details[0]["success"])
            self.assertEqual(details[0]["generated"]["executable_plan"], str(executable_plan))
            self.assertTrue(is_runner_compatible_executable(executable_plan))

    def test_smart_llm_baseline_mode_writes_parallel_runner_summary_path(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            baseline_root = Path(tmp_dir) / "baselines" / "SMART-LLM"
            task_run_dir = write_parallel_run_fixture_task(
                baseline_root,
                "6",
                0,
                log_parts=("logs", "2"),
            )

            result_code = plantocode_main(
                [
                    "--base-line",
                    "SMART-LLM",
                    "--root",
                    str(baseline_root),
                ]
            )

            self.assertEqual(result_code, 0)
            executable_plan = task_run_dir / "plan_to_code" / "executable_plan.py"
            details = json.loads((baseline_root / "plan_to_code_results.json").read_text(encoding="utf-8"))
            summary = json.loads((baseline_root / "plan_to_code_summary.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["successful_generations"], 1)
            self.assertEqual(details[0]["status"], "success")
            self.assertTrue(details[0]["success"])
            self.assertEqual(details[0]["generated"]["executable_plan"], str(executable_plan))
            self.assertTrue(is_runner_compatible_executable(executable_plan))

    def test_baseline_mode_explicit_logs_and_output_dirs_override_defaults(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            baseline_root = root / "baselines" / "LaMMA-P"
            source_root = root / "custom_source"
            output_dir = root / "custom_summary"
            task_run_dir = write_parallel_run_fixture_task(source_root, "6", 0)

            result_code = plantocode_main(
                [
                    "--base-line",
                    "LaMMA-P",
                    "--root",
                    str(baseline_root),
                    "--logs-dir",
                    str(source_root / "logs"),
                    "--output-dir",
                    str(output_dir),
                ]
            )

            self.assertEqual(result_code, 0)
            executable_plan = task_run_dir / "plan_to_code" / "executable_plan.py"
            details = json.loads((output_dir / "plan_to_code_results.json").read_text(encoding="utf-8"))

            self.assertTrue(executable_plan.exists())
            self.assertEqual(details[0]["generated"]["executable_plan"], str(executable_plan))
            self.assertFalse((baseline_root / "plan_to_code_results" / "plan_to_code_results.json").exists())
            self.assertTrue(is_runner_compatible_executable(executable_plan))

    def test_parallel_run_converts_all_summary_tasks(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            floor6_task = write_parallel_run_fixture_task(root, "6", 0)
            floor7_task = write_parallel_run_fixture_task(root, "7", 0)
            parallel_run = root / "parallel_runs" / "pddlrun_fixture"
            write_parallel_run_summary(root, parallel_run, [floor6_task, floor7_task])

            result_code = plantocode_main(
                [
                    "--parallel-run",
                    str(parallel_run),
                    "--output-dir",
                    str(root / "summary"),
                ]
            )

            self.assertEqual(result_code, 0)
            self.assertTrue((floor6_task / "plan_to_code" / "executable_plan.py").exists())
            self.assertTrue((floor7_task / "plan_to_code" / "executable_plan.py").exists())
            summary = json.loads((root / "summary" / "plan_to_code_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["total_results"], 2)
            self.assertEqual(summary["successful_generations"], 2)

    def test_parallel_run_floor_plan_filter_converts_only_matching_tasks(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            floor6_task = write_parallel_run_fixture_task(root, "6", 0)
            floor7_task = write_parallel_run_fixture_task(root, "7", 0)
            parallel_run = root / "parallel_runs" / "pddlrun_fixture"
            write_parallel_run_summary(root, parallel_run, [floor6_task, floor7_task])

            result_code = plantocode_main(
                [
                    "--parallel-run",
                    str(parallel_run),
                    "--floor-plan",
                    "FloorPlan6",
                    "--output-dir",
                    str(root / "summary"),
                ]
            )

            self.assertEqual(result_code, 0)
            self.assertTrue((floor6_task / "plan_to_code" / "executable_plan.py").exists())
            self.assertFalse((floor7_task / "plan_to_code" / "executable_plan.py").exists())
            summary = json.loads((root / "summary" / "plan_to_code_summary.json").read_text(encoding="utf-8"))
            details = json.loads((root / "summary" / "plan_to_code_results.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["total_results"], 1)
            self.assertEqual(summary["successful_generations"], 1)
            self.assertEqual(details[0]["floor_plan"], "6")


if __name__ == "__main__":
    unittest.main()
