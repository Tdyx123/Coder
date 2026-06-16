import re
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from executor_system.action_plan import Action, FAILURE_FAIL_STAGE, FAILURE_RETRY
from executor_system import actions as executor_actions
from executor_system import context as runtime_context
from executor_system import runtime as runtime_module
from executor_system.demo_state import ground_truth_lock, verified_ground_truth_goal_signatures
from executor_system.executor import Executor
from executor_system.goals import goal_state_verified
from executor_system.runtime import ThorRuntime
from executor_system.utils import position_to_grid_key


class FakeEvent:
    def __init__(self, success=True, error_message="", metadata=None):
        if metadata is not None:
            self.metadata = dict(metadata)
            return
        self.metadata = {"lastActionSuccess": success}
        if error_message:
            self.metadata["errorMessage"] = error_message


def runtime_without_init():
    runtime = object.__new__(ThorRuntime)
    runtime.log_retry = lambda *_args, **_kwargs: None
    return runtime


def object_action_runtime(objects, *, visible_when=None, horizon=35.0, yaw=0.0):
    runtime = runtime_without_init()
    runtime.robot_agent_map = {"robot1": 0}
    runtime.physical_agent_count = 1
    runtime.stats_lock = threading.Lock()
    runtime.total_exec = 0
    runtime.success_exec = 0
    runtime.operated_object_names = set()
    runtime.operated_object_names_lock = threading.Lock()
    runtime.agent_held_object_overrides = {}
    runtime.agent_held_object_overrides_lock = threading.Lock()
    calls = []
    state = {
        "objects": [dict(obj) for obj in objects],
        "horizon": float(horizon),
        "yaw": float(yaw),
        "position": {"x": 0.0, "y": 0.9, "z": 0.0},
    }

    def refresh_visibility():
        if visible_when is None:
            return
        for obj in state["objects"]:
            obj["visible"] = bool(visible_when(state, obj))

    def metadata(success=True, error_message=""):
        result = {
            "lastActionSuccess": success,
            "agentId": 0,
            "agent": {
                "position": dict(state["position"]),
                "rotation": {"y": state["yaw"]},
                "cameraHorizon": state["horizon"],
            },
            "objects": [dict(obj) for obj in state["objects"]],
        }
        if error_message:
            result["errorMessage"] = error_message
        return result

    def agent_event(_agent_id):
        return FakeEvent(metadata=metadata())

    def current_objects(_agent_id=None):
        return [dict(obj) for obj in state["objects"]]

    def step(payload, **_kwargs):
        calls.append(dict(payload))
        action = payload.get("action")
        degrees = float(payload.get("degrees", 0.0) or 0.0)
        if action == "LookUp":
            state["horizon"] -= degrees
        elif action == "LookDown":
            state["horizon"] += degrees
        elif action == "RotateRight":
            state["yaw"] = (state["yaw"] + degrees) % 360.0
        elif action == "RotateLeft":
            state["yaw"] = (state["yaw"] - degrees) % 360.0
        elif action == "OpenObject":
            object_id = str(payload.get("objectId") or "")
            for obj in state["objects"]:
                if str(obj.get("objectId") or "") == object_id:
                    obj["openness"] = 1.0
                    break
        refresh_visibility()
        return FakeEvent(metadata=metadata())

    refresh_visibility()
    runtime.agent_event = agent_event
    runtime.current_objects = current_objects
    runtime.step = step
    return runtime, calls


def put_object_payload_runtime():
    held_id = "Apple|+00.00|+00.90|+00.00"
    receptacle_id = "Table|+01.00|+00.80|+00.00"
    receptacle = {
        "objectId": receptacle_id,
        "objectType": "Table",
        "name": "Table",
        "visible": True,
        "distance": 1.0,
    }
    runtime, calls = object_action_runtime(
        [
            {
                "objectId": held_id,
                "objectType": "Apple",
                "name": "Apple",
                "visible": True,
                "distance": 0.2,
            },
            receptacle,
        ]
    )
    held_objects = {held_id}
    runtime.agent_held_objects_for = lambda _agent_id: set(held_objects)

    def release_agent_held_objects(_agent_id):
        held_objects.clear()

    runtime.release_agent_held_objects = release_agent_held_objects
    return runtime, calls, held_id, receptacle


