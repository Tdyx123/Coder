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

from plantocode import main as plantocode_main


def write_json(path: Path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")


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
                ["--logs-dir", str(root / "logs"), "--output-dir", str(root / "summary")]
            )

            self.assertEqual(result_code, 0)
            executable_plan = task_run_dir / "plan_to_code" / "executable_plan.py"
            summary = json.loads((root / "summary" / "plan_to_code_summary.json").read_text(encoding="utf-8"))
            details = json.loads((root / "summary" / "plan_to_code_results.json").read_text(encoding="utf-8"))

            self.assertTrue(executable_plan.exists())
            self.assertEqual(summary["successful_generations"], 1)
            self.assertEqual(details[0]["status"], "success")
            py_compile.compile(str(executable_plan), doraise=True)

            executable_text = executable_plan.read_text(encoding="utf-8")
            self.assertIn("BUNDLE_DATA =", executable_text)
            self.assertIn("TaskPlan.from_dict(BUNDLE_DATA[\"task_plan\"])", executable_text)
            self.assertNotIn("build_task_plan_from_pddlrun_paths(", executable_text)

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


if __name__ == "__main__":
    unittest.main()
