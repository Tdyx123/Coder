import copy
import io
import sys
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from executor_system.evaluation import EvaluationContext
from executor_system import context as runtime_context
from executor_system.actions import _wait_for_object_cold
from executor_system.goals import record_satisfied_temperature_goal_states
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

        self.assertEqual(result["evaluation_version"], "atomic_goals_v3")
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

    def test_independent_states_can_use_different_instances(self):
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
        self.assertEqual(result["gcr"], 1.0)

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
            [{"name": "Bowl", "states": [], "contains": ["Cup_1"]}],
            object_id_bindings=[{"object": "Cup_1", "object_id": "Cup|1"}],
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

        self.assertEqual(result["evaluation_version"], "atomic_goals_v3")
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

    def test_temperature_scan_resolves_bound_alias_to_one_instance(self):
        goals = [{"name": "Mug_1", "states": ["HOT"], "contains": []}]
        context = EvaluationContext.from_goals(goals, object_id_bindings=[{"object": "Mug_1", "object_id": "Mug|1"}])
        runtime = FakeRuntime(
            [
                {"objectId": "Mug|1", "objectType": "Mug", "temperature": "Cold"},
                {"objectId": "Mug|2", "objectType": "Mug", "temperature": "Hot"},
            ],
            aliases={"Mug_1": "Mug|1"},
        )

        recorded = record_satisfied_temperature_goal_states(runtime, context)

        self.assertEqual(recorded, 0)
        self.assertFalse(context.has_observation("Mug_1", "HOT"))
        runtime._objects[0]["temperature"] = "Hot"
        self.assertEqual(record_satisfied_temperature_goal_states(runtime, context), 1)
        self.assertTrue(context.has_observation("Mug_1", "HOT"))

    def test_demo_entry_installs_context_before_transient_observation(self):
        import demo

        goals = [{"name": "Mug", "states": ["HOT"], "contains": []}]
        runtime = self._entry_runtime(
            [{"objectId": "Mug|1", "objectType": "Mug", "temperature": "Cold"}]
        )
        bundle = SimpleNamespace(
            object_mapping_warnings=[],
            object_id_bindings=[],
            task_plan=object(),
            no_trans=0,
        )

        def execute(_plan):
            self.assertIsNotNone(runtime.evaluation_context)
            runtime.evaluation_context.record_observation("Mug", "HOT", "Mug|1")

        with patch.object(
            demo,
            "load_task_record",
            return_value={"robot list": [1], "object_states": goals, "trans": 0},
        ), patch.object(demo, "floor_plan_from_task_file", return_value="1"), patch.object(
            demo, "build_robot_team", return_value=[{"name": "robot1"}]
        ), patch.object(
            demo, "build_task_plan_from_pddlrun_paths", return_value=bundle
        ), patch.object(
            demo, "ThorRuntime", return_value=runtime
        ), patch.object(
            demo, "run_action_plan", side_effect=execute
        ), redirect_stdout(io.StringIO()):
            self.assertEqual(demo.main(), 0)

        self.assertTrue(runtime.evaluation_context.has_observation("Mug", "HOT"))

    def test_demo_entry_preserves_null_metrics_for_invalid_evaluation(self):
        import demo

        goals = [{"name": "LightSwitch", "states": ["OFF"], "contains": []}]
        runtime = self._entry_runtime(
            [{"objectId": "LightSwitch|1", "objectType": "LightSwitch"}]
        )
        bundle = SimpleNamespace(
            object_mapping_warnings=[],
            object_id_bindings=[],
            task_plan=object(),
            no_trans=0,
        )
        output = io.StringIO()
        with patch.object(
            demo,
            "load_task_record",
            return_value={"robot list": [1], "object_states": goals, "trans": 0},
        ), patch.object(demo, "floor_plan_from_task_file", return_value="1"), patch.object(
            demo, "build_robot_team", return_value=[{"name": "robot1"}]
        ), patch.object(
            demo, "build_task_plan_from_pddlrun_paths", return_value=bundle
        ), patch.object(
            demo, "ThorRuntime", return_value=runtime
        ), patch.object(
            demo, "run_action_plan"
        ), redirect_stdout(output):
            self.assertEqual(demo.main(), 0)

        self.assertIn("SR:None, TC:None", output.getvalue())

    @staticmethod
    def _entry_runtime(objects):
        runtime = FakeRuntime(objects)
        runtime.evaluation_context = None
        runtime.register_object_id_bindings = lambda _bindings: None
        runtime.step = lambda _payload, **_kwargs: None
        runtime.evaluate = lambda goals: ThorRuntime.evaluate(runtime, goals)
        runtime.log_unmet_goals = lambda _goals: []
        runtime.generate_video = lambda: None
        runtime.write_final_metadata = lambda: None
        runtime.stop = lambda: None
        return runtime


if __name__ == "__main__":
    unittest.main()
