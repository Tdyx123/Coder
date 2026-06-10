import tempfile
import unittest
from pathlib import Path


import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from executor_system.pddlrun_adapter import (
    PddlRunAdapterError,
    build_task_plan_from_pddlrun_paths,
    parse_plan_actions,
    resolve_plan_files,
)


class PddlRunExecutorAdapterTest(unittest.TestCase):
    def write_file(self, path: Path, content: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_scans_plan_folder_and_builds_parallel_stages(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            allocate_file = self.write_file(
                root / "02_allocate" / "02_allocate_output.txt",
                "# Sequence of Operations:\n"
                "Subtask 1: Robot 2;\n"
                "Subtask 2: Robot 1;Subtask 3: Robot 1;\n",
            )
            plan_folder = root / "08_planner" / "outputs"
            self.write_file(
                plan_folder / "subtask_01_problem_validated_plan.txt",
                "(gotoobject robot1 window1)\n(breakobject robot1 window1)\n",
            )
            self.write_file(
                plan_folder / "subtask_02_problem_validated_plan.txt",
                "(gotoobject robot1 cabinet)\n(openobject robot1 cabinet)\n",
            )
            self.write_file(
                plan_folder / "subtask_03_problem_validated_plan.txt",
                "(gotoobject robot1 drawer)\n(openobject robot1 drawer)\n",
            )

            bundle = build_task_plan_from_pddlrun_paths(
                task="break window and open furniture",
                robots=[{"name": "robot1"}, {"name": "robot2"}],
                allocate_file=allocate_file,
                plan_folder=plan_folder,
                plan_files=[],
                object_names=["Window", "Cabinet", "Drawer"],
            )

            self.assertEqual(len(bundle.task_plan.stages), 2)
            first_stage = bundle.task_plan.stages[0]
            second_stage = bundle.task_plan.stages[1]
            self.assertEqual(list(first_stage.robot_action_queues), ["robot2"])
            self.assertEqual(
                [action.action_type for action in first_stage.robot_action_queues["robot2"]],
                ["GoToObject", "BreakObject"],
            )
            self.assertEqual(
                [action.action_type for action in second_stage.robot_action_queues["robot1"]],
                ["GoToObject", "OpenObject", "GoToObject", "OpenObject"],
            )
            self.assertEqual(
                second_stage.robot_action_queues["robot1"][0].args(),
                ("Cabinet",),
            )
            self.assertEqual(bundle.no_trans, 6)

    def test_explicit_plan_files_override_plan_folder_scan(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            allocate_file = self.write_file(
                root / "02_allocate" / "02_allocate_output.txt",
                "# Sequence of Operations:\nSubtask 1: Robot 1;\n",
            )
            plan_folder = root / "08_planner" / "outputs"
            selected = self.write_file(
                plan_folder / "subtask_01_problem_validated_plan.txt",
                "(gotoobject robot1 book)\n(openobject robot1 book)\n",
            )
            self.write_file(
                plan_folder / "subtask_99_problem_validated_plan.txt",
                "(gotoobject robot1 drawer)\n(openobject robot1 drawer)\n",
            )

            bundle = build_task_plan_from_pddlrun_paths(
                task="open book",
                robots=[{"name": "robot1"}],
                allocate_file=allocate_file,
                plan_folder=plan_folder,
                plan_files=[selected],
                object_names=["Book", "Drawer"],
            )

            self.assertEqual(set(bundle.plan_files), {1})
            self.assertEqual(bundle.no_trans, 2)

    def test_special_action_argument_mapping(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            allocate_file = self.write_file(
                root / "02_allocate" / "02_allocate_output.txt",
                "# Sequence of Operations:\nSubtask 1: Robot 1;\n",
            )
            plan_folder = root / "08_planner" / "outputs"
            self.write_file(
                plan_folder / "subtask_01_problem_validated_plan.txt",
                "\n".join(
                    [
                        "(prepareegg robot1 egg pan)",
                        "(sliceobject robot1 potato countertop knife)",
                        "(cleanobject robot1 bowl sink)",
                        "(runmicrowave robot1 microwave potato)",
                        "(runtoaster robot1 toaster bread)",
                        "(cookbystoveburner robot1 stoveburner pan egg)",
                        "(heatbystoveburner robot1 stoveburner pan)",
                        "(fillwater robot1 sink mug)",
                        "(coldobject robot1 fridge potato)",
                    ]
                )
                + "\n",
            )

            bundle = build_task_plan_from_pddlrun_paths(
                task="special actions",
                robots=[{"name": "robot1"}],
                allocate_file=allocate_file,
                plan_folder=plan_folder,
                plan_files=[],
                object_names=[
                    "Egg",
                    "Pan",
                    "Potato",
                    "CounterTop",
                    "Knife",
                    "Bowl",
                    "Sink",
                    "Microwave",
                    "Toaster",
                    "Bread",
                    "StoveBurner",
                    "Mug",
                    "Fridge",
                ],
            )

            actions = bundle.task_plan.stages[0].robot_action_queues["robot1"]
            self.assertEqual(actions[0].action_type, "PrepareEgg")
            self.assertEqual(actions[0].args(), ("Egg",))
            self.assertEqual(actions[1].args(), ("Potato",))
            self.assertEqual(actions[2].args(), ("Bowl",))
            self.assertEqual(actions[3].args(), ("Microwave", "Potato"))
            self.assertEqual(actions[4].args(), ("Toaster", "Bread"))
            self.assertEqual(actions[5].args(), ("StoveBurner", "Pan", "Egg"))
            self.assertEqual(actions[6].args(), ("StoveBurner", "Pan"))
            self.assertEqual(actions[7].args(), ("Sink", "Mug"))
            self.assertEqual(actions[8].args(), ("Fridge", "Potato"))

    def test_missing_allocate_file_and_plan_folder_report_clear_errors(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with self.assertRaisesRegex(PddlRunAdapterError, "Allocation output file not found"):
                build_task_plan_from_pddlrun_paths(
                    task="missing",
                    robots=[{"name": "robot1"}],
                    allocate_file=root / "missing_allocate.txt",
                    plan_folder=root / "plans",
                )

            allocate_file = self.write_file(
                root / "02_allocate" / "02_allocate_output.txt",
                "# Sequence of Operations:\nSubtask 1: Robot 1;\n",
            )
            with self.assertRaisesRegex(PddlRunAdapterError, "Planner output folder not found"):
                build_task_plan_from_pddlrun_paths(
                    task="missing",
                    robots=[{"name": "robot1"}],
                    allocate_file=allocate_file,
                    plan_folder=root / "missing_plans",
                )

    def test_plan_file_validation_errors_are_clear(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            bad_name = self.write_file(root / "not_a_subtask_plan.txt", "(gotoobject robot1 book)\n")
            with self.assertRaisesRegex(PddlRunAdapterError, "Could not infer subtask id"):
                build_task_plan_from_pddlrun_paths(
                    task="bad filename",
                    robots=[{"name": "robot1"}],
                    allocate_file=self.write_file(
                        root / "02_allocate" / "02_allocate_output.txt",
                        "# Sequence of Operations:\nSubtask 1: Robot 1;\n",
                    ),
                    plan_folder=root,
                    plan_files=[bad_name],
                )

            with self.assertRaisesRegex(PddlRunAdapterError, "Unsupported PDDL action"):
                parse_plan_actions("(unsupportedaction robot1 book)\n")

            with self.assertRaisesRegex(PddlRunAdapterError, "Planner output file"):
                resolve_plan_files(root, [root / "does_not_exist_plan.txt"])


if __name__ == "__main__":
    unittest.main()
