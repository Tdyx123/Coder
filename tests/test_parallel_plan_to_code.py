import io
import json
import py_compile
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from parallel_plan_to_code import (
    ObjectNameResolver,
    PlanEncodingError,
    encode_action,
    main as parallel_plan_to_code_main,
    parse_allocation_phases,
    parse_plan_actions,
    render_code_plan,
)


def write_json(path: Path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")


def write_encoding_fixture_task(root: Path, task_index: int, allocation_text: str) -> Path:
    task_run_dir = root / "logs" / "intermediate_runs" / "sample___6" / "task" / f"20260526_{task_index:03d}"
    task = f"open the cabinet task {task_index}"

    write_json(
        task_run_dir / "inputs" / "task_context.json",
        {
            "task": task,
            "robots": [{"name": "robot1", "skills": ["GoToObject", "OpenObject"]}],
            "objects_ai": "\n\nobjects = [{'name': 'Cabinet', 'mass': 0.0}]",
        },
    )
    (task_run_dir / "02_allocate").mkdir(parents=True)
    (task_run_dir / "02_allocate" / "02_allocate_output.txt").write_text(
        allocation_text,
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
            "task": task,
            "test_set": "sample",
            "floor_plan": "6",
            "task_run_dir": str(task_run_dir),
        },
    )
    return task_run_dir


