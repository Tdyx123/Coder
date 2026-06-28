import importlib.util
import json
import py_compile
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "baselines" / "LaMMA-P.py"
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def load_module():
    spec = importlib.util.spec_from_file_location("lammap_final_plan_to_code", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lammap = load_module()


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_run(
    root: Path,
    task_slug: str,
    run_id: str,
    final_plan: str,
    *,
    task: str = "open the drawer, then put the mug on the shelf.",
    manifest_extra=None,
) -> Path:
    run_dir = (
        root
        / "baselines"
        / "LaMMA-P"
        / "logs"
        / "intermediate_runs"
        / "final_test_new_0609_1"
        / task_slug
        / run_id
    )
    write_json(
        run_dir / "inputs" / "task_context.json",
        {
            "task": task,
            "robots": [
                {"name": "robot1", "skills": ["GoToObject", "OpenObject", "PickupObject", "PutObject"]},
                {"name": "robot2", "skills": ["GoToObject", "BreakObject"]},
            ],
            "objects_ai": (
                "objects = ["
                "{'name': 'Drawer', 'mass': 0.0}, "
                "{'name': 'Mug', 'mass': 0.1}, "
                "{'name': 'Shelf', 'mass': 0.0}, "
                "{'name': 'Window', 'mass': 0.0}"
                "]"
            ),
        },
    )
    manifest = {
        "task": task,
        "test_set": "final_test_new_0609_1",
        "floor_plan": "1",
        "task_index": 0,
        "repo_root": str(root),
    }
    if manifest_extra:
        for key, value in manifest_extra.items():
            if value is None:
                manifest.pop(key, None)
            else:
                manifest[key] = value
    write_json(run_dir / "run_manifest.json", manifest)
    dataset_path = root / "data" / "final_test_new_0609_1" / "FloorPlan1.jsonl"
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    dataset_path.write_text(
        json.dumps(
            {
                "task": task,
                "robot list": [1, 2],
                "object_states": [
                    {"name": "Drawer", "contains": [], "states": ["OPENED"]},
                    {"name": "Mug", "contains": [], "states": []},
                ],
                "trans": 3,
                "min_trans": 6,
            },
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    final_path = run_dir / "08_final_match" / "02_final_plan.txt"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_text(final_plan, encoding="utf-8")
    return run_dir


class LaMMAPFinalPlanToCodeTest(unittest.TestCase):
    def test_classifies_and_parses_timed_direct_actions(self):
        result = lammap.classify_final_plan(
            """```pddl
0.000: (gotoobject robot drawer) [1.000]
1.000: (openobject robot drawer) [1.000]
```"""
        )

        self.assertEqual(result.category, "timed_direct_actions")
        self.assertEqual([action.action_type for action in result.actions], ["GoToObject", "OpenObject"])

    def test_classifies_start_end_actions_and_keeps_start_only(self):
        result = lammap.classify_final_plan(
            """```pddl
0.000: (start GoToObject robot2 window) [1.000]
1.000: (end GoToObject robot2 window)
1.000: (start BreakObject robot2 window) [1.000]
2.000: (end BreakObject robot2 window)
```"""
        )

        self.assertEqual(result.category, "timed_start_end_actions")
        self.assertEqual([action.action_type for action in result.actions], ["GoToObject", "BreakObject"])
        resolver = lammap.ObjectNameResolver(["Window"])
        encoded = lammap.encode_actions(result.actions, resolver, [{"name": "robot1"}, {"name": "robot2"}])
        self.assertEqual([action.robot_id for action in encoded], ["robot2", "robot2"])

    def test_hyphenated_action_names_are_normalized(self):
        result = lammap.classify_final_plan(
            """```pddl
0.0: (go-to-object robot drawer) [2.0]
2.0: (open-object robot drawer) [1.5]
```"""
        )

        self.assertEqual(result.category, "timed_direct_actions")
        self.assertEqual([action.action_type for action in result.actions], ["GoToObject", "OpenObject"])

    def test_prepareegg_and_breakegg_actions_encode_as_break_egg(self):
        result = lammap.classify_final_plan(
            """```pddl
0.0: (prepareegg robot1 egg pan) [1.0]
1.0: (breakegg robot1 egg) [1.0]
```"""
        )

        self.assertEqual([action.action_type for action in result.actions], ["BreakEgg", "BreakEgg"])
        resolver = lammap.ObjectNameResolver(["Egg", "Pan"])
        encoded = lammap.encode_actions(result.actions, resolver, [{"name": "robot1"}])
        self.assertEqual([action.action_type for action in encoded], ["BreakEgg", "BreakEgg"])
        self.assertEqual([action.args for action in encoded], [("Egg",), ("Egg",)])

    def test_build_task_plan_data_groups_contiguous_robot_segments(self):
        actions = [
            lammap.EncodedAction(0, "0", 0, "robot1", "GoToObject", ("Drawer",), "a"),
            lammap.EncodedAction(1, "1", 1, "robot1", "OpenObject", ("Drawer",), "b"),
            lammap.EncodedAction(2, "2", 2, "robot2", "GoToObject", ("Window",), "c"),
            lammap.EncodedAction(3, "3", 3, "robot2", "BreakObject", ("Window",), "d"),
            lammap.EncodedAction(4, "4", 4, "robot1", "GoToObject", ("Mug",), "e"),
        ]

        plan_data = lammap.build_task_plan_data("task", actions)

        self.assertEqual(
            [stage["stage_id"] for stage in plan_data["stages"]],
            ["Robot Segment 1", "Robot Segment 2", "Robot Segment 3"],
        )
        self.assertEqual(
            [list(stage["robot_action_queues"]) for stage in plan_data["stages"]],
            [["robot1"], ["robot2"], ["robot1"]],
        )
        self.assertEqual(
            [len(stage["robot_action_queues"][list(stage["robot_action_queues"])[0]]) for stage in plan_data["stages"]],
            [2, 2, 1],
        )

    def test_domain_dump_is_skipped(self):
        result = lammap.classify_final_plan(
            """Looking at the plan:
```pddl
(define (plan task)
  (:objects robot1 - robot drawer - object)
  (:init (not (object-open drawer)))
  (:goal (object-open drawer))
)
```
"""
        )

        self.assertEqual(result.category, "domain_problem_dump")
        self.assertFalse(result.actions)

    def test_process_run_writes_outputs_under_baseline_root(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            logs_dir = root / "baselines" / "LaMMA-P" / "logs" / "intermediate_runs" / "final_test_new_0609_1"
            output_dir = root / "baselines" / "LaMMA-P" / "plan_to_code_results"
            run_dir = write_run(
                root,
                "open_the_drawer_then_put_the_mug",
                "20260621_001",
                """```pddl
0.000: (gotoobject robot drawer) [1.000]
1.000: (openobject robot drawer) [1.000]
2.000: (gotoobject robot mug) [1.000]
3.000: (pickupobject robot mug mug) [1.000]
4.000: (gotoobject robot shelf) [1.000]
5.000: (putobject robot mug shelf) [1.000]
```""",
                manifest_extra={"floor_plan": None, "task_index": None},
            )
            write_json(
                root
                / "baselines"
                / "LaMMA-P"
                / "parallel_runs"
                / "pddlrun_fixture"
                / "FloorPlan1"
                / "summary.json",
                {
                    "floor_plan": "1",
                    "results": [
                        {
                            "task_run_dir": str(run_dir),
                            "task_index": 0,
                            "task": "open the drawer, then put the mug on the shelf.",
                            "status": "success",
                        }
                    ],
                },
            )

            result_code = lammap.main([
                "--logs-dir",
                str(logs_dir),
                "--output-dir",
                str(output_dir),
            ])

            self.assertEqual(result_code, 0)
            run_output = run_dir / "plan_to_code"
            self.assertTrue((run_output / "executable_plan.py").is_file())
            self.assertFalse((run_output / "code_plan.py").exists())
            self.assertFalse((run_output / "encoding_summary.json").exists())
            py_compile.compile(str(run_output / "executable_plan.py"), doraise=True)
            executable_text = (run_output / "executable_plan.py").read_text(encoding="utf-8")
            self.assertIn("generated_plan_runtime", executable_text)
            self.assertIn("BUNDLE_DATA", executable_text)
            self.assertIn("TASK_FILE", executable_text)
            self.assertIn("TASK_INDEX", executable_text)
            global_summary = json.loads((output_dir / "plan_to_code_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(global_summary["successful_generations"], 1)
            results = json.loads((output_dir / "plan_to_code_results.json").read_text(encoding="utf-8"))
            self.assertEqual(results[0]["status"], "success")
            self.assertTrue(results[0]["success"])
            self.assertEqual(results[0]["category"], "timed_direct_actions")
            self.assertEqual(results[0]["action_count"], 6)
            self.assertEqual(results[0]["stage_count"], 1)
            self.assertEqual(results[0]["generated"]["executable_plan"], str(run_output / "executable_plan.py"))

    def test_process_run_skips_domain_dump_without_code(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            logs_dir = root / "baselines" / "LaMMA-P" / "logs" / "intermediate_runs" / "final_test_new_0609_1"
            output_dir = root / "baselines" / "LaMMA-P" / "plan_to_code_results"
            run_dir = write_run(
                root,
                "domain_dump_task",
                "20260621_001",
                """```pddl
(define (plan task)
  (:objects robot1 - robot drawer - object)
  (:init (not (object-open drawer)))
  (:goal (object-open drawer))
)
```""",
            )

            result_code = lammap.main([
                "--logs-dir",
                str(logs_dir),
                "--output-dir",
                str(output_dir),
            ])

            self.assertEqual(result_code, 0)
            run_output = run_dir / "plan_to_code"
            results = json.loads((output_dir / "plan_to_code_results.json").read_text(encoding="utf-8"))
            self.assertEqual(results[0]["status"], "skipped")
            self.assertEqual(results[0]["category"], "domain_problem_dump")
            self.assertFalse(results[0]["success"])
            self.assertFalse((run_output / "code_plan.py").exists())
            self.assertFalse((run_output / "executable_plan.py").exists())
            self.assertFalse((run_output / "encoding_summary.json").exists())


if __name__ == "__main__":
    unittest.main()
