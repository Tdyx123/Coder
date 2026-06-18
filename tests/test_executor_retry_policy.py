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

    def teleport_to_position_direct(agent_id, target_position, *, max_retries=3):
        calls.append(
            {
                "action": "Teleport",
                "agentId": agent_id,
                "position": dict(target_position),
            }
        )
        state["position"] = dict(target_position)
        refresh_visibility()

    refresh_visibility()
    runtime._test_state = state
    runtime.agent_event = agent_event
    runtime.current_objects = current_objects
    runtime.metadata_held_objects = lambda _agent_id: set()
    runtime.step = step
    runtime.teleport_to_position_direct = teleport_to_position_direct
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
    def __init__(self, *, put_error, fill_on_faucet_on=False):
        self.robot_agent_map = {"robot1": 0}
        self.physical_agent_count = 1
        self.stats_lock = threading.Lock()
        self.total_exec = 0
        self.success_exec = 0
        self.put_error = put_error
        self.fill_on_faucet_on = fill_on_faucet_on
        self.calls = []
        self.mug = {
            "objectId": "Mug|+00.00|+00.90|+00.00",
            "objectType": "Mug",
            "name": "Mug",
            "isFilledWithLiquid": False,
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
            self.mug["isFilledWithLiquid"] = True
        elif action == "EmptyLiquidFromObject":
            self.mug["fillLiquid"] = ""
            self.mug["isFilledWithLiquid"] = False

        with self.stats_lock:
            self.success_exec += 1
        return FakeEvent()

    def toggle_objects(self, action, robot, obj_name):
        self.calls.append((action, obj_name))
        with self.stats_lock:
            self.total_exec += 1
            self.success_exec += 1
        if action == "ToggleObjectOn" and self.fill_on_faucet_on:
            self.mug["isFilledWithLiquid"] = True
            self.mug["fillLiquid"] = "water"
        return FakeEvent()


def run_fillwater_with_runtime(runtime):
    previous_runtime = runtime_context.runtime
    runtime_context.runtime = runtime
    try:
        return executor_actions.FillWater("robot1", "Sink", "Mug")
    finally:
        runtime_context.runtime = previous_runtime


def run_emptyliquid_with_runtime(runtime):
    previous_runtime = runtime_context.runtime
    runtime_context.runtime = runtime
    try:
        return executor_actions.EmptyLiquid("robot1", "Mug")
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

    def test_dirtyobject_defaults_force_action_true(self):
        runtime, calls = object_action_runtime(
            [
                {
                    "objectId": "Plate|+00.00|+01.00|+00.00",
                    "objectType": "Plate",
                    "name": "Plate",
                    "visible": True,
                    "distance": 1.0,
                    "dirtyable": True,
                    "isDirty": False,
                }
            ],
        )

        runtime.object_action("DirtyObject", "robot1", "Plate")

        self.assertIs(calls[0]["forceAction"], True)

    def test_dirtyobject_global_switch_can_disable_force_action(self):
        runtime, calls = object_action_runtime(
            [
                {
                    "objectId": "Plate|+00.00|+01.00|+00.00",
                    "objectType": "Plate",
                    "name": "Plate",
                    "visible": True,
                    "distance": 1.0,
                    "dirtyable": True,
                    "isDirty": False,
                }
            ],
        )

        with patch.object(runtime_module, "DIRTY_OBJECT_FORCE_ACTION", False):
            runtime.object_action("DirtyObject", "robot1", "Plate")

        self.assertIs(calls[0]["forceAction"], False)

    def test_emptyliquid_defaults_force_action_true(self):
        runtime, calls = object_action_runtime(
            [
                {
                    "objectId": "Mug|+00.00|+01.00|+00.00",
                    "objectType": "Mug",
                    "name": "Mug",
                    "visible": True,
                    "distance": 1.0,
                    "isFilledWithLiquid": True,
                    "fillLiquid": "water",
                }
            ],
        )

        runtime.object_action("EmptyLiquidFromObject", "robot1", "Mug")

        self.assertIs(calls[0]["forceAction"], True)

    def test_emptyliquid_global_switch_can_disable_force_action(self):
        runtime, calls = object_action_runtime(
            [
                {
                    "objectId": "Mug|+00.00|+01.00|+00.00",
                    "objectType": "Mug",
                    "name": "Mug",
                    "visible": True,
                    "distance": 1.0,
                    "isFilledWithLiquid": True,
                    "fillLiquid": "water",
                }
            ],
        )

        with patch.object(runtime_module, "EMPTY_LIQUID_FORCE_ACTION", False):
            runtime.object_action("EmptyLiquidFromObject", "robot1", "Mug")

        self.assertIs(calls[0]["forceAction"], False)

    def test_find_stoveburner_prefers_empty_receptacle_object_ids(self):
        occupied_id = "StoveBurner|+00.00|+00.92|+00.00"
        empty_id = "StoveBurner|+02.00|+00.92|+00.00"
        runtime, _calls = object_action_runtime(
            [
                {
                    "objectId": occupied_id,
                    "objectType": "StoveBurner",
                    "name": "StoveBurner",
                    "visible": True,
                    "distance": 0.1,
                    "receptacleObjectIds": ["Pan|+00.00|+00.95|+00.00"],
                },
                {
                    "objectId": empty_id,
                    "objectType": "StoveBurner",
                    "name": "StoveBurner",
                    "visible": False,
                    "distance": 9.0,
                    "receptacleObjectIds": [],
                },
            ]
        )

        selected = runtime.find_object("StoveBurner", agent_id=0)

        self.assertEqual(selected["objectId"], empty_id)

    def test_find_stoveburner_prefers_empty_parent_receptacles(self):
        occupied_id = "StoveBurner|+00.00|+00.92|+00.00"
        empty_id = "StoveBurner|+02.00|+00.92|+00.00"
        runtime, _calls = object_action_runtime(
            [
                {
                    "objectId": occupied_id,
                    "objectType": "StoveBurner",
                    "name": "StoveBurner",
                    "visible": True,
                    "distance": 0.1,
                    "receptacleObjectIds": [],
                },
                {
                    "objectId": empty_id,
                    "objectType": "StoveBurner",
                    "name": "StoveBurner",
                    "visible": False,
                    "distance": 9.0,
                    "receptacleObjectIds": [],
                },
                {
                    "objectId": "Pan|+00.00|+00.95|+00.00",
                    "objectType": "Pan",
                    "name": "Pan",
                    "parentReceptacles": [occupied_id],
                },
            ]
        )

        selected = runtime.find_object("StoveBurner", agent_id=0)

        self.assertEqual(selected["objectId"], empty_id)

    def test_find_stoveburner_keeps_existing_order_when_all_occupied(self):
        visible_id = "StoveBurner|+00.00|+00.92|+00.00"
        hidden_id = "StoveBurner|+02.00|+00.92|+00.00"
        runtime, _calls = object_action_runtime(
            [
                {
                    "objectId": hidden_id,
                    "objectType": "StoveBurner",
                    "name": "StoveBurner",
                    "visible": False,
                    "distance": 1.0,
                    "receptacleObjectIds": ["Pot|+02.00|+00.95|+00.00"],
                },
                {
                    "objectId": visible_id,
                    "objectType": "StoveBurner",
                    "name": "StoveBurner",
                    "visible": True,
                    "distance": 9.0,
                    "receptacleObjectIds": ["Pan|+00.00|+00.95|+00.00"],
                },
            ]
        )

        selected = runtime.find_object("StoveBurner", agent_id=0)

        self.assertEqual(selected["objectId"], visible_id)

    def test_toggleobjecton_defaults_force_action_true(self):
        runtime, calls = object_action_runtime(
            [
                {
                    "objectId": "DeskLamp|+00.00|+01.00|+00.00",
                    "objectType": "DeskLamp",
                    "name": "DeskLamp",
                    "visible": True,
                    "distance": 1.0,
                    "isToggled": False,
                }
            ],
        )

        runtime.object_action("ToggleObjectOn", "robot1", "DeskLamp")

        self.assertIs(calls[0]["forceAction"], True)

    def test_toggleobjecton_global_switch_can_disable_force_action(self):
        runtime, calls = object_action_runtime(
            [
                {
                    "objectId": "DeskLamp|+00.00|+01.00|+00.00",
                    "objectType": "DeskLamp",
                    "name": "DeskLamp",
                    "visible": True,
                    "distance": 1.0,
                    "isToggled": False,
                }
            ],
        )

        with patch.object(runtime_module, "TOGGLE_OBJECT_ON_FORCE_ACTION", False):
            runtime.object_action("ToggleObjectOn", "robot1", "DeskLamp")

        self.assertIs(calls[0]["forceAction"], False)

    def test_toggleobjectoff_defaults_force_action_true(self):
        runtime, calls = object_action_runtime(
            [
                {
                    "objectId": "DeskLamp|+00.00|+01.00|+00.00",
                    "objectType": "DeskLamp",
                    "name": "DeskLamp",
                    "visible": True,
                    "distance": 1.0,
                    "isToggled": True,
                }
            ],
        )

        runtime.object_action("ToggleObjectOff", "robot1", "DeskLamp")

        self.assertIs(calls[0]["forceAction"], True)

    def test_toggleobjectoff_global_switch_can_disable_force_action(self):
        runtime, calls = object_action_runtime(
            [
                {
                    "objectId": "DeskLamp|+00.00|+01.00|+00.00",
                    "objectType": "DeskLamp",
                    "name": "DeskLamp",
                    "visible": True,
                    "distance": 1.0,
                    "isToggled": True,
                }
            ],
        )

        with patch.object(runtime_module, "TOGGLE_OBJECT_OFF_FORCE_ACTION", False):
            runtime.object_action("ToggleObjectOff", "robot1", "DeskLamp")

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

    def test_pickup_clip_error_teleports_backward_and_retries(self):
        object_id = "Apple|+00.00|+00.90|+01.00"
        runtime, calls = object_action_runtime(
            [
                {
                    "objectId": object_id,
                    "objectType": "Apple",
                    "name": "Apple",
                    "visible": True,
                    "distance": 1.0,
                }
            ],
            yaw=90.0,
        )
        pickup_errors = [runtime_module.PICKUP_OBJECT_CLIP_ERROR, None]

        def step(payload, **_kwargs):
            calls.append(dict(payload))
            if payload.get("action") == "PickupObject":
                error = pickup_errors.pop(0)
                if error:
                    raise RuntimeError(f"InvalidOperationException: {error}")
                metadata = runtime.agent_event(0).metadata
                return FakeEvent(metadata=metadata)
            return FakeEvent(metadata=runtime.agent_event(0).metadata)

        runtime.step = step

        runtime.object_action("PickupObject", "robot1", "Apple")

        self.assertEqual(
            [call["action"] for call in calls],
            ["PickupObject", "Teleport", "PickupObject"],
        )
        self.assertAlmostEqual(calls[1]["position"]["x"], -0.25)
        self.assertAlmostEqual(calls[1]["position"]["y"], 0.9)
        self.assertAlmostEqual(calls[1]["position"]["z"], 0.0)
        self.assertEqual(runtime.total_exec, 1)
        self.assertEqual(runtime.success_exec, 1)

    def test_pickup_clip_retry_continues_after_retry_and_teleport_failures(self):
        object_id = "Apple|+00.00|+00.90|+01.00"
        runtime, calls = object_action_runtime(
            [
                {
                    "objectId": object_id,
                    "objectType": "Apple",
                    "name": "Apple",
                    "visible": True,
                    "distance": 1.0,
                }
            ],
            yaw=0.0,
        )
        pickup_attempts = 0
        teleport_attempts = 0

        def step(payload, **_kwargs):
            nonlocal pickup_attempts
            calls.append(dict(payload))
            if payload.get("action") == "PickupObject":
                pickup_attempts += 1
                metadata = runtime.agent_event(0).metadata
                if pickup_attempts < 3:
                    metadata["lastActionSuccess"] = False
                    metadata["errorMessage"] = runtime_module.PICKUP_OBJECT_CLIP_ERROR
                return FakeEvent(metadata=metadata)
            return FakeEvent(metadata=runtime.agent_event(0).metadata)

        def teleport_to_position_direct(agent_id, target_position, *, max_retries=3):
            nonlocal teleport_attempts
            teleport_attempts += 1
            calls.append(
                {
                    "action": "Teleport",
                    "agentId": agent_id,
                    "position": dict(target_position),
                }
            )
            if teleport_attempts == 2:
                raise RuntimeError("teleport blocked")
            runtime._test_state["position"] = dict(target_position)

        runtime.step = step
        runtime.teleport_to_position_direct = teleport_to_position_direct

        runtime.object_action("PickupObject", "robot1", "Apple")

        self.assertEqual(
            [call["action"] for call in calls],
            [
                "PickupObject",
                "Teleport",
                "PickupObject",
                "Teleport",
                "Teleport",
                "PickupObject",
            ],
        )
        teleport_positions = [
            call["position"]
            for call in calls
            if call["action"] == "Teleport"
        ]
        self.assertAlmostEqual(teleport_positions[0]["z"], -0.25)
        self.assertAlmostEqual(teleport_positions[1]["z"], -0.75)
        self.assertAlmostEqual(teleport_positions[2]["z"], -1.0)
        self.assertEqual(runtime.total_exec, 1)
        self.assertEqual(runtime.success_exec, 1)

    def test_pickup_target_visibility_exception_scans_view_and_retries(self):
        object_id = "Apple|+00.00|+00.90|+01.00"
        runtime, calls = object_action_runtime(
            [
                {
                    "objectId": object_id,
                    "objectType": "Apple",
                    "name": "Apple",
                    "visible": True,
                    "distance": 1.0,
                }
            ],
            horizon=35.0,
        )
        base_step = runtime.step
        pickup_attempts = 0

        def step(payload, **kwargs):
            nonlocal pickup_attempts
            if payload.get("action") != "PickupObject":
                return base_step(payload, **kwargs)
            calls.append(dict(payload))
            pickup_attempts += 1
            if pickup_attempts == 1:
                raise RuntimeError(
                    "NullReferenceException: "
                    f"{runtime_module.PICKUP_OBJECT_TARGET_VISIBILITY_ERROR}"
                )
            return FakeEvent(metadata=runtime.agent_event(0).metadata)

        runtime.step = step

        runtime.object_action("PickupObject", "robot1", "Apple")

        self.assertEqual(
            [call["action"] for call in calls],
            ["PickupObject", "LookUp", "PickupObject"],
        )
        self.assertEqual(calls[1]["degrees"], 10.0)
        self.assertEqual(runtime._test_state["horizon"], 25.0)
        self.assertEqual(runtime.total_exec, 1)
        self.assertEqual(runtime.success_exec, 1)

    def test_pickup_target_visibility_event_scans_all_offsets_and_restores(self):
        object_id = "Apple|+00.00|+00.90|+01.00"
        runtime, calls = object_action_runtime(
            [
                {
                    "objectId": object_id,
                    "objectType": "Apple",
                    "name": "Apple",
                    "visible": True,
                    "distance": 1.0,
                }
            ],
            horizon=35.0,
        )
        base_step = runtime.step
        pickup_horizons = []

        def step(payload, **kwargs):
            if payload.get("action") != "PickupObject":
                return base_step(payload, **kwargs)
            calls.append(dict(payload))
            pickup_horizons.append(runtime._test_state["horizon"])
            metadata = runtime.agent_event(0).metadata
            metadata["lastActionSuccess"] = False
            metadata["errorMessage"] = (
                runtime_module.PICKUP_OBJECT_TARGET_VISIBILITY_ERROR
            )
            return FakeEvent(metadata=metadata)

        runtime.step = step

        with self.assertRaisesRegex(
            RuntimeError,
            runtime_module.PICKUP_OBJECT_TARGET_VISIBILITY_ERROR,
        ):
            runtime.object_action("PickupObject", "robot1", "Apple")

        self.assertEqual(
            pickup_horizons,
            [35.0, 25.0, 15.0, 5.0, 45.0, 55.0, 65.0],
        )
        self.assertEqual(
            [
                call["action"]
                for call in calls
                if call["action"] in {"LookUp", "LookDown"}
            ],
            [
                "LookUp",
                "LookUp",
                "LookUp",
                "LookDown",
                "LookDown",
                "LookDown",
                "LookUp",
            ],
        )
        self.assertEqual(runtime._test_state["horizon"], 35.0)
        self.assertEqual(runtime.total_exec, 1)
        self.assertEqual(runtime.success_exec, 0)

    def test_break_target_visibility_exception_scans_view_and_retries(self):
        object_id = "Window|+00.00|+01.50|+01.00"
        runtime, calls = object_action_runtime(
            [
                {
                    "objectId": object_id,
                    "objectType": "Window",
                    "name": "Window",
                    "visible": True,
                    "distance": 1.0,
                }
            ],
            horizon=35.0,
        )
        base_step = runtime.step
        break_attempts = 0

        def step(payload, **kwargs):
            nonlocal break_attempts
            if payload.get("action") != "BreakObject":
                return base_step(payload, **kwargs)
            calls.append(dict(payload))
            break_attempts += 1
            if break_attempts == 1:
                raise RuntimeError(
                    "NullReferenceException: "
                    f"{runtime_module.PICKUP_OBJECT_TARGET_VISIBILITY_ERROR}"
                )
            return FakeEvent(metadata=runtime.agent_event(0).metadata)

        runtime.step = step

        runtime.object_action("BreakObject", "robot1", "Window")

        self.assertEqual(
            [call["action"] for call in calls],
            ["BreakObject", "LookUp", "BreakObject"],
        )
        self.assertEqual(calls[1]["degrees"], 10.0)
        self.assertEqual(runtime._test_state["horizon"], 25.0)
        self.assertEqual(runtime.total_exec, 1)
        self.assertEqual(runtime.success_exec, 1)

    def test_break_target_visibility_event_scans_all_offsets_and_restores(self):
        object_id = "Window|+00.00|+01.50|+01.00"
        runtime, calls = object_action_runtime(
            [
                {
                    "objectId": object_id,
                    "objectType": "Window",
                    "name": "Window",
                    "visible": True,
                    "distance": 1.0,
                }
            ],
            horizon=35.0,
        )
        base_step = runtime.step
        break_horizons = []

        def step(payload, **kwargs):
            if payload.get("action") != "BreakObject":
                return base_step(payload, **kwargs)
            calls.append(dict(payload))
            break_horizons.append(runtime._test_state["horizon"])
            metadata = runtime.agent_event(0).metadata
            metadata["lastActionSuccess"] = False
            metadata["errorMessage"] = (
                runtime_module.PICKUP_OBJECT_TARGET_VISIBILITY_ERROR
            )
            return FakeEvent(metadata=metadata)

        runtime.step = step

        with self.assertRaisesRegex(
            RuntimeError,
            runtime_module.PICKUP_OBJECT_TARGET_VISIBILITY_ERROR,
        ):
            runtime.object_action("BreakObject", "robot1", "Window")

        self.assertEqual(
            break_horizons,
            [35.0, 25.0, 15.0, 5.0, 45.0, 55.0, 65.0],
        )
        self.assertEqual(
            [
                call["action"]
                for call in calls
                if call["action"] in {"LookUp", "LookDown"}
            ],
            [
                "LookUp",
                "LookUp",
                "LookUp",
                "LookDown",
                "LookDown",
                "LookDown",
                "LookUp",
            ],
        )
        self.assertEqual(runtime._test_state["horizon"], 35.0)
        self.assertEqual(runtime.total_exec, 1)
        self.assertEqual(runtime.success_exec, 0)

    def test_break_non_visibility_error_does_not_scan_or_backoff(self):
        object_id = "Vase|+00.00|+00.90|+01.00"
        runtime, calls = object_action_runtime(
            [
                {
                    "objectId": object_id,
                    "objectType": "Vase",
                    "name": "Vase",
                    "visible": True,
                    "distance": 1.0,
                }
            ],
        )

        def step(payload, **_kwargs):
            calls.append(dict(payload))
            metadata = runtime.agent_event(0).metadata
            metadata["lastActionSuccess"] = False
            metadata["errorMessage"] = runtime_module.PICKUP_OBJECT_CLIP_ERROR
            return FakeEvent(metadata=metadata)

        def teleport_to_position_direct(*_args, **_kwargs):
            raise AssertionError("BreakObject clip failures should not teleport")

        runtime.step = step
        runtime.teleport_to_position_direct = teleport_to_position_direct

        with self.assertRaisesRegex(
            RuntimeError,
            runtime_module.PICKUP_OBJECT_CLIP_ERROR,
        ):
            runtime.object_action("BreakObject", "robot1", "Vase")

        self.assertEqual([call["action"] for call in calls], ["BreakObject"])
        self.assertEqual(runtime.total_exec, 1)
        self.assertEqual(runtime.success_exec, 0)

    def test_break_egg_visibility_retry_records_broken_goal_state(self):
        object_id = "Egg|+00.00|+00.90|+01.00"
        broken_object_id = "EggCracked|+00.00|+00.90|+01.00"
        runtime, calls = object_action_runtime(
            [
                {
                    "objectId": object_id,
                    "objectType": "Egg",
                    "name": "Egg",
                    "visible": True,
                    "distance": 1.0,
                }
            ],
            horizon=35.0,
        )
        base_step = runtime.step
        break_attempts = 0

        def step(payload, **kwargs):
            nonlocal break_attempts
            if payload.get("action") != "BreakObject":
                return base_step(payload, **kwargs)
            calls.append(dict(payload))
            break_attempts += 1
            if break_attempts == 1:
                raise RuntimeError(
                    "NullReferenceException: "
                    f"{runtime_module.PICKUP_OBJECT_TARGET_VISIBILITY_ERROR}"
                )
            runtime._test_state["objects"].append(
                {
                    "objectId": broken_object_id,
                    "objectType": "EggCracked",
                    "name": "EggCracked",
                    "visible": True,
                    "distance": 1.0,
                }
            )
            return FakeEvent(metadata=runtime.agent_event(0).metadata)

        runtime.step = step

        runtime.object_action("BreakObject", "robot1", "Egg")

        self.assertEqual(
            [call["action"] for call in calls],
            ["BreakObject", "LookUp", "BreakObject"],
        )
        self.assertTrue(goal_state_verified("Egg", "BROKEN"))
        self.assertIn("egg", runtime.operated_object_names)
        self.assertIn("eggcracked", runtime.operated_object_names)
        self.assertEqual(runtime.total_exec, 1)
        self.assertEqual(runtime.success_exec, 1)

    def test_pickup_non_clip_error_does_not_backoff_retry(self):
        object_id = "Apple|+00.00|+00.90|+01.00"
        runtime, calls = object_action_runtime(
            [
                {
                    "objectId": object_id,
                    "objectType": "Apple",
                    "name": "Apple",
                    "visible": True,
                    "distance": 1.0,
                }
            ],
        )

        def step(payload, **_kwargs):
            calls.append(dict(payload))
            metadata = runtime.agent_event(0).metadata
            metadata["lastActionSuccess"] = False
            metadata["errorMessage"] = "object is not visible"
            return FakeEvent(metadata=metadata)

        def teleport_to_position_direct(*_args, **_kwargs):
            raise AssertionError("non-clip Pickup failures should not teleport")

        runtime.step = step
        runtime.teleport_to_position_direct = teleport_to_position_direct

        with self.assertRaisesRegex(RuntimeError, "object is not visible"):
            runtime.object_action("PickupObject", "robot1", "Apple")

        self.assertEqual([call["action"] for call in calls], ["PickupObject"])
        self.assertEqual(runtime.total_exec, 1)
        self.assertEqual(runtime.success_exec, 0)

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

    def test_teleport_candidates_backfill_from_global_reachable_positions(self):
        runtime = runtime_without_init()
        target = {"x": 0.0, "y": 0.0, "z": 0.0}
        current_position = {"x": 3.25, "y": 0.0, "z": 0.0}
        blocked_position = {"x": 0.5, "y": 0.0, "z": 0.0}
        runtime.reachable_positions = [current_position]
        runtime.global_reachable_positions = [
            current_position,
            *[
                {"x": 0.25 * index, "y": 0.0, "z": 0.0}
                for index in range(1, 13)
            ],
        ]
        blocker_bounds = (
            "Blocker|+00.50|+00.00|+00.00",
            (0.45, 0.55, -1.0, 1.0, -0.05, 0.05),
            0.0,
        )
        runtime.scene_object_bounds = lambda _agent_id: [blocker_bounds]

        candidates = runtime.teleport_candidate_positions(
            target,
            agent_id=0,
            include_agent_positions=False,
        )

        candidate_keys = [position_to_grid_key(position) for position in candidates]
        self.assertEqual(len(candidates), runtime_module.TELEPORT_CANDIDATE_LIMIT)
        self.assertEqual(len(candidate_keys), len(set(candidate_keys)))
        self.assertNotIn(position_to_grid_key(blocked_position), candidate_keys)
        self.assertEqual(
            [position["x"] for position in candidates],
            [0.25, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 3.25],
        )

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

    def test_emptyliquid_helper_calls_empty_liquid_from_object(self):
        runtime = FillWaterRuntime(put_error=None)
        runtime.mug["fillLiquid"] = "water"

        run_emptyliquid_with_runtime(runtime)

        self.assertEqual(runtime.calls, [("EmptyLiquidFromObject", "Mug")])
        self.assertEqual(runtime.mug["fillLiquid"], "")

    def test_fillwater_skips_fill_action_when_object_already_has_water(self):
        runtime = FillWaterRuntime(put_error=None, fill_on_faucet_on=True)

        run_fillwater_with_runtime(runtime)

        self.assertEqual(
            [call[0] for call in runtime.calls],
            [
                "PutObject",
                "ToggleObjectOn",
                "ToggleObjectOff",
                "PickupObject",
            ],
        )
        self.assertEqual(runtime.mug["fillLiquid"], "water")

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
