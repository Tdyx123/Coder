import sys
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


from executor_system.movement import (
    MovementMode,
    NavigationMetrics,
    NavigationRequest,
    NoInteractionPoseError,
)
from executor_system.runtime import ThorRuntime
from executor_system.step_movement import StepMovementStrategy
from executor_system.teleport_movement import TeleportMovementStrategy


def navigation_request(
    destination,
    candidate,
    *,
    phase_coordinator=None,
):
    center = dict(destination["position"])
    return NavigationRequest(
        robot="robot1",
        agent_id=0,
        dest_obj="Apple",
        destination=dict(destination),
        center=center,
        candidate_positions=(dict(candidate),),
        object_resource=destination["objectId"],
        next_action=None,
        phase_coordinator=phase_coordinator,
    )


class MovementStrategySelectionTest(unittest.TestCase):
    def test_runtime_defaults_to_step_strategy(self):
        runtime = object.__new__(ThorRuntime)
        runtime.physical_agent_count = 1

        runtime.configure_movement(None, environ={})

        self.assertIs(runtime.movement_config.mode, MovementMode.STEP)
        self.assertIsInstance(runtime.movement_strategy, StepMovementStrategy)


class TeleportMovementStrategyTest(unittest.TestCase):
    def test_wait_rebuild_move_notify_order_uses_rebuilt_destination(self):
        order = []
        first_destination = {
            "objectId": "Apple|first",
            "objectType": "Apple",
            "position": {"x": 1.0, "y": 0.9, "z": 0.0},
        }
        second_destination = {
            "objectId": "Apple|second",
            "objectType": "Apple",
            "position": {"x": 1.1, "y": 0.9, "z": 0.0},
        }
        first_candidate = {"x": 0.75, "y": 0.0, "z": 0.0}
        second_candidate = {"x": 1.25, "y": 0.0, "z": 0.0}

        class RecordingCoordinator:
            def wait_until_goto_candidates_clear(self, agent_id, positions):
                order.append(("wait", agent_id, tuple(dict(item) for item in positions)))
                return True

            def notify_agent_position_changed(self, agent_id):
                order.append(("notify", agent_id))

        coordinator = RecordingCoordinator()
        initial_request = navigation_request(
            first_destination,
            first_candidate,
            phase_coordinator=coordinator,
        )
        rebuilt_request = navigation_request(
            second_destination,
            second_candidate,
            phase_coordinator=coordinator,
        )

        class RecordingRuntime:
            def build_navigation_request(self, *args, **kwargs):
                order.append(("rebuild", args, kwargs))
                return rebuilt_request

            def teleport_and_face_candidate_positions(
                self,
                agent_id,
                positions,
                *,
                face_target,
                object_resource,
                search_center,
                restrict_to_candidate_positions,
            ):
                order.append(
                    (
                        "teleport_and_face",
                        agent_id,
                        tuple(dict(item) for item in positions),
                        dict(face_target),
                        object_resource,
                        dict(search_center),
                        restrict_to_candidate_positions,
                    )
                )
                return dict(positions[0])

        metrics = NavigationMetrics(MovementMode.TELEPORT)
        strategy = TeleportMovementStrategy(RecordingRuntime(), metrics)

        result = strategy.navigate(initial_request)

        self.assertEqual(
            order,
            [
                ("wait", 0, (first_candidate,)),
                (
                    "rebuild",
                    ("robot1", "Apple"),
                    {
                        "next_action": None,
                        "phase_coordinator": coordinator,
                        "action_wave": None,
                    },
                ),
                (
                    "teleport_and_face",
                    0,
                    (second_candidate,),
                    second_destination["position"],
                    "Apple|second",
                    second_destination["position"],
                    True,
                ),
                ("notify", 0),
            ],
        )
        self.assertEqual(result.destination, second_destination)
        self.assertEqual(result.position, second_candidate)

    def test_interaction_target_drives_facing_and_visibility_candidates(self):
        original_destination = {
            "objectId": "CounterTop|1",
            "objectType": "CounterTop",
            "position": {"x": 1.0, "y": 0.9, "z": 0.0},
        }
        interaction_destination = {
            "objectId": "CreditCard|1",
            "objectType": "CreditCard",
            "position": {"x": 1.25, "y": 0.9, "z": 0.0},
        }
        candidates = (
            {"x": 0.75, "y": 0.0, "z": 0.0},
            {"x": 1.5, "y": 0.0, "z": 0.0},
        )
        request = NavigationRequest(
            robot="robot1",
            agent_id=0,
            dest_obj="CounterTop",
            destination=dict(original_destination),
            center=dict(original_destination["position"]),
            candidate_positions=tuple(dict(item) for item in candidates),
            object_resource="CounterTop|1",
            next_action=None,
            phase_coordinator=None,
            interaction_target="CreditCard",
            interaction_destination=dict(interaction_destination),
            interaction_center=dict(interaction_destination["position"]),
            interaction_object_resource="CreditCard|1",
            interaction_target_replaced=True,
        )

        class RecordingRuntime:
            def __init__(self):
                self.calls = []
                self.selected = None

            def teleport_and_face_candidate_positions(self, agent_id, positions, **kwargs):
                excluded = {
                    (round(float(item[0]), 2), round(float(item[1]), 2))
                    for item in kwargs.get("excluded_grid_keys", set())
                }
                selected = next(
                    item
                    for item in positions
                    if (round(float(item["x"]) / 0.25), round(float(item["z"]) / 0.25))
                    not in excluded
                )
                self.selected = dict(selected)
                self.calls.append((agent_id, dict(kwargs), dict(selected)))
                return dict(selected)

            def find_object(self, target, *, agent_id, require_center=False):
                result = dict(interaction_destination)
                result["visible"] = self.selected == candidates[1]
                return result

        runtime = RecordingRuntime()
        metrics = NavigationMetrics(MovementMode.TELEPORT)

        result = TeleportMovementStrategy(runtime, metrics).navigate(request)

        self.assertEqual(result.destination, original_destination)
        self.assertEqual(result.position, candidates[1])
        self.assertEqual(len(runtime.calls), 2)
        for _agent_id, kwargs, _selected in runtime.calls:
            self.assertEqual(kwargs["face_target"], interaction_destination["position"])
            self.assertEqual(kwargs["object_resource"], "CreditCard|1")
            self.assertEqual(kwargs["search_center"], interaction_destination["position"])
        self.assertEqual(metrics.to_dict()["invisible_interaction_candidates"], 1)

    def test_all_invisible_teleport_candidates_raise_no_interaction_pose(self):
        destination = {
            "objectId": "CreditCard|1",
            "objectType": "CreditCard",
            "position": {"x": 1.25, "y": 0.9, "z": 0.0},
        }
        candidates = (
            {"x": 0.75, "y": 0.0, "z": 0.0},
            {"x": 1.5, "y": 0.0, "z": 0.0},
        )
        request = NavigationRequest(
            robot="robot1",
            agent_id=0,
            dest_obj="CounterTop",
            destination={"objectId": "CounterTop|1", "position": destination["position"]},
            center=dict(destination["position"]),
            candidate_positions=candidates,
            object_resource="CounterTop|1",
            next_action=None,
            phase_coordinator=None,
            interaction_target="CreditCard",
            interaction_destination=dict(destination),
            interaction_center=dict(destination["position"]),
            interaction_object_resource="CreditCard|1",
            interaction_target_replaced=True,
        )

        class InvisibleRuntime:
            def teleport_and_face_candidate_positions(self, _agent_id, positions, **kwargs):
                excluded = kwargs.get("excluded_grid_keys", set())
                return next(
                    dict(item)
                    for item in positions
                    if (round(item["x"] / 0.25), round(item["z"] / 0.25))
                    not in set(excluded)
                )

            def find_object(self, *_args, **_kwargs):
                return {**destination, "visible": False}

        with self.assertRaises(NoInteractionPoseError):
            TeleportMovementStrategy(
                InvisibleRuntime(),
                NavigationMetrics(MovementMode.TELEPORT),
            ).navigate(request)


