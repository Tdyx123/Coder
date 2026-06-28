import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from executor_system.action_plan import TaskPlan
from executor_system.parallel_runner import run_action_plan_tolerant
from executor_system.task_plan import TaskPlanParser


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


class PreTaskActionPlanTest(unittest.TestCase):
    def test_task_plan_parser_records_wait_one_tick_helper(self):
        def wait_once(robot):
            WaitOneTick(robot)

        plan = TaskPlanParser("task").parse(
            [("Phase 1", [({"name": "robot1"}, [wait_once])])]
        )

        actions = plan.stages[0].robot_action_queues["robot1"]
        self.assertEqual([action.action_type for action in actions], ["WaitOneTick"])
        self.assertEqual(actions[0].args(), ())

    def test_from_dict_inserts_pre_task_stage_before_real_stages(self):
        plan = TaskPlan.from_dict(
            {
                "task_id": "task",
                "pre_task_stage_id": "Robot1Setup",
                "pre_task_action_queues": {
                    "robot1": [
                        {
                            "action_type": "Wait",
                            "robot_id": "robot2",
                            "parameters": {},
                        },
                        {
                            "action_type": "GoToObject",
                            "parameters": {"args": ["CounterTop"]},
                        },
                    ],
                },
                "stages": [
                    {
                        "stage_id": "Phase 1",
                        "robot_action_queues": {
                            "robot2": [
                                {
                                    "action_type": "OpenObject",
                                    "parameters": {"args": ["Cabinet"]},
                                },
                            ],
                        },
                    },
                ],
            }
        )

        self.assertEqual(
            [stage.stage_id for stage in plan.stages],
            ["Robot1Setup", "Phase 1"],
        )
        pre_task_queue = plan.stages[0].robot_action_queues
        self.assertEqual(list(pre_task_queue), ["robot1"])
        self.assertEqual(
            [action.robot_id for action in pre_task_queue["robot1"]],
            ["robot1", "robot1"],
        )
        self.assertEqual(
            [action.action_type for action in pre_task_queue["robot1"]],
            ["Wait", "GoToObject"],
        )
        self.assertEqual(
            plan.stages[1].robot_action_queues["robot2"][0].robot_id,
            "robot2",
        )

    def test_from_dict_without_pre_task_keeps_existing_stage_order(self):
        plan = TaskPlan.from_dict(
            {
                "task_id": "task",
                "stages": [
                    {
                        "stage_id": "Phase 1",
                        "robot_action_queues": {
                            "robot2": [
                                {"action_type": "Wait", "parameters": {}},
                            ],
                        },
                    },
                ],
            }
        )

        self.assertEqual([stage.stage_id for stage in plan.stages], ["Phase 1"])
        self.assertEqual(list(plan.stages[0].robot_action_queues), ["robot2"])

    def test_pre_task_stage_is_global_barrier_before_real_actions(self):
        calls = []

        def fake_execute(_adapter, robot_id, action, **_kwargs):
            calls.append((robot_id, action.action_type))
            return FakeEvent()

        raw_plan = {
            "task_id": "task",
            "pre_task_action_queues": {
                "robot1": [
                    {"action_type": "Wait", "parameters": {}},
                ],
            },
            "stages": [
                {
                    "stage_id": "Phase 1",
                    "robot_action_queues": {
                        "robot2": [
                            {"action_type": "Wait", "parameters": {}},
                        ],
                    },
                },
            ],
        }

        with patch("executor_system.action_plan.AI2ThorAdapter.execute", fake_execute):
            result = run_action_plan_tolerant(FakeRuntime(), raw_plan, timeout_seconds=5)

        self.assertFalse(result["timed_out"])
        self.assertEqual(result["executed_actions"], 2)
        self.assertEqual(calls, [("robot1", "Wait"), ("robot2", "Wait")])

    def test_pre_task_rejects_non_robot1_queue(self):
        with self.assertRaisesRegex(RuntimeError, "can only target 'robot1'"):
            TaskPlan.from_dict(
                {
                    "task_id": "task",
                    "pre_task_action_queues": {
                        "robot2": [
                            {"action_type": "Wait", "parameters": {}},
                        ],
                    },
                    "stages": [
                        {
                            "stage_id": "Phase 1",
                            "robot_action_queues": {
                                "robot2": [
                                    {"action_type": "Wait", "parameters": {}},
                                ],
                            },
                        },
                    ],
                }
            )


if __name__ == "__main__":
    unittest.main()