class FillWaterRuntime:
    def __init__(self, *, put_error):
        self.robot_agent_map = {"robot1": 0}
        self.physical_agent_count = 1
        self.stats_lock = threading.Lock()
        self.total_exec = 0
        self.success_exec = 0
        self.put_error = put_error
        self.calls = []
        self.mug = {
            "objectId": "Mug|+00.00|+00.90|+00.00",
            "objectType": "Mug",
            "name": "Mug",
            "fillLiquid": "",
        }
        self.sink_basin = {
            "objectId": "Sink|-01.90|+00.97|-01.50|SinkBasin",
            "objectType": "SinkBasin",
            "name": "SinkBasin",
        }
        self.faucet = {
            "objectId": "Faucet|-01.90|+01.20|-01.50",
            "objectType": "Faucet",
            "name": "Faucet",
        }
        self.held_objects = {0: {self.mug["objectId"]}}

    def physical_agent_id(self, robot_id):
        return self.robot_agent_map[str(robot_id)]

    def find_object(self, pattern, agent_id=None, require_center=False):
        if str(pattern) in {"Mug", self.mug["objectId"]}:
            return dict(self.mug)
        if str(pattern) in {"Sink", "SinkBasin", self.sink_basin["objectId"]}:
            return dict(self.sink_basin)
        if str(pattern) in {"Faucet", self.faucet["objectId"]}:
            return dict(self.faucet)
        raise RuntimeError(f"Could not find object {pattern!r}.")

    def find_objects(self, pattern, agent_id=None):
        if str(pattern) in {"Sink", "SinkBasin", self.sink_basin["objectId"]}:
            return [dict(self.sink_basin)]
        if str(pattern) in {"Faucet", self.faucet["objectId"]}:
            return [dict(self.faucet)]
        if str(pattern) in {"Mug", self.mug["objectId"]}:
            return [dict(self.mug)]
        return []

    def agent_held_object_matching(self, agent_id, obj_name):
        held = self.held_objects.get(agent_id, set())
        if str(obj_name) == "Mug" and self.mug["objectId"] in held:
            return self.mug["objectId"]
        if str(obj_name) in held:
            return str(obj_name)
        return None

    def agent_held_objects_for(self, agent_id):
        return set(self.held_objects.get(agent_id, set()))

    def object_action(
        self,
        action,
        robot,
        obj_name,
        *,
        extra_object_resources=(),
        action_parameters=None,
    ):
        self.calls.append((action, obj_name))
        with self.stats_lock:
            self.total_exec += 1

        if action == "PutObject" and self.put_error:
            raise RuntimeError(
                "PutObject failed for agent 0 on "
                f"{self.sink_basin['objectId']}: {self.put_error}"
            )

        if action == "PutObject":
            self.held_objects[0] = set()
        elif action == "PickupObject":
            self.held_objects.setdefault(0, set()).add(self.mug["objectId"])
        elif action == "FillObjectWithLiquid":
            self.mug["fillLiquid"] = (action_parameters or {}).get("fillLiquid", "water")
        elif action == "EmptyLiquidFromObject":
            self.mug["fillLiquid"] = ""

        with self.stats_lock:
            self.success_exec += 1
        return FakeEvent()

    def toggle_objects(self, action, robot, obj_name):
        self.calls.append((action, obj_name))
        with self.stats_lock:
            self.total_exec += 1
            self.success_exec += 1
        return FakeEvent()


def run_fillwater_with_runtime(runtime):
    previous_runtime = runtime_context.runtime
    runtime_context.runtime = runtime
    try:
        return executor_actions.FillWater("robot1", "Sink", "Mug")
    finally:
        runtime_context.runtime = previous_runtime


