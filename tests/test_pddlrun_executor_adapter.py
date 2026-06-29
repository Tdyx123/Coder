import json
import tempfile
import unittest
from pathlib import Path


import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from executor_system.pddlrun_adapter import (
    ObjectNameResolver,
    PddlRunAdapterError,
    build_task_plan_from_pddlrun_paths,
    encode_plan_action,
    parse_plan_actions,
    resolve_plan_files,
)
from executor_system.action_plan import (
    AI2ThorAdapter,
    Action,
    MultiStageActionPlan,
    PlanValidator,
    ResourceInferencer,
    StagePlan,
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

    def test_allocation_subtasks_without_plans_are_skipped(self):
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
                plan_folder / "subtask_03_problem_validated_plan.txt",
                "(gotoobject robot1 drawer)\n(openobject robot1 drawer)\n",
            )

            bundle = build_task_plan_from_pddlrun_paths(
                task="break window and open drawer",
                robots=[{"name": "robot1"}, {"name": "robot2"}],
                allocate_file=allocate_file,
                plan_folder=plan_folder,
                plan_files=[],
                object_names=["Window", "Cabinet", "Drawer"],
            )

            self.assertEqual(
                [[(item.subtask_id, item.robot_number) for item in phase] for phase in bundle.phases],
                [[(1, 2)], [(3, 1)]],
            )
            self.assertEqual(len(bundle.task_plan.stages), 2)
            first_stage = bundle.task_plan.stages[0]
            second_stage = bundle.task_plan.stages[1]
            self.assertEqual(list(first_stage.robot_action_queues), ["robot2"])
            self.assertEqual(
                [action.action_type for action in first_stage.robot_action_queues["robot2"]],
                ["GoToObject", "BreakObject"],
            )
            self.assertEqual(list(second_stage.robot_action_queues), ["robot1"])
            self.assertEqual(
                [action.action_type for action in second_stage.robot_action_queues["robot1"]],
                ["GoToObject", "OpenObject"],
            )
            self.assertEqual(bundle.no_trans, 4)

    def test_unassigned_planner_outputs_are_appended_in_final_phase(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            allocate_file = self.write_file(
                root / "02_allocate" / "02_allocate_output.txt",
                "# Sequence of Operations:\n"
                "Subtask 1: Robot 1;\n"
                "Subtask 2: Robot 1;\n",
            )
            plan_folder = root / "08_planner" / "outputs"
            self.write_file(
                plan_folder / "subtask_01_problem_validated_plan.txt",
                "(gotoobject robot1 cabinet)\n(openobject robot1 cabinet)\n",
            )
            self.write_file(
                plan_folder / "subtask_03_problem_validated_plan.txt",
                "(gotoobject robot2 drawer)\n(openobject robot2 drawer)\n",
            )

            bundle = build_task_plan_from_pddlrun_paths(
                task="open cabinet and drawer",
                robots=[{"name": "robot1"}, {"name": "robot2"}],
                allocate_file=allocate_file,
                plan_folder=plan_folder,
                plan_files=[],
                object_names=["Cabinet", "Drawer"],
            )

            self.assertEqual(
                [[(item.subtask_id, item.robot_number) for item in phase] for phase in bundle.phases],
                [[(1, 1)], [(3, 2)]],
            )
            self.assertEqual(len(bundle.task_plan.stages), 2)
            self.assertEqual(bundle.task_plan.stages[1].stage_id, "Phase 2")
            self.assertEqual(list(bundle.task_plan.stages[1].robot_action_queues), ["robot2"])
            self.assertEqual(
                [action.action_type for action in bundle.task_plan.stages[1].robot_action_queues["robot2"]],
                ["GoToObject", "OpenObject"],
            )
            self.assertEqual(bundle.no_trans, 4)

    def test_allocation_without_assignments_falls_back_to_robot1_for_all_plans(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            allocate_file = self.write_file(
                root / "02_allocate" / "02_allocate_output.txt",
                "# Sequence of Operations:\nNo executable allocation was produced.\n",
            )
            plan_folder = root / "08_planner" / "outputs"
            self.write_file(
                plan_folder / "subtask_01_problem_validated_plan.txt",
                "(gotoobject robot2 cabinet)\n(openobject robot2 cabinet)\n",
            )
            self.write_file(
                plan_folder / "subtask_03_problem_validated_plan.txt",
                "(gotoobject robot2 drawer)\n(openobject robot2 drawer)\n",
            )

            bundle = build_task_plan_from_pddlrun_paths(
                task="open cabinet and drawer",
                robots=[{"name": "robot1"}, {"name": "robot2"}],
                allocate_file=allocate_file,
                plan_folder=plan_folder,
                plan_files=[],
                object_names=["Cabinet", "Drawer"],
            )

            self.assertEqual(
                [[(item.subtask_id, item.robot_number) for item in phase] for phase in bundle.phases],
                [[(1, 1), (3, 1)]],
            )
            self.assertEqual(len(bundle.task_plan.stages), 1)
            stage = bundle.task_plan.stages[0]
            self.assertEqual(stage.stage_id, "Phase 1")
            self.assertEqual(list(stage.robot_action_queues), ["robot1"])
            self.assertEqual(
                [action.action_type for action in stage.robot_action_queues["robot1"]],
                ["GoToObject", "OpenObject", "GoToObject", "OpenObject"],
            )
            self.assertEqual(bundle.no_trans, 4)

    def test_allocation_unknown_robot_number_falls_back_to_robot1(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            allocate_file = self.write_file(
                root / "02_allocate" / "02_allocate_output.txt",
                "# Sequence of Operations:\nSubtask 1: Robot 18;\n",
            )
            plan_folder = root / "08_planner" / "outputs"
            self.write_file(
                plan_folder / "subtask_01_problem_validated_plan.txt",
                "(gotoobject robot18 cabinet)\n(openobject robot18 cabinet)\n",
            )

            bundle = build_task_plan_from_pddlrun_paths(
                task="open cabinet",
                robots=[
                    {"name": "robot1"},
                    {"name": "robot2"},
                    {"name": "robot3"},
                    {"name": "robot4"},
                ],
                allocate_file=allocate_file,
                plan_folder=plan_folder,
                plan_files=[],
                object_names=["Cabinet"],
            )

            self.assertEqual(
                [[(item.subtask_id, item.robot_number) for item in phase] for phase in bundle.phases],
                [[(1, 1)]],
            )
            self.assertEqual(len(bundle.task_plan.stages), 1)
            stage = bundle.task_plan.stages[0]
            self.assertEqual(list(stage.robot_action_queues), ["robot1"])
            self.assertEqual(
                [action.action_type for action in stage.robot_action_queues["robot1"]],
                ["GoToObject", "OpenObject"],
            )
            self.assertEqual(bundle.no_trans, 2)

    def test_unassigned_plan_unknown_robot_number_falls_back_to_robot1(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            allocate_file = self.write_file(
                root / "02_allocate" / "02_allocate_output.txt",
                "# Sequence of Operations:\nSubtask 1: Robot 1;\n",
            )
            plan_folder = root / "08_planner" / "outputs"
            self.write_file(
                plan_folder / "subtask_01_problem_validated_plan.txt",
                "(gotoobject robot1 cabinet)\n(openobject robot1 cabinet)\n",
            )
            self.write_file(
                plan_folder / "subtask_02_problem_validated_plan.txt",
                "(gotoobject robot18 drawer)\n(openobject robot18 drawer)\n",
            )

            bundle = build_task_plan_from_pddlrun_paths(
                task="open cabinet and drawer",
                robots=[
                    {"name": "robot1"},
                    {"name": "robot2"},
                    {"name": "robot3"},
                    {"name": "robot4"},
                ],
                allocate_file=allocate_file,
                plan_folder=plan_folder,
                plan_files=[],
                object_names=["Cabinet", "Drawer"],
            )

            self.assertEqual(
                [[(item.subtask_id, item.robot_number) for item in phase] for phase in bundle.phases],
                [[(1, 1)], [(2, 1)]],
            )
            self.assertEqual(list(bundle.task_plan.stages[1].robot_action_queues), ["robot1"])
            self.assertEqual(
                [action.action_type for action in bundle.task_plan.stages[1].robot_action_queues["robot1"]],
                ["GoToObject", "OpenObject"],
            )
            self.assertEqual(bundle.no_trans, 4)

    def test_phases_empty_after_missing_plan_filter_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            allocate_file = self.write_file(
                root / "02_allocate" / "02_allocate_output.txt",
                "# Sequence of Operations:\n"
                "Subtask 1: Robot 1;\n"
                "Subtask 2: Robot 1;\n"
                "Subtask 3: Robot 1;\n",
            )
            plan_folder = root / "08_planner" / "outputs"
            self.write_file(
                plan_folder / "subtask_02_problem_validated_plan.txt",
                "(gotoobject robot1 drawer)\n(openobject robot1 drawer)\n",
            )

            bundle = build_task_plan_from_pddlrun_paths(
                task="open drawer",
                robots=[{"name": "robot1"}],
                allocate_file=allocate_file,
                plan_folder=plan_folder,
                plan_files=[],
                object_names=["Cabinet", "Drawer", "Window"],
            )

            self.assertEqual(
                [[(item.subtask_id, item.robot_number) for item in phase] for phase in bundle.phases],
                [[(2, 1)]],
            )
            self.assertEqual(len(bundle.task_plan.stages), 1)
            self.assertEqual(bundle.task_plan.stages[0].stage_id, "Phase 1")
            self.assertEqual(
                [action.action_type for action in bundle.task_plan.stages[0].robot_action_queues["robot1"]],
                ["GoToObject", "OpenObject"],
            )
            self.assertEqual(bundle.no_trans, 2)

    def test_empty_robot_action_queue_is_dropped_from_stage(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            allocate_file = self.write_file(
                root / "02_allocate" / "02_allocate_output.txt",
                "# Sequence of Operations:\n"
                "Subtask 1: Robot 1;Subtask 2: Robot 2;\n",
            )
            plan_folder = root / "08_planner" / "outputs"
            self.write_file(
                plan_folder / "subtask_01_problem_validated_plan.txt",
                "; cost = 0 (unit cost)\n",
            )
            self.write_file(
                plan_folder / "subtask_02_problem_validated_plan.txt",
                "(gotoobject robot2 drawer)\n(openobject robot2 drawer)\n",
            )

            bundle = build_task_plan_from_pddlrun_paths(
                task="open drawer",
                robots=[{"name": "robot1"}, {"name": "robot2"}],
                allocate_file=allocate_file,
                plan_folder=plan_folder,
                plan_files=[],
                object_names=["Drawer"],
            )

            self.assertEqual(
                [[(item.subtask_id, item.robot_number) for item in phase] for phase in bundle.phases],
                [[(2, 2)]],
            )
            self.assertEqual(len(bundle.task_plan.stages), 1)
            stage = bundle.task_plan.stages[0]
            self.assertEqual(stage.stage_id, "Phase 1")
            self.assertEqual(list(stage.robot_action_queues), ["robot2"])
            self.assertEqual(
                [action.action_type for action in stage.robot_action_queues["robot2"]],
                ["GoToObject", "OpenObject"],
            )
            self.assertEqual(bundle.no_trans, 2)

    def test_empty_stage_is_dropped_after_empty_subtask_pruning(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            allocate_file = self.write_file(
                root / "02_allocate" / "02_allocate_output.txt",
                "# Sequence of Operations:\n"
                "Subtask 1: Robot 1;\n"
                "Subtask 2: Robot 1;\n",
            )
            plan_folder = root / "08_planner" / "outputs"
            self.write_file(
                plan_folder / "subtask_01_problem_validated_plan.txt",
                "\n; empty plan\n",
            )
            self.write_file(
                plan_folder / "subtask_02_problem_validated_plan.txt",
                "(gotoobject robot1 drawer)\n(openobject robot1 drawer)\n",
            )

            bundle = build_task_plan_from_pddlrun_paths(
                task="open drawer",
                robots=[{"name": "robot1"}],
                allocate_file=allocate_file,
                plan_folder=plan_folder,
                plan_files=[],
                object_names=["Drawer"],
            )

            self.assertEqual(
                [[(item.subtask_id, item.robot_number) for item in phase] for phase in bundle.phases],
                [[(2, 1)]],
            )
            self.assertEqual(len(bundle.task_plan.stages), 1)
            self.assertEqual(bundle.task_plan.stages[0].stage_id, "Phase 1")
            self.assertEqual(
                [action.action_type for action in bundle.task_plan.stages[0].robot_action_queues["robot1"]],
                ["GoToObject", "OpenObject"],
            )
            self.assertEqual(bundle.no_trans, 2)

    def test_all_empty_planner_outputs_report_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            allocate_file = self.write_file(
                root / "02_allocate" / "02_allocate_output.txt",
                "# Sequence of Operations:\n"
                "Subtask 1: Robot 1;\n"
                "Subtask 2: Robot 2;\n",
            )
            plan_folder = root / "08_planner" / "outputs"
            self.write_file(
                plan_folder / "subtask_01_problem_validated_plan.txt",
                "; cost = 0 (unit cost)\n",
            )
            self.write_file(
                plan_folder / "subtask_02_problem_validated_plan.txt",
                "\n",
            )

            with self.assertRaisesRegex(
                PddlRunAdapterError,
                "No executable actions found in planner outputs",
            ):
                build_task_plan_from_pddlrun_paths(
                    task="nothing to do",
                    robots=[{"name": "robot1"}, {"name": "robot2"}],
                    allocate_file=allocate_file,
                    plan_folder=plan_folder,
                    plan_files=[],
                    object_names=["Drawer"],
                )

    def test_allocation_with_no_planned_subtasks_reports_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            allocate_file = self.write_file(
                root / "02_allocate" / "02_allocate_output.txt",
                "# Sequence of Operations:\nSubtask 1: Robot 1;\n",
            )
            plan_folder = root / "08_planner" / "outputs"
            self.write_file(
                plan_folder / "subtask_02_problem_validated_plan.txt",
                "(gotoobject robot1 drawer)\n(openobject robot1 drawer)\n",
            )

            with self.assertRaisesRegex(
                PddlRunAdapterError,
                "no subtask\\(s\\) with planner output",
            ):
                build_task_plan_from_pddlrun_paths(
                    task="open drawer",
                    robots=[{"name": "robot1"}],
                    allocate_file=allocate_file,
                    plan_folder=plan_folder,
                    plan_files=[],
                    object_names=["Drawer"],
                )

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
            self.assertEqual(actions[0].args(), ("Egg", "Pan"))
            self.assertEqual(actions[1].args(), ("Potato",))
            self.assertEqual(actions[2].args(), ("Bowl",))
            self.assertEqual(actions[3].args(), ("Microwave", "Potato"))
            self.assertEqual(actions[4].args(), ("Toaster", "Bread"))
            self.assertEqual(actions[5].args(), ("StoveBurner", "Pan", "Egg"))
            self.assertEqual(actions[6].args(), ("StoveBurner", "Pan"))
            self.assertEqual(actions[7].args(), ("Sink", "Mug"))
            self.assertEqual(actions[8].args(), ("Fridge", "Potato"))

    def test_prepareegg_and_breakegg_pddl_actions_keep_distinct_shapes(self):
        actions = parse_plan_actions(
            "(prepareegg robot1 egg pan)\n"
            "(breakegg robot1 egg)\n"
        )

        self.assertEqual([action.name for action in actions], ["PrepareEgg", "BreakEgg"])
        encoded = [
            encode_plan_action(action, ObjectNameResolver(["Egg", "Pan"]))
            for action in actions
        ]
        self.assertEqual([item.action.action_type for item in encoded], ["PrepareEgg", "BreakEgg"])
        self.assertEqual([item.action.args() for item in encoded], [("Egg", "Pan"), ("Egg",)])
        self.assertEqual([item.object_tokens for item in encoded], [("egg", "pan"), ("egg",)])

    def test_break_egg_executor_action_validator_requires_single_egg_target(self):
        valid_plan = MultiStageActionPlan(
            "break-egg",
            [StagePlan("Stage 1", {"robot1": [Action("BreakEgg", {"args": ("Egg",)})]})],
        )
        PlanValidator().validate(valid_plan)

        invalid_plan = MultiStageActionPlan(
            "break-egg",
            [
                StagePlan(
                    "Stage 1",
                    {"robot1": [Action("BreakEgg", {"args": ("Egg", "Pan")})]},
                )
            ],
        )
        with self.assertRaisesRegex(RuntimeError, "BreakEgg.*requires Egg"):
            PlanValidator().validate(invalid_plan)

    def test_prepare_egg_executor_action_validator_requires_two_args_with_egg_first(self):
        valid_plan = MultiStageActionPlan(
            "prepare-egg",
            [StagePlan("Stage 1", {"robot1": [Action("PrepareEgg", {"args": ("Egg", "Pan")})]})],
        )
        PlanValidator().validate(valid_plan)

        one_arg_plan = MultiStageActionPlan(
            "prepare-egg",
            [StagePlan("Stage 1", {"robot1": [Action("PrepareEgg", {"args": ("Egg",)})]})],
        )
        with self.assertRaisesRegex(RuntimeError, "PrepareEgg.*exactly two"):
            PlanValidator().validate(one_arg_plan)

        non_egg_plan = MultiStageActionPlan(
            "prepare-egg",
            [StagePlan("Stage 1", {"robot1": [Action("PrepareEgg", {"args": ("Potato", "Pan")})]})],
        )
        with self.assertRaisesRegex(RuntimeError, "PrepareEgg.*Egg"):
            PlanValidator().validate(non_egg_plan)

    def test_prepare_egg_executor_breaks_first_egg_arg_and_keeps_two_resources(self):
        class FakeRuntime:
            def __init__(self):
                self.calls = []

            def object_action(self, action_type, robot_id, obj_name):
                self.calls.append((action_type, robot_id, obj_name))
                return "event"

        action = Action("PrepareEgg", {"args": ("Egg", "Pan")})
        self.assertEqual(ResourceInferencer().object_names(action), ("Egg", "Pan"))

        runtime = FakeRuntime()
        result = AI2ThorAdapter(runtime).execute("robot1", action)

        self.assertEqual(result, "event")
        self.assertEqual(runtime.calls, [("BreakObject", "robot1", "Egg")])

        with self.assertRaisesRegex(RuntimeError, "PrepareEgg requires Egg and container"):
            AI2ThorAdapter(runtime).execute("robot1", Action("PrepareEgg", {"args": ("Egg",)}))

        with self.assertRaisesRegex(RuntimeError, "BreakEgg can only target Egg"):
            AI2ThorAdapter(runtime).execute("robot1", Action("PrepareEgg", {"args": ("Potato", "Pan")}))

    def test_wait_one_tick_pddl_action_has_no_object_args(self):
        actions = parse_plan_actions("(waitonetick robot1)\n")

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].name, "WaitOneTick")

        encoded = encode_plan_action(actions[0], ObjectNameResolver(["Apple"]))
        self.assertEqual(encoded.action.action_type, "WaitOneTick")
        self.assertEqual(encoded.action.args(), ())
        self.assertEqual(encoded.object_tokens, ())

    def test_numbered_object_id_bindings_are_preserved_as_runtime_aliases(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            allocate_file = self.write_file(
                root / "02_allocate" / "02_allocate_output.txt",
                "# Sequence of Operations:\nSubtask 1: Robot 1;\n",
            )
            plan_folder = root / "08_planner" / "outputs"
            self.write_file(
                plan_folder / "subtask_01_problem_validated_plan.txt",
                "(gotoobject robot1 Drawer_2)\n(openobject robot1 Drawer_2)\n",
            )
            bindings_path = root / "05_problem_generation" / "key_object_id_bindings.json"
            bindings_path.parent.mkdir(parents=True, exist_ok=True)
            bindings_path.write_text(
                json.dumps(
                    [
                        {
                            "object": "Drawer_1",
                            "object_type": "Drawer",
                            "object_id": "Drawer|+01.00|+00.20|-00.30",
                            "number": 1,
                            "count": 2,
                            "multiple": True,
                            "roles": ["key_object"],
                        },
                        {
                            "object": "Drawer_2",
                            "object_type": "Drawer",
                            "object_id": "Drawer|+01.00|+00.60|-00.30",
                            "number": 2,
                            "count": 2,
                            "multiple": True,
                            "roles": ["key_object"],
                        },
                    ]
                ),
                encoding="utf-8",
            )

            bundle = build_task_plan_from_pddlrun_paths(
                task="open the second drawer",
                robots=[{"name": "robot1"}],
                allocate_file=allocate_file,
                plan_folder=plan_folder,
                plan_files=[],
                object_names=["Drawer"],
            )

            actions = bundle.task_plan.stages[0].robot_action_queues["robot1"]
            self.assertEqual(actions[0].args(), ("Drawer_2",))
            self.assertEqual(actions[1].args(), ("Drawer_2",))
            self.assertEqual(
                bundle.object_mappings["Drawer_2"],
                "Drawer|+01.00|+00.60|-00.30",
            )
            self.assertEqual(
                [binding["object"] for binding in bundle.object_id_bindings],
                ["Drawer_1", "Drawer_2"],
            )

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