class ParallelPlanToCodeTest(unittest.TestCase):
    def test_allocation_phases_group_same_robot_subtasks_serially(self):
        allocation = """
# SOLUTION
# Sequence of Operations:
Subtask 1: Robot 2;
Subtask 2: Robot 1;Subtask 3: Robot 1;
"""
        phases = parse_allocation_phases(allocation)

        self.assertEqual(
            [[(item.subtask_id, item.robot_number) for item in phase] for phase in phases],
            [[(1, 2)], [(2, 1), (3, 1)]],
        )

        resolver = ObjectNameResolver(["Window", "Cabinet", "Drawer"])
        encoded_by_subtask = {
            1: [encode_action(parse_plan_actions("(breakobject robot1 window1)")[0], resolver)],
            2: [encode_action(parse_plan_actions("(openobject robot1 cabinet)")[0], resolver)],
            3: [encode_action(parse_plan_actions("(openobject robot1 drawer)")[0], resolver)],
        }
        code_plan = render_code_plan(
            "break the window, then open the cabinet and drawer",
            [{"name": "robot1"}, {"name": "robot2"}],
            phases,
            encoded_by_subtask,
        )

        self.assertIn("args=(robots[0], [run_subtask_02, run_subtask_03])", code_plan)
        self.assertIn("args=(robots[1], [run_subtask_01])", code_plan)

    def test_plan_parser_ignores_cost_and_resolves_object_names(self):
        plan_text = """
(gotoobject robot1 window1)
(gotoobject robot1 counterTop)
(putobject robot1 paper_towel_roll stove_burner)
; cost = 3 (unit cost)
"""
        actions = parse_plan_actions(plan_text)
        resolver = ObjectNameResolver(["Window", "CounterTop", "PaperTowelRoll", "StoveBurner"])
        calls = [encode_action(action, resolver).call for action in actions]

        self.assertEqual(
            calls,
            [
                "GoToObject(robot, 'Window')",
                "GoToObject(robot, 'CounterTop')",
                "PutObject(robot, 'PaperTowelRoll', 'StoveBurner')",
            ],
        )
        self.assertEqual(resolver.mappings["window1"], "Window")
        self.assertEqual(resolver.mappings["counterTop"], "CounterTop")
        self.assertEqual(resolver.mappings["paper_towel_roll"], "PaperTowelRoll")
        self.assertEqual(resolver.mappings["stove_burner"], "StoveBurner")

    def test_parallel_run_fixture_generates_compilable_standalone_scripts(self):
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
                    "objects_ai": "\n\nobjects = [{'name': 'Window', 'mass': 0.0}, {'name': 'Cabinet', 'mass': 0.0}, {'name': 'Drawer', 'mass': 0.0}]",
                },
            )
            (task_run_dir / "02_allocate").mkdir(parents=True)
            (task_run_dir / "02_allocate" / "02_allocate_output.txt").write_text(
                "# Sequence of Operations:\nSubtask 1: Robot 2;\nSubtask 2: Robot 1;Subtask 3: Robot 1;\n",
                encoding="utf-8",
            )
            outputs_dir = task_run_dir / "08_planner" / "outputs"
            outputs_dir.mkdir(parents=True)
            (outputs_dir / "subtask_01_problem_validated_plan.txt").write_text(
                "(gotoobject robot1 window1)\n(breakobject robot1 window1)\n; cost = 2 (unit cost)\n",
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
                    "task": "break the window, then open the cabinet and the drawer.",
                    "test_set": "sample",
                    "floor_plan": "6",
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
                            {"name": "Cabinet", "contains": ["Book"], "states": ["OPENED"]},
                            {"name": "Drawer", "contains": [], "states": ["OPENED"]},
                        ],
                        "trans": 3,
                        "min_trans": 6,
                    },
                    ensure_ascii=False,
                ) + "\n",
                encoding="utf-8",
            )

            parallel_run = root / "parallel_runs" / "pddlrun_fixture"
            write_json(
                parallel_run / "summary.json",
                {
                    "repo_root": str(root),
                    "test_set": "sample",
                    "summaries": [
                        {
                            "floor_plan": "6",
                            "results": [
                                {
                                    "floor_plan": "6",
                                    "task_index": 0,
                                    "task": "break the window, then open the cabinet and the drawer.",
                                    "status": "success",
                                    "task_run_dir": str(task_run_dir),
                                }
                            ],
                        }
                    ],
                },
            )

            result_code = parallel_plan_to_code_main(
                ["--parallel-run", str(parallel_run), "--floor-plan", "6", "--task-index", "0"]
            )

            self.assertEqual(result_code, 0)
            code_plan = task_run_dir / "plan_to_code" / "code_plan.py"
            executable_plan = task_run_dir / "plan_to_code" / "executable_plan.py"
            summary = json.loads((task_run_dir / "plan_to_code" / "encoding_summary.json").read_text(encoding="utf-8"))

            self.assertTrue(code_plan.exists())
            self.assertTrue(executable_plan.exists())
            self.assertEqual(summary["status"], "success")
            self.assertEqual(summary["phase_count"], 2)
            self.assertEqual(summary["no_trans"], 6)
            self.assertEqual(summary["object_mapping_warnings"], [])
            executable_text = executable_plan.read_text(encoding="utf-8")
            self.assertIn("from ai2thor.platform import CloudRendering", executable_text)
            self.assertIn("HEADLESS = True", executable_text)
            self.assertIn("platform=CloudRendering", executable_text)
            self.assertIn("'states': ['BROKEN']", executable_text)
            self.assertIn("states = obj_gt.get(\"states\") or []", executable_text)
            self.assertIn("gcr_tasks += len(states) + len(contains)", executable_text)
            self.assertIn("for state in states:", executable_text)
            self.assertNotIn("obj_gt.get(\"state\")", executable_text)
            py_compile.compile(str(code_plan), doraise=True)
            py_compile.compile(str(executable_plan), doraise=True)

            result_code = parallel_plan_to_code_main(
                ["--parallel-run", str(parallel_run), "--floor-plan", "6", "--task-index", "0", "--no-headless"]
            )

            self.assertEqual(result_code, 0)
            executable_text = executable_plan.read_text(encoding="utf-8")
            self.assertIn("HEADLESS = False", executable_text)
            py_compile.compile(str(executable_plan), doraise=True)

    def test_batch_mode_skips_plan_encoding_errors_and_continues(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bad_task_run_dir = write_encoding_fixture_task(
                root,
                0,
                "# Sequence of Operations:\nNo executable allocation was produced.\n",
            )
            good_task_run_dir = write_encoding_fixture_task(
                root,
                1,
                "# Sequence of Operations:\nSubtask 1: Robot 1;\n",
            )

            parallel_run = root / "parallel_runs" / "pddlrun_fixture"
            write_json(
                parallel_run / "summary.json",
                {
                    "repo_root": str(root),
                    "test_set": "sample",
                    "summaries": [
                        {
                            "floor_plan": "6",
                            "results": [
                                {
                                    "floor_plan": "6",
                                    "task_index": 0,
                                    "task": "open the cabinet task 0",
                                    "status": "success",
                                    "task_run_dir": str(bad_task_run_dir),
                                },
                                {
                                    "floor_plan": "6",
                                    "task_index": 1,
                                    "task": "open the cabinet task 1",
                                    "status": "success",
                                    "task_run_dir": str(good_task_run_dir),
                                },
                            ],
                        }
                    ],
                },
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result_code = parallel_plan_to_code_main(["--parallel-run", str(parallel_run), "--floor-plan", "6"])

            output = stdout.getvalue()
            self.assertEqual(result_code, 0)
            self.assertIn(
                "Skipped task_index=0 FloorPlan6: No subtask-to-robot assignments found in allocation output.",
                output,
            )
            self.assertIn("Generated task_index=1 FloorPlan6", output)
            self.assertIn("Encoded 1 task(s), skipped 1 task(s)", output)

            bad_summary = json.loads(
                (bad_task_run_dir / "plan_to_code" / "encoding_summary.json").read_text(encoding="utf-8")
            )
            good_summary = json.loads(
                (good_task_run_dir / "plan_to_code" / "encoding_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(bad_summary["status"], "error")
            self.assertIn("No subtask-to-robot assignments found", bad_summary["error"])
            self.assertEqual(good_summary["status"], "success")
            self.assertTrue((good_task_run_dir / "plan_to_code" / "code_plan.py").exists())

    def test_task_index_mode_does_not_skip_plan_encoding_errors(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bad_task_run_dir = write_encoding_fixture_task(
                root,
                0,
                "# Sequence of Operations:\nNo executable allocation was produced.\n",
            )

            parallel_run = root / "parallel_runs" / "pddlrun_fixture"
            write_json(
                parallel_run / "summary.json",
                {
                    "repo_root": str(root),
                    "test_set": "sample",
                    "summaries": [
                        {
                            "floor_plan": "6",
                            "results": [
                                {
                                    "floor_plan": "6",
                                    "task_index": 0,
                                    "task": "open the cabinet task 0",
                                    "status": "success",
                                    "task_run_dir": str(bad_task_run_dir),
                                }
                            ],
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(
                PlanEncodingError,
                "No subtask-to-robot assignments found in allocation output",
            ):
                parallel_plan_to_code_main(
                    ["--parallel-run", str(parallel_run), "--floor-plan", "6", "--task-index", "0"]
                )


if __name__ == "__main__":
    unittest.main()