class ExecutorRetryPolicyTest(unittest.TestCase):
    def setUp(self):
        with ground_truth_lock:
            verified_ground_truth_goal_signatures.clear()

    def test_actions_default_to_fail_stage(self):
        self.assertEqual(Action("MoveAhead").on_failure, FAILURE_FAIL_STAGE)
        self.assertEqual(
            Action.from_any({"action_type": "PickupObject"}).on_failure,
            FAILURE_FAIL_STAGE,
        )

    def test_non_teleport_step_does_not_retry_even_when_requested(self):
        runtime = runtime_without_init()
        calls = []

        def step_direct(payload, *, check_success=True, save_frame=True):
            calls.append(dict(payload))
            return FakeEvent(False, "MoveAhead failed")

        runtime._step_direct = step_direct

        event = runtime.step(
            {"action": "MoveAhead", "agentId": 0},
            check_success=False,
            retry_on_failure=True,
            max_retries=3,
        )

        self.assertFalse(event.metadata["lastActionSuccess"])
        self.assertEqual(len(calls), 1)

    def test_putobject_defaults_force_action_true(self):
        runtime, calls, held_id, _receptacle = put_object_payload_runtime()

        runtime.object_action(
            "PutObject",
            "robot1",
            "Table",
            extra_object_resources=(held_id,),
        )

        self.assertIs(calls[0]["forceAction"], True)

    def test_putobject_global_switch_can_disable_force_action(self):
        runtime, calls, held_id, _receptacle = put_object_payload_runtime()

        with patch.object(runtime_module, "PUT_OBJECT_FORCE_ACTION", False):
            runtime.object_action(
                "PutObject",
                "robot1",
                "Table",
                extra_object_resources=(held_id,),
            )

        self.assertIs(calls[0]["forceAction"], False)

    def test_closeobject_defaults_force_action_true(self):
        runtime, calls = object_action_runtime(
            [
                {
                    "objectId": "Cabinet|+00.00|+01.00|+00.00",
                    "objectType": "Cabinet",
                    "name": "Cabinet",
                    "visible": True,
                    "distance": 1.0,
                }
            ],
        )

        runtime.object_action("CloseObject", "robot1", "Cabinet")

        self.assertIs(calls[0]["forceAction"], True)

    def test_closeobject_global_switch_can_disable_force_action(self):
        runtime, calls = object_action_runtime(
            [
                {
                    "objectId": "Cabinet|+00.00|+01.00|+00.00",
                    "objectType": "Cabinet",
                    "name": "Cabinet",
                    "visible": True,
                    "distance": 1.0,
                }
            ],
        )

        with patch.object(runtime_module, "CLOSE_OBJECT_FORCE_ACTION", False):
            runtime.object_action("CloseObject", "robot1", "Cabinet")

        self.assertIs(calls[0]["forceAction"], False)

    def test_put_held_object_in_receptacle_defaults_force_action_true(self):
        runtime, calls, _held_id, receptacle = put_object_payload_runtime()

        runtime.put_held_object_in_receptacle(0, receptacle)

        self.assertIs(calls[0]["forceAction"], True)

    def test_look_actions_round_degrees_to_tenth(self):
        runtime = runtime_without_init()
        calls = []

        def step_direct(payload, *, check_success=True, save_frame=True):
            calls.append(dict(payload))
            return FakeEvent(True)

        runtime._step_direct = step_direct

        cases = [
            ({"action": "LookDown", "degrees": 12.34, "agentId": 0}, 12.3),
            ({"action": "LookUp", "degrees": 12.36, "agentId": 0}, 12.4),
            ({"action": "LookDown", "degrees": 0.04, "agentId": 0}, 0.1),
        ]
        for payload, expected_degrees in cases:
            with self.subTest(payload=payload):
                calls.clear()
                runtime.step(payload, check_success=False)

                self.assertEqual(calls[0]["degrees"], expected_degrees)

    def test_non_look_action_degrees_are_not_normalized(self):
        runtime = runtime_without_init()
        calls = []

        def step_direct(payload, *, check_success=True, save_frame=True):
            calls.append(dict(payload))
            return FakeEvent(True)

        runtime._step_direct = step_direct

        runtime.step(
            {"action": "RotateRight", "degrees": 12.34, "agentId": 0},
            check_success=False,
        )

        self.assertEqual(calls[0]["degrees"], 12.34)

    def test_teleport_step_retries_until_success(self):
        runtime = runtime_without_init()
        calls = []

        def step_direct(payload, *, check_success=True, save_frame=True):
            calls.append(dict(payload))
            if len(calls) == 1:
                return FakeEvent(False, "Teleport failed")
            return FakeEvent(True)

        runtime._step_direct = step_direct

        event = runtime.step(
            {
                "action": "Teleport",
                "agentId": 0,
                "position": {"x": 1.0, "y": 0.0, "z": 1.0},
            },
            check_success=False,
            retry_on_failure=True,
            max_retries=3,
        )

        self.assertTrue(event.metadata["lastActionSuccess"])
        self.assertEqual(len(calls), 2)

    def test_non_teleport_action_retry_policy_fails_without_rerunning(self):
        class FailingRuntime:
            physical_agent_count = 1

            def __init__(self):
                self.step_calls = 0

            def physical_agent_id(self, _robot_id):
                return 0

            def current_agent_position(self, _agent_id):
                return {"x": 0.0, "y": 0.0, "z": 0.0}

            def agent_event(self, _agent_id):
                event = FakeEvent(True)
                event.metadata["agent"] = {"rotation": {"y": 0.0}}
                return event

            def agent_held_objects_for(self, _agent_id):
                return set()

            def step(self, _payload, **_kwargs):
                self.step_calls += 1
                raise RuntimeError("MoveAhead failed")

        runtime = FailingRuntime()
        action = Action("MoveAhead", on_failure=FAILURE_RETRY, max_retries=2)
        executor = Executor(runtime, "robot1", [action])

        with self.assertRaisesRegex(RuntimeError, "MoveAhead failed"):
            executor.execute()

        self.assertEqual(runtime.step_calls, 1)

    def test_teleport_and_face_retries_next_candidate_after_rotation_failure(self):
        runtime = runtime_without_init()
        candidates = [
            {"x": 0.0, "y": 0.0, "z": 0.0},
            {"x": 1.0, "y": 0.0, "z": 0.0},
        ]
        teleport_calls = []
        face_calls = []

        def select_teleport_position(
            _agent_id,
            _search_center,
            *,
            candidate_positions,
            excluded_grid_keys,
            restrict_to_candidate_positions=False,
        ):
            for position in candidate_positions:
                if position_to_grid_key(position) not in excluded_grid_keys:
                    return dict(position)
            raise RuntimeError("No reachable teleport candidate")

        def try_teleport_to_position_direct(_agent_id, target_position, *, max_retries=3):
            teleport_calls.append(dict(target_position))
            return FakeEvent(True)

        def face_position_direct(_agent_id, face_target):
            face_calls.append(dict(face_target))
            if len(face_calls) == 1:
                raise RuntimeError("Rotate failed")

        runtime.select_teleport_position = select_teleport_position
        runtime.try_teleport_to_position_direct = try_teleport_to_position_direct
        runtime.face_position_direct = face_position_direct

        with patch("executor_system.runtime.log", lambda _message: None):
            result = runtime.teleport_and_face_first_working_candidate(
                0,
                candidates,
                face_target={"x": 3.0, "y": 0.0, "z": 3.0},
                search_center=candidates[0],
            )

        self.assertEqual(result, candidates[1])
        self.assertEqual(teleport_calls, candidates)
        self.assertEqual(len(face_calls), 2)

    def test_open_object_skips_physical_action_for_blinds(self):
        object_id = "Blinds|-00.40|+02.16|-01.92"
        runtime, calls = object_action_runtime(
            [
                {
                    "objectId": object_id,
                    "objectType": "Blinds",
                    "name": "Blinds",
                    "visible": False,
                    "axisAlignedBoundingBox": {
                        "center": {"x": 0.0, "y": 2.16, "z": 1.0},
                    },
                }
            ],
            visible_when=lambda state, _obj: state["horizon"] <= -20.0,
        )

        runtime.object_action("OpenObject", "robot1", "Blinds")

        self.assertEqual(calls, [])
        self.assertEqual(runtime.total_exec, 1)
        self.assertEqual(runtime.success_exec, 1)
        self.assertIn("blinds", runtime.operated_object_names)
        self.assertTrue(goal_state_verified("Blinds", "OPENED"))

    def test_open_object_visible_target_does_not_scan_view(self):
        object_id = "Drawer|+00.00|+00.80|+01.00"
        runtime, calls = object_action_runtime(
            [
                {
                    "objectId": object_id,
                    "objectType": "Drawer",
                    "name": "Drawer",
                    "visible": True,
                    "axisAlignedBoundingBox": {
                        "center": {"x": 0.0, "y": 0.8, "z": 1.0},
                    },
                }
            ],
        )

        logs = []
        with patch("executor_system.runtime.log", logs.append):
            runtime.object_action("OpenObject", "robot1", "Drawer")

        self.assertEqual([call["action"] for call in calls], ["OpenObject"])
        self.assertEqual(calls[0]["objectId"], object_id)
        self.assertIn(
            f"OpenObject openness: {object_id} openness=1.0",
            logs,
        )

    def test_open_object_scan_failure_does_not_call_openobject(self):
        object_id = "Cabinet|-00.40|+02.16|-01.92"
        runtime, calls = object_action_runtime(
            [
                {
                    "objectId": object_id,
                    "objectType": "Cabinet",
                    "name": "Cabinet",
                    "visible": False,
                    "axisAlignedBoundingBox": {
                        "center": {"x": 0.0, "y": 2.16, "z": 1.0},
                    },
                }
            ],
        )

        with self.assertRaisesRegex(
            RuntimeError,
            f"OpenObject target {re.escape(object_id)} is not visible",
        ):
            runtime.object_action("OpenObject", "robot1", "Cabinet")

        self.assertNotIn("OpenObject", [call["action"] for call in calls])

    def test_non_open_object_does_not_run_visibility_scan(self):
        object_id = "Blinds|-00.40|+02.16|-01.92"
        runtime, calls = object_action_runtime(
            [
                {
                    "objectId": object_id,
                    "objectType": "Blinds",
                    "name": "Blinds",
                    "visible": False,
                    "axisAlignedBoundingBox": {
                        "center": {"x": 0.0, "y": 2.16, "z": 1.0},
                    },
                }
            ],
        )

        runtime.object_action("CloseObject", "robot1", "Blinds")

        self.assertEqual([call["action"] for call in calls], ["CloseObject"])

    def test_fillwater_skips_sinkbasin_putobject_no_valid_positions(self):
        runtime = FillWaterRuntime(
            put_error="SinkBasin: No valid positions to place object found"
        )
        logs = []

        with patch("executor_system.actions.log", logs.append):
            run_fillwater_with_runtime(runtime)

        self.assertEqual(
            [call[0] for call in runtime.calls],
            [
                "PutObject",
                "ToggleObjectOn",
                "FillObjectWithLiquid",
                "ToggleObjectOff",
                "PickupObject",
            ],
        )
        self.assertEqual(runtime.mug["fillLiquid"], "water")
        self.assertTrue(any("Skipping FillWater SinkBasin PutObject" in log for log in logs))

    def test_fillwater_skipped_sinkbasin_putobject_does_not_penalize_exec_rate(self):
        runtime = FillWaterRuntime(
            put_error="SinkBasin: No valid positions to place object found"
        )

        with patch("executor_system.actions.log", lambda _message: None):
            run_fillwater_with_runtime(runtime)

        self.assertEqual(runtime.total_exec, 4)
        self.assertEqual(runtime.success_exec, 4)

    def test_fillwater_reraises_other_putobject_failures(self):
        runtime = FillWaterRuntime(put_error="SinkBasin: object is not visible")

        with self.assertRaisesRegex(RuntimeError, "object is not visible"):
            run_fillwater_with_runtime(runtime)

        self.assertEqual(runtime.total_exec, 1)
        self.assertEqual(runtime.success_exec, 0)


if __name__ == "__main__":
    unittest.main()