class NavigationActionScopeTest(unittest.TestCase):
    def test_only_actions_inside_navigation_scope_are_counted(self):
        class RecordingController:
            def step(self, payload):
                return type(
                    "Event",
                    (),
                    {
                        "metadata": {
                            "lastActionSuccess": True,
                            "lastAction": payload.get("action"),
                        }
                    },
                )()

        runtime = object.__new__(ThorRuntime)
        runtime.physical_agent_count = 1
        runtime.configure_movement("step", environ={})
        runtime.controller = RecordingController()
        runtime.controller_lock = threading.RLock()
        runtime.save_frames = lambda _event: None
        runtime.assert_success = lambda _event, _payload: None

        runtime._step_direct(
            {"action": "Initialize", "agentId": 0},
            check_success=False,
            save_frame=False,
        )
        runtime._step_direct(
            {"action": "Teleport", "agentId": 0},
            check_success=False,
            save_frame=False,
        )
        with runtime.navigation_action_scope():
            runtime._step_direct(
                {"action": "MoveAhead", "agentId": 0},
                check_success=False,
                save_frame=False,
            )

        self.assertEqual(
            runtime.navigation_metrics.to_dict()["action_counts"],
            {"MoveAhead": 1},
        )


if __name__ == "__main__":
    unittest.main()
