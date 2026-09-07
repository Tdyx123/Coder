import copy
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from executor_system.evaluation import EvaluationContext
from executor_system import context as runtime_context
from executor_system.actions import _wait_for_object_cold
from executor_system.runtime import ThorRuntime


class FakeRuntime:
    def __init__(self, objects, aliases=None):
        self._objects = [dict(obj) for obj in objects]
        self._aliases = dict(aliases or {})
        self.stats_lock = threading.Lock()
        self.total_exec = 0
        self.success_exec = 0

    def current_objects(self, _agent_id=None):
        return [dict(obj) for obj in self._objects]

    def resolve_object_alias(self, name, agent_id=None):
        del agent_id
        return self._aliases.get(name, name)


class EvaluationContractTest(unittest.TestCase):
    def test_observation_does_not_add_a_goal(self):
        goals = [{"name": "Cabinet", "states": ["OPENED"], "contains": []}]
        original = copy.deepcopy(goals)
        context = EvaluationContext.from_goals(goals)

        context.record_observation("Mug", "HOT", "Mug|1")

        self.assertEqual(goals, original)
        self.assertEqual(len(context.goals), 1)
        self.assertEqual(context.goals[0].name, "Cabinet")

    def test_observations_are_not_shared_between_tasks(self):
        goals = [{"name": "Mug", "states": ["HOT"], "contains": []}]
        first = EvaluationContext.from_goals(goals)
        first.record_observation("Mug", "HOT", "Mug|1")

        second = EvaluationContext.from_goals(goals)

        self.assertFalse(second.has_observation("Mug", "HOT"))

    def test_unrelated_hot_observation_does_not_satisfy_closed_cabinet(self):
        context = EvaluationContext.from_goals(
            [{"name": "Cabinet", "states": ["OPENED"], "contains": []}]
        )
        context.record_observation("Mug", "HOT", "Mug|1")
        runtime = FakeRuntime(
            [{"objectId": "Cabinet|1", "objectType": "Cabinet", "isOpen": False}]
        )

        result = context.evaluate(runtime)

        self.assertEqual(result["evaluation_version"], "fixed_goals_v2")
        self.assertEqual(result["evaluation_status"], "valid")
        self.assertEqual(result["gcr"], 0.0)
        self.assertEqual(result["satisfied_goal_count"], 0)

    def test_hot_observation_remains_satisfied_after_object_cools(self):
        context = EvaluationContext.from_goals(
            [{"name": "Mug", "states": ["HOT"], "contains": []}]
        )
        context.record_observation("Mug", "HOT", "Mug|1")
        runtime = FakeRuntime(
            [{"objectId": "Mug|1", "objectType": "Mug", "temperature": "Cold"}]
        )

        result = context.evaluate(runtime)

        self.assertEqual(result["evaluation_status"], "valid")
        self.assertEqual(result["gcr"], 1.0)

    def test_missing_negative_state_fields_make_evaluation_invalid(self):
        cases = (
            ("LightSwitch", "OFF"),
            ("Cabinet", "CLOSED"),
            ("Mug", "CLEANED"),
        )
        for object_type, state in cases:
            with self.subTest(state=state):
                context = EvaluationContext.from_goals(
                    [{"name": object_type, "states": [state], "contains": []}]
                )
                runtime = FakeRuntime(
                    [{"objectId": f"{object_type}|1", "objectType": object_type}]
                )

                result = context.evaluate(runtime)

                self.assertEqual(result["evaluation_status"], "invalid")
                self.assertIsNone(result["gcr"])
                self.assertIsNone(result["tc"])
                self.assertIsNone(result["sr"])
                self.assertIsNone(result["ru"])
                self.assertIsNone(result["satisfied_goal_count"])
                self.assertEqual(result["goal_results"][0]["status"], "unknown")

    def test_contains_type_match_is_exact(self):
        context = EvaluationContext.from_goals(
            [{"name": "Bowl", "states": [], "contains": ["Cup"]}]
        )
        runtime = FakeRuntime(
            [
                {
                    "objectId": "Bowl|1",
                    "objectType": "Bowl",
                    "receptacleObjectIds": ["EggCup|1"],
                },
                {"objectId": "EggCup|1", "objectType": "EggCup"},
            ]
        )

        result = context.evaluate(runtime)

        self.assertEqual(result["evaluation_status"], "valid")
        self.assertEqual(result["gcr"], 0.0)

    def test_one_instance_must_satisfy_every_state(self):
        context = EvaluationContext.from_goals(
            [{"name": "Mug", "states": ["HOT", "CLEANED"], "contains": []}]
        )
        runtime = FakeRuntime(
            [
                {
                    "objectId": "Mug|1",
                    "objectType": "Mug",
                    "temperature": "Hot",
                    "isDirty": True,
                },
                {
                    "objectId": "Mug|2",
                    "objectType": "Mug",
                    "temperature": "Cold",
                    "isDirty": False,
                },
            ]
        )

        result = context.evaluate(runtime)

        self.assertEqual(result["evaluation_status"], "valid")
        self.assertEqual(result["gcr"], 0.0)

    def test_later_caller_mutation_does_not_change_goals(self):
        goals = [{"name": "Mug", "states": ["HOT"], "contains": []}]
        context = EvaluationContext.from_goals(goals)
        goals[0]["name"] = "Cabinet"
        goals[0]["states"].append("CLEANED")

        result = context.evaluate(
            FakeRuntime(
                [{"objectId": "Mug|1", "objectType": "Mug", "temperature": "Hot"}]
            )
        )

        self.assertEqual(context.goals[0].name, "Mug")
        self.assertEqual(context.goals[0].states, ("HOT",))
        self.assertEqual(result["gcr"], 1.0)

    def test_bound_contains_alias_requires_exact_object_id(self):
        context = EvaluationContext.from_goals(
            [{"name": "Bowl", "states": [], "contains": ["Cup_1"]}]
        )
        runtime = FakeRuntime(
            [
                {
                    "objectId": "Bowl|1",
                    "objectType": "Bowl",
                    "receptacleObjectIds": ["Cup|2"],
                },
                {"objectId": "Cup|1", "objectType": "Cup"},
                {"objectId": "Cup|2", "objectType": "Cup"},
            ],
            aliases={"Cup_1": "Cup|1"},
        )

        result = context.evaluate(runtime)

        self.assertEqual(result["gcr"], 0.0)

    def test_empty_goals_require_explicit_noop_permission(self):
        invalid = EvaluationContext.from_goals([]).evaluate(FakeRuntime([]))
        valid = EvaluationContext.from_goals([], allow_empty=True).evaluate(FakeRuntime([]))

        self.assertEqual(invalid["evaluation_status"], "invalid")
        self.assertIsNone(invalid["gcr"])
        self.assertEqual(valid["evaluation_status"], "valid")
        self.assertEqual(valid["gcr"], 1.0)

    def test_runtime_evaluate_delegates_to_its_fixed_context(self):
        goals = [{"name": "Mug", "states": ["HOT"], "contains": []}]
        runtime = object.__new__(ThorRuntime)
        runtime.evaluation_context = EvaluationContext.from_goals(goals)
        runtime.current_objects = lambda _agent_id=None: [
            {"objectId": "Mug|1", "objectType": "Mug", "temperature": "Hot"}
        ]
        runtime.stats_lock = threading.Lock()
        runtime.total_exec = 0
        runtime.success_exec = 0

        result = ThorRuntime.evaluate(runtime, copy.deepcopy(goals))

        self.assertEqual(result["evaluation_version"], "fixed_goals_v2")
        self.assertEqual(result["gcr"], 1.0)
        with self.assertRaises(ValueError):
            ThorRuntime.evaluate(
                runtime,
                [{"name": "Cabinet", "states": ["OPENED"], "contains": []}],
            )

    def test_cold_wait_fallback_does_not_record_unsatisfied_temperature(self):
        goals = [{"name": "Mug", "states": ["COLD"], "contains": []}]

        class NeverColdRuntime(FakeRuntime):
            def __init__(self):
                super().__init__(
                    [
                        {
                            "objectId": "Mug|1",
                            "objectType": "Mug",
                            "temperature": "RoomTemp",
                        }
                    ]
                )
                self.evaluation_context = EvaluationContext.from_goals(goals)

            def physical_agent_id(self, _robot):
                return 0

            def find_object(self, _name, agent_id=None):
                del agent_id
                return self.current_objects()[0]

            def step(self, _payload, **_kwargs):
                return None

        runtime = NeverColdRuntime()
        runtime_context.runtime = runtime
        try:
            with patch("executor_system.actions.INTERACTION_MAX_PASS_STEPS", 6):
                _wait_for_object_cold("robot1", "Mug", "ColdObject")
        finally:
            runtime_context.runtime = None

        self.assertFalse(runtime.evaluation_context.has_observation("Mug", "COLD"))


if __name__ == "__main__":
    unittest.main()
